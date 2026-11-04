"""Query-value rule (an extension of ``corpus_live``'s).

A provenance-bearing pool from the site's OWN listing page (as the browser
rendered it) across all its content regions, frozen with a seeded draw and
fixed roles BEFORE any search is made. Nothing is invented: every token is
text the site served. Each token passes ``corpus_live``'s token filter (first word of a
text, 3-30 characters, letters, not an interface word, no duplicate
casefold); the added sources also drop English function words and listing
boilerplate (``FUNCTION_WORDS``).

Sources, in pool order (prefer shared content terms for broad searches,
keep exact names too):

  shared    words in the link texts of at least TWO different rows (any
            repeated group outside page furniture), most frequent first, then
            document order: a word several item titles share is a query the
            site's search answers with several results;
  rows      the first word of the first link of each row of the first
            qualifying repeated group outside nav/header/footer (``corpus_live``'s rule);
  tags      the text of links whose href path has a category/tag/facet-like
            segment (``/tag/``, ``/category/``, ``/topic/``, ``/genre/`` ...),
            outside page furniture, in document order;
  headings  the first word of each h1-h4 outside page furniture;
  titles    the first word of the first link of every row of every OTHER
            repeated group outside page furniture, in document order;
  suggest   the site's own autocomplete: strings in the same-host JSON
            responses observed while ONE seed letter was typed into the start
            page's visible search input (``suggest_tokens``). Used only when the
            listing sources stay below ``N_ELIGIBLE`` (it costs a page load
            and the suggestion requests; it sees no search result).

The pool is the first ``POOL`` tokens with the sources INTERLEAVED
round-robin in that order (one new token of each in turn); a token keeps the
first source that gave it. Eligibility needs
``N_ELIGIBLE`` = 4 values (q1 compile / the agent's task, q2 selection, q3 +
q4 validation); a fifth (q5, the MCP call) is drawn when the pool has one and
is not needed for eligibility. Held-out values are never replaced after an
outcome.
"""
from __future__ import annotations

import html.parser
import json
import re
import unicodedata
import urllib.parse
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import corpus_live as CL

N_VALUES = CL.N_VALUES          # drawn when available (q1..q5)
N_ELIGIBLE = 4                  # needed for a proof (q1..q4); q5 is the MCP call
POOL = CL.POOL
STOPWORDS = CL.STOPWORDS
_WORD = re.compile(r"[^\W_][\w.+\-]*", re.UNICODE)
_TAGGY = {"tag", "tags", "category", "categories", "cat", "topic", "topics", "genre",
          "genres", "subject", "subjects", "collection", "collections", "keyword",
          "keywords", "label", "labels", "theme", "themes"}
FURNITURE = ("nav", "header", "footer", "aside")


#: English function words and listing boilerplate (dates, counts, ratings):
#: never a query from the added sources (the ``rows`` source keeps ``corpus_live``'s filter)
FUNCTION_WORDS = frozenset("""
the and for with from that this these those your you are was were has have had
how what why who when where which into onto over under our its not but can will
new one two per via than then also just only more most any each every other
ago day days hour hours minute minutes min mins week weeks month months year years
updated update rating ratings review reviews view views like likes read reads
votes vote comments comment downloads download free
explore welcome latest discover featured trending recent popular related check
join follow subscribe share save link links edit want manage see get find total
""".split())


def ok_token(tok: str, strict: bool = False) -> bool:
    low = tok.casefold()
    return (3 <= len(tok) <= 30 and bool(CL._OK.match(tok)) and any(c.isalpha() for c in tok)
            and low not in STOPWORDS and not (strict and low in FUNCTION_WORDS))


def _norm(tok: str) -> str:
    return CL._EDGE.sub("", unicodedata.normalize("NFC", tok or ""))


class _Links(html.parser.HTMLParser):
    """Anchors with href, text and whether they sit in page furniture."""

    VOID = {"input", "br", "img", "meta", "link", "hr", "source", "area", "base", "col",
            "embed", "param", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.links: List[Dict[str, Any]] = []
        self._cur: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a":
            self._cur = {"href": a.get("href", ""), "text": [],
                         "furniture": any(t in FURNITURE for t in self.stack)}
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "a" and self._cur is not None:
            self._cur["text"] = " ".join(" ".join(self._cur["text"]).split())
            self.links.append(self._cur)
            self._cur = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._cur is not None and data.strip():
            self._cur["text"].append(data.strip())


def _groups(listing_dom: str):
    from anyuse.execution.html_extract import parse, repeated_groups
    root = parse(listing_dom or "")

    def first_link(node):
        for d in node.walk():
            if d.tag == "a" and d.text():
                return d
        return None

    def furniture(node):
        cur = node.parent
        while cur is not None:
            if cur.tag in FURNITURE:
                return True
            cur = cur.parent
        return False
    out = []
    for rows in repeated_groups(root, CL.MIN_ROWS):
        if furniture(rows[0]):
            continue
        links = [first_link(r) for r in rows]
        if sum(1 for a in links if a is not None) < 0.8 * len(rows):
            continue
        out.append([a.text() for a in links if a is not None])
    return out


def _add(out: List[Tuple[str, str]], seen: set, tok: str, source: str) -> None:
    tok = _norm(tok)
    if tok and ok_token(tok, strict=source != "rows") and tok.casefold() not in seen:
        seen.add(tok.casefold())
        out.append((tok, source))


class _Headings(html.parser.HTMLParser):
    """h1-h4 texts outside page furniture."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.out: List[str] = []
        self._cur: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4") and not any(t in FURNITURE for t in self.stack):
            self._cur = []
        if tag not in _Links.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4") and self._cur is not None:
            self.out.append(" ".join(" ".join(self._cur).split()))
            self._cur = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._cur is not None and data.strip():
            self._cur.append(data.strip())


def listing_tokens(listing_dom: str, limit: int = POOL) -> List[Tuple[str, str]]:
    """``[(token, source), ...]`` from the listing alone, at most ``limit``: the
    sources shared, rows, tags, headings, titles are INTERLEAVED round-robin in
    that order (one token of each in turn), so a full pool holds broad shared
    terms and exact names alike; a token keeps the first source that gave it."""
    lists: Dict[str, List[str]] = {k: [] for k in ("shared", "rows", "tags", "headings",
                                                    "titles")}
    groups = _groups(listing_dom)
    # shared: words in the link texts of >= 2 different rows
    count: Counter = Counter()
    first_at: Dict[str, int] = {}
    shape: Dict[str, str] = {}
    pos = 0
    for texts in groups:
        for t in texts:
            words = {}
            for w in _WORD.findall(t):
                w = _norm(w)
                if w and ok_token(w, strict=True):
                    words.setdefault(w.casefold(), w)
            for low, w in words.items():
                count[low] += 1
                first_at.setdefault(low, pos)
                shape.setdefault(low, w)
            pos += 1
    lists["shared"] = [shape[low] for low in sorted(
        (k for k, c in count.items() if c >= 2), key=lambda k: (-count[k], first_at[k]))]
    lists["rows"] = list(CL.qualifying_tokens(listing_dom, limit=limit))
    # tags / facets: links whose href path has a category/tag-like segment
    p = _Links()
    try:
        p.feed(listing_dom or "")
    except Exception:
        pass
    for a in p.links:
        if a["furniture"] or not a["text"]:
            continue
        segs = [s.lower() for s in urllib.parse.urlsplit(a["href"]).path.split("/") if s]
        if any(s in _TAGGY for s in segs):
            lists["tags"].append(CL.token_of(a["text"]))
    h = _Headings()
    try:
        h.feed(listing_dom or "")
    except Exception:
        pass
    lists["headings"] = [CL.token_of(t) for t in h.out]
    # titles: first words of every group's links
    lists["titles"] = [CL.token_of(t) for texts in groups for t in texts]
    out: List[Tuple[str, str]] = []
    seen: set = set()
    idx = {k: 0 for k in lists}
    while len(out) < limit and any(idx[k] < len(lists[k]) for k in lists):
        for k in lists:
            # the next token of this source that is new (skip taken ones)
            while idx[k] < len(lists[k]):
                tok = lists[k][idx[k]]
                idx[k] += 1
                n = len(out)
                _add(out, seen, tok, k)
                if len(out) > n:
                    break
            if len(out) >= limit:
                break
    return out[:limit]


def suggest_tokens(responses: Iterable[Tuple[str, str]], seed: str,
                   limit: int = POOL) -> List[str]:
    """Tokens from the site's own suggestion responses: ``responses`` are
    ``(url, body)`` of same-host JSON answers observed while ``seed`` (one
    letter) was typed. Every string value that starts with the seed letter
    (case-insensitive) contributes its first word, in response order."""
    out: List[str] = []
    seen: set = set()
    s = (seed or "").casefold()[:1]

    def walk(x):
        if isinstance(x, str):
            yield x
        elif isinstance(x, dict):
            for v in x.values():
                yield from walk(v)
        elif isinstance(x, list):
            for v in x:
                yield from walk(v)
    for _url, body in responses:
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            continue
        for text in walk(data):
            text = " ".join(str(text).split())
            if not text or not text.casefold().startswith(s) or len(text) > 120:
                continue
            tok = _norm(CL.token_of(text))
            if tok and ok_token(tok, strict=True) and tok.casefold() not in seen:
                seen.add(tok.casefold())
                out.append(tok)
                if len(out) >= limit:
                    return out
    return out


def seed_letter(tokens: Sequence[str]) -> str:
    for t in tokens:
        for ch in t:
            if ch.isalpha():
                return ch.lower()
    return "a"


def pool(listing_dom: str, suggestions: Sequence[str] = ()) -> List[Tuple[str, str]]:
    out = listing_tokens(listing_dom)
    seen = {t.casefold() for t, _s in out}
    for tok in suggestions:
        if len(out) >= POOL:
            break
        _add(out, seen, tok, "suggest")
    return out[:POOL]
