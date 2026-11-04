"""Courtesy checks before a compiled rung is made the preferred way to run a
workflow: what the site's ``robots.txt`` says, and whether a human has
acknowledged that site's terms.

**This is a courtesy mechanism, not legal advice and not a compliance control.**
``robots.txt`` governs crawlers; a compiled workflow replays a request the user
already made through the site's own UI, which is a different thing, and a site's
terms may permit or forbid either regardless of what the file says. What this
module does is make the question *visible* and record the answer, so that
persisting a compiled rung is a decision somebody made rather than something
that happened quietly.

Default posture: a host that has NOT been acknowledged still COMPILES and is
still reported in full — the template, the proof, everything — but is **not
persisted as the preferred implementation**. Nothing is hidden; the rung simply
does not become the default way the workflow runs until a human says so.

Acknowledgement lives in ``~/.anyuse/api_hosts.json`` (override with
``ANYUSE_API_HOSTS_FILE``), a plain map of host → record, so it is auditable and
editable by hand::

    {"api.example.org": {"acknowledged": true, "by": "…", "at": "2026-11-03T…",
                   "note": "public JSON API, documented"}}

The CLI records it inline: a command that runs a proof installs an asker
(:func:`asking_hosts`). After ``robots.txt`` is read and before the proof
replays the compiled request against a host that is refused only for want of an
acknowledgement, :func:`ask_for_host` asks once; a yes writes the same record
``anyuse workflow acknowledge-host`` writes, with the note
:data:`CONFIRMED_AT_COMPILE`. Under ``--dry-run`` / ``--no-persist`` the yes
holds for that command only and nothing is written. A ``robots.txt`` disallow is
never asked about. Without an asker (MCP, benches, background work) nothing is
asked.
"""
from __future__ import annotations

import contextvars
import datetime
import json
import logging
import os
import time
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from anyuse.paths import anyuse_data_path

logger = logging.getLogger(__name__)

#: User-agent the robots check asks about. A compiled replay is not a crawler,
#: but asking under a named agent is more honest than asking as "*".
ROBOTS_AGENT = "anyuse-compiled-workflow"

_ROBOTS_CACHE: Dict[str, Any] = {}

#: How long a cached ``robots.txt`` is reused before it is fetched again. One
#: day: long enough that a session of compiling does not re-ask, short enough
#: that a site which changes its mind is honoured the next day. Override with
#: ``ANYUSE_ROBOTS_TTL_S``; ``0`` disables the on-disk cache entirely.
ROBOTS_TTL_S = 86_400
#: Sentinel meaning "this host serves no robots.txt" — worth caching too, since
#: a 404 costs the site a request just like a hit does. Another 4xx is cached as
#: ``_CACHE_ABSENT + ":4xx:<status>"`` (no rules, the status kept as the reason).
_CACHE_ABSENT = "\x00absent"

#: The robots.txt status rules: RFC 9309 section 2.3.1 (docs/TRAFFIC_POLICY.md):
#: * 2xx: the file's rules; 404 and EVERY other 4xx (401 and 403 included, 429 excluded): "unavailable", NO rules -
#:   the site is allowed, the status is the recorded reason (``robots_unavailable_4xx``);
#: * 429: back off and retry, at most :data:`ROBOTS_429_RETRIES` times (each wait the Retry-After header, at most
#:   :data:`ROBOTS_429_MAX_WAIT_S`); still 429: refused for this run (``robots_rate_limited_429``), never cached on disk;
#: * 5xx and a network failure (a timeout, a refused or reset connection, a TLS or DNS failure): "unreachable",
#:   COMPLETE DISALLOW for this run (``robots_unreachable``), never cached on disk;
#: * a redirect is followed as RFC 9309 section 2.3.1.2 says: at most
#:   :data:`ROBOTS_MAX_REDIRECTS` consecutive hops, to ANY host - each hop one GET of the robots file (counted in the
#:   site's budget where the caller passes ``on_hop``); a hop to a never-contacted host (NEVER_CONTACT, and government /
#:   .int / europa / military with ANYUSE_REFUSE_GOV_HOSTS) or a non-public address, a loop, or more than 5 hops is UNREACHABLE: complete
#:   disallow, typed (:class:`RobotsUnreachable`), the redirect chain in the note. A file reached by redirects is obeyed
#:   for the host that was asked.
ROBOTS_429_RETRIES = 2
ROBOTS_429_WAIT_S = 2.0
ROBOTS_429_MAX_WAIT_S = 10.0
ROBOTS_MAX_REDIRECTS = 5
#: the wait between 429 retries (injectable in tests)
_sleep = time.sleep
#: host -> the reason a verdict that is not the file's own was reached (a 4xx, an unreachable host, a 429)
_ROBOTS_NOTES: Dict[str, str] = {}


class RobotsHTTPStatus(Exception):
    """robots.txt answered a status that is not 2xx, 3xx or 404 (``status``; ``retry_after`` in seconds or None)."""

    def __init__(self, url: str, status: int, retry_after: Optional[float] = None):
        super().__init__(f"{url} answered HTTP {status}")
        self.status = int(status)
        self.retry_after = retry_after


class RobotsUnreachable(Exception):
    """RFC 9309 section 2.3.1.2: the redirects of a robots.txt GET did not reach a file -
    ``reason`` one of ``redirect_loop``, ``too_many_redirects``, ``redirect_to_refused_host`` (NEVER_CONTACT, and
    government (ANYUSE_REFUSE_GOV_HOSTS)), ``redirect_to_refused_destination`` (a non-public address, not
    http(s)), ``redirect_not_sent`` (the caller's hop gate: the site's budget, a bench's screen); ``chain`` the URLs
    asked, in order. Unreachable: a complete disallow for this run."""

    def __init__(self, reason: str, chain: List[str], detail: str = ""):
        self.reason, self.chain, self.detail = reason, list(chain), detail
        super().__init__(f"{reason}{': ' + detail if detail else ''}")


#: the caller's per-hop gate for the robots fetch in progress (``fetch_robots(on_hop=...)``): ``gate(hop_url) -> ""``
#: to send the hop, else why not. A context variable, so a bench's own fetcher that calls :func:`_default_fetch` inside
#: :func:`fetch_robots` still has every hop counted.
_HOP_GATE: "contextvars.ContextVar[Optional[Any]]" = contextvars.ContextVar("anyuse_robots_hop_gate", default=None)
#: robots.txt URL -> the redirect chain of its last fetch (read and cleared by :func:`fetch_robots` for its note)
ROBOTS_CHAINS: Dict[str, List[str]] = {}
#: robots.txt URL -> what made its last fetch fail: "status" (the site answered 5xx / 429 / 4xx), "redirect" (its
#: redirects reached no file) or "network" (no answer at all)
ROBOTS_FAILURES: Dict[str, str] = {}


def robots_status_of(exc: BaseException) -> Optional[int]:
    """The HTTP status a failed robots.txt fetch carries (this module's :class:`RobotsHTTPStatus`, or an injected
    fetcher's ``httpx.HTTPStatusError``), else None (a network failure)."""
    st = getattr(exc, "status", None)
    if st is None:
        st = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(st) if st is not None else None
    except (TypeError, ValueError):
        return None


def robots_status_rule(status: Optional[int]) -> str:
    """The RFC 9309 rule for a robots.txt answer's HTTP status (None: no answer - a network failure): ``rules`` (2xx),
    ``absent`` (404), ``none`` (any other 4xx except 429: no rules), ``rate_limited`` (429) or ``unreachable`` (5xx, no
    answer, anything else: complete disallow). The one place the status is read; every robots fetcher uses it."""
    if status is None:
        return "unreachable"
    st = int(status)
    if 200 <= st < 300:
        return "rules"
    if st == 404:
        return "absent"
    if st == 429:
        return "rate_limited"
    if 400 <= st < 500:
        return "none"
    return "unreachable"


def robots_failure_kind(exc: BaseException) -> str:
    """``none`` (a 4xx other than 429: no rules), ``rate_limited`` (429), ``unreachable`` (5xx or a network failure)
    or ``policy`` (the pre-send policy refused the first GET: a refused destination, a bench's host screen). A redirect
    that reaches no file (:class:`RobotsUnreachable`) is ``unreachable``."""
    if isinstance(exc, RobotsUnreachable):
        return "unreachable"
    if isinstance(exc, PermissionError):
        return "policy"
    rule = robots_status_rule(robots_status_of(exc))
    return "none" if rule == "absent" else rule if rule in ("none", "rate_limited") else "unreachable"


def _absent_marker(status: int) -> str:
    return _CACHE_ABSENT if int(status) == 404 else f"{_CACHE_ABSENT}:4xx:{int(status)}"


def _is_absent(cached: Optional[str]) -> bool:
    return isinstance(cached, str) and cached.startswith(_CACHE_ABSENT)


def _absent_note(cached: str, robots_url: str) -> str:
    if cached == _CACHE_ABSENT:
        return f"no robots.txt at {robots_url}"
    return (f"robots_unavailable_4xx: {robots_url} answered HTTP {cached.rsplit(':', 1)[-1]} "
            f"(RFC 9309: unavailable, no rules)")


def _via(chain: Optional[List[str]]) -> str:
    """`` (redirects: a -> b -> c)`` for a fetch that was redirected, else ``""``."""
    return f" (redirects: {' -> '.join(c[:120] for c in chain)})" if chain and len(chain) > 1 else ""


def fetch_robots(robots_url: str, fetcher=None, on_hop=None) -> Tuple[str, Optional[str], str]:
    """ONE robots.txt GET under the status rules above (every caller - the product's gate, the compile, the MCP route,
    the fallback, the benches - goes through it): ``("rules", text, "")``, ``("rules", None, "")`` for a 404 (no file),
    ``("none", <cache marker>, note)`` for another 4xx (no rules), ``("disallow", None, note)`` for a 429 that stays,
    a 5xx, a network failure or a redirect that reached no file, ``("policy", None, note)`` when the pre-send policy
    refused the first GET (a refused destination: fail closed as before). ``fetcher(robots_url) -> text | None`` is injectable;
    it raises for any other status (:class:`RobotsHTTPStatus` or an ``httpx.HTTPStatusError``).

    Redirects (RFC 9309 section 2.3.1.2): followed by :func:`_default_fetch`, up to 5 hops
    to any host; ``on_hop(hop_url) -> "" | why not`` is asked before each hop's GET (the product's gate counts it in the
    site's budget); a refused hop, a loop or a 6th redirect is :class:`RobotsUnreachable` -> ``("disallow", ...)``.
    Every note of a redirected fetch names its chain (``... (redirects: a -> b)``)."""
    err: Optional[BaseException] = None
    text: Optional[str] = None
    ROBOTS_CHAINS.pop(robots_url, None)
    token = _HOP_GATE.set(on_hop)
    try:
        for attempt in range(ROBOTS_429_RETRIES + 1):
            try:
                text = fetcher(robots_url) if fetcher else _default_fetch(robots_url)
                err = None
                break
            except Exception as exc:                         # noqa: BLE001 - classified below (RFC 9309)
                err = exc
                if robots_failure_kind(exc) == "rate_limited" and attempt < ROBOTS_429_RETRIES:
                    wait = getattr(exc, "retry_after", None)
                    _sleep(min(float(wait) if wait is not None else ROBOTS_429_WAIT_S, ROBOTS_429_MAX_WAIT_S))
                    continue
                break
    finally:
        _HOP_GATE.reset(token)
    chain = ROBOTS_CHAINS.get(robots_url)
    if isinstance(err, RobotsUnreachable):
        chain = err.chain or chain
        ROBOTS_CHAINS[robots_url] = list(chain or [])
    via = _via(chain)
    if err is None:
        return "rules", text, ""
    kind = robots_failure_kind(err)
    code = robots_status_of(err)
    # what answered - the site (an HTTP status, a redirect chain) or nothing (a network failure, possibly ours)
    ROBOTS_FAILURES[robots_url] = ("redirect" if isinstance(err, RobotsUnreachable) else
                                   "status" if code is not None else "network")
    if kind == "none":
        marker = _absent_marker(code or 404)
        return "none", marker, _absent_note(marker, robots_url) + via
    if kind == "rate_limited":
        return "disallow", None, (f"robots_rate_limited_429: {robots_url} answered HTTP 429 after {ROBOTS_429_RETRIES} "
                                  f"retries{via} (refused for this run)")
    if isinstance(err, RobotsUnreachable):
        return "disallow", None, (f"robots_unreachable: {robots_url}: {err}{via} (RFC 9309: unreachable, complete "
                                  f"disallow)")
    if kind == "unreachable":
        return "disallow", None, (f"robots_unreachable: could not read {robots_url}: {err}{via} (RFC 9309: unreachable, "
                                  f"complete disallow)" if code is None else
                                  f"robots_unreachable: {robots_url} answered HTTP {code}{via} (RFC 9309: unreachable, "
                                  f"complete disallow)")
    return "policy", None, f"could not read {robots_url}: {err}{via}"


def robots_read(robots_url: str, fetcher=None, on_hop=None) -> Tuple[str, Optional[str], str, Dict[str, Any]]:
    """(The MCP route used to read robots.txt again on every call - two search-provider
    files with a redirect hop each - and spent the call's ceiling.) The ONE robots.txt read with the ONE cache - the
    on-disk store and its TTL (``ANYUSE_ROBOTS_TTL_S``, at most 24 hours, RFC 9309 section 2.4), else
    :func:`fetch_robots`, whose answer is stored: the file (with its redirect chain and landing URL), a 404 or another
    4xx (no rules), an unreachable verdict (complete disallow, read again after :data:`ROBOTS_UNREACHABLE_TTL_S`); a
    pre-send policy refusal is never stored. ``(kind, text, note, info)`` as ``fetch_robots`` plus ``info``
    (``{"cached", "landed", "chain"}``). A cached read sends nothing - its redirect hops included. No HTTP cache headers
    are read (this module never has)."""
    host = urllib.parse.urlsplit(robots_url).netloc
    entry = _cache_entry(host)
    if entry is not None:
        body = entry["body"]
        info = {"cached": True, "landed": str(entry.get("landed") or ""), "chain": list(entry.get("chain") or [])}
        if _cache_unreachable(body):
            return "disallow", None, str(entry.get("note") or f"robots_unreachable: {robots_url} (cached)"), info
        if body == _CACHE_ABSENT:
            return "rules", None, "", info
        if _is_absent(body):
            return "none", body, _absent_note(body, robots_url), info
        return "rules", body, "", info
    ROBOTS_FAILURES.pop(robots_url, None)
    kind, text, note = fetch_robots(robots_url, fetcher, on_hop=on_hop)
    chain = list(ROBOTS_CHAINS.pop(robots_url, None) or [])
    landed = ROBOTS_REDIRECTS.get(robots_url, "")
    failure = ROBOTS_FAILURES.pop(robots_url, "")
    if kind == "rules":
        _cache_store(host, text if text is not None else _CACHE_ABSENT, chain=chain, landed=landed)
    elif kind == "none":
        _cache_store(host, text or _absent_marker(404), chain=chain, landed=landed)
    elif kind == "disallow" and failure in ("status", "redirect"):
        # the SITE's answer (a 5xx, a 429 that stayed, redirects that reached no file) is cached; a failure to reach it
        # at all (DNS, a refused connection, no network - possibly this machine's own) is not: it stays this process's
        # complete disallow, as before, and the next process reads again
        _cache_store(host, _CACHE_UNREACHABLE, note=note, chain=chain, ttl=ROBOTS_UNREACHABLE_TTL_S)
    return kind, text, note, {"cached": False, "landed": landed, "chain": chain}


class RobotsRules:
    """RFC 9309 section 2.2 rule matching (the standard library's ``RobotFileParser`` matches
    paths by plain prefix in file order - no ``*`` / ``$`` wildcards, first match - so a CMS robots file with
    ``Allow: /core/*.js?`` never applied and the site's own scripts were refused under ``Disallow: /core/``, while a
    wildcard ``Disallow: /*/print`` never applied either):

    * groups by user-agent: the group(s) whose value equals this crawler's product token or a leading
      hyphen-delimited part of it (case-insensitive, :meth:`product_tokens`; never a substring) are combined; else the
      ``*`` group(s); else no rules (allowed);
    * ``Allow`` / ``Disallow`` patterns with ``*`` (any sequence) and a final ``$`` (end of the path), matched against
      the path and query, percent-decoded on both sides;
    * the LONGEST matching pattern wins; on a tie ``Allow`` wins; an empty ``Disallow`` is no rule;
    * ``/robots.txt`` itself is always allowed.

    Also read: ``Crawl-delay`` per group (not RFC 9309; the benches' pacing honours it - :meth:`crawl_delay`)."""

    def __init__(self) -> None:
        self.groups: List[Tuple[List[str], List[Tuple[bool, str]]]] = []
        #: id(group) -> its Crawl-delay in seconds
        self.delays: Dict[int, float] = {}

    def parse(self, lines) -> None:
        current: Optional[Tuple[List[str], List[Tuple[bool, str]]]] = None
        last_was_agent = False
        for raw in lines:
            line = str(raw).split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key in ("user-agent", "useragent"):
                if current is None or not last_was_agent:
                    current = ([], [])
                    self.groups.append(current)
                current[0].append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if current is None or (key == "disallow" and not value):
                    continue
                if value:
                    current[1].append((key == "allow", urllib.parse.unquote(value)))
            elif key == "crawl-delay":
                last_was_agent = False
                if current is not None:
                    try:
                        self.delays[id(current)] = max(0.0, float(value))
                    except ValueError:
                        pass
            else:
                last_was_agent = False

    @staticmethod
    def product_tokens(agent: str) -> Tuple[str, ...]:
        """The names a group's ``User-agent`` value is compared with (RFC 9309 section 2.2.1): the product token
        (the agent string up to its first ``/`` or space, lower-cased) and each of its leading hyphen-delimited parts
        (``anyuse-compiled-workflow`` -> ``anyuse-compiled-workflow``, ``anyuse-compiled``, ``anyuse``), so a group
        written for the product family applies too. Never a part from the middle or the end of the token."""
        token = re.split(r"[/\s]", str(agent or "").strip().lower(), maxsplit=1)[0]
        parts = token.split("-")
        if not token or not all(parts):
            return (token,) if token else ()
        return tuple("-".join(parts[:n]) for n in range(len(parts), 0, -1))

    def _groups_for(self, agent: str) -> List[Tuple[List[str], List[Tuple[bool, str]]]]:
        names = set(self.product_tokens(agent))
        mine = [g for g in self.groups
                if any(tok != "*" and tok and re.split(r"[/\s]", tok, maxsplit=1)[0] in names for tok in g[0])]
        return mine or [g for g in self.groups if "*" in g[0]]

    def _rules_for(self, agent: str) -> List[Tuple[bool, str]]:
        return [r for g in self._groups_for(agent) for r in g[1]]

    def crawl_delay(self, agent: str) -> Optional[float]:
        """The ``Crawl-delay`` of the group(s) this crawler obeys (the largest), else None."""
        ds = [self.delays[id(g)] for g in self._groups_for(agent) if id(g) in self.delays]
        return max(ds) if ds else None

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        end = pattern.endswith("$")
        body = pattern[:-1] if end else pattern
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return re.match(rx + ("$" if end else ""), path) is not None

    def can_fetch(self, agent: str, url: str) -> bool:
        parts = urllib.parse.urlsplit(str(url or ""))
        path = urllib.parse.unquote(parts.path or "/") + (f"?{urllib.parse.unquote(parts.query)}" if parts.query else "")
        if (parts.path or "/") == "/robots.txt":
            return True
        best: Optional[Tuple[int, bool]] = None
        for allow, pattern in self._rules_for(agent):
            if self._matches(pattern, path):
                cand = (len(pattern), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]


def _disallow_all() -> "RobotsRules":
    parser = RobotsRules()
    parser.parse(["User-agent: *", "Disallow: /"])
    return parser


def robots_cache_file() -> str:
    return os.environ.get("ANYUSE_ROBOTS_CACHE_FILE") or str(
        anyuse_data_path("robots_cache.json"))


#: RFC 9309 section 2.4: a cached robots.txt is used for at most 24 hours (the setting may only shorten it)
ROBOTS_CACHE_MAX_S = 86_400
#: an UNREACHABLE verdict (a 5xx, a network failure, a 429 that stayed, a redirect that reached no
#: file) is cached as a complete disallow in the same store, and the file is read again after this many seconds - RFC
#: 9309 sets only maxima (section 2.4: 24 hours for a cached copy; section 2.3.1.4: complete disallow while unreachable,
#: "unavailable" only after a reasonably long period such as 30 days), so the shorter rule is ours: retry after 1 hour
ROBOTS_UNREACHABLE_TTL_S = 3_600
#: the cache marker of an unreachable verdict (its note and redirect chain are kept in the entry)
_CACHE_UNREACHABLE = "\x00unreachable"


def _ttl() -> int:
    from anyuse.settings import get as get_setting
    return max(0, min(int(get_setting("ANYUSE_ROBOTS_TTL_S")), ROBOTS_CACHE_MAX_S))


def _cache_read() -> Dict[str, dict]:
    path = robots_cache_file()
    if not _ttl() or not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _cache_entry(host: str) -> Optional[Dict[str, Any]]:
    """The fresh cache entry of ``host`` (``{"at", "body"[, "chain", "landed", "note", "ttl"]}``), or None: an entry
    lives its own ``ttl`` (an unreachable verdict: :data:`ROBOTS_UNREACHABLE_TTL_S`), never longer than the setting."""
    entry = _cache_read().get(host)
    if not isinstance(entry, dict) or not isinstance(entry.get("body"), str):
        return None
    try:
        age = time.time() - float(entry.get("at", 0))
        life = min(float(entry.get("ttl") or _ttl()), float(_ttl()))
    except (TypeError, ValueError):
        return None
    return entry if age <= life else None


def _cache_load(host: str) -> Optional[str]:
    """The cached robots.txt body for ``host`` (or a marker: absent, a 4xx, unreachable), or None if absent/stale."""
    entry = _cache_entry(host)
    return entry["body"] if entry is not None else None


def _cache_unreachable(cached: Optional[str]) -> bool:
    return isinstance(cached, str) and cached.startswith(_CACHE_UNREACHABLE)


def _cache_store(host: str, body: str, **extra: Any) -> None:
    if not _ttl():
        return
    path = robots_cache_file()
    data = _cache_read()
    data[host] = dict({"at": time.time(), "body": body}, **{k: v for k, v in extra.items() if v})
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError as exc:                 # a cache that cannot be written is
        logger.debug("robots cache not written: %s", exc)   # not an error


# ---------------------------------------------------------------------------
# robots policy — a product setting, not a constant
# ---------------------------------------------------------------------------

#: Honour ``robots.txt``: a disallowed path is never compiled into a preferred
#: rung and never replayed through the api rung. The default, and the only
#: setting AnyUse's own benchmarks and recordings ever run under.
ROBOTS_RESPECT = "respect"
#: Ignore ``robots.txt`` everywhere. Never the default, never implied by
#: anything else, and the user owns the consequences.
ROBOTS_IGNORE = "ignore"
ROBOTS_POLICIES = (ROBOTS_RESPECT, ROBOTS_IGNORE)
#: ``ANYUSE_ROBOTS`` = ``respect`` (the default: RFC 9309) | ``user_initiated`` (a lookup or a
#: compile the user / caller explicitly started - an MCP tool call, a CLI run or compile - may fetch paths robots.txt
#: disallows; breadth exploration and unattended / background work still respect it) | ``ignore`` (robots.txt never
#: applied). In EVERY mode: the honest user agent, pacing, budgets and per-host caps, the NEVER_CONTACT refusals (and
#: the government ones with ANYUSE_REFUSE_GOV_HOSTS), the third-party rules, no CAPTCHA or bot-wall bypass; robots.txt
#: is still fetched and counted, and an overridden verdict is recorded ("robots would disallow") so a run says what
#: it overrode. Under ``respect``, a signed-in task the user started (:data:`ROBOTS_SIGNED_IN`) is not held to
#: robots.txt on the site it signs in to; background work is.
ROBOTS_SETTING = "ANYUSE_ROBOTS"
ROBOTS_USER_INITIATED = "user_initiated"
ROBOTS_MODES = (ROBOTS_RESPECT, ROBOTS_USER_INITIATED, ROBOTS_IGNORE)
#: who started the work in progress: "" (unattended / background: the default), ``user:<surface>`` (an MCP tool call, a
#: CLI run) or ``auto:<kind>`` (breadth exploration or background work inside a user's call). Set only by
#: :func:`user_initiated` / :func:`unattended`; a run's robots gate reads it ONCE, when it is built.
_INITIATION: "contextvars.ContextVar[str]" = contextvars.ContextVar("anyuse_robots_initiation", default="")


def robots_mode() -> str:
    """The robots setting: ``ANYUSE_ROBOTS``; an unrecognised value is ``respect`` (a typo
    never turns the check off). The older global switch ``ANYUSE_ROBOTS_POLICY=ignore`` still means ``ignore``."""
    from anyuse.settings import get as get_setting
    try:
        raw = str(get_setting(ROBOTS_SETTING) or ROBOTS_RESPECT).strip().lower()
    except Exception:                                  # noqa: BLE001 - unreadable: respect
        raw = ROBOTS_RESPECT
    mode = raw if raw in ROBOTS_MODES else ROBOTS_RESPECT
    if mode == ROBOTS_RESPECT:
        try:
            legacy = str(get_setting("ANYUSE_ROBOTS_POLICY") or "").strip().lower()
        except Exception:                              # noqa: BLE001
            legacy = ""
        if legacy == ROBOTS_IGNORE:
            return ROBOTS_IGNORE
    return mode


class _Initiation:
    def __init__(self, value: str):
        self.value, self.token = value, None

    def __enter__(self):
        self.token = _INITIATION.set(self.value)
        return self

    def __exit__(self, *exc):
        _INITIATION.reset(self.token)
        return False


def user_initiated(surface: str = "call") -> "_Initiation":
    """``with user_initiated("mcp"):`` - the work inside was explicitly started by the user or a caller (an MCP tool
    call, a CLI run or compile). Under ``ANYUSE_ROBOTS=user_initiated`` its robots gates allow what robots.txt
    disallows (recorded)."""
    return _Initiation(f"user:{surface}")


def unattended(kind: str = "breadth") -> "_Initiation":
    """``with unattended("breadth"):`` - breadth exploration or background work, also inside a user's call: robots.txt
    applies under ``user_initiated``."""
    return _Initiation(f"auto:{kind}")


def initiation() -> str:
    return _INITIATION.get()


def robots_override(explicit: Optional[str] = None) -> str:
    """``""`` when robots.txt applies to the work in progress, else WHY it does not (for the run record): an explicit
    ``ignore`` (a CLI flag, a site mode), ``ANYUSE_ROBOTS=ignore``, or ``ANYUSE_ROBOTS=user_initiated`` inside
    :func:`user_initiated`. An explicit ``respect`` (or any unrecognised explicit value) keeps robots.txt applied."""
    if explicit is not None and str(explicit).strip():
        return "ignore (explicit policy)" if str(explicit).strip().lower() == ROBOTS_IGNORE else ""
    mode = robots_mode()
    if mode == ROBOTS_IGNORE:
        return "ANYUSE_ROBOTS=ignore"
    if mode == ROBOTS_USER_INITIATED and _INITIATION.get().startswith("user:"):
        return f"ANYUSE_ROBOTS=user_initiated ({_INITIATION.get()[5:]})"
    return ""


#: Under ``respect``: a task the user started (``user:*`` - a CLI agent run or replay, an MCP call) that is signed in or
#: signs in on a site (it uses the user's imported session, or logs in with the user's credentials) is not crawling.
#: Its requests to that site's own origin are not held to robots.txt, and each one robots.txt would refuse is
#: recorded with this reason. Watch runs, schedules, breadth exploration, recompiles and compile-time discovery are
#: never user-started (``""`` / ``auto:*``) and keep respecting it.
ROBOTS_SIGNED_IN = "a signed-in task you started"


def signed_in_override(explicit: Optional[str] = None, started: Optional[str] = None) -> str:
    """The reason a signed-in task records for an own-origin request robots.txt would refuse (``"a signed-in task you
    started (cli)"``), or ``""``: the work in progress (``started``, else :func:`initiation`) is not user-started, or an
    explicit policy decides alone."""
    if explicit is not None and str(explicit).strip():
        return ""
    s = str(_INITIATION.get() if started is None else started or "")
    if not s.startswith("user:"):
        return ""
    return f"{ROBOTS_SIGNED_IN} ({s[5:]})" if s[5:] else ROBOTS_SIGNED_IN


def robots_policy(explicit: Optional[str] = None) -> str:
    """The robots policy of the work in progress: ``respect`` (default) or ``ignore`` - :func:`robots_override` (the
    ONE reading of ``ANYUSE_ROBOTS``). Precedence: an explicit argument (a CLI flag), then the
    setting. An unrecognised value is ``respect`` - a typo must never silently turn the check off."""
    return ROBOTS_IGNORE if robots_override(explicit) else ROBOTS_RESPECT


def robots_would(url: str) -> Tuple[str, str]:
    """What robots.txt says of ``url`` from what this process knows (no request): ``("allow" | "disallow" |
    "unreadable" | "unknown", note)`` - the verdict a run records when it overrides it."""
    host = urllib.parse.urlsplit(url).netloc if "://" in str(url or "") else str(url or "")
    st, note = robots_status(host)
    if st == "unreadable":
        return "unreadable", note
    ok, why = robots_allows_cached(url)
    if ok is False:
        return "disallow", why
    if ok is None and not robots_known_absent(url):
        return "unknown", why
    return "allow", why


def host_override(host: str, path: Optional[str] = None) -> Optional[dict]:
    """A per-host robots override the user recorded, or ``None``.

    Stored next to the host acknowledgement, because it is the same kind of
    statement — "I looked at this host and I take responsibility" — and it must
    be as visible and as editable. A host override is the sanctioned way to say
    "this is my own site" or "I have written permission"; it does not turn the
    check off anywhere else.
    """
    record = host_entry(load_hosts(path), host)
    ov = record.get("robots_override")
    return ov if isinstance(ov, dict) and ov.get("ignore_robots") else None


def hosts_file() -> str:
    """ANYUSE_API_HOSTS_FILE wins; otherwise it lives in the data dir."""
    return os.environ.get("ANYUSE_API_HOSTS_FILE") or str(
        anyuse_data_path("api_hosts.json"))


def load_hosts(path: Optional[str] = None) -> Dict[str, dict]:
    path = path or hosts_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return {}


def host_entry(hosts: Dict[str, dict], host: str) -> dict:
    """``host``'s registry entry, or ``{}``. A host name is case-insensitive, and keys come both from
    :func:`host_key` (lower case) and from a URL's ``netloc`` as it was written, so the match ignores case;
    an exact key wins."""
    record = hosts.get(host)
    if record is None:
        wanted = str(host or "").lower()
        record = next((value for key, value in hosts.items() if str(key).lower() == wanted), None)
    return record if isinstance(record, dict) else {}


def _registry_key(hosts: Dict[str, dict], host: str) -> str:
    """The key ``host`` is stored under: an existing entry's own key whatever its case, else ``host``."""
    if host in hosts:
        return host
    wanted = str(host or "").lower()
    return next((key for key in hosts if str(key).lower() == wanted), host)


def acknowledge_host(host: str, note: str = "", by: str = "",
                     path: Optional[str] = None,
                     ignore_robots: bool = False,
                     robots_reason: str = "",
                     readonly_post_endpoint: str = "",
                     readonly_post_operation: str = "") -> dict:
    """Record that a human accepted responsibility for replaying against ``host``.

    ``ignore_robots=True`` additionally records a per-host robots override —
    "this is my own site" or "I have permission in writing". It requires a
    reason, it is written in plain sight next to the acknowledgement, and every
    proof and MCP listing that touches the host repeats it.
    """
    path = path or hosts_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hosts = load_hosts(path)
    # one entry per host: a second acknowledgement in another case updates the first
    host = _registry_key(hosts, host)
    previous = host_entry(hosts, host)
    record = {
        "acknowledged": True, "note": note, "by": by,
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if ignore_robots:
        if not (robots_reason or "").strip():
            raise ValueError(
                "a per-host robots override needs a reason — record WHY this "
                "host may be replayed against despite its robots.txt "
                "(e.g. 'my own site', 'written permission from the operator')")
        record["robots_override"] = {
            "ignore_robots": True, "reason": robots_reason.strip(), "by": by,
            "at": record["at"],
        }
    elif previous.get("robots_override"):
        record["robots_override"] = previous["robots_override"]
    if bool(readonly_post_endpoint) != bool(readonly_post_operation):
        raise ValueError("read-only POST approval requires both endpoint and operation")
    if readonly_post_endpoint:
        from anyuse.execution.request_policy import origin
        parsed = urllib.parse.urlsplit(readonly_post_endpoint)
        if parsed.scheme.lower() != "https" or parsed.netloc.lower() != str(host).lower():
            raise ValueError("read-only POST endpoint must be an https URL on the acknowledged host")
        approvals = list(previous.get("readonly_post_approvals") or [])
        approval = {
            "endpoint": origin(readonly_post_endpoint) + (parsed.path or "/"),
            "operation": readonly_post_operation.strip(),
            "by": by, "at": record["at"],
        }
        approvals = [item for item in approvals if not (
            item.get("endpoint") == approval["endpoint"]
            and item.get("operation") == approval["operation"])]
        record["readonly_post_approvals"] = [*approvals, approval]
    elif previous.get("readonly_post_approvals"):
        record["readonly_post_approvals"] = previous["readonly_post_approvals"]
    hosts[host] = record
    with open(path, "w") as fh:
        json.dump(hosts, fh, indent=2, sort_keys=True)
    return hosts[host]


def host_key(value: str) -> str:
    """The registry key for what a person typed as a host: ``host[:port]`` in lower case, the form
    :func:`host_acknowledged` is asked with. A URL gives its host and port (``https://Example.org/search``
    is ``example.org``). Raises ValueError for anything that names no web host."""
    text = str(value or "").strip()
    usage = "give a host with an optional port, like example.org or 127.0.0.1:8080, or the site's URL"
    if not text or any(ch.isspace() for ch in text):
        raise ValueError(f"{value!r} is not a host: {usage}")
    parts = urllib.parse.urlsplit(text if "://" in text else "//" + text)
    if parts.scheme and parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"{value!r} is not an http or https address: {usage}")
    try:
        parts.port
    except ValueError:
        raise ValueError(f"{value!r} has a port that is not a number: {usage}") from None
    # an empty port (``https://example.org:/``) is the default port, which a tool's URL leaves out
    netloc = parts.netloc.rpartition("@")[2].lower().rstrip(":")
    if not parts.hostname or not netloc:
        raise ValueError(f"{value!r} names no host: {usage}")
    return netloc


def host_acknowledged(host: str, path: Optional[str] = None) -> bool:
    """Recorded in the registry, or answered yes in the command in progress under ``--dry-run`` /
    ``--no-persist`` (:func:`acknowledged_for_this_command`)."""
    return _recorded(host, path) or acknowledged_for_this_command(host)


def _recorded(host: str, path: Optional[str] = None) -> bool:
    return bool(host_entry(load_hosts(path), host).get("acknowledged"))


# ---------------------------------------------------------------------------
# the inline question (the CLI)
# ---------------------------------------------------------------------------

#: The note a yes to the CLI's inline question records.
CONFIRMED_AT_COMPILE = "confirmed at compile"
#: :attr:`HostQuestion.purpose`: before the proof replays the compiled request, or before saving a tool
#: whose proof sent none through the compiled request (a page only the browser replay reproduced)
PURPOSE_PROOF = "proof"
PURPOSE_SAVE = "save"


@dataclass
class HostQuestion:
    """What an asker is asked: may AnyUse send requests to ``host``, which is not acknowledged yet?"""
    host: str
    url: str
    robots_allowed: Optional[bool] = None
    robots_note: str = ""
    robots_overridden: str = ""
    #: the proof's request count, ``(fewest, most)``; ``(0, 0)`` when not known
    requests: Tuple[int, int] = (0, 0)
    purpose: str = PURPOSE_PROOF


#: the command in progress's :class:`_Asking`: its asker, ``asker(question) -> (yes, why_not)``, whether a yes is
#: written, and the hosts answered yes without writing. Installed only by the CLI (:func:`asking_hosts`); None
#: everywhere else, where an unacknowledged host is refused as it always was.
_HOST_ASKER: "contextvars.ContextVar[Optional[Any]]" = contextvars.ContextVar("anyuse_host_asker", default=None)


class _Asking:
    def __init__(self, asker, record: bool = True):
        self.asker, self.record, self.token = asker, bool(record), None
        #: hosts answered yes while ``record`` is False (lower case): acknowledged until the command ends, never written
        self.yes = set()

    def __enter__(self):
        self.token = _HOST_ASKER.set(self)
        return self.asker

    def __exit__(self, *exc):
        _HOST_ASKER.reset(self.token)
        return False


def asking_hosts(asker, *, record: bool = True) -> "_Asking":
    """``with asking_hosts(asker):`` - inside, :func:`ask_for_host` puts a :class:`HostQuestion` to ``asker`` before
    the proof replays the compiled request against a host that is not acknowledged yet. ``record=False``
    (``--dry-run``, ``--no-persist``): a yes holds until the block ends and is not written to the registry."""
    return _Asking(asker, record)


def host_asker():
    """The asker installed for the command in progress, or None."""
    state = _HOST_ASKER.get()
    return state.asker if state is not None else None


def acknowledged_for_this_command(host: str) -> bool:
    """``host`` was answered yes in the command in progress, which writes nothing (``--dry-run``, ``--no-persist``)."""
    state = _HOST_ASKER.get()
    return bool(state is not None and str(host or "").lower() in state.yes)


def only_unacknowledged(verdict: Any) -> bool:
    """The verdict refuses only because the host is not acknowledged: ``robots.txt`` does not disallow the path, or
    its disallow was overridden (a per-host override, the ``ignore`` policy)."""
    if getattr(verdict, "may_prefer", False) or getattr(verdict, "acknowledged", True) is not False:
        return False
    return (getattr(verdict, "robots_allowed", None) is not False
            or bool(getattr(verdict, "robots_overridden", "")))


def ask_for_host(url: str, verdict: Any, *, requests: Tuple[int, int] = (0, 0),
                 purpose: str = PURPOSE_PROOF, hosts_path: Optional[str] = None) -> Tuple[bool, str]:
    """Ask the installed asker about ``url``'s host when ``verdict`` refuses only for want of an acknowledgement.
    A yes records the acknowledgement as ``anyuse workflow acknowledge-host`` does (same registry, note
    :data:`CONFIRMED_AT_COMPILE`), or, when the asker was installed with ``record=False``, holds it for the
    command in progress only. Returns ``(acknowledged now, why not)``; ``(False, "")`` when nothing was asked
    (no asker, or the verdict refuses for another reason)."""
    state = _HOST_ASKER.get()
    if state is None or not only_unacknowledged(verdict):
        return False, ""
    host = str(getattr(verdict, "host", "") or urllib.parse.urlsplit(url).netloc)
    ok, why = state.asker(HostQuestion(
        host=host, url=url, robots_allowed=getattr(verdict, "robots_allowed", None),
        robots_note=str(getattr(verdict, "robots_note", "") or ""),
        robots_overridden=str(getattr(verdict, "robots_overridden", "") or ""),
        requests=tuple(requests or (0, 0)), purpose=purpose))
    if not ok:
        return False, str(why or f"{host} is not acknowledged")
    if not state.record:
        state.yes.add(host.lower())
    elif not _recorded(host, hosts_path):
        acknowledge_host(host, note=CONFIRMED_AT_COMPILE, path=hosts_path)
    return True, ""


def readonly_post_approved(url: str, operation: str,
                           path: Optional[str] = None) -> bool:
    """Exact endpoint and operation approval stored under this host."""
    from anyuse.execution.request_policy import origin
    parsed = urllib.parse.urlsplit(url)
    endpoint = origin(url) + (parsed.path or "/")
    record = host_entry(load_hosts(path), parsed.netloc)
    return any(item.get("endpoint") == endpoint and item.get("operation") == operation
               for item in (record.get("readonly_post_approvals") or [])
               if isinstance(item, dict))


def robots_allows(url: str, fetcher=None, on_hop=None) -> Tuple[Optional[bool], str]:
    """Does ``robots.txt`` allow :data:`ROBOTS_AGENT` to fetch ``url``?

    Returns ``(True | False | None, explanation)``. ``None`` means "could not
    tell" — no ``robots.txt``, a network error, an unparseable file — and is
    deliberately NOT treated as a refusal: a missing file is not a prohibition,
    and pretending otherwise would be its own kind of dishonesty.

    The result is cached in memory for the process AND on disk for
    :data:`ROBOTS_TTL_S`, so compiling three workflows on one site fetches
    ``robots.txt`` once rather than three times. Fewer requests to somebody
    else's server is the point; the speed-up is incidental.

    ``fetcher(robots_url) -> str`` is injectable so tests never touch a network. ``on_hop`` (:func:`fetch_robots`):
    asked before each redirect hop's GET.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    if not host:
        return None, "no host in url"
    robots_url = urllib.parse.urlunsplit((parts.scheme or "https", host,
                                          "/robots.txt", "", ""))
    if host in _ROBOTS_CACHE:
        parser = _ROBOTS_CACHE[host]
        if parser is None:
            # The cache is transparent: a host whose robots.txt is absent must
            # give the SAME answer whether the absence was learned a moment ago
            # or read back from disk. A cache that changes the message is a
            # cache that changes behaviour.
            return None, _ROBOTS_NOTES.get(host) or f"no robots.txt at {robots_url}"
        if host in _ROBOTS_NOTES:
            return False, _ROBOTS_NOTES[host]
    elif (cached := _cache_load(host)) is not None:
        _ROBOTS_NOTES.pop(host, None)
        if _cache_unreachable(cached):
            # an unreachable verdict is cached like a file (complete disallow, read again after
            # ROBOTS_UNREACHABLE_TTL_S)
            note = str((_cache_entry(host) or {}).get("note") or f"robots_unreachable: {robots_url} (cached)")
            _ROBOTS_CACHE[host] = _disallow_all()
            _ROBOTS_NOTES[host] = note
            return False, note
        if _is_absent(cached):
            _ROBOTS_CACHE[host] = None
            if cached != _CACHE_ABSENT:
                _ROBOTS_NOTES[host] = _absent_note(cached, robots_url)
            return None, _absent_note(cached, robots_url)
        parser = RobotsRules()
        parser.parse(cached.splitlines())
        _ROBOTS_CACHE[host] = parser
    else:
        _ROBOTS_NOTES.pop(host, None)
        kind, text, note, _info = robots_read(robots_url, fetcher, on_hop=on_hop)   # stored there
        if kind == "none":
            # RFC 9309: a 4xx (401 / 403 included) is "unavailable" - no rules, the site is allowed
            _ROBOTS_CACHE[host] = None
            _ROBOTS_NOTES[host] = note
            return None, note
        if kind == "disallow":
            # RFC 9309: a 429 still after the retries, a 5xx or a network failure - complete disallow for this run
            _ROBOTS_CACHE[host] = _disallow_all()
            _ROBOTS_NOTES[host] = note
            return False, note
        if kind == "policy":
            # the reason is kept for the host (``robots_status``) - a refused request then says
            # WHY its robots.txt gave no answer, never "robots.txt disallows"
            _ROBOTS_CACHE[host] = None
            _ROBOTS_NOTES[host] = note
            return None, note
        landed = ROBOTS_REDIRECTS.pop(robots_url, "")
        # the file the redirects landed on is the landing host's own robots.txt only at its /robots.txt (RFC 9309: it is
        # obeyed for the host that was asked either way)
        lp = urllib.parse.urlsplit(landed) if landed else None
        other = lp.netloc if lp is not None and lp.path == "/robots.txt" and not lp.query else ""
        if text is None:
            _ROBOTS_CACHE[host] = None
            if other and other != host:
                _ROBOTS_CACHE[other] = None
                _cache_store(other, _CACHE_ABSENT)
            return None, f"no robots.txt at {robots_url}"
        parser = RobotsRules()
        parser.parse(text.splitlines())
        _ROBOTS_CACHE[host] = parser
        if landed and landed != robots_url:
            # a redirected fetch (www <-> apex, http -> https, another host): the file the redirects landed on is the
            # landing host's own robots.txt too when it sits at /robots.txt, so the page that lives there is gated by it
            # (never fail-closed for want of a fetch)
            if other and other != host:
                _ROBOTS_CACHE[other] = parser
                _cache_store(other, text)
            allowed = parser.can_fetch(ROBOTS_AGENT, url)
            return bool(allowed), (f"robots.txt for {host} (redirected to {landed}) "
                                   f"{'allows' if allowed else 'disallows'} {ROBOTS_AGENT} on {parts.path or '/'}")
    if parser is None:
        return None, f"no usable robots.txt for {host}"
    allowed = parser.can_fetch(ROBOTS_AGENT, url)
    return bool(allowed), (f"robots.txt for {host} {'allows' if allowed else 'disallows'} "
                           f"{ROBOTS_AGENT} on {parts.path or '/'}")


def robots_allows_cached(url: str) -> Tuple[Optional[bool], str]:
    """:func:`robots_allows` restricted to what is already known locally.

    Never opens a socket: it answers from the in-process cache or the on-disk
    one and returns ``(None, …)`` when neither has this host. This is what the
    *replay* path uses, for two reasons. Replaying a compiled rung must not cost
    the site an extra ``robots.txt`` request every time, and a unit test that
    executes an api step against an injected transport must not reach the
    network to find out whether it may.

    The compile path still fetches (:func:`check_permission`), so a host is
    always checked at least once, freshly, before anything is persisted.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    if not host:
        return None, "no host in url"
    parser = _ROBOTS_CACHE.get(host, "\x00miss")
    if parser == "\x00miss":
        _ROBOTS_NOTES.pop(host, None)
        cached = _cache_load(host)
        if cached is None:
            return None, f"no cached robots.txt for {host} — not fetched on the replay path"
        if _cache_unreachable(cached):
            note = str((_cache_entry(host) or {}).get("note") or f"robots_unreachable: {host} (cached)")
            _ROBOTS_CACHE[host] = _disallow_all()
            _ROBOTS_NOTES[host] = note
            return False, note
        if _is_absent(cached):
            return None, _absent_note(cached, host) if cached != _CACHE_ABSENT else f"no robots.txt at {host}"
        parser = RobotsRules()
        parser.parse(cached.splitlines())
        _ROBOTS_CACHE[host] = parser
    if parser is None:
        return None, _ROBOTS_NOTES.get(host) or f"no robots.txt at {host}"
    if host in _ROBOTS_NOTES:
        return False, _ROBOTS_NOTES[host]
    allowed = parser.can_fetch(ROBOTS_AGENT, url)
    return bool(allowed), (f"cached robots.txt for {host} "
                           f"{'allows' if allowed else 'disallows'} "
                           f"{ROBOTS_AGENT} on {parts.path or '/'}")


def robots_status(host: str) -> Tuple[str, str]:
    """(A hosted search provider's requests used to be refused at the robots gate with the evidence
    "robots.txt disallows" when that provider's hosts had given NO usable robots.txt answer; the same held for static
    asset hosts.) What this process knows about ``host``'s robots.txt, local only - ``("rules", "")`` a file was read
    (its rules decide),
    ``("absent", note)`` a 404 / other 4xx (no rules), ``("unreadable", note)`` a 429 that stayed, a 5xx, a network
    failure, a redirect that reached no file (a refused host, a loop, a 6th hop) or a GET the pre-send policy refused
    (a refused destination: fail closed),
    ``("unknown", "")`` never fetched here."""
    h = str(host or "").lower()
    note = _ROBOTS_NOTES.get(h, "")
    parser = _ROBOTS_CACHE.get(h, "\x00miss")
    if parser == "\x00miss":
        cached = _cache_load(h)
        if cached is None:
            return "unknown", ""
        if _cache_unreachable(cached):
            return "unreadable", str((_cache_entry(h) or {}).get("note") or "unreachable (cached)")
        return ("absent", _absent_note(cached, f"https://{h}/robots.txt")) if _is_absent(cached) else ("rules", "")
    if parser is None:
        if not note or note.startswith(("robots_unavailable_4xx", "no robots.txt")):
            return "absent", note
        return "unreadable", note
    return ("unreadable", note) if note else ("rules", "")


def robots_known_absent(url: str) -> bool:
    """True only when this host's ``robots.txt`` was FETCHED and found absent (a 404, kept in the on-disk cache):
    a missing file is not a prohibition (:func:`robots_allows`). A host never fetched, or one whose fetch
    failed, is NOT known absent - a fail-closed gate keeps refusing it. Local only, never opens a socket."""
    host = urllib.parse.urlsplit(url).netloc if "://" in str(url or "") else str(url or "")
    return bool(host) and _is_absent(_cache_load(host))


def replay_override(url: str, hosts_path: Optional[str] = None, policy: Optional[str] = None,
                    signed_in: bool = False) -> str:
    """The note a run records when :func:`replay_refusal` let a URL through that robots.txt
    disallows (the policy, a per-host override, or a signed-in task the user started: ``signed_in``, the request is
    the template's own origin replayed with the user's session), else ``""``."""
    allowed, note = robots_allows_cached(url)
    if allowed is not False:
        return ""
    host = urllib.parse.urlsplit(url).netloc
    if host_override(host, hosts_path):
        return f"robots would disallow ({note}); a per-host override was recorded"
    # the rule that let it through: ANYUSE_ROBOTS=ignore, else the sign-in, else the policy
    signed = signed_in_override(policy) if signed_in and robots_mode() != ROBOTS_IGNORE else ""
    why = signed or robots_override(policy)
    return f"robots would disallow ({note}); overridden: {why}" if why else ""


def replay_refusal(url: str, hosts_path: Optional[str] = None,
                   policy: Optional[str] = None, signed_in: bool = False) -> str:
    """Why this URL may not be replayed through the api rung, or ``""``. ``signed_in``: the request goes to the
    template's own origin with the user's imported session (``login=replay``); in a task the user started it is not
    held to robots.txt (:data:`ROBOTS_SIGNED_IN`).

    Local-only and therefore cheap enough to run before every request.
    """
    allowed, note = robots_allows_cached(url)
    if allowed is not False:
        return ""
    host = urllib.parse.urlsplit(url).netloc
    if robots_policy(policy) == ROBOTS_IGNORE or host_override(host, hosts_path):
        return ""
    if signed_in and signed_in_override(policy):
        return ""
    return (f"{note}; robots policy is 'respect', so this path is not replayed. "
            f"Record a per-host override with "
            f"`anyuse workflow acknowledge-host {host} --ignore-robots --reason ...` "
            f"if this is your own site or you have the operator's permission.")


#: robots.txt URL -> the URL its redirects landed on (``www.X`` -> ``X``, or any other host); read and cleared by
#: :func:`robots_allows`, which obeys the file for the host that was asked, keeps it for the landing host too when it
#: sits at /robots.txt, and says so in its note
ROBOTS_REDIRECTS: Dict[str, str] = {}


def _default_fetch(robots_url: str, hop_screen=None, *, get=None, resolver=None,
                   check_destination: bool = True) -> Optional[str]:
    """GET robots.txt, following its redirects as RFC 9309 section 2.3.1.2 says: at most
    :data:`ROBOTS_MAX_REDIRECTS` consecutive hops, to any host. Before each hop's GET: the hop must be http(s), not a
    URL already asked (a loop), not a 6th hop, not a never-contacted host (``web_scope.contact_refusal``: NEVER_CONTACT,
    and government / .int / europa / military with ANYUSE_REFUSE_GOV_HOSTS), not a non-public address (``destination_refusal``); then the
    caller's hop gate (:data:`_HOP_GATE`: the site's budget) and ``hop_screen(hop_url) -> "" | why`` (a bench's own
    screen) must let it go. Any of these refusing is :class:`RobotsUnreachable` (complete disallow, typed). The first
    GET's own destination refusal stays a pre-send refusal (``PermissionError``). The chain is kept in
    :data:`ROBOTS_CHAINS`, the landing URL in :data:`ROBOTS_REDIRECTS`. ``get(url) -> (status, headers, text)`` and
    ``resolver`` are injectable (tests: no network); ``check_destination=False`` for a ``get`` that applies the
    destination policy to each request itself (``discover.HttpFetcher``)."""
    from anyuse.execution import site_canon as SC
    from anyuse.execution.request_policy import destination_refusal, exact_origin_allowlist, origin
    from anyuse.execution.web_scope import contact_refusal, host_of
    allowed = set(exact_origin_allowlist(robots_url, SC.sibling_origins(robots_url)))
    res = {"resolver": resolver} if resolver is not None else {}
    chain: List[str] = [robots_url]
    gate = _HOP_GATE.get()

    def header(headers, name: str):
        return next((v for k, v in dict(headers or {}).items() if str(k).lower() == name), None)

    def walk(get_one):
        current = robots_url
        refusal = destination_refusal(current, allowed, resolve=True, **res) if check_destination else ""
        if refusal:
            raise PermissionError(f"robots pre-send policy refused request: {refusal}")
        while True:
            status, headers, text = get_one(current)
            location = header(headers, "location") if int(status) in (301, 302, 303, 307, 308) else None
            if not location:
                return current, int(status), headers, text
            nxt = urllib.parse.urldefrag(urllib.parse.urljoin(current, location))[0]
            chain.append(nxt)
            ROBOTS_CHAINS[robots_url] = list(chain)
            if len(chain) - 1 > ROBOTS_MAX_REDIRECTS:
                raise RobotsUnreachable("too_many_redirects", chain, f"more than {ROBOTS_MAX_REDIRECTS} redirects")
            if nxt in chain[:-1]:
                raise RobotsUnreachable("redirect_loop", chain, f"{nxt[:120]} was asked before")
            p = urllib.parse.urlsplit(nxt)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise RobotsUnreachable("redirect_to_refused_destination", chain, f"not http(s): {nxt[:80]}")
            why = contact_refusal(host_of(nxt))
            if why:
                raise RobotsUnreachable("redirect_to_refused_host", chain, f"{host_of(nxt)}: {why}")
            allowed.add(origin(nxt))
            refusal = destination_refusal(nxt, allowed, resolve=True, **res) if check_destination else ""
            if refusal:
                raise RobotsUnreachable("redirect_to_refused_destination", chain, refusal)
            for screen in (gate, hop_screen):
                why = str(screen(nxt) or "") if screen is not None else ""
                if why:
                    raise RobotsUnreachable("redirect_not_sent", chain, why[:160])
            current = nxt
    if get is not None:
        current, status, headers, text = walk(get)
    else:
        import httpx
        with httpx.Client(timeout=10, follow_redirects=False,
                          transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=1),
                          headers={"User-Agent": ROBOTS_AGENT}) as client:
            def one(u):
                r = client.get(u)
                return r.status_code, r.headers, r.text
            current, status, headers, text = walk(one)
    if current != robots_url:
        ROBOTS_REDIRECTS[robots_url] = current
    if int(status) == 404:
        return None
    if not 200 <= int(status) < 300:
        # RFC 9309: the status decides - classified by robots_allows
        ra = header(headers, "retry-after")
        try:
            retry_after = float(ra) if ra is not None else None
        except ValueError:
            retry_after = None
        raise RobotsHTTPStatus(current, int(status), retry_after)
    return text


@dataclass
class PermissionVerdict:
    """Whether a compiled rung may be made preferred, and on what basis."""
    may_prefer: bool = False
    host: str = ""
    robots_allowed: Optional[bool] = None
    robots_note: str = ""
    acknowledged: bool = False
    reason: str = ""
    #: "respect" | "ignore" — the policy in force for this check
    policy: str = ROBOTS_RESPECT
    #: set when a disallow was overridden, so every report can say so
    robots_overridden: str = ""

    def to_dict(self) -> dict:
        return {"may_prefer": self.may_prefer, "host": self.host,
                "robots_allowed": self.robots_allowed, "robots_note": self.robots_note,
                "acknowledged": self.acknowledged, "reason": self.reason,
                "robots_policy": self.policy,
                "robots_overridden": self.robots_overridden}


def check_permission(url: str, hosts_path: Optional[str] = None,
                     fetcher=None, policy: Optional[str] = None) -> PermissionVerdict:
    """The courtesy gate. Compilation is never blocked; PREFERENCE is.

    * ``robots.txt`` explicitly disallows the path → never preferred, unless the
      user has recorded a per-host override or set the global policy to
      ``ignore``. Both are visible in the verdict and repeated in every report.
    * host not acknowledged → compiled and reported, but not preferred.
    * acknowledged and not disallowed → may be preferred.

    ``policy`` is the resolved global setting; leave it ``None`` to read it from
    the environment. A per-host override applies whatever the global policy is,
    because it is the *narrower* statement.
    """
    host = urllib.parse.urlsplit(url).netloc
    resolved = robots_policy(policy)
    allowed, note = robots_allows(url, fetcher)
    ack = host_acknowledged(host, hosts_path)
    verdict = PermissionVerdict(host=host, robots_allowed=allowed, robots_note=note,
                                acknowledged=ack, policy=resolved)
    if allowed is False:
        override = host_override(host, hosts_path)
        if override:
            verdict.robots_overridden = (
                f"per-host override recorded by {override.get('by') or 'the user'} "
                f"on {override.get('at')}: {override.get('reason')}")
        elif resolved == ROBOTS_IGNORE:
            verdict.robots_overridden = (
                "global robots policy is 'ignore' — robots.txt was read and its "
                "disallow was deliberately not honoured; this is the user's "
                "responsibility, not a judgement AnyUse made")
        else:
            verdict.reason = (f"robots.txt disallows this path for {ROBOTS_AGENT}; "
                              f"not persisted as preferred")
            return verdict
    if not ack:
        verdict.reason = (f"host {host!r} is not acknowledged; run "
                          f"`anyuse workflow acknowledge-host {host}`")
        return verdict
    verdict.may_prefer = True
    verdict.reason = f"{note}; host acknowledged"
    if not _recorded(host, hosts_path):
        verdict.reason += " for this command (not recorded)"
    if verdict.robots_overridden:
        verdict.reason += f"; ROBOTS DISALLOW OVERRIDDEN — {verdict.robots_overridden}"
    return verdict
