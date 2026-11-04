"""One reusable, hash-checked Qt source protocol cache, without code execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

from anyuse.paths import anyuse_data_path
from . import rea_mapping as R
from . import rea_qt_moc as Q

CACHE_ENV = "ANYUSE_REA_QT_SOURCE_CACHE"
MAX_SOURCE_BYTES = 1024**2


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def default_cache():
    return Path(os.environ.get(CACHE_ENV) or anyuse_data_path("rea-qt-sources", Q.QT_COMMIT))


def _manifest(files):
    return {"format": "anyuse.rea-qt-moc-source-retention/1", "status": "complete", "qtbase_commit": Q.QT_COMMIT,
            "version_under_review": Q.QT_VERSION, "source_only": True, "downloaded_source_executed": False,
            "native_abi_admission": False, "files": files}


def _write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def setup(out=None, *, transport=None):
    """Explicit public-source download; no application data is sent."""
    R.assert_guest()
    root = Path(out or default_cache()).resolve()
    _need(not root.exists(), "new Qt source cache required; existing cache is never overwritten")
    root.mkdir(mode=0o700, parents=True)
    files = {}
    _write(root / "request.json", {"format": "anyuse.rea-qt-source-cache-request/1", "commit": Q.QT_COMMIT,
                                  "expected_sha256": dict(Q._SOURCES), "source_execution": False})
    try:
        with httpx.Client(timeout=30, follow_redirects=False, transport=transport, trust_env=False) as client:
            for name, digest in Q._SOURCES.items():
                url = "https://raw.githubusercontent.com/qt/qtbase/" + Q.QT_COMMIT + "/src/corelib/kernel/" + name
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    chunks, count = [], 0
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        _need(count <= MAX_SOURCE_BYTES, "Qt source exceeds its byte bound")
                        chunks.append(chunk)
                raw = b"".join(chunks)
                _need(0 < len(raw) <= MAX_SOURCE_BYTES and hashlib.sha256(raw).hexdigest() == digest,
                      "Qt source SHA-256 differs from the supported revision: " + name)
                with (root / name).open("xb") as stream:
                    stream.write(raw)
                files[name] = {"sha256": digest, "bytes": len(raw), "status": "retained_source_bytes_only", "url": url}
        _write(root / "manifest.json", _manifest(files))
        return {"status": "complete", "cache": str(root), "manifest": str(root / "manifest.json"),
                "source_only": True, "source_execution": False, "files": len(files)}
    except BaseException as error:
        _write(root / "failure.json", {"status": "incomplete", "files": files,
                "error": {"type": type(error).__name__, "message": str(error)[:2048]}, "source_execution": False})
        raise


def retain(out, *, cache=None):
    """Copy exactly the supported source bytes into a session's evidence closure."""
    root, source = Path(out), Path(cache or default_cache())
    _need(not root.exists(), "new Qt source evidence folder required")
    if not source.is_dir() or not (source / "manifest.json").is_file():
        root.mkdir(mode=0o700)
        receipt = root / "evidence.json"
        _write(receipt, {"format": "anyuse.rea-missing-static-capability/1", "status": "needs_capability",
            "reason": "qt_source_cache_unavailable", "expected_commit": Q.QT_COMMIT,
            "setup": "anyuse map rea qt-sources --out " + str(source), "source_execution": False})
        return {"status": "needs_capability", "reason": "qt_source_cache_unavailable", "receipt": str(receipt),
                "evidence_only": True, "proof_created": False, "callable_abi_admitted": False, "lifetime_admitted": False}
    _need(source.is_absolute() and source.resolve(strict=True) == source and not (source / "manifest.json").is_symlink(),
          "canonical Qt source cache required")
    manifest = R.load_json(source / "manifest.json")
    _need(manifest.get("format") == "anyuse.rea-qt-moc-source-retention/1" and manifest.get("status") == "complete" and
          manifest.get("qtbase_commit") == Q.QT_COMMIT and manifest.get("version_under_review") == Q.QT_VERSION and
          manifest.get("source_only") is True and manifest.get("downloaded_source_executed") is False and
          manifest.get("native_abi_admission") is False and set(manifest.get("files", {})) == set(Q._SOURCES),
          "incomplete or incompatible Qt source cache")
    # Validate every file before creating a usable retained manifest.
    originals = {}
    for name, digest in Q._SOURCES.items():
        path = source / name
        _need(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MAX_SOURCE_BYTES,
              "canonical bounded Qt source file required")
        raw = path.read_bytes()
        _need(hashlib.sha256(raw).hexdigest() == digest and manifest["files"][name].get("sha256") == digest and
              manifest["files"][name].get("bytes") == len(raw) and
              manifest["files"][name].get("status") == "retained_source_bytes_only", "Qt cached source changed")
        originals[name] = raw
    root.mkdir(mode=0o700)
    resources = []
    for name, raw in originals.items():
        file = root / name
        with file.open("xb") as stream:
            stream.write(raw)
        resources.append({"file": str(file), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    receipt = root / "manifest.json"
    _write(receipt, manifest)
    return {"status": "retained_qt_source_protocol", "receipt": str(receipt), "resources": resources,
            "source_only": True, "source_execution": False, "evidence_only": True,
            "proof_created": False, "callable_abi_admitted": False, "lifetime_admitted": False}
