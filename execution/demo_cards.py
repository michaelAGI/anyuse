"""The RESULT CONTAINERS of a compiled demonstration tool. PURE
(the runtime read is one local JS evaluation, ``CARDS_JS``; nothing is sent).

The problem: a site-search tool that keeps "the first k links of the result URL
pattern" lists the per-platform links of ONE game ("Disponibile per: PC, XONE, PS4"), which share
that pattern, as separate results. The fix is ONE result per result container:

* **compile time** (the demonstration's own result page, its recorded DOM): each demonstrated
  result's TITLE link (a link of the reported URL whose text names the reported title) and its
  CARD, the nearest REPEATED ancestor of that title link (its parent holds >= 2 children with the
  same descriptor) that holds one result while a sibling card holds another one at the same place:
  another demonstrated result (``separates``) or, when none does, another link of the result
  region in >= 2 sibling cards (``siblings``). A card is kept as the descriptor chain of the card
  and its 3 ancestors (``tag#id.class.class``, ids and classes with a digit dropped - the
  descriptor of ``demo_tool.LINK_SIG_JS``), with the title link's path inside the card. With no
  repeated ancestor (a result page showing one result) the title link itself is the card
  (``kind: title``, its own chain).
* **run time**: every link of the page is placed in the lowest ancestor matching a compiled card
  chain (``CARDS_JS``); the links of one card are ONE result: its primary title link (the compiled
  title path; else a link in a heading; else the URL most of the card's links point to; else the
  first), titled by that URL's texts in the card. A link outside every compiled card is not a
  result; the sub-links of a result (per-platform pages, variants, categories, authors) are never
  listed as results.

Paths inside a card carry a POSITION when a node has same-descriptor siblings (``div@0`` is the
first of several plain ``div``): a title in the first plain ``div`` of a card and the platform
links in its third are told apart.

``test_demo_cards.py`` drives all of it with fakes (one game's per-platform links in one card).
"""
from __future__ import annotations

import contextlib
import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: the elements the page reader numbers (``web_arch_run.CONTROL_SEL``, in the same order)
VOID = frozenset("area base br col embed hr img input link meta param source track wbr keygen".split())
#: their content is not part of the document tree (a template's content, noscript with scripts on)
INERT = frozenset(("template", "noscript"))
RAW = frozenset(("script", "style"))
#: never a result card
NEVER_CARD = frozenset(("html", "body", "main", "header", "nav", "footer", "aside"))
#: how far above a title link a card may be
MAX_UP = 14
#: path separator (never in a descriptor)
SEP = "\u0001"
#: the areas of the page chrome (READ_JS's ``areaOf``)
CHROME_AREAS = ("header", "nav", "footer")


# ---------------------------------------------------------------------------
# the document tree of a recorded DOM (outerHTML: every non-void element is closed)
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "svg", "inert", "_d")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["Node"], svg: bool = False,
                 inert: bool = False):
        self.tag, self.attrs, self.parent, self.svg, self.inert = tag, attrs, parent, svg, inert
        self.children: List[Any] = []
        self._d: Optional[str] = None

    def elements(self) -> List["Node"]:
        return [c for c in self.children if isinstance(c, Node) and not c.inert]


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.stack: List[Node] = [self.root]
        self.base = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        parent = self.stack[-1]
        a: Dict[str, str] = {}
        for k, v in attrs:
            a.setdefault(str(k).lower(), "" if v is None else str(v))
        svg = tag == "svg" or (parent.svg and parent.tag != "foreignobject")
        n = Node(tag, a, parent, svg, parent.inert or parent.tag in INERT)
        parent.children.append(n)
        if tag == "base" and not self.base and a.get("href") and not n.inert:
            self.base = a["href"]
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID and self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        for k in range(len(self.stack) - 1, 0, -1):
            if self.stack[k].tag == tag:
                del self.stack[k:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(html: str) -> Tuple[Node, str]:
    """(root, the document's <base href> or "")."""
    t = _Tree()
    try:
        t.feed(html or "")
        t.close()
    except Exception:                                  # noqa: BLE001 - a partial tree is still a tree
        pass
    return t.root, t.base


def walk(n: Node) -> Iterable[Node]:
    """The elements under ``n`` in document order (not ``n``; never inert content)."""
    stack = list(reversed(n.elements()))
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.elements()))


def text_of(n: Node, limit: int = 2000) -> str:
    out: List[str] = []
    size = 0
    stack: List[Any] = [n]
    while stack and size < limit:
        x = stack.pop()
        if isinstance(x, str):
            out.append(x)
            size += len(x)
            continue
        if x.inert or x.tag in RAW:
            continue
        stack.extend(reversed(x.children))
    return " ".join(" ".join(out).split())[:limit]


def label_of(n: Node) -> str:
    """READ_JS's label of a link: aria-label, its text, its title, an image's alt."""
    t = n.attrs.get("aria-label") or text_of(n, 400) or n.attrs.get("title") or ""
    if not t:
        for x in walk(n):
            if x.tag == "img" and x.attrs.get("alt"):
                t = x.attrs["alt"]
                break
    return " ".join(str(t).split())[:160]


_DIGIT = re.compile(r"\d")


#: web tool version 11 (``bem_free``): the card descriptors leave out a BEM modifier class
_BEM_FREE = False


@contextlib.contextmanager
def bem_free(on: bool = True):
    """Web tool version 11 (the result cards ``tf-card type-page`` and
    ``tf-card tf-card--img-sm type-page`` are one card kind; the modifier took the descriptor's second class slot and
    the result read stopped "a result card of a layout the demonstration did not show"): inside, ``desc`` leaves out a
    class ``block--modifier`` whose ``block`` is a class of the same element (``CARDS_JS_V11`` reads the same in the
    page). A parsed tree is described in one mode only (``Node._d`` caches the descriptor)."""
    global _BEM_FREE
    old = _BEM_FREE
    _BEM_FREE = bool(on)
    try:
        yield
    finally:
        _BEM_FREE = old


def bem_classes(classes: Sequence[str]) -> List[str]:
    """Version 11: the classes without a BEM modifier of another of them (``x--y`` next to ``x``)."""
    return [c for c in classes if not (c.find("--") > 0 and c[:c.find("--")] in classes)]


def desc(n: Optional[Node]) -> str:
    """``tag#id.class.class`` (ids and classes with a digit dropped, the first 2 classes; an SVG
    element's classes are not read) - ``demo_tool.LINK_SIG_JS``'s descriptor. Inside ``bem_free`` (version 11) a BEM
    modifier class of another of the element's classes is left out before the first 2 are taken."""
    if n is None or n.tag.startswith("#"):
        return ""
    if n._d is not None:
        return n._d
    i = n.attrs.get("id") or ""
    idp = "#" + i if i and not _DIGIT.search(i) else ""
    cls = [] if n.svg else [c for c in (n.attrs.get("class") or "").split() if c and not _DIGIT.search(c)]
    cls = (bem_classes(cls) if _BEM_FREE else cls)[:2]
    n._d = n.tag + idp + ("." + ".".join(cls) if cls else "")
    return n._d


def same_desc_siblings(n: Node) -> List[Node]:
    p = n.parent
    if p is None:
        return [n]
    d = desc(n)
    return [c for c in p.elements() if c.tag == n.tag and desc(c) == d]


def pdesc(n: Node) -> str:
    """The descriptor with the node's position among its same-descriptor siblings (``div@2``)
    when it has any."""
    sibs = same_desc_siblings(n)
    if len(sibs) < 2:
        return desc(n)
    return desc(n) + "@" + str(next(k for k, s in enumerate(sibs) if s is n))


def chain_of(n: Node) -> List[str]:
    """The card chain: the descriptors of the node's 3 ancestors and its own (outermost first)."""
    p = n.parent
    g = p.parent if p is not None else None
    gg = g.parent if g is not None else None
    return [desc(gg), desc(g), desc(p), desc(n)]


def desc_loose(d: str) -> str:
    """A descriptor without its classes after the first (``li.wp-block-post.page`` -> ``li.wp-block-post``)."""
    return ".".join(str(d or "").split(".")[:2])


def chain_matches(c: Sequence[str], spec: Dict[str, Any]) -> bool:
    """Does a node's chain (``chain_of``) match a CARD spec's? Exactly; or, for a spec compiled ``loose``
    (a blog system that marks each result card with its post type - ``li.wp-block-post.page`` for the
    demonstrated results, ``li.wp-block-post.post`` for a news post the site lists among them for another value), the
    same three ancestors and the card's own tag, id and FIRST class: a sibling card of the same result list whose
    further class names a type. A title spec matches exactly."""
    w = list(spec.get("chain") or [])
    c = list(c)
    if c == w:
        return True
    if not spec.get("loose") or spec.get("kind") == "title" or len(c) != 4 or len(w) != 4:
        return False
    return c[:3] == w[:3] and desc_loose(c[3]) == desc_loose(w[3])


def rel_path(x: Node, top: Node) -> str:
    """The positional path from ``x`` up to (not including) its ancestor ``top``."""
    out = []
    while x is not None and x is not top:
        out.append(pdesc(x))
        x = x.parent
    return SEP.join(out)


def contains(n: Node, x: Node) -> bool:
    while x is not None:
        if x is n:
            return True
        x = x.parent
    return False


#: a header / footer inside a sectioning element is that section's own (a module switch so the
#: measurement can compare before / after; READ_JS and CARDS_JS apply the same rule live)
SCOPED_HEADERS = True


def _sectioning_ancestor(x: Node) -> bool:
    """The nearest sectioning ancestor is an ``article`` (a card's own header / footer). A header inside ``main`` or a
    ``section`` stays page chrome: sites put their banner and menu there."""
    y = x.parent
    while y is not None and not y.tag.startswith("#"):
        if y.tag in ("article", "aside", "main", "nav", "section"):
            return y.tag == "article"
        y = y.parent
    return False


def area_of(n: Node) -> str:
    """READ_JS's ``areaOf``: the nearest header / nav / footer / aside / main (or role). A ``header`` / ``footer``
    without a role whose nearest sectioning ancestor is an ``article`` is that article's own header (HTML: no banner, no
    contentinfo), never the page chrome - the next area up decides (every
    result title in ``article > header.entry-header`` would read as page header and be left out of the result region)."""
    roles = {"banner": "header", "navigation": "nav", "contentinfo": "footer", "complementary": "aside",
             "main": "main", "search": "search", "dialog": "dialog"}
    x: Optional[Node] = n
    while x is not None and not x.tag.startswith("#"):
        r = x.attrs.get("role") or ""
        if SCOPED_HEADERS and x.tag in ("header", "footer") and not r and _sectioning_ancestor(x):
            x = x.parent
            continue
        if x.tag in ("header", "nav", "footer", "aside", "main") or r in roles:
            if x.tag == "main" or r == "main":
                return "main"
            if x.tag == "header" or r == "banner":
                return "header"
            if x.tag == "nav" or r == "navigation":
                return "nav"
            if x.tag == "footer" or r == "contentinfo":
                return "footer"
            if x.tag == "aside" or r == "complementary":
                return "aside"
            return roles.get(r, "body")
        x = x.parent
    return "body"


def is_control(n: Node) -> bool:
    """``web_arch_run.CONTROL_SEL`` on one element."""
    a = n.attrs
    if n.tag == "a" and "href" in a:
        return True
    if n.tag in ("button", "select", "summary"):
        return True
    if n.tag == "input" and (a.get("type") or "").lower() in ("submit", "button", "checkbox", "radio"):
        return True
    return (a.get("role") or "") in ("button", "link", "tab", "option", "menuitem", "checkbox")


def control_nodes(root: Node) -> List[Node]:
    """The page reader's numbered elements in order (``elements[].i`` indexes this list)."""
    return [n for n in walk(root) if is_control(n)]


def _pct(part: str, extra: str) -> str:
    """Percent-encode what the browser's URL serializer encodes (C0 controls, space, non-ASCII as
    UTF-8, and ``extra``); an existing ``%XX`` stays."""
    out = []
    for ch in part:
        o = ord(ch)
        if o <= 0x20 or o >= 0x7f or ch in extra:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)
    return "".join(out)


def browser_url(u: str) -> str:
    """``u`` serialized as the browser's ``a.href`` gives it (WHATWG): the host lower-cased, the path and
    the query percent-encoded (a space as ``%20``, ``ö`` as ``%C3%B6``) - a recorded DOM's raw href
    and the reader's ``a.href`` then compare equal (a result link ``?q=Knoll International``
    vs ``?q=Knoll%20International``)."""
    s = str(u or "")
    try:
        p = urllib.parse.urlsplit(s)
    except ValueError:
        return s
    if p.scheme.lower() not in ("http", "https"):
        return s
    host = p.netloc
    try:
        hn = p.hostname or ""
        if hn and any(ord(c) > 0x7f for c in hn):
            host = host.replace(p.hostname, hn.encode("idna").decode("ascii"))
    except (UnicodeError, ValueError):
        pass
    return urllib.parse.urlunsplit((p.scheme.lower(), host.lower(), _pct(p.path, '"<>`{}'),
                                    _pct(p.query, "\"<>'"), _pct(p.fragment, '"<>`')))


def norm(u: str) -> str:
    from anyuse.execution import web_arch as W
    return W.norm_url(browser_url(str(u or "").split("#", 1)[0]))


def href_of(n: Node, page_url: str, base: str = "") -> str:
    """The link's URL as the browser resolves it (``a.href``): an empty href is the document's URL;
    serialized as the browser does (``browser_url``)."""
    h = re.sub(r"[\t\n\r]", "", (n.attrs.get("href") or "").strip())
    if h.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
        return h
    try:
        b = urllib.parse.urljoin(page_url, base) if base else page_url
        return browser_url(urllib.parse.urljoin(b, h) if h else b)
    except ValueError:
        return ""


def anchors(root: Node, page_url: str, base: str = "") -> List[Tuple[Node, str]]:
    return [(n, href_of(n, page_url, base)) for n in walk(root) if n.tag == "a" and "href" in n.attrs]


# ---------------------------------------------------------------------------
# compile time: the title link and the card of each demonstrated result
# ---------------------------------------------------------------------------

HEADINGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))


def heading_role(a: Node, card: Node) -> bool:
    """Is the link a HEADING link of its card: a heading on its path up to the card, or a heading inside it?"""
    return any(x.tag in HEADINGS for x in _up_to(a, card)) or any(x.tag in HEADINGS for x in walk(a))


def role_of(a: Node, card: Node) -> Dict[str, Any]:
    """Web-watch: the ROLE of a demonstrated title link in its card - its own descriptor and whether
    it is a heading link. A card's result link is its title-path link, else a link of this role; a link
    of another role (a category / tag / author link) is never the card's result."""
    return {"anchor": desc(a), "heading": heading_role(a, card)}


def title_anchor(cands: Sequence[Node], title: str) -> Optional[Node]:
    """Of the links to one result URL: the one whose text names the reported title best, then the
    longest text, then the first."""
    from anyuse.execution import web_arch as W
    if not cands:
        return None
    want = W.content_tokens(title)

    def score(k_n):
        k, n = k_n
        t = label_of(n)
        have = set(W.tokens(t))
        share = sum(1 for w in want if w in have) / len(want) if want else 0.0
        # a link with its own text before an image link named by its alt
        return (round(share, 3), bool(text_of(n, 200)), min(len(t), 160), -k)
    return max(enumerate(cands), key=score)[1]


def _mirrors(s: Node, path: str, rx, not_url: str, page_url: str, base: str) -> List[str]:
    """URLs of the region links in ``s`` at the positional ``path`` (other than ``not_url``)."""
    out = []
    for x in walk(s) if s.tag != "a" else [s]:
        if x.tag != "a" or "href" not in x.attrs:
            continue
        if rel_path(x, s) != path and not (x is s and path == ""):
            continue
        u = href_of(x, page_url, base)
        if u and norm(u) != not_url and (rx is None or rx.match(u.split("#", 1)[0])):
            out.append(norm(u))
    return out


def find_card(t: Node, url: str, others: Sequence[Node], rx, page_url: str, base: str) -> Tuple[Optional[Node], str]:
    """The card of the title link ``t`` (see the module doc): (node, "separates" | "siblings"),
    or (None, why)."""
    u = norm(url)
    ups = []
    x: Optional[Node] = t
    while x is not None and not x.tag.startswith("#") and x.tag not in NEVER_CARD and len(ups) < MAX_UP:
        ups.append(x)
        x = x.parent
    lone = None
    for n in ups:
        if any(contains(n, o) for o in others):
            break                                      # n holds another demonstrated result: too high
        sibs = same_desc_siblings(n)
        if len(sibs) < 2:
            continue
        path = rel_path(t, n)
        # n holds ONE result: no other region URL at the title's place inside it (else no higher
        # ancestor holds one result either)
        if _mirrors(n, path, rx, u, page_url, base):
            break
        for s in sibs:
            if s is n:
                continue
            for o in others:
                if contains(s, o) and rel_path(o, s) == path:
                    return n, "separates"
        if lone is None:
            hits = sum(1 for s in sibs if s is not n and _mirrors(s, path, rx, u, page_url, base))
            if hits >= 2:
                lone = n
                if not others:
                    break
    if lone is not None:
        return lone, "siblings"
    return None, "no repeated ancestor holds one result while its siblings hold others"


def _path_key(u: str) -> str:
    """The URL without its query and fragment (a result link's tracking query: ``?_pos=1&_sid=..``)."""
    try:
        p = urllib.parse.urlsplit(norm(u))
    except ValueError:
        return ""
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def result_cards(dom: str, page_url: str, wanted: Sequence[Sequence[Any]], regex: str = "",
                 avoid_widgets: bool = False, avoid_specs: Sequence[Dict[str, Any]] = (),
                 v10: bool = False, v15: bool = False) -> Dict[str, Any]:
    """The compiled result structure of a demonstration's result page: for each wanted
    ``(url, title[, other URLs of the same result])`` its title link and card, as
    ``{"cards": [spec, ...], "results": [...]}``; a spec is ``{"kind": "card"|"title", "chain":
    [4 descriptors], "rels": [title paths]}``. A result's links are those to its URL; else (a URL
    the demonstrator typed, a redirect) those whose URL without the query is one of its URLs'.
    ``avoid_widgets`` (version 8, the next candidate region): a result's links outside a sidebar / widget
    (``widget_why``) and outside the cards of ``avoid_specs`` (the rejected structures) are its candidates when it
    has any. ``v10``: the widget rule with version 10's sidebar names (``widget_why``)."""
    root, base = parse(dom)
    rx = re.compile(regex, re.I) if regex else None
    by: Dict[str, List[Node]] = {}
    by_path: Dict[str, List[Node]] = {}
    for n, h in anchors(root, page_url, base):
        if h:
            by.setdefault(norm(h), []).append(n)
            by_path.setdefault(_path_key(h), []).append(n)
    titles: List[Tuple[str, str, Optional[Node]]] = []
    seen = set()
    for w in wanted:
        url, title = str(w[0] or ""), str(w[1] or "") if len(w) > 1 else ""
        alts = [str(x) for x in (w[2] if len(w) > 2 else []) or [] if x]
        k = norm(url)
        if not url or k in seen:
            continue
        seen.add(k)
        cands = by.get(k) or []
        for a in [url] + alts:
            if not cands:
                cands = by.get(norm(a)) or by_path.get(_path_key(a)) or []
        if avoid_widgets:
            cands = [c for c in cands if not widget_why(c, v10=v10, v15=v15) and _card_hit(c, avoid_specs) is None] \
                or cands
        titles.append((url, title, title_anchor(cands, title)))
    specs: List[Dict[str, Any]] = []
    rows = []
    for url, title, t in titles:
        row: Dict[str, Any] = {"url": url, "title_link": None}
        if t is None:
            row["why"] = "no link of this URL on the result page"
            rows.append(row)
            continue
        others = [o for _u, _t, o in titles if o is not None and o is not t and norm(_u) != norm(url)]
        card, how = find_card(t, href_of(t, page_url, base) or url, others, rx, page_url, base)
        row.update(title_link=label_of(t)[:120], title_area=area_of(t), title_chain=chain_of(t))
        if card is None:
            spec = {"kind": "title", "chain": chain_of(t), "rels": [""], "roles": [role_of(t, t)]}
            row.update(card=None, how=how)
        else:
            spec = {"kind": "card", "chain": chain_of(card), "rels": [rel_path(t, card)], "roles": [role_of(t, card)]}
            row.update(card=desc(card), how=how, card_siblings=len(same_desc_siblings(card)))
        row["title_role"] = spec["roles"][0]
        for s in specs:
            if s["kind"] == spec["kind"] and s["chain"] == spec["chain"]:
                if spec["rels"][0] not in s["rels"]:
                    s["rels"].append(spec["rels"][0])
                if spec["roles"][0] not in s["roles"]:
                    s["roles"].append(spec["roles"][0])
                break
        else:
            specs.append(spec)
        rows.append(row)
    # a title spec never shadows a card spec: cards first
    specs.sort(key=lambda s: s["kind"] != "card")
    return {"cards": specs, "results": rows}


# ---------------------------------------------------------------------------
# version 8 (a compiled card chain that is a "NEUE REPORTS" sidebar
# ``div#box_recent ... li.tf-sidelist__item`` inside ``aside``, whose items coincide with the results for the
# demonstration value): a result structure must be the QUERY-DEPENDENT region
# ---------------------------------------------------------------------------

#: id / class tokens that NAME a related / new / popular / latest widget (split at - _ and spaces; lower case). Layout
#: words are not among them: a "sidebar" / "widget" class also wraps main content
#: (``cms-section-sidebar-main-content``, ``page-widget-container``)
WIDGET_TOKENS = frozenset("""
related recent recently popular latest trending recommended recommendations similar neue neueste aktuell aktuelle
beliebt beliebte meistgelesen empfehlungen bestseller bestsellers topseller nouveautes populaires recents recientes
populares nieuwste populair gerelateerd senaste populara nyeste populaere uusimmat suosituimmat najnowsze popularne
polecane
""".split())
#: the label / heading of a widget box ("Neue Reports", "Related posts", "Meistgelesen", ...), folded
WIDGET_LABEL = re.compile(
    r"\b(?:neue?[nrs]?|neueste[nrs]?|aktuell\w*|beliebt\w*|meistgelesen\w*|meist gelesen\w*|related|recent\w*|popular|"
    r"latest|most (?:read|viewed|popular)|trending|recommended|empfohlen\w*|empfehlung\w*|similar|ahnliche\w*|"
    r"auch interessant|weitere artikel|mehr zum thema|you may also like|nouveaut\w*|populaires?|derniers? articles?|"
    r"articoli recenti|piu letti|recientes|populares|nieuwste|populair\w*|gerelateerd\w*|senaste|popular\w*|nyeste|"
    r"uusimmat|suosituimmat|najnowsze|popularne|polecane)\b")
#: how far above a card a widget container is looked for
WIDGET_UP = 6


def _own_heading(box: Node, n: Node) -> bool:
    """(a search result "Fachverband SHK NRW stellt Führungsteam neu
    auf" in ``li > h4.title > a``: the ``li``'s first child - the result's OWN title - would be read as the box's
    label, "neu" match "Neue ...", and the result card become a widget box; a title "Neue Nikon Kompaktkamera"
    the same): is ``box`` ONE result's card whose "label" is its own title - its label element (first element child)
    holds a link, and every link in the box goes to that one target? A widget box ("Neue Reports") lists several."""
    kids = box.elements()
    if not kids:
        return False
    head = [str(a.attrs.get("href") or "").split("#")[0] for a in walk(kids[0]) if a.tag == "a" and a.attrs.get("href")]
    if not head:
        return False
    targets = {str(a.attrs.get("href") or "").split("#")[0] for a in walk(box) if a.tag == "a" and a.attrs.get("href")}
    return targets == {head[0]}


def _box_label(n: Node) -> str:
    """A container's own label: its first element child's text when that is a heading or a label / title element
    (<= 60 characters), else ""."""
    kids = n.elements()
    if not kids:
        return ""
    k = kids[0]
    cls = (k.attrs.get("class") or "").lower()
    if k.tag in HEADINGS or k.tag in ("header", "legend", "caption") or re.search(r"label|title|heading|header|headline", cls):
        t = text_of(k, 80)
        return t if 0 < len(t) <= 60 else ""
    return ""


# ---------------------------------------------------------------------------
# version 10 (a demonstration that reports an item of ``div#sidebar-a > ul >
# li``: the compiled structure keeps that sidebar list next to the result table, and version 8's widget rule has no
# "sidebar" token - the tool lists the sidebar's news): an element whose OWN id or class names a sidebar is a
# widget container
# ---------------------------------------------------------------------------

#: one id / class token that names a sidebar (a compound such as "rightsidebar" holds "sidebar")
SIDEBAR_WORDS_V10 = frozenset(("sidebar", "sidebars", "aside", "rail", "siderail", "sidecol", "sidecolumn", "sidepanel",
                               "sidenav", "sidebox", "sideblock"))
#: "side" followed by one of these tokens names a sidebar too ("side-bar", "side_col", "side-panel")
SIDE_PAIR_V10 = frozenset(("bar", "col", "column", "panel", "rail", "nav", "box", "block"))
#: a LAYOUT name that mentions the sidebar without being one: a modifier word ("has-sidebar", "with-sidebar",
#: "no-sidebar", "content-sidebar-wrap", "sidebar-toggle") ...
LAYOUT_WORDS_V10 = frozenset(("has", "with", "no", "without", "wrap", "wrapper", "layout", "toggle", "toggler", "overlay",
                              "backdrop", "trigger"))


def sidebar_name(name: str, v15: bool = False) -> bool:
    """Version 10: does ONE id or class name a sidebar? Its tokens (split at - _ and spaces, lower case) hold a
    sidebar word (``SIDEBAR_WORDS_V10``, or a token containing "sidebar", or "side" + ``SIDE_PAIR_V10``) and it is not
    a layout name: no modifier word (``LAYOUT_WORDS_V10``), no "main" after the sidebar word
    (``cms-section-sidebar-main-content`` is the main column of a sidebar layout; ``td-main-sidebar`` is a
    sidebar), no "content" before it (``content-sidebar`` is a page layout; ``sidebar-content`` is the
    sidebar's own content). ``v15`` (version 15): the token rule alone, without those token-order rules - an
    element that holds the page's main landmark, or every card the structure reads, is a layout wrapper by its
    structure (``holds_main``, ``structure_items``)."""
    toks = [t for t in re.split(r"[-_\s]+", str(name or "").lower()) if t]
    at = None
    for i, t in enumerate(toks):
        if t in SIDEBAR_WORDS_V10 or ("sidebar" in t and len(t) <= 16) or \
                (t == "side" and i + 1 < len(toks) and toks[i + 1] in SIDE_PAIR_V10):
            at = i
            break
    if at is None:
        return False
    if any(t in LAYOUT_WORDS_V10 for t in toks):
        return False
    if not v15 and ("main" in toks[at + 1:] or "content" in toks[:at]):
        return False
    return True


def holds_main(x: Node, v15: bool = False) -> bool:
    """Version 10: does the element hold the page's MAIN column - a ``main`` element, ``role=main``, or an element
    whose id / class has the token "main" (``cms-section-sidebar`` holds ``...-sidebar-main-content``)?
    Such an element is a layout wrapper of a sidebar AND the main content, never a sidebar itself. ``v15``
    (version 15): the main landmark only (the HTML / ARIA structure), no class token."""
    for n in walk(x):
        if n.tag == "main" or str(n.attrs.get("role") or "").lower() == "main":
            return True
        if v15:
            continue
        names = str(n.attrs.get("id") or "") + " " + str(n.attrs.get("class") or "")
        if "main" in re.split(r"[-_\s]+", names.lower()):
            return True
    return False


def widget_why(n: Optional[Node], v10: bool = False, v15: bool = False) -> str:
    """Version 8: why the node sits in a sidebar / widget - the page's ``aside`` / ``nav`` / ``footer`` / ``header``
    (READ_JS's areas), or a container within ``WIDGET_UP`` levels whose id / class names a widget
    (``WIDGET_TOKENS``) or whose own label says related / new / popular / latest (``WIDGET_LABEL``); "" when not.
    ``v10`` (version 10; after version 8's reasons): also a container whose own id or one of its classes names a
    SIDEBAR (``sidebar_ancestor``: one that does not hold the page's main column - a layout wrapper). ``v15``: the
    sidebar rule of version 15 (``sidebar_name``, ``holds_main``)."""
    from anyuse.execution import web_arch as W
    if n is None:
        return ""
    a = area_of(n)
    if a in ("aside", "nav", "footer", "header"):
        return f"inside the page's {a}"
    x: Optional[Node] = n
    for _k in range(WIDGET_UP):
        if x is None or x.tag.startswith("#") or x.tag in ("html", "body", "main"):
            break
        toks = set(re.split(r"[-_\s]+", (x.attrs.get("id") or "").lower() + " " + (x.attrs.get("class") or "").lower()))
        hit = sorted(t for t in toks if t in WIDGET_TOKENS)
        if hit:
            return f"inside a widget container ({desc(x)}: {hit[0]})"
        lab = _box_label(x)
        if lab and WIDGET_LABEL.search(W.fold(lab)) and not _own_heading(x, n):
            return f"inside a widget box labelled {lab[:40]!r}"
        x = x.parent
    if v10:
        sa = sidebar_ancestor(n, v15=v15)
        if sa is not None:
            nm = next(nm for nm in [sa.attrs.get("id") or ""] + (sa.attrs.get("class") or "").split()
                      if sidebar_name(nm, v15=v15))
            return f"inside a sidebar container ({desc(sa)}: {nm[:40]})"
    return ""


def sidebar_ancestor(n: Optional[Node], v15: bool = False) -> Optional[Node]:
    """Version 10: the element within ``WIDGET_UP`` levels of ``n`` (itself included) whose own id or one of its
    classes names a sidebar (``sidebar_name``) and that does not hold the page's main column (``holds_main``); None."""
    x: Optional[Node] = n
    for _k in range(WIDGET_UP):
        if x is None or x.tag.startswith("#") or x.tag in ("html", "body", "main"):
            break
        if any(sidebar_name(nm, v15=v15) for nm in [x.attrs.get("id") or ""] + (x.attrs.get("class") or "").split()) \
                and not holds_main(x, v15=v15):
            return x
        x = x.parent
    return None


def widget_results(dom: str, page_url: str, wanted: Sequence[Sequence[Any]], v10: bool = True,
                   v15: bool = False, static_specs: Sequence[Dict[str, Any]] = ()) -> List[str]:
    """Version 10: the demonstrated results (``wanted``, as ``result_cards`` takes them) whose every link on the
    recorded page sits in a sidebar / widget (``widget_why``) - a sidebar item the demonstrator reported among its
    results; their URLs. ``v15``: with ``static_specs`` (the blocks that show the same items without the query,
    ``demo_compile.query_region``) a link sits in a widget when it is in such a block's card, by that differential
    alone; else version 15's sidebar rule, and a sidebar-named container that holds a link of every demonstrated
    result is around the results, never beside them."""
    root, base = parse(dom)
    by: Dict[str, List[Node]] = {}
    by_path: Dict[str, List[Node]] = {}
    for n, h in anchors(root, page_url, base):
        if h:
            by.setdefault(norm(h), []).append(n)
            by_path.setdefault(_path_key(h), []).append(n)
    out: List[str] = []
    found: List[List[Node]] = []
    for w in wanted:
        url = str(w[0] or "")
        alts = [str(x) for x in (w[2] if len(w) > 2 else []) or [] if x]
        cands = by.get(norm(url)) or []
        for a in [url] + alts:
            if not cands:
                cands = by.get(norm(a)) or by_path.get(_path_key(a)) or []
        found.append(cands)
        if v15 and static_specs:
            if cands and all(_card_hit(c, static_specs) is not None for c in cands):
                out.append(url)
            continue
        if cands and all(widget_why(c, v10=v10, v15=v15) for c in cands):
            out.append(url)
    if v15 and out and not static_specs:
        around = []
        for w, cands in zip(wanted, found):
            if str(w[0] or "") not in out:
                continue
            sa = [sidebar_ancestor(c, v15=True) for c in cands]
            if all(x is not None and all(any(x is y or contains(x, y) for y in cs) for cs in found if cs)
                   for x in sa):
                around.append(str(w[0] or ""))
        out = [u for u in out if u not in around]
    return out


def structure_items(dom: str, page_url: str, specs: Sequence[Dict[str, Any]], v10: bool = False,
                    v15: bool = False) -> List[Dict[str, Any]]:
    """Version 8 (compile time): every card of a RECORDED page that the compiled structure (``specs``) reads, in
    document order - its result URL (the link at a compiled title path, else its first link), its whole text
    (<= 2000 characters), its area and ``widget_why`` (``v10``: with version 10's sidebar names). A title spec's card is
    its link. ``v15``: version 15's sidebar rule, a title spec's card text is its repeated tile (``tile15``), and each
    card names the index of the spec that reads it (``spec``)."""
    root, base = parse(dom)
    out: List[Dict[str, Any]] = []
    taken: List[Node] = []
    for x in walk(root):
        if x.tag in ("html", "body") or any(contains(t, x) for t in taken[-3:]):
            continue
        c = chain_of(x)
        si = next((j for j, sp in enumerate(specs) if chain_matches(c, sp) and
                   (sp.get("kind") != "title" or (x.tag == "a" and "href" in x.attrs))), None)
        if si is None:
            continue
        taken.append(x)
        links = [a for a in ([x] if x.tag == "a" else walk(x)) if a.tag == "a" and "href" in a.attrs]
        rels = specs[si].get("rels") or [""]
        prim = next((a for a in links if (rel_path(a, x) if a is not x else "") in rels), links[0] if links else None)
        # a title spec's card is its link: its text is the tile around it (READ_JS's ``ctx`` container)
        box = (tile15(x) if v15 else _tile_of(x)) if specs[si].get("kind") == "title" else x
        url = href_of(prim, page_url, base) if prim is not None else ""
        out.append({"url": url, "text": card_words(box, url), "area": area_of(x),
                    "widget": widget_why(x, v10=v10, v15=v15), **({"spec": si} if v15 else {})})
    if v10:
        # version 10: a sidebar-named container that holds EVERY card the structure reads is around the results
        # (a shop's search page that wraps its listing in a filter sidebar's container), never a sidebar beside them
        for it, x in zip(out, taken):
            if it["widget"].startswith("inside a sidebar container"):
                sa = sidebar_ancestor(x, v15=v15)
                if sa is not None and all(y is sa or contains(sa, y) for y in taken):
                    it["widget"] = ""
    return out


def card_words(n: Node, url: str = "", limit: int = 2000) -> str:
    """A card's words for the query check: its visible text (whitespace collapsed, <= ``limit`` characters; a
    template-rendered card's indentation never uses the budget up), its links' and images' ``title`` /
    ``aria-label`` / ``alt``, and its result URL's path words."""
    parts: List[str] = []
    size = 0
    stack: List[Any] = [n]
    while stack and size < limit:
        x = stack.pop()
        if isinstance(x, str):
            t = " ".join(x.split())
            if t:
                parts.append(t)
                size += len(t) + 1
            continue
        if x.inert or x.tag in RAW:
            continue
        for k in ("title", "aria-label", "alt"):
            v = " ".join(str(x.attrs.get(k) or "").split())
            if v and x.tag in ("a", "img"):
                parts.append(v)
                size += len(v) + 1
        stack.extend(reversed(x.children))
    try:
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    except ValueError:
        path = ""
    parts.append(re.sub(r"[/_\-.]+", " ", path))
    return " ".join(parts)[:limit + 400]


_TILE_CLASS = re.compile(r"card|product|item|tile|result", re.I)


def tile15(a: Node) -> Node:
    """Version 15: the tile around a link by the page's structure - its nearest ancestor (``MAX_UP`` levels, never
    ``body``) whose parent holds another child of the same descriptor that links another target (the repeated
    tiles of a list: two plain ``div`` inside one card, one with the title and one with the brand, are parts of one
    tile); else ``_tile_of``'s ``li`` / ``article`` / class-name rule."""
    own = {str(n.attrs.get("href") or "") for n in walk(a) if n.tag == "a"}
    x = a.parent
    for _k in range(MAX_UP):
        if x is None or x.tag.startswith("#") or x.tag in ("html", "body") or x.parent is None:
            break
        d = desc(x)
        if any(c is not x and desc(c) == d and
               any(n.tag == "a" and n.attrs.get("href") and str(n.attrs.get("href")) not in own for n in walk(c))
               for c in x.parent.elements()):
            return x
        x = x.parent
    return _tile_of(a)


def _tile_of(a: Node) -> Node:
    """READ_JS's ``ctx`` container of a link: its nearest ``li`` / ``article`` / card-, product-, item-, tile- or
    result-classed ancestor (else the link)."""
    x = a.parent
    while x is not None and not x.tag.startswith("#") and x.tag not in ("html", "body"):
        if x.tag in ("li", "article") or _TILE_CLASS.search(x.attrs.get("class") or ""):
            return x
        x = x.parent
    return a


# ---------------------------------------------------------------------------
# run time: the card of every link (CARDS_JS in the page; cards_map on a recorded DOM)
# ---------------------------------------------------------------------------

def cards_map(root: Node, specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS`` on a parsed document: ``{"n": cards, "map": {i: [spec, card, rel_ok, in_heading]}}``
    keyed by the page reader's element index (``control_nodes``)."""
    ids: Dict[int, int] = {}
    out: Dict[str, List[int]] = {}
    for k, a in enumerate(control_nodes(root)):
        if a.tag != "a" or "href" not in a.attrs:
            continue
        hit = _card_hit(a, specs)
        if hit is None:
            continue
        si, card = hit
        cid = ids.setdefault(id(card), len(ids))
        rel = rel_path(a, card)
        inh = any(x.tag in ("h1", "h2", "h3", "h4", "h5", "h6") for x in _up_to(a, card))
        out[str(k)] = [si, cid, int(rel in specs[si]["rels"]), int(inh)]
    return {"n": len(ids), "map": out}


def _up_to(a: Node, card: Node) -> List[Node]:
    out = []
    x: Optional[Node] = a
    while x is not None:
        out.append(x)
        if x is card:
            break
        x = x.parent
    return out


def _card_hit(a: Node, specs: Sequence[Dict[str, Any]]) -> Optional[Tuple[int, Node]]:
    """The lowest ancestor of ``a`` matching a CARD spec's chain; else ``a`` itself when its
    chain is a TITLE spec's (a title spec never takes a link that sits in a compiled card)."""
    x: Optional[Node] = a
    for _j in range(MAX_UP):
        if x is None or x.tag.startswith("#") or x.tag in ("html", "body"):
            break
        c = chain_of(x)
        for si, s in enumerate(specs):
            if s.get("kind") != "title" and chain_matches(c, s):
                return si, x
        x = x.parent
    c = chain_of(a)
    for si, s in enumerate(specs):
        if s.get("kind") == "title" and c == list(s.get("chain") or []):
            return si, a
    return None


#: the card of every link, in the page (a local read). ``%s`` is the JSON of the specs.
CARDS_JS = r"""
/*anyuse:cards*/ ((specs) => {
  const SEL = %s;
  const dc = new Map();
  const D = n => { if (!n || !n.tagName) return ''; if (dc.has(n)) return dc.get(n);
    const i = n.getAttribute('id') || ''; const idp = i && !/\d/.test(i) ? '#' + i : '';
    const svg = (typeof SVGElement !== 'undefined') && (n instanceof SVGElement);
    const cls = svg ? '' : (n.getAttribute('class') || '').split(/\s+/).filter(c => c && !/\d/.test(c)).slice(0, 2).join('.');
    const d = n.tagName.toLowerCase() + idp + (cls ? '.' + cls : ''); dc.set(n, d); return d; };
  const PD = n => { const d = D(n); const p = n.parentElement; if (!p) return d; let cnt = 0, k = -1;
    for (const c of p.children) { if (c.tagName === n.tagName && D(c) === d) { if (c === n) k = cnt; cnt++; } }
    return cnt >= 2 ? d + '@' + k : d; };
  const CH = n => { const p = n.parentElement; const g = p && p.parentElement; const gg = g && g.parentElement;
    return [D(gg), D(g), D(p), D(n)].join('\u0001'); };
  const want = specs.map(s => (s.chain || []).join('\u0001'));
  const DL = d => d.split('.').slice(0, 2).join('.');
  const wantL = specs.map(s => (s.loose && s.kind !== 'title' && (s.chain || []).length === 4) ?
    s.chain.slice(0, 3).concat([DL(s.chain[3])]).join('\u0001') : null);
  const CHL = n => { const p = n.parentElement; const g = p && p.parentElement; const gg = g && g.parentElement;
    return [D(gg), D(g), D(p), DL(D(n))].join('\u0001'); };
  const ids = new Map(); const out = {};
  const all = document.querySelectorAll(SEL);
  for (let k = 0; k < all.length && k < 5000; k++) {
    const a = all[k]; if (a.tagName.toLowerCase() !== 'a' || !a.hasAttribute('href')) continue;
    let x = a, hit = -1, card = null;
    for (let j = 0; j < 14 && x && x.tagName && x !== document.body && x !== document.documentElement; j++) {
      const c = CH(x);
      for (let si = 0; si < specs.length; si++) {
        if (specs[si].kind !== 'title' && (c === want[si] || (wantL[si] !== null && CHL(x) === wantL[si]))) {
          hit = si; card = x; break; } }
      if (card) break;
      x = x.parentElement; }
    if (!card) { const c = CH(a);
      for (let si = 0; si < specs.length; si++) {
        if (specs[si].kind === 'title' && c === want[si]) { hit = si; card = a; break; } } }
    if (!card) continue;
    if (!ids.has(card)) ids.set(card, ids.size);
    const rel = []; let inh = 0; let y = a;
    while (y && y !== card) { rel.push(PD(y)); if (/^H[1-6]$/.test(y.tagName)) inh = 1; y = y.parentElement; }
    if (card && /^H[1-6]$/.test(card.tagName)) inh = 1;
    const ok = (specs[hit].rels || []).indexOf(rel.join('\u0001')) >= 0 ? 1 : 0;
    out[k] = [hit, ids.get(card), ok, inh]; }
  return {n: ids.size, map: out};
})(%s)
"""


MARK = "/*anyuse:cards*/"


def cards_js(specs: Sequence[Dict[str, Any]]) -> str:
    from anyuse.execution.web_arch_run import CONTROL_SEL
    return CARDS_JS % (json.dumps(CONTROL_SEL), json.dumps(list(specs), ensure_ascii=True))


# ---------------------------------------------------------------------------
# web-watch: the Product item's OWN microdata (``demo_tool.PAGE_EXTRA_JS_V6`` on a recorded DOM)
# ---------------------------------------------------------------------------

def _props(n: Node) -> List[str]:
    return (n.attrs.get("itemprop") or "").split()


def item_owner(n: Node) -> Optional[Node]:
    """The item a property element belongs to: its nearest ancestor with ``itemscope`` (never itself)."""
    x = n.parent
    while x is not None and not x.tag.startswith("#"):
        if "itemscope" in x.attrs:
            return x
        x = x.parent
    return None


def own_prop(item: Node, prop: str) -> Optional[Node]:
    """The first element under ``item`` carrying ``itemprop=prop`` that belongs to ``item`` itself
    (not to a nested item such as a brand, a manufacturer or an offer)."""
    for n in walk(item):
        if prop in _props(n) and item_owner(n) is item:
            return n
    return None


def _md_value(n: Optional[Node]) -> str:
    if n is None:
        return ""
    return str(n.attrs.get("content") or n.attrs.get("href") or text_of(n, 400) or "").strip()[:300]


def microdata_of(dom: str) -> Dict[str, str]:
    """The first schema.org Product microdata item's OWN name, and its own (or its own offers') price,
    currency and availability - ``PAGE_EXTRA_JS_V6``'s reading of a recorded DOM ({} with no item)."""
    root, _base = parse(dom)
    scope = next((n for n in walk(root) if "schema.org/Product" in (n.attrs.get("itemtype") or "")), None)
    if scope is None:
        return {}
    offers = [o for o in walk(scope) if "itemscope" in o.attrs and "offers" in _props(o) and item_owner(o) is scope]

    def pick(prop: str) -> str:
        n = own_prop(scope, prop)
        if n is not None:
            return _md_value(n)
        for o in offers:
            m = own_prop(o, prop)
            if m is not None:
                return _md_value(m)
        return ""
    return {"name": _md_value(own_prop(scope, "name")), "price": pick("price"), "currency": pick("priceCurrency"),
            "availability": pick("availability")}


#: version 8: a price-like text (a number with a currency mark) - ``demo_tool.PAGE_EXTRA_JS_V8``'s test
_STRUCK_PRICE = re.compile(r"\d[\d .,\u00a0]*\s?(?:€|eur|kr|sek|nok|dkk|zł|zl|pln|chf|£|\$|:-|,-)|(?:€|£|\$|chf)\s?\d",
                           re.I)
_STRUCK_TAGS = frozenset(("del", "s", "strike"))


def struck_of(dom: str, limit: int = 30) -> List[str]:
    """Version 8: the price texts a RECORDED page strikes through - inside ``del`` / ``s`` / ``strike`` or an
    element whose inline style says ``line-through`` (<= ``limit``, <= 40 characters each, document order);
    ``demo_tool.PAGE_EXTRA_JS_V8`` reads the same in the page (there also the computed style)."""
    root, _base = parse(dom)
    out: List[str] = []
    stack: List[Tuple[Any, bool]] = [(root, False)]
    while stack and len(out) < limit:
        x, struck = stack.pop()
        if isinstance(x, str):
            t = " ".join(x.split())
            if struck and t and len(t) <= 40 and _STRUCK_PRICE.search(t) and t not in out:
                out.append(t)
            continue
        if x.inert or x.tag in RAW:
            continue
        st = struck or x.tag in _STRUCK_TAGS or "line-through" in (x.attrs.get("style") or "").lower()
        stack.extend((c, st) for c in reversed(x.children))
    return out


# ---------------------------------------------------------------------------
# version 9 (a buy box's "Produkt dostępny" and the next element's
# "Wysyłka w ciągu: 2-3 dni" read as "Produkt dostępnyWysyłka ..." - inline-block elements' texts glued): the visible
# text with a separator at every element boundary
# ---------------------------------------------------------------------------

#: the elements that are block boxes by the HTML default style sheet (a recorded DOM has no computed style)
SEP_BLOCK_TAGS = frozenset("""address article aside blockquote center details dialog dd dir div dl dt fieldset figcaption
figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr legend li listing main menu nav ol optgroup option p plaintext
pre section select summary table caption thead tbody tfoot tr ul""".split())
SEP_CELL_TAGS = frozenset(("td", "th"))
SEP_SKIP_TAGS = frozenset(("head", "title", "svg", "math", "iframe", "object", "embed", "canvas", "video", "audio",
                           "img", "picture", "source", "track", "map", "area", "input", "textarea"))
_SEP_DISPLAY = re.compile(r"(?:^|;)\s*display\s*:\s*([a-z-]+)", re.I)
_SEP_VIS_HIDDEN = re.compile(r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)", re.I)
#: the main region ``web_arch_run.READ_JS`` reads its ``main_text`` in
_SEP_MAIN = (("main", None), (None, ("role", "main")), (None, ("id", "main")), (None, ("id", "content")),
             (None, ("class", "main-content")))


def _sep_display(n: Node) -> str:
    m = _SEP_DISPLAY.search(n.attrs.get("style") or "")
    if m:
        return m.group(1).lower()
    return "block" if n.tag in SEP_BLOCK_TAGS else "table-cell" if n.tag in SEP_CELL_TAGS else "inline"


def sep_join(parts: Sequence[str]) -> str:
    """The separator text's normal form (``TEXT_JS_V9`` returns the same): spaces around a line or cell break dropped,
    repeated breaks merged."""
    s = "".join(parts)
    s = re.sub(r" *\t *", "\t", s)
    s = re.sub(r"[ \t]*\n[ \t\n]*", "\n", s)
    s = re.sub(r"\t{2,}", "\t", s)
    return s.strip()


def sep_text_node(n: Node, limit: int = 20000) -> str:
    """Version 9: ``n``'s visible text with a separator at every element boundary - a line break around each
    block / inline-block / flex / grid box (by the tag's default display or an inline ``display``) and for ``<br>``, a
    tab around a table cell; whitespace collapsed inside text; ``hidden`` / inline ``display: none`` skipped, inline
    ``visibility: hidden`` text left out. ``demo_tool.TEXT_JS_V9`` is the same reading in the page (the computed
    style there)."""
    out: List[str] = []
    size = 0
    stack: List[Any] = [(n, False)]
    while stack and size < limit:
        x, closing = stack.pop()
        if closing:
            out.append(x)
            continue
        if isinstance(x, str):
            t = re.sub(r"[ \t\n\r\f]+", " ", x)
            out.append(t)
            size += len(t)
            continue
        if x.inert or x.tag in RAW or x.tag in SEP_SKIP_TAGS or x.svg or "hidden" in x.attrs:
            continue
        if x.tag == "br":
            out.append("\n")
            continue
        d = _sep_display(x)
        if d == "none":
            continue
        sep = "\t" if d == "table-cell" else "" if d in ("inline", "contents") else "\n"
        hide = bool(_SEP_VIS_HIDDEN.search(x.attrs.get("style") or ""))
        out.append(sep)
        stack.append((sep, True))
        for c in reversed(x.children):
            if isinstance(c, str):
                if not hide:
                    stack.append((c, False))
            else:
                stack.append((c, False))
    return sep_join(out)[:limit]


def sep_text_of(dom: str, limit: int = 20000) -> Dict[str, str]:
    """Version 9: a recorded DOM's body and main region read with element-boundary separators
    (``sep_text_node``): ``{"text", "main_text"}`` - what ``demo_tool.TEXT_JS_V9`` answers in the page."""
    root, _base = parse(dom)
    body = next((x for x in walk(root) if x.tag == "body"), root)
    main = None
    for tag, attr in _SEP_MAIN:
        for x in walk(body):
            if (tag and x.tag == tag) or (attr and attr[0] != "class" and x.attrs.get(attr[0]) == attr[1]) or \
                    (attr and attr[0] == "class" and attr[1] in (x.attrs.get("class") or "").split()):
                main = x
                break
        if main is not None:
            break
    return {"text": sep_text_node(body, limit), "main_text": sep_text_node(main if main is not None else body, limit)}


def recorded_answer(expr: str, page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What the in-page read ``expr`` (a cards read; ``demo_tool.PAGE_EXTRA_JS_V6`` / ``_V8``; ``TEXT_JS_V9``) answers on a
    RECORDED page (its DOM and reader elements): the offline replay of a saved render and the fake
    pages of the tests; None when ``expr`` is neither."""
    if str(expr or "").lstrip().startswith("/*anyuse:tiles10*/"):
        # the oracle's tile read (``demo_oracle.TILES_JS``) on a recorded DOM
        root, base = parse(str(page.get("dom") or ""))
        out: Dict[str, str] = {}
        for a, h in anchors(root, str(page.get("url") or ""), base):
            k = str(h or "").split("#")[0]
            if k and k not in out:
                t = _tile_of(a)
                if t is not a:
                    out[k] = text_of(t, 600)
        return out
    if str(expr or "").lstrip().startswith("/*anyuse:text9*/"):
        return sep_text_of(str(page.get("dom") or ""))
    if str(expr or "").lstrip().startswith("/*anyuse:extra8*/"):
        dom = str(page.get("dom") or "")
        return {"meta": dict(page.get("meta") or {}), "microdata": microdata_of(dom), "struck": struck_of(dom)}
    if str(expr or "").lstrip().startswith("/*anyuse:extra6*/"):
        return {"meta": dict(page.get("meta") or {}), "microdata": microdata_of(str(page.get("dom") or ""))}
    specs12 = specs_of_js_v12(expr)
    if specs12 is not None:
        return {k: v for k, v in recorded_cards_v12(page, specs12).items() if k in ("n", "map", "cards", "orphans",
                                                                                    "container")}
    specs11 = specs_of_js_v11(expr)
    if specs11 is not None:
        return {k: v for k, v in recorded_cards_v11(page, specs11).items() if k in ("n", "map", "cards", "orphans",
                                                                                    "container")}
    specs10 = specs_of_js_v10(expr)
    if specs10 is not None:
        return {k: v for k, v in recorded_cards_v10(page, specs10).items() if k in ("n", "map", "cards", "orphans",
                                                                                    "container")}
    specs8 = specs_of_js_v8(expr)
    if specs8 is not None:
        return {k: v for k, v in recorded_cards_v8(page, specs8).items() if k in ("n", "map", "cards", "orphans",
                                                                                  "container")}
    specs6 = specs_of_js_v6(expr)
    if specs6 is not None:
        return {k: v for k, v in recorded_cards_v6(page, specs6).items() if k in ("n", "map", "cards", "orphans",
                                                                                  "container")}
    specs = specs_of_js(expr)
    if specs is None:
        return None
    return {k: v for k, v in recorded_cards(page, specs).items() if k in ("n", "map")}


def specs_of_js(expr: str) -> Optional[List[Dict[str, Any]]]:
    """The specs a ``cards_js`` expression carries (for fake pages); None when it is not one."""
    s = str(expr or "").rstrip()
    if MARK not in s or not s.endswith(")"):
        return None
    try:
        return json.loads(s[s.rfind("})(") + 3:-1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# run time: one result per card
# ---------------------------------------------------------------------------

def group(elements: Sequence[Dict[str, Any]], cmap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """ONE item per card from the region's link elements (already filtered: area, URL pattern,
    controls) and the card map: ``{title, url, i, ctx, el, card, texts}`` in page order. A link
    outside every compiled card is not a result."""
    from anyuse.execution import demo_compile as C
    m = (cmap or {}).get("map") or {}
    cards: Dict[int, List[Tuple[Dict[str, Any], List[int]]]] = {}
    order: List[int] = []
    for e in elements:
        hit = m.get(str(e.get("i")))
        if not hit:
            continue
        cid = int(hit[1])
        if cid not in cards:
            cards[cid] = []
            order.append(cid)
        cards[cid].append((e, [int(v) for v in hit]))
    out = []
    for cid in order:
        els = cards[cid]
        prim = next((e for e, h in els if h[2]), None) or next((e for e, h in els if h[3]), None)
        if prim is None:
            cnt: Dict[str, int] = {}
            for e, _h in els:
                k = C._norm(str(e.get("href")))
                cnt[k] = cnt.get(k, 0) + 1
            top = max(cnt.values())
            prim = next(e for e, _h in els if cnt[C._norm(str(e.get("href")))] == top)
        k = C._norm(str(prim.get("href")))
        same = [e for e, _h in els if C._norm(str(e.get("href"))) == k]
        texts = [" ".join(str(e.get("text") or "").split()) for e in same]
        title = C.item_title(texts) or " ".join(str(prim.get("text") or "").split()) or max(texts, key=len)
        ctx = next((" ".join(str(e.get("ctx") or "").split()) for e in [prim] + same if e.get("ctx")), "")
        if not title:
            title = ctx[:160]
        if not title:
            continue
        out.append({"title": title, "url": C.strip_fragment(str(prim.get("href"))), "i": prim.get("i"), "ctx": ctx,
                    "el": prim, "card": cid, "sub_links": len({C._norm(str(e.get("href"))) for e, _h in els}) - 1})
    return out


# ---------------------------------------------------------------------------
# offline: a recorded page read as the tool would read it (the compile's self-check, the tests)
# ---------------------------------------------------------------------------

def recorded_cards(page: Dict[str, Any], specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS``'s answer for a RECORDED page (its DOM and its reader elements): the card map
    computed on the parsed DOM, keyed by the reader's element index when the recorded links agree
    with the parsed control list (``aligned``); else re-keyed by URL and order among equal URLs."""
    root, base = parse(str(page.get("dom") or ""))
    url = str(page.get("url") or "")
    got = cards_map(root, specs)
    ctl = control_nodes(root)
    rec = [e for e in page.get("elements") or [] if e.get("tag") == "a" and e.get("href")]
    agree = 0
    for e in rec:
        i = e.get("i")
        if isinstance(i, int) and 0 <= i < len(ctl) and ctl[i].tag == "a" and \
                norm(href_of(ctl[i], url, base)) == norm(str(e.get("href"))):
            agree += 1
    got["aligned"] = round(agree / len(rec), 3) if rec else 1.0
    if rec and agree < len(rec):
        # re-key: the k-th recorded link to URL u is the k-th parsed link to u
        pos: Dict[str, List[int]] = {}
        for k, n in enumerate(ctl):
            if n.tag == "a":
                pos.setdefault(norm(href_of(n, url, base)), []).append(k)
        used: Dict[str, int] = {}
        m2 = {}
        for e in rec:
            u = norm(str(e.get("href")))
            j = used.get(u, 0)
            used[u] = j + 1
            ks = pos.get(u) or []
            if j < len(ks) and str(ks[j]) in got["map"]:
                m2[str(e.get("i"))] = got["map"][str(ks[j])]
        got["map"] = m2
        got["rekeyed"] = True
    return got


# ---------------------------------------------------------------------------
# web-watch (tools of version >= 6, ``region["cards_version"] == 6``): the card read with the title
# link's ROLE (``role_of``). ``CARDS_JS_V6`` in the page; ``cards_map_v6`` on a recorded DOM.
# ---------------------------------------------------------------------------

MARK6 = "/*anyuse:cards6*/"


def card_text(n: Node) -> str:
    """A card's text: its first heading's, else its own (<= 160 characters)."""
    h = next((x for x in walk(n) if x.tag in HEADINGS), None)
    return " ".join(text_of(h if h is not None else n, 400).split())[:160]


def cards_map_v6(root: Node, specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V6`` on a parsed document: ``{"n", "map": {i: [spec, card, rel_ok, in_heading, role_ok]},
    "cards": [[card, spec, pos, links, text, area, visible]]}``.

    * ``map``: every link (the page reader's index) in a compiled card - the lowest ancestor matching a
      card spec's chain, else the link itself for a title spec - whether it sits at a compiled title
      path, whether a heading is on its path up to the card, and whether it has the demonstrated title
      link's ROLE (``role_of``: its own descriptor and whether it is a heading link);
    * ``cards``: EVERY card of the page in document order (a card's id is its rank), whether or not a
      link maps to it: its position (the reader index of its first link, else of the first control
      after it), its number of links, its text (``card_text``), its area (READ_JS's) and whether its box is
      rendered (``getClientRects``, not display:none; always 1 on a recorded DOM). A card WITH links is
      shown when the reader saw one of its links (``read_region_v6``)."""
    import bisect
    ctl = control_nodes(root)
    order = {id(n): k for k, n in enumerate(walk(root))}
    hits: Dict[int, Tuple[int, Node]] = {}
    for k, a in enumerate(ctl):
        if a.tag == "a" and "href" in a.attrs:
            hit = _card_hit(a, specs)
            if hit is not None:
                hits[k] = hit
    nodes: Dict[int, Tuple[Node, int]] = {}
    want = [list(s.get("chain") or []) for s in specs]
    for x in walk(root):
        if x.tag in ("html", "body"):
            continue
        c = chain_of(x)
        for si, s in enumerate(specs):
            if s.get("kind") != "title" and chain_matches(c, s):
                nodes.setdefault(id(x), (x, si))
                break
    for si, card in hits.values():
        nodes.setdefault(id(card), (card, si))
    cards = sorted(nodes.values(), key=lambda t: order.get(id(t[0]), 0))
    cid = {id(n): j for j, (n, _si) in enumerate(cards)}
    out: Dict[str, List[int]] = {}
    first: Dict[int, int] = {}
    nlinks: Dict[int, int] = {}
    for k in sorted(hits):
        si, card = hits[k]
        a, j, spec = ctl[k], cid[id(card)], specs[si]
        inh = any(x.tag in HEADINGS for x in _up_to(a, card))
        out[str(k)] = [si, j, int(rel_path(a, card) in (spec.get("rels") or [])), int(inh),
                       int(role_of(a, card) in (spec.get("roles") or []))]
        first.setdefault(j, k)
        nlinks[j] = nlinks.get(j, 0) + 1
    ctl_order = [order.get(id(n), 0) for n in ctl]
    rows = []
    for j, (n, si) in enumerate(cards):
        pos = first[j] if j in first else bisect.bisect_right(ctl_order, order.get(id(n), 0))
        rows.append([j, si, pos, nlinks.get(j, 0), card_text(n), area_of(n), 1])
    # web-watch: the links of the RESULTS CONTAINER (the cards' lowest common ancestor) that no
    # compiled card holds - a result card of a layout the demonstration did not show
    box = results_container([n for n, _si in cards if area_of(n) not in CHROME_AREAS])
    orphans = [[k, SEP.join(chain_of(a))] for k, a in enumerate(ctl) if box is not None and k not in hits and
               a.tag == "a" and "href" in a.attrs and contains(box, a)]
    return {"n": len(cards), "map": out, "cards": rows, "orphans": orphans,
            "container": desc(box) if box is not None else ""}


def results_container(cards: Sequence[Node]) -> Optional[Node]:
    """The lowest common ancestor of the page's result cards (a single card's parent); None when there is
    no card or the ancestor is the whole document (``html`` / ``body``: too broad to say what is a result)."""
    if not cards:
        return None
    if len(cards) == 1:
        box = cards[0].parent
    else:
        def chain(n: Node) -> List[Node]:
            out: List[Node] = []
            x: Optional[Node] = n
            while x is not None and not x.tag.startswith("#"):
                out.append(x)
                x = x.parent
            return out[::-1]
        common = chain(cards[0])
        for c in cards[1:]:
            a = chain(c)
            i = 0
            while i < len(common) and i < len(a) and common[i] is a[i]:
                i += 1
            common = common[:i]
        box = common[-1] if common else None
    if box is None or box.tag.startswith("#") or box.tag in ("html", "body"):
        return None
    return box


#: the version-6 card read, in the page (a local read). ``%s``: the reader's selector, the specs' JSON.
CARDS_JS_V6 = r"""
/*anyuse:cards6*/ ((specs) => {
  const SEL = %s;
  const dc = new Map();
  const D = n => { if (!n || !n.tagName) return ''; if (dc.has(n)) return dc.get(n);
    const i = n.getAttribute('id') || ''; const idp = i && !/\d/.test(i) ? '#' + i : '';
    const svg = (typeof SVGElement !== 'undefined') && (n instanceof SVGElement);
    const cls = svg ? '' : (n.getAttribute('class') || '').split(/\s+/).filter(c => c && !/\d/.test(c)).slice(0, 2).join('.');
    const d = n.tagName.toLowerCase() + idp + (cls ? '.' + cls : ''); dc.set(n, d); return d; };
  const PD = n => { const d = D(n); const p = n.parentElement; if (!p) return d; let cnt = 0, k = -1;
    for (const c of p.children) { if (c.tagName === n.tagName && D(c) === d) { if (c === n) k = cnt; cnt++; } }
    return cnt >= 2 ? d + '@' + k : d; };
  const CH = n => { const p = n.parentElement; const g = p && p.parentElement; const gg = g && g.parentElement;
    return [D(gg), D(g), D(p), D(n)].join('\u0001'); };
  const HX = /^H[1-6]$/;
  const shown = n => { if (!n.getClientRects().length) return false;
    const cs = getComputedStyle(n); return cs.visibility !== 'hidden'; };
  const areaOf = n => { const SEL = 'header,nav,footer,aside,main,[role=banner],[role=navigation],' +
      '[role=contentinfo],[role=complementary],[role=main],[role=search],[role=dialog]';
    let a = n.closest(SEL);
    while (a && /^(HEADER|FOOTER)$/.test(a.tagName) && !a.getAttribute('role') && a.parentElement &&
           (a.parentElement.closest('article,aside,main,nav,section') || {}).tagName === 'ARTICLE')
      a = a.parentElement.closest(SEL);
    if (!a) return 'body'; const t = a.tagName.toLowerCase(); const r = a.getAttribute('role') || '';
    if (t === 'main' || r === 'main') return 'main'; if (t === 'header' || r === 'banner') return 'header';
    if (t === 'nav' || r === 'navigation') return 'nav'; if (t === 'footer' || r === 'contentinfo') return 'footer';
    if (t === 'aside' || r === 'complementary') return 'aside'; if (r === 'dialog') return 'dialog';
    if (r === 'search') return 'search'; return 'body'; };
  const txt = (n, lim) => { const parts = []; let size = 0;
    const w = document.createTreeWalker(n, NodeFilter.SHOW_TEXT); let t;
    while (size < lim && (t = w.nextNode())) { let p = t.parentElement, skip = false;
      while (p && p !== n.parentElement) { if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(p.tagName)) { skip = true; break; }
        p = p.parentElement; }
      if (!skip) { parts.push(t.nodeValue); size += t.nodeValue.length; } }
    return parts.join(' ').split(/\s+/).filter(Boolean).join(' ').slice(0, lim); };
  const cardText = n => { const h = n.querySelector('h1,h2,h3,h4,h5,h6');
    return txt(h || n, 400).split(/\s+/).filter(Boolean).join(' ').slice(0, 160); };
  const want = specs.map(s => (s.chain || []).join('\u0001'));
  const DL = d => d.split('.').slice(0, 2).join('.');
  const wantL = specs.map(s => (s.loose && s.kind !== 'title' && (s.chain || []).length === 4) ?
    s.chain.slice(0, 3).concat([DL(s.chain[3])]).join('\u0001') : null);
  const CHL = n => { const p = n.parentElement; const g = p && p.parentElement; const gg = g && g.parentElement;
    return [D(gg), D(g), D(p), DL(D(n))].join('\u0001'); };
  const all = Array.from(document.querySelectorAll(SEL)).slice(0, 5000);
  const els = Array.from(document.querySelectorAll('*'));
  const ord = new Map(); els.forEach((e, i) => ord.set(e, i));
  const hits = new Map();
  for (let k = 0; k < all.length; k++) {
    const a = all[k]; if (a.tagName.toLowerCase() !== 'a' || !a.hasAttribute('href')) continue;
    let x = a, hit = -1, card = null;
    for (let j = 0; j < 14 && x && x.tagName && x !== document.body && x !== document.documentElement; j++) {
      const c = CH(x);
      for (let si = 0; si < specs.length; si++) {
        if (specs[si].kind !== 'title' && (c === want[si] || (wantL[si] !== null && CHL(x) === wantL[si]))) {
          hit = si; card = x; break; } }
      if (card) break;
      x = x.parentElement; }
    if (!card) { const c = CH(a);
      for (let si = 0; si < specs.length; si++) {
        if (specs[si].kind === 'title' && c === want[si]) { hit = si; card = a; break; } } }
    if (card) hits.set(k, [hit, card]); }
  const nodes = new Map();
  for (const x of els) { const tn = x.tagName.toLowerCase(); if (tn === 'html' || tn === 'body') continue;
    const c = CH(x);
    for (let si = 0; si < specs.length; si++) {
      if (specs[si].kind !== 'title' && (c === want[si] || (wantL[si] !== null && CHL(x) === wantL[si]))) {
        if (!nodes.has(x)) nodes.set(x, si); break; } } }
  for (const h of hits.values()) { if (!nodes.has(h[1])) nodes.set(h[1], h[0]); }
  const cards = Array.from(nodes.entries()).sort((p, q) => ord.get(p[0]) - ord.get(q[0]));
  const cid = new Map(); cards.forEach((p, j) => cid.set(p[0], j));
  const out = {}; const first = new Map(); const nl = new Map();
  for (const k of Array.from(hits.keys()).sort((p, q) => p - q)) {
    const [si, card] = hits.get(k); const a = all[k]; const j = cid.get(card);
    const rel = []; let inh = 0; let y = a;
    while (y && y !== card) { rel.push(PD(y)); if (HX.test(y.tagName)) inh = 1; y = y.parentElement; }
    if (card && HX.test(card.tagName)) inh = 1;
    const hrole = inh === 1 || a.querySelector('h1,h2,h3,h4,h5,h6') !== null;
    const relOk = (specs[si].rels || []).indexOf(rel.join('\u0001')) >= 0 ? 1 : 0;
    const roleOk = (specs[si].roles || []).some(r => r && r.anchor === D(a) && !!r.heading === hrole) ? 1 : 0;
    out[k] = [si, j, relOk, inh, roleOk];
    if (!first.has(j)) first.set(j, k);
    nl.set(j, (nl.get(j) || 0) + 1); }
  const cOrd = all.map(e => ord.get(e));
  const rows = cards.map((p, j) => { let pos = first.has(j) ? first.get(j) : -1;
    if (pos < 0) { const o = ord.get(p[0]); pos = cOrd.length;
      for (let k = 0; k < cOrd.length; k++) { if (cOrd[k] > o) { pos = k; break; } } }
    return [j, p[1], pos, nl.get(j) || 0, cardText(p[0]), areaOf(p[0]), shown(p[0]) ? 1 : 0]; });
  const CHROME = ['header', 'nav', 'footer'];
  const inRes = cards.map(p => p[0]).filter(n => CHROME.indexOf(areaOf(n)) < 0);
  const anc = n => { const a = []; let x = n; while (x) { a.push(x); x = x.parentElement; } return a.reverse(); };
  let box = null;
  if (inRes.length === 1) box = inRes[0].parentElement;
  else if (inRes.length > 1) { let common = anc(inRes[0]);
    for (const c of inRes.slice(1)) { const a = anc(c); let i = 0;
      while (i < common.length && i < a.length && common[i] === a[i]) i++; common = common.slice(0, i); }
    box = common.length ? common[common.length - 1] : null; }
  if (box && (box === document.body || box === document.documentElement)) box = null;
  const orphans = [];
  if (box) { for (let k = 0; k < all.length; k++) { const a = all[k];
    if (!hits.has(k) && a.tagName.toLowerCase() === 'a' && a.hasAttribute('href') && box.contains(a)) orphans.push([k, CH(a)]); } }
  return {n: cards.length, map: out, cards: rows, orphans: orphans, container: box ? D(box) : ''};
})(%s)
"""


def cards_js_v6(specs: Sequence[Dict[str, Any]]) -> str:
    from anyuse.execution.web_arch_run import CONTROL_SEL
    return CARDS_JS_V6 % (json.dumps(CONTROL_SEL), json.dumps(list(specs), ensure_ascii=True))


def specs_of_js_v6(expr: str) -> Optional[List[Dict[str, Any]]]:
    """The specs a ``cards_js_v6`` expression carries (for fake pages); None when it is not one."""
    s = str(expr or "").strip()
    if not s.startswith(MARK6) or not s.endswith(")"):
        return None
    try:
        return json.loads(s[s.rfind("})(") + 3:-1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# version 8 (titles read as their cards' "> Hausaufgabe anzeigen" /
# "> Referat anzeigen" links, or carrying "Read: " - an image link's aria-label):
# a title is the VISIBLE text of the card's title link (``region["cards_version"] == 8``)
# ---------------------------------------------------------------------------

#: class names that hide an element visually (kept for screen readers)
_HIDE_CLASS = re.compile(r"(?:^|\s)(?:sr-only|sr-text|screen-reader-text|screen-reader-only|visually-hidden|"
                         r"visuallyhidden|a11y-hidden|u-hidden-visually|hidden-visually|assistive-text|element-invisible|"
                         r"hide-text|offscreen|show-for-sr|invisible)(?:\s|$)", re.I)
_HIDE_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def _hidden(x: Node) -> bool:
    a = x.attrs
    return (str(a.get("aria-hidden") or "").lower() == "true" or "hidden" in a or bool(_HIDE_CLASS.search(a.get("class") or ""))
            or bool(_HIDE_STYLE.search(a.get("style") or "")))


def visible_text(n: Node, limit: int = 200) -> str:
    """Version 8: a link's VISIBLE text on a recorded DOM - its text nodes outside visually hidden elements
    (``aria-hidden``, ``hidden``, an sr-only class, an inline ``display: none`` / ``visibility: hidden``); never an
    ``aria-label``, ``title`` or ``alt``. ``CARDS_JS_V8`` reads the same in the page (there the computed style)."""
    out: List[str] = []
    size = 0
    stack: List[Any] = [n]
    while stack and size < limit + 100:
        x = stack.pop()
        if isinstance(x, str):
            t = " ".join(x.split())
            if t:
                out.append(t)
                size += len(t) + 1
            continue
        if x.inert or x.tag in RAW or _hidden(x):
            continue
        stack.extend(reversed(x.children))
    return " ".join(out)[:limit]


#: (a result link that wraps the whole card - the product's name
#: element (itemprop=name), its price and the "Hors Taxe" label; or the heading and the teaser):
#: a link that holds a heading or the item's own name element is named by it, never by the card's whole text; the
#: price, label and teaser lines stay the card's other text (``ctx``). Structure only (headings, role=heading,
#: microdata itemprop=name), no word list.
LINK_NAME = True
_HX = re.compile(r"^h[1-6]$")


def _before_paragraph(n: Node, limit: int = 200) -> str:
    """(``<a><span>Vorhangschiene mit 3 Innenläufen #1W</span><p>Diese
    Vorhangschiene ...</p></a>``): the visible text of a link's content BEFORE its first paragraph (the paragraph is the
    teaser); "" when the link holds no paragraph or nothing comes before it."""
    if not any(x.tag == "p" for x in walk(n)):
        return ""
    parts: List[str] = []
    for c in n.children:
        if isinstance(c, str):
            if c.strip():
                parts.append(c)
            continue
        if c.inert or c.tag in RAW:
            continue
        if c.tag == "p" or any(x.tag == "p" for x in walk(c)):
            break
        t = visible_text(c, limit)
        if t:
            parts.append(t)
    return " ".join(" ".join(parts).split())[:limit]


def link_name(n: Node, limit: int = 200, paragraphs: bool = False) -> str:
    """The title of a link that wraps more than its title: the visible text of its first heading (``h1``-``h6``,
    ``role=heading``), else of its first ``itemprop=name`` element, else (``paragraphs``) its text before its
    first paragraph; "" when it holds none of these, or holds nothing else (its own text names it)."""
    if not LINK_NAME or n is None:
        return ""
    name = ""
    for x in walk(n):
        if _HX.match(x.tag) or x.attrs.get("role") == "heading":
            name = visible_text(x, limit)
            if name:
                break
    if not name:
        for x in walk(n):
            if "name" in str(x.attrs.get("itemprop") or "").split():
                name = visible_text(x, limit)
                if name:
                    break
    if not name and paragraphs:
        name = _before_paragraph(n, limit)
    if not name:
        return ""
    whole = visible_text(n, 600)
    return name if len(" ".join(whole.split())) > len(" ".join(name.split())) + 2 else ""


#: (a card whose links are its image, a label "Personenseite", the heading's link and a
#: teaser link - all to the same target, where an agent answers the image link, titled by the label and the teaser): a link
#: whose own text is not its card's heading is named by the CARD'S heading when that heading links the same target
CARD_HEADING_CLIMB = 6


def card_heading(a: Node, page_url: str, base: str = "", climb: int = CARD_HEADING_CLIMB) -> str:
    """The visible text of the heading (``h1``-``h6``, ``role=heading``) of ``a``'s card that links ``a``'s own target
    (or sits inside a link to it); the card is the nearest ancestor holding such a heading, never one whose headings
    link another target (a list of cards); "" when there is none, or when ``a``'s own text is that heading's."""
    if not LINK_NAME or a is None:
        return ""
    target = norm(href_of(a, page_url, base))
    own = " ".join(visible_text(a, 300).split())
    x = a.parent
    for _ in range(climb):
        if x is None or x.tag.startswith("#") or x.tag in ("body", "html"):
            return ""
        heads = [h for h in walk(x) if _HX.match(h.tag) or h.attrs.get("role") == "heading"]
        mine, other = [], False
        for h in heads:
            hrefs = {norm(href_of(n, page_url, base)) for n in walk(h) if n.tag == "a" and "href" in n.attrs}
            up = h.parent
            while up is not None and up is not x and up.tag != "a":
                up = up.parent
            if up is not None and up.tag == "a" and "href" in up.attrs:
                hrefs.add(norm(href_of(up, page_url, base)))
            if target in hrefs:
                mine.append(h)
            elif hrefs:
                other = True
        if other:
            return ""
        if mine:
            t = " ".join(visible_text(mine[0], 200).split())
            return t if t and t != own else ""
        x = x.parent
    return ""


def link_names(dom: str, page_url: str, elements: Sequence[Dict[str, Any]],
               card_headings: bool = False, paragraphs: bool = False) -> Dict[str, str]:
    """The reader's link numbering (``elements[].i``) -> :func:`link_name`, for the links of a page's DOM that wrap
    more than their title (a local parse); with ``card_headings`` also :func:`card_heading` for a link whose
    own text is not its card's heading. The DOM's controls are matched to the reader's elements by position and URL
    (``recorded_cards_v6``'s alignment), else by URL in order."""
    if not LINK_NAME or not dom:
        return {}
    try:
        root, base = parse(dom)
    except Exception:                                  # noqa: BLE001 - an unparsable page names nothing
        return {}
    ctl = control_nodes(root)
    rec = [e for e in elements or [] if e.get("tag") == "a" and e.get("href") and isinstance(e.get("i"), int)]
    pos: Dict[str, List[Node]] = {}
    for n in ctl:
        if n.tag == "a":
            pos.setdefault(norm(href_of(n, page_url, base)), []).append(n)
    used: Dict[str, int] = {}
    out: Dict[str, str] = {}
    for e in rec:
        u = norm(str(e.get("href")))
        i = int(e["i"])
        node = ctl[i] if 0 <= i < len(ctl) and ctl[i].tag == "a" and norm(href_of(ctl[i], page_url, base)) == u \
            else None
        if node is None:
            j = used.get(u, 0)
            used[u] = j + 1
            ks = pos.get(u) or []
            node = ks[j] if j < len(ks) else None
        nm = link_name(node, paragraphs=paragraphs) if node is not None else ""
        if not nm and card_headings and node is not None:
            nm = card_heading(node, page_url, base)
        if nm:
            out[str(i)] = nm
    return out


def cards_map_v8(root: Node, specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V8`` on a parsed document: ``cards_map_v6`` with each mapped link's VISIBLE text as the 6th value
    of its map entry (``visible_text``)."""
    got = cards_map_v6(root, specs)
    ctl = control_nodes(root)
    for k, entry in got["map"].items():
        entry.append(visible_text(ctl[int(k)]))
    return got


def recorded_cards_v6(page: Dict[str, Any], specs: Sequence[Dict[str, Any]], _mapper=None) -> Dict[str, Any]:
    """``CARDS_JS_V6``'s answer for a RECORDED page: ``cards_map_v6`` on its parsed DOM, re-keyed by URL
    (and order among equal URLs) when the reader's elements do not align with the parsed controls
    (``recorded_cards``)."""
    root, base = parse(str(page.get("dom") or ""))
    url = str(page.get("url") or "")
    got = (_mapper or cards_map_v6)(root, specs)
    ctl = control_nodes(root)
    rec = [e for e in page.get("elements") or [] if e.get("tag") == "a" and e.get("href")]
    agree = sum(1 for e in rec if isinstance(e.get("i"), int) and 0 <= e["i"] < len(ctl) and ctl[e["i"]].tag == "a"
                and norm(href_of(ctl[e["i"]], url, base)) == norm(str(e.get("href"))))
    got["aligned"] = round(agree / len(rec), 3) if rec else 1.0
    if rec and agree < len(rec):
        pos: Dict[str, List[int]] = {}
        for k, n in enumerate(ctl):
            if n.tag == "a":
                pos.setdefault(norm(href_of(n, url, base)), []).append(k)
        used: Dict[str, int] = {}
        back: Dict[int, int] = {}
        m2 = {}
        for e in rec:
            u = norm(str(e.get("href")))
            j = used.get(u, 0)
            used[u] = j + 1
            ks = pos.get(u) or []
            if j < len(ks):
                back[ks[j]] = int(e.get("i"))
                if str(ks[j]) in got["map"]:
                    m2[str(e.get("i"))] = got["map"][str(ks[j])]
        got["map"] = m2
        got["orphans"] = [[back[k], ch] for k, ch in got.get("orphans") or [] if k in back]
        # a card's position in the reader's numbering: its first link's, else the next recorded control's
        rec_ks = sorted(back)
        import bisect
        for row in got["cards"]:
            k = bisect.bisect_left(rec_ks, int(row[2]))
            row[2] = back[rec_ks[k]] if k < len(rec_ks) else (max(back.values()) + 1 if back else int(row[2]))
        got["rekeyed"] = True
    return got


def recorded_cards_v8(page: Dict[str, Any], specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V8``'s answer for a RECORDED page (``recorded_cards_v6`` with ``cards_map_v8``)."""
    return recorded_cards_v6(page, specs, _mapper=cards_map_v8)


MARK8 = "/*anyuse:cards8*/"
#: ``CARDS_JS_V8``'s additions: a link's VISIBLE text - its text nodes outside aria-hidden / hidden /
#: sr-only-classed / display:none / visibility:hidden / clipped (1 px, ``rect(0``) elements
_VIS_JS = r"""
  const HIDE = /(^|\s)(sr-only|sr-text|screen-reader-text|screen-reader-only|visually-hidden|visuallyhidden|a11y-hidden|u-hidden-visually|hidden-visually|assistive-text|element-invisible|hide-text|offscreen|show-for-sr|invisible)(\s|$)/i;
  const hid = (e, top) => { for (let x = e; x && x !== top.parentElement; x = x.parentElement) {
      if (x.getAttribute('aria-hidden') === 'true' || x.hidden || HIDE.test(x.getAttribute('class') || '')) return true;
      const cs = getComputedStyle(x); if (cs.display === 'none' || cs.visibility === 'hidden') return true;
      if (cs.position === 'absolute' && cs.overflow === 'hidden' && (parseFloat(cs.width) <= 1 || parseFloat(cs.height) <= 1)) return true;
      if (/rect\(0/.test(cs.clip || '')) return true; } return false; };
  const vtx = a => { const parts = []; let size = 0; const w = document.createTreeWalker(a, NodeFilter.SHOW_TEXT); let t;
    while (size < 300 && (t = w.nextNode())) { const p = t.parentElement;
      if (!p || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(p.tagName) || hid(p, a)) continue;
      parts.push(t.nodeValue); size += t.nodeValue.length; }
    return parts.join(' ').split(/\s+/).filter(Boolean).join(' ').slice(0, 200); };
"""
#: the version-8 card read, in the page: ``CARDS_JS_V6`` plus each mapped link's VISIBLE text as the 6th value of its
#: map entry (``demo_cards.cards_map_v8`` is the same reading on a recorded DOM)
CARDS_JS_V8 = CARDS_JS_V6.replace("/*anyuse:cards6*/", MARK8, 1).replace(
    "  const HX = /^H[1-6]$/;", _VIS_JS.strip("\n") + "\n  const HX = /^H[1-6]$/;", 1).replace(
    "    out[k] = [si, j, relOk, inh, roleOk];", "    out[k] = [si, j, relOk, inh, roleOk, vtx(a)];", 1)


MARK10 = "/*anyuse:cards10*/"
#: version 10 (the breadth workflows: a sorted / filtered list is checked on the prices / dates its CARDS show):
#: the version-8 card read plus each card's whole visible text (<= 600 characters) as the 8th value of its row
#: (``demo_cards.cards_map_v10`` is the same reading on a recorded DOM)
CARDS_JS_V10 = CARDS_JS_V8.replace(MARK8, MARK10, 1).replace(
    "    return [j, p[1], pos, nl.get(j) || 0, cardText(p[0]), areaOf(p[0]), shown(p[0]) ? 1 : 0]; });",
    "    const tile = n => n.closest('li,article,[class*=card],[class*=product],[class*=item],[class*=tile],"
    "[class*=result]') || n;\n"
    "    return [j, p[1], pos, nl.get(j) || 0, cardText(p[0]), areaOf(p[0]), shown(p[0]) ? 1 : 0, "
    "txt(specs[p[1]].kind === 'title' ? tile(p[0]) : p[0], 1200)]; });",
    1)
#: a card's whole text (characters); a TITLE spec's card is its link - its text is the tile around it (READ_JS's ``ctx``
#: container: ``_tile_of``)
CARD_TEXT_CHARS = 1200


def cards_js_v10(specs: Sequence[Dict[str, Any]]) -> str:
    from anyuse.execution.web_arch_run import CONTROL_SEL
    return CARDS_JS_V10 % (json.dumps(CONTROL_SEL), json.dumps(list(specs), ensure_ascii=True))


def specs_of_js_v10(expr: str) -> Optional[List[Dict[str, Any]]]:
    """The specs a ``cards_js_v10`` expression carries (for fake pages); None when it is not one."""
    s = str(expr or "").strip()
    if not s.startswith(MARK10) or not s.endswith(")"):
        return None
    try:
        return json.loads(s[s.rfind("})(") + 3:-1])
    except ValueError:
        return None


def cards_map_v10(root: Node, specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V10`` on a parsed document: ``cards_map_v8`` with each card row's whole text (<= 600 characters)."""
    got = cards_map_v8(root, specs)
    nodes: Dict[int, Node] = {}
    want = [list(s.get("chain") or []) for s in specs]
    ctl = control_nodes(root)
    order = {id(n): k for k, n in enumerate(walk(root))}
    cand: List[Node] = []
    seen = set()
    for x in walk(root):
        if x.tag in ("html", "body"):
            continue
        c = chain_of(x)
        if any(s.get("kind") != "title" and chain_matches(c, s) for s in specs) and id(x) not in seen:
            cand.append(x)
            seen.add(id(x))
    for k, a in enumerate(ctl):
        if a.tag == "a" and "href" in a.attrs:
            hit = _card_hit(a, specs)
            if hit is not None and id(hit[1]) not in seen:
                cand.append(hit[1])
                seen.add(id(hit[1]))
    cand.sort(key=lambda n: order.get(id(n), 0))
    for j, n in enumerate(cand):
        nodes[j] = n
    for row in got["cards"]:
        n = nodes.get(int(row[0]))
        if n is not None and (specs[int(row[1])].get("kind") == "title"):
            n = _tile_of(n)
        row.append(text_of(n, CARD_TEXT_CHARS) if n is not None else "")
    return got


def recorded_cards_v10(page: Dict[str, Any], specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V10``'s answer for a RECORDED page (``recorded_cards_v6`` with ``cards_map_v10``)."""
    return recorded_cards_v6(page, specs, _mapper=cards_map_v10)


MARK11 = "/*anyuse:cards11*/"
_D_CLS_V10 = "    const cls = svg ? '' : (n.getAttribute('class') || '').split(/\\s+/).filter(c => c && !/\\d/.test(c)).slice(0, 2).join('.');"
_D_CLS_V11 = ("    const cl0 = svg ? [] : (n.getAttribute('class') || '').split(/\\s+/).filter(c => c && !/\\d/.test(c));\n"
              "    const cls = cl0.filter(c => { const b = c.indexOf('--'); return !(b > 0 && cl0.indexOf(c.slice(0, b)) >= 0); })"
              ".slice(0, 2).join('.');")
#: version 11: the version-10 card read with the descriptors of ``bem_free`` (a BEM modifier class of another class of
#: the element left out); ``demo_cards.cards_map_v11`` is the same reading on a recorded DOM
CARDS_JS_V11 = CARDS_JS_V10.replace(MARK10, MARK11, 1).replace(_D_CLS_V10, _D_CLS_V11, 1)
assert CARDS_JS_V11.count("cl0.indexOf(c.slice(0, b))") == 1 and MARK11 in CARDS_JS_V11


def cards_js_v11(specs: Sequence[Dict[str, Any]]) -> str:
    from anyuse.execution.web_arch_run import CONTROL_SEL
    return CARDS_JS_V11 % (json.dumps(CONTROL_SEL), json.dumps(list(specs), ensure_ascii=True))


def specs_of_js_v11(expr: str) -> Optional[List[Dict[str, Any]]]:
    """The specs a ``cards_js_v11`` expression carries (for fake pages); None when it is not one."""
    s = str(expr or "").strip()
    if not s.startswith(MARK11) or not s.endswith(")"):
        return None
    try:
        return json.loads(s[s.rfind("})(") + 3:-1])
    except ValueError:
        return None


def cards_map_v11(root: Node, specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V11`` on a parsed document: ``cards_map_v10`` with ``bem_free``'s descriptors."""
    with bem_free():
        return cards_map_v10(root, specs)


def recorded_cards_v11(page: Dict[str, Any], specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V11``'s answer for a RECORDED page (``recorded_cards_v6`` with ``cards_map_v11``)."""
    return recorded_cards_v6(page, specs, _mapper=cards_map_v11)


MARK12 = "/*anyuse:cards12*/"
#: version 12 (a promo block of CLONED result cards before the results, read by a
#: version-10 tool as results - a false accept its checks could not see): the class / id words of a container that
#: holds promotion, advertising or recommendation cards (the 11 languages; a word is a whole token of the class / id
#: - camelCase, '-' and '_' split - or two adjacent tokens joined: "top-seller", "best-seller")
PROMO_WORDS12 = frozenset("""
ad ads adslot adslots adunit adunits adbox adbanner advert adverts advertorial advertorials advertisement advertisements
advertising advertiser anzeige anzeigen werbung werbeanzeige werbeanzeigen werbeblock sponsor sponsors sponsored
sponsoring sponsorship sponsorlink sponsorlinks gesponsert gesponserte sponsorise sponsorises sponsorisee sponsorisees
sponsorizzato sponsorizzati sponsorizzata patrocinado patrocinados patrocinada patrocinio publicite publicites
publicidad pubblicita reklame reklam reklamer annonce annonces advertentie advertenties gesponsord gesponsorde
sponsrad sponsrade sponset sponsorowane reklama reklamy promo promos promoted promotion promotions promotional
recommendation recommendations recommended recommender recommends empfehlung empfehlungen recommandation
recommandations recommandes recomendacion recomendaciones recomendados recomendado raccomandazioni raccomandati
consigliati consigliato aanbevolen aanbeveling aanbevelingen rekommenderade rekommendationer anbefalet anbefalinger
anbefalte anbefalt polecane polecamy rekomendacje rekomendowane bestseller bestsellers topseller topsellers
crosssell crossselling upsell upselling trending
""".split())
_SPLIT12 = re.compile(r"[^a-z0-9]+")
_CAMEL12 = re.compile(r"([a-z])([A-Z])")


def promo_tokens12(n: Node) -> List[str]:
    """Version 12: a container's class / id tokens (camelCase, '-', '_' split, lower case) and every two adjacent
    tokens joined."""
    raw = (n.attrs.get("class") or "") + " " + (n.attrs.get("id") or "")
    t = [x for x in _SPLIT12.split(_CAMEL12.sub(r"\1 \2", raw).lower()) if x]
    return t + [t[i] + t[i + 1] for i in range(len(t) - 1)]


def promo_marked12(base: Node, bases: Sequence[Node] = ()) -> Optional[Node]:
    """The nearest container above ``base`` (its parent up to, not including, ``body``) whose class / id names a
    promotion, an advertisement or a recommendation (``PROMO_WORDS12``) and that holds a BLOCK of cards - at least two
    of ``bases`` (a card's own tile with a sale badge, ``promo-tag``, is no block); None."""
    x = base.parent
    while x is not None and not x.tag.startswith("#") and x.tag not in ("body", "html"):
        if any(t in PROMO_WORDS12 for t in promo_tokens12(x)) and sum(1 for d in bases if contains(x, d)) >= 2:
            return x
        x = x.parent
    return None


def promo_flags12(bases: Sequence[Node], chrome: Sequence[bool] = ()) -> List[int]:
    """Version 12: per card (its node; a title spec's: its tile), 1 when it sits in a promotion / advertising /
    recommendation BLOCK (a container holding at least two cards, ``promo_marked12``) that does NOT hold every card the
    structure reads outside the page chrome (``chrome``: per card, in the header / navigation / footer) - a block
    beside the results (cloned cards included), never the results' own container, never a card's own badge; else 0."""
    keep = [b for k, b in enumerate(bases) if not (k < len(chrome) and chrome[k])]
    out = []
    for b in bases:
        m = promo_marked12(b, bases)
        out.append(1 if m is not None and not all(contains(m, d) for d in keep) else 0)
    return out


_PROMO_JS12 = (
    "  const PW12 = new Set(" + json.dumps(sorted(PROMO_WORDS12)) + ");\n"
    "  const tile12 = a => { for (let x = a.parentElement; x && x !== document.body && x !== document.documentElement; "
    "x = x.parentElement) { const t = x.tagName.toLowerCase(); if (t === 'li' || t === 'article' || "
    "/card|product|item|tile|result/i.test(x.getAttribute('class') || '')) return x; } return a; };\n"
    "  const toks12 = n => { const t = ((n.getAttribute('class') || '') + ' ' + (n.getAttribute('id') || ''))"
    ".replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean); const o = t.slice(); "
    "for (let i = 0; i + 1 < t.length; i++) o.push(t[i] + t[i + 1]); return o; };\n"
    "  const bases12 = cards.map(p => specs[p[1]].kind === 'title' ? tile12(p[0]) : p[0]);\n"
    "  const mark12 = b => { for (let x = b.parentElement; x && x !== document.body && x !== document.documentElement; "
    "x = x.parentElement) { if (toks12(x).some(w => PW12.has(w)) && bases12.filter(d => x.contains(d)).length >= 2) "
    "return x; } return null; };\n"
    "  const chrome12 = cards.map(p => ['header', 'nav', 'footer'].indexOf(areaOf(p[0])) >= 0);\n"
    "  const promo12 = bases12.map(b => { const m = mark12(b); "
    "return !!m && !bases12.every((d, i) => chrome12[i] || m.contains(d)); });\n")
_ROWS_V6 = "  const rows = cards.map((p, j) => { let pos = first.has(j) ? first.get(j) : -1;"
_ROW_TAIL_V10 = "txt(specs[p[1]].kind === 'title' ? tile(p[0]) : p[0], 1200)]; });"
#: version 12: the version-11 card read plus each card's PROMO flag as the 9th value of its row (``promo_flags12``;
#: ``demo_cards.cards_map_v12`` is the same reading on a recorded DOM)
CARDS_JS_V12 = CARDS_JS_V11.replace(MARK11, MARK12, 1).replace(_ROWS_V6, _PROMO_JS12 + _ROWS_V6, 1).replace(
    _ROW_TAIL_V10, "txt(specs[p[1]].kind === 'title' ? tile(p[0]) : p[0], 1200), promo12[j] ? 1 : 0]; });", 1)
assert MARK12 in CARDS_JS_V12 and CARDS_JS_V12.count("promo12[j]") == 1 and CARDS_JS_V12.count("const PW12") == 1


def cards_js_v12(specs: Sequence[Dict[str, Any]]) -> str:
    from anyuse.execution.web_arch_run import CONTROL_SEL
    return CARDS_JS_V12 % (json.dumps(CONTROL_SEL), json.dumps(list(specs), ensure_ascii=True))


def specs_of_js_v12(expr: str) -> Optional[List[Dict[str, Any]]]:
    """The specs a ``cards_js_v12`` expression carries (for fake pages); None when it is not one."""
    s = str(expr or "").strip()
    if not s.startswith(MARK12) or not s.endswith(")"):
        return None
    try:
        return json.loads(s[s.rfind("})(") + 3:-1])
    except ValueError:
        return None


def _card_nodes12(root: Node, specs: Sequence[Dict[str, Any]]) -> List[Node]:
    """The cards ``cards_map_v10`` reads, in its order (the same rule: the nodes whose chain is a non-title spec's, and
    every link's card)."""
    want = [list(s.get("chain") or []) for s in specs]
    order = {id(n): k for k, n in enumerate(walk(root))}
    cand: List[Node] = []
    seen = set()
    for x in walk(root):
        if x.tag in ("html", "body"):
            continue
        c = chain_of(x)
        if any(s.get("kind") != "title" and chain_matches(c, s) for s in specs) and id(x) not in seen:
            cand.append(x)
            seen.add(id(x))
    for a in control_nodes(root):
        if a.tag == "a" and "href" in a.attrs:
            hit = _card_hit(a, specs)
            if hit is not None and id(hit[1]) not in seen:
                cand.append(hit[1])
                seen.add(id(hit[1]))
    cand.sort(key=lambda n: order.get(id(n), 0))
    return cand


def cards_map_v12(root: Node, specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V12`` on a parsed document: ``cards_map_v11`` with each card row's promo flag (``promo_flags12``)."""
    with bem_free():
        got = cards_map_v10(root, specs)
        nodes = _card_nodes12(root, specs)
    kinds = {}
    for row in got["cards"]:
        kinds[int(row[0])] = specs[int(row[1])].get("kind")
    bases = [(_tile_of(n) if kinds.get(j) == "title" else n) for j, n in enumerate(nodes)]
    flags = promo_flags12(bases, [area_of(n) in CHROME_AREAS for n in nodes])
    for row in got["cards"]:
        j = int(row[0])
        row.append(flags[j] if j < len(flags) else 0)
    return got


def recorded_cards_v12(page: Dict[str, Any], specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``CARDS_JS_V12``'s answer for a RECORDED page (``recorded_cards_v6`` with ``cards_map_v12``)."""
    return recorded_cards_v6(page, specs, _mapper=cards_map_v12)


def cards_js_v8(specs: Sequence[Dict[str, Any]]) -> str:
    from anyuse.execution.web_arch_run import CONTROL_SEL
    return CARDS_JS_V8 % (json.dumps(CONTROL_SEL), json.dumps(list(specs), ensure_ascii=True))


def specs_of_js_v8(expr: str) -> Optional[List[Dict[str, Any]]]:
    """The specs a ``cards_js_v8`` expression carries (for fake pages); None when it is not one."""
    s = str(expr or "").strip()
    if not s.startswith(MARK8) or not s.endswith(")"):
        return None
    try:
        return json.loads(s[s.rfind("})(") + 3:-1])
    except ValueError:
        return None


def _path_segments(u: str) -> List[str]:
    try:
        return [s for s in urllib.parse.urlsplit(u).path.split("/") if s]
    except ValueError:
        return []


def section_link(u: str, others: Sequence[str]) -> bool:
    """Is ``u`` a SECTION of another link of the card - its path a strict prefix of the other's (a
    category / tag page: ``/actualite/insolite`` over ``/actualite/insolite/une-honda-...html``)?"""
    a = _path_segments(u)
    return bool(a) and any(len(b) > len(a) and b[:len(a)] == a for b in (_path_segments(o) for o in others))


def tile_texts11(elements: Sequence[Dict[str, Any]], prim: Dict[str, Any], reach: int = 12) -> List[str]:
    """Version 11: the texts of the links to ``prim``'s URL next to it in the reader's order - its tile (a tile
    links its image, its name, its rating and a quick view to the same URL): outwards from ``prim`` until a link to
    another URL (at most ``reach`` controls each way)."""
    from anyuse.execution import demo_compile as C
    k = C._norm(str(prim.get("href") or ""))
    pos = next((j for j, e in enumerate(elements) if e is prim), None)
    if pos is None or not k:
        return []
    out: List[str] = []
    for step in (-1, 1):
        j = pos + step
        while 0 <= j < len(elements) and abs(j - pos) <= reach:
            e = elements[j]
            if e.get("tag") == "a" and e.get("href"):
                if C._norm(str(e.get("href"))) != k:
                    break
                out.append(" ".join(str(e.get("text") or "").split()))
            j += step
    return out


def page_dates15(dom: str) -> set:
    """Version 15: the texts (folded) the page marks as dates by its own markup - each ``<time>`` element's text and
    each element whose ``itemprop`` is a schema.org date (``datePublished``, ``dateModified``, ``dateCreated``,
    ``startDate``, ``endDate``)."""
    from anyuse.execution import web_arch as W
    if not dom:
        return set()
    root, _base = parse(dom)
    props = {"datepublished", "datemodified", "datecreated", "startdate", "enddate"}
    out = set()
    for n in walk(root):
        if n.tag == "time" or props & set(str(n.attrs.get("itemprop") or "").lower().split()):
            t = W.fold(text_of(n, 200))
            if t:
                out.add(t)
    return out


def read_region_v6(elements: Sequence[Dict[str, Any]], cmap: Dict[str, Any], region: Dict[str, Any], site: str,
                   exclude: Sequence[str] = (), scope: Optional[Dict[str, Any]] = None,
                   names: Optional[Dict[str, str]] = None, value: str = "",
                   dates: Optional[set] = None) -> Dict[str, Any]:
    """The results of a version-6 card region, in page order, and what could NOT be read
    (``{"items": [...], "incomplete": [...], "promo": [...], "scope_why": ""}``; each item ``{title, url, i, ctx, el,
    card, sub_links, pos}``). ``scope``: the tool's RESULT SCOPE (``demo_compile.result_scope``) - a card
    whose link leaves it or carries a promotional marker is a typed ``promo`` item (``scope_items``), never a result;
    ``scope_why`` names why the whole list is uncertain (most cards outside the scope, or no scope).

    Every card of the page (``cards_map_v6``'s ``cards``) outside the page chrome and visible is ONE result:
    its link at a compiled title path (whatever its URL pattern), else its link of the demonstrated title
    ROLE that is not a section of another of its links (``role_of``). A category / tag / author link - a link
    of another role - is never a result: a card with links but none in the title role is reported as
    incomplete (``{"why", "pos", "title", "urls"}``), never answered. A link of the compiled result
    pattern inside the results container that no compiled card holds (``orphans``: a card of a layout
    the demonstration did not show) is reported the same way - unless its link's chain is one the
    demonstration's own page showed among its reported results without reporting it (``known_other``,
    ``demo_compile.compile_cards``: a layout the demonstrator saw and did not count). A card whose title
    link leaves the site, or a card with no link but a title, is a result with NO link (``url: ""``,
    ``link: "off-site" | "none"``), never dropped.

    A version-15 region (``cards_version`` 15) reads the page's own evidence first: a title-role text the page gives
    several targets and that names nothing of the input ``value`` is a label, never a title, and so is a text the page
    marks as a date (``dates``, ``page_dates15``); a leading or trailing run of words every title shares that names
    nothing of the input and that a colon, a bar or an arrow sets off is a UI affix (``ui_affixes15``); then the
    version-11 and version-12 word rules."""
    from anyuse.execution import demo_compile as C
    from anyuse.execution import web_arch as W
    m = (cmap or {}).get("map") or {}
    ex = {C._norm(u) for u in exclude}
    v8r = int(region.get("cards_version") or 0) >= 8
    v10r = int(region.get("cards_version") or 0) >= 10
    v11r = int(region.get("cards_version") or 0) >= 11
    v12r = int(region.get("cards_version") or 0) >= 12
    v15r = int(region.get("cards_version") or 0) >= 15
    page_labels = (W.link_labels(elements) or set()) if v15r else set()

    def label15(t: str) -> bool:
        """Version 15: a text that is no title by the page's own evidence (a label only where the input is known)."""
        f = W.fold(t)
        return bool(f) and ((bool(value) and f in page_labels and not C.name_share(t, value)) or f in (dates or ()))
    links: Dict[int, List[Tuple[Dict[str, Any], List[int]]]] = {}
    for e in elements:
        if e.get("tag") != "a" or not e.get("href"):
            continue
        hit = m.get(str(e.get("i")))
        if hit:
            links.setdefault(int(hit[1]), []).append((e, [int(v) for v in hit[:5]]))

    def vis_of(e: Dict[str, Any]) -> Optional[str]:
        """Version 8: the link's VISIBLE text (the 6th value of its map entry), None when the read has none."""
        hit = m.get(str(e.get("i")))
        return str(hit[5]) if hit and len(hit) > 5 and isinstance(hit[5], str) else None

    # version 15 (``web_arch.site_evidence``): a text the page gives one target is that target's title, whatever
    # word it holds; a text it gives several targets is a label and keeps the word rule
    labels = W.link_labels(elements) if W.site_evidence_first() else None

    def usable(e: Dict[str, Any]) -> bool:
        href = str(e.get("href") or "")
        # a result title that MENTIONS a gated word ("Haus kaufen mit alter Heizung") is no control
        return bool(href) and not W.asset_href(href) and not W.forbidden_link(e.get("text"), href, labels) and \
            C.same_site(href, site) and C._norm(href) not in ex

    def offsite(e: Dict[str, Any]) -> bool:
        href = str(e.get("href") or "")
        return href.lower().startswith(("http://", "https://")) and not C.same_site(href, site) and \
            not W.asset_href(href)

    # (a card's category link, /news/category/<id>, at a
    # compiled title path before the article's own link, /news/<id>/<slug>): of a card's links at a title path, the
    # one of the cards' majority URL SHAPE (``url_identity.majority_shape``) is its result link
    from anyuse.execution import url_identity as UI
    shape = UI.majority_shape([str(e.get("href")) for els in links.values() for e, h in els
                               if (h[2] or (len(h) > 4 and h[4])) and usable(e)])
    items: List[Dict[str, Any]] = []
    incomplete: List[Dict[str, Any]] = []
    linkless: List[Dict[str, Any]] = []
    for row in (cmap or {}).get("cards") or []:
        j, _si, pos, nlink, text = int(row[0]), int(row[1]), int(row[2]), int(row[3]), str(row[4] or "")
        area, visible = (str(row[5]) if len(row) > 5 else ""), (int(row[6]) if len(row) > 6 else 1)
        els = links.get(j) or []
        # page chrome; a card with links is shown when the reader saw one of them (READ_JS lists visible
        # controls only), a card without links by its own box (a card of floated columns has a zero-height
        # box: its box never hides a card whose links are seen)
        if area in CHROME_AREAS or (nlink and not els) or (not nlink and not visible):
            continue
        if v12r and len(row) > 8 and int(row[8] or 0):
            # version 12: a card in a promotion / advertising / recommendation block beside the results is never
            # a result (``promo_flags12``)
            continue
        if not els:
            # web-watch: a result card without any link - a result with no link (its title only)
            # (a section heading "Get smarter" must not be answered as a row): only when the compiled
            # card spec says the site's results have no links (``linkless_results``, measured on the build page) -
            # otherwise a linkless card is a section or teaser heading, never a row (reported, not answered)
            if nlink == 0 and len(text.split()) >= 2:
                if region.get("linkless_results"):
                    items.append({"title": text, "url": "", "i": None, "ctx": "", "el": None, "card": j, "pos": pos,
                                  "link": "none", "sub_links": 0})
                else:
                    linkless.append({"title": text[:160], "pos": pos})
            continue
        urls = [str(e.get("href")) for e, _h in els]
        prim = next((e for e, h in els if h[2] and usable(e) and UI.url_shape(str(e.get("href"))) == shape), None) \
            if shape else None
        prim = prim or next((e for e, h in els if h[2] and usable(e)), None)
        if prim is None:
            prim = next((e for e, h in els if len(h) > 4 and h[4] and usable(e) and
                         not section_link(str(e.get("href")), urls)), None)
        if prim is None:
            # web-watch: the card's title link leaves the site - a result with no same-site link
            off = next((e for e, h in els if (h[2] or (len(h) > 4 and h[4])) and offsite(e)), None)
            if off is not None:
                texts = [" ".join(str(off.get("text") or "").split())]
                items.append({"title": C.item_title(texts) or texts[0] or text, "url": "", "i": off.get("i"),
                              "ctx": "", "el": None, "card": j, "pos": pos, "link": "off-site",
                              "off_site_host": C.host_of(off.get("href")), "sub_links": 0})
                continue
            incomplete.append({"why": "a result card with no link in the demonstrated title role", "pos": pos,
                               "title": text[:160], "urls": [C.strip_fragment(u) for u in urls][:4]})
            continue
        k = C._norm(str(prim.get("href")))
        same = [e for e, _h in els if C._norm(str(e.get("href"))) == k]
        texts = [" ".join(str(e.get("text") or "").split()) for e in same]
        title = ""
        if v8r and vis_of(prim) is not None:
            # version 8: the title link's VISIBLE text without UI affixes; a title link that shows none (an image
            # link): the visible texts of the card's other links to the result (never an aria-label / title / alt)
            title = C.clean_title(vis_of(prim) or "") or \
                C.item_title([C.clean_title(vis_of(e) or "") for e in same if e is not prim])
            if v11r and (C.no_title11(title) or (v15r and label15(title))):
                # version 11 (a quick-view or detail link, a date badge "Sep. 20 2026" in the title role): the card's
                # other links to the result name it (their visible texts), else the links to the result next to the
                # title link in the page's order (the same tile); version 15: a label or date by the page's evidence
                alt = C.item_title([C.clean_title(vis_of(e) or "") for e in same if e is not prim
                                    if not (v15r and label15(vis_of(e) or ""))]) or \
                    C.item_title([C.clean_title(t) for t in tile_texts11(elements, prim)
                                  if not (v15r and label15(t))])
                if alt and not C.no_title11(alt):
                    title = alt
        title = title or C.item_title(texts) or " ".join(str(prim.get("text") or "").split()) or max(texts, key=len)
        # (version 15: a heading that is a label or a date by the page's evidence, or no title by version 11's
        # lists, names nothing)
        named = next(((names or {}).get(str(e.get("i"))) for e in [prim] + same if (names or {}).get(str(e.get("i")))
                      and not (v15r and (label15(str((names or {}).get(str(e.get("i")))))
                                         or C.no_title11((names or {}).get(str(e.get("i"))))))), "")
        if named:
            # the link wraps the card - its heading / name element is the title (``link_name``)
            title = named
        ctx = next((" ".join(str(e.get("ctx") or "").split()) for e in [prim] + same if e.get("ctx")), "")
        title = title or ctx[:160] or text
        if not title:
            continue
        # how often the card itself links its result with ONE text (an image link and a title link):
        # those repeats are the card's own, never evidence of a category (``demo_compile.drop_categories``)
        folded = [" ".join(t.lower().split()) for t in texts if t.strip()]
        own_repeat = max((folded.count(t) for t in set(folded)), default=1)
        items.append({"title": title, "url": C.strip_fragment(str(prim.get("href"))), "i": prim.get("i"), "ctx": ctx,
                      "el": prim, "card": j, "pos": pos, "link": "site",
                      "sub_links": len({C._norm(str(e.get("href"))) for e, _h in els}) - 1, "own_repeat": own_repeat})
        if v10r:
            # version 10: the card's whole visible text (the prices / dates a sorted list is checked on)
            items[-1]["card_text"] = str(row[7]) if len(row) > 7 and isinstance(row[7], str) else ""
    # web-watch: a link of the compiled result pattern inside the results container that no compiled
    # card holds is a result card of a layout the demonstration did not show - reported, never skipped
    rx = re.compile(region.get("regex") or r"^$", re.I)
    by_i = {str(e.get("i")): e for e in elements if e.get("tag") == "a" and e.get("href")}
    known = set(region.get("known_other") or [])
    # (each post's date link "30. September 2022" in its entry header):
    # an orphan link to a target a card already read is a second link of that result (a date, a meta line), never a
    # result card of another layout
    seen = {C._norm(str(it.get("url") or "")) for it in items if it.get("url")}
    for i, ch in (cmap or {}).get("orphans") or []:
        e = by_i.get(str(i))
        if e is None or e.get("area") in CHROME_AREAS or not usable(e) or ch in known:
            continue
        href = C.strip_fragment(str(e.get("href")))
        if not (rx.match(href) or rx.match(str(e.get("href")))) or C._norm(href) in seen:
            continue
        seen.add(C._norm(href))
        incomplete.append({"why": "a result card of a layout the demonstration did not show", "pos": int(i),
                           "title": " ".join(str(e.get("text") or e.get("ctx") or "").split())[:160], "urls": [href],
                           "chain": ch})
    incomplete.sort(key=lambda u: u["pos"])
    if v15r and value:
        # version 15: a UI affix every title shares (the list's own differential, where the input is known)
        lead, tail = C.ui_affixes15([str(it.get("title") or "") for it in items if it.get("link") == "site"], value)
        if lead or tail:
            for it in items:
                if it.get("link") == "site":
                    it["title"] = C.strip_affixes15(str(it.get("title") or ""), lead, tail)
    sc = scope_items(items, scope, v15=v15r)
    return {"items": sc["items"], "incomplete": incomplete, "promo": sc["promo"], "scope_why": sc["uncertain"],
            "linkless": linkless}


# ---------------------------------------------------------------------------
# A result card belongs to the tool's RESULT SCOPE - the hosts its recorded
# results link to, stored by the compile (``demo_compile.result_scope``). A card whose link leaves that scope
# (another host or subdomain) or carries a promotional marker is a typed ``promo`` item, never an ``ok`` result row.
# ---------------------------------------------------------------------------

#: a link relation that marks a paid or untrusted placement
PROMO_REL = frozenset(("nofollow", "sponsored"))
#: a tracking / placement query parameter of a link ...
PROMO_PARAM = re.compile(r"^(?:ref|refer|referrer|source|aff|affid|affiliate|partner|partnerid|placement|promo|sponsor|"
                         r"utm_medium|utm_source|utm_campaign|utm_content)$", re.I)
#: ... whose value names a promotional placement (split at non-alphanumerics; whole tokens)
PROMO_VALUE_TOKENS = frozenset("""
promo promos promotion promotional sponsor sponsored sponsoring ad ads advert adverts advertising affiliate affiliates
partner partners banner banners direct deal deals paid cpc cpm display teaser widget anzeige werbung
""".split())


def promo_marker(el: Optional[Dict[str, Any]], url: str) -> str:
    """Why a result link is a promotional placement, or ``""``: its ``rel`` (``PROMO_REL``), or a tracking
    parameter whose value names a placement (``PROMO_PARAM`` / ``PROMO_VALUE_TOKENS``:
    ``ref=search_results_direct_name``). Reads only the link element the page reader gave (its ``rel``) and the URL."""
    rel = set(str((el or {}).get("rel") or "").lower().split())
    hit = rel & PROMO_REL
    if hit:
        return "rel=" + ",".join(sorted(hit))
    try:
        q = urllib.parse.urlsplit(str(url or "")).query
    except ValueError:
        return ""
    for k, v in urllib.parse.parse_qsl(q, keep_blank_values=True):
        if PROMO_PARAM.match(k):
            toks = {t for t in re.split(r"[^a-z0-9]+", v.lower()) if t}
            if toks & PROMO_VALUE_TOKENS:
                return f"{k}={v[:60]}"
    return ""


def param_markers15(linked: Sequence[Dict[str, Any]]) -> Dict[int, str]:
    """Version 15: a placement marker read in the list's own links - a tracking / placement parameter
    (``PROMO_PARAM``) that one card's link alone carries among three or more linked cards, or with a value no other
    card's link carries while two or more of them share another one. ``{id(item): why}``."""
    vals: Dict[str, Dict[str, int]] = {}
    per: Dict[int, List[Tuple[str, str]]] = {}
    for it in linked:
        try:
            q = urllib.parse.parse_qsl(urllib.parse.urlsplit(str(it.get("url") or "")).query, keep_blank_values=True)
        except ValueError:
            q = []
        mine = [(k.lower(), v) for k, v in q if PROMO_PARAM.match(k)]
        per[id(it)] = mine
        for k, v in mine:
            vals.setdefault(k, {})
            vals[k][v] = vals[k].get(v, 0) + 1
    out: Dict[int, str] = {}
    for it in linked:
        for k, v in per[id(it)]:
            carriers = sum(vals[k].values())
            if carriers == 1 and len(linked) >= 3:
                out[id(it)] = f"only this result link carries {k}={v[:60]}"
                break
            if carriers >= 3 and vals[k][v] == 1 and max(vals[k].values()) >= 2:
                out[id(it)] = f"{k}={v[:60]}, where the other result links carry another {k}"
                break
    return out


def scope_items(items: Sequence[Dict[str, Any]], scope: Optional[Dict[str, Any]], v15: bool = False) -> Dict[str, Any]:
    """The RESULT SCOPE applied to a card read: ``{"items": the in-scope results, "promo": the typed out-of-scope
    cards, "uncertain": why the whole list is uncertain or ""}``.

    * ``scope`` None: the caller has no tool (a compile-time self-check): everything unchanged.
    * a card whose link's HOST is not one of the scope's hosts (the exact host, not the registrable domain: a sibling
      subdomain the recorded results never used is outside) is a ``promo`` item (``link: "promo"``, its URL kept as
      ``promo_url``, ``promo_why``), never a result;
    * a card whose link carries a promotional marker (``promo_marker``) is a ``promo`` item - unless the marker is the
      page's norm (more than half of the page's linked cards carry one: a site that marks every result link
      ``nofollow`` is not advertising);
    * unknown is uncertain: when half or more of the linked cards link outside the scope, the recorded scope does not
      describe this page (the results moved, or the structure reads something else) - ``uncertain`` names it and the
      caller never answers ``ok``; a scope with no host is unknown as well.
    Items without a same-site link (``link`` ``none`` / ``off-site``) pass through unchanged. ``v15`` (version 15):
    the list's own links mark a placement first (``rel``, then ``param_markers15``), the placement words of
    ``PROMO_VALUE_TOKENS`` last."""
    out = {"items": list(items), "promo": [], "uncertain": ""}
    if scope is None:
        return out
    hosts = {str(h).lower() for h in (scope.get("hosts") or []) if h}
    if not hosts:
        out["uncertain"] = "the tool declares no result scope (no recorded result host)"
        return out
    linked = [it for it in items if it.get("url") and it.get("link") in (None, "site")]
    if not linked:
        return out

    def host(u: str) -> str:
        try:
            return (urllib.parse.urlsplit(str(u or "")).hostname or "").lower()
        except ValueError:
            return ""
    markers = {id(it): promo_marker(it.get("el"), str(it.get("url"))) for it in linked}
    if v15:
        own = param_markers15(linked)
        for it in linked:
            rel = set(str((it.get("el") or {}).get("rel") or "").lower().split()) & PROMO_REL
            if not rel and own.get(id(it)):
                markers[id(it)] = own[id(it)]
    marked = sum(1 for it in linked if markers[id(it)])
    norm_marker = marked * 2 > len(linked)
    off = [it for it in linked if host(it["url"]) not in hosts]
    linked_ids = {id(it) for it in linked}
    keep: List[Dict[str, Any]] = []
    promo: List[Dict[str, Any]] = []
    for it in items:
        if id(it) not in linked_ids:
            keep.append(it)
            continue
        h = host(it["url"])
        why = ""
        if h not in hosts:
            why = f"the link leaves the result scope ({h} is none of {', '.join(sorted(hosts))})"
        elif markers[id(it)] and not norm_marker:
            why = f"a promotional marker on the link ({markers[id(it)]})"
        if why:
            promo.append(dict(it, link="promo", promo_url=it["url"], url="", promo_why=why, host=h))
        else:
            keep.append(it)
    out.update(items=keep, promo=promo)
    if off and len(off) * 2 >= len(linked):
        out["uncertain"] = (f"{len(off)} of {len(linked)} result cards link outside the tool's result scope "
                            f"({', '.join(sorted(hosts))}): the recorded scope does not describe this page")
    return out
