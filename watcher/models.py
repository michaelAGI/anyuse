"""
Watcher Models - Structured steps and historical workflow sessions.

These models capture:
- Structured actions and historical semantic tags (action + reason + context)
- Click anchors (small crops around click points)
- Watch sessions (collection of events during a watching period)
- Raw workflow drafts (before composition into final Workflow)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from enum import Enum
import re
import uuid


def to_snake_case(text: str) -> str:
    """Normalise arbitrary text into a short snake_case identifier.

    Used to derive stable ``AbstractionRecord.title`` values (the merge key)
    from free-text workflow names / goals.
    """
    if not text:
        return "untitled"
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:60] or "untitled"


class TagType(Enum):
    """Structured and historical semantic event types."""
    WORKFLOW_START = "WORKFLOW_START"  # User begins a distinct task
    WORKFLOW_END = "WORKFLOW_END"      # Task completed
    STEP = "STEP"                      # Discrete action (click, type, scroll)
    INTENT = "INTENT"                  # High-level why/goal
    ACTION = "ACTION"                  # Specific action details
    CONTEXT_CHANGE = "CONTEXT_CHANGE"  # App switch, URL change
    OBSERVATION = "OBSERVATION"        # What Gemini sees/understands
    DECISION = "DECISION"              # Why this path vs alternatives
    NEED_CLARIFICATION = "NEED_CLARIFICATION"  # Gemini needs user to explain
    UNDERSTOOD = "UNDERSTOOD"          # Gemini confirms understanding after clarification
    TASK_SWITCH = "TASK_SWITCH"        # User manually switched tasks (hotkey)
    CONTEXT_LABEL = "CONTEXT_LABEL"    # User labeled current context as a task (hotkey)


@dataclass
class TagEvent:
    """
    A structured action or historical semantic event.
    
    Contains not just WHAT happened but WHY and in what CONTEXT.
    """
    id: str
    tag_type: TagType
    timestamp: datetime
    
    # Core content
    data: Dict[str, Any] = field(default_factory=dict)
    
    # Semantic context (the WHY)
    reason: str = ""              # Why this action was taken
    context: str = ""             # What's on screen, current state
    expected_result: str = ""     # What should happen next
    
    # Location (for STEP/ACTION tags)
    x: Optional[int] = None
    y: Optional[int] = None
    target: str = ""              # UI element description
    
    # Workflow association
    workflow_name: Optional[str] = None  # Which workflow this belongs to
    
    # Linked anchor
    anchor_id: Optional[str] = None

    # Confidence (0.0-1.0) — how confident the model is in this tag
    confidence: float = 0.5

    # State graph reference (Plan 10)
    ax_state_id: Optional[str] = None  # state graph state ID at time of this event
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tag_type": self.tag_type.value,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
            "reason": self.reason,
            "context": self.context,
            "expected_result": self.expected_result,
            "x": self.x,
            "y": self.y,
            "target": self.target,
            "workflow_name": self.workflow_name,
            "anchor_id": self.anchor_id,
            "confidence": self.confidence,
            "ax_state_id": self.ax_state_id,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "TagEvent":
        return cls(
            id=data["id"],
            tag_type=TagType(data["tag_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            data=data.get("data", {}),
            reason=data.get("reason", ""),
            context=data.get("context", ""),
            expected_result=data.get("expected_result", ""),
            x=data.get("x"),
            y=data.get("y"),
            target=data.get("target", ""),
            workflow_name=data.get("workflow_name"),
            anchor_id=data.get("anchor_id"),
            confidence=data.get("confidence", 0.5),
            ax_state_id=data.get("ax_state_id"),
        )
    
    @classmethod
    def from_gemini_json(cls, json_data: dict) -> "TagEvent":
        """
        Parse a historical semantic tag from JSON.
        
        Expected format:
        {
            "type": "STEP",
            "action": "click",
            "target": "New repository button",
            "x": 450, "y": 120,
            "reason": "to start creating a new repository",
            "context": "GitHub dashboard showing user's repositories",
            "expected_result": "opens new repository form"
        }
        """
        tag_type = TagType(json_data.get("type", "OBSERVATION"))
        
        return cls(
            id=uuid.uuid4().hex[:12],
            tag_type=tag_type,
            timestamp=datetime.now(),
            data={k: v for k, v in json_data.items() 
                  if k not in ["type", "x", "y", "target", "reason", "context", "expected_result", "name"]},
            reason=json_data.get("reason", ""),
            context=json_data.get("context", ""),
            expected_result=json_data.get("expected_result", ""),
            x=json_data.get("x"),
            y=json_data.get("y"),
            target=json_data.get("target", ""),
            workflow_name=json_data.get("name"),  # For WORKFLOW_START/END
            confidence=float(json_data.get("confidence", 0.5)),
        )


@dataclass
class ClickAnchor:
    """
    A small visual anchor captured around a click point.
    
    Stored as a small crop (~80x40 pixels) for visual matching during replay.
    Much more storage-efficient than full screenshots.
    """
    id: str
    timestamp: datetime
    
    # Click location
    x: int
    y: int
    
    # Image data
    filename: str              # Relative path to image file
    size: Tuple[int, int] = (80, 40)  # Width x height of crop
    
    # Context
    app: str = ""              # Application where click occurred
    target_description: str = ""  # What was clicked (from Gemini)
    
    # Linked tag event
    tag_event_id: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "x": self.x,
            "y": self.y,
            "filename": self.filename,
            "size": list(self.size),
            "app": self.app,
            "target_description": self.target_description,
            "tag_event_id": self.tag_event_id,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ClickAnchor":
        return cls(
            id=data["id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            x=data["x"],
            y=data["y"],
            filename=data["filename"],
            size=tuple(data.get("size", [80, 40])),
            app=data.get("app", ""),
            target_description=data.get("target_description", ""),
            tag_event_id=data.get("tag_event_id"),
        )
    
    @classmethod
    def create(cls, x: int, y: int, session_id: str, step_num: int, app: str = "") -> "ClickAnchor":
        """Create a new click anchor."""
        anchor_id = uuid.uuid4().hex[:8]
        filename = f"step_{step_num:03d}_{anchor_id}.png"
        
        return cls(
            id=anchor_id,
            timestamp=datetime.now(),
            x=x,
            y=y,
            filename=filename,
            app=app,
        )


@dataclass
class WatchSession:
    """
    A structured recording or historical observation session.

    Keeps legacy events, click anchors and clarification data deserializable
    alongside structured step events and AX state fingerprints.
    """
    id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    
    # Captured data
    events: List[TagEvent] = field(default_factory=list)
    anchors: List[ClickAnchor] = field(default_factory=list)
    
    # Clarification points for hindsight review
    clarification_points: List[ClarificationPoint] = field(default_factory=list)
    
    # Session metadata
    status: str = "recording"  # "recording", "stopped", "composed", "reviewed"
    storage_path: Optional[str] = None  # Path to session data directory
    
    # Statistics
    total_clicks: int = 0
    workflows_detected: int = 0
    apps_used: List[str] = field(default_factory=list)
    
    # Review status
    needs_review: bool = False  # True if there are unresolved clarification points

    # Historical caption observations remain deserializable for old sessions.
    # The step recorder does not populate this field.
    raw_observations: List[dict] = field(default_factory=list)

    # Structured steps (watcher/step_capture.py, ANYUSE_WATCH_STRUCTURED_STEPS): STEP tags
    # with an action verb and the target's AX identity, framed per recording by their own
    # WORKFLOW_START / WORKFLOW_END (named ``steps_<app>_<n>``), and the screen
    # fingerprints the steps name in ``ax_state_id``. Typed values are vault markers.
    step_events: List[TagEvent] = field(default_factory=list)
    ax_states: Dict[str, dict] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Get session duration in seconds."""
        end = self.ended_at or datetime.now()
        return (end - self.started_at).total_seconds()
    
    @property
    def workflow_names(self) -> List[str]:
        """Get names of workflows detected in this session."""
        names = []
        for event in self.events:
            if event.tag_type == TagType.WORKFLOW_START and event.workflow_name:
                names.append(event.workflow_name)
        return names
    
    @property
    def structured_workflow_names(self) -> List[str]:
        """The recordings of the structured step capture, in order."""
        return [e.workflow_name for e in self.step_events
                if e.tag_type == TagType.WORKFLOW_START and e.workflow_name]

    def structured_draft(self, workflow_name: str) -> dict:
        """One structured recording as the draft ``execution/draft_compile.py`` reads: the
        ``RawWorkflowDraft`` keys plus ``app``, ``bundle_id`` (the app it started in),
        ``ax_states`` (the fingerprints its steps name) and ``is_partial`` (no WORKFLOW_END: the
        watcher stopped mid-way). ``apps_involved`` lists every app a step was in, the starting
        app first (each step carries its own ``app`` / ``bundle_id``)."""
        events = [e for e in self.step_events if e.workflow_name == workflow_name]
        start = next((e for e in events if e.tag_type == TagType.WORKFLOW_START), None)
        if start is None:
            raise KeyError(workflow_name)
        app = str(start.data.get("app") or "")
        apps = [app] if app else []
        for e in events:
            a = str((e.data or {}).get("app") or "") if e.tag_type == TagType.STEP else ""
            if a and a not in apps:
                apps.append(a)
        used = [e.ax_state_id for e in events if e.ax_state_id]
        draft = RawWorkflowDraft(name=workflow_name, goal=str(start.data.get("goal") or ""),
                                 events=events, apps_involved=apps,
                                 session_id=self.id).to_dict()
        draft.update(app=app, bundle_id=str(start.data.get("bundle_id") or ""),
                     ax_states={sid: self.ax_states[sid] for sid in dict.fromkeys(used)
                                if sid in self.ax_states},
                     is_partial=not any(e.tag_type == TagType.WORKFLOW_END for e in events))
        return draft

    def add_event(self, event: TagEvent):
        """Add a tag event to the session."""
        self.events.append(event)
        if event.tag_type == TagType.WORKFLOW_START:
            self.workflows_detected += 1
    
    def add_anchor(self, anchor: ClickAnchor):
        """Add a click anchor to the session."""
        self.anchors.append(anchor)
        self.total_clicks += 1
        if anchor.app and anchor.app not in self.apps_used:
            self.apps_used.append(anchor.app)
    
    def get_events_for_workflow(self, workflow_name: str) -> List[TagEvent]:
        """Get all events belonging to a specific workflow."""
        in_workflow = False
        events = []
        
        for event in self.events:
            if event.tag_type == TagType.WORKFLOW_START and event.workflow_name == workflow_name:
                in_workflow = True
            
            if in_workflow:
                events.append(event)
            
            if event.tag_type == TagType.WORKFLOW_END and event.workflow_name == workflow_name:
                break
        
        return events
    
    def link_anchor_to_event(self, anchor: ClickAnchor, event: TagEvent):
        """Link a click anchor to a tag event."""
        anchor.tag_event_id = event.id
        event.anchor_id = anchor.id
    
    def add_clarification_point(self, point: ClarificationPoint):
        """Add a point needing clarification."""
        self.clarification_points.append(point)
        self.needs_review = True
    
    @property
    def unresolved_clarifications(self) -> List[ClarificationPoint]:
        """Get clarification points that haven't been resolved."""
        return [p for p in self.clarification_points if not p.resolved]
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "events": [e.to_dict() for e in self.events],
            "anchors": [a.to_dict() for a in self.anchors],
            "clarification_points": [p.to_dict() for p in self.clarification_points],
            "status": self.status,
            "storage_path": self.storage_path,
            "total_clicks": self.total_clicks,
            "workflows_detected": self.workflows_detected,
            "apps_used": self.apps_used,
            "needs_review": self.needs_review,
            "raw_observations": self.raw_observations,
            "step_events": [e.to_dict() for e in list(self.step_events)],
            "ax_states": dict(self.ax_states),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WatchSession":
        return cls(
            id=data["id"],
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=datetime.fromisoformat(data["ended_at"]) if data.get("ended_at") else None,
            events=[TagEvent.from_dict(e) for e in data.get("events", [])],
            anchors=[ClickAnchor.from_dict(a) for a in data.get("anchors", [])],
            clarification_points=[ClarificationPoint.from_dict(p) for p in data.get("clarification_points", [])],
            status=data.get("status", "stopped"),
            storage_path=data.get("storage_path"),
            total_clicks=data.get("total_clicks", 0),
            workflows_detected=data.get("workflows_detected", 0),
            apps_used=data.get("apps_used", []),
            needs_review=data.get("needs_review", False),
            raw_observations=data.get("raw_observations", []),
            step_events=[TagEvent.from_dict(e) for e in data.get("step_events", []) or []],
            ax_states=dict(data.get("ax_states") or {}),
        )

    @classmethod
    def create_new(cls) -> "WatchSession":
        """Create a new watch session."""
        return cls(
            id=uuid.uuid4().hex[:12],
            started_at=datetime.now(),
        )


@dataclass
class ClarificationPoint:
    """
    Historical clarification data retained for session deserialization.

    Older caption recordings stored uncertain actions and later explanations
    here. The step recorder does not create or review these points.
    """
    id: str
    timestamp: datetime
    
    # What was happening
    screenshot: Optional[bytes] = None  # Screenshot at this moment
    screenshot_path: Optional[str] = None  # Saved screenshot path
    detected_workflow: Optional[str] = None  # What we think was happening
    confidence: float = 0.5  # How sure we are (0-1)
    
    # Why clarification is needed
    unclear_reason: str = ""  # "random clicking", "unknown goal", etc.
    partial_understanding: str = ""  # What we do understand
    
    # Context at this moment
    app: str = ""
    window_title: str = ""
    recent_actions: List[str] = field(default_factory=list)  # Last few inputs
    
    # Resolution (filled after review)
    user_explanation: Optional[str] = None
    corrected_workflow: Optional[str] = None
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "screenshot_path": self.screenshot_path,
            "detected_workflow": self.detected_workflow,
            "confidence": self.confidence,
            "unclear_reason": self.unclear_reason,
            "partial_understanding": self.partial_understanding,
            "app": self.app,
            "window_title": self.window_title,
            "recent_actions": self.recent_actions,
            "user_explanation": self.user_explanation,
            "corrected_workflow": self.corrected_workflow,
            "resolved": self.resolved,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ClarificationPoint":
        return cls(
            id=data["id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            screenshot_path=data.get("screenshot_path"),
            detected_workflow=data.get("detected_workflow"),
            confidence=data.get("confidence", 0.5),
            unclear_reason=data.get("unclear_reason", ""),
            partial_understanding=data.get("partial_understanding", ""),
            app=data.get("app", ""),
            window_title=data.get("window_title", ""),
            recent_actions=data.get("recent_actions", []),
            user_explanation=data.get("user_explanation"),
            corrected_workflow=data.get("corrected_workflow"),
            resolved=data.get("resolved", False),
            resolved_at=datetime.fromisoformat(data["resolved_at"]) if data.get("resolved_at") else None,
        )
    
    @classmethod
    def create(
        cls,
        confidence: float,
        unclear_reason: str,
        app: str = "",
        window_title: str = "",
        detected_workflow: Optional[str] = None,
        partial_understanding: str = "",
        recent_actions: Optional[List[str]] = None,
    ) -> "ClarificationPoint":
        """Create a new clarification point."""
        return cls(
            id=uuid.uuid4().hex[:8],
            timestamp=datetime.now(),
            confidence=confidence,
            unclear_reason=unclear_reason,
            app=app,
            window_title=window_title,
            detected_workflow=detected_workflow,
            partial_understanding=partial_understanding,
            recent_actions=recent_actions or [],
        )


@dataclass
class RawWorkflowDraft:
    """
    A raw workflow draft extracted from a watch session.
    
    This is the intermediate representation before final composition
    into a proper Workflow object.
    """
    name: str
    goal: str = ""
    
    # Raw events from session
    events: List[TagEvent] = field(default_factory=list)
    anchors: List[ClickAnchor] = field(default_factory=list)
    
    # Extracted metadata
    apps_involved: List[str] = field(default_factory=list)
    urls_visited: List[str] = field(default_factory=list)
    
    # Session reference
    session_id: str = ""
    
    # Completion status
    is_partial: bool = False  # True if WORKFLOW_END wasn't received
    
    # Composition status
    composed: bool = False
    workflow_id: Optional[str] = None  # ID after composition
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "goal": self.goal,
            "events": [e.to_dict() for e in self.events],
            "anchors": [a.to_dict() for a in self.anchors],
            "apps_involved": self.apps_involved,
            "urls_visited": self.urls_visited,
            "session_id": self.session_id,
            "is_partial": self.is_partial,
            "composed": self.composed,
            "workflow_id": self.workflow_id,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "RawWorkflowDraft":
        return cls(
            name=data["name"],
            goal=data.get("goal", ""),
            events=[TagEvent.from_dict(e) for e in data.get("events", [])],
            anchors=[ClickAnchor.from_dict(a) for a in data.get("anchors", [])],
            apps_involved=data.get("apps_involved", []),
            urls_visited=data.get("urls_visited", []),
            session_id=data.get("session_id", ""),
            composed=data.get("composed", False),
            workflow_id=data.get("workflow_id"),
        )
    
    @classmethod
    def from_session(cls, session: WatchSession, workflow_name: str) -> "RawWorkflowDraft":
        """Extract a workflow draft from a session."""
        events = session.get_events_for_workflow(workflow_name)
        
        # Get anchors linked to these events
        event_ids = {e.id for e in events}
        anchors = [a for a in session.anchors if a.tag_event_id in event_ids]
        
        # Extract goal from WORKFLOW_START or first INTENT
        goal = ""
        for event in events:
            if event.tag_type == TagType.WORKFLOW_START:
                goal = event.data.get("goal", "")
                break
            elif event.tag_type == TagType.INTENT and not goal:
                goal = event.data.get("description", "")
        
        # Collect apps and URLs
        apps = []
        urls = []
        for event in events:
            if event.tag_type == TagType.CONTEXT_CHANGE:
                app = event.data.get("app", "")
                url = event.data.get("url", "")
                if app and app not in apps:
                    apps.append(app)
                if url and url not in urls:
                    urls.append(url)
        
        return cls(
            name=workflow_name,
            goal=goal,
            events=events,
            anchors=anchors,
            apps_involved=apps,
            urls_visited=urls,
            session_id=session.id,
        )


@dataclass
class AbstractionRecord:
    """A durable, generalizable, queryable distillation of what the watcher saw.

    Three granularities selected by ``kind``:

    - ``task``: a full multi-step workflow. ``structure`` = ``{goal,
      trigger_phrases, steps:[{action, target_role, target_name, app, reason}]}``
      — the composer's refined steps *stripped of x/y coordinates and anchor
      filenames* (those stay in the concrete ``Workflow`` for replay). This is
      the part that generalizes across machines.
    - ``app_transition``: one edge of a per-app UI state graph. ``structure`` =
      ``{app, from_state_id, to_state_id, action, element:{role, name}}`` where
      state ids are the structural AX hashes (``TagEvent.ax_state_id``).
    - ``step_pattern``: a repeated micro-pattern. ``structure`` = ``{action,
      target_role, target_name, app, frequency}``.

    Only ``summary`` (one paragraph) + typed entities + ``memory_ref`` are ever
    legal to copy across the sharing boundary (Contract I §2). ``structure`` /
    steps / anchors stay in ``~/.anyuse``.
    """

    kind: str                       # "task" | "app_transition" | "step_pattern"
    title: str                      # snake_case identifier (merge key component)
    summary: str = ""               # ONE paragraph — the only cross-boundary field
    structure: Dict[str, Any] = field(default_factory=dict)  # kind-specific
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    time_range: Tuple[Optional[float], Optional[float]] = (None, None)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    apps: List[str] = field(default_factory=list)
    domains: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    confidence: float = 0.5         # 1.0 only after human confirmation
    seen_count: int = 1             # bumped on re-observation (merge, not duplicate)
    provenance: Dict[str, Any] = field(default_factory=dict)  # refs only, never bytes
    memory_ref: str = ""            # "anyuse:session/{session_id}#abs/{id}"
    privacy_tier: str = "local_only"  # "local_only" | "shareable_summary"

    def __post_init__(self):
        if not self.memory_ref:
            provenance = self.provenance if isinstance(self.provenance, dict) else {}
            session_id = provenance.get("session_id", "") or ""
            self.memory_ref = f"anyuse:session/{session_id}#abs/{self.id}"

    @property
    def primary_app(self) -> str:
        """The first app — the third component of the (kind, title, app) merge key."""
        return self.apps[0] if self.apps else ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "structure": self.structure,
            "time_range": list(self.time_range),
            "created_at": self.created_at,
            "apps": self.apps,
            "domains": self.domains,
            "tags": self.tags,
            "confidence": self.confidence,
            "seen_count": self.seen_count,
            "provenance": self.provenance,
            "memory_ref": self.memory_ref,
            "privacy_tier": self.privacy_tier,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AbstractionRecord":
        tr = data.get("time_range") or [None, None]
        if not isinstance(tr, (list, tuple)) or len(tr) != 2:
            tr = [None, None]
        return cls(
            kind=data["kind"],
            title=data["title"],
            summary=data.get("summary", ""),
            structure=data.get("structure", {}) or {},
            id=data.get("id") or uuid.uuid4().hex[:12],
            time_range=(tr[0], tr[1]),
            created_at=data.get("created_at") or datetime.now().isoformat(),
            apps=data.get("apps", []) or [],
            domains=data.get("domains", []) or [],
            tags=data.get("tags", []) or [],
            confidence=data.get("confidence", 0.5),
            seen_count=data.get("seen_count", 1),
            provenance=data.get("provenance", {}) or {},
            memory_ref=data.get("memory_ref", "") or "",
            privacy_tier=data.get("privacy_tier", "local_only") or "local_only",
        )
