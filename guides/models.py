"""
Guide Models - Data structures for application/website guides.

Guides provide "how-to" documentation for applications and websites
that help the AI agent understand how to navigate and use them.
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from datetime import datetime
from enum import Enum
from urllib.parse import urlparse
import re


class GuideType(Enum):
    """Type of guide - application or website."""
    APPLICATION = "application"
    WEBSITE = "website"


@dataclass
class ActivityRecord:
    """
    Record of an activity performed in an app.
    
    Captures what was done, by whom (user or agent), and the outcome.
    """
    description: str                    # "Created a new repository"
    source: str = "user"               # "user" (watched) or "agent" (executed)
    task: str = ""                     # Original task/goal if from agent
    workflow_name: str = ""            # Associated workflow if any
    success: bool = True               # Did the activity succeed?
    timestamp: str = ""                # When this happened
    duration_seconds: float = 0.0      # How long it took
    steps_count: int = 0               # Number of steps involved
    notes: str = ""                    # Additional context/observations
    
    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "source": self.source,
            "task": self.task,
            "workflow_name": self.workflow_name,
            "success": self.success,
            "timestamp": self.timestamp,
            "duration_seconds": self.duration_seconds,
            "steps_count": self.steps_count,
            "notes": self.notes,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ActivityRecord":
        return cls(
            description=data.get("description", ""),
            source=data.get("source", "user"),
            task=data.get("task", ""),
            workflow_name=data.get("workflow_name", ""),
            success=data.get("success", True),
            timestamp=data.get("timestamp", ""),
            duration_seconds=data.get("duration_seconds", 0.0),
            steps_count=data.get("steps_count", 0),
            notes=data.get("notes", ""),
        )


@dataclass
class AppGuide:
    """
    Documentation guide for an application or website.
    
    Contains:
    - Short description for context summaries
    - Full guide with detailed how-to instructions
    - UI landmarks and navigation tips
    - Common shortcuts
    - Use cases
    - Linked workflows
    - Activity history (what users/agent have done in this app)
    """
    name: str                                    # "Google Chrome", "github.com"
    guide_type: GuideType                        # application or website
    short_description: str = ""                  # 1-2 sentences for context summary
    full_guide: str = ""                         # Full markdown how-to documentation
    
    # App identification
    domain: Optional[str] = None                 # For websites: "github.com"
    app_aliases: List[str] = field(default_factory=list)  # ["Chrome", "Google Chrome"]
    bundle_id: Optional[str] = None              # macOS bundle ID for apps
    # WHERE `name` came from, recorded by whoever derived it and NEVER re-derived
    # from the string afterwards: "os_app_name" (the OS-reported application name)
    # or "url_host" (the host of a visited URL, which may be the tenant). None
    # means nobody recorded it, and an unrecorded identity does not travel — see
    # APP IDENTITY NEEDS A PROVENANCE in guides/projection.py.
    identity_provenance: Optional[str] = None
    
    # Documentation details
    use_cases: List[str] = field(default_factory=list)    # ["browse web", "manage tabs"]
    ui_landmarks: List[str] = field(default_factory=list) # ["address bar at top", "tabs below"]
    common_shortcuts: Dict[str, str] = field(default_factory=dict)  # {"new tab": "Cmd+T"}
    navigation_tips: List[str] = field(default_factory=list)
    
    # Linked workflows
    linked_workflows: List[str] = field(default_factory=list)  # Workflow IDs
    
    # Activity context - what has been done in this app
    user_activities: List[ActivityRecord] = field(default_factory=list)   # From watching user
    agent_activities: List[ActivityRecord] = field(default_factory=list)  # From agent executions
    common_tasks: List[str] = field(default_factory=list)    # Most frequent tasks
    successful_patterns: List[str] = field(default_factory=list)  # What works well
    known_issues: List[str] = field(default_factory=list)    # What to avoid/watch out for
    
    # Metadata
    created_at: str = ""
    updated_at: str = ""
    version: int = 1

    # UI State Graph (Plan 10)
    ui_states: Dict[str, dict] = field(default_factory=dict)       # serialized UIState objects
    ui_transitions: List[dict] = field(default_factory=list)        # serialized UITransition objects
    
    @property
    def is_website(self) -> bool:
        """Check if this is a website guide."""
        return self.guide_type == GuideType.WEBSITE
    
    @property
    def is_application(self) -> bool:
        """Check if this is an application guide."""
        return self.guide_type == GuideType.APPLICATION
    
    @property
    def filename(self) -> str:
        """Get safe filename for storage."""
        # Convert name to safe filename
        safe_name = re.sub(r'[^\w\s-]', '', self.name.lower())
        safe_name = re.sub(r'[-\s]+', '_', safe_name)
        return f"{safe_name}.json"
    
    def matches_app(self, app_name: str) -> bool:
        """
        Check if this guide matches an application name.
        
        Handles variations like "Google Chrome" vs "Chrome".
        """
        if not self.is_application:
            return False
        
        app_lower = app_name.lower()
        
        # Exact match
        if self.name.lower() == app_lower:
            return True
        
        # Check aliases
        for alias in self.app_aliases:
            if alias.lower() == app_lower:
                return True
        
        # Partial match (e.g., "Chrome" matches "Google Chrome")
        if app_lower in self.name.lower() or self.name.lower() in app_lower:
            return True
        
        # Check bundle ID
        if self.bundle_id and self.bundle_id.lower() == app_lower:
            return True
        
        return False
    
    def matches_url(self, url: str) -> bool:
        """
        Check if this guide matches a URL.
        
        Args:
            url: URL to check against
        
        Returns:
            True if this guide covers the given URL's domain
        """
        if not self.is_website:
            return False
        
        try:
            parsed = urlparse(url)
            url_domain = parsed.netloc.lower()
            
            # Remove www. prefix for matching
            if url_domain.startswith('www.'):
                url_domain = url_domain[4:]
            
            guide_domain = (self.domain or self.name).lower()
            if guide_domain.startswith('www.'):
                guide_domain = guide_domain[4:]
            
            # Exact domain match
            if url_domain == guide_domain:
                return True
            
            # Subdomain match (e.g., api.github.com matches github.com)
            if url_domain.endswith('.' + guide_domain):
                return True
            
            return False
        except Exception:
            return False
    
    def get_short_summary(self) -> str:
        """
        Get a short summary for context inclusion.
        
        Format: "  - Name: short description (use cases)"
        """
        use_cases_str = ", ".join(self.use_cases[:3]) if self.use_cases else "general use"
        return f"  - {self.name}: {self.short_description} ({use_cases_str})"
    
    def get_full_context(self) -> str:
        """
        Get full guide documentation for AI context.
        
        Returns markdown-formatted guide content including activity history.
        """
        lines = [
            f"# {self.name} Guide",
            f"Type: {self.guide_type.value}",
            "",
        ]
        
        if self.short_description:
            lines.append(self.short_description)
            lines.append("")
        
        if self.use_cases:
            lines.append("## Use Cases")
            for uc in self.use_cases:
                lines.append(f"- {uc}")
            lines.append("")
        
        if self.ui_landmarks:
            lines.append("## UI Landmarks")
            for landmark in self.ui_landmarks:
                lines.append(f"- {landmark}")
            lines.append("")
        
        if self.common_shortcuts:
            lines.append("## Keyboard Shortcuts")
            for action, shortcut in self.common_shortcuts.items():
                lines.append(f"- {action}: {shortcut}")
            lines.append("")
        
        if self.navigation_tips:
            lines.append("## Navigation Tips")
            for tip in self.navigation_tips:
                lines.append(f"- {tip}")
            lines.append("")
        
        # Activity context - what has been done in this app
        if self.common_tasks:
            lines.append("## Common Tasks in This App")
            for task in self.common_tasks[:10]:
                lines.append(f"- {task}")
            lines.append("")
        
        if self.successful_patterns:
            lines.append("## What Works Well")
            for pattern in self.successful_patterns[:5]:
                lines.append(f"- {pattern}")
            lines.append("")
        
        if self.known_issues:
            lines.append("## Known Issues / Watch Out For")
            for issue in self.known_issues[:5]:
                lines.append(f"- ⚠️ {issue}")
            lines.append("")
        
        # Recent agent activity (helps agent know what it has done before)
        recent_agent = [a for a in self.agent_activities if a.success][-5:]
        if recent_agent:
            lines.append("## Recent Agent Activity (What I've Done Before)")
            for activity in recent_agent:
                result = "✓" if activity.success else "✗"
                lines.append(f"- {result} {activity.description}")
                if activity.task:
                    lines.append(f"  Task: {activity.task}")
            lines.append("")
        
        # Recent user activity (helps understand user patterns)
        recent_user = self.user_activities[-5:]
        if recent_user:
            lines.append("## Recent User Activity (What User Does)")
            for activity in recent_user:
                lines.append(f"- {activity.description}")
                if activity.workflow_name:
                    lines.append(f"  (Workflow: {activity.workflow_name})")
            lines.append("")
        
        if self.full_guide:
            lines.append("## Detailed Guide")
            lines.append(self.full_guide)
            lines.append("")
        
        if self.linked_workflows:
            lines.append("## Related Workflows")
            lines.append(f"Available workflows: {', '.join(self.linked_workflows[:10])}")
            lines.append("")
        
        return "\n".join(lines)
    
    def add_user_activity(
        self,
        description: str,
        workflow_name: str = "",
        steps_count: int = 0,
        duration_seconds: float = 0.0,
        notes: str = "",
    ):
        """Add a user activity record (from watching)."""
        activity = ActivityRecord(
            description=description,
            source="user",
            workflow_name=workflow_name,
            success=True,
            timestamp=datetime.now().isoformat(),
            duration_seconds=duration_seconds,
            steps_count=steps_count,
            notes=notes,
        )
        self.user_activities.append(activity)
        
        # Keep only last 50 user activities
        if len(self.user_activities) > 50:
            self.user_activities = self.user_activities[-50:]
    
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
        """Add an agent activity record (from execution)."""
        activity = ActivityRecord(
            description=description,
            source="agent",
            task=task,
            workflow_name=workflow_name,
            success=success,
            timestamp=datetime.now().isoformat(),
            duration_seconds=duration_seconds,
            steps_count=steps_count,
            notes=notes,
        )
        self.agent_activities.append(activity)
        
        # Keep only last 50 agent activities
        if len(self.agent_activities) > 50:
            self.agent_activities = self.agent_activities[-50:]
        
        # Update successful patterns or known issues
        if success and notes:
            if notes not in self.successful_patterns:
                self.successful_patterns.append(notes)
                self.successful_patterns = self.successful_patterns[-20:]
        elif not success and notes:
            if notes not in self.known_issues:
                self.known_issues.append(notes)
                self.known_issues = self.known_issues[-10:]
    
    def update_common_tasks(self):
        """Update common_tasks based on activity history."""
        from collections import Counter
        
        # Count task descriptions
        all_tasks = []
        for a in self.user_activities:
            if a.description:
                all_tasks.append(a.description.lower()[:80])
        for a in self.agent_activities:
            if a.task:
                all_tasks.append(a.task.lower()[:80])
            elif a.description:
                all_tasks.append(a.description.lower()[:80])
        
        # Get most common
        counter = Counter(all_tasks)
        self.common_tasks = [task for task, _ in counter.most_common(15)]
    
    def link_workflow(self, workflow_id: str):
        """Link a workflow to this guide."""
        if workflow_id not in self.linked_workflows:
            self.linked_workflows.append(workflow_id)
    
    def unlink_workflow(self, workflow_id: str):
        """Unlink a workflow from this guide."""
        if workflow_id in self.linked_workflows:
            self.linked_workflows.remove(workflow_id)
    
    def get_state_graph(self) -> "StateGraph":
        """
        Deserialize and return the StateGraph for this guide.

        Returns a fully hydrated StateGraph instance from the serialized
        ui_states and ui_transitions stored on this guide.
        """
        from anyuse.guides.state_graph import StateGraph

        return StateGraph.from_dict({
            "states": self.ui_states,
            "transitions": self.ui_transitions,
        })

    def set_state_graph(self, graph: "StateGraph") -> None:
        """
        Serialize and store the StateGraph on this guide.

        Converts the StateGraph to serialized dicts for JSON-safe storage.
        """
        data = graph.to_dict()
        self.ui_states = data.get("states", {})
        self.ui_transitions = data.get("transitions", [])

    def to_dict(self) -> dict:
        """Serialize guide to dictionary."""
        return {
            "name": self.name,
            "guide_type": self.guide_type.value,
            "short_description": self.short_description,
            "full_guide": self.full_guide,
            "domain": self.domain,
            "app_aliases": self.app_aliases,
            "bundle_id": self.bundle_id,
            "identity_provenance": self.identity_provenance,
            "use_cases": self.use_cases,
            "ui_landmarks": self.ui_landmarks,
            "common_shortcuts": self.common_shortcuts,
            "navigation_tips": self.navigation_tips,
            "linked_workflows": self.linked_workflows,
            # Activity context
            "user_activities": [a.to_dict() for a in self.user_activities],
            "agent_activities": [a.to_dict() for a in self.agent_activities],
            "common_tasks": self.common_tasks,
            "successful_patterns": self.successful_patterns,
            "known_issues": self.known_issues,
            # Metadata
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            # UI State Graph (Plan 10)
            "ui_states": self.ui_states,
            "ui_transitions": self.ui_transitions,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "AppGuide":
        """Deserialize guide from dictionary."""
        guide_type = GuideType(data.get("guide_type", "application"))
        
        # Deserialize activity records
        user_activities = [
            ActivityRecord.from_dict(a) for a in data.get("user_activities", [])
        ]
        agent_activities = [
            ActivityRecord.from_dict(a) for a in data.get("agent_activities", [])
        ]
        
        return cls(
            name=data["name"],
            guide_type=guide_type,
            short_description=data.get("short_description", ""),
            full_guide=data.get("full_guide", ""),
            domain=data.get("domain"),
            app_aliases=data.get("app_aliases", []),
            bundle_id=data.get("bundle_id"),
            # Absent in every guide file written before provenance was recorded.
            # It stays None: inferring it back from the name is the bug this
            # field exists to close.
            identity_provenance=data.get("identity_provenance"),
            use_cases=data.get("use_cases", []),
            ui_landmarks=data.get("ui_landmarks", []),
            common_shortcuts=data.get("common_shortcuts", {}),
            navigation_tips=data.get("navigation_tips", []),
            linked_workflows=data.get("linked_workflows", []),
            # Activity context
            user_activities=user_activities,
            agent_activities=agent_activities,
            common_tasks=data.get("common_tasks", []),
            successful_patterns=data.get("successful_patterns", []),
            known_issues=data.get("known_issues", []),
            # Metadata
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            version=data.get("version", 1),
            # UI State Graph (Plan 10)
            ui_states=data.get("ui_states", {}),
            ui_transitions=data.get("ui_transitions", []),
        )
    
    @classmethod
    def create_app_guide(
        cls,
        name: str,
        short_description: str = "",
        use_cases: Optional[List[str]] = None,
        bundle_id: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ) -> "AppGuide":
        """
        Create a new application guide.
        
        Args:
            name: Application name (e.g., "Google Chrome")
            short_description: Brief description
            use_cases: List of common use cases
            bundle_id: macOS bundle identifier
            aliases: Alternative names for the app
        
        Returns:
            New AppGuide instance
        """
        return cls(
            name=name,
            guide_type=GuideType.APPLICATION,
            short_description=short_description,
            use_cases=use_cases or [],
            bundle_id=bundle_id,
            app_aliases=aliases or [],
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
    
    @classmethod
    def create_website_guide(
        cls,
        name: str,
        domain: str,
        short_description: str = "",
        use_cases: Optional[List[str]] = None,
    ) -> "AppGuide":
        """
        Create a new website guide.
        
        Args:
            name: Website name (e.g., "GitHub")
            domain: Website domain (e.g., "github.com")
            short_description: Brief description
            use_cases: List of common use cases
        
        Returns:
            New AppGuide instance
        """
        return cls(
            name=name,
            guide_type=GuideType.WEBSITE,
            domain=domain,
            short_description=short_description,
            use_cases=use_cases or [],
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

