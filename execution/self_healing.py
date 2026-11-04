"""
Self-Healing -- Workflow and State Graph Evolution.

When execution encounters issues and the AI fixes them, this module ensures
the fixes are persisted so the system learns:

1. WorkflowTemplate steps get updated with new implementations
2. State graph gets new states, transitions, and implementations
3. Guides get updated with new knowledge
4. Embeddings get re-indexed
5. Execution results feed into session review

Every execution makes the system smarter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from anyuse.execution.drift_gate import FINGERPRINT_PENDING_NOTE
from anyuse.guides.models import AppGuide
from anyuse.guides.state_graph import (
    StateGraph,
    TransitionImpl,
    UIState,
    UIFingerprint,
    compute_structural_hash,
    _role_frequency,
)
from anyuse.memory.models import WorkflowTemplate

if TYPE_CHECKING:
    # ``ExecutionResult`` / ``StepResult`` appear ONLY in annotations here, but a
    # module-top import of them pulled executor.py -> planner.py -> ``from google
    # import genai`` + a bare ``import config``. That welded self-healing, which
    # does no model call of its own, to the Gemini SDK. With the ``annotations``
    # future import above, the annotations stay strings and this block is
    # import-time inert.
    from anyuse.execution.executor import ExecutionResult, StepResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HealingReport
# ---------------------------------------------------------------------------

@dataclass
class HealingReport:
    """Summary of what was learned from an execution."""

    workflow_name: str = ""
    workflow_version_before: int = 1
    workflow_version_after: int = 1

    # What changed
    steps_updated: int = 0
    interventions_applied: int = 0
    new_states_discovered: int = 0
    new_transitions_discovered: int = 0
    new_implementations_added: int = 0

    # Workflow changes
    workflow_version_bumped: bool = False
    steps_modified: List[str] = field(default_factory=list)

    # Overall
    execution_success: bool = False
    changes_persisted: bool = False

    def to_dict(self) -> dict:
        return {
            "workflow_name": self.workflow_name,
            "workflow_version_before": self.workflow_version_before,
            "workflow_version_after": self.workflow_version_after,
            "steps_updated": self.steps_updated,
            "interventions_applied": self.interventions_applied,
            "new_states_discovered": self.new_states_discovered,
            "new_transitions_discovered": self.new_transitions_discovered,
            "new_implementations_added": self.new_implementations_added,
            "workflow_version_bumped": self.workflow_version_bumped,
            "steps_modified": self.steps_modified,
            "execution_success": self.execution_success,
            "changes_persisted": self.changes_persisted,
        }

    def summary(self) -> str:
        """Human-readable summary for OutputInterface notification."""
        parts: List[str] = []
        parts.append(
            f"Workflow '{self.workflow_name}' v{self.workflow_version_before}"
        )

        if self.execution_success:
            parts.append("executed successfully.")
        else:
            parts.append("execution failed.")

        changes: List[str] = []
        if self.steps_updated:
            changes.append(f"{self.steps_updated} step(s) updated")
        if self.interventions_applied:
            changes.append(f"{self.interventions_applied} intervention(s) applied")
        if self.new_states_discovered:
            changes.append(f"{self.new_states_discovered} new state(s) discovered")
        if self.new_transitions_discovered:
            changes.append(
                f"{self.new_transitions_discovered} new transition(s) discovered"
            )
        if self.new_implementations_added:
            changes.append(
                f"{self.new_implementations_added} new implementation(s) added"
            )
        if self.workflow_version_bumped:
            changes.append(f"version bumped to v{self.workflow_version_after}")

        if changes:
            parts.append("Learned: " + ", ".join(changes) + ".")
        else:
            parts.append("No new learnings.")

        if self.changes_persisted:
            parts.append("All changes persisted.")
        elif changes:
            parts.append("Changes NOT persisted (storage unavailable).")

        return " ".join(parts)


# ---------------------------------------------------------------------------
# SelfHealingManager
# ---------------------------------------------------------------------------

class SelfHealingManager:
    """Orchestrates learning from execution results."""

    def __init__(
        self,
        workflow_storage=None,
        guide_storage=None,
        embedding_store=None,
        guide_generator=None,
    ):
        """
        Args:
            workflow_storage: WorkflowStorage for persisting workflow templates.
            guide_storage: GuideStorage for persisting app guides.
            embedding_store: LocalEmbeddingStore (optional) for
                re-indexing workflow embeddings after significant changes.
            guide_generator: GuideGenerator for feeding execution traces
                back into the learning system.
        """
        self._workflow_storage = workflow_storage
        self._guide_storage = guide_storage
        self._embedding_store = embedding_store
        self._guide_generator = guide_generator

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_execution_result(
        self,
        result: ExecutionResult,
        workflow: WorkflowTemplate,
        state_graph: StateGraph,
        app_guide: Optional[AppGuide] = None,
    ) -> HealingReport:
        """
        Process execution results and persist all learnings.

        Called after every execution (success or failure).

        1. Update workflow template steps with success/failure data
        2. If AI intervened: add new implementations to state graph transitions
        3. If new states discovered: add to state graph
        4. If workflow steps were modified: bump version, re-save
        5. Update guide with new state graph
        6. Re-embed if significant changes (optional)
        7. Generate execution trace for session review

        Returns:
            HealingReport summarizing what was learned.
        """
        report = HealingReport(
            workflow_name=workflow.name,
            workflow_version_before=workflow.version,
            execution_success=result.success,
        )

        all_changes: List[str] = []

        # 1. Update step-level reliability data
        step_changes = self._update_workflow_steps(workflow, result.steps)
        report.steps_updated = len(step_changes)
        all_changes.extend(step_changes)

        # 2. Apply AI intervention fixes to the graph and workflow
        intervention_changes = self._apply_interventions(
            workflow, result.steps, state_graph
        )
        report.interventions_applied = len(intervention_changes)
        all_changes.extend(intervention_changes)

        # 3. Discover new states from execution
        new_state_ids = self._discover_new_states(result, state_graph)
        report.new_states_discovered = len(new_state_ids)
        if new_state_ids:
            all_changes.append(
                f"New states discovered: {', '.join(new_state_ids)}"
            )

        # Count new transitions and implementations
        report.new_transitions_discovered = result.transitions_discovered
        report.new_implementations_added = (
            report.interventions_applied + result.transitions_discovered
        )

        # 4. Bump workflow version if significant changes occurred
        significant_changes = (
            report.interventions_applied > 0
            or report.new_states_discovered > 0
        )
        if significant_changes:
            workflow.version += 1
            workflow.updated_at = datetime.now().isoformat()
            report.workflow_version_bumped = True

        report.workflow_version_after = workflow.version

        # Update workflow run counts
        workflow.run_count += 1
        if result.success:
            workflow.success_count += 1

        report.steps_modified = all_changes

        # 5-6. Persist everything
        try:
            self._persist_changes(workflow, state_graph, app_guide, all_changes)
            report.changes_persisted = True
        except Exception as exc:
            logger.warning("Failed to persist healing changes: %s", exc)
            report.changes_persisted = False

        # 7. Feed execution trace into guide generator for session review
        if self._guide_generator and app_guide:
            try:
                trace = self._build_execution_trace(result, workflow)
                self._guide_generator.learn_from_execution(
                    trace, app_guide.name
                )
            except Exception as exc:
                logger.debug(
                    "Could not feed execution trace to guide generator: %s", exc
                )

        # 8. Save execution trace for process mining
        self._save_execution_trace(result, workflow)

        logger.info("Healing report: %s", report.summary())
        return report

    def _save_execution_trace(
        self,
        result: ExecutionResult,
        workflow: WorkflowTemplate,
    ) -> None:
        """Convert execution result to trace and persist it."""
        if self._workflow_storage is None:
            return

        try:
            from anyuse.execution.process_mining import ExecutionTrace

            trace = ExecutionTrace.from_execution_result(result, workflow)
            self._workflow_storage.save_trace(trace)
            logger.debug(
                "Saved execution trace '%s' for workflow '%s'",
                trace.trace_id,
                workflow.name,
            )
        except Exception as exc:
            logger.debug(
                "Could not save execution trace for '%s': %s",
                workflow.name,
                exc,
            )

    # ------------------------------------------------------------------
    # Step reliability tracking
    # ------------------------------------------------------------------

    def _update_workflow_steps(
        self,
        workflow: WorkflowTemplate,
        step_results: List[StepResult],
    ) -> List[str]:
        """
        Update step-level reliability data.

        For each step:
        - Increment step_attempts
        - If success: increment step_successes, record last_success_modality
        - If failed: record failure details

        Returns:
            List of human-readable change descriptions.
        """
        changes: List[str] = []

        for idx, step_result in enumerate(step_results):
            if step_result.skipped:
                continue

            if idx >= len(workflow.steps):
                # AI-injected steps may exceed the original workflow length
                break

            wf_step = workflow.steps[idx]
            wf_step.step_attempts += 1

            if step_result.success:
                wf_step.step_successes += 1
                if step_result.method:
                    prev_modality = wf_step.last_success_modality
                    wf_step.last_success_modality = step_result.method
                    if prev_modality and prev_modality != step_result.method:
                        changes.append(
                            f"Step {idx + 1} '{wf_step.intent}': modality "
                            f"changed {prev_modality} -> {step_result.method}"
                        )
                    else:
                        changes.append(
                            f"Step {idx + 1} '{wf_step.intent}': "
                            f"success via {step_result.method}"
                        )
                else:
                    changes.append(
                        f"Step {idx + 1} '{wf_step.intent}': success"
                    )
            else:
                changes.append(
                    f"Step {idx + 1} '{wf_step.intent}': "
                    f"failed ({step_result.error or 'unknown'})"
                )

        return changes

    # ------------------------------------------------------------------
    # Intervention application
    # ------------------------------------------------------------------

    def _apply_interventions(
        self,
        workflow: WorkflowTemplate,
        step_results: List[StepResult],
        state_graph: StateGraph,
    ) -> List[str]:
        """
        Apply AI intervention fixes to the state graph and workflow.

        When InterventionHandler fixed a step:
        1. Add the new TransitionImpl to the relevant transition in the graph
        2. Update the workflow step with the fix info
        3. If the fix changed the target element name, update the step

        Returns:
            List of human-readable change descriptions.
        """
        changes: List[str] = []

        for idx, step_result in enumerate(step_results):
            if step_result.skipped:
                continue

            # Detect intervention: a step that succeeded but has
            # "Fixed by intervention" in its reasoning
            is_intervention = (
                step_result.success
                and step_result.reasoning
                and "intervention" in step_result.reasoning.lower()
            )

            if not is_intervention:
                continue

            # The intervention handler already added the implementation
            # to the graph during execution (in StateGraphExecutor.execute).
            # Here we record the fact and update the workflow step metadata.

            if idx < len(workflow.steps):
                wf_step = workflow.steps[idx]
                wf_step.last_success_modality = step_result.method or ""

                change_desc = (
                    f"Intervention on step {idx + 1} '{wf_step.intent}': "
                    f"fixed via {step_result.method}"
                )
                changes.append(change_desc)

                # If the step result carries a new state, record it on
                # the workflow step for future reference
                if step_result.new_state:
                    wf_step.expected_after_state = step_result.new_state

        return changes

    # ------------------------------------------------------------------
    # State discovery
    # ------------------------------------------------------------------

    def _discover_new_states(
        self,
        result: ExecutionResult,
        state_graph: StateGraph,
    ) -> List[str]:
        """
        Add newly discovered states to the graph.

        During execution, the executor may have identified states
        not previously in the graph (via AX snapshots). These are
        recorded in result.states_discovered.

        Returns:
            List of new state IDs added.
        """
        new_ids: List[str] = []

        for state_id in result.states_discovered:
            if state_id in state_graph.states:
                # Already known, just update times_seen
                state_graph.states[state_id].times_seen += 1
                state_graph.states[state_id].last_seen = (
                    datetime.now().isoformat()
                )
                continue

            # Try to build a real fingerprint if the execution result carried
            # AX element data for this state. ExecutionResult does not carry
            # per-state AX snapshots today (they are consumed during execution),
            # but probe defensively so a future enrichment is honored.
            fingerprint = self._fingerprint_for_discovered_state(result, state_id)

            if fingerprint is not None:
                new_state = UIState(
                    state_id=state_id,
                    app="",  # Will be filled by guide generator
                    fingerprint=fingerprint,
                    times_seen=1,
                    last_seen=datetime.now().isoformat(),
                )
            else:
                # Create a minimal UIState placeholder.  A richer fingerprint
                # will be built when the guide generator processes the
                # execution trace and has access to the full AX snapshot.
                # Mark it so the drift gate recognizes an empty fingerprint as a
                # KNOWN placeholder (no_recorded_fingerprint -> safe path) rather
                # than treating its 0.0 similarity as genuine UI drift.
                new_state = UIState(
                    state_id=state_id,
                    app="",  # Will be filled by guide generator
                    fingerprint=UIFingerprint(),
                    times_seen=1,
                    last_seen=datetime.now().isoformat(),
                    custom_render_notes=[FINGERPRINT_PENDING_NOTE],
                )
            state_graph.add_state(new_state)
            new_ids.append(state_id)
            logger.info("Discovered new state: %s", state_id)

        return new_ids

    @staticmethod
    def _fingerprint_for_discovered_state(
        result: ExecutionResult,
        state_id: str,
    ) -> Optional[UIFingerprint]:
        """
        Build a real UIFingerprint for a discovered state IF the execution
        result carries AX element data for it.

        ExecutionResult does not expose per-state AX snapshots today, so this
        returns None (and the caller falls back to a marked placeholder). The
        probe is defensive: if a future enrichment attaches AX element dicts
        (via a ``state_fingerprints`` mapping on the result), we build a genuine
        fingerprint here rather than persisting an empty one.
        """
        state_ax = getattr(result, "state_fingerprints", None)
        if not isinstance(state_ax, dict):
            return None

        elements = state_ax.get(state_id)
        if not elements:
            return None

        required_elements = [
            {"role": el.get("role", ""), "title": el.get("title", "")}
            for el in elements
            if el.get("role")
        ]
        struct_hash = compute_structural_hash(elements) if elements else ""
        if not required_elements and not struct_hash:
            return None

        count = len(elements)
        return UIFingerprint(
            required_elements=required_elements,
            element_count_range=(max(0, count - 20), count + 20),
            structural_hash=struct_hash,
            # Without this, every state self-healing discovers has structural
            # similarity 0.0 against any live screen, forever: the hash is
            # one-way and _decode_role_frequency() returns {} by construction.
            # identify_state could then never recognise a healed state
            #.
            role_frequency=_role_frequency(elements),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_changes(
        self,
        workflow: WorkflowTemplate,
        state_graph: StateGraph,
        app_guide: Optional[AppGuide],
        changes: List[str],
    ) -> None:
        """
        Persist all changes to storage.

        1. Save updated workflow template
        2. Save updated state graph on the guide
        3. Save guide
        4. Optionally re-embed workflow (if embedding_store available)
        """
        # 1. Save workflow template
        if self._workflow_storage is not None:
            owner_id = workflow.id
            if hasattr(self._workflow_storage, "get"):
                owner = self._workflow_storage.get(workflow.name)
                if owner is not None:
                    previous = self._workflow_storage.get_template(owner.id)
                    if previous is None or previous.id != workflow.id:
                        raise ValueError("workflow template ownership changed before persistence")
                    owner_id = owner.id
            self._workflow_storage.save_template(owner_id, workflow)
            logger.debug("Saved workflow template '%s' v%d", workflow.name, workflow.version)

        # 2 + 3. Update guide with state graph and save
        if app_guide is not None:
            app_guide.set_state_graph(state_graph)
            app_guide.updated_at = datetime.now().isoformat()

            # Record the execution as an agent activity on the guide
            app_guide.add_agent_activity(
                description=f"Executed workflow '{workflow.name}'",
                task=workflow.name,
                workflow_name=workflow.name,
                success=True,
                steps_count=len(workflow.steps),
                notes="; ".join(changes[:5]) if changes else "",
            )

            if self._guide_storage is not None:
                self._guide_storage.save(app_guide)
                logger.debug("Saved guide '%s' with updated state graph", app_guide.name)

        # 4. Re-embed workflow if embedding store is available and there
        # were significant changes
        if self._embedding_store is not None and changes:
            try:
                # Build text representation for embedding
                text = (
                    f"{workflow.name}: {workflow.description}. "
                    f"Steps: {', '.join(s.intent for s in workflow.steps)}"
                )
                self._embedding_store.upsert(
                    doc_id=f"workflow:{workflow.id}",
                    text=text,
                    metadata={
                        "type": "workflow_template",
                        "name": workflow.name,
                        "version": workflow.version,
                        "app": workflow.required_app,
                    },
                )
                logger.debug(
                    "Re-embedded workflow '%s' in embedding store",
                    workflow.name,
                )
            except Exception as exc:
                logger.debug(
                    "Could not re-embed workflow '%s': %s",
                    workflow.name,
                    exc,
                )

    # ------------------------------------------------------------------
    # Execution trace (for guide generator / session review)
    # ------------------------------------------------------------------

    def _build_execution_trace(
        self,
        result: ExecutionResult,
        workflow: WorkflowTemplate,
    ) -> dict:
        """
        Build an execution trace dict for session review.

        Format compatible with GuideGenerator.learn_from_execution().

        The trace captures each step's before/after state so the guide
        generator can enrich the state graph with full AX snapshots.
        """
        steps_trace: List[dict] = []

        for idx, step_result in enumerate(result.steps):
            step_data: dict = {
                "intent": (
                    workflow.steps[idx].intent
                    if idx < len(workflow.steps)
                    else f"step_{idx + 1}"
                ),
                "success": step_result.success,
                "method": step_result.method,
                "error": step_result.error,
                "reasoning": step_result.reasoning,
                "ax_diff_summary": step_result.ax_diff_summary,
                "new_state": step_result.new_state,
                # The actual before_ax / after_ax snapshots are not
                # available here (they were consumed during execution).
                # The trace includes the diff summary instead.
                "before_ax": [],
                "after_ax": [],
                "implementations": [],
            }

            # If the step succeeded, record the modality as an implementation
            if step_result.success and step_result.method:
                step_data["implementations"] = [
                    {
                        "modality": step_result.method,
                        "command": {},
                        "times_used": 1,
                        "times_succeeded": 1,
                    }
                ]

            steps_trace.append(step_data)

        return {
            "steps": steps_trace,
            "app": workflow.required_app,
            "workflow_name": workflow.name,
            "workflow_version": workflow.version,
            "success": result.success,
            "duration_seconds": result.duration_seconds,
            "states_discovered": result.states_discovered,
            "transitions_discovered": result.transitions_discovered,
        }
