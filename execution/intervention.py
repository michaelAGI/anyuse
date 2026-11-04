"""
AI Intervention — Fixes failed steps during execution.

Strategy:
1. Send AX tree as text to Gemini Flash (cheap, fast, private)
2. If AI says it needs visual -> send screenshot (expensive, slow)
3. If AI fixes the step -> record the fix for the state graph

The AI is NOT a fallback — it's a co-pilot that can:
- Fix broken steps from AX tree alone (most cases)
- Request visual mode when AX is insufficient
- Skip unnecessary steps
- Inject new steps
"""
import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from anyuse.execution.model_adapter import ModelAdapter, image_data_url
from anyuse.execution.plan_types import PlannedStep
from anyuse.guides.state_graph import StateGraph, TransitionImpl

logger = logging.getLogger(__name__)

_INTERVENTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "command", "reasoning"],
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "ax_command", "js_command", "applescript_command",
                "coordinate_command", "need_visual", "skip", "cannot_fix",
            ],
        },
        "command": {"type": "object"},
        "reasoning": {"type": "string"},
    },
}


def _fix_refused(where: str) -> bool:
    """The JS / AppleScript / coordinate fixes below send input outside the
    executor's gated primitives, so they apply the same two checks themselves:
    the drive gate, then the foreground interlock."""
    from anyuse.execution.actions import drive_enabled
    if not drive_enabled():
        logger.info("%s skipped: actuation gated (ANYUSE_DRIVE_ENABLED off)", where)
        return True
    from anyuse.execution.foreground_interlock import refusal
    return refusal(where) is not None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class InterventionResult:
    """Result of an AI intervention attempt on a failed step."""

    success: bool = False
    action_taken: str = ""
    # "ax_command", "js_command", "applescript_command",
    # "coordinate_command", "visual_fix", "skipped", "failed"
    command: Optional[dict] = None
    modality: Optional[str] = None
    new_implementation: Optional[TransitionImpl] = None
    reasoning: str = ""
    guidance_needed: bool = False
    guidance_context: Optional[dict] = None  # for MCP signal
    llm_calls: int = 0

    def to_dict(self) -> dict:
        d = {
            "success": self.success,
            "action_taken": self.action_taken,
            "command": self.command,
            "modality": self.modality,
            "new_implementation": (
                self.new_implementation.to_dict()
                if self.new_implementation else None
            ),
            "reasoning": self.reasoning,
            "guidance_needed": self.guidance_needed,
            "llm_calls": self.llm_calls,
        }
        if self.guidance_context is not None:
            d["guidance_context"] = self.guidance_context
        return d


# ---------------------------------------------------------------------------
# InterventionHandler
# ---------------------------------------------------------------------------

class InterventionHandler:
    """
    AI-powered intervention for failed execution steps.

    Uses two models:
    - Text model (Flash): fast, cheap AX-based intervention (first pass)
    - Vision model (Computer Use): screenshot-based intervention (last resort)
    """

    def __init__(
        self,
        adapter: Optional[ModelAdapter] = None,
        visual_adapter: Optional[ModelAdapter] = None,
        model: str = "",
        vision_model: str = "",
    ):
        self._adapter = adapter or ModelAdapter.for_role(
            "planner", **({"model": model} if model else {})
        )
        self._visual_adapter = visual_adapter or ModelAdapter.for_role(
            "visual", **({"model": vision_model} if vision_model else {})
        )
        # the executor whose checked path an AX fix
        # runs through (its app-bound tree, identity / ambiguity / app / document
        # checks, approval gate). Bound by StateGraphExecutor right before it
        # asks for an intervention; without one an AX fix never acts.
        self._checked_executor = None

    def bind_executor(self, executor) -> None:
        """Run AX fixes through ``executor.execute_checked_step`` on its tree."""
        self._checked_executor = executor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def intervene(
        self,
        failed_step: PlannedStep,
        current_ax_json: str,
        state_graph: StateGraph,
        perception_context: dict,
        verification_context: dict,
    ) -> InterventionResult:
        """
        Try to fix a failed step.

        1. Send AX tree + context to Flash (text-only)
        2. AI decides: fix via AX/JS command, request visual, skip, or fail
        3. If AI requests visual: capture screenshot, send to vision model
        4. Execute the fix
        5. Return result with new implementation for graph
        """
        prompt = self._build_intervention_prompt(
            failed_step, current_ax_json, state_graph,
            perception_context, verification_context,
        )
        llm_calls = 0

        try:
            response = await self._adapter.acomplete(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                json_schema=_INTERVENTION_SCHEMA,
                schema_name="intervention",
            )
            llm_calls += response.llm_calls
            if not response.ok:
                raise RuntimeError(response.error or "intervention provider failed")
            response_text = response.text
            parsed = self._parse_intervention_response(response_text)

        except Exception as exc:
            logger.error("Intervention AI call failed: %s", exc)
            return InterventionResult(
                success=False,
                action_taken="failed",
                reasoning=f"AI call failed: {exc}",
                llm_calls=llm_calls,
            )

        action = parsed.get("action", "cannot_fix")
        reasoning = parsed.get("reasoning", "")
        command = parsed.get("command", {})

        # Handle the AI's decision
        if action == "need_visual":
            # AI says it needs a screenshot to decide
            logger.info("Intervention AI requested visual mode")
            visual_result = await self._visual_intervention(failed_step, current_ax_json)
            visual_result.llm_calls += llm_calls
            # If visual intervention also fails, enrich with guidance context
            if not visual_result.success and visual_result.action_taken == "failed":
                guidance_ctx = self._build_guidance_context(
                    failed_step, current_ax_json,
                    strategies_tried=["ax: text-based intervention", "visual: screenshot-based intervention"],
                    reasoning=visual_result.reasoning,
                )
                visual_result.guidance_needed = True
                visual_result.guidance_context = guidance_ctx
            return visual_result

        elif action == "skip":
            return InterventionResult(
                success=True,
                action_taken="skipped",
                reasoning=reasoning or "AI determined step can be skipped",
                llm_calls=llm_calls,
            )

        elif action == "cannot_fix":
            guidance_ctx = self._build_guidance_context(
                failed_step, current_ax_json,
                strategies_tried=["ax: text-based intervention"],
                reasoning=reasoning,
            )
            return InterventionResult(
                success=False,
                action_taken="failed",
                reasoning=reasoning or "AI could not determine a fix",
                guidance_needed=True,
                guidance_context=guidance_ctx,
                llm_calls=llm_calls,
            )

        elif action in ("ax_command", "js_command", "applescript_command", "coordinate_command"):
            # AI provided a fix command — execute it
            fix_result = await self._execute_fix(action, command, failed_step, reasoning)
            # If the fix command itself failed, enrich with guidance context
            if not fix_result.success:
                guidance_ctx = self._build_guidance_context(
                    failed_step, current_ax_json,
                    strategies_tried=[f"{action}: {reasoning}"],
                    reasoning=fix_result.reasoning,
                )
                fix_result.guidance_needed = True
                fix_result.guidance_context = guidance_ctx
            fix_result.llm_calls += llm_calls
            return fix_result

        else:
            return InterventionResult(
                success=False,
                action_taken="failed",
                reasoning=f"Unknown action from AI: {action}",
                llm_calls=llm_calls,
            )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_intervention_prompt(
        self,
        step: PlannedStep,
        ax_json: str,
        state_graph: StateGraph,
        perception_context: dict,
        verification_context: dict,
    ) -> str:
        """Build prompt asking AI to fix the step."""

        # Available transitions from the current state in the graph
        available_transitions = ""
        if state_graph:
            # Find transitions from all states (we might not know exact current state)
            for state_id in state_graph.states:
                transitions = state_graph.get_available_transitions(state_id)
                if transitions:
                    for t in transitions:
                        impls = ", ".join(
                            f"{impl.modality}(rate={impl.success_rate:.0%})"
                            for impl in t.implementations
                        )
                        available_transitions += (
                            f"  From {state_id}: \"{t.intent}\" -> {t.to_state} "
                            f"[{impls}]\n"
                        )

        perc = json.dumps(perception_context, indent=2) if perception_context else "{}"
        verif = json.dumps(verification_context, indent=2) if verification_context else "{}"

        prompt = f"""A workflow step failed. Here is the current UI state:

FAILED STEP:
  Intent: {step.intent}
  Target element: {step.target_element}
  Planned modality: {step.modality}
  Planned command: {json.dumps(step.command)}
  Input data: {step.input_data or "none"}

EXPECTED AFTER: {step.expected_after}

WHAT HAPPENED (verification context):
{verif}

CURRENT AX TREE:
{ax_json[:12000]}

PERCEPTION SIGNALS:
{perc}

AVAILABLE TRANSITIONS FROM KNOWN STATES:
{available_transitions if available_transitions else "  (no known transitions)"}

INSTRUCTIONS:
- Analyze the AX tree to understand the current UI state.
- Determine if the failed step can be fixed with an alternative command.
- If the element exists in the AX tree under a different name/role, provide the correct command.
- Prefer ax_command, then coordinate_command. Use js_command or applescript_command only when neither
  can do it: AppleScript (and browser JavaScript, which runs over AppleScript) runs only against an app
  that already granted macOS Automation, and is skipped otherwise.
- If you cannot determine the fix from the AX tree alone, request visual mode.
- If the step is unnecessary (e.g., the expected state is already achieved), skip it.

Respond with JSON:
{{
    "action": "ax_command" | "js_command" | "applescript_command" | "coordinate_command" | "need_visual" | "skip" | "cannot_fix",
    "command": {{}},
    "reasoning": "explanation of your decision"
}}

For ax_command, use: {{"role": "AXButton", "title": "...", "action": "press"}}
For js_command, use: {{"script": "document.querySelector(...).click()"}}
For applescript_command, use: {{"script": "tell application ..."}}
For coordinate_command, use: {{"x": 500, "y": 300, "action": "click"}}"""

        return prompt

    # ------------------------------------------------------------------
    # Visual intervention (last resort)
    # ------------------------------------------------------------------

    async def _visual_intervention(
        self, step: PlannedStep, ax_json: str
    ) -> InterventionResult:
        """AI requested visual mode. Send screenshot + AX tree."""
        # Capture a screenshot
        try:
            screenshot_bytes = await asyncio.to_thread(self._capture_screenshot)
        except Exception as exc:
            logger.error("Screenshot capture failed: %s", exc)
            return InterventionResult(
                success=False,
                action_taken="failed",
                reasoning=f"Could not capture screenshot for visual intervention: {exc}",
            )

        if screenshot_bytes is None:
            return InterventionResult(
                success=False,
                action_taken="failed",
                reasoning="Screenshot capture returned empty result",
            )

        # Build a prompt for the vision model
        visual_prompt = f"""A workflow step failed and I need visual help to fix it.

FAILED STEP:
  Intent: {step.intent}
  Target element: {step.target_element}
  Expected: {step.expected_after}

CURRENT AX TREE (may be incomplete):
{ax_json[:8000]}

Look at the screenshot and determine how to execute this step.
Respond with JSON:
{{
    "action": "ax_command" | "js_command" | "coordinate_command" | "skip" | "cannot_fix",
    "command": {{}},
    "reasoning": "what you see and how to fix it"
}}

For coordinate_command, provide pixel coordinates from the screenshot:
{{"x": <pixel_x>, "y": <pixel_y>, "action": "click"}}"""

        llm_calls = 0
        try:
            response = await self._visual_adapter.acomplete(
                [{"role": "user", "content": visual_prompt}],
                temperature=0.1,
                json_schema=_INTERVENTION_SCHEMA,
                schema_name="visual_intervention",
                images=[image_data_url(screenshot_bytes)],
            )
            llm_calls += response.llm_calls
            if not response.ok:
                raise RuntimeError(response.error or "visual intervention provider failed")
            response_text = response.text
            parsed = self._parse_intervention_response(response_text)

            action = parsed.get("action", "cannot_fix")
            command = parsed.get("command", {})
            reasoning = parsed.get("reasoning", "Visual intervention")

            if action in ("ax_command", "js_command", "applescript_command", "coordinate_command"):
                fix_result = await self._execute_fix(action, command, step, reasoning)
                fix_result.llm_calls += llm_calls
                return fix_result
            elif action == "skip":
                return InterventionResult(
                    success=True,
                    action_taken="skipped",
                    reasoning=reasoning,
                    llm_calls=llm_calls,
                )
            else:
                return InterventionResult(
                    success=False,
                    action_taken="failed",
                    reasoning=reasoning or "Visual AI could not fix the step",
                    llm_calls=llm_calls,
                )

        except Exception as exc:
            logger.error("Visual intervention failed: %s", exc)
            return InterventionResult(
                success=False,
                action_taken="failed",
                reasoning=f"Visual intervention error: {exc}",
                llm_calls=llm_calls,
            )

    # ------------------------------------------------------------------
    # Fix execution
    # ------------------------------------------------------------------

    async def _execute_fix(
        self,
        action: str,
        command: dict,
        original_step: PlannedStep,
        reasoning: str,
    ) -> InterventionResult:
        """Execute the fix command provided by AI."""

        modality_map = {
            "ax_command": "ax",
            "js_command": "js",
            "applescript_command": "applescript",
            "coordinate_command": "coordinate",
        }
        modality = modality_map.get(action, "unknown")

        # Create a modified step with the fix command
        fix_step = PlannedStep(
            intent=original_step.intent,
            source="ai_injected",
            target_element=original_step.target_element,
            modality=modality,
            command=command,
            input_data=original_step.input_data,
            needs_visual=False,
            expected_after=original_step.expected_after,
            expected_after_state=original_step.expected_after_state,
            reasoning=reasoning,
        )

        # We need access to the executor to run the fix.
        # Since the intervention handler is called from the executor,
        # we execute the fix command directly here.
        success = False
        error = None

        try:
            if modality == "ax":
                success = await self._run_ax_fix(command, original_step)
            elif modality == "js":
                success = await self._run_js_fix(command)
            elif modality == "applescript":
                success = await self._run_applescript_fix(command)
            elif modality == "coordinate":
                success = await self._run_coordinate_fix(command)
            else:
                error = f"Unknown fix modality: {modality}"
        except Exception as exc:
            error = str(exc)
            logger.error("Fix execution failed: %s", exc)

        if success:
            # Build a new TransitionImpl to record in the state graph
            new_impl = TransitionImpl(
                modality=modality,
                command=command,
                times_used=1,
                times_succeeded=1,
            )
            return InterventionResult(
                success=True,
                action_taken=action,
                command=command,
                modality=modality,
                new_implementation=new_impl,
                reasoning=reasoning,
            )
        else:
            return InterventionResult(
                success=False,
                action_taken="failed",
                command=command,
                modality=modality,
                reasoning=error or f"Fix command failed (modality={modality})",
            )

    async def _run_ax_fix(self, command: dict, original_step: Optional[PlannedStep] = None) -> bool:
        """Execute an AX fix command through the run's checked path.

        It used to take the FRONTMOST app's pid and press ``find(role, title)[0]``:
        no identity, no ambiguity check, whatever app happened to be in front.
        Now the fix is a step like any other, run by the bound executor's
        ``execute_checked_step`` on the run's app-bound tree: a role/title with
        no recorded identity is accepted only when exactly ONE element matches,
        the app and document the failed step recorded are re-checked right
        before acting, the verb must be one the executor supports, and the
        approval gate sees it. No bound executor (or one without a tree) means
        no fix: never the frontmost app's first match."""
        executor = self._checked_executor
        if executor is None or getattr(executor, "_ax_tree", None) is None:
            logger.warning("AX fix refused: no app-bound AX tree for this run (never the frontmost "
                           "app's first match)")
            return False
        raw = str(command.get("action", "press") or "press")
        action = raw[2:].lower() if raw.startswith("AX") else raw
        fix_cmd = {"role": command.get("role", ""), "title": command.get("title", ""),
                   "action": action}
        for key in ("keycode", "flags", "range", "text"):
            if key in command:
                fix_cmd[key] = command[key]
        # the app and the document the failed step was recorded in
        orig = (original_step.command or {}) if original_step is not None else {}
        sel = orig.get("selector") if isinstance(orig.get("selector"), dict) else {}
        for key in ("app", "document"):
            if sel.get(key) or orig.get(key):
                fix_cmd[key] = sel.get(key) or orig.get(key)
        value = command.get("value")
        step = PlannedStep(
            intent=(original_step.intent if original_step is not None else "intervention fix"),
            source="ai_injected", target_element=str(command.get("title", "")),
            modality="ax", command=fix_cmd,
            input_data=value if value is not None else (
                original_step.input_data if original_step is not None else None),
            reasoning="intervention AX fix (checked path)",
        )
        try:
            res, _verification = await executor.execute_checked_step(step)
        except Exception as exc:  # noqa: BLE001 - a fix that errors did not happen
            logger.error("AX fix execution error: %s", exc)
            return False
        if not res.get("success"):
            logger.info("AX fix not applied (%s): %s", res.get("reason_code") or res.get("outcome_kind"),
                        res.get("error"))
        return bool(res.get("success"))

    async def _run_js_fix(self, command: dict) -> bool:
        """Execute a JavaScript fix command."""
        if _fix_refused("intervention.js_fix"):
            return False
        try:
            from anyuse.context.macos import MacOSContext

            ctx = MacOSContext()
            script = command.get("script", "")
            if not script:
                return False
            # browser JavaScript runs over AppleScript: only in a browser that granted Automation
            from anyuse.context.macos import _CHROMIUM_BROWSERS, applescript_refusal
            browser = str((ctx.get_front_app_live() or {}).get("name") or "")
            no_consent = (applescript_refusal(f'tell application "{browser}"')
                          if browser == "Safari" or browser in _CHROMIUM_BROWSERS
                          else f"the front app ({browser or 'unknown'}) is no browser")
            if no_consent:
                logger.info("JS fix skipped: %s", no_consent)
                return False

            result = ctx.execute_javascript_in_browser(script, browser=browser)
            return result is not None

        except Exception as exc:
            logger.error("JS fix execution error: %s", exc)
            return False

    async def _run_applescript_fix(self, command: dict) -> bool:
        """Execute an AppleScript fix command, only against apps that already granted Automation
        (a fix's ``tell application "TextEdit"`` raised the macOS consent prompt in an
        unattended replay; it timed out and macOS recorded a denial)."""
        script = command.get("script", "")
        if not script:
            return False
        if _fix_refused("intervention.applescript_fix"):
            return False
        from anyuse.context.macos import applescript_refusal
        no_consent = applescript_refusal(script)
        if no_consent:
            logger.info("AppleScript fix skipped: %s", no_consent)
            return False

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode == 0
        except Exception as exc:
            logger.error("AppleScript fix execution error: %s", exc)
            return False

    async def _run_coordinate_fix(self, command: dict) -> bool:
        """Execute a coordinate-based fix command."""
        if _fix_refused("intervention.coordinate_fix"):
            return False
        try:
            import pyautogui

            x = command.get("x", 0)
            y = command.get("y", 0)
            action = command.get("action", "click")

            if action == "click":
                await asyncio.to_thread(pyautogui.click, x, y)
            elif action == "double_click":
                await asyncio.to_thread(pyautogui.doubleClick, x, y)
            elif action == "right_click":
                await asyncio.to_thread(pyautogui.rightClick, x, y)
            elif action == "type":
                text = command.get("text", "")
                await asyncio.to_thread(pyautogui.click, x, y)
                await asyncio.sleep(0.1)
                await asyncio.to_thread(pyautogui.typewrite, text, interval=0.02)
            else:
                await asyncio.to_thread(pyautogui.click, x, y)

            return True

        except Exception as exc:
            logger.error("Coordinate fix execution error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Guidance context (for MCP needs_guidance signal)
    # ------------------------------------------------------------------

    def _build_guidance_context(
        self,
        failed_step: PlannedStep,
        current_ax_json: str,
        strategies_tried: List[str],
        reasoning: str,
    ) -> dict:
        """
        Build structured guidance context when intervention cannot fix a step.

        Extracts actionable elements from the AX tree and formulates a
        question the calling agent can relay to the user.

        Args:
            failed_step: The step that failed.
            current_ax_json: Current AX tree as JSON.
            strategies_tried: List of strategies that were attempted.
            reasoning: AI's reasoning for why it could not fix.

        Returns:
            Structured dict with intent, what was tried, available options,
            and a human-readable question.
        """
        # Extract actionable elements from the AX tree summary
        options_available = []
        try:
            ax_data = json.loads(current_ax_json)
            elements = ax_data if isinstance(ax_data, list) else ax_data.get("elements", [])
            for el in elements:
                role = el.get("role", "")
                title = el.get("title", "")
                # Only include elements that could be interacted with
                if role in (
                    "AXButton", "AXLink", "AXMenuItem", "AXPopUpButton",
                    "AXCheckBox", "AXRadioButton", "AXTextField",
                    "AXTextArea", "AXComboBox", "AXTab", "AXToolbar",
                    "AXMenuBarItem",
                ):
                    label = title or role
                    if label and label not in options_available:
                        options_available.append(label)
                        # Cap at 20 options to keep the payload manageable
                        if len(options_available) >= 20:
                            break
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        # Truncate AX JSON for the current_context field
        context_summary = current_ax_json[:3000] if current_ax_json else ""

        question = (
            f"The step '{failed_step.intent}' could not be completed. "
            f"Target element: '{failed_step.target_element}'. "
            f"Expected outcome: '{failed_step.expected_after}'. "
            f"Which element or action should be used?"
        )

        return {
            "intent": failed_step.intent,
            "what_tried": strategies_tried,
            "current_context": context_summary,
            "options_available": options_available,
            "question": question,
            "target_element": failed_step.target_element,
            "expected_after": failed_step.expected_after,
            "reasoning": reasoning,
        }

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_intervention_response(self, response: str) -> dict:
        """Parse AI's fix response from JSON."""
        if not response.strip():
            return {"action": "cannot_fix", "reasoning": "Empty AI response"}

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            cleaned = response.strip()
            if "```json" in cleaned:
                cleaned = cleaned.split("```json", 1)[1]
                cleaned = cleaned.split("```", 1)[0].strip()
            elif "```" in cleaned:
                cleaned = cleaned.split("```", 1)[1]
                cleaned = cleaned.split("```", 1)[0].strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                logger.warning("Cannot parse intervention response: %s", response[:200])
                return {
                    "action": "cannot_fix",
                    "reasoning": f"Failed to parse AI response: {response[:200]}",
                }

    # ------------------------------------------------------------------
    # Screenshot capture
    # ------------------------------------------------------------------

    def _capture_screenshot(self) -> Optional[bytes]:
        """Capture the current screen as PNG bytes."""
        try:
            import Quartz
            from Quartz import (
                CGWindowListCreateImage,
                CGRectInfinite,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )

            image = CGWindowListCreateImage(
                CGRectInfinite,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
                0,
            )
            if image is None:
                return None

            from Quartz import (
                CGImageDestinationCreateWithData,
                CGImageDestinationAddImage,
                CGImageDestinationFinalize,
            )
            from CoreFoundation import CFDataCreateMutable, kCFAllocatorDefault
            import CoreFoundation

            data = CFDataCreateMutable(kCFAllocatorDefault, 0)
            dest = CGImageDestinationCreateWithData(data, "public.png", 1, None)
            if dest is None:
                return None

            CGImageDestinationAddImage(dest, image, None)
            CGImageDestinationFinalize(dest)

            # Convert CFData to bytes
            length = CoreFoundation.CFDataGetLength(data)
            return bytes(CoreFoundation.CFDataGetBytePtr(data)[:length])

        except Exception as exc:
            logger.warning("Screenshot capture via Quartz failed: %s", exc)

        # Fallback: use screencapture command
        try:
            import tempfile
            import os

            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()

            subprocess.run(
                ["screencapture", "-x", tmp.name],
                capture_output=True,
                timeout=10,
            )

            with open(tmp.name, "rb") as f:
                data = f.read()

            os.unlink(tmp.name)
            return data if data else None

        except Exception as exc:
            logger.error("Screenshot capture fallback failed: %s", exc)
            return None
