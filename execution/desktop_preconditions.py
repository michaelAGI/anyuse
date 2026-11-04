"""State-conditioned probing.

The effect probe alone presses each candidate once, from ONE probe start. In
that start 2-52 items per app were disabled (Finder's selection commands with
nothing selected, Notes' New Note with the note list hidden, the edit commands
with no text selected), so they were never classified. This module derives
generic PRECONDITIONS from the start state's accessibility facts, so that the
guest can establish one, read which disabled items it enables, probe those
items under it, and restore the state:

    precondition        derived from (roles and observed state only)
    ─────────────────   ────────────────────────────────────────────────────
    pane_shown          an earlier probe row whose press ADDED a container to
                        the window (a pane toggle); established by pressing
                        that item, restored by pressing it again
    one_selected        a container with >= 1 row, nothing selected and a
                        settable selection ("one object selected"); the first
                        row is selected, restored by clearing the selection.
                        A container that already has a selection in the
                        start is a navigation list (its selection IS the
                        shown scope) and is never used
    text_selected       a text area with text and a settable selected range
                        ("text selected in a document"); all of its text is
                        selected, restored by collapsing the range

A precondition may REQUIRE another one (a list shown by a pane toggle, then
one of its rows selected; an object selected, then its document's text). The first precondition, in the fixed order below,
under which an item is enabled is recorded as the one that enabled it; it
becomes part of that candidate's declared clean start.

Facts the guest supplies (pure dicts):

    {"containers": {key: {"rows": int, "selected": int, "selectable": bool, "unique": bool}},
     "text_areas": {key: {"chars": int, "selected": int, "selectable": bool, "unique": bool}}}

No bundle id, title or position decides anything here: container keys are
``role:identifier`` readings inside one app's window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

KINDS = ("text_selected", "one_selected", "pane_shown")
#: the order preconditions are tried in: least invasive first (a selection is
#: undone by clearing it; a pane toggle changes a persisted layout)
ORDER = {k: i for i, k in enumerate(KINDS)}
#: combined preconditions are at most this deep (a pane, then a selection)
MAX_DEPTH = 2


@dataclass(frozen=True)
class Precondition:
    kind: str
    target: str                         # container key | area key | trigger key
    establish: Dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    restore: Dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    requires: Tuple["Precondition", ...] = ()

    @property
    def key(self) -> str:
        own = f"{self.kind}:{self.target}"
        return "+".join([r.key for r in self.requires] + [own])

    def chain(self) -> List["Precondition"]:
        """Every precondition to establish, in order (requirements first)."""
        out: List[Precondition] = []
        for r in self.requires:
            out.extend(r.chain())
        out.append(self)
        return out

    def describe(self) -> str:
        own = {"pane_shown": f"the pane holding {', '.join(self.establish.get('adds') or []) or '?'} shown "
                             f"(pressed {self.target})",
               "one_selected": f"one object selected in {self.target}",
               "text_selected": f"text selected in {self.target}"}.get(self.kind, self.kind)
        return "; ".join([r.describe() for r in self.requires] + [own])

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "target": self.target,
                "establish": dict(self.establish), "restore": dict(self.restore),
                "requires": [r.to_dict() for r in self.requires], "describe": self.describe()}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Precondition":
        return Precondition(d["kind"], d["target"], dict(d.get("establish") or {}),
                            dict(d.get("restore") or {}),
                            tuple(Precondition.from_dict(r) for r in d.get("requires") or []))


def pane_toggles(probes: Iterable[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], List[str]]]:
    """(trigger, added container keys) of every probe row whose press ADDED a
    container to the window without opening or closing a window (a pane
    toggle). Rows from any earlier probe start count; duplicates collapse."""
    out, seen = [], set()
    for r in probes:
        b = (r.get("before") or {})
        a = (r.get("after") or {})
        if not isinstance(b.get("containers"), dict) or not isinstance(a.get("containers"), dict):
            continue
        bw = [w for w in b.get("windows") or [] if w.get("role") == "AXWindow"]
        aw = [w for w in a.get("windows") or [] if w.get("role") == "AXWindow"]
        if len(bw) != len(aw) or (a.get("sheets") or 0) != (b.get("sheets") or 0):
            continue
        added = sorted(set(a["containers"]) - set(b["containers"]))
        trig = r.get("trigger") or {}
        if added and trig.get("key") and trig["key"] not in seen:
            seen.add(trig["key"])
            out.append((trig, added))
    return out


#: which kinds may be derived INSIDE an established precondition (depth 2):
#: a pane shows a list (select in it) or a document (select its text); a
#: selected object opens its document (select its text).
INSIDE = {"pane_shown": ("text_selected", "one_selected"), "one_selected": ("text_selected",)}


def changed_keys(start: Dict[str, Any], now: Dict[str, Any]) -> List[str]:
    """Container / text-area keys whose facts differ from the start (they
    appeared, or e.g. a text area that was empty now holds text)."""
    out = []
    for part in ("containers", "text_areas"):
        s0, s1 = start.get(part) or {}, now.get(part) or {}
        out += [k for k in s1 if s0.get(k) != s1.get(k)]
    return sorted(out)


def derive_inside(parent: Precondition, start: Dict[str, Any], now: Dict[str, Any]) -> List[Precondition]:
    """Preconditions established on top of ``parent`` (depth 2): only kinds
    ``INSIDE`` allows, only on containers/areas the parent changed."""
    if len(parent.chain()) >= MAX_DEPTH or parent.kind not in INSIDE:
        return []
    ch = set(changed_keys(start, now))
    allowed = INSIDE[parent.kind]
    kids = derive(now, requires=(parent,), exclude=[k for part in ("containers", "text_areas")
                                                    for k in (now.get(part) or {}) if k not in ch])
    return [k for k in kids if k.kind in allowed and k.target != parent.target]


def derive(facts: Dict[str, Any], probes: Iterable[Dict[str, Any]] = (),
           requires: Tuple[Precondition, ...] = (), exclude: Iterable[str] = ()) -> List[Precondition]:
    """The preconditions one start state supports, in trial order.

    ``facts``: the start's containers and text areas (module docstring).
    ``probes``: earlier probe rows (pane toggles are read from them; ignored
    when ``requires`` is given: a pane is only toggled from the plain start).
    ``exclude``: container / area keys not to target (inside a precondition:
    everything the precondition did not change)."""
    exclude = set(exclude)
    out: List[Precondition] = []
    for key, a in sorted((facts.get("text_areas") or {}).items()):
        if key in exclude:
            continue
        if (a or {}).get("chars", 0) >= 1 and (a or {}).get("selectable") and not (a or {}).get("selected") \
                and (a or {}).get("unique", True):
            out.append(Precondition("text_selected", key, {"area": key, "range": "all"},
                                    {"area": key, "range": "collapse"}, requires))
    for key, c in sorted((facts.get("containers") or {}).items()):
        if key in exclude:
            continue
        c = c or {}
        # a key shared by several containers (a month grid's day lists) does
        # not pick one: it is never a precondition target
        if c.get("rows", 0) >= 1 and c.get("selectable") and not c.get("selected") and c.get("unique", True):
            out.append(Precondition("one_selected", key, {"container": key, "row": 0},
                                    {"container": key, "clear": True}, requires))
    if not requires:
        for trig, added in pane_toggles(probes):
            out.append(Precondition("pane_shown", trig["key"], {"trigger": trig, "adds": added},
                                    {"trigger": trig, "removes": added}))
    return sorted(out, key=lambda p: (len(p.chain()), ORDER[p.kind], p.key))


def established(pc: Precondition, facts: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Did the guest reach the precondition? Read from facts taken AFTER
    establishing it (and every requirement)."""
    if not isinstance(facts, dict):
        return False, "state not readable after establishing"
    for p in pc.chain():
        if p.kind == "pane_shown":
            missing = [k for k in p.establish.get("adds") or [] if k not in (facts.get("containers") or {})]
            if missing:
                return False, f"pane not shown: {missing} missing"
        elif p.kind == "one_selected":
            c = (facts.get("containers") or {}).get(p.target)
            if not c or c.get("selected") != 1:
                return False, f"{p.target}: {0 if not c else c.get('selected')} selected, want 1"
        elif p.kind == "text_selected":
            a = (facts.get("text_areas") or {}).get(p.target)
            if not a or not a.get("selected"):
                return False, f"{p.target}: no text selected"
    return True, "established"


def attribute(disabled: Sequence[str], trials: Sequence[Tuple[str, Iterable[str]]]) -> Dict[str, str]:
    """{item key: precondition key} for every item disabled in the plain
    start: the FIRST precondition, in trial order, under which it read
    enabled. ``trials`` = [(precondition key, keys read enabled under it)].
    Items never enabled are absent."""
    left = list(disabled)
    out: Dict[str, str] = {}
    for pkey, enabled in trials:
        en = set(enabled)
        for k in list(left):
            if k in en:
                out[k] = pkey
                left.remove(k)
    return out


def merge_rows(plain: Sequence[Dict[str, Any]], conditioned: Sequence[Dict[str, Any]]
               ) -> List[Dict[str, Any]]:
    """The probe rows the mapping reads: every plain row, except that an item
    disabled in the plain start and probed under a precondition is
    represented by its conditioned row (which carries ``precondition``)."""
    cond = {r["key"]: r for r in conditioned if r.get("precondition")}
    # a marked run is classified from ONE start state: a member enabled only
    # under a precondition never joins a run whose other members were probed
    # in the plain start; it is classified alone (Finder's view group
    # would otherwise gain a 'flag' member and lose its class)
    plain_runs = {r.get("run") for r in plain if r.get("kind") == "run" and r.get("enabled")}
    for k, r in cond.items():
        if r.get("kind") == "run" and r.get("run") in plain_runs:
            cond[k] = dict(r, kind="shortcut", run_alone=r.get("run"), run=None, run_members=[])
    out = []
    for r in plain:
        if r.get("enabled") is False and r["key"] in cond:
            out.append(cond.pop(r["key"]))
        else:
            out.append(r)
    out.extend(cond.values())
    return out


def candidate_precondition(rows: Sequence[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The precondition a candidate built from ``rows`` declares in its clean
    start: none when no row needed one; the shared one when every conditioned
    row needed the same; otherwise the candidate is not provable (its members
    need different starts)."""
    pcs = {r["precondition"]["key"]: r["precondition"] for r in rows if r.get("precondition")}
    if not pcs:
        return None, None
    if len(pcs) == 1 and all(r.get("precondition") for r in rows):
        return next(iter(pcs.values())), None
    if len(pcs) == 1:
        # some members enabled in the plain start, others only under the
        # precondition: the precondition does not disable the plain ones
        # (they were enabled in both? not known) -> declare it, the proof checks
        return next(iter(pcs.values())), None
    return None, f"members need different preconditions: {sorted(pcs)}"


def clean_problems(pc: Optional[Dict[str, Any]], facts: Optional[Dict[str, Any]]) -> List[str]:
    """Clean-start check for a candidate with a declared precondition: the
    precondition must hold in the observed start (the hygiene check proper,
    ``desktop_hygiene.check``, is run with no_selection=False for a
    one_selected precondition)."""
    if not pc:
        return []
    ok, why = established(Precondition.from_dict(pc), facts)
    return [] if ok else [f"declared precondition not met: {why}"]
