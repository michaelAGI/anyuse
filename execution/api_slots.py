"""Nested-layer slots: a parameter that lives inside decoded layers of a request.

A plain compiled template binds a parameter to a whole query value, a path
segment or a top-level JSON/form body leaf. Real sites nest the typed value
deeper: Google's batchexecute puts it in a form field holding a JSON array
whose element is itself a JSON string, other sites send base64(JSON) state in a
query value, GraphQL ``variables`` as a JSON string, a percent-encoded
``next=/search?q=...`` URL, or a leaf that only *contains* the value
(``"from:{q} lang:en"``).

A **slot** names where a value goes as a path of decode steps, starting at the
captured request and ending at a leaf::

    {"param": "q", "at": ["form:f.req", "json:/0/0/1", "json:/1"]}
    {"param": "q", "at": ["query:state", "b64", "json:/filters/q"]}
    {"param": "q", "at": ["query:next", "query:q"]}
    {"param": "q", "at": ["query:q"], "template": "from:{q} lang:en"}
    {"param": "q", "at": ["body", "json:/query"],
     "template": "query {{ search(q: \\"{q}\\") {{ id }} }}", "escape": "json"}

The grammar:

``query[n]:<key>``  first step: the request URL's query; later: the current
                    string is a URL and this is its query. ``n`` counts a
                    repeated key's occurrences (``query:k`` is ``query[0]:k``).
``form[n]:<key>``   first step: a form-encoded body; later: the current string
                    is form-encoded.
``path:<i>``        first step only: URL path segment ``i`` (0 = after the
                    first ``/``).
``body``            first step only: the whole body text.
``json:<pointer>``  the current string is JSON; RFC 6901 pointer into it.
``b64[:<flags>]``   the current string is base64 of UTF-8 text; ``url``/``std``
                    and ``pad``/``nopad`` record what every recording showed.
``pct``             the current string is percent-encoded text.

Every step after the first decodes one layer; a slot has at most
:data:`MAX_SLOT_LAYERS` of them.

Filling (:func:`fill_request`) decodes only the layers a slot touches, sets the
leaf and re-encodes only those layers. Each re-encode splices: the decoded text
before and after the changed span keeps its original raw bytes (a decoded→raw
offset map is built for percent-encoding and for JSON string literals, a JSON
value is located by its exact span), and only the changed span is encoded, in
the layer's own style (space as ``+`` or ``%20``, characters the recording left
raw stay raw, ``\\uXXXX`` only in an all-ASCII JSON text). Key order, spacing,
``\\/`` escapes and every byte outside the touched leaf survive. A base64 layer
is the one exception: it is re-encoded whole, in the alphabet and padding the
recordings showed, and refused where they showed none the new value needs.
There is no ``{x}`` interpolation over raw captured text; a slot template's
``{{``/``}}`` are literal braces, and a templated leaf's own syntax (pairs,
query, fragment, segments) must survive the fill.

Discovery (:func:`discover_slots`) walks every decoded layer of a request, up to
:data:`MAX_SLOT_LAYERS` deep and under size caps, and returns slots for the
leaves that carry the recorded example value. :func:`deep_refusal` walks the
same layers and applies the read-only POST rung's acceptance rules to each of
them, so nesting is never a way around a rule; a request with a layer the walk
cannot read (undecodable, unparsable, deeper than the cap, past a size cap) is
refused because it cannot be inspected.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

#: Decode steps a slot may take after its container step.
MAX_SLOT_LAYERS = 4
#: A string longer than this is not decoded further.
MAX_DECODE_CHARS = 65536
#: Leaves one walk may emit before it stops (and a safety scan refuses).
MAX_WALK_LEAVES = 4096
#: Decoded text one walk may produce before it stops (and a safety scan refuses).
MAX_WALK_CHARS = 1 << 20
#: Containers one JSON layer may nest before a walk stops (and a scan refuses).
MAX_JSON_NESTING = 64
#: Shortest string tried as base64.
MIN_B64_CHARS = 8
#: Shortest example value discovery binds (``api_compile.MIN_MATCH_LEN``), in
#: ``api_compile.match_length`` units: two ideographs ("大阪") are enough.
MIN_VALUE_CHARS = 3
#: ``schema_minor`` of a template that carries slots (``schema_version`` stays 2).
SLOTS_SCHEMA_MINOR = 1

ESCAPES = ("", "url", "form", "json", "text")
#: What a ``b64:<flags>`` step may assert about the encoding, from recordings.
B64_FLAGS = ("url", "std", "pad", "nopad")
LEAF_TYPES = ("string", "number", "boolean")


class SlotError(ValueError):
    """A slot does not describe the request, or a value cannot be filled."""


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    kind: str          # query | form | path | body | json | b64 | pct
    key: str = ""      # pair key, json pointer, or path index
    index: int = 0     # occurrence of a repeated pair key

    def text(self) -> str:
        if self.kind in ("query", "form"):
            occ = f"[{self.index}]" if self.index else ""
            return f"{self.kind}{occ}:{self.key}"
        if self.kind in ("path", "json"):
            return f"{self.kind}:{self.key}"
        if self.kind == "b64" and self.key:
            return f"b64:{self.key}"
        return self.kind


_PAIR_STEP = re.compile(r"^(query|form)(?:\[(\d+)\])?:(.*)$", re.S)
_FIRST_KINDS = ("query", "form", "path", "body")
_LATER_KINDS = ("query", "form", "json", "b64", "pct")


def parse_step(raw: str) -> Step:
    raw = str(raw)
    m = _PAIR_STEP.match(raw)
    if m:
        return Step(m.group(1), m.group(3), int(m.group(2) or 0))
    if raw.startswith("json:"):
        ptr = raw[5:]
        if ptr and not ptr.startswith("/"):
            raise SlotError(f"json pointer {ptr!r} must be empty or start with '/'")
        return Step("json", ptr)
    if raw.startswith("path:"):
        if not raw[5:].isdigit():
            raise SlotError(f"path step {raw!r} needs a segment index")
        return Step("path", raw[5:])
    if raw in ("body", "b64", "pct"):
        return Step(raw)
    if raw.startswith("b64:"):
        flags = raw[4:].split(",")
        if not flags or set(flags) - set(B64_FLAGS) or len(set(flags)) != len(flags) \
                or {"url", "std"} <= set(flags) or {"pad", "nopad"} <= set(flags):
            raise SlotError(f"b64 step {raw!r}: flags are url|std and pad|nopad")
        return Step("b64", ",".join(sorted(flags)))
    raise SlotError(f"unknown slot step {raw!r}")


def parse_at(at: Sequence[str]) -> List[Step]:
    """Parse and position-check a slot path."""
    if not at:
        raise SlotError("a slot path needs at least one step")
    steps = [parse_step(s) for s in at]
    if steps[0].kind not in _FIRST_KINDS:
        raise SlotError(f"slot path must start at the request ({'/'.join(_FIRST_KINDS)}), "
                        f"not {steps[0].text()!r}")
    for s in steps[1:]:
        if s.kind not in _LATER_KINDS:
            raise SlotError(f"step {s.text()!r} may only start a slot path")
    if len(steps) - 1 > MAX_SLOT_LAYERS:
        raise SlotError(f"slot path decodes {len(steps) - 1} layers, cap is {MAX_SLOT_LAYERS}")
    return steps


def _ptr_tokens(ptr: str) -> List[str]:
    if ptr == "":
        return []
    return [t.replace("~1", "/").replace("~0", "~") for t in ptr[1:].split("/")]


def _ptr_join(tokens: Iterable[Any]) -> str:
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)


def format_at(at: Sequence[str]) -> str:
    return " > ".join(at)


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    """Where one parameter goes. ``template`` is set for a value that is only
    part of its leaf; ``escape`` says how the value is written inside that
    leaf; ``leaf`` is the recorded leaf's JSON type."""
    param: str
    at: Tuple[str, ...]
    template: Optional[str] = None
    escape: str = ""
    leaf: str = "string"

    @property
    def steps(self) -> List[Step]:
        return parse_at(self.at)

    def params(self) -> List[str]:
        if self.template is None:
            return [self.param]
        return [name for kind, name in parse_template(self.template) if kind == "hole"]

    def signature(self) -> Tuple[Any, ...]:
        return (tuple(self.at), self.template, self.escape, self.leaf)

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {"param": self.param, "at": list(self.at)}
        if self.template is not None:
            out["template"] = self.template
            out["escape"] = self.escape
        if self.leaf != "string":
            out["leaf"] = self.leaf
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Slot":
        tmpl = data.get("template")
        return cls(param=str(data.get("param") or ""),
                   at=tuple(str(s) for s in (data.get("at") or ())),
                   template=None if tmpl is None else str(tmpl),
                   escape=str(data.get("escape") or ""),
                   leaf=str(data.get("leaf") or "string"))


_HOLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_template(template: str) -> List[Tuple[str, str]]:
    """``[("text", ...), ("hole", name), ...]``; ``{{``/``}}`` are literal braces."""
    out: List[Tuple[str, str]] = []
    buf: List[str] = []
    i = 0
    while i < len(template):
        c = template[i]
        if c == "{" and template.startswith("{{", i):
            buf.append("{")
            i += 2
        elif c == "}" and template.startswith("}}", i):
            buf.append("}")
            i += 2
        elif c == "{":
            m = _HOLE.match(template, i)
            if not m:
                raise SlotError(f"slot template has a stray '{{' at {i}")
            if buf:
                out.append(("text", "".join(buf)))
                buf = []
            out.append(("hole", m.group(1)))
            i = m.end()
        elif c == "}":
            raise SlotError(f"slot template has a stray '}}' at {i}")
        else:
            buf.append(c)
            i += 1
    if buf:
        out.append(("text", "".join(buf)))
    return out


#: Characters a ``text``-escaped value may not carry: in a URL- or form-shaped
#: leaf they would start a pair, a fragment, a query, an escape or a segment.
_TEXT_FORBIDDEN = frozenset("&=#?%+/\\")


def escape_value(value: str, escape: str) -> str:
    """Write ``value`` the way its leaf holds it.

    ``""`` raw (only in a leaf with no URL or form syntax), ``url``/``form``
    percent-encoded, ``json`` as inside a ``"..."`` literal, ``text`` raw but
    refusing any character that is syntax in a URL or form leaf (the recorded
    value was raw there and had a character, a space, that ``url`` would have
    rewritten)."""
    if escape == "":
        return value
    if escape == "text":
        bad = sorted({c for c in value if c in _TEXT_FORBIDDEN or not c.isprintable()})
        if bad:
            raise SlotError(f"the value carries {''.join(bad)!r}, which is syntax in this "
                            f"URL/form-shaped leaf")
        return value
    if escape == "url":
        return urllib.parse.quote(value, safe="")
    if escape == "form":
        return urllib.parse.quote_plus(value, safe="")
    if escape == "json":
        return json.dumps(value, ensure_ascii=False)[1:-1]
    raise SlotError(f"unknown slot escape {escape!r}")


def render_template(template: str, values: Mapping[str, Any], escape: str) -> str:
    parts = []
    for kind, text in parse_template(template):
        if kind == "text":
            parts.append(text)
        else:
            if text not in values:
                raise KeyError(text)
            parts.append(escape_value(_scalar_text(values[text]), escape))
    return "".join(parts)


def _scalar_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    raise SlotError(f"a slot takes a scalar value, not {type(value).__name__}")


def _template_text(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# codecs with decoded -> raw offset maps
# ---------------------------------------------------------------------------

_HEX2 = re.compile(r"[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
#: Always percent-encoded inside a query/form value: they delimit pairs.
_PAIR_ALWAYS = frozenset("%&=+#;")
#: Always percent-encoded inside a path segment.
_PATH_ALWAYS = frozenset("%/?#;")
#: Always percent-encoded inside a ``pct`` layer.
_PCT_ALWAYS = frozenset("%")


def pct_map(raw: str, plus: bool) -> Tuple[str, List[int]]:
    """Strict percent-decoding with a map: ``starts[i]`` is the raw offset of
    decoded char ``i``; ``starts[-1] == len(raw)``. Invalid UTF-8 raises."""
    units: List[Tuple[int, int]] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "%" and _HEX2.fullmatch(raw[i + 1:i + 3]):
            units.append((int(raw[i + 1:i + 3], 16), i))
            i += 3
        elif c == "+" and plus:
            units.append((0x20, i))
            i += 1
        else:
            for b in c.encode("utf-8", "surrogatepass"):
                units.append((b, i))
            i += 1
    chars: List[str] = []
    starts: List[int] = []
    j = 0
    while j < len(units):
        b0 = units[j][0]
        size = (1 if b0 < 0x80 else 2 if 0xC0 <= b0 < 0xE0 else 3 if 0xE0 <= b0 < 0xF0
                else 4 if 0xF0 <= b0 < 0xF8 else 0)
        if not size or j + size > len(units):
            raise SlotError("percent-encoded text is not UTF-8")
        try:
            ch = bytes(u[0] for u in units[j:j + size]).decode("utf-8")
        except UnicodeDecodeError:
            raise SlotError("percent-encoded text is not UTF-8") from None
        chars.append(ch)
        starts.append(units[j][1])
        j += size
    starts.append(n)
    return "".join(chars), starts


def pct_decode(raw: str, plus: bool = False) -> str:
    return pct_map(raw, plus)[0]


def pct_encode(text: str, sample: str, plus: bool, always: frozenset) -> str:
    """Percent-encode ``text`` in the style of ``sample`` (the layer's raw text):
    unreserved characters stay raw, a printable ASCII character the sample
    carries raw stays raw unless it is structural for the layer, space is ``+``
    where the sample used ``+``, hex case follows the sample."""
    raw_chars = set(sample)
    space = "+" if (plus and "+" in sample) else "%20"
    lower = bool(re.search(r"%[0-9a-f][a-f]|%[a-f][0-9a-f]", sample)) and \
        not re.search(r"%[0-9A-F][A-F]|%[A-F][0-9A-F]", sample)
    fmt = "%%%02x" if lower else "%%%02X"
    out: List[str] = []
    for ch in text:
        if ch in _UNRESERVED:
            out.append(ch)
        elif ch == " ":
            out.append(space if space == "+" else fmt % 0x20)
        elif (ord(ch) < 0x7F and ch.isprintable() and ch not in always
              and ch in raw_chars):
            out.append(ch)
        else:
            out.append("".join(fmt % b for b in ch.encode("utf-8", "surrogatepass")))
    return "".join(out)


_JSON_ESC = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n",
             "r": "\r", "t": "\t"}


def json_string_map(literal: str) -> Tuple[str, List[int]]:
    """Decode a JSON string literal (quotes included) with a map from decoded
    char to raw offset; ``starts[-1]`` is the closing quote's offset."""
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        raise SlotError("not a JSON string literal")
    out: List[str] = []
    starts: List[int] = []
    i, end = 1, len(literal) - 1
    while i < end:
        c = literal[i]
        if c == "\\":
            e = literal[i + 1]
            if e == "u":
                cp = int(literal[i + 2:i + 6], 16)
                j = i + 6
                if 0xD800 <= cp < 0xDC00 and literal[j:j + 2] == "\\u":
                    lo = int(literal[j + 2:j + 6], 16)
                    if 0xDC00 <= lo < 0xE000:
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                        j += 6
                out.append(chr(cp))
                starts.append(i)
                i = j
            else:
                if e not in _JSON_ESC:
                    raise SlotError(f"bad JSON escape \\{e}")
                out.append(_JSON_ESC[e])
                starts.append(i)
                i += 2
        else:
            out.append(c)
            starts.append(i)
            i += 1
    starts.append(end)
    return "".join(out), starts


_WS = re.compile(r"[ \t\n\r]*")
_DECODER = json.JSONDecoder()


def json_span(text: str, tokens: Sequence[str]) -> Tuple[int, int]:
    """Exact ``(start, end)`` of the value at ``tokens`` in JSON ``text``.

    Duplicate object keys resolve to the LAST occurrence, as ``json.loads``
    reads them, so the span filled is the value the server would read.
    """
    try:
        idx = _WS.match(text, 0).end()
        for tok in tokens:
            ch = text[idx:idx + 1]
            if ch == "{":
                idx = _WS.match(text, idx + 1).end()
                found = None
                if text[idx:idx + 1] != "}":
                    while True:
                        if text[idx:idx + 1] != '"':
                            raise SlotError("malformed JSON object")
                        key, idx = json.decoder.scanstring(text, idx + 1)
                        idx = _WS.match(text, idx).end()
                        if text[idx:idx + 1] != ":":
                            raise SlotError("malformed JSON object")
                        idx = _WS.match(text, idx + 1).end()
                        start = idx
                        _v, idx = _DECODER.raw_decode(text, idx)
                        if key == tok:
                            found = start
                        idx = _WS.match(text, idx).end()
                        sep = text[idx:idx + 1]
                        if sep == ",":
                            idx = _WS.match(text, idx + 1).end()
                            continue
                        if sep == "}":
                            break
                        raise SlotError("malformed JSON object")
                if found is None:
                    raise SlotError(f"JSON object has no key {tok!r}")
                idx = found
            elif ch == "[":
                if not re.fullmatch(r"0|[1-9][0-9]*", tok):
                    raise SlotError(f"JSON array index {tok!r} is not a number")
                want = int(tok)
                idx = _WS.match(text, idx + 1).end()
                n = 0
                found = None
                if text[idx:idx + 1] != "]":
                    while True:
                        start = idx
                        _v, idx = _DECODER.raw_decode(text, idx)
                        if n == want:
                            found = start
                            break
                        n += 1
                        idx = _WS.match(text, idx).end()
                        sep = text[idx:idx + 1]
                        if sep == ",":
                            idx = _WS.match(text, idx + 1).end()
                            continue
                        break
                if found is None:
                    raise SlotError(f"JSON array has no element {want}")
                idx = found
            else:
                raise SlotError(f"JSON pointer token {tok!r} reaches a scalar")
        _v, end = _DECODER.raw_decode(text, idx)
        return idx, end
    except (ValueError, IndexError, RecursionError) as exc:
        if isinstance(exc, SlotError):
            raise
        raise SlotError(f"JSON text does not parse: {exc}") from None


_B64_RE = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")


def b64_style(raw: str) -> Tuple[str, Optional[bool]]:
    """What one recorded base64 string shows about its encoding.

    ``alphabet`` is ``"url"`` (it holds ``-``/``_``), ``"std"`` (``+``/``/``) or
    ``""`` (neither: it cannot tell). ``padded`` is True (it ends in ``=``),
    False (its length needs padding and has none) or None (no padding was
    needed: it cannot tell).
    """
    alphabet = "url" if ("-" in raw or "_" in raw) else "std" if ("+" in raw or "/" in raw) else ""
    if raw.endswith("="):
        return alphabet, True
    return alphabet, (False if len(raw) % 4 else None)


def b64_merge(styles: Iterable[Tuple[str, Optional[bool]]]) -> Tuple[str, Optional[bool]]:
    """One style from several observations; contradicting ones raise."""
    styles = list(styles)
    alphabets = {a for a, _p in styles if a}
    pads = {p for _a, p in styles if p is not None}
    if len(alphabets) > 1:
        raise SlotError("the recordings use both base64 alphabets at this layer")
    if len(pads) > 1:
        raise SlotError("the recordings pad this base64 layer both ways")
    return (alphabets.pop() if alphabets else ""), (pads.pop() if pads else None)


def b64_flags(style: Tuple[str, Optional[bool]]) -> str:
    alphabet, padded = style
    flags = ([alphabet] if alphabet else []) + ([] if padded is None
                                                else ["pad" if padded else "nopad"])
    return ",".join(sorted(flags))


def _flags_style(flags: str) -> Tuple[str, Optional[bool]]:
    parts = set(filter(None, (flags or "").split(",")))
    alphabet = "url" if "url" in parts else "std" if "std" in parts else ""
    padded = True if "pad" in parts else False if "nopad" in parts else None
    return alphabet, padded


def b64_decode_text(raw: str) -> str:
    if not _B64_RE.fullmatch(raw) or (("-" in raw or "_" in raw)
                                      and ("+" in raw or "/" in raw)):
        raise SlotError("not base64")
    body = raw.rstrip("=")
    if len(body) % 4 == 1:
        raise SlotError("not base64")
    padded = body + "=" * (-len(body) % 4)
    try:
        data = (base64.urlsafe_b64decode(padded) if ("-" in raw or "_" in raw)
                else base64.b64decode(padded, validate=True))
        return data.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise SlotError("not base64 of UTF-8 text") from None


def b64_encode_text(text: str, alphabet: str, padded: Optional[bool]) -> str:
    """Encode in the recorded style. Where the new encoding needs a character
    or padding no recording showed (``""`` / ``None``), it refuses rather than
    guess: a server reading URL-safe base64 rejects ``+``, one reading padded
    base64 may reject a missing ``=``."""
    body = base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii").rstrip("=")
    if "+" in body or "/" in body:
        if alphabet == "url":
            body = body.replace("+", "-").replace("/", "_")
        elif alphabet != "std":
            raise SlotError("the new base64 needs '+'/'/' or '-'/'_', and no recording "
                            "showed which alphabet this layer uses")
    if len(body) % 4:
        if padded is None:
            raise SlotError("the new base64 needs padding, and no recording showed "
                            "whether this layer is padded")
        if padded:
            body += "=" * (-len(body) % 4)
    return body


def _affixes(old: str, new: str) -> Tuple[int, int]:
    p = 0
    lim = min(len(old), len(new))
    while p < lim and old[p] == new[p]:
        p += 1
    s = 0
    lim -= p
    while s < lim and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    return p, s


def _splice(raw: str, old: str, new: str, starts: List[int], encode) -> str:
    """Replace only the changed span of a decoded layer in its raw text."""
    if old == new:
        return raw
    p, s = _affixes(old, new)
    ra, rb = starts[p], starts[len(old) - s]
    return raw[:ra] + encode(new[p:len(new) - s]) + raw[rb:]


# ---------------------------------------------------------------------------
# url and pair containers
# ---------------------------------------------------------------------------

def split_url(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    """``(head, query, fragment)`` by plain string split; None when absent."""
    head, hsep, frag = url.partition("#")
    head, qsep, query = head.partition("?")
    return head, (query if qsep else None), (frag if hsep else None)


def join_url(head: str, query: Optional[str], frag: Optional[str]) -> str:
    out = head
    if query is not None:
        out += "?" + query
    if frag is not None:
        out += "#" + frag
    return out


@dataclass
class _Pair:
    raw_key: str
    start: int
    vstart: int
    vend: int
    has_eq: bool


def raw_pairs(text: str) -> List[_Pair]:
    out: List[_Pair] = []
    pos = 0
    for part in text.split("&"):
        start = pos
        pos += len(part) + 1
        if not part:
            continue
        eq = part.find("=")
        if eq < 0:
            out.append(_Pair(part, start, start + len(part), start + len(part), False))
        else:
            out.append(_Pair(part[:eq], start, start + eq + 1, start + len(part), True))
    return out


def decoded_pairs(text: str) -> List[Tuple[str, int, _Pair]]:
    """``(decoded key, occurrence, pair)``; a key that does not decode is skipped."""
    seen: Dict[str, int] = {}
    out = []
    for pair in raw_pairs(text):
        try:
            key = pct_decode(pair.raw_key, plus=True)
        except SlotError:
            continue
        occ = seen.get(key, 0)
        seen[key] = occ + 1
        out.append((key, occ, pair))
    return out


def _pair_step(kind: str, key: str, occ: int) -> str:
    return Step(kind, key, occ).text()


def path_bounds(head: str) -> Tuple[int, int]:
    """``(start, end)`` of the path inside a URL head (scheme://host/path)."""
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*", head)
    start = m.end() if m else 0
    return start, len(head)


def drop_query_keys(url: str, keys: Iterable[str]) -> str:
    """Remove every pair whose decoded key is in ``keys``; other bytes stay."""
    keys = set(keys)
    if not keys:
        return url
    head, query, frag = split_url(url)
    if query is None:
        return url
    kept, dropped = [], False
    for pair in raw_pairs(query):
        try:
            key = pct_decode(pair.raw_key, plus=True)
        except SlotError:
            key = None
        if key is not None and key in keys:
            dropped = True
        else:
            kept.append(query[pair.start:pair.vend])
    if not dropped:
        return url
    return join_url(head, "&".join(kept) if kept else None, frag)


# ---------------------------------------------------------------------------
# fill
# ---------------------------------------------------------------------------

_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


class _Writer:
    """Produce the new leaf from the old one. For a JSON number or boolean
    leaf it returns the new JSON literal; for every other leaf the new text."""

    def __init__(self, slot: Slot, values: Mapping[str, Any]):
        self.slot = slot
        self.values = values
        self.wrote: List[Any] = []
        self.old: Any = None

    def __call__(self, old: Any) -> str:
        slot = self.slot
        self.old = old
        if slot.template is not None:
            if not isinstance(old, str):
                raise SlotError(f"templated slot {format_at(slot.at)} reaches a "
                                f"{type(old).__name__} leaf, not a string")
            if slot.escape != "json" and _looks_url(old):
                _no_dot_segments(slot.template, self.values)
            new = render_template(slot.template, self.values, slot.escape)
            if leaf_structure(new) != leaf_structure(old):
                raise SlotError(f"filling {format_at(slot.at)} would change its leaf's "
                                f"syntax (pairs, query, fragment, segments)")
            self.wrote.append(new)
            return new
        if slot.param not in self.values:
            raise KeyError(slot.param)
        value = self.values[slot.param]
        if slot.leaf == "number":
            if isinstance(old, bool) or not isinstance(old, (int, float)):
                raise SlotError(f"slot {format_at(slot.at)} expects a JSON number leaf")
            if isinstance(value, bool):
                raise SlotError(f"{slot.param!r} must be a number for this slot")
            text = json.dumps(value) if isinstance(value, (int, float)) else str(value)
            if not _JSON_NUMBER.fullmatch(text):
                raise SlotError(f"{slot.param!r} must be a JSON number for this slot")
            self.wrote.append(json.loads(text))
            return text
        if slot.leaf == "boolean":
            if not isinstance(old, bool):
                raise SlotError(f"slot {format_at(slot.at)} expects a JSON boolean leaf")
            if isinstance(value, bool):
                flag = value
            elif str(value).lower() in ("true", "false"):
                flag = str(value).lower() == "true"
            else:
                raise SlotError(f"{slot.param!r} must be true or false for this slot")
            self.wrote.append(flag)
            return "true" if flag else "false"
        if not isinstance(old, str):
            raise SlotError(f"slot {format_at(slot.at)} expects a string leaf, found "
                            f"{type(old).__name__}")
        new = _scalar_text(value)
        self.wrote.append(new)
        return new


class _Reader:
    """A writer that records the leaf and leaves it unchanged."""

    def __init__(self):
        self.leaf: Any = None
        self.seen = False

    def __call__(self, old: Any) -> Any:
        self.leaf, self.seen = old, True
        return old


def _fill_text(text: str, steps: Sequence[Step], fn) -> str:
    if not steps:
        new = fn(text)
        if not isinstance(new, str):
            raise SlotError("a text leaf takes a string")
        return new
    step, rest = steps[0], steps[1:]
    if step.kind == "json":
        return _fill_json(text, step, rest, fn)
    if step.kind == "b64":
        decoded = b64_decode_text(text)
        new = _fill_text(decoded, rest, fn)
        if new == decoded:
            return text
        alphabet, padded = b64_merge([b64_style(text), _flags_style(step.key)])
        return b64_encode_text(new, alphabet, padded)
    if step.kind == "pct":
        decoded, starts = pct_map(text, plus=False)
        new = _fill_text(decoded, rest, fn)
        return _splice(text, decoded, new, starts,
                       lambda mid: pct_encode(mid, text, False, _PCT_ALWAYS))
    if step.kind == "query":
        head, query, frag = split_url(text)
        if query is None:
            raise SlotError(f"step {step.text()!r}: the string is not a URL with a query")
        return join_url(head, _fill_pairs(query, step, rest, fn), frag)
    if step.kind == "form":
        return _fill_pairs(text, step, rest, fn)
    raise SlotError(f"step {step.text()!r} cannot decode a nested string")


def _fill_pairs(text: str, step: Step, rest: Sequence[Step], fn) -> str:
    for key, occ, pair in decoded_pairs(text):
        if key != step.key or occ != step.index:
            continue
        raw_val = text[pair.vstart:pair.vend]
        decoded, starts = pct_map(raw_val, plus=True)
        new = _fill_text(decoded, rest, fn)
        new_raw = _splice(raw_val, decoded, new, starts,
                          lambda mid: pct_encode(mid, raw_val, True, _PAIR_ALWAYS))
        if new_raw == raw_val:
            return text
        if not pair.has_eq:
            return text[:pair.vend] + "=" + new_raw + text[pair.vend:]
        return text[:pair.vstart] + new_raw + text[pair.vend:]
    raise SlotError(f"no pair {step.text()!r} in the {step.kind} text")


def _fill_json(text: str, step: Step, rest: Sequence[Step], fn) -> str:
    start, end = json_span(text, _ptr_tokens(step.key))
    raw_val = text[start:end]
    value = json.loads(raw_val)
    ascii_only = text.isascii()
    if rest:
        if not isinstance(value, str):
            raise SlotError(f"step {step.text()!r} reaches a {type(value).__name__}, "
                            f"not a string to decode further")
        inner = _fill_text(value, rest, fn)
        new_raw = _splice_json_string(raw_val, value, inner, ascii_only)
    else:
        new = fn(value)
        if isinstance(value, str):
            if not isinstance(new, str):
                raise SlotError("a JSON string leaf stays a string")
            new_raw = _splice_json_string(raw_val, value, new, ascii_only)
        elif isinstance(value, (bool, int, float)):
            if new is value:              # read-only walk
                return text
            new_raw = str(new)
        else:
            raise SlotError(f"step {step.text()!r} reaches a JSON "
                            f"{type(value).__name__}, not a scalar leaf")
    if new_raw == raw_val:
        return text
    return text[:start] + new_raw + text[end:]


def _splice_json_string(literal: str, old: str, new: str, ascii_only: bool) -> str:
    decoded, starts = json_string_map(literal)
    if decoded != old:
        raise SlotError("JSON string literal does not decode as parsed")
    return _splice(literal, old, new, starts,
                   lambda mid: json.dumps(mid, ensure_ascii=ascii_only)[1:-1])


def _fill_url_path(url: str, step: Step, rest: Sequence[Step], fn) -> str:
    head, query, frag = split_url(url)
    pstart, pend = path_bounds(head)
    path = head[pstart:pend]
    segs = path.split("/")
    i = int(step.key) + 1
    if not path.startswith("/") or i >= len(segs):
        raise SlotError(f"the URL has no path segment {step.key}")
    raw = segs[i]
    decoded, starts = pct_map(raw, plus=False)
    new = _fill_text(decoded, rest, fn)
    if not new:
        raise SlotError("a path segment may not be filled empty")
    if not rest and new != decoded and new in (".", ".."):
        raise SlotError(f"{new!r} is a relative path segment")
    segs[i] = _splice(raw, decoded, new, starts,
                      lambda mid: pct_encode(mid, raw, False, _PATH_ALWAYS))
    return join_url(head[:pstart] + "/".join(segs), query, frag)


def _apply(url: str, body: Optional[str], steps: Sequence[Step], fn
           ) -> Tuple[str, Optional[str]]:
    try:
        return _apply_steps(url, body, steps, fn)
    except RecursionError:
        raise SlotError("a decoded layer nests too deeply") from None


def _apply_steps(url: str, body: Optional[str], steps: Sequence[Step], fn
                 ) -> Tuple[str, Optional[str]]:
    first, rest = steps[0], steps[1:]
    if first.kind == "query":
        head, query, frag = split_url(url)
        if query is None:
            raise SlotError(f"the request URL has no query for {first.text()!r}")
        return join_url(head, _fill_pairs(query, first, rest, fn), frag), body
    if first.kind == "path":
        return _fill_url_path(url, first, rest, fn), body
    if body is None:
        raise SlotError(f"slot step {first.text()!r} addresses a request without a body")
    if first.kind == "form":
        return url, _fill_pairs(body, first, rest, fn)
    return url, _fill_text(body, rest, fn)


def read_slot(url: str, body: Optional[str], at: Sequence[str]) -> Any:
    """The decoded leaf a slot path reaches in a request."""
    reader = _Reader()
    _apply(url, body, parse_at(at), reader)
    if not reader.seen:
        raise SlotError(f"slot {format_at(at)} reaches nothing")
    return reader.leaf


def fill_request(url: str, body: Optional[str], slots: Sequence[Any],
                 values: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
    """Fill every slot; returns the new ``(url, body)``.

    Each slot is applied to the request the previous one produced (slots are
    addressed structurally, so filling one never moves another), then read
    back: a leaf that does not decode to what was written raises
    :class:`SlotError` rather than reaching the wire.
    """
    for raw in slots:
        slot = raw if isinstance(raw, Slot) else Slot.from_dict(raw)
        writer = _Writer(slot, values)
        url, body = _apply(url, body, slot.steps, writer)
        if writer.wrote and not _same_leaf(read_slot(url, body, slot.at), writer.wrote[-1]):
            raise SlotError(f"slot {format_at(slot.at)} did not read back as written")
    return url, body


def _leaf_kind(leaf: Any) -> str:
    if isinstance(leaf, bool):
        return "boolean"
    if isinstance(leaf, (int, float)):
        return "number"
    return "string"


def replace_leaf(url: str, body: Optional[str], at: Sequence[str], expected: str,
                 new: str) -> Tuple[str, Optional[str]]:
    """Swap one leaf of a request, keeping its JSON type; the leaf must
    currently read ``expected``. Used to swap a rotated token in place."""
    leaf = read_slot(url, body, at)
    if _scalar_text(leaf) != expected:
        raise SlotError(f"{format_at(at)} reads {_scalar_text(leaf)!r}, not {expected!r}")
    slot = Slot(param="value", at=tuple(at), leaf=_leaf_kind(leaf))
    return fill_request(url, body, [slot], {"value": new})


def replace_in_text(text: str, at: Sequence[str], expected: str, new: str) -> str:
    """:func:`replace_leaf` inside an already-decoded string (``at`` holds
    only nested steps: json/b64/pct/query/form)."""
    steps = [parse_step(s) for s in at]
    for s in steps:
        if s.kind not in _LATER_KINDS:
            raise SlotError(f"step {s.text()!r} does not decode a string")
    reader = _Reader()
    _fill_text(text, steps, reader)
    if not reader.seen or _scalar_text(reader.leaf) != expected:
        raise SlotError(f"{format_at(at) or 'the value'} does not read {expected!r}")
    slot = Slot(param="value", at=("body",) + tuple(at), leaf=_leaf_kind(reader.leaf))
    writer = _Writer(slot, {"value": new})
    out = _fill_text(text, steps, writer)
    check = _Reader()
    _fill_text(out, steps, check)
    if not _same_leaf(check.leaf, writer.wrote[-1]):
        raise SlotError("the value did not read back as written")
    return out


def _no_dot_segments(template: str, values: Mapping[str, Any]) -> None:
    """A hole in a URL leaf may not be a relative path segment: the rule a
    single-segment path parameter already follows
    (``param_binding.bind_path_value``)."""
    from anyuse.execution.param_binding import PathBindError, bind_path_value
    for kind, name in parse_template(template):
        if kind == "hole" and name in values:
            try:
                bind_path_value(_scalar_text(values[name]), 1)
            except PathBindError as exc:
                raise SlotError(f"{name!r} in a URL leaf: {exc}") from None


def _segment_class(seg: str) -> str:
    try:
        seg = pct_decode(seg, plus=False)
    except SlotError:
        return "unreadable"
    return seg if seg in ("", ".", "..") else "segment"


def leaf_structure(text: str) -> Tuple[Any, ...]:
    """The syntax of a templated leaf at its own level: a URL's scheme and host,
    its path segments (each classed as empty, ``.``, ``..`` after decoding, or
    a segment), its query and fragment keys; a pair string's keys; a JSON
    text's key paths. Percent-encoded or base64 text counts as text: an
    escaped value is data. A fill may change values, never this."""
    kind, payload = _decode_once(text)
    if kind == "query" or (kind not in ("json", "bad") and _looks_url(text)):
        head, query, frag = split_url(text)
        start, _end = path_bounds(head)
        keys = lambda part: (None if part is None else
                             tuple(p.raw_key for p in raw_pairs(part)))
        return ("url", head[:start],
                tuple(_segment_class(seg) for seg in head[start:].split("/")),
                keys(query), keys(frag))
    if kind == "form":
        return ("form", tuple(p.raw_key for p in raw_pairs(text)))
    if kind == "json":
        return ("json", _json_shape(payload))
    if kind == "bad":
        return ("unreadable",)
    return ("text",)


def _json_shape(node: Any) -> Any:
    if isinstance(node, _Obj):
        return tuple((k, _json_shape(v)) for k, v in node)
    if isinstance(node, list):
        return tuple(_json_shape(v) for v in node)
    return type(node).__name__


def _same_leaf(got: Any, want: Any) -> bool:
    if isinstance(want, bool) or isinstance(got, bool):
        return isinstance(got, bool) and isinstance(want, bool) and got == want
    if isinstance(want, (int, float)):
        return isinstance(got, (int, float)) and got == want
    return isinstance(got, str) and got == want


# ---------------------------------------------------------------------------
# walking every decoded layer
# ---------------------------------------------------------------------------

@dataclass
class Leaf:
    at: Tuple[str, ...]
    value: Any
    layers: int
    #: the JSON key or pair key this leaf sits under ("" for array items)
    key: str = ""
    #: True when the leaf decoded into a deeper layer (it is a container)
    decoded: bool = False
    #: False where a slot may not be bound: a path segment's own text (the
    #: path binding's job), a fragment, a shadowed duplicate JSON key
    bindable: bool = True


@dataclass
class Walk:
    leaves: List[Leaf] = field(default_factory=list)
    #: every object key and pair key, with the path of the layer it is in
    keys: List[Tuple[Tuple[str, ...], str]] = field(default_factory=list)
    #: strings that decode further than the layer cap allowed
    deeper: List[Tuple[str, ...]] = field(default_factory=list)
    #: why the walk stopped early or could not read a layer ("" when it saw everything)
    truncated: str = ""
    chars: int = 0


class _Obj(list):
    """A JSON object as its ``(key, value)`` pairs, duplicates kept."""


def loads_pairs(text: str) -> Any:
    """``json.loads`` keeping every pair of every object, duplicates included."""
    return json.loads(text, object_pairs_hook=_Obj)


_URLQ_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#]*)?[^\s?#]*\?[^\s#]*=[^\s#]*(?:#\S*)?$")
#: A URL that starts like one (a scheme or a "/") may hold raw spaces after it.
_URLQ_LOOSE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#]+|/)[^?#]*\?[^#]*=")
_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_JSONISH = re.compile(r"^\s*(?:\{|\[\s*[\[{\"])")


def _decode_once(text: str) -> Tuple[str, Any]:
    """How ``text`` decodes one layer: ``("json", value)``, ``("b64", text)``,
    ``("query", None)``, ``("form", None)``, ``("pct", text)``, ``("", None)``
    for plain text, or ``("bad", reason)`` for a string shaped like a layer
    that no decoder reads (a walk that meets one cannot see to the bottom)."""
    if not text or len(text) > MAX_DECODE_CHARS:
        return "", None
    if _GQL_DOC.match(text):
        return "", None                     # a GraphQL document: judged as one, not split
    lead = text.lstrip()[:1]
    if lead in ("{", "["):
        try:
            return "json", loads_pairs(text)
        except RecursionError:
            return "bad", "JSON nested too deeply to parse"
        except ValueError:
            pass
    if _JSONISH.match(text):
        # never read as pairs or a URL: '=' inside unparsable JSON is not syntax
        return "bad", "a JSON-shaped string that does not parse"
    if len(text) >= MIN_B64_CHARS and _B64_RE.fullmatch(text):
        try:
            decoded = b64_decode_text(text)
        except SlotError:
            decoded = None
        if decoded is not None and decoded.lstrip()[:1] in ("{", "["):
            try:
                json.loads(decoded)
                return "b64", decoded
            except (ValueError, RecursionError):
                pass
    if "?" in text and (_URLQ_RE.match(text) or _URLQ_LOOSE.match(text)):
        return "query", None
    if "=" in text and not _B64_RE.fullmatch(text):
        return "form", None                 # any k=v[&k=v]: every key is read
    if _PCT_RE.search(text):
        try:
            decoded = pct_decode(text, plus=False)
        except SlotError:
            return "bad", "percent-encoded text that is not UTF-8"
        if decoded != text:
            return "pct", decoded
    return "", None


def _looks_decodable(text: str) -> bool:
    if len(text) > MAX_DECODE_CHARS:
        lead = text.lstrip()[:1]
        return lead in ("{", "[") or "%" in text or "=" in text \
            or bool(_B64_RE.fullmatch(text[:256]))
    return bool(_decode_once(text)[0])


#: bind modes: "yes" leaves are bindable; "path" not until a json/b64 layer;
#: "no" never (a fragment, a shadowed duplicate key)
_BIND_YES, _BIND_PATH, _BIND_NO = "yes", "path", "no"


class _Walker:
    def __init__(self, max_layers: int):
        self.max_layers = max_layers
        self.w = Walk()

    def _budget(self, add_chars: int = 0) -> bool:
        w = self.w
        if w.truncated:
            return False
        w.chars += add_chars
        if len(w.leaves) >= MAX_WALK_LEAVES:
            w.truncated = f"more than {MAX_WALK_LEAVES} leaves"
            return False
        if w.chars > MAX_WALK_CHARS:
            w.truncated = f"more than {MAX_WALK_CHARS} decoded characters"
            return False
        return True

    def _stop(self, why: str) -> None:
        if not self.w.truncated:
            self.w.truncated = why

    def leaf(self, at, value, layers, key="", bind=_BIND_YES) -> Optional[Leaf]:
        if not self._budget():
            return None
        lf = Leaf(tuple(at), value, layers, key, bindable=(bind == _BIND_YES))
        self.w.leaves.append(lf)
        return lf

    def descend(self, text: str, at: Tuple[str, ...], layers: int,
                lf: Optional[Leaf], bind: str = _BIND_YES) -> None:
        if not isinstance(text, str) or not text or not self._budget():
            return
        if len(text) > MAX_DECODE_CHARS:
            if _looks_decodable(text):
                self._stop(f"a string at {format_at(at)} is over the "
                           f"{MAX_DECODE_CHARS}-character decode cap")
            return
        kind, payload = _decode_once(text)
        if not kind:
            return
        if lf is not None:
            lf.decoded = True             # a container is never matched as a leaf
        if kind == "bad":
            self._stop(f"{payload} at {format_at(at)}")
            return
        if layers + 1 > self.max_layers:
            self.w.deeper.append(tuple(at))
            return
        structured = _BIND_YES if bind == _BIND_PATH else bind
        if kind == "json":
            self._budget(len(text))
            self.json_value(payload, at, "", layers + 1, "", structured)
        elif kind == "b64":
            self._budget(len(payload))
            self.descend_container(payload, at + ("b64",), layers + 1, structured)
        elif kind == "query":
            head, query, frag = split_url(text)
            self.pairs(query or "", "query", at, layers + 1, bind)
            if frag:
                self.fragment(frag, at, layers + 1)
        elif kind == "form":
            self.pairs(text, "form", at, layers + 1, bind)
        elif kind == "pct":
            self._budget(len(payload))
            child_at = at + ("pct",)
            child = self.leaf(child_at, payload, layers + 1, bind=bind)
            self.descend(payload, child_at, layers + 1, child, bind)

    def descend_container(self, text: str, at: Tuple[str, ...], layers: int,
                          bind: str) -> None:
        """``text`` is already one decoded layer (b64): parse it, at ``layers``."""
        kind, payload = _decode_once(text)
        if kind == "bad":
            self._stop(f"{payload} at {format_at(at)}")
        elif kind == "json":
            if layers + 1 > self.max_layers:
                self.w.deeper.append(tuple(at))
                return
            self.json_value(payload, at, "", layers + 1, "", bind)

    def json_value(self, node: Any, at: Tuple[str, ...], ptr: str, layers: int,
                   key: str, bind: str = _BIND_YES, depth: int = 0) -> None:
        if not self._budget():
            return
        if depth > MAX_JSON_NESTING:
            self._stop(f"JSON at {format_at(at)} nests deeper than {MAX_JSON_NESTING}")
            return
        step = at + ("json:" + ptr,)
        if isinstance(node, _Obj):
            last = {k: i for i, (k, _v) in enumerate(node)}
            for i, (k, v) in enumerate(node):
                self.w.keys.append((step, str(k)))
                # an earlier duplicate is what a lenient server may read: walk
                # it, but a pointer reaches only the last, so never bind it
                child_bind = bind if last[k] == i else _BIND_NO
                self.json_value(v, at, ptr + _ptr_join([k]), layers, str(k),
                                child_bind, depth + 1)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self.json_value(v, at, ptr + f"/{i}", layers, "", bind, depth + 1)
        else:
            lf = self.leaf(step, node, layers, key, bind)
            if isinstance(node, str) and lf is not None:
                self.descend(node, step, layers, lf, bind)

    def pairs(self, text: str, kind: str, at: Tuple[str, ...], layers: int,
              bind: str = _BIND_YES) -> None:
        seen: Dict[str, int] = {}
        for pair in raw_pairs(text):
            if not self._budget():
                return
            try:
                key = pct_decode(pair.raw_key, plus=True)
                value = pct_decode(text[pair.vstart:pair.vend], plus=True)
            except SlotError:
                self._stop(f"a pair of {format_at(at) or 'the request'} does not "
                           f"percent-decode as UTF-8")
                return
            occ = seen.get(key, 0)
            seen[key] = occ + 1
            self.w.keys.append((at, key))
            step = at + (_pair_step(kind, key, occ),)
            lf = self.leaf(step, value, layers, key, bind)
            self.descend(value, step, layers, lf, bind)

    def fragment(self, frag: str, at: Tuple[str, ...], layers: int) -> None:
        """A URL's fragment: read like a pair string or a plain text, never bound."""
        step = at + ("#fragment",)
        try:
            text = pct_decode(frag, plus=False)
        except SlotError:
            self._stop(f"the fragment at {format_at(at) or 'the request'} does not "
                       f"percent-decode as UTF-8")
            return
        if "=" in frag and not _JSONISH.match(text):
            self.pairs(frag, "form", step, layers, _BIND_NO)
        else:
            lf = self.leaf(step, text, layers, "", _BIND_NO)
            self.descend(text, step, layers, lf, _BIND_NO)


def _body_kind(body: str) -> str:
    """``"json"``, ``"form"`` or ``""``, judged on the stripped body, as
    ``api_step.readonly_post_shape`` judges it (a trailing newline is no reason
    to leave a form body unread)."""
    stripped = body.strip()
    if stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
            return "json"
        except (ValueError, RecursionError):
            return ""
    if "=" in stripped and "\n" not in stripped:
        return "form"
    return ""


def walk_request(url: str, body: Optional[str] = None,
                 max_layers: int = MAX_SLOT_LAYERS) -> Walk:
    """Every leaf and key of a request, through up to ``max_layers`` decodes.

    Anything the walk cannot read (a key or value that does not
    percent-decode, a JSON-shaped string that does not parse, a size or depth
    cap) sets ``truncated``. Duplicate JSON keys are all walked. Fragments are
    walked. A path segment's own text and its pct/form/query layers yield
    leaves that are never bindable (a value that is part of a path is the
    path binding's job); below a json/b64 layer they are.
    """
    walker = _Walker(max_layers)
    head, query, frag = split_url(url or "")
    pstart, pend = path_bounds(head)
    path = head[pstart:pend]
    for i, seg in enumerate(path.split("/")[1:] if path.startswith("/") else []):
        if not seg:
            continue
        try:
            decoded = pct_decode(seg, plus=False)
        except SlotError:
            walker._stop(f"path segment {i} does not percent-decode as UTF-8")
            break
        walker.descend(decoded, (f"path:{i}",), 0, None, _BIND_PATH)
    if query:
        walker.pairs(query, "query", (), 0)
    if frag:
        walker.fragment(frag, (), 0)
    if body:
        kind = _body_kind(body)
        if kind == "json":
            if len(body) > MAX_DECODE_CHARS:
                walker._stop(f"the body is over the {MAX_DECODE_CHARS}-character cap")
            else:
                walker.json_value(loads_pairs(body), ("body",), "", 1, "")
        elif kind == "form":
            walker.pairs(body.strip(), "form", (), 0)
        elif body.lstrip()[:1] in ("{", "["):
            walker._stop("the body looks like JSON but does not parse")
        else:
            walker._stop("the body is neither JSON nor a form encoding")
    return walker.w


# ---------------------------------------------------------------------------
# the acceptance rules over every decoded layer
# ---------------------------------------------------------------------------

#: A GraphQL document: an operation, an anonymous selection, or a fragment,
#: after GraphQL's ignored tokens (whitespace, commas, a BOM, ``#`` comments).
_GQL_IGNORED = r"(?:[\s,\ufeff]|#[^\n]*(?:\n|$))*"
_GQL_DOC = re.compile(
    rf"^{_GQL_IGNORED}(?:(?:query|mutation|subscription)\b[^{{]*\{{|\{{{_GQL_IGNORED}[A-Za-z_]"
    rf"|fragment\s+[A-Za-z_]\w*\s+on\s)", re.I)
#: Fields that name a GraphQL operation's type (``operation`` is the AST's).
_OP_TYPE_KEYS = frozenset({"operationType", "operationKind", "operation_type", "opType",
                           "operation"})
#: Fields that name a (persisted) operation; a name ending in Mutation or
#: Subscription is a write, whatever id it is sent with.
_OP_NAME_KEYS = frozenset({"operationName", "operation_name", "fb_api_req_friendly_name",
                           "opName"})
_WRITE_NAME = re.compile(r"(?:mutation|subscription)$", re.I)

#: Credential-shaped values, named. Each is a well-known token format; a match
#: anywhere inside a decoded leaf refuses the request.
_TOKEN_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("a JSON Web Token", re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # a scheme word followed by a 16+ character run holding a digit: "basic
    # electroencephalography" typed into a search is no credential
    ("a bearer/basic credential", re.compile(
        r"(?i)\b(?:bearer|basic)\s+(?=[A-Za-z._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}")),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("a Stripe secret key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Google OAuth token", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def credential_token(text: str) -> str:
    """The name of the credential format ``text`` carries, or ``""``."""
    if not isinstance(text, str) or len(text) < 16:
        return ""
    for name, rx in _TOKEN_SHAPES:
        if rx.search(text):
            return name
    return ""


def is_graphql_document(text: str, key: str = "") -> bool:
    if not isinstance(text, str):
        return False
    if key == "query" and "{" in text:
        return True
    return bool(_GQL_DOC.match(text))


def graphql_refusal(text: str, key: str = "") -> str:
    """Why a decoded string may not be sent in a read, or ``""``.

    Scoped to GraphQL: a string that IS a GraphQL document (an operation, a
    selection or a fragment at its start, or text with ``{`` under a key named
    ``query``) is held to the rung's own rule (``readonly_post_shape``): any
    ``mutation`` or ``subscription`` in it refuses. An operation-type field
    (``operationType`` and kin) saying mutation/subscription refuses. Any other
    string, a search for "gene mutation (BRCA1)" included, is a value.
    """
    if not isinstance(text, str):
        return ""
    if key in _OP_TYPE_KEYS and text.strip().lower() in ("mutation", "subscription"):
        return f"the operation type field {key!r} says {text.strip().lower()}"
    if key in _OP_NAME_KEYS and _WRITE_NAME.search(text.strip()):
        return f"the operation {text.strip()!r} is named as a mutation or a subscription"
    if is_graphql_document(text, key):
        low = text.lower()
        if "mutation" in low or "subscription" in low:
            return "a GraphQL document mentions a mutation or a subscription"
    return ""


def deep_refusal(url: str, body: Optional[str] = None,
                 headers: Optional[Mapping[str, Any]] = None,
                 max_layers: int = MAX_SLOT_LAYERS,
                 exempt: Optional[Mapping[str, Any]] = None) -> str:
    """Why this request may not be compiled or sent, judged over every decoded
    layer, or ``""``.

    The read-only POST rung's rules, applied at every layer: a credential-ish
    key (``api_step.signed_param_reason``) anywhere, a credential-shaped value
    anywhere (``headers`` values included), a GraphQL mutation/subscription in
    any decoded string. A request the walk could not see to the bottom (layers
    deeper than ``max_layers``, a size cap) is refused for that reason alone.

    ``exempt`` (``{key: values}``, keys compared in lower case): a value the
    site's page stated (``api_step.RequestTemplate.page_exemption``). A key in
    it passes the credential-name rule when every value under it is one of its
    values, and those exact values pass the credential-shape rule. Every other
    check is unchanged.
    """
    from anyuse.execution.api_step import signed_param_reason

    allowed = {str(k).lower(): {str(v) for v in vals} for k, vals in (exempt or {}).items()}
    walk = walk_request(url, body, max_layers)
    if walk.truncated:
        return f"the request's decoded layers cannot be fully inspected ({walk.truncated})"
    if walk.deeper:
        return (f"the value at {format_at(walk.deeper[0])} decodes deeper than "
                f"{max_layers} layers and cannot be inspected")

    def page_leaf(lf: Leaf) -> bool:
        return str(lf.key).lower() in allowed and str(lf.value) in allowed[str(lf.key).lower()]

    for at, key in walk.keys:
        low = str(key).lower()
        if low in allowed:
            under = [lf for lf in walk.leaves if str(lf.key).lower() == low]
            if under and all(page_leaf(lf) for lf in under):
                continue
        why = signed_param_reason(key)
        if why:
            where = format_at(at) if at else "the top layer"
            return f"{why} (decoded layer {where})"
    for lf in walk.leaves:
        if not isinstance(lf.value, str):
            continue
        token = "" if page_leaf(lf) else credential_token(lf.value)
        if token:
            return f"{format_at(lf.at)} carries {token} — a credential is never replayed"
        why = graphql_refusal(lf.value, lf.key)
        if why:
            return f"{why} ({format_at(lf.at)}) — only a pure query may be replayed"
    for name, value in (headers or {}).items():
        if str(value) in allowed.get(str(name).lower(), ()):
            continue
        token = credential_token(str(value))
        if token:
            return f"request header {name!r} carries {token}"
    return ""


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    from anyuse.execution.api_compile import normalise
    return normalise(text)


def _looks_url(text: str) -> bool:
    """A URL leaf: it starts with a scheme or a "/". A "?" in prose is not one."""
    return text.startswith("/") or bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text))


#: Pairs with no whitespace: ``k=v&k2=v2`` (a pair may lack ``=`` after the first).
_PAIRS_STRICT = re.compile(r"^[^\s&=?#]+=[^\s&]*(?:&[^\s&=]+(?:=[^\s&]*)?)*$")


def literal_state(text: str, start: int, end: int, graphql: bool) -> str:
    """Where ``text[start:end]`` sits: ``"in"`` a ``"..."`` string literal,
    ``"out"`` of every literal, or ``"unsure"``.

    GraphQL-aware when ``graphql``: a ``#`` comment runs to the end of the line
    and its quotes count for nothing; ``\"\"\"block strings\"\"\"`` have
    their own escaping, so a hit inside one, or in a comment, is ``"unsure"``.
    A hit that straddles a literal's edge, or a literal left open at the end,
    is ``"unsure"`` too: the caller then makes no slot.
    """
    state = "out"                 # out | str | block | comment
    i, n = 0, len(text)
    hit_state = None
    while i < n:
        if i == start:
            hit_state = state
        if start < i < end and state != hit_state:
            return "unsure"
        c = text[i]
        if state == "out":
            if graphql and text.startswith('"""', i):
                state, i = "block", i + 3
                continue
            if c == '"':
                state = "str"
            elif graphql and c == "#":
                state = "comment"
        elif state == "str":
            if c == "\\":
                if i + 1 == start:
                    return "unsure"           # the hit begins on an escaped character
                i += 2
                continue
            if c == '"':
                state = "out"
            elif c == "\n":
                return "unsure"           # an unterminated literal
        elif state == "block":
            if text.startswith('"""', i) and not text.startswith('\\"""', i - 1):
                state, i = "out", i + 3
                continue
        elif state == "comment":
            if c == "\n":
                state = "out"
        i += 1
    if hit_state is None:
        hit_state = state
    if state in ("str", "block"):
        return "unsure"                   # a literal left open
    if hit_state == "str":
        return "in"
    if hit_state == "out":
        return "out"
    return "unsure"


def _occurrences(text: str, needle: str) -> List[int]:
    out, i = [], text.find(needle)
    while i >= 0:
        out.append(i)
        i = text.find(needle, i + len(needle))
    return out


def _standalone(text: str, start: int, end: int, needle: str) -> bool:
    if needle[:1].isalnum() and start > 0 and text[start - 1].isalnum():
        return False
    if needle[-1:].isalnum() and end < len(text) and text[end].isalnum():
        return False
    return True


def _url_or_form_shaped(text: str) -> bool:
    return _looks_url(text) or bool(_PAIRS_STRICT.match(text))


def _substring_slot(lf: Leaf, value: str, name: str) -> Optional[Slot]:
    text = lf.value
    forms = [(value, "")]
    quoted = urllib.parse.quote(value, safe="")
    if quoted != value:
        forms.append((quoted, "url"))
    plussed = urllib.parse.quote_plus(value, safe="")
    if plussed not in (value, quoted):
        forms.append((plussed, "form"))
    gql = is_graphql_document(text, lf.key)
    for needle, escape in forms:
        hits = [i for i in _occurrences(text, needle)
                if _standalone(text, i, i + len(needle), needle)]
        if not hits:
            continue
        if escape == "":
            states = {literal_state(text, i, i + len(needle), gql) for i in hits}
            if states == {"in"}:
                escape = "json"
            elif states != {"out"}:
                return None                 # mixed or unsure: no escape is known to fit
            elif gql:
                return None                 # raw text into GraphQL source: refused
            elif _url_or_form_shaped(text):
                # never raw into URL/form syntax: percent-encoded where that
                # writes the recorded value back unchanged, else raw but
                # refusing every syntax character
                escape = "url" if quoted == value else "text"
        if escape == "json" and json.dumps(value, ensure_ascii=False)[1:-1] != value:
            continue                        # the recording did not escape it as JSON
        parts, prev = [], 0
        for i in hits:
            parts.append(_template_text(text[prev:i]))
            parts.append("{%s}" % name)
            prev = i + len(needle)
        parts.append(_template_text(text[prev:]))
        return Slot(param=name, at=lf.at, template="".join(parts), escape=escape)
    return None


def discover_slots(url: str, body: Optional[str], value: str, name: str,
                   max_layers: int = MAX_SLOT_LAYERS) -> Tuple[List[Slot], str]:
    """Slots for every leaf of the request that carries ``value``.

    A leaf equal to the value (exactly, else under ``api_compile.normalise``)
    is a whole slot, and a JSON number leaf spelled like the value is a number
    slot. Only when no leaf is the value does a leaf that CONTAINS it become a
    templated slot: the value must stand alone (no letter or digit touching
    it), it may be found raw, percent-encoded (``escape="url"``) or
    form-encoded (``"form"``); found inside a ``"..."`` literal it is
    JSON-escaped (``"json"``); found raw in a GraphQL document outside a string
    literal it is refused. Leaves that decoded into a deeper layer are not
    matched themselves; the deeper layer is.
    """
    from anyuse.execution.api_compile import match_length
    value = str(value or "")
    if match_length(value.strip()) < MIN_VALUE_CHARS:
        return [], f"example value {value!r} is shorter than {MIN_VALUE_CHARS} characters"
    walk = walk_request(url, body, max_layers)
    if walk.truncated:
        return [], f"the request cannot be searched ({walk.truncated})"
    leaves = [lf for lf in walk.leaves if not lf.decoded and lf.bindable]
    exact = [lf for lf in leaves if isinstance(lf.value, str) and lf.value == value]
    if not exact:
        want = _norm(value)
        exact = [lf for lf in leaves if isinstance(lf.value, str) and want
                 and _norm(lf.value) == want]
    slots = [Slot(param=name, at=lf.at) for lf in exact]
    slots += [Slot(param=name, at=lf.at, leaf="number") for lf in leaves
              if isinstance(lf.value, (int, float)) and not isinstance(lf.value, bool)
              and _JSON_NUMBER.fullmatch(value) and json.loads(value) == lf.value
              and json.dumps(lf.value) == value]
    if not slots:
        for lf in leaves:
            if isinstance(lf.value, str) and len(lf.value) > len(value):
                slot = _substring_slot(lf, value, name)
                if slot is not None:
                    slots.append(slot)
    seen: Set[Tuple] = set()
    unique = []
    for s in slots:
        if s.signature() not in seen:
            seen.add(s.signature())
            unique.append(s)
    if not unique:
        why = f"the typed value {value!r} is in no leaf of the request's decoded layers"
        if walk.deeper:
            why += (f" (a string at {format_at(walk.deeper[0])} decodes deeper than "
                    f"the {max_layers}-layer cap and was not searched)")
        return [], why
    return unique, ""


def slot_problems(url: str, body: Optional[str], slots: Sequence[Mapping[str, Any]],
                  declared: Iterable[str]) -> List[str]:
    """Everything wrong with a template's slots, against its raw request."""
    out: List[str] = []
    declared = set(declared)
    bound: Set[str] = set()
    for raw in slots:
        try:
            slot = Slot.from_dict(raw)
            parse_at(slot.at)
            if slot.escape not in ESCAPES:
                out.append(f"slot {format_at(slot.at)} has an unknown escape {slot.escape!r}")
            if slot.leaf not in LEAF_TYPES:
                out.append(f"slot {format_at(slot.at)} has an unknown leaf type {slot.leaf!r}")
            names = slot.params()
        except SlotError as exc:
            out.append(f"slot {raw!r}: {exc}")
            continue
        for name in names:
            if name not in declared:
                out.append(f"slot {format_at(slot.at)} fills {{{name}}}, which is not "
                           f"a declared parameter")
        bound.update(names)
        try:
            leaf = read_slot(url, body, slot.at)
        except SlotError as exc:
            out.append(f"slot {format_at(slot.at)} does not describe the request: {exc}")
            continue
        kind = ("boolean" if isinstance(leaf, bool) else
                "number" if isinstance(leaf, (int, float)) else
                "string" if isinstance(leaf, str) else type(leaf).__name__)
        want = "string" if slot.template is not None else slot.leaf
        if kind != want:
            out.append(f"slot {format_at(slot.at)} reaches a {kind} leaf, recorded as {want}")
        elif slot.template is not None and slot.escape == "" and _url_or_form_shaped(leaf):
            out.append(f"slot {format_at(slot.at)} writes a value raw into a URL/form-shaped "
                       f"leaf; its escape must be url, form or text")
    for name in sorted(declared - bound):
        out.append(f"input {name!r} has no slot: it would change nothing on the wire")
    return out


# ---------------------------------------------------------------------------
# agreement across recordings
# ---------------------------------------------------------------------------

#: Recordings that must reach an endpoint before a slot there counts as
#: agreed: the compiled one and at least one other. A slot inside a path
#: segment needs the path binding's three.
MIN_VOTERS = 2
MIN_PATH_VOTERS = 3


def is_path_slot(slot: Slot) -> bool:
    return slot.at[0].startswith("path:")


def endpoint_of(method: str, url: str, mask: Optional[int] = None) -> Tuple[str, str, str]:
    """``(method, host, path)``; ``mask`` replaces path segment ``mask`` by
    ``*`` (the segment a path-borne slot lives in differs per recording)."""
    sp = urllib.parse.urlsplit(url or "")
    path = sp.path
    if mask is not None:
        segs = path.split("/")
        if 0 <= mask + 1 < len(segs):
            segs[mask + 1] = "*"
        path = "/".join(segs)
    return ((method or "GET").upper(), sp.netloc, path)


def slot_endpoint(method: str, url: str, slot: Slot) -> Tuple[str, str, str]:
    first = parse_step(slot.at[0])
    return endpoint_of(method, url, int(first.key) if first.kind == "path" else None)


def _endpoints(method: str, url: str) -> List[Tuple[str, str, str]]:
    path = urllib.parse.urlsplit(url or "").path
    count = len(path.split("/")) - 1 if path.startswith("/") else 0
    return [endpoint_of(method, url)] + [endpoint_of(method, url, i) for i in range(count)]


@dataclass
class SlotEvidence:
    """Which slot each recording of a DIFFERENT value supports, per endpoint.

    The rule is the path binding's (``param_binding.BindingEvidence``): a slot
    is only used when every recording that reached the same endpoint carries
    its own value in the same place, with the same template remainder, and
    when enough recordings reached it (:data:`MIN_VOTERS`, or
    :data:`MIN_PATH_VOTERS` for a slot inside a path segment, whose segment is
    masked in the endpoint). A recording that never reached the endpoint has
    no vote; no votes is no agreement. ``b64`` keeps every recorded base64
    string per (endpoint, step), so the encoding style comes from all of them.
    """
    per_recording: List[Dict[Tuple[str, str, str], Set[Tuple]]] = field(default_factory=list)
    placeholder: str = "q"
    b64: Dict[Tuple[Tuple[str, str, str], Tuple[str, ...]], List[str]] = field(default_factory=dict)

    @property
    def n_recordings(self) -> int:
        return len(self.per_recording)

    def corroborates(self, method: str, url: str, slot: Slot) -> Tuple[bool, str]:
        ep = slot_endpoint(method, url, slot)
        voters = [rec for rec in self.per_recording if ep in rec]
        sig = Slot(param=self.placeholder, at=slot.at, template=slot.template and
                   slot.template.replace("{%s}" % slot.param, "{%s}" % self.placeholder),
                   escape=slot.escape, leaf=slot.leaf).signature()
        against = sum(1 for rec in voters if sig not in rec[ep])
        where = f"{ep[1]}{ep[2]}"
        if against:
            return False, (f"{against} of {len(voters)} recording(s) of {where} carry "
                           f"their value elsewhere than {format_at(slot.at)}")
        need = MIN_PATH_VOTERS if is_path_slot(slot) else MIN_VOTERS
        if len(voters) < need:
            return False, (f"only {len(voters)} recording(s) of different values reached "
                           f"{where}; {format_at(slot.at)} needs {need} that agree")
        return True, f"{len(voters)} recording(s) agree"


def slot_evidence(recordings: Sequence[dict], placeholder: str = "q") -> SlotEvidence:
    ev = SlotEvidence(placeholder=placeholder)
    seen_values: Set[str] = set()
    for rec in recordings:
        value = str(((rec.get("param") or {}).get("value")) or "")
        key = " ".join(value.split()).casefold()
        if not value.strip() or key in seen_values:
            continue
        seen_values.add(key)
        here: Dict[Tuple[str, str, str], Set[Tuple]] = {}
        for req in rec.get("network") or []:
            url = str(req.get("url") or "")
            if not url.startswith("http"):
                continue
            method = str(req.get("method") or "GET")
            body = req.get("post_data") if isinstance(req.get("post_data"), str) else None
            for ep in _endpoints(method, url):
                here.setdefault(ep, set())
            slots, _why = discover_slots(url, body, value, placeholder)
            for slot in slots:
                ep = slot_endpoint(method, url, slot)
                here.setdefault(ep, set()).add(slot.signature())
                for k, step in enumerate(slot.at):
                    if step == "b64":
                        try:
                            raw = read_slot(url, body, slot.at[:k])
                        except SlotError:
                            continue
                        if isinstance(raw, str):
                            ev.b64.setdefault((ep, tuple(slot.at[:k + 1])), []).append(raw)
        ev.per_recording.append(here)
    return ev


def corroborated(method: str, url: str, slots: Sequence[Slot],
                 evidence: Optional[SlotEvidence]) -> Tuple[List[Slot], List[str]]:
    """The slots the recordings agree on, and why the others were dropped.

    Without evidence (one recording) a slot outside the path stands alone, as
    a plain query binding does; a slot inside a path segment never does.
    """
    kept, why = [], []
    for slot in slots:
        if evidence is None:
            if is_path_slot(slot):
                why.append(f"{format_at(slot.at)} lies inside a path segment, which needs "
                           f"{MIN_PATH_VOTERS} corroborating recordings")
            else:
                kept.append(slot)
            continue
        ok, reason = evidence.corroborates(method, url, slot)
        (kept.append(slot) if ok else why.append(reason))
    return kept, why


def annotate_b64(method: str, url: str, body: Optional[str], slots: Sequence[Slot],
                 evidence: Optional[SlotEvidence]) -> Tuple[List[Slot], List[str]]:
    """Write every recording's evidence about each base64 layer into its step
    (``b64:url,nopad``), so a fill knows the alphabet and padding the site
    uses; recordings that contradict each other drop the slot."""
    kept, refused = [], []
    for slot in slots:
        at = list(slot.at)
        ok = True
        for k, step in enumerate(slot.at):
            if parse_step(step).kind != "b64":
                continue
            raws = [read_slot(url, body, slot.at[:k])]
            if evidence is not None:
                raws += evidence.b64.get((slot_endpoint(method, url, slot),
                                          tuple(slot.at[:k + 1])), [])
            try:
                flags = b64_flags(b64_merge(b64_style(r) for r in raws if isinstance(r, str)))
            except SlotError as exc:
                refused.append(f"{format_at(slot.at)}: {exc}")
                ok = False
                break
            at[k] = f"b64:{flags}" if flags else "b64"
        if ok:
            kept.append(Slot(slot.param, tuple(at), slot.template, slot.escape, slot.leaf))
    return kept, refused


def describe(slots: Sequence[Slot]) -> str:
    bits = []
    for s in slots:
        bit = format_at(s.at)
        if s.template is not None:
            bit += f" (part of the leaf{', ' + s.escape + '-escaped' if s.escape else ''})"
        bits.append(bit)
    return "; ".join(bits)
