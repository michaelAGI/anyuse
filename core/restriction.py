"""
Restriction policy + compliance enforcement for AnyUse desktop actions.

This module turns an orchestrator's BOUNDARY object into a locally-enforced policy.
An orchestrator may send a ``boundary`` object with every ``desktop_execute`` call:

    {
        "list_mode": "blocklist" | "allowlist",
        "allowed_domains": ["*.example.com", "stripe.com"],
        "blocked_domains": ["*.evil.com"],
        "allowed_apps": ["Safari", "Mail"],
        "blocked_apps": ["Terminal"],
        "max_actions": 500,
        "action_rules": {"click_at": "exclude"},
        "confirmation_behavior": "selective",
        "safety_instructions": "...",
    }

``RestrictionPolicy`` owns the local allowlist/blocklist decision for *domains*
(navigation targets), *apps*, action rules and a run's action allowance.
An action requiring an unavailable guard evaluator is refused.

Design constraints
------------------
* **Default-safe / additive.** When no boundary is configured the policy must
  be *permissive* so existing flows are unchanged. Enforcement only kicks in
  when a caller actually passes a boundary.
* **Suffix-bypass resistant.** A ``*.example.com`` pattern must match
  ``sub.example.com`` but MUST NOT match ``evil-example.com.attacker.com`` or
  ``notexample.com``. Matching is done on DNS *label* boundaries, never on raw
  substrings.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: normalization
# ---------------------------------------------------------------------------

def _canonicalize_ipv4(host: str) -> str:
    """Canonicalize an IPv4 literal (decimal/hex/octal integer or dotted form)
    to its dotted-quad string.

    Browsers (and the C ``inet_aton`` family) accept many surprising IPv4
    spellings: a bare decimal integer (``3232235777``), a hex integer
    (``0xc0a80101``), octal (``0300.0250.01.01``), and dword/part-count
    variants. A blocklist entry of ``192.168.1.1`` is trivially bypassed by
    one of those alternate spellings unless we canonicalize first.

    Returns the dotted-quad string if ``host`` is recognizably an IPv4 literal,
    else the original ``host`` unchanged (so real DNS names pass through).
    """
    if not host:
        return host

    # Already a clean dotted-quad? Round-trip through ipaddress to canonicalize
    # (also folds e.g. octal-looking dotted parts the int() path catches below).
    # First: a single integer literal (decimal / 0x hex / 0o or leading-zero
    # octal). int(host, 0) parses base prefixes; bare leading-zero octals like
    # "030052000401" are handled by the explicit octal retry.
    candidate = host
    try:
        as_int = int(candidate, 0)
    except (ValueError, TypeError):
        # Not a single integer. Try a leading-zero (C-style) octal integer
        # that int(.., 0) rejects in Python 3 ("030..." needs 0o prefix).
        as_int = None
        if re.fullmatch(r"0[0-7]+", candidate):
            try:
                as_int = int(candidate, 8)
            except ValueError:
                as_int = None
    else:
        # int(host, 0) also accepts plain "0" and would treat "0" as valid.
        # Guard the integer range below regardless.
        pass

    if as_int is not None:
        try:
            if 0 <= as_int <= 0xFFFFFFFF:
                return str(ipaddress.IPv4Address(as_int))
        except (ipaddress.AddressValueError, ValueError):
            pass
        # Out-of-range integer is not a valid IPv4 literal; fall through.

    # Dotted form with possibly hex/octal components, e.g. "0xc0.0xa8.1.1" or
    # "0300.0250.01.01". Treat as IPv4 only if every part is an integer literal.
    if "." in candidate:
        parts = candidate.split(".")
        if 2 <= len(parts) <= 4 and all(parts):
            ints: List[int] = []
            ok = True
            for p in parts:
                try:
                    n = int(p, 0)
                except (ValueError, TypeError):
                    if re.fullmatch(r"0[0-7]+", p):
                        n = int(p, 8)
                    else:
                        ok = False
                        break
                ints.append(n)
            if ok and 2 <= len(parts) <= 4:
                # inet_aton dword semantics: the leading parts are single octets
                # (<=255); the LAST part absorbs the remaining low octets
                # (a.b.c -> c is 16-bit, a.b -> b is 24-bit). Browsers/libc accept
                # these short forms, so canonicalize them too (not just 4-part) —
                # else a blocked 192.168.1.1 is reachable via 192.11010049 / 192.168.257.
                leads, last = ints[:-1], ints[-1]
                last_octets = 4 - len(leads)
                if all(0 <= x <= 0xFF for x in leads) and 0 <= last < (1 << (8 * last_octets)):
                    value = last
                    for i, x in enumerate(leads):
                        value |= x << (8 * (3 - i))
                    try:
                        return str(ipaddress.IPv4Address(value))
                    except (ipaddress.AddressValueError, ValueError):
                        pass

    return host


def _idna_host(host: str) -> str:
    """NFC-normalize then IDNA-encode ``host`` to its punycode ASCII form.

    Applied identically to policy patterns and input hosts so a Unicode IDN
    (``bücher.de``) and its punycode spelling (``xn--bcher-kva.de``) compare
    equal — closing the IDN/punycode confusable bypass.

    Per-label so that ``*.`` wildcards and IP literals pass through. Any label
    we cannot encode is left as-is rather than dropped; a host that is wholly
    un-encodable (raises) falls back to its NFC form. Encoding is guarded so a
    malformed IDN never crashes the security check — fail-closed by leaving the
    value in a form that simply won't match an allow rule.
    """
    if not host:
        return host
    try:
        nfc = unicodedata.normalize("NFC", host)
    except (TypeError, ValueError):
        return host

    out_labels: List[str] = []
    for label in nfc.split("."):
        if not label or label == "*" or label.isascii():
            # Empty (trailing dot), wildcard, or already-ASCII labels need no
            # IDNA transform.
            out_labels.append(label)
            continue
        try:
            out_labels.append(label.encode("idna").decode("ascii"))
        except (UnicodeError, ValueError):
            # Un-encodable label: keep the NFC form. It will not match an
            # ASCII punycode allow-rule, which is the fail-closed outcome.
            out_labels.append(label)
    return ".".join(out_labels)


def _normalize_host(value: str) -> str:
    """Reduce a URL or bare host to a lowercase, dot-trimmed hostname.

    Accepts full URLs (``https://Sub.Example.com:443/path``), scheme-less
    hosts (``Example.com``), and host:port. Returns ``""`` for input we can't
    interpret as a host (so the caller can decide how to treat the unknown).

    Security normalizations (all applied so policy entries and input hosts are
    compared on the *same* canonical form):

    * **WHATWG backslash + userinfo handling.** Browsers treat ``\\`` as a path
      separator in the authority and discard any ``userinfo@`` prefix. Python's
      ``urlparse`` does neither, so ``https://evil.com\\@allowed.com`` parses to
      host ``allowed.com`` while a browser navigates to ``evil.com``. We replace
      backslashes with forward slashes and strip everything up to and including
      the last ``@`` in the authority before parsing.
    * **IPv4 literal canonicalization** (decimal/hex/octal → dotted-quad).
    * **IDNA / NFC normalization** to punycode ASCII.
    """
    if not value or not isinstance(value, str):
        return ""

    raw = value.strip()
    if not raw:
        return ""

    # WHATWG URL parsing treats backslashes in the authority as forward
    # slashes; Python's urlparse does not. Normalize first so the authority
    # boundary matches browser behavior. (Done on the whole string — a literal
    # backslash never legitimately appears in a host or path we care about.)
    raw = raw.replace("\\", "/")

    # If it looks like a URL (has a scheme or starts with //), parse it.
    host = ""
    if "://" in raw or raw.startswith("//"):
        parsed = urlparse(raw if "://" in raw else "http:" + raw)
        # urlparse already drops userinfo into .username, exposing only the
        # real host via .hostname — that is exactly the browser-visible host.
        host = parsed.hostname or ""
    else:
        # Could be "host", "host:port", "userinfo@host", or "host/path".
        candidate = raw.split("/", 1)[0]
        # Strip any userinfo prefix (everything up to and including the last
        # '@'), mirroring how a browser resolves the authority. This closes the
        # "evil.com@allowed.com" style trick on scheme-less inputs too.
        if "@" in candidate:
            candidate = candidate.rsplit("@", 1)[1]
        # Strip a :port suffix (but leave IPv6 brackets/colons alone — we
        # don't expect IPv6 literals in boundary rules, and treating them as
        # hosts verbatim is the safe conservative choice).
        if candidate.count(":") == 1:
            candidate = candidate.split(":", 1)[0]
        host = candidate

    host = host.strip().lower().rstrip(".")
    if not host:
        return ""

    # Canonicalize alternate IPv4 spellings to dotted-quad so a blocked
    # 192.168.1.1 can't be reached via 3232235777 / 0xc0a80101 / octal.
    host = _canonicalize_ipv4(host)

    # NFC + IDNA so Unicode IDNs and their punycode form compare equal.
    host = _idna_host(host)

    return host


def _normalize_app(value: str) -> str:
    """Normalize a macOS app name for comparison.

    Strips a trailing ``.app``, lowercases, and collapses surrounding
    whitespace so ``"Google Chrome.app"`` and ``"google chrome"`` compare
    equal. Bundle-id style values (``com.apple.Safari``) are lowercased as-is.
    """
    if not value or not isinstance(value, str):
        return ""
    name = value.strip()
    if name.lower().endswith(".app"):
        name = name[: -len(".app")]
    return name.strip().lower()


# ---------------------------------------------------------------------------
# Domain matching (suffix-bypass resistant)
# ---------------------------------------------------------------------------

def domain_matches(pattern: str, host: str) -> bool:
    """Return True if ``host`` matches ``pattern`` on DNS label boundaries.

    Supported pattern forms:

    * ``example.com``      — exact match. Also matches subdomains
      (``sub.example.com``) because a bare domain in a boundary list is
      universally read as "this domain and everything under it".
    * ``*.example.com``    — any *single-or-multi* label subdomain
      (``a.example.com``, ``a.b.example.com``) but **not** the apex
      ``example.com`` itself, and **not** ``evil-example.com.attacker.com``.
    * ``*``                — matches everything (wildcard catch-all).
    * ``*example.com``     — partial-label globs are rejected as a match to
      avoid the classic suffix bypass; only full-label ``*.`` wildcards work.

    The function never does a raw substring/``endswith`` check, which is the
    bug that lets ``evil-example.com.attacker.com`` slip past a naive
    ``host.endswith("example.com")``.
    """
    pat = _normalize_host(pattern) if pattern else ""
    # _normalize_host strips a leading "*." (urlparse-safe), so handle the
    # wildcard explicitly off the raw pattern instead.
    raw = (pattern or "").strip().lower().rstrip(".")
    h = _normalize_host(host)

    if not raw or not h:
        return False

    # Catch-all
    if raw == "*":
        return True

    # Wildcard subdomain pattern: must be exactly "*." + base, where base is a
    # real domain (label boundary enforced by the leading dot we require).
    if raw.startswith("*."):
        base = _normalize_host(raw[2:].rstrip("."))
        if not base:
            return False
        # host must END WITH ".base" — the leading dot guarantees we only
        # match on a label boundary. "evil-example.com.attacker.com" with
        # base "example.com" -> we require host to end with ".example.com",
        # which "...attacker.com" does not. Apex "example.com" also fails
        # (no leading-dot prefix), matching DNS wildcard semantics.
        return h.endswith("." + base)

    # Reject any other glob (e.g. "*example.com", "ex*.com") as a *match* —
    # we do not support partial-label wildcards because they invite bypasses.
    if "*" in raw:
        return False

    # Exact domain: match the apex itself OR any subdomain of it, both on
    # label boundaries.
    base = pat or raw
    if h == base:
        return True
    return h.endswith("." + base)


# ---------------------------------------------------------------------------
# RestrictionPolicy
# ---------------------------------------------------------------------------

@dataclass
class RestrictionPolicy:
    """A locally-enforceable allow/block policy for domains and apps.

    Attributes:
        list_mode: ``"allowlist"`` or ``"blocklist"``. In allowlist mode a
            target must match the allowed list to be permitted; in blocklist
            mode a target is permitted unless it matches the blocked list.
        allowed_domains / blocked_domains: domain glob patterns.
        allowed_apps / blocked_apps: macOS app names (``.app`` optional).
        default_deny: when True and *no list constrains a target type*, the
            target is denied rather than allowed. Only meaningful for
            allowlist mode; ignored for blocklist mode where the default is
            inherently "allow unless blocked".
        active: whether this policy enforces anything at all. A policy parsed
            from an empty/absent boundary is inactive → fully permissive,
            preserving existing behavior.
    """

    list_mode: str = "blocklist"
    allowed_domains: List[str] = field(default_factory=list)
    blocked_domains: List[str] = field(default_factory=list)
    allowed_apps: List[str] = field(default_factory=list)
    blocked_apps: List[str] = field(default_factory=list)
    default_deny: bool = False
    active: bool = True
    action_rules: Dict[str, str] = field(default_factory=dict)
    max_actions: Optional[int] = None

    # ---- construction --------------------------------------------------

    @classmethod
    def permissive(cls) -> "RestrictionPolicy":
        """A no-op policy that allows everything (existing behavior)."""
        return cls(list_mode="blocklist", active=False)

    @classmethod
    def from_boundary(cls, boundary_config: Optional[Dict[str, Any]]) -> "RestrictionPolicy":
        """Build a policy from an orchestrator BOUNDARY dict (tolerant parse).

        Unknown keys are ignored; missing keys fall back to safe defaults;
        scalar values where a list is expected are coerced to a one-element
        list. An empty or ``None`` boundary yields a permissive (inactive)
        policy so that callers with no boundary keep their current behavior.
        """
        if not boundary_config or not isinstance(boundary_config, dict):
            return cls.permissive()

        def _as_list(key: str) -> List[str]:
            val = boundary_config.get(key, [])
            if val is None:
                return []
            if isinstance(val, str):
                return [val] if val.strip() else []
            if isinstance(val, (list, tuple, set)):
                return [str(v).strip() for v in val if str(v).strip()]
            return []

        raw_mode = boundary_config.get("list_mode", "blocklist")
        mode = str(raw_mode).strip().lower() if raw_mode else "blocklist"
        # Track whether the mode was an absent/empty default vs. an explicit but
        # unrecognized value — the latter must FAIL CLOSED, not fall through to a
        # permissive blocklist (an intended allowlist would otherwise leak).
        mode_unrecognized = False
        if mode not in ("allowlist", "blocklist"):
            if mode in ("allow", "whitelist"):
                mode = "allowlist"
            elif mode in ("block", "deny", "blacklist"):
                mode = "blocklist"
            elif not raw_mode or not str(raw_mode).strip():
                # Absent / empty list_mode → safe-for-existing-flows default.
                mode = "blocklist"
            else:
                # Explicit but unrecognized token (e.g. "allow-list",
                # "permit", "open"). SECURITY: do not silently downgrade to a
                # permissive blocklist — a config author who meant to restrict
                # would have their policy quietly disabled. Fail closed by
                # treating it as a deny-by-default allowlist.
                mode = "allowlist"
                mode_unrecognized = True
                logger.warning(
                    "Unrecognized boundary list_mode %r — failing closed to "
                    "allowlist + default_deny", raw_mode,
                )

        allowed_domains = _as_list("allowed_domains")
        blocked_domains = _as_list("blocked_domains")
        allowed_apps = _as_list("allowed_apps")
        blocked_apps = _as_list("blocked_apps")
        rules = boundary_config.get("action_rules", {})
        if not isinstance(rules, dict) or any(
            not isinstance(k, str) or not k.strip() or v not in ("allow", "guard", "exclude")
            for k, v in rules.items()
        ):
            raise ValueError("boundary action_rules must map action names to allow, guard or exclude")
        rules = {k.strip().lower(): v for k, v in rules.items()}
        maximum = boundary_config.get("max_actions")
        if maximum is not None and (type(maximum) is not int or maximum < 0):
            raise ValueError("boundary max_actions must be a non-negative integer")

        # default_deny: honor an explicit flag if present, otherwise infer.
        # In allowlist mode the natural default is deny-by-default.
        if "default_deny" in boundary_config and not mode_unrecognized:
            default_deny = bool(boundary_config.get("default_deny"))
        else:
            # Unrecognized mode forces deny-by-default regardless of any flag,
            # so a malformed boundary can never become permissive.
            default_deny = mode == "allowlist"

        # A boundary that names no domains and no apps in either direction is
        # effectively empty — treat as permissive UNLESS it explicitly asked
        # for allowlist+default_deny (a deliberate "deny everything" stance).
        has_any_rule = bool(
            allowed_domains or blocked_domains or allowed_apps or blocked_apps
        )
        active = has_any_rule or bool(rules) or maximum is not None or (mode == "allowlist" and default_deny)

        return cls(
            list_mode=mode,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            allowed_apps=allowed_apps,
            blocked_apps=blocked_apps,
            default_deny=default_deny,
            active=active,
            action_rules=rules,
            max_actions=maximum,
        )

    @classmethod
    def from_settings(cls, env: Optional[Dict[str, str]] = None) -> "RestrictionPolicy":
        """The local runner's own boundary for app switches (the draft replay and fill): the settings ``ANYUSE_APP_BLOCKLIST`` and
        ``ANYUSE_APP_ALLOWLIST`` (comma-separated bundle ids or app names). An allowlist makes
        the policy an allowlist; neither set is the permissive policy, as before."""
        from anyuse.settings import get as get_setting

        def _items(name: str) -> List[str]:
            raw = get_setting(name, env=env) if env is not None else get_setting(name)
            return [x.strip() for x in str(raw or "").split(",") if x.strip()]
        blocked, allowed = _items("ANYUSE_APP_BLOCKLIST"), _items("ANYUSE_APP_ALLOWLIST")
        if not blocked and not allowed:
            return cls.permissive()
        return cls.from_boundary({"list_mode": "allowlist" if allowed else "blocklist",
                                  "blocked_apps": blocked, "allowed_apps": allowed})

    # ---- domain checks -------------------------------------------------

    def _domain_in(self, host: str, patterns: List[str]) -> Optional[str]:
        """Return the first pattern that matches ``host``, else None."""
        for p in patterns:
            if domain_matches(p, host):
                return p
        return None

    def is_domain_allowed(self, host: str) -> Tuple[bool, str]:
        """Decide if a navigation target host is permitted.

        Returns ``(allowed, reason)``. When the policy is inactive everything
        is allowed (existing behavior preserved).
        """
        if not self.active:
            return True, "No boundary policy active"

        norm = _normalize_host(host)
        if not norm:
            # We couldn't interpret the target as a host. Be conservative
            # only when default_deny is set; otherwise allow (can't restrict
            # what we can't identify, and blocklist mode is allow-by-default).
            if self.default_deny:
                return False, f"Unparseable navigation target '{host}' denied (default-deny)"
            return True, f"Unparseable navigation target '{host}' — no restriction applied"

        # Blocklist always wins as an explicit deny, even in allowlist mode.
        blocked = self._domain_in(norm, self.blocked_domains)
        if blocked:
            return False, f"Domain '{norm}' blocked by rule '{blocked}'"

        if self.list_mode == "allowlist":
            allowed = self._domain_in(norm, self.allowed_domains)
            if allowed:
                return True, f"Domain '{norm}' allowed by rule '{allowed}'"
            # Not on the allowlist.
            if self.allowed_domains or self.default_deny:
                return False, f"Domain '{norm}' not in allowlist"
            # Allowlist mode but no domain allowlist configured and not
            # default-deny → nothing constrains domains, allow.
            return True, f"Domain '{norm}' allowed (no domain allowlist configured)"

        # blocklist mode: allowed because it wasn't blocked above.
        return True, f"Domain '{norm}' allowed (not in blocklist)"

    # ---- app checks ----------------------------------------------------

    def _app_in(self, app: str, names: List[str]) -> Optional[str]:
        target = _normalize_app(app)
        for n in names:
            if _normalize_app(n) == target:
                return n
        return None

    def is_app_allowed(self, app: str) -> Tuple[bool, str]:
        """Decide if launching/using a macOS app is permitted.

        Returns ``(allowed, reason)``. Inactive policy → allow everything.
        """
        if not self.active:
            return True, "No boundary policy active"

        norm = _normalize_app(app)
        if not norm:
            if self.default_deny:
                return False, f"Unidentified app target denied (default-deny)"
            return True, "Unidentified app target — no restriction applied"

        blocked = self._app_in(app, self.blocked_apps)
        if blocked:
            return False, f"App '{app}' blocked by rule '{blocked}'"

        if self.list_mode == "allowlist":
            allowed = self._app_in(app, self.allowed_apps)
            if allowed:
                return True, f"App '{app}' allowed by rule '{allowed}'"
            if self.allowed_apps or self.default_deny:
                return False, f"App '{app}' not in allowlist"
            return True, f"App '{app}' allowed (no app allowlist configured)"

        return True, f"App '{app}' allowed (not in blocklist)"

    def is_app_target_allowed(self, bundle_id: str, name: str = "") -> Tuple[bool, str]:
        """An app named by its bundle id and, as an extra, its localized name: blocked when
        either matches a blocklist entry (the bundle id first); under an allowlist allowed when
        either matches an allowlist entry. A name alone never unblocks a blocked bundle id."""
        if not self.active:
            return True, "No boundary policy active"
        for target in (bundle_id, name):
            if target:
                hit = self._app_in(target, self.blocked_apps)
                if hit:
                    return False, f"App '{target}' blocked by rule '{hit}'"
        if self.list_mode == "allowlist":
            for target in (bundle_id, name):
                if target and self._app_in(target, self.allowed_apps):
                    return True, f"App '{target}' allowed by the allowlist"
            if self.allowed_apps or self.default_deny:
                return False, f"App '{bundle_id or name}' not in allowlist"
        return True, f"App '{bundle_id or name}' allowed"


# ---------------------------------------------------------------------------
# ComplianceChecker
# ---------------------------------------------------------------------------

# Action names that carry a navigation/URL target.
_NAV_ACTIONS = {
    "navigate",
    "open_url",
    "goto",
    "go_to_url",
    "open_web_browser",
    "browse",
}

# Action names that carry an app-launch target.
_APP_ACTIONS = {
    "open_app",
    "launch_app",
    "open_application",
    "activate_app",
    "switch_app",
}

# Keys we look in (in priority order) to find a URL on an action.
_URL_KEYS = ("url", "href", "link", "address", "target_url")
# Keys we look in to find an app name on an action.
_APP_KEYS = ("app", "app_name", "application", "bundle_id", "target_app", "name")

# Action names that can navigate the page/system but carry their target inside
# an opaque payload (a script, a key sequence, a shell command) rather than a
# clean URL field. When a policy is active these are treated cautiously: if we
# can pull a URL/host out of their string args we check it, and if we can't but
# they could still navigate, we deny (fail-closed) rather than wave them
# through. This is the defense-in-depth backstop for action types we don't
# model explicitly (e.g. {"action":"javascript","args":{"script":"..."}}).
_SCRIPTING_ACTIONS = {
    "javascript",
    "js",
    "eval",
    "evaluate",
    "execute_script",
    "exec_script",
    "run_script",
    "run_js",
    "inject_script",
    "applescript",
    "osascript",
    "shell",
    "exec",
    "execute",
    "run_command",
    "command",
}

# Benign actions that never navigate and have no domain/app target. They are
# allowed even under an active policy and are NOT subjected to the cautious
# scripting-action deny, even if a stray string value happens to contain a URL
# substring that doesn't actually navigate anywhere.
_BENIGN_ACTIONS = {
    "click",
    "click_at",
    "double_click",
    "right_click",
    "type",
    "type_text",
    "key",
    "key_press",
    "press_key",
    "hotkey",
    "scroll",
    "scroll_at",
    "move",
    "move_mouse",
    "drag",
    "wait",
    "screenshot",
    "noop",
}

# Match an absolute / scheme-relative URL embedded in an arbitrary string value
# (e.g. inside a JS snippet: location.href='https://evil.com'). We deliberately
# require a scheme or a leading "//" here rather than also matching bare dotted
# tokens: arbitrary script bodies are full of dotted identifiers
# (``document.title``, ``window.location``) that are NOT navigation targets, and
# matching them produces noisy false positives. A genuinely opaque navigator
# that yields no URL is instead caught by the scripting-action fail-closed path.
# The extracted candidate is re-validated via _normalize_host / is_domain_allowed
# downstream, so a false positive only costs a redundant (still-correct) check.
_EMBEDDED_URL_RE = re.compile(
    r"""(?xi)
    (?:https?:)?//[^\s'"`)\]<>]+              # scheme-relative or absolute URL
    """
)


class ExecutionControl:
    """Cooperative cancellation checked again at every action admission.

    A running native call is never declared cancelled by abandoning its thread.
    The owner must retain its resource lease until that worker actually returns.
    """
    def __init__(self, deadline: float, *, clock=None):
        self.deadline = deadline
        self.clock = clock or time.monotonic
        self._cancelled = threading.Event()
        self.reason = ""

    def cancel(self, reason="desktop execution cancelled"):
        self.reason = reason
        self._cancelled.set()

    def check(self):
        if self._cancelled.is_set():
            return False, self.reason
        if self.clock() >= self.deadline:
            return False, "desktop execution deadline expired"
        return True, ""

    def require(self):
        allowed, reason = self.check()
        if not allowed:
            raise TimeoutError(reason)


async def await_owned_worker(awaitable, control=None):
    """Cancellation cannot detach a thread that may still be actuating."""
    import asyncio
    future = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            if future.cancelled():
                raise
            cancelled = True
            if control is not None:
                control.cancel()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ComplianceChecker:
    """Consults a :class:`RestrictionPolicy` before a desktop action runs.

    Wire-up point: ``anyuse/core/agent.py`` builds one of these from the
    orchestrator boundary and hands it to the orchestrator/executor. The MCP
    server and plan executor read :attr:`actions_executed` for reporting.

    The checker is intentionally *additive and default-safe*: with a
    permissive (inactive) policy, :meth:`check_action` always allows, so flows
    that run without a boundary behave exactly as before.
    """

    def __init__(self, policy: Optional[RestrictionPolicy] = None, *, control=None, guard_evaluator=None):
        self.policy = policy or RestrictionPolicy.permissive()
        self.control = control
        self.guard_evaluator = guard_evaluator
        self._lock = threading.RLock()
        # Counters surfaced to the MCP layer for "actions_taken" reporting.
        self.actions_executed = 0
        self.actions_blocked = 0
        self.block_reasons: List[str] = []

    # ---- target extraction --------------------------------------------

    @staticmethod
    def _extract(action: Any) -> Tuple[str, Dict[str, Any]]:
        """Pull (action_name, args) out of a loosely-typed action object.

        Accepts dicts shaped like ``{"action": "navigate", "args": {...}}``
        (the GuardEvaluator/plan contract) and also flat dicts where the
        target keys sit at the top level. Objects with ``.action``/``.args``
        attributes are supported too.
        """
        name = ""
        args: Dict[str, Any] = {}

        if isinstance(action, dict):
            name = str(
                action.get("action")
                or action.get("name")
                or action.get("type")
                or ""
            ).strip()
            raw_args = action.get("args")
            if isinstance(raw_args, dict):
                args = dict(raw_args)
            else:
                # Flat shape — treat the whole dict (minus the name key) as args.
                args = {
                    k: v
                    for k, v in action.items()
                    if k not in ("action", "name", "type", "args")
                }
        else:
            name = str(getattr(action, "action", "") or getattr(action, "name", "")).strip()
            raw_args = getattr(action, "args", None)
            if isinstance(raw_args, dict):
                args = dict(raw_args)

        return name.lower(), args

    @staticmethod
    def _find(args: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
        for k in keys:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    @staticmethod
    def _iter_strings(value: Any) -> List[str]:
        """Recursively collect every string scalar inside ``value``.

        Walks dicts/lists/tuples/sets so a URL nested in a payload (a script
        body, a list of args, a headers dict) is still reachable. Caps the walk
        defensively so a pathological deeply-nested payload can't blow the
        stack.
        """
        out: List[str] = []

        def _walk(v: Any, depth: int) -> None:
            if depth > 6:
                return
            if isinstance(v, str):
                if v.strip():
                    out.append(v)
            elif isinstance(v, dict):
                for sub in v.values():
                    _walk(sub, depth + 1)
            elif isinstance(v, (list, tuple, set)):
                for sub in v:
                    _walk(sub, depth + 1)

        _walk(value, 0)
        return out

    @classmethod
    def _embedded_targets(cls, args: Dict[str, Any]) -> List[str]:
        """Pull every URL/host substring out of ALL string arg values.

        This is the defense-in-depth scan: actions like
        ``{"action":"javascript","args":{"script":"location.href='https://evil.com'"}}``
        hide their navigation target inside an opaque string the explicit
        URL-key lookup misses. We extract candidates here and the caller checks
        each against the domain policy.
        """
        targets: List[str] = []
        seen = set()
        for s in cls._iter_strings(args):
            for m in _EMBEDDED_URL_RE.findall(s):
                cand = m.strip().strip("'\"`")
                if cand and cand not in seen:
                    seen.add(cand)
                    targets.append(cand)
        return targets

    # ---- the decision --------------------------------------------------

    def check_action(self, action: Any) -> Tuple[bool, str]:
        # Admission and the count reservation are atomic across worker threads.
        with self._lock:
            return self._check_action(action)

    def _check_action(self, action: Any) -> Tuple[bool, str]:
        """Return ``(allowed, reason)`` for a desktop action.

        Counts permitted actions in :attr:`actions_executed` and blocked ones
        in :attr:`actions_blocked`. When the policy is inactive this is a
        pass-through that simply tallies the action as executed.
        """
        if self.control is not None:
            allowed, reason = self.control.check()
            if not allowed:
                return self._deny(reason)
        name, args = self._extract(action)
        if self.policy.max_actions is not None and self.actions_executed >= self.policy.max_actions:
            return self._deny("boundary max_actions exhausted")
        # Match public verbs as well as their actuator aliases. An exclude or
        # guard wins over an allow, so aliases cannot weaken the boundary.
        aliases = {"click_at": ("click",), "type_text_at": ("type", "type_text"),
                   "key_combination": ("key", "press_key"), "scroll_at": ("scroll",),
                   "hover_at": ("hover",), "drag_to": ("drag",)}
        names = {name, "*"}
        for canonical, group in aliases.items():
            if name == canonical or name in group:
                names.update((canonical, *group))
        if name in {"click_at", "click", "double_click", "right_click"}:
            names.update(("click_at", "click"))
            if name == "right_click" or args.get("button") == "right":
                names.add("right_click")
            if name == "double_click" or args.get("clicks", 1) == 2:
                names.add("double_click")
        rules = {self.policy.action_rules[n] for n in names if n in self.policy.action_rules}
        if "exclude" in rules:
            return self._deny(f"boundary excludes action '{name}'")
        if "guard" in rules:
            if self.guard_evaluator is None:
                return self._deny(f"action '{name}' requires a configured guard evaluator")
            try:
                allowed = self.guard_evaluator(action)
            except Exception:
                return self._deny(f"guard evaluation failed for '{name}'")
            if allowed is not True:
                return self._deny(f"guard did not affirm action '{name}'")
            if self.control is not None and not self.control.check()[0]:
                return self._deny(self.control.check()[1])
        # Fast path: no active policy → preserve existing behavior.
        if not self.policy or not self.policy.active:
            self.actions_executed += 1
            return True, ""

        name, args = self._extract(action)

        # Determine whether this action has a domain or app target we can
        # evaluate. Actions with neither are out of scope for this policy
        # (clicks, typing, scrolls) and are allowed.
        url = self._find(args, _URL_KEYS)
        app = self._find(args, _APP_KEYS)

        # If the action name signals navigation but we also/only have a URL,
        # check the domain. A URL present on any action is enough to evaluate.
        if url is not None or name in _NAV_ACTIONS:
            target = url if url is not None else ""
            allowed, reason = self.policy.is_domain_allowed(target)
            if not allowed:
                return self._deny(reason)

        if app is not None or name in _APP_ACTIONS:
            target = app if app is not None else ""
            allowed, reason = self.policy.is_app_allowed(target)
            if not allowed:
                return self._deny(reason)

        # Defense-in-depth (fix #5): an action may smuggle a navigation target
        # inside an opaque string arg (a JS snippet, an AppleScript, a shell
        # command) that the explicit URL-key lookup above does not see. Scan
        # ALL string arg values for embedded URLs/hosts and check each against
        # the domain policy. This is skipped for benign no-target actions so a
        # URL that merely appears in typed text / a click label can't trip it.
        if name not in _BENIGN_ACTIONS:
            embedded = self._embedded_targets(args)
            for cand in embedded:
                allowed, reason = self.policy.is_domain_allowed(cand)
                if not allowed:
                    return self._deny(
                        f"Embedded navigation target rejected: {reason}"
                    )

            # An action that COULD navigate (a scripting/exec action) but whose
            # payload yielded no extractable target leaves us unable to vet it.
            # With a constraining (allowlist / default-deny) policy active we
            # fail closed rather than execute an un-vettable navigator. If we
            # DID extract a target above and it passed, the navigation has been
            # vetted, so the backstop does not fire.
            if (
                not embedded
                and name in _SCRIPTING_ACTIONS
                and self._is_constraining()
            ):
                return self._deny(
                    f"Action '{name}' can navigate but its target could not be "
                    "verified against the active allowlist policy (fail-closed)"
                )

        # Allowed (either no target to check, or all checks passed).
        self.actions_executed += 1
        return True, ""

    def _deny(self, reason: str) -> Tuple[bool, str]:
        self.actions_blocked += 1
        self.block_reasons.append(reason)
        return False, reason

    def _is_constraining(self) -> bool:
        """True if the active policy restricts where navigation may go.

        Only allowlist mode (or an explicit default-deny) actually bounds the
        set of permitted destinations; a pure blocklist still allows the open
        web, so an un-vettable scripting action there is no worse than any
        other navigation and need not be blanket-denied.
        """
        pol = self.policy
        if not pol or not pol.active:
            return False
        return pol.list_mode == "allowlist" or bool(pol.default_deny)

    def reset(self) -> None:
        """Reset counters (e.g. between independent tasks)."""
        self.actions_executed = 0
        self.actions_blocked = 0
        self.block_reasons = []
