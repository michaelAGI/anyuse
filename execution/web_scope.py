"""Widening of the guarded browser's web scope (all OFF by default).

``execution/cdp_guard.py`` sends a request only to the page's own host (plus
hosts a run lists explicitly), and only GET/HEAD. That misses search results served by a hosted search backend the
site embeds (Google Programmable Search, Yext), scripts on the site's own CDN
host, and POST searches. This module is the policy for four settings
(``settings.py``; every one security-relevant, TTY confirmation to enable):

* ``ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS`` -- a provider from the DECLARED
  catalogue (:data:`PROVIDERS`) is granted for ONE site only after the site's
  own document or own script names it (script URL / config, never guessed from
  the provider's traffic). The grant covers that provider's search hosts and
  paths only, GET/HEAD plus the provider's documented read-only query method
  (e.g. Algolia's ``POST /1/indexes/*/queries``), and the CORS preflight of that
  method. Analytics endpoints of the same vendors are not in the catalogue.
* ``ANYUSE_ALLOW_READONLY_POST_SEARCH`` -- a same-site POST is sent only when it
  is the page's own search: the action of a POST search form found in the
  site's document with a body limited to that form's fields, or a small body
  that carries the site's search value in a search-like field. Paths and field
  names that suggest login, cart, account, write, delete, subscribe, comment or
  vote are refused. That the response is a result listing is decided by the
  unchanged held-out proof: a POST whose answer is not a listing never proves.
* ``ANYUSE_ALLOW_SITE_OWN_HOSTS`` -- hosts of the site's registrable domain
  (not on a shared-hosting suffix, not an analytics-looking label), or a host
  declared for the site with a reason; GET/HEAD. ON by default
  (``from_settings``), off = the site's exact host.
* ``ANYUSE_THIRD_PARTY_POLICY`` -- ``block`` (default), ``allow_with_blocklist``
  (any other host, minus the pinned EasyList/EasyPrivacy DOMAIN snapshot in
  ``web_scope_data/``), ``allow_all``; GET/HEAD only. Challenge-provider hosts
  are never allowed by it (the live-solve exception is separate).

**One setting for third-party hosts: ``ANYUSE_THIRD_PARTY``**
(:func:`third_party_mode`, :func:`third_party_flags`), the ONE place every guarded path
(the agent, the compile and its proof replays, the replay, the MCP route, the fallback)
takes its third-party scope from:

* ``off`` -- first-party only (no provider, no third-party host);
* ``search`` (DEFAULT) -- the site's own search provider: the third-party host the
  site's own page sends the typed value to in a data call (:meth:`WebScope.observed_call`:
  GET/HEAD under the path the value went to, its CORS preflight, a read-only search
  POST right after typing), and the declared catalogue :data:`PROVIDERS` (Algolia,
  Doofinder, Klevu, Findologic, Searchspring, ...) as the hint that lets a provider's
  widget script and documented read query through once the site's own page names it;
* ``any`` -- any third-party host the site's own page uses for its content (APIs,
  widgets, data CDNs): every host but those on the pinned ad/tracker snapshot.

In every mode: read-only, as the page itself sends it (GET/HEAD, a provider's
documented read query); robots.txt of EVERY host checked (RFC 9309,
``api_permission``); the NEVER_CONTACT list, and government / ``.int`` / europa /
military hosts when ``ANYUSE_REFUSE_GOV_HOSTS`` is on, refused before any request
(:func:`contact_refusal`); every third-party host on the run record
(:meth:`WebScope.site_report`); paced with the site; nothing beyond what the page
itself requests. ``ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS`` and
``ANYUSE_THIRD_PARTY_POLICY`` are superseded by it and no longer read.

Every host allowed here counts against the page's SITE (one pacer slot, one
request total, one byte total) and its own ``robots.txt`` is consulted through
the guard's ``allow_url``. A host outside all of these stays third-party: failed
in the browser, and fatal if a response from it reaches the network anyway.

Provider keys: a grant never reads a key. The page sends its own public search
key; the guard's records (sent/failed URLs) and the bench's state are passed
through :func:`redact_keys`. Tier-1 compiled HTTP tools never carry a provider
key: attach redacts credential-like query keys at ingestion, so a
page-keyed tool reads the key from the site's page per call, else the browser replay's
page itself sends its key (docs/THREAT_MODEL.md).
"""
from __future__ import annotations

import gzip
import html.parser
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

KIND_PROVIDER = "provider"
KIND_POST = "post_search"
KIND_OWN = "own_host"
KIND_THIRD = "third_party"
#: a store platform's own asset CDN, first party for static assets (``PLATFORM_CDNS``)
KIND_PLATFORM = "platform_cdn"
#: the page's own SCRIPTS and STYLESHEETS from any other
#: registrable domain the page requests - GET only, by the browser's own resource type; never images, fonts, media,
#: XHR / fetch, websockets or beacons; never a host on the pinned ad / tracker snapshot; never a request carrying the
#: typed value
KIND_PAGE_CODE = "page_code"
PAGE_CODE_TYPES = frozenset({"Script", "Stylesheet"})
PAGE_CODE_SETTING = "ANYUSE_THIRD_PARTY_PAGE_CODE"
KINDS = (KIND_PROVIDER, KIND_POST, KIND_OWN, KIND_THIRD, KIND_PLATFORM, KIND_PAGE_CODE)
#: what a platform CDN may serve the site's page: static assets only, GET/HEAD only
PLATFORM_TYPES = frozenset({"Script", "Stylesheet", "Font", "Image"})
THIRD_PARTY_POLICIES = ("block", "allow_with_blocklist", "allow_all")
READ = frozenset({"GET", "HEAD"})
MAX_POST_BODY = 8192
#: sources larger than this are scanned only up to it
MAX_SCAN_CHARS = 3_000_000
#: never granted by any setting here (the live-solve exception is separate)
CHALLENGE_HOSTS = frozenset({
    "challenges.cloudflare.com", "hcaptcha.com", "js.hcaptcha.com", "newassets.hcaptcha.com",
    "imgs.hcaptcha.com", "www.recaptcha.net", "api.hcaptcha.com",
})
CHALLENGE_PATHS = (("www.google.com", "/recaptcha/"), ("www.gstatic.com", "/recaptcha/"))
BLOCKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_scope_data",
                              "easylist_domains.txt.gz")
#: pinned snapshot (docs/TRAFFIC_POLICY.md): sha256 of the uncompressed text
BLOCKLIST_SHA256 = "f64d1a2992b7a78787d7a40fb4c1e814a7daca09f2a01a88a38fe9a45490ac4d"
#: the ONE third-party setting (``ANYUSE_THIRD_PARTY``) and its default
THIRD_PARTY_MODES = ("off", "search", "any")
DEFAULT_THIRD_PARTY = "search"


def third_party_mode(value: Optional[str] = None) -> str:
    """The run's third-party mode: ``value`` when given, else the setting ``ANYUSE_THIRD_PARTY`` (default
    ``search``). Anything else is refused (a typo never widens the scope)."""
    if value is None:
        try:
            from anyuse.settings import get as get_setting
            value = get_setting("ANYUSE_THIRD_PARTY")
        except Exception:  # noqa: BLE001 - no settings module (an offline harness): the default
            value = DEFAULT_THIRD_PARTY
    m = str(value or DEFAULT_THIRD_PARTY).strip().lower()
    if m not in THIRD_PARTY_MODES:
        raise ValueError(f"ANYUSE_THIRD_PARTY must be one of {THIRD_PARTY_MODES}, not {value!r}")
    return m


def third_party_flags(mode: Optional[str] = None) -> Dict[str, Any]:
    """What a mode allows, as :class:`WebScope` flags: ``providers`` (the site's own search provider) and
    ``third_party`` (``block`` / ``allow_with_blocklist``). Every guarded path builds its scope from this."""
    m = third_party_mode(mode)
    return {"providers": m in ("search", "any"), "third_party": "allow_with_blocklist" if m == "any" else "block",
            "platform_cdn": m in ("search", "any"), "observed_providers": m in ("search", "any"), "mode": m}


def contact_refusal(host: str, refuse_gov: Optional[bool] = None) -> str:
    """Why ``host`` is never contacted, whatever the mode, or ``""``: a host on the NEVER_CONTACT list, and a
    government / ``.int`` / europa / military host (``gov_hosts``, fail closed) when the government rule is on
    (``refuse_gov``; ``None`` reads ``ANYUSE_REFUSE_GOV_HOSTS``). The benchmarks screen more hosts on top of this
    (``web_runtime/web_arch_live.refusal``)."""
    from anyuse.execution.gov_hosts import government_refusal
    h = _name(host)
    if not h:
        return "no host"
    why = government_refusal(h, refuse_gov)
    if why:
        return f"government host ({why})"
    from anyuse.execution.site_modes import _never_contact
    if h in _never_contact():
        return "on NEVER_CONTACT"
    return ""


# ---------------------------------------------------------------------------
# host helpers
# ---------------------------------------------------------------------------

def host_of(url: str) -> str:
    p = urllib.parse.urlsplit(url or "")
    host = p.netloc.lower().rsplit("@", 1)[-1]
    if (p.scheme == "https" and host.endswith(":443")) or (p.scheme == "http" and host.endswith(":80")):
        host = host.rsplit(":", 1)[0]
    return host


def _name(host: str) -> str:
    return (host or "").split(":", 1)[0].lower().rstrip(".")


#: multi-label PUBLIC suffixes (a small built-in list; a suffix it does not hold is
#: read by :data:`GENERIC_SECOND_LEVELS` and, when still unknown, by :meth:`WebScope.is_own_host`'s binding rule)
PUBLIC_SUFFIX2 = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk",
    "nhs.uk", "police.uk", "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "org.nz", "govt.nz", "ac.nz", "net.nz", "co.jp", "ne.jp", "or.jp", "ac.jp",
    "go.jp", "gr.jp", "ed.jp", "lg.jp", "co.kr", "or.kr", "go.kr", "ac.kr", "com.br",
    "org.br", "gov.br", "net.br", "edu.br", "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "ac.cn", "com.hk", "org.hk", "gov.hk", "edu.hk", "com.tw", "org.tw", "gov.tw", "edu.tw",
    "com.sg", "org.sg", "gov.sg", "edu.sg", "com.my", "org.my", "gov.my", "co.in", "org.in",
    "gov.in", "ac.in", "net.in", "co.za", "org.za", "gov.za", "ac.za", "com.mx", "org.mx",
    "gob.mx", "com.ar", "org.ar", "gob.ar", "com.tr", "org.tr", "gov.tr", "edu.tr", "co.il",
    "org.il", "gov.il", "ac.il", "com.pl", "org.pl", "gov.pl", "edu.pl", "gv.at", "ac.at",
    "co.at", "or.at", "com.es", "org.es", "gob.es", "com.pt", "gov.pt", "com.ua", "gov.ua",
    "com.ru", "org.ru", "com.eg", "gov.eg", "com.sa", "gov.sa", "com.ph", "gov.ph",
    "com.vn", "gov.vn", "co.id", "go.id", "ac.id", "or.id", "co.th", "go.th", "ac.th",
    "com.co", "gov.co", "com.pe", "gob.pe", "com.ng", "gov.ng", "co.ke", "go.ke",
    "europa.eu", "com.de", "gc.ca", "gov.scot", "gov.wales",
})
#: shared-hosting (PRIVATE) suffixes: every label under them is a different
#: tenant, so "the same registrable domain" is the full tenant host here and a
#: sibling tenant is never the site's own host
PRIVATE_SUFFIXES = frozenset({
    "github.io", "gitlab.io", "herokuapp.com", "netlify.app", "netlify.com", "vercel.app",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "appspot.com", "blogspot.com",
    "wordpress.com", "wixsite.com", "squarespace.com", "weebly.com", "tumblr.com",
    "azurewebsites.net", "cloudapp.net", "azureedge.net", "cloudfront.net",
    "s3.amazonaws.com", "amazonaws.com", "fly.dev", "onrender.com", "readthedocs.io",
    "sharepoint.com", "myshopify.com", "substack.com", "neocities.org", "glitch.me",
    "surge.sh", "now.sh", "repl.co", "ngrok.io", "fastly.net", "akamaized.net",
    "edgekey.net", "cdn77.org", "b-cdn.net", "googleusercontent.com", "storage.googleapis.com",
    "blob.core.windows.net", "digitaloceanspaces.com", "r2.dev", "hf.space", "streamlit.app",
})
#: first labels that name tracking/telemetry even on the site's own domain
TRACKING_LABELS = frozenset({
    "metrics", "smetrics", "stats", "analytics", "collect", "collector", "tracking", "track",
    "tracker", "pixel", "beacon", "beacons", "ads", "ad", "adserver", "adservice", "telemetry",
    "log", "logs", "logging", "sentry", "tag", "tags", "tagmanager", "gtm", "sst", "ssc",
    "insights", "events", "rum",
    # own hosts are on by default: the self-hosted analytics products' usual labels (a site's
    # own ``matomo.<site>`` host carries its analytics)
    "matomo", "piwik",
})


#: the generic TLD strings (IANA's original generic top-level domains, RFC 1591 section 2) that country registries
#: reuse as their public second levels: under a two-letter country code TLD, ``com.pk``, ``org.ng``, ``net.pl``,
#: ``co.ve``, ``ac.za``, ``edu.tr`` and ``gov.br`` are suffixes a registrant registers under, never one
#: registrant's domain (``co`` and ``ac`` are the conventional commercial and academic second levels of the same
#: scheme)
GENERIC_SECOND_LEVELS = frozenset({"com", "net", "org", "edu", "gov", "ac", "co"})

#: an optional pinned copy of the Public Suffix List (https://publicsuffix.org/list/public_suffix_list.dat, ICANN and
#: PRIVATE sections). Used when present and when its SHA-256 equals the hex digest kept next to it
#: (``public_suffix_list.dat.sha256``); without it the built-in lists and :data:`GENERIC_SECOND_LEVELS` decide.
PSL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_scope_data", "public_suffix_list.dat")
PSL_SHA256_FILE = PSL_FILE + ".sha256"
_PSL: Optional[Tuple[frozenset, frozenset, frozenset]] = None
_PSL_STATUS = ""


def _idna(label: str) -> str:
    try:
        return label.encode("idna").decode("ascii").lower() if any(ord(c) > 127 for c in label) else label.lower()
    except UnicodeError:
        return ""


def parse_psl(text: str) -> Tuple[frozenset, frozenset, frozenset]:
    """``(rules, wildcards, exceptions)`` of a Public Suffix List text: plain rules (``co.uk``), the parents of
    wildcard rules (``*.ck`` -> ``ck``) and exception rules (``!www.ck`` -> ``www.ck``), IDNA-encoded."""
    rules, wild, exc = set(), set(), set()
    for raw in (text or "").splitlines():
        line = raw.strip().split()[0] if raw.strip() else ""
        if not line or line.startswith("//"):
            continue
        kind = "rule"
        if line.startswith("!"):
            kind, line = "exc", line[1:]
        elif line.startswith("*."):
            kind, line = "wild", line[2:]
        name = ".".join(_idna(x) for x in line.strip(".").split("."))
        if not name or ".." in name or name.startswith(".") or name.endswith("."):
            continue
        {"rule": rules, "wild": wild, "exc": exc}[kind].add(name)
    return frozenset(rules), frozenset(wild), frozenset(exc)


def public_suffix_list() -> Optional[Tuple[frozenset, frozenset, frozenset]]:
    """The pinned Public Suffix List (:data:`PSL_FILE`) as :func:`parse_psl` reads it, or ``None`` when the file is
    absent or does not match its SHA-256 (:func:`psl_status` says which). Loaded once."""
    global _PSL, _PSL_STATUS
    if _PSL is not None or _PSL_STATUS:
        return _PSL
    import hashlib
    if not os.path.exists(PSL_FILE):
        _PSL_STATUS = "absent"
        return None
    try:
        with open(PSL_FILE, "rb") as fh:
            raw = fh.read()
        with open(PSL_SHA256_FILE, encoding="ascii") as fh:
            want = (fh.read().split() or [""])[0].strip().lower()
    except OSError as exc:
        _PSL_STATUS = f"unreadable ({type(exc).__name__})"
        return None
    if not want or hashlib.sha256(raw).hexdigest() != want:
        _PSL_STATUS = "its SHA-256 does not match public_suffix_list.dat.sha256: not used"
        import logging
        logging.getLogger(__name__).warning("web_scope: %s %s", PSL_FILE, _PSL_STATUS)
        return None
    _PSL = parse_psl(raw.decode("utf-8", "replace"))
    _PSL_STATUS = "loaded"
    return _PSL


def psl_status() -> str:
    """``loaded``, ``absent``, or why the pinned Public Suffix List is not used."""
    public_suffix_list()
    return _PSL_STATUS


def _psl_suffix_len(parts: List[str], psl: Tuple[frozenset, frozenset, frozenset]) -> int:
    """How many trailing labels of ``parts`` are the public suffix by the list's algorithm: an exception rule wins,
    else the longest plain or wildcard rule, else the TLD alone (the implicit ``*`` rule)."""
    rules, wild, exc = psl
    best = 1
    for n in range(len(parts), 0, -1):
        cand = ".".join(parts[-n:])
        if cand in exc:
            return n - 1
        if cand in rules:
            best = max(best, n)
        if n < len(parts) and cand in wild:
            best = max(best, n + 1)
    return best


def suffix_of(host: str) -> Tuple[str, str]:
    """``(public suffix, how it is known)`` of ``host``. ``how``: ``psl`` (the pinned list), ``private`` (a
    shared-hosting suffix), ``listed`` (:data:`PUBLIC_SUFFIX2`), ``generic`` (:data:`GENERIC_SECOND_LEVELS` under a
    two-letter country code), ``tld`` (no rule matched: the top-level domain alone). ``("", "")`` for an IP address
    or a single label."""
    name = _name(host)
    parts = name.split(".")
    if len(parts) < 2 or re.fullmatch(r"[0-9.]+", name) or ":" in name:
        return "", ""
    psl = public_suffix_list()
    if psl is not None:
        n = min(_psl_suffix_len(parts, psl), len(parts))
        return ".".join(parts[-n:]), "psl"
    for n in (4, 3, 2):
        if len(parts) > n and ".".join(parts[-n:]) in PRIVATE_SUFFIXES:
            return ".".join(parts[-n:]), "private"
    two = ".".join(parts[-2:])
    if two in PUBLIC_SUFFIX2:
        return two, "listed"
    if re.fullmatch(r"[a-z]{2}", parts[-1]) and parts[-2] in GENERIC_SECOND_LEVELS:
        return two, "generic"
    return parts[-1], "tld"


def registrable(host: str) -> str:
    """The registrable domain (eTLD+1) of ``host``: one label under its public suffix (:func:`suffix_of`).

    A host on a shared-hosting suffix returns the full tenant name (one label
    under the suffix), so sibling tenants never share it. A host that is itself
    a public suffix returns itself."""
    name = _name(host)
    suffix, _how = suffix_of(name)
    if not suffix:
        return name
    parts = name.split(".")
    n = len(suffix.split("."))
    return ".".join(parts[-(n + 1):]) if len(parts) > n else name


def suffix_uncertain(host: str) -> bool:
    """Is ``host``'s registrable domain a guess? True when no list or rule knew its suffix and its TLD is a
    two-letter country code with public second levels this build does not enumerate (not one of
    ``site_modes.FLAT_TLDS``): there, two hosts that share a guessed registrable domain may belong to different
    registrants (``a.waw.pl``, ``b.waw.pl``), and only the site's own evidence binds them
    (:meth:`WebScope.is_own_host`)."""
    _suffix, how = suffix_of(host)
    if how != "tld":
        return False
    tld = _name(host).rsplit(".", 1)[-1]
    if not re.fullmatch(r"[a-z]{2}", tld):
        return False
    from anyuse.execution.site_modes import FLAT_TLDS
    return tld not in FLAT_TLDS


def _under(name: str, domain: str) -> bool:
    return name == domain or name.endswith("." + domain)


# ---------------------------------------------------------------------------
# the provider catalogue (declared; nothing outside it is ever a provider)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Read:
    """One read-only request shape beyond GET/HEAD: method + path regex."""
    method: str
    path: str


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    #: evidence in the site's OWN document/script. Each is a regex; a match of
    #: any of them (together with ``requires`` when set) detects the provider
    detect: Tuple[str, ...]
    #: fixed provider hosts -> allowed path prefixes ("" = any path)
    hosts: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    #: per-customer hosts derived from the evidence: regex whose group 1 is an
    #: id or a host, and host templates using ``{id}``
    derive: Tuple[str, ...] = ()
    templates: Tuple[str, ...] = ()
    #: a derived host must match this shape
    shape: str = ""
    #: path prefixes on derived hosts ("" = any)
    derived_paths: Tuple[str, ...] = ("",)
    #: documented read-only query requests beyond GET/HEAD
    reads: Tuple[Read, ...] = ()
    #: a word that must also appear in the source (for generic-looking ids)
    requires: str = ""
    #: query/body/header names carrying the page's PUBLIC search key (redacted
    #: in every record; never extracted)
    key_params: Tuple[str, ...] = ()


PROVIDERS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "google_cse", "Google Programmable Search (CSE)",
        detect=(r"(?:cse\.google\.com|www\.google\.com)/cse(?:\.js|/cse\.js)\?[^\"'<>\s]*\bcx=",
                r"cse\.google\.com/cse\?[^\"'<>\s]*\bcx=",
                r"class=[\"'][^\"']*\bgcse-(?:search|searchbox|searchresults)\b"
                r"[\s\S]{0,4000}\bcx\s*[=:]\s*[\"'][0-9a-z:_-]{10,}",
                r"\bcx\s*[=:]\s*[\"'][0-9a-z:_-]{10,}[\"'][\s\S]{0,4000}cse\.google\.com/cse\.js"),
        hosts=(("cse.google.com", ("/cse.js", "/cse/", "/cse?")),
               ("www.google.com", ("/cse/", "/cse.js", "/jsapi")),
               ("www.googleapis.com", ("/customsearch/",))),
        key_params=("cx", "cse_tok", "key")),
    # Boost Commerce (a store's search page drew no item while the results came from this provider's search API on
    # services.mybcapps.com, which the scope refused): Boost AI Search & Discovery, a site-search SaaS that serves the
    # site's own search (its widget loads from cdn.boostcommerce.io); GET search / filter reads only
    ProviderSpec(
        "boost_commerce", "Boost Commerce (Boost AI Search & Discovery)",
        detect=(r"services\.mybcapps\.com", r"cdn\.boostcommerce\.io/widget-integration",
                r"\bbc-sf-filter/(?:search|filter)\b"),
        # the search / instant-search / filter reads (the page's own code names them: bc-sf-filter/search, its SSR
        # twin bc-sf-ssr-node/search, bc-sf-filter/filter); never subscribe-b2s, bundles, products or recommend
        hosts=(("services.mybcapps.com", ("/bc-sf-filter/search", "/bc-sf-filter/filter",
                                          "/bc-sf-ssr-node/search")),)),
    ProviderSpec(
        "algolia", "Algolia",
        detect=(r"\b([A-Za-z0-9]{10})-dsn\.algolia\.net",
                r"https?://([A-Za-z0-9]{10})(?:-[1-3])?\.algolia(?:net)?\.(?:net|com)",
                r"(?:appId|applicationId|app_id|algoliaAppId|ALGOLIA_APP_ID|algolia_app_id)"
                r"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9]{10})[\"']"),
        requires="algolia",
        derive=(r"\b([A-Za-z0-9]{10})-dsn\.algolia\.net",
                r"https?://([A-Za-z0-9]{10})(?:-[1-3])?\.algolia(?:net)?\.(?:net|com)",
                r"(?:appId|applicationId|app_id|algoliaAppId|ALGOLIA_APP_ID|algolia_app_id)"
                r"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9]{10})[\"']"),
        templates=("{id}-dsn.algolia.net", "{id}.algolia.net", "{id}-1.algolianet.com",
                   "{id}-2.algolianet.com", "{id}-3.algolianet.com"),
        shape=r"^[a-z0-9]{10}(?:-dsn|-[1-3])?\.algolia(?:net)?\.(?:net|com)$",
        derived_paths=("/1/indexes/",),
        reads=(Read("POST", r"^/1/indexes/[^/]+/(?:queries|query)$"),),
        key_params=("x-algolia-api-key", "x-algolia-application-id", "x-algolia-agent")),
    ProviderSpec(
        "yext", "Yext Search",
        detect=(r"assets\.sitescdn\.net/(?:answers|search-ui)",
                r"(?:liveapi|liveapi-cached)\.yext\.com/v2/accounts/",
                r"(?:prod-cdn\.us|cdn)\.yextapis\.com",
                r"answers\.yext(?:apis)?\.com"),
        hosts=(("liveapi.yext.com", ("/v2/accounts/me/answers/", "/v2/accounts/me/search/")),
               ("liveapi-cached.yext.com", ("/v2/accounts/me/answers/",
                                            "/v2/accounts/me/search/")),
               ("cdn.yextapis.com", ("/v2/accounts/me/search/", "/v2/accounts/me/answers/")),
               ("prod-cdn.us.yextapis.com", ("/v2/accounts/me/search/",
                                             "/v2/accounts/me/answers/")),
               ("assets.sitescdn.net", ("/answers", "/search-ui", "/answers-search-bar"))),
        key_params=("api_key", "apiKey", "experienceKey")),
    ProviderSpec(
        "elastic_app_search", "Elastic App Search",
        detect=(r"https://([a-z0-9-]+\.ent(?:-search)?\.[a-z0-9-]+\.[a-z0-9.-]*"
                r"(?:cloud\.es\.io|elastic-cloud\.com))",),
        derive=(r"https://([a-z0-9-]+\.ent(?:-search)?\.[a-z0-9-]+\.[a-z0-9.-]*"
                r"(?:cloud\.es\.io|elastic-cloud\.com))",),
        templates=("{id}",),
        shape=r"^[a-z0-9-]+\.ent(?:-search)?\.[a-z0-9-]+\.[a-z0-9.-]*"
              r"(?:cloud\.es\.io|elastic-cloud\.com)$",
        derived_paths=("/api/as/v1/engines/",),
        reads=(Read("POST", r"^/api/as/v1/engines/[^/]+/(?:search|query_suggestion|"
                            r"multi_search)(?:\.json)?$"),),
        key_params=("authorization",)),
    ProviderSpec(
        "swiftype", "Elastic Site Search (Swiftype)",
        detect=(r"s\.swiftypecdn\.com/install/", r"search-api\.swiftype\.com",
                r"_st\(\s*[\"']install[\"']"),
        hosts=(("s.swiftypecdn.com", ("",)),
               ("search-api.swiftype.com", ("/api/v1/public/",)),
               ("api.swiftype.com", ("/api/v1/public/",))),
        reads=(Read("POST", r"^/api/v1/public/(?:engines|installs/[^/]+)/"
                            r"(?:search|suggest)\.json$"),),
        key_params=("engine_key",)),
    ProviderSpec(
        "typesense_cloud", "Typesense Cloud",
        detect=(r"\b([a-z0-9]+(?:-[1-3])?\.a1\.typesense\.net)",),
        derive=(r"\b([a-z0-9]+(?:-[1-3])?\.a1\.typesense\.net)",),
        templates=("{id}",),
        shape=r"^[a-z0-9]+(?:-[1-3])?\.a1\.typesense\.net$",
        derived_paths=("/collections/", "/multi_search"),
        reads=(Read("POST", r"^/multi_search$"),),
        key_params=("x-typesense-api-key",)),
    ProviderSpec(
        "coveo", "Coveo",
        detect=(r"static\.cloud\.coveo\.com/(?:searchui|headless|atomic)",
                r"platform(?:-eu|-au|-ca)?\.cloud\.coveo\.com/rest/search",
                r"\b([a-z0-9]{6,})\.org\.coveo\.com"),
        hosts=(("platform.cloud.coveo.com", ("/rest/search/",)),
               ("platform-eu.cloud.coveo.com", ("/rest/search/",)),
               ("platform-au.cloud.coveo.com", ("/rest/search/",)),
               ("platform-ca.cloud.coveo.com", ("/rest/search/",)),
               ("static.cloud.coveo.com", ("/searchui/", "/headless/", "/atomic/"))),
        derive=(r"\b([a-z0-9]{6,})\.org\.coveo\.com",),
        templates=("{id}.org.coveo.com",),
        shape=r"^[a-z0-9]{6,}\.org\.coveo\.com$",
        derived_paths=("/rest/search/",),
        reads=(Read("POST", r"^/rest/search/v2(?:/querySuggest|/facet)?/?$"),),
        key_params=("authorization", "access_token")),
    ProviderSpec(
        "bing_custom_search", "Bing Custom Search",
        detect=(r"ui\.customsearch\.ai/api/ux/", r"api\.bing\.microsoft\.com/v7\.0/custom/"),
        hosts=(("ui.customsearch.ai", ("/api/ux/", "/api/search", "/")),
               ("api.bing.microsoft.com", ("/v7.0/custom/",))),
        key_params=("customconfig", "subscription-key")),
    # ANYUSE_THIRD_PARTY=search: the e-commerce search providers sites embed, from their
    # public integration docs. A host or path the site's page uses beyond an entry is refused (fail closed) and the run
    # shows it (the typed stop provider_search); an entry is widened only by such evidence.
    ProviderSpec(
        "searchspring", "Searchspring",
        detect=(r"snapui\.searchspring\.io/([a-z0-9]{6})/", r"\b([a-z0-9]{6})\.a\.searchspring\.io",
                r"cdn\.searchspring\.net/search/v3/js/searchspring\.catalog\.js"),
        requires="searchspring",
        hosts=(("snapui.searchspring.io", ("",)), ("cdn.searchspring.net", ("/search/", "/ajax_search/"))),
        derive=(r"snapui\.searchspring\.io/([a-z0-9]{6})/", r"\b([a-z0-9]{6})\.a\.searchspring\.io",
                r"siteId[\"']?\s*[:=]\s*[\"']([a-z0-9]{6})[\"']"),
        templates=("{id}.a.searchspring.io",),
        shape=r"^[a-z0-9]{6}\.a\.searchspring\.io$",
        derived_paths=("/api/search/", "/api/meta/", "/api/suggest/")),
    ProviderSpec(
        "klevu", "Klevu",
        detect=(r"js\.klevu\.com/", r"\b((?:[a-z]{2})?cs\d+(?:v2)?)\.ksearchnet\.com"),
        requires="klevu",
        hosts=(("js.klevu.com", ("/core/", "/klevu-js-v1/", "/theme/", "/recs/")),),
        derive=(r"\b((?:[a-z]{2})?cs\d+(?:v2)?)\.ksearchnet\.com",),
        templates=("{id}.ksearchnet.com",),
        shape=r"^(?:[a-z]{2})?cs\d+(?:v2)?\.ksearchnet\.com$",
        derived_paths=("/cs/v2/search", "/cloud-search/n-search/"),
        reads=(Read("POST", r"^/cs/v2/search/?$"),),
        key_params=("ticket", "apikey", "klevu_apikey")),
    ProviderSpec(
        "doofinder", "Doofinder",
        detect=(r"cdn\.doofinder\.com/", r"\b(eu1|us1|ap1)-(?:search|layer)\.doofinder\.com"),
        requires="doofinder",
        hosts=(("cdn.doofinder.com", ("",)),),
        derive=(r"\b(eu1|us1|ap1)-(?:search|layer)\.doofinder\.com",
                r"zone[\"']?\s*[:=]\s*[\"'](eu1|us1|ap1)[\"']"),
        templates=("{id}-search.doofinder.com", "{id}-layer.doofinder.com"),
        shape=r"^(?:eu1|us1|ap1)-(?:search|layer)\.doofinder\.com$",
        derived_paths=("/5/", "/6/", "/search", "/layer/", "/api/"),
        key_params=("hashid",)),
    ProviderSpec(
        "findologic", "Findologic",
        detect=(r"cdn\.findologic\.com/", r"service\.findologic\.com/ps/"),
        requires="findologic",
        hosts=(("cdn.findologic.com", ("",)), ("service.findologic.com", ("/ps/",))),
        key_params=("shopkey",)),
)
BY_NAME = {p.name: p for p in PROVIDERS}


@dataclass(frozen=True)
class PlatformCDN:
    """A store platform's shared asset CDN: ``hosts`` (a full-match regex of its host names) and ``detect`` (the
    evidence in the site's OWN document that its page loads its own assets from there)."""
    name: str
    label: str
    hosts: str
    detect: str


#: ANYUSE_THIRD_PARTY=search: the platforms whose shops load their OWN theme, scripts,
#: styles, fonts and images from the platform's shared CDN (a store on a hosted platform had its whole theme refused).
#: A hint, read before the general rule (:func:`asset_hosts`: any host the site's own document loads its static assets
#: from, while that host answers with static assets only): an entry's host that the site's own source names is granted
#: for static assets, and the run record names the platform.
PLATFORM_CDNS: Tuple[PlatformCDN, ...] = (
    PlatformCDN("shopify", "Shopify",
                hosts=r"cdn\.shopify\.com|cdn\.shopifycdn\.net|fonts\.shopifycdn\.com|extensions\.shopifycdn\.com",
                detect=r"(?:cdn\.shopify\.com|(?:cdn|fonts|extensions)\.shopifycdn\.(?:com|net))/"),
    PlatformCDN("bigcommerce", "BigCommerce", hosts=r"cdn\d{0,3}\.bigcommerce\.com",
                detect=r"cdn\d{0,3}\.bigcommerce\.com/s-[a-z0-9]+/"),
    PlatformCDN("wix", "Wix", hosts=r"static\.wixstatic\.com|static\.parastorage\.com",
                detect=r"static\.(?:wixstatic|parastorage)\.com/"),
    PlatformCDN("squarespace", "Squarespace",
                hosts=r"static1\.squarespace\.com|assets\.squarespace\.com|images\.squarespace-cdn\.com",
                detect=r"(?:static1\.squarespace\.com|assets\.squarespace\.com|images\.squarespace-cdn\.com)/"),
)


_NAMED_HOST = re.compile(r"(?:https?:)?(?:\\?/){2}([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}"
                         r"[a-z0-9])?)+)(?::\d+)?(?=[/\\\"'?#\s)]|$)", re.I)


def named_hosts(source: str) -> List[str]:
    """The hosts one piece of the site's own source names in a URL (``https://h/``, ``//h/``, JSON-escaped
    ``https:\\/\\/h\\/``), lower-cased, in order of first mention."""
    seen: Dict[str, None] = {}
    for m in _NAMED_HOST.finditer((source or "")[:MAX_SCAN_CHARS]):
        seen.setdefault(m.group(1).lower().rstrip("."), None)
    return list(seen)


#: the HTML attributes through which a document loads a static asset: element -> attributes (``srcset`` lists URLs)
_ASSET_ATTRS = {"img": ("src", "srcset", "data-src", "data-srcset"), "source": ("src", "srcset", "data-srcset"),
                "script": ("src",), "video": ("src", "poster"), "audio": ("src",), "track": ("src",),
                "input": ("src",), "embed": ("src",), "image": ("href", "xlink:href"), "use": ("href", "xlink:href")}
#: ``<link rel=...>`` values that load (or prepare to load) a static asset
_ASSET_RELS = frozenset({"stylesheet", "preload", "modulepreload", "prefetch", "icon", "shortcut", "apple-touch-icon",
                         "apple-touch-icon-precomposed", "mask-icon", "preconnect", "dns-prefetch"})
_CSS_URL = re.compile(r"url\(\s*[\"']?\s*((?:https?:)?//[^\"')\s]+)", re.I)
#: a response that is data or a page, not a static asset, by its content type (JSON, XML, HTML, plain text, a form
#: encoding, an event stream)
DATA_MIME = re.compile(r"^(?:application/(?:[\w.+-]*json|[\w.+-]*xml|x-www-form-urlencoded|x-ndjson)|"
                       r"text/(?:html|plain|xml|event-stream|csv))", re.I)


class _AssetParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls: List[Tuple[str, str]] = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        for attr in _ASSET_ATTRS.get(tag, ()):
            raw = a.get(attr, "")
            for part in (raw.split(",") if attr.endswith("srcset") else [raw]):
                u = part.strip().split(" ")[0]
                if u:
                    self.urls.append((u, f"{tag} {attr}"))
        if tag == "link" and a.get("href") and set(a.get("rel", "").lower().split()) & _ASSET_RELS:
            self.urls.append((a["href"].strip(), f"link rel={a.get('rel', '').strip()[:30]}"))
        for m in _CSS_URL.finditer(a.get("style", "")):
            self.urls.append((m.group(1), f"{tag} style"))
        self.in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            for m in _CSS_URL.finditer(data or ""):
                self.urls.append((m.group(1), "style url()"))


def asset_hosts(document: str, page_url: str = "") -> Dict[str, str]:
    """The hosts the site's own document loads its static assets from: ``{host: where it names it}``, read from the
    HTML elements and attributes that load an asset (``img src`` / ``srcset``, ``script src``, ``link`` with a
    loading ``rel``, ``video poster``, CSS ``url()`` in ``<style>`` and ``style``). A host only linked to
    (``a href``) or mentioned in text is not one."""
    p = _AssetParser()
    try:
        p.feed((document or "")[:MAX_SCAN_CHARS])
    except Exception:          # noqa: BLE001 - a broken page yields what was parsed
        pass
    page = host_of(page_url) if page_url else ""
    out: Dict[str, str] = {}
    for u, where in p.urls:
        try:
            full = urllib.parse.urljoin(page_url, u) if page_url else u
            sp = urllib.parse.urlsplit(full if "//" in full else "")
        except ValueError:
            continue
        if sp.scheme not in ("http", "https") or not sp.hostname:
            continue
        h = host_of(full)
        if h and h != page and h not in out:
            out[h] = where
    return out


def detect_platform_cdns(source: str) -> List[Tuple[PlatformCDN, List[str]]]:
    """The platform CDNs one piece of the site's OWN source loads its assets from: ``(entry, [hosts it names])`` -
    only hosts the source itself names and the entry declares."""
    text = (source or "")[:MAX_SCAN_CHARS]
    out = []
    for cdn in PLATFORM_CDNS:
        if not re.search(cdn.detect, text, re.I):
            continue
        named = sorted({m.group(1).lower() for m in re.finditer(r"(?:https?:)?//([a-z0-9.-]+)/", text, re.I)
                        if re.fullmatch(cdn.hosts, m.group(1).lower())})
        if named:
            out.append((cdn, named))
    return out


@dataclass
class ProviderGrant:
    provider: str
    site: str
    #: host -> allowed path prefixes
    hosts: Dict[str, Tuple[str, ...]]
    evidence: str                  # what matched, keys redacted
    source: str                    # the site URL the evidence came from


def detect_providers(source: str) -> List[Tuple[ProviderSpec, Dict[str, Tuple[str, ...]], str]]:
    """Providers named by one piece of the site's OWN source (HTML or JS).

    Returns ``(spec, {host: path prefixes}, evidence)``. Nothing is guessed: a
    provider is returned only when one of its declared patterns matches, and a
    per-customer host only when the source names it (or its id) and it has the
    provider's host shape."""
    text = (source or "")[:MAX_SCAN_CHARS]
    if not text:
        return []
    low = text.lower()
    out = []
    for spec in PROVIDERS:
        if spec.requires and spec.requires not in low:
            continue
        hit = None
        for rx in spec.detect:
            hit = re.search(rx, text, re.I)
            if hit:
                break
        if not hit:
            continue
        hosts: Dict[str, Tuple[str, ...]] = {h: paths for h, paths in spec.hosts}
        for rx in spec.derive:
            for m in re.finditer(rx, text, re.I):
                ident = m.group(1).lower()
                for tmpl in spec.templates:
                    h = tmpl.format(id=ident).lower()
                    if spec.shape and not re.match(spec.shape, h):
                        continue
                    hosts[h] = tuple(spec.derived_paths)
        if not hosts:
            continue
        ev = redact_keys(hit.group(0))[:160]
        out.append((spec, hosts, ev))
    return out


# ---------------------------------------------------------------------------
# key redaction (records, logs, bench state)
# ---------------------------------------------------------------------------

_ALL_KEY_PARAMS = sorted({k.lower() for p in PROVIDERS for k in p.key_params
                          if k.lower() not in ("authorization",)} |
                         {"api_key", "apikey", "key", "access_token"}, key=len, reverse=True)
_KEY_RX = re.compile(r"(?i)((?:^|[?&;\s\"'{,])(?:" + "|".join(re.escape(k) for k in _ALL_KEY_PARAMS)
                     + r")[\"']?\s*[=:]\s*[\"']?)([^&\s\"',}]+)")
_BEARER_RX = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact_keys(text: str) -> str:
    """Replace the values of provider key parameters (and bearer tokens)."""
    if not text:
        return text
    out = _KEY_RX.sub(lambda m: m.group(1) + "<redacted>", str(text))
    return _BEARER_RX.sub(lambda m: m.group(1) + "<redacted>", out)


def redact_obj(obj: Any) -> Any:
    """``redact_keys`` over every string in a JSON-like structure."""
    if isinstance(obj, str):
        return redact_keys(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(x) for x in obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# POST search forms in the site's document
# ---------------------------------------------------------------------------

#: the last rung of "is this a search field / form / path": words an identifier or a label is or starts with
#: (:func:`names_word`), after the page's structure and the run's own typing
SEARCH_WORDS = ("search", "query", "keyword", "suche", "recherche", "buscar", "zoek", "cerca", "find", "lookup")
_SEARCH_KEYS = frozenset({"q", "s", "query", "search", "keyword", "keywords", "term", "terms",
                          "k", "text", "searchword", "search_query", "find", "searchtext",
                          "searchterm", "search_term", "qs", "kw", "wd", "query_string",
                          "searchstring", "search_string", "phrase", "suche", "stichwort"})


def name_tokens(text: str) -> List[str]:
    """The words of an identifier or a label, case-folded: runs of letters in any script, split where a
    lower-case letter meets an upper-case one (``searchBox`` -> ``search``, ``box``; ``site-suche`` -> ``site``,
    ``suche``; ``HTMLForm`` -> ``html``, ``form``)."""
    out: List[str] = []
    for run in re.findall(r"[^\W\d_]+", str(text or "")):
        cur = run[0]
        for i in range(1, len(run)):
            c, prev, nxt = run[i], run[i - 1], run[i + 1] if i + 1 < len(run) else ""
            if c.isupper() and (prev.islower() or (prev.isupper() and nxt.islower())):
                out.append(cur)
                cur = c
            else:
                cur += c
        out.append(cur)
    return [t.casefold() for t in out]


def names_word(text: str, vocabulary: Sequence[str] = SEARCH_WORDS) -> str:
    """The first word of ``vocabulary`` that a word of ``text`` (:func:`name_tokens`) is, or starts with (a
    compound identifier: ``searchbox``, ``zoekterm``), or ``""``. Never a word inside or at the end of another
    (``research``, ``besuchen``)."""
    for t in name_tokens(text):
        for w in vocabulary:
            if t == w or t.startswith(w):
                return w
    return ""


def field_carrying(pairs: Iterable[Tuple[str, str]], values: Iterable[str]) -> str:
    """The first body field whose WHOLE value is one of ``values`` (normalised as :func:`carried_value` does, a
    query string inside a body value included), or ``""``."""
    want = {n for n in (_norm(v) for v in values or ()) if len(n) >= MIN_OBSERVED_VALUE}
    if not want:
        return ""
    for k, v in pairs or ():
        cands = [(k, v)]
        if "=" in str(v) and not _NESTED_URL.match(str(v)):
            try:
                cands += urllib.parse.parse_qsl(str(v), keep_blank_values=True)
            except ValueError:
                pass
        for kk, vv in cands:
            if not vv or _NESTED_URL.match(str(vv)):
                continue
            n = _norm(vv)
            if n in want or _EDGE.sub("", n) in want:
                return str(kk)
    return ""


@dataclass
class SearchForm:
    host: str
    path: str
    fields: frozenset


class _FormParser(html.parser.HTMLParser):
    """The page's forms. A form is a search form by the page's structure first (HTML ``<search>``, ARIA
    ``role=search`` on the form, an ``input type=search`` or ``role=searchbox`` in it), else by a text input's
    field name (:data:`_SEARCH_KEYS`), else by the words of its labels (:func:`names_word`, the last rung)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: List[dict] = []
        self.cur: Optional[dict] = None
        self.in_search = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "search":
            self.in_search += 1
        if tag == "form":
            self.cur = {"method": a.get("method", "get").lower(), "action": a.get("action", ""),
                        "role": a.get("role", ""), "blob": " ".join(a.values()),
                        "fields": set(), "search": bool(self.in_search), "password": False}
            self.forms.append(self.cur)
            return
        if self.cur is None:
            return
        if tag in ("input", "select", "textarea", "button"):
            t = a.get("type", "text" if tag == "input" else tag).lower()
            name = a.get("name", "")
            if name:
                self.cur["fields"].add(name)
            if t == "password":
                self.cur["password"] = True
            if tag == "input" and t in ("text", "search", ""):
                blob = " ".join([name, a.get("id", ""), a.get("placeholder", ""),
                                 a.get("aria-label", ""), a.get("class", "")])
                if t == "search" or a.get("role", "").lower() == "searchbox" or \
                        name.lower() in _SEARCH_KEYS or names_word(blob):
                    self.cur["search"] = True

    def handle_endtag(self, tag):
        if tag == "search" and self.in_search:
            self.in_search -= 1
        if tag == "form":
            self.cur = None


def post_search_forms(html_text: str, page_url: str) -> List[SearchForm]:
    """POST forms of the page that are search forms (a search-like text input,
    no password field), with their action and field names."""
    p = _FormParser()
    try:
        p.feed((html_text or "")[:MAX_SCAN_CHARS])
    except Exception:          # noqa: BLE001 - a broken page yields what was parsed
        pass
    out = []
    for f in p.forms:
        if f["method"] != "post" or f["password"]:
            continue
        if not (f["search"] or f["role"].lower() == "search" or names_word(f["blob"])):
            continue
        action = urllib.parse.urljoin(page_url, f["action"] or page_url)
        sp = urllib.parse.urlsplit(action)
        if sp.scheme not in ("http", "https"):
            continue
        out.append(SearchForm(host=host_of(action), path=sp.path or "/",
                              fields=frozenset(f["fields"])))
    return out


# ---------------------------------------------------------------------------
# state-changing words (paths and field names)
# ---------------------------------------------------------------------------

STATE_WORDS = frozenset({
    "login", "logout", "logon", "signin", "signout", "signup", "register", "registration",
    "auth", "oauth", "password", "passwd", "cart", "basket", "checkout", "payment", "billing",
    "account", "accounts", "profile", "write", "edit", "update", "save", "create", "add",
    "upload", "delete", "remove", "destroy", "subscribe", "unsubscribe", "newsletter",
    "comment", "comments", "reply", "vote", "votes", "like", "likes", "favorite", "favourite",
    "follow", "wishlist", "donate", "contact", "feedback",
})
#: compound words that name a state change inside one token (``userlogin``)
STATE_COMPOUNDS = ("signin", "signup", "signout", "login", "logout", "addtocart", "checkout",
                   "subscribe", "newsletter", "password", "wishlist")
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def words(text: str) -> List[str]:
    """Lower-case words of a path or field name (camelCase and separators split)."""
    return [w.lower() for w in _WORD.findall(str(text or ""))]


def state_word(text: str) -> str:
    """The first state-changing word in ``text`` (a compound token that starts
    or ends with one, like ``userlogin``/``addtocart``, included), or ``""``."""
    for w in words(text):
        if w in STATE_WORDS:
            return w
        for c in STATE_COMPOUNDS:
            if w.startswith(c) or w.endswith(c):
                return c
    return ""


def body_pairs(body: str, content_type: str = "") -> Tuple[Optional[List[Tuple[str, str]]], str]:
    """``(key, value)`` pairs of a form or JSON body (nested keys included), or
    ``(None, why)`` when the body cannot be read as a small search body."""
    raw = (body or "").strip()
    ct = (content_type or "").lower()
    if "multipart" in ct:
        return None, "multipart body (an upload, not a search)"
    if len(raw.encode("utf-8", "ignore")) > MAX_POST_BODY:
        return None, f"body over {MAX_POST_BODY} bytes"
    if not raw:
        return None, "empty body"
    if raw[:1] in "{[":
        try:
            data = json.loads(raw)
        except ValueError:
            return None, "body is neither JSON nor a form encoding"
        out: List[Tuple[str, str]] = []

        def walk(node, depth=0):
            if depth > 6:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, (dict, list)):
                        out.append((str(k), ""))
                        walk(v, depth + 1)
                    else:
                        out.append((str(k), "" if v is None else str(v)))
            elif isinstance(node, list):
                for v in node:
                    walk(v, depth + 1)
        walk(data)
        return out, ""
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None, "body is neither JSON nor a form encoding"
    if not pairs:
        return None, "body is neither JSON nor a form encoding"
    return [(k, v) for k, v in pairs], ""


def graphql_refusal(body: str) -> str:
    """'' or why a GraphQL body is not a read: a document mentioning a mutation
    or subscription (``api_step.readonly_post_shape``'s rule), or a persisted
    operation whose ``operationName`` names a state change."""
    raw = (body or "").strip()
    if raw[:1] not in "{[":
        return ""
    from anyuse.execution.api_step import readonly_post_shape
    kind, _op, why = readonly_post_shape(raw)
    if kind == "graphql" and why:
        return why
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    for d in (data if isinstance(data, list) else [data]):
        if isinstance(d, dict) and isinstance(d.get("operationName"), str):
            name = d["operationName"]
            if re.search(r"(?i)mutation|subscription", name) or state_word(name):
                return f"GraphQL operation {name[:60]!r} names a state change"
    return ""


def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", urllib.parse.unquote_plus(str(v or ""))).strip().lower()


#: the resource types of a data call that may carry the typed value to the site's search provider (JSONP is a Script)
OBSERVED_DATA_TYPES = frozenset({"XHR", "Fetch", "Script", "EventSource", "Other"})
#: a typed value shorter than this never makes a provider (too many unrelated values match)
MIN_OBSERVED_VALUE = 2
_NESTED_URL = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//", re.I)
_EDGE = re.compile(r"^[\W_]+|[\W_]+$")


def _mentions(url: str, values: Iterable[str]) -> bool:
    """Does ``url`` (decoded, a page address encoded inside it included) name one of ``values`` as a whole word
    (``tv`` is not named by ``tvshow.js``)?"""
    low = urllib.parse.unquote_plus(urllib.parse.unquote_plus(str(url or ""))).casefold()
    for v in values or ():
        w = " ".join(str(v or "").split()).casefold()
        if w and re.search(r"(?<![^\W_])" + re.escape(w) + r"(?![^\W_])", low):
            return True
    return False


@dataclass
class ObservedProvider:
    """A third-party host the site's own page sent the typed value to in a data call: the site's search provider for
    the run. ``paths``: the path areas the value went to (a directory ``/v2/``, or the endpoint itself at the root);
    ``evidence``: the first such request, keys redacted; ``rtype``: its resource type."""
    site: str
    host: str
    paths: List[str]
    evidence: str
    rtype: str

    def covers(self, path: str) -> bool:
        p = path or "/"
        return any(p == a or (a.endswith("/") and a != "/" and p.startswith(a)) for a in self.paths)


def path_area(path: str) -> str:
    """The area a request path grants: its directory (``/v2/query`` -> ``/v2/``), or the path itself when that
    directory is the root (``/search`` -> ``/search``): the whole host is never an area."""
    p = path or "/"
    d = p[:p.rfind("/") + 1] or "/"
    return d if d != "/" else p


def carried_value(url: str, post: str = "", values: Iterable[str] = (), content_type: str = "") -> str:
    """The typed value (normalised) a request carries as a whole query parameter value, a form or JSON body value
    (:func:`body_pairs`, a query string inside a body value included) or a path segment (its file extension aside),
    or ``""``. A value that is itself a URL (``dl=https://...``: a page address a tracker reports) never counts, nor
    does a typed value shorter than :data:`MIN_OBSERVED_VALUE` characters."""
    want = {n for n in (_norm(v) for v in values or ()) if len(n) >= MIN_OBSERVED_VALUE}
    if not want:
        return ""
    try:
        sp = urllib.parse.urlsplit(url or "")
        cands = [v for _k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)]
    except ValueError:
        return ""
    segs = [urllib.parse.unquote(x) for x in (sp.path or "").split("/") if x]
    cands += segs + [os.path.splitext(x)[0] for x in segs if "." in x]
    if post:
        pairs, _why = body_pairs(post, content_type)
        for _k, v in pairs or ():
            cands.append(v)
            if "=" in v and not _NESTED_URL.match(v):
                cands += [x for _kk, x in urllib.parse.parse_qsl(v, keep_blank_values=True)]
    for c in cands:
        if not c or _NESTED_URL.match(c):
            continue
        n = _norm(c)
        for one in (n, _EDGE.sub("", n)):
            if one in want:
                return one
    return ""


# ---------------------------------------------------------------------------
# the blocklist snapshot
# ---------------------------------------------------------------------------

_BLOCKLIST: Optional[frozenset] = None


#: the page-code rule (known trackers and ads stay blocked): a small pinned SUPPLEMENT of
#: tracker / analytics / ad script hosts the domain snapshot misses, each with its reason (``web_scope_data/
#: tracker_supplement.json``, versioned, sha256-checked like the snapshot)
SUPPLEMENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_scope_data", "tracker_supplement.json")
SUPPLEMENT_SHA256 = "ea3da4520daed041a610f1f12db856d07c47eaab35f0287fa91e3b81c2adca84"


def tracker_supplement() -> Dict[str, str]:
    """``{domain: reason}`` of the pinned supplement (checked against its sha256)."""
    import hashlib
    with open(SUPPLEMENT_FILE, "rb") as fh:
        raw = fh.read()
    if hashlib.sha256(raw).hexdigest() != SUPPLEMENT_SHA256:
        raise RuntimeError("web_scope: the tracker supplement does not match its pinned sha256; refusing to use it")
    doc = json.loads(raw.decode("utf-8"))
    return {str(e["domain"]).strip().lower(): str(e.get("reason") or "") for e in doc.get("entries") or []}


def blocklist() -> frozenset:
    """The pinned EasyList/EasyPrivacy DOMAIN snapshot plus the pinned tracker supplement (loaded once, checked)."""
    global _BLOCKLIST
    if _BLOCKLIST is None:
        import hashlib
        with gzip.open(BLOCKLIST_FILE, "rb") as fh:
            raw = fh.read()
        if hashlib.sha256(raw).hexdigest() != BLOCKLIST_SHA256:
            raise RuntimeError("web_scope: the blocklist snapshot does not match its pinned "
                               "sha256; refusing to use an unverified filter list")
        _BLOCKLIST = frozenset(ln.strip() for ln in raw.decode("utf-8").splitlines()
                               if ln.strip() and not ln.startswith("#")) | frozenset(tracker_supplement())
    return _BLOCKLIST


def blocklisted(host: str, domains: Optional[Iterable[str]] = None) -> str:
    """The listed domain that covers ``host`` (itself or a parent), or ``""``."""
    names = domains if domains is not None else blocklist()
    parts = _name(host).split(".")
    for i in range(len(parts) - 1):
        d = ".".join(parts[i:])
        if d in names:
            return d
    return ""


def challenge_host(host: str, path: str = "/") -> bool:
    name = _name(host)
    if name in CHALLENGE_HOSTS:
        return True
    return any(name == h and (path or "/").startswith(p) for h, p in CHALLENGE_PATHS)


# ---------------------------------------------------------------------------
# the run's scope
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    kind: str = ""                 # one of KINDS, or "" (refused)
    why: str = ""                  # refusal reason ("" when allowed)
    detail: str = ""               # provider name / matched form / list domain

    @property
    def ok(self) -> bool:
        return bool(self.kind) and not self.why


class WebScope:
    """One run's widened scope. Construct with explicit flags (a benchmark) or
    :meth:`from_settings` (the four settings). All off = today's guard."""

    def __init__(self, *, providers: bool = False, readonly_post: bool = False,
                 own_hosts: bool = False, third_party: str = "block",
                 declared: Optional[Dict[str, Sequence[Any]]] = None,
                 blocklist_domains: Optional[Iterable[str]] = None, mode: str = "", platform_cdn: bool = False,
                 page_code: bool = False, observed_providers: bool = False, refuse_gov: Optional[bool] = None):
        if third_party not in THIRD_PARTY_POLICIES:
            raise ValueError(f"third_party must be one of {THIRD_PARTY_POLICIES}")
        from anyuse.execution.gov_hosts import rule_on
        #: ANYUSE_REFUSE_GOV_HOSTS for this run (read once here when not given)
        self.refuse_gov = rule_on() if refuse_gov is None else bool(refuse_gov)
        #: the host the site's own page sends the typed value to is its search provider (with ``providers``)
        self.observed_providers = bool(observed_providers)
        #: site -> host -> what the site's page sent the typed value to (:meth:`observed_call`)
        self.observed: Dict[str, Dict[str, ObservedProvider]] = {}
        #: the ANYUSE_THIRD_PARTY mode this scope was built from ("" when built from explicit flags)
        self.mode = str(mode or "")
        #: a store platform's own asset CDN is first party for static assets
        self.platform_cdn = bool(platform_cdn)
        #: the page's own scripts / stylesheets from other domains (``KIND_PAGE_CODE``)
        self.page_code = bool(page_code)
        #: site -> platform name -> the CDN hosts its own documents named
        self.platform_grants: Dict[str, Dict[str, set]] = {}
        #: site -> host -> where the site's own document loads a static asset from it (:func:`asset_hosts`)
        self.asset_grants: Dict[str, Dict[str, str]] = {}
        #: site -> host -> the content type of the first non-asset answer it gave (it then serves data, not assets)
        self.asset_data: Dict[str, Dict[str, str]] = {}
        self.providers = bool(providers)
        self.readonly_post = bool(readonly_post)
        self.own_hosts = bool(own_hosts)
        self.third_party = third_party
        self._blocklist = frozenset(blocklist_domains) if blocklist_domains is not None else None
        #: site -> {host: reason} declared by the user (exact hosts)
        self.declared: Dict[str, Dict[str, str]] = {}
        for site, rows in (declared or {}).items():
            self.declared[_name(site)] = self._declare(site, rows)
        #: site -> provider name -> grant
        self.grants: Dict[str, Dict[str, ProviderGrant]] = {}
        #: site -> POST search forms seen in the site's own documents
        self.forms: Dict[str, List[SearchForm]] = {}
        #: site -> search values the run may type (normalised)
        self.values: Dict[str, set] = {}
        #: host -> the site it was last allowed for (accounting)
        self.host_site: Dict[str, str] = {}
        #: (site, kind) -> requests sent; (site, kind, host) -> requests
        self.usage: Dict[str, Dict[str, int]] = {}
        self.usage_hosts: Dict[str, Dict[str, Dict[str, int]]] = {}
        #: every refusal a scope rule decided (for the report), capped
        self.refusals: List[Tuple[str, str, str]] = []
        #: the site's document/script sources scanned (count per site)
        self.scanned: Dict[str, int] = {}
        #: site -> host -> how the site's own evidence binds it (a URL in the site's own document or script, a
        #: redirect of the site's navigation): what makes a host under an uncertain suffix the site's own
        self.bound: Dict[str, Dict[str, str]] = {}

    # -- construction ----------------------------------------------------------

    @classmethod
    def from_settings(cls, declared_path: Optional[str] = None, mode: Optional[str] = None) -> Optional["WebScope"]:
        """The run's scope from the settings: the third-party mode (``ANYUSE_THIRD_PARTY``, :func:`third_party_flags`;
        ``mode`` overrides it) and the two first-party settings (``ANYUSE_ALLOW_READONLY_POST_SEARCH``,
        ``ANYUSE_ALLOW_SITE_OWN_HOSTS``). ``None`` when nothing is widened (mode ``off`` and both off: today's
        first-party guard)."""
        from anyuse.settings import get as get_setting
        flags = third_party_flags(mode)
        prov, tp = flags["providers"], flags["third_party"]
        post = bool(get_setting("ANYUSE_ALLOW_READONLY_POST_SEARCH"))
        own = bool(get_setting("ANYUSE_ALLOW_SITE_OWN_HOSTS"))
        # the page's scripts / stylesheets from other domains - ON by default with the
        # third-party modes search / any; ANYUSE_THIRD_PARTY=off (first party only) keeps them off
        code = bool(get_setting(PAGE_CODE_SETTING)) and flags["mode"] != "off"
        if not (prov or post or own or code or tp != "block"):
            return None
        declared = {}
        path = declared_path or os.environ.get("ANYUSE_SITE_HOSTS_FILE", "").strip()
        if not path:
            from anyuse.paths import anyuse_data_path
            path = str(anyuse_data_path("site_hosts.json"))
        if own and os.path.exists(path):
            declared = load_declared(path)
        from anyuse.execution.gov_hosts import rule_on
        return cls(providers=prov, readonly_post=post, own_hosts=own, third_party=tp,
                   declared=declared, mode=flags["mode"], platform_cdn=flags["platform_cdn"], page_code=code,
                   observed_providers=flags["observed_providers"], refuse_gov=rule_on())

    @classmethod
    def from_legacy_settings(cls, declared_path: Optional[str] = None) -> Optional["WebScope"]:
        """The four settings as they were read before ANYUSE_THIRD_PARTY (``ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS``,
        ``ANYUSE_THIRD_PARTY_POLICY`` and the two first-party ones) - ONLY for the historical benches
        whose protocol names them (``execution/web_runtime/live_bench.web_scope_for``); every product path uses :meth:`from_settings`.
        ``None`` when all are off."""
        from anyuse.settings import get as get_setting, resolve
        prov = bool(get_setting("ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS"))
        post = bool(get_setting("ANYUSE_ALLOW_READONLY_POST_SEARCH"))
        # the site's own hosts are the product's DEFAULT; the historical benches' protocols
        # name the setting off unless set, and keep that reading
        r = resolve("ANYUSE_ALLOW_SITE_OWN_HOSTS")
        own = bool(r.value) if r.source != "default" else False
        tp = str(get_setting("ANYUSE_THIRD_PARTY_POLICY") or "block")
        if not (prov or post or own or tp != "block"):
            return None
        declared = {}
        path = declared_path or os.environ.get("ANYUSE_SITE_HOSTS_FILE", "").strip()
        if not path:
            from anyuse.paths import anyuse_data_path
            path = str(anyuse_data_path("site_hosts.json"))
        if own and os.path.exists(path):
            declared = load_declared(path)
        return cls(providers=prov, readonly_post=post, own_hosts=own, third_party=tp, declared=declared)

    @staticmethod
    def _declare(site: str, rows: Sequence[Any]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for row in rows or ():
            if isinstance(row, dict):
                host, reason = str(row.get("host") or ""), str(row.get("reason") or "")
            else:
                raise ValueError(f"web scope: a declared host for {site} needs "
                                 f"{{'host': ..., 'reason': ...}}")
            h = host.strip().lower()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", h):
                raise ValueError(f"web scope: declared host {host!r} is not an exact host "
                                 f"name (no wildcard, scheme, port or path)")
            if len(reason.strip()) < 10:
                raise ValueError(f"web scope: declared host {h} for {site} needs a reason")
            if blocklisted(h) or challenge_host(h):
                raise ValueError(f"web scope: declared host {h} is an ad/tracker/challenge "
                                 f"host and cannot be a site's own host")
            out[h] = reason.strip()
        return out

    @property
    def enabled(self) -> bool:
        return self.providers or self.readonly_post or self.own_hosts or \
            self.third_party != "block" or self.platform_cdn or self.page_code

    @property
    def wants_sources(self) -> bool:
        """The guard must show this scope the site's own documents/scripts."""
        return self.providers or self.readonly_post or self.platform_cdn or self.own_hosts

    def knobs(self) -> Dict[str, Any]:
        from anyuse.execution.gov_hosts import SETTING as GOV_SETTING
        return {"ANYUSE_THIRD_PARTY": self.mode or None, "platform_cdn": self.platform_cdn,
                PAGE_CODE_SETTING: self.page_code, "observed_providers": self.observed_providers,
                GOV_SETTING: self.refuse_gov,
                "ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS": self.providers,
                "ANYUSE_ALLOW_READONLY_POST_SEARCH": self.readonly_post,
                "ANYUSE_ALLOW_SITE_OWN_HOSTS": self.own_hosts,
                "ANYUSE_THIRD_PARTY_POLICY": self.third_party,
                "declared_hosts": {s: sorted(h) for s, h in self.declared.items()},
                "blocklist_sha256": BLOCKLIST_SHA256, "tracker_supplement_sha256": SUPPLEMENT_SHA256}

    # -- what the site's own page shows ------------------------------------------

    def observe(self, site: str, url: str, rtype: str, body: str) -> List[str]:
        """Scan one of the site's OWN documents/scripts. The caller guarantees
        the URL is the site's (same host, a listed host or an own host).
        Returns the names of providers newly granted."""
        site = _name(site)
        if not body or not self.wants_sources:
            return []
        self.scanned[site] = self.scanned.get(site, 0) + 1
        new = []
        if self.own_hosts and rtype in ("Document", "Script") and suffix_uncertain(site):
            # the binding decides only under a suffix the build cannot tell (:meth:`is_own_host`)
            for h in named_hosts(body):
                self.bind(site, h, f"named by the site's own {rtype.lower()} {redact_keys(url)[:80]}")
        if self.providers:
            for spec, hosts, ev in detect_providers(body):
                g = self.grants.setdefault(site, {}).get(spec.name)
                if g is None:
                    self.grants[site][spec.name] = ProviderGrant(
                        provider=spec.name, site=site, hosts=dict(hosts), evidence=ev,
                        source=redact_keys(url)[:160])
                    new.append(spec.name)
                else:
                    for h, paths in hosts.items():
                        g.hosts.setdefault(h, paths)
        if self.platform_cdn and rtype == "Document":
            for cdn, hosts in detect_platform_cdns(body):
                self.platform_grants.setdefault(site, {}).setdefault(cdn.name, set()).update(hosts)
            have = self.asset_grants.setdefault(site, {})
            for h, where in asset_hosts(body, url).items():
                if len(have) < 200:
                    have.setdefault(h, where)
        if self.readonly_post and rtype == "Document":
            have = self.forms.setdefault(site, [])
            for f in post_search_forms(body, url):
                if f not in have:
                    have.append(f)
        return new

    def bind(self, site: str, host: str, how: str) -> None:
        """Record the site's own evidence that ``host`` belongs to it (a URL in its own document or script, a
        redirect of its navigation). Only hosts on the site's registrable domain are kept: a binding never makes
        another domain the site's own."""
        s, h = _name(site), _name(host)
        if not s or not h or h == s or registrable(h) != registrable(s):
            return
        b = self.bound.setdefault(s, {})
        if h not in b and len(b) < 500:
            b[h] = str(how or "")[:160]

    def note_values(self, site: str, values: Iterable[str]) -> None:
        """The search values a run will type on ``site`` (the POST rule's evidence)."""
        vs = self.values.setdefault(_name(site), set())
        vs.update(_norm(v) for v in values or () if _norm(v))

    def note_response(self, site: str, host: str, mime: str, status: int = 200) -> None:
        """A response a host gave the site's page: a 2xx answer that is data or a page (:data:`DATA_MIME`: JSON,
        XML, HTML, text) ends that host's asset grant for the run."""
        s, h = _name(site), _name(host)
        if h not in (self.asset_grants.get(s) or {}) or not 200 <= int(status or 0) < 300:
            return
        mt = str(mime or "").split(";", 1)[0].strip().lower()
        if DATA_MIME.match(mt):
            self.asset_data.setdefault(s, {}).setdefault(h, mt[:60])

    def asset_host(self, site: str, host: str) -> str:
        """Where the site's own document loads a static asset from ``host`` (:func:`asset_hosts`), while every
        answer the host gave was a static asset; ``""`` otherwise."""
        s, h = _name(site), _name(host)
        where = (self.asset_grants.get(s) or {}).get(h, "")
        if not where or h in (self.asset_data.get(s) or {}):
            return ""
        return where

    def grant_for(self, site: str, host: str) -> Optional[Tuple[ProviderGrant, Tuple[str, ...]]]:
        for g in (self.grants.get(_name(site)) or {}).values():
            if host in g.hosts:
                return g, g.hosts[host]
        return None

    def observed_for(self, site: str, host: str, path: Optional[str] = None) -> Optional[ObservedProvider]:
        """The observed search provider ``host`` of ``site`` (under ``path``'s area when given), or ``None``."""
        o = (self.observed.get(_name(site)) or {}).get(_name(host))
        if o is None or (path is not None and not o.covers(path)):
            return None
        return o

    # -- decisions -----------------------------------------------------------------

    def is_own_host(self, site: str, host: str) -> str:
        """'' or why ``host`` is the site's own (registrable domain / declared)."""
        s, h = _name(site), _name(host)
        if not self.own_hosts or not s or not h or s == h:
            return ""
        if h in self.declared.get(s, {}):
            return "declared: " + self.declared[s][h][:80]
        reg = registrable(s)
        if not reg or "." not in reg or registrable(h) != reg:
            return ""
        if not _under(h, reg):
            return ""
        first = h[:-len(reg)].rstrip(".").split(".")[0] if h != reg else ""
        if first in TRACKING_LABELS:
            return ""
        if blocklisted(h, self._blocklist):
            return ""
        if suffix_uncertain(s):
            # the shared registrable domain is a guess (an unlisted country suffix): the site's own documents,
            # scripts or redirects must name the host
            how = (self.bound.get(s) or {}).get(h)
            return f"same registrable domain {reg}, {how}" if how else ""
        return "same registrable domain " + reg

    def classify(self, site: str, url: str, method: str, rtype: str, post: str = "",
                 headers: Optional[Dict[str, Any]] = None, same_site: bool = False,
                 values: Iterable[str] = (), acting: bool = False) -> Verdict:
        """May the page of ``site`` send this request under the widened scope?

        ``same_site``: the host is already the site's by the run's origin
        policy (only the POST rule can widen such a request). ``values``: the
        run's input values, joined by the values typed on the site
        (:meth:`note_values`); ``acting``: the page is in the window of the
        run's typing (a preflight may then precede the provider's first call)."""
        s = _name(site)
        values = tuple(values or ()) + tuple(sorted(self.values.get(s, ())))
        host = host_of(url)
        sp = urllib.parse.urlsplit(url)
        path = sp.path or "/"
        m = (method or "GET").upper()
        if not same_site:
            # no allowance ever reaches a NEVER_CONTACT host (or a government host
            # with ANYUSE_REFUSE_GOV_HOSTS) - in every mode, before any request
            never = contact_refusal(host, self.refuse_gov)
            if never:
                return Verdict(why=f"third-party host {host} ({never}: never contacted)")
        hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        if same_site:
            if m in READ:
                return Verdict()                  # today's rule already allows it
            if m == "POST":
                return self._post(s, url, post, hdrs, values, acting=acting)
            return Verdict(why=f"method {m} is not allowed by the run's policy")
        # a different host: catalogue grant, platform CDN, own host, observed provider, page code, then the
        # third-party policy
        if self.providers:
            hit = self.grant_for(s, host)
            if hit is not None:
                v = self._provider(hit[0], hit[1], host, path, m, rtype, post, hdrs)
                if not v.ok and m != "POST":
                    # the catalogue is a hint: what the site's page sends the typed value to is its search
                    seen = self.observed_call(s, url, m, rtype, post, hdrs, values=values, acting=acting)
                    if seen is not None and seen.ok:
                        return seen
                if v.ok:
                    self.host_site[host] = s
                return v
        if self.platform_cdn:
            plat = next((n for n, hs in (self.platform_grants.get(s) or {}).items() if host in hs), "")
            if plat and m in READ and rtype in PLATFORM_TYPES:
                self.host_site[host] = s
                return Verdict(kind=KIND_PLATFORM, detail=plat)
            if plat and self.third_party == "block":
                if m not in READ:
                    return Verdict(why=f"third-party host {host}: the {plat} asset CDN serves static assets by GET only")
                return Verdict(why=f"third-party host {host}: the {plat} asset CDN serves the site's static assets "
                                   f"only (scripts, CSS, fonts, images), never a {rtype or 'request'} of another site")
            # (mode any: a non-static request to the platform's host is judged by the third-party rule below)
            where = self.asset_host(s, host)
            if where and m in READ and rtype in PLATFORM_TYPES and not challenge_host(host, path) and \
                    not blocklisted(host, self._blocklist) and not _mentions(url, values):
                # the host the site's own document loads its static assets from, while it answers with assets only
                # (any other request to it is judged by the rules below)
                self.host_site[host] = s
                return Verdict(kind=KIND_PLATFORM, detail=f"asset host ({where})")
        own = self.is_own_host(s, host)
        if own:
            if m not in READ:
                return Verdict(why=f"method {m} to the site's own host {host} (GET/HEAD only)")
            self.host_site[host] = s
            return Verdict(kind=KIND_OWN, detail=own)
        if m != "POST":
            # (a POST to an observed provider is judged by the guard's read-only search POST rule)
            seen = self.observed_call(s, url, m, rtype, post, hdrs, values=values, acting=acting)
            # (ANYUSE_THIRD_PARTY=any: what the provider rule refuses is still judged by the third-party policy
            # below, so a content API the page sends the value to loads like any other content host)
            if seen is not None and (seen.ok or self.third_party == "block"):
                return seen
        if self.page_code and m == "GET" and rtype in PAGE_CODE_TYPES:
            # the page's own code from another domain - never a listed ad / tracker host, a
            # challenge provider, or a request that carries the typed value (it never leaks to an unchosen third party)
            if challenge_host(host, path):
                return Verdict(why=f"third-party host {host} (a challenge provider; never page code)")
            listed = blocklisted(host, self._blocklist)
            if listed:
                return Verdict(why=f"third-party host {host} (on the ad/tracker blocklist: {listed})")
            if _mentions(url, values):
                return Verdict(why=f"third-party host {host}: a page-code request that carries the typed value")
            self.host_site[host] = s
            return Verdict(kind=KIND_PAGE_CODE, detail=rtype.lower())
        if self.third_party != "block":
            if challenge_host(host, path):
                return Verdict(why=f"third-party host {host} (a challenge provider; never "
                                   f"allowed by the third-party policy)")
            if self.third_party == "allow_with_blocklist":
                listed = blocklisted(host, self._blocklist)
                if listed:
                    return Verdict(why=f"third-party host {host} (on the ad/tracker "
                                       f"blocklist: {listed})")
            if m not in READ:
                return Verdict(why=f"method {m} to third-party host {host} (GET/HEAD only)")
            if rtype == "Document":
                # ANYUSE_THIRD_PARTY=any: the hosts the site's own page uses for its
                # CONTENT - never a page of another site (a navigation or a third-party frame)
                return Verdict(why=f"third-party host {host}: a document of another site is never loaded under the "
                                   f"third-party policy")
            self.host_site[host] = s
            return Verdict(kind=KIND_THIRD, detail=self.third_party)
        return Verdict(why=f"third-party host {host}")

    def _provider(self, g: ProviderGrant, prefixes: Tuple[str, ...], host: str, path: str,
                  m: str, rtype: str, post: str, hdrs: Dict[str, str]) -> Verdict:
        spec = BY_NAME[g.provider]
        if prefixes and not any(p == "" or path.startswith(p) for p in prefixes):
            return Verdict(why=f"third-party host {host}: path {path[:60]} is outside the "
                               f"{spec.label} search paths granted for {g.site}")
        if rtype == "Document":
            return Verdict(why=f"third-party host {host}: a provider host is never a page "
                               f"navigation")
        if m in READ:
            return Verdict(kind=KIND_PROVIDER, detail=g.provider)
        if m == "OPTIONS":
            want = hdrs.get("access-control-request-method", "").upper()
            if want in READ or any(r.method == want and re.match(r.path, path)
                                   for r in spec.reads):
                return Verdict(kind=KIND_PROVIDER, detail=g.provider + " (preflight)")
            return Verdict(why=f"method OPTIONS to {host}: not the preflight of a documented "
                               f"{spec.label} read")
        if any(r.method == m and re.match(r.path, path) for r in spec.reads):
            pairs, why = body_pairs(post, hdrs.get("content-type", ""))
            if pairs is None and post:
                return Verdict(why=f"method {m} to {host}: {why}")
            gql = graphql_refusal(post)
            if gql:
                return Verdict(why=f"method {m} to {host}: {gql}")
            for k, _v in pairs or ():
                w = state_word(k)
                if w:
                    return Verdict(why=f"method {m} to {host}: body field {k!r} suggests a "
                                       f"state change ({w})")
            return Verdict(kind=KIND_PROVIDER, detail=g.provider + f" ({m} read)")
        return Verdict(why=f"method {m} to {host} is not a documented {spec.label} read "
                           f"query")

    def observed_call(self, site: str, url: str, method: str, rtype: str, post: str = "",
                      headers: Optional[Dict[str, Any]] = None, values: Iterable[str] = (),
                      acting: bool = False) -> Optional[Verdict]:
        """The site's own evidence for its search provider (``ANYUSE_THIRD_PARTY`` search / any): a data call of the
        site's page (:data:`OBSERVED_DATA_TYPES`) that carries the typed value (:func:`carried_value`) makes its host
        the site's search provider for the run, under the path area the value went to (:func:`path_area`). After
        that, a GET/HEAD to that area, the CORS preflight of a GET/HEAD/POST, and (``acting``, through the guard's
        read-only search POST rule) a small search POST are the provider's. ``None``: the rule has nothing to say
        and the other rules decide. Never a challenge provider, a never-contacted host, a document, a host on the
        ad/tracker blocklist, a telemetry or state-changing path, or a method beyond GET/HEAD/POST."""
        if not (self.providers and self.observed_providers):
            return None
        s, host = _name(site), host_of(url)
        try:
            path = urllib.parse.urlsplit(url).path or "/"
        except ValueError:
            return None
        if challenge_host(host, path) or contact_refusal(host, self.refuse_gov):
            return None
        m = (method or "GET").upper()
        hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        known = self.observed_for(s, host)
        if m == "OPTIONS":
            want = hdrs.get("access-control-request-method", "").upper()
            if want in READ | {"POST"} and not blocklisted(host, self._blocklist) and \
                    (known is not None or acting or carried_value(url, values=values)):
                # the request it clears would be refused on this path: no preflight either
                bad = self._observed_path_refusal(path)
                if bad:
                    return Verdict(why=f"third-party host {host}: {bad}")
                self.host_site[host] = s
                return Verdict(kind=KIND_PROVIDER, detail=f"observed: {host} (preflight)")
            return None
        if rtype == "Document":
            return None
        carried = carried_value(url, post if m == "POST" else "", values, hdrs.get("content-type", ""))
        if not carried:
            if known is not None and m in READ and known.covers(path) and not self._observed_path_refusal(path):
                self.host_site[host] = s
                return Verdict(kind=KIND_PROVIDER, detail=f"observed: {host}")
            return None
        if rtype not in OBSERVED_DATA_TYPES:
            return None                    # an image or a font named after the value is an asset, not a search
        listed = blocklisted(host, self._blocklist)
        if listed:
            return Verdict(why=f"third-party host {host} (on the ad/tracker blocklist: {listed})")
        bad = self._observed_path_refusal(path)
        if bad:
            return Verdict(why=f"third-party host {host}: {bad}")
        if m == "POST":
            if not acting:
                return Verdict(why=f"method POST to {host}: not sent right after typing into the site's search box")
            pairs, why = body_pairs(post, hdrs.get("content-type", ""))
            if pairs is None:
                return Verdict(why=f"method POST to {host}: {why}")
            gql = graphql_refusal(post)
            if gql:
                return Verdict(why=f"method POST to {host}: {gql}")
            for k, _v in pairs:
                w = state_word(k)
                if w:
                    return Verdict(why=f"method POST to {host}: body field {k[:40]!r} suggests a state change ({w})")
        elif m not in READ:
            return Verdict(why=f"method {m} to third-party host {host} (GET/HEAD only)")
        o = self.observed.setdefault(s, {}).get(_name(host))
        area = path_area(path)
        if o is None:
            self.observed[s][_name(host)] = ObservedProvider(site=s, host=_name(host), paths=[area],
                                                              evidence=redact_keys(url)[:160], rtype=rtype)
        elif area not in o.paths and len(o.paths) < 20:
            o.paths.append(area)
        self.host_site[host] = s
        return Verdict(kind=KIND_PROVIDER, detail=f"observed: {host}")

    @staticmethod
    def _observed_path_refusal(path: str) -> str:
        """Why a path is never the observed provider's (telemetry, infrastructure, a state change), or ``""``."""
        from anyuse.execution.cdp_guard import NOT_SEARCH_POST_PATH, TELEMETRY_PATH
        if TELEMETRY_PATH.search(path or "/") or NOT_SEARCH_POST_PATH.search(path or "/"):
            return "its path names telemetry or infrastructure, not a search"
        w = state_word(path)
        return f"its path suggests a state change ({w})" if w else ""

    def _post(self, site: str, url: str, post: str, hdrs: Dict[str, str],
              values: Iterable[str], acting: bool = False) -> Verdict:
        """The page's own read-only search POST, after every refusal (state words, telemetry, credential headers,
        GraphQL mutations, size): a POST search form in the site's own document whose fields cover the body; else,
        sent right after the run typed into the site's search box (``acting``), a body field whose whole value is
        that typed value (:func:`field_carrying`; whether its answer is a listing that changes with the input is
        the proof's question); else, the last rung, a search value in a field or under a path named like a
        search (:data:`_SEARCH_KEYS`, :func:`names_word`)."""
        if not self.readonly_post:
            return Verdict(why="method POST is not allowed by the run's policy")
        sp = urllib.parse.urlsplit(url)
        host, path = host_of(url), sp.path or "/"
        for seg in [p for p in path.split("/") if p] + [k for k, _ in
                                                        urllib.parse.parse_qsl(sp.query)]:
            w = state_word(seg)
            if w:
                return Verdict(why=f"method POST refused: the path suggests a state change "
                                   f"({w})")
        from anyuse.execution.cdp_guard import TELEMETRY_PATH
        if TELEMETRY_PATH.search(path):
            # an analytics/telemetry endpoint that echoes the
            # typed query (a "search" event) is not the page's search
            return Verdict(why="method POST refused: the path names telemetry, not a search")
        if any(h in hdrs for h in ("authorization", "x-api-key", "x-auth-token")):
            return Verdict(why="method POST refused: the request carries a credential header")
        pairs, why = body_pairs(post, hdrs.get("content-type", ""))
        if pairs is None:
            return Verdict(why=f"method POST refused: {why}")
        gql = graphql_refusal(post)
        if gql:
            return Verdict(why=f"method POST refused: {gql}")
        for k, _v in pairs:
            w = state_word(k)
            if w:
                return Verdict(why=f"method POST refused: body field {k!r} suggests a state "
                                   f"change ({w})")
        keys = {k for k, _ in pairs}
        for f in self.forms.get(site, []):
            if f.host == host and f.path == path and keys <= set(f.fields):
                return Verdict(kind=KIND_POST, detail=f"search form {path[:60]}")
        typed = self.values.get(site, set())
        carrier = field_carrying(pairs, typed) if acting and typed else ""
        if carrier:
            return Verdict(kind=KIND_POST, detail=f"the typed value in field {carrier[:40]!r} right after typing")
        want = set(typed) | {_norm(v) for v in values or () if _norm(v)}
        searchy_path = bool(names_word(path))
        for k, v in pairs:
            if _norm(v) and _norm(v) in want and (k.lower() in _SEARCH_KEYS or
                                                  names_word(k) or searchy_path):
                return Verdict(kind=KIND_POST, detail=f"search field {k[:40]!r}")
        return Verdict(why="method POST refused: not the page's own search (no POST search "
                           "form with these fields, and no search field carries the search "
                           "value)")

    # -- accounting --------------------------------------------------------------------

    def site_for(self, host: str) -> Optional[str]:
        return self.host_site.get(_name(host)) or self.host_site.get(host)

    def record(self, site: str, kind: str, host: str) -> None:
        s = _name(site)
        u = self.usage.setdefault(s, {})
        u[kind] = u.get(kind, 0) + 1
        hh = self.usage_hosts.setdefault(s, {}).setdefault(kind, {})
        hh[host] = hh.get(host, 0) + 1

    def refused(self, site: str, host: str, why: str) -> None:
        if len(self.refusals) < 2000:
            self.refusals.append((_name(site), host, why[:160]))

    def site_report(self, site: str) -> Dict[str, Any]:
        s = _name(site)
        return {"mode": self.mode or None, "usage": dict(self.usage.get(s, {})),
                "hosts": {k: dict(v) for k, v in self.usage_hosts.get(s, {}).items()},
                "grants": {n: {"hosts": sorted(g.hosts), "evidence": g.evidence,
                               "source": g.source}
                           for n, g in (self.grants.get(s) or {}).items()},
                "platform_cdn": {n: sorted(h) for n, h in (self.platform_grants.get(s) or {}).items()},
                "asset_hosts": {h: w for h, w in (self.asset_grants.get(s) or {}).items()
                                if h not in (self.asset_data.get(s) or {})},
                "post_forms": [f"{f.host}{f.path}" for f in self.forms.get(s, [])],
                "observed": {h: {"paths": list(o.paths), "evidence": o.evidence, "type": o.rtype}
                             for h, o in (self.observed.get(s) or {}).items()},
                "scanned": self.scanned.get(s, 0),
                "refusals": [list(r[1:]) for r in self.refusals if r[0] == s][:40]}


def load_declared(path: str) -> Dict[str, List[dict]]:
    """``{"sites": {"www.example.org": [{"host": "...", "reason": "..."}]}}``."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    sites = data.get("sites") if isinstance(data, dict) else None
    if not isinstance(sites, dict):
        raise ValueError(f"{path}: expected {{'sites': {{site: [{{host, reason}}]}}}}")
    return {str(k): list(v or []) for k, v in sites.items()}
