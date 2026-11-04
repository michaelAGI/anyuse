"""The MCP tool ``agent_run`` (the default profile; the meta profile's ``call_operation`` on site ``agent``):
one open-ended agent run through the harness.

It returns the outcome (the stop), the reason, the model calls (the process counter's delta), the cost and
the compiled tool's name when one was saved.

Commit steps (a login, a cart, a form sent, a payment, a deletion): by default (``ANYUSE_MCP_AGENT_COMMITS=human``)
the run stops at the first one with ``outcome: "human_required"`` and hands it to the user, even when the task asked
for it, because over MCP the task comes from an agent. With ``ANYUSE_MCP_AGENT_COMMITS=agent`` ("initiation =
approval"): a commit step the task covers runs without a question; a deviation stops; a step
the task does NOT cover is asked of the CALLER - the calling model gets ``outcome: "confirm_required"`` with the exact
action, target, values and amount and a ``resume`` token, and calls ``agent_run`` again with ``{"resume": token,
"confirm": true|false}`` (a one-time code: ``{"resume": token, "otp": "123456"}``). The run waits in this server
process; no answer within ``CONFIRM_TTL_S`` means stop, never a guess. A resume token answers exactly one question,
once. The daily spend cap applies to the policy's model calls (``serve()`` arms the ledger).
"""
from __future__ import annotations

import contextvars
import queue
import secrets
import threading
from typing import Any, Callable, Dict, Mapping, Optional

TOOL_NAME = "agent_run"
#: the name before tool names dropped their dots; a call by it still reaches this tool
LEGACY_NAMES = ("agent.run",)
TOOL_DESCRIPTION = (
    "Run ONE open-ended task through the AnyUse agent harness: a model policy chooses typed actions "
    "(browser on a guarded container page for `site`, desktop through the guest job agent for `app`, HTTP GET, "
    "the task workspace, proven runtime tools). It browses, reads and fills; a commit step (a login, a cart, a "
    "form sent, a payment, a deletion) stops with outcome human_required and the exact step: hand that step to "
    "the user, do not run it yourself. Only if the user set ANYUSE_MCP_AGENT_COMMITS=agent does a step the task "
    "asks for run, a deviation (another target, a higher amount, an extra step) stop, and a step the task did not "
    "ask for return outcome confirm_required with a resume token: call agent_run again with "
    "{resume, confirm: true|false} (or {resume, otp} for a one-time code). "
    "Returns the outcome, the reason, the model calls and cost, and the compiled tool's name if `compile` saved one."
)
TOOL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "The task text; {name} placeholders are filled from values"},
        "site": {"type": "string", "description": "A website (host or URL) to work on in the guarded browser"},
        "app": {"type": "string", "description": "A desktop app (name or bundle id) to work in through the guest"},
        "values": {"type": "object", "description": "Named values for the task's placeholders; a password only as "
                                                    "a reference ${secret:NAME} (the Keychain item)",
                   "additionalProperties": {"type": "string"}},
        "policy": {"type": "string", "enum": ["model", "cerebras", "claude", "scripted"], "default": "model",
                   "description": "model: the configured model (ANYUSE_MODEL_PROVIDER); cerebras: its older name; "
                                  "claude: Sonnet through the Claude CLI"},
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 200},
        "compile": {"type": "boolean", "default": False,
                    "description": "Compile a finished run into a saved tool (desktop; web needs fresh values)"},
        "fresh": {"type": "array", "items": {"type": "string"},
                  "description": "Fresh values for a web compile's proof (>= 2)"},
        "resume": {"type": "string", "description": "The token of a confirm_required answer: answers that question"},
        "confirm": {"type": "boolean", "description": "With resume: true runs the asked step, false stops the run"},
        "otp": {"type": "string", "description": "With resume: the one-time code the site asked for"},
    },
    "additionalProperties": False,
}

#: how long a run waits for the caller's answer to one question
CONFIRM_TTL_S = 300
#: how long one call waits for the run's next event (a question or the end)
WAIT_S = 900


class PendingRun:
    """One run on its own thread whose questions are answered through ``agent_run {"resume": ...}`` calls."""

    def __init__(self, run: Callable[..., Dict[str, Any]], args: tuple, kwargs: Dict[str, Any], ttl_s: float):
        self.ttl_s = float(ttl_s)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.answers: "queue.Queue[Any]" = queue.Queue()
        self.token = ""
        self.question: Optional[Dict[str, Any]] = None
        self.final: Optional[Dict[str, Any]] = None
        self.expired_tokens: set = set()
        self.lock = threading.Lock()
        kwargs = dict(kwargs, asker=self.ask)
        # the run thread carries the caller's context (an MCP tool call is user-initiated
        # work - ANYUSE_ROBOTS=user_initiated reads that from the context the call set)
        ctx = contextvars.copy_context()
        self.thread = threading.Thread(target=ctx.run, args=(self._main, run, args, kwargs), daemon=True,
                                       name="anyuse-agent-run")

    def start(self) -> "PendingRun":
        self.thread.start()
        return self

    def _main(self, run, args, kwargs) -> None:
        try:
            out = run(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - never a crash of the server
            out = {"ok": False, "outcome": "error", "reason": f"{type(exc).__name__}: {exc}"[:300], "model_calls": 0,
                   "cost_usd": 0.0, "compiled": None}
        self.final = out
        self.events.put(("done", out))

    def ask(self, q):
        """The harness's asker: publish the question with a fresh token and wait for its answer."""
        from anyuse.execution import commit_policy as CP
        with self.lock:
            self.token = secrets.token_urlsafe(18)
            q.token, q.expires_in_s = self.token, int(self.ttl_s)
            self.question = q.to_dict()
        self.events.put(("question", self.question))
        try:
            ans = self.answers.get(timeout=self.ttl_s)
        except queue.Empty:
            ans = CP.Answer()
            with self.lock:
                self.expired_tokens.add(self.token)
        with self.lock:
            self.token = ""
        return ans if isinstance(ans, CP.Answer) else CP.Answer()

    def answer(self, token: str, confirm: Optional[bool], otp: str) -> Optional[str]:
        """None when the answer was taken; else why not."""
        from anyuse.execution import commit_policy as CP
        with self.lock:
            if not self.token or token != self.token:
                return "this token does not answer the open question (used, expired or unknown)"
            kind = (self.question or {}).get("question")
            if kind == CP.OTP and not otp:
                return "this question asks for a one-time code: resume with {\"otp\": \"...\"}"
            if kind == CP.CONFIRM and confirm is None:
                return "this question asks for a yes or no: resume with {\"confirm\": true|false}"
            self.token = ""
        self.answers.put(CP.Answer(confirmed=confirm if kind == CP.CONFIRM else None, code=otp or ""))
        return None

    def next_event(self, wait_s: float) -> tuple:
        try:
            return self.events.get(timeout=wait_s)
        except queue.Empty:
            return ("timeout", None)


#: token -> the run that asked (the server process's own memory; a restarted server forgets every question)
PENDING: Dict[str, PendingRun] = {}
_PENDING_LOCK = threading.Lock()


def _failure(reason: str, outcome: str = "refused") -> Dict[str, Any]:
    return {"ok": False, "outcome": outcome, "reason": reason, "model_calls": 0, "cost_usd": 0.0, "compiled": None}


def _event_answer(pr: PendingRun, wait_s: float) -> Dict[str, Any]:
    kind, payload = pr.next_event(wait_s)
    if kind == "question":
        with _PENDING_LOCK:
            PENDING[payload.get("token", "")] = pr
        otp = payload.get("question") == "otp"
        return {"ok": False, "outcome": "confirm_required", "reason": payload.get("why"),
                "confirm_required": payload,
                "resume": {"token": payload.get("token"), "expires_in_s": payload.get("expires_in_s"),
                           "how": ('call agent_run with {"resume": "<token>", "otp": "<the code>"}; no answer stops '
                                   'the run' if otp else
                                   'call agent_run with {"resume": "<token>", "confirm": true} to allow this one step, '
                                   '{"resume": "<token>", "confirm": false} to stop the run; no answer stops it')},
                "run_id": payload.get("run_id"), "model_calls": None, "cost_usd": None, "compiled": None}
    if kind == "done":
        return public(payload)
    return _failure(f"the run is still working after {wait_s:.0f} s; it stops on its own budget", "error")


def handle(arguments: Mapping[str, Any], *, run: Optional[Callable[..., Dict[str, Any]]] = None,
           storage=None, ttl_s: float = CONFIRM_TTL_S, wait_s: float = WAIT_S, **injected: Any) -> Dict[str, Any]:
    """One call; ``run`` is ``harness_product.run_agent_task`` (tests inject a fake); ``injected`` carries the
    server's fakes (``toolbox``, ``policy_obj``, ``guest``, ``page_factory``)."""
    args = dict(arguments or {})
    token = str(args.get("resume") or "").strip()
    if token:
        with _PENDING_LOCK:
            pr = PENDING.pop(token, None)
        if pr is None:
            return _failure("no open question has this token (answered, expired or unknown); no answer means the "
                            "run stopped", "expired")
        conf = args.get("confirm")
        why = pr.answer(token, conf if isinstance(conf, bool) else None, str(args.get("otp") or "").strip())
        if why:
            if token in pr.expired_tokens:
                out = public(pr.final) if pr.final is not None else _failure("the question expired", "expired")
                out["outcome_note"] = "the question expired before the answer: no answer means stop"
                return out
            with _PENDING_LOCK:
                if pr.token == token:
                    PENDING[token] = pr          # a wrong answer shape: the question stays open
            return _failure(why)
        return _event_answer(pr, wait_s)
    task = str(args.get("task") or "").strip()
    if not task:
        return _failure("task is required (or resume with the token of a confirm_required answer)")
    values = args.get("values") or {}
    if not isinstance(values, dict) or not all(isinstance(v, (str, int, float)) for v in values.values()):
        return _failure("values must be an object of strings")
    policy = str(args.get("policy") or "model").lower()
    if policy == "scripted" and not injected.get("policy_obj") and not injected.get("script"):
        return _failure("a scripted policy needs a script (not over MCP)")
    if run is None:
        from anyuse.execution.harness_product import run_agent_task
        run = run_agent_task
    from anyuse.settings import get as get_setting
    agent_commits = str(get_setting("ANYUSE_MCP_AGENT_COMMITS") or "human").strip().lower() == "agent"
    kwargs = dict(values={str(k): str(v) for k, v in values.items()}, site=str(args.get("site") or ""),
                  app=str(args.get("app") or ""), policy=policy, max_steps=args.get("max_steps"),
                  compile=bool(args.get("compile")), fresh=[str(x) for x in (args.get("fresh") or [])],
                  storage=storage, commits_need_human=not agent_commits, **injected)
    pr = PendingRun(run, (task,), kwargs, ttl_s).start()
    return _event_answer(pr, wait_s)


def public(out: Dict[str, Any]) -> Dict[str, Any]:
    """The MCP shape: outcome, reason, model calls, cost, the commits and questions, the compiled tool's name (never
    a page record, never a secret: the harness's redactor already replaced them by their references)."""
    comp = out.get("compiled") or None
    return {"ok": bool(out.get("ok")), "outcome": out.get("outcome"), "reason": out.get("reason"),
            "steps": out.get("steps"), "decisions": out.get("decisions"), "model_calls": out.get("model_calls", 0),
            "cost_usd": out.get("cost_usd", 0.0), "result": out.get("result"),
            "effects": out.get("effects"), "held": out.get("held"), "stopped_action": out.get("stopped_action"),
            "commits": out.get("commits"), "questions": out.get("questions"),
            "compiled": ({"name": comp.get("name"), "status": comp.get("status"), "reasons": comp.get("reasons")}
                         if comp else None),
            "refusal": out.get("refusal"), "run_id": out.get("run_id"), "policy": out.get("policy"),
            **({"rea_executions": out["rea_executions"]} if out.get("rea_executions") else {}),
            **({"rea_escalation": out["rea_escalation"]} if out.get("rea_escalation") is not None else {})}
