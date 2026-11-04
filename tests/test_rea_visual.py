"""Exact-window visual evidence stays separate from verified action proofs."""
import base64
import copy
import json
import os
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_visual as V

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1sAAAAASUVORK5CYII=")


class FakeBackend:
    def __init__(self, app):
        self.app = app
        self.calls = []
        self.failure = None
        self.after_failure = None
        self.rc = 0
        self.png = PNG
        self.after = None
        self.change_bundle = False
        self.state = {"session": {"uid": 501, "on_console": True},
                      "apps": [{"bundle_id": "org.test.Canvas", "bundle": str(app), "pid": 321,
                                "launch_time": 12345.25, "terminated": False}],
                      "windows": [{"id": 456, "pid": 321, "title": "Scene", "layer": 0,
                                   "on_screen": True, "alpha": 1.0,
                                   "bounds": {"X": 20.0, "Y": 10.0, "Width": 900.0, "Height": 700.0}}]}

    def inspect(self, bundle_id):
        assert bundle_id == "org.test.Canvas"
        if self.calls and self.after_failure:
            raise self.after_failure
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.after if self.calls and self.after is not None else self.state)

    def capture(self, argv, *, timeout=V.CAPTURE_TIMEOUT):
        self.calls.append(argv)
        if self.change_bundle:
            (self.app / "Contents/resource.txt").write_text("same version, changed resource")
        if isinstance(self.png, Exception):
            raise self.png
        Path(argv[-1]).write_bytes(self.png)
        return SimpleNamespace(returncode=self.rc, stdout=b"", stderr=b"capture diagnostic")


@pytest.fixture
def visual(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = tmp_path / "Canvas.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.test.Canvas",
                                                            "CFBundleVersion": "1"}))
    backend = FakeBackend(app)
    monkeypatch.setattr(V, "Backend", lambda: backend)
    return app, backend, tmp_path / "visual"


def test_capture_is_window_scoped_and_remains_evidence(visual):
    app, backend, out = visual
    result = V.capture(str(app), str(out))
    assert result["status"] == "captured"
    assert backend.calls == [["/usr/sbin/screencapture", "-x", "-o", "-l", "456", "-t", "png", str(out / "window.png")]]
    assert result["target"] == R.fingerprint(app)
    assert result["identity"] == result["after_identity"]
    assert result["identity"]["process"]["bundle"] == str(app)
    assert result["image"]["sha256"] == R._file_hash(out / "window.png")
    assert (result["image"]["width"], result["image"]["height"]) == (1, 1)
    assert result["image"]["pixels_per_point"] == {"x": 1 / 900, "y": 1 / 700}
    assert result["capture_started_at"] <= result["capture_finished_at"]
    assert result["verified"] is False and result["actuated"] is False and result["evidence_only"] is True
    assert json.loads((out / "visual.json").read_text()) == result
    assert not (out / "proof.json").exists()


def test_nonzero_owned_window_layer_requires_explicit_selection(visual, tmp_path):
    app, backend, out = visual
    backend.state["windows"][0]["layer"] = 8
    refused = V.capture(str(app), str(out))
    assert refused["status"] == "unavailable" and not backend.calls
    metadata = V.inspect(str(app), str(tmp_path / "all-owned-layers"), window_layer=None)
    assert metadata["status"] == "observed" and metadata["windows"][0]["layer"] == 8
    captured = V.capture(str(app), str(tmp_path / "explicit-layer"), window_layer=8)
    assert captured["status"] == "captured" and captured["identity"]["window"]["layer"] == 8
    assert captured["identity"] == captured["after_identity"] and len(backend.calls) == 1


@pytest.mark.parametrize("layer", [None, True, -1, 101, 8.0, "8"])
def test_window_layer_is_bounded_typed_and_refused_before_output(visual, layer):
    app, backend, out = visual
    with pytest.raises(ValueError, match="window layer"):
        V.capture(str(app), str(out), window_layer=layer)
    assert not out.exists() and not backend.calls


@pytest.mark.parametrize("message", ["guest screen is locked", "no existing Screen Recording grant"])
def test_permission_or_lock_unavailable_preserves_receipt_without_capture(visual, message):
    app, backend, out = visual
    backend.failure = V.Unavailable(message)
    result = V.capture(str(app), str(out))
    assert result["status"] == "unavailable" and result["error"]["message"] == message
    assert result["image"] is None and not backend.calls and (out / "visual.json").exists()


@pytest.mark.parametrize("change", ["missing", "other_app", "same_id_other_path", "exited", "two_apps", "reused_pid"])
def test_wrong_ambiguous_or_invalid_process_refuses_before_capture(visual, change, tmp_path):
    app, backend, out = visual
    process = backend.state["apps"][0]
    if change == "missing":
        backend.state["apps"] = []
    elif change == "other_app":
        process["bundle_id"] = "org.test.Other"
    elif change == "same_id_other_path":
        other = tmp_path / "Other.app"
        other.mkdir()
        process["bundle"] = str(other)
    elif change == "exited":
        process["terminated"] = True
    elif change == "two_apps":
        backend.state["apps"].append(copy.deepcopy(process))
    else:
        process["launch_time"] = None
    result = V.capture(str(app), str(out))
    assert result["status"] == "unavailable" and not backend.calls


@pytest.mark.parametrize("change", ["wrong_pid", "offscreen", "transparent", "wrong_layer", "ambiguous", "bad_bounds"])
def test_unavailable_windows_do_not_fall_back_to_a_display_capture(visual, change):
    app, backend, out = visual
    window = backend.state["windows"][0]
    if change == "wrong_pid":
        window["pid"] = 777
    elif change == "offscreen":
        window["on_screen"] = False
    elif change == "transparent":
        window["alpha"] = 0
    elif change == "wrong_layer":
        window["layer"] = 3
    elif change == "ambiguous":
        backend.state["windows"].append(copy.deepcopy(window))
    else:
        window["bounds"]["Width"] = float("nan")
    result = V.capture(str(app), str(out))
    assert result["status"] == "unavailable" and not backend.calls
    assert result["image"] is None


def test_exact_title_scopes_among_two_windows(visual):
    app, backend, out = visual
    second = copy.deepcopy(backend.state["windows"][0])
    second.update(id=999, title="Other scene")
    backend.state["windows"].append(second)
    result = V.capture(str(app), str(out), window_title="Other scene")
    assert result["status"] == "captured" and backend.calls[0][4] == "999"


def test_wrong_exact_title_refuses_even_when_one_window_exists(visual):
    app, backend, out = visual
    result = V.capture(str(app), str(out), window_title="scene")
    assert result["status"] == "unavailable" and not backend.calls


@pytest.mark.parametrize("change", ["window", "bounds", "title", "pid_relaunch", "session", "bundle"])
def test_changes_during_capture_preserve_png_but_never_accept_it(visual, change):
    app, backend, out = visual
    backend.after = copy.deepcopy(backend.state)
    if change == "window":
        backend.after["windows"][0]["id"] += 1
    elif change == "bounds":
        backend.after["windows"][0]["bounds"]["X"] += 1
    elif change == "title":
        backend.after["windows"][0]["title"] = "A different document"
    elif change == "pid_relaunch":
        backend.after["apps"][0]["launch_time"] += 1
    elif change == "session":
        backend.after["session"]["uid"] += 1
    else:
        backend.change_bundle = True
    result = V.capture(str(app), str(out))
    assert result["status"] == "unavailable" and result["image"] is None
    assert len(backend.calls) == 1 and (out / "window.png").exists()
    assert any(x["file"] == "window.png" for x in result["artifacts"])
    assert result["verified"] is False


@pytest.mark.parametrize("failure", ["exit", "timeout", "empty", "malformed", "incomplete"])
def test_failed_or_incomplete_capture_persists_error_and_diagnostics(visual, failure):
    app, backend, out = visual
    if failure == "exit":
        backend.rc = 1
    elif failure == "timeout":
        backend.png = subprocess.TimeoutExpired(["screencapture"], 15, output=b"partial", stderr=b"timeout")
    elif failure == "empty":
        backend.png = b""
    elif failure == "malformed":
        backend.png = b"not an image" * 12
    else:
        backend.png = PNG[:-12]
    result = V.capture(str(app), str(out))
    assert result["status"] == "error" and result["image"] is None and len(backend.calls) == 1
    assert (out / "visual.json").exists() and (out / "capture.stderr").exists()
    if failure == "timeout":
        assert result["run"]["timed_out"] is True
        assert (out / "capture.stdout").read_text() == "partial"


@pytest.mark.parametrize("message", ["guest locked during capture", "Screen Recording grant changed"])
def test_lock_or_permission_change_after_capture_never_accepts_pixels(visual, message):
    app, backend, out = visual
    backend.after_failure = V.Unavailable(message)
    result = V.capture(str(app), str(out))
    assert result["status"] == "unavailable" and result["image"] is None
    assert result["error"]["message"] == message and (out / "window.png").exists()
    assert len(backend.calls) == 1


def test_oversized_image_is_preserved_without_unbounded_hashing(visual, monkeypatch):
    app, backend, out = visual
    backend.png = PNG * 2
    monkeypatch.setattr(V, "MAX_PNG_BYTES", len(PNG))
    result = V.capture(str(app), str(out))
    assert result["status"] == "error" and result["image"] is None
    entry = next(x for x in result["artifacts"] if x["file"] == "window.png")
    assert entry["oversized"] is True and entry["sha256"] is None
    assert (out / "window.png").read_bytes() == PNG * 2


def test_no_overwrite_or_output_inside_bundle(visual):
    app, backend, out = visual
    with pytest.raises(ValueError, match="outside"):
        V.capture(str(app), str(app / "Contents/visual"))
    out.mkdir()
    with pytest.raises(FileExistsError):
        V.capture(str(app), str(out))
    assert not backend.calls


def test_vm_guard_precedes_all_reads_writes_and_native_backend(tmp_path, monkeypatch):
    calls = []
    def refuse():
        raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    monkeypatch.setattr(V, "Backend", lambda: calls.append("native"))
    with pytest.raises(PermissionError, match="VM only"):
        V.capture("nonexistent.app", str(tmp_path / "guarded"))
    with pytest.raises(PermissionError, match="VM only"):
        V.inspect("nonexistent.app", str(tmp_path / "guarded-windows"))
    assert not calls and not (tmp_path / "guarded").exists() and not (tmp_path / "guarded-windows").exists()


def test_window_discovery_persists_full_native_titles_without_pixels_or_ax(visual):
    app, backend, out = visual
    first = backend.state["windows"][0]
    first["title"] = "Scene.game — Player A / Player B — White to move"
    second = copy.deepcopy(first)
    second.update(id=999, title="A second scene")
    backend.state["windows"].append(second)
    foreign = copy.deepcopy(first)
    foreign.update(pid=777, title="Unrelated app")
    overlay = copy.deepcopy(first)
    overlay.update(id=777, layer=3, title="Target overlay")
    backend.state["windows"].extend([foreign, overlay])
    result = V.inspect(str(app), str(out))
    assert result["status"] == "observed" and result["target"] == R.fingerprint(app)
    assert [w["title"] for w in result["windows"]] == [first["title"], second["title"]]
    assert result["identity"]["process"]["pid"] == 321 and result["window_count"] == 2
    assert result["truncated"] is False and result["window_limit"] == 128
    assert not backend.calls and sorted(p.name for p in out.iterdir()) == ["windows.json"]
    assert result["evidence_only"] is True and result["proof_created"] is False
    assert result["verified"] is False and result["actuated"] is False
    assert json.loads((out / "windows.json").read_text()) == result


def test_window_discovery_reports_a_bounded_prefix_and_truncation(visual):
    app, backend, out = visual
    window = backend.state["windows"][0]
    backend.state["windows"] = [dict(copy.deepcopy(window), id=i + 1, title=f"Scene {i}")
                                for i in range(132)]
    result = V.inspect(str(app), str(out))
    assert result["status"] == "observed" and len(result["windows"]) == 128
    assert result["window_count"] == 132 and result["truncated"] is True and not backend.calls


def test_window_discovery_does_not_require_a_window_or_launch_the_app(visual):
    app, backend, out = visual
    backend.state["windows"] = []
    result = V.inspect(str(app), str(out))
    assert result["status"] == "observed" and result["windows"] == [] and not backend.calls
    backend.state["apps"] = []
    missing = V.inspect(str(app), str(out.parent / "not-running"))
    assert missing["status"] == "unavailable" and not backend.calls


@pytest.mark.parametrize("failure", ["permission", "wrong_process", "bad_bounds", "oversized_caption"])
def test_window_discovery_failures_are_saved_as_evidence_not_success(visual, failure):
    app, backend, out = visual
    if failure == "permission":
        backend.failure = V.Unavailable("no existing Screen Recording grant")
    elif failure == "wrong_process":
        backend.state["apps"][0]["bundle_id"] = "org.test.Other"
    elif failure == "bad_bounds":
        backend.state["windows"][0]["bounds"]["Height"] = 0
    else:
        backend.state["windows"][0]["title"] = "a" * (V.MAX_TITLE_BYTES + 1)
    result = V.inspect(str(app), str(out))
    assert result["status"] == "unavailable" and (out / "windows.json").exists()
    assert result["proof_created"] is False and not backend.calls


def test_window_discovery_detects_same_version_app_change(visual, monkeypatch):
    app, backend, out = visual
    native_inspect = backend.inspect
    def changed(bundle_id):
        snapshot = native_inspect(bundle_id)
        (app / "Contents/new-resource.txt").write_text("different bytes, same version")
        return snapshot
    monkeypatch.setattr(backend, "inspect", changed)
    result = V.inspect(str(app), str(out))
    assert result["status"] == "unavailable" and "fingerprint changed" in result["error"]["message"]
    assert result["target"] != result["after_target"] and not backend.calls


def native_backend(app, *, locked=False, permission=True, console=True):
    """Native boundary fixture: no imported framework, AX, GUI or subprocess."""
    calls = []
    def session():
        calls.append("session")
        return {"CGSSessionScreenIsLocked": locked, "kCGSSessionOnConsoleKey": console,
                "kCGSSessionUserIDKey": os.getuid()}
    def preflight():
        calls.append("preflight")
        return permission
    process = SimpleNamespace(bundleURL=lambda: SimpleNamespace(path=lambda: str(app)),
                              launchDate=lambda: SimpleNamespace(timeIntervalSince1970=lambda: 12345.25),
                              bundleIdentifier=lambda: "org.test.Canvas", processIdentifier=lambda: 321,
                              isTerminated=lambda: False)
    def apps(bundle_id):
        calls.append("apps")
        assert bundle_id == "org.test.Canvas"
        return [process]
    def windows(options, null_window):
        calls.append("windows")
        assert options == 3 and null_window == 0
        return [{"number": 456, "pid": 321, "title": "Scene", "layer": 0, "onscreen": True,
                 "alpha": 1.0, "bounds": {"X": 20.0, "Y": 10.0, "Width": 900.0, "Height": 700.0}}]
    # Bypass construction only for this inert test. Production construction has
    # its own VM guard before loading either native framework.
    backend = object.__new__(V.Backend)
    backend.AppKit = SimpleNamespace(NSRunningApplication=SimpleNamespace(
        runningApplicationsWithBundleIdentifier_=apps))
    backend.Quartz = SimpleNamespace(CGSessionCopyCurrentDictionary=session,
                                    kCGSessionOnConsoleKey="kCGSSessionOnConsoleKey",
                                    kCGSessionUserIDKey="kCGSSessionUserIDKey",
                                    CGPreflightScreenCaptureAccess=preflight,
                                    CGWindowListCopyWindowInfo=windows,
                                    kCGWindowListOptionOnScreenOnly=1,
                                    kCGWindowListExcludeDesktopElements=2, kCGNullWindowID=0,
                                    kCGWindowNumber="number", kCGWindowOwnerPID="pid",
                                    kCGWindowName="title", kCGWindowLayer="layer",
                                    kCGWindowIsOnscreen="onscreen", kCGWindowAlpha="alpha",
                                    kCGWindowBounds="bounds")
    return backend, calls


@pytest.mark.parametrize("state", ["locked", "permission", "off_console"])
def test_native_boundary_stops_before_window_enumeration_without_console_or_grant(tmp_path, state):
    backend, calls = native_backend(tmp_path / "Unused.app", locked=state == "locked",
                                    permission=state != "permission", console=state != "off_console")
    with pytest.raises(V.Unavailable):
        backend.inspect("org.test.Canvas")
    assert calls == (["session", "preflight"] if state == "permission" else ["session"])


def test_native_boundary_normalizes_process_and_quartz_metadata_without_ax(tmp_path):
    app = tmp_path / "Native.app"
    app.mkdir()
    backend, calls = native_backend(app)
    snapshot = backend.inspect("org.test.Canvas")
    identity = V._identity(snapshot, app.resolve(), "org.test.Canvas", "Scene")
    assert calls == ["session", "preflight", "apps", "windows"]
    assert identity["process"]["bundle"] == str(app)
    assert identity["process"]["launch_time"] == 12345.25
    assert identity["window"]["id"] == 456


def test_native_bridge_number_subclasses_become_builtins_before_strict_identity(tmp_path):
    class BridgedInt(int):
        pass
    class BridgedFloat(float):
        pass
    app = tmp_path / "Native.app"
    app.mkdir()
    backend, _ = native_backend(app)
    native_windows = backend.Quartz.CGWindowListCopyWindowInfo
    def windows(options, null_window):
        rows = native_windows(options, null_window)
        for row in rows:
            for key in ("number", "pid", "layer"):
                row[key] = BridgedInt(row[key])
            row["alpha"] = BridgedFloat(row["alpha"])
            row["bounds"] = {key: BridgedFloat(value) for key, value in row["bounds"].items()}
        return rows
    backend.Quartz.CGWindowListCopyWindowInfo = windows
    snapshot = backend.inspect("org.test.Canvas")
    window = snapshot["windows"][0]
    assert all(type(window[k]) is int for k in ("id", "pid", "layer"))
    assert type(window["alpha"]) is float and all(type(v) is float for v in window["bounds"].values())
    identity = V._identity(snapshot, app.resolve(), "org.test.Canvas", "Scene")
    assert identity["window"]["id"] == 456


@pytest.mark.parametrize("field", ["number", "pid", "layer", "alpha", "bounds"])
@pytest.mark.parametrize("value", [None, "456", True, float("nan"), float("inf")])
def test_native_bridge_invalid_numbers_refuse_instead_of_becoming_valid_metadata(tmp_path, field, value):
    backend, _ = native_backend(tmp_path / "Unused.app")
    native_windows = backend.Quartz.CGWindowListCopyWindowInfo
    def windows(options, null_window):
        rows = native_windows(options, null_window)
        if field == "bounds":
            rows[0]["bounds"]["Width"] = value
        else:
            rows[0][field] = value
        return rows
    backend.Quartz.CGWindowListCopyWindowInfo = windows
    with pytest.raises(V.Unavailable, match="numeric"):
        backend.inspect("org.test.Canvas")


@pytest.mark.parametrize("field", ["number", "pid", "layer"])
def test_native_bridge_fractional_integer_fields_refuse(tmp_path, field):
    backend, _ = native_backend(tmp_path / "Unused.app")
    native_windows = backend.Quartz.CGWindowListCopyWindowInfo
    def windows(options, null_window):
        rows = native_windows(options, null_window)
        rows[0][field] = 456.5
        return rows
    backend.Quartz.CGWindowListCopyWindowInfo = windows
    with pytest.raises(V.Unavailable, match="numeric"):
        backend.inspect("org.test.Canvas")


@pytest.mark.parametrize("console_key,uid_key", [
    ("kCGSSessionOnConsoleKey", "kCGSSessionUserIDKey"),
    ("test_console_dictionary_key", "test_user_dictionary_key"),
])
def test_session_lookup_uses_quartz_constant_values_instead_of_python_names(tmp_path, console_key, uid_key):
    backend, _ = native_backend(tmp_path / "Unused.app")
    backend.Quartz.kCGSessionOnConsoleKey = console_key
    backend.Quartz.kCGSessionUserIDKey = uid_key
    backend.Quartz.CGSessionCopyCurrentDictionary = lambda: {
        "CGSSessionScreenIsLocked": False, console_key: True, uid_key: os.getuid(),
        # The Python constant names are deliberately misleading dictionary keys.
        "kCGSessionOnConsoleKey": False, "kCGSessionUserIDKey": -1}
    snapshot = backend.inspect("org.test.Canvas")
    assert snapshot["session"] == {"uid": os.getuid(), "on_console": True}


def test_native_backend_guard_precedes_framework_import(monkeypatch):
    def refuse():
        raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    with pytest.raises(PermissionError, match="VM only"):
        V.Backend()


def effect_policy(app, *, params=None):
    from anyuse.execution import rea_visual_effect as X
    return X.validate_policy({"format": X.POLICY, "id": "visible-edit", "target": R.fingerprint(app),
        "kind": "visible_state_transition", "params": params or [], "observer": {"window_title": "Scene"},
        "before": "The document is empty.", "after": "The requested value from inputs is visibly present.",
        "minimum_confidence": .8, "model": {"provider": "local", "base_url": "http://127.0.0.1:11434/v1",
            "model": "test-visual", "api_key_env": "", "retries": 0, "timeout": 30, "max_tokens": 512}})


@pytest.fixture
def effect(visual, monkeypatch, tmp_path):
    from anyuse.execution import rea_visual_effect as X, rea_runtime_bridge as B
    from anyuse.execution import rea_process_restart as RR, model_adapter as M, rea_inspector_bridge as I
    import httpx
    app, backend, out = visual
    backend.state["frontmost_pid"] = 321
    identity = {"bundle": str(app), "bundle_id": "org.test.Canvas", "executable": str(app / "Contents/MacOS/Canvas"),
                "pid": 321, "launch_time": 12345.25, "uid": os.getuid()}
    monkeypatch.setattr(B, "process_identity", lambda *a, **kw: copy.deepcopy(identity))
    monkeypatch.setattr(X.F, "require_clear", lambda where: SimpleNamespace(allowed=True))
    source = tmp_path / "effect-producer.py"; source.write_text("fake producer for an isolated orchestration test")
    def sources(reader):
        pin = X._pin(source); reader.read(pin, "source", retain=False); return [pin]
    monkeypatch.setattr(X, "_sources", sources)
    kernel = {"pid": 321, "ppid": 1, "uid": os.getuid(), "executable": identity["executable"], "started_at": "Thu Jan  1 03:25:45 1970"}
    def psquery(pid, folder, deadline):
        folder.mkdir(mode=0o700); value = {"test_kernel": True}; pin = X._save(folder / "receipt.json", value)
        return value, pin
    monkeypatch.setattr(RR, "_ps_query", psquery)
    monkeypatch.setattr(RR, "_kernel", lambda *a, **kw: copy.deepcopy(kernel))
    monkeypatch.setattr(RR, "_bind_epoch", lambda *a: None)
    resources = {"provider": "inspector", "adapter": {},
                 "transport": {"config": {"file": str(tmp_path / "fake-transport.json")}}, "resources": {}}
    monkeypatch.setattr(X, "_provider_resources", lambda *a: copy.deepcopy(resources))
    calls, verdicts, requests = [], ["success", "success"], []
    def invoke(bundle, actor, folder, **kwargs):
        assert kwargs.get("expected_identity") == identity
        folder = Path(folder); folder.mkdir(mode=0o700); calls.append(copy.deepcopy(actor))
        receipt = {"status": "executed", "identity": identity, "after_identity": identity,
                   "expected_identity": copy.deepcopy(kwargs["expected_identity"]),
                   "resource_review_required": False, "test_actor": actor}
        X._save(folder / "inspector.json", receipt); return receipt
    monkeypatch.setattr(I, "invoke", invoke)
    runtime_consumer = X._runtime
    def runtime(reader, pin, actor, got_identity, target, got_resources):
        receipt = reader.json(pin, "trial")
        assert receipt["test_actor"] == actor and got_identity == identity
        if receipt.get("resource_review_required"):
            raise R.ResourceReviewRequired("uncertain test runtime")
        return {"provider": "inspector", "invocations": 1, "native_calls": 0,
                "getter_calls": 0, "method_dispatch_attempts": 1}
    monkeypatch.setattr(X, "_runtime", runtime)
    def response(request):
        body = json.loads(request.content); requests.append(body)
        result = verdicts.pop(0)
        verdict = {"result": result, "is_success": result == "success", "confidence": .95,
                   "explanation": "Observed the declared visible state.", "evidence": ["The requested text is visible."]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdict)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
    adapter = M.ModelAdapter(M.ModelConfig(**effect_policy(app)["model"]), transport=httpx.MockTransport(response), budget=M.RunBudget(0))
    declaration = tmp_path / "inspector-declaration.json"; declaration.write_text("{}")
    actor = {"kind": "inspector_method", "receiver": {"root": {"kind": "global", "name": "fixture"}, "steps": []},
             "method": "applyValue", "function_sha256": "a" * 64,
             "arguments": [], "return_type": "void", "signature_evidence": {
                 "kind": "reviewed_js", "file": str(declaration), "sha256": R._file_hash(declaration), "pointer": "/contract"}}
    return SimpleNamespace(X=X, app=app, backend=backend, out=out, identity=identity, actor=actor, adapter=adapter,
                           calls=calls, verdicts=verdicts, requests=requests, source=source, invoke=invoke,
                           runtime_consumer=runtime_consumer)


def alpha_png(*, point=None, mode="RGBA", metadata=False):
    import io
    from PIL import Image, PngImagePlugin
    image = Image.new(mode, (1024, 768), (201, 92, 77, 0) if mode == "RGBA" else (201, 92, 77))
    if point is not None: image.putpixel(point[:2], (201, 92, 77, point[2]))
    output = io.BytesIO(); info = PngImagePlugin.PngInfo()
    if metadata: info.add_text("private", "OTHER_APPLICATION_RGB_PAYLOAD")
    image.save(output, format="PNG", pnginfo=info); image.close(); return output.getvalue()


def alpha_overlay(effect, monkeypatch, *, source=None, count=1, mutate=None):
    source = alpha_png(point=(0, 0, 1), metadata=True) if source is None else source
    rows = [{"id": 999 + i, "pid": 111 + i, "title": "OTHER_APPLICATION_RGB_PAYLOAD", "layer": 20 + i,
             "on_screen": True, "alpha": 1.0, "bounds": {"X": 0., "Y": 0., "Width": 1024., "Height": 768.}}
            for i in range(count)]
    effect.backend.state["windows"][:0] = rows
    capture = effect.backend.capture
    def run(argv, *, timeout=V.CAPTURE_TIMEOUT):
        if argv[4] in {str(row["id"]) for row in rows}:
            assert 0 < timeout <= effect.X.ALPHA_SECONDS
            effect.backend.calls.append(copy.deepcopy(argv)); Path(argv[-1]).write_bytes(source() if callable(source) else source)
            if mutate: mutate()
            return SimpleNamespace(returncode=0, stdout=b"not retained", stderr=b"not retained")
        return capture(argv, timeout=timeout)
    monkeypatch.setattr(effect.backend, "capture", run)
    return rows


def test_visual_alpha_transparent_fullscreen_retains_only_exact_lossless_alpha(effect, monkeypatch):
    import io
    from PIL import Image
    alpha_overlay(effect, monkeypatch)
    p = effect_policy(effect.app)
    intent = effect.X.make_intent(str(effect.app), p, "transparent-overlay", "learning", {})
    assert effect.X.validate_intent(p, intent) == intent
    retained = intent["context"]["inventory"]["occlusion_alpha"][0]
    encoded = base64.b64decode(retained["mask"]["base64"])
    with Image.open(io.BytesIO(encoded)) as mask:
        assert mask.mode == "LA" and mask.size == (1024, 768)
        assert mask.getchannel("L").getextrema() == (0, 0)
        assert mask.getpixel((0, 0)) == (0, 1)  # Nonzero OUTSIDE the target is retained, not rounded away.
    assert b"tEXt" not in encoded and "OTHER_APPLICATION_RGB_PAYLOAD" not in json.dumps(intent)
    assert all(not Path(argv[-1]).exists() for argv in effect.backend.calls)
    assert not effect.requests and not effect.calls


def test_visual_alpha_complete_trial_revalidates_masks_before_actor_and_after_image(effect, monkeypatch):
    alpha_overlay(effect, monkeypatch)
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "alpha-trial", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "model_judged", result
    assert result["model_calls"] == 2 and len(effect.calls) == 1
    assert effect.X.validate_trial(p, result["case"], effect.actor) == result["measurement"]
    for role in ("before", "after"):
        wrapper = json.loads((effect.out / role / "capture.json").read_text())
        assert wrapper["context_before"]["inventory"]["occlusion_alpha"]
        assert wrapper["context_after"]["inventory"]["occlusion_alpha"]


@pytest.mark.parametrize("opacity", [1, 255])
def test_visual_alpha_any_nonzero_overlap_refuses_without_model_or_actor(effect, monkeypatch, opacity):
    alpha_overlay(effect, monkeypatch, source=alpha_png(point=(20, 10, opacity)))
    with pytest.raises(ValueError, match="nonzero alpha"):
        effect.X.make_intent(str(effect.app), effect_policy(effect.app), "actual-overlay", "learning", {})
    assert not effect.requests and not effect.calls


def test_visual_alpha_partial_edge_pixel_rounds_outward(effect, monkeypatch):
    effect.backend.state["windows"][0]["bounds"]["X"] = 20.25
    alpha_overlay(effect, monkeypatch, source=alpha_png(point=(20, 10, 1)))
    with pytest.raises(ValueError, match="nonzero alpha"):
        effect.X.make_intent(str(effect.app), effect_policy(effect.app), "partial-overlay", "learning", {})
    assert not effect.calls


@pytest.mark.parametrize("bad", ["RGB", "16bit", "crc", "no_end", "trailing", "animation", "late_animation", "unknown_critical"])
def test_visual_alpha_unknown_precision_or_malformed_source_never_becomes_transparent(effect, monkeypatch, bad):
    import io, struct, zlib
    from PIL import Image
    source = alpha_png(mode="RGB" if bad == "RGB" else "RGBA")
    if bad == "16bit":
        def chunk(kind, data): return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        source = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 16, 6, 0, 0, 0))
        source += chunk(b"IDAT", zlib.compress(b"\0" + b"\0" * 7 + b"\1")) + chunk(b"IEND", b"")
    elif bad == "crc":
        position = source.index(b"IDAT"); size = struct.unpack(">I", source[position - 4:position])[0]
        changed = bytearray(source); changed[position + 4 + size] ^= 1; source = bytes(changed)
    elif bad == "no_end": source = source[:-12]
    elif bad == "trailing": source += b"untrusted trailing bytes"
    elif bad == "animation":
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0)); second = Image.new("RGBA", (16, 16), (1, 0, 0, 1))
        out = io.BytesIO(); image.save(out, format="PNG", save_all=True, append_images=[second]); source = out.getvalue()
    elif bad in {"late_animation", "unknown_critical"}:
        kind = b"acTL" if bad == "late_animation" else b"ABCD"
        data = struct.pack(">II", 1, 0) if bad == "late_animation" else b""
        extra = struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        source = source[:-12] + extra + source[-12:]
    alpha_overlay(effect, monkeypatch, source=source)
    with pytest.raises((ValueError, OSError)):
        effect.X.make_intent(str(effect.app), effect_policy(effect.app), "unsupported-alpha", "learning", {})
    assert not effect.requests and not effect.calls


@pytest.mark.parametrize("changed", ["id", "geometry", "scale", "missing", "duplicate", "source", "time", "mask", "luminance"])
def test_visual_alpha_retained_evidence_is_redecoded_and_exactly_bound(effect, monkeypatch, changed):
    import io, hashlib
    from PIL import Image
    alpha_overlay(effect, monkeypatch)
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "bound-alpha", "learning", {})
    rows = intent["context"]["inventory"]["occlusion_alpha"]; row = rows[0]
    if changed == "id": row["window"]["id"] += 1
    elif changed == "geometry": row["window"]["bounds"]["Y"] += 1
    elif changed == "scale": row["pixel_scale"] = 2.
    elif changed == "missing": rows.clear()
    elif changed == "duplicate": rows.append(copy.deepcopy(row))
    elif changed == "source": row["sources"]["visual_effect"]["sha256"] = "0" * 64
    elif changed == "time": row["finished_monotonic"] = intent["observed_monotonic"] + 1
    else:
        image = Image.new("LA", (1024, 768), (0, 0))
        image.putpixel((20, 10), (0, 1) if changed == "mask" else (1, 0))
        out = io.BytesIO(); image.save(out, format="PNG"); encoded = out.getvalue()
        row["mask"] = {"base64": base64.b64encode(encoded).decode(), "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    with pytest.raises(ValueError): effect.X.validate_intent(p, intent)
    assert not effect.requests and not effect.calls


@pytest.mark.parametrize("limit", ["windows", "pixels", "aggregate_pixels", "bytes"])
def test_visual_alpha_capture_and_aggregate_limits_refuse(effect, monkeypatch, limit):
    alpha_overlay(effect, monkeypatch, count=9 if limit == "windows" else 2 if limit == "aggregate_pixels" else 1)
    if limit == "pixels": monkeypatch.setattr(effect.X, "MAX_ALPHA_PIXELS", 10)
    elif limit == "aggregate_pixels": monkeypatch.setattr(effect.X, "MAX_ALPHA_TOTAL_PIXELS", 1024 * 768)
    elif limit == "bytes": monkeypatch.setattr(effect.X, "MAX_ALPHA_TOTAL_BYTES", 45)
    with pytest.raises(ValueError):
        effect.X.make_intent(str(effect.app), effect_policy(effect.app), "bounded-alpha", "learning", {})
    if limit == "windows": assert not effect.backend.calls
    assert not effect.requests and not effect.calls


def test_visual_alpha_stack_drift_and_unknown_window_metadata_do_not_admit(effect, monkeypatch):
    alpha_overlay(effect, monkeypatch, mutate=lambda: effect.backend.state["windows"][0]["bounds"].update(Y=1.))
    with pytest.raises(ValueError, match="stack/epoch/source"):
        effect.X.make_intent(str(effect.app), effect_policy(effect.app), "drifted-alpha", "learning", {})
    assert not effect.requests and not effect.calls


@pytest.mark.parametrize("overlap", [False, True])
def test_visual_alpha_bookends_ignore_only_fully_known_disjoint_window_changes(effect, monkeypatch, overlap):
    def insert_window():
        if any(row["id"] == 888 for row in effect.backend.state["windows"]): return
        effect.backend.state["windows"].insert(0, {"id": 888, "pid": 222, "title": "not retained", "layer": 2147483630,
            "on_screen": True, "alpha": 1., "bounds": {"X": 30. if overlap else 990., "Y": 20. if overlap else 0.,
                                                       "Width": 28., "Height": 5.}})
    alpha_overlay(effect, monkeypatch, mutate=insert_window)
    policy = effect_policy(effect.app)
    if overlap:
        with pytest.raises(ValueError, match="stack/epoch/source"):
            effect.X.make_intent(str(effect.app), policy, "new-relevant-overlay", "learning", {})
    else:
        intent = effect.X.make_intent(str(effect.app), policy, "new-disjoint-window", "learning", {})
        assert effect.X.validate_intent(policy, intent) == intent
        masks = intent["context"]["inventory"]["occlusion_alpha"]
        assert len(masks) == 1 and all(row["id"] != 888 for row in masks[0]["after"]["above_windows"])
    assert not effect.requests and not effect.calls


@pytest.mark.parametrize("field", ["alpha", "layer", "on_screen"])
def test_visual_alpha_unknown_above_metadata_refuses_without_capture(effect, monkeypatch, field):
    alpha_overlay(effect, monkeypatch); effect.backend.state["windows"][0][field] = None
    with pytest.raises(ValueError, match="unknown above-window"):
        effect.X.make_intent(str(effect.app), effect_policy(effect.app), "unknown-alpha", "learning", {})
    assert not effect.backend.calls and not effect.requests and not effect.calls


def test_visual_alpha_is_freshly_measured_after_before_judge_before_actor(effect, monkeypatch):
    holder = [alpha_png()]; alpha_overlay(effect, monkeypatch, source=lambda: holder[0])
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "fresh-alpha", "learning", {})
    judge = effect.X._judge
    def changed(*args, **kwargs):
        value = judge(*args, **kwargs)
        if args[2] == "before": holder[0] = alpha_png(point=(20, 10, 1))
        return value
    monkeypatch.setattr(effect.X, "_judge", changed)
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "refused" and result["model_calls"] == 1 and not effect.calls
    assert "nonzero alpha" in result["error"]["message"]


def test_visual_unoccluded_context_does_not_capture_other_windows(effect):
    effect.X.make_intent(str(effect.app), effect_policy(effect.app), "no-overlap", "learning", {})
    assert not effect.backend.calls and not effect.requests and not effect.calls


def test_visual_effect_accepts_zero_and_parameterized_contracts_without_app_tables(effect):
    p = effect_policy(effect.app)
    assert effect.X._inputs(p, {}) == {}
    p = effect_policy(effect.app, params=[{"name": "value", "type": "string", "source": "text"}])
    actor = copy.deepcopy(effect.actor); actor["arguments"] = [{"type": "number", "value": {"input": "value"}}]
    assert effect.X.bind_actor(actor, p, {"value": "42.5"})["arguments"] == [{"type": "number", "value": 42.5}]
    with pytest.raises(ValueError): effect.X.bind_actor(actor, p, {"other": "42"})


@pytest.mark.parametrize("change", ["nan", "bool_confidence", "bool_text", "inconsistent", "unknown_result", "extra", "empty_evidence"])
def test_visual_effect_model_judgments_are_strict_not_truthy(change):
    from anyuse.execution import rea_visual_effect as X
    value = {"result": "success", "is_success": True, "confidence": .9, "explanation": "Visible.", "evidence": ["Text."]}
    if change == "nan": value["confidence"] = float("nan")
    elif change == "bool_confidence": value["confidence"] = True
    elif change == "bool_text": value["is_success"] = "false"
    elif change == "inconsistent": value["result"] = "failed"
    elif change == "unknown_result": value["result"] = "verified"
    elif change == "extra": value["override_native_failure"] = True
    else: value["evidence"] = []
    with pytest.raises(ValueError): X.validate_judgment(value)


@pytest.mark.parametrize("change", ["default_provider", "retry", "credential_url", "bad_threshold", "same_effect", "unknown_observer"])
def test_visual_effect_requires_explicit_bounded_model_and_effect_policy(effect, change):
    p = effect_policy(effect.app)
    if change == "default_provider": del p["model"]["provider"]
    elif change == "retry": p["model"]["retries"] = 1
    elif change == "credential_url": p["model"]["base_url"] = "http://secret@127.0.0.1/v1"
    elif change == "bad_threshold": p["minimum_confidence"] = True
    elif change == "same_effect": p["after"] = p["before"]
    else: p["observer"]["callback"] = "do-anything"
    with pytest.raises(ValueError): effect.X.validate_policy(p)


def test_visual_effect_run_retains_two_real_mocked_calls_and_model_judged_tier(effect):
    from anyuse.execution import model_adapter as M
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "new-effect", "learning", {})
    count = M.process_call_count()
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == result["verification_tier"] == "model_judged", result
    assert result["proof_created"] is False and result["model_calls"] == M.process_call_count() - count == 2
    assert len(effect.calls) == 1 and len(effect.requests) == 2
    assert result["measurement"]["kernel"]["pid"] == 321
    assert result["measurement"]["verification_tier"] == "model_judged"
    assert result["measurement"]["deterministic_proof_created"] is False
    assert effect.X.validate_trial(p, result["case"], effect.actor) == result["measurement"]
    assert not (effect.out / "proof.json").exists()


def test_visual_effect_explicit_layer_is_bound_through_trial_and_reopening(effect):
    effect.backend.state["windows"][0]["layer"] = 8
    policy = effect_policy(effect.app)
    with pytest.raises(ValueError, match="owned visual window"):
        effect.X.make_intent(str(effect.app), policy, "default-layer", "learning", {})
    policy["observer"]["window_layer"] = 8
    policy = effect.X.validate_policy(policy)
    intent = effect.X.make_intent(str(effect.app), policy, "declared-layer", "learning", {})
    assert intent["context"]["window"]["layer"] == 8
    result = effect.X.run_trial(str(effect.app), effect.actor, policy, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "model_judged" and result["model_calls"] == 2 and len(effect.calls) == 1
    assert effect.X.validate_trial(policy, result["case"], effect.actor) == result["measurement"]
    changed = copy.deepcopy(policy)
    changed["observer"]["window_layer"] = 0
    with pytest.raises(ValueError):
        effect.X.validate_intent(changed, intent)


def test_visual_effect_precondition_refusal_stops_before_actor(effect):
    effect.verdicts[:] = ["uncertain"]
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "bad-before", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "refused" and result["model_calls"] == 1 and not effect.calls
    assert (effect.out / "precondition/model-input.json").is_file()
    assert json.loads((effect.out / "precondition/judgment.json").read_text())["status"] == "refused"


def test_visual_effect_json_object_compatibility_still_supplies_exact_response_schema(effect):
    from anyuse.execution import model_adapter as M
    p = effect_policy(effect.app); p["model"]["structured_output_mode"] = "json_object"
    adapter = M.ModelAdapter(M.ModelConfig(**p["model"]), transport=effect.adapter._transport, budget=M.RunBudget(0))
    intent = effect.X.make_intent(str(effect.app), p, "json-object", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=adapter)
    assert result["status"] == "model_judged", result
    assert all(row["response_format"] == {"type": "json_object"} for row in effect.requests)
    for row in effect.requests:
        content = row["messages"][-1]["content"]
        text = content if isinstance(content, str) else next(item["text"] for item in content if item["type"] == "text")
        assert json.loads(text)["response_schema"] == effect.X.SCHEMA


def test_visual_effect_native_dispatch_preserves_configured_transport_without_override(effect, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    actor = {"kind": "objc_method", "receiver": {"kind": "class", "class": "Fixture"},
             "selector": "perform:", "signature": "v24@0:8@16", "arguments": [{"type": "nil", "value": None}], "return_type": "void"}
    options = []
    def collect(bundle, folder, request, **kwargs):
        options.append(kwargs); folder = Path(folder); folder.mkdir(mode=0o700)
        receipt = {"test_actor": actor, "resource_review_required": False}
        effect.X._save(folder / "runtime.json", receipt); return receipt
    monkeypatch.setattr(B, "collect", collect)
    def runtime(reader, pin, bound, identity, target, resources):
        assert reader.json(pin, "trial")["test_actor"] == bound == actor
        return {"provider": "native", "invocations": 1, "native_calls": 1, "getter_calls": 0, "method_dispatch_attempts": 1}
    monkeypatch.setattr(effect.X, "_runtime", runtime)
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "configured-transport", "learning", {})
    result = effect.X.run_trial(str(effect.app), actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "model_judged", result
    assert len(options) == 1 and "backend" not in options[0] and options[0]["expected_identity"] == effect.identity


def test_visual_effect_native_uncertainty_is_not_overridden_by_model(effect, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I
    def failed(*args, **kwargs):
        receipt = effect.invoke(*args, **kwargs); receipt["resource_review_required"] = True
        effect.X._save(Path(args[2]) / "inspector.json", receipt); return receipt
    monkeypatch.setattr(I, "invoke", failed)
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "uncertain-native", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "refused" and result["resource_review_required"] is True
    assert len(effect.calls) == len(effect.requests) == 1 and result["model_calls"] == 1
    assert not (effect.out / "after").exists()


def test_visual_effect_changed_process_during_model_refuses_before_dispatch(effect, monkeypatch):
    original = effect.X._judge
    def changed(*args, **kwargs):
        result = original(*args, **kwargs); effect.identity["launch_time"] += 1; return result
    monkeypatch.setattr(effect.X, "_judge", changed)
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "changed-epoch", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "refused" and result["model_calls"] == 1 and not effect.calls


def test_visual_effect_inspector_handoff_pins_epoch_before_provider_connection(effect, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I
    admitted = copy.deepcopy(effect.identity); provider_attempts = []
    def changed_before_connection(bundle, actor, folder, *, expected_identity, **kwargs):
        provider_attempts.append(copy.deepcopy(expected_identity))
        assert expected_identity == admitted
        effect.identity["launch_time"] += 1
        if effect.identity != expected_identity:
            raise I.Unavailable("owned inspector process epoch changed before connection")
        return effect.invoke(bundle, actor, folder, expected_identity=expected_identity, **kwargs)
    monkeypatch.setattr(I, "invoke", changed_before_connection)
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "inspector-handoff", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "refused" and len(provider_attempts) == 1
    assert not effect.calls and result["model_calls"] == 1 and len(effect.requests) == 1
    assert not (effect.out / "after").exists()


def test_visual_effect_inspector_consumer_rejects_receipt_without_preconnection_epoch(effect, monkeypatch, tmp_path):
    from anyuse.execution import rea_inspector_bridge as I
    monkeypatch.setattr(I, "receipt_valid", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unbound receipt must stop first")))
    receipt = {"resource_review_required": False, "error": None, "identity": effect.identity,
               "after_identity": effect.identity, "target": R.fingerprint(effect.app), "model_calls": 0}
    file = tmp_path / "unbound-inspector.json"; pin = effect.X._save(file, receipt)
    with pytest.raises(ValueError, match="expected process epoch"):
        effect.runtime_consumer(effect.X._Reader(), pin, effect.actor, effect.identity, receipt["target"], {})


def test_visual_effect_reserves_after_judgment_budget_before_actor(effect):
    p = effect_policy(effect.app)
    intent = effect.X.make_intent(str(effect.app), p, "short-budget", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), timeout=30, adapter=effect.adapter)
    assert result["status"] == "refused" and "reserved after capture" in result["error"]["message"]
    assert result["actuated"] is False and result["resource_review_required"] is False
    assert result["model_calls"] == 0 and not effect.calls and len(effect.requests) == 0
    journal = json.loads((effect.out / "journal.json").read_text())
    assert journal["dispatch_started_monotonic"] is None


def test_visual_effect_idempotent_setter_judges_postcondition_with_identical_images(effect):
    p = effect_policy(effect.app)
    p["before"] = "The document is visible and may already have the requested setting."
    p["after"] = "The requested setting is visible. This setter is idempotent and may already be satisfied."
    intent = effect.X.make_intent(str(effect.app), p, "idempotent-setter", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "model_judged", result
    request = effect.requests[-1]; system = request["messages"][0]["content"]
    content = request["messages"][-1]["content"]
    text = next(item["text"] for item in content if item["type"] == "text")
    state = json.loads(text)
    images = [item["image_url"]["url"] for item in content if item["type"] == "image_url"]
    assert len(images) == 2 and images[0] == images[1]
    assert state["expected_after"] == p["after"] and "postcondition" in state["question"]
    assert "idempotent" in system and "Identical screenshots may be correct" in system
    assert "call completion alone" in state["question"]


def test_visual_effect_tampered_model_or_image_evidence_cannot_revalidate(effect):
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "tampered", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "model_judged", result
    image = effect.out / "before/window.png"; image.write_bytes(image.read_bytes() + b"changed")
    with pytest.raises(ValueError): effect.X.validate_trial(p, result["case"], effect.actor)


def test_visual_effect_unvalidated_dispatch_retains_unknown_liveness(effect, monkeypatch):
    monkeypatch.setattr(effect.X, "_runtime", lambda *a, **kw: (_ for _ in ()).throw(ValueError("malformed cleanup receipt")))
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "bad-cleanup", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "refused" and result["actuated"] is None
    assert result["resource_review_required"] is True and result["model_calls"] == 1
    assert len(effect.calls) == 1 and not (effect.out / "after").exists()


def test_visual_effect_negative_after_judgment_preserves_known_actor_and_cleanup(effect):
    effect.verdicts[:] = ["success", "failed"]
    p = effect_policy(effect.app); intent = effect.X.make_intent(str(effect.app), p, "bad-after", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, p, intent, str(effect.out), adapter=effect.adapter)
    assert result["status"] == "refused" and result["actuated"] is True
    assert result["resource_review_required"] is False and result["model_calls"] == 2
    assert len(effect.calls) == 1 and (effect.out / "judgment/judgment.json").exists()


def test_visual_effect_occlusion_and_known_window_mismatch_stop_without_model(effect, monkeypatch):
    p = effect_policy(effect.app)
    alpha_overlay(effect, monkeypatch, source=alpha_png(point=(20, 10, 255)))
    with pytest.raises(ValueError, match="nonzero alpha"):
        effect.X.make_intent(str(effect.app), p, "covered", "learning", {})
    assert not effect.requests and not effect.calls and not effect.out.exists()


def test_visual_effect_source_closure_includes_campaign_and_delegated_restart_consumer(tmp_path, monkeypatch):
    from anyuse.execution import rea_visual_effect as X, rea_visual_campaign as C, rea_state_campaign as C0
    current = tmp_path / "visual-campaign.py"; current.write_text("fixed visual campaign consumer")
    delegated = tmp_path / "state-campaign.py"; delegated.write_text("fixed delegated restart consumer")
    monkeypatch.setattr(C, "__file__", str(current))
    monkeypatch.setattr(C0, "__file__", str(delegated))
    reader = X._Reader(); sources = X._sources(reader)
    pins = {row["file"]: row for row in sources}
    assert str(current) in pins and str(delegated) in pins
    delegated.write_text("changed delegated restart consumer")
    with pytest.raises(ValueError, match="differ|exact size"):
        reader.read(pins[str(delegated)], "source", retain=False)


def test_visual_effect_argument_domains_are_optional_literal_and_inclusive(effect):
    p = effect_policy(effect.app)
    assert p["input_constraints"] == {} and effect.X.validate_inputs(p, {}) == {}
    p = effect_policy(effect.app, params=[{"name": "value", "type": "string", "source": "text"}])
    p["input_constraints"] = {"value": {"kind": "decimal", "minimum": "0.75", "maximum": "1.50"}}
    for value in ("0.75", "1.50", "1.0", "1e0"):
        assert effect.X.validate_inputs(p, {"value": value}) == {"value": value}
    p["input_constraints"] = {"value": {"kind": "enum", "values": ["true", "false"]}}
    assert effect.X.validate_inputs(p, {"value": "false"}) == {"value": "false"}
    with pytest.raises(ValueError, match="enum"):
        effect.X.validate_inputs(p, {"value": "False"})


@pytest.mark.parametrize("constraint", [
    {"kind": "enum", "values": []},
    {"kind": "enum", "values": ["true", "true"]},
    {"kind": "enum", "values": [True]},
    {"kind": "enum", "values": [""]},
    {"kind": "enum", "values": [str(i) for i in range(65)]},
    {"kind": "enum", "values": ["true"], "unknown": True},
    {"kind": "decimal", "minimum": "NaN", "maximum": "1.50"},
    {"kind": "decimal", "minimum": ".75", "maximum": "1.50"},
    {"kind": "decimal", "minimum": .75, "maximum": "1.50"},
    {"kind": "decimal", "minimum": "1.50", "maximum": "0.75"},
    {"kind": "decimal", "minimum": "0.75", "maximum": "Infinity"},
    {"kind": "decimal", "minimum": "0.75", "maximum": "1.50", "inclusive": True},
    {"kind": "regex", "expression": ".*"},
])
def test_visual_effect_malformed_argument_constraints_are_refused(effect, constraint):
    p = effect_policy(effect.app, params=[{"name": "value", "type": "string", "source": "text"}])
    p["input_constraints"] = {"value": constraint}
    with pytest.raises(ValueError): effect.X.validate_policy(p)


def test_visual_effect_constraints_cannot_name_unknown_parameters(effect):
    p = effect_policy(effect.app)
    p["input_constraints"] = {"undeclared": {"kind": "enum", "values": ["true"]}}
    with pytest.raises(ValueError, match="declared"):
        effect.X.validate_policy(p)


@pytest.mark.parametrize("constraint,value", [
    ({"kind": "enum", "values": ["true", "false"]}, "yes"),
    ({"kind": "decimal", "minimum": "0.75", "maximum": "1.50"}, "0.7499999999999999999999"),
    ({"kind": "decimal", "minimum": "0.75", "maximum": "1.50"}, "1.5000000000000000000001"),
    ({"kind": "decimal", "minimum": "0.75", "maximum": "1.50"}, "NaN"),
])
def test_visual_effect_out_of_domain_stops_before_process_model_or_actor(effect, monkeypatch, constraint, value):
    from anyuse.execution import rea_runtime_bridge as B
    p = effect_policy(effect.app, params=[{"name": "value", "type": "string", "source": "text"}])
    p["input_constraints"] = {"value": constraint}
    monkeypatch.setattr(B, "process_identity", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("process access must not occur")))
    with pytest.raises(ValueError):
        effect.X.make_intent(str(effect.app), p, "outside-domain", "learning", {"value": value})
    assert not effect.requests and not effect.calls and not effect.backend.calls and not effect.out.exists()



def test_visual_trial_shares_production_budget_between_judgments(effect, monkeypatch):
    from anyuse.execution import model_adapter as M
    import httpx
    policy = effect_policy(effect.app)
    policy["model"].update(price_in=1., price_out=1.)
    monkeypatch.setenv("ANYUSE_RUN_BUDGET_USD", ".025")
    budgets = []
    original = M.ModelAdapter.__init__
    def response(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "result": "success", "is_success": True, "confidence": .95,
            "explanation": "Inert fixture state", "evidence": ["Fixture"]})}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 24000, "completion_tokens": 512}})
    def init(self, config, **kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        original(self, config, **kwargs)
        budgets.append(self.budget)
    monkeypatch.setattr(M.ModelAdapter, "__init__", init)
    intent = effect.X.make_intent(str(effect.app), policy, "shared-budget", "learning", {})
    result = effect.X.run_trial(str(effect.app), effect.actor, policy, intent, str(effect.out))
    assert len({id(b) for b in budgets}) == 1
    assert result["status"] == "refused" and result["model_calls"] == 1


def test_hosted_visual_policy_never_assumes_missing_prices_are_free(effect):
    policy = effect_policy(effect.app)
    policy["model"].update(provider="openai", base_url="https://observer.invalid/v1")
    policy["model"].pop("price_in"); policy["model"].pop("price_out")
    with pytest.raises(ValueError, match="explicit price_in"):
        effect.X.validate_policy(policy)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
