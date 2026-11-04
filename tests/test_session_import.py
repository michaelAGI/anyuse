"""Cookie import for site-mode login=replay (execution/session_import.py).

Offline: synthetic SQLite databases, encrypted with a FAKE password so the AES
round-trips. The real Keychain reader is never called (a fake ``password_reader``
is injected) and no real browser profile is touched. The database is read into
memory (``:memory:`` + ``deserialize``), never copied to disk."""
from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import tempfile
import time
import unittest

from unittest import mock

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from anyuse.execution import session_import as SI


def _encrypt(value: str, key: bytes, *, meta_version: int, host_key: str) -> bytes:
    blob = value.encode("utf-8")
    if meta_version >= 24:
        blob = hashlib.sha256(host_key.encode("utf-8")).digest() + blob
    pad = 16 - (len(blob) % 16)
    blob = blob + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(blob) + enc.finalize()


def _chrome_db(path, rows, *, meta_version, password):
    """rows: (host_key, name, value, path, is_secure, expires_utc)."""
    key = SI.derive_key(password)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
    conn.execute("INSERT INTO meta VALUES('version', ?)", (str(meta_version),))
    conn.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, encrypted_value BLOB, "
                 "path TEXT, is_secure INTEGER, expires_utc INTEGER)")
    for host_key, name, value, cpath, sec, exp in rows:
        conn.execute("INSERT INTO cookies VALUES(?,?,?,?,?,?)",
                     (host_key, name, _encrypt(value, key, meta_version=meta_version,
                                               host_key=host_key), cpath, sec, exp))
    conn.commit()
    conn.close()


def _firefox_db(path, rows):
    """rows: (host, name, value, path, isSecure, expiry)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE moz_cookies(host TEXT, name TEXT, value TEXT, path TEXT, "
                 "isSecure INTEGER, expiry INTEGER)")
    for row in rows:
        conn.execute("INSERT INTO moz_cookies VALUES(?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()


def _header_dict(header: str) -> dict:
    out = {}
    for part in header.split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


PAST_CHROME = int((time.time() - 1000) * 1_000_000) + 11_644_473_600_000_000


class ChromeValues(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "Cookies")

    def test_v24_prefix_verified_and_stripped(self):
        _chrome_db(self.db, [(".example.com", "sid", "SECRET-123", "/", 0, 0)],
                   meta_version=24, password="pw")
        cookies = SI.chrome_cookies(self.db, "example.com", "pw")
        self.assertEqual([(c.name, c.value) for c in cookies], [("sid", "SECRET-123")])

    def test_pre_v24_has_no_domain_prefix(self):
        _chrome_db(self.db, [("example.com", "sid", "abc", "/", 0, 0)],
                   meta_version=18, password="pw")
        self.assertEqual(SI.chrome_cookies(self.db, "example.com", "pw")[0].value, "abc")

    def test_expired_and_other_hosts_dropped(self):
        _chrome_db(self.db, [(".example.com", "live", "a", "/", 0, 0),
                             (".example.com", "stale", "b", "/", 0, PAST_CHROME),
                             (".other.com", "theirs", "c", "/", 0, 0)],
                   meta_version=24, password="pw")
        self.assertEqual([c.name for c in SI.chrome_cookies(self.db, "example.com", "pw")],
                         ["live"])

    def test_names_need_no_decryption(self):
        _chrome_db(self.db, [(".example.com", "sid", "x", "/", 0, 0)],
                   meta_version=24, password="pw")
        self.assertEqual(SI.chrome_names(self.db, "www.example.com"), ["sid"])


class WrongKeyIsRefused(unittest.TestCase):
    def test_v24_prefix_refuses_all_200_random_wrong_keys(self):
        """meta >= 24: a wrong key must pass PKCS7 AND reproduce sha256(host_key)
        in the first 32 plaintext bytes — every one of 200 random keys fails."""
        right = SI.derive_key("correct horse")
        blob = _encrypt("the-value", right, meta_version=24, host_key=".example.com")
        self.assertEqual(SI.decrypt_chrome_value(blob, right, meta_version=24,
                                                 host_key=".example.com"), "the-value")
        rng = random.Random(20261009)
        refused = 0
        for _ in range(200):
            wrong = bytes(rng.getrandbits(8) for _ in range(16))
            try:
                SI.decrypt_chrome_value(blob, wrong, meta_version=24, host_key=".example.com")
            except SI.SessionImportError:
                refused += 1
        self.assertEqual(refused, 200)

    def test_padding_alone_below_v24(self):
        """meta < 24 has NO integrity check besides PKCS7 (Chromium v10 carries no
        MAC): a random wrong key yields valid-looking padding about 1 time in 255
        (last byte 0x01, or 0x02 0x02, ...). Every byte of the pad is verified, so
        the false-accept rate is that floor and no higher; with a fixed seed the
        count is deterministic."""
        right = SI.derive_key("correct horse")
        blob = _encrypt("the-value", right, meta_version=18, host_key="")
        rng = random.Random(42)
        refused = 0
        for _ in range(200):
            wrong = bytes(rng.getrandbits(8) for _ in range(16))
            try:
                SI.decrypt_chrome_value(blob, wrong, meta_version=18)
            except SI.SessionImportError:
                refused += 1
        self.assertGreaterEqual(refused, 195)          # ~1/255 accepted at most

    def test_a_short_pad_with_a_wrong_inner_byte_is_refused(self):
        key = SI.derive_key("k")
        # plaintext whose last two bytes are 0x01 0x02: pad says 2, bytes differ
        enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
        blob = b"v10" + enc.update(b"A" * 14 + b"\x01\x02") + enc.finalize()
        with self.assertRaises(SI.SessionImportError):
            SI.decrypt_chrome_value(blob, key, meta_version=18)

    def test_unknown_version_prefix_is_refused(self):
        with self.assertRaises(SI.SessionImportError):
            SI.decrypt_chrome_value(b"v11" + b"\x00" * 16, SI.derive_key("x"),
                                    meta_version=10)

    def test_a_value_with_no_prefix_is_plaintext(self):
        self.assertEqual(SI.decrypt_chrome_value(b"plain", SI.derive_key("x"),
                                                 meta_version=10), "plain")


class PerHopSelection(unittest.TestCase):
    """The cookie injection test: several hosts, paths, host-only, Secure,
    duplicates — then per-hop selection by domain/path/Secure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "Cookies")
        _chrome_db(self.db, [
            (".example.com", "sid", "S", "/", 0, 0),
            ("example.com", "__Host-tok", "H", "/", 1, 0),      # host-only, Secure
            (".example.com", "deep", "D", "/app", 0, 0),
            (".example.com", "x", "root", "/", 0, 0),           # duplicate name, root
            ("example.com", "x", "deep", "/app", 0, 0),         # duplicate name, deeper
            (".other.com", "nope", "N", "/", 0, 0),
        ], meta_version=24, password="pw")
        self.cookies = [c.as_record()
                        for c in SI.chrome_cookies(self.db, "example.com", "pw")]

    def test_base_host_over_https(self):
        got = _header_dict(SI.cookie_header_for(self.cookies, "https://example.com/"))
        self.assertEqual(got, {"sid": "S", "__Host-tok": "H", "x": "root"})

    def test_host_only_cookie_not_sent_to_subdomain(self):
        got = _header_dict(SI.cookie_header_for(self.cookies, "https://www.example.com/"))
        self.assertEqual(got, {"sid": "S", "x": "root"})       # no __Host-tok

    def test_secure_cookie_not_sent_over_http(self):
        got = _header_dict(SI.cookie_header_for(self.cookies, "http://example.com/"))
        self.assertNotIn("__Host-tok", got)
        self.assertEqual(got.get("sid"), "S")

    def test_most_specific_path_wins_for_a_duplicate_name(self):
        got = _header_dict(SI.cookie_header_for(self.cookies, "https://example.com/app/p"))
        self.assertEqual(got["x"], "deep")                      # not 'last row wins'
        self.assertEqual(got["deep"], "D")


class FirefoxValues(unittest.TestCase):
    def test_plaintext_values_and_expiry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "cookies.sqlite")
        _firefox_db(db, [(".example.com", "sid", "plain", "/", 0, 0),
                         ("example.com", "stale", "x", "/", 0, int(time.time()) - 10)])
        cookies = SI.firefox_cookies(db, "www.example.com")
        self.assertEqual([(c.name, c.value) for c in cookies], [("sid", "plain")])


class ImportCookies(unittest.TestCase):
    def test_import_uses_injected_readers_and_the_stored_db_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "Cookies")
        _chrome_db(db, [(".shop.test", "sid", "S", "/", 0, 0)],
                   meta_version=24, password="kc")
        calls = {"pw": 0, "db": 0}

        def password_reader(browser):
            calls["pw"] += 1
            return "kc"

        def db_resolver(browser, profile):             # must NOT be used (db_path stored)
            calls["db"] += 1
            raise AssertionError("db_resolver used although db_path was stored")

        session = {"browser": "chrome", "profile": "Profile 1", "host": "shop.test",
                   "db_path": db, "cookie_names": ["sid"]}
        cookies = SI.import_cookies(session, password_reader=password_reader,
                                    db_resolver=db_resolver)
        self.assertEqual([c.value for c in cookies], ["S"])
        self.assertEqual((calls["pw"], calls["db"]), (1, 0))

    def test_firefox_import_needs_no_password(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "cookies.sqlite")
        _firefox_db(db, [("shop.test", "sid", "FF", "/", 0, 0)])

        def password_reader(browser):
            raise AssertionError("Firefox import must not read a keychain password")

        session = {"browser": "firefox", "profile": "p", "host": "shop.test",
                   "db_path": db, "cookie_names": ["sid"]}
        cookies = SI.import_cookies(session, password_reader=password_reader)
        self.assertEqual([c.value for c in cookies], ["FF"])

    def test_the_database_is_never_copied_to_disk_wal_fixture(self):
        """A WAL-mode Firefox database whose newest cookie lives only in the -wal
        file: it is read (frames merged in memory), nothing is written next to
        the database, and nothing lands in the temp directory."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        private_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(private_tmp.cleanup)
        db = os.path.join(tmp.name, "cookies.sqlite")
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE moz_cookies(host TEXT, name TEXT, value TEXT, "
                       "path TEXT, isSecure INTEGER, expiry INTEGER)")
        writer.commit()
        writer.execute("INSERT INTO moz_cookies VALUES('.shop.test','old','O','/',0,0)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # 'old' is in the main file
        writer.execute("INSERT INTO moz_cookies VALUES('.shop.test','fresh','F','/',0,0)")
        writer.commit()                                         # 'fresh' only in -wal
        with open(db, "rb") as fh:
            self.assertEqual(fh.read(20)[18:20], b"\x02\x02")  # WAL-format header
        self.assertGreater(os.path.getsize(db + "-wal"), 0)
        before = sorted(os.listdir(tmp.name))
        stamps = {n: os.path.getmtime(os.path.join(tmp.name, n)) for n in before}
        with mock.patch.object(tempfile, "tempdir", private_tmp.name):
            cookies = SI.firefox_cookies(db, "shop.test")
        self.assertEqual(sorted((c.name, c.value) for c in cookies),
                         [("fresh", "F"), ("old", "O")])
        self.assertEqual(sorted(os.listdir(tmp.name)), before)          # no copy beside it
        self.assertEqual({n: os.path.getmtime(os.path.join(tmp.name, n)) for n in before},
                         stamps)                                        # nothing modified
        self.assertEqual(os.listdir(private_tmp.name), [])              # no temp file

    def test_an_uncommitted_wal_tail_is_ignored(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "cookies.sqlite")
        writer = sqlite3.connect(db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE moz_cookies(host TEXT, name TEXT, value TEXT, "
                       "path TEXT, isSecure INTEGER, expiry INTEGER)")
        writer.execute("INSERT INTO moz_cookies VALUES('.shop.test','kept','K','/',0,0)")
        # corrupt the last frame's checksum: reading must stop before it
        wal = db + "-wal"
        with open(wal, "rb") as fh:
            data = bytearray(fh.read())
        data[-1] ^= 0xFF
        merged = SI._apply_wal(bytearray(open(db, "rb").read()), bytes(data))
        self.assertIsInstance(merged, bytearray)                 # no exception, no crash


if __name__ == "__main__":
    unittest.main()
