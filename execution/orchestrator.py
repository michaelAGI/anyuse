"""
Execution Orchestrator -- Top-level entry point for state graph execution.

Ties together:
- ExecutionPlanner (AI plans the execution)
- StateGraphExecutor (executes the plan)
- InterventionHandler (fixes failures)
- SelfHealingManager (persists learnings)
- ReplayEnvironment (clean room)

Single entry point: orchestrator.run(task, params, workflow_template)
"""
import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from anyuse.execution.executor import ExecutionResult, StateGraphExecutor
from anyuse.execution.outcome import STOP_KINDS as _STOP_KINDS
from anyuse.execution.intervention import InterventionHandler
from anyuse.execution.planner import ExecutionPlanner
from anyuse.execution.self_healing import HealingReport, SelfHealingManager
from anyuse.guides.models import AppGuide
from anyuse.guides.state_graph import StateGraph
from anyuse.memory.environment import ReplayEnvironment, RestoreReport
from anyuse.memory.models import WorkflowTemplate

logger = logging.getLogger(__name__)


def _template_recorded_graph(workflow) -> Optional[StateGraph]:
    """Load a private copy of the workflow's demonstration, when one was saved."""
    data = getattr(workflow, "recorded_graph", None)
    if not data:
        return None
    if not isinstance(data, dict):
        raise ValueError("recorded_graph must be an object")
    return StateGraph.from_dict(copy.deepcopy(data))


def _entry_app(workflow, state_graph: StateGraph) -> str:
    """The app the clean room brings forward before a replay. A demonstrated launch starts in
    its recorded entry app: the recorder's required_app can name the final app, and preparing
    that would move replay away from its first recorded screen before the drift gate runs. The
    required app goes by its recorded bundle id, which opens it under any localized name."""
    entry_app = workflow.required_app
    entry_path = list(getattr(workflow, "state_path", None) or [])
    if entry_path:
        entry_state = state_graph.get_state(entry_path[0])
        if entry_state is not None and isinstance(entry_state.app, str) and entry_state.app:
            entry_app = entry_state.app
    bundle_id = str(getattr(workflow, "required_bundle_id", "") or "")
    if bundle_id and entry_app == workflow.required_app:
        entry_app = bundle_id
    return entry_app


def _recorded_graph_problem(workflow, graph: StateGraph) -> Optional[dict]:
    """Check the complete ordered demonstration before preparation or replay acts."""
    intents = list(getattr(workflow, "steps", None) or [])
    if not intents or len(graph.transitions) != len(intents):
        return {"reason": "recorded_transition_count_mismatch"}
    for index, (intent, transition) in enumerate(zip(intents, graph.transitions)):
        before = getattr(intent, "expected_before_state", "")
        after = getattr(intent, "expected_after_state", "")
        name = getattr(intent, "intent", "")
        if (not before or not after or (transition.from_state, transition.to_state, transition.intent)
                != (before, after, name) or before not in graph.states or after not in graph.states
                or not transition.implementations):
            return {"reason": "recorded_transition_mismatch", "step_index": index}
        rea = [impl for impl in transition.implementations if impl.modality == "rea"]
        if rea:
            try:
                from anyuse.execution.rea_replay import literal_command
                if len(transition.implementations) != 1 or set(rea[0].command) != {"action", "args"}:
                    raise ValueError("REA transitions must have one literal implementation")
                literal_command(rea[0].command["action"], rea[0].command["args"])
            except (ValueError, TypeError, KeyError):
                return {"reason": "invalid_recorded_rea_command", "step_index": index}
    return None


def _drift_gate_default() -> bool:
    """AX drift gate default: ON unless ANYUSE_DRIFT_GATE_ENABLED is falsey.

    Safe to default on — it is a pure pre-step AX comparison that no-ops without a
    recorded baseline and only escalates a real mismatch to the gated,
    non-actuating visual tier (never a new actuation site).
    """
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_DRIFT_GATE_ENABLED"))


def _deterministic_replay_enabled() -> bool:
    """Deterministic replay default: ON unless ANYUSE_DETERMINISTIC_REPLAY is falsey.

    When it MISSES (no recorded path, or a rung fails), the LLM planner takes over —
    the "cheap runtime, expensive teacher" architecture. Safe default: with the
    drive gate off or AX/coords unavailable, every rung misses and we fall through
    to the planner exactly as before.
    """
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_DETERMINISTIC_REPLAY"))


# ---------------------------------------------------------------------------
# OrchestratorResult
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorResult:
    """Result of orchestrated execution."""

    success: bool = False
    message: str = ""
    execution_result: Optional[ExecutionResult] = None
    healing_report: Optional[HealingReport] = None
    duration_seconds: float = 0.0
    mode: str = "replay"  # "replay" or "explore"
    needs_guidance: bool = False
    guidance_context: Optional[dict] = None  # structured info about what's blocked
    llm_calls: int = 0
    #: the STOP kind (execution/outcome.py) when a step or the recorded replay
    #: stopped the run (a refusal, a drift, an unobserved or deviating effect)
    outcome_kind: str = ""
    restoration_report: RestoreReport = field(default_factory=RestoreReport)

    @property
    def completion_status(self) -> str:
        """Primary action success and confirmed/requested cleanup are separate."""
        return ("complete" if self.success and self.restoration_report.status in
                {"restored", "not_requested"} else "incomplete")

    def to_dict(self) -> dict:
        d = {
            "success": self.success,
            "message": self.message,
            "restoration": self.restoration_report.to_dict(),
            "cleanup_status": self.restoration_report.status,
            "completion_status": self.completion_status,
            "execution_result": (
                self.execution_result.to_dict()
                if self.execution_result
                else None
            ),
            "healing_report": (
                self.healing_report.to_dict()
                if self.healing_report
                else None
            ),
            "duration_seconds": round(self.duration_seconds, 2),
            "mode": self.mode,
            "needs_guidance": self.needs_guidance,
            "llm_calls": self.llm_calls,
        }
        if self.guidance_context is not None:
            d["guidance_context"] = self.guidance_context
        if self.outcome_kind:
            d["outcome_kind"] = self.outcome_kind
        return d


# ---------------------------------------------------------------------------
# ExecutionOrchestrator
# ---------------------------------------------------------------------------

class ExecutionOrchestrator:
    """Top-level orchestrator for state graph workflow execution."""

    def __init__(
        self,
        api_key: str = None,
        workflow_storage=None,
        guide_storage=None,
        embedding_store=None,
        guide_generator=None,
        action_executor=None,
        verbose: bool = True,
        drift_gate_enabled: Optional[bool] = None,
        hil_gate=None,
        rea_store=None,
        differential_probe=None,
        rea_trace_tracker=None,
        rea_front_reader=None,
    ):
        """
        Initialize the orchestrator and all sub-components.

        Args:
            api_key: Gemini API key.  Defaults to config.GEMINI_API_KEY.
            workflow_storage: WorkflowStorage instance.
            guide_storage: GuideStorage instance.
            embedding_store: LocalEmbeddingStore (optional, Plan 05).
            guide_generator: GuideGenerator instance.
            action_executor: ActionExecutor for coordinate/browser actions.
            verbose: Whether to log informational messages.
        """
        # ``api_key`` is retained as a source-compatible argument, but model
        # credentials are no longer captured here. The adapter reads its named
        # environment/file credential only when a call is actually made.
        self._api_key = api_key
        self._workflow_storage = workflow_storage
        self._guide_storage = guide_storage
        self._action_executor = action_executor
        self._rea_store = rea_store
        self._differential_probe = differential_probe
        self._rea_trace_tracker = rea_trace_tracker
        self._rea_front_reader = rea_front_reader
        self._active_rea_ui_trace = None
        self.rea_ui_trace = None
        self._verbose = verbose

        # Verification + safety gates wired into the executor below. Previously
        # NEITHER was passed to StateGraphExecutor, so the only LLM-free
        # verification (AX drift) and the pause-before-destructive hold were
        # unreachable. Both default to a safe posture:
        #   * drift gate — pure pre-step AX analysis; no-ops without a recorded
        #     baseline and only escalates a mismatch to the gated (non-actuating)
        #     visual tier. Default ON; override via ANYUSE_DRIFT_GATE_ENABLED=0.
        #   * HIL gate — a replay runs under its WORKFLOW's approval level
        #     (``_gate_for``; default auto = no holds). The gate built here
        #     only lends its approver to that per-workflow gate.
        self._drift_gate_enabled = (
            drift_gate_enabled if drift_gate_enabled is not None else _drift_gate_default()
        )
        if hil_gate is not None:
            self._hil_gate = hil_gate
        else:
            try:
                from anyuse.execution.hil_gate import HILGate
                self._hil_gate = HILGate()
            except Exception as exc:
                logger.warning("Could not initialize HILGate: %s", exc)
                self._hil_gate = None

        # Both are cheap and provider-free to construct. They allocate no HTTP
        # client and read no credential until a planning/intervention call.
        self._planner: Optional[ExecutionPlanner] = ExecutionPlanner()
        self._intervention_handler: Optional[InterventionHandler] = InterventionHandler()

        # Self-healing manager
        self._healing_manager = SelfHealingManager(
            workflow_storage=workflow_storage,
            guide_storage=guide_storage,
            embedding_store=embedding_store,
            guide_generator=guide_generator,
        )

        # Environment manager (clean room)
        self._environment = ReplayEnvironment()
        self.last_restoration_report = RestoreReport()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        task: str,
        workflow: WorkflowTemplate,
        params: dict = None,
        app_guide: Optional[AppGuide] = None,
        use_clean_room: bool = True,
    ) -> OrchestratorResult:
        """
        Execute a workflow template through the state graph.

        Full flow:
        1. Set up clean room environment
        2. Get current AX snapshot
        3. Build perception context
        4. AI plans the execution (one text-only call)
        5. Execute the plan step by step
        6. Process results through self-healing
        7. Restore environment
        8. Return result

        Args:
            task: Natural-language description of the task.
            workflow: The WorkflowTemplate to execute.
            params: Parameter values to fill workflow placeholders.
            app_guide: AppGuide for the target application (optional).
            use_clean_room: Whether to set up/restore environment.

        Returns:
            OrchestratorResult with execution and healing details.
        """
        params = params or {}
        start_time = time.time()
        self.last_restoration_report = RestoreReport()

        if self._verbose:
            logger.info(
                "Orchestrator: running workflow '%s' for task: %s",
                workflow.name,
                task,
            )

        # The workflow's own demonstration keeps commands from separate
        # recordings apart, including on a fresh install without an app guide.
        try:
            state_graph = _template_recorded_graph(workflow)
        except Exception as exc:
            return OrchestratorResult(
                success=False, message=f"Recorded workflow graph is unreadable: {exc}",
                mode="replay-deterministic", outcome_kind="refused")
        if state_graph is None:
            state_graph = app_guide.get_state_graph() if app_guide else StateGraph()
        else:
            problem = _recorded_graph_problem(workflow, state_graph)
            if problem is not None:
                return OrchestratorResult(
                    success=False, message=f"Recorded workflow graph does not match its steps: {problem['reason']}",
                    mode="replay-deterministic", outcome_kind="refused")

        entry_app = _entry_app(workflow, state_graph)

        # -- 1. Clean room setup --
        saved_env = None
        restoration = RestoreReport()
        if use_clean_room and entry_app:
            restoration = RestoreReport.unknown("Preparation did not provide a captured environment state.")
            try:
                saved_env = self._environment.prepare(entry_app)
                if self._verbose:
                    logger.info(
                        "Clean room prepared for app '%s'",
                        entry_app,
                    )
            except Exception as exc:
                restoration = RestoreReport.unknown("Preparation raised " + type(exc).__name__ + ": " + str(exc))
                logger.warning("Clean room setup failed: %s", exc)

        result = None
        try:
            # One capture belongs to this run, and only the physical GUI
            # dispatch sites use it. Preparation/restoration are not task input.
            try:
                from anyuse.execution.rea_ui_trace import ReaUiTraceCapture
                self._active_rea_ui_trace = ReaUiTraceCapture.for_task(
                    task, tracker=self._gui_trace_tracker(),
                    differential_probe=getattr(self, "_differential_probe", None))
            except Exception as exc:
                logger.warning("REA GUI capture unavailable: %s", type(exc).__name__)
                self._active_rea_ui_trace = None
            self.rea_ui_trace = self._active_rea_ui_trace
            result = await self._execute_workflow(
                task, workflow, params, state_graph, app_guide
            )
        finally:
            if getattr(self._active_rea_ui_trace, "probe_disabled", False) is True:
                self._differential_probe = None
            self._active_rea_ui_trace = None
            # -- 7. Restore environment --
            if saved_env is not None:
                try:
                    restored = self._environment.restore(saved_env)
                    restoration = (restored if isinstance(restored, RestoreReport) else
                                   RestoreReport.unknown("Restoration returned no structured report."))
                except Exception as exc:
                    restoration = RestoreReport.unknown("Restoration raised " + type(exc).__name__ + ": " + str(exc))
                    logger.warning("Clean room restore failed: %s", exc)
            # A primary exception propagates unchanged; its cleanup outcome is
            # still inspectable on this orchestrator. Never replace task errors.
            self.last_restoration_report = restoration
            if result is not None:
                result.restoration_report = restoration

        result.duration_seconds = time.time() - start_time
        return result

    async def explore(
        self,
        task: str,
        app_guide: Optional[AppGuide] = None,
    ) -> OrchestratorResult:
        """
        First-time execution -- no workflow template.

        The AI explores the UI to complete the task.
        Everything it does gets recorded into the state graph.

        This is the "explore" phase from the proposal doc.
        After this, a WorkflowTemplate can be created from the trace.

        For now, delegates to the existing AgentOrchestrator
        but wraps it with state graph recording.

        Args:
            task: Natural-language description of the task.
            app_guide: AppGuide for the target application (optional).

        Returns:
            OrchestratorResult indicating what happened.
        """
        start_time = time.time()
        orchestrator = None

        # Drive the goal free-form via the computer-use orchestrator (pillar 4b).
        try:
            from anyuse.execution.agent_orchestrator import AgentOrchestrator

            orchestrator = AgentOrchestrator(
                api_key=self._api_key, action_executor=self._action_executor,
                differential_probe=getattr(self, "_differential_probe", None),
                rea_trace_tracker=self._gui_trace_tracker(),
                front_reader=getattr(self, "_rea_front_reader", None))
            # run() returns the final response TEXT; success = no Error prefix.
            response = await asyncio.to_thread(orchestrator.run, task)
            success = bool(response) and not response.startswith("Error")

            # If we have a guide, record the exploration as an activity
            if app_guide is not None:
                app_guide.add_agent_activity(
                    description=f"Explored: {task}",
                    task=task,
                    success=success,
                    notes="explore mode via ExecutionOrchestrator",
                )
                if self._guide_storage is not None:
                    try:
                        self._guide_storage.save(app_guide)
                    except Exception as exc:
                        logger.debug("Could not save guide after explore: %s", exc)

            return OrchestratorResult(
                success=success,
                message=response,
                execution_result=None,
                healing_report=None,
                duration_seconds=time.time() - start_time,
                mode="explore",
                llm_calls=orchestrator.model_calls,
            )

        except Exception as exc:
            logger.error("Explore mode failed: %s", exc)
            return OrchestratorResult(
                success=False,
                message=f"Explore mode failed: {exc}",
                duration_seconds=time.time() - start_time,
                mode="explore",
            )
        finally:
            if getattr(getattr(orchestrator, "rea_ui_trace", None), "probe_disabled", False) is True:
                self._differential_probe = None

    # ------------------------------------------------------------------
    # Internal execution
    # ------------------------------------------------------------------

    def _gui_trace_tracker(self):
        tracker = getattr(self, "_rea_trace_tracker", None)
        if tracker is None and getattr(self, "_workflow_storage", None) is not None:
            from anyuse.execution.learning import ReaFallbackTracker
            tracker = ReaFallbackTracker(self._workflow_storage)
        return tracker

    def _gate_for(self, workflow):
        """The approval gate a replay of ``workflow`` runs under: the
        workflow's OWN level, resolved exactly as the draft pipeline resolves it
        (``draft_compile.workflow_hil_mode``: default ``auto`` = no step asks; a
        replay started with its values is the approval; deviations stop
        instead). The injected gate contributes only its approver. The global
        ``ANYUSE_HIL_MODE`` neither raises nor lowers a workflow's holds."""
        try:
            from anyuse.execution.draft_compile import workflow_hil_mode
            from anyuse.execution.hil_gate import HILGate
            return HILGate(mode=workflow_hil_mode(workflow),
                           approver=getattr(self._hil_gate, "approver", None))
        except Exception as exc:  # noqa: BLE001 - no gate means no holds; never a crash
            logger.warning("Could not build the workflow's approval gate: %s", exc)
            return None

    def _make_platform(self, ax_tree):
        """The macOS Platform (gated AX actuation via perform_verb) bound to ax_tree."""
        if ax_tree is None:
            return None
        try:
            from anyuse.osplatform.macos import MacOSPlatform
            return MacOSPlatform(ax_tree=ax_tree, action_executor=self._action_executor)
        except Exception as exc:
            logger.debug("Could not build MacOSPlatform: %s", exc)
            return None

    def _make_visual_verifier(self):
        """The no-LLM OCR/pixel verifier + a screenshot capture (best-effort)."""
        try:
            from anyuse.execution.visual_verify import VisualVerifier
            from anyuse.context.screenshot import ScreenshotCapture
            return VisualVerifier(), ScreenshotCapture()
        except Exception as exc:
            logger.debug("Could not build VisualVerifier: %s", exc)
            return None, None

    async def replay_deterministic(self, workflow, state_graph=None, ax_tree=None, executor=None,
                                   params=None) -> dict:
        """Replay a recorded template WITHOUT any LLM call — the cheap runtime.

        Resolve every recorded intent in its saved order, including self-loops.
        Searching only from the first to the last state loses typing and key
        steps whose structural UI state stays the same. All intents are resolved
        before any dispatch, so an incomplete recording never runs a prefix.
        Templates without intents keep the legacy graph-path lookup.
        With an injected ``executor`` this is pure/unit-testable.
        """
        path = list(getattr(workflow, "state_path", None) or [])
        intents = list(getattr(workflow, "steps", None) or [])
        transitions = None
        try:
            recorded_graph = _template_recorded_graph(workflow)
        except Exception as exc:
            return {"success": False, "reason": f"recorded graph is unreadable: {exc}",
                    "outcome_kind": "refused", "llm_calls": 0, "steps": []}
        if recorded_graph is not None:
            state_graph = recorded_graph
            transitions = list(recorded_graph.transitions)
            problem = _recorded_graph_problem(workflow, recorded_graph)
            if problem is not None:
                return {"success": False, **problem, "outcome_kind": "refused", "llm_calls": 0, "steps": []}
        elif intents:
            if state_graph is None:
                state_graph = StateGraph()
            transitions = []
            for index, intent in enumerate(intents):
                before = getattr(intent, "expected_before_state", "")
                after = getattr(intent, "expected_after_state", "")
                name = getattr(intent, "intent", "")
                matches = [t for t in state_graph.transitions
                           if before and after and t.from_state == before
                           and t.to_state == after and t.intent == name]
                if not matches:
                    return {"success": False, "reason": "no_recorded_transition",
                            "step_index": index, "llm_calls": 0, "steps": []}
                if len(matches) > 1:
                    return {"success": False, "reason": "ambiguous_recorded_transition",
                            "step_index": index, "llm_calls": 0, "steps": []}
                transitions.append(matches[0])
        elif len(path) < 2:
            return {"success": False, "reason": "no_recorded_path", "llm_calls": 0}
        if state_graph is None:
            state_graph = StateGraph()
        if executor is None:
            verifier, shot = self._make_visual_verifier()
            macos_ctx = None
            try:
                from anyuse.context.macos import MacOSContext
                macos_ctx = MacOSContext()
                if ax_tree is None:
                    from anyuse.context.accessibility import AccessibilityTree
                    from anyuse.context.live_accessibility import FrontmostAccessibilityTree
                    ax_tree = FrontmostAccessibilityTree(
                        lambda: macos_ctx.get_frontmost_pid(live=True), AccessibilityTree)
            except Exception:
                pass
            executor = StateGraphExecutor(
                action_executor=self._action_executor,
                rea_store=getattr(self, "_rea_store", None),
                rea_ui_trace=getattr(self, "_active_rea_ui_trace", None),
                rea_front_reader=getattr(self, "_rea_front_reader", None),
                ax_tree=ax_tree,
                macos_context=macos_ctx,  # so 'js' transitions replay deterministically too
                environment=self._environment,
                intervention_handler=self._intervention_handler,
                drift_gate_enabled=self._drift_gate_enabled,
                hil_gate=self._gate_for(workflow),
                platform=self._make_platform(ax_tree),
                visual_verifier=verifier,
                screenshot_capture=shot,
            )
        from anyuse.execution.deterministic import replay_path, replay_transitions
        if transitions is not None:
            result = await replay_transitions(transitions, executor, params, state_graph=state_graph)
            result["path_len"] = len(transitions)
            return result
        return await replay_path(state_graph, path[0], path[-1], executor, params)

    async def _execute_workflow(
        self,
        task: str,
        workflow: WorkflowTemplate,
        params: dict,
        state_graph: StateGraph,
        app_guide: Optional[AppGuide],
    ) -> OrchestratorResult:
        """
        Core execution logic, separated from environment management.

        Steps 2-6 of the run() flow.
        """
        # -- 2. Get current AX snapshot --
        ax_tree = None
        ax_snapshot = None
        current_ax_json = '{"elements": []}'

        try:
            from anyuse.context.macos import MacOSContext
            from anyuse.context.accessibility import AccessibilityTree
            from anyuse.context.live_accessibility import FrontmostAccessibilityTree

            macos_ctx = MacOSContext()
            ax_tree = FrontmostAccessibilityTree(
                lambda: macos_ctx.get_frontmost_pid(live=True), AccessibilityTree)
            ax_snapshot = ax_tree.snapshot()
            if ax_snapshot:
                current_ax_json = ax_snapshot.to_json()
        except ImportError:
            logger.debug(
                "MacOSContext/AccessibilityTree not available; "
                "proceeding without AX data."
            )
        except Exception as exc:
            logger.warning("Could not capture AX snapshot: %s", exc)

        # -- 3. Build perception context --
        perception_context = {}
        if ax_snapshot is not None:
            try:
                perception_context = ExecutionPlanner.build_perception_context(
                    ax_snapshot,
                    {"app_name": workflow.required_app},
                )
            except Exception as exc:
                logger.debug("Could not build perception context: %s", exc)

        # -- 3.5 Recorded replay first (the cheap runtime). Its returned llm_calls
        # is the runtime truth: deterministic adapters report zero, while a selected
        # computer-use rung reports its actual calls. On a miss the planner below
        # is the fallback/teacher. Safe default: with the drive gate off or AX/
        # coords unavailable every rung "misses" and we fall through unchanged.
        recorded_steps = {(intent.expected_before_state, intent.expected_after_state, intent.intent)
                          for intent in workflow.steps}
        has_rea = bool(
            state_graph is not None
            and any(impl.modality == "rea" for transition in state_graph.transitions
                    if (transition.from_state, transition.to_state, transition.intent) in recorded_steps
                    for impl in transition.implementations)
        )
        if has_rea and not _deterministic_replay_enabled():
            return OrchestratorResult(success=False, mode="replay-deterministic", outcome_kind="refused",
                                      message="Recorded REA workflows require deterministic replay")
        if _deterministic_replay_enabled():
            t0 = time.time()
            try:
                replay_kwargs = {"state_graph": state_graph, "ax_tree": ax_tree}
                if params:
                    replay_kwargs["params"] = params
                det = await self.replay_deterministic(workflow, **replay_kwargs)
            except Exception as exc:
                logger.debug("Deterministic replay errored: %s", exc)
                det = {"success": False, **({"outcome_kind": "uncertain_completion", "reason": str(exc)}
                                           if has_rea else {})}
            if det.get("success"):
                if self._verbose:
                    logger.info("Replay hit (%d transitions, %d LLM calls)",
                                det.get("path_len", 0), det.get("llm_calls", 0))
                return OrchestratorResult(
                    success=True,
                    message=(f"Replayed: {det.get('path_len', 0)} transitions, "
                             f"{det.get('llm_calls', 0)} LLM calls."),
                    duration_seconds=time.time() - t0,
                    mode="replay-deterministic",
                    llm_calls=int(det.get("llm_calls", 0) or 0),
                )
            elif det.get("outcome_kind") in _STOP_KINDS:
                # A refusal, a drift from the recorded screen, an unobserved or
                # deviating effect: the replay STOPS with its reason. It is not
                # a "miss" the model-backed planner should now act on (a
                # deviation stops, it does not ask).
                return OrchestratorResult(
                    success=False,
                    message=(f"Replay stopped ({det.get('outcome_kind')}) at "
                             f"{det.get('stopped_at')!r}: {det.get('reason')}"),
                    duration_seconds=time.time() - t0,
                    mode="replay-deterministic",
                    llm_calls=int(det.get("llm_calls", 0) or 0),
                    outcome_kind=str(det.get("outcome_kind")),
                )
            elif self._verbose and not has_rea:
                logger.info("Deterministic replay miss (%s); falling back to planner.",
                            det.get("reason") or det.get("stopped_at"))

            if has_rea:
                return OrchestratorResult(success=False, mode="replay-deterministic",
                                          outcome_kind="incomplete" if det.get("steps") else "refused",
                                          message="Recorded REA workflow cannot fall through to model planning")

        # -- 4. Plan the execution --
        if self._planner is None:
            return OrchestratorResult(
                success=False,
                message=(
                    "Execution planner not available (missing API key?). "
                    "Cannot plan workflow execution."
                ),
                mode="replay",
            )

        try:
            plan = await self._planner.plan(
                task=task,
                params=params,
                workflow=workflow,
                current_ax_json=current_ax_json,
                state_graph=state_graph,
                perception_context=perception_context,
                app_guide=app_guide,
            )
        except Exception as exc:
            logger.error("Execution planning failed: %s", exc)
            return OrchestratorResult(
                success=False,
                message=f"Execution planning failed: {exc}",
                mode="replay",
            )

        if self._verbose:
            logger.info(
                "Plan: %d steps, %d skipped, %d injected. Current state: %s",
                len(plan.steps),
                len(plan.skipped_steps),
                len(plan.injected_steps),
                plan.current_state,
            )

        # -- 5. Execute the plan --
        try:
            macos_ctx_for_exec = None
            try:
                from anyuse.context.macos import MacOSContext
                macos_ctx_for_exec = MacOSContext()
            except ImportError:
                pass

            verifier, shot = self._make_visual_verifier()
            executor = StateGraphExecutor(
                action_executor=self._action_executor,
                rea_store=getattr(self, "_rea_store", None),
                rea_ui_trace=getattr(self, "_active_rea_ui_trace", None),
                rea_front_reader=getattr(self, "_rea_front_reader", None),
                ax_tree=ax_tree,
                macos_context=macos_ctx_for_exec,
                environment=self._environment,
                intervention_handler=self._intervention_handler,
                drift_gate_enabled=self._drift_gate_enabled,
                hil_gate=self._gate_for(workflow),
                platform=self._make_platform(ax_tree),
                visual_verifier=verifier,
                screenshot_capture=shot,
            )

            execution_result = await executor.execute(
                plan=plan,
                workflow=workflow,
                state_graph=state_graph,
            )
        except Exception as exc:
            logger.error("Execution failed: %s", exc)
            return OrchestratorResult(
                success=False,
                message=f"Execution failed: {exc}",
                mode="replay",
                llm_calls=int(getattr(plan, "llm_calls", 0) or 0),
            )

        # -- 6. Self-healing: process results and persist learnings --
        healing_report = self._healing_manager.process_execution_result(
            result=execution_result,
            workflow=workflow,
            state_graph=state_graph,
            app_guide=app_guide,
        )

        # Build result message
        step_count = len(execution_result.steps)
        succeeded_count = sum(
            1 for s in execution_result.steps if s.success
        )

        # Check for guidance_needed from intervention results
        needs_guidance = False
        guidance_context = None

        if execution_result.success:
            message = (
                f"Workflow '{workflow.name}' completed successfully "
                f"({succeeded_count}/{step_count} steps). "
                f"{healing_report.summary()}"
            )
        else:
            failed_steps = [
                s for s in execution_result.steps if not s.success
            ]
            first_failure = (
                failed_steps[0].error if failed_steps else "Unknown"
            )
            message = (
                f"Workflow '{workflow.name}' failed "
                f"({succeeded_count}/{step_count} steps succeeded). "
                f"First failure: {first_failure}. "
                f"{healing_report.summary()}"
            )

            # If intervention was attempted and returned guidance context,
            # propagate it so the MCP layer can signal needs_guidance.
            if hasattr(execution_result, 'intervention_guidance') and execution_result.intervention_guidance:
                needs_guidance = True
                guidance_context = execution_result.intervention_guidance
            else:
                # Scan step results for intervention failure with guidance
                # The executor records guidance context on the result when
                # intervention returns guidance_needed=True.
                if hasattr(execution_result, 'guidance_from_steps') and execution_result.guidance_from_steps:
                    needs_guidance = True
                    guidance_context = execution_result.guidance_from_steps

        return OrchestratorResult(
            outcome_kind=str((execution_result.stopped or {}).get("outcome_kind") or ""),
            success=execution_result.success,
            message=message,
            execution_result=execution_result,
            healing_report=healing_report,
            mode="replay",
            needs_guidance=needs_guidance,
            guidance_context=guidance_context,
            llm_calls=(int(getattr(plan, "llm_calls", 0) or 0)
                       + int(execution_result.llm_calls or 0)),
        )
