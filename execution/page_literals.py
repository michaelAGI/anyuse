"""Values the site's own page states by name, and where it states them.

A search provider's public key, its app id and its index sit in the site's own
page (``window.cfg = {"searchKey": "..."}``, ``data-app-id="..."``,
``?apiKey=...&``). A compiled HTTP tool may read such a value from that page at
run time and send it the way the page did. What is kept is a
:class:`Locator`: the name and its punctuation, never the value. At run time
the page is fetched again and the locator reads the value it states now, so a
rotated key needs no recompile.

The rule (:func:`statements`), deterministic and independent of the page's
language:

* a NAME is ``[A-Za-z_$][A-Za-z0-9_$-]{1,63}`` with at least two letters, not
  preceded by a name character;
* the separator holds ``:`` or ``=``, with at most a quote (escaped or not) on
  each side and a little space;
* the VALUE is ``[A-Za-z0-9_.~-]``, followed by a quote, ``&``, ``<``,
  whitespace, ``,;})]``, a backslash or the end of the text.

A value passed as a bare function argument has no name, so it gives no
locator. :func:`locate` keeps a name only when the page states exactly that
value under it and no other.

The recorders keep page keys without keeping the key:

* :func:`mark_network` (a learn recording, a HAR, replay traffic): a request
  field with a credential-like name (``api_step.signed_param_reason``,
  ``api_step.CREDENTIAL_HEADERS``) whose exact value an earlier body of the
  site's own registrable domain stated by name is kept with ``page_keys`` (the
  field and its locator); its value is replaced by ``<redacted>`` in the
  request and by :data:`SHOWN` in every body. Any other credential request
  stays ``credential_bound`` with its bodies dropped.
* :class:`SourceIndex` holds keyed fingerprints of the stated values only, for
  a recorder that must not hold page text (attach).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

#: what a page value reads as in every result, trace and record
SHOWN = "<page value>"
#: how many site bodies a recorder indexes
MAX_SOURCES = 24
#: a body is scanned only up to this many characters
MAX_SCAN_CHARS = 3_000_000
#: names one locator keeps
MAX_NAMES = 3
#: statements one source contributes to an index
MAX_STATEMENTS = 20_000
#: a secret shorter than this is never located (a short value is stated by chance)
MIN_SECRET_LEN = 6
#: a non-secret literal shorter than this is never located
MIN_LITERAL_LEN = 4
REDACTED = "<redacted>"
KIND = "page_literal"

_STMT = re.compile(
    r"(?<![A-Za-z0-9_$-])([A-Za-z_$][A-Za-z0-9_$-]{1,63})"
    r"((?:\\?[\"'])?[ \t]{0,3}[:=][ \t]{0,3}(?:\\?[\"'])?)"
    r"([A-Za-z0-9_.~-]{1,512})"
    r"(?=[\"'&<\s,;})\]\\]|$)")
_LETTER = re.compile(r"[A-Za-z]")
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$-]{1,63}")
_SEP = re.compile(r"(?:\\?[\"'])?[ \t]{0,3}[:=][ \t]{0,3}(?:\\?[\"'])?")
#: the headers a page key is never read into: a request that sent one never carries a page key
NEVER_HEADERS = frozenset({"cookie", "set-cookie", "authorization", "proxy-authorization"})


# ---------------------------------------------------------------------------
# the scanner and the locator
# ---------------------------------------------------------------------------

def statements(text: str, min_len: int = 1, max_len: int = 512
               ) -> Iterator[Tuple[str, str, str, str, int]]:
    """``(name, sep, value, right, offset)`` for every name/value statement of ``text``."""
    raw = str(text or "")[:MAX_SCAN_CHARS]
    for m in _STMT.finditer(raw):
        name, sep, value = m.group(1), m.group(2), m.group(3)
        if len(_LETTER.findall(name)) < 2 or not (min_len <= len(value) <= max_len):
            continue
        end = m.end(3)
        right = raw[end] if end < len(raw) else ""
        yield name, sep, value, right, m.start(1)


@dataclass(frozen=True)
class Locator:
    """Where a page states one value: up to three ``(name, sep)`` pairs, the
    character after the value, and the length bounds a read accepts. A secret
    locator never falls back to ``else_value``."""
    names: Tuple[Tuple[str, str], ...]
    right: str = '"'
    min_len: int = 1
    max_len: int = 512
    secret: bool = False
    else_value: Optional[str] = None

    def to_dict(self) -> dict:
        return {"kind": KIND, "names": [[n, s] for n, s in self.names], "right": self.right,
                "min": int(self.min_len), "max": int(self.max_len), "secret": bool(self.secret),
                "else": None if self.secret else self.else_value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Locator":
        data = data or {}
        names = tuple((str(p[0]), str(p[1])) for p in (data.get("names") or [])
                      if isinstance(p, (list, tuple)) and len(p) == 2)
        secret = bool(data.get("secret"))
        other = data.get("else")
        return cls(names=names[:MAX_NAMES], right=str(data.get("right") or ""),
                   min_len=int(data.get("min") or 1), max_len=int(data.get("max") or 512),
                   secret=secret, else_value=None if secret or other is None else str(other))

    def ident(self) -> str:
        """The first 12 hex characters of sha256 over the locator's sorted JSON."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def label(self) -> str:
        return self.names[0][0] if self.names else "?"


def locator_problems(data: Any) -> List[str]:
    """What is wrong with a stored locator, or ``[]``."""
    if not isinstance(data, Mapping) or data.get("kind") != KIND:
        return ["a page binding must be a page_literal locator"]
    loc = Locator.from_dict(data)
    out: List[str] = []
    if not loc.names:
        out.append("a page_literal locator names no field")
    for name, sep in loc.names:
        if not _NAME.fullmatch(name):
            out.append(f"page_literal name {name[:40]!r} is not a name")
        if not _SEP.fullmatch(sep):
            out.append(f"page_literal separator {sep[:12]!r} is not a ':' or '=' separator")
    if not (1 <= loc.min_len <= loc.max_len <= 512):
        out.append("page_literal length bounds are invalid")
    return out


def _bounds(value: str) -> Tuple[int, int]:
    n = len(value)
    return max(1, n - n // 2), min(512, max(n, n * 2))


def locate(text: str, value: str, *, secret: bool = False) -> Optional[Locator]:
    """A locator for ``value`` in ``text``: the ``(name, sep)`` pairs under which
    ``text`` states exactly ``value`` and no other value, sharing the first
    occurrence's right-hand character; ``None`` when there is none."""
    value = str(value or "")
    if len(value) < (MIN_SECRET_LEN if secret else MIN_LITERAL_LEN) or not _STMT.fullmatch(
            f"ab={value}"):
        return None
    seen: Dict[Tuple[str, str], Set[str]] = {}
    firsts: List[Tuple[int, str, str, str]] = []
    for name, sep, val, right, at in statements(text):
        seen.setdefault((name, sep), set()).add(val)
        if val == value:
            firsts.append((at, name, sep, right))
    if not firsts:
        return None
    right = firsts[0][3]
    pairs: List[Tuple[str, str]] = []
    for _at, name, sep, r in sorted(firsts):
        if r != right or (name, sep) in pairs or seen.get((name, sep)) != {value}:
            continue
        pairs.append((name, sep))
        if len(pairs) >= MAX_NAMES:
            break
    if not pairs:
        return None
    lo, hi = _bounds(value)
    return Locator(names=tuple(pairs), right=right, min_len=lo, max_len=hi, secret=secret,
                   else_value=None if secret else value)


def read(text: str, loc: Locator) -> Tuple[Optional[str], str]:
    """The one value ``text`` states under ``loc`` now, or ``(None, why)``.

    Every name of the locator must agree. A non-secret locator falls back to its
    recorded value when the page no longer states it; a secret one never does,
    and never reads a value shorter than :data:`MIN_SECRET_LEN` (one that short
    is stated by chance, and :func:`scrub` would not hide it)."""
    want = set(loc.names)
    values: List[str] = []
    low = max(loc.min_len, MIN_SECRET_LEN) if loc.secret else loc.min_len
    for name, sep, val, right, _at in statements(text, low, loc.max_len):
        if (name, sep) in want and right == loc.right and val not in values:
            values.append(val)
    if len(values) == 1:
        return values[0], ""
    if len(values) > 1:
        return None, f"the page states {len(values)} different values for {loc.label()}"
    if not loc.secret and loc.else_value is not None:
        return loc.else_value, ""
    return None, f"the page no longer states {loc.label()}"


def scrub(obj: Any, values: Iterable[str]) -> Any:
    """``obj`` with every occurrence of ``values`` (plain, percent- and
    form-encoded) replaced by :data:`SHOWN`, in every string and key."""
    forms: List[str] = []
    for v in values or ():
        v = str(v or "")
        if len(v) < 4:
            continue
        for f in (v, urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v)):
            if f not in forms:
                forms.append(f)
    if not forms:
        return obj
    forms.sort(key=len, reverse=True)

    def one(x: Any) -> Any:
        if isinstance(x, str):
            for f in forms:
                if f in x:
                    x = x.replace(f, SHOWN)
            return x
        if isinstance(x, dict):
            return {one(k) if isinstance(k, str) else k: one(v) for k, v in x.items()}
        if isinstance(x, list):
            return [one(v) for v in x]
        if isinstance(x, tuple):
            return tuple(one(v) for v in x)
        return x
    return one(obj)


# ---------------------------------------------------------------------------
# the request fields that carry a credential-like name
# ---------------------------------------------------------------------------

def _json_leaves(node: Any, prefix: str = "") -> Iterator[Tuple[str, str, Any]]:
    """``(path, key, value)`` of every scalar leaf of a decoded JSON body."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                yield from _json_leaves(v, p)
            else:
                yield p, str(k), v
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _json_leaves(v, f"{prefix}[{i}]")


def credential_fields(url: str, headers: Optional[Mapping[str, Any]] = None,
                      body: Optional[str] = None) -> Tuple[Dict[str, str], str]:
    """``({at: raw value}, never)``: the request's fields whose NAME is
    credential-like (``query:KEY``, ``header:name``, ``body:PATH``), and why the
    request can never carry a page key (a ``Cookie``, ``Authorization`` or
    ``Proxy-Authorization`` header) or ``""``."""
    from anyuse.execution.api_step import CREDENTIAL_HEADERS, signed_param_reason
    out: Dict[str, str] = {}
    try:
        query = urllib.parse.parse_qsl(urllib.parse.urlsplit(str(url or "")).query,
                                       keep_blank_values=True)
    except ValueError:
        query = []
    for k, v in query:
        if signed_param_reason(k):
            out[f"query:{k}"] = v
    for name, value in (headers or {}).items():
        low = str(name).lower()
        if low in NEVER_HEADERS:
            if low != "set-cookie":
                return {}, f"the request carried a {name!r} header"
            continue
        if low in CREDENTIAL_HEADERS or signed_param_reason(low):
            out[f"header:{low}"] = str(value or "")
    raw = (body or "").strip()
    if raw[:1] in "{[":
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        for path, key, value in _json_leaves(data):
            if signed_param_reason(key) and isinstance(value, str):
                out[f"body:{path}"] = value
    elif raw and "=" in raw:
        for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True):
            if signed_param_reason(k):
                out[f"body:{k}"] = v
    return out, ""


# ---------------------------------------------------------------------------
# the sources: bodies of the site's own pages
# ---------------------------------------------------------------------------

class SourceIndex:
    """Keyed fingerprints (HMAC-SHA256) of the values site bodies state by
    name, each with the first source that states it and its locator. The text
    of a source is dropped once it is indexed."""

    def __init__(self, key: Optional[bytes] = None):
        self._key = key if key is not None else os.urandom(16)
        self._found: Dict[str, dict] = {}
        self.sources = 0

    def fingerprint(self, value: str) -> str:
        return hmac.new(self._key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()

    def add(self, order: int, url: str, text: str) -> int:
        """Index the statements of one source; returns how many values were new."""
        if self.sources >= MAX_SOURCES or not text:
            return 0
        self.sources += 1
        groups: Dict[Tuple[str, str], Set[str]] = {}
        firsts: Dict[str, List[Tuple[int, str, str, str]]] = {}
        n = 0
        for name, sep, val, right, at in statements(text, MIN_SECRET_LEN):
            groups.setdefault((name, sep), set()).add(val)
            firsts.setdefault(val, []).append((at, name, sep, right))
            n += 1
            if n >= MAX_STATEMENTS:
                break
        new = 0
        for val, hits in firsts.items():
            fp = self.fingerprint(val)
            seen = self._found.get(fp)
            if seen is not None and seen["source_index"] <= int(order):
                # the earliest source is kept: a recorder may index bodies in the
                # order they arrive, not the order their requests were sent
                continue
            right = hits[0][3]
            pairs = []
            for _at, name, sep, r in hits:
                if r == right and (name, sep) not in pairs and groups.get((name, sep)) == {val}:
                    pairs.append((name, sep))
                if len(pairs) >= MAX_NAMES:
                    break
            if not pairs:
                continue
            lo, hi = _bounds(val)
            loc = Locator(names=tuple(pairs), right=right, min_len=lo, max_len=hi, secret=True)
            self._found[fp] = {"source_index": int(order), "source_url": str(url),
                               "locator": loc.to_dict()}
            new += seen is None
        return new

    def find(self, fp: str, before: int) -> Optional[dict]:
        """The source that stated the fingerprinted value, when it came before ``before``."""
        hit = self._found.get(fp)
        if hit is None or hit["source_index"] >= before:
            return None
        return dict(hit)


def provenance(fields: Mapping[str, str], index: SourceIndex, before: int,
               fingerprints: Optional[Mapping[str, str]] = None
               ) -> Tuple[Optional[List[dict]], str]:
    """``(page_keys, "")`` when an earlier source states every field's value by
    name, else ``(None, why)``. ``fingerprints`` (``{at: fp}``) replaces the raw
    values for a recorder that holds only fingerprints."""
    out: List[dict] = []
    for at in sorted(fields if fingerprints is None else fingerprints):
        fp = (fingerprints or {}).get(at) or index.fingerprint(fields[at])
        hit = index.find(fp, before)
        if hit is None:
            return None, f"the site's page did not state the value of {at.split(':', 1)[-1]!r}"
        out.append({"at": at, **hit})
    return out, ""


#: the resource types whose bodies are sources
SOURCE_TYPES = frozenset({"Document", "XHR", "Fetch", "Script", ""})


def _site_of(url: str) -> str:
    from anyuse.execution.web_scope import host_of, registrable
    return registrable(host_of(url))


def _textual(entry: Mapping[str, Any]) -> bool:
    body = entry.get("body")
    if not isinstance(body, str) or not body:
        return False
    mime = str(entry.get("mime") or "").lower()
    return not mime or any(h in mime for h in ("html", "json", "javascript", "text", "xml",
                                                 "ecmascript"))


def redact_fields(url: str, headers: Optional[Mapping[str, Any]], body: Optional[str],
                  ats: Iterable[str]) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """The request with the page-key fields' values replaced by ``<redacted>``
    (the headers removed: the template names them through ``page_keys``)."""
    ats = set(ats)
    sp = urllib.parse.urlsplit(str(url or ""))
    pairs = urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
    query = urllib.parse.urlencode([(k, REDACTED if f"query:{k}" in ats else v) for k, v in pairs])
    new_url = urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, query, sp.fragment))
    hdrs = {k: v for k, v in (headers or {}).items() if f"header:{str(k).lower()}" not in ats}
    raw = (body or "")
    if raw.strip()[:1] in "{[":
        try:
            data = json.loads(raw)

            def walk(node, prefix=""):
                if isinstance(node, dict):
                    return {k: (REDACTED if f"body:{prefix + '.' if prefix else ''}{k}" in ats
                                and not isinstance(v, (dict, list))
                                else walk(v, f"{prefix}.{k}" if prefix else str(k)))
                            for k, v in node.items()}
                if isinstance(node, list):
                    return [walk(v, f"{prefix}[{i}]") for i, v in enumerate(node)]
                return node
            body = json.dumps(walk(data), separators=(",", ":"), ensure_ascii=False)
        except ValueError:
            pass
    elif raw and "=" in raw:
        body = urllib.parse.urlencode([(k, REDACTED if f"body:{k}" in ats else v)
                                       for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True)])
    return new_url, hdrs, body


def _redacted_query(url: str) -> str:
    from anyuse.execution.api_step import signed_param_reason
    sp = urllib.parse.urlsplit(str(url or ""))
    pairs = urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
    if not any(signed_param_reason(k) for k, _v in pairs):
        return str(url or "")
    query = urllib.parse.urlencode([(k, REDACTED if signed_param_reason(k) else v) for k, v in pairs])
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, query, sp.fragment))


def mark_network(network: Sequence[Mapping[str, Any]], site_host: str,
                 secrets_out: Optional[Set[str]] = None,
                 refuse_unstated: bool = True) -> List[dict]:
    """Sanitised copies of raw traffic (a learn agent's, a HAR's, a replay's).

    A request with credential-named fields keeps ``page_keys`` when an earlier
    body of ``site_host``'s registrable domain states each field's exact value
    by name (:func:`provenance`); the values are replaced by ``<redacted>`` in
    the request, and by :data:`SHOWN` in every body of the traffic. Any other
    credential request becomes ``credential_bound`` with its bodies dropped.
    ``secrets_out`` receives the replaced values, so the caller can scrub the
    rest of its recording (the page text, the DOM). With ``refuse_unstated=False``
    (the learn agent's recording, which never held a refusal) any other credential
    request stays as it was recorded."""
    site = _site_of(f"https://{site_host or ''}/") if site_host else ""
    index = SourceIndex()
    sources_seen = 0
    out: List[dict] = []
    secrets: Set[str] = set()
    for order, raw in enumerate(network or ()):
        entry = dict(raw)
        url = str(entry.get("url") or "")
        if (sources_seen < MAX_SOURCES and str(entry.get("resource_type") or "") in SOURCE_TYPES
                and _textual(entry) and site and _site_of(url) == site):
            index.add(order, url, str(entry.get("body") or ""))
            sources_seen += 1
        fields, never = credential_fields(url, entry.get("headers") or {}, entry.get("post_data"))
        if never and not refuse_unstated:
            out.append(entry)
            continue
        if never:
            entry.update(credential_bound=never, body=None, post_data=None,
                         url=_redacted_query(url))
            out.append(entry)
            continue
        if not fields:
            out.append(entry)
            continue
        keys, why = provenance(fields, index, order)
        if keys is None and not refuse_unstated:
            out.append(entry)
            continue
        if keys is None:
            entry.update(credential_bound=why, body=None, post_data=None,
                         url=_redacted_query(url),
                         headers={k: v for k, v in (entry.get("headers") or {}).items()
                                  if f"header:{str(k).lower()}" not in fields})
            out.append(entry)
            continue
        secrets.update(v for v in fields.values() if v)
        entry["url"], entry["headers"], entry["post_data"] = redact_fields(
            url, entry.get("headers") or {}, entry.get("post_data"), fields)
        entry["page_keys"] = keys
        entry["credential_bound"] = ""
        out.append(entry)
    if secrets:
        out = [scrub(e, secrets) for e in out]
        if secrets_out is not None:
            secrets_out.update(secrets)
    return out


def keyed_provenance(bodies: Sequence[Tuple[str, Optional[str], str]],
                     fields: Mapping[int, Mapping[str, str]], site_host: str
                     ) -> Tuple[Dict[int, List[dict]], Set[str]]:
    """``({index: page_keys}, secrets)`` for a recorder that holds the raw traffic
    apart from its entries (a HAR): ``bodies`` is ``(url, body, mime)`` per entry in
    order, ``fields`` the credential-named fields of the entries that have them
    (:func:`credential_fields`). An entry gets page keys when an earlier text body
    of ``site_host``'s registrable domain states every field's value by name."""
    site = _site_of(f"https://{site_host}/") if site_host else ""
    index = SourceIndex()
    found: Dict[int, List[dict]] = {}
    secrets: Set[str] = set()
    for i, (url, body, mime) in enumerate(bodies):
        if i in fields and fields[i]:
            keys, _why = provenance(fields[i], index, i)
            if keys is not None:
                found[i] = keys
                secrets.update(v for v in fields[i].values() if v)
        if site and isinstance(body, str) and body and _site_of(url) == site \
                and _textual({"body": body, "mime": mime}):
            index.add(i, url, body)
    return found, secrets


def page_keys_of(request: Mapping[str, Any]) -> Dict[str, dict]:
    """``{at: page key}`` of a sanitised request."""
    out: Dict[str, dict] = {}
    for k in request.get("page_keys") or []:
        if isinstance(k, Mapping) and k.get("at") and isinstance(k.get("locator"), Mapping):
            out[str(k["at"])] = dict(k)
    return out
