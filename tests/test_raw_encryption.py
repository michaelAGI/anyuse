"""Tests for raw-artifact encrypt-at-rest.

Covers:
  - bytes/json roundtrip; the on-disk blob is MAGIC-prefixed ciphertext
    (json.loads on the raw file fails — nothing readable in the clear)
  - transparent reads: a legacy plaintext file (no magic) passes through
  - wrong key → None (never garbage, never plaintext)
  - fail-closed: with no crypto constructible, writes are SKIPPED
    (return False, no file, and never a plaintext fallback)
  - SessionStorage: save → encrypted session.json → load roundtrip;
    legacy plaintext session.json still loads; list_sessions sees both
  - BatchWatcher._save_session_data writes encrypted session.json

Self-executing (pytest is not installed in this env). Keys are always
injected (Fernet.generate_key()) — tests never touch the Keychain or the
~/.anyuse key file.

Run from the anyuse package dir:
    cd anyuse && .venv/bin/python test_raw_encryption.py
"""

import json
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

from anyuse.watcher import raw_store
from anyuse.watcher.raw_store import (
    MAGIC,
    RawArtifactCrypto,
    read_bytes_transparent,
    read_json_transparent,
    write_bytes_encrypted,
    write_json_encrypted,
)
from anyuse.watcher.models import WatchSession
from anyuse.watcher.storage import SessionStorage

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


def _crypto() -> RawArtifactCrypto:
    return RawArtifactCrypto(key=Fernet.generate_key())


# ---------------------------------------------------------------------------
# 1. bytes roundtrip + on-disk shape
# ---------------------------------------------------------------------------

def test_bytes_roundtrip():
    c = _crypto()
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "frame.jpg"
        payload = b"\xff\xd8\xff jpeg-ish bytes \x00\x01"
        check("write_bytes_encrypted returns True",
              write_bytes_encrypted(p, payload, c))
        blob = p.read_bytes()
        check("on-disk blob is MAGIC-prefixed", blob.startswith(MAGIC))
        check("payload not on disk in the clear", payload not in blob)
        check("transparent read decrypts",
              read_bytes_transparent(p, c) == payload)


# ---------------------------------------------------------------------------
# 2. json roundtrip; raw file unreadable as json
# ---------------------------------------------------------------------------

def test_json_roundtrip():
    c = _crypto()
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "obs_0001.json"
        obj = {"ai_response": "user replied to alice@example.com", "n": 3}
        check("write_json_encrypted returns True",
              write_json_encrypted(p, obj, c))
        try:
            json.loads(p.read_text())
            plain_readable = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            plain_readable = False
        check("raw file is NOT plain-readable json", not plain_readable)
        check("transparent json read roundtrips",
              read_json_transparent(p, c) == obj)


# ---------------------------------------------------------------------------
# 3. legacy plaintext passthrough
# ---------------------------------------------------------------------------

def test_legacy_plaintext_passthrough():
    c = _crypto()
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "session.json"
        p.write_text(json.dumps({"id": "legacy1", "events": []}))
        data = read_json_transparent(p, c)
        check("legacy plaintext json still reads",
              isinstance(data, dict) and data.get("id") == "legacy1")
        raw = pathlib.Path(td) / "plain.bin"
        raw.write_bytes(b"just bytes")
        check("legacy plaintext bytes pass through",
              read_bytes_transparent(raw, c) == b"just bytes")


# ---------------------------------------------------------------------------
# 4. wrong key → None, not garbage
# ---------------------------------------------------------------------------

def test_wrong_key():
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "x.json"
        write_json_encrypted(p, {"secret": 1}, _crypto())
        other = _crypto()
        check("wrong-key bytes read → None",
              read_bytes_transparent(p, other) is None)
        check("wrong-key json read → None",
              read_json_transparent(p, other) is None)


# ---------------------------------------------------------------------------
# 5. fail-closed: no crypto → write skipped, nothing on disk
# ---------------------------------------------------------------------------

def test_fail_closed_write_skip():
    saved = (raw_store._default_crypto, raw_store._default_crypto_failed)
    raw_store._default_crypto = None
    raw_store._default_crypto_failed = True  # forces get_default → None
    try:
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "never.json"
            ok = write_json_encrypted(p, {"leak": "no"}, crypto=None)
            check("write with no crypto returns False", ok is False)
            check("no file written (no plaintext fallback)", not p.exists())
    finally:
        raw_store._default_crypto, raw_store._default_crypto_failed = saved


# ---------------------------------------------------------------------------
# 6. SessionStorage: encrypted save/load + legacy load + list_sessions
# ---------------------------------------------------------------------------

def test_session_storage():
    c = _crypto()
    with tempfile.TemporaryDirectory() as td:
        storage = SessionStorage(base_dir=pathlib.Path(td), crypto=c)

        session = WatchSession.create_new()
        session.apps_used = ["Mail"]
        storage.save(session)

        session_file = pathlib.Path(td) / session.id / "session.json"
        check("session.json exists", session_file.exists())
        check("session.json is encrypted",
              session_file.read_bytes().startswith(MAGIC))

        storage._cache.clear()
        loaded = storage.load(session.id)
        check("encrypted session loads",
              loaded is not None and loaded.id == session.id
              and loaded.apps_used == ["Mail"])

        # Legacy plaintext session alongside
        legacy = WatchSession.create_new()
        legacy_dir = pathlib.Path(td) / legacy.id
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "session.json").write_text(
            json.dumps(legacy.to_dict(), default=str))
        check("legacy plaintext session loads",
              storage.load(legacy.id) is not None)

        infos = storage.list_sessions()
        check("list_sessions sees encrypted + legacy",
              {i["id"] for i in infos} == {session.id, legacy.id},
              f"got {[i['id'] for i in infos]}")


# ---------------------------------------------------------------------------
# 7. BatchWatcher._save_session_data writes encrypted
# ---------------------------------------------------------------------------

def test_batch_watcher_session_save():
    from anyuse.watcher.batch_watcher import BatchWatcher, BatchWatcherConfig

    c = _crypto()
    with tempfile.TemporaryDirectory() as td:
        watcher = BatchWatcher(
            BatchWatcherConfig(output_level="silent"), raw_crypto=c)
        session = WatchSession.create_new()
        session.storage_path = str(pathlib.Path(td) / session.id)
        watcher._session = session

        watcher._save_session_data()

        session_file = pathlib.Path(session.storage_path) / "session.json"
        check("watcher session.json exists", session_file.exists())
        check("watcher session.json is encrypted",
              session_file.read_bytes().startswith(MAGIC))
        data = read_json_transparent(session_file, c)
        check("watcher session.json decrypts to the session",
              isinstance(data, dict) and data.get("id") == session.id)


if __name__ == "__main__":
    tests = [
        test_bytes_roundtrip,
        test_json_roundtrip,
        test_legacy_plaintext_passthrough,
        test_wrong_key,
        test_fail_closed_write_skip,
        test_session_storage,
        test_batch_watcher_session_save,
    ]
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except Exception as e:  # noqa: BLE001 - report, keep going
            FAIL += 1
            print(f"  FAIL {t.__name__} raised: {type(e).__name__}: {e}")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
