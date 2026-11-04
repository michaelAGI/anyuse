"""Readiness for a typed search: wait for the DOM to stop changing, not a timer.

The typing probe (``cdp_guard.GuardedSession.type_search``) used fixed
timers: 3 s after typing, 5 s after Enter. Two stored failures show what a
timer gets wrong:

* a result request that carries the typed value is still in flight when the
  timer reads the page (the rendered list is not there yet);
* the value is typed before the page's scripts took over the input, and the
  hydration wipes it (the search then runs with an empty query).

This module replaces the timer with evidence, all read from the page itself:

1. a ``MutationObserver`` (installed by the probe, idempotent, re-installed on a
   new document) reports how long the DOM has been quiet;
2. the session's own network log reports whether a same-site XHR/fetch is in
   flight, and whether one of them carries the typed value.

The wait ends when the document is complete and the DOM has been quiet for
``quiet_ms`` with no XHR/fetch in flight. While only requests WITHOUT the typed
value are open (a long poll, a beacon), it ends once a request carrying the value
has answered and the DOM has been quiet for ``quiet_ms``, or once the DOM has been
quiet for ``stable_ms``. Otherwise it ends at the ceiling. The
ceiling is the old timer, so a quiet page finishes sooner; the ONLY extension
is ``inflight_grace_s`` while a request that carries the typed value has not
answered (default 0: off). Waiting sends nothing: no request is added.

When the page cannot answer the probe (an old fake, a context being torn down
on every poll), the wait falls back to the timer's remaining time, exactly as
before.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: installs the observer once per document and reports the quiet time
SETTLE_PROBE_JS = r"""
(() => {
  const w = window;
  if (!w.__anyuseSettle) {
    const st = {last: performance.now(), n: 0};
    try {
      new MutationObserver((ms) => { st.n += ms.length; st.last = performance.now(); })
        .observe(document.documentElement || document,
                 {subtree: true, childList: true, characterData: true, attributes: true});
    } catch (e) { return null; }
    w.__anyuseSettle = st;
  }
  const s = w.__anyuseSettle;
  return {quiet_ms: Math.round(performance.now() - s.last), mutations: s.n,
          ready: document.readyState};
})()
"""

#: the value the focused/indexed input holds now
INPUT_VALUE_JS = r"""
(() => { const el = document.querySelectorAll('input')[%d];
  return el ? String(el.value || '') : null; })()
"""

DEFAULT_QUIET_MS = 500
DEFAULT_POLL_S = 0.15


@dataclass
class SettleReport:
    ended: str = ""                 # quiet | dom_stable | value_answered | ceiling | inflight_ceiling | timer
    waited_s: float = 0.0
    polls: int = 0
    mutations: int = 0
    inflight_at_end: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ended": self.ended, "waited_s": round(self.waited_s, 3),
                "polls": self.polls, "mutations": self.mutations,
                "inflight_at_end": self.inflight_at_end}


def decide(obs: Optional[dict], elapsed_s: float, *, ceiling_s: float,
           quiet_ms: int = DEFAULT_QUIET_MS, inflight: int = 0,
           inflight_value: int = 0, inflight_grace_s: float = 0.0, stable_ms: int = 0,
           value_answered: int = 0) -> str:
    """``"wait"`` or why the wait ends. Pure. THE settle rule of every browser path (the
    typed search, the agent's actions, the compile's proof replays and the replays use it; ``web_arch_run.settle_bounded``
    and :func:`settle_page` are its callers).

    ``inflight``: same-site XHR/fetch not yet answered; ``inflight_value``: of
    those, the ones that carry the typed value. ``stable_ms`` (0: off): a DOM quiet that long ends the wait although
    a request WITHOUT the value is still open (``dom_stable``: a page's own long poll never answers).
    ``value_answered``: requests carrying the typed value that answered since the action; with one of them answered
    and only requests without the value open, a DOM quiet for ``quiet_ms`` ends the wait (``value_answered``)."""
    hard = ceiling_s + (max(0.0, inflight_grace_s) if inflight_value else 0.0)
    if elapsed_s >= hard:
        return "inflight_ceiling" if hard > ceiling_s else "ceiling"
    if not isinstance(obs, dict):
        return "wait"
    if str(obs.get("ready") or "") != "complete":
        return "wait"
    if int(obs.get("quiet_ms") or 0) < quiet_ms:
        return "wait"
    if inflight or inflight_value:
        if value_answered and not inflight_value:
            return "value_answered"
        if stable_ms and not inflight_value and int(obs.get("quiet_ms") or 0) >= stable_ms:
            return "dom_stable"
        return "wait"
    return "quiet"


def value_forms(value: Any) -> Tuple[str, ...]:
    """The lower-cased forms in which a page may send ``value``: as typed and as each deterministic
    transform a URL binding uses (``param_binding.TRANSFORMS``: lower, hyphen, slug); empty forms are
    dropped."""
    v = str(value or "").strip()
    if not v:
        return ()
    from anyuse.execution.param_binding import TRANSFORMS
    out: List[str] = []
    for _name, fn in TRANSFORMS:
        try:
            form = str(fn(v) or "").strip().lower()
        except Exception:                              # noqa: BLE001 - a transform that cannot apply
            continue
        if form and form not in out:
            out.append(form)
    return tuple(out)


def carries_value(url: Any, post: Any, forms: Sequence[str]) -> bool:
    """Does a request carry one of ``forms`` (:func:`value_forms`) in its URL or body: raw,
    percent-encoded, plus-encoded or JSON-escaped, or plain once the request is decoded?
    The host is not read: an input that is part of the site's own name (``crates`` on
    crates.io) would otherwise be carried by every request to it."""
    if not forms:
        return False
    import json
    import urllib.parse
    where = str(url or "")
    try:
        sp = urllib.parse.urlsplit(where)
        if sp.netloc:
            where = urllib.parse.urlunsplit(("", "", sp.path, sp.query, sp.fragment))
    except ValueError:                                 # an unparsable URL is read whole
        pass
    blob = (where + " " + str(post or "")).lower()
    for f in forms:
        if not f:
            continue
        if any(x and x in blob for x in (f, urllib.parse.quote(f).lower(),
                                          urllib.parse.quote_plus(f).lower(),
                                          json.dumps(f)[1:-1])):
            return True
    try:
        plain = urllib.parse.unquote_plus(blob)
    except Exception:                                  # noqa: BLE001 - undecodable stays as sent
        return False
    return any(f and f in plain for f in forms)


def inflight_requests(events: List[dict], value: str = "",
                      types=("XHR", "Fetch"), since: Optional[int] = None) -> Dict[str, int]:
    """Same-page XHR/fetch requests sent and not yet finished or failed, from the
    session's CDP event log; ``value`` counts those whose URL or body carries it.

    With ``since`` (an index into ``events``, taken before an action), ``answered_value``
    counts the requests carrying the value that were sent at or after it and finished
    (a failed one does not count)."""
    open_: Dict[str, dict] = {}
    #: request id -> (event index of its requestWillBeSent, the request), while open
    sent: Dict[str, Tuple[int, dict]] = {}
    answered: List[dict] = []
    for i, e in enumerate(events):
        if e.get("sessionId"):
            continue
        m, p = e.get("method"), e.get("params") or {}
        rid = p.get("requestId")
        if not rid:
            continue
        if m == "Network.requestWillBeSent" and str(p.get("type") or "") in types:
            open_[rid] = p.get("request") or {}
            sent[rid] = (i, open_[rid])
        elif m in ("Network.loadingFinished", "Network.loadingFailed"):
            open_.pop(rid, None)
            at, req = sent.pop(rid, (-1, {}))
            if m == "Network.loadingFinished" and since is not None and at >= since:
                answered.append(req)
    forms = value_forms(value)
    with_value = sum(1 for r in open_.values()
                     if carries_value(r.get("url"), r.get("postData"), forms))
    out = {"inflight": len(open_), "value": with_value}
    if since is not None:
        out["answered_value"] = sum(1 for r in answered
                                    if carries_value(r.get("url"), r.get("postData"), forms))
    return out


async def wait_settled(session, ceiling_s: float, *, value: str = "",
                       quiet_ms: int = DEFAULT_QUIET_MS, poll_s: float = DEFAULT_POLL_S,
                       inflight_grace_s: float = 0.0, stable_ms: int = 0,
                       since: Optional[int] = None,
                       clock: Callable[[], float] = time.monotonic) -> SettleReport:
    """Pump the session's events until :func:`decide` ends the wait. ``since``: the index
    into the session's event log taken before the action; the requests carrying ``value``
    sent from there on that answered may end the wait (:func:`inflight_requests`)."""
    rep = SettleReport()
    t0 = clock()
    unanswered = 0
    while True:
        elapsed = clock() - t0
        try:
            obs = await session.js(SETTLE_PROBE_JS)
        except Exception:
            obs = None
        if not isinstance(obs, dict):
            unanswered += 1
            if unanswered >= 3:
                # the page cannot answer the probe: the timer's remaining time
                left = max(0.0, ceiling_s - elapsed)
                if left:
                    await session.settle(left)
                rep.ended, rep.waited_s = "timer", clock() - t0
                rep.notes.append("the page did not answer the settle probe")
                return rep
        else:
            unanswered = 0
            rep.mutations = int(obs.get("mutations") or 0)
        fl = inflight_requests(getattr(session, "_events", []) or [], value, since=since)
        why = decide(obs, elapsed, ceiling_s=ceiling_s, quiet_ms=quiet_ms,
                     inflight=fl["inflight"], inflight_value=fl["value"],
                     inflight_grace_s=inflight_grace_s, stable_ms=stable_ms,
                     value_answered=fl.get("answered_value", 0))
        rep.polls += 1
        if why != "wait":
            rep.ended, rep.waited_s, rep.inflight_at_end = why, clock() - t0, fl["inflight"]
            return rep
        await session.settle(poll_s)


# ---------------------------------------------------------------------------
# the page settle after an action or a navigation
# ---------------------------------------------------------------------------
#: after an agent action, a replay's navigation or a proof replay's search: the document complete, the DOM quiet for
#: ``DEFAULT_QUIET_MS`` and no XHR / fetch of the page in flight (a DOM quiet for ``PAGE_SETTLE_STABLE_MS`` ends it
#: although a long poll stays open), at the latest after ``PAGE_SETTLE_CEILING_S``. Waiting sends nothing.
PAGE_SETTLE_CEILING_S = 5.0
PAGE_SETTLE_STABLE_MS = 1200


#: Loading announcements (www.drako.it's results page was read while its grid still said "Caricamento
#: prodotti..." in a role=status box): the page ANNOUNCES it is still loading - a visible status / progressbar /
#: aria-busy region of the main content, or a loading / spinner / skeleton box whose short text says loading (the
#: languages ``LOADING_JS`` lists) - and the settle waits for the announcement to go, at most ``LOADING_WAIT_S``
#: more. Waiting sends nothing.
LOADING_WAIT_S = 8.0
LOADING_POLL_S = 0.5
LOADING_JS = r"""
(() => { /*__anyuseLoading*/
  const re = /(loading|lädt|laden|wird geladen|caricamento|chargement|cargando|carregando|bezig met laden|laddar|indlæser|laster inn|ładowanie|wczytywanie|načítání|betöltés|yükleniyor|загрузка)|^\s*(…|\.\.\.)\s*$/i;
  const vis = n => { const r = n.getBoundingClientRect(); if (!r.width || !r.height) return false;
    const cs = getComputedStyle(n); return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
  const sel = '[role=status],[role=progressbar],[aria-busy=true],[class*=loading],[class*=Loading],[class*=spinner],' +
              '[class*=skeleton],[id*=loading]';
  for (const n of document.querySelectorAll(sel)) {
    if (!vis(n)) continue;
    const t = String(n.innerText || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (n.getAttribute('aria-busy') === 'true' && n.closest('main,[role=main]'))
      return {why: 'aria-busy', text: t.slice(0, 80)};
    if (t && t.length <= 80 && re.test(t)) return {why: n.getAttribute('role') || 'loading box', text: t.slice(0, 80)};
  }
  return null;
})()
"""


async def loading_announced(page) -> Optional[Dict[str, Any]]:
    """``{"why", "text"}`` when the page announces it is still loading (``LOADING_JS``), else None (also for a page
    that cannot answer)."""
    try:
        got = await page.js(LOADING_JS)
    except Exception:                                  # noqa: BLE001 - no answer is no announcement
        return None
    if isinstance(got, dict) and got.get("why") and "text" in got and "quiet_ms" not in got:
        return {"why": str(got.get("why"))[:30], "text": str(got.get("text") or "")[:80]}
    return None


async def settle_page(page, *, ceiling_s: float = PAGE_SETTLE_CEILING_S, trail: Optional[List[dict]] = None,
                      clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Wait until the page settled (:func:`decide`, the one rule); the report is appended to ``trail`` (the record:
    how the wait ended, how long, the mutations, the requests still open). A page that still announces it is
    loading (``loading_announced``) is waited for, at most ``LOADING_WAIT_S`` more (recorded as ``loading``)."""
    rep = await wait_settled(page, ceiling_s, stable_ms=PAGE_SETTLE_STABLE_MS, clock=clock)
    d = rep.to_dict()
    load = await loading_announced(page)
    if load:
        t0 = clock()
        ended = "ceiling"
        while clock() - t0 < LOADING_WAIT_S:
            await page.settle(LOADING_POLL_S)
            if not await loading_announced(page):
                ended = "gone"
                break
        if ended == "gone":
            # the list it announced renders now: the one settle rule once more (bounded as before)
            await wait_settled(page, ceiling_s, stable_ms=PAGE_SETTLE_STABLE_MS, clock=clock)
        d["loading"] = {"why": load["why"], "text": load["text"], "ended": ended, "waited_s": round(clock() - t0, 2)}
    if trail is not None and len(trail) < 80:
        trail.append(d)
    return d


class PageSettle:
    """The ``settle(page)`` callable every browser path takes, with its reports kept in ``trail``."""

    def __init__(self, ceiling_s: float = PAGE_SETTLE_CEILING_S):
        self.ceiling_s = ceiling_s
        self.trail: List[dict] = []

    async def __call__(self, page) -> None:
        await settle_page(page, ceiling_s=self.ceiling_s, trail=self.trail)

