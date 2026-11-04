"""The web architecture, live.

usage: web_arch_live.py frame  <R>                      offline: the frame from screen/hosts_raw.tsv
       web_arch_live.py screen <R>                      robots.txt + ONE home-page GET per host, frame order
       web_arch_live.py cohort <R>                      offline: main sites, reserves, author inputs
       web_arch_live.py run    <R> --arm A --half H --browser URL [--container NAME] [--dev SITE]
       web_arch_live.py report <R>

Arms (every one under the same guard, judge, sites and tasks):
* ``baseline``     the existing Cerebras-only browser agent (``agent_browser.StepAgent``,
                   the L1 level: gpt-oss-120b, its escalation prompt);
* ``sonnet_agent`` the same loop with Sonnet 5 (OpenRouter) deciding every action from the
                   same DOM text (a frontier proxy; no map, no picker; hard cap US$1.50);
* ``arch``         ``execution/web_arch_run.WebArch``: planner w1 -> map -> OpenJev picker
                   (both orders + none) -> guarded actions; confirmer in shadow (PRIMARY).

Per site and arm: robots.txt -> the site's frozen tasks (one run each, one fresh anonymous
page per task) -> the judge -> the proof of a tool compiled from the first successful run
with a search (held-out values, tier 2 browser replay, tier 1 HTTP when the result URL
carries the value) -> (arch) the compiled repeat of every successful task with no model.

Traffic (docs/TRAFFIC_POLICY.md, approved for benchmarks): <= 300 requests per site in
total over the screen and all arms, split into fixed arm shares; 1 req/s; >= 3 s between
documents; statics <= 4 in flight and only below the share's static part; <= 30 MB per
site; UA ``research-probe/0.1 (read-only; person-paced)``; robots respected on every host;
a block, challenge, 403 or 429 stops the host for good and adds it to NEVER_CONTACT; an
anonymous browser (the cookie jar emptied before every task). Government hosts
(``gov_hosts``) are never chosen as sites; NEVER_CONTACT hosts, and government hosts
with ``ANYUSE_REFUSE_GOV_HOSTS`` on, are never contacted.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import fcntl
import functools
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import anyuse  # noqa: E402
if os.path.realpath(os.path.dirname(anyuse.__file__)) != os.path.realpath(os.path.dirname(os.path.dirname(HERE))):
    raise SystemExit(f"refused: `anyuse` resolves to {anyuse.__file__}, not this checkout")

from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution.cdp_guard import DOC_MIN_INTERVAL_S, REQUEST_MIN_INTERVAL_S, RequestBudget, TrafficViolation  # noqa: E402,E501

SPEC_ID = "SPEC_WEB_ARCH.md"
SEED = "anyuse-web-arch-2026-11-03"
UA = "research-probe/0.1 (read-only; person-paced)"
NEVER_CONTACT = os.path.join(HERE, "NEVER_CONTACT.txt")
CANDIDATES = os.path.join(HERE, "web_arch_candidates.tsv")
TASKS_FILE = os.path.join(HERE, "web_arch_tasks.json")
ARMS = ("baseline", "sonnet_agent", "arch")
SITE_CAP = 300
#: the site's 300 split into fixed arm shares (screen + robots below 10)
SHARES = {"baseline": 90, "sonnet_agent": 90, "arch": 110}
BYTES = {"baseline": 9 * 1024 * 1024, "sonnet_agent": 9 * 1024 * 1024, "arch": 11 * 1024 * 1024}
#: the share's last 40 % are for documents and data (the static share)
STATIC_RESERVE_FRAC = 0.4
SKIP_TYPES = ("Image", "Media", "Font")
MARGIN = 4
#: a task is not started with less than this left in the arm's share
MIN_TASK_LEFT = 14
#: a proof is not started with less than this left
MIN_PROOF_LEFT = 24
N_SCREEN = {"shop": 36, "other": 24}
MAIN = {"shop": 14, "other": 10}
#: screening continues in frame order while a stratum has fewer eligible sites than this
MIN_ELIGIBLE = {"shop": 26, "other": 16}
SCREEN_EXTRA_MAX = 140
#: a robots.txt Crawl-delay above this (seconds) makes a site ineligible
MAX_CRAWL_DELAY = 5.0
#: development sites (fresh, removed from the frame before any contact)
N_DEV = {"shop": 12, "other": 6}
#: the web scope of every arm (the site-owned allowances of ``live_bench``'s arm S)
SCOPE = {"ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS": True, "ANYUSE_ALLOW_READONLY_POST_SEARCH": True,
         "ANYUSE_ALLOW_SITE_OWN_HOSTS": True, "ANYUSE_THIRD_PARTY_POLICY": "block"}
#: never contacted, whatever the frame says (competitor agents' own domains)
COMPETITORS = ("browser-use.com", "openclaw.ai", "openclaw.com", "anthropic.com", "openai.com",
               "multion.ai", "adept.ai", "induced.ai", "browserbase.com", "skyvern.com")
TYPED_INFLIGHT_GRACE_S = 15.0
AGENT_MAX_STEPS = 10
SONNET_MAX_STEPS = 8


def R_(root: str, *p: str) -> str:
    return os.path.join(root, *p)


@contextlib.contextmanager
def locked(path: str):
    with open(path + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, default=str, ensure_ascii=False)
    os.replace(tmp, path)


def never() -> set:
    from never_contact import never_contact
    return never_contact()


def refusal(host: str) -> str:
    """'' or why no request may go to ``host`` (NEVER_CONTACT, government incl.
    public bodies when ANYUSE_REFUSE_GOV_HOSTS is on, a competitor's domain)."""
    from never_contact import refusal as nc
    why = nc(host)
    if why:
        return why
    reg = registrable(host)
    if reg in COMPETITORS:
        return "competitor domain"
    return ""


def add_never_contact(host: str, why: str) -> None:
    with locked(NEVER_CONTACT):
        with open(NEVER_CONTACT) as fh:
            lines = fh.read().splitlines()
        if host in {ln.strip() for ln in lines if not ln.startswith("#")}:
            return
        head = [ln for ln in lines if ln.startswith("#")]
        body = sorted({ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")} | {host})
        head.append(f"# 2026-11-03 (web-arch): {host}: {why[:90]}")
        with open(NEVER_CONTACT, "w") as fh:
            fh.write("\n".join(head + body) + "\n")


_SUFFIX2 = {"co.uk", "org.uk", "ac.uk", "com.au", "co.jp", "co.nz", "com.br", "co.za", "co.in", "com.mx",
            "com.tr", "com.cn", "com.sg", "co.kr"}


def registrable(host: str) -> str:
    p = (host or "").lower().strip(".").split(".")
    if len(p) >= 3 and ".".join(p[-2:]) in _SUFFIX2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


# ---------------------------------------------------------------------------
# 1. the frame (offline)
# ---------------------------------------------------------------------------

def build_frame(raw_rows: Sequence[Tuple[str, str, str]], prior: set, nc: set,
                gov=None) -> Tuple[List[Dict[str, str]], Dict[str, List[str]]]:
    """Filter the remembered hosts (PURE): malformed, a registrable domain any
    earlier cohort or archive wrote down, NEVER_CONTACT, government (incl.
    public bodies, fail-closed), competitor domains, duplicate registrable
    domains; then shuffle each stratum with the frame seed and interleave
    3 shops : 2 others."""
    if gov is None:
        from anyuse.execution.gov_hosts import government_host as gov
    prior_reg = {registrable(h) for h in prior}
    dropped: Dict[str, List[str]] = {}
    seen: set = set()
    keep: Dict[str, List[Dict[str, str]]] = {"shop": [], "other": []}
    for host, stratum, cat in raw_rows:
        h = host.strip().lower()
        why = ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", h) or "skip" in cat or stratum not in keep:
            why = "malformed"
        elif h in prior or registrable(h) in prior_reg:
            why = "prior"
        elif h in nc:
            why = "never_contact"
        elif gov(h):
            why = "government"
        elif registrable(h) in COMPETITORS:
            why = "competitor"
        elif registrable(h) in seen:
            why = "duplicate"
        if why:
            dropped.setdefault(why, []).append(h)
            continue
        seen.add(registrable(h))
        keep[stratum].append({"site": h, "stratum": stratum, "category": cat.strip()})
    for st in keep:
        random.Random(f"{SEED}:frame:{st}").shuffle(keep[st])
    out: List[Dict[str, str]] = []
    s, o = list(keep["shop"]), list(keep["other"])
    while s or o:
        for _ in range(3):
            if s:
                out.append(s.pop(0))
        for _ in range(2):
            if o:
                out.append(o.pop(0))
    return out, dropped


def cmd_frame(root: str) -> int:
    raw = [ln.rstrip("\n").split("\t") for ln in open(R_(root, "screen", "hosts_raw.tsv")) if ln.strip()]
    raw = [(r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "") for r in raw]
    prior = {ln.strip() for ln in open(R_(root, "screen", "prior_hosts_snapshot.txt")) if ln.strip()}
    # the NEVER_CONTACT list as it stood when the frame was first built (before any
    # contact): a later block never reshuffles the frame
    snap = R_(root, "screen", "never_contact_at_frame.txt")
    if not os.path.exists(snap):
        with open(snap, "w") as fh:
            fh.write("\n".join(sorted(never())) + "\n")
    nc_at_frame = {ln.strip() for ln in open(snap) if ln.strip() and not ln.startswith("#")}
    frame, dropped = build_frame(raw, prior, nc_at_frame)
    # DEV sites (development only, never measured, never in a rate): the LAST
    # 12 shops and the LAST 6 others of the frame order, removed from it
    dev = [e for e in frame if e["stratum"] == "shop"][-N_DEV["shop"]:] + \
        [e for e in frame if e["stratum"] == "other"][-N_DEV["other"]:]
    frame = [e for e in frame if e not in dev]
    save_json(R_(root, "screen", "dev_sites.json"), dev)
    with open(CANDIDATES, "w") as fh:
        for e in frame:
            fh.write(f"{e['site']}\t{e['stratum']}\t{e['category']}\n")
    save_json(R_(root, "screen", "frame_dropped.json"), dropped)
    print(f"frame: {len(frame)} fresh ({sum(e['stratum'] == 'shop' for e in frame)} shop); "
          f"dropped {({k: len(v) for k, v in dropped.items()})}")
    return 0


def dev_sites(root: str) -> set:
    return {e["site"] for e in load_json(R_(root, "screen", "dev_sites.json"), [])}


def load_frame() -> List[Dict[str, str]]:
    out = []
    for ln in open(CANDIDATES):
        if ln.strip():
            site, stratum, cat = ln.rstrip("\n").split("\t")
            out.append({"site": site, "stratum": stratum, "category": cat})
    return out


# ---------------------------------------------------------------------------
# 2. the screen (robots.txt + ONE home-page GET per host)
# ---------------------------------------------------------------------------

_PRICE_HINT = re.compile(r"(€|£|\$|\bEUR\b|\bGBP\b|\bUSD\b|\bCHF\b|\bkr\b)\s?\d|\d[.,]\d{2}\s?(€|£|\bEUR\b|\bCHF\b|kr)")
_CART_HINT = re.compile(r"(?i)add to (cart|basket|bag)|in den warenkorb|/cart\b|/basket\b|warenkorb|panier|carrito|carrello")


def shop_signals(body: str) -> Dict[str, Any]:
    """Reporting only, from the page already fetched (no request)."""
    blocks = re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body or "")
    facts = W.product_facts(blocks)
    return {"jsonld_products": len(facts), "price_marks": len(_PRICE_HINT.findall(body or "")),
            "cart_marks": len(_CART_HINT.findall(body or "")),
            "shop_like": bool(facts) or (len(_PRICE_HINT.findall(body or "")) >= 3 and bool(_CART_HINT.search(body or "")))}


def snippets(body: str, url: str, limit: int = 1600) -> Dict[str, Any]:
    """What the task authors see of the start page (supplied by us; they never
    contact the site): the title, headings, visible text lines and link texts."""
    from anyuse.execution.confirmer import read_html
    r = read_html(body or "")
    scan = D.scan_page(body or "", url)
    host = D.host_of(url)
    links, seen = [], set()
    for ln in scan.links:
        t = " ".join((ln.text or "").split())
        if not t or len(t) < 3 or len(t) > 70 or t.lower() in seen or D.host_of(ln.url) != host:
            continue
        if W.forbidden_control(t, ln.url):
            continue
        seen.add(t.lower())
        links.append(t)
    text = r.get("text") or ""
    return {"title": r.get("title"), "headings": r.get("headings"),
            "links": links[:80], "text": text[:limit]}


def cmd_screen(root: str, dev: bool = False) -> int:
    import discover_bench as DBN
    import live_bench as V
    import value_screen as VS
    import query_values as VV
    frame = load_json(R_(root, "screen", "dev_sites.json"), []) if dev else load_frame()
    dst = R_(root, "dev", "screen.json") if dev else R_(root, "screen", "screen.json")
    out = load_json(dst, {"sites": {}, "order": []})

    def eligible(st):
        return sum(1 for s in out["sites"].values() if s.get("stratum") == st and s.get("screen") == "eligible")

    def screened(st):
        return sum(1 for s in out["sites"].values() if s.get("stratum") == st)
    extra = 0
    for e in frame:
        site, st = e["site"], e["stratum"]
        if site in out["sites"]:
            continue
        base = dev or screened(st) < N_SCREEN[st]
        more = (not base) and eligible(st) < MIN_ELIGIBLE[st] and extra < SCREEN_EXTRA_MAX
        if not (base or more):
            continue
        if more:
            extra += 1
        start = f"https://{site}/"
        rec: Dict[str, Any] = {"site": site, "stratum": st, "category": e["category"], "start_url": start,
                               "at": time.strftime("%H:%M:%S"), "extra": bool(more)}
        why = refusal(site) or ("" if dev or site not in dev_sites(root) else "a DEV site (never measured)")
        if why:
            rec["screen"] = "refused: " + why
            out["sites"][site] = rec
            out["order"].append(site)
            save_json(dst, out)
            continue
        pacer = V.HostPacer(1.0)
        fetch = D.HttpFetcher(user_agent=UA, allow_url=lambda u: robots.allow(u), pacer=pacer,
                              max_requests_per_host=SITE_CAP)
        robots = DBN.Robots(fetch)
        robots.load(site)
        rec["robots"] = robots.notes.get(site)
        if str(rec["robots"]).startswith("robots.txt 40") or "blocked" in str(rec["robots"]):
            add_never_contact(site, f"web-arch screen: {rec['robots']}")
            rec["screen"] = "robots"
        elif not robots.allow(start):
            rec["screen"] = "robots"
        else:
            d = V.crawl_delay(robots, site)
            if d > 1.0:
                pacer.intervals[site] = d
                rec["crawl_delay"] = d
        if rec.get("screen") is None and float(rec.get("crawl_delay") or 0) > MAX_CRAWL_DELAY:
            # robots.txt asks for more than MAX_CRAWL_DELAY s between requests: a task
            # of ~10 page loads would take hours (DEV www.kartell.com: 100 s); not eligible
            rec["screen"] = "crawl_delay"
        elif rec.get("screen") is None:
            r = fetch.get(start)
            rec.update(status=r.status, final_url=r.final_url, error=r.error[:120], bytes=len(r.body or ""))
            import site_screen as SS
            blk = V.page_block(SS.text_of(r.body), r.final_url or start,
                               [{"resource_type": "Document", "url": r.final_url or start, "status": r.status}])
            if r.error.startswith("blocked") or blk or r.status in (401, 403, 429):
                why = blk or r.error or f"HTTP {r.status}"
                add_never_contact(site, f"web-arch screen: {why}")
                rec.update(screen="blocked", why=why[:160])
            elif r.status != 200 or D.host_of(r.final_url or start) != site or "html" not in (r.mime or ""):
                rec["screen"] = "no_page"
            else:
                rec.update(VS.classify_safe(site, start, start, r.body, r.final_url or start,
                                             lambda dom: VV.listing_tokens(dom), min_tokens=VV.N_ELIGIBLE))
                rec["shop"] = shop_signals(r.body)
                rec["snippets"] = snippets(r.body, r.final_url or start)
                rec["tokens"] = [t for t, _s in VV.listing_tokens(r.body)][:40]
                rec["stratum_screen"] = "get_form" if rec.get("stratum") == "get_form" else None
                rec["stratum"] = st
                if rec.get("screen") == "eligible" and rec.get("listing_url") and \
                        not robots.allow(rec["listing_url"]):
                    rec["screen"] = "robots"
                if rec.get("screen") == "eligible":
                    os.makedirs(os.path.dirname(dst) + "/home", exist_ok=True)
                    with open(os.path.join(os.path.dirname(dst), "home", f"{site}.html"), "w") as fh:
                        fh.write(r.body or "")
        rec["requests"] = int(pacer.count.get(site, 0))
        out["sites"][site] = rec
        out["order"].append(site)
        save_json(dst, out)
        print(f"{site:38s} {st:5s} {rec.get('screen')!s:14s} req={rec['requests']} "
              f"shop={((rec.get('shop') or {}).get('shop_like'))}", flush=True)
    print({st: {"screened": screened(st), "eligible": eligible(st)} for st in ("shop", "other")})
    return 0


# ---------------------------------------------------------------------------
# 3. the cohort (offline, from the screen): main, reserves, author inputs
# ---------------------------------------------------------------------------

def cohort_from(screen: Dict[str, Any]) -> Dict[str, Any]:
    """PURE: eligible sites in screen order per stratum; the first MAIN[stratum]
    are the main sites (halves A/B alternate), the rest reserves in order."""
    el = {"shop": [], "other": []}
    for site in screen["order"]:
        s = screen["sites"][site]
        if s.get("screen") == "eligible" and s.get("stratum") in el:
            el[s["stratum"]].append(site)
    main = {st: el[st][:MAIN[st]] for st in el}
    reserves = {st: el[st][MAIN[st]:] for st in el}
    # interleave the strata by their position in each list (shops and others spread evenly)
    keyed = [((i + 0.5) / max(1, len(main["shop"])), 0, site) for i, site in enumerate(main["shop"])] + \
            [((j + 0.5) / max(1, len(main["other"])), 1, site) for j, site in enumerate(main["other"])]
    order = [site for _k, _s, site in sorted(keyed)]
    half = {site: ("A" if i % 2 == 0 else "B") for i, site in enumerate(order)}
    labels = {}
    for i, site in enumerate(el["shop"] + el["other"]):
        labels[site] = f"S{i + 1:02d}"
    return {"main": main, "reserves": reserves, "order": order, "half": half, "labels": labels,
            "eligible": {st: len(el[st]) for st in el}}


def cmd_cohort(root: str) -> int:
    screen = load_json(R_(root, "screen", "screen.json"), None)
    c = cohort_from(screen)
    save_json(R_(root, "screen", "cohort.json"), c)
    inputs = []
    for site in c["main"]["shop"] + c["main"]["other"] + c["reserves"]["shop"] + c["reserves"]["other"]:
        s = screen["sites"][site]
        sn = s.get("snippets") or {}
        inputs.append({"label": c["labels"][site], "category": s.get("category"),
                       "shop": s.get("stratum") == "shop",
                       "title": sn.get("title"), "headings": sn.get("headings"),
                       "links": sn.get("links"), "text": sn.get("text")})
    save_json(R_(root, "authoring", "site_inputs.json"), inputs)
    print(json.dumps({k: v for k, v in c.items() if k in ("eligible", "main", "reserves")}, indent=1))
    return 0


# ---------------------------------------------------------------------------
# 4. one arm on one site
# ---------------------------------------------------------------------------

def site_ledger_path(root: str) -> str:
    return R_(root, "runs", "site_ledger.json")


def site_total(root: str, site: str) -> Tuple[int, int]:
    d = load_json(site_ledger_path(root), {})
    e = d.get(site) or {}
    return sum(int(v.get("requests", 0)) for v in e.values()), sum(int(v.get("bytes", 0)) for v in e.values())


def book_site(root: str, site: str, phase: str, requests: int, nbytes: int) -> None:
    p = site_ledger_path(root)
    with locked(p):
        d = load_json(p, {})
        prev = d.setdefault(site, {}).get(phase)
        if prev:
            # a second booking of the same phase (a DEV re-run, a resumed arm) adds up
            requests, nbytes = int(requests) + int(prev.get("requests", 0)), int(nbytes) + int(prev.get("bytes", 0))
        d[site][phase] = {"requests": int(requests), "bytes": int(nbytes),
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        save_json(p, d)


def share_for(arm: str, used: int) -> int:
    """PURE: the arm's share on a site, never beyond what the 300 leaves."""
    return max(0, min(SHARES[arm], SITE_CAP - used - MARGIN))


class SiteCtx:
    """The per-site, per-arm guard: its own pacer, fetcher, robots, budget and
    web scope, so every cap is the arm's share of THIS site."""

    def __init__(self, site: str, arm: str, share: int, nbytes: int, endpoint: str, gate):
        import discover_bench as DBN
        import live_bench as V
        from anyuse.execution.web_scope import WebScope
        self.site, self.arm, self.share, self.endpoint = site, arm, share, endpoint
        self.pacer = V.HostPacer(1.0)
        # the third-party part of the scope is the ONE setting ANYUSE_THIRD_PARTY
        # (web_scope.third_party_flags; default "search" = the site's own search provider, as this scope had); the
        # first-party allowances stay the arms' (SCOPE)
        from anyuse.execution.web_scope import third_party_flags
        self.scope = WebScope(readonly_post=SCOPE["ANYUSE_ALLOW_READONLY_POST_SEARCH"],
                              own_hosts=SCOPE["ANYUSE_ALLOW_SITE_OWN_HOSTS"], **third_party_flags())

        def allow(u):
            h = D.host_of(u)
            if refusal(h):
                return False
            if h not in self.robots.parsers and self.scope.site_for(h):
                self.robots.load(h)
            return self.robots.allow(u)
        self.allow = allow
        self.fetch = D.HttpFetcher(user_agent=UA, allow_url=allow, pacer=self.pacer,
                                   max_requests_per_host=share)
        self.robots = DBN.Robots(self.fetch)
        self.budget = RequestBudget(self.pacer, per_load_cap=share, per_host_cap=share, site_cap=share,
                                    nav_min_interval_s=DOC_MIN_INTERVAL_S, load_per_navigation=True,
                                    realistic_browser=True, static_bytes_cap=nbytes, static_in_flight=4,
                                    task_gate=gate, web_scope=self.scope,
                                    static_reserve=int(share * STATIC_RESERVE_FRAC),
                                    skip_types=SKIP_TYPES)
        self.budget.bind_fetcher(self.fetch)
        self.budget.cache_enabled = True

    def allowed(self, url: str) -> bool:
        try:
            return bool(self.allow(url)) and not D._ACCOUNT.search(urllib.parse.urlsplit(url).path)
        except Exception:
            return False

    def used(self) -> int:
        return self.budget.site_total(self.site)

    def left(self) -> int:
        return self.share - self.used()

    def nbytes(self) -> int:
        return int(self.budget.site_bytes.get(self.budget.site_of(self.site), 0))


#: the sonnet_agent's wider view (a frontier agent reads more of the page than the
#: historical 40 controls / 2,500 characters): 60 controls, 100 lines / 3,000 characters;
#: its clicks use the same wider control list
SONNET_ELEMENTS, SONNET_LINES, SONNET_CHARS = 60, 100, 3000


def wide_js():
    from anyuse.execution import agent_browser as AB
    obs = AB.OBSERVE_JS.replace(".slice(0, 40);", f".slice(0, {SONNET_ELEMENTS});") \
        .replace(".slice(0, 60).join('\\n').slice(0, 2500)", f".slice(0, {SONNET_LINES}).join('\\n').slice(0, {SONNET_CHARS})")
    pick = AB.PICK_JS.replace(".slice(0, 40);", f".slice(0, {SONNET_ELEMENTS});")
    assert obs != AB.OBSERVE_JS and pick != AB.PICK_JS and f"slice(0, {SONNET_CHARS})" in obs
    return obs, pick


def session_class(wide: bool = False):
    """The bench's guarded page: GET only (non-GET left to the web scope's
    read-only rules), the cookie jar emptied before the task (anonymous), the
    static cache harvested after, every observation's page and product data
    kept (the agents' pages for the judge)."""
    import ladder_verify as LR
    LR.UA = UA
    LR.GetOnlySession.inflight_grace_s = TYPED_INFLIGHT_GRACE_S
    from anyuse.execution import web_arch_run as RR

    obs_js, pick_js = wide_js() if wide else (None, None)

    class BenchSession(LR.GetOnlySession):
        def __init__(self, endpoint, allow_url=None, budget=None, **kw):
            super().__init__(endpoint, user_agent=UA, allow_url=allow_url, budget=budget)
            self.pages: List[Dict[str, Any]] = []
            self.doms: List[Tuple[str, str]] = []

        if wide:
            async def focus(self, index: int) -> bool:
                return bool(await self.js(pick_js % ("false", int(index))))

            async def click(self, index: int) -> None:
                await self.js(pick_js % ("true", int(index)))
                await self.settle(5.0)

        async def __aenter__(self):
            await super().__aenter__()
            await self.call("Network.clearBrowserCookies")
            return self

        async def __aexit__(self, *exc):
            try:
                if self.budget.cache_enabled:
                    try:
                        await self.harvest_cache()
                    except Exception:                  # noqa: BLE001
                        pass
            finally:
                await super().__aexit__(*exc)

        async def observe(self):
            obs = (await self.js(obs_js) or {}) if wide else await super().observe()
            try:
                r = await RR.read_page(self)
                self.pages.append({"url": r.get("url"), "title": r.get("title"), "h1": r.get("h1"),
                                   "links": RR.link_texts(r),
                                   "facts": (r.get("facts") or [])[:6],
                                   "product": W.main_product(r.get("facts") or []),
                                   "text_head": str(r.get("main_text") or r.get("text") or "")[:1500],
                                   "breadcrumb": r.get("breadcrumb"), "text": str(r.get("text") or "")[:20000]})
                self.doms.append((str(r.get("url") or ""), await self.dom()))
                self.doms = self.doms[-10:]
            except Exception:                          # noqa: BLE001 - an observation never fails
                pass
            return obs
    return BenchSession


#: the static cache of a site, shared by the arms in turn (static assets only, under
#: the guard's HTTP caching rules; a hit is not sent and not counted): a person's
#: browser cache. Scratch, not evidence.
CACHE_DIR = "/tmp/anyuse-the measurement harness"


def _cache_file(site: str) -> str:
    return os.path.join(CACHE_DIR, re.sub(r"[^a-z0-9]+", "_", site.lower()) + ".json")


def load_cache(budget, site: str) -> int:
    d = load_json(_cache_file(site), {})
    now_c, now_w, n = budget.clock(), time.time(), 0
    for k, e in (d.get("entries") or {}).items():
        left = float(e.get("expires_wall", 0)) - now_w
        if left <= 0:
            continue
        e2 = {kk: vv for kk, vv in e.items() if kk != "expires_wall"}
        e2.update(expires_at=now_c + left, stored_at=now_c)
        budget.cache[k] = e2
        n += 1
    for m, url, v in d.get("vary") or []:
        budget.vary[(m, url)] = tuple(v)
    return n


def save_cache(budget, site: str) -> int:
    os.makedirs(CACHE_DIR, exist_ok=True)
    now_c, now_w = budget.clock(), time.time()
    entries = {k: dict({kk: vv for kk, vv in e.items() if kk not in ("expires_at", "stored_at")},
                       expires_wall=now_w + (e["expires_at"] - now_c))
               for k, e in budget.cache.items() if e.get("expires_at", 0) > now_c}
    save_json(_cache_file(site), {"entries": entries, "vary": [[m, u, list(v)] for (m, u), v in budget.vary.items()]})
    return len(entries)


def settle_fn(endpoint: str):
    from anyuse.execution.agent_browser import Navigator
    nav = Navigator(endpoint, ceiling_s=20.0)

    async def settle(page):
        await nav._settle(page, time.monotonic())
    return settle


class Arm:
    """Everything one arm process needs: models, the reader, the ledger."""

    def __init__(self, root: str, arm: str, endpoint: str, log, dev: bool = False):
        import web_arch_models as M
        self.root, self.arm, self.endpoint, self.log, self.dev = root, arm, endpoint, log, dev
        self.M = M
        self.led = M.Ledger(R_(root, "ledger.json"))
        self.spec = W.load_planner_spec()
        self.or_key = M.read_key("OPENROUTER_API_KEY")
        self.reader = M.make_reader(self.or_key, self.led, R_(root, "runs", "reader_cache.json"),
                                    R_(root, "runs", "reader_log.jsonl"))
        self.openjev_log = R_(root, "runs", f"openjev_{arm}.jsonl")
        if arm == "arch":
            ck = M.read_key("CEREBRAS_API_KEY")
            self.planner = M.make_planner(ck, self.spec, self.led)
            self.extractor = M.make_extractor(ck, self.spec, self.led)
            del ck
            mode = os.environ.get("WEB_ARCH_PICKER", "openjev")
            if mode == "openjev":
                self.chooser = M.make_openjev_chooser(log=self._ojlog)
                self.confirmer = M.make_openjev_confirmer(log=self._ojlog)
            else:                                      # development only: no picker, no confirmer
                self.chooser = self.confirmer = None
            self.t2 = W.load_t2()
        elif arm == "baseline":
            os.environ["ANYUSE_MODEL_ENV_FILE"] = M.ENV_FILE
        elif arm == "sonnet_agent":
            # development spend is booked apart: the arm's US$1.50 cap counts measured calls only
            self.brain = M.SonnetBrain(self.or_key, self.led, kind="sonnet_agent_dev" if dev else "sonnet_agent")

    def _ojlog(self, rec: Dict[str, Any]) -> None:
        with open(self.openjev_log, "a") as fh:
            fh.write(json.dumps(dict(rec, ts=round(time.time(), 3))) + "\n")

    # -- one task ---------------------------------------------------------------------
    def run_task(self, ctx: SiteCtx, task: Dict[str, Any], start_url: str, shots_dir: str) -> Dict[str, Any]:
        Session = session_class(wide=self.arm == "sonnet_agent")
        holder: List[Any] = []

        def factory(endpoint, allow_url=None, **kw):
            s = Session(endpoint, allow_url=allow_url or ctx.allowed, budget=ctx.budget)
            holder.append(s)
            return s
        t0 = time.monotonic()
        if self.arm == "arch":
            from anyuse.execution import web_arch_run as RR

            def sink(where, data_url, _t=task["id"]):
                os.makedirs(shots_dir, exist_ok=True)
                name = f"{_t}_{where}_{int(time.time() * 1000) % 10 ** 8}.jpg"
                with open(os.path.join(shots_dir, name), "wb") as fh:
                    fh.write(base64.b64decode(data_url.split(",", 1)[1]))
                return name
            eng = RR.WebArch(self.planner, extractor=self.extractor, chooser=self.chooser,
                             confirmer=self.confirmer, t2=self.t2, settle=settle_fn(self.endpoint),
                             spec=self.spec, shot_sink=sink)

            async def go():
                async with factory(self.endpoint, allow_url=ctx.allowed) as page:
                    rec = await eng.run(page, dict(task, task_kind=task.get("kind")), start_url)
                    rec["guard_failed"] = [list(f) for f in page.guard.failed[:60]]
                    return rec
            rec = asyncio.run(go())
            rec["task_kind"] = task.get("kind")
        else:
            from anyuse.execution import agent_browser as AB
            from anyuse.execution import web_arch_run as RR
            from anyuse.execution.model_adapter import RunBudget
            if self.arm == "baseline":
                lv = AB.default_levels()[0]
                brain = AB.CerebrasBrain(role=lv.role, max_tokens=lv.max_tokens, run_budget=RunBudget(lv.max_usd),
                                         price_in=lv.price_in, price_out=lv.price_out)
                deadline, max_calls = lv.max_seconds, lv.max_calls
            else:
                brain, deadline, max_calls = self.brain, 300.0, SONNET_MAX_STEPS + 2
            agent = AB.StepAgent(brain, session_factory=factory, endpoint=self.endpoint,
                                 max_steps=AGENT_MAX_STEPS if self.arm == "baseline" else SONNET_MAX_STEPS, allow_url=ctx.allowed, trace=True,
                                 system=AB.SYSTEM_ESCALATION, no_progress_steps=AB.NO_PROGRESS_STEPS,
                                 max_calls=max_calls, deadline_s=deadline)
            text = str(task.get("utterance") or "") + RR.AGENT_TASK_SUFFIX
            run = agent.run(text, start_url, "")
            page = holder[-1] if holder else None
            rec = RR.agent_record(run, task, self.arm, page.pages if page else [],
                                  guard_failed=[tuple(f) for f in (page.guard.failed if page else [])])
            rec["guard_failed"] = [list(f) for f in (page.guard.failed[:60] if page else [])]
            if page is not None and page.pages:
                last = page.pages[-1]
                rec["final"].update(title=last.get("title"), h1=last.get("h1"), facts=last.get("facts"),
                                    breadcrumb=last.get("breadcrumb"), links=last.get("links"))
                if not rec["final"].get("text"):
                    rec["final"]["text"] = last.get("text")
            if page is not None and rec["program"]["ops"] and rec.get("search_value"):
                # the compile page: the first observation after the search
                after = next((u for t in run.trace or [] if (t.get("choice") or {}).get("action")
                              in ("search", "submit") for u in [((t.get("outcome") or {}).get("url_after"))] if u), "")
                dom = next((d for u, d in page.doms if after and u == after), "")
                if dom:
                    rec.setdefault("doms", {})["after_search"] = dom
            if self.arm == "baseline":
                # Cerebras reports no price: the list price of the tokens, booked on the ledger
                rec["cost"] = self.M.cerebras_cost(int(run.tokens_in or 0), int(run.tokens_out or 0))
                self.led("baseline", actual=rec["cost"])
            if run.stop == "budget_exhausted":
                rec["budget_exhausted"] = True
            if run.stop == AB.INFRASTRUCTURE_STOP:
                rec["stop"] = "infrastructure"
                rec["infra"] = run.error
        rec["seconds_wall"] = round(time.monotonic() - t0, 2)
        rec["requests_after"] = ctx.used()
        return rec


def guard_time(root: str, fallback: float = 0.0) -> float:
    """The pod guard (epoch seconds: the pod's stop time minus 10 minutes), from
    runs/pod_guard_epoch.txt (updated when main moves the stop time), else ``fallback``."""
    try:
        return float(open(R_(root, "runs", "pod_guard_epoch.txt")).read().split()[0])
    except (OSError, ValueError, IndexError):
        return float(fallback or 0.0)


def past_guard(arm) -> bool:
    g = guard_time(arm.root, getattr(arm, "stop_at", 0.0))
    return bool(g) and time.time() > g


def is_blocked(rec: Dict[str, Any]) -> str:
    """The same block check for every arm and phase: the run's own stop, then
    the final page's text with the session's requests to the site (and, on the
    first page, a login wall). ``block_strict_rule`` (every request, as ``live_bench`` counts) is
    kept beside it for the report."""
    from anyuse.execution.web_arch_run import site_block
    if rec.get("stop") == "blocked":
        return str(rec.get("blocked") or "blocked")
    fin = rec.get("final") or {}
    rec["block_strict_rule"] = site_block(str(fin.get("text") or ""), str(fin.get("url") or ""),
                                      rec.get("net") or [], all_requests=True)
    return site_block(str(fin.get("text") or ""), str(fin.get("url") or ""), rec.get("net") or [])


# ---------------------------------------------------------------------------
# 5. the proof (held-out values) and the compiled repeat
# ---------------------------------------------------------------------------

def recording(dom: str, value: str, url: str) -> Dict[str, Any]:
    return {"dom": dom, "param": {"name": "q", "value": value}, "final_url": url, "ui": {}}


def tier1_template(url: str, value: str) -> str:
    """The result URL with the value replaced by ``{q}`` (query or path), or ""."""
    if not url or not value:
        return ""
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    hit = [k for k, v in q if W.fold(v) == W.fold(value)]
    if hit:
        pairs = [(k, "{q}" if W.fold(v) == W.fold(value) else v) for k, v in q]
        qs = "&".join(f"{urllib.parse.quote_plus(k)}=" + ("{q}" if v == "{q}" else urllib.parse.quote_plus(v))
                      for k, v in pairs)
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, qs, ""))
    path = urllib.parse.unquote(p.path)
    for form in (value, value.replace(" ", "-"), value.replace(" ", "+"), value.replace(" ", "%20")):
        if form and form.lower() in path.lower():
            i = path.lower().index(form.lower())
            return urllib.parse.urlunsplit((p.scheme, p.netloc, path[:i] + "{q}" + path[i + len(form):], p.query, ""))
    return ""


def prove(arm: Arm, ctx: SiteCtx, rec: Dict[str, Any], values: List[str], out_dir: str) -> Dict[str, Any]:
    """Prove the tool compiled from ``rec`` (a successful run with a search) on
    held-out values (``proof_split``): compile = the run's own value, fitting =
    values[0], validation = values[1:3]. Tier 2 = the browser replay; tier 1 =
    the HTTP GET of the result URL template, compared with the replay's list."""
    from anyuse.execution import proof_split as PS
    from anyuse.execution import web_arch_run as RR
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth
    prog = rec.get("program") or {}
    q0 = W.search_value(prog)
    res: Dict[str, Any] = {"task": rec.get("task"), "arm": arm.arm, "compile_value": q0}
    if not q0:
        res.update(status="no_tool", why="the run made no search")
        return res
    item = any((p.get("product") or {}).get("price") is not None for p in rec.get("pages") or []) and \
        any(o.get("op") == "goto" for o in prog.get("ops") or [])
    kind = W.tool_kind(prog, item_facts=item)
    res["kind"] = kind
    split = PS.assign([q0] + list(values[:3]), n_selection=0, n_fitting=1)
    res["split"] = split.to_dict()
    search_tool = W.proof_tool(prog, "search")
    tool = W.proof_tool(prog, kind) if kind == "item" else search_tool
    Session = session_class()
    settle = settle_fn(arm.endpoint)

    def replay(ops, by_rule=True):
        async def go():
            async with Session(arm.endpoint, allow_url=ctx.allowed, budget=ctx.budget) as page:
                return await RR.replay(page, ops, settle=settle, by_rule=by_rule)
        return asyncio.run(go())
    compile_dom = (rec.get("doms") or {}).get("after_search") or ""
    compile_url = ""
    if not compile_dom:
        r0 = replay(W.bind(search_tool["ops"], q0))
        compile_dom, compile_url = (r0.get("doms") or {}).get("after_search") or "", r0.get("search_url") or ""
        res["compile_replayed"] = True
    fit_v = split.fitting[0] if split.fitting else values[0]
    r_fit = replay(W.bind(search_tool["ops"], fit_v))
    split.note_fit([q0, fit_v])
    if is_blocked(r_fit):
        res.update(status="blocked", why=is_blocked(r_fit))
        return res
    fit_dom = (r_fit.get("doms") or {}).get("after_search") or ""
    recs = [recording(compile_dom, q0, compile_url), recording(fit_dom, fit_v, r_fit.get("search_url") or "")]
    selector = derive_ground_truth(recs) if compile_dom and fit_dom else ""
    res["selector"] = selector
    if not selector:
        res.update(status="failed", why="no result list derivable on the compile + fitting pages "
                                        + str(recs[0].get("recorder_warning") or recs[1].get("recorder_warning") or ""))
        return res
    compile_items = [str(x) for x in extract(compile_dom, selector, limit=30)]
    cases = []
    for v in list(split.validation)[:2]:
        if ctx.left() < 10:
            cases.append({"value": v, "tier2": False, "why": "arm share exhausted"})
            continue
        rv = replay(W.bind(tool["ops"], v))
        blk = is_blocked(rv)
        if blk:
            res.update(status="blocked", why=blk)
            return res
        dom = (rv.get("doms") or {}).get("after_search") or ""
        items = [str(x) for x in extract(dom, selector, limit=30)] if dom else []
        text = next((p.get("text_head") for p in rv.get("pages") or [] if p.get("url") == rv.get("search_url")), "")
        ok, why = W.list_contract(items, v, text or "")
        changed = W.values_changed(compile_items, items)
        case = {"value": v, "items": items[:10], "n_items": len(items), "contract": why, "changed": changed,
                "search_url": rv.get("search_url"), "tier2": bool(ok and changed and rv.get("stop") != "failed"),
                "stop": rv.get("stop")}
        if kind == "item":
            prod = W.main_product((rv.get("final") or {}).get("facts") or [], v)
            case["item"] = {"url": (rv.get("final") or {}).get("url"),
                            "product": {k: (prod or {}).get(k) for k in ("name", "price", "currency", "availability")}
                            if prod else None}
            case["item_ok"] = bool(prod and prod.get("price") is not None)
        # tier 1: the result URL as an HTTP GET, its list against the replay's
        tmpl = tier1_template(rv.get("search_url") or "", v)
        case["tier1_template"] = tmpl
        if tmpl and items and ctx.left() >= 3:
            url = tmpl.replace("{q}", urllib.parse.quote_plus(v))
            if ctx.allowed(url):
                g = ctx.fetch.get(url)
                got = [str(x) for x in extract(g.body or "", selector, limit=40)] if g.status == 200 else []
                case["tier1"], case["tier1_why"] = W.labels_agree(got, items)
                case["tier1_status"] = g.status
        # the audit's evidence: the saved page text of this validation value
        page_txt = next((p.get("text") for p in rv.get("pages") or []), "")
        with open(os.path.join(out_dir, f"proof_{rec.get('task')}_{re.sub(r'[^a-z0-9]+', '_', v.lower())[:30]}.json"), "w") as fh:
            json.dump({"value": v, "items": items[:10], "pages": rv.get("pages"), "final": rv.get("final"),
                       "case": case}, fh, ensure_ascii=False, default=str)
        cases.append(case)
    res["cases"] = cases
    passed = [c for c in cases if c.get("tier2") and (kind != "item" or c.get("item_ok"))]
    split.finalize()
    res["held_out"] = split.held_out
    res["tier2"] = "proved" if len(passed) >= 2 and split.held_out else "failed"
    res["tier1"] = "proved" if res["tier2"] == "proved" and all(c.get("tier1") for c in cases) else "failed"
    res["status"] = "proved" if res["tier2"] == "proved" else "failed"
    res["model_calls"] = 0
    if res["status"] != "proved":
        res["why"] = "; ".join(f"{c['value']!r}: {c.get('contract') or c.get('why')} changed={c.get('changed')}"
                               f" item_ok={c.get('item_ok')}" for c in cases)[:400]
    return res


def compiled_repeat(arm: Arm, ctx: SiteCtx, rec: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    from anyuse.execution import web_arch_run as RR
    Session = session_class()
    settle = settle_fn(arm.endpoint)
    prog = rec.get("program") or {}

    async def go():
        async with Session(arm.endpoint, allow_url=ctx.allowed, budget=ctx.budget) as page:
            return await RR.replay(page, prog.get("ops") or [], settle=settle, by_rule=False)
    rep = asyncio.run(go())
    rep["start_url"] = rec.get("start_url")
    rep["task"] = task.get("id")
    j = W.judge_run(task, rep, arm.reader)
    return {"task": task.get("id"), "judge": j, "model_calls": rep.get("model_calls", 0),
            "stop": rep.get("stop"), "answer": rep.get("answer"), "compilable": prog.get("compilable"),
            "why": prog.get("why"), "seconds": rep.get("seconds"), "blocked": is_blocked(rep)}


# ---------------------------------------------------------------------------
# 6. the run: one arm over one half of the sites
# ---------------------------------------------------------------------------

def take_reserve(root: str, stratum: str, replaced: str) -> Optional[str]:
    p = R_(root, "runs", "reserves_used.json")
    c = load_json(R_(root, "screen", "cohort.json"), {})
    with locked(p):
        used = load_json(p, {})
        for site in (c.get("reserves") or {}).get(stratum) or []:
            if site in used or refusal(site):
                continue
            used[site] = {"for": replaced, "at": time.strftime("%H:%M:%S")}
            save_json(p, used)
            return site
    return None


def run_site(arm: Arm, site: str, tasks: List[Dict[str, Any]], values: List[str], out: str, gate,
             state: Dict[str, Any], write) -> str:
    """Returns "blocked" when the site blocked us (replace it), else ""."""
    e = state["entries"].setdefault(site, {"site": site, "arm": arm.arm, "status": "started",
                                           "started": time.strftime("%H:%M:%S")})
    write()
    used, used_bytes = site_total(arm.root, site)
    share = share_for(arm.arm, used)
    nbytes = max(0, min(BYTES[arm.arm], 30 * 1024 * 1024 - used_bytes - 512 * 1024))
    e.update(share=share, prior_requests=used, prior_bytes=used_bytes)
    if share < MIN_TASK_LEFT or nbytes < 1024 * 1024:
        e.update(status="done", outcome="share_exhausted_before_start")
        write()
        return ""
    ctx = SiteCtx(site, arm.arm, share, nbytes, arm.endpoint, gate)
    e["cache_loaded"] = load_cache(ctx.budget, site)
    sdir = R_(out, re.sub(r"[^a-z0-9]+", "_", site))
    os.makedirs(sdir, exist_ok=True)
    start = f"https://{site}/"
    blocked = ""
    try:
        with gate.task_sync(site):
            ctx.robots.load(site)
            e["robots"] = ctx.robots.notes.get(site)
            if str(e["robots"]).startswith("robots.txt 40") or "blocked" in str(e["robots"]):
                blocked = f"robots.txt: {e['robots']}"
            elif not ctx.robots.allow(start):
                e.update(status="done", outcome="robots_start")
                return ""
            import live_bench as V
            d = V.crawl_delay(ctx.robots, site)
            if d > 1.0:
                ctx.pacer.intervals[site] = d
                e["crawl_delay"] = d
            e["tasks"] = []
            succ: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            pending: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for t in tasks if not blocked else []:
                if arm.arm == "arch" and past_guard(arm):
                    e["tasks"].append({"task": t["id"], "outcome": "not_run_pod_time"})
                    e["pod_guard"] = time.strftime("%H:%M:%S")
                    continue
                if ctx.left() < MIN_TASK_LEFT:
                    e["tasks"].append({"task": t["id"], "outcome": "share_exhausted", "left": ctx.left()})
                    continue
                if arm.arm == "sonnet_agent" and not arm.led(arm.brain.kind):
                    e["tasks"].append({"task": t["id"], "outcome": "not_run_cap"})
                    state["cap_reached"] = True
                    continue
                arm.log(f"{site:30s} {arm.arm} {t['id']} {t['kind']}: {t['utterance'][:70]}")
                rec = arm.run_task(ctx, t, start, R_(sdir, "shots"))
                ctx.budget.check()
                blk = is_blocked(rec)
                j = W.judge_run(t, rec, arm.reader) if not blk else {"verdict": "blocked", "why": blk}
                rec["judge"] = j
                doms = rec.pop("doms", None) or {}
                with open(R_(sdir, f"{t['id']}.json"), "w") as fh:
                    json.dump(rec, fh, ensure_ascii=False, default=str)
                if doms.get("after_search"):
                    rec["doms"] = doms                    # kept in memory for the proof
                row = {"task": t["id"], "kind": t["kind"], "verdict": j.get("verdict"), "why": str(j.get("why"))[:200],
                       "answer_verdict": j.get("answer_verdict"), "wrong_data": j.get("wrong_data"),
                       "forbidden": j.get("forbidden"), "stop": rec.get("stop"), "answer": str(rec.get("answer"))[:200],
                       "model_calls": rec.get("model_calls"), "cost": round(float(rec.get("cost") or 0.0), 5),
                       "seconds": rec.get("seconds_wall"), "requests_after": rec.get("requests_after")}
                e["tasks"].append(row)
                arm.log(f"{site:30s} {arm.arm} {t['id']} -> {row['verdict']} ({row['why'][:80]}) "
                        f"stop={row['stop']} req={row['requests_after']}/{share}")
                write()
                if blk:
                    blocked = blk
                    break
                if j.get("verdict") == "infrastructure":
                    raise RuntimeError(f"infrastructure: {j.get('why')}")
                if j.get("verdict") == "ok":
                    succ.append((t, rec))
                elif j.get("verdict") == "pending_reader":
                    # judged later from the saved page (the reader was not reachable); provisional:
                    # a proof or a compiled repeat built on it counts only if the re-judged run is ok
                    pending.append((t, rec))
            # the proof: the first successful run that searched
            if not blocked:
                cand = next(((t, r) for t, r in succ if W.search_value(r.get("program") or {})), None) or \
                    next(((t, r) for t, r in pending if W.search_value(r.get("program") or {})), None)
                if cand is None:
                    e["proof"] = {"status": "no_tool", "why": "no successful run with a search"}
                elif ctx.left() < MIN_PROOF_LEFT:
                    e["proof"] = {"status": "share_exhausted", "left": ctx.left()}
                elif arm.arm == "arch" and past_guard(arm):
                    e["proof"] = {"status": "not_run_pod_time"}
                else:
                    e["proof"] = prove(arm, ctx, cand[1], values, sdir)
                    e["proof"]["provisional"] = cand in pending
                    ctx.budget.check()
                    if e["proof"].get("status") == "blocked":
                        blocked = str(e["proof"].get("why"))
                arm.log(f"{site:30s} {arm.arm} proof -> {(e.get('proof') or {}).get('status')} "
                        f"{str((e.get('proof') or {}).get('why') or '')[:100]}")
            # the compiled repeat of every successful architecture task (no model)
            if arm.arm == "arch" and not blocked:
                e["compiled"] = []
                for t, r in succ + pending:
                    if past_guard(arm):
                        e["compiled"].append({"task": t["id"], "outcome": "not_run_pod_time"})
                        continue
                    if ctx.left() < 8:
                        e["compiled"].append({"task": t["id"], "outcome": "share_exhausted"})
                        continue
                    cr = compiled_repeat(arm, ctx, r, t)
                    ctx.budget.check()
                    e["compiled"].append(cr)
                    arm.log(f"{site:30s} compiled {t['id']} -> {cr['judge'].get('verdict')} calls={cr['model_calls']}")
                    if cr.get("blocked"):
                        blocked = cr["blocked"]
                        break
    except TrafficViolation:
        raise
    finally:
        e["requests"] = ctx.used()
        e["bytes"] = ctx.nbytes()
        e["static_skipped"] = dict(getattr(ctx.budget, "static_skipped", {}) or {})
        e["refused_methods"] = [list(x) for x in ctx.budget.refused_methods[:20]]
        e["third_party_refused"] = len(ctx.budget.third_party_refused)
        e["violations"] = list(ctx.budget.violations)
        e["web_scope"] = ctx.scope.site_report(site) if hasattr(ctx.scope, "site_report") else {}
        e["min_gap_documents_s"] = _min_doc_gap(ctx.budget, site)
        e["static_peak"] = ctx.budget.static_peak
        # what the site's count is made of (report and diagnosis): request types and the most
        # frequent paths the guard sent
        e["sent_types"] = dict(Counter(ty for (_h, _t, _u, ty) in ctx.budget.sent))
        e["sent_top"] = Counter(urllib.parse.urlsplit(str(u)).path[:60] for (_h, _t, u, _ty)
                                in ctx.budget.sent).most_common(8)
        book_site(arm.root, site, arm.arm, e["requests"], e["bytes"])
        try:
            e["cache_saved"] = save_cache(ctx.budget, site)
        except Exception:                              # noqa: BLE001
            pass
        e["cache_hits"] = dict(ctx.budget.cache_hits)
        write()
    if blocked:
        ctx.fetch.blocked[site] = blocked
        add_never_contact(site, blocked)
        e.update(status="done", outcome="blocked", blocked=blocked[:200])
        write()
        return "blocked"
    e.update(status="done", outcome="done")
    write()
    return ""


def _min_doc_gap(budget, site: str) -> Optional[float]:
    last, gap = None, None
    for h, t, _u, ty in budget.sent:
        if ty == "Document" and budget.site_of(h) == budget.site_of(site):
            if last is not None:
                gap = min(gap if gap is not None else 1e9, round(t - last, 3))
            last = t
    return gap


def load_tasks() -> Dict[str, Any]:
    return load_json(TASKS_FILE, {})


def cmd_run(args) -> int:
    from anyuse.execution.traffic_gate import SiteGate
    root = os.path.abspath(args.root)
    dev = bool(args.dev)
    out = R_(root, "dev" if dev else "runs", f"{args.arm}_{args.half}" if not dev else f"{args.arm}_dev")
    os.makedirs(out, exist_ok=True)
    logf = open(R_(out, "run.log"), "a")

    def log(msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()
    head = subprocess.run(["git", "-C", os.path.dirname(os.path.dirname(HERE)), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    state_path = R_(out, "state.json")
    state = load_json(state_path, None) or {"spec": SPEC_ID, "arm": args.arm, "half": args.half, "code": head,
                                           "entries": {}, "queue_done": [], "ledger": [],
                                           "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    state["code_now"] = head

    def write():
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_json(state_path, state)
    arm = Arm(root, args.arm, args.browser, log, dev=dev)
    arm.stop_at = float(args.stop_at or 0)
    tasks_all = load_tasks()
    if dev:
        dev_tasks = load_json(R_(root, "dev", "dev_tasks.json"), {})
        queue = [args.dev]
        tasks_of = lambda s: dev_tasks.get(s, {}).get("tasks") or []      # noqa: E731
        values_of = lambda s: dev_tasks.get(s, {}).get("values") or []    # noqa: E731
    else:
        if not tasks_all.get("sites"):
            raise SystemExit("refused: the frozen task file the measurement harness is missing")
        c = load_json(R_(root, "screen", "cohort.json"), {})
        queue = [s for s in c["order"] if c["half"].get(s) == args.half] + \
            [l["in"] for l in state["ledger"] if l.get("in") and l.get("half") == args.half]
        if args.arm != "baseline":
            # the reserves the baseline phase took for this half run in every later arm
            base = load_json(R_(root, "runs", f"baseline_{args.half}", "state.json"), {}) or {}
            for l in base.get("ledger") or []:
                if l.get("in") and l["in"] not in queue:
                    queue.append(l["in"])
            # a site the baseline phase filed as infrastructure is discarded: not re-run
            discarded = {s for s, e in (base.get("entries") or {}).items() if e.get("outcome") == "infrastructure"}
            for s in [s for s in queue if s in discarded]:
                queue.remove(s)
                state["entries"].setdefault(s, {"site": s, "status": "done", "outcome": "skipped",
                                                "why": "discarded in the baseline phase (infrastructure); not re-run"})
        tasks_of = lambda s: tasks_all["sites"].get(s, {}).get("tasks") or []     # noqa: E731
        values_of = lambda s: tasks_all["sites"].get(s, {}).get("values") or []   # noqa: E731
    gate = SiteGate(per_site=1, max_sites=1)
    from anyuse.execution.browser_supervisor import BrowserSupervisor, ContainerController, ContainerSpec
    controller = None
    if args.container:
        port = urllib.parse.urlsplit(args.browser).port
        controller = ContainerController(ContainerSpec(name=args.container, memory="3g", cpus=2,
                                                       publish=f"127.0.0.1:{port}:9222"))
    sup = BrowserSupervisor(args.browser, container=controller, log=log)
    stop_before = float(args.stop_at or 0)
    try:
        while queue:
            site = queue.pop(0)
            if (stop_before or arm.arm == "arch") and past_guard(arm):
                log(f"STOP GUARD: the pod stops soon; stopping before {site}")
                state["stopped_by_guard"] = time.strftime("%H:%M:%S")
                break
            prior = state["entries"].get(site) or {}
            if prior.get("status") == "done":
                continue
            if prior.get("status") == "started":
                prior.update(status="done", outcome="infrastructure",
                             why="the run stopped mid-site; not re-run (spec §7)")
                write()
                continue
            why = refusal(site) or ("" if dev or site not in dev_sites(root) else "a DEV site (never measured)")
            if why:
                state["entries"][site] = {"site": site, "status": "done", "outcome": "skipped", "why": why}
                write()
                continue
            ts = tasks_of(site)
            if not ts:
                state["entries"][site] = {"site": site, "status": "done", "outcome": "no_tasks"}
                write()
                continue
            try:
                sup.require(site)
            except Exception as exc:                    # noqa: BLE001
                log(f"{site:30s} browser down before any request: {exc}; stopping (exit 4)")
                return 4
            m0, w0 = time.monotonic(), time.time()
            try:
                res = run_site(arm, site, ts, values_of(site), out, gate, state, write)
            except TrafficViolation:
                raise
            except Exception as exc:                    # noqa: BLE001
                from anyuse.execution.browser_supervisor import confirm
                infra = confirm(exc, args.browser)
                e = state["entries"].setdefault(site, {})
                e.update(status="done", outcome="infrastructure", why=f"{type(exc).__name__}: {exc}"[:240])
                write()
                log(f"{site:30s} INFRASTRUCTURE mid-site (not re-run): {exc}")
                if infra is None and not str(exc).startswith("infrastructure"):
                    raise
                res = ""
            finally:
                try:
                    sup.after_site(site)
                except Exception:                       # noqa: BLE001
                    pass
            slept = (time.time() - w0) - (time.monotonic() - m0)
            if slept > 30:
                state["entries"][site]["host_sleep_s"] = round(slept, 1)
                log(f"{site:30s} HOST SLEEP {slept:.0f} s during the site (reported)")
            if res == "blocked" and arm.arm == "baseline" and not dev:
                stratum = (load_json(R_(root, "screen", "screen.json"), {}).get("sites", {}).get(site) or {}).get("stratum")
                nxt = take_reserve(root, stratum or "shop", site)
                state["ledger"].append({"out": site, "in": nxt, "half": args.half, "at": time.strftime("%H:%M:%S")})
                if nxt:
                    queue.append(nxt)
                log(f"{site:30s} BLOCKED -> replaced by {nxt}")
                write()
            if state.get("cap_reached") and arm.arm == "sonnet_agent":
                log("sonnet_agent cap reached: the remaining tasks are not run (reported)")
    except TrafficViolation as exc:
        state["traffic_violation"] = str(exc)[:400]
        write()
        log(f"TRAFFIC VIOLATION: {exc}")
        return 3
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write()
    log("finished")
    return 0


# ---------------------------------------------------------------------------
# 7. the report
# ---------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return round(100 * (c - r) / d, 1), round(100 * (c + r) / d, 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("frame", "screen", "cohort", "run", "report"))
    ap.add_argument("root")
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--half", default="A")
    ap.add_argument("--browser", default="")
    ap.add_argument("--container", default="")
    ap.add_argument("--dev", default="")
    ap.add_argument("--stop-at", default="", help="epoch seconds: stop at a site boundary after this")
    a = ap.parse_args(argv)
    if a.cmd == "frame":
        return cmd_frame(a.root)
    if a.cmd == "screen":
        os.makedirs(R_(a.root, "dev" if a.dev else "screen"), exist_ok=True)
        return cmd_screen(a.root, dev=bool(a.dev))
    if a.cmd == "cohort":
        return cmd_cohort(a.root)
    if a.cmd == "run":
        if not a.arm or not a.browser:
            raise SystemExit("run needs --arm and --browser")
        return cmd_run(a)
    if a.cmd == "report":
        import web_arch_report
        return web_arch_report.main(a.root)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
