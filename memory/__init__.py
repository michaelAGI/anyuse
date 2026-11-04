"""
Workflow Memory Module - Record, save, and replay workflows.

Features:
- Visual anchoring with pixel-based template matching
- AI fallback when matching fails
- Window state restoration
- Variable substitution for credentials
- Checkpoint verification

Import shape (PEP 562 lazy re-export)
-------------------------------------
This package used to EAGERLY import every submodule at package import time
(``recorder`` → ``matcher`` → ``import pyautogui`` at module top level). That
made *any* access to the package — including the pure, actuation-free
``anyuse.memory.capability_feed`` read-only feed — drag in the screen/mouse
actuation stack. On a headless host (no ``pyautogui`` / no display) the eager
chain raises ``ImportError`` at ``import anyuse.memory.capability_feed`` time,
which broke the read-only GET /internal/brain/capabilities feed and the
capability unit tests that mock the descriptors and never touch pyautogui.

To keep the public import surface byte-for-byte identical while removing the
eager heavy-import side effect, the public names below are re-exported LAZILY
via module ``__getattr__`` (PEP 562). The submodule is imported only the first
time one of its names is actually accessed off the package, so:

  * ``from anyuse.memory.capability_feed import list_capability_descriptors`` —
    and ``from anyuse.memory import capability_feed`` — import WITHOUT pulling
    pyautogui (capability_feed itself only lazy-imports ``memory.storage``).
  * ``from anyuse.memory import WorkflowRecorder`` (or ``TemplateMatcher`` etc.)
    still works EXACTLY as before — it triggers the matching submodule import on
    first access, which is where ``pyautogui`` is loaded, on the host that
    actually drives. No public name was removed or renamed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# Public name → the submodule that defines it. Access to any of these off the
# package (``anyuse.memory.WorkflowRecorder`` / ``from anyuse.memory import ...``)
# triggers the submodule import LAZILY, on first use, instead of eagerly at
# package import. The mapping is the single source of truth for ``__all__`` too.
_LAZY_EXPORTS = {
    # Models (models.py imports no actuation stack, but defer it for uniformity)
    "Workflow": "anyuse.memory.models",
    "WorkflowStep": "anyuse.memory.models",
    "VisualAnchor": "anyuse.memory.models",
    "WindowState": "anyuse.memory.models",
    # Intent-based models (Plan 10)
    "WorkflowParam": "anyuse.memory.models",
    "WorkflowIntent": "anyuse.memory.models",
    "WorkflowTemplate": "anyuse.memory.models",
    "migrate_step_to_intent": "anyuse.memory.models",
    # Storage
    "WorkflowStorage": "anyuse.memory.storage",
    # Recording (recorder → matcher → pyautogui; the heavy chain, now deferred)
    "WorkflowRecorder": "anyuse.memory.recorder",
    "RecordingContext": "anyuse.memory.recorder",
    "StateGraphRecorder": "anyuse.memory.recorder",
    "StateGraphRecordingContext": "anyuse.memory.recorder",
    # Playback
    "WorkflowPlayer": "anyuse.memory.player",
    "SmartPlayer": "anyuse.memory.player",
    # Matching (matcher.py does ``import pyautogui`` at module top — heavy)
    "TemplateMatcher": "anyuse.memory.matcher",
    "AnchorCapture": "anyuse.memory.matcher",
    # Window
    "WindowManager": "anyuse.memory.window_manager",
    # Environment
    "ReplayEnvironment": "anyuse.memory.environment",
    "EnvironmentState": "anyuse.memory.environment",
}

__all__ = list(_LAZY_EXPORTS.keys())


def __getattr__(name: str):
    """PEP 562 lazy attribute hook for the package's public re-exports.

    Imports the owning submodule only on first access of ``name`` and caches the
    resolved object back onto this module's globals so subsequent lookups skip
    the hook (standard PEP 562 memoization). Anything not in the export map
    raises ``AttributeError`` exactly as a normal missing attribute would.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target)
    value = getattr(module, name)
    globals()[name] = value  # cache so the hook fires at most once per name
    return value


def __dir__():
    """Include the lazily re-exported names in ``dir(anyuse.memory)``."""
    return sorted(list(globals().keys()) + __all__)


if TYPE_CHECKING:
    # Static type checkers / IDEs do not run __getattr__, so spell the public
    # surface out for them. This block is import-time inert at runtime (the
    # ``if TYPE_CHECKING`` guard is always False when executed), so it does NOT
    # re-introduce the eager pyautogui import — it exists purely for tooling.
    from anyuse.memory.models import (  # noqa: F401
        Workflow,
        WorkflowStep,
        VisualAnchor,
        WindowState,
        WorkflowParam,
        WorkflowIntent,
        WorkflowTemplate,
        migrate_step_to_intent,
    )
    from anyuse.memory.storage import WorkflowStorage  # noqa: F401
    from anyuse.memory.recorder import (  # noqa: F401
        WorkflowRecorder,
        RecordingContext,
        StateGraphRecorder,
        StateGraphRecordingContext,
    )
    from anyuse.memory.player import WorkflowPlayer, SmartPlayer  # noqa: F401
    from anyuse.memory.matcher import TemplateMatcher, AnchorCapture  # noqa: F401
    from anyuse.memory.window_manager import WindowManager  # noqa: F401
    from anyuse.memory.environment import (  # noqa: F401
        ReplayEnvironment,
        EnvironmentState,
    )
