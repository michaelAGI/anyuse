"""Sample 5 (attach product path) — the corpus, fixed BEFORE any of it was fetched.

The rules (selection, the query rule, the agent, the split, the tiers, the
denominator, the replacement buckets, the traffic limits) are in
the design notes, committed in the same commit as this file.
Nothing here may change after the first request; a replacement is taken from
``RESERVE`` in listed order and every swap is reported with its bucket.

Shape of an entry::

    {"id", "site", "category",
     "start_url",     # the page whose visible search input the agent types into
     "listing_url"}   # the site's own listing the query rule reads (same host)

No query value is written here. The four values per site come from the query
rule (``query_values``) applied to ``listing_url`` at run time, so none is
invented. Every host was chosen from memory, before any request, for variety;
every host is absent from the prior-host snapshot (see the specification)
and from the measurement harness (``check_fresh``, run by ``plan``).
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import List

MAIN = [
    {"id": "s5_hex", "site": "hex.pm", "category": "package_registry",
     "start_url": "https://hex.pm/packages", "listing_url": "https://hex.pm/packages"},
    {"id": "s5_ocaml", "site": "ocaml.org", "category": "package_registry",
     "start_url": "https://ocaml.org/packages", "listing_url": "https://ocaml.org/packages"},
    {"id": "s5_shardbox", "site": "shardbox.org", "category": "package_registry",
     "start_url": "https://shardbox.org/", "listing_url": "https://shardbox.org/"},
    {"id": "s5_psgallery", "site": "www.powershellgallery.com", "category": "package_registry",
     "start_url": "https://www.powershellgallery.com/packages",
     "listing_url": "https://www.powershellgallery.com/packages"},
    {"id": "s5_rubydoc", "site": "www.rubydoc.info", "category": "api_docs",
     "start_url": "https://www.rubydoc.info/gems", "listing_url": "https://www.rubydoc.info/gems"},
    {"id": "s5_wpplugins", "site": "wordpress.org", "category": "plugin_directory",
     "start_url": "https://wordpress.org/plugins/", "listing_url": "https://wordpress.org/plugins/"},
    {"id": "s5_sqlite", "site": "www.sqlite.org", "category": "docs_search",
     "start_url": "https://www.sqlite.org/search", "listing_url": "https://www.sqlite.org/docs.html"},
    {"id": "s5_postgres", "site": "www.postgresql.org", "category": "docs_search",
     "start_url": "https://www.postgresql.org/search/",
     "listing_url": "https://www.postgresql.org/docs/current/sql-commands.html"},
    {"id": "s5_freesound", "site": "freesound.org", "category": "media_library",
     "start_url": "https://freesound.org/", "listing_url": "https://freesound.org/browse/tags/"},
    {"id": "s5_xenocanto", "site": "xeno-canto.org", "category": "nature_archive",
     "start_url": "https://xeno-canto.org/", "listing_url": "https://xeno-canto.org/"},
    {"id": "s5_oapen", "site": "library.oapen.org", "category": "open_books",
     "start_url": "https://library.oapen.org/",
     "listing_url": "https://library.oapen.org/browse?type=subject"},
    {"id": "s5_numdam", "site": "www.numdam.org", "category": "math_archive",
     "start_url": "https://www.numdam.org/", "listing_url": "https://www.numdam.org/journals/"},
    {"id": "s5_hal", "site": "hal.science", "category": "open_archive",
     "start_url": "https://hal.science/", "listing_url": "https://hal.science/browse/domain"},
    {"id": "s5_escholarship", "site": "escholarship.org", "category": "open_archive",
     "start_url": "https://escholarship.org/", "listing_url": "https://escholarship.org/"},
    {"id": "s5_ons", "site": "www.ons.gov.uk", "category": "official_statistics",
     "start_url": "https://www.ons.gov.uk/", "listing_url": "https://www.ons.gov.uk/economy"},
    {"id": "s5_fedreg", "site": "www.federalregister.gov", "category": "government_documents",
     "start_url": "https://www.federalregister.gov/",
     "listing_url": "https://www.federalregister.gov/agencies"},
    {"id": "s5_openfoodfacts", "site": "world.openfoodfacts.org", "category": "product_database",
     "start_url": "https://world.openfoodfacts.org/",
     "listing_url": "https://world.openfoodfacts.org/categories"},
    {"id": "s5_bmo", "site": "bugzilla.mozilla.org", "category": "bug_tracker",
     "start_url": "https://bugzilla.mozilla.org/",
     "listing_url": "https://bugzilla.mozilla.org/describecomponents.cgi"},
    {"id": "s5_glottolog", "site": "glottolog.org", "category": "linguistics_database",
     "start_url": "https://glottolog.org/glottolog",
     "listing_url": "https://glottolog.org/glottolog/family"},
    {"id": "s5_twfy", "site": "www.theyworkforyou.com", "category": "parliament_records",
     "start_url": "https://www.theyworkforyou.com/",
     "listing_url": "https://www.theyworkforyou.com/mps/"},
]

RESERVE = [
    {"id": "s5r_doab", "site": "directory.doabooks.org", "category": "open_books",
     "start_url": "https://directory.doabooks.org/",
     "listing_url": "https://directory.doabooks.org/browse?type=classification_text"},
    {"id": "s5r_ddb", "site": "www.deutsche-digitale-bibliothek.de", "category": "cultural_heritage",
     "start_url": "https://www.deutsche-digitale-bibliothek.de/",
     "listing_url": "https://www.deutsche-digitale-bibliothek.de/"},
    {"id": "s5r_tna", "site": "discovery.nationalarchives.gov.uk", "category": "archives",
     "start_url": "https://discovery.nationalarchives.gov.uk/",
     "listing_url": "https://discovery.nationalarchives.gov.uk/"},
    {"id": "s5r_scholar", "site": "scholar.archive.org", "category": "open_archive",
     "start_url": "https://scholar.archive.org/", "listing_url": "https://scholar.archive.org/"},
    {"id": "s5r_rhea", "site": "www.rhea-db.org", "category": "life_science_database",
     "start_url": "https://www.rhea-db.org/", "listing_url": "https://www.rhea-db.org/"},
    {"id": "s5r_reactome", "site": "reactome.org", "category": "life_science_database",
     "start_url": "https://reactome.org/", "listing_url": "https://reactome.org/content/toc"},
    {"id": "s5r_ntrs", "site": "ntrs.nasa.gov", "category": "technical_reports",
     "start_url": "https://ntrs.nasa.gov/", "listing_url": "https://ntrs.nasa.gov/"},
    {"id": "s5r_openverse", "site": "openverse.org", "category": "media_library",
     "start_url": "https://openverse.org/", "listing_url": "https://openverse.org/"},
    {"id": "s5r_mdz", "site": "www.digitale-sammlungen.de", "category": "cultural_heritage",
     "start_url": "https://www.digitale-sammlungen.de/",
     "listing_url": "https://www.digitale-sammlungen.de/"},
    {"id": "s5r_certkb", "site": "www.kb.cert.org", "category": "security_advisories",
     "start_url": "https://www.kb.cert.org/vuls/", "listing_url": "https://www.kb.cert.org/vuls/"},
    {"id": "s5r_eol", "site": "eol.org", "category": "nature_archive",
     "start_url": "https://eol.org/", "listing_url": "https://eol.org/"},
    {"id": "s5r_wals", "site": "wals.info", "category": "linguistics_database",
     "start_url": "https://wals.info/", "listing_url": "https://wals.info/feature"},
    {"id": "s5r_ssoar", "site": "www.ssoar.info", "category": "open_archive",
     "start_url": "https://www.ssoar.info/ssoar/", "listing_url": "https://www.ssoar.info/ssoar/"},
    {"id": "s5r_cppref", "site": "en.cppreference.com", "category": "api_docs",
     "start_url": "https://en.cppreference.com/w/",
     "listing_url": "https://en.cppreference.com/w/cpp/algorithm"},
    {"id": "s5r_eom", "site": "encyclopediaofmath.org", "category": "reference_wiki",
     "start_url": "https://encyclopediaofmath.org/wiki/Main_Page",
     "listing_url": "https://encyclopediaofmath.org/wiki/Main_Page"},
]

TARGET = 20
N_QUERIES = 4

BY_ID = {w["id"]: w for w in MAIN + RESERVE}

# ---------------------------------------------------------------------------
# the query rule — pure, offline-testable
# ---------------------------------------------------------------------------

#: interface words that are never a query, whatever the listing says
STOPWORDS = frozenset("""
about all back browse cancel close contact cookies deutsch download english
español first français help home last login log logout menu more next page
prev previous privacy register search show sign skip terms top view
""".split())
MIN_ROWS = 5
_EDGE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_OK = re.compile(r"^[^\W_][\w.+\-]*$", re.UNICODE)


def token_of(link_text: str) -> str:
    """The first whitespace token of a link text, edge punctuation stripped."""
    parts = (link_text or "").split()
    if not parts:
        return ""
    return _EDGE.sub("", unicodedata.normalize("NFC", parts[0]))


def query_values(listing_dom: str, n: int = N_QUERIES) -> List[str]:
    """The first ``n`` distinct qualifying tokens of the listing's main link list.

    1. parse the rendered listing page; take its repeated sibling groups with
       >= ``MIN_ROWS`` members (``html_extract.repeated_groups``, ranked by text
       mass), skip groups inside a ``nav``, ``header`` or ``footer`` element,
       and keep the first group in which >= 80 % of the rows contain a link;
    2. per row, in document order, the text of the row's first link, reduced
       to its first token (``token_of``);
    3. keep a token iff it is 3-30 characters, starts with a letter or digit,
       contains a letter, has only letters, digits and ``. _ + -``, is not in
       ``STOPWORDS`` and was not kept before (case-insensitive).
    """
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
            if len(out) == n:
                return out
        return out
    return []


def check_fresh() -> None:
    """Run by ``plan`` before the first request (a host this run itself adds to
    NEVER_CONTACT.txt later must not break the report)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "NEVER_CONTACT.txt")
    never = set()
    if os.path.exists(path):
        with open(path) as fh:
            never = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    sites = [w["site"] for w in MAIN + RESERVE]
    assert len(sites) == len(set(sites)), "a host appears twice"
    assert len(MAIN) == TARGET
    clash = sorted(set(sites) & never)
    assert not clash, f"never-contact hosts in the corpus: {clash}"
    for w in MAIN + RESERVE:
        for key in ("start_url", "listing_url"):
            assert w[key].startswith(f"https://{w['site']}/"), (w["id"], key)

