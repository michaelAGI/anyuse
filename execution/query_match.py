"""Does a text name the query? The rule the proof and the runtime contract share.

In a script written without spaces between words (Chinese, Japanese, Korean,
Thai, ...) a query can stand inside a longer run of letters, and the run then
may or may not be about it: 東京駅 (Tokyo station) names 東京, but 東京都
(Tokyo Metropolis) does not name 京都 (Kyoto). ``unicode_text.occurrences``
says where an occurrence stands (``word``, ``start``, ``end``, ``inside``).
Where the answer is accepted, an occurrence at a position the call does not
accept goes through this cascade:

1. the site's own evidence: the positions at which the site's own result
   lists named their query on the fitting inputs (:func:`calibrate`). They are
   stored in the result contract (``query_positions``, with
   ``query_calibration`` saying why), so the proof's digest pins them, and
   validation inputs never set them;
2. the configured check model (``execution/model_check.py``), only where the
   caller's :class:`Reading` allows it (a product call, :func:`for_call`): one
   yes/no question per reading of the text at that place (the query as a word
   of its own, or a longer word around it), the chosen reading checked against
   the text. A clear verified answer decides; anything else is no opinion;
3. the strict rule: the query on its own or at the start of a run
   (``unicode_text.STRICT_POSITIONS``).

A contract without ``query_positions`` (a tool proved before) accepts every
position, so it matches exactly as it did. Proofs, re-proofs and watch runs
open no model scope here and send no model request.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from anyuse.execution import model_check as MC
from anyuse.execution import unicode_text as U

KIND = "query_word"
#: the model step's one question: one abstract wording
QUERY_WORD = MC.Question(
    KIND,
    "The text below holds '{target}' at the place marked ⟦ ⟧. Read the text as words of its "
    "language: is the line marked >> the word that stands at that place?")
#: what decided that a text names the query, besides a position
MODEL, ABSENT = "model", "absent"
#: how much of the text around the occurrence the model reads
TEXT_REACH = 120
#: longer words offered besides the query: the shortest few around it, and the whole run
MAX_LONGER = 4
CONTRACT_KEY = "query_positions"
CALIBRATION_KEY = "query_calibration"


class Reading:
    """How one call decides that a text names the query: the positions it accepts
    and whether the configured check model may read an occurrence at another one.
    ``model_calls`` counts the requests it sent; the model is asked once per run of
    letters in a call."""

    def __init__(self, positions: Sequence[str] = U.ALL_POSITIONS, model: bool = False,
                 where: str = ""):
        self.positions: Tuple[str, ...] = tuple(p for p in positions if p in U.ALL_POSITIONS)
        self.model = bool(model)
        self.where = str(where or "")
        self.model_calls = 0
        self.backend = ""
        self._read: Dict[Tuple[str, str, int, int], Optional[bool]] = {}

    def read(self, text: str, word: str, start: int, end: int) -> Optional[bool]:
        """The model's reading of ``word`` at ``text[start:end]`` (:func:`model_reads_word`),
        asked once per run of letters around it: the statement rule reads the same line
        in several windows."""
        lo, hi = U.run_bounds(text, start, end)
        key = (text[lo:hi], word, start - lo, end - lo)
        if key not in self._read:
            named, verdict = model_reads_word(text, word, start, end, where=self.where)
            if verdict is not None:
                self.model_calls += verdict.calls
                self.backend = self.backend or (verdict.backend if verdict.calls else "")
            self._read[key] = named
        return self._read[key]


#: every position, no model: the matching of a tool proved before
ANY = Reading()


def for_call(contract: Optional[Mapping[str, Any]], ctx: Any = None, where: str = "") -> Reading:
    """The reading of a runtime check: the contract's positions (:func:`contract_positions`),
    and the check model only on a product call (``ctx`` set and the ``product`` scope of
    ``model_check`` open, both by ``run_compiled``)."""
    model = ctx is not None and MC.current_scope() == "product"
    return Reading(contract_positions(contract), model=model, where=where)


def contract_positions(contract: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """The positions a contract accepts: every position for a contract without
    ``query_positions`` (proved before positions existed), else its list."""
    if not isinstance(contract, Mapping) or CONTRACT_KEY not in contract:
        return U.ALL_POSITIONS
    raw = contract.get(CONTRACT_KEY)
    got = [p for p in U.ALL_POSITIONS if isinstance(raw, (list, tuple)) and p in raw]
    return tuple(got) or U.STRICT_POSITIONS


# ---------------------------------------------------------------------------
# the model step
# ---------------------------------------------------------------------------

def longer_words(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Spans of ``text`` that hold ``text[start:end]`` and stay inside its run of
    letters: one or two more characters on either side, shortest first, at most
    ``MAX_LONGER - 1`` of them, then the whole run."""
    lo, hi = U.run_bounds(text, start, end)
    spans = []
    for left in range(0, 3):
        for right in range(0, 3):
            a, b = start - left, end + right
            if (left or right) and lo <= a and b <= hi:
                spans.append((a, b))
    spans.sort(key=lambda s: (s[1] - s[0], s[0]))
    out = [s for s in spans if s != (lo, hi)][:MAX_LONGER - 1]
    if (lo, hi) != (start, end):
        out.append((lo, hi))
    return out


def model_reads_word(text: str, word: str, start: int, end: int, *, where: str = ""
                     ) -> Tuple[Optional[bool], Optional[MC.Verdict]]:
    """Does the configured check model read ``word`` at ``text[start:end]`` as a word
    of its own (True) or as letters of a longer word (False)? ``(None, verdict)``
    when it gives no clear verified answer, ``(None, None)`` when nothing was asked.

    The candidates are the query itself and the longer words around it
    (:func:`longer_words`); the model's pick must be a word that stands there."""
    if not word or text[start:end] != word:
        return None, None
    spans = [(start, end)] + longer_words(text, start, end)
    if len(spans) < 2:
        return None, None
    lo = max(0, start - TEXT_REACH)
    hi = min(len(text), end + TEXT_REACH)
    state = text[lo:start] + "⟦" + text[start:end] + "⟧" + text[end:hi]
    cands = [MC.Candidate(text=text[a:b], ref=(a, b)) for a, b in spans]

    def verify(c: MC.Candidate) -> bool:
        a, b = c.ref
        return a <= start and end <= b and text[a:b] == c.text

    try:
        index, verdict = MC.choose(QUERY_WORD, word, cands, state=state, verify=verify,
                                   where=where or "query_match.word")
    except Exception:                                 # noqa: BLE001 - no opinion
        return None, None
    if index is None or not 0 <= index < len(cands):
        return None, verdict
    return index == 0, verdict


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

def word_named(low: str, word: str, reading: Optional[Reading] = None) -> Tuple[bool, str]:
    """``(named, how)``: does the normalised ``word`` stand in the normalised ``low``
    at a position ``reading`` accepts, or as the model reads it there? ``how`` is the
    position, ``model``, or ``absent`` (no occurrence at all)."""
    r = reading or ANY
    found = U.occurrences(low, word, r.positions)
    if found:
        return True, found[0][2]
    best = U.best_occurrence(low, word)
    if best is None:
        return False, ABSENT
    start, end, pos = best
    if r.model and pos in (U.END, U.INSIDE) and MC.available(KIND):
        if r.read(low, word, start, end):
            return True, MODEL
    return False, pos


def naming(text: Any, value: Any, reading: Optional[Reading] = None, *,
           whole_value: bool = False) -> Tuple[bool, str]:
    """``(named, how)`` for ``text`` and the query ``value``, both normalised here.

    ``whole_value``: the query's words as ONE contiguous run (an identity key);
    otherwise every word of the query (two or more characters, or one
    ideograph; all of them when none is that long) on its own. ``how`` is the
    weakest reading that was needed (``absent`` or the refused position when
    the text does not name it)."""
    from anyuse.execution.api_compile import is_word, normalise
    low, q = normalise(text), normalise(value)
    if not low or not q:
        return False, ABSENT
    words = [q] if whole_value else ([w for w in q.split() if is_word(w)] or q.split())
    how = U.WORD
    for w in words:
        ok, got = word_named(low, w, reading)
        if not ok:
            return False, got
        if got == MODEL or _RANK.get(got, 0) > _RANK.get(how, 0):
            how = got
    return True, how


_RANK = {U.WORD: 0, U.START: 1, U.END: 2, U.INSIDE: 3, MODEL: 4}


def names(text: Any, value: Any, reading: Optional[Reading] = None, *,
          whole_value: bool = False) -> bool:
    """:func:`naming`'s answer alone."""
    return naming(text, value, reading, whole_value=whole_value)[0]


def inside_a_word(how: str) -> bool:
    """Did :func:`naming` refuse an occurrence because it stands inside a longer word?"""
    return how in (U.END, U.INSIDE)


# ---------------------------------------------------------------------------
# calibration: the positions the site's own result lists show
# ---------------------------------------------------------------------------

def positions_on_page(labels: Sequence[Any], value: Any) -> List[str]:
    """The positions at which a result list's labels hold the query's words (each
    label's best occurrence). ``[]`` for a query in a script with spaces."""
    from anyuse.execution.api_compile import is_word, normalise
    q = normalise(value)
    words = [w for w in q.split() if is_word(w)] or q.split()
    if not words or not any(U.unspaced(w[0]) or U.unspaced(w[-1]) for w in words):
        return []
    seen = set()
    for label in labels:
        low = normalise(label)
        for w in words:
            best = U.best_occurrence(low, w)
            if best is not None:
                seen.add(best[2])
    return [p for p in U.ALL_POSITIONS if p in seen]


def calibrate(cases: Sequence[Mapping[str, Any]], min_list: int) -> Tuple[List[str], str]:
    """``(positions, why)`` from the FITTING inputs' result lists (``[{"value",
    "ui_items"}]``): the strict positions, plus each position at which a list of
    at least ``min_list`` labels held its own query. A shorter list is never its
    own evidence, and validation inputs are never passed here."""
    found: Dict[str, int] = {}
    lists = 0
    for case in cases:
        labels = [str(x) for x in case.get("ui_items") or [] if str(x).strip()]
        if len(labels) < max(1, int(min_list)):
            continue
        lists += 1
        for p in positions_on_page(labels, case.get("value", "")):
            found[p] = found.get(p, 0) + 1
    extra = [p for p in (U.END, U.INSIDE) if p in found]
    positions = [p for p in U.ALL_POSITIONS if p in U.STRICT_POSITIONS or p in extra]
    if extra:
        shown = ", ".join(f"{p} of a word on {found[p]} of {lists} list page(s)" for p in extra)
        why = f"the site's own result lists named their input inside a longer word ({shown})"
    else:
        why = (f"start of a word or on its own: none of {lists} fitting list page(s) named "
               f"its input at the end of or inside a longer word")
    return positions, why
