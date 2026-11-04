"""The desktop architecture test with fixes for the failure causes of
``desktop_e2e5``.   Builds on ``desktop_e2e`` ...
``desktop_e2e5`` (all left byte-identical) and is PURE like them (no pyobjc,
no subprocess, no network): the guest, the planner and the picker are
injected callables.

``desktop_e2e5`` failed its rule on the planner (a Notes SEARCH that was the
whole "find" of a described note, never followed by opening it), the
judge (Notes' search switches the sidebar to its search scope "Alle auf meinem
Mac"), the executor (TextEdit's settings for new documents live in its
Einstellungen window, which the harness could not operate) and the
picker (right picks below t2; "folding tables" vs German contents). The
generic pieces added here:

1. **Find means show**: a goal to find, locate, look up or show an item ends
   with that item OPEN, never with only a search list (a planner rule and a
   deterministic post-plan check that appends the open step; a search the
   user asked for stays in front of it).
2. **judge**: the note shown and its content decide; while a Notes search is
   active the folder item reads "the declared folder or the search scope".
3. **settings_set**: a generic executor capability for an app's own settings
   window (the app menu's Einstellungen/Settings item): a named tab, a
   checkbox, choice or pop-up set by its label, verified, the window closed.
4. **The language bridge**: the screen summary names the UI language; the
   planner phrases a target in the user's words AND in the UI language
   (``target_ui``); the picker question gives both; candidates keep their
   on-screen text, compacted to OpenJev's 120 characters per option.
5. **t2 recalibrated** for OpenJev on a larger development set.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import desktop_e2e as X
from . import desktop_e2e2 as Y
from . import desktop_e2e3 as Z
from . import desktop_e2e4 as W
from . import desktop_e2e5 as V

HERE = os.path.dirname(os.path.abspath(__file__))
PICKER6_PATH = os.path.join(HERE, "desktop_e2e6_picker.json")
PLANNER6_PATH = os.path.join(HERE, "desktop_e2e6_planner.json")
TASKS6_PATH = os.path.join(HERE, "desktop_e2e6_tasks.json")
GEN6 = X.GEN + "/e6"

# ---------------------------------------------------------------------------
# 1. Find means show
# ---------------------------------------------------------------------------

#: words that ask to FIND, LOCATE, LOOK UP or SHOW an item ("look up" as a bigram)
FIND_WORDS = {"find", "finde", "finden", "findest", "findet", "locate", "zeig", "zeige", "zeigen", "show",
              "such", "suche", "suchen", "sucht", "suchst", "search", "searches", "searching", "raussuchen",
              "heraussuchen", "durchsuch", "durchsuche", "durchsuchen", "lookup", "wo"}
#: a SEARCH the user asks for: English "search", German "such ... nach" / "durchsuchen"
_SEARCH_EN = {"search", "searches", "searching"}
_SUCH_DE = {"such", "suche", "suchen", "sucht", "suchst"}
_DURCH_DE = {"durchsuch", "durchsuche", "durchsuchen", "durchsucht"}


def asks_find(goal: str) -> bool:
    """Does the goal ask to find, locate, look up or show something?"""
    toks = X.tokens(goal)
    if set(toks) & FIND_WORDS:
        return True
    return any(a == "look" and b == "up" for a, b in zip(toks, toks[1:]))


def asks_search(goal: str) -> bool:
    """Does the USER ask for a search (not only for finding an item)? "Search my
    notes for ...", "Such in meinen Notizen nach ...", "durchsuch ...", or a
    pattern the goal quotes after a search word; "Such sie bitte raus" (find
    it) and "find the one about ..." ask for none."""
    toks = X.tokens(goal)
    for k, t in enumerate(toks):
        if t in _SEARCH_EN or t in _DURCH_DE:
            return True
        if t in _SUCH_DE and ("nach" in toks[k + 1:k + 8] or "nach" in toks[max(0, k - 4):k]):
            return True
    return False


def find_means_show(goal: str, steps: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(steps, changes): when the goal asks to find / locate / look up / show an
    item and the plan leaves a content app (Notes, Finder) with a SEARCH as its
    last step there (a search list, no item open), an
    ``open_item`` for the matched item is appended right after that search. Its
    target is the search's words (the engine decides the row by the map when the
    words are the user's and name one row, else by the picker, which reads the
    whole goal). A search the user did not ask for is then dropped by
    ``refine_plan5``; one the user asked for stays in front of the open."""
    out = [dict(s) for s in steps]
    changes: List[str] = []
    if not asks_find(goal):
        return out, changes
    for app in sorted(Y.CONTENT_APPS):
        idx = [i for i, s in enumerate(out) if s.get("app") == app]
        if not idx:
            continue
        j = idx[-1]
        s = out[j]
        if s.get("effect") != "scoped_find" or not X.norm(s.get("value")) or W.VAR_RE.search(str(s.get("value"))):
            continue
        if V.goal_names_search(goal, s["value"]):
            # the user names this search pattern ("Search for 'invoice' only in the Archive folder"): the search
            # list IS what the user asked for, no item is described (checked offline on held-out goals)
            continue
        new = {"effect": "open_item", "app": app, "target": s["value"], "value": "", "scope": "", "mode": "",
               "find": "", "target_ui": s.get("target_ui") or "", "added_by": "find means show"}
        out.insert(j + 1, new)
        changes.append(f"find means show: appended open_item({app}: {s['value']!r}) after the search that ended "
                       f"the plan in {app} (a search list alone shows no item)")
    return out, changes


#: apps whose screen summary lists their open documents (a menu setting acts on the front document)
DOC_APPS = {"TextEdit": "textedit_documents"}


def route_settings_by_screen(ref: Y.Refined, screen: Optional[Dict[str, Any]], emap=None) -> None:
    """A menu setting (flag_toggle) of a document app that the screen shows
    running with NO document open, planned before any step that opens or
    creates a document, has no document to act on: the setting the user wants
    lives in the app's settings for new documents, so the step becomes
    settings_set (the w6 planner can still plan flag_toggle there)."""
    if not screen:
        return
    for app, key in DOC_APPS.items():
        if screen.get(key) != []:
            continue                                  # a document is open (or the app does not run)
        opened = False
        for i, s in enumerate(ref.steps):
            if s["effect"] in ("open_item", "object_create") and s["app"] in (app, "Finder"):
                opened = True
            if s["effect"] == "flag_toggle" and s["app"] == app and not opened:
                new = dict(s, effect="settings_set", flag_target=s["target"])
                # a target that names a menu GROUP ("Rechtschreibung und Grammatik"), not one flag, names no
                # setting either (the label rule would name "Rechtschreibung und Grammatik prüfen"): the
                # setting is then chosen by the user's words (target_ui, else the picker)
                if emap is not None and emap.resolve_flag(app, s["target"]).by != "map":
                    ui = s.get("target_ui") or ""
                    other = bool(ui) and X.norm(ui) != X.norm(s["target"])
                    # the same group name as target_ui would name the other setting again
                    new["target"], new["target_ui"] = (ui if other else ""), ""
                ref.steps[i] = new
                ref.changes.append(f"flag_toggle({s['target']!r}) -> settings_set: the screen shows {app} with no "
                                   "document open (a menu setting has no document to act on; the app's settings "
                                   "for new documents hold it)")


def _key(s: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return s.get("effect") or "", s.get("app") or "", X.norm(s.get("target")), X.norm(s.get("value"))


def refine_plan6(goal: str, steps: Sequence[Dict[str, Any]], emap) -> Y.Refined:
    """``refine_plan5`` with find-means-show in front and
    one change: a search the USER asks for ("search my notes for", "such nach")
    stays in front of the open/select that follows it (``refine_plan5``'s rules
    and the minimal-plan rule drop a search right before an open; a goal that
    asks for a search wants the search AND the note shown). A search the user
    did not ask for is dropped as before."""
    st, changes = find_means_show(goal, steps)
    ref = V.refine_plan5(goal, st, emap)
    if asks_search(goal):
        have = {_key(s) for s in ref.steps}
        for i, s in enumerate(st):
            if s.get("effect") != "scoped_find" or s.get("app") not in Y.CONTENT_APPS or _key(s) in have:
                continue
            nxt = st[i + 1] if i + 1 < len(st) else None
            if nxt is None or nxt.get("app") != s.get("app") or nxt.get("effect") not in ("open_item", "select_item"):
                continue
            k = next((j for j, t in enumerate(ref.steps) if _key(t) == _key(nxt)), None)
            if k is None:
                continue
            ref.steps.insert(k, dict(s, scope=""))
            have.add(_key(s))
            ref.changes.append(f"kept scoped_find({s['value'][:40]!r}) before {nxt['effect']}: the user asks for a "
                               "search")
    ref.changes = changes + ref.changes
    return ref


# ---------------------------------------------------------------------------
# The planner w6
# ---------------------------------------------------------------------------

def load_planner6_spec(path: str = PLANNER6_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def planner6_messages(goal: str, spec: Optional[Dict[str, Any]] = None,
                      history: Optional[List[Dict[str, str]]] = None,
                      context: Optional[str] = None) -> List[Dict[str, str]]:
    """w6's messages (``desktop_e2e4``'s layout: the goal, the screen line as data)."""
    spec = spec or load_planner6_spec()
    user = spec["user_prefix"] + goal + ((spec["context_prefix"] + context) if context else "")
    return ([{"role": "system", "content": spec["w6_system"]}, {"role": "user", "content": user}]
            + list(history or []))


def parse_plan6(text: str, spec: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """``parse_plan4`` (w6's vocabulary) keeping ``target_ui`` (the target in
    the UI's language; "" when the planner gives none)."""
    spec = spec or load_planner6_spec()
    steps, err = W.parse_plan4(text, spec)
    if err:
        return steps, err
    raw = []
    try:
        t = (text or "").strip()
        obj = json.loads(t[t.find("{"): t.rfind("}") + 1])
        raw = [s for s in obj.get("steps") or [] if isinstance(s, dict)]
    except ValueError:
        raw = []
    for s, r in zip(steps, raw + [{}] * (len(steps) - len(raw))):
        s["target_ui"] = str(r.get("target_ui") or "")
    return steps, None


# ---------------------------------------------------------------------------
# The architecture arm
# ---------------------------------------------------------------------------

def load_picker6(path: str = PICKER6_PATH) -> Dict[str, Any]:
    """The frozen acceptance calibration of this module."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def t2_of6(picker: str, spec: Optional[Dict[str, Any]] = None) -> float:
    """This module's t2 of a picker (OpenJev only)."""
    t = ((spec or load_picker6())["pickers"].get(picker) or {}).get("t2")
    if t is None:
        raise KeyError(f"no calibrated t2 for the picker {picker!r}")
    return float(t)


class Arch6(V.Arch5):
    """``Arch5`` with this module's fixes (see the module docstring)."""

    def __init__(self, guest, emap, planner=None, picker_name: Optional[str] = None, **kw):
        super().__init__(guest, emap, planner=planner, picker_name=picker_name, **kw)
        if picker_name not in (None, "none"):
            self.threshold = t2_of6(picker_name)          # t2 recalibrated on the larger development set

    def _settings_set(self, task, rec, step, log, bid, fixed):
        return _SettingsMixin._settings_set(self, task, rec, step, log, bid, fixed)

    # -- the language bridge: the UI language on the screen line; both phrasings in the picker question ----
    def plan(self, rec: X.Rec, goal: str):
        """``desktop_e2e4``'s plan (the screen line as data) with the UI language named."""
        if self.planner is None:
            raise X.GuestError("model needed: no planner in compiled mode")
        sc = self.g("screen")
        ok = isinstance(sc, dict) and sc.get("status") == "done" and isinstance(sc.get("screen"), dict)
        self._screen_raw = sc["screen"] if ok else None
        self._screen = screen_line6(sc["screen"]) if ok else None
        self._ui_lang = (sc["screen"].get("ui_language") if ok else None) or getattr(self, "_ui_lang", None)
        text, steps, err, entry = self._call_planner(rec, goal, screen=self._screen)
        if not err and W.refused_unsupported(steps):
            hist = W.refusal_feedback(text, self._spec5())
            text, steps, err, entry = self._call_planner(rec, goal, hist, self._screen)
            entry["history"] = hist
            entry["refusal_feedback"] = True
        return self._checked(rec, goal, text, steps, err, entry)

    def pick(self, rec: X.Rec, goal: str, state: str, cands: List[Dict[str, Any]], step_log: Dict[str, Any],
             question: Optional[str] = None):
        """``Arch5``'s pick (absolute dates, both orders, min confidence >= t2, the
        date veto) with new inputs: options fitted to OpenJev's 120
        characters (content kept), the UI language in the state, and the
        question giving the item in the user's words AND the screen's language."""
        if self.picker_name == "none" and self.planner is not None:
            step_log["pick"] = {"index": None, "reason": "no picker (mode none): hand-off", "calls": 0, "cost": 0.0,
                                "answers": [], "options": [fit_option(c["text"]) for c in cands], "state": state}
            rec.handoffs += 1
            rec.gated += 1
            self._inbox(rec, "picker hand-off: no picker (mode none)", step_log)
            return None
        if self.chooser is None:
            raise X.GuestError("model needed: no picker in compiled mode")
        kind = V.cand_kind(state)
        shown = cands
        if kind:
            shown = self._dates(kind, cands, step_log)
            state = fitted_state(kind, V.dated_state(kind, state, self._now))
        state = ui_state(state, getattr(self, "_ui_lang", None))
        step = step_log.get("step") or {}
        bq = bridge_question(goal, step.get("target") or "", step.get("target_ui") or "",
                             getattr(self, "_ui_lang", None) or "de", question)
        q = bq or question
        opts = [fit_option(c["text"]) for c in shown]
        sp = None
        if self.spend is not None:
            def sp(k, actual=None, _k=self.picker_kind):
                return self.spend(_k, actual=actual) if actual is not None else self.spend(_k)
        p = V.consistent_pick5(self.chooser, goal, state, opts, threshold=self.threshold, spend=sp, question=q,
                               chars=OPTION_CHARS6)
        rec.calls["picker"] += p.calls
        rec.cost += p.cost
        step_log["pick"] = dict(p.to_dict(), options=opts, state=state, t2=self.threshold,
                                rule="both orders agree and min confidence >= t2")
        if q:
            step_log["pick"]["question"] = q
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

    def _spec5(self) -> Dict[str, Any]:
        # the w6 planner's spec everywhere ``Arch5`` reads its spec (templates, the vocabulary)
        if getattr(self, "_spec6_cache", None) is None:
            self._spec6_cache = load_planner6_spec()
        return self._spec6_cache

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
        steps, err = parse_plan6(text, self._spec5())
        entry = {"text": (text or "")[:4000], "steps": steps, "error": err or (meta or {}).get("error"),
                 "usage": (meta or {}).get("usage"), "feedback": bool(history), "screen": screen}
        rec.plan.append(entry)
        return text, steps, err, entry

    def _checked_once(self, rec, goal, text, steps, err, entry, done=()):
        """``Arch5``'s restriction feedback and plan check with ``refine_plan6``."""
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
        ref = refine_plan6(goal, steps, self.m)
        W.route_by_screen(ref, getattr(self, "_screen_raw", None))
        route_settings_by_screen(ref, getattr(self, "_screen_raw", None) if not done else None, self.m)
        entry["refined"] = ref.to_dict()
        entry["pending"] = ref.pending
        if ref.problems:
            return [], "; ".join(ref.problems), text, entry
        self._pending = ref.pending
        return ref.steps, None, text, entry


# ---------------------------------------------------------------------------
# 2. judge: the note shown and its content; a search-scoped sidebar
# ---------------------------------------------------------------------------

#: the sidebar items Notes selects while a search is active (its search scopes)
SEARCH_SCOPE_PREFIXES = ("alle ", "all ")
SEARCH_SCOPE_NAMES = {"suchergebnisse", "search results"}


def is_search_scope(label: Any) -> bool:
    """Is a Notes sidebar item a SEARCH SCOPE ("Alle auf meinem Mac", "All iCloud",
    ...), which Notes selects while a search is active, rather than a folder?"""
    n = X.norm(label)
    return bool(n) and (n.startswith(SEARCH_SCOPE_PREFIXES) or n in SEARCH_SCOPE_NAMES)


def _flat(text: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).casefold().split())


def _probe(task: X.Task, kind: str) -> Optional[int]:
    return next((i for i, p in enumerate(task.read) if p.get("kind") == kind), None)


def judge_from(j: Dict[str, Any], task: X.Task, start, end) -> Dict[str, Any]:
    """The ``judge`` verdict (judge + TextEdit's new-document settings)
    re-read with this module's rule for the note SHOWN: the item
    "<folder> / <note> shown" is ok when the editor shows the author's note
    (its title, and its text where the reader read it and the translation
    declares it) and the sidebar reads the declared folder, OR the search scope
    while a search is active (Notes switches the sidebar to "Alle auf meinem
    Mac" for every search). A note shown whose text is not the author's is
    wrong. Nothing else changes; success and wrong actions are recounted only
    when an item changed."""
    out = json.loads(json.dumps(j))
    out = _notes_folder_item(out, task, start, end)
    want = task.expect.get("notes_shown")
    k = _probe(task, "notes")
    if not want or k is None:
        return out
    e0 = ((end or [])[k] if k < len(end or []) else {}) or {}
    s0 = ((start or [])[k] if k < len(start or []) else {}) or {}
    label = f"{want['folder']} / {want['note']} shown"
    item = next((i for i in out.get("items") or [] if i.get("what") == label), None)
    if item is None or not e0.get("ok"):
        return out
    search_on = bool(_flat(e0.get("search")))
    folder = e0.get("folder")
    folder_ok = folder == want["folder"] or (search_on and is_search_scope(folder))
    note_ok = e0.get("shown_note") == want["note"]
    text, shown = want.get("text"), e0.get("shown_text")
    content = None if text is None or shown is None else _flat(text) in _flat(shown)
    if folder_ok and note_ok and content is not False:
        new = "ok"
    elif item["status"] == "ok" and content is False:
        new = "wrong"
    elif (folder, e0.get("shown_note")) == (s0.get("folder"), s0.get("shown_note")):
        new = "missing"
    else:
        new = "wrong" if item["status"] == "ok" else item["status"]
    why = ("the declared folder" if folder == want["folder"] else f"the search scope {folder!r} while the search "
           f"{e0.get('search')!r} is active" if folder_ok else f"the sidebar reads {folder!r}")
    item["judge_note"] = (f"{why}; the note shown {e0.get('shown_note')!r}"
                      + ("" if content is None else f"; its text {'is' if content else 'is NOT'} the author's"))
    if new != item["status"]:
        item["judge_changed"] = f"{item['status']} -> {new}"
        item["status"] = new
        items = out["items"]
        out["success"] = bool(items) and all(i["status"] == "ok" for i in items) and not out.get("unexpected") \
            and not out.get("irreversible")
        out["wrong_actions"] = sum(1 for i in items if i["status"] == "wrong") + len(out.get("unexpected") or [])
    out["judge"] = "judge_notes"
    return out


def _recount(out: Dict[str, Any]) -> None:
    items = out["items"]
    out["success"] = bool(items) and all(i["status"] == "ok" for i in items) and not out.get("unexpected") \
        and not out.get("irreversible")
    out["wrong_actions"] = sum(1 for i in items if i["status"] == "wrong") + len(out.get("unexpected") or [])


def _notes_folder_item(out: Dict[str, Any], task: X.Task, start, end) -> Dict[str, Any]:
    """``expect["notes_folder"]`` (the translation of a folder the author names
    without a note): "Notes shows the folder F" is ok when the sidebar reads F,
    or the search scope while a search is active; missing when it reads what it read
    at the start; else wrong."""
    want = task.expect.get("notes_folder")
    k = _probe(task, "notes")
    if not want or k is None:
        return out
    e0 = ((end or [])[k] if k < len(end or []) else {}) or {}
    s0 = ((start or [])[k] if k < len(start or []) else {}) or {}
    folder = e0.get("folder")
    if not e0.get("ok"):
        st = "missing"
    elif folder == want or (bool(_flat(e0.get("search"))) and is_search_scope(folder)):
        st = "ok"
    else:
        st = "missing" if folder == s0.get("folder") else "wrong"
    out["items"].append({"what": f"Notes shows the folder {want!r}", "status": st, "judge_note": f"the sidebar reads "
                         f"{folder!r}" + (f" while the search {e0.get('search')!r} is active" if e0.get("search") else "")})
    out["judge"] = "judge_notes"
    _recount(out)
    return out


def judge(task: X.Task, start, end, record=None, emap=None, reader=None,
           statements: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """judge (``desktop_e2e5``'s independent judge) + this module's note-shown rule."""
    return judge_from(V.judge(task, start, end, record, emap, reader, statements), task, start, end)


def run_one6(task: X.Task, arm: str, run: int, guest, *, arch=None, baseline=None, compiled=None,
             mode: str = "measured", emap=None, reader=None, statements: Optional[Dict[str, str]] = None):
    """``run_one5`` (a clean start, twice at most; the arm; the reader), judged by
    judge for EVERY arm (the judge verdict kept as ``judge_prefs``)."""
    out = V.run_one5(task, arm, run, guest, arch=arch, baseline=baseline, compiled=compiled, mode=mode, emap=emap,
                     reader=reader, statements=statements)
    j = out.get("judge") or {}
    if j and not j.get("not_run"):
        out["judge_prefs"] = j
        out["judge"] = judge_from(j, task, out.get("read_start") or [], out.get("read_end") or [])
    return out


# ---------------------------------------------------------------------------
# 3. settings_set: an app's own settings window (generic; TextEdit is the first case)
# ---------------------------------------------------------------------------

SETTINGS_STATE = ("Frontmost application: {app}. The listed items are the settings in its settings window "
                  "(Einstellungen), given as tab > label (kind: current value).")


def settings_candidates(read: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The controls of a settings read (every tab), each with the text the picker
    reads: "<tab> > <label> (<kind>[ in <group>]: <value>)"."""
    out = []
    for tab, cs in (read.get("controls") or {}).items():
        if not isinstance(cs, list):
            continue
        for c in cs:
            if not (c.get("label") or "").strip():
                continue
            v = c.get("value")
            vs = ("on" if v else "off") if isinstance(v, bool) else str(v)
            grp = f" in {c['group']}" if c.get("group") else ""
            out.append(dict(c, tab=tab, text=f"{tab + ' > ' if tab else ''}{c['label'].strip()} ({c['kind']}{grp}: {vs})"))
    return out


def label_match6(value: Any, labels: Sequence[Any]) -> List[int]:
    """The map's label rule (``label_match``), else the same with abbreviations: a
    word the value writes with a trailing dot ("autom.") names a label word it
    begins ("automatisch"; TextEdit's menu says "Rechtschreibung autom.
    korrigieren", its settings window "Rechtschreibung automatisch korrigieren")."""
    hit = X.label_match(value, labels)
    if hit:
        return hit
    raw = unicodedata.normalize("NFC", str(value or "")).casefold()
    abbr = {m for m in re.findall(r"([0-9a-zäöüßéèàç]+)\.", raw)}
    vt = X.tokens(value)
    if not abbr or not vt:
        return []

    def fits(lab):
        lt = X.tokens(lab)
        return all(t in lt or (t in abbr and any(x.startswith(t) and len(x) > len(t) for x in lt)) for t in vt)
    return [i for i, lab in enumerate(labels) if fits(lab)]


def resolve_setting(target: str, value: str, cands: Sequence[Dict[str, Any]]) -> Tuple[List[int], Any, str]:
    """(indices of the controls the planned target names by the map's label rule,
    the value to set, how). ``target`` = "<label>" or "<tab> > <label>"; an on/off
    value sets a checkbox or selects a choice; any other value is a pop-up's
    option, or a choice of the group the target names."""
    parts = [p.strip() for p in str(target or "").split(" > ") if p.strip()]
    label = parts[-1] if parts else ""
    tab = parts[0] if len(parts) > 1 else ""
    pool = list(range(len(cands)))
    if tab:
        in_tab = [i for i in pool if X.norm(cands[i].get("tab")) == X.norm(tab)]
        pool = in_tab or pool
    want = X.on_off(value)
    if want is not None:
        pool = [i for i in pool if cands[i]["kind"] in ("checkbox", "choice")]
        hit = [pool[k] for k in label_match6(label, [cands[i]["label"].strip() for i in pool])]
        return hit, want, "label"
    # an option: a choice of the group the target names, else a pop-up the target names, else the one choice
    # the value names (the guest reads no group label when the window gives its choices none: TextEdit's Format)
    ch = [i for i in pool if cands[i]["kind"] == "choice" and X.label_match(label, [cands[i].get("group") or ""])]
    hit = [ch[k] for k in X.label_match(value, [cands[i]["label"].strip() for i in ch])] if ch else []
    if hit:
        return hit, True, "choice of its group"
    pops = [i for i in pool if cands[i]["kind"] == "popup"]
    hit = [pops[k] for k in X.label_match(label, [cands[i]["label"].strip() for i in pops])]
    if hit:
        return hit, str(value or "").strip(), "pop-up option"
    ch = [i for i in pool if cands[i]["kind"] == "choice"]
    hit = [ch[k] for k in X.label_match(value, [cands[i]["label"].strip() for i in ch])]
    return hit, (True if hit else str(value or "").strip()), "the choice the value names"


class _SettingsMixin:
    """Arch6's settings_set handler (mixed in below)."""

    def _settings_set(self, task, rec, step, log, bid, fixed):
        app = X.APP_OF[bid]
        if fixed is not None:
            if fixed.get("op") != "settings_set":
                return fixed.get("op") or "failed"
            c = {"tab": fixed.get("tab") or "", "label": fixed["label"], "kind": fixed["kind"]}
            want = fixed["value"]
            log["resolution"] = {"by": "compiled"}
        else:
            r = self.call("settings", bundle=bid, action="read")
            if r.get("status") != "done":
                log["why"] = f"settings read: {r.get('detail') or r.get('status')}"
                return "failed"
            cands = settings_candidates(r)
            log["settings_read"] = {"window": r.get("window"), "tabs": r.get("tabs"), "n": len(cands)}
            if not cands:
                log["why"] = "the settings window lists no checkbox, choice or pop-up"
                return "failed"
            hit, want, how = resolve_setting(step["target"], step["value"], cands)
            if len(hit) != 1 and (step.get("target_ui") or "").strip():
                hit2, want2, how2 = resolve_setting(step["target_ui"], step["value"], cands)
                if len(hit2) == 1:
                    hit, want, how = hit2, want2, how2 + " (the target in the screen's language)"
            if want is None or want == "":
                log["why"] = f"settings_set value {step['value']!r} is neither on/off nor an option"
                return "failed"
            state = SETTINGS_STATE.format(app=X.UI_NAMES.get(bid, app))
            if len(hit) == 1:
                c = cands[hit[0]]
                log["resolution"] = {"by": "map", "why": f"one control named by the target ({how})", "label": c["label"]}
                if self.planner is not None and not V.user_named_option(c["label"], task.text) \
                        and (self.chooser is not None or self.picker_name == "none"):
                    xc = [{"text": x["text"], "option": {"key": i, "label": x["label"]}} for i, x in enumerate(cands)]
                    q = f"Which listed setting does the user want changed? Task: {task.text}"
                    stop = self._crosscheck(rec, task, log, bid, xc, {"key": hit[0], "label": c["label"]}, q) \
                        if len(xc) > 1 else None
                    if stop:
                        return stop
            else:
                log["resolution"] = {"by": "picker", "why": f"{len(hit)} controls fit the target"}
                c = self.pick(rec, task.text, state, cands, log,
                              question=f"Which listed setting should be changed for this task: {task.text}")
                if c is None:
                    return "handoff"
                if isinstance(want, str) and c["kind"] != "popup":
                    want = True
        log["resolved"] = {"op": "settings_set", "tab": c.get("tab") or "", "label": c["label"], "kind": c["kind"],
                           "value": want}
        try:
            r = self._act(rec, log, "settings", bundle=bid, action="set", tab=c.get("tab") or "", label=c["label"],
                          kind=c["kind"], value=want)
        except X._Prompt:
            return "prompt"
        if r.get("status") == "gated":
            return "gated"
        if r.get("status") != "done" or not r.get("verified"):
            log["why"] = f"settings set: {r.get('detail') or r.get('status')}"
            return "failed"
        log["check"] = (f"the setting {c['label']!r} reads {r.get('after')!r} after the change (before "
                        f"{r.get('before')!r}); the window closed")
        return "ok"


# ---------------------------------------------------------------------------
# The tasks (a NEW independent pool); the judge is judge for every arm
# ---------------------------------------------------------------------------

def load_tasks6(path: str = TASKS6_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_task6(t: Dict[str, Any], titles, baseline, plain_text_title="", gen: str = GEN6) -> X.Task:
    """``build_task5``'s translation conventions under this module's data root ({GEN6})."""
    t = json.loads(json.dumps(t).replace("{GEN6}", "{GEN5}"))
    return V.build_task5(t, titles, baseline, plain_text_title, gen)


def tasks6(data: Optional[Dict[str, Any]] = None, drawn: Optional[bool] = True) -> List[X.Task]:
    data = data or load_tasks6()
    out = [build_task6(t, data["te_prefs_titles"], data["te_prefs_baseline"], data.get("te_prefs_plain_text") or "")
           for t in data["tasks"]]
    return [t for t in out if drawn is None or (not t.dev) == drawn]


def task6_by_id(tid: str, data: Optional[Dict[str, Any]] = None) -> X.Task:
    for t in tasks6(data, drawn=None):
        if t.id == tid:
            return t
    raise KeyError(tid)


def statements6(tid: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    data = data or load_tasks6()
    t = next(x for x in data["tasks"] if x["author_task"]["id"] == tid)
    return {k.replace("{GEN6}", GEN6): v for k, v in (t["translation"].get("statements") or {}).items()}


def tasks6_sha256(data: Optional[Dict[str, Any]] = None) -> str:
    return X.tasks_sha256(tasks6(data, drawn=True))


def seeds6_spec(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return Z.seeds3_spec(data or load_tasks6())


# ---------------------------------------------------------------------------
# 4. The language bridge; picker options that fit OpenJev's 120 characters
# ---------------------------------------------------------------------------

OPTION_CHARS6 = 120          # OpenJev's limit per option text (its API; the client cuts longer texts)
LANG_NAMES = {"de": "German", "en": "English"}
#: menu-bar titles that name the UI language (the app menus every app of this test has)
_MENU_WORDS = {"de": {"ablage", "bearbeiten", "darstellung", "fenster", "hilfe", "gehe zu", "suchen", "einfügen"},
               "en": {"file", "edit", "view", "window", "help", "go", "insert"}}


def ui_language_of(menu_titles: Sequence[Any]) -> Optional[str]:
    """The user interface's language from the front app's menu-bar titles (read
    from the screen, nothing opened): "de" / "en" / None when undecided."""
    t = {X.norm(x) for x in menu_titles or []}
    score = {k: len(t & v) for k, v in _MENU_WORDS.items()}
    best = max(score, key=score.get)
    return best if score[best] >= 2 and list(score.values()).count(score[best]) == 1 else None


def screen_line6(sc: Dict[str, Any]) -> str:
    """``desktop_e2e4``'s screen line plus the UI language (the language bridge)."""
    line = W.screen_line(sc)
    lang = sc.get("ui_language")
    if lang in LANG_NAMES:
        line = line.rstrip(".") + f"; the user interface is in {LANG_NAMES[lang]} ({lang})."
    return line


def goal_language(goal: str) -> Optional[str]:
    """A rough language of the user's words (German words / umlauts vs English
    function words), for the question only; None when undecided."""
    toks = X.tokens(goal)
    de = sum(t in {"der", "die", "das", "und", "mit", "nicht", "den", "dem", "ein", "eine", "mal", "bitte", "ich",
                   "mir", "mein", "meine", "meinen", "auf", "im", "zu", "nach", "von", "ist"} or bool(re.search("[äöüß]", t))
             for t in toks)
    en = sum(t in {"the", "and", "with", "not", "a", "an", "my", "of", "to", "in", "on", "please", "it", "is", "from",
                   "for", "about"} for t in toks)
    return "de" if de > en else "en" if en > de else None


def bridge_question(goal: str, target: str, target_ui: str, ui_lang: Optional[str],
                    base: Optional[str] = None) -> Optional[str]:
    """The picker question with BOTH phrasings of the item: the user's words and
    the screen's language (the language bridge). None when the planner gave no other phrasing
    (the default question then stands)."""
    t, tu = (target or "").strip(), (target_ui or "").strip()
    if not t or not tu or X.norm(t) == X.norm(tu):
        return None
    lang = LANG_NAMES.get(ui_lang or "", "the screen's language")
    q = base or f"Which listed item should be used for this task: {goal}"
    return (f"{q}\nThe item wanted, in the user's words: \"{t}\"; the same in the language of the screen "
            f"({lang}): \"{tu}\". The listed items are shown as they appear on the screen.")


_SIZE_RE = re.compile(r"^\d[\d.,]*\s*(byte|bytes|kb|mb|gb|ko|mo)$", re.I)
_KIND_WORDS = {"reiner text", "formatierter text", "ordner", "folder", "plain text", "rich text", "text", "dokument",
               "document", "rtf-dokument", "textdokument"}
_FOLDER_KIND = {"ordner", "folder"}


def fit_option(text: str, limit: int = OPTION_CHARS6) -> str:
    """A candidate's text within ``limit`` characters with what tells the items
    apart kept (e.g. "Bestand 04.txt | modified …, created … | 33 Byte |
    Reiner Text | content: Gegenstand: Klap" — cut at 120 characters by the
    client, both rows read "Klap"): the name, "folder" for a folder, the other
    cells (a note's first line, a place's kind), the modification date, then the
    content, shortened last. Size, file kind and the creation date (no
    information in this test's data) are left out. On-screen words are kept as
    they are (never translated)."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit and " | " not in s:
        return s or "(empty)"
    cells = s.split(" | ")
    head, rest = cells[0], cells[1:]
    date, content, other, folder = "", "", [], False
    for c in rest:
        lc = c.casefold()
        if lc.startswith("modified "):
            date = "modified " + c[len("modified "):].split(",")[0].strip()
        elif lc.startswith("content:"):
            content = c
        elif V.absolute_label(c, None):
            date = date or "modified " + V.absolute_label(c, None)
        elif _SIZE_RE.match(c.strip()):
            continue
        elif c.strip() in ("--", "—", "-", "–"):
            continue
        elif lc.strip() in _KIND_WORDS:
            folder = folder or lc.strip() in _FOLDER_KIND
        else:
            other.append(c)
    parts = [head] + (["folder"] if folder else []) + other + ([date] if date else [])
    base = " | ".join(parts)
    if content:
        room = limit - len(base) - 3
        if room >= 12:
            base = base + " | " + (content if len(content) <= room else content[:room - 1].rstrip() + "…")
    if len(base) > limit:
        base = base[:limit - 1].rstrip() + "…"
    return base


def fitted_state(kind: Optional[str], state: str) -> str:
    """The picker state's column description for the fitted options."""
    if kind == "finder":
        state = state.replace("name | modified and created (absolute dates) | size | kind | content",
                              "name | \"folder\" for a folder | modified (absolute date) | content (the file's first "
                              "words)")
    elif kind == "panel":
        state = state.replace("Each file is listed as: name | modified and created (absolute dates).",
                              "Each item is listed as: name | modified (absolute date).")
    return state


def ui_state(state: str, ui_lang: Optional[str]) -> str:
    """The picker state names the UI language read from the screen (the states
    of ``desktop_e2e`` ... ``desktop_e2e5`` say German)."""
    if ui_lang in LANG_NAMES and ui_lang != "de":
        state = state.replace("The user interface is in German.", f"The user interface is in {LANG_NAMES[ui_lang]}.")
    return state
