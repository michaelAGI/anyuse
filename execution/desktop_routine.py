"""Compiled desktop routines with INPUT BINDING, replayed on NEW inputs with runtime checks ("done once, repeated
reliably", as on the web). Technical notes: the design notes.
PURE like the ``desktop_e2e*`` modules (no pyobjc, no subprocess, no network): the guest is an injected callable; those
modules are left byte-identical.

A run's compiled routine (``Rec.compiled``: one ``{"step", "resolved"}`` per step that passed its check) replays
the SAME task on the same data with 0 model calls (measured: 15/15, 12/12, ...). Here:

1. **Input binding** (``derive_routine``): the routine's parameters are read off the successful trace — the text
   written or appended (``text``), the old text an edit replaces (``anchor``), the detail read from a text
   (``detail``), the term looked up (``lookup``), the name of a new object (``name``), the folder entered
   (``folder``), the note or file opened or selected (``note`` / ``file``), a flag's value (``setting``: on/off)
   and a menu group's option (``choice``) — each bound to EVERY place it occurs: a step's ``target`` / ``value`` /
   ``find`` / ``scope``, the resolution's ``label`` / ``enter`` / ``option``, and its verbatim spans in the request text (the
   checks that read the user's words read the new values). ``instantiate`` writes new values into a copy; the
   compiled format is unchanged (a plain list): a routine without parameters, or a replay without new values, is
   today's behaviour byte for byte.
2. **Runtime checks on every parameterized replay** (``ReplayArch``: ``desktop_e2e12.Arch12`` with
   ``precondition13`` in compiled mode, i.e. with the recovery layer): the parameter values first (a flag is on/off; a
   choice is exactly one option of the recorded group; no option that is irreversible or reaches outside the Mac);
   then, before acting, the TARGET'S IDENTITY (the named folder / note / file exists exactly once, is of the
   right kind, lies in the folder the replay entered; a new object's name is not taken yet; before a human gate
   the item selected or shown is the one the replay bound; the note rows are read in every Notes view: a note
   opened from the gallery hides the grid, the guest goes back to it for the candidates,
   and a view whose rows cannot be read is a refusal, never an empty folder); the exact permitted diff before and after the save,
   the journal with its compare-and-swap restore, the concurrency and focus checks (``desktop_e2e9``'s, inherited); the
   irreversible-action guard (the guest's, unchanged) and the human gates the routine recorded. An unmet check
   is an honest failure (nothing guessed); a restore is recorded as an error (judge).
3. **A choice is bound by the option's identity** (``execution/choice_identity.py``): the parameter
   carries the recorded option's position-free ``key`` (its AXIdentifier; a shared identifier with its shortcut
   and recorded title) and the group's ``options``; a value names an option by that key, its recorded key or its
   RECORDED label; a parameterized replay carries the identity to the guest in the option's trigger, so the
   guest finds the option by identity in another UI language and in a reordered list.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import choice_identity as CI
from . import desktop_e2e as X
from . import desktop_e2e4 as W
from . import desktop_e2e6 as U
from . import desktop_e2e12 as Z
from . import desktop_fixes as F2

ROUTINE_FORMAT = "anyuse-desktop-routine/1"
NOTES, FINDER, TEXTEDIT = "com.apple.Notes", "com.apple.finder", "com.apple.TextEdit"

#: the kinds of parameter, each with the family that ties equal values together (one value, one parameter)
ROUTINE_KINDS = {"folder": "item", "note": "item", "file": "item", "text": "words", "anchor": "words", "detail": "words",
           "lookup": "words", "name": "words", "setting": "step", "choice": "step"}
#: the order in which a value found under several kinds names its parameter
_KIND_RANK = ("detail", "anchor", "lookup", "text", "name", "folder", "note", "file", "setting", "choice")
#: option labels that reach outside the Mac (Lexikon's web sources): never chosen by a replay
EXTERNAL_LABELS15 = ("wikipedia", "alle lexika", "all dictionaries")
#: a compiled stop: the step is a refusal / gate / location stop, it carries no input
_STOP_OPS = ("gate", "refuse", "location", "restored", "handoff")
#: request spans shorter than this are not bound (a value "on" or "18" would hit unrelated words)
MIN_GOAL_SPAN = 3


def _literal(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip()) and not W.VAR_RE.search(v)


def _item_kind(step: Dict[str, Any]) -> str:
    return "note" if X.BUNDLES.get(step.get("app")) == NOTES else "file"


def occurrences(compiled: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, Tuple[Any, ...]]]:
    """(kind, value, path) of every input of a compiled routine; a path is (step index, "step" | "resolved", key
    [, list index])."""
    out: List[Tuple[str, str, Tuple[Any, ...]]] = []
    for i, c in enumerate(compiled):
        s, r = c.get("step") or {}, c.get("resolved") or {}
        eff, op = s.get("effect"), r.get("op")
        if op in _STOP_OPS:
            continue
        if _literal(s.get("scope")):
            # the container / document the plan names for this step (desktop_e2e4's location check reads it)
            out.append(("file" if W.FILE_RE.search(s["scope"]) else "folder", s["scope"], (i, "step", "scope")))
        if eff == "navigate" and r.get("label"):
            out.append(("folder", r["label"], (i, "resolved", "label")))
            if X.norm(s.get("target")) == X.norm(r["label"]):
                out.append(("folder", r["label"], (i, "step", "target")))
        elif eff in ("open_item", "select_item") and r.get("label"):
            k = _item_kind(s)
            out.append((k, r["label"], (i, "resolved", "label")))
            if X.norm(s.get("target")) == X.norm(r["label"]):
                out.append((k, r["label"], (i, "step", "target")))
            for j, f in enumerate(r.get("enter") or []):
                out.append(("folder", f, (i, "resolved", "enter", j)))
        elif eff in ("doc_save", "field_rw"):
            if _literal(s.get("value")):
                out.append(("text", s["value"], (i, "step", "value")))
            if _literal(s.get("find")):
                out.append(("anchor", s["find"], (i, "step", "find")))
        elif eff == "scoped_find" and _literal(s.get("value")):
            out.append(("lookup", s["value"], (i, "step", "value")))
        elif eff == "extract" and _literal(s.get("find")):
            out.append(("detail", s["find"], (i, "step", "find")))
            if r.get("label") == s["find"]:
                out.append(("detail", s["find"], (i, "resolved", "label")))
        elif eff == "object_create" and _literal(s.get("value")):
            out.append(("name", s["value"], (i, "step", "value")))
        elif eff == "flag_toggle" and X.on_off(s.get("value")) is not None:
            out.append(("setting", s["value"], (i, "step", "value")))
        elif eff == "toggle_select" and _literal(s.get("value")) and (r.get("entry") or {}).get("options"):
            out.append(("choice", s["value"], (i, "step", "value")))
    return out


def derive_routine(compiled: Sequence[Dict[str, Any]], goal: str = "", **meta) -> Dict[str, Any]:
    """The routine of a successful run: its compiled steps EXACTLY as recorded, and its parameters, each bound to
    every place it occurs (equal values of one family are ONE parameter: a file name in the open step and in the
    request, an edit's old text in the extract and in the edit) and to its verbatim spans in the request."""
    groups: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    order: List[Tuple[str, Any]] = []
    for kind, value, path in occurrences(compiled):
        fam = ROUTINE_KINDS[kind]
        key = (fam, path[0] if fam == "step" else F2.nfc(value))          # one name, composed or not: one parameter
        if key not in groups:
            groups[key] = {"kinds": [], "value": value, "bind": []}
            order.append(key)
        g = groups[key]
        if kind not in g["kinds"]:
            g["kinds"].append(kind)
        g["bind"].append(list(path))
    params: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for key in order:
        g = groups[key]
        kind = min(g["kinds"], key=_KIND_RANK.index)
        seen[kind] = seen.get(kind, 0) + 1
        name = kind if seen[kind] == 1 else f"{kind}_{seen[kind]}"
        v = g["value"]
        spans = (F2.nfc(goal).count(F2.nfc(v)) if (goal and ROUTINE_KINDS[kind] != "step" and len(v) >= MIN_GOAL_SPAN)
                 else 0)
        p = {"name": name, "kind": kind, "value": v, "bind": g["bind"], "goal_spans": spans}
        if kind == "choice":
            p.update(_choice_identity(compiled, g["bind"][0][0]))
        params.append(p)
    return {"format": ROUTINE_FORMAT, "steps": list(compiled), "goal": goal, "params": params, **meta}


def _group(compiled: Sequence[Dict[str, Any]], i: int) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    r = compiled[i].get("resolved") or {}
    return list(((r.get("entry") or {}).get("options")) or []), r.get("option")


def _choice_identity(compiled: Sequence[Dict[str, Any]], i: int) -> Dict[str, Any]:
    """A choice parameter's identity (``choice_identity``): the recorded option's position-free key and the
    group's options (key and recorded label), ``by: identity``; ``by: label`` when the recorded option has no
    position-free identity (then a value is matched to the recorded labels, as before)."""
    options, opt = _group(compiled, i)
    keys = CI.stable_keys(options)
    at = [k for k, o in enumerate(options) if opt is not None and o.get("key") == opt.get("key")]
    key = keys[at[0]] if len(at) == 1 else None
    return {"by": "identity" if key else "label", "key": key,
            "options": [{"key": k, "label": o.get("label")} for o, k in zip(options, keys)]}


def is_routine(x: Any) -> bool:
    return isinstance(x, dict) and x.get("format") == ROUTINE_FORMAT


def parameter_problems(routine: Dict[str, Any], values: Dict[str, Any]) -> List[str]:
    """The new values a replay refuses before acting: an unknown parameter, an empty or multi-line name, a flag value
    that is not on/off, a choice that is not exactly one option of the recorded group, an option that is
    irreversible or reaches outside the Mac."""
    probs: List[str] = []
    by = {p["name"]: p for p in routine.get("params") or []}
    for name, v in (values or {}).items():
        p = by.get(name)
        if p is None:
            probs.append(f"unknown parameter {name!r}")
            continue
        if not isinstance(v, str):
            probs.append(f"{name}: the value is not text")
            continue
        k = p["kind"]
        if k in ("folder", "note", "file", "name", "lookup", "detail", "anchor") and (not v.strip() or "\n" in v):
            probs.append(f"{name}: an empty or multi-line {k}")
        if k in ("folder", "file", "name") and "/" in v:
            probs.append(f"{name}: a {k} name with a path separator")
        if k == "setting" and X.on_off(v) is None:
            probs.append(f"{name}: {v!r} is not on/off")
        if k == "choice":
            opt, why = _option_for(routine, p, v)
            if why:
                probs.append(f"{name}: {why}")
    return probs


def _option_for(routine: Dict[str, Any], p: Dict[str, Any], label: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """The recorded option a choice value names: by its identity key, its recorded key or its recorded label
    (``choice_identity.resolve_value``); a parameter derived before identities (no ``by``) by its label."""
    i = p["bind"][0][0]
    entry = ((routine["steps"][i].get("resolved") or {}).get("entry") or {})
    options = entry.get("options") or []
    if p.get("by") == "identity":
        idx, why = CI.resolve_value(label, options, require_key=False)
        if idx is None:
            return None, why
        o = options[idx]
    else:
        hits = [o for o in options if X.norm(o.get("label")) == X.norm(label)]
        if len(hits) != 1:
            return None, f"{len(hits)} options of the recorded group are labelled {label!r}"
        o = hits[0]
    why = X.irreversible_reason(o.get("label"), o.get("path"))
    if why:
        return None, f"the option {label!r} is irreversible ({why})"
    if any(e in X.norm(o.get("label")) for e in EXTERNAL_LABELS15):
        return None, f"the option {label!r} reaches outside the Mac"
    if p.get("by") == "identity" and CI.stable_keys(options)[idx] is None:
        return None, f"the option {label!r} {CI.NO_IDENTITY}"
    return o, ""


def _set(steps: List[Dict[str, Any]], path: Sequence[Any], value: Any) -> None:
    node: Any = steps[path[0]][path[1]]
    for k in path[2:-1]:
        node = node[k]
    node[path[-1]] = value


def _goal_with(goal: str, swaps: Dict[str, str]) -> str:
    if not swaps or not goal:
        return goal
    pat = re.compile("|".join(re.escape(k) for k in sorted(swaps, key=len, reverse=True)))
    return pat.sub(lambda m: swaps[m.group(0)], goal)


def instantiate(routine: Any, values: Optional[Dict[str, Any]] = None
                ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """(the compiled steps, the request text, problems) for a replay with ``values`` = {parameter name: new
    value}. A plain list (today's compiled format) or no new values: the steps exactly as recorded (the same
    list, byte-identical). Problems mean the replay must not act."""
    if not is_routine(routine):
        if values:
            return list(routine or []), "", ["a routine without parameters takes no values"]
        return routine, "", []
    vals = {k: F2.nfc(v) for k, v in (values or {}).items()}               # new values bound composed
    probs = parameter_problems(routine, vals)
    by = {p["name"]: p for p in routine.get("params") or []}
    changed = {k: v for k, v in vals.items() if k in by and not _same_value(routine, by[k], v)}
    if probs or not changed:
        return routine["steps"], routine.get("goal") or "", probs
    steps = copy.deepcopy(routine["steps"])
    swaps: Dict[str, str] = {}
    for name, v in changed.items():
        p = by[name]
        if p["kind"] == "choice":
            opt = _option_for(routine, p, v)[0]
            for path in p["bind"]:
                _set(steps, path, opt.get("label"))           # the step names the option by its recorded label
            _set(steps, [p["bind"][0][0], "resolved", "option"], copy.deepcopy(opt))
            continue
        for path in p["bind"]:
            _set(steps, path, v)
            if p["kind"] == "folder" and path[1:] == ["resolved", "label"]:
                steps[path[0]]["resolved"]["via"] = None          # a new folder: the identity check finds its row
        if p.get("goal_spans"):
            swaps[F2.nfc(p["value"])] = v
    _carry_identities(steps)
    return steps, _goal_with(F2.nfc(routine.get("goal") or ""), swaps), []


def _same_value(routine: Dict[str, Any], p: Dict[str, Any], v: Any) -> bool:
    """A value that names the recorded one (a choice: the recorded option, by key, recorded key or label)."""
    if p["kind"] == "choice" and p.get("by") == "identity":
        i = p["bind"][0][0]
        opt = _option_for(routine, p, v)[0]
        rec = (routine["steps"][i].get("resolved") or {}).get("option") or {}
        return opt is not None and opt.get("key") == rec.get("key")
    return F2.same_name(v, p["value"])


def _carry_identities(steps: List[Dict[str, Any]]) -> None:
    """In a parameterized replay's COPY: every menu-group option the replay presses or reads carries its
    position-free identity in its trigger (``trigger.identity``), so the guest finds it by identity, not by
    the recorded shortcut alone. Options without one keep today's trigger."""
    for c in steps:
        s, r = c.get("step") or {}, c.get("resolved") or {}
        if s.get("effect") != "toggle_select" or not isinstance(r.get("entry"), dict):
            continue
        options = r["entry"].get("options") or []
        for k, o in enumerate(options):
            ident = CI.identity_for(options, k)
            if ident is not None and isinstance(o.get("trigger"), dict):
                o["trigger"]["identity"] = ident
        opt = r.get("option")
        if isinstance(opt, dict) and isinstance(opt.get("trigger"), dict):
            k = next((k for k, o in enumerate(options) if o.get("key") == opt.get("key")), None)
            if k is not None and "identity" in options[k].get("trigger", {}):
                opt["trigger"]["identity"] = options[k]["trigger"]["identity"]


def routine_sha256(routine: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(routine, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 2. The checked replay
# ---------------------------------------------------------------------------

_LIST_OF = {("navigate", FINDER): "finder_locations", ("navigate", NOTES): "notes_folders",
            ("open_item", NOTES): "notes_notes", ("select_item", NOTES): "notes_notes",
            ("open_item", FINDER): "finder_files", ("select_item", FINDER): "finder_files"}


class ReplayArch(F2.DesktopFixes, Z.Arch12):
    """A routine's replay on new inputs: ``Arch12`` with ``precondition13`` in COMPILED mode (no planner,
    no picker: 0 model calls; the recovery layer on every write) plus the checks of this module's
    docstring (item 2), under the desktop fixes layer (``desktop_fixes.DesktopFixes``).
    ``prepare(routine, values)`` gives the steps and the request text for ``run_one``; the run refuses to act when
    the values failed their check."""

    protocol15 = "the checked replay of a parameterized routine (Arch12, declared precondition, compiled)"

    def __init__(self, guest, emap, **kw):
        kw.setdefault("planner", None)
        kw.setdefault("precondition13", True)
        super().__init__(guest, emap, **kw)
        self._problems15: List[str] = []
        self._values15: Dict[str, Any] = {}

    def prepare(self, routine: Any, values: Optional[Dict[str, Any]] = None
                ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
        steps, goal, probs = instantiate(routine, values)
        self._problems15, self._values15 = list(probs), dict(values or {})
        return steps, goal, probs

    def run(self, task: X.Task, rec: X.Rec, compiled=None) -> X.Rec:
        self._bound15: Dict[str, str] = {}
        rec.__dict__.setdefault("identity15", [])
        rec.__dict__["values15"] = dict(self._values15)
        if self._problems15:
            rec.__dict__["parameter_problems15"] = list(self._problems15)
            rec.stopped = "refused (parameter check): " + "; ".join(self._problems15)[:300]
            return rec
        return super().run(task, rec, compiled)

    # -- the target's identity, before acting ------------------------------------------------------
    def _rows15(self, bid: str, kind: str, candidates: bool = False) -> Dict[str, Any]:
        """The list ``kind`` as the guest reads it. ``candidates``: the identity check asks for the rows to act
        on in EVERY Notes view: a note opened from the gallery hides the grid (as a start view it gave 6
        false refusals), so the guest goes back to the grid for them (``back``), exactly the read the step itself
        makes right after; a plain read (the gate's check of the note shown) never goes back."""
        try:
            if candidates and kind == "notes_notes":
                return self.call("rows", bundle=bid, kind=kind, back=True)
            return self.call("rows", bundle=bid, kind=kind)
        except X.GuestError:
            return {}

    def _identity15(self, step: Dict[str, Any], fixed: Dict[str, Any]) -> Optional[str]:
        eff, bid = step.get("effect"), X.BUNDLES.get(step.get("app"))
        op = fixed.get("op")
        if op in _STOP_OPS or op is None:
            return None
        if eff == "object_create":
            return self._identity_create15(bid, step.get("value") or "")
        kind = _LIST_OF.get((eff, bid))
        label = fixed.get("label")
        if kind is None or not label:
            return None
        if eff != "navigate" and bid == FINDER and fixed.get("enter"):
            kind, label, want_kind = "finder_files", fixed["enter"][0], "folder"
        else:
            want_kind = "folder" if eff == "navigate" else ("file" if bid == FINDER else "note")
        rows = self._rows15(bid, kind, candidates=True)
        self._went_back15 = bool(rows.get("went_back"))
        if not rows:
            return f"the list {kind} could not be read"
        if kind == "notes_notes" and not rows.get("rows"):
            # never act without identity: a view whose rows cannot be read is not an empty folder
            return (f"no note rows could be read in {rows.get('where')!r}"
                    + (f" (the note {rows.get('shown_note')!r} is shown, the list is not)" if rows.get("shown_note")
                       else "") + f": the identity of {label!r} is not confirmed")
        rs = [r for r in rows.get("rows") or [] if F2.same_name(r.get("label"), label)
              and (kind not in ("finder_locations", "notes_folders") or not fixed.get("via")
                   or r.get("via") == fixed.get("via"))]
        if len(rs) != 1:
            return (f"{label!r} is not in {rows.get('where')!r}" if not rs else
                    f"{len(rs)} items are named {label!r} in {rows.get('where')!r} (not guessed)")
        r = rs[0]
        if kind == "notes_folders" and U.is_search_scope(label):
            return f"{label!r} is a search scope, not a folder"
        if kind == "finder_files" and want_kind == "file" and r.get("kind") == "folder":
            return f"{label!r} is a folder, the routine opens a file"
        if kind == "finder_files" and want_kind == "folder" and r.get("kind") not in (None, "folder"):
            return f"{label!r} is not a folder"
        folder = self._bound15.get(f"folder:{bid}")
        if kind in ("notes_notes", "finder_files") and folder and not F2.same_name(rows.get("where"), folder):
            return f"the list shows {rows.get('where')!r}, the replay entered {folder!r}"
        return None

    def _identity_create15(self, bid: Optional[str], name: str) -> Optional[str]:
        try:
            where = self.call("shown_folder", bundle=bid)
            folder = where.get("path") or ""
            names = (self.call("listing", path=folder).get("names") or []) if folder else []
        except X.GuestError:
            return None
        bound = self._bound15.get(f"folder:{bid}")
        if bound and folder and not F2.same_name(folder.rstrip("/").rsplit("/", 1)[-1], bound):
            return f"the folder shown is {folder.rsplit('/', 1)[-1]!r}, the replay entered {bound!r}"
        if name and F2.nfc(name) in {F2.nfc(n) for n in names}:
            return f"{name!r} already exists in {folder.rsplit('/', 1)[-1]!r}"
        return None

    def _gate15(self, step: Dict[str, Any]) -> Optional[str]:
        """Before a human gate: the item selected (Finder) or shown (Notes) is the one this replay bound."""
        bid = X.BUNDLES.get(step.get("app"))
        want = self._bound15.get(f"item:{bid}")
        if not want or bid not in (FINDER, NOTES):
            return None
        rows = self._rows15(bid, "finder_files" if bid == FINDER else "notes_notes")
        seen = ([r["label"] for r in rows.get("rows") or [] if r.get("selected")] if bid == FINDER
                else [rows.get("shown_note")])
        same = len(seen) == 1 and F2.same_name(seen[0], want)
        return None if same else f"before the gate the item is {seen!r}, the replay bound {want!r}"

    def do_step(self, task, rec, step, log, fixed=None):
        why = None
        self._went_back15 = False
        if fixed is not None:
            why = self._identity15(step, fixed)
            if why is None and step.get("effect") == "human_gate":
                why = self._gate15(step)
        if why:
            log["why"] = "identity check: " + why + " (nothing done)"
            log["identity15"] = {"ok": False, "why": why}
            rec.identity15.append({"i": log.get("i"), "effect": step.get("effect"), "why": why})
            return "failed"
        if fixed is not None:
            log["identity15"] = {"ok": True, **({"went_back": True} if self._went_back15 else {})}
        out = super().do_step(task, rec, step, log, fixed)
        if out == "ok":
            bid = X.BUNDLES.get(step.get("app"))
            res = log.get("resolved") or {}
            if step.get("effect") == "navigate" and res.get("label"):
                self._bound15[f"folder:{bid}"] = res["label"]
                self._bound15.pop(f"item:{bid}", None)
            elif step.get("effect") in ("open_item", "select_item") and res.get("label"):
                if res.get("enter"):
                    self._bound15[f"folder:{bid}"] = res["enter"][-1]
                self._bound15[f"item:{bid}"] = res["label"]
        return out


def replay_arch(guest, emap, **kw) -> ReplayArch:
    return ReplayArch(guest, emap, **kw)
