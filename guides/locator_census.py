"""Locator census — the MEASURED chrome-vs-content ledger.

THE RULE (architect's, implemented here, not relitigated):

    A locator whose text is STRUCTURAL (app chrome) travels between tenants.
    A locator whose text is CONTENT (a record value) does not.

    An OCR anchor on "Mueller, Anna" is worthless to another tenant — that string
    is never on their screen — AND it is a leak. Excluding content-derived
    locators costs no capability. "Speichern", "Buchen", field labels: those are
    what transfers.

HOW THE DECISION IS MADE — MEASURED, NEVER GUESSED FROM STRING SHAPE
--------------------------------------------------------------------
No regex over the text. No "looks like a name" heuristic. The census records,
per structural SLOT, which texts were observed there and in how many DISTINCT
sessions, and answers two questions from that record alone:

    variance  — did this slot ever carry a DIFFERENT text?      → CONTENT
    recurrence— did this exact text recur across >= N sessions?  → CHROME

A slot is a structural address inside one UI state, not a string:

    ax_title / ax_identifier   "AXButton#2"      the 3rd AXButton, in tree order
    ocr                        "region:12,4,10,5" the quantized screen region
    intent                     "edge->{to_ref}"   the graph edge

WHY THE COUNTER IS NEW
----------------------
Nothing in anyuse counted locator recurrence before this module. The fields that
look like they do are dead on the guide path:
``UIElement.times_seen`` and ``UIState.times_seen`` are written as the literal 1
(``memory/recorder.py``, ``guides/generator.py``) and merged with ``max()`` /
append-only (``guides/state_graph.py`` add_state), so they never exceed 1.
``AppObservations.landmarks`` counts LLM-written prose, not locators, and its
counts are dropped at the guide boundary. ``AbstractionRecord.seen_count`` is a
real cross-session counter but keys on (kind, title, primary_app), not on a
locator. So rather than guess from string shape, this module ADDS the counter.
It has to be fed (``observe_state_graph`` at session end) before anything can be
classified as chrome — see the UNSURE default below.

THE CONSERVATIVE DEFAULT
------------------------
Three verdicts, and only one of them travels:

    CHROME   measured: recurred unchanged across >= min_sessions sessions, and
             the slot has never carried a different text.
    CONTENT  measured: the slot varies — it has carried more than one text.
    UNSURE   not measured: slot never observed, text never observed there, or
             seen in fewer than min_sessions sessions.

UNSURE does NOT travel. Under-sharing is recoverable by observing more sessions;
a leaked customer name in a shared map is not.

Storage lives at ``~/.anyuse/locator_census.json`` — deliberately NOT inside
``~/.anyuse/app_guides/``, because ``GuideStorage._load_all_guides`` globs
``*.json`` there and skips only ``index.json``, so any extra file in that
directory is parsed as a guide and warns on every startup (``observations.json``
already trips this).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from anyuse.paths import anyuse_data_path

logger = logging.getLogger(__name__)

# ── verdicts ────────────────────────────────────────────────────────────────
CHROME = "chrome"
CONTENT = "content"
UNSURE = "unsure"

#: A locator must recur unchanged across at least this many DISTINCT sessions
#: before it is chrome. 1 is not enough: a single session proves nothing about
#: whether the slot varies per record.
DEFAULT_MIN_SESSIONS = 2

# ── slot kinds ──────────────────────────────────────────────────────────────
KIND_AX_TITLE = "ax_title"
KIND_AX_IDENTIFIER = "ax_identifier"
KIND_OCR = "ocr"
KIND_INTENT = "intent"

#: Slot kinds where exactly ONE text is expected at the slot, so a second text
#: at the same slot IS the measurement of "varies per record". An edge may
#: legitimately carry several distinct intents, so KIND_INTENT is not exclusive
#: and is decided by recurrence alone.
_EXCLUSIVE_KINDS = frozenset({KIND_AX_TITLE, KIND_AX_IDENTIFIER, KIND_OCR})

#: Screen regions are quantized before becoming a slot address so that a 1-pixel
#: layout jitter does not mint a fresh, unmeasured slot every session.
_OCR_REGION_GRID = 8

_CENSUS_VERSION = 1

SlotKey = Tuple[str, str, str, str]  # (app, state_ref, kind, slot)


@dataclass(frozen=True)
class Verdict:
    """The measured answer for one locator, with the evidence behind it.

    ``reason`` never embeds the locator text — a verdict is safe to ship in a
    projection's diagnostics.
    """
    verdict: str
    reason: str
    sessions: int = 0
    variants: int = 0

    @property
    def travels(self) -> bool:
        return self.verdict == CHROME

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "sessions": self.sessions,
            "variants": self.variants,
        }


# ── slot addressing ─────────────────────────────────────────────────────────
def _norm_app(app: Any) -> str:
    return str(app or "").strip().lower()


def _norm_text(text: Any) -> str:
    """Exact text, whitespace-trimmed. NOT casefolded: 'recurs unchanged' means
    unchanged, and a case flip is a change worth noticing."""
    return str(text or "").strip()


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a Mapping or an object (UIElement, AX node, ...)."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def element_slots(elements: Sequence[Any]) -> List[str]:
    """Structural slot address for each element, in list order.

    ``"AXButton#2"`` = the third AXButton in this state's element list. The same
    function must address a slot at observe time and at classify time, which is
    why both the census and the projection call it rather than re-deriving.
    """
    seen: Dict[str, int] = {}
    slots: List[str] = []
    for el in elements:
        role = str(_attr(el, "role", "") or "unknown")
        idx = seen.get(role, 0)
        seen[role] = idx + 1
        slots.append(f"{role}#{idx}")
    return slots


def ocr_slot(region: Any) -> Optional[str]:
    """Quantized region address for an OCR anchor, or None when it has no region.

    An OCR anchor with NO region is a full-screen text search: it has no
    structural address, so the census cannot measure whether that position
    varies per record, and the anchor can never be proven chrome. Callers get
    None and must treat the anchor as UNSURE (it does not travel). Producers of
    OCR anchors should record a region.
    """
    if not region:
        return None
    try:
        vals = list(region)[:4]
        if len(vals) < 4:
            return None
        x, y, w, h = (int(v) for v in vals)
    except (TypeError, ValueError):
        return None
    q = _OCR_REGION_GRID
    return f"region:{x // q},{y // q},{w // q},{h // q}"


def state_ref_of(state: Any) -> str:
    """The cross-tenant identity of a UI state: its role-only structural hash.

    NOT ``state_id`` — the two id schemes in this codebase disagree (the recorder
    hashes roles only, the generator hashes role+title), and a role+title digest
    is minted fresh per data record. The structural hash is role-frequency only
    and is the same on every tenant's screen.
    """
    fp = _attr(state, "fingerprint", None)
    return str(_attr(fp, "structural_hash", "") or "")


# ── the ledger ──────────────────────────────────────────────────────────────
@dataclass
class LocatorCensus:
    """Recurrence ledger over locator slots. The ONLY input to chrome-vs-content.

    Keyed (app, state_ref, kind, slot) -> {text: [session_id, ...]}. Session ids
    are stored, not just a count, so replaying one session twice cannot inflate a
    locator into chrome.
    """

    slots: Dict[SlotKey, Dict[str, List[str]]] = field(default_factory=dict)

    # ── ingest ──────────────────────────────────────────────────────────
    def observe(
        self,
        *,
        app: str,
        state_ref: str,
        kind: str,
        slot: Optional[str],
        text: Any,
        session_id: str,
    ) -> None:
        """Record one sighting. No-op for an unaddressable slot or empty text."""
        text = _norm_text(text)
        session_id = str(session_id or "").strip()
        if not text or not slot or not session_id or not state_ref:
            return
        key: SlotKey = (_norm_app(app), str(state_ref), str(kind), str(slot))
        texts = self.slots.setdefault(key, {})
        sessions = texts.setdefault(text, [])
        if session_id not in sessions:
            sessions.append(session_id)

    def observe_elements(
        self, *, app: str, state_ref: str, session_id: str, elements: Sequence[Any]
    ) -> None:
        """Observe an ordered element list (UIElements, AX dicts, or
        fingerprint ``required_elements`` entries) under its structural slots."""
        for el, slot in zip(elements, element_slots(elements)):
            self.observe(app=app, state_ref=state_ref, kind=KIND_AX_TITLE,
                         slot=slot, text=_attr(el, "title", ""),
                         session_id=session_id)
            self.observe(app=app, state_ref=state_ref, kind=KIND_AX_IDENTIFIER,
                         slot=slot, text=_attr(el, "identifier", ""),
                         session_id=session_id)

    def observe_ocr_anchors(
        self, *, app: str, state_ref: str, session_id: str, anchors: Sequence[Any]
    ) -> None:
        """Observe OCR anchors. Region-less anchors are skipped — unaddressable."""
        for anchor in anchors:
            slot = ocr_slot(_attr(anchor, "region", None))
            self.observe(app=app, state_ref=state_ref, kind=KIND_OCR, slot=slot,
                         text=_attr(anchor, "text", ""), session_id=session_id)

    def observe_intent(
        self, *, app: str, from_ref: str, to_ref: str, intent: Any, session_id: str
    ) -> None:
        self.observe(app=app, state_ref=from_ref, kind=KIND_INTENT,
                     slot=f"edge->{to_ref}", text=intent, session_id=session_id)

    def observe_ui_state(
        self, state: Any, *, session_id: str, ocr_anchors: Sequence[Any] = ()
    ) -> None:
        """Observe one UIState: its elements, its fingerprint's required_elements,
        and any OCR anchors captured on it.

        Both element lists land in the same slot space on purpose. If the two
        producers disagree about ordinals, the slot genuinely varies and the
        locator is measured as CONTENT — which is the conservative answer, and
        the honest one: an ordinal nobody agrees on is not a locator.
        """
        state_ref = state_ref_of(state)
        if not state_ref:
            return
        app = _attr(state, "app", "") or ""
        self.observe_elements(app=app, state_ref=state_ref, session_id=session_id,
                              elements=list(_attr(state, "elements", []) or []))
        fp = _attr(state, "fingerprint", None)
        required = list(_attr(fp, "required_elements", []) or []) if fp else []
        self.observe_elements(app=app, state_ref=state_ref, session_id=session_id,
                              elements=required)
        if ocr_anchors:
            self.observe_ocr_anchors(app=app, state_ref=state_ref,
                                     session_id=session_id, anchors=ocr_anchors)

    def observe_state_graph(
        self,
        graph: Any,
        *,
        session_id: str,
        ocr_anchors: Optional[Mapping[str, Sequence[Any]]] = None,
    ) -> None:
        """One-call ingest for a whole session: every state and every edge.

        THIS is the wiring point. Call it once at session end with the session's
        StateGraph; until it has been called for at least ``min_sessions``
        distinct sessions, every locator is UNSURE and the projection is empty.

        ``ocr_anchors`` maps the LOCAL ``state_id`` (that is what a capture has
        on hand) to the anchors seen on that state.
        """
        states = dict(_attr(graph, "states", {}) or {})
        anchors_by_state = dict(ocr_anchors or {})
        ref_by_id: Dict[str, str] = {}
        for sid, state in states.items():
            ref = state_ref_of(state)
            if ref:
                ref_by_id[sid] = ref
            self.observe_ui_state(state, session_id=session_id,
                                  ocr_anchors=anchors_by_state.get(sid, ()))
        for t in list(_attr(graph, "transitions", []) or []):
            from_ref = ref_by_id.get(str(_attr(t, "from_state", "")), "")
            to_ref = ref_by_id.get(str(_attr(t, "to_state", "")), "")
            if not from_ref or not to_ref:
                continue
            app = _attr(states.get(str(_attr(t, "from_state", ""))), "app", "") or ""
            self.observe_intent(app=app, from_ref=from_ref, to_ref=to_ref,
                                intent=_attr(t, "intent", ""), session_id=session_id)

    # ── the classifier ──────────────────────────────────────────────────
    def classify(
        self,
        *,
        app: str,
        state_ref: str,
        kind: str,
        slot: Optional[str],
        text: Any,
        min_sessions: int = DEFAULT_MIN_SESSIONS,
    ) -> Verdict:
        """Chrome, content, or unsure — decided from the ledger, nothing else."""
        text = _norm_text(text)
        if not text:
            return Verdict(UNSURE, "empty_text")
        if not slot:
            return Verdict(UNSURE, "no_structural_slot")
        if not state_ref:
            return Verdict(UNSURE, "no_state_ref")

        key: SlotKey = (_norm_app(app), str(state_ref), str(kind), str(slot))
        texts = self.slots.get(key)
        if not texts:
            return Verdict(UNSURE, "slot_never_measured")

        variants = len(texts)
        sessions = len(texts.get(text, ()))
        if not sessions:
            return Verdict(UNSURE, "text_never_observed_at_slot", 0, variants)

        # Variance beats recurrence: a slot that has carried more than one text
        # is a data slot, however often any single value repeated in it.
        if kind in _EXCLUSIVE_KINDS and variants > 1:
            return Verdict(CONTENT, f"slot_varies_per_record:{variants}_texts",
                           sessions, variants)

        if sessions < min_sessions:
            return Verdict(UNSURE, f"seen_in_{sessions}_of_min_{min_sessions}_sessions",
                           sessions, variants)

        return Verdict(CHROME, f"recurred_unchanged_in_{sessions}_sessions",
                       sessions, variants)

    # ── persistence ─────────────────────────────────────────────────────
    @staticmethod
    def default_path() -> Path:
        return anyuse_data_path("locator_census.json")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": _CENSUS_VERSION,
            "slots": [
                {"app": app, "state_ref": ref, "kind": kind, "slot": slot,
                 "texts": {t: list(s) for t, s in texts.items()}}
                for (app, ref, kind, slot), texts in sorted(self.slots.items())
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LocatorCensus":
        slots: Dict[SlotKey, Dict[str, List[str]]] = {}
        for row in (data.get("slots") or []):
            try:
                key: SlotKey = (
                    _norm_app(row.get("app")), str(row.get("state_ref") or ""),
                    str(row.get("kind") or ""), str(row.get("slot") or ""),
                )
                texts = {str(t): [str(s) for s in (v or [])]
                         for t, v in (row.get("texts") or {}).items()}
            except AttributeError:
                continue
            if key[1] and key[3] and texts:
                slots[key] = texts
        return cls(slots=slots)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "LocatorCensus":
        """Read the ledger. Total: any failure yields an EMPTY census, which
        classifies everything UNSURE and therefore projects nothing — the
        conservative direction to fail in."""
        p = Path(path) if path else cls.default_path()
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except FileNotFoundError:
            return cls()
        except Exception as exc:  # noqa: BLE001 - corrupt ledger must not crash a capture
            logger.warning("locator census unreadable at %s (%s) - starting empty", p, exc)
            return cls()

    def save(self, path: Optional[Path] = None) -> Path:
        """Write the ledger atomically. Raises on real IO failure — a silently
        lost census would silently stop every future projection."""
        p = Path(path) if path else self.default_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, p)
        return p
