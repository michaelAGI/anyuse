"""
Execution Engine — Plans and executes workflows through the state graph.

Phase 4 of Plan 10: AI Pre-Processor + Multi-Modal Executor.
Phase 5 of Plan 10: Self-Healing Workflows + Graph Evolution.
Phase 6 of Plan 10: Process Mining for workflow optimization.

Components:
- ExecutionPlanner: One cheap text-only AI call to plan the full execution.
- StateGraphExecutor: Multi-modal step executor (AX, JS, AppleScript, coordinate, visual).
- InterventionHandler: AI co-pilot that fixes failed steps.
- SelfHealingManager: Persists learnings from every execution.
- ExecutionOrchestrator: Top-level entry point tying everything together.
- ProcessMiner: Analyzes execution data to discover patterns and propose improvements.

Import shape (PEP 562 lazy re-export)
-------------------------------------
This package used to EAGERLY import every submodule at package import time, and
the first one was ``planner``, whose module top does ``from google import
genai``. Because ``import anyuse.execution.drift_gate`` executes the PACKAGE
first, that meant **the entire model-free half of the ladder could not be
imported without the Gemini SDK installed** — the drift gate, self-healing, the
deterministic replay rungs and the HIL gate, every one of which documents itself
as doing no model call at all. ``drift_gate``'s own docstring says "PURE
analysis … NEVER calls a model"; it still needed ``google-genai`` on the box to
be importable.

In a real macOS GUI session on a machine that has pyobjc but deliberately NO
model SDK and no API key, importing
``anyuse.execution.drift_gate``, ``.self_healing``, ``.deterministic`` and
``.hil_gate`` all failed with ``ModuleNotFoundError: No module named 'google'``.

The public surface is unchanged: the names below are re-exported LAZILY (PEP
562), so the owning submodule is imported the first time a name is actually
accessed off the package. ``from anyuse.execution import ExecutionPlanner``
still works exactly as before and still needs the SDK — but only then, and only
for the parts that genuinely use it. This mirrors ``anyuse/memory/__init__.py``,
which had the same defect for ``pyautogui`` and was fixed the same way.
"""
from __future__ import annotations

# Every name below is loaded on first use (PEP 562), not at import. The
# desktop-side components import the model SDK and desktop frameworks at module
# level; the compile path (`api_step`, `api_compile`, `cdp_replay`,
# `learn_loop`, `api_permission`, `blocked`) needs none of them, and
# `pyproject.toml` promises that path runs on httpx + click alone. An eager
# import here made `anyuse doctor` fail on exactly that install.
# `from anyuse.execution import ExecutionPlanner` still works and still costs
# what it cost.

_LAZY = {
    # Planner
    "ExecutionPlanner": "planner", "ExecutionPlan": "plan_types", "PlannedStep": "plan_types",
    # Executor
    "StateGraphExecutor": "executor", "ExecutionResult": "executor", "StepResult": "executor",
    # Intervention
    "InterventionHandler": "intervention", "InterventionResult": "intervention",
    # Self-Healing
    "SelfHealingManager": "self_healing", "HealingReport": "self_healing",
    # Orchestrator
    "ExecutionOrchestrator": "orchestrator", "OrchestratorResult": "orchestrator",
    # Process Mining
    "ProcessMiner": "process_mining", "ExecutionTrace": "process_mining",
    "TraceStep": "process_mining", "ConformanceReport": "process_mining",
    "MiningInsight": "process_mining",
}

__all__ = list(_LAZY)


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'anyuse.execution' has no attribute {name!r}")
    import importlib
    return getattr(importlib.import_module(f"anyuse.execution.{module}"), name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
