"""Deterministic discovery bench: protocol versions v1/v2 and v3.

usage: discover_bench.py run <out.json> --hosts-file the measurement harness
                         [--browser http://IP:9222]   # guarded container browser
                         [--arm-b --ledger PATH]      # v3 ARM B (models)
                         [--cache DIR]                # DEV tuning only
       discover_bench.py report <out.json>

A hosts file is ``{"name": "H1", "n": 20, "hosts": [...], "reserves": [...]}``.

For each host: robots.txt (neutral UA) -> eligibility -> ``discover_host`` ->
ARM A: per capability, up to 3 candidates in rule order through ``discover.prove``
(the unchanged learn loop, tier 1) -> ARM B (optional, after ARM A): Jev ranking,
the per-screen router, L1/L2/L3 proposals, the router audit. The bench host
acknowledgement (execution/web_runtime/bench_ack.py) must be on with a scratch ANYUSE_DATA_DIR.

Traffic: GET only; one pacer (>= 1 s between requests to one host) shared by
discovery, the guarded browser (every sub-request), substituted navigations and
the compiled request; robots respected for three tokens; <= 30 requests per page
load and <= 200 per host; a block page stops the host for good; a traffic
violation stops the run (exit 3). User-Agent (this bench only):
``research-probe/0.1 (read-only; 1 req/s)``.
"""
from __future__ import annotations

import argparse
import functools
import json
import math
import os
import sys
import time
import urllib.parse
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Import `anyuse` ONLY through a pkgroot on PYTHONPATH (a symlink named `anyuse`
# to this checkout), never via the parent directory: a sibling checkout named
# `anyuse` would shadow this one.
import anyuse  # noqa: E402
if os.path.realpath(os.path.dirname(anyuse.__file__)) != os.path.realpath(os.path.dirname(os.path.dirname(HERE))):
    raise SystemExit(f"refused: `anyuse` resolves to {anyuse.__file__}, not this checkout; "
                     f"put a pkgroot symlink to {os.path.dirname(os.path.dirname(HERE))} first on PYTHONPATH")
from anyuse.execution import discover as D  # noqa: E402

UA = "research-probe/0.1 (read-only; 1 req/s)"
TOKENS = ("research-probe", "anyuse-learn", "anyuse-compiled-workflow")
SET_A = ["packages.gentoo.org", "packages.ubuntu.com", "packages.msys2.org",
         "slackbuilds.org", "pkgs.racket-lang.org", "code.dlang.org", "savannah.gnu.org",
         "gitlab.gnome.org"]
SET_B_ORDER = ["www.freebsd.org", "eric.ed.gov", "onlinebooks.library.upenn.edu",
               "data.humdata.org", "www.freshports.org", "formulae.brew.sh",
               "www.mankier.com", "ourairports.com", "bioconductor.org", "www.php.net",
               "pursuit.purescript.org"]
CANDIDATES_PER_CAP = 3
RULE_PROVED, RULE_USD = 2, 0.10


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(100 * (c - h), 1), round(100 * (c + h), 1))


class Robots:
    """robots.txt per host, fetched ONCE through the shared fetcher (paced, neutral UA)."""

    def __init__(self, fetch: D.HttpFetcher, *, refuse_competitors: bool = True):
        from anyuse.execution import api_permission as AP
        self.fetch = fetch
        self.refuse_competitors = bool(refuse_competitors)
        self.parsers, self.texts, self.notes = {}, {}, {}
        #: the robots.txt redirect hops being read (the MCP route's own robots gate refused the hop
        #: 0h9u3cb0u5-dsn.algolia.net/robots.txt -> https://algolia.net/1/404 as a page - reading the file is not a page)
        self._hops: set = set()
        #: ANYUSE_ROBOTS: read ONCE, when the context is built (who started the call);
        #: what an override let through is recorded per host
        self.override = AP.robots_override()
        self.overrides: Dict[str, Dict[str, Any]] = {}
        #: host -> was its robots.txt answered from the product's cache (nothing sent)
        self.cached: Dict[str, bool] = {}

    def load(self, host: str):
        """robots.txt of ``host`` through the product's ONE robots module (this is the MCP web-tool
        route's robots too, via ``watch_live``): ``api_permission.fetch_robots``
        (the RFC 9309 status rules, the redirects walked as section 2.3.1.2 says - 5 hops to any host, a refused host / a
        loop / a 6th hop a typed complete disallow) with each GET one request of THIS fetcher (paced, its refusals,
        counted in the asked host's site), and the rules matched by ``api_permission.RobotsRules`` (section 2.2)."""
        from anyuse.execution.web_scope import contact_refusal, registrable
        from web_arch_live import COMPETITORS
        why = contact_refusal(host)
        if not why and self.refuse_competitors and registrable(host) in COMPETITORS:
            why = "competitor domain"
        if why:
            self.parsers[host], self.texts[host], self.notes[host] = None, None, f"refused ({why}): disallow all"
            return None
        if host in self.parsers:
            return self.parsers[host]
        from anyuse.execution import api_permission as AP
        robots_url = f"https://{host}/robots.txt"

        def one(u: str):
            self._hops.add(u)
            try:
                r = self.fetch.get(u, follow=False, count_as=host)
            except TypeError:                      # a fetcher without single-hop GETs (the DEV / browser fetchers)
                r = self.fetch.get(u)
            if r.error and (not r.status or r.error.startswith("blocked")):
                raise ConnectionError(r.error)     # refused before sending, a network failure, a block page
            return r.status, r.headers or {}, r.body or ""
        AP.ROBOTS_REDIRECTS.pop(robots_url, None)
        # the product's ONE robots cache (its store and TTL) - a call within the TTL sends nothing
        kind, text, note, info = AP.robots_read(robots_url, fetcher=lambda u: AP._default_fetch(
            u, get=one, check_destination=False))              # the fetcher checks each request's destination itself
        landed = AP.ROBOTS_REDIRECTS.pop(robots_url, "") or str(info.get("landed") or "")
        self.cached[host] = bool(info.get("cached"))
        rp = AP.RobotsRules()
        cached = " (cached)" if info.get("cached") else ""
        if kind == "rules" and text is not None:
            rp.parse(text.splitlines())
            note = "parsed" + (f" (redirected to {landed})" if landed else "") + cached
        elif kind == "rules":
            text = ""
            note = "no robots.txt (404): allow" + (f" (redirected to {landed})" if landed else "") + cached
        elif kind == "none":
            text = ""
            note = f"{note}: no rules, allow"
        else:                                      # unreachable, a 429 that stayed, a refused first GET: disallow all
            rp, text = None, None
            note = f"{note}: disallow all"
        self.parsers[host], self.texts[host], self.notes[host] = rp, text, note
        return rp

    def allow(self, url: str) -> bool:
        from anyuse.execution.web_scope import contact_refusal, registrable
        from web_arch_live import COMPETITORS
        h = urllib.parse.urlsplit(url).hostname or ""
        if contact_refusal(h) or (self.refuse_competitors and registrable(h) in COMPETITORS):
            return False       # never contacted, in every mode (the benches also screen COMPETITORS)
        host = urllib.parse.urlsplit(url).netloc.lower()
        if urllib.parse.urlsplit(url).path.endswith("/robots.txt") or url in self._hops:
            return True        # robots.txt itself, and a hop of its redirects (RFC 9309 2.3.1.2)
        rp = self.parsers.get(host)
        ok = bool(rp) and all(rp.can_fetch(t, url) for t in TOKENS)
        if ok or not self.override:
            return ok
        rec = self.overrides.setdefault(host, {"requests": 0, "robots_would": "disallow" if rp else "unknown",
                                               "why": str(self.notes.get(host) or "not read")[:300],
                                               "overridden_by": self.override, "example": url[:200]})
        rec["requests"] += 1
        return True

    def root_ok(self, host: str) -> bool:
        rp = self.load(host)
        return bool(rp) and all(rp.can_fetch(t, f"https://{host}/") for t in TOKENS)

    def text_for(self, robots_url: str):
        """``check_permission``'s fetcher: answer from what we already fetched."""
        host = urllib.parse.urlsplit(robots_url).netloc.lower()
        t = self.texts.get(host)
        return t if t else None


class CachedFetcher(D.HttpFetcher):
    """DEV tuning only: a page fetched once is read from disk afterwards (robots
    and the block list still apply). Never used for a held-out run."""

    def __init__(self, cache_dir, **kw):
        super().__init__(**kw)
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.hits = 0

    def _path(self, url):
        import hashlib
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def get(self, url, max_hops=5):
        why = self.refusal(url)
        path = self._path(url)
        if not why and os.path.exists(path):
            with open(path) as fh:
                d = json.load(fh)
            self.hits += 1
            return D.Fetched(**d)
        out = super().get(url, max_hops)
        if not out.error or out.error.startswith("redirect off"):
            with open(path, "w") as fh:
                json.dump(out.__dict__, fh)
        return out


def never_contact() -> set:
    """Central list: hosts no bench may contact again."""
    from anyuse.execution.web_runtime.never_contact import never_contact as nc
    return nc()


def _government(host: str) -> str:
    """A government host is never contacted."""
    from anyuse.execution.gov_hosts import government_host
    return government_host(host)


def choose_hosts(which: str, robots: Robots, state: dict, hosts_file: str = ""):
    plan = []
    if hosts_file:
        with open(hosts_file) as fh:
            spec = json.load(fh)
        n, name = int(spec.get("n", 16)), spec.get("name", "X")
        k = 0
        for h in list(spec["hosts"]) + list(spec.get("reserves", [])):
            if k >= n:
                break
            if h in spec.get("never", []) or h in never_contact():
                continue
            if _government(h):
                state.setdefault("skipped", {})[h] = "skipped: government host (owner rule)"
                continue
            ok = robots.root_ok(h)
            plan.append((name, h, ok, robots.notes.get(h)))
            k += int(ok)
        state["eligibility"] = [{"set": s_, "host": h, "eligible": ok, "robots": note}
                                for s_, h, ok, note in plan]
        state["hosts_file"] = hosts_file
        return [(s_, h) for s_, h, ok, _n in plan if ok]
    if "A" in which:
        for h in SET_A:
            if h in never_contact():
                continue
            if _government(h):
                state.setdefault("skipped", {})[h] = "skipped: government host (owner rule)"
                continue
            plan.append(("A", h, robots.root_ok(h), robots.notes.get(h)))
    if "B" in which:
        n = 0
        for h in SET_B_ORDER:
            if n >= 8:
                break
            if h in never_contact():
                continue
            if _government(h):
                state.setdefault("skipped", {})[h] = "skipped: government host (owner rule)"
                continue
            ok = robots.root_ok(h)
            plan.append(("B", h, ok, robots.notes.get(h)))
            n += int(ok)
    state["eligibility"] = [{"set": s, "host": h, "eligible": ok, "robots": note}
                            for s, h, ok, note in plan]
    return [(s, h) for s, h, ok, _n in plan if ok]


#: the run's guarded-browser navigator (set by run(); none without --browser)
BROWSER_NAV: dict = {}
#: v4 switches (set by run()): {"on": bool, "bfetch": BrowserFetcher or None}
V4: dict = {}


def _prove_list(cands, host, origin, proof_fetch, nav, executor, gate, http, fetch,
                limit=CANDIDATES_PER_CAP, cache=None, label=""):
    """Prove candidates in order until one proves; reuse ``cache`` (template -> tried
    dict) for a candidate already proved or failed in this host's run.

    v4 (``V4`` set by run()): the guarded tier-2 replay is given to every proof,
    detail candidates get the detail proof first, and a search whose plain-HTTP
    proof failed is proved once more with the page recorded in the guarded
    browser (its tier-2 replay then decides)."""
    tried = []
    v4 = V4.get("on")
    bfetch = V4.get("bfetch")
    for c in cands[:limit]:
        if host in fetch.blocked or (v4 and bfetch is not None and host in bfetch.budget.stopped):
            break
        if cache is not None and c.template in cache:
            t = dict(cache[c.template])
            t["reused_from_arm_a"] = True
            tried.append(t)
        else:
            # a search the typing probe saw answered by the page's own XHR is
            # recorded in the guarded browser, so the recording carries that call
            cnav = nav
            if cnav is None and c.source in ("typed", "xhr"):
                cnav = BROWSER_NAV.get("nav")
            replay = bfetch.replay() if (v4 and bfetch is not None) else None
            bopen = bfetch.navigator if (v4 and bfetch is not None) else None
            import copy as _copy
            orig = _copy.deepcopy(c)
            D.prove(c, origin, proof_fetch, executor=executor, proof_preflight=gate,
                    transport=D.PacedTransport(http), navigator=cnav, replay=replay,
                    browser_open=bopen, detail_fields=bool(v4))
            if v4 and bfetch is not None and cnav is None and c.cap == "search" and \
                    c.where != "type" and c.status == "discovered" and \
                    not any((t.get("proof") or {}).get("browser_retry") for t in tried) and \
                    host not in fetch.blocked and host not in bfetch.budget.stopped:
                # tier 2: the same candidate, recorded in the guarded browser
                first = c.to_dict()
                b = _copy.deepcopy(orig)
                D.prove(b, origin, proof_fetch, executor=executor, proof_preflight=gate,
                        transport=D.PacedTransport(http), navigator=bfetch.navigator,
                        replay=bfetch.replay())
                b.proof = dict(b.proof or {})
                b.proof["http_attempt"] = {"status": first["status"],
                                           "reason": first["reason"][:200]}
                b.proof["browser_retry"] = True
                if b.status == "proved" or b.found or not c.found:
                    c = b
            t = c.to_dict()
            if cache is not None:
                cache[c.template] = t
            tried.append(t)
            print(f"  {label:2s} {host:30s} {c.cap:7s} {c.status:10s} {c.source:10s} "
                  f"{c.template[:64]} | {c.reason[:80]}", flush=True)
        if t["status"] == "proved":
            break
    return tried


def _best(tried, cap, host, fetch, n_cands):
    best = next((t for t in tried if t["status"] == "proved"), None) or (
        tried[0] if tried else None)
    if best is None:
        best = {"cap": cap, "status": "refused", "found": False, "confirmed": False,
                "reason": ("host blocked" if host in fetch.blocked else
                           "not found: no candidate from the readers"),
                "template": "", "values": [], "source": ""}
    return {"best": best, "tried": tried, "n_candidates": n_cands}


def run(args) -> int:
    import bench_ack
    from anyuse.execution.api_permission import check_permission
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import permission_preflight
    from anyuse.execution.cdp_guard import RequestBudget, TrafficViolation
    if not bench_ack.enabled():
        print("refused: set ANYUSE_BENCH_ACK_PUBLIC_HOSTS=1 with a scratch ANYUSE_DATA_DIR "
              "(the specification requires the bench acknowledgement)")
        return 2
    bench_ack.check_scratch_registry()
    if args.arm_b and not args.ledger:
        print("refused: ARM B needs --ledger (the whole-task spend ledger, outside the repo)")
        return 2
    v4 = args.version == "v4"
    if v4 and args.arm_b:
        print("refused: the v4 specification runs with the model arm OFF")
        return 2
    state = {"spec": "SPEC_DISCOVERY_V4.md" if v4 else "SPEC_DISCOVERY_V3.md",
             "version": args.version, "user_agent": UA,
             "tokens": TOKENS, "hosts": {}, "arm_b": bool(args.arm_b),
             "caps": {"per_load": 30, "per_host": 200},
             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    robots_holder = {}
    fcls = D.HttpFetcher
    if args.cache:
        fcls = functools.partial(CachedFetcher, args.cache)
    fetch = fcls(user_agent=UA, allow_url=lambda u: robots_holder["r"].allow(u),
                 max_requests_per_host=200, memo=v4)
    robots = Robots(fetch)
    http = fetch
    budget = RequestBudget(fetch.pacer)
    budget.bind_fetcher(fetch)   # one site ceiling for every channel (default = the host cap)
    # evidence: the scheduled send time of EVERY request (HTTP and browser), per host
    sends = []
    _reserve = fetch.pacer.reserve

    def _recorded(host):
        gap = _reserve(host)
        sends.append((host, fetch.pacer.last[host]))
        return gap
    fetch.pacer.reserve = _recorded
    bfetch = None
    if args.browser:
        import discover_browser as DB
        bfetch = DB.BrowserFetcher(args.browser, http, budget, cache=v4)
        BROWSER_NAV["nav"] = bfetch.navigator
        state["arm"] = "guarded container browser (chromedp/headless-shell, CDP Fetch)"
    robots_holder["r"] = robots
    V4.update(on=v4, bfetch=bfetch)
    ledger = None
    if args.arm_b:
        import discover_agent as B
        ledger = B.Ledger(args.ledger)
        from anyuse.execution.model_adapter import credential_status, resolve_model_config
        state["credentials"] = {r: credential_status(resolve_model_config(r))
                                for r in ("agent", "agent_l2", "agent_l3")}
        state["ledger_start"] = {"spent": dict(ledger.spent), "calls": dict(ledger.calls)}
    prior = {}
    if args.resume and os.path.exists(args.out):
        with open(args.out) as fh:
            prior = json.load(fh).get("hosts", {})
        state["resumed_hosts"] = sorted(prior)
        state["hosts"].update(prior)
    if prior and args.hosts_file:
        # resume: the eligibility of the interrupted run stands; ineligible hosts
        # are not contacted again, eligible ones only get their robots.txt again
        with open(args.out) as fh:
            old = json.load(fh)
        state["eligibility"] = old.get("eligibility") or []
        state["hosts_file"] = args.hosts_file
        state["crashed"] = old.get("crashed", {})
        hosts = [(e["set"], e["host"]) for e in state["eligibility"] if e["eligible"]]
        for _s, h in hosts:
            if h not in prior:
                robots.load(h)
    else:
        hosts = choose_hosts(args.set, robots, state, args.hosts_file)
    ack = bench_ack.acknowledge_public_hosts([h for _s, h in hosts if h not in prior],
                                             robots.root_ok)
    state["bench_ack"] = ack
    checker = functools.partial(check_permission, fetcher=robots.text_for)
    gate = lambda compiled, rec: permission_preflight(compiled, rec, checker=checker)  # noqa: E731
    executor = ApiStepExecutor(transport=D.PacedTransport(fetch))

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
        state["cache_hits"] = dict(budget.cache_hits)
        state["memo_hits"] = dict(getattr(fetch, "memo_hits", {}) or {})
        if ledger is not None:
            state["ledger_end"] = {"spent": dict(ledger.spent), "calls": dict(ledger.calls),
                                   "refused": dict(ledger.refused)}
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(args.out, "w") as fh:
            json.dump(state, fh, indent=2, default=str)

    try:
        for hset, host in hosts:
            if host in prior:
                continue
            try:
                _run_host(hset, host, state, fetch, http, bfetch, robots, executor, gate,
                          budget, ledger, args)
            except TrafficViolation:
                raise
            except Exception as exc:
                # a driver crash on a host scores that host 0 on every
                # capability, in both arms; it stays in every denominator
                state["hosts"][host] = crash_record(hset, host, f"{type(exc).__name__}: {exc}",
                                                    fetch.pacer.count.get(host, 0),
                                                    budget.max_load(host),
                                                    fetch.blocked.get(host, ""))
                print(f"{hset} {host:32s} CRASH {type(exc).__name__}: {str(exc)[:120]}",
                      flush=True)
            budget.check()
            write()
    except TrafficViolation as exc:
        state["traffic_violation"] = str(exc)[:500]
        state["log"] = fetch.log
        write()
        print(f"STOPPED: traffic violation: {exc}", flush=True)
        return 3
    state["log"] = fetch.log
    write()
    return 0


def crash_record(hset, host, why, requests, max_load, blocked=""):
    """A host whose run crashed: 0 on every capability in both arms, counted in
    both primary denominators (its signals are unknown: the conservative case)."""
    zero = {c: {"best": {"cap": c, "status": "refused", "found": False, "confirmed": False,
                         "reason": f"driver crash: {why[:200]}", "template": "",
                         "values": [], "source": ""}, "tried": [], "n_candidates": 0}
            for c in D.CAPS}
    return {"set": hset, "host": host, "crash": why[:400], "caps": zero,
            "arm_b": {"caps": json.loads(json.dumps(zero)), "usd": {}, "model_calls": 0},
            "signals": {"reachable": True, "has_search": True, "has_items": True,
                        "conservative_after_crash": True},
            "requests": requests, "max_load": max_load, "blocked": blocked, "js": None,
            "discovery": {}, "seconds": 0}


def _run_host(hset, host, state, fetch, http, bfetch, robots, executor, gate, budget,
              ledger, args):
    t0 = time.time()
    origin = f"https://{host}"
    hd = D.discover_host(origin, bfetch or fetch, robots_txt=robots.texts.get(host) or "",
                         search_probe=bfetch.search_probe if bfetch else None,
                         typed_candidates=bool(V4.get("on")) and bfetch is not None)
    js = bool(bfetch) and host in bfetch.js_hosts
    proof_fetch = bfetch if js else http
    nav = bfetch.navigator if js else None
    hs = {"set": hset, "host": host, "discovery": hd.to_dict(), "caps": {}, "js": js,
          "signals": hd.signals}
    cache = {}
    # ---- ARM A: deterministic, rule order ----------------------------------
    # v4: detail first (its proof costs 4 requests), so a JS search cannot
    # spend the host's cap before it
    order = ("detail", "search", "next", "filter") if V4.get("on") else D.CAPS
    for cap in order:
        cands = hd.caps.get(cap) or []
        tried = _prove_list(cands, host, origin, proof_fetch, nav, executor, gate, http, fetch,
                            cache=cache, label="A")
        hs["caps"][cap] = _best(tried, cap, host, fetch, len(cands))
    hs["requests_arm_a"] = fetch.pacer.count.get(host, 0)
    # ---- ARM B: Jev ranking, then L1 / L2 / L3 proposals -------------------
    if ledger is not None:
        hs["arm_b"] = _arm_b(hd, host, origin, proof_fetch, nav, executor, gate, http, fetch,
                            bfetch, budget, ledger, cache)
    hs["requests"] = fetch.pacer.count.get(host, 0)
    hs["max_load"] = budget.max_load(host)
    hs["cache_hits"] = budget.cache_hits.get(host, 0)
    import collections as _co
    hs["sent_by_type"] = dict(_co.Counter(ty for (h, _t, _u, ty) in budget.sent if h == host))
    hs["sent_top_urls"] = _co.Counter(u.split("?")[0][:120] for (h, _t, u, _ty) in budget.sent
                                      if h == host).most_common(12)
    hs["memo_hits"] = (getattr(fetch, "memo_hits", {}) or {}).get(host, 0)
    if bfetch is not None:
        tp = getattr(bfetch, "third_party", {}).get(host) or {}
        hs["third_party_refused"] = dict(sorted(tp.items(), key=lambda kv: -kv[1])[:10])
    if V4.get("on"):
        from anyuse.execution.discover_replay import persist_tools, tool_records
        recs = tool_records(host, hs["caps"])
        hs["tools"] = recs
        if recs and os.environ.get("ANYUSE_DATA_DIR"):
            persist_tools(recs, os.environ["ANYUSE_DATA_DIR"])
    hs["seconds"] = round(time.time() - t0, 1)
    hs["blocked"] = fetch.blocked.get(host, "")
    hs["stopped"] = budget.stopped.get(host, "")
    state["hosts"][host] = hs
    pa = sum(1 for c in hs["caps"].values() if c["best"]["status"] == "proved")
    pb = sum(1 for c in (hs.get("arm_b") or {}).get("caps", {}).values()
             if c["best"]["status"] == "proved")
    print(f"{hset} {host:32s} A {pa}/4 B {pb}/4  {hs['requests']} req (max load "
          f"{hs['max_load']})  {hs['seconds']:.0f}s {hs['blocked']} {hs['stopped']}", flush=True)


def _arm_b(hd, host, origin, proof_fetch, nav, executor, gate, http, fetch, bfetch, budget,
           ledger, cache):
    """ARM B: Jev ranking, per-screen router -> L1, L2, L3, router audit."""
    import discover_agent as B
    out = {"caps": {}, "jev": {}, "router": [], "levels": [], "audit": [],
           "usd": {"cerebras": 0.0, "openrouter": 0.0}, "model_calls": 0}

    def gone():
        return host in fetch.blocked or host in budget.stopped

    # 1-2. readers' candidates, Jev ranking when >= 2 compete
    for cap in D.CAPS:
        cands = list(hd.caps.get(cap) or [])
        order, how = cands, "rule"
        if len(cands) >= 2 and not gone():
            v = B.jev_pick(cap, cands, ledger, seed=B.seed_of(host, cap))
            out["jev"][cap] = v
            if v.get("asked"):
                out["model_calls"] += 1
                out["usd"]["openrouter"] += B.JEV_CALL_USD
            if v.get("pick") is not None:
                pick = cands[:8][v["pick"]]
                order = [pick] + [c for c in cands if c is not pick]
                how = "jev"
        tried = _prove_list(order, host, origin, proof_fetch, nav, executor, gate, http, fetch,
                            cache=cache, label="B")
        r = _best(tried, cap, host, fetch, len(cands))
        r["origin"] = how if r["best"]["status"] == "proved" else ""
        out["caps"][cap] = r
    need = [c for c in D.CAPS if out["caps"][c]["best"]["status"] != "proved"]
    need_at_router = list(need)
    fallback = {c: [list(x.values) for x in (hd.caps.get(c) or [])[:3]] for c in D.CAPS}
    fallback["search"] = [list(x.values) for x in (hd.caps.get("search") or [])[:1]] + \
        [list(v) for x in (hd.caps.get("search") or [])[:1] for v in x.alt_values]
    xhr = [u for (h, _t, u, ty) in budget.sent if h == host and ty in ("XHR", "Fetch")][:15]

    def call(level, view, caps_needed, image="", record=None):
        res = B.propose(level, origin, caps_needed, dict(view, xhr=xhr), ledger,
                        allow_url=fetch.allow_url, image=image)
        pool = "cerebras" if level == "L1" else "openrouter"
        out["model_calls"] += res.get("calls", 0)
        out["usd"][pool] += res.get("usd", 0.0)
        rec = {k: v for k, v in res.items() if k != "caps"}
        rec["proposed"] = {c: [x.template for x in v] for c, v in res.get("caps", {}).items()}
        proved_caps = []
        for cap in list(caps_needed):
            props = res.get("caps", {}).get(cap) or []
            for c in props:
                B.fill_values(c, fallback.get(cap) or [])
            tried = _prove_list(props, host, origin, proof_fetch, nav, executor, gate, http,
                                fetch, limit=B.MAX_PER_CAP, cache=cache, label=level)
            if any(t["status"] == "proved" for t in tried):
                proved_caps.append(cap)
                if record is not None:
                    r = _best(tried, cap, host, fetch, len(props))
                    r["origin"] = level
                    r["tried_before"] = out["caps"][cap]["tried"]
                    out["caps"][cap] = r
            elif record is not None:
                out["caps"][cap].setdefault("model_tried", []).extend(tried)
        rec["proved_caps"] = proved_caps
        return rec

    # 3-4. the per-screen router, then L1 on each routed screen
    unrouted = []
    for sc in hd.screens:
        if not need or gone():
            break
        summ = sc["summary"]
        jv = B.jev_route(summ, ledger, seed=B.seed_of(host, sc["url"], "route"))
        if jv.get("asked"):
            out["model_calls"] += 1
            out["usd"]["openrouter"] += B.JEV_CALL_USD
        why = B.route_reasons(summ, jv)
        entry = {"url": sc["url"][:160], "why_read": sc["why"], "jev": jv, "routed": why,
                 "l1": None}
        if why:
            entry["l1"] = call("L1", sc["view"], list(need), record=True)
            need = [c for c in need if c not in entry["l1"]["proved_caps"]]
        else:
            unrouted.append(sc)
        out["router"].append(entry)
    # 5. L2 once per host on the host-level view
    if need and not gone() and hd.view:
        out["levels"].append(dict(call("L2", hd.view, list(need), record=True), level="L2"))
        need = [c for c in need if c not in out["levels"][-1]["proved_caps"]]
    # 6. L3 only when the home page's text view is near-empty
    home_chars = (hd.screens[0]["summary"].get("text_chars", 0) if hd.screens else 0)
    if need and not gone() and hd.view and home_chars < B.NEAR_EMPTY_CHARS and bfetch:
        image = bfetch.screenshot(origin + "/")
        out["levels"].append(dict(call("L3", hd.view, list(need), image=image, record=True),
                                  level="L3"))
    elif need:
        out["levels"].append({"level": "L3", "skipped": f"home text {home_chars} chars "
                              f">= {B.NEAR_EMPTY_CHARS} (or no browser)"})
    # 7. router audit: a seeded 1-in-3 sample of the unrouted screens (not counted)
    for sc in unrouted:
        if gone() or not need_at_router:
            break
        if B.audited(host, sc["url"]):
            rec = call("L1", sc["view"], list(need_at_router), record=None)
            out["audit"].append({"url": sc["url"][:160], "l1": rec})
    return out


def _distinct_proved(caps: dict) -> int:
    seen = set()
    for c in caps.values():
        b = c["best"]
        if b["status"] == "proved":
            seen.add(b["template"])
    return len(seen)


def summarise_v3(d: dict) -> dict:
    """Per set, per arm: the protocol's metrics."""
    elig = d.get("eligibility") or []
    out = {}
    sets = []
    for e in elig:
        if e["set"] not in sets:
            sets.append(e["set"])
    for h in d["hosts"].values():
        if h["set"] not in sets:
            sets.append(h["set"])
    for hset in sets:
        inel = [e["host"] for e in elig if e["set"] == hset and not e["eligible"]]
        rows = [h for h in d["hosts"].values() if h["set"] == hset]
        blocked = {h["host"]: h["blocked"] for h in rows if h.get("blocked")}
        unreach = [h["host"] for h in rows if not h.get("blocked")
                   and not (h.get("signals") or {}).get("reachable")]
        reach = [h for h in rows if not h.get("blocked")
                 and (h.get("signals") or {}).get("reachable")]
        S = [h for h in reach if h["signals"].get("has_search")]
        Dd = [h for h in reach if h["signals"].get("has_items")]
        res = {"n_listed": len(rows) + len(inel), "ineligible": inel, "blocked": blocked,
               "unreachable": unreach, "n_reachable": len(reach), "n_has_search": len(S),
               "n_has_items": len(Dd), "arms": {},
               "traffic": {h["host"]: [h.get("requests") or 0, h.get("max_load") or 0]
                           for h in rows}}
        for arm in ("A", "B"):
            def caps_of(h):
                return h["caps"] if arm == "A" else (h.get("arm_b") or {}).get("caps")
            if arm == "B" and not any(h.get("arm_b") for h in rows):
                continue

            def proved(h, cap):
                c = caps_of(h) or {}
                return (c.get(cap) or {}).get("best", {}).get("status") == "proved"
            ks = sum(1 for h in S if proved(h, "search"))
            kd = sum(1 for h in Dd if proved(h, "detail"))
            nb = len(blocked)
            old = sum(1 for h in rows if not h.get("blocked") and caps_of(h)
                      and _distinct_proved(caps_of(h)) >= RULE_PROVED)
            per = {c: {"found": sum(1 for h in rows if caps_of(h) and
                                    caps_of(h)[c]["best"].get("found")),
                       "proved": sum(1 for h in rows if caps_of(h) and proved(h, c))}
                   for c in D.CAPS}
            srcs = {}
            for h in rows:
                for c, v in (caps_of(h) or {}).items():
                    if v["best"]["status"] == "proved":
                        k = v.get("origin") or v["best"].get("source") or "?"
                        if arm == "B":
                            k = f"{v.get('origin') or 'rule'}/{v['best'].get('source')}"
                        srcs[k] = srcs.get(k, 0) + 1
            a = {"search": [ks, len(S), wilson(ks, len(S))],
                 "detail": [kd, len(Dd), wilson(kd, len(Dd))],
                 "search_blocked_as_fail": [ks, len(S) + nb, wilson(ks, len(S) + nb)],
                 "detail_blocked_as_fail": [kd, len(Dd) + nb, wilson(kd, len(Dd) + nb)],
                 "old_metric": [old, len(rows), wilson(old, len(rows))],
                 "per_cap": per, "proved_sources": srcs}
            a["meets_primary_bar"] = bool(S and Dd and ks / len(S) >= 0.60
                                          and kd / len(Dd) >= 0.50)
            a["old_verdict"] = ("`anyuse map <host>` can be a button today"
                                if rows and old / len(rows) >= 0.5 else
                                "still a recording session")
            if arm == "B":
                usd = {"cerebras": 0.0, "openrouter": 0.0}
                calls = 0
                tools_b = tools_a = 0
                routed = routed_hit = aud = aud_hit = screens = l1_calls = 0
                for h in rows:
                    b = h.get("arm_b") or {}
                    for k in usd:
                        usd[k] += float((b.get("usd") or {}).get(k, 0.0))
                    calls += int(b.get("model_calls") or 0)
                    tools_b += _distinct_proved(b.get("caps") or {}) if b.get("caps") else 0
                    tools_a += _distinct_proved(h["caps"])
                    for e in b.get("router") or []:
                        screens += 1
                        if e.get("l1") is not None:
                            routed += 1
                            l1_calls += 1
                            routed_hit += int(bool(e["l1"].get("proved_caps")))
                    for e in b.get("audit") or []:
                        aud += 1
                        aud_hit += int(bool(e["l1"].get("proved_caps")))
                a.update(usd={k: round(v, 5) for k, v in usd.items()}, model_calls=calls,
                         calls_per_tool=round(calls / tools_b, 2) if tools_b else None,
                         calls_per_added_tool=(round(calls / (tools_b - tools_a), 2)
                                               if tools_b > tools_a else None),
                         tools=[tools_a, tools_b],
                         router={"screens_asked": screens, "routed_l1": routed,
                                 "routed_with_proof": routed_hit, "audited_unrouted": aud,
                                 "audited_with_proof": aud_hit,
                                 "cerebras_calls_saved_vs_every_screen": screens - l1_calls})
            res["arms"][arm] = a
        out[hset] = res
    return out


def report_v3(d: dict) -> str:
    lines = []
    for hset, r in summarise_v3(d).items():
        lines.append(f"\n### Set {hset} (listed {r['n_listed']}, reachable {r['n_reachable']}, "
                     f"has_search {r['n_has_search']}, has_items {r['n_has_items']})\n")
        lines.append(f"* ineligible: {r['ineligible']}")
        lines.append(f"* blocked: {r['blocked']}")
        lines.append(f"* unreachable: {r['unreachable']}")
        for arm, a in r["arms"].items():
            lines.append(f"* **ARM {arm}**: search {a['search'][0]}/{a['search'][1]} "
                         f"{a['search'][2]}, detail {a['detail'][0]}/{a['detail'][1]} "
                         f"{a['detail'][2]} -> primary bar "
                         f"{'MET' if a['meets_primary_bar'] else 'NOT met'}; blocked as "
                         f"failures: search {a['search_blocked_as_fail'][:2]}, detail "
                         f"{a['detail_blocked_as_fail'][:2]}; old v2 metric "
                         f"{a['old_metric'][0]}/{a['old_metric'][1]} {a['old_metric'][2]} "
                         f"({a['old_verdict']})")
            lines.append(f"  * per capability found/proved: "
                         + ", ".join(f"{c} {v['found']}/{v['proved']}"
                                     for c, v in a["per_cap"].items())
                         + f"; proved from: {a['proved_sources']}")
            if arm == "B":
                lines.append(f"  * spend {a['usd']}, model calls {a['model_calls']}, calls per "
                             f"proved tool {a['calls_per_tool']}, per added tool "
                             f"{a['calls_per_added_tool']} (tools A/B {a['tools']}); router "
                             f"{a['router']}")
        tr = r["traffic"]
        if tr:
            mx = max(tr.values(), key=lambda x: x[0])
            ml = max(v[1] for v in tr.values())
            lines.append(f"* traffic: max requests per host {mx[0]}, max single load {ml}")
    lines.append(f"\nviolations {d.get('violations')}; stopped {d.get('stopped')}; "
                 f"traffic_violation {d.get('traffic_violation', '')}")
    return "\n".join(lines)


def _tier(b: dict) -> int:
    return int(((b or {}).get("proof") or {}).get("tier") or 1)


def summarise_v4(d: dict) -> dict:
    """The v3 primary metric with tier 1 and tier 2
    reported apart, plus traffic per proved tool."""
    base = summarise_v3(d)
    elig = d.get("eligibility") or []
    for hset, res in base.items():
        rows = [h for h in d["hosts"].values() if h["set"] == hset]
        reach = [h for h in rows if not h.get("blocked")
                 and (h.get("signals") or {}).get("reachable")]
        S = [h for h in reach if h["signals"].get("has_search")]
        Dd = [h for h in reach if h["signals"].get("has_items")]

        def best(h, cap):
            return ((h.get("caps") or {}).get(cap) or {}).get("best") or {}

        def proved(h, cap, tier=None):
            b = best(h, cap)
            return b.get("status") == "proved" and (tier is None or _tier(b) == tier)
        a = res["arms"]["A"]
        for cap, pool in (("search", S), ("detail", Dd)):
            a[f"{cap}_tier1"] = [sum(1 for h in pool if proved(h, cap, 1)), len(pool)]
            a[f"{cap}_tier2"] = [sum(1 for h in pool if proved(h, cap, 2)), len(pool)]
        kinds, tiers = {}, {1: 0, 2: 0}
        tools = 0
        run_ms = {1: [], 2: []}
        for h in rows:
            seen = set()
            for cap in D.CAPS:
                b = best(h, cap)
                if b.get("status") != "proved":
                    continue
                k = f"{cap}/{b.get('proof_kind') or 'learn'}/tier{_tier(b)}"
                kinds[k] = kinds.get(k, 0) + 1
                if b.get("template") in seen:
                    continue
                seen.add(b.get("template"))
                tools += 1
                tiers[_tier(b)] += 1
                ms = ((b.get("proof") or {}).get("later_run_ms"))
                if ms:
                    run_ms[_tier(b)].append(float(ms))
        req = sum(int(h.get("requests") or 0) for h in rows)
        import statistics as _st
        a.update(proof_kinds=kinds, tools_tier1=tiers[1], tools_tier2=tiers[2],
                 tools=tools,
                 median_run_ms={t: (round(_st.median(v), 1) if v else None)
                                for t, v in run_ms.items()})
        res["requests_total"] = req
        res["requests_per_proved_tool"] = round(req / tools, 1) if tools else None
        res["max_requests_per_host"] = max((int(h.get("requests") or 0) for h in rows),
                                           default=0)
        res["max_single_load"] = max((int(h.get("max_load") or 0) for h in rows), default=0)
        res["cache_hits_total"] = sum(int(h.get("cache_hits") or 0) for h in rows)
        res["memo_hits_total"] = sum(int(h.get("memo_hits") or 0) for h in rows)
        res["n_eligible"] = len(rows)
        res["n_listed"] = len(rows) + len([e for e in elig if e["set"] == hset
                                           and not e["eligible"]])
    return base


def report_v4(d: dict) -> str:
    lines = []
    for hset, r in summarise_v4(d).items():
        a = r["arms"]["A"]
        lines.append(f"\n### Set {hset} (listed {r['n_listed']}, eligible {r['n_eligible']}, "
                     f"reachable {r['n_reachable']}, has_search {r['n_has_search']}, "
                     f"has_items {r['n_has_items']})\n")
        lines.append(f"* ineligible: {r['ineligible']}")
        lines.append(f"* blocked: {r['blocked']}")
        lines.append(f"* unreachable: {r['unreachable']}")
        lines.append(f"* **primary**: search {a['search'][0]}/{a['search'][1]} {a['search'][2]} "
                     f"(tier 1 {a['search_tier1'][0]}, tier 2 {a['search_tier2'][0]}), detail "
                     f"{a['detail'][0]}/{a['detail'][1]} {a['detail'][2]} (tier 1 "
                     f"{a['detail_tier1'][0]}, tier 2 {a['detail_tier2'][0]}) -> bar "
                     f"{'MET' if a['meets_primary_bar'] else 'NOT met'}")
        lines.append(f"* blocked as failures: search {a['search_blocked_as_fail'][:2]}, detail "
                     f"{a['detail_blocked_as_fail'][:2]}")
        lines.append(f"* old v2 metric: {a['old_metric'][0]}/{a['old_metric'][1]} "
                     f"{a['old_metric'][2]} ({a['old_verdict']})")
        lines.append("* per capability found/proved: " + ", ".join(
            f"{c} {v['found']}/{v['proved']}" for c, v in a["per_cap"].items())
            + f"; sources {a['proved_sources']}; kinds {a['proof_kinds']}")
        lines.append(f"* distinct tools {a['tools']} (tier 1 {a['tools_tier1']}, tier 2 "
                     f"{a['tools_tier2']}); median later run ms {a['median_run_ms']}")
        lines.append(f"* traffic: {r['requests_total']} requests, "
                     f"{r['requests_per_proved_tool']} per proved tool, max per host "
                     f"{r['max_requests_per_host']}, max single load {r['max_single_load']}, "
                     f"browser cache answers {r['cache_hits_total']}, identical-URL memo "
                     f"{r['memo_hits_total']}")
    lines.append(f"\nviolations {d.get('violations')}; stopped {d.get('stopped')}; "
                 f"traffic_violation {d.get('traffic_violation', '')}; min gap "
                 f"{min((d.get('min_gap_s') or {'-': 9}).values())}")
    return "\n".join(lines)


def report(args) -> int:
    with open(args.out) as fh:
        d = json.load(fh)
    if d.get("version") == "v4":
        print(report_v4(d))
        return 0
    if d.get("version") == "v3":
        print(report_v3(d))
        return 0
    lines = []
    sets = []
    for h in d["hosts"].values():
        if h["set"] not in sets:
            sets.append(h["set"])
    for hset in sets:
        rows = [h for h in d["hosts"].values() if h["set"] == hset]
        if not rows:
            continue
        lines.append(f"\n### Set {hset}\n")
        lines.append("| host | search | detail | next | filter | proved | req | s | US$ |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        per = {c: {"found": 0, "confirmed": 0, "proved": 0, "sort": 0} for c in D.CAPS}
        passing = passing_t1 = 0
        for h in rows:
            cells, proved, proved_t1 = [], 0, 0
            seen_t, seen_t1 = set(), set()
            for c in D.CAPS:
                b = h["caps"][c]["best"]
                for k in ("found", "confirmed"):
                    per[c][k] += int(bool(b.get(k)))
                per[c]["proved"] += int(b["status"] == "proved")
                per[c]["sort"] += int(b["status"] == "proved" and b.get("proof_kind") == "sort")
                # a host's tools are its DISTINCT proved templates
                if b["status"] == "proved" and b["template"] not in seen_t:
                    seen_t.add(b["template"])
                    proved += 1
                mark = {"proved": "**proved**", "discovered": "discovered", "checked": "checked",
                        "refused": "refused"}[b["status"]]
                tier = (b.get("proof") or {}).get("tier", 1)
                if b["status"] == "proved" and tier == 1 and b["template"] not in seen_t1:
                    seen_t1.add(b["template"])
                    proved_t1 += 1
                if b["status"] == "proved" and b.get("proof_kind") == "sort":
                    mark = "**proved (sort)**"
                elif b["status"] == "proved" and tier == 2:
                    mark = "**proved (tier 2)**"
                cells.append(f"{mark} ({b.get('source') or '-'})")
            usd = float(h.get("ranker_usd") or 0.0)
            passing += int(proved >= RULE_PROVED and usd <= RULE_USD)
            passing_t1 += int(proved_t1 >= RULE_PROVED and usd <= RULE_USD)
            lines.append(f"| {h['host']} | " + " | ".join(cells) +
                         f" | {proved} | {h['requests']} | {h['seconds']:.0f} | {usd:.3f} |")
        n = len(rows)
        tot = {k: sum(per[c][k] for c in D.CAPS) for k in ("found", "confirmed", "proved")}
        lines.append("")
        for c in D.CAPS:
            lines.append(f"* {c}: found {per[c]['found']}/{n} {wilson(per[c]['found'], n)}, "
                         f"confirmed {per[c]['confirmed']}/{n}, proved {per[c]['proved']}/{n} "
                         f"{wilson(per[c]['proved'], n)}"
                         + (f" (of which sort proof {per[c]['sort']})" if per[c]['sort'] else ""))
        for k in ("found", "confirmed", "proved"):
            lines.append(f"* all capabilities {k}: {tot[k]}/{4 * n} {wilson(tot[k], 4 * n)}")
        lines.append(f"* hosts with >= {RULE_PROVED} proved tools at <= US${RULE_USD:.2f}: "
                     f"**{passing}/{n}** {wilson(passing, n)}")
        lines.append(f"* ... counting tier-1 proofs only: {passing_t1}/{n} {wilson(passing_t1, n)}")
        verdict = ("`anyuse map <host>` can be a button today" if n and passing / n >= 0.5
                   else "still a recording session")
        lines.append(f"* VERDICT (fixed text), set {hset}: {verdict}")
    lines.append(f"\nspend US$ {d.get('spend_usd')}; requests {d.get('requests')}; "
                 f"blocked {d.get('blocked')}")
    lines.append(f"eligibility: {d.get('eligibility')}")
    print("\n".join(lines))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("out")
    ap.add_argument("--set", default="AB")
    ap.add_argument("--hosts-file", default="")
    ap.add_argument("--browser", default="",
                    help="v3: CDP endpoint of the container browser (http://IP:9222); every "
                         "request is guarded (execution/cdp_guard.py)")
    ap.add_argument("--cache", default="", help="DEV tuning only: page cache directory")
    ap.add_argument("--arm-b", action="store_true",
                    help="v3 ARM B: Jev ranking + L1/L2/L3 proposals after ARM A, per host")
    ap.add_argument("--ledger", default="",
                    help="ARM B whole-task spend ledger (JSON, outside the repository)")
    ap.add_argument("--version", default="v4", choices=["v3", "v4"],
                    help="v4: replay tier, detail proof, run "
                         "cache; v3 reproduces the v3 bench")
    ap.add_argument("--resume", action="store_true",
                    help="keep hosts already in <out.json>; run only the others")
    args = ap.parse_args()
    return run(args) if args.cmd == "run" else report(args)


if __name__ == "__main__":
    raise SystemExit(main())
