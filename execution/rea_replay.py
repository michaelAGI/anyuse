"""Literal, pinned registered REA commands shared by both workflow players.

These commands are demonstrations, not parameter templates. A replay reopens
the registration and delegates every primitive to the ordinary catalog gate.
"""
from __future__ import annotations

import copy
import json
import re

from .outcome import REFUSED, UNCERTAIN_COMPLETION, refusal

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"rea\.[a-zA-Z0-9_-]{1,48}\.[a-zA-Z0-9_-]{1,48}\Z")
MAX_COMMAND_BYTES = 128000


def literal_command(action, args):
    """Validate and detach a bounded recorded command without substitutions."""
    if action not in {"rea_action", "rea_plan"} or type(args) is not dict:
        raise ValueError("typed recorded REA action required")
    def pure(value, depth=0):
        if depth > 16:
            raise ValueError("recorded REA JSON is too deeply nested")
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("recorded REA JSON keys must be strings")
            for child in value.values():
                pure(child, depth + 1)
        elif type(value) is list:
            for child in value:
                pure(child, depth + 1)
        elif type(value) not in {str, int, float, bool, type(None)}:
            raise ValueError("recorded REA inputs must be pure JSON")
    pure(args)
    raw = json.dumps(args, ensure_ascii=False, allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise ValueError("recorded REA command exceeds byte bound")
    value = json.loads(raw)
    steps = value.get("steps") if action == "rea_plan" else [value]
    if action == "rea_plan" and set(value) != {"steps"}:
        raise ValueError("recorded REA plan accepts only steps")
    if type(steps) is not list or not 1 <= len(steps) <= 32:
        raise ValueError("recorded REA plan requires 1–32 steps")
    for step in steps:
        if (type(step) is not dict or set(step) != {"name", "inputs", "catalog_sha256", "verification_tier"}
                or type(step["name"]) is not str or not _NAME.fullmatch(step["name"])
                or type(step["inputs"]) is not dict or type(step["catalog_sha256"]) is not str
                or not _SHA.fullmatch(step["catalog_sha256"])
                or step["verification_tier"] not in {"deterministic", "model_judged"}):
            raise ValueError("recorded REA step requires literal inputs and proof pins")
        if action == "rea_plan" and step["verification_tier"] != "deterministic":
            raise ValueError("recorded REA plans require deterministic verification")
    if action == "rea_plan":
        from .rea_catalog import plan_steps
        plan_steps([{"name": step["name"], "inputs": step["inputs"],
                     "expected_catalog_sha256": step["catalog_sha256"]} for step in steps])
    return value


def execute_recorded(action, args, *, store=None, checker=None):
    """Execute once; every refusal or uncertain effect is terminal for replay."""
    from .actions import drive_enabled
    from .foreground_interlock import require_clear
    from .rea_catalog import CatalogStore
    from .system_prompt_policy import resolve_configured

    prompt_calls = 0
    try:
        command = literal_command(action, args)
        if not drive_enabled():
            return refusal("REA replay requires ANYUSE_DRIVE_ENABLED", code="drive_gate", method="rea")
        store = store if store is not None else CatalogStore()
        steps = command["steps"] if action == "rea_plan" else [command]
        # Check the whole demonstration before executing any prefix. The catalog
        # compares the digest again under its own execution lock.
        specs = store.list_tools()
        for step in steps:
            matches = [spec for spec in specs if spec["name"] == step["name"]]
            if (len(matches) != 1 or matches[0].get("catalog_sha256") != step["catalog_sha256"]
                    or matches[0].get("verification_tier") != step["verification_tier"]):
                raise ValueError("recorded REA registration or proof pins changed")
        # Resolve only at the outer decision boundary. A response invalidates
        # the demonstration's starting observation and stops this replay.
        prompt = resolve_configured("rea_replay.before_operation", task_context="Replay a recorded REA operation")
        prompt_calls = (prompt.get("decision") or {}).get("model_calls", 0)
        if prompt.get("attempted") or prompt.get("reobserve_required") or prompt.get("next_action") == "stop":
            raise ValueError("system prompt requires fresh observation before recorded REA replay")
    except Exception as exc:
        return refusal(str(exc), code="rea_pins", method="rea", model_calls=prompt_calls)

    def execute_step(name, inputs, *, expected_catalog_sha256, expected_identity=None):
        started = False
        try:
            # Read-only interlock inside a batch: never answer a prompt or
            # resume the remainder of a plan on a changed screen.
            require_clear("rea_replay.before_step")
            if checker is not None:
                allowed, reason = checker.check_action({"action": "rea_action", "args": {
                    "name": name, "inputs": copy.deepcopy(inputs)}})
                if allowed is not True:
                    raise ValueError("REA blocked by BOUNDARY: " + str(reason))
            started = True
            kwargs = {"expected_catalog_sha256": expected_catalog_sha256}
            if expected_identity is not None:
                kwargs["expected_identity"] = expected_identity
            return store.execute(name, copy.deepcopy(inputs), **kwargs)
        except Exception as exc:
            return {"ok": False, "status": "uncertain" if started else "refused", "reason": str(exc)}

    if action == "rea_plan":
        try:
            result = store.execute_plan([{"name": step["name"], "inputs": step["inputs"],
                                          "expected_catalog_sha256": step["catalog_sha256"]} for step in steps],
                                        execute_step=execute_step)
        except Exception as exc:
            return {"success": False, "method": "rea", "outcome_kind": UNCERTAIN_COMPLETION,
                    "error": str(exc), "terminal_on_failure": True, "automatic_retry": False}
        executions = result.get("executions", [])
    else:
        execution = execute_step(steps[0]["name"], steps[0]["inputs"],
                                 expected_catalog_sha256=steps[0]["catalog_sha256"])
        executions = [execution]
        result = dict(execution, completed_steps=1 if execution.get("ok") is True else 0,
                      failed_step_index=None if execution.get("ok") is True else 0)
    counts = [prompt_calls] + [e.get("model_calls", 0) for e in executions]
    model_calls = sum(counts) if all(type(count) is int and count >= 0 for count in counts) else None
    if result.get("ok") is not True:
        kind = REFUSED if result.get("status") == "refused" else UNCERTAIN_COMPLETION
        return {"success": False, "method": "rea", "outcome_kind": kind,
                "error": str(result.get("reason") or result.get("status") or "REA replay stopped"),
                "completed_steps": result.get("completed_steps", 0),
                "failed_step_index": result.get("failed_step_index"), "executions": executions,
                "model_calls": model_calls,
                "terminal_on_failure": True, "automatic_retry": False}
    return {"success": True, "method": "rea", "completed_steps": len(steps), "executions": executions,
            "verification_tier": ("deterministic" if action == "rea_plan" else steps[0]["verification_tier"]),
            "model_calls": model_calls,
            "terminal_on_failure": True, "automatic_retry": False}
