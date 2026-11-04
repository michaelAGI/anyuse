"""
State Graph Executor — Executes plans through the state graph.

Multi-modal execution:
- AX API: press buttons, set text field values (via pyobjc)
- JavaScript: execute JS in browser (via AppleScript)
- AppleScript: native app control
- Coordinate: pyautogui click/type (existing actions layer)
- Computer Use: full screenshot -> AI (last resort)

Per-step verification via AX diff (fast, free) or AI (when needed).
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from anyuse.execution.outcome import (
    REFUSED,
    STOP_KINDS,
    classify_outcome,
    refusal,
)
from anyuse.execution.plan_types import ExecutionPlan, PlannedStep
from anyuse.guides.state_graph import (
    StateGraph,
    TransitionImpl,
)
from anyuse.memory.models import WorkflowTemplate
from anyuse.context.accessibility import (
    AXDiff,
    AXSnapshot,
    AccessibilityTree,
)

logger = logging.getLogger(__name__)

#: how long a drifted pre-step verdict is re-read while the screen settles (a window opening)
DRIFT_SETTLE_S = 2.0
DRIFT_SETTLE_POLL_S = 0.25

# `from __future__ import annotations` (above) makes every ``PlannedStep`` /
# ``ExecutionPlan`` type hint in this module a lazy string — this class never
# needed the real objects to construct or dispatch a step, only to name their
# shape. Both names come from the dependency-free `execution/plan_types.py`,
# not from `anyuse.execution.planner`, so `import anyuse.execution.executor`
# (StateGraphExecutor, the PRODUCT's own step dispatcher) needs no model SDK
# to be importable, on a machine with none installed.


def _visual_drive_enabled() -> bool:
    """The keystone safety gate for the planner-path visual tier: True only when
    ``ANYUSE_DRIVE_ENABLED`` is explicitly truthy. Default OFF — uses the exact
    truthy set as the drive seam's gate. NO capture/model/actuation happens
    in _execute_visual unless this returns True.
    """
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_DRIVE_ENABLED"))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """Result of executing a single planned step."""

    success: bool = False
    method: str = ""
    skipped: bool = False
    error: Optional[str] = None
    ax_diff_summary: Optional[str] = None
    new_state: Optional[str] = None
    reasoning: str = ""
    llm_calls: int = 0
    #: the step's outcome kind (execution/outcome.py) when it STOPPED the run
    #: (a refusal, an unobserved or deviating effect); "" otherwise.
    outcome_kind: str = ""

    def to_dict(self) -> dict:
        d = {
            "success": self.success,
            "method": self.method,
            "skipped": self.skipped,
            "error": self.error,
            "ax_diff_summary": self.ax_diff_summary,
            "new_state": self.new_state,
            "reasoning": self.reasoning,
            "llm_calls": self.llm_calls,
        }
        if self.outcome_kind:
            d["outcome_kind"] = self.outcome_kind
        return d


@dataclass
class ExecutionResult:
    """Result of executing an entire plan."""

    steps: List[StepResult] = field(default_factory=list)
    success: bool = False
    duration_seconds: float = 0.0
    states_discovered: List[str] = field(default_factory=list)
    transitions_discovered: int = 0
    intervention_guidance: Optional[dict] = None
    # Set when intervention fails and returns guidance_needed=True.
    # Contains structured context about the blockage for MCP signal.
    guidance_from_steps: Optional[dict] = None
    llm_calls: int = 0
    # Aggregated guidance from step-level intervention failures.
    #: why the run STOPPED (a step's outcome kind in outcome.STOP_KINDS and its
    #: reason) - a deviation stops, it does not ask
    stopped: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "steps": [s.to_dict() for s in self.steps],
            "success": self.success,
            "duration_seconds": round(self.duration_seconds, 2),
            "states_discovered": self.states_discovered,
            "transitions_discovered": self.transitions_discovered,
            "llm_calls": self.llm_calls,
        }
        if self.stopped is not None:
            d["stopped"] = self.stopped
        if self.intervention_guidance is not None:
            d["intervention_guidance"] = self.intervention_guidance
        if self.guidance_from_steps is not None:
            d["guidance_from_steps"] = self.guidance_from_steps
        return d


# ---------------------------------------------------------------------------
# StateGraphExecutor
# ---------------------------------------------------------------------------

# The platform verb map: EVERY AX action the executor
# performs, and how, in one table both dispatch branches read. ``verb`` is the
# abstract ``osplatform.Verb`` a Platform performs (None: the action has no
# platform verb and always runs through the inline handler, i.e. the gated
# ``AccessibilityTree`` primitive that does the real work); ``native`` is the AX
# action the inline branch sends for a plain action ("" for an action with its
# own handler). Before this table the platform branch mapped anything it did not
# know - ``enter_text``, ``press_key``, ``insert_text``, ``replace_range``,
# ``raise`` - to ``invoke``, i.e. AXPress, and reported success. An action that is
# not in the table is a typed refusal on both branches, never a press.
AX_ACTIONS = {
    "press": {"verb": "invoke", "native": "AXPress"},
    "toggle": {"verb": "toggle", "native": "AXPress"},
    "expand": {"verb": "expand", "native": "AXPress"},
    "confirm": {"verb": "confirm", "native": "AXConfirm"},
    "cancel": {"verb": "cancel", "native": "AXCancel"},
    "increment": {"verb": "increment", "native": "AXIncrement"},
    "decrement": {"verb": "decrement", "native": "AXDecrement"},
    "show_menu": {"verb": "show_menu", "native": "AXShowMenu"},
    "set_value": {"verb": "set_value", "native": ""},
    "raise": {"verb": None, "native": "AXRaise"},
    "enter_text": {"verb": None, "native": ""},
    "insert_text": {"verb": None, "native": ""},
    "replace_range": {"verb": None, "native": ""},
    "press_key": {"verb": None, "native": ""},
    "key": {"verb": None, "native": ""},
    # a read-only step: the target is resolved and identity-checked like any
    # other, then its value is READ, never pressed
    "read": {"verb": None, "native": ""},
}

# The runner contract for the map (execution/ax_actions.py):
# select_row, append_text, replace_line and open_app - no platform verb, each runs its own
# handler in ``_ax_inline``; ``open_app`` acts on the app, not on a resolved element
# (``TARGETLESS``). ``AX_SELECTOR_KINDS``: selector kinds ``_resolve_target`` resolves by their
# own rule instead of a recorded identity (``focused``).
from anyuse.execution import ax_actions as _AXC  # noqa: E402

AX_ACTIONS.update(_AXC.AX_ACTIONS)
AX_SELECTOR_KINDS = _AXC.SELECTOR_KINDS

#: Back-compat view: recorder action -> platform verb, for the actions that have one.
_ACTION_TO_VERB = {a: v["verb"] for a, v in AX_ACTIONS.items() if v["verb"]}


def _same_identity(recorded, live) -> bool:
    """Is ``live`` (a :class:`context.accessibility.Selector` built from a just-
    resolved candidate) the SAME element ``recorded`` (the stored Selector) was
    made from?

    This ports `execution/guest/flows.py`'s ``Replayer.resolve()`` / ``same_identity()``
    — the check the VM harness applies before every actuation — to the
    product's own ``Selector`` model. The harness's identity also carries a menu
    item's command-character/modifiers; ``Selector`` does not model those, so the
    fields compared here are the ones both sides actually have: canonical role,
    AXIdentifier, name (AXTitle/AXDescription/AXValue) and role-path, in that
    order of trust.

    Role must always agree. An identifier on either side is decisive — it never
    falls through to a weaker check that could paper over a mismatch. With no
    identifier on either side, the (already resolved) name must match exactly.
    With neither an identifier nor a name on either side, only an identical
    role-path counts as the same element; "same role, nothing else" — which is
    exactly what let ``_resolve_target()`` return "the first element of this
    role" — is never accepted as a match.
    """
    if recorded.canonical_role != live.canonical_role:
        return False
    # where the element lives is part of what it is. Compared only when
    # both sides know it (a selector recorded before these fields carries
    # neither); the executor separately REQUIRES both, right before acting,
    # whenever the recording has them (``_app_document_refusal``).
    if recorded.app and live.app and recorded.app != live.app:
        return False
    if recorded.document and live.document:
        from anyuse.context.accessibility import normalize_document
        if normalize_document(recorded.document) != normalize_document(live.document):
            return False
    # a menu element's menu titles are part of what it is (a namesake in another submenu,
    # or the menu-bar item named like a menu item, is another element)
    rec_menu, live_menu = getattr(recorded, "menu_path", None), getattr(live, "menu_path", None)
    if rec_menu is not None and live_menu is not None and list(rec_menu) != list(live_menu):
        return False
    # so is the container a document-less window shows (another folder: another element)
    if getattr(recorded, "scope", None) and getattr(live, "scope", None):
        from anyuse.context.container_scope import DIFFERENT, same_scope
        if same_scope(recorded.scope, live.scope) == DIFFERENT:
            return False
    if not recorded.identifier and live.identifier:
        # a map hop pinned to a menu place matches the live menu element there by its name, even when AppKit gave
        # that element a generated ``_NS:<n>`` id (the menu paths were compared above)
        from anyuse.context.accessibility import menu_place_identity
        if menu_place_identity(recorded, live.identifier, live.name, live_menu):
            return True
    if recorded.identifier or live.identifier:
        if not (bool(recorded.identifier) and recorded.identifier == live.identifier):
            return False
        # an identifier the recording shared with other elements is not an identity
        if (getattr(recorded, "shared_id", None) or 0) > 0 and recorded.name and live.name != recorded.name:
            return False
        return True
    if recorded.name or live.name:
        return bool(recorded.name) and recorded.name == live.name
    return bool(recorded.path) and recorded.path == live.path


class StateGraphExecutor:
    """
    Executes an ExecutionPlan step by step through the state graph.

    Each step is executed using the planned modality (AX, JS, AppleScript,
    coordinate, or full AI vision). After each step the AX tree is diffed
    to verify success cheaply before falling back to AI verification.
    """

    def __init__(
        self,
        action_executor=None,
        ax_tree: Optional[AccessibilityTree] = None,
        macos_context=None,
        environment=None,
        intervention_handler=None,
        visual_resolver=None,
        drift_gate_enabled: bool = False,
        hil_gate=None,
        platform=None,
        visual_verifier=None,
        screenshot_capture=None,
        api_executor=None,
        rea_store=None,
        rea_ui_trace=None,
        rea_front_reader=None,
    ):
        """
        Args:
            action_executor: ActionExecutor from actions/executor.py for
                coordinate-based and browser actions.
            ax_tree: AccessibilityTree instance for AX API actions and
                snapshot capture.
            macos_context: MacOSContext instance for JS injection and
                system info.
            environment: ReplayEnvironment instance for environment
                preparation/cleanup.
            intervention_handler: InterventionHandler for AI-assisted
                fix when steps fail (optional).
            visual_resolver: VisualStepResolver for the planner-path
                ``computer_use`` modality (optional). Defaults to None so the
                gated path lazily constructs one; tests inject a fake.
            drift_gate_enabled: Opt-in pre-step drift check. When False (the
                default), execution is byte-for-byte the prior behavior — no
                drift comparison happens and existing callers are unchanged.
                When True, a deterministic step (ax/js/coordinate) carrying an
                ``expected_before_state`` has its live AX compared to the
                recorded baseline; ONLY on a real mismatch is that single step
                escalated to the (gated, non-actuating) visual tier. PURE
                analysis — no new capture/actuation site, no gate change.
            hil_gate: Optional :class:`anyuse.execution.hil_gate.HILGate` —
                the pause-before-destructive hold consulted BEFORE a step is
                dispatched to its modality actuator. Default ``None`` →
                completely inert: ``_execute_step`` behaves byte-for-byte as
                before. When a gate IS injected it only ever holds when BOTH the
                master ``ANYUSE_DRIVE_ENABLED`` gate is on AND the gate's
                ``ANYUSE_HIL_MODE`` is active (``destructive``/``all``); with HIL
                mode ``off`` (the default) the gate is consulted but always
                returns actuate=True, so existing behavior is preserved. The
                gate ONLY decides — it never actuates; on a hold the step is
                returned blocked WITHOUT calling the actuator (read-only steps,
                e.g. ax reads / screenshots, are never held).
            api_executor: Optional
                :class:`anyuse.execution.api_step.ApiStepExecutor` used by the
                COMPILED ``api`` rung. Default ``None`` → one is constructed on
                first use (and inherits the ActionExecutor's BOUNDARY checker).
                Tests and the benchmark inject a fake/stub transport here to
                exercise the fall-back without a network.
        """
        self._action_executor = action_executor
        self._ax_tree = ax_tree
        self._macos_context = macos_context
        self._environment = environment
        self._intervention_handler = intervention_handler
        self._visual_resolver = visual_resolver
        self._drift_gate_enabled = drift_gate_enabled
        self._hil_gate = hil_gate
        # osplatform.Platform — when injected, AX actuation routes through its gated,
        # cross-platform perform_verb (closes the gap where AX clicks fired
        # regardless of ANYUSE_DRIVE_ENABLED). None → legacy inline AX dispatch.
        self._platform = platform
        # VisualVerifier (+ a screenshot capture) — the no-LLM OCR/pixel tier the
        # verify path falls to when the AX diff is inconclusive.
        self._visual_verifier = visual_verifier
        self._screenshot_capture = screenshot_capture
        # ApiStepExecutor for the COMPILED "api" rung — injected by tests/the
        # benchmark, else constructed lazily on first use (see _execute_api).
        # None here changes nothing: a workflow with no compiled implementation
        # never reaches that branch.
        self._api_executor = api_executor
        # Trusted application injection retains fresh restoration providers.
        self._rea_store = rea_store
        # Only physical GUI actions use this around-actuation capture. Native,
        # AX and API implementations keep their own evidence contracts.
        self._rea_ui_trace = rea_ui_trace
        self._rea_front_reader = rea_front_reader

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        plan: ExecutionPlan,
        workflow: WorkflowTemplate,
        state_graph: StateGraph,
    ) -> ExecutionResult:
        """
        Execute the plan step by step.

        For each step:
        1. Take before-AX snapshot
        2. Execute action using planned modality
        3. Take after-AX snapshot
        4. Verify: AX diff fast path, or AI if needed
        5. If failed: call intervention handler
        6. Update state graph with results
        """
        start_time = time.time()
        result = ExecutionResult()
        all_succeeded = True
        succeeded_count_so_far = 0

        for step_idx, step in enumerate(plan.steps):
            logger.info(
                "Step %d/%d: [%s] %s (modality=%s)",
                step_idx + 1, len(plan.steps),
                step.source, step.intent, step.modality,
            )

            # 1. Take before snapshot
            before_ax = self._take_snapshot()

            # 1b. Optional pre-step drift gate (opt-in; default off preserves
            # prior behavior exactly). For a DETERMINISTIC step (ax/js/coordinate)
            # that carries a recorded baseline, compare the live AX to that
            # baseline. If — and ONLY if — the UI has actually drifted, escalate
            # this single step to the (gated, non-actuating) visual tier. No
            # drift => keep the cheap planned modality (the token-saver).
            eff_step = step
            drift_note: Optional[str] = None
            if self._drift_gate_enabled:
                eff_step, drift_note, before_ax = await self._settled_drift_gate(
                    step, step_idx, workflow, state_graph, before_ax
                )

            # 2. Execute (effective step may have been escalated by the gate)
            exec_result = await self._execute_step(eff_step)

            # Brief pause to let UI settle
            await asyncio.sleep(0.3)

            # 3. Take after snapshot
            after_ax = self._take_snapshot()

            # 4. Verify. A declared expected effect decides the step's
            # success (it can turn an actuator's success into a failure).
            verification = self._verify_step(eff_step, before_ax, after_ax)
            exec_result = self._apply_effect(eff_step, exec_result, before_ax, after_ax,
                                             verification, state_graph)
            step_result = StepResult(
                method=exec_result.get("method", eff_step.modality),
                ax_diff_summary=verification.get("ax_diff_summary"),
                llm_calls=int(exec_result.get("llm_calls", exec_result.get("model_calls", 0)) or 0),
            )
            result.llm_calls += step_result.llm_calls
            if exec_result.get("success", False):
                # Check if AX diff clearly shows expected change
                if eff_step.modality == "rea":
                    step_result.success = True
                    tier = exec_result.get("verification_tier", "deterministic")
                    step_result.reasoning = f"Registered REA execution completed ({tier} verification)"
                elif verification.get("clearly_matched", False):
                    step_result.success = True
                    step_result.reasoning = "AX diff confirms expected change"
                elif verification.get("has_changes", False):
                    # Changes happened but we cannot confirm the exact expected
                    # outcome from AX diff alone — treat as success with caveat
                    step_result.success = True
                    step_result.reasoning = (
                        "Action executed; AX diff shows changes but "
                        "could not confirm exact expected outcome"
                    )
                else:
                    # No AX changes detected — might be OK (e.g. navigation)
                    # or might indicate failure
                    step_result.success = True
                    step_result.reasoning = (
                        "Action executed; no AX changes detected "
                        "(may be expected for this action type)"
                    )
            else:
                step_result.success = False
                step_result.error = exec_result.get("error", "Unknown error")
                step_result.reasoning = f"Execution failed: {step_result.error}"

            # A STOP kind (a refusal, an unobserved or deviating effect, a
            # policy hold) ends the run here, with its reason: no intervention
            # model is asked to "fix" it and no later step runs on a screen the
            # product could not confirm (a deviation stops, it does not
            # ask).
            kind = classify_outcome(exec_result)
            stop_kind = kind if (not step_result.success and kind in STOP_KINDS) else ""
            if stop_kind:
                step_result.outcome_kind = stop_kind
                step_result.reasoning = f"stopped ({stop_kind}): {step_result.error}"

            if drift_note:
                # Keep WHY this step was escalated to vision visible alongside
                # the execution outcome (the drift gate fired before execute).
                step_result.reasoning = (
                    f"{drift_note}; {step_result.reasoning}"
                    if step_result.reasoning
                    else drift_note
                )

            # 5. If failed and we have an intervention handler, try to fix
            if (not step_result.success and not stop_kind
                    and self._intervention_handler is not None):
                logger.info("Step %d failed, attempting intervention...", step_idx + 1)
                try:
                    current_ax_json = self._get_current_ax_json()
                    from anyuse.execution.planner import ExecutionPlanner
                    perception_context = ExecutionPlanner.build_perception_context(
                        after_ax, {"app_name": workflow.required_app}
                    ) if after_ax else {}

                    # an AX fix runs through THIS executor's checked path
                    # (app-bound tree, identity, ambiguity, approval), never on
                    # the frontmost app's first match.
                    if hasattr(self._intervention_handler, "bind_executor"):
                        self._intervention_handler.bind_executor(self)
                    intervention = await self._intervention_handler.intervene(
                        failed_step=step,
                        current_ax_json=current_ax_json,
                        state_graph=state_graph,
                        perception_context=perception_context,
                        verification_context=verification,
                    )
                    intervention_calls = int(getattr(intervention, "llm_calls", 0) or 0)
                    step_result.llm_calls += intervention_calls
                    result.llm_calls += intervention_calls

                    if intervention.success:
                        step_result.success = True
                        step_result.method = intervention.modality or step.modality
                        step_result.reasoning = (
                            f"Fixed by intervention: {intervention.reasoning}"
                        )
                        # Record new implementation in graph
                        if intervention.new_implementation:
                            current_state = plan.current_state or "unknown"
                            next_state = step.expected_after_state or "unknown"
                            state_graph.add_implementation(
                                from_state=current_state,
                                to_state=next_state,
                                intent=step.intent,
                                impl=intervention.new_implementation,
                            )
                            result.transitions_discovered += 1
                    else:
                        step_result.reasoning = (
                            f"Intervention failed: {intervention.reasoning}"
                        )
                        # Capture guidance context if intervention needs guidance
                        if intervention.guidance_needed and intervention.guidance_context:
                            guidance = intervention.guidance_context.copy()
                            guidance["partial_results"] = {
                                "steps_completed": succeeded_count_so_far,
                                "steps_remaining": len(plan.steps) - step_idx - 1,
                            }
                            result.intervention_guidance = guidance
                except Exception as exc:
                    logger.error("Intervention error: %s", exc)
                    step_result.reasoning = f"Intervention error: {exc}"

            # 6. Update state graph with results
            self._update_graph(state_graph, before_ax, after_ax, step, step_result)

            # Track succeeded steps for guidance context
            if step_result.success:
                succeeded_count_so_far += 1

            # Track discovered states
            if step_result.new_state and step_result.new_state not in result.states_discovered:
                result.states_discovered.append(step_result.new_state)

            result.steps.append(step_result)

            if stop_kind:
                all_succeeded = False
                result.stopped = {"step": step_idx, "outcome_kind": stop_kind,
                                  "reason": step_result.error,
                                  "reason_code": exec_result.get("reason_code")}
                logger.warning("Step %d stopped the run (%s): %s",
                               step_idx + 1, stop_kind, step_result.error)
                break

            if not step_result.success:
                all_succeeded = False
                # Continue to next step — we don't abort on individual failures
                # unless the step was critical (non-optional in workflow)
                wf_step = None
                if step_idx < len(workflow.steps):
                    wf_step = workflow.steps[step_idx] if step.source == "workflow" else None
                if wf_step and not wf_step.optional:
                    logger.warning(
                        "Required step %d failed, stopping execution", step_idx + 1
                    )
                    break

        result.success = all_succeeded
        result.duration_seconds = time.time() - start_time
        return result

    # ------------------------------------------------------------------
    # HIL pause-before-destructive hold (default-inert pre-dispatch gate)
    # ------------------------------------------------------------------

    @staticmethod
    def _step_to_hil_action(step: PlannedStep) -> dict:
        """Project a :class:`PlannedStep` into the loosely-typed action shape
        ``hil_gate.classify_step`` consumes (``{"action": <verb>, "args": {...}}``).

        The classifier keys off the action NAME plus every embedded string arg
        (it reuses ``ComplianceChecker._iter_strings``), so a destructive keyword
        in a button title / set_value / script / typed text is caught. We pick a
        representative verb per modality and surface the command's human-facing
        strings (role/title/value/script/text/keys) as args so e.g. an ax
        ``press`` on a "Delete account" button classifies destructive, while an
        ax ``read``/screenshot stays read-only. PURE — builds a dict only; it
        touches no OS layer and actuates nothing.
        """
        cmd = step.command or {}
        modality = step.modality

        # A COMPILED api step is not a UI action at all — it is an HTTP request.
        # Project it through the api module so the EXISTING classifier sees a
        # read-only verb for GET/HEAD and an unrecognised verb (→ fail-closed
        # destructive) for anything that writes. No new gate, no new policy.
        if modality == "api":
            from anyuse.execution.api_step import api_hil_action, load_command
            payload = {k: v for k, v in cmd.items() if k != "bound_params"}
            bound = cmd.get("bound_params") if "bound_params" in cmd else None
            action = api_hil_action(load_command(payload), bound)
            if step.intent:
                action["args"]["intent"] = step.intent
            return action

        # Per-modality verb: prefer the command's own action verb where the
        # modality carries one (ax/coordinate), else use the modality name as a
        # stable token (js/applescript/computer_use). This keeps read-only AX
        # actions (e.g. a future 'read') and screenshots classifying read-only.
        if modality in ("ax", "coordinate"):
            verb = str(cmd.get("action", "") or modality)
        else:
            # js / applescript are scripting (side-effecting by nature in the
            # classifier); computer_use resolves to a coordinate click downstream.
            verb = modality

        # Surface command strings the classifier should see for keyword matching.
        args = {
            k: v
            for k, v in cmd.items()
            if k in ("role", "title", "value", "script", "text", "keys",
                     "label", "name", "direction")
        }
        if step.input_data:
            args["input_data"] = step.input_data
        # Carry the intent text too — a destructive intent ("delete the file")
        # should be visible to the classifier as a backstop.
        if step.intent:
            args["intent"] = step.intent

        return {"action": verb, "args": args}

    async def _maybe_hold_for_approval(self, step: PlannedStep) -> Optional[dict]:
        """Consult the injected HIL gate BEFORE dispatch; return a blocked result
        dict if the step is held, else ``None`` (meaning "proceed to dispatch").

        Default-inert contract (NOTHING changes unless HIL is explicitly on):

          * No gate injected (``self._hil_gate is None``) → return ``None``
            immediately. Byte-for-byte the prior behavior.
          * Gate present but its mode is ``off`` (the default) → return ``None``.
            We do NOT even call ``require_approval`` so a mode-off gate can never
            perturb dispatch; existing behavior is preserved exactly.
          * Gate present AND mode active (``destructive``/``all``) → classify the
            step and ask the gate, passing the LIVE master gate
            (``_visual_drive_enabled()``) as ``drive_enabled``:
              - master gate OFF → gate returns ``actuate=False`` ("nothing to
                approve"); we treat that as "no hold, proceed" so the executor's
                own downstream gating (e.g. ``_execute_visual``) is unchanged —
                the HIL layer never itself blocks a path the master gate already
                governs.
              - master gate ON, step below threshold (read-only) → actuate=True →
                proceed (no hold).
              - master gate ON, step at/above threshold → HOLD: either
                ``ApprovalRequired`` is raised (no approver) or the approver
                denies → we return a non-actuating blocked dict WITHOUT calling
                the actuator.

        The actuator is reached ONLY when this returns ``None``.
        """
        gate = self._hil_gate
        if gate is None:
            return None
        # A mode-off gate is inert: never consult it, never perturb dispatch.
        if getattr(gate, "mode", "off") == "off":
            return None

        # Import locally so the executor module has no hard dependency on the
        # gate's symbols and tests can inject a fake gate without it.
        from anyuse.execution.hil_gate import ApprovalRequired

        drive_enabled = _visual_drive_enabled()
        # the SAME projection and keywords as the
        # draft pipeline (draft_compile._gate_action / gate_keywords): under
        # ``destructive`` an AX step is held when its LABELS are destructive,
        # not merely because the classifier does not know the verb ``press``
        # (which held every AX step); under ``all`` every non-read-only step.
        from anyuse.execution.draft_compile import _gate_action, gate_keywords
        mode = getattr(gate, "mode", "all")
        action = _gate_action(step, mode)
        kwargs = {"drive_enabled": drive_enabled}
        try:
            import inspect as _inspect
            if "keywords" in _inspect.signature(gate.require_approval).parameters:
                kwargs["keywords"] = gate_keywords(mode)
        except (TypeError, ValueError):
            pass
        try:
            decision = await gate.require_approval(action, **kwargs)
        except ApprovalRequired as exc:
            logger.info(
                "HIL hold: step intent=%r (modality=%s) requires approval "
                "(%s) — NOT actuating", step.intent, step.modality, exc.step_class,
            )
            return {
                "success": False,
                "blocked": True,
                "method": step.modality,
                "error": str(exc),
                "hil_step_class": exc.step_class,
            }

        if not decision.actuate:
            # When the master gate is OFF the gate returns actuate=False with an
            # empty step_class ("nothing to approve") — that is NOT a HIL hold;
            # let the executor's own downstream gating handle it (proceed). A
            # real HIL denial carries a step_class and reason.
            if not decision.step_class:
                return None
            logger.info(
                "HIL hold: step intent=%r (modality=%s) blocked by approver "
                "(%s) — NOT actuating", step.intent, step.modality, decision.reason,
            )
            return {
                "success": False,
                "blocked": True,
                "method": step.modality,
                "error": f"held by HIL gate: {decision.reason}",
                "hil_step_class": decision.step_class,
            }

        # actuate=True → proceed to dispatch (no hold).
        return None

    # ------------------------------------------------------------------
    # Modality execution methods
    # ------------------------------------------------------------------

    async def execute_checked_step(
        self,
        step: PlannedStep,
        step_idx: Optional[int] = None,
        workflow: Optional[WorkflowTemplate] = None,
        state_graph: Optional[StateGraph] = None,
        *,
        expected_state_id: Optional[str] = None,
        on_drift: str = "escalate",
    ) -> tuple[dict, dict]:
        """The checked path for ONE step: before/after AX snapshots around
        ``_execute_step``, verified with ``_verify_step`` — the same
        after-action check ``execute()`` applies to every planned step.

        Calling ``executor._execute_step(step)`` directly would skip the
        independent AX-diff evidence ``execute()`` gathers after every
        actuation. This method is that checked wrapper, reachable without a
        full ``ExecutionPlan`` (which deterministic replay does not build):
        ``execution/deterministic.py``'s ``replay_transitions`` goes through
        it instead of touching ``_execute_step`` raw.

        The pre-step drift gate (``_apply_drift_gate``) runs here too. It
        needs a ``WorkflowTemplate`` positional step index
        (``_expected_before_state``) that a bare ``PlannedStep`` does not
        carry on its own. ``step_idx``/``workflow``/``state_graph`` are that
        missing context — all three optional and default ``None``, so a
        caller that only has a ``step`` (every unit test) is
        byte-for-byte unaffected: the gate only evaluates when a caller
        supplies where in a recorded workflow this step sits, and remains
        opt-in on top of that (``drift_gate_enabled``, default off — the
        token-saver: no baseline, no gate, no vision call). When it DOES run,
        the effective step (possibly escalated to ``computer_use`` by a real
        baseline mismatch) is what gets executed and verified, exactly as
        ``execute()`` already does for a full plan.

        Returns ``(exec_result, verification)`` — ``exec_result`` is exactly
        what ``_execute_step`` returned for the EFFECTIVE step
        (success/error/method/outcome/…), EXCEPT that a declared expected
        effect (``_apply_effect``) that is not observed, or a change
        outside it, turns a success into ``uncertain_completion`` /
        ``deviation``; ``verification`` is ``_verify_step``'s AX-diff evidence
        plus the effect verdict (``verification["effect"]``). When the drift gate was evaluated
        (all three of ``step_idx``/``workflow``/``state_graph`` given and
        ``drift_gate_enabled``), ``verification["drift_gate"]`` records
        whether it fired and why.
        """
        # a recorded step that starts in another app than the tree reads (the previous step brought
        # it forward): the tree follows the recorded app first
        from_app = self._recorded_app(state_graph, expected_state_id)
        if from_app:
            await asyncio.to_thread(self._follow_recorded_app, from_app, self.APP_BEFORE_S)
        before_pid = getattr(self._ax_tree, "_pid", None)
        before_ax = self._take_snapshot()

        eff_step = step
        drift_note: Optional[str] = None
        # a recorded transition passes its ``from_state`` directly
        # (``expected_state_id``); a workflow step passes its index.
        gate_evaluated = (
            state_graph is not None and self._drift_gate_enabled
            and (bool(expected_state_id) or (step_idx is not None and workflow is not None))
        )
        if gate_evaluated:
            eff_step, drift_note, before_ax = await self._settled_drift_gate(
                step, step_idx, workflow, state_graph, before_ax,
                expected_state_id=expected_state_id,
            )
            if drift_note is not None and on_drift == "stop":
                # A recorded replay that is not on the recorded screen does not
                # hand the step to a vision model (0 model calls) and does not
                # act: it stops with the reason (a deviation stops, it does
                # not ask). Nothing was dispatched.
                verification = {"drift_gate": {"fired": True, "note": drift_note},
                                "ax_diff_summary": None}
                why = drift_note.replace(" -> vision fallback", "")
                return refusal(f"the screen is not the recorded state "
                               f"{expected_state_id or ''}: {why}".replace("  ", " "),
                               code="drift", method=step.modality), verification

        exec_result = await self._execute_step(eff_step)
        # a step whose recorded after-state is in another app (cmd+space opens Spotlight; a Return
        # there launches TextEdit): the tree follows that app once it is in front, so the
        # after-state is read where the recording read it, not in the app the replay started in
        to_state = (((eff_step.command or {}).get("expect") or {}).get("state")
                    if isinstance((eff_step.command or {}).get("expect"), dict) else None)
        to_app = self._recorded_app(state_graph, to_state)
        if to_app and exec_result.get("success"):
            await asyncio.to_thread(self._follow_recorded_app, to_app, self.APP_SWITCH_S)
        switched = before_pid is not None and getattr(self._ax_tree, "_pid", None) != before_pid
        after_ax = self._take_snapshot()
        # two apps' trees do not diff: a switched step is judged by its recorded after-state
        base_ax = None if switched else before_ax
        verification = self._verify_step(eff_step, base_ax, after_ax) or {}
        if gate_evaluated:
            verification["drift_gate"] = {"fired": drift_note is not None, "note": drift_note}
        exec_result = self._apply_effect(eff_step, exec_result, base_ax, after_ax,
                                         verification, state_graph)
        if switched:
            exec_result = self._switched_state_check(to_state, exec_result, after_ax,
                                                     verification, state_graph)
        return exec_result, verification

    #: how long a step that brings another app forward (a launch through Spotlight, a Dock
    #: click) is given until the app of its recorded after-state is in front, and how long a step
    #: is given until the app it starts in is (polled ``APP_POLL_S`` apart)
    APP_SWITCH_S = 6.0
    APP_BEFORE_S = 1.0
    APP_POLL_S = 0.1
    _app_sleep = staticmethod(time.sleep)
    #: ``() -> {"name", "pid"}`` of the app in front now; None: ``MacOSContext.get_front_app_live``
    front_reader = None
    #: ``pid -> localized app name``; None: ``MacOSContext.app_name_for_pid``
    app_namer = None

    @staticmethod
    def _recorded_app(state_graph, state_id) -> str:
        """The app the recorded state ``state_id`` was read in ("" when unknown)."""
        if state_graph is None or not state_id or not hasattr(state_graph, "get_state"):
            return ""
        try:
            st = state_graph.get_state(state_id)
        except Exception:  # noqa: BLE001
            return ""
        return str(getattr(st, "app", "") or "")

    def _follow_recorded_app(self, app: str, wait_s: float) -> bool:
        """Rebind the AX tree to ``app`` when it reads another app now and ``app`` is (or comes)
        in front within ``wait_s``. Only a tree bound to an app by pid is rebound, and only when
        the app it reads now can be named; the front app is read live (AX first). True when the
        tree was rebound. A learned run crosses apps (Terminal -> Spotlight -> TextEdit), while
        the replay's tree was bound once, to the app in front at its start."""
        tree = self._ax_tree
        if not app or tree is None or getattr(tree, "_pid", None) is None:
            return False
        from anyuse.context.macos import MacOSContext
        namer = self.app_namer or MacOSContext.app_name_for_pid
        try:
            current = namer(tree._pid)
        except Exception:  # noqa: BLE001
            current = ""
        if not current or current.casefold() == app.casefold():
            return False
        reader = self.front_reader or (lambda: MacOSContext().get_front_app_live())
        for i in range(max(1, int(round(wait_s / self.APP_POLL_S)))):
            if i:
                self._app_sleep(self.APP_POLL_S)
            try:
                front = reader()
            except Exception:  # noqa: BLE001
                front = None
            if front and front.get("pid") and str(front.get("name") or "").casefold() == app.casefold():
                tree._pid = int(front["pid"])
                logger.info("the AX tree follows %s (pid %s), the app of the recorded state", app, tree._pid)
                return True
        logger.info("%s did not come to the front within %.1f s; the tree stays on %s", app, wait_s, current)
        return False

    def _switched_state_check(self, state_id, exec_result: dict, after_ax, verification: dict,
                              state_graph) -> dict:
        """A step that brought another app forward is checked by its recorded after-state alone
        (re-read for up to ``sum(_EFFECT_SETTLE)`` seconds while the app's window comes up):
        ``uncertain_completion`` when the screen is not that state."""
        if not exec_result.get("success") or not state_id or state_graph is None:
            return exec_result
        from anyuse.execution import step_effect as SE
        state = SE._state_check(state_id, after_ax, state_graph)
        for delay in self._EFFECT_SETTLE:
            if not state.get("checked") or state.get("matched"):
                break
            self._effect_sleep(delay)
            after_ax = self._take_snapshot()
            state = SE._state_check(state_id, after_ax, state_graph)
        verification["effect"] = dict(verification.get("effect") or {}, state=state, app_switch=True)
        if state.get("checked") and not state.get("matched"):
            out = dict(exec_result)
            out.update(success=False, acted=True, outcome_kind="uncertain_completion",
                       reason_code="effect_not_observed",
                       error=(f"the action ran, its declared effect is uncertain: the screen is not the "
                              f"recorded state {state_id} ({state.get('why')})"))
            return out
        return exec_result

    async def _execute_step(self, step: PlannedStep) -> dict:
        """Dispatch to the appropriate modality executor.

        Pre-dispatch HIL hold (default-inert): if a :class:`HILGate` was
        injected AND it is actively configured to hold (its mode is not
        ``off``), the step is classified BEFORE any actuator is reached and held
        when it meets the configured threshold. The hold is returned as a
        non-actuating ``{"success": False, "blocked": True, ...}`` result —
        matching this method's dict result contract — WITHOUT calling the
        modality actuator. With NO gate (the default) or a gate whose mode is
        ``off``, dispatch is byte-for-byte the prior behavior; read-only steps
        (ax reads, screenshots, scroll/hover/wait) are never held.
        """
        hold = await self._maybe_hold_for_approval(step)
        if hold is not None:
            return hold

        modality_map = {
            "api": self._execute_api,
            "rea": self._execute_rea,
            "ax": self._execute_ax,
            "js": self._execute_js,
            "applescript": self._execute_applescript,
            "coordinate": self._execute_coordinate,
            "computer_use": self._execute_visual,
        }

        executor_fn = modality_map.get(step.modality)
        if executor_fn is None:
            return {
                "success": False,
                "error": f"Unknown modality: {step.modality}",
                "method": step.modality,
            }

        try:
            def boundary_refusal():
                checker = getattr(self._action_executor, "_compliance_checker", None)
                if checker is None:
                    return None
                control = getattr(checker, "control", None)
                if control is not None:
                    allowed, reason = control.check()
                    if not allowed:
                        return refusal(reason, code="deadline", method=step.modality)
                # Coordinate/vision dispatch and API requests already consult
                # this same checker at their actuator/transport boundary.
                if step.modality in {"ax", "js", "applescript"}:
                    command = dict(step.command or {})
                    name = command.get("action", "press") if step.modality == "ax" else step.modality
                    try:
                        allowed, reason = checker.check_action({"action": name, "args": command})
                    except Exception as exc:
                        return refusal("BOUNDARY evaluation failed: " + str(exc), code="boundary", method=step.modality)
                    if allowed is not True:
                        return refusal("blocked by BOUNDARY: " + str(reason), code="boundary", method=step.modality)
                return None

            if asyncio.iscoroutinefunction(executor_fn):
                denied = boundary_refusal()
                if denied is not None:
                    return denied
                return await executor_fn(step)
            else:
                def dispatch():
                    denied = boundary_refusal()
                    return denied if denied is not None else executor_fn(step)
                from anyuse.core.restriction import await_owned_worker
                checker = getattr(self._action_executor, "_compliance_checker", None)
                future = asyncio.get_running_loop().run_in_executor(None, dispatch)
                return await await_owned_worker(future, getattr(checker, "control", None))
        except TimeoutError as exc:
            return refusal(str(exc), code="deadline", method=step.modality)
        except Exception as exc:
            logger.error("Step execution error (%s): %s", step.modality, exc)
            return {
                "success": False,
                "error": str(exc),
                "method": step.modality,
                **({"outcome_kind": "uncertain_completion"} if step.modality == "rea" else {}),
            }

    def _execute_rea(self, step: PlannedStep) -> dict:
        from anyuse.execution.rea_replay import execute_recorded
        command = step.command or {}
        if set(command) != {"action", "args"}:
            return refusal("invalid recorded REA command", code="rea_pins", method="rea")
        return execute_recorded(command["action"], command["args"],
                                store=self._rea_store,
                                checker=getattr(self._action_executor, "_compliance_checker", None))

    def _require_dispatch_time(self):
        checker = getattr(self._action_executor, "_compliance_checker", None)
        control = getattr(checker, "control", None)
        if control is not None:
            control.require()

    def _execute_api(self, step: PlannedStep) -> dict:
        """Execute a COMPILED implementation: a declarative HTTP request template.

        This is the top rung of the ladder (``deterministic._STABILITY["api"] = 0``).
        ``step.command`` is a :class:`anyuse.execution.api_step.RequestTemplate`
        dict; ``command["bound_params"]`` carries the workflow's parameter
        bindings (attached by ``deterministic.impl_to_step``, never persisted).

        It touches no window, no element and no pixel — so it needs no
        Accessibility/Screen-Recording grant and is NOT governed by
        ``ANYUSE_DRIVE_ENABLED`` (that gate is about moving the mouse and
        keyboard). ``ANYUSE_API_MODALITY=off`` disables the rung outright.
        Only GET/HEAD are ever sent; anything else is refused by the api
        executor itself and additionally classified destructive by the existing
        HIL gate (see ``_step_to_hil_action``).

        Every failure mode — disabled rung, refused method, network error,
        non-2xx, non-JSON, failed postcondition — returns ``success=False`` so
        ``deterministic.replay_transitions`` falls through to the recorded UI
        implementation below it.
        """
        from anyuse.execution.api_step import ApiStepExecutor, load_command

        cmd = dict(step.command or {})
        params = cmd.pop("bound_params", None) or {}
        template = load_command(cmd)
        executor = self._api_executor
        if executor is None:
            executor = ApiStepExecutor(
                # Reuse the ActionExecutor's BOUNDARY checker when one is
                # installed, so an api request obeys the same domain policy a
                # UI navigation does.
                compliance_checker=getattr(self._action_executor, "_compliance_checker", None),
                # Product replay path: per-site modes apply here (one accessor).
                apply_site_modes=True,
            )
            self._api_executor = executor
        result = executor.execute(template, params)
        if not result.get("success"):
            logger.info("api step %r failed (%s) — falling through to the UI path",
                        step.intent, result.get("error"))
        return result

    def _execute_ax(self, step: PlannedStep) -> dict:
        """Execute via AX API. Use AccessibilityTree.perform_action() or set_value()."""
        if self._ax_tree is None:
            return {"success": False, "error": "No AccessibilityTree available", "method": "ax"}

        cmd = step.command
        role = cmd.get("role", "")
        title = cmd.get("title", "")
        action = cmd.get("action", "press")
        sel_data = cmd.get("selector")
        if action not in AX_ACTIONS:
            # never "try the string as an AX action", never a default press
            return refusal(f"unsupported AX action {action!r} (supported: "
                           f"{', '.join(sorted(AX_ACTIONS))})", code="unsupported_verb", method="ax")
        if action in _AXC.TARGETLESS:
            # an action on the app itself (``open_app``): no element to resolve or check;
            # the platform branch's gate as for any action, then the handler, whose tree
            # primitive is gated too (drive gate + foreground interlock)
            element = f"app {cmd.get('app_name') or cmd.get('bundle_id') or ''}".strip()
            if self._platform is not None:
                from anyuse.execution.actions import drive_enabled
                if not drive_enabled():
                    return {"success": False, "gated": True, "method": "ax",
                            "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)",
                            "element": element}
            return self._ax_inline(action, None, step, cmd, element, None)

        def _resolve_target():
            """Resolve the step's target, or REFUSE — never guess.

            It never falls back to "the first element of this role/title"
            when a recorded Selector fails to resolve. Like the VM test harness
            (`execution/guest/flows.py`'s ``Replayer``), it resolves with the
            configured matcher, then independently checks the result against
            the recorded identity and refuses a mismatch before anything is
            pressed, so the product's own executor — not just the harness measuring
            it — refuses a wrong target.

            Returns ``(element_or_None, outcome, reason_or_None)`` where
            ``outcome`` is ``"resolved"``, ``"refused"`` (a recorded identity
            existed and no live candidate confirmed it) or ``"uncertain"`` (no
            recorded identity to check against, and the plain role/title lookup
            was not itself unambiguous).
            """
            if sel_data and sel_data.get("kind"):
                # a selector KIND (``AX_SELECTOR_KINDS``) has no recorded identity to compare:
                # ``focused`` is the element with keyboard focus right after the previous step,
                # of the selector's role and never a password field (ax_actions.resolve_focused);
                # the app / document check below still runs on it
                el, why = _AXC.resolve_kind(self._ax_tree, sel_data)
                if why is not None:
                    return None, "refused", why["error"], why["reason_code"]
                return el, "resolved", None, None
            if sel_data:
                from anyuse.context.accessibility import Selector
                try:
                    recorded = Selector.from_dict(sel_data)
                except Exception as exc:
                    return None, "refused", f"unreadable recorded selector ({exc})", "identity"
                if not recorded.document and cmd.get("document"):
                    # a selector recorded without its document
                    # (TextEdit's "First Text View" is in every document window; a map- or draft-compiled step)
                    # is scoped by the document the step names: with two windows open it resolves in that
                    # document's window instead of being refused as ambiguous
                    recorded = replace(recorded, document=str(cmd["document"]))
                if not hasattr(self._ax_tree, "find_selector"):
                    return None, "refused", "ax_tree cannot resolve a stored selector", "no_tree"
                try:
                    candidate = self._ax_tree.find_selector(recorded)
                except Exception as exc:
                    return None, "refused", f"find_selector raised ({exc})", "identity"
                if candidate is None:
                    # The configured matcher (cascade or weighted) found nothing
                    # it would stand behind — weighted abstains below its margin.
                    # That is "no candidate within the matcher's threshold", not
                    # a miss to paper over with a role/title guess. Two or
                    # more candidates are an ambiguity, refused as such.
                    amb = getattr(self._ax_tree, "last_ambiguity", None)
                    if isinstance(amb, dict) and amb.get("n", 0) > 1:
                        return None, "refused", (
                            f"ambiguous: {amb.get('why') or amb['n']} — refusing to pick one"
                        ), "ambiguous"
                    return None, "refused", (
                        f"no candidate matched the recorded identity {recorded.describe()}"
                    ), "not_found"
                try:
                    live = self._ax_tree.to_selector(candidate)
                except Exception as exc:
                    return None, "refused", f"could not describe the resolved candidate ({exc})", "identity"
                if not _same_identity(recorded, live):
                    code = "identity"
                    if recorded.app and live.app and recorded.app != live.app:
                        code = "wrong_app"
                    elif _same_identity(replace(recorded, document=""), replace(live, document="")):
                        code = "wrong_document"
                    elif getattr(recorded, "scope", None) and _same_identity(
                            replace(recorded, document="", scope=None), replace(live, document="", scope=None)):
                        code = "wrong_container"
                    return None, "refused", (
                        f"resolved element does not match the recorded identity "
                        f"(recorded={recorded.describe()}, got={live.describe()})"
                    ), code
                return candidate, "resolved", None, None

            # No selector was ever recorded for this step (a legacy role/title-only
            # command). There is no recorded identity to confirm against, so the
            # only safe accept is a genuinely UNAMBIGUOUS match — never "the first
            # one".
            els = self._ax_tree.find(role=role, title=title if title else None)
            if len(els) == 1:
                return els[0], "resolved", None, None
            if not els:
                return None, "uncertain", f"no element found for role={role!r} title={title!r}", "not_found"
            return None, "uncertain", (
                f"{len(els)} candidates for role={role!r} title={title!r}, "
                f"ambiguous — no recorded identity to disambiguate"
            ), "ambiguous"

        # Wait-for-element: UIs often lag a frame after the prior step, so a single
        # immediate lookup failing was the #1 replay fragility. Retry with backoff
        # (~1.5s total) before declaring a miss. (When found on the first try — incl.
        # all spy-based unit tests — no sleep happens.) A REFUSAL is retried too
        # (the UI may simply still be catching up), but never turns into a guess.
        target, outcome, reason, code = _resolve_target()
        if target is None:
            import time as _time
            for _delay in (0.1, 0.2, 0.4, 0.8):
                _time.sleep(_delay)
                target, outcome, reason, code = _resolve_target()
                if target is not None:
                    break
        if target is None and not sel_data and title and code == "not_found":
            # a planned step with no recorded
            # identity names its target by a LABEL only. When no element carries
            # it (a planner's English "Save" on a German "Sichern" UI), say so,
            # with the nearest labels that ARE on screen, and stop; no
            # translation table, no intervention model.
            from anyuse.execution.label_check import planned_label_problem
            prob = planned_label_problem(role, title, self._take_snapshot())
            if prob is not None:
                reason, code = prob["reason"], prob["code"]
        if target is None:
            # every unresolved target is a typed
            # REFUSAL (outcome_kind "refused", in STOP_KINDS): a retryable
            # failure would let the recorded ladder fall through to a
            # coordinate click that cannot see the identity this check
            # protects. A legacy role/title lookup that
            # found nothing or several is refused the same way (its ``outcome``
            # stays "uncertain" for the log).
            return refusal(
                reason or f"AX element not found: selector={bool(sel_data)} {role} '{title}'",
                code=code or "not_found", method="ax", outcome=outcome)

        # the app and the document, checked IMMEDIATELY before acting (after
        # the resolution and its retries, so a window that came to the front
        # meanwhile is seen). The recorded selector's ``app``/``document`` or,
        # for a step with no recorded selector (an intervention's fix), the
        # command's own ``app``/``document``.
        expected = self._expected_identity(sel_data, cmd)
        if expected is not None:
            why = self._app_document_refusal(expected, target,
                                             keystrokes=action in ("press_key", "key"))
            if why is not None:
                return refusal(why[0], code=why[1], method="ax")

        # Actuate. With a Platform injected, route through the GATED, cross-platform
        # verb seam — this is where the keystone gate now covers AX actuation (it
        # previously fired regardless of ANYUSE_DRIVE_ENABLED). Without a platform
        # (unit tests with a spy ax_tree), the inline AX dispatch runs. Both read
        # AX_ACTIONS: an action with no platform verb runs its inline handler
        # on either branch, so the two branches can never disagree about it.
        element = f"{role} '{title}'"
        spec = AX_ACTIONS[action]
        if self._platform is not None:
            from anyuse.execution.actions import drive_enabled
            if not drive_enabled():
                return {"success": False, "gated": True, "method": "ax",
                        "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)",
                        "element": element}
            if spec["verb"]:
                verb = spec["verb"]
                value = (step.input_data or cmd.get("value", "")) if verb == "set_value" else None
                self._require_dispatch_time()
                success = self._platform.perform_verb(target, verb, value)
                return {"success": success, "method": "ax", "element": element, "verb": verb}
        return self._ax_inline(action, target, step, cmd, element, expected)

    def _ax_inline(self, action, target, step, cmd, element, expected) -> dict:
        """The inline handler of every AX_ACTIONS entry. Each one ends in a
        gated ``AccessibilityTree`` primitive (drive gate + foreground
        interlock inside the primitive)."""
        self._require_dispatch_time()
        native = AX_ACTIONS[action]["native"]
        if action == "set_value":
            value = step.input_data or cmd.get("value", "")
            success = self._ax_tree.set_value(target, value)
        elif native:
            # press / toggle / expand / confirm / cancel / increment / decrement /
            # show_menu / raise: one native AX action each, from the table
            success = self._ax_tree.perform_action(target, native)
        elif action == "enter_text":
            # `set_value` is the AXValue-write rung
            # ALONE, which Finder's rename/search fields
            # ignore (they act on what a user TYPES). `enter_text` climbs the
            # full ax_value -> ax_selected_text -> keystrokes ladder and reports
            # which rung actually served — the same evidence model the harness
            # (execution/guest/flows.py's Replayer) already uses. ``expect_document``
            # is the recorded full document when the recording has one.
            value = step.input_data or cmd.get("value", "") or cmd.get("text", "")
            entry = (self._ax_tree.enter_text(target, value, expect_document=expected.document)
                     if expected is not None and expected.document
                     else self._ax_tree.enter_text(target, value))
            return {
                "success": bool(entry), "method": "ax",
                "element": element, "rung": entry.rung,
                "entry": entry.to_dict() if hasattr(entry, "to_dict") else None,
            }
        elif action in ("insert_text", "replace_range"):
            # Fast path: an AXSelectedText edit at the
            # insertion point, or over an explicit (start, length) range, with
            # the value read back — AccessibilityTree.edit_text. The target was
            # resolved above through the same identity check as every step.
            value = step.input_data if step.input_data is not None else cmd.get("text", "")
            rng = cmd.get("range") if action == "replace_range" else None
            if action == "replace_range" and not rng:
                return {"success": False, "method": "ax", "element": element,
                        "error": "replace_range needs command['range'] = [start, length]"}
            ev = self._ax_tree.edit_text(target, value,
                                         replace_range=tuple(rng) if rng else None,
                                         expect_value=cmd.get("expect_value"))
            return {"success": bool(ev), "method": "ax", "element": element,
                    "rung": "ax_selected_text", "readback": ev.readback,
                    "entry": ev.to_dict()}
        elif action in ("press_key", "key"):
            # the raw key-press primitive Finder's rename/search fields
            # need to COMMIT a value (a literal Return) — `enter_text` gets the
            # text in; this is the key that makes the app act on it.
            # `context.keyboard.press_key` is deliberately not self-gating (its
            # own module docstring: "checked by the caller"); this dispatch is
            # that caller, via `AccessibilityTree.press_key` (gated).
            keycode = cmd.get("keycode")
            if keycode is None:
                return {"success": False, "method": "ax", "element": element,
                        "error": "press_key needs command['keycode']"}
            success = self._ax_tree.press_key(keycode, cmd.get("flags", 0) or 0)
        elif action == "read":
            reader = getattr(self._ax_tree, "read_value", None)
            value = reader(target) if reader is not None else None
            return {"success": True, "method": "ax", "element": element, "read_only": True,
                    "value": value}
        elif action in _AXC.HANDLERS:
            # the runner contract (execution/ax_actions.py): select_row, append_text,
            # replace_line, open_app - each its act and its read-back, ending in a gated
            # tree primitive; a read-back mismatch fails the step
            return _AXC.HANDLERS[action](self._ax_tree, target, step, cmd)
        else:  # pragma: no cover - AX_ACTIONS and this handler are kept in step by a test
            return refusal(f"no inline handler for {action!r}", code="unsupported_verb", method="ax")

        return {
            "success": success,
            "method": "ax",
            "element": element,
        }

    def _execute_js(self, step: PlannedStep) -> dict:
        """Execute via JavaScript injection. Use MacOSContext.execute_javascript_in_browser()."""
        if self._macos_context is None:
            return {"success": False, "error": "No MacOSContext available", "method": "js"}

        cmd = step.command
        script = cmd.get("script", "")
        if not script:
            return {"success": False, "error": "No JS script in command", "method": "js"}

        from anyuse.execution.actions import drive_enabled
        if not drive_enabled():
            return {"success": False, "gated": True, "method": "js",
                    "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)"}
        from anyuse.execution.foreground_interlock import refusal
        refused = refusal("executor.js")
        if refused:
            return {"success": False, "refused": True, "method": "js", "error": refused}

        browser = cmd.get("browser")  # None = auto-detect
        self._require_dispatch_time()
        result = self._macos_context.execute_javascript_in_browser(script, browser=browser)

        if result is not None:
            return {"success": True, "method": "js", "result": result}
        else:
            return {
                "success": False,
                "error": "JavaScript execution returned None (browser may not be available)",
                "method": "js",
            }

    def _execute_applescript(self, step: PlannedStep) -> dict:
        """Execute via AppleScript. Use subprocess osascript."""
        cmd = step.command
        script = cmd.get("script", "")
        if not script:
            return {"success": False, "error": "No AppleScript in command", "method": "applescript"}

        from anyuse.execution.actions import drive_enabled
        if not drive_enabled():
            return {"success": False, "gated": True, "method": "applescript",
                    "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)"}
        from anyuse.execution.foreground_interlock import refusal
        refused = refusal("executor.applescript")
        if refused:
            return {"success": False, "refused": True, "method": "applescript", "error": refused}
        # only against apps that already granted Automation (read without asking); else the
        # step fails over to the next rung instead of raising the macOS consent prompt, which an
        # unattended replay cannot answer (it times out and macOS records a denial)
        from anyuse.context.macos import applescript_refusal
        no_consent = applescript_refusal(script)
        if no_consent:
            return {"success": False, "method": "applescript", "error": no_consent,
                    "reason_code": "automation_not_granted"}

        self._require_dispatch_time()
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return {
                    "success": True,
                    "method": "applescript",
                    "output": proc.stdout.strip(),
                }
            else:
                return {
                    "success": False,
                    "error": proc.stderr.strip() or f"osascript exit code {proc.returncode}",
                    "method": "applescript",
                }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "AppleScript timed out", "method": "applescript"}
        except Exception as exc:
            return {"success": False, "error": str(exc), "method": "applescript"}

    def _execute_coordinate(self, step: PlannedStep) -> dict:
        """Execute via coordinate click. Use existing ActionExecutor."""
        if self._action_executor is None:
            return {
                "success": False,
                "error": "No ActionExecutor available",
                "method": "coordinate",
            }

        cmd = step.command
        action = cmd.get("action", "click")
        x = cmd.get("x", 0)
        y = cmd.get("y", 0)

        denied = self._coordinate_identity_refusal(step)
        if denied is not None:
            return denied
        def actuate(name, params):
            return self._actuate_coordinate(step, name, params)

        if action == "click":
            result = actuate(
                "click_at", {"x": x, "y": y, "button": cmd.get("button", "left")}
            )
        elif action == "type":
            text = step.input_data or cmd.get("text", "")
            result = actuate(
                "type_text_at",
                {"x": x, "y": y, "text": text, "press_enter": cmd.get("press_enter", False)},
            )
        elif action == "double_click":
            result = actuate("click_at", {"x": x, "y": y, "clicks": 2})
        elif action == "right_click":
            result = actuate("click_at", {"x": x, "y": y, "button": "right"})
        elif action == "hover":
            result = actuate("hover_at", {"x": x, "y": y})
        elif action == "key_combination":
            result = actuate("key_combination", {"keys": cmd.get("keys", "")})
        elif action == "scroll":
            result = actuate("scroll_at", {"x": x, "y": y,
                "direction": cmd.get("direction", "down"), "magnitude": cmd.get("magnitude", 800)})
        elif action == "drag":
            result = actuate("drag_to", {"x1": cmd.get("x1", x), "y1": cmd.get("y1", y),
                "x2": cmd.get("x2", x), "y2": cmd.get("y2", y)})
        else:
            result = actuate("click_at", {"x": x, "y": y})

        result["method"] = "coordinate"
        return result

    def _coordinate_identity_refusal(self, step):
        cmd = step.command
        action, x, y = cmd.get("action", "click"), cmd.get("x", 0), cmd.get("y", 0)
        # a recorded coordinate rung is a blind
        # click unless something checks WHAT is under the point. When the
        # recording kept the identity of the element it clicked (``selector``:
        # the recorder stores it beside the coordinates, and
        # ``deterministic.impl_to_step`` attaches the transition's AX selector
        # to an older coordinate impl), hit-test the point right before acting
        # and refuse anything else - the same ``_same_identity`` rule the AX
        # rung applies. Keys and scrolling act on no element under the point.
        if action == "drag":
            # a recorded drag kept what was under each end; both are hit-tested
            for px, py, key in ((cmd.get("x1", x), cmd.get("y1", y), "from_selector"),
                                (cmd.get("x2", x), cmd.get("y2", y), "to_selector")):
                if cmd.get(key):
                    why = self._hit_test_refusal(px, py, cmd.get(key))
                    if why is not None:
                        return refusal(why[0], code=why[1], method="coordinate")
        elif cmd.get("selector") and action not in ("key_combination", "scroll"):
            why = self._hit_test_refusal(x, y, cmd.get("selector"))
            if why is not None:
                return refusal(why[0], code=why[1], method="coordinate")

        return None

    def _trace_front(self):
        """Use injected/current context only; tracing never creates a GUI context."""
        reader = (getattr(self, "_rea_front_reader", None) or self.front_reader or
                  getattr(getattr(self, "_macos_context", None), "get_front_app_live", None))
        if reader is None:
            return None
        try:
            value = reader()
            return dict(value) if type(value) is dict else None
        except Exception as exc:
            logger.warning("REA GUI capture cannot read front app: %s", type(exc).__name__)
            return None

    def _actuate_coordinate(self, step, action, params):
        trace = getattr(self, "_rea_ui_trace", None)
        if trace is None:
            return self._action_executor.execute(action, params)
        front = self._trace_front()
        guarded = {"checked": False, "refusal": None}
        def before_dispatch():
            guarded["checked"] = True
            # Probe setup can take time. Recheck the coordinate target and focus
            # immediately before the existing actuator, without raising an app.
            current = self._trace_front()
            if front is not None and any((current or {}).get(key) != front.get(key) for key in ("pid", "bundle_id")):
                guarded["refusal"] = refusal("focus changed during GUI capture; fresh observation required",
                                              code="focus_changed", method="coordinate")
            else:
                guarded["refusal"] = self._coordinate_identity_refusal(step)
            return guarded["refusal"]
        try:
            return trace.execute(self._action_executor, action, params, front,
                                 front_reader=self._trace_front, before_dispatch=before_dispatch)
        except Exception as exc:
            # Capture failures must never resend an action that already ran.
            logger.warning("REA GUI capture unavailable: %s", type(exc).__name__)
            if guarded["refusal"] is not None:
                return guarded["refusal"]
            if guarded["checked"]:
                raise
            denied = before_dispatch()
            return denied if denied is not None else self._action_executor.execute(action, params)

    #: how long (seconds, in steps) an unobserved declared effect is
    #: looked for again after the action - reads only, never a second action
    _EFFECT_SETTLE = (0.1, 0.2, 0.4, 0.8)
    _effect_sleep = staticmethod(time.sleep)

    def _apply_effect(self, step: PlannedStep, exec_result: dict, before_ax, after_ax,
                      verification: dict, state_graph=None) -> dict:
        """The step's DECLARED expected effect
        (``command["expect"]``, ``execution/step_effect.py``) decides success.

        Only an action that reported success is checked (a failure stays a
        failure). The effect is looked for in the AX diff and the screen after
        the action; if it is not there yet, the screen is read again for up to
        ``sum(_EFFECT_SETTLE)`` seconds - never acted on again, so a delayed
        effect ends as ``uncertain_completion``, not as a second write. A
        change outside the declared effect is a ``deviation``. Both are STOP
        kinds. ``verification["effect"]`` carries the verdict; a step that
        declares nothing keeps the actuator's own evidence (``undeclared``)."""
        from anyuse.execution import step_effect as SE
        effect = SE.declared(step)
        if not effect or not exec_result.get("success"):
            if effect is None and exec_result.get("success"):
                verification["effect"] = {"kind": SE.UNDECLARED}
            return exec_result
        target = None
        sel = (step.command or {}).get("selector")
        if isinstance(sel, dict) and sel:
            from anyuse.context.accessibility import Selector
            try:
                target = Selector.from_dict(sel)
            except Exception:  # noqa: BLE001
                target = None
        value = step.input_data if step.input_data is not None else (step.command or {}).get("text")

        def _judge(after):
            diff = None
            if self._ax_tree is not None and before_ax is not None and after is not None:
                try:
                    diff = self._ax_tree.diff(before_ax, after)
                except Exception:  # noqa: BLE001 - no diff, no observation
                    diff = None
            return SE.evaluate(effect, before_ax, after, diff, target=target,
                               input_value=value, state_graph=state_graph,
                               independent=self._independent_readings(step, target))

        verdict = _judge(after_ax)
        for delay in self._EFFECT_SETTLE:
            if not verdict.outcome_kind or after_ax is None:
                break
            self._effect_sleep(delay)
            after_ax = self._take_snapshot()
            verdict = _judge(after_ax)
        verification["effect"] = verdict.to_dict()
        kind = verdict.outcome_kind
        if not kind:
            return exec_result
        out = dict(exec_result)
        out.update(success=False, acted=True, outcome_kind=kind,
                   reason_code="deviation" if kind == "deviation" else "effect_not_observed",
                   error=(f"the action ran but {verdict.reason}" if kind == "deviation" else
                          f"the action ran, its declared effect is uncertain: {verdict.reason}"),
                   effect=verdict.to_dict())
        return out

    #: verifier independence (``ANYUSE_ATTRIBUTE_VERIFY``): an injected ``(step, target) -> [(source, text)]`` that
    #: reads the written text from ANOTHER source than AX (the file after a save, the document over Apple Events);
    #: None (the default): the AX effect check alone
    independent_reader = None

    def _independent_readings(self, step, target):
        reader = getattr(self, "independent_reader", None)
        if reader is None:
            return None
        try:
            return reader(step, target)
        except Exception:  # noqa: BLE001 - an unreadable source is no reading (AX decides as before)
            return None

    def _hit_test_refusal(self, x, y, recorded_sel) -> Optional[tuple]:
        """``None`` when the element under ``(x, y)`` is the recorded one, else
        ``(reason, code)``. Fails closed: no tree, no hit-test or no element
        under the point is a refusal, never a click on trust."""
        from anyuse.context.accessibility import Selector
        tree = self._ax_tree
        if tree is None or not hasattr(tree, "find_at") or not hasattr(tree, "to_selector"):
            return ("cannot hit-test the click point: no AX tree to confirm the recorded "
                    "element", "no_tree")
        try:
            recorded = Selector.from_dict(recorded_sel)
            el = tree.find_at(x, y)
            if el is None:
                return (f"no element under ({x}, {y}); recorded {recorded.describe()}", "not_found")
            live = tree.to_selector(el)
        except Exception as exc:  # noqa: BLE001 - a failed hit-test refuses
            return (f"hit-test at ({x}, {y}) failed ({exc})", "identity")
        if not _same_identity(recorded, live):
            return (f"the element under ({x}, {y}) is {live.describe()}, the recording "
                    f"clicked {recorded.describe()}", "identity")
        why = self._app_document_refusal(recorded, el)
        if why is not None:
            return why
        return None

    @staticmethod
    def _expected_identity(sel_data, cmd) -> Optional[Any]:
        """The recorded ``Selector`` (with ``app``/``document``) a step must still
        match, or a bare one built from the command's ``app``/``document``."""
        from anyuse.context.accessibility import Selector
        sel = None
        if isinstance(sel_data, dict) and sel_data:
            try:
                sel = Selector.from_dict(sel_data)
            except Exception:  # noqa: BLE001 - _resolve_target already refused it
                sel = None
            if sel is not None and sel_data.get("kind"):
                # a selector KIND checks its own container scope where it resolves (the row kind:
                # ax_actions.resolve_row -> scope_refusal, through the same module); checking it
                # here again would read the scope a second way
                sel.scope = None
        app = (sel.app if sel else "") or str((cmd or {}).get("app") or "")
        doc = (sel.document if sel else "") or str((cmd or {}).get("document") or "")
        if not app and not doc and not (sel is not None and sel.scope):
            return None
        if sel is None:
            sel = Selector()
        sel.app, sel.document = app, doc
        return sel

    def _app_document_refusal(self, recorded, element, keystrokes: bool = False) -> Optional[tuple]:
        """``None`` when the target is still in the recorded app and document,
        else ``(reason, code)``. Fail-closed for a real tree: an app or a
        document that cannot be read is a refusal. A test double that has no
        way to report its app or documents at all is not asked."""
        from anyuse.context.accessibility import same_document
        tree = self._ax_tree
        if recorded is None or tree is None:
            return None
        if recorded.app:
            getter = getattr(tree, "bundle_id", None)
            if getter is not None:
                live_app = getter()
                if not live_app:
                    return (f"cannot confirm the app: the AX tree is not bound to "
                            f"{recorded.app}", "wrong_app")
                if live_app != recorded.app:
                    return (f"wrong app: the target is in {live_app}, the recording was in "
                            f"{recorded.app}", "wrong_app")
            front = getattr(tree, "frontmost_bundle_id", None)
            if keystrokes and front is not None:
                live_front = front()
                if live_front != recorded.app:
                    return (f"wrong app in front: keystrokes would go to {live_front or 'an unknown app'}, "
                            f"the recording typed into {recorded.app}", "wrong_app")
        if recorded.document:
            win = getattr(tree, "element_window", None)
            if win is not None:
                title, url = win(element)
                if not same_document(recorded.document, url, title):
                    # neither path is quoted: a document's path or title is the person's data
                    return ("wrong document: the target's window shows another document than the "
                            "recording acted on", "wrong_document")
        if getattr(recorded, "scope", None):
            # the container a document-less window shows (a Finder folder, a Notes folder),
            # read again right before acting. Another container is another element. A recording
            # that read the folder's full path acts on the folder's content (a file or note row)
            # only in the folder it can confirm; one that read only the name acts where the name
            # agrees (``context/container_scope.py``: a name rules a folder out, never in).
            # Chrome outside the content (toolbar, sidebar) is the same in every such window.
            from anyuse.context.container_scope import DIFFERENT, SAME, same_scope, scope_strength
            read_scope = getattr(tree, "element_scope", None)
            if read_scope is not None:
                verdict = same_scope(recorded.scope, read_scope(element))
                if verdict == DIFFERENT:
                    return ("wrong container: the target's window shows another folder than the recording "
                            "acted in", "wrong_container")
                content = getattr(tree, "in_container_content", None)
                if (verdict != SAME and scope_strength(recorded.scope) == "path"
                        and content is not None and content(element)):
                    return ("container not confirmed: the recording read the folder's path, which the target's "
                            "window does not show now; a file or note in it is not acted on",
                            "container_unconfirmed")
        return None

    async def _execute_visual(self, step: PlannedStep) -> dict:
        """
        Full AI visual mode. Capture screenshot, send to the computer-use vision
        model, parse to a coordinate action, and actuate THROUGH the existing
        _execute_coordinate path.

        The capture + model + parse loop lives in VisualStepResolver (side-effect
        free). Actuation is reached ONLY when ANYUSE_DRIVE_ENABLED is truthy and
        ONLY via the pre-existing _execute_coordinate — no new pyautogui site.
        """
        cmd = step.command

        # Deterministic shortcut: if the command already carries coordinates,
        # use them directly. Unchanged from prior behavior (no gate needed).
        if "x" in cmd and "y" in cmd:
            return self._execute_coordinate(step)

        # Gate OFF (default): non-actuating structured result, byte-for-byte the
        # prior no-actuation contract. No screenshot, no model call, no drive.
        if not _visual_drive_enabled():
            return {
                "success": False,
                "method": "computer_use",
                "needs_orchestrator": True,
                "error": "visual mode gated (ANYUSE_DRIVE_ENABLED off)",
            }

        # Gate ON: lazily construct a resolver (or use the injected one), ask it
        # to PLAN a coordinate action, then delegate to _execute_coordinate.
        if self._visual_resolver is None:
            from anyuse.execution.visual_step import VisualStepResolver
            self._visual_resolver = VisualStepResolver()

        resolved = await self._visual_resolver.resolve(
            step, self._get_current_ax_json()
        )

        if not resolved.get("ok"):
            return {
                "success": False,
                "method": "computer_use",
                "llm_calls": int(resolved.get("llm_calls", 0) or 0),
                "error": resolved.get("error", "Visual resolver could not plan an action"),
            }

        # Hand the planned coordinate command to the existing coordinate path.
        step.command = resolved["command"]
        result = self._execute_coordinate(step)
        result["llm_calls"] = int(resolved.get("llm_calls", 0) or 0)
        return result

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def _verify_step(
        self,
        step: PlannedStep,
        before_ax: Optional[AXSnapshot],
        after_ax: Optional[AXSnapshot],
    ) -> dict:
        """
        Verify step succeeded.

        Fast path: AX diff clearly shows expected change -> success, no AI call.
        Slow path: AX diff inconclusive -> return verification context for AI.

        Returns structured context, NOT a bool. No thresholds.
        """
        verification: Dict[str, Any] = {
            "has_changes": False,
            "clearly_matched": False,
            "ax_diff_summary": None,
            "expected_after": step.expected_after,
        }

        if step.modality == "rea":
            # The registered runtime observer owns this proof. An AX diff
            # cannot promote model-judged verification to deterministic proof.
            verification["verification_tier"] = (step.command.get("args") or {}).get("verification_tier")
            return verification

        if before_ax is None or after_ax is None or self._ax_tree is None:
            verification["ax_diff_summary"] = "Cannot verify: AX snapshots unavailable"
            return verification

        diff = self._ax_tree.diff(before_ax, after_ax)

        has_changes = bool(diff.added or diff.removed or diff.changed)
        verification["has_changes"] = has_changes
        verification["ax_diff_summary"] = diff.to_summary()

        # Fast path: check if the diff clearly matches the expected outcome
        if step.expected_after and has_changes:
            verification["clearly_matched"] = diff.clearly_matches(step.expected_after)

        # Include raw diff counts for AI evaluation
        verification["added_count"] = len(diff.added)
        verification["removed_count"] = len(diff.removed)
        verification["changed_count"] = len(diff.changed)

        # No-LLM OCR tier: when the AX diff did NOT clearly confirm the outcome, try
        # the visual verifier against a LITERAL expected text — step.command
        # ["expected_text"], NOT the free-form NL expected_after intent (which would
        # substring-match random on-screen words and false-positive a failed step to
        # success). Dormant until a recording captures a literal expected_text, so it
        # never fires on the NL intent. Completes the AX→OCR deterministic stack.
        expected_text = step.command.get("expected_text") if isinstance(step.command, dict) else None
        if (not verification["clearly_matched"] and self._visual_verifier is not None
                and self._screenshot_capture is not None and expected_text):
            try:
                png = self._screenshot_capture.capture()
                if png:
                    vres = self._visual_verifier.verify(live_img=png, expected_text=expected_text)
                    verification["visual_tier"] = vres.tier
                    verification["visual_confidence"] = vres.confidence
                    if vres.passed:
                        verification["clearly_matched"] = True
                        verification["visual_matched"] = True
            except Exception as exc:
                logger.debug("visual verify tier skipped: %s", exc)

        return verification

    # ------------------------------------------------------------------
    # State graph updates
    # ------------------------------------------------------------------

    def _update_graph(
        self,
        state_graph: StateGraph,
        before_ax: Optional[AXSnapshot],
        after_ax: Optional[AXSnapshot],
        step: PlannedStep,
        result: StepResult,
    ):
        """Update state graph with what we learned from this step."""
        if not result.success:
            # Record failure on existing transitions
            from_state = step.expected_after_state or "unknown"
            for t in state_graph.transitions:
                if t.intent == step.intent:
                    t.times_executed += 1
                    t.last_executed = datetime.now().isoformat()
                    t.last_failure_reason = result.error or "Unknown"
            return

        # Record success
        from_state = step.expected_after_state or "unknown"
        to_state = result.new_state or step.expected_after_state or "unknown"

        # Create or update transition implementation
        impl = TransitionImpl(
            modality=result.method or step.modality,
            command=step.command,
            times_used=1,
            times_succeeded=1,
        )

        state_graph.add_implementation(
            from_state=from_state,
            to_state=to_state,
            intent=step.intent,
            impl=impl,
        )

        # Update transition stats
        existing = state_graph._find_transition(from_state, to_state, step.intent)
        if existing:
            existing.times_executed += 1
            existing.times_succeeded += 1
            existing.last_executed = datetime.now().isoformat()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Drift gate (opt-in; deterministic replay vs vision-on-drift)
    # ------------------------------------------------------------------

    # Modalities considered "cheap / deterministic" — eligible for the drift
    # check. A step already planned for vision is left untouched.
    _DETERMINISTIC_MODALITIES = ("ax", "js", "coordinate")

    @staticmethod
    def _ax_snapshot_to_element_dicts(
        snapshot: Optional[AXSnapshot],
    ) -> List[dict]:
        """
        Convert an AXSnapshot into the element-dict form consumed by
        UIFingerprint.matches() / StateGraph.identify_state(), mirroring the
        recorder's role/title extraction shape ({"role": ..., "title": ...}).
        PURE — reads an already-captured snapshot, captures nothing new.
        """
        if snapshot is None:
            return []
        out = []
        for el in snapshot.elements:
            d = {"role": el.role or "", "title": el.title or ""}
            tag = getattr(el, "top_tag", None)
            if tag:
                d["tag"] = tag      # the window / menu it belongs to (counted by drift_gate)
            out.append(d)
        return out

    def _gate_count_tag(self, step: PlannedStep, before_ax: Optional[AXSnapshot]) -> Optional[str]:
        """The part of the app the step acts in, for the drift gate's count: the window (or
        menu) of the step's target, resolved read-only by its recorded selector exactly as
        ``_resolve_target`` resolves it; when it does not resolve, the focused window. A READ of
        the tree (no capture, no actuation); None for a tree that cannot say (a test double)."""
        tree = self._ax_tree
        if tree is None or not hasattr(tree, "top_level_tag"):
            return None
        cmd = step.command or {}
        sel_data = cmd.get("selector")
        el = None
        if isinstance(sel_data, dict) and sel_data and not sel_data.get("kind") and hasattr(tree, "find_selector"):
            try:
                from anyuse.context.accessibility import Selector
                recorded = Selector.from_dict(sel_data)
                if not recorded.document and cmd.get("document"):
                    recorded = replace(recorded, document=str(cmd["document"]))
                el = tree.find_selector(recorded)
            except Exception:  # noqa: BLE001 - no target: the focused window
                el = None
        try:
            tag = tree.top_level_tag(el) if el is not None else None
        except Exception:  # noqa: BLE001
            tag = None
        return tag or getattr(before_ax, "focused_tag", None)

    def _expected_before_state(
        self,
        step: PlannedStep,
        step_idx: int,
        workflow: WorkflowTemplate,
    ) -> Optional[str]:
        """
        Resolve the recorded baseline state_id for this step.

        Prefers the planned step's own ``expected_after_state`` linkage only as a
        fallback; the authoritative source is the corresponding workflow step's
        ``expected_before_state`` (WorkflowIntent). Returns None when the step
        carries no recorded baseline (the gate then no-ops for that step).
        """
        # Match the workflow step positionally, only for workflow-sourced steps
        # (AI-injected/navigation steps have no recorded baseline).
        if step.source == "workflow" and step_idx < len(workflow.steps):
            wf_step = workflow.steps[step_idx]
            before = getattr(wf_step, "expected_before_state", "") or ""
            if before:
                return before
        return None

    async def _settled_drift_gate(self, step, step_idx, workflow, state_graph, before_ax,
                                  expected_state_id=None):
        """``_apply_drift_gate``, re-read while the screen settles: a step that opens a window
        (File > New) leaves the next step's gate looking at the screen before that window
        exists. A verdict that still drifts after ``DRIFT_SETTLE_S`` stands, with its reason.
        Returns ``(effective_step, drift_note, the snapshot the verdict was read on)``."""
        eff_step, drift_note = self._apply_drift_gate(
            step, step_idx, workflow, state_graph, before_ax, expected_state_id=expected_state_id)
        deadline = time.monotonic() + DRIFT_SETTLE_S
        while drift_note is not None and time.monotonic() < deadline:
            await asyncio.sleep(DRIFT_SETTLE_POLL_S)
            before_ax = self._take_snapshot()
            eff_step, drift_note = self._apply_drift_gate(
                step, step_idx, workflow, state_graph, before_ax, expected_state_id=expected_state_id)
        return eff_step, drift_note, before_ax

    def _apply_drift_gate(
        self,
        step: PlannedStep,
        step_idx: int,
        workflow: Optional[WorkflowTemplate],
        state_graph: StateGraph,
        before_ax: Optional[AXSnapshot],
        expected_state_id: Optional[str] = None,
    ) -> tuple[PlannedStep, Optional[str]]:
        """
        Pre-step drift decision. Returns (effective_step, drift_note).

        ``expected_state_id``: the recorded state this step must start
        from, when the caller knows it directly (a recorded transition's
        ``from_state``) instead of through a workflow step index.

        Only DETERMINISTIC steps (ax/js/coordinate) with a recorded baseline are
        evaluated. On a real baseline mismatch the returned step is a COPY with
        modality flipped to 'computer_use' (needs_visual=True) so the existing,
        gated, non-actuating _execute_visual handles it; otherwise the original
        step is returned unchanged (the token-saver: deterministic replay, no
        vision). PURE — no capture/actuation, no gate read/write. The
        element count is compared for the step's target window (or menu), which
        ``_gate_count_tag`` finds with a read of the tree.
        """
        if step.modality not in self._DETERMINISTIC_MODALITIES:
            return step, None

        if not expected_state_id and workflow is not None and step_idx is not None:
            expected_state_id = self._expected_before_state(step, step_idx, workflow)
        if not expected_state_id:
            return step, None

        ax_elements = self._ax_snapshot_to_element_dicts(before_ax)

        from anyuse.execution import drift_gate

        report = drift_gate.evaluate_drift(
            state_graph, expected_state_id, ax_elements,
            count_tag=self._gate_count_tag(step, before_ax),
        )
        if not report.drifted:
            # No drift — keep the cheap planned modality untouched.
            return step, None

        # Drift detected: escalate ONLY this step to the visual tier. A local
        # copy keeps the original plan/step intact; _execute_visual stays gated
        # and non-actuating, so this cannot trigger real input.
        #
        # The recorded coordinates are exactly what the drift just invalidated,
        # so the escalated command must NOT carry stale x/y — otherwise
        # _execute_visual's deterministic x/y shortcut would actuate at a now-
        # untrusted point, bypassing the gate. Strip x/y so vision RESOLVES the
        # target (and stays gated/non-actuating when ANYUSE_DRIVE_ENABLED is off).
        escalated_command = {
            k: v for k, v in (step.command or {}).items() if k not in ("x", "y")
        }
        escalated = replace(
            step,
            modality="computer_use",
            needs_visual=True,
            command=escalated_command,
        )
        note = f"drift detected: {report.reason} -> vision fallback"
        logger.info(
            "Drift gate fired on step %s (expected_state=%s): %s",
            "?" if step_idx is None else step_idx + 1, expected_state_id, note,
        )
        return escalated, note

    def _take_snapshot(self) -> Optional[AXSnapshot]:
        """Take an AX snapshot, returning None if unavailable."""
        if self._ax_tree is None:
            return None
        try:
            return self._ax_tree.snapshot()
        except Exception as exc:
            logger.warning("Failed to take AX snapshot: %s", exc)
            return None

    def _get_current_ax_json(self) -> str:
        """Get the current AX tree as JSON string.

        Built directly from ``AXSnapshot``'s dataclass fields
        (``context/accessibility.py``); only the intervention path and the
        visual-tier gate escalation reach this.
        """
        import dataclasses
        import json as _json
        snap = self._take_snapshot()
        if snap is None:
            return '{"error": "AX tree unavailable"}'
        return _json.dumps({"app_name": snap.app_name,
                            "elements": [dataclasses.asdict(e) for e in snap.elements]})
