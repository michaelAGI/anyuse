"""
Visual Step Resolver — the planner-path twin of intervention._visual_intervention.

The planner can emit ``computer_use`` steps deliberately (needs_visual) and for
the ENTIRE fallback plan when AI planning fails (planner._fallback_plan sets every
step to modality='computer_use'). When such a step has no deterministic x/y
locator, the executor needs SOMETHING to turn "the screen" into a coordinate
action. That is this module's only job.

VisualStepResolver is pure perception + planning:

    capture screenshot  ->  configured vision model  ->  parse JSON  ->  coordinate command

It NEVER actuates. It produces a normalized command dict that the executor then
runs THROUGH the pre-existing _execute_coordinate path (which is itself gated by
ANYUSE_DRIVE_ENABLED in the executor). No mouse/keyboard is ever touched here.

This reuses the proven idiom of intervention.InterventionHandler._visual_intervention
(prompt wording and tolerant JSON parser) but adds two injectable seams —
``screenshot_capture`` and ``adapter`` — so the whole
capture->model->parse loop is unit-testable without a real screen or network.
"""
import asyncio
import json
import logging
from typing import Optional

from anyuse.execution.model_adapter import ModelAdapter, image_data_url
from anyuse.execution.plan_types import PlannedStep

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VisualStepResolver
# ---------------------------------------------------------------------------

class VisualStepResolver:
    """
    Resolve a ``computer_use`` planned step into a coordinate command using the
    computer-use vision model.

    Side-effect-free: it captures the screen, asks the model where to act, and
    parses the answer into a command. It does NOT click/type — actuation is the
    caller's responsibility (and is gated there).
    """

    def __init__(
        self,
        adapter: Optional[ModelAdapter] = None,
        vision_model: str = "",
        screenshot_capture=None,
        pixel_scale: Optional[float] = None,
    ):
        """
        Args:
            adapter: provider-agnostic adapter; defaults to the ``visual`` role.
            vision_model: optional model-name override for that role.
            screenshot_capture: optional injectable with a ``.capture() -> bytes``
                method (defaults to a fresh ScreenshotCapture at resolve time).
                Injected by tests with a fake so no real screen is captured.
            adapter is injectable in tests with an in-process fake transport.
        """
        self._screenshot_capture = screenshot_capture
        # The model returns SCREENSHOT-PIXEL coords; the executor's coordinate path
        # uses LOGICAL POINTS. Convert at this boundary (Retina-safe). Injectable for
        # deterministic tests.
        self._pixel_scale = pixel_scale

        self._adapter = adapter or ModelAdapter.for_role(
            "visual", **({"model": vision_model} if vision_model else {})
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(self, step: PlannedStep, ax_json: str = "") -> dict:
        """
        Perceive the screen and PLAN the next coordinate action for ``step``.

        Returns a normalized dict (never actuates):
            {"ok": bool, "command": {"x", "y", "action", "text"?},
             "reasoning": str, "error"?: str}
        """
        # 1. Capture the screen (injectable; defaults to a real ScreenshotCapture).
        capture = self._screenshot_capture
        if capture is None:
            from anyuse.context.screenshot import ScreenshotCapture
            capture = ScreenshotCapture()

        try:
            png = await asyncio.to_thread(capture.capture)
        except Exception as exc:
            logger.error("Visual resolver screenshot capture failed: %s", exc)
            return {
                "ok": False,
                "command": {},
                "reasoning": "",
                "error": f"Could not capture screenshot: {exc}",
            }

        if not png:
            return {
                "ok": False,
                "command": {},
                "reasoning": "",
                "error": "Screenshot capture returned empty result",
            }

        # 2. Build a vision prompt (reuses intervention._visual_intervention wording).
        prompt = self._build_prompt(step, ax_json)

        # 3. Ask the computer-use model where to act.
        try:
            response = await self._adapter.acomplete(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                json_schema=_VISUAL_SCHEMA,
                schema_name="visual_step",
                images=[image_data_url(png)],
            )
            if not response.ok:
                return {
                    "ok": False,
                    "command": {},
                    "reasoning": "",
                    "llm_calls": response.llm_calls,
                    "provider_unreachable": response.provider_unreachable,
                    "error": f"Vision model call failed: {response.error}",
                }
        except Exception as exc:
            logger.error("Visual resolver model call failed: %s", exc)
            return {
                "ok": False,
                "command": {},
                "reasoning": "",
                "llm_calls": 0,
                "error": f"Vision model call failed: {exc}",
            }

        response_text = response.text
        parsed = self._parse_response(response_text)

        # 4. Normalize to a coordinate command.
        command = parsed.get("command") or {}
        reasoning = parsed.get("reasoning", "")

        if not isinstance(command, dict) or "x" not in command or "y" not in command:
            return {
                "ok": False,
                "command": {},
                "reasoning": reasoning,
                "llm_calls": response.llm_calls,
                "error": (
                    "Vision model did not return a coordinate command: "
                    f"{response_text[:200]}"
                ),
            }

        # Default action to a click if the model omitted it.
        command.setdefault("action", "click")

        # Convert SCREENSHOT-PIXEL coords → LOGICAL POINTS so _execute_coordinate
        # (pyautogui, points) clicks the right place on Retina. Mirrors
        # AgentOrchestrator's vision-boundary conversion.
        try:
            from anyuse.context.coordinates import px_to_pt, screen_scale
            scale = self._pixel_scale if self._pixel_scale is not None else screen_scale()
            cx, cy = px_to_pt(command["x"], command["y"], scale=scale)
            command["x"], command["y"] = int(round(cx)), int(round(cy))
        except Exception as exc:
            logger.debug("visual resolver px→pt conversion skipped: %s", exc)

        return {
            "ok": True,
            "command": command,
            "reasoning": reasoning,
            "llm_calls": response.llm_calls,
        }

    # ------------------------------------------------------------------
    # Prompt construction (reuses intervention._visual_intervention wording)
    # ------------------------------------------------------------------

    def _build_prompt(self, step: PlannedStep, ax_json: str) -> str:
        """Build the vision prompt. Mirrors intervention._visual_intervention."""
        return f"""A workflow step needs visual help to execute.

STEP:
  Intent: {step.intent}
  Target element: {step.target_element}
  Expected: {step.expected_after}

CURRENT AX TREE (may be incomplete):
{ax_json[:8000]}

Look at the screenshot and determine how to execute this step.
Respond with JSON:
{{
    "action": "coordinate_command" | "skip" | "cannot_fix",
    "command": {{}},
    "reasoning": "what you see and how to act"
}}

For coordinate_command, provide pixel coordinates from the screenshot:
{{"x": <pixel_x>, "y": <pixel_y>, "action": "click", "text": "<optional text>"}}"""

    # ------------------------------------------------------------------
    # Response parsing (same tolerant idiom as intervention._parse_intervention_response)
    # ------------------------------------------------------------------

    def _parse_response(self, response: str) -> dict:
        """Parse the model's response from JSON, tolerating markdown fences."""
        if not response.strip():
            return {"action": "cannot_fix", "reasoning": "Empty model response"}

        try:
            return json.loads(response)
        except json.JSONDecodeError:
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
                logger.warning("Cannot parse visual resolver response: %s", response[:200])
                return {
                    "action": "cannot_fix",
                    "reasoning": f"Failed to parse model response: {response[:200]}",
                }


_VISUAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "command", "reasoning"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["coordinate_command", "skip", "cannot_fix"],
        },
        "command": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "action": {"type": "string"},
                "text": {"type": "string"},
            },
        },
        "reasoning": {"type": "string"},
    },
}
