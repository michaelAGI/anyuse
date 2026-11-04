"""
Guide Generator - Auto-generates application guides from watcher observations and workflows.

Learns by observing:
- Historical session observations (structured step ingestion is not implemented)
- Workflow compositions (steps, targets, shortcuts)
- Cross-workflow patterns for the same app/website

Creates a self-improving knowledge base where:
- Workflows inform Guides (step targets → landmarks, actions → shortcuts)
- Guides inform Workflows (context for smarter replay)
- Both are bidirectionally linked for discoverability
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict
from datetime import datetime
import json
import os

from anyuse.execution.model_adapter import ModelAdapter
from anyuse.guides.models import AppGuide, GuideType
from anyuse.guides.projection import (
    IDENTITY_OS_APP_NAME,
    IDENTITY_URL_HOST,
    RECORDED_IDENTITY_PROVENANCE,
)
from anyuse.guides.storage import GuideStorage
from anyuse.watcher.models import WatchSession, TagEvent, TagType, RawWorkflowDraft


# Model for guide synthesis
SYNTHESIS_MODEL = "gemini-2.5-flash-preview-09-2025"


@dataclass
class AppObservations:
    """
    Accumulated observations for an app/website.
    
    Tracks frequency to prioritize common patterns over one-off behaviors.
    Also tracks activity context - what users and agents have done.
    """
    # Core observations with frequency counts
    use_cases: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    landmarks: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    shortcuts: Dict[str, Tuple[str, int]] = field(default_factory=dict)  # action -> (keys, count)
    navigation_tips: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Workflow references
    linked_workflows: Set[str] = field(default_factory=set)
    workflow_goals: Dict[str, int] = field(default_factory=lambda: defaultdict(int))  # goal descriptions
    
    # Step patterns (what users commonly do in this app)
    step_patterns: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Activity context - what was done in this app (user + agent)
    user_activities: List[Dict] = field(default_factory=list)    # From watching user
    agent_activities: List[Dict] = field(default_factory=list)   # From agent executions
    successful_patterns: List[str] = field(default_factory=list) # What worked well
    known_issues: List[str] = field(default_factory=list)        # What failed/issues
    
    # Metadata
    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)
    total_sessions: int = 0
    
    @property
    def total_observations(self) -> int:
        return (
            sum(self.use_cases.values()) +
            sum(self.landmarks.values()) +
            sum(v[1] for v in self.shortcuts.values()) +
            sum(self.navigation_tips.values())
        )
    
    @property
    def unique_observations(self) -> int:
        return (
            len(self.use_cases) +
            len(self.landmarks) +
            len(self.shortcuts) +
            len(self.navigation_tips)
        )
    
    def add_use_case(self, case: str):
        """Add a use case observation (with deduplication)."""
        normalized = case.strip()[:100].lower()
        if normalized:
            self.use_cases[normalized] += 1
            self.last_seen = datetime.now()
    
    def add_landmark(self, landmark: str):
        """Add a UI landmark observation."""
        normalized = landmark.strip()[:150]
        if normalized:
            self.landmarks[normalized] += 1
            self.last_seen = datetime.now()
    
    def add_shortcut(self, action: str, keys: str):
        """Add a keyboard shortcut observation."""
        action_normalized = action.strip()[:50].lower()
        if action_normalized and keys:
            existing = self.shortcuts.get(action_normalized)
            if existing:
                self.shortcuts[action_normalized] = (keys, existing[1] + 1)
            else:
                self.shortcuts[action_normalized] = (keys, 1)
            self.last_seen = datetime.now()
    
    def add_navigation_tip(self, tip: str):
        """Add a navigation tip."""
        normalized = tip.strip()[:200]
        if normalized:
            self.navigation_tips[normalized] += 1
            self.last_seen = datetime.now()
    
    def add_workflow(self, workflow_name: str, goal: str = ""):
        """Link a workflow and its goal."""
        self.linked_workflows.add(workflow_name)
        if goal:
            self.workflow_goals[goal.strip()[:100].lower()] += 1
        self.last_seen = datetime.now()
    
    def add_step_pattern(self, pattern: str):
        """Add a common step pattern."""
        normalized = pattern.strip()[:100]
        if normalized:
            self.step_patterns[normalized] += 1
            self.last_seen = datetime.now()
    
    def get_top_items(self, data: Dict[str, int], n: int = 10) -> List[str]:
        """Get top N items by frequency."""
        sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=True)
        return [item for item, _ in sorted_items[:n]]
    
    def add_user_activity(
        self,
        description: str,
        workflow_name: str = "",
        steps_count: int = 0,
        duration_seconds: float = 0.0,
        notes: str = "",
    ):
        """Record a user activity (from watching)."""
        activity = {
            "description": description,
            "source": "user",
            "workflow_name": workflow_name,
            "steps_count": steps_count,
            "duration_seconds": duration_seconds,
            "notes": notes,
            "timestamp": datetime.now().isoformat(),
            "success": True,
        }
        self.user_activities.append(activity)
        # Keep last 50
        if len(self.user_activities) > 50:
            self.user_activities = self.user_activities[-50:]
        self.last_seen = datetime.now()
    
    def add_agent_activity(
        self,
        description: str,
        task: str = "",
        workflow_name: str = "",
        success: bool = True,
        steps_count: int = 0,
        duration_seconds: float = 0.0,
        notes: str = "",
    ):
        """Record an agent activity (from execution)."""
        activity = {
            "description": description,
            "source": "agent",
            "task": task,
            "workflow_name": workflow_name,
            "success": success,
            "steps_count": steps_count,
            "duration_seconds": duration_seconds,
            "notes": notes,
            "timestamp": datetime.now().isoformat(),
        }
        self.agent_activities.append(activity)
        # Keep last 50
        if len(self.agent_activities) > 50:
            self.agent_activities = self.agent_activities[-50:]
        
        # Track successful patterns or issues
        if success and notes:
            if notes not in self.successful_patterns:
                self.successful_patterns.append(notes)
                self.successful_patterns = self.successful_patterns[-20:]
        elif not success and notes:
            if notes not in self.known_issues:
                self.known_issues.append(notes)
                self.known_issues = self.known_issues[-10:]
        
        self.last_seen = datetime.now()
    
    def to_dict(self) -> dict:
        """Serialize for storage."""
        return {
            "use_cases": dict(self.use_cases),
            "landmarks": dict(self.landmarks),
            "shortcuts": {k: list(v) for k, v in self.shortcuts.items()},
            "navigation_tips": dict(self.navigation_tips),
            "linked_workflows": list(self.linked_workflows),
            "workflow_goals": dict(self.workflow_goals),
            "step_patterns": dict(self.step_patterns),
            # Activity context
            "user_activities": self.user_activities,
            "agent_activities": self.agent_activities,
            "successful_patterns": self.successful_patterns,
            "known_issues": self.known_issues,
            # Metadata
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "total_sessions": self.total_sessions,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "AppObservations":
        """Deserialize from storage."""
        obs = cls()
        obs.use_cases = defaultdict(int, data.get("use_cases", {}))
        obs.landmarks = defaultdict(int, data.get("landmarks", {}))
        obs.shortcuts = {
            k: tuple(v) for k, v in data.get("shortcuts", {}).items()
        }
        obs.navigation_tips = defaultdict(int, data.get("navigation_tips", {}))
        obs.linked_workflows = set(data.get("linked_workflows", []))
        obs.workflow_goals = defaultdict(int, data.get("workflow_goals", {}))
        obs.step_patterns = defaultdict(int, data.get("step_patterns", {}))
        
        # Activity context
        obs.user_activities = data.get("user_activities", [])
        obs.agent_activities = data.get("agent_activities", [])
        obs.successful_patterns = data.get("successful_patterns", [])
        obs.known_issues = data.get("known_issues", [])
        
        if data.get("first_seen"):
            obs.first_seen = datetime.fromisoformat(data["first_seen"])
        if data.get("last_seen"):
            obs.last_seen = datetime.fromisoformat(data["last_seen"])
        obs.total_sessions = data.get("total_sessions", 0)
        
        return obs


class GuideGenerator:
    """
    Generates and updates application guides from watcher observations and workflows.
    
    Integration with Workflow System:
    - Learns from workflow compositions (steps become landmarks/shortcuts)
    - Cross-links workflows to guides
    - Extracts patterns across multiple workflows for same app
    - Uses workflow goals as use cases
    
    Learning Sources:
    1. WatchSession events (real-time observation)
    2. RawWorkflowDraft (from workflow composition)
    3. Workflow objects (after composition)
    4. Cross-workflow analysis (patterns across workflows)
    """
    
    OBSERVATIONS_FILE = "observations.json"
    #: Sibling of the observations file: {storage key -> identity provenance}.
    #: Its own file on purpose — a store written before provenance was recorded
    #: simply has no such file, every key in it is unrecorded, and nothing in it
    #: travels. Merging it into observations.json would have made "missing" and
    #: "empty" indistinguishable from "old", which is the state that must fail.
    KEY_PROVENANCE_FILE = "key_provenance.json"

    def __init__(
        self,
        guide_storage: GuideStorage,
        api_key: Optional[str] = None,
        min_observations: int = 3,
        auto_generate: bool = True,
        verbose: bool = False,
        adapter: Optional[ModelAdapter] = None,
    ):
        """
        Initialize the guide generator.
        
        Args:
            guide_storage: Storage for saving guides
            api_key: Gemini API key for LLM synthesis
            min_observations: Minimum observations before generating/updating guide
            auto_generate: Automatically generate guides when threshold met
            verbose: Print debug output
        """
        self.storage = guide_storage
        self.api_key = api_key  # compatibility only; adapter owns call-time lookup
        self.min_observations = min_observations
        self.auto_generate = auto_generate
        self.verbose = verbose
        
        # Provider-free construction. A missing call-time credential simply
        # takes the existing deterministic fallback descriptions.
        self.client = adapter or ModelAdapter.for_role("planner", model=SYNTHESIS_MODEL)
        
        # Accumulated observations per app/domain
        self._observations: Dict[str, AppObservations] = {}

        # Where each key CAME FROM. See IDENTITY PROVENANCE below: this is the
        # recorded fact, and nothing downstream may re-derive it from the string.
        self._key_provenance: Dict[str, str] = {}

        # Load existing observations
        self._load_observations()
        self._load_key_provenance()

    def _get_observations_path(self):
        """Get path to observations file."""
        return self.storage.base_dir / self.OBSERVATIONS_FILE

    def _load_observations(self):
        """Load accumulated observations from disk."""
        path = self._get_observations_path()
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                for key, obs_data in data.items():
                    self._observations[key] = AppObservations.from_dict(obs_data)
                if self.verbose:
                    print(f"Loaded observations for {len(self._observations)} apps/sites")
            except Exception as e:
                if self.verbose:
                    print(f"Warning: Could not load observations: {e}")
    
    def _save_observations(self):
        """Save accumulated observations to disk."""
        path = self._get_observations_path()
        try:
            data = {key: obs.to_dict() for key, obs in self._observations.items()}
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            if self.verbose:
                print(f"Warning: Could not save observations: {e}")
    
    # ─────────────────────────────────────────────────────────────────
    # IDENTITY PROVENANCE
    #
    # A storage key is a bare string, and a bare string cannot say whether it is
    # a vendor's application name or a customer's hostname. "angebote-mueller-
    # gmbh" is both shapes at once: it is the netloc of an intranet ERP URL, and
    # it has no dot, no scheme and no slash to give that away. Every downstream
    # test that tried to tell them apart by looking (`"." in key`) laundered that
    # host into an "application" identity, and the tenant travelled.
    #
    # So the fact is recorded HERE, at the only place that knows it for certain:
    # the keyword argument the key arrived on. `url=` is url_host, permanently
    # and whatever the resulting string looks like; `app=` is os_app_name. It is
    # persisted next to the observations, copied onto the guide at creation, and
    # from then on only ever READ — never recomputed.
    # ─────────────────────────────────────────────────────────────────

    def _get_key_provenance_path(self):
        """Get path to the key-provenance file."""
        return self.storage.base_dir / self.KEY_PROVENANCE_FILE

    def _load_key_provenance(self):
        """Load recorded key provenance from disk. Missing file = nothing was
        ever recorded, so every key is unrecorded and none of them travel."""
        path = self._get_key_provenance_path()
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                for key, provenance in data.items():
                    if provenance in RECORDED_IDENTITY_PROVENANCE:
                        self._key_provenance[str(key)] = str(provenance)
            except Exception as e:
                if self.verbose:
                    print(f"Warning: Could not load key provenance: {e}")

    def _save_key_provenance(self):
        """Save recorded key provenance to disk."""
        path = self._get_key_provenance_path()
        try:
            with open(path, 'w') as f:
                json.dump(self._key_provenance, f, indent=2)
        except Exception as e:
            if self.verbose:
                print(f"Warning: Could not save key provenance: {e}")

    def _record_key_provenance(self, key: str, provenance: str):
        """Record where ``key`` came from.

        ``url_host`` is STICKY. Once a key has been derived from a visited URL,
        seeing the same string arrive later as an app name does not clear that —
        the host may be the tenant, and no later observation unlearns it. The
        record therefore only ever moves towards the refusing value.
        """
        prior = self._key_provenance.get(key)
        if IDENTITY_URL_HOST in (prior, provenance):
            provenance = IDENTITY_URL_HOST
        if prior != provenance:
            self._key_provenance[key] = provenance
            self._save_key_provenance()

    @staticmethod
    def derive_key(app: str = "", url: str = "") -> Optional[Tuple[str, str]]:
        """``(key, provenance)`` for an app name or a visited URL, or None.

        Pure: it records nothing and reads nothing. The provenance follows the
        argument, not the result — that is the whole point.
        """
        if url:
            # Extract domain from URL
            from urllib.parse import urlparse
            try:
                parsed = urlparse(url if "://" in url else f"https://{url}")
                domain = parsed.netloc.lower()
                if domain.startswith("www."):
                    domain = domain[4:]
                key = domain or None
            except Exception:
                key = url.lower()
            return (key, IDENTITY_URL_HOST) if key else None
        elif app:
            return (app.lower(), IDENTITY_OS_APP_NAME) if app.lower() else None
        return None

    def _get_key(self, app: str = "", url: str = "") -> Optional[str]:
        """Get the storage key for an app/website, recording where it came from."""
        derived = self.derive_key(app=app, url=url)
        if derived is None:
            return None
        key, provenance = derived
        self._record_key_provenance(key, provenance)
        return key
    
    def _get_observations(self, key: str) -> AppObservations:
        """Get or create observations for a key."""
        if key not in self._observations:
            self._observations[key] = AppObservations()
        return self._observations[key]
    
    # ─────────────────────────────────────────────────────────────────
    # Learning from Watch Sessions
    # ─────────────────────────────────────────────────────────────────
    
    def learn_from_session(self, session: WatchSession):
        """
        Learn from a complete watch session.
        
        Extracts:
        - Context changes → app/website usage patterns
        - Steps → UI landmarks and targets
        - Intents → use cases
        - Decisions → navigation tips
        - Keyboard events → shortcuts
        """
        current_app = ""
        current_url = ""
        
        for event in session.events:
            # Track context changes
            if event.tag_type == TagType.CONTEXT_CHANGE:
                current_app = event.data.get("app", current_app)
                current_url = event.data.get("url", current_url)
                
                key = self._get_key(current_app, current_url)
                if key:
                    self._get_observations(key).total_sessions += 1
                continue
            
            # Determine current key
            key = self._get_key(current_app, current_url)
            if not key:
                continue
            
            obs = self._get_observations(key)
            
            # Extract from STEP events
            if event.tag_type == TagType.STEP:
                # Target becomes landmark
                if event.target:
                    obs.add_landmark(event.target)
                
                # Action + target becomes step pattern
                action = event.data.get("action", "")
                if action and event.target:
                    pattern = f"{action}: {event.target}"
                    obs.add_step_pattern(pattern)
                
                # Check for keyboard shortcuts
                if action in ("key", "hotkey", "shortcut") or "key" in str(event.data):
                    keys = event.data.get("keys", event.data.get("key", ""))
                    reason = event.reason or event.target
                    if keys and reason:
                        obs.add_shortcut(reason, str(keys))
            
            # Extract from INTENT events
            elif event.tag_type == TagType.INTENT:
                description = event.data.get("description", event.reason)
                if description:
                    obs.add_use_case(description)
            
            # Extract from DECISION events
            elif event.tag_type == TagType.DECISION:
                reasoning = event.data.get("reasoning", event.reason)
                if reasoning:
                    obs.add_navigation_tip(reasoning)
            
            # Extract from OBSERVATION events
            elif event.tag_type == TagType.OBSERVATION:
                # Observations about UI can become landmarks
                observation = event.data.get("description", event.context)
                if observation and len(observation) < 100:
                    obs.add_landmark(observation)
        
        # Link workflows detected in session and record user activities
        for workflow_name in session.workflow_names:
            # Get workflow events to extract goal
            workflow_events = session.get_events_for_workflow(workflow_name)
            goal = ""
            steps_count = 0
            
            for evt in workflow_events:
                if evt.tag_type == TagType.WORKFLOW_START:
                    goal = evt.data.get("goal", "")
                elif evt.tag_type == TagType.STEP:
                    steps_count += 1
            
            for app in session.apps_used:
                key = self._get_key(app)
                if key:
                    obs = self._get_observations(key)
                    obs.add_workflow(workflow_name, goal)
                    
                    # Record as user activity
                    activity_desc = goal if goal else f"Performed workflow: {workflow_name}"
                    obs.add_user_activity(
                        description=activity_desc,
                        workflow_name=workflow_name,
                        steps_count=steps_count,
                        duration_seconds=session.duration_seconds / max(1, len(session.workflow_names)),
                    )
        
        # If no workflows detected, still record general activity
        if not session.workflow_names and session.apps_used:
            for app in session.apps_used:
                key = self._get_key(app)
                if key:
                    obs = self._get_observations(key)
                    obs.add_user_activity(
                        description=f"General usage session ({session.total_clicks} clicks)",
                        steps_count=session.total_clicks,
                        duration_seconds=session.duration_seconds,
                    )
        
        # Save observations
        self._save_observations()
        
        # Auto-generate if enabled
        if self.auto_generate:
            self._check_and_generate()
    
    # ─────────────────────────────────────────────────────────────────
    # Learning from Workflows
    # ─────────────────────────────────────────────────────────────────
    
    def learn_from_workflow_draft(self, draft: RawWorkflowDraft):
        """
        Learn from a raw workflow draft (before composition).
        
        Extracts:
        - Apps/URLs involved → create guide entries
        - Workflow goal → use case
        - Events → landmarks, shortcuts, patterns
        """
        # Process each involved app/url
        for app in draft.apps_involved:
            key = self._get_key(app)
            if key:
                obs = self._get_observations(key)
                obs.add_workflow(draft.name, draft.goal)
                
                if draft.goal:
                    obs.add_use_case(draft.goal)
        
        for url in draft.urls_visited:
            key = self._get_key(url=url)
            if key:
                obs = self._get_observations(key)
                obs.add_workflow(draft.name, draft.goal)
                
                if draft.goal:
                    obs.add_use_case(draft.goal)
        
        # Extract from events
        for event in draft.events:
            # Determine which app/url this event belongs to
            event_app = event.data.get("app", "")
            event_url = event.data.get("url", "")
            key = self._get_key(event_app, event_url)
            
            if not key:
                # Fall back to first app/url from draft
                if draft.urls_visited:
                    key = self._get_key(url=draft.urls_visited[0])
                elif draft.apps_involved:
                    key = self._get_key(draft.apps_involved[0])
            
            if not key:
                continue
            
            obs = self._get_observations(key)
            
            # Extract landmarks from targets
            if event.target:
                obs.add_landmark(event.target)
            
            # Extract step patterns
            action = event.data.get("action", "")
            if action and event.target:
                obs.add_step_pattern(f"{action}: {event.target}")
            
            # Extract reasons as navigation tips
            if event.reason:
                obs.add_navigation_tip(event.reason)
        
        self._save_observations()
        
        if self.auto_generate:
            self._check_and_generate()
    
    def learn_from_workflow(self, workflow: "Workflow"):
        """
        Learn from a composed workflow.
        
        Extracts:
        - Workflow description/goal → use case
        - Step descriptions → landmarks and patterns
        - Trigger phrases → alternative use case phrasings
        """
        from anyuse.memory.models import Workflow
        
        # We need to determine which app(s) this workflow is for
        # Try to infer from workflow tags or name
        apps = []
        
        # Check tags for app hints
        if hasattr(workflow, 'tags') and workflow.tags:
            apps.extend(workflow.tags)
        
        # If no apps identified, skip
        if not apps:
            return
        
        for app in apps:
            key = self._get_key(app)
            if not key:
                continue
            
            obs = self._get_observations(key)
            
            # Link workflow
            obs.add_workflow(workflow.name, workflow.description or "")
            
            # Workflow description as use case
            if workflow.description:
                obs.add_use_case(workflow.description)
            
            # Trigger phrases as use cases
            if hasattr(workflow, 'trigger_phrases') and workflow.trigger_phrases:
                for phrase in workflow.trigger_phrases:
                    obs.add_use_case(phrase)
            
            # Step descriptions as landmarks/patterns
            for step in workflow.steps:
                if step.description:
                    obs.add_step_pattern(step.description)
                    
                    # Short descriptions might be landmarks
                    if len(step.description) < 60:
                        obs.add_landmark(step.description)
        
        self._save_observations()
        
        if self.auto_generate:
            self._check_and_generate()
    
    def learn_from_multiple_workflows(self, workflows: List["Workflow"], app_key: str):
        """
        Extract patterns from multiple workflows for the same app.
        
        Finds common steps, landmarks, and patterns across workflows
        to build more comprehensive guides.
        """
        obs = self._get_observations(app_key)
        
        # Track step frequencies across workflows
        step_freqs: Dict[str, int] = defaultdict(int)
        target_freqs: Dict[str, int] = defaultdict(int)
        
        for workflow in workflows:
            obs.add_workflow(workflow.name, workflow.description or "")
            
            for step in workflow.steps:
                # Count step patterns
                if step.description:
                    step_freqs[step.description] += 1
                
                # Count targets
                args = step.args or {}
                if "target" in args:
                    target_freqs[args["target"]] += 1
        
        # Add frequently occurring patterns (appear in 2+ workflows)
        for pattern, count in step_freqs.items():
            if count >= 2:
                obs.add_step_pattern(pattern)
                obs.add_navigation_tip(f"Common step: {pattern}")
        
        for target, count in target_freqs.items():
            if count >= 2:
                obs.add_landmark(target)
        
        self._save_observations()
    
    # ─────────────────────────────────────────────────────────────────
    # Learning from Agent Executions
    # ─────────────────────────────────────────────────────────────────
    
    def learn_from_agent_execution(
        self,
        task: str,
        apps_used: List[str],
        urls_visited: List[str],
        success: bool,
        duration_seconds: float = 0.0,
        actions_count: int = 0,
        workflow_name: str = "",
        error: str = "",
        notes: str = "",
    ):
        """
        Learn from an agent task execution.
        
        Called after the agent completes a task to record what was done,
        whether it succeeded, and any learnings.
        
        Args:
            task: The original task description
            apps_used: List of applications that were used
            urls_visited: List of URLs that were visited
            success: Whether the task succeeded
            duration_seconds: How long execution took
            actions_count: Number of actions performed
            workflow_name: Name of workflow if one was used/created
            error: Error message if failed
            notes: Any additional notes/observations
        """
        # Determine what to record as notes
        activity_notes = notes
        if not activity_notes:
            if success:
                activity_notes = f"Completed in {actions_count} actions"
            else:
                activity_notes = error[:100] if error else "Task failed"
        
        # Record activity for each app
        for app in apps_used:
            key = self._get_key(app)
            if key:
                obs = self._get_observations(key)
                
                # Create task description
                description = f"Task: {task[:100]}"
                if workflow_name:
                    description = f"Workflow '{workflow_name}': {task[:80]}"
                
                obs.add_agent_activity(
                    description=description,
                    task=task,
                    workflow_name=workflow_name,
                    success=success,
                    steps_count=actions_count,
                    duration_seconds=duration_seconds,
                    notes=activity_notes,
                )
                
                # If successful, extract use case from task
                if success:
                    # Simplified task as use case
                    use_case = task[:80].lower()
                    obs.add_use_case(use_case)
        
        # Record activity for each URL
        for url in urls_visited:
            key = self._get_key(url=url)
            if key:
                obs = self._get_observations(key)
                
                description = f"Task: {task[:100]}"
                if workflow_name:
                    description = f"Workflow '{workflow_name}': {task[:80]}"
                
                obs.add_agent_activity(
                    description=description,
                    task=task,
                    workflow_name=workflow_name,
                    success=success,
                    steps_count=actions_count,
                    duration_seconds=duration_seconds,
                    notes=activity_notes,
                )
                
                if success:
                    use_case = task[:80].lower()
                    obs.add_use_case(use_case)
        
        self._save_observations()
        
        if self.verbose:
            status = "✓" if success else "✗"
            print(f"📚 {status} Recorded agent activity for: {', '.join(apps_used + urls_visited)}")
        
        # Auto-generate if enabled
        if self.auto_generate:
            self._check_and_generate()
    
    # ─────────────────────────────────────────────────────────────────
    # Guide Generation
    # ─────────────────────────────────────────────────────────────────
    
    def _check_and_generate(self):
        """Check if any apps have enough observations to generate/update guides."""
        for key, obs in self._observations.items():
            if obs.total_observations >= self.min_observations:
                self._generate_or_update_guide(key, obs)
    
    def generate_guides(self, force: bool = False) -> List[AppGuide]:
        """
        Generate/update guides from all accumulated observations.
        
        Args:
            force: Generate even if below observation threshold
        
        Returns:
            List of guides that were created or updated
        """
        updated_guides = []
        
        for key, obs in self._observations.items():
            if not force and obs.total_observations < self.min_observations:
                continue
            
            guide = self._generate_or_update_guide(key, obs)
            if guide:
                updated_guides.append(guide)
        
        return updated_guides
    
    def _generate_or_update_guide(self, key: str, obs: AppObservations) -> Optional[AppGuide]:
        """Generate or update a guide for a specific app/website."""
        # Check if guide exists
        existing = self.storage.get(key) or self.storage.get_by_app(key)
        
        if existing:
            guide = self._update_guide(existing, obs)
        else:
            guide = self._create_guide(key, obs)
        
        if guide:
            self.storage.save(guide)
            if self.verbose:
                print(f"{'Updated' if existing else 'Created'} guide: {guide.name}")
        
        return guide
    
    def _create_guide(self, key: str, obs: AppObservations) -> AppGuide:
        """Create a new guide from observations.

        The guide's KIND and its IDENTITY PROVENANCE both come from the recorded
        fact about ``key`` — see IDENTITY PROVENANCE above. A key derived from a
        `url=` argument makes a website guide no matter how the host reads: an
        intranet ERP at ``http://angebote-mueller-gmbh/`` used to fail the dot
        test, become an application guide with no domain, and carry the customer
        onto the wire as an "OS app name".
        """
        provenance = self._key_provenance.get(key)
        if provenance in RECORDED_IDENTITY_PROVENANCE:
            is_website = provenance == IDENTITY_URL_HOST
        else:
            # A key carried over from an observations file written before the
            # provenance was recorded. The kind still has to be decided for LOCAL
            # lookup (storage.get_by_url and friends), and the old guess is as
            # good as it ever was for that — but a guess is not a record: the
            # guide is stamped with nothing and the projection refuses it.
            is_website = "." in key and not key.startswith("/")

        # Get top items by frequency
        top_use_cases = obs.get_top_items(obs.use_cases, 5)
        top_landmarks = obs.get_top_items(obs.landmarks, 10)
        top_tips = obs.get_top_items(obs.navigation_tips, 8)
        
        # Format shortcuts
        top_shortcuts = {}
        sorted_shortcuts = sorted(
            obs.shortcuts.items(),
            key=lambda x: x[1][1],  # Sort by count
            reverse=True
        )[:10]
        for action, (keys, _) in sorted_shortcuts:
            top_shortcuts[action] = keys
        
        # Try to synthesize a description using LLM
        description = self._synthesize_description(key, obs)
        
        if is_website:
            guide = AppGuide.create_website_guide(
                name=key,
                domain=key,
                short_description=description,
                use_cases=top_use_cases,
            )
        else:
            guide = AppGuide.create_app_guide(
                name=key,
                short_description=description,
                use_cases=top_use_cases,
            )

        # Stamp the recorded fact, or leave it unstamped when there is none.
        # The constructors deliberately do not do this for us: create_app_guide
        # is reached from the unrecorded branch above too, and having it assert
        # "os_app_name" would launder the guess straight back into a provenance.
        guide.identity_provenance = (
            provenance if provenance in RECORDED_IDENTITY_PROVENANCE else None
        )

        # Add observations
        guide.ui_landmarks = top_landmarks
        guide.common_shortcuts = top_shortcuts
        guide.navigation_tips = top_tips
        guide.linked_workflows = list(obs.linked_workflows)
        
        # Add activity context
        from anyuse.guides.models import ActivityRecord
        guide.user_activities = [
            ActivityRecord.from_dict(a) for a in obs.user_activities[-20:]
        ]
        guide.agent_activities = [
            ActivityRecord.from_dict(a) for a in obs.agent_activities[-20:]
        ]
        guide.successful_patterns = obs.successful_patterns[:10]
        guide.known_issues = obs.known_issues[:5]
        
        # Compute common tasks from activity history
        guide.update_common_tasks()
        
        # Generate full guide content if we have enough data
        if obs.total_observations >= 10 and self.client:
            guide.full_guide = self._synthesize_full_guide(key, obs)
        
        return guide
    
    def _update_guide(self, guide: AppGuide, obs: AppObservations) -> AppGuide:
        """Update an existing guide with new observations."""
        # Get new top items
        new_use_cases = obs.get_top_items(obs.use_cases, 10)
        new_landmarks = obs.get_top_items(obs.landmarks, 15)
        new_tips = obs.get_top_items(obs.navigation_tips, 12)
        
        # Merge use cases (add new ones)
        existing_cases = set(uc.lower() for uc in guide.use_cases)
        for case in new_use_cases:
            if case.lower() not in existing_cases:
                guide.use_cases.append(case)
        guide.use_cases = guide.use_cases[:10]  # Keep top 10
        
        # Merge landmarks
        existing_landmarks = set(lm.lower() for lm in guide.ui_landmarks)
        for landmark in new_landmarks:
            if landmark.lower() not in existing_landmarks:
                guide.ui_landmarks.append(landmark)
        guide.ui_landmarks = guide.ui_landmarks[:15]
        
        # Merge shortcuts
        sorted_shortcuts = sorted(
            obs.shortcuts.items(),
            key=lambda x: x[1][1],
            reverse=True
        )
        for action, (keys, _) in sorted_shortcuts[:10]:
            if action not in guide.common_shortcuts:
                guide.common_shortcuts[action] = keys
        
        # Merge navigation tips
        existing_tips = set(tip.lower() for tip in guide.navigation_tips)
        for tip in new_tips:
            if tip.lower() not in existing_tips:
                guide.navigation_tips.append(tip)
        guide.navigation_tips = guide.navigation_tips[:12]
        
        # Merge workflows
        existing_workflows = set(guide.linked_workflows)
        for wf in obs.linked_workflows:
            if wf not in existing_workflows:
                guide.linked_workflows.append(wf)
        
        # Merge activity context
        from anyuse.guides.models import ActivityRecord
        
        # Add new user activities
        existing_user_timestamps = {a.timestamp for a in guide.user_activities}
        for activity_dict in obs.user_activities[-20:]:
            if activity_dict.get("timestamp") not in existing_user_timestamps:
                guide.user_activities.append(ActivityRecord.from_dict(activity_dict))
        guide.user_activities = guide.user_activities[-50:]
        
        # Add new agent activities
        existing_agent_timestamps = {a.timestamp for a in guide.agent_activities}
        for activity_dict in obs.agent_activities[-20:]:
            if activity_dict.get("timestamp") not in existing_agent_timestamps:
                guide.agent_activities.append(ActivityRecord.from_dict(activity_dict))
        guide.agent_activities = guide.agent_activities[-50:]
        
        # Merge successful patterns and known issues
        for pattern in obs.successful_patterns:
            if pattern not in guide.successful_patterns:
                guide.successful_patterns.append(pattern)
        guide.successful_patterns = guide.successful_patterns[-20:]
        
        for issue in obs.known_issues:
            if issue not in guide.known_issues:
                guide.known_issues.append(issue)
        guide.known_issues = guide.known_issues[-10:]
        
        # Update common tasks
        guide.update_common_tasks()
        
        # Increment version
        guide.version += 1
        
        # Re-synthesize full guide if significantly more data
        if obs.total_observations >= 20 and self.client and not guide.full_guide:
            guide.full_guide = self._synthesize_full_guide(guide.name, obs)
        
        return guide
    
    def _synthesize_description(self, key: str, obs: AppObservations) -> str:
        """Use LLM to synthesize a short description."""
        if not self.client:
            return f"Auto-learned guide for {key}"
        
        try:
            prompt = f"""Based on these observations of user behavior with "{key}", 
write a 1-2 sentence description of what this app/website is used for.

Observed use cases: {list(obs.use_cases.keys())[:5]}
Common actions: {list(obs.step_patterns.keys())[:5]}
Related workflows: {list(obs.linked_workflows)[:3]}

Keep it concise and informative."""

            response = self.client.complete(
                [{"role": "user", "content": prompt}], temperature=0.3,
                max_tokens=200,
            )
            if not response.ok:
                raise RuntimeError(response.error or "guide synthesis provider failed")
            return response.text.strip()[:200]
            
        except Exception as e:
            if self.verbose:
                print(f"Description synthesis failed: {e}")
            return f"Auto-learned guide for {key}"
    
    def _synthesize_full_guide(self, key: str, obs: AppObservations) -> str:
        """Use LLM to synthesize a comprehensive guide."""
        if not self.client:
            return ""
        
        try:
            prompt = f"""Create a helpful guide for "{key}" based on observed user behavior.

**Observed Data:**

Use cases (what users do):
{json.dumps(list(obs.use_cases.keys())[:10], indent=2)}

UI landmarks (things users interact with):
{json.dumps(list(obs.landmarks.keys())[:15], indent=2)}

Keyboard shortcuts used:
{json.dumps({k: v[0] for k, v in list(obs.shortcuts.items())[:10]}, indent=2)}

Navigation patterns:
{json.dumps(list(obs.navigation_tips.keys())[:10], indent=2)}

Common step patterns:
{json.dumps(list(obs.step_patterns.keys())[:10], indent=2)}

Related workflows:
{list(obs.linked_workflows)}

**Generate a markdown guide that includes:**
1. Overview (2-3 sentences)
2. Key Features & Use Cases
3. Navigation Tips (how to get around)
4. Useful Keyboard Shortcuts
5. Common Workflows (reference the related workflows)

Keep it practical and actionable. Focus on the most frequently observed patterns."""

            response = self.client.complete(
                [{"role": "user", "content": prompt}], temperature=0.4,
                max_tokens=2000,
            )
            if not response.ok:
                raise RuntimeError(response.error or "guide synthesis provider failed")
            return response.text.strip()
            
        except Exception as e:
            if self.verbose:
                print(f"Full guide synthesis failed: {e}")
            return ""
    
    # ─────────────────────────────────────────────────────────────────
    # Learning from Execution Traces (State Graph)
    # ─────────────────────────────────────────────────────────────────

    def learn_from_execution(self, execution_trace: dict, app_name: str):
        """
        After a workflow executes, update the guide's state graph.

        Merges the discovered states and transitions from an execution
        trace into the guide for the given app.

        Args:
            execution_trace: dict with keys:
                - steps: list of step dicts, each containing:
                    - before_ax: list of AX element dicts (role, title, ...)
                    - after_ax: list of AX element dicts
                    - intent: str describing what the step does
                    - implementations: list of TransitionImpl dicts
                    - url: optional URL at time of step
                    - title: optional window title at time of step
                - app: application name
            app_name: which app guide to update
        """
        from anyuse.guides.state_graph import (
            StateGraph, UIState, UIFingerprint, UIElement,
            UITransition, TransitionImpl, compute_structural_hash,
        )

        key = self._get_key(app_name)
        if not key:
            return

        # Get or create guide
        guide = self.storage.get(key) or self.storage.get_by_app(key)
        if guide is None:
            # Create a minimal guide so we can attach the state graph
            guide = self._create_guide(key, self._get_observations(key))
            if guide is None:
                return

        graph = guide.get_state_graph()
        now_iso = datetime.now().isoformat()

        steps = execution_trace.get("steps", [])
        previous_state_id = None

        for step in steps:
            before_elements = step.get("before_ax", [])
            after_elements = step.get("after_ax", [])
            intent = step.get("intent", "")
            impl_dicts = step.get("implementations", [])
            url = step.get("url", "")
            title = step.get("title", "")

            # Build before-state
            before_state = self._build_state_from_elements(
                before_elements, app_name, url, title, now_iso
            )
            if before_state is not None:
                graph.add_state(before_state)
                previous_state_id = before_state.state_id

            # Build after-state
            after_state = self._build_state_from_elements(
                after_elements, app_name, url, title, now_iso
            )
            after_state_id = None
            if after_state is not None:
                graph.add_state(after_state)
                after_state_id = after_state.state_id

            # Build transition
            from_id = previous_state_id
            to_id = after_state_id
            if from_id and to_id and intent:
                implementations = [
                    TransitionImpl.from_dict(d) for d in impl_dicts
                ]
                transition = UITransition(
                    from_state=from_id,
                    to_state=to_id,
                    intent=intent,
                    implementations=implementations,
                    times_executed=1,
                    times_succeeded=1,
                    last_executed=now_iso,
                )
                graph.add_transition(transition)

            # Advance
            if after_state_id:
                previous_state_id = after_state_id

        # Save updated guide
        guide.set_state_graph(graph)
        self.storage.save(guide)

        if self.verbose:
            state_count = len(graph.states)
            trans_count = len(graph.transitions)
            print(
                f"Updated state graph for {app_name}: "
                f"{state_count} states, {trans_count} transitions"
            )

    def learn_states_from_observation(
        self, ax_snapshot_data: dict, app_name: str, url: str = ""
    ):
        """
        BatchWatcher observed a page. Add its elements to the state graph passively.

        This is a lightweight version of learn_from_execution: it only adds
        a state (no transition), useful for passively growing the graph as
        the watcher observes the user browsing.

        Args:
            ax_snapshot_data: dict with key "elements" containing a list of
                AX element dicts (each with at least "role" and optionally
                "title", "identifier", "description").
            app_name: Application name.
            url: Optional URL if the observation is from a browser.
        """
        from anyuse.guides.state_graph import StateGraph

        key = self._get_key(app_name, url)
        if not key:
            return

        guide = self.storage.get(key) or self.storage.get_by_app(key)
        if guide is None:
            # If no guide exists yet and we have a URL, try URL-based lookup
            if url:
                guide = self.storage.get_by_url(url)
            if guide is None:
                return

        elements = ax_snapshot_data.get("elements", [])
        if not elements:
            return

        now_iso = datetime.now().isoformat()
        state = self._build_state_from_elements(
            elements, app_name, url, "", now_iso
        )
        if state is None:
            return

        graph = guide.get_state_graph()
        graph.add_state(state)
        guide.set_state_graph(graph)
        self.storage.save(guide)

        if self.verbose:
            print(
                f"Passively added state {state.state_id} to {guide.name} "
                f"({len(state.elements)} elements)"
            )

    def _build_state_from_elements(
        self,
        elements: List[dict],
        app_name: str,
        url: str,
        title: str,
        now_iso: str,
    ) -> Optional["UIState"]:
        """
        Build a UIState from a list of AX element dicts.

        Shared helper for learn_from_execution and learn_states_from_observation.
        Returns None if state_graph imports are unavailable.
        """
        try:
            from anyuse.guides.state_graph import (
                UIState, UIFingerprint, UIElement, compute_structural_hash,
                generate_state_id, _role_frequency,
            )
            from anyuse.context.accessibility import ACTIONABLE_ROLES
        except ImportError:
            return None

        if not elements:
            return None

        # Build fingerprint. ACTIONABLE_ROLES is imported, not redefined: this
        # module used to carry its own shorter set, which made "actionable" mean
        # two different things and fed a second state-id scheme
        #+(c)).
        actionable = [el for el in elements if el.get("role", "") in ACTIONABLE_ROLES]
        required_elements = [
            {"role": el["role"], **({"title": el["title"]} if el.get("title") else {})}
            for el in actionable
        ]
        struct_hash = compute_structural_hash(elements)

        count = len(elements)
        fingerprint = UIFingerprint(
            required_elements=required_elements,
            element_count_range=(max(0, count - 20), count + 20),
            structural_hash=struct_hash,
            # The structural hash is one-way; without the decoded distribution
            # every similarity against this fingerprint is 0.0 (defect (d)).
            role_frequency=_role_frequency(elements),
        )

        # THE state id — the same title-insensitive function the recorder uses,
        # over the same actionable subset, so both producers now agree.
        state_id = generate_state_id(actionable, app_name)

        # Build UIElements
        role_to_type = {
            "AXButton": "button", "AXTextField": "text_field",
            "AXTextArea": "text_field", "AXCheckBox": "checkbox",
            "AXRadioButton": "radio", "AXPopUpButton": "popup_button",
            "AXComboBox": "combo_box", "AXSlider": "slider",
            "AXLink": "link", "AXMenuItem": "menu_item",
            "AXMenuBarItem": "menu_bar_item", "AXTab": "tab",
            "AXSwitch": "switch",
        }
        ui_elements = []
        for el in actionable:
            role = el.get("role", "")
            el_type = role_to_type.get(role, "button")
            accepts_input = role in ("AXTextField", "AXTextArea", "AXComboBox")
            actions = []
            if role in ("AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXTab"):
                actions.append("press")
            if role in ("AXTextField", "AXTextArea", "AXComboBox"):
                actions.append("set_value")
            if role in ("AXCheckBox", "AXRadioButton", "AXSwitch"):
                actions.append("toggle")

            ui_elements.append(UIElement(
                role=role,
                title=el.get("title", ""),
                identifier=el.get("identifier"),
                description=el.get("description", ""),
                element_type=el_type,
                actions=actions,
                accepts_input=accepts_input,
                times_seen=1,
                last_seen=now_iso,
            ))

        state = UIState(
            state_id=state_id,
            app=app_name,
            fingerprint=fingerprint,
            elements=ui_elements,
            times_seen=1,
            last_seen=now_iso,
        )
        if url:
            state.url_patterns.append(url)
        if title:
            state.title_patterns.append(title)

        return state

    # ─────────────────────────────────────────────────────────────────
    # Query Methods
    # ─────────────────────────────────────────────────────────────────

    def get_observations(self, key: str) -> Optional[AppObservations]:
        """Get observations for an app/website.

        A LOOKUP, so it uses the pure ``derive_key`` and records nothing. Asking
        about a key must never invent a provenance for it: the caller here hands
        over a bare string with no idea where it came from, and treating that as
        "arrived on app=" would launder an unrecorded host into an app name — the
        exact move this whole mechanism exists to stop.
        """
        derived = self.derive_key(app=key)
        key = derived[0] if derived else key.lower()
        return self._observations.get(key)
    
    def list_observed_apps(self) -> List[Tuple[str, int]]:
        """List all observed apps with their observation counts."""
        return [
            (key, obs.total_observations)
            for key, obs in sorted(
                self._observations.items(),
                key=lambda x: x[1].total_observations,
                reverse=True
            )
        ]
    
    def get_stats(self) -> Dict:
        """Get generator statistics."""
        return {
            "apps_observed": len(self._observations),
            "total_observations": sum(
                obs.total_observations for obs in self._observations.values()
            ),
            "guides_generated": len([
                k for k, obs in self._observations.items()
                if self.storage.get(k) or self.storage.get_by_app(k)
            ]),
            "pending_generation": len([
                k for k, obs in self._observations.items()
                if obs.total_observations >= self.min_observations
                and not (self.storage.get(k) or self.storage.get_by_app(k))
            ]),
        }


# ─────────────────────────────────────────────────────────────────────
# Integration Functions
# ─────────────────────────────────────────────────────────────────────

def create_integrated_learner(
    guide_storage: GuideStorage,
    workflow_storage: "WorkflowStorage",
    api_key: Optional[str] = None,
    verbose: bool = False,
) -> "IntegratedLearner":
    """
    Create an integrated learner that updates both workflows and guides.
    
    Returns an IntegratedLearner that can be used as a callback
    for the watcher system.
    """
    return IntegratedLearner(guide_storage, workflow_storage, api_key, verbose)


class IntegratedLearner:
    """
    Coordinates learning between workflow and guide systems.
    
    Use as a callback for workflow composition:
        learner = IntegratedLearner(guide_storage, workflow_storage)
        composer.on_workflow_composed = learner.on_workflow_composed
    """
    
    def __init__(
        self,
        guide_storage: GuideStorage,
        workflow_storage: "WorkflowStorage",
        api_key: Optional[str] = None,
        verbose: bool = False,
    ):
        self.guide_storage = guide_storage
        self.workflow_storage = workflow_storage
        self.api_key = api_key
        self.verbose = verbose
        
        # Initialize generator
        self.guide_generator = GuideGenerator(
            guide_storage=guide_storage,
            api_key=api_key,
            verbose=verbose,
        )
        
        # Track processed items to avoid duplicates
        self._processed_sessions: Set[str] = set()
        self._processed_workflows: Set[str] = set()
    
    def on_session_complete(self, session: WatchSession):
        """
        Called when a watch session completes.
        
        Updates guide observations from the session.
        """
        if session.id in self._processed_sessions:
            return
        
        self._processed_sessions.add(session.id)
        
        if self.verbose:
            print(f"📚 Learning from session {session.id}...")
        
        self.guide_generator.learn_from_session(session)
    
    def on_workflow_learned(self, draft: RawWorkflowDraft):
        """
        Called when a workflow draft is created from watching.
        
        Updates guide observations from the workflow.
        """
        if draft.name in self._processed_workflows:
            return
        
        self._processed_workflows.add(draft.name)
        
        if self.verbose:
            print(f"📚 Learning from workflow draft: {draft.name}")
        
        self.guide_generator.learn_from_workflow_draft(draft)
        
        # Auto-link workflow to relevant guides
        self._link_workflow_to_guides(draft)
    
    def on_workflow_composed(self, workflow: "Workflow"):
        """
        Called after a workflow is fully composed.
        
        Extracts additional patterns and links to guides.
        """
        if self.verbose:
            print(f"📚 Learning from composed workflow: {workflow.name}")
        
        self.guide_generator.learn_from_workflow(workflow)
    
    def _link_workflow_to_guides(self, draft: RawWorkflowDraft):
        """Link a workflow to its related guides."""
        # Link to app guides
        for app in draft.apps_involved:
            guide = self.guide_storage.get_by_app(app)
            if guide:
                self.guide_storage.link_workflow_to_guide(guide.name, draft.name)
                if self.verbose:
                    print(f"  Linked {draft.name} → {guide.name}")
        
        # Link to website guides
        for url in draft.urls_visited:
            guide = self.guide_storage.get_by_url(url)
            if guide:
                self.guide_storage.link_workflow_to_guide(guide.name, draft.name)
                if self.verbose:
                    print(f"  Linked {draft.name} → {guide.name}")
    
    def analyze_cross_workflow_patterns(self, app_key: str):
        """
        Analyze patterns across all workflows for an app.
        
        Call this periodically or when enough workflows exist.
        """
        # Get all workflows linked to this app's guide
        guide = self.guide_storage.get(app_key) or self.guide_storage.get_by_app(app_key)
        
        if not guide or not guide.linked_workflows:
            return
        
        # Load workflows
        workflows = []
        for wf_name in guide.linked_workflows:
            wf = self.workflow_storage.get(wf_name)
            if wf:
                workflows.append(wf)
        
        if len(workflows) >= 2:
            if self.verbose:
                print(f"📊 Analyzing {len(workflows)} workflows for {app_key}")
            
            self.guide_generator.learn_from_multiple_workflows(workflows, app_key)
    
    def on_agent_execution(
        self,
        task: str,
        result: "AgentResult",
        apps_used: Optional[List[str]] = None,
        urls_visited: Optional[List[str]] = None,
    ):
        """
        Called after agent completes a task execution.
        
        Records what the agent did, whether it succeeded, and any learnings.
        
        Args:
            task: The original task
            result: AgentResult from execution
            apps_used: Apps that were used (extracted from context if not provided)
            urls_visited: URLs visited (extracted from actions if not provided)
        """
        # Extract apps from result if not provided
        if apps_used is None:
            apps_used = []
            # Try to extract from workflow match
            if result.workflow_matched:
                wf = self.workflow_storage.get(result.workflow_matched)
                if wf and hasattr(wf, 'tags'):
                    apps_used = [t for t in wf.tags if not "." in t]
        
        # Extract URLs from actions if not provided
        if urls_visited is None:
            urls_visited = []
            for action in (result.actions or []):
                if action.get("action") == "navigate":
                    url = action.get("args", {}).get("url", "")
                    if url and url not in urls_visited:
                        urls_visited.append(url)
        
        # Learn from the execution
        self.guide_generator.learn_from_agent_execution(
            task=task,
            apps_used=apps_used,
            urls_visited=urls_visited,
            success=result.success,
            duration_seconds=result.duration_seconds,
            actions_count=len(result.actions or []),
            workflow_name=result.workflow_matched or "",
            error=result.error or "",
            notes=result.message or "",
        )
    
    def get_stats(self) -> Dict:
        """Get learner statistics."""
        return {
            "sessions_processed": len(self._processed_sessions),
            "workflows_processed": len(self._processed_workflows),
            "guide_generator": self.guide_generator.get_stats(),
        }
