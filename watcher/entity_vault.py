"""Entity vault — tokenization + encrypted storage + grant-gated deref.

Every private entity in a durable
watcher artifact is replaced by a stable pseudonymous marker

    [[ent:<class>:<12-hex-hmac>]]

whose real value lives Fernet-encrypted in ``~/.anyuse/entity_vault.db``.
Tokens are HMAC-SHA256(k_tok, class || 0x00 || canonical(value)) truncated to
12 hex chars — deterministic, so the same entity always yields the same token
(correlation works; content never crosses). Dereference requires a matching
``grants`` row (+ user presence for the sensitive classes) and always writes a
``deref_audit`` row — allowed or denied.

Key custody (spec §5): two independent random keys under the macOS Keychain
service ``anyuse-entity-vault`` via keyring — NOT the password/PBKDF2 path of
credentials/vault.py. When keyring is unavailable the vault degrades to a
0600-permission key file under ~/.anyuse (loud warning; documented degraded
mode) rather than silently disabling privacy.

Everything here is best-effort toward the watcher loop: tokenisation never
raises into a producer; on total key failure the fallback is REDACTION
(``[[redacted:<class>]]``) — fail closed, never leak.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import re
import sqlite3
import unicodedata
import uuid
from base64 import urlsafe_b64encode, urlsafe_b64decode
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from anyuse.paths import anyuse_data_path, vault_key_file

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken
    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - crypto is in requirements
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    keyring = None  # type: ignore[assignment]
    KEYRING_AVAILABLE = False


# ---------------------------------------------------------------------------
# Entity classes (spec §2). Unknown classes are treated as vault_presence —
# over-protect by default.
# ---------------------------------------------------------------------------

# Never written anywhere — enforced at the CAPTURE layer (spec §8); listed here
# so the regex net can recognise card shapes and refuse to vault them.
NEVER_CAPTURED = frozenset({
    "password", "totp", "payment_card", "recovery_code",
})

# Grant + user presence per deref.
VAULT_PRESENCE = frozenset({
    "person", "email_address", "email_subject", "email_content",
    "message_content", "document_content", "order", "amount",
    "address", "phone",
    # Meeting content (TODOS #29): the abstraction says "meeting about
    # [[ent:meeting_topic:...]]"; the topic and the FULL transcript live here.
    "meeting_topic", "meeting_transcript",
})

# Standing grant sufficient (audited).
VAULT_GRANT = frozenset({
    "project", "url", "file", "window_title", "other",
    # UI labels the structured step capture recorded (button / menu / link titles,
    # fingerprint titles): the compiler on this machine needs them to match a control
    # again, so a standing grant suffices (audited, no presence prompt).
    "ui_label",
})

_CLASS_RE = re.compile(r"^[a-z_]{1,32}$")
MARKER_RE = re.compile(r"\[\[ent:([a-z_]{1,32}):([0-9a-f]{12})\]\]")
REDACTED_MARKER_RE = re.compile(r"\[\[redacted:([a-z_]{1,32})\]\]")

MODE_RANK = {"search": 0, "deref": 1, "bulk": 2}


def tier_for(class_: str) -> str:
    """'never_captured' | 'vault_presence' | 'vault_grant' for an entity class."""
    if class_ in NEVER_CAPTURED:
        return "never_captured"
    if class_ in VAULT_GRANT:
        return "vault_grant"
    return "vault_presence"  # includes unknown classes: over-protect


def canonical(value: str, class_: str = "other") -> str:
    """Canonicalise an entity value so trivial variants merge to one token.

    NFC normalise, trim, casefold, collapse internal whitespace. Class-specific
    additions can hang off ``class_`` later; casefold already lowercases emails.
    """
    text = unicodedata.normalize("NFC", str(value)).strip().casefold()
    return re.sub(r"\s+", " ", text)


def marker(class_: str, token: str) -> str:
    return f"[[ent:{class_}:{token}]]"


def redacted_marker(class_: str) -> str:
    return f"[[redacted:{class_}]]"


# ---------------------------------------------------------------------------
# The deterministic regex net (spec §3): machine-shaped PII the model may miss.
# Order matters — cards before phones (a card number must not half-match as a
# phone), emails before anything containing digits.
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13-19 digits allowing space/dash separators; Luhn-checked before use.
_CARD_RE = re.compile(r"(?<![\d./-])(?:\d[ -]?){12,18}\d(?![\d./-])")
# International-ish phone shapes; requires >= 9 digits total (checked in code)
# so timestamps and small ids don't match.
_PHONE_RE = re.compile(
    r"(?<![\w./-])\+?\d{1,3}[\s\-.]?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{2,4}(?![\w./-])"
)
_AMOUNT_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d{1,2})?")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def regex_net(text: str) -> List[Tuple[int, int, str, str]]:
    """Find machine-shaped PII spans in ``text``.

    Returns non-overlapping ``(start, end, span_text, class)`` tuples,
    earliest-first. Only spans NOT already inside an ``[[ent:...]]`` /
    ``[[redacted:...]]`` marker are reported.
    """
    if not text:
        return []

    protected: List[Tuple[int, int]] = [
        m.span() for m in MARKER_RE.finditer(text)
    ] + [m.span() for m in REDACTED_MARKER_RE.finditer(text)]

    def _inside_protected(s: int, e: int) -> bool:
        return any(s < pe and e > ps for ps, pe in protected)

    found: List[Tuple[int, int, str, str]] = []

    def _add(m: re.Match, class_: str) -> None:
        s, e = m.span()
        if _inside_protected(s, e):
            return
        if any(s < fe and e > fs for fs, fe, _, _ in found):
            return  # overlaps an earlier (higher-priority) match
        found.append((s, e, m.group(0), class_))

    for m in _EMAIL_RE.finditer(text):
        _add(m, "email_address")
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            _add(m, "payment_card")
    for m in _PHONE_RE.finditer(text):
        if len(re.sub(r"\D", "", m.group(0))) >= 9:
            _add(m, "phone")
    for m in _AMOUNT_RE.finditer(text):
        _add(m, "amount")

    found.sort(key=lambda t: t[0])
    return found


_ANY_MARKER_RE = re.compile(
    r"\[\[(?:ent:[a-z_]{1,32}:[0-9a-f]{12}|redacted:[a-z_]{1,32})\]\]"
)


def _replace_outside_markers(text: str, span: str, replacement: str) -> str:
    """``str.replace`` that never touches the inside of an existing
    ``[[ent:...]]`` / ``[[redacted:...]]`` marker — a model span like "mail"
    must not corrupt a previously inserted ``[[ent:email_address:...]]``."""
    parts: List[str] = []
    last = 0
    for m in _ANY_MARKER_RE.finditer(text):
        parts.append(text[last:m.start()].replace(span, replacement))
        parts.append(m.group(0))
        last = m.end()
    parts.append(text[last:].replace(span, replacement))
    return "".join(parts)


def redact_text(text: str) -> str:
    """Vault-free fallback: replace regex-net matches with non-derefable
    ``[[redacted:<class>]]`` markers. Loses data, never leaks (spec §3
    defense-in-depth for when no vault is constructible)."""
    if not text:
        return text
    out: List[str] = []
    last = 0
    for s, e, _, class_ in regex_net(text):
        out.append(text[last:s])
        out.append(redacted_marker(class_))
        last = e
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Presence check (spec §5 v1): LAContext Touch ID / passcode prompt via pyobjc.
# App-enforced in v1; injectable for tests and for the v2 SecAccessControl move.
# ---------------------------------------------------------------------------

def la_context_presence(reason: str = "anyuse entity access") -> bool:
    """Prompt for user presence (biometry or passcode). Fail-CLOSED: any
    unavailability/error/timeout returns False."""
    try:
        import threading

        import LocalAuthentication  # pyobjc

        ctx = LocalAuthentication.LAContext.alloc().init()
        policy = 2  # LAPolicyDeviceOwnerAuthentication (biometry OR passcode)
        ok, _err = ctx.canEvaluatePolicy_error_(policy, None)
        if not ok:
            return False
        result = {"ok": False}
        done = threading.Event()

        def _reply(success, _error):
            result["ok"] = bool(success)
            done.set()

        ctx.evaluatePolicy_localizedReason_reply_(policy, reason, _reply)
        done.wait(timeout=60)
        return result["ok"]
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Key custody
# ---------------------------------------------------------------------------

_KEYRING_SERVICE = "anyuse-entity-vault"
# The key file is resolved through anyuse.paths.vault_key_file(), which honours
# ANYUSE_VAULT_KEYFILE / ANYUSE_SECRETS_DIR and DELIBERATELY does not follow
# ANYUSE_DATA_DIR: moving the data directory must not silently mint a new key
# and orphan every artefact encrypted under the old one.
#
# _KEYFILE is the in-process override hook — set it to a Path to pin the key
# file for this process (the custody tests do exactly that). None means "ask
# the resolver", which is the production case.
_KEYFILE: Optional[Path] = None


def _keyfile() -> Path:
    """The key file: the in-process pin if one is set, otherwise the resolver."""
    return _KEYFILE if _KEYFILE is not None else vault_key_file()


def _keyring_is_usable() -> bool:
    """The null backend discards writes; use durable file custody instead."""
    if not KEYRING_AVAILABLE:
        return False
    get_backend = getattr(keyring, "get_keyring", None)
    if not callable(get_backend):
        # Injectable providers can implement just get_password/set_password.
        return True
    try:
        from keyring.backends.null import Keyring as NullKeyring
    except ImportError:
        # Optional classifier unavailable: retain the existing provider path.
        return True
    try:
        return not isinstance(get_backend(), NullKeyring)
    except Exception:
        return False


def _load_or_create_keys() -> Tuple[bytes, bytes]:
    """Return (k_tok, k_enc). Keychain via keyring preferred; 0600 key file as
    the documented degraded fallback. Raises RuntimeError only when neither
    store is writable (callers then fall back to redaction)."""
    if _keyring_is_usable():
        try:
            tok_b64 = keyring.get_password(_KEYRING_SERVICE, "k_tok")
            enc_b64 = keyring.get_password(_KEYRING_SERVICE, "k_enc")
            if not tok_b64 or not enc_b64:
                # Continuity: keys minted into the degraded key file BEFORE
                # keyring became available must seed the keyring — minting
                # fresh ones here would silently orphan every existing token
                # and Fernet blob in the vault.
                seed = _keyfile_values()
                if not tok_b64:
                    tok_b64 = seed.get("k_tok") or \
                        urlsafe_b64encode(os.urandom(32)).decode()
                    keyring.set_password(_KEYRING_SERVICE, "k_tok", tok_b64)
                if not enc_b64:
                    enc_b64 = seed.get("k_enc") or Fernet.generate_key().decode()
                    keyring.set_password(_KEYRING_SERVICE, "k_enc", enc_b64)
            return urlsafe_b64decode(tok_b64.encode()), enc_b64.encode()
        except Exception as e:
            logger.warning("entity_vault: keyring unavailable (%s); "
                           "falling back to key file", e)

    # Degraded mode: key file with restrictive permissions. The file is a
    # shared bag of named keys (k_tok/k_enc here; k_raw and other named keys
    # via load_or_create_named_key/set_named_secret can create it FIRST), so
    # partial contents are a normal state — fill in whichever vault keys are
    # missing and preserve everything else.
    try:
        data: Dict[str, str] = {}
        if _keyfile().exists():
            data = json.loads(_keyfile().read_text())
        missing = [k for k in ("k_tok", "k_enc") if k not in data]
        if missing:
            if "k_tok" in missing:
                data["k_tok"] = urlsafe_b64encode(os.urandom(32)).decode()
            if "k_enc" in missing:
                data["k_enc"] = Fernet.generate_key().decode()
            _keyfile().parent.mkdir(parents=True, exist_ok=True)
            _keyfile().write_text(json.dumps(data))
            _keyfile().chmod(0o600)
            logger.warning("entity_vault: keys %s stored in %s (keyring "
                           "unavailable) — DEGRADED custody, see spec §5",
                           missing, _keyfile())
        return (urlsafe_b64decode(data["k_tok"].encode()),
                data["k_enc"].encode())
    except Exception as e:
        raise RuntimeError(f"entity_vault: no key store available: {e}")


def _keyfile_values() -> Dict[str, str]:
    """The degraded key file's named-key bag, {} when absent/unreadable.
    Used to seed the keyring for continuity when it becomes available."""
    try:
        if _keyfile().exists():
            data = json.loads(_keyfile().read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def get_named_secret(name: str) -> Optional[str]:
    """Read a named secret from the vault's custody chain (Keychain via
    keyring, else the 0600 key file). None when absent everywhere."""
    if _keyring_is_usable():
        try:
            value = keyring.get_password(_KEYRING_SERVICE, name)
            if value:
                return value
        except Exception:
            pass
    try:
        if _keyfile().exists():
            return json.loads(_keyfile().read_text()).get(name)
    except Exception:
        pass
    return None


def set_named_secret(name: str, value: str) -> None:
    """Store a named secret under the vault's custody chain. Raises
    RuntimeError when neither store is writable (callers fail closed)."""
    if _keyring_is_usable():
        try:
            keyring.set_password(_KEYRING_SERVICE, name, value)
            return
        except Exception as e:
            logger.warning("entity_vault: keyring unavailable for %s (%s); "
                           "falling back to key file", name, e)
    try:
        data: Dict[str, str] = {}
        if _keyfile().exists():
            data = json.loads(_keyfile().read_text())
        data[name] = value
        _keyfile().parent.mkdir(parents=True, exist_ok=True)
        _keyfile().write_text(json.dumps(data))
        _keyfile().chmod(0o600)
        logger.warning("entity_vault: secret %r stored in %s (keyring "
                       "unavailable) — DEGRADED custody, see spec §5",
                       name, _keyfile())
    except Exception as e:
        raise RuntimeError(f"entity_vault: no key store available for {name}: {e}")


def load_or_create_named_key(name: str) -> bytes:
    """A named Fernet key under the same custody chain as the vault keys:
    Keychain (service ``anyuse-entity-vault``) preferred, the 0600 key file as
    the documented degraded fallback. Used for keys beyond the vault pair —
    e.g. ``k_raw`` (spec §9 raw-artifact encryption). Raises RuntimeError when
    neither store is writable."""
    if _keyring_is_usable():
        try:
            b64 = keyring.get_password(_KEYRING_SERVICE, name)
            if not b64:
                # Same continuity rule as _load_or_create_keys: a key already
                # minted into the degraded file (e.g. k_raw before keyring was
                # installed) seeds the keyring instead of a fresh mint that
                # would orphan everything encrypted under it.
                b64 = _keyfile_values().get(name) or Fernet.generate_key().decode()
                keyring.set_password(_KEYRING_SERVICE, name, b64)
            return b64.encode()
        except Exception as e:
            logger.warning("entity_vault: keyring unavailable for %s (%s); "
                           "falling back to key file", name, e)

    try:
        data: Dict[str, str] = {}
        if _keyfile().exists():
            data = json.loads(_keyfile().read_text())
        if name not in data:
            data[name] = Fernet.generate_key().decode()
            _keyfile().parent.mkdir(parents=True, exist_ok=True)
            _keyfile().write_text(json.dumps(data))
            _keyfile().chmod(0o600)
            logger.warning("entity_vault: key %r stored in %s (keyring "
                           "unavailable) — DEGRADED custody, see spec §5",
                           name, _keyfile())
        return data[name].encode()
    except Exception as e:
        raise RuntimeError(f"entity_vault: no key store available for {name}: {e}")


# ---------------------------------------------------------------------------
# The vault
# ---------------------------------------------------------------------------

class Denied(Exception):
    """Raised nowhere — deref returns result dicts; this exists for callers
    that prefer ``deref(...).get('ok')`` style guards to read clearly."""


class EntityVault:
    """Encrypted entity store + grants + audited pointwise deref (spec §4-§6).

    ``k_tok``/``k_enc``/``db_path``/``presence_provider`` are injectable for
    tests; production uses Keychain-held keys and the default DB path.
    """

    DB_PATH = anyuse_data_path("entity_vault.db")

    def __init__(
        self,
        db_path: Optional[Path] = None,
        k_tok: Optional[bytes] = None,
        k_enc: Optional[bytes] = None,
        presence_provider: Optional[Callable[[str], bool]] = None,
        custody: Optional[Any] = None,
        custody_path: Optional[Path] = None,
    ):
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("entity_vault requires the cryptography package")
        keys_injected = k_tok is not None or k_enc is not None
        if k_tok is None or k_enc is None:
            loaded_tok, loaded_enc = _load_or_create_keys()
            k_tok = k_tok or loaded_tok
            k_enc = k_enc or loaded_enc
        self._k_tok = k_tok
        self._fernet = Fernet(k_enc)
        self._presence = presence_provider or la_context_presence

        # v2 custody (vault_custody.py, spec §5): encrypt-side public key at
        # capture, presence-gated private key per deref. Auto-loaded from the
        # machine sidecar ONLY for production instantiation (no injected
        # keys) — tests with injected keys stay hermetic unless they inject
        # a custody too.
        self._custody = custody
        if self._custody is None and not keys_injected:
            try:
                from anyuse.watcher.vault_custody import load_custody
                self._custody = load_custody(custody_path)
            except Exception as e:
                logger.warning("entity_vault: custody load failed (%s); "
                               "staying v1", e)
        self.db_path = Path(db_path) if db_path else self.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -- schema ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS entities(
                    token TEXT PRIMARY KEY,
                    class TEXT NOT NULL,
                    ciphertext BLOB NOT NULL,
                    first_seen TEXT, last_seen TEXT,
                    seen_count INTEGER DEFAULT 1,
                    apps TEXT)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS grants(
                    id TEXT PRIMARY KEY,
                    grantee TEXT NOT NULL,
                    classes TEXT NOT NULL,
                    scope TEXT,
                    mode TEXT NOT NULL DEFAULT 'deref',
                    requires_presence INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT, expires_at TEXT, revoked_at TEXT,
                    created_via TEXT)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS deref_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, token TEXT, grantee TEXT, purpose TEXT,
                    grant_id TEXT, allowed INTEGER, presence_used INTEGER)"""
            )
            conn.commit()
        finally:
            conn.close()

    # -- tokenisation ----------------------------------------------------

    def token_for(self, value: str, class_: str) -> str:
        """The deterministic token — computable without touching the DB."""
        digest = hmac.new(
            self._k_tok,
            class_.encode() + b"\0" + canonical(value, class_).encode(),
            hashlib.sha256,
        ).hexdigest()
        return digest[:12]

    def tokenize(self, value: str, class_: str, app: str = "") -> str:
        """Vault ``value`` and return its 12-hex token.

        never_captured classes are NOT stored — the caller gets a token it can
        embed as a marker, but there is no ciphertext to deref (what is never
        written cannot be stolen; the capture layer should have suppressed it
        before it got here, this is the last line).
        """
        if not _CLASS_RE.match(class_ or ""):
            class_ = "other"
        token = self.token_for(value, class_)
        if class_ in NEVER_CAPTURED:
            return token
        now = datetime.now().isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT seen_count, apps FROM entities WHERE token = ?",
                (token,),
            )
            row = cur.fetchone()
            if row:
                seen, apps_json = row
                apps = json.loads(apps_json) if apps_json else []
                if app and app not in apps:
                    apps.append(app)
                conn.execute(
                    "UPDATE entities SET last_seen = ?, seen_count = ?, apps = ? "
                    "WHERE token = ?",
                    (now, (seen or 1) + 1, json.dumps(apps), token),
                )
            else:
                conn.execute(
                    "INSERT INTO entities(token, class, ciphertext, first_seen, "
                    "last_seen, seen_count, apps) VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (token, class_, self._encrypt_value(str(value).encode()),
                     now, now, json.dumps([app] if app else [])),
                )
            conn.commit()
            return token
        finally:
            conn.close()

    def tokenize_text(
        self,
        text: str,
        entities: Optional[List[Dict[str, str]]] = None,
        app: str = "",
    ) -> str:
        """Replace private spans in ``text`` with ``[[ent:class:token]]`` markers.

        ``entities`` is the model-extracted list (``[{span, class}]``, spec §3
        ENTITIES tag); the deterministic regex net always runs afterwards as
        the backstop. Never raises — on failure returns the redaction fallback.
        """
        if not text:
            return text
        try:
            out = text
            # Model-extracted spans first, longest-first so substrings of an
            # already-replaced span don't double-fire.
            for ent in sorted(entities or [], key=lambda e: -len(e.get("span") or "")):
                span = ent.get("span") or ""
                class_ = ent.get("class") or "other"
                if not span or span not in out:
                    continue
                if class_ in NEVER_CAPTURED:
                    out = _replace_outside_markers(out, span, redacted_marker(class_))
                else:
                    out = _replace_outside_markers(
                        out, span, marker(class_, self.tokenize(span, class_, app)))
            # Regex net over what remains.
            pieces: List[str] = []
            last = 0
            for s, e, span, class_ in regex_net(out):
                pieces.append(out[last:s])
                if class_ in NEVER_CAPTURED:
                    pieces.append(redacted_marker(class_))
                else:
                    pieces.append(marker(class_, self.tokenize(span, class_, app)))
                last = e
            pieces.append(out[last:])
            return "".join(pieces)
        except Exception as e:
            logger.warning("entity_vault: tokenize_text failed (%s); redacting", e)
            return redact_text(text)

    def tokenize_record(self, record: Any, entities: Optional[List[Dict[str, str]]] = None) -> Any:
        """Tokenize the durable text fields of an AbstractionRecord in place
        (and return it): summary, structure.goal, step target_name/reason,
        tags — and the regex net over title (spec §3: title stays readable,
        machine-shaped PII still cannot survive in it)."""
        try:
            app = ""
            apps = getattr(record, "apps", None) or []
            if apps:
                app = apps[0]
            record.summary = self.tokenize_text(getattr(record, "summary", "") or "", entities, app)
            record.title = self.tokenize_text(getattr(record, "title", "") or "", None, app)
            structure = getattr(record, "structure", None) or {}
            if isinstance(structure, dict):
                # Generic walk: every free-text string in structure gets the
                # tokenizer (existing markers are protected, so refs like
                # topic_ref/transcript_ref pass through untouched). Lists of
                # strings (decisions, action_items, trigger_phrases) too.
                for key, val in list(structure.items()):
                    if isinstance(val, str) and val:
                        structure[key] = self.tokenize_text(val, entities, app)
                    elif isinstance(val, list):
                        structure[key] = [
                            self.tokenize_text(x, entities, app)
                            if isinstance(x, str) else x
                            for x in val
                        ]
                for step in structure.get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    for key in ("target_name", "reason"):
                        if step.get(key):
                            step[key] = self.tokenize_text(step[key], entities, app)
            tags = getattr(record, "tags", None)
            if isinstance(tags, list):
                record.tags = [self.tokenize_text(t, entities, app) for t in tags]
            return record
        except Exception as e:
            logger.warning("entity_vault: tokenize_record failed (%s)", e)
            return record

    # -- grants ----------------------------------------------------------

    def grant(
        self,
        grantee: str,
        classes: List[str],
        mode: str = "deref",
        scope: Optional[Dict[str, Any]] = None,
        requires_presence: bool = True,
        expires_at: Optional[str] = None,
        created_via: str = "api",
    ) -> str:
        grant_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO grants(id, grantee, classes, scope, mode, "
                "requires_presence, created_at, expires_at, created_via) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (grant_id, grantee, json.dumps(sorted(set(classes))),
                 json.dumps(scope) if scope else None,
                 mode if mode in MODE_RANK else "deref",
                 1 if requires_presence else 0,
                 datetime.now().isoformat(), expires_at, created_via),
            )
            conn.commit()
            return grant_id
        finally:
            conn.close()

    def revoke(self, grant_id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (datetime.now().isoformat(), grant_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_grants(self, grantee: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            sql = ("SELECT id, grantee, classes, scope, mode, requires_presence, "
                   "created_at, expires_at, revoked_at, created_via FROM grants")
            args: tuple = ()
            if grantee:
                sql += " WHERE grantee = ?"
                args = (grantee,)
            rows = conn.execute(sql, args).fetchall()
            return [
                {
                    "id": r[0], "grantee": r[1],
                    "classes": json.loads(r[2]) if r[2] else [],
                    "scope": json.loads(r[3]) if r[3] else None,
                    "mode": r[4], "requires_presence": bool(r[5]),
                    "created_at": r[6], "expires_at": r[7],
                    "revoked_at": r[8], "created_via": r[9],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def _find_grant(self, grantee: str, class_: str, mode: str,
                    token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The first live grant covering (grantee, class, mode[, token])."""
        want_rank = MODE_RANK.get(mode, 1)
        now = datetime.now().isoformat()
        for g in self.list_grants(grantee):
            if g["revoked_at"]:
                continue
            if g["expires_at"] and g["expires_at"] < now:
                continue
            if class_ not in g["classes"]:
                continue
            if MODE_RANK.get(g["mode"], -1) < want_rank:
                continue
            scope = g["scope"] or {}
            if token and scope.get("tokens") and token not in scope["tokens"]:
                continue
            return g
        return None

    # -- deref / search ---------------------------------------------------

    def _audit(self, token: str, grantee: str, purpose: str,
               grant_id: Optional[str], allowed: bool, presence_used: bool):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO deref_audit(ts, token, grantee, purpose, grant_id, "
                "allowed, presence_used) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), token, grantee, purpose,
                 grant_id, 1 if allowed else 0, 1 if presence_used else 0),
            )
            conn.commit()
        finally:
            conn.close()

    def deref(self, token: str, grantee: str, purpose: str = "") -> Dict[str, Any]:
        """Grant-checked, presence-gated, audited dereference.

        Returns ``{ok: True, value, class, grant_id}`` or
        ``{ok: False, reason}``. Values are for in-flight use only — callers
        must never persist them (spec §6).
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT class, ciphertext FROM entities WHERE token = ?",
                (token,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            self._audit(token, grantee, purpose, None, False, False)
            return {"ok": False, "reason": "not_found"}

        class_, ciphertext = row
        g = self._find_grant(grantee, class_, "deref", token=token)
        if not g:
            self._audit(token, grantee, purpose, None, False, False)
            return {"ok": False, "reason": "no_grant"}

        blob = ciphertext if isinstance(ciphertext, bytes) else bytes(ciphertext)
        # When the decrypt path is OS-enforced the presence prompt fires
        # INSIDE the key release — the app-level prompt would double-ask.
        # That's every AUV2SE blob, and any AUV2* blob whose custody reports
        # os_enforced (v2sw with ANYUSE_VAULT_CUSTODY=os armed).
        os_gated = blob.startswith(b"AUV2SE") or (
            blob.startswith(b"AUV2")
            and bool(getattr(self._custody, "os_enforced", False)))
        presence_used = False
        if (g["requires_presence"] and tier_for(class_) == "vault_presence"
                and not os_gated):
            presence_used = True
            try:
                ok = bool(self._presence("anyuse: allow "
                                         f"{grantee} to read one {class_} entity"))
            except Exception:
                ok = False
            if not ok:
                self._audit(token, grantee, purpose, g["id"], False, True)
                return {"ok": False, "reason": "presence_denied"}

        try:
            value = self._decrypt_value(blob).decode()
        except Exception:
            self._audit(token, grantee, purpose, g["id"], False, presence_used)
            return {"ok": False, "reason": "decrypt_failed"}

        self._audit(token, grantee, purpose, g["id"], True, presence_used)
        return {"ok": True, "value": value, "class": class_, "grant_id": g["id"]}

    def _encrypt_value(self, data: bytes) -> bytes:
        """v2 custody when configured (promptless public-key path); v1 Fernet
        otherwise. A broken custody falls back to Fernet — still encrypted,
        never plaintext."""
        if self._custody is not None:
            try:
                return self._custody.encrypt(data)
            except Exception as e:
                logger.warning("entity_vault: custody encrypt failed (%s); "
                               "falling back to v1 Fernet", e)
        return self._fernet.encrypt(data)

    def _decrypt_value(self, blob: bytes) -> bytes:
        """Per-row sniff: AUV2* → custody (fail-closed without one); anything
        else is a v1 Fernet token."""
        if blob.startswith(b"AUV2"):
            if self._custody is None:
                raise ValueError("v2 ciphertext but no custody configured")
            return self._custody.decrypt(blob)
        return self._fernet.decrypt(blob)

    def migrate_custody(self) -> int:
        """Re-encrypt v1 Fernet rows under the configured v2 custody's public
        key. Returns the count migrated. Safe to re-run — the sniff skips
        already-migrated rows; an undecryptable row is skipped loudly, never
        dropped."""
        if self._custody is None:
            raise RuntimeError("no v2 custody configured")
        migrated = 0
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT token, ciphertext FROM entities").fetchall()
            for token, blob in rows:
                raw = blob if isinstance(blob, bytes) else bytes(blob)
                if raw.startswith(b"AUV2"):
                    continue
                try:
                    value = self._fernet.decrypt(raw)
                except Exception as e:
                    logger.warning("migrate_custody: cannot decrypt %s (%s); "
                                   "row left as-is", token, e)
                    continue
                conn.execute(
                    "UPDATE entities SET ciphertext = ? WHERE token = ?",
                    (self._custody.encrypt(value), token))
                migrated += 1
            conn.commit()
            return migrated
        finally:
            conn.close()

    def forget(self, token: str, max_seen: int = 1) -> bool:
        """Delete an entity whose value was written at most ``max_seen`` times - a value
        whose only reference is being withdrawn (a step recorded on a surface flagged
        afterwards). A value seen more often is referenced elsewhere and stays. The freed
        cells are overwritten (``secure_delete``): a plain DELETE leaves the ciphertext in
        the file's free pages."""
        conn = self._connect()
        try:
            conn.execute("PRAGMA secure_delete = ON")
            cur = conn.execute("DELETE FROM entities WHERE token = ? AND seen_count <= ?",
                               (token, int(max_seen)))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def search_token(self, term: str, class_: str, grantee: str,
                     purpose: str = "") -> Optional[str]:
        """Exact-entity search without decryption (spec §7): HMAC the query
        term and return the token IF it exists in the vault and ``grantee``
        holds a ``search``-mode grant for the class. Audited either way."""
        g = self._find_grant(grantee, class_, "search")
        token = self.token_for(term, class_)
        if not g:
            self._audit(token, grantee, f"search:{purpose}", None, False, False)
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM entities WHERE token = ?", (token,)
            ).fetchone()
        finally:
            conn.close()
        self._audit(token, grantee, f"search:{purpose}", g["id"],
                    bool(row), False)
        return token if row else None

    def audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ts, token, grantee, purpose, grant_id, allowed, "
                "presence_used FROM deref_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"ts": r[0], "token": r[1], "grantee": r[2], "purpose": r[3],
                 "grant_id": r[4], "allowed": bool(r[5]),
                 "presence_used": bool(r[6])}
                for r in rows
            ]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Module-level lazy default (mirrors BatchWatcher._get_abstraction_store):
# best-effort, never raises; None means "redact instead".
# ---------------------------------------------------------------------------

_default_vault: Optional[EntityVault] = None
_default_vault_failed = False


def get_default_vault() -> Optional[EntityVault]:
    global _default_vault, _default_vault_failed
    if _default_vault is not None:
        return _default_vault
    if _default_vault_failed:
        return None
    try:
        _default_vault = EntityVault()
        return _default_vault
    except Exception as e:
        logger.warning("entity_vault: default vault unavailable (%s); "
                       "falling back to redaction", e)
        _default_vault_failed = True
        return None


def tokenize_or_redact(text: str, vault: Optional[EntityVault] = None) -> str:
    """The AbstractionStore.upsert backstop (spec §3): with a vault, produce
    real derefable tokens; without one, redact. Either way machine-shaped PII
    cannot reach a durable row in the clear."""
    v = vault if vault is not None else get_default_vault()
    if v is not None:
        return v.tokenize_text(text)
    return redact_text(text)
