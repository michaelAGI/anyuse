"""Offline static-cache regressions; no REA, Ghidra or app is executed."""
import fcntl
import json
from pathlib import Path

import pytest

from anyuse.execution import rea_analysis_cache as C
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_ghidra_artifacts as G
from test_rea_mapping import app
from test_rea_runtime_bridge import _inert_macho


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    home = tmp_path / "ghidra"
    (home / "support").mkdir(parents=True)
    (home / "Ghidra").mkdir()
    (home / "support/analyzeHeadless").write_text("inert launcher")
    (home / "Ghidra/application.properties").write_text("application.version=12.1.4\n")
    monkeypatch.setenv("GHIDRA_INSTALL_DIR", str(home))
    rea = tmp_path / "rea"
    rea.write_text("inert REA")
    rea.chmod(0o700)
    calls = []

    def run(argv, output, error, **kwargs):
        calls.append(argv[1])
        output.write_text("rea 6.7.0\n" if argv[1] == "--version" else json.dumps({"operation": argv[1], "evidence_only": True}))
        error.write_text("")
        return {"returncode": 0, "timed_out": False, "output": output.name,
                "sha256": R._file_hash(output), "stderr": error.name}

    monkeypatch.setattr(R, "_bounded_run", run)
    return rea, home, calls, run


def test_cache_is_inert_until_write_and_rejects_corruption_symlinks_and_locked_io(tmp_path):
    store = C.StaticAnalysisCache(tmp_path / "cache")
    key = C.key({"request": 1})
    assert store.get(key) is None and not store.path.exists()
    assert store.put(key, {"data": "observation"})
    assert store.get(key) == {"data": "observation"}
    entry = store.path / (key + ".json")
    value = json.loads(entry.read_text())
    value["payload"]["data"] = "changed"
    entry.write_text(json.dumps(value))
    assert store.get(key) is None
    assert store.put(key, {"data": "observation"})
    assert entry.stat().st_mode & 0o777 == 0o600
    with (store.path / ".lock").open("r+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert store.get(key) is None
        assert not store.put(key, {"data": "competing producer"})
    assert store.get(key) == {"data": "observation"}
    external = tmp_path / "external"
    external.write_text("preserve")
    entry.unlink()
    entry.symlink_to(external)
    assert store.get(key) is None
    assert store.put(key, {"data": "replacement"})
    assert external.read_text() == "preserve" and not entry.is_symlink()
    alias = tmp_path / "alias"
    alias.symlink_to(store.path, target_is_directory=True)
    assert C.StaticAnalysisCache(alias).get(key) is None
    assert not C.StaticAnalysisCache(alias).put(key, {})


def test_cache_prunes_entries_bytes_and_interrupted_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_ENTRIES", 2)
    monkeypatch.setattr(C, "MAX_TOTAL_BYTES", 1000)
    store = C.StaticAnalysisCache(tmp_path / "cache")
    first, second, third = [C.key(i) for i in range(3)]
    assert store.put(first, "x" * 100)
    (store.path / ".write-interrupted").write_text("unfinished")
    assert store.put(second, "y" * 100)
    assert store.put(third, "z" * 100)
    assert store.get(first) is None and store.get(second) is not None
    assert not (store.path / ".write-interrupted").exists()
    assert len(list(store.path.glob("*.json"))) == 2
    assert sum(p.stat().st_size for p in store.path.glob("*.json")) <= C.MAX_TOTAL_BYTES
    assert not store.put(first, "x" * 1100)
    monkeypatch.setattr(C, "MAX_TOTAL_BYTES", 600)
    assert store.put(first, "x" * 300)
    assert store.put(second, "y" * 300)
    assert store.get(first) is None
    assert sum(p.stat().st_size for p in store.path.glob("*.json")) <= C.MAX_TOTAL_BYTES


def test_cli_reuses_only_matching_binary_query_toolchain_and_records_provenance(app, tmp_path, tools):
    rea, home, calls, _ = tools

    def investigate(name, **kwargs):
        return R.investigate(str(app), str(rea), str(tmp_path / name), **kwargs)

    first = investigate("first", query="save")
    assert calls == ["--version", "analyze", "search"]
    second = investigate("second", query="save")
    assert calls == ["--version", "analyze", "search", "--version"]
    assert all(r["cache"]["hit"] and r["cache"]["evidence_only"] for r in second["runs"])
    assert second["status"] == "evidence_collected" and "proof" not in second
    assert (tmp_path / "first/evidence-0.json").read_bytes() == (tmp_path / "second/evidence-0.json").read_bytes()
    investigate("different-query", query="open")
    assert calls[-2:] == ["--version", "search"]
    (app / "Contents/MacOS/fixture").write_bytes(b"different inert binary")
    investigate("different-binary", query="save")
    assert calls[-3:] == ["--version", "analyze", "search"]
    rea.write_text("changed inert REA")
    investigate("different-rea")
    assert calls[-2:] == ["--version", "analyze"]
    (home / "support/analyzeHeadless").write_text("changed inert launcher")
    investigate("different-ghidra")
    assert calls[-2:] == ["--version", "analyze"]
    (home / "Ghidra/application.properties").write_text("application.version=unreviewed\n")
    investigate("unpinned-toolchain")
    assert calls[-2:] == ["--version", "analyze"]
    assert first["target"] == second["target"]


def test_cli_cache_preserves_exact_utf8_evidence_bytes(app, tmp_path, tools, monkeypatch):
    rea, _, calls, run = tools
    original = b'{\r\n  "observation": "exact artifact"\r\n}\r\n'

    def crlf(argv, output, error, **kwargs):
        result = run(argv, output, error, **kwargs)
        if argv[1] != "--version":
            output.write_bytes(original)
            result["sha256"] = R._file_hash(output)
        return result

    monkeypatch.setattr(R, "_bounded_run", crlf)
    R.investigate(str(app), str(rea), str(tmp_path / "first"))
    R.investigate(str(app), str(rea), str(tmp_path / "second"))
    assert calls == ["--version", "analyze", "--version"]
    assert (tmp_path / "second/evidence-0.json").read_bytes() == original


@pytest.mark.parametrize("failure", ["exit", "timeout", "drift"])
def test_cli_failures_and_changed_originals_never_enter_cache(app, tmp_path, tools, monkeypatch, failure):
    rea, _, calls, run = tools

    def fail(argv, output, error, **kwargs):
        result = run(argv, output, error, **kwargs)
        if argv[1] != "--version":
            if failure == "exit":
                result["returncode"] = 1
            elif failure == "timeout":
                result["timed_out"] = True
            else:
                (app / "Contents/Resources/data").write_text("drift during analysis")
        return result

    monkeypatch.setattr(R, "_bounded_run", fail)
    with pytest.raises(ValueError):
        R.investigate(str(app), str(rea), str(tmp_path / "failed"))
    assert not list((tmp_path / "data/rea/ghidra_cache").glob("*.json"))
    assert R.load_json(tmp_path / "failed/analysis.json")["status"] == "incomplete"


def test_fixed_ghidra_reuses_raw_static_data_and_rechecks_original_bytes(tmp_path, tools, monkeypatch):
    image = tmp_path / "Native.dylib"
    image.write_bytes(_inert_macho())
    calls = []

    def run(argv, folder, deadline):
        calls.append(argv)
        observed = {"ghidra": "12.1.4", "language": "AARCH64:LE:64:AppleSilicon", "big_endian": False,
                    "image_base": "0x0", "sites": [], "records": [], "data_ranges": []}
        for offset in argv[-3].split(","):
            observed["sites"].append({"image_offset": offset, "instructions": [], "body_imported_ranges": [
                {"image_offset": offset, "bytes": 4, "imported_hex": image.read_bytes()[int(offset, 16):int(offset, 16) + 4].hex()}]})
        R.write_json(folder / "ghidra-observation.json", observed)

    monkeypatch.setattr(G, "_run", run)
    request = {"kind": "ghidra_code", "relative": image.name, "entry_offsets": ["0x1000"], "record_offsets": []}
    first = G.build(tmp_path, request, tmp_path / "first")
    second = G.build(tmp_path, request, tmp_path / "second")
    assert len(calls) == 1 and first["cache"]["hit"] is False and second["cache"]["hit"] is True
    assert second["proof_created"] is False and second["app_execution"] is False
    assert R.load_json(tmp_path / "second/evidence.json")["image"]["sha256"] == R._file_hash(image)
    assert not (tmp_path / "second/original-image.dylib").exists()
    store = C.StaticAnalysisCache()
    altered = store.get(second["cache"]["key"])
    altered["sites"][0]["body_imported_ranges"][0]["imported_hex"] = "ffffffff"
    store.put(second["cache"]["key"], altered)
    with pytest.raises(ValueError, match="instruction bytes differ"):
        G.build(tmp_path, request, tmp_path / "invalid-cache")
    request = {**request, "entry_offsets": ["0x1004"]}
    third = G.build(tmp_path, request, tmp_path / "third")
    assert len(calls) == 2 and third["cache"]["hit"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
