"""Replay preparation and cleanup never need Apple Events for app/window changes."""

from pathlib import Path
import plistlib
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace

_PKGROOT = TemporaryDirectory(prefix="anyuse-window-tests-")
Path(_PKGROOT.name, "anyuse").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
sys.path.insert(0, _PKGROOT.name)

import pytest

from anyuse.context import accessibility as AX
from anyuse.execution import surface_discovery
from anyuse.memory import window_manager as WM
from anyuse.memory.environment import ReplayEnvironment
from anyuse.memory.models import WindowState


class _Value:
    def __init__(self, kind, pair):
        self.kind, self.pair = kind, tuple(pair)


class _App:
    def __init__(self, world, name, bundle, pid):
        self.world, self.name, self.bundle, self.pid = world, name, bundle, pid
        self.terminated, self.launched_at = False, float(pid)

    def localizedName(self):
        return self.name

    def bundleIdentifier(self):
        return self.bundle

    def processIdentifier(self):
        return self.pid

    def isTerminated(self):
        return self.terminated

    def launchDate(self):
        return self.launched_at

    def activateWithOptions_(self, options):
        self.world.activations.append((self.bundle, options))
        if self.world.activate_ok:
            self.world.front = self.bundle
        return True


class _World:
    def __init__(self):
        self.drive, self.refused, self.activate_ok = True, False, True
        self.front = "com.apple.Terminal"
        self.activations, self.launches, self.writes, self.spawned = [], [], [], []
        self.failed_attribute, self.ignore_writes, self.switch_window = None, False, False
        self.apps = [_App(self, "Terminal", "com.apple.Terminal", 10),
                     _App(self, "Schach", "com.apple.Chess", 42)]
        self.attrs = {}
        for app in self.apps:
            window = f"window-{app.pid}"
            self.attrs[("app", app.pid)] = {"AXFocusedWindow": window, "AXWindows": [window]}
            self.attrs[window] = {"AXPosition": _Value(1, (10, 20)), "AXSize": _Value(2, (800, 600))}

    def api(self):
        module = ModuleType("ApplicationServices")
        module.kAXValueCGPointType, module.kAXValueCGSizeType = 1, 2
        module.AXUIElementCreateApplication = lambda pid: ("app", pid)
        module.AXValueCreate = _Value

        def get_value(value, kind, none):
            assert none is None and kind == value.kind
            first, second = value.pair
            return True, (SimpleNamespace(x=first, y=second) if kind == 1 else
                          SimpleNamespace(width=first, height=second))

        def get_attribute(element, name, none):
            value = self.attrs.get(element, {}).get(name)
            return (0, value) if value is not None else (-25212, None)

        def set_attribute(element, name, value):
            self.writes.append((element, name, value.pair))
            if name == self.failed_attribute:
                return -25205
            if not self.ignore_writes:
                self.attrs[element][name] = value
                if name == "AXSize":
                    self.attrs[element]["AXPosition"] = _Value(1, (-1, -1))
            if self.switch_window:
                self.attrs[("app", 42)]["AXFocusedWindow"] = "different-window"
            return 0

        module.AXValueGetValue = get_value
        module.AXUIElementCopyAttributeValue = get_attribute
        module.AXUIElementSetAttributeValue = set_attribute
        return module

    def appkit(self):
        module = ModuleType("AppKit")
        workspace = SimpleNamespace(runningApplications=lambda: list(self.apps))

        def launch(bundle_id, options, params, identifier):
            self.launches.append(bundle_id)
            if self.activate_ok:
                self.front = bundle_id
            return True, None

        workspace.launchAppWithBundleIdentifier_options_additionalEventParamDescriptor_launchIdentifier_ = launch
        module.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: workspace)
        module.NSRunningApplication = SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=lambda bid: [app for app in self.apps if app.bundle == bid])
        module.NSApplicationActivateIgnoringOtherApps = 1
        return module


@pytest.fixture
def world(monkeypatch):
    world = _World()
    api, appkit = world.api(), world.appkit()
    monkeypatch.setitem(sys.modules, "ApplicationServices", api)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(AX, "_ax", lambda: api)
    monkeypatch.setattr(AX, "_appkit", lambda: appkit)
    monkeypatch.setattr(AX, "_drive_enabled", lambda: world.drive)
    monkeypatch.setattr(AX, "_interlock", lambda where: "blocked" if world.refused else None)
    monkeypatch.setattr(AX, "ACTIVATE_SETTLE_S", 0)
    monkeypatch.setattr(AX, "frontmost_reading", lambda: {
        "bundle_id": world.front, "front_source": "ax", "reads": {"ax": world.front}, "conflict": False})
    monkeypatch.setattr(WM, "_interlock_refused", lambda where: world.refused)
    monkeypatch.setattr(surface_discovery, "find_bundle", lambda name: None)
    monkeypatch.setattr(WM.WindowManager, "_window_sleep", staticmethod(lambda seconds: None))

    def no_spawn(*args, **kwargs):
        world.spawned.append(args)
        raise AssertionError("app/window replay must not spawn osascript")

    monkeypatch.setattr(WM.subprocess, "run", no_spawn)
    return world


def _state(**changes):
    bounds = {"x": 100, "y": 200, "width": 900, "height": 700}
    bounds.update(changes)
    return WindowState(app="Schach", bounds=bounds)


def test_localized_app_activates_by_bundle_with_live_confirmation(world):
    assert WM.WindowManager().bring_to_front("Schach") is True
    assert world.activations == [("com.apple.Chess", 1)]
    assert world.front == "com.apple.Chess" and world.spawned == []


def test_activation_requires_the_requested_app_to_become_foreground(world):
    world.activate_ok = False
    assert WM.WindowManager().bring_to_front("Schach") is False
    assert len(world.activations) == 1 and world.spawned == []


@pytest.mark.parametrize("gate", ["drive", "interlock"])
def test_existing_gates_stop_activation_and_geometry_writes(world, gate):
    if gate == "drive":
        world.drive = False
    else:
        world.refused = True
    wm = WM.WindowManager()
    assert wm.bring_to_front("Schach") is False
    assert wm.set_window_state(_state()) is False
    assert world.activations == [] and world.writes == [] and world.spawned == []


def test_window_restoration_resizes_then_positions_and_reads_back_the_same_window(world):
    assert WM.WindowManager().set_window_state(_state()) is True
    assert world.writes == [("window-42", "AXSize", (900, 700)),
                            ("window-42", "AXPosition", (100, 200))]
    assert world.attrs["window-42"]["AXPosition"].pair == (100, 200)
    assert world.spawned == []


def test_geometry_already_matching_does_not_write_again(world):
    assert WM.WindowManager().set_window_state(_state(x=10, y=20, width=800, height=600)) is True
    assert world.writes == [] and world.spawned == []


@pytest.mark.parametrize("attribute", ["AXSize", "AXPosition"])
def test_ax_write_failures_do_not_fall_back_to_applescript(world, attribute):
    world.failed_attribute = attribute
    assert WM.WindowManager().set_window_state(_state()) is False
    assert world.writes[-1][1] == attribute and world.spawned == []


def test_successful_return_codes_without_changed_bounds_are_not_success(world):
    world.ignore_writes = True
    assert WM.WindowManager().set_window_state(_state()) is False
    assert len(world.writes) == 2 and world.spawned == []


def test_focus_change_stops_before_writing_the_other_geometry_attribute(world):
    world.switch_window = True
    assert WM.WindowManager().set_window_state(_state()) is False
    assert [row[1] for row in world.writes] == ["AXSize"] and world.spawned == []


@pytest.mark.parametrize("change", [{"width": 0}, {"height": -1}, {"x": float("nan")}, {"y": True}])
def test_invalid_bounds_do_not_mutate_the_window(world, change):
    assert WM.WindowManager().set_window_state(_state(**change)) is False
    assert world.writes == [] and world.spawned == []


def test_absent_app_or_focused_window_returns_false_without_scripting(world):
    wm = WM.WindowManager()
    assert wm.bring_to_front("Missing App") is False
    assert wm.set_window_state(WindowState(app="Missing App", bounds=_state().bounds)) is False
    del world.attrs[("app", 42)]["AXFocusedWindow"]
    assert wm.set_window_state(_state()) is False
    assert world.writes == [] and world.spawned == []


def _installed_bundle(tmp_path, name, identifier):
    bundle = tmp_path / f"{name}.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier}))
    return str(bundle)


@pytest.mark.parametrize("name", ["Drawing", "Drawing.app"])
def test_nonrunning_installed_app_name_launches_by_its_bundle_metadata(world, monkeypatch, tmp_path, name):
    bundle = _installed_bundle(tmp_path, "Drawing", "org.example.Drawing")
    monkeypatch.setattr(surface_discovery, "find_bundle", lambda requested: bundle if requested == name else None)
    assert WM.WindowManager().bring_to_front(name) is True
    assert world.launches == ["org.example.Drawing"]
    assert world.front == "org.example.Drawing" and world.spawned == []


def test_english_bundle_name_finds_the_localized_running_app_for_both_operations(world, monkeypatch, tmp_path):
    bundle = _installed_bundle(tmp_path, "Chess", "com.apple.Chess")
    monkeypatch.setattr(surface_discovery, "find_bundle", lambda name: bundle if name == "Chess" else None)
    wm = WM.WindowManager()
    original = wm.get_window_state("Chess")
    assert original.app == "Chess" and original.bounds == {"x": 10, "y": 20, "width": 800, "height": 600}
    assert wm.bring_to_front("Chess") is True
    state = _state()
    state.app = "Chess"
    assert wm.set_window_state(state) is True
    assert world.activations == [("com.apple.Chess", 1)] and world.launches == []
    assert world.writes[-1][0] == "window-42" and world.spawned == []


def test_replay_prepare_and_cleanup_use_the_same_nonprompting_operations(world, monkeypatch):
    wm = WM.WindowManager()
    monkeypatch.setattr(wm, "get_dnd_state", lambda: True)
    monkeypatch.setattr("anyuse.memory.environment.time.sleep", lambda seconds: None)
    environment = ReplayEnvironment(window_manager=wm)
    saved = environment.prepare("Schach", _state().bounds)
    assert saved.original_frontmost_app == "Terminal"
    environment.restore(saved)
    assert world.attrs["window-42"]["AXPosition"].pair == (10, 20)
    assert world.attrs["window-42"]["AXSize"].pair == (800, 600)
    assert world.front == "com.apple.Terminal" and world.spawned == []


def test_default_focus_observer_is_unknown_and_never_uses_legacy_preferences(world):
    manager = WM.WindowManager()
    assert manager.get_dnd_state() is None
    assert manager.enable_dnd() is False and manager.disable_dnd() is False
    assert world.spawned == []


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("returncode,readback,expected", [(0, "target", True), (0, "old", False),
                                                          (0, "unknown", False), (1, "target", False)])
def test_dnd_command_requires_zero_exit_and_independent_known_readback(world, monkeypatch, enabled, returncode, readback, expected):
    manager = WM.WindowManager()
    state = [not enabled]
    monkeypatch.setattr(manager, "get_dnd_state", lambda: state[0])
    def run(argv, **kwargs):
        world.spawned.append(argv)
        state[0] = enabled if readback == "target" else not enabled if readback == "old" else None
        return SimpleNamespace(returncode=returncode)
    monkeypatch.setattr(WM.subprocess, "run", run)
    assert (manager.enable_dnd() if enabled else manager.disable_dnd()) is expected
    assert manager.last_dnd_change_attempted is True
    assert world.spawned == [["shortcuts", "run", "Turn On Focus" if enabled else "Turn Off Focus"]]


@pytest.mark.parametrize("blocked", ["drive", "interlock"])
def test_known_dnd_change_keeps_existing_drive_and_foreground_gates(world, monkeypatch, blocked):
    manager = WM.WindowManager()
    monkeypatch.setattr(manager, "get_dnd_state", lambda: False)
    if blocked == "drive":
        world.drive = False
    else:
        world.refused = True
    assert manager.enable_dnd() is False and world.spawned == []
    assert manager.last_dnd_change_attempted is False


def test_unreadable_focus_state_does_not_spawn_a_settings_command(world, monkeypatch):
    manager = WM.WindowManager()
    def unavailable():
        raise RuntimeError("no authoritative state")
    monkeypatch.setattr(manager, "get_dnd_state", unavailable)
    assert manager.enable_dnd() is False and manager.disable_dnd() is False
    assert world.spawned == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
