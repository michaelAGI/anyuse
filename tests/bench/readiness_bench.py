"""Offline A/B bench: the browser replay's readiness settle against the fixed timer.

The question: when the browser rung reads a page as soon as the recorded
evidence is there, instead of at the end of the timer's 10 s window, does it
ever read too early, and how much sooner does it answer? Each case is a
simulated result page (:class:`SimPage`): a timeline on a fake clock that
releases the protocol events Chrome would send and answers the readiness probe
and the DOM read as the page stands at that instant. The read goes through the
product's own settle (``CdpDriver._settle_and_read``) and then
``ReplayExecutor.execute``, so success and failure are the product's verdict.
Nothing opens a socket, starts a browser or calls a model, and no real time
passes: a case takes milliseconds.

Usage::

    python tests/bench/readiness_bench.py run --old <tree> --new <tree> \
        [--python <interpreter>] [--out <dir>]
    python tests/bench/readiness_bench.py compare OLD.json NEW.json

``--old`` is a checkout at the commit the feature branch forked from, ``--new``
the feature checkout. The script copies itself into ``--out`` (outside both
trees) and runs that copy once per tree with the same interpreter. Each run's
PYTHONPATH is a pkgroot directory holding an ``anyuse`` symlink to the tree,
then the tree, then its ``tools/guarded_tests`` (whose sitecustomize refuses
sockets). HOME and every AnyUse data path point into ``--out``, and no
``ANYUSE_*`` variable is passed through, so each tree runs on its own defaults.

A tree without ``CdpDriver._settle_and_read`` gets the same block as
:func:`_inline_settle_and_read`, a copy of what ``CdpDriver._drive`` ran
inline, with ``cdp_replay.time`` pointed at the simulated clock.

Modes, 12 fixture words x 17 seeds = 204 cases each (odd seeds add a clock the
page re-renders every second):

* ``xhr``: the document has an empty list; one ``/api/search`` request answers
  after U(0, 3.5) s and the page renders all 6 rows.
* ``lazy``: a first request answers after U(0, 1.5) s with 6 rows. When the
  word has more, a second request (``&page=2``) goes out U(0.2, 2.0) s after
  the first batch is on screen and answers after U(0, 1.5) s with 2 to 4 more.
  Four words have no second page.
* ``ssr+xhr``: 2 rows in the document, 4 more from one request after U(0, 3.5) s.
* ``big``: 30 rows as two requests of 15, on the lazy timing.
* ``never``: the request never answers, so the rows never appear.
* ``typeahead``: a search as you type that rewrites the URL in place. The
  recording holds the empty ``/search`` shell, then for every prefix of the
  word from two letters on a ``/api/suggest`` and a ``/api/search`` call whose
  bodies name the word's rows. The replay opens ``/search?q=word`` and the page
  makes one ``/api/search`` call, as in ``xhr``.
* ``typeahead+enter``: typed on the home page (suggestions and 5-row previews
  per prefix), then Enter opens ``/search?q=word``, whose one
  ``/api/search?...&limit=50`` call brings every row. The replay makes only
  that last call.
* ``few``: the recorded words keep 6 rows, so the count floor is 6; the other
  words get 0 to 2 rows that name the word (:data:`FEW_ROWS`). A word with no
  row says ``No results found for "word"`` once its call answered.

Recordings: river (compiled from), stone and cloud, each with its full list,
the document and every request the page made, with bodies. In lazy mode river
and stone have a second page and cloud does not, so the count floor is 6.
Templates are ``template_from_recording(river, "q", recordings=[river, stone,
cloud], readiness=...)``, the call ``api_compile.runtime_rungs`` makes.

Arms:

* ``timer``: readiness=False; the page is read at the ceiling.
* ``ready``: readiness=True; the tree's own rule.
* ``ready-prev`` (NEW only): the NEW template without ``rule`` and
  ``value_calls``, i.e. the rule that waits for the recorded request counts and
  the recorded item count. It must match OLD ``ready`` case by case when
  ``--old`` is a tree with request counts.
* ``ready-old`` (NEW only): the NEW template with its readiness dict as an
  earlier rule wrote it: no request counts, the item floor capped at 12.
* ``compiled``: the browser template ``api_compile.runtime_rungs`` stores for a
  compiled tool, on the tree's defaults.

Metrics per arm and mode: early read (the DOM read holds a list other than the
full one), wrong success (success with a list other than the full one),
failures, median and p95 simulated time to the read, and how the settle ended.
The probe answer carries ``rlen``, the length of the rows' text, which the
``value_calls`` rule watches for stability.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import types
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

HOST = "fix.test"
SELECTOR = "ul#results li.result"
WORDS = ["amber", "birch", "cedar", "cloud", "delta", "ember", "falcon", "harbor",
         "maple", "river", "stone", "tulip"]
SUFFIXES = ["core", "cli", "kit", "lite", "sdk", "tools"]
#: the words recorded: the first is compiled from, the others are the held-out recordings
RECORDED = ["river", "stone", "cloud"]
MODES = ["xhr", "lazy", "ssr+xhr", "big", "never", "typeahead", "typeahead+enter", "few"]
#: modes whose recording holds the calls made while typing; the replay opens the result URL
TYPEAHEAD = ("typeahead", "typeahead+enter")
SEEDS = 17
#: lazy mode: rows on the second page per word; 0 means the page makes no second request
LAZY_EXTRA = {"amber": 4, "birch": 2, "cedar": 0, "cloud": 0, "delta": 3, "ember": 4,
              "falcon": 0, "harbor": 2, "maple": 3, "river": 4, "stone": 2, "tulip": 0}
#: few mode: rows per word on the replay; the recorded words keep their 6
FEW_ROWS = {"amber": 2, "birch": 0, "cedar": 1, "cloud": 6, "delta": 2, "ember": 0,
            "falcon": 1, "harbor": 2, "maple": 0, "river": 6, "stone": 6, "tulip": 1}
#: seconds from a response finishing to its rows being in the DOM
RENDER_S = 0.005
#: the window the timer reads at: ``CdpDriver._drive``'s min(settle_s + 6, timeout_s)
#: for the template defaults (4 s, 45 s)
CEILING_MS = 10_000.0
POLL_MS = 150.0


def names(word: str, n: int) -> List[str]:
    """The fixture site's rows for ``word`` (its six per-word packages first), ``n`` of them."""
    k = WORDS.index(word) % 6
    first = [f"{word}-{s}" for s in SUFFIXES[k:] + SUFFIXES[:k]]
    return (first + [f"{word}-pkg{i:02d}" for i in range(6, max(6, n))])[:n]


def full_list(word: str, mode: str) -> List[str]:
    if mode == "lazy":
        return names(word, 6 + LAZY_EXTRA[word])
    if mode == "big":
        return names(word, 30)
    if mode == "few":
        return names(word, FEW_ROWS[word])
    return names(word, 6)


def page_url(word: str) -> str:
    return f"https://{HOST}/search?q={urllib.parse.quote(word)}"


def api_url(word: str, page: int = 1, limit: int = 0) -> str:
    return (f"https://{HOST}/api/search?q={urllib.parse.quote(word)}"
            + (f"&page={page}" if page > 1 else "") + (f"&limit={limit}" if limit else ""))


def suggest_url(prefix: str) -> str:
    return f"https://{HOST}/api/suggest?q={urllib.parse.quote(prefix)}"


def prefixes(word: str) -> List[str]:
    """What a type-ahead search sends while ``word`` is typed: every prefix from two letters on."""
    return [word[:i] for i in range(2, len(word) + 1)]


# ---------------------------------------------------------------------------
# the simulated page
# ---------------------------------------------------------------------------

class SimPage:
    """One result page on a simulated clock.

    ``batches`` are the page's own requests: ``(sent_at, finished_at or None,
    url, rows on screen once it is rendered)``. :meth:`pump` advances the clock
    and releases the protocol events that are due; :meth:`js` answers what
    ``CdpDriver`` evaluates (the readiness probe, the DOM, the text, the URL).
    """

    def __init__(self, word: str, mode: str, seed: int, tick: Optional[bool] = None):
        rng = random.Random(f"{mode}|{word}|{seed}")
        self.word, self.mode, self.seed = word, mode, seed
        self.tick = bool(seed % 2) if tick is None else bool(tick)
        self.url = page_url(word)
        self.full = full_list(word, mode)
        self.now = 0.0
        self.dom_reads: List[float] = []
        self.doc_done = rng.uniform(0.05, 0.4)
        self.complete_at = self.doc_done + rng.uniform(0.0, 0.3)
        self.ssr = 2 if mode == "ssr+xhr" else 0
        t1 = self.doc_done
        self.batches: List[Tuple[float, Optional[float], str, int]] = []
        if mode in ("xhr", "ssr+xhr", "never", "few") or mode in TYPEAHEAD:
            d = rng.uniform(0.0, 3.5)
            self.batches.append((t1, None if mode == "never" else t1 + d,
                                 api_url(word, limit=50 if mode == "typeahead+enter" else 0),
                                 len(self.full)))
        else:
            first = 15 if mode == "big" else 6
            d1, gap, d2 = rng.uniform(0.0, 1.5), rng.uniform(0.2, 2.0), rng.uniform(0.0, 1.5)
            self.batches.append((t1, t1 + d1, api_url(word), first))
            if len(self.full) > first:
                t2 = t1 + d1 + RENDER_S + gap
                self.batches.append((t2, t2 + d2, api_url(word, 2), len(self.full)))
        timeline: List[Tuple[float, int, dict]] = []

        def event(at, method, rid, url=None):
            params: Dict[str, Any] = {"requestId": rid}
            if url is not None:
                params["request"] = {"url": url, "method": "GET"}
            timeline.append((at, len(timeline), {"method": method, "params": params}))

        event(0.0, "Network.requestWillBeSent", "doc", self.url)
        event(self.doc_done, "Network.loadingFinished", "doc")
        event(self.doc_done, "Network.requestWillBeSent", "js", f"https://{HOST}/static/app.js")
        event(self.doc_done + 0.02, "Network.loadingFinished", "js")
        for i, (sent, done, url, _rows) in enumerate(self.batches):
            event(sent, "Network.requestWillBeSent", f"api{i + 1}", url)
            if done is not None:
                event(done, "Network.loadingFinished", f"api{i + 1}")
        self._timeline = sorted(timeline, key=lambda x: (x[0], x[1]))
        self._released = 0

    # -- the page at an instant ------------------------------------------------

    def clock(self) -> float:
        return self.now

    def rows_at(self, t: float) -> int:
        if t < self.doc_done:
            return 0
        count = self.ssr
        for _sent, done, _url, rows in self.batches:
            if done is not None and t >= done + RENDER_S:
                count = rows
        return count

    def done_at(self) -> Optional[float]:
        """When the full list is on screen, or ``None`` when it never is."""
        last = self.batches[-1]
        return None if last[1] is None else last[1] + RENDER_S

    def ready_state(self, t: float) -> str:
        if t < self.doc_done:
            return "loading"
        return "interactive" if t < self.complete_at else "complete"

    def html(self, t: float, rows: Optional[int] = None) -> str:
        if t < self.doc_done:
            return "<html><head></head><body></body></html>"
        rows = self.rows_at(t) if rows is None else rows
        clock = ""
        if self.tick:
            s = 12 * 3600 + int(t)
            clock = f'<p id="clock">{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}</p>'
        items = "".join(f'<li class="result"><a href="/pkg/{n}">{n}</a></li>'
                        for n in self.full[:rows])
        empty = ""
        done = self.done_at()
        if self.mode == "few" and not self.full and done is not None and t >= done:
            empty = f'<p class="empty">No results found for "{self.word}"</p>'
        return (f"<html><head><title>Search results for {self.word} - Fixture Packages</title>"
                f'</head><body><nav><a href="/">Fixture Packages</a></nav>'
                f'<form action="/search"><input name="q" value="{self.word}"></form>{clock}'
                f'<ul id="results">{items}</ul>{empty}<script src="/static/app.js"></script>'
                f"</body></html>")

    def served_html(self) -> str:
        """The document as the server sent it: the shell with its server-rendered rows."""
        return self.html(self.doc_done, rows=self.ssr)

    # -- what CdpDriver calls ----------------------------------------------------

    def pump_into(self, events: List[dict]):
        async def pump(seconds):
            self.now += max(0.0, float(seconds))
            while (self._released < len(self._timeline)
                   and self._timeline[self._released][0] <= self.now + 1e-9):
                events.append(copy.deepcopy(self._timeline[self._released][2]))
                self._released += 1
        return pump

    async def js(self, expr: str):
        from anyuse.execution.browser_supervisor import DOM_EXPR, TEXT_EXPR
        if expr == DOM_EXPR:
            self.dom_reads.append(self.now)
            return self.html(self.now)
        if expr == TEXT_EXPR:
            return visible_text(self.html(self.now))
        if expr == "location.href":
            return self.url
        if "readyState" in expr and "querySelectorAll" in expr:
            m = re.search(r"querySelectorAll\((\".*?\")\)", expr)
            selector = json.loads(m.group(1)) if m else ""
            dom = self.html(self.now)
            if selector == SELECTOR:
                n = self.rows_at(self.now)
                rlen = sum(len(name) for name in self.full[:n])
            else:
                from anyuse.execution.html_extract import parse, select
                try:
                    found = select(parse(dom), selector)
                    n, rlen = len(found), sum(len(x.text()) for x in found)
                except ValueError:
                    n, rlen = -1, 0
            return {"n": n, "rlen": rlen, "len": len(dom), "ready": self.ready_state(self.now)}
        return None


def visible_text(dom: str) -> str:
    from anyuse.execution.html_extract import parse
    return parse(dom, visible_only=True).text()


# ---------------------------------------------------------------------------
# the read: the product's settle on the simulated clock
# ---------------------------------------------------------------------------

async def _inline_settle_and_read(driver, js, pump, events, readiness, ceiling):
    """The settle-and-read block ``CdpDriver._drive`` ran inline before it became
    ``_settle_and_read``, unchanged apart from returning ``(dom, settle)``.
    ``cdp_replay.time`` is the simulated clock while it runs."""
    from anyuse.execution import cdp_replay as R
    from anyuse.execution.browser_supervisor import DOM_EXPR
    t_settle = R.time.monotonic()
    if readiness:
        settle = await driver._await_ready(js, pump, events, readiness, ceiling)
    else:
        await pump(ceiling)
        settle = {"how": "timer", "waited_ms": round(ceiling * 1000)}
    dom = await js(DOM_EXPR) or ""
    if (readiness and settle.get("how") == "ready"
            and readiness.get("fallback") == "timer"):
        ok, why = R.ready_read_passes(dom, readiness)
        if not ok:
            remaining = max(0.0, ceiling - (R.time.monotonic() - t_settle))
            await pump(remaining)
            dom = await js(DOM_EXPR) or ""
            settle = {**settle, "how": "ready->timer_fallback", "first_read": why,
                      "waited_ms": round((R.time.monotonic() - t_settle) * 1000)}
    return dom, settle


def settle_and_read(page: SimPage, readiness: Optional[Dict[str, Any]],
                    ceiling_s: float) -> Tuple[str, Dict[str, Any]]:
    from anyuse.execution import cdp_replay as R
    driver = R.CdpDriver("http://127.0.0.1:9")
    events: List[dict] = []
    if hasattr(driver, "_settle_and_read"):
        return asyncio.run(driver._settle_and_read(page.js, page.pump_into(events), events,
                                                   readiness, ceiling_s, clock=page.clock))
    saved = R.time
    R.time = types.SimpleNamespace(monotonic=page.clock, sleep=time.sleep, time=time.time)
    try:
        return asyncio.run(_inline_settle_and_read(driver, page.js, page.pump_into(events),
                                                   events, readiness, ceiling_s))
    finally:
        R.time = saved


class SimDriver:
    """A replay driver that opens a :class:`SimPage` for the URL's word and reads it
    through the product's settle; the executor passes it the template's readiness."""

    def __init__(self, mode: str, seed: int, tick: Optional[bool] = None):
        self.mode, self.seed, self.tick = mode, seed, tick
        self.page: Optional[SimPage] = None
        self.dom = ""
        self.settle: Dict[str, Any] = {}

    def open(self, url, settle_s, timeout_s, readiness=None):
        from anyuse.execution.cdp_replay import PageResult
        word = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.page = page = SimPage(word, self.mode, self.seed, self.tick)
        ceiling = min(float(settle_s) + 6.0, float(timeout_s))
        self.dom, self.settle = settle_and_read(page, readiness, ceiling)
        return PageResult(url=url, dom=self.dom, text=visible_text(self.dom), status=200,
                          elapsed_ms=page.now * 1000.0, settle=dict(self.settle))


# ---------------------------------------------------------------------------
# recordings and templates
# ---------------------------------------------------------------------------

def _get(url: str, body: str, mime: str = "application/json") -> Dict[str, Any]:
    return {"url": url, "method": "GET", "status": 200, "mime": mime, "body": body}


def _rows_body(rows: List[str]) -> str:
    return json.dumps({"count": len(rows), "results": [{"name": n} for n in rows]})


def _typed_network(page: "SimPage", mode: str) -> List[Dict[str, Any]]:
    """The requests a type-ahead recording holds: the calls made while the word was
    typed, then (``typeahead+enter``) the result document and its one call."""
    script = _get(f"https://{HOST}/static/app.js", "render(fetch('/api/search'))",
                  "application/javascript")
    shell = page.html(page.doc_done, rows=0).replace(page.word, "")
    if mode == "typeahead":
        network = [_get(f"https://{HOST}/search", shell, "text/html"), script]
        for p in prefixes(page.word):
            network += [_get(suggest_url(p), json.dumps({"suggestions": page.full[:5]})),
                        _get(api_url(p), _rows_body(page.full))]
        return network
    network = [_get(f"https://{HOST}/", shell.replace("<ul id=\"results\"></ul>", ""),
                    "text/html"), script]
    for p in prefixes(page.word):
        network += [_get(suggest_url(p), json.dumps({"suggestions": page.full[:5]})),
                    _get(api_url(p, limit=5), _rows_body(page.full[:5]))]
    return network + [_get(page.url, page.served_html(), "text/html"), script,
                      _get(api_url(page.word, limit=50), _rows_body(page.full))]


def recording(word: str, mode: str) -> Dict[str, Any]:
    """What the recorder saw for ``word`` on a page that answered (``never`` is
    recorded as ``xhr``): the full list, the document and every request, with bodies."""
    page = SimPage(word, "xhr" if mode == "never" else mode, 0, tick=False)
    if mode in TYPEAHEAD:
        network = _typed_network(page, mode)
    else:
        network = [_get(page.url, page.served_html(), "text/html"),
                   _get(f"https://{HOST}/static/app.js", "render(fetch('/api/search'))",
                        "application/javascript")]
        shown = page.ssr
        for _sent, _done, url, rows in page.batches:
            batch = page.full[shown:rows]
            shown = rows
            network.append(_get(url, _rows_body(batch)))
    return {"workflow": "fixture_search", "site": HOST, "param": {"name": "q", "value": word},
            "final_url": page.url, "ground_truth_selector": SELECTOR,
            "ui": {"items": list(page.full), "text": visible_text(page.html(1e9))},
            "network": network}


def prev_readiness(tdict: Dict[str, Any]) -> Dict[str, Any]:
    """``tdict`` without the ``value_calls`` rule: the replay waits for the recorded
    request counts and the recorded item count."""
    out = copy.deepcopy(tdict)
    ready = out.get("readiness") or {}
    ready.pop("rule", None)
    ready.pop("value_calls", None)
    return out


def old_readiness(tdict: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """``tdict`` with its readiness as an earlier rule wrote it: no per-pattern request
    counts, and an item floor of min(the first 12 recorded rows, the count floor)."""
    out = prev_readiness(tdict)
    ready = out.get("readiness") or {}
    if ready:
        ready.pop("explaining_min", None)
        capped = max(1, len([i for i in (base["ui"]["items"] or [])[:12] if str(i).strip()]))
        ready["min_items"] = min(capped, int(out["expect"]["min_items"]))
    return out


def templates(mode: str, with_old: bool = False) -> Dict[str, Dict[str, Any]]:
    """The arms' template dicts for ``mode``."""
    from anyuse.execution.api_compile import runtime_rungs
    from anyuse.execution.cdp_replay import template_from_recording
    recs = [recording(w, mode) for w in RECORDED]
    base = recs[0]
    out = {"timer": template_from_recording(base, "q", recordings=recs, readiness=False).to_dict(),
           "ready": template_from_recording(base, "q", recordings=recs, readiness=True).to_dict(),
           "compiled": runtime_rungs(base, recordings=recs)["browser"]["template"]}
    if with_old:
        out["ready-prev"] = prev_readiness(out["ready"])
        out["ready-old"] = old_readiness(out["ready"], base)
    return out


def replay_case(tdict: Dict[str, Any], mode: str, word: str, seed: int,
                tick: Optional[bool] = None) -> Dict[str, Any]:
    """One replay through ``ReplayExecutor.execute``, and what the read held."""
    from anyuse.execution.cdp_replay import ReplayExecutor, ReplayTemplate
    from anyuse.execution.html_extract import extract
    driver = SimDriver(mode, seed, tick)
    out = ReplayExecutor(driver=driver).execute(ReplayTemplate.from_dict(tdict), {"q": word})
    full = full_list(word, mode)
    read = [x for x in extract(driver.dom, SELECTOR, limit=100) if str(x).strip()]
    items = list((out.get("extracted") or {}).get("items") or []) if out.get("success") else None
    page, settle = driver.page, driver.settle
    return {"mode": mode, "word": word, "seed": seed, "tick": bool(page and page.tick),
            "done_ms": (round(page.done_at() * 1000.0, 3) if page and page.done_at() is not None
                        else None),
            "success": bool(out.get("success")), "outcome_kind": out.get("outcome_kind") or "",
            "n_read": len(read), "n_full": len(full), "early": read != full,
            "wrong_success": bool(out.get("success")) and items != full,
            "t_ms": round(page.now * 1000.0, 3) if page else None,
            "dom_reads": len(page.dom_reads) if page else 0,
            "how": settle.get("how") or "", "first_read": settle.get("first_read") or "",
            "error": str(out.get("error") or "")[:160]}


# ---------------------------------------------------------------------------
# one tree
# ---------------------------------------------------------------------------

def run_arm(label: str, tree: str, out_path: str) -> None:
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    from anyuse.execution import cdp_replay as R
    scratch = tempfile.mkdtemp(prefix=f"arm-{label}-", dir=os.path.dirname(out_path))
    os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = os.path.join(scratch, "blocked.json")
    rows: List[Dict[str, Any]] = []
    tmeta: Dict[str, Any] = {}
    t0 = time.monotonic()
    for mode in MODES:
        tmpls = templates(mode, with_old=(label == "NEW"))
        tmeta[mode] = {arm: {"min_items": t["expect"].get("min_items"),
                             "readiness": t.get("readiness") or {}}
                       for arm, t in tmpls.items()}
        for arm, tdict in tmpls.items():
            for word in WORDS:
                for seed in range(SEEDS):
                    rows.append({"arm": f"{label}:{arm}", **replay_case(tdict, mode, word, seed)})
    meta = {"label": label, "tree": tree, "anyuse": real,
            "settle_and_read": hasattr(R.CdpDriver, "_settle_and_read"),
            "templates": tmeta, "wall_s": round(time.monotonic() - t0, 2)}
    with open(out_path, "w") as fh:
        json.dump({"meta": meta, "rows": rows}, fh, indent=1, default=str)
    print(f"{label}: {len(rows)} rows in {meta['wall_s']} s -> {out_path}")


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------

ARMS = ["OLD:timer", "OLD:ready", "OLD:compiled", "NEW:timer", "NEW:ready-old",
        "NEW:ready-prev", "NEW:ready", "NEW:compiled"]
#: modes the value_calls rule must read at most 40% of the timer's median
FAST_MODES = ("xhr", "ssr+xhr") + TYPEAHEAD + ("few",)


def p95(values: List[float]) -> float:
    vals = sorted(values)
    return vals[max(0, -(-95 * len(vals) // 100) - 1)] if vals else 0.0


def summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    times = [r["t_ms"] for r in rows if r["t_ms"] is not None]
    how: Dict[str, int] = {}
    for r in rows:
        how[r["how"] or "-"] = how.get(r["how"] or "-", 0) + 1
    return {"n": len(rows), "early": sum(r["early"] for r in rows),
            "wrong": sum(r["wrong_success"] for r in rows),
            "fail": sum(not r["success"] for r in rows),
            "ok": sum(r["success"] and not r["wrong_success"] for r in rows),
            "median": statistics.median(times) if times else 0.0, "p95": p95(times), "how": how}


def compare(paths: List[str]) -> str:
    rows: List[Dict[str, Any]] = []
    metas = []
    for p in paths:
        with open(p) as fh:
            data = json.load(fh)
        rows.extend(data["rows"])
        metas.append(data["meta"])
    by: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        by.setdefault((r["arm"], r["mode"]), []).append(r)
    lines = []
    for m in metas:
        lines.append(f"{m['label']}: anyuse -> {m['anyuse']}; _settle_and_read: "
                     f"{m['settle_and_read']}; {m['wall_s']} s")
        for mode in MODES:
            t = m["templates"].get(mode) or {}
            lines.append(f"  {mode:<8} " + "; ".join(
                f"{arm}: floor {v['min_items']}, ready floor "
                f"{(v['readiness'] or {}).get('min_items', '-')}, counts "
                f"{(v['readiness'] or {}).get('explaining_min', '-')}, batches "
                f"{(v['readiness'] or {}).get('value_calls', '-')}"
                for arm, v in t.items()))
    arms = [a for a in ARMS if any(k[0] == a for k in by)]
    s = {(a, m): summary(by.get((a, m), [])) for a in arms for m in MODES}
    for mode in MODES:
        lines.append("")
        lines.append(f"{mode}   (n per arm = {s[(arms[0], mode)]['n']})")
        lines.append(f"  {'arm':<14} {'early':>6} {'wrong-ok':>9} {'fail':>5} {'right':>6} "
                     f"{'median ms':>10} {'p95 ms':>8}  settle")
        for a in arms:
            x = s[(a, mode)]
            how = ", ".join(f"{k} {v}" for k, v in sorted(x["how"].items()))
            lines.append(f"  {a:<14} {x['early']:>6} {x['wrong']:>9} {x['fail']:>5} {x['ok']:>6} "
                         f"{x['median']:>10.0f} {x['p95']:>8.0f}  {how}")

    def key(r):
        return (r["mode"], r["word"], r["seed"])

    def same(a, b):
        fields = ("success", "n_read", "t_ms", "how", "early", "wrong_success")
        ra = {key(r): tuple(r[f] for f in fields) for m in MODES for r in by.get((a, m), [])}
        rb = {key(r): tuple(r[f] for f in fields) for m in MODES for r in by.get((b, m), [])}
        return bool(ra) and ra == rb

    new = "NEW:ready"
    checks: Dict[str, bool] = {}
    if "OLD:ready" in arms:
        checks["cross-check: NEW ready-prev equals OLD ready, case by case"] = same(
            "OLD:ready", "NEW:ready-prev")
        checks["cross-check: NEW timer equals OLD timer, case by case"] = same("OLD:timer",
                                                                             "NEW:timer")
    checks["NEW ready: 0 wrong successes in every mode"] = all(
        s[(new, m)]["wrong"] == 0 for m in MODES)
    checks["NEW ready: early reads no higher than the timer in every mode"] = all(
        s[(new, m)]["early"] <= s[("NEW:timer", m)]["early"] for m in MODES)
    checks["NEW ready: 0 early reads outside never"] = all(
        s[(new, m)]["early"] == 0 for m in MODES if m != "never")
    for m in FAST_MODES:
        checks[f"NEW ready: {m} median at most 40% of the timer's"] = (
            s[(new, m)]["median"] <= 0.4 * s[("NEW:timer", m)]["median"])
    checks["NEW ready: lazy and big read the full list or at the cap"] = all(
        (not r["early"]) or r["t_ms"] >= CEILING_MS - 1e-6
        for m in ("lazy", "big") for r in by.get((new, m), []))
    checks["NEW ready: never is read at 10 s +- 1 poll, at the ceiling"] = all(
        r["how"] == "ceiling" and abs(r["t_ms"] - CEILING_MS) <= POLL_MS
        for r in by.get((new, "never"), []))
    checks["NEW compiled tools read like NEW ready, case by case"] = same(new, "NEW:compiled")
    lines.append("")
    for k, ok in checks.items():
        lines.append(f"{'PASS' if ok else 'FAIL'}  {k}")
    lines.append(f"pass criterion: {'holds' if all(checks.values()) else 'does NOT hold'}")
    ref = "NEW:ready-old"
    lines.append(f"the rule without request counts ({ref}): {s[(ref, 'lazy')]['wrong']} wrong "
                 f"successes in lazy, {s[(ref, 'big')]['fail']} failures in big (timer: "
                 f"{s[('NEW:timer', 'big')]['fail']})")
    prev = "OLD:ready" if "OLD:ready" in arms else "NEW:ready-prev"
    for m in TYPEAHEAD + ("few",):
        rows_m = by.get((prev, m), [])
        lines.append(f"the rule with recorded counts ({prev}) on {m}: "
                     f"{sum(r['how'] == 'ceiling' for r in rows_m)} of {len(rows_m)} reads "
                     f"ended at the ceiling")
    for m in MODES:
        lines.append(f"{m:<15} median: timer {s[('NEW:timer', m)]['median']:.0f} ms, "
                     f"{prev} {s[(prev, m)]['median']:.0f} ms, NEW ready "
                     f"{s[(new, m)]['median']:.0f} ms "
                     f"({100.0 * s[(new, m)]['median'] / max(1.0, s[('NEW:timer', m)]['median']):.0f}%)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-readiness-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "readiness_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    home = os.path.join(out, "home")
    os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
    results = []
    for label, tree in trees.items():
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({
            "PYTHONPATH": os.pathsep.join([pkgroot, tree, os.path.join(tree, "tools", "guarded_tests")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", f"api_hosts_{label}.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", f"blocked_{label}.json"),
            "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, ".anyuse", f"robots_{label}.json"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, me, "arm", "--label", label,
                               "--tree", tree, "--out-file", path], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.tree), os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
