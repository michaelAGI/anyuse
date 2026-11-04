"""
Drift Gate — deterministic-replay vs vision-fallback decision (Wave-6).

The token-saver loop for visual self-healing. During replay the executor knows
which UI state each deterministic step expects to start from (the recorded
baseline). This module compares the LIVE accessibility elements against that
recorded baseline fingerprint and decides ONE thing:

    "Has the UI actually drifted away from what we recorded?"

If NOT drifted -> the executor keeps the cheap planned modality (ax/js/coordinate)
and never touches vision. This is the whole point: deterministic replay stays
free. ONLY when a real baseline mismatch is detected (a required anchor missing,
the interactive-element count far outside the recorded range, or — for a
placeholder fingerprint we can't trust — no recorded evidence at all) does the
caller escalate that single step to the (already-gated, non-actuating) visual
tier.

This module is PURE analysis over already-captured AX element dicts:
    [{"role": ..., "title": ...}, ...]

It NEVER captures the screen, NEVER calls pyautogui / osascript / screencapture,
NEVER actuates, and NEVER reads or mutates ANYUSE_DRIVE_ENABLED or the
second-actuation guard of the drive seam. It builds on the EXISTING drift primitives
(UIFingerprint.matches / StateGraph.identify_state) and, mirroring their
"raw signals, no thresholds as the decision layer" style, returns the raw match
data alongside a conservative `drifted` flag so a future LLM caller can override.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from anyuse.guides.state_graph import StateGraph, UIState

# Documented structural-similarity floor. NOT used as the primary decision layer
# (required_missing drives the conservative flag); it is a secondary signal so a
# state whose anchors all happen to be present but whose overall role
# distribution has collapsed is still flagged. The raw similarity is always
# returned in `signals` so a future LLM caller can override this floor.
STRUCTURAL_SIMILARITY_FLOOR = 0.5

# A marker placed on a UIState by the self-healing placeholder path
# (self_healing._discover_new_states) when a state was discovered without any AX
# data to fingerprint. When present, an empty UIFingerprint is a KNOWN
# placeholder — not genuine drift evidence — and we take the safe path rather
# than emitting a bogus 0.0-similarity "drift".
FINGERPRINT_PENDING_NOTE = "fingerprint_pending"


@dataclass
class DriftReport:
    """
    The drift decision for one replay step.

    Conservative `drifted` flag + the raw match signals. The signals are kept so
    a future LLM caller can reason over them and override the heuristic flag.
    """

    drifted: bool = False
    reason: str = ""
    # One of: "" (no drift) | "required_missing" | "count_out_of_range"
    #         | "low_structural_similarity" | "no_recorded_fingerprint"

    matched_state_id: Optional[str] = None
    # The state_id whose fingerprint was evaluated (when expected_state_id is
    # known), or the best-scoring state from identify_state() otherwise.

    signals: Dict[str, Any] = field(default_factory=dict)
    # Raw match data from UIFingerprint.matches() (required_found/missing,
    # count_in_range, structural_similarity, new_elements), plus any extra
    # context the caller may want (e.g. all_matches when no expected state).

    def to_dict(self) -> dict:
        return {
            "drifted": self.drifted,
            "reason": self.reason,
            "matched_state_id": self.matched_state_id,
            "signals": self.signals,
        }


logger = logging.getLogger(__name__)

_TOLERANCE_ENV = "ANYUSE_DRIFT_MISSING_TOLERANCE"


def _env_tolerance() -> float:
    """Fraction of required anchors that may be missing before drift is declared.

    Default 0.05. The anchor study's fire-rate
    table (docs/COMPILE.md) measured it on WEB pages; measured again on
    recorded AX worlds, 0.02 and 0.05 each added
    one wrong action (a namesake gap in the resolver), and after the resolver
    fixes the same rule chose 0.05: no wrong action added,
    85 right cases refused instead of 502 at 0.0.
    The fraction is of the REQUIRED anchors, so fewer than 20 anchors tolerate
    none missing.
    """
    from anyuse.settings import get as get_setting
    return float(get_setting(_TOLERANCE_ENV))


def _is_synthetic(state: Optional[UIState]) -> bool:
    """A baseline built, not recorded (``state_graph.SYNTHETIC_NOTE`` in its notes: the map's menu-group states)."""
    from anyuse.guides.state_graph import SYNTHETIC_NOTE
    return state is not None and any(str(n).startswith(SYNTHETIC_NOTE) for n in (state.custom_render_notes or []))


def _fingerprint_is_placeholder(state: Optional[UIState]) -> bool:
    """
    True when this state's fingerprint carries no recorded evidence — i.e. the
    self-healing placeholder: no required_elements AND an empty structural_hash.

    Such a fingerprint cannot be trusted to distinguish "current UI" from "drift"
    (every comparison yields 0.0 structural similarity and a default count range),
    so the gate must NOT treat it as genuine drift evidence.
    """
    if state is None:
        return True
    fp = state.fingerprint
    if fp is None:
        return True
    no_required = not fp.required_elements
    no_hash = not (fp.structural_hash or "").strip()
    return no_required and no_hash


def _classify(match: Dict[str, Any],
              missing_tolerance: Optional[float] = None) -> tuple[bool, str]:
    """
    Derive a conservative (drifted, reason) from one UIFingerprint.matches()
    result. Mirrors the existing "no thresholds as the DECISION layer" style:
    required_missing being non-empty is the primary, hard signal; the structural
    floor is a secondary safety net. The raw signals stay intact for an LLM
    override upstream.
    """
    required_missing = match.get("required_missing") or []
    if required_missing:
        # TOLERANCE (default 0.05). With no
        # tolerance, ONE absent anchor declares drift for the whole step: the
        # anchor study measured 0.2 % of elements disappearing flipping 34.6 % of
        # UNCHANGED screens to "drift", 1 % flipping 64.7 %, 5 % flipping 96.1 %
        # — every one of those escalating to the expensive visual tier. The
        # fraction is of the anchors that were REQUIRED, so a screen with one
        # anchor cannot be tolerated into silence.
        tolerance = missing_tolerance if missing_tolerance is not None else _env_tolerance()
        required_total = len(match.get("required_found") or []) + len(required_missing)
        tolerated = (tolerance > 0.0 and required_total
                     and (len(required_missing) / required_total) <= tolerance)
        if not tolerated:
            return True, "required_missing"
        # Tolerance applies ONLY to the missing-anchor signal. Count and
        # structural checks below still run; tolerating one signal is not a
        # blanket declaration that the state is healthy.

    if not match.get("count_in_range", True):
        return True, "count_out_of_range"

    similarity = match.get("structural_similarity", 1.0)
    if similarity < STRUCTURAL_SIMILARITY_FLOOR:
        return True, "low_structural_similarity"

    return False, ""


def _scope_count(match: Dict[str, Any], state: Optional[UIState], ax_elements: List[dict],
                 count_tag: Optional[str]) -> None:
    """Compare the element count of the step's TARGET part of the app (its window, or the
    menu it navigates) with that part's recorded count, ±20, instead of the whole app's. A second
    window of the app no longer fires the gate; the target window growing or shrinking by more
    than 20 elements still does. Only when the caller names the part (``count_tag``, a
    ``top_level_tag``) and the fingerprint recorded it; else the app-wide check stands. The
    app-wide verdict stays in the signals (``app_count_in_range``)."""
    fp = state.fingerprint if state is not None else None
    scoped = getattr(fp, "scoped_counts", None) or {}
    if not count_tag or not scoped:
        return
    key = "window" if count_tag.startswith("window:") else count_tag
    if key not in scoped:
        return
    recorded = int(scoped[key])
    live = sum(1 for el in ax_elements if el.get("tag") == count_tag)
    match["app_count_in_range"] = match.get("count_in_range", True)
    match["count_in_range"] = max(0, recorded - 20) <= live <= recorded + 20
    match["count_scope"] = {"part": key, "recorded": recorded, "live": live}


def evaluate_drift(
    state_graph: StateGraph,
    expected_state_id: Optional[str],
    ax_elements: List[dict],
    count_tag: Optional[str] = None,
) -> DriftReport:
    """
    Decide whether the live UI has drifted from the recorded baseline.

    Args:
        state_graph: The app's StateGraph (source of recorded fingerprints).
        expected_state_id: The state_id the replay step expects to start from
            (WorkflowIntent.expected_before_state). When falsy/unknown, falls
            back to StateGraph.identify_state() over all states and evaluates
            the best-scoring candidate.
        ax_elements: Already-captured live AX elements as
            [{"role": ..., "title": ...}, ...]. NOTHING is captured here.
        count_tag: optional, the top-level part of the app the step acts
            in (``AccessibilityTree.top_level_tag`` of its target: ``window:<k>``
            or ``menu:<title>``); the elements carry theirs as ``"tag"``. The
            ±20 count range then applies to that part (``_scope_count``).

    Returns:
        DriftReport with a conservative `drifted` flag, a `reason`, the
        `matched_state_id`, and the raw `signals` for a possible LLM override.

    Safe-path guarantees:
        - A placeholder fingerprint (no required_elements + empty
          structural_hash) yields reason="no_recorded_fingerprint",
          drifted=True — the SAFE path (can't trust a placeholder), NOT a
          false 0.0-similarity "drift".
        - PURE: no capture, no model, no actuation, no gate read/write.
    """
    ax_elements = ax_elements or []

    if expected_state_id:
        expected_state = state_graph.get_state(expected_state_id)

        # Placeholder / no recorded evidence -> safe path, not bogus drift.
        if _fingerprint_is_placeholder(expected_state):
            return DriftReport(
                drifted=True,
                reason="no_recorded_fingerprint",
                matched_state_id=expected_state_id,
                signals={
                    "placeholder": True,
                    "note": (
                        "expected state has no recorded fingerprint "
                        "(required_elements empty and structural_hash empty); "
                        "cannot trust for deterministic replay"
                    ),
                    "live_element_count": len(ax_elements),
                },
            )

        match = expected_state.fingerprint.matches(ax_elements)
        if _is_synthetic(expected_state):
            # a SYNTHETIC baseline (the map's state of a menu group: its probed
            # options as required elements) records no screen, so its element-count range and structural hash say
            # nothing about the live app (the whole Finder app is hundreds of elements, the synthetic range 8-48:
            # every map-compiled sort replay "drifted"). It is judged only on what it does claim: the probed options
            # are shown (the missing-anchor tolerance applies); the target's identity check stays the guard.
            match["synthetic"] = True
            match["skipped"] = {"count_in_range": match.get("count_in_range"),
                                "structural_similarity": match.get("structural_similarity")}
            match["count_in_range"], match["structural_similarity"] = True, 1.0
            drifted, reason = _classify(match)
            match["expected_state_id"] = expected_state_id
            return DriftReport(drifted=drifted, reason=reason, matched_state_id=expected_state_id, signals=match)
        _scope_count(match, expected_state, ax_elements, count_tag)
        drifted, reason = _classify(match)
        match["expected_state_id"] = expected_state_id
        return DriftReport(
            drifted=drifted,
            reason=reason,
            matched_state_id=expected_state_id,
            signals=match,
        )

    # No expected state known: use the EXISTING identify_state over all states.
    all_matches = state_graph.identify_state(ax_elements)
    if not all_matches:
        # No recorded states at all — nothing to compare against. Safe path:
        # treat as "no recorded fingerprint" so the caller escalates rather than
        # replaying blind against an empty graph.
        return DriftReport(
            drifted=True,
            reason="no_recorded_fingerprint",
            matched_state_id=None,
            signals={
                "placeholder": True,
                "note": "no recorded states in graph to compare against",
                "live_element_count": len(ax_elements),
            },
        )

    # Pick the best candidate by (fewest required_missing, highest structural
    # similarity, count_in_range). This is only a candidate selection, not the
    # drift decision — the chosen state is then classified the same way, and all
    # raw matches are returned in signals for an LLM override.
    def _score(item):
        _sid, m = item
        return (
            -len(m.get("required_missing") or []),
            m.get("structural_similarity", 0.0),
            1 if m.get("count_in_range", False) else 0,
        )

    best_id, best_match = max(all_matches.items(), key=_score)

    # If the best candidate is itself a placeholder fingerprint, take the safe
    # path rather than emitting a bogus 0.0-similarity drift against it.
    best_state = state_graph.get_state(best_id)
    if _fingerprint_is_placeholder(best_state):
        return DriftReport(
            drifted=True,
            reason="no_recorded_fingerprint",
            matched_state_id=best_id,
            signals={
                "placeholder": True,
                "note": (
                    "best-scoring state has no recorded fingerprint "
                    "(placeholder); cannot trust for deterministic replay"
                ),
                "all_matches": all_matches,
                "live_element_count": len(ax_elements),
            },
        )

    best_match = dict(best_match)
    _scope_count(best_match, best_state, ax_elements, count_tag)
    drifted, reason = _classify(best_match)
    best_match["all_matches"] = all_matches
    return DriftReport(
        drifted=drifted,
        reason=reason,
        matched_state_id=best_id,
        signals=best_match,
    )
