"""
Window Manager - Control window positions and sizes on macOS.

Used to restore window state before workflow replay.
"""
import subprocess
import math
import plistlib
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple
from dataclasses import dataclass, field

from anyuse.memory.models import WindowState


def _interlock_refused(where: str) -> bool:
    """Activating an app or moving its window changes what the person sees, so
    both go through the foreground interlock (execution/foreground_interlock.py)
    like every other input primitive. Reads are not checked."""
    from anyuse.execution.foreground_interlock import refusal
    return refusal(where) is not None


def _running_app(name: Optional[str] = None, bundle_id: Optional[str] = None):
    """The running application with that bundle id or localized name, or None."""
    try:
        from AppKit import NSWorkspace
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if bundle_id and str(app.bundleIdentifier() or "") == bundle_id:
                return app
            if name and str(app.localizedName() or "") == name:
                return app
    except Exception:  # noqa: BLE001
        return None
    return None


def _app_bundle_id(name: str) -> str:
    """Resolve a running display name, installed bundle name/path, or bundle id.

    Bundle metadata is read directly; app-name resolution never sends Apple
    events. This also maps an English bundle name such as Chess to a running
    app whose localized display name is Schach.
    """
    app = _running_app(name=name, bundle_id=name)
    if app is not None:
        return str(app.bundleIdentifier() or "")
    try:
        from anyuse.execution.surface_discovery import find_bundle

        bundle = find_bundle(name)
        if bundle:
            with (Path(bundle) / "Contents" / "Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            identifier = info.get("CFBundleIdentifier") if isinstance(info, dict) else None
            return identifier if isinstance(identifier, str) else ""
    except Exception:
        pass
    if (not (name or "").lower().endswith(".app")
            and re.fullmatch(r"(?:[A-Za-z0-9_-]+\.)+[A-Za-z0-9_-]+", name or "")):
        return name
    return ""


@dataclass(frozen=True)
class WindowRestorePoint:
    """In-process cleanup identity; never part of a serialized workflow."""

    state: WindowState
    pid: int
    bundle_id: str
    process: Any = field(repr=False, compare=False)
    window: Any = field(repr=False, compare=False)
    launch_date: Any = field(repr=False, compare=False)

    def process_is_live(self) -> bool:
        try:
            from AppKit import NSWorkspace

            if self.launch_date is None or self.process.isTerminated():
                return False
            return any(int(app.processIdentifier()) == self.pid
                       and str(app.bundleIdentifier() or "") == self.bundle_id
                       and app.launchDate() == self.launch_date
                       for app in NSWorkspace.sharedWorkspace().runningApplications())
        except Exception:
            return False


class WindowManager:
    """
    Manage replay activation and window geometry through Accessibility.
    
    Capabilities:
    - Get window positions and sizes
    - Resize and move windows
    - Bring apps to front
    - Restore recorded window state
    """

    _WINDOW_SETTLE = (0.05, 0.1, 0.2, 0.4)
    _window_sleep = staticmethod(time.sleep)
    
    def get_frontmost_app(self) -> str:
        """The frontmost application's name, read in-process: the AX focus, then the
        run-loop-ticked workspace and the menu-bar owner, then the window server
        (``context.accessibility.frontmost_reading``). Never System Events: asking it
        raises an Automation prompt on a fresh Mac and stalls until someone answers.
        "" when no source answers."""
        try:
            from anyuse.context.accessibility import frontmost_reading
            bundle_id = frontmost_reading().get("bundle_id")
        except Exception:  # noqa: BLE001 - no AX / AppKit: no answer
            return ""
        app = _running_app(bundle_id=bundle_id) if bundle_id else None
        return str(app.localizedName() or "") if app is not None else ""
    
    def get_window_state(self, app_name: str) -> Optional[WindowState]:
        """
        Get the current window state for an application.
        
        Args:
            app_name: Name of the application
        
        Returns:
            WindowState or None if app not found
        """
        # read through Accessibility (granted for any desktop run), not System Events
        # (its Automation prompt stalls a fresh Mac)
        app = _running_app(name=app_name, bundle_id=app_name) if app_name else None
        if app is None and app_name:
            bundle_id = _app_bundle_id(app_name)
            app = _running_app(bundle_id=bundle_id) if bundle_id else None
        if app is None:
            return None
        try:
            from anyuse.context.accessibility import focused_window_info
            info = focused_window_info(int(app.processIdentifier()))
        except Exception:  # noqa: BLE001
            return None
        frame = (info or {}).get("frame")
        if not frame or frame[0] is None or frame[1] is None:
            return None
        (x, y), (w, h) = frame
        return WindowState(app=app_name, bounds={"x": int(x), "y": int(y),
                                                 "width": int(w), "height": int(h)})
    
    def capture_window_state(self, app_name: str) -> Optional[WindowRestorePoint]:
        """Capture the exact live process/window and its bounds for this replay's cleanup."""
        try:
            from anyuse.context import accessibility as ax

            app = _running_app(name=app_name, bundle_id=app_name)
            if app is None:
                bundle_id = _app_bundle_id(app_name)
                app = _running_app(bundle_id=bundle_id) if bundle_id else None
            if app is None or app.isTerminated():
                return None
            pid, bundle_id = int(app.processIdentifier()), str(app.bundleIdentifier() or "")
            launch_date = app.launchDate()
            if not bundle_id or launch_date is None:
                return None
            tree = ax.AccessibilityTree(pid)
            root = ax._ax().AXUIElementCreateApplication(pid)
            window = tree.attr(root, "AXFocusedWindow")
            windows = tree.attr(root, "AXWindows")
            if window is None or windows is None or window not in windows:
                return None
            position = ax._ax_pair(tree.attr(window, "AXPosition"), "point")
            size = ax._ax_pair(tree.attr(window, "AXSize"), "size")
            if position is None or size is None:
                return None
            state = WindowState(app=app_name, bounds={"x": position[0], "y": position[1],
                                                       "width": size[0], "height": size[1]})
            return WindowRestorePoint(state, pid, bundle_id, app, window, launch_date)
        except Exception:
            return None

    def restore_window_state(self, point: WindowRestorePoint) -> bool:
        """Restore only the captured window while its original process/window still exists."""
        return self.set_window_state(point.state, _restore_point=point)

    def set_window_state(self, state: WindowState, *, _restore_point: Optional[WindowRestorePoint] = None) -> bool:
        """Set the focused window's bounds, or the original live window during cleanup.

        Replay cleanup uses this too. No Apple event is sent to System Events,
        so a missing Automation grant cannot open a consent dialog during
        restoration. Each geometry write keeps the drive gate and interlock.
        """
        if _interlock_refused(f"window_manager.set_window_state({state.app})"):
            return False
        try:
            from anyuse.context import accessibility as ax

            bounds = state.bounds
            values = [bounds[k] for k in ("x", "y", "width", "height")]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in values) or values[2] <= 0 or values[3] <= 0:
                return False
            if _restore_point is not None:
                if not _restore_point.process_is_live():
                    return False
                app = _restore_point.process
            else:
                app = _running_app(name=state.app, bundle_id=state.app)
                if app is None:
                    bundle_id = _app_bundle_id(state.app)
                    app = _running_app(bundle_id=bundle_id) if bundle_id else None
            if app is None:
                return False
            tree = ax.AccessibilityTree(int(app.processIdentifier()))
            where = f"window_manager.set_window_state({state.app})"
            if tree.actuation_refusal(where):
                return False
            api = ax._ax()
            root = api.AXUIElementCreateApplication(int(app.processIdentifier()))
            window = _restore_point.window if _restore_point is not None else tree.attr(root, "AXFocusedWindow")
            if window is None:
                return False

            position, size = tuple(values[:2]), tuple(values[2:])

            def same_window():
                if _restore_point is not None:
                    windows = tree.attr(root, "AXWindows")
                    return (_restore_point.process_is_live() and windows is not None and window in windows)
                return tree.attr(root, "AXFocusedWindow") == window

            def matches(attribute, kind, wanted):
                actual = ax._ax_pair(tree.attr(window, attribute), kind)
                return actual is not None and all(
                    math.isclose(a, b, abs_tol=0.5, rel_tol=0) for a, b in zip(actual, wanted))

            # Resize first: an app can adjust its position while resizing.
            # Normal placement requires the same focused window before each
            # write. Cleanup keeps its captured original window even after
            # replay focuses another one, and revalidates that original's life.
            for attribute, kind, wanted, value_type in (
                ("AXSize", "size", size, api.kAXValueCGSizeType),
                ("AXPosition", "point", position, api.kAXValueCGPointType),
            ):
                if not same_window():
                    return False
                if matches(attribute, kind, wanted):
                    continue
                value = api.AXValueCreate(value_type, wanted)
                if value is None or tree.actuation_refusal(where):
                    return False
                if api.AXUIElementSetAttributeValue(window, attribute, value) != 0:
                    return False

            def confirmed():
                return same_window() and matches("AXPosition", "point", position) and matches("AXSize", "size", size)

            if confirmed():
                return True
            for delay in self._WINDOW_SETTLE:
                self._window_sleep(delay)
                if confirmed():
                    return True
            return False
        except Exception:
            return False
    
    def bring_to_front(self, app_name: str) -> bool:
        """Activate the named running app, confirming it through the live AX focus.

        Installed app names and bundle identifiers can also launch through
        ``AccessibilityTree.activate_app``. Bundle metadata provides the name
        lookup; no AppleScript is used for lookup or activation.
        """
        if _interlock_refused(f"window_manager.bring_to_front({app_name})"):
            return False
        try:
            from anyuse.context.accessibility import AccessibilityTree

            tree = AccessibilityTree()
            if tree.actuation_refusal(f"window_manager.bring_to_front({app_name})"):
                return False
            bundle_id = _app_bundle_id(app_name)
            return bool(bundle_id) and tree.activate_app(bundle_id)
        except Exception:
            return False
    
    def prepare_for_replay(self, state: WindowState) -> bool:
        """
        Prepare window state for workflow replay.
        
        1. Bring app to front
        2. Restore window size/position
        
        Args:
            state: Recorded window state
        
        Returns:
            True if successful
        """
        # Bring to front
        if not self.bring_to_front(state.app):
            return False
        
        # Wait for app to come forward
        import time
        time.sleep(0.5)
        
        # Restore window state
        return self.set_window_state(state)
    
    def get_all_windows(self) -> Dict[str, List[str]]:
        """
        Get all open windows grouped by application.
        
        Returns:
            Dict mapping app names to lists of window titles
        """
        script = '''
        set windowInfo to ""
        tell application "System Events"
            set appProcesses to every process whose background only is false
            repeat with appProcess in appProcesses
                set appName to name of appProcess
                try
                    set appWindows to every window of appProcess
                    repeat with appWindow in appWindows
                        set windowTitle to name of appWindow
                        set windowInfo to windowInfo & appName & "|" & windowTitle & "\\n"
                    end repeat
                end try
            end repeat
        end tell
        return windowInfo
        '''
        
        try:
            result = self._run_applescript(script)
            windows: Dict[str, List[str]] = {}
            
            for line in result.strip().split("\n"):
                if "|" in line:
                    parts = line.split("|", 1)
                    app_name = parts[0].strip()
                    window_title = parts[1].strip() if len(parts) > 1 else ""
                    
                    if app_name not in windows:
                        windows[app_name] = []
                    if window_title:
                        windows[app_name].append(window_title)
            
            return windows
        except Exception:
            return {}
    
    # ------------------------------------------------------------------
    # Do Not Disturb (Focus) helpers
    # ------------------------------------------------------------------

    def enable_dnd(self) -> bool:
        """Request DND only with known state; confirm command and readback."""
        return self._set_dnd(True)

    def disable_dnd(self) -> bool:
        """Request DND only with known state; confirm command and readback."""
        return self._set_dnd(False)

    @property
    def last_dnd_change_attempted(self) -> bool:
        """Whether the last DND request reached its settings command."""
        return getattr(self, "_last_dnd_change_attempted", False) is True

    def _set_dnd(self, enabled: bool) -> bool:
        self._last_dnd_change_attempted = False
        try:
            before = self.get_dnd_state()
            if type(before) is not bool:
                return False
            if before is enabled:
                return True
            from anyuse.context.accessibility import AccessibilityTree

            if AccessibilityTree().actuation_refusal("window_manager.set_dnd"):
                return False
            self._last_dnd_change_attempted = True
            result = subprocess.run(
                ["shortcuts", "run", "Turn On Focus" if enabled else "Turn Off Focus"],
                capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return False
            after = self.get_dnd_state()
            return type(after) is bool and after is enabled
        except Exception:
            return False

    def get_dnd_state(self) -> Optional[bool]:
        """Return authoritative Focus state, or None when it cannot be read.

        No supported authoritative modern Focus observer is established here.
        The legacy notificationcenterui defaults key alone does not establish
        active Focus. Report unknown instead of treating a missing/stale value
        as off; default preparation/restoration therefore leave DND unchanged.
        """
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_applescript(self, script: str) -> str:
        """Run an AppleScript and return the output."""
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"AppleScript error: {result.stderr}")
        
        return result.stdout


if __name__ == "__main__":
    # Test window manager
    wm = WindowManager()
    
    print("Frontmost app:", wm.get_frontmost_app())
    
    print("\nAll windows:")
    for app, windows in wm.get_all_windows().items():
        print(f"  {app}: {windows}")

