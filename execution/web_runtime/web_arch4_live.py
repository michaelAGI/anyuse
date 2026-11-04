"""The web architecture, live: an agent demonstrates a
workflow once, and AnyUse compiles it into a checked tool, or returns an explicit
failure.

usage: web_arch4_live.py frame  <R>                              offline: the frame (``web_arch3_live``'s never-contacted hosts)
       web_arch4_live.py screen <R> --part P --parts N --browser URL [--extend] [--dev]
       web_arch4_live.py cohort <R>                              offline: main sites, reserves, demonstrators, values
       web_arch4_live.py demo   <R> --part P --browser URL [--dev SITE]   the demonstration + its compilation
       web_arch4_live.py wave   <R> --wave immediate|delayed --part P --browser URL [--dev SITE]
       web_arch4_live.py report <R>

Per site: robots.txt + one home-page GET (HTTP), then the site's own search rendered in
a guarded browser for a start-page item word (the RESULT page gives the values) and for
a seeded nonsense term (the empty result must be the site's own statement); the values
(1 demonstration + 3 held-out, one of them the empty term) are drawn by the seed before
any demonstration. One demonstration (the assigned local CLI in a no-tools policy loop,
``execution/demo_policy.py``) -> its trace -> ``demo_compile.compile_trace`` (the trace
only) -> a checked tool or an explicit failure. Each wave: per held-out value the tool
(a fresh context, 0 model calls), the oracle's own search render and product page (fresh
contexts), tier 1 over HTTP (reported only, immediate wave), then ONE blind-reader call
per site (the OTHER CLI) and the ledger. A wrong accepted output is a hard stop.

Traffic exactly as approved for benchmarks (docs/TRAFFIC_POLICY.md): <= 300 requests and
<= 30 MB per site over the whole measurement, split into phase shares that no earlier phase
may spend (``execution/demo_traffic.py``); >= 3 s between documents; robots respected;
no stealth, no CAPTCHA; a block stops the host and adds it to NEVER_CONTACT.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import gzip
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
import web_arch2_live as L2  # noqa: E402
import web_arch3_live as L3  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_oracle as O  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import demo_traffic as TR  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution.cdp_guard import TrafficViolation  # noqa: E402

SPEC_ID = "SPEC_WEB_ARCH4.md"
SEED = "anyuse-web-arch4-2026-11-03"
UA = L.UA
NEVER_CONTACT = L.NEVER_CONTACT
CANDIDATES = os.path.join(HERE, "web_arch4_candidates.tsv")
VALUES_FILE = os.path.join(HERE, "web_arch4_values.json")
ARCH3_CANDIDATES = os.path.join(HERE, "web_arch3_candidates.tsv")
REAL_HOME = os.environ.get("ANYUSE_REAL_HOME") or os.path.expanduser("~")
#: absolute (the bench runs with a scratch HOME: never expanduser)
ARCH3_ROOT = os.path.join(REAL_HOME, "anyuse-raw-results/web-arch3-2026-11-03")
N_SCREEN = {"shop": 66, "other": 44}
MAIN = {"shop": 16, "other": 14}
RESERVES_WANTED = 4
#: the screen's extension goes on while a stratum has fewer eligible sites than this (main + 4;
#: ``web_arch4b_live`` screens 6 more others, the fill of shop slots when the shops run short)
EXTENSION_TARGET = {"shop": MAIN["shop"] + RESERVES_WANTED, "other": MAIN["other"] + RESERVES_WANTED}
#: a SHOP stratum short of eligible sites is filled by the next others (30 sites, 90 requests)
FILL_SHOP_SLOTS_WITH_OTHERS = False
SCREEN_EXTRA_MAX = 60
N_DEV = {"shop": 3, "other": 2}
MAX_CRAWL_DELAY = 5.0
SEED_TRIES = 3
#: candidate values kept from the seed's result page (seeded order); each is searched once and kept
#: only when the site lists a result naming it (DEV www.przyslijprzepis.pl: a word of the card texts
#: gave "Liczba przepisow: 0"); the first 3 kept are the demonstration value and 2 held-out values
VALUE_POOL = 5
EMPTY_TRIES = 2
RESULT_WAIT_S = 8.0
DEMO_CALL_CAP = 291  # 300 in total: the 9 DEV calls came first
DEV_CALL_CAP = 12
READER_CALL_CAP = 30
DEMO_CLI_TIMEOUT_S = 300
READER_CLI_TIMEOUT_S = 1500
DEMONSTRATORS = ("claude", "codex")
CLI_LABEL = {"claude": "claude -p --model sonnet", "codex": "codex exec -m o4-mini (reasoning low)"}
READER_LABEL = {"claude": "codex exec -m o4-mini (reasoning medium)", "codex": "claude -p --model sonnet"}
SCRATCH = os.environ.get("ANYUSE_WA4_SCRATCH") or \
    "/tmp/anyuse-the measurement harness"
CACHE_DIR = os.environ.get("ANYUSE_WA4_CACHE") or os.path.join(SCRATCH, "cache")
CLI_EMPTY_DIR = os.path.join(SCRATCH, "cli_empty")
CLI_OUT_DIR = os.path.join(SCRATCH, "cli_out")
#: the bench's label in NEVER_CONTACT notes (``web_arch4b_live`` sets its own)
BENCH_LABEL = "web-arch4"
#: the hosts earlier benches contacted (``screen/<file>``): never screened, by registrable domain
PRIOR_CONTACTED_FILE = "arch3_contacted.txt"
#: DEV only: the blind reader runs in the DEV wave too (``--dev-reader``; its calls count as DEV calls)
DEV_READER = False
#: DEV only: every screen render (the page read with its DOM) and the home page kept here, so the
#: screen's value rules can be re-run offline on the same pages (the rules are frozen on DEV)
DUMP_DIR = ""
#: True: a wrong accepted output stops the run. False: it is a MEASURED error - recorded
#: (``runs/WRONG_OUTPUTS.json``, a ledger event), audited and reported; the run goes on
WRONG_OUTPUT_HARD_STOP = True
#: the screen's value check: "rules" (the default) = the frozen value rules
#: exactly; "openjev" = every candidate of the rules' draw, in the rules' own order, is also asked
#: OpenJev's typed question (execution/value_check.py, via ``VALUE_CHECKER``) and kept only on its accept
#: rule, a rejected one replaced by the next of that order (execution/web_runtime/web_valuecheck.checked_values)
VALUE_CHECK = "rules"
VALUE_CHECKER = None
#: tools of format version >= 6: a wave's site context takes the tool's DECLARED hosts of
#: the site as its origin policy before anything is sent (``demo_tool.apply_site_hosts``; one site for the guard,
#: the pacer, the caps and the site gate) and reads robots.txt for each; a version-5 tool declares none. Off:
#: the earlier behaviour unchanged
SITE_HOSTS_FROM_TOOL = False
#: ``web_valuecheck2``, both OFF by default (frozen runs unchanged): every candidate
#: of the rules' draw is CLEANED (execution/value_clean.py) before any check (execution/web_runtime/web_valuecheck2.cleaned_values);
#: a PNG crop of every candidate's element is taken in the seed render the screen already has (no request) and
#: kept beside the screen record (``CROPS_DIR``; default: ``<screen record's folder>/crops``)
VALUE_CLEAN = False
#: the tool version a demonstration compiles to (None: ``demo_compile.TOOL_VERSION``; a bench may pin 7)
COMPILE_VERSION = None
VALUE_CROPS = False
CROPS_DIR = ""
#: the blind reader's parser: False (the default) = the frozen parser exactly
#: (``demo_oracle.parse_reader(text, ids)``); True = ``parse_reader(text, ids, last=True)``, the LAST
#: valid JSON object holding a ``cases`` list (a reader answer that corrects itself)
READER_PARSE_LAST = False
#: for FUTURE runs, whose protocol must name it: False (the default) = one
#: reader call per site and wave exactly; True = ``demo_oracle.ask_reader``: a case whose answer is truncated or has
#: no final verdict is asked ONCE more (the same case; a second call against the same reader cap) before its verdict
#: is recorded
READER_ONCE_MORE = False

load_json, save_json, locked = L.load_json, L.save_json, L.locked
registrable = L.registrable
refusal = L.refusal


def R_(root: str, *p: str) -> str:
    return os.path.join(root, *p)


def add_never_contact(host: str, why: str) -> None:
    with locked(NEVER_CONTACT):
        with open(NEVER_CONTACT) as fh:
            lines = fh.read().splitlines()
        if host in {ln.strip() for ln in lines if not ln.startswith("#")}:
            return
        head = [ln for ln in lines if ln.startswith("#")]
        body = sorted({ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")} | {host})
        head.append(f"# {time.strftime('%Y-%m-%d')} ({BENCH_LABEL}): {host}: {why[:90]}")
        with open(NEVER_CONTACT, "w") as fh:
            fh.write("\n".join(head + body) + "\n")


def slug(site: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", site.lower())


# ---------------------------------------------------------------------------
# 1. the frame: ``web_arch3_live``'s measured frame, minus every host it contacted
# ---------------------------------------------------------------------------

def arch3_contacted(r3: str = ARCH3_ROOT) -> set:
    out = set()
    s = load_json(R_(r3, "screen", "screen.json"), {}) or {}
    for k, v in (s.get("sites") or {}).items():
        out |= {k, str(v.get("site") or ""), str(v.get("frame_host") or ""), str(v.get("canonical_from") or "")}
    out |= set((load_json(R_(r3, "runs", "site_ledger.json"), {}) or {}).keys())
    out |= set((load_json(R_(r3, "dev", "site_ledger_dev.json"), {}) or {}).keys())
    out |= {e.get("site") for e in load_json(R_(r3, "screen", "dev_sites.json"), []) or []}
    for st in ("baseline_A", "baseline_B", "arch_A", "arch_B"):
        d = load_json(R_(r3, "runs", st, "state.json"), {}) or {}
        out |= set((d.get("entries") or {}).keys())
        for e in (d.get("entries") or {}).values():
            out |= set((e.get("hosts_sent") or {}).keys())
    return {h for h in out if h}


def cmd_frame(root: str) -> int:
    rows = [ln.rstrip("\n").split("\t") for ln in open(ARCH3_CANDIDATES) if ln.strip()]
    contacted = arch3_contacted()
    if len(contacted) < 77:
        raise SystemExit(f"refused: arch3 contacted hosts not found ({len(contacted)}) under {ARCH3_ROOT}")
    snap = R_(root, "screen", "never_contact_at_frame.txt")
    if not os.path.exists(snap):
        with open(snap, "w") as fh:
            fh.write("\n".join(sorted(L.never())) + "\n")
    nc = {ln.strip() for ln in open(snap) if ln.strip() and not ln.startswith("#")}
    with open(R_(root, "screen", "arch3_contacted.txt"), "w") as fh:
        fh.write("\n".join(sorted(contacted)) + "\n")
    old = L.SEED
    try:
        L.SEED = SEED
        frame, dropped = L.build_frame([(r[0], r[1], r[2] if len(r) > 2 else "") for r in rows], contacted, nc)
    finally:
        L.SEED = old
    dev = [e for e in frame if e["stratum"] == "shop"][-N_DEV["shop"]:] + \
        [e for e in frame if e["stratum"] == "other"][-N_DEV["other"]:]
    frame = [e for e in frame if e not in dev]
    save_json(R_(root, "screen", "dev_sites.json"), dev)
    with open(CANDIDATES, "w") as fh:
        for e in frame:
            fh.write(f"{e['site']}\t{e['stratum']}\t{e['category']}\n")
    save_json(R_(root, "screen", "frame_dropped.json"), dropped)
    print(f"frame: {len(frame)} ({sum(e['stratum'] == 'shop' for e in frame)} shops); DEV {len(dev)}; "
          f"arch3 contacted {len(contacted)}; dropped {({k: len(v) for k, v in dropped.items()})}")
    return 0


def cmd_frame_supplement(root: str) -> int:
    """The ``web_arch3_live`` remainder gave too few eligible sites under this bench's
    stricter screen (values from result pages, the empty state): a SUPPLEMENT of host names
    written for this bench (``screen/hosts_raw_supplement.tsv``), filtered by registrable
    domain against ``web_arch3_live``'s prior-host snapshot (every earlier cohort and
    archive), its written hosts, every host of this frame, its contacted
    hosts, NEVER_CONTACT, the government rule incl. public bodies and competitor domains;
    reshuffled with ``...:supplement:<stratum>``, 3 shops : 2 others, and APPENDED after the
    frame (the frame's order is unchanged)."""
    raw = [ln.rstrip("\n").split("\t") for ln in open(R_(root, "screen", "hosts_raw_supplement.tsv")) if ln.strip()]
    raw = [(r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "") for r in raw]
    prior = {ln.strip() for ln in open(R_(ARCH3_ROOT, "screen", "prior_hosts_snapshot.txt")) if ln.strip()}
    prior |= {ln.split("\t")[0].strip() for ln in open(R_(ARCH3_ROOT, "screen", "hosts_raw.tsv")) if ln.strip()}
    prior |= {ln.split("\t")[0].strip() for ln in open(CANDIDATES) if ln.strip()}
    prior |= {e["site"] for e in load_json(R_(root, "screen", "dev_sites.json"), []) or []}
    prior |= arch3_contacted()
    nc = L.never()
    old = L.SEED
    try:
        L.SEED = SEED + ":supplement"
        sup, dropped = L.build_frame(raw, prior, nc)
    finally:
        L.SEED = old
    with open(CANDIDATES, "a") as fh:
        for e in sup:
            fh.write(f"{e['site']}\t{e['stratum']}\t{e['category']}\n")
    save_json(R_(root, "screen", "frame_supplement_dropped.json"), dropped)
    print(f"supplement: {len(sup)} ({sum(e['stratum'] == 'shop' for e in sup)} shops) appended; dropped "
          f"{({k: len(v) for k, v in dropped.items()})}")
    return 0


def load_frame() -> List[Dict[str, str]]:
    out = []
    for ln in open(CANDIDATES):
        if ln.strip():
            site, stratum, cat = ln.rstrip("\n").split("\t")
            out.append({"site": site, "stratum": stratum, "category": cat})
    return out


def dev_sites(root: str) -> set:
    return {e["site"] for e in load_json(R_(root, "screen", "dev_sites.json"), []) or []}


# ---------------------------------------------------------------------------
# 2. values from the site's own RESULT pages (pure helpers)
# ---------------------------------------------------------------------------

_PRICE = re.compile(r"(\d[\d .,]*\s?(€|eur|kr|sek|nok|dkk|zl|zł|pln|chf|£|\$|:-|,-))|((€|£|\$|chf)\s?\d[\d.,]*)", re.I)
_NOISE = re.compile(r"(?i)(\b\d(?:[.,]\d)?\s*(?:/\s*5\s*)?(?:tähteä|sterne|stars?|étoiles|estrellas|stelle|sterren|stjerner|"
                    r"stjärnor|gwiazdek)\b|\b(?:keskiarvoltaan|bewertungen|reviews?|avis|opiniones|recensioni|beoordelingen|"
                    r"arvostelua|recensioner|anmeldelser|opinii)\b|\(\d+\)|\b\d+(?:[.,]\d+)?\s?%|★+|☆+|…|\.\.\.|\bnew\b|\bneu\b|\bnieuw\b|\bnouveau\b|\bsale\b|"
                    r"\bangebot\b|\bbestseller\b|\btop\b|\bonline only\b)")


#: a tile's stock / availability label (seen in the records: "I lager ...", "6 Kvar ...",
#: "... Op voorraad")
_STOCK = (r"(?:niet op voorraad|op voorraad|nicht auf lager|auf lager|sofort lieferbar|lieferbar|out of stock|in stock|"
          r"en stock|disponible|disponibile|ikke p[aå] lager|i lager|p[aå] lager|varastossa|dost[eę]pny|uitverkocht|"
          r"ausverkauft|sold out|slutsald|udsolgt|utsolgt|\d+ kvar|\d+ left|nur noch \d+(?: st(?:ü|ue?)ck)?)")
_PREFIX = re.compile(r"(?i)^(zum artikel|zum produkt|zur produktseite|weiter zum produkt|produktkort|details|"
                     r"mehr(?: zu| infos?)?|view|view product|quick (?:view|buy|shop)|schnellansicht|bekijk|"
                     r"voir|ver|vedi|katso|zobacz|shop|kaufen|productdetails|g[aå] til|nyhet|nyheter|neu|new|nieuw|"
                     r"nouveau|nouveaut[eé]|novit[aà]|nuevo|uusi|nowo[sś][cć]|sale|" + _STOCK + r")\s*[:\-–]?\s+")
#: a tile's variant / stock tail ("... + more variants", "... + weitere Farben", "... Op voorraad")
_SUFFIX = re.compile(r"(?i)(?:\s*\+\s*(?:\d+\s+)?(?:more|weitere|meer|plus de|m[aá]s|pi[uù]|fler|flere)\s+\w+|\s+" + _STOCK + r")\s*$")
_RATING = re.compile(r"(?i)\bbewertet mit \d+(?:[.,]\d+)? von \d+|\b\d(?:[.,]\d+)? (?:out of|von|sur|de|su|van|av|af|z) 5\b|"
                     r"^\d[.,]\d\s+")


def clean_title(t: str) -> str:
    import html as _html
    t = " ".join(_html.unescape(str(t or "")).split())
    for _ in range(3):
        t2 = _SUFFIX.sub("", _PREFIX.sub("", t)).strip()
        if t2 == t:
            break
        t = t2
    t = _RATING.sub(" ", t)
    t = _PRICE.sub(" ", t)
    t = _NOISE.sub(" ", t)
    # a leading symbol or a lone rating left over ("= günstiger ...", "5.0 Kaffeebohnen ...")
    t = re.sub(r"^[\W_]+|^\d[.,]\d\s+", "", t.strip())
    t = re.sub(r"\s+", " ", t).strip(" -|·,:;/")
    return t


def stem_names(text: str, seed: str) -> bool:
    """Does ``text`` name the seed word, tolerating an inflected ending (the screen's
    cluster choice only: PL/FI/DE search stems, "kurczak" for "kurczaka")?"""
    want = W.content_tokens(seed)
    have = W.tokens(text)
    if not want:
        return False
    for w in want:
        if not any(t == w or (len(t) >= 4 and len(w) >= 4 and
                              t[:max(4, min(len(t), len(w)) - 2)] == w[:max(4, min(len(t), len(w)) - 2)])
                   for t in have):
            return False
    return True


#: the result items' longest link texts (their card descriptions) of the last ``result_titles``
LONG_TEXTS: List[str] = []


def title_of(texts: Sequence[str]) -> str:
    """An item's title among its links' texts: ``demo_compile.item_title`` (the longest text of
    2-14 words, never a call to action), else the longest text."""
    t = C.item_title(texts)
    if t:
        return t
    import html as _html
    ts = [" ".join(_html.unescape(str(x or "")).split()) for x in texts]
    return max(ts, key=len) if ts else ""


def result_titles(dom: str, url: str, seed: str) -> Tuple[List[Tuple[str, str]], str]:
    """The result list of a rendered search page (pure): the link cluster whose member
    texts name the seed term most (outside the site chrome), as (title, url) in page
    order; else the content links naming it."""
    scan = D.scan_page(dom or "", url)
    host = (urllib.parse.urlsplit(url).netloc or "").lower()
    all_texts: Dict[str, List[str]] = {}
    for ln in scan.links:
        all_texts.setdefault(ln.url, []).append(ln.text or "")
    best = None
    for c in D.link_clusters(scan.links, host, min_members=3):
        if c.frac("chrome") > 0.5 or c.nav_frac() >= 0.6:
            continue
        texts: Dict[str, str] = {}
        order: List[str] = []
        for v, t in c.members:
            u = c.template.replace("{v}", v)
            if u not in texts:
                order.append(u)
                texts[u] = title_of(all_texts.get(u) or [str(t or "")])
        naming = sum(1 for u in order if C.name_share(texts[u], seed) > 0 or stem_names(texts[u], seed))
        key = (naming, round(c.frac("main"), 2), len(order))
        if naming >= 2 and (best is None or key > best[0]):
            best = (key, [(texts[u], u) for u in order if texts[u]], c.template)
    if best is not None:
        # the result list proper: the cluster's items naming the seed, when >= 4 do (a sidebar of
        # popular items shares the cluster's URL pattern: DEV www.przyslijprzepis.pl)
        named = [(t, u) for t, u in best[1] if C.name_share(t, seed) > 0 or stem_names(t, seed)]
        rows = named if len(named) >= 4 else best[1]
        LONG_TEXTS[:] = [max(all_texts.get(u) or [""], key=len) for _t, u in rows]
        return rows, f"cluster {best[2][:80]} ({len(best[1])} links, {best[0][0]} name the seed; {len(rows)} kept)"
    out, seen = [], set()
    LONG_TEXTS[:] = []
    for ln in scan.links:
        t = " ".join((ln.text or "").split())
        if ln.chrome or not t or ln.url in seen or C.host_of(ln.url) != host.split(":")[0]:
            continue
        if (C.name_share(t, seed) > 0 or stem_names(t, seed)) and not W.forbidden_control(t, ln.url):
            seen.add(ln.url)
            out.append((t, ln.url))
    return out, f"content links naming the seed ({len(out)})"


#: words that make a category name a site-chrome seed (help, search, policies, stores, services), folded
SEED_CHROME = frozenset("""assistenza ayuda aide hilfe help suche erweiterte advanced recherche buscar cerca ricerca zoeken
sok haku szukaj policy cookie cookies privacy negozi negozio stores store tiendas tienda magasins magasin filialen filiale
butikker butiker butik spesa online service servizio servizi servicio kundenservice klantenservice kundeservice contatti
contacto contact kontakt impressum faq login konto account carrello warenkorb panier cesta cart newsletter blog magazin
magazine news jobs karriere press presse app apps download gift geschenk gutschein voucher inspiration ideen ideas idee
marchi marken brands merken marques marcas offerte angebote aanbiedingen promo promotions outlet sale saldi rebajas soldes
nieuws noticias notizie actualites uber ueber about chi siamo nosotros
refer friend friends earn rewards reward club member members klarna finance prospekte prospekt brochure brochures katalog
catalogue flyer volantino zakelijk business gewerbe professionnels empresas aziende korting rabatt discount deals deal
code friday cyber""".split())


#: shops: words that make a tile's title site chrome or a promotion, never a product value
#: (www.myprotein.com drew "Order by 1am for Next Day Delivery" and "Earn Credit?"; others
#: drew "zzgl. Versandkosten", "Store Opening Hours", "Free Standard Delivery On Orders", "SKIP TO CONTENT")
VALUE_CHROME = frozenset("""delivery shipping versand versandkosten versandkostenfrei lieferung livraison envio envios
spedizione spedizioni bezorging verzending levering leverans frakt fragt toimitus dostawa wysylka returns retouren
rucksendung ruecksendung rueckversand opening offnungszeiten oeffnungszeiten horaires skip startseite homepage
hauptinhalt contenido contenuto inhoud refer credit newsletter nieuwsbrief gutschein voucher vouchers giftcard
cadeaukaart presentkort gavekort lahjakortti bestellschein awards award sortieren sorteren ordina ordenar trier
direzione crescente decrescente klarna financing finanzierung ratenkauf cookie cookies privacy login anmelden konto
account zuruck zurueck terug ofertas oferta offerte angebote aanbiedingen erbjudanden tilbud tarjoukset promocje
promotions promotion promo korting rabatt rabatte discount sconto descuento rabais remise alennus rabat
varianten variants variant verfugbar available beschikbaar disponibles disponibili tillganglig saatavilla""".split())
_PROMO = re.compile(r"\b(?:use code|code:|gebruik code|extra off|\d+ ?% off|order by|bestellt? bis|commandez avant|"
                    r"ends \d|endet|free (?:standard |next day )?delivery|gratis (?:versand|verzending|levering|frakt)|"
                    r"bis zu|guenstiger|gunstiger)\b|"
                    # a countdown ("06 S. 56 Min."): two numbers with time units
                    r"\b\d+ ?(?:d|t|h|s|std|min|sek|sec|tage?|days?|hours?|stunden|minuten|sekunden|uren|minuten)\b\.?"
                    r"\W{0,4}\d+ ?(?:h|s|std|min|sek|sec|hours?|minutes?|stunden|minuten|sekunden|uren)\b")
#: shops: a category name that is content, not products (www.cookinglife.nl "Recepten")
SHOP_SEED_CHROME = frozenset("""recipes recipe rezepte rezept recettes recette recetas receta ricette ricetta recepten recept
opskrifter oppskrifter reseptit przepisy ratgeber tips tipps conseils consejos consigli advies""".split())


def chrome_value(t: str) -> bool:
    """A product title that is site chrome or a promotion (``VALUE_CHROME``, ``_PROMO``), or not a
    name at all (fewer than 2 words of >= 3 letters: a countdown "06 S. 56 Min. . Bis zu")."""
    words = [w for w in W.content_tokens(t) if len(w) >= 3 and w.isalpha()]
    return len(words) < 2 or bool(set(W.content_tokens(t)) & VALUE_CHROME) or bool(_PROMO.search(W.fold(t)))


def short_name(t: str) -> str:
    """A long product title's name part (www.cookinglife.nl: "Sola Braadpan Pompoen Oranje -
    Gietijzer - ø 24 cm / 2.8 Liter - ..."): above 6 words, the head before the first " - ", " | ",
    " / " or ", " when it keeps >= 2 words and >= 2 content words."""
    if len(t.split()) <= 6:
        return t
    head = re.split(r"\s+[-–|/]\s+|,\s+", t, maxsplit=1)[0].strip()
    if len(head.split()) >= 2 and len(W.content_tokens(head)) >= 2:
        return head
    return t


def product_rows(r: Dict[str, Any], cluster_only: bool = False) -> List[Tuple[str, str]]:
    """Shops: the PRODUCT tiles of a rendered page - same-site links outside the
    header, navigation and footer whose card (or own text) shows a price, and the products its
    schema.org data names - as (title, url). Any link cluster naming the
    seed drew site chrome and categories ("Cookie Policy", "Pflege & Hygiene")."""
    host = C.host_of(r.get("url"))
    texts: Dict[str, List[str]] = {}
    order: List[Tuple[str, str]] = []
    priced = set()
    priced_links = {W.norm_url(C.strip_fragment(u)) for u in r.get("priced_links") or []}
    for e in r.get("elements") or []:
        if e.get("tag") != "a" or not e.get("href") or e.get("area") in ("header", "nav", "footer"):
            continue
        href = C.strip_fragment(str(e["href"]))
        if not C.same_site(href, host) or W.asset_href(href) or W.forbidden_control(e.get("text"), href):
            continue
        k = W.norm_url(href)
        if k not in texts:
            texts[k] = []
            order.append((k, href))
        texts[k].append(str(e.get("text") or ""))
        if _PRICE.search(str(e.get("ctx") or "")) or _PRICE.search(str(e.get("text") or "")) or \
                k in priced_links:
            priced.add(k)
    rows = [(clean_title(title_of(texts[k])), href) for k, href in order if k in priced]
    # a tile whose only text is a call to action ('Leggi di piu') has no title
    rows = [(t, u) for t, u in rows if not C.CTA.match(W.fold(t))]
    # a skip link, the home page or the page itself is never a product tile
    here = W.norm_url(C.strip_fragment(str(r.get("url") or "")))
    home = W.norm_url(f"https://{host}/")
    rows = [(t, u) for t, u in rows if W.norm_url(C.strip_fragment(u)) not in (here, home)]
    rows = product_cluster(rows, strict=cluster_only)
    seen = {W.fold(t) for t, _u in rows}
    for f in r.get("facts") or []:
        n = " ".join(str(f.get("name") or "").split())
        if n and W.fold(n) not in seen and f.get("price") is not None:
            rows.append((n, str(f.get("url") or "")))
            seen.add(W.fold(n))
    return [(t, u) for t, u in rows if len(W.content_tokens(t)) >= 2]


def product_cluster(rows: List[Tuple[str, str]], strict: bool = False) -> List[Tuple[str, str]]:
    """The priced tiles that share the PRODUCT URL pattern: the largest link cluster among
    them (>= 3 links) - a priced category tile ('Live & Studio', www.kirstein.de) has another
    URL shape than the products; all rows when no cluster holds 3 - none when ``strict`` (the
    seed's result page: a page without 3 products sharing a URL pattern is not a
    product list; its priced links were promotions: www.myprotein.com)."""
    if len(rows) < 3:
        return [] if strict else rows
    links = [D.Link(url=u, text=t) for t, u in rows if u]
    best = None
    hosts = {(urllib.parse.urlsplit(u).netloc or "").lower() for _t, u in rows if u}
    for h in hosts:
        for c in D.link_clusters(links, h, min_members=3):
            urls = {W.norm_url(c.template.replace("{v}", v)) for v in c.values}
            n = sum(1 for _t, u in rows if W.norm_url(u) in urls)
            if best is None or n > best[0]:
                best = (n, urls)
    if best is None or best[0] < 3:
        return [] if strict else rows
    return [(t, u) for t, u in rows if W.norm_url(u) in best[1]]


def product_values(titles: Sequence[str], site: str, k: int = 3, first_n: int = 12) -> List[str]:
    """Shops: ``k`` distinct result titles (2-10 words, >= 2 content words, none a
    word-subset of another), seeded from the first ``first_n`` usable titles."""
    pool, seen = [], set()
    for t in titles:
        c = short_name(clean_title(t))
        toks = W.content_tokens(c)
        if not (2 <= len(c.split()) <= 10) or len(toks) < 2 or W.forbidden_control(c, "") or chrome_value(c):
            continue
        key = frozenset(toks)
        if key in seen:
            continue
        seen.add(key)
        pool.append(c)
        if len(pool) >= first_n:
            break
    rng = random.Random(f"{SEED}:values:{site}")
    rng.shuffle(pool)
    out: List[str] = []
    for c in pool:
        s = set(W.content_tokens(c))
        if any(s <= set(W.content_tokens(o)) or set(W.content_tokens(o)) <= s for o in out):
            continue
        out.append(c)
        if len(out) >= k:
            break
    return out


#: function words of the frame's languages that ``web_arch3_values.GENERIC`` misses (folded)
VALUE_STOP = frozenset("""ktora ktory ktore ktorych jako jest oraz przy przez tego tych tylko sobie jego jej
sie bardzo nasze nasza nasz wasze moje twoje kiedy dlaczego ktorego takze wiele jaki jaka jakie
joka jotka mutta sekä kanssa ilman niin myos tama tassa siita miten miksi ovat
eller ogsa hvordan hvorfor derfor eller ikke denne dette disse hvilke vilken vilket vilka
dessa detta denna darfor varfor hvad hvem hvilken
tahtea tahtia keskiarvoltaan tahtiluokitus arvostelua bewertungen bewertung reviews review stars sterne etoiles
estrellas stelle sterren stjerner stjarnor gwiazdek avis opiniones recensioni beoordelingen recensioner anmeldelser
also dies diese dieser dieses deine wieso warum welche welcher
""".split())


def word_values(titles: Sequence[str], site: str, seed: str, k: int = 3, top: int = 10) -> List[str]:
    """Other sites: ``k`` words shared by >= 2 result titles, >= 4
    letters, not generic, not the seed's or the site's own words; seeded from the
    ``top`` most shared."""
    import web_arch3_values as V3
    host_words = set(re.split(r"[.-]", site.lower())) | VALUE_STOP
    seedw = set(W.content_tokens(seed))
    df: Counter = Counter()
    first: Dict[str, Tuple[int, str]] = {}
    for i, t in enumerate(titles):
        ws = {}
        for raw in re.findall(r"[^\W\d_][^\W_]*", str(t)):
            # one word = one token of the matching the tools use (W.tokens: a letter outside
            # a-z after folding, e.g. Polish "l-stroke", would split the word: not a value)
            toks = W.content_tokens(raw)
            if len(toks) != 1:
                continue
            f = toks[0]
            if len(f) < 4 or not f.isalpha() or f in V3.GENERIC or f in L2.CHROME or f in W.STOPWORDS or \
                    f in host_words or f in seedw or f.startswith(L2.CHROME_STEMS) or stem_names(f, seed):
                continue
            ws.setdefault(f, raw)
        for f, raw in ws.items():
            df[f] += 1
            first.setdefault(f, (i, raw.lower() if raw.isupper() else raw))
    shared = sorted([f for f in df if df[f] >= 2], key=lambda f: (-df[f], first[f][0]))[:top]
    rng = random.Random(f"{SEED}:values:{site}")
    rng.shuffle(shared)
    return [first[f][1] for f in shared[:k]]


_SYL = ("zor", "qua", "vex", "lim", "brak", "tu", "nix", "ol", "ev", "zir", "mun", "dax", "pel", "quo", "rin", "yth",
        "gra", "ux", "fen", "kaz", "vor", "jil", "trax", "om", "plu", "zeb")


def empty_terms(site: str, n: int = EMPTY_TRIES) -> List[str]:
    """Seeded nonsense product-like terms (two words of 3 syllables each): a site's
    search validly finds nothing for them."""
    rng = random.Random(f"{SEED}:empty:{site}")
    out = []
    for _ in range(n):
        words = []
        for _w in range(2):
            w = "".join(rng.choice(_SYL) for _s in range(3))
            words.append(w.capitalize())
        out.append(" ".join(words))
    return out


def is_empty_page(read: Dict[str, Any], term: str) -> Tuple[bool, str]:
    st = C.no_results_statement(read.get("main_text") or read.get("text"))
    naming = [e for e in W.content_links(read) if C.name_share(e.get("text"), term) > 0]
    if st and not naming:
        return True, f"the page says {st!r}; no link names the term"
    return False, ("no 'no results' statement" if not st else f"{len(naming)} link(s) name the term")


# ---------------------------------------------------------------------------
# 3. the per-site guard, the phases and the browser
# ---------------------------------------------------------------------------

def booked(root: str, site: str) -> Dict[str, Dict[str, int]]:
    d = load_json(L.site_ledger_path(root), {}) or {}
    return {p: v for p, v in (d.get(site) or {}).items() if p in TR.PHASES}


#: DEV (never measured): smaller shares (www.przyslijprzepis.pl was ``web_arch3_live``'s DEV site too: its cumulative traffic
#: stays below 300 requests and 30 MB)
DEV_SHARES = {"screen": 60, "demo": 50, "immediate": 60, "delayed": 0}
DEV_BYTES = {"screen": 4 * TR.MB, "demo": 5 * TR.MB, "immediate": 5 * TR.MB, "delayed": 0}


def dev_share_for(phase: str, b: Dict[str, Dict[str, int]]) -> Tuple[int, int]:
    used = sum(int(v.get("requests", 0)) for v in b.values())
    used_b = sum(int(v.get("bytes", 0)) for v in b.values())
    own = b.get(phase) or {}
    later = [p for p in TR.PHASES[TR.PHASES.index(phase) + 1:] if p not in b]
    req = min(DEV_SHARES[phase] - int(own.get("requests", 0)), sum(DEV_SHARES.values()) - used -
              sum(DEV_SHARES[p] for p in later))
    nb = min(DEV_BYTES[phase] - int(own.get("bytes", 0)), sum(DEV_BYTES.values()) - used_b -
             sum(DEV_BYTES[p] for p in later))
    return max(0, req), max(0, nb)


def phase_ctx(root: str, site: str, phase: str, endpoint: str, gate) -> Tuple[Optional[L3.SiteCtx], Tuple[int, int]]:
    dev = os.path.basename(root.rstrip("/")) == "dev"
    req, nb = (dev_share_for if dev else TR.share_for)(phase, booked(root, site))
    if req <= 0 or nb <= 0:
        return None, (req, nb)
    ctx = L3.SiteCtx(site, phase, req, nb, endpoint, gate)
    return ctx, (req, nb)


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


def left(ctx) -> Tuple[int, int]:
    return ctx.left(), ctx.bytes_left()


def research_root(root: str) -> str:
    """The research folder (DEV runs live in ``<research>/dev``)."""
    return os.path.dirname(root.rstrip("/")) if os.path.basename(root.rstrip("/")) == "dev" else root


def hard_stopped(root: str) -> bool:
    """A hard stop anywhere in the run (the DEV folder's or the measured one's)."""
    return os.path.exists(R_(root, "runs", "HARD_STOP.json")) or \
        os.path.exists(R_(research_root(root), "runs", "HARD_STOP.json"))


def persist_violation(root: str, site: str, phase: str, rec: Dict[str, Any]) -> str:
    """A traffic violation (a HARD STOP): the guard's report and the RAW events of
    every flagged request go to ``runs/VIOLATION_<site>_<phase>_<time>.json`` BEFORE the
    process stops, and ``runs/HARD_STOP.json`` is written (every lane stops before its next
    site). Returns the file written."""
    at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rr = research_root(root)
    os.makedirs(R_(rr, "runs"), exist_ok=True)
    path = R_(rr, "runs", f"VIOLATION_{slug(site)}_{phase}_{time.strftime('%H%M%S')}.json")
    save_json(path, {"site": site, "phase": phase, "at": at, "dev": rr != root,
                     "violations": rec.get("violations"), "violation_events": rec.get("violation_events"),
                     "browser_cache_hits": rec.get("browser_cache_hits"), "requests": rec.get("requests"),
                     "bytes": rec.get("bytes"), "hosts_sent": rec.get("hosts_sent")})
    hs = R_(rr, "runs", "HARD_STOP.json")
    if not os.path.exists(hs):
        save_json(hs, {"why": "a traffic violation (hard stop)", "at": at, "site": site, "phase": phase,
                       "violation": (rec.get("violations") or [""])[0], "events_file": os.path.basename(path),
                       "site_traffic": {"requests": rec.get("requests"), "bytes": rec.get("bytes")}})
    return path


def close_ctx(root: str, ctx, site: str, phase: str, rec: Dict[str, Any]) -> None:
    """Book the phase's traffic on the site ledger and keep the guard's report."""
    rec["requests"] = ctx.used()
    rec["bytes"] = ctx.nbytes()
    rec["violations"] = list(ctx.budget.violations)
    # the raw CDP events of every flagged request (without them an alarm is undiagnosable)
    rec["violation_events"] = list(ctx.budget.violation_events)
    rec["browser_cache_hits"] = len(ctx.budget.browser_cache_hits)
    rec["hosts_sent"] = dict(Counter(h for (h, _t, _u, _ty) in ctx.budget.sent))
    rec["sent_types"] = dict(Counter(ty for (_h, _t, _u, ty) in ctx.budget.sent))
    rec["min_gap_documents_s"] = L._min_doc_gap(ctx.budget, site)
    rec["third_party_refused"] = len(ctx.budget.third_party_refused)
    rec["refused_methods"] = [list(x) for x in ctx.budget.refused_methods[:10]]
    L.book_site(root, site, phase, rec["requests"], rec["bytes"])
    if rec["violations"] or rec["violation_events"]:
        rec["violation_file"] = persist_violation(root, site, phase, rec)
    try:
        rec["cache_saved"] = save_cache(ctx.budget, site)
    except Exception:                                  # noqa: BLE001
        pass


def session_class():
    return L2.session_class()


def settle():
    return L2.settle()


def in_page(ctx, endpoint: str, fn) -> Any:
    """``fn(page)`` in a NEW browser context (a fresh anonymous session)."""
    Session = session_class()

    async def go():
        async with Session(endpoint, allow_url=ctx.allowed, budget=ctx.budget) as page:
            r = await fn(page)
            if isinstance(r, dict):
                r.setdefault("fresh", dict(getattr(page, "fresh_check", {}) or {}))
            return r
    return asyncio.run(go())


async def document_status(page) -> Optional[int]:
    """The HTTP status of the page's current document (its last Document response for the
    current URL; the first Document when none matches), None when unknown. A local read."""
    try:
        net = await page.network(want_bodies=False)
        here = C.strip_fragment(str(await page.url()))
    except Exception:                                  # noqa: BLE001
        return None
    docs = [n for n in net or [] if str(n.get("resource_type") or "") == "Document" and n.get("status")]
    for n in reversed(docs):
        if C.strip_fragment(str(n.get("url") or "")) == here:
            return int(n["status"])
    return int(docs[0]["status"]) if docs else None


async def render_search(page, start_url: str, value: str, templates: Sequence[str]) -> Dict[str, Any]:
    """The screen's render of the site's own search: its search URL seen on the start
    page, else the box on the start page; the result wait; the page with its DOM."""
    from anyuse.execution import demo_tool as T
    from anyuse.execution.web_arch_run import site_block
    st = settle()
    tm = [t for t in templates if "{q}" in t][:2]
    how = "typed"
    for t in tm:
        # the site's search URLs seen on the start page, in order; one the site redirects to a
        # path robots.txt refuses (DEV www.przyslijprzepis.pl: /search -> /search/recipes), that
        # fails to load, or whose document is an HTTP error (DEV www.roller.de: its form's
        # /search/?text= answers 404 "Seite nicht gefunden") is not used: the next one, else the box
        await page.goto(C.fill(t, value))
        await st(page)
        if not str(await page.url()).startswith("chrome-error") and (await document_status(page) or 200) < 400:
            how = "url"
            break
    if how == "typed":
        await page.goto(start_url)
        await st(page)
    await T.consent(page, "screen")
    if how == "typed":
        probe = await page.type_search(value)
        await st(page)
        if isinstance(probe, dict) and probe.get("note") and not probe.get("input"):
            how = "typed: " + str(probe.get("note"))
    await O._wait_named(page, value, RESULT_WAIT_S)
    r = await T.read_full(page, with_dom=True)
    try:
        net = await page.network(want_bodies=False)
    except Exception:                                  # noqa: BLE001
        net = []
    r["blocked"] = site_block(str(r.get("text") or ""), str(r.get("url") or ""), net, start=False)
    r["how"] = how
    return r


# ---------------------------------------------------------------------------
# 4. the screen
# ---------------------------------------------------------------------------

def screen_path(root: str, dev: bool = False) -> str:
    return R_(root, "screen", "screen.json")


def screen_update(path: str, site: str, rec: Dict[str, Any]) -> None:
    with locked(path):
        d = load_json(path, {"sites": {}}) or {"sites": {}}
        d["sites"][site] = rec
        save_json(path, d)


def screen_http(root: str, ctx, site: str, dev: bool) -> Tuple[Dict[str, Any], str]:
    """robots.txt (+ the www/apex variant's) and ONE home-page GET (``web_arch3_live``'s rules)."""
    import live_bench as V
    import site_screen as SS
    import value_screen as VS
    import query_values as VV
    rec: Dict[str, Any] = {}
    robots = ctx.robots
    robots.load(site)
    rec["robots"] = robots.notes.get(site)
    start = f"https://{site}/"
    if str(rec["robots"]).startswith("robots.txt 40") or "blocked" in str(rec["robots"]):
        add_never_contact(site, f"{BENCH_LABEL}{' DEV' if dev else ''} screen: {rec['robots']}")
        return dict(rec, screen="robots"), ""
    if not robots.allow(start):
        return dict(rec, screen="robots"), ""
    d = V.crawl_delay(robots, site)
    if d > 1.0:
        ctx.pacer.intervals[site] = d
        rec["crawl_delay"] = d
    if d > MAX_CRAWL_DELAY:
        return dict(rec, screen="crawl_delay"), ""
    r = ctx.fetch.get(start)
    ctx.budget.add_bytes(site, L3.wire_bytes(r))
    rec.update(status=r.status, final_url=r.final_url, error=(r.error or "")[:120], bytes=len(r.body or ""))
    blk = V.page_block(SS.text_of(r.body), r.final_url or start,
                       [{"resource_type": "Document", "url": r.final_url or start, "status": r.status}])
    if (r.error or "").startswith("blocked") or blk or r.status in (401, 403, 429):
        why = blk or r.error or f"HTTP {r.status}"
        add_never_contact(site, f"{BENCH_LABEL}{' DEV' if dev else ''} screen: {why}")
        return dict(rec, screen="blocked", why=why[:160]), ""
    if r.status != 200 or D.host_of(r.final_url or start) != site or "html" not in (r.mime or ""):
        return dict(rec, screen="no_page"), ""
    url = r.final_url or start
    dump(site, "home", {"url": url, "status": r.status, "body": r.body or ""})
    rec.update(VS.classify_safe(site, start, start, r.body, url, lambda dom: VV.listing_tokens(dom),
                                 min_tokens=VV.N_ELIGIBLE))
    rec.pop("stratum", None)
    tmpls = W.search_url_templates(r.body, url, re.findall(
        r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.body or ""))
    # the search box's own GET form first (what a person's Enter submits), then SearchAction, then links
    tmpls = [t for t in tmpls if C.search_template_ok(t["template"])]
    tmpls = sorted(tmpls, key=lambda t: (0 if t.get("how") == "GET search form" else 1, -float(t.get("score") or 0)))
    rec["search_urls"] = [t["template"][:300] for t in tmpls[:3]]
    rec["item_terms"] = L3.item_terms(r.body, url)[:12]
    rec["categories"] = [c for c in L2.value_pool(r.body, url) if 1 <= len(c.split()) <= 2][:12]
    rec["tile_seeds"] = tile_seeds(r.body, url)
    rec["snippets"] = L.snippets(r.body, url)
    if rec.get("screen") == "no_search" and tmpls:
        rec["screen"], rec["eligible_by"] = "eligible", "the site's search URL on its page"
    if rec.get("screen") != "eligible":
        return rec, r.body
    sample = (rec["item_terms"] or ["test"])[0]
    if tmpls:
        u = C.fill(tmpls[0]["template"], sample)
        rec["search"] = {"how": "url_template", "url": u[:200], "ok": bool(robots.allow(u))}
    else:
        rec["search"] = L2.search_check(site, r.body, url, robots, sample)
    if not rec["search"]["ok"]:
        rec["screen"] = "robots_search"
    elif not rec["item_terms"]:
        rec["screen"] = "value_pool"
    return rec, r.body


def tile_seeds(body: str, url: str, n: int = 3) -> List[str]:
    """Shops: the first two words of the start page's PRODUCT tiles (a title with a
    price, or a schema.org product), e.g. "Marmot Precip": a search for them lists products."""
    import web_arch3_values as V3
    out: List[str] = []
    for sc, t in V3.scored_titles(body or "", url):
        if sc < 2:
            continue
        words = [w for w in clean_title(t).split() if W.content_tokens(w) and not W.content_tokens(w)[0].isdigit()]
        seed = " ".join(words[:2])
        if len(words) >= 2 and W.fold(seed) not in {W.fold(x) for x in out} and \
                not (set(W.content_tokens(seed)) & SEED_CHROME):
            out.append(seed)
        if len(out) >= n:
            break
    return out


def seed_terms(rec: Dict[str, Any]) -> List[str]:
    """The screen's search seeds: the site's own category names on its start
    page (navigation link texts of 1-2 words, ``web_arch2_live``'s value pool) alternating with
    ``web_arch3_values``' item words, the first category first. The item words alone were banner words
    on many start pages ("prodotti", "korting", "volgende": www.eataly.net, www.kwantum.nl) whose
    searches list no items; a category's search lists its items."""
    chrome = SEED_CHROME | (SHOP_SEED_CHROME | VALUE_CHROME if rec.get("stratum") == "shop" else frozenset())
    cats = [c for c in rec.get("categories") or [] if not (set(W.content_tokens(c)) & chrome)]
    items = [c for c in rec.get("item_terms") or [] if not (set(W.content_tokens(c)) & chrome)]
    out: List[str] = list(rec.get("tile_seeds") or [])[:2] if rec.get("stratum") == "shop" else []
    for a, b in zip(cats + [""] * len(items), items + [""] * len(cats)):
        for x in (a, b):
            if x and W.fold(x) not in {W.fold(y) for y in out}:
                out.append(x)
    return out


def screen_values(ctx, endpoint: str, site: str, stratum: str, rec: Dict[str, Any], dev: bool,
                  value_check: Optional[str] = None, value_clean: Optional[bool] = None,
                  value_crops: Optional[bool] = None) -> Dict[str, Any]:
    """The browser stage: the site's own search for a start-page item word (<= 2 tries):
    the RESULT page's titles give the values; then the seeded empty term (<= 2 tries)
    must show the site's own "no results" statement. ``value_check``: "rules" or "openjev"
    (default: the module's ``VALUE_CHECK``, "rules"); ``value_clean`` / ``value_crops`` (default: the
    module's ``VALUE_CLEAN`` / ``VALUE_CROPS``, off)."""
    value_check = value_check or VALUE_CHECK
    if value_check not in ("rules", "openjev"):
        raise ValueError(f"value_check {value_check!r}: 'rules' or 'openjev'")
    value_clean = VALUE_CLEAN if value_clean is None else bool(value_clean)
    value_crops = VALUE_CROPS if value_crops is None else bool(value_crops)
    start = f"https://{site}/"
    tm = rec.get("search_urls") or []
    tries = []
    vals: List[str] = []
    for seed in seed_terms(rec)[:SEED_TRIES]:
        if not TR.can_start("value_render", *left(ctx)):
            rec.update(screen="screen_share", why="the screen share ran out before the values")
            return rec
        if value_crops:
            import web_valuecheck2 as VK2
            r = in_page(ctx, endpoint, lambda page, s=seed: VK2.render_with_crops(page, render_search, start, s, tm,
                                                                                  site, stratum))
            crops = r.pop("value_crops", None)
        else:
            r = in_page(ctx, endpoint, lambda page, s=seed: render_search(page, start, s, tm))
        dump(site, f"seed {seed}", dict(r, term=seed, stratum=stratum))
        ctx.budget.check()
        if r.get("blocked"):
            add_never_contact(site, f"{BENCH_LABEL}{' DEV' if dev else ''} screen render: {r['blocked']}")
            rec.update(screen="blocked", why=str(r["blocked"])[:160])
            return rec
        if stratum == "shop":
            titles, how = product_rows(r, cluster_only=True), "product tiles with a price (one URL pattern)"
            vals = product_values([t for t, _u in titles], site, k=VALUE_POOL)
        else:
            titles, how = result_titles(r.get("dom") or "", str(r.get("url") or ""), seed)
            # content items: titles of >= 3 words (a link of 1-2 words is a section or a control)
            titles = [(t, u) for t, u in titles if len(t.split()) >= 3]
            vals = word_values([t for t, _u in titles], site, seed, k=VALUE_POOL)
        checked: Dict[str, Any] = {}
        if value_clean:
            import web_valuecheck2 as VK2
            if value_check == "openjev" and VALUE_CHECKER is None:
                raise RuntimeError('value_check="openjev" needs a VALUE_CHECKER (execution.value_check.ValueChecker)')
            vals, checked = VK2.cleaned_values(stratum, titles, site, seed, r, vals,
                                               VALUE_CHECKER if value_check == "openjev" else None, VALUE_POOL)
        elif value_check == "openjev":
            import web_valuecheck as VCK
            vals, checked = VCK.checked_values(stratum, titles, site, seed, r, vals, VALUE_CHECKER, VALUE_POOL)
        if value_crops:
            checked = dict(checked, **VK2.save_crops(crops, CROPS_DIR, site, len(tries)))
        tries.append({"seed": seed, "how": r.get("how"), "url": r.get("url"), "titles": [t for t, _u in titles][:20],
                      "region": how, "values": vals, "fresh": r.get("fresh"), **checked})
        if len(vals) >= 3:
            break
    rec["value_tries"] = tries
    if len(vals) < 3:
        rec.update(screen="value_pool", why=f"fewer than 3 values from the result pages ({len(vals)})")
        return rec
    kept, checks = [], []
    for v in vals:
        if len(kept) >= 3:
            break
        if not TR.can_start("value_render", *left(ctx)):
            rec.update(screen="screen_share", why="the screen share ran out while checking the values")
            return rec
        r = in_page(ctx, endpoint, lambda page, s=v: render_search(page, start, s, tm))
        dump(site, f"value {v}", dict(r, term=v, stratum=stratum))
        ctx.budget.check()
        if r.get("blocked"):
            add_never_contact(site, f"{BENCH_LABEL}{' DEV' if dev else ''} screen render: {r['blocked']}")
            rec.update(screen="blocked", why=str(r["blocked"])[:160])
            return rec
        if stratum == "shop":
            rows, how = product_rows(r), "product tiles with a price"
        else:
            rows, how = result_titles(r.get("dom") or "", str(r.get("url") or ""), v)
            rows = [(t, u) for t, u in rows if len(t.split()) >= 3]
        # a result naming >= 75 % of the value's words (the tools' own identity share): a tile's title
        # may shorten or reorder the value ("Tierra Womens Back Up Jacket Relaxed Fit");
        # never on a page stating "no results" (DEV www.myprotein.com: "No search results for
        # ..." above recommended products and a promotion link naming the value)
        naming = [t for t, _u in rows if C.names_value(t, v) or C.name_share(t, v) >= C.MIN_NAME_SHARE]
        nores = C.no_results_statement(r.get("main_text") or r.get("text"))
        checks.append({"value": v, "url": r.get("url"), "rows": len(rows), "naming": len(naming), "region": how,
                       "no_results": nores})
        if naming and not nores:
            kept.append(v)
    rec["value_checks"] = checks
    vals = kept
    if len(vals) < 3:
        rec.update(screen="value_pool", why=f"fewer than 3 values the site's search lists results for ({len(vals)})")
        return rec
    etries = []
    empty = ""
    for term in empty_terms(site):
        if not TR.can_start("value_render", *left(ctx)):
            rec.update(screen="screen_share", why="the screen share ran out before the empty term")
            return rec
        r = in_page(ctx, endpoint, lambda page, s=term: render_search(page, start, s, tm))
        dump(site, f"empty {term}", dict(r, term=term, stratum=stratum))
        ctx.budget.check()
        if r.get("blocked"):
            add_never_contact(site, f"{BENCH_LABEL}{' DEV' if dev else ''} screen render: {r['blocked']}")
            rec.update(screen="blocked", why=str(r["blocked"])[:160])
            return rec
        ok, why = is_empty_page(r, term)
        etries.append({"term": term, "ok": ok, "why": why, "url": r.get("url"), "how": r.get("how")})
        if ok:
            empty = term
            break
    rec["empty_tries"] = etries
    if not empty:
        rec.update(screen="no_empty_state", why="the site did not state 'no results' for the seeded empty terms")
        return rec
    rec["values"] = {"demo": vals[0], "held_out": [vals[1], vals[2], empty], "empty_index": 2,
                     "seed_term": tries[-1]["seed"]}
    return rec


def dump(site: str, kind: str, data: Dict[str, Any]) -> None:
    """DEV only (``DUMP_DIR``): keep a page for the offline re-run of the value rules."""
    if not DUMP_DIR:
        return
    d = R_(DUMP_DIR, slug(site))
    os.makedirs(d, exist_ok=True)
    n = len(os.listdir(d))
    with gzip.open(R_(d, f"{n:02d}_{re.sub(r'[^a-z0-9]+', '_', kind.lower())[:40]}.json.gz"), "wt",
                   encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, default=str)


def variant_host(site: str) -> str:
    return site[4:] if site.startswith("www.") else "www." + site


def screen_one(root: str, site: str, st: str, cat: str, endpoint: str, gate, dev: bool, extra: bool,
               index: int) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"site": site, "frame_host": site, "stratum": st, "category": cat, "index": index,
                           "at": time.strftime("%H:%M:%S"), "extra": extra}
    why = refusal(site) or ("" if dev or site not in dev_sites(root) else "a DEV site (never measured)")
    if not why:
        prior = {ln.strip() for ln in open(R_(root, "screen", PRIOR_CONTACTED_FILE)) if ln.strip()}
        if site in prior or registrable(site) in {registrable(h) for h in prior}:
            why = "contacted by an earlier round"
    if why:
        rec["screen"] = "refused: " + why
        return rec
    ctx, share = phase_ctx(root, site, "screen", endpoint, gate)
    if ctx is None:
        rec["screen"] = "screen_share"
        return rec
    host = site
    # ``web_arch3_live``'s www/apex rule: a robots.txt that redirects
    # off the host -> the site answers on its www / apex variant: the screen goes on there
    try:
        with gate.task_sync(site):
            ctx.robots.load(site)
            note = str(ctx.robots.notes.get(site))
    except TrafficViolation:
        raise
    if "redirect off the host" in note and not refusal(variant_host(site)):
        pre: Dict[str, Any] = {}
        close_ctx(root, ctx, site, "screen", pre)
        rec["robots_frame_host"] = note
        rec["frame_host_requests"] = pre.get("requests")
        host = variant_host(site)
        rec.update(site=host, canonical_from=site)
        ctx, share = phase_ctx(root, host, "screen", endpoint, gate)
        if ctx is None:
            rec["screen"] = "screen_share"
            return rec
    rec["share"] = share
    rec["cache_loaded"] = load_cache(ctx.budget, host)
    try:
        with gate.task_sync(host):
            r, _body = screen_http(root, ctx, host, dev)
            rec.update(r)
            rec["site"] = host
            if rec.get("screen") == "eligible":
                screen_values(ctx, endpoint, host, st, rec, dev)
                if rec.get("screen") == "eligible" and not rec.get("values"):
                    rec["screen"] = "value_pool"
    except TrafficViolation:
        raise
    finally:
        close_ctx(root, ctx, host, "screen", rec)
    return rec


def screened_hosts(d: Dict[str, Any]) -> set:
    return set(d.get("sites", {}).keys()) | {str(r.get("frame_host") or "") for r in d.get("sites", {}).values()}


def cmd_screen(args) -> int:
    global CROPS_DIR
    root = os.path.abspath(args.root)
    dev = bool(args.dev)
    path = screen_path(root, dev)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if VALUE_CROPS and not CROPS_DIR:
        CROPS_DIR = os.path.join(os.path.dirname(path), "crops")
    from anyuse.execution.traffic_gate import SiteGate
    from anyuse.execution.browser_supervisor import BrowserSupervisor
    gate = SiteGate(per_site=1, max_sites=1)
    sup = BrowserSupervisor(args.browser, container=None, log=print)
    if dev:
        frame = load_json(R_(root, "screen", "dev_sites.json"), []) or []
        todo = [(i, e, False) for i, e in enumerate(frame) if not args.site or e["site"] == args.site]
    else:
        frame = load_frame()
        idx = {"shop": 0, "other": 0}
        base = []
        for i, e in enumerate(frame):
            if idx[e["stratum"]] < N_SCREEN[e["stratum"]]:
                base.append((i, e))
            idx[e["stratum"]] += 1
        # the base in lanes; with --extend the lane first re-takes its base hosts not screened (a host
        # whose record was removed for a re-screen), then the extension
        todo = [(i, e, False) for n, (i, e) in enumerate(base) if n % args.parts == args.part_n]
    done = screened_hosts(load_json(path, {"sites": {}}) or {"sites": {}})
    n = 0

    def run(i, e, extra):
        nonlocal n
        if hard_stopped(root):
            print(f"HARD STOP flag ({R_(research_root(root), 'runs', 'HARD_STOP.json')}): not starting {e['site']}",
                  flush=True)
            raise SystemExit(3)
        try:
            sup.require(e["site"])
        except Exception as exc:                        # noqa: BLE001
            print(f"{e['site']:34s} browser down before any request: {exc}; stopping (exit 4)", flush=True)
            raise SystemExit(4)
        try:
            rec = screen_one(root, e["site"], e["stratum"], e["category"], args.browser, gate, dev, extra, i)
        except TrafficViolation:
            raise
        except Exception as exc:                        # noqa: BLE001
            # a page that stalls the browser (or the browser dies) mid-screen: the host is filed
            # 'infrastructure' (not eligible, never re-screened); its traffic was booked (finally)
            rec = {"site": e["site"], "frame_host": e["site"], "stratum": e["stratum"], "category": e["category"],
                   "index": i, "extra": extra, "screen": "infrastructure",
                   "why": f"{type(exc).__name__}: {exc}"[:240], "at": time.strftime("%H:%M:%S")}
            screen_update(path, e["site"], rec)
            print(f"{time.strftime('%H:%M:%S')} {e['site']:34s} INFRASTRUCTURE {rec['why'][:100]}", flush=True)
            ok, why_down = sup.healthy()
            if not ok:
                print(f"browser down after {e['site']}: {why_down}; stopping (exit 4)", flush=True)
                raise SystemExit(4)
            return
        try:
            sup.after_site(e["site"])
        except Exception:                               # noqa: BLE001
            pass
        screen_update(path, rec.get("site") or e["site"], rec)
        n += 1
        vals = rec.get("values") or {}
        print(f"{time.strftime('%H:%M:%S')} {e['site']:34s} {e['stratum']:5s} {rec.get('screen')!s:16s} "
              f"req={rec.get('requests')} values={[vals.get('demo')] + list(vals.get('held_out') or [])}", flush=True)
    try:
        for i, e, extra in todo:
            if e["site"] in done or e["site"] in screened_hosts(load_json(path, {"sites": {}}) or {"sites": {}}):
                continue
            run(i, e, extra)
        if args.extend and not dev:
            # the extension, in frame order and in lanes like the base: every further host
            # of the frame (and its appended supplement) while its stratum has fewer eligible sites than
            # main + 4 reserves
            base_sites = {e["site"] for _i, e in base}
            rest = [(i, e) for i, e in enumerate(frame) if e["site"] not in base_sites]
            for n_, (i, e) in enumerate(rest):
                if n_ % args.parts != args.part_n:
                    continue
                d = load_json(path, {"sites": {}}) or {"sites": {}}
                el = Counter(s.get("stratum") for s in d["sites"].values() if s.get("screen") == "eligible")
                if el[e["stratum"]] >= EXTENSION_TARGET[e["stratum"]]:
                    continue
                if e["site"] in screened_hosts(d):
                    continue
                run(i, e, True)
    except TrafficViolation as exc:
        print(f"TRAFFIC VIOLATION: {exc}", flush=True)
        return 3
    d = load_json(path, {"sites": {}}) or {"sites": {}}
    print({st: {"screened": sum(1 for s in d["sites"].values() if s.get("stratum") == st),
                "eligible": sum(1 for s in d["sites"].values() if s.get("stratum") == st and s.get("screen") == "eligible")}
           for st in ("shop", "other")})
    return 0


# ---------------------------------------------------------------------------
# 5. the cohort, the demonstrators, the frozen values
# ---------------------------------------------------------------------------

def cohort_from(screen: Dict[str, Any]) -> Dict[str, Any]:
    """PURE: eligible sites in frame order per stratum; main = the first 16 shops and 14
    others (a stratum short of eligible sites is filled by the next shops, so shops stay
    >= 50 %); the rest are ordered reserves. Slots interleave the strata; the
    demonstrator of each slot is drawn by the seed, balanced 15 / 15 (shops 8 / 8 and
    others 7 / 7 when full); parts A/B/C alternate."""
    el: Dict[str, List[str]] = {"shop": [], "other": []}
    for site, s in sorted(screen["sites"].items(), key=lambda kv: kv[1].get("index", 1e9)):
        if s.get("screen") == "eligible" and s.get("stratum") in el and s.get("values"):
            el[s["stratum"]].append(site)
    shops = el["shop"][:MAIN["shop"]]
    others = el["other"][:MAIN["other"]]
    short = MAIN["other"] - len(others)
    fill = el["shop"][MAIN["shop"]:MAIN["shop"] + max(0, short)]
    res_shop = el["shop"][MAIN["shop"] + len(fill):]
    fill_o: List[str] = []
    if FILL_SHOP_SLOTS_WITH_OTHERS:
        # shops short -> the next others take the shop slots (the shop share is reported)
        fill_o = el["other"][MAIN["other"]:MAIN["other"] + max(0, MAIN["shop"] - len(shops))]
        others = others + fill_o
    res_other = el["other"][len(others):]
    main_shop = shops + fill
    keyed = [((i + 0.5) / max(1, len(main_shop)), 0, s) for i, s in enumerate(main_shop)] + \
        [((j + 0.5) / max(1, len(others)), 1, s) for j, s in enumerate(others)]
    order = [s for _k, _t, s in sorted(keyed)]
    rng = random.Random(f"{SEED}:demonstrator")
    n_shop, n_other = len(main_shop), len(others)
    d_shop = ["claude"] * (n_shop // 2) + ["codex"] * (n_shop - n_shop // 2)
    d_other = ["codex"] * (n_other // 2) + ["claude"] * (n_other - n_other // 2)
    rng.shuffle(d_shop)
    rng.shuffle(d_other)
    dem = {s: d_shop[i] for i, s in enumerate(main_shop)}
    dem.update({s: d_other[i] for i, s in enumerate(others)})
    part = {s: "ABC"[i % 3] for i, s in enumerate(order)}
    return {"main": {"shop": main_shop, "other": others}, "order": order, "demonstrator": dem, "part": part,
            "reserves": {"shop": res_shop, "other": res_other}, "eligible": {k: len(v) for k, v in el.items()},
            "filled_other_slots_with_shops": fill, "filled_shop_slots_with_others": fill_o}


def cmd_cohort(root: str) -> int:
    screen = load_json(screen_path(root, False), None)
    c = cohort_from(screen)
    save_json(R_(root, "screen", "cohort.json"), c)
    sites = {}
    for s in c["order"] + c["reserves"]["shop"] + c["reserves"]["other"]:
        rec = screen["sites"][s]
        sites[s] = {"stratum": rec["stratum"], "workflow": "product_lookup" if rec["stratum"] == "shop" else "site_search",
                    "demonstrator": c["demonstrator"].get(s), "part": c["part"].get(s),
                    "slot": c["order"].index(s) if s in c["order"] else None, "main": s in c["order"],
                    "demo_value": rec["values"]["demo"], "held_out": rec["values"]["held_out"],
                    "empty_index": rec["values"]["empty_index"], "seed_term": rec["values"]["seed_term"],
                    "oracle_templates": rec.get("search_urls") or [], "start_url": f"https://{s}/"}
    save_json(VALUES_FILE, {"seed": SEED, "spec": SPEC_ID, "sites": sites, "order": c["order"],
                            "reserves": c["reserves"]})
    led = O.RequestLedger(R_(root, "runs", "ledger.json"))
    n = led.schedule([{"site": s, "workflow": sites[s]["workflow"], "demonstrator": sites[s]["demonstrator"],
                       "held_out": sites[s]["held_out"], "empty": sites[s]["empty_index"], "slot": sites[s]["slot"]}
                      for s in c["order"]])
    print(json.dumps({k: c[k] for k in ("eligible", "main", "reserves", "filled_other_slots_with_shops")}, indent=1))
    print(Counter(c["demonstrator"].values()), "scheduled requests:", n)
    return 0


def cmd_cohort_dev(root: str) -> int:
    """DEV: every eligible DEV site, the demonstrators alternating (never measured)."""
    screen = load_json(screen_path(root), {"sites": {}}) or {"sites": {}}
    el = [s for s, r in sorted(screen["sites"].items(), key=lambda kv: kv[1].get("index", 0))
          if r.get("screen") == "eligible" and r.get("values")]
    sites = {}
    for i, s in enumerate(el):
        rec = screen["sites"][s]
        sites[s] = {"stratum": rec["stratum"], "workflow": "product_lookup" if rec["stratum"] == "shop" else "site_search",
                    "demonstrator": DEMONSTRATORS[i % 2], "part": "A", "slot": i, "main": True,
                    "demo_value": rec["values"]["demo"], "held_out": rec["values"]["held_out"],
                    "empty_index": rec["values"]["empty_index"], "seed_term": rec["values"]["seed_term"],
                    "oracle_templates": rec.get("search_urls") or [], "start_url": f"https://{s}/"}
    save_json(R_(root, "dev_values.json"), {"seed": SEED, "dev": True, "sites": sites, "order": el, "reserves": {}})
    O.RequestLedger(R_(root, "runs", "ledger.json")).schedule(
        [{"site": s, "workflow": sites[s]["workflow"], "demonstrator": sites[s]["demonstrator"],
          "held_out": sites[s]["held_out"], "empty": sites[s]["empty_index"]} for s in el])
    print(json.dumps(sites, indent=1, ensure_ascii=False)[:3000])
    return 0


def values_of(dev: bool, root: str) -> Dict[str, Any]:
    vals = load_json(R_(root, "dev_values.json") if dev else VALUES_FILE, {}) or {}
    if not dev:
        # a site replaced before its demonstration (a bot wall at the start) hands its slot to a reserve
        repl = load_json(R_(root, "runs", "replacements.json"), {}) or {}
        for old, r in repl.items():
            new = r.get("in")
            if new and new in vals.get("sites", {}) and old in vals.get("sites", {}):
                vals["sites"][new].update({k: vals["sites"][old].get(k) for k in ("demonstrator", "part", "slot")},
                                          replaces=old, main=True)
    return vals


# ---------------------------------------------------------------------------
# 6. the CLI runners (real HOME, an empty working directory, no key)
# ---------------------------------------------------------------------------

def clean_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
           and not k.startswith(("ANYUSE_", "XDG_", "PYTHON"))}
    env["HOME"] = REAL_HOME
    return env


def _empty_dir() -> str:
    os.makedirs(CLI_EMPTY_DIR, exist_ok=True)
    os.makedirs(CLI_OUT_DIR, exist_ok=True)
    if os.listdir(CLI_EMPTY_DIR):
        raise RuntimeError(f"the CLIs' working directory is not empty: {CLI_EMPTY_DIR}")
    return CLI_EMPTY_DIR


def claude_call(prompt: str, system: str, timeout: int) -> Tuple[str, Dict[str, Any]]:
    cmd = ["claude", "-p", "--model", "sonnet", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--output-format", "json", "--system-prompt", system]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=_empty_dir(), env=clean_env(),
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", {"error": f"timeout {timeout}s", "seconds": round(time.monotonic() - t0, 1)}
    meta: Dict[str, Any] = {"seconds": round(time.monotonic() - t0, 1)}
    if r.returncode != 0:
        meta["error"] = f"rc={r.returncode} {r.stderr[-300:]}"
        return "", meta
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return r.stdout, meta
    meta.update(cost_usd=d.get("total_cost_usd"), usage=d.get("usage"), is_error=d.get("is_error"))
    if d.get("is_error"):
        meta["error"] = str(d.get("result") or "is_error")[:300]
    return str(d.get("result") or ""), meta


def codex_call(prompt: str, system: str, effort: str, timeout: int, tag: str) -> Tuple[str, Dict[str, Any]]:
    empty = _empty_dir()
    out = os.path.join(CLI_OUT_DIR, f"{tag}_{int(time.time() * 1000)}.txt")
    cmd = ["codex", "exec", "-m", "o4-mini", "-c", f'model_reasoning_effort="{effort}"', "--sandbox", "read-only",
           "--skip-git-repo-check", "--ephemeral", "-C", empty, "-o", out, "-"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, input=system + "\n\n" + prompt, capture_output=True, text=True, cwd=empty,
                           env=clean_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", {"error": f"timeout {timeout}s", "seconds": round(time.monotonic() - t0, 1)}
    meta: Dict[str, Any] = {"seconds": round(time.monotonic() - t0, 1)}
    m = re.search(r"tokens used\s*\n?\s*([\d,]+)", r.stderr or "")
    if m:
        meta["usage"] = {"tokens": int(m.group(1).replace(",", ""))}
    if r.returncode != 0:
        meta["error"] = f"rc={r.returncode} {(r.stderr or '')[-300:]}"
        return "", meta
    text = open(out).read() if os.path.exists(out) else r.stdout
    try:
        os.remove(out)
    except OSError:
        pass
    return text, meta


class CallCounter:
    """The bench's CLI call caps across processes (a locked JSON file)."""

    def __init__(self, path: str):
        self.path = path

    def take(self, kind: str, cap: int) -> bool:
        with locked(self.path):
            d = load_json(self.path, {}) or {}
            if int(d.get(kind, 0)) >= cap:
                return False
            d[kind] = int(d.get(kind, 0)) + 1
            save_json(self.path, d)
            return True

    def read(self) -> Dict[str, int]:
        return load_json(self.path, {}) or {}


def demo_cli(who: str) -> Callable[[str], Tuple[str, Dict[str, Any]]]:
    from anyuse.execution import demo_policy as P
    if who == "claude":
        return lambda prompt: claude_call(prompt, P.SYSTEM, DEMO_CLI_TIMEOUT_S)
    return lambda prompt: codex_call(prompt, P.SYSTEM, "low", DEMO_CLI_TIMEOUT_S, "demo")


def reader_cli(demonstrator: str) -> Callable[[str], Tuple[str, Dict[str, Any]]]:
    """The OTHER CLI than the site's demonstrator."""
    if demonstrator == "claude":
        return lambda prompt: codex_call(prompt, "You are a careful, neutral auditor.", "medium",
                                         READER_CLI_TIMEOUT_S, "reader")
    return lambda prompt: claude_call(prompt, "You are a careful, neutral auditor. Answer with one JSON object only.",
                                      READER_CLI_TIMEOUT_S)


# ---------------------------------------------------------------------------
# 7. the demonstration and its compilation
# ---------------------------------------------------------------------------

def out_dir(root: str, dev: bool, *p: str) -> str:
    d = R_(root, "runs", *p)
    os.makedirs(d, exist_ok=True)
    return d


def save_trace(path: str, trace: Dict[str, Any]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(trace, fh, ensure_ascii=False, default=str)


def load_trace(path: str) -> Dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def demo_site(root: str, site: str, entry: Dict[str, Any], endpoint: str, gate, counter: CallCounter,
              dev: bool, log) -> Dict[str, Any]:
    from anyuse.execution import demo_policy as P
    rec: Dict[str, Any] = {"site": site, "demonstrator": entry["demonstrator"], "workflow": entry["workflow"],
                           "started": time.strftime("%H:%M:%S")}
    ctx, share = phase_ctx(root, site, "demo", endpoint, gate)
    if ctx is None:
        return dict(rec, status="not_run", why="the demonstration share is exhausted")
    rec["share"] = share
    rec["cache_loaded"] = load_cache(ctx.budget, site)
    kind, cap = ("dev_demo", DEV_CALL_CAP) if dev else ("demo", DEMO_CALL_CAP)
    cli = demo_cli(entry["demonstrator"])
    trace: Dict[str, Any] = {}
    try:
        with gate.task_sync(site):
            ctx.robots.load(site)
            if not ctx.robots.allow(entry["start_url"]):
                return dict(rec, status="not_run", why="robots.txt refuses the start page now")
            trace = in_page(ctx, endpoint, lambda page: P.run_demo(
                page, entry["workflow"], entry["demo_value"], entry["start_url"], site, cli, settle(),
                cli_name=CLI_LABEL[entry["demonstrator"]], budget=lambda: counter.take(kind, cap)))
            ctx.budget.check()
    except TrafficViolation:
        raise
    finally:
        close_ctx(root, ctx, site, "demo", rec)
    d = out_dir(root, dev, "demos")
    tpath = R_(d, f"{slug(site)}.trace.json.gz")
    save_trace(tpath, trace)
    rec.update(stop=trace.get("stop"), calls=trace.get("calls"), seconds=trace.get("seconds"),
               cli_seconds=trace.get("cli_seconds"), trace=os.path.basename(tpath),
               cost_usd=round(sum(float((m or {}).get("cost_usd") or 0) for m in trace.get("cli_meta") or []), 4),
               tokens=sum(int(((m or {}).get("usage") or {}).get("tokens") or
                              (((m or {}).get("usage") or {}).get("input_tokens") or 0) +
                              (((m or {}).get("usage") or {}).get("output_tokens") or 0)) for m in trace.get("cli_meta") or []),
               fresh=(trace.get("fresh") or {}).get("fresh"))
    if trace.get("stop") == "blocked":
        rec["blocked"] = trace.get("blocked")
    valid, why, det = C.demo_validity(load_trace(tpath))
    rec.update(valid=valid, validity=why, validity_detail=det)
    # the compilation: the saved trace only
    got = C.compile_trace(load_trace(tpath), version=COMPILE_VERSION)
    rec["compile"] = {"ok": got["ok"], "why": got.get("why"), "report": got.get("report")}
    if got["ok"]:
        tool = got["tool"]
        save_json(R_(out_dir(root, dev, "tools"), f"{slug(site)}.tool.json"), tool)
        rec["tool_sha256"] = C.tool_digest(tool)
    rec["status"] = "done"
    log(f"{site:32s} demo[{entry['demonstrator']}] stop={trace.get('stop')} calls={trace.get('calls')} "
        f"valid={valid} ({why[:70]}) compile={got['ok']} {str(got.get('why') or '')[:70]} req={rec.get('requests')}")
    return rec


def load_tool(root: str, dev: bool, site: str, demo_rec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """The site's compiled tool, or why there is none. A tool is used only when its
    demonstration is valid (an invalid demonstration yields no tool)."""
    if not demo_rec or demo_rec.get("status") != "done":
        return None, "no demonstration ran" + (f" ({demo_rec.get('why')})" if demo_rec else "")
    if not demo_rec.get("valid"):
        return None, f"invalid demonstration: {demo_rec.get('validity')}"
    if not (demo_rec.get("compile") or {}).get("ok"):
        return None, f"compile failed: {(demo_rec.get('compile') or {}).get('why')}"
    t = load_json(R_(root, "runs", "tools", f"{slug(site)}.tool.json"), None)
    if not t or C.tool_digest(t) != demo_rec.get("tool_sha256"):
        return None, "the tool file does not match its compile record"
    return t, ""


# ---------------------------------------------------------------------------
# 8. a wave: the tool, the oracle, tier 1, the blind reader, the ledger
# ---------------------------------------------------------------------------

def hard_stop_path(root: str, dev: bool = False) -> str:
    return R_(root, "runs", "HARD_STOP.json")


def wave_site(root: str, wave: str, site: str, entry: Dict[str, Any], demo_rec: Dict[str, Any], endpoint: str,
              gate, counter: CallCounter, dev: bool, log) -> Dict[str, Any]:
    ledger = O.RequestLedger(R_(root, "runs", "ledger.json"))
    rec: Dict[str, Any] = {"site": site, "wave": wave, "started": time.strftime("%H:%M:%S")}
    tool, why_none = load_tool(root, dev, site, demo_rec)
    order = [1, 2, 3]
    random.Random(f"{SEED}:order:{site}").shuffle(order)
    rec["order"] = order
    ev_dir = out_dir(root, dev, wave, slug(site))
    if tool is None:
        for k in order:
            ledger.set(O.request_id(wave, site, k), not_run=f"no tool: {why_none}")
        rec.update(status="done", outcome="no_tool", why=why_none)
        return rec
    ctx, share = phase_ctx(root, site, wave, endpoint, gate)
    if ctx is None:
        for k in order:
            ledger.set(O.request_id(wave, site, k), not_run="the wave's share is exhausted")
        return dict(rec, status="done", outcome="share_exhausted")
    rec["share"] = share
    if SITE_HOSTS_FROM_TOOL:
        rec["origin_policy"] = T.apply_site_hosts(ctx.budget, tool)
    rec["cache_loaded"] = load_cache(ctx.budget, site)
    cases: List[Dict[str, Any]] = []
    accepted: List[str] = []
    blocked = ""
    try:
        with gate.task_sync(site):
            ctx.robots.load(site)
            for h in (rec.get("origin_policy") or {}).get(site) or []:
                if not refusal(h):
                    ctx.robots.load(h)
            for k in order:
                rid = O.request_id(wave, site, k)
                value = entry["held_out"][k - 1]
                if hard_stopped(root):
                    ledger.set(rid, not_run="hard stop")
                    continue
                if blocked:
                    ledger.set(rid, not_run=f"blocked: {blocked[:80]}")
                    continue
                if not TR.can_start("tool", *left(ctx)):
                    ledger.set(rid, not_run="the wave's share is exhausted")
                    continue
                out = in_page(ctx, endpoint, lambda page, v=value: T.run_tool(page, tool, v, settle(), RESULT_WAIT_S))
                ctx.budget.check()
                save_json(R_(ev_dir, f"v{k}_tool.json"), out)
                ledger.set(rid, tool_status=out["status"], accepted=out.get("accepted"), tool_why=str(out.get("why"))[:300],
                           output=out.get("output"), search_via=out.get("search_via"),
                           fresh=(out.get("fresh") or {}).get("fresh"), model_calls=out.get("model_calls"),
                           seconds=out.get("seconds"),
                           **({"infra_retries": out["infra_retries"]} if out.get("infra_retries") else {}))
                if out["status"] == "blocked":
                    blocked = str(out.get("why"))
                    continue
                if not TR.can_start("oracle_search", *left(ctx)):
                    ledger.set(rid, oracle="not run: the wave's share is exhausted")
                    continue
                ev = in_page(ctx, endpoint, lambda page, v=value: O.oracle_search(
                    page, entry["start_url"], v, entry.get("oracle_templates") or [], settle()))
                ctx.budget.check()
                if ev.get("how") == "blocked":
                    blocked = str(ev.get("blocked"))
                    ledger.set(rid, oracle=f"blocked: {blocked[:80]}")
                    continue
                item_ev = None
                purl = str((out.get("output") or {}).get("url") or "") if out.get("status") == "ok" else ""
                if purl and entry["workflow"] == "product_lookup" and TR.can_start("oracle_item", *left(ctx)):
                    item_ev = in_page(ctx, endpoint, lambda page, u=purl: O.oracle_item(page, u, settle()))
                    ctx.budget.check()
                    if item_ev.get("blocked"):
                        blocked = str(item_ev.get("blocked"))
                        item_ev = None
                save_json(R_(ev_dir, f"v{k}_oracle.json"), {"search": ev, "item": item_ev})
                oretry = list(ev.get("infra_retries") or []) + list((item_ev or {}).get("infra_retries") or [])
                ledger.set(rid, oracle=ev.get("how"), oracle_fresh=(ev.get("fresh") or {}).get("fresh"),
                           oracle_item=bool(item_ev), **({"oracle_infra_retries": oretry} if oretry else {}))
                if wave == "immediate" and TR.can_start("http", *left(ctx)):
                    def get(url):
                        if not ctx.allowed(url):
                            return None, ""
                        g = ctx.fetch.get(url)
                        ctx.budget.add_bytes(site, L3.wire_bytes(g))
                        return g.status, g.body or ""
                    h = O.http_tier1(tool, value, get, out)
                    ledger.set(rid, http=h)
                cases.append(O.reader_case(rid, entry["workflow"], value, out, ev, item_ev))
                if out.get("accepted"):
                    accepted.append(rid)
    except TrafficViolation:
        raise
    finally:
        close_ctx(root, ctx, site, wave, rec)
    if blocked:
        rec["blocked"] = blocked
        add_never_contact(site, f"{BENCH_LABEL}{' DEV' if dev else ''} {wave}: {blocked}")
    # the blind reader: ONE call per site per wave, the OTHER CLI than the demonstrator (DEV: only with
    # --dev-reader, counted against the DEV share of the demonstration calls)
    if cases and (not dev or DEV_READER):
        kind, cap = (f"reader_{wave}", READER_CALL_CAP) if not dev else ("dev_demo", DEV_CALL_CAP)
        if counter.take(kind, cap):
            prompt = O.reader_prompt(cases)
            again = None
            if READER_ONCE_MORE:
                # a truncated case, or one without a final verdict, is asked once more
                got = O.ask_reader(reader_cli(entry["demonstrator"]), cases, last=READER_PARSE_LAST,
                                   budget=lambda: counter.take(kind, cap))
                text, meta, verdicts, again = got["text"], got["meta"], got["verdicts"], got["again"]
            else:
                text, meta = reader_cli(entry["demonstrator"])(prompt)
                ids = [c["id"] for c in cases]
                verdicts = O.parse_reader(text, ids, last=True) if READER_PARSE_LAST else O.parse_reader(text, ids)
            rj = {"model": READER_LABEL[entry["demonstrator"]], "meta": meta,
                  "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "answer": text, "verdicts": verdicts}
            if again is not None:
                rj["again"] = again
            save_json(R_(ev_dir, "reader.json"), rj)
            for rid, v in verdicts.items():
                ledger.set(rid, audit=dict(v, model=READER_LABEL[entry["demonstrator"]]))
            wrong = [rid for rid in accepted if verdicts.get(rid, {}).get("verdict") == "wrong"]
            rec["reader"] = {"calls": 2 if (again or {}).get("asked") else 1, "seconds": meta.get("seconds"),
                             "error": meta.get("error")}
            if wrong and WRONG_OUTPUT_HARD_STOP:
                save_json(hard_stop_path(root, dev), {"why": "a wrong accepted output", "requests": wrong,
                                                      "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                ledger.event("hard_stop", requests=wrong)
                log(f"HARD STOP: a wrong accepted output ({wrong})")
            elif wrong:
                record_wrong(root, site, wave, wrong, verdicts)
                ledger.event("wrong_output", requests=wrong)
                log(f"WRONG OUTPUT (a measured error, recorded; the round goes on): {wrong}")
        else:
            rec["reader"] = {"calls": 0, "why": "the reader call cap was reached"}
    rec.update(status="done", outcome="blocked" if blocked else "done")
    log(f"{site:32s} {wave}: " + ", ".join(f"v{k}={(load_json(R_(ev_dir, f'v{k}_tool.json'), {}) or {}).get('status')}"
                                           for k in order) + f" req={rec.get('requests')}")
    return rec


def record_wrong(root: str, site: str, wave: str, wrong: Sequence[str], verdicts: Dict[str, Any]) -> None:
    """A wrong accepted output when it does not stop the run (``WRONG_OUTPUT_HARD_STOP`` off):
    appended to ``runs/WRONG_OUTPUTS.json`` with the reader's verdicts (the audit)."""
    p = R_(root, "runs", "WRONG_OUTPUTS.json")
    with locked(p):
        d = load_json(p, {"wrong": []}) or {"wrong": []}
        for rid in wrong:
            d["wrong"].append({"request": rid, "site": site, "wave": wave, "verdict": verdicts.get(rid),
                               "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        save_json(p, d)


# ---------------------------------------------------------------------------
# 9. the process: one part of the sites, resumable, a state file per command
# ---------------------------------------------------------------------------

def cmd_sites(args) -> int:
    root = os.path.abspath(args.root)
    dev = bool(args.dev)
    from anyuse.execution.traffic_gate import SiteGate
    from anyuse.execution.browser_supervisor import BrowserSupervisor, confirm
    vals = values_of(dev, root)
    if not vals.get("sites"):
        raise SystemExit("refused: the frozen values are missing")
    tag = args.cmd if args.cmd == "demo" else f"wave_{args.wave}"
    sdir = out_dir(root, dev, "state")
    state_path = R_(sdir, f"{tag}_{args.part}.json")
    head = subprocess.run(["git", "-C", os.path.dirname(os.path.dirname(HERE)), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    state = load_json(state_path, None) or {"spec": SPEC_ID, "cmd": tag, "part": args.part, "code": head,
                                            "entries": {}, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    state["code_now"] = head
    logf = open(R_(sdir, f"{tag}_{args.part}.log"), "a")

    def log(msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    def write():
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_json(state_path, state)
    counter = CallCounter(R_(root, "runs", "cli_calls.json"))
    gate = SiteGate(per_site=1, max_sites=1)
    sup = BrowserSupervisor(args.browser, container=None, log=log)
    if dev:
        queue = [args.site] if args.site else sorted(vals["sites"])
    else:
        queue = [s for s in vals["order"] if vals["sites"][s].get("part") == args.part]
        repl = load_json(R_(root, "runs", "replacements.json"), {}) or {}
        queue = [repl.get(s, {}).get("in") or s for s in queue]
    demos = {}
    if args.cmd == "wave":
        for p in "ABC":
            d = load_json(R_(sdir, f"demo_{p}.json"), {}) or {}
            demos.update(d.get("entries") or {})
    try:
        while queue:
            site = queue.pop(0)
            prior = state["entries"].get(site) or {}
            if prior.get("status") == "done":
                continue
            if prior.get("status") == "started":
                prior.update(status="done", outcome="infrastructure", why="the process stopped mid-site; not re-run")
                if args.cmd == "wave":
                    led = O.RequestLedger(R_(root, "runs", "ledger.json"))
                    for k in (1, 2, 3):
                        rid = O.request_id(args.wave, site, k)
                        if not (led.data()["requests"].get(rid) or {}).get("tool_status"):
                            led.set(rid, not_run="infrastructure (the process stopped mid-site)")
                write()
                continue
            if hard_stopped(root) and args.cmd == "demo":
                log(f"HARD STOP flag: not starting {site}")
                return 3
            if hard_stopped(root) and args.cmd == "wave":
                log(f"HARD STOP flag: not starting {site}")
                led = O.RequestLedger(R_(root, "runs", "ledger.json"))
                for k in (1, 2, 3):
                    rid = O.request_id(args.wave, site, k)
                    if not (led.data()["requests"].get(rid) or {}).get("tool_status"):
                        led.set(rid, not_run="hard stop")
                state["entries"][site] = {"site": site, "status": "done", "outcome": "hard_stop"}
                write()
                continue
            why = refusal(site)
            if why:
                state["entries"][site] = {"site": site, "status": "done", "outcome": "skipped", "why": why}
                write()
                continue
            entry = vals["sites"][site]
            try:
                sup.require(site)
            except Exception as exc:                    # noqa: BLE001
                log(f"{site:32s} browser down before any request: {exc}; stopping (exit 4)")
                return 4
            state["entries"][site] = {"site": site, "status": "started", "started": time.strftime("%H:%M:%S")}
            write()
            m0, w0 = time.monotonic(), time.time()
            try:
                if args.cmd == "demo":
                    rec = demo_site(root, site, entry, args.browser, gate, counter, dev, log)
                    if rec.get("stop") == "blocked" and not dev and _blocked_at_start(root, site):
                        # a bot wall on the start page before any action: the site is not enrolled and
                        # is replaced by the next reserve of its stratum (never bypassed)
                        nxt = take_reserve(root, entry["stratum"], site)
                        rec["replaced_by"] = nxt
                        add_never_contact(site, f"{BENCH_LABEL} demonstration: a bot wall on the start page")
                        log(f"{site:32s} BOT WALL at the start -> replaced by {nxt}")
                        if nxt:
                            vals = values_of(dev, root)
                            queue.insert(0, nxt)
                else:
                    rec = wave_site(root, args.wave, site, entry, demos.get(site) or {}, args.browser, gate, counter,
                                    dev, log)
            except TrafficViolation:
                raise
            except Exception as exc:                    # noqa: BLE001
                infra = confirm(exc, args.browser)
                rec = {"site": site, "status": "done", "outcome": "infrastructure", "why": f"{type(exc).__name__}: {exc}"[:240]}
                log(f"{site:32s} INFRASTRUCTURE mid-site (not re-run): {exc}")
                if args.cmd == "wave":
                    led = O.RequestLedger(R_(root, "runs", "ledger.json"))
                    for k in (1, 2, 3):
                        rid = O.request_id(args.wave, site, k)
                        if not (led.data()["requests"].get(rid) or {}).get("tool_status"):
                            led.set(rid, not_run="infrastructure")
                if infra is None:
                    state["entries"][site] = rec
                    write()
                    raise
            finally:
                try:
                    sup.after_site(site)
                except Exception:                       # noqa: BLE001
                    pass
            slept = (time.time() - w0) - (time.monotonic() - m0)
            if slept > 30:
                rec["host_sleep_s"] = round(slept, 1)
                log(f"{site:32s} HOST SLEEP {slept:.0f} s during the site (reported)")
            state["entries"][site] = rec
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


def _blocked_at_start(root: str, site: str) -> bool:
    """Was the demonstration's block on its first page (before any action)?"""
    try:
        tr = load_trace(R_(root, "runs", "demos", f"{slug(site)}.trace.json.gz"))
    except Exception:                                  # noqa: BLE001
        return False
    return tr.get("stop") == "blocked" and not tr.get("steps")


def take_reserve(root: str, stratum: str, replaced: str) -> Optional[str]:
    """The next unused reserve of the stratum takes the replaced site's slot (its
    demonstrator and part; its own values drawn at the screen); the ledger marks the
    replaced site's requests and schedules the reserve's (the denominator stays 90)."""
    p = R_(root, "runs", "replacements.json")
    vals = load_json(VALUES_FILE, {}) or {}
    with locked(p):
        used = load_json(p, {}) or {}
        taken = {v.get("in") for v in used.values()}
        nxt = None
        for site in (vals.get("reserves") or {}).get(stratum) or []:
            if site in taken or refusal(site):
                continue
            nxt = site
            break
        used[replaced] = {"in": nxt, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "why": "bot wall on the start page"}
        save_json(p, used)
    led = O.RequestLedger(R_(root, "runs", "ledger.json"))
    for wave in ("immediate", "delayed"):
        for k in (1, 2, 3):
            led.set(O.request_id(wave, replaced, k), replaced_by=nxt or "(no reserve left)")
    if nxt:
        e = vals["sites"][nxt]
        led.schedule([{"site": nxt, "workflow": e["workflow"], "demonstrator": vals["sites"][replaced]["demonstrator"],
                       "held_out": e["held_out"], "empty": e["empty_index"], "slot": vals["sites"][replaced]["slot"]}])
    led.event("replacement", out=replaced, into=nxt)
    return nxt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("frame", "frame_supplement", "screen", "cohort", "demo", "wave", "report"))
    ap.add_argument("root")
    ap.add_argument("--part", default="A")
    ap.add_argument("--parts", type=int, default=3)
    ap.add_argument("--browser", default="")
    ap.add_argument("--wave", choices=("immediate", "delayed"), default="immediate")
    ap.add_argument("--extend", action="store_true")
    ap.add_argument("--dev", action="store_true")
    ap.add_argument("--site", default="")
    ap.add_argument("--dev-reader", action="store_true")
    a = ap.parse_args(argv)
    a.part_n = "ABC".index(a.part) if a.part in "ABC" else int(a.part)
    a.root = os.path.abspath(a.root)
    if a.dev:
        # DEV (never measured): every file under <R>/dev, the frame's DEV sites only
        import shutil
        research = a.root
        a.root = R_(research, "dev")
        os.makedirs(R_(a.root, "screen"), exist_ok=True)
        for f in ("dev_sites.json", PRIOR_CONTACTED_FILE):
            if os.path.exists(R_(research, "screen", f)):
                shutil.copy(R_(research, "screen", f), R_(a.root, "screen", f))
    os.makedirs(R_(a.root, "screen"), exist_ok=True)
    os.makedirs(R_(a.root, "runs"), exist_ok=True)
    global DUMP_DIR, DEV_READER
    DEV_READER = bool(a.dev and a.dev_reader)
    if a.dev and a.cmd == "screen":
        DUMP_DIR = R_(a.root, "dumps")
    if a.cmd == "frame":
        return cmd_frame(os.path.abspath(a.root))
    if a.cmd == "frame_supplement":
        return cmd_frame_supplement(os.path.abspath(a.root))
    if a.cmd == "cohort":
        return cmd_cohort_dev(a.root) if a.dev else cmd_cohort(a.root)
    if a.cmd == "report":
        import web_arch4_report as REP
        return REP.main(os.path.abspath(a.root))
    if not a.browser:
        raise SystemExit(f"{a.cmd} needs --browser")
    if a.cmd == "screen":
        return cmd_screen(a)
    return cmd_sites(a)


if __name__ == "__main__":
    raise SystemExit(main())
