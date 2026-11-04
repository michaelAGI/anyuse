"""Container scope: WHICH folder a document-less window shows.

A TextEdit window names its document (``AXDocument``), so a selector recorded in it is scoped to
that document. A Finder window and a Notes window name none (Finder: 142 of 142 recorded
front reads without ``AXDocument``). Their list shows the CONTENT of a container, a folder or a
Notes folder, and the same file name or note title can be shown from another container. Measured:
the resolver acted on such a namesake from another folder in 71 of 1,522 cases.

This module is the ONE definition of a window's container identity, shared by the resolver
(``context/accessibility.py``: recorded in the selector like ``document``, checked before acting)
and by the row lanes (``select_row``, the watcher's row capture). It reads the window's own AX
attributes, and FIRST an Apple-Events source when one is plugged in (below); never the file system.

**The Apple-Events source** (the AppleScript route). ``set_script_reader(reader)``
plugs in a callable the guest supplies (``execution/applescript_route.ScriptScopeReader``: Finder's
``POSIX path of (target of front window as alias)``, Notes' selected note's folder with its account and parents).
For an app with a reader it is asked first, with the window's AX title and frame; an answer
``{"status": "ok", "key": ..., "title": ...}`` is the scope with strength ``"path"`` and source ``"applescript"``
(the title stays the window's own display name). Anything else (``automation_not_granted`` (-1743/-1744),
``not_running``, ``no_answer``, an exception) falls back to the AX reading below, and ``read_scope`` reports the
answer. None is plugged in by default (the route OFF): every read is the AX reading, unchanged. Because the
resolver, the executor's check before acting, ``select_row`` and the watcher all read through ``container_scope``,
they all get path strength wherever the Apple-Events source answers.

What is read, per app (``READERS``):

* **Finder** (``finder_folder``). The folder's full path, from the first of:
  1. the window's ``AXDocument`` (a directory URL), 2. the window's ``AXURL``,
  3. a POSIX path shown as the window title (Finder's "show the path in the title" option),
  4. an element of the window outside its lists (the title bar's proxy icon, the path bar)
     whose ``AXURL`` is a file URL ending in the window title.
  None of these was present in the recorded sessions (they read no ``AXURL``). Else the window
  title alone: the folder's NAME, a weak identity.
* **Notes** (``notes_folder``). The folder outline's selected row (``AXSelectedRows``, else a row
  with ``AXSelected``) whose label is the folder named in the window title, with its account and
  parent folders (``AXDisclosedByRow``, else ``AXDisclosureLevel``): ``"iCloud/Privat/Arzt"``.
  Else the folder named in the window title (``"Arzt – 3 Notizen"`` -> ``"Arzt"``), weak.

A scope is ``{"kind", "strength", "key", "title", "source"}`` (key and title in Unicode NFC: AX returns some
paths decomposed, ``AXDocument`` among them, and a name compared in one form must match it in the other):

* ``strength == "path"``: ``key`` is the container's full identity (a normalised path, or the
  Notes outline path), ``title`` its display name;
* ``strength == "title"``: ``key`` is ``""`` and ``title`` the display name only. A name can rule
  a container OUT (another name is another folder) but never IN: two folders can share a name.

``same_scope(recorded, live)`` -> ``SAME`` (full identities equal), ``DIFFERENT`` (full
identities differ, or the names differ) or ``UNCONFIRMED`` (only names, and they agree; or a side
is unknown). The resolver's policy on top of it (``accessibility.find_selector`` and the executor's
check before acting):

* a candidate in a ``DIFFERENT`` container is not the element (filtered; alone, refused);
* a recording that read the container's full path (``scope_strength == "path"``) acts on the
  container's CONTENT (``in_container_content``: a row of a list other than the app's navigation
  sidebar) only when the live container is ``SAME``: an unconfirmed one is refused, never guessed;
* a recording that read only the container's name acts on an ``UNCONFIRMED`` container. Its name
  rules other containers out, never in: two folders with the same name are the same to AX, and
  only a path read tells them apart. Refusing there would refuse every correct replay of such a
  recording (124 measured correct runs select Finder files and Notes notes in windows
  that show names only).

``scope_key(scope, salt)`` is the scope as a salted HMAC for storage, as the watcher keeps a
document (``accessibility.document_key``); ``same_scope`` accepts it as the recorded side.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

FINDER = "com.apple.finder"
NOTES = "com.apple.Notes"

SAME = "same"
DIFFERENT = "different"
UNCONFIRMED = "unconfirmed"

#: a scope stored as a salted key: ``scopekey:<salt id>:<kind>:<strength>:<HMAC key>:<HMAC title>``
SCOPE_KEY_PREFIX = "scopekey:"

ROW_CONTAINER_ROLES = ("AXTable", "AXOutline", "AXList", "AXBrowser")
ROW_ROLES = ("AXRow", "AXOutlineRow")

#: the navigation sidebar of a container app, by its list's AXDescription (localized: German and
#: English measured or documented; another locale's sidebar counts as content, which only refuses)
SIDEBAR_DESCRIPTIONS = {
    FINDER: frozenset({"Seitenleiste", "Sidebar"}),
    NOTES: frozenset({"Ordner", "Folders"}),
}

#: elements read at most while looking for a URL-bearing element or a folder outline
SCAN_CAP = 400
SCAN_DEPTH = 6
#: rows of one outline read at most for a Notes folder's parents
ROW_CAP = 500

# "Arzt – 3 Notizen" / "Arzt — 1 Note": the trailing count is not part of the folder's name
_NOTES_COUNT = re.compile(r"^(?P<name>.*\S)\s+[–—-]\s+\d[\d.,]*\s+\S+$")

Reader = Callable[[Any, str], Any]


# ---------------------------------------------------------------------------
# reading (through the tree's ``attr`` / ``children``: pure over any tree-like object)
# ---------------------------------------------------------------------------

def _reader(tree) -> Tuple[Reader, Callable[[Any], List[Any]]]:
    attr = getattr(tree, "attr", None)
    kids = getattr(tree, "children", None)
    if not callable(attr) or not callable(kids):
        raise TypeError("container_scope needs a tree with attr(element, name) and children(element)")
    return attr, kids


def _s(v: Any) -> str:
    if v is None:
        return ""
    try:
        return str(v).strip()
    except Exception:  # noqa: BLE001 - an unprintable value is no value
        return ""


def _file_path(v: Any) -> str:
    """A file URL or an absolute path -> the normalised absolute path; anything else -> ""."""
    from urllib.parse import unquote, urlparse
    text = _s(v)
    if text.lower().startswith("file://"):
        text = unquote(urlparse(text).path or "")
    if not text.startswith("/"):
        return ""
    return text.rstrip("/") or "/"


def scope_title(app: Optional[str], window_title: Optional[str]) -> str:
    """The container's display name in a window title: Finder's title as is, Notes' title
    without its trailing note count."""
    title = _s(window_title)
    if app == NOTES:
        m = _NOTES_COUNT.match(title)
        if m:
            return m.group("name").strip()
    return title


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def _scope(kind: str, strength: str, key: str, title: str, source: str) -> Dict[str, str]:
    return {"kind": kind, "strength": strength, "key": _nfc(key), "title": _nfc(title), "source": source}


def _walk(attr: Reader, kids, root, skip_lists: bool):
    """(element, depth) below ``root`` in document order, bounded by SCAN_CAP / SCAN_DEPTH;
    with ``skip_lists`` a list's rows are not entered."""
    stack = [(c, 1) for c in reversed(kids(root) or [])]
    seen = 0
    while stack and seen < SCAN_CAP:
        el, depth = stack.pop()
        seen += 1
        yield el, depth
        role = _s(attr(el, "AXRole"))
        if skip_lists and role in ROW_CONTAINER_ROLES + ROW_ROLES:
            continue
        if depth < SCAN_DEPTH:
            stack.extend((c, depth + 1) for c in reversed(kids(el) or []))


def _finder(attr: Reader, kids, window) -> Optional[Dict[str, str]]:
    kind = "finder_folder"
    title = _s(attr(window, "AXTitle"))
    for source in ("AXDocument", "AXURL"):
        path = _file_path(attr(window, source))
        if path:
            return _scope(kind, "path", path, os.path.basename(path) or title, source)
    if title.startswith("/"):
        path = title.rstrip("/") or "/"
        return _scope(kind, "path", path, os.path.basename(path) or path, "title_path")
    if title:
        for el, _d in _walk(attr, kids, window, skip_lists=True):
            path = _file_path(attr(el, "AXURL"))
            if path and os.path.basename(path) == title:
                return _scope(kind, "path", path, title, "url_element")
        return _scope(kind, "title", "", title, "title")
    return None


def _row_label(attr: Reader, kids, row) -> str:
    own = _s(attr(row, "AXTitle")) or _s(attr(row, "AXDescription"))
    if own:
        return own
    stack = [(c, 1) for c in reversed(kids(row) or [])]
    while stack:
        el, depth = stack.pop()
        role = _s(attr(el, "AXRole"))
        text = ""
        if role in ("AXStaticText", "AXTextField"):
            text = _s(attr(el, "AXValue"))
        elif role == "AXCell":
            text = _s(attr(el, "AXTitle")) or _s(attr(el, "AXDescription"))
        if text:
            return text
        if depth < 3:
            stack.extend((c, depth + 1) for c in reversed(kids(el) or []))
    return ""


def _rows(attr: Reader, kids, box) -> List[Any]:
    rows = attr(box, "AXRows")
    try:
        if rows is not None:
            return list(rows)[:ROW_CAP]
    except Exception:  # noqa: BLE001
        pass
    return [c for c in (kids(box) or []) if _s(attr(c, "AXRole")) in ROW_ROLES][:ROW_CAP]


def _level(attr: Reader, row) -> Optional[int]:
    v = attr(row, "AXDisclosureLevel")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _outline_chain(attr: Reader, kids, box, row) -> Optional[List[str]]:
    """The labels from the outline's top level down to ``row`` (account, parent folders, the
    row), or None when the outline gives no parent relation (one level is no chain)."""
    chain = [_row_label(attr, kids, row)]
    parent = attr(row, "AXDisclosedByRow")
    if parent is not None:
        for _ in range(8):
            if parent is None:
                return list(reversed(chain))
            chain.append(_row_label(attr, kids, parent))
            parent = attr(parent, "AXDisclosedByRow")
        return None
    level = _level(attr, row)
    if level is None:
        return None
    rows = _rows(attr, kids, box)
    try:
        i = next(k for k, r in enumerate(rows) if r == row)
    except StopIteration:
        return None
    want = level
    for r in reversed(rows[:i]):
        lv = _level(attr, r)
        if lv is not None and lv < want:
            chain.append(_row_label(attr, kids, r))
            want = lv
            if lv <= 0:
                break
    return list(reversed(chain))


def _notes(attr: Reader, kids, window) -> Optional[Dict[str, str]]:
    kind = "notes_folder"
    folder = scope_title(NOTES, _s(attr(window, "AXTitle")))
    if folder:
        for box, _d in _walk(attr, kids, window, skip_lists=True):
            if _s(attr(box, "AXRole")) != "AXOutline":
                continue
            sel = attr(box, "AXSelectedRows")
            try:
                selected = list(sel) if sel is not None else []
            except Exception:  # noqa: BLE001
                selected = []
            if not selected:
                selected = [r for r in _rows(attr, kids, box) if attr(r, "AXSelected") is True]
            hits = [r for r in selected if _row_label(attr, kids, r) == folder]
            if len(hits) != 1:
                continue
            chain = _outline_chain(attr, kids, box, hits[0])
            if not chain or len(chain) < 2 or not all(chain):
                break
            # two rows with the same account / parents / name are one name, not one identity
            twins = [r for r in _rows(attr, kids, box) if _row_label(attr, kids, r) == folder]
            if sum(1 for r in twins if _outline_chain(attr, kids, box, r) == chain) > 1:
                break
            return _scope(kind, "path", "/".join(chain), folder, "sidebar_selection")
        return _scope(kind, "title", "", folder, "title")
    return None


#: app bundle id -> (kind, reader). Another document-less container app is one entry here.
READERS: Dict[str, Tuple[str, Callable[..., Optional[Dict[str, str]]]]] = {
    FINDER: ("finder_folder", _finder),
    NOTES: ("notes_folder", _notes),
}


def has_reader(app: Optional[str]) -> bool:
    return bool(app) and app in READERS


# ---------------------------------------------------------------------------
# the Apple-Events source
# ---------------------------------------------------------------------------

#: the plugged-in Apple-Events reader (None: the route is off, AX only)
_SCRIPT_READER: Optional[Callable[[Dict[str, Any]], Any]] = None
#: ``read_scope``'s default: the plugged-in reader
PLUGGED = object()


def set_script_reader(reader: Optional[Callable[[Dict[str, Any]], Any]]):
    """Plug in (or, with None, remove) the Apple-Events source every ``container_scope`` read asks first.
    ``reader(query)`` gets ``{"app", "title", "frame", "window", "tree"}`` (``frame``: ``(x, y, w, h)`` when the
    tree gives plain pairs, else None: the guest's reader computes it from ``window``) and answers a dict with
    ``status`` (``"ok"`` with ``key`` and ``title``; otherwise why not). Returns the reader it replaced."""
    global _SCRIPT_READER
    old, _SCRIPT_READER = _SCRIPT_READER, reader
    return old


def script_reader() -> Optional[Callable[[Dict[str, Any]], Any]]:
    return _SCRIPT_READER


def _pair(v: Any) -> Optional[Tuple[float, float]]:
    try:
        if isinstance(v, dict):
            v = (v.get("x", v.get("width")), v.get("y", v.get("height")))
        a, b = v[0], v[1]
        return float(a), float(b)
    except Exception:  # noqa: BLE001 - an AXValue the tree did not unwrap: the reader computes it
        return None


def _frame(attr: Reader, window) -> Optional[Tuple[float, float, float, float]]:
    pos, size = _pair(attr(window, "AXPosition")), _pair(attr(window, "AXSize"))
    if pos is None or size is None:
        return None
    return pos[0], pos[1], size[0], size[1]


def _ask_script(reader, tree, attr: Reader, window, app: str) -> Dict[str, Any]:
    query = {"app": app, "title": _nfc(_s(attr(window, "AXTitle"))), "frame": _frame(attr, window),
             "window": window, "tree": tree}
    try:
        ans = reader(query)
    except Exception as exc:  # noqa: BLE001 - a failing source is no answer; AX decides
        return {"status": "error", "why": f"{type(exc).__name__}"}
    if not isinstance(ans, dict) or not ans.get("status"):
        return {"status": "no_answer", "why": "the reader gave no status"}
    return ans


def _script_scope(app: str, attr: Reader, window, ans: Dict[str, Any]) -> Optional[Dict[str, str]]:
    kind = READERS[app][0]
    key = _s(ans.get("key"))
    if app == FINDER:
        key = _file_path(key)
    if not key:
        return None
    title = scope_title(app, _s(attr(window, "AXTitle")))
    if not title or title.startswith("/"):
        title = _s(ans.get("title")) or os.path.basename(key) or key
    return _scope(kind, "path", key, title, "applescript")


def read_scope(tree, window, app: Optional[str] = None,
               reader: Any = PLUGGED) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
    """``(scope, script)``: the container ``window`` shows (as ``container_scope``) and the Apple-Events
    source's answer (None when no source was asked: none plugged in, or an app without a reader). ``reader``
    defaults to the plugged-in one; None asks AX only. A read; never raises."""
    if window is None:
        return None, None
    try:
        if app is None:
            getter = getattr(tree, "bundle_id", None)
            app = getter() if callable(getter) else None
        if not has_reader(app):
            return None, None
        attr, kids = _reader(tree)
        rd = _SCRIPT_READER if reader is PLUGGED else reader
        script = None
        if rd is not None:
            script = _ask_script(rd, tree, attr, window, app)
            if script.get("status") == "ok":
                sc = _script_scope(app, attr, window, script)
                if sc is not None:
                    return sc, script
                script = dict(script, status="no_answer", why="the answer named no container")
        return READERS[app][1](attr, kids, window), script
    except Exception:  # noqa: BLE001 - an unreadable window has no scope
        return None, None


def container_scope(tree, window, app: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The container ``window`` shows, or None (no window, an app without a reader, nothing
    readable). ``tree`` reads attributes (``attr(element, name)``, ``children(element)``);
    ``app`` is the window's app (default: ``tree.bundle_id()``). The plugged-in Apple-Events
    source first (``set_script_reader``), then the window's AX attributes. A read; never raises."""
    return read_scope(tree, window, app)[0]


#: raw sources listed at most per window (``raw_sources``)
RAW_URL_CAP = 20
RAW_ROW_CAP = 200


def raw_sources(tree, window, app: Optional[str] = None) -> Dict[str, Any]:
    """What a window offers as container identity, for the guest's ``scope_read`` (so the offline side sees
    which sources exist live): the window's ``AXTitle``, ``AXDocument``, ``AXURL``; up to 20 elements outside its
    lists that carry an ``AXURL`` (role, subrole, URL); for Notes the folder outline's rows (label,
    ``AXSelected``, ``AXDisclosureLevel``, the label of ``AXDisclosedByRow``; at most 200). A read."""
    out: Dict[str, Any] = {}
    if window is None:
        return out
    try:
        if app is None:
            getter = getattr(tree, "bundle_id", None)
            app = getter() if callable(getter) else None
        attr, kids = _reader(tree)
        out = {"AXTitle": _s(attr(window, "AXTitle")), "AXDocument": _s(attr(window, "AXDocument")),
               "AXURL": _s(attr(window, "AXURL")), "url_elements": []}
        for el, _d in _walk(attr, kids, window, skip_lists=True):
            url = _s(attr(el, "AXURL"))
            if url:
                out["url_elements"].append({"role": _s(attr(el, "AXRole")), "subrole": _s(attr(el, "AXSubrole")),
                                            "AXURL": url})
                if len(out["url_elements"]) >= RAW_URL_CAP:
                    break
        if app == NOTES:
            rows: List[Dict[str, Any]] = []
            for box, _d in _walk(attr, kids, window, skip_lists=True):
                if _s(attr(box, "AXRole")) != "AXOutline" or \
                        _s(attr(box, "AXDescription")) not in SIDEBAR_DESCRIPTIONS[NOTES]:
                    continue
                for r in _rows(attr, kids, box)[:RAW_ROW_CAP]:
                    parent = attr(r, "AXDisclosedByRow")
                    rows.append({"label": _row_label(attr, kids, r), "selected": attr(r, "AXSelected") is True,
                                 "level": _level(attr, r),
                                 "parent": _row_label(attr, kids, parent) if parent is not None else None})
                break
            out["outline_rows"] = rows
    except Exception as exc:  # noqa: BLE001 - what was read so far is the answer
        out["error"] = type(exc).__name__
    return out


def in_container_content(tree, element, app: Optional[str] = None) -> bool:
    """Is ``element`` part of a container's CONTENT: a row (or inside one) of a list that is not
    the app's navigation sidebar? Finder's file list and Notes' note list are; Finder's sidebar
    places and Notes' folder list are not (they are the same in every window of the app). Only
    for an app with a reader. A read; an unreadable element counts as content (it only refuses)."""
    try:
        if app is None:
            getter = getattr(tree, "bundle_id", None)
            app = getter() if callable(getter) else None
        if not has_reader(app):
            return False
        attr, _kids = _reader(tree)
        cur, row = element, None
        for _ in range(5):
            if cur is None:
                break
            if _s(attr(cur, "AXRole")) in ROW_ROLES:
                row = cur
                break
            cur = attr(cur, "AXParent")
        if row is None:
            return False
        box = attr(row, "AXParent")
        for _ in range(6):
            if box is None:
                return True
            if _s(attr(box, "AXRole")) in ROW_CONTAINER_ROLES:
                break
            box = attr(box, "AXParent")
        if box is None:
            return True
        return _s(attr(box, "AXDescription")) not in SIDEBAR_DESCRIPTIONS.get(app, frozenset())
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# storage and comparison
# ---------------------------------------------------------------------------

def _salt(salt: Optional[bytes], create: bool) -> Optional[bytes]:
    if salt is not None:
        return salt if len(salt) >= 16 else None
    from anyuse.context.accessibility import _document_salt
    return _document_salt(create=create)


def _salt_id(salt: bytes) -> str:
    from anyuse.context.accessibility import _salt_id as doc_salt_id
    return doc_salt_id(salt)


def _mac(salt: bytes, part: str, text: str) -> str:
    if not text:
        return "-"
    return hmac.new(salt, f"anyuse scope {part}\0{text}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def scope_key(scope: Optional[Dict[str, str]], salt: Optional[bytes] = None, create: bool = False) -> str:
    """``scope`` as a salted key for storage (no path, no folder name in it), or "" (no scope,
    no salt). ``salt`` defaults to this machine's document salt (``accessibility.document_key``;
    ``create`` makes it when there is none). A key from another machine's salt confirms nothing
    here (``same_scope`` -> UNCONFIRMED)."""
    if not isinstance(scope, dict) or not scope.get("kind"):
        return ""
    s = _salt(salt, create)
    if s is None:
        return ""
    return ":".join([SCOPE_KEY_PREFIX + _salt_id(s), str(scope["kind"]), str(scope.get("strength") or "title"),
                     _mac(s, "key", str(scope.get("key") or "")), _mac(s, "title", str(scope.get("title") or ""))])


def scope_strength(scope: Union[None, str, Dict[str, str]]) -> str:
    """``"path"`` / ``"title"`` for a raw scope or a ``scope_key``; ``""`` for none."""
    if isinstance(scope, dict):
        return str(scope.get("strength") or "") if scope.get("kind") else ""
    if is_scope_key(scope):
        parsed = _parse_key(scope)
        return parsed["strength"] if parsed else ""
    return ""


def is_scope_key(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(SCOPE_KEY_PREFIX)


def _parse_key(value: str) -> Optional[Dict[str, str]]:
    parts = value[len(SCOPE_KEY_PREFIX):].split(":")
    if len(parts) != 5:
        return None
    sid, kind, strength, key, title = parts
    return {"salt_id": sid, "kind": kind, "strength": strength,
            "key": "" if key == "-" else key, "title": "" if title == "-" else title}


def _compare(rec: Dict[str, str], live: Dict[str, str]) -> str:
    if rec.get("kind") != live.get("kind"):
        return DIFFERENT
    rk, lk = rec.get("key") or "", live.get("key") or ""
    if rec.get("strength") == "path" and live.get("strength") == "path" and rk and lk:
        return SAME if rk == lk else DIFFERENT
    rt, lt = rec.get("title") or "", live.get("title") or ""
    if rt and lt and rt != lt:
        return DIFFERENT
    return UNCONFIRMED


def same_scope(recorded: Union[None, str, Dict[str, str]], live: Optional[Dict[str, str]],
               salt: Optional[bytes] = None) -> str:
    """Is the container ``live`` (a fresh ``container_scope`` read) the recorded one?
    ``SAME``: both full identities read and equal. ``DIFFERENT``: full identities that differ,
    another kind, or another display name (a renamed folder is another folder, as a renamed
    target is another element). ``UNCONFIRMED``: only display names, and they agree; or a side
    unknown. ``recorded`` may be a raw scope or a ``scope_key``."""
    if not recorded or not isinstance(live, dict) or not live.get("kind"):
        return UNCONFIRMED
    if isinstance(recorded, str):
        parsed = _parse_key(recorded) if is_scope_key(recorded) else None
        s = _salt(salt, create=False)
        if parsed is None or s is None or parsed["salt_id"] != _salt_id(s):
            return UNCONFIRMED
        hashed = _parse_key(scope_key(live, salt=s)) or {}
        return _compare(parsed, hashed)
    if not isinstance(recorded, dict) or not recorded.get("kind"):
        return UNCONFIRMED
    return _compare(recorded, live)
