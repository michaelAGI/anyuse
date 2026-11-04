"""
Workflow Models - Data structures for workflow recording and replay.

Includes visual anchors for pixel-based matching during replay.
Includes supervision data for post-execution verification.
Includes intent-based workflow templates (State Graph).
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from pathlib import Path
import copy
import json
from enum import Enum


class IssueSeverity(Enum):
    """Severity levels for verification issues."""
    ERROR = "error"      # Must be fixed before workflow can be used
    WARNING = "warning"  # Should be fixed for reliability
    INFO = "info"        # Suggestion for improvement


class IssueType(Enum):
    """Types of issues that can be detected during verification."""
    WINDOW_SPECIFIC = "window_specific"        # Coordinates only work at specific window size
    HARDCODED_POSITION = "hardcoded_position"  # Absolute screen position
    REDUNDANT_STEP = "redundant_step"          # Duplicates previous step
    APP_NOT_FOCUSED = "app_not_focused"        # Action on wrong app
    FRAGILE_TIMING = "fragile_timing"          # Relies on specific timing
    MISSING_CHECKPOINT = "missing_checkpoint"  # Critical action without verification
    GOAL_NOT_ACHIEVED = "goal_not_achieved"    # Final state doesn't match goal
    UNEXPECTED_STATE = "unexpected_state"      # Unexpected window/app state


@dataclass
class VerificationIssue:
    """
    An issue detected during workflow verification.
    
    Used to flag problems that could cause replay failures.
    """
    step_index: int                  # Which step has the issue (-1 for workflow-level)
    issue_type: str                  # IssueType value
    description: str                 # Human-readable description
    severity: str = "warning"        # IssueSeverity value
    suggested_fix: str = ""          # How to fix the issue
    
    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "issue_type": self.issue_type,
            "description": self.description,
            "severity": self.severity,
            "suggested_fix": self.suggested_fix,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "VerificationIssue":
        return cls(
            step_index=data.get("step_index", -1),
            issue_type=data.get("issue_type", ""),
            description=data.get("description", ""),
            severity=data.get("severity", "warning"),
            suggested_fix=data.get("suggested_fix", ""),
        )


@dataclass
class WindowContext:
    """
    Window context at the time of an action.
    
    Captures which app was active and window bounds for verification.
    """
    app: str                         # Application name
    bounds: Dict[str, int]           # {"x": 0, "y": 25, "width": 1440, "height": 875}
    is_frontmost: bool = True        # Whether this app was frontmost
    screen_size: Tuple[int, int] = (1920, 1080)  # Screen resolution at recording time
    
    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "bounds": self.bounds,
            "is_frontmost": self.is_frontmost,
            "screen_size": list(self.screen_size),
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "WindowContext":
        return cls(
            app=data.get("app", ""),
            bounds=data.get("bounds", {}),
            is_frontmost=data.get("is_frontmost", True),
            screen_size=tuple(data.get("screen_size", [1920, 1080])),
        )


@dataclass
class VisualAnchor:
    """
    Visual anchor for a UI element.
    
    Stores a cropped screenshot of the click target area
    for fast pixel-based matching during replay.
    """
    filename: str                    # e.g., "step_001_target.png"
    size: Tuple[int, int]           # (width, height) of the anchor
    offset: Tuple[int, int] = (0, 0) # Offset from anchor center to actual click point
    confidence: float = 0.8          # Minimum confidence for matching
    
    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size": list(self.size),
            "offset": list(self.offset),
            "confidence": self.confidence,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "VisualAnchor":
        return cls(
            filename=data["filename"],
            size=tuple(data.get("size", [80, 40])),
            offset=tuple(data.get("offset", [0, 0])),
            confidence=data.get("confidence", 0.8),
        )


@dataclass
class WindowState:
    """
    Window state at time of recording.
    
    Used to restore window position/size before replay.
    """
    app: str                         # Application name
    bounds: Dict[str, int]           # {"x": 0, "y": 25, "width": 1440, "height": 875}
    is_fullscreen: bool = False
    
    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "bounds": self.bounds,
            "is_fullscreen": self.is_fullscreen,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "WindowState":
        return cls(
            app=data["app"],
            bounds=data.get("bounds", {}),
            is_fullscreen=data.get("is_fullscreen", False),
        )


@dataclass
class WorkflowStep:
    """
    A single step in a workflow.
    
    Includes optional visual anchor for pixel-based matching during replay.
    
    """
    action: str
    args: Dict[str, Any]
    description: str = ""
    
    # Visual matching (small anchor crops for element finding)
    visual_anchor: Optional[VisualAnchor] = None
    
    # Timing
    delay_before: float = 0.0
    delay_after: float = 0.3
    
    # Execution
    optional: bool = False     # Continue on failure
    retry_count: int = 0
    max_retries: int = 2
    
    # Smart Replay (set by Gemini analysis)
    # Live Watcher supervises entire replay in real-time via video stream
    replay_mode: str = "coordinate"         # "coordinate" or "visual_match"
    use_visual_anchor: bool = False         # Whether to use visual anchor for this step
    
    # Recorded state
    timestamp: float = 0.0
    ai_reasoning: str = ""                   # AI's reasoning/thinking for this step
    window_context: Optional[WindowContext] = None  # Window state at time of action
    
    def to_dict(self) -> dict:
        data = {
            "action": self.action,
            "args": self.args,
            "description": self.description,
            "delay_before": self.delay_before,
            "delay_after": self.delay_after,
            "optional": self.optional,
            "replay_mode": self.replay_mode,
            "use_visual_anchor": self.use_visual_anchor,
            "timestamp": self.timestamp,
        }
        
        if self.visual_anchor:
            data["visual_anchor"] = self.visual_anchor.to_dict()
        
        if self.ai_reasoning:
            data["ai_reasoning"] = self.ai_reasoning
        
        if self.window_context:
            data["window_context"] = self.window_context.to_dict()
        
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowStep":
        visual_anchor = None
        if "visual_anchor" in data:
            if isinstance(data["visual_anchor"], str):
                # Legacy format: just filename
                visual_anchor = VisualAnchor(filename=data["visual_anchor"], size=(80, 40))
            else:
                visual_anchor = VisualAnchor.from_dict(data["visual_anchor"])
        
        window_context = None
        if "window_context" in data:
            window_context = WindowContext.from_dict(data["window_context"])
        
        return cls(
            action=data["action"],
            args=data.get("args", {}),
            description=data.get("description", ""),
            visual_anchor=visual_anchor,
            delay_before=data.get("delay_before", 0.0),
            delay_after=data.get("delay_after", 0.3),
            optional=data.get("optional", False),
            replay_mode=data.get("replay_mode", "coordinate"),
            use_visual_anchor=data.get("use_visual_anchor", False),
            timestamp=data.get("timestamp", 0.0),
            ai_reasoning=data.get("ai_reasoning", ""),
            window_context=window_context,
        )


@dataclass
class Workflow:
    """
    A complete workflow with steps, visual anchors, and metadata.
    
    Includes verification status and self-improvement tracking.
    """
    id: str
    name: str
    description: str = ""
    steps: List[WorkflowStep] = field(default_factory=list)
    
    # Trigger matching
    trigger_phrases: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    
    # Required credentials
    variables: List[str] = field(default_factory=list)
    
    # Window state
    window_state: Optional[WindowState] = None
    
    # Metadata
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    
    # Statistics
    run_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_run: str = ""
    last_error: str = ""
    
    # Smart Replay Analysis
    smart_analyzed: bool = False  # Whether Gemini 3 Pro has analyzed this workflow
    verification_steps: List[int] = field(default_factory=list)  # Step indices that need AI verification
    
    # Verification (supervision)
    verified: bool = False                                      # Has this workflow passed verification?
    verification_issues: List[VerificationIssue] = field(default_factory=list)  # Issues found
    robustness_score: float = 0.0                              # 0-1, likelihood of successful replay
    verify_after_play: bool = True                             # Whether to verify after replay
    self_improvement_attempts: int = 0                         # Number of self-improvement cycles
    last_verification: str = ""                                # ISO timestamp of last verification
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 1.0
    
    @property
    def storage_dir(self) -> str:
        """Get storage directory name."""
        return f"workflow_{self.id}"
    
    @property
    def screenshots_dir(self) -> str:
        """Get screenshots subdirectory name."""
        return f"workflow_{self.id}/screenshots"
    
    @property
    def has_errors(self) -> bool:
        """Check if workflow has any error-level issues."""
        return any(issue.severity == "error" for issue in self.verification_issues)
    
    @property
    def has_warnings(self) -> bool:
        """Check if workflow has any warning-level issues."""
        return any(issue.severity == "warning" for issue in self.verification_issues)
    
    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
            "trigger_phrases": self.trigger_phrases,
            "tags": self.tags,
            "variables": self.variables,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "run_count": self.run_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_run": self.last_run,
            "last_error": self.last_error,
            "smart_analyzed": self.smart_analyzed,
            "verification_steps": self.verification_steps,
            # Verification fields
            "verified": self.verified,
            "verification_issues": [i.to_dict() for i in self.verification_issues],
            "robustness_score": self.robustness_score,
            "verify_after_play": self.verify_after_play,
            "self_improvement_attempts": self.self_improvement_attempts,
            "last_verification": self.last_verification,
        }
        
        if self.window_state:
            data["window_state"] = self.window_state.to_dict()
        
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> "Workflow":
        window_state = None
        if "window_state" in data:
            window_state = WindowState.from_dict(data["window_state"])
        
        verification_issues = []
        for issue_data in data.get("verification_issues", []):
            verification_issues.append(VerificationIssue.from_dict(issue_data))
        
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            steps=[WorkflowStep.from_dict(s) for s in data.get("steps", [])],
            trigger_phrases=data.get("trigger_phrases", []),
            tags=data.get("tags", []),
            variables=data.get("variables", []),
            window_state=window_state,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            version=data.get("version", 1),
            run_count=data.get("run_count", 0),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
            last_run=data.get("last_run", ""),
            last_error=data.get("last_error", ""),
            smart_analyzed=data.get("smart_analyzed", False),
            verification_steps=data.get("verification_steps", []),
            # Verification fields
            verified=data.get("verified", False),
            verification_issues=verification_issues,
            robustness_score=data.get("robustness_score", 0.0),
            verify_after_play=data.get("verify_after_play", True),
            self_improvement_attempts=data.get("self_improvement_attempts", 0),
            last_verification=data.get("last_verification", ""),
        )
    
    @classmethod
    def create_new(cls, name: str, description: str = "") -> "Workflow":
        """Create a new workflow with generated ID."""
        import uuid
        workflow_id = uuid.uuid4().hex[:8]

        return cls(
            id=workflow_id,
            name=name,
            description=description,
            trigger_phrases=[name.lower(), name.replace("_", " ").lower()],
            created_at=datetime.now().isoformat(),
        )


# ---------------------------------------------------------------------------
# Intent-based workflow models for the State Graph Execution Engine
# ---------------------------------------------------------------------------

@dataclass
class WorkflowParam:
    """
    A parameter for a parameterized workflow template.

    Parameters allow workflows to be reused with different inputs,
    e.g., a "process refund" workflow parameterized by amount and order ID.
    """
    name: str
    param_type: str = "string"
    # "string", "number", "credential"

    required: bool = True
    default: Optional[str] = None
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "param_type": self.param_type,
            "required": self.required,
            "default": self.default,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowParam":
        return cls(
            name=data.get("name", ""),
            param_type=data.get("param_type", "string"),
            required=data.get("required", True),
            default=data.get("default"),
            description=data.get("description", ""),
        )


@dataclass
class WorkflowIntent:
    """
    One step in an intent-based workflow, defined by WHAT to do, not HOW.

    Unlike WorkflowStep (which stores concrete actions like "click at 500,300"),
    WorkflowIntent stores the intent ("enter refund amount") and the target
    element ("AmountField"). The State Graph execution engine resolves the
    best implementation modality at runtime.
    """
    intent: str
    # What to do: "enter refund amount", "click submit", "select dropdown option"

    target_element: str
    # Element identifier within the state, e.g. "AmountField", "SubmitButton"

    expected_before_state: str = ""
    expected_after_state: str = ""
    # state_id references — which state we should be in before/after this step

    expected_after: str = ""
    # Human-readable description of expected outcome:
    # "amount field shows the entered value"

    input_data: Optional[str] = None
    # Parameterized input, e.g. "${amount}", "fixed_value", None for clicks

    input_type: str = "click"
    # "type", "select", "click", "toggle"

    optional: bool = False
    # If True, workflow continues even if this step fails

    ai_reasoning: str = ""
    # AI's reasoning for why this step is needed

    # Execution tracking
    step_attempts: int = 0
    step_successes: int = 0
    last_success_modality: str = ""
    # Which modality last succeeded: "ax", "js", "applescript", "coordinate", "computer_use"

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "target_element": self.target_element,
            "expected_before_state": self.expected_before_state,
            "expected_after_state": self.expected_after_state,
            "expected_after": self.expected_after,
            "input_data": self.input_data,
            "input_type": self.input_type,
            "optional": self.optional,
            "ai_reasoning": self.ai_reasoning,
            "step_attempts": self.step_attempts,
            "step_successes": self.step_successes,
            "last_success_modality": self.last_success_modality,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowIntent":
        return cls(
            intent=data.get("intent", ""),
            target_element=data.get("target_element", ""),
            expected_before_state=data.get("expected_before_state", ""),
            expected_after_state=data.get("expected_after_state", ""),
            expected_after=data.get("expected_after", ""),
            input_data=data.get("input_data"),
            input_type=data.get("input_type", "click"),
            optional=data.get("optional", False),
            ai_reasoning=data.get("ai_reasoning", ""),
            step_attempts=data.get("step_attempts", 0),
            step_successes=data.get("step_successes", 0),
            last_success_modality=data.get("last_success_modality", ""),
        )


@dataclass
class WorkflowTemplate:
    """
    A parameterized path through the state graph.

    Represents a reusable workflow defined by intents rather than
    concrete actions. The State Graph execution engine resolves each
    WorkflowIntent step into concrete actions at runtime, choosing
    the best available modality.

    This is the successor to the flat Workflow class.
    Existing Workflow objects remain supported for backward compatibility.
    """
    id: str
    name: str
    description: str = ""

    parameters: List[WorkflowParam] = field(default_factory=list)
    steps: List[WorkflowIntent] = field(default_factory=list)

    state_path: List[str] = field(default_factory=list)
    # Ordered list of state_ids traversed by this workflow

    entry_states: List[str] = field(default_factory=list)
    # Which states this workflow can start from

    required_app: str = ""
    # Application that must be active

    required_bundle_id: str = ""
    # Its bundle id: the replay opens the same app under any localized name
    # (a recording of "Chess" replays where the app is called "Schach")

    # Statistics
    run_count: int = 0
    success_count: int = 0
    version: int = 1

    # Matching
    trigger_phrases: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    # Metadata
    created_at: str = ""
    updated_at: str = ""

    # Orchestrator integration
    api_hint: Optional[str] = None
    # Metadata hint for the orchestrator's marketplace, e.g. "stripe_refund_v2"

    # Per-workflow approval level ({"level": "auto"|"all"|"destructive"|"off", ...}),
    # the same record the draft pipeline keeps (execution/draft_compile.py
    # approval_level / effective_hil_mode read it). Empty = the default, auto:
    # a replay started with its values holds nothing.
    approval: Dict[str, Any] = field(default_factory=dict)

    # The demonstration's own states and ordered, unmerged transitions. Shared
    # app guides can merge equivalent paths from other workflows with different
    # commands; replay must retain this workflow's exact demonstrated commands.
    recorded_graph: Dict[str, Any] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.run_count == 0:
            return 0.0
        return self.success_count / self.run_count

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "parameters": [p.to_dict() for p in self.parameters],
            "steps": [s.to_dict() for s in self.steps],
            "state_path": self.state_path,
            "entry_states": self.entry_states,
            "required_app": self.required_app,
            **({"required_bundle_id": self.required_bundle_id} if self.required_bundle_id else {}),
            "run_count": self.run_count,
            "success_count": self.success_count,
            "version": self.version,
            "trigger_phrases": self.trigger_phrases,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "api_hint": self.api_hint,
            **({"approval": dict(self.approval)} if self.approval else {}),
            **({"recorded_graph": copy.deepcopy(self.recorded_graph)} if self.recorded_graph else {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowTemplate":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            parameters=[
                WorkflowParam.from_dict(p) for p in data.get("parameters", [])
            ],
            steps=[
                WorkflowIntent.from_dict(s) for s in data.get("steps", [])
            ],
            state_path=data.get("state_path", []),
            entry_states=data.get("entry_states", []),
            required_app=data.get("required_app", ""),
            required_bundle_id=str(data.get("required_bundle_id") or ""),
            run_count=data.get("run_count", 0),
            success_count=data.get("success_count", 0),
            version=data.get("version", 1),
            trigger_phrases=data.get("trigger_phrases", []),
            tags=data.get("tags", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            api_hint=data.get("api_hint"),
            approval=dict(data.get("approval") or {}),
            recorded_graph=copy.deepcopy(data.get("recorded_graph") or {}),
        )

    @classmethod
    def create_new(cls, name: str, description: str = "") -> "WorkflowTemplate":
        """Create a new workflow template with generated ID."""
        import uuid
        template_id = uuid.uuid4().hex[:8]

        return cls(
            id=template_id,
            name=name,
            description=description,
            trigger_phrases=[name.lower(), name.replace("_", " ").lower()],
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )


def migrate_step_to_intent(step: WorkflowStep) -> WorkflowIntent:
    """
    Convert a legacy flat WorkflowStep to an intent-based WorkflowIntent.

    This is a best-effort migration. The resulting WorkflowIntent captures
    the action description as the intent and infers input_type from the
    action type. State references are left empty since the legacy step
    has no state graph context.

    Args:
        step: A legacy WorkflowStep to convert.

    Returns:
        A WorkflowIntent with fields populated from the step data.
    """
    # Map action types to input_type
    action_to_input_type = {
        "click": "click",
        "double_click": "click",
        "right_click": "click",
        "type": "type",
        "key": "type",
        "hotkey": "type",
        "select": "select",
        "scroll": "click",
        "toggle": "toggle",
        "checkbox": "toggle",
    }

    action = step.action.lower()
    input_type = action_to_input_type.get(action, "click")

    # Extract target element from args if available
    target_element = ""
    if "element" in step.args:
        target_element = str(step.args["element"])
    elif "selector" in step.args:
        target_element = str(step.args["selector"])
    elif "description" in step.args:
        target_element = str(step.args["description"])

    # Extract input data from args
    input_data = None
    if "text" in step.args:
        input_data = str(step.args["text"])
    elif "value" in step.args:
        input_data = str(step.args["value"])
    elif "keys" in step.args:
        input_data = str(step.args["keys"])

    # Use step description as the intent, falling back to action + target
    intent = step.description if step.description else f"{step.action} {target_element}".strip()

    return WorkflowIntent(
        intent=intent,
        target_element=target_element,
        expected_before_state="",
        expected_after_state="",
        expected_after="",
        input_data=input_data,
        input_type=input_type,
        optional=step.optional,
        ai_reasoning=step.ai_reasoning,
        step_attempts=0,
        step_successes=0,
        last_success_modality="",
    )

