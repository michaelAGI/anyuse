"""
AnyUse - Computer Control Using Screen Elements

A macOS agent that uses Gemini to control your computer.

Usage:
    # Simple
    from anyuse import run
    result = run("Open Safari")
    
    # Full control
    from anyuse import Agent
    agent = Agent(verbose=True)
    result = agent.run("Open Safari")
    
    # Async
    from anyuse import arun
    result = await arun("Open Safari")
    
    # Agentic (for tools)
    agent = Agent(auto_confirm=True)
    result = agent.run(task)
    return result.to_dict()
    
    # Guide generation
    from anyuse.guides import GuideGenerator, IntegratedLearner
    learner = IntegratedLearner()
    learner.learn_from_session(session)
"""

import importlib
from typing import TYPE_CHECKING

# NOTE: This package previously imported the actuation stack
# (``anyuse.core.agent`` -> ``anyuse.credentials`` -> ``vault`` -> ``pyautogui``)
# and the watcher/guides subpackages eagerly at import time. That made
# ``import anyuse`` (and even ``import anyuse.watcher.<submodule>``, which runs
# this package ``__init__``) hard-fail whenever an optional heavy dependency
# (pyautogui, pyobjc, ...) was not installed.
#
# To keep perception/watcher modules importable in lightweight environments,
# the convenience re-exports below are now resolved lazily via PEP 562
# ``__getattr__``. ``from anyuse import Agent`` still works on demand; the heavy
# dependency is only required when the attribute is actually accessed.

# Map of public attribute name -> (submodule, attribute) for lazy re-export.
_LAZY_EXPORTS = {
    # Agent / core
    "Agent": ("anyuse.core.agent", "Agent"),
    "run": ("anyuse.core.agent", "run"),
    "arun": ("anyuse.core.agent", "arun"),
    "AgentResult": ("anyuse.core.result", "AgentResult"),
    "ResultStatus": ("anyuse.core.result", "ResultStatus"),
    "ActionResult": ("anyuse.core.result", "ActionResult"),
    # Guides
    "AppGuide": ("anyuse.guides", "AppGuide"),
    "GuideStorage": ("anyuse.guides", "GuideStorage"),
    "GuideGenerator": ("anyuse.guides", "GuideGenerator"),
    "IntegratedLearner": ("anyuse.guides", "IntegratedLearner"),
}


def __getattr__(name):
    """Lazily resolve convenience re-exports (PEP 562).

    This defers importing the actuation/watcher/guides stacks (and their
    optional heavy dependencies) until the corresponding attribute is actually
    accessed, so ``import anyuse`` stays cheap and dependency-light.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    module = importlib.import_module(module_name)
    value = getattr(module, attr)
    globals()[name] = value  # cache for subsequent access
    return value


def __dir__():
    return sorted(list(globals().keys()) + list(_LAZY_EXPORTS.keys()))


if TYPE_CHECKING:  # pragma: no cover - for type checkers / IDEs only
    from anyuse.core.agent import Agent, run, arun
    from anyuse.core.result import AgentResult, ResultStatus, ActionResult
    from anyuse.guides import (
        AppGuide,
        GuideStorage,
        GuideGenerator,
        IntegratedLearner,
    )

__version__ = "0.4.0"
__all__ = [
    # Agent
    "Agent",
    "run", 
    "arun",
    "AgentResult",
    "ResultStatus",
    "ActionResult",
    # Guides
    "AppGuide",
    "GuideStorage",
    "GuideGenerator",
    "IntegratedLearner",
]
