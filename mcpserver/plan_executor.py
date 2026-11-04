"""
Plan Executor — Handles step-by-step execution with checkpoint callbacks.

When an orchestrator sends a `plan` object with `desktop_execute`, this module:
1. Iterates plan.steps[] by index
2. Executes each step.instruction as a sub-task via Agent.run()
3. At each checkpoint: captures screenshot → POSTs to checkpoint_callback_url
4. Collects checkpoint_results[] for the response

A failed step stops the plan. task_text is used when the plan has no steps.
"""
import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from anyuse.core.result import RestoreReport

logger = logging.getLogger(__name__)


async def run_agent_worker(agent, instruction):
    """Keep ownership of a synchronous worker until it actually returns."""
    control = getattr(agent, "_execution_control", None)
    if control is not None:
        control.require()
    from anyuse.core.restriction import await_owned_worker
    future = asyncio.get_running_loop().run_in_executor(None, agent.run, instruction)
    result = await await_owned_worker(future, control)
    if control is not None:
        control.require()
    return result


@dataclass
class CheckpointResult:
    """Result of evaluating a single checkpoint."""
    checkpoint_id: str
    passed: bool
    reason: str = ""
    
    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass
class PlanExecutionResult:
    """Result of executing an entire plan."""
    success: bool
    message: str = ""
    actions_taken: int = 0
    checkpoint_results: List[CheckpointResult] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)  # base64
    steps_completed: int = 0
    total_steps: int = 0
    restoration_report: RestoreReport = field(default_factory=RestoreReport)

    @property
    def completion_status(self) -> str:
        return ("complete" if self.success and self.restoration_report.status in
                {"restored", "not_requested"} else "incomplete")
    
    def to_dict(self) -> dict:
        return {
            "status": "ok" if self.success else "error",
            "result": self.message,
            "actions_taken": self.actions_taken,
            "screenshots": self.screenshots,
            "checkpoint_results": [cr.to_dict() for cr in self.checkpoint_results],
            "steps_completed": self.steps_completed,
            "total_steps": self.total_steps,
            "restoration": self.restoration_report.to_dict(),
            "cleanup_status": self.restoration_report.status,
            "completion_status": self.completion_status,
        }


class PlanExecutor:
    """
    Execute a plan step-by-step with checkpoint callbacks to the orchestrator.
    
    The plan object from the orchestrator looks like:
    {
        "steps": [
            {
                "index": 0,
                "instruction": "Open Safari and go to stripe.com",
                "checkpoints": [
                    {
                        "id": "cp_a1b2c3d4",
                        "condition": "stripe.com dashboard is visible",
                        "guard_node_id": "node_42"
                    }
                ]
            },
            ...
        ],
        "task_text": "Readable fallback of all steps",
        "checkpoint_callback_url": "http://orchestrator:8080/evaluate_checkpoint"
    }
    """
    
    def __init__(self, agent, plan: dict, boundary: Optional[dict] = None):
        """
        Args:
            agent: AnyUse Agent instance (with boundary enforcement already configured)
            plan: Plan object from the orchestrator
            boundary: Boundary rules (for guard evaluation)
        """
        self.agent = agent
        self.plan = plan
        self.boundary = boundary or {}
        self.steps = plan.get("steps", [])
        self.task_text = plan.get("task_text", "")
        self.callback_url = plan.get("checkpoint_callback_url", "")
        self.checkpoint_results: List[CheckpointResult] = []
        self.screenshots: List[str] = []
        self.total_actions = 0
        self._restoration_reports: List[RestoreReport] = []
        self.control = getattr(agent, "_execution_control", None)

    def _check_deadline(self):
        if self.control is not None:
            self.control.require()
    
    async def execute(self) -> PlanExecutionResult:
        """Execute the plan step-by-step."""
        self._restoration_reports = []
        if any(step.get("checkpoints") for step in self.steps) and not self.callback_url:
            return PlanExecutionResult(success=False, message="Declared checkpoints require a configured evaluator",
                                       total_steps=len(self.steps))
        if not self.steps:
            # No steps — fall back to task_text as free-form
            return await self._execute_freeform()
        
        steps_completed = 0
        
        for step in sorted(self.steps, key=lambda s: s.get("index", 0)):
            step_index = step.get("index", steps_completed)
            instruction = step.get("instruction", "")
            checkpoints = step.get("checkpoints", [])
            
            if not instruction:
                continue
            
            logger.info(f"Plan step {step_index}: {instruction[:80]}")
            
            # Execute the step instruction
            receipt_index = len(self._restoration_reports)
            self._restoration_reports.append(RestoreReport.unknown("Step execution did not return a cleanup receipt"))
            try:
                self._check_deadline()
                result = await run_agent_worker(self.agent, instruction)
                self._restoration_reports[receipt_index] = RestoreReport.from_result(result)
                
                # Track actions
                if self.agent._compliance_checker:
                    self.total_actions = self.agent._compliance_checker.actions_executed

                if getattr(result, "success", None) is not True:
                    reason = getattr(result, "message", "") or "Execution did not confirm success"
                    return PlanExecutionResult(
                        success=False,
                        message=f"Step {step_index} failed: {reason}",
                        actions_taken=self.total_actions,
                        checkpoint_results=self.checkpoint_results,
                        screenshots=self.screenshots,
                        steps_completed=steps_completed,
                        total_steps=len(self.steps),
                        restoration_report=RestoreReport.combine(self._restoration_reports),
                    )
                
                # Capture screenshot after step
                screenshot_b64 = await self._capture_screenshot()
                if screenshot_b64:
                    self.screenshots.append(screenshot_b64)
                
                # Evaluate checkpoints for this step
                for cp in checkpoints:
                    self._check_deadline()
                    cp_result = await self._evaluate_checkpoint(
                        checkpoint=cp,
                        screenshot_b64=screenshot_b64,
                        step_index=step_index,
                    )
                    self.checkpoint_results.append(cp_result)
                    
                    if not cp_result.passed:
                        # Checkpoint failed — abort plan
                        return PlanExecutionResult(
                            success=False,
                            message=f"Checkpoint {cp_result.checkpoint_id} failed at step {step_index}: {cp_result.reason}",
                            actions_taken=self.total_actions,
                            checkpoint_results=self.checkpoint_results,
                            screenshots=self.screenshots,
                            steps_completed=steps_completed,
                            total_steps=len(self.steps),
                            restoration_report=RestoreReport.combine(self._restoration_reports),
                        )
                
                steps_completed += 1
                
            except Exception as e:
                logger.error(f"Step {step_index} failed: {e}")
                return PlanExecutionResult(
                    success=False,
                    message=f"Step {step_index} failed: {str(e)}",
                    actions_taken=self.total_actions,
                    checkpoint_results=self.checkpoint_results,
                    screenshots=self.screenshots,
                    steps_completed=steps_completed,
                    total_steps=len(self.steps),
                    restoration_report=RestoreReport.combine(self._restoration_reports),
                )
        
        return PlanExecutionResult(
            success=True,
            message=f"Plan completed: {steps_completed}/{len(self.steps)} steps executed",
            actions_taken=self.total_actions,
            checkpoint_results=self.checkpoint_results,
            screenshots=self.screenshots,
            steps_completed=steps_completed,
            total_steps=len(self.steps),
            restoration_report=RestoreReport.combine(self._restoration_reports),
        )
    
    async def _execute_freeform(self) -> PlanExecutionResult:
        """Fall back to executing task_text as a single free-form instruction."""
        if not self.task_text:
            return PlanExecutionResult(
                success=False,
                message="Plan has no steps and no task_text fallback",
            )
        
        logger.info(f"Plan fallback: executing task_text as free-form")
        
        try:
            self._check_deadline()
            result = await run_agent_worker(self.agent, self.task_text)
            
            actions = 0
            if self.agent._compliance_checker:
                actions = self.agent._compliance_checker.actions_executed
            
            return PlanExecutionResult(
                success=getattr(result, "success", None) is True,
                message=result.to_dict().get("message", "") if hasattr(result, 'to_dict') else str(result),
                actions_taken=actions,
                screenshots=self.screenshots,
                restoration_report=RestoreReport.from_result(result),
            )
        except Exception as e:
            return PlanExecutionResult(
                success=False,
                message=f"Free-form execution failed: {str(e)}",
                restoration_report=RestoreReport.unknown("Free-form execution did not return a cleanup receipt"),
            )
    
    async def _evaluate_checkpoint(
        self,
        checkpoint: dict,
        screenshot_b64: Optional[str] = None,
        step_index: int = 0,
    ) -> CheckpointResult:
        """
        Evaluate a checkpoint by POSTing to the callback URL.
        
        POST body:
        {
            "checkpoint_id": "cp_a1b2c3d4",
            "condition": "stripe.com dashboard is visible",
            "screenshot_b64": "...",
            "guard_node_id": "node_42",
            "current_url": "https://stripe.com/dashboard",
            "agent_thought": "I navigated to the dashboard"
        }
        
        Expected response:
        { "passed": true/false, "reason": "...", "feedback": "..." }
        """
        cp_id = checkpoint.get("id", f"cp_step{step_index}")
        condition = checkpoint.get("condition", "")
        guard_node_id = checkpoint.get("guard_node_id")
        
        if not self.callback_url:
            return CheckpointResult(
                checkpoint_id=cp_id,
                passed=False,
                reason="No checkpoint evaluator configured",
            )
        
        # Build callback payload
        payload = {
            "checkpoint_id": cp_id,
            "condition": condition,
            "screenshot_b64": screenshot_b64 or "",
            "current_url": "",  # Would need browser context
            "agent_thought": getattr(self.agent, '_current_ai_description', ''),
        }
        if guard_node_id:
            payload["guard_node_id"] = guard_node_id
        
        try:
            self._check_deadline()
            timeout = 30.0
            if self.control is not None:
                timeout = min(timeout, max(.001, self.control.deadline - self.control.clock()))
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(self.callback_url, json=payload)
                response.raise_for_status()
                data = response.json()
                self._check_deadline()
                if not isinstance(data, dict) or type(data.get("passed")) is not bool:
                    raise ValueError("checkpoint evaluator must return a boolean passed verdict")
                
                return CheckpointResult(
                    checkpoint_id=cp_id,
                    passed=data["passed"] is True,
                    reason=data.get("reason", ""),
                )
        except Exception as e:
            logger.error(f"Checkpoint callback failed for {cp_id}: {e}")
            return CheckpointResult(
                checkpoint_id=cp_id,
                passed=False,
                reason=f"Callback error: {str(e)}",
            )
    
    async def _capture_screenshot(self) -> Optional[str]:
        """Capture current screen and return base64-encoded PNG."""
        try:
            loop = asyncio.get_event_loop()
            
            def capture():
                from anyuse.context.screenshot import ScreenshotCapture
                sc = ScreenshotCapture()
                import io
                img = sc.capture()
                if img:
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    return base64.b64encode(buf.getvalue()).decode("utf-8")
                return None
            
            return await loop.run_in_executor(None, capture)
        except Exception as e:
            logger.warning(f"Screenshot capture failed: {e}")
            return None
