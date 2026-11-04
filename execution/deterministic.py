"""Deterministic, token-free replay — the north-star "cheap runtime".

The literal promise of the product: once a path is learned, replay it WITHOUT
calling an LLM. Until now the planner only stringified the state graph into a
Gemini prompt, so every "replay" re-reasoned (StateGraph.best_implementation had
ZERO callers). This module wires the missing path:

    find_path(from, to)  →  for each transition: best/ranked implementation
                         →  StateGraphExecutor._execute_step (per-modality dispatch)
                         →  fall through to the next implementation on failure

No ExecutionPlanner is imported. Results report the actual aggregate
``llm_calls``; it is zero only when every selected adapter was model-free.

Implementation ordering is a STABILITY-ranked fall-through (the locator ladder):
proven implementations first (by success rate), then by modality stability
(api > ax > js > applescript > coordinate > computer_use), then by usage. So a
transition tries its best-known deterministic way first and only escalates on
failure.
"""
from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from anyuse.execution.plan_types import PlannedStep
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UITransition
from anyuse.execution.outcome import STOP_KINDS, classify_outcome

if TYPE_CHECKING:  # import-time inert; see impl_to_step for why it is deferred
    from anyuse.execution.planner import PlannedStep

logger = logging.getLogger(__name__)

# Lower = more stable / preferred when success data is equal (the ladder order).
# "api" sits ABOVE "ax": a COMPILED implementation (a declarative HTTP request
# proved equivalent to the recorded UI outcome on parameter values that were NOT
# in the recording — see execution/api_compile.py + api_verify.py) is the most
# stable rung there is. It depends on no window, no element path and no pixel, so
# it cannot drift. An "api" implementation only ever enters a state graph through
# an explicit `anyuse workflow compile` run, so this reordering changes nothing
# for an uncompiled workflow; and any api failure (network, non-2xx, shape
# mismatch, postcondition false) simply falls through to the recorded UI path
# below it, which is the pre-existing behaviour.
# The ladder, most durable first. `cdp` (deterministic browser replay) sits
# between the compiled request and the desktop UI path: it is two orders of
# magnitude slower than a proved request and one order faster than an agent,
# makes zero model calls like the rung above it, and works whenever the page
# can be reached with the input in its URL. Inserting it shifts the numbers of
# the rungs below but NOT their relative order, which is what the existing
# ranking tests pin.
_STABILITY = {"api": 0, "cdp": 1, "ax": 2, "js": 3, "applescript": 4,
              "coordinate": 5, "computer_use": 6}

#: rungs that act on the screen: their recorded to_state is a postcondition
_UI_MODALITIES = ("ax", "coordinate")

# Recorder verb (click_at/type_text_at/…) → the executor's short action vocab.
_AT_VERB = {"click_at": "click", "type_text_at": "type", "double_click_at": "double_click",
            "right_click_at": "right_click", "hover_at": "hover"}


def _normalize_coordinate_command(cmd: dict) -> dict:
    """Translate the RECORDER's coordinate command into what _execute_coordinate reads.

    The recorder stores {absolute_x, absolute_y, relative_x, relative_y,
    action=<verb>_at}; _execute_coordinate reads x/y and a short action vocab
    (click/type/…). Without this, replay reads x/y as 0,0 and every *_at verb falls
    through to a plain click — so a recorded *type* became a click at the origin.
    (Recorded absolute coords are already logical points — pyautogui/pynput space —
    so no px→pt conversion here; only vision-model pixels get converted, upstream.)
    """
    out = dict(cmd)
    if "x" not in out and "absolute_x" in out:
        out["x"] = out["absolute_x"]
    if "y" not in out and "absolute_y" in out:
        out["y"] = out["absolute_y"]
    act = out.get("action", "")
    if act in _AT_VERB:
        out["action"] = _AT_VERB[act]
    return out


def rank_implementations(transition: UITransition) -> List[TransitionImpl]:
    """Order a transition's implementations for fall-through: proven first
    (success-rate desc), then by modality stability, then by usage."""
    rea = [impl for impl in transition.implementations if impl.modality == "rea"]
    if rea:
        # A REA proof is scoped to this exact operation. UI/API alternatives
        # cannot stand in for a failed pin or an uncertain runtime effect.
        return rea
    return sorted(
        transition.implementations,
        key=lambda impl: (-impl.success_rate, _STABILITY.get(impl.modality, 9), -impl.times_used),
    )


def _llm_calls(result: Dict[str, Any]) -> int:
    """Read one adapter's canonical count without double-counting aliases."""
    value = result.get("llm_calls") if "llm_calls" in result else result.get("model_calls", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _recorded_selector(transition: UITransition) -> Optional[Dict[str, Any]]:
    """The recorded AX selector of the element ``transition`` acts on (from its
    ``ax`` implementation), or ``None``. Several AX implementations that
    disagree about the element are no identity at all (``None``)."""
    sels = []
    for impl in transition.implementations:
        sel = (impl.command or {}).get("selector") if impl.modality == "ax" else None
        if isinstance(sel, dict) and sel:
            sels.append(sel)
    if not sels or any(s != sels[0] for s in sels[1:]):
        return None
    return dict(sels[0])


def impl_to_step(transition: UITransition, impl: TransitionImpl,
                 params: Optional[Dict[str, Any]] = None) -> PlannedStep:
    """Build an executable PlannedStep from a stored transition implementation.

    The command dict is modality-specific and passed straight through to the
    matching ``_execute_*`` method (which reads ``step.command``).

    ``params`` are the workflow's runtime parameter bindings. They are attached
    to the COPY of the command as ``bound_params`` for the ``api`` modality only
    (a compiled request template is parameterised: ``_execute_api`` binds
    ``{name}`` placeholders from here). The stored ``impl.command`` is never
    mutated, so nothing parameter-shaped is ever persisted into the graph.

    ``PlannedStep`` is the module-top import from ``execution.plan_types``
    (dependency-free), not a deferred import from ``execution.planner``:
    ``PlannedStep``/``ExecutionPlan`` live in ``plan_types.py`` (see the
    module docstring above and ``execution/planner.py``'s own comment), so the
    token-free ladder loads without the Gemini SDK; a
    deferred import here would only re-import ``execution.planner`` as an
    unnecessary side effect (regression: `test_deterministic_import_does_not
    _load_model_backed_modules`)."""
    cmd = dict(impl.command or {})
    if impl.modality == "coordinate":
        cmd = _normalize_coordinate_command(cmd)
        if not cmd.get("selector"):
            # the recorded identity of the element this transition acts on
            # (its AX rung's selector, recorded from the same click). The
            # coordinate rung hit-tests the point against it before clicking,
            # so a coordinate rung is never a blind click when the recording
            # knew what it clicked. Only the COPY carries it.
            recorded = _recorded_selector(transition)
            if recorded:
                cmd["selector"] = recorded
    elif impl.modality == "api" and params:
        cmd["bound_params"] = dict(params)
    return PlannedStep(
        intent=transition.intent,
        source="workflow",
        target_element=str(cmd.get("target_element") or cmd.get("element_path") or ""),
        modality=impl.modality,
        command=cmd,
        input_data=cmd.get("text") or cmd.get("input_data"),
        expected_after=transition.intent,
        expected_after_state=transition.to_state,
        reasoning="deterministic replay (no LLM)",
    )


def _has_baseline(state_graph: Optional[StateGraph], state_id: Optional[str]) -> bool:
    """Does ``state_graph`` hold a usable recorded fingerprint for ``state_id``?
    No baseline, no gate (as in the draft pipeline's ``_usable_states``): a
    placeholder or a missing state is not evidence that the screen drifted."""
    if state_graph is None or not state_id:
        return False
    from anyuse.execution.drift_gate import _fingerprint_is_placeholder
    return not _fingerprint_is_placeholder(state_graph.get_state(state_id))


def _checked_kwargs(executor, **wanted) -> Dict[str, Any]:
    """The keyword arguments ``executor.execute_checked_step`` accepts, out of
    ``wanted`` (an older executor or a test double takes fewer)."""
    try:
        params = inspect.signature(executor.execute_checked_step).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(wanted)
    return {k: v for k, v in wanted.items() if k in params}


async def replay_transitions(transitions: List[UITransition], executor,
                             params: Optional[Dict[str, Any]] = None,
                             state_graph: Optional[StateGraph] = None) -> Dict[str, Any]:
    """Replay a sequence of transitions deterministically via ``executor``.

    ``executor`` is a StateGraphExecutor. Each implementation attempt goes
    through ``executor.execute_checked_step`` — before/after AX snapshots
    around the per-modality ``_execute_step`` dispatch, verified the same way
    ``execute()`` verifies a planned step — rather than calling
    ``_execute_step`` raw. Before this, deterministic replay bypassed the
    checked path entirely: not just the
    identity refusal now built into ``_resolve_target()``, but also the
    independent AX-diff evidence ``execute()`` gathers after every actuation.
    The pre-step drift gate runs too when ``state_graph`` is given (below:
    the transition's ``from_state`` is the expected state); the after-action
    verification is unconditional. For each transition we try ranked
    implementations until one succeeds, recording usage stats (so replay also
    improves the success ranking). ``llm_calls`` is the sum reported by every
    attempted adapter (``exec_result`` from ``execute_checked_step`` is exactly
    what ``_execute_step`` would have returned), including failed fall-through
    rungs and computer-use — it is zero only when every attempted rung was.

    ``params`` (optional, default ``None`` → prior behaviour byte-for-byte) are
    the workflow's parameter bindings, used only by a compiled ``api``
    implementation to bind its request template.

    ``state_graph``: the graph the transitions
    come from. With it, every attempt passes the transition's ``from_state``
    to the executor's pre-step drift gate (``expected_state_id``) - the same
    gate the draft replay and the planned path run - and a fired gate STOPS the
    replay (``on_drift="stop"``: a ``refused`` outcome, nothing dispatched, no
    vision model) instead of escalating. Without it the gate cannot run (no
    baseline to compare with), as before.
    """
    steps_report: List[Dict[str, Any]] = []
    total_llm_calls = 0
    for t in transitions:
        impls = rank_implementations(t)
        if not impls:
            steps_report.append({"transition": t.intent, "success": False, "reason": "no implementations"})
            return {"success": False, "stopped_at": t.intent, "steps": steps_report,
                    "llm_calls": total_llm_calls}

        tried: List[Dict[str, Any]] = []
        succeeded = False
        for impl in impls:
            step = impl_to_step(t, impl, params)
            if impl.modality in _UI_MODALITIES and _has_baseline(state_graph, t.to_state):
                # the recorded after-state is part of the step's declared
                # effect (a UI rung must leave the screen in the transition's
                # to_state; an api rung changes no screen). Only the copy.
                from anyuse.execution.step_effect import merge
                step.command["expect"] = merge(step.command.get("expect"),
                                               {"state": t.to_state, "source": "transition"})
            if hasattr(executor, "execute_checked_step"):
                kw = (_checked_kwargs(executor, state_graph=state_graph,
                                      expected_state_id=t.from_state, on_drift="stop")
                      if _has_baseline(state_graph, t.from_state)
                      else _checked_kwargs(executor, state_graph=state_graph)
                      if state_graph is not None else {})
                res, verification = await executor.execute_checked_step(step, **kw)
            else:
                # Fakes/spies in existing tests that only implement
                # _execute_step (no snapshot/verify surface) still work —
                # this is strictly additive, not a required interface.
                res, verification = await executor._execute_step(step), None
            attempt_llm_calls = _llm_calls(res)
            total_llm_calls += attempt_llm_calls
            ok = bool(res.get("success"))
            kind = classify_outcome(res)
            tried.append({
                "modality": impl.modality, "success": ok, "error": res.get("error"),
                "outcome_kind": kind, "outcome": res.get("outcome"),
                "reason_code": res.get("reason_code"),
                "llm_calls": attempt_llm_calls,
                "ax_diff_summary": (verification or {}).get("ax_diff_summary"),
                "drift_gate": (verification or {}).get("drift_gate"),
            })
            impl.times_used += 1
            if ok:
                impl.times_succeeded += 1
                succeeded = True
                break
            if kind in STOP_KINDS:
                t.times_executed += 1
                steps_report.append({"transition": t.intent, "success": False,
                                     "tried": tried, "modality_used": None,
                                     "outcome_kind": kind})
                return {"success": False, "stopped_at": t.intent,
                        "outcome_kind": kind, "reason": res.get("error"),
                        "reason_code": res.get("reason_code"), "steps": steps_report,
                        "llm_calls": total_llm_calls}

        t.times_executed += 1
        if succeeded:
            t.times_succeeded += 1
        steps_report.append({"transition": t.intent, "success": succeeded, "tried": tried,
                             "modality_used": next((x["modality"] for x in tried if x["success"]), None)})
        if not succeeded:
            return {"success": False, "stopped_at": t.intent,
                    "outcome_kind": "retryable_failure",
                    "steps": steps_report, "llm_calls": total_llm_calls}

    return {"success": True, "steps": steps_report, "llm_calls": total_llm_calls}


async def replay_path(state_graph: StateGraph, from_state: str, to_state: str, executor,
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """find_path(from→to) over the graph, then deterministically replay it.

    This is the literal find_path→best_implementation→executor wiring. Returns a
    'no_path' result (not an exception) when the graph has no route — the caller
    then escalates to computer-use exploration."""
    transitions = state_graph.find_path(from_state, to_state)
    if not transitions:
        return {"success": False, "reason": "no_path", "from": from_state, "to": to_state,
                "steps": [], "llm_calls": 0}
    result = await replay_transitions(transitions, executor, params, state_graph=state_graph)
    result["path_len"] = len(transitions)
    return result
