"""A blind benchmark corpus, fixed BEFORE any of it was fetched.

Selection rule, denominators, the dry-look admission test and the proof
standard: the measurement harness, committed in the same commit as this
file. Nothing here may change after the first fetch; a replacement is taken
from ``RESERVE`` in listed order and every swap is reported.

Shape of an entry::

    {"id", "site", "category", "cls",           # "list" | "detail"
     "url_tmpl",                                 # {v} is the input value
     "param",                                    # the parameter's name
     "recorded",                                 # the value compiled from
     "fresh": [v1, v2],                          # values the proof uses
     "path_style": bool,                         # the id lives in the PATH
     "task"}                                     # what `anyuse learn` is told

What it adds, beyond being untouched
------------------------------------
* **A quota of path-style detail workflows.** At least 8 entries whose
  identifier is carried by the URL path rather than a query value, because
  those are the case under test and ``corpus_sample2`` had only three.
  Several span more than one segment on purpose.
* **Every entry carries a `task`**, phrased the way a person would type it, so
  the same corpus can drive the learn loop from ZERO recordings
  and the ladder from recordings without two corpora that might
  differ.
* **No host from the earlier corpora** (``corpus_sample2`` and the one before it), asserted at import
  against the full 73-host list those two used.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# class A — search -> a visible result list
# ---------------------------------------------------------------------------

LIST_WORKFLOWS = [
    # -- package registries --------------------------------------------------
    {"id": "librs_search", "site": "lib.rs", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://lib.rs/search?q={v}",
     "recorded": "serde", "fresh": ["regex", "clap"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "ctan_search", "site": "ctan.org", "category": "package_registry",
     "cls": "list", "param": "phrase",
     "url_tmpl": "https://ctan.org/search?phrase={v}",
     "recorded": "tikz", "fresh": ["biblatex", "beamer"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "archlinux_search", "site": "archlinux.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://archlinux.org/packages/?q={v}",
     "recorded": "firefox", "fresh": ["vim", "curl"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "aur_search", "site": "aur.archlinux.org", "category": "package_registry",
     "cls": "list", "param": "K", "url_tmpl": "https://aur.archlinux.org/packages?K={v}",
     "recorded": "yay", "fresh": ["paru", "spotify"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "macports_search", "site": "ports.macports.org", "category": "package_registry",
     "cls": "list", "param": "q",
     "url_tmpl": "https://ports.macports.org/search/?q={v}&name=on",
     "recorded": "wget", "fresh": ["tmux", "jq"], "path_style": False,
     "task": "search this site for the port {q} and stop when the result list is visible"},
    {"id": "fedora_search", "site": "packages.fedoraproject.org",
     "category": "package_registry", "cls": "list", "param": "query",
     "url_tmpl": "https://packages.fedoraproject.org/search?query={v}",
     "recorded": "htop", "fresh": ["ripgrep", "fish"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "clojars_search", "site": "clojars.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://clojars.org/search?q={v}",
     "recorded": "ring", "fresh": ["cheshire", "hiccup"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "opam_search", "site": "opam.ocaml.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://opam.ocaml.org/packages/?q={v}",
     "recorded": "lwt", "fresh": ["dune", "yojson"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "snapcraft_search", "site": "snapcraft.io", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://snapcraft.io/search?q={v}",
     "recorded": "inkscape", "fresh": ["gimp", "krita"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},

    # -- documentation / manual pages ----------------------------------------
    {"id": "manarch_search", "site": "man.archlinux.org",
     "category": "documentation_search", "cls": "list", "param": "q",
     "url_tmpl": "https://man.archlinux.org/search?q={v}",
     "recorded": "ssh", "fresh": ["rsync", "tar"], "path_style": False,
     "task": "search this site for the manual page {q} and stop when the result list is visible"},
    {"id": "manopenbsd_search", "site": "man.openbsd.org",
     "category": "documentation_search", "cls": "list", "param": "query",
     "url_tmpl": "https://man.openbsd.org/?query={v}&apropos=1&sec=0&arch=default&manpath=OpenBSD-current",
     "recorded": "pledge", "fresh": ["unveil", "kqueue"], "path_style": False,
     "task": "search this site for the manual page {q} and stop when the result list is visible"},
    {"id": "ietf_search", "site": "datatracker.ietf.org",
     "category": "documentation_search", "cls": "list", "param": "name",
     "url_tmpl": "https://datatracker.ietf.org/doc/search?name={v}&sort=&rfcs=on",
     "recorded": "quic", "fresh": ["oauth", "dnssec"], "path_style": False,
     "task": "search this site for documents about {q} and stop when the result list is visible"},

    # -- open data / government ----------------------------------------------
    {"id": "datagvat_search", "site": "www.data.gv.at", "category": "open_data",
     "cls": "list", "param": "search",
     "url_tmpl": "https://www.data.gv.at/suche/?search={v}",
     "recorded": "verkehr", "fresh": ["umwelt", "energie"], "path_style": False,
     "task": "search this site for datasets about {q} and stop when the result list is visible"},
    {"id": "overheidnl_search", "site": "data.overheid.nl", "category": "open_data",
     "cls": "list", "param": "zoekterm",
     "url_tmpl": "https://data.overheid.nl/datasets?zoekterm={v}",
     "recorded": "verkeer", "fresh": ["milieu", "onderwijs"], "path_style": False,
     "task": "search this site for datasets about {q} and stop when the result list is visible"},
    {"id": "datagouvfr_search", "site": "www.data.gouv.fr", "category": "open_data",
     "cls": "list", "param": "q",
     "url_tmpl": "https://www.data.gouv.fr/datasets/?q={v}",
     "recorded": "transport", "fresh": ["energie", "logement"], "path_style": False,
     "task": "search this site for datasets about {q} and stop when the result list is visible"},

    # -- library catalogues / reference --------------------------------------
    {"id": "hathitrust_search", "site": "catalog.hathitrust.org",
     "category": "library_catalogue", "cls": "list", "param": "lookfor",
     "url_tmpl": "https://catalog.hathitrust.org/Search/Home?lookfor={v}&type=all",
     "recorded": "dickens", "fresh": ["austen", "melville"], "path_style": False,
     "task": "search this catalogue for {q} and stop when the result list is visible"},
    {"id": "oeis_search", "site": "oeis.org", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://oeis.org/search?q={v}",
     "recorded": "fibonacci", "fresh": ["catalan", "bernoulli"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "musicbrainz_search", "site": "musicbrainz.org", "category": "reference",
     "cls": "list", "param": "query",
     "url_tmpl": "https://musicbrainz.org/search?query={v}&type=artist&method=indexed",
     "recorded": "portishead", "fresh": ["bjork", "radiohead"], "path_style": False,
     "task": "search this site for the artist {q} and stop when the result list is visible"},

    # -- code hosting ---------------------------------------------------------
    {"id": "salsa_search", "site": "salsa.debian.org", "category": "code_hosting",
     "cls": "list", "param": "search",
     "url_tmpl": "https://salsa.debian.org/explore/projects?search={v}",
     "recorded": "apt", "fresh": ["dpkg", "systemd"], "path_style": False,
     "task": "search this site for projects called {q} and stop when the result list is visible"},

    # -- weather / transport with a UI ---------------------------------------
    {"id": "tidesandcurrents_search", "site": "tidesandcurrents.noaa.gov",
     "category": "weather_transport", "cls": "list", "param": "id",
     "url_tmpl": "https://tidesandcurrents.noaa.gov/stationhome.html?id={v}",
     "recorded": "8443970", "fresh": ["9414290", "8518750"], "path_style": False,
     "task": "open the page for station {q} and stop when its details are visible"},
]

# ---------------------------------------------------------------------------
# class B — open ONE item, read its named fields.
# The fixed quota: at least 8 of these carry the identifier in the PATH.
# ---------------------------------------------------------------------------

DETAIL_WORKFLOWS = [
    # -- multi-segment path identifiers (the path-style case) -----------------
    {"id": "archlinux_detail", "site": "archlinux.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://archlinux.org/packages/{v}/",
     "recorded": "core/x86_64/bash", "fresh": ["core/x86_64/coreutils",
                                               "extra/x86_64/vim"],
     "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "fedora_detail", "site": "packages.fedoraproject.org",
     "category": "package_registry", "cls": "detail", "param": "pkg",
     "url_tmpl": "https://packages.fedoraproject.org/pkgs/{v}/",
     "recorded": "htop/htop", "fresh": ["fish/fish", "ripgrep/ripgrep"],
     "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "clojars_detail", "site": "clojars.org", "category": "package_registry",
     "cls": "detail", "param": "artifact",
     "url_tmpl": "https://clojars.org/{v}",
     "recorded": "ring/ring-core", "fresh": ["cheshire/cheshire",
                                             "hiccup/hiccup"],
     "path_style": True,
     "task": "open the page for the library {q} and stop when its named fields are visible"},
    {"id": "salsa_detail", "site": "salsa.debian.org", "category": "code_hosting",
     "cls": "detail", "param": "repo", "url_tmpl": "https://salsa.debian.org/{v}",
     "recorded": "apt-team/apt", "fresh": ["debian/devscripts", "dpkg-team/dpkg"],
     "path_style": True,
     "task": "open the repository page for {q} and stop when its details are visible"},
    {"id": "timeanddate_detail", "site": "www.timeanddate.com",
     "category": "weather_transport", "cls": "detail", "param": "place",
     "url_tmpl": "https://www.timeanddate.com/weather/{v}",
     "recorded": "norway/oslo", "fresh": ["germany/berlin", "portugal/lisbon"],
     "path_style": True,
     "task": "open the weather page for {q} and stop when its named fields are visible"},

    # -- single-segment path identifiers -------------------------------------
    {"id": "librs_detail", "site": "lib.rs", "category": "package_registry",
     "cls": "detail", "param": "crate", "url_tmpl": "https://lib.rs/crates/{v}",
     "recorded": "serde", "fresh": ["regex", "clap"], "path_style": True,
     "task": "open the page for the crate {q} and stop when its named fields are visible"},
    {"id": "ctan_detail", "site": "ctan.org", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://ctan.org/pkg/{v}",
     "recorded": "tikz", "fresh": ["biblatex", "beamer"], "path_style": True,
     "task": "open the page for the package {q} and stop when its named fields are visible"},
    {"id": "macports_detail", "site": "ports.macports.org",
     "category": "package_registry", "cls": "detail", "param": "port",
     "url_tmpl": "https://ports.macports.org/port/{v}/",
     "recorded": "wget", "fresh": ["tmux", "jq"], "path_style": True,
     "task": "open the page for the port {q} and stop when its named fields are visible"},
    {"id": "snapcraft_detail", "site": "snapcraft.io", "category": "package_registry",
     "cls": "detail", "param": "snap", "url_tmpl": "https://snapcraft.io/{v}",
     "recorded": "inkscape", "fresh": ["gimp", "krita"], "path_style": True,
     "task": "open the page for {q} and stop when its named fields are visible"},
    {"id": "oeis_detail", "site": "oeis.org", "category": "reference",
     "cls": "detail", "param": "seq", "url_tmpl": "https://oeis.org/{v}",
     "recorded": "A000045", "fresh": ["A000108", "A000110"], "path_style": True,
     "task": "open the page for sequence {q} and stop when its named fields are visible"},
    {"id": "aur_detail", "site": "aur.archlinux.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://aur.archlinux.org/packages/{v}",
     "recorded": "yay", "fresh": ["paru", "downgrade"], "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},

    # -- a delimited fragment inside ONE segment (the path_part case) ---------
    {"id": "manarch_detail", "site": "man.archlinux.org",
     "category": "documentation_search", "cls": "detail", "param": "page",
     "url_tmpl": "https://man.archlinux.org/man/{v}.1.en",
     "recorded": "rsync", "fresh": ["tar", "ssh"], "path_style": True,
     "task": "open the manual page for {q} and stop when its named sections are visible"},
]

# ---------------------------------------------------------------------------
# reserve — used IN THIS ORDER to replace a robots-disallowed entry, a blocked
# host, or one the dry look rejects
# ---------------------------------------------------------------------------

RESERVE = [
    {"id": "endoflife_detail", "site": "endoflife.date", "category": "reference",
     "cls": "detail", "param": "product", "url_tmpl": "https://endoflife.date/{v}",
     "recorded": "python", "fresh": ["nodejs", "postgresql"], "path_style": True,
     "task": "open the page for {q} and stop when its table of versions is visible"},
    {"id": "iana_detail", "site": "www.iana.org", "category": "reference",
     "cls": "detail", "param": "registry",
     "url_tmpl": "https://www.iana.org/assignments/{v}",
     "recorded": "media-types/media-types.xhtml",
     "fresh": ["http-status-codes/http-status-codes.xhtml",
               "uri-schemes/uri-schemes.xhtml"],
     "path_style": True,
     "task": "open the registry page for {q} and stop when its table is visible"},
    {"id": "pkgsorg_search", "site": "pkgs.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://pkgs.org/search/?q={v}",
     "recorded": "nginx", "fresh": ["redis", "postgresql"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "zbmath_search", "site": "zbmath.org", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://zbmath.org/?q={v}",
     "recorded": "banach", "fresh": ["hilbert", "galois"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "distrowatch_detail", "site": "distrowatch.com", "category": "reference",
     "cls": "detail", "param": "distribution",
     "url_tmpl": "https://distrowatch.com/table.php?distribution={v}",
     "recorded": "debian", "fresh": ["fedora", "alpine"], "path_style": False,
     "task": "open the page for the distribution {q} and stop when its table is visible"},
    {"id": "libraries_search", "site": "libraries.io", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://libraries.io/search?q={v}",
     "recorded": "express", "fresh": ["lodash", "axios"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "opam_detail", "site": "opam.ocaml.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://opam.ocaml.org/packages/{v}/",
     "recorded": "lwt", "fresh": ["dune", "yojson"], "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "manopenbsd_detail", "site": "man.openbsd.org",
     "category": "documentation_search", "cls": "detail", "param": "page",
     "url_tmpl": "https://man.openbsd.org/{v}",
     "recorded": "pledge.2", "fresh": ["unveil.2", "kqueue.2"], "path_style": True,
     "task": "open the manual page for {q} and stop when its named sections are visible"},
    {"id": "hathitrust_detail", "site": "catalog.hathitrust.org",
     "category": "library_catalogue", "cls": "detail", "param": "rec",
     "url_tmpl": "https://catalog.hathitrust.org/Record/{v}",
     "recorded": "000578090", "fresh": ["001718526", "008649431"],
     "path_style": True,
     "task": "open the catalogue record {q} and stop when its named fields are visible"},
    {"id": "musicbrainz_detail", "site": "musicbrainz.org", "category": "reference",
     "cls": "detail", "param": "artist",
     "url_tmpl": "https://musicbrainz.org/artist/{v}",
     "recorded": "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11",
     "fresh": ["a74b1b7f-71a5-4011-9441-d0b5e4122711",
               "87c5dedd-371d-4a53-9f7f-80522fb7f3cb"],
     "path_style": True,
     "task": "open the artist page {q} and stop when its named fields are visible"},
    {"id": "ietf_detail", "site": "datatracker.ietf.org",
     "category": "documentation_search", "cls": "detail", "param": "doc",
     "url_tmpl": "https://datatracker.ietf.org/doc/{v}/",
     "recorded": "rfc9000", "fresh": ["rfc8446", "rfc7540"], "path_style": True,
     "task": "open the document page for {q} and stop when its named fields are visible"},
    {"id": "gutenbergau_search", "site": "gutenberg.net.au",
     "category": "library_catalogue", "cls": "list", "param": "q",
     "url_tmpl": "https://gutenberg.net.au/plusfifty-a-m.html#{v}",
     "recorded": "conrad", "fresh": ["chesterton", "buchan"], "path_style": False,
     "task": "find the author {q} on this page and stop when the list is visible"},
    {"id": "sourcehutpages_search", "site": "man.sr.ht",
     "category": "documentation_search", "cls": "list", "param": "q",
     "url_tmpl": "https://man.sr.ht/?search={v}",
     "recorded": "git", "fresh": ["builds", "lists"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "wikiless_search", "site": "www.lwn.net", "category": "reference",
     "cls": "list", "param": "words",
     "url_tmpl": "https://lwn.net/Search/DoSearch?words={v}",
     "recorded": "btrfs", "fresh": ["io_uring", "ebpf"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
]

SAMPLE3 = LIST_WORKFLOWS + DETAIL_WORKFLOWS
BY_ID = {w["id"]: w for w in SAMPLE3 + RESERVE}

#: Every host the earlier corpora drew on. No entry here may name one.
USED_HOSTS = {
    "anaconda.org", "archive.org", "arxiv.org", "babel.hathitrust.org",
    "catalog.data.gov", "codeberg.org", "cran.r-project.org", "crates.io",
    "data.europa.eu", "data.gov.au", "data.gov.ie", "data.norge.no",
    "datos.gob.es", "dblp.org", "developer.mozilla.org", "doaj.org", "docs.rs",
    "en.wikipedia.org", "en.wikisource.org", "en.wiktionary.org",
    "geocoding-api.open-meteo.com", "gitea.com", "gitlab.com",
    "hackage.haskell.org", "hex.pm", "hexdocs.pm", "launchpad.net",
    "libris.kb.se", "luarocks.org", "man7.org", "metacpan.org",
    "nominatim.openstreetmap.org", "opendata.paris.fr", "opendata.swiss",
    "opendata.vancouver.ca", "openlibrary.org", "ourworldindata.org",
    "packages.debian.org", "packagist.org", "pkg.go.dev",
    "pkgs.alpinelinux.org", "portal.dnb.de", "pub.dev", "pypi.org",
    "readthedocs.org", "repology.org", "rubygems.org", "search.f-droid.org",
    "search.maven.org", "software.opensuse.org", "sourceforge.net", "sr.ht",
    "standardebooks.org", "transport.opendata.ch", "wiki.archlinux.org",
    "wiki.debian.org", "wiki.gentoo.org", "wttr.in", "www.data.gov.uk",
    "www.data.qld.gov.au", "www.dataportal.se", "www.dati.gov.it",
    "www.dwds.de", "www.fadedpage.com", "www.govdata.de", "www.gutenberg.org",
    "www.loc.gov", "www.ndbc.noaa.gov", "www.nuget.org", "www.rfc-editor.org",
    "www.wikidata.org", "www.yr.no", "zenodo.org",
}

#: Fixed minimums. The run fails loudly rather than quietly shrinking.
TARGET_ALLOWED = 24
TARGET_PATH_STYLE_DETAIL = 8


def _self_check():
    """Run at import: the selection rule must hold for the file as committed."""
    ids = [w["id"] for w in SAMPLE3 + RESERVE]
    assert len(ids) == len(set(ids)), "duplicate workflow id"
    overlap = {w["site"] for w in SAMPLE3 + RESERVE} & USED_HOSTS
    assert not overlap, f"sample-3 entry reuses a host from sample 1 or 2: {overlap}"
    path_detail = [w for w in DETAIL_WORKFLOWS if w["path_style"]]
    assert len(path_detail) >= TARGET_PATH_STYLE_DETAIL, len(path_detail)
    assert len(SAMPLE3) >= TARGET_ALLOWED, len(SAMPLE3)
    for w in SAMPLE3 + RESERVE:
        assert "{v}" in w["url_tmpl"], w["id"]
        assert len(w["fresh"]) >= 2, w["id"]
        assert w["recorded"] not in w["fresh"], w["id"]
        assert w["url_tmpl"].startswith("https://"), w["id"]
        assert w["task"] and "{q}" in w["task"], w["id"]
        assert w["cls"] in ("list", "detail"), w["id"]
        if w["path_style"]:
            head = w["url_tmpl"].split("{v}")[0]
            assert "?" not in head, f"{w['id']} claims path_style but {{v}} is in the query"


_self_check()
