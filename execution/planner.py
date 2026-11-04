"""
Execution Planner — a configured model plans the entire workflow execution.

One cheap text-only adapter call that:
- Identifies current state from AX tree
- Plans which steps to execute, skip, modify, inject
- Chooses modality per step (AX, JS, AppleScript, coordinate, visual)
- Fills in parameterized data
- Flags steps that need visual mode

No screenshots sent. AX tree as JSON text = cheap and fast.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from anyuse.guides.models import AppGuide
from anyuse.guides.state_graph import StateGraph, UITransition
from anyuse.memory.models import WorkflowTemplate
from anyuse.execution.model_adapter import ModelAdapter
from anyuse.execution.plan_types import ExecutionPlan, PlannedStep

logger = logging.getLogger(__name__)

# PlannedStep and ExecutionPlan are plain dataclasses with no model
# dependency; they live in the dependency-free `execution/plan_types.py`
# (imported above), so importing them never needs a provider SDK, and
# ExecutionPlanner itself talks to `ModelAdapter`
# (httpx only; Cerebras/OpenRouter/local/Gemini are configuration presets, not
# SDKs — see `execution/model_adapter.py`, `docs/MODEL_CONFIGURATION.md`).
# No provider SDK import exists anywhere in this module.


# ---------------------------------------------------------------------------
# ExecutionPlanner
# ---------------------------------------------------------------------------

class ExecutionPlanner:
    """
    Plans workflow execution with ONE provider-agnostic text call.

    Given a task, a workflow template, the current AX tree, and a state
    graph, the planner produces an ExecutionPlan that tells the executor
    exactly what to do, which modality to use, and what to expect.
    """

    def __init__(
        self,
        adapter: Optional[ModelAdapter] = None,
        model: str = "",
    ):
        # Construction is model-free. Credential lookup and the first httpx
        # client allocation happen only inside adapter.complete(), when plan()
        # actually asks a model.
        self._adapter = adapter or ModelAdapter.for_role(
            "planner", **({"model": model} if model else {})
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan(
        self,
        task: str,
        params: dict,
        workflow: WorkflowTemplate,
        current_ax_json: str,
        state_graph: StateGraph,
        perception_context: dict,
        app_guide: Optional[AppGuide] = None,
    ) -> ExecutionPlan:
        """
        One text-only AI call to plan the entire execution.

        Returns an ExecutionPlan with steps, modalities, and reasoning.
        """
        prompt = self._build_prompt(
            task, params, workflow, current_ax_json,
            state_graph, perception_context, app_guide,
        )
        llm_calls = 0

        try:
            response = await self._adapter.acomplete(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                json_schema=_PLAN_SCHEMA,
                schema_name="execution_plan",
            )
            llm_calls += response.llm_calls
            if not response.ok:
                raise RuntimeError(response.error or "planning provider failed")
            plan = self._parse_response(response.text, workflow)
            plan = _steer_away_from_applescript(plan, current_ax_json)
            plan.llm_calls += llm_calls
            return plan

        except Exception as exc:
            logger.error("Execution planning failed: %s", exc)
            # Fallback: produce a plan that replays every workflow step
            # using computer_use modality so the executor can still run.
            plan = self._fallback_plan(workflow, params)
            plan.llm_calls += llm_calls
            return plan

    @staticmethod
    def build_perception_context(
        ax_snapshot,
        app_info: dict,
        omniparser_result=None,
    ) -> dict:
        """
        Build structured perception context for the AI.

        No thresholds — just raw signals.
        """
        from anyuse.context.accessibility import GENERIC_ROLES, ACTIONABLE_ROLES

        ax_element_count = 0
        ax_actionable_elements = 0
        ax_generic_count = 0
        has_canvas = False
        has_web_area = False

        if ax_snapshot is not None:
            elements = getattr(ax_snapshot, "root_elements", [])
            ax_element_count = len(elements)
            for elem in elements:
                if elem.role in ACTIONABLE_ROLES:
                    ax_actionable_elements += 1
                if elem.role in GENERIC_ROLES:
                    ax_generic_count += 1
                if elem.role == "AXCanvas":
                    has_canvas = True
                if elem.role in ("AXWebArea", "AXWebContent"):
                    has_web_area = True

        ax_generic_ratio = (
            ax_generic_count / ax_element_count if ax_element_count > 0 else 0.0
        )

        omni_element_count = 0
        omni_vs_ax_gap = 0
        if omniparser_result is not None:
            omni_element_count = getattr(omniparser_result, "element_count", 0)
            omni_vs_ax_gap = omni_element_count - ax_actionable_elements

        return {
            "ax_element_count": ax_element_count,
            "ax_actionable_elements": ax_actionable_elements,
            "ax_generic_ratio": round(ax_generic_ratio, 3),
            "has_canvas": has_canvas,
            "has_web_area": has_web_area,
            "app_name": app_info.get("app_name", ""),
            "omniparser_element_count": omni_element_count,
            "omniparser_vs_ax_gap": omni_vs_ax_gap,
        }

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        task: str,
        params: dict,
        workflow: WorkflowTemplate,
        ax_json: str,
        state_graph: StateGraph,
        perception_context: dict,
        guide: Optional[AppGuide],
    ) -> str:
        """Build the planning prompt. Must be clear and structured."""

        # Workflow steps as intent list
        intent_lines = []
        for i, step in enumerate(workflow.steps, 1):
            param_note = ""
            if step.input_data:
                resolved = step.input_data
                for k, v in params.items():
                    resolved = resolved.replace(f"${{{k}}}", str(v))
                param_note = f"  input_data={resolved}"
            reliability = ""
            if step.step_attempts > 0:
                rate = step.step_successes / step.step_attempts
                reliability = f"  reliability={rate:.0%} ({step.step_attempts} attempts)"
                if step.last_success_modality:
                    reliability += f"  last_ok_modality={step.last_success_modality}"
            intent_lines.append(
                f"  {i}. intent=\"{step.intent}\" "
                f"target=\"{step.target_element}\" "
                f"type={step.input_type}"
                f"{param_note}{reliability}"
            )

        # State graph context — identify what states match
        state_match_section = ""
        if state_graph and state_graph.states:
            state_match_section = "\nKNOWN STATES IN GRAPH:\n"
            for sid, state in state_graph.states.items():
                available = state_graph.get_available_transitions(sid)
                trans_summary = ", ".join(
                    f'"{t.intent}" -> {t.to_state}' for t in available[:5]
                )
                state_match_section += (
                    f"  - {sid} (app={state.app}, "
                    f"elements={len(state.elements)}, "
                    f"transitions=[{trans_summary}])\n"
                )

        # Available transition implementations for reliability context
        transition_impls = ""
        for t in (state_graph.transitions if state_graph else []):
            if t.implementations:
                impls = []
                for impl in t.implementations:
                    impls.append(
                        f"{impl.modality}(rate={impl.success_rate:.0%}, "
                        f"used={impl.times_used})"
                    )
                transition_impls += (
                    f"  {t.intent} [{t.from_state} -> {t.to_state}]: "
                    f"{', '.join(impls)}\n"
                )

        # Guide context
        guide_section = ""
        if guide:
            guide_section = f"\nAPP GUIDE:\n{guide.get_full_context()[:2000]}\n"

        # Perception signals
        perc = json.dumps(perception_context, indent=2) if perception_context else "{}"

        # AppleScript only for apps that already granted Automation (the executor refuses
        # the rest rather than raise the consent prompt, so offering it would waste the step)
        scriptable = _apps_granting_automation(workflow)
        if scriptable:
            applescript_rule = (
                '"applescript": Use AppleScript — for native app automation (menu clicks, '
                "app-level commands). Address only these apps, which granted Automation: "
                + ", ".join(scriptable) + ".")
        else:
            applescript_rule = (
                '"applescript": NOT AVAILABLE — no app here granted Automation, and an Apple '
                "Event would raise the macOS consent prompt. Never choose it.")

        prompt = f"""You are a workflow execution planner. Given a task, a workflow template, and the current UI state (as an accessibility tree), plan the exact steps to execute.

TASK: {task}

PARAMETERS: {json.dumps(params)}

WORKFLOW TEMPLATE (intent-based steps):
{chr(10).join(intent_lines)}

CURRENT ACCESSIBILITY TREE (JSON):
{ax_json[:15000]}

{state_match_section}

TRANSITION IMPLEMENTATIONS (reliability history):
{transition_impls if transition_impls else "  (no history yet)"}

PERCEPTION SIGNALS:
{perc}
{guide_section}

INSTRUCTIONS:
1. Identify which known state (from the state graph) the current AX tree represents, or describe it if unknown.
2. Determine if navigation is needed to reach the workflow's starting state.
3. For each workflow step, choose the best execution modality:
   - "ax": Use AX API (press buttons, set text values) — PREFERRED when AX tree shows the element clearly.
   - "js": Execute JavaScript in browser — PREFERRED for web apps when JS selectors are known.
   - {applescript_rule}
   - "coordinate": Use pyautogui click/type at pixel coordinates — when position is known and stable.
   - "computer_use": Full screenshot + AI vision — LAST RESORT, expensive and slow.
4. For "ax" modality, provide command as {{"role": "AXButton", "title": "Save", "action": "press"}} or {{"role": "AXTextField", "title": "Search", "action": "set_value"}}. "title" is matched against BOTH the element's "title" AND "ax_description" fields in the accessibility tree above — many controls (e.g. macOS Calculator's buttons) have no title at all and are labelled only by ax_description. When an element's "title" is empty or null, put its "ax_description" value in "title" instead; do not invent a value that appears in neither field.
5. For "js" modality, provide command as {{"script": "document.querySelector(...).click()"}}.
6. For "applescript" modality, provide command as {{"script": "tell application ..."}}. Prefer "ax" over "applescript" whenever the accessibility tree above already shows the target element (e.g. a menu click that the tree exposes as an AXMenuItem) — AppleScript/System Events is for what the AX tree does not expose, not a substitute for a press the "ax" modality already covers.
7. For "coordinate" modality, provide command as {{"x": 500, "y": 300, "action": "click"}} or {{"x": 500, "y": 300, "action": "type", "text": "..."}}.
8. Skip steps that are unnecessary given the current state.
9. Inject navigation steps if the UI is not at the expected starting state.
10. Resolve parameterized input_data using the provided PARAMETERS.
11. Set needs_visual=true ONLY for steps where AX tree is insufficient (canvas elements, custom-rendered UI, complex visual verification).

Respond with JSON in this exact format:
{{
    "current_state": "state_id or description",
    "navigation_needed": ["step description if nav needed"],
    "reasoning": "Why this plan was chosen",
    "steps": [
        {{
            "intent": "what this step does",
            "source": "workflow" or "ai_injected" or "navigation",
            "target_element": "element description",
            "modality": "ax" or "js" or "applescript" or "coordinate" or "computer_use",
            "command": {{}},
            "input_data": null,
            "needs_visual": false,
            "expected_after": "what should change",
            "expected_after_state": null,
            "reasoning": "why this modality was chosen"
        }}
    ],
    "skipped_steps": ["intent: reason"],
    "injected_steps": ["intent: reason"]
}}"""
        return prompt

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self, response_text: str, workflow: WorkflowTemplate
    ) -> ExecutionPlan:
        """Parse AI's JSON response into ExecutionPlan."""
        if not response_text.strip():
            logger.warning("Empty response from planner AI, using fallback")
            return self._fallback_plan(workflow, {})

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse planner JSON: %s", exc)
            # Try to extract JSON from markdown code blocks
            cleaned = response_text.strip()
            if "```json" in cleaned:
                cleaned = cleaned.split("```json", 1)[1]
                cleaned = cleaned.split("```", 1)[0].strip()
            elif "```" in cleaned:
                cleaned = cleaned.split("```", 1)[1]
                cleaned = cleaned.split("```", 1)[0].strip()
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                logger.error("Cannot parse planner response at all, using fallback")
                return self._fallback_plan(workflow, {})

        steps = []
        for step_data in data.get("steps", []):
            steps.append(PlannedStep.from_dict(step_data))

        return ExecutionPlan(
            current_state=data.get("current_state", ""),
            navigation_needed=data.get("navigation_needed", []),
            steps=steps,
            skipped_steps=data.get("skipped_steps", []),
            injected_steps=data.get("injected_steps", []),
            reasoning=data.get("reasoning", ""),
        )

    # ------------------------------------------------------------------
    # Fallback plan
    # ------------------------------------------------------------------

    def _fallback_plan(
        self, workflow: WorkflowTemplate, params: dict
    ) -> ExecutionPlan:
        """
        Generate a fallback plan when the AI call fails.

        Converts each workflow intent into a computer_use step so
        the executor can still attempt execution via full AI vision.
        """
        steps = []
        for wf_step in workflow.steps:
            resolved_input = wf_step.input_data
            if resolved_input and params:
                for k, v in params.items():
                    resolved_input = resolved_input.replace(f"${{{k}}}", str(v))

            steps.append(PlannedStep(
                intent=wf_step.intent,
                source="workflow",
                target_element=wf_step.target_element,
                modality="computer_use",
                command={"description": wf_step.intent},
                input_data=resolved_input,
                needs_visual=True,
                expected_after=wf_step.expected_after,
                expected_after_state=wf_step.expected_after_state or None,
                reasoning="Fallback: AI planning failed, using computer_use for all steps",
            ))

        return ExecutionPlan(
            current_state="unknown",
            navigation_needed=[],
            steps=steps,
            skipped_steps=[],
            injected_steps=[],
            reasoning="Fallback plan: AI planning call failed, all steps use computer_use modality",
        )


# In a live TextEdit measurement the planner
# chose "applescript" for a plain File > Save menu click in all 3 runs
# (`click menu item "Save" of menu "File" of menu bar 1 of process
# "TextEdit"` via System Events), and every one timed out in the guest
# (~30 s each) — most of the measured "act"
# latency was that timeout, not real work. The prompt instruction above asks
# the model to prefer "ax"; this is the deterministic backstop for the exact,
# narrow case a script can be proven equivalent to without interpreting
# AppleScript at all: a bare "click menu item "X" of menu "Y" ..." command
# where an AXMenuItem titled X is ALREADY present in the AX tree the plan was
# built from. Anything else about the script is left alone. The rewritten
# step still passes through `_execute_ax`'s `_resolve_target()` at
# execution time exactly like any other "ax" step — this only changes which
# modality is tried, never what is allowed to actuate.
_APPLESCRIPT_MENU_CLICK_RE = re.compile(
    r'click\s+menu item\s+"([^"]+)"\s+of\s+menu\s+"[^"]*"', re.IGNORECASE)


def _ax_menu_item_titles(ax_dump: Any) -> set:
    """Every AXMenuItem title present in a raw AX-tree JSON structure (the
    same shape ``current_ax_json`` carries into the prompt) — used only to
    decide whether a step already has a matching "ax" target; never to
    resolve or actuate anything itself."""
    out: set = set()

    def walk(node):
        if isinstance(node, dict):
            # the raw dump ({"role": "AXMenuItem", "title": ...}) and the compact
            # AXSnapshot.to_json() form the orchestrator actually passes
            # ({"r": "MenuItem", "t": ...}); reading only the first made this
            # steer a no-op on the product path
            if node.get("role") == "AXMenuItem" and node.get("title"):
                out.add(node["title"])
            elif node.get("r") in ("MenuItem", "AXMenuItem") and node.get("t"):
                out.add(node["t"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(ax_dump)
    return out


#: System Events carries the menu-click scripts; its consent is separate from the app's
_SYSTEM_EVENTS = ("System Events", "com.apple.systemevents")


def _apps_granting_automation(workflow: WorkflowTemplate) -> List[str]:
    """The names of the workflow's app and System Events when each has already granted this
    process Automation, read without asking (``context.macos.automation_status``)."""
    app = str(getattr(workflow, "required_app", "") or "")
    bundle_id = str(getattr(workflow, "required_bundle_id", "") or "")
    try:
        from anyuse.context import macos
        if app and not bundle_id:
            bundle_id = macos.bundle_resolver(app) or ""
        granted = []
        for name, bid in ((app or bundle_id, bundle_id), _SYSTEM_EVENTS):
            if bid and macos.automation_status(bid) == "granted" and name not in granted:
                granted.append(name)
        return granted
    except Exception as exc:  # noqa: BLE001 - no reading, no AppleScript
        logger.debug("Automation consent not read: %s", exc)
        return []


def _steer_away_from_applescript(plan: ExecutionPlan, current_ax_json: str) -> ExecutionPlan:
    """Rewrite an "applescript" step to "ax" IN PLACE, only when its script is
    exactly a plain menu-item click and that exact title is already an
    AXMenuItem in the AX tree the plan was built from. A script that does
    anything else (a non-menu command, a compound script, a title with no
    live AXMenuItem match) is left completely unchanged."""
    try:
        ax_dump = json.loads(current_ax_json) if isinstance(current_ax_json, str) else current_ax_json
    except (TypeError, ValueError):
        return plan
    menu_titles = _ax_menu_item_titles(ax_dump)
    if not menu_titles:
        return plan
    for step in plan.steps:
        if step.modality != "applescript":
            continue
        script = (step.command or {}).get("script") or ""
        match = _APPLESCRIPT_MENU_CLICK_RE.search(script)
        if not match:
            continue
        title = match.group(1)
        if title not in menu_titles:
            continue
        step.modality = "ax"
        step.command = {"role": "AXMenuItem", "title": title, "action": "press"}
        step.reasoning = (
            f"{step.reasoning} [steered from applescript to ax: an AXMenuItem "
            f"titled {title!r} is already in the AX tree — D19]"
        ).strip()
    return plan


# Every provider's `complete()` call sets
# ``"strict": True`` unconditionally (execution/model_adapter.py). OpenAI's
# strict structured-output mode — which Cerebras enforces exactly, found the
# first time this schema was sent to a real strict-mode provider —
# requires every object node, recursively, to declare its own
# closed ``properties``/``additionalProperties: false``; a free-form
# ``{"type": "object"}`` for ``command`` (needed since AX/JS/AppleScript/
# coordinate commands each carry different fields) is rejected outright:
# "Object fields require at least one of: 'properties' or 'anyOf'". The
# closed union below is the fix: one fixed set of nullable fields covering
# every modality's command shape. ``selector`` is deliberately NOT one of
# them — a freshly *planned* step never carries a prior recording to select
# by; that field exists only on the REPLAY path
# (execution/deterministic.py's impl_to_step), which never goes through this
# schema at all.
_COMMAND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["role", "title", "action", "script", "x", "y", "text"],
    "properties": {
        "role": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "action": {"type": ["string", "null"]},
        "script": {"type": ["string", "null"]},
        "x": {"type": ["number", "null"]},
        "y": {"type": ["number", "null"]},
        "text": {"type": ["string", "null"]},
    },
}

_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "current_state", "navigation_needed", "reasoning", "steps",
        "skipped_steps", "injected_steps",
    ],
    "properties": {
        "current_state": {"type": "string"},
        "navigation_needed": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "intent", "source", "target_element", "modality", "command",
                    "input_data", "needs_visual", "expected_after",
                    "expected_after_state", "reasoning",
                ],
                "properties": {
                    "intent": {"type": "string"},
                    "source": {"type": "string"},
                    "target_element": {"type": "string"},
                    "modality": {"type": "string"},
                    "command": _COMMAND_SCHEMA,
                    "input_data": {"type": ["string", "null"]},
                    "needs_visual": {"type": "boolean"},
                    "expected_after": {"type": "string"},
                    "expected_after_state": {"type": ["string", "null"]},
                    "reasoning": {"type": "string"},
                },
            },
        },
        "skipped_steps": {"type": "array", "items": {"type": "string"}},
        "injected_steps": {"type": "array", "items": {"type": "string"}},
    },
}
