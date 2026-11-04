"""Bounded reuse of static investigation data, never execution or proof.

REA owns its Ghidra projects; AnyUse caches only completed output it retains.
Callers still validate current originals and create fresh investigation receipts.
Missing/unpinned toolchains and unavailable caches simply run uncached.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from anyuse.paths import anyuse_data_path

FORMAT = "anyuse.rea-static-cache/1"
MAX_ENTRY_BYTES = 20 * 1024**2
MAX_TOTAL_BYTES = 128 * 1024**2
MAX_ENTRIES = 64
_ENTRY = re.compile(r"[0-9a-f]{64}\.json\Z")


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def key(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def ghidra_pin():
    """Only reuse the supported pinned tool version, measured at its actual home."""
    from .rea_setup import PINS
    home = Path(os.environ.get("GHIDRA_INSTALL_DIR", "/missing-ghidra")).expanduser()
    try:
        home = home.resolve(strict=True)
        files = [home / "support/analyzeHeadless", home / "Ghidra/application.properties"]
        if any(p.is_symlink() or not p.is_file() or p.stat().st_size > 1024**2 for p in files):
            return None
        properties = files[1].read_text()
        version = PINS["ghidra"]["version"]
        if re.search(r"^application\.version\s*=\s*" + re.escape(version) + r"\s*$", properties, re.M) is None:
            return None
        return {"home": str(home), "version": version,
                "pinned_distribution_sha256": PINS["ghidra"]["sha256"],
                "files": {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    except (OSError, UnicodeError):
        return None


class StaticAnalysisCache:
    """One atomic JSON entry per content key, with a nonblocking shared lock.

    Locks cover cache IO only, never a tool process. Concurrent producers may do
    duplicate work; each publishes a complete entry. A crash cannot leave an
    admitted partial entry. Bounds include interrupted temporary writes.
    """

    def __init__(self, path=None):
        self.path = (Path(path) if path is not None else
                     anyuse_data_path("rea", "ghidra_cache")).expanduser().absolute()

    def _lock(self, *, create):
        if create:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (not self.path.is_dir() or self.path.is_symlink() or
                self.path.resolve(strict=True) != self.path):
            raise OSError("canonical static cache directory required")
        fd = os.open(self.path / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("regular static cache lock required")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return os.fdopen(fd, "a+b")
        except BaseException:
            os.close(fd)
            raise

    def get(self, cache_key):
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise ValueError("static cache key must be a SHA-256")
        try:
            with self._lock(create=False):
                path = self.path / (cache_key + ".json")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ENTRY_BYTES:
                        return None
                    value = json.loads(stream.read(MAX_ENTRY_BYTES + 1))
                if (type(value) is not dict or set(value) != {"format", "key", "payload", "payload_sha256"} or
                        value["format"] != FORMAT or value["key"] != cache_key or
                        key(value["payload"]) != value["payload_sha256"]):
                    return None
                return value["payload"]
        except (OSError, ValueError, TypeError, UnicodeError):
            return None

    def put(self, cache_key, payload):
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise ValueError("static cache key must be a SHA-256")
        raw = _encoded({"format": FORMAT, "key": cache_key, "payload": payload,
                        "payload_sha256": key(payload)})
        if len(raw) > MAX_ENTRY_BYTES or len(raw) > MAX_TOTAL_BYTES:
            return False
        temporary = None
        try:
            with self._lock(create=True):
                # Remove our interrupted writes and evict oldest completed data.
                entries = []
                for path in self.path.iterdir():
                    if path.name.startswith(".write-"):
                        if path.is_file() and not path.is_symlink():
                            path.unlink()
                    elif _ENTRY.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                        entries.append((path.stat().st_mtime_ns, path.stat().st_size, path))
                entries.sort()
                total = sum(row[1] for row in entries)
                while entries and (len(entries) >= MAX_ENTRIES or total + len(raw) > MAX_TOTAL_BYTES):
                    _, size, path = entries.pop(0)
                    path.unlink()
                    total -= size
                fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.path)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path / (cache_key + ".json"))
                temporary = None
                return True
        except OSError:
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
