"""v2 OS-enforced key custody — SecAccessControl-protected Keychain items.

Custody tier v2: move key-release enforcement from APP code (the v1
``la_context_presence`` prompt around a plain keyring item) into the OS
itself. A key stored here lives in a Keychain
generic-password item created with ``kSecAttrAccessControl =
SecAccessControlCreateWithFlags(kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
kSecAccessControlUserPresence)`` — READING the item makes macOS demand Touch ID
/ passcode, no matter which process asks or whether our Python code remembered
to prompt. What that buys over v1: the presence gate can no longer be
skipped by a code path that forgets it, and the item never leaves this device
(no iCloud sync, no export).

Honest limits: session-resident malware that can drive
the UI while the user approves still wins; the ``never_captured`` class remains
the only defense with no residue. And a process that has ALREADY read the key
holds it in memory for its lifetime — OS custody gates RELEASE, not use.

Opt-in: ``ANYUSE_VAULT_CUSTODY=os`` arms this tier; ``entity_vault`` then
prefers it for every named key and falls back to the v1 chain (keyring →
0600 key file) when the framework or the user says no — fail-DEGRADED for
custody (the vault still works), never fail-open (a protected item is never
silently rewritten as unprotected).

Every Security call goes through an injectable ``sec`` shim so the logic is
unit-testable without the real Keychain (headless test runs cannot answer a
Touch ID prompt). The default shim is the real pyobjc Security framework
(``pyobjc-framework-Security`` — add to requirements when arming this tier).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CUSTODY_ENV = "ANYUSE_VAULT_CUSTODY"
# A DISTINCT service from the v1 keyring items ("anyuse-entity-vault") so the
# two tiers can coexist during migration and neither clobbers the other.
_OS_SERVICE = "anyuse-entity-vault-os"

# errSecItemNotFound / errSecDuplicateItem / errSecUserCanceled
_ERR_NOT_FOUND = -25300
_ERR_DUPLICATE = -25299
_ERR_USER_CANCELED = -128


def enabled() -> bool:
    """The v2 opt-in: ``ANYUSE_VAULT_CUSTODY=os``."""
    return (os.environ.get(_CUSTODY_ENV) or "").strip().lower() == "os"


class _RealSec:
    """The production shim — thin passthrough to pyobjc Security."""

    def __init__(self):
        import Security  # pyobjc-framework-Security
        self.S = Security

    def make_access_control(self):
        S = self.S
        ac, err = S.SecAccessControlCreateWithFlags(
            None,
            S.kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            S.kSecAccessControlUserPresence,
            None,
        )
        if ac is None:
            raise RuntimeError(f"SecAccessControlCreateWithFlags failed: {err}")
        return ac

    def add_item(self, account: str, secret: bytes, access_control: Any) -> int:
        S = self.S
        query = {
            S.kSecClass: S.kSecClassGenericPassword,
            S.kSecAttrService: _OS_SERVICE,
            S.kSecAttrAccount: account,
            S.kSecValueData: secret,
            S.kSecAttrAccessControl: access_control,
        }
        # pyobjc out-param convention: SecItemAdd(query, None) returns
        # (OSStatus, result) as a tuple, not the bare status.
        status = S.SecItemAdd(query, None)
        if isinstance(status, tuple):
            status = status[0]
        return int(status)

    def read_item(self, account: str, reason: str) -> tuple:
        """Returns (status, data|None). The read TRIGGERS the OS presence
        prompt (kSecUseOperationPrompt carries the reason string)."""
        S = self.S
        query = {
            S.kSecClass: S.kSecClassGenericPassword,
            S.kSecAttrService: _OS_SERVICE,
            S.kSecAttrAccount: account,
            S.kSecReturnData: True,
            S.kSecUseOperationPrompt: reason,
        }
        status, data = S.SecItemCopyMatching(query, None)
        return int(status), (bytes(data) if data is not None else None)

    def delete_item(self, account: str) -> int:
        S = self.S
        query = {
            S.kSecClass: S.kSecClassGenericPassword,
            S.kSecAttrService: _OS_SERVICE,
            S.kSecAttrAccount: account,
        }
        return int(S.SecItemDelete(query))


def _default_sec() -> Optional[_RealSec]:
    try:
        return _RealSec()
    except Exception:
        return None


def available(sec: Any = None) -> bool:
    """True when an OS-custody backend is constructible (framework present)."""
    return (sec if sec is not None else _default_sec()) is not None


def store_protected_key(name: str, secret: bytes, *, sec: Any = None) -> bool:
    """Store ``secret`` under OS-enforced presence custody. Returns True on a
    FRESH store; False on duplicate (existing item is NEVER overwritten — a
    rotation must be an explicit delete+store) or any failure."""
    s = sec if sec is not None else _default_sec()
    if s is None:
        return False
    try:
        ac = s.make_access_control()
        status = s.add_item(str(name), bytes(secret), ac)
        if status == 0:
            return True
        if status == _ERR_DUPLICATE:
            logger.info("key_custody: item %s already exists (not overwritten)",
                        name)
            return False
        logger.warning("key_custody: SecItemAdd(%s) failed: %s", name, status)
        return False
    except Exception:
        logger.warning("key_custody: store failed for %s", name, exc_info=True)
        return False


def read_protected_key(name: str, *, reason: str = "anyuse vault key access",
                       sec: Any = None) -> Optional[bytes]:
    """Read a protected key — the OS shows the presence prompt. None when the
    item is absent, the user cancels, or anything errors (fail-closed)."""
    s = sec if sec is not None else _default_sec()
    if s is None:
        return None
    try:
        status, data = s.read_item(str(name), reason)
        if status == 0 and data:
            return bytes(data)
        if status == _ERR_USER_CANCELED:
            logger.info("key_custody: user declined presence for %s", name)
        elif status != _ERR_NOT_FOUND:
            logger.warning("key_custody: SecItemCopyMatching(%s) failed: %s",
                           name, status)
        return None
    except Exception:
        logger.warning("key_custody: read failed for %s", name, exc_info=True)
        return None


def delete_protected_key(name: str, *, sec: Any = None) -> bool:
    """Explicit removal (the rotation primitive). True iff an item was deleted."""
    s = sec if sec is not None else _default_sec()
    if s is None:
        return False
    try:
        return int(s.delete_item(str(name))) == 0
    except Exception:
        logger.warning("key_custody: delete failed for %s", name, exc_info=True)
        return False


def load_or_create_protected_key(name: str, *, generate=None,
                                 reason: str = "anyuse vault key access",
                                 sec: Any = None) -> Optional[bytes]:
    """The custody entrypoint entity_vault consults when v2 is armed.

    Read (OS presence prompt) → return; absent → generate (default: a fresh
    Fernet key) + store under presence custody + return. None on ANY refusal /
    failure — the caller falls back to the v1 chain, and a protected item is
    never silently replaced by an unprotected one."""
    s = sec if sec is not None else _default_sec()
    if s is None:
        return None
    existing = read_protected_key(name, reason=reason, sec=s)
    if existing is not None:
        return existing
    # Distinguish "absent" from "present but refused": a refused read must NOT
    # mint a second key (data written under the old key would orphan). The add
    # attempt below is that probe — a DUPLICATE status means the read was
    # refused, and we return None instead of minting.
    if generate is None:
        from cryptography.fernet import Fernet
        secret = Fernet.generate_key()
    else:
        secret = generate()
    if store_protected_key(name, secret, sec=s):
        logger.info("key_custody: minted OS-custody key %s (presence-gated)", name)
        return bytes(secret)
    # Duplicate (the read was REFUSED, not absent) or store failure → None.
    return None


def migrate_key(name: str, current: bytes, *, sec: Any = None) -> bool:
    """Upgrade an EXISTING v1 key into OS custody (same bytes — no data
    re-encryption needed). True iff the protected item now stores ``current``.
    The v1 copy is left in place deliberately: the caller decides when the
    degraded fallback may be retired."""
    if not current:
        return False
    return store_protected_key(name, bytes(current), sec=sec)
