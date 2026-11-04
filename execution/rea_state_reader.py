"""Closed read-only state-reader registry. Unknown selectors fail before IO."""
from __future__ import annotations

from . import rea_ax_state as S

STRICT_CAPABILITY = "ax_focused_readonly/1"
OWNED_CAPABILITY = "ax_owned_visible_readonly/1"
POLICY1 = "anyuse.rea-fixed-state-task/1"
POLICY2 = "anyuse.rea-fixed-state-task/2"


def strictcap(): return STRICT_CAPABILITY


def ownedcap(): return OWNED_CAPABILITY


def for_capability(name):
    if type(name) is not str: raise ValueError("literal fixed state-reader capability required")
    if name == STRICT_CAPABILITY: return S
    if name == OWNED_CAPABILITY:
        from . import rea_owned_ax_state as O
        return O
    raise ValueError("unknown state-reader capability")


def capability(policy):
    if type(policy) is not dict or type(policy.get("format")) is not str:
        raise ValueError("explicit fixed state-task policy format required")
    if policy["format"] == POLICY1:
        if "observer" in policy: raise ValueError("legacy policy cannot select a different observer")
        return STRICT_CAPABILITY
    if policy["format"] == POLICY2:
        observer = policy.get("observer")
        if (type(observer) is not dict or set(observer) != {"capability"} or
                type(observer["capability"]) is not str or observer["capability"] != OWNED_CAPABILITY):
            raise ValueError("policy2 requires the exact opt-in owned state-reader capability")
        return OWNED_CAPABILITY
    raise ValueError("unknown fixed state-task policy format")


def for_policy(policy):
    # T remains responsible for the complete task schema. This selector checks
    # its version/observer branch before importing the only additional reader.
    return for_capability(capability(policy))
