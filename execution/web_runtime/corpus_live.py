"""The corpus of the ``live_bench`` measurement (ARM S vs ARM L), fixed BEFORE any of it was fetched.

Nothing here may change after the first request. Every host was chosen
from memory, before any request, and is absent (by registrable domain) from
the prior-host snapshot and from the measurement harness.

Shape of an entry::

    {"site", "stratum",     # expected search type, from memory: get | js | post
     "start_url",          # the page whose search the task uses (same host)
     "listing_url"}        # the site's own listing the query rule reads (same host)

Per stratum, the list order is the draw order: the first entries are the main
pool (cohort 1 takes the first half of it, cohort 2 the second half), the rest
are that stratum's reserves, used in order. The arm of every main entry comes
from :func:`assign` (seeded shuffle inside cohort x stratum, then alternating
S, L), so it is fixed now and printed by ``live_bench.py plan``.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import unicodedata
from typing import Dict, List

SEED = "anyuse-live-2026-11-03"
#: main sites per cohort and stratum (pairs: half S, half L)
PER_COHORT = {"get": 10, "js": 8, "post": 6}
COHORTS = (1, 2)
N_VALUES = 5            # q1 compile (the agent's task), q2 selection, q3+q4 validation, q5 MCP
POOL = 30               # the seeded draw is over the first POOL qualifying tokens


def _e(site, stratum, start, listing):
    return {"site": site, "stratum": stratum, "start_url": start, "listing_url": listing}


GET = [
    _e("www.behindthename.com", "get", "https://www.behindthename.com/",
       "https://www.behindthename.com/names/usage/english"),
    _e("www.fontsquirrel.com", "get", "https://www.fontsquirrel.com/",
       "https://www.fontsquirrel.com/fonts/list/popular"),
    _e("fontlibrary.org", "get", "https://fontlibrary.org/en", "https://fontlibrary.org/en/catalogue"),
    _e("www.allaboutbirds.org", "get", "https://www.allaboutbirds.org/guide/",
       "https://www.allaboutbirds.org/guide/browse/taxonomy"),
    _e("www.tvmaze.com", "get", "https://www.tvmaze.com/", "https://www.tvmaze.com/shows"),
    _e("www.tate.org.uk", "get", "https://www.tate.org.uk/", "https://www.tate.org.uk/art/artists"),
    _e("www.e-codices.unifr.ch", "get", "https://www.e-codices.unifr.ch/en",
       "https://www.e-codices.unifr.ch/en/list"),
    _e("www.zeno.org", "get", "https://www.zeno.org/", "https://www.zeno.org/Literatur"),
    _e("www.deutschestextarchiv.de", "get", "https://www.deutschestextarchiv.de/",
       "https://www.deutschestextarchiv.de/list"),
    _e("iep.utm.edu", "get", "https://iep.utm.edu/", "https://iep.utm.edu/a/"),
    _e("www.nhs.uk", "get", "https://www.nhs.uk/", "https://www.nhs.uk/conditions/"),
    _e("eudml.org", "get", "https://eudml.org/", "https://eudml.org/journals"),
    _e("in-the-sky.org", "get", "https://in-the-sky.org/",
       "https://in-the-sky.org/data/constellations_list.php"),
    _e("wiki.osdev.org", "get", "https://wiki.osdev.org/Expanded_Main_Page",
       "https://wiki.osdev.org/Special:AllPages"),
    _e("www.dokuwiki.org", "get", "https://www.dokuwiki.org/dokuwiki", "https://www.dokuwiki.org/plugins"),
    _e("collections.mfa.org", "get", "https://collections.mfa.org/", "https://collections.mfa.org/collections"),
    _e("paperity.org", "get", "https://paperity.org/", "https://paperity.org/journals/"),
    _e("www.ensembl.org", "get", "https://www.ensembl.org/index.html",
       "https://www.ensembl.org/info/about/species.html"),
    _e("www.erudit.org", "get", "https://www.erudit.org/en/", "https://www.erudit.org/en/journals/"),
    _e("elifesciences.org", "get", "https://elifesciences.org/", "https://elifesciences.org/subjects"),
    # reserves
    _e("www.jstage.jst.go.jp", "get", "https://www.jstage.jst.go.jp/browse/-char/en",
       "https://www.jstage.jst.go.jp/browse/-char/en"),
    _e("www.digitalcommonwealth.org", "get", "https://www.digitalcommonwealth.org/",
       "https://www.digitalcommonwealth.org/collections"),
    _e("texashistory.unt.edu", "get", "https://texashistory.unt.edu/",
       "https://texashistory.unt.edu/explore/collections/"),
    _e("www.compart.com", "get", "https://www.compart.com/en/unicode/",
       "https://www.compart.com/en/unicode/block"),
    _e("www.poetryfoundation.org", "get", "https://www.poetryfoundation.org/",
       "https://www.poetryfoundation.org/poets"),
    _e("openclipart.org", "get", "https://openclipart.org/", "https://openclipart.org/"),
    _e("foldoc.org", "get", "https://foldoc.org/", "https://foldoc.org/"),
    _e("avibase.bsc-eoc.org", "get", "https://avibase.bsc-eoc.org/avibase.jsp?lang=EN",
       "https://avibase.bsc-eoc.org/avibase.jsp?lang=EN"),
]

JS = [
    _e("caniuse.com", "js", "https://caniuse.com/", "https://caniuse.com/ciu/index"),
    _e("www.protondb.com", "js", "https://www.protondb.com/", "https://www.protondb.com/explore"),
    _e("exoplanet.eu", "js", "https://exoplanet.eu/home/", "https://exoplanet.eu/catalog/"),
    _e("fonts.bunny.net", "js", "https://fonts.bunny.net/", "https://fonts.bunny.net/"),
    _e("phoible.org", "js", "https://phoible.org/", "https://phoible.org/languages"),
    _e("docs.sympy.org", "js", "https://docs.sympy.org/latest/index.html",
       "https://docs.sympy.org/latest/reference/index.html"),
    _e("docs.qgis.org", "js", "https://docs.qgis.org/latest/en/docs/index.html",
       "https://docs.qgis.org/latest/en/docs/user_manual/index.html"),
    _e("www.statsmodels.org", "js", "https://www.statsmodels.org/stable/index.html",
       "https://www.statsmodels.org/stable/user-guide.html"),
    _e("docs.opencv.org", "js", "https://docs.opencv.org/4.x/", "https://docs.opencv.org/4.x/modules.html"),
    _e("docs.gtk.org", "js", "https://docs.gtk.org/gtk4/", "https://docs.gtk.org/gtk4/"),
    _e("docs.sqlalchemy.org", "js", "https://docs.sqlalchemy.org/en/20/",
       "https://docs.sqlalchemy.org/en/20/core/index.html"),
    _e("docs.celeryq.dev", "js", "https://docs.celeryq.dev/en/stable/",
       "https://docs.celeryq.dev/en/stable/reference/index.html"),
    _e("docs.scrapy.org", "js", "https://docs.scrapy.org/en/latest/",
       "https://docs.scrapy.org/en/latest/index.html"),
    _e("docs.zephyrproject.org", "js", "https://docs.zephyrproject.org/latest/",
       "https://docs.zephyrproject.org/latest/samples/index.html"),
    _e("docs.mesa3d.org", "js", "https://docs.mesa3d.org/", "https://docs.mesa3d.org/"),
    _e("matplotlib.org", "js", "https://matplotlib.org/stable/", "https://matplotlib.org/stable/api/index.html"),
    # reserves
    _e("scikit-learn.org", "js", "https://scikit-learn.org/stable/",
       "https://scikit-learn.org/stable/api/index.html"),
    _e("pandas.pydata.org", "js", "https://pandas.pydata.org/docs/",
       "https://pandas.pydata.org/docs/reference/index.html"),
    _e("networkx.org", "js", "https://networkx.org/documentation/stable/",
       "https://networkx.org/documentation/stable/reference/index.html"),
    _e("api.rubyonrails.org", "js", "https://api.rubyonrails.org/", "https://api.rubyonrails.org/"),
    _e("simpleicons.org", "js", "https://simpleicons.org/", "https://simpleicons.org/"),
    _e("www.wikiart.org", "js", "https://www.wikiart.org/", "https://www.wikiart.org/en/artists-by-art-movement"),
]

POST = [
    _e("www.mobileread.com", "post", "https://www.mobileread.com/forums/", "https://www.mobileread.com/forums/"),
    _e("anilist.co", "post", "https://anilist.co/", "https://anilist.co/search/anime"),
    _e("community.mybb.com", "post", "https://community.mybb.com/", "https://community.mybb.com/"),
    _e("www.simplemachines.org", "post", "https://www.simplemachines.org/community/index.php",
       "https://www.simplemachines.org/community/index.php"),
    _e("www.eevblog.com", "post", "https://www.eevblog.com/forum/", "https://www.eevblog.com/forum/"),
    _e("forum.lazarus.freepascal.org", "post", "https://forum.lazarus.freepascal.org/index.php",
       "https://forum.lazarus.freepascal.org/index.php"),
    _e("forums.spacebattles.com", "post", "https://forums.spacebattles.com/",
       "https://forums.spacebattles.com/"),
    _e("forum.pine64.org", "post", "https://forum.pine64.org/", "https://forum.pine64.org/"),
    _e("forum.doom9.org", "post", "https://forum.doom9.org/", "https://forum.doom9.org/"),
    _e("forum.videohelp.com", "post", "https://forum.videohelp.com/", "https://forum.videohelp.com/"),
    _e("xenforo.com", "post", "https://xenforo.com/community/", "https://xenforo.com/community/"),
    _e("appdb.winehq.org", "post", "https://appdb.winehq.org/",
       "https://appdb.winehq.org/objectManager.php?sClass=vendor&sTitle=Browse+Vendors"),
    # reserves
    _e("forums.sufficientvelocity.com", "post", "https://forums.sufficientvelocity.com/",
       "https://forums.sufficientvelocity.com/"),
    _e("www.gesetze-im-internet.de", "post", "https://www.gesetze-im-internet.de/",
       "https://www.gesetze-im-internet.de/aktuell.html"),
    _e("www.bailii.org", "post", "https://www.bailii.org/", "https://www.bailii.org/databases.html"),
    _e("www.austlii.edu.au", "post", "https://www.austlii.edu.au/", "https://www.austlii.edu.au/databases.html"),
]

STRATA = {"get": GET, "js": JS, "post": POST}
#: DEV pilot only: two ``corpus_sample5`` hosts where the
#: attach path proved; never part of a cohort, never reported as a result
PILOT = {"www.sqlite.org": _e("www.sqlite.org", "get", "https://www.sqlite.org/search",
                              "https://www.sqlite.org/docs.html"),
         "ocaml.org": _e("ocaml.org", "get", "https://ocaml.org/packages",
                         "https://ocaml.org/packages")}
BY_SITE = {e["site"]: e for rows in STRATA.values() for e in rows}
BY_SITE.update(PILOT)


def main_pool(stratum: str) -> List[dict]:
    return STRATA[stratum][:PER_COHORT[stratum] * len(COHORTS)]


def reserves(stratum: str) -> List[dict]:
    return STRATA[stratum][PER_COHORT[stratum] * len(COHORTS):]


def assign() -> Dict[str, dict]:
    """site -> {"cohort", "arm", "stratum"} for every MAIN entry (fixed, seeded)."""
    out: Dict[str, dict] = {}
    for stratum, n in PER_COHORT.items():
        pool = main_pool(stratum)
        for ci, cohort in enumerate(COHORTS):
            chunk = [e["site"] for e in pool[ci * n:(ci + 1) * n]]
            rng = random.Random(f"{SEED}:{stratum}:{cohort}")
            rng.shuffle(chunk)
            for i, site in enumerate(chunk):
                out[site] = {"cohort": cohort, "arm": "S" if i % 2 == 0 else "L",
                             "stratum": stratum}
    return out


def queue(cohort: int, arm: str) -> List[str]:
    """The main sites of one cohort x arm, in run order (stratum order, then seeded)."""
    a = assign()
    rows = [s for s, v in a.items() if v["cohort"] == cohort and v["arm"] == arm]
    rank = {e["site"]: i for i, e in enumerate(GET + JS + POST)}
    return sorted(rows, key=lambda s: rank[s])


# ---------------------------------------------------------------------------
# the query rule -- pure, offline-testable
# ---------------------------------------------------------------------------

#: interface words that are never a query (``corpus_sample5``'s list, unchanged)
STOPWORDS = frozenset("""
about all back browse cancel close contact cookies deutsch download english
espanol español first français help home last login log logout menu more next page
prev previous privacy register search show sign skip terms top view
""".split())
MIN_ROWS = 5
_EDGE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_OK = re.compile(r"^[^\W_][\w.+\-]*$", re.UNICODE)


def token_of(link_text: str) -> str:
    parts = (link_text or "").split()
    if not parts:
        return ""
    return _EDGE.sub("", unicodedata.normalize("NFC", parts[0]))


def qualifying_tokens(listing_dom: str, limit: int = POOL) -> List[str]:
    """``corpus_sample5``'s rule, steps 1-4, keeping up to ``limit`` tokens in document order."""
    from anyuse.execution.html_extract import parse, repeated_groups
    root = parse(listing_dom or "")

    def first_link(node):
        for d in node.walk():
            if d.tag == "a" and d.text():
                return d
        return None

    def in_chrome(node):
        cur = node.parent
        while cur is not None:
            if cur.tag in ("nav", "header", "footer"):
                return True
            cur = cur.parent
        return False

    for rows in repeated_groups(root, MIN_ROWS):
        if in_chrome(rows[0]):
            continue
        links = [first_link(r) for r in rows]
        if sum(1 for a in links if a is not None) < 0.8 * len(rows):
            continue
        out: List[str] = []
        seen = set()
        for a in links:
            if a is None:
                continue
            tok = token_of(a.text())
            low = tok.casefold()
            if not (3 <= len(tok) <= 30) or not _OK.match(tok):
                continue
            if not any(ch.isalpha() for ch in tok) or low in STOPWORDS or low in seen:
                continue
            seen.add(low)
            out.append(tok)
            if len(out) == limit:
                return out
        return out
    return []


def query_values(site: str, listing_dom: str, n: int = N_VALUES) -> List[str]:
    """The seeded draw: ``n`` of the first ``POOL`` qualifying tokens, order =
    roles (q1 compile = the agent's task value, q2 selection, q3 + q4
    validation, q5 the fresh-process MCP call). Fewer than ``n`` -> ``[]``-like
    short list (the ``query_rule`` bucket)."""
    toks = qualifying_tokens(listing_dom)
    if len(toks) < n:
        return toks
    rng = random.Random(f"{SEED}:values:{site}")
    return rng.sample(toks, n)


# ---------------------------------------------------------------------------
# freshness (run by ``plan`` before the first request)
# ---------------------------------------------------------------------------

_SUFFIX2 = {"co.uk", "org.uk", "ac.uk", "gov.uk", "edu.au", "com.au", "org.au", "gov.au",
            "net.au", "co.nz", "go.jp", "ac.jp", "co.jp", "gv.at", "ac.at",
            "readthedocs.io", "github.io", "europa.eu"}


def registrable(host: str) -> str:
    """eTLD+1 with a small built-in suffix list (enough for this corpus)."""
    p = host.lower().split(".")
    for n in (3, 2):
        if len(p) > n and ".".join(p[-n:]) in _SUFFIX2 | {"jst.go.jp"}:
            return ".".join(p[-(n + 1):])
    return ".".join(p[-2:])


def check_fresh(prior_hosts_path: str = "") -> List[str]:
    """Assert the corpus is well-formed and fresh; return any clash (empty = ok)."""
    here = os.path.dirname(os.path.abspath(__file__))
    never = set()
    with open(os.path.join(here, "NEVER_CONTACT.txt")) as fh:
        never = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    sites = [e["site"] for rows in STRATA.values() for e in rows]
    assert len(sites) == len(set(sites)), "a host appears twice"
    for st, n in PER_COHORT.items():
        assert len(main_pool(st)) == n * len(COHORTS), st
        assert n % 2 == 0, "strata are split into pairs"
    for e in BY_SITE.values():
        for key in ("start_url", "listing_url"):
            assert e[key].startswith(f"https://{e['site']}/"), (e["site"], key)
    regs = [registrable(s) for s in sites]
    assert len(regs) == len(set(regs)), "two corpus hosts share a registrable domain"
    prior = set()
    if prior_hosts_path:
        with open(prior_hosts_path) as fh:
            prior = {ln.strip() for ln in fh if ln.strip()}
    used = {registrable(h) for h in prior | never}
    return sorted(s for s in sites if registrable(s) in used)


def snapshot_digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
