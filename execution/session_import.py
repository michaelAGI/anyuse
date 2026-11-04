"""Import one host's cookies from a browser profile a human picked.

``login = replay`` (execution/site_modes.py) lets a credential-bound request
replay against a site, using a session the person already established in their
everyday browser — so there is no password to type and no 2FA/CAPTCHA to redo.
This module reads that session's cookies.

What is read, and when
----------------------
* Cookie **names** are plaintext in the Chromium/Firefox cookie database, so
  ``anyuse sites login`` discovers which cookies a host has WITHOUT decrypting
  anything and without a Keychain prompt. Only the names and a reference (the
  browser, the profile directory name, the cookie host, the chosen database
  path) are persisted — never a value. :func:`cookie_names`.
* Cookie **values** are decrypted only at REPLAY time, held in memory for the
  duration of one call, and never written anywhere (no log, no template, no
  saved session). :func:`import_cookies`.

The database is never copied to disk. It is read into memory with
``sqlite3.connect(":memory:").deserialize(bytes)`` (Python 3.11+), so a Firefox
database — whose values are plaintext — is never left behind in a temp file.

**WAL.** Firefox (and other browsers) keep the cookie database in WAL mode,
so a fresh cookie may live only in the ``-wal`` file. The committed WAL frames
are merged into the in-memory copy (salts and checksums verified, an unfinished
transaction ignored) and the header is switched to rollback-journal mode on that
copy only (:func:`_database_bytes`). Nothing is ever written to the user's
database or its ``-wal``/``-shm`` files.

Where values come from
----------------------
* **Chrome family on macOS** (Chrome, Arc, Brave, Edge, Chromium): the
  ``Cookies`` SQLite file. Values are ``v10``-prefixed AES-128-CBC with a
  16-space IV, under ``PBKDF2-HMAC-SHA1(password, b"saltysalt", 1003, 16)``. The
  password is the Keychain generic-password item ``"<Browser> Safe Storage"``.
  When the DB ``meta`` ``version`` is >= 24 the decrypted plaintext is prefixed
  by a 32-byte ``SHA-256(host_key)`` that is verified and stripped; a mismatch
  means a wrong key and is refused. :func:`chrome_cookies`.
* **Firefox**: ``cookies.sqlite`` / ``moz_cookies``; values are plaintext.
  :func:`firefox_cookies`.

Injected I/O
------------
The Keychain password read and every filesystem path are **injected** callables
or arguments. The real Keychain reader (:func:`keychain_password`, a
``security find-generic-password`` subprocess) exists for production but is
NEVER called by the tests or from here with a real argument — a test always
passes a fake ``password_reader``. AES uses the ``cryptography`` package, which
the project already depends on; this module does not hand-roll AES.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import struct
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: PBKDF2 parameters Chromium uses for the macOS 'v10' cookie encryption.
_SALT = b"saltysalt"
_ITERATIONS = 1003
_KEY_LENGTH = 16
_IV = b" " * 16

#: Keychain generic-password service name per Chromium-family browser.
SAFE_STORAGE_SERVICE = {
    "chrome": "Chrome Safe Storage",
    "arc": "Arc Safe Storage",
    "brave": "Brave Safe Storage",
    "edge": "Microsoft Edge Safe Storage",
    "chromium": "Chromium Safe Storage",
}

CHROME_FAMILY = tuple(SAFE_STORAGE_SERVICE)


class SessionImportError(RuntimeError):
    """A profile could not be read, or a value could not be decrypted."""


@dataclass(frozen=True)
class Cookie:
    """One cookie, with everything the per-hop send rules need. Values live in
    memory only."""
    name: str
    value: str
    domain: str          # the cookie's domain, without a leading dot
    host_only: bool      # True when it has no Domain attribute (exact host only)
    path: str
    secure: bool

    def as_record(self) -> Dict[str, object]:
        return {"name": self.name, "value": self.value, "domain": self.domain,
                "host_only": self.host_only, "path": self.path, "secure": self.secure}


# ---------------------------------------------------------------------------
# reading the SQLite database into memory (never copied to disk)
# ---------------------------------------------------------------------------

_WAL_MAGIC = (0x377F0682, 0x377F0683)


def _wal_checksum(data: bytes, s0: int, s1: int, big: bool) -> Tuple[int, int]:
    words = struct.unpack((">" if big else "<") + "%dI" % (len(data) // 4), data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def _apply_wal(db: bytearray, wal: bytes) -> bytearray:
    """Merge the COMMITTED frames of a ``-wal`` file into ``db``, in memory.

    Follows the SQLite WAL format: the header checksum and every frame's salts
    and cumulative checksum are verified; reading stops at the first invalid
    frame, and frames after the last commit frame (an unfinished transaction)
    are ignored. Nothing is written back to the user's files."""
    if len(wal) < 32:
        return db
    magic, _version, page_size, _ckpt, salt1, salt2, c1, c2 = struct.unpack(">8I", wal[:32])
    if magic not in _WAL_MAGIC or page_size < 512 or page_size & (page_size - 1):
        return db
    big = bool(magic & 1)
    s0, s1 = _wal_checksum(wal[:24], 0, 0, big)
    if (s0, s1) != (c1, c2):
        return db
    committed: Dict[int, bytes] = {}
    pending: Dict[int, bytes] = {}
    db_pages = 0
    off = 32
    while off + 24 + page_size <= len(wal):
        frame = wal[off:off + 24]
        pgno, commit, fs1, fs2, fc1, fc2 = struct.unpack(">6I", frame)
        if (fs1, fs2) != (salt1, salt2) or pgno == 0:
            break
        page = wal[off + 24:off + 24 + page_size]
        s0, s1 = _wal_checksum(frame[:8], s0, s1, big)
        s0, s1 = _wal_checksum(page, s0, s1, big)
        if (s0, s1) != (fc1, fc2):
            break
        pending[pgno] = page
        if commit:
            committed.update(pending)
            pending = {}
            db_pages = commit
        off += 24 + page_size
    if not committed:
        return db
    need = db_pages * page_size
    if len(db) < need:
        db.extend(b"\x00" * (need - len(db)))
    for pgno, page in committed.items():
        start = (pgno - 1) * page_size
        db[start:start + page_size] = page
    del db[need:]
    return db


def _database_bytes(db_path: str) -> bytes:
    """The database as bytes, ready for ``deserialize``, read only in memory.

    A WAL-mode database (header bytes 18/19 == 2) cannot be deserialized as is;
    its committed ``-wal`` frames are merged in memory and bytes 18/19 are set
    to 1 (rollback journal) on the in-memory copy only."""
    try:
        with open(db_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise SessionImportError(f"cannot read {db_path}: {exc}") from exc
    if len(data) >= 20 and (data[18] == 2 or data[19] == 2):
        buf = bytearray(data)
        try:
            with open(db_path + "-wal", "rb") as fh:
                buf = _apply_wal(buf, fh.read())
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SessionImportError(f"cannot read {db_path}-wal: {exc}") from exc
        if len(buf) >= 20:
            buf[18] = 1
            buf[19] = 1
        return bytes(buf)
    return data


def _open_in_memory(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SessionImportError(f"no cookie database at {db_path}")
    data = _database_bytes(db_path)
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(data)                       # Python 3.11+, no temp file
    except (sqlite3.DatabaseError, OverflowError) as exc:
        conn.close()
        raise SessionImportError(f"{db_path} is not a readable SQLite database: {exc}") from exc
    return conn


def _query(db_path: str, sql: str):
    conn = _open_in_memory(db_path)
    try:
        return list(conn.execute(sql))
    except sqlite3.DatabaseError as exc:
        raise SessionImportError(f"cannot read {db_path}: {exc}") from exc
    finally:
        conn.close()


def _host_matches(cookie_host: str, want: str) -> bool:
    ch = (cookie_host or "").lower().strip()
    w = (want or "").lower().strip().rstrip(".")
    if not ch or not w:
        return False
    if ch.startswith("."):
        bare = ch[1:]
        return w == bare or w.endswith("." + bare)
    return ch == w


def _expired(expires: int, *, epoch: str) -> bool:
    import time
    try:
        exp = int(expires)
    except (TypeError, ValueError):
        return False
    if exp <= 0:
        return False
    unix = exp / 1_000_000 - 11_644_473_600 if epoch == "chrome" else exp
    return unix < time.time()


# ---------------------------------------------------------------------------
# cookie NAMES — plaintext, no decryption, no Keychain
# ---------------------------------------------------------------------------

def chrome_names(db_path: str, host: str) -> List[str]:
    rows = _query(db_path, "SELECT host_key, name, expires_utc FROM cookies")
    return [str(name) for host_key, name, exp in rows
            if _host_matches(str(host_key), host) and not _expired(exp, epoch="chrome")]


def firefox_names(db_path: str, host: str) -> List[str]:
    rows = _query(db_path, "SELECT host, name, expiry FROM moz_cookies")
    return [str(name) for ck_host, name, exp in rows
            if _host_matches(str(ck_host), host) and not _expired(exp, epoch="firefox")]


def cookie_names(browser: str, db_path: str, host: str) -> List[str]:
    """The cookie NAMES a host has in this profile. Plaintext; no value touched."""
    if (browser or "").lower() == "firefox":
        return firefox_names(db_path, host)
    return chrome_names(db_path, host)


# ---------------------------------------------------------------------------
# AES-128-CBC value decryption (macOS 'v10'), with full verification
# ---------------------------------------------------------------------------

def derive_key(password: str) -> bytes:
    """The AES key Chromium derives from the Safe-Storage password (macOS v10)."""
    from cryptography.hazmat.primitives.hashes import SHA1
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=SHA1(), length=_KEY_LENGTH, salt=_SALT,
                     iterations=_ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def decrypt_chrome_value(blob: bytes, key: bytes, *, meta_version: int,
                         host_key: str = "") -> str:
    """Decrypt one Chromium cookie value with ``key`` and VERIFY it.

    AES-128-CBC, IV = 16 spaces, PKCS7 padding (every pad byte checked). When
    ``meta_version >= 24`` the plaintext is prefixed by ``SHA-256(host_key)``,
    which is compared to the expected hash; a mismatch means a wrong key and is
    refused rather than silently kept. A ``vNN`` prefix other than ``v10`` is
    refused (not returned as plaintext); a blob with no ``vNN`` prefix is an
    older plaintext value and is returned as-is."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not blob:
        return ""
    if len(blob) >= 3 and blob[0:1] == b"v" and blob[1:3].isdigit():
        if blob[0:3] != b"v10":
            raise SessionImportError(
                f"unsupported cookie encryption prefix {blob[0:3]!r} "
                "(only macOS v10 is supported)")
        ciphertext = blob[3:]
    else:
        return blob.decode("utf-8", "replace")       # legacy unencrypted value
    if not ciphertext or len(ciphertext) % 16:
        raise SessionImportError("ciphertext is not a whole number of AES blocks")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 16 or pad > len(padded) or padded[-pad:] != bytes([pad]) * pad:
        raise SessionImportError("bad PKCS7 padding (wrong key?)")
    plaintext = padded[:-pad]
    if meta_version >= 24:
        if len(plaintext) < 32:
            raise SessionImportError("plaintext too short to carry the v24 domain hash")
        expected = hashlib.sha256((host_key or "").encode("utf-8")).digest()
        if not hmac.compare_digest(plaintext[:32], expected):
            raise SessionImportError("v24 domain-hash prefix does not match the host "
                                     "(wrong key?)")
        plaintext = plaintext[32:]
    return plaintext.decode("utf-8", "replace")


def _chrome_meta_version(conn: sqlite3.Connection) -> int:
    try:
        rows = list(conn.execute("SELECT value FROM meta WHERE key='version'"))
        return int(rows[0][0]) if rows else 0
    except (sqlite3.DatabaseError, TypeError, ValueError, IndexError):
        return 0


def _chrome_bool(column_value) -> bool:
    try:
        return bool(int(column_value))
    except (TypeError, ValueError):
        return bool(column_value)


# ---------------------------------------------------------------------------
# cookie records (name, value, domain, host_only, path, secure) in memory
# ---------------------------------------------------------------------------

def chrome_cookies(db_path: str, host: str, password: str, *,
                   names: Optional[List[str]] = None) -> List[Cookie]:
    key = derive_key(password)
    conn = _open_in_memory(db_path)
    try:
        meta = _chrome_meta_version(conn)
        rows = list(conn.execute(
            "SELECT host_key, name, encrypted_value, path, is_secure, expires_utc "
            "FROM cookies"))
    finally:
        conn.close()
    wanted = set(names) if names is not None else None
    out: List[Cookie] = []
    for host_key, name, blob, path, is_secure, exp in rows:
        if not _host_matches(str(host_key), host) or _expired(exp, epoch="chrome"):
            continue
        if wanted is not None and str(name) not in wanted:
            continue
        if isinstance(blob, str):
            blob = blob.encode("latin-1", "ignore")
        value = decrypt_chrome_value(bytes(blob or b""), key, meta_version=meta,
                                     host_key=str(host_key))
        hk = str(host_key)
        out.append(Cookie(name=str(name), value=value, domain=hk.lstrip("."),
                          host_only=not hk.startswith("."), path=str(path or "/"),
                          secure=_chrome_bool(is_secure)))
    return out


def firefox_cookies(db_path: str, host: str, *,
                    names: Optional[List[str]] = None) -> List[Cookie]:
    rows = _query(db_path, "SELECT host, name, value, path, isSecure, expiry "
                           "FROM moz_cookies")
    wanted = set(names) if names is not None else None
    out: List[Cookie] = []
    for ck_host, name, value, path, is_secure, exp in rows:
        if not _host_matches(str(ck_host), host) or _expired(exp, epoch="firefox"):
            continue
        if wanted is not None and str(name) not in wanted:
            continue
        hk = str(ck_host)
        out.append(Cookie(name=str(name), value=str(value or ""), domain=hk.lstrip("."),
                          host_only=not hk.startswith("."), path=str(path or "/"),
                          secure=_chrome_bool(is_secure)))
    return out


# ---------------------------------------------------------------------------
# per-hop selection — a cookie is sent only when domain, path and Secure match
# ---------------------------------------------------------------------------

def _domain_ok(host: str, cookie: Dict[str, object]) -> bool:
    domain = str(cookie.get("domain") or "").lower()
    if not domain:
        return False
    if cookie.get("host_only"):
        return host == domain
    return host == domain or host.endswith("." + domain)


def _path_ok(request_path: str, cookie_path: str) -> bool:
    cp = cookie_path or "/"
    rp = request_path or "/"
    if cp == rp:
        return True
    if rp.startswith(cp):
        return cp.endswith("/") or rp[len(cp):len(cp) + 1] == "/"
    return False


def cookie_header_for(cookies, url: str) -> str:
    """Build the ``Cookie`` header for ``url`` from in-memory records.

    A cookie is included only when its domain, path and Secure attribute match
    the hop (an https-only cookie is never sent over http; a host-only cookie —
    including a ``__Host-`` cookie — goes only to its exact host). For a repeated
    name the most specific wins (longest path, then host-only, then longest
    domain), as a browser does; never 'last row wins'."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    is_https = parts.scheme == "https"
    chosen: Dict[str, tuple] = {}
    for c in cookies:
        rec = c if isinstance(c, dict) else c.as_record()
        if rec.get("secure") and not is_https:
            continue
        if not _domain_ok(host, rec) or not _path_ok(path, str(rec.get("path") or "/")):
            continue
        spec = (len(str(rec.get("path") or "/")), 1 if rec.get("host_only") else 0,
                len(str(rec.get("domain") or "")))
        name = str(rec.get("name"))
        if name not in chosen or spec > chosen[name][1]:
            chosen[name] = (str(rec.get("value") or ""), spec)
    return "; ".join(f"{name}={value}" for name, (value, _) in chosen.items())


# ---------------------------------------------------------------------------
# the real (production) readers — defined, never called by tests or from here
# ---------------------------------------------------------------------------

def keychain_password(browser: str) -> str:  # pragma: no cover - never run offline
    """Read the '<Browser> Safe Storage' password from the macOS Keychain.

    The ONLY place a real Keychain read happens; never invoked by the tests
    (a fake ``password_reader`` is always injected) nor by this module offline."""
    import subprocess
    service = SAFE_STORAGE_SERVICE.get((browser or "").lower())
    if not service:
        raise SessionImportError(f"no Safe Storage service name for browser {browser!r}")
    try:
        out = subprocess.run(["security", "find-generic-password", "-w", "-s", service],
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:  # noqa: BLE001
        raise SessionImportError(f"could not read the Keychain item {service!r}: {exc}") from exc
    return out.stdout.strip()


def default_cookie_db(browser: str, profile: str) -> str:  # pragma: no cover
    """The default macOS cookie-database path for ``browser``/``profile``.

    Only a fallback: a session reference stores the exact database path the
    human chose (``anyuse sites login --db``), so this is used only when none was
    recorded. Firefox requires a profile; a missing one is an error rather than
    an invalid path."""
    home = os.path.expanduser("~")
    low = (browser or "").lower()
    if low == "firefox":
        if not profile:
            raise SessionImportError("Firefox needs a profile directory name")
        return os.path.join(home, "Library/Application Support/Firefox/Profiles",
                            profile, "cookies.sqlite")
    roots = {
        "chrome": "Library/Application Support/Google/Chrome",
        "arc": "Library/Application Support/Arc/User Data",
        "brave": "Library/Application Support/BraveSoftware/Brave-Browser",
        "edge": "Library/Application Support/Microsoft Edge",
        "chromium": "Library/Application Support/Chromium",
    }
    base = roots.get(low)
    if not base:
        raise SessionImportError(f"unknown browser {browser!r}")
    return os.path.join(home, base, profile or "Default", "Cookies")


# ---------------------------------------------------------------------------
# the value-import entry point used by the replay path
# ---------------------------------------------------------------------------

def import_cookies(session: Dict[str, object], *,
                   password_reader: Callable[[str], str],
                   db_resolver: Optional[Callable[[str, str], str]] = None
                   ) -> List[Cookie]:
    """Resolve the referenced session to in-memory :class:`Cookie` records.

    ``session`` is the reference stored in ``site_modes.toml`` (browser, profile,
    host, cookie_names, and the chosen ``db_path``). The database path the human
    chose is used verbatim, so the profile read at login is the one replayed.
    ``password_reader(browser) -> password`` is injected so no real Keychain item
    is touched in a test; ``db_resolver`` is a fallback used only when the
    reference stored no path."""
    browser = str(session.get("browser") or "").lower()
    profile = str(session.get("profile") or "")
    host = str(session.get("host") or "")
    names = [str(n) for n in (session.get("cookie_names") or [])] or None
    if not browser or not host:
        raise SessionImportError("session reference is missing a browser or host")
    db_path = str(session.get("db_path") or "")
    if not db_path:
        resolver = db_resolver or default_cookie_db
        db_path = resolver(browser, profile)
    if browser == "firefox":
        return firefox_cookies(db_path, host, names=names)
    if browser in CHROME_FAMILY:
        password = password_reader(browser)
        return chrome_cookies(db_path, host, password, names=names)
    raise SessionImportError(f"unsupported browser {browser!r}")
