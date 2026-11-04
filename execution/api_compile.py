"""Deterministic API extraction — turn a recording into a :class:`RequestTemplate`.

No model is involved in the decision. The compiler answers three questions from
a recording (what the UI showed + the network traffic it produced):

  1. **Which request explains the screen?** Flatten every JSON response into
     scalar leaves and score it by how much of the visible result list those
     leaves account for (:func:`score_candidate`). The request the UI actually
     rendered from wins; analytics beacons, metadata pings and unrelated XHRs
     score ~0.
  2. **Which URL/query/body component carries the user's typed value?** Look for
     the recorded parameter value in the path segments, the query values and the
     JSON body leaves; that component becomes a ``{param}`` placeholder.
  3. **What is volatile?** Cache-busters, telemetry and epoch-looking values are
     dropped (:data:`api_step.VOLATILE_PARAM_KEYS`); signed/session-bound
     parameters make the candidate ineligible rather than being stripped.

The output is a template plus the extraction paths and a deterministic
postcondition. It is a *candidate* until ``api_verify`` proves it on parameter
values that were never in the recording —

Where a typed decision layer would help is recorded, not wired: when two or more
candidates score within :data:`AMBIGUITY_EPS` of each other the result is
``ambiguous`` and carries every candidate, so a later version can hand that
choice to a small typed model instead of a threshold.

Input shape (``recording``) — deliberately minimal so the compiler is not tied
to any one recorder::

    {"param": {"name": "q", "value": "serde"},
     "ui": {"items": ["serde v1.0.229 …", …], "text": "…"},
     "network": [{"url": ..., "method": "GET", "status": 200,
                  "mime": "application/json", "body": "<raw text>",
                  "resource_type": "XHR", "bytes": 1234}, …]}
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from anyuse.execution.api_step import (
    PICK_BY_NAME,
    READ_METHODS,
    PlanStep,
    RequestPlan,
    SIGNED_PARAM_KEYS,
    VOLATILE_PARAM_PREFIXES,
    VOLATILE_PARAM_KEYS,
    RequestTemplate,
    keyed_rows,
    load_command,
    json_path,
    json_leaves,
    pick_row,
)
from anyuse.execution.param_binding import BindingEvidence, binding_evidence
from anyuse.execution import unicode_text as _U
from anyuse.execution.blocked import challenge_served_instead

logger = logging.getLogger(__name__)

#: Two candidates whose scores differ by less than this are "equally plausible".
AMBIGUITY_EPS = 0.05
#: A candidate must explain at least this share of the visible items.
MIN_COVERAGE = 0.5
#: The SECOND step of a chain may explain less, because the chain structure is
#: itself strong evidence: its URL literally carries a value the first response
#: produced, which is not a coincidence. The fresh-input proof still decides.
CHAIN_MIN_COVERAGE = 0.3
#: How many visible result items participate in scoring.
TOP_ITEMS = 10
#: Shortest normalised string that may count as a value match (kills "a"/"of"),
#: in :func:`match_length` units.
MIN_MATCH_LEN = 3

#: A containment match must account for at least this share of the longer
#: string. Without it a one-word JSON leaf ("serde", a keyword) "explains" a
#: whole sentence that merely contains it, and the extractor happily compiles
#: the keyword list instead of the description.
MIN_CONTAINMENT_RATIO = 0.6

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^0-9a-z]+")
_EPOCHY = re.compile(r"^\d{9,16}$")
_HEXY = re.compile(r"^[0-9a-f]{16,}$", re.I)
#: Characters that change how a letter is drawn, not which letter it is:
#: variation selectors (the Arabic tatweel is dropped by ``unicode_text.fold_letters``).
_DRAWING_ONLY = dict.fromkeys([*range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0),
                               *range(0x180B, 0x1810)])


# ---------------------------------------------------------------------------
# levers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Levers:
    """The compile levers, individually switchable.

    They exist as a switch and not as a rewrite so the effect of each one can be
    measured on an untouched sample: :meth:`Levers.none` reproduces the
    compiler without them exactly (the "levers off" baseline). The default is
    everything on — a lever that is not on by default is not a product, it is a
    branch.

    The first three (a–c) moved coverage by **zero** on two independent
    samples and are kept for diagnosis only, to be re-examined rather than
    extended. (d) is switchable for exactly the same reason: so that "it
    bought coverage" is a measurement and not a hope.
    """
    #: (a) derive an HTML selector from the page's repeated structure as well as
    #: from the matched values, and keep whichever explains more.
    repetition_selector: bool = True
    #: (b) drop a query parameter only on evidence that it does not matter,
    #: instead of on its name alone.
    evidence_volatile: bool = True
    #: (c) the read-only POST rung — GraphQL ``query`` operations and search
    #: POSTs the page itself issued, never anything carrying a credential.
    readonly_post: bool = False
    #: (d) bind a parameter that is a run of path segments or a
    #: delimited part of one, when every recording corroborates the same site.
    path_binding: bool = True
    #: (f) nested-layer slots (execution/api_slots.py): bind a parameter that
    #: sits inside decoded layers (JSON in a form field or query value,
    #: base64 JSON, a nested URL, part of a leaf). Tried only where every
    #: plain binding found nothing, so every template the other levers build
    #: is built identically.
    nested_slots: bool = True

    @classmethod
    def none(cls) -> "Levers":
        return cls(repetition_selector=False, evidence_volatile=False,
                   readonly_post=False, path_binding=False, nested_slots=False)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Levers":
        import os
        from anyuse.settings import get as get_setting
        env = env if env is not None else dict(os.environ)
        raw = str(get_setting("ANYUSE_COMPILE_LEVERS", env=env)).strip().lower()
        if raw in ("", "on", "all", "default"):
            return cls()
        if raw in ("off", "none"):
            return cls.none()
        wanted = {p.strip() for p in raw.split(",") if p.strip()}
        return cls(repetition_selector="repetition" in wanted,
                   evidence_volatile="volatile" in wanted,
                   readonly_post="post" in wanted,
                   path_binding="binding" in wanted or "path" in wanted,
                   nested_slots="slots" in wanted or "nested" in wanted)


DEFAULT_LEVERS = Levers()


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

#: the version of :func:`normalise`'s output. A learned empty page
#: (execution/empty_page.py) stores normalised lines and this number; one
#: stored under another number is not used. Raise it whenever the output changes.
NORMALISE_VERSION = 2


def normalise(text: Any) -> str:
    """Casefold, strip punctuation, collapse whitespace.

    The single normalisation used everywhere in compile *and* verify, so the
    equivalence proof compares the same way the extractor matched.

    Letters and digits of every script are kept. Text is put in NFKC form
    before and after casefolding, so a precomposed and a decomposed accent,
    full-width and half-width forms and compatibility ligatures compare equal.
    Then (``unicode_text.fold_letters``): digits of every script are read by
    their value (``٣`` is ``3``); the accents of letters that have case are
    dropped (``ΑΘΗΝΑ`` = ``Αθήνα``, ``MÜNCHEN`` = ``München``), and a Latin
    letter drawn with a stroke reads as its base letter (``Wrocław`` =
    ``Wroclaw``); the optional vowel marks of Arabic and Hebrew and the Arabic
    tatweel are dropped. Any other combining mark stays with the letter before
    it (Thai and Devanagari vowel signs are part of their word). Invisible
    format characters (soft hyphen, zero-width joiners, direction marks) and
    variation selectors are dropped without splitting the word they sit in.
    ASCII text takes the plain path, so its result is what it always was.
    """
    if text is None:
        return ""
    s = str(text)
    if s.isascii():
        s = _NONWORD.sub(" ", s.lower())
        return _WS.sub(" ", s).strip()
    s = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", s).casefold())
    s = _U.fold_letters(s.translate(_DRAWING_ONLY))
    out: List[str] = []
    in_word = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            in_word = True
            continue
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        if cat[0] == "M" and in_word:
            out.append(ch)
            continue
        if in_word:
            out.append(" ")
        in_word = False
    return "".join(out).rstrip()


#: kept under these names for the callers that import them from here
_dense = _U.dense
_unspaced = _U.unspaced
match_length = _U.match_length


def long_enough(text: str) -> bool:
    """Is ``text`` long enough to count as a value match (:data:`MIN_MATCH_LEN`)?"""
    return len(text) >= MIN_MATCH_LEN or match_length(text) >= MIN_MATCH_LEN


def is_word(token: str) -> bool:
    """Is a normalised token a word rather than a stray letter: two or more
    characters, or one ideograph (:func:`match_length`)?"""
    return match_length(token) >= 2


def word_spans(text: str, words: str,
               positions: Sequence[str] = _U.ALL_POSITIONS) -> List[Tuple[int, int]]:
    """Where the normalised ``words`` occur in the normalised ``text`` as whole
    words, left to right and not overlapping.

    An occurrence starts and ends at a word boundary: a space or an end of
    ``text``. A script written without spaces between words marks no
    boundaries, so next to one of its characters (:func:`_unspaced`) every
    position is one: "東京" occurs in "東京タワー", and "iphone" in
    "iphoneケース". For ASCII text this is the whole-token match of
    ``(?<![a-z0-9])word(?![a-z0-9])``. ``positions`` keeps only the occurrences
    at those places of a run of an unspaced script (``unicode_text``: on its
    own, at the start, the end or inside of a longer run); the default keeps
    every one."""
    return [(a, b) for a, b, _pos in _U.occurrences(text, words, positions)]


def contains_words(text: str, words: str,
                   positions: Sequence[str] = _U.ALL_POSITIONS) -> bool:
    """Do the normalised ``words`` occur in the normalised ``text`` as whole
    words (:func:`word_spans`)?"""
    return bool(word_spans(text, words, positions))


_NUMBER_TEXT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _as_number(value: Any) -> Optional[float]:
    """A JSON number, or text that is exactly one, as a number; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and _NUMBER_TEXT.fullmatch(value.strip()):
        text = value.strip()
        return int(text) if re.fullmatch(r"[+-]?\d+", text) else float(text)
    return None


def _as_bool(value: Any) -> Optional[bool]:
    """A JSON boolean, or the text ``true`` / ``false``, as a bool; else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _scalars_match(a: Any, b: Any) -> bool:
    """:func:`values_match` when one side is not text: booleans and numbers by value."""
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        left, right = _as_bool(a), _as_bool(b)
        return left is not None and left == right
    left, right = _as_number(a), _as_number(b)
    if left is not None and right is not None:
        return left == right
    return type(a) is type(b) and a == b


def values_match(a: Any, b: Any, ratio: float = MIN_CONTAINMENT_RATIO) -> bool:
    """Do two already-normalised strings denote the same thing?

    Equality, or containment where the shorter string accounts for at least
    ``ratio`` of the longer one. The single match predicate used by both the
    extractor's scoring and the verifier's comparison, so a template is checked
    the same way it was built.

    A value that is not text (a JSON number, boolean, list or object read
    straight from a response) is compared by value: ``42`` matches ``42``,
    ``42.0`` and the text ``"42"``, ``True`` matches ``True`` and ``"true"``,
    and a list or object matches only an equal one.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return _scalars_match(a, b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return short in long and len(short) >= ratio * len(long)


def first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """One eligible request from the recording, with its score and evidence."""
    url: str
    method: str = "GET"
    status: int = 200
    mime: str = ""
    bytes: int = 0
    data: Any = None
    coverage: float = 0.0
    matched_items: int = 0
    total_items: int = 0
    #: generalised leaf path -> number of visible items it explained
    path_hits: Dict[str, int] = field(default_factory=dict)
    #: per visible item, the generalised paths that explained it (for set cover)
    item_paths: List[List[str]] = field(default_factory=list)
    ineligible: str = ""
    #: position in the recording; a chain producer must precede its consumer
    recording_index: int = -1
    #: Site-mode login=replay: the session cookie NAMES the recorder observed on
    #: this request (never values). Carried onto the template as ``cookie_refs``
    #: so the proved rung knows which cookies to import at replay time. Empty for
    #: every ordinary (credential-free) recording.
    cookie_names: List[str] = field(default_factory=list)
    #: Site-mode identity=browser: the User-Agent the recorder captured on this
    #: request, carried onto the template as ``browser_ua`` (never invented).
    browser_ua: str = ""
    #: the credential-named fields of this request whose values an earlier page
    #: of the site stated by name (``page_literals.mark_network``): each with
    #: its source page and locator, never the value
    page_keys: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc

    def summary(self) -> dict:
        sp = urllib.parse.urlsplit(self.url)
        return {"host": sp.netloc, "path": sp.path, "method": self.method,
                "coverage": round(self.coverage, 3), "bytes": self.bytes,
                "best_path": self.best_path(), "ineligible": self.ineligible}

    def best_path(self) -> str:
        if not self.path_hits:
            return ""
        return max(self.path_hits.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


def eligible_requests(recording: dict) -> List[dict]:
    """The requests a template may be built from: 2xx, read method, JSON body."""
    out = []
    for recording_index, r in enumerate(recording.get("network") or []):
        if (r.get("method") or "GET").upper() not in READ_METHODS:
            continue
        status = r.get("status")
        if not status or not (200 <= int(status) < 300):
            continue
        if not str(r.get("url", "")).startswith("https://"):
            continue
        body = r.get("body")
        if not body:
            continue
        mime = (r.get("mime") or "").lower()
        if "json" not in mime and not str(body).lstrip()[:1] in ("{", "["):
            continue
        try:
            r = dict(r)
            r["_data"] = json.loads(body)
            r["_recording_index"] = recording_index
        except (ValueError, TypeError):
            continue
        out.append(r)
    return out


def visible_items(recording: dict, limit: int = TOP_ITEMS,
                  exclude: Optional[Any] = None) -> List[str]:
    """The on-screen result list, normalised to one label per item.

    ``exclude`` is a set of labels to drop — see :func:`page_chrome`.
    """
    items = (recording.get("ui") or {}).get("items") or []
    skip = exclude or frozenset()
    out: List[str] = []
    seen = set()
    for raw in items:
        label = normalise(first_line(raw))
        if not long_enough(label) or label in seen or label in skip:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= limit:
            break
    return out


def page_chrome(recordings: List[dict], min_recordings: int = 2) -> set:
    """Labels that appear in EVERY recording of a DIFFERENT query.

    A result list changes when the query changes; a navigation bar, a footer,
    a cookie banner and a "Sign in" link do not. Anything present in all of
    them is therefore page chrome the recorder's selector swept up, not an
    answer to the question the user asked.

    This matters more than it sounds. The chrome dilutes the ground truth on
    both sides of the equivalence proof: the compiler is asked to find a
    request that explains "Sign in", it cannot, coverage falls below the
    threshold and a perfectly good rung is rejected — or worse, a rung IS
    emitted and then fails its proof for a reason that has nothing to do with
    the data. Removing it is the single highest-yield recorder-side fix
    measured; see `docs/COMPILE.md` for the before/after.

    Conservative by construction: it needs at least ``min_recordings``
    recordings, and a label must appear in ALL of them. A result that genuinely
    matches every query in the sample is dropped too — with three queries that
    is possible, so the number of dropped labels is reported rather than hidden.
    """
    sets = []
    for rec in recordings or []:
        labels = {normalise(first_line(raw))
                  for raw in ((rec.get("ui") or {}).get("items") or [])}
        labels = {x for x in labels if long_enough(x)}
        if labels:
            sets.append(labels)
    if len(sets) < min_recordings:
        return set()
    common = set(sets[0])
    for other in sets[1:]:
        common &= other
    return common


def score_candidate(request: dict, items: List[str]) -> Candidate:
    """Value-overlap score: what share of the visible items do this response's
    JSON leaves account for, and via which leaf path?"""
    cand = Candidate(url=request.get("url", ""), method=(request.get("method") or "GET").upper(),
                     status=int(request.get("status") or 0), mime=request.get("mime") or "",
                     bytes=int(request.get("bytes") or 0), data=request.get("_data"),
                     total_items=len(items),
                     recording_index=int(request.get("_recording_index", -1)),
                     cookie_names=[str(n) for n in (request.get("cookie_names") or [])],
                     browser_ua=str(request.get("request_user_agent") or ""),
                     page_keys=[dict(k) for k in (request.get("page_keys") or [])
                                if isinstance(k, dict)])
    leaves = [(p, normalise(v)) for p, v in json_leaves(cand.data)
              if isinstance(v, (str, int, float)) and long_enough(normalise(v))]
    if not items or not leaves:
        return cand
    matched = 0
    for label in items:
        hit_paths = set()
        for path, value in leaves:
            if values_match(label, value):
                hit_paths.add(generalise_path(path))
        cand.item_paths.append(sorted(hit_paths))
        if hit_paths:
            matched += 1
            for p in hit_paths:
                cand.path_hits[p] = cand.path_hits.get(p, 0) + 1
    cand.matched_items = matched
    cand.coverage = matched / len(items)
    cand.ineligible = _ineligible_reason(
        cand.url, _page_key_names(cand.page_keys) if cand.page_keys and page_keys_enabled() else ())
    return cand


def cover_paths(candidate: "Candidate", max_paths: int = 4) -> List[str]:
    """Smallest set of leaf paths that together explain the visible items.

    A result LIST is explained by one path (``packages[*].id``). A DETAIL screen is
    not: its name, its description and its version live at three unrelated
    paths, and picking only the most frequent one throws two thirds of the
    evidence away (and, on a package's detail screen, picks the package's
    *keywords* because the package name happens to appear there). Greedy set cover
    over :attr:`Candidate.item_paths`, largest gain first, ties broken by the
    shorter path so the plainest field wins.
    """
    uncovered = {i for i, paths in enumerate(candidate.item_paths) if paths}
    chosen: List[str] = []
    while uncovered and len(chosen) < max_paths:
        best, best_gain = "", 0
        for path in candidate.path_hits:
            gain = sum(1 for i in uncovered if path in candidate.item_paths[i])
            if gain > best_gain or (gain == best_gain and gain > 0 and best and len(path) < len(best)):
                best, best_gain = path, gain
        if not best_gain:
            break
        chosen.append(best)
        uncovered -= {i for i in uncovered if best in candidate.item_paths[i]}
    return chosen


def generalise_path(path: str) -> str:
    """``packages[3].name`` → ``packages[*].name`` — one item's path becomes the
    pattern that reads every item."""
    return re.sub(r"\[\d+\]", "[*]", path)


def _ineligible_reason(url: str, exempt: Sequence[str] = ()) -> str:
    """Why a request's query makes it ineligible; a key in ``exempt`` (a page
    key the site's page stated, :func:`_page_key_names`) does not."""
    q = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    skip = {str(k).lower() for k in exempt}
    for k, _v in q:
        if k.lower() in SIGNED_PARAM_KEYS and k.lower() not in skip:
            return f"signed/session-bound query parameter {k!r}"
    return ""


def _page_key_names(page_keys: Sequence[Dict[str, Any]]) -> List[str]:
    """The field names (query key, header, last body key) of a request's page keys."""
    out: List[str] = []
    for k in page_keys or ():
        at = str(k.get("at") or "")
        where = at.split(":", 1)[-1]
        name = where.rsplit(".", 1)[-1] if at.startswith("body:") else where
        if name:
            out.append(name.lower())
    return out


def page_keys_enabled() -> bool:
    """``ANYUSE_PAGE_KEYS``: may a compiled HTTP tool read a value from the site's page?"""
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_PAGE_KEYS"))


# ---------------------------------------------------------------------------
# parameter location
# ---------------------------------------------------------------------------

@dataclass
class ParamSite:
    """Where the user's typed value sits in a request."""
    where: str        # "query" | "path" | "none"
    key: str = ""     # query key, or the path-segment index as a string
    exact: bool = True


def locate_param(url: str, value: str) -> ParamSite:
    """Find the URL component that carries ``value``.

    Query values are compared raw and normalised (a UI turns "rust async" into
    ``rust+async``); path segments are compared after percent-decoding. Query
    wins over path when both match, because a query parameter is the far more
    common carrier and is unambiguous to re-bind.
    """
    want = normalise(value)
    if not want:
        return ParamSite("none")
    sp = urllib.parse.urlsplit(url)
    for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
        if normalise(val) == want:
            return ParamSite("query", key, exact=(val == value))
    segments = [s for s in sp.path.split("/") if s]
    for i, seg in enumerate(segments):
        if normalise(urllib.parse.unquote(seg)) == want:
            return ParamSite("path", str(i), exact=(urllib.parse.unquote(seg) == value))
    return ParamSite("none")


def error_document(recording: dict) -> str:
    """``"HTTP 404"`` when the page the recording ended on was served as an error.

    An error page can carry a repeated group that changes between loads (a
    request id, a host id), which a cross-input derivation then mistakes for
    a result list, and the page would be admitted as ground truth. Only the
    LAST response for the final URL counts, and only a 4xx/5xx status.
    """
    final = str(recording.get("final_url") or "").split("#")[0]
    if not final:
        return ""
    status = None
    for r in recording.get("network") or []:
        if str(r.get("url") or "").split("#")[0] == final and r.get("status") is not None:
            status = r.get("status")
    try:
        code = int(status) if status is not None else 0
    except (TypeError, ValueError):
        return ""
    return f"HTTP {code}" if code >= 400 else ""


def diagnose_failure(recording: dict) -> str:
    """Say WHY no template could be built, in the site's own terms.

    A bare "no eligible JSON response" hides the interesting distinction between
    "this page is server-rendered", "the call is a POST", "the call is signed",
    and "we were served an anti-bot challenge instead of the page". The
    benchmark reports these separately, so the compiler names them.
    """
    reqs = recording.get("network") or []
    url = str(recording.get("final_url") or "")

    # Shape-aware, not substring: a result page whose items mention "recaptcha"
    # is a served page (execution/blocked.py:challenge_served_instead).
    challenge = challenge_served_instead(
        (recording.get("ui") or {}).get("text") or "", url,
        [str(r.get("url") or "") for r in reqs],
        list((recording.get("ui") or {}).get("items") or []))
    if challenge:
        return ("anti-bot: an interstitial challenge (CAPTCHA / bot check) was served "
                f"instead of the page ({challenge})")

    json_posts = [r for r in reqs
                  if (r.get("method") or "").upper() not in READ_METHODS
                  and "json" in (r.get("mime") or "").lower()
                  and int(r.get("status") or 0) // 100 == 2]
    if json_posts:
        hosts = sorted({urllib.parse.urlsplit(r["url"]).netloc for r in json_posts})
        # the credential-name rule the compiler refuses keys by ("x-...-api-key")
        from anyuse.execution.api_step import signed_param_reason
        signed = any(signed_param_reason(k)
                     for r in json_posts
                     for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(r["url"]).query))
        return ("the results come from a non-read request (POST) to "
                + ", ".join(hosts)
                + (" carrying a per-client API key" if signed else "")
                + " — a POST is compiled only by the read-only POST rung, and "
                  "only when it is a GraphQL query or a search the page issued "
                  "once, with no credential anywhere in it")

    signed_gets = [r for r in reqs if _ineligible_reason(r.get("url", ""))]
    if signed_gets:
        return f"the explaining request is signed/session-bound: {_ineligible_reason(signed_gets[0]['url'])}"

    html = sum(1 for r in reqs if "text/html" in (r.get("mime") or "").lower())
    jsons = sum(1 for r in reqs if "json" in (r.get("mime") or "").lower())
    if html and not jsons:
        return f"server-rendered HTML: {html} document response(s), no JSON call at all"
    if jsons:
        if _labels_in_html(recording):
            return (f"{jsons} JSON response(s) present but none explains the visible list "
                    f"(e.g. only facets/telemetry); the result list itself is server-rendered")
        return (f"{jsons} JSON response(s) present but none carries the visible labels "
                f"(only facets/telemetry, or the request that fills the list was not captured)")
    return "no eligible JSON response in the recording"


def _labels_in_html(recording: dict) -> bool:
    """Is the visible list in a captured HTML document (server-rendered), rather
    than filled in by a request after the page loaded? At least half the visible
    labels stand in one document as whole words (:func:`contains_words`)."""
    labels = visible_items(recording)
    if not labels:
        return False
    for r in recording.get("network") or []:
        if "text/html" not in (r.get("mime") or "").lower():
            continue
        body = normalise(str(r.get("body") or ""))
        if body and sum(1 for label in labels
                        if label in body and contains_words(body, label)) * 2 >= len(labels):
            return True
    return False


def _volatile(key: str, value: str) -> bool:
    low = key.lower()
    if low in VOLATILE_PARAM_KEYS or any(low.startswith(p) for p in VOLATILE_PARAM_PREFIXES):
        return True
    return bool(_EPOCHY.match(value or "") or _HEXY.match(value or ""))


# ---------------------------------------------------------------------------
# evidence-based volatile parameters — lever (b)
# ---------------------------------------------------------------------------

@dataclass
class VolatileEvidence:
    """What several recordings of DIFFERENT queries say about each query key.

    The name rule decides "is this parameter noise?" from the key's
    *name* (`_`, `cb`, `ts`, `r`, `random`, …) and from its value looking like an
    epoch or a hex blob. That is a guess, and it is wrong in both directions: a
    site whose API really takes ``r=1`` or ``ts=week`` has that parameter
    silently stripped and the template then answers a different question, while a
    cache-buster called ``nonce2`` sails straight through.

    With two recordings of two different queries there is evidence instead. A key
    whose value **changed between recordings that differ only in the query** is
    noise the page regenerates. A key whose value was **the same in both** is
    part of the question and is kept, whatever it is called.
    """
    #: (host, path, key) whose value differed between recordings
    varying: set = field(default_factory=set)
    #: (host, path, key) seen with the same value in >= 2 recordings
    stable: set = field(default_factory=set)

    def decide(self, url: str, key: str, value: str) -> bool:
        """True when this query key should be dropped from the template."""
        sp = urllib.parse.urlsplit(url)
        ident = (sp.netloc, sp.path, key)
        if ident in self.varying:
            return True
        if ident in self.stable:
            # Evidence says it is part of the question: never drop it on a name.
            return False
        return _volatile(key, value)


def volatile_evidence(recordings: List[dict]) -> VolatileEvidence:
    """Build :class:`VolatileEvidence` from recordings of different queries."""
    seen: Dict[Tuple[str, str, str], set] = {}
    param_values = {normalise((r.get("param") or {}).get("value", "")) for r in recordings}
    for rec in recordings:
        for req in rec.get("network") or []:
            url = req.get("url") or ""
            if not url.startswith("http"):
                continue
            sp = urllib.parse.urlsplit(url)
            for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
                if normalise(val) in param_values:
                    continue      # this key carries the question itself
                seen.setdefault((sp.netloc, sp.path, key), set()).add(val)
    ev = VolatileEvidence()
    for ident, vals in seen.items():
        (ev.varying if len(vals) > 1 else ev.stable).add(ident)
    return ev


# ---------------------------------------------------------------------------
# template emission
# ---------------------------------------------------------------------------


def urlify(request_url: str, param_name: str, param_value: str,
           extra_bindings: Optional[List[Tuple[str, str]]] = None,
           evidence: Optional[VolatileEvidence] = None,
           binding: Optional["BindingEvidence"] = None,
           report: Optional[dict] = None,
           force_site: Optional[Any] = None,
           nested: bool = False,
           slot_evidence: Optional[Any] = None,
           method: str = "GET"):
    """Turn a recorded URL into a template URL + query, shared by both rungs.

    Returns ``(url, query, dropped, used, "")`` or ``(None, {}, [], [], reason)``.
    The user's parameter MUST be locatable: a request that cannot be re-bound to
    a new value is a bookmark, not a compiled workflow. ``extra_bindings`` are
    ``(literal, placeholder)`` pairs for values an earlier chain step produced.
    ``evidence`` is lever (b): when present it decides which query keys are
    noise, and the name list is consulted only for keys it has never seen.

    ``binding`` is the cross-recording evidence about awkward
    carriers — a value spanning several path segments, or a delimited part of
    one. It is consulted **only when the plain whole-segment / whole-value
    binding finds nothing**, so every template the plain binding builds is built
    identically and any measured difference is attributable. ``report``, when
    given, is filled with what was chosen and what the runners-up were.

    ``nested`` (lever f) is consulted only after both of those found nothing:
    the value may then sit inside decoded layers of a query value
    (:func:`_nested_binding`). The URL is returned verbatim with its query
    string, the query dict is empty, and ``report["slots"]`` says where the
    value goes.
    """
    report = report if report is not None else {}
    site = locate_param(request_url, param_value)
    if force_site is not None or site.where == "none":
        alt, why = _evidence_binding(request_url, param_name, param_value,
                                     extra_bindings, evidence, binding, report,
                                     force_site=force_site)
        if alt is not None:
            return alt
        if nested and force_site is None:
            n_url, _body, _slots, n_dropped, n_why = _nested_binding(
                method, request_url, None, param_name, param_value, evidence,
                slot_evidence, report)
            if n_url is not None:
                return n_url, {}, n_dropped, [param_name], ""
            why = "; ".join(w for w in (why, f"nested layers: {n_why}" if n_why else "") if w)
        return None, {}, [], [], (
            f"parameter {param_name!r}={param_value!r} is not carried by "
            f"any whole path segment or query value of the request" + (f" — {why}" if why else ""))

    bindings: Dict[str, str] = {}
    for literal, name in (extra_bindings or []):
        if normalise(literal):
            bindings.setdefault(normalise(literal), name)
    bindings[normalise(param_value)] = param_name

    sp = urllib.parse.urlsplit(request_url)
    segments = [x for x in sp.path.split("/") if x]
    used: List[str] = []
    for i, seg in enumerate(segments):
        name = bindings.get(normalise(urllib.parse.unquote(seg)))
        if name and not (site.where == "path" and int(site.key) != i and name == param_name):
            segments[i] = "{%s}" % name
            used.append(name)
    path = "/" + "/".join(segments) if segments else "/"
    if segments and sp.path.endswith("/"):
        # "/result/" and "/result" are different resources: a server that
        # answers the first may 404 the second.
        path += "/"
    url = urllib.parse.urlunsplit((sp.scheme, sp.netloc, path, "", ""))

    query: Dict[str, str] = {}
    dropped: List[str] = []
    for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
        name = bindings.get(normalise(val))
        if name and (site.where != "query" or key == site.key or name != param_name):
            query[key] = "{%s}" % name
            used.append(name)
            continue
        drop = (evidence.decide(request_url, key, val) if evidence is not None
                else _volatile(key, val))
        if drop:
            dropped.append(key)
            continue
        query[key] = val
    report.setdefault("binding", {"where": site.where, "key": site.key,
                                  "span": 1, "transform": "identity",
                                  "describe": "whole "
                                  + ("query value" if site.where == "query"
                                     else "path segment")})
    return url, query, dropped, used, ""


def _binding_alternatives(candidate: "Candidate", param_name: str, param_value: str,
                          evidence, binding, report, chosen: RequestTemplate
                          ) -> List[RequestTemplate]:
    """Templates for the corroborated binding sites that were NOT chosen.

    They go into :attr:`CompileResult.alternatives`, so the fresh-input proof
    tries them when the first one fails. This is what makes "a wrong split dies
    in the proof" measurable instead of asserted: every one of these was
    supported by all recordings and only a live 404 or a mismatch can separate
    them.
    """
    from anyuse.execution.param_binding import BindingSite
    out: List[RequestTemplate] = []
    for raw in (report.get("binding_alternatives") or [])[:3]:
        site = BindingSite(where=raw["where"], key=raw["key"],
                           prefix=raw.get("prefix", ""), suffix=raw.get("suffix", ""),
                           transform=raw.get("transform", "identity"),
                           span=int(raw.get("span", 1)))
        tmpl, _why = build_template(candidate, param_name, param_value,
                                    evidence=evidence, binding=binding,
                                    report={}, force_site=site)
        if tmpl is not None and tmpl.url != chosen.url:
            tmpl.notes += f"; alternative binding: {site.describe()}"
            out.append(tmpl)
    return out


def _evidence_binding(request_url: str, param_name: str, param_value: str,
                      extra_bindings, evidence, binding, report, force_site=None):
    """The path binding: bind a value that is only PART of a path.

    Returns ``(urlify tuple, "")`` or ``(None, reason)``. Every site offered
    here was supported by EVERY recording; the runners-up are reported so the
    verifier can try them and so a reader can see what the proof killed.
    """
    from anyuse.execution.param_binding import (corroborated_sites, site_params,
                                                templated_url)
    if force_site is not None:
        sites, why = [force_site], ""
    else:
        sites, why = corroborated_sites(request_url, param_value, binding, param_name)
    if not sites:
        report["binding_refused"] = why
        return None, why
    chosen = sites[0]
    url_t = templated_url(request_url, chosen, param_name)
    if url_t is None:                            # cannot happen after corroboration
        report["binding_refused"] = "the corroborated site does not describe this URL"
        return None, report["binding_refused"]
    sp = urllib.parse.urlsplit(url_t)
    query: Dict[str, str] = {}
    dropped: List[str] = []
    for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
        if val == "{%s}" % param_name:
            query[key] = val
            continue
        drop = (evidence.decide(request_url, key, val) if evidence is not None
                else _volatile(key, val))
        if drop:
            dropped.append(key)
            continue
        query[key] = val
    path_params, transforms = site_params(chosen, param_name)
    report["binding"] = chosen.to_dict()
    report["binding_alternatives"] = [s.to_dict() for s in sites[1:4]]
    report["path_params"] = path_params
    report["param_transforms"] = transforms
    url = urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))
    return (url, query, dropped, [param_name], ""), ""


def _nested_binding(method: str, request_url: str, body: Optional[str],
                    param_name: str, param_value: str,
                    evidence: Optional[VolatileEvidence], slot_evidence,
                    report: dict, need_body: bool = False):
    """Lever (f): bind a value that sits inside decoded layers of the request.

    Returns ``(url, body, slots, dropped, "")`` or ``(None, None, [], [], reason)``.
    The slots are discovered through up to ``api_slots.MAX_SLOT_LAYERS``
    decodes; with recordings of other values (``slot_evidence``) a slot is kept
    only where every recording of the same endpoint carries its own value in
    the same place. The request must pass the read-only rules over every
    decoded layer (``api_slots.deep_refusal``). Volatile query pairs are
    dropped exactly as for a plain template, by splicing them out, so every
    other byte of the URL stays as recorded.
    """
    from anyuse.execution import api_slots as S
    slots, why = S.discover_slots(request_url, body, param_value, param_name)
    if not slots:
        return None, None, [], [], why
    slots, refused = S.corroborated(method, request_url, slots, slot_evidence)
    if slots:
        slots, b64_refused = S.annotate_b64(method, request_url, body, slots, slot_evidence)
        refused += b64_refused
    if not slots:
        return None, None, [], [], "; ".join(refused[:2])
    if need_body and not any(s.at[0].startswith(("form", "body")) for s in slots):
        return None, None, [], [], (f"the POST body does not carry the typed value "
                                    f"{param_value!r}, so it cannot be re-bound")
    why = S.deep_refusal(request_url, body)
    if why:
        return None, None, [], [], why
    containers = {S.parse_step(s.at[0]).key for s in slots if s.at[0].startswith("query")}
    head, query, _frag = S.split_url(request_url)
    drop: List[str] = []
    for key, _occ, pair in S.decoded_pairs(query or ""):
        if key in containers:
            continue
        try:
            val = S.pct_decode(query[pair.vstart:pair.vend], plus=True)
        except S.SlotError:
            continue
        if (evidence.decide(request_url, key, val) if evidence is not None
                else _volatile(key, val)):
            drop.append(key)
    url = S.drop_query_keys(S.join_url(head, query, None), drop)
    report["slots"] = [s.to_dict() for s in slots]
    report["binding"] = {"where": "nested", "key": "", "span": 1, "transform": "identity",
                         "describe": "nested slot " + S.describe(slots)}
    if refused:
        report["slots_refused"] = refused
    return url, body, slots, sorted(set(drop)), ""


def build_template(candidate: Candidate, param_name: str, param_value: str,
                   origin: str = "deterministic",
                   extra_bindings: Optional[List[Tuple[str, str]]] = None,
                   evidence: Optional[VolatileEvidence] = None,
                   binding: Optional["BindingEvidence"] = None,
                   report: Optional[dict] = None,
                   force_site: Optional[Any] = None,
                   nested: bool = False,
                   slot_evidence: Optional[Any] = None
                   ) -> Tuple[Optional[RequestTemplate], str]:
    """Emit a :class:`RequestTemplate` from a scored candidate.

    Returns ``(template, "")`` or ``(None, reason)``. The user's parameter must
    be locatable in the URL: a template that cannot be re-bound to a new value
    is not a compiled workflow, it is a bookmark.

    ``extra_bindings`` is ``[(literal_value, placeholder_name), ...]`` for values
    this request carries that came from an EARLIER response in a chain (a record
    id taken from a search result). They are substituted the same way the user's
    parameter is; unlike it, a missing one is not an error, because the same
    builder is used for single requests.
    """
    report = report if report is not None else {}
    url, query, dropped, used, why = urlify(candidate.url, param_name, param_value,
                                            extra_bindings, evidence=evidence,
                                            binding=binding, report=report,
                                            force_site=force_site, nested=nested,
                                            slot_evidence=slot_evidence,
                                            method=candidate.method)
    if url is None:
        return None, why

    paths = cover_paths(candidate)
    leaf_path = paths[0] if paths else candidate.best_path()
    list_path, item_key = _split_list_path(leaf_path)
    expect: Dict[str, Any] = {"status_in": [200], "content_type": "json"}

    if "[*]" in leaf_path and candidate.path_hits.get(leaf_path, 0) >= candidate.matched_items:
        # One list path explains the whole screen → a LIST workflow. ``list_path``
        # may legitimately be "" when the document IS the list (json_path("")
        # returns the document), so test for the marker, not for truthiness.
        shape = "list"
        extract = {"items": leaf_path}
        recorded = json_path(candidate.data, list_path)
        recorded_count = len(recorded) if isinstance(recorded, list) else 0
        expect.update({"path": list_path,
                       "min_items": max(1, recorded_count, candidate.matched_items)})
        if item_key:
            expect["required_keys"] = [item_key]
    else:
        # Several unrelated paths are needed → a DETAIL/FIELDS workflow.
        shape = "fields"
        extract = {f"field_{i + 1}": p for i, p in enumerate(paths)} or (
            {"field_1": leaf_path} if leaf_path else {})
        prefix = _common_prefix(paths or [leaf_path])
        expect.update({"path": prefix, "non_empty": True})

    site = report.get("binding") or {}
    tmpl = RequestTemplate(
        method=candidate.method, url=url, query=query,
        headers={"accept": "application/json"},
        extract=extract, shape=shape, expect=expect,
        params=sorted({param_name, *used}), origin=origin,
        path_params=dict(report.get("path_params") or {}),
        param_transforms=dict(report.get("param_transforms") or {}),
        slots=[dict(slot) for slot in (report.get("slots") or [])],
        notes=(f"compiled from recording; coverage="
               f"{candidate.coverage:.2f} of {candidate.total_items} visible items"
               + (f"; dropped volatile query params: {sorted(dropped)}" if dropped else "")
               + (f"; parameter bound to {site.get('describe')}"
                  if site.get("where") in ("path_run", "path_part", "nested") else "")
                + (f"; login=replay cookies {sorted(candidate.cookie_names)}"
                   if candidate.cookie_names else "")),
    )
    # Site-mode login=replay: keep cookie NAME references where the values were.
    # The values never enter the template (problems() still rejects a literal
    # Cookie header); they are imported into memory at replay time only.
    if candidate.cookie_names:
        tmpl.cookie_refs = list(candidate.cookie_names)
    # identity=browser replays the captured UA; carry it so nothing is invented.
    if candidate.browser_ua:
        tmpl.browser_ua = candidate.browser_ua
    problems = tmpl.problems()
    if problems:
        return None, "; ".join(problems)
    return tmpl, ""


def _split_list_path(leaf_path: str) -> Tuple[str, str]:
    """``packages[*].name`` → ``("packages", "name")``; ``results[*]`` → ``("results","")``."""
    if not leaf_path or "[*]" not in leaf_path:
        return "", ""
    head, _, tail = leaf_path.partition("[*]")
    return head, tail.lstrip(".")


def _common_prefix(paths: List[str]) -> str:
    """Longest shared dotted prefix of several leaf paths (``""`` if none)."""
    parts = [p.split(".") for p in paths if p]
    if not parts:
        return ""
    out: List[str] = []
    for chunk in zip(*parts):
        if len(set(chunk)) != 1 or "[" in chunk[0]:
            break
        out.append(chunk[0])
    return ".".join(out[:-1]) if len(out) == len(parts[0]) else ".".join(out)


# ---------------------------------------------------------------------------
# the compile entry point
# ---------------------------------------------------------------------------

@dataclass
class CompileResult:
    """Everything the compile step decided, and why."""
    #: ok | ambiguous | chained | embedded_json | html | failed
    #: (the rung ladder, best first: a JSON API beats embedded JSON beats
    #: an HTML selector — see _compile_document_rungs for why)
    status: str = "failed"
    reason: str = ""
    #: a RequestTemplate, or a RequestPlan when the workflow needs a chain —
    #: both carry the same surface (params/shape/extract/problems), so the
    #: executor, the verifier and the ladder never branch on which it is.
    template: Optional[Any] = None
    #: equally-plausible runners-up, already emitted as templates. The VERIFIER
    #: picks among these when the winner fails on fresh input — a deterministic
    #: stand-in for the typed decision layer noted below.
    alternatives: List[RequestTemplate] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    param_name: str = ""
    param_value: str = ""
    #: set when >=2 candidates are within AMBIGUITY_EPS — the place a small typed
    #: decision model would replace the threshold (not wired)
    typed_decision_would_help: bool = False
    #: labels dropped as page chrome (see page_chrome), for the report — a
    #: reader should be able to see what was taken out of the ground truth
    chrome_dropped: List[str] = field(default_factory=list)
    #: Where the parameter was bound, and which corroborated
    #: runners-up the verifier may try. Reported so "the proof killed a wrong
    #: split" is a countable event rather than a claim.
    binding: Dict[str, Any] = field(default_factory=dict)
    binding_alternatives: List[Dict[str, Any]] = field(default_factory=list)
    #: how many alternative binding sites the fresh-input proof rejected
    binding_rejected: int = 0
    #: which reading of the visible list explained the request: "rows" (each
    #: result row's whole text) or "labels" (each row's own name; see
    #: html_extract.row_label). The proof is held to the same reading.
    ground_truth_reading: str = "rows"
    #: how many requests the fresh-input proof sends, ``(fewest, most)``: set by
    #: ``api_verify.compile_and_verify`` before its preflight; ``(0, 0)`` before that
    proof_requests: Tuple[int, int] = (0, 0)
    #: the recording holds a chain that no second recorded input corroborated
    #: (:data:`CHAIN_UNCORROBORATED`): a fitting recording of another input can
    #: compile it (``api_verify.chain_fitting``)
    chain_needs_fitting: bool = False
    #: why no fitting input generated from the site's own search fitted the chain
    #: (``execution/chain_fit.py``); empty when none was tried
    chain_fit_note: str = ""

    def to_dict(self) -> dict:
        out = {
            "status": self.status, "reason": self.reason,
            "template": self.template.to_dict() if self.template else None,
            "alternatives": [t.to_dict() for t in self.alternatives],
            "param": {"name": self.param_name, "value": self.param_value},
            "candidates": [c.summary() for c in self.candidates[:8]],
            "typed_decision_would_help": self.typed_decision_would_help,
            "chrome_dropped": list(self.chrome_dropped),
            "binding": dict(self.binding),
            "binding_alternatives": list(self.binding_alternatives),
            "binding_rejected": self.binding_rejected,
            "ground_truth_reading": self.ground_truth_reading,
        }
        if self.chain_fit_note:
            out["chain_fit_note"] = self.chain_fit_note
        return out


def compile_recording(recording: dict, param_name: str = "", param_value: str = "",
                      allow_chain: bool = True,
                      chrome: Optional[Any] = None,
                      levers: Optional["Levers"] = None,
                      evidence: Optional[VolatileEvidence] = None,
                      binding: Optional["BindingEvidence"] = None,
                      chain_recordings: Optional[List[dict]] = None,
                      slot_evidence: Optional[Any] = None) -> CompileResult:
    """Deterministically compile one recording into a candidate template or plan.

    ``allow_chain`` (default True) lets the compiler emit a two-step
    :class:`RequestPlan` when the explaining request carries a value an earlier
    response produced. ``chain_recordings`` supplies fitting recordings of
    other inputs: the producer/consumer relation must recur on at least two
    distinct inputs, and it must be distinguishable from binding the caller's
    input directly. Set ``allow_chain`` false to force single-request behavior.

    ``chrome`` is a set of labels to ignore in the visible list — see
    :func:`page_chrome`. ``compile_and_verify`` computes it from the recording
    plus the fresh ones, since it is the only caller that holds several
    recordings of different queries.

    ``levers`` selects the compile levers (default: all on;
    :meth:`Levers.none` turns them all off). ``evidence`` is lever (b)'s
    cross-recording view of which query parameters are noise; like ``chrome`` it
    can only be computed by a caller holding more than one recording.

    ``slot_evidence`` (``api_slots.SlotEvidence``) is lever (f)'s
    cross-recording view of where a nested value sits; without it a nested slot
    stands on this recording alone, as a plain query binding does (never one
    inside a path segment).

    Lever (f) is a fallback for the WHOLE ladder, not per candidate: the
    recording is compiled with nested slots off first, and only when nothing at
    all compiled is it compiled again with them on. So every recording the
    other levers compile yields exactly the template it did without lever (f),
    whichever rung or candidate a nested slot would have favoured.
    """
    kwargs = dict(param_name=param_name, param_value=param_value, allow_chain=allow_chain,
                  levers=levers, evidence=evidence, binding=binding,
                  slot_evidence=slot_evidence)
    res = _compile_with_levers(recording, chrome=chrome, chain_recordings=chain_recordings,
                               **kwargs)
    labelled = labels_reading(recording)
    if res.template is not None or labelled is None:
        return res
    # The whole result rows explain no request (a row carries a version, a
    # description and counts next to its name). Read each row by its own name
    # instead, as the request's field carries it; the proof then uses the same
    # reading (compile_and_verify).
    chain = ([labels_reading(r) or r for r in chain_recordings]
             if chain_recordings else chain_recordings)
    alt_chrome = page_chrome([labelled, *chain]) if (chrome and chain) else chrome
    alt = _compile_with_levers(labelled, chrome=alt_chrome, chain_recordings=chain, **kwargs)
    if alt.template is None:
        return res
    alt.ground_truth_reading = "labels"
    return alt


def labels_reading(recording: dict) -> Optional[dict]:
    """The recording with each result row read by its own name (``ui.labels``),
    or None when it has no such reading or it is the same as the whole rows."""
    ui = recording.get("ui") or {}
    labels = ui.get("labels")
    if not labels or list(labels) == list(ui.get("items") or []):
        return None
    return {**recording, "ui": {**ui, "items": list(labels)}}


def _compile_with_levers(recording: dict, param_name: str = "", param_value: str = "",
                         allow_chain: bool = True, chrome: Optional[Any] = None,
                         levers: Optional["Levers"] = None,
                         evidence: Optional[VolatileEvidence] = None,
                         binding: Optional["BindingEvidence"] = None,
                         chain_recordings: Optional[List[dict]] = None,
                         slot_evidence: Optional[Any] = None) -> CompileResult:
    levers = levers if levers is not None else DEFAULT_LEVERS
    kwargs = dict(param_name=param_name, param_value=param_value, allow_chain=allow_chain,
                  chrome=chrome, evidence=evidence, binding=binding,
                  chain_recordings=chain_recordings)
    if not getattr(levers, "nested_slots", False):
        return _compile_recording_once(recording, levers=levers, nested=False, **kwargs)
    import dataclasses
    plain = _compile_recording_once(recording, nested=False, **kwargs,
                                    levers=dataclasses.replace(levers, nested_slots=False))
    if plain.template is not None:
        return plain
    return _compile_recording_once(recording, levers=levers, nested=True,
                                   slot_evidence=slot_evidence, **kwargs)


def _compile_recording_once(recording: dict, param_name: str = "", param_value: str = "",
                            allow_chain: bool = True,
                            chrome: Optional[Any] = None,
                            levers: Optional["Levers"] = None,
                            evidence: Optional[VolatileEvidence] = None,
                            binding: Optional["BindingEvidence"] = None,
                            chain_recordings: Optional[List[dict]] = None,
                            slot_evidence: Optional[Any] = None,
                            nested: bool = False) -> CompileResult:
    """One pass of :func:`compile_recording`, with nested slots ``nested``."""
    levers = levers if levers is not None else DEFAULT_LEVERS
    param = recording.get("param") or {}
    param_name = param_name or param.get("name") or "q"
    param_value = param_value or param.get("value") or ""
    res = CompileResult(param_name=param_name, param_value=param_value)

    items = visible_items(recording, exclude=chrome)
    if search_to_home(recording, param_value):
        # the site redirected the typed search to its home page: whatever the
        # home page lists, it lists for every query
        res.reason = SEARCH_TO_HOME
        return res
    if not items and chrome and visible_items(recording):
        # Everything the recorder saw ALSO appears for the other queries. That
        # is not a compiler problem and it is not an empty page: it means the
        # page did not respond to the parameter at all — either the selector
        # only ever caught navigation and footer, or the site ignored the query
        # (a redirect to a generic listing does this). Saying "no result list"
        # here would send the reader looking in the wrong place; a rung compiled
        # from this recording would "work" on every input by returning the same
        # constant, which is a bookmark wearing a search's clothes.
        res.reason = (
            f"every one of the {len(visible_items(recording))} visible labels also "
            f"appears for the OTHER recorded queries, so nothing on screen answers "
            f"the parameter. Either the result selector matched only page chrome "
            f"(navigation, footer, banners) or the site ignored the parameter and "
            f"served a generic listing — check the recording's final_url. Examples "
            f"dropped: {sorted(chrome)[:4]}")
        return res
    if not items:
        # Nothing on screen means there is nothing to explain AND no ground
        # truth to prove equivalence against — distinguish "the site blocked
        # us" from "the UI simply rendered nothing here".
        diag = diagnose_failure(recording)
        if recording.get("recorder_warning"):
            res.reason = f"unusable recording: {recording['recorder_warning']}"
            return res
        res.reason = ("the recording shows no result list — " + diag
                      if diag.startswith("anti-bot")
                      else "the recording shows no result list, so there is nothing to explain "
                           "and no ground truth for an equivalence proof"
                           + (f" ({len(eligible_requests(recording))} JSON response(s) were "
                              f"observed, so an endpoint exists but the UI half is missing)"
                              if eligible_requests(recording) else ""))
        return res

    requests = eligible_requests(recording)
    if not requests:
        # No JSON API to explain the screen — walk down the ladder:
        # embedded JSON (data, addressed by JSON paths) before HTML selectors
        # (presentation). Three of seven measured failures landed here.
        doc_report: Dict[str, Any] = {}
        tmpl, why = _compile_document_rungs(recording, items, param_name, param_value,
                                            levers=levers, evidence=evidence,
                                            binding=binding, report=doc_report,
                                            slot_evidence=slot_evidence)
        if tmpl is not None:
            res.template = tmpl
            res.status = "embedded_json" if tmpl.extract_kind == "embedded_json" else "html"
            res.reason = tmpl.notes
            res.binding = dict(doc_report.get("binding") or {})
            res.binding_alternatives = list(doc_report.get("binding_alternatives") or [])
            return res
        keyed, why_keyed = ((None, "") if not page_keys_enabled() else
                            compile_page_keyed_post(recording, items, param_name, param_value,
                                                    levers=levers, evidence=evidence))
        if keyed is not None:
            res.template = keyed
            res.status = "page_keyed"
            res.reason = keyed.notes
            return res
        post, why_post = ((None, "") if not levers.readonly_post else
                          compile_readonly_post(recording, items, param_name,
                                                param_value, evidence=evidence,
                                                nested=nested,
                                                slot_evidence=slot_evidence))
        if post is not None:
            res.template = post
            res.status = "readonly_post"
            res.reason = post.notes
            return res
        res.reason = (diagnose_failure(recording)
                      + (f" ({why})" if why else "")
                      + (f" [read-only POST rung: {why_post}]" if why_post else "")
                      + (f" [page-keyed POST: {why_keyed}]" if why_keyed else ""))
        return res

    scored = sorted((score_candidate(r, items) for r in requests),
                    key=lambda c: (-c.coverage, c.bytes))
    res.candidates = scored

    viable = [c for c in scored if c.coverage >= MIN_COVERAGE and not c.ineligible]
    if not viable:
        best = scored[0]
        blocked = next((c for c in scored if c.ineligible and c.coverage >= MIN_COVERAGE), None)
        doc_report: Dict[str, Any] = {}
        tmpl, _why = _compile_document_rungs(recording, items, param_name, param_value,
                                             levers=levers, evidence=evidence,
                                             binding=binding, report=doc_report,
                                             slot_evidence=slot_evidence)
        if tmpl is not None:
            res.template = tmpl
            res.status = "embedded_json" if tmpl.extract_kind == "embedded_json" else "html"
            res.reason = tmpl.notes
            res.binding = dict(doc_report.get("binding") or {})
            res.binding_alternatives = list(doc_report.get("binding_alternatives") or [])
            return res
        why_post = ""
        keyed, why_keyed = ((None, "") if not page_keys_enabled() else
                            compile_page_keyed_post(recording, items, param_name, param_value,
                                                    levers=levers, evidence=evidence))
        if keyed is not None:
            res.template = keyed
            res.status = "page_keyed"
            res.reason = keyed.notes
            return res
        if levers.readonly_post:
            post, why_post = compile_readonly_post(recording, items, param_name,
                                                   param_value, evidence=evidence,
                                                   nested=nested,
                                                   slot_evidence=slot_evidence)
            if post is not None:
                res.template = post
                res.status = "readonly_post"
                res.reason = post.notes
                return res
        if blocked is not None:
            res.reason = f"best-explaining request is not replayable: {blocked.ineligible}"
        else:
            res.reason = (f"no request explains the screen (best coverage "
                          f"{best.coverage:.2f} < {MIN_COVERAGE} over {len(items)} items) — "
                          + diagnose_failure(recording))
        if why_post:
            res.reason += f" [read-only POST rung: {why_post}]"
        if why_keyed:
            res.reason += f" [page-keyed POST: {why_keyed}]"
        return res

    top = viable[0]
    rivals = [c for c in viable[1:]
              if abs(c.coverage - top.coverage) < AMBIGUITY_EPS
              and (c.host, urllib.parse.urlsplit(c.url).path)
              != (top.host, urllib.parse.urlsplit(top.url).path)]
    res.typed_decision_would_help = bool(rivals)

    # A request that sends a key the site's own page stated: read the key from
    # that page at run time (never stored) and send the request as the page did
    keyed_why = ""
    if top.page_keys and page_keys_enabled():
        keyed, keyed_why = compile_page_keyed(recording, top, param_name, param_value,
                                              evidence=evidence, binding=binding,
                                              nested=nested, slot_evidence=slot_evidence)
        if keyed is not None:
            res.template = keyed
            res.status = "page_keyed"
            res.reason = keyed.notes
            return res

    # A CHAIN first: if the request that explains the screen carries a value some
    # earlier response produced, one request cannot reproduce this workflow at
    # all, and compiling a single request would silently pin the recorded id.
    if allow_chain:
        plan, chain_why = compile_chain(
            recording, items, scored, param_name, param_value,
            evidence_recordings=chain_recordings)
        if plan is not None:
            res.template = plan
            res.status = "chained"
            res.reason = plan.notes
            return res
        if chain_why:
            res.reason = chain_why
            res.chain_needs_fitting = chain_why == CHAIN_UNCORROBORATED
            return res

    report: Dict[str, Any] = {}
    tmpl, why = build_template(top, param_name, param_value, evidence=evidence,
                               binding=binding, report=report, nested=nested,
                               slot_evidence=slot_evidence)
    if tmpl is None:
        # The winner cannot be re-bound; try the next viable candidate before
        # giving up (a detail endpoint often sits next to a list endpoint).
        for alt in viable[1:4]:
            report = {}
            tmpl, why2 = build_template(alt, param_name, param_value, evidence=evidence,
                                        binding=binding, report=report, nested=nested,
                                        slot_evidence=slot_evidence)
            if tmpl is not None:
                top, why = alt, ""
                break
            why = why or why2
    if tmpl is None:
        # the request that explains the screen does not carry the typed value: a
        # search the site redirected to a landing page whose values it sends
        landing, landing_why = compile_landing_plan(
            recording, items, scored, param_name, param_value,
            evidence_recordings=chain_recordings, evidence=evidence)
        if landing is not None:
            res.template = landing
            res.status = "landing"
            res.reason = landing.notes
            return res
        if landing_why:
            res.reason = landing_why
            res.chain_needs_fitting = landing_why == LANDING_UNCORROBORATED
            return res
        res.reason = why or "could not emit a template from any viable candidate"
        if keyed_why and top.page_keys:
            res.reason += f" [page-keyed: {keyed_why}]"
        return res

    res.template = tmpl
    res.binding = dict(report.get("binding") or {})
    res.binding_alternatives = list(report.get("binding_alternatives") or [])
    res.alternatives.extend(_binding_alternatives(top, param_name, param_value,
                                                  evidence, binding, report, tmpl))
    for alt in viable:
        if alt is top or len(res.alternatives) >= 3:
            continue
        alt_tmpl, _ = build_template(alt, param_name, param_value, evidence=evidence,
                                     binding=binding, nested=nested,
                                     slot_evidence=slot_evidence)
        if alt_tmpl is not None and alt_tmpl.url != tmpl.url:
            res.alternatives.append(alt_tmpl)
    res.status = "ambiguous" if rivals else "ok"
    if rivals:
        res.reason = (f"{len(rivals) + 1} requests explain the screen equally well "
                      f"({', '.join(c.host + urllib.parse.urlsplit(c.url).path for c in [top] + rivals)})"
                      f" — a typed decision layer would choose here; verification decides instead")
    return res



# ---------------------------------------------------------------------------
# the document rungs — embedded JSON, then HTML selectors
# ---------------------------------------------------------------------------

def _compile_document_rungs(recording: dict, items: List[str], param_name: str,
                            param_value: str, levers: "Levers" = DEFAULT_LEVERS,
                            evidence: Optional[VolatileEvidence] = None,
                            binding: Optional["BindingEvidence"] = None,
                            report: Optional[dict] = None,
                            slot_evidence: Optional[Any] = None
                            ) -> Tuple[Optional[RequestTemplate], str]:
    """Try the document-backed rungs in preference order and return the first hit.

    **embedded JSON before HTML selectors**, always, and not as a tie-break:
    both read the same response, but one addresses the page's data and the other
    its presentation. A class rename breaks the selector and leaves the payload
    untouched, so when both can explain the screen the payload is the better
    compile — even if the selector scored marginally higher on the recording,
    which is a measurement of one page, not of durability.
    """
    report = report if report is not None else {}
    nested = bool(getattr(levers, "nested_slots", False))
    tmpl, why_json = compile_embedded_json(recording, items, param_name, param_value,
                                           evidence=evidence, binding=binding,
                                           report=report, nested=nested,
                                           slot_evidence=slot_evidence)
    if tmpl is not None:
        return tmpl, ""
    tmpl, why_html = compile_html(recording, items, param_name, param_value,
                                  levers=levers, evidence=evidence, binding=binding,
                                  report=report, slot_evidence=slot_evidence)
    if tmpl is not None:
        return tmpl, ""
    return None, "; ".join(w for w in (why_json, why_html) if w)


def compile_embedded_json(recording: dict, items: List[str], param_name: str,
                          param_value: str,
                          evidence: Optional[VolatileEvidence] = None,
                          binding: Optional["BindingEvidence"] = None,
                          report: Optional[dict] = None,
                          nested: bool = False,
                          slot_evidence: Optional[Any] = None
                          ) -> Tuple[Optional[RequestTemplate], str]:
    """Compile a page that ships its data as JSON inside the HTML.

    Why this sits ABOVE the HTML rung in the ladder: the extraction is a JSON
    path into the page's own data structure, not a CSS chain into its markup. A
    redesign renames classes and re-nests elements — it rarely renames the
    hydration payload — so this survives the change that breaks a selector.

    The implementation deliberately reuses the JSON rung whole: every blob found
    in the document becomes a :class:`Candidate` scored by the same value
    overlap, and :func:`build_template` emits the same list/fields extraction,
    the same postcondition and the same parameter binding. Only three things
    differ: the request asks for HTML, ``extract_kind`` says where the JSON came
    from, and ``embedded`` records which payload to re-find on replay.
    """
    from anyuse.execution.embedded_json import describe_locator, find_embedded

    docs = eligible_html_requests(recording)
    if not docs:
        return None, "no 2xx HTML document in the recording"

    found_any = False
    best: Tuple[float, Optional[RequestTemplate]] = (0.0, None)
    for doc in docs:
        blobs = find_embedded(doc.get("body") or "")
        if blobs:
            found_any = True
        for blob in blobs:
            cand = score_candidate(
                {"url": doc["url"], "method": doc.get("method", "GET"),
                 "status": doc.get("status", 200), "mime": doc.get("mime", ""),
                 "bytes": blob.raw_chars, "_data": blob.data},
                items)
            if cand.coverage < MIN_COVERAGE or cand.ineligible:
                continue
            if cand.coverage <= best[0]:
                continue
            sub: Dict[str, Any] = {}
            tmpl, _why = build_template(cand, param_name, param_value,
                                        evidence=evidence, binding=binding,
                                        report=sub, nested=nested,
                                        slot_evidence=slot_evidence)
            if tmpl is None:
                continue
            if report is not None:
                report.update(sub)
            tmpl.extract_kind = "embedded_json"
            tmpl.embedded = dict(blob.locator)
            tmpl.headers = {"accept": "text/html"}
            # The DOCUMENT is HTML; the postcondition checks the payload inside it.
            tmpl.expect = {**tmpl.expect, "content_type": "html"}
            tmpl.notes = (
                f"compiled from JSON embedded in the page — {describe_locator(blob.locator)}"
                f" ({blob.raw_chars:,} chars); coverage={cand.coverage:.2f} of "
                f"{len(items)} visible items. Preferred over an HTML selector "
                f"because it reads the page's DATA, not its markup, so a "
                f"redesign does not break it. Not a published interface either: "
                f"if the payload disappears the postcondition fails and the rung "
                f"falls back to the recorded UI path.")
            if tmpl.problems():
                continue
            best = (cand.coverage, tmpl)
    if best[1] is not None:
        return best[1], ""
    if found_any:
        return None, ("the page embeds JSON, but no payload explained the visible "
                      "list well enough to compile")
    return None, "no embedded JSON payload in the document"


# ---------------------------------------------------------------------------
# the HTML rung — one GET into a server-rendered document
# ---------------------------------------------------------------------------

#: An HTML rung's selector must not match most of the page.
HTML_MAX_MATCHES = 60


def eligible_html_requests(recording: dict) -> List[dict]:
    """Document responses a template could be built from: 2xx GET, https, HTML."""
    out = []
    for r in recording.get("network") or []:
        if (r.get("method") or "GET").upper() not in READ_METHODS:
            continue
        status = r.get("status")
        if not status or not (200 <= int(status) < 300):
            continue
        if not str(r.get("url", "")).startswith("https://"):
            continue
        if "html" not in (r.get("mime") or "").lower():
            continue
        if not r.get("body"):
            continue
        out.append(r)
    return out


def compile_html(recording: dict, items: List[str], param_name: str,
                 param_value: str, levers: "Levers" = DEFAULT_LEVERS,
                 evidence: Optional[VolatileEvidence] = None,
                 binding: Optional["BindingEvidence"] = None,
                 report: Optional[dict] = None,
                 slot_evidence: Optional[Any] = None
                 ) -> Tuple[Optional[RequestTemplate], str]:
    """Compile a server-rendered workflow: one GET plus an HTML selector.

    Only the document whose own URL carries the typed value is considered — the
    page the user's query produced, not some other page that happens to be in
    the log. The selector is derived from where the on-screen values actually
    sit (``html_extract.derive_selector``) and is rejected if it matches most of
    the page, which is what a bare ``a`` or ``li`` would do.

    With lever (a) on, a second derivation runs from the page's *repeated
    structure* (``derive_selector_repeated``) and whichever explains more of the
    visible list wins. The evidence for why: on code hosts and package
    registries the value-first derivation collapsed to a bare ``a``, which is then correctly
    rejected as unspecific — so a list sitting plainly in the document compiled
    to nothing at all.

    The bodies are raw server responses, so they are read with the implied
    end tags, as a browser reads them, and the template records that tree
    (``html_parser``) for the calls that follow.
    """
    from anyuse.execution.html_extract import (
        HTML5_PARSER, derive_selector, derive_selector_repeated, selector_is_one_list,
        selector_is_specific)

    docs = eligible_html_requests(recording)
    if not docs:
        return None, "no 2xx HTML document in the recording"

    best: Tuple[float, Optional[RequestTemplate], str] = (0.0, None, "")
    for doc in docs:
        sub: Dict[str, Any] = {}
        approved: List[str] = []
        hop = value_hop(doc.get("redirected_from") or [], param_value)
        if not hop.startswith("https://") or (urllib.parse.urlsplit(hop).path.rstrip("/")
                                              == urllib.parse.urlsplit(str(doc["url"])).path.rstrip("/")):
            # an http-to-https upgrade, a www sibling or a trailing slash: the page the
            # search landed on is the search URL itself
            hop = ""
        if hop:
            # a search the site redirected to a landing page: the search URL the site
            # formed, never a template guessed from the landing page's own URL, and the
            # landing page it redirects to (the site's own hosts only)
            from anyuse.execution.request_policy import origin as request_origin
            from anyuse.execution.site_canon import leaves_site
            url, query, dropped, _used, why = urlify(hop, param_name, param_value,
                                                     evidence=evidence, report=sub, method="GET")
            if url is not None and leaves_site(str(doc["url"]), hop):
                url = None
            if url is not None and request_origin(str(doc["url"])) != request_origin(hop):
                approved = [request_origin(str(doc["url"]))]
        else:
            url, query, dropped, _used, why = urlify(doc["url"], param_name, param_value,
                                                     evidence=evidence, binding=binding,
                                                     report=sub,
                                                     nested=bool(getattr(levers, "nested_slots",
                                                                         False)),
                                                     slot_evidence=slot_evidence,
                                                     method="GET")
        if url is None:
            continue
        attempts = [("values", *derive_selector(doc["body"], items, implied_end_tags=True))]
        if levers.repetition_selector:
            attempts.append(("repetition",
                             *derive_selector_repeated(doc["body"], items,
                                                       implied_end_tags=True)))
        for how, selector, coverage, _vals in attempts:
            if not selector:
                continue
            if not (selector_is_specific(doc["body"], selector, HTML_MAX_MATCHES,
                                         implied_end_tags=True)
                    or selector_is_one_list(doc["body"], selector, implied_end_tags=True)):
                continue
            if coverage < MIN_COVERAGE or coverage <= best[0]:
                continue
            tmpl = RequestTemplate(
                method="GET", url=url, query=query,
                headers={"accept": "text/html"},
                extract={"items": selector}, extract_kind="html", shape="list",
                expect={"status_in": [200], "content_type": "html",
                        "selector": selector, "min_items": max(1, len(_vals))},
                params=[param_name], origin="deterministic",
                path_params=dict(sub.get("path_params") or {}),
                param_transforms=dict(sub.get("param_transforms") or {}),
                slots=[dict(slot) for slot in (sub.get("slots") or [])],
                html_parser=HTML5_PARSER, approved_origins=list(approved),
                notes=(f"compiled from server-rendered HTML ({how} derivation); "
                       f"selector {selector!r} covers {coverage:.2f} of "
                       f"{len(items)} visible items"
                       + (f"; dropped query params: {sorted(dropped)}" if dropped else "")
                       + ". Bound to markup, not to a published interface: a class "
                         "rename breaks it, the postcondition catches it, and the "
                         "rung falls back to the recorded UI path."),
            )
            if tmpl.problems():
                continue
            best = (coverage, tmpl, "")
            if report is not None:
                report.update(sub)
    if best[1] is None:
        return None, ("a server-rendered document was present but no specific selector "
                      "explained the visible list")
    return best[1], ""


# ---------------------------------------------------------------------------
# the read-only POST rung — lever (c)
# ---------------------------------------------------------------------------
#
# The one measured `keyed_post` case is the motivating evidence and, as it turns
# out, also the case this rung REFUSES: the search is a hosted search provider's
# `multi_search` POST whose URL carries the provider's `x-...-api-key`. That
# is the point. A rung that accepts everything a site happens to POST is not a
# rung, it is a hole; this one states what it will not do and then does not do
# it.

#: How many times the page may have issued the same POST during one recording.
#: Twice is a retry or a second page; the compiler cannot tell which body is
#: "the search", so it refuses rather than guessing.
READONLY_POST_MAX_OCCURRENCES = 1


def _post_credential_reason(req: dict, exempt: Sequence[str] = ()) -> str:
    """Why this recorded POST may never be replayed, or ``""``.

    Checked over the URL query, the request headers the recorder captured and
    the body's own keys — all three, because a credential in any one of them
    makes the request session-bound and therefore neither safe nor reproducible.
    ``exempt``: the ``at`` of the page keys the site's page stated
    (``page_literals.mark_network``); their recorded values are ``<redacted>``.
    """
    from anyuse.execution.api_step import CREDENTIAL_HEADERS, signed_param_reason
    from anyuse.execution.page_literals import REDACTED

    keyed = set(exempt)
    url = req.get("url") or ""
    for key, val in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                           keep_blank_values=True):
        if f"query:{key}" in keyed and val == REDACTED:
            continue
        why = signed_param_reason(key)
        if why:
            return why + " (in the request's query string)"
    for name in (req.get("headers") or {}):
        low = str(name).lower()
        if low in CREDENTIAL_HEADERS:
            return (f"the recorded request carries the credential header "
                    f"{name!r} — it only works as a signed-in session")
        why = signed_param_reason(low)
        if why:
            return why + " (a request header)"
    body = req.get("post_data") or ""
    body_keyed = {at.split(":", 1)[1].rsplit(".", 1)[-1] for at in keyed if at.startswith("body:")}
    for key, val in _post_body_keys(body):
        if key in body_keyed and val == REDACTED:
            continue
        why = signed_param_reason(key)
        if why:
            return why + " (in the POST body)"
    return ""


def _post_body_keys(body: str) -> List[Tuple[str, Any]]:
    """(key, value) pairs of a JSON or form body, nested keys included."""
    out: List[Tuple[str, Any]] = []
    raw = (body or "").strip()
    if not raw:
        return out
    if raw[:1] in ("{", "["):
        try:
            data = json.loads(raw)
        except ValueError:
            return out

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    out.append((str(k), v))
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
        return out
    try:
        return [(k, v) for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True)]
    except Exception:
        return out


def _bind_in_body(body: str, param_value: str, param_name: str) -> Tuple[Optional[str], str]:
    """Replace the recorded query value inside a POST body with ``{param}``.

    Returns ``(body_template, "")`` or ``(None, reason)``. The substitution is
    structural for JSON (walk the decoded document, replace the scalar, re-emit)
    and pair-wise for a form body — never a blind string replace, which would
    happily corrupt a body whose query value also occurs inside a field name.
    """
    raw = (body or "").strip()
    want = normalise(param_value)
    if not want:
        return None, "no parameter value to bind"
    if raw[:1] in ("{", "["):
        try:
            data = json.loads(raw)
        except ValueError:
            return None, "POST body is not valid JSON"
        hits = [0]

        def walk(node):
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v) for v in node]
            if isinstance(node, str) and normalise(node) == want:
                hits[0] += 1
                return "{%s}" % param_name
            return node

        bound = walk(data)
        if not hits[0]:
            return None, (f"the POST body does not carry the typed value "
                          f"{param_value!r}, so it cannot be re-bound")
        return json.dumps(bound, separators=(",", ":"), ensure_ascii=False), ""
    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    if not pairs:
        return None, "POST body is neither valid JSON nor a form encoding"
    hits = 0
    out = []
    for k, v in pairs:
        if normalise(v) == want:
            out.append((k, "{%s}" % param_name))
            hits += 1
        else:
            out.append((k, v))
    if not hits:
        return None, (f"the POST body does not carry the typed value "
                      f"{param_value!r}, so it cannot be re-bound")
    return "&".join(f"{urllib.parse.quote(k)}={v if v.startswith('{') else urllib.parse.quote(v)}"
                    for k, v in out), ""


def readonly_post_candidates(recording: dict) -> List[dict]:
    """POSTs the recording shows the page issuing exactly once, with a body."""
    posts: Dict[Tuple[str, str], List[dict]] = {}
    for r in recording.get("network") or []:
        if (r.get("method") or "GET").upper() != "POST":
            continue
        status = r.get("status")
        if not status or not (200 <= int(status) < 300):
            continue
        url = str(r.get("url") or "")
        if not url.startswith("https://"):
            continue
        if not r.get("post_data") or not r.get("body"):
            continue
        sp = urllib.parse.urlsplit(url)
        posts.setdefault((sp.netloc, sp.path), []).append(r)
    return [reqs[0] for reqs in posts.values()
            if len(reqs) <= READONLY_POST_MAX_OCCURRENCES]


def compile_readonly_post(recording: dict, items: List[str], param_name: str,
                          param_value: str,
                          evidence: Optional[VolatileEvidence] = None,
                          nested: bool = False,
                          slot_evidence: Optional[Any] = None
                          ) -> Tuple[Optional[RequestTemplate], str]:
    """Compile a search that the page performs with a POST — lever (c).

    Accepted only when ALL of these hold, and the refusal names the one that
    failed:

    1. the POST is 2xx, https, has a body and a body in the response;
    2. the page issued it **once** during the recording (once per query), so it
       is the search and not a retry, a batch or a write;
    3. the body parses as JSON or as a form encoding, under the size cap;
    4. if it is GraphQL, the document is a ``query`` — any ``mutation`` or
       ``subscription`` anywhere in it refuses the whole request;
    5. nothing about it carries a credential: no signed/keyed query parameter,
       no ``Cookie``/``Authorization``/``X-Api-Key`` header, no credential-ish
       key in the body;
    6. the body carries the typed value, so it can be re-bound;
    7. the response explains the visible list at least as well as a GET
       candidate would have to (``MIN_COVERAGE``).

    Rules 4 and 5 hold over EVERY decoded layer of the request, not only the
    top one (``api_slots.deep_refusal``): JSON inside a string or a form
    field, base64 JSON, a nested URL or form string, percent-encoded text. A
    GraphQL mutation/subscription in any of them, a credential-ish key or a
    credential-shaped value anywhere refuses the request, and so does a
    request whose layers go deeper than the inspection cap.

    With ``nested`` (lever f), rule 6 may be met by a value inside decoded
    layers of the body: the template then keeps the captured body verbatim and
    fills a slot (``RequestTemplate.slots``).

    The template is still only a candidate: ``api_verify`` proves it on fresh
    input like every other rung, and a POST that cannot be proved is discarded.
    """
    cands = readonly_post_candidates(recording)
    if not cands:
        return None, "no single-occurrence 2xx POST with a body in the recording"

    reasons: List[str] = []
    best: Tuple[float, Optional[RequestTemplate]] = (0.0, None)
    for req in cands:
        host = urllib.parse.urlsplit(req["url"]).netloc
        why = _post_credential_reason(req)
        if why:
            reasons.append(f"{host}: {why}")
            continue
        kind, op, shape_why = _readonly_post_shape(req.get("post_data") or "")
        if shape_why:
            reasons.append(f"{host}: {shape_why}")
            continue
        if kind == "graphql" and op not in ("query", "anonymous"):
            reasons.append(f"{host}: GraphQL operation is a {op!r}, not a query")
            continue
        from anyuse.execution.api_slots import deep_refusal
        deep_why = deep_refusal(req["url"], req.get("post_data") or "",
                                req.get("headers") or {})
        if deep_why:
            reasons.append(f"{host}: {deep_why}")
            continue
        try:
            data = json.loads(req["body"])
        except (ValueError, TypeError):
            reasons.append(f"{host}: the response is not JSON, so it cannot be extracted from")
            continue
        cand = score_candidate({**req, "_data": data}, items)
        if cand.coverage < MIN_COVERAGE:
            reasons.append(f"{host}: the response explains only "
                           f"{cand.coverage:.2f} of the visible list")
            continue
        body_tmpl, bind_why = _bind_in_body(req.get("post_data") or "",
                                            param_value, param_name)
        nested_url, nested_slots, nested_dropped = None, [], []
        if body_tmpl is None and nested:
            sub: Dict[str, Any] = {}
            nested_url, _b, nested_slots, nested_dropped, n_why = _nested_binding(
                "POST", req["url"], req.get("post_data") or "", param_name, param_value,
                evidence, slot_evidence, sub, need_body=True)
            if nested_url is None:
                bind_why = f"{bind_why}; nested layers: {n_why}"
        if body_tmpl is None and nested_url is None:
            reasons.append(f"{host}: {bind_why}")
            continue
        sp = urllib.parse.urlsplit(req["url"])
        query: Dict[str, str] = {}
        dropped: List[str] = []
        for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
            drop = (evidence.decide(req["url"], key, val) if evidence is not None
                    else _volatile(key, val))
            if drop:
                dropped.append(key)
            else:
                query[key] = val
        tmpl, emit_why = build_template(cand, param_name, param_value,
                                        evidence=evidence)
        if tmpl is None:
            # The URL need not carry the value for a POST — the body does.
            tmpl = _post_template_from(cand, param_name)
            if tmpl is None:
                reasons.append(f"{host}: {emit_why}")
                continue
        tmpl.method = "POST"
        tmpl.url = urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))
        tmpl.query = query
        tmpl.body = body_tmpl
        if nested_url is not None:
            # schema 2.1: the captured URL and body verbatim, the value in slots
            from anyuse.execution.api_slots import describe
            from anyuse.execution.workflow_schema import input_spec
            tmpl.url, tmpl.query, dropped = nested_url, {}, nested_dropped
            tmpl.body = req.get("post_data") or ""
            tmpl.slots = [s.to_dict() for s in nested_slots]
            tmpl.parameter_schema = [input_spec(param_name).to_dict()]
        tmpl.readonly_post = True
        tmpl.headers = {"accept": "application/json",
                        "content-type": ("application/json" if kind in ("json", "graphql")
                                         else "application/x-www-form-urlencoded")}
        tmpl.params = [param_name]
        tmpl.notes = (
            f"compiled as a READ-ONLY POST ({kind}"
            + (f", operation {op}" if op else "") + f") to {host}{sp.path}; "
            f"coverage={cand.coverage:.2f} of {len(items)} visible items"
            + (f"; dropped query params: {sorted(dropped)}" if dropped else "")
            + (f"; parameter bound to nested slot {describe(nested_slots)}"
               if nested_url is not None else "")
            + ". The page issued this exact request once for this query and it "
              "carries no credential; it is still a POST, so it is refused "
              "outright by ANYUSE_API_READONLY_POST=off and it re-derives its "
              "own read-only claim from the body on every execution.")
        problems = tmpl.problems()
        if problems:
            reasons.append(f"{host}: " + "; ".join(problems))
            continue
        if cand.coverage > best[0]:
            best = (cand.coverage, tmpl)
    if best[1] is not None:
        return best[1], ""
    return None, ("; ".join(reasons[:3]) if reasons
                  else "no POST in the recording qualified as a read")


def _readonly_post_shape(body: str):
    from anyuse.execution.api_step import readonly_post_shape
    return readonly_post_shape(body)


def _post_template_from(cand: Candidate, param_name: str) -> Optional[RequestTemplate]:
    """A minimal template for a POST whose URL carries no parameter at all."""
    paths = cover_paths(cand)
    leaf = paths[0] if paths else cand.best_path()
    if not leaf:
        return None
    list_path, item_key = _split_list_path(leaf)
    expect: Dict[str, Any] = {"status_in": [200], "content_type": "json"}
    if list_path:
        expect["path"] = list_path
        recorded = json_path(cand.data, list_path)
        expect["min_items"] = max(
            1, len(recorded) if isinstance(recorded, list) else 0, cand.matched_items)
        if item_key:
            expect["required_keys"] = [item_key]
    return RequestTemplate(method="POST", url=cand.url, shape="list",
                           extract={"items": leaf}, extract_kind="json",
                           expect=expect, params=[param_name],
                           origin="deterministic")


# ---------------------------------------------------------------------------
# chains — a request whose URL carries a value an earlier response produced
# ---------------------------------------------------------------------------

#: Name given to the value a chain's first step hands to its second.
CHAIN_PARAM = "chain_id"


def _url_literals(url: str, exclude: str) -> List[str]:
    """Path segments and query values of ``url`` other than ``exclude``.

    These are the candidates for "a value some earlier response produced": an
    id, a slug, a key. Volatile and obviously structural pieces are skipped.
    """
    sp = urllib.parse.urlsplit(url)
    out: List[str] = []
    skip = normalise(exclude)
    for seg in sp.path.split("/"):
        seg = urllib.parse.unquote(seg)
        if seg and normalise(seg) and normalise(seg) != skip and long_enough(seg):
            out.append(seg)
    for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
        if val and normalise(val) != skip and not _volatile(key, val) and long_enough(val):
            out.append(val)
    return out


def find_producer(literal: str, candidates: List[Candidate],
                  consumer: Candidate) -> Tuple[Optional[Candidate], str]:
    """Which earlier response produced ``literal``, and at which path?

    Looks for an exact normalised leaf match in every OTHER eligible response.
    The path is generalised (``packages[*].id``); which row of that list a run
    binds (the first, or the one named by the input) is settled across the
    recordings by :func:`_row_rule`. Returns ``(candidate, path)`` or
    ``(None, "")``.
    """
    want = normalise(literal)
    best: Tuple[Optional[Candidate], str, int] = (None, "", -1)
    for cand in candidates:
        if cand is consumer or cand.url == consumer.url:
            continue
        if (cand.recording_index >= 0 and consumer.recording_index >= 0
                and cand.recording_index >= consumer.recording_index):
            continue
        for path, value in json_leaves(cand.data):
            if not isinstance(value, (str, int, float)):
                continue
            if normalise(value) != want:
                continue
            # Prefer a leaf inside a list (a result row) over a scalar at the
            # document root: the row is what the user clicked.
            score = 1 if "[" in path else 0
            if score > best[2]:
                best = (cand, generalise_path(path), score)
            break
    return best[0], best[1]


@dataclass
class _ChainObservation:
    """One recording's candidate producer → consumer dependency.

    ``data`` is the producer's response and ``opened`` the index of the row of
    the bound list whose key is ``literal`` (-1 when it cannot be located).
    """
    plan: RequestPlan
    literal: str
    input_value: str
    signature: Tuple[Any, ...]
    data: Any = None
    opened: int = -1
    #: JSON type of the producer value the URL carried (``workflow_schema.value_type``)
    key_type: str = "string"


def _opened_row(data: Any, path: str, literal: str) -> int:
    """Index of the row of ``path``'s list whose key is ``literal``, else -1."""
    want = normalise(literal)
    for index, _row, key in keyed_rows(data, path) or []:
        if normalise(key) == want:
            return index
    return -1


def _key_type(data: Any, path: str, literal: str) -> str:
    """JSON type of the value at ``path`` that the URL carried as ``literal``:
    ``integer``, ``number`` or ``string``."""
    from anyuse.execution.workflow_schema import value_type
    want = normalise(literal)
    rows = keyed_rows(data, path)
    if rows is not None:
        keys = [key for _index, _row, key in rows]
    else:
        found = json_path(data, path)
        keys = found if isinstance(found, list) else [found]
    for key in keys:
        if (isinstance(key, (str, int, float)) and not isinstance(key, bool)
                and normalise(key) == want):
            return value_type(key)
    return "string"


def _derived_type(observations: List[_ChainObservation]) -> str:
    """One JSON type for the chain value across the recordings: a number only
    when every recording's producer returned a number."""
    types = {o.key_type for o in observations}
    if types == {"integer"}:
        return "integer"
    if types <= {"integer", "number"}:
        return "number"
    return "string"


def _name_paths(row: Any, value: str) -> List[str]:
    """Fields of one row whose value is ``value`` once normalised."""
    want = normalise(value)
    if not want or not isinstance(row, dict):
        return []
    return [path for path, leaf in json_leaves(row)
            if "[" not in path and isinstance(leaf, (str, int, float))
            and not isinstance(leaf, bool) and normalise(leaf) == want
            and json_path(row, path) == leaf]


def _rule_text(rule: Dict[str, Any]) -> str:
    """How a row rule reads in a plan's notes."""
    field_ = rule.get("name_path") or "key"
    return (f"(the row whose {field_} is the input {rule.get('input')!r}: exact, then "
            "normalised" + (", else the first row" if rule.get("else_first") else "")
            + ")")


def _row_rule(observations: List[_ChainObservation], path: str,
              input_name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Which row of the first step's list the recordings opened.

    ``(None, "")`` keeps the first-entry binding: every recording opened the
    first row, or the opened row cannot be located. ``(rule, "")`` is a
    :attr:`PlanStep.pick` rule: some recording opened a later row whose name is
    what was typed, and one rule reproduces every recording (exact name, then
    normalised name, then the first row only where a recording showed the page
    opening it with no row of that name). ``(None, reason)`` when no rule
    reproduces them.
    """
    if not observations or any(o.opened < 0 for o in observations):
        return None, ""
    tables = [keyed_rows(o.data, path) or [] for o in observations]
    if all(rows and o.opened == rows[0][0] for o, rows in zip(observations, tables)):
        return None, ""
    _head, _, tail = path.partition("[*]")
    tail = tail.lstrip(".")
    fields: List[str] = []
    for o, rows in zip(observations, tables):
        row = next((r for index, r, _key in rows if index == o.opened), None)
        found = _name_paths(row, o.input_value) if tail else []
        if not tail and normalise(row) == normalise(o.input_value):
            found = [""]
        for name in found:
            name = "" if name == tail else name
            if name not in fields:
                fields.append(name)
    fields.sort(key=lambda name: name != "")
    for name_path in fields:
        rule: Dict[str, Any] = {"by": PICK_BY_NAME, "input": input_name,
                                "name_path": name_path, "else_first": False}
        unnamed = [o for o in observations
                   if pick_row(o.data, path, rule, o.input_value)[0] is None]
        rule["else_first"] = bool(unnamed)
        named_later = any(
            pick_row(o.data, path, rule, o.input_value)[2] in ("exact", "normalised")
            and o.opened != rows[0][0]
            for o, rows in zip(observations, tables) if rows)
        if named_later and all(pick_row(o.data, path, rule, o.input_value)[1] == o.opened
                               for o in observations):
            return rule, ""
    return None, (
        "chain refused: a recording opened a result other than the first, and "
        "neither 'the first result' nor 'the result named what was typed' matches "
        "every recording. Record the search again, and open the result whose name "
        "is what you typed")


def _chain_signature(step1: RequestTemplate, path: str,
                     step2: RequestTemplate) -> Tuple[Any, ...]:
    """Stable relationship identity across recordings of different inputs."""
    return (
        step1.method, step1.url, tuple(sorted(step1.query.items())), step1.body or "",
        path,
        step2.method, step2.url, tuple(sorted(step2.query.items())), step2.body or "",
    )


def _chain_observations(scored: List[Candidate], param_name: str,
                        param_value: str) -> Tuple[List[_ChainObservation], List[str]]:
    """All replayable chain relationships visible in one recording."""
    out: List[_ChainObservation] = []
    refusals: List[str] = []
    seen = set()
    viable = [c for c in scored if c.coverage >= CHAIN_MIN_COVERAGE and not c.ineligible]
    for consumer in viable[:6]:
        # Include the caller input. Equality is exactly the ambiguous case that
        # cross-recording evidence must resolve; excluding it made `q=figment`
        # invisible when the first hit of a package search was also `figment`.
        for literal in _url_literals(consumer.url, exclude=""):
            producer, path = find_producer(literal, scored, consumer)
            if producer is None:
                continue
            step1_tmpl, _why1 = build_template(producer, param_name, param_value)
            if step1_tmpl is None:
                continue
            step2_tmpl, why2 = build_template(
                consumer, CHAIN_PARAM, literal,
                extra_bindings=[(param_value, param_name)])
            if step2_tmpl is None:
                refusals.append(
                    f"chain detected but the second request is not replayable: {why2}")
                continue
            if CHAIN_PARAM not in step2_tmpl.params:
                continue
            signature = _chain_signature(step1_tmpl, path, step2_tmpl)
            if signature in seen:
                continue
            seen.add(signature)
            step1_tmpl.extract = step1_tmpl.extract or {"items": path}
            plan = RequestPlan(
                steps=[PlanStep(name="find", template=step1_tmpl,
                                bind={CHAIN_PARAM: path}),
                       PlanStep(name="open", template=step2_tmpl)],
                params=[param_name], shape=step2_tmpl.shape, origin="deterministic",
                notes=(f"chained: {urllib.parse.urlsplit(step1_tmpl.url).path} → "
                       f"{urllib.parse.urlsplit(step2_tmpl.url).path}; "
                       f"{CHAIN_PARAM} bound from {path!r} (first entry); "
                       "derived binding corroborated by multiple recorded inputs"),
            )
            problems = plan.problems()
            if problems:
                refusals.append("chain rejected: " + "; ".join(problems))
                continue
            out.append(_ChainObservation(plan=plan, literal=literal,
                                         input_value=param_value,
                                         signature=signature, data=producer.data,
                                         opened=_opened_row(producer.data, path, literal),
                                         key_type=_key_type(producer.data, path, literal)))
    return out, refusals


#: Why :func:`compile_chain` refuses a chain that only one recorded input shows.
CHAIN_UNCORROBORATED = (
    "chain-like producer/consumer relation found, but a derived binding "
    "requires corroboration from at least 2 distinct recorded inputs; refusing "
    "a one-recording inference")


def compile_chain(recording: dict, items: List[str], scored: List[Candidate],
                  param_name: str, param_value: str,
                  evidence_recordings: Optional[List[dict]] = None
                  ) -> Tuple[Optional[RequestPlan], str]:
    """Build a two-step plan when the explaining request carries a produced value.

    The rule is deliberately narrow and deterministic. A request URL literal
    must occur in another response and the same producer path / consumer binding
    must recur in recordings of at least two distinct inputs. At least one of
    those recordings must show the produced literal differing from the caller
    input; otherwise a direct input binding and a derived first-hit binding are
    observationally identical, so compilation refuses instead of guessing.
    The same recordings settle which row the chain binds (:func:`_row_rule`):
    the first row, or the row whose name is the input when a recording opened
    a later row of that name. A recording that neither rule reproduces refuses
    the chain.

    Returns ``(plan, "")`` or ``(None, reason)``. ``reason`` is empty when there
    simply is no chain (the caller then compiles a single request).
    """
    base, refusals = _chain_observations(scored, param_name, param_value)
    if not base:
        return None, refusals[0] if refusals else ""

    others: List[Tuple[str, List[_ChainObservation]]] = []
    seen_inputs = {normalise(param_value)}
    for rec in evidence_recordings or []:
        value = str((rec.get("param") or {}).get("value") or "")
        normalized = normalise(value)
        if not normalized or normalized in seen_inputs:
            continue
        seen_inputs.add(normalized)
        others.append((normalized, _recording_observations(rec, param_name)))

    ambiguous_count = 0
    row_refusal = ""
    for candidate in base:
        matches = [candidate]
        matched_inputs = {normalise(candidate.input_value)}
        for normalized, observations in others:
            match = next((item for item in observations
                          if item.signature == candidate.signature), None)
            if match is not None:
                matches.append(match)
                matched_inputs.add(normalized)
        if len(matched_inputs) < 2:
            continue
        if any(normalise(item.literal) != normalise(item.input_value)
               for item in matches):
            plan = candidate.plan
            path = plan.steps[0].bind[CHAIN_PARAM]
            rule, why = _row_rule(matches, path, param_name)
            if why:
                row_refusal = row_refusal or why
                continue
            if rule:
                plan.steps[0].pick = {CHAIN_PARAM: rule}
                problems = plan.problems()
                if problems:
                    row_refusal = row_refusal or "chain rejected: " + "; ".join(problems)
                    continue
                plan.notes = plan.notes.replace("(first entry)", _rule_text(rule))
            key_type = _derived_type(matches)
            if key_type != "string":
                plan.type_derived(CHAIN_PARAM, key_type)
                plan.notes += f"; {CHAIN_PARAM} is a JSON {key_type} in every recording"
            return plan, ""
        ambiguous_count = max(ambiguous_count, len(matched_inputs))

    if row_refusal:
        return None, row_refusal
    if ambiguous_count:
        return None, (
            "chain binding cannot be distinguished from the caller input: the "
            f"producer value equals the input in all {ambiguous_count} corroborating "
            "recordings; refusing rather than guessing input vs derived")
    if any(normalise(item.literal) != normalise(item.input_value) for item in base):
        return None, CHAIN_UNCORROBORATED
    # Equality on one recording is not enough even to call this a chain: an
    # ordinary detail page often makes side requests whose URL repeats its input.
    # Let the normal single-request compiler proceed unless a second recording
    # corroborates the relationship (and can then distinguish or refuse it).
    return None, ""


def _recording_observations(recording: dict, param_name: str = "q") -> List[_ChainObservation]:
    """The chain relationships one recording shows, its requests scored against
    its own visible list (as :func:`compile_chain` reads each evidence recording)."""
    value = str((recording.get("param") or {}).get("value") or "")
    items = visible_items(recording)
    scored = sorted((score_candidate(req, items) for req in eligible_requests(recording)),
                    key=lambda candidate: (-candidate.coverage, candidate.bytes))
    observations, _why = _chain_observations(
        scored, str((recording.get("param") or {}).get("name") or param_name), value)
    return observations


def chain_observations(recording: dict) -> List[_ChainObservation]:
    """Every chain relationship ``recording`` shows: read by its whole result rows,
    then by each row's own name (:func:`labels_reading`), one per signature. A
    recording of another input corroborates a chain when one of these has the
    chain's signature (:func:`compile_chain`)."""
    out: List[_ChainObservation] = []
    seen = set()
    for rec in (recording, labels_reading(recording)):
        if rec is None:
            continue
        for observation in _recording_observations(rec):
            if observation.signature not in seen:
                seen.add(observation.signature)
                out.append(observation)
    return out


def _placeholders(tmpl: RequestTemplate) -> set:
    names = set()
    for text in [tmpl.url, *tmpl.query.values(), tmpl.body or ""]:
        names.update(_PLACEHOLDER_RE.findall(text or ""))
    return names


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---------------------------------------------------------------------------
# page-keyed plans — a request that sends a value the site's own page states
# ---------------------------------------------------------------------------

#: the plan step that fetches the site's page and reads the values it states
PAGE_STEP = "page"
#: a request field whose value the site's page stated (a provider key)
PAGE_KEY = "page_key_{n}"
#: a provider literal (app id, index) the same page states next to the key
PAGE_LIT = "page_lit_{n}"
#: a value a landing page states that the data call sends
LANDING_PARAM = "landing_{n}"
#: how far from a key's statement on the page a provider literal may stand
NEAR_KEY_CHARS = 400
PAGE_TEXT_HEADERS = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
SEARCH_TO_HOME = ("search_to_home: the site sends every query to its home page, so it has "
                  "no search to compile")
LANDING_UNCORROBORATED = (
    "landing_uncorroborated: the search redirects to a landing page whose value the data call "
    "sends, but no second recorded input shows that page stating another value; a recording "
    "of another input can compile it")


def _drop_query_keys(url: str, keys: Sequence[str]) -> str:
    drop = set(keys)
    sp = urllib.parse.urlsplit(url)
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
             if k not in drop]
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path,
                                    urllib.parse.urlencode(pairs), ""))


def _source_body(recording: dict, key: Dict[str, Any]) -> Tuple[int, str]:
    """``(index, body)`` in ``recording["network"]`` of the page a page key names
    (``source_url``; ``source_index`` when that entry is this page). A recorder
    numbers its own traffic, and a recording keeps only the requests it may
    compile, so the number alone may point at another entry."""
    net = recording.get("network") or []
    url = str(key.get("source_url") or "")
    at = int(key.get("source_index", -1))
    order = ([at] if 0 <= at < len(net) else []) + list(range(len(net)))
    for i in order:
        body = net[i].get("body")
        if str(net[i].get("url") or "") == url and isinstance(body, str) and body:
            return i, body
    return -1, ""


def _page_step(source_url: str, param_name: str, param_value: str,
               evidence: Optional[VolatileEvidence] = None,
               name: str = PAGE_STEP) -> Tuple[Optional[RequestTemplate], str]:
    """A GET of the site's page, read as text (``extract_kind="page_text"``).
    The page's URL keeps the typed value as a parameter when it carries it."""
    url = str(source_url or "").split("#")[0]
    if not url.startswith("https://"):
        return None, f"the page that states the value is not https ({url[:80]})"
    params: List[str] = []
    if locate_param(url, param_value).where != "none":
        u, query, _dropped, _used, why = urlify(url, param_name, param_value, evidence=evidence)
        if u is None:
            return None, why
        params = [param_name]
    else:
        sp = urllib.parse.urlsplit(url)
        query = {k: v for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
                 if not (evidence.decide(url, k, v) if evidence is not None else _volatile(k, v))}
        u = urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path or "/", "", ""))
    tmpl = RequestTemplate(method="GET", url=u, query=query, headers=dict(PAGE_TEXT_HEADERS),
                           extract_kind="page_text", extract={}, shape="fields",
                           expect={"status_in": [200]}, params=params, origin="deterministic",
                           notes=f"step {name!r}: the site's page, read as text for the values "
                                 "it states by name")
    problems = tmpl.problems()
    if problems:
        return None, "; ".join(problems)
    return tmpl, ""


def _key_windows(text: str, locators: Sequence[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """Spans of ``text`` within :data:`NEAR_KEY_CHARS` of a key's statement."""
    out: List[Tuple[int, int]] = []
    for loc in locators:
        for name, sep in (loc.get("names") or []):
            for m in re.finditer(re.escape(str(name) + str(sep)), text):
                out.append((m.start() - NEAR_KEY_CHARS, m.end() + NEAR_KEY_CHARS))
    return out


def _provider_literals(tmpl: RequestTemplate, text: str, key_locators: Sequence[Dict[str, Any]],
                       exclude: str) -> List[Tuple[str, Any, str, Dict[str, Any]]]:
    """``(where, key, literal, locator)`` for the template's constant query values
    and path segments that the source page states by name next to the key (an
    app id, an index): read from the page at run time, with the recorded value
    as the fallback."""
    from anyuse.execution.page_literals import locate, statements
    windows = _key_windows(text, key_locators)
    if not windows:
        return []
    found: List[Tuple[str, Any, str]] = []
    skip = normalise(exclude)
    for key, val in tmpl.query.items():
        if not _PLACEHOLDER_RE.search(str(val)) and normalise(val) != skip:
            found.append(("query", key, str(val)))
    if not tmpl.slots:
        segs = urllib.parse.urlsplit(tmpl.url).path.split("/")
        for i, seg in enumerate(segs):
            if seg and not _PLACEHOLDER_RE.search(seg) and normalise(seg) != skip:
                found.append(("path", i, urllib.parse.unquote(seg)))
    out: List[Tuple[str, Any, str, Dict[str, Any]]] = []
    for where, key, lit in found:
        loc = locate(text, lit)
        if loc is None:
            continue
        near = any(lo <= at <= hi for name, sep, val, _r, at in statements(text)
                   if val == lit and (name, sep) in loc.names for lo, hi in windows)
        if near:
            out.append((where, key, lit, loc.to_dict()))
    return out


class _KeepStated:
    """Lever (b)'s evidence, except that a query key whose value the site's page
    states next to the key is never noise: it is read from the page instead."""

    def __init__(self, evidence: Optional[VolatileEvidence], keys: Sequence[str]):
        self.evidence, self.keys = evidence, set(keys)

    def decide(self, url: str, key: str, value: str) -> bool:
        if key in self.keys:
            return False
        return self.evidence.decide(url, key, value) if self.evidence is not None \
            else _volatile(key, value)


def _stated_query_keys(url: str, text: str, key_locators: Sequence[Dict[str, Any]],
                       exclude: str) -> List[str]:
    """The query keys of ``url`` whose values the page ``text`` states by name next to the key."""
    probe = RequestTemplate(method="GET", url=urllib.parse.urlunsplit(
        urllib.parse.urlsplit(url)._replace(query="", fragment="")),
        query={k: v for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                                        keep_blank_values=True)})
    return [key for where, key, _lit, _loc in _provider_literals(probe, text, key_locators, exclude)
            if where == "query"]


def _set_body_leaf(body: Optional[str], path: str, value: str) -> Optional[str]:
    """``body`` with the JSON leaf at dotted ``path`` (or the form field) set to ``value``."""
    raw = str(body or "")
    if raw.strip()[:1] in "{[":
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        parts = path.split(".")
        node = data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            return None
        node[parts[-1]] = value
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    if not any(k == path for k, _v in pairs):
        return None
    return "&".join(f"{urllib.parse.quote(k)}={v if k == path else urllib.parse.quote(v)}"
                    for k, v in [(k, value if k == path else v) for k, v in pairs])


def _with_page_fields(tmpl: RequestTemplate, fields: Dict[str, str],
                      literals: Sequence[Tuple[str, Any, str, Dict[str, Any]]] = (),
                      lit_names: Sequence[str] = ()) -> Tuple[Optional[RequestTemplate], str]:
    """``tmpl`` with each page key's field (``{at: name}``) and each provider
    literal set to its placeholder, and its parameter list rebuilt."""
    data = tmpl.to_dict()
    query, headers, body = dict(data["query"]), dict(data["headers"]), data.get("body")
    url = data["url"]
    for at, name in fields.items():
        kind, _, key = at.partition(":")
        if kind == "query":
            if tmpl.slots:
                return None, "a page key in the query of a slotted request is not compiled"
            query[key] = "{%s}" % name
        elif kind == "header":
            headers[key] = "{%s}" % name
        elif kind == "body":
            body = _set_body_leaf(body, key, "{%s}" % name)
            if body is None:
                return None, f"the body has no field {key!r} for its page key"
        else:
            return None, f"unknown page key field {at!r}"
    sp = urllib.parse.urlsplit(url)
    segs = sp.path.split("/")
    for (where, key, _lit, _loc), name in zip(literals, lit_names):
        if where == "query":
            query[key] = "{%s}" % name
        elif where == "path":
            segs[int(key)] = "{%s}" % name
    url = urllib.parse.urlunsplit((sp.scheme, sp.netloc, "/".join(segs), sp.query, sp.fragment))
    names = list(fields.values()) + list(lit_names)
    data.update(url=url, query=query, headers=headers, body=body, parameter_schema=[],
                params=sorted(set(data.get("params") or []) | set(names)))
    out = RequestTemplate.from_dict(data)
    return out, ""


def compile_page_keyed(recording: dict, cand: Candidate, param_name: str, param_value: str, *,
                       evidence: Optional[VolatileEvidence] = None,
                       binding: Optional["BindingEvidence"] = None,
                       nested: bool = False, slot_evidence: Optional[Any] = None
                       ) -> Tuple[Optional[RequestPlan], str]:
    """A two-step plan for a request that sends a key the site's own page stated:
    GET that page and read the key by its locator, then send the request the way
    the page did. The provider's app id and index, when the same page states them
    next to the key, are read the same way with the recorded value as the
    fallback. Returns ``(plan, "")`` or ``(None, why)``."""
    keys = [k for k in cand.page_keys if isinstance(k.get("locator"), dict)]
    if not keys:
        return None, ""
    sources = {int(k.get("source_index", -1)) for k in keys}
    if len(sources) != 1:
        return None, "the request's keys come from different pages of the site"
    src = keys[0]
    ats = sorted(str(k["at"]) for k in keys)
    url_wo = _drop_query_keys(cand.url, [at.split(":", 1)[1] for at in ats
                                         if at.startswith("query:")])
    import dataclasses
    _at, text = _source_body(recording, src)
    stated = _stated_query_keys(url_wo, text, [k["locator"] for k in keys], param_value)
    report: Dict[str, Any] = {}
    tmpl, why = build_template(dataclasses.replace(cand, url=url_wo), param_name, param_value,
                               evidence=_KeepStated(evidence, stated), binding=binding,
                               report=report, nested=nested, slot_evidence=slot_evidence)
    if tmpl is None:
        return None, why
    names = {at: PAGE_KEY.format(n=i + 1) for i, at in enumerate(ats)}
    literals = _provider_literals(tmpl, text, [k["locator"] for k in keys], param_value)
    lit_names = [PAGE_LIT.format(n=i + 1) for i in range(len(literals))]
    tmpl, why = _with_page_fields(tmpl, names, literals, lit_names)
    if tmpl is None:
        return None, why
    page, why = _page_step(str(src.get("source_url") or ""), param_name, param_value, evidence)
    if page is None:
        return None, why
    page_bind = {names[str(k["at"])]: dict(k["locator"]) for k in keys}
    page_bind.update({name: loc for (_w, _k, _l, loc), name in zip(literals, lit_names)})
    return _page_plan(page, page_bind, tmpl, param_name, cand, src, literals)


def _page_plan(page: RequestTemplate, page_bind: Dict[str, Dict[str, Any]],
               tmpl: RequestTemplate, param_name: str, cand: Candidate, src: Dict[str, Any],
               literals: Sequence[Any]) -> Tuple[Optional[RequestPlan], str]:
    host = urllib.parse.urlsplit(tmpl.url).netloc
    plan = RequestPlan(
        steps=[PlanStep(name=PAGE_STEP, template=page, page_bind=page_bind),
               PlanStep(name="search", template=tmpl)],
        params=[param_name], shape=tmpl.shape, origin="deterministic",
        notes=(f"page-keyed: reads {len(page_bind) - len(literals)} key(s)"
               + (f" and {len(literals)} provider value(s)" if literals else "")
               + f" from {urllib.parse.urlsplit(str(src.get('source_url') or '')).path or '/'} "
               f"of the site at run time (never stored), then sends the request to {host} "
               f"as the page did; coverage={cand.coverage:.2f} of {cand.total_items} visible items"))
    problems = plan.problems()
    if problems:
        return None, "page-keyed plan rejected: " + "; ".join(problems[:3])
    return plan, ""


def _redacted_exemption(ats: Sequence[str]) -> Dict[str, set]:
    from anyuse.execution.page_literals import REDACTED
    out: Dict[str, set] = {}
    for at in ats:
        kind, _, key = str(at).partition(":")
        name = key.rsplit(".", 1)[-1] if kind == "body" else key
        out.setdefault(name.lower(), set()).add(REDACTED)
    return out


def compile_page_keyed_post(recording: dict, items: List[str], param_name: str,
                            param_value: str, *, levers: Optional["Levers"] = None,
                            evidence: Optional[VolatileEvidence] = None
                            ) -> Tuple[Optional[RequestPlan], str]:
    """A page-keyed plan whose request is a POST (a hosted search provider's
    ``queries`` or ``multi_search`` read): compiled when the site's own page sent it in
    the read shape the observed-provider rule accepts
    (``api_step.provider_read_shape``; it then runs once its host is
    acknowledged), or under the ``post`` compile lever. Every rule of the
    read-only POST rung holds; the page keys are the one exemption."""
    from anyuse.execution import page_literals as PL
    from anyuse.execution.api_slots import deep_refusal
    from anyuse.execution.api_step import provider_read_shape
    levers = levers if levers is not None else DEFAULT_LEVERS
    reasons: List[str] = []
    best: Tuple[float, Optional[RequestPlan]] = (0.0, None)
    for req in readonly_post_candidates(recording):
        keys = list(PL.page_keys_of(req).values())
        if not keys:
            continue
        host = urllib.parse.urlsplit(req["url"]).netloc
        ats = sorted(str(k["at"]) for k in keys)
        if len({int(k.get("source_index", -1)) for k in keys}) != 1:
            reasons.append(f"{host}: the request's keys come from different pages of the site")
            continue
        src = keys[0]
        why = _post_credential_reason(req, exempt=ats)
        post = req.get("post_data") or ""
        kind, op, shape_why = _readonly_post_shape(post)
        why = why or shape_why or (f"GraphQL operation is a {op!r}, not a query"
                                   if kind == "graphql" and op not in ("query", "anonymous") else "")
        why = why or deep_refusal(req["url"], post, req.get("headers") or {},
                                  exempt=_redacted_exemption(ats))
        if why:
            reasons.append(f"{host}: {why}")
            continue
        site = urllib.parse.urlsplit(str(src.get("source_url") or "")).netloc
        shape_refusal = provider_read_shape(site, req["url"], post, req.get("headers") or {},
                                            [param_value])
        if shape_refusal and not levers.readonly_post:
            reasons.append(f"{host}: {shape_refusal}; a POST that is not the read the site's page "
                           "sends its search provider compiles only with the 'post' compile lever")
            continue
        try:
            data = json.loads(req["body"])
        except (ValueError, TypeError):
            reasons.append(f"{host}: the response is not JSON, so it cannot be extracted from")
            continue
        cand = score_candidate({**req, "_data": data}, items)
        if cand.coverage < MIN_COVERAGE:
            reasons.append(f"{host}: the response explains only {cand.coverage:.2f} of the "
                           "visible list")
            continue
        body_tmpl, bind_why = _bind_in_body(post, param_value, param_name)
        if body_tmpl is None:
            reasons.append(f"{host}: {bind_why}")
            continue
        tmpl = _post_template_from(cand, param_name)
        if tmpl is None:
            reasons.append(f"{host}: no extraction path explains the visible list")
            continue
        url_wo = _drop_query_keys(req["url"], [at.split(":", 1)[1] for at in ats
                                               if at.startswith("query:")])
        _at, text = _source_body(recording, src)
        keep = _KeepStated(evidence, _stated_query_keys(url_wo, text, [k["locator"] for k in keys],
                                                        param_value))
        sp = urllib.parse.urlsplit(url_wo)
        tmpl.method = "POST"
        tmpl.url = urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))
        tmpl.query = {k: v for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
                      if not keep.decide(req["url"], k, v)}
        tmpl.body = body_tmpl
        tmpl.readonly_post = True
        tmpl.provider_read = "" if shape_refusal else site.lower()
        tmpl.headers = {"accept": "application/json",
                        "content-type": ("application/json" if kind in ("json", "graphql")
                                         else "application/x-www-form-urlencoded")}
        names = {at: PAGE_KEY.format(n=i + 1) for i, at in enumerate(ats)}
        literals = _provider_literals(tmpl, text, [k["locator"] for k in keys], param_value)
        lit_names = [PAGE_LIT.format(n=i + 1) for i in range(len(literals))]
        filled, why = _with_page_fields(tmpl, names, literals, lit_names)
        if filled is None:
            reasons.append(f"{host}: {why}")
            continue
        page, why = _page_step(str(src.get("source_url") or ""), param_name, param_value,
                               evidence)
        if page is None:
            reasons.append(f"{host}: {why}")
            continue
        page_bind = {names[str(k["at"])]: dict(k["locator"]) for k in keys}
        page_bind.update({name: loc for (_w, _k, _l, loc), name in zip(literals, lit_names)})
        plan, why = _page_plan(page, page_bind, filled, param_name, cand, src, literals)
        if plan is None:
            reasons.append(f"{host}: {why}")
            continue
        plan.notes += ("; the search is a read-only POST the site's page sends its search provider"
                       if filled.provider_read else "; the search is a read-only POST")
        if cand.coverage > best[0]:
            best = (cand.coverage, plan)
    if best[1] is not None:
        return best[1], ""
    return None, "; ".join(reasons[:3])


# ---------------------------------------------------------------------------
# a search that redirects to a landing page
# ---------------------------------------------------------------------------

def _is_document(entry: Dict[str, Any]) -> bool:
    return (str(entry.get("resource_type") or "") == "Document"
            or "html" in str(entry.get("mime") or "").lower())


def value_hop(hops: Sequence[Any], value: str) -> str:
    """The redirect hop that carries ``value``: the first https one, else the
    first one (an http hop the site upgraded to https), else ``""``."""
    carrying = [str(h) for h in hops or () if h and locate_param(str(h), value).where != "none"]
    secure = [h for h in carrying if h.startswith("https://")]
    return (secure or carrying or [""])[0]


def landing_documents(recording: dict, value: str) -> List[Tuple[int, Dict[str, Any], str]]:
    """``(index, document, hop)`` for each document the site reached through a
    redirect from a URL carrying ``value`` (``hop``, :func:`value_hop`). The
    search URL the site formed is what a plan replays, never a template guessed
    from the landing page's own URL."""
    out: List[Tuple[int, Dict[str, Any], str]] = []
    for i, entry in enumerate(recording.get("network") or []):
        hops = [str(h) for h in (entry.get("redirected_from") or []) if h]
        if not hops or not _is_document(entry):
            continue
        hop = value_hop(hops, value)
        if hop:
            out.append((i, entry, hop))
    return out


def _home_like(url: str, recording: dict) -> bool:
    """Is ``url`` the site's home page or the page the search was typed on?"""
    sp = urllib.parse.urlsplit(str(url or ""))
    if (sp.path or "/") == "/" and not sp.query:
        return True
    start = str((recording.get("agent") or {}).get("start_url") or recording.get("start_url")
                or "")
    return bool(start) and str(url).split("#")[0].rstrip("/") == start.split("#")[0].rstrip("/")


def _searched_after(recording: dict, index: int, value: str) -> bool:
    """Does a request after entry ``index`` carry ``value`` (the page searched for
    it after landing, as a script-routed page does)? In a request body, as whole
    words (:func:`contains_words`): "art" is not carried by "start"."""
    want = normalise(value)
    for entry in (recording.get("network") or [])[index + 1:]:
        if locate_param(str(entry.get("url") or ""), value).where != "none":
            return True
        post = entry.get("post_data")
        if want and isinstance(post, str) and contains_words(
                normalise(urllib.parse.unquote_plus(post)), want):
            return True
    return False


def search_to_home(recording: dict, value: str) -> bool:
    """Did the recorded search redirect to the site's home page (or back to the
    page it was typed on), with no later request carrying the value? Then the
    site has no search to compile. A page that lands on ``/`` and then sends
    the value in a data call (a script-routed search) does search."""
    return any(_home_like(str(doc.get("url") or ""), recording)
               and not _searched_after(recording, i, value)
               for i, doc, _hop in landing_documents(recording, value))


def _landing_literals(data_url: str, landing_body: str, exclude: str
                      ) -> List[Tuple[str, Dict[str, Any]]]:
    """``(literal, locator)``: the data call's URL literals the landing page states by name."""
    from anyuse.execution.page_literals import Locator, locate
    out: List[Tuple[str, Dict[str, Any]]] = []
    for lit in _url_literals(data_url, exclude):
        if lit == "<redacted>":
            continue
        loc = locate(landing_body, lit)
        if loc is None:
            continue
        # a landing value is never replaced by the recorded one: another input lands elsewhere
        bare = Locator(names=loc.names, right=loc.right, min_len=loc.min_len,
                       max_len=loc.max_len, secret=False, else_value=None)
        out.append((lit, bare.to_dict()))
    return out


def _corroborated(lit: str, loc: Dict[str, Any], data_url: str, value: str,
                  others: Sequence[dict]) -> bool:
    """Does another recorded input's landing page state, through ``loc``, a
    different value that its own data call sends where this one sends ``lit``?"""
    from anyuse.execution.page_literals import Locator, read
    locator = Locator.from_dict(loc)
    sp = urllib.parse.urlsplit(data_url)
    for rec in others:
        other_value = str((rec.get("param") or {}).get("value") or "")
        if not other_value or normalise(other_value) == normalise(value):
            continue
        for i, doc, _hop in landing_documents(rec, other_value):
            got, _why = read(str(doc.get("body") or ""), locator)
            if not got or got == lit:
                continue
            for later in (rec.get("network") or [])[i + 1:]:
                lp = urllib.parse.urlsplit(str(later.get("url") or ""))
                if (lp.netloc, lp.path.replace(got, lit)) != (sp.netloc, sp.path):
                    continue
                if got in urllib.parse.unquote(str(later.get("url") or "")):
                    return True
    return False


def compile_landing_plan(recording: dict, items: List[str], scored: List[Candidate],
                         param_name: str, param_value: str, *,
                         evidence_recordings: Optional[List[dict]] = None,
                         evidence: Optional[VolatileEvidence] = None
                         ) -> Tuple[Optional[RequestPlan], str]:
    """The search URL, its same-site redirect to a landing page, then the data
    call that explains the screen with the values the landing page states.

    The data call comes after the landing, does not carry the typed value, and
    sends a value the landing page states by name; at least one such value must
    be corroborated by a recording of another input (its landing page states
    another value through the same locator, and its data call sends that one).
    A landing that is the site's home page, or the same page for every recorded
    input, is :data:`SEARCH_TO_HOME`. Returns ``(plan, "")``, ``(None, reason)``, or
    ``(None, "")`` when the recording shows no landing."""
    from anyuse.execution.site_canon import leaves_site
    docs = landing_documents(recording, param_value)
    if not docs:
        return None, ""
    if search_to_home(recording, param_value):
        return None, SEARCH_TO_HOME
    others = [r for r in (evidence_recordings or []) if r is not recording]
    mine = {str(doc.get("url") or "").split("#")[0] for _i, doc, _h in docs}
    landed = []
    for rec in others:
        v = str((rec.get("param") or {}).get("value") or "")
        if v and normalise(v) != normalise(param_value):
            theirs = {str(d.get("url") or "").split("#")[0] for _i, d, _h in landing_documents(rec, v)}
            if theirs:
                landed.append(bool(mine & theirs))
    if landed and all(landed):
        # every other input landed on this page too: the landing does not depend on the
        # search (two close inputs may share one, as "nike" and "nike air" do)
        return None, SEARCH_TO_HOME
    index, landing, hop = docs[-1]
    body = str(landing.get("body") or "")
    data = [c for c in scored if c.coverage >= MIN_COVERAGE and c.recording_index > index
            and locate_param(c.url, param_value).where == "none"]
    if not data or not body:
        return None, ""
    cand = data[0]
    lits = _landing_literals(cand.url, body, param_value)
    if not lits:
        return None, ""
    corroborated = [(lit, loc) for lit, loc in lits
                    if _corroborated(lit, loc, cand.url, param_value, others)]
    if not corroborated:
        return None, LANDING_UNCORROBORATED
    names = [LANDING_PARAM.format(n=i + 1) for i in range(len(corroborated))]
    first_lit = corroborated[0][0]
    keys = [k for k in cand.page_keys if isinstance(k.get("locator"), dict)]
    import dataclasses
    url_wo = _drop_query_keys(cand.url, [str(k["at"]).split(":", 1)[1] for k in keys
                                         if str(k["at"]).startswith("query:")])
    data_tmpl, why = build_template(
        dataclasses.replace(cand, url=url_wo), names[0], first_lit,
        extra_bindings=[(lit, name) for (lit, _loc), name in zip(corroborated[1:], names[1:])],
        evidence=evidence)
    if data_tmpl is None:
        return None, f"the data call cannot take the landing page's value: {why}"
    data_tmpl.params = sorted(set(data_tmpl.params) - {param_name})
    key_names = {str(k["at"]): PAGE_KEY.format(n=i + 1) for i, k in enumerate(keys)}
    if key_names:
        data_tmpl, why = _with_page_fields(data_tmpl, key_names)
        if data_tmpl is None:
            return None, why
    search, why = _page_step(hop, param_name, param_value, evidence, name="search")
    if search is None:
        return None, why
    landing_url = str(landing.get("url") or "")
    if leaves_site(landing_url, hop):
        return None, "the search redirects to another site"
    from anyuse.execution.request_policy import origin as request_origin
    if request_origin(landing_url) != request_origin(hop):
        # the site's own redirect to its own other host (a www sibling, a shop subdomain)
        search.approved_origins = [request_origin(landing_url)]
    search_bind = {name: loc for (_lit, loc), name in zip(corroborated, names)}
    steps = [PlanStep(name="search", template=search, page_bind=search_bind)]
    if keys:
        same = [k for k in keys if _source_body(recording, k)[0] == index]
        if len(same) == len(keys):
            search_bind.update({key_names[str(k["at"])]: dict(k["locator"]) for k in keys})
        else:
            src = keys[0]
            if len({int(k.get("source_index", -1)) for k in keys}) != 1:
                return None, "the data call's keys come from different pages of the site"
            page, why = _page_step(str(src.get("source_url") or ""), param_name, param_value,
                                   evidence)
            if page is None:
                return None, why
            steps.insert(0, PlanStep(name=PAGE_STEP, template=page, page_bind={
                key_names[str(k["at"])]: dict(k["locator"]) for k in keys}))
    steps.append(PlanStep(name="data", template=data_tmpl))
    plan = RequestPlan(
        steps=steps, params=[param_name], shape=data_tmpl.shape, origin="deterministic",
        notes=(f"landing: {urllib.parse.urlsplit(hop).path} redirects to "
               f"{urllib.parse.urlsplit(landing_url).path}; the data call "
               f"{urllib.parse.urlsplit(cand.url).netloc}{urllib.parse.urlsplit(cand.url).path} "
               f"sends {len(names)} value(s) the landing page states by name, corroborated by "
               f"another recorded input; coverage={cand.coverage:.2f} of {cand.total_items} "
               "visible items"))
    problems = plan.problems()
    if problems:
        return None, "landing plan rejected: " + "; ".join(problems[:3])
    return plan, ""


# ---------------------------------------------------------------------------
# persistence — where a proved template lives
# ---------------------------------------------------------------------------

#: File written beside ``template.json`` in the workflow directory.
COMPILED_FILE = "compiled_api.json"
COMPILED_VERSION = 2


def migrate_compiled_record(record: dict) -> dict:
    """Return the current tagged-schema form of a compile record of format version 1 or 2.

    Format-1 ``{q}`` declarations become bounded string ``input`` values.  A plan's
    ``bind`` edges become ``derived`` values sourced from that earlier step;
    cursor pagination uses the same migration and is therefore never exposed as
    caller input.  The semantic request is unchanged, so the existing proof is
    pinned to the migrated representation once.
    """
    import copy
    from anyuse.execution.workflow_schema import template_digest

    migrated = copy.deepcopy(record or {})
    version = int(migrated.get("version") or 1)
    if version > COMPILED_VERSION:
        return migrated
    if version < COMPILED_VERSION:
        command = load_command(migrated.get("template") or {})
        migrated["template"] = command.to_dict()
        migrated["version"] = COMPILED_VERSION
        migrated["migration"] = {
            "from_version": version,
            "parameters": "legacy placeholders became bounded input values; plan binds became derived",
        }
        proof = dict(migrated.get("proof") or {})
        proof["template_digest"] = template_digest(migrated["template"])
        migrated["proof"] = proof
    return migrated


def proof_matches_template(record: dict) -> bool:
    """Whether the evidence is pinned to this exact template and its consts."""
    from anyuse.execution.workflow_schema import template_digest
    proof = record.get("proof") or {}
    expected = str(proof.get("template_digest") or "")
    return bool(expected) and expected == template_digest(record.get("template") or {})


def compiled_record(template: RequestTemplate, proof: dict, workflow: str = "",
                    source: Optional[dict] = None,
                    chrome_filtered: Optional[List[str]] = None,
                    permission: Optional[dict] = None) -> dict:
    """The on-disk record: the template AND the evidence it was kept on.

    The proof travels with the template on purpose — a compiled implementation
    with no record of what was checked is exactly the kind of claim this project
    does not make.

    ``chrome_filtered`` is the list of on-screen labels the page-chrome filter
    removed before anything was proved. It is stored, not just counted: the
    filter defines what "the result list" was taken to mean, and if it removed
    something that was actually a result then the proof answered the wrong
    question. ``permission`` records the robots verdict and the policy in force,
    including a per-host override if one was used.
    """
    import datetime
    from anyuse.execution.workflow_schema import template_digest
    template_dict = template.to_dict()
    pinned_proof = dict(proof or {})
    pinned_proof["template_digest"] = template_digest(template_dict)
    # the drift anchor (execution/step_repair.py) lives on the record, not in
    # the proof: it is response content, and the proof is surfaced over MCP
    anchor = pinned_proof.pop("drift_anchor", None)
    # the generated fitting input of a chain (execution/chain_fit.py) is a name the
    # site's search returned: it lives on the record beside the anchor, never in
    # the proof, and the template digest does not cover it
    chain_fit = pinned_proof.pop("chain_fit", None)
    from anyuse.execution.api_step import signed_in_template
    if signed_in_template(template):
        anchor = None          # signed-in (login=replay) items are never persisted
        chain_fit = None
    out = {
        "version": COMPILED_VERSION,
        "workflow": workflow,
        "compiled_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "template": template_dict,
        "proof": pinned_proof,
        "chrome_filtered": sorted(chrome_filtered or []),
        "permission": dict(permission or {}),
        "source": source or {},
    }
    if isinstance(anchor, dict):
        out["drift_anchor"] = anchor
    if isinstance(chain_fit, dict):
        out["chain_fit"] = chain_fit
    return out


def runtime_rungs(recording: dict, *, task: str = "", start_url: str = "", origin: str = "",
                  recordings: Optional[List[dict]] = None) -> dict:
    """The rungs below the compiled request, for the runtime cascade (``run_compiled``).

    ``browser``: a replay of the recorded result page in a container browser, read with the
    selector the ground truth was derived with (each row's name when the recording carried
    that reading). Only when the recording has an https result page and a derived selector.
    ``task``: what the agent is asked when the faster rungs fail, with the parameter as a
    ``{name}`` placeholder. ``page``: the result page as a URL template (:func:`result_page`).
    Records written before these existed simply have none of them.
    """
    out: dict = {}
    param = str((recording.get("param") or {}).get("name") or "q")
    final_url = str(recording.get("final_url") or "")
    site = str(recording.get("site") or urllib.parse.urlsplit(final_url or start_url).netloc or "")
    page = result_page(recording, recordings or ())
    if page is not None:
        out["page"] = page
    if recording.get("ground_truth_selector") and final_url.startswith("https://"):
        try:
            from anyuse.execution.cdp_replay import template_from_recording
            # readiness=None: ANYUSE_TIER2_READINESS decides (on unless switched off)
            tmpl = template_from_recording(recording, param, recordings=recordings, readiness=None)
            labels = bool((recording.get("ui") or {}).get("labels"))
            tmpl.read_labels = labels
            if tmpl.url and not tmpl.problems():
                out["browser"] = {"template": tmpl.to_dict(), "read": "labels" if labels else "rows",
                                  "origin": origin or "recording"}
                fitted = [str(v) for v in recording.get("ground_truth_fitted_on") or [] if v]
                if fitted:
                    # the inputs its selector was derived on: an automatic recompile compiles from
                    # one of these when the call's own input is a held-out proof input
                    out["browser"]["fitted_on"] = fitted
        except Exception as exc:  # noqa: BLE001 - no browser rung is a smaller tool, not a failed compile
            logger.debug("no browser rung for this record: %s", exc)
    if site:
        text = (task or "").strip()
        given = bool(text)
        if not text:
            text = f"search {site} for {{{param}}} and list the names of the results in page order"
        elif "{" + param + "}" not in text:
            text = f"{text} (the value to use: {{{param}}})"
        out["task"] = {"text": text, "param": param, "site": site, "given": given,
                       "start_url": start_url or f"https://{site}/"}
    return out


def result_page(recording: dict, recordings: Sequence[dict] = ()) -> Optional[dict]:
    """The recorded result page as a URL template, for a heal that reads it with a plain
    GET (execution/step_heal.py); None when the page is not https or the input is not
    bound in its URL.

    ``html_holds_items``: the page's HTML document held the visible labels
    (:func:`_labels_in_html`), so a plain GET of it shows what the browser showed.
    ``values``: the recordings' inputs; they hold no response content.
    """
    param = str((recording.get("param") or {}).get("name") or "q")
    value = str((recording.get("param") or {}).get("value") or "")
    final_url = str(recording.get("final_url") or "")
    if not value or not final_url.startswith("https://"):
        return None
    from anyuse.execution.learn_loop import url_template_for
    url_t, path_params, transforms, _how = url_template_for(final_url, value, param)
    if not url_t:
        return None
    parts = urllib.parse.urlsplit(url_t)
    values: List[str] = []
    for rec in [recording, *recordings]:
        v = str(((rec or {}).get("param") or {}).get("value") or "")
        if v and normalise(v) not in {normalise(x) for x in values}:
            values.append(v)
    return {"url": urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
            "query": dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True)),
            "param": param, "path_params": dict(path_params), "transforms": dict(transforms),
            "html_holds_items": _labels_in_html(recording), "values": values}


def with_runtime_rungs(record: dict, recording: Optional[dict], **kw: Any) -> dict:
    """``record`` with :func:`runtime_rungs` added (unchanged without a recording).
    The browser replay reads the query at the places of a longer word that the
    compiled request's proof calibrated (``query_positions``), so both rungs accept
    the same answers."""
    if recording:
        record.update(runtime_rungs(recording, **kw))
        from anyuse.execution import query_match as QM
        from anyuse.execution import result_contract as RC
        compiled = RC.contract_of((record.get("template") or {}).get("expect") or {})
        browser = RC.contract_of(((record.get("browser") or {}).get("template") or {})
                                 .get("expect") or {})
        if compiled is not None and browser is not None and QM.CONTRACT_KEY in compiled:
            browser[QM.CONTRACT_KEY] = list(compiled[QM.CONTRACT_KEY])
            if QM.CALIBRATION_KEY in compiled:
                browser[QM.CALIBRATION_KEY] = str(compiled[QM.CALIBRATION_KEY])
    return record


def persist_compiled(storage, workflow_id: str, record: dict) -> str:
    """Write ``compiled_api.json`` into the workflow directory. Returns the path."""
    import os
    workflow_dir = storage.get_workflow_dir(workflow_id)
    os.makedirs(workflow_dir, exist_ok=True)
    path = os.path.join(str(workflow_dir), COMPILED_FILE)
    with open(path, "w") as fh:
        json.dump(migrate_compiled_record(record), fh, indent=2)
    return path


def load_compiled(storage, workflow_id: str) -> Optional[dict]:
    """Read back a persisted compile record, or ``None``."""
    import os
    path = os.path.join(str(storage.get_workflow_dir(workflow_id)), COMPILED_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return migrate_compiled_record(json.load(fh))
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None


def add_api_implementation(state_graph, from_state: str, to_state: str, intent: str,
                           template: RequestTemplate) -> None:
    """Add the compiled template to a state graph as an ``api`` implementation.

    Uses the EXISTING ``StateGraph.add_implementation`` — no schema change:
    ``TransitionImpl.command`` is already an opaque dict, and implementations are
    deduped on the modality string, so re-compiling replaces the previous api
    rung and leaves ax/js/coordinate untouched. ``deterministic.rank_implementations``
    then puts it first (``_STABILITY["api"] = 0``).
    """
    from anyuse.guides.state_graph import TransitionImpl
    state_graph.add_implementation(
        from_state, to_state, intent,
        TransitionImpl(modality="api", command=template.to_dict(),
                       times_used=0, times_succeeded=0),
    )


# ---------------------------------------------------------------------------
# redacted summary (the ONLY thing a generator model may ever see)
# ---------------------------------------------------------------------------

def redacted_summary(recording: dict, max_requests: int = 12) -> dict:
    """A model-safe view of a recording: shapes and keys, no values.

    Query values are replaced by ``"<param>"`` when they equal the typed value
    and by their *type* otherwise; response bodies are reduced to leaf paths and
    types. Request headers, response headers, cookies and raw bodies never
    appear. This is what :func:`api_compile` hands a generator when the
    deterministic extractor is ambiguous or fails — the verifier, not the model,
    still decides whether the result is kept.
    """
    param = recording.get("param") or {}
    value = param.get("value") or ""
    out_reqs = []
    for r in eligible_requests(recording)[:max_requests]:
        sp = urllib.parse.urlsplit(r["url"])
        q = []
        for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
            if normalise(v) == normalise(value):
                q.append({"key": k, "value": "<PARAM>"})
            elif _volatile(k, v):
                q.append({"key": k, "value": "<VOLATILE>"})
            else:
                q.append({"key": k, "value": f"<{type(v).__name__}:{len(v)}>"})
        shapes = sorted({generalise_path(p): type(v).__name__
                         for p, v in json_leaves(r["_data"])}.items())[:60]
        out_reqs.append({
            "method": r.get("method", "GET"), "host": sp.netloc, "path": sp.path,
            "query": q, "response_shape": [{"path": p, "type": t} for p, t in shapes],
        })
    return {
        "param_name": param.get("name") or "q",
        "visible_items": visible_items(recording),
        "requests": out_reqs,
    }
