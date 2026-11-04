"""
Main Agent interface for AnyUse.

Provides both sync and async APIs for programmatic use.
Designed to be called by other AI agents as a tool.
"""
import os
import re
import sys
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Union
from contextlib import contextmanager

# Add parent paths for imports during development
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from anyuse.core.result import AgentResult, ResultStatus, ActionResult, RestoreReport
from anyuse.credentials.accounts import CredentialMode, CredentialType
from anyuse.credentials.sso import SSOProvider, SSOConfig, create_sso_config
from anyuse.credentials.two_factor import TwoFactorMethod, TwoFactorConfig

logger = logging.getLogger(__name__)

#: a text a task quotes ("…", “…”, „…“, «…», or '…' standing apart from words)
_QUOTED_TEXT = re.compile(r'"([^"]+)"|“([^”]+)”|„([^“”]+)[“”]|«([^»]+)»|(?<!\w)\'([^\']+)\'(?!\w)')
#: a text a task names without quotes: what follows "the text" up to the end of the task
_NAMED_TEXT = re.compile(r"\b(?:the text|den text|text)\s*:?\s+(.+?)\s*$", re.IGNORECASE)


def requested_text(task: str) -> Optional[str]:
    """The exact text a task asks for, when it names one: the first quoted text, else what
    follows "the text" to the end of the task ("… containing the text Hello from AnyUse");
    None when it names none."""
    m = _QUOTED_TEXT.search(task or "")
    if m:
        return next(g for g in m.groups() if g is not None)
    m = _NAMED_TEXT.search(task or "")
    return m.group(1) if m else None


def replay_outcome(orch_result) -> Tuple[ResultStatus, str]:
    """The status of a replayed workflow from the orchestrator's own outcome: a
    success only when the replay succeeded; a run the execution STOPPED keeps its
    stop kind in the reason (``outcome.stop_text``), so a caller - the learning
    orchestrator's replay-first path - stops too instead of handing the screen to
    a model; an unobserved effect is ``UNCERTAIN``."""
    from anyuse.execution.outcome import STOP_KINDS, UNCERTAIN_COMPLETION, stop_text
    message = str(getattr(orch_result, "message", "") or "")
    if getattr(orch_result, "success", False):
        return ResultStatus.SUCCESS, "the replay succeeded"
    kind = str(getattr(orch_result, "outcome_kind", "") or "")
    if kind in STOP_KINDS:
        status = ResultStatus.UNCERTAIN if kind == UNCERTAIN_COMPLETION else ResultStatus.FAILED
        return status, stop_text(kind, message)[:300]
    return ResultStatus.FAILED, message[:300]

class Agent:
    """
    Main AnyUse agent interface.

    Designed for:
    1. Direct use by humans (via CLI)
    2. Programmatic use by Python code
    3. Agentic use by other AI agents/tools

    Auto-Learning:
        The agent automatically learns from successful executions.
        When you run a task, it:
        1. Checks if a similar task was done before
        2. If found with good success rate -> replays it (fast, no AI!)
        3. If not found or replay fails -> runs with Gemini
        4. On success -> saves the workflow for next time

    Usage:
        # Simple sync
        agent = Agent()
        result = agent.run("Open Safari")  # Learned!
        result = agent.run("Open Safari")  # Replayed (no AI)

        # Async
        result = await agent.arun("Open Safari")

        # Agentic (for tools)
        agent = Agent(auto_confirm=True)
        result = agent.run(task)
        return result.to_dict()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_turns: int = 20,
        auto_confirm: bool = False,
        verbose: bool = False,
        enable_planning: bool = True,
        enable_memory: bool = True,
        enable_credentials: bool = True,
        auto_learn: bool = True,
        auto_replay: bool = True,
        # Verification settings
        enable_verification: bool = True,
        verify_every_n_steps: int = 0,  # 0 = use smart checkpoints
        verify_smart: bool = True,  # Use planner's verification checkpoints
        # Workflow supervision settings
        capture_screenshots: bool = True,       # Capture full screenshots for supervision
        auto_verify_recording: bool = True,     # Verify workflows after recording
        auto_verify_replay: bool = False,       # Verify workflows after replay
        auto_self_improve: bool = False,        # Self-improve on verification failure
        max_improve_attempts: int = 3,          # Max self-improvement loops
        # Credential modes
        credential_mode: str = "user",
        # SSO configuration
        preferred_sso: Optional[str] = None,
        sso_account: Optional[str] = None,
        sso_credential_alias: Optional[str] = None,
        sso_2fa_method: Optional[str] = None,
        sso_totp_alias: Optional[str] = None,
        # orchestrator BOUNDARY integration
        boundary_rules: Optional[Dict[str, Any]] = None,
        scoped_credentials: Optional[Dict[str, str]] = None,
        rea_store=None,
    ):
        """
        Initialize the agent.

        Args:
            api_key: Gemini API key (or use GEMINI_API_KEY env var)
            max_turns: Maximum turns in agent loop
            auto_confirm: Skip safety confirmations (for agentic use)
            verbose: Print detailed output
            enable_planning: Use hierarchical task planning
            enable_memory: Enable workflow memory/learning
            enable_credentials: Enable credential vault
            auto_learn: Automatically record successful task executions
            auto_replay: Automatically replay matching learned workflows
            enable_verification: Enable goal verification (Gemini Flash)
            verify_every_n_steps: Verify every N steps (0 = use smart checkpoints)
            verify_smart: Use planner's verification checkpoints (default True)
            capture_screenshots: Capture full screenshots for workflow supervision
            auto_verify_recording: Verify workflows after auto-learn recording
            auto_verify_replay: Verify workflows after replay
            auto_self_improve: Automatically re-record broken steps on verification failure
            max_improve_attempts: Maximum self-improvement attempts
            credential_mode: "user" for user credentials, "agent" for agent-created
            preferred_sso: Preferred SSO provider ("google", "microsoft", etc.)
            sso_account: Account/email to use for SSO
            sso_credential_alias: Keychain alias for SSO password
            sso_2fa_method: 2FA method for SSO ("totp", "sms", "push")
            sso_totp_alias: Keychain alias for TOTP secret
            boundary_rules: orchestrator BOUNDARY rules dict for sandboxing
            scoped_credentials: Pre-resolved credential key-value pairs from the orchestrator
        """
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.max_turns = max_turns
        self.auto_confirm = auto_confirm
        self.verbose = verbose
        self.enable_planning = enable_planning
        self.enable_memory = enable_memory
        self.enable_credentials = enable_credentials
        self.auto_learn = auto_learn
        self.auto_replay = auto_replay

        # Verification settings (uses Gemini Flash)
        self.enable_verification = enable_verification
        self.verify_every_n_steps = verify_every_n_steps
        self.verify_smart = verify_smart

        # Workflow supervision settings
        self.capture_screenshots = capture_screenshots
        self.auto_verify_recording = auto_verify_recording
        self.auto_verify_replay = auto_verify_replay
        self.auto_self_improve = auto_self_improve
        self.max_improve_attempts = max_improve_attempts

        # Credential mode
        self.credential_mode = CredentialMode(credential_mode.lower())

        # SSO configuration
        self.sso_config: Optional[SSOConfig] = None
        if preferred_sso and sso_account:
            self.sso_config = create_sso_config(
                provider=preferred_sso,
                account=sso_account,
                credential_alias=sso_credential_alias,
                two_factor_method=sso_2fa_method,
                totp_alias=sso_totp_alias,
            )

        # Lazy-initialized components
        self._orchestrator = None
        self._rea_store = rea_store
        self._credentials = None
        self._workflows = None
        self._planner = None
        self._learner = None
        self._verifier = None
        self._guide_storage = None

        # orchestrator BOUNDARY integration
        self.boundary_rules = boundary_rules
        self.scoped_credentials = scoped_credentials or {}
        self._compliance_checker = None

        # Build compliance checker from boundary rules if provided.
        #
        # Default-safe: only construct a checker when a boundary is actually
        # configured. With no boundary, _compliance_checker stays None and
        # existing flows are unchanged (nothing is enforced). The checker
        # itself is also a no-op when the parsed policy is inactive (empty
        # boundary), so even a present-but-empty boundary preserves behavior.
        if self.boundary_rules:
            from anyuse.core.restriction import RestrictionPolicy, ComplianceChecker
            policy = RestrictionPolicy.from_boundary(self.boundary_rules)
            self._compliance_checker = ComplianceChecker(policy=policy)

        # Recording state
        self._recording = False
        self._recorded_actions: List[ActionResult] = []
        self._recording_name: Optional[str] = None

        # Current AI context (what Gemini said last)
        self._current_ai_description = ""

    @property
    def orchestrator(self):
        """Lazy initialize orchestrator."""
        if self._orchestrator is None:
            # Import here to avoid circular imports
            from anyuse.execution.agent_orchestrator import AgentOrchestrator
            self._orchestrator = AgentOrchestrator(
                api_key=self.api_key,
                max_turns=self.max_turns,
                auto_confirm=self.auto_confirm,
                verbose=self.verbose,
                credentials=self.credentials,
                enable_planning=self.enable_planning,
                enable_verification=self.enable_verification,
                verify_every_n_steps=self.verify_every_n_steps,
                verify_smart=self.verify_smart,
                **({"rea_store": self._rea_store} if getattr(self, "_rea_store", None) is not None else {}),
            )
            # Override compliance checker with boundary-derived one if present.
            # Tolerant wiring: the orchestrator/executor may not expose a
            # compliance hook in every build, so guard each step rather than
            # letting a missing attribute crash agent construction.
            if self._compliance_checker:
                executor = getattr(self._orchestrator, "executor", None)
                set_cc = getattr(executor, "set_compliance_checker", None)
                if callable(set_cc):
                    set_cc(self._compliance_checker)
                self._orchestrator._compliance_checker = self._compliance_checker
            # a finished run is saved as a workflow only after the goal check confirms it
            self._orchestrator.goal_check = self._goal_check_for_save
        return self._orchestrator

    @property
    def verifier(self):
        """Lazy initialize goal verifier (uses Gemini Flash)."""
        if self._verifier is None and self.enable_verification:
            from anyuse.execution.verifier import GoalVerifier
            self._verifier = GoalVerifier(api_key=self.api_key)
        return self._verifier

    @property
    def credentials(self):
        """Lazy initialize credential resolver."""
        if self._credentials is None and self.enable_credentials:
            from anyuse.credentials.vault import CredentialVault
            self._credentials = CredentialVault(
                mode=self.credential_mode,
                sso_config=self.sso_config,
            )
        return self._credentials

    @property
    def workflows(self):
        """Lazy initialize workflow storage."""
        if self._workflows is None and self.enable_memory:
            from anyuse.memory.storage import WorkflowStorage
            self._workflows = WorkflowStorage()
        return self._workflows

    @property
    def guide_storage(self):
        """Lazy initialize guide storage for state graph persistence."""
        if self._guide_storage is None:
            try:
                from anyuse.guides.storage import GuideStorage
                self._guide_storage = GuideStorage()
            except Exception:
                self._guide_storage = None
        return self._guide_storage

    @property
    def learner(self):
        """Lazy initialize learning orchestrator with StateGraphRecorder."""
        if self._learner is None and self.enable_memory:
            from anyuse.execution.learning import LearningOrchestrator
            self._learner = LearningOrchestrator(
                self.orchestrator,
                storage=self.workflows,
                auto_learn=self.auto_learn,
                auto_replay=self.auto_replay,
                verbose=self.verbose,
                guide_storage=self.guide_storage,
                replay_fn=self.play,  # token-free replay of a matched saved workflow
                # Pass supervision settings as extra kwargs (accepted via **kwargs)
                capture_screenshots=self.capture_screenshots,
                auto_verify_recording=self.auto_verify_recording,
                auto_verify_replay=self.auto_verify_replay,
                auto_self_improve=self.auto_self_improve,
                max_improve_attempts=self.max_improve_attempts,
            )
        return self._learner

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------

    #: the goal verifier's verdict counts only at this confidence or above
    VERIFIER_MIN_CONFIDENCE = 0.6

    def _decide_outcome(self, response: str, task: str = "") -> Tuple[ResultStatus, str]:
        """The run's outcome and its reason.

        There is NO "assume success". The decision is, in order:

        1. a structural failure in the orchestrator's own response (an error, a stop,
           the turn cap, "Task incomplete") -> ``FAILED``;
        2. the goal verifier, when one is configured: its model call is counted at the
           process counter (``execution.model_adapter``); a confident ``success`` /
           ``failed`` verdict decides; anything else (``partial``, ``uncertain``, a low
           confidence, a verifier error, no screenshot) -> ``UNCERTAIN`` with the reason;
        3. no verifier -> ``UNCERTAIN``: the response text alone cannot establish that a
           goal was reached.

        ``UNCERTAIN`` stops the run with its reason and is never a success.
        """
        if not response:
            return ResultStatus.FAILED, "empty response"
        # a run the EXECUTION stopped (execution/outcome.py STOP kinds: a refusal, a drift,
        # an unobserved or deviating effect) is never a success and is not re-judged by a
        # verifier: an unobserved effect is UNCERTAIN, every other stop FAILED (a
        # deviation stops, it does not ask)
        from anyuse.execution.outcome import UNCERTAIN_COMPLETION, stopped_kind
        kind = stopped_kind(response)
        if kind:
            return (ResultStatus.UNCERTAIN if kind == UNCERTAIN_COMPLETION else ResultStatus.FAILED,
                    response[:200])
        low = response.lower()
        if response == "Agent terminated" or "maximum turns reached" in low:
            return ResultStatus.FAILED, response[:200]
        if response.startswith("Error") or "Task incomplete" in response:
            return ResultStatus.FAILED, response[:200]
        text_verdict = self._text_check(task)
        if text_verdict is not None:
            return text_verdict
        if not (self.verifier and task):
            return (ResultStatus.UNCERTAIN,
                    "no goal verifier: the response text alone does not establish the goal")
        try:
            screenshot = self._capture_screenshot()
        except Exception as exc:  # noqa: BLE001 - no screen, no verdict
            return ResultStatus.UNCERTAIN, f"the goal verifier had no screenshot: {exc}"[:200]
        try:
            # judged from the screen against the task; the agent's own "done" is not evidence
            verification = self.verifier.verify_goal(
                screenshot_bytes=screenshot, goal=task, steps_completed=[],
                current_state=("Judge only from the screenshot whether the goal is reached. The app "
                               "may have reacted after the last action (an opponent's reply move, a "
                               "refreshed view): a goal reached and then answered by the app is reached."))
        except Exception as exc:  # noqa: BLE001
            return ResultStatus.UNCERTAIN, f"the goal verifier failed: {exc}"[:200]
        verdict = str(getattr(verification, "result", "") or "").lower()
        conf = float(getattr(verification, "confidence", 0.0) or 0.0)
        why = str(getattr(verification, "explanation", "") or "")[:200]
        if conf >= self.VERIFIER_MIN_CONFIDENCE and verdict == "success" and verification.is_success:
            return ResultStatus.SUCCESS, f"the goal verifier: success (p={conf:.2f}) {why}".strip()
        if conf >= self.VERIFIER_MIN_CONFIDENCE and verdict == "failed":
            return ResultStatus.FAILED, f"the goal verifier: failed (p={conf:.2f}) {why}".strip()
        return (ResultStatus.UNCERTAIN,
                f"the goal verifier was not decisive ({verdict or 'no verdict'}, p={conf:.2f}) {why}".strip())

    def _text_check(self, task: str) -> Optional[Tuple[ResultStatus, str]]:
        """When the task names an exact text (``requested_text``), the focused text element of
        the front app (read over AX; a test injects ``_focused_text_fn``) holds it with no extra
        blank lines or spaces around it. ``(FAILED, why)`` when it holds the text only with extra
        whitespace around it (for example ``"\\nHello from AnyUse"``, which the screenshot verifier
        accepted at 0.96); None when the task names no text, no text element is focused, or the
        text matches exactly or not at all (the verifier then decides). A read, no model call."""
        want = requested_text(task)
        if not want:
            return None
        reader = getattr(self, "_focused_text_fn", None)
        try:
            if reader is not None:
                info = reader()
            else:
                from anyuse.context.macos import MacOSContext
                info = MacOSContext().get_focused_text()
        except Exception as exc:  # noqa: BLE001 - no reading: the verifier decides
            logger.debug("focused text not read for the goal check: %s", exc)
            return None
        if not info:
            return None
        value = str(info.get("value") or "")
        if value != want and value.strip() == want.strip():
            return (ResultStatus.FAILED,
                    f"the text is {value[:120]!r}, not exactly {want[:120]!r}: extra blank lines or "
                    f"spaces around it")
        return None

    def _capture_screenshot(self) -> bytes:
        """The final screen for the verifier (a test injects ``_screenshot_fn``)."""
        fn = getattr(self, "_screenshot_fn", None)
        if fn is not None:
            return fn()
        from anyuse.context.screenshot import ScreenshotCapture
        return ScreenshotCapture().capture()

    def _is_success(self, response: str, task: str = "") -> bool:
        """Back-compat: True only for a decided success (never assumed)."""
        return self._decide_outcome(response, task)[0] is ResultStatus.SUCCESS

    def _decide_replayed(self, task: str, response: str) -> None:
        """A run that a saved workflow served through the RECORDED replay with no model call is
        decided by that replay's own step checks (each step's declared effect and recorded
        after-state) and the exact-text check: no goal-verifier model call on top, which made
        every learned replay cost a call. A replay the model planner served keeps the verifier."""
        lr = getattr(self, "_last_replay", None)
        if not lr or response != lr.get("message"):
            return
        if lr.get("status") is ResultStatus.SUCCESS and lr.get("mode") == "replay-deterministic" \
                and not lr.get("llm_calls"):
            verdict = self._text_check(task)
            self._decided = (task,) + (verdict or (
                ResultStatus.SUCCESS, "the recorded replay succeeded; its steps were checked without a model"))

    def _goal_check_for_save(self, task: str, response: str):
        """The orchestrator asks this before saving a finished run as a workflow: only a run the
        goal verifier confirms is learned. The verdict is kept so the run's result reuses it."""
        status, reason = self._decide_outcome(response, task)
        self._decided = (task, status, reason)
        return status is ResultStatus.SUCCESS, reason

    def _result(self, task: str, response: str, start_time: float, mark: int,
                turns_used: int) -> AgentResult:
        from anyuse.execution.model_adapter import calls_since
        decided = getattr(self, "_decided", None)
        if decided and decided[0] == task:
            status, reason = decided[1], decided[2]       # the check that gated saving
        else:
            status, reason = self._decide_outcome(response, task)
        success = status is ResultStatus.SUCCESS
        rea_executions = getattr(getattr(self, "_orchestrator", None), "rea_executions", [])
        if rea_executions and not rea_executions[-1].get("ok"):
            status = ResultStatus.UNCERTAIN if rea_executions[-1].get("status") == "uncertain" else ResultStatus.FAILED
            success = False
            reason = str(rea_executions[-1].get("reason") or "REA execution stopped")
        last_replay = getattr(self, "_last_replay", None) or {}
        report = last_replay.get("restoration_report")
        return AgentResult(
            status=status, success=success, message=response,
            error=None if success else (reason if status is ResultStatus.UNCERTAIN else response),
            task=task, duration_seconds=time.time() - start_time, turns_used=turns_used,
            max_turns=self.max_turns, llm_calls=calls_since(mark), outcome_reason=reason,
            restoration_report=(report if isinstance(report, RestoreReport) else RestoreReport()),
            rea_executions=rea_executions,
            rea_escalation=getattr(self, "_last_rea_escalation", None),
        )

    # ---------------------------------------------------------
    # Sync API
    # ---------------------------------------------------------

    def run(self, task: str, replay: bool = True, learn: bool = True) -> AgentResult:
        """
        Execute a task synchronously with auto-learning.

        Auto-Learning Flow:
        1. Check if task matches a saved workflow
        2. If match with good success rate -> replay it (fast, no AI!)
        3. If replay fails or no match -> run with Gemini
        4. If Gemini succeeds -> save as workflow for next time

        Args:
            task: Natural language task description
            replay: try a matching saved workflow first (False: never, e.g. inside a replay's takeover)
            learn: save a successful run as a workflow (False: a run that starts mid-task)

        Returns:
            AgentResult with structured result data
        """
        start_time = time.time()
        # every model call of this run (the orchestrator's, a replay's, the goal
        # verifier's) is the delta of the ONE process counter
        from anyuse.execution.model_adapter import calls_since, process_call_count
        mark = process_call_count()
        self._decided = None
        self._last_replay = None
        self._last_rea_escalation = None
        if self._orchestrator is not None and hasattr(self._orchestrator, "rea_executions"):
            self._orchestrator.rea_executions = []

        try:
            # Use learning orchestrator if enabled
            if self.enable_memory and (self.auto_learn or self.auto_replay):
                response = self.learner.run(task, replay=replay, learn=learn)
                self._last_rea_escalation = getattr(self.learner, "last_rea_escalation", None)
                self._decide_replayed(task, response)
                turns = len(self.orchestrator.history) // 2 if self.orchestrator.history else 0
                return self._result(task, response, start_time, mark, turns)

            # Fallback: Run without learning
            response = self.orchestrator.run(task)
            from anyuse.execution.learning import ReaFallbackTracker, computer_use_was_executed
            self._last_rea_escalation = ReaFallbackTracker(getattr(self, "_workflows", None)).record_run(
                task, rea_executions=getattr(self.orchestrator, "rea_executions", ()) or (),
                used_computer_use=computer_use_was_executed(self.orchestrator),
            )
            return self._result(task, response, start_time, mark, len(self.orchestrator.history) // 2)

        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            last_replay = getattr(self, "_last_replay", None) or {}
            report = last_replay.get("restoration_report")
            return AgentResult.failure_result(error, task=task, llm_calls=calls_since(mark),
                                              duration_seconds=time.time() - start_time,
                                              outcome_reason=f"exception: {error}"[:200],
                                              restoration_report=(report if isinstance(report, RestoreReport)
                                                                  else RestoreReport()),
                                              rea_executions=getattr(self._orchestrator, "rea_executions", []),
                                              rea_escalation=self._last_rea_escalation)

    # ---------------------------------------------------------
    # Async API
    # ---------------------------------------------------------

    async def arun(self, task: str) -> AgentResult:
        """
        Execute a task asynchronously.

        Args:
            task: Natural language task description

        Returns:
            AgentResult with structured result data
        """
        # For now, wrap sync in executor
        # TODO: Implement true async orchestrator
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.run, task)

    def run_background(self, task: str) -> asyncio.Task:
        """
        Start a task in the background, return immediately.

        This allows your program to continue while the agent works.
        Await the returned Task when you need the result.

        Args:
            task: Natural language task description

        Returns:
            asyncio.Task that resolves to AgentResult when complete

        Usage:
            async def main():
                agent = Agent()

                # Start task - returns immediately
                task = agent.run_background("Open Safari")

                # Your program continues...
                print("Agent working in background")

                # Await when you need the result
                result = await task
                print(f"Done: {result.message}")
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, create one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.create_task(self.arun(task))

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        pass

    # ---------------------------------------------------------
    # Workflow API
    # ---------------------------------------------------------

    @contextmanager
    def record(self, name: str):
        """
        Context manager to record a workflow.

        Usage:
            with agent.record("my_workflow"):
                agent.run("Do something")
                agent.run("Do something else")
        """
        self._recording = True
        self._recording_name = name
        self._recorded_actions = []

        if self.verbose:
            print(f"  Recording workflow: {name}")

        try:
            yield self
        finally:
            self._recording = False

            if self.workflows and self._recorded_actions:
                self.workflows.save(name, self._recorded_actions)
                if self.verbose:
                    print(f"  Workflow '{name}' saved with {len(self._recorded_actions)} actions")

    def play(self, name: str) -> AgentResult:
        """
        Play a saved workflow via the state graph engine (preferred)
        or fall back to WorkflowPlayer.

        Args:
            name: Name of the workflow to play

        Returns:
            AgentResult
        """
        if not self.workflows:
            return AgentResult.failure_result("Workflow memory not enabled")

        workflow = self.workflows.get(name)
        if not workflow:
            return AgentResult.failure_result(f"Workflow '{name}' not found")

        return self._play_workflow(workflow)

    def _play_workflow(self, workflow) -> AgentResult:
        """Internal workflow playback -- prefers state graph execution."""
        from anyuse.execution.draft_compile import play_refusal
        refusal = play_refusal(self.workflows, workflow)
        if refusal:
            return AgentResult.failure_result(refusal)
        # Try state graph execution first (PRIMARY path)
        if self.workflows and self.workflows.has_template(workflow.id):
            started_at = None
            try:
                template = self.workflows.get_template(workflow.id)
                if template is not None:
                    # Load guide for the target app (an older template may hold the whole
                    # front-app dict as required_app)
                    app_name = template.required_app
                    if isinstance(app_name, dict):
                        app_name = str(app_name.get("name") or "")
                    app_guide = None
                    if self.guide_storage and app_name:
                        try:
                            app_guide = self.guide_storage.get_by_app(app_name)
                        except Exception as exc:
                            logger.warning("the app guide of %r could not be read, so the replay has no "
                                           "recorded graph: %s", app_name, exc)
                    if app_guide is None and self.verbose:
                        logger.info("no app guide for %r: the recorded replay has no graph to walk", app_name)

                    from anyuse.execution.orchestrator import ExecutionOrchestrator
                    from anyuse.execution.model_adapter import calls_since, process_call_count

                    orch = ExecutionOrchestrator(
                        workflow_storage=self.workflows,
                        guide_storage=self.guide_storage,
                        action_executor=self.orchestrator.executor,
                        rea_store=getattr(self.orchestrator, "rea_store", None),
                        verbose=self.verbose,
                    )

                    import asyncio
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    # the planner (a recorded-replay miss) gets the task in words, not the slug
                    task_text = workflow.description or template.description or workflow.name
                    mark = process_call_count()
                    started_at = time.time()
                    if loop and loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            orch_result = pool.submit(
                                asyncio.run,
                                orch.run(task_text, template, app_guide=app_guide),
                            ).result()
                    else:
                        orch_result = asyncio.run(
                            orch.run(task_text, template, app_guide=app_guide)
                        )

                    status, reason = replay_outcome(orch_result)
                    success = status is ResultStatus.SUCCESS
                    llm_calls = max(int(getattr(orch_result, "llm_calls", 0) or 0), calls_since(mark))
                    mode = str(getattr(orch_result, "mode", "") or "")
                    served = ("the recorded replay" if mode == "replay-deterministic"
                              else "the model planner" if llm_calls else "the planned replay")
                    message = (f"{orch_result.message} (served by {served}; "
                               f"{llm_calls} model call{'' if llm_calls == 1 else 's'})")
                    self._record_play(workflow, template, success, orch_result.message)
                    restoration = RestoreReport.from_result(orch_result)
                    self._last_replay = {"workflow": workflow.name, "mode": mode, "status": status,
                                         "llm_calls": llm_calls, "message": message,
                                         "restoration_report": restoration}
                    return AgentResult(
                        status=status,
                        success=success,
                        message=message,
                        error=None if success else orch_result.message,
                        task=workflow.name,
                        duration_seconds=orch_result.duration_seconds,
                        workflow_matched=workflow.name,
                        outcome_reason=reason,
                        llm_calls=llm_calls,
                        restoration_report=restoration,
                    )
            except Exception as exc:
                if started_at is not None:
                    from anyuse.execution.outcome import INCOMPLETE, stop_text
                    error = f"{type(exc).__name__}: {exc}"
                    message = f"State-graph execution failed: {error}"
                    restoration = RestoreReport.from_interrupted_orchestrator(orch)
                    llm_calls = calls_since(mark)
                    self._last_replay = {"workflow": workflow.name, "mode": "replay",
                                         "status": ResultStatus.FAILED, "llm_calls": llm_calls,
                                         "message": message, "restoration_report": restoration}
                    return AgentResult(
                        status=ResultStatus.FAILED, success=False, message=message, error=error,
                        task=workflow.name, duration_seconds=time.time() - started_at,
                        workflow_matched=workflow.name, outcome_reason=stop_text(INCOMPLETE, message),
                        llm_calls=llm_calls, restoration_report=restoration,
                    )
                logger.warning("the state-graph replay of %r failed (%s: %s); falling back to the step "
                               "player", workflow.name, type(exc).__name__, exc)

        # FALLBACK: Use WorkflowPlayer for legacy workflows without templates
        if self.verbose:
            print(f"  Playing workflow (legacy): {workflow.name}")

        from anyuse.memory.player import WorkflowPlayer
        player = WorkflowPlayer(
            self.orchestrator.executor,
            self.credentials,
            self,  # For LLM fallback
            rea_store=getattr(self.orchestrator, "rea_store", None),
        )
        return player.play(workflow)

    def _record_play(self, workflow, template, success: bool, message: str) -> None:
        """A template replay counts as a run of the workflow (else ``workflow list`` says "Runs: 0"
        after three plays) and of its template."""
        try:
            self.workflows.update_stats(workflow.name, success, "" if success else str(message)[:200])
        except Exception as exc:  # noqa: BLE001 - the stats do not decide the replay
            logger.warning("the run stats of %r were not updated: %s", workflow.name, exc)
        try:
            template.run_count += 1
            if success:
                template.success_count += 1
            self.workflows.save_template(workflow.id, template)
        except Exception as exc:  # noqa: BLE001
            logger.warning("the template stats of %r were not updated: %s", workflow.name, exc)

    def list_workflows(self) -> List[str]:
        """List all saved workflow names."""
        if not self.workflows:
            return []
        return self.workflows.list_names()

    def export_workflow(self, name: str, filepath: str):
        """Export a workflow to JSON file."""
        if self.workflows:
            self.workflows.export(name, filepath)

    def import_workflow(self, filepath: str):
        """Import a workflow from JSON file."""
        if self.workflows:
            self.workflows.import_from(filepath)

    # ---------------------------------------------------------
    # Credential API
    # ---------------------------------------------------------

    def set_credential(
        self,
        alias: str,
        value: str,
        persist: bool = False
    ):
        """
        Store a credential.

        Args:
            alias: Name for the credential
            value: The credential value (NEVER logged!)
            persist: Whether to save to disk (encrypted)
        """
        if self.credentials:
            self.credentials.set_credential(alias, value, persist=persist)

    def use_credential(self, alias: str, press_enter: bool = False) -> bool:
        """
        Type a credential at current cursor position.

        The value is NEVER exposed to logs or LLM.

        Args:
            alias: Name of the credential
            press_enter: Whether to press Enter after

        Returns:
            True if successful
        """
        if self.credentials:
            # The vault types with pyautogui directly; an agent caller goes
            # through the foreground interlock first. (The CLI's own
            # human-at-the-keyboard command calls the vault, not this.)
            from anyuse.execution.foreground_interlock import refusal
            if refusal(f"agent.use_credential({alias})"):
                return False
            result = self.credentials.use_credential(alias, press_enter=press_enter)
            return result.get("success", False)
        return False

    def list_credentials(self) -> List[str]:
        """List credential aliases (not values!)."""
        if self.credentials:
            return self.credentials.list_aliases()
        return []

    # ---------------------------------------------------------
    # 2FA API
    # ---------------------------------------------------------

    def set_2fa_secret(self, alias: str, secret: str) -> bool:
        """
        Store a TOTP secret for an account.

        Args:
            alias: Account alias (e.g., "github", "google-sso")
            secret: Base32-encoded TOTP secret from authenticator setup

        Returns:
            True if stored successfully
        """
        if self.credentials:
            return self.credentials.set_2fa_secret(alias, secret)
        return False

    def get_2fa_code(self, alias: str) -> Optional[str]:
        """
        Get current TOTP code for an account.

        Args:
            alias: Account alias

        Returns:
            6-digit TOTP code or None
        """
        if self.credentials:
            return self.credentials.get_2fa_code(alias)
        return None

    def read_sms_code(
        self,
        sender: str = None,
        wait: bool = True,
        timeout: int = 120,
    ) -> Optional[str]:
        """
        Read verification code from SMS (Messages app).

        Requires iPhone SMS forwarding to be enabled on Mac.

        Args:
            sender: Expected sender to filter by (optional)
            wait: Whether to wait for new message
            timeout: Seconds to wait

        Returns:
            Verification code or None
        """
        if self.credentials:
            return self.credentials.read_sms_code(
                sender=sender,
                wait=wait,
                timeout=timeout,
            )
        return None

    def set_backup_codes(self, alias: str, codes: List[str]) -> bool:
        """
        Store backup/recovery codes for an account.

        Args:
            alias: Account alias
            codes: List of backup codes

        Returns:
            True if stored successfully
        """
        if self.credentials:
            return self.credentials.set_backup_codes(alias, codes)
        return False

    def get_backup_code(self, alias: str) -> Optional[str]:
        """
        Get the next unused backup code.

        Automatically marks it as used.

        Args:
            alias: Account alias

        Returns:
            Backup code or None
        """
        if self.credentials:
            return self.credentials.get_backup_code(alias)
        return None

    # ---------------------------------------------------------
    # Account Management API (Agent Mode)
    # ---------------------------------------------------------

    def create_account(
        self,
        service: str,
        credential_type: str = "website",
        username: str = None,
        email: str = None,
        url: str = None,
        app_bundle_id: str = None,
    ) -> Dict[str, Any]:
        """
        Create a new account for a service (agent mode).

        Generates secure credentials and registers the account.
        Will use preferred SSO if available.

        Args:
            service: Service name/domain
            credential_type: "website", "app", "cli", "api", "system"
            username: Username (auto-generated if None)
            email: Email (auto-generated if None)
            url: URL for websites
            app_bundle_id: Bundle ID for apps

        Returns:
            Dict with account info (no password values!)
        """
        if not self.credentials:
            return {"error": "Credentials not enabled"}

        cred_type = CredentialType(credential_type)
        account = self.credentials.create_account(
            service=service,
            credential_type=cred_type,
            username=username,
            email=email,
            url=url,
            app_bundle_id=app_bundle_id,
        )

        return {
            "service": account.service,
            "username": account.username,
            "email": account.email,
            "password_alias": account.password_alias,
            "created_via_sso": account.created_via_sso,
            "sso_provider": account.sso_provider.value if account.sso_provider else None,
        }

    def has_account(self, service: str) -> bool:
        """Check if we have an account for a service."""
        if self.credentials:
            return self.credentials.has_account(service)
        return False

    def get_account(self, service: str) -> Optional[Dict[str, Any]]:
        """Get account info for a service (no passwords!)."""
        if not self.credentials:
            return None

        account = self.credentials.get_account(service)
        if not account:
            return None

        return {
            "service": account.service,
            "type": account.credential_type.value,
            "username": account.username,
            "email": account.email,
            "password_alias": account.password_alias,
            "created_via_sso": account.created_via_sso,
            "sso_provider": account.sso_provider.value if account.sso_provider else None,
            "has_2fa": account.has_2fa,
            "two_factor_method": account.two_factor_method.value if account.two_factor_method else None,
            "created_by": account.created_by,
            "created_at": account.created_at,
        }

    def list_accounts(
        self,
        credential_type: str = None,
        created_by: str = None,
    ) -> List[Dict[str, Any]]:
        """
        List all registered accounts (no passwords!).

        Args:
            credential_type: Filter by type ("website", "app", etc.)
            created_by: Filter by creator ("user" or "agent")

        Returns:
            List of account info dicts
        """
        if not self.credentials:
            return []

        cred_type = CredentialType(credential_type) if credential_type else None
        accounts = self.credentials.list_accounts(
            credential_type=cred_type,
            created_by=created_by,
        )

        return [
            {
                "service": a.service,
                "type": a.credential_type.value,
                "username": a.username,
                "email": a.email,
                "created_by": a.created_by,
                "created_via_sso": a.created_via_sso,
            }
            for a in accounts
        ]

    # ---------------------------------------------------------
    # Verification API (Gemini Flash)
    # ---------------------------------------------------------

    def verify_goal(self, goal: str, steps_completed: List[str] = None) -> Dict[str, Any]:
        """
        Verify if a goal was achieved using Gemini Flash.

        Uses screenshot analysis to determine if the goal is complete.

        Args:
            goal: Description of what should have been achieved
            steps_completed: Optional list of steps that were performed

        Returns:
            Dict with:
            - result: "success", "partial", "failed", or "uncertain"
            - confidence: 0.0-1.0
            - explanation: What was observed
            - evidence: List of supporting observations
            - suggestions: What to try if failed
        """
        if not self.verifier:
            return {"result": "uncertain", "confidence": 0.0, "explanation": "Verifier not enabled"}

        from anyuse.context.screenshot import ScreenshotCapture
        capture = ScreenshotCapture()
        screenshot = capture.capture()

        verification = self.verifier.verify_goal(
            screenshot_bytes=screenshot,
            goal=goal,
            steps_completed=steps_completed or [],
        )

        return verification.to_dict()

    def quick_check(self, question: str) -> Dict[str, Any]:
        """
        Quick yes/no verification about the current screen.

        Uses Gemini Flash for fast, cheap verification.

        Args:
            question: Yes/no question about what's on screen

        Returns:
            Dict with answer, confidence, and explanation

        Example:
            result = agent.quick_check("Is Safari open?")
            if result["answer"]:
                print("Safari is open")
        """
        if not self.verifier:
            return {"answer": False, "confidence": 0.0, "explanation": "Verifier not enabled"}

        from anyuse.context.screenshot import ScreenshotCapture
        capture = ScreenshotCapture()
        screenshot = capture.capture()

        answer, confidence, explanation = self.verifier.quick_check(screenshot, question)

        return {
            "answer": answer,
            "confidence": confidence,
            "explanation": explanation,
        }

    def get_current_plan(self) -> Optional[Dict[str, Any]]:
        """
        Get the current execution plan (if any).

        Returns plan created by Gemini 3 Pro during task execution.

        Returns:
            Dict with plan details or None if no plan active
        """
        if self._orchestrator and self._orchestrator.current_plan:
            plan = self._orchestrator.current_plan
            return {
                "goal": plan.goal,
                "steps": [s.to_dict() for s in plan.steps],
                "progress": plan.get_progress(),
                "is_complete": plan.is_complete(),
            }
        return None

    # ---------------------------------------------------------
    # Learning API
    # ---------------------------------------------------------

    def get_learning_stats(self) -> Dict[str, Any]:
        """
        Get statistics about learned workflows.

        Returns:
            Dict with learning stats including:
            - workflows_learned: Number of saved workflows
            - total_runs: Total replay attempts
            - overall_success_rate: Success rate of replays
            - workflows: List of workflow details
        """
        if self.learner:
            return self.learner.get_learning_stats()
        return {"workflows_learned": 0, "total_runs": 0}

    def list_learned(self) -> List[str]:
        """List all learned workflow names."""
        if self.learner:
            return self.learner.list_learned()
        return self.list_workflows()

    def forget(self, name: str) -> bool:
        """
        Forget/delete a learned workflow.

        Args:
            name: Name of workflow to forget

        Returns:
            True if deleted successfully
        """
        if self.learner:
            return self.learner.forget(name)
        if self.workflows:
            return self.workflows.delete(name)
        return False

    def forget_all(self) -> int:
        """
        Forget all learned workflows.

        Returns:
            Number of workflows deleted
        """
        count = 0
        for name in self.list_learned():
            if self.forget(name):
                count += 1
        return count

    # ---------------------------------------------------------
    # Context API
    # ---------------------------------------------------------

    def get_context(self) -> Dict[str, Any]:
        """Get current macOS context."""
        from anyuse.context.macos import MacOSContext
        ctx = MacOSContext()
        return ctx.get_full_context()

    def screenshot(self) -> bytes:
        """Capture current screen as PNG bytes."""
        from anyuse.context.screenshot import ScreenshotCapture
        capture = ScreenshotCapture()
        return capture.capture()

    def screenshot_base64(self) -> str:
        """Capture current screen as base64 string."""
        from anyuse.context.screenshot import ScreenshotCapture
        capture = ScreenshotCapture()
        return capture.capture_base64()


# ---------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------

def run(task: str, **kwargs) -> AgentResult:
    """
    Quick function to run a task.

    Usage:
        from anyuse import run
        result = run("Open Safari")

        if result:
            print("Success!")
    """
    agent = Agent(**kwargs)
    return agent.run(task)


async def arun(task: str, **kwargs) -> AgentResult:
    """
    Quick async function to run a task.

    Usage:
        from anyuse import arun
        result = await arun("Open Safari")
    """
    agent = Agent(**kwargs)
    return await agent.arun(task)


# ---------------------------------------------------------
# Tool wrapper for other agents
# ---------------------------------------------------------

def computer_use_tool(
    task: str,
    auto_confirm: bool = True,
    max_turns: int = 20
) -> Dict[str, Any]:
    """
    Tool function for use by other AI agents.

    Returns structured dictionary suitable for LLM consumption.

    Usage with LangChain:
        from langchain.tools import Tool
        tool = Tool(
            name="computer_use",
            func=computer_use_tool,
            description="Control macOS computer"
        )
    """
    agent = Agent(auto_confirm=auto_confirm, max_turns=max_turns)
    result = agent.run(task)
    return result.to_dict()
