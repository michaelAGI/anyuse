"""Pure lifecycle contract tests; root executes these in the guarded guest."""
import copy
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_inspector_lifecycle as L


def private(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    app = root / "Runtime Trial.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/MacOS/trial").write_bytes(b"controlled original fixture")
    (app / "Contents/Resources/app.asar").write_bytes(b"virtual renderer archive")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "org.anyuse.lifecycle-fixture", "CFBundleExecutable": "trial",
        "CFBundleShortVersionString": "1", "CFBundleVersion": "1"}))
    policy = {"format": L.POLICY, "bundle": str(app), "target": L.R.fingerprint(app),
              "launch_profile": "chromium_renderer_cdp", "port": 28429,
              "relative_url": "Contents/Resources/app.asar/ui/index.html",
              "profile": str(root / "profile"), "config": str(root / "config.json")}
    policy_file = private(root / "policy.json", policy)
    monkeypatch.setattr(L.R, "assert_guest", lambda: None)
    return SimpleNamespace(root=root, app=app, policy=policy, policy_file=policy_file,
                           profile=Path(policy["profile"]), config=Path(policy["config"]))


class Clock:
    def __init__(self): self.now = 100.0
    def monotonic(self): return self.now
    def sleep(self, value): self.now += max(0.01, value)


def runtime(f, monkeypatch):
    clock, opens, terminations = Clock(), [], []
    monkeypatch.setattr(L.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(L.time, "sleep", clock.sleep)
    monkeypatch.setattr(L.time, "time", lambda: 1000.0)
    state = SimpleNamespace(alive=False, pid=7101, suffix={"query": "view=1&profile=owned", "fragment": "/editor"},
                            snapshot_error=None, opens=opens, terminations=terminations, snapshots=0)
    def main():
        return {"bundle_id": f.policy["target"]["bundle_id"], "bundle": str(f.app),
                "executable": str(f.app / "Contents/MacOS/trial"), "pid": state.pid,
                "launch_time": 1000.5, "uid": os.getuid()}
    def kernel():
        return {"pid": state.pid, "ppid": 1, "uid": os.getuid(), "started_at": "fixed start " + str(state.pid),
                "executable": str(f.app / "Contents/MacOS/trial")}
    state.main, state.kernel = main, kernel
    monkeypatch.setattr(L, "_registrations", lambda _: [{"pid": state.pid, "terminated": False}] if state.alive else [])
    monkeypatch.setattr(L, "_kernel_candidates", lambda *a: [kernel()] if state.alive else [])
    monkeypatch.setattr(L, "_port_absent", lambda *a: not state.alive)
    monkeypatch.setattr(L, "_exact_main", lambda *a: (main(), kernel()))
    def listener(*args):
        assert state.alive
        return {"pid": state.pid, "uid": os.getuid(), "listen_addresses": ["127.0.0.1:28429"]}
    monkeypatch.setattr(L.I, "_listener", listener)
    def opened(policy, bundle, profile, deadline):
        intent = json.loads((f.root / state.out_name / "receipt.json").read_text())
        assert intent["status"] == "intent" and intent["launch_attempts"] == 1
        assert not f.config.exists() or state.is_restart
        opens.append((policy, bundle, profile))
        state.alive = True
    monkeypatch.setattr(L, "_open", opened)
    monkeypatch.setattr(L, "_initial_page", lambda *a: (
        (f.app / f.policy["relative_url"]).as_uri() + "?" + state.suffix["query"] + "#" + state.suffix["fragment"],
        state.suffix.copy()))
    def snapshot(policy, bundle, app, process, config, profile, deadline):
        state.snapshots += 1
        if state.snapshot_error:
            raise state.snapshot_error
        artifact = profile / "Preferences"
        artifact.write_text("owned profile metadata fixture")
        return {"identity": app, "kernel_identity": process, "listener": listener(), "renderers": [],
                "selected_url": (f.app / f.policy["relative_url"]).as_uri() + "?" + config["document_suffix"]["query"] + "#" + config["document_suffix"]["fragment"],
                "execution_context": {"unique_id": "context-" + str(state.pid)},
                "observed_profile_use": {"status": "observed_use", "artifacts": [{"file": str(artifact), "pid": state.pid}]},
                "cleanup": {"browser_socket_closed": True, "page_socket_closed": True}}
    monkeypatch.setattr(L, "_snapshot", snapshot)
    def terminate(*a, before_request):
        before_request()
        terminations.append(state.pid)
        state.alive = False
        return True
    monkeypatch.setattr(L, "_terminate", terminate)
    monkeypatch.setattr(L, "_kernel_absent", lambda *a: not state.alive)
    monkeypatch.setattr(L, "_wait_tick", lambda deadline: clock.sleep(0.1))
    state.out_name, state.is_restart = "launch", False
    return state


@pytest.mark.parametrize("name,args", [("launch", ("missing", "missing-out")),
                                      ("inspect", ("missing", "missing-out")),
                                      ("close", ("missing", "missing-out"))])
def test_vm_guard_precedes_every_read_write_or_native_action(monkeypatch, name, args):
    def denied(): raise PermissionError("VM only")
    monkeypatch.setattr(L.R, "assert_guest", denied)
    monkeypatch.setattr(L, "_private", lambda *a: pytest.fail("read before guard"))
    monkeypatch.setattr(L, "_registrations", lambda *a: pytest.fail("native before guard"))
    with pytest.raises(PermissionError, match="VM only"):
        getattr(L, name)(*args)


@pytest.mark.parametrize("change", [
    {"argv": ["--unsafe"]}, {"environment": {"NODE_OPTIONS": "source"}}, {"javascript": "source"},
    {"port": True}, {"port": 80}, {"port": 65536}, {"launch_profile": "caller_js"},
    {"relative_url": "../escape"}, {"relative_url": "/absolute"}, {"relative_url": "a//b"},
    {"relative_url": "a/./b"}, {"relative_url": "a?query"}, {"relative_url": "a#fragment"},
    {"relative_url": "a%2fb"}, {"profile": "relative"}, {"profile": "/tmp/control\npath"},
])
def test_closed_policy_refuses_before_native_operations_or_output(tmp_path, monkeypatch, change):
    f = fixture(tmp_path, monkeypatch)
    private(f.policy_file, {**f.policy, **change})
    monkeypatch.setattr(L, "_absent", lambda *a: pytest.fail("native preflight on invalid policy"))
    with pytest.raises(ValueError): L.launch(f.policy_file, f.root / "out")
    assert not (f.root / "out").exists() and not f.profile.exists()


def test_duplicate_json_and_incomplete_fingerprint_refuse(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    f.policy_file.write_text('{"format":"one","format":"two"}')
    with pytest.raises(ValueError, match="duplicate"): L.launch(f.policy_file, f.root / "out")
    bad = copy.deepcopy(f.policy)
    bad["target"].pop("build")
    private(f.policy_file, bad)
    with pytest.raises(ValueError, match="complete target"): L.launch(f.policy_file, f.root / "out")


@pytest.mark.parametrize("kind", ["mode", "hardlink", "alias", "overlap", "bundle_changed", "preexisting_profile", "preexisting_config"])
def test_owned_paths_and_initial_absence_before_launch(tmp_path, monkeypatch, kind):
    f = fixture(tmp_path, monkeypatch)
    selected = f.policy_file
    if kind == "mode": f.policy_file.chmod(0o644)
    elif kind == "hardlink": os.link(f.policy_file, f.root / "duplicate")
    elif kind == "alias":
        selected = f.root / "policy-alias"
        selected.symlink_to(f.policy_file)
    elif kind == "overlap": private(f.policy_file, {**f.policy, "config": str(f.profile / "config.json")})
    elif kind == "bundle_changed": (f.app / "Contents/MacOS/trial").write_bytes(b"changed")
    elif kind == "preexisting_profile": f.profile.mkdir(mode=0o700)
    else: private(f.config, {"preexisting": True})
    monkeypatch.setattr(L, "_absent", lambda *a: pytest.fail("preflight before path rejection"))
    with pytest.raises((OSError, ValueError)): L.launch(selected, f.root / "out")
    assert not (f.root / "out").exists()


@pytest.mark.parametrize("occupied", ["registration", "kernel", "port"])
def test_any_preexisting_app_process_or_listener_blocks_before_intent(tmp_path, monkeypatch, occupied):
    f = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "_registrations", lambda _: [1] if occupied == "registration" else [])
    monkeypatch.setattr(L, "_kernel_candidates", lambda *a: [1] if occupied == "kernel" else [])
    monkeypatch.setattr(L, "_port_absent", lambda *a: occupied != "port")
    monkeypatch.setattr(L, "_open", lambda *a: pytest.fail("unapproved launch"))
    with pytest.raises(ValueError): L.launch(f.policy_file, f.root / "out")
    assert not (f.root / "out").exists() and not f.profile.exists()


def test_one_launch_freezes_exact_suffix_observed_profile_and_no_task_credit(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    result = L.launch(f.policy_file, f.root / "launch", timeout=10)
    assert result["status"] == "ready" and len(state.opens) == 1 and state.snapshots == 1
    assert result["transport"]["document_suffix"] == state.suffix
    assert result["transport"]["config"] == L._private(f.config)[1]
    assert result["requested_profile"]["file"] == str(f.profile)
    assert result["observed_profile_use"]["status"] == "observed_use"
    assert result["isolation_verified"] is False and result["metadata_only"] is False
    assert result["inspector_commands_metadata_only"] is True
    assert result["model_calls"] == result["task_calls"] == result["app_task_methods"] == 0
    assert not result["proof_created"] and not result["task_result_verified"]
    assert (f.config.stat().st_mode & 0o777) == 0o600
    assert (f.profile.stat().st_mode & 0o777) == 0o700


def test_snapshot_failure_is_not_repeated_and_never_auto_closed(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    error = ValueError("profile or socket observation failed")
    error.lifecycle_cleanup = {"page_socket_closed": False, "browser_socket_closed": True}
    state.snapshot_error = error
    result = L.launch(f.policy_file, f.root / "launch", timeout=10)
    assert result["status"] == "incomplete" and result["resource_review_required"]
    assert result["cleanup"]["page_socket_closed"] is False
    assert state.alive and len(state.opens) == state.snapshots == 1 and not state.terminations
    assert not f.config.exists() and result["launch_attempts"] == 1


def test_first_fresh_process_replacement_is_not_adopted(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    original = L._initial_page
    def replaced(*args):
        state.pid = 7102
        return original(*args)
    monkeypatch.setattr(L, "_initial_page", replaced)
    result = L.launch(f.policy_file, f.root / "launch", timeout=10)
    assert result["status"] == "incomplete" and "replaced" in result["error"]["message"]
    assert result["identity"]["pid"] == 7101 and state.snapshots == 0 and len(state.opens) == 1


def test_listener_attribution_precedes_startup_http(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    def denied(*a): raise ValueError("foreign listener")
    monkeypatch.setattr(L.I, "_listener", denied)
    monkeypatch.setattr(L, "_initial_page", lambda *a: pytest.fail("HTTP before listener attribution"))
    result = L.launch(f.policy_file, f.root / "launch", timeout=10)
    assert result["status"] == "incomplete" and len(state.opens) == 1


def test_fixed_argv_and_injection_environment_scrub(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    observed = []
    for name in ("NODE_OPTIONS", "NODE_PATH", "ELECTRON_RUN_AS_NODE", "DYLD_INSERT_LIBRARIES", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "caller controlled")
    monkeypatch.setenv("REA_KEEP_CONTEXT", "owned")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    def command(args, deadline, *, env):
        observed.append((args, env))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(L, "_run", command)
    L._open(f.policy, f.app, f.profile, 100)
    args, env = observed[0]
    assert args[:4] == ["/usr/bin/open", "-n", str(f.app), "--args"]
    assert "--user-data-dir=" + str(f.profile) in args
    assert "--remote-debugging-address=127.0.0.1" in args and "--remote-debugging-port=28429" in args
    assert all("caller controlled" != value for value in env.values()) and env["REA_KEEP_CONTEXT"] == "owned"
    assert env["LC_ALL"] == "de_DE.UTF-8" and env["LANG"] == "fr_FR.UTF-8"


@pytest.mark.parametrize("lc_all,lang", [("de_DE.UTF-8", "fr_FR.UTF-8"),
                                        ("fr_FR.UTF-8", "ja_JP.UTF-8"), (None, "de_DE.UTF-8")])
def test_kernel_inventory_forces_child_locale_without_changing_caller(tmp_path, monkeypatch, lc_all, lang):
    f = fixture(tmp_path, monkeypatch)
    if lc_all is None:
        monkeypatch.delenv("LC_ALL", raising=False)
    else:
        monkeypatch.setenv("LC_ALL", lc_all)
    monkeypatch.setenv("LANG", lang)
    monkeypatch.setenv("LC_TIME", "de_DE.UTF-8")
    monkeypatch.setenv("REA_KEEP_CONTEXT", "owned")
    caller = dict(os.environ)
    calls = []
    executable = f.app / "Contents/MacOS/trial"
    started = "Fri Oct 16 09:30:00 2026"
    def process(args, **kwargs):
        calls.append((args, kwargs))
        raw = (f"7101 1 {os.getuid()} {started} {executable}\n"
               f"8101 1 {os.getuid()} {started} {f.root / 'Other.app/Contents/MacOS/other'}\n").encode("utf-8")
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
    monkeypatch.setattr(L.subprocess, "run", process)
    rows = L._kernel_candidates(f.app, L.time.monotonic() + 2)
    assert rows == [{"pid": 7101, "ppid": 1, "uid": os.getuid(), "started_at": started, "executable": str(executable)}]
    assert len(calls) == 1 and calls[0][0] == ["/bin/ps", "-axo", "pid=,ppid=,uid=,lstart=,comm="]
    options = calls[0][1]
    assert options["env"] == dict(caller, LC_ALL="C", LANG="C") and options["env"] is not os.environ
    assert options["capture_output"] is True and options["check"] is False
    assert 0 < options["timeout"] <= 2 and dict(os.environ) == caller


def test_locale_change_preserves_kernel_epoch_but_changed_epoch_blocks_close(tmp_path, monkeypatch):
    exact_main = L._exact_main
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    epoch = {"started_at": "Fri Oct 16 09:30:00 2026"}
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setattr(L, "_exact_main", exact_main)
    monkeypatch.setattr(L.B, "process_identity", lambda *args: state.main())
    def process(args, **kwargs):
        assert args == ["/bin/ps", "-o", "ppid=,uid=,lstart=,comm=", "-p", str(state.pid)]
        assert kwargs["env"]["LC_ALL"] == kwargs["env"]["LANG"] == "C"
        return SimpleNamespace(returncode=0,
            stdout=f"1 {os.getuid()} {epoch['started_at']} {f.app / 'Contents/MacOS/trial'}\n", stderr="")
    monkeypatch.setattr(L.subprocess, "run", process)
    launch = L.launch(f.policy_file, f.root / "launch", timeout=10)
    assert launch["status"] == "ready" and launch["kernel_identity"]["started_at"] == epoch["started_at"]
    assert os.environ["LC_ALL"] == "de_DE.UTF-8"
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    observed = L.inspect(launch["receipt"], f.root / "inspect", timeout=10)
    assert observed["status"] == "inspected" and observed["kernel_identity"] == launch["kernel_identity"]
    epoch["started_at"] = "Fri Oct 16 09:30:01 2026"
    refused = L.close(launch["receipt"], f.root / "close", timeout=10)
    assert refused["status"] == "incomplete" and refused["resource_review_required"] is True
    assert "identity changed" in refused["error"]["message"] and refused["termination_requests"] == 0
    assert launch["identity"] == state.main() and launch["kernel_identity"]["started_at"] != epoch["started_at"]
    assert len(state.opens) == 1 and not state.terminations and state.alive
    assert os.environ["LC_ALL"] == "fr_FR.UTF-8" and os.environ["LANG"] == "ja_JP.UTF-8"


def test_restart_requires_closed_previous_and_exact_profile_config_suffix(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    first = L.launch(f.policy_file, f.root / "launch", timeout=10)
    raw, inode = f.config.read_bytes(), f.profile.stat().st_ino
    with pytest.raises(ValueError, match="registered"):
        L.launch(f.policy_file, f.root / "still-running", previous=first["receipt"], timeout=10)
    closed = L.close(first["receipt"], f.root / "close", timeout=10)
    assert closed["status"] == "closed" and closed["termination_requests"] == 1 and not closed["metadata_only"]
    state.pid, state.out_name, state.is_restart = 7102, "restart", True
    second = L.launch(f.policy_file, f.root / "restart", previous=first["receipt"], timeout=10)
    assert second["status"] == "ready" and second["identity"]["pid"] != first["identity"]["pid"]
    assert second["transport"] == first["transport"] and f.config.read_bytes() == raw and f.profile.stat().st_ino == inode
    assert len(state.opens) == 2 and len(state.terminations) == 1


def test_restart_suffix_drift_preserves_config_and_stops(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    first = L.launch(f.policy_file, f.root / "launch", timeout=10)
    raw = f.config.read_bytes()
    state.alive = False
    state.pid, state.out_name, state.is_restart = 7102, "restart", True
    state.suffix = {"query": "changed=1", "fragment": "/editor"}
    second = L.launch(f.policy_file, f.root / "restart", previous=first["receipt"], timeout=10)
    assert second["status"] == "incomplete" and "no rebinding" in second["error"]["message"]
    assert f.config.read_bytes() == raw and state.snapshots == 1 and state.alive and not state.terminations


def test_inspect_identity_drift_and_socket_failure_cannot_be_ready(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    launch = L.launch(f.policy_file, f.root / "launch", timeout=10)
    observed = L.inspect(launch["receipt"], f.root / "inspect", timeout=10)
    assert observed["status"] == "inspected" and observed["metadata_only"] and len(state.opens) == 1
    error = ValueError("socket close failed")
    error.lifecycle_cleanup = {"page_socket_closed": False, "browser_socket_closed": True}
    state.snapshot_error = error
    failed = L.inspect(launch["receipt"], f.root / "inspect-failed", timeout=10)
    assert failed["resource_review_required"] and not failed["cleanup"]["page_socket_closed"]
    state.pid = 7777
    drifted = L.inspect(launch["receipt"], f.root / "inspect-drift", timeout=10)
    assert drifted["status"] == "incomplete" and "no adoption" in drifted["error"]["message"]
    assert not state.terminations


@pytest.mark.parametrize("behavior", ["refused", "dialog"])
def test_normal_close_refusal_or_dialog_stops_without_force_or_retry(tmp_path, monkeypatch, behavior):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    launch = L.launch(f.policy_file, f.root / "launch", timeout=10)
    def stop(*a, before_request):
        before_request()
        state.terminations.append(state.pid)
        return behavior != "refused"
    monkeypatch.setattr(L, "_terminate", stop)
    started = L.time.monotonic()
    result = L.close(launch["receipt"], f.root / "close", timeout=1)
    assert result["status"] == "incomplete" and result["resource_review_required"]
    assert state.alive and len(state.terminations) == 1 and len(state.opens) == 1
    if behavior == "dialog":
        # The polling loop owns its deadline even when the wait helper only
        # advances time and never checks the deadline itself.
        assert result["error"]["type"] == "TimeoutError"
        assert 1 <= L.time.monotonic() - started < 1.2


def test_fresh_kernel_and_registration_absence_override_cached_termination(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    launch = L.launch(f.policy_file, f.root / "launch", timeout=10)
    result = L.close(launch["receipt"], f.root / "close", timeout=10)
    assert result["status"] == "closed" and all(result["closure_observation"].values())
    assert len(state.terminations) == 1


def test_normal_terminate_rechecks_full_identity_after_bundle_read(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    launch = L.launch(f.policy_file, f.root / "launch", timeout=10)
    # Exercise the actual normal-termination helper, with no desktop framework.
    monkeypatch.setattr(L, "_terminate", ORIGINAL_TERMINATE)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSRunningApplication=SimpleNamespace(
        runningApplicationsWithBundleIdentifier_=lambda _: pytest.fail("identity drift must precede terminate"))))
    original = L._unchanged
    def changed(*a, **k):
        original(*a, **k)
        state.pid = 7777
    monkeypatch.setattr(L, "_unchanged", changed)
    result = L.close(launch["receipt"], f.root / "close", timeout=10)
    assert result["status"] == "incomplete" and result["termination_requests"] == 0 and not state.terminations


ORIGINAL_TERMINATE = L._terminate
ORIGINAL_SNAPSHOT = L._snapshot


@pytest.mark.parametrize("failure", [None, "context", "renderers", "close"])
def test_snapshot_is_metadata_only_binds_fresh_context_and_confirms_socket_closure(tmp_path, monkeypatch, failure):
    f = fixture(tmp_path, monkeypatch)
    state = runtime(f, monkeypatch)
    state.alive = True
    f.profile.mkdir(mode=0o700)
    config = L._config_data(f.policy, state.suffix)
    url = (f.app / f.policy["relative_url"]).as_uri() + "?" + state.suffix["query"] + "#" + state.suffix["fragment"]
    selected = {"id": "page", "type": "page", "url": url, "webSocketDebuggerUrl": "page-socket"}
    frame = {"id": "frame", "url": url.split("#")[0], "urlFragment": "#" + state.suffix["fragment"]}
    calls = []
    class Connection:
        def __init__(self, name):
            self.name, self.events, self.closed, self.invalid_context = name, [], False, False
            self.bound_context, self.frame_calls = None, 0
        def call(self, method, params=None):
            calls.append(method)
            assert method in {"Page.getFrameTree", "Runtime.enable"}
            if method == "Runtime.enable":
                self.events.append({"method": "Runtime.executionContextCreated", "params": {"context": {
                    "id": 7, "uniqueId": "fresh-context", "auxData": {"isDefault": True, "frameId": "frame"}}}})
                return {}
            self.frame_calls += 1
            if failure == "context" and self.frame_calls == 3:
                assert self.bound_context == {"unique_id": "fresh-context", "frame_id": "frame"}
                self.invalid_context = True
            return {"frameTree": {"frame": frame}}
        def close(self):
            if failure == "close" and self.name == "page-socket": raise ValueError("close failed")
            self.closed = True
    connections = []
    def connection(name, deadline):
        value = Connection(name)
        connections.append(value)
        return value
    monkeypatch.setattr(L.I, "_socket", lambda name, deadline: name)
    monkeypatch.setattr(L.I, "_CDP", connection)
    monkeypatch.setattr(L.I, "_select_target", lambda *a: ("browser-socket", selected.copy()))
    renderer_calls = []
    def renderers(*a):
        renderer_calls.append(1)
        return [{"pid": 71 if failure == "renderers" and len(renderer_calls) == 2 else 70}]
    monkeypatch.setattr(L.I, "_renderer_processes", renderers)
    monkeypatch.setattr(L, "_owned_processes", lambda *a: {state.pid: state.kernel()})
    monkeypatch.setattr(L, "_profile_use", lambda *a: {"status": "observed_use", "artifacts": [{"pid": state.pid}]})
    monkeypatch.setattr(L.B, "process_identity", lambda *a: state.main())
    monkeypatch.setattr(L.I, "_process", lambda *a: state.kernel())
    if failure:
        with pytest.raises(ValueError) as error:
            ORIGINAL_SNAPSHOT(f.policy, f.app, state.main(), state.kernel(), config, f.profile, 110)
        assert error.value.lifecycle_cleanup["browser_socket_closed"]
        assert error.value.lifecycle_cleanup["page_socket_closed"] is (failure != "close")
    else:
        result = ORIGINAL_SNAPSHOT(f.policy, f.app, state.main(), state.kernel(), config, f.profile, 110)
        assert result["execution_context"]["unique_id"] == "fresh-context" and all(result["cleanup"].values())
    assert len(connections) == 2 and "Runtime.evaluate" not in calls and "Runtime.callFunctionOn" not in calls
    assert not state.opens and not state.terminations


def test_nul_field_lsof_profile_observation_requires_exact_inode_uid_and_process(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    f.profile.mkdir(mode=0o700)
    artifact = f.profile / "Cache file"
    artifact.write_bytes(b"content never read")
    meta = artifact.stat()
    process = {"pid": 42, "uid": os.getuid(), "started_at": "owned start"}
    raw = (f"p42\0u{os.getuid()}\0\nf7\0tREG\0D0x{meta.st_dev:x}\0i{meta.st_ino}\0n{artifact}\0\n").encode()
    monkeypatch.setattr(L, "_run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw, stderr=b""))
    monkeypatch.setattr(L.I, "_process", lambda *a: process.copy())
    observed = L._profile_use(f.profile, {42: process}, 100)
    assert observed["status"] == "observed_use" and observed["contents_read"] is False
    assert observed["artifacts"][0]["inode"] == meta.st_ino
    raw = raw.replace(f"i{meta.st_ino}\0".encode(), b"i1\0")
    with pytest.raises(ValueError, match="inode/device"): L._profile_use(f.profile, {42: process}, 100)


@pytest.mark.parametrize("bad", ["foreign_pid", "foreign_uid", "newline_spoof", "symlink", "process_replaced"])
def test_profile_observation_never_trusts_requested_argv_alone(tmp_path, monkeypatch, bad):
    f = fixture(tmp_path, monkeypatch)
    f.profile.mkdir(mode=0o700)
    artifact = f.profile / "Preferences"
    artifact.write_bytes(b"unused")
    value = artifact.stat()
    pid, uid = (99 if bad == "foreign_pid" else 42), (os.getuid() + 1 if bad == "foreign_uid" else os.getuid())
    name = str(artifact)
    if bad == "newline_spoof": name = str(f.root / "foreign") + "\nn" + str(artifact)
    if bad == "symlink":
        alias = f.profile / "alias"
        alias.symlink_to(artifact)
        name = str(alias)
    raw = f"p{pid}\0u{uid}\0\nf7\0tREG\0D0x{value.st_dev:x}\0i{value.st_ino}\0n{name}\0\n".encode()
    monkeypatch.setattr(L, "_run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw, stderr=b""))
    process = {"pid": 42, "uid": os.getuid(), "started_at": "original"}
    monkeypatch.setattr(L.I, "_process", lambda *a: {**process, "started_at": "replacement"} if bad == "process_replaced" else process)
    with pytest.raises(ValueError): L._profile_use(f.profile, {42: process}, 100)


def test_initial_page_uniqueness_and_raw_suffix_pin(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    url = (f.app / f.policy["relative_url"]).as_uri() + "?profile=%2Fowned&wid=1#/editor"
    rows = [{"type": "page", "url": url}]
    monkeypatch.setattr(L.I, "_http_json", lambda *a: rows)
    seen, suffix = L._initial_page(f.policy, f.app, 100)
    assert seen == url and suffix == {"query": "profile=%2Fowned&wid=1", "fragment": "/editor"}
    rows *= 2
    with pytest.raises(ValueError, match="ambiguous"): L._initial_page(f.policy, f.app, 100)


def test_cli_help_is_lazy_and_explicit_operator_dispatch(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli as C
    monkeypatch.setattr(C.R, "assert_guest", lambda: None)
    selected = private(tmp_path / "input.json", {})
    calls = []
    fake = SimpleNamespace(**{name: (lambda *a, _name=name, **k: (calls.append((_name, a, k)) or {
        "status": {"launch": "ready", "inspect": "inspected", "close": "closed"}[_name]}))
        for name in ("launch", "inspect", "close")})
    import anyuse.execution as execution
    monkeypatch.setattr(execution, "rea_inspector_lifecycle", fake)
    runner = CliRunner()
    assert runner.invoke(C.rea_commands, ["inspector", "--help"]).exit_code == 0 and not calls
    for name in ("launch", "inspect", "close"):
        args = ["inspector", name, str(selected), "--out", str(tmp_path / name), "--timeout", "7"]
        if name == "launch": args += ["--previous", str(selected)]
        value = runner.invoke(C.rea_commands, args)
        assert value.exit_code == 0, value.output
    assert calls[0] == ("launch", (str(selected), str(tmp_path / "launch")), {"previous": str(selected), "timeout": 7})
    assert calls[1][0] == "inspect" and calls[1][2] == {"timeout": 7}
    assert calls[2][0] == "close" and calls[2][2] == {"timeout": 7}
    fake.launch = lambda *a, **k: {"status": "incomplete", "resource_review_required": True}
    failed = runner.invoke(C.rea_commands, ["inspector", "launch", str(selected), "--out", str(tmp_path / "failed")])
    assert failed.exit_code != 0 and "inspect the receipt" in failed.output


if __name__ == "__main__":
    L.R.assert_guest()
    raise SystemExit(pytest.main([__file__, "-q"]))
