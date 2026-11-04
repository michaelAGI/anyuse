"""The government-host check.

With ``ANYUSE_REFUSE_GOV_HOSTS`` on (off by default), a government, ``.int``,
EU-institution or military host is never contacted on any path: not by a run,
a compile, a proof, a retry, a robots.txt fetch or a bench, and the guarded
browser's web-scope allowances never let a page pull one in as a provider, own
host or third party. Every request-time check goes through
:func:`government_refusal`, which applies the setting. The never-contact list
(``site_modes._never_contact``, ``web_scope.contact_refusal``) is refused in
every mode either way.

:func:`listed_government_host` matches the declared lists below (suffix rules
and explicit non-suffix public-body domains). :func:`government_host` adds the
fail-closed heuristic :func:`looks_governmental`: a host that only looks
governmental counts too and is reported; it is not added to the list on a
guess. :func:`government_host` is the list itself, whatever the setting: the
benches choose their sites with it.

The bench-side entry point is ``execution/web_runtime/never_contact.py`` (``is_government``,
``refusal``), which re-exports this check next to ``never_contact()``.
"""
from __future__ import annotations

import re
from typing import Optional

#: top-level domains that are governmental / intergovernmental
GOV_TLDS = ("gov", "mil", "int")
#: second-level labels that mark government under a two-letter country TLD:
#: gov.<cc>, gv.<cc>, go.<cc>, gob.<cc>, gouv.<cc>, govt.<cc>
CC_GOV_LABELS = ("gov", "gv", "go", "gob", "gouv", "govt")
#: exact registrable government domains (and everything under them)
GOV_DOMAINS = (
    "govt.nz", "gc.ca", "canada.ca", "gov.uk", "gouv.fr", "admin.ch", "gv.at",
    "europa.eu", "bund.de", "gov.scot", "gov.wales",
    # German Land (state) portal domains, listed explicitly
    "baden-wuerttemberg.de", "bayern.de", "berlin.de", "brandenburg.de", "bremen.de",
    "hamburg.de", "hessen.de", "mecklenburg-vorpommern.de", "niedersachsen.de", "nrw.de",
    "rlp.de", "saarland.de", "sachsen.de", "sachsen-anhalt.de", "schleswig-holstein.de",
    "thueringen.de",
)
#: declared NON-SUFFIX government and public-body domains (the host itself or
#: any subdomain): ministries and heads of state/government, parliaments,
#: central banks, national statistics offices, national libraries, archives
#: and museums run as government bodies, public health services, national,
#: state, provincial, canton and city portals, intergovernmental bodies.
#: Listed explicitly; anything else that only LOOKS governmental is blocked fail-closed by
#: the heuristic below and reported.
PUBLIC_BODY_DOMAINS = (
    # Germany (federal)
    "bundesregierung.de", "bundeskanzler.de", "bundespraesident.de", "bundestag.de",
    "bundesrat.de", "bundesverfassungsgericht.de", "bundesfinanzministerium.de",
    "auswaertiges-amt.de", "bundesbank.de", "destatis.de", "statistikportal.de",
    "arbeitsagentur.de", "bundesarchiv.de", "dnb.de", "deutsche-digitale-bibliothek.de",
    # German cities (official portals)
    "muenchen.de", "stadt-koeln.de", "frankfurt.de", "duesseldorf.de", "dresden.de",
    "leipzig.de", "nuernberg.de", "stuttgart.de", "hannover.de", "bonn.de",
    # Austria
    "oenb.at", "statistik.at", "onb.ac.at", "parlament.at",
    # Switzerland: federal parliament, central bank, canton portals, cities
    "parlament.ch", "snb.ch", "stadt-zuerich.ch", "ag.ch", "ai.ch", "ar.ch", "be.ch", "bl.ch",
    "bs.ch", "fr.ch", "ge.ch", "gl.ch", "gr.ch", "ju.ch", "lu.ch", "ne.ch", "nw.ch", "ow.ch",
    "sg.ch", "sh.ch", "so.ch", "sz.ch", "tg.ch", "ti.ch", "ur.ch", "vd.ch", "vs.ch", "zg.ch",
    "zh.ch",
    # France
    "elysee.fr", "assemblee-nationale.fr", "senat.fr", "conseil-constitutionnel.fr",
    "banque-france.fr", "insee.fr", "bnf.fr", "service-public.fr", "paris.fr",
    # United Kingdom and Ireland
    "nhs.uk", "parliament.uk", "bankofengland.co.uk", "bl.uk", "nls.uk", "iwm.org.uk",
    "library.wales", "rbge.org.uk", "nhm.ac.uk", "sciencemuseumgroup.org.uk", "vam.ac.uk",
    "britishmuseum.org", "nationalgallery.org.uk", "npg.org.uk", "tate.org.uk", "rmg.co.uk",
    "nms.ac.uk", "kew.org", "historicengland.org.uk", "historicenvironment.scot",
    "oireachtas.ie", "centralbank.ie", "cso.ie", "nli.ie",
    # Benelux
    "kb.nl", "rijksoverheid.nl", "overheid.nl", "tweedekamer.nl", "eerstekamer.nl", "dnb.nl",
    "cbs.nl", "nationaalarchief.nl", "amsterdam.nl", "rotterdam.nl", "denhaag.nl",
    "fgov.be", "belgium.be", "nbb.be", "kbr.be", "lachambre.be", "dekamer.be", "senate.be",
    # Nordics
    "regjeringen.no", "stortinget.no", "norges-bank.no", "ssb.no", "nb.no", "kommune.no",
    "riksdagen.se", "regeringen.se", "riksbank.se", "scb.se", "kb.se", "stockholm.se",
    "dst.dk", "ft.dk", "nationalbanken.dk", "kb.dk", "regeringen.dk", "kk.dk",
    "stat.fi", "eduskunta.fi", "suomenpankki.fi", "kansalliskirjasto.fi", "valtioneuvosto.fi",
    "hel.fi",
    # Southern and Central Europe
    "istat.it", "bancaditalia.it", "camera.it", "senato.it", "governo.it", "quirinale.it",
    "normattiva.it", "bde.es", "ine.es", "congreso.es", "senado.es", "bne.es", "boe.es",
    "madrid.es", "bportugal.pt", "parlamento.pt", "ine.pt", "nbp.pl", "bn.org.pl",
    "cnb.cz", "czso.cz", "psp.cz", "nkp.cz", "vlada.cz",
    # Americas
    "si.edu", "usps.com", "ourcommons.ca", "parl.ca", "sencanada.ca", "bankofcanada.ca",
    "ontario.ca", "quebec.ca", "alberta.ca", "novascotia.ca", "manitoba.ca",
    "saskatchewan.ca", "gnb.ca", "yukon.ca", "toronto.ca", "montreal.ca", "vancouver.ca",
    "ottawa.ca", "calgary.ca",
    # Asia-Pacific
    "parliament.nz", "boj.or.jp", "bok.or.kr",
    # Intergovernmental
    "un.org", "worldbank.org", "imf.org", "oecd.org", "wto.org", "unesco.org", "unicef.org",
    "bis.org", "iaea.org", "ilo.org", "fao.org", "eib.org",
)
#: US state government domains (state.<st>.us)
_US_STATE = re.compile(r"(^|\.)state\.[a-z]{2}\.us$")
#: words that suggest a public authority; used ONLY to report hosts the list
#: does not cover (fail closed: they are not contacted until a person decides)
_SUSPECT = re.compile(
    r"(^|[.-])(gov|govt|gouv|gob|government|gobierno|regierung|ministry|ministerium|minister|"
    r"parliament|parlament|bundestag|bundesrat|senat|senate|assemblee|congress|ourcommons|"
    r"europarl|census|statistik|stat|istat|ssb|cso|ons|destatis|insee|ine|cbs|scb|dst|"
    r"abs|bls|bea|federalreserve|courts?|judiciary|police|army|navy|defence|defense|"
    r"municipality|council|gemeinde|kommune|stadt|comune|ayuntamiento|prefecture)([.-]|$)")


def _name(host: str) -> str:
    return (host or "").split(":", 1)[0].strip().lower().rstrip(".")


def listed_government_host(host: str) -> str:
    """Why ``host`` is a government/public-body host by the DECLARED list, or ``""``."""
    name = _name(host)
    if not name or "." not in name:
        return ""
    labels = name.split(".")
    if labels[-1] in GOV_TLDS:
        return f".{labels[-1]} domain"
    if len(labels) >= 2 and len(labels[-1]) == 2 and labels[-2] in CC_GOV_LABELS:
        return f".{labels[-2]}.{labels[-1]} domain"
    # a provincial/state government zone: gov.bc.ca, gouv.qc.ca, gov.nl.ca
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-3] in ("gov", "gouv", "govt"):
        return f".{'.'.join(labels[-3:])} domain"
    for d in GOV_DOMAINS:
        if name == d or name.endswith("." + d):
            return f"{d} (government domain)"
    for d in PUBLIC_BODY_DOMAINS:
        if name == d or name.endswith("." + d):
            return f"{d} (government / public body)"
    if _US_STATE.search(name) or re.search(r"(^|\.)comune\.[a-z-]+\.it$", name):
        return "state/municipal domain"
    return ""


def government_host(host: str, fail_closed: bool = True) -> str:
    """Why ``host`` may not be contacted under the government rule, or
    ``""``: the declared list, and (``fail_closed``, the default) any host that
    LOOKS governmental by :func:`looks_governmental` (blocked and reported)."""
    why = listed_government_host(host)
    if why or not fail_closed:
        return why
    word = looks_governmental(host)
    return f"looks governmental ({word}): blocked fail-closed, reported" if word else ""


#: refuse government, ``.int``, EU-institution and military hosts on every path (off by default)
SETTING = "ANYUSE_REFUSE_GOV_HOSTS"


def rule_on() -> bool:
    """Is ``ANYUSE_REFUSE_GOV_HOSTS`` on? A settings read that fails counts as off (the default)."""
    try:
        from anyuse.settings import get as get_setting
        return bool(get_setting(SETTING))
    except Exception:  # noqa: BLE001 - no settings module or an unreadable file: the default
        return False


def government_refusal(host: str, on: Optional[bool] = None) -> str:
    """Why a request to ``host`` is refused under the government rule, or ``""``: :func:`government_host` when the
    rule is on (``on``; ``None`` reads ``ANYUSE_REFUSE_GOV_HOSTS``), else ``""``."""
    if not (rule_on() if on is None else on):
        return ""
    return government_host(host)


def looks_governmental(host: str) -> str:
    """The word that makes a host look governmental although the declared list
    does not match it, or ``""`` (blocked fail-closed by :func:`government_host`)."""
    if listed_government_host(host):
        return ""
    m = _SUSPECT.search(_name(host))
    return m.group(2) if m else ""
