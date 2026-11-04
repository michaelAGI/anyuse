"""Offline preparation of the fixed runtime adapter from explicit cached tools.

No installer, target-app argument, Frida import, process attachment or permission
probe exists here. Cached bytes are audited build inputs, not authenticated
registry downloads. The compiler runs only on build(), in a fresh workspace.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

SOURCE = Path(__file__).with_name("rea_runtime_agent.js")
SOURCE_SHA256 = "77b94169960213bf1594a79a62aa6bdb83dab22fa76f3439f0a7d1a32b402db1"
VERSIONS = {"frida-compile": "19.1.0", "frida-objc-bridge": "8.0.6", "frida-swift-bridge": "3.0.2"}
COMPILER = "frida-compile/dist/cli.js"
COMPILER_SHA256 = "4368b342a6437d86664d4a50f26ab014aec337ff5871d650e434ea3931e3bde8"
PROTOCOL = "anyuse.rea-phased-permit/1"
MAX_FILES = 20000
MAX_TREE_BYTES = 512 * 1024**2
MAX_CACHE_FILE_BYTES = 128 * 1024**2
MAX_FILE_BYTES = 64 * 1024**2
MAX_NODE_BYTES = 256 * 1024**2
MAX_BUNDLE_BYTES = 8 * 1024**2
MAX_OUTPUT_BYTES = 1024**2


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _raw(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _fields(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, maximum, *, allow_empty=False):
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and (0 if allow_empty else 1) <= before.st_size <= maximum,
          "bounded regular build input required: " + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        _need(_fields(os.fstat(stream.fileno())) == _fields(before), "build input changed on open")
        raw = stream.read(maximum + 1)
        _need(_fields(os.fstat(stream.fileno())) == _fields(before) == _fields(path.lstat()),
              "build input changed while reading")
    _need(len(raw) == before.st_size, "build input size changed")
    return raw, {"file": str(path), "sha256": _hash(raw), "bytes": len(raw),
                 "identity": list(_fields(before))}


def _cache_pin(path):
    """Hash a bounded cached file in chunks, including large native addons."""
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= MAX_CACHE_FILE_BYTES,
          "bounded regular cached input required: " + str(path))
    h, total = hashlib.sha256(), 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        _need(_fields(os.fstat(stream.fileno())) == _fields(before), "cached input changed on open")
        while True:
            chunk = stream.read(min(1024**2, MAX_CACHE_FILE_BYTES - total + 1))
            if not chunk:
                break
            total += len(chunk)
            _need(total <= MAX_CACHE_FILE_BYTES, "cached file exceeds byte bound")
            h.update(chunk)
        _need(total == before.st_size and
              _fields(os.fstat(stream.fileno())) == _fields(before) == _fields(path.lstat()),
              "cached input changed while hashing")
    return {"file": str(path), "sha256": h.hexdigest(), "bytes": total,
            "identity": list(_fields(before))}


def _canonical(value, *, directory=False):
    path = Path(value).expanduser().absolute()
    _need(not path.is_symlink(), "explicit build root/tool cannot be a symlink")
    path = path.resolve(strict=True)
    _need(path.is_dir() if directory else path.is_file(), "build input has the wrong kind")
    return path


def _tree(root):
    """Pin every cached entry, without exclusions or following directory links."""
    rows, total = {}, 0
    def walk(directory, depth=0):
        nonlocal total
        _need(depth <= 64, "cached module tree exceeds directory-depth bound")
        for path in sorted(directory.iterdir()):
            _need(len(rows) < MAX_FILES, "cached module tree exceeds file bound")
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                _need(target.is_relative_to(root) and (target.is_file() or target.is_dir()),
                      "cached symlink must resolve inside the explicit module root")
                rows[relative] = {"kind": "symlink", "target": os.readlink(path),
                                  "resolved": target.relative_to(root).as_posix(), "identity": list(_fields(info))}
                _need(_fields(path.lstat()) == _fields(info), "cached symlink changed")
            elif stat.S_ISDIR(info.st_mode):
                rows[relative] = {"kind": "directory", "identity": list(_fields(info))}
                walk(path, depth + 1)
                _need(_fields(path.lstat()) == _fields(info), "cached directory changed")
            else:
                # Empty metadata/files are harmless, but special files are not.
                _need(stat.S_ISREG(info.st_mode), "cached module tree contains a special file")
                pin = _cache_pin(path)
                total += pin["bytes"]
                _need(total <= MAX_TREE_BYTES, "cached module tree exceeds byte bound")
                rows[relative] = {"kind": "file", **pin}
    before = _fields(root.stat())
    walk(root)
    _need(_fields(root.stat()) == before, "cached module root changed")
    return {"root": str(root), "root_identity": list(before), "entries": rows,
            "entry_count": len(rows), "bytes": total, "sha256": _hash(_raw(rows))}


def _snapshot(node, modules):
    node, modules = _canonical(node), _canonical(modules, directory=True)
    _need(os.access(node, os.X_OK), "cached Node executable is not executable")
    _, node_pin = _read(node, MAX_NODE_BYTES)
    source, source_pin = _read(SOURCE.resolve(strict=True), MAX_FILE_BYTES)
    _need(source_pin["sha256"] == SOURCE_SHA256, "bundled fixed adapter source changed")
    tree = _tree(modules)
    packages = {}
    for name, version in VERSIONS.items():
        package = (modules / name / "package.json").resolve(strict=True)
        _need(package.is_relative_to(modules), "package escapes explicit module root")
        raw, pin = _read(package, 1024**2)
        value = json.loads(raw)
        _need(type(value) is dict and value.get("name") == name and value.get("version") == version,
              "cached dependency must be " + name + "@" + version)
        packages[name] = {"version": version, "package": pin}
    compiler = (modules / COMPILER).resolve(strict=True)
    _need(compiler.is_relative_to(modules), "compiler escapes explicit module root")
    _, compiler_pin = _read(compiler, MAX_FILE_BYTES)
    _need(compiler_pin["sha256"] == COMPILER_SHA256, "fixed cached compiler entry changed")
    return {"node": node_pin, "source": source_pin, "modules": tree,
            "compiler": compiler_pin, "packages": packages}, source


def _adapter(path, source_sha):
    path = _canonical(path)
    raw, pin = _read(path, MAX_BUNDLE_BYTES)
    manifest_raw, manifest_pin = _read(Path(str(path) + ".manifest.json"), 65536)
    value = json.loads(manifest_raw)
    _need(type(value) is dict and value.get("format") == "anyuse.rea-runtime-adapter/1" and
          value.get("source_sha256") == source_sha and value.get("bundle_sha256") == pin["sha256"] and
          value.get("interlock_protocol") == PROTOCOL and value.get("dependencies") == VERSIONS,
          "compiled adapter/source/dependency/permit manifest changed")
    return {"bundle": pin, "manifest": manifest_pin, "build": value}


def doctor(node, modules, adapter=None):
    """Read-only cache/adapter check; readiness is not process access or proof."""
    result = {"format": "anyuse.rea-runtime-build-check/1", "status": "unavailable", "build_ready": False,
              "process_access_checked": False, "frida_loaded": False, "compiler_calls": 0,
              "dependency_authentication": "explicit cached bytes pinned; no registry authentication performed"}
    try:
        result["inputs"], _ = _snapshot(node, modules)
        if adapter is not None:
            result["adapter"] = _adapter(adapter, result["inputs"]["source"]["sha256"])
        try:
            result["python_frida_distribution"] = importlib.metadata.version("frida")
        except importlib.metadata.PackageNotFoundError:
            result["python_frida_distribution"] = None
        result.update(status="ready", build_ready=True)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
    return result


def _write(path, value):
    raw = _raw(value)
    _need(len(raw) <= 32 * 1024**2, "build receipt exceeds byte bound")
    path.write_bytes(raw)


def _group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _compile(command, cwd, env, folder, timeout):
    """One owned process group; bounded raw pipes, no shell or retry."""
    result = {"command": command, "timeout_seconds": timeout, "returncode": None,
              "output_overflow": False, "timed_out": False, "cleanup_confirmed": False,
              "termination_signals": [], "error": None}
    started = time.monotonic()
    proc = None
    selector = selectors.DefaultSelector()
    streams = {}
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        for name in ("stdout", "stderr"):
            pipe = getattr(proc, name)
            streams[pipe.fileno()] = [folder / ("compiler." + name), 0]
            streams[pipe.fileno()][0].write_bytes(b"")
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map() or proc.poll() is None:
            if time.monotonic() - started >= timeout:
                result["timed_out"] = True
                raise TimeoutError("fixed adapter compiler timed out")
            for key, _ in selector.select(min(.1, max(.001, timeout - (time.monotonic() - started)))):
                path, count = streams[key.fd]
                data = os.read(key.fd, min(65536, MAX_OUTPUT_BYTES - count + 1))
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                keep = data[:MAX_OUTPUT_BYTES - count]
                with path.open("ab") as stream:
                    stream.write(keep)
                streams[key.fd][1] += len(keep)
                if len(keep) != len(data):
                    result["output_overflow"] = True
                    raise ValueError("compiler raw output exceeded byte bound; retained prefix only")
        result["returncode"] = proc.wait(timeout=1)
    except BaseException as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
        if not isinstance(error, Exception):
            raise
    finally:
        selector.close()
        if proc is not None:
            # The group was created by this Popen only. No app/user process is a target.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if not _group_exists(proc.pid):
                    break
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                result["termination_signals"].append(int(sig))
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            result["returncode"] = proc.poll()
            result["cleanup_confirmed"] = not _group_exists(proc.pid) and result["returncode"] is not None
            for name in ("stdout", "stderr"):
                getattr(proc, name).close()
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        result["outputs"] = {}
        for name in ("stdout", "stderr"):
            path = folder / ("compiler." + name)
            if path.is_file():
                raw = path.read_bytes()
                _need(len(raw) <= MAX_OUTPUT_BYTES, "retained compiler output exceeded byte bound")
                result["outputs"][name] = {"file": str(path), "bytes": len(raw), "sha256": _hash(raw)}
        _write(folder / "compiler.json", result)
    return result


def _bundle(path, work, modules):
    """Validate complete Frida bundle framing and every retained source-map input."""
    raw, pin = _read(path, MAX_BUNDLE_BYTES)
    _need(raw.startswith("📦\n".encode()), "compiler output is not a Frida bundle")
    header, body = raw.split("✄\n".encode(), 1)
    rows = header.decode("utf-8").splitlines()[1:]
    _need(len(rows) == 2, "fixed build needs its code and complete source map")
    files = {}
    for i, row in enumerate(rows):
        match = re.fullmatch(r"([1-9][0-9]{0,7}) (/rea-runtime-agent\.js(?:\.map)?)", row)
        _need(match is not None and match[2] not in files, "invalid fixed bundle header")
        size = int(match[1])
        _need(len(body) >= size, "truncated compiler bundle")
        files[match[2]], body = body[:size], body[size:]
        if i < len(rows) - 1:
            _need(body.startswith("\n✄\n".encode()), "truncated compiler bundle separator")
            body = body[len("\n✄\n".encode()):]
    _need(not body and set(files) == {"/rea-runtime-agent.js", "/rea-runtime-agent.js.map"},
          "incomplete or extra compiler bundle content")
    _need(b"rpc.exports" in files["/rea-runtime-agent.js"], "compiled fixed adapter lacks its RPC export")
    mapping = json.loads(files["/rea-runtime-agent.js.map"])
    _need(type(mapping) is dict and mapping.get("version") == 3 and
          type(mapping.get("sources")) is list and 1 <= len(mapping["sources"]) <= 512,
          "complete compiler source map required")
    sources = []
    for source in mapping["sources"]:
        _need(type(source) is str and 0 < len(source) <= 4096, "invalid source-map input")
        if source.startswith("frida-builtins:/"):
            _need(".." not in Path(source[len("frida-builtins:/"):]).parts, "invalid builtin source")
            sources.append({"builtin": source})
            continue
        resolved = (work / source).resolve(strict=True)
        _need(resolved == work / "rea-runtime-agent.js" or resolved.is_relative_to(modules),
              "compiled source escapes audited fixed source/module cache")
        _, item = _read(resolved, MAX_FILE_BYTES)
        sources.append(item)
    _need(any(row.get("file") == str(work / "rea-runtime-agent.js") for row in sources),
          "source map omits the fixed adapter input")
    return {"bundle": pin, "source_map_inputs": sources,
            "scope": "all cached bytes pinned; bundled source-map inputs confined; no OS file-read tracing claim"}


def build(node, modules, out, timeout=120):
    """Compile this exact bundled source once with the admitted offline cache."""
    _need(type(timeout) is int and 1 <= timeout <= 600, "compiler timeout must be 1–600 seconds")
    before, source = _snapshot(node, modules)
    folder = Path(out).expanduser().absolute().resolve()
    modules = Path(before["modules"]["root"])
    _need(not folder.exists() and not folder.is_relative_to(modules) and not modules.is_relative_to(folder) and
          not folder.is_relative_to(SOURCE.resolve().parent), "fresh output outside source/module cache required")
    folder.mkdir(mode=0o700, parents=True)
    work = folder / "work"
    work.mkdir(mode=0o700)
    (work / "node_modules").symlink_to(modules, target_is_directory=True)
    (work / "rea-runtime-agent.js").write_bytes(source)
    for name in ("home", "tmp"):
        (work / name).mkdir(mode=0o700)
    bundle = folder / "rea-runtime-agent.bundle.js"
    command = [before["node"]["file"], before["compiler"]["file"], str(work / "rea-runtime-agent.js"),
               "-o", str(bundle), "-c"]
    env = {"PATH": str(Path(before["node"]["file"]).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(work / "home"), "TMPDIR": str(work / "tmp"), "NO_COLOR": "1"}
    result = {"format": "anyuse.rea-runtime-build/1", "status": "building", "error": None,
              "command": command, "environment": env, "compiler_calls": 0, "downloads": 0,
              "installations": 0, "app_launches": 0, "attachments": 0, "native_calls": 0,
              "model_calls": 0, "cache_unchanged": False, "source_unchanged": False}
    _write(folder / "inputs-before.json", before)
    _write(folder / "build.json", result)
    try:
        # Recheck immediately before executing, not just while constructing output.
        check, _ = _snapshot(before["node"]["file"], modules)
        _need(check == before, "build inputs changed before compiler dispatch")
        result["compiler_calls"] = 1
        compiler = _compile(command, work, env, folder, timeout)
        result["compiler"] = compiler
        _need(compiler.get("returncode") == 0 and compiler.get("cleanup_confirmed") is True and
              compiler.get("error") is None and compiler.get("output_overflow") is False and
              compiler.get("timed_out") is False, "fixed compiler failed or owned cleanup needs review")
        result["output"] = _bundle(bundle, work, modules)
    except BaseException as error:
        result.update(status="failed", error={"type": type(error).__name__, "message": str(error)[:2048]})
        if not isinstance(error, Exception):
            raise
    finally:
        try:
            after, _ = _snapshot(before["node"]["file"], modules)
            _write(folder / "inputs-after.json", after)
            result["cache_unchanged"] = after["modules"] == before["modules"]
            result["source_unchanged"] = after["source"] == before["source"]
            _need(after == before and (work / "rea-runtime-agent.js").read_bytes() == source,
                  "source/tool/dependency bytes or identity changed during build")
            _need((work / "node_modules").is_symlink() and (work / "node_modules").resolve() == modules,
                  "build module binding changed")
            if result["status"] == "building":
                body = {"format": "anyuse.rea-runtime-adapter/1", "source_sha256": before["source"]["sha256"],
                        "bundle_sha256": result["output"]["bundle"]["sha256"], "compiler": "frida-compile19.1.0",
                        "dependencies": VERSIONS, "source_manifest_sha256": _hash(_raw(before)),
                        "interlock_protocol": PROTOCOL}
                manifest = Path(str(bundle) + ".manifest.json")
                _write(manifest, body)
                result["adapter"] = _adapter(bundle, before["source"]["sha256"])
                result["status"] = "built"
        except (OSError, ValueError, TypeError) as error:
            result.update(status="failed", error={"type": type(error).__name__, "message": str(error)[:2048]})
        _write(folder / "build.json", result)
    return result
