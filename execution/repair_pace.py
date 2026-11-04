"""PACING and RATE LIMITS for the repair ladder's own runs (ladder-only, OFF by default:
``repair_ladder.LadderConfig(pacing=True)``, ``repair_check.OracleChecker(rate_limit=True)``).

On www.woodworker.de the safe upgrade gate's shadow runs searched seconds apart and met the site's "Sie
dürfen nur alle 10 Sekunden eine Suche starten" page - the candidate's run "failed" there, so the gate counted the
candidate WORSE and refused it, and the census's known wrong answer of that tool stayed withheld. A rate limit is
INFRASTRUCTURE: it is never a refusal, a block, an answer or a reason to refuse an upgrade.

* ``rate_limit_why(text, why)``: "" or why a page is a RATE-LIMIT page - an HTTP 429 (the tool's own block reason), or
  a short page stating "too many requests" (11 languages) or a minimum interval / a wait in seconds next to a search /
  request / retry word - and the wait it states (seconds; 0 when it states none). General: no site is named.
* ``Pacer``: a per-host minimum interval between the ladder's page openings and checker calls - ``MIN_INTERVAL_S`` or
  the host's robots.txt crawl-delay, whichever is longer, and after a rate limit the stated wait (or the response's
  Retry-After) capped at ``MAX_WAIT_S``. One per process (``PACER``): every ladder invocation of the process shares the
  hosts' clocks.

Nothing here contacts anything; the wait is ``asyncio.sleep`` (a test replaces the clock and the sleep).
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from anyuse.execution import demo_compile as C

#: pacing is ON in the ladder's default configuration
#: (``LadderConfig(pacing=False)`` turns it off); the ladder itself stays off by default
PACING_BY_DEFAULT = True
#: the per-host minimum interval between two of the ladder's page openings (a search each): www.woodworker.de
#: allows one search every 10 s
MIN_INTERVAL_S = 10.0
#: a rate limit is retried once after its wait; a stated wait above this is not waited for (the run stays rate-limited)
MAX_WAIT_S = 60.0
#: the wait after a rate limit that states none (and no Retry-After)
DEFAULT_WAIT_S = 20.0
#: a rate-limit statement is read on a SHORT page only (the statement is the page's content, not an article about it)
SHORT_PAGE_CHARS = 3000
#: ... and on a short line
SHORT_LINE_CHARS = 240

RATE_LIMITED = "rate_limited"

_SECONDS = r"(?:seconds?|secs?|sekunden|sekunde|sek|secondes?|segundos?|secondi|secondo|seconden|seconde|sekund\w*|" \
           r"sekunder|sekuntia|sekunti|sekunnin)"
_TOO_MANY = re.compile(
    r"\btoo many (?:requests|searches|queries)\b|\brate[ -]?limit(?:ed|ing)?\b|"
    r"\bzu viele (?:anfragen|suchanfragen|zugriffe|suchen)\b|"
    r"\btrop de (?:requetes|demandes|recherches)\b|"
    r"\bdemasiadas (?:solicitudes|peticiones|busquedas|consultas)\b|"
    r"\btroppe (?:richieste|ricerche)\b|"
    r"\bte veel (?:verzoeken|aanvragen|zoekopdrachten|zoekacties)\b|"
    r"\b(?:za duzo|zbyt wiele|zbyt duzo) (?:zapytan|zadan|wyszukiwan)\b|"
    r"\bfor mange (?:foresporgsler|forsporgsler|anmodninger|sogninger|sokninger)\b|"
    r"\bfor manga (?:forfragningar|sokningar|anrop)\b|"
    r"\bliian monta (?:pyyntoa|hakua)\b")
#: "one search every N seconds" / "alle N Sekunden" / "toutes les N secondes" ...
_EVERY = re.compile(r"\b(?:every|alle|toutes les|cada|ogni|elke|om de|co|hver|hvert|var|joka)\s+(\d{1,3})\s*" + _SECONDS)
#: "wait N seconds" / "in N Sekunden erneut" ...
_WAIT = re.compile(r"\b(?:wait\w*|wart\w*|patient\w*|attend\w*|esper\w*|wacht\w*|odczek\w*|poczek\w*|vent\w*|"
                   r"vant\w*|odot\w*)\b[^.!?]{0,40}?\b(\d{1,3})\s*" + _SECONDS + r"|"
                   r"\b(?:in|nach|dans|en|tra|fra|za|om|efter|kuluttua)\s+(\d{1,3})\s*" + _SECONDS)
#: the statement of a minimum interval / a wait must be about searching / requests / trying again
_ACT = re.compile(r"\b(?:search\w*|such\w*|recherch\w*|busqu\w*|ricerc\w*|zoek\w*|szuk\w*|wyszuk\w*|sog\w*|sok\w*|"
                  r"hak\w*|request\w*|anfrage\w*|requete\w*|solicitud\w*|richiest\w*|verzoek\w*|zapyt\w*|"
                  r"again|erneut|nochmal|nouveau|de nuevo|di nuovo|opnieuw|ponownie|igen|igjen|uudelleen|"
                  r"retry|try)\b")


def rate_limit_why(text: Any = "", why: Any = "") -> Tuple[str, float]:
    """("", 0) or (why the page is a RATE-LIMIT page, the wait it states in seconds - 0 when none). ``why``: the run's
    own block reason (``demo_tool``: "HTTP 429 on the document ..."); ``text``: the page's visible text."""
    w = str(why or "")
    if re.match(r"^(?:block:\s*)?HTTP 429\b", w) or w.startswith(RATE_LIMITED):
        return "HTTP 429 (too many requests)", 0.0
    t = str(text or "")
    if not t.strip() or len(" ".join(t.split())) > SHORT_PAGE_CHARS:
        return "", 0.0
    lines = [(raw, C.fold9(raw)) for raw in t.split("\n")]
    lines = [(raw, ln) for raw, ln in lines if ln and len(ln) <= SHORT_LINE_CHARS]
    waits = [_wait_of(_EVERY.search(ln) or _WAIT.search(ln)) for _raw, ln in lines]
    stated = next((w for w in waits if w), 0.0)
    for (raw, line), w in zip(lines, waits):
        if _TOO_MANY.search(line) or (w and _ACT.search(line)):
            return f"the page states a rate limit ({raw.strip()[:100]!r})", w or stated
    return "", 0.0


def _wait_of(m: Optional["re.Match[str]"]) -> float:
    if not m:
        return 0.0
    for g in m.groups():
        if g and g.isdigit():
            return float(g)
    return 0.0


def retry_after(network: Any) -> float:
    """The Retry-After (seconds) of a 429 document in a page's request log, 0 when none is stated."""
    for r in network or []:
        if int((r or {}).get("status") or 0) != 429:
            continue
        h = {str(k).lower(): str(v) for k, v in ((r or {}).get("headers") or {}).items()}
        v = h.get("retry-after", "").strip()
        if v.isdigit():
            return float(v)
    return 0.0


def host_of(tool: Dict[str, Any]) -> str:
    s = tool.get("search") or {}
    u = str(s.get("template") or s.get("start_url") or tool.get("start_url") or "")
    return (urllib.parse.urlsplit(u).hostname or str(tool.get("site") or "")).lower()


class Pacer:
    """The per-host minimum interval (see the module doc). ``crawl_delay(host)`` -> seconds or None (robots.txt);
    ``clock`` / ``sleep`` replace ``time.monotonic`` / ``asyncio.sleep`` in tests."""

    def __init__(self, min_interval_s: float = MIN_INTERVAL_S,
                 crawl_delay: Optional[Callable[[str], Optional[float]]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Optional[Callable[[float], Awaitable[Any]]] = None):
        self.min_interval_s = float(min_interval_s)
        self.crawl_delay = crawl_delay
        self.clock = clock
        self.sleep = sleep or asyncio.sleep
        self.last: Dict[str, float] = {}
        self.not_before: Dict[str, float] = {}
        self.waited_s = 0.0
        self.events: list = []

    def interval(self, host: str) -> float:
        d = None
        try:
            d = self.crawl_delay(host) if self.crawl_delay else None
        except Exception:                              # noqa: BLE001 - robots.txt unread: the minimum
            d = None
        return max(self.min_interval_s, float(d or 0.0))

    async def wait(self, host: str, why: str = "") -> float:
        """Wait until ``host`` may be opened again (its interval since the last opening, a rate limit's wait); mark the
        opening. The seconds waited."""
        now = self.clock()
        due = max(self.last.get(host, -1e18) + self.interval(host), self.not_before.get(host, -1e18))
        w = max(0.0, due - now)
        if w > 0:
            await self.sleep(w)
            self.waited_s += w
        self.last[host] = self.clock()
        if w > 0:
            self.events.append({"host": host, "waited_s": round(w, 2), "why": why or "interval"})
        return w

    def backoff(self, host: str, stated_s: float = 0.0) -> float:
        """After a rate limit: the host is not opened again before the stated wait (or ``DEFAULT_WAIT_S``) has passed.
        The wait; ``-1`` when the stated wait exceeds ``MAX_WAIT_S`` (not waited for)."""
        w = float(stated_s or 0.0) or DEFAULT_WAIT_S
        if w > MAX_WAIT_S:
            self.events.append({"host": host, "rate_limit_wait_s": w, "why": "over the cap: not retried"})
            return -1.0
        w = max(w, self.interval(host))
        self.not_before[host] = self.clock() + w
        self.events.append({"host": host, "rate_limit_wait_s": w, "why": "rate limit"})
        return w


#: the process's pacer (every ladder invocation of the process shares the hosts' clocks)
PACER = Pacer()


def reset(pacer: Optional[Pacer] = None) -> Pacer:
    """Replace the process's pacer (tests; a bench that sets the crawl delays)."""
    global PACER
    PACER = pacer or Pacer()
    return PACER
