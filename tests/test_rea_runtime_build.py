"""Offline builder boundary tests; no actual compiler, Frida, app or network."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from anyuse.execution import rea_runtime_build as B


def test_packaged_fixed_source_matches_the_production_builder_admission_pin():
    assert B.SOURCE_SHA256 == hashlib.sha256(B.SOURCE.read_bytes()).hexdigest()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    node = tmp_path / "node"
    node.write_bytes(b"inert node fixture")
    node.chmod(0o700)
    modules = tmp_path / "cached/node_modules"
    modules.mkdir(parents=True)
    for name, version in B.VERSIONS.items():
        package = modules / name
        package.mkdir()
        (package / "package.json").write_text(json.dumps({"name": name, "version": version}))
        (package / "index.js").write_text("audited cached dependency bytes")
    compiler = modules / B.COMPILER
    compiler.parent.mkdir()
    compiler.write_text("inert fixed compiler fixture")
    (modules / ".bin").mkdir()
    (modules / ".bin/frida-compile").symlink_to("../frida-compile/dist/cli.js")
    source = tmp_path / "source/rea_runtime_agent.js"
    source.parent.mkdir()
    source.write_text("fixed source fixture; not executed")
    monkeypatch.setattr(B, "SOURCE", source)
    monkeypatch.setattr(B, "SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(B, "COMPILER_SHA256", hashlib.sha256(compiler.read_bytes()).hexdigest())
    return node, modules, source


def framed(mapping):
    files = [("/rea-runtime-agent.js", b"rpc.exports={};\n"),
             ("/rea-runtime-agent.js.map", json.dumps(mapping).encode())]
    header = "📦\n" + "".join(str(len(raw)) + " " + name + "\n" for name, raw in files)
    return header.encode() + "✄\n".encode() + "\n✄\n".encode().join(raw for _, raw in files)


@pytest.fixture
def compiler(monkeypatch):
    calls = []
    def run(command, cwd, env, folder, timeout):
        calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout})
        target = Path(command[command.index("-o") + 1])
        target.write_bytes(framed({"version": 3, "sources": ["rea-runtime-agent.js",
            "node_modules/frida-objc-bridge/index.js", "frida-builtins:/node-globals.js"],
            "mappings": "", "names": []}))
        (folder / "compiler.stdout").write_bytes(b"raw compiler stdout\n")
        (folder / "compiler.stderr").write_bytes(b"raw compiler stderr\n")
        return {"returncode": 0, "cleanup_confirmed": True, "error": None,
                "output_overflow": False, "timed_out": False}
    monkeypatch.setattr(B, "_compile", run)
    return calls


def test_offline_build_preserves_full_cache_and_emits_existing_adapter_contract(cache, compiler, tmp_path, monkeypatch):
    node, modules, source = cache
    monkeypatch.setenv("NODE_OPTIONS", "--require /must-not-load.js")
    monkeypatch.setenv("NODE_PATH", "/must-not-resolve")
    before = B.doctor(node, modules)
    assert before["status"] == "ready" and before["compiler_calls"] == 0 and not compiler
    out = tmp_path / "new-build"
    result = B.build(node, modules, out, timeout=17)
    assert result["status"] == "built" and result["compiler_calls"] == len(compiler) == 1
    assert result["cache_unchanged"] and result["source_unchanged"]
    assert B.doctor(node, modules)["inputs"] == before["inputs"]
    call = compiler[0]
    assert call["command"] == [str(node), str(modules / B.COMPILER), str(out / "work/rea-runtime-agent.js"),
                               "-o", str(out / "rea-runtime-agent.bundle.js"), "-c"]
    assert call["timeout"] == 17 and set(call["env"]) == {"PATH", "HOME", "TMPDIR", "NO_COLOR"}
    assert (out / "work/rea-runtime-agent.js").read_bytes() == source.read_bytes()
    manifest = json.loads((out / "rea-runtime-agent.bundle.js.manifest.json").read_text())
    assert manifest["format"] == "anyuse.rea-runtime-adapter/1" and manifest["dependencies"] == B.VERSIONS
    assert manifest["interlock_protocol"] == "anyuse.rea-phased-permit/1"
    assert manifest["source_sha256"] == B.SOURCE_SHA256
    assert B.doctor(node, modules, adapter=out / "rea-runtime-agent.bundle.js")["status"] == "ready"
    assert (out / "compiler.stdout").read_bytes() == b"raw compiler stdout\n"
    assert result["app_launches"] == result["attachments"] == result["model_calls"] == result["downloads"] == 0


@pytest.mark.parametrize("change", ["version", "source", "compiler", "external-symlink", "dangling-symlink"])
def test_invalid_preparation_never_invokes_compiler_or_creates_output(cache, compiler, tmp_path, change):
    node, modules, source = cache
    if change == "version":
        (modules / "frida-objc-bridge/package.json").write_text(json.dumps({"name": "frida-objc-bridge", "version": "9.0.0"}))
    elif change == "source":
        source.write_text("changed fixed source")
    elif change == "compiler":
        (modules / B.COMPILER).write_text("changed compiler")
    else:
        (modules / "unreviewed-link").symlink_to(tmp_path / ("node" if change == "external-symlink" else "missing"))
    assert B.doctor(node, modules)["status"] == "unavailable"
    with pytest.raises((ValueError, OSError)):
        B.build(node, modules, tmp_path / "out")
    assert not compiler and not (tmp_path / "out").exists()


@pytest.mark.parametrize("change", ["dependency", "source", "node"])
def test_input_drift_during_compile_is_failed_without_manifest(cache, compiler, tmp_path, monkeypatch, change):
    node, modules, source = cache
    original = B._compile
    def drift(*args):
        result = original(*args)
        path = {"dependency": modules / "frida-objc-bridge/index.js", "source": source, "node": node}[change]
        path.write_bytes(path.read_bytes() + b" changed")
        return result
    monkeypatch.setattr(B, "_compile", drift)
    out = tmp_path / "out"
    result = B.build(node, modules, out)
    assert result["status"] == "failed" and result["compiler_calls"] == 1
    assert not (out / "rea-runtime-agent.bundle.js.manifest.json").exists()
    assert json.loads((out / "build.json").read_text())["status"] == "failed"


def test_cache_drift_before_dispatch_refuses_without_compiler(cache, compiler, tmp_path, monkeypatch):
    node, modules, _ = cache
    original = B._snapshot
    count = 0
    def snapshot(*args):
        nonlocal count
        count += 1
        if count == 2:
            (modules / "frida-objc-bridge/index.js").write_text("changed before dispatch")
        return original(*args)
    monkeypatch.setattr(B, "_snapshot", snapshot)
    result = B.build(node, modules, tmp_path / "out")
    assert result["status"] == "failed" and result["compiler_calls"] == 0 and not compiler


@pytest.mark.parametrize("failure", ["missing", "truncated", "wrong-source-map", "compiler-error", "output-overflow"])
def test_missing_incomplete_unreviewed_or_failed_compiler_output_cannot_be_published(cache, compiler, tmp_path, monkeypatch, failure):
    node, modules, _ = cache
    original = B._compile
    outside = tmp_path / "outside.js"
    outside.write_text("not an admitted compiler input")
    def failed(command, cwd, env, folder, timeout):
        result = original(command, cwd, env, folder, timeout)
        bundle = folder / "rea-runtime-agent.bundle.js"
        if failure == "missing":
            bundle.unlink()
        elif failure == "truncated":
            bundle.write_bytes(bundle.read_bytes()[:-1])
        elif failure == "wrong-source-map":
            bundle.write_bytes(framed({"version": 3, "sources": ["rea-runtime-agent.js", str(outside)],
                                       "mappings": "", "names": []}))
        elif failure == "compiler-error":
            result.update(returncode=1, error={"type": "FixtureError"})
        else:
            result["output_overflow"] = True
        return result
    monkeypatch.setattr(B, "_compile", failed)
    out = tmp_path / "out"
    result = B.build(node, modules, out)
    assert result["status"] == "failed" and len(compiler) == 1
    assert not (out / "rea-runtime-agent.bundle.js.manifest.json").exists()


def test_existing_output_or_cache_descendant_is_refused_before_compiler(cache, compiler, tmp_path):
    node, modules, _ = cache
    for out in (tmp_path, modules / "out"):
        with pytest.raises(ValueError, match="fresh output"):
            B.build(node, modules, out)
    assert not compiler and not (modules / "out").exists()


def test_doctor_rejects_tampered_adapter_manifest_without_compiler(cache, compiler, tmp_path):
    node, modules, _ = cache
    out = tmp_path / "out"
    assert B.build(node, modules, out)["status"] == "built"
    path = out / "rea-runtime-agent.bundle.js.manifest.json"
    value = json.loads(path.read_text()); value["source_sha256"] = "0" * 64
    path.write_text(json.dumps(value))
    result = B.doctor(node, modules, out / "rea-runtime-agent.bundle.js")
    assert result["status"] == "unavailable" and result["compiler_calls"] == 0 and len(compiler) == 1


def test_cli_build_and_doctor_are_offline_and_keep_existing_runtime_command(cache, compiler, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli as C
    node, modules, _ = cache
    monkeypatch.setattr(C.R, "assert_guest", lambda: (_ for _ in ()).throw(AssertionError("no target/guest work")))
    runner = CliRunner()
    arguments = ["--node", str(node), "--modules", str(modules)]
    checked = runner.invoke(C.rea_commands, ["runtime-doctor", *arguments])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["build_ready"] is True and not compiler
    out = tmp_path / "cli-build"
    built = runner.invoke(C.rea_commands, ["runtime-build", *arguments, "--out", str(out), "--timeout", "19"])
    assert built.exit_code == 0, built.output
    assert json.loads(built.output)["status"] == "built" and len(compiler) == 1
    assert compiler[0]["timeout"] == 19
    checked = runner.invoke(C.rea_commands, ["runtime-doctor", *arguments,
                            "--adapter", str(out / "rea-runtime-agent.bundle.js")])
    assert checked.exit_code == 0 and json.loads(checked.output)["adapter"]["build"]["interlock_protocol"] == B.PROTOCOL
    # Registration did not replace the existing target invocation surface.
    command = C.rea_commands.commands["runtime"]
    assert command.callback.__name__ == "runtime" and [p.name for p in command.params] == ["bundle", "request", "out", "timeout"]


def test_cli_invalid_prep_and_failed_build_exit_nonzero_without_publishing(cache, compiler, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli as C
    node, modules, _ = cache
    runner = CliRunner()
    arguments = ["--node", str(node), "--modules", str(modules)]
    manifest = modules / "frida-objc-bridge/package.json"
    original = manifest.read_bytes()
    manifest.write_text(json.dumps({"name": "frida-objc-bridge", "version": "wrong"}))
    checked = runner.invoke(C.rea_commands, ["runtime-doctor", *arguments])
    assert checked.exit_code != 0 and '"build_ready": false' in checked.output and not compiler
    out = tmp_path / "bad-cli-build"
    built = runner.invoke(C.rea_commands, ["runtime-build", *arguments, "--out", str(out)])
    assert built.exit_code != 0 and not compiler and not out.exists()
    manifest.write_bytes(original)
    original_compile = B._compile
    def missing(*args):
        result = original_compile(*args)
        (args[3] / "rea-runtime-agent.bundle.js").unlink()
        return result
    monkeypatch.setattr(B, "_compile", missing)
    out = tmp_path / "failed-cli-build"
    built = runner.invoke(C.rea_commands, ["runtime-build", *arguments, "--out", str(out)])
    assert built.exit_code != 0 and json.loads((out / "build.json").read_text())["status"] == "failed"
    assert not (out / "rea-runtime-agent.bundle.js.manifest.json").exists()


@pytest.mark.parametrize("size,accepted", [(1025, True), (3072, True), (4096, True), (4097, False)])
def test_large_cached_files_are_fully_stream_pinned_with_a_separate_finite_limit(
        cache, compiler, tmp_path, monkeypatch, size, accepted):
    node, modules, _ = cache
    # Scale the actual64MiB/128MiB issue down; do not allocate a huge fixture.
    monkeypatch.setattr(B, "MAX_FILE_BYTES", 1024)
    monkeypatch.setattr(B, "MAX_CACHE_FILE_BYTES", 4096)
    payload = bytes(i % 251 for i in range(size))
    entry = modules / "frida-objc-bridge/large-cached-entry.node"
    entry.write_bytes(payload)
    original_read = B._read
    def no_whole_file_read(path, *args, **kwargs):
        assert path != entry, "large cached file must use streaming pinning"
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(B, "_read", no_whole_file_read)
    result = B.doctor(node, modules)
    if accepted:
        assert result["build_ready"] is True
        pin = result["inputs"]["modules"]["entries"][entry.relative_to(modules).as_posix()]
        assert pin["bytes"] == size and pin["sha256"] == hashlib.sha256(payload).hexdigest()
        changed = payload[:-1] + bytes([(payload[-1] + 1) % 256])
        entry.write_bytes(changed)
        after = B.doctor(node, modules)
        pin2 = after["inputs"]["modules"]["entries"][entry.relative_to(modules).as_posix()]
        assert pin2["sha256"] == hashlib.sha256(changed).hexdigest() != pin["sha256"]
    else:
        assert result["build_ready"] is False and "cached input" in result["error"]["message"]
        with pytest.raises(ValueError):
            B.build(node, modules, tmp_path / "out")
        assert not (tmp_path / "out").exists()
    assert not compiler


def test_larger_per_file_support_does_not_relax_aggregate_cache_budget(cache, compiler, tmp_path, monkeypatch):
    node, modules, _ = cache
    baseline = B.doctor(node, modules)
    actual = baseline["inputs"]["modules"]["bytes"]
    monkeypatch.setattr(B, "MAX_TREE_BYTES", actual - 1)
    result = B.doctor(node, modules)
    assert result["build_ready"] is False and "tree exceeds byte bound" in result["error"]["message"]
    with pytest.raises(ValueError):
        B.build(node, modules, tmp_path / "out")
    assert not compiler and not (tmp_path / "out").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
