"""Fresh, pinned runtime compiler dependencies in one caller-owned prefix.

This installs npm dependencies, not an adapter or app integration. npm lifecycle
scripts are disabled. The sole admitted lifecycle entry runs only after its
reviewed early-exit condition (an authenticated native binding) is established.
No app, model, Frida Python import, attachment or global installation occurs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import selectors
import shutil
import signal
import subprocess
import tarfile
import time
from urllib.parse import urlsplit

import httpx

from . import rea_runtime_build as B
from . import rea_setup as S

FORMAT = "anyuse.rea-runtime-setup/1"
VERSIONS = {**B.VERSIONS, "frida": "17.23.3"}
INTEGRITIES = {
    "frida": "sha512-h3UPc770oHxdHY6GX24kxESGTSMGNo/Sa3PtMcGKfGp/TNoBu+Lv+ufSmtyhx59+8N5n6La1aTWEU86aqBCZrg==",
    "frida-compile": "sha512-tHzBRF2QKn4Jm3L5ofQfB5AH8wlaYyDKoaWeSvckXN8FNr5qvJpp8/efG8A+ukHMlJXHAgOcNTQfyvRDsfuDzQ==",
    "frida-objc-bridge": "sha512-FafCVXbh8837VIyYv9OjJFwPv8w3EiJbDpdQbK50316TfY1paK8URiICzEQIboRLgAjtoi9MugXnys2DOZbiUg==",
    "frida-swift-bridge": "sha512-7rfr10t/F3JMBtmfc92462lWhxEmKFP8Wz4z5v2rR0Ni/05+q1TaNoSnrVlwm9hWe0BxVQFr9LfWo3MnG8YGnA==",
}
BINDING_URL = "https://github.com/frida/frida/releases/download/17.23.3/frida-v17.23.3-napi-v8-darwin-arm64.tar.gz"
BINDING_SHA256 = "a94cb164c135dc342d0c5331d640396997e04454e69339653e63535288080085"
BINDING_DIGEST_SOURCE = "https://github.com/frida/frida/releases/expanded_assets/17.23.3"
INSTALL_SHA256 = "c28b6cad456a3e185463baabc3b93024b94a17f4ff0d42f286290ddc9c68050c"
INSTALL_RELATIVE = "frida/scripts/install.js"
RESERVE_BYTES = 500 * 1024**2
MAX_INSTALL_BYTES = B.MAX_TREE_BYTES
MAX_ARCHIVE_BYTES = 64 * 1024**2
MAX_NODE_ARCHIVE_BYTES = 128 * 1024**2
MAX_LOCK_BYTES = 8 * 1024**2
_need = B._need


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("non-finite JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _portable(tree):
    return {name: {key: value for key, value in row.items()
                   if key in {"kind", "sha256", "bytes", "target"}}
            for name, row in tree["entries"].items()}


def _archive_rows(path, *, strip_root=None, maximum=B.MAX_TREE_BYTES):
    """Read bounded archive contents; never extract paths or follow links."""
    rows, total = {}, 0
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            _need(len(rows) < B.MAX_FILES, "archive member limit exceeded")
            name = member.name.rstrip("/")
            parts = PurePosixPath(name).parts
            _need(name and not name.startswith("/") and all(p not in {".", ".."} for p in parts)
                  and "\\" not in name, "unsafe archive path")
            if strip_root is not None:
                _need(parts[0] == strip_root, "unexpected archive root")
                if len(parts) == 1:
                    _need(member.isdir(), "archive root must be a directory")
                    continue
                name = "/".join(parts[1:])
            _need(name not in rows, "duplicate archive member")
            if member.isdir():
                row = {"kind": "directory"}
            elif member.issym():
                row = {"kind": "symlink", "target": member.linkname}
            else:
                _need(member.isfile() and 0 <= member.size <= B.MAX_NODE_BYTES,
                      "archive requires bounded regular files")
                total += member.size
                _need(total <= maximum, "expanded archive byte limit exceeded")
                digest, count = hashlib.sha256(), 0
                with archive.extractfile(member) as stream:
                    while chunk := stream.read(1024**2):
                        count += len(chunk)
                        _need(count <= member.size, "archive size changed")
                        digest.update(chunk)
                _need(count == member.size, "truncated archive member")
                row = {"kind": "file", "bytes": count, "sha256": digest.hexdigest()}
            rows[name] = row
    return rows


def _node_input(node):
    node = B._canonical(node)
    home = node.parent.parent
    _need(node == home / "bin/node" and home.name == "node" and os.access(node, os.X_OK),
          "explicit REA setup Node executable required")
    archive = home.parent / "dl" / Path(S.PINS["node"]["url"]).name
    _, archive_pin = B._read(archive, MAX_NODE_ARCHIVE_BYTES)
    _need(archive_pin["sha256"] == S.PINS["node"]["sha256"], "REA setup Node archive checksum changed")
    tree = B._tree(home)
    expected = _archive_rows(archive, strip_root=archive.name.removesuffix(".tar.gz"))
    _need(_portable(tree) == expected, "Node/npm installation differs from authenticated setup archive")
    npm = home / "lib/node_modules/npm/bin/npm-cli.js"
    _, npm_pin = B._read(npm, 1024**2)
    _, node_pin = B._read(node, B.MAX_NODE_BYTES)
    _, source_pin = B._read(B.SOURCE, B.MAX_FILE_BYTES)
    _need(source_pin["sha256"] == B.SOURCE_SHA256, "fixed runtime source changed")
    return {"node": node_pin, "npm": npm_pin, "archive": archive_pin, "tree": tree,
            "source": source_pin, "archive_url": S.PINS["node"]["url"],
            "installer_sources": [B._read(path, B.MAX_FILE_BYTES)[1] for path in
                                  (Path(__file__).resolve(), Path(B.__file__).resolve(), Path(S.__file__).resolve())]}


def _lock(path):
    raw, pin = B._read(path, MAX_LOCK_BYTES)
    value = _json(raw)
    _need(type(value) is dict and value.get("lockfileVersion") == 3 and
          type(value.get("packages")) is dict and 1 < len(value["packages"]) <= 2000,
          "bounded npm v3 lock required")
    entries = value["packages"]
    _need(type(entries.get("")) is dict and entries[""].get("dependencies") == VERSIONS,
          "lock root must retain the four exact runtime versions")
    for name, item in entries.items():
        if not name:
            continue
        _need(type(name) is str and name.startswith("node_modules/") and
              ".." not in PurePosixPath(name).parts and "\\" not in name and
              type(item) is dict and item.get("link") is not True,
              "only registry package lock entries admitted")
        url = urlsplit(item.get("resolved", ""))
        _need(url.scheme == "https" and url.netloc == "registry.npmjs.org" and
              url.path.endswith(".tgz") and not url.query and not url.fragment,
              "every dependency must resolve to the fixed HTTPS npm registry")
        integrity = item.get("integrity")
        _need(type(integrity) is str and integrity.startswith("sha512-"), "SHA-512 npm integrity required")
        try:
            digest = base64.b64decode(integrity[7:], validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid npm integrity") from error
        _need(len(digest) == 64, "invalid npm SHA-512 length")
    for name, version in VERSIONS.items():
        item = entries.get("node_modules/" + name, {})
        _need(item.get("version") == version and item.get("integrity") == INTEGRITIES[name] and
              item.get("resolved") == f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz",
              "reviewed top-level runtime package/version/integrity changed: " + name)
    return {"pin": pin, "packages": entries, "package_count": len(entries) - 1}


def _space(folder, *, preflight=False):
    available = shutil.disk_usage(folder).free
    _need(available >= RESERVE_BYTES + (MAX_INSTALL_BYTES if preflight else 0),
          "runtime setup storage reserve would be exceeded")
    return available


def _environment(folder, node):
    # Keep the caller's actual HOME; npm config/cache and temporary state are isolated.
    env = {key: os.environ[key] for key in ("HOME", "DEVELOPER_DIR") if key in os.environ}
    env.update(PATH=str(node.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin", NO_COLOR="1",
               TMPDIR=str(folder / "tmp"), XDG_CACHE_HOME=str(folder / "cache"),
               NPM_CONFIG_CACHE=str(folder / "npm-cache"),
               NPM_CONFIG_USERCONFIG=str(folder / "npm-user.conf"),
               NPM_CONFIG_GLOBALCONFIG=str(folder / "npm-global.conf"),
               NPM_CONFIG_UPDATE_NOTIFIER="false", NPM_CONFIG_AUDIT="false", NPM_CONFIG_FUND="false")
    return env


def _command(command, cwd, env, folder, timeout):
    """Bounded owned subprocess group, with a continuously checked disk reserve."""
    result = {"command": command, "returncode": None, "error": None, "cleanup_confirmed": False,
              "termination_signals": [], "timeout_seconds": timeout}
    started, proc = time.monotonic(), None
    selector, streams = selectors.DefaultSelector(), {}
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        for name in ("stdout", "stderr"):
            path, pipe = folder / name, getattr(proc, name)
            path.write_bytes(b"")
            streams[pipe.fileno()] = [path, 0]
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map() or proc.poll() is None:
            _need(time.monotonic() - started < timeout, "runtime setup command timed out")
            _space(folder)
            for key, _ in selector.select(.1):
                path, count = streams[key.fd]
                data = os.read(key.fd, min(65536, B.MAX_OUTPUT_BYTES - count + 1))
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                keep = data[:B.MAX_OUTPUT_BYTES - count]
                with path.open("ab") as stream:
                    stream.write(keep)
                streams[key.fd][1] += len(keep)
                _need(len(keep) == len(data), "runtime setup output exceeded bound; retained prefix only")
        result["returncode"] = proc.wait(timeout=1)
    except BaseException as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
        if not isinstance(error, Exception):
            raise
    finally:
        selector.close()
        if proc is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if not B._group_exists(proc.pid):
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
            result["cleanup_confirmed"] = not B._group_exists(proc.pid) and proc.poll() is not None
            for name in ("stdout", "stderr"):
                getattr(proc, name).close()
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        result["outputs"] = {name: B._read(folder / name, B.MAX_OUTPUT_BYTES, allow_empty=True)[1]
                             for name in ("stdout", "stderr") if (folder / name).exists()}
        B._write(folder / "command.json", result)
    return result


def _download(path, deadline):
    """One pinned HTTPS asset; bounded redirects, size and total elapsed time."""
    allowed = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    current = BINDING_URL
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        for _ in range(4):
            remaining = deadline - time.monotonic()
            _need(remaining > 0, "runtime setup download timed out")
            parsed = urlsplit(current)
            _need(parsed.scheme == "https" and parsed.hostname in allowed and parsed.port in (None, 443)
                  and parsed.username is None and parsed.password is None, "unapproved binding download redirect")
            with client.stream("GET", current, timeout=min(30, remaining)) as response:
                if response.is_redirect:
                    current = str(response.next_request.url)
                    continue
                response.raise_for_status()
                _need(response.status_code == 200, "binding download requires a complete response")
                total, digest = 0, hashlib.sha256()
                with path.open("xb") as stream:
                    for chunk in response.iter_bytes(65536):
                        _need(time.monotonic() < deadline, "runtime setup download timed out")
                        total += len(chunk)
                        _need(total <= MAX_ARCHIVE_BYTES, "binding download exceeded byte bound")
                        _space(path.parent)
                        stream.write(chunk)
                        digest.update(chunk)
                _need(total > 0 and digest.hexdigest() == BINDING_SHA256, "native binding archive checksum mismatch")
                return {"url": BINDING_URL, "final_host": parsed.hostname, "digest_source": BINDING_DIGEST_SOURCE,
                        "verified_sha256": BINDING_SHA256, "pin": B._read(path, MAX_ARCHIVE_BYTES)[1]}
    raise ValueError("binding download exceeded redirect limit")


def _binding(archive_path, package):
    _, pin = B._read(archive_path, MAX_ARCHIVE_BYTES)
    _need(pin["sha256"] == BINDING_SHA256, "native binding archive checksum mismatch")
    rows = _archive_rows(archive_path, maximum=B.MAX_CACHE_FILE_BYTES)
    _need(set(rows) in ({"build/frida_binding.node"}, {"build", "build/frida_binding.node"}) and
          rows["build/frida_binding.node"]["kind"] == "file" and
          rows["build/frida_binding.node"]["bytes"] > 0 and
          ("build" not in rows or rows["build"]["kind"] == "directory"),
          "prebuilt binding archive contains unexpected roles")
    directory = package / "build"
    _need(not directory.is_symlink() and (not directory.exists() or directory.is_dir()),
          "regular package build directory required")
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / "frida_binding.node"
    _need(not target.exists() and not target.is_symlink(), "fresh native binding required")
    with tarfile.open(archive_path, "r:gz") as archive:
        with archive.extractfile("build/frida_binding.node") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024**2)
    actual = B._cache_pin(target)
    _need(actual["sha256"] == rows["build/frida_binding.node"]["sha256"] and
          actual["bytes"] == rows["build/frida_binding.node"]["bytes"], "binding extraction changed bytes")
    return actual


def setup(*, node, out, timeout=900):
    """Install into a fresh prefix. Preflight errors raise; started failures retain receipts."""
    _need(type(timeout) is int and 1 <= timeout <= 3600, "setup timeout must be 1–3600 seconds")
    _need(platform.system() == "Darwin" and platform.machine() == "arm64", "runtime setup requires macOS arm64")
    deadline = time.monotonic() + timeout
    before = _node_input(node)
    node_path = Path(before["node"]["file"])
    candidate = Path(out).expanduser().absolute()
    _need(not candidate.exists() and not candidate.is_symlink(), "new nonexistent runtime prefix required")
    folder = candidate.parent.resolve(strict=True) / candidate.name
    _need(not folder.is_relative_to(node_path.parent.parent) and
          not folder.is_relative_to(Path(__file__).resolve().parent), "runtime prefix must be outside tool/source inputs")
    free = _space(folder.parent, preflight=True)
    folder.mkdir(mode=0o700)
    for name in ("tmp", "cache", "npm-cache", "downloads", "steps", "tools"):
        (folder / name).mkdir(mode=0o700)
    for name in ("npm-user.conf", "npm-global.conf"):
        (folder / name).write_bytes(b"")
    env, work = _environment(folder, node_path), folder / "tools"
    package = {"name": "anyuse-private-runtime", "version": "1.0.0", "private": True, "dependencies": VERSIONS}
    B._write(work / "package.json", package)
    result = {"format": FORMAT, "status": "installing", "error": None, "prefix": str(folder),
              "node": str(node_path), "modules": str(work / "node_modules"), "source": before["source"],
              "environment": env, "steps": [], "free_bytes_before": free,
              "reserve_bytes": RESERVE_BYTES, "max_install_bytes": MAX_INSTALL_BYTES,
              "provenance": {"node_archive": before["archive"], "node_archive_url": before["archive_url"],
                  "native_digest_source": BINDING_DIGEST_SOURCE, "npm_top_integrities": INTEGRITIES,
                  "installer_sources": before["installer_sources"],
                  "transitive_policy": "new registry lock with SHA-512 per entry; versions frozen by retained lock"},
              "app_launches": 0, "attachments": 0, "native_calls": 0, "model_calls": 0,
              "adapter_compilations": 0, "cleanup_confirmed": True, "inputs_unchanged": False}
    B._write(folder / "inputs-before.json", before)
    B._write(folder / "setup.json", result)
    def run(label, args, cwd=work):
        _need(_node_input(node_path) == before, "Node/npm/source inputs changed before command dispatch")
        remaining = deadline - time.monotonic()
        _need(remaining > 0, "runtime setup deadline exhausted")
        step = folder / "steps" / f"{len(result['steps']):02d}-{label}"
        step.mkdir(mode=0o700)
        try:
            record = _command(args, cwd, env, step, remaining)
        except BaseException:
            result["cleanup_confirmed"] = False
            if (step / "command.json").is_file():
                record = _json(B._read(step / "command.json", B.MAX_OUTPUT_BYTES)[0])
                result["steps"].append({"name": label, **record})
                result["cleanup_confirmed"] = all(item.get("cleanup_confirmed") is True for item in result["steps"])
            B._write(folder / "setup.json", result)
            raise
        result["steps"].append({"name": label, **record})
        result["cleanup_confirmed"] = all(item["cleanup_confirmed"] for item in result["steps"])
        B._write(folder / "setup.json", result)
        _need(record["returncode"] == 0 and record["error"] is None and record["cleanup_confirmed"],
              "runtime setup command failed or owned cleanup needs review: " + label)
        _space(folder)
        _need(B._tree(folder)["bytes"] <= MAX_INSTALL_BYTES, "runtime installation exceeds expanded tree bound")
    try:
        npm = [str(node_path), before["npm"]["file"]]
        flags = ["--ignore-scripts", "--registry=https://registry.npmjs.org", "--no-audit", "--no-fund",
                 "--update-notifier=false", "--fetch-retries=0", "--fetch-timeout=60000"]
        run("resolve-lock", npm + ["install", "--package-lock-only"] + flags)
        locked = _lock(work / "package-lock.json")
        result["lock"] = locked["pin"]
        B._write(folder / "resolved-lock.json", locked)
        _need(not (work / "node_modules").exists(), "lock resolution unexpectedly created modules")
        run("install-packages", npm + ["ci"] + flags)
        _need(_lock(work / "package-lock.json") == locked, "npm ci changed the resolved lock")
        modules = work / "node_modules"
        for name, version in VERSIONS.items():
            value = _json(B._read(modules / name / "package.json", 1024**2)[0])
            _need(value.get("name") == name and value.get("version") == version, "installed package version changed")
        script = modules / INSTALL_RELATIVE
        _, script_pin = B._read(script, 65536)
        _need(script_pin["sha256"] == INSTALL_SHA256, "reviewed native install entry changed")
        _, compiler_pin = B._read(modules / B.COMPILER, B.MAX_FILE_BYTES)
        _need(compiler_pin["sha256"] == B.COMPILER_SHA256, "runtime compiler entry changed")
        result["install_script"] = script_pin
        asset = folder / "downloads" / Path(BINDING_URL).name
        result["native_archive"] = _download(asset, deadline)
        result["binding"] = _binding(asset, modules / "frida")
        pre_script = B._tree(modules)
        B._write(folder / "modules-before-script.json", pre_script)
        _need(B._read(script, 65536)[1] == script_pin and
              B._cache_pin(Path(result["binding"]["file"])) == result["binding"], "reviewed install inputs changed")
        run("reviewed-native-entry", [str(node_path), str(script)], cwd=modules / "frida")
        after_script = B._tree(modules)
        _need(after_script == pre_script, "native install early-exit changed package bytes or identity")
        B._write(folder / "modules.json", after_script)
        result["module_tree"] = {key: value for key, value in after_script.items() if key != "entries"}
        result["compiler"] = compiler_pin
        result["status"] = "installed"
    except BaseException as error:
        result.update(status="failed", error={"type": type(error).__name__, "message": str(error)[:2048]})
        if not isinstance(error, Exception):
            raise
    finally:
        try:
            after = _node_input(node_path)
            B._write(folder / "inputs-after.json", after)
            result["inputs_unchanged"] = after == before
            _need(result["inputs_unchanged"], "Node/npm/source inputs changed during runtime installation")
            result["free_bytes_after"] = _space(folder)
            _need(B._tree(folder)["bytes"] <= MAX_INSTALL_BYTES, "runtime installation exceeds expanded tree bound")
        except Exception as error:
            issue = {"type": type(error).__name__, "message": str(error)[:2048]}
            result["final_check_error"] = issue
            if result["error"] is None:
                result["error"] = issue
            result["status"] = "failed"
        B._write(folder / "setup.json", result)
    return result
