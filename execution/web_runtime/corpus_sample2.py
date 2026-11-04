"""A blind benchmark corpus, fixed BEFORE any of it was recorded.

Selection rule, denominators and proof standard: the measurement harness,
committed first. This file is the machine-readable half and nothing in it may
change after the first recording; a replacement is taken from ``RESERVE`` in
listed order and the swap is reported.

Shape of an entry::

    {"id", "site", "category", "cls",          # "list" | "detail"
     "url_tmpl",                                # {v} is the input value
     "param",                                   # the parameter's name
     "recorded",                                # the value compiled from
     "fresh": [v1, v2],                         # values the proof uses
     "note"}

No entry names a host of the earlier corpus (``SAMPLE1_HOSTS``). No entry is a raw JSON
endpoint: every one is a page a person could look at. No entry needs a login.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# class A — search -> visible result list  (target: >= 20 robots-allowed)
# ---------------------------------------------------------------------------

LIST_WORKFLOWS = [
    # -- package registries --------------------------------------------------
    {"id": "packagist_search", "site": "packagist.org", "category": "package_registry",
     "cls": "list", "param": "query", "url_tmpl": "https://packagist.org/?query={v}",
     "recorded": "guzzle", "fresh": ["monolog", "twig"]},
    {"id": "rubygems_search", "site": "rubygems.org", "category": "package_registry",
     "cls": "list", "param": "query", "url_tmpl": "https://rubygems.org/search?query={v}",
     "recorded": "rails", "fresh": ["sinatra", "nokogiri"]},
    {"id": "hackage_search", "site": "hackage.haskell.org", "category": "package_registry",
     "cls": "list", "param": "terms", "url_tmpl": "https://hackage.haskell.org/packages/search?terms={v}",
     "recorded": "aeson", "fresh": ["lens", "conduit"]},
    {"id": "metacpan_search", "site": "metacpan.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://metacpan.org/search?q={v}",
     "recorded": "Moose", "fresh": ["DBI", "Plack"]},
    {"id": "luarocks_search", "site": "luarocks.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://luarocks.org/search?q={v}",
     "recorded": "luasocket", "fresh": ["penlight", "busted"]},
    {"id": "alpine_search", "site": "pkgs.alpinelinux.org", "category": "package_registry",
     "cls": "list", "param": "name", "url_tmpl": "https://pkgs.alpinelinux.org/packages?name={v}",
     "recorded": "curl", "fresh": ["nginx", "python3"]},
    {"id": "debian_search", "site": "packages.debian.org", "category": "package_registry",
     "cls": "list", "param": "keywords",
     "url_tmpl": "https://packages.debian.org/search?keywords={v}",
     "recorded": "rsync", "fresh": ["tmux", "htop"]},
    {"id": "repology_search", "site": "repology.org", "category": "package_registry",
     "cls": "list", "param": "search",
     "url_tmpl": "https://repology.org/projects/?search={v}",
     "recorded": "ffmpeg", "fresh": ["sqlite", "imagemagick"]},

    # -- documentation search -------------------------------------------------
    {"id": "hexdocs_search", "site": "hexdocs.pm", "category": "documentation_search",
     "cls": "list", "param": "q", "url_tmpl": "https://hexdocs.pm/elixir/search.html?q={v}",
     "recorded": "Enum", "fresh": ["Stream", "Task"]},
    {"id": "gentoowiki_search", "site": "wiki.gentoo.org", "category": "documentation_search",
     "cls": "list", "param": "search",
     "url_tmpl": "https://wiki.gentoo.org/index.php?search={v}&title=Special%3ASearch&fulltext=1",
     "recorded": "portage", "fresh": ["systemd", "kernel"]},
    {"id": "debianwiki_search", "site": "wiki.debian.org", "category": "documentation_search",
     "cls": "list", "param": "value",
     "url_tmpl": "https://wiki.debian.org/FindPage?action=fullsearch&value={v}",
     "recorded": "apt", "fresh": ["network", "backports"]},
    {"id": "man7_search", "site": "man7.org", "category": "documentation_search",
     "cls": "list", "param": "q",
     "url_tmpl": "https://man7.org/linux/man-pages/dir_section_2.html#{v}",
     "recorded": "open", "fresh": ["read", "write"],
     "note": "an index page, not a query — kept as a hard case on purpose"},

    # -- open data / government / statistics ----------------------------------
    {"id": "datagovau_search", "site": "data.gov.au", "category": "open_data_portal",
     "cls": "list", "param": "q", "url_tmpl": "https://data.gov.au/data/dataset?q={v}",
     "recorded": "rainfall", "fresh": ["population", "roads"]},
    {"id": "datagovie_search", "site": "data.gov.ie", "category": "open_data_portal",
     "cls": "list", "param": "q", "url_tmpl": "https://data.gov.ie/dataset?q={v}",
     "recorded": "housing", "fresh": ["transport", "health"]},
    {"id": "datosgob_search", "site": "datos.gob.es", "category": "open_data_portal",
     "cls": "list", "param": "q",
     "url_tmpl": "https://datos.gob.es/es/catalogo?q={v}",
     "recorded": "turismo", "fresh": ["educacion", "empleo"]},
    {"id": "dataportal_se_search", "site": "www.dataportal.se", "category": "open_data_portal",
     "cls": "list", "param": "q",
     "url_tmpl": "https://www.dataportal.se/datasets?q={v}",
     "recorded": "trafik", "fresh": ["miljo", "skola"]},
    {"id": "datanorge_search", "site": "data.norge.no", "category": "open_data_portal",
     "cls": "list", "param": "q",
     "url_tmpl": "https://data.norge.no/datasets?q={v}",
     "recorded": "vann", "fresh": ["skole", "vei"]},
    {"id": "vancouver_search", "site": "opendata.vancouver.ca", "category": "open_data_portal",
     "cls": "list", "param": "q",
     "url_tmpl": "https://opendata.vancouver.ca/explore/?q={v}",
     "recorded": "parks", "fresh": ["trees", "bikes"]},

    # -- library catalogues / archives ----------------------------------------
    {"id": "standardebooks_search", "site": "standardebooks.org", "category": "library_catalogue",
     "cls": "list", "param": "query",
     "url_tmpl": "https://standardebooks.org/ebooks?query={v}",
     "recorded": "dickens", "fresh": ["austen", "verne"]},
    {"id": "loc_search", "site": "www.loc.gov", "category": "library_catalogue",
     "cls": "list", "param": "q", "url_tmpl": "https://www.loc.gov/search/?q={v}",
     "recorded": "maps", "fresh": ["photographs", "newspapers"]},
    {"id": "hathitrust_search", "site": "babel.hathitrust.org", "category": "library_catalogue",
     "cls": "list", "param": "q1",
     "url_tmpl": "https://babel.hathitrust.org/cgi/ls?q1={v};a=srchls;anyall1=all",
     "recorded": "botany", "fresh": ["geology", "astronomy"]},
    {"id": "libris_search", "site": "libris.kb.se", "category": "library_catalogue",
     "cls": "list", "param": "q", "url_tmpl": "https://libris.kb.se/hitlist?q={v}",
     "recorded": "strindberg", "fresh": ["lagerlof", "bergman"]},

    # -- reference / bibliography ---------------------------------------------
    {"id": "dblp_search", "site": "dblp.org", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://dblp.org/search?q={v}",
     "recorded": "raft consensus", "fresh": ["paxos", "bloom filter"]},
    {"id": "doaj_search", "site": "doaj.org", "category": "reference",
     "cls": "list", "param": "ref",
     "url_tmpl": "https://doaj.org/search/articles?ref=homepage-box&source=%7B%22query%22%3A%7B%22query_string%22%3A%7B%22query%22%3A%22{v}%22%7D%7D%7D",
     "recorded": "photosynthesis", "fresh": ["glaciers", "vaccines"]},
    {"id": "zenodo_search", "site": "zenodo.org", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://zenodo.org/search?q={v}",
     "recorded": "climate", "fresh": ["genome", "seismic"]},
    {"id": "dwds_search", "site": "www.dwds.de", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://www.dwds.de/r?q={v}",
     "recorded": "Haus", "fresh": ["Baum", "Wasser"]},

    # -- weather / transport WITH a user interface -----------------------------
    {"id": "yr_search", "site": "www.yr.no", "category": "weather_transport",
     "cls": "list", "param": "q", "url_tmpl": "https://www.yr.no/en/search?q={v}",
     "recorded": "Bergen", "fresh": ["Tromso", "Stavanger"]},
    {"id": "wttr_city", "site": "wttr.in", "category": "weather_transport",
     "cls": "list", "param": "loc", "url_tmpl": "https://wttr.in/{v}",
     "recorded": "Leipzig", "fresh": ["Porto", "Tallinn"],
     "note": "a text UI in a browser — deliberately unusual markup"},
    {"id": "ndbc_station", "site": "www.ndbc.noaa.gov", "category": "weather_transport",
     "cls": "list", "param": "station",
     "url_tmpl": "https://www.ndbc.noaa.gov/station_page.php?station={v}",
     "recorded": "41001", "fresh": ["46042", "44013"]},

    # -- code hosting ----------------------------------------------------------
    {"id": "gitea_explore", "site": "gitea.com", "category": "code_hosting",
     "cls": "list", "param": "q", "url_tmpl": "https://gitea.com/explore/repos?q={v}",
     "recorded": "docker", "fresh": ["ansible", "terraform"]},
    {"id": "launchpad_search", "site": "launchpad.net", "category": "code_hosting",
     "cls": "list", "param": "text",
     "url_tmpl": "https://launchpad.net/projects/+index?text={v}",
     "recorded": "bzr", "fresh": ["cloud", "ubuntu"]},
]

# ---------------------------------------------------------------------------
# class B — open an item, read named fields  (target: >= 10 robots-allowed)
# ---------------------------------------------------------------------------

DETAIL_WORKFLOWS = [
    {"id": "packagist_detail", "site": "packagist.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://packagist.org/packages/{v}",
     "recorded": "guzzlehttp/guzzle", "fresh": ["monolog/monolog", "twig/twig"]},
    {"id": "rubygems_detail", "site": "rubygems.org", "category": "package_registry",
     "cls": "detail", "param": "gem", "url_tmpl": "https://rubygems.org/gems/{v}",
     "recorded": "rails", "fresh": ["sinatra", "nokogiri"]},
    {"id": "hackage_detail", "site": "hackage.haskell.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://hackage.haskell.org/package/{v}",
     "recorded": "aeson", "fresh": ["lens", "conduit"]},
    {"id": "alpine_detail", "site": "pkgs.alpinelinux.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://pkgs.alpinelinux.org/package/edge/main/x86_64/{v}",
     "recorded": "curl", "fresh": ["nginx", "git"]},
    {"id": "debian_detail", "site": "packages.debian.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://packages.debian.org/stable/{v}",
     "recorded": "rsync", "fresh": ["tmux", "htop"]},
    {"id": "repology_detail", "site": "repology.org", "category": "package_registry",
     "cls": "detail", "param": "project",
     "url_tmpl": "https://repology.org/project/{v}/versions",
     "recorded": "ffmpeg", "fresh": ["sqlite", "imagemagick"]},
    {"id": "luarocks_detail", "site": "luarocks.org", "category": "package_registry",
     "cls": "detail", "param": "mod",
     "url_tmpl": "https://luarocks.org/modules/{v}",
     "recorded": "luasocket/luasocket", "fresh": ["lunarmodules/busted",
                                                  "lunarmodules/penlight"]},
    {"id": "standardebooks_detail", "site": "standardebooks.org",
     "category": "library_catalogue", "cls": "detail", "param": "book",
     "url_tmpl": "https://standardebooks.org/ebooks/{v}",
     "recorded": "charles-dickens/a-christmas-carol",
     "fresh": ["jane-austen/pride-and-prejudice", "mary-shelley/frankenstein"]},
    {"id": "zenodo_detail", "site": "zenodo.org", "category": "reference",
     "cls": "detail", "param": "rec", "url_tmpl": "https://zenodo.org/records/{v}",
     "recorded": "3509134", "fresh": ["1205166", "1319838"]},
    {"id": "dblp_detail", "site": "dblp.org", "category": "reference",
     "cls": "detail", "param": "venue",
     "url_tmpl": "https://dblp.org/db/conf/{v}/index.html",
     "recorded": "sosp", "fresh": ["osdi", "nsdi"]},
    {"id": "ndbc_detail", "site": "www.ndbc.noaa.gov", "category": "weather_transport",
     "cls": "detail", "param": "station",
     "url_tmpl": "https://www.ndbc.noaa.gov/station_history.php?station={v}",
     "recorded": "41001", "fresh": ["46042", "44013"]},
    {"id": "gitea_detail", "site": "gitea.com", "category": "code_hosting",
     "cls": "detail", "param": "repo", "url_tmpl": "https://gitea.com/{v}",
     "recorded": "gitea/tea", "fresh": ["gitea/awesome-gitea", "gitea/act_runner"]},
]

# ---------------------------------------------------------------------------
# reserve — used IN THIS ORDER to replace a robots-disallowed or blocked entry
# ---------------------------------------------------------------------------

RESERVE = [
    {"id": "anaconda_search", "site": "anaconda.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://anaconda.org/search?q={v}",
     "recorded": "numpy", "fresh": ["scipy", "pandas"]},
    {"id": "cran_search", "site": "cran.r-project.org", "category": "package_registry",
     "cls": "list", "param": "pkg",
     "url_tmpl": "https://cran.r-project.org/web/packages/{v}/index.html",
     "recorded": "ggplot2", "fresh": ["dplyr", "data.table"]},
    {"id": "opensuse_search", "site": "software.opensuse.org", "category": "package_registry",
     "cls": "list", "param": "q",
     "url_tmpl": "https://software.opensuse.org/search?q={v}",
     "recorded": "vim", "fresh": ["emacs", "git"]},
    {"id": "fdroid_search", "site": "search.f-droid.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://search.f-droid.org/?q={v}",
     "recorded": "camera", "fresh": ["notes", "maps"]},
    {"id": "ourworldindata_search", "site": "ourworldindata.org",
     "category": "open_data_portal", "cls": "list", "param": "q",
     "url_tmpl": "https://ourworldindata.org/search?q={v}",
     "recorded": "energy", "fresh": ["literacy", "malaria"]},
    {"id": "datigov_it_search", "site": "www.dati.gov.it", "category": "open_data_portal",
     "cls": "list", "param": "q",
     "url_tmpl": "https://www.dati.gov.it/view-dataset?keywords={v}",
     "recorded": "scuola", "fresh": ["mobilita", "ambiente"]},
    {"id": "dataqld_search", "site": "www.data.qld.gov.au", "category": "open_data_portal",
     "cls": "list", "param": "q",
     "url_tmpl": "https://www.data.qld.gov.au/dataset?q={v}",
     "recorded": "water", "fresh": ["schools", "roads"]},
    {"id": "gutendex_ui", "site": "www.fadedpage.com", "category": "library_catalogue",
     "cls": "list", "param": "q",
     "url_tmpl": "https://www.fadedpage.com/csearch.php?author={v}",
     "recorded": "Dickens", "fresh": ["Twain", "Conrad"]},
    {"id": "wikisource_search", "site": "en.wikisource.org", "category": "reference",
     "cls": "list", "param": "search",
     "url_tmpl": "https://en.wikisource.org/w/index.php?search={v}&fulltext=1",
     "recorded": "constitution", "fresh": ["odyssey", "shakespeare"]},
    {"id": "arxiv_list", "site": "arxiv.org", "category": "reference",
     "cls": "list", "param": "archive",
     "url_tmpl": "https://arxiv.org/list/{v}/recent",
     "recorded": "cs.DC", "fresh": ["cs.PL", "math.CO"]},
    {"id": "rubygems_owner_detail", "site": "rubygems.org", "category": "package_registry",
     "cls": "detail", "param": "gem",
     "url_tmpl": "https://rubygems.org/gems/{v}/versions",
     "recorded": "rails", "fresh": ["rake", "rspec"]},
    {"id": "hackage_deps_detail", "site": "hackage.haskell.org",
     "category": "package_registry", "cls": "detail", "param": "pkg",
     "url_tmpl": "https://hackage.haskell.org/package/{v}/dependencies",
     "recorded": "aeson", "fresh": ["text", "bytestring"]},
    {"id": "standardebooks_author_detail", "site": "standardebooks.org",
     "category": "library_catalogue", "cls": "detail", "param": "author",
     "url_tmpl": "https://standardebooks.org/ebooks?query={v}",
     "recorded": "Conrad", "fresh": ["Melville", "Wells"]},
    {"id": "dblp_pid_detail", "site": "dblp.org", "category": "reference",
     "cls": "detail", "param": "venue",
     "url_tmpl": "https://dblp.org/db/journals/{v}/index.html",
     "recorded": "cacm", "fresh": ["tocs", "tods"]},
]

SAMPLE2 = LIST_WORKFLOWS + DETAIL_WORKFLOWS
BY_ID = {w["id"]: w for w in SAMPLE2 + RESERVE}

#: Hosts of the earlier corpus. No entry here may name one.
SAMPLE1_HOSTS = {
    "crates.io", "pypi.org", "www.nuget.org", "hex.pm", "pub.dev",
    "search.maven.org", "docs.rs", "pkg.go.dev", "developer.mozilla.org",
    "readthedocs.org", "wiki.archlinux.org", "openlibrary.org",
    "www.gutenberg.org", "portal.dnb.de", "archive.org", "catalog.data.gov",
    "data.europa.eu", "www.data.gov.uk", "www.govdata.de", "opendata.swiss",
    "codeberg.org", "gitlab.com", "sourceforge.net", "sr.ht",
    "en.wikipedia.org", "www.wikidata.org", "en.wiktionary.org",
    "nominatim.openstreetmap.org", "opendata.paris.fr",
    "geocoding-api.open-meteo.com", "transport.opendata.ch",
    "www.rfc-editor.org",
}


def _self_check():
    """Run at import: the selection rule must hold for the file as committed."""
    ids = [w["id"] for w in SAMPLE2 + RESERVE]
    assert len(ids) == len(set(ids)), "duplicate workflow id"
    overlap = {w["site"] for w in SAMPLE2 + RESERVE} & SAMPLE1_HOSTS
    assert not overlap, f"sample-2 entry reuses a sample-1 host: {overlap}"
    assert len(LIST_WORKFLOWS) >= 20, len(LIST_WORKFLOWS)
    assert len(DETAIL_WORKFLOWS) >= 10, len(DETAIL_WORKFLOWS)
    for w in SAMPLE2 + RESERVE:
        assert "{v}" in w["url_tmpl"], w["id"]
        assert len(w["fresh"]) >= 2, w["id"]
        assert w["recorded"] not in w["fresh"], w["id"]
        assert w["url_tmpl"].startswith("https://"), w["id"]


_self_check()
