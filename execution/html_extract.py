"""Deterministic extraction from server-rendered HTML — the second compiled rung.

Three workflows that would not compile failed for the same
reason: the result list is rendered on the server and there is no JSON call to
find. That is not a niche case, it is most of the older web. This module gives
the compiler a second target: **one GET, then a deterministic path into the HTML**.

Built on the stdlib ``html.parser`` — no new dependency, and nothing here
executes scripts, loads subresources or renders anything. It reads markup as
text and walks a tree.

The path language is a deliberately small CSS subset::

    "div.results li.row a.title"

a descendant chain of ``tag`` or ``tag.class`` steps. Matching returns **every**
node satisfying the chain, in document order, and extraction takes their text —
so one path expresses "the title of each result row" without any index, which is
what makes it survive a list that grows or shrinks.

Derivation (:func:`derive_selector`) goes the other way: given the values that
were on screen, find the nodes holding them and compute the longest common
tag/class chain that selects those nodes and few others. No model, no heuristics
beyond "the markup around the values the user actually saw".

**Brittleness, stated plainly.** An HTML rung is bound to the markup, not to a
published interface. A class rename or a re-nest breaks it. That is handled, not
prevented: the extraction returns nothing or the wrong count, the postcondition
fails, the rung falls back to the recorded UI path, and the demotion/recompile
path in ``api_recompile`` re-derives the selector from the stored recording —
which only persists again if the fresh-input proof passes. Completeness and
identity checks reduce this risk, but selectors remain a semantic limitation.
"""
from __future__ import annotations

import html.parser
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Tags whose text is never content a user reads.
_SKIP_TEXT = {"script", "style", "noscript", "template", "head", "title"}
#: Tags that never wrap a result row; ignored when deriving a chain.
_STRUCTURAL = {"html", "body"}
#: The attributes a :class:`Node` keeps (every other one is dropped while parsing).
_KEEP_ATTRS = ("href", "itemprop", "itemscope", "itemtype", "aria-label", "content", "property",
               "type", "rel")
_WS = re.compile(r"\s+")
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_HTML_NODES = 100_000
MAX_HTML_DEPTH = 128
#: ``RequestTemplate.html_parser`` of a template whose selector was derived on
#: the tree the implied end tags build (``implied_end_tags=True``)
HTML5_PARSER = "html5"


class HtmlLimitError(ValueError):
    pass


@dataclass
class Node:
    """One element. Deliberately minimal: tag, classes, id, text, children, a link's href and a few
    attributes that name what an element holds."""
    tag: str = ""
    classes: Tuple[str, ...] = ()
    node_id: str = ""
    parent: Optional["Node"] = None
    children: List["Node"] = field(default_factory=list)
    _text: List[str] = field(default_factory=list)
    #: for each ``_text`` chunk, how many children preceded it: text and child
    #: elements are read back in DOCUMENT order. Before, a node's own text came
    #: first and its children after, so "<h3>R/a.R:</h3>pkg: title" read
    #: "pkg: title R/a.R:" and no longer matched the same row served as
    #: "<h3>R/a.R:</h3><p>pkg: title</p>" (a documentation index's rows).
    _text_at: List[int] = field(default_factory=list)
    #: an ``a`` element's ``href``, as written ("" for every other element)
    href: str = ""
    #: the element's attributes of :data:`_KEEP_ATTRS` (lower-case names), as written:
    #: what names a value (itemprop, aria-label, a JSON-LD script's type) and where a
    #: link points
    attrs: Dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        """Visible text of this node and its descendants, in document order,
        whitespace-collapsed."""
        if self.tag in _SKIP_TEXT:
            return ""
        at = self._text_at if len(self._text_at) == len(self._text) else [0] * len(self._text)
        parts: List[str] = []
        ti = 0
        for ci, child in enumerate(self.children):
            while ti < len(self._text) and at[ti] <= ci:
                parts.append(self._text[ti])
                ti += 1
            t = child.text()
            if t:
                parts.append(t)
        parts.extend(self._text[ti:])
        return _WS.sub(" ", " ".join(p for p in parts if p)).strip()

    def chain(self) -> List[Tuple[str, Tuple[str, ...]]]:
        """(tag, classes) from the root down to this node."""
        out: List[Tuple[str, Tuple[str, ...]]] = []
        cur: Optional[Node] = self
        while cur is not None and cur.tag:
            if cur.tag not in _STRUCTURAL:
                out.append((cur.tag, cur.classes))
            cur = cur.parent
        return list(reversed(out))

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


#: An inline style that takes an element out of what a person sees.
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", re.I)


def hidden_by_markup(attrs: Dict[str, str]) -> bool:
    """Does the MARKUP itself hide this element?

    Only what the serialised DOM says: the ``hidden`` attribute, an inline
    ``display: none`` / ``visibility: hidden``, or ``aria-hidden="true"``. A
    client-side filter that hides the rows it rejects (instead of removing them)
    leaves exactly these marks in ``outerHTML``. Class-based hiding depends on a
    stylesheet and is deliberately not guessed at.
    """
    if "hidden" in attrs:
        return True
    if str(attrs.get("aria-hidden", "")).strip().lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(str(attrs.get("style", ""))))


# ---------------------------------------------------------------------------
# implied end tags (WHATWG HTML, tree construction, "in body" and the table modes)
# ---------------------------------------------------------------------------

#: the "special" elements: an li, dd or dt search stops at one of them, except
#: at address, div and p
_SPECIAL = frozenset({
    "address", "applet", "area", "article", "aside", "base", "basefont", "bgsound",
    "blockquote", "body", "br", "button", "caption", "center", "col", "colgroup", "dd",
    "details", "dir", "div", "dl", "dt", "embed", "fieldset", "figcaption", "figure",
    "footer", "form", "frame", "frameset", "h1", "h2", "h3", "h4", "h5", "h6", "head",
    "header", "hgroup", "hr", "html", "iframe", "img", "input", "keygen", "li", "link",
    "listing", "main", "marquee", "menu", "meta", "nav", "noembed", "noframes",
    "noscript", "object", "ol", "p", "param", "plaintext", "pre", "script", "search",
    "section", "select", "source", "style", "summary", "table", "tbody", "td",
    "template", "textarea", "tfoot", "th", "thead", "title", "tr", "track", "ul", "wbr",
    "xmp"})
_LIST_ITEM_STOP = _SPECIAL - {"address", "div", "p"}
#: start tags that close an open p. A table does so only in no-quirks mode,
#: which this parser does not track, so it is left out.
_CLOSES_P = frozenset({
    "address", "article", "aside", "blockquote", "center", "details", "dialog", "dir",
    "div", "dl", "fieldset", "figcaption", "figure", "footer", "form", "header",
    "hgroup", "main", "menu", "nav", "ol", "p", "pre", "listing", "search", "section",
    "summary", "ul", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "xmp", "plaintext",
    "li", "dd", "dt"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
#: element scope; button scope adds button. The MathML and SVG names are the
#: ones html.parser reports (lower-cased).
_SCOPE = frozenset({"applet", "caption", "html", "table", "td", "th", "marquee",
                    "object", "template", "mi", "mo", "mn", "ms", "mtext",
                    "annotation-xml", "foreignobject", "desc"})
_BUTTON_SCOPE = _SCOPE | {"button"}
_TABLE_SCOPE = frozenset({"html", "table", "template"})
#: what "generate implied end tags" pops off the current node
_IMPLIED = frozenset({"dd", "dt", "li", "optgroup", "option", "p", "rb", "rp", "rt",
                      "rtc"})
#: start tags the head may hold; any other start tag, or text, closes it
_HEAD_CONTENT = frozenset({"base", "basefont", "bgsound", "link", "meta", "noframes",
                           "script", "style", "template", "title", "noscript"})
#: start tags that close an open table section (and the row and cell inside it)
_TABLE_SECTION_START = frozenset({"caption", "col", "colgroup", "tbody", "tfoot", "thead"})
_TABLE_SECTION = frozenset({"caption", "tbody", "tfoot", "thead", "tr", "td", "th"})
#: the SVG and MathML roots, and the elements inside them where HTML content
#: resumes (the HTML and MathML text integration points; html.parser reports
#: tag names lower-cased)
_FOREIGN_ROOTS = frozenset({"svg", "math"})
_FOREIGN_EXITS = frozenset({"foreignobject", "desc", "title", "mi", "mo", "mn", "ms",
                            "mtext"})


class _TreeBuilder(html.parser.HTMLParser):
    """html.parser → Node tree. Tolerant: unclosed tags do not derail it.

    ``implied_end_tags`` is for a raw server response body: a start tag first
    closes the elements the HTML standard closes for it (an open li before a
    new li, a p before a block, a cell before a new cell), and a self-closing
    tag inside svg or math (``<path/>``) closes at once. ``implied`` counts
    those closes per closed tag. Left out: the adoption agency for misnested
    formatting tags, reopening the formatting elements (``a``, ``b``, ``em``
    ...) that an implied close pops, foster parenting, an implicit tbody, the
    empty p a stray ``</p>`` inserts, and HTML start tags that end svg or
    math content. When no rule fires, the tree is the one the plain end-tag
    rule builds.

    Off (the default), only an end tag closes an element. That is the right
    reading of a DOM snapshot: it is the browser's tree already, serialised
    with every end tag, and a script can build trees the parser never would,
    such as a p that holds a div. The rules would split that p.

    ``visible_only`` drops every subtree the markup itself hides
    (:func:`hidden_by_markup`): the subtree is built but never attached."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self, visible_only: bool = False, implied_end_tags: bool = False):
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="")
        self._stack: List[Node] = [self.root]
        self._nodes = 0
        self.visible_only = visible_only
        self.implied_end_tags = implied_end_tags
        self.implied: Dict[str, int] = {}

    def _close_from(self, k: int) -> None:
        """Close the open element at stack index ``k`` and everything above it."""
        tag = self._stack[k].tag
        self.implied[tag] = self.implied.get(tag, 0) + 1
        del self._stack[k:]

    def _find(self, targets, stop) -> int:
        """Stack index of the nearest open element in ``targets``, searching
        down from the current node and giving up at an element in ``stop``;
        0 when there is none (index 0 is the root, which never closes)."""
        for i in range(len(self._stack) - 1, 0, -1):
            tag = self._stack[i].tag
            if tag in targets:
                return i
            if tag in stop:
                return 0
        return 0

    def _close_p(self) -> None:
        k = self._find(("p",), _BUTTON_SCOPE)
        if k:
            self._close_from(k)

    def _pop_implied(self, keep=()) -> None:
        while len(self._stack) > 1 and self._stack[-1].tag in _IMPLIED \
                and self._stack[-1].tag not in keep:
            self._close_from(len(self._stack) - 1)

    def _in_foreign(self) -> bool:
        """Is the current node svg or math content, not HTML inside it?"""
        for i in range(len(self._stack) - 1, 0, -1):
            tag = self._stack[i].tag
            if tag in _FOREIGN_ROOTS:
                return True
            if tag in _FOREIGN_EXITS:
                return False
        return False

    def _implied_closes(self, tag: str) -> None:
        cur = self._stack[-1].tag
        if (cur == "head" and tag not in _HEAD_CONTENT) or \
                (cur == "colgroup" and tag not in ("col", "template")):
            self._close_from(len(self._stack) - 1)
        if tag in ("li", "dd", "dt"):
            k = self._find(("li",) if tag == "li" else ("dd", "dt"), _LIST_ITEM_STOP)
            if k:
                self._close_from(k)
            self._close_p()
        elif tag in _CLOSES_P:
            self._close_p()
            if tag in _HEADING_TAGS and self._stack[-1].tag in _HEADING_TAGS:
                self._close_from(len(self._stack) - 1)
        elif tag == "option":
            if self._stack[-1].tag == "option":
                self._close_from(len(self._stack) - 1)
        elif tag == "optgroup":
            for closes in ("option", "optgroup"):
                if self._stack[-1].tag == closes:
                    self._close_from(len(self._stack) - 1)
        elif tag in ("td", "th"):
            k = self._find(("td", "th", "caption"), _TABLE_SCOPE | {"tr"})
            if k:
                self._close_from(k)
        elif tag == "tr":
            k = self._find(("tr", "caption"), _TABLE_SCOPE | {"thead", "tbody", "tfoot"})
            if k:
                self._close_from(k)
        elif tag in _TABLE_SECTION_START:
            # the lowest open section, row or cell of the current table
            k = 0
            for i in range(len(self._stack) - 1, 0, -1):
                t = self._stack[i].tag
                if t in _TABLE_SCOPE:
                    break
                if t in _TABLE_SECTION:
                    k = i
            if k:
                self._close_from(k)
        elif tag in ("rb", "rtc", "rp", "rt"):
            if self._find(("ruby",), _SCOPE):
                self._pop_implied(keep=("rtc",) if tag in ("rp", "rt") else ())

    def handle_starttag(self, tag, attrs):
        self._nodes += 1
        if self._nodes > MAX_HTML_NODES:
            raise HtmlLimitError(f"HTML node count exceeds cap {MAX_HTML_NODES}")
        tag = tag.lower()
        if self.implied_end_tags:
            # before the depth check: a long list of unclosed rows is flat
            self._implied_closes(tag)
        if len(self._stack) > MAX_HTML_DEPTH:
            raise HtmlLimitError(f"HTML nesting exceeds depth cap {MAX_HTML_DEPTH}")
        attrd = {k.lower(): (v or "") for k, v in attrs}
        node = Node(tag=tag,
                    classes=tuple(sorted(attrd.get("class", "").split())),
                    node_id=attrd.get("id", ""),
                    parent=self._stack[-1],
                    href=attrd.get("href", "").strip() if tag == "a" else "",
                    attrs={k: v for k, v in attrd.items() if k in _KEEP_ATTRS})
        if not (self.visible_only and hidden_by_markup(attrd)):
            self._stack[-1].children.append(node)
        if tag not in self.VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        # svg and math honour the self-closing slash; HTML ignores it on a
        # non-void element (``<div/>`` stays open)
        foreign = self.implied_end_tags and (tag in _FOREIGN_ROOTS or self._in_foreign())
        self.handle_starttag(tag, attrs)
        if foreign and tag not in self.VOID:
            self._close_from(len(self._stack) - 1)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if data and data.strip():
            if self.implied_end_tags and self._stack[-1].tag == "head":
                # text in the head ends it, as a body start tag would
                self._close_from(len(self._stack) - 1)
            node = self._stack[-1]
            node._text.append(data.strip())
            node._text_at.append(len(node.children))


#: pseudo-element placeholders a DOM snapshot writes (``<::after></::after>``,
#: ``<::marker>``). The HTML parser read them as TEXT ("< ::after>") and, worse,
#: the bogus end tag swallowed following content, so a captured result row no
#: longer matched the same row in the server's HTML (tier 1 found "no specific
#: selector" while the list sat in the document).
_PSEUDO_TAG = re.compile(r"</?::[A-Za-z-]+\s*/?>")


def parse(markup: str, visible_only: bool = False, implied_end_tags: bool = False) -> Node:
    """Parse markup into a :class:`Node` tree. Never raises on bad HTML.

    ``visible_only`` leaves out the subtrees the markup hides (see
    :func:`hidden_by_markup`); the default keeps every node, as before.
    ``implied_end_tags``: build the tree a browser builds from a raw server
    response, closing the elements whose end tag the markup left out (see
    :class:`_TreeBuilder`). Leave it off for a DOM snapshot."""
    if len((markup or "").encode("utf-8", "ignore")) > MAX_HTML_BYTES:
        raise HtmlLimitError(f"HTML input exceeds {MAX_HTML_BYTES}-byte cap")
    builder = _TreeBuilder(visible_only=visible_only, implied_end_tags=implied_end_tags)
    try:
        builder.feed(_PSEUDO_TAG.sub("", markup or ""))
        builder.close()
    except HtmlLimitError:
        raise
    except Exception:                      # a malformed document is still usable
        pass
    return builder.root


# ---------------------------------------------------------------------------
# the path language
# ---------------------------------------------------------------------------

def parse_selector(selector: str) -> List[Tuple[str, Tuple[str, ...]]]:
    """``"div.results li.row a"`` → ``[("div",("results",)), ("li",("row",)), ("a",())]``."""
    steps: List[Tuple[str, Tuple[str, ...]]] = []
    for part in (selector or "").split():
        bits = part.split(".")
        tag = bits[0] or "*"
        # ``tag#id`` (the result-region selector); the id rides in the
        # class tuple as "#id"
        ident = ""
        if "#" in tag:
            tag, ident = tag.split("#", 1)
            tag = tag or "*"
        cls = sorted(b for b in bits[1:] if b)
        steps.append((tag.lower(), tuple(cls + (["#" + ident] if ident else []))))
    return steps


def _step_matches(node: Node, step: Tuple[str, Tuple[str, ...]]) -> bool:
    tag, classes = step
    if tag != "*" and node.tag != tag:
        return False
    return all((node.node_id == c[1:]) if c.startswith("#") else (c in node.classes)
               for c in classes)


def _select_child_aware(root: Node, selector: str) -> List[Node]:
    """``a b > c``: ``>`` makes the next step a DIRECT child of the previous
    one (the result-region records: heterogeneous children of one
    region). Matched right to left, as :func:`select`."""
    toks = selector.split()
    steps: List[Tuple[Tuple[str, Tuple[str, ...]], bool]] = []
    child = False
    for t in toks:
        if t == ">":
            child = True
            continue
        steps.extend((st, child) for st in parse_selector(t))
        child = False
    if not steps:
        return []

    def match_up(node: Node, i: int) -> bool:
        # steps[i] matched ``node``; match steps[:i] among its ancestors
        if i == 0:
            return True
        prev, is_child = steps[i - 1][0], steps[i][1]
        cur = node.parent
        if is_child:
            return cur is not None and bool(cur.tag) and _step_matches(cur, prev) \
                and match_up(cur, i - 1)
        while cur is not None and cur.tag:
            if _step_matches(cur, prev) and match_up(cur, i - 1):
                return True
            cur = cur.parent
        return False
    last = len(steps) - 1
    return [n for n in root.walk() if n.tag and _step_matches(n, steps[last][0])
            and match_up(n, last)]


def select(root: Node, selector: str) -> List[Node]:
    """Every node matching the descendant chain, in document order."""
    if ">" in (selector or "").split():
        return _select_child_aware(root, selector)
    steps = parse_selector(selector)
    if not steps:
        return []
    out: List[Node] = []
    for node in root.walk():
        if not node.tag or not _step_matches(node, steps[-1]):
            continue
        # walk up, consuming the earlier steps as ancestors (not necessarily direct)
        needed = list(steps[:-1])
        cur = node.parent
        while needed and cur is not None:
            if _step_matches(cur, needed[-1]):
                needed.pop()
            cur = cur.parent
        if not needed:
            out.append(node)
    return out


def extract(markup: str, selector: str, limit: int = 50,
            visible_only: bool = False, labels: bool = False,
            implied_end_tags: bool = False) -> List[str]:
    """Text of every node the selector matches (``visible_only``: of the nodes
    the markup does not hide; ``labels``: each node's :func:`row_label` instead
    of its whole text; ``implied_end_tags``: as :func:`parse`)."""
    root = parse(markup, visible_only=visible_only, implied_end_tags=implied_end_tags)
    read = row_label if labels else Node.text
    return [t for t in (read(n) for n in select(root, selector)[:limit]) if t]


_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def _names_something(text: str) -> bool:
    return len(text) >= 2 and any(ch.isalpha() for ch in text)


def row_label(node: Node) -> str:
    """One result row's own name: its first heading, else its first link that
    names something, else the whole row text.

    A result row carries more than its name (a version, a description, download
    counts, "Homepage Documentation"), and the whole row collapsed into one line
    matches no single field of the request that filled it. The row's heading or
    main link is what the page shows as the result (a package registry's ``a.name``)."""
    for wanted in (_HEADINGS, frozenset({"a"})):
        for n in node.walk():
            if n is not node and n.tag in wanted:
                text = n.text()
                if _names_something(text):
                    return text
    return node.text()


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------

def _chain_selector(chain: Sequence[Tuple[str, Tuple[str, ...]]]) -> str:
    return " ".join(tag + "".join("." + c for c in classes) for tag, classes in chain)


def _common_suffix(chains: List[List[Tuple[str, Tuple[str, ...]]]], depth: int
                   ) -> Optional[List[Tuple[str, Tuple[str, ...]]]]:
    """The shared last ``depth`` steps, keeping only classes ALL chains share."""
    if not chains or any(len(c) < depth for c in chains):
        return None
    out: List[Tuple[str, Tuple[str, ...]]] = []
    for i in range(-depth, 0):
        tags = {c[i][0] for c in chains}
        if len(tags) != 1:
            return None
        shared = set(chains[0][i][1])
        for c in chains[1:]:
            shared &= set(c[i][1])
        out.append((tags.pop(), tuple(sorted(shared))))
    return out


# ---------------------------------------------------------------------------
# repetition — find the repeated row, then the field inside it
# ---------------------------------------------------------------------------
#
# Lever (a). The chain derivation above starts from the values and
# walks *up*. That fails in two ways the evidence showed repeatedly:
#
#   * the shared suffix collapses to a bare ``a`` (code hosts, package registries) because the
#     row markup carries no class the matched nodes agree on — and a bare ``a``
#     is then rejected as unspecific, so nothing compiles although the list is
#     right there in the document;
#   * the on-screen values are two different fields of one row (a package list:
#     name, version, name, version …), so no single chain covers more than half.
#
# Repetition derivation goes the other way round: find the container whose
# children repeat — that is what a result list *is* — and only then look for the
# field inside a row. It yields ``<row selector> <field chain>``, which is
# specific by construction and survives a list growing or shrinking.

#: A repeated group must have at least this many members to be a result list.
MIN_ROWS = 3
#: A field chain must be present in at least this share of the rows.
MIN_FIELD_PRESENCE = 0.8
#: Rows shorter than this (normalised text) are separators, not content.
MIN_ROW_TEXT = 2


def _signature(node: "Node") -> Tuple[str, Tuple[str, ...]]:
    return (node.tag, node.classes)


def repeated_groups(root: Node, min_rows: int = MIN_ROWS) -> List[List[Node]]:
    """Sibling groups that repeat: the candidate result lists, largest first.

    A group is the set of children of one parent that share a ``(tag, classes)``
    signature. Groups are ranked by how much text they carry in total and then
    by member count — a navigation bar repeats too, but its items are one word
    each, while result rows carry sentences.
    """
    groups: List[List[Node]] = []
    for node in root.walk():
        if not node.children:
            continue
        by_sig: Dict[Tuple[str, Tuple[str, ...]], List[Node]] = {}
        for child in node.children:
            if not child.tag or child.tag in _SKIP_TEXT:
                continue
            by_sig.setdefault(_signature(child), []).append(child)
        for sig, members in by_sig.items():
            if len(members) < min_rows:
                continue
            kept = [m for m in members if len(m.text()) >= MIN_ROW_TEXT]
            if len(kept) < min_rows:
                continue
            groups.append(kept)
    groups.sort(key=lambda g: (-sum(len(m.text()) for m in g), -len(g)))
    return groups


def _row_selector(rows: List[Node], max_depth: int = 3) -> str:
    """A selector for the rows themselves: their signature plus enough ancestry.

    The deepest ancestry that still selects all the rows wins, because a bare
    ``li`` matches every list on the page and a two-step ``ul.list-packages li``
    matches exactly the result list.
    """
    chains = [r.chain() for r in rows]
    best = ""
    for depth in range(min(max_depth, min(len(c) for c in chains)), 0, -1):
        suffix = _common_suffix(chains, depth)
        if suffix is None:
            continue
        best = _chain_selector(suffix)
        break
    return best


def _relative_chains(row: Node, max_depth: int = 3) -> List[str]:
    """Candidate field selectors inside one row, relative to it.

    For every descendant carrying text, the last one..``max_depth`` steps of its
    chain, with and without classes. Suffixes rather than full chains because
    ``select`` matches descendants at any depth, and a full chain is useless on a
    page that nests a title five divs deep (package registries do exactly that,
    which is why the first cut of this function found no field at all). The class-free
    variant is what survives a row whose wrapper classes differ per row — a
    highlight on the first hit, a badge on the last.
    """
    out: List[str] = []
    base = len(row.chain())
    for node in row.walk():
        if node is row or not node.tag or node.tag in _SKIP_TEXT:
            continue
        if not node.text():
            continue
        chain = node.chain()[base:]
        if not chain:
            continue
        for depth in range(1, min(max_depth, len(chain)) + 1):
            suffix = chain[-depth:]
            out.append(_chain_selector(suffix))
            bare = [(tag, ()) for tag, _cls in suffix]
            out.append(_chain_selector(bare))
    return out


def derive_selector_repeated(markup: str, values: Sequence[str], matcher=None,
                             min_rows: int = MIN_ROWS, max_groups: int = 12,
                             max_matches: int = 60, implied_end_tags: bool = False
                             ) -> Tuple[str, float, List[str]]:
    """Derive ``<row> <field>`` from the repeated structure of the page.

    Scored exactly like :func:`derive_selector`: the share of ``values`` the
    selector accounts for. A tie is broken towards the selector whose extracted
    count is closest to the row count, because a field that appears once per row
    is a column of the result table and a field that appears eleven times in
    three rows is markup noise.
    """
    if matcher is None:
        from anyuse.execution.api_compile import normalise, values_match

        def matcher(a, b):                                   # noqa: E306
            return values_match(normalise(a), normalise(b))

    wanted = [v for v in values if v]
    if not wanted:
        return "", 0.0, []
    root = parse(markup, implied_end_tags=implied_end_tags)

    best_sel, best_cov, best_vals, best_gap = "", 0.0, [], 10 ** 6
    for rows in repeated_groups(root, min_rows)[:max_groups]:
        rowsel = _row_selector(rows)
        if not rowsel:
            continue
        # the row itself is a candidate "field" (a one-column list)
        candidates = {""}
        for chain in _relative_chains(rows[0]):
            candidates.add(chain)
        present: Dict[str, int] = {c: 0 for c in candidates}
        for row in rows:
            got = set(_relative_chains(row)) | {""}
            for c in candidates:
                if c in got:
                    present[c] += 1
        for chain in sorted(candidates):
            if present[chain] < MIN_FIELD_PRESENCE * len(rows):
                continue
            selector = (rowsel + " " + chain).strip()
            got = extract(markup, selector, limit=max_matches + 1,
                          implied_end_tags=implied_end_tags)
            if not got or len(got) > max_matches:
                # A field selector that matches more nodes than a result list
                # ever has is picking up everything inside the row, not a
                # column of it. Rejected here rather than later, so the search
                # can still find the tighter chain in the same group.
                continue
            covered = sum(1 for v in wanted if any(matcher(v, g) for g in got))
            cov = covered / len(wanted)
            if cov <= 0.0:
                continue           # never emit a selector that explains nothing
            gap = abs(len(got) - len(rows))
            if cov > best_cov + 1e-9 or (abs(cov - best_cov) <= 1e-9 and gap < best_gap):
                best_sel, best_cov, best_vals, best_gap = selector, cov, got, gap
    return best_sel, best_cov, best_vals


def derive_list_items(markups: Sequence[str], min_rows: int = MIN_ROWS,
                      limit: int = 30, values: Optional[Sequence[str]] = None,
                      implied_end_tags: bool = False) -> Tuple[str, List[str]]:
    """Ground truth WITHOUT a hand-written selector — lever (f).

    Given the documents two *different* queries produced, return the repeated
    group that (a) exists in both and (b) whose content actually changed between
    them. Navigation, facet sidebars and footers repeat just as neatly as result
    rows; what separates a result list from page furniture is that it answers
    the question, so it differs when the question differs.

    Returns ``(row_selector, items_of_the_first_markup)``; ``("", [])`` when no
    repeated group changed, which is itself a finding — the page did not respond
    to the query and there is no ground truth to prove anything against.

    ``values`` (optional, one per markup): the query each page answered.
    Among the groups that changed, one whose rows NAME their page's value ranks
    first, as in :func:`derive_typed_items`: a facet box changes with the query
    too, but its rows name formats and tags, not the query (an open-data portal:
    a format-facet list beat the dataset list on churn).
    """
    if not markups:
        return "", []
    first = parse(markups[0], implied_end_tags=implied_end_tags)
    others = [parse(m, implied_end_tags=implied_end_tags) for m in markups[1:]]
    if not others:
        return "", []

    best: Tuple[float, int, str, List[str]] = (-1.0, 0, "", [])
    changed: List[Tuple[float, int, str, List[str], List[Node]]] = []
    for rows in repeated_groups(first, min_rows)[:20]:
        sel = _row_selector(rows)
        if not sel:
            continue
        mine = [t for t in (r.text() for r in rows) if t]
        if not mine:
            continue
        mine_set = {_WS.sub(" ", t).strip().lower() for t in mine}
        # how much of this group's content is NOT in the other query's version
        churn = 1.0
        seen_everywhere = True
        for other in others:
            theirs = [n.text() for n in select(other, sel)]
            if not theirs:
                seen_everywhere = False
                break
            theirs_set = {_WS.sub(" ", t).strip().lower() for t in theirs if t}
            overlap = len(mine_set & theirs_set) / max(1, len(mine_set))
            churn = min(churn, 1.0 - overlap)
        if not seen_everywhere:
            continue
        text_mass = sum(len(t) for t in mine)
        score = churn
        if values and churn > 0.0:
            score += _named_share(values, [mine] + [[n.text() for n in select(o, sel)]
                                                    for o in others])
        changed.append((score, text_mass, sel, mine[:limit], rows, churn))
        if score > best[0] + 1e-9 or (abs(score - best[0]) <= 1e-9 and text_mass > best[1]):
            best = (score, text_mass, sel, mine[:limit])
    if best[0] <= 0.0:
        return "", []
    if values:
        # two FIELDS of one list (dt/dd pairs under one parent: the quote and
        # its author) both change; naming the query must not move the choice
        # from one field of the same rows to another: there the old rule
        # (churn, then text) decides (a quotation list: the author field
        # named the query and replaced the quote rows)
        plain = max(changed, key=lambda c: (c[5], c[1]))
        top = next(c for c in changed if c[2] == best[2])
        if plain is not top and plain[5] > 0.0 and plain[4][0].parent is top[4][0].parent:
            best = (plain[0], plain[1], plain[2], plain[3])
    # a changed group in page furniture (a facet list in a <nav>, a "recent" box in an
    # <aside>) is not the result list when a changed group outside it exists; inside
    # <main>, when one exists there, wins (a format-facet list in a <nav> was picked over
    # the dataset list). The landmarks and class names are the last tie-break: a group
    # whose rows name the input on every page is dropped by them only for another such
    # group outside the furniture (a "related searches" box beside the result list)
    total = _page_text_len(first)
    live = [c for c in changed if c[0] > 0.0]
    # a group whose rows name their page's value on every page answers the input
    # (``score - churn`` is its named share)
    info = {id(c): (c, in_chrome(c[4][0], total), in_main(c[4][0]),
                    bool(values) and c[0] - c[5] >= 1.0 - 1e-9) for c in live}
    pool = _furniture_filter(live, lambda c: info[id(c)])
    if pool is not live and not any(c[2] == best[2] for c in pool):
        top = max(pool, key=lambda c: (c[0], c[1]))
        best = (top[0], top[1], top[2], top[3])
    changed = pool
    # A container of lists (a "shows / people / networks" results page: a few
    # section blocks that each hold a list) changes as much as the list inside
    # it and carries more text, so it wins the tie above. Its rows are whole
    # sections, not results. Descend: a group that changed as much, has MORE
    # rows, and lies entirely inside ONE row of the winner is the result list
    # (a sectioned results page: 4 section blocks were proved against,
    # coverage 0.25). Repeated while it applies; the largest such group by
    # text wins.
    cur = next(c for c in changed if c[2] == best[2])
    while True:
        inner = [c for c in changed
                 if c is not cur and c[0] >= cur[0] - 1e-9 and len(c[4]) > len(cur[4])
                 and _inside_one(c[4], cur[4])]
        if not inner:
            break
        cur = max(inner, key=lambda c: c[1])
    return cur[2], cur[3]


def _inside_one(rows: Sequence[Node], outer_rows: Sequence[Node]) -> bool:
    """True iff every node of ``rows`` descends from the same node of ``outer_rows``."""
    outer = {id(o) for o in outer_rows}
    home = None
    for r in rows:
        cur, hit = r.parent, None
        while cur is not None:
            if id(cur) in outer:
                hit = id(cur)
                break
            cur = cur.parent
        if hit is None or (home is not None and hit != home):
            return False
        home = hit
    return True


#: page furniture: landmark tags and whole id/class tokens (a snapshot keeps tags,
#: ids and classes only, no ``role``). They are the last tie-break between
#: candidates the comparison across the pages left: a region or group whose rows
#: name each page's value is dropped for sitting in one only when another such
#: candidate sits outside.
_CHROME_TAGS = {"nav", "header", "footer", "aside"}
_CHROME_TOKENS = {"sidebar", "side-bar", "navbar", "nav", "navigation", "breadcrumb",
                  "breadcrumbs", "site-header", "site-footer", "masthead", "footer"}
_MAIN_TOKENS = {"main", "main-content", "maincontent", "content-main"}


def _is_chrome_node(node: "Node") -> bool:
    if node.tag in _CHROME_TAGS:
        return True
    toks = {t.lower() for t in node.classes} | ({node.node_id.lower()} if node.node_id else set())
    return bool(toks & _CHROME_TOKENS)


def in_chrome(node: "Node", page_text_len: int = 0) -> bool:
    """Is ``node`` inside page furniture (nav, header, footer, aside, a sidebar)?

    A furniture ancestor that holds most of the page's text is not furniture: a
    page that forgot to close its ``<header>`` nests everything inside it."""
    cur = node.parent
    while cur is not None:
        if cur.tag and _is_chrome_node(cur):
            if not page_text_len or len(cur.text()) < 0.5 * page_text_len:
                return True
        cur = cur.parent
    return False


def in_main(node: "Node") -> bool:
    """Is ``node`` inside the page's main landmark (``<main>``, id/class main)?"""
    cur = node.parent
    while cur is not None:
        if cur.tag == "main":
            return True
        toks = {t.lower() for t in cur.classes} | ({cur.node_id.lower()} if cur.node_id else set())
        if toks & _MAIN_TOKENS:
            return True
        cur = cur.parent
    return False


def _page_text_len(root: "Node") -> int:
    return len(root.text())


def _furniture_filter(cands: list, rows_of) -> list:
    """Drop candidates whose rows sit in page furniture, and prefer the main
    landmark: when some candidate lies inside it, only those are kept. Falls
    back to the unfiltered list when the filter would leave nothing.
    ``rows_of(c)`` is ``(c, in furniture, in main[, bound])``: a candidate the
    comparison across the pages found input-bound (its rows name each page's
    value). The landmarks are the tie-break among the input-bound candidates
    (a "related searches" box beside the result list names the input too);
    when they would drop every input-bound candidate, those stay, and the
    candidates' scores choose among what is kept."""
    if not cands:
        return cands

    def bound(c) -> bool:
        f = rows_of(c)
        return len(f) > 3 and bool(f[3])
    kept = [c for c in cands if not rows_of(c)[1]]
    kept = kept or cands
    main = [c for c in kept if rows_of(c)[2]]
    kept = main or kept
    if any(bound(c) for c in kept) or not any(bound(c) for c in cands):
        return kept
    ids = {id(c) for c in kept}
    return [c for c in cands if id(c) in ids or bound(c)]


def _names(value: str, texts: Sequence[str]) -> bool:
    """Do ``texts`` (one page's rows) contain every word of ``value``?"""
    from anyuse.execution.api_compile import contains_words, is_word, normalise
    words = [w for w in normalise(value).split() if is_word(w)]
    if not words or not texts:
        return False
    blob = normalise(" ".join(texts))
    return all(contains_words(blob, w) for w in words)


def _named_share(values: Sequence[str], per_page: Sequence[Sequence[str]]) -> float:
    """The share of pages whose rows name that page's value."""
    pairs = list(zip(values, per_page))
    if not pairs:
        return 0.0
    return sum(1.0 for v, p in pairs if _names(str(v or ""), p)) / len(pairs)


def derive_typed_items(pages: Sequence[Tuple[str, str]], before: str = "",
                       min_rows: int = MIN_ROWS, limit: int = 30,
                       implied_end_tags: bool = False) -> Tuple[str, List[str], str]:
    """Ground truth for a search reached by TYPING: where did the answer appear?

    ``pages`` is ``[(typed value, DOM after typing + Enter), ...]`` for two or
    more different values; ``before`` is the page's DOM just before the value
    was typed (optional). Differences from :func:`derive_list_items`, each for
    one way a typed search failed to yield ground truth:

    * the pages are read **visible-only**: a client-side filter that hides the
      rows it rejects (``hidden``, inline ``display:none``) leaves every row in
      the markup, so the full parse sees an unchanged list;
    * candidate groups come from EVERY page (and from ``before``), not only
      the first: a result list that is short for one value (fewer than
      ``min_rows`` rows) still yields its selector from another page;
    * among groups that change between the values, one that also changed from
      ``before`` (the page reacted to the typing) and whose rows name the typed
      value ranks first. Page furniture (a static "featured" list) repeats and
      may even change per load, but it does not name the query.

    Returns ``(row_selector, items_of_the_first_page, how)``; ``("", [], why)``
    when no group qualifies — the typed page did not answer the value in a way
    that can be checked, and nothing is claimed.
    """
    pages = [(str(v or ""), str(d or "")) for v, d in pages if d]
    if len(pages) < 2:
        return "", [], "fewer than two typed pages to compare"
    roots = [parse(d, visible_only=True, implied_end_tags=implied_end_tags)
             for _v, d in pages]
    broot = parse(before, visible_only=True, implied_end_tags=implied_end_tags) \
        if before else None

    sels: List[str] = []
    flags: Dict[str, Tuple[bool, bool]] = {}
    # the before page contributes candidates too: a filter can leave fewer than
    # ``min_rows`` visible rows on every typed page
    for root in roots + ([broot] if broot is not None else []):
        total = _page_text_len(root)
        for rows in repeated_groups(root, min_rows)[:20]:
            sel = _row_selector(rows)
            if sel and sel not in sels:
                sels.append(sel)
                flags[sel] = (in_chrome(rows[0], total), in_main(rows[0]))

    def texts(root: Node, sel: str) -> List[str]:
        return [t for t in (n.text() for n in select(root, sel)) if t]

    def key(ts: Sequence[str]) -> set:
        return {_WS.sub(" ", t).strip().lower() for t in ts}

    scored: List[Tuple[float, int, str, List[str], str]] = []
    bound: Dict[str, bool] = {}
    for sel in sels:
        per = [texts(r, sel) for r in roots]
        first = per[0]
        if not first or sum(1 for p in per if p) < 2:
            continue
        # every other page must differ from the first somewhere in this group
        churn = 1.0
        for other in per[1:]:
            mine, theirs = key(first), key(other)
            overlap = len(mine & theirs) / max(1, len(mine))
            churn = min(churn, 1.0 - overlap)
        if churn <= 0.0:
            continue
        reacted = 0.0
        if broot is not None:
            was = key(texts(broot, sel))
            reacted = sum(1.0 for p in per if key(p) != was) / len(per)
        named = _named_share([v for v, _d in pages], per)
        bound[sel] = named >= 1.0 - 1e-9
        score = churn + 0.5 * reacted + 1.0 * named
        mass = sum(len(t) for t in first)
        how = (f"typed-page region: churn {churn:.2f} between the values, "
               f"{reacted:.2f} of the pages changed from before typing, "
               f"{named:.2f} name their value")
        scored.append((score, mass, sel, first[:limit], how))
    # among the regions that changed, page furniture never outranks one
    # outside it, and the main landmark wins when it holds one (the same rule
    # as derive_list_items); a region whose rows name every page's value is dropped
    # by that only for another such region outside the furniture
    scored = _furniture_filter(scored, lambda c: (c, *flags.get(c[2], (False, False)),
                                                  bound.get(c[2], False)))
    best: Tuple[float, int, str, List[str], str] = (-1.0, 0, "", [], "")
    for cand in scored:
        if cand[0] > best[0] + 1e-9 or (abs(cand[0] - best[0]) <= 1e-9 and cand[1] > best[1]):
            best = cand
    if best[0] <= 0.0:
        return "", [], ("no visible repeated group changed between the typed values")
    return best[2], best[3], best[4]


# ---------------------------------------------------------------------------
# the result-region model
# ---------------------------------------------------------------------------
#
# The repeated-group rule needs >= 3 siblings with the SAME (tag, classes).
# Heterogeneous cards, a short answer and sectioned results break it although
# the page answered. The region model instead compares the rendered pages of
# two fitting values: the query-responsive region is an element present (by
# its path) on both pages whose text differs between them and that names each
# page's value outside a heading echo; its records are its children (after
# unwrapping single wrappers), whatever their tags. Region, segmentation and
# selector are fixed from the fitting pages only; validation never sees them.

#: a record must carry at least this much text
REGION_MIN_RECORD_TEXT = 3
#: at most this many records in a region (a region is a result list, not a page)
REGION_MAX_RECORDS = 200


def _path_key(node: Node) -> Tuple[Tuple[str, str, Tuple[str, ...], int], ...]:
    """The element path: (tag, id, classes, position among same-signature
    siblings) from the root; the same container on two pages has one key."""
    out = []
    cur: Optional[Node] = node
    while cur is not None and cur.tag:
        nth = 0
        if cur.parent is not None:
            for sib in cur.parent.children:
                if sib is cur:
                    break
                if sib.tag == cur.tag and sib.classes == cur.classes:
                    nth += 1
        out.append((cur.tag, cur.node_id, cur.classes, nth))
        cur = cur.parent
    return tuple(reversed(out))


def _unwrap(node: Node) -> Node:
    """Descend through wrappers: a single element child carrying (nearly) all
    of the text."""
    cur = node
    while True:
        kids = [c for c in cur.children if c.tag and c.tag not in _SKIP_TEXT and c.text()]
        if len(kids) != 1:
            return cur
        if len(kids[0].text()) < 0.9 * max(1, len(cur.text())):
            return cur
        cur = kids[0]


#: a region holding more than this share of the page's text is the page, not
#: its answer
REGION_MAX_SHARE = 0.6


def _records(region: Node, value: str = "") -> List[Node]:
    """The region's children that carry text. A child the page marks as furniture (a
    landmark tag, a class or id name) is left out, unless ``value`` is given and it names
    it (a region looked for inside furniture, whose records the page may mark so too)."""
    return [c for c in region.children if c.tag and c.tag not in _SKIP_TEXT
            and len(c.text()) >= REGION_MIN_RECORD_TEXT
            and (not _is_chrome_node(c) or (bool(value) and _names(value, [c.text()])))]


def _echo(node: Node, value: str) -> bool:
    """A heading (or a record that is ONLY a heading) echoing the query."""
    if node.tag in ("h1", "h2", "h3", "h4", "h5", "h6", "title", "label", "legend"):
        return True
    kids = [c for c in node.walk() if c is not node and c.tag in ("h1", "h2", "h3")]
    return bool(kids) and len(" ".join(k.text() for k in kids)) >= 0.8 * len(node.text())


_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6", "title", "label", "legend", "button"}


def _in_heading(node: Node) -> bool:
    cur: Optional[Node] = node
    while cur is not None and cur.tag:
        if cur.tag in _HEADINGS:
            return True
        cur = cur.parent
    return False


def _region_selector(pairs: Sequence[Tuple[Node, Node]], max_depth: int = 4) -> str:
    """The shortest chain suffix (classes, then ids) of the FIRST pair's region
    that selects exactly the region on EVERY ``(region, root)`` pair."""
    node = pairs[0][0]
    chain = []
    cur: Optional[Node] = node
    while cur is not None and cur.tag:
        if cur.tag not in _STRUCTURAL:
            chain.append(cur)
        cur = cur.parent
    chain.reverse()

    def step(n: Node, with_id: bool) -> str:
        ident = f"#{n.node_id}" if with_id and n.node_id and re.match(r"^[A-Za-z][\w-]*$",
                                                                        n.node_id) else ""
        return n.tag + ident + "".join("." + c for c in n.classes)

    def unique(sel: str) -> bool:
        for reg, root in pairs:
            hits = select(root, sel)
            if len(hits) != 1 or hits[0] is not reg:
                return False
        return True
    for with_id in (False, True):
        for depth in range(1, min(max_depth, len(chain)) + 1):
            sel = " ".join(step(n, with_id) for n in chain[-depth:])
            if unique(sel):
                return sel
    # an id-bearing ancestor further up anchors the chain
    for i in range(len(chain) - 1, -1, -1):
        if chain[i].node_id and re.match(r"^[A-Za-z][\w-]*$", chain[i].node_id):
            tail = chain[max(i + 1, len(chain) - 2):]
            sel = " ".join([step(chain[i], True)] + [step(n, False) for n in tail])
            if unique(sel):
                return sel
            break
    return ""


def _naming_blocks(root: Node, value: str, total: int, furniture: bool = True) -> List[Node]:
    """The smallest elements whose text names ``value``, outside headings and
    form controls, and with ``furniture``, outside page furniture."""
    out = []
    for n in root.walk():
        if not n.tag or n.tag in _SKIP_TEXT or n.tag in _STRUCTURAL:
            continue
        if not _names(value, [n.text()]):
            continue
        if any(_names(value, [c.text()]) for c in n.children if c.tag):
            continue                      # a child names it: not the smallest
        if _in_heading(n) or (furniture and (in_chrome(n, total) or _is_chrome_node(n))):
            continue
        out.append(n)
    return out


def _echoes(pages: Sequence[Tuple[str, str]], blocks: Sequence[Sequence[Node]]) -> List[set]:
    """Per page, the naming blocks that only echo the value: the same element path names
    its page's value on every page, with the same text once each page's value and its
    numbers are masked (a breadcrumb, a "12 results for x" line, a search box's label).
    Whatever the page calls them, they hold no answer."""
    from anyuse.execution.empty_page import mask
    maps = [{_path_key(b): mask(b.text(), v) for b in bs} for (v, _d), bs in zip(pages, blocks)]
    out: List[set] = []
    for i, ((v, _d), bs) in enumerate(zip(pages, blocks)):
        echo = set()
        for b in bs:
            key, text = _path_key(b), mask(b.text(), v)
            if all(maps[j].get(key) == text for j in range(len(pages)) if j != i):
                echo.add(id(b))
        out.append(echo)
    return out


def _ancestors(node: Node) -> List[Node]:
    out, cur = [], node
    while cur is not None and cur.tag:
        out.append(cur)
        cur = cur.parent
    return out


def _lca(nodes: Sequence[Node]) -> Optional[Node]:
    if not nodes:
        return None
    common = _ancestors(nodes[0])
    for n in nodes[1:]:
        anc = {id(a) for a in _ancestors(n)}
        common = [a for a in common if id(a) in anc]
    return common[0] if common else None


def _densest(blocks: Sequence[Node], total: int, furniture: bool = True) -> Optional[Node]:
    """The deepest container holding (nearly, >= 80 %) the most naming blocks,
    below the page-share limit and outside headings (with ``furniture``, outside
    page furniture too).
    A stray mention elsewhere (a breadcrumb, a "results for" line) does not
    stretch the region over the page, as a plain common ancestor would."""
    count: Dict[int, int] = {}
    node_of: Dict[int, Node] = {}
    depth: Dict[int, int] = {}
    for b in blocks:
        for i, a in enumerate(_ancestors(b)):
            if a.tag in _STRUCTURAL or len(a.text()) > REGION_MAX_SHARE * max(1, total):
                break
            count[id(a)] = count.get(id(a), 0) + 1
            node_of[id(a)] = a
            depth[id(a)] = len(_ancestors(a))
    ok = [k for k in count if not _in_heading(node_of[k]) and not (furniture and (
        _is_chrome_node(node_of[k]) or in_chrome(node_of[k], total)))]
    if not ok:
        return None
    # the DEEPEST container holding at least 80 % of the most any container
    # holds: one stray mention (a "N results for X" line) beside a list does
    # not lift the region above the list
    top = max(count[k] for k in ok)
    best = max((k for k in ok if count[k] >= 0.8 * top), key=lambda k: (depth[k], count[k]))
    return node_of[best]


def derive_result_region(pages: Sequence[Tuple[str, str]], before: str = "",
                         limit: int = 30, implied_end_tags: bool = False
                         ) -> Tuple[str, List[str], str]:
    """Ground truth from the query-responsive REGION of two or more fitting
    pages ``[(value, dom), ...]`` (read visible-only). Returns ``(selector,
    records_of_the_first_page, how)``; the selector is ``<region> > *``;
    ``("", [], why)`` when no region qualifies.

    On every page the region is the container holding the most of the
    smallest elements that name that page's value (headings, form controls,
    and the blocks that only echo the value on every page excluded
    (:func:`_echoes`); ties to the deeper container), raised to the level
    where it has at least two records (children) when the answer is a single
    block. It qualifies when the SAME element path is the region on every page
    (a card grid on one page and a sidebar on another is no region), it holds at
    most ``REGION_MAX_SHARE`` of the page text, its text differs between every
    pair of pages (and from ``before`` when given), and one selector (tag.class
    chain) picks exactly it on every page.

    Page furniture (the landmark tags and the class and id names of
    :data:`_CHROME_TOKENS`) is the last tie-break: the region is looked for
    outside it first. When no region qualifies there, one inside it counts when
    at least half of its records name their page's value on every page: an
    input-bound region is never refused for what the page calls it."""
    pages = [(str(v or ""), str(d or "")) for v, d in pages if d]
    if len(pages) < 2:
        return "", [], "fewer than two pages to compare"
    roots = [parse(d, visible_only=True, implied_end_tags=implied_end_tags)
             for _v, d in pages]
    first = _region(pages, roots, before, limit, implied_end_tags, furniture=True)
    if first[0]:
        return first
    second = _region(pages, roots, before, limit, implied_end_tags, furniture=False)
    if second[0]:
        return second[0], second[1], second[2] + " (inside what the page marks as furniture)"
    return first


def _region(pages: Sequence[Tuple[str, str]], roots: Sequence[Node], before: str, limit: int,
            implied_end_tags: bool, furniture: bool) -> Tuple[str, List[str], str]:
    """:func:`derive_result_region` with page furniture excluded or not."""
    where = "outside headings and furniture" if furniture else "outside headings"
    found = []
    for (value, _d), root in zip(pages, roots):
        total = len(root.text())
        blocks = _naming_blocks(root, value, total, furniture)
        if not blocks:
            return "", [], f"no block names the value {value!r} {where}"
        found.append(blocks)
    echoes = _echoes(pages, found)
    # the comparison across the pages first: a block every page repeats (its value masked)
    # is no answer. When that leaves a page with none (rows all built the same way from the
    # value), the comparison says nothing here and every page keeps its blocks
    kept = [[b for b in bs if id(b) not in echo] for bs, echo in zip(found, echoes)]
    if all(kept):
        found = kept
    regions: List[Node] = []
    all_blocks: List[List[Node]] = []
    for (value, _d), root, blocks in zip(pages, roots, found):
        total = len(root.text())
        reg = _densest(blocks, total, furniture)
        # outside furniture a child the page marks as furniture (a breadcrumb in the region)
        # is no record; inside it, one that names the value is
        own = "" if furniture else value
        # a single naming block: its record is one child of the result region
        while reg is not None and reg.parent is not None and len(_records(_unwrap(reg), own)) < 2:
            if len(reg.parent.text()) > REGION_MAX_SHARE * max(1, total) or \
                    not reg.parent.tag or reg.parent.tag in _STRUCTURAL:
                break
            reg = reg.parent
        if reg is None or not reg.tag or reg.tag in _STRUCTURAL:
            return "", [], "the naming blocks share no region below the page"
        regions.append(reg)
        all_blocks.append(blocks)
    # the page with the most naming blocks fixes the region's element path; a
    # page with a shorter answer must hold ALL its naming blocks inside the
    # element at that same path (a single answer is one record of the region)
    lead = max(range(len(pages)), key=lambda i: len(all_blocks[i]))
    key = _path_key(regions[lead])
    for i, root in enumerate(roots):
        if i == lead:
            continue
        same = [n for n in root.walk() if n.tag and _path_key(n) == key]
        if not same:
            return "", [], "the region is not the same element on every page"
        inside = {id(n) for n in same[0].walk()}
        if not all(id(b) in inside for b in all_blocks[i]):
            return "", [], "the region is not the same element on every page"
        regions[i] = same[0]
    for reg, root in zip(regions, roots):
        total = len(root.text())
        if _in_heading(reg) or (furniture and (in_chrome(reg, total) or _is_chrome_node(reg))):
            return "", [], "the naming blocks sit in page furniture"
        if len(reg.text()) > REGION_MAX_SHARE * max(1, total):
            return "", [], "the naming blocks span the whole page, not a region"
    texts = [r.text() for r in regions]
    if len(set(texts)) != len(texts):
        return "", [], "the region did not change between the values"
    sel = _region_selector([(regions[lead], roots[lead])] +
                           [(r, root) for i, (r, root) in enumerate(zip(regions, roots))
                            if i != lead])
    if not sel:
        return "", [], "the region has no selector that picks it on every page"
    if before:
        broot = parse(before, visible_only=True, implied_end_tags=implied_end_tags)
        if any(b.text() in texts for b in select(broot, sel)):
            return "", [], "a page still shows the pre-search region"
    recs = [_records(r, "" if furniture else v) for r, (v, _d) in zip(regions, pages)]
    if any(not r or len(r) > REGION_MAX_RECORDS for r in recs):
        return "", [], "the region has no records (or too many)"
    if not furniture and any(2 * sum(1 for c in r if _names(v, [c.text()])) < len(r)
                             for r, (v, _d) in zip(recs, pages)):
        return "", [], "the region inside page furniture does not name the value in most records"
    items = [r.text() for r in recs[0]][:limit]
    how = (f"result region {sel!r}: the lowest common container of the blocks naming "
           f"each page's value, changed between the values, "
           f"{min(len(r) for r in recs)}+ record(s) per page")
    return sel + " > *", items, how


def derive_selector(markup: str, values: Sequence[str], matcher=None,
                    max_depth: int = 4, implied_end_tags: bool = False
                    ) -> Tuple[str, float, List[str]]:
    """Find a selector that yields the on-screen ``values``.

    Returns ``(selector, coverage, extracted)``. ``coverage`` is the share of
    ``values`` the selector accounts for; ``""`` when nothing works.

    The method: locate the deepest node whose own text equals each value, take
    those nodes' tag/class chains, and try the longest shared suffix first —
    a deep, class-rich chain is specific, and we only shorten it when the deep
    one does not actually select the nodes. Then score the candidate the same
    way the JSON extractor scores a response, so an HTML rung and a JSON rung
    are held to one standard.
    """
    if matcher is None:
        from anyuse.execution.api_compile import normalise, values_match
        def matcher(a, b):                                   # noqa: E306
            return values_match(normalise(a), normalise(b))

    root = parse(markup, implied_end_tags=implied_end_tags)
    wanted = [v for v in values if v]
    if not wanted:
        return "", 0.0, []

    nodes: List[Node] = []
    for value in wanted:
        best: Optional[Node] = None
        for node in root.walk():
            if not node.tag or node.tag in _SKIP_TEXT:
                continue
            t = node.text()
            if not t or not matcher(value, t):
                continue
            # deepest match wins: the innermost node holding exactly this value
            if best is None or len(node.chain()) > len(best.chain()):
                best = node
        if best is not None:
            nodes.append(best)
    if not nodes:
        return "", 0.0, []

    # Consensus, not unanimity. A search page usually echoes the query in its
    # heading, so the FIRST value often matches a heading node that shares no
    # structure with the result rows; requiring every matched node to agree then
    # yields nothing at all. Group the matched nodes by their leaf (tag, classes)
    # and derive from the largest group, which is the repeated row.
    groups: Dict[Tuple[str, Tuple[str, ...]], List[Node]] = {}
    for n in nodes:
        groups.setdefault((n.tag, n.classes), []).append(n)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), -len(g[0].chain())))

    best_sel, best_cov, best_vals = "", 0.0, []
    for group in ordered[:3]:
        chains = [n.chain() for n in group]
        sel, cov, vals = _best_for_chains(chains, markup, wanted, matcher, max_depth,
                                          implied_end_tags)
        if cov > best_cov + 1e-9:
            best_sel, best_cov, best_vals = sel, cov, vals
    return best_sel, best_cov, best_vals


def _best_for_chains(chains, markup, wanted, matcher, max_depth, implied_end_tags=False):
    best_sel, best_cov, best_vals = "", 0.0, []
    for depth in range(min(max_depth, min(len(c) for c in chains)), 0, -1):
        suffix = _common_suffix(chains, depth)
        if suffix is None:
            continue
        selector = _chain_selector(suffix)
        got = extract(markup, selector, implied_end_tags=implied_end_tags)
        if not got:
            continue
        covered = sum(1 for v in wanted if any(matcher(v, g) for g in got))
        cov = covered / len(wanted)
        # Prefer coverage; on a tie prefer the tighter (deeper) selector, which
        # is the one less likely to also pick up navigation and footers.
        if cov > best_cov + 1e-9:
            best_sel, best_cov, best_vals = selector, cov, got
    return best_sel, best_cov, best_vals


def selector_is_specific(markup: str, selector: str, max_matches: int = 60,
                         implied_end_tags: bool = False) -> bool:
    """Reject a selector that matches most of the page (e.g. a bare ``a``)."""
    root = parse(markup, implied_end_tags=implied_end_tags)
    return 0 < len(select(root, selector)) <= max_matches


def selector_is_one_list(markup: str, selector: str, max_rows: int = 1000,
                         implied_end_tags: bool = False) -> bool:
    """A long result list is still ONE list: accept a selector whose matches are
    all children of one parent, when the selector is qualified (a class on its
    last step, or at least two steps). A bare ``a`` or ``li`` spread over the page
    fails the one-parent test. A 60-match cap would refuse every result page
    longer than 60 rows (a distribution's package search, a repository list, a
    ports category); the fresh-input proof that follows still applies."""
    steps = parse_selector(selector)
    if not steps:
        return False
    if len(steps) < 2 and not steps[-1][1]:
        return False
    nodes = select(parse(markup, implied_end_tags=implied_end_tags), selector)
    if not (0 < len(nodes) <= max_rows):
        return False
    return len({id(n.parent) for n in nodes}) == 1
