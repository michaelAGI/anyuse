"""Application-guide public API, loaded lazily.

Importing a state-graph value object must not also import guide generation and
its model SDK. Public names remain compatible through module ``__getattr__``.
"""
from __future__ import annotations

_LAZY = {
    "AppGuide": "models", "GuideType": "models", "ActivityRecord": "models",
    "GuideStorage": "storage", "GuideDetector": "detector",
    "AppObservations": "generator", "GuideGenerator": "generator",
    "IntegratedLearner": "generator",
    "LocatorCensus": "locator_census", "Verdict": "locator_census",
    "CHROME": "locator_census", "CONTENT": "locator_census", "UNSURE": "locator_census",
    "NEVER_TRAVELS": "projection", "PROJECTION_VERSION": "projection",
    "ProjectionLeak": "projection", "project_app_guide": "projection",
    "StateGraph": "state_graph", "UIState": "state_graph",
    "UITransition": "state_graph", "TransitionImpl": "state_graph",
    "UIElement": "state_graph", "UIFingerprint": "state_graph",
    "compute_structural_hash": "state_graph",
}

__all__ = list(_LAZY)


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'anyuse.guides' has no attribute {name!r}")
    import importlib
    return getattr(importlib.import_module(f"anyuse.guides.{module}"), name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
