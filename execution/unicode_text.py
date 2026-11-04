"""Letters, digits and word positions in every script.

The pieces ``api_compile.normalise`` and the query match
(``execution/query_match.py``) are built from, read from the Unicode
properties of each character, with no table per language:

* **digits** are read by their value: every decimal digit (``Nd``) becomes
  0-9, so ``٢٠٢٤``, ``๒๕๖๗`` and ``2024`` are the same number;
* **accents of letters with case** are dropped: in the canonical
  decomposition a combining mark after a letter that has an upper and a lower
  case is removed (``Αθήνα`` = ``ΑΘΗΝΑ``, ``München`` = ``Munchen``,
  ``Hội An`` = ``Hoi An``). A Latin letter drawn with a stroke or a hook has no
  decomposition; its Unicode name says what it is drawn from (``LATIN SMALL
  LETTER L WITH STROKE``), and it reads as that letter (ł = l, ø = o, đ = d).
  Letters of other cased scripts without a decomposition stay letters of their
  own (Kazakh қ is not к);
* **optional vowel marks of Arabic and Hebrew** are dropped: a mark with a
  nonzero canonical combining class after an Arabic or Hebrew letter (harakat,
  niqqud, the hamza a letter decomposes into), and the Arabic modifier letters
  that stretch or annotate a word (the tatweel). ``مَكْتَبَة`` = ``مكتبة``;
* letters of other scripts without case keep every mark (Thai, Devanagari,
  the kana voicing marks).

**Positions**: in a script written without spaces between words (Chinese,
Japanese, Korean, Thai, ...) a query can stand inside a longer run of letters.
:func:`occurrences` says where each occurrence stands: on its own (``word``:
東京 in 東京タワー, where the script changes), at the start of a run
(``start``: 東京 in 東京駅), at its end (``end``: 京都 in 東京都) or inside it
(``inside``: 京都 in 東京都庁). The script of a character is the first word of
its Unicode name (``CJK``, ``HIRAGANA``, ``KATAKANA``, ``HANGUL``, ``THAI``,
...); marks and modifier letters (ー, 々) continue the run they stand in. An
occurrence that the text goes on with a combining mark ends inside a letter
and stands ``inside`` (ไข in ไข่). In a script with spaces an occurrence needs a
space, an end of the text or a letter of an unspaced script on both sides, and
is always ``word``.

Pure, standard library only.
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

WORD, START, END, INSIDE = "word", "start", "end", "inside"
ALL_POSITIONS: Tuple[str, ...] = (WORD, START, END, INSIDE)
#: the query on its own or at the start of a run: compounds of these scripts put
#: the word they modify last, so a query at the start of a run is the usual case
#: (東京駅 is a station of Tokyo); at the end it often belongs to another word
#: (京都 in 東京都)
STRICT_POSITIONS: Tuple[str, ...] = (WORD, START)
_RANK = {WORD: 0, START: 1, END: 2, INSIDE: 3}

#: scripts whose optional vowel marks are dropped
_ABJADS = ("ARABIC", "HEBREW")


def dense(ch: str) -> bool:
    """A Han ideograph or a Hangul syllable."""
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0xAC00 <= o <= 0xD7AF or 0x20000 <= o <= 0x3FFFF)


def unspaced(ch: str) -> bool:
    """A character of a script written without spaces between words: Chinese,
    Japanese, Korean (whose particles attach to the word), Thai, Lao, Khmer,
    Myanmar and Yi."""
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7FF or 0xF900 <= o <= 0xFAFF
            or 0x0E00 <= o <= 0x0EFF or 0x1000 <= o <= 0x109F or 0x1100 <= o <= 0x11FF
            or 0x1780 <= o <= 0x17FF or 0xA000 <= o <= 0xA4CF or 0xA960 <= o <= 0xA97F
            or 0x20000 <= o <= 0x3FFFF)


def match_length(text: str) -> int:
    """The length the match floors compare: characters, with a Han ideograph or
    a Hangul syllable counted twice. One of them carries about what two Latin
    letters do, and two-character names ("大阪", "서울") are common."""
    if text.isascii():
        return len(text)
    return len(text) + sum(1 for ch in text if dense(ch))


@lru_cache(maxsize=4096)
def script_of(ch: str) -> str:
    """The first word of ``ch``'s Unicode name (``LATIN``, ``CJK``, ``HIRAGANA``,
    ``THAI``, ...); ``DIGIT`` for a decimal digit; ``""`` for a mark, a modifier
    letter (ー, 々) or a character without a name, which continue the run they
    stand in."""
    cat = unicodedata.category(ch)
    if cat == "Nd":
        return "DIGIT"
    if cat[0] == "M" or cat == "Lm":
        return ""
    name = unicodedata.name(ch, "")
    return name.split(" ", 1)[0] if name else ""


def ascii_digits(s: str) -> str:
    """``s`` with every decimal digit of any script written 0-9."""
    if s.isascii():
        return s
    return "".join(str(unicodedata.decimal(ch)) if not ch.isascii()
                   and unicodedata.category(ch) == "Nd" else ch for ch in s)


def _cased(ch: str) -> bool:
    return ch.lower() != ch.upper()


@lru_cache(maxsize=4096)
def base_letter(ch: str) -> str:
    """The letter a Latin letter without a canonical decomposition is drawn from,
    by its Unicode name: ``LATIN SMALL LETTER L WITH STROKE`` is ``l``. Only
    when that letter exists and has case; any other character is returned as it is."""
    if ch.isascii() or not _cased(ch):
        return ch
    deco = unicodedata.decomposition(ch)
    if deco and not deco.startswith("<"):
        return ch
    name = unicodedata.name(ch, "")
    if not name.startswith("LATIN ") or " WITH " not in name:
        return ch
    try:
        base = unicodedata.lookup(name.split(" WITH ", 1)[0])
    except KeyError:
        return ch
    return base if len(base) == 1 and _cased(base) else ch


#: what :func:`fold_letters` does with a character: keep it, drop it, or how it
#: starts a run of letters whose marks are dropped
_KEEP, _DROP, _CASED, _ABJAD = "keep", "drop", "cased", "abjad"


@lru_cache(maxsize=8192)
def _fold_char(ch: str) -> Tuple[str, str]:
    """``(kind, replacement)`` for one character of an NFD string."""
    cat = unicodedata.category(ch)
    if cat == "Nd":
        return _KEEP, str(unicodedata.decimal(ch))
    if cat[0] == "M":
        return "mark", ch
    if cat[0] != "L":
        return _KEEP, ch
    script = script_of(ch) or (unicodedata.name(ch, "").split(" ", 1)[0])
    if script in _ABJADS:
        # a modifier letter of these scripts stretches or annotates the word it is in
        return (_DROP, "") if cat == "Lm" else (_ABJAD, ch)
    if _cased(ch):
        return _CASED, base_letter(ch) if script == "LATIN" else ch
    return _KEEP, ch


def keeps_marks(ch: str) -> bool:
    """Do the combining marks after ``ch`` belong to its letter: is it a letter of a
    script without case other than Latin, Arabic and Hebrew (Thai, Devanagari, the
    kana)? Those are the marks :func:`fold_letters` keeps on a letter."""
    if unicodedata.category(ch)[0] != "L" or _fold_char(ch)[0] != _KEEP:
        return False
    return (script_of(ch) or unicodedata.name(ch, "").split(" ", 1)[0]) != "LATIN"


def fold_letters(s: str) -> str:
    """Digits by value, accents of letters with case dropped, Latin letters drawn
    with a stroke read as their base letter, Arabic and Hebrew optional marks
    dropped; every other mark kept. The result is in NFC form."""
    if s.isascii():
        return s
    out: List[str] = []
    base = ""                 # the kind of the letter the next marks belong to
    for ch in unicodedata.normalize("NFD", s):
        kind, rep = _fold_char(ch)
        if kind == "mark":
            if base == _CASED or (base == _ABJAD and unicodedata.combining(ch)):
                continue
            out.append(ch)
            continue
        if kind == _DROP:
            base = _ABJAD
            continue
        out.append(rep)
        base = kind
    return unicodedata.normalize("NFC", "".join(out))


# ---------------------------------------------------------------------------
# positions in a run of letters
# ---------------------------------------------------------------------------

def _runs_on(ch: str) -> bool:
    """A mark or a modifier letter (ー, 々): it continues the run it stands in."""
    cat = unicodedata.category(ch)
    return cat[0] == "M" or cat == "Lm"


def _edge_script(words: str, last: bool) -> str:
    """The script at one edge of the query; the marks and modifier letters there
    are skipped (a Thai word may end in a tone mark, a Japanese one in ー)."""
    seq = reversed(words) if last else iter(words)
    for ch in seq:
        s = script_of(ch)
        if s or not _runs_on(ch):
            return s
    return ""


def _side(text: str, at: int, edge: str, edge_script: str) -> Optional[bool]:
    """Is the side of an occurrence next to ``text[at]`` bounded? ``None`` when it
    is no occurrence at all (a letter of a spaced script runs on)."""
    if at < 0 or at >= len(text) or text[at] == " ":
        return True
    if unspaced(edge):
        return bool(edge_script) and script_of(text[at]) not in ("", edge_script)
    return True if unspaced(text[at]) else None


def _position(start_bounded: bool, end_bounded: bool) -> str:
    if start_bounded:
        return WORD if end_bounded else START
    return END if end_bounded else INSIDE


def _classify(text: str, start: int, words: str, head: str, tail: str) -> Optional[str]:
    """The position of the occurrence of ``words`` at ``text[start]``; ``None`` when
    it is no occurrence. An occurrence in an unspaced script that the text goes on
    with a combining mark ends inside a letter (ไข in ไข่, whose last letter carries a
    tone mark the input lacks), so it does not start a word: it stands inside one."""
    end = start + len(words)
    before = _side(text, start - 1, words[0], head)
    after = _side(text, end, words[-1], tail) if before is not None else None
    if before is None or after is None:
        return None
    pos = _position(before, after)
    if pos == START and unspaced(words[-1]) and end < len(text) \
            and unicodedata.category(text[end])[0] == "M":
        return INSIDE
    return pos


def occurrences(text: str, words: str, positions: Sequence[str] = ALL_POSITIONS
                ) -> List[Tuple[int, int, str]]:
    """``(start, end, position)`` for each occurrence of the normalised ``words``
    in the normalised ``text``, left to right and not overlapping, whose position
    is one of ``positions``. With every position this finds exactly the spans of
    the whole-word rule (``api_compile.word_spans``)."""
    out: List[Tuple[int, int, str]] = []
    if not words or not text:
        return out
    head, tail = _edge_script(words, False), _edge_script(words, True)
    want = set(positions)
    start = text.find(words)
    while start >= 0:
        end = start + len(words)
        pos = _classify(text, start, words, head, tail)
        if pos is not None and pos in want:
            out.append((start, end, pos))
            start = text.find(words, end)
            continue
        start = text.find(words, start + 1)
    return out


def best_occurrence(text: str, words: str) -> Optional[Tuple[int, int, str]]:
    """The occurrence of ``words`` in ``text`` at the best position (word, then
    start, end, inside), the first of those; ``None`` when there is none."""
    best: Optional[Tuple[int, int, str]] = None
    if not words or not text:
        return best
    head, tail = _edge_script(words, False), _edge_script(words, True)
    start = text.find(words)
    while start >= 0:
        pos = _classify(text, start, words, head, tail)
        if pos is not None and (best is None or _RANK[pos] < _RANK[best[2]]):
            best = (start, start + len(words), pos)
            if pos == WORD:
                break
        start = text.find(words, start + 1)
    return best


def run_bounds(text: str, start: int, end: int) -> Tuple[int, int]:
    """The run of letters around ``text[start:end]``: extended on each side while
    the next character is of the script of the occurrence's edge (or a mark or
    modifier letter, which continue a run)."""
    lo, hi = start, end
    head = script_of(text[start]) if start < len(text) else ""
    tail = _edge_script(text[start:end], True) if end > start else head
    while lo > 0 and text[lo - 1] != " " and script_of(text[lo - 1]) in ("", head):
        lo -= 1
    while hi < len(text) and text[hi] != " " and script_of(text[hi]) in ("", tail):
        hi += 1
    return lo, hi
