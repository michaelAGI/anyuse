"""The general agent harness: ONE loop the four existing loops can run on.

    task text + values -> Harness.run(policy, toolbox)
        observe (typed) -> policy (model-agnostic) -> action (typed, with an effect class)
        gate: step cap, call cap, budget, wall time, the effect policy, the tool set's own guards
        dispatch through the tool set's EXISTING guarded primitive
        -> Trace (kind web | desktop | http | files), a stop with a reason

The tool sets (``harness_tools``) hold injected backends; the policy (``harness_policies``) holds
the injected model call. Optional system-prompt handling runs only at explicit decision boundaries,
on the desktop tool's actual guest runner or the REA-only process's guarded VM context. Manual mode
does no prompt IO. Model calls
in this process use the ONE counter in ``execution.model_adapter`` (F15); validated guest usage
adds only calls not already counted here. The call cap includes system-prompt model decisions.
"""
from __future__ import annotations

import contextlib
import os
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import harness_types as T

HARNESS_VERSION = 1
SETTING_MAX_STEPS = "ANYUSE_AGENT_MAX_STEPS"
SETTING_UNASKED = "ANYUSE_AGENT_UNASKED_IRREVERSIBLE"
SETTING_MAX_USD = "ANYUSE_AGENT_MAX_USD"
#: who approves a commit step of agent_run over MCP: human (default) or agent
SETTING_MCP_COMMITS = "ANYUSE_MCP_AGENT_COMMITS"
DEFAULT_MAX_STEPS = 20
DEFAULT_MAX_USD = 1.0
DEFAULT_WALL_S = 600.0
#: consecutive steps with the same screen and no successful action before the run stops
DEFAULT_NO_PROGRESS = 3
UNASKED_ASK = "ask"
UNASKED_STOP = "stop"
#: an earlier name of ``ask``; read as ``ask``
UNASKED_HOLD = "hold"
UNASKED_MODES = (UNASKED_ASK, UNASKED_STOP, UNASKED_HOLD)

#: ``approver(context) -> bool``; synchronous (a CLI prompt, a test's lambda, a routed inbox)
Approver = Callable[[Dict[str, Any]], bool]


class _SystemPromptStop(RuntimeError):
    def __init__(self, resolution: Dict[str, Any], reason: str):
        super().__init__(reason)
        invalidated = bool(resolution.get("attempted") or resolution.get("reobserve_required"))
        self.result = {"status": "reobserve_required" if invalidated else "manual_required",
                       "reobserve_required": invalidated, "automatic_retry": False,
                       "system_prompt": resolution}


@dataclass
class HarnessConfig:
    max_steps: int = DEFAULT_MAX_STEPS
    #: policy decisions, including system-prompt model decisions (0 = ``max_steps``)
    max_calls: int = 0
    max_usd: float = DEFAULT_MAX_USD
    wall_s: float = DEFAULT_WALL_S
    no_progress_steps: int = DEFAULT_NO_PROGRESS
    #: open tasks: a commit step the task does not cover is asked of the caller (``ask``, the
    #: default) or stops the run unasked (``stop``, for unattended use)
    unasked_irreversible: str = UNASKED_ASK
    #: every commit step (a login, a cart, a form sent, a payment, a deletion) stops and is handed to the user, even
    #: when the task asked for it: set for agent_run over MCP, where the task and any confirmation come from an agent
    commits_need_human: bool = False
    #: an unreadable answer is repaired once (the policy sees why) before the run stops
    unreadable_repairs: int = 1
    #: an action naming an element that is not there (or not of that kind) is answered with
    #: a corrective observation (what the valid elements are) instead of a wasted step, at most this often per run;
    #: each correction is still a policy call (the call cap allows for them)
    index_corrections: int = 3

    @classmethod
    def from_settings(cls, **overrides: Any) -> "HarnessConfig":
        from anyuse.settings import get as get_setting
        cfg = cls(**overrides)
        if "max_steps" not in overrides:
            try:
                cfg.max_steps = int(get_setting(SETTING_MAX_STEPS))
            except Exception:  # noqa: BLE001 - the default stands
                cfg.max_steps = DEFAULT_MAX_STEPS
        if "max_usd" not in overrides:
            # ANYUSE_AGENT_MAX_USD: a constant per-run budget would stop a CLI policy (booked at the CLI's reported
            # cost) before the documented desktop check's dozen decisions
            try:
                raw = get_setting(SETTING_MAX_USD)
                cfg.max_usd = math.inf if str(raw).strip().lower() == "unlimited" else float(raw)
            except Exception:  # noqa: BLE001 - the default stands
                cfg.max_usd = DEFAULT_MAX_USD
        if "unasked_irreversible" not in overrides:
            mode = str(get_setting(SETTING_UNASKED) or "").strip().lower()
            mode = UNASKED_ASK if mode == UNASKED_HOLD else mode
            cfg.unasked_irreversible = mode if mode in (UNASKED_ASK, UNASKED_STOP) else UNASKED_STOP
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# "explicitly asked" (section 4): deterministic, documented, tested
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN.findall(str(text or ""))]


def destructive_words(label: str) -> List[str]:
    """The destructive keywords (EN+DE, ``hil_gate``) a label contains, as substrings of its lowercase text: a label
    that holds a keyword as part of a word ("PayNow", "Deleteall") counts."""
    from anyuse.execution.hil_gate import DESTRUCTIVE_KEYWORDS_ALL
    low = str(label or "").lower()
    return [kw for kw in DESTRUCTIVE_KEYWORDS_ALL if kw in low]


def _verb_in(word: str, text: str) -> bool:
    """The keyword as a whole word of the task text, or its stem (the first five letters of a keyword of five or more)
    as a token prefix: "delete" is asked by "deleting", "löschen" by "lösche"; a task word that only holds the keyword
    inside it ("dropdown", "undelete") asks for nothing."""
    if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text):
        return True
    stem = word[:5]
    return len(word) >= 5 and any(t.startswith(stem) for t in _tokens(text))


def asked_for(task: str, label: str, target: str = "") -> bool:
    """Was this irreversible action explicitly asked for by the task text?

    True only when (1) a destructive keyword found in the action's label or arguments also occurs
    in the task text (as the word or its stem), and (2) when the action names a target, a content
    token (>= 3 characters) of that target occurs in the task text. "Delete the draft note" asks for
    a press on "Delete" with the row "draft"; "tidy up my notes" does not.
    """
    low_task = str(task or "").lower()
    words = destructive_words(label)
    if not words or not any(_verb_in(w, low_task) for w in words):
        return False
    if target:
        toks = set(_tokens(target))
        if toks and not (toks & set(_tokens(task))):
            return False
    return True


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
class Harness:
    """``run(task, values)`` -> :class:`harness_types.Trace`.

    ``toolbox``: :class:`harness_tools.Toolbox` (``observe``, ``available``, ``effect_of``, ``gate``,
    ``dispatch``). ``policy``: ``decide(task, observation, history, available) -> Decision``.
    ``approver``: asked only under ``unasked_irreversible = hold``. ``clock``: injectable."""

    def __init__(self, toolbox, policy, config: Optional[HarnessConfig] = None, *,
                 approver: Optional[Approver] = None, clock: Optional[Callable[[], float]] = None,
                 run_id: str = "", workspace: str = "", asker=None, judge=None, mandate=None):
        self.toolbox = toolbox
        self.policy = policy
        self.config = config or HarnessConfig()
        self.approver = approver
        self.clock = clock or time.monotonic
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.workspace = workspace
        #: open tasks: ``asker(Question) -> Answer`` (a terminal, the MCP bridge, a test); the earlier ``approver(ctx) ->
        #: bool`` is wrapped into one
        self.asker = asker
        #: ``judge(task, commit) -> {"answer"}``: consulted only for an uncertain coverage rule result
        self.judge = judge
        #: a given mandate (a replay's: exactly its recorded commit steps); else read from the task
        self.mandate = mandate

    # -- helpers ----------------------------------------------------------------------------
    def _model_scope(self):
        """The model-check scope around one dispatched step (``execution/model_check.py``): ``agent`` when a model
        drives the run (the policy calls one) and no mandate was given, so a demonstration's search may put several
        submit controls or search URLs to the configured small model; nothing for a replay (a given mandate, a policy
        without a model) and nothing new inside a scope that is already open (a product call keeps its own)."""
        from anyuse.execution import model_check as MC
        if self.mandate is None and getattr(self.policy, "calls_model", True) and not MC.current_scope():
            return MC.scope("agent")
        return contextlib.nullcontext()

    def _history_line(self, k: int, action: Optional[T.Action], outcome: Optional[T.Outcome],
                      why: str = "") -> str:
        if action is None:
            return f"{k + 1}. (your answer was not one valid JSON action: {why})"
        s = f"{k + 1}. {action}"
        if outcome is None:
            return s
        if outcome.ok:
            where = outcome.url_after or (outcome.observation.url if outcome.observation else "")
            return s + (f" -> ok, now at {where[:120]}" if where else " -> ok")
        return s + f" -> FAILED: {outcome.why[:160]}"

    def _stop(self, trace: T.Trace, stop: str, reason: str, action: Optional[T.Action] = None) -> None:
        trace.stop, trace.stop_reason = stop, reason[:400]
        if action is not None:
            trace.stopped_action = action.to_dict()

    def _asker(self):
        """The caller's channel: the asker, else the earlier approver (a yes/no) wrapped into one, else None."""
        if self.asker is not None:
            return self.asker
        if self.approver is None:
            return None
        from anyuse.execution import commit_policy as CP
        approver = self.approver

        def ask(q):
            if q.kind != CP.CONFIRM:
                return CP.Answer()
            c = q.commit
            ctx = {"task": self._task, "action": (c.action if c else {}), "effect": c.effect if c else T.IRREVERSIBLE,
                   "target": (c.label or c.target) if c else "", "step": q.step, "run_id": self.run_id,
                   "question": q.to_dict()}
            try:
                return CP.Answer(confirmed=bool(approver(ctx)))
            except Exception:  # noqa: BLE001 - a failing approver is a declined question
                return CP.Answer(confirmed=False)
        return ask

    def _record_question(self, trace: T.Trace, q, ans) -> None:
        red = self._redactor()
        rec = {"question": q.to_dict(), "answer": {"confirmed": ans.confirmed if ans is not None else None,
                                                   "code": "${otp}" if (ans is not None and ans.code) else ""}}
        trace.questions.append(red.obj(rec) if red is not None else rec)

    def _redactor(self):
        return getattr(self.toolbox, "redactor", None)

    def _commit_gate(self, trace: T.Trace, mandate, used, commit, k: int):
        """``(verdict, stop)`` for one commit step: covered -> proceed; deviation -> stop; not covered -> the caller is
        asked once (no answer = stop, never a guess); ``stop`` mode or no one to ask -> stop unasked."""
        from anyuse.execution import commit_policy as CP
        v = CP.decide(mandate, commit, used, self.judge)
        if v.kind == CP.DEVIATION:
            self._stop(trace, T.Stop.DEVIATION, f"step {k}: {commit.cls} {commit.label!r}: {v.why}")
            return v, T.Stop.DEVIATION
        if self.config.commits_need_human:
            self._stop(trace, T.Stop.HUMAN_REQUIRED,
                       f"hand to the user (do not run it yourself): step {k}, the {commit.cls} step {commit.label!r} "
                       f"on {commit.target[:80]!r}. Over MCP the user does logins, payments, sent forms and "
                       f"deletions; {SETTING_MCP_COMMITS}=agent lets the calling agent approve them")
            return v, T.Stop.HUMAN_REQUIRED
        if v.kind == CP.COVERED:
            return v, None
        asker = self._asker()
        mode = self.config.unasked_irreversible
        if mode == UNASKED_STOP or asker is None:
            self._stop(trace, T.Stop.IRREVERSIBLE_NOT_ASKED,
                       f"step {k}: the {commit.cls} step {commit.label!r} on {commit.target[:80]!r} is not covered by "
                       f"the task ({v.why}); {SETTING_UNASKED}={mode}"
                       + ("; no one to ask, so the run stops" if asker is None else "; the run stops unasked"))
            return v, T.Stop.IRREVERSIBLE_NOT_ASKED
        q = CP.Question(CP.CONFIRM, v.why, commit, step=k, run_id=self.run_id)
        try:
            ans = asker(q)
        except Exception:  # noqa: BLE001 - a failing channel is no answer
            ans = CP.Answer()
        ans = ans if isinstance(ans, CP.Answer) else CP.Answer()
        self._record_question(trace, q, ans)
        if ans.confirmed is True:
            return CP.Verdict(CP.COVERED, f"the caller confirmed ({v.why})", "confirmed", v.uncertain, v.judged), None
        if ans.confirmed is False:
            self._stop(trace, T.Stop.CONFIRM_DECLINED, f"step {k}: the caller declined the {commit.cls} step "
                                                       f"{commit.label!r} ({v.why})")
            return v, T.Stop.CONFIRM_DECLINED
        self._stop(trace, T.Stop.CONFIRM_UNANSWERED, f"step {k}: no answer to the question about the {commit.cls} "
                                                     f"step {commit.label!r}; no answer means stop ({v.why})")
        return v, T.Stop.CONFIRM_UNANSWERED

    def _otp_bridge(self, trace: T.Trace):
        """The tool sets' own questions (a one-time code): the same channel, recorded on the trace."""
        from anyuse.execution import commit_policy as CP

        def ask(q):
            q.run_id = self.run_id
            q.step = len(trace.steps)
            asker = self._asker() if q.kind != CP.OTP else (self.asker or None)
            if asker is None:
                ans = CP.Answer()
            else:
                try:
                    ans = asker(q)
                except Exception:  # noqa: BLE001
                    ans = CP.Answer()
            ans = ans if isinstance(ans, CP.Answer) else CP.Answer()
            self._record_question(trace, q, ans)
            return ans
        return ask

    def _redact_obs(self, obs: T.Observation) -> T.Observation:
        red = self._redactor()
        if red is None or not red.active:
            return obs
        o = T.Observation(**{k: v for k, v in obs.__dict__.items()})
        o.url, o.title, o.text, o.note = red.text(o.url), red.text(o.title), red.text(o.text), red.text(o.note)
        o.lists = [red.text(x) for x in o.lists]
        o.page, o.mapping = red.obj(o.page), red.obj(o.mapping)
        o.elements = [T.Element(**{k: red.obj(v) for k, v in e.__dict__.items()}) for e in o.elements]
        return o

    def _redact_trace(self, trace: T.Trace) -> None:
        red = self._redactor()
        if red is None or not red.active:
            return
        for st in trace.steps:
            st.observation = self._redact_obs(st.observation)
            st.outcome.why = red.text(st.outcome.why)
            st.outcome.data, st.outcome.input = red.obj(st.outcome.data), red.obj(st.outcome.input)
            st.outcome.observation = None
            st.decision_text = red.text(st.decision_text)
            st.commit, st.verdict = red.obj(st.commit), red.obj(st.verdict)
        trace.result = red.obj(trace.result)
        trace.stop_reason = red.text(trace.stop_reason)
        trace.stopped_action = red.obj(trace.stopped_action)
        trace.questions = red.obj(trace.questions)
        trace.system_prompt_usage = red.obj(trace.system_prompt_usage)

    def _record_prompt_usage(self, trace: T.Trace, resolution: Dict[str, Any], *, remote: bool,
                             locally_counted: int = 0) -> None:
        decision = resolution.get("decision")
        if not isinstance(decision, dict) or "model_calls" not in decision:
            return
        calls = decision.get("model_calls")
        calls_known = type(calls) is int and calls >= 0
        calls = calls if calls_known else 0
        cost = decision.get("cost_usd")
        known = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
        charged = decision.get("budget_charged_usd", cost)
        charge_known = type(charged) in (int, float) and math.isfinite(charged) and charged >= 0
        usage = trace.system_prompt_usage
        if not usage:
            usage.update(model_calls=0, remote_model_calls=0, model_calls_complete=True, cost_usd=0.0,
                         cost_complete=True, budget_charged_usd=0.0, budget_complete=True, decisions=[])
        usage["model_calls"] += calls
        usage["remote_model_calls"] += max(0, calls - locally_counted) if remote else 0
        usage["model_calls_complete"] = usage["model_calls_complete"] and calls_known
        usage["cost_complete"] = usage["cost_complete"] and (known or (calls_known and calls == 0))
        usage["budget_complete"] = usage["budget_complete"] and (charge_known or (calls_known and calls == 0))
        if charge_known:
            usage["budget_charged_usd"] += float(charged)
        if known:
            usage["cost_usd"] += float(cost)
            trace.cost_usd += float(cost)
        usage["decisions"].append({"route": "guest" if remote else "local", "model_calls": calls if calls_known else None,
                                   "cost_usd": float(cost) if known else None,
                                   "budget_charged_usd": float(charged) if charge_known else None,
                                   "provider": decision.get("provider"), "model": decision.get("model"),
                                   "budget_exhausted": bool(decision.get("budget_exhausted"))})

    def _budget_used(self, trace: T.Trace) -> float:
        usage = trace.system_prompt_usage
        return trace.cost_usd + max(0.0, usage.get("budget_charged_usd", 0.0) - usage.get("cost_usd", 0.0))

    def _prompt_decision_limits(self, trace: T.Trace, started: float, max_calls: int):
        if os.environ.get("ANYUSE_SYSTEM_PROMPT_MODE", "manual") != "llm":
            return None
        limit = self.config.max_usd
        return {"remaining_usd": max(0.0, limit - self._budget_used(trace)) if math.isfinite(limit) and limit > 0 else None,
                "remaining_calls": min(100000, max(0, max_calls - trace.decisions - trace.system_prompt_usage.get("model_calls", 0))),
                "timeout_s": min(86400.0, max(0.0, self.config.wall_s - (self.clock() - started)))}

    def _observation_for_decision(self, previous: Optional[T.Observation] = None, *,
                                  prompt_boundary: bool = True, trace: Optional[T.Trace] = None,
                                  decision_limits=None) -> T.Observation:
        """Resolve configured system prompts only at an explicit agent decision boundary.

        An attempted choice invalidates a tool's cached after-observation. This
        never retries an action, and the existing per-action guards still run.
        """
        resolution = {}
        sets = getattr(self.toolbox, "sets", {})
        redactor = self._redactor()
        task_context = redactor.text(self._task) if redactor is not None else self._task
        if decision_limits is not None and (decision_limits["remaining_calls"] == 0 or decision_limits["timeout_s"] == 0
                or decision_limits["remaining_usd"] == 0):
            prompt_boundary = False
        from anyuse.execution.model_adapter import process_call_count, calls_since
        prompt_mark = process_call_count()
        if prompt_boundary and "desktop" in sets:
            resolution = sets["desktop"].resolve_system_prompt("agent_harness.before_observation", task_context=task_context,
                                                               decision_limits=decision_limits)
        elif prompt_boundary and "rea" in sets:
            from anyuse.execution.system_prompt_policy import resolve_configured
            resolution = resolve_configured("agent_harness.before_observation", task_context=task_context,
                decision_limits=decision_limits, decision_deadline_monotonic=
                min(self._prompt_wall_deadline, self.clock() + decision_limits["timeout_s"]) if decision_limits is not None else None)
        if trace is not None:
            self._record_prompt_usage(trace, resolution, remote="desktop" in sets,
                                      locally_counted=calls_since(prompt_mark))
        invalidated = bool(resolution.get("attempted") or resolution.get("reobserve_required"))
        must_stop = resolution.get("next_action") == "stop"
        # Even an uncertain attempt consumes a fresh observation before stopping;
        # no target or partial action from the earlier screen can be resumed.
        try:
            obs = self.toolbox.observe() if invalidated or (previous is None and not must_stop) else previous
        except Exception as exc:
            if invalidated:
                raise _SystemPromptStop(resolution, "system prompt attempt requires a new observation; "
                                        f"fresh observation failed: {type(exc).__name__}: {exc}") from exc
            raise
        if must_stop:
            raise _SystemPromptStop(resolution, "system prompt requires manual handling: "
                                    + str(resolution.get("reason") or "stopped"))
        if invalidated:
            from anyuse.execution.harness_policies import ModelPolicy
            if type(self.policy) is not ModelPolicy:
                raise _SystemPromptStop(resolution, "system prompt choice invalidated pending targets; "
                                        "observe again and make a new policy decision before continuing")
            obs = _noted(obs, "A configured system-prompt choice was attempted. This is a fresh observation; "
                              "choose a new action from it. Earlier targets are invalid.")
        return obs

    def _stop_for_system_prompt(self, trace: T.Trace, exc: _SystemPromptStop) -> None:
        trace.result = exc.result
        self._stop(trace, T.Stop.UNCERTAIN if exc.result["reobserve_required"] else T.Stop.HUMAN_REQUIRED, str(exc))

    # -- the run ----------------------------------------------------------------------------
    def run(self, task: str, values: Optional[Dict[str, str]] = None, *, start_url: str = "",
            kind: str = "", _system_prompt_prepared: bool = False) -> T.Trace:
        from anyuse.execution.model_adapter import calls_since, process_call_count
        cfg = self.config
        values = {str(k): str(v) for k, v in (values or {}).items()}
        task_text = task
        for name, v in values.items():
            task_text = task_text.replace("{" + name + "}", v)
        trace = T.Trace(kind=kind or self.toolbox.kind, task=task_text, values=values,
                        start_url=start_url or self.toolbox.start_url, site=self.toolbox.site,
                        app=self.toolbox.app, bundle=self.toolbox.bundle, run_id=self.run_id,
                        harness_version=HARNESS_VERSION, workspace=self.workspace)
        from collections import Counter
        from anyuse.execution import commit_policy as CP
        from anyuse.execution.secret_refs import has_secret_ref
        self._task = task_text
        mandate = self.mandate or CP.Mandate.from_task(task_text, values)
        trace.mandate = mandate.to_dict()
        used: Counter = Counter()
        if hasattr(self.toolbox, "set_asker"):
            self.toolbox.set_asker(self._otp_bridge(trace))
        policy_task = task_text + ("\n(Credentials for this site are available to the login action; you never see the "
                                   "password.)" if has_secret_ref(values) else "")
        mark = process_call_count()
        t0 = self.clock()
        self._prompt_wall_deadline = t0 + cfg.wall_s
        history: List[str] = []
        repairs = 0
        corrections = 0
        # the two typed corrections given at most once each (a lookup that browses a menu before it searched;
        # a done without result rows on a page that lists results)
        searched, search_corrected, rows_corrected = False, False, False
        # (an agent that reads and scrolls one unchanged page until max_steps): consecutive read-only actions on one
        # page, and the one correction for it
        reads, read_url, reads_corrected = 0, "", False
        # the answer checks given once per kind (``answer_problem``), and the run's first observation
        answer_corrected: set = set()
        lookup = _lookup_values(values)
        same, last_key = 0, ""
        max_calls = cfg.max_calls or (cfg.max_steps + max(0, int(cfg.index_corrections or 0)))
        try:
            obs = self._observation_for_decision(trace=trace, prompt_boundary=not _system_prompt_prepared
                                                and cfg.max_steps > 0 and max_calls > 0,
                                                decision_limits=self._prompt_decision_limits(trace, t0, max_calls))
        except _SystemPromptStop as exc:
            self._stop_for_system_prompt(trace, exc)
            obs = None
        except Exception as exc:  # noqa: BLE001
            self._stop(trace, T.Stop.REFUSED, f"the first observation failed: {type(exc).__name__}: {exc}")
            obs = None
        browser = (getattr(self.toolbox, "sets", None) or {}).get("browser")
        if browser is not None and getattr(browser, "redirected", None):
            # a same-site redirect of the start page (site_canon): the page's own host is the trace's site
            trace.start_redirect = dict(browser.redirected)
            trace.site, trace.start_url = browser.site, browser.start_url
        k = -1
        start_obs = obs
        while obs is not None:
            k += 1
            if obs.blocked.startswith("start failed"):
                self._stop(trace, T.Stop.START_FAILED, f"step {k}: {obs.blocked}")
                break
            if obs.blocked.startswith("static runaway"):
                # the browser statics' runaway guard was hit - a typed stop, the site stopped
                self._stop(trace, T.Stop.STATIC_RUNAWAY, f"step {k}: {obs.blocked}")
                break
            if obs.blocked:
                self._stop(trace, T.Stop.CAPTCHA if obs.blocked.startswith("captcha") else T.Stop.BLOCKED,
                           f"step {k}: {obs.blocked} (never solved, never bypassed)"
                           if obs.blocked.startswith("captcha") else f"step {k}: {obs.blocked}")
                break
            if k >= cfg.max_steps:
                self._stop(trace, T.Stop.MAX_STEPS, f"{cfg.max_steps} steps")
                break
            if trace.decisions + trace.system_prompt_usage.get("model_calls", 0) >= max_calls:
                self._stop(trace, T.Stop.CALL_CAP, f"{max_calls} policy calls")
                break
            if cfg.max_usd <= 0 and getattr(self.policy, "calls_model", True):
                # 0 is a budget, not "no cap", as under the run budget and the daily cap: not one model call
                self._stop(trace, T.Stop.BUDGET, f"a budget of US$0 allows no model calls ({SETTING_MAX_USD})")
                break
            if self._budget_used(trace) >= cfg.max_usd > 0:
                self._stop(trace, T.Stop.BUDGET, f"US${trace.cost_usd:.4f} of US${cfg.max_usd:.4f}")
                break
            if self.clock() - t0 > cfg.wall_s:
                self._stop(trace, T.Stop.WALL_TIME, f"{cfg.wall_s:.0f} s")
                break
            # no progress: the same screen and no successful action for N steps
            key = obs.state_key()
            same = same + 1 if key == last_key else 0
            last_key = key
            if cfg.no_progress_steps and same >= cfg.no_progress_steps:
                self._stop(trace, T.Stop.NO_PROGRESS, f"the screen did not change for {same} steps")
                break
            # ONE policy call, ONE action
            c0 = self.clock()
            try:
                decision = self.policy.decide(policy_task, self._redact_obs(obs), list(history),
                                              self.toolbox.available())
            except Exception as exc:  # noqa: BLE001
                self._stop(trace, T.Stop.POLICY_ERROR, f"step {k}: {type(exc).__name__}: {exc}")
                break
            trace.decisions += 1
            trace.cost_usd += float(decision.cost_usd or 0.0)
            seconds = self.clock() - c0
            if decision.error and decision.action is None:
                self._stop(trace, T.Stop.POLICY_ERROR, f"step {k}: {decision.error}")
                break
            action = decision.action
            if action is None:
                why = decision.why or "no action"
                trace.steps.append(T.Step(k, obs, None, T.READ, T.Outcome(False, why), decision.text,
                                          decision.cost_usd, seconds))
                history.append(self._history_line(k, None, None, why))
                if repairs >= cfg.unreadable_repairs:
                    self._stop(trace, T.Stop.UNREADABLE, f"step {k}: {why}")
                    break
                repairs += 1
                # the ONE re-ask carries the parse error (two unreadable answers on a long results page were cut
                # off; element numbers keep it short)
                obs = _noted(obs, f"YOUR LAST ANSWER COULD NOT BE READ ({why[:200]}); it may have been cut off. Answer "
                                  f"with ONE short JSON action object only, e.g. {{\"action\": \"done\", \"result\": "
                                  f"{{\"results\": [{{\"element\": 12}}, {{\"element\": 15}}]}}}} (element numbers, "
                                  f"at most 10).")
                continue
            provider_stop = action.kind == T.Done.kind and browser is not None and \
                bool(getattr(browser, "refused_provider", "")) and _no_rows(action.result)
            if action.kind == T.Done.kind and obs.kind == "web" and lookup and corrections < cfg.index_corrections \
                    and not provider_stop:
                # (a "no results" after a refused search provider is the typed stop below, never a corrective note)
                kind_, note = answer_problem(action.result, obs, start_obs, lookup, searched)
                if kind_ and kind_ not in answer_corrected:
                    # a done answer the page cannot back gets ONE corrective
                    # note per kind, no step used (``answer_problem``)
                    answer_corrected.add(kind_)
                    corrections += 1
                    trace.corrections.append({"k": k, "action": action.to_dict(), "why": f"answer: {kind_}"})
                    history.append(f"(not run, no step used) {action}: {note[:160]}")
                    obs = _noted(obs, f"YOUR ANSWER WAS NOT TAKEN: {note}")
                    k -= 1
                    last_key = ""
                    continue
            rowless = False
            if action.kind == T.Done.kind and obs.kind == "web" and lookup and not rows_corrected and \
                    corrections < cfg.index_corrections:
                # the agent's own step: a question about the task is asked in the run's own scope
                with self._model_scope():
                    rowless = rowless_answer(action.result, task_text)
            if rowless:
                note = rows_note(obs)
                if note:
                    # (done {"search": ..., "url": ...} on a page that lists the
                    # results): a lookup's done without rows gets the page's result links ONCE, no step used
                    rows_corrected = True
                    corrections += 1
                    trace.corrections.append({"k": k, "action": action.to_dict(), "why": "done without result rows"})
                    history.append(f"(not run, no step used) {action}: the answer lists no result rows")
                    obs = _noted(obs, "YOUR ANSWER WAS NOT TAKEN: it lists no result rows. Answer {\"results\": "
                                      "[{\"element\": N}, ...]} with the numbers of the page's result links in page "
                                      "order, or {\"no_results\": true} if the page says nothing was found. " + note)
                    k -= 1
                    last_key = ""
                    continue
            if action.kind in (T.Click.kind, T.Open.kind) and obs.kind == "web" and lookup and not searched and \
                    not search_corrected and corrections < cfg.index_corrections:
                box = search_box_of(obs)
                target = (obs.mapping or {}).get(getattr(action, "index", None)) if isinstance(obs.mapping, dict) else None
                is_link = action.kind == T.Open.kind or (isinstance(target, dict) and target.get("src") == "el")
                if box and is_link:
                    # (an agent that clicks through the categories although the page
                    # has a search box; the compile then refuses a run that never searched): ONCE, no step used
                    search_corrected = True
                    corrections += 1
                    trace.corrections.append({"k": k, "action": action.to_dict(), "why": "browsing before searching"})
                    history.append(f"(not run, no step used) {action}: the page has a search box; search first")
                    obs = _noted(obs, f"YOUR LAST ACTION WAS NOT RUN: this page has a search box ({box}). Search the "
                                      f"site for the task's value with the search action first, instead of following a "
                                      f"menu or category link.")
                    k -= 1
                    last_key = ""
                    continue
            if action.kind in READ_ONLY_KINDS and obs.kind == "web":
                reads = reads + 1 if obs.url == read_url else 1
                read_url = obs.url
                if lookup and reads >= READS_BEFORE_ANSWER and not reads_corrected and \
                        corrections < cfg.index_corrections:
                    reads_corrected = True
                    corrections += 1
                    trace.corrections.append({"k": k, "action": action.to_dict(), "why": "reading one page again"})
                    history.append(f"(not run, no step used) {action}: this page was read already")
                    note = rows_note(obs)
                    obs = _noted(obs, "YOUR LAST ACTION WAS NOT RUN: you already read this page and it did not change. "
                                      "Answer from it now - done with {\"results\": [{\"element\": N}, ...]} (the "
                                      "numbers of its result links, in page order), or {\"no_results\": true} if it "
                                      "lists none - or take a different action. " + note)
                    k -= 1
                    last_key = ""
                    continue
            else:
                reads, read_url = 0, ""
            if action.kind == T.Search.kind:
                searched = True
            if action.kind in T.CONTROL:
                trace.steps.append(T.Step(k, obs, action, T.READ, T.Outcome(True), decision.text,
                                          decision.cost_usd, seconds))
                if action.kind == T.Done.kind and obs.kind == "web" and browser is not None and \
                        getattr(browser, "refused_provider", "") and _no_rows(action.result):
                    # the site's results come from a refused search provider - a
                    # "no results" answer is that, typed, never a claim that the site has none
                    self._stop(trace, T.Stop.PROVIDER_SEARCH, f"step {k}: {browser.refused_provider}", action)
                    break
                if action.kind == T.Done.kind:
                    trace.result = action.result
                    if obs.kind == "web":
                        # the answer's references (element numbers, cut URLs and titles) resolved against the page it
                        # was given on - ONE resolver for every consumer: the compile, the proof, the report, the
                        # fallback (answer_refs; cut URLs made the compile refuse 4 of 5 runs)
                        from anyuse.execution import answer_refs as AR
                        trace.result, trace.refs = AR.resolve_on_observation("", action.result, obs)
                    self._stop(trace, T.Stop.DONE, "the policy finished", action)
                elif action.kind == T.Fail.kind:
                    self._stop(trace, T.Stop.FAIL, action.reason or "the policy gave up", action)
                else:
                    self._stop(trace, T.Stop.ABSTAIN, action.reason or "the policy abstained", action)
                break
            # the effect class: declared by the tool, raised by the labels, never lowered
            effect = self.toolbox.effect_of(action, obs)
            # the tool set's own gate first (destination, robots, gov, a forbidden control, the
            # interlock, an element that is not there): what it refuses never reaches the effect policy
            refusal, hard = self.toolbox.gate(action, obs)
            if refusal and not hard and corrections < cfg.index_corrections and is_index_refusal(refusal):
                # an element that is not there (or not of that kind): a corrective observation, not a wasted step
                corrections += 1
                note = self.toolbox.correction(action, obs) if hasattr(self.toolbox, "correction") else ""
                trace.corrections.append({"k": k, "action": action.to_dict(), "why": refusal[:200]})
                history.append(f"(not run, no step used) {action}: {refusal[:160]}")
                obs = _noted(obs, f"YOUR LAST ACTION WAS NOT RUN: {refusal[:200]}. {note}".strip())
                k -= 1
                last_key = ""
                continue
            if refusal:
                outcome = T.Outcome(False, refusal)
                trace.steps.append(T.Step(k, obs, action, effect, outcome, decision.text, decision.cost_usd,
                                          seconds, held=T.needs_gate(effect)))
                history.append(self._history_line(k, action, outcome))
                if hard:
                    self._stop(trace, hard, f"step {k}: {refusal}", action)
                    break
                continue
            # then the commit policy: a step that commits something - a login, a cart, a form
            # sent, a payment, a deletion; an irreversible or unknown action of any tool set - is covered by the task,
            # asked of the caller, or a deviation that stops the run
            commit = self.toolbox.commit_of(action, obs) if hasattr(self.toolbox, "commit_of") else None
            if commit is None and T.needs_gate(effect):
                label = self.toolbox.target_label(action, obs)
                commit = CP.Commit(CP.class_of_label(label), label=label, action=action.to_dict())
            verdict = None
            if commit is not None:
                verdict, stop = self._commit_gate(trace, mandate, used, commit, k)
                if stop:
                    trace.stopped_action = action.to_dict()
                    trace.steps.append(T.Step(k, obs, action, effect, T.Outcome(False, trace.stop_reason),
                                              decision.text, decision.cost_usd, seconds, held=True,
                                              commit=commit.to_dict(), verdict=verdict.to_dict()))
                    break
                if hasattr(self.toolbox, "begin_commit"):
                    self.toolbox.begin_commit(action, commit)
            try:
                with self._model_scope():
                    outcome = self.toolbox.dispatch(action, obs)
            except Exception as exc:  # noqa: BLE001 - an error is an outcome, never a crash
                outcome = T.Outcome(False, f"{type(exc).__name__}: {exc}"[:300])
            finally:
                if commit is not None and hasattr(self.toolbox, "end_commit"):
                    self.toolbox.end_commit(action)
            if commit is not None and outcome.ok:
                used[commit.cls] += 1
                used[(commit.cls, CP.fold(commit.target))] += 1
            red = self._redactor()
            if red is not None and red.active:
                outcome.why = red.text(outcome.why)
            trace.steps.append(T.Step(k, obs, action, effect, outcome, decision.text, decision.cost_usd,
                                      seconds, held=T.needs_gate(effect) or commit is not None,
                                      commit=commit.to_dict() if commit is not None else None,
                                      verdict=verdict.to_dict() if verdict is not None else None))
            history.append(self._history_line(k, action, outcome))
            if action.kind == T.RunReaAction.kind and not outcome.ok:
                trace.result = outcome.data
                self._stop(trace, T.Stop.REFUSED if outcome.status == "rea_refused" else T.Stop.UNCERTAIN,
                           f"step {k}: {outcome.why}; REA failures are terminal", action)
                break
            if outcome.status in ("post_only_search", "off_site_search", "robots_search", "provider_search",
                                  "static_runaway", "no_site_search"):
                self._stop(trace, {"post_only_search": T.Stop.POST_ONLY_SEARCH, "off_site_search": T.Stop.OFF_SITE_SEARCH,
                                   "robots_search": T.Stop.ROBOTS_SEARCH,
                                   "provider_search": T.Stop.PROVIDER_SEARCH,
                                   "static_runaway": T.Stop.STATIC_RUNAWAY,
                                   "no_site_search": T.Stop.NO_SITE_SEARCH}[outcome.status],
                           f"step {k}: {outcome.why}", action)
                break
            if outcome.status == "otp_unanswered":
                self._stop(trace, T.Stop.OTP_UNANSWERED, f"step {k}: {outcome.why}; no answer means stop", action)
                break
            if outcome.status in ("prompt", "stop"):
                self._stop(trace, T.Stop.BLOCKED, f"step {k}: the guest reported {outcome.status}: {outcome.why}",
                           action)
                break
            if outcome.ok:
                same = -1                # progress resets the no-progress count
            try:
                obs = self._observation_for_decision(outcome.observation, trace=trace,
                    prompt_boundary=not _system_prompt_prepared and k + 1 < cfg.max_steps,
                    decision_limits=self._prompt_decision_limits(trace, t0, max_calls))
            except _SystemPromptStop as exc:
                self._stop_for_system_prompt(trace, exc)
                break
            except Exception as exc:  # noqa: BLE001
                self._stop(trace, T.Stop.REFUSED, f"step {k}: the observation failed: {type(exc).__name__}: {exc}")
                break
        if not trace.stop:
            self._stop(trace, T.Stop.MAX_STEPS, f"{cfg.max_steps} steps")
        trace.model_calls = calls_since(mark) + trace.system_prompt_usage.get("remote_model_calls", 0)
        trace.seconds = self.clock() - t0
        self._redact_trace(trace)
        return trace


#: the read-only actions, and how many of them on one unchanged page come before the correction
READ_ONLY_KINDS = (T.ReadText.kind, T.ReadTable.kind, T.Scroll.kind, T.ExtractFields.kind)
READS_BEFORE_ANSWER = 3


def _lookup_values(values: Dict[str, str]) -> str:
    """The value a LOOKUP run searches for (one value, no credential, no form fields), else "" (a flow: logging in,
    filling a form, a cart - its clicks are its job)."""
    from anyuse.execution.web_flow_compile import CREDENTIAL_NAMES
    vals = [(k, v) for k, v in (values or {}).items()]
    if len(vals) != 1 or vals[0][0].lower() in CREDENTIAL_NAMES or not str(vals[0][1]).strip():
        return ""
    return str(vals[0][1])


def _no_rows(result: Any) -> bool:
    """A list answer without rows: ``no_results``, or a dict with no results / rows (and no single item's facts)."""
    if not isinstance(result, dict):
        return False
    rows = result.get("results") if isinstance(result.get("results"), list) else result.get("rows")
    return bool(result.get("no_results")) or (not rows and not result.get("name") and not result.get("url"))


def rowless_answer(result: Any, task: str = "") -> bool:
    """A lookup's done whose result is a list answer (``harness_compile.workflow_of``: not one item's facts) but lists no
    rows and does not say no_results (``{"search": ..., "url": ...}``). During a run the check model is asked about the
    task only inside the scope the run holds open (``Harness._model_scope``), never in the compile scope."""
    from anyuse.execution import harness_compile as HC
    if not isinstance(result, dict):
        return False
    rows = result.get("results") if isinstance(result.get("results"), list) else result.get("rows")
    if rows or result.get("no_results"):
        return False
    return HC.workflow_of(T.Trace(kind="web", task=task, result=result), scope="") == "site_search"


def rows_note(obs: T.Observation, n: int = 30) -> str:
    """The page's links the resolver can take (``answer_refs.links_from_observation``), outside the header and footer,
    as ``[N] text``; "" when the page lists fewer than 2 (nothing to answer from)."""
    from anyuse.execution import answer_refs as AR
    from anyuse.execution import ax_tree as AXT
    page = obs.page if isinstance(obs.page, dict) else {}
    # a link the browser's tree puts in the page's chrome (navigation, banner, contentinfo, complementary)
    # is left out like the reader's header / nav / footer
    area_of_i = {e.get("i"): ("nav" if AXT.chrome(e) else str(e.get("area") or ""))
                 for e in page.get("elements") or [] if isinstance(e, dict)}
    mapping = obs.mapping if isinstance(obs.mapping, dict) else {}

    def area(idx: Optional[int]) -> str:
        return area_of_i.get((mapping.get(idx) or {}).get("i"), "") if idx is not None else ""
    links = [l for l in AR.links_from_observation(obs) if (l.name or l.text) and
             area(l.index) not in ("header", "nav", "footer")]
    if len(links) < 2:
        return ""
    return "The page's links: " + "; ".join(f"[{l.index}] {AR.shown(l.name or l.text, 60)}" for l in links[:n]) + "."


#: the elements a web policy is shown (``harness_policies.default_prompt`` renders the first 60)
SHOWN_ELEMENTS = 60
#: the answer checks (``answer_problem``), each switchable
ANSWER_CHECKS = ("unseen", "search_links", "offsite", "empty", "unchanged", "stated_count", "unstated_empty")
_CHROME_AREAS = ("header", "nav", "footer", "aside")


def _content_hrefs(obs: T.Observation) -> set:
    """The page's link targets outside its chrome (the reader's header / nav / footer / aside, the tree's landmarks)."""
    from anyuse.execution import answer_refs as AR
    from anyuse.execution import ax_tree as AXT
    page = obs.page if isinstance(obs.page, dict) else {}
    return {AR._norm(e.get("href")) for e in page.get("elements") or [] if isinstance(e, dict) and e.get("tag") == "a"
            and e.get("href") and str(e.get("href")).startswith("http") and AXT.area_of(e) not in _CHROME_AREAS
            and not AXT.chrome(e)}


def _search_keys(url: str, value: str) -> List[str]:
    """The query keys of ``url`` whose value carries the searched ``value`` (the page's own search parameter)."""
    import urllib.parse
    from anyuse.execution import web_arch as W
    v = W.fold(value)
    try:
        q = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return []
    return [k for k, x in q if v and v in W.fold(x)]


def answer_problem(result: Any, obs: T.Observation, start: Optional[T.Observation], value: str = "",
                   searched: bool = False, checks: Sequence[str] = ANSWER_CHECKS) -> Tuple[str, str]:
    """``(kind, note)`` when a lookup's done answer
    cannot be what the page lists, ``("", "")`` otherwise - read from the observation the answer was given on:

    * ``unseen`` - a row names an element the observation did not show (not on the page, or past the first
      ``SHOWN_ELEMENTS`` listed: rows #66-#75 of a list that showed fewer, or #1-#5 of a page that listed none);
    * ``search_links`` - a row's link re-runs the site's own search (the results page's own path with its search
      parameter, or the page itself with only a fragment: a "did you mean", a related search, a "more results" or a
      sort link);
    * ``unchanged`` - after a search, the page links nothing outside its chrome that the start page did not (the search
      changed nothing, and the answer is the start page's menu);
    * ``stated_count`` - "no results" while the page states a positive result count ("776 results found for
      knife");
    * ``offsite`` - a minority of rows leave the site (a partner shop's link);
    * ``unstated_empty`` - "no results" on a page that does not say so (the verifier's own statement rule,
      ``attribute_verify.verify_empty``: "no results" on a page whose results were never drawn);
    * ``empty`` - the page shows no element and no text (not drawn yet)."""
    from anyuse.execution import answer_refs as AR
    if not isinstance(result, dict) or obs is None or obs.kind != "web":
        return "", ""
    rows = [r for r in (result.get("results") or []) if isinstance(r, dict)] if isinstance(result.get("results"), list) \
        else []
    nums = [r.get("element") for r in rows if isinstance(r.get("element"), int)]
    mapping = obs.mapping if isinstance(obs.mapping, dict) else {}
    if "unseen" in checks and nums:
        shown = {e.index for e in obs.elements[:SHOWN_ELEMENTS]}
        missing = [n for n in nums if n not in mapping and n not in shown]
        unseen = [n for n in nums if n not in shown and n not in missing]
        if missing or unseen:
            bad = (missing + unseen)[:6]
            return "unseen", (f"element(s) {', '.join(map(str, bad))} " +
                              ("are not on this page" if missing else f"were not shown to you (the observation lists "
                               f"{min(len(obs.elements), SHOWN_ELEMENTS)} elements)") +
                              ". Answer only with the numbers of result links listed in the observation.")
    if "search_links" in checks and nums:
        import urllib.parse
        here = urllib.parse.urlsplit(obs.url or "")
        keys = set(_search_keys(obs.url or "", value))
        bad = []
        for n in nums:
            href = str((mapping.get(n) or {}).get("href") or "")
            if not href.startswith("http"):
                continue
            u = urllib.parse.urlsplit(href)
            same_page = AR._norm(href.split("#")[0]) == AR._norm((obs.url or "").split("#")[0])
            reruns = bool(keys) and u.netloc.lower() == here.netloc.lower() and u.path.rstrip("/") == \
                here.path.rstrip("/") and any(k in keys for k, _v in urllib.parse.parse_qsl(u.query, True))
            if same_page or reruns:
                bad.append(n)
        if bad:
            return "search_links", (f"element(s) {', '.join(map(str, bad[:6]))} open the site's own search again (a "
                                    f"suggestion, a related search, a 'more results', sort or page link), not a result. "
                                    f"Answer with the result links themselves.")
    if "offsite" in checks and len(nums) >= 3:
        import urllib.parse
        from anyuse.execution import site_canon as SC
        hrefs = [str((mapping.get(n) or {}).get("href") or "") for n in nums]
        off = [n for n, h in zip(nums, hrefs) if h.startswith("http") and SC.leaves_site(h, obs.url or "")]
        if off and len(off) * 2 < len(nums):
            return "offsite", (f"element(s) {', '.join(map(str, off[:6]))} leave the site while its other results do not "
                               f"(an ad, a partner shop, a social profile), not results of its search. Answer with the "
                               f"site's own result links.")
    if "empty" in checks and not obs.elements and not str(obs.text or "").strip():
        return "empty", ("the page shows nothing (no element, no text): it was not drawn, so it says nothing about the "
                         "search. Read it again or search differently - never answer from it.")
    if "unchanged" in checks and searched and start is not None and start is not obs:
        now, before = _content_hrefs(obs), _content_hrefs(start)
        if before and not (now - before):
            return "unchanged", ("this page links nothing that the page you started on did not: your search did not "
                                 "change it, so it shows no results for the search. Do not answer from it - search "
                                 "differently (another search box, the site's search page or its search URL).")
    if "stated_count" in checks and result.get("no_results") and not nums:
        from anyuse.execution import attribute_verify as AV
        st = AV.count_statement([ln for ln in str(obs.text or "").split("\n") if ln.strip()])
        if st and int(st["count"]) > 0:
            return "stated_count", (f"the page states {st['count']} results ({st['line'][:80]!r}), so 'no results' is "
                                    f"not what it says. Answer with its result links, or read why they are not listed.")
    if "unstated_empty" in checks and result.get("no_results") and not nums:
        from anyuse.execution import attribute_verify as AV
        e = AV.verify_empty(AV.Page("", str(obs.text or ""), obs.url or ""), value).decisions[0]
        if e.status != AV.VERIFIED:
            return "unstated_empty", ("the page does not say that the search found nothing (no 'no results' line), so "
                                      "'no results' is a guess. If its results did not appear, read it once more or "
                                      "search differently; if they cannot appear, fail with the reason.")
    return "", ""


def search_box_of(obs: T.Observation) -> str:
    """``[N] name`` of the observation's first search box, "" when it lists none."""
    for k, m in sorted((obs.mapping or {}).items()) if isinstance(obs.mapping, dict) else []:
        if (m or {}).get("src") == "input":
            return f"[{k}] {((m.get('input') or {}).get('name') or m.get('text') or 'search')}"
    return ""


#: the gate refusals that name an element that is not there or not of the asked kind
_INDEX_REFUSAL = re.compile(r"^element\(?s?\)? [\d, ]+ (is not on this page|is not a search box|is a search box, "
                            r"not a link or button|is a form field|is a drop-down|are not form fields on this page)")


def is_index_refusal(why: str) -> bool:
    return bool(_INDEX_REFUSAL.match(str(why or "")))


def _noted(obs: T.Observation, note: str) -> T.Observation:
    """The same observation with a note the policy reads first (a copy; the trace keeps the original)."""
    o = T.Observation(**{k: v for k, v in obs.__dict__.items()})
    o.note = (note + ("\n" + obs.note if obs.note else ""))[:4000]
    return o


# ---------------------------------------------------------------------------
# The per-task workspace (section 3, files)
# ---------------------------------------------------------------------------
def workspace_dir(run_id: str, base: Optional[str] = None) -> str:
    """``<data dir>/agent_runs/<run id>/`` with ``in/`` and ``out/``; created."""
    from anyuse.paths import anyuse_data_path
    root = base or str(anyuse_data_path("agent_runs"))
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:64] or "run"
    ws = os.path.join(root, safe)
    for sub in ("", "in", "out"):
        os.makedirs(os.path.join(ws, sub), exist_ok=True)
    return ws


def report(trace: T.Trace) -> Dict[str, Any]:
    """The short report of a run (what the CLI and the e2e print)."""
    return {"run_id": trace.run_id, "kind": trace.kind, "task": trace.task, "stop": trace.stop,
            "stop_reason": trace.stop_reason, "steps": len(trace.steps), "decisions": trace.decisions,
            "model_calls": trace.model_calls, "cost_usd": round(trace.cost_usd, 6),
            "seconds": round(trace.seconds, 3), "result": trace.result, "refs": dict(trace.refs),
            "effects": [s.effect for s in trace.steps if s.action is not None],
            "held": sum(1 for s in trace.steps if s.held), "stopped_action": trace.stopped_action,
            "commits": [{"step": s.k, **(s.commit or {}), "verdict": (s.verdict or {}).get("kind"),
                         "why": (s.verdict or {}).get("why"), "ok": s.outcome.ok}
                        for s in trace.steps if s.commit is not None],
            "questions": list(trace.questions), "mandate": trace.mandate,
            **({"system_prompt_usage": dict(trace.system_prompt_usage)} if trace.system_prompt_usage else {}),
            **({"rea_executions": [s.outcome.data for s in trace.steps
                                    if s.action is not None and s.action.kind == T.RunReaAction.kind]}
               if any(s.action is not None and s.action.kind == T.RunReaAction.kind for s in trace.steps) else {})}


def run_task(task: str, toolbox, policy, values: Optional[Dict[str, str]] = None, *,
             config: Optional[HarnessConfig] = None, approver: Optional[Approver] = None,
             start_url: str = "", asker=None, judge=None) -> T.Trace:
    """One run with the settings' configuration."""
    cfg = config or HarnessConfig.from_settings()
    return Harness(toolbox, policy, cfg, approver=approver, asker=asker, judge=judge).run(task, values,
                                                                                         start_url=start_url)
