"""Sample 5: the attach product path on 20 never-contacted sites.

usage: attach_bench.py plan
       attach_bench.py run    <out-dir> --browser http://127.0.0.1:PORT
       attach_bench.py report <out-dir>

The protocol is the design notes (committed before the first
request); the corpus and the query rule are ``execution/web_runtime/corpus_sample5.py``. Per
site: robots (one GET) -> the listing page in the guarded browser -> 4 query
values -> 4 sessions, each a scripted agent typing into the visible search
input while a separate ``anyuse attach`` process observes -> the function
``anyuse compile last`` runs (``attach.compile_sessions``) with the shared paced
GET-only transport -> tier 2 (guarded browser replay) when tier 1 did not pass.

Traffic: GET only; one pacer (>= 1 s between two requests to one host) for
robots, the agent's browser, the compiled requests and the replays; every
browser request through ``execution.cdp_guard`` (<= 30 per page load, <= 200
per host); a block page, a 403 or a 429 stops the host for good and adds it to
the measurement harness; a cap or bypass raises ``TrafficViolation`` and
stops the run (exit 3). User-Agent ``research-probe/0.1 (read-only; 1 req/s)``.
No model calls.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import signal
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

import corpus_sample5 as C  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution.cdp_guard import RequestBudget, TrafficViolation  # noqa: E402
from ladder_verify import (UA, GetOnlyGuard, GetOnlySession,  # noqa: E402
                            GuardedReplayDriver)

SPEC_ID = "SPEC_ATTACH_BENCH.md"
ATTACH_MAX_S = 180
NEVER_CONTACT = os.path.join(HERE, "NEVER_CONTACT.txt")


# ---------------------------------------------------------------------------
# block detection on what the agent's browser saw
# ---------------------------------------------------------------------------

def page_block(text: str, url: str, network: List[dict]) -> str:
    """Why this page is a block page (stop the host), or ``""``."""
    from anyuse.execution.blocked import HARD_REASONS, LOGIN_WALL, detect_blocked_ui
    host = D.host_of(url or "")
    for r in network or []:
        if (r.get("resource_type") == "Document" and D.host_of(r.get("url") or "") == host
                and int(r.get("status") or 0) in (403, 429)):
            return f"HTTP {r['status']} on the document {str(r.get('url'))[:80]}"
    state = detect_blocked_ui(text or "", "", url or "", network or [])
    if state.blocked and (state.reason in HARD_REASONS or state.reason == LOGIN_WALL):
        return f"{state.reason} ({'; '.join(state.evidence)[:80]})"
    return ""


def add_never_contact(host: str, why: str) -> None:
    with open(NEVER_CONTACT) as fh:
        lines = fh.read().splitlines()
    if host in {ln.strip() for ln in lines if not ln.startswith("#")}:
        return
    head = [ln for ln in lines if ln.startswith("#")]
    body = sorted({ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")} | {host})
    head.append(f"# 2026-11-03 (attach bench): {host}: {why[:90]}")
    with open(NEVER_CONTACT, "w") as fh:
        fh.write("\n".join(head + body) + "\n")


# ---------------------------------------------------------------------------
# the agent + the attach observer
# ---------------------------------------------------------------------------

def start_attach(endpoint: str, target: str, task: str, value: str, log) -> subprocess.Popen:
    """``anyuse attach`` as its own process, pinned to the agent's page."""
    p = subprocess.Popen([sys.executable, "-m", "anyuse", "attach", "--cdp", endpoint,
                          "--target", target, "--task", task, "--input", f"q={value}",
                          "--max-seconds", str(ATTACH_MAX_S)],
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


def stop_attach(p: subprocess.Popen, log) -> str:
    if p.poll() is None:
        p.send_signal(signal.SIGINT)
    try:
        out, _ = p.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    for line in (out or "").splitlines():
        log(f"  [attach] {line}")
    return out or ""


async def _listing(bfetch, url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"url": url}
    t0 = time.monotonic()
    async with bfetch._factory(bfetch.endpoint, allow_url=bfetch._allowed) as page:
        await page.goto(url)
        await bfetch._settler._settle(page, t0)
        out.update(dom=await page.dom(), text=await page.text(), final_url=await page.url(),
                   network=await page.network(want_bodies=False),
                   sent=sum(page.guard.count.values()), failed=list(page.guard.failed[:20]))
    return out


async def _session(bfetch, entry: dict, value: str, log) -> Dict[str, Any]:
    """One agent run with AnyUse attached to its page."""
    out: Dict[str, Any] = {"value": value, "start_url": entry["start_url"]}
    t0 = time.monotonic()
    async with bfetch._factory(bfetch.endpoint, allow_url=bfetch._allowed) as page:
        proc = start_attach(bfetch.endpoint, page._target, entry["id"], value, log)
        try:
            await page.goto(entry["start_url"])
            await bfetch._settler._settle(page, t0)
            text0, url0 = await page.text(), await page.url()
            blk = page_block(text0, url0, await page.network(want_bodies=False))
            if blk:
                out["blocked"] = f"start page: {blk}"
                return out
            probe = await page.type_search(value)
            out["probe"] = {k: probe.get(k) for k in ("input", "document", "note", "refused")}
            out["probe"]["xhr"] = list(probe.get("xhr") or [])[:10]
            out["final_url"] = await page.url()
            out["text_head"] = (await page.text() or "")[:400]
            blk = page_block(await page.text(), out["final_url"],
                             await page.network(want_bodies=False))
            if blk:
                out["blocked"] = f"result page: {blk}"
            out["sent"] = sum(page.guard.count.values())
            out["failed"] = list(page.guard.failed[:20])
        finally:
            out["attach_log"] = stop_attach(proc, log)[-600:]
            out["seconds"] = round(time.monotonic() - t0, 2)
    return out


# ---------------------------------------------------------------------------
# compile last + tier 2
# ---------------------------------------------------------------------------

def compile_last(task: str, executor, checker) -> Dict[str, Any]:
    """What ``anyuse compile last --task <task>`` runs, minus persisting."""
    from anyuse.execution import attach as A
    from anyuse.execution.api_verify import permission_preflight
    row: Dict[str, Any] = {}
    sessions = A.select_sessions(A.list_sessions(), task=task)
    row["n_sessions"] = len(sessions)
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
        row.update(tier1="failed", tier1_reason=f"compile refused: {exc}"[:300])
        return row
    row["status"] = report.get("status")
    row["split"] = dict(proof.split or {})
    row["scope"] = proof.scope
    row["selector"] = report.get("ground_truth_selector")
    row["values"] = report.get("values")
    row["param"] = report.get("param")
    row["repeat_ms"] = report.get("repeat_ms")
    row["template_url"] = getattr(compiled.template, "url", "") if compiled.template else ""
    row["rung"] = getattr(compiled.template, "extract_kind", "") if compiled.template else ""
    row["compile_reason"] = (compiled.reason or "")[:300]
    row["proof_reason"] = (proof.reason or "")[:300]
    row["cases"] = [{"value": c.value, "passed": c.passed, "coverage": round(c.coverage, 2),
                     "ui": c.ui_count, "api": c.api_count, "reason": (c.reason or "")[:120],
                     "ms": round(c.elapsed_ms or 0.0, 1)} for c in proof.cases]
    row["tier1_model_calls"] = 0
    if proof.passed:
        row["tier1"] = "proved" if proof.effective_status == "proved" else "checked"
    else:
        row["tier1"] = "failed"
        row["tier1_reason"] = (proof.reason or compiled.reason or "")[:300]
    row["_recs"] = recs
    # the sessions' roles as compile_sessions declared them: a chain it fitted on the selection
    # session reports that session as fitting in the proof split, and tier 2 still fits on it
    row["_n_selection"] = int(report.get("n_selection") or 0)
    return row


def tier2(row: Dict[str, Any], replay) -> None:
    """Tier 2 on the recordings ``compile_sessions`` returned (spec §5)."""
    import ladder as L
    from anyuse.execution.api_compile import page_chrome, visible_items
    from anyuse.execution.cdp_replay import for_validation, template_from_recording
    recs = row.pop("_recs", None) or []
    n_sel = row.pop("_n_selection", 0)
    if row.get("tier1") in ("proved", "checked"):
        row["tier2"] = "not reached"
        return
    if len(recs) < 4 or not n_sel:
        row["tier2"] = "failed"
        row["tier2_reason"] = ("no held-out split to replay against"
                               if recs else "no recordings: " + str(row.get("tier1_reason", "")))[:300]
        return
    fit = recs[:1 + n_sel]
    val = recs[1 + n_sel:]
    chrome = page_chrome(fit)
    template = template_from_recording(recs[0], row.get("param") or "q", recordings=fit,
                                       readiness=True)
    if not template.url:
        row["tier2"] = "failed"
        row["tier2_reason"] = f"refused: {template.notes[:200]}"
        return
    proof_t = for_validation(template)
    cases, lats = [], []
    for r in val:
        value = (r.get("param") or {}).get("value", "")
        out = replay.execute(proof_t, {row.get("param") or "q": value})
        if out.get("elapsed_ms"):
            lats.append(out["elapsed_ms"])
        if not out.get("success"):
            cases.append((False, f"{value!r}: {str(out.get('error'))[:120]}"))
            if out.get("requires_human"):
                row["tier2_blocked"] = str(out.get("error"))[:160]
            continue
        got = [str(x) for x in (out.get("extracted") or {}).get("items") or []]
        ok, note = L.correctness(got[:40], visible_items(r, exclude=chrome), min_values=3)
        cases.append((ok, f"{value!r}: {note}"))
    row["tier2_url"] = template.url
    row["tier2_ms"] = [round(x, 1) for x in lats]
    row["tier2_cases"] = [c[1] for c in cases]
    row["tier2_model_calls"] = 0
    if len(cases) >= 2 and all(c[0] for c in cases):
        row["tier2"] = "proved"
    else:
        row["tier2"] = "failed"
        row["tier2_reason"] = "; ".join(c[1] for c in cases if not c[0])[:300]


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def run(args) -> int:
    import bench_ack
    import discover_bench as DBN
    import discover_browser as DBR
    from anyuse.execution.api_permission import check_permission
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.cdp_replay import ReplayExecutor

    if not bench_ack.enabled():
        print("refused: set ANYUSE_BENCH_ACK_PUBLIC_HOSTS=1 with a scratch ANYUSE_DATA_DIR")
        return 2
    bench_ack.check_scratch_registry()
    os.makedirs(args.out, exist_ok=True)
    logf = open(os.path.join(args.out, "run.log"), "a")

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    state_path = os.path.join(args.out, "state.json")
    state: Dict[str, Any] = {"spec": SPEC_ID, "user_agent": UA,
                             "caps": {"per_load": 30, "per_host": 200, "min_interval_s": 1.0},
                             "entries": {}, "ledger": [], "D": [],
                             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            prior = json.load(fh)
        state.update({k: prior[k] for k in ("entries", "ledger", "D") if k in prior})
        state["resumed_from"] = prior.get("generated_at")
        state["prior_requests"] = prior.get("requests")
        state["started_at"] = prior.get("started_at", state["started_at"])

    fetch = D.HttpFetcher(user_agent=UA, allow_url=lambda u: robots.allow(u),
                          max_requests_per_host=200)
    robots = DBN.Robots(fetch)
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
    executor = ApiStepExecutor(transport=D.PacedTransport(fetch))
    checker = functools.partial(check_permission, fetcher=robots.text_for)
    replay = ReplayExecutor(driver=GuardedReplayDriver(bfetch, budget))

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
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(state_path, "w") as fh:
            json.dump(state, fh, indent=2, default=str)

    # the queue is rebuilt from the ledger on a resume: MAIN, then each reserve
    # entry a replacement pulled in, in ledger order
    queue = [w["id"] for w in C.MAIN] + [l["in"] for l in state["ledger"] if l.get("in")]
    used = set(queue)
    reserve = [w["id"] for w in C.RESERVE if w["id"] not in used]

    def replace(wid: str, bucket: str, why: str) -> None:
        state["entries"][wid] = {"id": wid, "status": "replaced", "bucket": bucket, "why": why[:300]}
        nxt = reserve.pop(0) if reserve else None
        state["ledger"].append({"out": wid, "bucket": bucket, "why": why[:200], "in": nxt})
        if nxt:
            queue.append(nxt)
        log(f"{wid:18s} REPLACED ({bucket}): {why[:100]}")

    def stop_host(entry: dict, why: str) -> None:
        host = entry["site"]
        fetch.blocked[host] = why
        budget.stopped[host] = why
        add_never_contact(host, why)
        log(f"{entry['id']:18s} BLOCKED, host stopped and added to NEVER_CONTACT: {why[:100]}")

    try:
        while queue and len(state["D"]) < C.TARGET:
            wid = queue.pop(0)
            prior_e = state["entries"].get(wid) or {}
            if prior_e.get("status") in ("done", "replaced"):
                continue                                   # finished before a resume
            if prior_e.get("status") == "started":
                # crashed mid-site: its partial sessions are discarded and the
                # site is run again (named in the results)
                from anyuse.execution import attach as A
                for sess in A.list_sessions():
                    if A.task_name([sess]) == wid:
                        A.forget(sess.get("id"))
                state.setdefault("restarted", []).append(wid)
                if wid in state["D"]:
                    state["D"].remove(wid)
            entry = C.BY_ID[wid]
            host = entry["site"]
            info: Dict[str, Any] = {"id": wid, "site": host, "category": entry["category"],
                                    "status": "started"}
            state["entries"][wid] = info
            # -- robots -------------------------------------------------------
            robots.load(host)
            info["robots"] = robots.notes.get(host)
            bad = [u for u in (entry["start_url"], entry["listing_url"]) if not robots.allow(u)]
            if bad:
                replace(wid, "robots", f"robots.txt ({info['robots']}) disallows {bad[0]}")
                write()
                continue
            info["bench_ack"] = bench_ack.acknowledge_public_hosts([host], robots.root_ok)
            # -- the listing and the query rule --------------------------------
            lst = asyncio.run(_listing(bfetch, entry["listing_url"]))
            budget.check()
            blk = page_block(lst.get("text", ""), lst.get("final_url", ""), lst.get("network"))
            info["listing"] = {"final_url": lst.get("final_url"), "sent": lst.get("sent"),
                               "failed": lst.get("failed")}
            with open(os.path.join(args.out, f"{wid}.listing.html"), "w") as fh:
                fh.write(lst.get("dom") or "")
            if blk:
                stop_host(entry, f"listing: {blk}")
                info.update(status="done", outcome="blocked", tier1="failed", tier2="failed",
                            tier1_reason=f"blocked: {blk}", tier2_reason=f"blocked: {blk}")
                state["D"].append(wid)
                write()
                continue
            values = C.query_values(lst.get("dom") or "")
            info["values"] = values
            log(f"{wid:18s} values {values}")
            if len(values) < C.N_QUERIES:
                replace(wid, "query_rule", f"the listing yields {len(values)} value(s): {values}")
                write()
                continue
            # -- 4 attached sessions -------------------------------------------
            sessions: List[dict] = []
            outcome = ""
            for i, value in enumerate(values):
                s = asyncio.run(_session(bfetch, entry, value, log))
                budget.check()
                sessions.append(s)
                probe = s.get("probe") or {}
                refused = [f for f in (probe.get("refused") or [])
                           if str(f[1]).startswith("robots_refused")]
                log(f"{wid:18s} session {i + 1} {value!r}: {s.get('final_url', '')[:90]} "
                    f"sent={s.get('sent')} {s.get('blocked') or probe.get('note') or ''}")
                if s.get("blocked"):
                    stop_host(entry, s["blocked"])
                    outcome = "blocked"
                    break
                if i == 0 and probe.get("note") == "no visible search input":
                    outcome = "no_search_input"
                    break
                if refused:
                    outcome = "robots_search_path"
                    break
            info["sessions"] = sessions
            if outcome in ("no_search_input", "robots_search_path"):
                replace(wid, outcome, str((sessions[-1].get("probe") or {}).get("refused")
                                          or "the start page shows no visible search input"))
                write()
                continue
            state["D"].append(wid)
            if outcome == "blocked":
                info.update(status="done", outcome="blocked", tier1="failed", tier2="failed",
                            tier1_reason=f"blocked: {sessions[-1]['blocked']}",
                            tier2_reason="blocked")
                write()
                continue
            # -- compile last (tier 1), then tier 2 -----------------------------
            row = compile_last(wid, executor, checker)
            budget.check()
            if host in fetch.blocked and row.get("tier1") != "proved":
                stop_host(entry, f"compiled request: {fetch.blocked[host]}")
                row.update(tier2="failed", tier2_reason="blocked")
                row.pop("_recs", None)
            else:
                tier2(row, replay)
                budget.check()
                if row.get("tier2_blocked"):
                    stop_host(entry, f"tier 2: {row['tier2_blocked']}")
            info.update(row)
            info["status"] = "done"
            log(f"{wid:18s} tier1={info.get('tier1')} tier2={info.get('tier2')} "
                f"{(info.get('tier1_reason') or info.get('scope') or '')[:90]}")
            write()
    except TrafficViolation as exc:
        state["traffic_violation"] = str(exc)[:400]
        write()
        log(f"TRAFFIC VIOLATION: {exc}")
        return 3
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write()
    return 0


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def _n_sessions(e: dict) -> int:
    """Sessions compiled (the first two sites' state stored this as an int)."""
    if "n_sessions" in e:
        return int(e["n_sessions"])
    v = e.get("sessions")
    return v if isinstance(v, int) else len(v or [])


def report(outdir: str) -> str:
    import ladder as L
    with open(os.path.join(outdir, "state.json")) as fh:
        state = json.load(fh)
    ids = list(state["D"])
    n = len(ids)
    E = state["entries"]

    def cell(k):
        p, lo, hi = L.wilson(k, n)
        return f"{k} / {n} | {100 * p:.1f} % [{100 * lo:.1f}, {100 * hi:.1f}]"

    lines = [f"D = {n} (target {C.TARGET}); replaced {len(state['ledger'])}\n",
             "| tier | status | n / D | share, Wilson 95 % |", "|---|---|---|---|"]
    for tier in ("tier1", "tier2"):
        for st in ("proved", "checked", "failed", "not reached"):
            k = sum(1 for w in ids if E[w].get(tier) == st)
            if st == "not reached" and tier == "tier1":
                continue
            lines.append(f"| {tier} | {st} | {cell(k)} |")
    both = sum(1 for w in ids if "proved" in (E[w].get("tier1"), E[w].get("tier2")))
    anyp = sum(1 for w in ids if {"proved", "checked"} & {E[w].get("tier1"), E[w].get("tier2")})
    lines.append(f"| 1 + 2 | proved | {cell(both)} |")
    lines.append(f"| 1 + 2 | proved or checked | {cell(anyp)} |")
    t1 = [c["ms"] for w in ids if E[w].get("tier1") in ("proved", "checked")
          for c in E[w].get("cases") or [] if c.get("ms")]
    t2 = [x for w in ids if E[w].get("tier2") == "proved" for x in E[w].get("tier2_ms") or []]
    lines.append(f"\nRepeat latency: tier 1 proof replays median "
                 f"{statistics.median(t1):.0f} ms over {len(t1)}" if t1 else
                 "\nRepeat latency: tier 1 none")
    lines.append(f"tier 2 replays median {statistics.median(t2):.0f} ms over {len(t2)}"
                 if t2 else "tier 2: none")
    lines.append("\n| id | site | values | sessions | tier 1 | tier 2 | reason / scope |")
    lines.append("|---|---|---|---|---|---|---|")
    for w in ids:
        e = E[w]
        why = e.get("tier1_reason") if e.get("tier1") == "failed" else e.get("scope")
        if e.get("tier2") == "failed" and e.get("tier2_reason"):
            why = f"{why} // t2: {e['tier2_reason']}"
        lines.append(f"| `{w}` | {e.get('site')} | {', '.join(e.get('values') or [])} | "
                     f"{_n_sessions(e)} | {e.get('tier1')} | {e.get('tier2')} | "
                     f"{str(why or '')[:160].replace('|', '/')} |")
    lines.append("\nReplacements:")
    for l in state["ledger"]:
        lines.append(f"* `{l['out']}` ({l['bucket']}): {l['why'][:140]} -> `{l['in']}`")
    req = state.get("requests") or {}
    lines.append("\nTraffic: max requests per host "
                 f"{max(req.values()) if req else 0} ({max(req, key=req.get) if req else '-'}), "
                 f"max per page load {max((state.get('max_load') or {0: 0}).values())}, "
                 f"min gap {min((state.get('min_gap_s') or {0: 0}).values())} s, "
                 f"violations {len(state.get('violations') or [])}, "
                 f"third-party leaks {len(state.get('third_party_leaks') or [])}, "
                 f"browser methods {state.get('browser_methods')}, blocked {state.get('blocked')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    r = sub.add_parser("run")
    r.add_argument("out")
    r.add_argument("--browser", required=True)
    q = sub.add_parser("report")
    q.add_argument("out")
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        C.check_fresh()
        for w in C.MAIN:
            print(f"{w['id']:18s} {w['site']:36s} {w['start_url']}  | {w['listing_url']}")
        print(f"reserve: {[w['id'] for w in C.RESERVE]}")
        return 0
    if args.cmd == "run":
        return run(args)
    print(report(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
