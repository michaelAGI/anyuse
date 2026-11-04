"""Human-in-the-loop (HIL) pause-before-destructive gate for the drive path.

This module is the finer-grained safety layer that sits *under* the binary
master gate. AnyUse already has two coarse safeguards on the drive seam:

  * the keystone ``ANYUSE_DRIVE_ENABLED`` switch (default OFF — nothing
    actuates), and
  * a domain/app allow-block boundary policy
    (``anyuse/core/restriction.py``: ``RestrictionPolicy`` / ``ComplianceChecker``).

Neither answers the question "is THIS step destructive, and should a human
approve it before it actuates?" The boundary checker is allow/deny on *where*
navigation goes, not destructive-vs-readonly; ``PlanExecutor`` checkpoints are
*post*-step verification, not a *pre*-actuation hold. This module adds:

  1. :func:`classify_step` — a read-only / side-effecting / destructive
     classifier reusing ``ComplianceChecker._extract`` and the action-name sets
     in ``restriction.py``.
  2. :class:`HILGate` — a pause-before-actuation hold. It only ever *decides*
     (returns an :class:`ApprovalDecision`); it NEVER touches
     pyautogui/AX/screencapture/osascript. The caller actuates only when
     ``decision.actuate`` is True.

Design constraints (additive + dormant by default)
---------------------------------------------------
* **Gate-OFF contract preserved absolutely.** :meth:`HILGate.require_approval`
  returns ``actuate=False`` the instant ``drive_enabled`` is False and NEVER
  calls the approver or any OS layer in that case. ``ANYUSE_DRIVE_ENABLED``
  stays the master switch (default OFF, unchanged).
* **Default-dormant.** ``ANYUSE_HIL_MODE`` defaults to ``"off"`` so even with
  the master gate ON, behavior is byte-for-byte unchanged until an operator
  opts in — additive, not a behavior change.
* **Fail-closed.** An unrecognized action under an active HIL gate classifies
  as ``destructive`` (matching ``restriction.py``'s fail-closed idiom), and a
  destructive step with no approver injected RAISES :class:`ApprovalRequired`
  rather than silently actuating.
* **Zero actuation.** This module imports no pyautogui/Quartz/screencapture/
  osascript and performs no I/O. Importing it actuates nothing.

Intended wire-points (a LATER, separately-authorized edit — NOT in this build)
------------------------------------------------------------------------------
This module is wired-by-injection so it cannot alter existing behavior until
opt-in, through injectable ``engine_runner`` / ``drive_enabled``. The seam a
future change would
consult :meth:`HILGate.require_approval` from, gated by ``drive_enabled``:

  * ``anyuse/execution/executor.py`` — inside ``StateGraphExecutor._execute_step``
    (the modality dispatch is at executor.py:345-353), BEFORE the
    ``modality_map`` dispatch, so a destructive step is held before AX/JS/
    AppleScript/coordinate actuation.

With ``ANYUSE_HIL_MODE`` unset every decision is ``actuate=(drive_enabled)``,
so importing/instantiating this module changes nothing until both the master
gate AND a HIL mode are turned on.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from anyuse.core.restriction import (
    _APP_ACTIONS,
    _BENIGN_ACTIONS,
    _NAV_ACTIONS,
    _SCRIPTING_ACTIONS,
    ComplianceChecker,
)


# ---------------------------------------------------------------------------
# Step classes + result carriers (lightweight dataclass idiom, mirroring
# intervention.InterventionResult)
# ---------------------------------------------------------------------------

class StepClass:
    """String constants for the destructiveness classification of a step."""

    READ_ONLY = "read_only"
    SIDE_EFFECTING = "side_effecting"
    DESTRUCTIVE = "destructive"


# Ordinal severity so a HIL mode can express a *threshold*: a step actuates
# freely while its class ranks below the configured mode.
_CLASS_RANK = {
    StepClass.READ_ONLY: 0,
    StepClass.SIDE_EFFECTING: 1,
    StepClass.DESTRUCTIVE: 2,
}

# HIL modes, ranked the same way. 'off' is the dormant default (no holds).
HIL_OFF = "off"
HIL_DESTRUCTIVE = "destructive"
HIL_ALL = "all"

# The minimum step-rank a mode holds at. 'destructive' holds only destructive
# steps; 'all' holds anything that isn't read-only (side-effecting + above).
_MODE_THRESHOLD = {
    HIL_DESTRUCTIVE: _CLASS_RANK[StepClass.DESTRUCTIVE],
    HIL_ALL: _CLASS_RANK[StepClass.SIDE_EFFECTING],
}


@dataclass(frozen=True)
class ApprovalDecision:
    """The decision returned by :meth:`HILGate.require_approval`.

    ``actuate`` is the ONLY field a caller may act on: actuate the step iff it
    is True. ``reason`` is a human-readable explanation; ``step_class`` is the
    :class:`StepClass` the step was labelled with (``""`` when the gate is OFF
    and the step was never classified).
    """

    actuate: bool
    reason: str
    step_class: str = ""


class ApprovalRequired(Exception):
    """Raised when a step needs human approval but no approver was injected.

    This is the fail-closed default: a destructive (or, under mode ``all``, a
    side-effecting) step CANNOT silently actuate when the gate is ON and no
    approver is configured. The caller must inject an ``approver`` to obtain an
    explicit decision, or treat the raise as a hard stop.
    """

    def __init__(self, step_class: str, message: str = ""):
        self.step_class = step_class
        super().__init__(
            message
            or (
                f"HIL approval required for a {step_class} step but no approver "
                "is configured (fail-closed: refusing to actuate)"
            )
        )


# ---------------------------------------------------------------------------
# Destructive keyword set
# ---------------------------------------------------------------------------

# Curated keywords that mark an action destructive/irreversible when they appear
# in the action NAME or in any embedded string arg. Matched as whole words
# (token boundaries) so "address" does not trip "drop"/"erase" etc. — but a
# substring like "Delete account" on a click label DOES match "delete".
_DESTRUCTIVE_KEYWORDS = (
    "delete",
    "remove",
    "trash",
    "empty-trash",
    "empty_trash",
    "purchase",
    "buy",
    "pay",
    "checkout",
    "submit",
    "send",
    "transfer",
    "confirm",
    "overwrite",
    "format",
    "erase",
    "drop",
    "wipe",
    "uninstall",
)

# The same verbs as a user of a German macOS sees them. Measured in the guest VM
# (macOS 26.6.2, de_DE): Finder's "Move to Trash" is the menu item
# "In den Papierkorb legen", and none of the English keywords above appears in
# it, so under an active HIL gate the step classified as SIDE-EFFECTING and
# would have actuated. A destructive verb the gate cannot read is a gate that
# is off. Substring matching means "löschen" also covers "Löschen …" and
# "Alle löschen"; over-holding is the safe direction.
_DESTRUCTIVE_KEYWORDS_DE = (
    "löschen", "loeschen", "papierkorb", "entfernen", "senden", "kaufen",
    "bezahlen", "überschreiben", "ueberschreiben", "formatieren", "deinstallieren",
    "leeren", "übertragen", "bestätigen",
)

#: What the gate consults by default: the English verbs plus the German ones.
DESTRUCTIVE_KEYWORDS_ALL = _DESTRUCTIVE_KEYWORDS + _DESTRUCTIVE_KEYWORDS_DE

# Read-only subset of restriction._BENIGN_ACTIONS: the benign actions that do
# NOT mutate state. screenshot/scroll/move/hover/wait/noop are pure perception
# or inert motion. click/type/key/drag are EXCLUDED here — they are benign for
# the boundary (no nav/app target) but CAN cause a side effect (clicking a
# Delete button, typing into a field), so they are only treated as read-only
# when not flagged destructive by keyword below.
_READ_ONLY_ACTIONS = frozenset(
    {"scroll", "scroll_at", "move", "move_mouse", "wait", "screenshot", "noop"}
    # 'hover'/'read' are not in restriction._BENIGN_ACTIONS but are read-only by
    # nature; include them so a perception-only step classifies cleanly.
    | {"hover", "read"}
)

# The mutating-but-benign-for-boundary actions: click/type/key/drag. These are
# in restriction._BENIGN_ACTIONS (no nav/app target) but are potentially
# side-effecting because they can drive a UI mutation. They classify as
# side_effecting unless a destructive keyword promotes them to destructive.
_POTENTIALLY_SIDE_EFFECTING = frozenset(_BENIGN_ACTIONS) - _READ_ONLY_ACTIONS


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def _matches_destructive(name: str, args: Dict[str, Any],
                         keywords: Tuple[str, ...] = DESTRUCTIVE_KEYWORDS_ALL) -> bool:
    """True if a destructive keyword appears in the action name or any embedded
    string arg.

    Reuses ``ComplianceChecker._iter_strings`` to walk nested payloads (a click
    label buried in args, a list of strings, a script body) exactly as the
    boundary checker's embedded-target scan does. Matching is a lowercase
    substring test against the curated keyword set: a label "Delete account"
    matches "delete"; an action named "submit_form" matches "submit".
    """
    hay = [name]
    for s in ComplianceChecker._iter_strings(args):
        hay.append(s.lower())
    blob = " ".join(hay)
    return any(kw in blob for kw in keywords)


def classify_step(action: Any, keywords: Tuple[str, ...] = DESTRUCTIVE_KEYWORDS_ALL) -> str:
    """Classify an action as read-only / side-effecting / destructive.

    Returns one of :data:`StepClass.READ_ONLY`, :data:`StepClass.SIDE_EFFECTING`,
    :data:`StepClass.DESTRUCTIVE`.

    Reuses ``ComplianceChecker._extract`` to pull ``(name, args)`` out of a
    loosely-typed action (dict ``{"action","args"}``, flat dict, or an object
    with ``.action``/``.args``) — the same shape the boundary checker accepts.

    Rules (most-severe wins):

    * **destructive** — a curated destructive keyword
      (delete/remove/trash/purchase/buy/pay/checkout/submit/send/transfer/
      confirm/overwrite/format/erase/drop/wipe/uninstall/empty-trash) appears in
      the action name OR any embedded string arg. Also the fail-closed bucket:
      an UNRECOGNIZED action classifies destructive.
    * **side_effecting** — a navigation / app-launch / scripting action
      (``restriction._NAV_ACTIONS`` / ``_APP_ACTIONS`` / ``_SCRIPTING_ACTIONS``),
      or a mutating-but-benign action (click/type/key/drag) with no destructive
      keyword.
    * **read_only** — screenshot/scroll/move/hover/wait/noop/read: pure
      perception or inert motion, no destructive keyword.

    NOTE: this only DECIDES; it touches no OS layer.
    """
    name, args = ComplianceChecker._extract(action)

    # Destructive keyword anywhere → most severe, wins outright.
    if _matches_destructive(name, args, keywords):
        return StepClass.DESTRUCTIVE

    # Pure read-only perception/motion.
    if name in _READ_ONLY_ACTIONS:
        return StepClass.READ_ONLY

    # Navigation / app-launch / scripting → side-effecting by nature.
    if name in _NAV_ACTIONS or name in _APP_ACTIONS or name in _SCRIPTING_ACTIONS:
        return StepClass.SIDE_EFFECTING

    # Mutating-but-benign-for-boundary (click/type/key/drag): can drive a UI
    # mutation but is not, on its own, destructive.
    if name in _POTENTIALLY_SIDE_EFFECTING:
        return StepClass.SIDE_EFFECTING

    # Unrecognized action under an active HIL gate → FAIL CLOSED as destructive
    # (mirrors restriction.py: an un-vettable action is denied, not waved
    # through). The caller only ever asks to classify when the gate is being
    # consulted, so an unknown step is held for approval rather than actuated.
    return StepClass.DESTRUCTIVE


# ---------------------------------------------------------------------------
# HILGate — the pause-before-destructive hold
# ---------------------------------------------------------------------------

def _read_mode(env_value: Optional[str]) -> str:
    """Normalize a HIL mode token; default OFF (dormant) for anything unknown.

    Unlike ``restriction.from_boundary`` (which fails CLOSED on an unrecognized
    boundary mode), an unknown HIL mode falls back to ``off`` — because the HIL
    gate is a layer ON TOP of the already-fail-closed master gate, and the
    operator-facing contract is "default-dormant; opt in explicitly". A typo'd
    ``ANYUSE_HIL_MODE`` must not silently start blocking the (already
    master-gated) drive path; it leaves behavior unchanged.
    """
    mode = (env_value or "").strip().lower()
    if mode in (HIL_OFF, HIL_DESTRUCTIVE, HIL_ALL):
        return mode
    return HIL_OFF


class HILGate:
    """Pause-before-actuation hold consulted on the drive seam.

    The gate is meaningful ONLY when the master ``drive_enabled`` is True. With
    it False, :meth:`require_approval` returns ``actuate=False`` immediately and
    NEVER consults the approver or any OS layer — gate-OFF means nothing runs,
    so there is nothing to approve.

    Args:
        mode: ``'off'`` (default — no holds, unchanged behavior) | ``'destructive'``
            (hold destructive steps) | ``'all'`` (hold anything not read-only).
            When ``None`` it is read from ``ANYUSE_HIL_MODE`` (default ``'off'``).
        approver: optional async callable ``approver(context: dict) -> bool``.
            When a step meets/exceeds the mode threshold the gate awaits it and
            actuates only on an explicit ``True``. When omitted, a held step
            RAISES :class:`ApprovalRequired` (fail-closed default).
    """

    def __init__(
        self,
        mode: Optional[str] = None,
        approver: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
    ):
        from anyuse.settings import get as get_setting
        self.mode = _read_mode(get_setting("ANYUSE_HIL_MODE", mode))
        self.approver = approver

    def _holds(self, step_class: str) -> bool:
        """True if the configured mode holds a step of ``step_class``."""
        threshold = _MODE_THRESHOLD.get(self.mode)
        if threshold is None:  # mode 'off' (or normalized to off)
            return False
        return _CLASS_RANK.get(step_class, _CLASS_RANK[StepClass.DESTRUCTIVE]) >= threshold

    async def require_approval(
        self, action: Any, *, drive_enabled: bool,
        keywords: Tuple[str, ...] = DESTRUCTIVE_KEYWORDS_ALL,
    ) -> ApprovalDecision:
        """Decide whether ``action`` may actuate, holding for approval if needed.

        Decision order (each branch is intentional — see module docstring):

        1. ``drive_enabled is False`` → ``actuate=False`` IMMEDIATELY, approver
           NEVER called. Preserves the master ANYUSE_DRIVE_ENABLED contract:
           gate-OFF means nothing runs, so there is nothing to approve.
        2. gate ON, mode ``'off'`` → ``actuate=True`` (no HIL configured;
           unchanged behavior — the step is still master-gated upstream).
        3. gate ON, step class below the mode threshold (e.g. read_only under
           ``'destructive'``) → ``actuate=True`` (no approval needed).
        4. gate ON, class meets/exceeds threshold → BLOCK:
           * no approver → raise :class:`ApprovalRequired` (fail-closed: a
             destructive step CANNOT silently actuate).
           * approver present → ``await approver(context)`` and actuate ONLY on
             an explicit ``True`` (anything else → ``actuate=False``, denied).

        This method NEVER touches pyautogui/AX/screencapture/osascript — it only
        DECIDES. It is the caller's contract to actuate iff ``actuate`` is True.
        """
        # (1) Master gate OFF → nothing to approve, nothing actuates. Short-circuit
        # BEFORE classifying or consulting the approver.
        if not drive_enabled:
            return ApprovalDecision(
                actuate=False, reason="gate OFF: no actuation", step_class=""
            )

        # (2) Gate ON but HIL not configured → unchanged behavior.
        if self.mode == HIL_OFF:
            return ApprovalDecision(
                actuate=True, reason="HIL mode off: no hold", step_class=""
            )

        step_class = classify_step(action, keywords)

        # (3) Below the mode threshold → actuate freely.
        if not self._holds(step_class):
            return ApprovalDecision(
                actuate=True,
                reason=f"{step_class} step below HIL threshold ({self.mode})",
                step_class=step_class,
            )

        # (4) At/above threshold → HOLD.
        if self.approver is None:
            # Fail-closed default: refuse to actuate a held step with no human.
            raise ApprovalRequired(step_class)

        context = self._context(action, step_class)
        approved = await self.approver(context)
        if approved is True:
            return ApprovalDecision(
                actuate=True,
                reason=f"approved by human ({step_class})",
                step_class=step_class,
            )
        return ApprovalDecision(
            actuate=False,
            reason=f"approval denied ({step_class})",
            step_class=step_class,
        )

    @staticmethod
    def _context(action: Any, step_class: str) -> Dict[str, Any]:
        """Build the read-only context dict handed to an injected approver.

        Carries the classification + the extracted (name, args) so a human/UI
        can render what is about to actuate. Contains no live handles — it is
        pure data, safe to serialize.
        """
        name, args = ComplianceChecker._extract(action)
        return {
            "step_class": step_class,
            "action": name,
            "args": args,
            "raw": action,
        }
