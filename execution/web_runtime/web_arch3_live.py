"""The web architecture, live, rebuilt on ``web_arch2_live``.

usage: web_arch3_live.py frame  <R>                      offline: the frame from screen/hosts_raw.tsv
       web_arch3_live.py screen <R> [--dev x]            robots.txt + ONE home-page GET per host, frame order
       web_arch3_live.py cohort <R>                      offline: main sites, reserves, author inputs
       web_arch3_live.py run    <R> --arm A --half H --browser URL [--stop-at EPOCH]

``web_arch_live`` and ``web_arch2_live`` proved 0 of 22 reachable sites each; in ``web_arch2_live`` the
architecture was below the plain Cerebras agent on runs (59 % vs 69 %). What this bench changes (each a
generic fix with fakes-only tests):

1. the proof finds script-rendered result lists: the SITE static cache (``RequestBudget.
   site_cache``: scripts and stylesheets kept for the site's whole session although they set
   a cookie or give no lifetime; never sent again, so never counted), an explicit proof share
   (the runs may not spend it), a byte floor per run, and the proof's tiers in order: tier 1
   = the HTTP GET of the result URL template (compile + fitting derive the list, the held-out
   values validate it); when HTTP cannot reproduce the list, tier 2 = the browser replay with
   a full render (the result wait) on the same values;
2. proof values are ITEM-level terms that result titles contain (``item_terms``: words of the
   product and article titles on the site's start page), drawn before any arm, never tuned
   on validation (``proof_split``);
3. the goal's gate before the plan (multilingual lexicon OR the model check);
4. never refuse a read-only task because the search box was not found (the site's search URL,
   navigation by the words);
5. a category page is not a product page (continue to the item);
6. richer picker options, the pairwise tie-break, the rule recalibrated on seen data;
7. the arch-specific fixes for the plain agent's lead: the result
   wait after a search, no shadow model calls (confirmer, screenshot picker), a stricter
   search judge for every arm (the start page is never a result page).

Arms: ``baseline`` (the Cerebras-only agent, unchanged) first, then ``arch`` (planner -> map
-> OpenJev picker -> guarded actions). Traffic exactly as approved for benchmarks.
"""
from __future__ import annotations

import argparse
import asyncio
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

import web_arch_live as L  # noqa: E402
import web_arch2_live as L2  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution.cdp_guard import TrafficViolation  # noqa: E402

SPEC_ID = "SPEC_WEB_ARCH3.md"
SEED = "anyuse-web-arch3-2026-11-03"
UA = L.UA
NEVER_CONTACT = L.NEVER_CONTACT
CANDIDATES = os.path.join(HERE, "web_arch3_candidates.tsv")
TASKS_FILE = os.path.join(HERE, "web_arch3_tasks.json")
ARMS = ("baseline", "arch")
SITE_CAP = 300
SHARES = {"baseline": 120, "arch": 170}
#: bytes per arm (the site's 30 MB: the screen, the baseline, the arch and in-flight overshoot)
BYTES = {"baseline": 11 * 1024 * 1024, "arch": 14 * 1024 * 1024}
#: the part of an arm's share the RUNS may not spend before the proof ran
PROOF_RESERVE = {"baseline": 28, "arch": 40}
PROOF_BYTES = {"baseline": 2 * 1024 * 1024, "arch": 3 * 1024 * 1024}
#: a run is not started with fewer bytes left than this (74 runs once started on a dead
#: page after the byte ceiling was reached)
MIN_RUN_BYTES = int(1.5 * 1024 * 1024)
#: statics stop at this share of the byte ceiling (the in-flight overshoot stays inside it)
STATIC_BYTES_HEADROOM = 0.85
STATIC_RESERVE_FRAC = 0.5
SKIP_TYPES = ("Image", "Media", "Font")
MARGIN = 4
RUNS = 3
MIN_RUN_LEFT = 10
MIN_PROOF_LEFT = 12
MIN_REPEAT_LEFT = 8
N_SCREEN = {"shop": 36, "other": 24}
MAIN = {"shop": 14, "other": 9}
MIN_ELIGIBLE = {"shop": 20, "other": 13}
SCREEN_EXTRA_MAX = 60
MAX_CRAWL_DELAY = 5.0
N_DEV = {"shop": 9, "other": 6}
MIN_POOL = 4
#: the bounded wait for a script-rendered result list after a search (local reads)
RESULT_WAIT_S = 6.0
#: the proof's tier 2 is a full render: the result wait of the replay
PROOF_RENDER_WAIT_S = 8.0
SCOPE = dict(L.SCOPE)
assert SCOPE["ANYUSE_ALLOW_READONLY_POST_SEARCH"] is True and SCOPE["ANYUSE_THIRD_PARTY_POLICY"] == "block"
TASK_ORDER = L2.TASK_ORDER
CACHE_DIR = "/tmp/anyuse-the measurement harness"


def R_(root: str, *p: str) -> str:
    return os.path.join(root, *p)


load_json, save_json, locked = L.load_json, L.save_json, L.locked
registrable = L.registrable
refusal = L.refusal


def add_never_contact(host: str, why: str) -> None:
    with locked(NEVER_CONTACT):
        with open(NEVER_CONTACT) as fh:
            lines = fh.read().splitlines()
        if host in {ln.strip() for ln in lines if not ln.startswith("#")}:
            return
        head = [ln for ln in lines if ln.startswith("#")]
        body = sorted({ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")} | {host})
        head.append(f"# 2026-11-03 (web-arch3): {host}: {why[:90]}")
        with open(NEVER_CONTACT, "w") as fh:
            fh.write("\n".join(head + body) + "\n")


def prior_hosts(root: str) -> set:
    return {ln.strip() for ln in open(R_(root, "screen", "prior_hosts_snapshot.txt")) if ln.strip()}


# ---------------------------------------------------------------------------
# 1. the frame (offline)
# ---------------------------------------------------------------------------

def build_frame(raw_rows, prior: set, nc: set, gov=None):
    old = L.SEED
    try:
        L.SEED = SEED
        return L.build_frame(raw_rows, prior, nc, gov)
    finally:
        L.SEED = old


def cmd_frame(root: str) -> int:
    raw = [ln.rstrip("\n").split("\t") for ln in open(R_(root, "screen", "hosts_raw.tsv")) if ln.strip()]
    raw = [(r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "") for r in raw]
    prior = prior_hosts(root)
    snap = R_(root, "screen", "never_contact_at_frame.txt")
    if not os.path.exists(snap):
        with open(snap, "w") as fh:
            fh.write("\n".join(sorted(L.never())) + "\n")
    nc = {ln.strip() for ln in open(snap) if ln.strip() and not ln.startswith("#")}
    frame, dropped = build_frame(raw, prior, nc)
    dev = [e for e in frame if e["stratum"] == "shop"][-N_DEV["shop"]:] + \
        [e for e in frame if e["stratum"] == "other"][-N_DEV["other"]:]
    frame = [e for e in frame if e not in dev]
    save_json(R_(root, "screen", "dev_sites.json"), dev)
    with open(CANDIDATES, "w") as fh:
        for e in frame:
            fh.write(f"{e['site']}\t{e['stratum']}\t{e['category']}\n")
    save_json(R_(root, "screen", "frame_dropped.json"), dropped)
    print(f"frame: {len(frame)} fresh ({sum(e['stratum'] == 'shop' for e in frame)} shop); DEV {len(dev)}; "
          f"dropped {({k: len(v) for k, v in dropped.items()})}")
    return 0


def load_frame() -> List[Dict[str, str]]:
    out = []
    for ln in open(CANDIDATES):
        if ln.strip():
            site, stratum, cat = ln.rstrip("\n").split("\t")
            out.append({"site": site, "stratum": stratum, "category": cat})
    return out


def dev_sites(root: str) -> set:
    return {e["site"] for e in load_json(R_(root, "screen", "dev_sites.json"), [])}


# ---------------------------------------------------------------------------
# 2. ITEM-level proof values (execution/web_runtime/web_arch3_values.py)
# ---------------------------------------------------------------------------

from web_arch3_values import GENERIC, draw_proof_values, item_terms, item_titles  # noqa: E402,F401


# ---------------------------------------------------------------------------
# 3. the screen: robots.txt + ONE home-page GET (+ the www/apex variant's robots.txt)
# ---------------------------------------------------------------------------

def variant_host(site: str) -> str:
    return site[4:] if site.startswith("www.") else "www." + site


def cmd_screen(root: str, dev: bool = False) -> int:
    import discover_bench as DBN
    import live_bench as V
    import site_screen as SS
    import value_screen as VS
    import query_values as VV
    frame = load_json(R_(root, "screen", "dev_sites.json"), []) if dev else load_frame()
    dst = R_(root, "dev", "screen.json") if dev else R_(root, "screen", "screen.json")
    out = load_json(dst, {"sites": {}, "order": []})

    def eligible(st):
        return sum(1 for s in out["sites"].values() if s.get("stratum") == st and s.get("screen") == "eligible")

    def screened(st):
        return sum(1 for s in out["sites"].values() if s.get("stratum") == st)
    extra = sum(1 for s in out["sites"].values() if s.get("extra"))
    prior = prior_hosts(root)
    prior_reg = {registrable(h) for h in prior}
    for e in frame:
        site, st = e["site"], e["stratum"]
        if site in out["sites"] or any((v.get("frame_host") == site) for v in out["sites"].values()):
            continue
        base = dev or screened(st) < N_SCREEN[st]
        more = (not base) and eligible(st) < MIN_ELIGIBLE[st] and extra < SCREEN_EXTRA_MAX
        if not (base or more):
            continue
        if more:
            extra += 1
        rec: Dict[str, Any] = {"site": site, "frame_host": site, "stratum": st, "category": e["category"],
                               "at": time.strftime("%H:%M:%S"), "extra": bool(more)}
        why = refusal(site) or ("" if dev or site not in dev_sites(root) else "a DEV site (never measured)") or \
            ("contacted by an earlier cohort" if site in prior or registrable(site) in prior_reg else "")
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
        if "redirect off the host" in str(rec["robots"]):
            # the site answers on its www / apex variant: its robots.txt there (one more request)
            alt = variant_host(site)
            if not refusal(alt) and alt not in prior:
                robots.load(alt)
                rec["robots_variant"] = robots.notes.get(alt)
                if robots.notes.get(alt) == "parsed" or str(robots.notes.get(alt)).startswith("no robots.txt"):
                    rec.update(site=alt, robots=robots.notes.get(alt), canonical_from=site)
                    site = alt
        start = f"https://{site}/"
        rec["start_url"] = start
        if str(rec["robots"]).startswith("robots.txt 40") or "blocked" in str(rec["robots"]):
            add_never_contact(site, f"web-arch3 screen: {rec['robots']}")
            rec["screen"] = "robots"
        elif not robots.allow(start):
            rec["screen"] = "robots"
        else:
            d = V.crawl_delay(robots, site)
            if d > 1.0:
                pacer.intervals[site] = d
                rec["crawl_delay"] = d
        if rec.get("screen") is None and float(rec.get("crawl_delay") or 0) > MAX_CRAWL_DELAY:
            rec["screen"] = "crawl_delay"
        elif rec.get("screen") is None:
            r = fetch.get(start)
            rec.update(status=r.status, final_url=r.final_url, error=r.error[:120], bytes=len(r.body or ""))
            blk = V.page_block(SS.text_of(r.body), r.final_url or start,
                               [{"resource_type": "Document", "url": r.final_url or start, "status": r.status}])
            if r.error.startswith("blocked") or blk or r.status in (401, 403, 429):
                why = blk or r.error or f"HTTP {r.status}"
                add_never_contact(site, f"web-arch3 screen: {why}")
                rec.update(screen="blocked", why=why[:160])
            elif r.status != 200 or D.host_of(r.final_url or start) != site or "html" not in (r.mime or ""):
                rec["screen"] = "no_page"
            else:
                url = r.final_url or start
                rec.update(VS.classify_safe(site, start, start, r.body, url,
                                             lambda dom: VV.listing_tokens(dom), min_tokens=VV.N_ELIGIBLE))
                rec["stratum_screen"] = rec.get("stratum")
                rec["stratum"] = st
                rec["shop"] = L.shop_signals(r.body)
                rec["snippets"] = L.snippets(r.body, url)
                rec["pool_names"] = L2.value_pool(r.body, url)
                rec["pool"] = item_terms(r.body, url)
                rec["item_titles"] = item_titles(r.body, url)[:30]
                tmpls = W.search_url_templates(r.body, url, re.findall(
                    r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.body or ""))
                rec["search_urls"] = [t["template"][:200] for t in tmpls[:3]]
                if rec.get("screen") == "no_search" and tmpls:
                    # the site's own search URL on its page is a search (the engine uses it)
                    rec["screen"] = "eligible"
                    rec["eligible_by"] = "search URL on the page (" + tmpls[0]["how"] + ")"
                if rec.get("screen") == "eligible":
                    sample = (rec["pool"] or ["test"])[0]
                    if tmpls and not (D.search_from_forms(D.scan_page(r.body or "", url))):
                        u = W.fill_search_url(tmpls[0]["template"], sample)
                        rec["search"] = {"how": "url_template", "url": u[:200], "ok": bool(robots.allow(u)),
                                         "template": tmpls[0]["template"][:200]}
                    else:
                        rec["search"] = L2.search_check(site, r.body, url, robots, sample)
                    if not rec["search"]["ok"]:
                        rec["screen"] = "robots_search"
                    elif len(rec["pool"]) < MIN_POOL:
                        rec["screen"] = "value_pool"
                if rec.get("screen") == "eligible":
                    os.makedirs(os.path.join(os.path.dirname(dst), "home"), exist_ok=True)
                    with open(os.path.join(os.path.dirname(dst), "home", f"{site}.html"), "w") as fh:
                        fh.write(r.body or "")
        rec["requests"] = int(sum(pacer.count.values()))
        if not dev:
            L.book_site(root, site, "screen", rec["requests"], int(rec.get("bytes") or 0))
        out["sites"][site] = rec
        out["order"].append(site)
        save_json(dst, out)
        print(f"{site:36s} {st:5s} {rec.get('screen')!s:14s} req={rec['requests']} "
              f"search={(rec.get('search') or {}).get('how')} pool={len(rec.get('pool') or [])}", flush=True)
    print({st: {"screened": screened(st), "eligible": eligible(st)} for st in ("shop", "other")})
    return 0


def cohort_from(screen: Dict[str, Any]) -> Dict[str, Any]:
    old = dict(L.MAIN)
    try:
        L.MAIN.clear()
        L.MAIN.update(MAIN)
        return L.cohort_from(screen)
    finally:
        L.MAIN.clear()
        L.MAIN.update(old)


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
# 4. one arm on one site: the guard (the site static cache, the byte headroom)
# ---------------------------------------------------------------------------

def share_for(arm: str, used: int) -> int:
    return max(0, min(SHARES[arm], SITE_CAP - used - MARGIN))


class SiteCtx(L2.SiteCtx):
    def __init__(self, site: str, arm: str, share: int, nbytes: int, endpoint: str, gate, browser_statics: bool = False,
                 search_post: bool = False, refuse_competitors: bool = True):
        super().__init__(site, arm, share, nbytes, endpoint, gate, browser_statics=browser_statics,
                         search_post=search_post, refuse_competitors=refuse_competitors)
        self.budget.site_cache = True
        self.budget.static_bytes_headroom = STATIC_BYTES_HEADROOM
        self.nbytes_cap = nbytes

    def bytes_left(self) -> int:
        return int(self.nbytes_cap - self.nbytes())


def run_left(ctx: SiteCtx, arm: str, proof_pending: bool) -> Tuple[int, int]:
    """(requests, bytes) the RUNS may still use: the proof's reserve is held back
    until the proof ran."""
    req = ctx.left() - (PROOF_RESERVE[arm] if proof_pending else 0)
    nb = ctx.bytes_left() - (PROOF_BYTES[arm] if proof_pending else 0)
    return req, nb


def load_cache(budget, site: str) -> int:
    old = L.CACHE_DIR
    try:
        L.CACHE_DIR = CACHE_DIR
        return L.load_cache(budget, site)
    finally:
        L.CACHE_DIR = old


def save_cache(budget, site: str) -> int:
    old = L.CACHE_DIR
    try:
        L.CACHE_DIR = CACHE_DIR
        return L.save_cache(budget, site)
    finally:
        L.CACHE_DIR = old


session_class = L2.session_class
settle = L2.settle


class Arm(L2.Arm):
    """``web_arch2_live``'s arm process with this bench's engine: the gate check, the result wait, the
    recalibrated picker; no shadow model calls (the confirmer and the screenshot
    picker were never acted on and doubled the model calls per run)."""

    def __init__(self, root: str, arm: str, endpoint: str, log, dev: bool = False):
        super().__init__(root, arm, endpoint, log, dev)
        self.picker_rule = W.load_picker_rule()
        if arm == "arch":
            ck = self.M.read_key("CEREBRAS_API_KEY")
            self.gate_check = self.M.make_gate_check(ck, self.spec, self.led)
            del ck
            self.confirmer = None

    def run_task(self, ctx, task: Dict[str, Any], start_url: str, shots_dir: str) -> Dict[str, Any]:
        if self.arm != "arch":
            return super().run_task(ctx, task, start_url, shots_dir)
        from anyuse.execution import web_arch_run as RR
        Session = session_class()
        holder: List[Any] = []
        t0 = time.monotonic()
        eng = RR.WebArch(self.planner, extractor=self.extractor, chooser=self.chooser, confirmer=None,
                         t2=self.t2, settle=settle(), spec=self.spec, shadow_images=False,
                         gate_check=self.gate_check, result_wait_s=RESULT_WAIT_S, picker_rule=self.picker_rule)

        async def go():
            async with Session(self.endpoint, allow_url=ctx.allowed, budget=ctx.budget) as page:
                holder.append(page)
                rec = await eng.run(page, dict(task, task_kind=task.get("kind")), start_url)
                rec["guard_failed"] = [list(f) for f in page.guard.failed[:80]]
                return rec
        rec = asyncio.run(go())
        rec["task_kind"] = task.get("kind")
        rec["fresh"] = [dict(getattr(s, "fresh_check", {}) or {}) for s in holder]
        rec["sessions"] = len(holder)
        rec["seconds_wall"] = round(time.monotonic() - t0, 2)
        rec["requests_after"] = ctx.used()
        rec["bytes_after"] = ctx.nbytes()
        rec["lanes"] = dict(ctx.budget.lanes.get(ctx.budget.site_of(ctx.site), {}))
        return rec


past_guard = L2.past_guard
is_blocked = L2.is_blocked


# ---------------------------------------------------------------------------
# 5. the proof: tier 1 (HTTP) first; tier 2 (the browser replay, full render) when HTTP
#    cannot reproduce the list; item-level held-out values
# ---------------------------------------------------------------------------

def proof_values(q0: str, values: Sequence[str]) -> List[str]:
    out = [v for v in values if W.fold(v) != W.fold(q0) and not (set(W.content_tokens(v)) & set(W.content_tokens(q0)))]
    return out[:3]


def recording(dom: str, value: str, url: str, before: str = "", text: str = "") -> Dict[str, Any]:
    """A proof recording for ``derive_ground_truth``: the page, its value, the page
    before the search (typing-region and result-region models) and its text (the
    no-list diagnosis reads it; left empty, every failure reads
    'not_rendered')."""
    r = {"dom": dom, "param": {"name": "q", "value": value}, "final_url": url, "ui": {"text": text}}
    if before:
        r["before_dom"] = before
    return r


def text_of(html: str) -> str:
    import site_screen as SS
    return SS.text_of(html or "")


def search_url_of(prog: Dict[str, Any], rec: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """(the result URL template with ``{q}``, how) of a compiled search, or ("", why): the
    site's search URL the run used, else the first page the run reached after its search
    whose URL carries the value (the result URL; DEV toom.de: an agent's record has no
    ``url_after``, its result page /s/hochbeet does)."""
    op = next((o for o in prog.get("ops") or [] if o.get("op") == "search"), None)
    if not op:
        return "", "no search op"
    if op.get("via") == "url" and op.get("template"):
        return str(op["template"]), "the site's search URL"
    v = str(op.get("value") or "")
    urls = [str(op.get("url_after") or "")]
    if rec:
        urls += [str(p.get("url") or "") for p in rec.get("pages") or []]
        urls.append(str((rec.get("final") or {}).get("url") or ""))
    for u in urls:
        t = L.tier1_template(u, v) if u else ""
        if t and W._same_site(urllib.parse.urlsplit(t.replace("{q}", "q")).hostname or "",
                              urllib.parse.urlsplit(str(prog.get("start_url") or "")).hostname or ""):
            return t, "the result URL"
    return "", "no result URL carries the value"


def url_tool(tmpl: str) -> Dict[str, Any]:
    """The search tool as the site's result URL (no start page, no typing): the proof's
    tier 2 replays it with a full render when HTTP cannot reproduce the list."""
    return {"kind": "search", "ops": [{"op": "search", "via": "url", "template": tmpl, "value": "{q}"}],
            "param": "q"}


def wire_bytes(g) -> int:
    """The bytes an HTTP GET moved: its Content-Length when the response states one, else
    the body's gzip size when it came compressed (httpx decodes it), else the body's size
    (the HTTP GETs count too; on toom.de decoded sizes over-counted 3x)."""
    import gzip
    h = {str(k).lower(): str(v) for k, v in (getattr(g, "headers", None) or {}).items()}
    raw = (getattr(g, "body", "") or "").encode("utf-8", "ignore")
    try:
        if h.get("content-length"):
            return int(h["content-length"])
    except ValueError:
        pass
    if h.get("content-encoding", "").strip().lower() in ("gzip", "br", "deflate", "zstd"):
        return len(gzip.compress(raw, 6))
    return len(raw)


def tier1(ctx: SiteCtx, tmpl: str, q0: str, fit_v: str, validation: Sequence[str], out_dir: str,
          task_id: str) -> Dict[str, Any]:
    """Tier 1: the compiled HTTP tool (GET of the template) on the compile + fitting
    values (the list's selector), then the held-out values (the contract)."""
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth
    res: Dict[str, Any] = {"template": tmpl}
    if not tmpl:
        res.update(status="no_template")
        return res

    def get(v: str):
        u = tmpl.replace("{q}", urllib.parse.quote_plus(v))
        if not ctx.allowed(u):
            return u, None
        g = ctx.fetch.get(u)
        ctx.budget.add_bytes(ctx.site, wire_bytes(g))
        return u, g
    pages = []
    for v in (q0, fit_v):
        if ctx.left() < 3:
            res.update(status="share_exhausted")
            return res
        u, g = get(v)
        if g is None:
            res.update(status="robots_refused", url=u[:200])
            return res
        if g.status != 200 or not g.body:
            res.update(status="http_error", why=f"HTTP {g.status} {g.error[:80]}", url=u[:200])
            return res
        pages.append((v, u, g.body))
    recs = [recording(b, v, u, text=text_of(b)[:20000]) for v, u, b in pages]
    selector = derive_ground_truth(recs)
    res["selector"] = selector
    if not selector:
        res.update(status="no_list", why=str(recs[0].get("recorder_warning") or recs[1].get("recorder_warning"))[:200])
        return res
    compile_items = [str(x) for x in extract(pages[0][2], selector, limit=30)]
    res["compile_items"] = compile_items[:10]
    cases = []
    for v in validation:
        if ctx.left() < 2:
            cases.append({"value": v, "ok": False, "why": "arm share exhausted"})
            continue
        u, g = get(v)
        body = (g.body or "") if g is not None and g.status == 200 else ""
        items = [str(x) for x in extract(body, selector, limit=30)] if body else []
        txt = text_of(body)
        ok, why = W.list_contract(items, v, txt[:6000])
        changed = W.values_changed(compile_items, items)
        case = {"value": v, "url": u[:200], "status": getattr(g, "status", None), "items": items[:10],
                "n_items": len(items), "contract": why, "changed": changed, "ok": bool(ok and changed)}
        with open(os.path.join(out_dir, f"proof_{task_id}_t1_{re.sub(r'[^a-z0-9]+', '_', v.lower())[:30]}.json"),
                  "w") as fh:
            json.dump({"tier": 1, "value": v, "items": items[:10], "url": u, "page_text": txt[:20000], "case": case},
                      fh, ensure_ascii=False, default=str)
        cases.append(case)
    res["cases"] = cases
    res["status"] = "proved" if len(cases) >= 2 and all(c.get("ok") for c in cases) else "failed"
    return res


def prove(arm: Arm, ctx: SiteCtx, rec: Dict[str, Any], values: List[str], out_dir: str) -> Dict[str, Any]:
    from anyuse.execution import proof_split as PS
    from anyuse.execution import web_arch_run as RR
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth
    prog = rec.get("program") or {}
    q0 = W.search_value(prog)
    res: Dict[str, Any] = {"task": rec.get("task"), "run": rec.get("run"), "arm": arm.arm, "compile_value": q0}
    if not q0:
        res.update(status="no_tool", why="the run made no search")
        return res
    vals = proof_values(q0, values)
    if len(vals) < 3:
        res.update(status="failed", why=f"fewer than 3 item values besides the compile value ({vals})")
        return res
    res["kind"] = "search"
    split = PS.assign([q0] + vals, n_selection=0, n_fitting=1)
    res["split"] = split.to_dict()
    fit_v = split.fitting[0]
    validation = list(split.validation)[:2]
    tmpl, how = search_url_of(prog, rec)
    # tier 2 replays the result URL when the run showed one (no start page, no typing race),
    # else the program up to its search
    tool = url_tool(tmpl) if tmpl else W.proof_tool(prog, "search")
    res["tool"] = tool
    res["template_how"] = how
    # tier 1: HTTP
    t1 = tier1(ctx, tmpl, q0, fit_v, validation, out_dir, str(rec.get("task")))
    res["tier1_detail"] = {k: v for k, v in t1.items() if k != "cases"}
    res["tier1_cases"] = t1.get("cases")
    split.note_fit([q0, fit_v])
    if t1.get("status") == "proved":
        split.finalize()
        res.update(tier1="proved", tier2="not_needed", status="proved", tier="tier1", held_out=split.held_out,
                   model_calls=0, cases=t1.get("cases"))
        return res
    res["tier1"] = "failed: " + str(t1.get("status"))
    # tier 2: the browser replay with a full render
    Session = session_class()
    st = settle()

    def replay(ops):
        async def go():
            async with Session(arm.endpoint, allow_url=ctx.allowed, budget=ctx.budget) as page:
                r = await RR.replay(page, ops, settle=st, by_rule=True, result_wait_s=PROOF_RENDER_WAIT_S)
                r["fresh"] = dict(page.fresh_check)
                return r
        return asyncio.run(go())
    before = (rec.get("doms") or {}).get("open") or ""
    compile_dom = (rec.get("doms") or {}).get("after_search") or ""
    compile_url = str(next((o.get("url_after") for o in prog.get("ops") or [] if o.get("op") == "search"), "") or "")
    if not compile_dom or ctx.left() < 6:
        if ctx.left() < 6:
            res.update(status="share_exhausted", why="no share left for the tier-2 replay")
            return res
        r0 = replay(W.bind(tool["ops"], q0))
        compile_dom, compile_url = (r0.get("doms") or {}).get("after_search") or "", r0.get("search_url") or ""
        before = before or (r0.get("doms") or {}).get("open") or ""
        res["compile_replayed"] = True
    if ctx.left() < 6:
        res.update(status="share_exhausted", why="no share left for the fitting replay")
        return res
    r_fit = replay(W.bind(tool["ops"], fit_v))
    if is_blocked(r_fit):
        res.update(status="blocked", why=is_blocked(r_fit))
        return res
    fit_dom = (r_fit.get("doms") or {}).get("after_search") or ""
    before = before or (r_fit.get("doms") or {}).get("open") or ""
    recs = [recording(compile_dom, q0, compile_url, before, text_of(compile_dom)[:20000]),
            recording(fit_dom, fit_v, r_fit.get("search_url") or "", before, text_of(fit_dom)[:20000])]
    selector = derive_ground_truth(recs) if compile_dom and fit_dom else ""
    visible = bool(recs[0].get("ground_truth_visible_only"))
    res["selector"] = selector
    res["fit"] = {"value": fit_v, "search_url": r_fit.get("search_url"), "stop": r_fit.get("stop"),
                  "has_dom": bool(fit_dom), "results_wait": [s.get("results_after_s") for s in r_fit.get("steps") or []
                                                             if "results_after_s" in s][:1]}
    if not selector:
        res.update(status="failed", why="tier 2: no result list derivable on the rendered compile + fitting pages "
                                        + str(recs[0].get("recorder_warning") or recs[1].get("recorder_warning") or ""))
        return res
    compile_items = [str(x) for x in extract(compile_dom, selector, limit=30, visible_only=visible)]
    res["compile_items"] = compile_items[:10]
    cases = []
    for v in validation:
        if ctx.left() < 6:
            cases.append({"value": v, "tier2": False, "why": "arm share exhausted"})
            continue
        rv = replay(W.bind(tool["ops"], v))
        blk = is_blocked(rv)
        if blk:
            res.update(status="blocked", why=blk)
            return res
        dom = (rv.get("doms") or {}).get("after_search") or ""
        items = [str(x) for x in extract(dom, selector, limit=30, visible_only=visible)] if dom else []
        text = (rv.get("final") or {}).get("text") or ""
        ok, why = W.list_contract(items, v, text[:6000])
        changed = W.values_changed(compile_items, items)
        case = {"value": v, "items": items[:10], "n_items": len(items), "contract": why, "changed": changed,
                "search_url": rv.get("search_url"), "tier2": bool(ok and changed and rv.get("stop") not in
                                                                  ("failed", "error")), "stop": rv.get("stop")}
        with open(os.path.join(out_dir, f"proof_{rec.get('task')}_{re.sub(r'[^a-z0-9]+', '_', v.lower())[:30]}.json"),
                  "w") as fh:
            json.dump({"tier": 2, "value": v, "items": items[:10], "pages": rv.get("pages"), "final": rv.get("final"),
                       "page_text": text[:20000], "case": case}, fh, ensure_ascii=False, default=str)
        cases.append(case)
    res["cases"] = cases
    passed = [c for c in cases if c.get("tier2")]
    split.finalize()
    res["held_out"] = split.held_out
    res["tier2"] = "proved" if len(passed) >= 2 and split.held_out else "failed"
    res["status"] = "proved" if res["tier2"] == "proved" else "failed"
    res["tier"] = "tier2" if res["status"] == "proved" else ""
    res["model_calls"] = 0
    if res["status"] != "proved":
        res["why"] = "; ".join(f"{c['value']!r}: {c.get('contract') or c.get('why')} changed={c.get('changed')}"
                               for c in cases)[:400]
    return res


def compiled_repeat(arm: Arm, ctx: SiteCtx, rec: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    from anyuse.execution import web_arch_run as RR
    Session = session_class()
    st = settle()
    prog = rec.get("program") or {}

    async def go():
        async with Session(arm.endpoint, allow_url=ctx.allowed, budget=ctx.budget) as page:
            r = await RR.replay(page, prog.get("ops") or [], settle=st, by_rule=False, result_wait_s=RESULT_WAIT_S)
            r["fresh"] = dict(page.fresh_check)
            return r
    rep = asyncio.run(go())
    rep["start_url"] = rec.get("start_url")
    rep["task"] = task.get("id")
    j = W.judge_run(task, rep, arm.reader)
    return {"task": task.get("id"), "from_run": rec.get("run"), "start_url": rec.get("start_url"),
            "judge": j, "model_calls": rep.get("model_calls", 0),
            "stop": rep.get("stop"), "answer": rep.get("answer"), "compilable": prog.get("compilable"),
            "why": prog.get("why"), "seconds": rep.get("seconds"), "blocked": is_blocked(rep),
            "final": rep.get("final"), "pages": rep.get("pages"), "net": rep.get("net")}


# ---------------------------------------------------------------------------
# 6. the run: one arm over one half of the sites
# ---------------------------------------------------------------------------

ordered = L2.ordered


def take_reserve(root: str, stratum: str, replaced: str) -> Optional[str]:
    return L.take_reserve(root, stratum, replaced)


def run_site(arm: Arm, site: str, tasks: List[Dict[str, Any]], values: List[str], out: str, gate,
             state: Dict[str, Any], write) -> str:
    """Returns "blocked" when the site blocked us (replace it), else ""."""
    e = state["entries"].setdefault(site, {"site": site, "arm": arm.arm, "status": "started",
                                           "started": time.strftime("%H:%M:%S")})
    write()
    used, used_bytes = L.site_total(arm.root, site)
    share = share_for(arm.arm, used)
    nbytes = max(0, min(BYTES[arm.arm], 30 * 1024 * 1024 - used_bytes - 3 * 1024 * 1024))
    e.update(share=share, prior_requests=used, prior_bytes=used_bytes, byte_cap=nbytes)
    if share < MIN_RUN_LEFT + 4 or nbytes < MIN_RUN_BYTES:
        e.update(status="done", outcome="share_exhausted_before_start")
        write()
        return ""
    ctx = SiteCtx(site, arm.arm, share, nbytes, arm.endpoint, gate)
    e["cache_loaded"] = load_cache(ctx.budget, site)
    sdir = R_(out, re.sub(r"[^a-z0-9]+", "_", site))
    os.makedirs(sdir, exist_ok=True)
    start = f"https://{site}/"
    blocked = ""
    succ: Dict[str, List[Dict[str, Any]]] = {}
    tasks = ordered(tasks)
    proof_state = {"pending": True}

    def try_proof(cands) -> None:
        nonlocal blocked
        if e.get("proof") and e["proof"].get("status") not in ("no_tool",):
            return
        cand = next(((t, r) for t, r in cands if W.search_value(r.get("program") or {})), None)
        if cand is None:
            e["proof"] = {"status": "no_tool", "why": "no successful run with a search"}
        elif ctx.left() < MIN_PROOF_LEFT:
            e["proof"] = {"status": "share_exhausted", "left": ctx.left()}
            proof_state["pending"] = False
        elif arm.arm == "arch" and past_guard(arm):
            e["proof"] = {"status": "not_run_pod_time"}
        else:
            e["proof"] = prove(arm, ctx, cand[1], values, sdir)
            e["proof"]["provisional"] = (cand[1].get("judge") or {}).get("verdict") == "pending_reader"
            e["proof"]["requests_after"] = ctx.used()
            proof_state["pending"] = False
            ctx.budget.check()
            if e["proof"].get("status") == "blocked":
                blocked = str(e["proof"].get("why"))
        arm.log(f"{site:30s} {arm.arm} proof -> {(e.get('proof') or {}).get('status')} "
                f"{(e.get('proof') or {}).get('tier') or ''} {str((e.get('proof') or {}).get('why') or '')[:100]}")
        write()
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
            done_search = False
            for t in tasks if not blocked else []:
                for k in range(1, RUNS + 1):
                    if arm.arm == "arch" and past_guard(arm):
                        e["tasks"].append({"task": t["id"], "run": k, "outcome": "not_run_pod_time"})
                        e["pod_guard"] = time.strftime("%H:%M:%S")
                        continue
                    req_left, bytes_left = run_left(ctx, arm.arm, proof_state["pending"])
                    if req_left < MIN_RUN_LEFT or bytes_left < MIN_RUN_BYTES:
                        e["tasks"].append({"task": t["id"], "run": k, "outcome": "share_exhausted",
                                           "left": ctx.left(), "bytes_left": ctx.bytes_left(),
                                           "proof_reserve_held": proof_state["pending"]})
                        continue
                    arm.log(f"{site:30s} {arm.arm} {t['id']} r{k} {t['kind']}: {t['utterance'][:70]}")
                    rec = arm.run_task(ctx, t, start, R_(sdir, "shots"))
                    rec["run"] = k
                    ctx.budget.check()
                    blk = is_blocked(rec)
                    j = W.judge_run(t, rec, arm.reader) if not blk else {"verdict": "blocked", "why": blk}
                    rec["judge"] = j
                    doms = rec.pop("doms", None) or {}
                    with open(R_(sdir, f"{t['id']}_r{k}.json"), "w") as fh:
                        json.dump(rec, fh, ensure_ascii=False, default=str)
                    if doms.get("after_search"):
                        rec["doms"] = doms
                    row = {"task": t["id"], "run": k, "kind": t["kind"], "verdict": j.get("verdict"),
                           "why": str(j.get("why"))[:200], "answer_verdict": j.get("answer_verdict"),
                           "wrong_data": j.get("wrong_data"), "forbidden": j.get("forbidden"),
                           "stop": rec.get("stop"), "answer": str(rec.get("answer"))[:200],
                           "model_calls": rec.get("model_calls"), "cost": round(float(rec.get("cost") or 0.0), 5),
                           "seconds": rec.get("seconds_wall"), "requests_after": rec.get("requests_after"),
                           "bytes_after": ctx.nbytes(),
                           "gate_by": (rec.get("gate_detect") or {}).get("by"),
                           "fresh": all((f or {}).get("fresh") for f in rec.get("fresh") or [{}])}
                    e["tasks"].append(row)
                    arm.log(f"{site:30s} {arm.arm} {t['id']} r{k} -> {row['verdict']} ({row['why'][:80]}) "
                            f"stop={row['stop']} req={row['requests_after']}/{share} MB={ctx.nbytes() / 1e6:.1f}")
                    write()
                    if blk:
                        blocked = blk
                        break
                    if j.get("verdict") == "infrastructure":
                        raise RuntimeError(f"infrastructure: {j.get('why')}")
                    if j.get("verdict") in ("ok", "pending_reader"):
                        succ.setdefault(t["id"], []).append(rec)
                if blocked:
                    break
                if t["kind"] == "search" and not done_search:
                    done_search = True
                    ok_runs = [(t, r) for r in succ.get(t["id"], []) if (r.get("judge") or {}).get("verdict") == "ok"]
                    try_proof(ok_runs)
                    if e.get("proof", {}).get("status") == "no_tool":
                        # no search run to prove yet: the reserve stays for another task's search
                        pass
                    if blocked:
                        break
            if not blocked and (not e.get("proof") or e["proof"].get("status") == "no_tool"):
                by_id = {t["id"]: t for t in tasks}
                others = [(by_id[tid], r) for tid in [t["id"] for t in tasks] for r in succ.get(tid, [])
                          if by_id[tid]["kind"] != "search" and (r.get("judge") or {}).get("verdict") == "ok"]
                if others or not e.get("proof"):
                    e["proof"] = None
                    try_proof(others)
            if arm.arm == "arch" and not blocked:
                e["compiled"] = []
                for t in tasks:
                    runs = succ.get(t["id"]) or []
                    if not runs:
                        continue
                    r = runs[0]
                    if past_guard(arm):
                        e["compiled"].append({"task": t["id"], "outcome": "not_run_pod_time"})
                        continue
                    if ctx.left() < MIN_REPEAT_LEFT or ctx.bytes_left() < MIN_RUN_BYTES // 2:
                        e["compiled"].append({"task": t["id"], "outcome": "share_exhausted"})
                        continue
                    cr = compiled_repeat(arm, ctx, r, t)
                    ctx.budget.check()
                    with open(R_(sdir, f"compiled_{t['id']}.json"), "w") as fh:
                        json.dump(cr, fh, ensure_ascii=False, default=str)
                    cr = {k: v for k, v in cr.items() if k not in ("final", "pages", "net")}
                    e["compiled"].append(cr)
                    arm.log(f"{site:30s} compiled {t['id']} -> {cr['judge'].get('verdict')} calls={cr['model_calls']}")
                    write()
                    if cr.get("blocked"):
                        blocked = cr["blocked"]
                        break
    except TrafficViolation:
        raise
    finally:
        e["requests"] = ctx.used()
        e["bytes"] = ctx.nbytes()
        e["lanes"] = dict(ctx.budget.lanes.get(ctx.budget.site_of(site), {}))
        e["noise_skipped"] = dict(ctx.budget.noise_skipped)
        e["static_skipped"] = dict(getattr(ctx.budget, "static_skipped", {}) or {})
        e["refused_methods"] = [list(x) for x in ctx.budget.refused_methods[:20]]
        e["third_party_refused"] = len(ctx.budget.third_party_refused)
        e["violations"] = list(ctx.budget.violations)
        e["web_scope"] = ctx.scope.site_report(site) if hasattr(ctx.scope, "site_report") else {}
        e["min_gap_documents_s"] = L._min_doc_gap(ctx.budget, site)
        e["static_peak"] = ctx.budget.static_peak
        e["sent_types"] = dict(Counter(ty for (_h, _t, _u, ty) in ctx.budget.sent))
        e["hosts_sent"] = dict(Counter(h for (h, _t, _u, _ty) in ctx.budget.sent))
        e["site_cache_overrides"] = dict(ctx.budget.site_cache_overrides)
        e["cache_refused"] = dict(ctx.budget.cache_refused)
        L.book_site(arm.root, site, arm.arm, e["requests"], e["bytes"])
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
                                           "entries": {}, "ledger": [],
                                           "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    state["code_now"] = head

    def write():
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_json(state_path, state)
    arm = Arm(root, args.arm, args.browser, log, dev=dev)
    arm.stop_at = float(args.stop_at or 0)
    if dev:
        dev_tasks = load_json(R_(root, "dev", "dev_tasks.json"), {})
        queue = [args.dev]
        tasks_of = lambda s: dev_tasks.get(s, {}).get("tasks") or []      # noqa: E731
        values_of = lambda s: dev_tasks.get(s, {}).get("values") or []    # noqa: E731
    else:
        tasks_all = load_tasks()
        if not tasks_all.get("sites"):
            raise SystemExit("refused: the frozen task file the measurement harness is missing")
        c = load_json(R_(root, "screen", "cohort.json"), {})
        queue = [s for s in c["order"] if c["half"].get(s) == args.half] + \
            [l_["in"] for l_ in state["ledger"] if l_.get("in") and l_.get("half") == args.half]
        if args.arm != "baseline":
            base = load_json(R_(root, "runs", f"baseline_{args.half}", "state.json"), {}) or {}
            for l_ in base.get("ledger") or []:
                if l_.get("in") and l_["in"] not in queue:
                    queue.append(l_["in"])
            discarded = {s for s, e in (base.get("entries") or {}).items()
                         if e.get("outcome") in ("infrastructure", "blocked")}
            for s in [s for s in queue if s in discarded]:
                queue.remove(s)
                state["entries"].setdefault(s, {"site": s, "status": "done", "outcome": "skipped",
                                                "why": "blocked or infrastructure in the baseline phase; "
                                                       "never re-contacted"})
        tasks_of = lambda s: tasks_all["sites"].get(s, {}).get("tasks") or []     # noqa: E731
        values_of = lambda s: tasks_all["sites"].get(s, {}).get("values") or []   # noqa: E731
    gate = SiteGate(per_site=1, max_sites=1)
    from anyuse.execution.browser_supervisor import BrowserSupervisor
    sup = BrowserSupervisor(args.browser, container=None, log=log)
    try:
        while queue:
            site = queue.pop(0)
            if arm.arm == "arch" and past_guard(arm):
                log(f"STOP GUARD: the pod stops soon; stopping before {site}")
                state["stopped_by_guard"] = time.strftime("%H:%M:%S")
                break
            prior = state["entries"].get(site) or {}
            if prior.get("status") == "done":
                continue
            if prior.get("status") == "started":
                prior.update(status="done", outcome="infrastructure",
                             why="the run stopped mid-site; not re-run (spec)")
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
    except TrafficViolation as exc:
        state["traffic_violation"] = str(exc)[:400]
        write()
        log(f"TRAFFIC VIOLATION: {exc}")
        return 3
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write()
    log("finished")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("frame", "screen", "cohort", "run"))
    ap.add_argument("root")
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--half", default="A")
    ap.add_argument("--browser", default="")
    ap.add_argument("--dev", default="")
    ap.add_argument("--stop-at", default="", help="epoch seconds: the pod guard fallback")
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
