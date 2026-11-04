"""
Workflow Recorder - Capture actions and visual anchors during execution.

Records successful task sequences as replayable workflows.
Includes StateGraphRecorder for capturing state graph data.
"""
import dataclasses
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

from anyuse.memory.models import (
    Workflow, WorkflowStep, VisualAnchor, WindowState,
    WorkflowTemplate, WorkflowIntent, WorkflowParam,
)
from anyuse.memory.window_manager import WindowManager
from anyuse.memory.storage import WorkflowStorage

logger = logging.getLogger(__name__)

# Accessibility tree (degrades gracefully when pyobjc/permission absent).
# NOTE: this previously imported ``AXNode`` and ``AX_AVAILABLE`` — neither of
# which context.accessibility exported — so the import ALWAYS failed and AX
# capture was silently disabled (zero recorded states). We import the real names;
# AXNode was unused here, so it is gone.
try:
    from anyuse.context.accessibility import AccessibilityTree, AXSnapshot, AX_AVAILABLE
except ImportError:
    AX_AVAILABLE = False
    AccessibilityTree = None  # type: ignore[assignment,misc]
    AXSnapshot = None  # type: ignore[assignment,misc]

# Conditional imports for state graph models
try:
    from anyuse.guides.state_graph import (
        StateGraph, UIState, UIFingerprint, UIElement, UITransition,
        TransitionImpl, compute_structural_hash, generate_state_id,
    )
    STATE_GRAPH_AVAILABLE = True
except ImportError:
    STATE_GRAPH_AVAILABLE = False

# Conditional import for macOS context
try:
    from anyuse.context.macos import MacOSContext
    MACOS_CONTEXT_AVAILABLE = True
except ImportError:
    MACOS_CONTEXT_AVAILABLE = False

# Browser app names for JS selector capture
BROWSER_APPS = frozenset({
    "Safari", "Google Chrome", "Chrome", "Firefox",
    "Microsoft Edge", "Arc", "Brave Browser", "Opera",
})

# Credential-like patterns for parameter detection
CREDENTIAL_PATTERNS = [
    re.compile(r'\$\{(\w+)\}'),            # ${variable}
    re.compile(r'password', re.IGNORECASE),
    re.compile(r'secret', re.IGNORECASE),
    re.compile(r'token', re.IGNORECASE),
    re.compile(r'api.?key', re.IGNORECASE),
]


class WorkflowRecorder:
    """
    Records actions during execution for later replay.
    
    Captures:
    - Action sequence
    - Visual anchors (cropped screenshots of click targets)
    - Window state
    - Task description
    
    Usage:
        recorder = WorkflowRecorder("my_workflow")
        recorder.start()
        
        # Actions are recorded...
        recorder.record_action("click_at", {"x": 100, "y": 200})
        
        workflow = recorder.save()
    """
    
    def __init__(
        self,
        name: str,
        description: str = "",
        storage: Optional[WorkflowStorage] = None
    ):
        """
        Initialize the recorder.
        
        Args:
            name: Name for the workflow
            description: Description of what the workflow does
            storage: Storage backend (uses default if not specified)
        """
        self.name = name
        self.description = description
        self.storage = storage or WorkflowStorage()
        self.window_manager = WindowManager()
        
        # Create workflow
        self.workflow = Workflow.create_new(name, description)
        
        # Set up directories
        self.workflow_dir = self.storage.get_workflow_dir(self.workflow.id)
        self.workflow_dir.mkdir(parents=True, exist_ok=True)
        
        # Anchor capture. Imported HERE, not at module top: matcher.py does
        # ``import pyautogui`` at its module top, so a top-level import made
        # ``import anyuse.memory.recorder`` impossible without the mouse/screen
        # actuation stack — even for a caller that only ever reads the AX tree.
        # anyuse/memory/__init__.py already defers this chain (PEP 562), but a
        # DIRECT ``import anyuse.memory.recorder`` bypassed that and still
        # pulled pyautogui in, which fails in a guest VM that has
        # pyobjc but no pyautogui.
        from anyuse.memory.matcher import AnchorCapture

        self.anchor_capture = AnchorCapture(self.workflow_dir)
        
        # State
        self.recording = False
        self.steps: List[WorkflowStep] = []
        self.start_time: Optional[float] = None
    
    def start(self):
        """Start recording."""
        self.recording = True
        self.start_time = time.time()
        self.steps = []
        
        # Capture initial window state (read in-process; never System Events)
        frontmost = self.window_manager.get_frontmost_app()
        if not frontmost:
            print("⚠️  The front app could not be read: this recording starts without a "
                  "window state (replay will not restore the window's position).")
        window_state = self.window_manager.get_window_state(frontmost) if frontmost else None
        if window_state:
            self.workflow.window_state = window_state
        
        print(f"🔴 Recording workflow: {self.name}")
    
    def record_action(
        self,
        action: str,
        args: Dict[str, Any],
        capture_anchor: bool = True,
        verification_checkpoint: bool = False,
        ai_reasoning: str = ""
    ):
        """
        Record an action.
        
        Args:
            action: Action name (e.g., "click_at", "type_text_at")
            args: Action arguments
            capture_anchor: Whether to capture visual anchor for click actions
            verification_checkpoint: Mark this step for Live API verification during replay
            ai_reasoning: AI's reasoning for this step (for debugging/learning)
        """
        if not self.recording:
            return
        
        step = WorkflowStep(
            action=action,
            args=args.copy(),
            description=f"{action}({self._format_args(args)})",
            timestamp=time.time(),
            ai_reasoning=ai_reasoning,
        )
        
        # Capture visual anchor for positional actions
        if (capture_anchor and action in ["click_at", "type_text_at", "hover_at"]
                and args.get("x") is not None and args.get("y") is not None):
            x = args.get("x")
            y = args.get("y")
            
            anchor = self.anchor_capture.capture_target(x, y)
            if anchor:
                step.visual_anchor = anchor
                step.use_visual_anchor = True
        
        self.steps.append(step)
        
        # Track verification checkpoints at workflow level
        if verification_checkpoint:
            step_index = len(self.steps) - 1
            if step_index not in self.workflow.verification_steps:
                self.workflow.verification_steps.append(step_index)
        
        print(f"  📝 Recorded: {step.description}")
    
    def stop(self) -> List[WorkflowStep]:
        """Stop recording and return steps."""
        self.recording = False
        print(f"⏹️ Recording stopped: {len(self.steps)} steps")
        return self.steps
    
    def save(self) -> Workflow:
        """
        Save the recorded workflow.
        
        Returns:
            The saved Workflow
        """
        self.stop()
        
        self.workflow.steps = self.steps
        self.workflow.updated_at = datetime.now().isoformat()
        
        # Save to storage
        self.storage.save(self.workflow)
        
        print(f"✓ Workflow '{self.workflow.name}' saved with {len(self.steps)} steps")
        return self.workflow
    
    def discard(self):
        """Discard the recording without saving."""
        self.recording = False
        self.steps = []
        
        # Clean up directory
        import shutil
        if self.workflow_dir.exists():
            shutil.rmtree(self.workflow_dir)
        
        print(f"🗑️ Recording discarded")
    
    def add_checkpoint(self, description: str = ""):
        """
        Add a verification checkpoint at the current step.
        
        Checkpoints trigger Live API verification during replay.
        The Live Watcher will verify the expected state after this step.
        """
        if not self.steps:
            return
        
        # Track at workflow level for Live API verification
        step_index = len(self.steps) - 1
        if step_index not in self.workflow.verification_steps:
            self.workflow.verification_steps.append(step_index)
        
        if description:
            self.steps[-1].description += f" [verify: {description}]"
    
    def _format_args(self, args: Dict[str, Any]) -> str:
        """Format args for display, hiding sensitive data."""
        safe_args = {}
        for k, v in args.items():
            if "password" in k.lower() or "credential" in k.lower():
                safe_args[k] = "***"
            elif isinstance(v, str) and len(v) > 30:
                safe_args[k] = v[:30] + "..."
            else:
                safe_args[k] = v
        
        return ", ".join(f"{k}={v!r}" for k, v in safe_args.items())


class RecordingContext:
    """
    Context manager for recording workflows.
    
    Usage:
        with RecordingContext("my_workflow") as recorder:
            # Run agent...
            recorder.record_action("click_at", {"x": 100, "y": 200})
        
        # Workflow is auto-saved on success, discarded on exception
    """
    
    def __init__(
        self,
        name: str,
        description: str = "",
        auto_save: bool = True
    ):
        self.recorder = WorkflowRecorder(name, description)
        self.auto_save = auto_save
        self.exception_occurred = False
    
    def __enter__(self) -> WorkflowRecorder:
        self.recorder.start()
        return self.recorder
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.exception_occurred = True
            print(f"⚠️ Recording aborted due to error: {exc_val}")
            self.recorder.discard()
        elif self.auto_save:
            self.recorder.save()
        
        return False  # Don't suppress exceptions


# ---------------------------------------------------------------------------
# State Graph Recorder
# ---------------------------------------------------------------------------

def _generate_state_id(elements: List[dict], app_name: str) -> str:
    """
    Deprecated alias. THE state id now lives in
    ``guides/state_graph.generate_state_id`` so that the recorder and the guide
    generator cannot disagree about it.

    Behaviour is unchanged: recorder-minted ids are identical byte-for-byte to
    the ones this function produced before the move.
    """
    return generate_state_id(elements, app_name)


def _build_fingerprint_from_snapshot(snapshot) -> "UIFingerprint":
    """
    Build a UIFingerprint from an AXSnapshot.

    Extracts actionable elements as required_elements and computes
    a structural hash from the role distribution.
    """
    if not STATE_GRAPH_AVAILABLE:
        return None  # type: ignore[return-value]

    # anchor_elements == actionable_elements unless ANYUSE_DRIFT_EXCLUDE_MENUBAR
    # is on, in which case menu-bar descendants are not required anchors.
    actionable = snapshot.anchor_elements if snapshot else []
    required_elements = []
    all_elements_dicts = []

    for node in actionable:
        entry = {"role": node.role}
        if node.title:
            entry["title"] = node.title
        required_elements.append(entry)

    # Build role-frequency dicts for structural hash
    for node in (snapshot.root_elements if snapshot else []):
        all_elements_dicts.append({"role": node.role, "title": node.title or ""})

    struct_hash = compute_structural_hash(all_elements_dicts) if all_elements_dicts else ""

    # Store the role-frequency explicitly (the hash is one-way) so near-duplicate
    # screens can be compared/merged rather than only exact-hash matched.
    role_frequency: Dict[str, int] = {}
    for el in all_elements_dicts:
        r = el.get("role", "unknown")
        role_frequency[r] = role_frequency.get(r, 0) + 1

    count = len(snapshot.root_elements) if snapshot else 0
    # the focused window's own count, and each menu's, so the drift gate can compare the
    # step's TARGET window (or menu) instead of the whole app: a second window of the app (a
    # Finder window is about 200 elements) puts every replay outside the app-wide ±20 range
    scoped: Dict[str, int] = {}
    focused = getattr(snapshot, "focused_tag", None) if snapshot else None
    if focused:
        for node in snapshot.root_elements:
            tag = getattr(node, "top_tag", None)
            if tag == focused:
                scoped["window"] = scoped.get("window", 0) + 1
            elif isinstance(tag, str) and tag.startswith("menu:"):
                scoped[tag] = scoped.get(tag, 0) + 1
    return UIFingerprint(
        required_elements=required_elements,
        element_count_range=(max(0, count - 20), count + 20),
        structural_hash=struct_hash,
        role_frequency=role_frequency,
        scoped_counts=scoped,
    )


def _build_ui_elements_from_snapshot(snapshot) -> List["UIElement"]:
    """
    Extract UIElement objects from an AXSnapshot's actionable elements.
    """
    if not STATE_GRAPH_AVAILABLE or snapshot is None:
        return []

    elements = []
    now_iso = datetime.now().isoformat()
    for node in snapshot.actionable_elements:
        # Determine element type from AX role
        role_to_type = {
            "AXButton": "button",
            "AXTextField": "text_field",
            "AXTextArea": "text_field",
            "AXCheckBox": "checkbox",
            "AXRadioButton": "radio",
            "AXPopUpButton": "popup_button",
            "AXComboBox": "combo_box",
            "AXSlider": "slider",
            "AXLink": "link",
            "AXMenuItem": "menu_item",
            "AXMenuBarItem": "menu_bar_item",
            "AXTab": "tab",
            "AXSwitch": "switch",
        }
        el_type = role_to_type.get(node.role, "button")

        # Determine available actions
        actions = []
        if node.role in ("AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXTab"):
            actions.append("press")
        if node.role in ("AXTextField", "AXTextArea", "AXComboBox"):
            actions.append("set_value")
        if node.role in ("AXCheckBox", "AXRadioButton", "AXSwitch"):
            actions.append("toggle")
        if node.role == "AXSlider":
            actions.extend(["increment", "decrement"])
        if node.role in ("AXPopUpButton",):
            actions.extend(["press", "show_menu"])

        accepts_input = node.role in ("AXTextField", "AXTextArea", "AXComboBox")

        # the selector as its dict (a Selector object makes the guide's JSON save fail with
        # "Object of type Selector is not JSON serializable")
        ax_path = node.to_selector().to_dict() if hasattr(node, "to_selector") else None

        el = UIElement(
            role=node.role,
            title=node.title or "",
            identifier=node.identifier,
            description=node.description or "",
            element_type=el_type,
            actions=actions,
            accepts_input=accepts_input,
            ax_path=ax_path,
            times_seen=1,
            last_seen=now_iso,
        )
        elements.append(el)
    return elements


def _detect_parameters(text: str) -> List["WorkflowParam"]:
    """
    Detect parameterized placeholders in text (e.g. ${amount}, ${email}).

    Returns a list of WorkflowParam objects for discovered placeholders.
    """
    if not STATE_GRAPH_AVAILABLE or not text:
        return []

    params = []
    seen = set()
    for match in CREDENTIAL_PATTERNS[0].finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        # Infer param type from name
        name_lower = name.lower()
        if any(kw in name_lower for kw in ("password", "secret", "token", "key")):
            param_type = "credential"
        elif any(kw in name_lower for kw in ("amount", "count", "price", "quantity")):
            param_type = "number"
        else:
            param_type = "string"

        params.append(WorkflowParam(
            name=name,
            param_type=param_type,
            required=True,
            description=f"Auto-detected parameter: {name}",
        ))
    return params


def _is_browser_app(app_name) -> bool:
    """Is ``app_name`` (a name, or a front-app dict carrying ``name``) a known browser?"""
    if isinstance(app_name, dict):
        app_name = app_name.get("name") or ""
    name = str(app_name or "").lower()
    return bool(name) and any(b.lower() in name for b in BROWSER_APPS)


def _incidental_app(app_name: str) -> bool:
    """An app a learned run starts from but never acts in: the terminal that launched it, a system
    prompt (``execution.agent_orchestrator.NEVER_FOCUS``). Its screen is no precondition of a
    replay, which may start anywhere."""
    if not app_name:
        return False
    try:
        from anyuse.execution.agent_orchestrator import NEVER_FOCUS
    except Exception:  # noqa: BLE001 - without the list every app counts
        return False
    return str(app_name).lower() in NEVER_FOCUS


class StateGraphRecorder:
    """
    Records workflow execution into both legacy Workflow and new
    StateGraph + WorkflowTemplate.

    Captures:
    - AX snapshots at each step -> UIStates
    - Transitions between states -> UITransitions with multiple implementations
    - Available modalities per element (AX, JS, coordinate)
    - Intent from AI reasoning
    - Parameterized data

    The recorder wraps the existing WorkflowRecorder for backward
    compatibility and adds state graph instrumentation on top.
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        storage: Optional[WorkflowStorage] = None,
        guide_storage=None,
    ):
        """
        Args:
            name: Name for the workflow.
            description: Description of what the workflow does.
            storage: WorkflowStorage backend (uses default if None).
            guide_storage: GuideStorage for persisting state graph to
                the application guide. May be None.
        """
        # Legacy recorder — we delegate step recording to it
        self._legacy_recorder = WorkflowRecorder(name, description, storage)
        self._guide_storage = guide_storage

        # State graph recording state
        self._ax_tree: Optional[Any] = None
        self._previous_snapshot: Optional[Any] = None
        self._previous_state_id: Optional[str] = None
        self._states_discovered: Dict[str, Any] = {}  # state_id -> UIState
        self._transitions_discovered: List[Any] = []  # UITransition list
        self._template_steps: List[Any] = []  # WorkflowIntent list
        self._template_params: List[Any] = []  # WorkflowParam list
        self._state_path: List[str] = []  # ordered state_id list

        # macOS context helper
        self._macos_ctx: Optional[Any] = None
        if MACOS_CONTEXT_AVAILABLE:
            self._macos_ctx = MacOSContext()

        # Current app info: the name and the bundle id apart (a whole front-app dict kept as the
        # name makes required_app a dict, and every guide lookup raises)
        self._current_app: str = ""
        self._current_bundle_id: str = ""
        self._current_pid: Optional[int] = None
        self._current_url: str = ""
        # the last app a recorded state was in that the run acts in: (name, bundle id)
        self._target_app: Tuple[str, str] = ("", "")

        # the action recorded last, until its after-state is read (the computer-use loop
        # records an action before it actuates it, so the after-state is read on its signal)
        self._pending: Optional[Dict[str, Any]] = None
        # the last after-state: (the id the recorded chain uses, the state's own id)
        self._last_after: Optional[Tuple[str, str]] = None
        # per template step: its action, keys, legacy step index and recorded transition
        self._steps_meta: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start recording. Capture initial AX state."""
        self._legacy_recorder.start()

        # Get frontmost app info
        self._refresh_app_context()

        # Capture initial AX snapshot
        initial_snapshot = self._take_snapshot()
        if initial_snapshot is not None:
            self._previous_snapshot = initial_snapshot
            state = self._snapshot_to_ui_state(initial_snapshot)
            if state is not None:
                self._states_discovered[state.state_id] = state
                self._previous_state_id = state.state_id
                self._state_path.append(state.state_id)
                self._last_after = (state.state_id, state.state_id)

        print(f"  State graph recording: AX {'available' if self._ax_tree else 'unavailable'}")

    def record_action(
        self,
        action: str,
        args: Dict[str, Any],
        capture_anchor: bool = True,
        ai_reasoning: str = "",
        expected_after: str = "",
        defer_after: bool = False,
    ):
        """
        Record an action with full state graph context.

        1. Take AX snapshot (before state)
        2. Record legacy step
        3. Create UIState from snapshot
        4. Query AX at click point -> get element details
        5. Build WorkflowIntent
        6. Build implementations (AX, JS, coordinate)

        ``defer_after``: the caller performs the action AFTER this call and then calls
        ``record_after_state()``, which reads the after-state and records the transition (the
        computer-use loop: an after-state read 0.3 s into this call is the BEFORE-state
        again, so every transition would be a self-loop). Without it the after-state is read 0.3 s
        later in this call, for a caller that records an action it already performed. An action
        whose after-state was never asked for gets it at the next ``record_action``/``save``.
        """
        if action in {"rea_action", "rea_plan"}:
            from anyuse.execution.rea_replay import literal_command
            args = literal_command(action, args)
        # the previous action's after-state, if its caller did not ask for it
        self._flush_pending()

        # 1. Refresh app context and take before-snapshot
        self._refresh_app_context()
        before_pid = self._current_pid
        before_snapshot = self._take_snapshot()

        # 2. Record legacy step
        legacy_steps = getattr(self._legacy_recorder, "steps", None)
        legacy_index = len(legacy_steps) if isinstance(legacy_steps, list) else None
        self._legacy_recorder.record_action(
            action=action,
            args=args,
            capture_anchor=capture_anchor,
            ai_reasoning=ai_reasoning,
        )

        # If state graph models are unavailable, stop here
        if not STATE_GRAPH_AVAILABLE:
            return

        # 3. Create UIState from before-snapshot
        before_state = None
        before_state_id = self._previous_state_id
        if before_snapshot is not None:
            before_state = self._snapshot_to_ui_state(before_snapshot)
            if before_state is not None:
                before_state_id = self._chain_before(before_state)
                if not self._state_path or self._state_path[-1] != before_state_id:
                    self._state_path.append(before_state_id)

        # 4. Query AX at click point and build implementations
        implementations = []
        target_element_name = ""
        input_data = None
        input_type = "click"
        element_node = None

        x = args.get("x")
        y = args.get("y")
        # a type into the focused element has no point: no lookup at a point, no coordinate rung
        has_point = x is not None and y is not None

        # 4a. AX implementation — query element at click coordinates.
        # find_at() returns a RAW element ref; describe_element() turns it into a
        # rich AXElementDesc (role/title/identifier/position/size) and to_selector()
        # builds a durable role:name:path Selector. The whole block is guarded so an
        # unexpected shape degrades the AX modality instead of aborting recording.
        element_node = None
        gesture_selector = None
        if has_point and self._ax_tree is not None and action in ("click_at", "type_text_at", "hover_at", "double_click_at", "right_click_at"):
            try:
                ref = self._ax_tree.find_at(int(x), int(y))
                if ref is not None:
                    element_node = self._ax_tree.describe_element(ref)
                    selector = self._ax_tree.to_selector(ref)
                    target_element_name = self._element_label(element_node)
                    ax_action = self._ax_action_for(action, element_node)
                    ax_impl = None if ax_action is None else TransitionImpl(
                        modality="ax",
                        command={
                            "selector": selector.to_dict(),
                            "element_path": selector.path,
                            "role": element_node.role,
                            "title": element_node.title or "",
                            "identifier": element_node.identifier or "",
                            "action": ax_action,
                            # Typed text for set_value replays — without this the
                            # deterministic path cleared the field instead of typing.
                            "text": args.get("text", "") or "",
                        },
                    )
                    if ax_impl is not None:
                        implementations.append(ax_impl)
                    else:
                        # the coordinate rung below still knows what was under the point
                        gesture_selector = selector.to_dict()
                    if before_state is not None and element_node.position and element_node.size:
                        self._update_element_position(before_state, element_node)
            except Exception as exc:
                import logging as _logging
                _logging.getLogger(__name__).debug("AX element capture at (%s,%s) failed: %s", x, y, exc)
                element_node = None

        # 4b. JS implementation — if browser is frontmost, try to get CSS selector
        if has_point and _is_browser_app(self._current_app) and action in ("click_at", "type_text_at"):
            js_selector = self._get_js_selector(int(x), int(y))
            if js_selector:
                js_command: Dict[str, Any] = {"selector": js_selector, "action": "click"}
                if action == "type_text_at":
                    text = args.get("text", "")
                    js_command["action"] = "set_value"
                    js_command["value"] = text
                js_impl = TransitionImpl(
                    modality="js",
                    command={"script": self._build_js_script(js_selector, js_command)},
                )
                implementations.append(js_impl)

                # Also store the selector on the UIElement if we found one
                if before_state is not None and element_node is not None:
                    self._set_element_js_selector(before_state, element_node, js_selector)

        # 4c. Coordinate implementation — window-relative coordinates
        window_bounds = self._get_window_bounds()
        if has_point and window_bounds and action in ("click_at", "type_text_at", "hover_at", "double_click_at", "right_click_at"):
            rel_x, rel_y = self._compute_relative_coords(
                int(x), int(y), window_bounds
            )
            coord_command = {
                "relative_x": rel_x,
                "relative_y": rel_y,
                "action": action,
                "absolute_x": int(x),
                "absolute_y": int(y),
                # Typed text for type_text_at replays (was dropped on replay).
                "text": args.get("text", "") or "",
            }
            ax_sel = next((i.command.get("selector") for i in implementations
                           if i.modality == "ax" and i.command.get("selector")), gesture_selector)
            if ax_sel:
                # what was under the point when it was clicked; the replay
                # hit-tests the point against it before clicking.
                coord_command["selector"] = dict(ax_sel)
            coord_impl = TransitionImpl(modality="coordinate", command=coord_command)
            implementations.append(coord_impl)

            # Store relative position on element
            if before_state is not None and element_node is not None:
                for el in before_state.elements:
                    if el.role == element_node.role and el.title == (element_node.title or ""):
                        el.relative_x = rel_x
                        el.relative_y = rel_y
                        break

        # 4d. a key chord, or text typed into the focused element, acts on no point: the coordinate
        # rung replays it as recorded (without an implementation the transition is never stored, and
        # the replay skips it). x/y stay None, so nothing is clicked first
        if action == "key_combination" and args.get("keys"):
            implementations.append(TransitionImpl(
                modality="coordinate", command={"action": "key_combination", "keys": args.get("keys")}))
        elif action == "type_text_at" and not has_point and args.get("text"):
            implementations.append(TransitionImpl(
                modality="coordinate", command={"action": action, "x": None, "y": None,
                                                "text": args.get("text"),
                                                "press_enter": bool(args.get("press_enter"))}))

        # 4e. a drag (without an implementation a replay skips it; in Chess that is the
        # move itself): two AXPress actions when both ends are buttons of a grid, then the drag
        # between the two points
        if action == "drag_to":
            implementations.extend(self._drag_implementations(args))
            ends = [i.command.get("selector") for i in implementations if i.modality == "ax"]
            if ends and not target_element_name:
                target_element_name = str((ends[0] or {}).get("name") or "")

        # 5. Detect input data and parameters
        if action in ("type_text_at", "type_text"):
            text = args.get("text", "")
            input_data = text
            input_type = "type"
            # Detect parameterised placeholders
            new_params = _detect_parameters(text)
            for p in new_params:
                if not any(ep.name == p.name for ep in self._template_params):
                    self._template_params.append(p)
        elif action in ("key", "hotkey", "key_combination"):
            input_data = args.get("keys", args.get("key", ""))
            input_type = "type"
        elif action in ("select",):
            input_data = args.get("value", args.get("option", ""))
            input_type = "select"
        else:
            input_type = "click"

        # A registered REA command is its own implementation. It has no AX,
        # script or coordinate alternative, and its inputs remain literal.
        if action in {"rea_action", "rea_plan"}:
            from anyuse.execution.rea_replay import literal_command
            implementations = [TransitionImpl(modality="rea", command={
                "action": action, "args": literal_command(action, args)})]

        # 6. Build WorkflowIntent
        intent_text = ai_reasoning if ai_reasoning else self._describe_action(action, args, target_element_name)
        intent = WorkflowIntent(
            intent=intent_text,
            target_element=target_element_name,
            expected_before_state=before_state_id or "",
            expected_after_state="",  # filled in when after-snapshot is taken
            expected_after=expected_after,
            input_data=input_data,
            input_type=input_type,
            ai_reasoning=ai_reasoning,
        )
        self._template_steps.append(intent)
        meta = {"action": action, "keys": args.get("keys") if action == "key_combination" else None,
                "legacy_index": legacy_index, "transition": None}
        self._steps_meta.append(meta)
        self._pending = {"intent": intent, "impls": implementations, "before_id": before_state_id,
                         "before_snapshot": before_snapshot, "before_pid": before_pid, "meta": meta}

        # 7. The after-state: on the caller's signal (defer_after), else after a short settle
        if not defer_after:
            time.sleep(0.3)
            self._flush_pending()

    def record_rea_action(self, name, inputs, *, catalog_sha256,
                          verification_tier="deterministic", ai_reasoning="", defer_after=True):
        """Record before dispatch; the caller discards the run if it fails.

        Catalog pins describe the demonstration; they never replace the fresh
        process epoch, proof validation or restoration declaration at replay.
        """
        from anyuse.execution.rea_replay import literal_command
        args = literal_command("rea_action", {"name": name, "inputs": inputs,
            "catalog_sha256": catalog_sha256, "verification_tier": verification_tier})
        self.record_action("rea_action", args, capture_anchor=False,
                           ai_reasoning=ai_reasoning, defer_after=defer_after)

    def record_rea_plan(self, steps, *, ai_reasoning="", defer_after=True):
        """Keep a deterministic batch as one recorded workflow transition."""
        from anyuse.execution.rea_replay import literal_command
        args = literal_command("rea_plan", {"steps": steps})
        self.record_action("rea_plan", args, capture_anchor=False,
                           ai_reasoning=ai_reasoning, defer_after=defer_after)

    def record_after_state(self, waited_s: Optional[float] = None):
        """
        Capture the after-state of the most recent action: the computer-use loop calls this
        once the action ran and the screen settled (``record_action(defer_after=True)``).
        ``waited_s``: how long the caller waited for the screen to settle (a launch); a longer
        wait becomes the legacy step's ``delay_after``, so the step player waits for it too.

        Without a pending action (an older caller): re-reads the last intent's after-state
        if it has none yet.
        """
        if self._flush_pending(waited_s=waited_s):
            return
        if not self._template_steps:
            return
        # Re-capture. The last intent may not have its after-state yet.
        last_intent = self._template_steps[-1]
        if last_intent.expected_after_state:
            return  # already captured
        self._capture_after_state(last_intent, [], self._previous_state_id)

    def _flush_pending(self, waited_s: Optional[float] = None) -> bool:
        """Read the after-state of the pending action and record its transition; False when
        no action is pending."""
        pending, self._pending = getattr(self, "_pending", None), None
        if pending is None:
            return False
        self._capture_after_state(pending["intent"], pending["impls"], pending["before_id"],
                                  before_snapshot=pending["before_snapshot"],
                                  before_pid=pending["before_pid"], meta=pending["meta"])
        idx = pending["meta"].get("legacy_index")
        steps = getattr(self._legacy_recorder, "steps", None)
        if waited_s and idx is not None and isinstance(steps, list) and idx < len(steps):
            steps[idx].delay_after = max(float(steps[idx].delay_after or 0.0), round(float(waited_s), 2))
        return True

    def stop(self) -> List[WorkflowStep]:
        """Stop recording and return legacy steps."""
        self._flush_pending()
        return self._legacy_recorder.stop()

    def save(self) -> Tuple[Workflow, Optional[WorkflowTemplate]]:
        """
        Save both the legacy workflow and the new WorkflowTemplate.

        Also updates the guide's state graph if guide_storage is
        available.

        Returns:
            (Workflow, WorkflowTemplate or None)
        """
        self._flush_pending()

        # Preserve intentional Returns and text layout exactly as demonstrated.
        # Launch settling prevents stale-launch keys; saving does not rewrite them.
        # Save legacy workflow
        legacy_workflow = self._legacy_recorder.save()

        # Build WorkflowTemplate
        template = None
        if STATE_GRAPH_AVAILABLE and self._template_steps:
            template = WorkflowTemplate.create_new(
                name=legacy_workflow.name,
                description=legacy_workflow.description,
            )
            template.steps = list(self._template_steps)
            template.parameters = list(self._template_params)
            template.state_path = list(self._state_path)
            template.entry_states = [self._state_path[0]] if self._state_path else []
            # the NAME of the app the run acted in last (never the front-app dict)
            template.required_app = self._target_app[0] or self._current_app
            template.required_bundle_id = (self._target_app[1] if self._target_app[0]
                                           else self._current_bundle_id)
            template.trigger_phrases = list(legacy_workflow.trigger_phrases)
            template.tags = list(legacy_workflow.tags)
            # the demonstration's own states and transitions, in step order, kept with the
            # template: a fresh install has no app guide, and a shared guide merges other
            # recordings' commands into the same transitions. Only a complete record (one
            # transition per step) is kept; the replay refuses a partial one.
            recorded = self._recorded_graph()
            if hasattr(template, "recorded_graph"):
                # An incomplete new recording must not fall back to a shared
                # guide as though it were an older, graphless template.
                template.recorded_graph = recorded or {"states": {}, "transitions": []}

            # Save template alongside the legacy workflow
            self._save_template(template, legacy_workflow)

        # Update guide's state graph
        if self._guide_storage is not None and STATE_GRAPH_AVAILABLE:
            self._update_guide_state_graph()

        step_count = len(self._template_steps)
        state_count = len(self._states_discovered)
        trans_count = len(self._transitions_discovered)
        print(
            f"  State graph: {state_count} states, "
            f"{trans_count} transitions, {step_count} intents"
        )
        return legacy_workflow, template

    def _recorded_graph(self) -> Optional[Dict[str, Any]]:
        """``StateGraph.to_dict()`` of this recording alone: the states its steps start and end
        in, and one transition per step in step order (a deep copy, so replay statistics never
        write into it). None when a step recorded no transition."""
        transitions = []
        for intent in self._template_steps:
            match = [t for t in self._transitions_discovered
                     if (t.from_state, t.to_state, t.intent)
                     == (intent.expected_before_state, intent.expected_after_state, intent.intent)
                     and all(t is not seen for seen in transitions)]
            if not match:
                return None
            transitions.append(match[0])
        used = {s for t in transitions for s in (t.from_state, t.to_state)}
        states = {sid: st for sid, st in self._states_discovered.items() if sid in used}
        if len(states) != len(used):
            return None
        graph = StateGraph(states=states, transitions=transitions).to_dict()
        from anyuse.guides.storage import _jsonable
        return json.loads(json.dumps(graph, default=_jsonable))

    def discard(self):
        """Discard the recording without saving."""
        self._legacy_recorder.discard()
        self._pending = None
        self._last_after = None
        self._steps_meta = []
        self._states_discovered.clear()
        self._transitions_discovered.clear()
        self._template_steps.clear()
        self._template_params.clear()
        self._state_path.clear()

    # ------------------------------------------------------------------
    # Properties for inspection
    # ------------------------------------------------------------------

    @property
    def states_discovered(self) -> Dict[str, Any]:
        """Return discovered UIState objects keyed by state_id."""
        return dict(self._states_discovered)

    @property
    def transitions_discovered(self) -> List[Any]:
        """Return discovered UITransition objects."""
        return list(self._transitions_discovered)

    @property
    def template_steps(self) -> List[Any]:
        """Return WorkflowIntent steps."""
        return list(self._template_steps)

    @property
    def template_params(self) -> List[Any]:
        """Return detected WorkflowParam objects."""
        return list(self._template_params)

    # ------------------------------------------------------------------
    # Internal helpers — AX tree interaction
    # ------------------------------------------------------------------

    #: polls of the live front-app reading while no source answers (AX gives no value for
    #: moments after a launch)
    FRONT_RETRY_S = (0.1, 0.2, 0.3)

    def _refresh_app_context(self):
        """Refresh the current front app (name, bundle id, pid) and the URL of a browser.

        Read LIVE (``MacOSContext.get_front_app_live``: AX first, the window server
        cross-checked by the app's AX off the main thread, never the untickled NSWorkspace
        read, which in a long-running process keeps the app that was in front at process start:
        every step of a computer-use run would be attributed to the launching Terminal, so a learned
        template would have one state and never replay without the model). When no source names the
        front app, the step gets no AX state (pid None) rather than the previous app's."""
        if not MACOS_CONTEXT_AVAILABLE or self._macos_ctx is None:
            return
        front = None
        try:
            live = getattr(self._macos_ctx, "get_front_app_live", None)
            if live is not None:
                front = live()
                for delay in self.FRONT_RETRY_S:
                    if front:
                        break
                    time.sleep(delay)
                    front = live()
            else:
                app = self._macos_ctx.get_frontmost_app()
                front = app if isinstance(app, dict) else {"name": str(app or ""), "bundle_id": "",
                                                           "pid": self._macos_ctx.get_frontmost_pid()}
        except Exception as exc:  # noqa: BLE001 - a step without a front app has no AX state
            logger.warning("the front app could not be read: %s", exc)
            front = None
        if not front or front.get("pid") is None:
            logger.info("no source named the front app; this step is recorded without an AX state")
            self._current_pid = None
            return
        self._current_app = str(front.get("name") or front.get("app") or "")
        self._current_bundle_id = str(front.get("bundle_id") or "")
        self._current_pid = int(front["pid"])
        if self._current_app and not _incidental_app(self._current_app):
            self._target_app = (self._current_app, self._current_bundle_id)
        if _is_browser_app(self._current_app):
            try:
                url = self._macos_ctx.get_browser_url(self._current_app)
            except TypeError:
                url = self._macos_ctx.get_browser_url()
            except Exception as exc:  # noqa: BLE001
                logger.debug("browser URL not read: %s", exc)
                url = None
            if url:
                self._current_url = url

    def _take_snapshot(self) -> Optional[Any]:
        """
        Take an AX snapshot of the frontmost application.

        Returns None if AX is unavailable, permission denied, or any
        error occurs. Callers must handle the None case.
        """
        if not AX_AVAILABLE or self._current_pid is None:
            return None
        try:
            self._ax_tree = AccessibilityTree(self._current_pid)
            return self._ax_tree.snapshot()
        except Exception:
            self._ax_tree = None
            return None

    def _snapshot_to_ui_state(self, snapshot) -> Optional[Any]:
        """
        Convert an AXSnapshot to a UIState.

        A state of an incidental app (``_incidental_app``: the terminal that launched the run, a
        system prompt) keeps no elements and an empty fingerprint: the drift gate has no
        baseline for it, so a replay may start from any app, and nothing of the terminal's
        window (its command lines) is stored.
        """
        if not STATE_GRAPH_AVAILABLE or snapshot is None:
            return None

        app = snapshot.app_name or self._current_app
        now_iso = datetime.now().isoformat()
        if _incidental_app(app):
            return UIState(state_id=_generate_state_id([], app), app=app, fingerprint=UIFingerprint(),
                           times_seen=1, last_seen=now_iso)

        fingerprint = _build_fingerprint_from_snapshot(snapshot)
        if fingerprint is None:
            return None

        # Generate stable state ID from the actionable elements
        actionable_dicts = [
            {"role": n.role, "title": n.title or ""}
            for n in snapshot.actionable_elements
        ]
        state_id = _generate_state_id(actionable_dicts, app)

        # Build UIElements
        elements = _build_ui_elements_from_snapshot(snapshot)

        state = UIState(
            state_id=state_id,
            app=app,
            fingerprint=fingerprint,
            elements=elements,
            times_seen=1,
            last_seen=now_iso,
        )

        # Add URL / title patterns
        if self._current_url:
            state.url_patterns.append(self._current_url)
        if snapshot.app_name:
            state.title_patterns.append(snapshot.app_name)

        return state

    # -- the recorded chain -------------------------------------------------------------------
    # The replay walks ``find_path(state_path[0], state_path[-1])``: the SHORTEST path. A step
    # that changes no AX identity (typing into a document: the state id is built from roles and
    # titles, not values) was a self-loop that path never took, and a return to a state seen
    # before let it skip the steps in between. So the chain a recording keeps never revisits a
    # state: such a step's after-state is an ALIAS ``<state id>~<digest>``, a copy of the same
    # screen under a new id, and the next step continues from the alias. The digest comes from
    # the chain position and the step, so the same recording always mints the same ids.

    def _alias(self, state, key: str) -> str:
        alias_id = f"{state.state_id}~{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"
        if alias_id not in self._states_discovered:
            self._states_discovered[alias_id] = dataclasses.replace(state, state_id=alias_id)
        return alias_id

    def _register(self, state) -> str:
        self._states_discovered[state.state_id] = state
        return state.state_id

    def _chain_before(self, state) -> str:
        """The chain id of the screen an action starts on."""
        last = getattr(self, "_last_after", None)
        if last is not None and last[1] == state.state_id:
            # the screen the previous step left (an alias stays the alias)
            if last[0] == state.state_id:
                self._register(state)
            return last[0]
        if state.state_id not in self._state_path:
            return self._register(state)
        self._register(state)
        return self._alias(state, f"return|{self._previous_state_id}|{len(self._state_path)}")

    def _chain_after(self, state, before_id: Optional[str], implementations: List[Any]) -> str:
        """The chain id of the screen an action left: the state's own id on a first visit, an
        alias on a self-loop or a return to a state already on the path."""
        before_real = (before_id or "").split("~", 1)[0]
        if state.state_id != before_real and state.state_id not in self._state_path:
            return self._register(state)
        self._register(state)
        step = [(i.modality, {k: v for k, v in (i.command or {}).items() if k != "expect"})
                for i in implementations or []]
        return self._alias(state, json.dumps([before_id, step], sort_keys=True, default=str))

    def _capture_after_state(
        self,
        intent: Any,
        implementations: List[Any],
        before_state_id: Optional[str],
        before_snapshot: Any = None,
        before_pid: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ):
        """
        Capture the after-snapshot, build the after-state, record
        the transition, and fill in the intent's expected_after_state.

        The demonstration's own before/after diff
        becomes each UI implementation's DECLARED expected effect
        (``command["expect"]``, ``execution/step_effect.effect_from_diff``):
        the target's value for a typed step, a window or sheet that opened or
        closed, a toggled control, and what else changed as permitted. The
        replay then checks the effect instead of assuming success. A step that
        brought another app forward declares no diff (two apps' trees do not
        diff); the replay checks it by its recorded after-state.
        """
        if not STATE_GRAPH_AVAILABLE:
            return

        self._refresh_app_context()
        after_snapshot = self._take_snapshot()
        switched = before_pid is not None and self._current_pid is not None and before_pid != self._current_pid
        if not switched:
            self._declare_effects(implementations, before_snapshot, after_snapshot)
        after_state_id = None
        if after_snapshot is not None:
            after_state = self._snapshot_to_ui_state(after_snapshot)
            if after_state is not None:
                after_state_id = self._chain_after(after_state, before_state_id, implementations)
                self._last_after = (after_state_id, after_state.state_id)
                if not self._state_path or self._state_path[-1] != after_state_id:
                    self._state_path.append(after_state_id)
            self._previous_snapshot = after_snapshot

        # Fill in after-state on the intent
        if after_state_id:
            intent.expected_after_state = after_state_id

        # Record transition
        if before_state_id and after_state_id and implementations:
            now_iso = datetime.now().isoformat()
            transition = UITransition(
                from_state=before_state_id,
                to_state=after_state_id,
                intent=intent.intent,
                implementations=implementations,
                times_executed=1,
                times_succeeded=1,
                last_executed=now_iso,
            )
            self._transitions_discovered.append(transition)
            if meta is not None:
                meta["transition"] = transition

        self._previous_state_id = after_state_id or before_state_id

    # -- a drag -------------------------------------------------------------------------------

    def _element_at(self, x, y) -> Optional[Tuple[Any, Dict[str, Any]]]:
        """``(element description, selector dict)`` of the AX element under ``(x, y)``, or None."""
        if self._ax_tree is None or x is None or y is None:
            return None
        try:
            ref = self._ax_tree.find_at(int(x), int(y))
            if ref is None:
                return None
            return self._ax_tree.describe_element(ref), self._ax_tree.to_selector(ref).to_dict()
        except Exception as exc:  # noqa: BLE001 - no element: the coordinate drag alone
            logger.debug("AX element at (%s,%s) not read: %s", x, y, exc)
            return None

    def _drag_implementations(self, args: Dict[str, Any]) -> List[Any]:
        """The recorded coordinate drag, with endpoint identities for replay hit-testing.

        Two AX buttons do not establish that pressing each is equivalent to a
        drag (toolbar rearrangement is one counterexample).
        """
        pts = [args.get(k) for k in ("x1", "y1", "x2", "y2")]
        if any(p is None for p in pts):
            return []
        x1, y1, x2, y2 = (int(round(float(p))) for p in pts)
        start, end = self._element_at(x1, y1), self._element_at(x2, y2)
        impls: List[Any] = []
        drag: Dict[str, Any] = {"action": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2}
        if start is not None:
            drag["from_selector"] = dict(start[1])
        if end is not None:
            drag["to_selector"] = dict(end[1])
        impls.append(TransitionImpl(modality="coordinate", command=drag))
        return impls

    def _declare_effects(self, implementations: List[Any], before: Any, after: Any) -> None:
        """Attach the observed effect to every ax/coordinate implementation."""
        if self._ax_tree is None or before is None or after is None:
            return
        try:
            from anyuse.execution.step_effect import effect_from_diff
            from anyuse.context.accessibility import Selector
            diff = self._ax_tree.diff(before, after)
            for impl in implementations or []:
                if impl.modality not in ("ax", "coordinate"):
                    continue
                cmd = impl.command
                sel = cmd.get("selector")
                target = Selector.from_dict(sel) if isinstance(sel, dict) and sel else None
                action = cmd.get("action", "")
                if impl.modality == "coordinate":
                    action = "set_value" if action == "type_text_at" else "press"
                cmd["expect"] = effect_from_diff(diff, target=target, action=action)
        except Exception as exc:  # noqa: BLE001 - a recording never fails on this
            import logging as _logging
            _logging.getLogger(__name__).debug("effect declaration skipped: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers — JS selector capture
    # ------------------------------------------------------------------

    def _get_js_selector(self, x: int, y: int) -> Optional[str]:
        """
        Try to get a CSS selector for the element at (x, y) in the
        frontmost browser tab.

        Uses MacOSContext.execute_javascript_in_browser().
        """
        if self._macos_ctx is None:
            return None
        try:
            # We need window-relative coordinates for elementFromPoint.
            # elementFromPoint uses viewport coordinates, not screen coords.
            # We subtract the window origin and any chrome offset (rough).
            window_bounds = self._get_window_bounds()
            if not window_bounds:
                return None

            # Convert screen coords to viewport coords (approximate —
            # the browser chrome height varies, but ~80px is reasonable
            # for most browsers).
            vp_x = x - window_bounds.get("x", 0)
            vp_y = y - window_bounds.get("y", 0) - 80  # toolbar offset

            if vp_x < 0 or vp_y < 0:
                return None

            js_code = (
                f"(function(){{"
                f"var el = document.elementFromPoint({vp_x}, {vp_y});"
                f"if (!el) return '';"
                f"var parts = [];"
                f"if (el.id) return '#' + el.id;"
                f"var tag = el.tagName.toLowerCase();"
                f"if (el.className && typeof el.className === 'string') {{"
                f"  var cls = el.className.trim().split(/\\s+/).slice(0,2).join('.');"
                f"  if (cls) return tag + '.' + cls;"
                f"}}"
                f"return tag;"
                f"}})()"
            )
            result = self._macos_ctx.execute_javascript_in_browser(js_code)
            if result and result.strip():
                return result.strip()
        except Exception:
            pass
        return None

    @staticmethod
    def _build_js_script(selector: str, command: Dict[str, Any]) -> str:
        """Build a JS script that performs the given action on the selector."""
        action = command.get("action", "click")
        escaped = selector.replace("'", "\\'")
        if action == "set_value":
            value = command.get("value", "").replace("'", "\\'")
            return (
                f"var el = document.querySelector('{escaped}');"
                f"if(el){{el.value='{value}';el.dispatchEvent(new Event('input'));}}"
            )
        else:
            return (
                f"var el = document.querySelector('{escaped}');"
                f"if(el){{el.click();}}"
            )

    # ------------------------------------------------------------------
    # Internal helpers — coordinate calculations
    # ------------------------------------------------------------------

    def _get_window_bounds(self) -> Optional[Dict[str, int]]:
        """Get the window bounds of the frontmost app."""
        try:
            wm = self._legacy_recorder.window_manager
            ws = wm.get_window_state(self._current_app)
            if ws and ws.bounds:
                return ws.bounds
        except Exception:
            pass
        return None

    @staticmethod
    def _compute_relative_coords(
        abs_x: int, abs_y: int, bounds: Dict[str, int]
    ) -> Tuple[float, float]:
        """Compute window-relative coordinates (0.0 to 1.0)."""
        w = bounds.get("width", 1)
        h = bounds.get("height", 1)
        bx = bounds.get("x", 0)
        by = bounds.get("y", 0)
        if w <= 0:
            w = 1
        if h <= 0:
            h = 1
        rel_x = (abs_x - bx) / w
        rel_y = (abs_y - by) / h
        return (max(0.0, min(1.0, rel_x)), max(0.0, min(1.0, rel_y)))

    # ------------------------------------------------------------------
    # Internal helpers — element identification
    # ------------------------------------------------------------------

    @staticmethod
    def _element_label(node) -> str:
        """Build a human-readable label for an AX element."""
        parts = []
        if node.title:
            parts.append(node.title)
        elif node.description:
            parts.append(node.description)
        elif node.identifier:
            parts.append(node.identifier)
        role_short = node.role.replace("AX", "")
        if parts:
            return f"{parts[0]} ({role_short})"
        return role_short

    @staticmethod
    def _ax_action_for(action_name: str, node) -> Optional[str]:
        """Map an agent action name + element role to an AX action, or ``None``
        when the gesture has no AX equivalent (a hover or a double click
        recorded as ``press`` would replay as a different action). A right
        click opens the element's menu (AXShowMenu)."""
        if action_name in ("type_text_at",):
            return "set_value"
        if action_name in ("hover_at", "double_click_at"):
            return None
        if action_name == "right_click_at":
            return "show_menu"
        if node.role in ("AXCheckBox", "AXRadioButton", "AXSwitch"):
            return "toggle"
        return "press"

    @staticmethod
    def _describe_action(action: str, args: Dict[str, Any], target: str) -> str:
        """Create a human-readable intent description from an action."""
        if target:
            if action == "type_text_at":
                text = args.get("text", "")
                preview = text[:30] + "..." if len(text) > 30 else text
                return f"Type '{preview}' into {target}"
            elif action == "click_at":
                return f"Click {target}"
            elif action == "double_click_at":
                return f"Double-click {target}"
            elif action == "right_click_at":
                return f"Right-click {target}"
            elif action == "hover_at":
                return f"Hover over {target}"
        # Fallback
        safe_args = {k: v for k, v in args.items() if k not in ("screenshot",)}
        return f"{action}({', '.join(f'{k}={v!r}' for k, v in safe_args.items())})"

    def _update_element_position(self, state, node):
        """Update the relative position on a UIElement within a state."""
        window_bounds = self._get_window_bounds()
        if not window_bounds or not node.position or not node.size:
            return
        cx, cy = node.center()
        rel_x, rel_y = self._compute_relative_coords(cx, cy, window_bounds)
        for el in state.elements:
            if el.role == node.role and el.title == (node.title or ""):
                el.relative_x = rel_x
                el.relative_y = rel_y
                break

    def _set_element_js_selector(self, state, node, selector: str):
        """Store a JS selector on a UIElement within a state."""
        for el in state.elements:
            if el.role == node.role and el.title == (node.title or ""):
                el.js_selector = selector
                break

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _save_template(self, template, legacy_workflow: Workflow):
        """
        Save the WorkflowTemplate as a JSON file alongside the legacy
        workflow in its storage directory.
        """
        try:
            from anyuse.guides.storage import write_json_atomic
            workflow_dir = self._legacy_recorder.storage.get_workflow_dir(
                legacy_workflow.id
            )
            write_json_atomic(workflow_dir / "template.json", template.to_dict())
        except Exception as e:
            logger.warning("could not save the workflow template: %s", e)
            print(f"  Warning: Could not save template: {e}")

    def _update_guide_state_graph(self):
        """
        Merge discovered states and transitions into the app guide's state graph and save it
        (atomically, ``GuideStorage.save``). With no guide for the app (a fresh install has
        none, and computer use never makes one) the learned transitions would be dropped and the
        replay run on an empty graph, so the guide is created, named and identified as the OS
        reports the app (``identity_provenance`` "os_app_name", with its bundle id).
        """
        app, bundle_id = getattr(self, "_target_app", ("", "")) or ("", "")
        app = app or (self._current_app if isinstance(self._current_app, str) else "")
        if not self._guide_storage or not app:
            return
        try:
            guide = self._guide_storage.get_by_app(app)
            if guide is None and self._current_url:
                # Try URL-based guide
                guide = self._guide_storage.get_by_url(self._current_url)
            if guide is None:
                from anyuse.guides.models import AppGuide
                guide = AppGuide.create_app_guide(name=app, bundle_id=bundle_id or None)
                guide.identity_provenance = "os_app_name"
                print(f"  Created the app guide for {app} (it keeps the learned states and transitions)")
            elif bundle_id and not guide.bundle_id:
                guide.bundle_id = bundle_id

            graph = guide.get_state_graph()

            for state in self._states_discovered.values():
                graph.add_state(state)
            for transition in self._transitions_discovered:
                graph.add_transition(transition)

            guide.set_state_graph(graph)
            self._guide_storage.save(guide)
        except Exception as e:
            logger.warning("could not update the guide state graph of %s: %s", app, e)
            print(f"  Warning: Could not update guide state graph: {e}")


class StateGraphRecordingContext:
    """
    Context manager for recording workflows with state graph capture.

    Usage:
        with StateGraphRecordingContext("my_workflow") as recorder:
            recorder.record_action("click_at", {"x": 100, "y": 200})

        # Workflow + template are auto-saved on success, discarded on exception.
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        auto_save: bool = True,
        guide_storage=None,
    ):
        self.recorder = StateGraphRecorder(
            name, description, guide_storage=guide_storage
        )
        self.auto_save = auto_save
        self.exception_occurred = False

    def __enter__(self) -> StateGraphRecorder:
        self.recorder.start()
        return self.recorder

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.exception_occurred = True
            print(f"Recording aborted due to error: {exc_val}")
            self.recorder.discard()
        elif self.auto_save:
            self.recorder.save()
        return False
