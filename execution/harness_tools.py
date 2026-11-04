"""The default tool sets of the agent harness, as typed actions behind the EXISTING guards.

* ``BrowserTools``: a duck-typed page (``goto``, ``js``, ``dom``, ``type_search``, ``search_inputs``,
  ``network``, ``url``, ``settle``): the guarded container page live (``cdp_guard.GuardedSession``),
  ``dom_page.DomPage`` offline. Observation and execution are the web demonstrator's own
  (``demo_policy.observe`` / ``execute``), so a harness trace IS a demonstration trace.
* ``DesktopTools``: the guest job agent as a callable ``guest(op, **args) -> dict`` (the closed op set
  of ``execution/guest/desktop_e2e_job.py``; ``ax_step`` runs one planned AX step through the checked
  executor with the guest's guards). Needs one more op, ``ax_elements`` (section 9 of the doc).
* ``HttpTools``: GET by default through one injected transport; POST only under the read-only POST
  approvals; every URL through the destination policy, the gov list and robots.
* ``FileTools``: the per-task workspace; nothing outside it.
* ``RuntimeTools``: the proven runtime-written tools (``harness_toolwrite``).
* ``Toolbox``: routes an action to its set; ``observe``, ``available``, ``effect_of``, ``gate``, ``dispatch``.

PURE: every backend is injected; no browser, AX, subprocess or socket is opened here.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import harness_types as T

HONEST_UA = "anyuse-agent-harness/0.1 (+read-only; respects robots.txt)"
HTTP_MAX_BYTES = 200_000
#: Return / Escape, the only keys an AX replay presses (``desktop_e2e.ALLOWED_KEYS``)
KEY_CODES = {"return": 36, "enter": 36, "escape": 53, "esc": 53}
#: the HIL classifier's read of a label (``draft_compile._LABEL_VERB``)
_LABEL_VERB = {"ax_press": "click", "ax_toggle": "click", "ax_enter_text": "type", "ax_key": "key"}


def run_sync(coro, loop=None):
    """Run one coroutine of an async page/transport from the synchronous loop (``loop``: a live session's
    own loop; without one a fresh loop per call, which an offline page does not mind)."""
    if loop is not None:
        return loop.run_until_complete(coro)
    own = asyncio.new_event_loop()
    try:
        return own.run_until_complete(coro)
    finally:
        own.close()


def gov_refusal(url: str) -> str:
    """With ``ANYUSE_REFUSE_GOV_HOSTS`` on, a government host is never contacted (fail closed); ``""`` otherwise."""
    from anyuse.execution.gov_hosts import government_refusal
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    if host and government_refusal(host):
        return f"government host {host!r} is never contacted (ANYUSE_REFUSE_GOV_HOSTS is on)"
    return ""


def label_effect(action: T.Action, label: str) -> str:
    """The effect class the LABELS of an action imply (never lower than the declared one)."""
    from anyuse.execution.desktop_e2e import irreversible_reason
    from anyuse.execution.hil_gate import StepClass, classify_step
    if irreversible_reason(label):
        return T.IRREVERSIBLE
    verb = _LABEL_VERB.get(action.kind, "click")
    cls = classify_step({"action": verb, "args": {"label": label, "text": getattr(action, "text", "")}})
    if cls == StepClass.DESTRUCTIVE:
        return T.IRREVERSIBLE
    return T.max_effect(action.effect, T.WRITE if cls == StepClass.SIDE_EFFECTING and action.effect != T.READ
                        else action.effect)


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------
async def _settle_quiet(page) -> None:
    await page.settle(0.0)


class BrowserTools:
    """The browser tool set over a duck-typed page (section 3). ``allow_url(url) -> bool`` is the
    robots gate the page's own guard also consults; ``related_origins``: the site's own API/CDN
    origins the run lists explicitly."""

    toolset = "browser"

    def __init__(self, page, start_url: str, site: str = "", *, related_origins: Sequence[str] = (),
                 allow_url: Optional[Callable[[str], bool]] = None, settle=None, workflow: str = "site_search",
                 loop=None):
        self.page = page
        #: a live session's websockets must stay on ONE event loop: the product passes the loop it opened
        #: the session on; an offline page (``DomPage``) needs none
        self.loop = loop
        self.start_url = start_url
        self.site = site or (urllib.parse.urlsplit(start_url).hostname or "")
        self.related_origins = tuple(related_origins)
        self.allow_url = allow_url
        self.settle = settle or _settle_quiet
        self.workflow = workflow
        self.history: List[str] = []
        self._first = True
        self._opened = False
        #: a same-site redirect of the start page (``site_canon``): ``{from, to}``
        self.redirected: Dict[str, str] = {}
        #: why a search's value went to a refused third-party host ("" when it did not)
        self.refused_provider = ""

    # -- observation --------------------------------------------------------------------------
    def observe(self) -> T.Observation:
        from anyuse.execution import demo_policy as DPOL
        from anyuse.execution import site_canon as SC
        from anyuse.execution.live_solve import challenge_evidence, model_may_see
        from anyuse.execution.web_arch_run import site_block
        start = not self._opened
        if not self._opened:
            run_sync(self.page.goto(self.start_url), self.loop)
            run_sync(self.settle(self.page), self.loop)
            self._opened = True
        rec, obs, mapping = run_sync(DPOL.observe(self.page), self.loop)
        empty = not (obs.get("elements") or []) and not str(obs.get("text") or rec.get("text") or "").strip()
        if not SC.is_error_page(str(rec.get("url") or "")) and \
                ((start and empty) or (self.REREAD_EMPTY and (empty or str(rec.get("ready") or "") == "loading"))):
            # the first read can come before the page is drawn - read once more; (a search's results page read
            # while still loading can show no element and no text, or a promo banner and no result, and the agent
            # would answer from it): every page, and a page still loading
            run_sync(self.settle(self.page), self.loop)
            rec, obs, mapping = run_sync(DPOL.observe(self.page), self.loop)
        try:
            net = run_sync(self.page.network(want_bodies=False), self.loop)
        except Exception:  # noqa: BLE001
            net = []
        url0 = str(rec.get("url") or "")
        if start and SC.is_error_page(url0):
            # the start page did not load: a typed reason (dns, tls, a redirect off the site, ...), never an error
            # page handed to the policy (such as "No site URL provided")
            cls, detail = SC.classify_failure(self.page, url=self.start_url)
            self._first = False
            return T.Observation(kind="web", url=url0, blocked=f"start failed ({cls}): {detail}"[:300])
        if start and not (obs.get("elements") or []) and not str(obs.get("text") or rec.get("text") or "").strip() \
                and not (rec.get("inputs") or []):
            self._first = False
            ev = refusal_evidence(self.page, self.start_url)
            return T.Observation(kind="web", url=url0, blocked=("start failed (empty_page): the start page shows no "
                                                                "element and no text a reader could use"
                                                                + (f" ({ev})" if ev else ""))[:400])
        if start:
            h0, h1 = (urllib.parse.urlsplit(self.start_url).hostname or "").lower(), \
                (urllib.parse.urlsplit(url0).hostname or "").lower()
            if h1 and h1 != h0 and SC.is_same_site_redirect(self.start_url, url0):
                # a same-site redirect (www <-> apex, http -> https) is followed and recorded; the page's own host is
                # the site from here on (the compiled tool's site)
                self.redirected = {"from": self.start_url, "to": url0}
                self.site, self.start_url = h1, url0
                self.related_origins = tuple(dict.fromkeys(list(self.related_origins) + SC.sibling_origins(url0)))
        blocked = site_block(str(rec.get("text") or ""), str(rec.get("url") or ""), net, start=self._first)
        self._first = False
        runaway = runaway_stop(self.page)
        if runaway:
            # a page load hit the browser statics' runaway guard - the run's typed stop
            return T.Observation(kind="web", url=url0, blocked=runaway[:300])
        if not blocked and not model_may_see(rec):
            blocked = "challenge page: " + (challenge_evidence(rec) or "quarantined")[:120]
        els: List[T.Element] = []
        for it in obs.get("elements") or []:
            n = int(it.get("n"))
            m = mapping.get(n) or {}
            # the element's FULL text (the renderer cuts it with the mark; the answer resolver reads it whole)
            full = " ".join(str(m.get("text") or "").split()) if m.get("src") == "el" else ""
            els.append(T.Element(index=n, kind=str(it.get("kind") or ""), text=full or str(it.get("text") or ""),
                                 href=str(m.get("href") or ""), tag=str(m.get("tag") or ""),
                                 card=str(it.get("card") or ""),
                                 options=[str(o.get("text") or "") for o in (m.get("options") or [])][:40]))
        url = str(rec.get("url") or "")
        els = self._with_changed_region(els, mapping, rec)
        els = self._new_first(els, url, rec)
        if not self.history or self.history[-1] != url:
            self.history.append(url)
        return T.Observation(kind="web", url=url, title=str(rec.get("title") or ""),
                             text=str(obs.get("text") or rec.get("text") or "")[:6000], elements=els,
                             inputs=[dict(i) for i in rec.get("inputs") or []], page=rec, mapping=dict(mapping),
                             lists=search_url_line(els, rec),
                             blocked=("site block: " + blocked) if blocked else "")

    #: at most this many links of the changed region are added when the observation's element cap left them out
    CHANGED_EXTRA = 40
    #: when the page names link containers, the container decides what changed (``_new_first``)
    NEW_BY_CONTAINER = True
    #: an empty read (no element, no text) of ANY page, or a read while the document is still loading, is read
    #: once more after settling
    REREAD_EMPTY = True

    def _with_changed_region(self, els: List[T.Element], mapping: Dict[int, Dict[str, Any]],
                             rec: Dict[str, Any]) -> List[T.Element]:
        """A link of the page's CHANGED region (a container the previous page did not have) that the capped
        observation left out is added, numbered after the others, with the demonstrator's own mapping entry (so it
        can be clicked and answered). Without it, a results page whose mega menu comes first in the document fills the
        observation's element cap, and its products are never listed."""
        from anyuse.execution import answer_refs as AR
        from anyuse.execution import ax_tree as AXT
        prev_sigs = getattr(self, "_prev_sigs", None)
        sigs = (rec or {}).get("link_sigs") or {}
        if not prev_sigs or not sigs:
            return els
        listed = {AR._norm(e.href) for e in els if e.href}
        n = max([e.index for e in els] + list(mapping) + [-1]) + 1
        added = 0
        # (a search typed into the FOOTER's box can draw its results into
        # the footer's own results container - left out as chrome, so no result link reached the policy): right after
        # an in-page search, a NEW container in the page's chrome whose links name the typed value is the search's own
        # results, listed like any changed region
        from anyuse.execution import web_arch as W
        typed = str(getattr(self, "_in_page_value", "") or "") if getattr(self, "_in_page_search", False) else ""
        value_sigs: set = set()
        if typed:
            tv = W.fold(typed)
            for e in (rec or {}).get("elements") or []:
                href = str(e.get("href") or "")
                if e.get("tag") == "a" and href and tv and tv in W.fold(str(e.get("text") or "")):
                    value_sigs |= {_local_sig(x) for x in sigs.get(href) or []} - prev_sigs
        for e in (rec or {}).get("elements") or []:
            if added >= self.CHANGED_EXTRA:
                break
            href = str(e.get("href") or "")
            text = " ".join(str(e.get("text") or "").split())
            if e.get("tag") != "a" or not href.startswith("http") or not text or AR.is_label(text):
                continue
            k = AR._norm(href)
            mine = {_local_sig(x) for x in sigs.get(href) or []}
            if k in listed or not mine or mine <= prev_sigs:
                continue
            if (e.get("area") in ("header", "nav", "footer") or AXT.chrome(e)) and not ((mine - prev_sigs) & value_sigs):
                continue
            mapping[n] = {"src": "el", "i": e.get("i"), "text": e.get("text"), "href": href, "tag": "a"}
            els.append(T.Element(index=n, kind="link", text=text, href=href, tag="a"))
            listed.add(k)
            n += 1
            added += 1
        return els

    def _new_first(self, els: List[T.Element], url: str, rec: Optional[Dict[str, Any]] = None) -> List[T.Element]:
        """The elements in the order the policy should read them (a page of 350 elements whose first 60 are
        the mega menu leaves the agent answering category links or "no results"). On a page reached from another
        page: the search boxes, then the links of the region that CHANGED (their containers - the READ record's link
        signatures, without the page-type body segment - were not on the previous page: the results), then the rest
        (menus and teaser blocks present on both pages, whatever items they rotate); each group in page order. Without link signatures, a link that was not on the previous page counts as
        changed. Also on the SAME URL right after a search that stayed there (an in-page search). The numbers
        (``index``) never change, only the order they are listed in."""
        from anyuse.execution import answer_refs as AR
        sigs = {AR._norm(h): {_local_sig(x) for x in (ss or [])} for h, ss in ((rec or {}).get("link_sigs") or {}).items()
                if h}
        pairs = {(AR._fold(e.text).lower(), AR._norm(e.href)) for e in els if e.href}
        page_sigs = set().union(*sigs.values()) if sigs else set()
        firsts = {AR._norm(h): _local_sig(ss[0]) for h, ss in ((rec or {}).get("link_sigs") or {}).items()
                  if h and isinstance(ss, list) and ss and ss[0]}
        # the links the page shows ONLY in its chrome (header, navigation, footer, a sidebar), by (text, target)
        from anyuse.execution import ax_tree as AXT
        where: Dict[Tuple[str, str], List[bool]] = {}
        menu: set = set()
        for x in (rec or {}).get("elements") or []:
            if x.get("tag") == "a" and x.get("href"):
                where.setdefault((AR._fold(" ".join(str(x.get("text") or "").split())).lower(), AR._norm(x.get("href"))),
                                 []).append(AXT.area_of(x) in _CHROME_AREAS or AXT.chrome(x))
                if AXT.area_of(x) in _MENU_AREAS or str(x.get("ax_landmark") or "") in _MENU_LANDMARKS:
                    menu.add(AR._norm(x.get("href")))
        chrome = {k for k, v in where.items() if all(v)}
        prev, prev_sigs = getattr(self, "_prev_pairs", None), getattr(self, "_prev_sigs", None)
        prev_chrome, prev_menu = getattr(self, "_prev_chrome", None) or set(), getattr(self, "_prev_menu", None) or set()
        self._prev_pairs, self._prev_sigs, self._prev_chrome, self._prev_menu = pairs, page_sigs, chrome, menu
        # (a search that draws in the page - the URL stays - where the policy, shown the first 60 of 150
        # elements in page order, never sees what the search drew at 134-153; it types into the page's other boxes,
        # guesses URLs and runs out of steps): right after a search that stayed on its URL the page is
        # ordered as after a navigation - what the search changed comes first
        in_page = bool(getattr(self, "_in_page_search", False))
        self._in_page_search = False
        if not prev or (url == getattr(self, "_prev_url", "") and not in_page):
            self._prev_url = url
            return els
        self._prev_url = url

        def changed(e: T.Element) -> bool:
            # the (text, target) pair was not on the previous page (a link kept with its text is chrome), and - when
            # the page names link containers - the target sits in a container the previous page did not have (a
            # teaser block that rotates its items keeps its container)
            if not e.href:
                return False
            mine = sigs.get(AR._norm(e.href))
            pair = (AR._fold(e.text).lower(), AR._norm(e.href))
            first_sig = firsts.get(AR._norm(e.href), "")
            if self.NEW_BY_CONTAINER and first_sig and prev_sigs and first_sig not in prev_sigs and pair not in chrome \
                    and pair not in prev_chrome and pair[1] not in menu and pair[1] not in prev_menu:
                # (a search whose FIRST results are the newest articles the start page also teases with the same
                # text - "kept, so chrome" lists them after the menus, beyond the 60 shown, and the answers skip
                # them): when the
                # page names link containers, a link whose FIRST container on the page (the reader lists a target's
                # containers in document order) is one the previous page did not have is new, even if the previous page
                # linked the same target with the same text elsewhere (a teaser block kept above the results keeps its
                # links unchanged; a sidebar's copy below them does not) - in its content, never in its chrome
                # (a link only in this page's chrome stays chrome), and never a target the site's header or navigation
                # links (a category, brand or listing page: a result card's tag, a breadcrumb)
                return True
            if (AR._fold(e.text).lower(), AR._norm(e.href)) in prev:
                return False
            if mine and prev_sigs:
                return not mine <= prev_sigs
            return True
        boxes = [e for e in els if e.kind == "search box"]
        new = [e for e in els if e.kind != "search box" and changed(e)]
        rest = [e for e in els if e not in boxes and e not in new]
        return boxes + new + rest

    # -- effects and the gate -----------------------------------------------------------------
    def _mapped(self, index: int, obs: T.Observation) -> Dict[str, Any]:
        return obs.mapping.get(int(index)) or {}

    def target_label(self, action: T.Action, obs: T.Observation) -> str:
        if hasattr(action, "index"):
            m = self._mapped(action.index, obs)
            return str(m.get("text") or (m.get("input") or {}).get("name") or "")
        return str(getattr(action, "url", "") or "")

    def effect_of(self, action: T.Action, obs: T.Observation) -> str:
        from anyuse.execution.web_arch import forbidden_control
        if action.kind in (T.Click.kind, T.Select.kind):
            m = self._mapped(action.index, obs)
            if forbidden_control(m.get("text"), m.get("href")):
                return T.IRREVERSIBLE
        if action.kind == T.Open.kind and forbidden_control("", action.url):
            return T.IRREVERSIBLE
        return action.effect

    def gate(self, action: T.Action, obs: T.Observation) -> Tuple[str, Optional[str]]:
        from anyuse.execution.demo_compile import same_site
        from anyuse.execution.request_policy import destination_refusal, exact_origin_allowlist
        from anyuse.execution.web_arch import forbidden_control
        if action.kind == T.Open.kind:
            here = obs.url or self.start_url
            u = urllib.parse.urljoin(here, action.url) if not action.url.startswith("http") else action.url
            why = gov_refusal(u)
            if why:
                return why, T.Stop.GOV_HOST
            if not same_site(u, self.site):
                return f"blocked: another website ({urllib.parse.urlsplit(u).hostname})", None
            why = destination_refusal(u, exact_origin_allowlist(self.start_url, self.related_origins))
            if why:
                return f"blocked: {why}", None
            why = forbidden_control("", u)
            if why:
                # as the demonstrator: a forbidden control ends the run, whatever the task says
                return f"forbidden: {why} (the browser never opens it)", T.Stop.REFUSED
            if self.allow_url is not None and not self.allow_url(u):
                return "blocked: robots.txt disallows this URL", None
            return "", None
        if action.kind in (T.Click.kind, T.Select.kind, T.Search.kind, T.Type.kind):
            m = self._mapped(action.index, obs)
            if not m:
                return f"element {action.index} is not on this page", None
            if action.kind in (T.Click.kind, T.Select.kind):
                why = forbidden_control(m.get("text"), m.get("href"))
                if why:
                    return f"forbidden: {why} (the browser never performs it, whatever the task says)", T.Stop.REFUSED
                if m.get("src") != "el":
                    return f"element {action.index} is a search box, not a link or button", None
                href = str(m.get("href") or "")
                if href.startswith("http"):
                    why = gov_refusal(href)
                    if why:
                        return why, T.Stop.GOV_HOST
                    if self.allow_url is not None and same_site(href, self.site) and not self.allow_url(href):
                        return "blocked: robots.txt disallows this link", None
            elif m.get("src") != "input":
                return f"element {action.index} is not a search box", None
        return "", None

    def correction(self, action: T.Action, obs: T.Observation) -> str:
        """What the valid elements for this action are (when the policy names an element that
        is not there or not of that kind; it gets this instead of a wasted step)."""
        boxes = [f"[{n}] {(m.get('input') or {}).get('name') or m.get('text') or 'search'}"
                 for n, m in sorted(obs.mapping.items()) if m.get("src") == "input"]
        if action.kind in (T.Search.kind, T.Type.kind):
            if boxes:
                return "The search boxes on this page: " + ", ".join(boxes[:8]) + "."
            urls = [str(u) for u in ((obs.page or {}).get("search_urls") or []) if u][:2]
            # (no visible search box - an agent guesses search URLs, runs out of steps or opens an apology page -
            # while the page's own code names the search URL): the note names it
            return ("This page lists no search box, but its own search form sends to " + " or ".join(urls) +
                    " - open that URL with the input value in place of {q}.") if urls else \
                "This page lists no search box; open the site's search page or another page instead."
        links = sum(1 for m in obs.mapping.values() if m.get("src") == "el")
        return f"Use the number of a listed link or button ({links} listed)" + \
            (f"; search boxes are {', '.join(boxes[:4])} (use search)." if boxes else ".")

    # -- dispatch -----------------------------------------------------------------------------
    def dispatch(self, action: T.Action, obs: T.Observation) -> T.Outcome:
        from anyuse.execution import demo_policy as DPOL
        from anyuse.execution import web_arch as W
        self._in_page_search = False
        rec = obs.page or {}
        host = urllib.parse.urlsplit(str(rec.get("url") or "")).hostname or ""
        if action.kind == T.Search.kind:
            d = {"action": "search", "element": action.index, "text": action.text}
        elif action.kind == T.Click.kind:
            d = {"action": "click", "element": action.index}
        elif action.kind == T.Select.kind:
            d = {"action": "select", "element": action.index, "option": action.option}
        elif action.kind == T.Open.kind:
            d = {"action": "goto", "url": action.url}
        elif action.kind == T.Back.kind:
            if len(self.history) < 2:
                return T.Outcome(False, "no page to go back to")
            self.history.pop()
            d = {"action": "goto", "url": self.history[-1]}
        elif action.kind == T.Type.kind:
            if not hasattr(self.page, "focus") or not hasattr(self.page, "type"):
                return T.Outcome(False, "this page types only through search (type + Enter): use search")
            m = self._mapped(action.index, obs)
            run_sync(self.page.focus(int((m.get("input") or {}).get("index", 0))), self.loop)
            run_sync(self.page.type(action.text), self.loop)
            return T.Outcome(True, how="typed_only", url_after=str(rec.get("url") or ""))
        elif action.kind == T.Scroll.kind:
            return T.Outcome(True, how="noop", url_after=str(rec.get("url") or ""), observation=obs)
        elif action.kind == T.ReadText.kind:
            text = str(rec.get("main_text") or rec.get("text") or "")
            return T.Outcome(True, how="read", url_after=str(rec.get("url") or ""), data={"text": text[:8000]},
                             observation=_with_note(obs, "TEXT READ:\n" + text[:4000]))
        elif action.kind == T.ReadTable.kind:
            rows = [{"title": str(e.get("text") or "")[:300], "url": str(e.get("href") or "")[:500]}
                    for e in W.content_links(rec, host)][:25]
            return T.Outcome(True, how="read", url_after=str(rec.get("url") or ""), data={"rows": rows},
                             observation=_with_note(obs, "TABLE READ (title | url):\n" + "\n".join(
                                 f"{r['title']} | {r['url']}" for r in rows)))
        elif action.kind == T.ExtractFields.kind:
            facts = W.product_facts(rec.get("jsonld") or [])
            main = facts[0] if facts else {}
            fields = {f: main.get(f) for f in action.fields}
            fields.setdefault("title", str(rec.get("title") or ""))
            return T.Outcome(True, how="read", url_after=str(rec.get("url") or ""), data={"fields": fields},
                             observation=_with_note(obs, "FIELDS: " + json.dumps(fields, ensure_ascii=False)[:1500]))
        else:
            return T.Outcome(False, f"not a browser action: {action.kind}")
        value = str(getattr(action, "text", "") or "")
        from anyuse.execution import site_canon as SC
        mark = SC.marks(self.page)
        posts0 = len(getattr(getattr(self.page, "budget", None), "search_posts", None) or [])
        trail = getattr(self.settle, "trail", None)
        n0 = len(trail) if isinstance(trail, list) else 0
        try:
            # a demonstration's page: several submit controls or search URLs may go to the
            # configured small model (``demo_policy.execute``)
            self.page.submit_chooser = True
        except AttributeError:
            pass
        res = run_sync(DPOL.execute(self.page, d, obs.mapping, self.site, self.settle, value), self.loop)
        out = T.Outcome(bool(res.get("ok")), str(res.get("why") or ""), how=str(res.get("how") or ""),
                        url_after=str(res.get("url_after") or ""), href=str(res.get("href") or ""),
                        input=dict(res.get("input") or {}), forbidden=str(res.get("forbidden") or ""),
                        data={k: v for k, v in res.items() if k in ("option_text", "option_value", "note",
                                                                     "documents", "search_url", "form_url",
                                                                     "submit")})
        # the site's search goes back to its unchanged start page: the typed stop, once the
        # more specific stops below (robots, another site, a POST-only search) are ruled out
        no_site = res.get("stop") == "no_site_search"
        if no_site:
            out.ok = True
        if isinstance(trail, list) and len(trail) > n0:
            out.data["settle"] = [dict(x) for x in trail[n0:]][:6]        # how each wait ended (recorded)
        posts = list(getattr(getattr(self.page, "budget", None), "search_posts", None) or [])[posts0:]
        if posts and action.kind == T.Search.kind:
            # the site's results came from its read-only POST search after typing -
            # recorded, so the compile replays the search by typing (a results URL loaded without typing gets no POST)
            out.data["search_post"] = [{"path": str(p.get("path") or "")[:120], "host": str(p.get("host") or "")}
                                       for p in posts[:2]]
        runaway = runaway_stop(self.page)
        if runaway:
            # the runaway guard stopped the site during this action - the typed stop, before
            # any reading of the error page the stopped navigation left (it would read as an
            # off-site search)
            return T.Outcome(False, runaway[:300], how=out.how, url_after=out.url_after, input=out.input,
                             status="static_runaway")
        if action.kind == T.Search.kind and out.ok:
            # a search that stayed on its URL drew (or did not draw) its results IN the page - the next
            # observation lists what changed first (``_new_first``)
            self._in_page_search = W.norm_url(out.url_after or obs.url) == W.norm_url(obs.url)
            self._in_page_value = value if self._in_page_search else ""
            # the site's search form sends to another site (a hosted search engine,
            # there in a new tab: the page stayed as it was) - the site's search runs elsewhere: a typed stop
            inp = dict(res.get("input") or {})
            target = SC.search_form_target(str((obs.page or {}).get("dom") or ""), obs.url, str(inp.get("name") or ""),
                                           str(inp.get("id") or ""))
            stayed = W.norm_url(out.url_after or obs.url) == W.norm_url(obs.url)
            left = str(out.url_after or "").startswith(("http://", "https://")) and \
                not SC.is_error_page(out.url_after) and SC.leaves_site(out.url_after, obs.url or self.start_url)
            # what the guard refused while the search ran, the typed value in its URL
            wall = search_refusal(self.page, mark, value, obs.url or self.start_url, target=target,
                                  url_after=out.url_after)
            if wall and wall[0] == "robots_search":
                return T.Outcome(False, wall[1], how=out.how, url_after=out.url_after, input=out.input, status=wall[0])
            if wall:
                # a refused request to another site carried the value: kept for the harness - a "no results" answer
                # after it is the typed stop provider_search, never a claim that the site has none (a site may also
                # send its search term to a third party and still show its results)
                self.refused_provider = wall[1]
            if left or (stayed and target and SC.leaves_site(target, obs.url or self.start_url)):
                target = out.url_after if left else target
                return T.Outcome(False, f"off_site_search: the site's search form sends to another site "
                                        f"({SC._host(target)})", how=out.how, url_after=out.url_after, input=out.input,
                                 status="off_site_search")
        if SC.is_error_page(out.url_after):
            # the navigation ended on an error page: a typed reason (lehrerfreund's POST search,
            # refused by the guard, reached the agent as "chrome-error" and a guess)
            cls, detail = SC.classify_failure(self.page, since=mark, url=str(d.get("url") or rec.get("url") or ""))
            if cls == "post_only" and action.kind == T.Search.kind:
                return T.Outcome(False, f"post_only_search: the site's search form sends POST ({detail})",
                                 how=out.how, url_after=out.url_after, input=out.input, status="post_only_search")
            if cls == "cross_site_redirect" and action.kind == T.Search.kind:
                # the site's search runs on another site (a hosted search
                # service); an agent would read the page again and again until max_steps
                return T.Outcome(False, f"off_site_search: the site's search leaves the site ({detail})",
                                 how=out.how, url_after=out.url_after, input=out.input, status="off_site_search")
            out.ok = False
            out.why = f"navigation failed ({cls}): {detail}"[:300]
        if no_site:
            return T.Outcome(False, str(res.get("why") or "")[:300], how=out.how, url_after=out.url_after,
                             input=out.input, status="no_site_search")
        if out.ok and out.url_after and (not self.history or self.history[-1] != out.url_after):
            self.history.append(out.url_after)
        return out


def search_url_line(els: Sequence[T.Element], rec: Dict[str, Any]) -> List[str]:
    """When the observation lists no search box but the page's own code names its search URL (the reader's
    ``search_urls``: a GET search form, an OpenSearch / SearchAction template), one line naming it - the policy can open
    it instead of guessing a URL (a guessed URL runs out of steps or opens an apology page with a guessed
    key)."""
    if not els or any(e.kind == "search box" for e in els):
        return []
    here = urllib.parse.urlsplit(str((rec or {}).get("url") or ""))
    urls = [str(u) for u in (rec or {}).get("search_urls") or [] if u][:2]
    # never on the search page itself: its own path, or its own search parameter filled (a search already run)
    filled = {k for k, v in urllib.parse.parse_qsl(here.query, keep_blank_values=True) if v.strip()}

    def searched_here(u: str) -> bool:
        t = urllib.parse.urlsplit(u)
        keys = {k for k, v in urllib.parse.parse_qsl(t.query, keep_blank_values=True) if "{q}" in v}
        return bool(keys & filled) or (t.path.rstrip("/") == here.path.rstrip("/") and bool(here.path.strip("/")))
    urls = [u for u in urls if not searched_here(u)]
    if not urls:
        return []
    return ["SITE SEARCH URL (this page shows no search box; its own search form sends to): " + " | ".join(urls) +
            " - open it with the input value in place of {q}"]


def runaway_stop(page: Any) -> str:
    """``"static runaway: ..."`` when the run's guard stopped a site at the browser statics' runaway guard (a real
    loop: ``cdp_guard.STATIC_RUNAWAY_REPEAT`` / ``_DISTINCT``), else ``""``."""
    runaway = getattr(getattr(page, "budget", None), "runaway", None)
    if not isinstance(runaway, dict) or not runaway:
        return ""
    site, why = next(iter(runaway.items()))
    return f"static runaway: {why}" if not str(why).startswith("static runaway") else str(why)


def refusal_evidence(page: Any, start_url: str, mark: Optional[Dict[str, int]] = None) -> str:
    """What the guard refused that a page needs to draw itself (a start page that draws nothing because every
    script and style comes from an asset subdomain of its own, which the origin policy refuses; such hosts are
    allowed by default) - requests to the SITE'S OWN hosts (same registrable domain;
    ``ANYUSE_ALLOW_SITE_OWN_HOSTS``) and requests robots.txt disallows. "" when it refused neither."""
    from anyuse.execution import web_scope as WS
    failed = list(getattr(getattr(page, "guard", None), "failed", None) or [])[int((mark or {}).get("failed", 0)):]
    own_dom = WS.registrable((urllib.parse.urlsplit(str(start_url or "")).hostname or "").lower())
    own: List[str] = []
    robots: Dict[str, int] = {}
    other: Dict[str, int] = {}
    for u, w in failed:
        h = (urllib.parse.urlsplit(str(u)).hostname or "").lower()
        w = str(w or "")
        if w.startswith("third-party host") and own_dom and WS.registrable(h) == own_dom and h not in own:
            own.append(h)
        elif w.startswith("third-party host") and h and not (own_dom and WS.registrable(h) == own_dom):
            other[h] = other.get(h, 0) + 1
        elif w.startswith("robots"):
            robots[h] = robots.get(h, 0) + 1
    parts = []
    if own:
        try:
            from anyuse.settings import get as get_setting
            on = bool(get_setting("ANYUSE_ALLOW_SITE_OWN_HOSTS"))
        except Exception:                              # noqa: BLE001 - unreadable: say what is known
            on = True
        # the site's own hosts are allowed by default - one refused then is a tracking
        # label, a listed ad/tracker host or a non-GET request
        why = ("the scope still refuses it: a tracking label, a listed ad/tracker host or not GET/HEAD" if on
               else "ANYUSE_ALLOW_SITE_OWN_HOSTS is off")
        parts.append(f"the guard refused the site's own host(s) {', '.join(own[:3])} ({why})")
    if robots:
        # (a granted search provider's robots.txt that gives no usable answer must not read as
        # "robots.txt disallows"): the hosts are named, and a robots.txt that could not be read is said apart
        # (fail closed: a 5xx, a network failure, a redirect the pre-send policy refused) - never "disallows"
        try:
            from anyuse.execution.api_permission import robots_status
        except Exception:                              # noqa: BLE001 - unreadable: say what is known
            robots_status = None                       # type: ignore[assignment]
        said, unread, unknown = [], [], []
        for h, n in sorted(robots.items(), key=lambda x: -x[1]):
            st, note = robots_status(h) if robots_status is not None and h else ("unknown", "")
            (unread if st == "unreadable" else unknown if st == "unknown" else said).append((h, n, note))
        if said:
            parts.append(f"{sum(n for _h, n, _w in said)} request(s) robots.txt disallows (" +
                         ", ".join(f"{h}: {n}" for h, n, _w in said[:3]) + ")")
        if unread:
            parts.append(f"{sum(n for _h, n, _w in unread)} request(s) refused because robots.txt could not be read (" +
                         "; ".join(f"{h}: {n} - {_short_robots_note(w)}" for h, n, w in unread[:3]) + ")")
        if unknown:
            parts.append(f"{sum(n for _h, n, _w in unknown)} request(s) refused because robots.txt was not read (" +
                         ", ".join(f"{h}: {n}" for h, n, _w in unknown[:3]) + "; fail closed)")
    # (scripts from a CDN pull zone on another domain): another host
    # the page asked for again and again is the page's own asset host on another domain (ANYUSE_THIRD_PARTY decides it)
    heavy = sorted((h for h, n in other.items() if n >= REFUSED_HOST_MIN), key=lambda h: -other[h])
    if heavy:
        parts.append("the guard refused " + ", ".join(f"{other[h]} request(s) to {h}" for h in heavy[:3]) +
                     " (another site's host: ANYUSE_THIRD_PARTY decides it)")
    return "; ".join(parts)


def _short_robots_note(note: str) -> str:
    """``api_permission``'s robots note, short: the HTTP status, the network error, the typed redirect reason and its
    chain (the record names WHY a robots file was unreadable), or the policy's reason."""
    import re as _re
    n = " ".join(str(note or "").split())
    n = _re.sub(r"^(?:robots_(?:unreachable|rate_limited_429|unavailable_4xx): )?(?:could not read )?", "", n)
    n = _re.sub(r"^https?://[^\s:]+/robots\.txt:? ", "", n)
    n = n.replace("robots pre-send policy refused request: ", "")
    n = _re.sub(r" \((?:RFC 9309:[^)]*|refused for this run)\)$", "", n)
    return n[:240] or "no answer"


#: a third-party host refused at least this often is named in ``refusal_evidence``
REFUSED_HOST_MIN = 3


def search_refusal(page: Any, mark: Dict[str, int], value: str, start_url: str, target: str = "",
                   url_after: str = "") -> Optional[Tuple[str, str]]:
    """``(status, why)`` when the guard refused, while a search ran, a request that carries the typed value: a document
    of the site that robots.txt disallows (search results under a disallowed path, where an agent reads the unchanged
    start page until max_steps) -> ``robots_search``; a request to ANOTHER site (results from a third-party search
    provider the origin policy refuses, where an agent answers "no results") ->
    ``provider_search``. None otherwise.

    A robots refusal is the search's own results only when the page did not reach a URL that carries the value
    (``url_after``) and it is the search form's target (``target``, when known: the same path, with or without the value)
    or, with no target known, it carries the value - typing that sends a suggestion request robots.txt disallows
    (/search/suggest) while the results page loads is not a wall."""
    from anyuse.execution import site_canon as SC
    v = " ".join(str(value or "").split()).lower()
    if not v:
        return None
    budget = getattr(page, "budget", None)
    scope = getattr(budget, "web_scope", None)
    start_host = (urllib.parse.urlsplit(str(start_url or "")).hostname or "").lower()
    site = budget.site_of(start_host) if budget is not None and hasattr(budget, "site_of") else start_host
    failed = list(getattr(getattr(page, "guard", None), "failed", None) or [])[int((mark or {}).get("failed", 0)):]
    forms = {v, urllib.parse.quote(v).lower(), urllib.parse.quote_plus(v).lower()}
    carrying = [(str(u), str(w or "")) for u, w in failed
                if any(f in urllib.parse.unquote_plus(str(u)).lower() or f in str(u).lower() for f in forms)]
    reached = any(f in urllib.parse.unquote_plus(str(url_after or "")).lower() for f in forms)
    t_path = urllib.parse.urlsplit(str(target or "")).path.rstrip("/")
    # (a form that sends /catalogsearch/result/?q= without the typed value): the search form's
    # own target refused by robots.txt is the wall whether or not the refused URL carries the value
    for u, w in [(str(u), str(w or "")) for u, w in failed]:
        if not w.startswith("robots") or SC.leaves_site(u, start_url) or reached:
            continue
        own = bool(t_path) and urllib.parse.urlsplit(u).path.rstrip("/") == t_path
        if own or (not t_path and (u, w) in carrying):
            return ("robots_search", f"robots_search: robots.txt disallows the site's search results ({u[:120]})")
    # (a search page that loads a hosted search provider the scope grants, whose own robots.txt disallows the
    # script - no result can draw, and an agent answers "no results" or the footer's links): a catalogued
    # site-search provider's call refused by ITS robots.txt while the search ran is kept like a refused provider - a
    # "no results" answer after it is the typed stop provider_search, never a claim that the site has none (the run
    # goes on: the site may also draw its own results)
    for u, w in [(str(u), str(w or "")) for u, w in failed]:
        if w.startswith("robots") and provider_call(u, scope, site):
            return ("provider_search", f"provider_search: the site's search runs on {SC._host(u)}, a search "
                                       f"provider whose robots.txt disallows it ({u[:100]})")
    types = getattr(getattr(page, "guard", None), "failed_types", None) or {}
    for u, w in carrying:
        # (under ANYUSE_THIRD_PARTY=search) only the ORIGIN POLICY's refusal makes the typed
        # stop - a granted provider's suggestion request that fails at the run's per-load cap is no refusal
        if not w.startswith("third-party host"):
            continue
        # (in-page results' thumbnails on the publisher's image host carry the value in their path, and
        # their refusal must not stop the run as "provider_search"):
        # only a refused search DATA call is evidence of a provider (an XHR / fetch / document / script / event source
        # / websocket), never an image, font, stylesheet or media file
        if not search_data_call(u, types.get(u, "")):
            continue
        host = SC._host(u)
        # the scope's own reason, when it gave one: "third-party host H (on the ad/tracker blocklist: D)"
        reason = w[len(f"third-party host {host}"):].strip(" :") if w.startswith(f"third-party host {host}") else ""
        reason = reason[1:-1] if reason.startswith("(") and reason.endswith(")") else reason
        why = (f"the web scope does not grant it for this site: {reason}" if reason else
               "the web scope does not grant it for this site (ANYUSE_THIRD_PARTY=search grants the host the site's "
               "page sends the typed value to, or a catalogued site-search provider the page names)")
        return ("provider_search", f"provider_search: the site's results come from {host}, another site; "
                                   f"{why} ({u[:100]})")
    return None


def provider_call(url: str, scope: Any = None, site: str = "") -> bool:
    """Is ``url`` a call of a site-search provider: a catalogued one (``web_scope.PROVIDERS``: its host and one of its
    paths), or the host the run's ``scope`` observed as ``site``'s provider (under the path area the typed value went
    to)?"""
    from anyuse.execution import web_scope as WS
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    host, path = (p.hostname or "").lower(), (p.path or "/") + (("?" + p.query) if p.query else "")
    if scope is not None and site and hasattr(scope, "observed_for") and \
            scope.observed_for(site, host, p.path or "/") is not None:
        return True
    return any(host == str(h).lower() and any(path.startswith(x) for x in paths)
               for spec in WS.PROVIDERS for h, paths in (spec.hosts or ()))


#: the resource types a search's data comes in
SEARCH_DATA_TYPES = frozenset(("XHR", "Fetch", "Document", "Script", "EventSource", "WebSocket", "Other", "Preflight"))
#: a file that is never a search's data (by the path's extension, when the type was not recorded)
_ASSET_FILE = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|css|mp4|webm|mp3|m4a|ogg)$", re.I)


#: the reader's areas of a page's chrome (``_new_first``)
_CHROME_AREAS = frozenset(("header", "nav", "footer", "aside"))
#: the areas and accessibility landmarks of a page's header and navigation (its listing pages' links)
_MENU_AREAS = frozenset(("header", "nav"))
_MENU_LANDMARKS = frozenset(("navigation", "banner"))


def search_data_call(url: str, rtype: str = "") -> bool:
    """Could this refused request be a search's DATA call (``SEARCH_DATA_TYPES``)? With no recorded
    type: not when its path names an image, font, stylesheet or media file."""
    if rtype:
        return rtype in SEARCH_DATA_TYPES
    try:
        path = urllib.parse.urlsplit(str(url or "")).path or ""
    except ValueError:
        return True
    return not _ASSET_FILE.search(path)


def _local_sig(sig: str) -> str:
    """A link container's signature without the page-type part (the ``body.<page classes>`` segment differs on every
    page type) and limited to its last three segments: what a menu or a teaser block keeps across pages."""
    parts = [p for p in str(sig or "").split(">") if p]
    if parts and parts[0].startswith(("body", "html")):
        parts = parts[1:]
    return ">".join(parts[-3:])


def _with_note(obs: T.Observation, note: str) -> T.Observation:
    o = T.Observation(**{k: v for k, v in obs.__dict__.items()})
    o.note = note[:4000]
    return o


# ---------------------------------------------------------------------------
# desktop
# ---------------------------------------------------------------------------
def fingerprint_of(elements: Sequence[T.Element], app: str) -> Tuple[str, Dict[str, Any]]:
    """``(state_id, UIState dict)`` the guest recorder's way (``interlock_drive_e2e.fingerprint_state``):
    the actionable elements outside the menu bar (role + title), the role frequency and the structural
    hash of the whole tree, the state id from the actionable roles."""
    from anyuse.context.accessibility import ACTIONABLE_ROLES
    from anyuse.guides import state_graph as sg

    def label(e: T.Element) -> str:
        # the drift gate's live capture labels an element by its AXTitle alone; the policy's label falls back to the
        # description ("Dokumentaktionen", TextEdit's title-bar button), and a baseline built from THAT reported the
        # element missing at every replay
        return (e.ax_title if e.ax_title is not None else e.text) or ""
    live = [{"role": e.role or "unknown", "title": label(e)} for e in elements]
    actionable = [{"role": e.role, "title": label(e)} for e in elements
                  if e.role in ACTIONABLE_ROLES and not e.in_menubar]
    fp = sg.UIFingerprint(required_elements=actionable,
                          element_count_range=(int(len(live) * 0.6), int(len(live) * 1.4) + 10),
                          structural_hash=sg.compute_structural_hash(live),
                          role_frequency=sg._role_frequency(live))
    sid = sg.generate_state_id(actionable, app)
    return sid, sg.UIState(state_id=sid, app=app, fingerprint=fp).to_dict()


class DesktopTools:
    """The desktop tool set over the guest job callable (section 3). ``interlock(where) -> refusal | None``
    is ``foreground_interlock.refusal`` in a live run (a test injects one)."""

    toolset = "desktop"

    def __init__(self, guest: Callable[..., Dict[str, Any]], app: str = "", bundle: str = "", *,
                 interlock: Optional[Callable[[str], Optional[str]]] = None):
        self.guest = guest
        self.app, self.bundle = app, bundle
        self.interlock = interlock
        self.start_url = ""
        self.site = ""

    def _call(self, op: str, **kw) -> Dict[str, Any]:
        r = self.guest(op, **kw)
        return r if isinstance(r, dict) else {"status": "refused", "detail": f"{op}: no answer"}

    def resolve_system_prompt(self, where: str, *, task_context: str = "", decision_limits=None) -> Dict[str, Any]:
        """Resolve only on this desktop's actual guest runner at an agent boundary.

        The client's opt-in and optional guest path are data. The guest alone
        reads the policy and checks ownership, drive authorization and session.
        Manual mode sends no command and never probes the host desktop.
        """
        from anyuse.execution.system_prompt_policy import MODE_ENV, POLICY_ENV, RESOLUTION_FORMAT, MAX_TASK_CONTEXT_BYTES
        result = {"format": RESOLUTION_FORMAT, "status": "disabled", "reason": "system prompts are handled manually",
                  "attempted": False, "reobserve_required": False, "next_action": "unchanged"}
        mode = os.environ.get(MODE_ENV, "manual")
        if mode == "manual":
            return result
        if mode not in ("configured_denials", "rules", "llm", "allow_all"):
            return dict(result, status="manual", reason="unknown system prompt mode", next_action="stop")
        args = {"where": where, "mode": mode}
        if mode == "llm":
            if type(task_context) is not str or not task_context.strip() or "\0" in task_context:
                return dict(result, status="manual", reason="llm mode requires an explicit task context", next_action="stop")
            try:
                bounded = len(task_context.encode("utf-8")) <= MAX_TASK_CONTEXT_BYTES
            except UnicodeError:
                bounded = False
            if not bounded:
                return dict(result, status="manual", reason="task context exceeds the UTF-8 limit", next_action="stop")
            args["task_context"] = task_context
            if decision_limits is not None:
                if type(decision_limits) is not dict or set(decision_limits) != {"remaining_usd", "remaining_calls", "timeout_s"}:
                    return dict(result, status="manual", reason="exact finite decision limits are required", next_action="stop")
                dollars, calls, seconds = (decision_limits[key] for key in ("remaining_usd", "remaining_calls", "timeout_s"))
                if (dollars is not None and (type(dollars) not in (int, float) or not math.isfinite(dollars) or dollars < 0)) or \
                        type(calls) is not int or not 0 <= calls <= 100000 or \
                        type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 86400:
                    return dict(result, status="manual", reason="decision limits are not finite bounded data", next_action="stop")
                args["decision_limits"] = decision_limits
        if POLICY_ENV in os.environ:
            args["policy_path"] = os.environ[POLICY_ENV]
        try:
            response = self._call("resolve_system_prompt", **args)
            resolution = response.get("resolution")
            if response.get("status") == "done" and type(resolution) is dict and \
                    resolution.get("format") == RESOLUTION_FORMAT and \
                    resolution.get("status") in ("disabled", "no_prompt", "manual", "denial_attempted", "action_attempted") and \
                    type(resolution.get("reason")) is str and \
                    resolution.get("next_action") in ("unchanged", "observe", "stop") and \
                    type(resolution.get("attempted")) is bool and type(resolution.get("reobserve_required")) is bool and \
                    (not resolution["attempted"] or resolution["reobserve_required"]) and \
                    (not resolution["reobserve_required"] or resolution["next_action"] != "unchanged") and \
                    (resolution["status"] not in ("denial_attempted", "action_attempted") or resolution["attempted"]) and \
                    (resolution["status"] != "manual" or resolution["next_action"] == "stop"):
                return resolution
            reason = "the guest did not return a complete system-prompt resolution"
        except Exception as exc:
            reason = f"the guest system-prompt resolution was not confirmed: {type(exc).__name__}"
        # A lost reply cannot prove that no press was attempted. Never retry
        # here or fall back to the host; discard targets and hand off instead.
        unknown = {"decision": {"model_calls": None, "cost_usd": None, "budget_charged_usd": None}} if mode == "llm" else {}
        return dict(result, status="manual", reason=reason, attempted=True,
                    reobserve_required=True, next_action="stop", **unknown)

    def observe(self) -> T.Observation:
        from anyuse.context.accessibility import canonical_role
        r = self._call("ax_elements")
        status = str(r.get("status") or "done")
        els: List[T.Element] = []
        for i, e in enumerate(r.get("elements") or []):
            role = str(e.get("role") or "")
            els.append(T.Element(index=i, kind=str(e.get("canonical_role") or canonical_role(role) or role),
                                 role=role, text=str(e.get("title") or ""), identifier=str(e.get("identifier") or ""),
                                 path=str(e.get("path") or ""), value=str(e.get("value") or "")[:200],
                                 enabled=bool(e.get("enabled", True)), in_menubar=bool(e.get("in_menubar")),
                                 ax_title=str(e["ax_title"]) if e.get("ax_title") is not None else None))
        app = str(r.get("app") or self.app)
        sid, state = fingerprint_of(els, app)
        rfp = r.get("fingerprint")
        if isinstance(rfp, dict) and (rfp.get("required_elements") or str(rfp.get("structural_hash") or "").strip()):
            # the guest's recorder fingerprint of the live app (the capture the drift gate reads at a replayed
            # step): the compiled workflow's baseline then counts and hashes the same view
            from anyuse.guides import state_graph as sg
            try:
                state = sg.UIState(state_id=sid, app=app,
                                   fingerprint=sg.UIFingerprint.from_dict(r["fingerprint"])).to_dict()
            except Exception:  # noqa: BLE001 - an unreadable fingerprint: the harness's own stands
                pass
        blocked = ""
        if status in ("prompt", "stop", "locked", "refused", "error"):
            blocked = f"the guest reports {status}: {r.get('detail') or ''}".strip()
        elif r.get("ok") is False:
            blocked = f"the guest could not read the screen: {r.get('detail') or 'no app'}"
        return T.Observation(kind="ax", app=app, bundle=str(r.get("bundle") or self.bundle),
                             window=str(r.get("window") or ""), state_id=sid, elements=els,
                             text=str(r.get("text") or "")[:8000], fingerprint=state, blocked=blocked,
                             lists=[str(x) for x in (r.get("lists") or [])])

    def _element(self, action: T.Action, obs: T.Observation) -> Optional[T.Element]:
        idx = getattr(action, "index", None)
        if idx is None:
            return None
        return next((e for e in obs.elements if e.index == int(idx)), None)

    def target_label(self, action: T.Action, obs: T.Observation) -> str:
        e = self._element(action, obs)
        return e.text if e is not None else ""

    def effect_of(self, action: T.Action, obs: T.Observation) -> str:
        if action.kind == T.AxRead.kind:
            return T.READ
        e = self._element(action, obs)
        if e is not None and e.role == "AXMenuBarItem" and action.kind in (T.AxPress.kind, T.AxToggle.kind):
            # structural, not by label: pressing a menu-bar item only opens its menu (Escape closes it);
            # "Format" and "Ablage" are menus, the items inside them are classified by their labels
            return T.max_effect(action.effect, T.WRITE)
        label = (e.text if e is not None else "") + " " + str(getattr(action, "text", "") or "")
        return T.max_effect(action.effect, label_effect(action, label))

    def gate(self, action: T.Action, obs: T.Observation) -> Tuple[str, Optional[str]]:
        if action.kind == T.AxRead.kind:
            return "", None
        if action.kind == T.AxKey.kind:
            if str(action.key).lower() not in KEY_CODES:
                return f"key {action.key!r} is not one an AX replay presses (Return or Escape only)", None
        else:
            e = self._element(action, obs)
            if e is None:
                return f"element {getattr(action, 'index', '?')} is not on this screen", None
            if not e.enabled:
                return f"element {e.index} ({e.text!r}) is disabled", None
            if action.kind == T.AxEnterText.kind and e.kind in ("secure_text_field", "password"):
                return "never typed into a password field", None
        if self.interlock is not None:
            why = self.interlock(f"agent_harness {action.kind}")
            if why:
                return str(why), T.Stop.REFUSED
        return "", None

    def planned_step(self, action: T.Action, obs: T.Observation) -> Dict[str, Any]:
        """The action as ONE ``PlannedStep`` dict (modality ``ax``) for the guest's ``ax_step``."""
        e = self._element(action, obs)
        verb = {T.AxPress.kind: "press", T.AxToggle.kind: "toggle", T.AxEnterText.kind: "enter_text",
                T.AxKey.kind: "press_key"}[action.kind]
        cmd: Dict[str, Any] = {"role": e.role if e else "", "title": e.text if e else "", "action": verb}
        if e is not None:
            cmd["selector"] = {"canonical_role": e.kind, "name": e.text, "identifier": e.identifier,
                               "path": e.path, "raw_role": e.role}
        if action.kind == T.AxKey.kind:
            cmd.update(keycode=KEY_CODES[str(action.key).lower()], flags=0)
        target = f"{e.role} {e.text}".strip() if e else ""
        return {"intent": f"{verb} {target}".strip(), "source": "workflow", "target_element": target,
                "modality": "ax", "command": cmd, "input_data": getattr(action, "text", None),
                "needs_visual": False, "expected_after": "", "expected_after_state": None, "reasoning": ""}

    def dispatch(self, action: T.Action, obs: T.Observation) -> T.Outcome:
        if action.kind == T.AxRead.kind:
            # the guest's read_text names the app by bundle id (called without one, every read failed)
            from anyuse.execution.desktop_e2e import BUNDLES
            r = self._call("read_text", bundle=self.bundle or BUNDLES.get(self.app, self.app), what="document")
            text = str(r.get("text") or "")
            return T.Outcome(str(r.get("status") or "") == "done", str(r.get("detail") or ""), how="read",
                             status=str(r.get("status") or ""), data={"text": text[:8000]},
                             observation=_with_note(obs, "TEXT READ:\n" + text[:4000]))
        step = self.planned_step(action, obs)
        r = self._call("ax_step", step=step)
        status = str(r.get("status") or "refused")
        return T.Outcome(status == "done", str(r.get("detail") or ""), how="ax_step", status=status,
                         data={"step": step})


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------
class HttpTools:
    """GET by default (section 3). ``transport(method, url, headers, body) -> (status, headers, text)``;
    ``robots(url) -> (allowed | None, why)`` (``api_permission.robots_allows_cached`` by default: an
    unknown host is refused, fail closed)."""

    toolset = "http"

    def __init__(self, transport: Callable[..., Tuple[int, Dict[str, str], str]], start_url: str = "", *,
                 related_origins: Sequence[str] = (), robots=None, user_agent: str = HONEST_UA,
                 hosts_path: Optional[str] = None, max_bytes: int = HTTP_MAX_BYTES, respect_robots: bool = True):
        self.transport = transport
        self.start_url = start_url
        self.site = urllib.parse.urlsplit(start_url).hostname or ""
        self.related_origins = tuple(related_origins)
        self.robots = robots
        self.user_agent = user_agent
        self.hosts_path = hosts_path
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self.last: Optional[T.Observation] = None

    def observe(self) -> T.Observation:
        return self.last or T.Observation(kind="http", url=self.start_url, note="no request sent yet")

    def target_label(self, action: T.Action, obs: T.Observation) -> str:
        return str(getattr(action, "url", "") or "")

    def effect_of(self, action: T.Action, obs: T.Observation) -> str:
        return action.effect

    def _robots(self, url: str) -> Tuple[Optional[bool], str]:
        if self.robots is not None:
            return self.robots(url)
        from anyuse.execution.api_permission import robots_allows_cached
        return robots_allows_cached(url)

    def gate(self, action: T.Action, obs: T.Observation) -> Tuple[str, Optional[str]]:
        from anyuse.execution.request_policy import destination_refusal, exact_origin_allowlist
        url = str(getattr(action, "url", "") or "")
        why = gov_refusal(url)
        if why:
            return why, T.Stop.GOV_HOST
        why = destination_refusal(url, exact_origin_allowlist(self.start_url, self.related_origins))
        if why:
            return f"blocked: {why}", None
        if action.kind == T.HttpPost.kind:
            from anyuse.execution.api_permission import readonly_post_approved
            from anyuse.settings import get as get_setting
            if not get_setting("ANYUSE_API_READONLY_POST"):
                return "a POST is a write: ANYUSE_API_READONLY_POST is off", None
            if not action.operation or not readonly_post_approved(url, action.operation, self.hosts_path):
                return (f"a POST is a write: {url} / {action.operation or '(no operation)'} is not an approved "
                        "read-only POST endpoint"), None
            return "", None
        if self.respect_robots:
            allowed, note = self._robots(url)
            if allowed is None:
                return f"blocked: robots.txt not known for this host ({note}); it is fetched once by the compile path", None
            if not allowed:
                return f"blocked: {note}", None
        return "", None

    def dispatch(self, action: T.Action, obs: T.Observation) -> T.Outcome:
        method = "POST" if action.kind == T.HttpPost.kind else "GET"
        headers = {"User-Agent": self.user_agent, "Accept": "text/html, application/json;q=0.9, */*;q=0.5"}
        body = getattr(action, "body", "") or None
        status, rheaders, text = self.transport(method, action.url, headers, body)
        text = str(text or "")[: self.max_bytes]
        ctype = str({k.lower(): v for k, v in (rheaders or {}).items()}.get("content-type") or "")
        data = None
        if "json" in ctype:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        o = T.Observation(kind="http", url=action.url, status=int(status), content_type=ctype,
                          text=text[:6000], json_data=data)
        self.last = o
        return T.Outcome(200 <= int(status) < 300, "" if 200 <= int(status) < 300 else f"HTTP {status}",
                         how=method.lower(), url_after=action.url, data={"status": int(status), "text": text},
                         observation=o)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
class FileTools:
    """The per-task workspace: reads anywhere under it, writes under ``out/`` only; a name with an
    absolute path, ``..``, or a symlink leaving the workspace is refused."""

    toolset = "files"

    def __init__(self, workspace: str):
        self.workspace = os.path.realpath(workspace)
        for sub in ("in", "out"):
            os.makedirs(os.path.join(self.workspace, sub), exist_ok=True)
        self.start_url = ""
        self.site = ""

    def safe_path(self, name: str, *, write: bool = False) -> Tuple[str, str]:
        """``(path, refusal)``: the real path inside the workspace, or why not."""
        n = str(name or "")
        if not n or os.path.isabs(n) or n.startswith("~") or "\x00" in n:
            return "", f"file name {n!r} is not a relative name inside the workspace"
        parts = n.replace("\\", "/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            return "", f"file name {n!r} must not contain '..' or empty segments"
        if write and parts[0] != "out":
            parts = ["out"] + parts
        path = os.path.join(self.workspace, *parts)
        real = os.path.realpath(path)
        if real != self.workspace and not real.startswith(self.workspace + os.sep):
            return "", f"{n!r} resolves outside the workspace (a symlink?)"
        parent = os.path.realpath(os.path.dirname(path))
        if parent != self.workspace and not parent.startswith(self.workspace + os.sep):
            return "", f"{n!r} resolves outside the workspace (a symlink?)"
        return real, ""

    def listing(self) -> List[str]:
        out: List[str] = []
        for root, _dirs, files in os.walk(self.workspace):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.workspace))
        return sorted(out)[:500]

    def observe(self) -> T.Observation:
        return T.Observation(kind="files", listing=self.listing())

    def target_label(self, action: T.Action, obs: T.Observation) -> str:
        return str(getattr(action, "name", "") or "")

    def effect_of(self, action: T.Action, obs: T.Observation) -> str:
        return action.effect

    def gate(self, action: T.Action, obs: T.Observation) -> Tuple[str, Optional[str]]:
        if action.kind == T.ListFiles.kind:
            return "", None
        _p, why = self.safe_path(action.name, write=action.kind == T.WriteFile.kind)
        return why, None

    def dispatch(self, action: T.Action, obs: T.Observation) -> T.Outcome:
        if action.kind == T.ListFiles.kind:
            o = self.observe()
            return T.Outcome(True, how="list", data={"listing": o.listing}, observation=o)
        path, why = self.safe_path(action.name, write=action.kind == T.WriteFile.kind)
        if why:
            return T.Outcome(False, why)
        if action.kind == T.ReadFile.kind:
            if not os.path.isfile(path):
                return T.Outcome(False, f"no file {action.name!r} in the workspace")
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(self_max := 2_000_000)
            o = self.observe()
            o.note = f"FILE {action.name}:\n{text[:4000]}"
            return T.Outcome(True, how="read", data={"text": text, "truncated": len(text) >= self_max}, observation=o)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(action.text))
        os.replace(tmp, path)
        return T.Outcome(True, how="write", data={"path": os.path.relpath(path, self.workspace)},
                         observation=self.observe())


# ---------------------------------------------------------------------------
# runtime-written tools
# ---------------------------------------------------------------------------
class RuntimeTools:
    """``RunTool`` on a proven tool of the registry, ``WriteTool`` through static checks + proof
    (``harness_toolwrite``)."""

    toolset = "tools"

    def __init__(self, registry, sandbox, files: Optional[FileTools] = None, fetch=None):
        self.registry, self.sandbox, self.files, self.fetch = registry, sandbox, files, fetch
        self.start_url = ""
        self.site = ""

    def observe(self) -> T.Observation:
        return T.Observation(kind="tool", listing=self.registry.names())

    def target_label(self, action: T.Action, obs: T.Observation) -> str:
        return str(getattr(action, "name", "") or (getattr(action, "spec", {}) or {}).get("name") or "")

    def effect_of(self, action: T.Action, obs: T.Observation) -> str:
        if action.kind == T.RunTool.kind:
            spec = self.registry.spec_of(action.name)
            return T.READ if spec is not None else T.UNKNOWN
        return action.effect

    def gate(self, action: T.Action, obs: T.Observation) -> Tuple[str, Optional[str]]:
        if action.kind == T.RunTool.kind and self.registry.spec_of(action.name) is None:
            return f"no proven tool named {action.name!r}", None
        return "", None

    def dispatch(self, action: T.Action, obs: T.Observation) -> T.Outcome:
        from anyuse.execution import harness_toolwrite as TW
        ws = self.files.workspace if self.files is not None else None
        if action.kind == T.RunTool.kind:
            r = self.registry.run(action.name, action.inputs, self.sandbox, workspace=ws, fetch=self.fetch)
            return T.Outcome(bool(r.get("ok")), str(r.get("why") or ""), how="tool", data=r,
                             observation=T.Observation(kind="tool", listing=self.registry.names(),
                                                       note=f"TOOL {action.name}: " + json.dumps(
                                                           r.get("outputs"), ensure_ascii=False, default=str)[:3000]))
        rep = TW.write_tool(action.spec, self.registry, self.sandbox, workspace=ws, fetch=self.fetch)
        return T.Outcome(bool(rep.get("saved")), str(rep.get("why") or ""), how="write_tool", data=rep,
                         observation=T.Observation(kind="tool", listing=self.registry.names(),
                                                   note=("TOOL WRITTEN " if rep.get("saved") else "TOOL REFUSED ")
                                                   + json.dumps({k: rep.get(k) for k in ("name", "why", "static",
                                                                                          "proof")},
                                                                ensure_ascii=False, default=str)[:3000]))


# ---------------------------------------------------------------------------
# the toolbox
# ---------------------------------------------------------------------------
class Toolbox:
    """The tool sets of one run. ``kind``: what the trace is about (``web`` when a browser is present,
    else ``desktop``, ``http``, ``files``)."""

    def __init__(self, *, browser: Optional[BrowserTools] = None, desktop: Optional[DesktopTools] = None,
                 http: Optional[HttpTools] = None, files: Optional[FileTools] = None,
                 tools: Optional[RuntimeTools] = None, rea=None, kind: str = ""):
        self.sets: Dict[str, Any] = {}
        for s in (browser, desktop, http, files, tools, rea):
            if s is not None:
                self.sets[s.toolset] = s
        self.kind = kind or ("web" if browser else "desktop" if desktop else "http" if http else
                             "files" if files else "none")
        primary = browser or desktop or http or files or tools or rea
        self.start_url = getattr(primary, "start_url", "") if primary else ""
        self.site = getattr(primary, "site", "") if primary else ""
        self.app = desktop.app if desktop else ""
        self.bundle = desktop.bundle if desktop else ""

    def available(self) -> List[str]:
        out = [k for k, c in T.ACTIONS.items() if c.toolset in self.sets]
        return out + list(T.CONTROL)

    def observe(self) -> T.Observation:
        obs = T.Observation(kind="none")
        for name in ("browser", "desktop", "http", "files", "tools"):
            if name in self.sets:
                obs = self.sets[name].observe()
                break
        if "rea" in self.sets:
            obs.rea_actions = self.sets["rea"].store.list_tools()
            last = self.sets["rea"].last_result
            if last is not None:
                summary = {key: last.get(key) for key in ("ok", "status", "verification_tier", "model_calls", "output_dir")}
                obs.note = (obs.note + "\nREA result: " + json.dumps(summary, ensure_ascii=False)).strip()
        return obs

    def _set(self, action: T.Action):
        return self.sets.get(action.toolset)

    def target_label(self, action: T.Action, obs: T.Observation) -> str:
        s = self._set(action)
        return s.target_label(action, obs) if s is not None else ""

    def effect_of(self, action: T.Action, obs: T.Observation) -> str:
        s = self._set(action)
        if s is None:
            return action.effect          # the gate refuses it; nothing is dispatched
        own = s.effect_of(action, obs)
        if action.effect == T.UNKNOWN:
            return own                    # a placeholder (run_tool): the saved tool's declared effect replaces it
        return T.max_effect(action.effect, own)

    def gate(self, action: T.Action, obs: T.Observation) -> Tuple[str, Optional[str]]:
        s = self._set(action)
        if s is None:
            return f"no {action.toolset} tools in this run", None
        return s.gate(action, obs)

    def dispatch(self, action: T.Action, obs: T.Observation) -> T.Outcome:
        s = self._set(action)
        if s is None:
            return T.Outcome(False, f"no {action.toolset} tools in this run")
        return s.dispatch(action, obs)

    def correction(self, action: T.Action, obs: T.Observation) -> str:
        """The tool set's corrective note for an action naming an element that is not there."""
        s = self._set(action)
        return s.correction(action, obs) if s is not None and hasattr(s, "correction") else ""

    # -- open tasks: commits, the caller's questions, the redactor ----------------
    def commit_of(self, action: T.Action, obs: T.Observation):
        """The commit this action would be (``commit_policy.Commit``), when its tool set reads one; else None."""
        s = self._set(action)
        f = getattr(s, "commit_of", None) if s is not None else None
        return f(action, obs) if f is not None else None

    def begin_commit(self, action: T.Action, commit) -> None:
        s = self._set(action)
        f = getattr(s, "begin_commit", None) if s is not None else None
        if f is not None:
            f(commit)

    def end_commit(self, action: T.Action) -> None:
        s = self._set(action)
        f = getattr(s, "end_commit", None) if s is not None else None
        if f is not None:
            f()

    def set_asker(self, ask) -> None:
        for s in self.sets.values():
            if hasattr(s, "ask"):
                s.ask = ask

    @property
    def redactor(self):
        for s in self.sets.values():
            r = getattr(s, "redactor", None)
            if r is not None:
                return r
        return None
