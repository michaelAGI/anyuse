"""The web ladder, re-proved under the held-out rule.

usage: ladder_verify.py plan   <values.json>
       ladder_verify.py run    <out-dir> --values values.json --browser http://IP:9222
       ladder_verify.py report <out-dir>

The workflows of execution/web_runtime/corpus_sample4.py keep their three values as
compile / fitting / selection inputs and get two NEW validation values from the
fixed rule in the measurement harness. The recorder and the ladder are the
same as for that corpus, with the roles made explicit:

* the dry look derives the ground-truth selector over ``rec`` + ``fit1`` only
  (``ground_truth_fitted_on`` says so on every recording); ``sel1`` is checked
  against it, ``val1``/``val2`` are only read with it;
* tier 1 (``execution/web_runtime/ladder.py`` ``run_workflow`` with ``roles``) compiles from
  ``rec``, fits on ``fit1``, chooses runner-ups on ``sel1``, validates once on
  ``val1`` + ``val2``;
* tier 2 builds its template from ``rec`` + ``fit1`` + ``sel1`` and is graded on
  the validation loads only.

Traffic: GET only (a non-GET browser request is failed); one pacer (>= 1 s
between two requests to one host) shared by robots, the recorder browser, the
compiled request and the tier-2 replay; robots.txt respected for three tokens;
every browser request through ``execution.cdp_guard`` (<= 30 per page load,
<= 200 per host, third parties and non-document/script/XHR types failed); a
block page, a 403 or a 429 stops the host for good; a cap or bypass raises
``TrafficViolation`` and stops the run (exit 3). User-Agent:
``research-probe/0.1 (read-only; 1 req/s)``. No model calls.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import anyuse  # noqa: E402
if os.path.realpath(os.path.dirname(anyuse.__file__)) != os.path.realpath(os.path.dirname(os.path.dirname(HERE))):
    raise SystemExit(f"refused: `anyuse` resolves to {anyuse.__file__}, not this checkout; "
                     f"put a pkgroot symlink to {os.path.dirname(os.path.dirname(HERE))} first on PYTHONPATH")

from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution.cdp_guard import (GuardedSession, LoadGuard,  # noqa: E402
                                        TrafficViolation)

UA = "research-probe/0.1 (read-only; 1 req/s)"
TOKENS = ("research-probe", "anyuse-learn", "anyuse-compiled-workflow")
MIN_GROUND_TRUTH = 3

#: ``corpus_sample4``'s D_allowed = 33, with the
#: tier each reached there (primary arm) — the comparison column
OLD_TIER = {
    "brew_detail": 1, "freshports_detail": 1, "mankier_detail": 1, "msys2_detail": 1,
    "ourairports_detail": 1, "php_detail": 1, "ubuntu_detail": 1, "gentoo_detail": 2,
    "bioconductor_detail": 3, "pursuit_detail": 3, "nvd_detail": 4, "lastfm_detail": 0,
    "eric_search": 1, "msys2_search": 1, "pubmed_search": 1, "savannah_search": 1,
    "upennbooks_search": 1, "gentoo_search": 2, "humdata_search": 2,
    "slackbuilds_search": 2, "dlang_search": 3, "freebsdman_search": 3,
    "gnome_search": 3, "racket_search": 3, "ubuntu_search": 3, "dailymed_search": 4,
    "legislation_search": 4, "hansard_search": 4, "isfdb_search": 4, "kde_search": 4,
    "nimble_search": 4, "philpapers_search": 4, "spi_search": 4,
}
#: hosts that served a block or challenge page to the ``corpus_sample4`` run and are
#: not contacted again, whether or not NEVER_CONTACT.txt lists them yet
SAMPLE4_REFUSED = {
    "swiftpackageindex.com": "Cloudflare Turnstile (sample 4)",
    "philpapers.org": "Cloudflare Turnstile (sample 4)",
    "www.isfdb.org": "Cloudflare Turnstile (sample 4)",
    "hansard.parliament.uk": "Cloudflare Turnstile (sample 4)",
    "nvd.nist.gov": "Cloudflare Turnstile (sample 4)",
    "invent.kde.org": "403 on the search path (sample 4)",
    "www.last.fm": "Fastly Client Challenge (sample 4)",
    "pubmed.ncbi.nlm.nih.gov": "reCAPTCHA on the second pass (sample 4)",
}
ROLES = {"fitting": ["fit1"], "selection": ["sel1"], "validation": ["val1", "val2"]}


def never_contact() -> set:
    from never_contact import never_contact as nc
    return nc()


def _government(host: str) -> str:
    """A government host is never contacted."""
    from anyuse.execution.gov_hosts import government_host
    return government_host(host)


def scope(values: Dict[str, dict]):
    """(in-scope workflow ids, {excluded id: reason}), in OLD_TIER order."""
    import corpus_sample4 as C
    nc = never_contact()
    run, out = [], {}
    for wid in OLD_TIER:
        site = C.BY_ID[wid]["site"]
        if site in nc:
            out[wid] = f"{site} is in the measurement harness"
        elif _government(site):
            out[wid] = "skipped: government host (owner rule)"
        elif site in SAMPLE4_REFUSED:
            out[wid] = f"{site}: {SAMPLE4_REFUSED[site]}"
        else:
            run.append(wid)
    return run, out


# ---------------------------------------------------------------------------
# GET only, on top of the shared guard
# ---------------------------------------------------------------------------

#: ``async (page, kind, decision)`` screenshot hook for tier-2 replay pages
#: (set by execution/web_runtime/live_bench.py when the corpus asks for screenshots); None = off
SHOT = None


class GetOnlyGuard(LoadGuard):
    """``LoadGuard`` that also fails every request whose method is not GET/HEAD.

    With a web scope on the budget (``execution/web_scope.py``) a non-GET
    request is left to the scope's rules instead (a page's own POST search, a
    provider's documented read and its preflight); everything else is still
    refused there."""

    methods: Dict[str, int] = {}

    async def handle(self, params: dict, send) -> bool:
        req = params.get("request") or {}
        method = str(req.get("method") or "GET").upper()
        rid = params.get("requestId")
        staged = params.get("responseStatusCode") is not None or params.get("responseErrorReason")
        if rid and not staged and method not in ("GET", "HEAD") and \
                getattr(self.budget, "web_scope", None) is None:
            self.failed.append((str(req.get("url") or "")[:160], f"method {method} is not sent"))
            await send("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"})
            GetOnlyGuard.methods[method + " (failed)"] = GetOnlyGuard.methods.get(
                method + " (failed)", 0) + 1
            return False
        sent = await super().handle(params, send)
        if sent:
            GetOnlyGuard.methods[method] = GetOnlyGuard.methods.get(method, 0) + 1
        return sent


class GetOnlySession(GuardedSession):
    def __init__(self, endpoint, user_agent="", allow_url=None, budget=None, **kw):
        super().__init__(endpoint, user_agent=user_agent, allow_url=allow_url,
                         budget=budget, **kw)
        self.guard = GetOnlyGuard(budget, allow_url=allow_url)
        self.guard.read_body = self._read_body


# ---------------------------------------------------------------------------
# tier 2 through the guard
# ---------------------------------------------------------------------------

class GuardedReplayDriver:
    """``ReplayExecutor``'s driver, with every request inside ``GetOnlySession``.

    Same settle as ``cdp_replay.CdpDriver`` (readiness with the timer's ceiling,
    the timer as the fall-back on an incomplete read); the difference is only
    that the page's requests pass the guard and the shared pacer."""

    def __init__(self, bfetch, budget):
        self.bfetch = bfetch
        self.budget = budget
        self.opened = 0

    def open(self, url: str, settle_s: float, timeout_s: float,
             readiness: Optional[Dict[str, Any]] = None, url_policy=None):
        from anyuse.execution.cdp_replay import PageResult
        why = self.bfetch.refusal(url) or ("account/login/cart path" if D._ACCOUNT.search(
            urllib.parse.urlsplit(url).path) else "")
        if why:
            return PageResult(url=url, error=f"refused before sending: {why}")
        out = asyncio.run(self._open(url, settle_s, timeout_s, readiness, url_policy))
        self.opened += 1
        self.budget.check()                           # TrafficViolation stops the run
        if out.dom:
            f = D.Fetched(url=url, final_url=out.url or url, status=out.status or 200,
                          headers={"content-type": "text/html"}, body=out.dom)
            self.bfetch._note_block(f)
        return out

    async def _open(self, url, settle_s, timeout_s, readiness, url_policy):
        from anyuse.execution.cdp_replay import CdpDriver, PageResult, ready_read_passes
        out = PageResult()
        t0 = time.monotonic()

        def allow(u):
            if not self.bfetch._allowed(u):
                return False
            return not (url_policy is not None and url_policy(u))

        try:
            async with GetOnlySession(self.bfetch.endpoint, user_agent=UA, allow_url=allow,
                                      budget=self.budget) as page:
                page.guard.start(url)
                await page.call("Page.navigate", {"url": url}, timeout=60.0)
                ceiling = min(settle_s + 6.0, timeout_s)
                t_settle = time.monotonic()
                if readiness:
                    helper = CdpDriver("http://unused.invalid:9222")   # no socket is opened
                    out.settle = await helper._await_ready(page.js, page.settle, page._events,
                                                           readiness, ceiling)
                else:
                    await page.settle(ceiling)
                    out.settle = {"how": "timer", "waited_ms": round(ceiling * 1000)}
                out.dom = await page.dom()
                if (readiness and out.settle.get("how") == "ready"
                        and readiness.get("fallback") == "timer"):
                    ok, why = ready_read_passes(out.dom, readiness)
                    if not ok:
                        remaining = max(0.0, ceiling - (time.monotonic() - t_settle))
                        await page.settle(remaining)
                        out.dom = await page.dom()
                        out.settle = {**out.settle, "how": "ready->timer_fallback",
                                      "first_read": why}
                out.text = await page.text()
                out.url = await page.url() or url
                if SHOT is not None:
                    # execution/web_runtime/shots.py: the tier-2 replay page; the shot
                    # helper itself never captures a challenge/block page
                    try:
                        await SHOT(page, "replay", {"page": "tier-2 navigate replay",
                                                    "url": url})
                    except Exception:            # noqa: BLE001 - never breaks a replay
                        pass
                for e in page._events:
                    if e.get("method") == "Network.responseReceived":
                        resp = (e.get("params") or {}).get("response") or {}
                        ru = resp.get("url") or ""
                        out.requests.append({"url": ru, "status": resp.get("status")})
                        if ru.split("#")[0] in (url, out.url) and out.status is None:
                            out.status = resp.get("status")
                if page.refused and not out.dom:
                    out.error = f"robots refused the navigation: {page.refused[0][:100]}"
        except TrafficViolation:
            raise
        except Exception as exc:
            out.error = f"browser error: {type(exc).__name__}: {exc}"[:200]
        out.elapsed_ms = (time.monotonic() - t0) * 1000.0
        return out


# ---------------------------------------------------------------------------
# the recorder
# ---------------------------------------------------------------------------

def _blocked_ui(rec: dict) -> str:
    from anyuse.execution.blocked import detect_blocked_ui
    state = detect_blocked_ui((rec.get("ui") or {}).get("text") or "", "",
                              str(rec.get("final_url") or ""), rec.get("network") or [],
                              items=(rec.get("ui") or {}).get("items") or [])
    return f"{state.reason} ({'; '.join(state.evidence)[:70]})" if state.blocked else ""


def load(bfetch, wf: dict, tag: str, value: str) -> dict:
    """One guarded page load -> a recording in the ``corpus_sample4`` shape."""
    url = wf["url_tmpl"].format(v=urllib.parse.quote(value, safe="/,"))
    host = D.host_of(url)
    rec = {"workflow": wf["id"], "site": wf["site"], "category": wf["category"],
           "cls": wf["cls"], "tag": tag, "path_style": wf.get("path_style", False),
           "task": wf.get("task", ""), "param": {"name": wf["param"], "value": value},
           "requested_url": url, "final_url": url, "ui": {"text": "", "items": []},
           "dom": "", "network": [], "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if not bfetch._allowed(url):
        rec.update(transport_error=True, robots_disallow=True,
                   recorder_warning=f"robots.txt disallows {url[:100]}")
        return rec
    t0 = time.monotonic()
    run, extra = bfetch.open(url, value)
    rec["ui_ms"] = (time.monotonic() - t0) * 1000.0
    rec["guard_failed"] = [list(f) for f in (extra.get("failed") or [])][:40]
    rec["guard_sent"] = extra.get("sent", 0)
    robots_hop = [f for f in (extra.get("failed") or []) if str(f[1]).startswith("robots_refused")]
    rec.update(final_url=run.final_url or url, dom=run.dom or "", network=run.network or [])
    rec["ui"]["text"] = run.text or ""
    if host in bfetch.blocked:
        rec["blocked_signal"] = bfetch.blocked[host]
        return rec
    if run.error:
        rec.update(transport_error=True, recorder_warning=run.error[:200])
    if robots_hop and (not run.dom or "chrome-error" in str(run.final_url)):
        rec.update(transport_error=True, robots_redirect=True,
                   recorder_warning=f"a redirect hop robots.txt disallows: {robots_hop[0][0][:100]}")
        return rec
    if not rec.get("transport_error"):
        sig = _blocked_ui(rec)
        if sig:
            rec["blocked_signal"] = sig
            bfetch.blocked[host] = sig
    return rec


def record_workflow(bfetch, wf: dict, vals: dict, outdir: str) -> dict:
    """Load rec, fit1 -> dry look -> sel1 (checked) -> val1, val2 (read with it)."""
    import dry_look
    plan = [("rec", vals["rec"]), ("fit1", vals["fit1"]), ("sel1", vals["sel1"])]
    plan += [(f"val{i + 1}", v) for i, v in enumerate(vals["validation"])]
    recs: Dict[str, dict] = {}
    info: Dict[str, Any] = {"id": wf["id"], "status": "", "notes": []}

    def one(tag, value, retries=1):
        for attempt in range(retries + 1):
            rec = load(bfetch, wf, tag, value)
            if rec.get("blocked_signal") or rec.get("robots_disallow") or rec.get("robots_redirect"):
                return rec
            if not rec.get("transport_error"):
                return rec
            if attempt < retries:
                info["notes"].append(f"{tag}: re-loaded once after {rec.get('recorder_warning', '')[:80]}")
        return rec

    def stop(status, why):
        info["status"], info["why"] = status, why
        return info

    for tag, value in plan[:2]:
        rec = one(tag, value)
        recs[tag] = rec
        if rec.get("blocked_signal"):
            _save(outdir, recs)
            return stop("blocked", f"{tag}: {rec['blocked_signal']}")
        if rec.get("robots_disallow") or rec.get("robots_redirect"):
            _save(outdir, recs)
            return stop("robots_disallow", rec.get("recorder_warning", ""))
        if rec.get("transport_error"):
            _save(outdir, recs)
            return stop("transport_error", f"{tag}: {rec.get('recorder_warning', '')}")
    selector, why = dry_look.admit(recs["rec"], recs["fit1"])
    info["selector"] = selector
    if not selector:
        _save(outdir, recs)
        return stop("dry_look_rejects", why)

    for tag, value in plan[2:]:
        rec = None
        for attempt in range(2):
            rec = one(tag, value, retries=0)
            if rec.get("blocked_signal") or rec.get("robots_disallow") or rec.get("robots_redirect"):
                break
            ok, why3, _items = dry_look.check_third(selector, recs["rec"], rec)
            if ok:
                break
            rec["recorder_warning"] = why3[:200]
            if attempt == 0:
                info["notes"].append(f"{tag}: re-loaded once ({why3[:80]})")
        recs[tag] = rec
        if rec.get("blocked_signal"):
            _apply(recs, selector, vals)
            _save(outdir, recs)
            return stop("blocked", f"{tag}: {rec['blocked_signal']}")
        if rec.get("robots_disallow") or rec.get("robots_redirect"):
            info["notes"].append(f"{tag}: {rec.get('recorder_warning', '')[:100]}")
            continue
        ok, why3, _items = dry_look.check_third(selector, recs["rec"], rec)
        if not ok:
            rec["no_ground_truth"] = why3[:200]
            info["notes"].append(f"{tag}: no ground truth under the admitted selector ({why3[:80]})")
    _apply(recs, selector, vals)
    _save(outdir, recs)
    return stop("recorded", "")


def _apply(recs: Dict[str, dict], selector: str, vals: dict) -> None:
    import dry_look
    usable = [r for r in recs.values() if r.get("dom")]
    dry_look.apply(usable, selector)
    for r in recs.values():
        r["ground_truth_selector"] = selector
        r["ground_truth_fitted_on"] = [vals["rec"], vals["fit1"]]
        r["dry_look"] = {"admitted_on": ["rec", "fit1"], "selector": selector,
                         "rule": "one derivation over rec + fit1; sel1 checked, "
                                 "val1/val2 only read with it (never re-derived)"}


def _save(outdir: str, recs: Dict[str, dict]) -> None:
    os.makedirs(os.path.join(outdir, "rec"), exist_ok=True)
    for tag, r in recs.items():
        with open(os.path.join(outdir, "rec", f"{r['workflow']}.{tag}.json"), "w") as fh:
            json.dump(r, fh)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def run(args) -> int:
    import bench_ack
    import corpus_sample4 as C
    import discover_bench as DBN
    import ladder as L
    from anyuse.execution.api_compile import Levers
    from anyuse.execution.api_permission import check_permission
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import permission_preflight
    from anyuse.execution.cdp_guard import RequestBudget
    from anyuse.execution.cdp_replay import ReplayExecutor
    import discover_browser as DBR

    if not bench_ack.enabled():
        print("refused: set ANYUSE_BENCH_ACK_PUBLIC_HOSTS=1 with a scratch ANYUSE_DATA_DIR")
        return 2
    bench_ack.check_scratch_registry()
    with open(args.values) as fh:
        values = json.load(fh)
    ids, excluded = scope(values)
    os.makedirs(args.out, exist_ok=True)
    state: Dict[str, Any] = {
        "spec": "SPEC_LADDER_VERIFY.md", "user_agent": UA, "tokens": TOKENS,
        "caps": {"per_load": 30, "per_host": 200, "min_interval_s": 1.0},
        "excluded": excluded, "workflows": {}, "rows": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    prior: Dict[str, Any] = {}
    if os.path.exists(os.path.join(args.out, "state.json")):
        # resume after a crash: recorded workflows and finished rows are NOT
        # fetched again; only robots.txt is read again for the hosts still to go
        with open(os.path.join(args.out, "state.json")) as fh:
            prior = json.load(fh)
        state["workflows"] = {k: v for k, v in (prior.get("workflows") or {}).items()
                              if v.get("status")}
        state["rows"] = list(prior.get("rows") or [])
        state["resumed_from"] = prior.get("generated_at")
        state["prior_requests"] = prior.get("requests")
    done_rows = {r["id"] for r in state["rows"]}
    robots_holder: Dict[str, Any] = {}
    fetch = D.HttpFetcher(user_agent=UA, allow_url=lambda u: robots_holder["r"].allow(u),
                          max_requests_per_host=200)
    fetch.blocked.update(prior.get("blocked") or {})     # a stopped host stays stopped
    robots = DBN.Robots(fetch)
    robots_holder["r"] = robots
    budget = RequestBudget(fetch.pacer)
    budget.bind_fetcher(fetch)   # one site ceiling for every channel (default = the host cap)
    sends: List[tuple] = []
    _reserve = fetch.pacer.reserve

    def _recorded(host):
        gap = _reserve(host)
        sends.append((host, fetch.pacer.last[host]))
        return gap
    fetch.pacer.reserve = _recorded
    bfetch = DBR.BrowserFetcher(args.browser, fetch, budget, session_factory=functools.partial(
        GetOnlySession, user_agent=UA, budget=budget))
    newly_blocked: Dict[str, str] = dict(prior.get("newly_blocked") or {})

    def write():
        state["requests"] = dict(fetch.pacer.count)
        state["max_load"] = {h: budget.max_load(h) for h in budget.loads}
        last, gaps = {}, {}
        for h, t in sends:
            if h in last:
                gaps[h] = min(gaps.get(h, 1e9), round(t - last[h], 3))
            last[h] = t
        state["min_gap_s"] = gaps
        state["violations"] = list(budget.violations)
        state["third_party_leaks"] = list(budget.leaks[:50])
        state["blocked"] = dict(fetch.blocked)
        state["stopped"] = dict(budget.stopped)
        state["browser_methods"] = dict(GetOnlyGuard.methods)
        state["http_log_methods"] = "GET only (HttpFetcher and PacedTransport refuse others)"
        state["newly_blocked"] = dict(newly_blocked)
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(os.path.join(args.out, "state.json"), "w") as fh:
            json.dump(state, fh, indent=2, default=str)

    try:
        # ---- robots (one GET per host) and the bench acknowledgement ------
        hosts = []
        for wid in ids:
            host = C.BY_ID[wid]["site"]
            if host not in hosts:
                hosts.append(host)
        contact = [h for h in hosts
                   if any(len(values[w]["validation"]) > 0 and w not in done_rows
                          for w in ids if C.BY_ID[w]["site"] == h)]
        for h in contact:
            robots.load(h)
        state["robots"] = {h: robots.notes.get(h) for h in contact}
        state["bench_ack"] = bench_ack.acknowledge_public_hosts(contact, robots.root_ok)
        write()

        # ---- record ----------------------------------------------------
        for wid in ids:
            wf, vals = C.BY_ID[wid], values[wid]
            if wid in state["workflows"]:
                continue                                  # recorded before a resume
            if not vals["validation"]:
                state["workflows"][wid] = {"id": wid, "status": "no_validation_values",
                                           "why": "the value rule yields 0 new values; "
                                                  "the host is not contacted"}
                print(f"{wid:24s} no validation values under the rule: not contacted", flush=True)
                continue
            if wf["site"] in fetch.blocked:
                state["workflows"][wid] = {"id": wid, "status": "blocked",
                                           "why": f"host stopped earlier: {fetch.blocked[wf['site']]}"}
                continue
            info = record_workflow(bfetch, wf, vals, args.out)
            if info["status"] == "blocked":
                newly_blocked[wf["site"]] = info.get("why", "")
            state["workflows"][wid] = info
            print(f"{wid:24s} {info['status']:18s} {info.get('why', '')[:70]} "
                  f"{'; '.join(info.get('notes') or [])[:80]}", flush=True)
            write()

        # ---- the ladder ------------------------------------------------
        L.READINESS_ON = True
        L.BROWSER_ONLY_ON = False
        levers = Levers.from_env({"ANYUSE_COMPILE_LEVERS": "on"})
        replay = ReplayExecutor(driver=GuardedReplayDriver(bfetch, budget))
        checker = functools.partial(check_permission, fetcher=robots.text_for)
        executor = ApiStepExecutor(transport=D.PacedTransport(fetch))
        for wid in ids:
            wf, info = C.BY_ID[wid], state["workflows"].get(wid, {})
            if info.get("status") != "recorded" or wid in done_rows:
                continue
            if wf["site"] in fetch.blocked:
                info["status"], info["why"] = "blocked", f"host stopped: {fetch.blocked[wf['site']]}"
                continue
            recs = {}
            for tag in ("rec", "fit1", "sel1", "val1", "val2"):
                p = os.path.join(args.out, "rec", f"{wid}.{tag}.json")
                if os.path.exists(p):
                    with open(p) as fh:
                        recs[tag] = json.load(fh)
            base = recs["rec"]
            gate = lambda compiled, _b=base: permission_preflight(compiled, _b, checker=checker)  # noqa: E731
            roles = dict(ROLES)
            if "sel1" not in recs or recs["sel1"].get("no_ground_truth"):
                roles["selection"] = []        # only a sel1 that passed
            row = L.run_workflow(wf, recs, levers, True, replay, permission_fetch=False,
                                 roles=roles, executor=executor, preflight=gate)
            row["ui_ms"] = base.get("ui_ms")
            if wf["site"] in fetch.blocked and row["tier"] not in (L.PROVED_TIER1, L.TIER2):
                row["tier"] = L.TIER4
                row["reason"] = f"host answered with a block/403/429: {fetch.blocked[wf['site']]}"
                newly_blocked.setdefault(wf["site"], fetch.blocked[wf["site"]])
            row["validation_values"] = [(recs[t].get("param") or {}).get("value")
                                        for t in ROLES["validation"] if t in recs]
            row["validation_ground_truth"] = {
                t: len((recs[t].get("ui") or {}).get("items") or [])
                for t in ROLES["validation"] if t in recs}
            state["rows"].append(row)
            print(f"{wid:24s} {row['tier']:15s} status={row.get('proof_status')} "
                  f"{(row.get('reason') or row.get('correctness_note') or '')[:70]}", flush=True)
            write()
    except TrafficViolation as exc:
        state["traffic_violation"] = str(exc)[:400]
        write()
        print(f"TRAFFIC VIOLATION: {exc}")
        return 3
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write()
    return 0


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def classify(row: dict, state: dict) -> Dict[str, str]:
    """Per tier: 'proved' | 'checked' | 'failed' | 'not reached' for one row."""
    import ladder as L
    t1 = "failed"
    if row["tier"] == L.PROVED_TIER1 and row.get("correct"):
        t1 = "proved" if row.get("proof_status") == "proved" else "checked"
    t2 = "not reached" if t1 != "failed" else "failed"
    if row["tier"] == L.TIER2 and row.get("correct"):
        gt = row.get("validation_ground_truth") or {}
        held = len([t for t, n in gt.items() if n >= MIN_GROUND_TRUTH]) >= 2
        t2 = "proved" if held else "checked"
    return {"tier1": t1, "tier2": t2}


def report(outdir: str) -> str:
    import ladder as L
    with open(os.path.join(outdir, "state.json")) as fh:
        state = json.load(fh)
    rows = {r["id"]: r for r in state["rows"]}
    ids = [w for w in OLD_TIER if w not in state["excluded"]]
    n = len(ids)
    per: Dict[str, Dict[str, str]] = {}
    for wid in ids:
        if wid in rows:
            per[wid] = classify(rows[wid], state)
        else:
            # never reached the ladder: blocked, robots, dry look, no values
            per[wid] = {"tier1": "not run", "tier2": "not run"}
    lines = []

    def cell(k):
        p, lo, hi = L.wilson(k, n)
        return f"{k} / {n} | {100 * p:.1f} % [{100 * lo:.1f}, {100 * hi:.1f}]"

    lines.append(f"D = {n} (sample-4 D_allowed 33 minus {len(state['excluded'])} excluded hosts)\n")
    lines.append("| tier | status | n / D | share, Wilson 95 % |")
    lines.append("|---|---|---|---|")
    for tier in ("tier1", "tier2"):
        for st in ("proved", "checked", "failed", "not run"):
            k = sum(1 for w in ids if per[w][tier] == st)
            lines.append(f"| {tier} | {st} | {cell(k)} |")
    both = sum(1 for w in ids if "proved" in (per[w]["tier1"], per[w]["tier2"]))
    anyp = sum(1 for w in ids if {"proved", "checked"} & {per[w]["tier1"], per[w]["tier2"]})
    lines.append(f"| 1 + 2 | proved | {cell(both)} |")
    lines.append(f"| 1 + 2 | proved or checked | {cell(anyp)} |")
    old1 = sum(1 for w in ids if OLD_TIER[w] == 1)
    old12 = sum(1 for w in ids if OLD_TIER[w] in (1, 2))
    lines.append(f"\nOld (sample 4, same {n} workflows): tier 1 {old1}, tiers 1 + 2 {old12}; "
                 f"all 33: 12 and 16.\n")
    lines.append("| id | old tier | recorder | new tier | tier 1 | tier 2 | validation values "
                 "(ground-truth n) | note |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for wid in ids:
        info = state["workflows"].get(wid, {})
        r = rows.get(wid, {})
        gt = r.get("validation_ground_truth") or {}
        vv = ", ".join(f"{v} ({gt.get(t, '-')})" for v, t in
                       zip(r.get("validation_values") or [], ("val1", "val2")))
        note = (r.get("reason") or r.get("correctness_note") or info.get("why") or "")
        lines.append(f"| `{wid}` | {OLD_TIER[wid]} | {info.get('status', '-')} | "
                     f"{r.get('tier', '-')} | {per[wid]['tier1']} | {per[wid]['tier2']} | "
                     f"{vv or '-'} | {note[:90].replace('|', '/')} |")
    lines.append("\nTraffic: max requests per host "
                 f"{max((state.get('requests') or {0: 0}).values())}, max per page load "
                 f"{max((state.get('max_load') or {0: 0}).values())}, min gap "
                 f"{min((state.get('min_gap_s') or {0: 0}).values())} s, violations "
                 f"{len(state.get('violations') or [])}, browser methods "
                 f"{state.get('browser_methods')}, newly blocked {state.get('newly_blocked')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("values")
    r = sub.add_parser("run")
    r.add_argument("out")
    r.add_argument("--values", required=True)
    r.add_argument("--browser", required=True)
    q = sub.add_parser("report")
    q.add_argument("out")
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        with open(args.values) as fh:
            values = json.load(fh)
        ids, excluded = scope(values)
        for wid in ids:
            v = values[wid]
            print(f"{wid:24s} old tier {OLD_TIER[wid]}  rec={v['rec']} fit1={v['fit1']} "
                  f"sel1={v['sel1']} val={v['validation']}")
        for wid, why in excluded.items():
            print(f"{wid:24s} EXCLUDED: {why}")
        return 0
    if args.cmd == "run":
        return run(args)
    print(report(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
