"""Human solves live: a person clears a site's human check; AnyUse only waits
or relays their own pointer and keys (``ANYUSE_CHALLENGE_POLICY=human_solves_live``).

**The person solves challenges; AnyUse never
does.** No model ever sees a challenge page or decides where to click: while a
page shows a challenge it is quarantined (:data:`QUARANTINE`,
:func:`model_may_see`), the learn agent stops on it, and nothing in this module
imports or calls a model. This module is a pure pointer relay.

Who may use it
--------------
Only a run whose ``origin`` is listed in ``ANYUSE_LIVE_SOLVE_ORIGINS`` (default
``user_task``; ``scheduled`` and ``bulk`` can be added, security-relevant).
``bench`` is never allowed: a benchmark run with the policy set is refused
outright (:func:`human_inbox.refuse_bench_live_solve`). Any other run falls
back to ``human_does_step``.

Two modes
---------
* **Attached browser** (the user's own visible browser, ``execution/attach.py``):
  AnyUse posts a card / asks "solve it in your browser", then only OBSERVES the
  pinned page (read-only) until it leaves the challenge state: no challenge
  marker, still the task's host, and content present. Then THIS task continues.
* **Headless** (AnyUse's own container browser): a :class:`LiveView` - a tiny
  stdlib HTTP server bound to ``127.0.0.1`` on a random port, one-time token in
  the URL, claimed by the first browser that opens it, expiring when the check
  clears or after ``ANYUSE_LIVE_SOLVE_TIMEOUT_S``. It shows periodic
  ``Page.captureScreenshot`` frames of the ONE pinned page target (held in
  memory only, sent only to that local viewer, never stored, never logged,
  never given to a model) and relays the person's clicks, drags, wheel and keys
  as ``Input.dispatchMouseEvent`` / ``Input.dispatchKeyEvent`` to that same
  target only (:class:`PinnedPage`). The view offers no navigation: no URL
  field, no endpoint that takes a URL, and ``Page.navigate`` is not relayable.

Session scope
-------------
A cleared session is valid for the rest of that ONE task (:class:`SolvedSessions`,
keyed by task id and host). The next task on that host asks again, also inside
a bulk run; at task end a headless browser's data for that origin is cleared.
Every solve is logged in the run's receipt chain (who, when, which task,
outcome, how many inputs were relayed) and never with a screenshot or the view
URL.
"""
from __future__ import annotations

import base64
import getpass
import hmac
import http.cookies
import http.server
import ipaddress
import json
import math
import secrets
import socket
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from anyuse.execution import blocked as _blocked
from anyuse.execution import human_inbox as hi

LOOPBACK_BINDS = ("127.0.0.1", "::1")
#: The only CDP methods the live view may send, to the pinned target only.
RELAY_METHODS = frozenset({"Input.dispatchMouseEvent", "Input.dispatchKeyEvent"})
VIEW_METHODS = frozenset({"Page.captureScreenshot"})
#: Sent once, at task end, to drop what the person's solve left in a headless
#: browser for that origin (the session is valid for one task only).
FORGET_METHODS = frozenset({"Storage.clearDataForOrigin"})
#: A cleared page must show at least this much text (or a result list).
MIN_CONTENT_CHARS = 40
MAX_EVENTS_PER_POST = 32
MAX_EVENTS_PER_SECOND = 60
MAX_BODY_BYTES = 8192
#: Browser-level keys: never relayed (they could navigate or open tooling).
FORBIDDEN_KEYS = frozenset({
    "BrowserBack", "BrowserForward", "BrowserRefresh", "BrowserHome", "BrowserSearch",
    "BrowserFavorites", "BrowserStop", "F5", "F6", "F11", "F12", "LaunchApplication1",
    "LaunchApplication2", "LaunchMail", "Help", "ContextMenu",
})
#: CDP modifier bits: Alt=1, Ctrl=2, Meta=4, Shift=8. Only Shift is relayed.
SHIFT = 8

MODE_ATTACH = "attach"
MODE_HEADLESS = "headless"
RECEIPT_TYPE = "live_solve"


class RelayRefused(ValueError):
    """An event or a method the relay will not send."""


class ChallengeQuarantined(RuntimeError):
    """A model hook was about to see a challenge page."""


# ---------------------------------------------------------------------------
# challenge state (the product's own detector; nothing new is guessed here)
# ---------------------------------------------------------------------------

def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()


def _same_host(a: str, b: str) -> bool:
    strip = (lambda h: h[4:] if h.startswith("www.") else h)
    return bool(a) and strip(a) == strip(b)


def challenge_evidence(obs: Mapping[str, Any]) -> str:
    """Why this observation is a challenge page, or ``""``."""
    obs = obs or {}
    url, text = str(obs.get("url") or ""), str(obs.get("text") or "")
    items = list(obs.get("items") or [])
    reqs = [r for r in (obs.get("requests") or []) if isinstance(r, Mapping)]
    # the RULES only: this is the guard in front of every model call (the
    # confirmer included), so it must never itself ask a model
    state = _blocked._detect_blocked_ui_rules(text, str(obs.get("markup") or ""), url, reqs,
                                              items)
    if state.blocked and state.reason in hi.CHALLENGE_REASONS:
        return state.reason
    return _blocked.challenge_served_instead(text, url, [str(r.get("url") or "")
                                                         for r in reqs], items)


def page_state(obs: Mapping[str, Any], host: str) -> str:
    """``challenge`` | ``left_host`` | ``waiting`` (no content yet) | ``cleared``."""
    if challenge_evidence(obs):
        return "challenge"
    url_host = host_of(str((obs or {}).get("url") or ""))
    if host and url_host and not _same_host(url_host, host.lower()):
        return "left_host"
    text = str((obs or {}).get("text") or "").strip()
    if (obs or {}).get("items") or len(text) >= MIN_CONTENT_CHARS:
        return "cleared"
    return "waiting"


# ---------------------------------------------------------------------------
# no model on a challenge page
# ---------------------------------------------------------------------------

class ModelQuarantine:
    """Page targets that show a challenge; no model hook may see them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._targets: set = set()

    def enter(self, target_id: str) -> None:
        with self._lock:
            self._targets.add(str(target_id))

    def leave(self, target_id: str) -> None:
        with self._lock:
            self._targets.discard(str(target_id))

    def active(self, target_id: Optional[str]) -> bool:
        with self._lock:
            return target_id is not None and str(target_id) in self._targets

    def guard(self, hook: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a model hook: refused on a quarantined target or a challenge page."""
        def guarded(*args, target_id: Optional[str] = None,
                    observation: Optional[Mapping[str, Any]] = None, **kwargs):
            if not model_may_see(observation or {}, target_id, quarantine=self):
                raise ChallengeQuarantined("no model sees a challenge page; a person "
                                           "solves it")
            return hook(*args, **kwargs)
        return guarded


QUARANTINE = ModelQuarantine()


def model_may_see(obs: Mapping[str, Any], target_id: Optional[str] = None, *,
                  quarantine: Optional[ModelQuarantine] = None) -> bool:
    """False while ``target_id`` is quarantined or ``obs`` is a challenge page."""
    q = quarantine or QUARANTINE
    if q.active(target_id):
        return False
    return not challenge_evidence(obs or {})


# ---------------------------------------------------------------------------
# the narrow guard exception: the challenge provider's own hosts, during a solve
# ---------------------------------------------------------------------------

#: The challenge providers' own hosts a challenge page loads its check from:
#: ``(exact host, path prefix)``. Declared, never inferred from the page. Only
#: while a live solve is active for ONE page target, only GET/HEAD, only
#: https on the default port (docs/TRAFFIC_POLICY.md "Live-solve exception").
CHALLENGE_PROVIDER_HOSTS: Tuple[Tuple[str, str], ...] = (
    ("challenges.cloudflare.com", "/"),
    ("www.google.com", "/recaptcha/"),
    ("www.gstatic.com", "/recaptcha/"),
    ("www.recaptcha.net", "/recaptcha/"),
    ("hcaptcha.com", "/"),
    ("js.hcaptcha.com", "/"),
    ("newassets.hcaptcha.com", "/"),
    ("imgs.hcaptcha.com", "/"),
)
EXCEPTION_METHODS = frozenset({"GET", "HEAD"})


class ChallengeHostException:
    """Page targets with an active live solve, and what their exception let through.

    ``allows(target, url, method)`` is true only while :meth:`grant` holds for
    exactly that target, for a declared provider host and path prefix, over
    https on the default port, for GET or HEAD. Every decision on a provider
    host is logged with the target, host, method and verdict (never the full
    URL: its query can carry the check's own tokens). :meth:`revoke` ends it
    (solved, expired, declined or the task ended)."""

    def __init__(self, hosts: Tuple[Tuple[str, str], ...] = CHALLENGE_PROVIDER_HOSTS,
                 clock: Callable[[], float] = time.time):
        self.hosts = tuple((h.lower(), p) for h, p in hosts)
        self.clock = clock
        self._lock = threading.Lock()
        self._active: Dict[str, float] = {}
        self.log: List[Dict[str, Any]] = []

    def grant(self, target_id: str) -> None:
        with self._lock:
            self._active[str(target_id)] = self.clock()
            self.log.append({"event": "granted", "target": str(target_id), "t": self.clock()})

    def revoke(self, target_id: str, why: str = "") -> None:
        with self._lock:
            if self._active.pop(str(target_id), None) is not None:
                self.log.append({"event": "revoked", "target": str(target_id),
                                 "why": str(why)[:40], "t": self.clock()})

    def active(self, target_id: Optional[str]) -> bool:
        with self._lock:
            return target_id is not None and str(target_id) in self._active

    def provider(self, url: str) -> bool:
        """``url`` is on a declared provider host and path (any method)."""
        try:
            sp = urllib.parse.urlsplit(str(url or ""))
            port = sp.port
        except ValueError:
            return False
        host = (sp.hostname or "").lower()
        if sp.scheme != "https" or sp.username or sp.password or port not in (None, 443):
            return False
        return any(host == h and (sp.path or "/").startswith(p) for h, p in self.hosts)

    def allows(self, target_id: Optional[str], url: str, method: str = "GET") -> bool:
        if not self.provider(url):
            return False                     # not a provider host: not our business
        ok = self.active(target_id) and str(method or "GET").upper() in EXCEPTION_METHODS
        with self._lock:
            self.log.append({"event": "request", "target": str(target_id or ""),
                             "host": host_of(url), "method": str(method or "GET")[:8],
                             "allowed": bool(ok), "t": self.clock()})
        return ok

    def allowed_count(self, target_id: str) -> int:
        with self._lock:
            return sum(1 for e in self.log if e.get("event") == "request"
                       and e.get("target") == str(target_id) and e.get("allowed"))


CHALLENGE_EXCEPTIONS = ChallengeHostException()


# ---------------------------------------------------------------------------
# the relay: one pinned target, two input methods, one screenshot method
# ---------------------------------------------------------------------------

def _num(value: Any, lo: float, hi_: float, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise RelayRefused(f"{what} is not a number") from None
    if not math.isfinite(x) or x < lo or x > hi_:
        raise RelayRefused(f"{what} is out of range")
    return x


def _mods(ev: Mapping[str, Any]) -> int:
    mods = int(_num(ev.get("mods", 0), 0, 15, "modifiers"))
    if mods & ~SHIFT:
        raise RelayRefused("only Shift is relayed (Ctrl/Alt/Meta shortcuts are not)")
    return mods


_MOUSE = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved",
          "wheel": "mouseWheel"}
_KEY = {"keydown": "keyDown", "keyup": "keyUp", "char": "char"}
_BUTTON_MASK = {"none": 0, "left": 1, "right": 2, "middle": 4}


def sanitize_event(ev: Mapping[str, Any], viewport: Tuple[int, int]) -> Tuple[str, Dict]:
    """One viewer event -> (CDP method, params built from scratch), or refuse."""
    if not isinstance(ev, Mapping):
        raise RelayRefused("an event must be an object")
    kind = str(ev.get("t") or "")
    w, h = viewport
    if kind in _MOUSE:
        x = _num(ev.get("x"), 0, w, "x")
        y = _num(ev.get("y"), 0, h, "y")
        button = str(ev.get("button") or ("none" if kind in ("move", "wheel") else "left"))
        if button not in _BUTTON_MASK:
            raise RelayRefused("unknown mouse button")
        params: Dict[str, Any] = {"type": _MOUSE[kind], "x": x, "y": y,
                                  "modifiers": _mods(ev), "button": button}
        if kind in ("down", "up"):
            params["clickCount"] = int(_num(ev.get("clicks", 1), 1, 3, "clicks"))
        if kind == "move":
            params["buttons"] = int(_num(ev.get("buttons", 0), 0, 7, "buttons"))
        if kind == "wheel":
            params["button"] = "none"
            params["deltaX"] = _num(ev.get("dx", 0), -2000, 2000, "dx")
            params["deltaY"] = _num(ev.get("dy", 0), -2000, 2000, "dy")
        return "Input.dispatchMouseEvent", params
    if kind in _KEY:
        key = ev.get("key")
        if not isinstance(key, str) or not 0 < len(key) <= 24 or not key.isprintable():
            raise RelayRefused("bad key")
        if key in FORBIDDEN_KEYS:
            raise RelayRefused(f"{key} is a browser key and is not relayed")
        params = {"type": _KEY[kind], "key": key, "modifiers": _mods(ev)}
        code = ev.get("code")
        if code is not None:
            if not (isinstance(code, str) and len(code) <= 24 and code.isalnum()):
                raise RelayRefused("bad key code")
            params["code"] = code
        vk = ev.get("vk")
        if vk is not None:
            params["windowsVirtualKeyCode"] = int(_num(vk, 0, 255, "key code"))
        text = ev.get("text")
        if text is not None and kind in ("keydown", "char"):
            if not (isinstance(text, str) and len(text) <= 4 and
                    (text.isprintable() or text in ("\r", "\b", "\t"))):
                raise RelayRefused("bad key text")
            params["text"] = text
        return "Input.dispatchKeyEvent", params
    raise RelayRefused(f"unknown event type {kind[:20]!r}")


class PinnedPage:
    """One page target. ``send(target_id, method, params) -> result`` is the
    caller's CDP transport; this class only ever passes its own pinned target
    id and only the methods above."""

    def __init__(self, target_id: str, send: Callable[[str, str, Dict], Any], *,
                 viewport: Tuple[int, int] = (1280, 900)):
        if not (isinstance(target_id, str) and target_id and len(target_id) <= 128):
            raise ValueError("a live view needs exactly one page target")
        self.target_id = target_id
        self._send = send
        self.viewport = (int(viewport[0]), int(viewport[1]))
        self.relayed = 0
        self._lock = threading.Lock()

    def _call(self, method: str, params: Dict[str, Any], allowed: frozenset) -> Any:
        if method not in allowed:
            raise RelayRefused(f"{method} is not relayed")
        with self._lock:
            return self._send(self.target_id, method, params)

    def screenshot(self) -> bytes:
        res = self._call("Page.captureScreenshot",
                         {"format": "jpeg", "quality": 70, "captureBeyondViewport": False},
                         VIEW_METHODS) or {}
        try:
            return base64.b64decode(str(res.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            return b""

    def relay(self, ev: Mapping[str, Any]) -> None:
        method, params = sanitize_event(ev, self.viewport)
        self._call(method, params, RELAY_METHODS)
        self.relayed += 1

    def forget_origin(self, host: str) -> None:
        host = str(host or "").lower()
        if not host or any(c in host for c in "/:@?# "):
            return
        for scheme in ("https", "http"):
            self._call("Storage.clearDataForOrigin",
                       {"origin": f"{scheme}://{host}", "storageTypes": "all"},
                       FORGET_METHODS)


# ---------------------------------------------------------------------------
# the loopback live view
# ---------------------------------------------------------------------------

def _loopback_client(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AnyUse: solve the check</title>
<style nonce="{nonce}">
body{{margin:0;font:14px system-ui,sans-serif;background:#111;color:#eee}}
header{{padding:8px 12px;background:#222}} #s{{color:#9cf}}
#f{{display:block;max-width:100%;cursor:crosshair;outline:none;margin:8px auto}}
</style></head><body>
<header>Solve the human check on <b>{host}</b> yourself. AnyUse only passes your
clicks and keys to that one page; no model sees it. <span id="s">waiting</span></header>
<img id="f" tabindex="0" alt="the page" width="{w}" height="{h}">
<script nonce="{nonce}">
(() => {{
const W={w}, H={h}, img=document.getElementById('f'), st=document.getElementById('s');
let q=[], alive=true, lastMove=0;
function pos(e){{const r=img.getBoundingClientRect();
  return {{x:Math.max(0,Math.min(W,(e.clientX-r.left)*W/r.width)),
          y:Math.max(0,Math.min(H,(e.clientY-r.top)*H/r.height))}};}}
function mods(e){{return e.shiftKey?8:0;}}
const B=['left','middle','right'];
function push(ev){{q.push(ev); if(q.length>=8) flush();}}
function flush(){{ if(!q.length||!alive) return; const b=JSON.stringify(q.splice(0,32));
  fetch('input',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:b,
    credentials:'same-origin'}}).catch(()=>{{}}); }}
setInterval(flush,60);
img.addEventListener('mousedown',e=>{{e.preventDefault();img.focus();
  push(Object.assign({{t:'down',button:B[e.button]||'left',mods:mods(e)}},pos(e)));}});
img.addEventListener('mouseup',e=>{{e.preventDefault();
  push(Object.assign({{t:'up',button:B[e.button]||'left',mods:mods(e)}},pos(e)));}});
img.addEventListener('mousemove',e=>{{const n=Date.now(); if(n-lastMove<40) return; lastMove=n;
  push(Object.assign({{t:'move',buttons:e.buttons&7,button:e.buttons&1?'left':'none',
    mods:mods(e)}},pos(e)));}});
img.addEventListener('wheel',e=>{{e.preventDefault();
  push(Object.assign({{t:'wheel',dx:e.deltaX,dy:e.deltaY,mods:mods(e)}},pos(e)));}},
  {{passive:false}});
img.addEventListener('contextmenu',e=>e.preventDefault());
img.addEventListener('keydown',e=>{{ if(e.ctrlKey||e.metaKey||e.altKey) return; e.preventDefault();
  const ev={{t:'keydown',key:e.key,code:e.code,vk:e.keyCode,mods:mods(e)}};
  if(e.key.length===1) ev.text=e.key; else if(e.key==='Enter') ev.text='\\r';
  push(ev);}});
img.addEventListener('keyup',e=>{{ if(e.ctrlKey||e.metaKey||e.altKey) return; e.preventDefault();
  push({{t:'keyup',key:e.key,code:e.code,vk:e.keyCode,mods:mods(e)}});}});
async function frame(){{ if(!alive) return;
  try{{ const r=await fetch('frame',{{cache:'no-store',credentials:'same-origin'}});
    if(r.status===200){{ const u=URL.createObjectURL(await r.blob()); const old=img.src;
      img.src=u; if(old&&old.startsWith('blob:')) URL.revokeObjectURL(old); }}
    else if(r.status===410||r.status===403){{ alive=false; }} }}catch(e){{}}
  setTimeout(frame,{interval_ms}); }}
async function state(){{ try{{ const r=await fetch('state',{{cache:'no-store',
    credentials:'same-origin'}}); if(r.status!==200){{alive=false;
    st.textContent='closed: you can close this tab'; return;}}
  const j=await r.json(); st.textContent=j.open?'waiting for the check to clear':
    ('closed ('+j.reason+'): you can close this tab'); if(!j.open){{alive=false;return;}}
  }}catch(e){{}} setTimeout(state,1000); }}
frame(); state();
}})();
</script></body></html>"""


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    view: "LiveView"

    def server_bind(self):
        # http.server's own server_bind adds socket.getfqdn(host), a reverse DNS lookup that on macOS
        # raises the Local Network permission prompt for Python
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AnyUse-live-view"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:        # URLs carry the token: never log
        return

    # -- helpers ---------------------------------------------------------------

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              extra: Optional[Dict[str, str]] = None, csp: str = "") -> None:
        self.send_response(code)
        headers = {
            "Content-Type": ctype, "Content-Length": str(len(body)),
            "Cache-Control": "no-store", "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": csp or "default-src 'none'; frame-ancestors 'none'",
        }
        headers.update(extra or {})
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _route(self) -> Optional[str]:
        """The sub-path after ``/v/<token>/`` if the request may proceed."""
        view = self.server.view
        if not _loopback_client(self.client_address[0]):
            self._send(403, b"loopback only")
            return None
        if self.headers.get("Host") != view.host_header:
            self._send(403, b"wrong host")          # DNS rebinding
            return None
        path = urllib.parse.urlsplit(self.path).path
        parts = path.split("/")
        if len(parts) != 4 or parts[0] or parts[1] != "v":
            self._send(404, b"not found")
            return None
        if not hmac.compare_digest(parts[2].encode(), view.token.encode()):
            self._send(404, b"not found")
            return None
        if not view.is_open():
            self._send(410, f"closed ({view.closed_reason})".encode())
            return None
        return parts[3]

    def _claim_ok(self) -> bool:
        view = self.server.view
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return False
        morsel = jar.get(view.cookie_name)
        return bool(view.claim and morsel is not None and
                    hmac.compare_digest(morsel.value.encode(), view.claim.encode()))

    # -- methods ------------------------------------------------------------------

    def do_GET(self) -> None:
        sub = self._route()
        if sub is None:
            return
        view = self.server.view
        if sub == "":
            claim = view.try_claim()
            if claim is None and not self._claim_ok():
                self._send(403, b"this live view was already opened elsewhere")
                return
            nonce = secrets.token_urlsafe(12)
            w, h = view.page.viewport
            body = _PAGE.format(nonce=nonce, host=_html(view.label), w=w, h=h,
                                interval_ms=int(view.frame_interval_s * 1000)).encode()
            extra = {}
            if claim is not None:
                extra["Set-Cookie"] = (f"{view.cookie_name}={claim}; Path=/v/{view.token}/; "
                                       f"HttpOnly; SameSite=Strict")
            csp = (f"default-src 'none'; img-src blob:; connect-src 'self'; "
                   f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                   f"form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
            self._send(200, body, "text/html; charset=utf-8", extra, csp)
            return
        if not self._claim_ok():
            self._send(403, b"open the view first")
            return
        if sub == "frame":
            data = view.frame()
            if not data:
                self._send(503, b"no frame")
                return
            self._send(200, data, "image/jpeg")
            return
        if sub == "state":
            self._send(200, json.dumps(view.state()).encode(), "application/json")
            return
        self._send(404, b"not found")

    def do_POST(self) -> None:
        sub = self._route()
        if sub is None:
            return
        view = self.server.view
        if sub != "input":
            self._send(404, b"not found")
            return
        if not self._claim_ok():
            self._send(403, b"open the view first")
            return
        if self.headers.get("Origin") != view.origin:
            self._send(403, b"cross-origin input refused")
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._send(415, b"json only")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY_BYTES:
            self._send(413, b"body too large")
            return
        try:
            events = json.loads(self.rfile.read(length))
        except ValueError:
            self._send(400, b"bad json")
            return
        if isinstance(events, Mapping):
            events = [events]
        if not isinstance(events, list) or len(events) > MAX_EVENTS_PER_POST:
            self._send(400, b"bad events")
            return
        try:
            for ev in events:
                view.relay(ev)
        except RelayRefused as exc:
            self._send(400, str(exc).encode()[:200])
            return
        self._send(204)

    def _refuse_method(self) -> None:
        self._send(405, b"method not allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _refuse_method  # noqa: N815


def _html(text: str) -> str:
    import html
    return html.escape(str(text or "the site"))[:120]


class LiveView:
    """A loopback-only view of ONE page target for a person to solve a check in.

    ``start()`` returns the URL (``http://127.0.0.1:<random port>/v/<token>/``).
    It is shown only to the person (elicitation prompt, their own terminal, or
    ``anyuse inbox live``), never to an agent or a model, and never logged.
    """

    cookie_name = "anyuse_lv"

    def __init__(self, page: PinnedPage, *, bind: str = "127.0.0.1", ttl_s: float = 900.0,
                 clock: Callable[[], float] = time.monotonic, frame_interval_s: float = 0.4,
                 label: str = ""):
        if bind not in LOOPBACK_BINDS:
            raise ValueError("the live view binds to loopback only (127.0.0.1 or ::1)")
        self.page, self.bind, self.clock = page, bind, clock
        self.ttl_s, self.frame_interval_s, self.label = float(ttl_s), frame_interval_s, label
        self.token = secrets.token_urlsafe(32)
        self.claim: Optional[str] = None
        self.closed_reason = ""
        self.port = 0
        self._expires = 0.0
        self._frame: Optional[bytes] = None
        self._frame_at = -1e9
        self._window: List[float] = []
        self._lock = threading.Lock()
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> str:
        server_cls = _Server
        if ":" in self.bind:
            server_cls = type("_Server6", (_Server,), {"address_family": socket.AF_INET6})
        self._server = server_cls((self.bind, 0), _Handler)
        self._server.view = self
        self.port = int(self._server.server_address[1])
        self._expires = self.clock() + self.ttl_s
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True,
                                        name="anyuse-live-view")
        self._thread.start()
        return self.url

    @property
    def host_header(self) -> str:
        return f"[{self.bind}]:{self.port}" if ":" in self.bind else f"{self.bind}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.host_header}"

    @property
    def url(self) -> str:
        return f"{self.origin}/v/{self.token}/"

    def is_open(self) -> bool:
        with self._lock:
            if self.closed_reason:
                return False
            if self._server is None:
                return False
            if self.clock() >= self._expires:
                self.closed_reason = "expired"
                self._frame = None
                return False
            return True

    def close(self, reason: str = "closed") -> None:
        with self._lock:
            self.closed_reason = self.closed_reason or reason or "closed"
            self._frame = None
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    # -- used by the handler -------------------------------------------------------

    def try_claim(self) -> Optional[str]:
        """The first opener gets the claim cookie; the token is one-time."""
        with self._lock:
            if self.claim is not None:
                return None
            self.claim = secrets.token_urlsafe(24)
            return self.claim

    def frame(self) -> bytes:
        """The latest screenshot, in memory only; re-captured at most every
        ``frame_interval_s``. Never written anywhere, never given to a model."""
        with self._lock:
            fresh = self.clock() - self._frame_at < self.frame_interval_s
            if fresh and self._frame:
                return self._frame
        data = self.page.screenshot()
        with self._lock:
            if self.closed_reason:
                return b""
            self._frame, self._frame_at = data, self.clock()
            return data

    def relay(self, ev: Mapping[str, Any]) -> None:
        with self._lock:
            now = self.clock()
            self._window = [t for t in self._window if now - t < 1.0]
            if len(self._window) >= MAX_EVENTS_PER_SECOND:
                raise RelayRefused("too many events")
            self._window.append(now)
        self.page.relay(ev)

    def state(self) -> Dict[str, Any]:
        open_ = self.is_open()
        return {"open": open_, "reason": self.closed_reason,
                "expires_in": max(0, int(self._expires - self.clock())) if open_ else 0}


# ---------------------------------------------------------------------------
# session scope: one task
# ---------------------------------------------------------------------------

class SolvedSessions:
    """Which (task, host) a person cleared. Valid for that one task only."""

    def __init__(self):
        self._lock = threading.Lock()
        self._grants: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def grant(self, task_id: str, host: str, origin: str,
              forget: Optional[Callable[[str], None]] = None) -> None:
        with self._lock:
            self._grants[(str(task_id), str(host).lower())] = {
                "origin": origin, "forget": forget}

    def valid(self, task_id: str, host: str, origin: Optional[str]) -> bool:
        with self._lock:
            g = self._grants.get((str(task_id), str(host or "").lower()))
        return bool(g) and g["origin"] == origin and origin != hi.ORIGIN_BENCH

    def end_task(self, task_id: str) -> List[str]:
        """Drop the task's grants; a headless browser forgets that origin."""
        with self._lock:
            keys = [k for k in self._grants if k[0] == str(task_id)]
            gone = [(k[1], self._grants.pop(k)) for k in keys]
        for host, g in gone:
            if g.get("forget"):
                try:
                    g["forget"](host)
                except Exception:        # noqa: BLE001 - the grant is gone regardless
                    pass
        return sorted(h for h, _ in gone)


SESSIONS = SolvedSessions()


# ---------------------------------------------------------------------------
# the solve
# ---------------------------------------------------------------------------

@dataclass
class SolveRequest:
    task_id: str
    host: str
    origin: Optional[str]
    run_id: str = ""
    manifest: str = ""
    step: str = ""


class AskHandle:
    """What an ask channel returns: ``withdrawn()`` is true once the person
    declined or the channel is gone (the wait stops early)."""

    def __init__(self, channel: str, withdrawn: Optional[Callable[[], bool]] = None):
        self.channel = channel
        self._withdrawn = withdrawn or (lambda: False)

    def withdrawn(self) -> bool:
        try:
            return bool(self._withdrawn())
        except Exception:                # noqa: BLE001
            return True


def message_for(req: SolveRequest, mode: str, view_url: Optional[str]) -> str:
    host = req.host or "the site"
    lines = [f"{host} asks for a human check (task {req.task_id[:12]}"
             f"{', step ' + req.step if req.step else ''})."]
    if mode == MODE_ATTACH:
        lines.append("Solve it yourself in your own browser window. AnyUse only watches "
                     "for the page to come back and then continues this task.")
    else:
        lines.append(f"Open this private view on this computer and solve it yourself: "
                     f"{view_url}")
        lines.append("It works once, only from this computer, and closes when the check "
                     "clears. AnyUse only passes your clicks and keys to that one page.")
    lines.append("No model sees the check. The cleared session serves only this task; "
                 "the next task on this host asks again.")
    return "\n".join(lines)


def _who(mode: str) -> Dict[str, str]:
    try:
        user = getpass.getuser()
    except Exception:                    # noqa: BLE001
        user = ""
    return {"os_user": user[:64],
            "surface": ("your attached browser" if mode == MODE_ATTACH else
                        "loopback live view (127.0.0.1)")}


class LiveSolver:
    """Runs one live solve for one task. ``ask(req, message, view_url)`` shows
    the request to the PERSON (elicitation, their terminal, or an inbox card
    plus notification) and returns an :class:`AskHandle` or ``None`` when no
    channel can reach them (then nothing is started)."""

    def __init__(self, *, ask: Callable[..., Optional[AskHandle]],
                 sessions: Optional[SolvedSessions] = None, policy: Optional[str] = None,
                 origins: Optional[str] = None, timeout_s: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 1.0,
                 quarantine: Optional[ModelQuarantine] = None,
                 view_factory: Callable[..., LiveView] = LiveView, inbox=None,
                 exceptions: Optional[ChallengeHostException] = None):
        self.ask, self.policy, self.origins = ask, policy, origins
        #: the narrow provider-host exception, granted for the solve's target only
        self.exceptions = exceptions or CHALLENGE_EXCEPTIONS
        #: optional ``human_inbox.Inbox``: every live solve gets a card for audit
        self.inbox = inbox
        self.sessions = sessions or SESSIONS
        self.timeout_s = float(timeout_s if timeout_s is not None
                               else hi.live_solve_timeout_s())
        self.clock, self.wall, self.sleep, self.poll_s = clock, wall, sleep, poll_s
        self.quarantine = quarantine or QUARANTINE
        self.view_factory = view_factory

    def applies(self, origin: Optional[str]) -> bool:
        hi.refuse_bench_live_solve(origin, self.policy)
        return hi.challenge_policy(self.policy, origin=origin,
                                   origins=self.origins) == hi.HUMAN_SOLVES_LIVE

    def solve(self, req: SolveRequest, page) -> Dict[str, Any]:
        """``page``: ``target_id``, ``mode`` (attach|headless), ``observe()`` and,
        headless only, ``send(target_id, method, params)`` and ``viewport``."""
        if not self.applies(req.origin):
            return {"status": "not_applicable",
                    "policy": hi.challenge_policy(self.policy, origin=req.origin,
                                                  origins=self.origins)}
        mode = MODE_HEADLESS if getattr(page, "mode", "") == MODE_HEADLESS else MODE_ATTACH
        target = str(page.target_id)
        if not challenge_evidence(page.observe()):
            return {"status": "not_challenge"}
        self.quarantine.enter(target)
        asked_at, outcome, view, pinned, channel = self.wall(), "expired", None, None, ""
        card_id = ""
        if mode == MODE_HEADLESS:
            # the page may now load the check from its provider's own hosts
            # (GET/HEAD, this target only); revoked below whatever happens
            self.exceptions.grant(target)
        try:
            view_url = None
            if mode == MODE_HEADLESS:
                pinned = PinnedPage(target, page.send,
                                    viewport=tuple(getattr(page, "viewport", (1280, 900))))
                view = self.view_factory(pinned, ttl_s=self.timeout_s, clock=self.clock,
                                         label=req.host)
                view_url = view.start()
            handle = self.ask(req, message_for(req, mode, view_url), view_url)
            if handle is None:
                outcome = "no_channel"
            else:
                channel = handle.channel
                card_id = str(getattr(handle, "card_id", "") or "")
                if not card_id and self.inbox is not None:
                    card_id = self.inbox.post_live(req, None)["id"]    # audit only
                deadline = self.clock() + self.timeout_s
                while True:
                    state = page_state(page.observe(), req.host)
                    if state == "cleared":
                        outcome = "cleared"
                        break
                    if state == "left_host":
                        outcome = "navigated_away"
                        break
                    if handle.withdrawn():
                        outcome = "declined"
                        break
                    if getattr(page, "closed", False):
                        outcome = "page_closed"          # the runner ended the page
                        break
                    if view is not None and not view.is_open():
                        outcome = "expired"
                        break
                    if self.clock() >= deadline:
                        outcome = "expired"
                        break
                    self.sleep(self.poll_s)
        finally:
            self.exceptions.revoke(target, outcome)
            if view is not None:
                view.close(outcome)
            if card_id and self.inbox is not None:
                self.inbox.finish_live(card_id, outcome)
        if outcome == "cleared":
            self.quarantine.leave(target)
            forget = pinned.forget_origin if pinned is not None else None
            self.sessions.grant(req.task_id, req.host, str(req.origin), forget)
        receipt = {
            "type": RECEIPT_TYPE, "task": req.task_id, "run_id": req.run_id,
            "manifest": req.manifest, "step": req.step, "host": req.host,
            "origin": req.origin, "policy": hi.HUMAN_SOLVES_LIVE, "mode": mode,
            "who": _who(mode), "asked_via": channel, "card": card_id, "asked_at": asked_at,
            "ended_at": self.wall(), "outcome": outcome,
            "inputs_relayed": pinned.relayed if pinned is not None else 0,
            "provider_requests_allowed": self.exceptions.allowed_count(target),
            "session_scope": "this task only",
            "models_on_challenge": "none",
        }
        return {"status": outcome, "receipt": receipt}


# ---------------------------------------------------------------------------
# ask channels (elicitation first, the person's terminal, then the inbox)
# ---------------------------------------------------------------------------

LIVE_SCHEMA = {"type": "object", "properties": {
    "done": {"type": "boolean", "title": "Keep waiting",
             "description": "Leave on while you solve it; AnyUse checks the page itself. "
                            "Turn off (or decline) to stop waiting."}},
    "required": ["done"]}


def elicitation_ask(elicitor) -> Callable[..., Optional[AskHandle]]:
    """Ask through the MCP client's elicitation prompt (shown to the person).

    The form runs on a worker thread; the solve does not wait for it. A
    decline, a cancel or ``done: false`` withdraws the request."""
    def ask(req: SolveRequest, message: str, view_url: Optional[str]) -> AskHandle:
        box: Dict[str, Any] = {}

        def run():
            try:
                box["reply"] = elicitor.ask(message, dict(LIVE_SCHEMA))
            except Exception:            # noqa: BLE001
                box["reply"] = {"action": "cancel"}

        threading.Thread(target=run, daemon=True, name="anyuse-live-ask").start()

        def withdrawn() -> bool:
            reply = box.get("reply")
            if reply is None:
                return False
            return not (reply.get("action") == "accept" and
                        (reply.get("content") or {}).get("done") is True)
        return AskHandle("mcp-elicitation", withdrawn)
    return ask


def tty_ask(gate) -> Callable[..., Optional[AskHandle]]:
    """A CLI run in the person's own terminal: the request goes to /dev/tty
    (never stdout), and only without agent markers (the terminal gate)."""
    def ask(req: SolveRequest, message: str, view_url: Optional[str]) -> Optional[AskHandle]:
        if gate.environment_refusal():
            return None
        try:
            with gate.open_tty() as tty:
                tty.write("\n" + message + "\n")
                tty.flush()
        except OSError:
            return None
        return AskHandle("cli")
    return ask


def inbox_ask(inbox, *, notify: Optional[Callable[[Mapping[str, Any]], Any]] = None
              ) -> Callable[..., Optional[AskHandle]]:
    """Scheduled and bulk runs: an inbox card plus a value-free notification;
    the run waits. The view URL is kept encrypted with the card and revealed
    only by ``anyuse inbox live <card>`` behind the terminal gate."""
    def ask(req: SolveRequest, message: str, view_url: Optional[str]) -> AskHandle:
        card = inbox.post_live(req, view_url)
        if notify is not None:
            try:
                notify(card)
            except Exception:            # noqa: BLE001
                pass

        def withdrawn() -> bool:
            cur = inbox.get(card["id"]) or {}
            return cur.get("status") in ("skipped", "cancelled", "closed")
        handle = AskHandle("inbox", withdrawn)
        handle.card_id = card["id"]      # type: ignore[attr-defined]
        return handle
    return ask


def first_channel(*asks: Optional[Callable[..., Optional[AskHandle]]]
                  ) -> Callable[..., Optional[AskHandle]]:
    """Try each channel in order; the first that reaches the person wins."""
    def ask(req, message, view_url):
        for a in asks:
            if a is None:
                continue
            handle = a(req, message, view_url)
            if handle is not None:
                return handle
        return None
    return ask


def default_solver(inbox, *, elicitor=None, gate=None, notify=None,
                   policy: Optional[str] = None) -> Optional[LiveSolver]:
    """The solver a surface attaches to its engine, or ``None`` when the
    policy is not ``human_solves_live``. Channels in order: the MCP client's
    elicitation prompt, the person's own terminal (a CLI run, gate-checked),
    then an inbox card plus a value-free notification while the run waits."""
    if hi._configured_policy(policy) != hi.HUMAN_SOLVES_LIVE:
        return None
    if notify is None:
        def notify(card):
            from anyuse.execution import notify as _notify
            return _notify.notify_card(card, elicitation_attached=False)
    ask = first_channel(elicitation_ask(elicitor) if elicitor is not None else None,
                        tty_ask(gate) if gate is not None else None,
                        inbox_ask(inbox, notify=notify))
    return LiveSolver(ask=ask, policy=policy, inbox=inbox)


# ---------------------------------------------------------------------------
# runner wiring: an async CDP page as the solver's (synchronous) page
# ---------------------------------------------------------------------------

def solver_applies(solver: Optional[LiveSolver], origin: Optional[str]) -> bool:
    """True only for a configured solver whose policy applies to ``origin``.
    A bench run never opens a solve (the refusal is swallowed here: the
    runner then stops on the challenge exactly as before)."""
    if solver is None:
        return False
    try:
        return bool(solver.applies(origin))
    except hi.LiveSolveRefused:
        return False


class AsyncPageBridge:
    """The solver runs in a worker thread; the page lives on an event loop.

    ``observe()`` and ``send()`` are marshalled onto ``loop``; ``send`` only
    ever reaches this bridge's own target (the PinnedPage allow-lists the
    methods). Nothing here feeds a model."""

    def __init__(self, loop, target_id: str, observe_async: Callable[[], Any],
                 call_async: Callable[[str, Dict[str, Any]], Any], *,
                 mode: str = MODE_HEADLESS, viewport: Tuple[int, int] = (1280, 900),
                 timeout_s: float = 30.0):
        self.loop = loop
        self.target_id = str(target_id)
        self.mode = mode
        self.viewport = viewport
        self._observe = observe_async
        self._call = call_async
        self.timeout_s = timeout_s
        #: set by the runner when the page is gone; the solve then ends
        self.closed = False

    def _run(self, coro):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout_s)

    def observe(self) -> Dict[str, Any]:
        try:
            return dict(self._run(self._observe()) or {})
        except Exception:                        # noqa: BLE001 - a dead page is "waiting"
            return {}

    def send(self, target_id: str, method: str, params: Dict[str, Any]) -> Any:
        if str(target_id) != self.target_id:
            raise RelayRefused("not this view's target")
        return self._run(self._call(method, params))


async def solve_async(solver: LiveSolver, req: SolveRequest, bridge: AsyncPageBridge
                      ) -> Dict[str, Any]:
    """Run one solve in a worker thread while the page's loop keeps running."""
    import asyncio
    return await asyncio.to_thread(solver.solve, req, bridge)
