"""
Workflow Player - Execute saved workflows with visual matching.

Replay strategy:
1. Load workflow
2. Prepare window state (resize if needed)
3. For each step:
   a. If has visual_anchor: use pixel matching to find target
   b. If not found: call AI to locate element
   c. Execute action at coordinates
4. On failure: AI analyzes and suggests fixes
5. After completion: Optional final goal verification

"""
import time
import re
import inspect
from dataclasses import replace
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List, TYPE_CHECKING

from anyuse.core.result import AgentResult, ResultStatus, ActionResult, RestoreReport
from anyuse.memory.models import Workflow, WorkflowStep
from anyuse.memory.window_manager import WindowManager
from anyuse.memory.storage import WorkflowStorage

if TYPE_CHECKING:
    from anyuse.credentials.vault import CredentialVault
    from anyuse.execution.verifier import GoalVerifier, VerificationResponse


def fallback_goal(workflow, index: int, step) -> str:
    """What the model is asked to do when a replay step fails: the workflow's own task."""
    task = (getattr(workflow, "description", "") or getattr(workflow, "name", "") or "").strip()
    action = getattr(step, "action", "") or "a step"
    return (f"{task} (a recorded replay of this task stopped at step {index + 1}, {action}; "
            f"finish the task from the current screen)")


class WorkflowPlayer:
    """
    Execute saved workflows with visual matching and AI fallback.

    Features:
    - Pixel-based template matching (fast, no API cost)
    - AI fallback when matching fails
    - Variable substitution for credentials
    - Window state preparation
    - Checkpoint verification
    """

    # Pattern for variable placeholders
    VAR_PATTERN = re.compile(r'\{\{(credential|password|secret|var):(\w+)\}\}')

    def __init__(
        self,
        executor,  # ActionExecutor
        credentials: Optional["CredentialVault"] = None,
        agent=None,  # Agent for LLM fallback
        storage: Optional[WorkflowStorage] = None,
        rea_store=None,
    ):
        """
        Initialize the workflow player.

        Args:
            executor: ActionExecutor for running actions
            credentials: CredentialVault for variable substitution
            agent: Agent instance for LLM fallback on failure
            storage: WorkflowStorage for accessing workflow files

        """
        self.executor = executor
        self.credentials = credentials
        self.agent = agent
        self.storage = storage or WorkflowStorage()
        self.window_manager = WindowManager()
        self.rea_store = rea_store

    def play(
        self,
        workflow: Workflow,
        variables: Optional[Dict[str, str]] = None,
        prepare_window: bool = True,
        fallback_to_llm: bool = True,
        smart_replay: bool = True,
    ) -> AgentResult:
        """
        Execute a workflow with smart replay.

        Smart Replay Mode:
        - COORDINATE: Fast coordinate-based replay for stable UI
        - VISUAL_MATCH: Uses visual anchors to find elements that may move

        Args:
            workflow: The workflow to execute
            variables: Additional variables for substitution
            prepare_window: Whether to restore window state
            fallback_to_llm: Whether to use LLM if matching/execution fails
            smart_replay: Whether to use smart hybrid replay (default True)

        Returns:
            AgentResult
        """
        replay_mode = "smart" if (smart_replay and workflow.smart_analyzed) else "coordinate"
        print(f"  Playing workflow: {workflow.name} (mode: {replay_mode})")

        start_time = time.time()
        action_results = []
        variables = variables or {}

        # Set up template matcher
        from anyuse.memory.matcher import TemplateMatcher
        workflow_dir = self.storage.get_workflow_dir(workflow.id)
        matcher = TemplateMatcher(workflow_dir)

        # Prepare window state
        if prepare_window and workflow.window_state:
            print(f"  Preparing window state for {workflow.window_state.app}")
            self.window_manager.prepare_for_replay(workflow.window_state)
            time.sleep(0.5)

        # Execute steps
        for i, step in enumerate(workflow.steps):
            step_num = i + 1

            # Check if this step needs AI verification (smart replay)
            use_ai_for_step = (
                step.action not in {"rea_action", "rea_plan"} and
                smart_replay and
                workflow.smart_analyzed and
                step.requires_ai_verification and
                step.verification_prompt and
                self.agent
            )

            if use_ai_for_step:
                print(f"  Step {step_num}/{len(workflow.steps)}: AI VERIFY: {step.description[:50]}...")
            else:
                print(f"  Step {step_num}/{len(workflow.steps)}: {step.description[:60]}...")

            # Resolve variables in args
            is_rea = step.action in {"rea_action", "rea_plan"}
            resolved_args = step.args if is_rea else self._resolve_variables(step.args, variables)

            # SMART REPLAY: Use AI for verification steps
            if is_rea:
                from anyuse.execution.rea_replay import execute_recorded
                result = execute_recorded(step.action, resolved_args, store=self.rea_store,
                    checker=getattr(self.executor, "_compliance_checker", None))
            elif use_ai_for_step:
                result = self._execute_ai_verification_step(step, resolved_args)
            else:
                # Check replay mode from smart analysis
                step_replay_mode = getattr(step, 'replay_mode', 'coordinate')
                use_visual = getattr(step, 'use_visual_anchor', False)

                # VISUAL MATCH: Use visual anchor to find UI element
                # Only if: step has anchor AND (use_visual_anchor=True OR replay_mode=visual_match)
                should_use_visual = (
                    step.visual_anchor and
                    step.action in ["click_at", "type_text_at", "hover_at"] and
                    (use_visual or step_replay_mode == "visual_match")
                )

                if should_use_visual:
                    original_coords = (resolved_args.get("x", 0), resolved_args.get("y", 0))

                    # Create AI fallback function
                    ai_fallback = None
                    if fallback_to_llm and self.agent:
                        ai_fallback = lambda: self._ai_find_element(step.description)

                    match_result = matcher.find_target(
                        step.visual_anchor,
                        original_coords,
                        ai_fallback
                    )

                    if match_result.found:
                        resolved_args["x"] = match_result.x
                        resolved_args["y"] = match_result.y

                        if match_result.method == "pixel":
                            print(f"    Found via pixel match")
                        elif match_result.method == "ai":
                            print(f"    Found via AI")
                        else:
                            print(f"    Using original coordinates")
                else:
                    # COORDINATE REPLAY: Just use recorded coordinates
                    if step.action in ["click_at", "type_text_at", "hover_at"]:
                        print(f"    Using exact coordinates (smart analysis: no visual match needed)")

                # Wait before action
                if step.delay_before > 0:
                    time.sleep(step.delay_before)

                # Execute the step
                result = self.executor.execute(step.action, resolved_args)

            action_result = ActionResult(
                action=step.action,
                args=resolved_args,
                success=result.get("success", False),
                error=result.get("error"),
            )
            action_results.append(action_result)

            if not result.get("success"):
                print(f"    Step failed: {result.get('error', 'Unknown error')}")

                from anyuse.execution.outcome import classify_outcome, STOP_KINDS, UNCERTAIN_COMPLETION, stop_text
                kind = classify_outcome(result)
                if kind in STOP_KINDS:
                    reason = stop_text(kind, str(result.get("error") or result.get("reason") or kind))
                    return AgentResult(
                        status=ResultStatus.UNCERTAIN if kind == UNCERTAIN_COMPLETION else ResultStatus.FAILED,
                        success=False, message=reason, error=reason, outcome_reason=reason,
                        task=workflow.name, actions=action_results,
                        duration_seconds=time.time() - start_time, workflow_matched=workflow.name,
                    )

                if step.optional:
                    print(f"    Step is optional, continuing...")
                    continue

                # Try LLM fallback
                if fallback_to_llm and self.agent:
                    print(f"    Falling back to LLM...")
                    # the goal is the task in words; the recorded steps are executor calls in
                    # screen points, which a model would misread as screenshot pixels
                    goal = fallback_goal(workflow, i, step)
                    # Select compatibility arguments before invoking the agent.
                    # A TypeError raised inside run may follow an external effect.
                    run = self.agent.run
                    options = {"replay": False, "learn": False}
                    try:
                        signature = inspect.signature(run)
                        signature.bind(goal, **options)
                    except (TypeError, ValueError):
                        options = {}
                    llm_result = run(goal, **options)

                    # the recorded workflow itself failed, whatever the model did after it
                    self.storage.update_stats(workflow.name, False,
                                              f"step {step_num} failed: {result.get('error', '')}"[:200])

                    return replace(
                        llm_result,
                        message=f"Workflow failed at step {step_num}, LLM took over: {llm_result.message}",
                        task=workflow.name,
                        actions=action_results + llm_result.actions,
                        duration_seconds=time.time() - start_time,
                        workflow_matched=workflow.name,
                    )

                # No fallback, fail
                self.storage.update_stats(workflow.name, False, result.get("error", ""))

                return AgentResult(
                    status=ResultStatus.FAILED,
                    success=False,
                    message=f"Workflow failed at step {step_num}: {step.description}",
                    error=result.get("error"),
                    task=workflow.name,
                    actions=action_results,
                    duration_seconds=time.time() - start_time,
                    workflow_matched=workflow.name,
                )

            # Wait after action
            if step.delay_after > 0:
                time.sleep(step.delay_after)

        # Workflow complete

        # Success!
        duration = time.time() - start_time
        print(f"  Workflow completed in {duration:.1f}s")

        # Update stats
        self.storage.update_stats(workflow.name, True)

        return AgentResult(
            status=ResultStatus.SUCCESS,
            success=True,
            message=f"Workflow '{workflow.name}' completed successfully",
            task=workflow.name,
            actions=action_results,
            duration_seconds=duration,
            workflow_matched=workflow.name,
        )

    def _resolve_variables(
        self,
        args: Dict[str, Any],
        extra_vars: Dict[str, str]
    ) -> Dict[str, Any]:
        """Resolve variable placeholders in arguments."""
        resolved = {}

        for key, value in args.items():
            if isinstance(value, str):
                resolved[key] = self._resolve_string(value, extra_vars)
            else:
                resolved[key] = value

        return resolved

    def _resolve_string(self, text: str, extra_vars: Dict[str, str]) -> str:
        """Resolve variables in a string."""
        def replace_match(match):
            var_type = match.group(1)
            var_name = match.group(2)

            if var_type in ("credential", "password", "secret"):
                if self.credentials:
                    value = self.credentials.get_credential(var_name)
                    if value:
                        return value
            elif var_type == "var":
                if var_name in extra_vars:
                    return extra_vars[var_name]

            return match.group(0)

        return self.VAR_PATTERN.sub(replace_match, text)

    def _ai_find_element(self, description: str) -> Optional[tuple]:
        """
        Use AI to find an element on screen.

        Args:
            description: Description of the element to find

        Returns:
            (x, y) coordinates or None
        """
        if not self.agent:
            return None

        try:
            # Ask AI to find the element
            result = self.agent.run(f"Find the {description} and tell me its coordinates")
            # Parse coordinates from result... this is simplified
            # In practice, you'd want a more structured response
            return None
        except Exception:
            return None

    def _execute_ai_verification_step(
        self,
        step: WorkflowStep,
        resolved_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a step that requires AI verification.

        Uses the Computer Use model to verify/execute the step using the
        natural language verification_prompt instead of coordinates.

        Args:
            step: The workflow step with verification_prompt
            resolved_args: Resolved arguments (may contain credentials)

        Returns:
            Result dict with success status
        """
        if not self.agent:
            # No agent available, fall back to coordinate execution
            return self.executor.execute(step.action, resolved_args)

        try:
            # Use the verification prompt for AI execution
            prompt = step.verification_prompt

            # If the original action had text to type, include it
            if "text" in resolved_args and resolved_args["text"]:
                prompt += f" The text to enter is: {resolved_args['text']}"

            print(f"    AI executing: {prompt[:60]}...")

            # Run with the agent (limited turns for single step)
            result = self.agent.run(prompt)

            return {
                "success": result.success,
                "error": result.error if not result.success else None,
                "method": "ai_verification",
                "message": result.message,
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"AI verification failed: {e}",
                "method": "ai_verification",
            }



class SmartPlayer:
    """
    Smart workflow player with state graph execution as the PRIMARY path.

    Execution priority:
    1. State graph execution via ExecutionOrchestrator (multi-modal, self-healing)
    2. Fresh AI execution (records into state graph for next time)

    WorkflowPlayer is no longer used in the happy path. The state graph
    engine is the main execution system.
    """

    def __init__(
        self,
        player: WorkflowPlayer,
        agent=None,
    ):
        self.player = player  # Kept for backward compat but not used in happy path
        self.agent = agent
        self.storage = player.storage

    def run_or_play(self, task: str) -> AgentResult:
        """
        Execute a task: state graph replay if available, otherwise AI.

        Execution flow:
        1. Find matching workflow
        2. If workflow has a state graph template -> ExecutionOrchestrator
        3. If AI selects a workflow with template -> ExecutionOrchestrator
        4. No template available -> run fresh with AI agent
           (AI execution records into state graph via StateGraphRecorder)

        Args:
            task: User's task description

        Returns:
            AgentResult
        """
        # Find matching workflow
        workflow = self.storage.find(task)

        if workflow:
            # Try state graph execution (PRIMARY path)
            result = self._try_state_graph_execution(task, workflow)
            if result is not None:
                return result

            # No state graph template -- still try to run with AI.
            # The AI execution will record into state graph for next time
            # via StateGraphRecorder in the LearningOrchestrator.

        # AI selection if we have workflows
        if self.agent and self.storage.list_all():
            selected = self._ai_select_workflow(task)
            if selected:
                result = self._try_state_graph_execution(task, selected)
                if result is not None:
                    return result

        # Run fresh with AI (will record into state graph via StateGraphRecorder)
        if self.agent:
            return self.agent.run(task)

        return AgentResult.failure_result("No matching workflow and no AI agent available")

    def _try_state_graph_execution(
        self, task: str, workflow: Workflow
    ) -> Optional[AgentResult]:
        """
        Try to execute via the state graph engine.

        1. Load WorkflowTemplate for this workflow (from template.json)
        2. Load AppGuide with state graph
        3. Create ExecutionOrchestrator
        4. Run the orchestrated execution
        5. Convert OrchestratorResult to AgentResult

        Returns None if state graph execution is not available
        (missing template, missing dependencies, etc.).
        """
        # 1. Check if a WorkflowTemplate exists for this workflow
        if not self.storage.has_template(workflow.id):
            return None

        template = self.storage.get_template(workflow.id)
        if template is None:
            return None

        # 2. Try to load an AppGuide for the target app
        app_guide = None
        guide_storage = None
        try:
            from anyuse.guides.storage import GuideStorage

            guide_storage = GuideStorage()
            if template.required_app:
                app_guide = guide_storage.get_by_app(template.required_app)
        except Exception:
            guide_storage = None

        # 3. Create the orchestrator
        try:
            from anyuse.execution.orchestrator import ExecutionOrchestrator

            orchestrator = ExecutionOrchestrator(
                workflow_storage=self.storage,
                guide_storage=guide_storage,
                action_executor=(
                    self.player.executor if hasattr(self.player, "executor") else None
                ),
                rea_store=getattr(self.player, "rea_store", None),
                verbose=True,
            )
        except Exception as exc:
            # If we cannot build the orchestrator (missing config, etc.),
            # fall back to AI execution silently.
            import logging
            logging.getLogger(__name__).debug(
                "State graph orchestrator unavailable: %s", exc
            )
            return None

        # 4. Run the orchestrated execution
        started_at = None
        try:
            import asyncio

            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            started_at = time.time()
            if loop and loop.is_running():
                # We are already inside an async context; create a task.
                # This should not happen in normal CLI usage, but handle
                # it gracefully.
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    orch_result = pool.submit(
                        asyncio.run,
                        orchestrator.run(task, template, app_guide=app_guide),
                    ).result()
            else:
                orch_result = asyncio.run(
                    orchestrator.run(task, template, app_guide=app_guide)
                )
        except Exception as exc:
            if started_at is not None:
                from anyuse.execution.outcome import INCOMPLETE, stop_text
                error = f"{type(exc).__name__}: {exc}"
                message = f"State-graph execution failed: {error}"
                return AgentResult(
                    status=ResultStatus.FAILED, success=False, message=message, error=error,
                    task=task, duration_seconds=time.time() - started_at,
                    workflow_matched=workflow.name, outcome_reason=stop_text(INCOMPLETE, message),
                    restoration_report=RestoreReport.from_interrupted_orchestrator(orchestrator),
                )
            import logging
            logging.getLogger(__name__).debug(
                "State graph execution failed, falling back: %s", exc
            )
            return None

        # 5. Convert OrchestratorResult to AgentResult
        status = ResultStatus.SUCCESS if orch_result.success else ResultStatus.FAILED
        return AgentResult(
            status=status,
            success=orch_result.success,
            message=orch_result.message,
            error=None if orch_result.success else orch_result.message,
            task=task,
            duration_seconds=orch_result.duration_seconds,
            workflow_matched=workflow.name,
            restoration_report=RestoreReport.from_result(orch_result),
        )

    def _ai_select_workflow(self, task: str) -> Optional[Workflow]:
        """
        Use AI to select the best matching workflow.

        Sends workflow summaries to Gemini for selection.
        """
        summary = self.storage.get_workflow_summary()

        prompt = f"""
{summary}

User task: "{task}"

Which workflow matches best? Or should we run fresh?
Reply with just the workflow name, or "none" if no match.
"""

        try:
            result = self.agent.run(prompt)
            # Parse workflow name from result
            for workflow in self.storage.list_all():
                if workflow.name.lower() in result.message.lower():
                    return workflow
        except Exception:
            pass

        return None


if __name__ == "__main__":
    print("WorkflowPlayer module loaded")
