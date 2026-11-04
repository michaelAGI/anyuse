"""Shared execution outcome kinds used by every implementation rung.

The kinds a run STOPS on (``STOP_KINDS``) are never retried and never fall
through to another rung of the ladder (``deterministic.replay_transitions``),
and the planned path (``StateGraphExecutor.execute``) neither asks the
intervention model nor continues after one:

* ``terminal_refusal``: a policy said no (approval, a person, robots, a block).
* ``refused``: the product could not confirm WHAT it would act on (the recorded
  identity, the app, the document, an ambiguous match, a planned label that is
  not on screen, a verb the platform does not support) and did not act.
  Without this kind an identity refusal would be classified
  ``retryable_failure`` and the ladder would fall through to a blind,
  recorded-coordinate click.
* ``uncertain_completion``: the step acted but its declared effect was not
  observed; acting again could write twice.
* ``deviation``: the step acted and the screen changed OUTSIDE the declared
  effect (the permitted-diff rule); a deviation stops, it does not ask.
* ``incomplete``: the rung ran out of road part-way.
"""
from __future__ import annotations

from typing import Any, Mapping

SUCCESS = "success"
TERMINAL_REFUSAL = "terminal_refusal"
REFUSED = "refused"
RETRYABLE_FAILURE = "retryable_failure"
UNCERTAIN_COMPLETION = "uncertain_completion"
DEVIATION = "deviation"
INCOMPLETE = "incomplete"

KINDS = frozenset({SUCCESS, TERMINAL_REFUSAL, REFUSED, RETRYABLE_FAILURE,
                   UNCERTAIN_COMPLETION, DEVIATION, INCOMPLETE})
STOP_KINDS = frozenset({TERMINAL_REFUSAL, REFUSED, UNCERTAIN_COMPLETION, DEVIATION, INCOMPLETE})


def classify_outcome(result: Mapping[str, Any]) -> str:
    """Return a stable kind for an executor result, including legacy flags.

    An explicit ``outcome_kind`` wins. Otherwise a result that says it REFUSED
    (``refused`` true, or ``outcome == "refused"``, the shape ``_execute_ax``
    returns) is ``refused``: it must never be read as a
    retryable failure, because the next rung of a recorded ladder is a
    coordinate click that cannot see the identity the refusal protected."""
    explicit = str(result.get("outcome_kind") or "")
    if explicit in KINDS:
        return explicit
    if result.get("success"):
        return SUCCESS
    if result.get("refused") or str(result.get("outcome") or "") == REFUSED:
        return REFUSED
    if result.get("deviation"):
        return DEVIATION
    if result.get("uncertain") or result.get("completion_uncertain"):
        return UNCERTAIN_COMPLETION
    if any(result.get(key) for key in (
            "blocked", "requires_approval", "requires_human", "robots_refused")):
        return TERMINAL_REFUSAL
    return RETRYABLE_FAILURE


def with_outcome(result: Mapping[str, Any]) -> dict:
    out = dict(result)
    out["outcome_kind"] = classify_outcome(out)
    return out


def refusal(reason: str, *, code: str, method: str, **extra: Any) -> dict:
    """The one result shape for "did not act, could not confirm the target".
    ``code`` is a short machine reason (``identity``, ``ambiguous``,
    ``wrong_app``, ``wrong_document``, ``label_mismatch``,
    ``unsupported_verb``, ``not_found``, ``no_tree``)."""
    out = {"success": False, "refused": True, "outcome": REFUSED, "outcome_kind": REFUSED,
           "reason_code": code, "error": reason, "method": method}
    out.update(extra)
    return out


#: how a STOPPED run states its outcome in text (the agent's response string and
#: ``AgentResult.outcome_reason``): ``stopped (<kind>): <reason>``
STOPPED_PREFIX = "stopped ("


def stop_text(kind: str, reason: str) -> str:
    return f"{STOPPED_PREFIX}{kind}): {reason}"


def stopped_kind(text: object) -> str:
    """The stop kind a ``stop_text`` names, or ``""``."""
    t = str(text or "")
    if not t.lower().startswith(STOPPED_PREFIX):
        return ""
    kind = t[len(STOPPED_PREFIX):].split(")", 1)[0].strip()
    return kind if kind in STOP_KINDS else ""
