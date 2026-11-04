"""Desktop engine, generation 4: CAPABILITIES.
  Builds on ``desktop_e2e``, ``desktop_e2e2`` and ``desktop_e2e3`` (all left
byte-identical) and is PURE like them (no pyobjc, no subprocess, no
network): the guest, the planner and the picker are injected callables.

``desktop_e2e3``'s independently written tasks exposed capability gaps (planner
20, executor 7 of 28 failed runs). The generic pieces added here:

1. **read_content** (a new step type): the text an app shows, read through AX
   by the guest (``read_text``: the front document's text area, the note shown,
   the note list, the selected file's Quick Look text), bound to a PLAN
   VARIABLE ``{{name}}``. A plan that ENDS with read_content is CONTINUED: the
   planner is called again with the steps done and the text read (as data) and
   plans the rest. Picker candidates for Finder files carry the files' Quick
   Look text as content snippets.
2. **Append / insert / edit / replace** (``mode``, ``find`` on field_rw and
   doc_save): read-modify-write on the text the document shows NOW; the check
   verifies the saved bytes against ``compose(old, mode, value, find)``, so the
   old text is preserved (append: as the prefix; insert: every old line in
   order; edit: all but the one replaced part).
3. **open_file**: a file opens through Finder (its row, Ablage > Öffnen) or,
   when no Finder window lists it, through TextEdit's own Open panel (AX in the
   panel our press opened; verified by the front document).
4. **navigate_into**: Finder subfolders (list and column view) by name; the
   guest verifies the move twice (the window's title and the listed names =
   the folder's entries on disk).
5. **Cross-app data flow**: ``{{name}}`` in a later step's value is replaced by
   the bound text (no clipboard).
6. **Location verification**: before object_create or a write, the container
   or document shown must be the one the plan NAMES (the step's scope or the
   last navigate target, by the map's label rule; the file the run opened);
   otherwise the step is not taken and the run stops as a REFUSAL.
7. **Planner w4** (``desktop_e2e4_planner.json``): the capability statements,
   the app routing rules, read_content, mode/find, plan variables; ``desktop_e2e2``'s
   safety rules (gate, refuse) unchanged.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import desktop_e2e as X
from . import desktop_e2e2 as Y
from . import desktop_e2e3 as Z

HERE = os.path.dirname(os.path.abspath(__file__))
PLANNER4_PATH = os.path.join(HERE, "desktop_e2e4_planner.json")
TASKS4_PATH = os.path.join(HERE, "desktop_e2e4_tasks.json")
GEN4 = X.GEN + "/e4"

#: worst case of one w4 planner call (a longer prompt; a continuation carries the text read)
X.WORST.setdefault("planner4", (6500 * 0.25 + 2500 * 0.69) / 1e6)
MODES = ("replace", "append", "insert", "edit")
MAX_CONTINUATIONS = 2
MAX_PREVIEWS = 12
READ_CHARS = 4000             # text shown to the planner per variable

# ---------------------------------------------------------------------------
# 1. The planner, wording w4
# ---------------------------------------------------------------------------


def load_planner4_spec(path: str = PLANNER4_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def planner4_messages(goal: str, spec: Optional[Dict[str, Any]] = None,
                      history: Optional[List[Dict[str, str]]] = None,
                      context: Optional[str] = None) -> List[Dict[str, str]]:
    """w4's messages; ``context`` = what is on the screen now (read from the
    screen, one line of data), appended to the request when given."""
    spec = spec or load_planner4_spec()
    user = spec["user_prefix"] + goal + ((spec["context_prefix"] + context) if context else "")
    return ([{"role": "system", "content": spec["w4_system"]}, {"role": "user", "content": user}]
            + list(history or []))


def screen_line(sc: Dict[str, Any]) -> str:
    """One line of what the guest's ``screen`` op read (names only)."""
    parts = [f"in front: {sc.get('front')}"]
    if sc.get("finder_window"):
        parts.append(f"Finder window shows the folder {sc['finder_window']!r}")
    if sc.get("notes_folder") is not None or "notes_note" in sc:
        parts.append(f"Notes shows the folder {sc.get('notes_folder')!r}"
                     + (f" with the note {sc['notes_note']!r} open" if sc.get("notes_note") else ""))
    if sc.get("textedit_documents") is not None:
        docs = sc["textedit_documents"]
        parts.append("TextEdit has " + (", ".join(repr(d) for d in docs) + " open" if docs else "no document open"))
    if sc.get("lexikon_running"):
        parts.append("Dictionary (Lexikon) is open" + (f" on the source {sc['lexikon_source']!r}"
                                                      if sc.get("lexikon_source") else ""))
    return "; ".join(parts) + "."


def parse_plan4(text: str, spec: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """``desktop_e2e2``'s parser with ``mode`` and ``find`` (absent = ""; a write's
    empty mode = replace)."""
    spec = spec or load_planner4_spec()
    types = [t["name"] for t in spec["types"]]
    apps = list(spec["apps"]) + ["none"]
    t = (text or "").strip()
    obj = None
    try:
        obj = json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            try:
                obj = json.loads(t[a:b + 1])
            except ValueError:
                pass
    if not isinstance(obj, dict) or not isinstance(obj.get("steps"), list):
        return [], "no JSON object with a 'steps' list"
    plan = []
    for s in obj["steps"]:
        if not isinstance(s, dict):
            return [], "step is not an object"
        if s.get("effect") not in types:
            return [], f"effect {s.get('effect')!r} not in the vocabulary"
        if s.get("app") not in apps:
            return [], f"app {s.get('app')!r} not allowed"
        mode = str(s.get("mode") or "")
        if mode and mode not in MODES:
            return [], f"mode {mode!r} not in {MODES}"
        if s["effect"] in ("field_rw", "doc_save") and not mode:
            mode = "replace"
        plan.append({"effect": s["effect"], "app": s["app"], "target": str(s.get("target") or ""),
                     "value": str(s.get("value") or ""), "scope": str(s.get("scope") or ""),
                     "mode": mode if s["effect"] in ("field_rw", "doc_save") else "",
                     "find": str(s.get("find") or "")})
    return plan, None


def step_line(s: Dict[str, Any], resolved: Any = None) -> str:
    args = [f"target={s.get('target')!r}"] + ([f"value={s.get('value')!r}"] if s.get("value") else []) + \
        ([f"mode={s.get('mode')}"] if s.get("mode") else []) + ([f"find={s.get('find')!r}"] if s.get("find") else [])
    out = f"{s['effect']}({s['app']}: {', '.join(args)})"
    lab = (resolved or {}).get("label") if isinstance(resolved, dict) else None
    return out + (f" -> {lab!r}" if lab else "")


def continuation_messages(previous_text: str, done: Sequence[Tuple[Dict[str, Any], Any]], reads: Dict[str, str],
                          spec: Optional[Dict[str, Any]] = None,
                          pending: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, str]]:
    """The second planner call after a plan that ended with read_content: the
    steps done (with what they resolved to), the steps the plan had after the
    read (not executed), and the texts read, as DATA."""
    spec = spec or load_planner4_spec()
    done_s = "\n".join(f"{i}. {step_line(s, r)}" for i, (s, r) in enumerate(done, 1)) or "(none)"
    pend_s = "\n".join(f"- {step_line(s)}" for s in pending) or "(none)"
    reads_s = "\n".join(f"{{{{{k}}}}} =\n<<<\n{v[:READ_CHARS]}\n>>>" for k, v in reads.items()) or "(none)"
    return [{"role": "assistant", "content": previous_text or ""},
            {"role": "user", "content": spec["continuation_template"].format(done=done_s, pending=pend_s,
                                                                             reads=reads_s)}]


def refused_unsupported(steps: Sequence[Dict[str, Any]]) -> bool:
    """A whole-plan refusal for "unsupported" (not outside the apps, not credentials)."""
    return len(steps) == 1 and steps[0]["effect"] == "refuse" and X.norm(steps[0]["value"]) == "unsupported"


def refusal_feedback(previous_text: str, spec: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    spec = spec or load_planner4_spec()
    return [{"role": "assistant", "content": previous_text or ""},
            {"role": "user", "content": spec["refusal_feedback_template"]}]


# ---------------------------------------------------------------------------
# 2. Plan variables (cross-app data flow; no clipboard)
# ---------------------------------------------------------------------------

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\-äöüÄÖÜß]+)\s*\}\}")


def var_name(value: Any) -> str:
    m = VAR_RE.search(str(value or ""))
    name = m.group(1) if m else str(value or "").strip()
    name = re.sub(r"[^A-Za-z0-9_\-äöüÄÖÜß]+", "_", name).strip("_")
    return name or "text"


def uses_var(step: Dict[str, Any], name: str) -> bool:
    return any(v == name for f in ("value", "find", "target") for v in VAR_RE.findall(str(step.get(f) or "")))


def copy_use(step: Dict[str, Any], name: str) -> bool:
    """Is this a COPY of the whole text (the variable in a write's value only)?"""
    in_value = name in VAR_RE.findall(str(step.get("value") or ""))
    elsewhere = any(name in VAR_RE.findall(str(step.get(f) or "")) for f in ("target", "find", "scope"))
    return step.get("effect") in ("field_rw", "doc_save") and in_value and not elsewhere


def substitute(step: Dict[str, Any], variables: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    """The step with every ``{{name}}`` in value / find / target replaced by the
    bound text; the names that are not bound."""
    missing: List[str] = []
    out = dict(step)
    for f in ("value", "find", "target"):
        v = str(step.get(f) or "")
        if "{{" not in v:
            continue

        def rep(m):
            k = m.group(1)
            if k not in variables:
                missing.append(k)
                return m.group(0)
            return variables[k]
        out[f] = VAR_RE.sub(rep, v)
    return out, missing


# ---------------------------------------------------------------------------
# 3. Text modes: read-modify-write, and the check that keeps the old text
# ---------------------------------------------------------------------------

def compose(old: str, mode: str, value: str, find: str = "") -> Tuple[Optional[str], Optional[str]]:
    """(new text, error). replace: value; append: value as a new last line
    (a trailing newline of the old text is the separator); insert: value as a
    new line after the ONE line containing ``find`` ("" = the first line);
    edit: the ONE occurrence of ``find`` replaced by value."""
    old, value, find = old or "", value or "", find or ""
    mode = mode or "replace"
    if mode == "replace":
        return value, None
    if mode == "append":
        if not value:
            return None, "append of an empty text"
        if not old:
            return value, None
        return (old + value) if old.endswith("\n") else (old + "\n" + value), None
    if mode == "insert":
        if not value:
            return None, "insert of an empty text"
        if not find:
            return (value + ("\n" + old if old else "")), None
        lines = old.split("\n")
        hit = [i for i, ln in enumerate(lines) if find in ln]
        if len(hit) != 1:
            return None, f"{len(hit)} lines contain {find!r} (insert needs exactly one)"
        lines.insert(hit[0] + 1, value)
        return "\n".join(lines), None
    if mode == "edit":
        if not find:
            return None, "edit without the old words (find)"
        n = old.count(find)
        if n != 1:
            return None, f"{find!r} occurs {n} times in the text (edit needs exactly one)"
        return old.replace(find, value, 1), None
    return None, f"unknown mode {mode!r}"


def check_text(old: str, new: str, mode: str, value: str, find: str = "") -> Optional[str]:
    """None when ``new`` (the saved bytes / the field's value) is exactly what
    the mode makes of ``old`` (a trailing newline aside), else why. The mode's
    own invariant is named: the old text preserved plus the addition."""
    want, err = compose(old, mode, value, find)
    if err:
        return err
    got = (new or "").rstrip("\n")
    if got == (want or "").rstrip("\n"):
        return None
    if mode == "append" and not got.startswith((old or "").rstrip("\n")):
        return f"the old text is not preserved (append): {got[:80]!r}"
    if mode == "insert" and [ln for ln in got.split("\n") if ln != value] != (old or "").rstrip("\n").split("\n"):
        return f"the old lines are not preserved (insert): {got[:80]!r}"
    if mode == "edit":
        return f"not the one edit of {find!r} -> {value!r}: {got[:80]!r}"
    return f"text reads {got[:80]!r}, want {(want or '')[:80]!r}"


# ---------------------------------------------------------------------------
# 4. The plan check: desktop_e2e3's identity check plus two rules
# ---------------------------------------------------------------------------

def refine_plan4(goal: str, steps: Sequence[Dict[str, Any]], emap) -> Y.Refined:
    """(r1) a field_rw right before a doc_save of the same app that writes the
    same text (or saves "") becomes that doc_save, keeping its mode and find;
    (r2) a read_content whose variable no later step uses ENDS the plan (the
    engine reads and asks for the rest; steps planned after it on values the
    planner had not seen are dropped); then ``desktop_e2e3``'s check (identities,
    restrictions, minimal plans)."""
    st = [dict(s, mode=s.get("mode") or "", find=s.get("find") or "", scope=s.get("scope") or "") for s in steps]
    changes: List[str] = []
    pending: List[Dict[str, Any]] = []
    i = 0
    while i + 1 < len(st):
        s, n = st[i], st[i + 1]
        if s["effect"] == "field_rw" and n["effect"] == "doc_save" and s["app"] == n["app"] \
                and X.norm(s["value"]) and (not X.norm(n["value"]) or X.norm(n["value"]) == X.norm(s["value"])) \
                and (n.get("mode") or "replace") == "replace":
            n.update(value=s["value"], mode=s.get("mode") or "replace", find=s.get("find") or "")
            changes.append(f"dropped field_rw({s['value'][:40]!r}): the doc_save writes it ({n['mode']})")
            del st[i]
            continue
        i += 1
    # (r4) a search whose pattern IS the item the next select/open picks (the whole
    # description "alte Packliste für die Hüttentour" typed into Notes' search, which matched no
    # note) is dropped: the picker reads the whole list; a search for another word stays
    i = 0
    while i + 1 < len(st):
        s, n = st[i], st[i + 1]
        if s["effect"] == "scoped_find" and n["effect"] in ("select_item", "open_item") and s["app"] == n["app"] \
                and s["app"] in Y.CONTENT_APPS and X.tokens(s["value"]) \
                and X.tokens(s["value"]) == X.tokens(n["target"]):
            changes.append(f"dropped scoped_find({s['value'][:40]!r}): it only repeats the item the next "
                           f"{n['effect']} picks (the picker reads the whole list)")
            del st[i]
            continue
        i += 1
    for i, s in enumerate(st):
        if s["effect"] == "read_content" and i + 1 < len(st):
            name = var_name(s["value"])
            later = [t for t in st[i + 1:] if uses_var(t, name)]
            if not later:
                changes.append(f"ended the plan after read_content({name!r}): no later step uses {{{{{name}}}}} "
                               f"(dropped {len(st) - i - 1} step(s) planned before the text was seen)")
                pending = st[i + 1:]
                st = st[:i + 1]
                break
            # a variable holds the WHOLE text read: it may be copied into a text (a write's
            # value), never stand for a word, a name or a place (the whole note
            # typed into Lexikon's search field): the planner picks those after seeing it
            bad = [t for t in later if not copy_use(t, name)]
            if bad:
                changes.append(f"ended the plan after read_content({name!r}): {{{{{name}}}}} is the whole text "
                               f"read, not a word or name ({bad[0]['effect']}); the rest is planned after reading")
                pending = st[i + 1:]
                st = st[:i + 1]
                break
    # (r5) the scope of a write in a document app names the DOCUMENT ("nur im Entwurf"): the
    # location check verifies it at the write (desktop_e2e2's rule refused such a plan)
    for s in st:
        if s["effect"] in ("doc_save", "field_rw") and s["app"] not in Y.NAVIGABLE_APPS and s.get("scope"):
            s["document"] = s["scope"]
            s["scope"] = ""
            changes.append(f"scope({s['document']!r}) of {s['effect']} names the document: checked at the write")
    # (r3) the scope of an open_item in a document app names the folder the file lives in:
    # both open routes use it (Finder: go into it; the Open panel: enter it), so desktop_e2e3's
    # check, which knows no locations in TextEdit, does not see (and drop) it
    kept = {}
    for k, s in enumerate(st):
        if s["effect"] == "open_item" and s["app"] not in Y.CONTENT_APPS and s.get("scope"):
            kept[id(s)] = s["scope"]
            s["scope"] = ""
    ref = Z.refine_plan3(goal, st, emap)
    for s in ref.steps:
        s.setdefault("mode", "")
        s.setdefault("find", "")
    for s, orig in zip([t for t in ref.steps if t["effect"] == "open_item" and t["app"] not in Y.CONTENT_APPS],
                       [t for t in st if t["effect"] == "open_item" and t["app"] not in Y.CONTENT_APPS]):
        if id(orig) in kept:
            s["scope"] = kept[id(orig)]
            changes.append(f"kept scope({kept[id(orig)]!r}) of open_item in {s['app']}: the folder the file lives in")
    ref.changes = changes + ref.changes
    ref.pending = pending                  # shown to the planner when it continues after the read
    return ref


def route_by_screen(ref: Y.Refined, screen: Optional[Dict[str, Any]]) -> None:
    """With NO Finder window on the screen and a TextEdit document open, a file
    cannot be reached through Finder: "navigate Finder X; open_item Finder Y" (a
    launch_app Finder before them dropped) becomes "open_item TextEdit Y, scope
    X" (TextEdit's Open panel, which starts in the open document's folder)."""
    if not screen or screen.get("finder_window") or not screen.get("textedit_documents"):
        return
    st, out, i = ref.steps, [], 0
    while i < len(st):
        s = st[i]
        nxt = st[i + 1] if i + 1 < len(st) else None
        if s["effect"] == "launch_app" and s["app"] == "Finder" and nxt is not None and nxt["app"] == "Finder" \
                and nxt["effect"] in ("navigate", "open_item"):
            ref.changes.append("dropped launch_app(Finder): no Finder window on the screen (the file opens in TextEdit)")
            i += 1
            continue
        if s["effect"] == "navigate" and s["app"] == "Finder" and nxt is not None and nxt["app"] == "Finder" \
                and nxt["effect"] == "open_item":
            out.append(dict(nxt, app="TextEdit", scope=s["target"] or nxt.get("scope") or ""))
            ref.changes.append(f"navigate(Finder {s['target']!r}) + open_item(Finder {nxt['target']!r}) -> "
                               "open_item(TextEdit) through its Open panel: no Finder window on the screen")
            i += 2
            continue
        if s["effect"] == "open_item" and s["app"] == "Finder":
            out.append(dict(s, app="TextEdit"))
            ref.changes.append(f"open_item(Finder {s['target']!r}) -> open_item(TextEdit): no Finder window on the screen")
            i += 1
            continue
        out.append(s)
        i += 1
    ref.steps[:] = out


# ---------------------------------------------------------------------------
# 5. Location verification (the container / document the plan names)
# ---------------------------------------------------------------------------

def names_location(named: str, shown: str) -> bool:
    """Does the plan's name for a place name the container shown? The map's
    label rule on the place's words without kind and stop words, either way
    round ("Herbstfest-Ordner" names "Herbstfest"); an empty name names none."""
    nb, sb = Z.bare_tokens(named), X.tokens(shown)
    if not nb or not shown:
        return False
    if X.norm(named) == X.norm(shown) or set(nb) <= set(sb):
        return True
    return bool(Z.bare_tokens(shown)) and set(Z.bare_tokens(shown)) <= set(nb)


FILE_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


class EffectMap4(Y.EffectMap2):
    """``desktop_e2e2``'s map (same data, desktop_e2e2_map.json). One more rule: a flag
    target that names a menu GROUP ("Rechtschreibung und Grammatik") and no single
    flag narrows the picker's candidates to that group's flags (task S18
    chose among all ten flags of the app)."""

    def resolve_flag(self, app: str, target: str) -> X.Resolution:
        res = super().resolve_flag(app, target)
        if res.by != "picker" or not target:
            return res
        groups = sorted({" > ".join(c["text"].split(" > ")[:-1]) for c in res.candidates})
        hit = X.label_match(target, [g.split(" > ")[-1] for g in groups])
        if len(hit) == 1:
            g = groups[hit[0]]
            narrowed = [c for c in res.candidates if c["text"].startswith(g + " > ")]
            if 1 <= len(narrowed) < len(res.candidates):
                return X.Resolution("picker", f"{res.why}; narrowed to the group {g!r} the target names",
                                    candidates=narrowed)
        return res


# ---------------------------------------------------------------------------
# 6. The architecture arm
# ---------------------------------------------------------------------------

class Arch4(Z.Arch3):
    """``desktop_e2e3``'s arm (identity map check, restriction feedback, the picker rule,
    one re-plan after a failed step, the compiled replay) with these
    capabilities: read_content + continuation, plan variables, write modes,
    open_file routes, navigate into subfolders, location verification."""

    def __init__(self, guest, emap, planner=None, **kw):
        super().__init__(guest, emap, planner=planner, **kw)
        self._vars: Dict[str, str] = {}
        self._where: Dict[str, str] = {}
        self._opened: Optional[str] = None
        self._pending: List[Dict[str, Any]] = []
        self._screen: Optional[str] = None
        self._screen_raw: Optional[Dict[str, Any]] = None

    # -- the planner -----------------------------------------------------------
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
        steps, err = parse_plan4(text)
        entry = {"text": (text or "")[:4000], "steps": steps, "error": err or (meta or {}).get("error"),
                 "usage": (meta or {}).get("usage"), "feedback": bool(history), "screen": screen}
        rec.plan.append(entry)
        return text, steps, err, entry

    def _checked(self, rec, goal, text, steps, err, entry, done=()):
        """Restriction feedback (``desktop_e2e2``) on the steps done + planned, then the
        plan check (``refine_plan4``)."""
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
            return [], err
        ref = refine_plan4(goal, steps, self.m)
        route_by_screen(ref, getattr(self, "_screen_raw", None))
        entry["refined"] = ref.to_dict()
        entry["pending"] = ref.pending
        if ref.problems:
            return [], "; ".join(ref.problems)
        self._pending = ref.pending
        return ref.steps, None

    def plan(self, rec: X.Rec, goal: str):
        if self.planner is None:
            raise X.GuestError("model needed: no planner in compiled mode")
        sc = self.g("screen")
        ok = isinstance(sc, dict) and sc.get("status") == "done" and isinstance(sc.get("screen"), dict)
        self._screen_raw = sc["screen"] if ok else None
        self._screen = screen_line(sc["screen"]) if ok else None
        text, steps, err, entry = self._call_planner(rec, goal, screen=self._screen)
        if not err and refused_unsupported(steps):
            # a doable edit refused as "unsupported" is asked once more with the capability
            # statement; the second answer is final (a refusal stays a refusal)
            hist = refusal_feedback(text)
            text, steps, err, entry = self._call_planner(rec, goal, hist, self._screen)
            entry["history"] = hist
            entry["refusal_feedback"] = True
        return self._checked(rec, goal, text, steps, err, entry)

    def continue_plan(self, rec: X.Rec, goal: str, done: List[Tuple[Dict[str, Any], Any]]):
        if self.planner is None:
            raise X.GuestError("model needed: no planner in compiled mode")
        prev = next((p.get("text") for p in reversed(rec.plan) if p.get("text")), "")
        hist = continuation_messages(prev, done, self._vars, pending=self._pending)
        text, steps, err, entry = self._call_planner(rec, goal, hist)
        entry["continuation"] = True
        entry["history"] = hist
        return self._checked(rec, goal, text, steps, err, entry, done=[s for s, _r in done])

    # -- one run ---------------------------------------------------------------
    def run(self, task: X.Task, rec: X.Rec, compiled: Optional[List[Dict[str, Any]]] = None) -> X.Rec:
        clock = X.Clock(self.clock_fn)
        self._vars, self._where, self._opened, self._pending, self._screen = {}, {}, None, [], None
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
            concrete, missing = substitute(step, self._vars)
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
            if outcome in ("refuse", "gate", "location") and compiled is None:
                rec.compiled.append({"step": step, "resolved": {"op": outcome}})
            if outcome in ("refuse", "gate", "handoff", "prompt", "gated", "location"):
                rec.stopped = {"refuse": "refused (plan)", "gate": "human gate (declined)",
                               "handoff": "picker hand-off (declined)", "prompt": "auth prompt seen",
                               "gated": "irreversible-action guard (declined)",
                               "location": "refused (location check)"}[outcome]
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

    def _continue(self, rec, task, plan, done_steps):
        """(plan, index, stop reason): the plan ended with read_content; the
        planner plans the rest with the text read (at most MAX_CONTINUATIONS)."""
        if rec.continuations >= MAX_CONTINUATIONS:
            return plan, len(plan), ""
        rec.continuations += 1
        rest, err = self.continue_plan(rec, task.text, done_steps)
        if err:
            return plan, len(plan), f"continuation gave no plan: {err}"
        return plan + rest, len(plan), ""

    # -- read_content --------------------------------------------------------------
    def _read_content(self, task, rec, step, log, bid, fixed):
        name = var_name(step["value"])
        r = self.call("read_text", bundle=bid, what=step["target"])
        if not r.get("ok"):
            log["why"] = f"read: {r.get('detail') or 'no text'}"
            return "failed"
        text = r.get("text") or ""
        if bid in ("com.apple.TextEdit", "com.apple.Notes"):
            r2 = self.call("read_text", bundle=bid, what=step["target"])
            if (r2.get("text") or "") != text:
                log["why"] = "the text changed between two reads"
                return "failed"
        self._vars[name] = text
        log["resolved"] = {"op": "read_content", "var": name, "source": r.get("source")}
        log["read"] = {"var": name, "chars": len(text), "text": text[:2000], "source": r.get("source")}
        log["check"] = "read twice, the same text" if bid in ("com.apple.TextEdit", "com.apple.Notes") else "read once"
        rec.reads.append({"var": name, "source": r.get("source"), "chars": len(text)})
        return "ok"

    # -- navigate (Finder subfolders / the folder above; Notes folders) ---------------
    def _navigate(self, task, rec, step, log, bid, fixed):
        kind = {"com.apple.finder": "finder_locations", "com.apple.Notes": "notes_folders"}.get(bid)
        if kind is None:
            log["why"] = f"navigate is not defined for {X.APP_OF.get(bid)}"
            return "failed"
        if fixed is not None and fixed.get("op") == "location":
            return "location"
        rows = self.call("rows", bundle=bid, kind=kind)
        cands = [{"text": r["text"], "label": r["label"], "shown": r.get("shown"), "via": r.get("via")}
                 for r in rows.get("rows") or []]
        if fixed is not None:
            ch = next((c for c in cands if c["label"] == fixed["label"]
                       and (c.get("via") == fixed.get("via") or not fixed.get("via"))), None)
            log["resolution"] = {"by": "compiled"}
        else:
            labels = [c["label"] for c in cands]
            hit = X.label_match(step["target"], labels)
            why = "one location label named by the target"
            if len(hit) != 1:
                bare = " ".join(Z.bare_tokens(step["target"]))
                if bare and bare != X.norm(step["target"]):
                    h2 = X.label_match(bare, labels)
                    if len(h2) == 1:
                        hit, why = h2, f"one location label named by the target without kind words ({bare!r})"
            if len(hit) > 1:                   # the same name twice: the shown folder or a subfolder first
                pref = [i for i in hit if cands[i].get("via") in ("shown", "sub")]
                if len(pref) == 1:
                    hit, why = pref, "one location label named by the target (the folder shown or a subfolder)"
            if len(hit) == 1:
                ch = cands[hit[0]]
                log["resolution"] = {"by": "map", "why": why}
            else:
                log["resolution"] = {"by": "picker", "why": f"{len(hit)} location labels fit"}
                ch = self.pick(rec, task.text, X.STATES[kind].format(where=rows.get("where") or ""), cands, log)
                if ch is None:
                    return "handoff"
        if ch is None:
            log["why"] = "compiled location not present"
            return "failed"
        log["resolved"] = {"op": "navigate", "label": ch["label"], "via": ch.get("via")}
        if not ch.get("shown"):
            try:
                r = self._act(rec, log, "select", bundle=bid, kind=kind, label=ch["label"], via=ch.get("via"))
            except X._Prompt:
                return "prompt"
            if not X._ok(r):
                log["why"] = f"select {ch['label']!r}: {r.get('detail')}"
                return "failed"
        after = self.call("rows", bundle=bid, kind=kind)
        if after.get("where") != ch["label"]:
            log["why"] = f"shows {after.get('where')!r}, not {ch['label']!r}"
            return "failed"
        if bid == "com.apple.finder" and ch.get("via") in ("sub", "up") and \
                os.path.basename(after.get("path") or "") != ch["label"]:
            log["why"] = f"the container read back is {after.get('path')!r}"
            return "failed"
        log["check"] = "container verified: " + (f"{after.get('path')}" if after.get("path") else after["where"])
        self._where[X.APP_OF[bid]] = step["target"] or ch["label"]
        return "ok"

    # -- location verification ---------------------------------------------------------
    def _named_container(self, app: str, step: Dict[str, Any]) -> Optional[str]:
        return step.get("scope") or self._where.get(app)

    def _location_check(self, rec, step, log, bid) -> Optional[str]:
        """None when the container shown is the one the plan names (or the plan
        names none); else why (the step is not taken: a refusal)."""
        app = X.APP_OF[bid]
        named = self._named_container(app, step)
        if not named:
            log["location"] = "the plan names no container (nothing to verify)"
            return None
        if bid == "com.apple.finder":
            path = self.call("shown_folder", bundle=bid).get("path") or ""
            shown = os.path.basename(path)
        elif bid == "com.apple.Notes":
            shown = self.call("rows", bundle=bid, kind="notes_folders").get("where") or ""
        else:
            log["location"] = f"no container reader for {app}"
            return None
        if names_location(named, shown):
            log["location"] = f"container {shown!r} is the one the plan names ({named!r})"
            return None
        return f"the container shown is {shown!r}, the plan names {named!r}"

    def _document_check(self, rec, step, log, doc: Optional[str]) -> Optional[str]:
        """None when the front document is the one the plan names: the file this
        run opened, a file name in the target, the document a scope names."""
        want = self._opened
        if want is None and FILE_RE.search(step.get("target") or ""):
            want = step["target"]
        named = step.get("document") or ""
        name = os.path.basename(doc or "")
        if want and not (doc and X.norm(name) == X.norm(os.path.basename(want))):
            return f"the front document is {name or None!r}, the plan named {os.path.basename(want)!r}"
        if named and not names_location(named, os.path.splitext(name)[0]):
            return f"the front document is {name or None!r}, the plan names the document {named!r}"
        if not want and not named:
            log["location"] = "the plan names no document (the front document)"
        else:
            log["location"] = f"front document {name!r} is the one the plan named"
        return None

    def _object_create(self, task, rec, step, log, bid, fixed):
        if fixed is not None and fixed.get("op") == "location":
            return "location"
        why = self._location_check(rec, step, log, bid)
        if why:
            log["why"] = why
            return "location"
        return super()._object_create(task, rec, step, log, bid, fixed)

    # -- writes with modes (read-modify-write) --------------------------------------------
    def _doc_save(self, task, rec, step, log, bid, fixed):
        from anyuse.execution import desktop_contracts as C
        if fixed is not None and fixed.get("op") == "location":
            return "location"
        entry = self._resolve_single(task, rec, step, log, bid, "doc_save", fixed)
        if entry in (None, "handoff"):
            return "handoff" if entry == "handoff" else "failed"
        area = next((e for e in self.m.entries(X.APP_OF[bid], "field_rw") if e.get("role") == "AXTextArea"), None)
        if area is None:
            log["why"] = "no text area mapped for the document"
            return "failed"
        front = self.call("front")
        if front.get("front_bundle") != bid:
            # the write acts in its own app: bring it to the front (a continuation may not plan the launch)
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
            new, err = compose(old, mode, value, find)
            if err:
                log["why"] = f"{mode}: {err}"
                return "failed"
            write = True
        log["write"] = {"mode": mode, "old_chars": len(old), "new_chars": len(new)}
        try:
            if write:
                r = self._act(rec, log, "enter", bundle=bid, target={"role": area["role"],
                                                                      "identifier": area["identifier"],
                                                                      "document": doc}, text=new, commit=False)
                if r.get("status") == "gated":
                    return "gated"
                if not X._ok(r):
                    log["why"] = f"write: {r.get('detail')}"
                    return "failed"
            r = self._act(rec, log, "press", bundle=bid, trigger=entry["trigger"], label=entry.get("path"))
            if r.get("status") == "gated":
                return "gated"
            if not X._ok(r):
                log["why"] = f"save: {r.get('detail')}"
                return "failed"
        except X._Prompt:
            return "prompt"
        after = self.call("file", path=doc)
        s_obs = {"identity": {"bundle_id": bid, "document": doc}, "complete": True,
                 "target": {"text": before.get("text") or ""}, "sentinel": None}
        obs = {"identity": {"bundle_id": bid, "document": doc},
               "complete": bool(after.get("ok")) and after.get("mtime") != before.get("mtime"),
               "target": {"text": after.get("text") or ""}, "sentinel": None}
        why = C.DOC_SAVE.validate(obs) or C.DOC_SAVE.expect(s_obs, new.rstrip("\n"), 0, obs) \
            or (check_text(old, after.get("text") or "", mode, value, find) if write else None)
        log["check"] = why or (f"doc_save contract met (the bytes on disk; {mode}: the old text preserved)"
                               if mode != "replace" else "doc_save contract met (the bytes on disk)")
        if why:
            log["why"] = why
            return "failed"
        return "ok"

    def _field_rw(self, task, rec, step, log, bid, fixed):
        from anyuse.execution import desktop_contracts as C
        if fixed is not None and fixed.get("op") == "location":
            return "location"
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
        new, err = compose(old, mode, value, find)
        if err:
            log["why"] = f"{mode}: {err}"
            return "failed"
        try:
            r = self._act(rec, log, "enter", bundle=bid, target=tgt, text=new, commit=False)
        except X._Prompt:
            return "prompt"
        if r.get("status") == "gated":
            return "gated"
        if not X._ok(r):
            log["why"] = f"write: {r.get('detail')}"
            return "failed"
        got = self.call("field", bundle=bid, **tgt)
        obs = {"identity": {"bundle_id": bid}, "complete": bool(got.get("ok")), "target": got.get("value"),
               "sentinel": None}
        why = C.FIELD_RW.validate(obs) or C.FIELD_RW.expect({"identity": {"bundle_id": bid}}, new, 0, obs) \
            or check_text(old, got.get("value") or "", mode, value, find)
        log["check"] = why or f"field_rw contract met ({mode}: the old text preserved)"
        if why:
            log["why"] = why
            return "failed"
        return "ok"

    # -- content rows: Notes notes, Finder items ----------------------------------------
    def _notes_row(self, task, rec, step, log, fixed, open_it: bool):
        bid = "com.apple.Notes"
        # the candidates (a note open from the gallery hides the grid: the guest goes back for them);
        # the read-back after the select is a plain read
        rows = self.call("rows", bundle=bid, kind="notes_notes", back=True)
        cands = [{"text": r["text"], "label": r["label"]} for r in rows.get("rows") or []]
        if fixed is not None:
            ch = next((c for c in cands if c["label"] == fixed["label"]), None)
            log["resolution"] = {"by": "compiled"}
        else:
            ch = self._pick_file(rec, task, step, log, cands,
                                 X.STATES["notes_notes"].format(where=rows.get("where") or ""), previews=False)
            if ch is None:
                return "handoff"
        if ch is None:
            log["why"] = "compiled row not present"
            return "failed"
        log["resolved"] = {"op": "open" if open_it else "select", "label": ch["label"]}
        try:
            r = self._act(rec, log, "select", bundle=bid, kind="notes_notes", label=ch["label"])
        except X._Prompt:
            return "prompt"
        if not X._ok(r):
            log["why"] = f"select {ch['label']!r}: {r.get('detail')}"
            return "failed"
        sel = self.call("rows", bundle=bid, kind="notes_notes")
        lab = ch["label"]
        snip = lab.lstrip("…").strip().rstrip("…").strip()
        snippet = bool(snip) and snip in (sel.get("shown_body") or "")
        if sel.get("shown_note") != lab and not snippet:
            log["why"] = f"editor shows {sel.get('shown_note')!r}"
            return "failed"
        log["check"] = f"the note shown is {sel.get('shown_note')!r}" + (" (its search snippet)" if snippet else "")
        return "ok"

    def _select_item(self, task, rec, step, log, bid, fixed):
        if bid == "com.apple.Notes":
            return self._notes_row(task, rec, step, log, fixed, open_it=False)
        if bid != "com.apple.finder":
            log["why"] = f"content items are not defined for {X.APP_OF.get(bid)}"
            return "failed"
        rows = self.call("rows", bundle=bid, kind="finder_files")
        cands = [{"text": r["text"], "label": r["label"], "kind": r.get("kind")} for r in rows.get("rows") or []]
        if fixed is not None:
            ch = next((c for c in cands if c["label"] == fixed["label"]), None)
            log["resolution"] = {"by": "compiled"}
        else:
            ch = self._pick_file(rec, task, step, log, cands,
                                 X.STATES["finder_files"].format(where=rows.get("where") or ""), previews=True)
            if ch is None:
                return "handoff"
        if ch is None:
            log["why"] = "compiled row not present"
            return "failed"
        log["resolved"] = {"op": "select", "label": ch["label"]}
        try:
            r = self._act(rec, log, "select", bundle=bid, kind="finder_files", label=ch["label"])
        except X._Prompt:
            return "prompt"
        if not X._ok(r):
            log["why"] = f"select {ch['label']!r}: {r.get('detail')}"
            return "failed"
        sel = self.call("rows", bundle=bid, kind="finder_files")
        if [x["label"] for x in sel.get("rows") or [] if x.get("selected")] != [ch["label"]]:
            log["why"] = "selection read back differs"
            return "failed"
        log["check"] = f"the selection is {ch['label']!r}"
        return "ok"

    # -- open a file: Finder, or TextEdit's own Open panel -------------------------------
    def _open_item(self, task, rec, step, log, bid, fixed):
        if bid == "com.apple.Notes":
            return self._notes_row(task, rec, step, log, fixed, open_it=True)
        route = (fixed or {}).get("route")
        if route == "finder":
            return self._open_finder(task, rec, step, log, fixed)
        if route == "panel":
            return self._open_panel(task, rec, step, log, "com.apple.TextEdit", fixed)
        if bid == "com.apple.finder":
            probe = self.call("rows", bundle="com.apple.finder", kind="finder_files")
            if X._ok(probe) and not probe.get("rows"):
                front = self.call("screen").get("screen") or {}
                if front.get("textedit_documents"):
                    # no Finder window lists files, TextEdit has a document open: its Open panel
                    log["route"] = "TextEdit's Open panel (planned in Finder, no Finder window lists files)"
                    self.call("launch", bundle="com.apple.TextEdit")
                    return self._open_panel(task, rec, step, log, "com.apple.TextEdit", fixed)
            return self._open_finder(task, rec, step, log, fixed)
        if bid == "com.apple.TextEdit":
            probe = self.call("rows", bundle="com.apple.finder", kind="finder_files")
            if X._ok(probe) and any(r.get("kind") == "file" for r in probe.get("rows") or []):
                log["route"] = "Finder (a Finder window lists files)"
                return self._open_finder(task, rec, step, log, fixed)
            log["route"] = "TextEdit's Open panel (no Finder window lists files)"
            return self._open_panel(task, rec, step, log, bid, fixed)
        log["why"] = f"open_item is not defined for {X.APP_OF.get(bid)}"
        return "failed"

    def _pick_file(self, rec, task, step, log, cands, state, previews: bool):
        """A content row: the map when the target names exactly one row by the
        label rule, else the picker, with content snippets (Quick Look) for
        Finder files."""
        hit = X.label_match(step["target"], [c["label"] for c in cands])
        if len(hit) == 1:
            log["resolution"] = {"by": "map", "why": "one row label named by the target"}
            return cands[hit[0]]
        log["resolution"] = {"by": "picker", "why": f"{len(hit)} row labels fit the target"}
        if previews:
            files = [c["label"] for c in cands if c.get("kind") == "file"][:MAX_PREVIEWS]
            if files:
                pv = self.call("previews", labels=files)
                snip = pv.get("previews") or {}
                log["previews"] = {k: (v or "")[:120] for k, v in snip.items()}
                cands = [dict(c, text=c["text"] + (f" | content: {' '.join((snip.get(c['label']) or '').split())[:160]}"
                                                   if snip.get(c["label"]) else "")) for c in cands]
        return self.pick(rec, task.text, state, cands, log)

    def _open_finder(self, task, rec, step, log, fixed):
        bid = "com.apple.finder"
        rows = self.call("rows", bundle=bid, kind="finder_files")
        named = step.get("scope") if step.get("app") != "Finder" else ""
        if fixed is None and named and not names_location(named, rows.get("where") or ""):
            sub = [r["label"] for r in rows.get("rows") or [] if r.get("kind") == "folder"
                   and names_location(named, r["label"])]
            if len(sub) == 1:
                try:
                    r = self._act(rec, log, "select", bundle=bid, kind="finder_locations", label=sub[0], via="sub")
                except X._Prompt:
                    return "prompt"
                if not X._ok(r):
                    log["why"] = f"enter {sub[0]!r}: {r.get('detail')}"
                    return "failed"
                log["entered"] = sub[0]
                rows = self.call("rows", bundle=bid, kind="finder_files")
        elif fixed is not None and fixed.get("enter"):
            for lab in fixed["enter"]:
                r = self._act(rec, log, "select", bundle=bid, kind="finder_locations", label=lab, via="sub")
                if not X._ok(r):
                    log["why"] = f"enter {lab!r}: {r.get('detail')}"
                    return "failed"
            rows = self.call("rows", bundle=bid, kind="finder_files")
        cands = [{"text": r["text"], "label": r["label"], "kind": r.get("kind")} for r in rows.get("rows") or []]
        if fixed is not None:
            ch = next((c for c in cands if c["label"] == fixed["label"]), None)
            log["resolution"] = {"by": "compiled"}
        else:
            ch = self._pick_file(rec, task, step, log, cands,
                                 X.STATES["finder_files"].format(where=rows.get("where") or ""), previews=True)
            if ch is None:
                return "handoff"
        if ch is None:
            log["why"] = "compiled row not present"
            return "failed"
        if ch.get("kind") == "folder":
            log["resolved"] = {"op": "open", "route": "finder", "label": ch["label"], "folder": True}
            try:
                r = self._act(rec, log, "select", bundle=bid, kind="finder_locations", label=ch["label"], via="sub")
            except X._Prompt:
                return "prompt"
            after = self.call("rows", bundle=bid, kind="finder_locations")
            if X._ok(r) and after.get("where") == ch["label"]:
                self._where["Finder"] = ch["label"]
                log["check"] = f"went into the folder {ch['label']!r} (container verified)"
                return "ok"
            log["why"] = f"enter {ch['label']!r}: {r.get('detail')}"
            return "failed"
        log["resolved"] = {"op": "open", "route": "finder", "label": ch["label"],
                           **({"enter": [log["entered"]]} if log.get("entered") else {})}
        try:
            r = self._act(rec, log, "select", bundle=bid, kind="finder_files", label=ch["label"])
        except X._Prompt:
            return "prompt"
        if not X._ok(r):
            log["why"] = f"select {ch['label']!r}: {r.get('detail')}"
            return "failed"
        trig = self.m.open_trigger("Finder")
        try:
            r = self._act(rec, log, "press", bundle=bid, trigger=trig["trigger"], label=trig["path"])
        except X._Prompt:
            return "prompt"
        if r.get("status") == "gated":
            return "gated"
        if not X._ok(r):
            log["why"] = f"open: {r.get('detail')}"
            return "failed"
        return self._await_document(log, ch["label"])

    def _await_document(self, log, label):
        doc = ""
        for _ in range(10):
            w = self.call("front")
            doc = w.get("document") or ""
            if os.path.basename(doc) == label:
                self._opened = doc
                log["check"] = f"front document {label!r}"
                return "ok"
            self.sleep(1.0)
        log["why"] = f"front document is {os.path.basename(doc)!r}, not {label!r}"
        return "failed"

    def _open_panel(self, task, rec, step, log, bid, fixed):
        trig = self.m.open_trigger(X.APP_OF[bid])
        if not trig:
            log["why"] = "no open trigger"
            return "failed"
        try:
            st = self._act(rec, log, "panel", action="show", trigger=trig["trigger"], label=trig["path"])
        except X._Prompt:
            return "prompt"
        if not X._ok(st):
            log["why"] = f"panel: {st.get('detail')}"
            return "failed"
        named = step.get("scope") or self._where.get("Finder")
        entered, goto = [], False
        if fixed is None and (st.get("path") is None or (named and not names_location(named, st.get("where") or "")
                                                          and not any(names_location(named, r["label"])
                                                                      for r in st.get("rows") or []))):
            # the panel lists a folder outside this test's data, or not the named one: move it to
            # the folder the Finder window shows (the user's current place), then look again
            g = self._act(rec, log, "panel", action="goto_finder")
            log["panel_goto"] = {"status": g.get("status"), "detail": g.get("detail")}
            if X._ok(g):
                goto = True
                st = self._act(rec, log, "panel", action="show", trigger=trig["trigger"], label=trig["path"])
        if fixed is not None:
            if fixed.get("goto"):
                g = self._act(rec, log, "panel", action="goto_finder")
                if not X._ok(g):
                    log["why"] = f"panel goto: {g.get('detail')}"
                    self._act(rec, log, "panel", action="cancel")
                    return "failed"
                goto = True
            for lab in fixed.get("enter") or []:
                st = self._act(rec, log, "panel", action="enter", label=lab)
                if not X._ok(st):
                    log["why"] = f"panel enter {lab!r}: {st.get('detail')}"
                    self._act(rec, log, "panel", action="cancel")
                    return "failed"
                entered.append(lab)
            st = self._act(rec, log, "panel", action="show", trigger=trig["trigger"], label=trig["path"])
        elif named and not names_location(named, st.get("where") or ""):
            sub = [r["label"] for r in st.get("rows") or [] if r.get("kind") == "folder"
                   and names_location(named, r["label"])]
            if len(sub) != 1:
                self._act(rec, log, "panel", action="cancel")
                log["why"] = f"the panel lists {st.get('where')!r}; {named!r} is not reachable from it"
                return "failed"
            st = self._act(rec, log, "panel", action="enter", label=sub[0])
            if not X._ok(st):
                self._act(rec, log, "panel", action="cancel")
                log["why"] = f"panel enter {sub[0]!r}: {st.get('detail')}"
                return "failed"
            entered.append(sub[0])
            st = self._act(rec, log, "panel", action="show", trigger=trig["trigger"], label=trig["path"])
        if st.get("path") is None:
            self._act(rec, log, "panel", action="cancel")
            log["why"] = "the panel's folder is not one of this test's folders (not verified)"
            return "failed"
        cands = [{"text": r["text"], "label": r["label"], "kind": r.get("kind")} for r in st.get("rows") or []
                 if r.get("kind") == "file"]
        if fixed is not None:
            ch = next((c for c in cands if c["label"] == fixed["label"]), None)
            log["resolution"] = {"by": "compiled"}
        else:
            state = (f"Frontmost application: TextEdit. Its Open panel (Ablage > Öffnen …) lists the files of the "
                     f"folder {st.get('where') or ''}. The listed items are file names. The user interface is in German.")
            ch = self._pick_file(rec, task, step, log, cands, state, previews=False)
            if ch is None:
                self._act(rec, log, "panel", action="cancel")
                return "handoff"
        if ch is None:
            self._act(rec, log, "panel", action="cancel")
            log["why"] = "compiled file not listed in the panel"
            return "failed"
        log["resolved"] = {"op": "open", "route": "panel", "label": ch["label"], "enter": entered,
                           **({"goto": True} if goto else {})}
        try:
            r = self._act(rec, log, "panel", action="open", label=ch["label"])
        except X._Prompt:
            return "prompt"
        if not X._ok(r):
            self._act(rec, log, "panel", action="cancel")
            log["why"] = f"panel open {ch['label']!r}: {r.get('detail')}"
            return "failed"
        return self._await_document(log, ch["label"])


# ---------------------------------------------------------------------------
# 7. The tasks (new independent pool) and one run
# ---------------------------------------------------------------------------

#: harness development tasks for routes the seen tasks never reach (never measured,
#: never counted; not from any pool): TextEdit's Open panel with no Finder window,
#: navigate into a subfolder in column view and in list view
_D = GEN4 + "/_dev"
DEV4_TASKS: List[X.Task] = [
    X.Task("P1", "Öffne die Datei Anhang aus dem Unterordner Unterlagen.", "single-app",
           {"app": "com.apple.TextEdit", "textedit": {"doc": _D + "/P1/Brief.txt", "seed": "Brief an Anna\n",
                                                      "sentinel": _D + "/P1/Unterlagen/Anhang.txt",
                                                      "sentinel_text": "Anhang: Liste\n", "flags": {}},
            "quit": ["com.apple.TextEdit"]},
           [{"kind": "front", "bundles": ["com.apple.TextEdit"]}, {"kind": "folder", "path": _D + "/P1/Unterlagen"}],
           {"opened": {"bundle": "com.apple.TextEdit", "path": _D + "/P1/Unterlagen/Anhang.txt"}}, dev=True),
] + [
    X.Task(tid, "Leg im Unterordner Belege einen neuen Ordner „Q3“ an.", "single-app",
           {"app": "com.apple.finder",
            "finder": {"folder": f"{_D}/{tid}/Projekt", "view": view, "sort": "Name",
                       "files": [{"name": "Info.txt", "modified": "2026-09-01 10:00", "text": "Info"}],
                       "subfolders": [{"name": "Belege", "files": [{"name": "Beleg1.txt", "modified": "2026-09-02 10:00",
                                                                    "text": "Beleg 1"}]}]},
            "quit": ["com.apple.TextEdit"]},
           [{"kind": "folder", "path": f"{_D}/{tid}/Projekt"}, {"kind": "folder", "path": f"{_D}/{tid}/Projekt/Belege"}],
           {"folder_added": {f"{_D}/{tid}/Projekt/Belege": [["Q3", "dir"]]}}, dev=True)
    for tid, view in (("C1", "Als Spalten"), ("L1", "Als Liste"))] + [
    X.Task("N1", "Öffne die Notiz mit der zweiten Zeile.", "single-app",
           {"app": "com.apple.Notes", "notes": {"folder": "AnyUse-R4-Galerie", "rest": "Alpha Notiz",
                                                "view": "Als Galerie", "clear_search": True},
            "quit": ["com.apple.Notes"]},
           [{"kind": "notes", "folders": ["AnyUse-R4-Galerie"]}, {"kind": "marks", "bundle": "com.apple.Notes",
                                                                   "top": "Darstellung"}],
           {"notes_shown": {"folder": "AnyUse-R4-Galerie", "note": "Beta Notiz"}}, dev=True)]
DEV4_TASKS.append(X.Task(
    "N2", "Open the note in which I wrote 'Dritte Zeile C'.", "single-app", DEV4_TASKS[-1].start, DEV4_TASKS[-1].read,
    {"notes_shown": {"folder": "AnyUse-R4-Galerie", "note": "Gamma Notiz"}}, dev=True))
#: the harness gallery task's own Notes folder (this module's development object)
DEV4_NOTES = {"AnyUse-R4-Galerie": [["Alpha Notiz", "Erste Zeile A"], ["Beta Notiz", "Zweite Zeile B"],
                                    ["Gamma Notiz", "Dritte Zeile C"]]}


def load_tasks4(path: str = TASKS4_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _folder4(tid: str, a: Dict[str, Any], gen: str) -> Optional[str]:
    ff = a["start"].get("finder_folder")
    return f"{gen}/{tid}/{ff['name']}" if ff else None


def build_start4(tid, a, titles, baseline, plain_text_title="", gen=GEN4):
    """``desktop_e2e3``'s literal start (``desktop_e2e3.build_start``) under this module's data root."""
    st = Z.build_start(tid, a, titles, baseline, plain_text_title)
    ff = a["start"].get("finder_folder")
    if ff:
        st["finder"]["folder"] = _folder4(tid, a, gen)
    if "textedit" in st:
        st["textedit"]["doc"] = st["textedit"]["doc"].replace(Z.GEN3, gen, 1)
    return st


def build_reads4(tid, a, extra=(), gen=GEN4):
    return [json.loads(json.dumps(p).replace(Z.GEN3, gen)) for p in Z.build_reads(tid, a, extra)]


def build_task4(t, titles, baseline, plain_text_title="", gen=GEN4) -> X.Task:
    a, tr = t["author_task"], t["translation"]
    tid = a["id"]
    expect = json.loads(json.dumps(tr["expect"]).replace("{GEN4}", gen))
    extra = json.loads(json.dumps(tr.get("extra_reads") or []).replace("{GEN4}", gen))
    return X.Task(tid, a["utterance"], Z._kind(a), build_start4(tid, a, titles, baseline, plain_text_title, gen),
                  build_reads4(tid, a, extra, gen), expect, dev=not t["drawn"])


def tasks4(data: Optional[Dict[str, Any]] = None, drawn: Optional[bool] = True) -> List[X.Task]:
    data = data or load_tasks4()
    out = [build_task4(t, data["te_prefs_titles"], data["te_prefs_baseline"], data.get("te_prefs_plain_text") or "")
           for t in data["tasks"]]
    return [t for t in out if drawn is None or (not t.dev) == drawn]


def task4_by_id(tid: str, data: Optional[Dict[str, Any]] = None) -> X.Task:
    for t in tasks4(data, drawn=None):
        if t.id == tid:
            return t
    raise KeyError(tid)


def statements4(tid: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    data = data or load_tasks4()
    t = next(x for x in data["tasks"] if x["author_task"]["id"] == tid)
    return {k.replace("{GEN4}", GEN4): v for k, v in (t["translation"].get("statements") or {}).items()}


def tasks4_sha256(data: Optional[Dict[str, Any]] = None) -> str:
    return X.tasks_sha256(tasks4(data, drawn=True))


def seeds4_spec(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return Z.seeds3_spec(data or load_tasks4())


def _marks_stable(task: X.Task, start, end):
    """Menu items that are UNMARKED at both ends, or unmarked at one end and
    absent at the other, are not marks that changed (Notes' dynamic
    "Darstellung > Neueste Notizen > <note>" list follows every opened note; the
    ``desktop_e2e`` judge compared item paths and counted such an item as a change).
    Only real flips (marked <-> unmarked, marked <-> absent) remain."""
    s2, e2 = list(start or []), list(end or [])
    for k, p in enumerate(task.read):
        if p.get("kind") != "marks" or k >= len(s2) or k >= len(e2):
            continue
        s, e = s2[k] or {}, e2[k] or {}
        if not (s.get("ok") and e.get("ok")):
            continue
        sm = {it["path"]: bool(it.get("mark")) for it in s.get("items") or []}
        em = {it["path"]: bool(it.get("mark")) for it in e.get("items") or []}
        drop = {x for x in set(sm) | set(em) if not sm.get(x) and not em.get(x) and (x in sm) != (x in em)}
        if drop:
            s2[k] = dict(s, items=[it for it in s.get("items") or [] if it["path"] not in drop])
            e2[k] = dict(e, items=[it for it in e.get("items") or [] if it["path"] not in drop])
    return s2, e2


def judge(task: X.Task, start, end, record=None, emap=None, reader=None,
           statements: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """``desktop_e2e3``'s judge (``judge``: the reader judge, coupled marks from the map,
    a declared start for menus of an app not running at the start, the LLM
    reader for changed texts) plus one translation key: ``opened_ok`` = documents
    the USER asked to open while the author's end state names only their text
    (neither required nor an unexpected change)."""
    start, end = _marks_stable(task, start, end)
    out = Z.judge(task, start, end, record, emap, reader, statements)
    # a note REWRITTEN in place (its title / first line gone, another one there, the
    # count unchanged: the desktop_e2e judge compared counts and same-title lines only) lost its text
    for p, s0, e0 in zip(task.read, start or [], end or []):
        if p.get("kind") != "notes" or not (s0 or {}).get("list") or not (e0 or {}).get("list"):
            continue
        if (s0.get("folder") != e0.get("folder")):
            continue
        a0, a1 = {t for t, _l in s0["list"]}, {t for t, _l in e0["list"]}
        gone, new = sorted(a0 - a1), sorted(a1 - a0)
        if gone and new and len(s0["list"]) == len(e0["list"]):
            for g in gone:
                out["irreversible"].append(f"note {g!r} rewritten (its text is gone; now {new})")
    if out["irreversible"]:
        out["success"] = False
    ok = {os.path.basename(str(p)) for p in task.expect.get("opened_ok") or []}
    if ok:
        keep = [u for u in out["unexpected"] if not (u.startswith("document opened: ") and u[17:] in ok)]
        out["allowed_opened"] = [u for u in out["unexpected"] if u not in keep]
        out["unexpected"] = keep
        items = out["items"]
        out["success"] = bool(items) and all(i["status"] == "ok" for i in items) and not out["unexpected"] \
            and not out["irreversible"]
        out["wrong_actions"] = sum(1 for i in items if i["status"] == "wrong") + len(out["unexpected"])
    return out


def run_one4(task: X.Task, arm: str, run: int, guest, *, arch: Optional[Arch4] = None,
             baseline: Optional[X.Baseline] = None, compiled: Optional[List[Dict[str, Any]]] = None,
             mode: str = "measured", emap=None, reader=None, statements: Optional[Dict[str, str]] = None
             ) -> Dict[str, Any]:
    """``desktop_e2e3``'s run (a clean start, twice at most; the arm; the reader) judged by judge."""
    rec = X.Rec(task.id, arm, run, mode=mode)
    t0 = time.monotonic()
    cs = None
    for attempt in (1, 2):
        cs = guest("clean_start", task=task.id, start=task.start, read=task.read)
        X._check_prompt_or_stop(cs)
        if cs.get("clean"):
            break
        rec.notes.append(f"dirty clean start (attempt {attempt}): {cs.get('problems')}")
    rec.clean = {"clean": bool(cs and cs.get("clean")), "problems": (cs or {}).get("problems"),
                 "setup": (cs or {}).get("setup"), "s": round(time.monotonic() - t0, 1)}
    if not (cs and cs.get("clean")):
        rec.stopped = "dirty clean start twice"
        out = rec.to_dict()
        out["judge"] = {"success": False, "items": [], "unexpected": [], "irreversible": [], "wrong_actions": 0,
                        "not_run": "dirty clean start"}
        out["RUN_STOP"] = "two consecutive dirty clean starts (hygiene broken)"
        return out
    start_read = cs.get("read") or []
    try:
        if arm.startswith("arch"):
            arch.run(task, rec, compiled=compiled)
        else:
            baseline.run(task, rec)
    except X._Prompt:
        rec.stopped = "auth prompt seen"
    end = guest("read_state", read=task.read)
    X._check_prompt_or_stop(end)
    out = rec.to_dict()
    out["read_start"], out["read_end"] = start_read, end.get("read") or []
    out["judge"] = judge(task, start_read, end.get("read") or [], out, emap, reader, statements)
    if out["judge"]["irreversible"]:
        out["RUN_STOP"] = "irreversible action: " + "; ".join(out["judge"]["irreversible"])
    return out
