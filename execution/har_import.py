"""Import a HAR file the user exported from their own browser's DevTools.

This is the adoption lever. Everything else in the compile path needs AnyUse to
have recorded the workflow; a HAR needs nothing but *File → Save all as HAR* in
a browser the person already has open, on a site they are already looking at.

What it does, in one line: turn a HAR into the same minimal ``recording`` dict
``api_compile`` already consumes, with every credential removed at the door, and
then run the **same fresh-input proof** — no shortcut, no lower bar.

Threat model
------------
**A HAR is attacker-controllable input.** The person may have been handed one,
or may have recorded a page that was not what it claimed to be. So:

* **No code execution.** The file is parsed with ``json.loads`` and nothing
  else. No ``eval``, no pickle, no XML entity resolution, no archive extraction.
* **No network, ever.** Importing a HAR fetches nothing. URLs inside it are
  data. In particular the importer never follows a URL, never resolves a
  redirect and never loads a referenced resource — the compile step can only
  ever emit a template for a request that is **already in the file**
  (:func:`origins`), and `--from-har` sends nothing by itself.
* **Size caps** on the file (:data:`MAX_FILE_BYTES`), the number of entries
  (:data:`MAX_ENTRIES`) and each stored body (:data:`MAX_BODY_BYTES`). A body
  over the cap is dropped rather than truncated: half a JSON document is not a
  smaller JSON document, it is a different one.
* **Path traversal is not a risk here** because the importer writes nothing.
  It returns data structures; persisting is the caller's decision and goes
  through the existing workflow storage.
* **Credentials are refused, not stripped**, except cookies: those are
  stripped, and the cookieless proof decides. See below.

Credentials
-----------
Three different things happen to three different kinds of secret.

*Removed at the door and never returned to the caller:* ``Cookie``,
``Set-Cookie``, ``Authorization``, ``Proxy-Authorization``, ``X-Api-Key`` and
every other header in :data:`api_step.CREDENTIAL_HEADERS`, plus the values in
HAR's own ``cookies`` arrays and anything whose name matches
:func:`api_step.signed_param_reason`. They never enter a recording, so they can
never be persisted, logged or sent.

*Cookies are kept as names and judged by the proof.* A browser sends its
cookies with every request to a site that set one (a consent choice,
analytics), and Firefox, Safari and Chrome's export "with sensitive data" write
them into the HAR. Refusing such requests would refuse almost every HAR those
exports produce. So a request whose only credential is a cookie stays a
candidate, with the cookie NAMES on :attr:`HarEntry.sent_cookies`. The
compiled template cannot carry a cookie (:data:`api_step.SAFE_HEADERS`), so the
fresh-input proof replays it without them. A request that needs its cookies
fails that proof and is not saved, and the report says it probably needs
sign-in (``needs_sign_in``).

*Recorded as a refusal reason:* the FACT that a request carried any other
credential: an ``Authorization``, API-key or CSRF header, or a credential in
the URL. Such an entry is marked ``credential_bound`` with the name of what it
carried, and :func:`compile_from_har` reports it instead of compiling it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from anyuse.execution.api_step import CREDENTIAL_HEADERS, signed_param_reason

logger = logging.getLogger(__name__)

#: Largest HAR accepted. A browser session of a few minutes is a few MB; 64 MB
#: is generous and still bounded.
MAX_FILE_BYTES = 64 * 1024 * 1024
#: Largest number of entries read from one file.
MAX_ENTRIES = 5_000
#: Largest single response body kept. Over this the body is DROPPED, not cut.
MAX_BODY_BYTES = 8 * 1024 * 1024

#: Only these schemes are looked at at all.
ALLOWED_SCHEMES = ("https", "http")

#: Response headers worth keeping. Everything else is discarded, because the
#: compiler needs exactly one thing from a response header block and a HAR's
#: header list is a good place for a surprise.
KEEP_RESPONSE_HEADERS = ("content-type",)

#: Cookie names kept per request, and the longest name kept. Names only: a
#: cookie value never leaves the parser.
MAX_COOKIE_NAMES = 20
MAX_COOKIE_NAME_CHARS = 64


class HarError(ValueError):
    """The file is not a usable HAR. The message is meant for a person."""


@dataclass
class HarEntry:
    """One sanitised request/response pair."""
    url: str = ""
    method: str = "GET"
    status: Optional[int] = None
    mime: str = ""
    body: Optional[str] = None
    post_data: Optional[str] = None
    bytes: int = 0
    resource_type: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    #: non-empty when this request carried a credential other than a cookie —
    #: the reason, in words
    credential_bound: str = ""
    #: why a body is missing, when it is
    body_dropped: str = ""
    #: the request's own User-Agent (identity=browser replays it; never invented)
    request_user_agent: str = ""
    #: the NAMES of the cookies the browser sent with this request, never a
    #: value. Not part of :meth:`to_network`: there ``cookie_names`` means a
    #: login=replay capture, whose template replays the cookies, and a HAR
    #: compile never does.
    sent_cookies: List[str] = field(default_factory=list)
    #: the credential-named fields whose values an earlier page of the site stated
    #: by name (``page_literals``): each field with its source page and locator
    page_keys: List[Dict[str, Any]] = field(default_factory=list)
    #: a redirect's target (``response.redirectURL``)
    redirect_url: str = ""
    #: the URLs of the redirects that led to this request, first first
    redirected_from: List[str] = field(default_factory=list)

    def to_network(self) -> dict:
        """The shape ``api_compile`` reads."""
        out = {"url": self.url, "method": self.method, "status": self.status,
               "mime": self.mime, "body": self.body, "post_data": self.post_data,
               "bytes": self.bytes, "resource_type": self.resource_type,
               "headers": dict(self.headers)}
        if self.request_user_agent:
            out["request_user_agent"] = self.request_user_agent
        if self.page_keys:
            out["page_keys"] = [dict(k) for k in self.page_keys]
        if self.redirect_url:
            out["redirect_url"] = self.redirect_url
        if self.redirected_from:
            out["redirected_from"] = list(self.redirected_from)
        return out


@dataclass
class HarImport:
    """A parsed HAR, plus everything that was refused and why."""
    entries: List[HarEntry] = field(default_factory=list)
    #: entries dropped before they became entries, as (url, reason)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    #: source file name, for the report only
    source: str = ""
    creator: str = ""

    @property
    def credential_bound(self) -> List[HarEntry]:
        return [e for e in self.entries if e.credential_bound]

    @property
    def with_cookies(self) -> List[HarEntry]:
        """Candidates the browser sent cookies with; the proof replays them without."""
        return [e for e in self.entries if e.sent_cookies and not e.credential_bound]

    def origins(self) -> List[str]:
        """Every ``scheme://host`` that appears in the file.

        A compiled template's URL must be one of these. Nothing outside the
        recorded origins is ever contacted, because nothing outside them is
        ever a candidate.
        """
        out = []
        for e in self.entries:
            sp = urllib.parse.urlsplit(e.url)
            origin = f"{sp.scheme}://{sp.netloc}"
            if origin not in out:
                out.append(origin)
        return out

    def summary(self) -> dict:
        return {"source": self.source, "creator": self.creator,
                "entries": len(self.entries),
                "skipped": len(self.skipped),
                "credential_bound": len(self.credential_bound),
                "with_cookies": len(self.with_cookies),
                "origins": self.origins()}


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _add_cookie_names(out: List[str], names) -> None:
    """Append cookie names to ``out``, deduped and capped.

    A name with a control character is skipped: no browser sends one, and the
    names are printed to the terminal in the sign-in hint."""
    for name in names:
        name = str(name or "").strip()[:MAX_COOKIE_NAME_CHARS]
        if (name and name.isprintable() and name not in out
                and len(out) < MAX_COOKIE_NAMES):
            out.append(name)


def _cookie_header_names(value: str) -> List[str]:
    """The cookie names in a ``Cookie`` header value; the values are dropped here."""
    return [part.split("=", 1)[0] for part in str(value or "").split(";") if "=" in part]


def _cookie_array_names(raw: Any) -> List[str]:
    """The names in a HAR ``request.cookies`` array; the values are never read."""
    if not isinstance(raw, list):
        return []
    return [item.get("name") for item in raw
            if isinstance(item, dict) and isinstance(item.get("name"), str)]


def _headers(raw: Any, cookies: Optional[List[str]] = None) -> Tuple[Dict[str, str], str]:
    """Sanitise a HAR header list → (kept headers, credential reason).

    With ``cookies`` (a request's header list), a ``Cookie`` header is not a
    refusal reason: its names are appended there and its value is dropped."""
    kept: Dict[str, str] = {}
    reason = ""
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        low = name.lower()
        if not low:
            continue
        if low == "cookie" and cookies is not None:
            _add_cookie_names(cookies, _cookie_header_names(item.get("value")))
            continue
        if low in CREDENTIAL_HEADERS:
            reason = reason or f"the request carried a {name!r} header"
            continue
        why = signed_param_reason(low)
        if why:
            reason = reason or f"the request carried a {name!r} header"
            continue
        # The value is never kept for anything but content-type. Header values
        # are the single most likely place for a token in a HAR, and the
        # compiler has no use for them.
        if low in KEEP_RESPONSE_HEADERS:
            kept[low] = str(item.get("value") or "")[:200]
    return kept, reason


def _query_credential(url: str) -> str:
    for key, _val in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                            keep_blank_values=True):
        why = signed_param_reason(key)
        if why:
            return why
    return ""


def _redact_query_credentials(url: str) -> str:
    """Remove credential values before a URL enters HarEntry or a report."""
    parts = urllib.parse.urlsplit(url)
    query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "<redacted>" if signed_param_reason(key) else value))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))


def _body(content: Any) -> Tuple[Optional[str], int, str]:
    """(text, size, dropped_reason) from a HAR ``response.content``."""
    if not isinstance(content, dict):
        return None, 0, "no content object"
    size = int(content.get("size") or 0)
    if str(content.get("encoding") or "").lower() == "base64":
        # Deliberately not decoded. A base64 body in a HAR is a binary
        # resource; decoding it would be work in service of nothing, and
        # handing arbitrary decoded bytes to a parser is the sort of thing this
        # module exists to avoid.
        return None, size, "body is base64-encoded (binary resource)"
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return None, size, "no response body recorded (DevTools was not capturing it)"
    if len(text.encode("utf-8", "ignore")) > MAX_BODY_BYTES:
        return None, size, f"response body over the {MAX_BODY_BYTES}-byte cap — dropped, not truncated"
    return text, size or len(text), ""


def parse_har(text: str, source: str = "",
              capture_ua_for=None) -> HarImport:
    """Parse and sanitise HAR text. Raises :class:`HarError` on anything else.

    ``capture_ua_for(host) -> bool`` (site-mode identity=browser, passed only by
    the product CLI through the one accessor) decides whether a request's
    User-Agent is kept; without it none is, so a default import is unchanged."""
    if len(text.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
        raise HarError(f"HAR is larger than the {MAX_FILE_BYTES // (1024 * 1024)} MB cap")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise HarError(f"not valid JSON: {exc}") from exc
    log = (data or {}).get("log") if isinstance(data, dict) else None
    if not isinstance(log, dict):
        raise HarError("no 'log' object — this does not look like a HAR file")
    raw_entries = log.get("entries")
    if not isinstance(raw_entries, list):
        raise HarError("'log.entries' is missing or is not a list")

    out = HarImport(source=source,
                    creator=str(((log.get("creator") or {})
                                 if isinstance(log.get("creator"), dict) else {}
                                 ).get("name") or ""))
    #: per entry: the raw credential-named fields (held only here) and its body
    raw_fields: Dict[int, Dict[str, str]] = {}
    for raw in raw_entries[:MAX_ENTRIES]:
        if not isinstance(raw, dict):
            continue
        req = raw.get("request") if isinstance(raw.get("request"), dict) else {}
        resp = raw.get("response") if isinstance(raw.get("response"), dict) else {}
        raw_url = str(req.get("url") or "")
        url = _redact_query_credentials(raw_url)
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            out.skipped.append((url[:120], f"scheme {scheme or 'none'!r} is not http(s)"))
            continue
        sent_cookies: List[str] = []
        kept, cred = _headers(req.get("headers"), cookies=sent_cookies)
        _add_cookie_names(sent_cookies, _cookie_array_names(req.get("cookies")))
        request_ua = ""
        if capture_ua_for is not None:
            try:
                wanted = bool(capture_ua_for(
                    (urllib.parse.urlsplit(url).hostname or "").lower()))
            except Exception:                         # noqa: BLE001
                wanted = False
            for item in (req.get("headers") or []) if wanted else []:
                if isinstance(item, dict) and str(item.get("name") or "").lower() == "user-agent":
                    request_ua = str(item.get("value") or "")[:300]
                    break
        body, size, dropped = _body(resp.get("content"))
        _resp_headers, _ = _headers(resp.get("headers"))
        mime = (_resp_headers.get("content-type")
                or str((resp.get("content") or {}).get("mimeType") or ""))
        post = (req.get("postData") or {}).get("text") if isinstance(
            req.get("postData"), dict) else None
        if isinstance(post, str) and len(post) > MAX_BODY_BYTES:
            post = None
        entry = HarEntry(
            url=url,
            method=str(req.get("method") or "GET").upper(),
            status=int(resp.get("status") or 0) or None,
            mime=mime.split(";")[0].strip(),
            body=body,
            post_data=post if isinstance(post, str) else None,
            bytes=size,
            resource_type=str(raw.get("_resourceType") or ""),
            headers=kept,
            credential_bound=cred or _query_credential(raw_url),
            body_dropped=dropped,
            request_user_agent=request_ua,
            sent_cookies=sent_cookies,
            redirect_url=_redact_query_credentials(urllib.parse.urljoin(
                raw_url, str(resp.get("redirectURL") or ""))) if resp.get("redirectURL") else "",
        )
        if entry.credential_bound and not cred_never(req.get("headers")):
            from anyuse.execution.page_literals import credential_fields
            fields, never = credential_fields(raw_url, _raw_headers(req.get("headers")),
                                              post if isinstance(post, str) else None)
            if fields and not never:
                raw_fields[len(out.entries)] = fields
        out.entries.append(entry)
    if len(raw_entries) > MAX_ENTRIES:
        out.skipped.append(("", f"{len(raw_entries) - MAX_ENTRIES} entries beyond "
                                f"the {MAX_ENTRIES}-entry cap were not read"))
    if not out.entries:
        raise HarError("the HAR contains no http(s) entries")
    _page_keys(out, raw_fields)
    _redirect_hops(out)
    return out


def _raw_headers(raw: Any) -> Dict[str, str]:
    """A HAR request's headers without its cookies (judged by name elsewhere)."""
    return {str(i.get("name")): str(i.get("value") or "") for i in raw or []
            if isinstance(i, dict) and i.get("name")
            and str(i.get("name")).lower() not in ("cookie", "set-cookie")}


def cred_never(raw: Any) -> bool:
    """An ``Authorization`` or ``Proxy-Authorization`` header: never a page key."""
    return any(isinstance(i, dict) and str(i.get("name") or "").lower()
               in ("authorization", "proxy-authorization") for i in raw or [])


def _page_keys(imported: HarImport, raw_fields: Dict[int, Dict[str, str]]) -> None:
    """A credential-bound entry whose every credential-named field an earlier body
    of the site states by name keeps ``page_keys`` and becomes a candidate; the
    stated values leave every body and URL (``page_literals.SHOWN``)."""
    if not raw_fields:
        return
    from anyuse.execution import page_literals as PL
    first_doc = next((e for e in imported.entries if "html" in (e.mime or "")), None)
    site = urllib.parse.urlsplit((first_doc or imported.entries[0]).url).hostname or ""
    found, secrets = PL.keyed_provenance(
        [(e.url, e.body, e.mime) for e in imported.entries], raw_fields, site)
    for i, keys in found.items():
        entry = imported.entries[i]
        entry.page_keys = keys
        entry.credential_bound = ""
        if entry.post_data:
            _u, _h, entry.post_data = PL.redact_fields(
                entry.url, {}, entry.post_data, [k["at"] for k in keys])
    if secrets:
        for entry in imported.entries:
            entry.body = PL.scrub(entry.body, secrets)
            entry.post_data = PL.scrub(entry.post_data, secrets)
            # another request may carry the key under a name that is not credential-like
            entry.url = PL.scrub(entry.url, secrets)
            entry.redirect_url = PL.scrub(entry.redirect_url, secrets)


def _redirect_hops(imported: HarImport) -> None:
    """Each request a recorded redirect led to keeps the hops (``redirected_from``)."""
    entries = imported.entries
    for i, entry in enumerate(entries):
        if not entry.redirect_url or not (entry.status and 300 <= int(entry.status) < 400):
            continue
        target = entry.redirect_url.split("#")[0]
        nxt = next((e for e in entries[i + 1:] if e.url.split("#")[0] == target), None)
        if nxt is not None and not nxt.redirected_from:
            nxt.redirected_from = (list(entry.redirected_from) + [entry.url])[-10:]


def load_har(path: str, capture_ua_for=None) -> HarImport:
    """Read and parse a HAR from disk, with the size cap applied first."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise HarError(f"cannot read {path}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise HarError(f"{path} is {size} bytes, over the {MAX_FILE_BYTES}-byte cap")
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_har(fh.read(), source=os.path.basename(path),
                         capture_ua_for=capture_ua_for)


# ---------------------------------------------------------------------------
# HAR -> recording
# ---------------------------------------------------------------------------

def har_to_recording(imported: HarImport, param_name: str, param_value: str,
                     expected: List[str], site: str = "") -> dict:
    """Build the minimal ``recording`` dict the compiler already consumes.

    ``expected`` is the user's answer to "what did the page show for this
    input?" — the ground truth. There is no way to get it from a HAR, which
    records traffic and not pixels, and guessing it would be the same mistake
    as a hand-written CSS selector. For an item's page it is the values the
    page showed; the proof names each one from the HAR's own response
    (``execution/field_names.py``).
    """
    network = [e.to_network() for e in imported.entries if not e.credential_bound]
    first_doc = next((e for e in imported.entries if "html" in (e.mime or "")), None)
    return {
        "param": {"name": param_name, "value": param_value},
        "ui": {"items": list(expected), "text": "\n".join(expected)},
        "final_url": first_doc.url if first_doc else (
            imported.entries[0].url if imported.entries else ""),
        "site": site or (urllib.parse.urlsplit(imported.entries[0].url).netloc
                         if imported.entries else ""),
        "network": network,
        "source": {"kind": "har", "file": imported.source,
                   "creator": imported.creator},
    }


@dataclass
class HarCase:
    """One HAR plus the input it was recorded with and what the user saw.

    A proof case may come without a HAR (``path`` empty): the user typed what the
    page showed for that input. The proof only needs the input and that ground
    truth; the first case, the one compiled from, always needs a HAR."""
    path: str = ""
    param_value: str = ""
    expected: List[str] = field(default_factory=list)


def typed_recording(param_name: str, param_value: str, expected: List[str]) -> dict:
    """A proof case the user typed: the input and what the page showed, no traffic.
    For an item's page the values are named by their order on the page."""
    return {"param": {"name": param_name, "value": param_value},
            "ui": {"items": list(expected), "text": "\n".join(expected)},
            "network": [], "source": {"kind": "typed"}}


_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def cookies_sent_with(template, imported: HarImport) -> List[str]:
    """The cookie names the browser sent with the request(s) ``template`` replays.

    A recorded request matches a template step on scheme, host, method and
    path, where a ``{name}`` placeholder in the template's path matches any
    text."""
    steps = getattr(template, "steps", None)
    templates = [step.template for step in steps] if steps else [template]
    names: List[str] = []
    for tmpl in templates:
        sp = urllib.parse.urlsplit(str(getattr(tmpl, "url", "") or ""))
        path = re.compile(".+?".join(re.escape(part)
                                     for part in _PLACEHOLDER.split(sp.path)))
        method = str(getattr(tmpl, "method", "") or "GET").upper()
        for entry in imported.with_cookies:
            ep = urllib.parse.urlsplit(entry.url)
            if ((ep.scheme, ep.netloc, entry.method) == (sp.scheme, sp.netloc, method)
                    and path.fullmatch(ep.path)):
                _add_cookie_names(names, entry.sent_cookies)
    return names


def sign_in_hint(cookie_names: List[str], held: Optional[dict] = None) -> str:
    """What to tell the user when a request recorded with cookies failed its proof.

    ``held`` (``host``, ``until``) is the do-not-auto-retry entry the proof's 403
    put on the host: until then nothing is sent there, the suggested attach
    included."""
    shown = ", ".join(cookie_names[:4]) + (", ..." if len(cookie_names) > 4 else "")
    wait = (f" The site answered 403, so AnyUse sends nothing more to {held['host']} "
            f"until {held['until']}; attach after that." if held else "")
    return (f"The request was recorded with cookies ({shown}). The proof sent it without "
            f"them, and it did not return what the page showed, so it probably needs you "
            f"to be signed in. A HAR cannot compile a signed-in search; attach with "
            f"--login-replay can (docs/SITE_MODES.md).{wait} If the search works signed "
            f"out, check the labels you gave.")


class _ProofAnswers:
    """The proof's executor, keeping what each replay got back, in order."""

    def __init__(self, executor):
        self.executor = executor
        self.results: List[dict] = []

    def execute(self, template, params, *args, _probe: bool = False, **kwargs):
        if _probe:
            # a made-up search that learns the site's empty page: not a proof replay
            from anyuse.execution.result_contract import probe_unsupported, takes_probe
            if not takes_probe(self.executor):
                return probe_unsupported()
            return self.executor.execute(template, params, *args, _probe=True, **kwargs)
        result = self.executor.execute(template, params, *args, **kwargs)
        self.results.append(result if isinstance(result, dict) else {})
        return result


def _may_need_cookies(result: dict) -> bool:
    """Whether a failed proof replay's answer can mean the request needs its cookies.

    It needs an answer: a refused connection, a timeout or a request that was
    never sent says nothing about cookies. A 5xx is the site failing, and a
    CAPTCHA, a challenge page or a rate limit asks for a person whatever the
    cookies; a bare 403 can be either, so it counts."""
    from anyuse.execution.blocked import FORBIDDEN, STICKY_REASONS
    try:
        status = int(result.get("status"))
    except (TypeError, ValueError):
        return False
    if status >= 500:
        return False
    reason = (result.get("blocked_state") or {}).get("reason") or ""
    return reason == FORBIDDEN or reason not in STICKY_REASONS


def _forbidden_hold(results: List[dict]) -> Optional[dict]:
    """The do-not-auto-retry entry a bare 403 among ``results`` put on its host."""
    from anyuse.execution.blocked import FORBIDDEN, blocked_until
    for result in results:
        if ((result.get("blocked_state") or {}).get("reason") == FORBIDDEN
                and result.get("status") is not None):
            host = urllib.parse.urlsplit(str(result.get("url") or "")).netloc
            entry = blocked_until(host) if host else None
            if entry:
                return {"host": host, "until": entry.get("until") or "later"}
    return None


def compile_from_har(cases: List[HarCase], param_name: str = "q",
                     min_cases: int = 2, levers=None, executor=None,
                     preflight=None, capture_ua_for=None, keep: Optional[dict] = None):
    """Compile from HARs and prove on the cases that were not compiled from.

    ``cases[0]`` is the recording; every later case is a fresh-input proof
    case. A search that opens a result (a chain) needs a second input to fit
    on (``api_verify.compile_and_verify_with_fit``). With three HARs it is
    generated from the site: another result's name from the first HAR's own
    search, sent with the chain's requests (``execution/chain_fit.py``), and
    every HAR after the first stays a proof case. When the site's search does
    not answer such a name, the second of four HARs fits it instead and is not
    a proof case. Returns ``(compile_result, proof, report)``;
    ``report["fitting"]`` names the HAR a chain was fitted on and
    ``report["generated_fitting"]`` the generated input. Nothing is persisted
    and nothing is sent except the proof's own replays and the generated
    input's requests, which go only to origins that appeared in the first HAR.
    ``keep``: receives the recordings by role (``recording``, ``fitting``, ``fresh``), for
    the tool's kept evidence (``evidence_store``); they hold no credential and cookies by
    name only.
    """
    from anyuse.execution.api_compile import page_chrome, volatile_evidence
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import (compile_and_verify_with_fit,
                                             permission_preflight)

    if len(cases) < 1 + min_cases:
        raise HarError(
            f"the fresh-input proof needs {min_cases} input value(s) that were not "
            f"compiled from: one HAR plus {min_cases} more cases, each another HAR "
            f"(--from-har/--har-param/--har-expect) or what the page showed for another "
            f"value (--expect VALUE='a|b|c'); {len(cases)} given, at least {1 + min_cases} needed")

    if not cases[0].path:
        raise HarError("the first case is the one compiled from, so it needs a HAR; "
                       "only the proof cases may be typed in")
    loaded = {i: load_har(c.path, capture_ua_for=capture_ua_for) for i, c in enumerate(cases) if c.path}
    imports = list(loaded.values())
    for i, case in enumerate(cases):
        if not case.expected:
            source = loaded[i].source if i in loaded else f"the typed case {case.param_value!r}"
            raise HarError(
                f"{source}: no expected results given. A HAR records traffic, "
                f"not what you saw; without the visible result list or item values "
                f"there is no ground truth and nothing can be proved.")
    recordings = [har_to_recording(loaded[i], param_name, c.param_value, c.expected) if i in loaded
                  else typed_recording(param_name, c.param_value, c.expected)
                  for i, c in enumerate(cases)]

    usable = [r for r in recordings if r["network"]]
    if not usable:
        blockers = sorted({e.credential_bound for imp in imports
                           for e in imp.credential_bound})
        raise HarError(
            "every request in this HAR is credential-bound, so there is nothing "
            "to compile: " + "; ".join(blockers[:3]) + ". A request that only "
            "works while you are signed in is out of scope — replaying it "
            "without the credential would not be safer, it would simply not "
            "work, and AnyUse does not store credentials.")

    allowed = imports[0].origins()
    def gate(compiled):
        template = compiled.template
        steps = getattr(template, "steps", None) if template is not None else None
        urls = ([step.template.url for step in steps] if steps else
                ([template.url] if template is not None else []))
        for url in urls:
            sp = urllib.parse.urlsplit(url)
            candidate_origin = f"{sp.scheme}://{sp.netloc}"
            if candidate_origin not in allowed:
                return (f"HAR origin check refused {candidate_origin}: it did not appear "
                        "in the first recording")
        if preflight is not None:
            return preflight(compiled)
        return permission_preflight(compiled, recordings[0], allowed_origins=allowed)
    answers = _ProofAnswers(executor or ApiStepExecutor())
    compiled, proof, fit = compile_and_verify_with_fit(
        recordings[0], recordings[1:], recordings[1:], min_cases=min_cases,
        executor=answers, levers=levers, preflight=gate, model_scope="compile")
    fitting = [cases[i].path for i, rec in enumerate(recordings) if rec is fit]
    if keep is not None:
        keep.update({"recording": recordings[0], "fitting": [fit] if fit is not None else [],
                     "fresh": [r for r in recordings[1:] if r is not fit]})
    if compiled.chain_needs_fitting:
        if fitting:
            # the fitting HAR shows no request that reads the first HAR's produced value
            hint = (f". {os.path.basename(fitting[0])} does not open a result the way the "
                    f"first HAR does, so it cannot fit the chain: export it again after "
                    f"opening the result")
        else:
            tried = (f". A value taken from the first HAR's own search results did not fit "
                     f"it ({compiled.chain_fit_note})" if compiled.chain_fit_note else "")
            hint = (f"{tried}. This search opens a result with a second request, so it needs "
                    f"{2 + min_cases} HARs of different values: the first is compiled from, "
                    f"the second fits the chain, and the others prove it. A typed --expect "
                    f"case cannot prove a chain: export another HAR and compile again")
        compiled.reason += hint
        proof.reason += hint
    report = {
        "source": [imp.summary() for imp in imports] + [
            {"kind": "typed", "value": c.param_value} for c in cases if not c.path],
        "fitting": fitting,
        **({"generated_fitting": dict(proof.chain_fit)} if proof.chain_fit else {}),
        "origins": imports[0].origins(),
        "credential_bound_requests": [
            {"url": e.url[:160], "why": e.credential_bound}
            for imp in imports for e in imp.credential_bound][:10],
        "cookie_requests": [
            {"url": e.url[:160], "cookies": e.sent_cookies[:6]}
            for imp in imports for e in imp.with_cookies][:10],
        "bodies_missing": [
            {"url": e.url[:160], "why": e.body_dropped}
            for imp in imports for e in imp.entries if e.body_dropped][:10],
    }
    if compiled.template is not None:
        url = getattr(compiled.template, "url", "") or ""
        sp = urllib.parse.urlsplit(url)
        origin = f"{sp.scheme}://{sp.netloc}"
        report["template_origin"] = origin
        report["origin_is_recorded"] = origin in imports[0].origins()
        if not report["origin_is_recorded"]:
            # Cannot happen by construction — every candidate comes from the
            # file — so if it ever does, refuse rather than reason about it.
            raise HarError(
                f"refusing a template for {origin}, which does not appear in the "
                f"imported HAR. This is a bug; please report it.")
        # The proof replayed without the cookies the browser sent. When the
        # site answered a case and the answer was wrong, the likeliest cause is
        # that the request needs them. The final validation is the last thing
        # that ran the executor, one replay per case.
        replies = answers.results[len(answers.results) - len(proof.cases):]
        failed = [reply for case, reply in zip(proof.cases, replies) if not case.passed]
        if not proof.passed and any(_may_need_cookies(reply) for reply in failed):
            names = cookies_sent_with(compiled.template, imports[0])
            if names:
                report["needs_sign_in"] = sign_in_hint(names, _forbidden_hold(failed))
    return compiled, proof, report
