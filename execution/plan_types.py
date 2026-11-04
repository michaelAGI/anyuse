"""Dependency-free value objects shared by planning and replay.

Keep this module free of model SDKs and platform integrations: deterministic
replay needs the shapes, but must not import the model-backed planner to get
them.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PlannedStep:
    """A single planned execution step with modality and command details."""

    intent: str
    source: str
    target_element: str
    modality: str
    command: dict = field(default_factory=dict)
    input_data: Optional[str] = None
    needs_visual: bool = False
    expected_after: str = ""
    expected_after_state: Optional[str] = None
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "source": self.source,
            "target_element": self.target_element,
            "modality": self.modality,
            "command": self.command,
            "input_data": self.input_data,
            "needs_visual": self.needs_visual,
            "expected_after": self.expected_after,
            "expected_after_state": self.expected_after_state,
            "reasoning": self.reasoning,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlannedStep":
        return cls(
            intent=data.get("intent", ""),
            source=data.get("source", "workflow"),
            target_element=data.get("target_element", ""),
            modality=data.get("modality", "computer_use"),
            command=data.get("command", {}),
            input_data=data.get("input_data"),
            needs_visual=data.get("needs_visual", False),
            expected_after=data.get("expected_after", ""),
            expected_after_state=data.get("expected_after_state"),
            reasoning=data.get("reasoning", ""),
        )


@dataclass
class ExecutionPlan:
    """Complete execution plan and the calls spent producing it."""

    current_state: str = ""
    navigation_needed: List[str] = field(default_factory=list)
    steps: List[PlannedStep] = field(default_factory=list)
    skipped_steps: List[str] = field(default_factory=list)
    injected_steps: List[str] = field(default_factory=list)
    reasoning: str = ""
    llm_calls: int = 0

    def to_dict(self) -> dict:
        return {
            "current_state": self.current_state,
            "navigation_needed": self.navigation_needed,
            "steps": [s.to_dict() for s in self.steps],
            "skipped_steps": self.skipped_steps,
            "injected_steps": self.injected_steps,
            "reasoning": self.reasoning,
            "llm_calls": self.llm_calls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionPlan":
        return cls(
            current_state=data.get("current_state", ""),
            navigation_needed=data.get("navigation_needed", []),
            steps=[PlannedStep.from_dict(s) for s in data.get("steps", [])],
            skipped_steps=data.get("skipped_steps", []),
            injected_steps=data.get("injected_steps", []),
            reasoning=data.get("reasoning", ""),
            llm_calls=int(data.get("llm_calls", 0) or 0),
        )
