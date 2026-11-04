"""Rescan: a browser-free heal candidate for a rotated persisted-query id.

GraphQL sites address an operation by a persisted id instead of sending the
document: X's ``queryId`` in the path, Meta's ``doc_id`` in a form body,
Apollo's ``extensions.persistedQuery.sha256Hash``. The id rotates when the site
ships a new bundle, and the compiled template's request then fails although
nothing about the operation changed. The new id is printed in the site's own
JavaScript next to a stable name (the operationName, the module name), so it
can be read back without a browser.

This module is pure: no network, no browser, no clock of its own. The caller
(the repair ladder) fetches the trigger document and the bundles it lists
(:func:`bundle_urls`), hands their texts to :func:`rescan`, swaps the answer in
with :func:`apply_token`, and validates the healed template by replaying it.
:class:`HealGuard` is the loop guard that keeps a failing op from being healed
over and over.

Only JavaScript is read (:func:`code_segments`: bundles, and the inline
scripts of a document only when no bundle answers; never JSON data blocks,
markup or page text), and only a token that is a whole string literal of its
segment (:func:`literal_spans`, scanned from the segment's start) written as a
persisted-id property value (:func:`token_key`: ``queryId: "<id>"``,
``doc_id``, ``sha256Hash`` and kin, not a bare ``id``, or a Relay module's
``exports = "<id>"``) is a candidate, so a user's text that mentions the
operation next to a token-shaped string is never picked. An anchor whose own
group declares ``operationType: "mutation"`` (or subscription) is skipped.

The rules :func:`rescan` applies, in order:

1. **Shape.** A candidate has the recorded token's length and charset family
   (digits, lower hex, upper hex, base64url, base64). A mixed-case
   alphanumeric token is base64url: its successor may contain ``-``/``_``.
2. **Window.** It lies within :data:`ANCHOR_WINDOW` characters of an anchor
   occurrence (counted between the two, not from their starts).
3. **Standalone anchor.** The anchor counts only as a name of its own: no
   identifier character (``[A-Za-z0-9_$]``) touches it on either side, so
   ``Followers`` is not found inside ``FollowersYouKnow``.
4. **Code beats prose.** An occurrence with whitespace right next to it (a log
   message, a comment) ranks below one without, for the token first and then
   for the anchor.
5. **Own group beats nearer.** A token in the anchor's own bracket group
   ranks above a nearer one outside it. Scanning the text between them from
   the anchor towards the token (string literals and comments skipped), no
   bracket may close the anchor's group and no ``;`` may end a statement at
   its level.
6. **Nearest**, after all of the above.
7. **A tie is no answer.** If the best rank is shared by two different
   tokens, nothing is returned. The same token found twice is no tie.

Limits: the bracket scan knows string literals and comments, not regex
literals or template literal ``${}`` nesting; a token split across string
concatenation, or kept in a JSON manifest, is not seen; the anchor must be
given (:func:`default_anchor` proposes one).
"""
from __future__ import annotations

import copy
import html as html_lib
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

#: How far from an anchor a token may sit, in characters between the two.
ANCHOR_WINDOW = 300
#: An op healed less than this many seconds ago that fails again goes stale.
HEAL_RECENT_S = 600.0
#: How long a stale op is left alone.
STALE_FOR_S = 1800.0

_IDENT = "A-Za-z0-9_$"
_OPEN, _CLOSE = "([{", ")]}"

# charset families, narrowest first: (name, alphabet class)
_FAMILIES: Tuple[Tuple[str, str], ...] = (
    ("digits", "0-9"),
    ("hex", "0-9a-f"),
    ("HEX", "0-9A-F"),
    ("base64url", "A-Za-z0-9_-"),
    ("base64", "A-Za-z0-9+/"),
)


@dataclass(frozen=True)
class TokenShape:
    family: str
    length: int

    @property
    def alphabet(self) -> str:
        return dict(_FAMILIES)[self.family]

    def pattern(self) -> "re.Pattern[str]":
        # A token may not be cut out of a longer run: no letter, digit, '_' or
        # '-' may touch it (and no '+', '/', '=' for a base64 token, whose
        # alphabet they belong to). A '/' next to any other token is a path
        # separator (X's /graphql/<queryId>/<name>).
        a = self.alphabet
        edge = "A-Za-z0-9_+/=-" if self.family == "base64" else "A-Za-z0-9_-"
        return re.compile(rf"(?<![{edge}])[{a}]{{{self.length}}}(?![{edge}])")

    def matches(self, text: str) -> bool:
        return len(text) == self.length and bool(re.fullmatch(f"[{self.alphabet}]+", text))


def token_shape(token: str) -> Optional[TokenShape]:
    """The recorded token's shape, or ``None`` when it is no token at all."""
    if not token or len(token) < 8:
        return None
    for family, alphabet in _FAMILIES:
        if re.fullmatch(f"[{alphabet}]+", token):
            return TokenShape(family, len(token))
    return None


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------

@dataclass
class RescanEvidence:
    file: str
    offset: int
    anchor_offset: int
    distance: int
    in_code: bool
    anchor_in_code: bool
    same_group: bool

    def to_dict(self) -> dict:
        return {"file": self.file, "offset": self.offset,
                "anchor_offset": self.anchor_offset, "anchor_distance": self.distance,
                "in_code": self.in_code, "anchor_in_code": self.anchor_in_code,
                "same_group": self.same_group}


@dataclass
class RescanResult:
    """A candidate replacement token with its evidence, or no answer and why.

    ``unchanged`` is set when the best candidate IS the recorded token: the
    bundles still carry it, so rotation is not what broke the op.
    """
    token: Optional[str]
    reason: str
    evidence: Optional[RescanEvidence] = None
    unchanged: bool = False
    #: shaped tokens seen within the window of some anchor occurrence
    considered: int = 0
    #: anchor occurrences that stood as a name of their own
    anchors: int = 0
    #: the best-ranked token even when it is the recorded one (``unchanged``)
    best: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.token is not None

    def to_dict(self) -> dict:
        return {"token": self.token, "best": self.best, "reason": self.reason,
                "unchanged": self.unchanged,
                "evidence": self.evidence.to_dict() if self.evidence else None,
                "considered": self.considered, "anchors": self.anchors}


def _prose(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return before.isspace() or after.isspace()


def _significant(segment: str) -> List[str]:
    """Brackets and ``;`` of a code segment, outside string literals and
    ``//`` / ``/* */`` comments (an apostrophe in a comment opens nothing)."""
    out: List[str] = []
    quote = ""
    i = 0
    while i < len(segment):
        c = segment[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif segment.startswith("//", i):
            nl = segment.find("\n", i)
            i = len(segment) if nl < 0 else nl
            continue
        elif segment.startswith("/*", i):
            close = segment.find("*/", i + 2)
            i = len(segment) if close < 0 else close + 2
            continue
        elif c in "\"'`":
            quote = c
        elif c in _OPEN or c in _CLOSE or c == ";":
            out.append(c)
        i += 1
    return out


def _between(text: str, first: Tuple[int, int], second: Tuple[int, int]) -> str:
    """The code between two items, minus the quotes that wrap each of them."""
    lo, hi = first[1], second[0]
    if 0 < first[0] and lo < len(text) and text[first[0] - 1] in "\"'`" \
            and text[lo] == text[first[0] - 1]:
        lo += 1
    if hi > 0 and second[1] < len(text) and text[hi - 1] in "\"'`" \
            and text[second[1]] == text[hi - 1]:
        hi -= 1
    return text[lo:hi] if lo <= hi else ""


def same_group(text: str, anchor: Tuple[int, int], token: Tuple[int, int]) -> bool:
    """Does ``token`` sit in the anchor's own bracket group (or deeper in it)?"""
    if token[0] >= anchor[1]:
        sig = _significant(_between(text, anchor, token))
        order = sig
        opens, closes = _OPEN, _CLOSE
    else:
        sig = _significant(_between(text, token, anchor))
        order = list(reversed(sig))
        opens, closes = _CLOSE, _OPEN            # walking backwards
    depth = 0
    for c in order:
        if c in opens:
            depth += 1
        elif c in closes:
            depth -= 1
            if depth < 0:
                return False
        elif c == ";" and depth == 0:
            return False
    return True


#: Property names a persisted id is written under, as ``name: "<id>"``. A bare
#: ``id`` is not one: Relay store records carry entity ids under it.
TOKEN_KEYS = frozenset({"queryId", "queryID", "query_id", "doc_id", "docId", "documentId",
                        "documentID", "sha256Hash", "persistedQueryId", "operationId"})
#: String literals and comments of a JavaScript segment, scanned from its start.
_LEXEMES = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`""",
                      re.S)


def literal_spans(code: str) -> Set[Tuple[int, int]]:
    """``(open, close)`` quote offsets of every string literal in ``code``,
    found by scanning from the segment's start, comments skipped: a quote
    inside another literal (a user's text holding ``queryId:'...'``) opens
    nothing."""
    return {(m.start(), m.end() - 1) for m in _LEXEMES.finditer(code)
            if m.group(0)[:1] in "\"'`"}
_KEY_BEFORE = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)[\"']?\s*([:=])\s*$")
_OP_TYPE = re.compile(r"""(?:operationType|operationKind)["']?\s*:\s*["'](\w+)["']""")
_SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_JS_TYPES = ("", "text/javascript", "application/javascript", "module", "text/ecmascript",
             "application/ecmascript", "text/jsx", "text/babel")


def token_key(code: str, start: int, end: int,
              spans: Optional[Set[Tuple[int, int]]] = None) -> str:
    """The property a candidate token is the string-literal value of, or "".

    The token must be a whole string literal of the segment (``spans``, from
    :func:`literal_spans`; computed when not given), written as
    ``key: "<token>"`` (a quoted key too) for a key in :data:`TOKEN_KEYS`, or as
    a Relay module's ``exports = "<token>"``. A token anywhere else (inside
    another literal such as a user's text, a log line, a URL) is no candidate.
    """
    if start < 1 or end >= len(code):
        return ""
    quote = code[start - 1]
    if quote not in "\"'`" or code[end] != quote:
        return ""
    if (start - 1, end) not in (spans if spans is not None else literal_spans(code)):
        return ""
    m = _KEY_BEFORE.search(code[max(0, start - 1 - 80):start - 1])
    if not m:
        return ""
    name, sep = m.group(1), m.group(2)
    if sep == "=":
        before = code[max(0, start - 1 - 80):start - 1].rstrip()
        if name != "exports" or before.endswith(("==", "!=", "<=", ">=")):
            return ""
        return name
    return name if name in TOKEN_KEYS else ""


def _is_document(text: str) -> bool:
    return text.lstrip()[:1] == "<" or bool(
        re.search(r"<(?:html|body|head|script)\b", text[:4096], re.I))


def code_segments(name: str, text: str) -> List[Tuple[int, str]]:
    """The JavaScript in one fetched text, as ``(offset, code)`` pieces.

    An HTML document yields its inline ``<script>`` bodies of a JavaScript
    type (never ``application/json``, ``ld+json`` or other data blocks, and
    never its markup or text); a text that parses as JSON is data and yields
    nothing; anything else is a bundle and is code as a whole.
    """
    stripped = text.lstrip()
    if _is_document(text):
        out = []
        for m in _SCRIPT_BLOCK.finditer(text):
            attrs = m.group(1)
            tm = re.search(r"""\btype\s*=\s*["']?([^"'\s>]+)""", attrs, re.I)
            kind = (tm.group(1).lower() if tm else "")
            if kind in _JS_TYPES and m.group(2).strip():
                out.append((m.start(2), m.group(2)))
        return out
    if stripped[:1] in ("{", "["):
        try:
            import json
            json.loads(text)
            return []
        except ValueError:
            pass
    return [(0, text)]


def _declared_operation(code: str, anchor: Tuple[int, int], window: int) -> str:
    """The operation type the anchor's own group declares, lower-case, or ""."""
    lo, hi = max(0, anchor[0] - window), min(len(code), anchor[1] + window)
    for m in _OP_TYPE.finditer(code, lo, hi):
        if same_group(code, anchor, m.span(1)):
            return m.group(1).lower()
    return ""


def _anchor_re(anchor: str) -> "re.Pattern[str]":
    return re.compile(rf"(?<![{_IDENT}]){re.escape(anchor)}(?![{_IDENT}])")


def _texts(texts: Any) -> List[Tuple[str, str]]:
    if isinstance(texts, Mapping):
        return [(str(k), str(v or "")) for k, v in texts.items()]
    return [(str(name), str(text or "")) for name, text in texts]


def rescan(recorded: str, anchor: str, texts: Any, *,
           template: Any = None, location: Optional[Sequence[str]] = None,
           window: int = ANCHOR_WINDOW) -> RescanResult:
    """Find the token that replaced ``recorded`` next to ``anchor``.

    ``texts`` is ``{name: text}`` or ``[(name, text), ...]``: the trigger
    document and the JS bundles it references, already fetched. When
    ``template`` and ``location`` (a slot path, see :func:`token_at`) are
    given, the template must carry ``recorded`` there, or there is no answer.
    """
    shape = token_shape(recorded)
    if shape is None:
        return RescanResult(None, f"the recorded token {recorded!r} has no token shape "
                                  f"(8+ characters of one charset family)")
    anchor = str(anchor or "")
    if not re.search(rf"[{_IDENT}]", anchor):
        return RescanResult(None, "no anchor: give the operationName or the name next to "
                                  "the token in the bundle")
    if template is not None and location is not None:
        try:
            current = token_at(template, location)
        except ValueError as exc:
            return RescanResult(None, f"the template has no token at {list(location)}: {exc}")
        if current != recorded:
            return RescanResult(None, f"the template carries {current!r} at {list(location)}, "
                                      f"not the recorded token")
    a_re, t_re = _anchor_re(anchor), shape.pattern()
    anchors = 0
    writes: List[str] = []
    sources = _texts(texts)
    # bundles first; a document's inline scripts only when no bundle answers
    by_kind: Dict[bool, List[Tuple[Tuple, str, RescanEvidence]]] = {False: [], True: []}
    for name, text in sources:
        inline = _is_document(text)
        for base, code in code_segments(name, text):
            spans: Optional[Set[Tuple[int, int]]] = None
            for am in a_re.finditer(code):
                anchors += 1
                a0, a1 = am.span()
                op = _declared_operation(code, (a0, a1), window)
                if op in ("mutation", "subscription"):
                    writes.append(f"{name}@{base + a0}: {op}")
                    continue
                # no endpos: the lookahead must see the text after a candidate
                for tm in t_re.finditer(code, max(0, a0 - window - shape.length)):
                    t0, t1 = tm.span()
                    if t0 > a1 + window:
                        break
                    if t1 > a0 and t0 < a1:
                        continue                     # overlaps the anchor itself
                    tok = tm.group(0)
                    if tok == anchor:
                        continue
                    if spans is None:
                        spans = literal_spans(code)
                    if not token_key(code, t0, t1, spans):
                        continue
                    dist = a0 - t1 if t1 <= a0 else t0 - a1
                    if dist > window:
                        continue
                    ev = RescanEvidence(
                        file=name, offset=base + t0, anchor_offset=base + a0, distance=dist,
                        in_code=not _prose(code, t0, t1),
                        anchor_in_code=not _prose(code, a0, a1),
                        same_group=same_group(code, (a0, a1), (t0, t1)))
                    rank = (ev.in_code, ev.anchor_in_code, ev.same_group, -ev.distance)
                    by_kind[inline].append((rank, tok, ev))
    found = by_kind[False] or by_kind[True]
    if not found and writes:
        return RescanResult(None, f"the anchor's operation is declared a write "
                                  f"({'; '.join(writes[:2])}) — a read is never healed "
                                  f"onto it", anchors=anchors)
    if not anchors:
        return RescanResult(None, f"the anchor {anchor!r} does not stand as a name of its "
                                  f"own in any of the {len(sources)} text(s)")
    if not found:
        return RescanResult(None, f"no {shape.family} token of length {shape.length}, "
                                  f"written as a persisted-id property value, within "
                                  f"{window} characters of the anchor", anchors=anchors)
    best = max(rank for rank, _t, _e in found)
    top = [(tok, ev) for rank, tok, ev in found if rank == best]
    tokens = sorted({tok for tok, _ev in top})
    if len(tokens) > 1:
        return RescanResult(None, f"tie: {len(tokens)} different tokens share the best rank "
                                  f"({', '.join(tokens[:3])}) — no answer",
                            considered=len(found), anchors=anchors)
    token, ev = top[0]
    if token == recorded:
        return RescanResult(None, "the bundles still carry the recorded token next to the "
                                  "anchor; rotation is not what failed",
                            evidence=ev, unchanged=True, considered=len(found),
                            anchors=anchors, best=token)
    return RescanResult(token, f"found next to {anchor!r} in {ev.file} at offset {ev.offset}",
                        evidence=ev, considered=len(found), anchors=anchors, best=token)


# ---------------------------------------------------------------------------
# where the token sits in a template
# ---------------------------------------------------------------------------

def _is_slotted(template: Any) -> bool:
    return bool(getattr(template, "slots", None))


def token_at(template: Any, location: Sequence[str]) -> str:
    """The value at ``location`` in a compiled template, as text.

    ``location`` is a slot path (``execution/api_slots.py``): ``["path:2"]``,
    ``["query:doc_id"]``, ``["form:doc_id"]``,
    ``["body", "json:/extensions/persistedQuery/sha256Hash"]``,
    ``["query:extensions", "json:/persistedQuery/sha256Hash"]``. It works on a
    slotted template (verbatim URL and body) and on a schema-2 one (query
    dict, body template).
    """
    from anyuse.execution import api_slots as S
    at = list(location)
    first = S.parse_at(at)[0]
    if first.kind == "query" and not _is_slotted(template) and S.split_url(template.url)[1] is None:
        raw = template.query.get(first.key)
        if raw is None:
            raise ValueError(f"the template's query has no {first.key!r}")
        return _read_text(raw, at[1:])
    leaf = S.read_slot(template.url, template.body, at)
    return S._scalar_text(leaf)


def _read_text(text: str, nested: Sequence[str]) -> str:
    from anyuse.execution import api_slots as S
    if not nested:
        return text
    reader = S._Reader()
    S._fill_text(text, [S.parse_step(s) for s in nested], reader)
    return S._scalar_text(reader.leaf)


def apply_token(template: Any, location: Sequence[str], recorded: str, new: str) -> Any:
    """A copy of ``template`` with the token at ``location`` swapped.

    Only that leaf changes, byte for byte (``api_slots.replace_leaf``); a JSON
    number stays a number. A schema-2 template's const spec for a swapped
    query value is updated with it, so the template stays valid. Raises
    ``ValueError`` when the template does not carry ``recorded`` there or the
    new token does not have the recorded shape.
    """
    from anyuse.execution import api_slots as S
    shape = token_shape(recorded)
    if shape is None or not shape.matches(new):
        raise ValueError(f"{new!r} does not have the recorded token's shape")
    out = copy.deepcopy(template)
    at = list(location)
    first = S.parse_at(at)[0]
    if first.kind == "query" and not _is_slotted(out) and S.split_url(out.url)[1] is None:
        raw = out.query.get(first.key)
        if raw is None:
            raise ValueError(f"the template's query has no {first.key!r}")
        value = S.replace_in_text(raw, at[1:], recorded, new)
        out.query[first.key] = value
        for spec in out.parameter_schema:
            if spec.get("kind") == "const" and f"query:{first.key}" in (spec.get("destinations") or []):
                spec["value"] = value
                spec["max_length"] = max(1, len(value))
        return out
    out.url, out.body = S.replace_leaf(out.url, out.body, at, recorded, new)
    return out


def default_anchor(template: Any, location: Sequence[str]) -> str:
    """A proposed anchor: the request's GraphQL ``operationName`` (body JSON,
    form field or query value), else the path segment right after a token
    that sits in the path (X's ``/graphql/<queryId>/<OperationName>``)."""
    import json
    from anyuse.execution import api_slots as S
    body = getattr(template, "body", None) or ""
    try:
        data = json.loads(body) if body.lstrip()[:1] in ("{", "[") else None
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("operationName"), str):
        return data["operationName"]
    pairs = dict(urllib.parse.parse_qsl(body)) if "=" in body and "{" not in body[:1] else {}
    query = dict(getattr(template, "query", None) or {})
    _head, raw_query, _frag = S.split_url(getattr(template, "url", "") or "")
    if raw_query:
        query.update(urllib.parse.parse_qsl(raw_query))
    for source in (pairs, query):
        if isinstance(source.get("operationName"), str) and source["operationName"]:
            return source["operationName"]
        name = source.get("fb_api_req_friendly_name")
        if isinstance(name, str) and name:
            # Meta's bundles name the module that exports the doc_id
            # "<Name>_facebookRelayOperation"; the bare name never stands alone there
            return name + "_facebookRelayOperation"
    first = S.parse_at(list(location))[0]
    if first.kind == "path":
        head = S.split_url(template.url)[0]
        start, end = S.path_bounds(head)
        segs = head[start:end].split("/")[1:]
        nxt = int(first.key) + 1
        if nxt < len(segs) and re.fullmatch(rf"[{_IDENT}]+", segs[nxt] or ""):
            return segs[nxt]
    return ""


# ---------------------------------------------------------------------------
# script bundles of a document
# ---------------------------------------------------------------------------

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ATTR = r"\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)"
_SCRIPT = re.compile(r"<script\b([^>]*)>", re.I)
_LINK = re.compile(r"<link\b([^>]*)>", re.I)
_BASE = re.compile(r"<base\b([^>]*)>", re.I)


def _attr(attrs: str, name: str) -> Optional[str]:
    m = re.search(rf"(?:^|\s){name}{_ATTR}", attrs, re.I)
    if not m:
        return None
    raw = m.group(1)
    if raw[:1] in ("'", '"'):
        raw = raw[1:-1]
    return html_lib.unescape(raw.strip())


def bundle_urls(document: str, final_url: str) -> List[str]:
    """The script bundles a document loads, absolute, in document order.

    ``<script src>``, ``<link rel=modulepreload>`` and ``<link rel=preload
    as=script>``, resolved against the document's FINAL URL (after redirects),
    or against its ``<base href>`` when it has one, itself resolved against
    the final URL. Only http(s) URLs; duplicates dropped; comments ignored.
    """
    doc = _COMMENT.sub("", document or "")
    base = final_url
    bm = _BASE.search(doc)
    if bm:
        href = _attr(bm.group(1), "href")
        if href:
            base = urllib.parse.urljoin(final_url, href)
    found: List[Tuple[int, str]] = []
    for m in _SCRIPT.finditer(doc):
        src = _attr(m.group(1), "src")
        if src:
            found.append((m.start(), src))
    for m in _LINK.finditer(doc):
        attrs = m.group(1)
        rel = (_attr(attrs, "rel") or "").lower().split()
        kind = (_attr(attrs, "as") or "").lower()
        if "modulepreload" in rel or ("preload" in rel and kind == "script"):
            href = _attr(attrs, "href")
            if href:
                found.append((m.start(), href))
    out: List[str] = []
    for _pos, ref in sorted(found):
        url = urllib.parse.urljoin(base, ref)
        if urllib.parse.urlsplit(url).scheme in ("http", "https") and url not in out:
            out.append(url)
    return out


# ---------------------------------------------------------------------------
# heal loop guard
# ---------------------------------------------------------------------------

@dataclass
class HealGuard:
    """Keeps a failing op from being healed over and over.

    * A heal whose template is byte-identical to the old one (both rendered
      with the same args) is not drift: :meth:`heal_succeeded` returns False,
      records nothing, and the caller reports the original failure as it was.
    * A heal that failed marks the op stale for :data:`STALE_FOR_S`.
    * An op healed less than :data:`HEAL_RECENT_S` ago that fails again is
      marked stale for :data:`STALE_FOR_S`.
    * A stale op is not healed; :meth:`check` says why and until when.
    * A heal that learned nothing (rate limited, challenged, logged out, the
      profile busy) is neither a success nor a failure: call nothing.

    The clock is injected; the state is a plain dict (:meth:`to_dict`), so the
    caller persists it wherever it keeps repair state.
    """
    clock: Callable[[], float] = time.time
    recent_s: float = HEAL_RECENT_S
    stale_s: float = STALE_FOR_S
    ops: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def check(self, op: str) -> Tuple[bool, str]:
        """``(may_heal, reason)``; the reason names a stale op's cause."""
        row = self.ops.get(op) or {}
        until = float(row.get("stale_until") or 0.0)
        now = self.clock()
        if until > now:
            return False, (f"stale for another {int(until - now)} s: "
                           f"{row.get('stale_reason') or 'no reason recorded'}")
        return True, ""

    def stale_reason(self, op: str) -> str:
        ok, why = self.check(op)
        return "" if ok else why

    def _stale(self, op: str, reason: str) -> str:
        row = self.ops.setdefault(op, {})
        row["stale_until"] = self.clock() + self.stale_s
        row["stale_reason"] = reason
        return reason

    def heal_failed(self, op: str, reason: str = "") -> str:
        return self._stale(op, "heal failed" + (f": {reason}" if reason else ""))

    def heal_succeeded(self, op: str, old: Any, new: Any,
                       args: Optional[Mapping[str, Any]] = None) -> bool:
        """Record a validated heal. False (nothing recorded) when the healed
        template sends exactly what the old one sent: that was not drift."""
        if identical(old, new, args or {}):
            return False
        row = self.ops.setdefault(op, {})
        row["healed_at"] = self.clock()
        row.pop("stale_until", None)
        row.pop("stale_reason", None)
        return True

    def failed(self, op: str, reason: str = "") -> str:
        """Note that ``op`` failed; returns the stale reason when this failure
        came too soon after a heal, else ``""``."""
        row = self.ops.get(op) or {}
        healed = row.get("healed_at")
        if healed is None:
            return ""
        age = self.clock() - float(healed)
        if age < self.recent_s:
            return self._stale(op, f"failed again {int(age)} s after a heal"
                                   + (f": {reason}" if reason else ""))
        return ""

    def to_dict(self) -> dict:
        return {"recent_s": self.recent_s, "stale_s": self.stale_s,
                "ops": copy.deepcopy(self.ops)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any],
                  clock: Callable[[], float] = time.time) -> "HealGuard":
        return cls(clock=clock, recent_s=float(data.get("recent_s", HEAL_RECENT_S)),
                   stale_s=float(data.get("stale_s", STALE_FOR_S)),
                   ops=copy.deepcopy(dict(data.get("ops") or {})))


def identical(old: Any, new: Any, args: Mapping[str, Any]) -> bool:
    """Do two templates send the same bytes for the same args?"""
    def wire(t):
        if hasattr(t, "render"):
            try:
                url, headers, body = t.render(dict(args))
                return ((getattr(t, "method", "") or "").upper(), url,
                        sorted(headers.items()), body)
            except (KeyError, ValueError):
                pass
        return t.to_dict() if hasattr(t, "to_dict") else t
    return wire(old) == wire(new)
