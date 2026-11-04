"""Inert public-pixel/native-receipt tests; use the guarded allowlist runner."""
import copy
import base64
import hashlib
import io
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_state_evidence as E
from anyuse.execution import rea_playback_state as O
from anyuse.execution import rea_playback_readiness as V
from anyuse.execution import rea_playback_trial as P
from test_rea_runtime_bridge import runtime


#: Exact bytes of the reviewed historical sources, vendored so the suite runs
#: without the development history (``testdata/reviewed_sources/<commit>/<role>``).
_REVIEWED_SOURCES = Path(__file__).resolve().parent.parent / "testdata" / "reviewed_sources"


def _reviewed_bytes(head, role):
    return (_REVIEWED_SOURCES / head / role).read_bytes()


OBSERVER = {"format": O.OBSERVER,
    "control": {"path": [{"attribute": "AXMenuBar", "index": None}, {"attribute": "AXChildren", "index": 2}],
                "role": "AXMenuItem", "identifier": "inert-playback-control", "playing_title": "Pause media", "paused_title": "Play media"},
    "video": {"region": [.1, .2, .8, .5], "frames": 3, "interval_seconds": .1,
              "black_luma": 10, "min_nonblack_fraction": .1, "motion_min": 3.0, "still_max": .1}}
ACTOR = {"kind": "objc_method", "receiver": {"class": "MappedMediaCoordinator", "kind": "class", "path": [
    {"selector": "sharedCoordinator", "signature": "@16@0:8"},
    {"selector": "activeWindow", "signature": "@16@0:8"},
    {"selector": "windowController", "signature": "@16@0:8"}]},
    "selector": "pausePlayback:", "signature": "v24@0:8@16", "arguments": [{"type": "nil", "value": None}], "return_type": "void"}


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += max(0, seconds)


@pytest.fixture
def playback(runtime, monkeypatch):
    app, identity, _, native, root = runtime
    out = root.parent / "playback-trial"
    media = root.parent / "public-media.fixture"
    media.write_bytes(b"Inert local media: no decoder or application is used.")
    window = {"id": 77, "pid": identity["pid"], "role": "AXWindow", "subrole": "AXStandardWindow", "title": media.name,
              "document": media.as_uri(), "position": [0.0, 0.0], "size": [120.0, 100.0]}
    context = {"format": O.CONTEXT, "window": window, "media": P._pin(media)}
    policy = {"format": P.POLICY, "id": "inert-fixed-playback", "target": R.fingerprint(app), "kind": "fixed_state_transition",
              "params": [], "input_scope": "fixed_no_actor_inputs", "heldout_scope": "fresh_trials_with_declared_restart",
              "phase_minimums": {"learning": 3, "fresh": 3, "heldout": 3}, "observer": copy.deepcopy(OBSERVER), "before": "playing", "after": "paused"}
    intent = {"format": P.INTENT, "id": "learning-0", "phase": "learning", "policy_sha256": R.digest(policy), "inputs": {},
              "deployment": {"id": "inert-existing-epoch", "kind": "existing_process", "identity": identity},
              "context": context, "observed_monotonic": 99.0}
    clock, controls = Clock(), {}
    monkeypatch.setattr(P.time, "monotonic", clock)
    row = {"index": 0, "op": "invoke", "status": "ok", "return_type": "void", "value": None,
           "method": {"class": "InertMovieController", "dispatch": "-", "selector": ACTOR["selector"], "signature": ACTOR["signature"],
                      "module": identity["executable"], "implementation_offset": "0x1000", "origin": "module_backed",
                      "return_type": "void", "argument_types": ["nil"]}}
    native["result_changes"].update(operations=[row], identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity),
        process={"pid": identity["pid"], "executable": identity["executable"], "architecture": "arm64"}, queue="main")
    def completed():
        folder = native["calls"][-1]["cwd"]
        worker = R.load_json(folder / "worker-output.json")
        R.write_json(folder / "worker-cleanup.json", {"format": B.CLEANUP_JOURNAL, "request_sha256": worker["request_sha256"],
            "status": worker["status"], "attached": worker["attached"], "frida_lifecycle": worker["frida_lifecycle"],
            "cleanup": worker["cleanup"], "cleanup_errors": worker["cleanup_errors"], "error": worker["error"],
            "native_state_uncertain": worker["native_state_uncertain"], "native_calls": worker["native_calls"],
            "invocations": worker["invocations"], "helper_load_attempts": worker["helper_load_attempts"], "completed_operations": len(worker["operations"])})
        clock.sleep(.2)
    native["after_worker"] = completed
    class Pixels:
        def __init__(self): self.frame_index, self.inspect_index = 0, 0
        def inspect(self, current_identity, observer, deadline):
            clock.sleep(.01)
            after = bool(native["calls"])
            current_window = copy.deepcopy(window)
            if controls.get("wrong_media"): current_window["document"] = "file:///different-media.fixture"
            system = {"identity": identity,
                "kernel": {"pid": identity["pid"], "ppid": 1, "uid": identity["uid"], "started_at": "inert-kernel-epoch", "executable": identity["executable"]},
                "session": {"asid": 7, "attribute_bits": 0x10, "uid": identity["uid"], "on_console": True, "login_done": True, "locked": False, "ax_trusted": True},
                "focus": {"frontmost_pid": identity["pid"], "focused_pid": None}}
            display = {"id": 1, "position": [0.0, 0.0], "size": [120.0, 100.0]}
            displays = [display]
            if controls.get("multi_display"): displays += [{**display, "id": 2}]
            visible = {"id": window["id"], "pid": identity["pid"], "layer": 0, "alpha": 1.0, "on_screen": True,
                       "position": window["position"], "size": window["size"], "title": media.name}
            above, below = [], []
            if controls.get("overlay"):
                above.append({**visible, "id": 999, "pid": 456, "layer": 25, "position": [40.0, 30.0], "size": [2.0, 2.0]})
            captures = []
            if controls.get("transparent_overlay"):
                above = [{**visible, "id": 999, "pid": 456, "layer": 25}]
                if controls.get("outside_churn"):
                    below = [{**visible, "id": 1001, "pid": 456, "title": "background-" + str(self.inspect_index)}]
                rows = [*above, visible, *below]
                # An actual alpha-bearing window image. Its bottom pixels can
                # vary, while the entire frozen video intersection stays clear.
                image = Image.new("RGBA", (120, 100), (40 + self.inspect_index, 50, 60, 0))
                for x in range(120): image.putpixel((x, 99), (80, 90, 100, 255))
                captures = [alpha_capture(image, 999, rows, clock)]
                if controls.get("outside_churn"):
                    for bookend, label in (("before", "capturing"), ("after", "finished")):
                        captures[0][bookend][-1]["title"] = label
                        captures[0][bookend].insert(0, {**visible, "id": 1002, "pid": 456, "layer": 99,
                                                       "position": [0.0, 0.0], "size": [2.0, 2.0]})
                self.inspect_index += 1
            return {"format": O.SNAPSHOT, "system": system, "window": current_window, "display": display, "displays": displays,
                    "visibility": {"selected": visible, "above": above, "below": below, "capture_scale": 1, "alpha_captures": captures},
                    "control": {"role": "AXMenuItem", "identifier": "inert-playback-control", "enabled": True,
                                "title": "Play media" if after and not controls.get("wrong_after_control") else "Pause media"}}
        def frame(self, display, file, deadline):
            after = bool(native["calls"])
            value = 120 if after else 40 + self.frame_index * 30
            if not after and controls.get("before_still"): value = 90
            if after and controls.get("after_moving"): value += self.frame_index * 30
            if controls.get("black"): value = 0
            Image.new("RGB", (120, 100), (value, value, value)).save(file)
            self.frame_index += 1
            clock.sleep(.05)
    def capture(observer, target, current_identity, current_context, state, destination, *, deadline):
        return O._capture(observer, target, current_identity, current_context, state, destination, deadline=deadline,
                          backend=Pixels(), clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(O, "capture", capture)
    def check_current(observer, current_identity, current_context, *, deadline):
        if controls.get("changed_at_dispatch"):
            raise ValueError("inert public foreground/window changed before dispatch")
        return O.validate_snapshot(Pixels().inspect(current_identity, observer, deadline), observer, current_identity, current_context)
    monkeypatch.setattr(O, "check_current", check_current)
    return {"app": app, "identity": identity, "native": native, "out": out, "policy": policy, "intent": intent,
            "context": context, "controls": controls, "clock": clock, "pixels": Pixels}


def run(data):
    return P.run_trial(str(data["app"]), ACTOR, data["policy"], data["intent"], str(data["out"]), expected_identity=data["identity"], timeout=10)


def alpha_capture(image, window_id, rows, clock):
    stream = io.BytesIO(); image.save(stream, format="PNG"); raw = stream.getvalue()
    start = clock(); clock.sleep(.001)
    return {"window_id": window_id, "started_monotonic": start, "finished_monotonic": clock(),
            "before": copy.deepcopy(rows), "after": copy.deepcopy(rows),
            "png": {"base64": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}


def visual_readiness(playback, tmp_path, *, backend=None, seconds=10, check=None):
    backend = playback["pixels"]() if backend is None else backend
    value = V._wait_for_playing(playback["policy"]["observer"], playback["policy"]["target"], playback["identity"],
        playback["context"], str(tmp_path / "public-pixel-readiness"), deadline=playback["clock"]() + seconds,
        backend=backend, clock=playback["clock"], sleep=playback["clock"].sleep, check=(lambda: None) if check is None else check)
    return value, backend


def test_public_visual_readiness_waits_for_actual_black_to_moving_pixels(playback, tmp_path):
    backend = playback["pixels"](); frame = backend.frame
    def delayed(display, file, deadline):
        playback["controls"]["black"] = backend.frame_index < 2
        frame(display, file, deadline)
    backend.frame = delayed
    result, _ = visual_readiness(playback, tmp_path, backend=backend)
    retained = R.load_json(result["receipt"]["file"])
    assert result["status"] == "ready" and result["sample_count"] == 5
    assert [row["metrics"]["nonblack_fraction"] for row in retained["samples"]] == [0, 0, 1, 1, 1]
    assert retained["decisions"][-1]["status"] == "ready"
    assert not result["proof_created"] and result["native_goal_calls"] == result["model_calls"] == 0
    assert not playback["native"]["calls"]
    # Readiness is not the trial's before proof: newly black frames still
    # refuse before any native actor, even though preparation just succeeded.
    playback["controls"]["black"] = True
    trial = run(playback)
    assert trial["status"] == "failed" and not trial["actuated"] and not playback["native"]["calls"]


def test_public_visual_readiness_waits_for_measured_motion(playback, tmp_path):
    backend = playback["pixels"](); frame = backend.frame
    def delayed(display, file, deadline):
        playback["controls"]["before_still"] = backend.frame_index < 3
        frame(display, file, deadline)
    backend.frame = delayed
    result, _ = visual_readiness(playback, tmp_path, backend=backend)
    retained = R.load_json(result["receipt"]["file"])
    assert result["sample_count"] == 5
    assert retained["decisions"][2]["status"] == "not_moving"
    assert not playback["native"]["calls"]


def test_public_visual_readiness_permanent_black_is_bounded_refusal(playback, tmp_path):
    playback["controls"]["black"] = True; backend = playback["pixels"]()
    with pytest.raises(ValueError, match="did not become nonblack"):
        visual_readiness(playback, tmp_path, backend=backend)
    retained = R.load_json(tmp_path / "public-pixel-readiness/readiness.json")
    assert retained["status"] == "refused" and len(retained["samples"]) == V.MAX_READINESS_FRAMES
    assert backend.frame_index == V.MAX_READINESS_FRAMES and not playback["native"]["calls"]


@pytest.mark.parametrize("failure", ["identity", "occlusion", "capture"])
def test_public_visual_readiness_uncertainty_is_terminal(playback, tmp_path, failure):
    backend = playback["pixels"](); inspect = backend.inspect; frame = backend.frame
    playback["controls"]["black"] = True
    def guarded_inspect(identity, observer, deadline):
        value = copy.deepcopy(inspect(identity, observer, deadline))
        if backend.frame_index and failure == "identity":
            value["system"]["identity"]["launch_time"] += 1
        return value
    def guarded_frame(display, file, deadline):
        if failure == "capture":
            raise PermissionError("inert capture permission unavailable")
        frame(display, file, deadline)
        if failure == "occlusion":
            playback["controls"]["overlay"] = True
    backend.inspect = guarded_inspect; backend.frame = guarded_frame
    with pytest.raises((ValueError, PermissionError)):
        visual_readiness(playback, tmp_path, backend=backend)
    retained = R.load_json(tmp_path / "public-pixel-readiness/readiness.json")
    assert retained["status"] == "refused" and len(retained["samples"]) == 0
    assert backend.frame_index == (0 if failure == "capture" else 1)
    assert not playback["native"]["calls"]


def test_public_visual_readiness_does_not_extend_caller_deadline(playback, tmp_path):
    playback["controls"]["black"] = True; backend = playback["pixels"]()
    with pytest.raises(ValueError, match="deadline"):
        visual_readiness(playback, tmp_path, backend=backend, seconds=.12)
    retained = R.load_json(tmp_path / "public-pixel-readiness/readiness.json")
    assert retained["status"] == "refused" and backend.frame_index <= 2
    assert retained["deadline_monotonic"] == 100.12 and not playback["native"]["calls"]


def test_public_visual_readiness_guard_change_is_terminal(playback, tmp_path):
    backend = playback["pixels"](); playback["controls"]["black"] = True
    def guard():
        if backend.frame_index:
            raise ValueError("inert foreground guard changed")
    with pytest.raises(ValueError, match="foreground guard changed"):
        visual_readiness(playback, tmp_path, backend=backend, check=guard)
    assert backend.frame_index == 1 and not playback["native"]["calls"]


def test_public_visual_readiness_storage_refuses_before_capture(playback, tmp_path, monkeypatch):
    monkeypatch.setattr(V.os, "statvfs", lambda path: SimpleNamespace(f_bavail=0, f_frsize=1))
    backend = playback["pixels"]()
    with pytest.raises(ValueError, match="storage reserve"):
        visual_readiness(playback, tmp_path, backend=backend)
    assert backend.frame_index == 0 and not (tmp_path / "public-pixel-readiness").exists()
    assert not playback["native"]["calls"]


def test_public_visual_readiness_png_budget_refuses_without_more_samples(playback, tmp_path, monkeypatch):
    monkeypatch.setattr(V, "MAX_READINESS_PNG_BYTES", 1)
    backend = playback["pixels"]()
    with pytest.raises(ValueError, match="PNG byte bound"):
        visual_readiness(playback, tmp_path, backend=backend)
    assert backend.frame_index == 1 and not playback["native"]["calls"]


@pytest.mark.parametrize("black", [False, True])
def test_public_readiness_large_bookends_have_bounded_individual_frame_files(playback, tmp_path, monkeypatch, black):
    # Each genuine-shaped bookend fits this reduced per-JSON bound; several
    # together do not. The compact manifest must retain final ready/refused.
    monkeypatch.setattr(R, "MAX_JSON_BYTES", 24 * 1024)
    backend = playback["pixels"](); inspect = backend.inspect
    playback["controls"]["black"] = black
    def large(identity, observer, deadline):
        value = copy.deepcopy(inspect(identity, observer, deadline))
        selected = value["visibility"]["selected"]
        value["visibility"]["below"] = [{**selected, "id": 1000 + i, "pid": 456,
            "title": "inert unrelated CG metadata " + str(i) + "x" * 1500} for i in range(4)]
        return value
    backend.inspect = large
    if black:
        with pytest.raises(ValueError, match="did not become nonblack"):
            visual_readiness(playback, tmp_path, backend=backend)
    else:
        result, _ = visual_readiness(playback, tmp_path, backend=backend)
        assert result["status"] == "ready"
    manifest = tmp_path / "public-pixel-readiness/readiness.json"
    retained = R.load_json(manifest)
    assert retained["status"] == ("refused" if black else "ready") and manifest.stat().st_size < R.MAX_JSON_BYTES
    assert len(retained["samples"]) == (V.MAX_READINESS_FRAMES if black else 3)
    assert sum(row["frame"]["bytes"] for row in retained["samples"]) > R.MAX_JSON_BYTES
    assert retained["limits"]["aggregate_frame_json_bytes"] == V.MAX_READINESS_FRAMES * R.MAX_JSON_BYTES
    for row in retained["samples"]:
        assert row["frame"]["bytes"] <= R.MAX_JSON_BYTES
        frame = R.load_json(row["frame"]["file"])
        assert len(frame["before"]["visibility"]["below"]) == len(frame["after"]["visibility"]["below"]) == 4
        assert R._file_hash(Path(row["frame"]["file"])) == row["frame"]["sha256"]
    assert not playback["native"]["calls"]


def test_public_readiness_changed_retained_frame_refuses_before_ready(playback, tmp_path):
    backend = playback["pixels"](); frame = backend.frame
    def changed(display, file, deadline):
        frame(display, file, deadline)
        if backend.frame_index == 3:
            retained = Path(file).parent / "frame-00.json"
            retained.write_bytes(retained.read_bytes() + b"\n")
    backend.frame = changed
    with pytest.raises(ValueError):
        visual_readiness(playback, tmp_path, backend=backend)
    retained = R.load_json(tmp_path / "public-pixel-readiness/readiness.json")
    assert retained["status"] == "refused" and backend.frame_index == 3
    assert not playback["native"]["calls"]


@pytest.mark.parametrize("readiness_refuses", [False, True])
def test_public_setup_requires_pixel_readiness_inside_original_deadline(playback, tmp_path, monkeypatch, readiness_refuses):
    from anyuse.execution import rea_public_campaign as U
    Node = lambda **values: SimpleNamespace(values=values)
    window = Node(AXDocument=playback["context"]["window"]["document"], AXMain=True, AXTitle="inert media")
    control = Node(AXRole="AXMenuItem", AXIdentifier="inert-playback-control", AXEnabled=True, AXTitle="Pause media")
    menu = Node(AXChildren=[None, None, control]); root = Node(AXWindows=[window], AXFocusedWindow=window, AXMenuBar=menu)
    calls = []
    owned = SimpleNamespace(activateWithOptions_=lambda options: calls.append("activate") or True)
    A = SimpleNamespace(AXUIElementCreateApplication=lambda pid: root,
        AXUIElementPerformAction=lambda node, action: calls.append(action) or 0,
        AXUIElementIsAttributeSettable=lambda *args: (0, True),
        AXUIElementSetAttributeValue=lambda *args: calls.append("set-main") or 0)
    K = SimpleNamespace(NSApplicationActivateIgnoringOtherApps=1,
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: SimpleNamespace(processIdentifier=lambda: playback["identity"]["pid"]))))
    Bfake = SimpleNamespace(process_identity=lambda *args: copy.deepcopy(playback["identity"]))
    monkeypatch.setattr(U, "api", lambda: (R, Bfake, O.S, None, None, K, A, None))
    monkeypatch.setattr(U, "identity", lambda expected: (expected, owned))
    monkeypatch.setattr(U, "gate", lambda expected, *args: (expected, owned))
    monkeypatch.setattr(U, "attr", lambda node, key, optional=False: node.values.get(key))
    monkeypatch.setattr(U.time, "monotonic", playback["clock"])
    monkeypatch.setattr(O, "current_context", lambda *args, **kwargs: copy.deepcopy(playback["context"]))
    destination = tmp_path / "ordinary-public-setup"
    def ready(observer, target, identity, context, out, *, deadline):
        assert deadline == 105.0 and out == str(destination / "pixel-readiness")
        assert R.load_json(destination / "setup.json")["status"] == "running"
        assert observer == OBSERVER and target == playback["policy"]["target"] and identity == playback["identity"]
        if readiness_refuses: raise ValueError("inert bounded pixel readiness refused")
        return {"status": "ready", "read_only": True, "proof_created": False}
    monkeypatch.setattr(V, "wait_for_playing", ready)
    context = {"bundle": str(playback["app"]), "target": playback["policy"]["target"],
        "expected_identity": playback["identity"], "out": str(destination), "deadline_monotonic": 105.0,
        "request": {"media": playback["context"]["media"]["file"], "observer": OBSERVER}}
    if readiness_refuses:
        with pytest.raises(ValueError, match="pixel readiness refused"): U.prepare_case(context)
    else:
        assert U.prepare_case(context)["status"] == "ready"
    journal = R.load_json(destination / "setup.json")
    assert journal["status"] == ("failed" if readiness_refuses else "ready")
    assert calls == ["activate", "AXRaise", "set-main"] and journal["play_attempts"] == 0
    assert journal["native_goal_calls"] == 0 and not playback["native"]["calls"]


def test_real_shared_transport_and_pixels_validate_four_calls_one_task(playback, monkeypatch):
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    report = result["measurement"]
    assert report["getter_calls"] == 3 and report["native_calls"] == 4 and report["invocations"] == 1
    assert report["inputs"] == {} and not report["task_proof_created"] and not report["restart_claimed"]
    assert result["case_file"] == P._pin(playback["out"] / "case.json")
    assert all(name == pin["file"] for name, pin in report["files"].items())
    assert len(playback["native"]["calls"]) == 1
    monkeypatch.setattr(B, "process_identity", lambda *a: (_ for _ in ()).throw(AssertionError("historical validation cannot look up old process")))
    monkeypatch.setattr(O, "_Mac", lambda: (_ for _ in ()).throw(AssertionError("historical validation cannot capture desktop")))
    assert P.validate_trial(playback["policy"], result["case"], ACTOR)["trial_measurement_validated"]


def test_private_worker_disposition_confirms_clean_worker_before_independent_after_refusal(playback):
    playback["controls"]["after_moving"] = True
    disposition = R._ReplayDisposition()
    result = P.run_trial(str(playback["app"]), ACTOR, playback["policy"], playback["intent"], str(playback["out"]),
                         expected_identity=playback["identity"], timeout=10, _disposition=disposition)
    assert result["status"] == "failed" and len(playback["native"]["calls"]) == 1
    assert disposition.worker_started is True and disposition.cleanup_confirmed is True


def test_private_worker_disposition_latches_unknown_when_playback_dispatch_is_interrupted(playback, monkeypatch):
    disposition = R._ReplayDisposition()
    primary = KeyboardInterrupt("inert playback worker interrupted")
    def interrupted(*args, **kwargs): raise primary
    monkeypatch.setattr(R, "_bounded_run", interrupted)
    with pytest.raises(KeyboardInterrupt) as raised:
        P.run_trial(str(playback["app"]), ACTOR, playback["policy"], playback["intent"], str(playback["out"]),
                    timeout=10, _disposition=disposition)
    assert raised.value is primary and disposition.worker_started is True and disposition.cleanup_confirmed is False


def test_serialized_disposition_is_refused_before_any_playback_work(playback):
    with pytest.raises(ValueError, match="private typed"):
        P.run_trial(str(playback["app"]), ACTOR, playback["policy"], playback["intent"], str(playback["out"]),
                    timeout=10, _disposition={"worker_started": True, "cleanup_confirmed": True})
    assert not playback["native"]["calls"] and not playback["out"].exists()


@pytest.mark.parametrize("failure", ["black", "before_still", "wrong_media", "multi_display", "changed_at_dispatch", "overlay"])
def test_invalid_before_fixture_never_dispatches(playback, failure):
    playback["controls"][failure] = True
    result = run(playback)
    assert result["status"] == "failed" and result["actuated"] is False
    assert not playback["native"]["calls"] and not (playback["out"] / "measurement.json").exists()


@pytest.mark.parametrize("failure", ["after_moving", "wrong_after_control"])
def test_after_refusal_keeps_actual_call_counts_and_never_retries(playback, failure):
    playback["controls"][failure] = True
    result = run(playback)
    assert result["status"] == "failed" and result["actuated"] is True
    assert result["native_calls"] == 4 and result["invocations"] == 1 and len(playback["native"]["calls"]) == 1
    assert not (playback["out"] / "measurement.json").exists()


@pytest.mark.parametrize("count", [1, 3, 5, True])
def test_native_counts_cannot_omit_getters_or_invent_extra_entries(playback, count):
    playback["native"]["result_changes"]["native_calls"] = count
    result = run(playback)
    assert result["status"] == "failed" and len(playback["native"]["calls"]) == 1
    assert not (playback["out"] / "measurement.json").exists()


def test_retained_black_pixels_fail_even_when_rehashed_and_statistics_updated(playback):
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    case = result["case"]
    after_file = Path(case["evidence"]["after"]["file"])
    after = R.load_json(after_file)
    for sample in after["samples"]:
        file = Path(sample["image"]["file"])
        Image.new("RGB", (120, 100), (0, 0, 0)).save(file)
        sample["image"] = P._pin(file)
        sample["metrics"], _ = O.image_metrics(file.read_bytes(), sample["before"]["window"], sample["before"]["display"], playback["policy"]["observer"]["video"])
    after["deltas"] = [0.0, 0.0]
    R.write_json(after_file, after)
    case["evidence"]["after"] = P._pin(after_file)
    with pytest.raises(ValueError, match="black"):
        P.validate_trial(playback["policy"], case, ACTOR)


def test_reported_statistics_are_recomputed_from_actual_pngs(playback):
    result = run(playback)
    before_file = Path(result["case"]["evidence"]["before"]["file"])
    before = R.load_json(before_file)
    before["samples"][0]["metrics"]["mean_rgb"][0] += 1
    R.write_json(before_file, before)
    case = result["case"]; case["evidence"]["before"] = P._pin(before_file)
    with pytest.raises(ValueError, match="statistics"):
        P.validate_trial(playback["policy"], case, ACTOR)


def test_visibility_rejects_tiny_high_layer_overlay_but_allows_edge_touch(playback):
    result = run(playback)
    snapshot = R.load_json(result["case"]["evidence"]["before"]["file"])["samples"][0]["before"]
    selected = snapshot["visibility"]["selected"]
    # The frozen crop starts at x=12. A window ending exactly at12 is outside.
    snapshot["visibility"]["above"] = [{**selected, "id": 900, "pid": 999, "layer": 99,
                                         "position": [0.0, 0.0], "size": [12.0, 80.0]}]
    O.validate_snapshot(snapshot, OBSERVER, playback["identity"], playback["context"])
    snapshot["visibility"]["above"][0].update(position=[13.0, 21.0], size=[.1, .1])
    with pytest.raises(ValueError, match="overlaps"):
        O.validate_snapshot(snapshot, OBSERVER, playback["identity"], playback["context"])


def test_actual_transparent_overlap_keeps_full_display_verification_and_dispatch_checkpoint(playback):
    playback["controls"]["transparent_overlay"] = True
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    case = result["case"]
    before = R.load_json(case["evidence"]["before"]["file"])
    first, second = before["samples"][0]["before"], before["samples"][0]["after"]
    assert first["visibility"]["alpha_captures"] != second["visibility"]["alpha_captures"]
    assert O._stable_snapshot(first, OBSERVER["video"]) == O._stable_snapshot(second, OBSERVER["video"])
    journal = R.load_json(case["evidence"]["journal"]["file"])
    assert len(journal["pre_dispatch"]["visibility"]["alpha_captures"]) == 1
    assert P.validate_trial(playback["policy"], case, ACTOR)["trial_measurement_validated"]


def test_unrelated_capture_surface_and_background_title_changes_are_retained_without_blocking(playback):
    playback["controls"].update(transparent_overlay=True, outside_churn=True)
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    case = result["case"]
    before = R.load_json(case["evidence"]["before"]["file"])
    snapshot = before["samples"][0]["before"]
    capture = snapshot["visibility"]["alpha_captures"][0]
    rows = [*snapshot["visibility"]["above"], snapshot["visibility"]["selected"], *snapshot["visibility"]["below"]]
    assert len(capture["before"]) == len(rows) + 1
    assert capture["before"][-1]["title"] != capture["after"][-1]["title"]
    assert P.validate_trial(playback["policy"], case, ACTOR)["trial_measurement_validated"]


@pytest.mark.parametrize("failure", ["new_overlay", "changed_overlay", "missing_selected", "duplicate_owned_binding",
                                    "foreground_changed", "reordered_overlaps", "invalid_irrelevant_row"])
def test_every_alpha_inventory_rechecks_binding_and_relevant_occlusion(playback, failure):
    playback["controls"].update(transparent_overlay=True, outside_churn=True)
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    case = result["case"]; file = Path(case["evidence"]["before"]["file"])
    value = R.load_json(file)
    snapshot = value["samples"][0]["before"]; visibility = snapshot["visibility"]
    capture = visibility["alpha_captures"][0]
    rows = capture["after"]; selected = visibility["selected"]
    if failure == "new_overlay": rows[0].update(position=[20.0, 30.0])
    elif failure == "changed_overlay": rows[1]["alpha"] = .5
    elif failure == "missing_selected": rows[:] = [row for row in rows if row["id"] != selected["id"]]
    elif failure == "duplicate_owned_binding": rows.append({**selected, "id": 2000})
    elif failure == "foreground_changed":
        rows.insert(0, {**selected, "id": 2000, "pid": 456, "position": [130.0, 0.0]})
    elif failure == "reordered_overlaps":
        second = {**selected, "id": 2000, "pid": 456, "layer": 26, "alpha": 0}
        visibility["above"].append(copy.deepcopy(second))
        for name in ("before", "after"): capture[name].insert(2, copy.deepcopy(second))
        capture["after"][1], capture["after"][2] = capture["after"][2], capture["after"][1]
    elif failure == "invalid_irrelevant_row": rows[0]["alpha"] = float("nan")
    if failure == "invalid_irrelevant_row":
        # JSON intentionally forbids NaN before historical reading too.
        with pytest.raises(ValueError): O.validate_snapshot(snapshot, OBSERVER, playback["identity"], playback["context"])
        return
    R.write_json(file, value); case["evidence"]["before"] = P._pin(file)
    with pytest.raises(ValueError): P.validate_trial(playback["policy"], case, ACTOR)


@pytest.mark.parametrize("failure", ["rgb", "nonzero_alpha", "wrong_scale", "wrong_hash", "wrong_size", "noncanonical_base64",
                                    "changed_owner", "changed_geometry", "changed_order", "missing", "duplicate", "extra",
                                    "late_capture", "changed_public_inventory", "wrong_display_scale"])
def test_historical_alpha_evidence_redecodes_pixels_and_refuses_unbound_measurements(playback, failure):
    playback["controls"]["transparent_overlay"] = True
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    case = result["case"]
    file = Path(case["evidence"]["before"]["file"])
    before = R.load_json(file)
    sample = before["samples"][0]
    visibility = sample["before"]["visibility"]
    capture = visibility["alpha_captures"][0]
    if failure in {"rgb", "nonzero_alpha", "wrong_scale"}:
        image = Image.new("RGB" if failure == "rgb" else "RGBA", (60, 50) if failure == "wrong_scale" else (120, 100),
                          (80, 90, 100) if failure == "rgb" else (80, 90, 100, 0))
        if failure == "nonzero_alpha": image.putpixel((12, 20), (80, 90, 100, 1))
        capture["png"] = alpha_capture(image, capture["window_id"], capture["before"], Clock())["png"]
    elif failure == "wrong_hash": capture["png"]["sha256"] = "0" * 64
    elif failure == "wrong_size": capture["png"]["bytes"] += 1
    elif failure == "noncanonical_base64": capture["png"]["base64"] += "\n"
    elif failure == "changed_owner": capture["after"][0]["pid"] += 1
    elif failure == "changed_geometry": capture["after"][0]["position"][0] += .1
    elif failure == "changed_order": capture["before"].reverse()
    elif failure == "missing": visibility["alpha_captures"] = []
    elif failure == "duplicate": visibility["alpha_captures"].append(copy.deepcopy(capture))
    elif failure == "extra": visibility["above"] = []
    elif failure == "late_capture": capture["finished_monotonic"] = sample["capture_started_monotonic"] + .001
    elif failure == "changed_public_inventory":
        # Both local PNG bookends agree, but public inventory differs from the
        # next snapshot. Actual transparency cannot excuse that context change.
        visibility["above"].append({**visibility["selected"], "id": 888, "pid": 555, "layer": 25, "alpha": 0})
        capture["before"].insert(1, copy.deepcopy(visibility["above"][-1])); capture["after"] = copy.deepcopy(capture["before"])
    elif failure == "wrong_display_scale": visibility["capture_scale"] = 2
    R.write_json(file, before); case["evidence"]["before"] = P._pin(file)
    with pytest.raises(ValueError): P.validate_trial(playback["policy"], case, ACTOR)


def test_alpha_crop_includes_every_partly_intersected_edge_pixel():
    image = Image.new("RGBA", (4, 4), (120, 120, 120, 0))
    row = {"position": [12.25, 20.25], "size": [2.0, 2.0]}
    bounds = [12.9, 20.9, 14.1, 22.1]
    def raw():
        stream = io.BytesIO(); image.save(stream, format="PNG"); return stream.getvalue()
    O._alpha_pixels(raw(), row, bounds, 2)
    image.putpixel((1, 1), (120, 120, 120, 1))
    with pytest.raises(ValueError, match="nonzero alpha"): O._alpha_pixels(raw(), row, bounds, 2)


def test_16bit_nonzero_alpha_cannot_be_silently_rounded_to_transparent():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    # A real valid 16-bit RGBA PNG with alpha1/65535. Pillow's normal RGBA
    # decoder reduces that alpha to0, so checking its pixels alone is unsound.
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 16, 6, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress(b"\x00" + struct.pack(">HHHH", 65535, 65535, 65535, 1))) + chunk(b"IEND", b"")
    with Image.open(io.BytesIO(raw)) as image:
        assert image.mode == "RGBA" and image.getchannel("A").getextrema() == (0, 0)
    with pytest.raises(ValueError, match="8-bit alpha"):
        O._alpha_pixels(raw, {"position": [0.0, 0.0], "size": [1.0, 1.0]}, [0.0, 0.0, 1.0, 1.0], 1)


def test_animated_alpha_png_cannot_hide_later_nontransparent_frame():
    stream = io.BytesIO()
    Image.new("RGBA", (2, 2), (80, 90, 100, 0)).save(stream, format="PNG", save_all=True,
        append_images=[Image.new("RGBA", (2, 2), (80, 90, 100, 255))], duration=100, loop=0)
    with pytest.raises(ValueError, match="one complete"):
        O._alpha_pixels(stream.getvalue(), {"position": [0.0, 0.0], "size": [2.0, 2.0]}, [0.0, 0.0, 2.0, 2.0], 1)


def test_predispatch_alpha_capture_must_be_inside_journal_checkpoint_interval(playback):
    playback["controls"]["transparent_overlay"] = True
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    case = result["case"]; file = Path(case["evidence"]["journal"]["file"])
    journal = R.load_json(file)
    capture = journal["pre_dispatch"]["visibility"]["alpha_captures"][0]
    capture["started_monotonic"] = journal["before_finished_monotonic"] - .1
    R.write_json(file, journal); case["evidence"]["journal"] = P._pin(file)
    with pytest.raises(ValueError, match="outside"): P.validate_trial(playback["policy"], case, ACTOR)


@pytest.mark.parametrize("failure", [None, "different_title_foremost", "duplicate_title_geometry", "missing_title", "inexact_geometry"])
def test_ax_quartz_binding_requires_unique_literal_title_exact_geometry_and_foremost(failure):
    keys = SimpleNamespace(kCGWindowOwnerPID="pid", kCGWindowBounds="bounds", kCGWindowName="title", kCGWindowNumber="id")
    front = {"pid": 321, "id": 77, "title": "Current media", "bounds": {"X": 0, "Y": 0, "Width": 120, "Height": 80}}
    back = {"pid": 321, "id": 78, "title": "Older media", "bounds": copy.deepcopy(front["bounds"])}
    if failure == "different_title_foremost": front["title"], back["title"] = back["title"], front["title"]
    elif failure == "duplicate_title_geometry": back["title"] = front["title"]
    elif failure == "missing_title": del front["title"]
    elif failure == "inexact_geometry": front["bounds"]["X"] = .25
    if failure:
        with pytest.raises(ValueError):
            O._select_owned_window([front, back], {"pid": 321}, [0.0, 0.0], [120.0, 80.0], "Current media", "file:///local/current.fixture", keys)
    else:
        assert O._select_owned_window([front, back], {"pid": 321}, [0.0, 0.0], [120.0, 80.0], "Current media", "file:///local/current.fixture", keys) is front


@pytest.mark.parametrize("failure", [None, "unrelated_title", "duplicate_basename_below", "empty_title", "missing_title"])
def test_decorated_ax_title_binds_document_basename_and_historical_full_inventory(playback, failure):
    context = playback["context"]
    context["window"]["title"] = "Decorated document title (standard application metadata)"
    result = run(playback)
    assert result["status"] == "trial_measurement_validated", result
    case = result["case"]
    before_file = Path(case["evidence"]["before"]["file"])
    before = R.load_json(before_file)
    # The actual public control and images are retained; the historical reader
    # must recompute title binding/ambiguity from CG rows, including rows below.
    for sample in before["samples"]:
        for bookend in ("before", "after"):
            visibility = sample[bookend]["visibility"]
            selected = visibility["selected"]
            assert selected["title"] == Path(context["media"]["file"]).name
            if failure == "unrelated_title": selected["title"] = "A different document"
            elif failure == "duplicate_basename_below":
                visibility["below"] = [{**selected, "id": selected["id"] + 1}]
            elif failure == "empty_title": selected["title"] = ""
            elif failure == "missing_title": selected["title"] = None
    R.write_json(before_file, before)
    case["evidence"]["before"] = P._pin(before_file)
    if failure:
        with pytest.raises(ValueError, match="Quartz"):
            P.validate_trial(playback["policy"], case, ACTOR)
    else:
        assert P.validate_trial(playback["policy"], case, ACTOR)["trial_measurement_validated"]


def test_live_quartz_selection_accepts_exact_canonical_document_basename():
    keys = SimpleNamespace(kCGWindowOwnerPID="pid", kCGWindowBounds="bounds", kCGWindowName="title", kCGWindowNumber="id")
    front = {"pid": 321, "id": 77, "title": "public media.fixture", "bounds": {"X": 0, "Y": 0, "Width": 120, "Height": 100}}
    window = {"pid": 321, "position": [0.0, 0.0], "size": [120.0, 100.0],
              "title": "Document display metadata", "document": "file:///local/public%20media.fixture"}
    assert O._select_owned_window([front], {"pid": 321}, window["position"], window["size"], window["title"], window["document"], keys) is front
    for document in ("file:///local/../public%20media.fixture", "file://other-host/local/public%20media.fixture", "https://example.invalid/public%20media.fixture"):
        with pytest.raises(ValueError, match="canonical"):
            O._select_owned_window([front], {"pid": 321}, window["position"], window["size"], window["title"], document, keys)


def test_replay_without_setup_hook_observes_current_context_but_does_not_open_media(playback, monkeypatch):
    calls = []
    def current(observer, identity, *, deadline):
        calls.append(identity)
        playback["clock"].sleep(.01)
        return copy.deepcopy(playback["context"])
    monkeypatch.setattr(O, "current_context", current)
    result = P.run_trial(str(playback["app"]), ACTOR, playback["policy"], None, str(playback["out"]), timeout=10)
    assert result["status"] == "trial_measurement_validated", result
    assert result["case"]["phase"] == "replay" and result["case"]["inputs"] == {} and len(calls) == 1


@pytest.mark.parametrize("change", ["inputs", "callback", "restart", "stale_identity"])
def test_closed_intent_rejects_actor_inputs_callbacks_fake_restart_or_stale_epoch(playback, change):
    intent = copy.deepcopy(playback["intent"])
    if change == "inputs": intent["inputs"] = {"clip": "not-an-actor-argument"}
    elif change == "callback": intent["setup"] = {"python": "run anything"}
    elif change == "restart": intent["deployment"]["kind"] = "restart"
    else: intent["deployment"]["identity"]["launch_time"] += 1
    with pytest.raises(ValueError):
        P.run_trial(str(playback["app"]), ACTOR, playback["policy"], intent, str(playback["out"]), timeout=10)
    assert not playback["native"]["calls"]


@pytest.mark.parametrize("key,value", [("frames", 2), ("frames", True), ("min_nonblack_fraction", 0), ("still_max", 4),
                                       ("interval_seconds", 0), ("motion_min", float("nan"))])
def test_observer_thresholds_cannot_make_black_or_single_frame_a_success(key, value):
    observer = copy.deepcopy(OBSERVER); observer["video"][key] = value
    with pytest.raises(ValueError): O.validate_observer(observer)


def _copied_playback_sources(tmp_path, monkeypatch, *, legacy=False):
    sources = {}
    for name in P._source_paths():
        file = tmp_path / "relocated-package/execution" / Path(name).name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(Path(name).read_bytes())
        if legacy and file.name == "rea_playback_trial.py":
            file.write_bytes(b"Inert reviewed legacy producer fixture; never imported or executed.\n")
        sources[str(file)] = P._pin(file)
    if legacy:
        profile = {"execution/" + Path(name).name: {key: pin[key] for key in ("bytes", "sha256")}
                   for name, pin in sources.items()}
        monkeypatch.setattr(P, "_LEGACY_SOURCE_PROFILE", profile)
    return sources


def test_reviewed_playback_profile_is_immutable_and_current_closure_has_advanced():
    assert len(P._LEGACY_SOURCE_PROFILE) == 9
    assert P._LEGACY_SOURCE_PROFILE[P._VALIDATOR_ROLE] == {
        "bytes": 23972, "sha256": "e5411b36f90f535f8b00b72d2d7ec54e9b357ccd32d22bcc00c56a2048860d80"}
    assert R.digest(P._LEGACY_SOURCE_PROFILE) == "0af991cba733e2b46bc2f666fb765dae54928ce70e392877deb3ac1bc702d154"
    current, _ = P._producer_profile({name: P._pin(name) for name in P._source_paths()})
    assert set(current) == set(P._LEGACY_SOURCE_PROFILE)
    # Receiver preconditions deliberately changed B and the fixed native JS.
    # Their historical producer pins remain immutable rather than being rebased.
    for role in ("execution/rea_runtime_bridge.py", "execution/rea_runtime_agent.js"):
        assert current[role] != P._LEGACY_SOURCE_PROFILE[role]


def test_actual_historical_playback_profile_is_refused_after_core_advance_before_evidence_reads(tmp_path):
    # This tests a declared incompatible closure before file reads. It does not
    # supply or certify historical native observations, calls or source bytes.
    historical = {str(tmp_path / "historical-package" / role): {
        "file": str(tmp_path / "historical-package" / role), **pin}
        for role, pin in P._LEGACY_SOURCE_PROFILE.items()}
    current = {name: P._pin(name) for name in P._source_paths()}
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("incompatible historical profile reached evidence reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        P._admit_sources(NoReads(), historical, current)


def test_current_playback_producers_are_admitted_against_current_physical_source_closure():
    current = {name: P._pin(name) for name in P._source_paths()}
    reader = E._Reader()
    admission = P._admit_sources(reader, current, current)
    assert admission["admission"] == "current_exact_roles" and admission["legacy_profile"] is None
    assert admission["producer_sources"] == admission["validator_sources"] == current
    assert admission["old_sources_imported"] is False
    reader.recheck()


def _b2_playback_sources(tmp_path, *, version="b2"):
    """Retain actual reviewed git bytes, never import a historical module."""
    sources = {}
    profile = P._REVIEWED_EXECUTION_RESTORATION_PAIR["producer"] if version == "b2" else P._REVIEWED_V19_SOURCE_PROFILE
    head = "b2b6600a28216618e0a961008443170857934995" if version == "b2" else "b53f41905d5b9d3c81b71d897c2e1f91185fad2e"
    for role, expected in profile.items():
        raw = _reviewed_bytes(head, role)
        file = tmp_path / ("reviewed-" + version + "-package") / role
        file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
        pin = P._pin(file)
        assert {key: pin[key] for key in ("bytes", "sha256")} == expected
        sources[str(file)] = pin
    return sources


def _reviewed_playback_installed_sources(tmp_path, variant):
    """Copy retained source bytes for validation only; no old code is imported."""
    sources = {}
    profile, head = {
        "execution_restoration": (P._REVIEWED_EXECUTION_RESTORATION_PAIR["installed_core"], "b56a7bc6bda9eb8cef5d90d6a8464e13b52afa11"),
        "visual_verification": (P._REVIEWED_VISUAL_VERIFICATION_CORE, "e314b3c0627b5c094f7139e86a4cf104b6e0e9c9"),
        "generic_workflow": (P._REVIEWED_GENERIC_WORKFLOW_CORE, "c8b433d778a356bff983173bc8286d7d537f748e"),
    }[variant]
    for name in P._source_paths():
        role = "execution/" + Path(name).name
        if role != P._VALIDATOR_ROLE:
            raw = _reviewed_bytes(head, role)
        else:
            raw = Path(name).read_bytes()
        file = tmp_path / ("installed-" + variant) / role
        file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
        sources[str(file)] = P._pin(file)
        if role != P._VALIDATOR_ROLE:
            assert {key: sources[str(file)][key] for key in ("bytes", "sha256")} == profile[role]
    return sources


def _use_reviewed_playback_installed_core(tmp_path, monkeypatch):
    """Validate retained history with reviewed core fixtures; import no old code."""
    installed = _reviewed_playback_installed_sources(tmp_path, "generic_workflow")
    monkeypatch.setattr(P, "_source_paths", lambda: sorted(installed))
    return installed


def test_reviewed_playback_core_variants_preserve_original_pair_and_distinct_full_maps():
    assert R.digest(P._REVIEWED_EXECUTION_RESTORATION_PAIR) == "ee7985cc01dc425a7a064468b64d3a8bf0ec22ddebd864899b5baebaf2571f4d"
    assert R.digest(P._REVIEWED_VISUAL_VERIFICATION_CORE) == "5118a8bf0415a813eeaaaba23bda1e89cd65eec3d4e629f6a7429edf96c5e90d"
    old = P._REVIEWED_EXECUTION_RESTORATION_PAIR["installed_core"]
    new = P._REVIEWED_VISUAL_VERIFICATION_CORE
    assert set(old) == set(new) == set(P._LEGACY_SOURCE_PROFILE) - {P._VALIDATOR_ROLE}
    assert {role for role in old if old[role] != new[role]} == {"execution/rea_harness.py", "execution/rea_mapping.py"}
    generic = P._REVIEWED_GENERIC_WORKFLOW_CORE
    assert R.digest(generic) == "82d966e26fda31e2e15733bdae60095d2761977c5e8a8e9e6dc17d3414bc9f4d"
    assert set(generic) == set(new)
    assert {role for role in new if new[role] != generic[role]} == {
        "execution/rea_runtime_bridge.py", "execution/rea_runtime_agent.js"}


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("variant", ["execution_restoration", "visual_verification", "generic_workflow"])
def test_reviewed_playback_producers_admit_separate_exact_complete_installed_cores(tmp_path, version, variant):
    sources = _b2_playback_sources(tmp_path, version=version)
    current = _reviewed_playback_installed_sources(tmp_path, variant)
    reader = E._Reader()
    admission = P._admit_sources(reader, sources, current)
    expected = ("reviewed_b2_execution_restoration_exact_core" if version == "b2" else "reviewed_v19_fixed_playback_exact_core")
    assert admission["admission"] == expected and admission["old_sources_imported"] is False
    assert admission["legacy_profile"].endswith("/visual-verification-core") is (variant == "visual_verification")
    assert admission["legacy_profile"].endswith("/generic-workflow-core") is (variant == "generic_workflow")
    assert admission["producer_sources"] == sources and admission["validator_sources"] == current
    assert set(reader.files) == set(sources) | set(current)
    reader.recheck()


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("fault", ["modified_harness", "modified_mapping", "old_harness_new_mapping", "new_harness_old_mapping"])
def test_reviewed_playback_producers_refuse_modified_or_cross_variant_core_before_reads(tmp_path, version, fault):
    sources = _b2_playback_sources(tmp_path, version=version)
    current = _reviewed_playback_installed_sources(tmp_path, "visual_verification")
    name = "rea_harness.py" if "harness" in fault and fault != "new_harness_old_mapping" else "rea_mapping.py"
    installed = next(key for key in current if Path(key).name == name)
    if fault.startswith("modified"):
        Path(installed).write_bytes(Path(installed).read_bytes() + b"\n# unreviewed core byte\n")
        current[installed] = P._pin(installed)
    else:
        original = _reviewed_playback_installed_sources(tmp_path, "execution_restoration")
        old = next(key for key in original if Path(key).name == name)
        Path(installed).write_bytes(Path(old).read_bytes()); current[installed] = P._pin(installed)
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("unreviewed complete core reached source reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        P._admit_sources(NoReads(), sources, current)


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("role", ["rea_runtime_bridge.py", "rea_runtime_agent.js"])
@pytest.mark.parametrize("fault", ["modified", "visual_role"])
def test_generic_playback_core_refuses_unreviewed_bytes_and_mixed_roles_before_reads(tmp_path, version, role, fault):
    sources = _b2_playback_sources(tmp_path, version=version)
    current = _reviewed_playback_installed_sources(tmp_path, "generic_workflow")
    installed = next(key for key in current if Path(key).name == role)
    if fault == "modified":
        raw = Path(installed).read_bytes() + b"\n# unreviewed generic core\n"
    else:
        visual = _reviewed_playback_installed_sources(tmp_path, "visual_verification")
        raw = Path(next(key for key in visual if Path(key).name == role)).read_bytes()
    Path(installed).write_bytes(raw); current[installed] = P._pin(installed)
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("unreviewed or mixed generic core reached source reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        P._admit_sources(NoReads(), sources, current)


@pytest.mark.parametrize("version", ["b2", "v19"])
def test_historical_playback_producers_refuse_advanced_current_core_before_reads(tmp_path, version):
    sources = _b2_playback_sources(tmp_path, version=version)
    current = {name: P._pin(name) for name in P._source_paths()}
    profile, _ = P._producer_profile(current)
    assert P._reviewed_core_variant(profile) is None
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("unreviewed current core reached historical source reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        P._admit_sources(NoReads(), sources, current)


def test_actual_b2_playback_producers_admit_only_the_exact_reviewed_installed_core(tmp_path):
    assert R.digest(P._REVIEWED_EXECUTION_RESTORATION_PAIR["producer"]) == "09c8594454e037a118fddf5794fc4e758e2221ef719047f7bf78c0f91b6a285d"
    sources = _b2_playback_sources(tmp_path)
    current = _reviewed_playback_installed_sources(tmp_path, "generic_workflow")
    reader = E._Reader()
    admission = P._admit_sources(reader, sources, current)
    assert admission["admission"] == "reviewed_b2_execution_restoration_exact_core"
    assert admission["producer_sources"] == sources and admission["validator_sources"] == current
    assert admission["old_sources_imported"] is False and set(reader.files) == set(sources) | set(current)
    reader.recheck()


@pytest.mark.parametrize("fault", ["producer_pin", "producer_bytes", "mixed_profile", "installed_core"])
def test_actual_b2_playback_pair_refuses_tampered_or_mixed_closures(tmp_path, fault):
    sources = _b2_playback_sources(tmp_path)
    current = _reviewed_playback_installed_sources(tmp_path, "generic_workflow")
    reader = E._Reader()
    assert P._admit_sources(reader, sources, current)["admission"] == "reviewed_b2_execution_restoration_exact_core"
    reader.recheck()
    old = next(name for name in sources if Path(name).name == "rea_harness.py")
    if fault == "producer_pin": sources[old]["sha256"] = "0" * 64
    elif fault == "producer_bytes": Path(old).write_bytes(Path(old).read_bytes() + b"changed")
    elif fault == "mixed_profile":
        Path(old).write_bytes(Path(P.H.__file__).read_bytes()); sources[old] = P._pin(old)
    else:
        installed = next(name for name in current if Path(name).name == "rea_mapping.py")
        current[installed]["sha256"] = "0" * 64
    with pytest.raises(ValueError): P._admit_sources(E._Reader(), sources, current)


def test_b2_playback_source_migration_keeps_full_raw_trial_validation(playback, tmp_path, monkeypatch):
    result = run(playback)
    assert result["status"] == "trial_measurement_validated"
    _use_reviewed_playback_installed_core(tmp_path, monkeypatch)
    sources, adapter = _historical_adapter_fixture(tmp_path, version="b2")
    case = _rebind_history_case(result["case"], sources, adapter)
    report = P.validate_trial(playback["policy"], case, ACTOR)
    assert report["source_admission"]["admission"] == "reviewed_b2_execution_restoration_exact_core"
    after_file = Path(case["evidence"]["after"]["file"])
    after = R.load_json(after_file); after["deltas"] = [255.0, 255.0]
    R.write_json(after_file, after); case["evidence"]["after"] = P._pin(after_file)
    with pytest.raises(ValueError): P.validate_trial(playback["policy"], case, ACTOR)


def _historical_adapter_fixture(tmp_path, *, version="v19"):
    sources = _b2_playback_sources(tmp_path, version=version)
    asset = tmp_path / "inert-historical-adapter.js"
    asset.write_bytes(b"inert retained compiled fixture; never executed")
    profile = P._REVIEWED_V19_SOURCE_PROFILE if version == "v19" else P._REVIEWED_EXECUTION_RESTORATION_PAIR["producer"]
    source = profile["execution/rea_runtime_agent.js"]["sha256"]
    build = {"format": "anyuse.rea-runtime-adapter/1", "source_sha256": source,
             "bundle_sha256": R._file_hash(asset), "compiler": "inert fixture"}
    manifest = Path(str(asset) + ".manifest.json"); R.write_json(manifest, build)
    adapter = {**P._pin(asset), "source_sha256": source, "manifest": P._pin(manifest), "build": build,
               "transport": {"kind": "local"}}
    return sources, adapter


@pytest.mark.parametrize("version", ["b2", "v19"])
def test_historical_adapter_reopens_exact_sources_and_cannot_be_a_live_adapter(tmp_path, monkeypatch, version):
    assert R.digest(P._REVIEWED_V19_SOURCE_PROFILE) == "7b86fefa3c47d6c4efca5c6a0253fb184a41b8a6b1dba923e4a029d0ebfa842c"
    sources, adapter = _historical_adapter_fixture(tmp_path, version=version)
    _use_reviewed_playback_installed_core(tmp_path, monkeypatch)
    reader = E._Reader()
    installed = P._historical_adapter(reader, adapter, sources)
    assert all(installed[key] == adapter[key] for key in installed)
    assert adapter["file"] in reader.files and adapter["manifest"]["file"] in reader.files
    assert set(sources) <= set(reader.files)
    reader.recheck()
    with pytest.raises(B.Unavailable): B._adapter(adapter["file"])


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("fault", ["asset", "manifest_source", "full_build", "adjacent", "symlink", "hardlink", "producer_source", "other_profile"])
def test_historical_adapter_rejects_tamper_alias_and_profile_misuse(tmp_path, monkeypatch, version, fault):
    sources, adapter = _historical_adapter_fixture(tmp_path, version=version)
    _use_reviewed_playback_installed_core(tmp_path, monkeypatch)
    P._historical_adapter(E._Reader(), adapter, sources)
    asset, manifest = Path(adapter["file"]), Path(adapter["manifest"]["file"])
    if fault == "asset": asset.write_bytes(b"changed compiled fixture")
    elif fault == "manifest_source":
        build = {**adapter["build"], "source_sha256": "0" * 64}
        R.write_json(manifest, build); adapter.update(build=build, manifest=P._pin(manifest))
    elif fault == "full_build": adapter["build"] = {**adapter["build"], "compiler": "changed recorded build"}
    elif fault == "adjacent":
        other = tmp_path / "unrelated-manifest.json"; other.write_bytes(manifest.read_bytes()); adapter["manifest"] = P._pin(other)
    elif fault == "symlink":
        other = tmp_path / "aliased-compiled.fixture"; asset.rename(other); asset.symlink_to(other)
    elif fault == "hardlink":
        import os
        os.link(asset, tmp_path / "compiled-hardlink.fixture")
    elif fault == "producer_source":
        file = next(Path(name) for name in sources if Path(name).name == "rea_runtime_agent.js")
        file.write_bytes(file.read_bytes() + b"changed old JS")
    else: sources = _b2_playback_sources(tmp_path, version="v19" if version == "b2" else "b2")
    with pytest.raises((ValueError, OSError)): P._historical_adapter(E._Reader(), adapter, sources)


def _rebind_history_case(case, sources, adapter, *, method_class=None):
    case = copy.deepcopy(case)
    execution_file = Path(case["evidence"]["execution"]["file"])
    runtime = R.load_json(execution_file)
    config_file = execution_file.parent / "worker-request.json"
    config = R.load_json(config_file); config["adapter"] = adapter; R.write_json(config_file, config)
    runtime["adapter"] = adapter
    if method_class is not None:
        worker_file = Path(case["evidence"]["worker"]["file"])
        worker = R.load_json(worker_file); worker["operations"][0]["method"]["class"] = method_class
        R.write_json(worker_file, worker)
        case["evidence"]["worker"] = P._pin(worker_file)
        runtime["result"] = worker; runtime["worker"]["sha256"] = case["evidence"]["worker"]["sha256"]
    for row in runtime["artifacts"]:
        pin = P._pin(execution_file.parent / row["file"])
        row.update(bytes=pin["bytes"], sha256=pin["sha256"])
    R.write_json(execution_file, runtime); case["evidence"]["execution"] = P._pin(execution_file)
    journal_file = Path(case["evidence"]["journal"]["file"])
    journal = R.load_json(journal_file)
    journal.update(sources=sources, execution=case["evidence"]["execution"])
    R.write_json(journal_file, journal); case["evidence"]["journal"] = P._pin(journal_file)
    return case


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("class_scope", ["old_grammar", "new_root", "new_terminal"])
def test_real_historical_sources_keep_full_trial_checks_and_original_class_grammar(playback, tmp_path, monkeypatch, version, class_scope):
    actor = copy.deepcopy(ACTOR)
    if class_scope == "new_root": actor["receiver"]["class"] = "." + actor["receiver"]["class"]
    result = P.run_trial(str(playback["app"]), actor, playback["policy"], playback["intent"], str(playback["out"]), timeout=10)
    assert result["status"] == "trial_measurement_validated"
    _use_reviewed_playback_installed_core(tmp_path, monkeypatch)
    sources, adapter = _historical_adapter_fixture(tmp_path, version=version)
    case = _rebind_history_case(result["case"], sources, adapter,
        method_class=".NewlyAdmittedClass" if class_scope == "new_terminal" else None)
    if version == "v19" and class_scope != "old_grammar":
        with pytest.raises(ValueError, match="original receiver and observed class grammar"):
            P.validate_trial(playback["policy"], case, actor)
    else:
        report = P.validate_trial(playback["policy"], case, actor)
        expected = "reviewed_v19_fixed_playback_exact_core" if version == "v19" else "reviewed_b2_execution_restoration_exact_core"
        assert report["source_admission"]["admission"] == expected
        assert report["source_admission"]["legacy_profile"].endswith("/generic-workflow-core")
        assert report["native_calls"] == 4 and report["invocations"] == 1
        after_file = Path(case["evidence"]["after"]["file"])
        after = R.load_json(after_file); after["deltas"] = [255.0, 255.0]
        R.write_json(after_file, after); case["evidence"]["after"] = P._pin(after_file)
        with pytest.raises(ValueError): P.validate_trial(playback["policy"], case, actor)


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("fault", ["module", "actor", "native_calls", "cleanup"])
def test_exact_historical_adapters_keep_native_trial_gates_after_repinning(playback, tmp_path, monkeypatch, version, fault):
    result = run(playback)
    assert result["status"] == "trial_measurement_validated"
    _use_reviewed_playback_installed_core(tmp_path, monkeypatch)
    sources, adapter = _historical_adapter_fixture(tmp_path, version=version)
    case = _rebind_history_case(result["case"], sources, adapter)
    assert P.validate_trial(playback["policy"], case, ACTOR)["native_calls"] == 4
    worker_file = Path(result["case"]["evidence"]["worker"]["file"])
    worker = R.load_json(worker_file)
    if fault == "module": worker["operations"][0]["method"]["module"] = "/outside/foreign-handler"
    elif fault == "actor": worker["operations"][0]["method"]["selector"] = "otherPlayback:"
    elif fault == "native_calls": worker["native_calls"] += 1
    else: worker["cleanup"]["detached"] = False
    R.write_json(worker_file, worker)
    case = _rebind_history_case(case, sources, adapter,
        method_class=worker["operations"][0]["method"]["class"])
    with pytest.raises(ValueError): P.validate_trial(playback["policy"], case, ACTOR)


@pytest.mark.parametrize("legacy", [False, True])
def test_playback_source_relocation_admits_only_current_or_reviewed_complete_profile(tmp_path, monkeypatch, legacy):
    sources = _copied_playback_sources(tmp_path, monkeypatch, legacy=legacy)
    before = copy.deepcopy(sources)
    reader, admission = E._Reader(), {}
    assert P._sources(reader, sources, admission=admission) == before
    assert sources == before
    assert admission["admission"] == ("reviewed_v1_exact_core" if legacy else "current_exact_roles")
    assert admission["producer_sources"] == before and not admission["old_sources_imported"]
    assert set(admission["validator_sources"]) == set(P._source_paths())
    reader.recheck()


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "mixed_root", "key_file", "changed", "unknown_profile", "symlink", "hardlink"])
def test_playback_source_closure_rejects_role_alias_and_hash_failures(tmp_path, monkeypatch, fault):
    sources = _copied_playback_sources(tmp_path, monkeypatch)
    name = next(iter(sources))
    file = Path(name)
    if fault == "missing": sources.pop(name)
    elif fault == "extra":
        extra = file.with_name("unexpected.py"); extra.write_bytes(b"extra")
        sources[str(extra)] = P._pin(extra)
    elif fault == "duplicate":
        removed = next(key for key in sources if key != name); sources.pop(removed)
        other = tmp_path / "duplicate/execution" / file.name
        other.parent.mkdir(parents=True); other.write_bytes(file.read_bytes())
        sources[str(other)] = P._pin(other)
    elif fault == "mixed_root":
        other = tmp_path / "other/execution" / file.name
        other.parent.mkdir(parents=True); other.write_bytes(file.read_bytes())
        sources.pop(name); sources[str(other)] = P._pin(other)
    elif fault == "key_file": sources[name]["file"] = name + ".different"
    elif fault in {"changed", "unknown_profile"}:
        file.write_bytes(file.read_bytes() + b"changed")
        if fault == "unknown_profile": sources[name] = P._pin(file)
    elif fault == "symlink":
        original = file.with_name("outside-source.fixture"); file.rename(original); file.symlink_to(original)
    elif fault == "hardlink":
        import os
        os.link(file, file.with_name("alias-source.fixture"))
    with pytest.raises((ValueError, OSError)):
        P._sources(E._Reader(), sources)


def test_reviewed_playback_profile_does_not_authorize_changed_current_core(tmp_path, monkeypatch):
    sources = _copied_playback_sources(tmp_path, monkeypatch, legacy=True)
    original = P._pin
    def changed(path):
        pin = original(path)
        if Path(path).name == "rea_harness.py": pin["sha256"] = "0" * 64
        return pin
    monkeypatch.setattr(P, "_pin", changed)
    with pytest.raises(ValueError, match="compatible legacy"):
        P._sources(E._Reader(), sources)


def test_playback_source_files_remain_pinned_through_final_recheck(tmp_path, monkeypatch):
    sources = _copied_playback_sources(tmp_path, monkeypatch)
    reader = E._Reader()
    P._sources(reader, sources)
    file = Path(next(iter(sources)))
    file.write_bytes(file.read_bytes() + b"changed after admission")
    with pytest.raises(ValueError): reader.recheck()


def test_legacy_playback_source_admission_still_revalidates_actual_trial(playback, tmp_path, monkeypatch):
    result = run(playback)
    assert result["status"] == "trial_measurement_validated"
    case = copy.deepcopy(result["case"])
    file = Path(case["evidence"]["journal"]["file"])
    journal = R.load_json(file)
    journal["sources"] = _copied_playback_sources(tmp_path, monkeypatch, legacy=True)
    R.write_json(file, journal)
    case["evidence"]["journal"] = P._pin(file)
    source_bytes = {name: Path(name).read_bytes() for name in journal["sources"]}
    report = P.validate_trial(playback["policy"], case, ACTOR)
    assert report["source_admission"]["admission"] == "reviewed_v1_exact_core"
    assert report["native_calls"] == 4 and report["invocations"] == 1
    assert source_bytes == {name: Path(name).read_bytes() for name in journal["sources"]}
    # Admission cannot rescue a changed independently recorded pixel contract.
    after_file = Path(case["evidence"]["after"]["file"])
    after = R.load_json(after_file); after["deltas"] = [255.0, 255.0]
    R.write_json(after_file, after); case["evidence"]["after"] = P._pin(after_file)
    with pytest.raises(ValueError): P.validate_trial(playback["policy"], case, ACTOR)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
