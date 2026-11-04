"""Tests for vault custody v2.

Covers:
  - the software envelope primitive: roundtrip, AUV2SW wire format, tamper
    detection (any flipped byte fails), wrong-key failure
  - enable_v2_software: keypair + sidecar, idempotent (second call loads,
    never rotates), private key goes through the injected secret store
  - EntityVault + custody: new entities are envelope-encrypted (not Fernet),
    deref roundtrips through the just-in-time private-key provider, and the
    app-level presence gate still fires for vault_presence classes (v2sw is
    app-enforced, like v1)
  - mixed vaults: v1 Fernet rows and v2 envelope rows coexist in one DB,
    deref sniffs per row
  - migrate_custody: re-encrypts exactly the v1 rows, derefs still work,
    re-run migrates nothing
  - fail-closed: an AUV2 blob in a vault with NO custody derefs as
    decrypt_failed (audited), never plaintext
  - AUV2SE (OS-enforced) blobs skip the app-level presence prompt — the OS
    owns the prompt inside the provider's decrypt

Self-executing (pytest is not installed in this env). Keys/sidecars/secret
stores are always injected — tests never touch the Keychain, the real
~/.anyuse key file, or the real custody sidecar.

Run from the anyuse package dir:
    cd anyuse && .venv/bin/python test_vault_custody.py
"""

import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec

from anyuse.watcher.entity_vault import EntityVault, marker
from anyuse.watcher.vault_custody import (
    MAGIC_SE,
    MAGIC_SW,
    SoftwareEnvelopeCustody,
    enable_v2_software,
    envelope_decrypt,
    envelope_encrypt,
    load_custody,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def _keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    return private, private.public_key()


def _vault(td, custody=None, presence=None):
    return EntityVault(
        db_path=pathlib.Path(td) / "vault.db",
        k_tok=b"0" * 32,
        k_enc=Fernet.generate_key(),
        presence_provider=presence or (lambda reason: True),
        custody=custody,
    )


class _SecretDict:
    """In-memory stand-in for the keyring/key-file secret chain."""

    def __init__(self):
        self.data = {}

    def set(self, name, value):
        self.data[name] = value

    def get(self, name=None):
        # provider form: no-arg callable returning the private PEM
        return self.data.get(name or "k_enc_v2_priv")


# ---------------------------------------------------------------------------

def test_envelope_primitive():
    private, public = _keypair()
    blob = envelope_encrypt(public, b"the secret value")
    check("blob carries AUV2SW magic", blob.startswith(MAGIC_SW))
    check("plaintext not in blob", b"the secret value" not in blob)
    check("roundtrip", envelope_decrypt(private, blob) == b"the secret value")

    # tamper anywhere → ValueError
    tampered_ok = 0
    for pos in (len(MAGIC_SW) + 2, len(blob) - 3):  # eph-pub region, ct region
        broken = bytearray(blob)
        broken[pos] ^= 0x01
        try:
            envelope_decrypt(private, bytes(broken))
            tampered_ok += 1
        except ValueError:
            pass
    check("tampered blobs fail", tampered_ok == 0)

    other_private, _ = _keypair()
    try:
        envelope_decrypt(other_private, blob)
        wrong_key_ok = True
    except ValueError:
        wrong_key_ok = False
    check("wrong private key fails", not wrong_key_ok)

    try:
        envelope_decrypt(private, MAGIC_SW + b"short")
        check("truncated blob fails", False)
    except ValueError:
        check("truncated blob fails", True)


def test_enable_idempotent():
    with tempfile.TemporaryDirectory() as td:
        sidecar = pathlib.Path(td) / "custody.json"
        secrets = _SecretDict()
        c1 = enable_v2_software(custody_path=sidecar,
                                secret_setter=secrets.set,
                                private_provider=secrets.get)
        check("sidecar written", sidecar.exists())
        check("private key stored via injected setter",
              "k_enc_v2_priv" in secrets.data
              and "PRIVATE KEY" in secrets.data["k_enc_v2_priv"])

        c2 = enable_v2_software(custody_path=sidecar,
                                secret_setter=secrets.set,
                                private_provider=secrets.get)
        blob = c1.encrypt(b"x")
        check("second enable loads the SAME keypair (c2 decrypts c1)",
              c2.decrypt(blob) == b"x")

        loaded = load_custody(sidecar, private_provider=secrets.get)
        check("load_custody roundtrips", loaded is not None
              and loaded.decrypt(blob) == b"x")
        check("missing sidecar → None (pure v1)",
              load_custody(pathlib.Path(td) / "nope.json") is None)


def test_vault_with_custody():
    with tempfile.TemporaryDirectory() as td:
        sidecar = pathlib.Path(td) / "custody.json"
        secrets = _SecretDict()
        custody = enable_v2_software(custody_path=sidecar,
                                     secret_setter=secrets.set,
                                     private_provider=secrets.get)
        prompts = []

        def presence(reason):
            prompts.append(reason)
            return True

        vault = _vault(td, custody=custody, presence=presence)
        token = vault.tokenize("Alice Smith", "person", app="Mail")

        conn = vault._connect()
        (blob,) = conn.execute(
            "SELECT ciphertext FROM entities WHERE token = ?",
            (token,)).fetchone()
        conn.close()
        raw = blob if isinstance(blob, bytes) else bytes(blob)
        check("new entity is envelope-encrypted", raw.startswith(MAGIC_SW))

        vault.grant("brain-local", ["person"], requires_presence=True)
        result = vault.deref(token, "brain-local", purpose="t")
        check("deref roundtrips through the provider",
              result.get("ok") and result["value"] == "Alice Smith",
              str(result))
        check("app-level presence still fires for v2sw", len(prompts) == 1)


def test_mixed_rows_and_migration():
    with tempfile.TemporaryDirectory() as td:
        k_tok, k_enc = b"1" * 32, Fernet.generate_key()
        # v1 vault writes two Fernet rows
        v1 = EntityVault(db_path=pathlib.Path(td) / "vault.db",
                         k_tok=k_tok, k_enc=k_enc,
                         presence_provider=lambda r: True)
        t_old = v1.tokenize("acme.example.com", "url")
        v1.tokenize("Bob Jones", "person")

        # same DB, now WITH custody: new rows are v2, old rows still deref
        secrets = _SecretDict()
        custody = enable_v2_software(
            custody_path=pathlib.Path(td) / "custody.json",
            secret_setter=secrets.set, private_provider=secrets.get)
        v2 = EntityVault(db_path=pathlib.Path(td) / "vault.db",
                         k_tok=k_tok, k_enc=k_enc,
                         presence_provider=lambda r: True, custody=custody)
        t_new = v2.tokenize("carol@example.com", "email_address")
        v2.grant("brain-local",
                 ["url", "person", "email_address"], requires_presence=False)

        r_old = v2.deref(t_old, "brain-local")
        r_new = v2.deref(t_new, "brain-local")
        check("v1 row derefs in a v2 vault (sniffed per row)",
              r_old.get("ok") and r_old["value"] == "acme.example.com",
              str(r_old))
        check("v2 row derefs", r_new.get("ok")
              and r_new["value"] == "carol@example.com", str(r_new))

        migrated = v2.migrate_custody()
        check("migrate re-encrypts exactly the 2 v1 rows", migrated == 2,
              f"migrated={migrated}")
        conn = v2._connect()
        kinds = [bytes(b).startswith(MAGIC_SW) for (b,) in
                 conn.execute("SELECT ciphertext FROM entities")]
        conn.close()
        check("all rows are v2 after migration", all(kinds))
        check("derefs still work post-migration",
              v2.deref(t_old, "brain-local").get("ok")
              and v2.deref(t_new, "brain-local").get("ok"))
        check("re-run migrates nothing", v2.migrate_custody() == 0)


def test_fail_closed_without_custody():
    with tempfile.TemporaryDirectory() as td:
        secrets = _SecretDict()
        custody = enable_v2_software(
            custody_path=pathlib.Path(td) / "custody.json",
            secret_setter=secrets.set, private_provider=secrets.get)
        k_tok, k_enc = b"2" * 32, Fernet.generate_key()
        v2 = EntityVault(db_path=pathlib.Path(td) / "vault.db",
                         k_tok=k_tok, k_enc=k_enc,
                         presence_provider=lambda r: True, custody=custody)
        token = v2.tokenize("Dora", "person")
        # A fresh vault over the same DB with NO custody (injected keys, so
        # no sidecar auto-load) cannot read the v2 row — and says so.
        v1_only = EntityVault(db_path=pathlib.Path(td) / "vault.db",
                              k_tok=k_tok, k_enc=k_enc,
                              presence_provider=lambda r: True)
        v1_only.grant("brain-local", ["person"], requires_presence=False)
        result = v1_only.deref(token, "brain-local")
        check("AUV2 blob without custody → decrypt_failed",
              result == {"ok": False, "reason": "decrypt_failed"}, str(result))
        audits = [a for a in v1_only.audit_log() if a["token"] == token]
        check("the refusal is audited", audits and not audits[0]["allowed"])


def test_partial_keyfile_is_supported():
    """The degraded key file is a shared bag of named keys — k_raw (raw_store)
    or a v2 private key can legitimately create it FIRST. _load_or_create_keys
    must fill in the missing vault keys and PRESERVE the others (regression:
    it KeyError'd on a k_raw-only file)."""
    import json as _json
    from anyuse.watcher import entity_vault as ev

    with tempfile.TemporaryDirectory() as td:
        keyfile = pathlib.Path(td) / "entity_vault.keys"
        keyfile.write_text(_json.dumps({"k_raw": "PRESERVED"}))
        saved = ev._KEYFILE
        ev._KEYFILE = keyfile
        # This test exercises the DEGRADED (key-file) branch — force it even
        # on a box where keyring is installed (the real Keychain must never
        # be touched by a test).
        saved_kr = ev.KEYRING_AVAILABLE
        ev.KEYRING_AVAILABLE = False
        try:
            k_tok, k_enc = ev._load_or_create_keys()
            check("keys created from a partial file",
                  len(k_tok) == 32 and k_enc)
            data = _json.loads(keyfile.read_text())
            check("existing named keys preserved",
                  data.get("k_raw") == "PRESERVED")
            check("vault keys now in the file",
                  "k_tok" in data and "k_enc" in data)
            k_tok2, k_enc2 = ev._load_or_create_keys()
            check("stable across reloads",
                  (k_tok, k_enc) == (k_tok2, k_enc2))
        finally:
            ev._KEYFILE = saved
            ev.KEYRING_AVAILABLE = saved_kr


def test_os_enforced_skips_app_prompt():
    class _OsEnforcedCustody:
        mode = "v2se"
        os_enforced = True

        def encrypt(self, data):
            return MAGIC_SE + data[::-1]  # stand-in for the SecKey blob

        def decrypt(self, blob):
            assert blob.startswith(MAGIC_SE)
            return blob[len(MAGIC_SE):][::-1]

    with tempfile.TemporaryDirectory() as td:
        prompts = []

        def presence(reason):
            prompts.append(reason)
            return False  # would DENY if ever consulted

        vault = _vault(td, custody=_OsEnforcedCustody(), presence=presence)
        token = vault.tokenize("Eve Adams", "person")
        vault.grant("brain-local", ["person"], requires_presence=True)
        result = vault.deref(token, "brain-local")
        check("AUV2SE deref succeeds without the app prompt",
              result.get("ok") and result["value"] == "Eve Adams", str(result))
        check("app presence provider was never consulted", prompts == [])


def test_os_private_provider_chain():
    """The key_custody wiring (VAULT_V2_CUSTODY_HANDOFF item 1): armed+available
    → presence-gated OS item (auto-migrating the key-file PEM in on first use),
    refusal FAILS CLOSED; unarmed → the named-secret chain."""
    import os as _os
    from test_key_custody import FakeSec
    from anyuse.watcher import vault_custody as vc
    from anyuse.watcher import entity_vault as ev

    # Point the named-secret chain at a temp key file (keyring absent here).
    with tempfile.TemporaryDirectory() as td:
        import json as _json
        keyfile = pathlib.Path(td) / "entity_vault.keys"
        keyfile.write_text(_json.dumps({"k_enc_v2_priv": "PEM-FROM-KEYFILE"}))
        saved_keyfile, ev._KEYFILE = ev._KEYFILE, keyfile
        # Named-secret reads must resolve from THIS key file, not whatever the
        # machine's real keyring holds (keyring may be installed).
        saved_kr, ev.KEYRING_AVAILABLE = ev.KEYRING_AVAILABLE, False
        saved_env = _os.environ.get("ANYUSE_VAULT_CUSTODY")
        try:
            # Unarmed → key-file chain, no OS reads.
            _os.environ.pop("ANYUSE_VAULT_CUSTODY", None)
            sec = FakeSec()
            check("unarmed → key-file PEM",
                  vc.os_private_provider(sec=sec) == "PEM-FROM-KEYFILE")
            check("unarmed → OS custody never consulted", sec.reads == [])

            # Armed + available + key-file PEM + no OS item → auto-migrate.
            _os.environ["ANYUSE_VAULT_CUSTODY"] = "os"
            got = vc.os_private_provider(sec=sec)
            check("armed: returns the PEM", got == "PEM-FROM-KEYFILE")
            check("armed: PEM auto-migrated under presence custody",
                  sec.items.get(vc._OS_PRIV_NAME) == b"PEM-FROM-KEYFILE")

            # Subsequent read comes from the OS item (presence-prompted).
            sec.reads.clear()
            got2 = vc.os_private_provider(sec=sec)
            check("armed: subsequent fetch reads the OS item",
                  got2 == "PEM-FROM-KEYFILE" and len(sec.reads) >= 1)

            # Refusal → None, and NO fallback to the key file.
            sec.refuse_presence = True
            check("armed: refusal fails CLOSED (no key-file fallback)",
                  vc.os_private_provider(sec=sec) is None)
            sec.refuse_presence = False

            # End-to-end: a v2sw custody armed with OS custody skips the
            # app-level prompt (the OS owns it) and derefs through the shim.
            # A REAL keypair replaces the placeholder PEM in the key file, and
            # a FRESH shim is used (OS items are never overwritten, so the
            # placeholder that auto-migrated above must not shadow the real
            # key).
            enable_v2_software(
                custody_path=pathlib.Path(td) / "custody.json",
                secret_setter=lambda n, v: keyfile.write_text(
                    _json.dumps({**_json.loads(keyfile.read_text()), n: v})),
                private_provider=None)  # default chain
            sec2 = FakeSec()
            custody = vc.SoftwareEnvelopeCustody(
                _json.loads((pathlib.Path(td) / "custody.json").read_text())
                ["public_pem"], sec=sec2)
            check("armed custody reports os_enforced", custody.os_enforced)

            prompts = []
            vault = _vault(td, custody=custody,
                           presence=lambda r: prompts.append(r) or False)
            token = vault.tokenize("Frank Ocean", "person")
            vault.grant("brain-local", ["person"], requires_presence=True)
            result = vault.deref(token, "brain-local")
            check("deref succeeds via the OS-custody provider",
                  result.get("ok") and result["value"] == "Frank Ocean",
                  str(result))
            check("app-level prompt skipped when OS custody owns it",
                  prompts == [])

            # Framework absent → degraded fallback to the key file, vault
            # keeps working. Absence is SIMULATED by stubbing the default-shim
            # resolver: where pyobjc is installed the
            # real armed path would auto-migrate this test PEM into the actual
            # login Keychain — a test must never do that.
            from anyuse.watcher import key_custody as kc
            saved_default_sec = kc._default_sec
            kc._default_sec = lambda: None
            try:
                check("armed but framework absent → key-file fallback (degraded)",
                      vc.os_private_provider() ==
                      _json.loads(keyfile.read_text())["k_enc_v2_priv"])
            finally:
                kc._default_sec = saved_default_sec
        finally:
            ev._KEYFILE = saved_keyfile
            ev.KEYRING_AVAILABLE = saved_kr
            if saved_env is None:
                _os.environ.pop("ANYUSE_VAULT_CUSTODY", None)
            else:
                _os.environ["ANYUSE_VAULT_CUSTODY"] = saved_env


def test_keyring_seeds_from_existing_keyfile():
    """Continuity when keyring becomes available AFTER keys were minted into
    the degraded key file: _load_or_create_keys
    and load_or_create_named_key must seed the keyring from the file's values,
    NOT mint fresh keys — a fresh mint would orphan every existing token,
    Fernet blob, and raw artifact."""
    import json as _json
    from base64 import urlsafe_b64decode, urlsafe_b64encode

    from anyuse.watcher import entity_vault as ev

    class FakeKeyring:
        def __init__(self):
            self.store = {}

        def get_password(self, service, name):
            return self.store.get((service, name))

        def set_password(self, service, name, value):
            self.store[(service, name)] = value

    file_tok = urlsafe_b64encode(b"T" * 32).decode()
    file_enc = "ZmFrZS1mZXJuZXQta2V5LWZha2UtZmVybmV0LWtleS0="
    file_raw = "cmF3LWtleS1yYXcta2V5LXJhdy1rZXktcmF3LWtleS0="

    with tempfile.TemporaryDirectory() as td:
        keyfile = pathlib.Path(td) / "entity_vault.keys"
        keyfile.write_text(_json.dumps(
            {"k_tok": file_tok, "k_enc": file_enc, "k_raw": file_raw}))
        fake = FakeKeyring()
        saved = (ev._KEYFILE, ev.KEYRING_AVAILABLE, ev.keyring)
        ev._KEYFILE, ev.KEYRING_AVAILABLE, ev.keyring = keyfile, True, fake
        try:
            k_tok, k_enc = ev._load_or_create_keys()
            check("k_tok seeded from key file (not fresh-minted)",
                  k_tok == urlsafe_b64decode(file_tok.encode()))
            check("k_enc seeded from key file",
                  k_enc == file_enc.encode())
            check("seeded values landed in the keyring",
                  fake.store.get((ev._KEYRING_SERVICE, "k_tok")) == file_tok)
            k_raw = ev.load_or_create_named_key("k_raw")
            check("named key (k_raw) seeded from key file",
                  k_raw == file_raw.encode())
            # A key absent EVERYWHERE still mints fresh (the normal path).
            fresh = ev.load_or_create_named_key("k_brand_new")
            check("absent-everywhere named key still mints",
                  bool(fresh) and fresh != file_raw.encode())
        finally:
            ev._KEYFILE, ev.KEYRING_AVAILABLE, ev.keyring = saved


if __name__ == "__main__":
    tests = [
        test_envelope_primitive,
        test_enable_idempotent,
        test_vault_with_custody,
        test_mixed_rows_and_migration,
        test_fail_closed_without_custody,
        test_partial_keyfile_is_supported,
        test_keyring_seeds_from_existing_keyfile,
        test_os_enforced_skips_app_prompt,
        test_os_private_provider_chain,
    ]
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except Exception as e:  # noqa: BLE001 - report, keep going
            FAIL += 1
            print(f"  FAIL {t.__name__} raised: {type(e).__name__}: {e}")
    if FAIL:
        print(f"\n{PASS} passed, {FAIL} failed")
        sys.exit(1)
    print(f"\n{PASS} passed, 0 failed")
    print("ALL VAULT CUSTODY TESTS PASSED")
