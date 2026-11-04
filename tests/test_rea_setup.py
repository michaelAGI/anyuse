"""Offline fakes only: never download, unpack or run an installer on the host."""
import hashlib
import json
import pathlib
import re
import sys
import tempfile
from types import SimpleNamespace

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-rea-setup-pkg-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_setup as S


def test_pins_are_exact_versions_with_checksums():
    for name in ("node", "jdk", "ghidra"):
        pin = S.PINS[name]
        assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]), name
        assert pin["url"].startswith("https://") and pin["version"] in pin["url"].replace("%2B", "+")
    assert re.fullmatch(r"\d+\.\d+\.\d+", S.PINS["rea"]["version"])
    assert S.PINS["rea"]["integrity"].startswith("sha512-")


def test_setup_refuses_the_host_without_the_opt_in(monkeypatch, tmp_path):
    def refuse():
        raise PermissionError("REA mapping execution is VM-only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    called = []
    monkeypatch.setattr(S, "fetch", lambda *a, **kw: called.append(a))
    with pytest.raises(PermissionError):
        S.setup(tmp_path / "prefix", on_host=True)
    assert not called and not (tmp_path / "prefix").exists()


def test_cli_setup_guards_before_work(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli
    monkeypatch.setattr(R, "_HOST_ALLOWED", False)
    monkeypatch.setattr(R.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="0\n"))
    result = CliRunner().invoke(rea_cli.rea_commands, ["setup", "--prefix", str(tmp_path / "p")])
    assert result.exit_code != 0 and not (tmp_path / "p").exists()


def test_setup_accepts_allow_host_after_the_subcommand(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli
    monkeypatch.setattr(R, "_HOST_ALLOWED", False)
    seen = {}

    def fake_setup(prefix, *, on_host, say):
        seen.update(prefix=prefix, on_host=on_host)
        return {"env": str(prefix / "env.sh"), "ghidra_ready": True, "doctor": "x"}
    monkeypatch.setattr(S, "setup", fake_setup)
    monkeypatch.setattr(R, "assert_guest", lambda: None if R.host_allowed() else (_ for _ in ()).throw(
        PermissionError("VM-only")))
    result = CliRunner().invoke(rea_cli.rea_commands, ["setup", "--allow-host", "--prefix", str(tmp_path / "p")])
    assert result.exit_code == 0, result.output
    assert seen["on_host"] is True and R.host_allowed()


def test_missing_command_line_tools_stop_with_the_install_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(S.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(S.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(S, "command_line_tools", lambda: False)
    with pytest.raises(ValueError, match="xcode-select --install"):
        S.setup(tmp_path / "prefix", on_host=True)
    with pytest.raises(ValueError, match="softwareupdate"):
        S.setup(tmp_path / "prefix", on_host=False)
    assert not (tmp_path / "prefix").exists()


def _exports(text):
    import shlex
    out = {}
    for line in text.splitlines():
        if line.startswith("export "):
            name, _, value = shlex.split(line)[1].partition("=")
            out[name] = value
    return out


def test_env_script_points_at_the_prefix(tmp_path):
    values = _exports(S.env_script(tmp_path))
    assert values["JAVA_HOME"] == f"{tmp_path}/jdk/Contents/Home"
    assert values["GHIDRA_INSTALL_DIR"] == f"{tmp_path}/deps/ghidra_{S.PINS['ghidra']['version']}_PUBLIC"
    assert values["REA_CLI"] == f"{tmp_path}/tools/node_modules/.bin/rea"
    assert values["PATH"] == f"{tmp_path}/node/bin:$PATH"


def test_env_script_never_expands_a_hostile_path(tmp_path):
    weird = tmp_path / "a b" / "$(touch pwned)" / "`id`" / "it's"
    text = S.env_script(weird)
    assert str(weird) not in text.splitlines()[0]          # the comment carries no path
    values = _exports(text)                                 # single-quoted: the literal path comes back
    assert values["JAVA_HOME"] == f"{weird}/jdk/Contents/Home"
    assert values["REA_CLI"] == f"{weird}/tools/node_modules/.bin/rea"


def test_prefix_with_other_files_is_never_used(tmp_path):
    foreign = tmp_path / "dev"
    (foreign / "node").mkdir(parents=True)
    (foreign / "node" / "keep.txt").write_text("user data")
    with pytest.raises(ValueError, match="already holds other files"):
        S._claim_prefix(foreign)
    assert (foreign / "node" / "keep.txt").read_text() == "user data" and not (foreign / S.MARKER).exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    S._claim_prefix(empty)
    assert (empty / S.MARKER).is_file()
    S._claim_prefix(empty)                                  # a folder this setup created is reusable
    with pytest.raises(ValueError, match="control characters"):
        S._claim_prefix(tmp_path / "bad\nname")


def _ready_prefix(prefix, *, lock_integrity=None, installed="6.7.0"):
    """A prefix whose Node, JDK and Ghidra are in place, so setup goes straight to REA."""
    prefix.mkdir(parents=True)
    (prefix / S.MARKER).write_text("")
    ghidra = f"deps/ghidra_{S.PINS['ghidra']['version']}_PUBLIC"
    for rel in ("node/bin/node", "node/bin/npm", "jdk/Contents/Home/bin/java",
                f"{ghidra}/support/analyzeHeadless", f"{ghidra}/{S.DECOMPILER}"):
        (prefix / rel).parent.mkdir(parents=True, exist_ok=True)
        (prefix / rel).write_text("")
    if installed:
        pkg = prefix / "tools/node_modules/rea-agents/package.json"
        pkg.parent.mkdir(parents=True)
        pkg.write_text(json.dumps({"version": installed}))
    if lock_integrity:
        (prefix / "tools").mkdir(exist_ok=True)
        (prefix / "tools/package-lock.json").write_text(json.dumps(
            {"packages": {"node_modules/rea-agents": {"version": "6.7.0", "integrity": lock_integrity}}}))


def _fake_tools(monkeypatch, commands, *, lock_integrity, doctor=(0, '{"healthy": true}')):
    from types import SimpleNamespace
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(S.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(S.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(S, "command_line_tools", lambda: True)

    def fake_run(cmd, **kw):
        commands.append((cmd, kw))
        name = pathlib.Path(cmd[0]).name
        if name == "node":
            return SimpleNamespace(stdout="v" + S.PINS["node"]["version"], stderr="", returncode=0)
        if name == "java":
            return SimpleNamespace(stdout="", stderr=f'openjdk version "{S.PINS["jdk"]["version"]}"', returncode=0)
        if name == "npm" and "--package-lock-only" in cmd:
            tools = pathlib.Path(kw["cwd"])
            (tools / "package-lock.json").write_text(json.dumps(
                {"packages": {"node_modules/rea-agents": {"version": "6.7.0", "integrity": lock_integrity}}}))
        if name == "npm" and cmd[1] == "ci":
            pkg = pathlib.Path(kw["cwd"]) / "node_modules/rea-agents/package.json"
            pkg.parent.mkdir(parents=True, exist_ok=True)
            pkg.write_text(json.dumps({"version": "6.7.0"}))
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "fetch", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no download expected")))
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=doctor[0], stdout=doctor[1]))


def test_npm_resolves_and_checks_integrity_before_installing(monkeypatch, tmp_path):
    monkeypatch.setenv("npm_config_global", "true")
    commands = []
    _fake_tools(monkeypatch, commands, lock_integrity="sha512-substituted")
    prefix = tmp_path / "p"
    _ready_prefix(prefix, installed="")
    with pytest.raises(ValueError, match="integrity differs"):
        S.setup(prefix, on_host=True)
    npm = [c for c, kw in commands if pathlib.Path(c[0]).name == "npm"]
    assert len(npm) == 1 and "--package-lock-only" in npm[0]         # nothing unpacked, no ci
    for flag in ("--ignore-scripts", "--global=false", "--prefix"):
        assert flag in npm[0]
    env = [kw["env"] for c, kw in commands if pathlib.Path(c[0]).name == "npm"][0]
    assert not any(k.lower().startswith("npm_config_") for k in env)


def test_npm_ci_runs_only_from_a_verified_lockfile(monkeypatch, tmp_path):
    commands = []
    _fake_tools(monkeypatch, commands, lock_integrity=S.PINS["rea"]["integrity"])
    prefix = tmp_path / "p"
    _ready_prefix(prefix, installed="6.6.0")                         # an older REA is replaced
    receipt = S.setup(prefix, on_host=True)
    npm = [c for c, kw in commands if pathlib.Path(c[0]).name == "npm"]
    assert [c[1] for c in npm] == ["install", "ci"] and "--ignore-scripts" in npm[1]
    assert receipt["ghidra_ready"] is True


@pytest.mark.parametrize("doctor", [(1, '{"healthy": true}'), (0, '{"healthy": "true"}'), (0, "[]"), (0, "")])
def test_doctor_must_exit_cleanly_and_report_healthy_true(monkeypatch, tmp_path, doctor):
    commands = []
    _fake_tools(monkeypatch, commands, lock_integrity=S.PINS["rea"]["integrity"], doctor=doctor)
    prefix = tmp_path / "p"
    _ready_prefix(prefix, lock_integrity=S.PINS["rea"]["integrity"])
    assert S.setup(prefix, on_host=True)["ghidra_ready"] is False
    assert not [c for c, kw in commands if pathlib.Path(c[0]).name == "npm"]   # installed and verified: no npm


def test_fetch_keeps_a_matching_file_and_rejects_a_mismatch(monkeypatch, tmp_path):
    good = tmp_path / "a.tar.gz"
    good.write_bytes(b"archive")
    digest = hashlib.sha256(b"archive").hexdigest()
    assert S.fetch("https://example.invalid/a", good, digest) == good   # no download when it matches

    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def raise_for_status(self):
            pass
        def iter_bytes(self, n):
            yield b"tampered"
    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **kw: Stream())
    bad = tmp_path / "b.tar.gz"
    with pytest.raises(ValueError, match="checksum mismatch"):
        S.fetch("https://example.invalid/b", bad, digest)
    assert not bad.exists() and not bad.with_suffix(".gz.part").exists()


def test_fetch_surfaces_network_errors_as_oserror(monkeypatch, tmp_path):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("no route to host")
    monkeypatch.setattr(httpx, "stream", boom)
    dest = tmp_path / "c.tar.gz"
    with pytest.raises(OSError):   # a clean message, not a raw httpx traceback
        S.fetch("https://example.invalid/c", dest, "0" * 64)
    assert not dest.with_suffix(".gz.part").exists()


def test_run_timeout_becomes_a_clean_error(monkeypatch):
    import subprocess

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=kw.get("timeout", 1))
    monkeypatch.setattr(S.subprocess, "run", slow)
    with pytest.raises(ValueError, match="timed out"):
        S._run(["/usr/bin/npm", "install"], timeout=900)


def test_locked_package_tolerates_broken_or_old_lockfiles(tmp_path):
    key = "node_modules/rea-agents"
    missing = tmp_path / "absent.json"
    assert S._locked_package(missing, key) == {}            # unreadable
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    assert S._locked_package(bad, key) == {}                # corrupt
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps({"dependencies": {"rea-agents": {"version": "6.7.0"}}}))
    assert S._locked_package(v1, key) == {}                 # old layout: no "packages"
    no_entry = tmp_path / "no_entry.json"
    no_entry.write_text(json.dumps({"packages": {"": {"name": "tools"}}}))
    assert S._locked_package(no_entry, key) == {}           # install never recorded the entry
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"packages": {key: {"version": "6.7.0", "integrity": "sha512-x"}}}))
    assert S._locked_package(good, key)["integrity"] == "sha512-x"


def test_jdk_of_another_version_is_reinstalled(monkeypatch, tmp_path):
    # A wrong-version JDK already on disk satisfies existence; setup must still re-fetch it.
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(S.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(S.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(S, "command_line_tools", lambda: True)
    prefix = tmp_path / "prefix"
    (prefix / "node" / "bin").mkdir(parents=True)
    (prefix / S.MARKER).write_text("")
    (prefix / "node" / "bin" / "node").write_text("")
    (prefix / "node" / "bin" / "npm").write_text("")
    (prefix / "jdk" / "Contents" / "Home" / "bin").mkdir(parents=True)
    (prefix / "jdk" / "Contents" / "Home" / "bin" / "java").write_text("")
    from types import SimpleNamespace

    def fake_run(cmd, **kw):
        name = pathlib.Path(cmd[0]).name
        if name == "node":   # correct version, so Node is left in place
            return SimpleNamespace(stdout="v" + S.PINS["node"]["version"] + "\n", stderr="", returncode=0)
        if name == "java":   # a different JDK version
            return SimpleNamespace(stdout="", stderr='openjdk version "17.0.1" 2021-10-19\n', returncode=0)
        raise AssertionError("unexpected command " + name)
    monkeypatch.setattr(S, "_run", fake_run)
    fetched = []
    monkeypatch.setattr(S, "fetch", lambda url, dest, sha, **kw: (fetched.append(url), dest)[1])

    class Stop(Exception):
        pass
    # The JDK branch is the first to unpack (Node matched), so stop there deterministically.
    def unpack(archive, target):
        raise Stop(str(target))
    monkeypatch.setattr(S, "_unpack_tar", unpack)
    with pytest.raises(Stop, match="jdk"):
        S.setup(prefix, on_host=True)
    assert fetched == [S.PINS["jdk"]["url"]]


def _fake_decompiler_tree(root):
    src = root / S.DECOMPILER_SOURCES
    src.mkdir(parents=True)
    for name in ("xml", "space", "coreext_a", "funcdata", "ghidra_arch", "ghidraext_b", "ghidra_process"):
        (src / f"{name}.cc").write_text("// source\n")
    (src / "Makefile").write_text(
        "CORE=\txml space $(COREEXT_NAMES)\n"
        "DECCORE=funcdata \\\n\t$(GHIDRAEXT_NAMES)\n"
        "GHIDRA=ghidra_arch ghidra_process\n")
    return src


def test_decompiler_sources_follow_ghidras_makefile(tmp_path):
    src = _fake_decompiler_tree(tmp_path)
    assert S.decompiler_sources(src) == ["xml", "space", "coreext_a", "funcdata", "ghidraext_b",
                                         "ghidra_arch", "ghidra_process"]
    (src / "space.cc").unlink()
    with pytest.raises(ValueError, match="does not match"):
        S.decompiler_sources(src)
    (src / "Makefile").write_text("CORE=xml\n")
    with pytest.raises(ValueError, match="no DECCORE list"):
        S.decompiler_sources(src)


def test_build_decompiler_compiles_every_source_and_links_into_place(monkeypatch, tmp_path):
    _fake_decompiler_tree(tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "-o" in cmd and "-c" not in cmd:
            pathlib.Path(cmd[cmd.index("-o") + 1]).write_text("binary")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(S, "_run", fake_run)
    out = S.build_decompiler(tmp_path, tmp_path / "build", say=lambda m: None)
    assert out == tmp_path / S.DECOMPILER and out.is_file() and out.stat().st_mode & 0o111
    compiles = [c for c in calls if "-c" in c]
    assert len(compiles) == 7 and all(c[:2] == ["/usr/bin/xcrun", "clang++"] for c in calls)


def test_setup_builds_the_decompiler_only_when_it_is_missing(monkeypatch, tmp_path):
    commands, built = [], []
    _fake_tools(monkeypatch, commands, lock_integrity=S.PINS["rea"]["integrity"])
    monkeypatch.setattr(S, "build_decompiler", lambda ghidra, build, say: built.append(ghidra))
    prefix = tmp_path / "p"
    _ready_prefix(prefix, lock_integrity=S.PINS["rea"]["integrity"])
    S.setup(prefix, on_host=True)
    assert built == []
    (prefix / f"deps/ghidra_{S.PINS['ghidra']['version']}_PUBLIC" / S.DECOMPILER).unlink()
    S.setup(prefix, on_host=True)
    assert len(built) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
