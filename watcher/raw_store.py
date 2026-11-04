"""Raw-artifact encrypt-at-rest.

The entity vault covers durable *abstractions*; this module covers the raw
replay-fidelity artifacts that never cross the boundary but sit on disk:
session.json TagEvents, historical raw observations / intent analyses, and
click-anchor PNGs. Whole-file Fernet with a third
Keychain-held key ``k_raw`` (same custody chain as k_tok/k_enc —
``entity_vault.load_or_create_named_key``).

On-disk format: ``ANYUSE-ENC1\\n`` magic + a Fernet token. The magic makes
detection unambiguous, so reads are TRANSPARENT: a legacy plaintext file (no
magic) passes through unchanged — pre-existing sessions keep loading.

Fail-closed policy: when no crypto is constructible (cryptography missing /
no key store), encrypted WRITES ARE SKIPPED with a loud warning — a raw
artifact is never silently written in the clear. Reads of magic-prefixed files
without a key return None.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken
    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - crypto is in requirements
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

MAGIC = b"ANYUSE-ENC1\n"

_PathLike = Union[str, Path]


class RawArtifactCrypto:
    """Fernet wrapper for whole-file raw-artifact encryption.

    ``key`` is injectable for tests; production defaults to the Keychain/
    key-file-held ``k_raw`` via :func:`entity_vault.load_or_create_named_key`.
    """

    KEY_NAME = "k_raw"

    def __init__(self, key: Optional[bytes] = None):
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("raw_store requires the cryptography package")
        if key is None:
            from anyuse.watcher.entity_vault import load_or_create_named_key
            key = load_or_create_named_key(self.KEY_NAME)
        self._fernet = Fernet(key)

    def encrypt_bytes(self, data: bytes) -> bytes:
        return MAGIC + self._fernet.encrypt(data)

    def decrypt_bytes(self, blob: bytes) -> Optional[bytes]:
        """Transparent decrypt: magic-prefixed → decrypted payload (None on a
        wrong key / corrupt token); anything else → returned as-is (legacy
        plaintext)."""
        if not blob.startswith(MAGIC):
            return blob
        try:
            return self._fernet.decrypt(blob[len(MAGIC):])
        except (InvalidToken, Exception):
            logger.warning("raw_store: failed to decrypt an %s artifact "
                           "(wrong key or corrupt file)", MAGIC.strip().decode())
            return None


# ---------------------------------------------------------------------------
# Module-level lazy default (mirrors entity_vault.get_default_vault):
# best-effort, never raises; None means "skip encrypted writes".
# ---------------------------------------------------------------------------

_default_crypto: Optional[RawArtifactCrypto] = None
_default_crypto_failed = False


def set_default_raw_crypto(crypto: Optional[RawArtifactCrypto]) -> None:
    """Pin the process-wide raw-artifact crypto, or reset it with ``None``.

    The test seam for the design notes (g). Without it, any code path that
    writes a raw artefact without passing ``crypto=`` — ``batch_watcher``'s
    intent analysis is one — lazily constructs ``RawArtifactCrypto()``, which
    mints ``k_raw`` through ``keyring`` and so writes into the user's login
    Keychain from a test run. Under a sandboxed HOME the Security framework
    cannot find a keychain and raises a **dialog on the user's desktop**; that
    happened once during this work.

    This only chooses which key the process uses. It reads no key material, and
    a process that never calls it behaves exactly as before.
    """
    global _default_crypto, _default_crypto_failed
    _default_crypto = crypto
    _default_crypto_failed = False


def get_default_raw_crypto() -> Optional[RawArtifactCrypto]:
    global _default_crypto, _default_crypto_failed
    if _default_crypto is not None:
        return _default_crypto
    if _default_crypto_failed:
        return None
    try:
        _default_crypto = RawArtifactCrypto()
        return _default_crypto
    except Exception as e:
        logger.warning("raw_store: default crypto unavailable (%s); raw "
                       "artifact writes will be SKIPPED (fail-closed)", e)
        _default_crypto_failed = True
        return None


# ---------------------------------------------------------------------------
# Convenience read/write — every raw-artifact IO site goes through these four.
# ---------------------------------------------------------------------------

def write_bytes_encrypted(
    path: _PathLike,
    data: bytes,
    crypto: Optional[RawArtifactCrypto] = None,
) -> bool:
    """Encrypt-and-write. Returns False (nothing written) when no crypto is
    available — fail-closed, never plaintext."""
    c = crypto if crypto is not None else get_default_raw_crypto()
    if c is None:
        logger.warning("raw_store: skipping write of %s (no crypto)", path)
        return False
    try:
        Path(path).write_bytes(c.encrypt_bytes(data))
        return True
    except Exception as e:
        logger.warning("raw_store: write failed for %s: %s", path, e)
        return False


def read_bytes_transparent(
    path: _PathLike,
    crypto: Optional[RawArtifactCrypto] = None,
) -> Optional[bytes]:
    """Read a raw artifact: encrypted files are decrypted, legacy plaintext
    passes through. None when the file is missing/undecryptable."""
    p = Path(path)
    try:
        blob = p.read_bytes()
    except OSError:
        return None
    if not blob.startswith(MAGIC):
        return blob
    c = crypto if crypto is not None else get_default_raw_crypto()
    if c is None:
        logger.warning("raw_store: cannot decrypt %s (no crypto)", path)
        return None
    return c.decrypt_bytes(blob)


def write_json_encrypted(
    path: _PathLike,
    obj: Any,
    crypto: Optional[RawArtifactCrypto] = None,
) -> bool:
    try:
        data = json.dumps(obj, indent=2, default=str).encode("utf-8")
    except (TypeError, ValueError) as e:
        logger.warning("raw_store: json encode failed for %s: %s", path, e)
        return False
    return write_bytes_encrypted(path, data, crypto)


def read_json_transparent(
    path: _PathLike,
    crypto: Optional[RawArtifactCrypto] = None,
) -> Optional[Any]:
    data = read_bytes_transparent(path, crypto)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
