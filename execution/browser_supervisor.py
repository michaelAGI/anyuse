"""Browser supervisor: keep the container browser answering, and when it does
not, fail the site cleanly as ``infrastructure`` (never as a product failure).

Why this exists (docs/TRAFFIC_POLICY.md "Browser infrastructure")
------------------------------------------------------------------------------
The headless containers "stopped answering" in several bench runs. The raw
logs show the SAME failure every time, and it is not a renderer crash:

* the host could no longer open a TCP connection to the container's DevTools
  port at all (``TimeoutError: [Errno 60] Connect call failed
  ('192.168.66.4x', 9222)`` in the loopback forwarder's log; a crashed Chrome
  in a live VM answers with a refusal, not a timeout), and the container
  runtime's own ``containerStats`` call into that VM failed as well;
* both arms' containers (two separate VMs) went silent within the same
  minute, 3 GB of memory did not prevent it, and the stops line up with
  another container network being torn down and re-created on the same host
  (``container system logs``: vmnet session released/allocated at 19:25:14,
  19:30:17 and 19:57:57, the three stops the log retention still covers).

The client code then turned an infrastructure outage into crashes and wrong
verdicts: blocking ``urllib`` calls inside the event loop, CDP timeouts and
``ConnectionClosedError: keepalive ping timeout`` escaping as tracebacks, a
dead browser's agent attempts filed as ``agent_failed``, and a resumed run
fetching a site's robots.txt before noticing that the browser was dead.

What this module provides (everything injectable; tests use fakes only)
----------------------------------------------------------------------
* :class:`InfrastructureFailure` and :func:`classify` - one category,
  ``infrastructure``, for "the browser, its VM or the path to it stopped
  answering". A site that ends this way is never counted as a product
  failure and is never re-run (so its host is never contacted beyond the
  budget it already used).
* :func:`probe` - a bounded health ping (``/json/version``), for use BEFORE a
  site is contacted and after any CDP timeout.
* :class:`PageWatch` - per-page crash detection from ``Inspector.targetCrashed``,
  ``Inspector.detached``, ``Target.targetCrashed`` and ``Target.targetDestroyed``.
* :func:`bounded` - evaluate expressions whose string result is truncated in
  the page (a multi-megabyte ``outerHTML`` is never shipped whole).
* :class:`BrowserSupervisor` - before/after each site: health check, optional
  container restart, stray page targets closed explicitly (one page per task).
* :class:`ContainerController` - the ``container`` CLI commands for a memory-
  and CPU-limited headless container with a published loopback port, restarted
  between sites. It only ever touches a container whose name starts with
  ``anyuse-browser-``, ``anyuse-webarch-`` or ``anyuse-webarch2-`` (the
  web-architecture bench's own browsers); any other container
  (a VM another agent uses) is refused.
"""
from __future__ import annotations

import asyncio
import errno
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

INFRASTRUCTURE = "infrastructure"

#: kinds of infrastructure failure (the ``kind`` of an InfrastructureFailure)
UNREACHABLE = "unreachable"          # /json/version or the websocket cannot be reached
STALLED = "stalled"                  # a CDP call got no answer in time
CONNECTION_LOST = "connection_lost"  # the websocket closed (keepalive or otherwise)
TARGET_CRASHED = "target_crashed"    # the page's renderer crashed
TARGET_GONE = "target_gone"          # the pinned page target no longer exists
TOO_MANY_TARGETS = "too_many_targets"

#: string results of a bounded read are cut to this many characters IN THE PAGE
MAX_DOM_CHARS = 8_000_000
MAX_TEXT_CHARS = 2_000_000
#: response bodies above this encoded size are not fetched from the browser
MAX_BODY_FETCH_BYTES = 5_000_000
#: page targets one run may hold before the supervisor closes strays
MAX_PAGE_TARGETS = 4

#: websocket keepalive: a ping every 20 s, and a whole minute for the pong, so
#: a briefly busy event loop is not mistaken for a dead browser (the loop
#: must never block that long; execution/web_runtime/live_bench.py runs attach in a thread)
WS_KEEPALIVE = {"ping_interval": 20.0, "ping_timeout": 60.0, "open_timeout": 10.0,
                "close_timeout": 5.0}

_NET_ERRNOS = {errno.ETIMEDOUT, errno.ECONNREFUSED, errno.EHOSTUNREACH,
               errno.ENETUNREACH, errno.ECONNRESET, errno.EPIPE, errno.ECONNABORTED,
               errno.EHOSTDOWN}

#: events that end a page: method -> kind
CRASH_EVENTS = {
    "Inspector.targetCrashed": TARGET_CRASHED,
    "Target.targetCrashed": TARGET_CRASHED,
    "Inspector.detached": TARGET_GONE,
    "Target.targetDestroyed": TARGET_GONE,
    "Target.detachedFromTarget": TARGET_GONE,
}


class InfrastructureFailure(RuntimeError):
    """The browser, its container or the path to it stopped answering.

    Never a verdict about the site or the product: a bench files the site as
    ``infrastructure`` (excluded from every rate, not re-run)."""

    category = INFRASTRUCTURE

    def __init__(self, kind: str, detail: str = ""):
        self.kind = str(kind)
        self.detail = str(detail)[:240]
        super().__init__(f"browser infrastructure ({self.kind}): {self.detail}")


def _names(exc: BaseException) -> List[str]:
    return [c.__name__ for c in type(exc).__mro__]


def is_connection_closed(exc: BaseException) -> bool:
    """A ``websockets`` ConnectionClosed (by name, so no import is needed)."""
    return any(n.startswith("ConnectionClosed") for n in _names(exc)) and \
        type(exc).__module__.startswith("websockets")


def _network_errno(exc: BaseException) -> bool:
    seen = 0
    cur: Optional[BaseException] = exc
    while cur is not None and seen < 5:
        if isinstance(cur, urllib.error.URLError) and isinstance(cur.reason, BaseException):
            cur = cur.reason
            seen += 1
            continue
        if isinstance(cur, ConnectionError):
            return True
        if isinstance(cur, OSError) and cur.errno in _NET_ERRNOS:
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def classify(exc: BaseException) -> Optional[str]:
    """The infrastructure kind of ``exc``, or ``None`` when it is not one.

    Definite: :class:`InfrastructureFailure`, a websockets ConnectionClosed, a
    socket-level network error (refused, unreachable, reset, connect timeout).
    A bare ``TimeoutError`` is NOT classified here (a slow site or a slow model
    raises it too): callers confirm it with :func:`probe` first
    (:func:`confirm`)."""
    if isinstance(exc, InfrastructureFailure):
        return exc.kind
    if is_connection_closed(exc):
        return CONNECTION_LOST
    if _network_errno(exc):
        return UNREACHABLE
    return None


def confirm(exc: BaseException, endpoint: str, *,
            http_get: Optional[Callable[[str, float], Any]] = None
            ) -> Optional[InfrastructureFailure]:
    """``exc`` as an InfrastructureFailure when it is one, or when it is a
    timeout and the browser does not answer a health ping; else ``None``."""
    kind = classify(exc)
    if kind:
        return exc if isinstance(exc, InfrastructureFailure) else \
            InfrastructureFailure(kind, f"{type(exc).__name__}: {exc}")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and endpoint:
        ok, why = probe(endpoint, http_get=http_get)
        if not ok:
            return InfrastructureFailure(UNREACHABLE, f"after {type(exc).__name__}: {why}")
    return None


# ---------------------------------------------------------------------------
# health ping
# ---------------------------------------------------------------------------

def http_get_json(url: str, timeout: float = 3.0) -> Any:
    """GET a DevTools HTTP endpoint (never follows a redirect, bounded)."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read(1_000_000).decode("utf-8", "replace") or "null")


def probe(endpoint: str, timeout: float = 3.0,
          http_get: Optional[Callable[[str, float], Any]] = None) -> Tuple[bool, str]:
    """``(True, browser)`` when ``/json/version`` answers within ``timeout``."""
    get = http_get or http_get_json
    try:
        info = get(f"{endpoint.rstrip('/')}/json/version", timeout)
    except Exception as exc:                      # noqa: BLE001 - any failure is "down"
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"
    if not isinstance(info, dict) or not info.get("webSocketDebuggerUrl"):
        return False, "no webSocketDebuggerUrl in /json/version"
    return True, str(info.get("Browser") or "browser")


async def aprobe(endpoint: str, timeout: float = 3.0,
                 http_get: Optional[Callable[[str, float], Any]] = None) -> Tuple[bool, str]:
    """:func:`probe` off the event loop (a blocking socket never stalls it)."""
    return await asyncio.to_thread(probe, endpoint, timeout, http_get)


async def browser_ws_url(endpoint: str, timeout: float = 10.0,
                         http_get: Optional[Callable[[str, float], Any]] = None) -> str:
    """The browser websocket from ``/json/version``, fetched off the event loop;
    any failure is :class:`InfrastructureFailure` (``unreachable``)."""
    get = http_get or http_get_json
    try:
        info = await asyncio.to_thread(get, f"{endpoint.rstrip('/')}/json/version", timeout)
    except Exception as exc:                      # noqa: BLE001
        raise InfrastructureFailure(UNREACHABLE, f"/json/version: {type(exc).__name__}: "
                                                 f"{str(exc)[:120]}") from exc
    url = str((info or {}).get("webSocketDebuggerUrl") or "") if isinstance(info, dict) else ""
    if not url:
        raise InfrastructureFailure(UNREACHABLE, "/json/version named no websocket")
    return url


def page_ws_url(endpoint: str, target_id: str) -> str:
    """The page websocket, BUILT from our own endpoint and the pinned id (never
    taken from what the browser reports)."""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", str(target_id or "")):
        raise InfrastructureFailure(TARGET_GONE, "not a target id")
    host = urllib.parse.urlsplit(endpoint).netloc
    return f"ws://{host}/devtools/page/{target_id}"


# ---------------------------------------------------------------------------
# per-page crash detection and bounded reads
# ---------------------------------------------------------------------------

class PageWatch:
    """Watches one pinned page target's events for a crash or its end."""

    def __init__(self, target_id: Optional[str] = None):
        self.target_id = target_id
        self.failed: Optional[InfrastructureFailure] = None

    def see(self, msg: dict) -> None:
        method = str((msg or {}).get("method") or "")
        kind = CRASH_EVENTS.get(method)
        if not kind or self.failed is not None:
            return
        p = (msg or {}).get("params") or {}
        if method.startswith("Target."):
            tid = p.get("targetId") or (p.get("targetInfo") or {}).get("targetId")
            if self.target_id and tid and tid != self.target_id:
                return                       # another target (a worker, an iframe)
        if method == "Inspector.detached" and \
                str(p.get("reason") or "") == "Render process gone.":
            kind = TARGET_CRASHED
        self.failed = InfrastructureFailure(kind, f"{method} {str(p)[:120]}")

    def check(self) -> None:
        if self.failed is not None:
            raise self.failed


def bounded(expr: str, max_chars: int) -> str:
    """An expression whose string result is cut to ``max_chars`` in the page."""
    n = int(max_chars)
    return (f"(()=>{{const v=({expr}); if (typeof v!=='string') return v; "
            f"return v.length>{n} ? v.slice(0,{n}) : v}})()")


DOM_EXPR = bounded("document.documentElement ? document.documentElement.outerHTML : ''",
                   MAX_DOM_CHARS)
TEXT_EXPR = bounded("document.body ? document.body.innerText : ''", MAX_TEXT_CHARS)


# ---------------------------------------------------------------------------
# the container
# ---------------------------------------------------------------------------

CONTAINER_PREFIX = "anyuse-browser-"
#: every prefix a supervisor may touch: the benches' own headless browsers only
#: (``anyuse-webarch-`` and ``anyuse-webarch2-``: the web-architecture bench)
CONTAINER_PREFIXES = (CONTAINER_PREFIX, "anyuse-webarch-", "anyuse-webarch2-")
_NAME_RE = re.compile(r"^anyuse-(browser|webarch|webarch2)-[a-z0-9][a-z0-9-]{0,30}$")
_PUBLISH_RE = re.compile(r"^127\.0\.0\.1:([0-9]{2,5}):9222$")


@dataclass
class ContainerSpec:
    """One headless browser container (Apple ``container`` CLI)."""
    name: str
    image: str = "chromedp/headless-shell:latest"
    memory: str = "2g"
    cpus: int = 2
    #: loopback-only published port (``127.0.0.1:PORT:9222``); the endpoint
    #: stays the same across restarts, so no forwarder process is needed
    publish: str = "127.0.0.1:9345:9222"
    #: appended to the image's own entrypoint flags
    chrome_args: Tuple[str, ...] = ("--disable-dev-shm-usage", "--renderer-process-limit=4",
                                    "--disable-background-networking")

    def check(self) -> None:
        if not _NAME_RE.match(self.name or ""):
            raise ValueError(f"container name must start with one of {CONTAINER_PREFIXES!r}; "
                             f"no other container is ever touched")
        if not re.fullmatch(r"[0-9]{1,3}[mg]", self.memory or ""):
            raise ValueError("memory must be like 2g or 1536m")
        if not 1 <= int(self.cpus) <= 8:
            raise ValueError("cpus must be 1-8")
        if self.publish and not _PUBLISH_RE.match(self.publish):
            raise ValueError("publish must be 127.0.0.1:PORT:9222 (loopback only)")
        for a in self.chrome_args:
            if not re.fullmatch(r"--[a-z0-9-]+(=[A-Za-z0-9,._-]+)?", a) or \
                    a.startswith(("--remote-debugging-address", "--user-data-dir")):
                raise ValueError(f"chrome arg refused: {a[:60]}")

    @property
    def endpoint(self) -> str:
        m = _PUBLISH_RE.match(self.publish or "")
        return f"http://127.0.0.1:{m.group(1)}" if m else ""


class ContainerController:
    """Start, stop and restart ONE ``anyuse-browser-*`` / ``anyuse-webarch-*`` container.

    ``run(argv) -> CompletedProcess`` is injected in tests; nothing here is
    ever called by the offline suite with the real CLI."""

    def __init__(self, spec: ContainerSpec, *, run: Optional[Callable] = None,
                 cli: str = "container", sleep: Callable[[float], None] = time.sleep,
                 http_get: Optional[Callable[[str, float], Any]] = None):
        spec.check()
        self.spec = spec
        self._run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True,
                                                        timeout=120))
        self.cli = cli
        self.sleep = sleep
        self.http_get = http_get
        self.restarts = 0
        self.log: List[str] = []

    def run_argv(self) -> List[str]:
        s = self.spec
        argv = [self.cli, "run", "-d", "--rm", "--name", s.name, "-m", s.memory,
                "-c", str(int(s.cpus))]
        if s.publish:
            argv += ["-p", s.publish]
        return argv + [s.image] + list(s.chrome_args)

    def stop_argv(self) -> List[str]:
        return [self.cli, "stop", self.spec.name]

    def stop(self) -> None:
        self._run(self.stop_argv())
        self.log.append(f"stopped {self.spec.name}")

    def start(self, wait_s: float = 30.0) -> None:
        res = self._run(self.run_argv())
        if getattr(res, "returncode", 0) not in (0, None):
            raise InfrastructureFailure(UNREACHABLE, f"container run failed: "
                                                     f"{str(getattr(res, 'stderr', ''))[:160]}")
        self.log.append(f"started {self.spec.name}")
        self.wait_healthy(wait_s)

    def restart(self, wait_s: float = 30.0) -> None:
        self.stop()
        self.sleep(2.0)
        self.start(wait_s)
        self.restarts += 1

    def wait_healthy(self, wait_s: float) -> None:
        endpoint = self.spec.endpoint
        if not endpoint:
            return
        deadline = time.monotonic() + wait_s
        why = ""
        while True:
            ok, why = probe(endpoint, http_get=self.http_get)
            if ok:
                return
            if time.monotonic() >= deadline:
                raise InfrastructureFailure(UNREACHABLE, f"fresh container not healthy: {why}")
            self.sleep(1.0)


# ---------------------------------------------------------------------------
# the supervisor
# ---------------------------------------------------------------------------

@dataclass
class SiteHealth:
    site: str
    ok: bool = True
    why: str = ""
    restarted: bool = False
    closed_targets: List[str] = field(default_factory=list)


class BrowserSupervisor:
    """Brackets every site of a run.

    ``before_site``: restart the container when configured, then a health
    ping; a browser that does not answer fails the site as ``infrastructure``
    BEFORE any request reaches the site (robots.txt included).
    ``after_site``: close every page target the site left open (one page per
    task, closed explicitly), so a long run never accumulates tabs."""

    def __init__(self, endpoint: str, *, container: Optional[ContainerController] = None,
                 restart_between_sites: bool = False, max_page_targets: int = MAX_PAGE_TARGETS,
                 http_get: Optional[Callable[[str, float], Any]] = None,
                 log: Callable[[str], None] = lambda _m: None):
        self.endpoint = endpoint.rstrip("/")
        self.container = container
        self.restart_between_sites = bool(restart_between_sites and container is not None)
        self.max_page_targets = int(max_page_targets)
        self.http_get = http_get or http_get_json
        self.log = log
        self.history: List[SiteHealth] = []
        self._baseline: set = set()

    def healthy(self) -> Tuple[bool, str]:
        return probe(self.endpoint, http_get=self.http_get)

    def page_targets(self) -> List[dict]:
        try:
            rows = self.http_get(f"{self.endpoint}/json/list", 3.0)
        except Exception:                          # noqa: BLE001
            return []
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("type") == "page"]

    def close_target(self, target_id: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", str(target_id or "")):
            return False
        try:
            self.http_get(f"{self.endpoint}/json/close/{target_id}", 3.0)
            return True
        except Exception:                          # noqa: BLE001 - "Target is closing" is text
            return True

    def before_site(self, site: str) -> SiteHealth:
        h = SiteHealth(site)
        if self.restart_between_sites:
            try:
                self.container.restart()
                h.restarted = True
            except Exception as exc:               # noqa: BLE001
                h.ok, h.why = False, f"restart failed: {exc}"[:200]
        if h.ok:
            ok, why = self.healthy()
            if not ok and self.container is not None and not h.restarted:
                self.log(f"browser not answering before {site} ({why}); restarting "
                         f"{self.container.spec.name}")
                try:
                    self.container.restart()
                    h.restarted = True
                    ok, why = self.healthy()
                except Exception as exc:           # noqa: BLE001
                    ok, why = False, f"restart failed: {exc}"
            h.ok, h.why = ok, ("" if ok else why)
        if h.ok:
            self._baseline = {t.get("id") for t in self.page_targets()}
        self.history.append(h)
        return h

    def require(self, site: str) -> SiteHealth:
        """:meth:`before_site`, raising :class:`InfrastructureFailure` when down."""
        h = self.before_site(site)
        if not h.ok:
            raise InfrastructureFailure(UNREACHABLE, f"before {site}: {h.why}")
        return h

    def after_site(self, site: str) -> List[str]:
        """Close page targets opened during the site and still open."""
        closed = []
        for t in self.page_targets():
            tid = str(t.get("id") or "")
            if tid and tid not in self._baseline and self.close_target(tid):
                closed.append(tid)
        if self.history and self.history[-1].site == site:
            self.history[-1].closed_targets = closed
        if closed:
            self.log(f"{site}: closed {len(closed)} page target(s) left open")
        return closed

    def check_targets(self, keep: Sequence[str] = ()) -> List[str]:
        """More than ``max_page_targets`` pages: close the ones not in ``keep``."""
        pages = self.page_targets()
        if len(pages) <= self.max_page_targets:
            return []
        closed = []
        for t in pages:
            tid = str(t.get("id") or "")
            if tid and tid not in keep and self.close_target(tid):
                closed.append(tid)
        return closed
