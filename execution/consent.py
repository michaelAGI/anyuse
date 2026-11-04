"""Deterministic, reject-only cookie-consent handling for browser paths.

The safety property in this module is intentionally simple: automatic choices
have no ``accept`` operation.  A browser can click an exact reject/necessary-
only control, optionally after one exact settings control, or it can stop and
ask a person.  Remembering that a person chose ``accept`` never turns it into
an automatic click; it only records why the browser still needs a hand-off.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from anyuse.paths import anyuse_data_path

POLICY_ENV = "ANYUSE_CONSENT_POLICY"
REJECT_NONESSENTIAL = "reject_nonessential"
ASK = "ask"
NEVER = "never"
POLICIES = (REJECT_NONESSENTIAL, ASK, NEVER)

DECISIONS_FILE = "consent_decisions.json"
REJECT = "reject"
ACCEPT = "accept"
DECISIONS = (REJECT, ACCEPT)

# Exact, normalized labels.  Deliberately no substring matching: page prose
# such as "accept all or reject all" must never become a clickable target.
_REJECT_LABELS = frozenset({
    # English
    "reject", "reject all", "reject all cookies", "decline", "decline all",
    "only necessary", "necessary only", "necessary cookies only",
    "use necessary cookies only", "essential only", "essential cookies only",
    # German
    "alle ablehnen", "alles ablehnen", "ablehnen", "nur notwendige",
    "nur notwendige cookies", "nur erforderliche", "nur erforderliche cookies",
    # French
    "tout refuser", "refuser tout", "refuser", "uniquement necessaires",
    "cookies necessaires uniquement",
    # Spanish
    "rechazar todo", "rechazar todas", "rechazar", "solo necesarias",
    "solo las necesarias", "solo cookies necesarias",
    # Italian
    "rifiuta tutto", "rifiuta tutti", "rifiuta", "solo necessari",
    "solo necessarie", "solo cookie necessari",
    # Dutch
    "alles weigeren", "alles afwijzen", "weigeren", "alleen noodzakelijke",
    "alleen noodzakelijke cookies",
})

_SETTINGS_LABELS = frozenset({
    "cookie settings", "cookie preferences", "manage cookies", "manage preferences",
    "customize", "customise", "settings",
    "cookie einstellungen", "cookieeinstellungen", "einstellungen",
    "parametres des cookies", "gerer les cookies", "personnaliser",
    "configuracion de cookies", "gestionar cookies", "personalizar",
    "impostazioni cookie", "gestisci cookie", "personalizza",
    "cookie instellingen", "cookieinstellingen", "cookies beheren", "voorkeuren",
})

# Stable reject controls from widespread CMPs.  These selectors are also used
# by the browser-side candidate collector.  Nothing generic such as
# ``button.secondary`` belongs here.
VENDOR_REJECT_SELECTORS = (
    # OneTrust
    "#onetrust-reject-all-handler",
    # Cookiebot
    "#CybotCookiebotDialogBodyButtonDecline",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",
    # Didomi
    "#didomi-notice-disagree-button",
    "[data-testid='notice-reject-button']",
    # Usercentrics
    "#uc-btn-deny-banner",
    "[data-testid='uc-deny-all-button']",
    # Quantcast Choice
    "[data-testid='qc-cmp2-reject-all']",
    "button[aria-label='Reject All'].qc-cmp2-button",
    # TrustArc
    ".trustarc-decline-btn",
    "[data-testid='trustarc-reject-all']",
)

# An extra belt around the whitelist.  If a control's accessible label says
# accept/agree/allow in any supported language, even a vendor-looking element
# is refused.  This protects against stale/reused framework ids.
_ACCEPT_WORDS = re.compile(
    r"\b(?:accept\w*|agree\w*|allow\w*|consent\w*|akzeptier\w*|zustimm\w*|"
    r"erlaub\w*|accepter\w*|autoriser\w*|aceptar\w*|permitir\w*|accetta\w*|"
    r"consenti\w*|toestaan\w*|accepteren\w*|akkoord)\b", re.I)


def normalize_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


@dataclass(frozen=True)
class ConsentControl:
    index: int
    text: str = ""
    element_id: str = ""
    classes: tuple[str, ...] = ()
    vendor_reject: bool = False
    disabled: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ConsentControl":
        return cls(index=int(value.get("index", -1)), text=str(value.get("text") or ""),
                   element_id=str(value.get("id") or ""),
                   classes=tuple(str(x) for x in (value.get("classes") or [])),
                   vendor_reject=bool(value.get("vendor_reject")),
                   disabled=bool(value.get("disabled")))


@dataclass(frozen=True)
class ConsentChoice:
    action: str
    index: int
    label: str
    reason: str

    def step(self, *, layer: int = 1) -> Dict[str, Any]:
        return {"action": "click", "source": "consent_policy",
                "purpose": ("reject_nonessential_cookie_consent"
                            if self.action == REJECT else "open_cookie_settings"),
                "target": {"label": self.label[:100], "match": self.reason,
                           "layer": layer},
                "deterministic": True}


def _safe(control: ConsentControl) -> bool:
    return (control.index >= 0 and not control.disabled
            and not _ACCEPT_WORDS.search(normalize_label(control.text)))


def choose_reject(controls: Iterable[Mapping[str, Any] | ConsentControl]
                  ) -> Optional[ConsentChoice]:
    """Choose the first exact reject control in DOM order, or ``None``."""
    for raw in controls:
        control = raw if isinstance(raw, ConsentControl) else ConsentControl.from_mapping(raw)
        if not _safe(control):
            continue
        label = normalize_label(control.text)
        if control.vendor_reject:
            return ConsentChoice(REJECT, control.index, control.text or control.element_id,
                                 "vetted_framework_reject_selector")
        if label in _REJECT_LABELS:
            return ConsentChoice(REJECT, control.index, control.text,
                                 "exact_reject_or_necessary_only_label")
    return None


def choose_settings(controls: Iterable[Mapping[str, Any] | ConsentControl]
                    ) -> Optional[ConsentChoice]:
    """Choose one exact settings control for the permitted second layer."""
    for raw in controls:
        control = raw if isinstance(raw, ConsentControl) else ConsentControl.from_mapping(raw)
        if not _safe(control):
            continue
        if normalize_label(control.text) in _SETTINGS_LABELS:
            return ConsentChoice("settings", control.index, control.text,
                                 "exact_cookie_settings_label")
    return None


def consent_policy(explicit: Optional[str] = None) -> str:
    if explicit is not None:
        raw = str(explicit).strip().lower()
    else:
        try:
            from anyuse import settings
            raw = str(settings.get(POLICY_ENV) or "").strip().lower()
        except Exception:
            raw = str(os.environ.get(POLICY_ENV) or "").strip().lower()
    return raw if raw in POLICIES else REJECT_NONESSENTIAL


def decisions_path(path: Optional[str | os.PathLike[str]] = None) -> Path:
    return Path(path) if path is not None else anyuse_data_path(DECISIONS_FILE)


def _host(value: str) -> str:
    parsed = urllib.parse.urlsplit(value if "://" in value else "//" + value)
    return (parsed.hostname or "").rstrip(".").lower()


def load_decisions(path: Optional[str | os.PathLike[str]] = None) -> Dict[str, dict]:
    try:
        with open(decisions_path(path), encoding="utf-8") as fh:
            raw = json.load(fh)
        hosts = raw.get("hosts") if isinstance(raw, dict) else None
        if not isinstance(hosts, dict):
            return {}
        out: Dict[str, dict] = {}
        for host, entry in hosts.items():
            choice = str((entry or {}).get("choice") or "") if isinstance(entry, dict) else ""
            clean = _host(str(host))
            if clean and choice in DECISIONS:
                out[clean] = {"choice": choice,
                              "updated": str((entry or {}).get("updated") or "")}
        return out
    except (OSError, ValueError, TypeError):
        return {}


def remember_decision(host: str, choice: str,
                      path: Optional[str | os.PathLike[str]] = None,
                      *, clock=time.time) -> Path:
    clean, chosen = _host(host), str(choice).strip().lower()
    if not clean:
        raise ValueError("a consent decision needs a host")
    if chosen not in DECISIONS:
        raise ValueError("a consent decision must be accept or reject")
    target = decisions_path(path)
    hosts = load_decisions(target)
    hosts[clean] = {"choice": chosen,
                    "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".tmp-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema": "anyuse.consent_decisions/1", "hosts": hosts}, fh,
                      indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return target


def reset_decisions(host: str = "", path: Optional[str | os.PathLike[str]] = None) -> int:
    target = decisions_path(path)
    if not host:
        count = len(load_decisions(target))
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return count
    clean = _host(host)
    hosts = load_decisions(target)
    if clean not in hosts:
        return 0
    del hosts[clean]
    if not hosts:
        target.unlink(missing_ok=True)
        return 1
    # Re-use the atomic writer without manufacturing a second format.
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".tmp-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema": "anyuse.consent_decisions/1", "hosts": hosts}, fh,
                      indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return 1


def remembered_decision(host: str,
                        path: Optional[str | os.PathLike[str]] = None) -> str:
    return str(load_decisions(path).get(_host(host), {}).get("choice") or "")


def automatic_action(host: str, explicit_policy: Optional[str] = None,
                     decisions_file: Optional[str | os.PathLike[str]] = None) -> str:
    """Return ``reject``, ``ask`` or ``never``.  Never returns ``accept``."""
    policy = consent_policy(explicit_policy)
    if policy == NEVER:
        return NEVER
    if policy == REJECT_NONESSENTIAL:
        return REJECT
    return REJECT if remembered_decision(host, decisions_file) == REJECT else ASK


def handoff_question(host: str) -> str:
    return f"Accept or reject cookies on {_host(host) or 'the site'}?"


# Browser-side collection is limited to interactive controls.  It reports data
# only; Python makes the decision.  Visibility and disabled checks keep a
# hidden template button from being selected.
CONTROLS_JS = """(() => {
  const selectors = %s;
  document.querySelectorAll('[data-anyuse-consent-index]').forEach(
    n => n.removeAttribute('data-anyuse-consent-index'));
  const nodes = Array.from(document.querySelectorAll(
    'button, a[href], input[type=button], input[type=submit], [role=button]'));
  const out = [];
  for (const n of nodes) {
    const r = n.getBoundingClientRect();
    const style = getComputedStyle(n);
    if (!r.width || !r.height || style.visibility === 'hidden' || style.display === 'none') continue;
    const text = (n.getAttribute('aria-label') || n.value || n.innerText ||
                  n.textContent || n.getAttribute('title') || '').trim().slice(0, 160);
    out.push({index: out.length, text: text, id: n.id || '',
              classes: Array.from(n.classList || []),
              disabled: !!n.disabled || n.getAttribute('aria-disabled') === 'true',
              vendor_reject: selectors.some(s => { try { return n.matches(s); } catch (_) { return false; } })});
    n.setAttribute('data-anyuse-consent-index', String(out.length - 1));
  }
  return out;
})()""" % json.dumps(VENDOR_REJECT_SELECTORS)


def click_js(index: int) -> str:
    """A click expression for a previously inspected candidate index."""
    return ("(() => { const n = document.querySelector('[data-anyuse-consent-index=\""
            + str(int(index)) + "\"]'); if (!n || n.disabled || "
            "n.getAttribute('aria-disabled') === 'true') return false; "
            "n.scrollIntoView({block:'center'}); n.focus(); n.click(); return true; })()")


class _ControlParser(HTMLParser):
    """Small fixture helper; production candidates come from ``CONTROLS_JS``."""

    TAGS = frozenset({"button", "a", "input"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls: List[dict] = []
        self._active: Optional[dict] = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        role = str(values.get("role") or "").lower()
        typ = str(values.get("type") or "").lower()
        interactive = (tag == "button" or role == "button"
                       or tag == "a" and "href" in values
                       or tag == "input" and typ in ("button", "submit"))
        if not interactive:
            return
        classes = str(values.get("class") or "").split()
        text = values.get("aria-label") or values.get("value") or values.get("title") or ""
        vendor = _fixture_vendor_reject(str(values.get("id") or ""), classes, values)
        self._active = {"index": len(self.controls), "text": str(text),
                        "id": str(values.get("id") or ""), "classes": classes,
                        "disabled": "disabled" in values or values.get("aria-disabled") == "true",
                        "vendor_reject": vendor, "tag": tag, "parts": []}
        self.controls.append(self._active)

    def handle_data(self, data):
        if self._active is not None:
            self._active["parts"].append(data)

    def handle_endtag(self, tag):
        if self._active is not None and tag == self._active["tag"]:
            if not self._active["text"]:
                self._active["text"] = " ".join(self._active["parts"]).strip()
            self._active.pop("parts", None)
            self._active.pop("tag", None)
            self._active = None


def _fixture_vendor_reject(element_id: str, classes: List[str], attrs: Mapping[str, str]
                           ) -> bool:
    if element_id in {"onetrust-reject-all-handler", "CybotCookiebotDialogBodyButtonDecline",
                      "CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",
                      "didomi-notice-disagree-button", "uc-btn-deny-banner"}:
        return True
    if "trustarc-decline-btn" in classes:
        return True
    testid = attrs.get("data-testid")
    return testid in {"notice-reject-button", "uc-deny-all-button",
                      "qc-cmp2-reject-all", "trustarc-reject-all"}


def controls_from_html(markup: str) -> List[dict]:
    parser = _ControlParser()
    parser.feed(markup or "")
    return parser.controls
