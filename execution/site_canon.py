"""ONE notion of "the same site across a redirect" and ONE classifier of a failed navigation.

Sites listed as ``www.X`` that live at ``X`` (and the reverse) failed before anything
was read - the robots.txt fetch refused the redirect hop to the other origin (robots unknown -> fail closed: 2 sites),
and the start document's redirect was refused by the guard's origin policy, so the agent saw ``chrome-error`` and
abstained with "No site URL provided" (2 sites). One rule, used by the robots fetch, the robots gate, the guard's
origin policy and the browser tool set:

* **same-site redirect** (:func:`is_same_site_redirect`): the ``www.`` sibling (``www.X`` <-> ``X``) of the same host,
  or the host itself, over the same scheme or http -> https, on the default port. It is followed and RECORDED.
  Anything else (another registrable domain, a subdomain other than www, https -> http, another port) stays refused.
* **a failed navigation** (:func:`classify_failure`) is typed from what the session saw: the guard's own refusals
  (``cross_site_redirect``, ``robots``, ``post_only``, ``budget``, ``refused``) and Chrome's network error of the
  document (``dns``, ``tls``, ``unreachable``, ``redirect_loop``, ``http_error``); ``unknown`` only when neither says.

Pure: no network, no page access beyond reading the attributes a live session keeps (``load_errors``, the guard's
``failed`` list, the budget's ``refused_methods`` / ``stopped``).
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

#: Chrome network error -> class (the document's ``Network.loadingFailed`` errorText)
NET_ERRORS = (
    (("ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED", "ERR_DNS"), "dns"),
    (("ERR_CERT", "ERR_SSL", "ERR_BAD_SSL", "ERR_TLS"), "tls"),
    (("ERR_TOO_MANY_REDIRECTS",), "redirect_loop"),
    (("ERR_CONNECTION", "ERR_TIMED_OUT", "ERR_ADDRESS_UNREACHABLE", "ERR_INTERNET_DISCONNECTED",
      "ERR_NETWORK_CHANGED", "ERR_EMPTY_RESPONSE", "ERR_SOCKET"), "unreachable"),
    (("ERR_HTTP_RESPONSE_CODE_FAILURE", "ERR_INVALID_RESPONSE", "ERR_INVALID_HTTP_RESPONSE"), "http_error"),
)
#: the typed classes of a failed start (``harness_types.Stop.START_FAILED``'s reason prefix)
CLASSES = ("dns", "tls", "unreachable", "redirect_loop", "http_error", "cross_site_redirect", "robots",
           "post_only", "budget", "refused", "empty_page", "unknown")


def _host(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u if "://" in str(u) else "https://" + str(u))
        return (p.hostname or "").lower()
    except ValueError:
        return ""


def sibling_of(host: str) -> str:
    """The ``www.`` sibling of a host (``www.X`` -> ``X``, ``X`` -> ``www.X``); "" for an IP or a bare name."""
    h = (host or "").lower().strip(".")
    if not h or "." not in h or h.replace(".", "").isdigit():
        return ""
    if h.startswith("www."):
        rest = h[4:]
        return rest if "." in rest else ""
    return "www." + h


def sibling_hosts(host: str) -> List[str]:
    """``[host, its www sibling]`` (the sibling omitted when there is none)."""
    h = (host or "").lower()
    s = sibling_of(h)
    return [h] + ([s] if s else [])


def is_same_site_redirect(from_url: str, to_url: str) -> bool:
    """A redirect hop the product follows: the same host or its www sibling, the same scheme or http -> https, the
    default port (none named, or 80/443 matching the scheme)."""
    try:
        a, b = urllib.parse.urlsplit(from_url), urllib.parse.urlsplit(urllib.parse.urljoin(from_url, to_url))
    except ValueError:
        return False
    if b.scheme not in ("http", "https") or not b.hostname:
        return False
    if not (a.scheme == b.scheme or (a.scheme == "http" and b.scheme == "https")):
        return False
    default = {"http": 80, "https": 443}
    for p in (a, b):
        try:
            port = p.port
        except ValueError:
            return False
        if port not in (None, default.get(p.scheme)):
            return False
    return (b.hostname or "").lower() in sibling_hosts((a.hostname or "").lower())


def sibling_origins(url: str) -> List[str]:
    """The origins a same-site redirect of ``url`` may land on (https for both hosts; the url's own scheme too)."""
    p = urllib.parse.urlsplit(url if "://" in str(url) else "https://" + str(url))
    out: List[str] = []
    for h in sibling_hosts((p.hostname or "").lower()):
        for scheme in dict.fromkeys([p.scheme or "https", "https"]):
            o = f"{scheme}://{h}"
            if o not in out:
                out.append(o)
    return out


def _net_class(text: str) -> str:
    t = str(text or "").upper()
    for keys, cls in NET_ERRORS:
        if any(k in t for k in keys):
            return cls
    return ""


def classify_failure(page: Any, *, since: Optional[Dict[str, int]] = None, url: str = "") -> Tuple[str, str]:
    """``(class, detail)`` of a navigation that ended on an error page, from what the session recorded AFTER the
    marks in ``since`` (:func:`marks`): the guard's refusals first (they are why Chrome failed the request), then the
    document's network error. ``("unknown", ...)`` when the session recorded nothing (an offline page)."""
    since = since or {}
    guard = getattr(page, "guard", None)
    failed = list(getattr(guard, "failed", None) or [])[int(since.get("failed", 0)):]
    budget = getattr(page, "budget", None)
    methods = list(getattr(budget, "refused_methods", None) or [])[int(since.get("methods", 0)):]
    errors = list(getattr(page, "load_errors", None) or [])[int(since.get("errors", 0)):]
    if any(str(m[0]).upper() == "POST" for m in methods if m):
        m = next(m for m in methods if m and str(m[0]).upper() == "POST")
        return "post_only", f"the page sent a POST ({str(m[1])[:120]}), which this read-only browser never sends"
    for u, why in failed:
        w = str(why or "")
        if w.startswith("third-party host"):
            return "cross_site_redirect", f"a request left the site for {w[len('third-party host '):][:80]} ({str(u)[:100]})"
        if w.startswith("robots"):
            return "robots", f"robots.txt refuses {str(u)[:120]}"
        if w.startswith(("host cap", "page-load cap", "static share")):
            return "budget", f"{w[:120]} ({str(u)[:100]})"
    docs = [e for e in errors if str(e.get("type") or "Document") == "Document"]
    for e in docs or errors:
        cls = _net_class(e.get("errorText") or "")
        if cls:
            return cls, f"{e.get('errorText')} on {str(e.get('url') or url)[:120]}"
    for u, why in failed:
        if why:
            return "refused", f"the guard refused {str(u)[:100]}: {str(why)[:120]}"
    for e in docs or errors:
        if e.get("errorText"):
            return "unknown", f"{e.get('errorText')} on {str(e.get('url') or url)[:120]}"
    return "unknown", f"the page did not load ({str(url)[:120]}) and the session recorded no reason"


def marks(page: Any) -> Dict[str, int]:
    """The current lengths of the session's records (pass to :func:`classify_failure` as ``since``)."""
    guard = getattr(page, "guard", None)
    budget = getattr(page, "budget", None)
    return {"failed": len(getattr(guard, "failed", None) or []),
            "methods": len(getattr(budget, "refused_methods", None) or []),
            "errors": len(getattr(page, "load_errors", None) or [])}


def is_error_page(url: str) -> bool:
    u = str(url or "")
    return u.startswith(("chrome-error://", "about:neterror")) or u == "about:blank#blocked"


__all__ = ["sibling_of", "sibling_hosts", "is_same_site_redirect", "sibling_origins", "classify_failure", "marks",
           "is_error_page", "CLASSES"]


def search_form_target(dom: str, base_url: str, input_name: str = "", input_id: str = "") -> str:
    """The URL the search input's own form sends to (its ``action``, absolute), "" when the input has no form with an
    action. The input is found by its ``name`` (else its ``id``) in the page's DOM; a local parse, nothing is sent
    (www.universetoday.com's search box is a form to duckduckgo.com in a new tab - the page
    stayed, no request left the browser, and the agent answered a start-page teaser)."""
    from html.parser import HTMLParser
    want_name, want_id = str(input_name or ""), str(input_id or "")
    if not dom or not (want_name or want_id):
        return ""

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.forms: List[Optional[str]] = []
            self.hit = ""

        def handle_starttag(self, tag, attrs):
            a = {k: (v or "") for k, v in attrs}
            if tag == "form":
                self.forms.append(a.get("action", "").strip() or None)
            elif tag == "input" and self.forms and not self.hit:
                if (want_name and a.get("name") == want_name) or (not want_name and want_id and a.get("id") == want_id):
                    act = self.forms[-1]
                    if act:
                        self.hit = act

        def handle_endtag(self, tag):
            if tag == "form" and self.forms:
                self.forms.pop()
    p = P()
    try:
        p.feed(dom)
    except Exception:                                  # noqa: BLE001 - a page that cannot be parsed decides nothing
        return ""
    if not p.hit or p.hit.lower().startswith(("javascript:", "#")):
        return ""
    return urllib.parse.urljoin(base_url, p.hit)


def leaves_site(url: str, start_url: str) -> bool:
    """Is ``url`` on another site than ``start_url`` (another registrable domain; the www sibling, a subdomain of the
    same domain and http -> https are the same site)?"""
    a, b = _host(url), _host(start_url)
    if not a or not b:
        return False

    def reg(h: str) -> str:
        p = h.split(".")
        return ".".join(p[-3:]) if len(p) >= 3 and len(p[-1]) == 2 and p[-2] in ("co", "com", "org", "net", "ac") \
            else ".".join(p[-2:])
    return reg(a) != reg(b)
