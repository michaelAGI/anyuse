"""A blind benchmark corpus, fixed BEFORE any of it was fetched.

Selection rule, denominators, the (fixed) dry-look admission test, the proof
standard, the confirm-once learn protocol, the readiness rule and the decision
texts: the measurement harness, committed in the same commit as this file.
Nothing here may change after the first fetch; a replacement is taken from
``RESERVE`` in listed order and every swap is reported.

Shape of an entry, as in ``corpus_sample3``::

    {"id", "site", "category", "cls",           # "list" | "detail"
     "url_tmpl",                                 # {v} is the input value
     "param",                                    # the parameter's name
     "recorded",                                 # the value compiled from
     "fresh": [v1, v2],                          # values the proof uses
     "path_style": bool,                         # the id lives in the PATH
     "task"}                                     # what `anyuse learn` is told

What it adds
------------
* **No host from the earlier corpora.** ``corpus_sample3``'s 73-host list plus the 30
  hosts ``corpus_sample3`` itself drew on are asserted against at import.
* **The dry look is the fixed one** (`execution/web_runtime/dry_look.py`): one derivation,
  never overwritten; a bad third load is a named recorder fault and the entry
  is replaced.
* **The values were chosen from memory, not from a fetch.** Some will turn out
  not to exist, to redirect, or to sit behind a wall; that is what the reserve
  and the ledger are for, and every such case is named in the results.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# class A — search -> a visible result list
# ---------------------------------------------------------------------------

LIST_WORKFLOWS = [
    # -- package registries --------------------------------------------------
    {"id": "gentoo_search", "site": "packages.gentoo.org", "category": "package_registry",
     "cls": "list", "param": "q",
     "url_tmpl": "https://packages.gentoo.org/packages/search?q={v}",
     "recorded": "vim", "fresh": ["curl", "htop"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "freshports_search", "site": "www.freshports.org", "category": "package_registry",
     "cls": "list", "param": "query",
     "url_tmpl": "https://www.freshports.org/search.php?query={v}&stype=name&method=match",
     "recorded": "nginx", "fresh": ["redis", "tmux"], "path_style": False,
     "task": "search this site for the port {q} and stop when the result list is visible"},
    {"id": "ubuntu_search", "site": "packages.ubuntu.com", "category": "package_registry",
     "cls": "list", "param": "keywords",
     "url_tmpl": "https://packages.ubuntu.com/search?keywords={v}&searchon=names&suite=all&section=all",
     "recorded": "vim", "fresh": ["curl", "htop"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "msys2_search", "site": "packages.msys2.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://packages.msys2.org/search?q={v}",
     "recorded": "vim", "fresh": ["git", "curl"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "slackbuilds_search", "site": "slackbuilds.org", "category": "package_registry",
     "cls": "list", "param": "search",
     "url_tmpl": "https://slackbuilds.org/result/?search={v}&sv=15.0",
     "recorded": "htop", "fresh": ["tmux", "neovim"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "racket_search", "site": "pkgs.racket-lang.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://pkgs.racket-lang.org/search?q={v}",
     "recorded": "json", "fresh": ["http", "markdown"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "spi_search", "site": "swiftpackageindex.com", "category": "package_registry",
     "cls": "list", "param": "query",
     "url_tmpl": "https://swiftpackageindex.com/search?query={v}",
     "recorded": "alamofire", "fresh": ["snapkit", "kingfisher"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "dlang_search", "site": "code.dlang.org", "category": "package_registry",
     "cls": "list", "param": "q", "url_tmpl": "https://code.dlang.org/search?q={v}",
     "recorded": "vibe", "fresh": ["mir", "silly"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},
    {"id": "nimble_search", "site": "nimble.directory", "category": "package_registry",
     "cls": "list", "param": "query",
     "url_tmpl": "https://nimble.directory/search?query={v}",
     "recorded": "jester", "fresh": ["karax", "nimcrypto"], "path_style": False,
     "task": "search this site for the package {q} and stop when the result list is visible"},

    # -- code hosting ---------------------------------------------------------
    {"id": "openhub_search", "site": "www.openhub.net", "category": "code_hosting",
     "cls": "list", "param": "query", "url_tmpl": "https://www.openhub.net/p?query={v}",
     "recorded": "nginx", "fresh": ["redis", "curl"], "path_style": False,
     "task": "search this site for projects called {q} and stop when the result list is visible"},
    {"id": "savannah_search", "site": "savannah.gnu.org", "category": "code_hosting",
     "cls": "list", "param": "words",
     "url_tmpl": "https://savannah.gnu.org/search/?words={v}&type_of_search=soft&Search=Search",
     "recorded": "emacs", "fresh": ["grep", "make"], "path_style": False,
     "task": "search this site for projects called {q} and stop when the result list is visible"},
    {"id": "kde_search", "site": "invent.kde.org", "category": "code_hosting",
     "cls": "list", "param": "search",
     "url_tmpl": "https://invent.kde.org/explore/projects?search={v}",
     "recorded": "krita", "fresh": ["kdenlive", "okular"], "path_style": False,
     "task": "search this site for projects called {q} and stop when the result list is visible"},
    {"id": "gnome_search", "site": "gitlab.gnome.org", "category": "code_hosting",
     "cls": "list", "param": "search",
     "url_tmpl": "https://gitlab.gnome.org/explore/projects?search={v}",
     "recorded": "gtk", "fresh": ["nautilus", "gimp"], "path_style": False,
     "task": "search this site for projects called {q} and stop when the result list is visible"},

    # -- documentation / manual pages ----------------------------------------
    {"id": "mankier_search", "site": "www.mankier.com", "category": "documentation_search",
     "cls": "list", "param": "q", "url_tmpl": "https://www.mankier.com/?q={v}",
     "recorded": "rsync", "fresh": ["tar", "ssh"], "path_style": False,
     "task": "search this site for the manual page {q} and stop when the result list is visible"},
    {"id": "freebsdman_search", "site": "www.freebsd.org", "category": "documentation_search",
     "cls": "list", "param": "query",
     "url_tmpl": "https://www.freebsd.org/cgi/man.cgi?query={v}&apropos=1",
     "recorded": "tar", "fresh": ["ssh", "cp"], "path_style": False,
     "task": "search this site for the manual page {q} and stop when the result list is visible"},

    # -- reference / scholarly -----------------------------------------------
    {"id": "pubmed_search", "site": "pubmed.ncbi.nlm.nih.gov", "category": "reference",
     "cls": "list", "param": "term", "url_tmpl": "https://pubmed.ncbi.nlm.nih.gov/?term={v}",
     "recorded": "aspirin", "fresh": ["insulin", "melatonin"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "philpapers_search", "site": "philpapers.org", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://philpapers.org/s/{v}",
     "recorded": "consciousness", "fresh": ["justice", "knowledge"], "path_style": True,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "eric_search", "site": "eric.ed.gov", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://eric.ed.gov/?q={v}",
     "recorded": "literacy", "fresh": ["dyslexia", "homework"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "upennbooks_search", "site": "onlinebooks.library.upenn.edu",
     "category": "library_catalogue", "cls": "list", "param": "author",
     "url_tmpl": "https://onlinebooks.library.upenn.edu/webbin/book/search?author={v}&amode=words",
     "recorded": "dickens", "fresh": ["austen", "twain"], "path_style": False,
     "task": "search this catalogue for the author {q} and stop when the result list is visible"},
    {"id": "isfdb_search", "site": "www.isfdb.org", "category": "library_catalogue",
     "cls": "list", "param": "arg",
     "url_tmpl": "https://www.isfdb.org/cgi-bin/se.cgi?arg={v}&type=Name",
     "recorded": "asimov", "fresh": ["heinlein", "herbert"], "path_style": False,
     "task": "search this site for the author {q} and stop when the result list is visible"},
    {"id": "duden_search", "site": "www.duden.de", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://www.duden.de/suchen/dudenonline/{v}",
     "recorded": "haus", "fresh": ["wasser", "licht"], "path_style": True,
     "task": "search this site for the word {q} and stop when the result list is visible"},

    # -- open data / government ----------------------------------------------
    {"id": "humdata_search", "site": "data.humdata.org", "category": "open_data",
     "cls": "list", "param": "q", "url_tmpl": "https://data.humdata.org/search?q={v}",
     "recorded": "population", "fresh": ["health", "education"], "path_style": False,
     "task": "search this site for datasets about {q} and stop when the result list is visible"},
    {"id": "canada_search", "site": "open.canada.ca", "category": "open_data",
     "cls": "list", "param": "q", "url_tmpl": "https://open.canada.ca/data/en/dataset?q={v}",
     "recorded": "water", "fresh": ["forest", "climate"], "path_style": False,
     "task": "search this site for datasets about {q} and stop when the result list is visible"},
    {"id": "hansard_search", "site": "hansard.parliament.uk", "category": "government",
     "cls": "list", "param": "searchTerm",
     "url_tmpl": "https://hansard.parliament.uk/search?searchTerm={v}",
     "recorded": "housing", "fresh": ["education", "transport"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "legislation_search", "site": "www.legislation.gov.uk", "category": "government",
     "cls": "list", "param": "title",
     "url_tmpl": "https://www.legislation.gov.uk/all?title={v}",
     "recorded": "housing", "fresh": ["education", "transport"], "path_style": False,
     "task": "search this site for legislation titled {q} and stop when the result list is visible"},
    {"id": "dailymed_search", "site": "dailymed.nlm.nih.gov", "category": "reference",
     "cls": "list", "param": "query",
     "url_tmpl": "https://dailymed.nlm.nih.gov/dailymed/search.cfm?query={v}",
     "recorded": "aspirin", "fresh": ["ibuprofen", "metformin"], "path_style": False,
     "task": "search this site for the drug {q} and stop when the result list is visible"},
]

# ---------------------------------------------------------------------------
# class B — open ONE item, read its named fields.
# The fixed quota: at least 8 of these carry the identifier in the PATH.
# ---------------------------------------------------------------------------

DETAIL_WORKFLOWS = [
    # -- multi-segment path identifiers --------------------------------------
    {"id": "gentoo_detail", "site": "packages.gentoo.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://packages.gentoo.org/packages/{v}",
     "recorded": "app-editors/vim", "fresh": ["net-misc/curl", "sys-process/htop"],
     "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "freshports_detail", "site": "www.freshports.org", "category": "package_registry",
     "cls": "detail", "param": "port", "url_tmpl": "https://www.freshports.org/{v}",
     "recorded": "www/nginx", "fresh": ["databases/redis", "sysutils/tmux"],
     "path_style": True,
     "task": "open the port page for {q} and stop when its named fields are visible"},
    {"id": "slackbuilds_detail", "site": "slackbuilds.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://slackbuilds.org/repository/15.0/{v}/",
     "recorded": "system/htop", "fresh": ["system/tmux", "development/neovim"],
     "path_style": True,
     "task": "open the page for {q} and stop when its named fields are visible"},
    {"id": "spi_detail", "site": "swiftpackageindex.com", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://swiftpackageindex.com/{v}",
     "recorded": "Alamofire/Alamofire", "fresh": ["SnapKit/SnapKit", "onevcat/Kingfisher"],
     "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "legislation_detail", "site": "www.legislation.gov.uk", "category": "government",
     "cls": "detail", "param": "act",
     "url_tmpl": "https://www.legislation.gov.uk/ukpga/{v}",
     "recorded": "2018/12", "fresh": ["1998/42", "2010/15"], "path_style": True,
     "task": "open the page for the act ukpga {q} and stop when its contents are visible"},

    # -- single-segment path identifiers -------------------------------------
    {"id": "ubuntu_detail", "site": "packages.ubuntu.com", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://packages.ubuntu.com/noble/{v}",
     "recorded": "vim", "fresh": ["curl", "htop"], "path_style": True,
     "task": "open the noble package page for {q} and stop when its named fields are visible"},
    {"id": "msys2_detail", "site": "packages.msys2.org", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://packages.msys2.org/base/{v}",
     "recorded": "vim", "fresh": ["git", "curl"], "path_style": True,
     "task": "open the base package page for {q} and stop when its named fields are visible"},
    {"id": "brew_detail", "site": "formulae.brew.sh", "category": "package_registry",
     "cls": "detail", "param": "formula",
     "url_tmpl": "https://formulae.brew.sh/formula/{v}",
     "recorded": "wget", "fresh": ["jq", "htop"], "path_style": True,
     "task": "open the formula page for {q} and stop when its named fields are visible"},
    {"id": "mankier_detail", "site": "www.mankier.com", "category": "documentation_search",
     "cls": "detail", "param": "page", "url_tmpl": "https://www.mankier.com/1/{v}",
     "recorded": "rsync", "fresh": ["tar", "ssh"], "path_style": True,
     "task": "open the manual page for {q} and stop when its named sections are visible"},
    {"id": "debiantracker_detail", "site": "tracker.debian.org",
     "category": "package_registry", "cls": "detail", "param": "pkg",
     "url_tmpl": "https://tracker.debian.org/pkg/{v}",
     "recorded": "vim", "fresh": ["curl", "htop"], "path_style": True,
     "task": "open the tracker page for the source package {q} and stop when its named fields are visible"},
    {"id": "nvd_detail", "site": "nvd.nist.gov", "category": "reference",
     "cls": "detail", "param": "cve", "url_tmpl": "https://nvd.nist.gov/vuln/detail/{v}",
     "recorded": "CVE-2021-44228", "fresh": ["CVE-2014-0160", "CVE-2017-5754"],
     "path_style": True,
     "task": "open the page for {q} and stop when its named fields are visible"},
    {"id": "lastfm_detail", "site": "www.last.fm", "category": "reference",
     "cls": "detail", "param": "artist", "url_tmpl": "https://www.last.fm/music/{v}",
     "recorded": "Radiohead", "fresh": ["Burial", "Portishead"], "path_style": True,
     "task": "open the artist page for {q} and stop when its named fields are visible"},
    {"id": "ourairports_detail", "site": "ourairports.com", "category": "weather_transport",
     "cls": "detail", "param": "icao", "url_tmpl": "https://ourairports.com/airports/{v}/",
     "recorded": "EGLL", "fresh": ["KJFK", "RJTT"], "path_style": True,
     "task": "open the airport page for {q} and stop when its named fields are visible"},
    {"id": "bioconductor_detail", "site": "bioconductor.org", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://bioconductor.org/packages/{v}/",
     "recorded": "limma", "fresh": ["edgeR", "DESeq2"], "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},

    # -- a delimited fragment inside ONE segment (the path_part case) ---------
    {"id": "php_detail", "site": "www.php.net", "category": "documentation_search",
     "cls": "detail", "param": "function",
     "url_tmpl": "https://www.php.net/manual/en/function.{v}.php",
     "recorded": "array-map", "fresh": ["str-replace", "preg-match"], "path_style": True,
     "task": "open the manual page for the function {q} and stop when its named sections are visible"},
    {"id": "pursuit_detail", "site": "pursuit.purescript.org", "category": "package_registry",
     "cls": "detail", "param": "pkg",
     "url_tmpl": "https://pursuit.purescript.org/packages/purescript-{v}",
     "recorded": "prelude", "fresh": ["arrays", "maybe"], "path_style": True,
     "task": "open the package page for purescript-{q} and stop when its named fields are visible"},
]

# ---------------------------------------------------------------------------
# reserve — used IN THIS ORDER to replace a robots-disallowed entry, a
# dry-look rejection or a recorder fault on the third load
# ---------------------------------------------------------------------------

RESERVE = [
    {"id": "cocoapods_detail", "site": "cocoapods.org", "category": "package_registry",
     "cls": "detail", "param": "pod", "url_tmpl": "https://cocoapods.org/pods/{v}",
     "recorded": "Alamofire", "fresh": ["SnapKit", "Kingfisher"], "path_style": True,
     "task": "open the pod page for {q} and stop when its named fields are visible"},
    {"id": "tmdb_detail", "site": "www.themoviedb.org", "category": "reference",
     "cls": "detail", "param": "movie", "url_tmpl": "https://www.themoviedb.org/movie/{v}",
     "recorded": "603", "fresh": ["348", "949"], "path_style": True,
     "task": "open the movie page with id {q} and stop when its named fields are visible"},
    {"id": "perldoc_detail", "site": "perldoc.perl.org", "category": "documentation_search",
     "cls": "detail", "param": "page", "url_tmpl": "https://perldoc.perl.org/{v}",
     "recorded": "perlre", "fresh": ["perlsyn", "perlop"], "path_style": True,
     "task": "open the documentation page {q} and stop when its named sections are visible"},
    {"id": "savannah_detail", "site": "savannah.gnu.org", "category": "code_hosting",
     "cls": "detail", "param": "project", "url_tmpl": "https://savannah.gnu.org/projects/{v}",
     "recorded": "emacs", "fresh": ["grep", "make"], "path_style": True,
     "task": "open the project page for {q} and stop when its named fields are visible"},
    {"id": "plato_search", "site": "plato.stanford.edu", "category": "reference",
     "cls": "list", "param": "query",
     "url_tmpl": "https://plato.stanford.edu/search/searcher.py?query={v}",
     "recorded": "kant", "fresh": ["hume", "aristotle"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "plato_detail", "site": "plato.stanford.edu", "category": "reference",
     "cls": "detail", "param": "entry", "url_tmpl": "https://plato.stanford.edu/entries/{v}/",
     "recorded": "kant", "fresh": ["hume", "aristotle"], "path_style": True,
     "task": "open the encyclopedia entry {q} and stop when its table of contents is visible"},
    {"id": "ncatlab_search", "site": "ncatlab.org", "category": "reference",
     "cls": "list", "param": "query", "url_tmpl": "https://ncatlab.org/nlab/search?query={v}",
     "recorded": "topos", "fresh": ["functor", "sheaf"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "ncatlab_detail", "site": "ncatlab.org", "category": "reference",
     "cls": "detail", "param": "page", "url_tmpl": "https://ncatlab.org/nlab/show/{v}",
     "recorded": "topos", "fresh": ["functor", "sheaf"], "path_style": True,
     "task": "open the page {q} and stop when its table of contents is visible"},
    {"id": "britannica_detail", "site": "www.britannica.com", "category": "reference",
     "cls": "detail", "param": "topic", "url_tmpl": "https://www.britannica.com/science/{v}",
     "recorded": "volcano", "fresh": ["glacier", "photosynthesis"], "path_style": True,
     "task": "open the article on {q} and stop when its named sections are visible"},
    {"id": "etymonline_search", "site": "www.etymonline.com", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://www.etymonline.com/search?q={v}",
     "recorded": "house", "fresh": ["water", "light"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "lichess_detail", "site": "lichess.org", "category": "reference",
     "cls": "detail", "param": "user", "url_tmpl": "https://lichess.org/@/{v}",
     "recorded": "DrNykterstein", "fresh": ["penguingim1", "nihalsarin"], "path_style": True,
     "task": "open the profile page for {q} and stop when its named fields are visible"},
    {"id": "debiansec_detail", "site": "security-tracker.debian.org",
     "category": "reference", "cls": "detail", "param": "pkg",
     "url_tmpl": "https://security-tracker.debian.org/tracker/source-package/{v}",
     "recorded": "openssl", "fresh": ["curl", "vim"], "path_style": True,
     "task": "open the security tracker page for the source package {q} and stop when its table is visible"},
    {"id": "linuxdie_detail", "site": "linux.die.net", "category": "documentation_search",
     "cls": "detail", "param": "page", "url_tmpl": "https://linux.die.net/man/1/{v}",
     "recorded": "rsync", "fresh": ["tar", "ssh"], "path_style": True,
     "task": "open the manual page for {q} and stop when its named sections are visible"},
    {"id": "manpagesubuntu_detail", "site": "manpages.ubuntu.com",
     "category": "documentation_search", "cls": "detail", "param": "page",
     "url_tmpl": "https://manpages.ubuntu.com/manpages/noble/en/man1/{v}.1.html",
     "recorded": "rsync", "fresh": ["tar", "ssh"], "path_style": True,
     "task": "open the manual page for {q} and stop when its named sections are visible"},
    {"id": "re3data_search", "site": "www.re3data.org", "category": "open_data",
     "cls": "list", "param": "query", "url_tmpl": "https://www.re3data.org/search?query={v}",
     "recorded": "genomics", "fresh": ["climate", "astronomy"], "path_style": False,
     "task": "search this site for repositories about {q} and stop when the result list is visible"},
    {"id": "london_search", "site": "data.london.gov.uk", "category": "open_data",
     "cls": "list", "param": "q", "url_tmpl": "https://data.london.gov.uk/search/?q={v}",
     "recorded": "housing", "fresh": ["transport", "crime"], "path_style": False,
     "task": "search this site for datasets about {q} and stop when the result list is visible"},
    {"id": "podcastindex_search", "site": "podcastindex.org", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://podcastindex.org/search?q={v}",
     "recorded": "history", "fresh": ["science", "cooking"], "path_style": False,
     "task": "search this site for podcasts about {q} and stop when the result list is visible"},
    {"id": "setlistfm_search", "site": "www.setlist.fm", "category": "reference",
     "cls": "list", "param": "query", "url_tmpl": "https://www.setlist.fm/search?query={v}",
     "recorded": "radiohead", "fresh": ["burial", "portishead"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "framagit_search", "site": "framagit.org", "category": "code_hosting",
     "cls": "list", "param": "search",
     "url_tmpl": "https://framagit.org/explore/projects?search={v}",
     "recorded": "framadate", "fresh": ["framaforms", "mobilizon"], "path_style": False,
     "task": "search this site for projects called {q} and stop when the result list is visible"},
    {"id": "repec_search", "site": "ideas.repec.org", "category": "reference",
     "cls": "list", "param": "q", "url_tmpl": "https://ideas.repec.org/cgi-bin/htsearch?q={v}",
     "recorded": "inflation", "fresh": ["unemployment", "tariffs"], "path_style": False,
     "task": "search this site for {q} and stop when the result list is visible"},
    {"id": "nvd_search", "site": "nvd.nist.gov", "category": "reference",
     "cls": "list", "param": "query",
     "url_tmpl": "https://nvd.nist.gov/vuln/search/results?form_type=Basic&results_type=overview&query={v}&search_type=all",
     "recorded": "openssl", "fresh": ["log4j", "curl"], "path_style": False,
     "task": "search this site for vulnerabilities mentioning {q} and stop when the result list is visible"},
    {"id": "dlang_detail", "site": "code.dlang.org", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://code.dlang.org/packages/{v}",
     "recorded": "vibe-d", "fresh": ["silly", "mir-algorithm"], "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "nimble_detail", "site": "nimble.directory", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://nimble.directory/pkg/{v}",
     "recorded": "jester", "fresh": ["karax", "nimcrypto"], "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "racket_detail", "site": "pkgs.racket-lang.org", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://pkgs.racket-lang.org/package/{v}",
     "recorded": "rackunit-lib", "fresh": ["http-easy", "markdown"], "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "duden_detail", "site": "www.duden.de", "category": "reference",
     "cls": "detail", "param": "word", "url_tmpl": "https://www.duden.de/rechtschreibung/{v}",
     "recorded": "Haus", "fresh": ["Wasser", "Licht"], "path_style": True,
     "task": "open the dictionary entry for {q} and stop when its named fields are visible"},
    {"id": "elpa_detail", "site": "elpa.gnu.org", "category": "package_registry",
     "cls": "detail", "param": "pkg", "url_tmpl": "https://elpa.gnu.org/packages/{v}.html",
     "recorded": "vertico", "fresh": ["consult", "marginalia"], "path_style": True,
     "task": "open the package page for {q} and stop when its named fields are visible"},
    {"id": "companieshouse_search", "site": "find-and-update.company-information.service.gov.uk",
     "category": "government", "cls": "list", "param": "q",
     "url_tmpl": "https://find-and-update.company-information.service.gov.uk/search?q={v}",
     "recorded": "tesco", "fresh": ["sainsbury", "greggs"], "path_style": False,
     "task": "search this site for companies called {q} and stop when the result list is visible"},
    {"id": "bgp_detail", "site": "bgp.he.net", "category": "reference",
     "cls": "detail", "param": "asn", "url_tmpl": "https://bgp.he.net/AS{v}",
     "recorded": "13335", "fresh": ["15169", "32934"], "path_style": True,
     "task": "open the page for AS{q} and stop when its named fields are visible"},
]

SAMPLE4 = LIST_WORKFLOWS + DETAIL_WORKFLOWS
SAMPLE = SAMPLE4
BY_ID = {w["id"]: w for w in SAMPLE4 + RESERVE}

#: Every host the earlier corpora drew on. No entry here may name one.
#: ``corpus_sample3``'s own list covers the two before it; its entries and reserve add 30.
from corpus_sample3 import RESERVE as _R3, SAMPLE3 as _S3, USED_HOSTS as _S12  # noqa: E402

USED_HOSTS = set(_S12) | {w["site"] for w in _S3 + _R3}

#: Fixed minimums. The run fails loudly rather than quietly shrinking.
TARGET_ALLOWED = 24
TARGET_PATH_STYLE_DETAIL = 8


def _self_check():
    """Run at import: the selection rule must hold for the file as committed."""
    ids = [w["id"] for w in SAMPLE4 + RESERVE]
    assert len(ids) == len(set(ids)), "duplicate workflow id"
    overlap = {w["site"] for w in SAMPLE4 + RESERVE} & USED_HOSTS
    assert not overlap, f"sample-4 entry reuses a host from samples 1-3: {overlap}"
    path_detail = [w for w in DETAIL_WORKFLOWS if w["path_style"]]
    assert len(path_detail) >= TARGET_PATH_STYLE_DETAIL, len(path_detail)
    assert len(SAMPLE4) >= TARGET_ALLOWED, len(SAMPLE4)
    for w in SAMPLE4 + RESERVE:
        assert "{v}" in w["url_tmpl"], w["id"]
        assert len(w["fresh"]) >= 2, w["id"]
        assert w["recorded"] not in w["fresh"], w["id"]
        assert w["url_tmpl"].startswith("https://"), w["id"]
        assert w["task"] and "{q}" in w["task"], w["id"]
        assert w["cls"] in ("list", "detail"), w["id"]
        assert all(v.isascii() and " " not in v for v in [w["recorded"], *w["fresh"]]), w["id"]
        if w["path_style"]:
            head = w["url_tmpl"].split("{v}")[0]
            assert "?" not in head, f"{w['id']} claims path_style but {{v}} is in the query"


_self_check()
