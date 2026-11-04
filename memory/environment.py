"""
Replay Environment — Clean Room Protocol for reliable workflow execution.

Before replay:
1. Save current state (for restoration)
2. Manage Do Not Disturb only with a known authoritative state
3. Bring target app to front (z-order)
4. Resize/position window
5. Verify state anchor (is the app in the expected state?)

After replay:
1. Report whether a preparation-owned DND change was restored
2. Restore window positions
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from anyuse.core.result import RestoreReport
from anyuse.memory.window_manager import WindowManager, WindowRestorePoint

logger = logging.getLogger(__name__)


@dataclass
class EnvironmentState:
    """
    Saved environment state for restoration after workflow replay.

    Captures available DND state, window geometry and the frontmost app.
    It does not capture document contents, undo history or private app state.
    """

    dnd_was_enabled: Optional[bool] = None
    original_window_states: Dict[str, dict] = field(default_factory=dict)
    original_frontmost_app: str = ""
    timestamp: float = 0.0
    dnd_change_attempted: bool = False
    dnd_change_confirmed: bool = False
    window_capture_status: Dict[str, str] = field(default_factory=dict)
    # Live AX references are valid only during this replay and are deliberately
    # excluded from to_dict/from_dict and from workflow persistence.
    _window_restore_points: Dict[str, WindowRestorePoint] = field(
        default_factory=dict, init=False, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "dnd_was_enabled": self.dnd_was_enabled,
            "dnd_change_attempted": self.dnd_change_attempted,
            "dnd_change_confirmed": self.dnd_change_confirmed,
            "window_capture_status": dict(self.window_capture_status),
            "original_window_states": self.original_window_states,
            "original_frontmost_app": self.original_frontmost_app,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EnvironmentState":
        return cls(
            dnd_was_enabled=(data.get("dnd_was_enabled") if type(data.get("dnd_was_enabled")) is bool else None),
            dnd_change_attempted=data.get("dnd_change_attempted") is True,
            dnd_change_confirmed=data.get("dnd_change_confirmed") is True,
            window_capture_status=dict(data.get("window_capture_status") or {}),
            original_window_states=data.get("original_window_states", {}),
            original_frontmost_app=data.get("original_frontmost_app", ""),
            timestamp=data.get("timestamp", 0.0),
        )


class ReplayEnvironment:
    """
    Clean Room Protocol — prepares a controlled environment for workflow replay.

    Before replay, this class:
    - Saves the current desktop state (frontmost app, window positions, DND)
    - Attempts DND preparation only when its original state is known
    - Brings the target application to the foreground
    - Optionally resizes/repositions the window to match the recording

    After replay, it reports restoration of those captured controls. Document
    contents, undo state, selection and internal app state are outside its scope.

    Uses WindowManager for window operations and MacOSContext for
    reading current system state.
    """

    def __init__(
        self,
        window_manager: Optional[WindowManager] = None,
    ):
        """
        Initialize the replay environment.

        Args:
            window_manager: WindowManager instance. Created if not provided.
        """
        self._wm = window_manager or WindowManager()

    def prepare(
        self,
        workflow_app: str,
        window_bounds: Optional[dict] = None,
    ) -> EnvironmentState:
        """
        Prepare the environment for workflow replay.

        Steps:
        1. Save current frontmost app
        2. Save current window state for the workflow's target app
        3. Attempt DND only with a known authoritative original state
        4. Bring the workflow app to the foreground
        5. Optionally resize/position the window to match the recording

        Args:
            workflow_app: Name of the application the workflow targets
                          (e.g. "Safari", "Finder").
            window_bounds: Optional dict with x, y, width, height to match
                           the recording's window geometry.

        Returns:
            EnvironmentState that can be passed to restore() later.
        """
        saved = EnvironmentState(timestamp=time.time())

        # 1. Save current frontmost app
        try:
            saved.original_frontmost_app = self._wm.get_frontmost_app()
        except Exception as exc:
            logger.warning("Could not determine frontmost app: %s", exc)
            saved.original_frontmost_app = ""

        # 2. Save current window state for the workflow's app
        saved.window_capture_status[workflow_app] = "unavailable"
        try:
            point = self._wm.capture_window_state(workflow_app)
            if point is not None:
                saved.original_window_states[workflow_app] = point.state.bounds.copy()
                saved._window_restore_points[workflow_app] = point
                saved.window_capture_status[workflow_app] = "captured"
            elif self._wm.get_window_state(workflow_app) is not None:
                logger.warning("Could not capture original window identity for %s; its geometry will not be "
                               "restored after replay", workflow_app)
        except Exception as exc:
            logger.warning(
                "Could not save window state for %s: %s", workflow_app, exc
            )

        # Unknown Focus state is not evidence that DND is off. Never mutate it
        # without a known original value that can later be checked/restored.
        try:
            observed = self._wm.get_dnd_state()
            saved.dnd_was_enabled = observed if type(observed) is bool else None
            if saved.dnd_was_enabled is False:
                try:
                    saved.dnd_change_confirmed = self.enable_dnd() is True
                finally:
                    saved.dnd_change_attempted = getattr(self._wm, "last_dnd_change_attempted", False) is True
        except Exception as exc:
            logger.warning("Could not prepare DND: %s", exc)

        # 4. Bring workflow app to front
        try:
            if not self._wm.bring_to_front(workflow_app):
                logger.warning("Could not bring %s to front: activation was not confirmed", workflow_app)
            # Small pause for the app to come forward and settle
            time.sleep(0.5)
        except Exception as exc:
            logger.warning("Could not bring %s to front: %s", workflow_app, exc)

        # 5. Resize/position window if bounds were provided
        if window_bounds:
            try:
                from anyuse.memory.models import WindowState

                target_state = WindowState(app=workflow_app, bounds=window_bounds)
                if not self._wm.set_window_state(target_state):
                    logger.warning("Could not resize window for %s: bounds were not confirmed", workflow_app)
                # Brief pause for window manager to finish the resize
                time.sleep(0.3)
            except Exception as exc:
                logger.warning(
                    "Could not resize window for %s: %s", workflow_app, exc
                )

        return saved

    def restore(self, saved_state: EnvironmentState) -> RestoreReport:
        """Restore captured desktop controls and report each observed outcome.

        Live geometry restoration keeps the original process/window identity.
        Unknown DND state never triggers a settings mutation. There is no
        document rollback, private-state reconstruction or replacement window.
        """
        fields: List[dict] = []
        dnd = {"field": "do_not_disturb", "status": "unknown",
               "expected": saved_state.dnd_was_enabled, "observed": None,
               "preparation_change_attempted": saved_state.dnd_change_attempted,
               "preparation_change_confirmed": saved_state.dnd_change_confirmed,
               "restoration_change_attempted": False}
        try:
            if type(saved_state.dnd_was_enabled) is not bool:
                dnd["reason"] = "Original Focus state is unavailable; no settings change attempted."
            else:
                observed = self._wm.get_dnd_state()
                dnd["observed"] = observed if type(observed) is bool else None
                if dnd["observed"] is None:
                    dnd["reason"] = "Current Focus state is unavailable; no settings change attempted."
                elif observed == saved_state.dnd_was_enabled:
                    dnd["status"] = "restored"
                elif not saved_state.dnd_change_attempted:
                    dnd["reason"] = "State differs without a recorded preparation-owned DND change; no mutation."
                else:
                    try:
                        changed = self.enable_dnd() if saved_state.dnd_was_enabled else self.disable_dnd()
                    finally:
                        dnd["restoration_change_attempted"] = getattr(self._wm, "last_dnd_change_attempted", False) is True
                    after = self._wm.get_dnd_state()
                    dnd["observed"] = after if type(after) is bool else None
                    dnd["status"] = ("restored" if changed is True and type(after) is bool and after is saved_state.dnd_was_enabled else
                                     "failed" if type(after) is bool else "unknown")
                    if dnd["status"] != "restored":
                        dnd["reason"] = "DND command and independent state readback did not both confirm restoration."
        except Exception as exc:
            dnd["reason"] = str(exc)
            logger.warning("Could not restore DND: %s", exc)
        fields.append(dnd)

        apps = dict.fromkeys([*saved_state.window_capture_status, *saved_state.original_window_states])
        for app_name in apps:
            row = {"field": "window_geometry", "app": app_name, "status": "unknown"}
            try:
                point = saved_state._window_restore_points.get(app_name)
                if point is None:
                    row["reason"] = "Original live window identity is unavailable."
                    logger.warning("Could not restore window state for %s: original live window identity is unavailable", app_name)
                elif self._wm.restore_window_state(point) is True:
                    row["status"] = "restored"
                else:
                    row["reason"] = "Original process/window or bounds could not be confirmed."
                    logger.warning("Could not restore window state for %s: original process/window or bounds "
                                   "could not be confirmed", app_name)
            except Exception as exc:
                row["reason"] = str(exc)
                logger.warning("Could not restore window state for %s: %s", app_name, exc)
            fields.append(row)

        front = {"field": "frontmost_app", "app": saved_state.original_frontmost_app, "status": "unknown"}
        try:
            if not saved_state.original_frontmost_app:
                front["reason"] = "Original frontmost app is unavailable."
            elif self._wm.bring_to_front(saved_state.original_frontmost_app) is True:
                front["status"] = "restored"
            else:
                front["reason"] = "Activation was not confirmed."
                logger.warning("Could not restore frontmost app %s: activation was not confirmed", saved_state.original_frontmost_app)
        except Exception as exc:
            front["reason"] = str(exc)
            logger.warning("Could not restore frontmost app %s: %s", saved_state.original_frontmost_app, exc)
        fields.append(front)
        return RestoreReport.from_fields(fields)

    # ------------------------------------------------------------------
    # DND helpers
    # ------------------------------------------------------------------

    def enable_dnd(self) -> bool:
        """
        Enable Do Not Disturb on macOS.

        Requires an authoritative known Focus state. Unavailable state leaves
        settings unchanged; a command alone never establishes success.

        Returns:
            True if DND was successfully enabled.
        """
        return self._wm.enable_dnd()

    def disable_dnd(self) -> bool:
        """
        Disable Do Not Disturb on macOS.

        Returns:
            True if DND was successfully disabled.
        """
        return self._wm.disable_dnd()

    # ------------------------------------------------------------------
    # State anchor verification
    # ------------------------------------------------------------------

    def verify_state_anchor(
        self,
        expected_elements: List[dict],
        workflow_app: str = "",
        ax_tree: Optional[List[dict]] = None,
    ) -> dict:
        """
        Build a structured report comparing expected vs actual UI state.

        This method does NOT make a threshold decision about whether the
        state is "close enough".  It returns raw structured data that the
        AI caller evaluates.

        Args:
            expected_elements: List of dicts describing expected UI elements.
                Each dict should have at least:
                    - role: str (e.g. "button", "textField")
                    - label: str (e.g. "Save", "Search")
                Optionally:
                    - value: str
                    - enabled: bool
            workflow_app: The app that should be frontmost.
            ax_tree: Optional accessibility tree snapshot. If provided,
                     it is searched for matches. If None, the method still
                     reports app/window status but element matching is
                     skipped.

        Returns:
            A dict with structured comparison data for AI evaluation::

                {
                    "expected_elements": [...],
                    "found_elements": [...],
                    "missing_elements": [...],
                    "unexpected_elements": [...],
                    "app_is_frontmost": bool,
                    "window_exists": bool,
                }
        """
        result: Dict[str, Any] = {
            "expected_elements": expected_elements,
            "found_elements": [],
            "missing_elements": [],
            "unexpected_elements": [],
            "app_is_frontmost": False,
            "window_exists": False,
        }

        # Check if the target app is frontmost
        if workflow_app:
            try:
                current_front = self._wm.get_frontmost_app()
                result["app_is_frontmost"] = (
                    current_front.lower() == workflow_app.lower()
                )
            except Exception:
                result["app_is_frontmost"] = False

            # Check if the target app has any window
            try:
                win_state = self._wm.get_window_state(workflow_app)
                result["window_exists"] = win_state is not None
            except Exception:
                result["window_exists"] = False

        # If no AX tree supplied, we can only report app/window status
        if ax_tree is None:
            result["missing_elements"] = list(expected_elements)
            return result

        # Build a lookup of AX tree elements for matching
        ax_lookup = _build_ax_lookup(ax_tree)

        found: List[dict] = []
        missing: List[dict] = []

        for expected in expected_elements:
            exp_role = expected.get("role", "").lower()
            exp_label = expected.get("label", "").lower()

            matched = False
            for ax_elem in ax_lookup:
                ax_role = ax_elem.get("role", "").lower()
                ax_label = ax_elem.get("label", "").lower()

                # Match by role + label substring
                role_match = (not exp_role) or (exp_role in ax_role)
                label_match = (not exp_label) or (exp_label in ax_label)

                if role_match and label_match:
                    found.append({
                        "expected": expected,
                        "actual": ax_elem,
                    })
                    matched = True
                    break

            if not matched:
                missing.append(expected)

        # Identify unexpected: AX elements that don't map to any expected
        expected_labels = {e.get("label", "").lower() for e in expected_elements}
        unexpected: List[dict] = []
        for ax_elem in ax_lookup:
            ax_label = ax_elem.get("label", "").lower()
            if ax_label and ax_label not in expected_labels:
                unexpected.append(ax_elem)

        result["found_elements"] = found
        result["missing_elements"] = missing
        result["unexpected_elements"] = unexpected

        return result

    def to_dict(self) -> dict:
        """Serialize the environment configuration (stateless snapshot)."""
        return {
            "class": "ReplayEnvironment",
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _build_ax_lookup(ax_tree: List[dict]) -> List[dict]:
    """
    Flatten an accessibility tree into a simple list of elements.

    Handles both flat lists and nested trees with a ``children`` key.
    """
    flat: List[dict] = []

    def _walk(nodes: List[dict]) -> None:
        for node in nodes:
            flat.append(node)
            children = node.get("children")
            if isinstance(children, list):
                _walk(children)

    _walk(ax_tree)
    return flat
