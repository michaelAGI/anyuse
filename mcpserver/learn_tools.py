"""MCP surface for LEARNING — propose an untrusted candidate, nothing else.

An MCP server cannot prove the endpoint it was handed is a throwaway container. It therefore exposes only the proposal half:

* ``propose_learn_candidate`` — the agent runs ONCE in the container browser
  and returns a candidate. It writes only when an explicit candidate store is
  configured; raw candidates have bounded retention.
Confirmation and persistence are CLI/human-only. The old delegated helper is
kept private for compatibility tests, but is neither advertised nor dispatched.

Three properties, stated so they can be checked:

1. **No local desktop actuator.** The proposal tool refuses unless
   ``ANYUSE_LEARN_ENDPOINT`` names a browser that
   :func:`agent_browser.assert_container_endpoint` accepts — not loopback,
   nothing open. That check is a heuristic and is described as one; the
   endpoint check is only a heuristic: operators must own and isolate the
   browser, and proposals remain untrusted candidates.
2. **No agent self-confirmation.** MCP can neither discover nor dispatch a
   confirmation tool.
3. **Every proposal navigation is robots-gated** before it is sent, exactly as in the
   CLI, under ``robots_policy = respect`` unless the stored policy says
   otherwise.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CANDIDATE_STORE_ENV = "ANYUSE_MCP_CANDIDATE_STORE"

TOOL_PROPOSE_NAME = "propose_learn_candidate"
TOOL_CONFIRM_NAME = "confirm_learn_candidate"

TOOL_PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string",
                 "description": "what to do, with {q} where the value goes, e.g. "
                                "'search this site for {q}'"},
        "url": {"type": "string", "description": "where the agent starts (https origin)"},
        "value": {"type": "string", "description": "the ONE value the agent looks for"},
        "param": {"type": "string", "description": "parameter name (default q)"},
        "detail": {"type": "boolean",
                   "description": "true when the task opens ONE item and reads fields"},
        "budget_usd": {"type": "number", "description": "stop the agent at this cost"},
    },
    "required": ["task", "url", "value"],
    "additionalProperties": False,
}
TOOL_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_id": {"type": "string", "description": "from propose_learn_candidate"},
        "url": {"type": "string",
                "description": "the candidate's final URL, restated EXACTLY. A "
                               "confirmation that names nothing is refused."},
        "values": {"type": "array", "items": {"type": "string"}, "minItems": 2,
                   "description": ">= 2 further values to substitute and prove on"},
        "budget_usd": {"type": "number"},
        "delegated_confirmation": {
            "type": "boolean",
            "description": "explicitly delegate confirmation to this MCP caller"},
    },
    "required": ["candidate_id", "url", "values"],
    "additionalProperties": False,
}
TOOL_PROPOSE_DESCRIPTION = (
    "Have AnyUse's step agent do a web task ONCE, for one value, in a headless browser "
    "inside a container, and return the candidate page it reached: final URL, title, a "
    "preview of the list, and where the value sits in the URL. The proposal writes "
    "nothing unless ANYUSE_MCP_CANDIDATE_STORE names an explicit candidate store. "
    "Refuses unless ANYUSE_LEARN_ENDPOINT names a non-loopback browser with nothing open; "
    "has no local desktop actuator; endpoint isolation remains the operator's "
    "responsibility. Costs model calls (reported)."
)
TOOL_CONFIRM_DESCRIPTION = (
    "Confirm a stored candidate only when delegated_confirmation=true (default off), "
    "then restate its URL exactly and "
    "let AnyUse bind the further values into the confirmed URL, open them (no model call), "
    "check each lands on the confirmed page's URL pattern and title, derive ground truth "
    "across the values, compile, run the fresh-input proof and persist ONLY if the proof "
    "passes and the host is acknowledged. Returns the same preview it confirmed, plus the "
    "outcome. Whoever calls this is the confirming party; the record says confirmed_by=mcp."
)


def _endpoint_or_refusal() -> Dict[str, Any]:
    from anyuse.execution.agent_browser import (NotAContainerError,
                                                assert_container_endpoint,
                                                learn_endpoint)
    ep = learn_endpoint()
    try:
        info = assert_container_endpoint(ep)
    except NotAContainerError as exc:
        return {"ok": False, "error": str(exc), "endpoint": ep}
    except Exception as exc:
        return {"ok": False, "error": f"could not reach {ep}: {type(exc).__name__}: {exc}",
                "endpoint": ep}
    return {"ok": True, "endpoint": ep, "browser": info.get("Browser", "")}


def _allow_url():
    import urllib.parse as _up

    from anyuse.execution.api_permission import (ROBOTS_IGNORE, host_override,
                                                 robots_allows, robots_policy)

    def allow(url: str) -> bool:
        if robots_policy() == ROBOTS_IGNORE:
            return True
        allowed, _ = robots_allows(url)
        return not (allowed is False and not host_override(_up.urlsplit(url).netloc))

    return allow


def propose(arguments: Dict[str, Any], agent=None, data_dir: Optional[str] = None,
            endpoint_check=None) -> Dict[str, Any]:
    from anyuse.execution.learn_loop import propose as _propose, save_candidate

    task = str(arguments.get("task") or "")
    url = str(arguments.get("url") or "")
    value = str(arguments.get("value") or "")
    if not task or not url or not value:
        return {"ok": False, "error": "task, url and value are required"}
    if not url.startswith("https://"):
        return {"ok": False, "error": "url must be an https origin"}
    check = (endpoint_check or _endpoint_or_refusal)()
    if not check.get("ok"):
        return {"ok": False, "error": check.get("error"), "persisted": "nothing"}
    if agent is None:
        from anyuse.execution.agent_browser import make_agent
        agent = make_agent(check["endpoint"], allow_url=_allow_url())
    cand = _propose(task, url, value, param=str(arguments.get("param") or "q"),
                    cls="detail" if arguments.get("detail") else "list", agent=agent)
    import os
    store = data_dir or os.environ.get(CANDIDATE_STORE_ENV) or ""
    out = {"ok": bool(cand.ok), "candidate": cand.summary(), "persisted": "nothing"}
    if cand.ok:
        if store:
            out["saved"] = save_candidate(cand, store)
            out["persisted"] = ("candidate only — expires under the configured retention; "
                                "no learned workflow")
            out["next"] = ("read the URL, title and preview, then use the CLI/human "
                           "confirmation path; MCP self-confirmation is not exposed")
        else:
            out["saved"] = None
            out["next"] = (f"proposal not stored; use the CLI for human confirmation or "
                           f"configure {CANDIDATE_STORE_ENV} intentionally")
    else:
        out["error"] = cand.blocked or cand.error
    return out


def confirm(arguments: Dict[str, Any], agent=None, navigator=None, executor=None,
            replay=None, persist=None, data_dir: Optional[str] = None,
            endpoint_check=None, proof_preflight=None) -> Dict[str, Any]:
    import os
    from anyuse.execution.learn_loop import (continue_learning, discard_candidate,
                                             explicit_confirmation, load_candidate)

    delegated = bool(arguments.get("delegated_confirmation"))
    if not delegated:
        return {"ok": False, "confirmed": False,
                "error": ("MCP confirmation is default-off; use the CLI/human flow or set "
                          "delegated_confirmation=true explicitly")}

    cid = str(arguments.get("candidate_id") or "")
    store = data_dir or os.environ.get(CANDIDATE_STORE_ENV) or ""
    if not store:
        return {"ok": False, "confirmed": False,
                "error": f"no explicit candidate store; configure {CANDIDATE_STORE_ENV}"}
    cand = load_candidate(cid, store)
    if cand is None:
        return {"ok": False, "error": f"no candidate {cid!r}; call {TOOL_PROPOSE_NAME} first"}
    preview = cand.summary()
    ok, why = explicit_confirmation(cand, str(arguments.get("url") or ""))
    if not ok:
        return {"ok": False, "candidate": preview, "confirmed": False,
                "error": f"not confirmed: {why}", "persisted": "nothing"}
    values = [str(v) for v in (arguments.get("values") or []) if str(v).strip()]
    values = [v for v in values if v != cand.value]
    if len(values) < 2:
        return {"ok": False, "candidate": preview, "confirmed": False,
                "error": "give at least two further values the compiler has not seen",
                "persisted": "nothing"}
    if agent is None or navigator is None:
        check = (endpoint_check or _endpoint_or_refusal)()
        if not check.get("ok"):
            return {"ok": False, "candidate": preview, "confirmed": False,
                    "error": check.get("error"), "persisted": "nothing"}
        from anyuse.execution.agent_browser import Navigator, make_agent
        allow = _allow_url()
        agent = agent or make_agent(check["endpoint"], allow_url=allow)
        navigator = navigator or Navigator(check["endpoint"], allow_url=allow)
    if executor is None:
        from anyuse.execution.api_step import ApiStepExecutor
        executor = ApiStepExecutor()
    if replay is None:
        from anyuse.execution.cdp_replay import ReplayExecutor
        replay = ReplayExecutor(endpoint=(_endpoint_or_refusal().get("endpoint") or None))
    if persist is None:
        from anyuse.execution.learn_loop import make_persister
        persist = make_persister()
    budget = arguments.get("budget_usd")
    result = continue_learning(cand, values, agent=agent, navigator=navigator,
                               executor=executor, replay=replay, persist=persist,
                               confirmed_by="mcp",
                               proof_preflight=proof_preflight,
                               budget_usd=float(budget) if budget is not None else None)
    if result.persisted:
        discard_candidate(cid, store)
    d = result.to_dict()
    for k in ("compiled", "proof", "tier2"):
        d[k] = d[k] if d.get(k) is None else {kk: vv for kk, vv in d[k].items()
                                              if kk in ("passed", "reason", "template",
                                                        "url", "ok", "note", "median_ms")}
    return {"ok": bool(result.persisted) and result.outcome in ("tier1", "tier2"),
            "confirmed": True,
            "confirmed_by": "mcp", "candidate": preview, "outcome": result.outcome,
            "reason": result.reason, "learned_url": result.learned_url,
            "persisted": result.persisted or "nothing",
            "cost": {"agent_runs": result.agent_runs, "navigations": result.navigations,
                     "model_calls": result.model_calls, "cost_usd": result.cost_usd,
                     "learn_seconds": round(result.learn_seconds, 1)},
            "first_run_ms": result.first_run_ms, "later_run_ms": result.later_run_ms,
            "later_run_model_calls": result.later_run_model_calls,
            "discarded": result.discarded, "landing_check": result.landing_check,
            "candidate_retained": not bool(result.persisted),
            "result": d}


def handle(name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Dispatch the proposal tool; confirmation is intentionally not exposed."""
    if name == TOOL_PROPOSE_NAME:
        return propose(dict(arguments or {}))
    return None
