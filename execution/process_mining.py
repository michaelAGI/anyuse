"""
Process Mining -- Discover, analyze, and optimize workflows from execution data.

Three layers:
1. Discovery: Find patterns across execution traces and observations
2. Conformance: Compare actual execution vs expected workflow template
3. Enhancement: Propose improvements iteratively using AI

This module analyzes ALL data (execution traces, sessions, observations)
to refine the state graph and propose workflow improvements.
"""

import asyncio
import json
import logging
import os
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from anyuse.execution.model_adapter import ModelAdapter
from anyuse.guides.state_graph import StateGraph, UITransition
from anyuse.memory.models import WorkflowTemplate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TraceStep:
    """One step in a trace."""

    intent: str
    from_state: str
    to_state: str
    modality_used: str
    success: bool
    duration_ms: float
    intervention_needed: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "modality_used": self.modality_used,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "intervention_needed": self.intervention_needed,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TraceStep":
        return cls(
            intent=data.get("intent", ""),
            from_state=data.get("from_state", ""),
            to_state=data.get("to_state", ""),
            modality_used=data.get("modality_used", ""),
            success=data.get("success", False),
            duration_ms=data.get("duration_ms", 0.0),
            intervention_needed=data.get("intervention_needed", False),
            error=data.get("error"),
        )


@dataclass
class ExecutionTrace:
    """One execution's trace data, normalized for analysis."""

    trace_id: str
    workflow_name: str
    app: str
    timestamp: str
    success: bool
    duration_seconds: float
    steps: List[TraceStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "workflow_name": self.workflow_name,
            "app": self.app,
            "timestamp": self.timestamp,
            "success": self.success,
            "duration_seconds": self.duration_seconds,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionTrace":
        return cls(
            trace_id=data.get("trace_id", ""),
            workflow_name=data.get("workflow_name", ""),
            app=data.get("app", ""),
            timestamp=data.get("timestamp", ""),
            success=data.get("success", False),
            duration_seconds=data.get("duration_seconds", 0.0),
            steps=[TraceStep.from_dict(s) for s in data.get("steps", [])],
        )

    @classmethod
    def from_execution_result(
        cls, result, workflow: WorkflowTemplate
    ) -> "ExecutionTrace":
        """
        Create an ExecutionTrace from an ExecutionResult and its workflow.

        Args:
            result: An ExecutionResult from the state graph executor.
            workflow: The WorkflowTemplate that was executed.

        Returns:
            A normalized ExecutionTrace.
        """
        trace_steps: List[TraceStep] = []

        for idx, step_result in enumerate(result.steps):
            # Determine intent from the workflow template if available
            intent = ""
            from_state = ""
            to_state = ""
            if idx < len(workflow.steps):
                wf_step = workflow.steps[idx]
                intent = wf_step.intent
                from_state = wf_step.expected_before_state or ""
                to_state = wf_step.expected_after_state or ""

            # Check if intervention was involved
            intervention_needed = (
                step_result.reasoning
                and "intervention" in step_result.reasoning.lower()
            ) if step_result.reasoning else False

            trace_steps.append(
                TraceStep(
                    intent=intent or f"step_{idx + 1}",
                    from_state=from_state,
                    to_state=step_result.new_state or to_state,
                    modality_used=step_result.method or "",
                    success=step_result.success,
                    duration_ms=0.0,  # Not tracked per-step in ExecutionResult
                    intervention_needed=intervention_needed,
                    error=step_result.error,
                )
            )

        return cls(
            trace_id=uuid.uuid4().hex[:12],
            workflow_name=workflow.name,
            app=workflow.required_app,
            timestamp=datetime.now().isoformat(),
            success=result.success,
            duration_seconds=getattr(result, "duration_seconds", 0.0),
            steps=trace_steps,
        )

    @classmethod
    def from_session_events(
        cls, events: List[dict], app_name: str
    ) -> "ExecutionTrace":
        """
        Create an ExecutionTrace from BatchWatcher session events.

        Args:
            events: List of event dicts from a BatchWatcher session.
            app_name: The application name.

        Returns:
            A normalized ExecutionTrace.
        """
        trace_steps: List[TraceStep] = []

        for event in events:
            intent = event.get("description", event.get("action", ""))
            trace_steps.append(
                TraceStep(
                    intent=intent,
                    from_state=event.get("from_state", ""),
                    to_state=event.get("to_state", ""),
                    modality_used=event.get("modality", "observed"),
                    success=event.get("success", True),
                    duration_ms=event.get("duration_ms", 0.0),
                    intervention_needed=False,
                    error=event.get("error"),
                )
            )

        # Derive overall success: all steps succeeded
        overall_success = all(s.success for s in trace_steps) if trace_steps else True

        # Derive timestamp from events or use now
        timestamp = ""
        if events:
            timestamp = events[0].get("timestamp", "")
        if not timestamp:
            timestamp = datetime.now().isoformat()

        # Compute total duration from individual steps
        total_duration_ms = sum(s.duration_ms for s in trace_steps)

        return cls(
            trace_id=uuid.uuid4().hex[:12],
            workflow_name="",  # Session events are not tied to a workflow name
            app=app_name,
            timestamp=timestamp,
            success=overall_success,
            duration_seconds=total_duration_ms / 1000.0,
            steps=trace_steps,
        )


@dataclass
class ConformanceReport:
    """Comparison of actual executions vs expected workflow."""

    workflow_name: str
    total_executions: int

    # Step-level conformance
    step_skip_rates: Dict[str, float] = field(default_factory=dict)
    step_failure_rates: Dict[str, float] = field(default_factory=dict)
    step_intervention_rates: Dict[str, float] = field(default_factory=dict)

    # Path conformance
    expected_path: List[str] = field(default_factory=list)
    actual_paths: List[Tuple[List[str], int]] = field(default_factory=list)
    deviation_rate: float = 0.0

    # AI-injected steps
    commonly_injected: List[Tuple[str, int]] = field(default_factory=list)
    commonly_skipped: List[Tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "workflow_name": self.workflow_name,
            "total_executions": self.total_executions,
            "step_skip_rates": self.step_skip_rates,
            "step_failure_rates": self.step_failure_rates,
            "step_intervention_rates": self.step_intervention_rates,
            "expected_path": self.expected_path,
            "actual_paths": [
                {"path": path, "count": count}
                for path, count in self.actual_paths
            ],
            "deviation_rate": self.deviation_rate,
            "commonly_injected": [
                {"intent": intent, "count": count}
                for intent, count in self.commonly_injected
            ],
            "commonly_skipped": [
                {"intent": intent, "count": count}
                for intent, count in self.commonly_skipped
            ],
        }

    def summary(self) -> str:
        """Human-readable summary of the conformance report."""
        parts = [
            f"Conformance report for '{self.workflow_name}' "
            f"({self.total_executions} executions):"
        ]

        if self.deviation_rate > 0:
            parts.append(
                f"  Path deviation rate: {self.deviation_rate:.1%}"
            )

        # Report steps with notable failure rates
        high_failure = [
            (step, rate)
            for step, rate in self.step_failure_rates.items()
            if rate > 0
        ]
        if high_failure:
            parts.append("  Steps with failures:")
            for step, rate in sorted(high_failure, key=lambda x: -x[1]):
                parts.append(f"    - '{step}': {rate:.0%} failure rate")

        # Report commonly injected steps
        if self.commonly_injected:
            parts.append("  Commonly injected steps:")
            for intent, count in self.commonly_injected[:5]:
                parts.append(f"    - '{intent}' ({count} times)")

        # Report commonly skipped steps
        if self.commonly_skipped:
            parts.append("  Commonly skipped steps:")
            for intent, count in self.commonly_skipped[:5]:
                parts.append(f"    - '{intent}' ({count} times)")

        return "\n".join(parts)


@dataclass
class MiningInsight:
    """A discovered insight from process mining."""

    insight_type: str
    # "redundant_step", "missing_step", "bottleneck",
    # "path_optimization", "parameter_pattern",
    # "degradation_warning", "workflow_discovery"

    severity: str
    # "info", "suggestion", "warning"

    description: str
    evidence: dict = field(default_factory=dict)
    suggested_action: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "insight_type": self.insight_type,
            "severity": self.severity,
            "description": self.description,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
        }


# ---------------------------------------------------------------------------
# ProcessMiner
# ---------------------------------------------------------------------------


class ProcessMiner:
    """Analyzes execution data to discover patterns and propose improvements."""

    def __init__(
        self,
        workflow_storage=None,
        guide_storage=None,
        api_key: Optional[str] = None,
        model: str = "",
        adapter: Optional[ModelAdapter] = None,
    ):
        """
        Initialize the ProcessMiner.

        Args:
            workflow_storage: WorkflowStorage for loading traces and workflows.
            guide_storage: GuideStorage for accessing app guides.
            api_key: Gemini API key. Defaults to config.GEMINI_API_KEY.
            model: Model name for AI analysis. Defaults to Flash model.
        """
        self._workflow_storage = workflow_storage
        self._guide_storage = guide_storage
        self._adapter = adapter or ModelAdapter.for_role(
            "planner", **({"model": model} if model else {})
        )
        self._model = self._adapter.config.model

    # ------------------------------------------------------------------
    # Trace collection
    # ------------------------------------------------------------------

    def collect_traces(
        self,
        workflow_name: Optional[str] = None,
        app: Optional[str] = None,
        limit: int = 100,
    ) -> List[ExecutionTrace]:
        """
        Collect execution traces from storage.

        Sources:
        - Execution results (from workflow template runs)
        - Session events (from BatchWatcher observations)
        - Workflow replay logs (from legacy player)

        Args:
            workflow_name: Filter by workflow name (optional).
            app: Filter by application name (optional).
            limit: Maximum number of traces to return.

        Returns:
            List of ExecutionTrace, most recent first.
        """
        if self._workflow_storage is None:
            return []

        try:
            traces = self._workflow_storage.get_traces(
                workflow_name=workflow_name, limit=limit
            )
        except (AttributeError, Exception) as exc:
            logger.debug("Could not load traces from storage: %s", exc)
            traces = []

        # Filter by app if requested
        if app:
            app_lower = app.lower()
            traces = [t for t in traces if t.app.lower() == app_lower]

        # Sort by timestamp descending (most recent first)
        traces.sort(key=lambda t: t.timestamp, reverse=True)

        return traces[:limit]

    # ------------------------------------------------------------------
    # Conformance analysis
    # ------------------------------------------------------------------

    def analyze_conformance(
        self,
        workflow: WorkflowTemplate,
        traces: List[ExecutionTrace],
    ) -> ConformanceReport:
        """
        Compare actual executions against the expected workflow.

        Computes:
        - Per-step success/skip/intervention rates
        - Path deviations (actual vs expected state sequence)
        - Commonly injected/skipped steps

        Args:
            workflow: The expected workflow template.
            traces: Execution traces to analyze.

        Returns:
            A ConformanceReport with all conformance metrics.
        """
        if not traces:
            return ConformanceReport(
                workflow_name=workflow.name,
                total_executions=0,
                expected_path=workflow.state_path,
            )

        total = len(traces)

        # Expected intents from the workflow
        expected_intents = [step.intent for step in workflow.steps]
        expected_path = workflow.state_path

        # Per-step counters
        step_total: Dict[str, int] = defaultdict(int)
        step_failures: Dict[str, int] = defaultdict(int)
        step_interventions: Dict[str, int] = defaultdict(int)

        # Track which expected steps appear / are skipped
        step_appearances: Dict[str, int] = defaultdict(int)

        # Path tracking
        path_counter: Counter = Counter()
        deviations = 0

        # Track intents that appear in traces but not in workflow
        injected_counter: Counter = Counter()
        skipped_counter: Counter = Counter()

        for trace in traces:
            trace_intents = [s.intent for s in trace.steps]

            # Build the actual state path from this trace
            actual_path = []
            for step in trace.steps:
                if step.from_state and (
                    not actual_path or actual_path[-1] != step.from_state
                ):
                    actual_path.append(step.from_state)
                if step.to_state and (
                    not actual_path or actual_path[-1] != step.to_state
                ):
                    actual_path.append(step.to_state)

            path_key = tuple(actual_path)
            path_counter[path_key] += 1

            # Check if path deviates from expected
            if expected_path and list(path_key) != expected_path:
                deviations += 1

            # Per-step analysis
            for step in trace.steps:
                step_total[step.intent] += 1
                if not step.success:
                    step_failures[step.intent] += 1
                if step.intervention_needed:
                    step_interventions[step.intent] += 1

            # Track skipped and injected steps
            for expected_intent in expected_intents:
                if expected_intent in trace_intents:
                    step_appearances[expected_intent] += 1
                else:
                    skipped_counter[expected_intent] += 1

            for trace_intent in trace_intents:
                if trace_intent not in expected_intents:
                    injected_counter[trace_intent] += 1

        # Compute rates
        step_skip_rates: Dict[str, float] = {}
        step_failure_rates: Dict[str, float] = {}
        step_intervention_rates: Dict[str, float] = {}

        for intent in set(list(step_total.keys()) + expected_intents):
            appearances = step_total.get(intent, 0)
            if intent in expected_intents:
                skip_count = total - step_appearances.get(intent, 0)
                step_skip_rates[intent] = skip_count / total if total > 0 else 0.0
            if appearances > 0:
                step_failure_rates[intent] = (
                    step_failures.get(intent, 0) / appearances
                )
                step_intervention_rates[intent] = (
                    step_interventions.get(intent, 0) / appearances
                )

        # Build actual_paths list (sorted by frequency)
        actual_paths: List[Tuple[List[str], int]] = [
            (list(path), count)
            for path, count in path_counter.most_common()
        ]

        deviation_rate = deviations / total if total > 0 else 0.0

        return ConformanceReport(
            workflow_name=workflow.name,
            total_executions=total,
            step_skip_rates=step_skip_rates,
            step_failure_rates=step_failure_rates,
            step_intervention_rates=step_intervention_rates,
            expected_path=expected_path,
            actual_paths=actual_paths,
            deviation_rate=deviation_rate,
            commonly_injected=injected_counter.most_common(),
            commonly_skipped=skipped_counter.most_common(),
        )

    # ------------------------------------------------------------------
    # Rule-based pattern discovery
    # ------------------------------------------------------------------

    def discover_patterns(
        self,
        traces: List[ExecutionTrace],
        state_graph: StateGraph,
    ) -> List[MiningInsight]:
        """
        Discover patterns from execution data WITHOUT AI.

        Rule-based pattern detection:
        - Redundant steps: step always skipped -> suggest removing
        - Missing steps: AI always injects same step -> suggest adding permanently
        - Bottlenecks: step with high failure rate or high intervention rate
        - Path optimization: shorter path exists in graph but workflow uses longer one
        - Degradation warning: success rate dropping over recent executions
        - Workflow discovery: repeated sequence across sessions not yet a workflow

        Args:
            traces: Execution traces to analyze.
            state_graph: The application's state graph.

        Returns:
            List of MiningInsight with evidence and suggested actions.
        """
        if not traces:
            return []

        insights: List[MiningInsight] = []

        # --- Per-step aggregation ---
        step_total: Dict[str, int] = defaultdict(int)
        step_failures: Dict[str, int] = defaultdict(int)
        step_interventions: Dict[str, int] = defaultdict(int)
        step_durations: Dict[str, List[float]] = defaultdict(list)

        for trace in traces:
            for step in trace.steps:
                step_total[step.intent] += 1
                if not step.success:
                    step_failures[step.intent] += 1
                if step.intervention_needed:
                    step_interventions[step.intent] += 1
                if step.duration_ms > 0:
                    step_durations[step.intent].append(step.duration_ms)

        total_traces = len(traces)

        # --- 1. Redundant steps: step exists in workflow but never executed
        # (always skipped across all traces) ---
        # We detect steps that appear in very few traces relative to total
        # Return the data; caller/AI decides if it's truly redundant.
        for intent, count in step_total.items():
            if count < total_traces and total_traces > 2:
                appearance_rate = count / total_traces
                # Only flag if it appears in less than half of executions
                if appearance_rate < 0.5:
                    insights.append(
                        MiningInsight(
                            insight_type="redundant_step",
                            severity="suggestion",
                            description=(
                                f"Step '{intent}' only appears in "
                                f"{appearance_rate:.0%} of executions "
                                f"({count}/{total_traces})."
                            ),
                            evidence={
                                "intent": intent,
                                "appearance_count": count,
                                "total_traces": total_traces,
                                "appearance_rate": appearance_rate,
                            },
                            suggested_action=(
                                f"Consider removing '{intent}' from the "
                                f"workflow if it is consistently unnecessary."
                            ),
                        )
                    )

        # --- 2. Missing steps: same intent injected across multiple traces ---
        injected_intents: Counter = Counter()
        for trace in traces:
            seen_in_trace: set = set()
            for step in trace.steps:
                # An injected step is one with a modality or intent not in
                # the workflow.  We detect repeating intents across traces.
                if step.intent not in seen_in_trace:
                    seen_in_trace.add(step.intent)
            # Track how many traces each intent appears in
            for intent in seen_in_trace:
                injected_intents[intent] += 1

        # Find intents that appear in many traces — potential missing steps
        for intent, count in injected_intents.most_common():
            if count >= total_traces and total_traces > 1:
                # This intent appears in every single trace — very likely needed
                insights.append(
                    MiningInsight(
                        insight_type="missing_step",
                        severity="suggestion",
                        description=(
                            f"Step '{intent}' appears in all {count} "
                            f"executions. Consider adding it permanently."
                        ),
                        evidence={
                            "intent": intent,
                            "occurrence_count": count,
                            "total_traces": total_traces,
                        },
                        suggested_action=(
                            f"Add '{intent}' as a permanent step in the "
                            f"workflow template."
                        ),
                    )
                )

        # --- 3. Bottlenecks: high failure or intervention rate ---
        for intent in step_total:
            count = step_total[intent]
            if count == 0:
                continue

            failure_rate = step_failures.get(intent, 0) / count
            intervention_rate = step_interventions.get(intent, 0) / count

            if failure_rate > 0 or intervention_rate > 0:
                avg_duration = 0.0
                if step_durations.get(intent):
                    avg_duration = sum(step_durations[intent]) / len(
                        step_durations[intent]
                    )

                insights.append(
                    MiningInsight(
                        insight_type="bottleneck",
                        severity="warning" if failure_rate > 0.5 else "info",
                        description=(
                            f"Step '{intent}' has "
                            f"{failure_rate:.0%} failure rate and "
                            f"{intervention_rate:.0%} intervention rate "
                            f"across {count} executions."
                        ),
                        evidence={
                            "intent": intent,
                            "total_executions": count,
                            "failure_rate": failure_rate,
                            "intervention_rate": intervention_rate,
                            "failures": step_failures.get(intent, 0),
                            "interventions": step_interventions.get(intent, 0),
                            "avg_duration_ms": avg_duration,
                        },
                        suggested_action=(
                            f"Investigate why '{intent}' fails frequently. "
                            f"Consider alternative modalities or element "
                            f"identification strategies."
                        ),
                    )
                )

        # --- 4. Path optimization: check if shorter path exists ---
        insights.extend(
            self._check_path_optimizations(traces, state_graph)
        )

        # --- 5. Degradation warning ---
        degradation = self._detect_degradation_from_traces(traces)
        if degradation:
            insights.append(degradation)

        # --- 6. Workflow discovery: repeated sequences across sessions ---
        insights.extend(
            self._discover_repeated_sequences(traces)
        )

        return insights

    def _check_path_optimizations(
        self,
        traces: List[ExecutionTrace],
        state_graph: StateGraph,
    ) -> List[MiningInsight]:
        """Check if shorter paths exist in the graph for commonly taken routes."""
        insights: List[MiningInsight] = []

        # Collect commonly used state paths
        path_counter: Counter = Counter()
        for trace in traces:
            states_visited: List[str] = []
            for step in trace.steps:
                if step.from_state and (
                    not states_visited or states_visited[-1] != step.from_state
                ):
                    states_visited.append(step.from_state)
                if step.to_state and (
                    not states_visited or states_visited[-1] != step.to_state
                ):
                    states_visited.append(step.to_state)
            if len(states_visited) >= 2:
                # Record start -> end path
                path_key = (states_visited[0], states_visited[-1])
                path_counter[path_key] += 1

        # For each commonly used path, check if a shorter one exists in the graph
        for (start, end), count in path_counter.most_common(10):
            if count < 2:
                continue

            shortest = state_graph.find_path(start, end)
            if not shortest:
                continue

            # Compare shortest graph path length with average trace path length
            trace_lengths: List[int] = []
            for trace in traces:
                states_visited = []
                for step in trace.steps:
                    if step.from_state and (
                        not states_visited
                        or states_visited[-1] != step.from_state
                    ):
                        states_visited.append(step.from_state)
                    if step.to_state and (
                        not states_visited
                        or states_visited[-1] != step.to_state
                    ):
                        states_visited.append(step.to_state)
                if (
                    states_visited
                    and states_visited[0] == start
                    and states_visited[-1] == end
                ):
                    trace_lengths.append(len(states_visited))

            if not trace_lengths:
                continue

            avg_trace_length = sum(trace_lengths) / len(trace_lengths)
            graph_length = len(shortest) + 1  # transitions + 1 = states

            if graph_length < avg_trace_length:
                insights.append(
                    MiningInsight(
                        insight_type="path_optimization",
                        severity="suggestion",
                        description=(
                            f"Route from '{start}' to '{end}' uses "
                            f"{avg_trace_length:.1f} states on average, "
                            f"but a {graph_length}-state path exists in "
                            f"the graph."
                        ),
                        evidence={
                            "from_state": start,
                            "to_state": end,
                            "avg_actual_length": avg_trace_length,
                            "shortest_length": graph_length,
                            "shortest_path_intents": [
                                t.intent for t in shortest
                            ],
                            "occurrences": count,
                        },
                        suggested_action=(
                            f"Consider restructuring the workflow to use "
                            f"the shorter path: "
                            + " -> ".join(t.intent for t in shortest)
                        ),
                    )
                )

        return insights

    def _detect_degradation_from_traces(
        self, traces: List[ExecutionTrace]
    ) -> Optional[MiningInsight]:
        """
        Check if success rate is declining across recent traces.

        Compares the most recent traces against the overall average.
        """
        if len(traces) < 4:
            return None

        # Traces are sorted most recent first
        window = min(len(traces) // 2, 10)
        recent = traces[:window]
        overall = traces

        recent_success = sum(1 for t in recent if t.success) / len(recent)
        overall_success = sum(1 for t in overall if t.success) / len(overall)

        # Return the trend data for caller/AI to evaluate
        if recent_success < overall_success:
            return MiningInsight(
                insight_type="degradation_warning",
                severity="warning",
                description=(
                    f"Recent success rate ({recent_success:.0%} over "
                    f"last {window} executions) is lower than overall "
                    f"({overall_success:.0%} over {len(overall)} executions)."
                ),
                evidence={
                    "recent_success_rate": recent_success,
                    "overall_success_rate": overall_success,
                    "recent_window": window,
                    "total_executions": len(overall),
                    "recent_failures": sum(
                        1 for t in recent if not t.success
                    ),
                },
                suggested_action=(
                    "Investigate recent failures to determine if the "
                    "application UI has changed or if there are new "
                    "edge cases."
                ),
            )

        return None

    def _discover_repeated_sequences(
        self, traces: List[ExecutionTrace]
    ) -> List[MiningInsight]:
        """
        Find repeated intent sequences across traces that might be new workflows.

        Looks for sequences of 3+ intents that appear identically in
        multiple traces (from different workflow_names or empty workflow_name).
        """
        insights: List[MiningInsight] = []

        # Extract intent sequences from traces without a workflow name
        # (these are observations, not template executions)
        sequences: List[Tuple[str, ...]] = []
        for trace in traces:
            if len(trace.steps) >= 3:
                seq = tuple(s.intent for s in trace.steps if s.success)
                if len(seq) >= 3:
                    sequences.append(seq)

        if len(sequences) < 2:
            return insights

        # Find common subsequences of length 3+
        subseq_counter: Counter = Counter()
        for seq in sequences:
            # Extract all contiguous subsequences of length 3+
            for length in range(3, min(len(seq) + 1, 8)):
                for start in range(len(seq) - length + 1):
                    subseq = seq[start : start + length]
                    subseq_counter[subseq] += 1

        # Report subsequences that appear in 2+ traces
        reported: set = set()
        for subseq, count in subseq_counter.most_common(10):
            if count < 2:
                break

            # Skip if this is a subsequence of an already-reported longer sequence
            subseq_str = " -> ".join(subseq)
            is_subset = False
            for existing in reported:
                if subseq_str in existing:
                    is_subset = True
                    break
            if is_subset:
                continue

            reported.add(subseq_str)
            insights.append(
                MiningInsight(
                    insight_type="workflow_discovery",
                    severity="info",
                    description=(
                        f"Repeated sequence found {count} times: "
                        + subseq_str
                    ),
                    evidence={
                        "sequence": list(subseq),
                        "occurrences": count,
                        "length": len(subseq),
                    },
                    suggested_action=(
                        f"Consider creating a workflow template for this "
                        f"repeated sequence of {len(subseq)} steps."
                    ),
                )
            )

        return insights

    # ------------------------------------------------------------------
    # AI-powered analysis
    # ------------------------------------------------------------------

    async def deep_analysis(
        self,
        traces: List[ExecutionTrace],
        state_graph: StateGraph,
        workflow: Optional[WorkflowTemplate] = None,
    ) -> List[MiningInsight]:
        """
        AI-powered deep analysis of execution data.

        Feeds execution history to Gemini Flash and asks:
        - What patterns do you see across these executions?
        - What steps could be combined or eliminated?
        - Are there failure patterns that suggest the workflow needs restructuring?
        - What parameters are users filling in -- can we set better defaults?

        Returns AI-generated insights alongside rule-based ones.

        Args:
            traces: Execution traces to analyze.
            state_graph: The application's state graph.
            workflow: The workflow template (optional, for context).

        Returns:
            List of MiningInsight from AI analysis.
        """
        if not traces:
            return []

        # Build trace summary for AI (limit to keep prompt manageable)
        trace_summaries = []
        for trace in traces[:20]:
            steps_summary = []
            for step in trace.steps:
                s = {
                    "intent": step.intent,
                    "success": step.success,
                    "modality": step.modality_used,
                    "intervention": step.intervention_needed,
                }
                if step.error:
                    s["error"] = step.error[:100]
                steps_summary.append(s)
            trace_summaries.append({
                "success": trace.success,
                "duration_s": trace.duration_seconds,
                "steps": steps_summary,
            })

        workflow_info = ""
        if workflow:
            workflow_info = (
                f"\nWORKFLOW TEMPLATE: {workflow.name}\n"
                f"Steps: {', '.join(s.intent for s in workflow.steps)}\n"
                f"Success rate: {workflow.success_rate:.0%} "
                f"({workflow.run_count} runs)\n"
            )

        # State graph summary
        graph_states = len(state_graph.states) if state_graph else 0
        graph_transitions = len(state_graph.transitions) if state_graph else 0

        prompt = f"""Analyze these execution traces for a desktop automation workflow and identify patterns, issues, and improvement opportunities.

{workflow_info}

STATE GRAPH: {graph_states} states, {graph_transitions} transitions

EXECUTION TRACES ({len(traces)} total, showing up to 20):
{json.dumps(trace_summaries, indent=2)}

Analyze the data and return a JSON array of insights:
[
    {{
        "insight_type": "redundant_step" | "missing_step" | "bottleneck" | "path_optimization" | "parameter_pattern" | "degradation_warning" | "workflow_discovery",
        "severity": "info" | "suggestion" | "warning",
        "description": "human-readable description of the insight",
        "evidence": {{}},
        "suggested_action": "what to do about it"
    }}
]

Focus on:
1. Steps that consistently fail or need intervention
2. Steps that could be combined or reordered
3. Patterns in what parameters users provide
4. Failure patterns suggesting UI changes
5. Steps that are always skipped (redundant)
6. Steps that AI always injects (missing from template)

Return ONLY the JSON array, no other text."""

        try:
            response = await self._adapter.acomplete(
                [{"role": "user", "content": prompt}], temperature=0.2,
                json_schema={"type": "array", "items": {"type": "object"}},
                schema_name="mining_insights",
            )
            if not response.ok:
                raise RuntimeError(response.error or "mining provider failed")
            response_text = response.text or "[]"
            parsed = json.loads(response_text)

            insights: List[MiningInsight] = []
            if isinstance(parsed, list):
                for item in parsed:
                    insights.append(
                        MiningInsight(
                            insight_type=item.get("insight_type", "info"),
                            severity=item.get("severity", "info"),
                            description=item.get("description", ""),
                            evidence=item.get("evidence", {}),
                            suggested_action=item.get("suggested_action"),
                        )
                    )
            return insights

        except Exception as exc:
            logger.error("Deep analysis AI call failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Workflow improvement proposals
    # ------------------------------------------------------------------

    async def propose_improvements(
        self,
        workflow: WorkflowTemplate,
        conformance: ConformanceReport,
        insights: List[MiningInsight],
        state_graph: StateGraph,
    ) -> WorkflowTemplate:
        """
        Generate an improved workflow template based on mining results.

        Uses AI to propose a revised workflow:
        - Remove steps that are always skipped
        - Add steps that AI always injects
        - Reorder for efficiency
        - Update modality preferences based on success data
        - Simplify parameterization based on patterns

        Returns a NEW template (doesn't modify the original).
        The user/system decides whether to adopt it.

        Args:
            workflow: The current workflow template.
            conformance: Conformance report for the workflow.
            insights: Discovered insights.
            state_graph: The application's state graph.

        Returns:
            A new WorkflowTemplate with proposed improvements.
        """
        # Build context for AI
        current_steps = []
        for step in workflow.steps:
            current_steps.append({
                "intent": step.intent,
                "target_element": step.target_element,
                "input_type": step.input_type,
                "optional": step.optional,
                "success_rate": (
                    step.step_successes / step.step_attempts
                    if step.step_attempts > 0
                    else 0.0
                ),
                "last_modality": step.last_success_modality,
            })

        insights_summary = [i.to_dict() for i in insights[:15]]
        conformance_summary = conformance.to_dict()

        prompt = f"""Given a workflow template and its analysis, propose an improved version.

CURRENT WORKFLOW: {workflow.name}
Description: {workflow.description}
Steps:
{json.dumps(current_steps, indent=2)}

CONFORMANCE REPORT:
{json.dumps(conformance_summary, indent=2)}

INSIGHTS:
{json.dumps(insights_summary, indent=2)}

Propose an improved workflow. Return JSON:
{{
    "description": "updated description",
    "steps": [
        {{
            "intent": "what to do",
            "target_element": "element identifier",
            "input_type": "click" | "type" | "select" | "toggle",
            "optional": false,
            "input_data": null,
            "ai_reasoning": "why this step is included/changed"
        }}
    ],
    "changes_made": ["list of changes and why"]
}}

Rules:
- Remove steps that are always skipped (skip rate > 80%)
- Add steps that AI always injects
- Reorder for efficiency if a shorter path exists
- Keep the workflow functional -- don't remove critical steps
- Explain each change in changes_made
- Return ONLY the JSON, no other text."""

        try:
            response = await self._adapter.acomplete(
                [{"role": "user", "content": prompt}], temperature=0.2,
                json_schema={
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "object"}},
                        "changes_made": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["description", "steps", "changes_made"],
                },
                schema_name="workflow_improvement",
            )
            if not response.ok:
                raise RuntimeError(response.error or "improvement provider failed")
            response_text = response.text
            parsed = json.loads(response_text)

            return self._build_improved_template(workflow, parsed)

        except Exception as exc:
            logger.error("Propose improvements AI call failed: %s", exc)
            return self._rule_based_improvements(
                workflow, conformance, insights
            )

    def _rule_based_improvements(
        self,
        workflow: WorkflowTemplate,
        conformance: ConformanceReport,
        insights: List[MiningInsight],
    ) -> WorkflowTemplate:
        """
        Apply rule-based improvements without AI.

        A fallback when AI is not available. Applies conservative
        changes based on the data.
        """
        from anyuse.memory.models import WorkflowIntent

        # Start with a copy of the current steps
        new_steps: List[WorkflowIntent] = []
        removed: List[str] = []

        for step in workflow.steps:
            skip_rate = conformance.step_skip_rates.get(step.intent, 0.0)
            # Mark high-skip-rate steps as optional rather than removing them
            new_step = WorkflowIntent(
                intent=step.intent,
                target_element=step.target_element,
                expected_before_state=step.expected_before_state,
                expected_after_state=step.expected_after_state,
                expected_after=step.expected_after,
                input_data=step.input_data,
                input_type=step.input_type,
                optional=step.optional or (skip_rate > 0.8),
                ai_reasoning=step.ai_reasoning,
                step_attempts=step.step_attempts,
                step_successes=step.step_successes,
                last_success_modality=step.last_success_modality,
            )

            if skip_rate > 0.8 and not step.optional:
                new_step.ai_reasoning = (
                    f"Marked optional: skipped in {skip_rate:.0%} of executions"
                )

            new_steps.append(new_step)

        # Add commonly injected steps
        for intent, count in conformance.commonly_injected:
            # Only add if it appears frequently and is not already in workflow
            if count >= conformance.total_executions * 0.8:
                existing_intents = {s.intent for s in new_steps}
                if intent not in existing_intents:
                    new_steps.append(
                        WorkflowIntent(
                            intent=intent,
                            target_element="",
                            input_type="click",
                            ai_reasoning=(
                                f"Added: AI injected in {count}/"
                                f"{conformance.total_executions} executions"
                            ),
                        )
                    )

        # Create improved template
        improved = WorkflowTemplate(
            id=uuid.uuid4().hex[:8],
            name=workflow.name,
            description=workflow.description,
            parameters=list(workflow.parameters),
            steps=new_steps,
            state_path=list(workflow.state_path),
            entry_states=list(workflow.entry_states),
            required_app=workflow.required_app,
            run_count=0,
            success_count=0,
            version=workflow.version + 1,
            trigger_phrases=list(workflow.trigger_phrases),
            tags=list(workflow.tags),
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            api_hint=workflow.api_hint,
        )

        return improved

    def _build_improved_template(
        self,
        original: WorkflowTemplate,
        ai_response: dict,
    ) -> WorkflowTemplate:
        """Build a new WorkflowTemplate from AI-proposed improvements."""
        from anyuse.memory.models import WorkflowIntent

        new_steps: List[WorkflowIntent] = []
        for step_data in ai_response.get("steps", []):
            new_steps.append(
                WorkflowIntent(
                    intent=step_data.get("intent", ""),
                    target_element=step_data.get("target_element", ""),
                    input_type=step_data.get("input_type", "click"),
                    optional=step_data.get("optional", False),
                    input_data=step_data.get("input_data"),
                    ai_reasoning=step_data.get("ai_reasoning", ""),
                )
            )

        return WorkflowTemplate(
            id=uuid.uuid4().hex[:8],
            name=original.name,
            description=ai_response.get("description", original.description),
            parameters=list(original.parameters),
            steps=new_steps,
            state_path=list(original.state_path),
            entry_states=list(original.entry_states),
            required_app=original.required_app,
            run_count=0,
            success_count=0,
            version=original.version + 1,
            trigger_phrases=list(original.trigger_phrases),
            tags=list(original.tags),
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            api_hint=original.api_hint,
        )

    # ------------------------------------------------------------------
    # Degradation detection (public API)
    # ------------------------------------------------------------------

    def detect_degradation(
        self,
        workflow: WorkflowTemplate,
        traces: List[ExecutionTrace],
        window: int = 10,
    ) -> Optional[MiningInsight]:
        """
        Check if a workflow's success rate is declining.

        Compare last `window` executions against overall average.
        If recent success rate is significantly lower, return a warning.

        No threshold -- returns the data with the trend; the calling
        code or AI decides what to do with it.

        Args:
            workflow: The workflow to check.
            traces: Execution traces (most recent first).
            window: Number of recent executions to compare.

        Returns:
            A MiningInsight if degradation is detected, None otherwise.
        """
        # Filter traces for this workflow
        wf_traces = [
            t for t in traces if t.workflow_name == workflow.name
        ]

        if len(wf_traces) < window:
            return None

        recent = wf_traces[:window]
        recent_success = sum(1 for t in recent if t.success) / len(recent)
        overall_success = (
            sum(1 for t in wf_traces if t.success) / len(wf_traces)
        )

        if recent_success < overall_success:
            return MiningInsight(
                insight_type="degradation_warning",
                severity="warning",
                description=(
                    f"Workflow '{workflow.name}': recent success rate "
                    f"({recent_success:.0%} over last {window} runs) is "
                    f"lower than overall ({overall_success:.0%} over "
                    f"{len(wf_traces)} runs)."
                ),
                evidence={
                    "workflow_name": workflow.name,
                    "recent_success_rate": recent_success,
                    "overall_success_rate": overall_success,
                    "window": window,
                    "total_executions": len(wf_traces),
                    "recent_failures": sum(
                        1 for t in recent if not t.success
                    ),
                    "recent_failure_intents": [
                        s.intent
                        for t in recent
                        if not t.success
                        for s in t.steps
                        if not s.success
                    ][:10],
                },
                suggested_action=(
                    "Investigate recent failures. The application UI may "
                    "have changed, or new edge cases may have appeared."
                ),
            )

        return None
