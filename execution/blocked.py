"""Blocked-state detection → hand the workflow back to a human.

When a site asks for a human check, the correct behaviour is to stop and say so.
**Nothing in this module bypasses, solves, or works around any challenge**, and
nothing should ever be added that does: no CAPTCHA solving, no token replay, no
header spoofing, no retry-until-it-works. A challenge is a site saying "not you,
not like this", and the only honest response is to tell the person, mark the host
so we stop knocking, and let them continue in their own browser session if they
want to.

What it does instead:

* :func:`detect_blocked` — deterministic signatures over a response (status,
  headers, body) and :func:`detect_blocked_ui` over what a browser rendered.
  Vendor challenge scripts and iframes (reCAPTCHA, hCaptcha, Turnstile), the
  typical 403/429 challenge bodies and headers (``cf-mitigated``,
  ``retry-after``), login walls, cookie-consent walls, rate limits and generic
  error pages.
* A typed :class:`BlockedState` with a machine-readable ``reason`` and a message
  written for a person, carried through the api executor's result and surfaced
  by the MCP tool.
* A "do not auto-retry" registry with a TTL (:func:`mark_blocked` /
  :func:`blocked_until`), so a blocked host is refused BEFORE a request is sent
  rather than being hammered once per replay.
* The pause goes through the EXISTING HIL gate: :func:`blocked_hil_action`
  projects the state into an action whose verb no action set recognises, which
  ``hil_gate.classify_step`` already classifies ``destructive`` (its fail-closed
  branch), so an active gate holds it and an inactive one still sees
  ``success=False``. No new gate, no new policy.

A hook is left for a typed judge to classify states this module cannot name
(:data:`UNKNOWN`) — see :func:`unknown_block_context`. Nothing is wired to it.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from anyuse.paths import anyuse_data_path

logger = logging.getLogger(__name__)

# -- reason codes ------------------------------------------------------------
CAPTCHA_RECAPTCHA = "captcha_recaptcha"
CAPTCHA_HCAPTCHA = "captcha_hcaptcha"
CAPTCHA_TURNSTILE = "captcha_turnstile"
CAPTCHA_GENERIC = "captcha_generic"
CHALLENGE_INTERSTITIAL = "challenge_interstitial"
RATE_LIMITED = "rate_limited"
FORBIDDEN = "forbidden"
LOGIN_WALL = "login_wall"
COOKIE_WALL = "cookie_wall"
ERROR_PAGE = "error_page"
UNKNOWN = "unknown_block"

#: How long a host stays on "do not auto-retry" when the site gave no hint.
DEFAULT_TTL_S = 6 * 3600
#: Only these reasons put a host on do-not-auto-retry. An error page is the
#: site being broken, not the site refusing us, so retrying later is fine and
#: suppressing the host for hours would be wrong.
STICKY_REASONS = frozenset({
    CAPTCHA_RECAPTCHA, CAPTCHA_HCAPTCHA, CAPTCHA_TURNSTILE, CAPTCHA_GENERIC,
    CHALLENGE_INTERSTITIAL, RATE_LIMITED, FORBIDDEN,
})
#: Rate limits are usually short-lived; honour Retry-After when present.
RATE_LIMIT_TTL_S = 900

_VENDOR = (
    (CAPTCHA_RECAPTCHA, (r"google\.com/recaptcha", r"\bg-recaptcha\b",
                         r"grecaptcha", r"recaptcha/api\.js")),
    (CAPTCHA_HCAPTCHA, (r"hcaptcha\.com", r"\bh-captcha\b", r"hcaptcha\.execute")),
    (CAPTCHA_TURNSTILE, (r"challenges\.cloudflare\.com", r"\bcf-turnstile\b",
                         r"cf-chl-", r"/cdn-cgi/challenge-platform")),
)
_GENERIC_CAPTCHA = (r"\bcaptcha\b", r"enter the characters", r"audio challenge",
                    r"i am not a robot", r"prove you are human")
_CHALLENGE_TEXT = (r"performing security verification", r"checking your browser",
                   r"verify you are human", r"security check", r"ray id",
                   r"attention required", r"ddos protection",
                   # "just a moment" is the Cloudflare interstitial's <title>;
                   # the others are self-hosted anti-scraper interstitials that
                   # answer 200 with a meta-refresh and a real-looking page.
                   r"just a moment", r"enable javascript and cookies to continue",
                   r"one more step before", r"unusual traffic from your")

#: URL markers of a challenge interstitial. sr.ht answered
#: ``/projects?search=mail`` with a 200 whose *final URL* was
#: ``?__goaway_challenge=meta-refresh&__goaway_id=…``. Nothing in the markup said
#: "challenge", so the recording was filed as an ordinary compile failure when
#: the site had in fact refused us — which flatters the compiler and hides a
#: block. A redirect that adds a challenge parameter is the signal.
_CHALLENGE_URL = (r"__goaway_challenge", r"/cdn-cgi/challenge-platform",
                  r"[?&]__cf_chl", r"/_Incapsula_Resource", r"/akam/\d",
                  r"[?&]captcha_challenge", r"[?&]bm-verify=")
#: the CDN's PASSIVE bot-detection script (Cloudflare's "JavaScript detections",
#: ``/cdn-cgi/challenge-platform/scripts/jsd/``, ``/cdn-cgi/challenge-platform/h/<x>/scripts/jsd/`` and its beacon
#: ``/cdn-cgi/challenge-platform/h/<x>/jsd/...``) runs in the
#: background of every page the CDN serves. Every challenge stop measured on fresh sites named only it
#: (www.sportsbikeshop.co.uk, www.digitalkamera.de - also on its search page, whose results render late -,
#: www.baumschule-horstmann.de, swimswam.com). A request to it is never challenge evidence; an interstitial carries its
#: own markers (its URL, its text, the challenge page's other requests).
#: Its beacon is part of it (www.digitalkamera.de again: ``POST /cdn-cgi/challenge-platform/h/g/jsd/oneshot/...``,
#: refused by the read-only browser and still in the page's network log).
_PASSIVE_DETECTION = re.compile(r"/cdn-cgi/challenge-platform/(?:h/[a-z]/)?(?:scripts/)?jsd/", re.I)


def is_passive_detection(url: str) -> bool:
    """A CDN's background bot-detection script (never a wall by itself)."""
    return bool(_PASSIVE_DETECTION.search(str(url or "")))


def challenge_url_marker(url: str) -> str:
    """The challenge marker a URL carries (``_CHALLENGE_URL``), or ``""`` - never for a passive detection script."""
    return "" if is_passive_detection(url) else _hit(_CHALLENGE_URL, url or "")


_LOGIN_TEXT = (r"\bsign in to continue\b", r"\blog in to continue\b",
               r"please sign in", r"please log in", r"login required",
               r"authentication required", r"you must be logged in")
_COOKIE_TEXT = (r"accept (all )?cookies", r"we use cookies", r"cookie (preferences|consent)",
                r"manage (your )?cookie", r"consent to the use of cookies",
                r"wir verwenden cookies", r"cookie[- ]?einstellungen",
                r"nous utilisons des cookies", r"param[eè]tres des cookies",
                r"usamos cookies", r"configuraci[oó]n de cookies",
                r"utilizziamo i cookie", r"impostazioni cookie",
                r"wij gebruiken cookies", r"cookie[- ]?instellingen")
_ERROR_TEXT = (r"\b(internal )?server error\b", r"service unavailable",
               r"temporarily unavailable", r"something went wrong",
               r"an error occurred", r"bad gateway")


def _hit(patterns, blob: str) -> Optional[str]:
    for pat in patterns:
        if re.search(pat, blob, re.I):
            return pat
    return None


@dataclass
class BlockedState:
    """A typed "the site will not serve this without a human" outcome."""
    blocked: bool = False
    reason: str = ""
    evidence: List[str] = field(default_factory=list)
    host: str = ""
    status: Optional[int] = None
    retry_after_s: Optional[int] = None
    ttl_s: int = DEFAULT_TTL_S

    @property
    def message(self) -> str:
        """What a person should read. Never a suggestion to work around it."""
        if not self.blocked:
            return ""
        host = self.host or "the site"
        if self.reason in (CAPTCHA_RECAPTCHA, CAPTCHA_HCAPTCHA, CAPTCHA_TURNSTILE,
                           CAPTCHA_GENERIC, CHALLENGE_INTERSTITIAL):
            return (f"{host} asks for a human check before it will serve this page. "
                    f"AnyUse has stopped and will not try to get around it. Open "
                    f"{host} in your own browser, complete the check there, and "
                    f"continue when you are ready.")
        if self.reason == RATE_LIMITED:
            wait = f" It asked us to wait {self.retry_after_s}s." if self.retry_after_s else ""
            return (f"{host} is rate-limiting us.{wait} AnyUse has stopped and will "
                    f"not retry automatically. Try again later, or run this step "
                    f"yourself.")
        if self.reason == LOGIN_WALL:
            return (f"{host} requires a signed-in session for this page. AnyUse "
                    f"replays only what a signed-out visitor can see, so it has "
                    f"stopped. Do this one in your own browser session.")
        if self.reason == COOKIE_WALL:
            return (f"{host} is showing a cookie-consent wall instead of the content. "
                    f"AnyUse will not click consent on your behalf. Accept or reject "
                    f"it in your own browser, then re-record this workflow.")
        if self.reason == FORBIDDEN:
            return (f"{host} refused the request (403). AnyUse has stopped and will "
                    f"not retry; the site may not permit automated access to this path.")
        if self.reason == ERROR_PAGE:
            return (f"{host} returned an error page rather than content. AnyUse has "
                    f"stopped; this is the site's problem, not a workflow problem.")
        return (f"{host} returned something AnyUse could not recognise as content "
                f"and it looks like a block. AnyUse has stopped rather than guess.")

    def to_dict(self) -> dict:
        return {"blocked": self.blocked, "reason": self.reason, "host": self.host,
                "status": self.status, "evidence": self.evidence[:4],
                "retry_after_s": self.retry_after_s, "ttl_s": self.ttl_s,
                "message": self.message,
                "action_required": "human" if self.blocked else ""}


def _retry_after(headers: Dict[str, str]) -> Optional[int]:
    """Seconds the site asked to wait in ``Retry-After``: a number of seconds or an HTTP
    date (RFC 9110); None without a readable one."""
    import email.utils
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            text = str(v).strip()
            try:
                return max(0, int(text))
            except ValueError:
                pass
            try:
                when = email.utils.parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                return None
            if when is None:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            return max(0, int(round(when.timestamp() - time.time())))
    return None


def retry_after_seconds(headers: Dict[str, str]) -> Optional[int]:
    """The response's ``Retry-After`` in seconds, or None (:func:`_retry_after`)."""
    return _retry_after(headers)


#: A document larger than this, served with 2xx, is a PAGE. A CAPTCHA vendor
#: script or the word "captcha" inside it is furniture — a login form somewhere
#: on the site, a package whose description mentions the word — and not a
#: challenge that was served INSTEAD of the content.
#:
#: packagist.org's ordinary package page is 83 KB of real content and mentions
#: ``grecaptcha`` three times; without this limit :func:`detect_blocked` called
#: it ``captcha_recaptcha``, put packagist.org on do-not-auto-retry for six
#: hours, and the workflow was filed as "the site said no". It is the same class
#: of mistake :func:`detect_blocked_ui` avoids by downgrading a soft wall when
#: content was in fact delivered.
CHALLENGE_MAX_DOC = 30_000
#: A consent WALL is nothing but the banner. Visible text beyond this many
#: characters means the content was served with a banner over it.
COOKIE_WALL_MAX_TEXT = 4_000


def _challenge_shaped(blob: str, status: Optional[int], url: str) -> str:
    """Why this response looks like a challenge PAGE rather than a page with a
    challenge mentioned in it — or ``""`` if it does not.

    A real interstitial is short and is mostly the challenge; it arrives with a
    non-2xx status, or at a URL carrying a challenge marker, or it announces
    itself in the title. A 200 that delivered eighty kilobytes of content did
    none of those things.
    """
    if status is not None and not (200 <= int(status) < 300):
        return f"HTTP {status}"
    if challenge_url_marker(url):
        return "the URL carries a challenge marker"
    if len(blob) <= CHALLENGE_MAX_DOC:
        return f"short document ({len(blob)} chars)"
    m = re.search(r"<title[^>]*>(.{0,200}?)</title>", blob, re.I | re.S)
    if m and _hit(_CHALLENGE_TEXT + _GENERIC_CAPTCHA, m.group(1)):
        return f"the document title says so ({m.group(1).strip()[:60]!r})"
    return ""


def detect_blocked(status: Optional[int], headers: Optional[Dict[str, str]],
                   body: str, url: str = "") -> BlockedState:
    """Deterministic blocked-state detection over one HTTP response.

    Order matters: a named vendor challenge is the most specific thing we can
    say, then the status-based signals, then the textual walls. Everything is a
    regex over markup or headers — nothing here executes a page.

    A CAPTCHA marker only counts when the response is **challenge-shaped**
    (:func:`_challenge_shaped`). Without that guard the detector reads the word
    rather than the situation, and a served page becomes a six-hour block.
    """
    host = urllib.parse.urlsplit(url).netloc
    headers = {str(k): str(v) for k, v in (headers or {}).items()}
    blob = (body or "")[:200_000]
    hdr_blob = " ".join(f"{k}:{v}" for k, v in headers.items())
    state = BlockedState(host=host, status=status)

    murl = "" if is_passive_detection(url) else url        # a passive detection script names no vendor wall
    for reason, patterns in _VENDOR:
        hit = _hit(patterns, blob) or _hit(patterns, murl)
        if hit:
            shape = _challenge_shaped(blob, status, url)
            if not shape:
                state.evidence.append(
                    f"matched {hit!r} but the response is a served page "
                    f"({len(blob)} chars, HTTP {status}) — treated as furniture, "
                    f"not as a challenge")
                continue
            state.blocked, state.reason = True, reason
            state.evidence = [f"matched {hit!r}", shape]
            return state

    if _hit((r"\bcf-mitigated\b", r"\bcf-chl-bypass\b"), hdr_blob):
        state.blocked, state.reason = True, CHALLENGE_INTERSTITIAL
        state.evidence = ["challenge header present"]
        return state

    hit = challenge_url_marker(url)
    if hit:
        state.blocked, state.reason = True, CHALLENGE_INTERSTITIAL
        state.evidence = [f"the URL we ended up at matches {hit!r}"]
        return state

    if status == 429:
        state.blocked, state.reason = True, RATE_LIMITED
        state.retry_after_s = _retry_after(headers)
        state.ttl_s = state.retry_after_s or RATE_LIMIT_TTL_S
        state.evidence = ["HTTP 429"]
        return state

    hit = _hit(_GENERIC_CAPTCHA, blob)
    if hit:
        shape = _challenge_shaped(blob, status, url)
        if shape:
            state.blocked, state.reason = True, CAPTCHA_GENERIC
            state.evidence = [f"matched {hit!r}", shape]
            return state
        state.evidence.append(
            f"matched {hit!r} in a served page ({len(blob)} chars) — the word is "
            f"not the situation; not treated as a block")

    hit = _hit(_CHALLENGE_TEXT, blob)
    if hit:
        shape = _challenge_shaped(blob, status, url)
        if shape:
            state.blocked, state.reason = True, CHALLENGE_INTERSTITIAL
            state.evidence = [f"matched {hit!r}", shape]
            return state
        state.evidence.append(
            f"matched {hit!r} in a served page ({len(blob)} chars) — not treated "
            f"as a block")

    if status == 403:
        state.blocked, state.reason = True, FORBIDDEN
        state.evidence = ["HTTP 403"]
        return state

    hit = _hit(_LOGIN_TEXT, blob)
    if hit:
        state.blocked, state.reason = True, LOGIN_WALL
        state.evidence = [f"matched {hit!r}"]
        return state

    hit = _hit(_COOKIE_TEXT, blob)
    if hit and len(blob) < 20_000:
        # A consent banner on a full page is normal furniture; a consent WALL is
        # a short document that is nothing but the banner.
        state.blocked, state.reason = True, COOKIE_WALL
        state.evidence = [f"matched {hit!r}", f"short document ({len(blob)} chars)"]
        return state

    # NOTE: a plain 5xx is NOT a blocked state. It is an ordinary failure the
    # ladder already handles by falling back, and calling it "blocked" would put
    # the host on do-not-auto-retry for hours because a server hiccuped once.
    # Only a 2xx that RENDERS an error page counts here.
    hit = _hit(_ERROR_TEXT, blob)
    if hit and len(blob) < 8_000 and (status is None or 200 <= int(status) < 300):
        state.blocked, state.reason = True, ERROR_PAGE
        state.evidence = [f"matched {hit!r}"]
        return state

    return state


#: Reasons that survive the page having rendered content anyway. A CAPTCHA or a
#: challenge page can carry a decorative list; a cookie banner over a working
#: result list is furniture, not a wall.
HARD_REASONS = frozenset({
    CAPTCHA_RECAPTCHA, CAPTCHA_HCAPTCHA, CAPTCHA_TURNSTILE, CAPTCHA_GENERIC,
    CHALLENGE_INTERSTITIAL, RATE_LIMITED, FORBIDDEN,
})


#: Requests that are never "the site answering us": beacons, CSP violation
#: reports, pings, pixels and the like. A status code on one of these says
#: nothing about whether the page was served — measured: a third-party
#: CSP-report endpoint's 429 blocked a whole host for the block TTL, and the
#: same beacon on the page's OWN host would have done the same.
_SIDE_RESOURCE_TYPES = frozenset({
    "ping", "cspviolationreport", "beacon", "image", "font", "stylesheet",
    "media", "texttrack", "manifest", "preflight", "signedexchange"})
_SIDE_URL = re.compile(
    r"(report-uri|/csp[-_/]?(report|violation)|/beacon\b|/collect\b|/pixel\b|"
    r"/analytics\b|/telemetry\b|/__utm|/gtm\.js|/g/collect|/sentry|/rum\b)",
    re.I)


def is_side_request(url: str, resource_type: Optional[str] = None) -> bool:
    """A tracker, beacon or report request — never evidence about the page."""
    if resource_type and str(resource_type).lower() in _SIDE_RESOURCE_TYPES:
        return True
    path = urllib.parse.urlsplit(url or "").path or ""
    host = urllib.parse.urlsplit(url or "").netloc or ""
    return bool(_SIDE_URL.search(path) or _SIDE_URL.search(host))


def detect_blocked_ui(page_text: str, markup: str = "", url: str = "",
                      requests: Optional[List[dict]] = None,
                      items: Optional[List[str]] = None) -> BlockedState:
    """The rules below, then the confirmer (off by default).

    The confirmer only logs (``shadow``) or tightens (``veto``, per kind that
    passed its enable rule); it never clears a block.
    """
    state = _detect_blocked_ui_rules(page_text, markup, url, requests, items)
    try:
        from anyuse.execution import confirmer
        if confirmer.mode() == "off":
            return state
        out = confirmer.observe_block(state, page_text, url, requests or [], items or [])
        if state.blocked and not getattr(out, "blocked", False):
            return state                     # never relax a stop
        return out
    except Exception:
        return state


def _detect_blocked_ui_rules(page_text: str, markup: str = "", url: str = "",
                             requests: Optional[List[dict]] = None,
                             items: Optional[List[str]] = None) -> BlockedState:
    """Blocked-state detection for the UI path (what a browser rendered).

    Takes the visible text (what the person would see), optionally the markup,
    and optionally the request log — a challenge platform often shows in the
    network log even when the rendered text is bland.

    ``items`` is the result list the page actually rendered. When it is
    non-empty, a *soft* wall (cookie consent, login prompt, error text) is
    downgraded to "not blocked": the content arrived, so whatever banner is on
    the page did not stop it. Without it, the detector called
    nuget.org and data.europa.eu blocked because their consent banners matched
    while both had served a full result list. A hard block —
    CAPTCHA, challenge interstitial, 403, 429 — is never downgraded.
    """
    # A named vendor challenge in the REQUEST LOG is the most specific thing we
    # can say, so look for it before the page's own words: "Just a moment" plus
    # a /cdn-cgi/challenge-platform request is Turnstile, and reporting it as a
    # generic interstitial would throw that away. Only URL markers are consulted
    # here — a status code is not, because one 403 on a third-party beacon must
    # never make a page that rendered fine count as blocked.
    # (www.sportsbikeshop.co.uk and www.digitalkamera.de rendered their whole start
    # page - its scripts, menus and widgets - while the CDN's background bot-detection script under
    # /cdn-cgi/challenge-platform/scripts/jsd/ ran; both were stopped as a Turnstile wall): a challenge request in the
    # log is a wall only when the page did NOT render its content - a short page, or one that says it checks the browser
    rendered = len(page_text or "") > CHALLENGE_MAX_VISIBLE_TEXT and not _hit(_CHALLENGE_TEXT, page_text or "")
    for r in requests or []:
        if rendered:
            break
        sub = detect_blocked(None, {}, "", r.get("url", ""))
        if sub.blocked:
            sub.host = urllib.parse.urlsplit(url).netloc or sub.host
            sub.evidence = [f"challenge request {r.get('url', '')[:80]}"]
            return sub

    state = detect_blocked(None, {}, f"{page_text}\n{markup}", url)
    if state.blocked:
        if state.reason == COOKIE_WALL and len(page_text or "") > COOKIE_WALL_MAX_TEXT:
            # A consent WALL is a document that is nothing but the banner (the
            # one measured was 1 414 characters). A page with this much
            # visible text served its content and put a banner over it
            # (a search page that said "more than 200
            # results" in 12 KB of text was filed as a wall).
            downgraded = BlockedState(host=state.host, status=state.status)
            downgraded.evidence = [
                f"consent banner matched but the page has {len(page_text)} chars of "
                f"visible text — furniture on a served page, not a wall"]
            return downgraded
        if items and state.reason not in HARD_REASONS:
            downgraded = BlockedState(host=state.host, status=state.status)
            downgraded.evidence = [
                f"{state.reason} markers present but the page rendered "
                f"{len(items)} result(s), so the content was served"]
            return downgraded
        return state
    # A status code is consulted only for requests to the PAGE'S OWN host. A
    # 429 from a third-party CSP-report endpoint, an analytics beacon or a CDN
    # the page happens to load is not the site refusing us — the page rendered
    # (dailymed.nlm.nih.gov, 224 KB of results, was filed as
    # rate-limited because report-uri.com answered its CSP reports with 429).
    page_host = (urllib.parse.urlsplit(url).netloc or "").lower()
    for r in requests or []:
        r_url = str(r.get("url") or "")
        r_host = (urllib.parse.urlsplit(r_url).netloc or "").lower()
        if page_host and r_host != page_host:
            continue
        if is_side_request(r_url, r.get("resource_type")):
            continue
        # on a page that rendered its content, only the request's status speaks, not a challenge URL marker
        sub = detect_blocked(r.get("status"), {}, "", "" if rendered else r.get("url", ""))
        if sub.blocked:
            sub.host = state.host or sub.host
            sub.evidence = [f"challenge request {r.get('url', '')[:80]}"]
            return sub
    return state


#: Visible text of a real challenge interstitial is a few hundred characters
#: ("Performing security verification ... Ray ID ..."). A page that shows more
#: than this and no result list is still treated as a challenge only on a URL
#: marker, never on a phrase.
CHALLENGE_MAX_VISIBLE_TEXT = 3_000


def challenge_served_instead(page_text: str, final_url: str = "",
                             request_urls: Optional[List[str]] = None,
                             items: Optional[List[str]] = None) -> str:
    """Evidence that a challenge was served INSTEAD of the page, or ``""``.

    For a recording the compiler could not explain. Bare substrings are not
    evidence: a package-search result page was labelled
    "anti-bot interstitial" because one result's name contained
    ``recaptcha``. The rule is the one :func:`detect_blocked` already uses for
    HTTP responses — the word is not the situation:

    * the URL the page ended at carries a challenge marker: a challenge;
    * otherwise, a page that rendered a result list was served — a challenge
      script among its sub-requests (many CDNs inject one into every page) or a
      result that mentions a CAPTCHA does not change that;
    * with no result list: a challenge-platform sub-request, or a challenge /
      CAPTCHA phrase (word-bounded) on a short page.
    """
    hit = challenge_url_marker(final_url or "")
    if hit:
        return f"the page ended at a URL matching {hit!r}"
    if items:
        return ""
    for u in request_urls or []:
        hit = challenge_url_marker(u or "")
        if hit:
            return f"a challenge-platform request ({(u or '')[:80]})"
    text = page_text or ""
    if len(text) <= CHALLENGE_MAX_VISIBLE_TEXT:
        hit = _hit(_CHALLENGE_TEXT + _GENERIC_CAPTCHA, text)
        if hit:
            return f"a short page ({len(text)} chars) matching {hit!r}"
    return ""


# ---------------------------------------------------------------------------
# "do not auto-retry" registry
# ---------------------------------------------------------------------------

def blocked_hosts_file() -> str:
    """ANYUSE_BLOCKED_HOSTS_FILE wins; otherwise it lives in the data dir."""
    return os.environ.get("ANYUSE_BLOCKED_HOSTS_FILE") or str(
        anyuse_data_path("api_blocked_hosts.json"))


def _load(path: Optional[str]) -> Dict[str, dict]:
    path = path or blocked_hosts_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_blocked(state: BlockedState, path: Optional[str] = None) -> dict:
    """Record "do not auto-retry this host" with a TTL. Returns the entry."""
    path = path or blocked_hosts_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hosts = _load(path)
    until = time.time() + max(60, int(state.ttl_s or DEFAULT_TTL_S))
    entry = {
        "reason": state.reason, "until_epoch": until,
        "until": datetime.datetime.fromtimestamp(until).astimezone().isoformat(
            timespec="seconds"),
        "message": state.message, "evidence": state.evidence[:4],
    }
    hosts[state.host] = entry
    with open(path, "w") as fh:
        json.dump(hosts, fh, indent=2, sort_keys=True)
    return entry


def blocked_until(host: str, path: Optional[str] = None,
                  now: Optional[float] = None) -> Optional[dict]:
    """The live "do not auto-retry" entry for ``host``, or ``None`` if expired."""
    entry = _load(path).get(host)
    if not entry:
        return None
    if float(entry.get("until_epoch", 0)) <= (now if now is not None else time.time()):
        return None
    return entry


def clear_blocked(host: str, path: Optional[str] = None) -> bool:
    """Forget a host's block — what a person calls after solving the check."""
    path = path or blocked_hosts_file()
    hosts = _load(path)
    if host not in hosts:
        return False
    del hosts[host]
    with open(path, "w") as fh:
        json.dump(hosts, fh, indent=2, sort_keys=True)
    return True


# ---------------------------------------------------------------------------
# browser-only hosts
# ---------------------------------------------------------------------------
#
# hackage.haskell.org answers AnyUse's named HTTP client with 403 and serves the
# container browser the page, normally, with no challenge. Both of its
# workflows were filed as tier 4 — "blocked, hand it to a human" — which is not
# what is happening: the site is not refusing *us*, it is refusing a client that
# is not a browser. Two workflows of thirty-two sat in the wrong bucket because
# of it.
#
# The answer is NOT to make the HTTP client look like a browser. AnyUse sends a
# descriptive User-Agent and does not spoof one, and that rule has no exceptions
# in it. The answer is to record the fact and route around it: on a browser-only
# host the compiled-request rung is never tried, the browser replay rung is, and
# every surface that mentions the host says which and why.
#
# The classification costs NO new traffic and never re-hits a refusing host. It
# is drawn from two observations we already hold: the compiled request's 403,
# and the recording, which the container browser made successfully at the time.

BROWSER_ONLY_FILE_ENV = "ANYUSE_BROWSER_ONLY_HOSTS_FILE"

#: Only these refusals can mean "not a browser". A CAPTCHA, a login wall or a
#: consent wall is a refusal of the *visitor*, and a browser that got past one
#: got past it because a human or a cookie did — never because it is a browser.
#: Classifying those as browser-only would be a way of talking ourselves into
#: re-hitting a host that said no.
BROWSER_ONLY_FROM = frozenset({FORBIDDEN, RATE_LIMITED})


def browser_only_hosts_file() -> str:
    return os.environ.get(BROWSER_ONLY_FILE_ENV) or str(
        anyuse_data_path("api_browser_only_hosts.json"))


def mark_browser_only(host: str, status: Optional[int], reason: str,
                      browser_evidence: str, path: Optional[str] = None
                      ) -> Optional[dict]:
    """Record ``host`` as "serves a browser, refuses a named HTTP client".

    ``browser_evidence`` must say how we know the browser was served — the
    recording's URL and item count, normally. Returns the entry, or ``None``
    when the reason is not one that may be reclassified.

    Recording this also **clears the host's do-not-auto-retry entry**, because
    it is the same fact better understood: the 403 was about the client, and the
    rung that will be used from now on is the one that did not get it.
    """
    if reason not in BROWSER_ONLY_FROM or not host or not browser_evidence:
        return None
    path = path or browser_only_hosts_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hosts = _load(path)
    entry = {
        "host": host, "status": status, "from_reason": reason,
        "browser_evidence": str(browser_evidence)[:300],
        "since": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": (f"{host} answered AnyUse's named HTTP client with "
                 f"{status or reason} and served the container browser the page. "
                 f"The compiled-request rung is not attempted here; the "
                 f"deterministic browser replay rung is. AnyUse does not change "
                 f"its User-Agent or headers to look like a browser — that is a "
                 f"rule with no exceptions, and this is what it costs."),
    }
    hosts[host] = entry
    with open(path, "w") as fh:
        json.dump(hosts, fh, indent=2, sort_keys=True)
    clear_blocked(host)
    return entry


def is_browser_only(host: str, path: Optional[str] = None) -> Optional[dict]:
    """The browser-only entry for ``host``, or ``None``.

    Deliberately has no TTL. It is not a punishment that should lapse; it is an
    observation about how the host treats two different clients, and it is
    cleared by :func:`clear_browser_only` or by a later successful request.
    """
    if not host:
        return None
    return _load(path or browser_only_hosts_file()).get(host)


def clear_browser_only(host: str, path: Optional[str] = None) -> bool:
    path = path or browser_only_hosts_file()
    hosts = _load(path)
    if host not in hosts:
        return False
    del hosts[host]
    with open(path, "w") as fh:
        json.dump(hosts, fh, indent=2, sort_keys=True)
    return True


def browser_only_hosts(path: Optional[str] = None) -> Dict[str, dict]:
    return dict(_load(path or browser_only_hosts_file()))


def browser_only_from_evidence(state: "BlockedState", browser_url: str,
                               browser_items: int, path: Optional[str] = None
                               ) -> Optional[dict]:
    """Reclassify a client refusal as "browser-only", from evidence in hand.

    ``state`` is what the compiled request got. ``browser_url`` and
    ``browser_items`` are what the container browser saw for the SAME host when
    the workflow was recorded — an already-made observation, not a new request.
    Returns the entry, or ``None`` when the refusal is not the reclassifiable
    kind or the browser never actually saw content.

    This function never makes a request and never re-hits anything.
    """
    if not state or not state.blocked:
        return None
    if int(browser_items or 0) < 1 or not browser_url:
        return None
    if urllib.parse.urlsplit(browser_url).netloc != state.host:
        return None
    return mark_browser_only(
        state.host, state.status, state.reason,
        f"the container browser loaded {browser_url[:120]} and read "
        f"{browser_items} item(s) from it, with no challenge", path=path)


# ---------------------------------------------------------------------------
# HIL projection + the typed-judge hook
# ---------------------------------------------------------------------------

def blocked_hil_action(state: BlockedState) -> dict:
    """Project a blocked state into the shape ``hil_gate.classify_step`` consumes.

    The verb ``blocked_<reason>`` is in no action set, so the gate's fail-closed
    branch classifies it ``destructive`` and an active gate holds it — which is
    exactly the desired "pause and ask the human". Pure; sends nothing.
    """
    return {"action": f"blocked_{state.reason or UNKNOWN}",
            "args": {"host": state.host, "intent": state.message,
                     "evidence": state.evidence[:3]}}


def unknown_block_context(status: Optional[int], headers: Optional[Dict[str, str]],
                          body: str, url: str = "") -> Dict[str, Any]:
    """HOOK (not wired): the redacted context a typed judge would classify.

    When :func:`detect_blocked` returns ``blocked=False`` but the caller still
    believes it was blocked — an unfamiliar vendor, a bespoke interstitial — this
    is what a small typed model would be asked about. Values are not included;
    only the shape of the page and the headers that bear on blocking. Nothing
    calls this yet, and the decision would be advisory: an unrecognised block
    must still stop the workflow, never continue it.
    """
    blob = (body or "")[:4000]
    return {
        "host": urllib.parse.urlsplit(url).netloc,
        "status": status,
        "headers_seen": sorted(k.lower() for k in (headers or {})
                               if k.lower() in ("server", "retry-after", "cf-mitigated",
                                                "content-type", "x-frame-options")),
        "document_length": len(body or ""),
        "iframe_count": len(re.findall(r"<iframe", blob, re.I)),
        "script_hosts": sorted({urllib.parse.urlsplit(m).netloc
                                for m in re.findall(r'src="(https://[^"]+)"', blob)})[:8],
        "form_count": len(re.findall(r"<form", blob, re.I)),
        "visible_text_sample": re.sub(r"<[^>]+>", " ", blob)[:300],
    }
