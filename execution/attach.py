"""Attach recorder: observe a Chromium another agent is driving, read-only.

Any agent (browser-use, Hermes, Claude Code with a browser, a person) drives a
Chromium. AnyUse attaches over the Chrome DevTools Protocol, **observes without
actuating**, and writes one sanitised session file. Several sessions of the
same task with different inputs are then compiled and proved by the unchanged
compiler (:mod:`api_compile`, :mod:`api_verify`), exactly like a HAR or an
agent recording: "your agent did it once, now it's a proved tool".

The recorder is separate from every actuator in AnyUse
(:mod:`agent_cdp` actuates; this module cannot). Design, threat model and
limits: ``docs/ATTACH.md``. The load-bearing rules, each enforced in code here:

* **Endpoint.** Only ``http://127.0.0.1:PORT`` or ``http://[::1]:PORT``, with an
  explicit port, no credentials, no path, query or fragment
  (:func:`validate_endpoint`). ``localhost`` is refused: it is a name, and a
  name can resolve somewhere else. Loopback is *not* authentication — it only
  narrows who can be reached; see the threat model.
* **Pinning.** One browser (its ``/json/version`` identity), one page target
  for the whole session. With several page targets and no ``--target`` the
  recorder refuses instead of guessing. The WebSocket URL is **built** from the
  validated endpoint and the target id, never taken from what the browser
  reports; a reported destination that differs is refused
  (:func:`page_ws_url`). If the connection drops the session ends: no
  reconnect, no other target, no other browser, and nothing is ever launched.
* **Read-only.** :class:`CdpObserver` sends only the methods in
  :data:`READ_ONLY_METHODS`. Anything else raises :class:`ReadOnlyViolation`
  before a byte is sent — no ``Runtime.evaluate``, no ``Input.*``, no
  ``Page.navigate``, no ``Fetch.*``, no ``Emulation.*``, no ``Target.*``.
  The recorder sends no network traffic of its own: response bodies come out of
  the browser's buffer.
* **Secrets are filtered before storage** (:class:`Recorder`). Cookie values,
  ``Authorization``/API-key headers, credential-looking query and body
  parameters, hidden and password field values, JWT/bearer tokens in bodies and
  text are never kept in memory past ingestion, so they cannot be written. A
  request that carried a credential is kept only as the *fact* that it did
  (``credential_bound``), and the compiler refuses it — as for HAR import.
  A page loaded with a session-looking cookie is treated as signed in: its
  visible text and DOM are withheld.
* **Storage.** One JSON file per session, mode 0600, in a 0700 directory under
  the AnyUse data dir (``<data>/attach/``). Sessions expire after
  :data:`DEFAULT_TTL_DAYS` days (``ANYUSE_ATTACH_TTL_DAYS``);
  ``anyuse attach --forget`` deletes them.

Ground truth
------------
An attached session has no human typing "what the page showed". The ground
truth is the **captured visible text of the result page** (a DOM snapshot of
laid-out nodes only), and the result list is derived across sessions of
different inputs by the same rule the learn loop uses
(:func:`learn_loop.derive_ground_truth`). The trade-off — the recorder trusts
that the page the agent ended on is the answer — is stated in
``docs/ATTACH.md``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets as _secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser as _HTMLParser
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution.api_step import CREDENTIAL_HEADERS, signed_param_reason
from anyuse.execution.har_import import (MAX_BODY_BYTES, _query_credential,
                                         _redact_query_credentials)

SESSION_KIND = "anyuse-attach-session"
SESSION_VERSION = 1
ATTACH_DIR = "attach"
DEFAULT_TTL_DAYS = 7.0
DEFAULT_MAX_SECONDS = 600.0

#: The ONLY protocol methods the observer may send. Each one reads: it enables
#: an event stream, or reads a buffer/snapshot the browser already holds.
READ_ONLY_METHODS = frozenset({
    "Network.enable",
    "Page.enable",
    "Network.getResponseBody",
    "DOMSnapshot.captureSnapshot",
})

#: Events that end a session: the pinned target is gone or detached.
STOP_EVENTS = {
    "Inspector.detached": "the browser detached this observer",
    "Inspector.targetCrashed": "the pinned page crashed",
    "Target.targetDestroyed": "the pinned page was closed",
    "Target.detachedFromTarget": "the pinned page was detached",
}

#: Resource types whose response bodies are worth keeping for the compiler.
BODY_TYPES = {"Document", "XHR", "Fetch"}
#: the site's own script bodies read (in memory) for the values they state by name
PAGE_KEY_SOURCES = 24
BODY_MIME_HINTS = ("json", "html", "xml", "text/plain", "javascript")
MAX_ENTRIES = 2000
MAX_SNAPSHOTS = 60
MAX_KEPT_DOMS = 12
MAX_DOM_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
MAX_POST_BYTES = 8192
MAX_DISCOVERY_BYTES = 1024 * 1024

#: Cookie names that suggest a signed-in session. Consent and analytics cookies
#: (``_ga``, ``consent``…) do not match; ``PHPSESSID``, ``sid``, ``auth`` do.
SESSION_COOKIE_HINTS = ("sess", "sid", "auth", "token", "login", "jwt", "remember",
                        "user", "account", "csrf", "xsrf", "identity", "logged")

_ENDPOINT_RE = re.compile(r"^http://(127\.0\.0\.1|\[::1\]):([0-9]{1,5})/?$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")
_KV = re.compile(r"""(["']?)([A-Za-z0-9_.\-]{2,64})\1(\s*[:=]\s*)(["'])((?:\\.|(?!\4).){0,4096}?)\4""")
_WS = re.compile(r"\s+")
_HIDDEN_INPUT = re.compile(
    r"<input\b[^>]*\btype\s*=\s*[\"']?(hidden|password)\b[^>]*>", re.I)
_VALUE_ATTR = re.compile(r"""\s(value)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_META_TOKEN = re.compile(
    r"<meta\b[^>]*\bname\s*=\s*[\"']?[^\"'>]*(csrf|xsrf|token)[^>]*>", re.I)
_CONTENT_ATTR = re.compile(r"""\s(content)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
REDACTED = "<redacted>"


#: what to install for the DevTools socket client (pyproject.toml extra "cdp")
CDP_INSTALL_HINT = "pip install -e '.[cdp]' (or pip install 'anyuse[cdp]')"


def require_cdp_client() -> None:
    """Attach (and learn) talk to the browser over its DevTools socket, which needs
    ``websockets``; say how to install it instead of failing mid-session."""
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        raise AttachError("talking to a browser over its DevTools socket needs the websockets "
                          "package, which is not installed: " + CDP_INSTALL_HINT)


class AttachError(RuntimeError):
    """Attach refused or failed. The message is meant for a person."""


class ReadOnlyViolation(AttachError):
    """The observer was asked to send a method that is not read-only."""


# ---------------------------------------------------------------------------
# endpoint validation and pinning
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Endpoint:
    host: str          # "127.0.0.1" or "[::1]"
    port: int

    @property
    def netloc(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def http(self) -> str:
        return f"http://{self.netloc}"


def validate_endpoint(url: str) -> Endpoint:
    """Accept only ``http://127.0.0.1:PORT`` or ``http://[::1]:PORT``.

    No names (``localhost`` can be re-pointed), no other loopback spellings
    (``127.1``, ``2130706433``, ``127.0.0.2``), no credentials, no path, query
    or fragment, and the port must be explicit.
    """
    if not isinstance(url, str):
        raise AttachError("the CDP endpoint must be a string")
    raw = url.strip()
    if raw != url or any(ch.isspace() for ch in raw):
        raise AttachError("the CDP endpoint must not contain whitespace")
    if "@" in raw:
        raise AttachError("credentials in the CDP endpoint are refused")
    m = _ENDPOINT_RE.match(raw)
    if not m:
        raise AttachError(
            f"refused CDP endpoint {raw[:80]!r}: only http://127.0.0.1:PORT or "
            f"http://[::1]:PORT with an explicit port and nothing else are accepted")
    port = int(m.group(2))
    if not 1 <= port <= 65535:
        raise AttachError(f"port {port} is out of range")
    return Endpoint(host=m.group(1), port=port)


def validate_target_id(target_id: str) -> str:
    if not isinstance(target_id, str) or not _TARGET_RE.match(target_id):
        raise AttachError("a target id is 8-64 letters, digits or hyphens")
    return target_id


def page_ws_url(endpoint: Endpoint, target_id: str, reported: str = "") -> str:
    """The page WebSocket URL, BUILT from the validated endpoint.

    ``reported`` (what ``/json/list`` said) is only checked, never used: if the
    browser names a different host, port or path, that is refused.
    """
    validate_target_id(target_id)
    built = f"ws://{endpoint.netloc}/devtools/page/{target_id}"
    if reported and reported != built:
        raise AttachError(
            "the browser reported a WebSocket destination that is not the validated "
            "endpoint and pinned target; refusing rather than following it")
    return built


def _no_redirect_opener():
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            raise AttachError("the CDP endpoint answered with a redirect; refused")
    # ProxyHandler({}) — an HTTP(S)_PROXY in the environment must never carry a
    # loopback request somewhere else.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def http_get_json(url: str, timeout: float = 5.0) -> Any:
    """GET a discovery document from the validated endpoint. No proxy, no redirect."""
    try:
        with _no_redirect_opener().open(url, timeout=timeout) as resp:
            raw = resp.read(MAX_DISCOVERY_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AttachError(f"no browser answers at {url} ({reason}); start one with remote debugging on "
                          "that port first (docs/ATTACH.md shows the command)") from exc
    if len(raw) > MAX_DISCOVERY_BYTES:
        raise AttachError("the discovery document is larger than the cap")
    return json.loads(raw.decode("utf-8", "replace"))


@dataclass
class Pin:
    endpoint: Endpoint
    target_id: str
    ws_url: str
    browser: str = ""
    browser_id: str = ""
    protocol: str = ""
    target_origin: str = ""


def _origin(url: str) -> str:
    sp = urllib.parse.urlsplit(url or "")
    return f"{sp.scheme}://{sp.netloc}" if sp.scheme else (url or "")[:40]


def pin_target(endpoint: Endpoint, target_id: Optional[str] = None,
               http_get: Callable[[str], Any] = http_get_json) -> Pin:
    """Read the browser identity and pin exactly one page target.

    Never creates, activates or switches targets. With several pages and no
    ``target_id`` it refuses and names the candidates (id and origin only —
    titles can be private).
    """
    version = http_get(f"{endpoint.http}/json/version")
    if not isinstance(version, dict):
        raise AttachError("the endpoint did not answer /json/version like a Chromium")
    bws = str(version.get("webSocketDebuggerUrl") or "")
    browser_id = ""
    if bws:
        sp = urllib.parse.urlsplit(bws)
        if sp.netloc != endpoint.netloc:
            raise AttachError("the browser reported a debugger URL on another host or "
                              "port than the validated endpoint; refused")
        browser_id = sp.path.rsplit("/", 1)[-1][:64]
    listing = http_get(f"{endpoint.http}/json/list")
    if not isinstance(listing, list):
        raise AttachError("the endpoint did not answer /json/list with a list")
    pages = [t for t in listing if isinstance(t, dict) and t.get("type") == "page"]
    if target_id is not None:
        validate_target_id(target_id)
        match = [t for t in pages if t.get("id") == target_id]
        if not match:
            raise AttachError(f"no page target {target_id!r} in this browser; refusing "
                              f"to pick another one")
        chosen = match[0]
    else:
        if not pages:
            raise AttachError("this browser has no page target to observe")
        if len(pages) > 1:
            names = ", ".join(f"{t.get('id')} ({_origin(str(t.get('url') or ''))})"
                              for t in pages[:10])
            raise AttachError(f"{len(pages)} page targets are open; pass --target ID "
                              f"to pin one: {names}")
        chosen = pages[0]
    tid = validate_target_id(str(chosen.get("id") or ""))
    ws = page_ws_url(endpoint, tid, str(chosen.get("webSocketDebuggerUrl") or ""))
    return Pin(endpoint=endpoint, target_id=tid, ws_url=ws,
               browser=str(version.get("Browser") or "")[:80], browser_id=browser_id,
               protocol=str(version.get("Protocol-Version") or "")[:16],
               target_origin=_origin(str(chosen.get("url") or "")))


# ---------------------------------------------------------------------------
# secret filtering — applied at ingestion, before anything is kept
# ---------------------------------------------------------------------------

def _credential_key(key: str) -> bool:
    low = (key or "").lower()
    return bool(signed_param_reason(low)) or low in (
        "password", "passwd", "pwd", "pass", "token", "secret", "otp", "pin",
        "authorization", "cookie", "session", "sid")


def redact_text(text: str) -> str:
    """Remove token-shaped values from free text, JSON, JS or HTML."""
    if not text:
        return text

    def kv(m: re.Match) -> str:
        if _credential_key(m.group(2)):
            return f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}{REDACTED}{m.group(4)}"
        return m.group(0)

    out = _KV.sub(kv, text)
    out = _JWT.sub(REDACTED, out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    return out


def redact_html(markup: str) -> str:
    """Hidden/password input values and token meta tags are removed, then text redaction."""
    if not markup:
        return markup
    out = _HIDDEN_INPUT.sub(lambda m: _VALUE_ATTR.sub(" value=\"\"", m.group(0)), markup)
    out = _META_TOKEN.sub(lambda m: _CONTENT_ATTR.sub(" content=\"\"", m.group(0)), out)
    return redact_text(out)


def _is_session_cookie(name: str) -> bool:
    low = (name or "").lower()
    return (low.startswith(("__secure-", "__host-"))
            or any(h in low for h in SESSION_COOKIE_HINTS)
            or bool(signed_param_reason(low)))


def cookie_reason(names: Sequence[str]) -> str:
    """Why these cookie NAMES look like a signed-in session, or ``""``."""
    for name in names:
        if _is_session_cookie(name):
            return f"the request carried a {name!r} cookie"
    return ""


def session_cookie_names(names: Sequence[str]) -> List[str]:
    """The session-looking subset of ``names`` (deduped, order kept). Used by the
    login=replay capture to record WHICH cookies a request needs — names only,
    never values."""
    out: List[str] = []
    for name in names:
        if name and _is_session_cookie(name) and name not in out:
            out.append(str(name))
    return out


#: anti-CSRF echo headers: the page copies a token the site gave it
CSRF_ECHO_HEADERS = ("x-csrftoken", "x-csrf-token", "x-xsrf-token")


def header_reason(headers: Dict[str, Any], csrf_from_site: bool = False) -> str:
    """Why these headers look like a credential, or ``""``. ``csrf_from_site``
    (``--anonymous-browser`` only): the site set an anti-CSRF cookie in this
    session, so the page echoing it in an ``X-CSRFToken``-style header is the
    anonymous visitor's token, not a sign-in (for example a catalogue's search
    XHR that echoes its CSRF cookie)."""
    for name in (headers or {}):
        low = str(name).lower()
        if low == "cookie":
            continue            # judged by cookie NAME, see cookie_reason
        if csrf_from_site and low in CSRF_ECHO_HEADERS:
            continue
        if low in CREDENTIAL_HEADERS or signed_param_reason(low):
            return f"the request carried a {name!r} header"
    return ""


def _cookie_names(header_value: str) -> List[str]:
    names = []
    for part in str(header_value or "").split(";"):
        if "=" in part:
            names.append(part.split("=", 1)[0].strip())
    return names


def sanitize_post(post: Optional[str], after_password_field: bool) -> Tuple[Optional[str], str]:
    """(kept body, credential reason). A body with a credential-looking key is
    dropped entirely, as is any POST sent after a password field was shown."""
    if post is None:
        return None, ""
    if after_password_field:
        return None, "sent after a password field was shown"
    if len(post) > MAX_POST_BYTES:
        return None, ""
    keys: List[str] = []
    stripped = post.strip()
    if stripped[:1] in "{[":
        try:
            def walk(v):
                if isinstance(v, dict):
                    for k, x in v.items():
                        keys.append(str(k))
                        walk(x)
                elif isinstance(v, list):
                    for x in v:
                        walk(x)
            walk(json.loads(stripped))
        except ValueError:
            pass
    else:
        keys = [k for k, _ in urllib.parse.parse_qsl(stripped, keep_blank_values=True)]
    for k in keys:
        if _credential_key(k):
            return None, f"the request body carried a {k!r} field"
    return redact_text(post), ""


# ---------------------------------------------------------------------------
# DOM snapshot -> sanitised visible markup and text
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "template", "head", "iframe", "svg",
              "object", "embed", "canvas", "textarea", "select", "option"}
_VOID = {"input", "br", "hr", "img", "meta", "link", "area", "base", "col", "wbr"}
_BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
          "section", "article", "header", "footer", "nav", "ul", "ol", "table",
          "dt", "dd", "br", "pre", "main", "aside", "form", "label", "a", "button"}


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def snapshot_to_page(result: dict) -> dict:
    """Turn a ``DOMSnapshot.captureSnapshot`` result into sanitised data.

    Only the main document is used. Only laid-out text nodes are kept (text
    under ``display:none`` has no layout object), only ``class`` and ``id``
    attributes survive, and no form value of any kind is ever read:
    ``input``, ``textarea``, ``select`` and ``contenteditable`` content is
    dropped. Returns ``{url, title, dom, text, password_field}``.
    """
    strings = result.get("strings") or []
    docs = result.get("documents") or []
    if not docs:
        return {"url": "", "title": "", "dom": "", "text": "", "password_field": False}
    doc = docs[0]

    def s(i):
        return strings[i] if isinstance(i, int) and 0 <= i < len(strings) else ""

    nodes = doc.get("nodes") or {}
    parents = nodes.get("parentIndex") or []
    types = nodes.get("nodeType") or []
    names = nodes.get("nodeName") or []
    values = nodes.get("nodeValue") or []
    attrs = nodes.get("attributes") or []
    laid_out = set((doc.get("layout") or {}).get("nodeIndex") or [])
    n = len(parents)
    children: List[List[int]] = [[] for _ in range(n)]
    roots = []
    for i in range(n):
        p = parents[i] if i < len(parents) else -1
        if isinstance(p, int) and 0 <= p < n:
            children[p].append(i)
        else:
            roots.append(i)

    def attr_map(i) -> Dict[str, str]:
        flat = attrs[i] if i < len(attrs) and isinstance(attrs[i], list) else []
        return {s(flat[k]).lower(): s(flat[k + 1]) for k in range(0, len(flat) - 1, 2)}

    password = False
    title = ""
    for i in range(n):
        if i < len(types) and types[i] == 1 and \
                s(names[i] if i < len(names) else -1).lower() == "title":
            title = " ".join(s(values[c]) for c in children[i]
                             if c < len(values)).strip()[:200]
            break
    html: List[str] = []
    lines: List[str] = []
    buf: List[str] = []

    def flush():
        line = _WS.sub(" ", " ".join(buf)).strip()
        buf.clear()
        if line:
            lines.append(line)

    stack: List[Tuple[int, bool]] = [(r, False) for r in reversed(roots)]
    while stack:
        i, closing = stack.pop()
        t = types[i] if i < len(types) else 0
        name = s(names[i] if i < len(names) else -1).lower()
        if closing:
            html.append(f"</{name}>")
            if name in _BLOCK:
                flush()
            continue
        if t == 3:                                   # text
            if i in laid_out:
                txt = _WS.sub(" ", s(values[i] if i < len(values) else -1)).strip()
                if txt:
                    txt = redact_text(txt)
                    html.append(_esc(txt))
                    buf.append(txt)
            continue
        if t in (9, 11):                              # document / fragment
            for c in reversed(children[i]):
                stack.append((c, False))
            continue
        if t != 1:
            continue
        a = attr_map(i)
        if name == "input":
            if a.get("type", "").lower() == "password":
                password = True
            html.append("<input>")
            continue
        if name in _SKIP_TAGS or "contenteditable" in a:
            continue
        keep = "".join(f' {k}="{_esc(a[k][:200])}"' for k in ("id", "class") if a.get(k))
        html.append(f"<{name}{keep}>")
        if name in _VOID:
            continue
        if name in _BLOCK:
            flush()
        stack.append((i, True))
        for c in reversed(children[i]):
            stack.append((c, False))
    flush()
    dom = "".join(html)
    text = "\n".join(lines)
    if len(dom.encode("utf-8", "ignore")) > MAX_DOM_BYTES:
        dom = ""
    if len(text.encode("utf-8", "ignore")) > MAX_TEXT_BYTES:
        text = text[:MAX_TEXT_BYTES]
    url = _redact_query_credentials(s(doc.get("documentURL"))) if s(doc.get("documentURL")) else ""
    if title:
        dom = f"<html><head><title>{_esc(redact_text(title))}</title></head>{dom}</html>"
    return {"url": url, "title": redact_text(title), "dom": dom, "text": text,
            "password_field": password}


# ---------------------------------------------------------------------------
# the recorder: pure, synchronous, fed protocol events
# ---------------------------------------------------------------------------

@dataclass
class _Req:
    url: str
    method: str
    resource_type: str
    post_data: Optional[str] = None
    status: Optional[int] = None
    mime: str = ""
    body: Optional[str] = None
    bytes: int = 0
    content_type: str = ""
    credential_bound: str = ""
    body_dropped: str = ""
    main_document: bool = False
    t: float = 0.0
    #: login=replay capture only: the session cookie NAMES this request carried
    #: (never values). Compilation turns these into the template's cookie_refs.
    cookie_names: List[str] = field(default_factory=list)
    #: The request's own User-Agent (identity=browser replays it; never invented).
    request_user_agent: str = ""
    #: the credential-named fields an earlier page of the site stated by name
    #: (``page_literals``): each with its source page and locator, never the value
    page_keys: List[Dict[str, Any]] = field(default_factory=list)
    #: credential-named fields wait for the end of the session: kept with their
    #: page keys when an earlier site body stated them, else refused
    page_key_pending: bool = False
    #: the URLs of the redirects that led to this request, first first
    redirected_from: List[str] = field(default_factory=list)
    #: the request's place in the session, for "stated earlier"
    seq: int = 0

    def to_network(self) -> dict:
        out = {"url": self.url, "method": self.method, "status": self.status,
               "mime": self.mime, "body": self.body, "post_data": self.post_data,
               "bytes": self.bytes, "resource_type": self.resource_type,
               "headers": ({"content-type": self.content_type}
                           if self.content_type else {}),
               "credential_bound": self.credential_bound,
               "body_dropped": self.body_dropped}
        if self.cookie_names:
            out["cookie_names"] = list(self.cookie_names)
        if self.request_user_agent:
            out["request_user_agent"] = self.request_user_agent
        if self.page_keys:
            out["page_keys"] = [dict(k) for k in self.page_keys]
        if self.redirected_from:
            out["redirected_from"] = list(self.redirected_from)
        return out


class Recorder:
    """Accumulates one session from protocol events. Holds only sanitised data.

    ``on_event`` returns a list of follow-ups for the async loop:
    ``("body", request_id)``, ``("snapshot", delay_s)`` or ``("stop", reason)``.
    """

    def __init__(self, pin: Optional[Pin] = None, task: str = "",
                 input_param: Optional[Dict[str, str]] = None,
                 clock: Callable[[], float] = time.time,
                 anonymous_browser: bool = False, login_replay_site: str = "",
                 capture_ua_for: Optional[Callable[[str], bool]] = None):
        self.pin = pin
        #: ``--login-replay`` (opt-in; default off): the registrable domain the
        #: operator picked for site-mode login=replay. A request to THAT site
        #: whose ONLY credential is a session cookie is KEPT (body captured) and
        #: its session cookie NAMES recorded (never values). A request to any
        #: OTHER host, or one with an Authorization/API-key header or a signed
        #: query/body, is still refused — logged-in bodies are kept only for the
        #: opted-in site.
        self.login_replay_site = str(login_replay_site or "").lower()
        #: identity=browser: a predicate (host -> bool) the PRODUCT attach passes
        #: (site_modes.browser_identity_for, through the one accessor). The
        #: request User-Agent is kept only for a host it accepts; without it
        #: (every bench, and every site not set identity=browser) none is kept,
        #: so a default session and the tool compiled from it are unchanged.
        self.capture_ua_for = capture_ua_for
        #: rid -> (session cookie names, cookie reason) seen before the request
        #: object existed; applied once the request's host is known.
        self.pending_cookie_login: Dict[str, Tuple[List[str], str]] = {}
        #: ``--anonymous-browser`` (opt-in; default off): the operator states the
        #: observed browser holds no sign-in (a fresh, empty cookie jar when the
        #: session starts). A session-looking cookie the site SET in this
        #: session, in answer to a request that carried no credential and before
        #: any password field was shown, is then an anonymous visitor cookie, not
        #: a sign-in. Every other cookie is judged by name as before.
        self.anonymous_browser = bool(anonymous_browser)
        self.site_set_cookies: set = set()
        self.task = task
        self.input = dict(input_param) if input_param else None
        self.clock = clock
        self.started = clock()
        self.requests: Dict[str, _Req] = {}
        self.order: List[str] = []
        self.pending_cred: Dict[str, str] = {}
        self.navigations: List[dict] = []
        self.snapshots: List[dict] = []
        self.main_frame: str = ""
        self.last_main_doc: str = ""
        self.password_seen = False
        self.dropped_entries = 0
        self.blocked_statics = 0
        self.stop_reason = ""
        #: human_solves_live: while a person solves a challenge on the pinned
        #: page, snapshots are not stored (no challenge content is ever kept)
        self.live_solve_active = False
        self.live_solves: List[dict] = []
        #: keyed fingerprints of the values the site's own bodies state by name;
        #: the bodies' text is not kept for it (page_literals.SourceIndex)
        from anyuse.execution.page_literals import SourceIndex
        self._sources = SourceIndex()
        #: rid -> {field: fingerprint} of a request whose credential-named fields
        #: wait for the end of the session
        self._key_fp: Dict[str, Dict[str, str]] = {}
        #: requests that sent a session cookie (``requestWillBeSentExtraInfo``)
        self._cookie_sent: set = set()
        self._seq = 0

    # -- events -------------------------------------------------------------

    def on_event(self, msg: dict) -> List[Tuple[str, Any]]:
        method = msg.get("method") or ""
        p = msg.get("params") or {}
        if method in STOP_EVENTS:
            if method in ("Target.targetDestroyed", "Target.detachedFromTarget"):
                tid = p.get("targetId") or (p.get("targetInfo") or {}).get("targetId")
                if self.pin and tid and tid != self.pin.target_id:
                    return []
            return [("stop", STOP_EVENTS[method])]
        rid = p.get("requestId")
        if method == "Network.requestWillBeSent" and rid:
            return self._request(rid, p)
        if method == "Network.requestWillBeSentExtraInfo" and rid:
            headers = p.get("headers") or {}
            names = [((c or {}).get("cookie") or {}).get("name", "")
                     for c in (p.get("associatedCookies") or [])
                     if not (c or {}).get("blockedReasons")]
            for k, v in headers.items():
                if str(k).lower() == "cookie":
                    names += _cookie_names(v)
            if self.anonymous_browser:
                names = [n for n in names if n not in self.site_set_cookies]
            header_why = header_reason(headers, self._csrf_from_site())
            cookie_why = cookie_reason(names)
            if self.login_replay_site and not header_why and cookie_why:
                # login=replay: defer the decision until the request's host is
                # known (_decide_cookie_login): a cookie-only request to the
                # opted-in site is kept with its session cookie NAMES; to any
                # other host it is dropped as before.
                self._apply_cookie_login(rid, session_cookie_names(names), cookie_why)
                return []
            why = header_why or cookie_why
            if cookie_why:
                # a request that sent a session cookie never carries a page key
                self._cookie_sent.add(rid)
                self._key_fp.pop(rid, None)
                r = self.requests.get(rid)
                if r is not None:
                    r.page_key_pending = False
            if why and not (header_why and not cookie_why and self._key_pending(rid)):
                self._mark(rid, why)
            return []
        if method == "Network.responseReceivedExtraInfo" and rid and self.anonymous_browser:
            r = self.requests.get(rid)
            if not self.password_seen and not (r is not None and r.credential_bound) \
                    and rid not in self.pending_cred:
                for k, v in (p.get("headers") or {}).items():
                    if str(k).lower() == "set-cookie":
                        for line in str(v).split("\n"):
                            name = line.split("=", 1)[0].strip()
                            if name and "=" in line:
                                self.site_set_cookies.add(name)
            return []
        if method == "Network.responseReceived" and rid in self.requests:
            r = self.requests[rid]
            resp = p.get("response") or {}
            try:
                r.status = int(resp.get("status") or 0) or None
            except (TypeError, ValueError):
                r.status = None
            r.mime = str(resp.get("mimeType") or "")[:100]
            r.resource_type = str(p.get("type") or r.resource_type)
            for k, v in (resp.get("headers") or {}).items():
                if str(k).lower() == "content-type":
                    r.content_type = str(v)[:200]
            return []
        if method == "Network.loadingFinished" and rid in self.requests:
            r = self.requests[rid]
            try:
                r.bytes = int(p.get("encodedDataLength") or 0)
            except (TypeError, ValueError):
                r.bytes = 0
            out: List[Tuple[str, Any]] = []
            if self._wants_body(r):
                out.append(("body", rid))
            if r.resource_type in BODY_TYPES:
                out.append(("snapshot", 0.8))
            return out
        if method == "Network.loadingFailed" and rid in self.requests:
            r = self.requests[rid]
            if r.resource_type not in BODY_TYPES and (
                    p.get("blockedReason") or "BLOCKED_BY_CLIENT" in str(p.get("errorText"))):
                # a static the browser never sent (a guard or extension failed it):
                # not traffic, and a page that retries one in a loop must not fill
                # the log (a page retrying a refused logo thousands of times
                # pushed the search DOCUMENT out of the 2000-entry log)
                del self.requests[rid]
                if rid in self.order:
                    self.order.remove(rid)
                self.blocked_statics += 1
                return []
            r.body_dropped = "loading failed"
            return []
        if method == "Page.frameNavigated":
            frame = p.get("frame") or {}
            if frame.get("parentId"):
                return []
            self.main_frame = str(frame.get("id") or self.main_frame)
            url = _redact_query_credentials(str(frame.get("url") or ""))
            self.navigations.append({"t": round(self.clock() - self.started, 3),
                                     "url": url})
            return [("snapshot", 1.5)]
        if method in ("Page.loadEventFired", "Page.domContentEventFired"):
            return [("snapshot", 0.5)]
        return []

    def _key_pending(self, rid: str) -> bool:
        """A request whose credential-named fields wait for the end of the session
        (a header named like a credential then decides nothing yet)."""
        r = self.requests.get(rid)
        return rid in self._key_fp and (r is None or r.page_key_pending)

    def _csrf_from_site(self) -> bool:
        return self.anonymous_browser and not self.password_seen and any(
            "csrf" in n.lower() or "xsrf" in n.lower() for n in self.site_set_cookies)

    def _mark(self, rid: str, why: str) -> None:
        r = self.requests.get(rid)
        if r is None:
            self.pending_cred[rid] = why
            return
        if not r.credential_bound:
            r.credential_bound = why
        r.body = None
        r.post_data = None

    def _apply_cookie_login(self, rid: str, names: List[str], why: str) -> None:
        """login=replay: decide now if the request exists, else stash by rid."""
        r = self.requests.get(rid)
        if r is None:
            self.pending_cookie_login[rid] = (names, why)
            return
        self._decide_cookie_login(r, names, why)

    def _decide_cookie_login(self, r: "_Req", names: List[str], why: str) -> None:
        """Keep a cookie-only request's NAMES when it is the opted-in site; drop
        it (mark credential-bound) for any other host."""
        from anyuse.execution.site_modes import site_key
        host = urllib.parse.urlsplit(r.url).netloc.split(":", 1)[0].lower()
        if site_key(host) == self.login_replay_site and not r.credential_bound:
            for name in names:
                if name and name not in r.cookie_names:
                    r.cookie_names.append(name)
        elif not r.credential_bound:
            r.credential_bound = why
            r.body = None
            r.post_data = None

    def _request(self, rid: str, p: dict) -> List[Tuple[str, Any]]:
        req = p.get("request") or {}
        raw_url = str(req.get("url") or "")
        scheme = urllib.parse.urlsplit(raw_url).scheme.lower()
        if scheme not in ("http", "https"):
            return []
        hops: List[str] = []
        if rid in self.requests:
            # a redirect re-uses the id: keep the final hop, as DevTools does,
            # with the hops it came through
            prev = self.requests[rid]
            if p.get("redirectResponse"):
                hops = (list(prev.redirected_from) + [prev.url])[-10:]
            self.order.remove(rid)
        elif len(self.order) >= MAX_ENTRIES:
            # full: a document or data request still gets in, in place of the
            # oldest static that never got an answer
            victim = next((k for k in self.order
                           if self.requests[k].resource_type not in BODY_TYPES
                           and self.requests[k].status is None), None) \
                if str(p.get("type") or "") in BODY_TYPES else None
            if victim is None:
                self.dropped_entries += 1
                return []
            self.order.remove(victim)
            del self.requests[victim]
            self.dropped_entries += 1
        raw_post = req.get("postData") if isinstance(req.get("postData"), str) else None
        post, post_why = sanitize_post(raw_post, self.password_seen)
        pending_why = self.pending_cred.pop(rid, "")
        why = (pending_why or header_reason(req.get("headers") or {}, self._csrf_from_site())
               or _query_credential(raw_url) or post_why)
        headers = {str(k).lower(): v for k, v in (req.get("headers") or {}).items()}
        rtype = str(p.get("type") or "")
        main = rtype == "Document" and (not self.main_frame
                                        or p.get("frameId") in (None, self.main_frame))
        keyed = self._page_key_fields(rid, raw_url, req.get("headers") or {}, raw_post, why,
                                      pending_why)
        if keyed is not None:
            from anyuse.execution.page_literals import redact_fields
            post = redact_text(redact_fields(raw_url, {}, raw_post, keyed)[2]) \
                if raw_post and len(raw_post) <= MAX_POST_BYTES else None
        self._seq += 1
        r = _Req(url=_redact_query_credentials(raw_url),
                 method=str(req.get("method") or "GET").upper()[:10],
                 resource_type=rtype, post_data=post if (keyed is not None or not why) else None,
                 content_type=str(headers.get("content-type") or "")[:200],
                 credential_bound=why, main_document=main,
                 request_user_agent=self._captured_ua(raw_url, headers),
                 t=round(self.clock() - self.started, 3),
                 page_key_pending=keyed is not None, redirected_from=hops, seq=self._seq)
        self.requests[rid] = r
        self.order.append(rid)
        pending = self.pending_cookie_login.pop(rid, None)
        if pending is not None:
            self._decide_cookie_login(r, *pending)
        if main:
            self.last_main_doc = rid
        return []

    def _page_key_fields(self, rid: str, url: str, headers: Dict[str, Any],
                         post: Optional[str], why: str, pending_why: str
                         ) -> Optional[List[str]]:
        """The credential-named fields of a request that may carry page keys
        (their names alone made it credential-bound: never a cookie, an
        ``Authorization`` value or a body sent after a password field), with
        their values' fingerprints held until the end of the session; else None."""
        from anyuse.execution.page_literals import credential_fields
        self._key_fp.pop(rid, None)
        if not why or self.password_seen or rid in self.pending_cookie_login \
                or rid in self._cookie_sent:
            return None
        if pending_why and not header_reason({k: "" for k in headers}):
            return None
        fields, never = credential_fields(url, headers, post)
        if never or not fields or any(f.split(":", 1)[1] == "authorization" for f in fields):
            return None
        self._key_fp[rid] = {at: self._sources.fingerprint(v) for at, v in fields.items()}
        return sorted(fields)

    def _site_body(self, r: "_Req") -> bool:
        """A body of the site the session is on (its registrable domain)."""
        from anyuse.execution.web_scope import host_of, registrable
        doc = self.requests.get(self.last_main_doc)
        base = doc.url if doc is not None else (self.pin.target_origin if self.pin else "")
        return bool(base) and registrable(host_of(r.url)) == registrable(host_of(base))

    def _captured_ua(self, url: str, headers: Dict[str, Any]) -> str:
        if self.capture_ua_for is None:
            return ""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        try:
            wanted = bool(self.capture_ua_for(host))
        except Exception:                             # noqa: BLE001
            wanted = False
        return str(headers.get("user-agent") or "")[:300] if wanted else ""

    def _wants_body(self, r: _Req) -> bool:
        if r.resource_type == "Script" and not r.credential_bound and r.status \
                and 200 <= r.status < 300 and self._sources.sources < PAGE_KEY_SOURCES \
                and self._site_body(r):
            # the site's own script may state a provider key: indexed, never stored
            return True
        if (r.credential_bound and not r.page_key_pending) or r.resource_type not in BODY_TYPES:
            return False
        if r.cookie_names:
            # login=replay: keep ONLY what the compile needs — a signed-in JSON
            # data response. A signed-in document (the logged-in page) is never
            # stored; its text and DOM are withheld too (on_snapshot).
            mime = (r.mime or r.content_type).lower()
            if r.resource_type == "Document" or "json" not in mime:
                r.body_dropped = "signed-in response not needed by the compile — not stored"
                return False
        if not r.status or not 200 <= r.status < 300:
            return False
        mime = (r.mime or r.content_type).lower()
        return any(h in mime for h in BODY_MIME_HINTS)

    def on_body(self, rid: str, result: dict) -> None:
        r = self.requests.get(rid)
        if r is None or (r.credential_bound and not r.page_key_pending):
            return
        if result.get("base64Encoded"):
            r.body_dropped = "binary body"
            return
        body = result.get("body")
        if not isinstance(body, str):
            r.body_dropped = "no body in the browser buffer"
            return
        if len(body.encode("utf-8", "ignore")) > MAX_BODY_BYTES:
            r.body_dropped = f"body over the {MAX_BODY_BYTES}-byte cap — dropped, not cut"
            return
        if not r.credential_bound and self._site_body(r):
            # the values it states by name, as keyed fingerprints (page_literals)
            self._sources.add(r.seq, r.url, body)
        if r.resource_type == "Script":
            return                                    # indexed only, never stored
        r.body = redact_html(body) if "html" in (r.mime or "").lower() else redact_text(body)

    def _resolve_page_keys(self) -> None:
        """Each waiting request: kept with its page keys when an earlier body of the
        site stated every credential-named field by name, else refused as before.
        The stated values then leave every stored body, URL and snapshot."""
        from anyuse.execution.page_literals import provenance
        found: set = set()
        for rid in list(self.order):
            r = self.requests.get(rid)
            fps = self._key_fp.get(rid)
            if r is None or not r.page_key_pending or not fps:
                continue
            keys, _why = provenance({}, self._sources, r.seq, fingerprints=fps)
            r.page_key_pending = False
            if keys is None:
                r.body = None
                r.post_data = None
                continue
            r.page_keys = keys
            r.credential_bound = ""
            found.update(fps.values())
        self._key_fp.clear()
        if not found:
            return
        for rid in self.order:
            r = self.requests[rid]
            if r.body:
                r.body = self._scrub_fps(r.body, found)
            if r.post_data:
                r.post_data = self._scrub_fps(r.post_data, found)
            # another request may carry the key under a name that is not credential-like
            r.url = self._scrub_url_fps(r.url, found)
            r.redirected_from = [self._scrub_url_fps(u, found) for u in r.redirected_from]
        for snap in self.snapshots:
            for k in ("text", "dom"):
                if snap.get(k):
                    snap[k] = self._scrub_fps(snap[k], found)

    def _scrub_url_fps(self, url: str, fps: set) -> str:
        """``url`` with every query value or path segment whose fingerprint is in ``fps`` replaced."""
        from anyuse.execution.page_literals import MIN_SECRET_LEN, SHOWN
        values = {t for t in re.findall(r"[A-Za-z0-9_.~-]{%d,512}" % MIN_SECRET_LEN, url or "")
                  if self._sources.fingerprint(t) in fps}
        for v in sorted(values, key=len, reverse=True):
            url = url.replace(v, SHOWN)
        return url

    def _scrub_fps(self, text: str, fps: set) -> str:
        """``text`` with every stated value whose fingerprint is in ``fps`` replaced."""
        from anyuse.execution.page_literals import SHOWN, statements
        values = {v for _n, _s, v, _r, _a in statements(text, 6)
                  if self._sources.fingerprint(v) in fps}
        for v in sorted(values, key=len, reverse=True):
            text = text.replace(v, SHOWN)
        return text

    def on_body_error(self, rid: str, why: str) -> None:
        r = self.requests.get(rid)
        if r is not None:
            r.body_dropped = why[:120]

    def on_snapshot(self, result: dict) -> None:
        page = snapshot_to_page(result)
        if page["password_field"]:
            self.password_seen = True
        doc = self.requests.get(self.last_main_doc)
        signed_in = bool(doc and (doc.credential_bound or doc.cookie_names))
        snap = {"t": round(self.clock() - self.started, 3), "url": page["url"],
                "title": page["title"], "password_field": page["password_field"]}
        if self.live_solve_active:
            snap.update(text="", dom="", withheld="a human check was being solved; "
                                                  "challenge pages are not stored")
        elif signed_in:
            snap.update(text="", dom="", withheld=(
                "the page was loaded with a credential "
                f"({doc.credential_bound or 'session cookies ' + ', '.join(doc.cookie_names)})"
                "; signed-in pages are not stored"))
        else:
            snap.update(text=page["text"], dom=page["dom"])
        if self.snapshots and self.snapshots[-1]["url"] == snap["url"] and \
                self.snapshots[-1].get("text") == snap.get("text"):
            self.snapshots[-1]["t"] = snap["t"]      # nothing changed on screen
            return
        self.snapshots.append(snap)
        if len(self.snapshots) > MAX_SNAPSHOTS:
            del self.snapshots[1]                     # keep the first (landing) page
        doms = [x for x in self.snapshots if x.get("dom")]
        for old in doms[:-MAX_KEPT_DOMS]:
            old["dom"] = ""

    # -- output -------------------------------------------------------------

    def to_session(self, session_id: str) -> dict:
        pin = self.pin
        self._resolve_page_keys()
        return {
            "kind": SESSION_KIND, "version": SESSION_VERSION, "id": session_id,
            "task": self.task, "input": self.input,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started)),
            "started": self.started, "ended": self.clock(),
            "stop_reason": self.stop_reason,
            "endpoint": pin.endpoint.netloc if pin else "",
            "browser": {"product": pin.browser, "id": pin.browser_id,
                        "protocol": pin.protocol} if pin else {},
            "target": {"id": pin.target_id, "origin_at_attach": pin.target_origin}
                      if pin else {},
            "navigations": self.navigations,
            "snapshots": self.snapshots,
            "network": [self.requests[rid].to_network() for rid in self.order],
            "dropped_entries": self.dropped_entries,
            "blocked_statics": self.blocked_statics,
            "password_field_seen": self.password_seen,
            "live_solves": list(self.live_solves),
            **({"login_replay_site": self.login_replay_site}
               if self.login_replay_site else {}),
        }

    def challenge_observation(self) -> Dict[str, Any]:
        """The latest snapshot as the live-solve detector reads it."""
        last = self.snapshots[-1] if self.snapshots else {}
        return {"url": last.get("url", ""), "text": last.get("text", ""),
                "markup": (last.get("dom") or "")[:200000],
                "requests": [{"url": self.requests[r].url} for r in self.order[-400:]
                             if r in self.requests]}

    def withhold_last(self, why: str) -> None:
        if self.snapshots:
            self.snapshots[-1].update(text="", dom="", withheld=why)


# ---------------------------------------------------------------------------
# the async observer: the only thing that speaks the protocol
# ---------------------------------------------------------------------------

class CdpObserver:
    """A CDP client that CAN ONLY send :data:`READ_ONLY_METHODS`.

    ``ws`` is anything with ``async send(str)``, ``async recv() -> str`` and
    ``async close()`` — a ``websockets`` connection or a test fake.
    """

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self.events: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
        self.sent: List[str] = []
        self._reader: Optional[asyncio.Task] = None
        self.closed = False

    def start(self) -> None:
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            while True:
                raw = await self.ws.recv()
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif msg.get("method"):
                    await self.events.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self.closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AttachError("the connection to the pinned page closed"))
            self._pending.clear()
            self.events.put_nowait(None)

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: float = 15.0) -> dict:
        if method not in READ_ONLY_METHODS:
            raise ReadOnlyViolation(f"{method} is not a read-only method; the attach "
                                    f"recorder never sends it")
        if self.closed:
            raise AttachError("the connection to the pinned page closed")
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        self.sent.append(method)
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        msg = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in msg:
            raise AttachError(f"{method}: {str(msg['error'])[:160]}")
        return msg.get("result") or {}

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.ws.close()
        except Exception:
            pass


async def observe(ws, recorder: Recorder, stop: Optional[asyncio.Event] = None,
                  max_seconds: float = DEFAULT_MAX_SECONDS,
                  on_ready: Optional[Callable[[], None]] = None,
                  delay_scale: float = 1.0, live_solver=None,
                  origin: Optional[str] = None, task_id: str = "") -> Recorder:
    """Observe one pinned page until stopped, closed, detached or timed out.

    ``delay_scale`` shortens the settle delays before a snapshot (tests only).

    ``live_solver`` (``human_solves_live``, allowed origins only, never
    bench): a challenge on the pinned page asks the person to solve it in
    their own browser; this observer keeps watching (read-only, nothing is
    relayed) and stores no snapshot while the check is up.
    """
    stop = stop or asyncio.Event()
    obs = CdpObserver(ws)
    obs.start()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    snapshot_due: Optional[float] = None
    solve: Dict[str, Any] = {"fut": None, "bridge": None}

    def maybe_solve() -> None:
        if live_solver is None or solve["fut"] is not None or recorder.live_solves:
            return
        from anyuse.execution import live_solve as ls
        now_obs = recorder.challenge_observation()
        if not ls.challenge_evidence(now_obs) or not ls.solver_applies(live_solver, origin):
            return
        recorder.withhold_last("a human check was shown; challenge pages are not stored")
        recorder.live_solve_active = True

        async def observe_page() -> Dict[str, Any]:
            try:
                res = await obs.call("DOMSnapshot.captureSnapshot",
                                     {"computedStyles": [], "includeDOMRects": False},
                                     timeout=20.0)
            except (AttachError, asyncio.TimeoutError):
                return {}
            page = snapshot_to_page(res)
            return {"url": page["url"], "text": page["text"],
                    "markup": (page["dom"] or "")[:200000],
                    "requests": recorder.challenge_observation()["requests"]}

        async def read_only(method, params):
            raise ls.RelayRefused("attach is read-only: nothing is relayed")
        host = (urllib.parse.urlsplit(now_obs.get("url") or "").hostname or "").lower()
        target = recorder.pin.target_id if recorder.pin else "attached-page"
        req = ls.SolveRequest(task_id=task_id or recorder.task or host, host=host,
                              origin=origin, run_id=task_id, step="attach")
        solve["bridge"] = ls.AsyncPageBridge(loop, target, observe_page, read_only,
                                             mode=ls.MODE_ATTACH)
        solve["fut"] = loop.run_in_executor(None, live_solver.solve, req, solve["bridge"])

    def collect_solve() -> bool:
        fut = solve["fut"]
        if fut is None or not fut.done() or not recorder.live_solve_active:
            return False
        try:
            res = fut.result()
        except Exception as exc:                 # noqa: BLE001
            res = {"status": "error", "receipt": {"error": type(exc).__name__}}
        recorder.live_solves.append({"status": res.get("status"),
                                     "receipt": res.get("receipt")})
        recorder.live_solve_active = False
        return True
    try:
        await obs.call("Network.enable", {"maxTotalBufferSize": 64_000_000,
                                          "maxResourceBufferSize": 16_000_000})
        await obs.call("Page.enable")
        snapshot_due = loop.time()          # what the page shows at attach time
        if on_ready is not None:
            on_ready()
        while True:
            if stop.is_set():
                recorder.stop_reason = recorder.stop_reason or "stopped by the user"
                break
            now = loop.time()
            if now >= deadline:
                recorder.stop_reason = f"--max-seconds {max_seconds:g} reached"
                break
            if collect_solve():
                snapshot_due = now                   # what the page shows after the check
            if snapshot_due is not None and now >= snapshot_due:
                snapshot_due = None
                await _snapshot(obs, recorder)
                maybe_solve()
                continue
            wait = min(deadline - now, 0.25)
            if snapshot_due is not None:
                wait = min(wait, max(0.0, snapshot_due - now))
            try:
                msg = await asyncio.wait_for(obs.events.get(), timeout=wait)
            except asyncio.TimeoutError:
                continue
            if msg is None:
                recorder.stop_reason = "the connection to the pinned page closed"
                break
            for action, arg in recorder.on_event(msg):
                if action == "stop":
                    recorder.stop_reason = arg
                elif action == "body":
                    try:
                        recorder.on_body(arg, await obs.call(
                            "Network.getResponseBody", {"requestId": arg}))
                    except (AttachError, asyncio.TimeoutError) as exc:
                        recorder.on_body_error(arg, str(exc) or "timed out")
                elif action == "snapshot":
                    due = loop.time() + float(arg) * delay_scale
                    snapshot_due = due if snapshot_due is None else max(snapshot_due, due)
            if recorder.stop_reason:
                break
        if not obs.closed and recorder.stop_reason and not recorder.stop_reason.startswith(
                ("the pinned", "the browser")):
            await _snapshot(obs, recorder)           # the final screen
    finally:
        if solve["bridge"] is not None:
            solve["bridge"].closed = True            # a running solve ends now
        await obs.close()
        if solve["fut"] is not None:
            try:
                await asyncio.wait_for(asyncio.shield(solve["fut"]), timeout=40)
            except Exception:                        # noqa: BLE001
                pass
            collect_solve()
    return recorder


async def _snapshot(obs: CdpObserver, recorder: Recorder) -> None:
    try:
        res = await obs.call("DOMSnapshot.captureSnapshot",
                             {"computedStyles": [], "includeDOMRects": False},
                             timeout=20.0)
    except (AttachError, asyncio.TimeoutError):
        return
    recorder.on_snapshot(res)


# ---------------------------------------------------------------------------
# storage: 0600 files in a 0700 directory, expiry, forget
# ---------------------------------------------------------------------------

_SESSION_FILE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{8}\.json$")


def sessions_dir(data_dir: Optional[str] = None) -> str:
    if data_dir is None:
        from anyuse.paths import anyuse_data_dir
        data_dir = str(anyuse_data_dir())
    return os.path.join(data_dir, ATTACH_DIR)


def new_session_id(clock: Callable[[], float] = time.time) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(clock())) + "-" + _secrets.token_hex(4)


def write_session(session: dict, data_dir: Optional[str] = None) -> str:
    """Write atomically with mode 0600 in a 0700 directory. Returns the path."""
    d = sessions_dir(data_dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    name = f"{session['id']}.json"
    if not _SESSION_FILE.match(name):
        raise AttachError("invalid session id")
    path = os.path.join(d, name)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(session, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _ttl_days() -> float:
    from anyuse.settings import get as get_setting
    return float(get_setting("ANYUSE_ATTACH_TTL_DAYS"))


def purge_expired(data_dir: Optional[str] = None, max_age_days: Optional[float] = None,
                  now: Optional[float] = None) -> int:
    days = _ttl_days() if max_age_days is None else max_age_days
    d = sessions_dir(data_dir)
    if not os.path.isdir(d):
        return 0
    cutoff = (now if now is not None else time.time()) - max(0.0, days) * 86400.0
    removed = 0
    for name in os.listdir(d):
        if not _SESSION_FILE.match(name):
            continue
        path = os.path.join(d, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def forget(session_id: str = "", data_dir: Optional[str] = None) -> int:
    """Delete one session (by id) or every attach session. Returns how many."""
    d = sessions_dir(data_dir)
    if not os.path.isdir(d):
        return 0
    removed = 0
    for name in os.listdir(d):
        if not (_SESSION_FILE.match(name) or name.endswith(".json.tmp")):
            continue
        if session_id and name != f"{session_id}.json":
            continue
        try:
            os.unlink(os.path.join(d, name))
            removed += 1
        except OSError:
            continue
    return removed


def list_sessions(data_dir: Optional[str] = None) -> List[dict]:
    """Every stored session, oldest first."""
    purge_expired(data_dir)
    d = sessions_dir(data_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not _SESSION_FILE.match(name):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("kind") == SESSION_KIND:
            data["_path"] = path
            out.append(data)
    out.sort(key=lambda x: (x.get("started") or 0, x.get("id") or ""))
    return out


# ---------------------------------------------------------------------------
# the CLI-facing attach run
# ---------------------------------------------------------------------------

def parse_input(raw: str) -> Optional[Dict[str, str]]:
    if not raw:
        return None
    if "=" in raw:
        name, value = raw.split("=", 1)
        name = name.strip() or "q"
    else:
        name, value = "q", raw
    if signed_param_reason(name) or _credential_key(name):
        raise AttachError(f"--input {name!r} looks like a credential; refused")
    return {"name": name, "value": value}


def attach(cdp: str, target_id: Optional[str] = None, task: str = "",
           input_value: str = "", max_seconds: float = DEFAULT_MAX_SECONDS,
           data_dir: Optional[str] = None, echo: Callable[[str], None] = print,
           http_get: Callable[[str], Any] = http_get_json, connect=None,
           anonymous_browser: bool = False, live_solver=None,
           origin: Optional[str] = None, login_replay_site: str = "",
           capture_ua_for: Optional[Callable[[str], bool]] = None) -> Tuple[str, dict]:
    """Validate, pin, observe until stopped, write the session. Returns (path, summary)."""
    endpoint = validate_endpoint(cdp)
    param = parse_input(input_value)
    if connect is None:
        require_cdp_client()
    pin = pin_target(endpoint, target_id, http_get=http_get)
    purge_expired(data_dir)
    recorder = Recorder(pin=pin, task=task, input_param=param,
                        anonymous_browser=anonymous_browser,
                        login_replay_site=login_replay_site,
                        capture_ua_for=capture_ua_for)

    async def run():
        if connect is None:
            import websockets
            ws = await websockets.connect(pin.ws_url, max_size=64 * 1024 * 1024,
                                          proxy=None, open_timeout=10,
                                          ping_interval=None)
        else:
            ws = await connect(pin.ws_url)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        echo(f"attached read-only: {pin.browser or 'Chromium'} at {endpoint.netloc}, "
             f"pinned target {pin.target_id}. Stop with Ctrl-C.")
        await observe(ws, recorder, stop=stop, max_seconds=max_seconds,
                      on_ready=lambda: echo("ATTACHED"), live_solver=live_solver,
                      origin=origin)

    asyncio.run(run())
    session_id = new_session_id(recorder.clock)
    session = recorder.to_session(session_id)
    path = write_session(session, data_dir)
    summary = {"id": session_id, "path": path, "stop_reason": recorder.stop_reason,
               "requests": len(session["network"]),
               "credential_bound": sum(1 for r in session["network"] if r["credential_bound"]),
               "snapshots": len(session["snapshots"]),
               "navigations": len(session["navigations"])}
    return path, summary


# ---------------------------------------------------------------------------
# sessions -> recordings -> compile and prove
# ---------------------------------------------------------------------------

def _value_in(value: str, url: str, text: str = "") -> bool:
    if not value:
        return False
    v = value.lower()
    u = urllib.parse.unquote_plus(url or "").lower()
    return v in u or v in (text or "").lower()


def result_snapshot(session: dict, value: str = "") -> Optional[dict]:
    """The page that answered: the latest snapshot showing the input, else the last."""
    snaps = [s for s in session.get("snapshots") or [] if s.get("dom") or s.get("text")]
    if not snaps:
        return None
    if value:
        hits = [s for s in snaps if _value_in(value, s.get("url", ""))] or \
               [s for s in snaps if _value_in(value, "", s.get("text", ""))]
        if hits:
            return hits[-1]
    return snaps[-1]


def infer_param(sessions: Sequence[dict]) -> Tuple[str, List[str], str]:
    """(name, values, how) for sessions recorded without ``--input``.

    The one query parameter of the final page URL whose value differs across
    every session. Anything else is ambiguous and refused.
    """
    given = [s.get("input") for s in sessions]
    if all(isinstance(g, dict) and g.get("value") for g in given):
        names = {g["name"] for g in given}
        if len(names) == 1:
            return names.pop(), [g["value"] for g in given], "given with --input"
    queries = []
    for s in sessions:
        snap = result_snapshot(s)
        url = (snap or {}).get("url") or ""
        queries.append(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                                   keep_blank_values=True)))
    keys = set(queries[0]) if queries else set()
    for q in queries[1:]:
        keys &= set(q)
    varying = [k for k in sorted(keys)
               if len({q[k] for q in queries}) == len(queries) and all(q[k] for q in queries)
               and not signed_param_reason(k)]
    if len(varying) != 1 and not varying:
        path = _infer_path_param(sessions)
        if path is not None:
            return path
    if len(varying) != 1:
        raise AttachError(
            "cannot tell which input the sessions differ in "
            f"({'none' if not varying else ', '.join(varying)} of the result URLs' "
            "query parameters vary). Re-record with `anyuse attach --input NAME=VALUE`.")
    k = varying[0]
    return k, [q[k] for q in queries], "inferred from the result page URL"


def _infer_path_param(sessions: Sequence[dict]) -> Optional[Tuple[str, List[str], str]]:
    """Item pages (``/package/vim``): the ONE path segment that differs in every
    session, with the same number of segments everywhere. Else None."""
    paths = []
    for s in sessions:
        snap = result_snapshot(s)
        paths.append([seg for seg in urllib.parse.urlsplit((snap or {}).get("url") or "")
                      .path.split("/") if seg])
    if len(paths) < 2 or len({len(p) for p in paths}) != 1 or not paths[0]:
        return None
    idx = [i for i in range(len(paths[0]))
           if len({urllib.parse.unquote(p[i]).lower() for p in paths}) == len(paths)]
    same = [i for i in range(len(paths[0])) if len({p[i] for p in paths}) == 1]
    if len(idx) != 1 or len(idx) + len(same) != len(paths[0]):
        return None
    i = idx[0]
    name = re.sub(r"[^a-z0-9_]+", "_", paths[0][i - 1].lower()).strip("_") if i else ""
    return (name or "key"), [urllib.parse.unquote(p[i]) for p in paths], \
        "inferred from the result page path"


def before_snapshot(session: dict, value: str = "") -> Optional[dict]:
    """The page as it was BEFORE the value was entered: the last stored web page
    earlier than the result snapshot whose URL does not carry the value and
    whose DOM differs from the result page. ``None`` when there is none."""
    result = result_snapshot(session, value)
    snaps = [s for s in session.get("snapshots") or [] if s.get("dom")
             and str(s.get("url") or "").startswith(("http://", "https://"))]
    earlier = []
    for s in snaps:
        if s is result:
            break
        earlier.append(s)
    # the URL only: the start page often SHOWS the value (it was drawn from it)
    earlier = [s for s in earlier if not _value_in(value, s.get("url", ""))
               and s.get("dom") != (result or {}).get("dom")]
    return earlier[-1] if earlier else None


def session_to_recording(session: dict, param_name: str, value: str, tag: str,
                         cls: str = "list") -> dict:
    snap = result_snapshot(session, value) or {}
    final_url = snap.get("url") or ""
    network = [dict(r) for r in session.get("network") or [] if not r.get("credential_bound")]
    # an in-page search (the result URL does not carry the value) keeps the
    # page as it was before typing, so the list proof can use the typed
    # changed-region rule (html_extract.derive_typed_items) in tier 1 too. A
    # search whose URL carries the value is a navigation: its pages are
    # compared as documents.
    before = before_snapshot(session, value) if snap and not _value_in(value, final_url) \
        else None
    extra = {}
    if before is not None:
        extra["before_dom"] = before["dom"]
    return {**extra,
        "workflow": session.get("task") or "",
        "site": urllib.parse.urlsplit(final_url).netloc,
        "category": "attached", "cls": cls, "tag": tag,
        "param": {"name": param_name, "value": value},
        "final_url": final_url,
        "ui": {"text": snap.get("text") or "", "items": []},
        "dom": snap.get("dom") or "",
        "network": network,
        "ui_bytes": sum(int(r.get("bytes") or 0) for r in network),
        "recorded_at": session.get("started_at", ""),
        "origin": "attach",
        "attach": {"session": session.get("id"), "browser": session.get("browser"),
                   "target": (session.get("target") or {}).get("id"),
                   "withheld": bool(snap.get("withheld"))},
    }


def task_name(sessions: Sequence[dict]) -> str:
    for s in sessions:
        if s.get("task"):
            return str(s["task"])
    snap = result_snapshot(sessions[-1]) if sessions else None
    host = urllib.parse.urlsplit((snap or {}).get("url") or "").netloc
    return "attach_" + re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_") if host else "attach"


def select_sessions(all_sessions: Sequence[dict], task: str = "",
                    n: int = 0) -> List[dict]:
    """The last ``n`` (default: all) sessions of ``task`` (default: the latest one's)."""
    if not all_sessions:
        raise AttachError("no attach sessions are stored; run `anyuse attach` first")
    want = task or task_name([all_sessions[-1]])
    same = [s for s in all_sessions if task_name([s]) == want]
    if n:
        same = same[-n:]
    return same


#: A tool is "proved" only when the result-list rule and the request choice
#: were fitted without ever seeing the validation sessions. That needs one
#: compile session, one selection session and at least two untouched
#: validation sessions.
from anyuse.execution import proof_split as _PS

MIN_SESSIONS_PROVED = 1 + _PS.N_SELECTION + _PS.MIN_VALIDATION
STATUS_PROVED = _PS.PROVED
STATUS_CHECKED = _PS.CHECKED
STATUS_FAILED = _PS.NOT_PROVED


def proof_status(passed: bool, selection_held_out: bool) -> str:
    return _PS.status_for(passed, selection_held_out)


SHAPES = ("auto", "list", "detail")


#: path words before the input that mark a path-style search (``/search/vim``): the last
#: rung of :func:`session_shape`, after the recorded pages' own evidence
_SEARCHY = re.compile(r"(?i)^(search|suche|s|q|query|find|results?|lookup)$")


#: page extensions an item URL may carry after its key (``/mineral/quartz.htm``)
_ITEM_EXT = re.compile(r"(?i)\.(?:s?html?|php|aspx?|jsp|cfm)$")


def _segment_key(value: str, seg: str) -> Optional[Tuple[str, str]]:
    """``(transform, extension)`` when path segment ``seg`` IS the item key
    ``value``: verbatim, lower-cased, or with spaces as hyphens
    (``param_binding.TRANSFORMS``), optionally followed by a page extension.
    ``None`` otherwise."""
    from anyuse.execution.param_binding import TRANSFORMS
    raw = urllib.parse.unquote(seg)
    m = _ITEM_EXT.search(raw)
    ext = m.group(0) if m else ""
    stem = raw[:-len(ext)] if ext else raw
    for name, fn in TRANSFORMS:
        if stem == fn(value):
            return name, ext
    if stem.lower() == value.lower():
        return "lower", ext
    return None


def _key_segment(value: str, url: str) -> Optional[int]:
    """Index of the ONE path segment that is the key ``value``, else None."""
    segs = [x for x in urllib.parse.urlsplit(url or "").path.split("/") if x]
    hits = [i for i, x in enumerate(segs) if value and _segment_key(value, x)]
    return hits[0] if len(hits) == 1 else None


def _in_path(value: str, url: str) -> bool:
    """The value is one path segment (verbatim, lower-cased or slugged, with or
    without a page extension), and the segment before it is not a search
    word (``/search/vim`` is a path-style SEARCH, a list)."""
    parts = urllib.parse.urlsplit(url or "")
    segs = [urllib.parse.unquote(x).lower() for x in parts.path.split("/") if x]
    i = _key_segment(value, url)
    if i is None:
        return False
    return not (i and _SEARCHY.match(segs[i - 1]))


def _in_query(value: str, url: str) -> bool:
    q = urllib.parse.parse_qsl(urllib.parse.urlsplit(url or "").query, keep_blank_values=True)
    return any(value.lower() == v.lower() for _k, v in q)


def _shape_of(url: str, k: int) -> Tuple[str, Tuple[str, ...], int]:
    """Where a page stands among the site's pages, its key segment ``k`` left out:
    the host, the path segments before the key, and how many segments there are."""
    sp = urllib.parse.urlsplit(url or "")
    segs = [x for x in sp.path.split("/") if x]
    return (sp.netloc.lower(), tuple(segs[:k]), len(segs))


def _has_shape(url: str, shape: Tuple[str, Tuple[str, ...], int]) -> bool:
    sp = urllib.parse.urlsplit(url or "")
    segs = [x for x in sp.path.split("/") if x]
    host, prefix, n = shape
    return sp.netloc.lower() == host and len(segs) == n and tuple(segs[:len(prefix)]) == prefix


def _lines_of(dom: str, memo: Optional[Dict[str, Any]] = None) -> List[Tuple[str, Any]]:
    """``(text, block)`` of each visible line of ``dom``. ``memo``: pages already read in
    this decision (every session's result page is compared with every recorded page)."""
    from anyuse.execution.html_extract import parse
    from anyuse.execution.result_contract import visible_lines
    dom = dom or ""
    if memo is not None and dom in memo:
        return memo[dom]
    try:
        out = [(t, b) for t, b, _h in visible_lines(parse(dom, visible_only=True))]
    except ValueError:
        out = []
    if memo is not None:
        memo[dom] = out
    return out


def _links_of(block: Any) -> List[Tuple[str, str]]:
    """``(href, text)`` of the links in the line ``block`` shows: an ``a`` around it, or the
    ``a`` elements among its inline content."""
    from anyuse.execution.result_contract import _INLINE_TAGS
    cur = block
    while cur is not None:
        if cur.tag == "a":
            return [(str(cur.href or ""), cur.text())]
        cur = cur.parent
    out, stack = [], list(getattr(block, "children", None) or [])
    while stack:
        n = stack.pop()
        if n.tag == "a":
            out.append((str(n.href or ""), n.text()))
        elif n.tag in _INLINE_TAGS:
            stack.extend(n.children)
    return out


def _lists_the_input(page: dict, value: str, target: str,
                     memo: Optional[Dict[str, Any]] = None) -> bool:
    """Does the recorded page ``page`` list ``value`` as one entry: a link to the page
    ``target``, or a line that is the input alone where other lines of the page stand too
    (a list's entries)?"""
    from anyuse.execution.api_compile import normalise
    from anyuse.execution.empty_page import _path
    want, url = normalise(value), str(page.get("url") or "")
    to = urllib.parse.urlsplit(target)
    lines = _lines_of(str(page.get("dom") or ""), memo)
    places: Dict[str, int] = {}
    for _t, b in lines:
        if b is not None:
            places[_path(b)] = places.get(_path(b), 0) + 1
    for t, b in lines:
        for href, _text in (_links_of(b) if b is not None else []):
            if not href:
                continue
            got = urllib.parse.urlsplit(urllib.parse.urljoin(url, href))
            if got.netloc.lower() == to.netloc.lower() and \
                    got.path.rstrip("/") == to.path.rstrip("/"):
                return True
        if want and normalise(t) == want and b is not None and places.get(_path(b), 0) >= 2:
            return True
    return False


class _InputValues(_HTMLParser):
    """The ``value`` of each text or search ``input`` of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if a.get("type", "text").strip().lower() in ("text", "search", "") and a.get("value"):
            self.values.append(a["value"])


def _target_shape(url: str) -> Tuple[str, Tuple[str, ...], int]:
    """A link target's shape: its host, the path segments before its last one, and how
    many segments there are (``/product/a-1`` and ``/product/b-2`` share one)."""
    sp = urllib.parse.urlsplit(url or "")
    segs = [x for x in sp.path.split("/") if x]
    return (sp.netloc.lower(), tuple(segs[:-1]), len(segs))


def _lists_results(page: dict, value: str, shape: Tuple[str, Tuple[str, ...], int],
                   memo: Optional[Dict[str, Any]] = None) -> bool:
    """Does the result page ``page`` list results for ``value``: its own search field holds
    the typed input, or two or more lines in one place of the page each link, by a text
    that names the input, to a page of this site that has another shape than this one,
    and those pages share one shape (the site's items)? A related item of this page's
    shape is a sibling, not a result; links to other sites (a code host, documentation),
    to pages under this page's own path (its versions, its issues) or to pages of
    different shapes are an item page's own links, not a result list."""
    from anyuse.execution.api_compile import normalise
    from anyuse.execution.empty_page import _path
    from anyuse.execution.result_contract import _names_all
    dom, url = str(page.get("dom") or ""), str(page.get("url") or "")
    p = _InputValues()
    try:
        p.feed(dom)
        p.close()
    except Exception:                                  # noqa: BLE001 - a malformed page
        pass
    if any(normalise(v) and normalise(v) == normalise(value) for v in p.values):
        return True
    here = urllib.parse.urlsplit(url)
    host, own = here.netloc.lower(), here.path.rstrip("/") + "/"
    rows: Dict[Tuple[str, Tuple[str, Tuple[str, ...], int]], int] = {}
    for t, b in _lines_of(dom, memo):
        if b is None or not _names_all(t, value):
            continue
        targets = set()
        for href, text in _links_of(b):
            if not href or not _names_all(text, value):
                continue
            to = urllib.parse.urljoin(url, href)
            sp = urllib.parse.urlsplit(to)
            # a page under this page's own path is one of its sub-pages, not a result
            if sp.netloc.lower() == host and not _has_shape(to, shape) \
                    and not sp.path.startswith(own):
                targets.add(_target_shape(to))
        for target in targets:
            key = (_path(b), target)
            rows[key] = rows.get(key, 0) + 1
    return any(n >= 2 for n in rows.values())


def _page_kind(sessions: Sequence[dict], i: int, value: str, page: dict, k: int,
               memo: Optional[Dict[str, Any]] = None) -> str:
    """``item``, ``list`` or ``""``: what the recorded pages say session ``i``'s result page
    is. An item when another recorded page of another shape (a listing, the site's home
    page) lists the input as one entry and the page lists no results; a list when the page
    lists results and no such page lists the input."""
    url = str(page.get("url") or "")
    shape = _shape_of(url, k)
    item = any(_lists_the_input(snap, value, url, memo)
               for s in sessions for snap in (s.get("snapshots") or [])
               if snap.get("dom") and not _has_shape(str(snap.get("url") or ""), shape))
    listed = _lists_results(page, value, shape, memo)
    return "item" if item and not listed else ("list" if listed and not item else "")


def session_shape(sessions: Sequence[dict], values: Sequence[str]) -> str:
    """``detail`` when every session ended on an ITEM page whose path segment is its
    input (``/package/vim``) and no query parameter carries it; else ``list``.

    A path that holds the input is an item page or a path-style search (``/zoeken/fiets``).
    The recorded pages decide first (:func:`_page_kind`): a page another recorded page lists
    as one entry is an item; a page that lists results for the input (its search field holds
    the input, or its rows link by the input's name to pages of another shape) is a search.
    When they say nothing, or say both, the word before the segment decides
    (:data:`_SEARCHY`, the last rung)."""
    finals = []
    for i, (s, v) in enumerate(zip(sessions, values)):
        page = result_snapshot(s, v) or {}
        url = str(page.get("url") or "")
        k = _key_segment(v, url)
        if k is None or _in_query(v, url):
            return "list"
        finals.append((i, v, page, k))
    memo: Dict[str, Any] = {}
    kinds = {_page_kind(sessions, i, v, page, k, memo) for i, v, page, k in finals}
    if "list" in kinds and "item" not in kinds:
        return "list"
    if "item" in kinds and "list" not in kinds:
        return "detail"
    return "detail" if all(_in_path(v, str(page.get("url") or ""))
                           for _i, v, page, _k in finals) else "list"


def _signed_in_list_values(rec: dict, site: str) -> Dict[str, List[str]]:
    """Generalised JSON list path -> values, over the signed-in (login=replay)
    data responses of the opted-in site in one recording."""
    from anyuse.execution.api_compile import eligible_requests, generalise_path
    from anyuse.execution.api_step import json_leaves
    from anyuse.execution.site_modes import site_key
    out: Dict[str, List[str]] = {}
    for req in eligible_requests(rec):
        if not req.get("cookie_names"):
            continue
        if site_key(urllib.parse.urlsplit(str(req.get("url") or "")).hostname or "") != site:
            continue
        for path, value in json_leaves(req.get("_data")):
            if "[" not in path or isinstance(value, bool) or \
                    not isinstance(value, (str, int, float)):
                continue
            out.setdefault(generalise_path(path), []).append(str(value))
    return out


def _response_ground_truth(fit: Sequence[dict], site: str) -> str:
    """The JSON list path that is the result list, chosen across the FITTING
    recordings only: present in every one, non-empty, and different for every
    input (a list that does not change with the input answers nothing). Text
    values win over ids."""
    per = [_signed_in_list_values(r, site) for r in fit]
    common = set(per[0]) if per else set()
    for p in per[1:]:
        common &= set(p)
    ranked = []
    for path in common:
        lists = [tuple(p[path]) for p in per]
        if any(not values for values in lists) or len(set(lists)) < len(lists):
            continue
        alpha = sum(sum(ch.isalpha() for ch in v) for values in lists for v in values)
        ranked.append(((min(len(v) for v in lists), alpha, -len(path)), path))
    if not ranked:
        raise AttachError("no signed-in JSON response of the site carries a list that "
                          "changes with the input, so there is no ground truth to prove on")
    return max(ranked)[1]


def _compile_login_replay_sessions(sessions, name, values, how, site, executor=None,
                                   preflight=None, robots_policy=None, min_cases=2,
                                   transport=None, label_keys=True):
    """Compile and PROVE a site-mode login=replay workflow from attach sessions.

    The signed-in pages were withheld at capture (their text and DOM are never
    stored), so the ground truth is the signed-in DATA response the compile
    needs: the JSON list path that changes with the input, chosen on the
    fitting sessions only and read from every validation session at that same
    path. The proof replays with the imported session through an executor that
    reads the site's modes via the one accessor. It is constructed only here,
    for sessions captured with ``--login-replay``, which no bench records."""
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify_with_fit, permission_preflight

    recs = [session_to_recording(s, name, v, f"attach{i}")
            for i, (s, v) in enumerate(zip(sessions, values))]
    split = _PS.assign(recs, min_validation=min_cases)
    fit = ([split.compile, *split.fitting, *split.selection] if split.held_out else recs)
    path = _response_ground_truth(fit, site)
    for r in recs:
        r["ui"]["items"] = _signed_in_list_values(r, site).get(path, [])[:30]
        r["ui"]["text"] = ""
        r["ground_truth_fitted_on"] = [str((f.get("param") or {}).get("value") or "")
                                       for f in fit]
    split.note_fit(_PS.value_of(r) for r in fit)
    origins: List[str] = []
    for r in recs[:1 + len(split.selection)]:
        for req in r["network"]:
            o = _origin(req.get("url", ""))
            if o not in origins:
                origins.append(o)
    base = recs[0]

    def gate(compiled):
        template = compiled.template
        steps = getattr(template, "steps", None) if template is not None else None
        urls = ([st.template.url for st in steps] if steps else
                ([template.url] if template is not None else []))
        for url in urls:
            if _origin(url) not in origins:
                return (f"attach origin check refused {_origin(url)}: it was not "
                        f"observed in the compile session")
        if preflight is not None:
            return preflight(compiled)
        from anyuse.execution.api_permission import check_permission
        return permission_preflight(
            compiled, base, allowed_origins=origins,
            checker=lambda url, hosts_path=None: check_permission(
                url, hosts_path=hosts_path, policy=robots_policy))

    proof_executor = executor or ApiStepExecutor(transport=transport, apply_site_modes=True)
    # a chain is fitted on the selection session, which the ground truth was fitted on too;
    # never on a generated input: signed-in response content is no input and no record field
    compiled, proof, chain_fit = compile_and_verify_with_fit(
        base, split.validation, split.selection, executor=proof_executor,
        min_cases=min_cases, preflight=gate, selection_recordings=split.selection,
        split=split, generate=False, label_keys=label_keys)
    _chain_hint(compiled, proof, chain_fit, len(recs), min_cases)
    lat = sorted(c.elapsed_ms for c in proof.cases if getattr(c, "elapsed_ms", None))
    report: Dict[str, Any] = {
        "sessions": [s.get("id") for s in sessions], "param": name, "values": values,
        "param_source": how, "ground_truth_selector": path, "shape": "list",
        "proof_kind": "list (signed-in response)",
        "ground_truth": ("the signed-in data response of each session at one JSON list "
                         "path (the signed-in pages were withheld)"),
        "ground_truth_source": ("signed-in data response, login=replay; pages withheld "
                                "(docs/SITE_MODES.md)"),
        # signed-in items are never echoed or stored
        "ground_truth_sample": [],
        "login_replay_site": site,
        "n_selection": len(split.selection),
        **_chain_fit_report(chain_fit),
        "selection_held_out": bool(proof.split.get("held_out")),
        "status": proof.effective_status, "scope": proof.scope,
        "repeat_ms": lat, "repeat_ms_median": lat[len(lat) // 2] if lat else None,
        "origins": origins,
    }
    return compiled, proof, report, recs


def _chain_hint(compiled, proof, chain_fit, n_sessions: int, min_cases: int) -> None:
    """Say what to record when a chain found in the compile session has no
    session to be fitted on, or the session it was fitted on does not show it
    (``api_verify.compile_and_verify_with_fit``)."""
    if not getattr(compiled, "chain_needs_fitting", False):
        return
    need = 2 + min_cases
    if chain_fit is not None:
        session = str((chain_fit.get("attach") or {}).get("session") or "")
        hint = (f". Session {session or '2'} does not open a result the way the first "
                f"session does, so it cannot fit the chain: record it again, opening the "
                f"result")
    else:
        note = getattr(compiled, "chain_fit_note", "")
        tried = (f". A value taken from the first session's own search results did not fit "
                 f"it ({note})" if note else "")
        hint = (f"{tried}. This task opens a search result with a second request, so it needs "
                f"{need} sessions with different inputs: the second fits the chain and the "
                f"later ones prove it. "
                + ("Record another session and compile again" if n_sessions < need else
                   "The second session has no JSON traffic to fit it on: record it again"))
    compiled.reason += hint
    proof.reason += hint


def _chain_fit_report(chain_fit, proof=None) -> Dict[str, Any]:
    """The report entry naming the session a chain was fitted on. It keeps its
    place in the session order (``n_selection``), so a recompile from the stored
    sessions fits the chain on it again. A fitting input generated from the
    site's own search (``proof.chain_fit``) is named as ``chain_fit`` instead:
    it is no session."""
    generated = getattr(proof, "chain_fit", None) if proof is not None else None
    if generated:
        return {"chain_fit": dict(generated)}
    if chain_fit is None:
        return {}
    return {"chain_fitted_on": str(((chain_fit.get("attach") or {}).get("session")) or "")}


def compile_sessions(sessions: Sequence[dict], executor=None, preflight=None,
                     robots_policy: Optional[str] = None, min_cases: int = 2,
                     shape: str = "auto", transport=None, model_scope: str = "compile",
                     prefer_fit: Sequence[str] = (), label_keys: bool = True):
    """Compile from attached sessions and prove on the ones not compiled from.

    With >= :data:`MIN_SESSIONS_PROVED` sessions the result-list selector and
    the request choice are fitted on the first two only (compile + selection)
    and every later session is validation that fitting never saw; a pass is
    ``"proved"``. With 3 sessions the selector has to be derived across all
    three result pages, so the validation sessions took part in fitting; a pass
    is only ``"checked (selection not held out)"``, never ``"proved"``.
    Returns ``(compiled, proof, report, recordings)``; ``report["status"]``
    carries the verdict. ``model_scope``: the model-check scope a chain's
    generated fitting input may ask its question in (``""``: no model is
    asked). ``prefer_fit``: values to try first for that input (the one a
    record keeps from its last compile). ``label_keys``: name an item page's
    extract keys after the page's labels (``api_verify.compile_and_verify``); a
    recompile of a tool published with positional ``field_N`` keys keeps them.
    """
    from anyuse.execution.api_compile import page_chrome, visible_items
    from anyuse.execution.api_verify import compile_and_verify_with_fit, permission_preflight
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth

    sessions = list(sessions)
    if len(sessions) < 1 + min_cases:
        raise AttachError(f"the fresh-input proof needs {min_cases} input(s) that were "
                          f"not compiled from: record at least {1 + min_cases} sessions "
                          f"of the same task with different inputs ({len(sessions)} given)")
    name, values, how = infer_param(sessions)
    if len({v.lower() for v in values}) != len(values):
        raise AttachError("two sessions used the same input; each must differ")
    login_sites = {str(s.get("login_replay_site") or "") for s in sessions}
    if any(login_sites):
        if len(login_sites) != 1:
            raise AttachError("these sessions mix login=replay captures with others, or "
                              "captures of different sites; compile them separately")
        return _compile_login_replay_sessions(
            sessions, name, values, how, login_sites.pop(), executor=executor,
            preflight=preflight, robots_policy=robots_policy, min_cases=min_cases,
            transport=transport, label_keys=label_keys)
    if shape not in SHAPES:
        raise AttachError(f"shape must be one of {', '.join(SHAPES)}")
    if shape == "auto":
        shape = session_shape(sessions, values)
    if shape == "detail":
        # item pages: the detail-specific proof (as in discovery), never the
        # list proof's repeated-group ground truth
        return compile_detail_sessions(sessions, name, values, how, transport=transport,
                                       preflight=preflight, robots_policy=robots_policy,
                                       min_cases=min_cases)
    recs = [session_to_recording(s, name, v, f"attach{i}")
            for i, (s, v) in enumerate(zip(sessions, values))]
    withheld = [r["attach"]["session"] for r in recs if r["attach"]["withheld"]]
    if withheld:
        raise AttachError("a result page was loaded signed in and was not stored "
                          f"(session {withheld[0]}); signed-in pages are out of scope")
    if not all(r["dom"] for r in recs):
        raise AttachError("a session has no stored result page snapshot")
    # the shared rule (execution/proof_split.py): >= 4 sessions hold out the
    # validation sessions; fewer cannot, and earn at most "checked"
    split = _PS.assign(recs, min_validation=min_cases)
    n_selection = len(split.selection)
    if split.held_out:
        fit = [split.compile, *split.fitting, *split.selection]
        selector = derive_ground_truth(fit)
        # a typed-region selector reads the pages visible-only; the validation
        # pages are read the same way
        vis = any(r.get("ground_truth_visible_only") for r in fit)
        for r in split.validation:
            r["ground_truth_selector"] = selector
            if vis:
                r["ground_truth_visible_only"] = True
            # the same two readings as derive_ground_truth: whole rows, and each row's name
            r["ui"]["items"] = (extract(r["dom"], selector, limit=30, visible_only=vis)
                                if selector else [])
            r["ui"]["labels"] = (extract(r["dom"], selector, limit=30, visible_only=vis,
                                         labels=True) if selector else [])
    else:
        fit = recs
        selector = derive_ground_truth(recs)
    fitted_on = [str((r.get("param") or {}).get("value") or "") for r in fit]
    for r in recs:
        r["ground_truth_fitted_on"] = list(fitted_on)
    split.note_fit(_PS.value_of(r) for r in fit)
    report: Dict[str, Any] = {
        "sessions": [s.get("id") for s in sessions], "param": name, "values": values,
        "param_source": how, "ground_truth_selector": selector, "shape": "list",
        "proof_kind": "list",
        "ground_truth": "visible text of each session's result page",
        "n_selection": n_selection,
    }
    if not selector:
        raise AttachError(recs[0].get("recorder_warning") or
                          "no result list could be derived: no repeated group on the "
                          "result pages changed between the inputs")
    chrome = page_chrome(fit)
    # the row names (when a row has a heading or a named link) read better than whole rows
    report["ground_truth_sample"] = (list((recs[0].get("ui") or {}).get("labels") or [])
                                     or visible_items(recs[0], exclude=chrome))[:8]
    origins: List[str] = []
    for r in recs[:1 + n_selection]:
        for req in r["network"]:
            o = _origin(req.get("url", ""))
            if o not in origins:
                origins.append(o)
    base = recs[0]

    def gate(compiled):
        template = compiled.template
        steps = getattr(template, "steps", None) if template is not None else None
        urls = ([st.template.url for st in steps] if steps else
                ([template.url] if template is not None else []))
        for url in urls:
            if _origin(url) not in origins:
                return (f"attach origin check refused {_origin(url)}: it was not "
                        f"observed in the compile session")
        if preflight is not None:
            return preflight(compiled)
        from anyuse.execution.api_permission import check_permission
        return permission_preflight(
            compiled, base, allowed_origins=origins,
            checker=lambda url, hosts_path=None: check_permission(
                url, hosts_path=hosts_path, policy=robots_policy))

    # a chain is fitted on the selection session, which the result list was fitted on too;
    # with no session free for it, on another result's name from the first session's search
    compiled, proof, chain_fit = compile_and_verify_with_fit(
        base, split.validation, split.selection, executor=executor, min_cases=min_cases,
        preflight=gate, selection_recordings=split.selection, split=split,
        model_scope=model_scope, prefer_fit=prefer_fit, label_keys=label_keys)
    _chain_hint(compiled, proof, chain_fit, len(recs), min_cases)
    report.update(_chain_fit_report(chain_fit, proof))
    report["selection_held_out"] = bool(proof.split.get("held_out"))
    report["status"] = proof.effective_status
    report["scope"] = proof.scope
    lat = sorted(c.elapsed_ms for c in proof.cases if getattr(c, "elapsed_ms", None))
    report["repeat_ms"] = lat
    report["repeat_ms_median"] = lat[len(lat) // 2] if lat else None
    report["origins"] = origins
    return compiled, proof, report, recs


# ---------------------------------------------------------------------------
# item pages: the detail-specific proof
# ---------------------------------------------------------------------------
#
# The LIST proof needs a repeated group that changes with the input. An item
# page has no such group, so there a detail task would fail or be "proved" on
# page chrome. Item-page sessions take the detail proof discovery uses
# (``discover.page_fields``: title, h1-h3, itemprop=name, og:title), with roles
# from ``proof_split.assign``:
#
# * compile + selection sessions (FIT): the field is chosen here only. It must be
#   on every fit page, differ between them, and name that page's key;
# * validation sessions: each key is fetched ONCE, fresh, through the compiled
#   URL template (never a cache). The chosen field on the fresh page must equal
#   the field on that session's CAPTURED page (what the agent saw: the ground
#   truth) and name the key; all field texts must be pairwise different.
#
# The claim is bounded: one identifying field reproduced per held-out key.

@dataclass
class DetailCase:
    value: str
    passed: bool
    reason: str = ""
    field: str = ""
    got: str = ""
    expected: str = ""
    elapsed_ms: float = 0.0


@dataclass
class DetailProof:
    """The outcome of the detail proof, shaped like ``ProofResult`` where the
    CLI and benches read it."""
    passed: bool
    cases: List[DetailCase] = field(default_factory=list)
    reason: str = ""
    split: Dict[str, Any] = field(default_factory=dict)
    effective_status: str = "not proved"
    scope: str = ""
    kind: str = "detail_fields"
    field: str = ""
    template_url: str = ""

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "passed": self.passed, "status": self.effective_status,
                "reason": self.reason, "split": dict(self.split), "scope": self.scope,
                "field": self.field, "template_url": self.template_url,
                "passed_count": self.passed_count, "case_count": len(self.cases),
                "selection_held_out": bool(self.split.get("held_out")),
                "cases": [dict(c.__dict__) for c in self.cases]}


def _detail_param_name(name: str) -> str:
    """A stable RequestTemplate placeholder from an attached input label."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(name or "")).strip("_") or "key"


def _detail_template(url: str, value: str, param: str) -> str:
    """``https://h/package/{package}`` from the compile session's item URL
    (``{mineral}.htm`` when the key carries a page extension)."""
    return _detail_template_t(url, value, param)[0]


def _detail_template_t(url: str, value: str, param: str) -> Tuple[str, str]:
    """``(template, transform)``: the transform (``param_binding``) turns an
    input into the key as the URL carries it (``lower``: ``Quartz`` ->
    ``quartz``); ``identity`` when it is verbatim."""
    parts = urllib.parse.urlsplit(url)
    segs = parts.path.split("/")
    hit = [(i, _segment_key(value, x)) for i, x in enumerate(segs) if x]
    hit = [(i, k) for i, k in hit if k]
    if len(hit) != 1:
        raise AttachError(f"the input {value!r} is not exactly one path segment of {url[:120]}")
    i, (transform, ext) = hit[0]
    segs[i] = "{" + param + "}" + ext
    return (urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/".join(segs), parts.query,
                                     "")), transform)


def _render(template: str, param: str, value: str, transform: str = "") -> str:
    if transform and transform != "identity":
        from anyuse.execution.param_binding import apply_transform
        value = apply_transform(transform, value)
    return template.replace("{" + param + "}", urllib.parse.quote(value, safe=""))


def _same_page(a: str, b: str) -> bool:
    def n(u):
        p = urllib.parse.urlsplit(u or "")
        return (p.scheme, p.netloc.lower(), urllib.parse.unquote(p.path).rstrip("/").lower(),
                p.query)
    return n(a) == n(b)


def compile_detail_sessions(sessions: Sequence[dict], name: str, values: Sequence[str],
                            how: str, transport=None, preflight=None,
                            robots_policy: Optional[str] = None, min_cases: int = 2):
    """Prove item-page sessions with the detail proof. Returns ``(compiled,
    proof, report, recordings)`` like :func:`compile_sessions`; ``compiled`` is
    a namespace with an executable ``RequestTemplate`` and ``proof`` a
    :class:`DetailProof`."""
    import types
    from anyuse.execution import discover as D
    from anyuse.execution.api_step import RequestTemplate
    param = _detail_param_name(name)
    recs = [session_to_recording(s, param, v, f"attach{i}", cls="detail")
            for i, (s, v) in enumerate(zip(sessions, values))]
    withheld = [r["attach"]["session"] for r in recs if r["attach"]["withheld"]]
    if withheld:
        raise AttachError("a result page was loaded signed in and was not stored "
                          f"(session {withheld[0]}); signed-in pages are out of scope")
    if not all(r["dom"] for r in recs):
        raise AttachError("a session has no stored item page snapshot")
    split = _PS.assign(recs, min_validation=min_cases)
    fit = [split.compile, *split.fitting, *split.selection] if split.held_out else recs
    split.note_fit(_PS.value_of(r) for r in fit)
    template, transform = _detail_template_t(recs[0]["final_url"], values[0], param)
    for r in fit:
        if not _same_page(r["final_url"], _render(template, param, r["param"]["value"],
                                                  transform)):
            raise AttachError(f"the item pages do not share one URL template: "
                              f"{r['final_url'][:100]} is not {template}")
    compiled = types.SimpleNamespace(
        template=None,
        reason="")
    report: Dict[str, Any] = {
        "sessions": [s.get("id") for s in sessions], "param": param, "values": list(values),
        "param_source": how, "shape": "detail", "proof_kind": "detail_fields",
        "ground_truth": "the identifying field of each session's captured item page",
        "ground_truth_selector": None, "n_selection": len(split.selection),
        "template_url": template}

    def finish(proof: DetailProof):
        split.finalize()
        proof.split = split.to_dict(proof.passed_count)
        proof.effective_status = split.status(proof.passed)
        proof.scope = _PS.scope_text({"status": proof.effective_status,
                                      "case_count": len(proof.cases), "split": proof.split},
                                     shape="detail", fields=[proof.field] if proof.field else [])
        proof.template_url = template
        report["selection_held_out"] = bool(split.held_out)
        report["status"] = proof.effective_status
        report["scope"] = proof.scope
        report["field"] = proof.field
        lat = sorted(c.elapsed_ms for c in proof.cases if c.elapsed_ms)
        report["repeat_ms"] = lat
        report["repeat_ms_median"] = lat[len(lat) // 2] if lat else None
        report["ground_truth_sample"] = [c.expected for c in proof.cases][:8]
        return compiled, proof, report, recs

    # -- fit: choose THE field on the compile + selection pages only -----------
    fields = [D.page_fields(r["dom"]) for r in fit]
    naming = []
    for k in D.DETAIL_FIELD_ORDER:
        if not all(k in f for f in fields):
            continue
        texts = [f[k] for f in fields]
        if len({D._norm(t) for t in texts}) < len(texts):
            continue                                   # constant: chrome, not the item
        if all(D.field_names_item(f[k], r["param"]["value"]) or
               D.field_carries_key(f[k], r["param"]["value"]) for f, r in zip(fields, fit)):
            naming.append(k)
    report["fit_fields"] = naming
    if not naming:
        return finish(DetailProof(passed=False, reason=(
            "detail fields: on the compile and selection pages no title, heading or name "
            f"field named the key on every page and changed between them "
            f"(fields seen: {sorted(fields[0])[:8]})")))
    field_ = naming[0]
    compiled.template = RequestTemplate(
        method="GET", url=template, headers={"accept": "text/html"},
        extract={field_: field_}, extract_kind="detail_fields", shape="fields",
        expect={"status_in": [200], "content_type": "html", "field": field_,
                "identity": {"parameter": param, "match": "detail_key"}},
        params=[param], origin="attach",
        param_transforms=({param: transform} if transform != "identity" else {}),
        notes="Fetch an item page and return the identifying field reproduced by "
              "the attach detail proof.")
    #: key -> field text; one entry per key (without a held-out split the
    #: validation sessions are also fit sessions)
    texts = {r["param"]["value"].lower(): f[field_] for f, r in zip(fields, fit)}
    origins: List[str] = []
    for r in fit if split.held_out else recs[:1 + len(split.selection)]:
        for req in r["network"]:
            o = _origin(req.get("url", ""))
            if o and o not in origins:
                origins.append(o)
    report["origins"] = origins
    if _origin(template) not in origins:
        return finish(DetailProof(passed=False, field=field_, reason=(
            f"attach origin check refused {_origin(template)}: it was not observed in the "
            f"compile session")))
    # one request per untouched key (the validation below)
    compiled.proof_requests = (len(split.validation), len(split.validation))
    if preflight is not None:
        why = preflight(compiled)
    else:
        from anyuse.execution.api_permission import check_permission
        from anyuse.execution.api_verify import permission_preflight
        why = permission_preflight(compiled, recs[0], allowed_origins=origins,
                                   checker=lambda url, hosts_path=None: check_permission(
                                       url, hosts_path=hosts_path, policy=robots_policy))
    if why:
        return finish(DetailProof(passed=False, field=field_, reason=str(why)[:300]))
    if transport is None:
        from anyuse.execution.api_step import HttpxTransport
        transport = HttpxTransport()
    # -- validation: ONE fresh request per untouched key -------------------------
    cases: List[DetailCase] = []
    for r in split.validation:
        v = r["param"]["value"]
        url = _render(template, param, v, transform)
        expected = D.page_fields(r["dom"]).get(field_, "")
        case = DetailCase(value=v, passed=False, field=field_, expected=expected)
        cases.append(case)
        if not _same_page(r["final_url"], url):
            case.reason = f"the session's item page {r['final_url'][:80]} is not {url[:80]}"
            continue
        t0 = time.monotonic()
        try:
            if hasattr(transport, "send_with_policy"):
                resp = transport.send_with_policy("GET", url, {"accept": "text/html"}, None,
                                                  15.0, allowed_origins=origins)
            else:
                resp = transport.send("GET", url, {"accept": "text/html"}, None, 15.0)
        except Exception as exc:
            case.reason = f"request refused: {type(exc).__name__}: {exc}"[:200]
            continue
        case.elapsed_ms = (time.monotonic() - t0) * 1000.0
        if not (200 <= int(resp.status) < 300):
            case.reason = f"HTTP {resp.status}"
            continue
        case.got = D.page_fields(resp.text).get(field_, "")
        if not expected:
            case.reason = f"the captured page has no {field_}"
        elif D._norm(case.got) != D._norm(expected):
            case.reason = (f"fresh {field_} {case.got[:60]!r} differs from the captured "
                           f"{expected[:60]!r}")
        elif not (D.field_names_item(case.got, v) or D.field_carries_key(case.got, v)):
            case.reason = f"the {field_} {case.got[:60]!r} does not name the key {v!r}"
        else:
            case.passed = True
            case.reason = f"{field_} {case.got[:60]!r} reproduced and names {v!r}"
            texts[v.lower()] = case.got
    passed = len(cases) >= min_cases and all(c.passed for c in cases)
    reason = "; ".join(c.reason for c in cases if not c.passed)[:300]
    if passed and len({D._norm(t) for t in texts.values()}) < len(texts):
        passed, reason = False, f"two keys gave the same {field_} ({list(texts.values())[:4]})"
    if passed:
        reason = (f"detail proof: the {field_} was reproduced by a fresh request and named "
                  f"the key for {len(cases)} validation session(s)")
    return finish(DetailProof(passed=passed, cases=cases, reason=reason, field=field_))
