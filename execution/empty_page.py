"""The site's own empty page: the first step of the empty-answer cascade.

A search that extracts nothing is an answer ("no results") only with evidence
that the site itself says so (:func:`result_contract.empty_verdict`). This
module gives the first and strongest evidence: the page the site serves for an
input that has no results, learned from the site itself, in whatever language
it is written.

Learning (:func:`learn`): two made-up inputs (:func:`nonsense_values`) are
searched, and their pages are compared with result pages of the same tool. Each
line is normalised, with the input masked as ``{q}`` and digit runs masked. A
line both made-up pages show and no result page shows is a MARK of the empty
page ("không tìm thấy sản phẩm nào cho {q}"). The signature also keeps a hash of
every line of the empty page, the lines it shares with every result page
(chrome, help text) and the places in the page whose lines changed between the
two loads. A JSON answer keeps the count field that is 0 for the made-up inputs
and positive for the result pages, the field that echoes the input, and any
other list that holds results.

Matching (:func:`match`): every mark is on the page; the page shows no line the
empty page lacks (else one more made-up search, sent at the same moment, must
show the same lines: then only the site's chrome changed); and when the empty
page does not name the input, an input known to have results still finds them.
A line whose links all open the site's own search for another input ("did you
mean apple?") is a suggestion, never a line the empty page lacks, also when it
continues the empty page's line. A page that passes is the site's own empty
page for this input. A page that fails a check the site's own pages settle
(rows the selector no longer reads, an error line, a search that no longer
reads its input) is not empty. Anything else is left to the cascade's next step.

Where a signature lives: a tool proved with this module pins it in its
template's result contract (``expect.result_contract.empty.page``), so the
proof's ``template_digest`` covers it. A tool proved before learns it at its
first empty answer and keeps it on its record (``record["empty_page"][rung]``)
under the digest of the template it was learned for, so a new template never
uses an old page. A signature records the version of the text normaliser it was
made with (``norm``); one made with another version is not used.

Every request goes through the ``fetch`` a caller passes: the call's own
executor, with its pre-send policy, robots rules and block detection.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from anyuse.execution import model_check as MC

EMPTY, NOT_EMPTY, UNKNOWN = "empty", "not_empty", "unknown"
#: what decided an empty answer
BY_SITE, BY_MODEL, BY_STATEMENT = "site_empty_page", "model_check", "page_statement"
#: the signature's format
VERSION = 1
#: the line masking below (the input, digit runs); part of a signature's ``norm``
MASK_VERSION = 1
#: made-up inputs searched to learn the empty page
PROBES = 2
MAX_MARKS = 6
MAX_MARK_CHARS = 160
MAX_PAGE_LINES = 1000
#: a learning that got no answer from the site is tried again after this
RETRY_AFTER_S = 3600.0
PROBE_ENV = "ANYUSE_EMPTY_PROBE"

#: the model step's one question (``model_check``): one abstract wording
EMPTY_STATEMENT = MC.Question(
    "empty_statement",
    "The page below answers a search for '{target}'. Does the line marked >> state "
    "that this search found nothing?")
#: the model step's question about a line that reads as an error report
ERROR_LINE = MC.Question(
    "error_line",
    "The page below answers a search for '{target}'. Does the line marked >> report "
    "that the site failed to answer this search?")

_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# pages, evidence, the per-call context
# ---------------------------------------------------------------------------

@dataclass
class Line:
    masked: str
    raw: str
    path: str
    #: every link in the line opens the site's own search for another input (:func:`suggests`)
    suggestion: bool = False


@dataclass
class Page:
    """One answer of the site for ``value``: a page (``html``) or a JSON answer (``json``)."""
    kind: str = "html"
    value: str = ""
    markup: str = ""
    #: what the browser shows (``innerText``); empty for a raw response
    text: str = ""
    #: the JSON answer (or the payload an embedded-JSON tool reads)
    data: Any = None
    items: List[Any] = field(default_factory=list)
    status: int = 0
    #: the site served it (a 2xx answer, not blocked)
    ok: bool = True
    #: not served, and that is the site's own answer (a 4xx other than 429)
    definite: bool = False
    error: str = ""
    url: str = ""
    implied_end_tags: bool = False
    #: JSON: the path of the template's own result list
    list_path: str = ""
    #: a tree of ``markup`` parsed visible-only, when the caller has one
    root: Any = field(default=None, repr=False)
    _vis: Optional[List[Tuple[str, Any, Any]]] = field(default=None, repr=False)
    _lines: Optional[List[Line]] = field(default=None, repr=False)


@dataclass
class Evidence:
    state: str = UNKNOWN
    #: the page's line (or the JSON field) the decision rests on
    line: str = ""
    why: str = ""
    by: str = ""
    requests: int = 0
    model_calls: int = 0
    #: a signature learned during this answer
    learned: Optional[Dict[str, Any]] = None
    backend: str = ""
    p: Optional[float] = None
    #: the site still shows its learned empty page for a made-up input, and this
    #: page is not it: a listed-language statement on it is not taken
    not_site_page: bool = False
    #: a made-up search now gets an empty page without the learned lines: the
    #: site's empty page changed since it was learned
    stale: bool = False

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"by": self.by} if self.by else {}
        if self.requests:
            out["requests"] = self.requests
        if self.model_calls:
            out["model_calls"] = self.model_calls
        if self.learned:
            out["learned"] = str(self.learned.get("learned") or "now")
        if self.backend:
            out["backend"] = self.backend
            if self.p is not None:
                out["p"] = round(self.p, 3)
        return out


@dataclass
class Context:
    """What one product call may do for an empty answer (set by ``run_compiled``)."""
    #: the record's signature for this rung and template, when it has one
    learned: Optional[Dict[str, Any]] = None
    #: inputs known to have results (the drift anchor's, the proof's)
    positives: List[str] = field(default_factory=list)
    #: requests may be sent (``ANYUSE_EMPTY_PROBE``)
    probe: bool = True
    #: a signature may be learned and kept
    learn: bool = True
    #: the digest of the template a learned signature is for
    digest: str = ""
    new_learned: Optional[Dict[str, Any]] = None
    requests: int = 0
    model_calls: int = 0


Fetch = Callable[[str], Page]


class Fetcher:
    """``fetch`` with one answer per input: an input asked twice in one verdict
    (the learning's made-up search, then the same-moment check) is sent once."""

    def __init__(self, fetch: Fetch):
        self._fetch = fetch
        self._seen: Dict[str, Page] = {}
        self.requests = 0

    def __call__(self, value: str) -> Page:
        from anyuse.execution.api_compile import normalise
        key = normalise(value) or str(value)
        if key not in self._seen:
            self.requests += 1
            try:
                page = self._fetch(value)
            except Exception as exc:                  # noqa: BLE001 - no answer, never a crash
                page = Page(value=value, ok=False, error=f"the request failed ({type(exc).__name__})")
            self._seen[key] = page
        return self._seen[key]


# ---------------------------------------------------------------------------
# settings and versions
# ---------------------------------------------------------------------------

def probe_enabled() -> bool:
    """May AnyUse search made-up inputs to learn and check a site's empty page?"""
    try:
        from anyuse.settings import get as get_setting
        return bool(get_setting(PROBE_ENV))
    except Exception:                                 # noqa: BLE001
        return str(os.environ.get(PROBE_ENV, "on")).strip().lower() not in ("0", "off", "false", "no")


def norm_version() -> str:
    from anyuse.execution.api_compile import NORMALISE_VERSION
    return f"{NORMALISE_VERSION}.{MASK_VERSION}"


def usable(sig: Any) -> bool:
    """A signature in this format, made with this normaliser."""
    return isinstance(sig, dict) and sig.get("v") == VERSION and sig.get("norm") == norm_version()


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# lines
# ---------------------------------------------------------------------------

def _digit_run(m: Any) -> str:
    run = m.group(0)
    return "0" if all(unicodedata.digit(ch, -1) == 0 for ch in run) else "#"


def mask(raw: str, value: str) -> str:
    """``raw`` normalised (``api_compile.normalise``, any script), every whole-word
    occurrence of ``value`` replaced by ``{q}`` and every digit run by ``#``
    (a run of zeros stays ``0``: "0 results" is not "12 results")."""
    from anyuse.execution.api_compile import normalise, word_spans
    text, q = normalise(raw), normalise(value)
    if q and text:
        for start, end in reversed(word_spans(text, q)):
            text = text[:start] + "{q}" + text[end:]
    return _DIGITS.sub(_digit_run, text)


def _path(block: Any) -> str:
    """Where a line stands: the hash of its block's chain of element signatures
    (tag and classes), digit runs in class names masked."""
    from anyuse.execution.result_contract import signature
    parts: List[str] = []
    cur = block
    while cur is not None and getattr(cur, "tag", ""):
        parts.append(_DIGITS.sub("#", signature(cur)))
        cur = cur.parent
    return _h("/".join(reversed(parts)))[:12]


def _on_screen(text: str) -> Callable[[str], bool]:
    screen = _WS.sub("", text).casefold() if (text or "").strip() else ""
    return lambda line: not screen or _WS.sub("", line).casefold() in screen


def visible(page: Page) -> List[Tuple[str, Any, Any]]:
    """``(text, block, heading)`` of each line a person sees: the markup parsed
    visible-only (``result_contract.visible_lines``) and, with the browser's
    text, only the lines on screen."""
    if page._vis is None:
        from anyuse.execution.html_extract import parse
        from anyuse.execution.result_contract import visible_lines
        try:
            root = page.root if page.root is not None else parse(
                page.markup or "", visible_only=True, implied_end_tags=page.implied_end_tags)
            vis = visible_lines(root)
        except ValueError:
            vis = []
        shown = _on_screen(page.text)
        page._vis = [x for x in vis if shown(x[0])]
    return page._vis


#: how much of a demonstration page's visible text a signature reads (what a proof case keeps,
#: ``harness_compile._page_record``)
TEXT_CHARS = 4000


def lines(page: Page) -> List[Line]:
    if page._lines is None:
        if page.kind == "text":
            # a demonstration page's visible text: its lines, with no place in a tree
            page._lines = [Line(mask(t, page.value), t.strip(), "")
                           for t in (page.text or "")[:TEXT_CHARS].splitlines() if t.strip()]
        else:
            page._lines = [Line(mask(t, page.value), t, _path(b), suggests(b, page))
                           for t, b, _hd in visible(page)]
    return page._lines


def _has_word(masked: str) -> bool:
    return any(ch.isalnum() for ch in masked.replace("{q}", ""))


def _contains(masked: str, part: str) -> bool:
    """``part`` stands in the normalised line ``masked`` as whole words."""
    return bool(part) and f" {part} " in f" {masked} "


def _line_links(block: Any) -> List[str]:
    """The targets of the links in the line ``block`` shows, read the way
    ``result_contract._links_in_line`` finds them: an ``a`` around the block, or
    an ``a`` with text among its inline content."""
    from anyuse.execution.result_contract import _INLINE_TAGS, _has_alnum
    out: List[str] = []
    cur = block
    while cur is not None:
        if cur.tag == "a":
            return [str(getattr(cur, "href", "") or "")]
        cur = cur.parent
    stack = list(getattr(block, "children", None) or [])
    while stack:
        n = stack.pop()
        if n.tag == "a" and _has_alnum(n.text()):
            out.append(str(getattr(n, "href", "") or ""))
        elif n.tag in _INLINE_TAGS:
            stack.extend(n.children)
    return out


def _other_search(href: str, page: Page) -> bool:
    """``href`` opens the site's own search for another input: the page's own
    address (host and path) with the query value that holds this input set to
    another value, as a "did you mean" or a related search links it."""
    from urllib.parse import parse_qsl, urljoin, urlsplit
    from anyuse.execution.api_compile import normalise
    want = normalise(page.value)
    if not (href and page.url and want):
        return False
    try:
        own = urlsplit(page.url)
        to = urlsplit(urljoin(page.url, href))
    except ValueError:
        return False
    keys = {k for k, v in parse_qsl(own.query, keep_blank_values=True) if normalise(v) == want}
    if not keys or to.netloc.lower() != own.netloc.lower() \
            or to.path.rstrip("/") != own.path.rstrip("/"):
        return False
    vals = [v for k, v in parse_qsl(to.query, keep_blank_values=True) if k in keys]
    return bool(vals) and all(normalise(v) and normalise(v) != want for v in vals)


def suggests(block: Any, page: Page) -> bool:
    """The line ``block`` shows links somewhere, and every link opens the site's
    own search for another input (:func:`_other_search`): a suggestion on the
    page, never a result row or an error."""
    links = _line_links(block) if block is not None and page.url else []
    return bool(links) and all(_other_search(h, page) for h in links)


# ---------------------------------------------------------------------------
# made-up inputs
# ---------------------------------------------------------------------------

_SYL = ("zor", "qua", "vex", "lim", "brak", "tu", "nix", "ol", "ev", "zir", "mun", "dax", "pel",
        "quo", "rin", "yth", "gra", "ux", "fen", "kaz", "vor", "jil", "trax", "om", "plu", "zeb")
_SEPARATORS = (" ", "-", "/", "_")


def _shape(like: Sequence[str]) -> Tuple[str, int, str, int, bool]:
    """``(kind, digits, separator, parts, lower)`` of the inputs ``like``."""
    vals = [v.strip() for v in like if v and v.strip()]
    if vals and all(v.isdigit() for v in vals):
        lengths = sorted(len(v) for v in vals)
        return "digits", lengths[len(lengths) // 2], "", 1, True
    sep, parts = "", 1
    if vals:
        counts = {s: sum(1 for v in vals if s in v) for s in _SEPARATORS}
        best = max(_SEPARATORS, key=lambda s: (counts[s], -_SEPARATORS.index(s)))
        if counts[best] * 2 > len(vals):
            sep = best
            n = sorted(len([p for p in v.split(best) if p]) for v in vals)
            parts = max(1, min(3, n[len(n) // 2]))
    lower = all(v == v.lower() for v in vals)
    return "words", 0, sep, parts, lower


def nonsense_values(like: Sequence[str], seed: str, n: int = PROBES) -> List[str]:
    """``n`` deterministic made-up inputs shaped like ``like`` (digits for digits,
    the same separator and number of parts, lower case when ``like`` is), seeded by
    ``seed`` (the template's URL). None shares a normalised word with ``like``
    or with another."""
    from anyuse.execution.api_compile import normalise
    like = [str(v) for v in like if str(v or "").strip()]
    taken = {t for v in like for t in normalise(v).split()}
    rng = random.Random(int(_h(f"empty-page|{seed}"), 16))
    kind, digits, sep, parts, lower = _shape(like)
    out: List[str] = []
    for _attempt in range(200):
        if len(out) >= n:
            break
        if kind == "digits":
            v = rng.choice("123456789") + "".join(rng.choice("0123456789")
                                                  for _ in range(max(3, digits) - 1))
        else:
            words = ["".join(rng.choice(_SYL) for _ in range(3)) for _ in range(parts)]
            v = (sep or " ").join(words if lower else [w.capitalize() for w in words])
        toks = normalise(v).split()
        if not toks or any(t in taken for t in toks):
            continue
        out.append(v)
        taken.update(toks)
    return out


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _base(kind: str, probes: Sequence[Page], results: int) -> Dict[str, Any]:
    return {"v": VERSION, "norm": norm_version(), "kind": kind,
            "probes": [p.value for p in probes], "result_pages": results}


def learn(kind: str, results: Sequence[Page], probes: Sequence[Page]) -> Optional[Dict[str, Any]]:
    """The site's empty page from ``probes`` (pages for made-up inputs) against
    ``results`` (pages that had results). ``None`` when a made-up search got no
    answer (a transport error, a timeout, a server error, a rate limit, a block):
    nothing can be said yet. ``kind: "none"`` with ``why`` when the site has no
    empty page of its own to learn: it answers made-up inputs with results or
    with an error, or its empty page shows nothing its result pages do not."""
    probes, results = list(probes), [r for r in results if r.ok]
    if len(probes) < 2:
        return None
    if any(not p.ok and not p.definite for p in probes):
        return None
    for p in probes:
        if not p.ok:
            return {**_base("none", probes, len(results)),
                    "why": f"a made-up input got an error answer ({(p.error or str(p.status))[:80]})"}
        if p.items:
            return {**_base("none", probes, len(results)),
                    "why": f"the site lists {len(p.items)} result(s) for a made-up input"}
    if not results:
        return None
    return _learn_lines(kind, results, probes) if kind in ("html", "text") \
        else _learn_json(results, probes)


def _learn_lines(kind: str, results: Sequence[Page], probes: Sequence[Page]) -> Dict[str, Any]:
    seen = [lines(p) for p in probes]
    sets = [{ln.masked for ln in ls} for ls in seen]
    stable = set.intersection(*sets)
    on_results = [{ln.masked for ln in lines(r)} for r in results]
    r_all = set().union(*on_results)
    r_common = set.intersection(*on_results)
    marks: List[str] = []
    for ln in seen[0]:
        m = ln.masked
        if m in stable and m not in r_all and m not in marks and _has_word(m) \
                and len(m) <= MAX_MARK_CHARS:
            marks.append(m)
            if len(marks) >= MAX_MARKS:
                break
    base = _base(kind, probes, len(results))
    if not marks:
        return {**base, "kind": "none",
                "why": "the page for a made-up input shows no line the result pages lack"}
    from anyuse.execution.result_contract import _reports_error
    err = next((m for m in (_reports_error(ln.raw, probes[0].value) for ln in seen[0]
                            if ln.masked in stable and ln.masked not in r_all) if m), None)
    if err:
        # the statement rule's error page: never learned as the page of a search that found nothing
        return {**base, "kind": "none",
                "why": f"the page for a made-up input reports an error ({err.group(0)[:60]!r})"}
    # text lines have no place in a tree, so no line of theirs is passed over as changing content
    places = sorted({ln.path for ls in seen for ln in ls if ln.masked not in stable}) \
        if kind == "html" else []
    return {**base, "marks": marks, "echo": any("{q}" in m for m in marks),
            "page": sorted({_h(m) for m in stable})[:MAX_PAGE_LINES],
            "shared": sorted({_h(m) for m in stable & r_common}), "volatile": places}


_MISSING = object()


def _keys_of(path: str) -> Tuple[str, ...]:
    """The object keys of a JSON path up to its first list step."""
    from anyuse.execution.api_step import _split_path
    out: List[str] = []
    for tok in _split_path(path or ""):
        if not isinstance(tok, str) or tok == "*":
            break
        out.append(tok)
    return tuple(out)


def _leaves(data: Any, prefix: Tuple[str, ...] = (), depth: int = 0
            ) -> Iterable[Tuple[Tuple[str, ...], Any]]:
    """``(key path, value)`` for every value reached through objects only (not lists)."""
    if not isinstance(data, dict) or depth > 12:
        return
    for k, v in data.items():
        p = prefix + (str(k),)
        yield p, v
        if isinstance(v, dict):
            yield from _leaves(v, p, depth + 1)


def _get(data: Any, path: Sequence[str]) -> Any:
    cur = data
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]
    return cur


def _count(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _learn_json(results: Sequence[Page], probes: Sequence[Page]) -> Dict[str, Any]:
    from anyuse.execution.api_compile import normalise
    own = _keys_of(results[0].list_path)
    paths = [p for p, _v in _leaves(probes[0].data)]
    counts, echoes, lists = [], [], []
    for p in paths:
        on_probes = [_get(pr.data, p) for pr in probes]
        on_results = [_get(r.data, p) for r in results]
        if all(_count(v) == 0 for v in on_probes) and all(
                _count(v) is not None and _count(v) > 0 and _count(v) >= len(r.items)
                for v, r in zip(on_results, results)):
            exact = all(_count(v) == len(r.items) for v, r in zip(on_results, results))
            counts.append((0 if exact else 1, len(p), p))
        if all(isinstance(v, str) and normalise(v) and normalise(v) == normalise(pg.value)
               for v, pg in zip(on_probes + on_results, list(probes) + list(results))):
            echoes.append((len(p), p))
    for p, _v in _leaves(results[0].data):
        if p == own or p in lists:
            continue
        if all(isinstance(_get(r.data, p), list) and _get(r.data, p) for r in results) and all(
                _get(pr.data, p) in (_MISSING, None, []) for pr in probes):
            lists.append(p)
    count_path = list(min(counts)[2]) if counts else None
    echo_path = list(min(echoes)[1]) if echoes else None
    base = _base("json", probes, len(results))
    if count_path is None and echo_path is None:
        return {**base, "kind": "none",
                "why": "the answer for a made-up input has no zero count and does not name the input"}
    # every field the made-up answers hold: an answer with others is not their kind of answer
    keys = sorted({p for pr in probes for p, _v in _leaves(pr.data)})[:MAX_PAGE_LINES]
    return {**base, "count_path": count_path, "echo_path": echo_path, "echo": bool(echo_path),
            "result_lists": [list(p) for p in lists], "list": list(own),
            "keys": [list(p) for p in keys]}


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def match(sig: Dict[str, Any], page: Page, *, fetch: Optional[Fetch] = None,
          positives: Sequence[str] = (), positive_ok: Optional[Callable[[Page], bool]] = None
          ) -> Evidence:
    """Is ``page`` the site's empty page ``sig`` for its input? EMPTY, NOT_EMPTY or UNKNOWN.

    ``fetch(value)`` searches another input through the call's executor; without
    it no request is sent, and what only a request can settle is UNKNOWN.
    ``positives`` are inputs known to have results; ``positive_ok(page)`` says
    whether a page for one of them passes the rung's own list or sparse check."""
    if not usable(sig) or sig.get("kind") != page.kind or page.kind not in ("html", "json"):
        return Evidence(why="no learned empty page for this kind of answer")
    if page.kind == "html":
        return _match_html(sig, page, fetch, positives, positive_ok)
    return _match_json(sig, page, fetch, positives, positive_ok)


#: inputs known to have results tried before a search is said to no longer read its input
#: (one may simply have lost its results)
POSITIVES = 2


def _others(positives: Sequence[str], value: str) -> List[str]:
    from anyuse.execution.api_compile import normalise
    mine = normalise(value)
    return [v for v in positives if normalise(v) and normalise(v) != mine][:POSITIVES]


def positive_check(page: Page, positives: Sequence[str], fetch: Fetch,
                   positive_ok: Callable[[Page], bool]) -> Tuple[str, Optional[Page], str]:
    """Search inputs known to have results (at most :data:`POSITIVES`) until one
    still finds them. ``("ok", that page, "")``; ``("none", None, why)`` when one
    got no answer or there is none to search; ``("fail", None, why)`` when each
    searched finds nothing: the search no longer reads its input, or the site
    answers every search this way."""
    tried = []
    for pos in _others(positives, page.value):
        got = fetch(pos)
        if not got.ok and not got.definite:
            return "none", None, f"the check with {pos!r}, an input known to have results, got no answer"
        if positive_ok(got):
            return "ok", got, ""
        tried.append(pos)
    if not tried:
        return "none", None, "no input known to have results can be checked here"
    shown = " and ".join(repr(p) for p in tried)
    return "fail", None, (f"the input{'s' if len(tried) > 1 else ''} {shown}, which had results, "
                          f"find{'' if len(tried) > 1 else 's'} none either: the search no longer "
                          f"reads its input, or the site answers every search this way")


def _control(sig: Dict[str, Any], page: Page, fetch: Optional[Fetch], positives: Sequence[str],
             positive_ok: Optional[Callable[[Page], bool]], why: str = "") -> Optional[Evidence]:
    """An input known to have results must still find them (:func:`positive_check`).
    Needed when the empty page does not name the input, and when ``why`` (the
    site's pages changed) says so."""
    if sig.get("echo") and not why:
        return None
    reason = why or "the site's empty page does not name the input"
    if fetch is None or positive_ok is None or not _others(positives, page.value):
        return Evidence(why=f"{reason}, and no input known to have results can be checked here")
    state, _got, said = positive_check(page, positives, fetch, positive_ok)
    if state == "fail":
        return Evidence(NOT_EMPTY, why=said)
    if state == "none":
        return Evidence(why=said)
    return None


#: why :func:`_same_moment` found the site's empty page changed
CHANGED = "the site's empty page changed: a made-up search at the same moment lacks its line"


def _same_moment(sig: Dict[str, Any], fetch: Fetch) -> Tuple[Optional[Page], str]:
    """One made-up search now: its page when it is still the site's empty page, else why not."""
    probe = fetch(str(sig["probes"][0]))
    if not probe.ok or probe.items:
        return None, "a made-up search at the same moment got no empty page to compare with"
    seen = {ln.masked for ln in lines(probe)}
    if any(m not in seen for m in sig.get("marks") or []):
        return None, CHANGED
    return probe, ""


def _mark_line(ln: Line, mark: str) -> bool:
    """``ln`` shows ``mark``: as the whole line, or followed in one sentence by a
    suggestion ("… for “aple”. Did you mean apple?")."""
    return ln.masked == mark or (ln.suggestion and _contains(ln.masked, mark))


def _match_html(sig, page, fetch, positives, positive_ok) -> Evidence:
    marks = list(sig.get("marks") or [])
    ls = lines(page)
    missing = [m for m in marks if not any(_mark_line(ln, m) for ln in ls)]
    if not marks or missing:
        why = (f"the page is not the site's empty page: it lacks the line "
               f"{(missing or [''])[0][:80]!r}")
        if fetch is None or not sig.get("probes"):
            return Evidence(why=why)
        probe, changed = _same_moment(sig, fetch)
        if probe is None:
            return Evidence(why=f"{why}; {changed}", stale=changed == CHANGED)
        # the site still answers a search that finds nothing with its empty page
        return Evidence(why=f"{why}, which a made-up search at the same moment still shows",
                        not_site_page=True)
    known, places = set(sig.get("page") or []), set(sig.get("volatile") or [])
    # a suggestion links only to the site's search for other inputs: the empty page for an
    # input near a real one, not a row or an error
    extra = [ln for ln in ls if _h(ln.masked) not in known and ln.path not in places
             and not ln.suggestion]
    changed = ""
    if extra:
        if fetch is None or not sig.get("probes"):
            return Evidence(why=(f"the page shows the site's empty page and {len(extra)} line(s) "
                                 f"it lacks (first {extra[0].raw[:80]!r}); no request may compare "
                                 f"them here"))
        probe, why = _same_moment(sig, fetch)
        if probe is None:
            return Evidence(why=why)
        seen = {ln.masked for ln in lines(probe)}
        left = [ln for ln in extra if ln.masked not in seen]
        if left:
            return Evidence(NOT_EMPTY, why=(
                f"the page holds {len(left)} line(s) the site's empty page lacks (first "
                f"{left[0].raw[:80]!r}): results the selector no longer reads, or an error"))
        # the same new lines on a made-up search: the site's pages changed (new chrome, or a
        # banner on every page), so its search must still find what it found before
        changed = (f"the site's empty page now shows {len(extra)} new line(s) (first "
                   f"{extra[0].raw[:80]!r})")
    ctrl = _control(sig, page, fetch, positives, positive_ok, changed)
    if ctrl is not None:
        return ctrl
    line = next(ln.raw for ln in ls if _mark_line(ln, marks[0]))
    return Evidence(EMPTY, line=line, by=BY_SITE, why=(
        f"the page is the site's own empty page for this input: it shows {line[:80]!r}, as "
        f"a search for a made-up input does"))


def _dotted(path: Sequence[str]) -> str:
    return ".".join(str(k) for k in path)


def _match_json(sig, page, fetch, positives, positive_ok) -> Evidence:
    from anyuse.execution.api_compile import normalise
    from anyuse.execution.api_step import json_path
    listed = json_path(page.data, page.list_path) if page.list_path else page.data
    if not isinstance(listed, list) or listed:
        return Evidence(why="the result path is not an empty list")
    said = ""
    cp, ep = sig.get("count_path"), sig.get("echo_path")
    if cp:
        v = _count(_get(page.data, cp))
        if v is None:
            return Evidence(why=f"the answer lacks the count field {_dotted(cp)!r}")
        if v != 0:
            return Evidence(NOT_EMPTY, why=(f"the answer says {_dotted(cp)}={v}: it has results "
                                            f"the list path no longer reads"))
        said = f"{_dotted(cp)}=0"
    if ep:
        v = _get(page.data, ep)
        if not isinstance(v, str):
            return Evidence(why=f"the answer lacks the field {_dotted(ep)!r} that names the input")
        if normalise(v) != normalise(page.value):
            return Evidence(NOT_EMPTY, why=(f"the answer names {v[:60]!r}, not the input: it is "
                                            f"not the answer to this search"))
        said = said or f"{_dotted(ep)}={v[:60]!r}"
    for lp in sig.get("result_lists") or []:
        v = _get(page.data, lp)
        if isinstance(v, list) and v:
            return Evidence(NOT_EMPTY, why=(f"the answer's {_dotted(lp)!r} holds {len(v)} "
                                            f"entr{'y' if len(v) == 1 else 'ies'}, empty for a "
                                            f"made-up search: results the list path no longer reads"))
    changed = ""
    if "keys" in sig:
        # fields no made-up answer held (an error, results under a new name): compared with a
        # made-up search at the same moment, as an HTML page's new lines are
        known = {tuple(k) for k in sig.get("keys") or []}
        extra = [p for p, _v in _leaves(page.data) if p not in known]
        if extra:
            why = (f"the answer holds {len(extra)} field(s) the site's empty answer lacks "
                   f"(first {_dotted(extra[0])!r})")
            if fetch is None or not sig.get("probes"):
                return Evidence(why=f"{why}; no request may compare them here")
            probe = fetch(str(sig["probes"][0]))
            seen = {p for p, _v in _leaves(probe.data)} if probe.ok and not probe.items else set()
            left = [p for p in extra if p not in seen]
            if left:
                return Evidence(why=(f"the answer holds {len(left)} field(s) a made-up search at "
                                     f"the same moment lacks (first {_dotted(left[0])!r})"))
            changed = f"the site's empty answer now holds {len(extra)} new field(s)"
    ctrl = _control(sig, page, fetch, positives, positive_ok, changed)
    if ctrl is not None:
        return ctrl
    return Evidence(EMPTY, line=said, by=BY_SITE,
                    why=f"the answer is the site's own empty answer for this input ({said})")


# ---------------------------------------------------------------------------
# demonstration tools (MCP web tools): learned at their proof, matched accept-only
# ---------------------------------------------------------------------------

def match_text(sig: Optional[Dict[str, Any]], read: Dict[str, Any], value: str) -> Evidence:
    """Is the demonstration page ``read`` (its visible ``text``) the site's empty page
    for ``value``? EMPTY or UNKNOWN, never NOT_EMPTY, and no request: every mark is
    there, they name the input, and no line is new. A tool's own run reads it
    (``demo_compile.judge_result_page``, ``demo_tool.wait_results``); an empty page
    that does not name the input is left to the page's statement, since a pure read
    cannot check that the search still reads its input."""
    if not usable(sig) or sig.get("kind") != "text" or not sig.get("echo"):
        return Evidence(why="no learned empty page that names the input")
    p = Page("text", str(value or ""), text=str((read or {}).get("text") or ""))
    ls = lines(p)
    have = {ln.masked for ln in ls}
    marks = list(sig.get("marks") or [])
    if not marks or any(m not in have for m in marks):
        return Evidence(why="the page is not the site's empty page")
    known = set(sig.get("page") or [])
    if any(_h(ln.masked) not in known for ln in ls):
        return Evidence(why="the page shows lines the site's empty page lacks")
    line = next(ln.raw for ln in ls if ln.masked == marks[0])
    return Evidence(EMPTY, line=line, by=BY_SITE, why=(
        f"the page is the site's own empty page for this input: it shows {line[:80]!r}, as a "
        f"search for a made-up input does"))


def learn_demo(replay_one: Callable[[str], Dict[str, Any]], cases: Sequence[Dict[str, Any]],
               like: Sequence[str], seed: str) -> Optional[Dict[str, Any]]:
    """The empty page of a demonstration tool that just passed its proof: the
    passing cases' pages (``page.text``) against :data:`PROBES` replays of made-up
    inputs (``replay_one(value)``, a ``harness_compile.replay_web`` result)."""
    results = [Page("text", str(c.get("value") or ""), text=str((c.get("page") or {}).get("text") or ""))
               for c in cases or () if isinstance(c, dict) and c.get("passed")
               and (c.get("page") or {}).get("text")]
    if not results:
        return None
    probes = []
    for v in nonsense_values(like, seed=seed):
        try:
            out = replay_one(v) or {}
        except Exception:                             # noqa: BLE001 - no answer, never a crash
            return None
        text = str((out.get("final_page") or {}).get("text") or "")
        status = str(out.get("status") or "")
        if not text or status == "blocked":
            return None
        got = (out.get("output") or {}).get("results") if isinstance(out.get("output"), dict) else None
        probes.append(Page("text", v, text=text,
                           items=list(got or []) if status == "ok" else []))
    sig = learn("text", results, probes)
    if sig is not None:
        sig.update({"learned": "proof", "at": _now()})
    return sig


# ---------------------------------------------------------------------------
# learning at the first empty answer of a tool proved before
# ---------------------------------------------------------------------------

def _retry(ctx: Context, why: str) -> Dict[str, Any]:
    return {"v": VERSION, "norm": norm_version(), "kind": "none", "why": why,
            "retry_after": round(time.time() + RETRY_AFTER_S), "for": ctx.digest}


def learn_now(page: Page, ctx: Context, fetch: Fetch, positive_ok: Callable[[Page], bool],
              *, again: Optional[Dict[str, Any]] = None
              ) -> Tuple[Optional[Dict[str, Any]], Optional[Evidence]]:
    """Learn the empty page of a tool that has none: an input known to have
    results, then :data:`PROBES` made-up inputs. ``(signature, None)``, or
    ``(None, NOT_EMPTY evidence)`` when the input known to have results finds none
    now. A search that got no answer leaves a retry marker in ``ctx.new_learned``.

    ``again``: the tool's learned page, which no longer matches the site. When
    the site now has no empty page to learn (results for anything, an error), or
    one that no longer names the input as the learned page did (an error page
    rarely does), that may pass: the old page stays and a retry marker is left."""
    if not _others(ctx.positives, page.value):
        return None, None
    state, got, said = positive_check(page, ctx.positives, fetch, positive_ok)
    if state == "none":
        ctx.new_learned = _retry(ctx, said)
        return None, None
    if state == "fail" or got is None:
        return None, Evidence(NOT_EMPTY, why=said)
    values = nonsense_values([got.value, page.value, *ctx.positives], seed=ctx.digest or page.url)
    probes = [fetch(v) for v in values]
    sig = learn(page.kind, [got], probes)
    if sig is None:
        ctx.new_learned = _retry(ctx, "a made-up search got no answer")
        return None, None
    if again is not None and sig.get("kind") == "none":
        ctx.new_learned = _retry(ctx, str(sig.get("why") or "no empty page to learn now"))
        return None, None
    if again is not None and again.get("echo") and not sig.get("echo"):
        ctx.new_learned = _retry(ctx, "the page for a made-up input no longer names the input")
        return None, None
    sig.update({"learned": "after the site changed it" if again is not None else "first empty answer",
                "at": _now(), "for": ctx.digest})
    ctx.new_learned = ctx.learned = sig
    return sig, None


def positive_values(record: Dict[str, Any]) -> List[str]:
    """Inputs known to have results: the drift anchor's, then the passing proof cases'."""
    from anyuse.execution.api_compile import normalise
    from anyuse.execution.step_repair import anchor_of
    out: List[str] = []
    a = anchor_of(record or {})
    vals = [a["value"]] if a else []
    for case in ((record or {}).get("proof") or {}).get("cases") or []:
        if isinstance(case, dict) and case.get("passed") and case.get("value"):
            vals.append(str(case["value"]))
    seen = set()
    for v in vals:
        k = normalise(v)
        if k and k not in seen:
            seen.add(k)
            out.append(str(v))
    return out


def context_for(record: Dict[str, Any], rung: str, template_dict: Dict[str, Any], *,
                learn: bool) -> Context:
    """The context of one call's ``rung`` (``request`` or ``browser``) of ``record``.

    The record's signature counts only when it was learned for this exact
    template (``for``) with this normaliser. A pending retry blocks learning
    until its time; ``learn=False`` (a pinned watch copy) never learns."""
    from anyuse.execution.workflow_schema import template_digest
    digest = template_digest(template_dict or {})
    sig = ((record or {}).get("empty_page") or {}).get(rung)
    learned, waiting = None, False
    if usable(sig) and sig.get("for") == digest:
        if sig.get("retry_after") is not None:
            try:
                waiting = time.time() < float(sig.get("retry_after"))
            except (TypeError, ValueError):
                waiting = False
        else:
            learned = sig
    on = probe_enabled()
    return Context(learned=learned, positives=positive_values(record), probe=on,
                   learn=bool(learn and on and not waiting), digest=digest)


# ---------------------------------------------------------------------------
# the model step's candidates
# ---------------------------------------------------------------------------

def statement_zone(page: Page, sig: Optional[Dict[str, Any]] = None
                   ) -> Tuple[List[MC.Candidate], bool]:
    """The lines that can state "no results" for this input: every line naming the
    input, and up to ``result_contract.SUMMARY_LINES`` lines below it, stopping at
    a line naming the input again or holding a link. Only link-free lines of at
    most :data:`MAX_MARK_CHARS` characters with a letter or digit are candidates;
    the empty page's marks come first. ``complete``: every line of the zone is a
    candidate, so a "no" for all of them covers each line the statement rule
    (step 3) could accept."""
    from anyuse.execution.result_contract import SUMMARY_LINES, _links_in_line, _names_all
    vis = visible(page)
    zone: List[int] = []
    complete = True

    def link(block: Any) -> bool:
        return block is not None and bool(_links_in_line(block))

    for i, (t, _b, _hd) in enumerate(vis):
        if not _names_all(t, page.value):
            continue
        if i not in zone:
            zone.append(i)
        for j in range(i + 1, min(len(vis), i + 1 + SUMMARY_LINES)):
            tj, bj, _hj = vis[j]
            if _names_all(tj, page.value):
                break
            if link(bj):
                complete = False
                break
            if j not in zone:
                zone.append(j)
    picked: List[int] = []
    for k in zone:
        t, b, _hd = vis[k]
        if len(t) <= MAX_MARK_CHARS and any(ch.isalnum() for ch in t) and not link(b):
            picked.append(k)
        else:
            complete = False
    marks = set((sig or {}).get("marks") or []) if usable(sig) else set()
    if marks:
        picked.sort(key=lambda k: (mask(vis[k][0], page.value) not in marks, k))
    if len(picked) > MC.MAX_CANDIDATES:
        complete = False
    picked = picked[:MC.MAX_CANDIDATES]
    cands = [MC.Candidate(text=vis[k][0], context="\n".join(t for t, _b, _hd in vis[max(0, k - 2):k]),
                          ref=k) for k in picked]
    return cands, complete


def model_step(page: Page, sig: Optional[Dict[str, Any]]) -> Evidence:
    """Step 2: ask the configured check model about the statement zone. Asks
    nothing outside a ``model_check`` scope (a proof, a heal, a watch run)."""
    if not MC.available(EMPTY_STATEMENT.kind):
        return Evidence(why="no check model is asked here")
    cands, complete = statement_zone(page, sig)
    if not cands:
        return Evidence(why="no line of the page names the input")
    texts = {t for t, _b, _hd in visible(page)}
    v = MC.check(EMPTY_STATEMENT, page.value, cands, verify=lambda c: c.text in texts,
                 guard_obs={"url": page.url, "text": page.text or "\n".join(sorted(texts)),
                            "markup": page.markup, "items": []},
                 where="empty_answer")
    ev = Evidence(model_calls=v.calls, backend=v.backend, p=v.p)
    if v.yes and v.candidate is not None:
        ev.state, ev.line, ev.by = EMPTY, v.candidate.text, BY_MODEL
        ev.why = (f"the check model read {v.candidate.text[:80]!r} as the site saying this search "
                  f"found nothing")
    elif v.no and complete:
        ev.state = NOT_EMPTY
        ev.why = ("the check model read none of the page's lines that name the input as saying "
                  "this search found nothing")
    else:
        ev.why = f"the check model gave no answer ({v.why[:80]})"
    return ev


def error_check(page: Page, lines: Sequence[str]) -> MC.Verdict:
    """Ask the configured check model, one question per line, whether each of
    ``lines`` (the page's lines that read as error reports) reports that the site
    failed to answer. A yes is verified against the page's own lines. Asks
    nothing outside a ``model_check`` scope."""
    if not MC.available(ERROR_LINE.kind):
        return MC.Verdict(why="no check model is asked here")
    vis = visible(page)
    texts = {t for t, _b, _hd in vis}
    texts.update(ln.strip() for ln in (page.text or "").splitlines() if ln.strip())
    order = [t for t, _b, _hd in vis]

    def context(line: str) -> str:
        k = order.index(line) if line in order else -1
        return "\n".join(order[max(0, k - 2):k]) if k > 0 else ""

    cands = [MC.Candidate(text=t, context=context(t), ref=i) for i, t in enumerate(lines)]
    return MC.check(ERROR_LINE, page.value, cands, verify=lambda c: c.text in texts,
                    guard_obs={"url": page.url, "text": page.text or "\n".join(order),
                               "markup": page.markup, "items": []},
                    where="empty_answer.error")


def unread_rows(page: Page) -> List[str]:
    """The page's lines that read as result rows: each names the input and holds a
    link (``result_contract._summary_above`` reads a row the same way), and at
    least two of them stand in the same place of the page (the same chain of
    element signatures), as the rows of a list do. A single link naming the input
    (a menu entry) is not a row, and neither is a line whose links all open the
    site's own search for other inputs (related searches, :func:`suggests`). On a
    page that extracted nothing, such rows are results the selector no longer reads."""
    from anyuse.execution.result_contract import _links_in_line, _names_all
    found = [(t, _path(b)) for t, b, _hd in visible(page)
             if b is not None and _names_all(t, page.value) and _links_in_line(b)
             and not suggests(b, page)]
    counts: Dict[str, int] = {}
    for _t, path in found:
        counts[path] = counts.get(path, 0) + 1
    return [t for t, path in found if counts[path] >= 2]


def veto(sig: Optional[Dict[str, Any]], line: str, value: str) -> bool:
    """Does the statement rule's ``line`` also stand on the site's result pages?"""
    return bool(line) and usable(sig) and sig.get("kind") == "html" \
        and _h(mask(line, value)) in set(sig.get("shared") or [])


# ---------------------------------------------------------------------------
# the browser replay's pages
# ---------------------------------------------------------------------------

def page_from_replay(raw: Dict[str, Any], value: str) -> Page:
    """A :class:`Page` from ``cdp_replay.ReplayExecutor.execute(..., _raw=True)``."""
    if not isinstance(raw, dict) or not raw.get("success"):
        return Page("html", value, ok=False,
                    error=str((raw or {}).get("error") or "no answer")[:200])
    status = raw.get("status")
    status = int(status) if isinstance(status, int) else 0
    served = status == 0 or 200 <= status < 300
    return Page("html", value, markup=str(raw.get("dom") or ""), text=str(raw.get("text") or ""),
                items=list(raw.get("items") or []), status=status, url=str(raw.get("url") or ""),
                ok=served, definite=(not served and 400 <= status < 500 and status != 429),
                error="" if served else f"HTTP {status}")
