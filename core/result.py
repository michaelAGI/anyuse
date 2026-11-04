"""
Structured result types for agentic use.

These types are designed to be easily consumed by other AI agents
and provide all the information needed to understand task completion.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum
import copy
import json


@dataclass
class RestoreReport:
    """Observed cleanup of captured desktop controls, never an app snapshot."""

    status: str = "not_requested"
    fields: List[dict] = field(default_factory=list)

    @classmethod
    def from_fields(cls, fields: List[dict]) -> "RestoreReport":
        statuses = [row["status"] for row in fields]
        if statuses and all(value == "restored" for value in statuses):
            status = "restored"
        elif "restored" in statuses:
            status = "partial"
        elif "failed" in statuses:
            status = "failed"
        else:
            status = "unknown"
        return cls(status, fields)

    @classmethod
    def unknown(cls, reason: str) -> "RestoreReport":
        return cls("unknown", [{"field": "environment", "status": "unknown", "reason": reason}])

    @classmethod
    def from_result(cls, result) -> "RestoreReport":
        """Keep an execution receipt; absence cannot certify cleanup."""
        report = getattr(result, "restoration_report", None)
        if isinstance(report, cls):
            return copy.deepcopy(report)
        return cls.unknown("Execution did not provide a restoration report")

    @classmethod
    def from_interrupted_orchestrator(cls, orchestrator) -> "RestoreReport":
        """An exception wrapper must retain cleanup observed by run's finally."""
        report = getattr(orchestrator, "last_restoration_report", None)
        if isinstance(report, cls):
            return copy.deepcopy(report)
        return cls.unknown("Interrupted execution did not provide a restoration report")

    @classmethod
    def combine(cls, reports: List["RestoreReport"]) -> "RestoreReport":
        """Retain every sub-execution receipt when a plan summarizes results."""
        statuses = [report.status for report in reports if report.status != "not_requested"]
        if not statuses:
            status = "not_requested"
        elif all(value == "restored" for value in statuses):
            status = "restored"
        elif any(value in {"restored", "partial"} for value in statuses):
            status = "partial"
        elif "failed" in statuses:
            status = "failed"
        else:
            status = "unknown"
        return cls(status, [{"field": "execution_cleanup", "execution_index": index,
                             "status": report.status, "restoration": report.to_dict()}
                            for index, report in enumerate(reports)])

    def to_dict(self) -> dict:
        return {"format": "anyuse.replay-restoration/1", "status": self.status,
                "scope": "captured_desktop_controls_only", "fields": copy.deepcopy(self.fields)}


class ResultStatus(Enum):
    """Status of an agent execution."""
    SUCCESS = "success"
    PARTIAL = "partial"      # Some subtasks failed
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    # the outcome could not be decided (no structural signal; a verifier that was
    # not confident, or failed): the run STOPS with ``outcome_reason`` and is never
    # reported as a success
    UNCERTAIN = "uncertain"


@dataclass
class ActionResult:
    """Result of a single action execution."""
    action: str
    args: Dict[str, Any]
    success: bool
    error: Optional[str] = None
    duration_ms: int = 0
    screenshot_hash: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "args": {k: v for k, v in self.args.items() 
                    if "password" not in k.lower() and "credential" not in k.lower()},
            "success": self.success,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class SubtaskResult:
    """Result of a planned subtask."""
    id: str
    description: str
    status: str
    actions: List[ActionResult] = field(default_factory=list)
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "actions_count": len(self.actions),
            "error": self.error,
        }


@dataclass
class AgentResult:
    """
    Complete result from agent execution.
    
    Designed for machine consumption by other agents.
    All fields are structured for easy parsing and decision making.
    
    Usage:
        result = agent.run("Open Safari")
        
        if result.success:
            print(f"Task completed: {result.message}")
        else:
            print(f"Failed: {result.error}")
        
        # For agentic use
        return result.to_dict()
    """
    # Core result
    status: ResultStatus
    success: bool
    message: str
    error: Optional[str] = None
    
    # Task info
    task: str = ""
    plan_summary: str = ""
    
    # Execution details
    actions: List[ActionResult] = field(default_factory=list)
    subtasks: List[SubtaskResult] = field(default_factory=list)
    turns_used: int = 0
    max_turns: int = 20
    
    # Final state context
    final_screenshot_b64: Optional[str] = None
    final_app: str = ""
    final_url: str = ""
    
    # Metrics
    duration_seconds: float = 0.0
    api_calls: int = 0
    estimated_cost_usd: float = 0.0
    tokens_used: int = 0
    #: the model calls of this run: the delta of the process counter in
    #: ``execution.model_adapter`` (the goal verifier's call included)
    llm_calls: int = 0
    #: why the outcome is what it is (a structural signal, a verifier verdict,
    #: or why it could not be decided)
    outcome_reason: str = ""

    # Workflow
    workflow_recorded: bool = False
    workflow_name: Optional[str] = None
    workflow_matched: Optional[str] = None  # If a workflow was used
    restoration_report: RestoreReport = field(default_factory=RestoreReport)
    rea_executions: List[Dict[str, Any]] = field(default_factory=list)
    # Advisory recommendation only; it never authorizes native discovery.
    rea_escalation: Optional[Dict[str, Any]] = None

    @property
    def completion_status(self) -> str:
        """Task success alone does not certify requested desktop cleanup."""
        return ("complete" if self.success and self.restoration_report.status in
                {"restored", "not_requested"} else "incomplete")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for JSON serialization.
        
        This is the primary format for agentic consumption.
        """
        return {
            "status": self.status.value,
            "success": self.success,
            "message": self.message,
            "error": self.error,
            "task": self.task,
            "plan_summary": self.plan_summary,
            "actions_count": len(self.actions),
            "actions": [a.to_dict() for a in self.actions[:10]],  # Limit for size
            "subtasks": [s.to_dict() for s in self.subtasks],
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "final_app": self.final_app,
            "final_url": self.final_url,
            "duration_seconds": round(self.duration_seconds, 2),
            "api_calls": self.api_calls,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "workflow_used": self.workflow_matched,
            "llm_calls": self.llm_calls,
            "outcome_reason": self.outcome_reason,
            "restoration": self.restoration_report.to_dict(),
            "cleanup_status": self.restoration_report.status,
            "completion_status": self.completion_status,
            **({"rea_executions": copy.deepcopy(self.rea_executions)} if self.rea_executions else {}),
            **({"rea_escalation": copy.deepcopy(self.rea_escalation)} if self.rea_escalation is not None else {}),
        }
    
    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)
    
    def __bool__(self) -> bool:
        """Allow `if result:` usage."""
        return self.success
    
    def __repr__(self) -> str:
        return f"AgentResult(success={self.success}, message='{self.message[:50]}...')"
    
    @classmethod
    def success_result(cls, message: str, **kwargs) -> "AgentResult":
        """Create a successful result."""
        return cls(
            status=ResultStatus.SUCCESS,
            success=True,
            message=message,
            **kwargs
        )
    
    @classmethod
    def failure_result(cls, error: str, **kwargs) -> "AgentResult":
        """Create a failed result."""
        return cls(
            status=ResultStatus.FAILED,
            success=False,
            message=f"Task failed: {error}",
            error=error,
            **kwargs
        )
    
    @classmethod
    def partial_result(cls, message: str, completed: int, total: int, **kwargs) -> "AgentResult":
        """Create a partial success result."""
        return cls(
            status=ResultStatus.PARTIAL,
            success=False,
            message=f"{message} ({completed}/{total} subtasks completed)",
            **kwargs
        )


# Type alias for convenience
Result = AgentResult

