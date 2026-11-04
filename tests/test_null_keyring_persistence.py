"""Durable file custody when keyring's installed backend discards writes.

Run only through tools/guarded_tests/run_allowed.sh. When installed, the real
keyring package's null backend and a subclass exercise production custody.
Those cases skip explicitly without that optional dependency; injected-provider
compatibility still runs. All keys, artifacts and sessions stay in scratch.
"""
from __future__ import annotations

from base64 import urlsafe_b64decode
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import traceback
from unittest import SkipTest, mock


def _require_guard():
    import sitecustomize
    assert getattr(sitecustomize, "MARK", None) == "DESKTOP GUARD", \
        "run this file through the guarded test runner"
    assert os.environ.get("PYTHON_KEYRING_BACKEND") == "keyring.backends.null.Keyring", \
        "the guarded runner must select the null keyring backend"
    assert os.environ.get("ANYUSE_DATA_DIR") and os.environ.get("ANYUSE_SECRETS_DIR"), \
        "the guarded runner must redirect data and secret storage"


# Check startup protection BEFORE importing the installed keyring package.
_require_guard()

try:
    import keyring
except ModuleNotFoundError as exc:
    if exc.name != "keyring":
        raise
    keyring = None
    NullKeyring = None
else:
    from keyring.backends.null import Keyring as NullKeyring

from anyuse.watcher import entity_vault as ev
from anyuse.watcher.models import TagEvent, TagType, WatchSession
from anyuse.watcher.raw_store import MAGIC, RawArtifactCrypto
from anyuse.watcher.storage import SessionStorage


if keyring is not None:
    class _NullSubclass(NullKeyring):
        """A configured variant still has the null backend's discard semantics."""
else:
    _NullSubclass = None


def _require_keyring():
    if keyring is None:
        raise SkipTest("optional keyring package is not installed")


@contextmanager
def _scratch_custody():
    scratch_parent = Path(os.environ["ANYUSE_DATA_DIR"]).parent
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="null-keyring-", dir=scratch_parent) as tmp:
        root = Path(tmp)
        keyfile = root / "secrets" / "entity_vault.keys"
        with mock.patch.dict(os.environ, {
                "ANYUSE_DATA_DIR": str(root / "data"),
                "ANYUSE_SECRETS_DIR": str(root / "secrets"),
                "ANYUSE_VAULT_KEYFILE": str(keyfile)}), \
                mock.patch.object(ev, "_KEYFILE", None):
            yield root, keyfile


@contextmanager
def _backend(backend):
    previous = keyring.get_keyring()
    assert isinstance(previous, NullKeyring), "refuse an unguarded real keyring"
    keyring.set_keyring(backend)
    try:
        with mock.patch.object(ev, "keyring", keyring), \
                mock.patch.object(ev, "KEYRING_AVAILABLE", True):
            yield
    finally:
        keyring.set_keyring(previous)


def _assert_null_backend_uses_durable_custody(backend):
    with _scratch_custody() as (_, keyfile), _backend(backend), \
            mock.patch.object(backend, "get_password", wraps=backend.get_password) as reads, \
            mock.patch.object(backend, "set_password", wraps=backend.set_password) as writes:
        assert ev.get_named_secret("missing") is None
        assert not keyfile.exists(), "a read must not mint custody material"
        ev.set_named_secret("test_secret", "persistent fixture secret")
        assert keyfile.exists(), "a no-op keyring write must fall back to a file"
        assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
        assert ev.get_named_secret("test_secret") == "persistent fixture secret"

        tok, enc = ev._load_or_create_keys()
        raw = ev.load_or_create_named_key("k_raw")
        assert len(tok) == 32
        assert len(urlsafe_b64decode(enc)) == len(urlsafe_b64decode(raw)) == 32
        assert enc != raw, "vault encryption and raw artifacts need separate keys"
        assert ev._load_or_create_keys() == (tok, enc)
        assert ev.load_or_create_named_key("k_raw") == raw
        first = RawArtifactCrypto()
        second = RawArtifactCrypto()
        payload = b"durable raw artifact from first independent instance"
        assert second.decrypt_bytes(first.encrypt_bytes(payload)) == payload

        # Updating a secret must preserve the already minted encryption keys.
        ev.set_named_secret("test_secret", "updated fixture secret")
        assert ev.get_named_secret("test_secret") == "updated fixture secret"
        bag = json.loads(keyfile.read_text())
        assert bag["test_secret"] == "updated fixture secret"
        assert urlsafe_b64decode(bag["k_tok"]) == tok
        assert bag["k_enc"].encode() == enc and bag["k_raw"].encode() == raw
        assert ev._load_or_create_keys() == (tok, enc)
        assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
        assert reads.call_count == writes.call_count == 0, \
            "null backends must be bypassed for every custody operation"


def test_actual_null_keyring_persists_secrets_and_keys():
    _require_keyring()
    _assert_null_backend_uses_durable_custody(NullKeyring())


def test_null_keyring_subclass_persists_secrets_and_keys():
    _require_keyring()
    _assert_null_backend_uses_durable_custody(_NullSubclass())


def test_injected_persistent_provider_without_get_keyring_is_honored():
    class PersistentProvider:
        def __init__(self):
            self.values = {}

        def get_password(self, service, name):
            return self.values.get((service, name))

        def set_password(self, service, name, value):
            self.values[service, name] = value

    provider = PersistentProvider()
    assert not hasattr(provider, "get_keyring")
    with _scratch_custody() as (_, keyfile), \
            mock.patch.object(ev, "keyring", provider), \
            mock.patch.object(ev, "KEYRING_AVAILABLE", True):
        ev.set_named_secret("test_secret", "provider-held secret")
        assert ev.get_named_secret("test_secret") == "provider-held secret"
        pair = ev._load_or_create_keys()
        raw = ev.load_or_create_named_key("k_raw")
        assert ev._load_or_create_keys() == pair
        assert ev.load_or_create_named_key("k_raw") == raw
        assert {name for _, name in provider.values} == \
            {"test_secret", "k_tok", "k_enc", "k_raw"}
        assert not keyfile.exists(), "a persistent provider must keep priority"


def test_injected_provider_survives_unavailable_null_backend_classifier():
    class PersistentProvider:
        def __init__(self):
            self.values = {}
            self.backend = object()

        def get_keyring(self):
            return self.backend

        def get_password(self, service, name):
            return self.values.get((service, name))

        def set_password(self, service, name, value):
            self.values[service, name] = value

    original_import = __import__
    classifier_attempts = []

    def without_null_classifier(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "keyring.backends.null":
            classifier_attempts.append(name)
            raise ImportError("fixture optional null classifier is unavailable")
        return original_import(name, globals, locals, fromlist, level)

    provider = PersistentProvider()
    with _scratch_custody() as (_, keyfile), \
            mock.patch.object(ev, "keyring", provider), \
            mock.patch.object(ev, "KEYRING_AVAILABLE", True), \
            mock.patch("builtins.__import__", side_effect=without_null_classifier):
        ev.set_named_secret("test_secret", "provider survives optional import failure")
        assert ev.get_named_secret("test_secret") == \
            "provider survives optional import failure"
        pair = ev._load_or_create_keys()
        raw = ev.load_or_create_named_key("k_raw")
        assert ev._load_or_create_keys() == pair
        assert ev.load_or_create_named_key("k_raw") == raw
        assert {name for _, name in provider.values} == \
            {"test_secret", "k_tok", "k_enc", "k_raw"}
        assert classifier_attempts, "the optional classifier import must be exercised"
        assert not keyfile.exists(), "an unclassified persistent provider keeps priority"


_SESSION_ID = "durable-null-keyring"
_PRIVATE_TEXT = "fixture document survives a fresh process"
_RAW_TEXT = b"fixture raw bytes survive a fresh process"


def _session_worker(action, root):
    _require_guard()
    _require_keyring()
    assert isinstance(keyring.get_keyring(), NullKeyring)
    assert ev.KEYRING_AVAILABLE, "this regression requires installed keyring"
    assert ev._KEYFILE is None, "exercise production path resolution"
    root = Path(root)
    storage = SessionStorage()
    assert storage._crypto is None, "do not inject session encryption"
    assert storage.base_dir.is_relative_to(root)
    raw_path = root / "raw.enc"
    session_path = storage.get_session_dir(_SESSION_ID) / "session.json"
    if action == "write":
        raw_path.write_bytes(RawArtifactCrypto().encrypt_bytes(_RAW_TEXT))
        session = WatchSession(
            id=_SESSION_ID, started_at=datetime(2026, 10, 20, 12), status="stopped",
            events=[TagEvent(id="fixture-event", tag_type=TagType.STEP,
                             timestamp=datetime(2026, 10, 20, 12),
                             data={"text": _PRIVATE_TEXT}, workflow_name="fixture")])
        storage.save(session)
        blob = session_path.read_bytes()
        assert blob.startswith(MAGIC) and _PRIVATE_TEXT.encode() not in blob
        assert raw_path.read_bytes().startswith(MAGIC)
    elif action == "read":
        assert RawArtifactCrypto().decrypt_bytes(raw_path.read_bytes()) == _RAW_TEXT
        session = storage.load(_SESSION_ID)
        assert session is not None, "fresh process must decrypt the saved session"
        assert session.events[0].data["text"] == _PRIVATE_TEXT
        another = SessionStorage()
        assert another.load(_SESSION_ID).to_dict() == session.to_dict()
    else:
        raise AssertionError(action)


def test_encrypted_session_survives_independent_processes_without_crypto_injection():
    _require_keyring()
    with _scratch_custody() as (root, keyfile):
        previous = None
        for action in ("write", "read"):
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--session-worker", action, str(root)],
                env=dict(os.environ), text=True, capture_output=True, timeout=30)
            assert result.returncode == 0, \
                f"{action} subprocess failed:\n{result.stdout}\n{result.stderr}"
            current = json.loads(keyfile.read_text())
            assert "k_raw" in current
            assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
            if previous is not None:
                assert current == previous, "opening a saved session must not rotate its key"
            previous = current


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--session-worker":
        _session_worker(sys.argv[2], sys.argv[3])
    else:
        tests = sorted((name, fn) for name, fn in list(globals().items())
                       if name.startswith("test_") and callable(fn))
        failed = skipped = 0
        for name, fn in tests:
            try:
                fn()
                print(f"PASS {name}")
            except SkipTest as exc:
                skipped += 1
                print(f"SKIP {name}: {exc}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
        print(f"{len(tests) - failed - skipped} passed, {skipped} skipped, {failed} failed")
        raise SystemExit(bool(failed))
