"""An OFFLINE page over recorded DOMs: the duck-typed page ``demo_tool.run_tool``
drives (``goto``, ``js``, ``dom``, ``type_search``, ``search_inputs``, ``network``, ``url``, ``settle``), answered from a
SITE of recorded pages - never a browser, never a request. The repair ladder's tests and the drift simulator replay a
compiled tool on it, on a saved render and on its perturbed copies.

The page readers are emulated on the parsed document (``demo_cards``' parser):

* ``web_arch_run.READ_JS``: the numbered controls (``CONTROL_SEL``'s order) with their labels, areas, links, the card
  context of a link, a drop-down's options, a checkbox's state; the headings, title, JSON-LD, canonical link; the body's
  and the main region's text. A recorded page's own reader TEXTS (``text``, ``main_text``, ...) are kept when the page
  carries them (``Recorded``): the emulated text cannot know what a style sheet hides. Visibility: an element under a
  ``hidden`` attribute, an inline ``display: none`` / ``visibility: hidden`` or a ``data-au-vis="0"`` mark (the
  recorded reader's own visibility, ``mark_visibility``) is not listed;
* ``demo_tool.LINK_SIG_JS``, ``PRICED_LINKS_JS``, ``PAGE_EXTRA_JS``; the cards / ``_V6`` / ``_V8`` / ``TEXT_JS_V9`` / tile
  reads through ``demo_cards.recorded_answer``;
* a click / a drop-down choice (``CLICK_JS`` / ``SELECT_JS``): a link is followed, a drop-down inside a GET form submits
  the form, an option whose value is a URL is followed;
* ``search_inputs`` / ``text_inputs`` / ``type_search``: ``cdp_guard.SEARCH_INPUTS_JS``'s inputs (the page's structure
  first, the names as whole words or word starts last) and the live replay's choice of the input
  (``cdp_guard.replay_inputs`` / ``recorded_input`` for a recorded ``want``: name / id / index; without one, the
  structure, then a GET form that submits the typed value, then the names), then the site's ``search`` hook (by
  default the GET form around the input).

PURE apart from the async page methods; nothing here touches the network.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_cards as K
from anyuse.execution import demo_compile as C
from anyuse.execution import web_arch as W

#: a document this module CHANGES is always parsed afresh (a caller may cache ``demo_cards.parse`` for its reads)
def _fresh_parse(html_text: str):
    """``demo_cards.parse`` done here (never a cached tree another reader shares)."""
    t = K._Tree()
    try:
        t.feed(html_text or "")
        t.close()
    except Exception:                                  # noqa: BLE001 - a partial tree is still a tree
        pass
    return t.root, t.base

#: the reader texts a recorded page may carry (kept instead of the emulated ones)
RECORDED_KEYS = ("title", "h1", "headings", "breadcrumb", "text", "main_text", "jsonld", "canonical", "og_type",
                 "md_products", "meta")
_HIDE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important)?\s*(?:;|$)",
                   re.I)
_CARDISH = re.compile(r"card|product|item|tile|result")
_PRICE_RX = re.compile(r"(\d[\d.,\s ]*\s?(€|eur|kr|sek|nok|dkk|zł|zl|pln|chf|£|\$|:-|,-))|((€|£|\$|chf)\s?\d)", re.I)
_META_KEYS = re.compile(r"^(og:title|og:type|og:url|og:price:amount|og:price:currency|product:price:amount|"
                        r"product:price:currency|product:availability|og:availability)$")


# ---------------------------------------------------------------------------
# the document: serialize, visibility
# ---------------------------------------------------------------------------

def serialize(n: K.Node) -> str:
    """``n``'s children as HTML (``demo_cards.parse`` reads it back to the same tree)."""
    out: List[str] = []
    _ser(n, out)
    return "".join(out)


def _ser(n: K.Node, out: List[str]) -> None:
    for c in n.children:
        if isinstance(c, str):
            out.append(c if n.tag in K.RAW else html.escape(c, quote=False))
            continue
        attrs = "".join(f' {k}="{html.escape(str(v), quote=True)}"' for k, v in c.attrs.items())
        out.append(f"<{c.tag}{attrs}>")
        if c.tag in K.VOID:
            continue
        _ser(c, out)
        out.append(f"</{c.tag}>")


def hidden(n: K.Node) -> bool:
    """Not rendered: the node or an ancestor has ``hidden``, an inline ``display: none`` / ``visibility: hidden``, is
    an ``<input type=hidden>``, or the node carries the recorded reader's ``data-au-vis="0"`` mark."""
    if n.attrs.get("data-au-vis") == "0" or (n.tag == "input" and (n.attrs.get("type") or "").lower() == "hidden"):
        return True
    x: Optional[K.Node] = n
    while x is not None and not x.tag.startswith("#"):
        if "hidden" in x.attrs or _HIDE.search(x.attrs.get("style") or "") or x.tag in ("template", "head"):
            return True
        x = x.parent
    return False


def mark_visibility(dom: str, page: Dict[str, Any], url: str = "") -> Tuple[str, float]:
    """The recorded reader's own VISIBILITY carried into the DOM: every numbered control (``CONTROL_SEL``'s order) gets
    ``data-au-vis="1"`` when the recorded reader listed it (``elements[].i``), else ``"0"``. Done only when the recorded
    links agree with the parsed controls (>= 90 % of them by URL, ``demo_cards.recorded_cards``' test); returns the
    marked DOM and the agreement (the DOM unchanged below 0.9)."""
    root, base = _fresh_parse(dom)
    url = url or str(page.get("url") or "")
    ctl = K.control_nodes(root)
    rec = [e for e in page.get("elements") or [] if isinstance(e.get("i"), int)]
    links = [e for e in rec if e.get("tag") == "a" and e.get("href")]
    agree = sum(1 for e in links if 0 <= e["i"] < len(ctl) and ctl[e["i"]].tag == "a" and
                K.norm(K.href_of(ctl[e["i"]], url, base)) == K.norm(str(e.get("href"))))
    share = round(agree / len(links), 3) if links else 1.0
    if share < 0.9:
        return dom, share
    seen = {e["i"] for e in rec}
    for k, n in enumerate(ctl):
        n.attrs["data-au-vis"] = "1" if k in seen else "0"
    return serialize(root), share


# ---------------------------------------------------------------------------
# the readers on a parsed document
# ---------------------------------------------------------------------------

def _label(n: K.Node, root: K.Node) -> str:
    """READ_JS's ``label``."""
    tag = n.tag
    t = n.attrs.get("aria-label") or K.text_of(n, 400) or n.attrs.get("title") or \
        (n.attrs.get("value", "") if tag == "input" else "")
    if not t:
        img = next((x for x in K.walk(n) if x.tag == "img" and x.attrs.get("alt")), None)
        if img is not None:
            t = img.attrs.get("alt") or ""
    typ = (n.attrs.get("type") or "").lower()
    if (tag == "input" and typ in ("checkbox", "radio")) or tag == "select":
        lab = _label_for(n, root)
        if tag == "select":
            t = (K.text_of(lab, 400) if lab is not None else "") or n.attrs.get("aria-label") or n.attrs.get("name") or ""
        else:
            t = (K.text_of(lab, 400) if lab is not None else "") or n.attrs.get("aria-label") or n.attrs.get("value") or ""
    return " ".join(str(t).split())[:160]


def _label_for(n: K.Node, root: K.Node) -> Optional[K.Node]:
    x = n.parent
    while x is not None and not x.tag.startswith("#"):
        if x.tag == "label":
            return x
        x = x.parent
    i = n.attrs.get("id")
    if i:
        return next((x for x in K.walk(root) if x.tag == "label" and x.attrs.get("for") == i), None)
    return None


def _ctx(a: K.Node, label: str) -> str:
    x = a.parent
    while x is not None and not x.tag.startswith("#"):
        if x.tag in ("li", "article") or _CARDISH.search(x.attrs.get("class") or ""):
            break
        x = x.parent
    if x is None or x.tag.startswith("#") or x.tag in ("body", "html"):
        return ""
    ct = K.text_of(x, 1000)
    return ct[:200] if ct and len(ct) <= 400 and ct != " ".join(K.text_of(a, 400).split()) else ""


def _main(root: K.Node) -> Optional[K.Node]:
    for x in K.walk(root):
        cls = (x.attrs.get("class") or "").split()
        if x.tag == "main" or x.attrs.get("role") == "main" or x.attrs.get("id") in ("main", "content") or \
                "main-content" in cls:
            return x
    return None


def read_tree(root: K.Node, base: str, url: str, dom: str, recorded: Optional[Dict[str, Any]] = None,
              cap11: bool = False) -> Dict[str, Any]:
    """``READ_JS``'s answer on a parsed document (see the module doc); ``recorded``: the recorded reader texts to keep.
    ``cap11``: ``READ_JS_V11``'s answer (web tool version 11, fix 4) - the cap of 700 controls counted outside the header,
    navigation and footer (at most 3000)."""
    KK = K
    els: List[Dict[str, Any]] = []
    body_n = 0
    for k, n in enumerate(KK.control_nodes(root)):
        if (body_n >= 700 or len(els) >= 3000) if cap11 else len(els) >= 700:
            break
        if hidden(n):
            continue
        e: Dict[str, Any] = {"i": k, "tag": n.tag, "text": _label(n, root), "area": KK.area_of(n),
                             "role": n.attrs.get("role") or "", "type": (n.attrs.get("type") or "").lower(), "y": 100 + k}
        if n.tag == "a":
            e["href"] = KK.href_of(n, url, base)
            e["rel"] = (n.attrs.get("rel") or "").lower()
            c = _ctx(n, e["text"])
            if c:
                e["ctx"] = c
            if not K.text_of(n, 10).strip() and any(x.tag in ("img", "picture", "svg") for x in KK.walk(n)):
                e["img_only"] = True
        if n.tag == "select":
            e["options"] = [{"value": o.attrs.get("value", K.text_of(o, 60)), "text": K.text_of(o, 60)[:60],
                             "selected": "selected" in o.attrs} for o in KK.walk(n) if o.tag == "option"][:40]
        if n.tag == "input" and e["type"] in ("checkbox", "radio"):
            e["checked"] = "checked" in n.attrs
        els.append(e)
        if e["area"] not in ("header", "nav", "footer"):
            body_n += 1
    hs = [K.text_of(x, 300) for x in KK.walk(root) if x.tag in ("h1", "h2", "h3") and not hidden(x)]
    h1 = [K.text_of(x, 300) for x in KK.walk(root) if x.tag == "h1" and not hidden(x)]
    title = next((K.text_of(x, 300) for x in KK.walk(root) if x.tag == "title"), "")
    crumbs = [K.text_of(x, 300) for x in KK.walk(root)
              if "readcrumb" in (x.attrs.get("aria-label") or "") or
              {"breadcrumb", "breadcrumbs"} & set((x.attrs.get("class") or "").split())][:2]
    ld = ["".join(c for c in x.children if isinstance(c, str))[:200000] for x in K.walk(root) if x.tag == "script" and (x.attrs.get("type") or "").lower() == "application/ld+json"][:16]
    canon = next((x for x in KK.walk(root) if x.tag == "link" and "canonical" in (x.attrs.get("rel") or "").lower().split()),
                 None)
    og = next((x for x in KK.walk(root) if x.tag == "meta" and x.attrs.get("property") == "og:type"), None)
    body = next((x for x in KK.walk(root) if x.tag == "body"), root)
    main = _main(root) or body
    out = {"url": url, "title": title, "headings": [h for h in hs if h][:12], "h1": [h for h in h1 if h][:2],
           "breadcrumb": [c for c in crumbs if c], "elements": els,
           "text": K.sep_text_node(body, 20000), "main_text": K.sep_text_node(main, 20000), "jsonld": ld,
           "canonical": KK.href_of(canon, url, base) if canon is not None and canon.attrs.get("href") else "",
           "ready": "complete", "og_type": (og.attrs.get("content") or "") if og is not None else "",
           "md_products": sum(1 for x in KK.walk(root) if "schema.org/Product" in (x.attrs.get("itemtype") or "")),
           "meta": meta_of(root)}
    for k in RECORDED_KEYS:
        if recorded and k in recorded and recorded[k] is not None:
            out[k] = recorded[k]
    return out


def meta_of(root: K.Node) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for m in K.walk(root):
        if m.tag != "meta":
            continue
        k = (m.attrs.get("property") or m.attrs.get("name") or "").lower()
        if _META_KEYS.match(k) and k not in meta:
            meta[k] = (m.attrs.get("content") or "")[:300]
    return meta


def link_sigs(root: K.Node, base: str, url: str) -> Dict[str, List[str]]:
    """``demo_tool.LINK_SIG_JS`` on a parsed document."""
    out: Dict[str, List[str]] = {}

    def item_of(a: K.Node) -> Optional[K.Node]:
        n = a
        for _k in range(8):
            p = n.parent
            if p is None or p.tag.startswith("#"):
                return None
            d = K.desc(n)
            if sum(1 for c in p.elements() if c.tag == n.tag and K.desc(c) == d) >= 2:
                return n
            n = p
        return None
    for k, (a, h) in enumerate(K.anchors(root, url, base)):
        if k >= 3000:
            break
        h = str(h or "").split("#")[0]
        it = item_of(a)
        sig = ""
        if it is not None:
            p = it.parent
            g = p.parent if p is not None else None
            gg = g.parent if g is not None else None
            sig = ">".join([K.desc(gg), K.desc(g), K.desc(p), K.desc(it)])
        lst = out.setdefault(h, [])
        if sig not in lst and len(lst) < 4:
            lst.append(sig)
    return out


def priced_links(root: K.Node, base: str, url: str) -> List[str]:
    """``demo_tool.PRICED_LINKS_JS`` on a parsed document."""
    out = set()
    for k, (a, h) in enumerate(K.anchors(root, url, base)):
        if k >= 3000:
            break
        n: Optional[K.Node] = a
        for _j in range(7):
            if n is None or n.tag.startswith("#"):
                break
            t = K.text_of(n, 1000)
            if len(t) > 900:
                break
            if _PRICE_RX.search(t):
                out.add(str(h or "").split("#")[0])
                break
            n = n.parent
    return sorted(out)


def named_by(words: Sequence[str], text: Any) -> bool:
    """``agent_browser.SEARCH_TOKENS_JS``'s ``namedBy``: the text split where a lower-case letter meets an upper-case
    one and before the last capital of a run of capitals that a lower-case letter follows, lower-cased, cut at every
    character that is no letter or digit; does a word equal one of ``words`` or start with one?"""
    s = str(text or "")
    for width, split in ((2, lambda a: a[0].islower() and a[1].isupper()),
                         (3, lambda a: a[0].isupper() and a[1].isupper() and a[2].islower())):
        out, i = [], 0
        while i < len(s):
            if i + width <= len(s) and split(s[i:i + width]):
                out.append(s[i] + " " + s[i + 1:i + width])
                i += width
            else:
                out.append(s[i])
                i += 1
        s = "".join(out)
    return any(t and any(t == w or t.startswith(w) for w in words) for t in re.split(r"[\W_]+", s.lower()))


def text_inputs_of(root: K.Node) -> List[Dict[str, Any]]:
    """``cdp_guard.SEARCH_INPUTS_JS`` on a parsed document: every text input outside a form with a password field
    (``index``: among all ``input`` elements) with its ``rank`` - 0 by the page's structure (``type=search``,
    ``role=searchbox``, itself or an ancestor ``role=search`` or ``<search>``), 2 when its name is ``q`` / ``s`` or its
    name, id, placeholder, label or class names a search (whole words or word starts), 3 any other - and
    ``named14`` (the rule of web tools before version 15: a search word anywhere)."""
    from anyuse.execution.cdp_guard import SEARCH_INPUT_WORDS
    out = []
    ins = [x for x in K.walk(root) if x.tag == "input"]
    for i, el in enumerate(ins):
        t = (el.attrs.get("type") or "text").lower()
        if t not in ("text", "search", ""):
            continue
        f = form_of(el)
        if f is not None and any(x.tag == "input" and (x.attrs.get("type") or "").lower() == "password" for x in K.walk(f)):
            continue
        nm = (el.attrs.get("name") or "").lower()
        blob = " ".join(el.attrs.get(k) or "" for k in ("name", "id", "placeholder", "aria-label", "class"))
        landmark = landmark14 = False
        x: Optional[K.Node] = el
        while x is not None and not x.tag.startswith("#"):
            landmark14 = landmark14 or x.attrs.get("role") == "search"
            landmark = landmark or landmark14 or x.tag == "search"
            x = x.parent
        rank = 0 if (t == "search" or (el.attrs.get("role") or "").lower() == "searchbox" or landmark) else \
            2 if (nm in ("q", "s") or named_by(SEARCH_INPUT_WORDS, blob)) else 3
        named14 = t == "search" or landmark14 or bool(re.search(r"search|query|find|keyword", blob, re.I)) or \
            nm in ("q", "s")
        out.append({"index": i, "visible": not hidden(el) and "disabled" not in el.attrs and "readonly" not in el.attrs,
                    "name": el.attrs.get("name") or "", "id": el.attrs.get("id") or "", "type": t, "rank": rank,
                    "search": rank < 3, "named14": named14})
    return out


def search_inputs_of(root: K.Node, v15: bool = True) -> List[Dict[str, Any]]:
    """The search-like inputs of a parsed document (``GuardedSession.search_inputs``: ranks 0 and 2 of
    :func:`text_inputs_of`); ``v15`` False: the inputs the rule of web tools before version 15 named (``named14``)."""
    return [i for i in text_inputs_of(root) if (i["rank"] < 3 if v15 else i["named14"])]


def form_of(n: K.Node) -> Optional[K.Node]:
    x = n.parent
    while x is not None and not x.tag.startswith("#"):
        if x.tag == "form":
            return x
        x = x.parent
    return None


def form_url(form: K.Node, url: str, base: str, values: Dict[str, str]) -> Optional[str]:
    """The URL a GET ``form`` submits to with ``values`` for its named fields (its other text / hidden / checked fields
    with their own values); None for a POST form (never submitted)."""
    if (form.attrs.get("method") or "get").lower() != "get":
        return None
    act = form.attrs.get("action")
    target = urllib.parse.urljoin(urllib.parse.urljoin(url, base) if base else url, act) if act else url.split("?")[0]
    pairs: List[Tuple[str, str]] = []
    done = set()
    for x in K.walk(form):
        nm = x.attrs.get("name")
        if not nm or x.tag not in ("input", "select") or "disabled" in x.attrs:
            continue
        t = (x.attrs.get("type") or "text").lower()
        if nm in values:
            if nm not in done:
                pairs.append((nm, values[nm]))
                done.add(nm)
            continue
        if x.tag == "select":
            opt = next((o for o in K.walk(x) if o.tag == "option" and "selected" in o.attrs), None)
            if opt is not None:
                pairs.append((nm, opt.attrs.get("value", K.text_of(opt, 60))))
        elif t in ("checkbox", "radio"):
            if "checked" in x.attrs:
                pairs.append((nm, x.attrs.get("value", "on")))
        elif t in ("hidden", "text", "search"):
            pairs.append((nm, x.attrs.get("value", "")))
    sp = urllib.parse.urlsplit(target)
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, urllib.parse.urlencode(pairs), ""))


# ---------------------------------------------------------------------------
# the site and the page
# ---------------------------------------------------------------------------

class Recorded:
    """One page of a site: its DOM and, optionally, the recorded reader texts (``RECORDED_KEYS``) to keep."""

    def __init__(self, url: str, dom: str, recorded: Optional[Dict[str, Any]] = None, status: int = 200,
                 ax: Optional[Dict[str, Any]] = None):
        self.url, self.dom, self.recorded, self.status = url, dom, dict(recorded or {}), status
        #: web-ax-tree: the page's recorded accessibility-tree snapshot (``ax_tree.read_snapshot``), when one was taken
        self.ax = ax


NOT_FOUND = "<html><head><title>404 Not Found</title></head><body><main><h1>Not Found</h1>" \
            "<p>The requested URL was not found on this server.</p></main></body></html>"


class DomSite:
    """Recorded pages by URL (``demo_compile._norm``). ``route(url)`` -> ``(Recorded, final_url)``: the exact page,
    else the one page of the same path (another query: a tracking parameter), else a 404 page. ``search`` (overridable):
    where a typed search for ``value`` from ``page`` goes - the GET form around the input, else ``typed``'s recorded
    result URL. A subclass (the drift simulator) changes ``route`` / ``search`` to model the site's own changes."""

    def __init__(self, pages: Sequence[Recorded] = (), typed: Optional[Dict[str, str]] = None):
        self.pages: Dict[str, Recorded] = {}
        for p in pages:
            self.add(p)
        self.typed = dict(typed or {})
        self._reads: Dict[str, Dict[str, Any]] = {}

    def add(self, p: Recorded) -> None:
        self.pages[C._norm(p.url)] = p

    def route(self, url: str) -> Tuple[Recorded, str]:
        """The exact page; else, among the pages of the same path, the one sharing the most query parameters with
        ``url`` and contradicting none (a parameter both carry with different values, compared without case), when it
        is unique; else 404."""
        u = C._norm(url)
        if u in self.pages:
            return self.pages[u], url
        # a query value is compared without case (a site's search treats "Laufrad" and "laufrad" alike)
        want = {k: v.lower() for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(str(url)).query,
                                                               keep_blank_values=True)}
        best: List[Tuple[int, Recorded]] = []
        for k, p in self.pages.items():
            if k.split("?")[0] != u.split("?")[0]:
                continue
            have = {kk: vv.lower() for kk, vv in urllib.parse.parse_qsl(urllib.parse.urlsplit(p.url).query,
                                                                         keep_blank_values=True)}
            if any(have[x] != want[x] for x in have.keys() & want.keys()):
                continue
            best.append((sum(1 for x in have.keys() & want.keys()), p))
        best.sort(key=lambda x: -x[0])
        if best and (len(best) == 1 or best[0][0] > best[1][0]):
            return best[0][1], url
        return Recorded(url, NOT_FOUND, {}, 404), url

    def search(self, page: Recorded, url: str, inp: Dict[str, Any], value: str) -> Optional[str]:
        root, base = K.parse(page.dom)
        ins = [x for x in K.walk(root) if x.tag == "input"]
        el = ins[inp["index"]] if 0 <= int(inp.get("index", -1)) < len(ins) else None
        f = form_of(el) if el is not None else None
        u = form_url(f, url, base, {el.attrs["name"]: value}) if f is not None and el.attrs.get("name") else None
        if u and C._norm(u) in self.pages:
            return u
        return self.typed.get(value) or u

    def read(self, p: Recorded, url: str, cap11: bool = False) -> Dict[str, Any]:
        """The page's READ_JS answer, link signatures and priced links (cached by the DOM's digest and the URL);
        ``cap11``: ``READ_JS_V11``'s controls."""
        key = hashlib.sha1((url + "\u0001" + p.dom).encode("utf-8", "replace")).hexdigest() + \
            hashlib.sha1(json.dumps(p.recorded, sort_keys=True, default=str).encode()).hexdigest()[:8] + \
            ("|cap11" if cap11 else "")
        got = self._reads.get(key)
        if got is None:
            root, base = K.parse(p.dom)
            got = read_tree(root, base, url, p.dom, p.recorded, cap11=cap11)
            got["link_sigs"] = link_sigs(root, base, url)
            got["priced_links"] = priced_links(root, base, url)
            got["inputs"] = search_inputs_of(root)
            got["text_inputs"] = text_inputs_of(root)
            got["dom"] = p.dom
            if len(self._reads) > 64:
                self._reads.clear()
            self._reads[key] = got
        return got


class DomPage:
    """The duck-typed page on a ``DomSite`` (see the module doc). ``gotos``: every URL loaded (the traffic a live run
    would send for its navigations); ``acts``: the clicks and choices."""

    def __init__(self, site: DomSite, url: str = "about:blank"):
        self.site = site
        self.gotos: List[str] = []
        self.acts: List[Dict[str, Any]] = []
        self.cur_url = url
        self.cur: Recorded = Recorded(url, "<html><body></body></html>", {}, 200)

    def read(self) -> Dict[str, Any]:
        return self.site.read(self.cur, self.cur_url)

    async def goto(self, url: str) -> None:
        url = urllib.parse.urljoin(self.cur_url, str(url)) if self.cur_url.startswith("http") else str(url)
        self.gotos.append(url)
        self.cur, self.cur_url = self.site.route(url)

    async def settle(self, s: float = 0.0) -> None:
        return None

    async def js(self, expr: str, read_only: bool = False) -> Any:
        from anyuse.execution import demo_tool as T
        from anyuse.execution import web_arch_run as RR
        from anyuse.execution.typed_readiness import SETTLE_PROBE_JS
        if expr == SETTLE_PROBE_JS:
            return {"quiet_ms": 10 ** 6, "mutations": 0, "ready": "complete"}    # a recorded page is settled
        r = self.read()
        if expr == RR.READ_JS:
            return json.loads(json.dumps({k: v for k, v in r.items()
                                          if k not in ("dom", "link_sigs", "priced_links", "inputs", "text_inputs")}))
        if expr == getattr(RR, "READ_JS_V11", None):
            # web tool version 11, fix 4: the controls past the reader's cap (counted outside the page chrome)
            r11 = self.site.read(self.cur, self.cur_url, cap11=True)
            return json.loads(json.dumps({k: v for k, v in r11.items()
                                          if k not in ("dom", "link_sigs", "priced_links", "inputs", "text_inputs")}))
        if expr == T.LINK_SIG_JS:
            return {k: list(v) for k, v in r["link_sigs"].items()}
        if expr == T.PRICED_LINKS_JS:
            return {u: True for u in r["priced_links"]}
        if expr == T.PAGE_EXTRA_JS:
            return {"meta": dict(r.get("meta") or {}), "microdata": K.microdata_of(self.cur.dom)}
        m = re.search(r"\}\)\((\d+), (\"(?:[^\"\\\\]|\\\\.)*\"|-?\d+)\)\s*$", str(expr))
        if m and ("n.click()" in expr or "dispatchEvent(new Event('change'" in expr):
            return await self._act(int(m.group(1)), json.loads(m.group(2)), "dispatchEvent(new Event('change'" in expr)
        return K.recorded_answer(expr, dict(r, meta=r.get("meta") or {}))

    async def _act(self, k: int, arg: Any, select: bool) -> Dict[str, Any]:
        root, base = K.parse(self.cur.dom)
        ctl = K.control_nodes(root)
        if not 0 <= k < len(ctl):
            return {"ok": False, "why": "gone"}
        n = ctl[k]
        self.acts.append({"i": k, "tag": n.tag, "arg": arg, "select": select})
        if select and n.tag == "select":
            v = str(arg)
            if re.match(r"^(?:https?:)?/", v):
                await self.goto(urllib.parse.urljoin(self.cur_url, v))
                return {"ok": True, "value": v}
            f = form_of(n)
            if f is not None and n.attrs.get("name"):
                u = form_url(f, self.cur_url, base, {n.attrs["name"]: v})
                if u:
                    await self.goto(u)
                    return {"ok": True, "value": v}
            return {"ok": False, "why": "the choice changed nothing (no GET form, no URL)"}
        if n.tag == "a" and n.attrs.get("href"):
            await self.goto(K.href_of(n, self.cur_url, base))
            return {"ok": True}
        f = form_of(n)
        if n.tag == "input" and (n.attrs.get("type") or "").lower() in ("checkbox", "radio") and f is not None:
            vals = {n.attrs.get("name") or "": "" if "checked" in n.attrs else n.attrs.get("value", "on")}
            u = form_url(f, self.cur_url, base, vals)
            if u:
                await self.goto(u)
                return {"ok": True}
        return {"ok": False, "why": "nothing to follow"}

    async def search_inputs(self) -> List[Dict[str, Any]]:
        return [dict(i) for i in self.read()["inputs"]]

    async def text_inputs(self) -> List[Dict[str, Any]]:
        return [dict(i) for i in self.read()["text_inputs"]]

    async def recorded_inputs(self, want: Dict[str, Any], named14: bool = False) -> List[Dict[str, Any]]:
        """``GuardedSession.recorded_inputs``: the inputs a typed replay of ``want`` chooses among
        (``cdp_guard.replay_inputs``), first the field the accessibility tree called a search box (``ax``)."""
        from anyuse.execution.cdp_guard import replay_inputs
        inputs = replay_inputs(await self.text_inputs(), want, named14)
        if want.get("ax") and not any(i.get("index") == want.get("index") for i in inputs):
            # web-ax-tree: a text field the accessibility tree calls a search box (``ax_tree.search_input``)
            got = ax_input(self.cur.dom, int(want.get("index", -1)))
            inputs = ([got] if got else []) + inputs
        return inputs

    def _choose_input(self, value: str) -> Optional[Dict[str, Any]]:
        """``GuardedSession._choose_input`` on the recorded page: the page's structure (rank 0); the one visible input,
        named like a search; of the first ``MEASURE_INPUTS`` by rank and page order, the one whose own GET form submits
        the typed value to this site (a recorded page sends no request and shows no live text, the measured typing's
        other answers); else the first named like a search."""
        from anyuse.execution.cdp_guard import GuardedSession
        vis = [i for i in self.read()["text_inputs"] if i.get("visible")]
        named = [i for i in vis if i.get("rank") == 2]
        structural = [i for i in vis if i.get("rank") == 0]
        if structural:
            return dict(structural[0])
        if len(vis) == 1 and named:
            return dict(named[0])
        root, base = K.parse(self.cur.dom)
        ins = [x for x in K.walk(root) if x.tag == "input"]
        from anyuse.execution.site_canon import leaves_site
        for cand in sorted(vis, key=lambda i: (i.get("rank", 2), int(i.get("index", 0))))[:GuardedSession.MEASURE_INPUTS]:
            el = ins[cand["index"]]
            f = form_of(el)
            u = form_url(f, self.cur_url, base, {el.attrs["name"]: value}) if f is not None and el.attrs.get("name") \
                else None
            if u and not leaves_site(u, self.cur_url) and C._norm(u) != C._norm(self.cur_url):
                return dict(cand)
        return dict(named[0]) if named else None

    async def type_search(self, value: str, want: Optional[Dict[str, Any]] = None, **kw: Any) -> Dict[str, Any]:
        """``cdp_guard.type_search``'s choice of the input (``want``: the same name or id, else the same index, else
        nothing is typed; ``named14``: a web tool before version 15, which also finds the inputs its own rule named),
        then the site's search (a GET form; a POST form is never submitted)."""
        from anyuse.execution.cdp_guard import recorded_input
        if want:
            inputs = await self.recorded_inputs(want, bool(kw.get("named14")))
        else:
            chosen = self._choose_input(value)
            inputs = [chosen] if chosen else []
        if not inputs:
            return {"input": None, "xhr": [], "document": "", "note": "no visible search input"}
        pick = inputs[0]
        if want:
            pick = recorded_input(inputs, want)
            if pick is None:
                return {"input": None, "xhr": [], "document": "", "note": "the recorded search input is not on the page"}
        start, n0 = self.cur_url, len(self.gotos)
        target = self.site.search(self.cur, self.cur_url, pick, value)
        out: Dict[str, Any] = {"input": pick, "xhr": [], "document": "", "note": ""}
        if target:
            await self.goto(target)
        elif kw.get("form_fallback", True) is not False:
            # Enter did nothing: the input's own GET form, as the browser submits it
            root, base = K.parse(self.cur.dom)
            ins = [x for x in K.walk(root) if x.tag == "input"]
            el = ins[pick["index"]] if 0 <= int(pick.get("index", -1)) < len(ins) else None
            f = form_of(el) if el is not None else None
            u = form_url(f, self.cur_url, base, {el.attrs["name"]: value}) \
                if f is not None and el.attrs.get("name") else None
            from anyuse.execution.site_canon import leaves_site
            if u and not leaves_site(u, start) and C._norm(u) != C._norm(start):
                out["form_url"] = u
                await self.goto(u)
        out["document"] = self.cur_url
        if kw.get("form_fallback", True) is not False:
            out["moved"] = self.cur_url != start
            out["documents"] = list(self.gotos[n0:])
        return out

    async def network(self, want_bodies: bool = True) -> List[Dict[str, Any]]:
        return [{"resource_type": "Document", "url": self.cur_url, "status": self.cur.status}]

    def ax_snapshot(self) -> Optional[Dict[str, Any]]:
        """The current page's recorded accessibility-tree snapshot (``Recorded.ax``), or None: offline there is no
        browser to compute one."""
        return getattr(self.cur, "ax", None)

    async def dom(self) -> str:
        return self.cur.dom

    async def url(self) -> str:
        return self.cur_url


def ax_input(dom: str, index: int) -> Optional[Dict[str, Any]]:
    """Input ``index`` of the document (``querySelectorAll('input')``'s order) when it takes a typed value (a text /
    search field, not hidden, not in a form with a password field): ``cdp_guard.AX_INPUT_JS``'s rule offline."""
    root, _base = K.parse(dom)
    ins = [x for x in K.walk(root) if x.tag == "input"]
    if not 0 <= index < len(ins):
        return None
    el = ins[index]
    t = str(el.attrs.get("type") or "text").lower()
    if t not in ("text", "search", "") or hidden(el):
        return None
    f = form_of(el)
    if f is not None and any(x.tag == "input" and str(x.attrs.get("type") or "").lower() == "password"
                             for x in K.walk(f)):
        return None
    return {"index": index, "visible": True, "name": str(el.attrs.get("name") or ""),
            "id": str(el.attrs.get("id") or ""), "type": t or "text", "ax": True}


async def no_settle(page: Any) -> None:
    return None


def run(site: DomSite, tool: Dict[str, Any], value: str, params: Optional[Dict[str, Any]] = None,
        page_factory: Optional[Callable[[], DomPage]] = None) -> Dict[str, Any]:
    """One ``demo_tool.run_tool`` invocation on a fresh ``DomPage`` of ``site`` (no wait: the page is already rendered);
    the run's time fields removed (they vary), the page's navigations added as ``gotos``."""
    import asyncio
    from anyuse.execution import demo_tool as T
    page = page_factory() if page_factory else DomPage(site)
    out = asyncio.run(T.run_tool(page, tool, value, no_settle, result_wait_s=0.0, params=params))
    for k in ("seconds", "results_after_s"):
        out.pop(k, None)
    out["gotos"] = list(page.gotos)
    return out
