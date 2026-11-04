"""THE BROWSER'S ACCESSIBILITY TREE as a general web signal.

Why: the page readers decide what a control is, which region a link is in and what an element is called from the DOM
with our own rules (a search-like input by its name / id / placeholder, an area by its tag, a label by its inner text).
The browser computes all three itself for assistive technology - the ROLE (``searchbox``, ``combobox``, ``link``,
``button``), the ACCESSIBLE NAME (``aria-label``, ``aria-labelledby``, a ``<label for>``, an icon's SVG ``<title>``, an
image's ``alt``, a placeholder) and the LANDMARKS (``banner``, ``navigation``, ``main``, ``contentinfo``,
``complementary``, ``search``, a named ``region`` / ``form``), with the CSS-hidden parts left out. By the DOM rules,
www.spurweite-n.de's quick search (a plain text field named ``Sel_Mixed_Full`` next to a "Suchen" button) and
www.holzprofi.com's (an icon button whose only name is its SVG ``<title>Search</title>``) were never shown to the agent.

One reader, used where it helps generally:
* the agent's observation (``demo_policy.build_observation``): the search boxes the tree names (a ``searchbox``, an
  editable ``combobox`` / ``textbox`` in a ``search`` landmark, one whose name or whose own form's button says search)
  first, then the search landmark's and the main landmark's controls, with computed names for controls whose DOM text
  is empty;
* result regions (``demo_compile.region_from_dom`` / ``region_links``): items inside ``main`` (or a feed / list /
  article) are preferred; links only in a navigation / banner / contentinfo / complementary landmark are excluded;
* titles: a link's computed name is a title candidate where its DOM text is empty (``answer_refs``).
The VERIFIER (``attribute_verify``) reads the same browser tree with its OWN code (principle 1: an independent second
read, a different method from the DOM card reader; it imports nothing from here).

``read_snapshot(page)``: the tree as CDP gives it - local CDP reads of the page as it is (``Accessibility.
getFullAXTree``, ``DOM.querySelectorAll`` for the reader's control and input orders, ``DOM.pushNodesByBackendIdsToFrontend``
to join them; nothing is sent to the site); ``read_raw`` / ``compact``: the compact record the readers use. A page
without CDP (the offline ``DomPage``) gives None unless it carries a recorded snapshot (``page.ax_snapshot()``: the
offline measurement loads saved DOMs into the container browser with the network off and records their trees). Every
consumer works as before without a tree.
"""
from __future__ import annotations

import inspect
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence

#: the raw record's format version
RAW_VERSION = 1
#: the most AX nodes kept (a page with more is cut: the tree's first nodes in document order)
MAX_NODES = 30000
NAME_CHARS = 300
AX_TIMEOUT_S = 20.0
#: landmark roles (Chrome's computed roles; ``region`` and ``form`` are landmarks only when named)
LANDMARKS = frozenset({"banner", "navigation", "main", "contentinfo", "complementary", "search", "region", "form"})
#: the page's chrome: never a result region
CHROME = frozenset({"banner", "navigation", "contentinfo", "complementary"})
#: containers of result items
ITEMS = frozenset({"feed", "list", "listitem", "article", "row", "gridcell", "table", "grid", "treeitem", "listbox"})
#: roles whose node is text, not an element (never joined to a DOM element)
TEXT_ROLES = frozenset({"StaticText", "InlineTextBox", "LineBreak", "text"})
#: editable roles a search value can be typed into
EDITABLE = frozenset({"searchbox", "combobox", "textbox"})
#: the roles of the reader's controls and inputs (only these are joined to DOM nodes: fewer DOM events in a session)
INTERACTIVE = EDITABLE | frozenset({"link", "button", "checkbox", "radio", "menuitem", "menuitemcheckbox",
                                    "menuitemradio", "tab", "option", "switch", "listbox", "spinbutton", "slider",
                                    "ComboBoxSelect", "PopUpButton", "DisclosureTriangle", "ComboBoxMenuButton"})
#: a name that says "search" (the measured sites' languages; word starts, German compounds ending in -suche)
SEARCH_NAME = re.compile(
    r"(?:^|[^a-z])(?:search|such|recherch|busc|zoek|cerca|ricerc|sok|sog|haku|szuk|wyszuk|hleda|vyhled|keres|pesquis|"
    r"find|query|keyword|stichwort|suchbegriff)|suche(?:n)?(?:[^a-z]|$)", re.I)
#: the reader's control selector (``web_arch_run.CONTROL_SEL``): the control order ``read["elements"][*]["i"]`` uses
HREFS_JS = r"""
(sel => Array.from(document.querySelectorAll(sel)).map(n => (n.href && typeof n.href === 'string') ? n.href : ''))(%s)
"""


def fold(s: Any) -> str:
    """Lower case, accents dropped (``ö`` -> ``o``; ``ø`` / ``å`` / ``æ`` kept as ``o`` / ``a`` / ``ae``)."""
    t = str(s or "").lower().replace("ø", "o").replace("æ", "ae").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


def says_search(name: Any) -> bool:
    return bool(SEARCH_NAME.search(fold(name)))


# ---------------------------------------------------------------------------
# reading (CDP)
# ---------------------------------------------------------------------------
async def read_snapshot(page: Any) -> Optional[Dict[str, Any]]:
    """The page's accessibility tree as CDP gives it, joined to the reader's orders: ``{"nodes": [CDP AX nodes, trimmed],
    "ctl": [backend node id | None per control k], "inp": [... per input i], "href": [href per control k]}``, or None (no
    CDP, a read that failed). A page that carries a recorded snapshot (``page.ax_snapshot()``: the offline measurement)
    gives that one. Local CDP reads only; nothing is sent to the site."""
    hook = getattr(page, "ax_snapshot", None)
    if callable(hook):
        try:
            got = hook()
            if inspect.isawaitable(got):
                got = await got
        except Exception:                              # noqa: BLE001 - no tree is no signal, never a failure
            return None
        return got if isinstance(got, dict) and isinstance(got.get("nodes"), list) else None
    call = getattr(page, "call", None)
    if not callable(call) or not hasattr(page, "js"):
        return None
    from anyuse.execution.web_arch_run import CONTROL_SEL
    import json as _json
    try:
        tree = await call("Accessibility.getFullAXTree", {}, timeout=AX_TIMEOUT_S)
        nodes = [trim(n) for n in list((tree or {}).get("nodes") or [])[:MAX_NODES]]
        doc = await call("DOM.getDocument", {"depth": 0}, timeout=AX_TIMEOUT_S)
        root = ((doc or {}).get("root") or {}).get("nodeId")
        ctl = list(((await call("DOM.querySelectorAll", {"nodeId": root, "selector": CONTROL_SEL},
                                timeout=AX_TIMEOUT_S)) or {}).get("nodeIds") or [])
        inp = list(((await call("DOM.querySelectorAll", {"nodeId": root, "selector": "input"},
                                timeout=AX_TIMEOUT_S)) or {}).get("nodeIds") or [])
        backend = [int(n["backendDOMNodeId"]) for n in nodes if n.get("backendDOMNodeId") and
                   _role(n) in INTERACTIVE]
        pushed = list(((await call("DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": backend},
                                   timeout=AX_TIMEOUT_S)) or {}).get("nodeIds") or []) if backend else []
        hrefs = await page.js(HREFS_JS % _json.dumps(CONTROL_SEL))
    except Exception:                                  # noqa: BLE001 - no tree is no signal, never a failure
        return None
    finally:
        try:
            await call("DOM.disable", {}, timeout=5.0)
        except Exception:                              # noqa: BLE001
            pass
    node_of = {int(nid): int(b) for b, nid in zip(backend, pushed) if nid}
    return {"nodes": nodes, "ctl": [node_of.get(int(x)) for x in ctl], "inp": [node_of.get(int(x)) for x in inp],
            "href": [str(h or "")[:500] for h in hrefs] if isinstance(hrefs, list) else []}


def trim(n: Dict[str, Any]) -> Dict[str, Any]:
    """A CDP AX node with what the readers use: id, parent, role, name, ignored, DOM node, the ``url`` property."""
    out: Dict[str, Any] = {"nodeId": str(n.get("nodeId") or ""), "role": {"value": _role(n)},
                           "name": {"value": _name(n)}}
    if n.get("parentId"):
        out["parentId"] = str(n.get("parentId"))
    if n.get("ignored"):
        out["ignored"] = True
    if n.get("backendDOMNodeId"):
        out["backendDOMNodeId"] = int(n["backendDOMNodeId"])
    url = _prop(n, "url")
    if url:
        out["properties"] = [{"name": "url", "value": {"type": "string", "value": str(url)[:500]}}]
    return out


async def read_raw(page: Any) -> Optional[Dict[str, Any]]:
    """The tree as the compact record the readers use (``compact``), or None."""
    snap = await read_snapshot(page)
    if not snap:
        return None
    return compact(snap["nodes"], snap.get("ctl") or [], snap.get("inp") or [], snap.get("href") or [])


def _role(n: Dict[str, Any]) -> str:
    return str(((n.get("role") or {}).get("value")) or "")


def _name(n: Dict[str, Any]) -> str:
    return " ".join(str(((n.get("name") or {}).get("value")) or "").split())[:NAME_CHARS]


def _prop(n: Dict[str, Any], key: str) -> Any:
    for p in n.get("properties") or []:
        if p.get("name") == key:
            return (p.get("value") or {}).get("value")
    return None


def compact(nodes: Sequence[Dict[str, Any]], ctl_backend: Sequence[Optional[int]],
            inp_backend: Sequence[Optional[int]], hrefs: Sequence[Any] = ()) -> Dict[str, Any]:
    """``{"v", "n": [[parent, role, name, ignored, url, backend], ...], "ctl": [node index | -1 per control k],
    "inp": [node index | -1 per input i], "href": [href per control k]}`` - the CDP nodes in document order, joined to
    the reader's control and input orders through their DOM node."""
    idx = {str(n.get("nodeId")): i for i, n in enumerate(nodes)}
    by_backend: Dict[int, int] = {}
    rows: List[List[Any]] = []
    for i, n in enumerate(nodes):
        parent = idx.get(str(n.get("parentId") or ""), -1)
        b = n.get("backendDOMNodeId")
        if b and _role(n) not in TEXT_ROLES:
            by_backend.setdefault(int(b), i)
        url = _prop(n, "url")
        rows.append([parent, _role(n), _name(n), 1 if n.get("ignored") else 0, str(url or "")[:500], int(b or 0)])
    return {"v": RAW_VERSION, "n": rows,
            "ctl": [by_backend.get(int(b), -1) if b else -1 for b in ctl_backend],
            "inp": [by_backend.get(int(b), -1) if b else -1 for b in inp_backend],
            "href": [str(h or "")[:500] for h in hrefs]}


# ---------------------------------------------------------------------------
# the digest (pure)
# ---------------------------------------------------------------------------
class Tree:
    """A raw record as a tree: roles, names, ancestors."""

    def __init__(self, raw: Dict[str, Any]):
        self.rows = [r for r in (raw or {}).get("n") or [] if isinstance(r, list) and len(r) >= 4]
        self.kids: List[List[int]] = [[] for _ in self.rows]
        for i, r in enumerate(self.rows):
            p = int(r[0])
            if 0 <= p < len(self.rows) and p != i:
                self.kids[p].append(i)
        self._land: Dict[int, str] = {}

    def role(self, i: int) -> str:
        return str(self.rows[i][1]) if 0 <= i < len(self.rows) else ""

    def name(self, i: int) -> str:
        return str(self.rows[i][2]) if 0 <= i < len(self.rows) else ""

    def ignored(self, i: int) -> bool:
        return bool(self.rows[i][3]) if 0 <= i < len(self.rows) else True

    def ancestors(self, i: int, limit: int = 200):
        seen = 0
        p = int(self.rows[i][0]) if 0 <= i < len(self.rows) else -1
        while 0 <= p < len(self.rows) and seen < limit:
            yield p
            p = int(self.rows[p][0])
            seen += 1

    def landmark(self, i: int) -> str:
        """The nearest landmark around node ``i`` ("" when none; a ``region`` / ``form`` only when named)."""
        if i in self._land:
            return self._land[i]
        out = ""
        for a in self.ancestors(i):
            r = self.role(a)
            if r in LANDMARKS and not self.ignored(a) and (r not in ("region", "form") or self.name(a)):
                out = r
                break
        self._land[i] = out
        return out

    def in_item(self, i: int) -> bool:
        return any(self.role(a) in ITEMS for a in self.ancestors(i, 12))

    def text(self, i: int, cap: int = 600) -> str:
        """The visible text under node ``i`` (its static texts, in order)."""
        out: List[str] = []
        todo, size = [i], 0
        while todo and size < cap:
            j = todo.pop()
            if self.role(j) in ("StaticText", "text") and self.name(j):
                out.append(self.name(j))
                size += len(self.name(j)) + 1
            todo.extend(reversed(self.kids[j]))
        return " ".join(out)[:cap]

    def near_search_button(self, i: int, up: int = 3, budget: int = 60) -> bool:
        """Is there a button whose name says search within ``up`` levels around node ``i`` (its own form / box)?"""
        for a in self.ancestors(i, up):
            r = self.role(a)
            if (r in LANDMARKS and r != "form") or r == "RootWebArea":
                break                                  # never across the field's own box into its landmark
            seen = 0
            todo = list(self.kids[a])
            while todo and seen < budget:
                j = todo.pop()
                seen += 1
                if j != i and self.role(j) == "button" and not self.ignored(j) and says_search(self.name(j)):
                    return True
                todo.extend(self.kids[j])
            if r == "form":
                break                                  # the field's own form: its buttons only
        return False


def search_input(t: Tree, i: int) -> str:
    """Why node ``i`` is a search box by the tree ("" when it is not): its role, its landmark, its name, its own form's
    button."""
    role = t.role(i)
    if t.ignored(i) or role not in EDITABLE:
        return ""
    if role == "searchbox":
        return "role searchbox"
    if t.landmark(i) == "search":
        return f"a {role} in the search landmark"
    if says_search(t.name(i)):
        return f"a {role} named {t.name(i)[:40]!r}"
    if t.near_search_button(i):
        return f"a {role} next to a search button"
    return ""


def enrich(read: Dict[str, Any], raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Add the tree's reading to a page read (``web_arch_run.READ_JS``'s ``elements`` / ``inputs``): each element its
    ``ax_role``, ``ax_name``, ``ax_landmark``, ``ax_item``; ``read["ax_inputs"]``: every input the tree calls a search
    box (index, role, name, why); ``read["ax_landmarks"]``. Pure; returns ``read``. No tree: ``read`` unchanged."""
    raw = raw if raw is not None else read.get("ax")
    if not isinstance(raw, dict) or not raw.get("n"):
        return read
    t = Tree(raw)
    ctl = raw.get("ctl") or []
    for e in read.get("elements") or []:
        try:
            k = int(e.get("i"))
        except (TypeError, ValueError):
            continue
        j = ctl[k] if 0 <= k < len(ctl) else -1
        if j is None or j < 0 or j >= len(t.rows):
            continue
        e["ax_role"], e["ax_name"] = t.role(j), t.name(j)
        e["ax_landmark"], e["ax_item"] = t.landmark(j), t.in_item(j)
        if t.ignored(j):
            e["ax_ignored"] = True
    found = []
    for idx, j in enumerate(raw.get("inp") or []):
        if j is None or j < 0 or j >= len(t.rows):
            continue
        why = search_input(t, j)
        if why:
            found.append({"index": idx, "role": t.role(j), "name": t.name(j)[:80], "why": why,
                          "landmark": t.landmark(j)})
    read["ax_inputs"] = found
    read["ax_landmarks"] = [{"role": t.role(i), "name": t.name(i)[:60]} for i in range(len(t.rows))
                            if t.role(i) in LANDMARKS and not t.ignored(i) and
                            (t.role(i) not in ("region", "form") or t.name(i))][:40]
    return read


#: the reader's area names for the tree's landmarks (``web_arch_run.READ_JS``'s ``area``)
AREA_OF = {"search": "search", "main": "main", "banner": "header", "navigation": "nav", "contentinfo": "footer",
           "complementary": "aside"}


def area_of(e: Dict[str, Any]) -> str:
    """The element's area: the tree's landmark when it names one of the reader's areas, else the reader's own."""
    return AREA_OF.get(str(e.get("ax_landmark") or ""), "") or str(e.get("area") or "")


def chrome(e: Dict[str, Any]) -> bool:
    """Is this element (``enrich``) in the page's chrome by the tree: a navigation, banner, contentinfo or
    complementary landmark?"""
    return str(e.get("ax_landmark") or "") in CHROME


def links(raw: Optional[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Every link of the tree by its URL (the AX ``url`` property, else the reader's href of its control): name,
    landmark, whether it sits in an item container."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not isinstance(raw, dict) or not raw.get("n"):
        return out
    t = Tree(raw)
    href_of: Dict[int, str] = {}
    for k, j in enumerate(raw.get("ctl") or []):
        if j is not None and 0 <= j < len(t.rows) and k < len(raw.get("href") or []):
            href_of[j] = str(raw["href"][k] or "")
    for i, r in enumerate(t.rows):
        if r[1] != "link" or t.ignored(i):
            continue
        u = str(r[4] or "") or href_of.get(i, "")
        if not u.startswith(("http://", "https://")):
            continue
        out.setdefault(u.split("#", 1)[0], []).append({"name": t.name(i), "landmark": t.landmark(i),
                                                       "item": t.in_item(i)})
    return out
