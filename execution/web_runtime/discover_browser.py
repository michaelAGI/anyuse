"""The guarded container-browser arm of execution/web_runtime/discover_bench.py (v3).

The v2 arm charged a page load's sub-requests to the pacer only AFTER
the load, and one load sent 1,011 requests to one host. v3 replaces it. Every
browser session is an :class:`execution.cdp_guard.GuardedSession`, in which
every request, sub-requests and redirect hops included, is decided and paced
before it is sent, under a 30-per-load and 200-per-host cap. After each
session the run-wide :class:`RequestBudget` is checked, and a violation raises
:class:`TrafficViolation`, which stops the run.

Headless Chromium runs ONLY inside Apple's ``container``
(``chromedp/headless-shell``). Nothing here touches the desktop.

* ``BrowserFetcher.get`` reads a page with ONE plain GET first. It renders the
  page in the guarded browser only when the GET does not carry it (few links,
  little text, an empty SPA root), and then returns the rendered DOM. A host
  whose home page needed rendering is a JS host, and its proof pages are
  recorded in the browser (``navigator``).
* ``search_probe`` types a site value into the visible search input and
  reports the same-host XHR/fetch URLs and the document URL (Enter).
* ``screenshot`` is the ARM B L3 view of a page (the container page only).
* There is no tier-2 replay in v3: the replay driver is not guarded.

v4:

* ``typed_open`` and ``replay`` give the discovery proof a GUARDED tier-2
  replay (``execution/discover_replay.py``): navigate mode and typed mode.
* Request economy, caps unchanged: the run cache (``RequestBudget.cache``) is
  on. The plain GET of a page that is then rendered seeds the cache with that
  document, so the render does not fetch it again; every session's same-host
  GET responses are harvested into it; a request for a URL this run already
  fetched is answered from it and never sent. A URL carrying the session's own
  input value is always fetched fresh.
"""
from __future__ import annotations

import asyncio
import functools
import re
import time
import urllib.parse

from anyuse.execution import discover as D
from anyuse.execution.agent_browser import AgentRun, Navigator
from anyuse.execution.cdp_guard import GuardedSession, RequestBudget, TrafficViolation  # noqa: F401

_SPA_ROOT = re.compile(r"<(div|main|section)\s[^>]*id=[\"'](root|app|__next|__nuxt|main-app)"
                       r"[\"'][^>]*>\s*</\1>|<app-root[^>]*>\s*</app-root>", re.I)


def needs_render(body: str, url: str) -> bool:
    """Does a plain GET NOT carry this page? (fixed in the protocol)"""
    if _SPA_ROOT.search(body or ""):
        return True
    scan = D.scan_page(body or "", url)
    host = D.host_of(url)
    same = {ln.url for ln in scan.links if D.host_of(ln.url) == host}
    txt = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", body or "")
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    return len(same) < 10 or len(" ".join(txt.split())) < 400


class BrowserFetcher(D.HttpFetcher):
    """Same gate, pacer, request cap and block list as the HTTP fetcher."""

    MAX_RENDERS = 4

    def __init__(self, endpoint: str, http: D.HttpFetcher, budget: RequestBudget,
                 session_factory=None, ceiling_s: float = 20.0, cache: bool = False):
        super().__init__(user_agent=http.user_agent, allow_url=http.allow_url,
                         pacer=http.pacer, max_requests_per_host=http.max_requests)
        self.blocked = http.blocked          # one block list for both
        self.log = http.log
        self.refused = http.refused
        self.endpoint = endpoint
        self.http = http
        self.budget = budget
        self.page_info = {}
        self.js_hosts = set()
        self.renders = {}
        self.sessions = 0
        self._factory = session_factory or functools.partial(
            GuardedSession, user_agent=self.user_agent, budget=budget)
        self._settler = Navigator(endpoint, ceiling_s=ceiling_s)
        budget.cache_enabled = bool(cache)
        budget.bind_fetcher(self)            # the site ceiling holds here too
        #: page host -> {third-party host: requests the guard failed}; report only
        self.third_party = {}
        self.harvested = 0

    def _allowed(self, url):
        try:
            return bool(self.allow_url(url)) and not D._ACCOUNT.search(
                urllib.parse.urlsplit(url).path)
        except Exception:
            return False

    # -- one guarded session -------------------------------------------------

    async def _session(self, url: str, value: str = "", inputs: bool = False,
                       probe_value: str = "", screenshot: bool = False,
                       want_input: dict = None):
        run = AgentRun(value=value, task=f"open {url}", start_url=url)
        extra = {}
        t0 = time.monotonic()
        fresh = tuple(v for v in (value, probe_value) if v)
        kw = {"fresh_values": fresh} if self.budget.cache_enabled else {}
        try:
            async with self._factory(self.endpoint, allow_url=self._allowed, **kw) as page:
                await page.goto(url)
                await self._settler._settle(page, t0)
                run.steps = [{"action": "navigate", "url": url}]
                if inputs:
                    extra["search_inputs"] = await page.search_inputs()
                if probe_value:
                    # the pre-typing page: the typed ground truth compares against it
                    run.before_dom = await page.dom()
                    extra["probe"] = await page.type_search(probe_value, want=want_input)
                    run.steps.append({"action": "type", "input": want_input or
                                      (extra["probe"] or {}).get("input")})
                    run.typed = [probe_value]
                if screenshot:
                    extra["screenshot"] = await page.screenshot()
                run.dom = await page.dom()
                run.text = await page.text()
                run.final_url = await page.url()
                run.network = await page.network()
                extra["failed"] = list(page.guard.failed[:40])
                extra["sent"] = sum(page.guard.count.values())
                extra["cache_hits"] = getattr(page.guard, "cache_hits", 0)
                if self.budget.cache_enabled and hasattr(page, "harvest_cache"):
                    self.harvested += await page.harvest_cache()
                if page.refused:
                    run.steps.append({"robots_refused": page.refused[:5]})
        except TrafficViolation:
            raise
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"[:200]
        run.seconds = time.monotonic() - t0
        return run, extra

    def open(self, url: str, value: str = "", **kw):
        why = self.refusal(url) or ("account/login/cart path: not opened"
                                    if D._ACCOUNT.search(urllib.parse.urlsplit(url).path) else "")
        if why:
            return AgentRun(value=value, start_url=url, error=why), {}
        host = D.host_of(url)
        run, extra = asyncio.run(self._session(url, value, **kw))
        self.sessions += 1
        # report only: third-party hosts the page tried to reach (never sent)
        for _u, why in extra.get("failed") or []:
            if why.startswith("third-party host "):
                tp = self.third_party.setdefault(host, {})
                k = why[len("third-party host "):]
                tp[k] = tp.get(k, 0) + 1
        self.budget.check()                  # raises TrafficViolation: stop the run
        status = 0
        for r in run.network or []:
            if r.get("resource_type") == "Document" and r.get("status"):
                status = int(r["status"])
        self.log.append({"url": url, "status": status, "via": "browser",
                         "requests": extra.get("sent", 0),
                         "cache_hits": extra.get("cache_hits", 0), "error": run.error[:80]})
        if run.dom:
            out = D.Fetched(url=url, final_url=run.final_url or url, status=status or 200,
                            headers={"content-type": "text/html"}, body=run.dom)
            self._note_block(out)
            if out.error.startswith("blocked"):
                run.error = out.error
        if run.final_url and D.host_of(run.final_url) != host and not run.error:
            run.error = f"redirect off the host: {run.final_url[:80]}"
        return run, extra

    # -- the fetcher's surface ---------------------------------------------

    def get(self, url: str, max_hops: int = 5) -> D.Fetched:
        r = self.http.get(url, max_hops)
        path = urllib.parse.urlsplit(url).path.lower()
        if path.endswith((".xml", ".txt", ".gz")) or "opensearch" in url.lower():
            return r                                   # machine files: read as text
        if r.error or not (200 <= int(r.status or 0) < 300):
            return r
        if "html" not in (r.mime or "text/html") or not needs_render(r.body, r.final_url or url):
            return r
        host = D.host_of(url)
        # (two JS hosts spent the 200-request cap in discovery renders):
        # at most MAX_RENDERS rendered discovery pages per host; later ones stay raw
        if self.renders.get(host, 0) >= self.MAX_RENDERS:
            return r
        self.renders[host] = self.renders.get(host, 0) + 1
        # v4: the render does not fetch the document again: the plain GET's
        # answer is handed to the browser ONCE (guard hardening: a one-shot
        # handoff, not a cache; never for a URL carrying an input value)
        self.budget.handoff(r.final_url or url, r.status, {"content-type": r.headers.get(
            "content-type") or r.headers.get("Content-Type") or "text/html; charset=utf-8"},
            r.body)
        run, extra = self.open(r.final_url or url, inputs=True)
        if host in self.blocked:
            return D.Fetched(url=url, final_url=run.final_url or url, status=r.status,
                             error=f"blocked: {self.blocked[host]}")
        if run.error or not run.dom:
            return r                                   # keep the plain page
        final = run.final_url or url
        self.page_info[final] = self.page_info[url] = {
            "rendered": True, "search_inputs": extra.get("search_inputs") or []}
        if urllib.parse.urlsplit(url).path in ("", "/"):
            self.js_hosts.add(host)
        return D.Fetched(url=url, final_url=final, status=r.status or 200,
                         headers={"content-type": "text/html"}, body=run.dom,
                         ms=run.seconds * 1000.0)

    def navigator(self, url: str, value: str) -> AgentRun:
        run, _extra = self.open(url, value)
        return run

    def typed_open(self, url: str, value: str, input_spec: dict) -> AgentRun:
        """v4 typed search: open ``url``, type ``value`` into the recorded input,
        press Enter, return the page as it then is (the guarded browser)."""
        run, extra = self.open(url, value, probe_value=value, want_input=dict(input_spec or {}))
        obs = extra.get("probe") or {}
        if not run.error and obs.get("note"):
            run.error = f"typing failed: {obs['note']}"
        return run

    def replay(self):
        """The discovery tier-2 executor over this guarded browser."""
        from anyuse.execution.discover_replay import GuardedReplay
        return GuardedReplay(self.navigator, self.typed_open,
                             host_blocked=lambda h: self.blocked.get(h, "") or
                             self.budget.stopped.get(h, ""))

    def search_probe(self, url: str, value: str) -> dict:
        run, extra = self.open(url, probe_value=value, inputs=True)
        obs = dict(extra.get("probe") or {})
        if run.error:
            obs["note"] = (obs.get("note", "") + f" {run.error}").strip()
        return obs

    def screenshot(self, url: str) -> str:
        run, extra = self.open(url, screenshot=True)
        return "" if run.error else str(extra.get("screenshot") or "")
