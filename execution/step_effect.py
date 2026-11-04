"""Declared expected effects decide a step's success.

The actuator alone does not decide whether a declared effect happened: a
``set_value`` read-back or a press that changed nothing on screen is not taken
as the effect. The bench's permitted-diff check (``execution/desktop_e2e9.py``),
which refuses a write outside the user's words, is here the general product
rule; nothing in it knows an app.

A step DECLARES its effect in ``step.command["expect"]`` (JSON, persisted with
the step or the recorded implementation):

``changes``   matchers that MUST be observed (all of them);
``permitted`` matchers that MAY also change;
``only``      (default true) anything else that changed is a DEVIATION - the
              bench's permitted-diff rule as a product rule: the effect must lie
              within the declared diff;
``state``     a recorded state id the screen must match afterwards (its
              fingerprint, through the drift gate's own comparison).

A matcher is ``{"op", "role", "name", "identifier", "value", "target"}``:
``op`` is ``added`` / ``removed`` / ``changed`` (in the diff), ``value`` /
``present`` / ``absent`` (in the screen afterwards) or ``any`` (permitted
only); ``role`` is the raw AX role, ``name`` the title or AX description,
``target: true`` the step's own recorded element, and ``value`` may be
``${input}`` (the value the step typed). The verdict:

``observed``      every required change is there and nothing else changed;
``not_observed``  a required change is missing (or the screen is not the
                  declared state): ``uncertain_completion`` - acting again could
                  write twice, so the run stops, and a delayed effect is never
                  answered with a second write;
``deviation``     something outside the declared effect changed: the run stops
                  (a deviation stops, it does not ask);
``undeclared``    the step declares nothing: the actuator's own evidence stands
                  (``set_value``/``enter_text`` read back; nothing else is
                  claimed);
``unverifiable``  the declaration cannot be checked here (no AX snapshots, a
                  state with no recorded fingerprint) - reported, and treated as
                  ``not_observed`` when it was the only check.

Some changes are never effects: scroll bars, progress and value indicators,
unlabelled structural containers and the menu bar's transient menus.
``effect_from_diff`` turns an observed diff (a demonstration, a proof) into a
declaration: the target's value for a write, a window or sheet that opened or
closed, a toggled control; everything else it saw is permitted, by identity
for a control and by role for content (rows, text, cells) whose values change
with the parameters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

WRITE_ACTIONS = frozenset({"set_value", "enter_text", "insert_text", "replace_range"})
TOGGLE_ACTIONS = frozenset({"toggle"})
VOLATILE_ROLES = frozenset({"AXScrollBar", "AXValueIndicator", "AXProgressIndicator",
                            "AXBusyIndicator", "AXLevelIndicator", "AXRelevanceIndicator"})
STRUCTURAL_ROLES = frozenset({"AXGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea",
                              "AXLayoutItem", "AXUnknown", "AXSplitter", ""})
WINDOW_ROLES = frozenset({"AXWindow", "AXSheet", "AXDialog", "AXPopover", "AXDrawer"})
INPUT = "${input}"

OBSERVED, NOT_OBSERVED, DEVIATION, UNDECLARED, UNVERIFIABLE = (
    "observed", "not_observed", "deviation", "undeclared", "unverifiable")


@dataclass
class EffectVerdict:
    kind: str
    reason: str = ""
    missing: List[Dict[str, Any]] = field(default_factory=list)
    unexpected: List[str] = field(default_factory=list)
    state: Optional[Dict[str, Any]] = None
    #: an UNVERIFIABLE verdict blocks (counts as not observed) only when the
    #: declaration REQUIRED a change that could not be looked for
    required: bool = False

    @property
    def outcome_kind(self) -> str:
        """The executor outcome this verdict turns a successful action into
        ("" = the action's own success stands)."""
        if self.kind == DEVIATION:
            return "deviation"
        if self.kind == NOT_OBSERVED or (self.kind == UNVERIFIABLE and self.required):
            return "uncertain_completion"
        return ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"kind": self.kind, "reason": self.reason}
        if self.missing:
            d["missing"] = self.missing
        if self.unexpected:
            d["unexpected"] = self.unexpected[:10]
        if self.state is not None:
            d["state"] = self.state
        return d


def declared(step: Any) -> Optional[Dict[str, Any]]:
    cmd = getattr(step, "command", None) or {}
    exp = cmd.get("expect") if isinstance(cmd, dict) else None
    return exp if isinstance(exp, dict) and exp else None


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def _canon(role: Optional[str]) -> str:
    from anyuse.context.accessibility import canonical_role
    return canonical_role(role)


def _is_target(el: Any, target: Any) -> bool:
    """Is snapshot element ``el`` the step's recorded element (a ``Selector``)?

    A snapshot carries no element references, so this is identity by
    attributes: the identifier when the recording has one, else the label
    (title or AX description). A selector's ``name`` may have come from the
    element's VALUE (``selector_from_attrs`` falls back to it), which a write
    changes; an element with no label and no identifier of the same role is
    therefore accepted as well - the identity check before the action already
    decided which element was written."""
    if target is None or el is None:
        return False
    if _canon(getattr(el, "role", None)) != getattr(target, "canonical_role", ""):
        return False
    tid = getattr(target, "identifier", "") or ""
    if tid:
        return (getattr(el, "identifier", None) or "") == tid
    labels = ((getattr(el, "title", None) or ""), (getattr(el, "ax_description", None) or ""))
    unlabelled = not any(labels) and not getattr(el, "identifier", None)
    tname = getattr(target, "name", "") or ""
    if tname:
        return tname in labels or unlabelled
    return unlabelled


def _identity_matches(m: Dict[str, Any], el: Any, target: Any) -> bool:
    if m.get("target") and not _is_target(el, target):
        return False
    role = m.get("role")
    if role and (getattr(el, "role", None) or "") != role:
        return False
    name = m.get("name")
    if name and name not in ((getattr(el, "title", None) or ""), (getattr(el, "ax_description", None) or "")):
        return False
    ident = m.get("identifier")
    if ident and (getattr(el, "identifier", None) or "") != ident:
        return False
    return True


def _value_of(m: Dict[str, Any], input_value: Optional[str]) -> Optional[str]:
    if "value" not in m:
        return None
    v = m.get("value")
    if v == INPUT:
        return "" if input_value is None else str(input_value)
    return None if v is None else str(v)


def _matches(m: Dict[str, Any], el: Any, target: Any, input_value: Optional[str]) -> bool:
    if not _identity_matches(m, el, target):
        return False
    have = getattr(el, "value", None) or ""
    if "contains" in m:
        part = m.get("contains")
        part = ("" if input_value is None else str(input_value)) if part == INPUT else str(part or "")
        if part not in have:
            return False
    want = _value_of(m, input_value)
    return want is None or have == want


def _entries(diff: Any) -> List[Tuple[str, Any]]:
    out: List[Tuple[str, Any]] = []
    for e in getattr(diff, "added", None) or []:
        out.append(("added", e))
    for e in getattr(diff, "removed", None) or []:
        out.append(("removed", e))
    for _b, a in getattr(diff, "changed", None) or []:
        out.append(("changed", a))
    return out


def is_noise(el: Any) -> bool:
    """A change that is never an effect (scrolling, progress, layout, menus)."""
    role = getattr(el, "role", None) or ""
    if role in VOLATILE_ROLES or getattr(el, "in_menubar", False):
        return True
    labelled = any(getattr(el, k, None) for k in ("title", "ax_description", "identifier"))
    return role in STRUCTURAL_ROLES and not labelled


def _describe(op: str, el: Any) -> str:
    label = (getattr(el, "title", None) or getattr(el, "ax_description", None)
             or getattr(el, "identifier", None) or "")
    val = getattr(el, "value", None)
    out = f"{op} {getattr(el, 'role', None) or '?'} {label!r}"
    if op == "changed" and val is not None:
        out += f" -> {str(val)[:40]!r}"
    return out


def _required_seen(m: Dict[str, Any], entries, after, target, input_value) -> Tuple[bool, List[int]]:
    """(seen, indexes of the diff entries it explains)."""
    op = m.get("op") or "any"
    explained = [i for i, (eop, el) in enumerate(entries)
                 if (op in ("any", "value", "present") or eop == op) and _identity_matches(m, el, target)]
    if op in ("value", "present"):
        seen = any(_matches(m, el, target, input_value) for el in (getattr(after, "elements", None) or []))
    elif op == "absent":
        seen = not any(_identity_matches(m, el, target) for el in (getattr(after, "elements", None) or []))
    else:
        seen = any(_matches(m, el, target, input_value) for i, (_o, el) in enumerate(entries)
                   if (op == "any" or entries[i][0] == op))
    return seen, explained


def _state_check(state_id: str, after: Any, state_graph: Any) -> Dict[str, Any]:
    from anyuse.execution import drift_gate
    if state_graph is None or not drift_gate_usable(state_graph, state_id):
        return {"state": state_id, "checked": False, "why": "no recorded fingerprint for this state here"}
    els = [{"role": getattr(e, "role", None) or "", "title": getattr(e, "title", None) or ""}
           for e in (getattr(after, "elements", None) or [])]
    rep = drift_gate.evaluate_drift(state_graph, state_id, els)
    return {"state": state_id, "checked": True, "matched": not rep.drifted, "why": rep.reason}


def drift_gate_usable(state_graph: Any, state_id: Optional[str]) -> bool:
    from anyuse.execution.drift_gate import _fingerprint_is_placeholder
    getter = getattr(state_graph, "get_state", None)
    return bool(state_id) and getter is not None and not _fingerprint_is_placeholder(getter(state_id))


def _independent_check(m: Dict[str, Any], after: Any, target: Any, input_value: Optional[str],
                       independent: Any) -> str:
    """Verifier independence (``attribute_verify.verify_text_effect``; ``ANYUSE_ATTRIBUTE_VERIFY``): a write's text
    seen in the AX value (the actuator's own source) must also be in every INDEPENDENT reading the step has (the file
    read after a save, the document text over Apple Events). "" when it holds or nothing independent was read."""
    if not independent or input_value is None:
        return ""
    if m.get("value") != INPUT and m.get("contains") != INPUT:
        return ""
    from anyuse.execution import attribute_verify as AV
    if not AV.enabled():
        return ""
    ax = next((getattr(el, "value", None) for el in (getattr(after, "elements", None) or [])
               if _matches(m, el, target, input_value)), None)
    d = AV.verify_text_effect(input_value, ax, list(independent), contains=m.get("contains") == INPUT)
    return "" if d.status == AV.VERIFIED else d.why


def evaluate(effect: Optional[Dict[str, Any]], before: Any, after: Any, diff: Any, *,
             target: Any = None, input_value: Optional[str] = None,
             state_graph: Any = None, independent: Any = None) -> EffectVerdict:
    """The verdict of one declared effect against one observed before/after. ``independent``: readings of the written
    text from another source than AX (``[(source, text)]``; verifier independence, used only with the setting on)."""
    if not effect:
        return EffectVerdict(UNDECLARED, "the step declares no expected effect")
    changes = list(effect.get("changes") or [])
    permitted = list(effect.get("permitted") or [])
    # the permitted-diff rule applies to a declaration that lists a diff (or says
    # "only" itself); a state-only declaration is checked by its state alone
    only = effect.get("only", bool(changes or permitted)) is not False
    state_id = effect.get("state")
    if after is None or diff is None:
        return EffectVerdict(UNVERIFIABLE, "no AX snapshots to observe the effect in",
                             required=bool(changes))
    entries = _entries(diff)
    explained: set = set()
    missing: List[Dict[str, Any]] = []
    for m in changes:
        seen, idx = _required_seen(m, entries, after, target, input_value)
        explained.update(idx)
        if not seen:
            missing.append({k: v for k, v in m.items() if v != INPUT})
            continue
        why = _independent_check(m, after, target, input_value, independent)
        if why:
            missing.append(dict({k: v for k, v in m.items() if v != INPUT}, independent=why))
    for m in permitted:
        for i, (eop, el) in enumerate(entries):
            mop = m.get("op") or "any"
            if (mop in ("any", "value", "present") or mop == eop) and _identity_matches(m, el, target):
                explained.add(i)
    unexpected = [_describe(op, el) for i, (op, el) in enumerate(entries)
                  if i not in explained and not is_noise(el)] if only else []
    state = _state_check(state_id, after, state_graph) if state_id else None
    state_failed = bool(state and state.get("checked") and not state.get("matched"))
    if unexpected:
        return EffectVerdict(DEVIATION, "changed outside the declared effect: " + "; ".join(unexpected[:5]),
                             missing=missing, unexpected=unexpected, state=state)
    if missing or state_failed:
        why = []
        if missing:
            why.append("not observed: " + "; ".join(
                f"{m.get('op', 'any')} {m.get('role') or ''} {m.get('name') or m.get('identifier') or ('the target' if m.get('target') else '')}".strip()
                + (f" (independent reading: {m['independent']})" if m.get("independent") else "")
                for m in missing[:5]))
        if state_failed:
            why.append(f"the screen is not the recorded state {state_id} ({state.get('why')})")
        return EffectVerdict(NOT_OBSERVED, "; ".join(why), missing=missing, state=state)
    if not changes and state is not None and not state.get("checked"):
        return EffectVerdict(UNVERIFIABLE, state.get("why", ""), state=state)
    return EffectVerdict(OBSERVED, "the declared effect was observed", state=state)


# ---------------------------------------------------------------------------
# declaring from an observation
# ---------------------------------------------------------------------------

def _identity(el: Any) -> Dict[str, Any]:
    m: Dict[str, Any] = {"role": getattr(el, "role", None) or ""}
    if getattr(el, "identifier", None):
        m["identifier"] = el.identifier
    elif getattr(el, "title", None) or getattr(el, "ax_description", None):
        m["name"] = el.title or el.ax_description
    return m


def effect_from_diff(diff: Any, *, target: Any = None, action: str = "",
                     state_after: Optional[str] = None) -> Dict[str, Any]:
    """A declaration from an observed demonstration (see the module docstring)."""
    from anyuse.context.accessibility import ACTIONABLE_ROLES
    changes: List[Dict[str, Any]] = []
    permitted: List[Dict[str, Any]] = []
    if action in ("set_value", "enter_text") and target is not None:
        changes.append({"op": "value", "target": True, "value": INPUT})
    elif action in WRITE_ACTIONS and target is not None:
        # an insert or a range edit keeps the rest of the text
        changes.append({"op": "value", "target": True, "contains": INPUT})
    elif action in TOGGLE_ACTIONS and target is not None:
        changes.append({"op": "changed", "target": True})
    seen_req = set()
    for op, el in _entries(diff):
        if is_noise(el) or (target is not None and _is_target(el, target) and changes):
            continue
        role = getattr(el, "role", None) or ""
        if role in WINDOW_ROLES and op in ("added", "removed") and (role, op) not in seen_req:
            seen_req.add((role, op))
            changes.append({"op": op, "role": role})          # by role: titles carry values
            continue
        m = _identity(el) if role in ACTIONABLE_ROLES or role in WINDOW_ROLES else {"role": role}
        m["op"] = "any"
        if m not in permitted:
            permitted.append(m)
    out: Dict[str, Any] = {"changes": changes, "permitted": permitted, "only": True,
                           "source": "observed"}
    if state_after:
        out["state"] = state_after
    return out


def merge(base: Optional[Dict[str, Any]], extra: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``base`` with ``extra``'s clauses added (base wins on ``only``/``state``)."""
    if not base:
        return dict(extra) if extra else None
    if not extra:
        return dict(base)
    out = dict(base)
    out["changes"] = list(base.get("changes") or []) + [m for m in extra.get("changes") or []
                                                          if m not in (base.get("changes") or [])]
    out["permitted"] = list(base.get("permitted") or []) + [m for m in extra.get("permitted") or []
                                                              if m not in (base.get("permitted") or [])]
    if "state" not in out and extra.get("state"):
        out["state"] = extra["state"]
    return out


def labels(matchers: Iterable[Dict[str, Any]]) -> List[str]:
    return [str(m.get("name") or m.get("identifier") or m.get("role") or "") for m in matchers]
