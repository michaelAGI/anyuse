"""Access-route regressions; execute only through the guarded guest runner."""
import json
import os
from pathlib import Path
import plistlib
import struct

import pytest

from anyuse.execution import rea_access as A
from anyuse.execution import rea_inspector_bridge as I


def app_fixture(tmp_path):
    app = tmp_path / "Access Trial.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "org.anyuse.rea.access-test", "CFBundleExecutable": "trial",
        "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
    }))
    (app / "Contents/MacOS/trial").write_bytes(struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0))
    return app


def static_commands(monkeypatch):
    monkeypatch.setattr(A.R, "assert_guest", lambda: None)
    def run(argv):
        if "--entitlements" in argv:
            return {"exit_code": 0, "stdout": plistlib.dumps({
                "com.apple.security.get-task-allow": False,
                "com.apple.security.cs.disable-library-validation": True,
                "private-unrelated-value": "not exported",
            }).decode(), "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": "flags=0x10000(runtime)\nTeamIdentifier=LABTEAM\n"}
    monkeypatch.setattr(A, "_command", run)


def inspector_receipt(**changes):
    return {"format": "anyuse.rea-inspector/1", "status": "observed",
            "state_uncertain": False, "resource_review_required": False,
            "method_dispatch_attempts": 0, "getter_dispatch_attempts": 0,
            "actuated": False, "proof_created": False, "task_result_verified": False,
            "cleanup": {"object_group_released": True, "page_socket_closed": True,
                        "browser_socket_closed": True, "requested_evaluations_completed": True,
                        "remote_execution_cancelled": False, "local_live_ids_discarded": True},
            **changes}


@pytest.mark.parametrize("probe", [None, "local", "gadget", "inspector"])
def test_guest_guard_precedes_target_reads_output_writes_and_probes(monkeypatch, tmp_path, probe):
    def denied(): raise PermissionError("VM required")
    monkeypatch.setattr(A.R, "assert_guest", denied)
    monkeypatch.setattr(A.R, "fingerprint", lambda _: pytest.fail("target read before VM guard"))
    out = tmp_path / "out"
    with pytest.raises(PermissionError, match="VM required"):
        A.inspect(tmp_path / "missing.app", out, probe=probe)
    assert not out.exists()


@pytest.mark.parametrize("probe", [None, "local", "gadget", "inspector"])
@pytest.mark.parametrize("alias", ["direct", "output_alias", "bundle_alias"])
def test_output_inside_target_is_refused_before_pinning_mkdir_or_any_probe(monkeypatch, tmp_path, probe, alias):
    app = app_fixture(tmp_path)
    selected = app
    out = app / "Contents/new-evidence/nested"
    if alias == "output_alias":
        link = tmp_path / "evidence-alias"
        link.symlink_to(app, target_is_directory=True)
        out = link / "Contents/new-evidence/nested"
    elif alias == "bundle_alias":
        selected = tmp_path / "Target Alias.app"
        selected.symlink_to(app, target_is_directory=True)
        out = selected / "Contents/new-evidence/nested"
    before = {str(path.relative_to(app)): path.read_bytes() for path in app.rglob("*") if path.is_file()}
    monkeypatch.setattr(A.R, "assert_guest", lambda: None)
    monkeypatch.setattr(A.R, "fingerprint", lambda *a: pytest.fail("target pinned before output containment"))
    monkeypatch.setattr(A, "_command", lambda *a: pytest.fail("native command before output containment"))
    monkeypatch.setattr(A.B, "collect", lambda *a, **k: pytest.fail("native probe before output containment"))
    monkeypatch.setattr(I, "collect", lambda *a, **k: pytest.fail("inspector probe before output containment"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("mkdir before output containment"))
    with pytest.raises(ValueError, match="outside the target app bundle"):
        A.inspect(selected, out, probe=probe)
    assert not (app / "Contents/new-evidence").exists()
    assert {str(path.relative_to(app)): path.read_bytes() for path in app.rglob("*") if path.is_file()} == before


def test_static_metadata_never_claims_access_or_tries_another_transport(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    monkeypatch.setattr(A.B, "collect", lambda *a, **k: pytest.fail("implicit probe"))
    monkeypatch.setattr(I, "collect", lambda *a, **k: pytest.fail("implicit inspector probe"))
    app = app_fixture(tmp_path)
    result = A.inspect(app, tmp_path / "static")
    assert result["probe"] is None and result["automatic_fallback"] is False
    assert result["inventory"]["architectures"][0]["architecture"] == "arm64"
    assert result["inventory"]["selected_entitlements"] == {
        "com.apple.security.get-task-allow": False,
        "com.apple.security.cs.disable-library-validation": True,
    }
    assert all(route["status"] != "access_observed" for route in result["routes"])
    assert result["target_unchanged"] and not result["actuated"]
    assert not result["proof_created"] and not result["task_result_verified"]
    inspector = next(route for route in result["routes"] if route["id"] == "inspector")
    assert inspector["status"] == "unprobed" and not inspector["metadata_implies_access_permission"]
    assert not inspector["changes_applied"] and inspector["requires"]
    assert "private-unrelated-value" not in Path(result["receipt"]).read_text()


def test_explicit_probe_preserves_denial_and_restores_backend_without_fallback(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_TRANSPORT", "gadget")
    calls = []
    def probe(bundle, out, request, timeout, backend):
        assert os.environ["ANYUSE_REA_RUNTIME_TRANSPORT"] == "gadget"
        calls.append((backend, request))
        return {"status": "unavailable", "error": {"type": "PermissionDeniedError", "message": "denied"},
                "result": {"invocations": 0, "attached": False}, "resource_review_required": False}
    monkeypatch.setattr(A.B, "collect", probe)
    result = A.inspect(app_fixture(tmp_path), tmp_path / "probe", probe="local")
    assert len(calls) == 1 and calls[0][0] == "local"
    assert all(op["op"] == "classes" for op in calls[0][1]["operations"])
    assert os.environ["ANYUSE_REA_RUNTIME_TRANSPORT"] == "gadget"
    assert result["probe"]["classification"]["category"] == "authorization_denied"
    assert next(r for r in result["routes"] if r["id"] == "gadget")["status"] == "unprobed"


def test_unknown_probe_failure_restores_environment_even_when_provider_raises(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    monkeypatch.delenv("ANYUSE_REA_RUNTIME_TRANSPORT", raising=False)
    def failed(*a, **k): raise RuntimeError("provider failure")
    monkeypatch.setattr(A.B, "collect", failed)
    with pytest.raises(RuntimeError, match="provider failure"):
        A.inspect(app_fixture(tmp_path), tmp_path / "error", probe="gadget")
    assert "ANYUSE_REA_RUNTIME_TRANSPORT" not in os.environ


def test_explicit_inspector_probe_is_bounded_dom_metadata_without_native_fallback(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    app = app_fixture(tmp_path)
    out = tmp_path / "inspector"
    monkeypatch.setenv("ANYUSE_REA_RUNTIME_TRANSPORT", "gadget")
    monkeypatch.setenv("ANYUSE_REA_INSPECTOR_CONFIG", "/guest/operator/config.json")
    monkeypatch.setattr(A.B, "collect", lambda *a, **k: pytest.fail("native fallback from inspector"))
    calls = []
    receipt = inspector_receipt()
    def probe(bundle, folder, request, *, timeout, config_file=None):
        assert config_file is None
        assert os.environ["ANYUSE_REA_INSPECTOR_CONFIG"] == "/guest/operator/config.json"
        assert I.validate_request(request) == request
        calls.append((bundle, folder, request, timeout))
        return receipt
    monkeypatch.setattr(I, "collect", probe)
    result = A.inspect(app, out, probe="inspector", timeout=7)
    assert calls == [(str(app.resolve()), out.resolve() / "probe-inspector",
                      {"operations": [{"op": "dom_query", "selector": "body", "max_nodes": 1}]}, 7)]
    assert os.environ["ANYUSE_REA_RUNTIME_TRANSPORT"] == "gadget"
    assert os.environ["ANYUSE_REA_INSPECTOR_CONFIG"] == "/guest/operator/config.json"
    saved = json.loads(Path(result["receipt"]).read_text())
    assert saved["probe"]["provider"] == "inspector" and saved["probe"]["backend"] == "inspector"
    assert saved["probe"]["receipt"] == str(out.resolve() / "probe-inspector/inspector.json")
    assert saved["probe"]["runtime"] == receipt
    classification = saved["probe"]["classification"]
    assert classification["category"] == "access_observed" and classification["attached"] is None
    assert classification["cleanup"] == receipt["cleanup"] and not classification["state_uncertain"]
    assert not saved["resource_review_required"] and not saved["proof_created"] and not saved["task_result_verified"]
    assert not saved["actuated"] and not saved["automatic_fallback"]
    assert all(route["status"] == "unprobed" for route in saved["routes"] if route["id"] in {"local", "gadget"})
    assert next(route for route in saved["routes"] if route["id"] == "inspector")["status"] == "access_observed"


def test_missing_inspector_configuration_refuses_before_connections_and_never_tries_native(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    monkeypatch.delenv("ANYUSE_REA_INSPECTOR_CONFIG", raising=False)
    monkeypatch.setattr(A.B, "collect", lambda *a, **k: pytest.fail("native fallback from missing config"))
    monkeypatch.setattr(I, "_socket", lambda *a, **k: pytest.fail("socket before operator configuration"))
    monkeypatch.setattr(I, "_listener", lambda *a, **k: pytest.fail("listener before operator configuration"))
    monkeypatch.setattr(I, "_http_json", lambda *a, **k: pytest.fail("HTTP before operator configuration"))
    out = tmp_path / "missing-config"
    with pytest.raises(A.B.Unavailable, match="owned canonical 0600"):
        A.inspect(app_fixture(tmp_path), out, probe="inspector")
    assert "ANYUSE_REA_INSPECTOR_CONFIG" not in os.environ
    assert not (out / "probe-inspector").exists() and not (out / "access.json").exists()


@pytest.mark.parametrize("failure", ["protocol_uncertain", "cleanup_failed", "observed_unreleased"])
def test_inspector_cleanup_and_uncertainty_are_preserved_without_observed_access_credit(monkeypatch, tmp_path, failure):
    static_commands(monkeypatch)
    monkeypatch.setattr(A.B, "collect", lambda *a, **k: pytest.fail("native fallback after inspector failure"))
    receipt = inspector_receipt(status="incomplete", error={"type": "ProtocolError", "message": "context lost"})
    if failure == "protocol_uncertain":
        receipt["state_uncertain"] = True
    else:
        receipt["cleanup"]["object_group_released"] = False
        receipt["resource_review_required"] = failure == "cleanup_failed"
        if failure == "observed_unreleased":
            receipt["status"] = "observed"
    monkeypatch.setattr(I, "collect", lambda *a, **k: receipt)
    result = A.inspect(app_fixture(tmp_path), tmp_path / "failed-probe", probe="inspector")
    classification = result["probe"]["classification"]
    assert result["probe"]["runtime"] == receipt
    assert classification["category"] == "resource_review_required"
    assert classification["cleanup"] == receipt["cleanup"]
    assert classification["state_uncertain"] is receipt["state_uncertain"]
    assert result["resource_review_required"] and not result["task_result_verified"]
    assert not result["automatic_fallback"]


def test_inspector_source_change_is_incomplete_without_access_or_task_credit(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    app = app_fixture(tmp_path)
    monkeypatch.setattr(A.B, "collect", lambda *a, **k: pytest.fail("native fallback after source change"))
    receipt = inspector_receipt(status="incomplete", error={"type": "Unavailable", "message": "app bytes changed during evaluation"})
    def probe(*a, **k):
        executable = app / "Contents/MacOS/trial"
        executable.write_bytes(executable.read_bytes() + b"changed")
        return receipt
    monkeypatch.setattr(I, "collect", probe)
    result = A.inspect(app, tmp_path / "changed-source", probe="inspector")
    assert result["status"] == "incomplete" and not result["target_unchanged"]
    assert result["probe"]["classification"]["category"] != "access_observed"
    assert result["probe"]["classification"]["cleanup"] == receipt["cleanup"]
    assert not result["resource_review_required"] and not result["proof_created"] and not result["task_result_verified"]


@pytest.mark.parametrize("error,category", [
    ({"type": "PermissionDeniedError", "message": "access denied"}, "authorization_denied"),
    ({"type": "Error", "message": "thread_create invalid argument"}, "injection_failed"),
    ({"type": "Error", "message": "connection refused"}, "endpoint_unavailable"),
    ({"type": "Error", "message": "library validation: different Team ID"}, "code_loading_denied"),
    ({"type": "Unavailable", "message": "target is not running"}, "target_unavailable"),
    ({"type": "Unavailable", "message": "need one unambiguous running target app"}, "target_missing_or_ambiguous"),
    ({"type": "Unavailable", "message": "running app identity does not match the exact target bundle"}, "target_identity_denied"),
])
def test_errors_are_classified_without_inventing_root_causes(error, category):
    result = A.classify_probe({"status": "unavailable", "error": error})
    assert result["category"] == category and not result["inferred_root_cause"]


def test_resource_failure_takes_precedence_over_observed_access():
    result = A.classify_probe({"status": "observed", "resource_review_required": True})
    assert result["category"] == "resource_review_required"


def test_native_uncertainty_and_cleanup_keep_their_native_receipt_shape():
    cleanup = {"unloaded": True, "detached": False}
    result = A.classify_probe({"status": "observed", "result": {
        "attached": True, "cleanup": cleanup, "native_state_uncertain": True}})
    assert result["category"] == "resource_review_required" and result["state_uncertain"]
    assert result["resource_review_required"] and result["attached"] is True and result["cleanup"] == cleanup


def test_documented_plugin_is_a_candidate_and_must_name_the_actual_target(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    evidence = {"kind": "native_plugin", "documentation": "https://vendor.example/native-addon",
                "observed_host_bundle_id": "org.anyuse.rea.access-test", "minimum_version": "1.0",
                "entrypoint": "native_init"}
    app = app_fixture(tmp_path)
    result = A.inspect(app, tmp_path / "plugin", plugin_evidence=evidence)
    plugin = next(route for route in result["routes"] if route["id"] == "native_plugin")
    assert plugin["status"] == "declared_not_verified" and not plugin["evidence"]["verified"]
    with pytest.raises(ValueError, match="exact host identity"):
        A.inspect(app, tmp_path / "wrong-plugin", plugin_evidence={**evidence, "observed_host_bundle_id": "other.app"})
    assert not (tmp_path / "wrong-plugin").exists()


def test_evidence_directory_is_not_reused_or_overwritten(monkeypatch, tmp_path):
    static_commands(monkeypatch)
    app = app_fixture(tmp_path)
    out = tmp_path / "receipt"
    A.inspect(app, out)
    original = (out / "access.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        A.inspect(app, out)
    assert (out / "access.json").read_bytes() == original


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
