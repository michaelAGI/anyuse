"""JSON embedded in a server-rendered page — the third compiled rung.

A great many pages that have no JSON *API* still ship their data as JSON
*inside* the HTML: a framework's hydration payload, a `<script
type="application/json">` island, a schema.org block. Reading that is far less
brittle than reading markup, because it is the page's own data structure rather
than its presentation: a redesign renames classes and re-nests divs, and the
hydration payload usually survives it untouched.

So the preference order, when more than one rung proves (docs/COMPILE.md says
the same in prose):

    JSON API  >  embedded JSON  >  HTML selectors

* a **JSON API** is a published interface with its own contract, and nothing on
  the page can break it;
* **embedded JSON** is not published, but it is *data*, addressed by the same
  auditable `a.b[*].c` paths the API rung uses, and it moves when the site's
  data model moves rather than when its CSS does;
* **HTML selectors** are bound to presentation, which changes most often.

What this module finds
----------------------
=========================  ==================================================
``script_json``            ``<script type="application/json">…</script>``
``next_data``              the same, with ``id="__NEXT_DATA__"`` (a framework's id)
``ld_json``                ``<script type="application/ld+json">…</script>``
``assignment``             ``window.APP_STATE = {...}``, ``var data = [...]``,
                           ``self.x = JSON.parse("...")``: any object or array
                           literal a page's script assigns to a name
=========================  ==================================================

Assignments are found by their structure, in the page's script elements only:
a name (a global, a member of one, a declared variable), ``=``, then a JSON
object or array, or ``JSON.parse`` of one string literal. Which payload holds
the result list is decided by the value overlap with the visible list and the
fresh-input proof, never by the name; the names of :data:`ASSIGNMENT_NAMES`
only rank payloads of the same size.

**The assignment parser does not execute anything.** It finds the name, takes
the literal right after ``=``, walks the text balancing brackets while
respecting string literals and escapes, and hands the slice to ``json.loads``.
A ``JSON.parse`` argument is one string literal, decoded by the escape rules
of a string literal alone (``\\n``, ``\\"``, ``\\xHH``, ``\\uXXXX``); a
template with ``${...}``, an octal escape or anything that is not one literal is
refused. There is no ``eval``, no ``exec``, no JS engine and no ``ast`` — so a
real JavaScript object literal (unquoted keys, trailing commas, ``undefined``,
``new Date()``) is simply **rejected**, not interpreted. That is a deliberate
limit: a value that is not JSON does not become one by being parsed harder, and
a compiler that reached for a JS evaluator to read a page would be running the
page's code, which is precisely what this whole approach exists to avoid.

A blob is addressed by a ``locator`` — a small dict that can find the same blob
again in a *different* response, which is what makes the rung replayable:

    {"kind": "script_id",   "id": "__NEXT_DATA__"}
    {"kind": "script_type", "type": "application/ld+json", "index": 0}
    {"kind": "assignment",  "name": "window.__INITIAL_STATE__"}   (the page's own name)

Brittleness, stated plainly: an embedded payload is not a published interface
either. The site can rename its hydration key or stop shipping one. That is
handled, not prevented — the extraction returns nothing, the postcondition
fails, the rung falls back to the recorded UI path, and ``api_recompile``
re-derives from the stored recording, persisting again only if the fresh-input
proof passes.
"""
from __future__ import annotations

import html.parser
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Script ``type`` attributes whose body is JSON.
JSON_SCRIPT_TYPES = ("application/json", "application/ld+json")

#: Hydration payload names some frameworks use, in preference order. They only
#: rank payloads of the same size (:func:`find_embedded`); every assignment a
#: page's script makes is found by its structure (:func:`_assignments`). A stored
#: tool that names one of them is also re-found outside a script element, as
#: before.
ASSIGNMENT_NAMES = (
    "window.__INITIAL_STATE__",
    "window.__PRELOADED_STATE__",
    "window.__NUXT__",
    "window.__APOLLO_STATE__",
    "window.__data",
    "window.__INITIAL_DATA__",
    "window.INITIAL_STATE",
    "__INITIAL_STATE__",
    "__APOLLO_STATE__",
)

#: Don't bother with a payload too small to hold a result list.
MIN_BLOB_CHARS = 24
#: Refuse to scan a pathologically large document (10 MB of markup is not a page).
MAX_MARKUP_CHARS = 10_000_000
#: Never walk more than this many characters looking for an assignment's end.
MAX_ASSIGNMENT_CHARS = 4_000_000
#: At most this many assigned literals of one document are parsed.
MAX_ASSIGNMENTS = 2000
#: Script ``type`` values whose body is a classic script or a module: none, ``module``
#: or a JavaScript MIME type (the HTML standard's list).
_JS_TYPE = re.compile(r"(?i)^(?:|module|(?:text|application)/(?:x-)?(?:java|ecma)script"
                      r"(?:1\.[0-5])?|text/(?:jscript|livescript))$")


def _bounded_json_loads(text: str) -> Any:
    from anyuse.execution.api_step import validate_json_limits
    data = json.loads(text)
    ok, reason = validate_json_limits(data)
    if not ok:
        raise ValueError(reason)
    return data


@dataclass
class Blob:
    """One JSON payload found inside a document."""
    origin: str = ""                      # script_json | next_data | ld_json | assignment
    locator: Dict[str, Any] = field(default_factory=dict)
    data: Any = None
    raw_chars: int = 0

    def describe(self) -> str:
        k = self.locator.get("id") or self.locator.get("name") or self.locator.get("type") or ""
        return f"{self.origin}({k})" if k else self.origin


# ---------------------------------------------------------------------------
# script islands
# ---------------------------------------------------------------------------

class _ScriptCollector(html.parser.HTMLParser):
    """Collect ``<script>`` bodies with their attributes. Nothing is executed.

    ``HTMLParser`` puts itself into CDATA mode for ``script``, so the body
    arrives verbatim through ``handle_data`` — entities are not decoded, which
    is exactly right for a JSON payload.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: List[Tuple[Dict[str, str], str]] = []
        self._attrs: Optional[Dict[str, str]] = None
        self._buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            self._attrs = {k.lower(): (v or "") for k, v in attrs}
            self._buf = []

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._attrs is not None:
            self.scripts.append((self._attrs, "".join(self._buf)))
            self._attrs, self._buf = None, []

    def handle_data(self, data):
        if self._attrs is not None:
            self._buf.append(data)


def _scripts(markup: str) -> List[Tuple[Dict[str, str], str]]:
    parser = _ScriptCollector()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:                       # a malformed document is still usable
        pass
    return parser.scripts


def _script_islands(markup: str) -> List[Blob]:
    out: List[Blob] = []
    type_counts: Dict[str, int] = {}
    for attrs, body in _scripts(markup):
        stype = (attrs.get("type") or "").strip().lower()
        if stype not in JSON_SCRIPT_TYPES:
            continue
        index = type_counts.get(stype, 0)
        type_counts[stype] = index + 1
        text = (body or "").strip()
        if len(text) < MIN_BLOB_CHARS:
            continue
        try:
            data = _bounded_json_loads(text)
        except ValueError:
            continue
        sid = (attrs.get("id") or "").strip()
        if sid:
            origin = "next_data" if sid == "__NEXT_DATA__" else "script_json"
            locator: Dict[str, Any] = {"kind": "script_id", "id": sid}
        elif stype == "application/ld+json":
            origin = "ld_json"
            locator = {"kind": "script_type", "type": stype, "index": index}
        else:
            origin = "script_json"
            locator = {"kind": "script_type", "type": stype, "index": index}
        out.append(Blob(origin=origin, locator=locator, data=data, raw_chars=len(text)))
    return out


# ---------------------------------------------------------------------------
# assignments — found and sliced, never executed
# ---------------------------------------------------------------------------

_OPEN = {"{": "}", "[": "]"}


def _balanced_slice(text: str, start: int) -> Optional[str]:
    """From ``text[start]`` (a ``{`` or ``[``), return the balanced literal.

    String-aware and escape-aware, so a ``}`` inside ``"a} b"`` does not close
    the object. Purely textual: no evaluation of any kind.
    """
    opener = text[start:start + 1]
    if opener not in _OPEN:
        return None
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    limit = min(len(text), start + MAX_ASSIGNMENT_CHARS)
    for i in range(start, limit):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string, quote = True, ch
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in ("}", "]"):
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


#: what JSON text holds outside its strings (``json.loads`` also reads NaN and Infinity)
_JSON_BARE = frozenset("0123456789+-.eE,:[]{} \t\r\n" "truefalsnull" "NaNInfinity")


def _json_slice(text: str, start: int) -> Optional[str]:
    """:func:`_balanced_slice` for JSON alone: the walk stops at the first character
    outside a string that JSON text cannot hold (a call's parenthesis, a regular
    expression, a single quote, a template), so a JavaScript literal in a large script
    costs one short walk, not one to the script's end."""
    if text[start:start + 1] not in _OPEN:
        return None
    depth = 0
    in_string = False
    escaped = False
    limit = min(len(text), start + MAX_ASSIGNMENT_CHARS)
    for i in range(start, limit):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in _OPEN:
            depth += 1
        elif ch in ("}", "]"):
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        elif ch not in _JSON_BARE:
            return None
    return None


#: an assignment in a script: a name (``window.x``, ``window["x"]``, ``a.b``, or a
#: variable declared with ``var``/``let``/``const``) that starts a token, ``=`` (not
#: ``==`` or ``=>``), then an object or array literal or ``JSON.parse(``
_ASSIGNMENT = re.compile(
    r"(?<![\w$.\]\)])(?:(?:var|let|const)\s+)?"
    r"(?P<name>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*"
    r"|\[\s*(?:\"[A-Za-z_$][\w$]*\"|'[A-Za-z_$][\w$]*')\s*\])*)"
    r"\s*=(?![=>])\s*(?P<rhs>[\[{]|JSON\s*\.\s*parse\s*\(\s*)")
#: how a JSON object or array begins (a JavaScript literal with a bare key does not)
_JSON_START = re.compile(r"\{\s*[\"}]|\[\s*[\[{\"\d\-\]tfn]")
_BRACKET_KEY = re.compile(r"\[\s*[\"']([^\"']+)[\"']\s*\]")

#: escapes of a string literal, decoded the way the literal itself defines them
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
               "\\": "\\", "'": "'", '"': '"', "/": "/", "`": "`", "\n": "", "\u2028": "",
               "\u2029": ""}


def _name(raw: str) -> str:
    """An assignment target as one dotted name: ``window["x"]`` is ``window.x``."""
    return re.sub(r"\s+", "", _BRACKET_KEY.sub(lambda m: "." + m.group(1), raw))


def _string_literal(text: str, start: int) -> Optional[Tuple[str, int]]:
    """``(value, end)`` of the string literal at ``text[start]`` (``"``, ``'`` or a
    template without ``${``), decoded by its escapes alone; ``None`` for anything
    else (an octal escape, a line break in a quoted literal, no closing quote)."""
    quote = text[start:start + 1]
    if quote not in ('"', "'", "`"):
        return None
    out: List[str] = []
    i, limit = start + 1, min(len(text), start + MAX_ASSIGNMENT_CHARS)
    while i < limit:
        ch = text[i]
        if ch == quote:
            value = "".join(out)
            try:                            # a surrogate pair written as two escapes
                value = value.encode("utf-16", "surrogatepass").decode("utf-16")
            except UnicodeError:
                pass
            return value, i + 1
        if ch == "\\":
            nxt = text[i + 1:i + 2]
            if nxt == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 2:i + 4]):
                out.append(chr(int(text[i + 2:i + 4], 16)))
                i += 4
            elif nxt == "u" and text[i + 2:i + 3] == "{":
                close = text.find("}", i + 3, i + 10)
                digits = text[i + 3:close] if close > 0 else ""
                if not re.fullmatch(r"[0-9A-Fa-f]{1,6}", digits) or int(digits, 16) > 0x10FFFF:
                    return None
                out.append(chr(int(digits, 16)))
                i = close + 1
            elif nxt == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", text[i + 2:i + 6]):
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
            elif nxt == "0" and not text[i + 2:i + 3].isdigit():
                out.append("\0")
                i += 2
            elif nxt == "\r":
                i += 3 if text[i + 2:i + 3] == "\n" else 2
            elif nxt in _JS_ESCAPES:
                out.append(_JS_ESCAPES[nxt])
                i += 2
            else:
                return None
            continue
        if quote == "`" and ch == "$" and text[i + 1:i + 2] == "{":
            return None                     # a template that runs code
        if ch in "\r\n" and quote != "`":
            return None
        out.append(ch)
        i += 1
    return None


def _parse_argument(text: str, start: int) -> Tuple[Optional[str], int]:
    """``(JSON text, end)``: what ``JSON.parse(`` is given at ``text[start]``, one
    string literal and the closing parenthesis; ``(None, start)`` for anything else."""
    got = _string_literal(text, start)
    if got is None:
        return None, start
    value, end = got
    close = re.match(r"\s*\)", text[end:end + 64])
    return (value, end + close.end()) if close else (None, start)


def _assigned(script: str, claimed: set, budget: List[int]) -> List[Tuple[str, Any, int]]:
    """``(name, data, chars)`` for every assignment in ``script`` whose value is JSON."""
    out: List[Tuple[str, Any, int]] = []
    inside: List[Tuple[int, int]] = []
    for m in _ASSIGNMENT.finditer(script):
        start = m.start("rhs")
        if budget[0] <= 0:
            break
        if any(a <= start < b for a, b in inside):
            continue                        # a literal inside a payload already read
        rhs = m.group("rhs")
        if rhs in ("{", "[") and not _JSON_START.match(script, start):
            continue                        # unquoted keys: a JavaScript literal, not walked
        if rhs in ("{", "["):
            literal = _json_slice(script, start)
            end = start + len(literal) if literal else start
        else:
            literal, end = _parse_argument(script, m.end("rhs"))
        if not literal or len(literal) < MIN_BLOB_CHARS:
            continue
        name = _name(m.group("name"))
        if name in claimed:
            continue                        # the first assignment to a given name wins
        budget[0] -= 1
        try:
            data = _bounded_json_loads(literal)
        except ValueError:
            # A JavaScript object literal is not JSON. Rejected on purpose:
            # see the module docstring. No eval, no ast, no JS engine.
            continue
        if not isinstance(data, (dict, list)):
            continue
        claimed.add(name)
        inside.append((start, end))
        out.append((name, data, len(literal)))
    return out


def _legacy_assignments(markup: str, names: set) -> List[Blob]:
    """The listed names assigned anywhere in ``markup`` (outside a script element too),
    as tools stored before structural finding read them."""
    out: List[Blob] = []
    claimed: set = set()
    for name in sorted(ASSIGNMENT_NAMES, key=len, reverse=True):
        if name in names:
            continue
        pattern = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*=\s*")
        for m in pattern.finditer(markup):
            start = m.end()
            if start in claimed:
                continue
            claimed.add(start)
            literal = _balanced_slice(markup, start)
            if not literal or len(literal) < MIN_BLOB_CHARS:
                continue
            try:
                data = _bounded_json_loads(literal)
            except ValueError:
                continue
            out.append(Blob(origin="assignment", locator={"kind": "assignment", "name": name},
                            data=data, raw_chars=len(literal)))
            break            # the first assignment to a given name wins
    return out


def _assignments(markup: str) -> List[Blob]:
    """Every assignment in the page's scripts whose value is JSON, found by its
    structure (:data:`_ASSIGNMENT`), named as the page names it. One payload, one
    blob: a literal inside a payload already read is not read again, and the first
    assignment to a name wins. The names of :data:`ASSIGNMENT_NAMES` come first,
    in their order; a listed name assigned outside a script element is found too."""
    out: List[Blob] = []
    claimed: set = set()
    budget = [MAX_ASSIGNMENTS]
    for attrs, body in _scripts(markup):
        if attrs.get("src") or not _JS_TYPE.match((attrs.get("type") or "").strip()):
            continue
        for name, data, chars in _assigned(body or "", claimed, budget):
            out.append(Blob(origin="assignment", locator={"kind": "assignment", "name": name},
                            data=data, raw_chars=chars))
    out += _legacy_assignments(markup, claimed)
    return sorted(out, key=lambda b: _rank(b.locator.get("name", "")))


def _rank(name: str) -> int:
    """A listed hydration name's place in :data:`ASSIGNMENT_NAMES`; any other name after them."""
    try:
        return ASSIGNMENT_NAMES.index(name)
    except ValueError:
        return len(ASSIGNMENT_NAMES)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def find_embedded(markup: str) -> List[Blob]:
    """Every JSON payload embedded in ``markup``, largest first.

    Largest first because a hydration payload carrying the result list is
    typically far bigger than a schema.org breadcrumb, and scoring the big one
    first gets to an answer sooner. The order is a preference, not a decision —
    every blob is still scored against what was on screen.
    """
    if not markup or len(markup) > MAX_MARKUP_CHARS:
        return []
    blobs = _script_islands(markup) + _assignments(markup)
    return sorted(blobs, key=lambda b: (-b.raw_chars, _rank(b.locator.get("name", ""))))


def select_blob(markup: str, locator: Dict[str, Any]) -> Any:
    """Re-find the blob ``locator`` names in a *fresh* response. ``None`` if gone.

    This is the replay half: the compile step chose a locator, and every later
    execution uses it to find the same payload in a response it has never seen.
    """
    kind = (locator or {}).get("kind")
    if kind == "assignment":
        for blob in _assignments(markup or ""):
            if blob.locator.get("name") == locator.get("name"):
                return blob.data
        return None
    if kind in ("script_id", "script_type"):
        for blob in _script_islands(markup or ""):
            loc = blob.locator
            if loc.get("kind") != kind:
                continue
            if kind == "script_id" and loc.get("id") == locator.get("id"):
                return blob.data
            if (kind == "script_type" and loc.get("type") == locator.get("type")
                    and int(loc.get("index", -1)) == int(locator.get("index", -2))):
                return blob.data
        return None
    return None


def extract(markup: str, locator: Dict[str, Any], path: str) -> Any:
    """``select_blob`` then a JSON path. ``None`` when either half misses."""
    from anyuse.execution.api_step import json_path

    data = select_blob(markup, locator)
    if data is None:
        return None
    return json_path(data, path)


def describe_locator(locator: Dict[str, Any]) -> str:
    """A one-line, human-checkable rendering for notes and error messages."""
    kind = (locator or {}).get("kind")
    if kind == "assignment":
        return f"the JSON assigned to {locator.get('name')}"
    if kind == "script_id":
        return f"<script id={locator.get('id')!r}>"
    if kind == "script_type":
        return (f"<script type={locator.get('type')!r}> "
                f"(#{int(locator.get('index', 0)) + 1} of that type)")
    return "an embedded JSON payload"
