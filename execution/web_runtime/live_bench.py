"""Live measurement: ARM S (scripted agent) vs ARM L (escalation ladder), both
under the benchmark traffic policy.

usage: live_bench.py plan
       live_bench.py run    <out-root> --cohort N --arm S|L --browser http://127.0.0.1:PORT
       live_bench.py report <out-root>
       live_bench.py audit-pack <out-root>

The corpus, the arms and the query rule are ``execution/web_runtime/corpus_live.py``. One process runs
one cohort x arm, one site at a time (``SiteGate(per_site=1, max_sites=1)``);
two processes on disjoint sites give at most two sites in parallel.

Per site: robots -> listing (5 values, seeded) -> start-page probe -> the arm's
4 attached sessions -> ``compile last`` (tier 1) -> tier 2 when tier 1 did not
pass -> persist a proved tier-1 tool -> a fresh-process MCP call with q5.

Traffic: see ``docs/TRAFFIC_POLICY.md`` (the benchmark policy). GET/HEAD only;
<= 300 requests per site in total; >= 3 s between navigations; documents and
data <= 1/s; statics <= 4 in flight, <= 30 MB; a block, challenge, 403 or 429
stops the host for good and adds it to NEVER_CONTACT; User-Agent
``research-probe/0.1 (read-only; person-paced)``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import functools
import glob
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import anyuse  # noqa: E402
if os.path.realpath(os.path.dirname(anyuse.__file__)) != os.path.realpath(os.path.dirname(os.path.dirname(HERE))):
    raise SystemExit(f"refused: `anyuse` resolves to {anyuse.__file__}, not this checkout")

import corpus_live as C  # noqa: E402
import ladder_verify as LR  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution.cdp_guard import DOC_MIN_INTERVAL_S, REQUEST_MIN_INTERVAL_S, RequestBudget, TrafficViolation  # noqa: E402,E501

UA = "research-probe/0.1 (read-only; person-paced)"
LR.UA = UA                                  # GuardedReplayDriver reads the module global
#: after Enter, wait up to this long while a request carrying the typed value is
#: still unanswered (the product default is 0 = the readiness timer alone). On
#: www.rubydoc.info the in-page filter's value XHR was still held by the 1/s
#: pacer when the DOM went quiet, so 3 of 4 sessions stored the unfiltered list.
TYPED_INFLIGHT_GRACE_S = 15.0
LR.GetOnlySession.inflight_grace_s = TYPED_INFLIGHT_GRACE_S
SPEC_ID = "SPEC_CL.md"
NEVER_CONTACT = os.path.join(HERE, "NEVER_CONTACT.txt")
PARAM = "q"
TASK_L = ("Use this website's own search to search for {q}, and stay on the page "
          "that shows the search results.")
SITE_CAP = 300
BYTES_CAP = 30 * 1024 * 1024
BUDGET_CB = 1.50
BUDGET_OR = 2.00
ATTACH_MAX_S = 600
#: a corpus's ``ANONYMOUS_BROWSER``: each arm process owns its headless container, its
#: cookie jar is emptied before every attached session, and attach runs with
#: ``--anonymous-browser``. ``corpus_live`` runs without it (False keeps that behaviour).
ANONYMOUS_BROWSER = False
#: DEV only (--force-typed): tier 2 replays by typing even when the result URL
#: carries the value, to exercise the typed replay on a live DEV site
FORCE_TYPED = False
ACTION_SETTLE_S = 2.0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def sid_of(site: str) -> str:
    return "v5_" + "".join(ch if ch.isalnum() else "_" for ch in site.lower())


@contextlib.contextmanager
def locked(path: str):
    """A cross-process lock (the two arm processes share reserves and spend)."""
    with open(path + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_json(path: str, default):
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, default=str)
    os.replace(tmp, path)


def add_never_contact(host: str, why: str) -> None:
    with locked(NEVER_CONTACT):
        with open(NEVER_CONTACT) as fh:
            lines = fh.read().splitlines()
        if host in {ln.strip() for ln in lines if not ln.startswith("#")}:
            return
        head = [ln for ln in lines if ln.startswith("#")]
        body = sorted({ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}
                      | {host})
        head.append(f"# 2026-11-03 (live bench): {host}: {why[:90]}")
        with open(NEVER_CONTACT, "w") as fh:
            fh.write("\n".join(head + body) + "\n")


def page_block(text: str, url: str, network: List[dict]) -> str:
    from attach_bench import page_block as _pb
    return _pb(text, url, network)


class HostPacer(D.Pacer):
    """``discover.Pacer`` with a per-host interval (robots ``Crawl-delay``)."""

    def __init__(self, min_interval_s: float = 1.0):
        super().__init__(min_interval_s)
        self.intervals: Dict[str, float] = {}

    def reserve(self, host: str) -> float:
        now = self.clock()
        last = self.last.get(host)
        gap = 0.0
        if last is not None:
            gap = max(0.0, self.intervals.get(host, self.min_interval_s) - (now - last))
        self.last[host] = now + gap
        self.count[host] = self.count.get(host, 0) + 1
        return gap


def crawl_delay(robots, host: str) -> float:
    rp = robots.parsers.get(host)
    if not rp:
        return 0.0
    best = 0.0
    for tok in ("research-probe", "anyuse-learn", "anyuse-compiled-workflow", "*"):
        try:
            d = rp.crawl_delay(tok)
        except Exception:
            d = None
        if d:
            best = max(best, float(d))
    return best


# ---------------------------------------------------------------------------
# attach (a separate read-only process pinned to the page)
# ---------------------------------------------------------------------------

def start_attach(endpoint: str, target: str, task: str, value: str, log) -> subprocess.Popen:
    extra = ["--anonymous-browser"] if ANONYMOUS_BROWSER else []
    p = subprocess.Popen([sys.executable, "-m", "anyuse", "attach", "--cdp", endpoint,
                          "--target", target, "--task", task, "--input", f"{PARAM}={value}",
                          "--max-seconds", str(ATTACH_MAX_S)] + extra,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.monotonic()
    while True:
        line = p.stdout.readline()
        if not line:
            raise RuntimeError(f"attach exited early rc={p.wait()}")
        log(f"  [attach] {line.rstrip()}")
        if line.strip() == "ATTACHED":
            return p
        if time.monotonic() - t0 > 30:
            p.kill()
            raise RuntimeError("attach did not come up within 30 s")


def stop_attach(p: Optional[subprocess.Popen], log) -> str:
    if p is None:
        return ""
    if p.poll() is None:
        p.send_signal(2)
    try:
        out, _ = p.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    for line in (out or "").splitlines()[-6:]:
        log(f"  [attach] {line}")
    return out or ""


def attached_factory(budget, log, fresh_values=()):
    """``(endpoint, allow_url=..., task=, value=) -> session`` whose page is observed
    by ``anyuse attach`` from before its first navigation until it closes."""

    class AttachedSession(LR.GetOnlySession):
        def __init__(self, endpoint, allow_url=None, task="", value="", **kw):
            super().__init__(endpoint, user_agent=UA, allow_url=allow_url, budget=budget)
            self.attach_task, self.attach_value = task, value
            self._proc = None

        async def __aenter__(self):
            await super().__aenter__()
            if self.attach_task and ANONYMOUS_BROWSER:
                # the jar must be empty when the attach session starts: every
                # cookie it then sees was set by the site in this session
                await self.call("Network.clearBrowserCookies")
            if self.attach_task:
                # in a thread: a blocking readline must never stall the event
                # loop (the page websocket's keepalive runs on it)
                self._proc = await asyncio.to_thread(start_attach, self.endpoint, self._target,
                                                     self.attach_task, self.attach_value, log)
            return self

        async def __aexit__(self, *exc):
            try:
                if "__La" in str(self.attach_task):
                    # the final page of each agent attempt (never a challenge)
                    try:
                        await _shot(self, "attempt_final",
                                    {"page": "ARM L agent attempt, final page",
                                     "task": self.attach_task}, self.attach_value)
                    except Exception:            # noqa: BLE001 - a shot never breaks a run
                        pass
                FAILED_LOG.extend((str(u), str(w)) for u, w in list(self.guard.failed))
                if budget.cache_enabled:
                    try:
                        await self.harvest_cache()
                    except Exception:
                        pass
                await asyncio.to_thread(stop_attach, self._proc, log)
            finally:
                await super().__aexit__(*exc)

    return AttachedSession


#: outcomes that are in NO rate: a site skipped before its turn, and a site
#: whose browser stopped answering (execution/browser_supervisor.py; never a
#: product failure, never re-run, docs/TRAFFIC_POLICY.md)
INFRASTRUCTURE = "infrastructure"
EXCLUDED_OUTCOMES = ("skipped", INFRASTRUCTURE)


def infrastructure_entry(info: Dict[str, Any], exc: BaseException, where: str) -> None:
    """File a site as ``infrastructure``: done, excluded from every rate, not
    re-run (so its host is never contacted beyond what it already got)."""
    from anyuse.execution.browser_supervisor import classify
    info.update(status="done", outcome=INFRASTRUCTURE,
                infrastructure={"kind": classify(exc) or "unknown", "where": where,
                                "error": f"{type(exc).__name__}: {exc}"[:240]})


#: every refused request of the attached sessions, in order (with ``EXT_VALUES`` the agent's
#: POST searches are tagged from it)
FAILED_LOG: List[tuple] = []


# ---------------------------------------------------------------------------
# the page loads of one site
# ---------------------------------------------------------------------------

def _redacted(budget, text: str) -> str:
    """A saved page with provider keys redacted when a web scope is on."""
    if getattr(budget, "web_scope", None) is None:
        return text
    from anyuse.execution.web_scope import redact_keys
    return redact_keys(text)


async def _shot(page, kind: str, decision: Dict[str, Any], value: str = "") -> None:
    """A screenshot at a decision point (execution/web_runtime/shots.py; never a challenge)."""
    if getattr(C, "SCREENSHOTS", False):
        import shots as SH
        await SH.shoot(page, kind, decision, value)


async def _plain(bfetch, url: str, inputs: bool = True, shot: str = "") -> Dict[str, Any]:
    """One guarded, unattached load (the listing, the start-page probe)."""
    out: Dict[str, Any] = {"url": url}
    t0 = time.monotonic()
    async with bfetch._factory(bfetch.endpoint, allow_url=bfetch._allowed) as page:
        await page.goto(url)
        await bfetch._settler._settle(page, t0)
        if shot:
            await _shot(page, "landing", {"page": shot, "rule": "listing/start page loaded"})
        out.update(dom=await page.dom(), text=await page.text(), final_url=await page.url(),
                   network=await page.network(want_bodies=False),
                   sent=sum(page.guard.count.values()), failed=list(page.guard.failed[:20]))
        if inputs:
            out["search_inputs"] = await page.search_inputs()
        if bfetch.budget.cache_enabled:
            await page.harvest_cache()
    return out


async def _suggest_probe(bfetch, start_url: str, seed: str) -> Dict[str, Any]:
    """The ``query_values`` source "suggest": open the start page, type ONE seed letter
    into its first visible search input (no Enter), wait for the page to
    settle, and read the same-host JSON answers that arrived meanwhile."""
    import query_values as VV
    from anyuse.execution.cdp_guard import FOCUS_INPUT_JS
    out: Dict[str, Any] = {"responses": [], "tokens": [], "note": ""}
    t0 = time.monotonic()
    async with bfetch._factory(bfetch.endpoint, allow_url=bfetch._allowed) as page:
        await page.goto(start_url)
        await bfetch._settler._settle(page, t0)
        inputs = [i for i in await page.search_inputs() if i.get("visible")]
        if not inputs:
            out["note"] = "no visible search input"
            return out
        n0 = len(await page.network(want_bodies=False))
        if not await page.js(FOCUS_INPUT_JS % int(inputs[0]["index"])):
            out["note"] = "could not focus the input"
            return out
        await page.call("Input.insertText", {"text": seed})
        await page.settle(4.0)
        host = D.host_of(start_url)
        net = (await page.network(want_bodies=True))[n0:]
        for r in net:
            if r.get("resource_type") not in ("XHR", "Fetch") or D.host_of(r.get("url", "")) != host:
                continue
            body = r.get("body") or ""
            if body:
                out["responses"].append((r.get("url", ""), body))
        out["tokens"] = VV.suggest_tokens(out["responses"], seed)
        out["responses"] = [(u[:160], len(b)) for u, b in out["responses"]]
        if bfetch.budget.cache_enabled:
            await page.harvest_cache()
    return out


async def _session_s(bfetch, factory, entry: dict, value: str, task: str) -> Dict[str, Any]:
    """ARM S: type into the first visible search input, press Enter (attached)."""
    out: Dict[str, Any] = {"value": value, "task": task}
    t0 = time.monotonic()
    async with factory(bfetch.endpoint, allow_url=bfetch._allowed, task=task,
                       value=value) as page:
        await page.goto(entry["start_url"])
        await bfetch._settler._settle(page, t0)
        text0, url0 = await page.text(), await page.url()
        out["start_text_head"] = (text0 or "")[:2000]
        out["start_final_url"] = url0
        blk = page_block(text0, url0, await page.network(want_bodies=False))
        if blk:
            out["blocked"] = f"start page: {blk}"
            return out
        out["before_dom"] = await page.dom()
        await _shot(page, "landing", {"page": "start page before typing", "task": task})
        probe = await page.type_search(value)
        out["probe"] = {k: probe.get(k) for k in ("input", "document", "note", "refused")}
        out["probe"]["xhr"] = list(probe.get("xhr") or [])[:10]
        out["final_url"] = await page.url()
        out["text"] = (await page.text() or "")[:20000]
        net = await page.network(want_bodies=False)
        out["doc_statuses"] = [(r.get("url", "")[:120], r.get("status"))
                               for r in net if r.get("resource_type") == "Document"][:10]
        blk = page_block(out["text"], out["final_url"], net)
        if blk:
            out["blocked"] = f"result page: {blk}"
        else:
            await _shot(page, "result", {"page": "ARM S result page", "task": task,
                                         "probe_note": (probe or {}).get("note")}, value)
        out["guard_failed"] = [list(f) for f in page.guard.failed[:30]]
    out["seconds"] = round(time.monotonic() - t0, 2)
    return out


def substitute(text: str, q1: str, value: str) -> str:
    if not q1:
        return text
    if text.strip().lower() == q1.strip().lower():
        return value
    low = text.lower()
    i = low.find(q1.lower())
    if i < 0:
        return text
    return text[:i] + value + text[i + len(q1):]


async def _trace_replay(bfetch, factory, entry: dict, steps: List[dict], q1: str, value: str,
                        task: str = "", compact: bool = True) -> Dict[str, Any]:
    """ARM L: the solved attempt's actions, replayed deterministically (0 model calls)."""
    from anyuse.execution.agent_browser import AgentRun, _resolve_consent_wall, action_index
    out: Dict[str, Any] = {"value": value, "task": task, "actions": []}
    t0 = time.monotonic()
    kw = {"task": task, "value": value} if task else {}
    async with factory(bfetch.endpoint, allow_url=bfetch._allowed, **kw) as page:
        await page.goto(entry["start_url"])
        await bfetch._settler._settle(page, t0)
        await _resolve_consent_wall(page, AgentRun(), f"agent:{entry['site']}")
        text0, url0 = await page.text(), await page.url()
        blk = page_block(text0, url0, await page.network(want_bodies=False))
        if blk:
            out["blocked"] = f"start page: {blk}"
            return out
        for st in steps:
            if not isinstance(st, dict) or "action" not in st or st.get("deterministic"):
                continue
            if (st.get("outcome") or {}).get("acted") is False:
                continue                      # refused in the run (the page had changed)
            kind = str(st.get("action"))
            if kind == "done":
                break
            idx = action_index(st)
            note = kind
            if kind == "type":
                if "before_dom" not in out:
                    out["before_dom"] = await page.dom()
                typed = substitute(str(st.get("text", q1)), q1, value)
                ok = await page.focus(idx)
                if ok:
                    # select what the field holds so the typed text REPLACES it
                    # (data.stadt-zuerich.ch: a trace that typed twice into
                    # a pre-filled field replayed as "WeiterbildungWeiterbildung")
                    await page.js(SELECT_FIELD_JS)
                    await page.type(typed)
                note = f"type[{idx}] {typed!r} focus={ok}"
            elif kind == "search":
                # the agent's one-step search, replayed as the same
                # transaction with the new value
                if "before_dom" not in out:
                    out["before_dom"] = await page.dom()
                typed = substitute(str(st.get("text", q1)), q1, value)
                res = await page.search(idx, typed)
                note = f"search[{idx}] {typed!r} {res.get('submit') if isinstance(res, dict) else ''}"
            elif kind == "submit":
                await page.focus(idx)
                await page.press_enter()
                note = f"submit[{idx}]"
            elif kind == "click":
                await page.click(idx)
                note = f"click[{idx}]"
            else:
                note = kind
            out["actions"].append(note)
            await page.settle(ACTION_SETTLE_S)
            if kind in ("submit", "click", "search") and compact:
                # compact: stop at the first submit/click after
                # which the page meets the deterministic task check. The agent's
                # later, redundant actions re-searched, clicked suggestions or
                # left the result page (www.nhs.uk replays ended on a
                # "did you mean" page, www.behindthename.com ran into the site
                # ceiling replaying 9 actions per value).
                url_now, text_now = await page.url(), await page.text()
                if agent_success(entry["start_url"], text0, url_now, text_now, value):
                    out["stopped_early"] = f"after action {len(out['actions'])} of the trace"
                    break
        out["final_url"] = await page.url()
        out["dom"] = await page.dom()
        out["text"] = (await page.text() or "")[:20000]
        net = await page.network(want_bodies=False)
        blk = page_block(out["text"], out["final_url"], net)
        if blk:
            out["blocked"] = f"result page: {blk}"
        else:
            await _shot(page, "result", {"page": "ARM L trace replay result page",
                                         "task": task, "actions": out["actions"][-4:]}, value)
        out["guard_failed"] = [list(f) for f in page.guard.failed[:30]]
    out["seconds"] = round(time.monotonic() - t0, 2)
    return out


#: select the focused text field's content (the next insertText replaces it)
SELECT_FIELD_JS = ("(function(e){if(e&&typeof e.select==='function'&&'value' in e)"
                   "{e.select();return true}return false})(document.activeElement)")


def agent_success(start_url: str, start_text: str, final_url: str, text: str, value: str,
                  blocked: str = "") -> bool:
    """On the start host, not blocked/error, changed, names q1."""
    if blocked or not final_url or not final_url.startswith(("http://", "https://")):
        return False
    if D.host_of(final_url) != D.host_of(start_url):
        return False
    norm = lambda u: str(u or "").split("#", 1)[0].rstrip("/")
    changed = norm(final_url) != norm(start_url) or (text or "")[:2000] != (start_text or "")[:2000]
    return changed and value.lower() in (text or "").lower()


# ---------------------------------------------------------------------------
# compile last (tier 1), tier 2, persist, MCP
# ---------------------------------------------------------------------------

def sessions_by_task(tasks: List[str]) -> List[Optional[dict]]:
    from anyuse.execution import attach as A
    all_s = A.list_sessions()
    out = []
    for t in tasks:
        same = [s for s in all_s if s.get("task") == t]
        out.append(same[-1] if same else None)
    return out


def compile_tier1(sessions: List[dict], executor, checker) -> Dict[str, Any]:
    from anyuse.execution import attach as A
    from anyuse.execution.api_verify import permission_preflight
    row: Dict[str, Any] = {}
    first: Dict[str, Any] = {}

    def gate(compiled):
        return permission_preflight(compiled, first, allowed_origins=row.get("origins"),
                                    checker=checker)
    try:
        name, values, _how = A.infer_param(sessions)
        s0 = A.result_snapshot(sessions[0], values[0]) or {}
        first["final_url"] = s0.get("url") or ""
        row["origins"] = sorted({A._origin(r.get("url", "")) for r in
                                 sessions[0].get("network") or [] if r.get("url")})
        t0 = time.monotonic()
        compiled, proof, report, recs = A.compile_sessions(sessions, executor=executor,
                                                           preflight=gate)
        row["compile_ms"] = round((time.monotonic() - t0) * 1000.0)
    except A.AttachError as exc:
        row.update(tier1="failed", tier1_reason=f"compile refused: {exc}"[:300],
                   tier1_template=False)
        return row
    row["status"] = report.get("status")
    row["split"] = dict(proof.split or {})
    row["scope"] = proof.scope
    row["selector"] = report.get("ground_truth_selector")
    row["values"] = report.get("values")
    row["shape"] = report.get("shape")
    row["template_url"] = getattr(compiled.template, "url", "") if compiled.template else ""
    row["tier1_template"] = compiled.template is not None
    row["rung"] = getattr(compiled.template, "extract_kind", "") if compiled.template else ""
    row["compile_reason"] = (compiled.reason or "")[:300]
    row["proof_reason"] = (proof.reason or "")[:300]
    row["cases"] = [{"value": c.value, "passed": c.passed,
                     "coverage": round(float(getattr(c, "coverage", 0.0) or 0.0), 2),
                     "ui": getattr(c, "ui_count", None), "api": getattr(c, "api_count", None),
                     "field": getattr(c, "field", None), "reason": (c.reason or "")[:120],
                     "ms": round(getattr(c, "elapsed_ms", 0.0) or 0.0, 1)} for c in proof.cases]
    row["tier1_model_calls"] = 0
    if proof.passed:
        row["tier1"] = "proved" if proof.effective_status == "proved" else "checked"
    else:
        row["tier1"] = "failed"
        row["tier1_reason"] = (proof.reason or compiled.reason or "")[:300]
    row["_compiled"], row["_proof"] = compiled, proof
    return row


def tier2(row: Dict[str, Any], sessions: List[dict], values: List[str], before: List[str],
          replay_nav, replay_typed, typed_input: dict, log, force: bool = False,
          force_typed: bool = False) -> None:
    """Fit on q1 + q2 only, replay q3 + q4 fresh, grade each
    against its own recorded page read with the fitted selector."""
    import ladder as L
    from anyuse.execution import attach as A
    from anyuse.execution.api_compile import page_chrome, visible_items
    from anyuse.execution.cdp_replay import for_validation, template_from_recording
    from anyuse.execution.discover_replay import TypedReplayTemplate
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth
    if row.get("tier1") in ("proved", "checked") and not force:
        row["tier2"] = "not reached"
        return
    recs = [A.session_to_recording(s, PARAM, v, f"attach{i}")
            for i, (s, v) in enumerate(zip(sessions, values))]
    for r, b in zip(recs, before):
        if b:
            r["before_dom"] = b
    if not all(r.get("dom") for r in recs):
        row.update(tier2="failed", tier2_reason="a session has no stored result page snapshot")
        return
    fit, val = recs[:2], recs[2:]
    selector = derive_ground_truth(fit)
    vis = any(r.get("ground_truth_visible_only") for r in fit)
    row["tier2_selector"] = selector
    row["tier2_visible_only"] = vis
    if not selector:
        row.update(tier2="failed", tier2_reason=(fit[0].get("recorder_warning") or
                                                 "no result list derivable on q1 + q2")[:300])
        return
    for r in val:
        r["ground_truth_selector"] = selector
        r.setdefault("ui", {})["items"] = extract(r["dom"], selector, limit=30,
                                                  visible_only=vis) if r.get("dom") else []
    chrome = page_chrome(fit)
    nav = all(A._value_in(v, r["final_url"]) for v, r in zip(values, recs)) and not force_typed
    row["tier2_mode"] = "navigate" if nav else "typed"
    if nav:
        template = template_from_recording(recs[0], PARAM, recordings=fit, readiness=True)
        if not template.url:
            row.update(tier2="failed", tier2_reason=f"refused: {template.notes[:200]}")
            return
        proof_t, runner = for_validation(template), replay_nav
    else:
        if replay_typed is None or not typed_input:
            row.update(tier2="failed", tier2_reason="typed mode: no recorded input or trace")
            return
        proof_t = TypedReplayTemplate(url=sessions_start(sessions, recs), params=[PARAM],
                                      input=dict(typed_input), extract={"items": selector},
                                      visible_only=vis,
                                      expect={"selector": selector, "min_items": 1},
                                      origin="bench-typed")
        runner = replay_typed
    cases, lats, audit = [], [], []
    for r in val:
        value = (r.get("param") or {}).get("value", "")
        out = runner.execute(proof_t, {PARAM: value})
        if out.get("elapsed_ms"):
            lats.append(out["elapsed_ms"])
        if not out.get("success"):
            cases.append((False, f"{value!r}: {str(out.get('error'))[:120]}"))
            if out.get("blocked") or out.get("requires_human"):
                row["tier2_blocked"] = str(out.get("error"))[:160]
            continue
        got = [str(x) for x in (out.get("extracted") or {}).get("items") or []]
        want = visible_items(r, exclude=chrome)
        ok, note = L.correctness(got[:40], want, min_values=3,
                                 value=value if getattr(C, "EXT_VALUES", False) else "")
        cases.append((ok, f"{value!r}: {note}"))
        audit.append({"value": value, "replayed_items": got[:10]})
    row["tier2_url"] = proof_t.url
    row["tier2_ms"] = [round(x, 1) for x in lats]
    row["tier2_cases"] = [c[1] for c in cases]
    row["tier2_model_calls"] = 0
    row["tier2_replayed"] = audit
    if len(cases) >= 2 and all(c[0] for c in cases):
        row["tier2"] = "proved"
    else:
        row["tier2"] = "failed"
        row["tier2_reason"] = "; ".join(c[1] for c in cases if not c[0])[:300]


def sessions_start(sessions, recs) -> str:
    return str((sessions[0].get("_start_url") or "")) or recs[0]["final_url"]


def persist_tier1(row: Dict[str, Any], site: str, wf: str) -> str:
    from anyuse.execution.api_compile import compiled_record, persist_compiled
    from anyuse.memory.storage import WorkflowStorage
    compiled, proof = row.get("_compiled"), row.get("_proof")
    if compiled is None or compiled.template is None or not proof.passed:
        return ""
    record = compiled_record(compiled.template, proof.to_dict(), workflow=wf,
                             # a proved detail-shape tool (attach's item-page path) is a
                             # namespace without chrome_dropped
                             chrome_filtered=list(getattr(compiled, "chrome_dropped", None) or ()),
                             source={"origin": "attach", "site": site,
                                     "ground_truth": "captured result-page text (docs/ATTACH.md)",
                                     "bench": "live"})
    return persist_compiled(WorkflowStorage(), wf, record)


def mcp_call(site: str, value: str, log, wf: str = "") -> Dict[str, Any]:
    """A fresh process runs the MCP server; list tools; call ours with ``value``."""
    t0 = time.monotonic()
    env = dict(os.environ, V5_USER_AGENT=UA)
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "v5_mcp_server.py")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=env)
    out: Dict[str, Any] = {"value": value}
    try:
        def rpc(i, method, params):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                      "params": params}) + "\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    raise RuntimeError("the MCP server closed its output")
                m = json.loads(line)
                if m.get("id") == i:
                    return m
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "live-bench", "version": "0"}})
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
                      + "\n")
        p.stdin.flush()
        tools = rpc(2, "tools/list", {})["result"]["tools"]
        from anyuse.mcpserver.compiled_tools import tool_host_piece, typed_tool_name
        piece = tool_host_piece(site)
        mine = [t for t in tools if t["name"].startswith(piece + "__")]
        if wf:
            exact = typed_tool_name(site, wf)
            mine = [t for t in mine if t["name"] == exact or t["name"].startswith(exact + "_")]
        out["tools"] = [t["name"] for t in mine]
        if not mine:
            out.update(ok=False, error="no typed tool for this host in tools/list")
            return out
        tool = mine[0]
        props = list(((tool.get("inputSchema") or {}).get("properties") or {}).keys())
        arg = props[0] if props else PARAM
        t1 = time.monotonic()
        res = rpc(3, "tools/call", {"name": tool["name"], "arguments": {arg: value}})
        out["call_ms"] = round((time.monotonic() - t1) * 1000.0, 1)
        result = res.get("result") or {}
        body = result.get("structuredContent")
        if body is None:
            try:
                body = json.loads((result.get("content") or [{}])[0].get("text") or "{}")
            except ValueError:
                body = {}
        items = body.get("result") or {}
        items = (items.get("items") if isinstance(items, dict) else items) or []
        out.update(ok=bool(body.get("ok")), n_items=len(items),
                   items=[str(x)[:120] for x in items[:10]],
                   error=str(body.get("error") or "")[:200],
                   served_by=body.get("served_by"), model_calls=body.get("model_calls"),
                   empty_state=body.get("empty_state"))
    except Exception as exc:
        out.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        try:
            p.stdin.close()
            p.terminate()
            p.wait(timeout=10)
        except Exception:
            p.kill()
        out["wall_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    log(f"  [mcp] {json.dumps({k: out.get(k) for k in ('ok', 'n_items', 'error', 'call_ms')})}")
    return out


# ---------------------------------------------------------------------------
# reserves and spend, shared by the two arm processes
# ---------------------------------------------------------------------------

def take_reserve(root: str, stratum: str, replaced: str) -> Optional[str]:
    path = os.path.join(root, "reserves.json")
    with locked(path):
        used = load_json(path, {"used": {}})
        taken = set(used["used"])
        from anyuse.execution.gov_hosts import government_host
        for e in C.reserves(stratum):
            if government_host(e["site"]) or e["site"] in _never():
                continue                    # a government host / NEVER_CONTACT
            if e["site"] not in taken:
                used["used"][e["site"]] = {"for": replaced, "at": time.strftime("%H:%M:%S")}
                save_json(path, used)
                return e["site"]
    return None


def spend(root: str, add: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    path = os.path.join(root, "spend.json")
    with locked(path):
        cur = load_json(path, {"cerebras_usd": 0.0, "openrouter_usd": 0.0})
        if add:
            for k, v in add.items():
                cur[k] = round(cur.get(k, 0.0) + float(v or 0.0), 6)
            save_json(path, cur)
        return cur


# ---------------------------------------------------------------------------
# one site
# ---------------------------------------------------------------------------

def run_site(ctx, site: str, arm: str, cohort: int, info: Dict[str, Any],
             force_tier2: bool = False, wf: str = "") -> str:
    """Returns the bucket name if the site is replaced, else ''.

    ``force_tier2`` (DEV only) also runs tier 2 when tier 1 proved, to exercise
    the typed-search replay; ``wf`` names the persisted workflow (default: the
    site id), so two arms on one DEV site keep two tools."""
    from anyuse.execution import attach as A
    import bench_ack
    fetch, robots, budget, bfetch, log = (ctx[k] for k in ("fetch", "robots", "budget",
                                                           "bfetch", "log"))
    entry = C.BY_SITE[site]
    sid = sid_of(site)
    host = site
    t_start = time.monotonic()
    req_start = budget.site_total(host)
    info.update(site=site, arm=arm, cohort=cohort, stratum=entry["stratum"], id=sid,
                status="started", started=time.strftime("%H:%M:%S"))
    if getattr(C, "SCREENSHOTS", False):
        import shots as SH
        SH.CURRENT["sid"] = f"{sid}_{arm}"
    scope = budget.web_scope
    phases: Dict[str, Dict[str, int]] = {}

    def phase(name: str) -> None:
        # the web-scope allowances used so far on this site, per phase
        if scope is not None:
            phases[name] = dict(scope.usage.get(site, {}))

    def stop_host(why: str) -> None:
        if own_cap(why):
            # our own 300-request ceiling, not the site refusing us: no block,
            # no NEVER_CONTACT (otherwise extensions.gnome.org was listed for it)
            info["site_cap"] = why[:200]
            log(f"{site:30s} site ceiling reached (ours, not a block): {why[:100]}")
            return
        fetch.blocked[host] = why
        budget.stopped[host] = why
        add_never_contact(host, why)
        info["blocked"] = why[:200]
        log(f"{site:30s} BLOCKED, host stopped and added to NEVER_CONTACT: {why[:100]}")

    def finish(outcome: str = "") -> str:
        if scope is not None:
            phase("end")
            info["web_scope"] = scope.site_report(site)
            info["web_scope_phases"] = phases
            info["web_scope_proof"] = proof_usage(phases)
        info["seconds"] = round(time.monotonic() - t_start, 1)
        info["requests"] = budget.site_total(host) + info.get("mcp_requests", 0)
        info["requests_this_task"] = info["requests"] - req_start
        info["bytes"] = budget.site_bytes.get(budget.site_of(host), 0)
        info["status"] = "done"
        return outcome

    # -- robots -----------------------------------------------------------------
    robots.load(host)
    info["robots"] = robots.notes.get(host)
    if str(info["robots"]).startswith("robots.txt 40") or "blocked" in str(info["robots"]):
        stop_host(f"robots.txt: {info['robots']}")
    bad = [u for u in (entry["start_url"], entry["listing_url"]) if not robots.allow(u)]
    if bad:
        info["bucket_why"] = f"robots.txt ({info['robots']}) disallows {bad[0]}"
        return finish("robots")
    delay = crawl_delay(robots, host)
    if delay > 1.0:
        fetch.pacer.intervals[host] = delay
        info["crawl_delay"] = delay
    info["bench_ack"] = bench_ack.acknowledge_public_hosts([host], robots.root_ok)
    # -- listing and values -------------------------------------------------------
    lst = asyncio.run(_plain(bfetch, entry["listing_url"], shot="listing"))
    budget.check()
    with open(os.path.join(ctx["out"], f"{sid}.listing.html"), "w") as fh:
        fh.write(_redacted(budget, lst.get("dom") or ""))
    info["listing"] = {"final_url": lst.get("final_url"), "sent": lst.get("sent"),
                       "failed": lst.get("failed")}
    blk = page_block(lst.get("text", ""), lst.get("final_url", ""), lst.get("network"))
    if blk:
        stop_host(f"listing: {blk}")
        info.update(outcome="blocked", failure="blocked")
        return finish()
    values = C.query_values(site, lst.get("dom") or "")
    if getattr(C, "EXT_VALUES", False) and C.needs_suggestions(lst.get("dom") or ""):
        # the ``query_values`` rule, last source: the site's own suggestions while ONE
        # seed letter is typed on the start page
        import query_values as VV
        seed = VV.seed_letter([t for t, _s in VV.listing_tokens(lst.get("dom") or "")])
        sug = asyncio.run(_suggest_probe(bfetch, entry["start_url"], seed))
        budget.check()
        info["suggest"] = {"seed": seed, "note": sug.get("note"),
                           "responses": len(sug.get("responses") or []),
                           "tokens": sug.get("tokens", [])[:30]}
        values = C.query_values(site, lst.get("dom") or "", suggestions=sug.get("tokens") or [])
    if getattr(C, "EXT_VALUES", False):
        srcs = C.value_sources(lst.get("dom") or "", (info.get("suggest") or {}).get("tokens") or [])
        info["value_sources"] = {v: srcs.get(v, "") for v in values}
    info["values"] = values
    log(f"{site:30s} values {values}")
    if len(values) < getattr(C, "N_ELIGIBLE", C.N_VALUES):
        info["bucket_why"] = f"the listing yields {len(values)} qualifying token(s)"
        return finish("query_rule")
    budget.protect(values)
    if scope is not None:
        # the POST-search rule's evidence: the values this run will type here
        scope.note_values(site, values)
    phase("listing")
    # -- start-page probe ------------------------------------------------------------
    if entry["start_url"] == entry["listing_url"]:
        probe = lst
    else:
        probe = asyncio.run(_plain(bfetch, entry["start_url"], shot="start page"))
        budget.check()
        blk = page_block(probe.get("text", ""), probe.get("final_url", ""), probe.get("network"))
        if blk:
            stop_host(f"start page: {blk}")
            info.update(outcome="blocked", failure="blocked")
            return finish()
    visible = [i for i in probe.get("search_inputs") or [] if i.get("visible")]
    info["search_inputs_visible"] = len(visible)
    start_text = probe.get("text") or ""
    if not visible:
        info["bucket_why"] = "the start page shows no visible search-like input"
        return finish("no_search_input")
    factory = attached_factory(budget, log)
    tasks = [f"{sid}__{arm}{k}" for k in range(1, 5)]
    before: List[str] = ["", "", "", ""]
    typed_input: Dict[str, Any] = {}
    replay_typed = None
    from anyuse.execution.discover_replay import GuardedReplay
    if arm in ("S", "T"):
        # ARM T is ARM S with the third-party policy allow_with_blocklist
        runs = []
        for k, value in enumerate(values[:4]):
            s = asyncio.run(_session_s(bfetch, factory, entry, value, tasks[k]))
            budget.check()
            runs.append({x: y for x, y in s.items() if x != "before_dom"})
            before[k] = s.get("before_dom") or ""
            pr = s.get("probe") or {}
            log(f"{site:30s} S{k + 1} {value!r}: {str(s.get('final_url', ''))[:90]} "
                f"{s.get('blocked') or pr.get('note') or ''}")
            refused = search_refused(pr.get("refused") or [], value)
            if s.get("blocked"):
                stop_host(s["blocked"])
                break
            if refused and k == 0:
                info["runs"] = runs
                info["bucket_why"] = str(refused)[:200]
                return finish("robots_search_path")
            if k == 0:
                info["agent_success"] = agent_success(entry["start_url"], start_text,
                                                      s.get("final_url", ""), s.get("text", ""),
                                                      value, s.get("blocked", ""))
                info["search_class"] = search_class(s)
                if pr.get("input"):
                    typed_input = {k2: pr["input"].get(k2) for k2 in ("index", "name", "id")}
        info["runs"] = runs
        if info.get("blocked"):
            info.update(outcome="blocked", failure="blocked")
            return finish()
        replay_typed = GuardedReplay(bfetch.navigator, bfetch.typed_open,
                                     host_blocked=lambda h: fetch.blocked.get(h, "")
                                     or budget.stopped.get(h, ""))
        session_tasks = tasks
    else:
        from anyuse.execution.agent_browser import EscalatingAgent, default_levels
        from anyuse.execution.model_adapter import RunBudget
        sp = spend(ctx["root"])
        cb_left, or_left = BUDGET_CB - sp["cerebras_usd"], BUDGET_OR - sp["openrouter_usd"]
        levels, or_run = [], or_left
        for lv in default_levels():
            if lv.name == "L1":
                if cb_left >= lv.max_usd:
                    levels.append(lv)
            elif or_run >= lv.max_usd:
                levels.append(lv)
                or_run -= lv.max_usd
        info["levels"] = [lv.name for lv in levels]
        attempts: List[str] = []

        def agent_factory(endpoint, allow_url=None, **kw):
            task = f"{sid}__La{len(attempts) + 1}"
            attempts.append(task)
            return factory(endpoint, allow_url=allow_url, task=task, value=values[0])

        agent = EscalatingAgent(levels=levels, endpoint=bfetch.endpoint,
                                session_factory=agent_factory, allow_url=bfetch._allowed,
                                run_budget=RunBudget(max(0.0, cb_left) + max(0.0, or_left)),
                                pause_s=3.0)
        t_agent = time.monotonic()
        failed0 = len(FAILED_LOG)
        run = agent.run(TASK_L, entry["start_url"], values[0])
        post = sorted({u for u, w in FAILED_LOG[failed0:]
                       if "method POST" in w and D.host_of(u) == host})
        if post:
            info["post_refused"] = post[:5]
        budget.check()
        esc = run.escalation or {}
        info["agent_seconds"] = round(time.monotonic() - t_agent, 1)
        if esc.get("stopped") == "infrastructure":
            from anyuse.execution.browser_supervisor import InfrastructureFailure
            raise InfrastructureFailure("agent_browser", "the agent's browser stopped "
                                        "answering: " + str((esc.get("attempts") or [{}])[-1]
                                                            .get("error") or "")[:160])
        cost = {"cerebras_usd": 0.0, "openrouter_usd": 0.0}
        for a in esc.get("attempts") or []:
            cost["cerebras_usd" if a.get("level") == "L1" else "openrouter_usd"] += \
                float(a.get("usd") or 0.0)
        spend(ctx["root"], cost)
        info["cost"] = {k: round(v, 6) for k, v in cost.items()}
        info["model_calls"] = run.model_calls
        info["escalation"] = {k: esc.get(k) for k in ("levels", "pruned_levels", "attempts",
                                                       "escalations", "solved_at", "stopped",
                                                       "triage")}
        info["agent_steps"] = [s for s in run.steps if isinstance(s, dict)][:40]
        info["agent_final_url"] = run.final_url
        info["agent_error"] = run.error
        with open(os.path.join(ctx["out"], f"{sid}.agent.html"), "w") as fh:
            fh.write(_redacted(budget, run.dom or ""))
        blk = str(esc.get("stopped") or "")
        blk = blk if blk.startswith("blocked") else page_block(run.text or "", run.final_url or "",
                                                              run.network or [])
        if blk:
            stop_host(f"agent: {blk}")
            info.update(outcome="blocked", failure="blocked", agent_success=False)
            return finish()
        info["agent_success"] = agent_success(entry["start_url"], start_text, run.final_url,
                                              run.text, values[0])
        info["search_class"] = search_class({"xhr": [], "doc_statuses": [],
                                             "guard_failed": [], "final_url": run.final_url},
                                            run_network=run.network)
        solved = esc.get("solved_at")
        robots_refused = [u for u in (run.refused or []) if D.host_of(u) == host
                          and not robots.allow(u)]
        if not solved:
            if robots_refused and str(run.final_url or "").startswith("chrome-error"):
                info["bucket_why"] = f"robots refused the agent's search: {robots_refused[:2]}"
                return finish("robots_search_path")
            if getattr(C, "EXT_VALUES", False):
                # the SEARCH request of ANY attempt refused by robots.txt
                # (www.8notes.com: search_fsm.asp?keyword=Music refused in
                # attempts 1-2, attempt 3 then stayed on the start page)
                searchy = agent_search_refused(list(agent.refused or []), values[0], host,
                                               robots.allow)
                if searchy:
                    info["bucket_why"] = f"robots refused the agent's search: {searchy[:2]}"
                    return finish("robots_search_path")
            info["outcome"] = "agent_failed"
            return finish()
        solved_i = max(i for i, a in enumerate(esc.get("attempts") or []) if a.get("goal_ok"))
        q1_task = attempts[solved_i] if solved_i < len(attempts) else attempts[-1]
        steps = [s for s in run.steps if isinstance(s, dict)]
        info["trace"] = steps
        runs = []
        for k in (1, 2, 3):
            r = asyncio.run(_trace_replay(bfetch, factory, entry, steps, values[0], values[k],
                                          tasks[k]))
            budget.check()
            before[k] = r.pop("before_dom", "") or ""
            r.pop("dom", None)
            runs.append(r)
            log(f"{site:30s} R{k + 1} {values[k]!r}: {str(r.get('final_url', ''))[:90]} "
                f"{r.get('blocked') or ''} {r.get('actions')}")
            if r.get("blocked"):
                stop_host(r["blocked"])
                break
        info["runs"] = runs
        if info.get("blocked"):
            info.update(outcome="blocked", failure="blocked")
            return finish()
        session_tasks = [q1_task] + tasks[1:]
        typed_input = {"trace": len(steps)}

        def trace_typed_open(url, value, spec, _steps=steps, _q1=values[0]):
            from anyuse.execution.agent_browser import AgentRun
            r = asyncio.run(_trace_replay(bfetch, bfetch._factory_plain, entry, _steps, _q1,
                                          value))
            budget.check()
            return AgentRun(value=value, start_url=url, final_url=r.get("final_url", ""),
                            dom=r.get("dom", ""), text=r.get("text", ""),
                            error=("blocked: " + r["blocked"]) if r.get("blocked") else "")
        replay_typed = GuardedReplay(bfetch.navigator, trace_typed_open,
                                     host_blocked=lambda h: fetch.blocked.get(h, "")
                                     or budget.stopped.get(h, ""))
    phase("sessions")
    # -- capture ------------------------------------------------------------------
    sessions = sessions_by_task(session_tasks)
    info["session_ids"] = [s.get("id") if s else None for s in sessions]
    for t, s in zip(session_tasks, sessions):
        if s is not None:
            s["_start_url"] = entry["start_url"]
    usable = [usable_snapshot(s, v) for s, v in zip(sessions, values[:4])]
    info["usable_capture"] = all(usable)
    withheld = sorted({str(sn.get("withheld"))[:160] for s in sessions if s
                       for sn in s.get("snapshots") or [] if sn.get("withheld")})
    if withheld:
        # attach keeps no DOM of a page loaded with a session-looking cookie
        # (docs/ATTACH.md: treated as signed in); reported, never worked around
        info["capture_withheld"] = withheld
    arch = os.path.join(ctx["out"], "attach_sessions")
    os.makedirs(arch, exist_ok=True)
    for s in glob.glob(os.path.join(A.sessions_dir(), "*.json")):
        try:
            with open(s) as fh:
                task = json.load(fh).get("task", "")
        except Exception:
            continue
        if str(task).startswith(sid + "__"):
            shutil.copy2(s, os.path.join(arch, os.path.basename(s)))
    if not info["usable_capture"]:
        info["outcome"] = "capture_failed"
        return finish()
    # -- tier 1 -------------------------------------------------------------------
    row = compile_tier1(sessions, ctx["executor"], ctx["checker"])
    budget.check()
    phase("tier1")
    if host in fetch.blocked and row.get("tier1") != "proved":
        stop_host(f"compiled request: {fetch.blocked[host]}")
    # -- tier 2 -------------------------------------------------------------------
    if not info.get("blocked"):
        tier2(row, sessions, values[:4], before, ctx["replay_nav"], replay_typed, typed_input,
              log, force=force_tier2, force_typed=FORCE_TYPED)
        budget.check()
        phase("tier2")
        if row.get("tier2_blocked"):
            stop_host(f"tier 2: {row['tier2_blocked']}")
    compiled = row.pop("_compiled", None)
    proof = row.pop("_proof", None)
    info.update(row)
    info["compile"] = bool(row.get("tier1_template")) or bool(row.get("tier2_url"))
    info["proved"] = "proved" in (row.get("tier1"), row.get("tier2"))
    # the audit's evidence: every validation page as captured (no repair)
    info["audit_pages"] = audit_pages(sessions, values[:4])
    # -- persist + MCP (tier 1 only) --------------------------------------------------
    if row.get("tier1") == "proved" and not info.get("blocked"):
        wf = wf or sid
        row["_compiled"], row["_proof"] = compiled, proof
        info["persisted"] = persist_tier1(row, site, wf)
        import re as _re
        url = (getattr(compiled.template, "url", "") or "")
        # the MCP value is separate from proof eligibility: q5 when the
        # pool had five values, else q1 (a fitting value, labelled as such)
        mcp_value = values[4] if len(values) > 4 else values[0]
        info["mcp_value_role"] = "q5" if len(values) > 4 else "q1 (fitting value; no fifth)"
        probe_url = _re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}",
                            urllib.parse.quote(mcp_value), url)
        if budget.site_refusal(host):
            info["mcp"] = {"ok": False, "error": budget.site_refusal(host)}
        elif not robots.allow(probe_url if "://" in probe_url else entry["start_url"]):
            info["mcp"] = {"ok": False, "error": "robots.txt disallows the call URL"}
        else:
            last = fetch.pacer.last.get(host, 0.0)
            wait = last + 3.0 - fetch.pacer.clock()
            if wait > 0:
                time.sleep(wait)
            info["mcp"] = mcp_call(site, mcp_value, log, wf=wf)
            fetch.pacer.count[host] = fetch.pacer.count.get(host, 0) + 1
            fetch.pacer.last[host] = fetch.pacer.clock()
    elif row.get("tier2") == "proved":
        info["mcp"] = {"ok": False, "error": "MCP n/a: MCP serves tier 1 only"}
    info["outcome"] = "proved" if info["proved"] else "failed"
    log(f"{site:30s} tier1={info.get('tier1')} tier2={info.get('tier2')} "
        f"mcp={(info.get('mcp') or {}).get('ok')} {(info.get('tier1_reason') or '')[:80]}")
    return finish()


def own_cap(why: str) -> bool:
    """Is this 'block' our own request ceiling rather than the site's answer?"""
    low = str(why or "").lower()
    return any(k in low for k in ("host cap of", "site ceiling of", "per-host cap"))


def search_refused(refused: List[Any], value: str) -> List[Any]:
    """The robots refusals of the SEARCH request itself: those whose URL carries
    the typed value. Any refused request of the typing session is not enough for
    ``robots_search_path`` (data.ontario.ca: a disallowed
    ``/api/i18n/en`` XHR while the search document ``/dataset/?q=`` was sent)."""
    low = (value or "").lower()
    out = []
    for f in refused:
        url, why = str(f[0]), str(f[1])
        if not why.startswith("robots_refused") or not low:
            continue
        if low in urllib.parse.unquote_plus(url).lower():
            out.append(f)
    return out


#: query keys that make a refused same-host URL the site's SEARCH request
SEARCH_KEYS = {"q", "query", "search", "s", "keyword", "keywords", "term", "k", "text",
               "searchword", "search_query", "find"}


def agent_search_refused(refused: List[str], value: str, host: str, allow) -> List[str]:
    """The agent's refused same-host requests that ARE the search: robots
    disallows them and they carry the typed value, or a search-like query key
    (``/search?q=&is_quick=true``, www.yeastgenome.org)."""
    out = []
    low = (value or "").lower()
    for u in refused:
        if D.host_of(u) != host or allow(u):
            continue
        q = urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query, keep_blank_values=True)
        if (low and low in urllib.parse.unquote_plus(u).lower()) or \
                any(k.lower() in SEARCH_KEYS for k, _v in q):
            out.append(u)
    return out


NO_LIST_CLASSES = ("unchanged", "not_rendered", "empty_results", "error_pages")


def no_list_class(e: Dict[str, Any]) -> str:
    """The named reason of a "no result list" refusal (learn_loop.no_list_diagnosis)."""
    import re as _re
    for k in ("tier1_reason", "tier2_reason"):
        m = _re.search(r"\((" + "|".join(NO_LIST_CLASSES) + r"):", str(e.get(k) or ""))
        if m:
            return m.group(1)
    return ""


def usable_snapshot(session: Optional[dict], value: str) -> bool:
    """A usable capture is a stored result page (DOM or text) that is not the
    blank tab: ``result_snapshot``'s fall-back to the blank first
    snapshot is not usable when attach withheld every real page."""
    from anyuse.execution import attach as A
    if session is None:
        return False
    snap = A.result_snapshot(session, value)
    if snap is None:
        return False
    url = str(snap.get("url") or "")
    return url.startswith(("http://", "https://")) and bool(snap.get("dom") or snap.get("text"))


def audit_pages(sessions: List[dict], values: List[str]) -> List[dict]:
    from anyuse.execution import attach as A
    out = []
    for i, (s, v) in enumerate(zip(sessions, values)):
        if i < 2 or s is None:
            continue
        snap = A.result_snapshot(s, v) or {}
        dom = snap.get("dom") or ""
        import re as _re
        m = _re.search(r"<title[^>]*>(.*?)</title>", dom, _re.I | _re.S)
        out.append({"value": v, "url": snap.get("url"),
                    "title": " ".join((m.group(1) if m else "").split())[:160],
                    "text": " ".join((snap.get("text") or "").split())[:1500]})
    return out


def search_class(s: Dict[str, Any], run_network=None) -> str:
    """Observed class of the search (reported only): post_refused | get_document |
    get_xhr | none."""
    failed = s.get("guard_failed") or []
    if any("method POST" in str(f[1]) or "method" in str(f[1]) and "POST" in str(f[1])
           for f in failed):
        return "post_refused"
    probe = s.get("probe") or {}
    if probe.get("document"):
        return "get_document"
    if probe.get("xhr"):
        return "get_xhr"
    if run_network is not None:
        docs = [r for r in run_network if r.get("resource_type") == "Document"]
        xhr = [r for r in run_network if r.get("resource_type") in ("XHR", "Fetch")]
        if len(docs) > 1:
            return "get_document"
        if xhr:
            return "get_xhr"
    return "none"


# ---------------------------------------------------------------------------
# the run (one cohort x arm)
# ---------------------------------------------------------------------------

def make_supervisor(args, log):
    """The run's browser supervisor. ``--container NAME`` (an
    ``anyuse-browser-*`` name only) lets it restart that container: between
    sites with ``--restart-between-sites``, and whenever the browser does not
    answer before a site."""
    from anyuse.execution.browser_supervisor import (BrowserSupervisor, ContainerController,
                                                     ContainerSpec)
    endpoint = str(args.browser)
    controller = None
    name = str(getattr(args, "container", "") or "")
    if name:
        port = urllib.parse.urlsplit(endpoint).port or 9345
        controller = ContainerController(ContainerSpec(
            name=name, memory=str(getattr(args, "container_memory", "2g") or "2g"),
            cpus=int(getattr(args, "container_cpus", 2) or 2),
            publish=f"127.0.0.1:{port}:9222"))
    return BrowserSupervisor(endpoint, container=controller,
                             restart_between_sites=bool(getattr(args, "restart_between_sites",
                                                                False)),
                             log=log)


def run(args) -> int:
    import bench_ack
    import discover_bench as DBN
    import discover_browser as DBR
    from anyuse.execution.api_permission import check_permission
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.execution.traffic_gate import SiteGate

    if not bench_ack.enabled():
        print("refused: set ANYUSE_BENCH_ACK_PUBLIC_HOSTS=1 with a scratch ANYUSE_DATA_DIR")
        return 2
    bench_ack.check_scratch_registry()
    root = os.path.abspath(args.out)
    pilot = bool(getattr(args, "pilot", "")) or bool(getattr(args, "dev", ""))
    dev = getattr(args, "dev", "")
    if dev == "all":
        tag = getattr(C, "DEV_ROUND", "")
        out = os.path.join(root, f"dev_all_{args.arm}" + (f"_{tag}" if tag else ""))
    elif dev:
        out = os.path.join(root, f"dev_{sid_of(dev)[3:]}_{args.arm}")
    else:
        out = os.path.join(root, f"pilot_{args.arm}" if pilot else f"c{args.cohort}_{args.arm}")
    os.makedirs(out, exist_ok=True)
    logf = open(os.path.join(out, "run.log"), "a")

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    head = subprocess.run(["git", "-C", os.path.dirname(os.path.dirname(HERE)), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    state_path = os.path.join(out, "state.json")
    state: Dict[str, Any] = {"spec": SPEC_ID, "user_agent": UA, "code": head,
                             "cohort": args.cohort, "arm": args.arm,
                             "knobs": {"site_cap": SITE_CAP, "per_load_cap": SITE_CAP,
                                       "nav_min_interval_s": 3.0, "min_interval_s": 1.0,
                                       "static_in_flight": 4, "static_bytes_cap": BYTES_CAP,
                                       "realistic_browser": True, "load_per_navigation": True,
                                       "site_gate": {"per_site": 1, "max_sites": 1},
                                       "cache": "static only",
                                       "anonymous_browser": ANONYMOUS_BROWSER},
                             "entries": {}, "ledger": [], "order": [],
                             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    prior = load_json(state_path, None)
    if prior:
        for k in ("entries", "ledger", "order", "started_at"):
            if k in prior:
                state[k] = prior[k]
        state["resumed_from"] = prior.get("generated_at")
        state.setdefault("prior_requests", []).append(prior.get("requests"))
        state["code_at_start"] = prior.get("code_at_start", prior.get("code"))
    else:
        state["code_at_start"] = head

    pacer = HostPacer(1.0)
    if hasattr(C, "ACTIVE_COHORT"):
        C.ACTIVE_COHORT = int(args.cohort)
    # cohort 2 and DEV (a corpus with ``STATIC_RESERVE``): the static share + benchmark static skipping
    fix = getattr(C, "STATIC_RESERVE", None) is not None and (int(args.cohort) == 2 or bool(dev))
    state["knobs"]["static_reserve"] = C.STATIC_RESERVE if fix else None
    state["knobs"]["skip_types"] = list(C.SKIP_TYPES) if fix else []
    scope = web_scope_for(args.arm, dev=bool(dev), log=log)
    if scope is not None:
        state["knobs"]["web_scope"] = scope.knobs()

    def allow(u):
        # robots.txt of every host the web scope allowed for the site (a
        # provider, an own host, a third party) is fetched once, lazily, and
        # counted against the site (docs/TRAFFIC_POLICY.md "Web scope")
        h = D.host_of(u)
        if scope is not None and h not in robots.parsers and scope.site_for(h):
            robots.load(h)
        return robots.allow(u)
    fetch = D.HttpFetcher(user_agent=UA, allow_url=allow, pacer=pacer,
                          max_requests_per_host=SITE_CAP)
    robots = DBN.Robots(fetch)
    gate = SiteGate(per_site=1, max_sites=1)
    budget = RequestBudget(pacer, per_load_cap=SITE_CAP, per_host_cap=SITE_CAP,
                           site_cap=SITE_CAP, nav_min_interval_s=DOC_MIN_INTERVAL_S, load_per_navigation=True,
                           realistic_browser=True, static_bytes_cap=BYTES_CAP,
                           static_in_flight=4, task_gate=gate, web_scope=scope,
                           static_reserve=(C.STATIC_RESERVE if fix else None),
                           skip_types=(tuple(C.SKIP_TYPES) if fix else ()))
    budget.bind_fetcher(fetch)
    if getattr(C, "SCREENSHOTS", False):
        import shots as SH
        SH.CURRENT["dir"] = os.path.join(out, "shots")
        LR.SHOT = SH.shoot
    sends: List[tuple] = []
    _reserve = pacer.reserve

    def _recorded(host):
        gap = _reserve(host)
        sends.append((host, pacer.last[host]))
        return gap
    pacer.reserve = _recorded
    plain_factory = functools.partial(LR.GetOnlySession, user_agent=UA, budget=budget)
    bfetch = DBR.BrowserFetcher(args.browser, fetch, budget, session_factory=plain_factory,
                                cache=True)
    bfetch._factory_plain = lambda endpoint, allow_url=None, **kw: plain_factory(
        endpoint, allow_url=allow_url)
    supervisor = make_supervisor(args, log)
    executor = ApiStepExecutor(transport=D.PacedTransport(fetch))
    checker = functools.partial(check_permission, fetcher=robots.text_for)
    replay_nav = ReplayExecutor(driver=LR.GuardedReplayDriver(bfetch, budget))
    ctx = {"fetch": fetch, "robots": robots, "budget": budget, "bfetch": bfetch, "log": log,
           "executor": executor, "checker": checker, "replay_nav": replay_nav, "out": out,
           "root": root}

    def write():
        state["requests"] = dict(pacer.count)
        state["bytes"] = dict(budget.site_bytes)
        state["static_peak"] = budget.static_peak
        last, gaps, docgaps = {}, {}, {}
        for h, t in sends:
            if h in last:
                gaps[h] = min(gaps.get(h, 1e9), round(t - last[h], 3))
            last[h] = t
        lastd = {}
        for h, t, _u, ty in budget.sent:
            if ty == "Document":
                if h in lastd:
                    docgaps[h] = min(docgaps.get(h, 1e9), round(t - lastd[h], 3))
                lastd[h] = t
        state["min_gap_paced_s"] = gaps
        state["min_gap_documents_s"] = docgaps
        state["violations"] = list(budget.violations)
        state["third_party_refused"] = len(budget.third_party_refused)
        state["refused_methods"] = [list(x) for x in budget.refused_methods[:50]]
        state["n_refused_methods"] = len(budget.refused_methods)
        state["blocked"] = dict(fetch.blocked)
        state["stopped"] = dict(budget.stopped)
        state["browser_methods"] = dict(LR.GetOnlyGuard.methods)
        state["validation_evidence"] = budget.validation_evidence()
        state["cache_hits"] = dict(budget.cache_hits)
        state["cache_entries"] = len(budget.cache)
        state["cache_refused"] = dict(budget.cache_refused)
        state["static_skipped"] = dict(getattr(budget, "static_skipped", {}) or {})
        if budget.web_scope is not None:
            state["preflights_unpaused"] = len(budget.preflights)
        if getattr(C, "SCREENSHOTS", False):
            import shots as SH
            state["screenshots"] = dict(SH.COUNTS)
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if budget.web_scope is not None:
            # no provider key outside the tool record (docs/THREAT_MODEL.md)
            from anyuse.execution.web_scope import redact_obj
            save_json(state_path, redact_obj(state))
        else:
            save_json(state_path, state)

    arms = list(args.arm) if dev else [args.arm]
    if dev == "all":
        # DEV re-run: every DEV site of this arm with budget left today
        queue = [f"{s}#{args.arm}" for s in C.dev_sites() if C.DEV_ARM.get(s) == args.arm]
        state.setdefault("dev_skipped", C.dev_skipped())
    elif dev:
        queue = [f"{dev}#{a}" for a in arms]
    else:
        queue = ([args.pilot] if pilot else [s for s in C.queue(args.cohort, args.arm)]) + \
            [l["in"] for l in state["ledger"] if l.get("in")]
    try:
        while queue:
            key = queue.pop(0)
            site, _, arm_k = key.partition("#")
            arm_k = arm_k or args.arm
            prior_e = state["entries"].get(key) or {}
            if prior_e.get("status") == "done":
                continue
            if prior_e.get("status") == "started":
                # the previous process stopped mid-site: discarded, never
                # re-run (its host already got part of its budget)
                state.setdefault("restarted", []).append(site)
                prior_e.update(status="done", outcome=INFRASTRUCTURE,
                               infrastructure={"kind": "process_stopped",
                                               "where": "resume",
                                               "error": "the run stopped mid-site; not "
                                                        "re-run, not replaced"})
                write()
                continue
            from anyuse.execution.gov_hosts import government_host
            if government_host(site):
                # a government host: never contacted
                if not prior_e:
                    state["entries"][key] = {"site": site, "arm": arm_k, "status": "done",
                                             "outcome": "skipped",
                                             "why": "skipped: government host (owner rule)"}
                    write()
                continue
            if site in fetch.blocked or site in _never():
                if not prior_e:
                    state["entries"][site] = {"site": site, "status": "done", "outcome": "skipped",
                                              "why": "on NEVER_CONTACT before its turn"}
                continue
            first = state["entries"].get(f"{site}#{arms[0]}") or {} if dev else {}
            if dev and key != f"{site}#{arms[0]}" and first.get("bucket") in (
                    "robots", "query_rule", "no_search_input"):
                # a site-level bucket is arm-independent: do not load the site again
                state["entries"][key] = {"site": site, "arm": arm_k, "status": "done",
                                         "outcome": "replaced", "bucket": first["bucket"],
                                         "bucket_why": "same as arm " + arms[0]}
                write()
                continue
            info: Dict[str, Any] = {}
            state["entries"][key] = info
            state["order"].append(key)
            seed = int(getattr(C, "screen_requests", lambda _s: 0)(site) or 0)
            if seed and site not in state.setdefault("screen_seeded", []):
                # the eligibility screen's requests (robots.txt + one home page
                # GET) count against this site's ceiling
                pacer.count[site] = pacer.count.get(site, 0) + seed
                state["screen_seeded"].append(site)
            write()
            try:
                # health BEFORE the site is contacted (robots.txt included);
                # a restart first when configured or when the browser is down
                supervisor.require(site)
            except Exception as exc:              # noqa: BLE001
                if getattr(C, "STOP_ON_DEAD_BROWSER", False) and not getattr(args, "container", ""):
                    # the browser is down BEFORE this site got any request. Filing it
                    # (and every later site of the queue) as infrastructure would leave
                    # the queue unrun. Stop instead; the external supervisor recreates
                    # the container and resumes, and this never-contacted site is
                    # queued again.
                    state["entries"].pop(key, None)
                    if key in state["order"]:
                        state["order"].remove(key)
                    write()
                    log(f"{site:30s} browser down before any request: stopping for a "
                        f"container restart ({exc})")
                    return 4
                infrastructure_entry(info, exc, "before the site")
                log(f"{site:30s} INFRASTRUCTURE before any request: {exc}")
                write()
                continue
            try:
                with gate.task_sync(site):
                    bucket = run_site(ctx, site, arm_k, args.cohort, info,
                                      force_tier2=bool(getattr(args, "force_tier2", False)),
                                      wf=(f"{sid_of(site)}_{arm_k}" if dev else ""))
            except TrafficViolation:
                raise
            except Exception as exc:              # noqa: BLE001
                from anyuse.execution.browser_supervisor import confirm
                infra = confirm(exc, bfetch.endpoint)
                if infra is None:
                    raise
                infrastructure_entry(info, infra, "mid-site")
                log(f"{site:30s} INFRASTRUCTURE mid-site (discarded, not re-run): {infra}")
                bucket = ""
            finally:
                try:
                    supervisor.after_site(site)
                except Exception:                 # noqa: BLE001
                    pass
            if bucket:
                info["outcome"] = "replaced"
                info["bucket"] = bucket
                nxt = None if pilot else take_reserve(root, C.BY_SITE[site]["stratum"], site)
                state["ledger"].append({"out": site, "bucket": bucket,
                                        "why": str(info.get("bucket_why"))[:200], "in": nxt})
                if nxt:
                    queue.append(nxt)
                log(f"{site:30s} REPLACED ({bucket}): {str(info.get('bucket_why'))[:100]} -> {nxt}")
            write()
    except TrafficViolation as exc:
        state["traffic_violation"] = str(exc)[:400]
        write()
        log(f"TRAFFIC VIOLATION: {exc}")
        return 3
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write()
    return 0


def web_scope_for(arm: str, dev: bool = False, log=print):
    """The run's web scope from the four settings (``execution/web_scope.py``),
    checked against what the corpus fixed for this arm: a mismatch
    refuses the run (the settings are part of the protocol). A corpus without
    ``ARM_SCOPE`` must run with every setting off."""
    from anyuse.execution.web_scope import WebScope
    # (the protocol names the four settings as they were: the legacy reader; the product's scope is
    # ANYUSE_THIRD_PARTY, web_scope.WebScope.from_settings)
    scope = WebScope.from_legacy_settings()
    want = getattr(C, "ARM_SCOPE", None)
    got = scope.knobs() if scope is not None else None
    if want is None:
        if scope is not None:
            raise SystemExit(f"refused: web-scope settings are on ({got}) but "
                             f"{C.__name__} specified none")
        return None
    exp = want.get(arm[:1] if dev else arm)
    if exp is None:
        raise SystemExit(f"refused: {C.__name__} specified no web scope for arm {arm}")
    have = {k: (got or {}).get(k, False if k != "ANYUSE_THIRD_PARTY_POLICY" else "block")
            for k in exp}
    if have != exp:
        raise SystemExit(f"refused: the settings {have} are not the specified web "
                         f"scope of arm {arm}: {exp}")
    log(f"web scope for arm {arm}: {exp}")
    return scope


def proof_usage(phases: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
    """Web-scope requests per allowance in each part of a site run: the agent's
    sessions (capture) and the proof (tier 1 + tier 2), from the cumulative
    per-phase counters."""
    order = ["listing", "sessions", "tier1", "tier2", "end"]
    seen = [p for p in order if p in phases]
    out: Dict[str, Dict[str, int]] = {}
    prev: Dict[str, int] = {}
    for p in seen:
        cur = phases[p]
        out[p] = {k: cur.get(k, 0) - prev.get(k, 0) for k in cur if cur.get(k, 0) - prev.get(k, 0)}
        prev = cur
    capture = {}
    for p in ("listing", "sessions"):
        for k, v in (out.get(p) or {}).items():
            capture[k] = capture.get(k, 0) + v
    proof = {}
    for p in ("tier1", "tier2"):
        for k, v in (out.get(p) or {}).items():
            proof[k] = proof.get(k, 0) + v
    return {"capture": capture, "proof": proof}


def _never() -> set:
    from anyuse.execution.web_runtime.never_contact import never_contact
    with open(NEVER_CONTACT, encoding="utf-8") as fh:
        return never_contact() | {ln.strip().lower() for ln in fh if ln.strip() and not ln.startswith("#")}


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

TAXONOMY = ("blocked", "agent_no_search", "agent_policy", "agent_goal", "agent_provider",
            "capture", "capture_cookie", "replay", "compile", "proof_sparse", "proof_mismatch", "site_cap",
            "mcp", "other")
#: ``EXT_VALUES``: named sub-classes (reported only; an ``EXT_VALUES`` run reports these instead of
#: the generic class they refine)
TAXONOMY_EXT = ("search_post", "results_unchanged", "results_not_rendered",
                "results_empty")


def failure_of(e: Dict[str, Any]) -> str:
    """One primary reason per failed eligible site; an ``EXT_VALUES``
    run refines ``agent_*`` / ``compile`` into :data:`TAXONOMY_EXT`."""
    base = _failure_of(e)
    if not getattr(C, "EXT_VALUES", False):
        return base
    if base in ("agent_no_search", "agent_policy", "agent_goal") and (
            e.get("post_refused") or e.get("search_class") == "post_refused"):
        return "search_post"
    if base in ("compile", "other", "agent_goal"):
        cls = no_list_class(e)
        if cls == "unchanged":
            return "results_unchanged"
        if cls == "not_rendered":
            return "results_not_rendered"
        if cls in ("empty_results", "error_pages"):
            return "results_empty"
    return base


def _failure_of(e: Dict[str, Any]) -> str:
    """One primary reason per failed eligible site."""
    if e.get("outcome") == "blocked" or e.get("blocked"):
        return "blocked"
    reasons = " ".join(str(e.get(k) or "") for k in ("tier1_reason", "tier2_reason",
                                                    "agent_error"))
    if "site ceiling" in reasons or "host cap" in reasons:
        return "site_cap"
    if e.get("outcome") == "agent_failed":
        esc = e.get("escalation") or {}
        stopped = str(esc.get("stopped") or "")
        if stopped in ("provider_unreachable", "provider_empty", "model_unreadable",
                       "budget_exhausted", "global budget exhausted",
                       "no configured model credentials"):
            return "agent_provider"
        trig = [a.get("trigger") for a in esc.get("attempts") or []]
        if "policy" in trig:
            return "agent_policy"
        if trig and all(t in ("abstain", "no_progress") for t in trig if t):
            return "agent_no_search"
        return "agent_goal"
    runs = e.get("runs") or []
    if e.get("arm") in ("S", "T") and runs:
        pr = runs[0].get("probe") or {}
        fails = " ".join(str(f[1]) for f in runs[0].get("guard_failed") or [])
        if pr.get("note"):
            return "agent_no_search"
        if not e.get("agent_success"):
            if "method" in fails or "third-party" in fails:
                return "agent_policy"
            return "agent_goal"
    if e.get("arm") == "L" and not e.get("usable_capture", True):
        if e.get("capture_withheld"):
            return "capture_cookie"
        return "replay" if any(not r.get("final_url") for r in runs) else "capture"
    if e.get("outcome") == "capture_failed":
        return "capture_cookie" if e.get("capture_withheld") else "capture"
    if not e.get("compile"):
        return "compile"
    if "insufficient evidence" in reasons:
        return "proof_sparse"
    if e.get("proved") and not (e.get("mcp") or {}).get("ok"):
        return "mcp"
    if not e.get("proved"):
        if "coverage" in reasons or "reproduce" in reasons:
            return "proof_mismatch"
        return "compile" if "refused" in reasons else "other"
    return ""


def primary_failure(e: Dict[str, Any]) -> str:
    """'' for a full success (proved, audited correct, MCP ok), else one reason."""
    if e.get("proved"):
        if e.get("audit") not in (None, "correct"):
            return "audit_wrong"
        if not (e.get("mcp") or {}).get("ok"):
            return "mcp"
        return ""
    return failure_of(e) or "other"


def newcombe(k1, n1, k2, n2):
    import ladder as L
    if not n1 or not n2:
        return (0.0, 0.0, 0.0)
    p1, l1, u1 = L.wilson(k1, n1)
    p2, l2, u2 = L.wilson(k2, n2)
    d = p1 - p2
    lo = d - ((p1 - l1) ** 2 + (u2 - p2) ** 2) ** 0.5
    hi = d + ((u1 - p1) ** 2 + (p2 - l2) ** 2) ** 0.5
    return d, lo, hi


def collect(root: str) -> Dict[str, Any]:
    rows = []
    for d in sorted(glob.glob(os.path.join(root, "c*_*"))):
        st = load_json(os.path.join(d, "state.json"), None)
        if not st:
            continue
        for site, e in st["entries"].items():
            e = dict(e)
            e.setdefault("site", site)
            e["cohort"], e["arm"] = st["cohort"], st["arm"]
            rows.append(e)
    audit = load_json(os.path.join(root, "audit.json"), {})
    for e in rows:
        a = audit.get(e.get("site")) or {}
        e["audit"] = a.get("verdict")
    return {"rows": rows, "spend": load_json(os.path.join(root, "spend.json"), {})}


def report(root: str) -> str:
    import ladder as L
    data = collect(root)
    rows = data["rows"]

    def cell(k, n):
        if not n:
            return f"{k} / 0"
        p, lo, hi = L.wilson(k, n)
        return f"{k} / {n} ({100 * p:.0f} %, [{100 * lo:.0f}, {100 * hi:.0f}])"

    lines = ["| cohort | arm | sampled | eligible | blocked | agent ok | usable capture | compile | "
             "proved (T1/T2) | MCP ok | proved + audit ok (eligible) | proved (all sampled) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    groups = [(c, a) for c in (1, 2, "1+2") for a in ("S", "L")]
    summary = {}
    for c, a in groups:
        rs = [e for e in rows if e.get("arm") == a and (c == "1+2" or e.get("cohort") == c)
              and e.get("status") == "done" and e.get("outcome") not in EXCLUDED_OUTCOMES]
        sampled = len(rs)
        elig = [e for e in rs if e.get("outcome") != "replaced"]
        n = len(elig)
        blocked = sum(1 for e in elig if e.get("outcome") == "blocked")
        ag = sum(1 for e in elig if e.get("agent_success"))
        cap = sum(1 for e in elig if e.get("usable_capture"))
        comp = sum(1 for e in elig if e.get("compile"))
        pr = [e for e in elig if e.get("proved")]
        t1 = sum(1 for e in pr if e.get("tier1") == "proved")
        t2 = sum(1 for e in pr if e.get("tier2") == "proved")
        mcp = sum(1 for e in elig if (e.get("mcp") or {}).get("ok"))
        good = sum(1 for e in pr if e.get("audit") == "correct")
        summary[(c, a)] = (good, n, len(pr), sampled)
        lines.append(f"| {c} | {a} | {sampled} | {n} | {blocked} | {cell(ag, n)} | {cell(cap, n)} | "
                     f"{cell(comp, n)} | {cell(len(pr), n)} ({t1}/{t2}) | {cell(mcp, n)} | "
                     f"{cell(good, n)} | {cell(len(pr), sampled)} |")
    for c in (1, 2, "1+2"):
        gS, nS = summary[(c, "S")][0], summary[(c, "S")][1]
        gL, nL = summary[(c, "L")][0], summary[(c, "L")][1]
        d, lo, hi = newcombe(gL, nL, gS, nS)
        lines.append(f"\nL - S (cohort {c}, proved + audit ok, eligible): {100 * d:+.0f} points, "
                     f"Newcombe 95 % [{100 * lo:+.0f}, {100 * hi:+.0f}]")
    # taxonomy: one primary reason per eligible site that did not reach
    # proved + audited correct + MCP ok
    lines.append("\n| failure (eligible sites, both cohorts) | S | L |\n|---|---|---|")
    for t in TAXONOMY + (TAXONOMY_EXT if getattr(C, "EXT_VALUES", False) else ()) + ("audit_wrong",):
        k = {a: sum(1 for e in rows if e.get("arm") == a and e.get("status") == "done"
                    and e.get("outcome") not in ("replaced",) + EXCLUDED_OUTCOMES
                    and primary_failure(e) == t) for a in ("S", "L")}
        lines.append(f"| {t} | {k['S']} | {k['L']} |")
    # traffic and cost
    done = [e for e in rows if e.get("status") == "done"
            and e.get("outcome") not in EXCLUDED_OUTCOMES]
    req = [e.get("requests") or 0 for e in done]
    byt = [e.get("bytes") or 0 for e in done]
    if req:
        lines.append(f"\nRequests per site: median {statistics.median(req):.0f}, max {max(req)}; "
                     f"bytes per site: median {statistics.median(byt) / 1e6:.1f} MB, "
                     f"max {max(byt) / 1e6:.1f} MB")
    for a in ("S", "L"):
        rs = [e for e in done if e.get("arm") == a and e.get("outcome") != "replaced"]
        secs = [e.get("seconds") or 0 for e in rs]
        pr = sum(1 for e in rs if e.get("proved"))
        usd = sum(sum((e.get("cost") or {}).values()) for e in rs)
        if secs:
            q = sorted(secs)
            lines.append(f"ARM {a}: setup time per eligible site median {statistics.median(secs) / 60:.1f} min, "
                         f"p95 {q[max(0, int(0.95 * len(q)) - 1)] / 60:.1f} min; total "
                         f"{sum(secs) / 60:.0f} min; per proved tool "
                         f"{(sum(secs) / pr / 60) if pr else float('nan'):.1f} min; model US$ {usd:.4f}, "
                         f"per proved tool {(usd / pr) if pr else float('nan'):.4f}")
    lines.append(f"Spend ledger: {data['spend']}")
    # per site
    lines.append("\n| cohort | arm | site | stratum | class | outcome | agent | T1 | T2 | MCP | audit | "
                 "req | MB | failure / reason |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for e in sorted(done, key=lambda x: (x.get("cohort"), x.get("arm"), x.get("site"))):
        why = e.get("bucket_why") or e.get("tier1_reason") or ""
        if e.get("tier2") == "failed" and e.get("tier2_reason"):
            why = f"{why} // t2: {e['tier2_reason']}"
        if e.get("blocked"):
            why = e["blocked"]
        lines.append(f"| {e.get('cohort')} | {e.get('arm')} | {e.get('site')} | {e.get('stratum')} | "
                     f"{e.get('search_class', '')} | {e.get('bucket') or e.get('outcome')} | "
                     f"{'y' if e.get('agent_success') else 'n'} | {e.get('tier1', '')} | "
                     f"{e.get('tier2', '')} | {(e.get('mcp') or {}).get('ok', '')} | "
                     f"{e.get('audit') or ''} | {e.get('requests', '')} | "
                     f"{(e.get('bytes') or 0) / 1e6:.1f} | "
                     f"{(failure_of(e) + ': ') if e.get('outcome') not in ('replaced',) and not e.get('proved') else ''}"
                     f"{str(why)[:140].replace('|', '/')} |")
    return "\n".join(lines)


def audit_pack(root: str) -> str:
    """The blind pack: every proved tool's validation pages + MCP answer, shuffled,
    with no arm, tier or outcome label."""
    import random
    data = collect(root)
    items = []
    for e in data["rows"]:
        if not e.get("proved"):
            continue
        items.append({"site": e["site"], "pages": e.get("audit_pages") or [],
                      "mcp": {k: (e.get("mcp") or {}).get(k) for k in ("value", "items", "n_items")}
                      if (e.get("mcp") or {}).get("ok") else None})
    random.Random("anyuse-live-audit").shuffle(items)
    path = os.path.join(root, "audit_pack.json")
    save_json(path, items)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--prior", default="")
    p.add_argument("--corpus", default="corpus_live", choices=("corpus_live",))
    r = sub.add_parser("run")
    r.add_argument("out")
    r.add_argument("--cohort", type=int, required=True, choices=(1, 2))
    r.add_argument("--arm", required=True, choices=("S", "L", "T", "SL", "LS"))
    r.add_argument("--corpus", default="corpus_live", choices=("corpus_live",))
    r.add_argument("--dev", default="", help="DEV gate: one previously contacted site "
                   "(corpus DEV set); --arm SL runs both arms in one process, one budget")
    r.add_argument("--force-typed", action="store_true",
                   help="DEV only: tier 2 in typed mode (TypedReplayTemplate)")
    r.add_argument("--force-tier2", action="store_true",
                   help="DEV only: run tier 2 even when tier 1 proved (typed replay check)")
    r.add_argument("--browser", required=True)
    r.add_argument("--container", default="",
                   help="an anyuse-browser-* container the supervisor may restart "
                        "(published on the --browser port); no other container is touched")
    r.add_argument("--container-memory", default="2g")
    r.add_argument("--container-cpus", type=int, default=2)
    r.add_argument("--restart-between-sites", action="store_true",
                   help="a fresh container before every site (needs --container)")
    r.add_argument("--pilot", default="",
                   help="DEV pilot on one non-corpus host (spec section 12)")
    q = sub.add_parser("report")
    q.add_argument("out")
    q.add_argument("--corpus", default="corpus_live", choices=("corpus_live",))
    a = sub.add_parser("audit-pack")
    a.add_argument("out")
    a.add_argument("--corpus", default="corpus_live", choices=("corpus_live",))
    args = ap.parse_args(argv)
    corpus = getattr(args, "corpus", "") or os.environ.get("BENCH_CORPUS", "corpus_live")
    if corpus != "corpus_live":
        import importlib
        global C, SPEC_ID, ANONYMOUS_BROWSER
        C = importlib.import_module(corpus)
        SPEC_ID = getattr(C, "SPEC_ID", SPEC_ID)
        ANONYMOUS_BROWSER = bool(getattr(C, "ANONYMOUS_BROWSER", False))
    if getattr(args, "dev", "") and args.dev != "all" and args.dev not in getattr(C, "DEV", {}):
        ap.error(f"--dev {args.dev}: not in {corpus}.DEV")
    if getattr(args, "dev", "") == "all" and not hasattr(C, "dev_sites"):
        ap.error(f"--dev all: {corpus} has no DEV re-run list")
    if (getattr(args, "force_tier2", False) or getattr(args, "force_typed", False)) \
            and not getattr(args, "dev", ""):
        ap.error("--force-tier2 / --force-typed are DEV only")
    if getattr(args, "force_typed", False):
        global FORCE_TYPED
        FORCE_TYPED = True
    if getattr(args, "dev", "") == "all" and getattr(args, "arm", "") not in ("S", "L"):
        ap.error("--dev all runs ONE arm (the arm each site ran)")
    if getattr(args, "arm", "") in ("SL", "LS") and not getattr(args, "dev", ""):
        ap.error("--arm SL / LS is DEV only")
    if getattr(args, "pilot", "") and args.pilot not in C.PILOT:
        ap.error(f"--pilot {args.pilot}: not in {corpus}.PILOT")
    if args.cmd == "plan":
        clash = C.check_fresh(args.prior)
        print("fresh:", "yes" if not clash else f"CLASH {clash}")
        for c in C.COHORTS:
            for arm in getattr(C, "ARMS", ("S", "L")):
                print(f"cohort {c} arm {arm}: {C.queue(c, arm)}")
        for st in C.STRATA:
            print(f"reserves {st}: {[e['site'] for e in C.reserves(st)]}")
        return 0 if not clash else 1
    if args.cmd == "run":
        return run(args)
    if args.cmd == "audit-pack":
        print(audit_pack(args.out))
        return 0
    print(report(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
