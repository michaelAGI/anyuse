"""Cleanup restores the captured live window even when replay changes focus."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

_PKGROOT = TemporaryDirectory(prefix="anyuse-window-identity-")
Path(_PKGROOT.name, "anyuse").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
sys.path.insert(0, _PKGROOT.name)

import pytest

from anyuse.context import accessibility as AX
from anyuse.memory.environment import EnvironmentState, ReplayEnvironment
from anyuse.memory.window_manager import WindowManager
from test_window_manager_no_prompt import world, _Value


@pytest.fixture
def replay(world, monkeypatch):
    manager = WindowManager()
    monkeypatch.setattr(manager, "get_dnd_state", lambda: True)
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    environment = ReplayEnvironment(window_manager=manager)
    saved = environment.prepare("Schach")
    assert "Schach" in saved._window_restore_points
    return world, manager, environment, saved


def _open_another_window(world):
    world.attrs["new-window"] = {"AXPosition": _Value(1, (300, 400)), "AXSize": _Value(2, (500, 600))}
    world.attrs[("app", 42)]["AXWindows"].append("new-window")
    world.attrs[("app", 42)]["AXFocusedWindow"] = "new-window"


def test_cleanup_restores_the_original_window_after_replay_opens_another(replay):
    world, manager, environment, saved = replay
    world.attrs["window-42"] = {"AXPosition": _Value(1, (100, 200)), "AXSize": _Value(2, (900, 700))}
    _open_another_window(world)
    environment.restore(saved)
    assert world.writes == [("window-42", "AXSize", (800, 600)), ("window-42", "AXPosition", (10, 20))]
    assert world.attrs["new-window"]["AXPosition"].pair == (300, 400)
    assert world.attrs["new-window"]["AXSize"].pair == (500, 600)
    assert world.attrs[("app", 42)]["AXFocusedWindow"] == "new-window"
    assert world.spawned == []


def test_closed_original_window_is_not_replaced_by_the_new_focused_window(replay, caplog):
    world, manager, environment, saved = replay
    _open_another_window(world)
    world.attrs[("app", 42)]["AXWindows"].remove("window-42")
    environment.restore(saved)
    assert world.writes == [] and world.spawned == []
    assert "Could not restore window state for Schach" in caplog.text


@pytest.mark.parametrize("change", ["terminated", "disappeared", "pid_reused", "unknown_launch_date"])
def test_cleanup_requires_the_original_process_instance(replay, caplog, change):
    world, manager, environment, saved = replay
    original = world.apps[1]
    if change == "terminated":
        original.terminated = True
    elif change == "disappeared":
        world.apps.remove(original)
    elif change == "pid_reused":
        original.launched_at += 1
    else:
        original.launched_at = None
    environment.restore(saved)
    assert world.writes == [] and world.spawned == []
    assert "original process/window or bounds could not be confirmed" in caplog.text


def test_window_disappearing_mid_resize_stops_the_remaining_write(replay, monkeypatch, caplog):
    world, manager, environment, saved = replay
    world.attrs["window-42"]["AXSize"] = _Value(2, (900, 700))
    api = AX._ax()
    original_set = api.AXUIElementSetAttributeValue

    def disappearing(element, attribute, value):
        result = original_set(element, attribute, value)
        world.attrs[("app", 42)]["AXWindows"] = []
        return result

    monkeypatch.setattr(api, "AXUIElementSetAttributeValue", disappearing)
    environment.restore(saved)
    assert [write[1] for write in world.writes] == ["AXSize"]
    assert "Could not restore window state for Schach" in caplog.text


def test_live_window_references_are_excluded_from_serialized_state(replay, caplog):
    world, manager, environment, saved = replay
    payload = saved.to_dict()
    serialized = json.dumps(payload)
    assert "_window_restore_points" not in payload and "window-42" not in serialized
    reloaded = EnvironmentState.from_dict(json.loads(serialized))
    assert reloaded._window_restore_points == {}
    environment.restore(reloaded)
    assert world.writes == []
    assert "original live window identity is unavailable" in caplog.text


def test_failed_front_app_restoration_is_reported(replay, caplog):
    world, manager, environment, saved = replay
    world.activate_ok = False
    environment.restore(saved)
    assert "Could not restore frontmost app Terminal: activation was not confirmed" in caplog.text
    assert world.front == "com.apple.Chess"


def test_disabled_drive_gate_stops_and_reports_cleanup(replay, caplog):
    world, manager, environment, saved = replay
    world.drive = False
    environment.restore(saved)
    assert world.writes == [] and len(world.activations) == 1
    assert "Could not restore window state for Schach" in caplog.text
    assert "Could not restore frontmost app Terminal" in caplog.text


def test_missing_appkit_during_cleanup_does_not_use_a_stale_window(replay, monkeypatch, caplog):
    world, manager, environment, saved = replay
    monkeypatch.setitem(sys.modules, "AppKit", None)
    environment.restore(saved)
    assert world.writes == [] and world.spawned == []
    assert "Could not restore window state for Schach" in caplog.text


def test_unconfirmed_preparation_activation_is_reported(world, monkeypatch, caplog):
    manager = WindowManager()
    monkeypatch.setattr(manager, "get_dnd_state", lambda: True)
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    world.activate_ok = False
    ReplayEnvironment(window_manager=manager).prepare("Schach")
    assert "Could not bring Schach to front: activation was not confirmed" in caplog.text


def test_unconfirmed_preparation_geometry_is_reported(world, monkeypatch, caplog):
    manager = WindowManager()
    monkeypatch.setattr(manager, "get_dnd_state", lambda: True)
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    world.ignore_writes = True
    ReplayEnvironment(window_manager=manager).prepare(
        "Schach", {"x": 100, "y": 200, "width": 900, "height": 700})
    assert "Could not resize window for Schach: bounds were not confirmed" in caplog.text


def test_unknown_process_launch_date_prevents_capture_and_is_reported_for_an_existing_window(world, monkeypatch, caplog):
    world.apps[1].launched_at = None
    manager = WindowManager()
    assert manager.capture_window_state("Schach") is None
    monkeypatch.setattr(manager, "get_dnd_state", lambda: True)
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    saved = ReplayEnvironment(window_manager=manager).prepare("Schach")
    assert saved._window_restore_points == {} and saved.original_window_states == {}
    assert "Could not capture original window identity for Schach" in caplog.text


def test_app_without_an_original_window_does_not_emit_a_capture_warning(world, monkeypatch, caplog):
    world.attrs[("app", 42)]["AXFocusedWindow"] = None
    world.attrs[("app", 42)]["AXWindows"] = []
    manager = WindowManager()
    monkeypatch.setattr(manager, "get_dnd_state", lambda: True)
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    saved = ReplayEnvironment(window_manager=manager).prepare("Schach")
    assert saved.original_window_states == {} and "Could not capture original window identity" not in caplog.text


def _field(report, name):
    return next(row for row in report.fields if row["field"] == name)


def test_restore_report_confirms_captured_controls_and_serializes_without_live_references(replay):
    world, manager, environment, saved = replay
    report = environment.restore(saved)
    assert report.status == "restored"
    assert {row["field"] for row in report.fields} == {"do_not_disturb", "window_geometry", "frontmost_app"}
    assert all(row["status"] == "restored" for row in report.fields)
    payload = report.to_dict()
    assert payload["scope"] == "captured_desktop_controls_only"
    assert "window-42" not in json.dumps(payload)
    payload["fields"].clear()
    assert len(report.fields) == 3


def test_lost_window_restoration_is_visible_as_partial(replay):
    world, manager, environment, saved = replay
    world.attrs[("app", 42)]["AXWindows"].remove("window-42")
    report = environment.restore(saved)
    assert report.status == "partial"
    assert _field(report, "window_geometry")["status"] == "unknown"
    assert world.writes == []


def test_unknown_original_dnd_never_changes_settings_during_prepare_or_restore(world, monkeypatch):
    manager = WindowManager()
    monkeypatch.setattr(manager, "get_dnd_state", lambda: None)
    monkeypatch.setattr(manager, "enable_dnd", lambda: pytest.fail("unknown DND must not be enabled"))
    monkeypatch.setattr(manager, "disable_dnd", lambda: pytest.fail("unknown DND must not be disabled"))
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    environment = ReplayEnvironment(manager)
    saved = environment.prepare("Schach")
    report = environment.restore(saved)
    assert saved.dnd_was_enabled is None and not saved.dnd_change_attempted
    assert report.status == "partial" and _field(report, "do_not_disturb")["status"] == "unknown"
    assert not _field(report, "do_not_disturb")["restoration_change_attempted"]
    assert world.spawned == []


def test_absent_legacy_dnd_value_and_mutation_tracking_remain_unknown():
    saved = EnvironmentState.from_dict({})
    assert saved.dnd_was_enabled is None and saved.dnd_change_attempted is False
    assert EnvironmentState.from_dict({"dnd_was_enabled": 0}).dnd_was_enabled is None
    assert EnvironmentState.from_dict(saved.to_dict()).dnd_was_enabled is None


def test_known_dnd_change_is_tracked_then_restored_with_fresh_readback(world, monkeypatch):
    manager = WindowManager()
    state, calls = [False], []
    monkeypatch.setattr(manager, "get_dnd_state", lambda: state[0])
    def change(value):
        calls.append(value)
        manager._last_dnd_change_attempted = True
        state[0] = value
        return True
    monkeypatch.setattr(manager, "enable_dnd", lambda: change(True))
    monkeypatch.setattr(manager, "disable_dnd", lambda: change(False))
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    environment = ReplayEnvironment(manager)
    saved = environment.prepare("Schach")
    assert saved.dnd_was_enabled is False and saved.dnd_change_attempted and saved.dnd_change_confirmed
    report = environment.restore(saved)
    assert report.status == "restored" and calls == [True, False]
    assert _field(report, "do_not_disturb")["observed"] is False
    assert _field(report, "do_not_disturb")["restoration_change_attempted"] is True


def test_refused_dnd_preparation_does_not_own_a_later_external_change(world, monkeypatch):
    manager = WindowManager()
    state = [False]
    monkeypatch.setattr(manager, "get_dnd_state", lambda: state[0])
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    world.drive = False
    environment = ReplayEnvironment(manager)
    saved = environment.prepare("Schach")
    assert not saved.dnd_change_attempted and not saved.dnd_change_confirmed
    world.drive, state[0] = True, True
    monkeypatch.setattr(manager, "disable_dnd", lambda: pytest.fail("a refused preparation owns no DND change"))
    report = environment.restore(saved)
    assert _field(report, "do_not_disturb")["status"] == "unknown"
    assert not _field(report, "do_not_disturb")["restoration_change_attempted"]
    assert world.spawned == []


def test_unknown_current_dnd_does_not_trigger_a_restoration_write(replay, monkeypatch):
    world, manager, environment, saved = replay
    saved.dnd_was_enabled, saved.dnd_change_attempted = False, True
    monkeypatch.setattr(manager, "get_dnd_state", lambda: None)
    monkeypatch.setattr(manager, "disable_dnd", lambda: pytest.fail("unknown current DND must not be mutated"))
    report = environment.restore(saved)
    assert report.status == "partial"
    assert _field(report, "do_not_disturb")["status"] == "unknown"
    assert _field(report, "do_not_disturb")["restoration_change_attempted"] is False


def test_dnd_without_preparation_ownership_is_not_changed_on_restore(replay, monkeypatch):
    world, manager, environment, saved = replay
    saved.dnd_was_enabled = False
    assert saved.dnd_change_attempted is False
    monkeypatch.setattr(manager, "disable_dnd", lambda: pytest.fail("no recorded DND change belongs to this replay"))
    report = environment.restore(saved)
    assert _field(report, "do_not_disturb")["status"] == "unknown"
    assert _field(report, "do_not_disturb")["observed"] is True


def test_all_unavailable_saved_controls_produce_unknown(world, monkeypatch):
    manager = WindowManager()
    monkeypatch.setattr(manager, "enable_dnd", lambda: pytest.fail("unavailable original state"))
    monkeypatch.setattr(manager, "disable_dnd", lambda: pytest.fail("unavailable original state"))
    report = ReplayEnvironment(manager).restore(EnvironmentState())
    assert report.status == "unknown"
    assert all(row["status"] == "unknown" for row in report.fields)
    assert world.writes == [] and world.activations == [] and world.spawned == []


def test_existing_positional_environment_state_fields_keep_their_meaning():
    bounds = {"Example": {"x": 1, "y": 2, "width": 300, "height": 200}}
    saved = EnvironmentState(False, bounds, "Terminal", 12.0)
    assert saved.dnd_was_enabled is False and saved.original_window_states == bounds
    assert saved.original_frontmost_app == "Terminal" and saved.timestamp == 12.0
    assert saved.dnd_change_attempted is False and saved.dnd_change_confirmed is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
