"""Inert native-runner fixtures for symbol evidence; run only in the guest suite."""
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile

import pytest

_HERE = Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-rea-symbols-pkg-")
(Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_symbols as S

OUTPUT = ("0000000100001234 (__TEXT,__text) non-external -[CanvasController setText:]\n"
          "0000000100002345 (__DATA,__objc_data) external _OBJC_CLASS_$_CanvasController\n"
          "                 (undefined) external _objc_msgSend (from libobjc)\n"
          "unrecognized Canvas diagnostic\n").encode()


def tool_binary(cpu=0x0100000c):
    return (b"\xcf\xfa\xed\xfe" + struct.pack("<7I", cpu, 0, 2, 1, 24, 0, 0) +
            struct.pack("<IIQQ", 0x80000028, 24, 56, 0) + b"fixture!")


@pytest.fixture
def symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(S.platform, "machine", lambda: "arm64")
    tool = tmp_path / "toolchain/llvm-nm"
    tool.parent.mkdir()
    tool.write_bytes(tool_binary())
    tool.chmod(0o700)
    monkeypatch.setenv(S.NM_SETTING, str(tool))
    app = tmp_path / "Canvas.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.test.Canvas",
                                                        "CFBundleExecutable": "Canvas", "CFBundleVersion": "1"}))
    executable = contents / "MacOS/Canvas"
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<II", 0x0100000c, 0) + b"\0" * 20)
    state = {"calls": [], "stdout": OUTPUT, "stderr": b"", "returncode": 0, "timed_out": False,
             "change_app": False, "change_native": False, "change_tool": False, "exception": None, "tool": tool}
    def runner(argv, output, error, *, timeout, cwd):
        state["calls"].append({"argv": argv, "timeout": timeout, "cwd": cwd})
        output.write_bytes(state["stdout"])
        error.write_bytes(state["stderr"])
        if state["change_app"]:
            (contents / "changed.txt").write_text("same version, different bytes")
        if state["change_native"]:
            native = Path(argv[-1])
            native.chmod(0o600)
            native.write_bytes(b"changed binary")
        if state["change_tool"]:
            tool.write_bytes(tool.read_bytes() + b"changed")
        if state["exception"]:
            raise state["exception"]
        return {"returncode": state["returncode"], "timed_out": state["timed_out"],
                "elapsed_s": .001, "output": output.name, "sha256": R._file_hash(output), "stderr": error.name}
    monkeypatch.setattr(R, "_bounded_run", runner)
    return app, executable, state, tmp_path / "symbols"


def test_fixed_nm_reads_private_copy_and_binds_native_and_raw_evidence(symbols):
    app, executable, state, out = symbols
    result = S.collect(str(app), str(out), query="CANVAS")
    assert result["status"] == "evidence_collected"
    assert state["calls"] == [{"argv": [str(state["tool"]), "-m", str(out / "native-input.macho")],
                                "timeout": 15, "cwd": out}]
    assert (out / "native-input.macho").read_bytes() == executable.read_bytes()
    assert result["target"] == R.fingerprint(app)
    assert result["native_input"]["source_sha256"] == result["native_input"]["sha256"] == R._file_hash(executable)
    assert result["raw_output"]["sha256"] == R._file_hash(out / "nm.txt")
    assert result["stderr"]["sha256"] == R._file_hash(out / "nm.stderr")
    assert result["rows"][0]["address"] == "0x0000000100001234"
    assert result["rows"][0]["symbol"] == "-[CanvasController setText:]"
    assert result["rows"][1]["section"] == "__DATA,__objc_data"
    assert result["rows"][2] == {"line": 4, "raw": "unrecognized Canvas diagnostic", "parsed": False}
    assert result["counts"]["matched_lines"] == result["counts"]["returned_rows"] == 3
    assert result["truncated"] is False
    assert result["tool_invocations"] == 1 and result["tool_unchanged"] is True
    assert result["tool"]["file"] == str(state["tool"]) and result["tool"]["sha256"] == R._file_hash(state["tool"])
    assert result["evidence_only"] is True and result["callable_api"] is False
    assert result["proof_created"] is False and result["actuated"] is False and result["model_calls"] == 0
    assert json.loads((out / "symbols.json").read_text()) == result
    assert not (out / "proof.json").exists()


def test_undefined_symbols_have_no_address_and_regex_shell_text_is_literal(symbols):
    app, _, state, out = symbols
    result = S.collect(str(app), str(out), query="OBJc_msg")
    assert len(result["rows"]) == 1 and result["rows"][0]["address"] is None
    assert result["rows"][0]["section"] == "undefined"
    query = "Canvas|$(touch /tmp/never-execute-this)"
    result = S.collect(str(app), str(out.parent / "literal"), query=query)
    assert result["rows"] == []
    assert all(query not in call["argv"] for call in state["calls"])


def test_universal_binary_uses_selected_slice_provenance(symbols, monkeypatch):
    app, executable, _, out = symbols
    thin = executable.read_bytes()
    offset = 4096
    executable.write_bytes(struct.pack(">II", 0xcafebabe, 1) + struct.pack(">IIIII", 0x0100000c, 0, offset, len(thin), 12)
                           + b"\0" * (offset - 28) + thin)
    monkeypatch.setattr(R.platform, "machine", lambda: "arm64")
    result = S.collect(str(app), str(out), query="setText:")
    assert result["status"] == "evidence_collected"
    assert result["native_input"]["kind"] == "universal_slice"
    assert result["native_input"]["offset"] == offset
    assert (out / "native-input.macho").read_bytes() == thin
    assert result["native_input"]["source_sha256"] == R._file_hash(executable)
    assert result["native_input"]["sha256"] != result["native_input"]["source_sha256"]


def test_rows_are_bounded_but_scanned_matches_are_counted(symbols):
    app, _, state, out = symbols
    state["stdout"] = b"matched raw symbol\n" * 140
    result = S.collect(str(app), str(out), query="matched", limit=128, timeout=30)
    assert len(result["rows"]) == 128 and result["counts"]["matched_lines"] == 140
    assert result["truncated"] is True and result["truncation"]["rows"] is True
    assert state["calls"][0]["timeout"] == 30


def test_scan_budget_and_oversized_lines_cannot_create_false_row_fragments(symbols, monkeypatch):
    app, _, state, out = symbols
    monkeypatch.setattr(S, "MAX_LINE_BYTES", 32)
    monkeypatch.setattr(S, "MAX_SCAN_BYTES", 1000)
    state["stdout"] = b"prefix " + b"x" * 110 + b" matched\nvalid matched row\n"
    result = S.collect(str(app), str(out), query="matched")
    assert [row["raw"] for row in result["rows"]] == ["valid matched row"]
    assert result["counts"]["oversized_lines"] == 1
    assert result["truncation"]["oversized_lines"] is True
    monkeypatch.setattr(S, "MAX_SCAN_BYTES", 20)
    state["stdout"] = b"visible matched\ntruncated matched trailing line\n"
    result = S.collect(str(app), str(out.parent / "scan-cap"), query="matched")
    assert [row["raw"] for row in result["rows"]] == ["visible matched"]
    assert result["counts"]["scanned_bytes"] == 20 and result["truncation"]["scan"] is True


@pytest.mark.parametrize("failure", ["timeout", "exit", "runner", "app", "copy", "tool"])
def test_failures_preserve_partial_output_hashes_and_never_claim_complete_evidence(symbols, failure):
    app, _, state, out = symbols
    state["stderr"] = b"retained diagnostic"
    if failure == "timeout":
        state["timed_out"] = True
    elif failure == "exit":
        state["returncode"] = 1
    elif failure == "runner":
        state["exception"] = OSError("runner unavailable")
    elif failure == "tool":
        state["change_tool"] = True
    else:
        state["change_app" if failure == "app" else "change_native"] = True
    result = S.collect(str(app), str(out), query="Canvas")
    assert result["status"] == "incomplete" and result["error"]
    assert (out / "nm.txt").read_bytes() == OUTPUT and (out / "nm.stderr").read_bytes() == b"retained diagnostic"
    assert result["raw_output"]["sha256"] == R._file_hash(out / "nm.txt")
    assert result["stderr"]["sha256"] == R._file_hash(out / "nm.stderr")
    assert json.loads((out / "symbols.json").read_text()) == result
    assert result["proof_created"] is False and len(state["calls"]) == 1


def test_non_macho_fails_before_nm_with_incomplete_receipt(symbols):
    app, executable, state, out = symbols
    executable.write_bytes(b"a script or other non-native app executable" * 2)
    result = S.collect(str(app), str(out))
    assert result["status"] == "incomplete" and "Mach-O" in result["error"]["message"]
    assert not state["calls"] and (out / "symbols.json").exists()


@pytest.mark.parametrize("kwargs", [{"query": "x" * 257}, {"query": "line\nbreak"}, {"query": "nul\0byte"},
                                    {"query": []}, {"limit": 129}, {"limit": True}, {"timeout": 31}, {"timeout": True}])
def test_argument_limits_refuse_before_output_or_native_tool(symbols, kwargs):
    app, _, state, out = symbols
    with pytest.raises(ValueError):
        S.collect(str(app), str(out), **kwargs)
    assert not state["calls"] and not out.exists()


def test_output_cannot_overwrite_or_enter_app_bundle(symbols):
    app, _, state, out = symbols
    with pytest.raises(ValueError, match="outside"):
        S.collect(str(app), str(app / "Contents/symbol-evidence"))
    out.mkdir()
    with pytest.raises(FileExistsError):
        S.collect(str(app), str(out))
    assert not state["calls"]


def test_vm_guard_precedes_paths_outputs_and_native_runner(symbols, monkeypatch):
    _, _, state, out = symbols
    def refuse():
        raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    with pytest.raises(PermissionError, match="VM only"):
        S.collect("missing.app", str(out), query=[])
    assert not state["calls"] and not out.exists()


def test_unavailable_tool_is_retained_before_private_copy_fingerprint_or_spawn(symbols, monkeypatch):
    app, _, state, out = symbols
    monkeypatch.delenv(S.NM_SETTING)
    monkeypatch.setattr(S, "DEFAULT_NM_PATHS", ())
    def forbidden(*args):
        raise AssertionError("unavailable symbol tooling must stop before app inspection/copy")
    monkeypatch.setattr(S, "_native_copy", forbidden)
    monkeypatch.setattr(R, "fingerprint", forbidden)
    readiness = S.tool_readiness()
    assert readiness["status"] == "unavailable" and readiness["tool"] is None and readiness["tool_invocations"] == 0
    result = S.collect(app, out)
    assert result["status"] == "unavailable" and result["error"] and result["run"] is None
    assert result["tool_invocations"] == 0 and result["native_input"] is None and result["target"] is None
    assert not state["calls"] and json.loads((out / "symbols.json").read_text()) == result


@pytest.mark.parametrize("bad", ["/usr/bin/nm", "/usr/bin/xcrun", "/usr/bin/llvm-nm", "relative/nm"])
def test_explicit_shims_and_relative_tools_never_fall_back_to_configured_tool(symbols, bad):
    app, _, state, out = symbols
    ready = S.tool_readiness(bad)
    assert ready["status"] == "unavailable" and ready["tool"] is None and ready["tool_invocations"] == 0
    result = S.collect(app, out, nm=bad)
    assert result["status"] == "unavailable" and result["run"] is None and not state["calls"]


@pytest.mark.parametrize("kind", ["script", "noexec", "oversized", "other_cpu", "wrong_filetype", "truncated"])
def test_invalid_real_tool_metadata_refuses_without_probe(symbols, monkeypatch, kind):
    app, _, state, out = symbols
    tool = state["tool"]
    if kind == "script":
        tool.write_bytes(b"#!/bin/sh\n/usr/bin/xcrun nm\n")
    elif kind == "noexec":
        tool.chmod(0o600)
    elif kind == "oversized":
        monkeypatch.setattr(S, "MAX_TOOL_BYTES", 10)
    elif kind == "other_cpu":
        tool.write_bytes(tool_binary(0x01000007))
    elif kind == "wrong_filetype":
        data = bytearray(tool_binary()); struct.pack_into("<I", data, 12, 6); tool.write_bytes(data)
    else:
        tool.write_bytes(tool_binary()[:40])
    assert S.tool_readiness()["status"] == "unavailable"
    result = S.collect(app, out)
    assert result["status"] == "unavailable" and result["run"] is None and not state["calls"]


def test_readiness_accepts_real_universal_native_slice_and_resolves_nm_alias(symbols, tmp_path):
    _, _, state, _ = symbols
    x86, arm = tool_binary(0x01000007), tool_binary()
    data = (struct.pack(">II", 0xcafebabe, 2) + struct.pack(">IIIII", 0x01000007, 0, 64, len(x86), 4) +
            struct.pack(">IIIII", 0x0100000c, 0, 128, len(arm), 4))
    state["tool"].write_bytes(data + b"\0" * (64 - len(data)) + x86 + arm)
    alias = tmp_path / "toolchain/nm"
    alias.symlink_to("llvm-nm")
    ready = S.tool_readiness(alias)
    assert ready["status"] == "ready" and ready["tool"]["file"] == str(state["tool"])
    assert ready["tool"]["architecture"]["container"] == "universal"
    assert ready["tool"]["architecture"]["offset"] == 128 and ready["tool"]["architecture"]["cpu_type"] == 0x0100000c
    assert not state["calls"] and ready["tool_invocations"] == 0


def test_tool_configuration_precedence_and_persistent_setting(symbols, tmp_path, monkeypatch):
    from anyuse import settings
    _, _, state, _ = symbols
    tool = state["tool"]
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "settings-data"))
    settings.set_value(S.NM_SETTING, str(tool))
    monkeypatch.delenv(S.NM_SETTING)
    ready = S.tool_readiness()
    assert ready["status"] == "ready" and ready["selection"] == "setting"
    monkeypatch.setenv(S.NM_SETTING, "/usr/bin/nm")
    assert S.tool_readiness()["status"] == "unavailable"
    assert S.tool_readiness(tool)["status"] == "ready"
    settings.reset_value(S.NM_SETTING)
    monkeypatch.delenv(S.NM_SETTING)
    monkeypatch.setattr(S, "DEFAULT_NM_PATHS", (str(tool),))
    assert S.tool_readiness()["selection"] == "fixed_existing_toolchain_paths"
    assert not state["calls"]


def test_tool_drift_before_dispatch_stops_with_zero_invocations(symbols, monkeypatch):
    app, _, state, out = symbols
    original = S._native_copy
    def changed(*args):
        result = original(*args)
        state["tool"].write_bytes(state["tool"].read_bytes() + b"drift")
        return result
    monkeypatch.setattr(S, "_native_copy", changed)
    result = S.collect(app, out)
    assert result["status"] == "incomplete" and "before dispatch" in result["error"]["message"]
    assert result["run"] is None and result["tool_invocations"] == 0 and not state["calls"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
