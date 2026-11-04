"""The web architecture, live, rebuilt on ``web_arch_live``.

usage: web_arch2_live.py frame  <R>                      offline: the frame from screen/hosts_raw.tsv
       web_arch2_live.py screen <R> [--dev x]            robots.txt + ONE home-page GET per host, frame order
       web_arch2_live.py cohort <R>                      offline: main sites, reserves, author inputs
       web_arch2_live.py run    <R> --arm A --half H --browser URL [--dev SITE] [--stop-at EPOCH]

``web_arch_live`` proved 0 of 22 reachable sites, mostly on the
harness and the test design. What this bench changes (each a generic fix with fakes-only
tests):

1. traffic accounting: the bounded settle (``web_arch_run.settle_bounded``) and the
   noise lane (``RequestBudget(noise_share=True)``): the page's own background
   requests are counted against the static share, never the action share;
2. the page's own read-only POST search (``ANYUSE_ALLOW_READONLY_POST_SEARCH``) with the
   typed value as its evidence, for every arm;
3. task design: every eligible site gets a ``search`` task (plus ``answer_question`` on
   shops, plus a ``gate`` task), drawn per kind from two independent authors; the
   screen requires a search the site's robots.txt allows; proof values from the
   site's own item and category names (the value pool), not interface words;
4.-5. the map: the named item, not the first result; section goals are navigations;
   never an image-only link or a file as an item;
6. 3 runs per task, each in a NEW browser context checked empty (``fresh_context``);
7. the proof path: the first successful run that searched (the search task first)
   feeds the compile + held-out proof (``proof_split``: compile value + >= 3 pool values)
   right after the search task's runs, before the other tasks spend the share.

Arms: ``baseline`` (the existing Cerebras-only agent, ``agent_browser.StepAgent`` L1,
unchanged) and ``arch`` (``web_arch_run.WebArch``: planner -> map -> OpenJev picker ->
guarded actions; confirmer in shadow). No sonnet_agent arm here. The blind reader
is a local CLI run AFTER the arms: the judge defers every verdict that needs it
(``pending_reader``) and the records are re-judged from the saved pages.

Traffic (docs/TRAFFIC_POLICY.md, approved for benchmarks): <= 300 requests per site in
total over the screen and both arms (shares: baseline 120, arch 170), 1 req/s, >= 3 s
between documents, statics <= 4 in flight, <= 30 MB per site, UA ``research-probe/0.1
(read-only; person-paced)``, robots respected on every host, a block/challenge/403/429
stops the host for good and adds it to NEVER_CONTACT, no stealth, no CAPTCHA solving.
Government hosts (incl. public bodies), NEVER_CONTACT, competitor domains and every
host an earlier cohort contacted are never contacted.
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import anyuse  # noqa: E402
if os.path.realpath(os.path.dirname(anyuse.__file__)) != os.path.realpath(os.path.dirname(os.path.dirname(HERE))):
    raise SystemExit(f"refused: `anyuse` resolves to {anyuse.__file__}, not this checkout")

import web_arch_live as L  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution.cdp_guard import DOC_MIN_INTERVAL_S, REQUEST_MIN_INTERVAL_S, RequestBudget, TrafficViolation  # noqa: E402,E501

SPEC_ID = "SPEC_WEB_ARCH2.md"
SEED = "anyuse-web-arch2-2026-11-03"
UA = L.UA
NEVER_CONTACT = L.NEVER_CONTACT
CANDIDATES = os.path.join(HERE, "web_arch2_candidates.tsv")
TASKS_FILE = os.path.join(HERE, "web_arch2_tasks.json")
ARMS = ("baseline", "arch")
SITE_CAP = 300
#: the site's 300 split into fixed arm shares (the screen's <= 3 and the margin below)
SHARES = {"baseline": 120, "arch": 170}
BYTES = {"baseline": 12 * 1024 * 1024, "arch": 16 * 1024 * 1024}
#: half of a share is reserved for what the run's actions cause (documents, action data);
#: statics and the page's background noise use the other half (the static share)
STATIC_RESERVE_FRAC = 0.5
SKIP_TYPES = ("Image", "Media", "Font")
MARGIN = 4
#: runs per task (``web_arch_live``: one)
RUNS = 3
#: a run is not started with less than this left in the arm's share
MIN_RUN_LEFT = 10
#: a proof is not started with less than this left
MIN_PROOF_LEFT = 20
#: a compiled repeat is not started with less than this left
MIN_REPEAT_LEFT = 8
N_SCREEN = {"shop": 36, "other": 24}
MAIN = {"shop": 14, "other": 9}
#: screening continues in frame order while a stratum has fewer eligible sites than this
MIN_ELIGIBLE = {"shop": 20, "other": 13}
SCREEN_EXTRA_MAX = 90
MAX_CRAWL_DELAY = 5.0
#: development sites (fresh, removed from the frame before any contact; never measured)
N_DEV = {"shop": 3, "other": 2}
#: the value pool must hold at least this many names (compile + fitting + 2 validation)
MIN_POOL = 4
SCOPE = dict(L.SCOPE)          # every web-scope allowance of ``web_arch_live`` on, for every arm
assert SCOPE["ANYUSE_ALLOW_READONLY_POST_SEARCH"] is True and SCOPE["ANYUSE_THIRD_PARTY_POLICY"] == "block"
TASK_ORDER = ("search", "answer_question", "open_item", "navigate_in_site", "filter_sort", "gate")


def R_(root: str, *p: str) -> str:
    return os.path.join(root, *p)


load_json, save_json, locked = L.load_json, L.save_json, L.locked
registrable = L.registrable


def refusal(host: str) -> str:
    """'' or why no request may go to ``host`` (NEVER_CONTACT, government incl. public
    bodies when ANYUSE_REFUSE_GOV_HOSTS is on, a competitor's domain)."""
    return L.refusal(host)


def add_never_contact(host: str, why: str) -> None:
    with locked(NEVER_CONTACT):
        with open(NEVER_CONTACT) as fh:
            lines = fh.read().splitlines()
        if host in {ln.strip() for ln in lines if not ln.startswith("#")}:
            return
        head = [ln for ln in lines if ln.startswith("#")]
        body = sorted({ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")} | {host})
        head.append(f"# 2026-11-03 (web-arch2): {host}: {why[:90]}")
        with open(NEVER_CONTACT, "w") as fh:
            fh.write("\n".join(head + body) + "\n")


def prior_hosts(root: str) -> set:
    return {ln.strip() for ln in open(R_(root, "screen", "prior_hosts_snapshot.txt")) if ln.strip()}


# ---------------------------------------------------------------------------
# 1. the frame (offline)
# ---------------------------------------------------------------------------

def build_frame(raw_rows, prior: set, nc: set, gov=None):
    """``web_arch_live``'s filter (malformed, prior registrable domain, NEVER_CONTACT,
    government incl. public bodies, competitor, duplicate) with this bench's seed."""
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
    nc = {ln.strip() for ln in open(R_(root, "screen", "never_contact_at_frame.txt"))
          if ln.strip() and not ln.startswith("#")}
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
# 2. the screen: robots.txt + ONE home-page GET; the search check and the value pool
# ---------------------------------------------------------------------------

#: interface words: a link text holding one of these is not an item or category name
CHROME = frozenset("""
home homepage shop shops store stores sale sales new news about us contact help faq faqs blog shipping delivery
returns return gift gifts card cards voucher vouchers giftcard careers career jobs job privacy policy terms
conditions cookie cookies login logout account register menu more all view see back top next previous prev search
cart basket bag wishlist newsletter offers offer deals deal brands brand discover explore learn read trade press
affiliate affiliates sitemap accessibility imprint impressum datenschutz agb versand kontakt hilfe uber filiale
filialen karriere neu neuheiten angebote angebot marken alle mehr anmelden konto warenkorb merkliste rechtliches
widerruf zahlung lieferung livraison aide contactez nous mentions legales cgv compte panier nouveautes promotions
promo promos marques tout voir plus magasins magasin contacto ayuda envio cuenta carrito ofertas nuevo winkels
winkel klantenservice bezorging retourneren aanbiedingen nieuw merken inloggen winkelwagen kundtjanst leverans
erbjudanden nyheter varumarken butiker english deutsch francais espanol nederlands svenska dansk suomi norsk
facebook instagram twitter youtube pinterest tiktok linkedin x usd eur gbp chf sek nok dkk skip content main
subscribe sign signin signup log register open close today free service services customer support order orders
track tracking find locator location locations club membership rewards loyalty app apps download stories story
inspiration journal magazine guide guides guides press community events event reviews review rating ratings
mijn mitt mina mein meine mon mes bestellingen bestellungen commandes bestallningar ordrer tilaukset visa alla
alles alle bekijk toon vis fri frakt fria returer gratis verzending retour retouren privacybeleid confidentialite
integritetspolicy suivre actualite actualites nieuwsbrief nyhetsbrev nyhedsbrev member members mitglied medlem
membre werden worden bli wunschzettel erstellen verlanglijst onskelista mailing list aanmelden registreren
registrieren tilmeld bonus treueprogramm gutschein gutscheine cadeau cadeaux tilbud rea erbjudande
""".split())
#: interface word STEMS (compounds: "Bezorgbundel", "Versandkosten", "Datenschutzerklärung")
CHROME_STEMS = ("bezorg", "versand", "retour", "privacy", "datenschutz", "newsletter", "nieuwsbrief", "wunsch",
                "member", "mitglied", "treue", "gutschein", "kundenservice", "klantenservice", "kundtjanst")


def value_pool(body: str, url: str, limit: int = 30) -> List[str]:
    """The site's own item and category names on its start page (PURE): same-host
    link texts of 1-4 words, no interface word, not a file, not a gated control;
    in page order, case-folded duplicates removed. The proof's held-out values are
    drawn from these (single start-page tokens gave interface
    words such as 'Favorites')."""
    scan = D.scan_page(body or "", url)
    host = D.host_of(url)
    out, seen = [], set()
    for ln in scan.links:
        t = " ".join((ln.text or "").split())
        if not (3 <= len(t) <= 40) or D.host_of(ln.url) != host or W.asset_href(ln.url):
            continue
        try:
            path = urllib.parse.urlsplit(ln.url).path or "/"
        except ValueError:
            continue
        if path.strip("/") == "" or W.forbidden_control(t, ln.url):
            continue
        toks = W.tokens(t)
        ct = W.content_tokens(t)
        if not (1 <= len(toks) <= 4) or not ct or any(w in CHROME for w in toks):
            continue
        if t.startswith("#") or any(w.startswith(CHROME_STEMS) for w in toks):
            continue
        # the site's own name is not an item ("Breuninger" on www.breuninger.com)
        if all(w in set(re.split(r"[.-]", host.lower())) for w in ct):
            continue
        if all(w.isdigit() for w in ct) or not re.search(r"[^\W\d_]{3,}", t):
            continue
        k = W.fold(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def search_check(site: str, body: str, url: str, robots, sample: str) -> Dict[str, Any]:
    """Can the site's own search be used under its robots.txt? (PURE but for
    ``robots.allow``). A GET search form: the result URL for ``sample``; a POST
    search form: its action; a search box without a form (a script-driven search):
    the usual result paths. ``ok`` False = not eligible (``robots_search``)."""
    from anyuse.execution.web_scope import post_search_forms
    scan = D.scan_page(body or "", url)
    caps = sorted(D.search_from_forms(scan), key=lambda c: -c.score)
    if caps:
        tmpl = caps[0].template
        u = tmpl.replace("{v}", urllib.parse.quote_plus(sample or "test"))
        return {"how": "get_form", "url": u[:200], "ok": bool(robots.allow(u)), "template": tmpl[:200]}
    posts = post_search_forms(body or "", url)
    if posts:
        act = f"https://{posts[0].host}{posts[0].path}"
        return {"how": "post_form", "url": act[:200], "ok": bool(robots.allow(act))}
    q = urllib.parse.quote_plus(sample or "test")
    usual = [f"https://{site}/search?q={q}", f"https://{site}/search?query={q}",
             f"https://{site}/catalogsearch/result/?q={q}", f"https://{site}/?s={q}"]
    allowed = [u for u in usual if robots.allow(u)]
    return {"how": "script", "url": usual[0][:200], "ok": usual[0] in allowed,
            "allowed_paths": [urllib.parse.urlsplit(u).path for u in allowed]}


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
        if str(rec["robots"]).startswith("robots.txt 40") or "blocked" in str(rec["robots"]):
            add_never_contact(site, f"web-arch2 screen: {rec['robots']}")
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
                add_never_contact(site, f"web-arch2 screen: {why}")
                rec.update(screen="blocked", why=why[:160])
            elif r.status != 200 or D.host_of(r.final_url or start) != site or "html" not in (r.mime or ""):
                rec["screen"] = "no_page"
            else:
                rec.update(VS.classify_safe(site, start, start, r.body, r.final_url or start,
                                             lambda dom: VV.listing_tokens(dom), min_tokens=VV.N_ELIGIBLE))
                # classify's own "stratum" (get_form / other) must not replace the frame's (shop / other):
                # the screen's stop rule and the cohort count by the frame's stratum (otherwise
                # 186 hosts are screened where the rule stops at 95)
                rec["stratum_screen"] = rec.get("stratum")
                rec["stratum"] = st
                rec["shop"] = L.shop_signals(r.body)
                rec["snippets"] = L.snippets(r.body, r.final_url or start)
                rec["pool"] = value_pool(r.body, r.final_url or start)
                if rec.get("screen") == "eligible":
                    rec["search"] = search_check(site, r.body, r.final_url or start, robots,
                                                 (rec["pool"] or ["test"])[0])
                    if not rec["search"]["ok"]:
                        rec["screen"] = "robots_search"
                    elif len(rec["pool"]) < MIN_POOL:
                        rec["screen"] = "value_pool"
                if rec.get("screen") == "eligible":
                    os.makedirs(os.path.join(os.path.dirname(dst), "home"), exist_ok=True)
                    with open(os.path.join(os.path.dirname(dst), "home", f"{site}.html"), "w") as fh:
                        fh.write(r.body or "")
        rec["requests"] = int(pacer.count.get(site, 0))
        if not dev:
            L.book_site(root, site, "screen", rec["requests"], int(rec.get("bytes") or 0))
        out["sites"][site] = rec
        out["order"].append(site)
        save_json(dst, out)
        print(f"{site:36s} {st:5s} {rec.get('screen')!s:14s} req={rec['requests']} "
              f"search={(rec.get('search') or {}).get('how')} pool={len(rec.get('pool') or [])}", flush=True)
    print({st: {"screened": screened(st), "eligible": eligible(st)} for st in ("shop", "other")})
    return 0


# ---------------------------------------------------------------------------
# 3. the cohort (offline)
# ---------------------------------------------------------------------------

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
# 4. the judge's reader: deferred during the arms, a local CLI afterwards
# ---------------------------------------------------------------------------

def reader_question(kind: str, task: Dict[str, Any], answer: str, context: str) -> Dict[str, Any]:
    """The blind reader's question (no arm label): the task, the answer, the saved page."""
    q = {"check": "an answer to a question" if kind == "answer" else "the page reached",
         "task": task.get("utterance"), "task_kind": task.get("kind"), "item": task.get("target"),
         "expected (the task author's words)": task.get("expected") or ""}
    if kind == "answer":
        q["assistant_answer"] = answer
    q["page"] = context[:9000]
    return q


def reader_key(q: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(q, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cache_reader(cache: Dict[str, Any], collect: Optional[Dict[str, Any]] = None) -> Callable:
    """A reader that answers from the batch reader's cache; an unseen question is
    'unavailable' (the judge files the run ``pending_reader``) and collected."""
    def reader(kind, task, answer, context):
        q = reader_question(kind, task, answer, context)
        k = reader_key(q)
        hit = cache.get(k)
        if hit and hit.get("verdict") in ("correct", "wrong", "undeterminable"):
            return hit["verdict"], str(hit.get("why") or "")
        if collect is not None:
            collect[k] = q
        return "unavailable", "deferred: judged after the arms by the blind reader (local CLI)"
    return reader


# ---------------------------------------------------------------------------
# 5. one arm on one site
# ---------------------------------------------------------------------------

def share_for(arm: str, used: int) -> int:
    return max(0, min(SHARES[arm], SITE_CAP - used - MARGIN))


class SiteCtx:
    """The per-site, per-arm guard (``web_arch_live``'s, plus the noise lane and this bench's shares)."""

    def __init__(self, site: str, arm: str, share: int, nbytes: int, endpoint: str, gate, browser_statics: bool = False,
                 search_post: bool = False, refuse_competitors: bool = True):
        import discover_bench as DBN
        import live_bench as V
        from anyuse.execution.web_scope import WebScope, contact_refusal
        self.site, self.arm, self.share, self.endpoint = site, arm, share, endpoint
        self.pacer = V.HostPacer(REQUEST_MIN_INTERVAL_S)
        # the third-party part of the scope is the ONE setting ANYUSE_THIRD_PARTY
        # (web_scope.third_party_flags; default "search" = the site's own search provider, as this scope had); the
        # first-party allowances stay the arms' (SCOPE)
        from anyuse.execution.web_scope import third_party_flags
        self.scope = WebScope(readonly_post=SCOPE["ANYUSE_ALLOW_READONLY_POST_SEARCH"],
                              own_hosts=SCOPE["ANYUSE_ALLOW_SITE_OWN_HOSTS"], **third_party_flags())

        self._loading: set = set()

        def allow(u):
            h = D.host_of(u)
            if (refusal(h) if refuse_competitors else contact_refusal(h)):
                return False
            if h not in self.robots.parsers and h not in self._loading and self.scope.site_for(h):
                # the lazy robots.txt of a host the web scope allowed, fetched ONCE: the fetch of
                # that robots.txt asks allow() again; without the guard this recursed until the
                # recursion limit and every level fetched robots.txt once more (up to
                # ~170 robots.txt GETs to a site's own host in one run,
                # counted against the arm's share)
                self._loading.add(h)
                try:
                    self.robots.load(h)
                finally:
                    self._loading.discard(h)
            return self.robots.allow(u)
        self.allow = allow
        self.fetch = D.HttpFetcher(user_agent=UA, allow_url=allow, pacer=self.pacer,
                                   max_requests_per_host=share)
        self.robots = DBN.Robots(self.fetch, refuse_competitors=refuse_competitors)
        # browser_statics (the product's replay route: ``watch_live``, the MCP web-tool route and
        # its fallback - the agent path's rule): the site's static assets load like a normal browser visit
        # (images and fonts too), counted apart, outside the share's request and byte caps, which then count documents
        # and data; off (the benches' fixed config) the context is unchanged
        self.budget = RequestBudget(self.pacer, per_load_cap=share, per_host_cap=share, site_cap=share,
                                    nav_min_interval_s=DOC_MIN_INTERVAL_S, load_per_navigation=True,
                                    realistic_browser=True, static_bytes_cap=nbytes, static_in_flight=4,
                                    task_gate=gate, web_scope=self.scope,
                                    static_reserve=int(share * STATIC_RESERVE_FRAC),
                                    skip_types=() if browser_statics else SKIP_TYPES, noise_share=True,
                                    browser_statics=bool(browser_statics),
                                    # the product route's read-only POST search
                                    search_post=bool(search_post))
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


def session_class():
    """``web_arch_live``'s bench page (GET only but the web scope's read-only rules, anonymous,
    every observation kept) in a NEW browser context per session."""
    Base = L.session_class(wide=False)

    class FreshBenchSession(Base):
        fresh_context = True
    return FreshBenchSession


#: the static cache of a site, shared by the arms in turn (static assets under the
#: guard's HTTP rules; a hit is neither sent nor counted). Scratch, not evidence.
CACHE_DIR = "/tmp/anyuse-the measurement harness"


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


def settle():
    from anyuse.execution.web_arch_run import settle_bounded

    async def s(page):
        await settle_bounded(page)
    return s


class Arm:
    """One arm process: the models, the deferred reader, the ledger."""

    def __init__(self, root: str, arm: str, endpoint: str, log, dev: bool = False):
        import web_arch_models as M
        self.root, self.arm, self.endpoint, self.log, self.dev = root, arm, endpoint, log, dev
        self.M = M
        self.led = M.Ledger(R_(root, "ledger.json"), cap=1.50)
        self.spec = W.load_planner_spec()
        self.reader = cache_reader(load_json(R_(root, "runs", "reader_cache.json"), {}))
        self.openjev_log = R_(root, "runs" if not dev else "dev", f"openjev_{arm}.jsonl")
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

    def _ojlog(self, rec: Dict[str, Any]) -> None:
        with open(self.openjev_log, "a") as fh:
            fh.write(json.dumps(dict(rec, ts=round(time.time(), 3))) + "\n")

    def run_task(self, ctx: SiteCtx, task: Dict[str, Any], start_url: str, shots_dir: str) -> Dict[str, Any]:
        """One run of one task in a fresh browser context."""
        Session = session_class()
        holder: List[Any] = []

        def factory(endpoint, allow_url=None, **kw):
            s = Session(endpoint, allow_url=allow_url or ctx.allowed, budget=ctx.budget)
            holder.append(s)
            return s
        t0 = time.monotonic()
        if self.arm == "arch":
            from anyuse.execution import web_arch_run as RR
            import base64

            def sink(where, data_url, _t=task["id"]):
                os.makedirs(shots_dir, exist_ok=True)
                name = f"{_t}_{where}_{int(time.time() * 1000) % 10 ** 8}.jpg"
                with open(os.path.join(shots_dir, name), "wb") as fh:
                    fh.write(base64.b64decode(data_url.split(",", 1)[1]))
                return name
            eng = RR.WebArch(self.planner, extractor=self.extractor, chooser=self.chooser,
                             confirmer=self.confirmer, t2=self.t2, settle=settle(),
                             spec=self.spec, shot_sink=sink)

            async def go():
                async with factory(self.endpoint, allow_url=ctx.allowed) as page:
                    rec = await eng.run(page, dict(task, task_kind=task.get("kind")), start_url)
                    rec["guard_failed"] = [list(f) for f in page.guard.failed[:80]]
                    return rec
            rec = asyncio.run(go())
            rec["task_kind"] = task.get("kind")
        else:
            from anyuse.execution import agent_browser as AB
            from anyuse.execution import web_arch_run as RR
            from anyuse.execution.model_adapter import RunBudget
            lv = AB.default_levels()[0]
            brain = AB.CerebrasBrain(role=lv.role, max_tokens=lv.max_tokens, run_budget=RunBudget(lv.max_usd),
                                     price_in=lv.price_in, price_out=lv.price_out)
            agent = AB.StepAgent(brain, session_factory=factory, endpoint=self.endpoint,
                                 max_steps=L.AGENT_MAX_STEPS, allow_url=ctx.allowed, trace=True,
                                 system=AB.SYSTEM_ESCALATION, no_progress_steps=AB.NO_PROGRESS_STEPS,
                                 max_calls=lv.max_calls, deadline_s=lv.max_seconds)
            text = str(task.get("utterance") or "") + RR.AGENT_TASK_SUFFIX
            run = agent.run(text, start_url, "")
            page = holder[-1] if holder else None
            rec = RR.agent_record(run, task, self.arm, page.pages if page else [],
                                  guard_failed=[tuple(f) for f in (page.guard.failed if page else [])])
            rec["guard_failed"] = [list(f) for f in (page.guard.failed[:80] if page else [])]
            if page is not None and page.pages:
                last = page.pages[-1]
                rec["final"].update(title=last.get("title"), h1=last.get("h1"), facts=last.get("facts"),
                                    breadcrumb=last.get("breadcrumb"), links=last.get("links"))
                if not rec["final"].get("text"):
                    rec["final"]["text"] = last.get("text")
            if page is not None and rec["program"]["ops"] and rec.get("search_value"):
                after = next((u for t in run.trace or [] if (t.get("choice") or {}).get("action")
                              in ("search", "submit") for u in [((t.get("outcome") or {}).get("url_after"))] if u), "")
                dom = next((d for u, d in page.doms if after and u == after), "")
                if dom:
                    rec.setdefault("doms", {})["after_search"] = dom
            rec["cost"] = self.M.cerebras_cost(int(run.tokens_in or 0), int(run.tokens_out or 0))
            self.led("baseline", actual=rec["cost"])
            if run.stop == AB.INFRASTRUCTURE_STOP:
                rec["stop"] = "infrastructure"
                rec["infra"] = run.error
        rec["fresh"] = [dict(getattr(s, "fresh_check", {}) or {}) for s in holder]
        rec["sessions"] = len(holder)
        rec["seconds_wall"] = round(time.monotonic() - t0, 2)
        rec["requests_after"] = ctx.used()
        rec["lanes"] = dict(ctx.budget.lanes.get(ctx.budget.site_of(ctx.site), {}))
        return rec


def past_guard(arm) -> bool:
    return L.past_guard(arm)


def is_blocked(rec: Dict[str, Any]) -> str:
    return L.is_blocked(rec)


# ---------------------------------------------------------------------------
# 6. the proof (held-out values) and the compiled repeat
# ---------------------------------------------------------------------------

def proof_values(q0: str, values: Sequence[str]) -> List[str]:
    """compile = the run's own value; then the site's pool values in drawn order,
    without the compile value (the spare fills in): fitting, validation 1, validation 2."""
    out = [v for v in values if W.fold(v) != W.fold(q0)]
    return out[:3]


def prove(arm: Arm, ctx: SiteCtx, rec: Dict[str, Any], values: List[str], out_dir: str) -> Dict[str, Any]:
    """``web_arch_live``'s proof (tier 2 = the browser replay with the selector derived on the
    compile + fitting pages, each validation list must name its value and differ from
    the compile list; tier 1 = the HTTP GET of the result URL template), with the
    bounded settle, fresh contexts, the pool values and a record of every page."""
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
        res.update(status="failed", why=f"fewer than 3 pool values besides the compile value ({vals})")
        return res
    res["kind"] = "search"
    split = PS.assign([q0] + vals, n_selection=0, n_fitting=1)
    res["split"] = split.to_dict()
    tool = W.proof_tool(prog, "search")
    res["tool"] = tool
    Session = session_class()
    st = settle()

    def replay(ops):
        async def go():
            async with Session(arm.endpoint, allow_url=ctx.allowed, budget=ctx.budget) as page:
                r = await RR.replay(page, ops, settle=st, by_rule=True)
                r["fresh"] = dict(page.fresh_check)
                return r
        return asyncio.run(go())
    compile_dom = (rec.get("doms") or {}).get("after_search") or ""
    compile_url = ""
    if not compile_dom:
        r0 = replay(W.bind(tool["ops"], q0))
        compile_dom, compile_url = (r0.get("doms") or {}).get("after_search") or "", r0.get("search_url") or ""
        res["compile_replayed"] = True
    fit_v = split.fitting[0]
    r_fit = replay(W.bind(tool["ops"], fit_v))
    split.note_fit([q0, fit_v])
    if is_blocked(r_fit):
        res.update(status="blocked", why=is_blocked(r_fit))
        return res
    fit_dom = (r_fit.get("doms") or {}).get("after_search") or ""
    recs = [L.recording(compile_dom, q0, compile_url), L.recording(fit_dom, fit_v, r_fit.get("search_url") or "")]
    selector = derive_ground_truth(recs) if compile_dom and fit_dom else ""
    res["selector"] = selector
    res["fit"] = {"value": fit_v, "search_url": r_fit.get("search_url"), "stop": r_fit.get("stop"),
                  "has_dom": bool(fit_dom)}
    if not selector:
        res.update(status="failed", why="no result list derivable on the compile + fitting pages "
                                        + str(recs[0].get("recorder_warning") or recs[1].get("recorder_warning") or ""))
        return res
    compile_items = [str(x) for x in extract(compile_dom, selector, limit=30)]
    res["compile_items"] = compile_items[:10]
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
                "search_url": rv.get("search_url"), "tier2": bool(ok and changed and rv.get("stop") not in
                                                                  ("failed", "error")),
                "stop": rv.get("stop")}
        tmpl = L.tier1_template(rv.get("search_url") or "", v)
        case["tier1_template"] = tmpl
        if tmpl and items and ctx.left() >= 3:
            url = tmpl.replace("{q}", urllib.parse.quote_plus(v))
            if ctx.allowed(url):
                g = ctx.fetch.get(url)
                got = [str(x) for x in extract(g.body or "", selector, limit=40)] if g.status == 200 else []
                case["tier1"], case["tier1_why"] = W.labels_agree(got, items)
                case["tier1_status"] = g.status
        page_txt = (rv.get("final") or {}).get("text") or ""
        with open(os.path.join(out_dir, f"proof_{rec.get('task')}_{re.sub(r'[^a-z0-9]+', '_', v.lower())[:30]}.json"),
                  "w") as fh:
            json.dump({"value": v, "items": items[:10], "pages": rv.get("pages"), "final": rv.get("final"),
                       "page_text": page_txt[:20000], "case": case}, fh, ensure_ascii=False, default=str)
        cases.append(case)
    res["cases"] = cases
    passed = [c for c in cases if c.get("tier2")]
    split.finalize()
    res["held_out"] = split.held_out
    res["tier2"] = "proved" if len(passed) >= 2 and split.held_out else "failed"
    res["tier1"] = "proved" if res["tier2"] == "proved" and all(c.get("tier1") for c in cases) else "failed"
    res["status"] = "proved" if res["tier2"] == "proved" else "failed"
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
            r = await RR.replay(page, prog.get("ops") or [], settle=st, by_rule=False)
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
# 7. the run: one arm over one half of the sites
# ---------------------------------------------------------------------------

def ordered(tasks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The site's tasks, the search task first (its runs feed the proof), gates last."""
    rank = {k: i for i, k in enumerate(TASK_ORDER)}
    return sorted(tasks, key=lambda t: (rank.get(t.get("kind"), 9), str(t.get("id"))))


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
    nbytes = max(0, min(BYTES[arm.arm], 30 * 1024 * 1024 - used_bytes - 512 * 1024))
    e.update(share=share, prior_requests=used, prior_bytes=used_bytes)
    if share < MIN_RUN_LEFT or nbytes < 1024 * 1024:
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

    def try_proof(cands: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
        nonlocal blocked
        if e.get("proof") and e["proof"].get("status") not in ("no_tool",):
            return
        cand = next(((t, r) for t, r in cands if W.search_value(r.get("program") or {})), None)
        if cand is None:
            e["proof"] = {"status": "no_tool", "why": "no successful run with a search"}
        elif ctx.left() < MIN_PROOF_LEFT:
            e["proof"] = {"status": "share_exhausted", "left": ctx.left()}
        elif arm.arm == "arch" and past_guard(arm):
            e["proof"] = {"status": "not_run_pod_time"}
        else:
            e["proof"] = prove(arm, ctx, cand[1], values, sdir)
            e["proof"]["provisional"] = (cand[1].get("judge") or {}).get("verdict") == "pending_reader"
            ctx.budget.check()
            if e["proof"].get("status") == "blocked":
                blocked = str(e["proof"].get("why"))
        arm.log(f"{site:30s} {arm.arm} proof -> {(e.get('proof') or {}).get('status')} "
                f"{str((e.get('proof') or {}).get('why') or '')[:100]}")
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
                    if ctx.left() < MIN_RUN_LEFT:
                        e["tasks"].append({"task": t["id"], "run": k, "outcome": "share_exhausted",
                                           "left": ctx.left()})
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
                        rec["doms"] = doms                  # kept in memory for the proof
                    row = {"task": t["id"], "run": k, "kind": t["kind"], "verdict": j.get("verdict"),
                           "why": str(j.get("why"))[:200], "answer_verdict": j.get("answer_verdict"),
                           "wrong_data": j.get("wrong_data"), "forbidden": j.get("forbidden"),
                           "stop": rec.get("stop"), "answer": str(rec.get("answer"))[:200],
                           "model_calls": rec.get("model_calls"), "cost": round(float(rec.get("cost") or 0.0), 5),
                           "seconds": rec.get("seconds_wall"), "requests_after": rec.get("requests_after"),
                           "fresh": all((f or {}).get("fresh") for f in rec.get("fresh") or [{}])}
                    e["tasks"].append(row)
                    arm.log(f"{site:30s} {arm.arm} {t['id']} r{k} -> {row['verdict']} ({row['why'][:80]}) "
                            f"stop={row['stop']} req={row['requests_after']}/{share}")
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
                    if blocked:
                        break
            # no proof yet: the first successful run of another task that searched
            if not blocked and (not e.get("proof") or e["proof"].get("status") == "no_tool"):
                by_id = {t["id"]: t for t in tasks}
                others = [(by_id[tid], r) for tid in [t["id"] for t in tasks] for r in succ.get(tid, [])
                          if by_id[tid]["kind"] != "search" and (r.get("judge") or {}).get("verdict") == "ok"]
                if others or not e.get("proof"):
                    e["proof"] = None
                    try_proof(others)
            # the compiled repeat of every successful architecture task (no model)
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
                    if ctx.left() < MIN_REPEAT_LEFT:
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
