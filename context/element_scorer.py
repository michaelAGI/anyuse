"""Weighted deterministic element matching — OPT-IN, default off.

``context/accessibility.py`` resolves a recorded element against a live tree with
an exact-match cascade: it either finds an exact hit or it does not, with no
score, no confidence and no margin. What that costs, measured on
2 036 held-out real-drift correspondences: cascade top-1 **0.794** end-to-end vs
**0.939** for a weighted scorer at 0.6 ms, and the cascade scores **0.000** on
the text-drift slice (a changed label makes an exact match impossible by
construction).

This module is that weighted scorer, plus the two things the cascade cannot
express — a **confidence** and a **top-two margin** — so a caller can abstain
instead of guessing. It sits BESIDE the cascade and is selected by config;
nothing changes unless somebody turns it on.

**About the shipped weights.** :data:`WEB_FITTED_WEIGHTS` is that measurement's fitted
vector. It was fitted on **web DOM** features. AX trees are flatter, noisier and
**have no class tokens at all**, and ``cls`` is a component the fitted vector
leans on. Treat these numbers as a documented prior, not as a result: **refit on
your own AX snapshots before trusting them** (``anyuse selfheal fit``). The
scorer renormalises over the components actually present, so a missing ``cls``
does not silently drag every score down — but renormalising is not refitting.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Weights fitted on web DOM pairs. A PRIOR for AX, not a result — see the
#: module docstring.
WEB_FITTED_WEIGHTS: Dict[str, float] = {
    "role": 0.18, "label": 0.34, "value": 0.06, "path": 0.12,
    "anc": 0.10, "sib": 0.08, "cls": 0.07, "pos": 0.05,
}
COMPONENTS = tuple(WEB_FITTED_WEIGHTS)

#: Weights refitted on real macOS AX pairs (360 labelled pairs from 6 stock
#: apps, id-withheld; holdout top-1 0.972, wrong-match 0.000). Used by the AX resolver when
#: ANYUSE_ELEMENT_MATCHER=weighted, unless ANYUSE_ELEMENT_WEIGHTS names a JSON
#: file written by `anyuse selfheal fit`.
AX_FITTED_WEIGHTS: Dict[str, float] = {
    "role": 0.213, "label": 0.288, "value": 0.019, "path": 0.153,
    "anc": 0.135, "sib": 0.037, "cls": 0.068, "pos": 0.087,
}
_WEIGHTS_ENV = "ANYUSE_ELEMENT_WEIGHTS"


def ax_weights() -> Dict[str, float]:
    """The weights the AX resolver uses in weighted mode."""
    path = os.environ.get(_WEIGHTS_ENV, "").strip()
    if path:
        try:
            import json
            with open(path) as fh:
                data = json.load(fh)
            return {k: float(data.get(k, 0.0)) for k in COMPONENTS}
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s=%r unreadable (%s); using the AX-fitted defaults", _WEIGHTS_ENV, path, exc)
    return dict(AX_FITTED_WEIGHTS)

#: A match must reach this score to be offered at all.
DEFAULT_MIN_SCORE = 0.55
#: ...and must beat the runner-up by this much, or the scorer abstains.
#: Measured: a model never helps at PICKING; the margin is what
#: separates "one candidate" from "two plausible candidates", and the honest
#: answer to the latter is to abstain, not to guess.
DEFAULT_MIN_MARGIN = 0.10

_MODE_ENV = "ANYUSE_ELEMENT_MATCHER"
_TOKENS = re.compile(r"[a-z0-9]+")


def matcher_mode(explicit: Optional[str] = None) -> str:
    """``"cascade"`` (the default, unchanged behaviour) or ``"weighted"``."""
    from anyuse.settings import get as get_setting
    mode = str(get_setting(_MODE_ENV, explicit)).strip().lower()
    return "weighted" if mode == "weighted" else "cascade"


# ---------------------------------------------------------------------------
# similarity components (the web matcher's, adapted to AX fields)
# ---------------------------------------------------------------------------

def _toks(s: str) -> set:
    return set(_TOKENS.findall((s or "").lower()))


def _seqsim(a: str, b: str) -> float:
    a, b = (a or "").lower(), (b or "").lower()
    if not a and not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b) if (a | b) else 0.0


def _canonical_role(role: str) -> str:
    r = (role or "").replace("AX", "").lower()
    if r in ("button", "menubutton", "popupbutton", "menuitem", "menubaritem"):
        return "button"
    if r in ("textfield", "textarea", "securetextfield", "searchfield", "combobox"):
        return "textfield"
    if r in ("checkbox", "radiobutton", "switch"):
        return "toggle"
    return r


def _path_suffix_sim(a: str, b: str) -> float:
    """Shared trailing path steps as a fraction of the shorter path."""
    pa = [x for x in (a or "").split("/") if x]
    pb = [x for x in (b or "").split("/") if x]
    if not pa or not pb:
        return 0.0
    n = 0
    for x, y in zip(reversed(pa), reversed(pb)):
        if x != y:
            break
        n += 1
    return n / min(len(pa), len(pb))


def components(target: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, float]:
    """Per-component similarity in [0, 1]. Deterministic; no model, no I/O."""
    t_role, c_role = target.get("role", ""), cand.get("role", "")
    role = 1.0 if t_role == c_role else (
        0.4 if _canonical_role(t_role) == _canonical_role(c_role) else 0.0)
    t_lab = target.get("label") or target.get("title") or ""
    c_lab = cand.get("label") or cand.get("title") or ""
    label = max(_seqsim(t_lab, c_lab), _jaccard(_toks(t_lab), _toks(c_lab)))
    value = _seqsim(str(target.get("value", ""))[:80], str(cand.get("value", ""))[:80])
    path = _path_suffix_sim(target.get("ax_path", ""), cand.get("ax_path", ""))
    anc = _jaccard(_toks(" ".join(target.get("ancestors") or [])),
                   _toks(" ".join(cand.get("ancestors") or [])))
    sib = 0.5 * _seqsim(target.get("prev_sib", ""), cand.get("prev_sib", "")) + \
        0.5 * _seqsim(target.get("next_sib", ""), cand.get("next_sib", ""))
    cls = _jaccard(set(target.get("classes") or []), set(cand.get("classes") or []))
    pos = 1.0 - min(1.0, abs(float(target.get("order_frac", 0.0))
                             - float(cand.get("order_frac", 0.0))))
    return {"role": role, "label": label, "value": value, "path": path,
            "anc": anc, "sib": sib, "cls": cls, "pos": pos}


def _present(target: Dict[str, Any], cand: Dict[str, Any]) -> set:
    """Components that carry information for this pair.

    ``cls`` is dropped for AX pairs, because AX elements have no classes and a
    component that is always 0 would otherwise cap every score. The remaining
    weights are renormalised — which keeps scores comparable, and is NOT the
    same as having refitted them.
    """
    present = set(COMPONENTS)
    if not (target.get("classes") or cand.get("classes")):
        present.discard("cls")
    if not (target.get("ancestors") or cand.get("ancestors")):
        present.discard("anc")
    if not (target.get("prev_sib") or target.get("next_sib")
            or cand.get("prev_sib") or cand.get("next_sib")):
        present.discard("sib")
    return present or set(COMPONENTS)


def score(target: Dict[str, Any], cand: Dict[str, Any],
          weights: Optional[Dict[str, float]] = None) -> float:
    """Weighted similarity in [0, 1], renormalised over present components."""
    weights = weights or WEB_FITTED_WEIGHTS
    comp = components(target, cand)
    present = _present(target, cand)
    total = sum(weights.get(k, 0.0) for k in present) or 1.0
    return sum(weights.get(k, 0.0) * comp[k] for k in present) / total


# ---------------------------------------------------------------------------
# resolution with confidence and margin
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    """What the scorer decided, and how sure it is."""
    index: int = -1
    element: Optional[Dict[str, Any]] = None
    score: float = 0.0
    runner_up: float = 0.0
    abstained: bool = True
    reason: str = ""
    mode: str = "weighted"
    ranked: List[Tuple[int, float]] = field(default_factory=list)

    @property
    def margin(self) -> float:
        return max(0.0, self.score - self.runner_up)

    @property
    def confidence(self) -> float:
        """Score tempered by the margin: a high score with a close runner-up is
        not confidence, it is a tie."""
        return round(self.score * (0.5 + 0.5 * min(1.0, self.margin / DEFAULT_MIN_MARGIN)), 4)

    @property
    def low_margin(self) -> bool:
        return self.element is not None and self.margin < DEFAULT_MIN_MARGIN

    def to_dict(self) -> dict:
        return {"index": self.index, "score": round(self.score, 4),
                "runner_up": round(self.runner_up, 4), "margin": round(self.margin, 4),
                "confidence": self.confidence, "abstained": self.abstained,
                "low_margin": self.low_margin, "reason": self.reason, "mode": self.mode}


def rank(target: Dict[str, Any], candidates: Sequence[Dict[str, Any]],
         weights: Optional[Dict[str, float]] = None) -> List[Tuple[int, float]]:
    """``[(index, score), ...]`` best first. Ties keep document order."""
    scored = [(i, score(target, c, weights)) for i, c in enumerate(candidates)]
    return sorted(scored, key=lambda kv: (-kv[1], kv[0]))


def resolve_weighted(target: Dict[str, Any], candidates: Sequence[Dict[str, Any]],
                     weights: Optional[Dict[str, float]] = None,
                     min_score: float = DEFAULT_MIN_SCORE,
                     min_margin: float = DEFAULT_MIN_MARGIN) -> MatchResult:
    """Pick the best candidate, or ABSTAIN. Abstaining is a real answer here."""
    if not candidates:
        return MatchResult(reason="no candidates", mode="weighted")
    ranked = rank(target, candidates, weights)
    best_i, best_s = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    res = MatchResult(index=best_i, element=dict(candidates[best_i]), score=best_s,
                      runner_up=second, ranked=ranked[:5], mode="weighted")
    if best_s < min_score:
        res.abstained, res.element, res.reason = True, None, (
            f"best score {best_s:.3f} below {min_score}")
        return res
    if (best_s - second) < min_margin:
        res.abstained, res.reason = True, (
            f"margin {best_s - second:.3f} below {min_margin} "
            f"(two plausible candidates — abstaining rather than guessing)")
        return res
    res.abstained, res.reason = False, "confident match"
    return res


def resolve_cascade(target: Dict[str, Any],
                    candidates: Sequence[Dict[str, Any]]) -> MatchResult:
    """The incumbent behaviour, re-expressed: exact identifier, else exact
    (role, label). No score, no margin — which is the point of the comparison."""
    t_id = target.get("identifier") or ""
    t_role = target.get("role") or ""
    t_lab = target.get("label") or target.get("title") or ""
    for i, c in enumerate(candidates):
        if t_id and (c.get("identifier") or "") == t_id:
            return MatchResult(index=i, element=dict(c), score=1.0, abstained=False,
                               reason="exact identifier", mode="cascade")
    for i, c in enumerate(candidates):
        if t_role and (c.get("role") or "") == t_role and \
                t_lab and (c.get("label") or c.get("title") or "") == t_lab:
            return MatchResult(index=i, element=dict(c), score=1.0, abstained=False,
                               reason="exact role+label", mode="cascade")
    return MatchResult(reason="no exact match", mode="cascade")


def resolve(target: Dict[str, Any], candidates: Sequence[Dict[str, Any]],
            mode: Optional[str] = None,
            weights: Optional[Dict[str, float]] = None, **kw) -> MatchResult:
    """Resolve by the configured matcher. Default is the cascade, unchanged."""
    if matcher_mode(mode) == "weighted":
        return resolve_weighted(target, candidates, weights, **kw)
    return resolve_cascade(target, candidates)


# ---------------------------------------------------------------------------
# persisting a repaired selector — OPT-IN PATH ONLY
# ---------------------------------------------------------------------------

def rewritten_selector(result: MatchResult,
                       min_margin: float = DEFAULT_MIN_MARGIN * 2) -> Optional[dict]:
    """The selector to persist after a confident re-anchor, or ``None``.

    Self-healing today records a NEW implementation when a step fails but never
    repairs the stored selector, so a fragile one stays at the top of the ladder. This returns a replacement ONLY when the
    weighted matcher was confident by a wide margin AND the new selector is
    durable (has an identifier or a name) — so a repair can only ever make a
    selector more specific, never less. Returns ``None`` in cascade mode, on an
    abstention, on a thin margin, or when the match is not durable.

    It only DESCRIBES the repair; persisting it is the caller's decision.
    """
    if result.mode != "weighted" or result.abstained or result.element is None:
        return None
    if result.margin < min_margin:
        return None
    el = result.element
    identifier = el.get("identifier") or ""
    label = el.get("label") or el.get("title") or ""
    if not (identifier or label):
        return None
    return {"role": el.get("role", ""), "identifier": identifier, "title": label,
            "ax_path": el.get("ax_path", ""),
            "_repair": {"score": round(result.score, 4),
                        "margin": round(result.margin, 4),
                        "confidence": result.confidence,
                        "matcher": "weighted"}}


# ---------------------------------------------------------------------------
# fit + evaluate (for the user's own AX snapshots)
# ---------------------------------------------------------------------------

@dataclass
class EvalReport:
    """Top-1, abstention and wrong-match for one matcher over labelled pairs."""
    matcher: str = ""
    n: int = 0
    top1: float = 0.0
    abstained: float = 0.0
    wrong: float = 0.0

    def line(self) -> str:
        return (f"{self.matcher:>10s}  n={self.n:<5d} top-1={self.top1:.3f}  "
                f"abstained={self.abstained:.3f}  wrong-match={self.wrong:.3f}")

    def to_dict(self) -> dict:
        return {"matcher": self.matcher, "n": self.n, "top1": round(self.top1, 4),
                "abstained": round(self.abstained, 4), "wrong": round(self.wrong, 4)}


def evaluate(pairs: Sequence[dict], mode: str = "weighted",
             weights: Optional[Dict[str, float]] = None) -> EvalReport:
    """``pairs`` = ``[{"target": {...}, "candidates": [...], "correct": <index|-1>}]``.

    ``correct == -1`` means the element is gone, and the RIGHT answer is to
    abstain — which is why abstention is reported next to top-1 rather than
    folded into it.
    """
    rep = EvalReport(matcher=mode, n=len(pairs))
    if not pairs:
        return rep
    hit = abst = wrong = 0
    for p in pairs:
        res = resolve(p["target"], p["candidates"], mode=mode, weights=weights)
        correct = int(p.get("correct", -1))
        if res.abstained or res.element is None:
            abst += 1
            if correct == -1:
                hit += 1              # abstaining on a gone element is correct
        elif res.index == correct:
            hit += 1
        else:
            wrong += 1
    rep.top1, rep.abstained, rep.wrong = hit / rep.n, abst / rep.n, wrong / rep.n
    return rep


def fit(pairs: Sequence[dict], start: Optional[Dict[str, float]] = None,
        rounds: int = 3, step: float = 0.04) -> Tuple[Dict[str, float], EvalReport]:
    """Coordinate search over the weights, maximising (top-1 − wrong-match).

    Deliberately crude and dependency-free: a few passes of "nudge one weight up
    or down, keep it if the objective improves", renormalised to sum to 1. On a
    few hundred labelled pairs that is enough, it is deterministic, and it can be
    read in one sitting — which matters more here than the last point of
    accuracy, because whoever runs this has to trust the number that comes out.
    """
    weights = dict(start or WEB_FITTED_WEIGHTS)

    def objective(w):
        rep = evaluate(pairs, "weighted", w)
        return rep.top1 - rep.wrong

    best = objective(weights)
    for _ in range(max(1, rounds)):
        improved = False
        for key in COMPONENTS:
            for delta in (step, -step):
                trial = dict(weights)
                trial[key] = max(0.0, trial[key] + delta)
                total = sum(trial.values()) or 1.0
                trial = {k: v / total for k, v in trial.items()}
                value = objective(trial)
                if value > best + 1e-9:
                    weights, best, improved = trial, value, True
        if not improved:
            break
    return weights, evaluate(pairs, "weighted", weights)
