"""Advisory phishing detection using the step recorder's surface context.

The recorder runs these deterministic checks every five seconds in an optional
background task, using the frontmost app, URL, window title and AX elements.
No screen frames or model calls are needed.

Checks:
  1. URL/host heuristics: punycode,
     raw-IP hosts, userinfo tricks (``https://paypal.com@evil.io``), brand
     names embedded in a NON-brand registrable domain (``paypal.com.evil.io``),
     lookalike domains (``paypa1.com``), suspicious TLDs, plain-HTTP logins.
  2. STRUCTURE heuristics — AX-derived: a login/password form present while
     the window title claims a brand the domain does not belong to; link text
     showing one URL while the link target is another (classic email phish).

ADVISORY, NEVER BLOCKING: a hit raises an alert (``kind=phishing_alert``),
posted only to ``ANYUSE_PHISHING_ALERT_URL`` when set, + an optional macOS notification.
Per-host cooldown prevents alert spam. Everything is best-effort and never
raises into the watcher loop.

Toggle: ``BatchWatcherConfig.phishing_guard`` (default False, matching the
repo's gates-default-off convention).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Brand → legitimate registrable domains. Deliberately small and high-value:
# these are the brands overwhelmingly imitated in credential phishing.
# ---------------------------------------------------------------------------

BRAND_DOMAINS: Dict[str, List[str]] = {
    "paypal": ["paypal.com", "paypal.me"],
    "google": ["google.com", "gmail.com", "youtube.com", "google.de"],
    "apple": ["apple.com", "icloud.com"],
    "microsoft": ["microsoft.com", "live.com", "office.com", "outlook.com",
                  "microsoftonline.com", "azure.com"],
    "amazon": ["amazon.com", "amazon.de", "aws.amazon.com"],
    "netflix": ["netflix.com"],
    "github": ["github.com"],
    "dropbox": ["dropbox.com"],
    "linkedin": ["linkedin.com"],
    "facebook": ["facebook.com", "meta.com"],
    "instagram": ["instagram.com"],
    "twitter": ["twitter.com", "x.com"],
    "binance": ["binance.com"],
    "coinbase": ["coinbase.com"],
    "steam": ["steampowered.com", "steamcommunity.com"],
    "dhl": ["dhl.com", "dhl.de"],
    "sparkasse": ["sparkasse.de"],
    "deutsche bank": ["deutsche-bank.de", "db.com"],
}

SUSPICIOUS_TLDS = (
    ".zip", ".mov", ".tk", ".ml", ".ga", ".cf", ".gq", ".top", ".icu",
    ".xyz", ".rest", ".cam", ".bar", ".click", ".link", ".work", ".quest",
)

LOGIN_HINTS = (
    "password", "passwort", "passcode", "sign in", "log in", "login",
    "anmelden", "verify your", "confirm your", "2fa", "one-time",
)


def _registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a public-suffix table: last two labels,
    or last three for common ccTLD second-levels (co.uk, com.au, ...)."""
    parts = host.lower().strip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    second_level = ".".join(parts[-2:])
    if second_level in ("co.uk", "com.au", "co.jp", "com.br", "co.in",
                        "com.mx", "co.nz", "com.tr", "com.cn"):
        return ".".join(parts[-3:])
    return second_level


def _levenshtein_leq1(a: str, b: str) -> bool:
    """True when edit distance(a, b) <= 1 (cheap check, no full DP table)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    # a is shorter or equal
    i = j = 0
    edited = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        if la == lb:
            i += 1
            j += 1
        else:
            j += 1  # skip one char in the longer string
    return True


@dataclass
class PhishingVerdict:
    suspicious: bool = False
    score: float = 0.0                 # 0..1, >= threshold ⇒ suspicious
    reasons: List[str] = field(default_factory=list)
    url: str = ""
    host: str = ""
    brand_guess: str = ""              # brand the page appears to claim

    def add(self, weight: float, reason: str):
        self.score = min(1.0, self.score + weight)
        self.reasons.append(reason)


class _NoAlertURL(Exception):
    pass


class PhishingGuard:
    """Deterministic phishing checks over URL, title and AX element context."""

    SUSPICIOUS_THRESHOLD = 0.5
    ALERT_COOLDOWN_S = 600.0  # one alert per host per 10 min

    def __init__(self, alert_url: Optional[str] = None, notify: bool = True):
        # An alert is posted only to a URL the user configured; otherwise it stays local.
        self._alert_url = (alert_url if alert_url is not None
                           else os.environ.get("ANYUSE_PHISHING_ALERT_URL", "")).strip()
        self._notify = notify
        self._last_alert_by_host: Dict[str, float] = {}
        # Advisory counters.
        self.checks = 0
        self.alerts = 0

    # -- tier 1+2: deterministic ------------------------------------------

    def check(
        self,
        url: str = "",
        window_title: str = "",
        ax_elements: Optional[List[Dict[str, Any]]] = None,
    ) -> PhishingVerdict:
        """Never raises. Empty/parse-fail inputs yield a benign verdict."""
        v = PhishingVerdict(url=url or "")
        try:
            self.checks += 1
            self._check_url(v, url or "")
            self._check_structure(v, window_title or "", ax_elements or [])
            v.suspicious = v.score >= self.SUSPICIOUS_THRESHOLD
        except Exception as e:  # noqa: BLE001 — advisory path, never crash
            logger.debug("phishing check failed: %s", e)
        return v

    def _check_url(self, v: PhishingVerdict, url: str) -> None:
        if not url:
            return
        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
        except ValueError:
            return
        host = (parsed.hostname or "").lower()
        if not host:
            return
        v.host = host
        reg = _registrable_domain(host)

        # Userinfo trick: https://paypal.com@evil.io — the browser goes to
        # evil.io while the URL reads like paypal.
        if "@" in (parsed.netloc or ""):
            v.add(0.6, "userinfo_in_url")

        # Raw IP host serving a page.
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
            v.add(0.5, "raw_ip_host")

        # Punycode / homoglyph carrier.
        if any(label.startswith("xn--") for label in host.split(".")):
            v.add(0.5, "punycode_host")

        # Suspicious TLD.
        if any(reg.endswith(tld) for tld in SUSPICIOUS_TLDS):
            v.add(0.25, f"suspicious_tld:{reg.rsplit('.', 1)[-1]}")

        # Brand-in-hostname but NOT the brand's registrable domain:
        # paypal.com.secure-verify.icu, login-paypal.evil.io, paypa1.com.
        for brand, domains in BRAND_DOMAINS.items():
            brand_key = brand.replace(" ", "")
            legit = reg in domains or any(
                reg == d or host.endswith("." + d) for d in domains)
            if legit:
                break
            hit = brand_key in host.replace("-", "").replace(".", "")
            if not hit:
                # Lookalike on the registrable domain's first label.
                first_label = reg.split(".")[0]
                if (len(brand_key) >= 5
                        and _levenshtein_leq1(first_label, brand_key)):
                    v.add(0.6, f"lookalike_domain:{brand}")
                    v.brand_guess = brand
                    break
                continue
            v.add(0.6, f"brand_in_foreign_host:{brand}")
            v.brand_guess = brand
            break

        # Credential-ish path over plain HTTP.
        if parsed.scheme == "http" and any(
                h in (parsed.path or "").lower() for h in ("login", "signin",
                                                           "verify", "account")):
            v.add(0.35, "http_credential_path")

    def _check_structure(
        self,
        v: PhishingVerdict,
        window_title: str,
        ax_elements: List[Dict[str, Any]],
    ) -> None:
        """Tier 2: title/domain mismatches and credential or link structure."""
        title = window_title.lower()
        has_secure_field = any(
            (el.get("role") == "AXSecureTextField") for el in ax_elements)
        has_login_words = any(h in title for h in LOGIN_HINTS) or any(
            h in str(el.get("title", "")).lower()
            or h in str(el.get("description", "")).lower()
            for el in ax_elements for h in LOGIN_HINTS
        )
        has_form = has_secure_field or has_login_words

        # Title claims a brand the domain doesn't belong to, on a page with a
        # credential form ⇒ strong signal.
        if has_form and v.host:
            reg = _registrable_domain(v.host)
            for brand, domains in BRAND_DOMAINS.items():
                if brand in title and reg not in domains and not any(
                        v.host.endswith("." + d) for d in domains):
                    v.add(0.5, f"title_brand_domain_mismatch:{brand}")
                    v.brand_guess = v.brand_guess or brand
                    break

        # Link text displaying one host while the target is another
        # (AX link elements expose their URL in some browsers/mail apps).
        for el in ax_elements:
            if el.get("role") not in ("AXLink", "AXStaticText"):
                continue
            shown = str(el.get("title") or el.get("description") or "")
            target = str(el.get("url") or el.get("value") or "")
            shown_host = self._host_in_text(shown)
            if not (shown_host and target):
                continue
            try:
                target_host = (urlparse(target).hostname or "").lower()
            except ValueError:
                continue
            if target_host and _registrable_domain(shown_host) != \
                    _registrable_domain(target_host):
                v.add(0.6, f"link_text_target_mismatch:{shown_host}->{target_host}")
                break

        # A secure field on an already-somewhat-suspicious URL compounds.
        if has_secure_field and v.score > 0:
            v.add(0.2, "credential_form_on_suspicious_url")

    @staticmethod
    def _host_in_text(text: str) -> str:
        m = re.search(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", text.lower())
        return m.group(1) if m else ""

    # -- alerting ------------------------------------------------------------

    def alert(self, v: PhishingVerdict) -> Dict[str, Any]:
        """Raise the advisory alert (optional webhook + optional macOS notification).
        Cooldown is per host. Never raises; returns a result dict."""
        host = v.host or "unknown"
        now = time.time()
        last = self._last_alert_by_host.get(host, 0.0)
        if now - last < self.ALERT_COOLDOWN_S:
            return {"ok": False, "skipped": True, "reason": "cooldown"}
        self._last_alert_by_host[host] = now
        self.alerts += 1

        summary = (f"Possible phishing page: {host} "
                   f"(score {v.score:.2f}; {', '.join(v.reasons[:4])})")
        payload = {
            "source": "anyuse.watcher.phishing_guard",
            "kind": "phishing_alert",
            "summary": summary,
            "entities": [{
                "url": v.url, "host": host, "score": round(v.score, 2),
                "reasons": v.reasons, "brand_guess": v.brand_guess,
            }],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z") or str(now),
        }
        result: Dict[str, Any] = {"ok": False, "skipped": True, "reason": "no alert URL configured"}
        try:
            if not self._alert_url:
                raise _NoAlertURL
            req = urllib.request.Request(
                self._alert_url,
                data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = {"ok": True,
                          "status": getattr(resp, "status", None) or resp.getcode()}
        except _NoAlertURL:
            pass
        except Exception as e:  # noqa: BLE001 — an offline webhook is fine
            result = {"ok": False, "error": str(e)}

        if self._notify:
            self._macos_notify(summary)
        return result

    @staticmethod
    def _macos_notify(text: str) -> None:
        try:
            import subprocess
            subprocess.run(
                ["osascript", "-e",
                 'display notification "{}" with title "anyuse watcher"'.format(
                     text.replace('"', "'"))],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
