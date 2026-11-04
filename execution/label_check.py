"""Planned labels against the labels actually on screen.

Guest runs showed the failure: a planner names a menu item in English
("Save") on a German UI ("Sichern"). The step then found nothing, was treated as
a retryable miss, and went to the intervention model. A planned step with no
recorded identity names its target only by a LABEL, so that label is the one
thing that can be checked before acting: if no element on screen carries it,
the step stops with a reason that lists the nearest observed labels (for a
re-plan with the real label list). No translation tables, no guessing.

Pure functions; no AX call, no model, no I/O. ``observed_labels`` reads an
``AXSnapshot``, the compact ``AXSnapshot.to_json()`` form the planner prompt
carries (``r``/``t``/``d``/``v``/``id``), or a raw nested ``role``/``title`` dump.
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

#: the AX attributes that are a control's human label, in the planner's view
_KEYS_FULL = ("title", "ax_description", "description", "value", "identifier")
_KEYS_COMPACT = ("t", "d", "v", "id")


def _role_of(node: Dict[str, Any]) -> str:
    role = str(node.get("role") or node.get("r") or "")
    return role if role.startswith("AX") or not role else "AX" + role


def labelled_elements(ax: Any) -> List[Tuple[str, str]]:
    """``[(role, label), ...]`` for every label of every element in ``ax``."""
    out: List[Tuple[str, str]] = []
    if ax is None:
        return out
    if isinstance(ax, str):
        try:
            ax = json.loads(ax)
        except (TypeError, ValueError):
            return out
    elements = getattr(ax, "elements", None)
    if elements is not None and not isinstance(ax, (dict, list)):
        for e in elements:
            for key in ("title", "ax_description", "value", "identifier"):
                val = getattr(e, key, None)
                if val:
                    out.append((str(getattr(e, "role", "") or ""), str(val)))
        return out

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            role = _role_of(node)
            for key in _KEYS_FULL + _KEYS_COMPACT:
                val = node.get(key)
                if isinstance(val, str) and val:
                    out.append((role, val))
            for key, val in node.items():
                if isinstance(val, (dict, list)):
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(ax)
    return out


def observed_labels(ax: Any) -> Set[str]:
    return {label for _role, label in labelled_elements(ax)}


def nearest(label: str, labels: Iterable[str], n: int = 5) -> List[str]:
    pool = sorted({x for x in labels if x and len(x) <= 120})
    return difflib.get_close_matches(label, pool, n=n, cutoff=0.0)[:n] if label else []


def planned_label_problem(role: str, label: str, ax: Any) -> Optional[Dict[str, Any]]:
    """``None`` when an element on screen carries ``label``; otherwise why not.

    ``{"code": "label_mismatch", ...}`` when no element carries the label at
    all; ``{"code": "role_mismatch", ...}`` when it is on screen but on another
    role than the plan named."""
    if not label:
        return None
    pairs = labelled_elements(ax)
    if not pairs:
        return None                      # nothing observed: the caller has no evidence either way
    roles = sorted({r for r, lab in pairs if lab == label})
    if not roles:
        near = nearest(label, (lab for _r, lab in pairs))
        return {"code": "label_mismatch", "label": label, "near": near,
                "reason": (f"the planned label {label!r} is not on screen"
                           + (f"; the nearest observed labels are {near}" if near else ""))}
    if role and role not in roles:
        return {"code": "role_mismatch", "label": label, "roles": roles,
                "reason": f"the label {label!r} is on screen as {', '.join(roles)}, not as {role}"}
    return None


def plan_label_problems(steps: Iterable[Any], ax: Any) -> List[Dict[str, Any]]:
    """The problems of every planned ``ax`` step without a recorded identity,
    against ONE observed screen (a pre-flight view; the executor repeats the
    check against the live screen right before each step acts)."""
    out = []
    for i, step in enumerate(steps):
        cmd = getattr(step, "command", None) or {}
        if getattr(step, "modality", "") != "ax" or cmd.get("selector"):
            continue
        prob = planned_label_problem(str(cmd.get("role") or ""), str(cmd.get("title") or ""), ax)
        if prob is not None:
            out.append(dict(prob, step=i))
    return out
