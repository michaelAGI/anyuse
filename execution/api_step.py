"""The ``api`` rung of the implementation ladder — replay a recorded UI step as
a declarative HTTP request instead of driving the UI.

This is the executable half of "compile recorded workflows down to API calls".
``anyuse/execution/api_compile.py`` derives a :class:`RequestTemplate` from a
recording; this module *runs* one and answers the executor's usual per-modality
contract (``{"success": bool, "method": "api", ...}``), so the compiled rung
slots into ``StateGraphExecutor._execute_step``'s ``modality_map`` next to
``_execute_ax`` / ``_execute_coordinate`` and into ``deterministic._STABILITY``
ABOVE ``ax`` — a compiled transition is tried first, and ANY failure (network,
non-2xx, wrong shape, postcondition false) falls through to the recorded UI path
exactly like a failing ``ax`` implementation does.

Safety contract
---------------
* **Read-only methods by default.** ``GET`` and ``HEAD`` are the default wire
  methods. A structurally classified POST still needs the runtime opt-in and
  exact endpoint/operation approval. Any other method is REFUSED here with
  ``requires_approval=True`` — it never auto-runs, whatever the HIL mode is.
  :func:`api_hil_action` additionally projects the request into the shape
  ``hil_gate.classify_step`` consumes so the EXISTING gate classifies a write as
  destructive (an unrecognised verb fails closed) once a later version wires
  writes at all.
* **No secrets, ever.** Only :data:`SAFE_HEADERS` may appear in a template;
  ``Authorization`` / ``Cookie`` / ``X-Api-Key`` and friends are rejected by
  :meth:`RequestTemplate.problems`, not silently dropped, so a template that
  needs auth fails to compile rather than quietly losing its credential. The
  credential-vault hook is DESIGNED and NOT
  implemented.
* **No desktop actuation.** Nothing here imports pyautogui/Quartz/AX/osascript
  and nothing takes a screenshot. An api step needs no TCC permission and is not
  governed by ``ANYUSE_DRIVE_ENABLED`` (that gate is about moving the mouse and
  keyboard, which this never does). ``ANYUSE_API_MODALITY=off`` is the kill
  switch that disables the rung entirely.
* **Additive by construction.** An ``api`` implementation can only enter a state
  graph through an explicit ``anyuse workflow compile`` run, so wiring the
  dispatch entry changes the behaviour of an uncompiled workflow not at all.

The transport is injectable (``transport=``) so every unit test — including the
deliberate 4xx/5xx/shape-change failure-injection tests — runs offline.
"""
from __future__ import annotations

import json
import math
import http.cookiejar
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ResourceLimitError(ValueError):
    """A response or plan exceeded a hard local resource budget."""

#: The only methods this module puts on the wire without a human.
READ_METHODS = frozenset({"GET", "HEAD"})

#: ``RequestTemplate.to_dict()["schema_version"]``; its minor is ``schema_minor``.
TEMPLATE_SCHEMA_VERSION = 2
#: The minor of a template that reads a value from the site's page
#: (``extract_kind == "page_text"``, a ``from: page`` derived value, a provider
#: read): an older AnyUse refuses it instead of dropping its page bindings.
PAGE_SCHEMA_MINOR = 2

#: The only request headers a compiled template may carry. Deliberately tiny:
#: anything that can carry identity or a credential is absent.
SAFE_HEADERS = frozenset({"accept", "accept-language", "content-type", "user-agent"})

#: Header names that mark a request as credential-bearing (out of scope — a
#: template naming one of these is rejected, never stripped-and-run).
CREDENTIAL_HEADERS = frozenset({
    "authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
    "proxy-authorization", "x-csrf-token", "x-xsrf-token",
})
#: Headers no value read from the site's page is ever sent in (``page_literals.NEVER_HEADERS``).
NEVER_PAGE_HEADERS = frozenset({"cookie", "set-cookie", "authorization", "proxy-authorization"})

# Transport, parser and plan budgets. These are hard safety caps, not tuning
# targets, and apply even when an injected transport returns an in-memory body.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_ITEMS = 100_000
MAX_REQUEST_TIMEOUT_S = 30.0
MAX_CHAIN_REQUESTS = 8
MAX_CHAIN_BYTES = 16 * 1024 * 1024
MAX_CHAIN_SECONDS = 30.0

#: Query/body keys that mark a request as signed or session-bound. Compilation
#: refuses these: replaying a signed request is neither safe nor reproducible.
SIGNED_PARAM_KEYS = frozenset({
    "token", "access_token", "api_key", "apikey", "key", "signature", "sig",
    "hmac", "auth", "session", "sessionid", "sid", "csrf", "_csrf", "xsrf",
    "nonce", "jwt", "bearer",
})

#: Query keys that are cache-busters / telemetry — dropped from a template.
VOLATILE_PARAM_KEYS = frozenset({
    "_", "cb", "cachebust", "cache_bust", "nocache", "ts", "timestamp", "rnd",
    "random", "r", "v_", "_t", "__", "gclid", "fbclid", "msclkid", "ref_src",
})
VOLATILE_PARAM_PREFIXES = ("utm_", "ga_", "_ga", "mc_", "pk_", "oly_")

#: Substrings that make a parameter name credential-ish whatever it is called in
#: full. :data:`SIGNED_PARAM_KEYS` is an exact-name set, and it alone
#: waved through ``x-typesense-api-key=…`` on rfc-editor.org — a per-site search
#: key sitting in a query string, which the compiler would happily have baked
#: into a template. Kept deliberately narrow so ordinary words survive:
#: ``keywords``, ``keyword``, ``monkey`` and ``sorting`` must NOT match.
SIGNED_PARAM_SUBSTRINGS = (
    "api-key", "api_key", "apikey", "access-token", "access_token",
    "auth-token", "auth_token", "-token", "_token", "csrf", "xsrf",
    "sessionid", "session-id", "session_id", "signature", "jwt",
    "bearer", "secret", "password", "passwd",
)


def signed_param_reason(key: str) -> str:
    """Why this parameter name may never appear in a template, or ``""``."""
    low = (key or "").lower()
    if low in SIGNED_PARAM_KEYS:
        return f"parameter {key!r} looks signed/session-bound — not replayable"
    for needle in SIGNED_PARAM_SUBSTRINGS:
        if needle in low:
            return (f"parameter {key!r} contains {needle!r} — it carries a "
                    f"credential or a per-client key and is not replayable")
    return ""

def signed_in_template(template: Any) -> bool:
    """Whether a template (object or stored dict) replays a signed-in session
    (site-mode login=replay: it carries ``cookie_refs``). Its extracted items are
    the user's logged-in data and are never persisted (no drift anchor, no
    last-good answer); they live only in the run's return value."""
    if isinstance(template, dict):
        refs = template.get("cookie_refs")
        steps = template.get("steps")
    else:
        refs = getattr(template, "cookie_refs", None)
        steps = getattr(template, "steps", None)
    if refs:
        return True
    for step in steps or []:
        inner = step.get("template") if isinstance(step, dict) else getattr(step, "template", None)
        if inner is not None and signed_in_template(inner):
            return True
    return False


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TRUTHY_OFF = {"0", "off", "false", "no"}

#: A read-only POST body larger than this is refused. A search request is small;
#: a large body is a document being uploaded, which is not a read.
READONLY_POST_MAX_BODY = 8192

_GQL_OP = re.compile(r"\s*(query|mutation|subscription)\b", re.I)


def readonly_post_shape(body: Optional[str]) -> Tuple[str, str, str]:
    """Classify a POST body: ``(kind, operation, refusal_reason)``.

    ``kind`` is ``"graphql"``, ``"json"`` or ``"form"``. ``operation`` is the
    GraphQL operation type (``query`` / ``mutation`` / ``subscription`` /
    ``anonymous``) and empty for the other kinds. A non-empty refusal reason
    means the body may not be replayed at all.

    Deliberately syntactic and deliberately strict: a body this function cannot
    read is refused rather than guessed at, and a GraphQL document containing
    *any* ``mutation`` or ``subscription`` keyword is refused even when the
    declared operation is a query, because one request may carry several
    operations.
    """
    raw = (body or "").strip()
    if not raw:
        return "", "", "empty POST body"
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            return "", "", "POST body is neither valid JSON nor a form encoding"
        doc = _graphql_document(data)
        if doc is not None:
            low = doc.lower()
            if "mutation" in low or "subscription" in low:
                m = _GQL_OP.match(doc)
                return "graphql", (m.group(1).lower() if m else "anonymous"), (
                    "GraphQL document mentions a mutation or a subscription — "
                    "refused; only a pure query may be replayed")
            m = _GQL_OP.match(doc)
            if m:
                return "graphql", m.group(1).lower(), ""
            if doc.lstrip().startswith("{"):
                return "graphql", "anonymous", ""
            return "graphql", "", "GraphQL document has no recognisable operation"
        return "json", "", ""
    if "=" in raw and "\n" not in raw:
        return "form", "", ""
    return "", "", "POST body is neither valid JSON nor a form encoding"


def _graphql_document(data: Any) -> Optional[str]:
    """The GraphQL source inside a decoded POST body, if this is GraphQL."""
    if isinstance(data, dict) and isinstance(data.get("query"), str) \
            and ("{" in data["query"]):
        return data["query"]
    if isinstance(data, list):
        parts = [_graphql_document(d) for d in data]
        found = [p for p in parts if p]
        if found:
            return "\n".join(found)
    return None


def api_modality_enabled() -> bool:
    """Kill switch. ``ANYUSE_API_MODALITY=off|0|false|no`` disables the rung."""
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_API_MODALITY"))


def page_keys_enabled() -> bool:
    """``ANYUSE_PAGE_KEYS``: may a compiled HTTP tool read a provider key from the site's page?"""
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_PAGE_KEYS"))


def plan_reads_page_keys(plan: "RequestPlan") -> bool:
    """Does a step of ``plan`` read a secret value (a provider key) from the site's page?
    A landing page's id or a provider's app id is not secret and not gated."""
    return any(bool((loc or {}).get("secret"))
               for step in plan.steps for loc in step.page_bind.values())


def readonly_post_operation(body: Optional[str]) -> str:
    """Stable operation identity used by endpoint approvals."""
    kind, _operation, why = readonly_post_shape(body)
    if why:
        return ""
    raw = (body or "").strip()
    if kind == "graphql":
        try:
            document = _graphql_document(json.loads(raw)) or ""
        except ValueError:
            document = ""
        match = re.match(r"\s*query(?:\s+([A-Za-z_][A-Za-z0-9_]*))?", document, re.I)
        return "query:" + ((match.group(1) if match else "") or "anonymous")
    if kind == "json":
        try:
            data = json.loads(raw)
        except ValueError:
            return ""
        keys = sorted(str(key) for key in data) if isinstance(data, dict) else ["list"]
        return "json:" + ",".join(keys)
    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    return "form:" + ",".join(sorted({key for key, _value in pairs}))


def readonly_post_enabled(url: str = "", body: Optional[str] = None,
                          hosts_path: Optional[str] = None) -> bool:
    """True only with the opt-in env flag and an exact endpoint/operation grant."""
    from anyuse.settings import get as get_setting
    if not get_setting("ANYUSE_API_READONLY_POST"):
        return False
    if not url or body is None:
        return False
    from anyuse.execution.api_permission import readonly_post_approved
    return readonly_post_approved(url, readonly_post_operation(body), hosts_path)


# ---------------------------------------------------------------------------
# JSON path (a deliberately tiny, deterministic subset)
# ---------------------------------------------------------------------------

def json_path(obj: Any, path: str) -> Any:
    """Read ``path`` out of a decoded JSON document.

    Supported syntax — and nothing else, on purpose (a compiled extraction path
    must be reproducible and auditable, not a mini query language):

        ``a.b``        object key
        ``a[0].b``     list index
        ``a[*].b``     map over a list (returns a list)
        ``""``         the document itself

    Returns ``None`` for any path that does not resolve (never raises).
    """
    if not path:
        return obj
    return _walk(obj, _split_path(path))


def validate_json_limits(data: Any, max_depth: int = MAX_JSON_DEPTH,
                         max_items: int = MAX_JSON_ITEMS) -> Tuple[bool, str]:
    """Bound JSON nesting and aggregate container members."""
    stack = [(data, 1)]
    items = 0
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            return False, f"JSON nesting exceeds depth cap {max_depth}"
        if isinstance(node, dict):
            items += len(node)
            stack.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            items += len(node)
            stack.extend((value, depth + 1) for value in node)
        if items > max_items:
            return False, f"JSON item count exceeds cap {max_items}"
    return True, ""


def _walk(cur: Any, tokens: List[Any]) -> Any:
    for i, token in enumerate(tokens):
        if token == "*":
            # Map the REST of the path over the list. ``crates[*].id`` must
            # yield the ids, not the crate objects — getting this wrong silently
            # turns an ordered extraction into a bag of every nested value.
            if not isinstance(cur, list):
                return None
            rest = tokens[i + 1:]
            if not rest:
                return list(cur)
            out = [v for v in (_walk(item, rest) for item in cur) if v is not None]
            return out or None
        if isinstance(token, int):
            if not isinstance(cur, list) or not (-len(cur) <= token < len(cur)):
                return None
            cur = cur[token]
        elif isinstance(cur, list):
            # a bare key applied to a list maps over it (a common real shape)
            out = [item[token] for item in cur if isinstance(item, dict) and token in item]
            return out or None
        elif isinstance(cur, dict):
            if token not in cur:
                return None
            cur = cur[token]
        else:
            return None
    return cur


def _split_path(path: str) -> List[Any]:
    tokens: List[Any] = []
    for part in path.split("."):
        if not part:
            continue
        head, *brackets = re.split(r"\[|\]", part)
        if head:
            tokens.append(head)
        for b in brackets:
            if not b:
                continue
            tokens.append("*" if b == "*" else int(b))
    return tokens


def json_leaves(obj: Any, prefix: str = "", limit: int = 4000) -> List[Tuple[str, Any]]:
    """Flatten a JSON document into ``(path, scalar)`` pairs.

    The compiler's value-overlap scoring works on these leaves: a request whose
    response leaves account for what is on screen is the request the UI used.
    List indices are kept in the path so a leaf can be traced back exactly.
    """
    out: List[Tuple[str, Any]] = []

    def walk(node: Any, p: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{p}.{k}" if p else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{p}[{i}]")
        else:
            out.append((p, node))

    walk(obj, prefix)
    return out


# ---------------------------------------------------------------------------
# RequestTemplate
# ---------------------------------------------------------------------------

@dataclass
class RequestTemplate:
    """A declarative, parameterised HTTP request — the ``api`` rung's command.

    Stored verbatim as ``TransitionImpl.command`` (modality ``"api"``), so it
    round-trips through the existing state-graph persistence with no schema
    change: ``TransitionImpl.to_dict`` already emits ``command`` as an opaque
    dict.

    ``url`` and the values in ``query``/``body`` are templates over the
    workflow's parameters using ``{name}`` placeholders — the same slot names
    ``execution/params.py`` infers from a recording.
    """

    method: str = "GET"
    url: str = ""                                    # may contain {param} in the path
    query: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None                       # template string (not executed in M1)
    timeout_s: float = 10.0
    #: name -> json path, read out of the response and returned as ``extracted``
    extract: Dict[str, str] = field(default_factory=dict)
    #: ``"json"``          — ``extract``/``expect`` paths are JSON paths into the
    #:                       decoded body (the default), i.e. a JSON API.
    #: ``"embedded_json"``  — the response is HTML; ``embedded`` names a JSON
    #:                       payload inside it, and the paths address THAT. Data,
    #:                       not presentation, so it survives a redesign that a
    #:                       selector would not; see execution/embedded_json.py.
    #: ``"html"``           — the paths are `html_extract` selectors over the
    #:                       markup itself. Bound to presentation, so the most
    #:                       brittle of the three; see execution/html_extract.py.
    #: ``"detail_fields"``  — the paths are the bounded item-page fields used
    #:                       by the attach/discovery detail proof (title,
    #:                       headings, itemprop=name, og:title).
    #: Preference when more than one proves: json > embedded_json > html.
    extract_kind: str = "json"
    #: For ``extract_kind == "embedded_json"``: which payload inside the document
    #: to read, as an ``embedded_json`` locator
    #: (``{"kind": "script_id", "id": "__NEXT_DATA__"}`` and friends). Empty for
    #: every other kind.
    embedded: Dict[str, Any] = field(default_factory=dict)
    #: ``"list"``  — the screen showed an ordered result list, extracted by one
    #:              ``a[*].b`` path; order IS part of the equivalence check.
    #: ``"fields"`` — the screen showed one record's fields, extracted by several
    #:              scalar paths; only the set of values is compared, because
    #:              the order of unrelated fields carries no meaning.
    shape: str = "list"
    #: deterministic postcondition, see :func:`check_expect`
    expect: Dict[str, Any] = field(default_factory=dict)
    #: parameter names this template consumes
    params: List[str] = field(default_factory=list)
    #: ``name -> how many whole path segments this parameter
    #: occupies``. Present only for a parameter the recordings proved spans more
    #: than one segment (``guzzlehttp/guzzle``). At render time the value must
    #: split into exactly that many non-empty segments or the request is refused
    #: (:func:`param_binding.bind_path_value`). A parameter absent from this map
    #: is encoded with no safe characters, exactly as before, so it can never
    #: add a segment, a query or a fragment.
    path_params: Dict[str, int] = field(default_factory=dict)
    #: ``name -> transform`` applied to the typed value before it
    #: goes into the URL (``lower``, ``hyphen``, ``slug``). Set only when EVERY
    #: recording agreed on the same one.
    param_transforms: Dict[str, str] = field(default_factory=dict)
    #: "deterministic" | "generator" — provenance, for the report and for audit
    origin: str = "deterministic"
    notes: str = ""
    #: Lever (c): this POST was PROVED to be a read — a GraphQL
    #: ``query`` operation or a search POST the page itself issued once per
    #: query, carrying no credential and no session-bound token. Only
    #: :func:`api_compile.compile_readonly_post` may set it, only after those
    #: checks, and the fresh-input proof still decides whether it is persisted.
    #: Everything else about a POST is refused exactly as before.
    readonly_post: bool = False
    #: Redirects or related API origins explicitly approved for this request.
    #: The request's own exact origin is implicit; no registrable-domain
    #: broadening is performed.
    approved_origins: List[str] = field(default_factory=list)
    #: Version-2 tagged dataflow. ``params`` remains a compatibility view for
    #: old state-graph commands; authorization and validation use these specs.
    parameter_schema: List[Dict[str, Any]] = field(default_factory=list)
    #: Nested-layer slots (execution/api_slots.py), schema 2.1. When present,
    #: ``url`` (query string included) and ``body`` are the captured request
    #: VERBATIM, ``query`` is empty, and every parameter is filled through a
    #: slot: a path of decode steps to the leaf it replaces. No ``{name}``
    #: substitution runs over such a template's raw text. Absent (the default)
    #: the template is the schema-2 template it always was, serialized
    #: identically.
    slots: List[Dict[str, Any]] = field(default_factory=list)
    #: The format this template was written in. ``schema_minor`` is 1 on a
    #: slotted template (2.1) and 0 otherwise; a template written by a NEWER
    #: AnyUse (a higher version or minor) is refused by :meth:`problems`, since
    #: its fields may mean something this runner would silently drop.
    schema_version: int = TEMPLATE_SCHEMA_VERSION
    schema_minor: int = 0
    #: Site-mode ``login = replay`` (execution/site_modes.py): the NAMES of the
    #: cookies this request needs, kept where the values were. NEVER a value —
    #: values are imported into memory at send time (execution/session_import.py)
    #: only for a login=replay + route=replay site. An empty list is an ordinary
    #: credential-free template, which is every template compiled today.
    cookie_refs: List[str] = field(default_factory=list)
    #: Site-mode ``identity = browser``: the User-Agent CAPTURED from the
    #: recorded browser request. identity=browser replays THIS string and never
    #: invents one; empty means nothing was captured and identity=browser is then
    #: refused at run time. Emitted only when present.
    browser_ua: str = ""
    #: The tree an ``html`` template reads its response with.
    #: ``"html5"`` (``html_extract.HTML5_PARSER``): the elements whose end tag
    #: the markup left out are closed as a browser closes them. Empty: only an
    #: end tag closes an element, the tree every HTML template compiled before
    #: the marker existed was proved on. Emitted only when present.
    html_parser: str = ""
    #: A read-only POST to the site's search provider that the site's own page
    #: sent itself, in the read shape ``web_scope.WebScope.observed_call``
    #: accepts: the host of that site. Such a request runs without
    #: ``ANYUSE_API_READONLY_POST`` once its host is acknowledged, and the
    #: shape is checked again on every send. Emitted only when present.
    provider_read: str = ""

    def __post_init__(self) -> None:
        from anyuse.execution.workflow_schema import ParameterSpec, input_spec, value_type
        if self.slots and not self.schema_minor:
            self.schema_minor = _slots_minor()
        if not self.parameter_schema:
            self.parameter_schema = [input_spec(str(name)).to_dict() for name in self.params]
        existing = {str(row.get("name")) for row in self.parameter_schema}
        for key, value in self.query.items():
            if _PLACEHOLDER.search(str(value)):
                continue
            name = "const_query_" + re.sub(r"[^A-Za-z0-9_]", "_", str(key))
            if name not in existing:
                self.parameter_schema.append(ParameterSpec(
                    name=name, kind="const", type=value_type(value), value=value,
                    destinations=(f"query:{key}",), max_length=max(1, len(str(value))),
                ).to_dict())
                existing.add(name)
        if not self.params:
            self.params = [str(row.get("name")) for row in self.parameter_schema
                           if row.get("kind") in ("input", "derived")]

    @property
    def input_params(self) -> List[str]:
        from anyuse.execution.workflow_schema import input_names, specs_from_dicts
        return input_names(specs_from_dicts(self.parameter_schema))

    @property
    def implied_end_tags(self) -> bool:
        """Read the response with the implied end tags (see :attr:`html_parser`)."""
        from anyuse.execution.html_extract import HTML5_PARSER
        return self.html_parser == HTML5_PARSER

    @property
    def effective_minor(self) -> int:
        """The minor this template is written as: at least the slots' minor once
        it carries slots, and at least :data:`PAGE_SCHEMA_MINOR` once it reads a
        value from the site's page, however they were set (a rung may set them
        after construction)."""
        minor = self.schema_minor
        if self.slots and 0 <= minor < _slots_minor():
            minor = _slots_minor()
        if self.reads_page and 0 <= minor < PAGE_SCHEMA_MINOR:
            minor = PAGE_SCHEMA_MINOR
        return minor

    @property
    def reads_page(self) -> bool:
        """Does this request read the site's page or send a value read from it?"""
        return (self.extract_kind == "page_text" or bool(self.provider_read)
                or bool(self.page_params()))

    def page_params(self) -> Dict[str, Dict[str, Any]]:
        """``{name: spec}`` of the values this request sends that a plan read
        from the site's page (derived, ``source["from"] == "page"``)."""
        return {str(row.get("name")): row for row in self.parameter_schema
                if row.get("kind") == "derived"
                and str((row.get("source") or {}).get("from") or "") == "page"}

    def page_fields(self) -> Dict[str, str]:
        """``{field: name}``: the query keys, headers (lower case) and body keys
        whose whole value is a page value's placeholder."""
        page = self.page_params()
        out: Dict[str, str] = {}
        if not page:
            return out

        def name_of(text: Any) -> str:
            m = _PLACEHOLDER.fullmatch(str(text or ""))
            return m.group(1) if m and m.group(1) in page else ""

        for key, val in self.query.items():
            if name_of(val):
                out[str(key)] = name_of(val)
        for key, val in urllib.parse.parse_qsl(urllib.parse.urlsplit(self.url).query,
                                               keep_blank_values=True):
            if name_of(val):
                out[str(key)] = name_of(val)
        for key, val in self.headers.items():
            if name_of(val):
                out[str(key).lower()] = name_of(val)
        for key, val in _body_pairs(self.body) + _form_pairs(self.body):
            if name_of(val):
                out[str(key)] = name_of(val)
        return out

    def page_exemption(self, values: Optional[Dict[str, Any]] = None) -> Dict[str, set]:
        """``{field: {value}}``: what the read-only rules let through in the
        page-value fields, exactly. ``values`` are the bound page values; without
        them the template's own placeholders."""
        out: Dict[str, set] = {}
        for fld, name in self.page_fields().items():
            if values is None:
                val = "{%s}" % name
            elif name in values and values[name] is not None:
                val = str(values[name])
            else:
                continue
            out.setdefault(fld.lower(), set()).add(val)
        return out

    # -- serialization (matches the project's to_dict/from_dict idiom) -------
    def to_dict(self) -> dict:
        out = {
            "schema_version": self.schema_version,
            "method": self.method, "url": self.url, "query": dict(self.query),
            "headers": dict(self.headers), "body": self.body,
            "timeout_s": self.timeout_s, "extract": dict(self.extract),
            "extract_kind": self.extract_kind, "embedded": dict(self.embedded),
            "shape": self.shape, "expect": dict(self.expect),
            "params": list(self.params), "origin": self.origin, "notes": self.notes,
            "readonly_post": bool(self.readonly_post),
            "path_params": dict(self.path_params),
            "param_transforms": dict(self.param_transforms),
            "approved_origins": list(self.approved_origins),
            "parameter_schema": [dict(row) for row in self.parameter_schema],
            # schema 2.1 keys exist only on a slotted (or newer) template, so a
            # schema-2 template's dict, and the proof digest pinned to it, is unchanged
            **({"schema_minor": self.effective_minor} if self.effective_minor else {}),
            **({"slots": [dict(slot) for slot in self.slots]} if self.slots else {}),
        }
        # Emitted only when present, so a credential-free template (every one
        # compiled today) serialises byte-for-byte as before.
        if self.cookie_refs:
            out["cookie_refs"] = list(self.cookie_refs)
        if self.browser_ua:
            out["browser_ua"] = self.browser_ua
        if self.html_parser:
            out["html_parser"] = self.html_parser
        if self.provider_read:
            out["provider_read"] = self.provider_read
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "RequestTemplate":
        data = data or {}
        return cls(
            method=str(data.get("method", "GET")).upper(),
            url=str(data.get("url", "")),
            query=dict(data.get("query") or {}),
            headers=dict(data.get("headers") or {}),
            body=data.get("body"),
            timeout_s=float(data.get("timeout_s", 10.0) or 10.0),
            extract=dict(data.get("extract") or {}),
            extract_kind=str(data.get("extract_kind") or "json"),
            embedded=dict(data.get("embedded") or {}),
            shape=str(data.get("shape") or "list"),
            expect=dict(data.get("expect") or {}),
            params=list(data.get("params") or []),
            path_params={str(k): int(v) for k, v in (data.get("path_params") or {}).items()},
            param_transforms={str(k): str(v) for k, v
                              in (data.get("param_transforms") or {}).items()},
            origin=str(data.get("origin", "deterministic")),
            notes=str(data.get("notes", "")),
            readonly_post=bool(data.get("readonly_post", False)),
            approved_origins=list(data.get("approved_origins") or []),
            parameter_schema=[dict(row) for row in (data.get("parameter_schema") or [])],
            slots=[dict(slot) for slot in (data.get("slots") or [])],
            schema_version=_int_field(data.get("schema_version"), TEMPLATE_SCHEMA_VERSION),
            schema_minor=_int_field(data.get("schema_minor"), 0),
            cookie_refs=[str(n) for n in (data.get("cookie_refs") or [])],
            browser_ua=str(data.get("browser_ua") or ""),
            html_parser=str(data.get("html_parser") or ""),
            provider_read=str(data.get("provider_read") or ""),
        )

    # -- validation ----------------------------------------------------------
    def problems(self, bound: bool = False,
                 exempt: Optional[Dict[str, Any]] = None) -> List[str]:
        """Everything wrong with this template, as human-readable strings.

        A non-empty list means the template must NOT be persisted or executed.
        Checks: scheme, method, header allowlist, credential headers, signed /
        session-bound parameters, and placeholders with no declared parameter.

        A slotted template (and, with ``bound=True``, a filled slotted request)
        is raw captured text: instead of the placeholder scan, its slots must
        describe the request and the read-only rules are applied to every
        decoded layer (``api_slots.deep_refusal``).

        ``exempt`` (``{field: values}``, default :meth:`page_exemption`): a
        query key, header or body key whose value is exactly one of its values
        is a value the site's page stated, so its credential-like name does not
        refuse it. A ``cookie``, ``set-cookie``, ``authorization`` or
        ``proxy-authorization`` header is never exempt.
        """
        out: List[str] = []
        version_why = template_version_problem(self.schema_version, self.effective_minor)
        if version_why:
            return [version_why]
        exempt = {str(k).lower(): {str(v) for v in vals}
                  for k, vals in (self.page_exemption() if exempt is None else exempt).items()}

        def exempted(key: Any, value: Any) -> bool:
            low = str(key).lower()
            return low not in NEVER_PAGE_HEADERS and str(value) in exempt.get(low, ())

        raw_text = bound or bool(self.slots)
        from anyuse.execution.workflow_schema import specs_from_dicts, validate_specs
        specs = specs_from_dicts(self.parameter_schema)
        out.extend(validate_specs(specs))
        for spec in specs:
            if spec.kind != "const":
                continue
            for destination in spec.destinations:
                if destination.startswith("query:"):
                    key = destination.split(":", 1)[1]
                    if key not in self.query or self.query[key] != spec.value:
                        out.append(f"const {spec.name!r} changed at query field {key!r}; "
                                   "the proved template is invalid")
        scheme = urllib.parse.urlsplit(self.url).scheme
        from anyuse.execution.request_policy import is_local_fixture_origin
        if scheme != "https" and not (scheme == "http" and is_local_fixture_origin(self.url)):
            out.append(f"url scheme must be https (got {scheme or 'none'!r})")
        if not (0 < float(self.timeout_s) <= MAX_REQUEST_TIMEOUT_S):
            out.append(f"timeout {self.timeout_s!r}s is outside (0, "
                       f"{MAX_REQUEST_TIMEOUT_S}] seconds")
        method = self.method.upper()
        if method not in READ_METHODS:
            if method == "POST" and self.readonly_post:
                out.extend(self._readonly_post_problems(exempt))
            else:
                out.append(f"method {self.method!r} is not a read method "
                           f"({'/'.join(sorted(READ_METHODS))}) — needs the HIL gate")
        if self.provider_read and not (method == "POST" and self.readonly_post):
            out.append("a provider read is a read-only POST; this template is not one")
        if self.provider_read and not re.fullmatch(r"[a-z0-9.-]{1,253}(:\d{1,5})?",
                                                   self.provider_read):
            out.append(f"provider_read {self.provider_read[:60]!r} is not a host")
        for h, v in self.headers.items():
            low = h.lower()
            if exempted(low, v):
                continue
            if low in CREDENTIAL_HEADERS:
                out.append(f"credential-bearing header {h!r} is out of scope")
            elif low not in SAFE_HEADERS:
                out.append(f"header {h!r} is not in the safe-header allowlist")
        for k, v in list(self.query.items()) + _body_pairs(self.body):
            if exempted(k, v):
                continue
            why = signed_param_reason(k)
            if why:
                out.append(why)
        declared = {spec.name for spec in specs if spec.kind in ("input", "derived")}
        if raw_text:
            out.extend(self._raw_text_problems(declared, bound, exempt))
        else:
            for text in [self.url, *self.query.values(), self.body or ""]:
                for name in _PLACEHOLDER.findall(text or ""):
                    if name not in declared:
                        out.append(f"placeholder {{{name}}} has no declared parameter")
        url_placeholders = set(_PLACEHOLDER.findall(self.url or ""))
        for name, span in self.path_params.items():
            if name not in url_placeholders:
                out.append(f"path_params names {name!r}, which is not a placeholder "
                           f"in the url — a multi-segment binding that binds nothing "
                           f"is a lie about the template's shape")
            if int(span) < 1:
                out.append(f"path_params[{name!r}] = {span}: a parameter cannot span "
                           f"fewer than one path segment")
        from anyuse.execution.param_binding import TRANSFORMS
        known = {n for n, _ in TRANSFORMS}
        for name, transform in self.param_transforms.items():
            if transform not in known:
                out.append(f"unknown parameter transform {transform!r} for {name!r} "
                           f"(known: {sorted(known)})")
        if self.html_parser and not self.implied_end_tags:
            out.append(f"unknown html_parser {self.html_parser!r}; update AnyUse or "
                       f"recompile the tool")
        return out

    def _raw_text_problems(self, declared: set, bound: bool,
                           exempt: Optional[Dict[str, set]] = None) -> List[str]:
        """Slot validity and the read-only rules over every decoded layer."""
        from anyuse.execution import api_slots
        out: List[str] = []
        if not bound:
            if self.query:
                out.append("a slotted template keeps its query in the url verbatim; "
                           "the query field must be empty")
            if self.path_params or self.param_transforms:
                out.append("a slotted template binds no path_params/param_transforms")
            out.extend(api_slots.slot_problems(self.url, self.body, self.slots, declared))
        if not (self.method.upper() == "POST" and self.readonly_post):
            # a read-only POST gets the same scan in _readonly_post_problems
            why = api_slots.deep_refusal(self.url, self.body, exempt=exempt or None)
            if why:
                out.append(why)
        return out

    def _readonly_post_problems(self, exempt: Optional[Dict[str, set]] = None) -> List[str]:
        """What still disqualifies a POST that claims to be a read.

        The claim is not taken on trust: this re-derives it from the template
        alone, so a hand-edited or round-tripped ``compiled_api.json`` cannot
        smuggle a mutation through by setting one boolean.
        """
        out: List[str] = []
        body = self.body or ""
        if not body:
            out.append("a read-only POST must carry a body — an empty POST is "
                       "indistinguishable from a write")
        if len(body) > READONLY_POST_MAX_BODY:
            out.append(f"read-only POST body is {len(body)} bytes, over the "
                       f"{READONLY_POST_MAX_BODY}-byte cap")
        kind, op, why = readonly_post_shape(body)
        if why:
            out.append(why)
        elif kind == "graphql" and op not in ("query", "anonymous"):
            out.append(f"GraphQL operation is a {op!r}, not a query — refused")
        # the rung's rules over every decoded layer, for every read-only POST,
        # on the template and (bound_request_problems) on the filled request
        from anyuse.execution.api_slots import deep_refusal
        deep = deep_refusal(self.url, body, exempt=exempt or None)
        if deep:
            out.append(deep)
        return out

    # -- rendering -----------------------------------------------------------
    def render(self, params: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, str], Optional[str]]:
        """Bind ``params`` into the template → ``(url, headers, body)``.

        Path placeholders are percent-encoded with no safe characters (a param
        can never inject a new path segment or query string); query values are
        encoded by ``urlencode``. A missing parameter raises ``KeyError`` — a
        half-bound URL must never reach the wire.

        A parameter listed in :attr:`path_params` is the one exception, and a
        narrow one: it may contribute exactly the number of ``/`` characters the
        recordings proved, each of its segments is still encoded with no safe
        characters, and a value of the wrong shape raises
        :class:`param_binding.PathBindError` instead of reshaping the URL.
        """
        from anyuse.execution.param_binding import apply_transform, bind_path_value
        from anyuse.execution.workflow_schema import resolve_values, specs_from_dicts
        params = params or {}
        resolved, errors = resolve_values(
            specs_from_dicts(self.parameter_schema), params,
            destination_step=getattr(params, "destination_step", ""),
            destination_url=self.url,
        )
        if errors:
            missing = [e for e in errors if e.startswith("missing input ")]
            if len(errors) == 1 and missing:
                name = missing[0].split("'", 2)[1]
                raise KeyError(f"missing workflow parameter {name!r} for api step")
            raise ValueError("parameter dataflow refused: " + "; ".join(errors))

        def typed(name: str) -> str:
            value = str(resolved[name])
            transform = self.param_transforms.get(name)
            return apply_transform(transform, value) if transform else value

        def sub_path(m: "re.Match[str]") -> str:
            name = m.group(1)
            value = typed(name)
            span = int(self.path_params.get(name, 1))
            return bind_path_value(value, span)

        def sub_raw(m: "re.Match[str]") -> str:
            return typed(m.group(1))

        if self.slots:
            # Schema 2.1: fill the slots in the verbatim request. Only the
            # layers a slot touches are decoded and re-encoded.
            from anyuse.execution.api_slots import fill_request
            try:
                url, body = fill_request(self.url, self.body, self.slots, resolved)
            except KeyError as exc:
                raise KeyError(f"missing workflow parameter {exc.args[0]!r} for api step") from None
            return url, self._render_headers(typed), body

        try:
            url = _PLACEHOLDER.sub(sub_path, self.url)
            query = {k: _PLACEHOLDER.sub(sub_raw, v) for k, v in self.query.items()}
            body = _bind_body(self.body, resolved, typed) if self.body else None
        except KeyError as exc:
            raise KeyError(f"missing workflow parameter {exc.args[0]!r} for api step") from None
        if query:
            sep = "&" if urllib.parse.urlsplit(url).query else "?"
            url = url + sep + urllib.parse.urlencode(query, doseq=False)
        return url, self._render_headers(typed), body

    def _render_headers(self, typed) -> Dict[str, str]:
        """The safe headers as written, and each page-value header with its value."""
        fields = self.page_fields()
        out: Dict[str, str] = {}
        for k, v in self.headers.items():
            low = k.lower()
            if low in SAFE_HEADERS:
                out[k] = v
            elif low in fields and low not in NEVER_PAGE_HEADERS:
                out[k] = typed(fields[low])
        return out


def _bind_body(body: str, params: Dict[str, Any], typed) -> str:
    """Bind body values after decoding, then serialize them again.

    Raw substitution into serialized JSON/form data lets quotes or ``&`` alter
    the request's fields.  Keys are therefore immutable, exact scalar
    placeholders receive the caller's scalar value, interpolated placeholders
    stay strings, and the standard encoders recreate the wire representation.
    """
    raw = str(body)
    stripped = raw.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"cannot bind an invalid JSON body: {exc}") from None

        def bind_json(node: Any) -> Any:
            if isinstance(node, dict):
                for key in node:
                    if _PLACEHOLDER.search(str(key)):
                        raise ValueError("body placeholders in object keys may change request structure")
                return {key: bind_json(value) for key, value in node.items()}
            if isinstance(node, list):
                return [bind_json(value) for value in node]
            if isinstance(node, str):
                exact = _PLACEHOLDER.fullmatch(node)
                if exact:
                    value = params[exact.group(1)]
                    if isinstance(value, (dict, list)):
                        raise ValueError("body parameters must be scalar values")
                    return value
                return _PLACEHOLDER.sub(lambda m: typed(m.group(1)), node)
            return node

        return json.dumps(bind_json(decoded), separators=(",", ":"), ensure_ascii=False)

    if "=" in raw and "\n" not in raw:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        bound = []
        for key, value in pairs:
            if _PLACEHOLDER.search(key):
                raise ValueError("body placeholders in form keys may change request structure")
            bound.append((key, _PLACEHOLDER.sub(lambda m: typed(m.group(1)), value)))
        return urllib.parse.urlencode(bound, doseq=False)

    if _PLACEHOLDER.search(raw):
        raise ValueError("body with placeholders must be valid JSON or form data")
    return raw


def bound_request_problems(template: "RequestTemplate", url: str,
                           headers: Dict[str, str], body: Optional[str],
                           page_values: Optional[Dict[str, Any]] = None) -> List[str]:
    """Validate and classify the complete rendered request just before send.

    ``page_values`` (``{name: value}``): the values this request sends that
    the plan read from the site's page; exactly those values pass the
    credential-name rules in exactly their fields (:meth:`RequestTemplate.page_exemption`)."""
    exempt = template.page_exemption(page_values or {}) if page_values is not None else {}
    bound = RequestTemplate.from_dict({
        **template.to_dict(), "url": url, "query": {}, "headers": headers,
        "body": body, "params": [], "path_params": {}, "param_transforms": {},
        "parameter_schema": [], "slots": [],
    })
    # A filled slotted request is raw text: no placeholder scan, and the
    # read-only rules run again over every decoded layer of what is sent.
    return bound.problems(bound=bool(getattr(template, "slots", None)), exempt=exempt)


def _slots_minor() -> int:
    from anyuse.execution.api_slots import SLOTS_SCHEMA_MINOR
    return SLOTS_SCHEMA_MINOR


def _int_field(value: Any, default: int) -> int:
    """An integer format field; anything else reads as an unknown future (-1
    is never valid, so :func:`template_version_problem` refuses it)."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return -1
    return value


def template_version_problem(version: int, minor: int) -> str:
    """Why this AnyUse must not run a template of format ``version.minor``, or "".

    Version 2 with minor 0 (plain), 1 (slots) or 2 (page values,
    :data:`PAGE_SCHEMA_MINOR`) is what this code reads. A newer one may carry
    fields this runner would drop (an older AnyUse drops ``slots`` and would
    send the recorded request verbatim), so it is refused, as
    ``draft_compile.record_version_problem`` refuses a newer record.
    """
    known = max(_slots_minor(), PAGE_SCHEMA_MINOR)
    if version != TEMPLATE_SCHEMA_VERSION or not (0 <= minor <= known):
        return (f"the template's format {version}.{minor} is not one this AnyUse reads "
                f"(up to {TEMPLATE_SCHEMA_VERSION}.{known}); update AnyUse or recompile")
    return ""


def _body_pairs(body: Optional[str]) -> List[Tuple[str, Any]]:
    if not body:
        return []
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    return [(p.split(".")[-1], v) for p, v in json_leaves(data)]


def _form_pairs(body: Optional[str]) -> List[Tuple[str, Any]]:
    raw = str(body or "")
    if not raw or raw.lstrip()[:1] in "{[" or "=" not in raw or "\n" in raw:
        return []
    try:
        return urllib.parse.parse_qsl(raw, keep_blank_values=True)
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# RequestPlan — two or more DEPENDENT requests
# ---------------------------------------------------------------------------

#: ``PlanStep.pick[...]["by"]``: bind the row whose name is the caller's input.
PICK_BY_NAME = "name"
#: :func:`pick_row`'s rule when the rows carry names and none is the caller's input.
PICK_NONE_NAMED = "none"


def keyed_rows(data: Any, path: str) -> Optional[List[Tuple[int, Any, Any]]]:
    """``(index, row, key)`` for each row of ``path``'s list that has a key.

    ``path`` has exactly one ``[*]`` (``crates[*].id``): the part before it is
    the list, the part after it the key inside one row. Rows without a scalar
    key are left out, as ``json_path`` leaves them out of the first-entry
    binding. Returns ``None`` when ``path`` does not name such a list.
    """
    if path.count("[*]") != 1:
        return None
    head, _, tail = path.partition("[*]")
    tail = tail.lstrip(".")
    if "[" in tail:
        return None
    rows = json_path(data, head) if head else data
    if not isinstance(rows, list):
        return None
    out: List[Tuple[int, Any, Any]] = []
    for index, row in enumerate(rows):
        key = json_path(row, tail) if tail else row
        if isinstance(key, (str, int, float)) and not isinstance(key, bool):
            out.append((index, row, key))
    return out


def pick_row(data: Any, path: str, pick: Dict[str, Any],
             wanted: Any) -> Tuple[Any, int, str]:
    """The key of the row in ``path``'s list whose name is ``wanted``.

    The name is the field ``pick["name_path"]`` of a row, or the key itself
    when that is empty. An exact match wins, then a normalised one (case,
    punctuation and spacing ignored). When no row has that name, the first row
    is taken only if ``pick["else_first"]`` is set, because a recording showed
    the page opening it in that case. Returns ``(key, row index, rule)`` with
    rule ``exact``, ``normalised`` or ``first``; ``(None, -1, "none")`` when the
    rows carry names and none is ``wanted``; ``(None, -1, "")`` when there is
    nothing to choose by (no list, no keyed row, or no row carrying the name
    field), which is a response that changed shape, not an answer.
    """
    from anyuse.execution.api_compile import normalise
    rows = keyed_rows(data, path)
    if not rows:
        return None, -1, ""
    name_path = str(pick.get("name_path") or "")

    def name_of(row: Any, key: Any) -> Optional[str]:
        name = json_path(row, name_path) if name_path else key
        if isinstance(name, (str, int, float)) and not isinstance(name, bool):
            return str(name)
        return None

    text = "" if wanted is None else str(wanted)
    names = [(index, key, name_of(row, key)) for index, row, key in rows]
    if all(name is None for _index, _key, name in names):
        return None, -1, ""
    for index, key, name in names:
        if name is not None and name == text:
            return key, index, "exact"
    want = normalise(text)
    if want:
        for index, key, name in names:
            if name is not None and normalise(name) == want:
                return key, index, "normalised"
    if pick.get("else_first"):
        index, _row, key = rows[0]
        return key, index, "first"
    return None, -1, PICK_NONE_NAMED


@dataclass
class PlanStep:
    """One request in a plan, plus what later steps may take from its response.

    ``bind`` maps a NEW parameter name to a JSON path into THIS step's response.
    A path that resolves to a list binds its FIRST entry — "the first result",
    which is what clicking the top row of a result list does — unless ``pick``
    names a rule for that binding: ``{"by": "name", "input": <plan input>,
    "name_path": <field of a row>, "else_first": bool}`` binds the row whose
    name is the caller's input (:func:`pick_row`).
    """
    name: str = ""
    template: RequestTemplate = field(default_factory=RequestTemplate)
    bind: Dict[str, str] = field(default_factory=dict)
    pick: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: ``name -> page_literals locator``: a value this step's response (a page
    #: of the site, read as text) states by name, read by the locator on every
    #: run. The locator is the name and its punctuation, never the value.
    #: Emitted only when present.
    page_bind: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {"name": self.name, "bind": dict(self.bind)}
        if self.pick:
            out["pick"] = {name: dict(rule) for name, rule in self.pick.items()}
        if self.page_bind:
            out["page_bind"] = {name: dict(loc) for name, loc in self.page_bind.items()}
        return {**out, **self.template.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        data = dict(data or {})
        name = str(data.pop("name", ""))
        bind = dict(data.pop("bind", {}) or {})
        pick = {str(key): dict(rule or {})
                for key, rule in dict(data.pop("pick", {}) or {}).items()}
        page_bind = {str(key): dict(loc or {})
                     for key, loc in dict(data.pop("page_bind", {}) or {}).items()}
        return cls(name=name, bind=bind, pick=pick, page_bind=page_bind,
                   template=RequestTemplate.from_dict(data))


@dataclass
class RequestPlan:
    """An ordered chain of requests where later steps use earlier responses.

    Deliberately NOT a general workflow language: a plan is a straight line, each
    step is a :class:`RequestTemplate`, and the only dataflow is ``bind``. That
    covers the shape a recorded read-only UI flow actually produces — *search,
    then open the first result* — and it stays auditable, which a loop or a
    conditional would not.

    It carries the same surface as a single template (``params``, ``shape``,
    ``extract``, ``problems()``), so the executor, the verifier and the ladder
    treat one request and a chain identically.
    """
    steps: List[PlanStep] = field(default_factory=list)
    params: List[str] = field(default_factory=list)
    shape: str = "list"
    origin: str = "deterministic"
    notes: str = ""
    parameter_schema: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        from anyuse.execution.request_policy import origin as request_origin
        from anyuse.execution.workflow_schema import ParameterSpec, input_spec
        if not self.parameter_schema:
            specs = [input_spec(str(name)).to_dict() for name in self.params]
            for index, producer in enumerate(self.steps):
                for name, path in producer.bind.items():
                    output_name = next((output for output, output_path
                                        in producer.template.extract.items()
                                        if output_path == path), str(name))
                    consumers = [step.name for step in self.steps[index + 1:]
                                 if name in step.template.params]
                    origins = [request_origin(step.template.url)
                               for step in self.steps[index + 1:]
                               if name in step.template.params and request_origin(step.template.url)]
                    specs.append(ParameterSpec(
                        name=str(name), kind="derived", type="string",
                        max_length=4096,
                        source={"step": producer.name, "output": output_name,
                                "path": str(path)},
                        destinations=tuple(consumers),
                        destination_origins=tuple(dict.fromkeys(origins)),
                    ).to_dict())
                for name, loc in producer.page_bind.items():
                    specs.append(page_spec(name, loc, producer.name, self.steps[index + 1:]))
            self.parameter_schema = specs
        by_name = {str(row.get("name")): dict(row) for row in self.parameter_schema}
        self.params = [name for name, row in by_name.items() if row.get("kind") == "input"]
        # A consuming request validates the same derived contract as the plan;
        # it must never silently downgrade response data into an input.
        for step in self.steps:
            rows = [dict(row) for row in step.template.parameter_schema
                    if row.get("kind") == "const"]
            for name in step.template.params:
                rows.append(dict(by_name.get(name) or {
                    "name": name, "kind": "input", "type": "string",
                    "max_length": 2048,
                }))
            step.template.parameter_schema = rows

    @property
    def input_params(self) -> List[str]:
        from anyuse.execution.workflow_schema import input_names, specs_from_dicts
        return input_names(specs_from_dicts(self.parameter_schema))

    def type_derived(self, name: str, value_type: str) -> None:
        """Declare the JSON type of the derived value ``name``.

        A producer that returns its keys as numbers (``{"id": 4021}``) binds a
        number, and the runtime check (``ParameterSpec.validate``) refuses a
        number declared as a string. The type is set on the plan's contract and
        on the copy each consuming step validates against."""
        for rows in [self.parameter_schema,
                     *(step.template.parameter_schema for step in self.steps)]:
            for row in rows:
                if row.get("name") == name and row.get("kind") == "derived":
                    row["type"] = value_type

    def dataflow_contract(self) -> Dict[str, Any]:
        """Auditable named-input/output contract for end-to-end proof."""
        by_name = {str(row.get("name")): row for row in self.parameter_schema}
        steps = []
        for step in self.steps:
            bindings = []
            for name, path in step.bind.items():
                spec = by_name.get(name) or {}
                row = step.pick.get(name)
                bindings.append({"name": name, "source_output":
                                 (spec.get("source") or {}).get("output", ""),
                                 "response_path": path,
                                 "destinations": list(spec.get("destinations") or []),
                                 **({"row": dict(row)} if row else {})})
            for name, loc in step.page_bind.items():
                spec = by_name.get(name) or {}
                bindings.append({"name": name, "source_output": name,
                                 "response_path": (spec.get("source") or {}).get("path", ""),
                                 "from": "page", "names": [p[0] for p in loc.get("names") or []],
                                 "secret": bool(loc.get("secret")),
                                 "destinations": list(spec.get("destinations") or [])})
            steps.append({"name": step.name,
                          "inputs": list(step.template.params),
                          "outputs": dict(step.template.extract),
                          "ordering": ("preserved" if step.template.shape == "list"
                                       else "not_applicable"),
                          "bindings": bindings})
        return {"inputs": list(self.input_params), "steps": steps,
                "ordering": "preserved" if self.shape == "list" else "not_applicable",
                "final_output": dict(self.extract)}

    def expression(self) -> str:
        """Compact human-readable form, e.g. ``detail(id=search(q).items[0])``."""
        by_name = {str(row.get("name")): row for row in self.parameter_schema}
        picks = {name: rule for step in self.steps for name, rule in step.pick.items()}
        rendered = ""
        rendered_steps: Dict[str, str] = {}
        for step in self.steps:
            args = []
            for name in step.template.params:
                spec = by_name.get(name) or {}
                if spec.get("kind") == "derived" and (spec.get("source") or {}).get("from") == "page":
                    source = spec.get("source") or {}
                    upstream = rendered_steps.get(
                        str(source.get("step")), f"{source.get('step')}(...)")
                    names = [p[0] for step2 in self.steps if step2.name == source.get("step")
                             for p in (step2.page_bind.get(name) or {}).get("names") or []]
                    args.append(f"{name}={upstream}.page[{names[0] if names else '?'}]")
                elif spec.get("kind") == "derived":
                    source = spec.get("source") or {}
                    upstream = rendered_steps.get(
                        str(source.get("step")), f"{source.get('step')}(...)")
                    rule = picks.get(name)
                    row = (f"[{rule.get('name_path') or 'key'}={rule.get('input')}]"
                           if rule else "[0]")
                    args.append(f"{name}={upstream}."
                                f"{source.get('output')}{row}")
                else:
                    args.append(name)
            rendered = f"{step.name}({', '.join(args)})"
            rendered_steps[step.name] = rendered
        return rendered

    @property
    def extract(self) -> Dict[str, str]:
        return self.steps[-1].template.extract if self.steps else {}

    @property
    def method(self) -> str:
        return self.steps[-1].template.method if self.steps else "GET"

    @property
    def url(self) -> str:
        return self.steps[-1].template.url if self.steps else ""

    def to_dict(self) -> dict:
        return {"schema_version": 2, "steps": [s.to_dict() for s in self.steps],
                "params": list(self.params),
                "parameter_schema": [dict(row) for row in self.parameter_schema],
                "shape": self.shape, "origin": self.origin, "notes": self.notes,
                "dataflow_contract": self.dataflow_contract()}

    @classmethod
    def from_dict(cls, data: dict) -> "RequestPlan":
        data = data or {}
        return cls(steps=[PlanStep.from_dict(s) for s in (data.get("steps") or [])],
                   params=list(data.get("params") or []),
                   shape=str(data.get("shape") or "list"),
                   origin=str(data.get("origin", "deterministic")),
                   notes=str(data.get("notes", "")),
                   parameter_schema=[dict(row) for row in
                                     (data.get("parameter_schema") or [])])

    def problems(self) -> List[str]:
        """Every step's problems, plus plan-level ones.

        A placeholder must be satisfied by a plan parameter or by a value bound
        by an EARLIER step; a step that reads a binding produced later (or never)
        is rejected here rather than failing halfway through a live chain.
        """
        out: List[str] = []
        from anyuse.execution.workflow_schema import specs_from_dicts, validate_specs
        specs = specs_from_dicts(self.parameter_schema)
        out.extend(validate_specs(specs))
        if not self.steps:
            return ["plan has no steps"]
        if len(self.steps) > MAX_CHAIN_REQUESTS:
            out.append(f"plan has {len(self.steps)} requests, cap is {MAX_CHAIN_REQUESTS}")
        available = {spec.name for spec in specs if spec.kind in ("input", "const")}
        inputs = {spec.name for spec in specs if spec.kind == "input"}
        spec_by_name = {spec.name: spec for spec in specs}
        for i, step in enumerate(self.steps):
            declared = set(step.template.params)
            missing = declared - available
            if missing:
                out.append(f"step {i + 1} ({step.name!r}) needs {sorted(missing)}, "
                           f"which no earlier step provides")
            probe = RequestTemplate.from_dict({**step.template.to_dict(),
                                               "params": sorted(available | declared)})
            out.extend(f"step {i + 1} ({step.name!r}): {p}" for p in probe.problems())
            for name, path in step.bind.items():
                spec = spec_by_name.get(name)
                if spec is None or spec.kind != "derived":
                    out.append(f"step {i + 1} ({step.name!r}) output {name!r} is not "
                               "declared as derived data")
                elif spec.source.get("step") != step.name or spec.source.get("path") != path:
                    out.append(f"step {i + 1} ({step.name!r}) derived source for "
                               f"{name!r} does not match its declared dataflow")
            if step.page_bind and step.template.extract_kind not in ("page_text", "html"):
                out.append(f"step {i + 1} ({step.name!r}) reads page values from a "
                           f"{step.template.extract_kind!r} response; only a page read as "
                           "text (page_text or html) states them")
            for name, loc in step.page_bind.items():
                from anyuse.execution.page_literals import Locator, locator_problems
                spec = spec_by_name.get(name)
                bad = locator_problems(loc)
                if bad:
                    out.extend(f"step {i + 1} ({step.name!r}) page value {name!r}: {b}"
                               for b in bad)
                elif name in step.bind:
                    out.append(f"step {i + 1} ({step.name!r}) binds {name!r} twice")
                elif spec is None or spec.kind != "derived" \
                        or spec.source.get("from") != "page" \
                        or spec.source.get("step") != step.name \
                        or spec.source.get("path") != "page:" + Locator.from_dict(loc).ident():
                    out.append(f"step {i + 1} ({step.name!r}) page value {name!r} does not "
                               "match its declared dataflow")
            for name, rule in step.pick.items():
                where = f"step {i + 1} ({step.name!r}) row rule for {name!r}"
                path = step.bind.get(name)
                if path is None:
                    out.append(f"{where} has no binding to choose a row for")
                elif rule.get("by") != PICK_BY_NAME:
                    out.append(f"{where} is unknown: {rule.get('by')!r}")
                elif rule.get("input") not in inputs:
                    out.append(f"{where} names {rule.get('input')!r}, which is not an input")
                elif path.count("[*]") != 1 or "[" in path.partition("[*]")[2]:
                    out.append(f"{where} needs a path into one list, not {path!r}")
                elif "[" in str(rule.get("name_path") or ""):
                    out.append(f"{where} reads its name from inside a list")
            available |= set(step.bind) | set(step.page_bind)
        return out


def page_spec(name: str, loc: Dict[str, Any], step: str,
              later: List["PlanStep"]) -> Dict[str, Any]:
    """The derived contract of a value ``step`` reads from the site's page: its
    source names the locator (never the value), and it may go only to the later
    steps that send it, at their origins."""
    from anyuse.execution.page_literals import Locator
    from anyuse.execution.request_policy import origin as request_origin
    from anyuse.execution.workflow_schema import ParameterSpec
    locator = Locator.from_dict(loc)
    consumers = [s.name for s in later if name in s.template.params]
    origins = [request_origin(s.template.url) for s in later
               if name in s.template.params and request_origin(s.template.url)]
    source = {"step": step, "output": str(name), "path": "page:" + locator.ident(),
              "from": "page"}
    if locator.secret:
        source["secret"] = "yes"
    return ParameterSpec(name=str(name), kind="derived", type="string",
                         max_length=locator.max_len, source=source,
                         destinations=tuple(consumers),
                         destination_origins=tuple(dict.fromkeys(origins))).to_dict()


def load_command(cmd: Any):
    """Read a stored ``api`` command back as a plan or a single template.

    A chain is recognised by its ``steps`` key; anything else is a single
    template, so commands compiled before plans existed load unchanged.
    """
    if isinstance(cmd, dict) and "steps" in cmd:
        return RequestPlan.from_dict(cmd)
    return RequestTemplate.from_dict(cmd)


# ---------------------------------------------------------------------------
# Postcondition
# ---------------------------------------------------------------------------

def check_expect_html(status: int, content_type: str, markup: str,
                      expect: Dict[str, Any], implied_end_tags: bool = False
                      ) -> Tuple[bool, str]:
    """Deterministic postcondition for an HTML rung. No model, no fuzz.

    ``expect`` keys: ``status_in`` (default ``[200]``), ``content_type``
    (substring), ``selector`` (the primary list selector) and ``min_items``.
    ``implied_end_tags``: the template's tree (``RequestTemplate.html_parser``).
    """
    from anyuse.execution.html_extract import extract as html_select

    allowed = expect.get("status_in") or [200]
    if status not in allowed:
        return False, f"status {status} not in {allowed}"
    ct_want = expect.get("content_type")
    if ct_want and ct_want not in (content_type or "").lower():
        return False, f"content-type {content_type!r} does not contain {ct_want!r}"
    selector = expect.get("selector")
    if selector:
        try:
            got = html_select(markup, selector, implied_end_tags=implied_end_tags)
        except ValueError as exc:
            return False, f"HTML parser refused input: {exc}"
        if expect.get("result_contract"):
            # The proved runtime result contract (execution/result_contract.py)
            # decides what a small or empty page means; a proof-time count is
            # not a property of the site.
            return True, "ok"
        min_items = int(expect.get("min_items", 1) or 1)
        if len(got) < min_items:
            return False, (f"selector {selector!r} matched {len(got)} node(s) with text, "
                           f"expected >= {min_items} — the markup probably changed")
    return True, "ok"


def check_expect_embedded(status: int, content_type: str, markup: str,
                          embedded: Dict[str, Any], expect: Dict[str, Any]
                          ) -> Tuple[bool, Any, str]:
    """Postcondition for an embedded-JSON rung. Returns ``(ok, data, reason)``.

    Two things can go wrong that a plain JSON rung cannot suffer, and each gets
    its own message so a user knows which one happened:
      * the document no longer carries that payload at all (the site stopped
        shipping it, or renamed the key) — the locator is reported;
      * the payload is there but the path inside it no longer resolves.
    After that it is the ordinary JSON postcondition on the payload.
    """
    from anyuse.execution.embedded_json import describe_locator, select_blob

    allowed = expect.get("status_in") or [200]
    if status not in allowed:
        return False, None, f"status {status} not in {allowed}"
    ct_want = expect.get("content_type")
    if ct_want and ct_want not in (content_type or "").lower():
        return False, None, f"content-type {content_type!r} does not contain {ct_want!r}"

    data = select_blob(markup, embedded)
    if data is None:
        return False, None, (f"the document no longer carries "
                             f"{describe_locator(embedded)} — the page stopped "
                             f"shipping that payload, or renamed it")
    # The JSON half is checked exactly as a JSON API response would be, minus the
    # content-type (already checked above against the DOCUMENT's type).
    sub = {k: v for k, v in (expect or {}).items() if k not in ("content_type", "status_in")}
    ok, reason = check_expect(status, "application/json", data, {**sub, "status_in": [status]})
    if not ok:
        return False, data, reason
    return True, data, "ok"


def _required_key_present(item: Dict[str, Any], path: str) -> bool:
    r"""Whether ``item`` carries an escaped dotted key path.

    A compiler path such as ``package.name`` means the nested key ``name``
    below ``package``.  A dot that belongs to a JSON key is escaped as ``\.``;
    backslash escapes the next character generally, so ``a\\\\b`` addresses
    the literal key ``a\\b``.  One-segment paths retain the old flat-key
    behavior.
    """
    parts: List[str] = []
    buf: List[str] = []
    escaped = False
    for char in str(path):
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    if escaped:
        buf.append("\\")
    parts.append("".join(buf))

    node: Any = item
    for key in parts:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def check_expect(status: int, content_type: str, data: Any, expect: Dict[str, Any]) -> Tuple[bool, str]:
    """Deterministic postcondition for an api step. No model, no fuzz.

    ``expect`` keys (all optional):
        ``status_in``       list of acceptable status codes (default ``[200]``)
        ``content_type``    substring the Content-Type must contain
        ``path``            json path to the primary collection/value
        ``min_items``       the collection must have at least this many entries
        ``required_keys``   every item must carry these escaped dotted key paths
        ``non_empty``       ``path`` must resolve to something truthy
    """
    allowed = expect.get("status_in") or [200]
    if status not in allowed:
        return False, f"status {status} not in {allowed}"
    ct_want = expect.get("content_type")
    if ct_want and ct_want not in (content_type or "").lower():
        return False, f"content-type {content_type!r} does not contain {ct_want!r}"
    path = expect.get("path")
    node = json_path(data, path) if path else data
    if expect.get("non_empty") and not node:
        return False, f"path {path!r} is empty"
    min_items = expect.get("min_items")
    if min_items is not None or expect.get("result_contract"):
        if not isinstance(node, list):
            return False, f"path {path!r} is {type(node).__name__}, expected a list"
        # with a proved result contract the count is judged by the contract
        # (execution/result_contract.py), not by a proof-time floor
        if not expect.get("result_contract") and len(node) < int(min_items):
            return False, f"path {path!r} has {len(node)} items, expected >= {min_items}"
    required = expect.get("required_keys") or []
    if required:
        items = node if isinstance(node, list) else [node]
        for i, item in enumerate(items[:20]):
            if not isinstance(item, dict):
                return False, f"item {i} of {path!r} is not an object"
            missing = [k for k in required if not _required_key_present(item, k)]
            if missing:
                return False, f"item {i} of {path!r} is missing {missing}"
    return True, "ok"


def check_extracted_fields(template, extracted: Any, schema: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Check a field tool's required extraction and its recorded value types.

    The optional schema is the value-free shape emitted by workflow_schema.
    This small subset validator keeps ordinary request execution provider-free.
    """
    if getattr(template, "shape", "list") != "fields":
        return True, "not a field tool"
    names = list((getattr(template, "extract", {}) or {}).keys())
    if not isinstance(extracted, dict) or not names or any(
            name not in extracted or extracted[name] is None for name in names):
        return False, "required extracted fields are missing or null"
    if schema is None:
        return True, "required extracted fields present"
    def matches(value, spec):
        if not isinstance(spec, dict) or set(spec) - {"type", "properties", "required", "additionalProperties", "items"}:
            return False
        kinds = spec.get("type", [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        actual = ("null" if value is None else "boolean" if type(value) is bool else
                  "number" if type(value) in (int, float) else "string" if isinstance(value, str) else
                  "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "unknown")
        if actual not in kinds and not (type(value) is int and "integer" in kinds):
            return False
        if type(value) is float and not math.isfinite(value):
            return False
        if actual == "object":
            props = spec.get("properties", {})
            if any(key not in value for key in spec.get("required", [])):
                return False
            if spec.get("additionalProperties") is False and set(value) - set(props):
                return False
            return all(matches(child, props[key]) for key, child in value.items() if key in props)
        if actual == "array" and "items" in spec:
            return all(matches(child, spec["items"]) for child in value)
        return True
    return (True, "recorded field schema matched") if matches(extracted, schema) else (False, "extracted field types differ from proof")


def check_output_identity(extracted: Dict[str, Any], params: Dict[str, Any],
                          expect: Dict[str, Any], *, template=None) -> Tuple[bool, str]:
    """Check the recording-derived semantic identity of an extraction.

    Identity is emitted only when the recorded answer itself contained the
    bound input. Replays must retain that relation; matching the same generic
    selector or JSON shape is not enough.
    """
    if template is not None:
        valid, why = check_extracted_fields(template, extracted)
        if not valid:
            return valid, why
    identity = (expect or {}).get("identity") or {}
    name = str(identity.get("parameter") or "")
    if not name:
        return True, "no semantic identity was recorded"
    if name not in params:
        return False, f"identity parameter {name!r} is missing"

    def scalars(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from scalars(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from scalars(child)
        elif value is not None:
            yield str(value)

    if identity.get("match") == "detail_key":
        from anyuse.execution.discover import field_carries_key, field_names_item
        want = str(params[name])
        if any(field_carries_key(value, want) or field_names_item(value, want)
               for value in scalars(extracted)):
            return True, f"detail field retains parameter identity {name!r}"
        return False, f"detail field no longer identifies bound parameter {name!r}"

    from anyuse.execution.api_compile import normalise, values_match
    want = normalise(params[name])
    if want and any(values_match(want, normalise(value))
                    for value in scalars(extracted)):
        return True, f"output retains parameter identity {name!r}"
    return False, f"output no longer identifies bound parameter {name!r}"


def _result_contract_verdict(template, params: Dict[str, Any], extracted: Dict[str, Any], *,
                             markup: Optional[str] = None, data: Any = None, executor=None,
                             url: str = ""):
    """The proved runtime result contract's verdict, or ``None`` for a template
    without one (the old count floor then applies, unchanged).

    An empty answer on a product call (``executor.empty_context``, set by
    ``run_compiled``) may search other inputs through ``executor`` to compare
    the page with the site's own empty page (execution/empty_page.py)."""
    from anyuse.execution import result_contract as RC
    contract = RC.contract_of(template)
    if contract is None or not RC.supports(template):
        return None
    items = (extracted or {}).get("items")
    selector = str(template.extract.get("items") or "")
    list_path = str((template.expect or {}).get("path") or "")
    ctx = getattr(executor, "empty_context", None) if executor is not None else None
    fetch = positive_ok = None
    if ctx is not None:
        qp = str(contract.get("query_param") or (list(template.params or []) or [""])[0])
        positive_ok = RC.positive_ok_for(contract, selector, list_path)
        if ctx.probe and qp:
            sent = dict(params or {})

            def fetch(value: str):
                return RC.page_from_raw(template, value,
                                        executor.execute(template, {**sent, qp: value},
                                                         _probe=True))
    if template.extract_kind == "html":
        return RC.check_html(contract, markup or "", selector,
                             params, items if isinstance(items, list) else [],
                             implied_end_tags=bool(getattr(template, "implied_end_tags",
                                                           False)),
                             empty=ctx, fetch=fetch, positive_ok=positive_ok, url=url)
    return RC.check_json(contract, data, list_path, params, items,
                         empty=ctx, fetch=fetch, positive_ok=positive_ok)


#: what an accepted empty answer tells the caller, by the cascade step that accepted it
_EMPTY_NOTES = {
    "site_empty_page": ("the page is the site's own empty page for this input; an empty answer "
                        "is the correct answer here"),
    "model_check": ("the check model read the page's line as the site saying it found nothing; "
                    "an empty answer is the correct answer here"),
}


def _contract_outcome(base: Dict[str, Any], verdict, extracted: Dict[str, Any],
                      common: Dict[str, Any], data: Any) -> Optional[Dict[str, Any]]:
    """A finished result for a refused or an evidenced-empty contract verdict;
    ``None`` when the run continues (list or sparse accepted)."""
    if verdict is None:
        return None
    if not verdict.ok:
        return {**base, **common, "success": False, "outcome_kind": "incomplete",
                "extracted": extracted, "result_contract": verdict.to_dict(),
                "error": f"result contract refused: {verdict.reason}"}
    if verdict.kind == "empty":
        return {**base, **common, "success": True, "outcome_kind": "success",
                "extracted": {**(extracted or {}), "items": []},
                "empty_state": verdict.empty_state,
                "result_contract": verdict.to_dict(), "data": data,
                "note": _EMPTY_NOTES.get(
                    verdict.empty_by, "the site reported no results for this query in its "
                                      "own words; an empty answer is the correct answer here")}
    return None


# ---------------------------------------------------------------------------
# HIL projection
# ---------------------------------------------------------------------------

def api_hil_action(template: RequestTemplate, params: Optional[Dict[str, Any]] = None) -> dict:
    """Project an api step into the loosely-typed shape ``hil_gate.classify_step``
    consumes, so the EXISTING gate — not a new one — decides.

    * ``GET``/``HEAD`` → verb ``"read"``, which is in ``hil_gate._READ_ONLY_ACTIONS``
      → ``read_only`` (unless a destructive keyword appears in the URL, in which
      case the gate's keyword rule promotes it — a conservative outcome we keep).
    * anything else → verb ``"api_<method>"``, which no action set recognises →
      the gate's fail-closed branch classifies it ``destructive``.

    For a :class:`RequestPlan` every step is projected and the MOST SEVERE
    verb wins, so a chain that ends in a write is held on the strength of that
    write even though it starts with a read.

    Pure: builds a dict, touches no OS layer, sends nothing.
    """
    if isinstance(template, RequestPlan):
        supplied = set((params or {}).keys())
        actions = [api_hil_action(
            s.template, params if set(s.template.params).issubset(supplied) else None)
            for s in template.steps]
        writes = [a for a in actions if a["action"] != "read"]
        chosen = writes[0] if writes else (actions[0] if actions else
                                           {"action": "read", "args": {}})
        chosen = dict(chosen)
        chosen["args"] = dict(chosen["args"])
        chosen["args"]["chain"] = [a["args"].get("url", "") for a in actions]
        if template.notes:
            chosen["args"]["intent"] = template.notes
        return chosen

    method = (template.method or "GET").upper()
    try:
        url, headers, body = template.render(params or {})
        complete_problems = bound_request_problems(template, url, headers, body)
    except (KeyError, ValueError):
        url, body = template.url, template.body
        complete_problems = template.problems()
        if params is not None and not complete_problems:
            complete_problems = ["request could not be bound"]
    if method in READ_METHODS and not complete_problems:
        verb = "read"
    elif method == "POST" and getattr(template, "readonly_post", False) \
            and not complete_problems:
        # Lever (c). A POST reaches the read-only verb ONLY when the
        # template re-derives its own read-only claim from the body (see
        # _readonly_post_problems) — the boolean alone is never enough. The
        # gate's destructive-keyword rule still runs over every embedded string,
        # so a body mentioning "submit"/"pay"/"delete" is promoted as usual.
        verb = "read"
    else:
        verb = f"api_{method.lower()}"
    args: Dict[str, Any] = {"url": url, "method": method}
    if body:
        args["body"] = body
    if template.notes:
        args["intent"] = template.notes
    return {"action": verb, "args": args}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

@dataclass
class ApiResponse:
    """What a transport must return. Keeps tests free of httpx."""
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    text: str = ""

    @property
    def content_type(self) -> str:
        for k, v in self.headers.items():
            if k.lower() == "content-type":
                return v
        return ""


class HttpxTransport:
    """Default transport: httpx, IPv4-forced, redirects followed, no cookies.

    IPv4 is forced by binding the local address to ``0.0.0.0`` — this machine's
    network has broken IPv6 and the stdlib tries AAAA first, adding ~4 s per dead
    address. Cookies are disabled so an api replay can never pick up or carry a
    session.
    """

    def __init__(self, timeout_s: float = 10.0, user_agent: str = ""):
        self._timeout = timeout_s
        self._ua = user_agent or "anyuse-compiled-workflow/0.1 (deterministic replay)"
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx
            class RejectAllCookies(http.cookiejar.DefaultCookiePolicy):
                def set_ok(self, cookie, request):
                    return False
                def return_ok(self, cookie, request):
                    return False
            jar = http.cookiejar.CookieJar(policy=RejectAllCookies())
            self._client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
                transport=import_module_httpx_transport(),
                headers={"User-Agent": self._ua},
            )
            # httpx copies a Cookies argument into its own jar during Client
            # construction, losing a custom CookiePolicy. Install the rejecting
            # jar afterwards so response extraction reaches this exact object.
            self._client.cookies.jar = jar
        return self._client

    @staticmethod
    def _ua_hop_ok(hop_host: str, user_agent_domain: str) -> bool:
        """Apply the browser UA only on a hop whose registrable domain is the
        entry and that the government rule does not refuse
        (``ANYUSE_REFUSE_GOV_HOSTS``)."""
        if not user_agent_domain or not hop_host:
            return False
        from anyuse.execution.gov_hosts import government_refusal
        from anyuse.execution.site_modes import site_key
        return site_key(hop_host) == user_agent_domain and not government_refusal(hop_host)

    def send(self, method: str, url: str, headers: Dict[str, str],
             body: Optional[str], timeout_s: float, *,
             user_agent: str = "", user_agent_domain: str = "",
             cookies=None) -> ApiResponse:
        from anyuse.execution.request_policy import exact_origin_allowlist
        return self.send_with_policy(method, url, headers, body, timeout_s,
                                     allowed_origins=exact_origin_allowlist(url),
                                     user_agent=user_agent,
                                     user_agent_domain=user_agent_domain, cookies=cookies)

    def send_with_policy(self, method: str, url: str, headers: Dict[str, str],
                         body: Optional[str], timeout_s: float, *,
                         allowed_origins, user_agent: str = "",
                         user_agent_domain: str = "", cookies=None) -> ApiResponse:
        """Send with a destination check before the first request and every redirect.

        ``user_agent`` (identity=browser) overrides the honest UA, but ONLY on a
        hop whose registrable domain equals ``user_agent_domain`` and that the
        government rule does not refuse, so a redirect off the site falls back to the honest
        UA. ``cookies`` (login=replay) is a list of cookie records; the ``Cookie``
        header is rebuilt per hop by ``session_import.cookie_header_for``, which
        matches domain, path and Secure (a host-only ``__Host-`` cookie goes only
        to its exact host, an https-only cookie never over http). The rejecting
        cookie jar is unchanged, so no ``Set-Cookie`` answer is ever stored or
        carried onward."""
        from anyuse.execution.request_policy import destination_refusal
        client = self._get_client()
        headers = {key: value for key, value in headers.items()
                   if key.lower() not in ("cookie", "set-cookie")}
        base_headers = dict(headers)
        if user_agent:
            base_headers = {k: v for k, v in base_headers.items()
                            if k.lower() != "user-agent"}
        cookies = list(cookies) if cookies else []
        current, current_method, current_body = url, method, body
        for _hop in range(11):
            refusal = destination_refusal(current, allowed_origins, resolve=True)
            if refusal:
                # after the first hop the site has answered; the message says which, since only a refusal of
                # the first hop means nothing was sent
                raise PermissionError(f"pre-send policy refused request: {refusal}" if not _hop else
                                      f"pre-send policy refused a redirect: {refusal}")
            hop_headers = dict(base_headers)
            hop_host = (urllib.parse.urlsplit(current).hostname or "").lower()
            if user_agent and self._ua_hop_ok(hop_host, user_agent_domain):
                hop_headers["User-Agent"] = user_agent
            if cookies:
                from anyuse.execution.session_import import cookie_header_for
                ch = cookie_header_for(cookies, current)
                if ch:
                    hop_headers["Cookie"] = ch
            client.cookies.clear()
            if hasattr(client, "stream"):
                with client.stream(current_method, current, headers=hop_headers,
                                   content=current_body, timeout=timeout_s) as resp:
                    length = resp.headers.get("content-length")
                    if length and int(length) > MAX_RESPONSE_BYTES:
                        raise ResourceLimitError(
                            f"response exceeds {MAX_RESPONSE_BYTES}-byte cap")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise ResourceLimitError(
                                f"response exceeds {MAX_RESPONSE_BYTES}-byte cap")
                        chunks.append(chunk)
                    encoding = getattr(resp, "encoding", None) or "utf-8"
                    response_text = b"".join(chunks).decode(encoding, errors="replace")
                    status_code = resp.status_code
                    response_headers = dict(resp.headers)
            else:  # injected test clients written against the original boundary
                resp = client.request(current_method, current, headers=hop_headers,
                                      content=current_body, timeout=timeout_s)
                response_text = resp.text
                if len(response_text.encode("utf-8", "ignore")) > MAX_RESPONSE_BYTES:
                    raise ResourceLimitError(
                        f"response exceeds {MAX_RESPONSE_BYTES}-byte cap")
                status_code, response_headers = resp.status_code, dict(resp.headers)
            client.cookies.clear()
            if status_code not in (301, 302, 303, 307, 308):
                return ApiResponse(status=status_code,
                                   headers={k: v for k, v in response_headers.items()
                                            if k.lower() != "set-cookie"},
                                   text=response_text)
            location = response_headers.get("location")
            if not location:
                return ApiResponse(status=status_code,
                                   headers={k: v for k, v in response_headers.items()
                                            if k.lower() != "set-cookie"},
                                   text=response_text)
            current = urllib.parse.urljoin(current, location)
            if status_code == 303 or (status_code in (301, 302)
                                           and current_method.upper() == "POST"):
                current_method, current_body = "GET", None
        raise RuntimeError("redirect budget exceeded (10 hops)")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def import_module_httpx_transport():
    """``httpx.HTTPTransport`` bound to IPv4 (see :class:`HttpxTransport`)."""
    import httpx
    return httpx.HTTPTransport(local_address="0.0.0.0", retries=1)


def page_secrets(template: Any, params: Optional[Dict[str, Any]]) -> List[str]:
    """The secret values ``params`` carries that a plan read from the site's page
    for ``template`` (a single template's derived ``from: page`` values whose
    source is marked secret)."""
    if not isinstance(template, RequestTemplate) or not params:
        return []
    out: List[str] = []
    for name, row in template.page_params().items():
        if (row.get("source") or {}).get("secret") and params.get(name) not in (None, ""):
            out.append(str(params[name]))
    return out


def provider_read_shape(site: str, url: str, body: Optional[str],
                        headers: Optional[Dict[str, str]], values: List[Any]) -> str:
    """``""`` when a POST is the read the site's page sends its search provider:
    a host outside the site's registrable domain, and the observed-provider rule
    (``web_scope.WebScope.observed_call``) with the request carrying the typed
    value, sent right after typing. Else why not. A POST to the site itself is
    the read-only POST rung's, under ``ANYUSE_API_READONLY_POST``."""
    from anyuse.execution.web_scope import WebScope, host_of, registrable
    if registrable(host_of(url)) == registrable(host_of(f"https://{site}/")):
        return "the request goes to the site itself, not to its search provider"
    scope = WebScope(providers=True, observed_providers=True)
    hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    hdrs.setdefault("content-type", "application/json"
                    if str(body or "").lstrip()[:1] in "{[" else
                    "application/x-www-form-urlencoded")
    verdict = scope.observed_call(site, url, "POST", "XHR", body or "", hdrs,
                                  values=[str(v) for v in values if v not in (None, "")],
                                  acting=True)
    if verdict is None:
        return "the request is not a data call that carries the typed value to the provider"
    return "" if verdict.ok else verdict.why


class ApiStepExecutor:
    """Run a :class:`RequestTemplate` and report it the way the executor expects.

    ``execute`` NEVER raises for an expected failure — a refused method, a
    network error, a non-2xx, unparseable JSON, a failed postcondition all come
    back as ``{"success": False, ...}`` so the ladder falls through to the
    recorded UI path (``deterministic.replay_transitions`` only needs
    ``result["success"]``).
    """

    def __init__(self, transport=None, compliance_checker=None,
                 readonly_post_hosts_path: Optional[str] = None,
                 apply_site_modes: bool = False, site_modes_path: Optional[str] = None,
                 password_reader=None, db_resolver=None):
        self._transport = transport
        self._compliance_checker = compliance_checker
        self.readonly_post_hosts_path = readonly_post_hosts_path
        #: Opt-in: read per-site modes (execution/site_modes.py) on THIS product
        #: executor. OFF by default, so the bench's executor (which never sets it)
        #: can never reach the accessor and always runs the safe identity.
        self.apply_site_modes = bool(apply_site_modes)
        self.site_modes_path = site_modes_path
        #: Injected in tests so no real Keychain item or browser profile is read.
        self._password_reader = password_reader
        self._db_resolver = db_resolver
        #: an ``empty_page.Context`` while ``run_compiled`` serves a product call
        #: (an empty answer may then search made-up inputs); ``None`` otherwise
        self.empty_context = None

    def _site_modes(self, host: str):
        """The single product-path accessor, consulted only when opted in."""
        if not self.apply_site_modes:
            return None
        try:
            from anyuse.execution import site_modes
            return site_modes.effective_modes(host, self.site_modes_path)
        except Exception as exc:                  # a modes read never fails a run
            logger.warning("could not read site modes for %s: %s", host, exc)
            return None

    @staticmethod
    def _auth_class(status: int, blocked_state) -> bool:
        """A login=replay response that means "the session is stale": a 401 or
        403 that is not a challenge, or a login wall. A CAPTCHA, an interstitial
        or a rate limit is never this — those stay sticky blocks."""
        from anyuse.execution.blocked import FORBIDDEN, LOGIN_WALL
        reason = getattr(blocked_state, "reason", "") or ""
        if reason == LOGIN_WALL:
            return True
        return status in (401, 403) and reason in ("", FORBIDDEN)

    @staticmethod
    def _auth_failure(base, resp, url, host, elapsed_ms, response_bytes, why) -> Dict[str, Any]:
        return {**base, "success": False, "auth_required": True, "requires_human": True,
                "outcome_kind": "terminal_refusal", "status": resp.status, "url": url,
                "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                "error": (f"the imported session for {host} is not signed in (HTTP "
                          f"{resp.status}; {why}); sign in to the site in your browser, "
                          f"then re-import it with `anyuse sites login {host}`")}

    def _import_cookies(self, modes) -> List[Dict[str, Any]]:
        """Resolve a login=replay session to in-memory cookie RECORDS (name,
        value, domain, host_only, path, secure) — for per-hop matching."""
        from anyuse.execution import session_import
        reader = self._password_reader or session_import.keychain_password
        resolver = self._db_resolver or session_import.default_cookie_db
        cookies = session_import.import_cookies(modes.session or {}, password_reader=reader,
                                                db_resolver=resolver)
        return [c.as_record() if hasattr(c, "as_record") else dict(c) for c in cookies]

    @property
    def transport(self):
        if self._transport is None:
            self._transport = HttpxTransport()
        return self._transport

    def execute(self, template, params: Optional[Dict[str, Any]] = None, *,
                _probe: bool = False) -> Dict[str, Any]:
        """Run a single :class:`RequestTemplate` or a whole :class:`RequestPlan`.

        ``_probe``: return the raw 2xx response (``text``, ``content_type``)
        instead of reading it, after every check before the send and the block
        detection after it; the empty-answer cascade reads it
        (``result_contract.page_from_raw``).

        A secret value the plan read from the site's page reads
        ``page_literals.SHOWN`` everywhere in the result."""
        result = self._execute(template, params, _probe=_probe)
        secrets = page_secrets(template, params)
        if secrets:
            from anyuse.execution.page_literals import scrub
            return scrub(result, secrets)
        return result

    def _execute(self, template, params: Optional[Dict[str, Any]] = None, *,
                 _probe: bool = False) -> Dict[str, Any]:
        if isinstance(template, RequestPlan):
            return self.execute_plan(template, params)

        base = {"method": "api", "modality": "api",
                "outcome_kind": "retryable_failure"}
        # Preserve ``DataflowValues`` provenance for derived response data.
        # Copying it to a plain dict would let a caller forge a derived value.
        bound_params = params or {}

        if not api_modality_enabled():
            return {**base, "success": False, "error": "api modality disabled (ANYUSE_API_MODALITY=off)"}

        # Per-site modes are read through the ONE accessor, and only when this
        # executor opted in (self.apply_site_modes). The default path never gets
        # here with modes set, so it is byte-for-byte unchanged.
        mode_host = urllib.parse.urlsplit(template.url).hostname or ""
        modes = self._site_modes(mode_host)
        replay_ua = ""
        ua_domain = ""
        robots_policy_override = None
        if modes is not None:
            if modes.identity == "browser":
                # identity=browser replays the CAPTURED browser User-Agent; a
                # template with none is refused (we never invent one).
                replay_ua = template.browser_ua
                if not replay_ua:
                    return {**base, "success": False, "blocked": True,
                            "outcome_kind": "terminal_refusal",
                            "error": (f"identity=browser is set for {mode_host} but this "
                                      f"workflow captured no browser User-Agent; recapture "
                                      f"it (attach/HAR) or use identity=honest")}
                from anyuse.execution.site_modes import site_key
                ua_domain = site_key(mode_host)
            if modes.robots == "ignore":
                robots_policy_override = "ignore"
        # A login=replay template carries cookie references where the values
        # were. It runs only for a site set login=replay AND route=replay;
        # otherwise the logged-in work keeps the real UI path and the api rung
        # declines (route=ui), which is the default for every site.
        if template.cookie_refs and not (
                modes and modes.login == "replay" and modes.route == "replay"):
            return {**base, "success": False, "blocked": True,
                    "outcome_kind": "terminal_refusal",
                    "error": (f"logged-in workflow (needs cookies "
                              f"{sorted(template.cookie_refs)}): enable it for {mode_host} "
                              f"with `anyuse sites set {mode_host} --login replay "
                              f"--route replay`, then `anyuse sites login {mode_host}`; "
                              f"the UI path runs it meanwhile")}

        problems = template.problems()
        if problems:
            # A non-read method is the one problem that is a *policy* refusal
            # rather than a malformed template — flag it so the caller can route
            # it to the HIL gate instead of treating it as a broken compile.
            needs_hil = (template.method or "GET").upper() not in READ_METHODS
            return {**base, "success": False, "blocked": needs_hil,
                    "requires_approval": needs_hil,
                    "outcome_kind": ("terminal_refusal" if needs_hil
                                     else "retryable_failure"),
                    "hil_action": api_hil_action(template, params) if needs_hil else None,
                    "error": "template refused: " + "; ".join(problems)}

        try:
            url, headers, body = template.render(bound_params)
        except KeyError as exc:
            return {**base, "success": False, "error": str(exc)}
        except ValueError as exc:                # PathBindError and friends: the
            return {**base, "success": False,    # value is the wrong SHAPE for
                    "error": f"parameter refused: {exc}"}   # this template

        page_vals = {name: bound_params[name] for name in template.page_params()
                     if name in bound_params}
        complete_problems = bound_request_problems(template, url, headers, body,
                                                   page_values=page_vals)
        if complete_problems:
            return {**base, "success": False, "blocked": True,
                    "requires_approval": True,
                    "outcome_kind": "terminal_refusal",
                    "hil_action": api_hil_action(template, params),
                    "error": "bound request refused: " + "; ".join(complete_problems)}
        # what the site's page stated stays out of every record, log line and block entry
        shown = url
        secret_vals = page_secrets(template, bound_params)
        if secret_vals:
            from anyuse.execution.page_literals import scrub
            shown = scrub(url, secret_vals)

        if (template.method or "GET").upper() == "POST" \
                and getattr(template, "readonly_post", False) \
                and not readonly_post_enabled(
                    url, body, getattr(self, "readonly_post_hosts_path", None)):
            why_not = self._provider_read_refusal(template, url, headers, body, bound_params)
            if why_not:
                return {**base, "success": False, "blocked": True,
                        "requires_approval": True, "outcome_kind": "terminal_refusal",
                        "hil_action": api_hil_action(template, bound_params),
                        "error": why_not}

        from anyuse.execution.request_policy import destination_refusal, exact_origin_allowlist
        allowed_origins = exact_origin_allowlist(template.url, template.approved_origins)
        destination_block = destination_refusal(url, allowed_origins)
        if destination_block:
            return {**base, "success": False, "blocked": True,
                    "outcome_kind": "terminal_refusal", "url": url,
                    "error": f"pre-send policy refused request: {destination_block}"}

        blocked = self._boundary_denial(shown)
        if blocked:
            return {**base, "success": False, "blocked": True,
                    "outcome_kind": "terminal_refusal", "error": blocked}

        # The robots policy applies to REPLAY, not only to compilation. This
        # check is local-only (it answers from the cache written when the
        # workflow was compiled) so it costs the site nothing and needs no
        # network in a test.
        # A login=replay template sent to its own origin with the user's imported
        # session is a signed-in task: started by the user (an MCP call, a CLI
        # run), it is not held to robots.txt (api_permission.ROBOTS_SIGNED_IN).
        from anyuse.execution.api_permission import replay_override, replay_refusal
        from anyuse.execution.request_policy import origin as _origin
        signed_in = bool(template.cookie_refs) and modes is not None and modes.login == "replay" \
            and bool(_origin(url)) and _origin(url) == _origin(template.url)
        signed = {"signed_in": True} if signed_in else {}
        refusal = replay_refusal(url, policy=robots_policy_override, **signed)
        if refusal:
            return {**base, "success": False, "blocked": True, "error": refusal,
                    "robots_refused": True, "outcome_kind": "terminal_refusal"}
        overridden = replay_override(url, policy=robots_policy_override, **signed)
        if overridden:
            # ANYUSE_ROBOTS: the run says what it overrode
            base = {**base, "robots_overridden": overridden}

        # A host that serves a browser and refuses a named HTTP client: do not
        # send. Not because it is dangerous — because it is pointless, and
        # because the honest alternative to "send it anyway" would be to change
        # the User-Agent, which AnyUse does not do. The browser replay rung is
        # the one that works here, and the error says so.
        from anyuse.execution.blocked import blocked_until, is_browser_only
        host = urllib.parse.urlsplit(url).netloc
        browser_only = is_browser_only(host)
        if browser_only:
            return {**base, "success": False, "browser_only": True, "url": url,
                    "browser_only_state": dict(browser_only),
                    "error": (f"{host} is recorded as browser-only: it answered "
                              f"AnyUse's named HTTP client with "
                              f"{browser_only.get('status') or browser_only.get('from_reason')}"
                              f" and served the container browser the same page. "
                              f"The compiled-request rung is not attempted here; "
                              f"the deterministic browser replay rung (cdp) is. "
                              f"AnyUse will not disguise its client to get in.")}

        # Do NOT knock on a door that already asked for a human. This is checked
        # before the request is sent, so a blocked host costs nothing per replay.
        held = blocked_until(host)
        if held:
            return {**base, "success": False, "blocked": True, "requires_human": True,
                    "outcome_kind": "terminal_refusal",
                    "blocked_state": {"blocked": True, "reason": held.get("reason"),
                                      "message": held.get("message"),
                                      "until": held.get("until"), "host": host,
                                      "action_required": "human"},
                    "error": (f"host is on do-not-auto-retry until {held.get('until')}: "
                              f"{held.get('reason')}")}

        # login=replay: import the session's cookie RECORDS into memory now, for
        # exactly this call. They are never written to disk, a log or a template.
        replay_cookies: Optional[List[Dict[str, Any]]] = None
        if template.cookie_refs and modes is not None:
            try:
                replay_cookies = self._import_cookies(modes)
            except Exception as exc:                  # noqa: BLE001
                return {**base, "success": False, "blocked": True,
                        "outcome_kind": "terminal_refusal", "url": url,
                        "error": (f"could not import the session for {host}: {exc}; "
                                  f"re-import it with `anyuse sites login {host}`")}

        from anyuse.execution.blocked import (STICKY_REASONS, blocked_hil_action,
                                              detect_blocked, mark_blocked)
        t0 = time.monotonic()
        reimported = False
        while True:
            extra: Dict[str, Any] = {}
            if replay_ua:
                extra["user_agent"] = replay_ua
                extra["user_agent_domain"] = ua_domain
            if replay_cookies:
                extra["cookies"] = replay_cookies
            try:
                if hasattr(self.transport, "send_with_policy"):
                    resp = self.transport.send_with_policy(
                        template.method.upper(), url, headers, body, template.timeout_s,
                        allowed_origins=allowed_origins, **extra)
                elif extra:
                    resp = self.transport.send(template.method.upper(), url, headers, body,
                                               template.timeout_s, **extra)
                else:
                    resp = self.transport.send(template.method.upper(), url, headers, body,
                                               template.timeout_s)
            except ResourceLimitError as exc:
                return {**base, "success": False, "blocked": True,
                        "outcome_kind": "terminal_refusal", "error": str(exc),
                        "elapsed_ms": (time.monotonic() - t0) * 1000.0, "url": url}
            except Exception as exc:                  # network, DNS, TLS, timeout
                return {**base, "success": False, "error": f"transport error: {exc}",
                        "elapsed_ms": (time.monotonic() - t0) * 1000.0, "url": url}
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            response_bytes = len((resp.text or "").encode("utf-8", "ignore"))
            if response_bytes > MAX_RESPONSE_BYTES:
                return {**base, "success": False, "blocked": True,
                        "outcome_kind": "terminal_refusal", "status": resp.status,
                        "url": url, "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "error": f"response exceeds {MAX_RESPONSE_BYTES}-byte cap"}

            # A challenge page can arrive with any status, including 200. Detect
            # it FIRST, so a bot wall (CAPTCHA, interstitial, rate limit) is
            # recorded as a block and the host is never hit a second time.
            blocked_state = detect_blocked(int(resp.status), resp.headers,
                                           resp.text or "", shown)

            # login=replay: a NON-challenge auth failure — a 401, a 403 the
            # detector filed as plain FORBIDDEN, or a login wall where data was
            # expected — is a stale session, not a site refusing us. It goes to
            # ONE re-import, then to the auth class with the `sites login` hint,
            # and never to a sticky do-not-retry block.
            if template.cookie_refs and modes is not None and self._auth_class(
                    int(resp.status), blocked_state):
                if not reimported:
                    reimported = True
                    try:
                        replay_cookies = self._import_cookies(modes)
                    except Exception as exc:          # noqa: BLE001
                        # never retry with the old cookies
                        return self._auth_failure(base, resp, url, host, elapsed_ms,
                                                  response_bytes,
                                                  f"re-import failed: {exc}")
                    continue
                return self._auth_failure(base, resp, url, host, elapsed_ms,
                                          response_bytes, "still refused after re-import")

            if blocked_state.blocked:
                try:
                    if blocked_state.reason in STICKY_REASONS:
                        mark_blocked(blocked_state)
                except OSError as exc:
                    logger.warning("could not record the blocked host: %s", exc)
                return {**base, "success": False, "blocked": True, "requires_human": True,
                        "outcome_kind": "terminal_refusal",
                        "status": resp.status, "url": url, "elapsed_ms": elapsed_ms,
                        "bytes": response_bytes,
                        "blocked_state": blocked_state.to_dict(),
                        "hil_action": blocked_hil_action(blocked_state),
                        "error": f"blocked ({blocked_state.reason}): {blocked_state.message}"}
            break

        if not (200 <= int(resp.status) < 300):
            from anyuse.execution.blocked import retry_after_seconds
            # a server error's Retry-After: how long the site asked callers to wait
            wait = retry_after_seconds(resp.headers)
            return {**base, "success": False, "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "error": f"non-2xx status {resp.status}",
                    **({"retry_after_s": wait} if wait is not None else {})}

        if _probe:
            return {**base, "success": True, "probe": True, "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "text": resp.text or "", "content_type": resp.content_type}

        if template.extract_kind == "page_text":
            # a page of the site read as text: a later step reads the values it
            # states by name (PlanStep.page_bind); nothing is extracted here
            return {**base, "success": True, "outcome_kind": "success",
                    "status": resp.status, "url": url, "elapsed_ms": elapsed_ms,
                    "bytes": response_bytes, "extracted": {}, "data": resp.text or ""}

        if template.extract_kind == "embedded_json":
            # The response is a document, but the DATA inside it is JSON: read
            # the payload the locator names, then use ordinary JSON paths.
            markup = resp.text or ""
            ok, data, reason = check_expect_embedded(
                int(resp.status), resp.content_type, markup, template.embedded,
                template.expect)
            if not ok:
                return {**base, "success": False, "status": resp.status, "url": url,
                        "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "outcome_kind": "incomplete",
                        "error": f"postcondition failed: {reason}"}
            extracted = {name: json_path(data, path)
                         for name, path in template.extract.items()}
            common = {"status": resp.status, "url": url, "elapsed_ms": elapsed_ms,
                      "bytes": response_bytes}
            verdict = _result_contract_verdict(template, bound_params, extracted, data=data,
                                               executor=self)
            done = _contract_outcome(base, verdict, extracted, common, data)
            if done is not None:
                return done
            identity_ok, identity_reason = check_output_identity(
                extracted, bound_params, template.expect, template=template)
            if not identity_ok:
                return {**base, "success": False, "status": resp.status, "url": url,
                        "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "outcome_kind": "incomplete", "extracted": extracted,
                        "error": f"postcondition identity failed: {identity_reason}",
                        **({"reason_code": "missing_required_fields"}
                           if not check_extracted_fields(template, extracted)[0] else {})}
            return {**base, "success": True, "outcome_kind": "success",
                    "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "extracted": extracted, "data": data,
                    **({"result_contract": verdict.to_dict()} if verdict else {})}

        if template.extract_kind == "detail_fields":
            # Item-page rung: use the exact field vocabulary the proof fitted,
            # including attribute-backed fields such as og:title which a text
            # selector cannot read.  Values are scalar because this is one
            # item, not a result list.
            markup = resp.text or ""
            ok, reason = check_expect_html(int(resp.status), resp.content_type,
                                           markup, template.expect)
            if not ok:
                return {**base, "success": False, "status": resp.status, "url": url,
                        "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "outcome_kind": "incomplete",
                        "error": f"postcondition failed: {reason}"}
            from anyuse.execution.discover import page_fields
            fields = page_fields(markup)
            extracted = {name: fields.get(field, "")
                         for name, field in template.extract.items()}
            missing_fields = [name for name, value in extracted.items() if not value]
            if missing_fields:
                return {**base, "success": False, "status": resp.status, "url": url,
                        "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "outcome_kind": "incomplete", "extracted": extracted,
                        "error": f"postcondition failed: missing detail field(s) {missing_fields}"}
            identity_ok, identity_reason = check_output_identity(
                extracted, bound_params, template.expect, template=template)
            if not identity_ok:
                return {**base, "success": False, "status": resp.status, "url": url,
                        "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "outcome_kind": "incomplete", "extracted": extracted,
                        "error": f"postcondition identity failed: {identity_reason}",
                        **({"reason_code": "missing_required_fields"}
                           if not check_extracted_fields(template, extracted)[0] else {})}
            return {**base, "success": True, "outcome_kind": "success",
                    "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "extracted": extracted, "data": markup}

        if template.extract_kind == "html":
            # Server-rendered rung: the document IS the data. Keep the markup as
            # `data` so a chain step can still bind from it via a selector.
            markup = resp.text or ""
            # the tree the template was proved on
            rules = template.implied_end_tags
            ok, reason = check_expect_html(int(resp.status), resp.content_type,
                                           markup, template.expect, implied_end_tags=rules)
            if not ok:
                return {**base, "success": False, "status": resp.status, "url": url,
                        "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "outcome_kind": "incomplete",
                        "error": f"postcondition failed: {reason}"}
            from anyuse.execution.html_extract import extract as html_select
            extracted = {name: html_select(markup, sel, implied_end_tags=rules)
                         for name, sel in template.extract.items()}
            common = {"status": resp.status, "url": url, "elapsed_ms": elapsed_ms,
                      "bytes": response_bytes}
            verdict = _result_contract_verdict(template, bound_params, extracted,
                                               markup=markup, executor=self, url=url)
            if verdict is not None and verdict.keep is not None \
                    and isinstance(extracted.get("items"), list):
                # one item per result record, as the proof measured it
                items = extracted["items"]
                extracted["items"] = [items[i] for i in verdict.keep if i < len(items)]
            done = _contract_outcome(base, verdict, extracted, common, markup)
            if done is not None:
                return done
            identity_ok, identity_reason = check_output_identity(
                extracted, bound_params, template.expect, template=template)
            if not identity_ok:
                return {**base, "success": False, "status": resp.status, "url": url,
                        "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                        "outcome_kind": "incomplete", "extracted": extracted,
                        "error": f"postcondition identity failed: {identity_reason}",
                        **({"reason_code": "missing_required_fields"}
                           if not check_extracted_fields(template, extracted)[0] else {})}
            return {**base, "success": True, "outcome_kind": "success",
                    "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "extracted": extracted, "data": markup,
                    **({"result_contract": verdict.to_dict()} if verdict else {})}

        try:
            data = json.loads(resp.text or "")
        except (ValueError, RecursionError) as exc:
            return {**base, "success": False, "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "error": f"response is not JSON: {exc}"}
        within_limits, limit_reason = validate_json_limits(data)
        if not within_limits:
            return {**base, "success": False, "blocked": True,
                    "outcome_kind": "terminal_refusal", "status": resp.status,
                    "url": url, "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "error": f"response parser refused input: {limit_reason}"}

        ok, reason = check_expect(int(resp.status), resp.content_type, data, template.expect)
        if not ok:
            return {**base, "success": False, "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "outcome_kind": "incomplete",
                    "error": f"postcondition failed: {reason}"}

        extracted = {name: json_path(data, path) for name, path in template.extract.items()}
        common = {"status": resp.status, "url": url, "elapsed_ms": elapsed_ms,
                  "bytes": response_bytes}
        verdict = _result_contract_verdict(template, bound_params, extracted, data=data,
                                           executor=self)
        done = _contract_outcome(base, verdict, extracted, common, data)
        if done is not None:
            return done
        identity_ok, identity_reason = check_output_identity(
            extracted, bound_params, template.expect, template=template)
        if not identity_ok:
            return {**base, "success": False, "status": resp.status, "url": url,
                    "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                    "outcome_kind": "incomplete", "extracted": extracted,
                    "error": f"postcondition identity failed: {identity_reason}",
                    **({"reason_code": "missing_required_fields"}
                       if not check_extracted_fields(template, extracted)[0] else {})}
        return {**base, "success": True, "outcome_kind": "success",
                "status": resp.status, "url": url,
                "elapsed_ms": elapsed_ms, "bytes": response_bytes,
                "extracted": extracted, "data": data,
                **({"result_contract": verdict.to_dict()} if verdict else {})}

    def execute_plan(self, plan: RequestPlan,
                     params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run a chain: each step's ``bind`` and ``page_bind`` feed the
        parameters of later steps (:meth:`_execute_plan`). A secret value a step
        read from the site's page reads ``page_literals.SHOWN`` everywhere in
        the result and its trace."""
        secrets: List[str] = []
        result = self._execute_plan(plan, params, secrets)
        if secrets:
            from anyuse.execution.page_literals import scrub
            return scrub(result, secrets)
        return result

    def _execute_plan(self, plan: RequestPlan, params: Optional[Dict[str, Any]],
                      secrets: List[str]) -> Dict[str, Any]:
        """Run a chain: each step's ``bind`` feeds the parameters of later steps.

        The chain stops at the FIRST failing step and reports which one it was,
        with the whole trace. Partial success is never reported as success: a
        plan whose second call 404s falls back to the recorded UI path exactly
        like a single failing request, because a half-done chain is not the
        outcome the recording proved.

        A bound path that resolves to a list binds its first entry ("open the
        top result"); an empty list is a failure, not an empty binding, because
        a chain built on "there is a first result" has no meaning without one.
        A binding with a row rule (``PlanStep.pick``) binds the row whose name
        is the caller's input instead; when no row has that name the chain
        stops with a ``refused`` / ``not_found`` result rather than opening
        another item. Rows that carry no name at all (or no list) are an
        ordinary failure: the response changed shape, so it counts against the
        compiled request and falls to the next rung like any other.

        A ``page_bind`` reads its value from the step's response text with its
        locator (``page_literals.read``): a page that no longer states it, or
        states two values, stops the chain at that step. ``secrets`` receives
        every secret page value, for the caller to scrub.
        """
        from anyuse.execution.outcome import classify_outcome
        base = {"method": "api", "modality": "api",
                "outcome_kind": "retryable_failure"}
        if not api_modality_enabled():
            return {**base, "success": False,
                    "error": "api modality disabled (ANYUSE_API_MODALITY=off)"}
        if not page_keys_enabled() and plan_reads_page_keys(plan):
            # checked before the first request, so nothing is sent
            return {**base, "success": False,
                    "error": "this tool reads its search provider's key from the site's page, "
                             "and ANYUSE_PAGE_KEYS is off; turn it on to use the HTTP tool, "
                             "or let the browser replay answer"}
        problems = plan.problems()
        if problems:
            needs_hil = any((s.template.method or "GET").upper() not in READ_METHODS
                            for s in plan.steps)
            return {**base, "success": False, "blocked": needs_hil,
                    "requires_approval": needs_hil,
                    "outcome_kind": ("terminal_refusal" if needs_hil
                                     else "retryable_failure"),
                    "hil_action": api_hil_action(plan, params) if needs_hil else None,
                    "error": "plan refused: " + "; ".join(problems)}

        from anyuse.execution.workflow_schema import (
            DataflowValues, resolve_values, specs_from_dicts,
        )
        plan_specs = specs_from_dicts(plan.parameter_schema)
        initial, dataflow_errors = resolve_values(
            [spec for spec in plan_specs if spec.kind != "derived"], params or {})
        if dataflow_errors:
            return {**base, "success": False, "outcome_kind": "terminal_refusal",
                    "error": "plan parameter dataflow refused: "
                             + "; ".join(dataflow_errors)}
        bound = DataflowValues(initial)
        specs_by_name = {spec.name: spec for spec in plan_specs}
        trace: List[Dict[str, Any]] = []
        total_ms = 0.0
        total_bytes = 0
        result: Dict[str, Any] = {}
        for i, step in enumerate(plan.steps):
            if total_ms / 1000.0 >= MAX_CHAIN_SECONDS:
                return {**base, "success": False, "blocked": True,
                        "outcome_kind": "terminal_refusal", "plan_trace": trace,
                        "failed_step": i + 1, "elapsed_ms": total_ms,
                        "bytes": total_bytes,
                        "error": f"chain exceeded {MAX_CHAIN_SECONDS}s time cap"}
            result = self.execute(step.template, bound.for_step(step.name))
            total_ms += float(result.get("elapsed_ms") or 0.0)
            total_bytes += int(result.get("bytes") or 0)
            trace.append({"step": i + 1, "name": step.name,
                          "url": result.get("url", step.template.url),
                          "success": bool(result.get("success")),
                          "outcome_kind": classify_outcome(result),
                          "status": result.get("status"),
                          "elapsed_ms": result.get("elapsed_ms"),
                          "error": result.get("error")})
            if total_bytes > MAX_CHAIN_BYTES:
                return {**base, "success": False, "blocked": True,
                        "outcome_kind": "terminal_refusal", "plan_trace": trace,
                        "failed_step": i + 1, "elapsed_ms": total_ms,
                        "bytes": total_bytes,
                        "error": f"chain exceeded {MAX_CHAIN_BYTES}-byte cap"}
            if not result.get("success"):
                kind = classify_outcome(result)
                # A producer with missing data cannot bind the next request.
                # This remains response-shape drift, not a partial final answer;
                # the chain still stops before dispatching its consumer.
                if i < len(plan.steps) - 1 and result.get("reason_code") == "missing_required_fields":
                    kind = "retryable_failure"
                preserved = {key: result[key] for key in (
                    "blocked", "requires_approval", "requires_human",
                    "robots_refused", "blocked_state", "hil_action", "uncertain")
                             if key in result}
                return {**base, **preserved, "success": False,
                        "outcome_kind": kind, "plan_trace": trace,
                        "failed_step": i + 1, "elapsed_ms": total_ms, "bytes": total_bytes,
                        "error": f"chain step {i + 1} ({step.name or step.template.url}) "
                                 f"failed: {result.get('error')}"}
            for name, path in step.bind.items():
                rule = step.pick.get(name)
                if rule:
                    wanted = initial.get(str(rule.get("input") or ""))
                    value, row, how = pick_row(result.get("data"), path, rule, wanted)
                    if how == PICK_NONE_NAMED:
                        shown = str(wanted or "")[:80]
                        return {**base, "success": False, "refused": True,
                                "outcome_kind": "refused", "reason_code": "not_found",
                                "plan_trace": trace, "failed_step": i + 1,
                                "elapsed_ms": total_ms, "bytes": total_bytes,
                                "error": (f"no result is named {shown!r}: the search "
                                          "answered, but this tool opens only the result "
                                          "with the name you asked for. Check the "
                                          "spelling, or ask for the exact name")}
                    if value is not None:
                        trace[-1].setdefault("picked", {})[name] = {"row": row, "rule": how}
                else:
                    value = json_path(result.get("data"), path)
                    if isinstance(value, list):
                        value = value[0] if value else None
                if value is None:
                    # a row rule with no row to read a name from: the response changed shape
                    what = (f"no result row with a {str(rule.get('name_path') or 'key')!r} "
                            "to choose from" if rule else "nothing")
                    return {**base, "success": False, "plan_trace": trace,
                            "failed_step": i + 1, "elapsed_ms": total_ms, "bytes": total_bytes,
                            "error": f"chain step {i + 1} produced {what} at {path!r} "
                                     f"for binding {name!r}"}
                spec = specs_by_name.get(name)
                if spec is None or spec.kind != "derived":
                    return {**base, "success": False, "outcome_kind": "terminal_refusal",
                            "plan_trace": trace, "failed_step": i + 1,
                            "elapsed_ms": total_ms, "bytes": total_bytes,
                            "error": f"chain output {name!r} has no derived contract"}
                ok, why = spec.validate(value)
                if not ok:
                    return {**base, "success": False, "outcome_kind": "incomplete",
                            "plan_trace": trace, "failed_step": i + 1,
                            "elapsed_ms": total_ms, "bytes": total_bytes,
                            "error": f"untrusted response binding refused: {why}"}
                bound.add_derived(name, value, spec.source)
                trace[-1].setdefault("bound", {})[name] = value
            for name, loc in step.page_bind.items():
                from anyuse.execution.page_literals import SHOWN, Locator, read
                locator = Locator.from_dict(loc)
                value, why = read(str(result.get("data") or ""), locator)
                if value is None:
                    return {**base, "success": False, "plan_trace": trace,
                            "failed_step": i + 1, "elapsed_ms": total_ms, "bytes": total_bytes,
                            "error": f"chain step {i + 1} ({step.name}) page value "
                                     f"{name!r}: {why}"}
                if locator.secret:
                    secrets.append(value)
                spec = specs_by_name.get(name)
                if spec is None or spec.kind != "derived" or spec.source.get("from") != "page":
                    return {**base, "success": False, "outcome_kind": "terminal_refusal",
                            "plan_trace": trace, "failed_step": i + 1,
                            "elapsed_ms": total_ms, "bytes": total_bytes,
                            "error": f"page value {name!r} has no derived contract"}
                ok, why = spec.validate(value)
                if not ok:
                    return {**base, "success": False, "outcome_kind": "incomplete",
                            "plan_trace": trace, "failed_step": i + 1,
                            "elapsed_ms": total_ms, "bytes": total_bytes,
                            "error": f"page value refused: {why}"}
                bound.add_derived(name, value, spec.source)
                trace[-1].setdefault("bound", {})[name] = SHOWN if locator.secret else value

        out = dict(result)
        out.update({"plan_trace": trace, "elapsed_ms": total_ms, "bytes": total_bytes,
                    "steps_run": len(plan.steps)})
        return out

    def _provider_read_refusal(self, template: RequestTemplate, url: str,
                               headers: Dict[str, str], body: Optional[str],
                               params: Dict[str, Any]) -> str:
        """``""`` when a read-only POST may run without ``ANYUSE_API_READONLY_POST``:
        a provider read (:attr:`RequestTemplate.provider_read`) that sends a
        value the plan read from the site's page, whose host is acknowledged,
        and whose bound request is still the read the site's page sends its
        search provider (``web_scope.WebScope.observed_call``). Otherwise the
        refusal."""
        default = ("read-only POST refused: set ANYUSE_API_READONLY_POST=on "
                   "and approve this exact endpoint/operation with "
                   "workflow acknowledge-host")
        site = str(getattr(template, "provider_read", "") or "")
        if not site or not template.page_params():
            return default
        host = urllib.parse.urlsplit(url).netloc
        from anyuse.execution.api_permission import host_acknowledged
        if not host_acknowledged(host, getattr(self, "readonly_post_hosts_path", None)):
            return (f"read-only POST refused: {host} is the search provider {site}'s page "
                    f"sends this read to; acknowledge it with `anyuse workflow "
                    f"acknowledge-host {host}`, or set ANYUSE_API_READONLY_POST=on and "
                    f"approve this exact endpoint/operation")
        why = provider_read_shape(site, url, body, headers,
                                  [params[n] for n in template.input_params if n in params])
        return f"read-only POST refused: {why}" if why else ""

    # -- boundary -----------------------------------------------------------
    def _boundary_denial(self, url: str) -> str:
        """Consult an injected ``core.restriction.ComplianceChecker`` (same
        contract ``ActionExecutor`` uses) for the navigation this request is.

        Returns a denial reason, or ``""`` to proceed. A broken checker denies
        (fail-closed), matching ``ActionExecutor.execute``.
        """
        checker = self._compliance_checker
        if checker is None:
            return ""
        try:
            verdict = checker.check_action({"action": "navigate", "args": {"url": url}})
            if isinstance(verdict, tuple):
                allowed, reason = (tuple(verdict) + (None,))[:2]
            else:
                allowed = getattr(verdict, "allowed", None)
                if allowed is None and isinstance(verdict, dict):
                    allowed = verdict.get("allowed", True)
                reason = getattr(verdict, "reason", None) or (
                    verdict.get("reason") if isinstance(verdict, dict) else None)
            if allowed is False:
                return f"blocked by BOUNDARY: {reason or 'policy denied'}"
        except Exception as exc:
            logger.warning("api boundary check errored for %s: %s", url, exc)
            return f"compliance check failed: {exc}"
        return ""
