"""Vault custody v2 — envelope encryption + swappable decrypt-key providers.

v1 custody is symmetric Fernet: the same ``k_enc`` that ENCRYPTS at capture
time also DECRYPTS — the always-running capture process necessarily holds the
key that unlocks the whole vault history. v2 splits the directions
ECIES-style:

    encrypt (capture, ambient, promptless) = P-256 PUBLIC key — not a secret
    decrypt (deref, rare, presence-gated)  = P-256 PRIVATE key, fetched
                                             just-in-time per deref

so compromising the capture process lets an attacker mint NEW ciphertexts but
not read history without also passing the deref path's presence gate.

Providers behind one seam:

- **SoftwareEnvelopeCustody** (active in this build): private-key PEM under
  the same Keychain/key-file custody chain as k_tok/k_enc
  (``entity_vault.get/set_named_secret``). Presence stays app-enforced
  (LAContext), exactly like v1 — the v2sw gain is capture-key hygiene plus
  the provider seam the OS-enforced tier drops into.
- **Enclave / ACL-item custody** (``AUV2SE``, feature-detected, NOT built
  here): Secure-Enclave P-256 (``SecKeyCreateRandomKey`` with
  ``kSecAttrTokenIDSecureEnclave`` + ``SecAccessControlCreateWithFlags``
  userPresence) where the OS itself prompts inside every
  ``SecKeyCreateDecryptedData``. Needs pyobjc-framework-Security and
  realistically a code-signed helper for the keychain entitlements.
  ``EntityVault.deref`` already skips the
  app-level prompt for ``AUV2SE`` blobs (the OS owns it), so the provider is
  a drop-in.

Wire format — ``entities.ciphertext`` is sniffed per row, so v1 rows keep
working forever and migration can be lazy:

    b"gAAAA..." (Fernet token)                        v1 symmetric
    b"AUV2SW\\x01" + eph_pub(65) + nonce(12) + ct     v2 software envelope
    b"AUV2SE\\x01" + opaque SecKey ECIES blob         v2 enclave (future)

CLI:
    python -m anyuse.watcher.vault_custody --status    # mode + row counts
    python -m anyuse.watcher.vault_custody --enable    # keygen + sidecar
    python -m anyuse.watcher.vault_custody --migrate   # re-encrypt v1 rows
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from anyuse.paths import anyuse_secret_path

MAGIC_SW = b"AUV2SW\x01"
MAGIC_SE = b"AUV2SE\x01"
MAGIC_ANY = b"AUV2"

_HKDF_INFO = b"anyuse-entity-vault-v2"
_PRIV_SECRET_NAME = "k_enc_v2_priv"
CUSTODY_PATH = anyuse_secret_path("entity_vault_custody.json")

_CURVE = ec.SECP256R1()
_EPH_PUB_LEN = 65   # SEC1 uncompressed point on P-256
_NONCE_LEN = 12


# ---------------------------------------------------------------------------
# The envelope primitive (software path)
# ---------------------------------------------------------------------------

def _derive_key(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_HKDF_INFO).derive(shared)


def envelope_encrypt(public_key: ec.EllipticCurvePublicKey,
                     plaintext: bytes) -> bytes:
    """ECDH(ephemeral, static-pub) → HKDF-SHA256 → AES-256-GCM."""
    eph = ec.generate_private_key(_CURVE)
    shared = eph.exchange(ec.ECDH(), public_key)
    key = _derive_key(shared)
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, MAGIC_SW)
    eph_pub = eph.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return MAGIC_SW + eph_pub + nonce + ct


def envelope_decrypt(private_key: ec.EllipticCurvePrivateKey,
                     blob: bytes) -> bytes:
    """Inverse of :func:`envelope_encrypt`. Raises ValueError on anything
    malformed, tampered, or keyed differently."""
    if not blob.startswith(MAGIC_SW):
        raise ValueError("not an AUV2SW envelope")
    body = blob[len(MAGIC_SW):]
    if len(body) < _EPH_PUB_LEN + _NONCE_LEN + 16:
        raise ValueError("truncated envelope")
    eph_pub_bytes = body[:_EPH_PUB_LEN]
    nonce = body[_EPH_PUB_LEN:_EPH_PUB_LEN + _NONCE_LEN]
    ct = body[_EPH_PUB_LEN + _NONCE_LEN:]
    try:
        eph_pub = ec.EllipticCurvePublicKey.from_encoded_point(
            _CURVE, eph_pub_bytes)
        shared = private_key.exchange(ec.ECDH(), eph_pub)
        return AESGCM(_derive_key(shared)).decrypt(nonce, ct, MAGIC_SW)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"envelope decrypt failed: {e}")


# ---------------------------------------------------------------------------
# Custody providers
# ---------------------------------------------------------------------------

class SoftwareEnvelopeCustody:
    """v2sw: promptless public-key encrypt; private key fetched just-in-time
    per decrypt via ``private_provider``.

    Default provider is :func:`os_private_provider` — the OS-custody chain:
    when ``ANYUSE_VAULT_CUSTODY=os`` is armed AND the Security framework is
    present, the key comes from a SecAccessControl userPresence item
    (``key_custody.py``, the macOS prompt fires INSIDE the read; an existing
    key-file PEM auto-migrates in on first use) and a refusal FAILS CLOSED;
    otherwise the vault's named-secret chain (keyring → 0600 key file).

    ``os_enforced`` is a property: True exactly when the key fetch will be
    OS-prompted — ``EntityVault.deref`` then skips the app-level LAContext
    prompt to avoid double-asking."""

    mode = "v2sw"

    def __init__(self, public_pem: str,
                 private_provider: Optional[Callable[[], Optional[str]]] = None,
                 sec: Any = None):
        self._public = serialization.load_pem_public_key(public_pem.encode())
        self._sec = sec  # injectable Security shim, tests only
        self._private_provider = (
            private_provider
            or (lambda: os_private_provider(sec=self._sec)))

    @property
    def os_enforced(self) -> bool:
        try:
            from anyuse.watcher import key_custody
            return key_custody.enabled() and key_custody.available(self._sec)
        except Exception:
            return False

    def encrypt(self, plaintext: bytes) -> bytes:
        return envelope_encrypt(self._public, plaintext)

    def decrypt(self, blob: bytes) -> bytes:
        pem = self._private_provider()
        if not pem:
            raise ValueError("v2 private key unavailable")
        private = serialization.load_pem_private_key(pem.encode(), password=None)
        return envelope_decrypt(private, blob)


_OS_PRIV_NAME = "v2sw_private_key"
_OS_READ_REASON = "anyuse: unlock the entity-vault private key"


def os_private_provider(sec: Any = None) -> Optional[str]:
    """The default private-key chain (see SoftwareEnvelopeCustody docstring).

    Armed + framework present:
      - key-file PEM exists, OS item absent → auto-migrate (store under
        presence custody) and return it;
      - OS item exists → presence-prompted read;
      - refusal → None. FAIL CLOSED — falling back on refusal would make the
        OS prompt decorative.
    Not armed (or framework absent, logged): the v1 named-secret chain.
    """
    from anyuse.watcher.entity_vault import get_named_secret
    try:
        from anyuse.watcher import key_custody
        if key_custody.enabled():
            if key_custody.available(sec):
                pem = get_named_secret(_PRIV_SECRET_NAME)
                if pem:
                    data = key_custody.load_or_create_protected_key(
                        _OS_PRIV_NAME, generate=lambda: pem.encode(),
                        reason=_OS_READ_REASON, sec=sec)
                else:
                    data = key_custody.read_protected_key(
                        _OS_PRIV_NAME, reason=_OS_READ_REASON, sec=sec)
                return data.decode() if data else None
            logger.warning("vault_custody: ANYUSE_VAULT_CUSTODY=os but the "
                           "Security framework is unavailable — key-file "
                           "fallback (degraded)")
    except Exception as e:
        logger.warning("vault_custody: os custody consult failed (%s); "
                       "key-file fallback", e)
    return get_named_secret(_PRIV_SECRET_NAME)


def _default_private_provider() -> Optional[str]:
    return os_private_provider()


# ---------------------------------------------------------------------------
# Sidecar (mode + public key — nothing secret) and lifecycle
# ---------------------------------------------------------------------------

def load_custody(
    custody_path: Optional[Path] = None,
    private_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Optional[SoftwareEnvelopeCustody]:
    """The custody configured on this machine, or None (pure v1)."""
    path = Path(custody_path) if custody_path else CUSTODY_PATH
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if data.get("mode") == "v2sw" and data.get("public_pem"):
            return SoftwareEnvelopeCustody(data["public_pem"], private_provider)
        logger.warning("vault_custody: unrecognised sidecar mode %r in %s",
                       data.get("mode"), path)
        return None
    except Exception as e:
        logger.warning("vault_custody: sidecar unreadable (%s); staying v1", e)
        return None


def enable_v2_software(
    custody_path: Optional[Path] = None,
    secret_setter: Optional[Callable[[str, str], None]] = None,
    private_provider: Optional[Callable[[], Optional[str]]] = None,
) -> SoftwareEnvelopeCustody:
    """Generate the v2 keypair and write the sidecar. Idempotent: an existing
    sidecar is loaded, never overwritten (rotation is an explicit act)."""
    path = Path(custody_path) if custody_path else CUSTODY_PATH
    existing = load_custody(path, private_provider)
    if existing is not None:
        return existing

    private = ec.generate_private_key(_CURVE)
    priv_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    if secret_setter is None:
        from anyuse.watcher.entity_vault import set_named_secret
        secret_setter = set_named_secret
    secret_setter(_PRIV_SECRET_NAME, priv_pem)  # raises → nothing written

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mode": "v2sw",
        "public_pem": pub_pem,
        "created_at": datetime.now().isoformat(),
    }, indent=2))
    logger.info("vault_custody: v2sw enabled (sidecar %s)", path)
    return SoftwareEnvelopeCustody(pub_pem, private_provider)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_status() -> dict:
    from anyuse.watcher.entity_vault import EntityVault
    from anyuse.watcher import key_custody
    custody = load_custody()
    vault = EntityVault()
    counts = {"v1": 0, "v2sw": 0, "v2se": 0}
    conn = vault._connect()
    try:
        for (blob,) in conn.execute("SELECT ciphertext FROM entities"):
            raw = bytes(blob) if not isinstance(blob, bytes) else blob
            if raw.startswith(MAGIC_SW):
                counts["v2sw"] += 1
            elif raw.startswith(MAGIC_SE):
                counts["v2se"] += 1
            else:
                counts["v1"] += 1
    finally:
        conn.close()
    return {
        "mode": custody.mode if custody else "v1",
        "rows": counts,
        "os_custody": {
            "armed": key_custody.enabled(),
            "framework_available": key_custody.available(),
        },
    }


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="anyuse.watcher.vault_custody",
        description="Entity-vault custody management (v1 Fernet ↔ v2 envelope).")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--enable", action="store_true",
                        help="generate the v2 keypair + sidecar (idempotent)")
    parser.add_argument("--migrate", action="store_true",
                        help="re-encrypt v1 rows under the v2 public key")
    parser.add_argument("--migrate-os", action="store_true",
                        help="copy the v2sw private key into OS-enforced "
                             "presence custody (needs ANYUSE_VAULT_CUSTODY=os "
                             "+ pyobjc-framework-Security)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    if args.enable:
        custody = enable_v2_software()
        print(f"custody: {custody.mode} enabled")
    if args.migrate_os:
        from anyuse.watcher import key_custody
        from anyuse.watcher.entity_vault import get_named_secret
        pem = get_named_secret(_PRIV_SECRET_NAME)
        if not pem:
            print("no v2sw private key to migrate — run --enable first")
            return 1
        if not key_custody.available():
            print("Security framework unavailable — install "
                  "pyobjc-framework-Security first")
            return 1
        if key_custody.migrate_key(_OS_PRIV_NAME, pem.encode()):
            print(f"private key stored under OS presence custody "
                  f"({_OS_PRIV_NAME}); arm with ANYUSE_VAULT_CUSTODY=os. "
                  "The key-file copy remains until you retire it explicitly.")
        else:
            print("not migrated (already present under OS custody, or the "
                  "store failed — see log)")
    if args.migrate:
        from anyuse.watcher.entity_vault import EntityVault
        vault = EntityVault()
        if vault._custody is None:
            print("no v2 custody configured — run --enable first")
            return 1
        n = vault.migrate_custody()
        print(f"migrated {n} rows to {vault._custody.mode}")
    if args.status or not (args.enable or args.migrate):
        print(json.dumps(_cli_status(), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
