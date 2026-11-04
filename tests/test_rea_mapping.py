"""Offline fakes only: never execute REA, an app, a VM, or an installer on the host."""
import copy
import json
import pathlib
import plistlib
import struct
import sys
import tempfile
from types import SimpleNamespace

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-rea-pkg-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution import rea_mapping as R
from anyuse.execution import surface_discovery as S


@pytest.fixture
def app(tmp_path):
    bundle = tmp_path / "Fixture.app"
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "test.rea.fixture", "CFBundleExecutable": "fixture",
        "CFBundleShortVersionString": "1.0", "CFBundleVersion": "7"}))
    (contents / "MacOS" / "fixture").write_bytes(b"inert fixture; not an executable")
    (contents / "Resources" / "data").write_text("resource version one")
    return bundle


@pytest.fixture
def candidate(app):
    return {"format": R.CANDIDATE, "id": "save_text", "label": "Save text",
            "target": R.fingerprint(app), "effect": "doc_save", "item_kind": "document", "mode": "replace",
            "params": [{"name": "output", "type": "path", "source": "path"},
                       {"name": "text", "type": "string", "source": "text"}],
            "argv": ["Contents/MacOS/fixture", "--save", "{output}", "--text", "{text}"],
            "observer": {"kind": "file", "path_param": "output"},
            "evidence": [{"file": "evidence-0.json", "sha256": "a" * 64, "pointer": "/observations/1"}]}


def cases():
    return [{"inputs": {"output": "$OUTPUT", "text": "First\nÄpfel"}, "before": None},
            {"inputs": {"output": "$OUTPUT", "text": "Second"}, "before": "old content"},
            {"inputs": {"output": "$OUTPUT", "text": "Third"}, "before": "old content"}]


def fake_actor(c, root, inputs, folder, timeout):
    path = pathlib.Path(inputs["output"])
    prior = path.read_text() if path.exists() and c.get("mode") == "append" else ""
    path.write_text(prior + inputs["text"])
    return {"returncode": 0, "timed_out": False}


@pytest.fixture
def guest(monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(R, "_execute", fake_actor)


def test_guest_detection_cannot_be_enabled_by_environment(monkeypatch):
    monkeypatch.setenv("ANYUSE_REA_VM", "1")
    monkeypatch.setattr(R.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="0\n"))
    with pytest.raises(PermissionError, match="refusing the host"):
        R.assert_guest()
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="1\n"))
    R.assert_guest()


def test_allow_host_is_a_per_process_opt_in(monkeypatch, capsys):
    monkeypatch.setattr(R, "_HOST_ALLOWED", False)
    monkeypatch.setattr(R, "_HOST_WARNED", False)
    monkeypatch.setattr(R.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="0\n"))
    with pytest.raises(PermissionError, match="--allow-host"):
        R.assert_guest()
    R.allow_host()
    R.assert_guest()
    R.assert_guest()
    err = capsys.readouterr().err
    assert err.count("--allow-host: running on this Mac") == 1


def test_cli_allow_host_flag_sets_the_opt_in(monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli
    monkeypatch.setattr(R, "_HOST_ALLOWED", False)
    result = CliRunner().invoke(rea_cli.rea_commands, ["--help"])
    assert result.exit_code == 0 and "--allow-host" in result.output
    assert not R.host_allowed()
    CliRunner().invoke(rea_cli.rea_commands, ["--allow-host", "describe", "--help"])
    assert R.host_allowed()


def test_all_execution_entrypoints_guard_before_work(monkeypatch, candidate, app, tmp_path):
    def refuse():
        raise PermissionError("VM-only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    for call in [lambda: R.investigate(str(app), "/missing/rea", str(tmp_path / "analysis")),
                 lambda: R.verify(candidate, cases(), str(app), str(tmp_path / "proof")),
                 lambda: R.replay(candidate, {}, {}, str(app), str(tmp_path / "replay"))]:
        with pytest.raises(PermissionError, match="VM-only"):
            call()
    assert not (tmp_path / "analysis").exists() and not (tmp_path / "proof").exists()


def test_resource_and_mode_changes_invalidate_same_version(app):
    before = R.fingerprint(app)
    resource = app / "Contents/Resources/data"
    resource.write_text("changed without version bump")
    after = R.fingerprint(app)
    assert before["version"] == after["version"] and before["sha256"] != after["sha256"]
    resource.chmod(0o700)
    assert R.fingerprint(app)["sha256"] != after["sha256"]


def test_bundle_symlink_cannot_escape(app, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("not app content")
    (app / "Contents/Resources/link").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        R.fingerprint(app)


def test_decompilation_is_a_focused_explicit_command(app):
    basic = R.analysis_commands(str(app), "/guest/rea")
    assert len(basic) == 1 and basic[0][1] == "analyze"
    deep = R.analysis_commands(str(app), "/guest/rea", query="save", function="0x1000")
    assert [c[1] for c in deep] == ["analyze", "search", "decompile"]
    assert deep[-1] == ["/guest/rea", "decompile", str(app), "--procedure=0x1000", "--provider", "ghidra", "--json"]
    assert deep[1] == ["/guest/rea", "search", str(app), "--pattern=save", "--provider", "ghidra", "--json"]
    objc = R.analysis_commands(str(app), "/guest/rea", function="-[Doc readFromData:ofType:error:]")
    assert objc[-1][3] == "--procedure=Doc::readFromData:ofType:error:"
    cls = R.analysis_commands(str(app), "/guest/rea", function="+[Doc version]")
    assert cls[-1][3] == "--procedure=Doc::version"
    assert R.analysis_commands(str(app), "/guest/rea", function="-x")[-1][3] == "--procedure=-x"
    # non-Ghidra providers keep REA's own naming; the Objective-C rewrite is Ghidra-only
    hopper = R.analysis_commands(str(app), "/guest/rea", provider="hopper", function="-[Doc save:]")
    assert hopper[-1] == ["/guest/rea", "decompile", str(app), "--procedure=-[Doc save:]",
                          "--provider", "hopper", "--json"]
    procs = R.analysis_commands(str(app), "/guest/rea", query="Doc", search_kind="procedures")
    assert procs[1] == ["/guest/rea", "search", str(app), "--pattern=Doc", "--kind", "procedures",
                        "--provider", "ghidra", "--json"]
    with pytest.raises(ValueError):
        R.analysis_commands(str(app), "/guest/rea", query="Doc", search_kind="bytes")
    js = R.analysis_commands(str(app), "/guest/rea", javascript="Contents/Resources")
    assert js[0][1] == "analyze-javascript-application"
    with pytest.raises(ValueError):
        R.analysis_commands(str(app), "/guest/rea", javascript="../outside")


def fat_fixture(*, fat64=False, duplicate_arm=False):
    cpus = [0x0100000c if duplicate_arm else 0x01000007, 0x0100000c]
    payloads = [struct.pack("<IIIIIIII", 0xfeedfacf, cpu, 0, 2, 0, 0, 0, 0) +
                bytes([i + 1]) * 32 for i, cpu in enumerate(cpus)]
    header = struct.pack(">II", 0xcafebabf if fat64 else 0xcafebabe, 2)
    for i, cpu in enumerate(cpus):
        values = [cpu, 0, 128 + 128*i, len(payloads[i]), 7]
        header += struct.pack(">IIQQII" if fat64 else ">IIIII", *(values + [0] if fat64 else values))
    data = header.ljust(128, b"\0") + payloads[0]
    return data.ljust(256, b"\0") + payloads[1], payloads[1]


@pytest.mark.parametrize("fat64", [False, True])
def test_native_slice_is_exact_and_original_bundle_is_unchanged(app, tmp_path, monkeypatch, fat64):
    monkeypatch.setattr(R.platform, "machine", lambda: "arm64")
    data, arm = fat_fixture(fat64=fat64)
    source = app / "Contents/MacOS/fixture"
    source.write_bytes(data)
    before = R.fingerprint(app)
    out = tmp_path / "slice"
    out.mkdir()
    target, receipt = R.native_analysis_target(app, out)
    assert pathlib.Path(target).read_bytes() == arm
    assert source.read_bytes() == data and R.fingerprint(app) == before
    assert receipt["offset"] == 256 and receipt["bytes"] == len(arm)
    assert receipt["sha256"] == R._file_hash(pathlib.Path(target))
    assert receipt["source_sha256"] == R._file_hash(source)
    assert R.analysis_commands(target, "/guest/rea")[0][2] == target


def test_native_slice_rejects_ambiguous_architecture_and_bad_bounds(app, tmp_path, monkeypatch):
    monkeypatch.setattr(R.platform, "machine", lambda: "arm64")
    source = app / "Contents/MacOS/fixture"
    out = tmp_path / "slice"
    out.mkdir()
    source.write_bytes(fat_fixture(duplicate_arm=True)[0])
    with pytest.raises(ValueError, match="unambiguous"):
        R.native_analysis_target(app, out)
    malformed = bytearray(fat_fixture()[0])
    struct.pack_into(">I", malformed, 8 + 20 + 8, 4096)
    source.write_bytes(malformed)
    with pytest.raises(ValueError, match="bounds"):
        R.native_analysis_target(app, out)
    assert not list(out.iterdir())


def test_native_slice_rejects_header_disagreement(app, tmp_path, monkeypatch):
    monkeypatch.setattr(R.platform, "machine", lambda: "arm64")
    data = bytearray(fat_fixture()[0])
    struct.pack_into("<I", data, 256 + 4, 0x01000007)
    (app / "Contents/MacOS/fixture").write_bytes(data)
    with pytest.raises(ValueError, match="does not match"):
        R.native_analysis_target(app, tmp_path)


def test_binding_treats_shell_syntax_as_data_and_checks_types(candidate):
    attack = 'hello; $(touch /tmp/never) `echo nope` "quotes"'
    argv = R.bind(candidate, {"output": "/guest/with spaces/file", "text": attack})
    assert argv[-1] == attack and argv[2] == "/guest/with spaces/file"
    for inputs in ({"output": "relative", "text": "x"}, {"output": "/x", "text": True},
                   {"output": "/x", "text": "x", "extra": "ignored?"}):
        with pytest.raises(ValueError):
            R.bind(candidate, inputs)


def test_unsupported_and_ambiguous_interfaces_stay_out(candidate):
    for change in ({"effect": "delete"}, {"argv": ["/bin/sh", "-c", "{text}", "{output}"]},
                   {"observer": {"kind": "actor_stdout", "path_param": "output"}},
                   {"argv": ["Contents/MacOS/fixture", "--output={output}", "{text}"]}):
        with pytest.raises(ValueError):
            R.validate_candidate({**candidate, **change})
    for malformed in ([], {**candidate, "target": []}, {**candidate, "params": ["not an object"]},
                      {**candidate, "argv": [42]}, {**candidate, "observer": []}):
        with pytest.raises(ValueError):
            R.validate_candidate(malformed)


def test_imported_verified_flag_does_not_promote(candidate):
    candidate["verified"] = True
    surface = R.candidate_surface(candidate, None, candidate["target"], app="Fixture")
    need = S.Need(app="Fixture", effect="doc_save", item_kind="document", mode="replace",
                  values={"path": "/guest/output", "text": "hello"})
    assert surface.interface == "partial" and S.serves(surface, need)[0] == "uncertain"
    assert "refused" in S.invocation(surface, {"output": "/guest/output", "text": "hello"})


def test_verify_route_and_replay_lifecycle(guest, candidate, app, tmp_path):
    proof = R.verify(candidate, cases(), str(app), str(tmp_path / "proof"))
    assert proof["status"] == "verified" and R.proof_status(candidate, proof, candidate["target"])[0]
    surface = R.candidate_surface(candidate, proof, candidate["target"], app="Fixture")
    need = S.Need(app="Fixture", effect="doc_save", item_kind="document", mode="replace",
                  values={"path": str(tmp_path / "replayed"), "text": "fresh input"})
    status, _, bindings = S.serves(surface, need)
    assert status == "served" and bindings == {"output": "path", "text": "text"}
    call = S.invocation(surface, {"output": need.values["path"], "text": need.values["text"]})
    assert call["requires_vm"] and call["via"] == "rea_mapping.replay" and "argv" not in call
    result = R.replay(candidate, proof, call["inputs"], str(app), str(tmp_path / "replay"))
    assert result["verdict"] == "verified" and (tmp_path / "replayed").read_text() == "fresh input"
    assert S.readback_for(surface, need)["readback"]["via"] == "file"
    assert S.readback_for(surface, need, ["ax"])["readback"]["via"] == "none"


def test_actor_success_is_not_file_success_and_no_retry(guest, monkeypatch, candidate, app, tmp_path):
    called = []
    def wrong(c, root, inputs, folder, timeout):
        called.append(inputs)
        pathlib.Path(inputs["output"]).write_text("wrong content")
        return {"returncode": 0, "timed_out": False}
    monkeypatch.setattr(R, "_execute", wrong)
    with pytest.raises(ValueError, match="without retry"):
        R.verify(candidate, cases(), str(app), str(tmp_path / "proof"))
    receipt = R.load_json(tmp_path / "proof/proof.json")
    assert len(called) == 1 and receipt["status"] == "incomplete"
    assert not R.proof_status(candidate, receipt, candidate["target"])[0]


def test_unsupported_replay_backend_refuses_expected_epoch_before_dispatch(guest, monkeypatch, candidate, app, tmp_path):
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: pytest.fail("unsupported pinned backend dispatched"))
    with pytest.raises(ValueError, match="cannot bind"):
        R.replay(candidate, {}, {}, str(app), str(tmp_path / "unsupported-epoch"), expected_identity={"pid": 999})
    assert not (tmp_path / "unsupported-epoch").exists()


def test_app_or_spec_change_refuses_replay_before_action(guest, monkeypatch, candidate, app, tmp_path):
    proof = R.verify(candidate, cases(), str(app), str(tmp_path / "proof"))
    edited = copy.deepcopy(candidate)
    edited["argv"][1] = "--different-effect"
    assert not R.proof_status(edited, proof, edited["target"])[0]
    (app / "Contents/Resources/data").write_text("updated")
    def never(*args, **kwargs):
        pytest.fail("must refuse before execution")
    monkeypatch.setattr(R, "_execute", never)
    with pytest.raises(ValueError, match="fingerprint changed"):
        R.replay(candidate, proof, {"output": str(tmp_path / "out"), "text": "test"}, str(app), str(tmp_path / "replay"))


def test_receipt_checks_complete_observations_and_diversity(guest, candidate, app, tmp_path):
    proof = R.verify(candidate, cases(), str(app), str(tmp_path / "proof"))
    partial = copy.deepcopy(proof)
    partial["cases"][0]["seen"] = {"exists": True}
    assert not R.proof_status(candidate, partial, candidate["target"])[0]
    duplicate = copy.deepcopy(proof)
    duplicate["cases"] = [duplicate["cases"][0]] * 3
    assert not R.proof_status(candidate, duplicate, candidate["target"])[0]
    same_inputs = [cases()[0]] * 3
    with pytest.raises(ValueError, match="distinct"):
        R.verify(candidate, same_inputs, str(app), str(tmp_path / "bad"))


def test_append_is_verified_against_initial_bytes(guest, candidate, app, tmp_path):
    candidate["mode"] = "append"
    proof = R.verify(candidate, cases(), str(app), str(tmp_path / "proof"))
    assert proof["status"] == "verified"
    assert (tmp_path / "proof/case-1/output").read_text() == "old contentSecond"


def test_cli_is_available_without_installing_rea(tmp_path, candidate):
    from click.testing import CliRunner
    from anyuse.cli import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["map", "rea", "--help"])
    assert result.exit_code == 0 and "analyze" in result.output and "verify" in result.output
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate))
    result = runner.invoke(cli, ["map", "rea", "inspect", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["current_app_checked"] is False
    from pathlib import Path
    native_example = Path(__file__).resolve().parents[1] / "examples/rea/native-receiver-candidate.json"
    native_res = runner.invoke(cli, ["map", "rea", "inspect", str(native_example)])
    assert native_res.exit_code == 0, native_res.output
    native_payload = json.loads(native_res.output)
    assert native_payload["id"] == "studio_select_september"
    assert native_payload["current_app_checked"] is False
    assert native_payload["contract"]["verification_tier"] == "model_judged"
    assert native_payload["contract"]["trial_policy"] == {
        "min_learning": 1, "min_verification": 1, "min_heldout": 1, "require_restart": True
    }
    assert native_payload["contract"]["native_evidence_basis"]["receiver_basis"] == "binary_correlated_hypothesis_v2"


def test_declared_script_rung_outranks_a_recovered_adapter(guest, monkeypatch, candidate, app, tmp_path):
    from anyuse.execution import map_compile as M
    proof = R.verify(candidate, cases(), str(app), str(tmp_path / "proof"))
    surface = R.candidate_surface(candidate, proof, candidate["target"], app="Fixture")
    transitions = S.surface_transitions([surface])
    amap = SimpleNamespace(transitions=transitions, get=lambda tid: transitions[0])
    t = transitions[0]
    step = M.PlanStep(t.id, t.effect, t.app, {"output": "/guest/output", "text": "hello"},
                      "test", t.sources, "", t.intent, 0)
    plan = SimpleNamespace(steps=[step])
    assert S.need_from_plan_step(step, amap).values == {"path": "/guest/output", "text": "hello"}
    assert S.prefer_surfaces(plan, amap)[0]["chosen"]["kind"] == "rea_adapter"
    preferred = S.prefer_surfaces(plan, amap, script_rung=lambda *args: (True, "save"))
    assert preferred[0]["chosen"]["kind"] == "scripting"
    assert S.surface_of(M.Transition.from_dict(transitions[0].to_dict())).kind == "rea_adapter"


def test_analysis_receipts_preserve_failures_and_do_not_claim_verification(guest, monkeypatch, app, tmp_path):
    tool = tmp_path / "rea-inert"
    tool.write_text("not actually executable")
    tool.chmod(0o700)
    calls = []
    def fake_run(argv, output, error, **kwargs):
        calls.append(argv)
        output.write_text("REA fake version\n" if argv[1] == "--version" else '{"inferred":true}')
        error.write_text("")
        return {"returncode": 0, "timed_out": False, "output": output.name, "sha256": R._file_hash(output)}
    monkeypatch.setattr(R, "_bounded_run", fake_run)
    report = R.investigate(str(app), str(tool), str(tmp_path / "analysis"), function="0x1000")
    assert report["status"] == "evidence_collected" and "verified" not in report
    assert [a[1] for a in calls] == ["--version", "analyze", "decompile"]
    def failing(argv, output, error, **kwargs):
        result = fake_run(argv, output, error, **kwargs)
        if argv[1] == "analyze":
            result["returncode"] = 1
        return result
    monkeypatch.setattr(R, "_bounded_run", failing)
    with pytest.raises(ValueError, match="failed"):
        R.investigate(str(app), str(tool), str(tmp_path / "failure"), function="0x1000")
    incomplete = R.load_json(tmp_path / "failure/analysis.json")
    assert incomplete["status"] == "incomplete" and len(incomplete["runs"]) == 1


@pytest.mark.parametrize("timed_out,code", [(False, "cleanup_incomplete"), (True, "cleanup_incomplete"), (False, "provider_timeout")])
def test_provider_cleanup_or_timeout_stops_analysis(guest, monkeypatch, app, tmp_path, timed_out, code):
    tool = tmp_path / "rea-inert"
    tool.write_text("inert")
    tool.chmod(0o700)
    calls = []
    def fail(argv, output, error, **kwargs):
        calls.append(argv[1])
        version = argv[1] == "--version"
        output.write_text("fake version" if version else json.dumps({"code": code, "category": "timeout" if code == "provider_timeout" else "provider"}))
        error.write_text("")
        return {"returncode": 0 if version else 1, "timed_out": timed_out and not version,
                "output": output.name, "sha256": R._file_hash(output)}
    monkeypatch.setattr(R, "_bounded_run", fail)
    with pytest.raises(R.ResourceReviewRequired, match="resources"):
        R.investigate(str(app), str(tool), str(tmp_path / "failed"), query="save", function="0x123")
    receipt = R.load_json(tmp_path / "failed/analysis.json")
    assert receipt["status"] == "incomplete" and receipt["resource_review_required"]
    assert calls == ["--version", "analyze"]


def test_timeout_stops_only_the_owned_process_group(monkeypatch, tmp_path):
    calls = []
    class FakeProcess:
        pid = 123456
        returncode = -15
        def __init__(self, argv, **kwargs):
            assert kwargs["start_new_session"] is True
            assert kwargs.get("shell", False) is False
            self.waits = 0
        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise R.subprocess.TimeoutExpired("fake", timeout)
            return self.returncode
    monkeypatch.setattr(R.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(R.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    run = R._bounded_run(["fake"], tmp_path / "stdout", tmp_path / "stderr", timeout=1)
    assert run["timed_out"] and run["returncode"] == -15
    assert calls == [(123456, R.signal.SIGTERM)]


def test_svg_backend_routes_validation_binding_proof_verification_and_replay_to_shared_harness(monkeypatch):
    from anyuse.execution import rea_harness as H
    candidate = {"backend": "guest_learned_svg_v1", "id": "routing_fixture"}
    calls = []
    monkeypatch.setattr(R, "assert_guest", lambda: calls.append("guard"))
    monkeypatch.setattr(H, "validate", lambda value: calls.append(("validate", value)) or value)
    monkeypatch.setattr(H, "bind", lambda value, inputs: calls.append(("bind", inputs)) or {"typed": inputs})
    monkeypatch.setattr(H, "proof_status", lambda value, proof, current: calls.append(("proof", proof, current)) or (True, "fixture"))
    monkeypatch.setattr(H, "verify", lambda *args: calls.append(("verify", args)) or {"status": "fixture-verified"})
    monkeypatch.setattr(H, "replay", lambda *args: calls.append(("replay", args)) or {"verdict": "fixture-replayed"})
    assert R.validate_candidate(candidate) == candidate
    assert R.bind(candidate, {"payload": "input"}) == {"typed": {"payload": "input"}}
    assert R.proof_status(candidate, {"receipt": 1}, {"target": 2}) == (True, "fixture")
    assert R.verify(candidate, [], "/not-read", "/not-created", timeout=5) == {"status": "fixture-verified"}
    assert R.replay(candidate, {}, {}, "/not-read", "/not-created", timeout=5) == {"verdict": "fixture-replayed"}
    assert {call[0] for call in calls if isinstance(call, tuple)} == {"validate", "bind", "proof", "verify", "replay"}
    assert calls.count("guard") == 2


def test_svg_backend_retains_explicit_scope_guard_during_task_binding(monkeypatch):
    from anyuse.execution import rea_harness as H
    c = {"backend": "guest_learned_svg_v1", "id": "scope_fixture", "effect": "load_state", "item_kind": "document", "mode": "",
         "scope": {"source": "path", "value": "/owned/workspace"},
         "params": [{"name": "payload", "source": "text"}, {"name": "oracle", "source": "name"}]}
    monkeypatch.setattr(H, "validate", lambda value: value)
    monkeypatch.setattr(H, "proof_status", lambda *args: (True, "fixture gate only"))
    seen = []
    monkeypatch.setattr(H, "bind", lambda candidate, inputs: seen.append(inputs) or inputs)
    surface = SimpleNamespace(invoke={"candidate": c, "proof": {}, "target": {}})
    def need(values):
        return SimpleNamespace(effect="load_state", item_kind="document", mode="", values=values)
    assert R.map_bindings(surface, need({"text": "input", "name": "/oracle"}))[0] == "uncertain"
    assert R.map_bindings(surface, need({"path": "/other", "text": "input", "name": "/oracle"}))[0] == "no"
    assert not seen
    result = R.map_bindings(surface, need({"path": "/owned/workspace", "text": "input", "name": "/oracle"}))
    assert result[0] == "served" and result[2] == {"payload": "text", "oracle": "name"}
    assert seen == [{"payload": "input", "oracle": "/oracle"}]


@pytest.mark.parametrize("with_profile", [False, True])
def test_agent_begin_cli_transports_optional_private_observer_declaration(app, tmp_path, monkeypatch, with_profile):
    from click.testing import CliRunner
    from anyuse.execution import rea_agent as G, rea_cli
    spec = {"task": "Read the complete result", "params": [],
            "success_criteria": ["Compare independent content"], "plan": {}}
    declaration = {"format": "inert-declaration-for-cli-transport",
                   "reference": {"private": "CALLER_REFERENCE_PRIVATE"}}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))
    profile_file = tmp_path / "observer.json"
    profile_file.write_text(json.dumps(declaration))
    tool = tmp_path / "rea-inert"
    tool.write_text("not executed")
    calls = []
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    def begin(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "investigating", "proof_created": False}
    monkeypatch.setattr(G, "begin", begin)
    argv = ["agent", "begin", str(app), str(spec_file), "--rea", str(tool),
            "--out", str(tmp_path / "new-session")]
    if with_profile:
        argv += ["--observer-profile", str(profile_file)]
    result = CliRunner().invoke(rea_cli.rea_commands, argv)
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[2:6] == (spec["task"], spec["params"], spec["success_criteria"], spec["plan"])
    assert kwargs["observer_profile"] == (declaration if with_profile else None)
    assert "CALLER_REFERENCE_PRIVATE" not in result.output
    assert not (tmp_path / "new-session").exists()


def test_agent_begin_cli_invalid_observer_json_never_begins_session(app, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution import rea_agent as G, rea_cli
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"task": "Inspect", "params": [], "success_criteria": ["Check"], "plan": {}}))
    profile_file = tmp_path / "observer.json"
    profile_file.write_text("{invalid")
    tool = tmp_path / "rea-inert"
    tool.write_text("not executed")
    calls = []
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(G, "begin", lambda *args, **kwargs: calls.append(args))
    result = CliRunner().invoke(rea_cli.rea_commands, ["agent", "begin", str(app), str(spec_file),
        "--rea", str(tool), "--out", str(tmp_path / "new-session"), "--observer-profile", str(profile_file)])
    assert result.exit_code != 0 and not calls
    assert not (tmp_path / "new-session").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


@pytest.mark.parametrize('command', ['execute', 'replay'])
@pytest.mark.parametrize('receipt,success', [
    ({'verdict': 'verified', 'model_calls': 0}, True),
    ({'verdict': 'model_judged', 'verification_tier': 'model_judged',
      'backend': 'guest_model_judged_state_v1', 'model_calls': 2}, True),
    ({'verdict': 'model_judged', 'backend': 'guest_model_judged_state_v1'}, False),
    ({'verdict': 'model_judged', 'verification_tier': 'deterministic',
      'backend': 'guest_model_judged_state_v1'}, False),
    ({'verdict': 'model_judged', 'verification_tier': 'model_judged', 'backend': 'unknown'}, False),
    ({'verdict': 'refuted', 'verification_tier': 'model_judged', 'backend': 'guest_model_judged_state_v1'}, False),
    ({'verdict': 'uncertain'}, False),
    ({'verdict': True, 'success': True}, False),
])
def test_replay_cli_exits_for_exact_supported_verdict_and_retains_original_tier(app, tmp_path, monkeypatch, command, receipt, success):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli, rea_actions as A
    files = {}
    for name in ('candidate', 'proof', 'catalog', 'selection', 'inputs'):
        file = tmp_path / (name + '.json'); file.write_text('{}'); files[name] = str(file)
    calls = []
    def replay(*args, **kwargs):
        calls.append((args, kwargs)); return copy.deepcopy(receipt)
    monkeypatch.setattr(A, 'execute', replay)
    monkeypatch.setattr(R, 'replay', replay)
    if command == 'execute':
        argv = ['execute', files['catalog'], files['selection']]
    else:
        argv = ['replay', files['candidate'], '--proof', files['proof'], '--bundle', str(app)]
    argv += ['--inputs', files['inputs'], '--out', str(tmp_path / 'replay-output'), '--timeout', '240']
    result = CliRunner().invoke(rea_cli.rea_commands, argv)
    assert result.exit_code == (0 if success else 1), result.output
    assert len(calls) == 1 and calls[0][1]['timeout'] == 240
    actual, _ = json.JSONDecoder().raw_decode(result.output)
    assert actual == receipt
    if success:
        assert json.loads(result.output) == receipt
    else:
        assert 'no automatic retry' in result.output
    assert not (tmp_path / 'replay-output').exists()


def test_documented_local_visual_configuration_builds_valid_profile_and_plan(app, tmp_path, monkeypatch):
    from anyuse.execution import rea_visual_campaign as C
    text = (_HERE / 'docs/REA_ACCESS_ROUTES.md').read_text()
    section = text.split('### Visible effects without a deterministic observer', 1)[1]
    code = section.split('```python\n', 1)[1].split('\n```', 1)[0]
    target = R.fingerprint(app)
    seen = []
    def fingerprint(bundle):
        seen.append(bundle); return copy.deepcopy(target)
    monkeypatch.setattr(R, 'fingerprint', fingerprint)
    monkeypatch.chdir(tmp_path)
    # Trusted repository documentation, with its sole app read replaced by an
    # inert fingerprint; no VM, network, inference or application dispatch.
    exec(compile(code, 'documented-visual-configuration', 'exec'), {})
    profile = json.loads((tmp_path / 'visual-profile.json').read_text())
    spec = json.loads((tmp_path / 'visual-task.json').read_text())
    validated = C.validate_profile_declaration(profile)
    plan = C.validate_coverage(spec['plan'], validated['policy'])
    assert seen == ['/path/to/Test.app']
    assert profile['policy']['target'] == target
    assert profile['policy']['model']['provider'] == 'local' and profile['policy']['model']['retries'] == 0
    assert profile['policy']['model']['base_url'].startswith('http://127.0.0.1:')
    assert profile['policy']['model']['api_key_env'] == ''
    assert all(len(plan[phase]) == 3 for phase in ('learning', 'verification', 'heldout'))
    assert plan['heldout'][0]['restart'] is True and all(row['restart'] is False for row in plan['verification'])
    assert 'actor' not in profile and 'evidence' not in profile
