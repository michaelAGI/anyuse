"""Inert dependency installation boundary tests; no downloads or subprocesses."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from anyuse.execution import rea_runtime_setup as R


def tar(path, rows):
    with tarfile.open(path, "w:gz") as archive:
        for name, body in rows:
            item = tarfile.TarInfo(name)
            if isinstance(body, str):
                item.type, item.linkname = tarfile.SYMTYPE, body
                archive.addfile(item)
            elif body is None:
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            else:
                item.size = len(body)
                archive.addfile(item, io.BytesIO(body))


def lock():
    entries = {"": {"dependencies": R.VERSIONS}}
    for name, version in R.VERSIONS.items():
        entries["node_modules/" + name] = {"version": version, "integrity": R.INTEGRITIES[name],
            "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"}
    entries["node_modules/example-dependency"] = {"version": "1.0.0",
        "resolved": "https://registry.npmjs.org/example-dependency/-/example-dependency-1.0.0.tgz",
        "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode()}
    return {"lockfileVersion": 3, "packages": entries}


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    home = tmp_path / "setup/node"
    node = home / "bin/node"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"inert Node fixture")
    node.chmod(0o700)
    npm = home / "lib/node_modules/npm/bin/npm-cli.js"
    npm.parent.mkdir(parents=True)
    npm.write_bytes(b"inert npm fixture")
    (home / "bin/npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
    archive = tmp_path / "setup/dl/node-v24.22.0-darwin-arm64.tar.gz"
    archive.parent.mkdir()
    root = archive.name.removesuffix(".tar.gz")
    rows = [(root, None)]
    for path in sorted(home.rglob("*")):
        name = root + "/" + path.relative_to(home).as_posix()
        rows.append((name, path.readlink().as_posix() if path.is_symlink() else
                     None if path.is_dir() else path.read_bytes()))
    tar(archive, rows)
    node_pins = {**R.S.PINS, "node": {**R.S.PINS["node"], "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}}
    monkeypatch.setattr(R.S, "PINS", node_pins)
    source = tmp_path / "source.js"
    source.write_bytes(b"inert fixed adapter fixture")
    monkeypatch.setattr(R.B, "SOURCE", source)
    monkeypatch.setattr(R.B, "SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(R.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(R.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(R.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 4 * 1024**3})())
    return node, archive, source


@pytest.fixture
def installer(inputs, monkeypatch, tmp_path):
    archive = tmp_path / "binding-fixture.tar.gz"
    tar(archive, [("build", None), ("build/frida_binding.node", b"inert native binding")])
    monkeypatch.setattr(R, "BINDING_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    script = b"inert reviewed installer fixture"
    compiler = b"inert compiler fixture"
    monkeypatch.setattr(R, "INSTALL_SHA256", hashlib.sha256(script).hexdigest())
    monkeypatch.setattr(R.B, "COMPILER_SHA256", hashlib.sha256(compiler).hexdigest())
    calls, downloads = [], []
    def command(command, cwd, env, folder, timeout):
        calls.append({"command": command, "cwd": cwd, "env": dict(env), "timeout": timeout})
        if "--package-lock-only" in command:
            (cwd / "package-lock.json").write_text(json.dumps(lock()))
        elif "ci" in command:
            modules = cwd / "node_modules"
            modules.mkdir()
            for name, version in R.VERSIONS.items():
                package = modules / name
                package.mkdir()
                (package / "package.json").write_text(json.dumps({"name": name, "version": version}))
            for relative, content in [(R.INSTALL_RELATIVE, script), (R.B.COMPILER, compiler),
                                      ("frida/build/src/frida.js", b"inert shipped wrapper")]:
                path = modules / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        else:
            assert Path(command[1]) == cwd / "scripts/install.js"
            assert (cwd / "build/frida_binding.node").read_bytes() == b"inert native binding"
        for name in ("stdout", "stderr"):
            (folder / name).write_bytes(b"inert command receipt")
        return {"command": command, "returncode": 0, "error": None, "cleanup_confirmed": True}
    def download(path, deadline):
        downloads.append(path)
        path.write_bytes(archive.read_bytes())
        return {"url": R.BINDING_URL, "pin": R.B._read(path, R.MAX_ARCHIVE_BYTES)[1]}
    monkeypatch.setattr(R, "_command", command)
    monkeypatch.setattr(R, "_download", download)
    return calls, downloads


def test_fresh_install_is_exact_scripts_disabled_and_binding_precedes_only_reviewed_entry(inputs, installer, tmp_path, monkeypatch):
    node, _, _ = inputs
    calls, downloads = installer
    monkeypatch.setenv("NODE_OPTIONS", "--require /unadmitted.js")
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://unadmitted.invalid")
    before_home = R.os.environ.get("HOME")
    out = tmp_path / "fresh"
    result = R.setup(node=node, out=out)
    assert result["status"] == "installed" and result["inputs_unchanged"] and result["cleanup_confirmed"]
    assert len(calls) == 3 and len(downloads) == 1
    assert [step["name"] for step in result["steps"]] == ["resolve-lock", "install-packages", "reviewed-native-entry"]
    for call in calls[:2]:
        assert "--ignore-scripts" in call["command"] and "--fetch-retries=0" in call["command"]
        assert call["command"][:2] == [str(node), str(node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js")]
        assert "--registry=https://registry.npmjs.org" in call["command"]
    assert calls[-1]["command"] == [str(node), str(out / "tools/node_modules" / R.INSTALL_RELATIVE)]
    assert calls[-1]["env"]["HOME"] == before_home
    assert "NODE_OPTIONS" not in calls[-1]["env"] and "NPM_CONFIG_REGISTRY" not in calls[-1]["env"]
    assert calls[-1]["env"]["NPM_CONFIG_USERCONFIG"] == str(out / "npm-user.conf")
    assert result["binding"]["sha256"] == hashlib.sha256(b"inert native binding").hexdigest()
    assert json.loads((out / "setup.json").read_text()) == result
    assert result["attachments"] == result["model_calls"] == result["native_calls"] == result["adapter_compilations"] == 0
    assert R.B.doctor(node, result["modules"])["build_ready"]


@pytest.mark.parametrize("change", ["node", "npm", "archive", "extra", "external-link", "wrong-arch", "disk", "existing"])
def test_untrusted_or_nonfresh_inputs_refuse_before_commands_and_downloads(inputs, installer, tmp_path, monkeypatch, change):
    node, archive, _ = inputs
    out = tmp_path / "out"
    if change == "node":
        node.write_bytes(b"changed")
    elif change == "npm":
        (node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js").write_bytes(b"changed")
    elif change == "archive":
        archive.write_bytes(b"changed")
    elif change == "extra":
        (node.parent.parent / "unadmitted.js").write_bytes(b"extra")
    elif change == "external-link":
        (node.parent.parent / "outside").symlink_to(archive)
    elif change == "wrong-arch":
        monkeypatch.setattr(R.platform, "machine", lambda: "x86_64")
    elif change == "disk":
        monkeypatch.setattr(R.shutil, "disk_usage", lambda _: type("Usage", (), {"free": R.RESERVE_BYTES})())
    else:
        out.mkdir()
    with pytest.raises((ValueError, OSError)):
        R.setup(node=node, out=out)
    assert not installer[0] and not installer[1]
    assert not (out / "setup.json").exists()


@pytest.mark.parametrize("change", ["version", "integrity", "remote", "transitive-remote", "transitive-integrity", "local-link", "duplicate"])
def test_lock_validation_refuses_unreviewed_inputs(tmp_path, change):
    value = copy.deepcopy(lock())
    entry = value["packages"]["node_modules/frida"]
    transitive = value["packages"]["node_modules/example-dependency"]
    if change == "version":
        entry["version"] = "17.99.0"
    elif change == "integrity":
        entry["integrity"] = transitive["integrity"]
    elif change == "remote":
        entry["resolved"] = "https://elsewhere.invalid/frida.tgz"
    elif change == "transitive-remote":
        transitive["resolved"] = "file:/elsewhere.tgz"
    elif change == "transitive-integrity":
        transitive["integrity"] = "sha512-invalid"
    elif change == "local-link":
        transitive["link"] = True
    path = tmp_path / "lock.json"
    raw = json.dumps(value)
    if change == "duplicate":
        raw = raw.replace('"lockfileVersion": 3', '"lockfileVersion": 3, "lockfileVersion": 3')
    path.write_text(raw)
    with pytest.raises(ValueError):
        R._lock(path)


@pytest.mark.parametrize("change", ["script", "compiler", "binding-present", "command", "cleanup", "lock-drift"])
def test_failed_install_retains_receipt_and_stops_before_binding_script(inputs, installer, tmp_path, monkeypatch, change):
    original = R._command
    def run(*args):
        result = original(*args)
        if "ci" in args[0]:
            modules = args[1] / "node_modules"
            if change == "script":
                (modules / R.INSTALL_RELATIVE).write_bytes(b"changed")
            elif change == "compiler":
                (modules / R.B.COMPILER).write_bytes(b"changed")
            elif change == "binding-present":
                (modules / "frida/build/frida_binding.node").write_bytes(b"unadmitted binding")
            elif change == "command":
                result.update(returncode=1, error={"type": "FixtureFailure"})
            elif change == "cleanup":
                result["cleanup_confirmed"] = False
            else:
                path = args[1] / "package-lock.json"
                path.write_bytes(path.read_bytes() + b"\n")
        return result
    monkeypatch.setattr(R, "_command", run)
    out = tmp_path / "out"
    result = R.setup(node=inputs[0], out=out)
    assert result["status"] == "failed" and len(installer[0]) == 2
    assert result["error"] and (out / "setup.json").exists()
    if change == "cleanup":
        assert result["cleanup_confirmed"] is False


@pytest.mark.parametrize("change", ["path", "symlink", "hardlink", "extra", "oversize", "duplicate", "digest"])
def test_binding_archive_never_extracts_unadmitted_roles(tmp_path, monkeypatch, change):
    archive = tmp_path / "binding.tar.gz"
    rows = [("build/frida_binding.node", b"fixture binding")]
    if change == "path":
        rows = [("../frida_binding.node", b"fixture")]
    elif change == "symlink":
        rows = [("build/frida_binding.node", "../outside")]
    elif change == "extra":
        rows.append(("build/other.node", b"unadmitted"))
    elif change == "duplicate":
        rows += rows
    tar(archive, rows)
    if change == "hardlink":
        with tarfile.open(archive, "w:gz") as stream:
            item = tarfile.TarInfo("build/frida_binding.node")
            item.type, item.linkname = tarfile.LNKTYPE, "outside"
            stream.addfile(item)
    monkeypatch.setattr(R, "BINDING_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    if change == "digest":
        monkeypatch.setattr(R, "BINDING_SHA256", "0" * 64)
    elif change == "oversize":
        monkeypatch.setattr(R.B, "MAX_CACHE_FILE_BYTES", 2)
    package = tmp_path / "package"
    package.mkdir()
    with pytest.raises(ValueError):
        R._binding(archive, package)
    assert not (package / "build").exists() and not (tmp_path / "frida_binding.node").exists()


def test_failure_and_final_drift_keep_primary_error(inputs, installer, tmp_path, monkeypatch):
    original = R._command
    def run(*args):
        result = original(*args)
        inputs[0].write_bytes(b"drift")
        return {**result, "returncode": 1, "error": {"type": "OriginalFailure"}}
    monkeypatch.setattr(R, "_command", run)
    result = R.setup(node=inputs[0], out=tmp_path / "out")
    assert result["status"] == "failed" and result["inputs_unchanged"] is False
    assert "resolve-lock" in result["error"]["message"]
    assert "differs" in result["final_check_error"]["message"]
    assert result["steps"][0]["error"]["type"] == "OriginalFailure"


def test_post_script_mutation_prevents_success(inputs, installer, tmp_path, monkeypatch):
    original = R._command
    def run(*args):
        result = original(*args)
        if len(args[0]) == 2:
            (args[1] / "build/frida_binding.node").write_bytes(b"changed native binding")
        return result
    monkeypatch.setattr(R, "_command", run)
    result = R.setup(node=inputs[0], out=tmp_path / "out")
    assert result["status"] == "failed" and "early-exit changed" in result["error"]["message"]


def test_interruption_preserves_owned_cleanup_evidence(inputs, installer, tmp_path, monkeypatch):
    def interrupted(command, cwd, env, folder, timeout):
        R.B._write(folder / "command.json", {"command": command, "returncode": -15,
                    "error": {"type": "KeyboardInterrupt"}, "cleanup_confirmed": True})
        raise KeyboardInterrupt()
    monkeypatch.setattr(R, "_command", interrupted)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        R.setup(node=inputs[0], out=out)
    result = json.loads((out / "setup.json").read_text())
    assert result["status"] == "failed" and result["error"]["type"] == "KeyboardInterrupt"
    assert result["cleanup_confirmed"] is True and len(result["steps"]) == 1
    assert result["steps"][0]["error"]["type"] == "KeyboardInterrupt"


@pytest.mark.parametrize("case", ["valid", "foreign-redirect", "too-many-redirects", "digest", "oversize"])
def test_download_verifies_official_redirect_and_complete_bounded_asset(tmp_path, monkeypatch, case):
    payload, requests = b"inert archive fixture", []
    monkeypatch.setattr(R, "BINDING_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(R, "_space", lambda *args, **kwargs: 4 * 1024**3)
    original = R.httpx.Client
    def handler(request):
        requests.append(str(request.url))
        if case == "foreign-redirect":
            return R.httpx.Response(302, headers={"location": "https://unadmitted.invalid/file"})
        if case == "too-many-redirects":
            return R.httpx.Response(302, headers={"location": "https://github.com/loop"})
        if len(requests) == 1:
            return R.httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/fixture"})
        return R.httpx.Response(200, content=payload)
    def client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        return original(transport=R.httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(R.httpx, "Client", client)
    if case == "oversize":
        monkeypatch.setattr(R, "MAX_ARCHIVE_BYTES", 2)
    elif case == "digest":
        monkeypatch.setattr(R, "BINDING_SHA256", "0" * 64)
    path = tmp_path / "download"
    if case == "valid":
        result = R._download(path, R.time.monotonic() + 10)
        assert result["verified_sha256"] == hashlib.sha256(payload).hexdigest()
        assert result["digest_source"] == R.BINDING_DIGEST_SOURCE and path.read_bytes() == payload
    else:
        with pytest.raises(ValueError):
            R._download(path, R.time.monotonic() + 10)
    assert len(requests) == {"foreign-redirect": 1, "too-many-redirects": 4}.get(case, 2)


def test_disk_reserve_failure_cleans_only_its_owned_process_group(tmp_path, monkeypatch):
    killed, created = [], []
    class Pipe:
        closed = False
        def __init__(self, fd):
            self.fd = fd
        def fileno(self):
            return self.fd
        def close(self):
            self.closed = True
    class Proc:
        pid, code = 123456, None
        stdout, stderr = Pipe(111), Pipe(112)
        def poll(self):
            return self.code
        def wait(self, timeout):
            assert self.code is not None
            return self.code
    proc = Proc()
    class Selector:
        def register(self, *args):
            pass
        def get_map(self):
            return {1: True}
        def close(self):
            pass
    def spawn(command, **kwargs):
        created.append((command, kwargs))
        return proc
    def kill(pid, sig):
        assert pid == proc.pid
        killed.append((pid, sig))
        proc.code = -int(sig)
    def no_space(_):
        raise ValueError("fixture disk reserve reached")
    monkeypatch.setattr(R.subprocess, "Popen", spawn)
    monkeypatch.setattr(R.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(R.B, "_group_exists", lambda pid: pid == proc.pid and proc.code is None)
    monkeypatch.setattr(R.os, "killpg", kill)
    monkeypatch.setattr(R, "_space", no_space)
    result = R._command(["inert-node", "inert-entry"], tmp_path, {"HOME": "/unchanged"}, tmp_path, 10)
    assert result["error"]["message"] == "fixture disk reserve reached"
    assert result["cleanup_confirmed"] and len(killed) == 1
    assert created[0][1]["start_new_session"] is True
    assert proc.stdout.closed and proc.stderr.closed
    assert json.loads((tmp_path / "command.json").read_text()) == result


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
