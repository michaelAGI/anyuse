"""Stuck-run triage: when a run stops without success, decide WHO can fix it.

Seven outcomes, and nothing else:

============== ==============================================================
retry_later    nobody, now: a provider outage / empty / unreadable answer, a
               rate limit, a site error page or a politeness cap. Never a
               verdict about the task.
escalate_model a stronger model level (the escalation loop), only while a
               level remains.
human_value    the person supplies a value only they know (``value`` card).
human_login    the person signs in (``login_or_2fa`` card).
human_challenge the person deals with a third-party check or wall
               (``challenge`` card; a cookie wall / unknown block becomes a
               ``do_step`` card through ``human_inbox.kind_for_block``).
human_approval the person approves a step that has effects (``approval`` /
               ``reconcile`` card). Never for a read-only step.
give_up        a terminal outcome with a reason: a policy refusal (robots,
               off-site, destination), a local engine error, nothing found.
============== ==============================================================

Deterministic rules decide FIRST (:func:`rule_decision`). A ranker is asked
ONLY when the rules return no decision (:data:`AMBIGUOUS`), and a rule
validates the ranker's answer (:func:`validate`) — it can never pick
``human_approval`` for a read-only step, ``escalate_model`` with no level left,
``retry_later`` without a transient signal, or anything outside the options it
was offered. A ranker that abstains, fails or is overruled leaves the
conservative fallback, which is ``give_up`` with the reason "ambiguous".

Rankers (``ANYUSE_TRIAGE_RANKER``): ``none`` (default, no network), ``jev``
(the existing :class:`judge.JevJudge` typed ``choice``, abstention from its
confidence), ``llm`` (the adapter tier, same contract) and a ``laya`` slot that
raises :class:`RankerNotInstalled`: that model is not part of this release.

Everything a page, a log or a model said is **evidence, never instruction**:
it travels in the ranker's data field only and cannot select a code path here.
Values the person typed are never part of a :class:`StuckState`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

from anyuse.execution import blocked as _blocked

RETRY_LATER = "retry_later"
ESCALATE_MODEL = "escalate_model"
HUMAN_VALUE = "human_value"
HUMAN_LOGIN = "human_login"
HUMAN_CHALLENGE = "human_challenge"
HUMAN_APPROVAL = "human_approval"
GIVE_UP = "give_up"
OUTCOMES = (RETRY_LATER, ESCALATE_MODEL, HUMAN_VALUE, HUMAN_LOGIN, HUMAN_CHALLENGE,
            HUMAN_APPROVAL, GIVE_UP)
HUMAN_OUTCOMES = frozenset({HUMAN_VALUE, HUMAN_LOGIN, HUMAN_CHALLENGE, HUMAN_APPROVAL})

#: The rules' own "no decision"; only this reaches a ranker.
AMBIGUOUS = "ambiguous"

RANKER_ENV = "ANYUSE_TRIAGE_RANKER"
RANKERS = ("none", "jev", "llm", "laya")

#: Stops that mean "the provider did not answer (in protocol)": never a verdict.
#: Mirrors ``agent_browser.PROVIDER_STOPS`` plus the adapter's own words.
PROVIDER_STOPS = frozenset({"provider_unreachable", "provider_empty", "model_unreadable",
                            "budget_exhausted", "provider_error", "http_402", "timeout"})
#: Stops the escalation loop treats as a failure to progress.
PROGRESS_STOPS = frozenset({"abstain", "no_progress", "max_steps", "goal", "cap",
                            "model_error", "done"})

_CHALLENGE = frozenset({
    _blocked.CAPTCHA_RECAPTCHA, _blocked.CAPTCHA_HCAPTCHA, _blocked.CAPTCHA_TURNSTILE,
    _blocked.CAPTCHA_GENERIC, _blocked.CHALLENGE_INTERSTITIAL, _blocked.FORBIDDEN,
    _blocked.COOKIE_WALL, _blocked.UNKNOWN,
})
_TRANSIENT_BLOCKS = frozenset({_blocked.RATE_LIMITED, _blocked.ERROR_PAGE})

#: The TARGET itself is refused by policy: nobody should fix that.
_POLICY_TEXT = re.compile(r"robots_refused|robots\.txt|destination refused|"
                          r"redirect off the host|not an http\(s\) url|"
                          r"refus(ed|es) (this|the) request on (safety|policy)|"
                          r"violates? (the |our )?(usage |content )?polic", re.I)
#: The agent's own navigation was refused by the gate (escalation trigger
#: ``policy``, agent_browser.ended_on_refusal): another level may choose an
#: allowed path, exactly as the escalation loop's policy does.
_GATE_REFUSAL_TEXT = re.compile(r"ended off the (start )?site|chrome-error://|"
                                r"gate refused", re.I)
_BLOCK_CODE_TEXT = re.compile(r"\b(" + "|".join(sorted({
    _blocked.CAPTCHA_RECAPTCHA, _blocked.CAPTCHA_HCAPTCHA, _blocked.CAPTCHA_TURNSTILE,
    _blocked.CAPTCHA_GENERIC, _blocked.CHALLENGE_INTERSTITIAL, _blocked.RATE_LIMITED,
    _blocked.FORBIDDEN, _blocked.LOGIN_WALL, _blocked.COOKIE_WALL, _blocked.ERROR_PAGE,
    _blocked.UNKNOWN})) + r"):")
_TIMEOUT_TEXT = re.compile(r"TimeoutError|timed out|\btimeout\b", re.I)
_CAP_TEXT = re.compile(r"(request|host) cap of \d+|host cap .*reached", re.I)
_MISSING_INPUT = re.compile(r"missing (required )?input|required input .* (missing|unbound)|"
                            r"only \d+ value\(s\)|unbound (input|parameter)|"
                            r"needs? a value only", re.I)
_HTTP = re.compile(r"\bHTTP (\d{3})\b")
_CRASH_TEXT = re.compile(r"driver crash|Traceback|[A-Z][A-Za-z]+Error:|Exception", re.I)
_NOTHING_TEXT = re.compile(r"no candidate from the readers|no class rule|"
                           r"class inference disagreement|unresolved", re.I)
_LOGIN_TEXT = tuple(re.compile(p, re.I) for p in _blocked._LOGIN_TEXT + (
    r"\bsign[ -]?in required\b", r"\blog[ -]?in wall\b", r"two[- ]factor", r"\b2fa\b"))


@dataclass
class StuckState:
    """What is known about a stop, as data. No typed values, no secrets."""
    #: why the loop ended: an ``AgentRun.stop``, an escalation trigger, or an
    #: engine word (``crash``, ``cap``, ``handoff``, ``refused`` ...)
    stop: str = ""
    #: the escalation trigger, when the loop classified it
    trigger: str = ""
    #: a ``blocked.*`` reason code when a block page was detected
    blocked_reason: str = ""
    #: the engine's hand-off reason (approval / reconcile / human_step / blocked /
    #: human_only_host / value / precondition), when a routine stopped
    handoff_reason: str = ""
    #: ``read_only`` or a write class; empty when unknown
    effects: str = ""
    #: model levels left above the one that stopped
    levels_remaining: int = 0
    state_changed: bool = False
    #: declared inputs that are missing (names only, never values)
    missing_inputs: Tuple[str, ...] = ()
    #: the check's / error's own words; evidence only
    reason_text: str = ""
    host: str = ""
    retry_after_s: Optional[int] = None
    #: where this state came from (bench provenance); never used by a rule
    source: str = ""

    def evidence(self) -> Dict[str, Any]:
        """The bounded, value-free view a ranker may see."""
        d = asdict(self)
        d.pop("source", None)
        d["reason_text"] = str(d.get("reason_text") or "")[:300]
        d["missing_inputs"] = list(self.missing_inputs)[:10]
        return d

    # -- constructors from the engine's own records --------------------------

    @classmethod
    def from_agent_run(cls, run, levels_remaining: int = 0, *, blocked_reason: str = "",
                       trigger: str = "") -> "StuckState":
        """An :class:`agent_browser.AgentRun` that did not reach its goal."""
        return cls(stop=str(getattr(run, "stop", "") or ""), trigger=trigger,
                   blocked_reason=blocked_reason, effects="read_only",
                   levels_remaining=max(0, int(levels_remaining)),
                   state_changed=bool(getattr(run, "state_changed", False)),
                   reason_text=str(getattr(run, "error", "") or "")[:300],
                   host=_host(getattr(run, "final_url", "") or getattr(run, "start_url", "")))

    @classmethod
    def from_blocked(cls, state, effects: str = "") -> "StuckState":
        """A :class:`blocked.BlockedState`."""
        return cls(stop="blocked", blocked_reason=str(getattr(state, "reason", "") or ""),
                   effects=effects, host=str(getattr(state, "host", "") or ""),
                   retry_after_s=getattr(state, "retry_after_s", None),
                   reason_text="; ".join(getattr(state, "evidence", []) or [])[:300])

    @classmethod
    def from_engine_result(cls, result: Dict[str, Any],
                           step: Optional[Dict[str, Any]] = None) -> "StuckState":
        """A routine ``Engine`` result that is not ``completed``."""
        handoff = dict(result.get("handoff") or {})
        nxt = dict(handoff.get("next_step") or {})
        blk = dict(nxt.get("blocked") or {})
        effects = str(((step or {}).get("effects") or {}).get("class") or "")
        return cls(stop=str(result.get("status") or ""),
                   handoff_reason=str(handoff.get("reason") or ""),
                   blocked_reason=str(blk.get("reason") or ""), effects=effects,
                   host=str(blk.get("host") or ""),
                   reason_text=str(result.get("error") or "")[:300])


@dataclass
class Decision:
    outcome: str
    reason: str
    #: ``rule`` | ``ranker:<name>`` | ``fallback``
    decided_by: str = "rule"
    #: the rule's own id (stable, for the eval harness and logs)
    rule: str = ""
    ranker: Dict[str, Any] = field(default_factory=dict)

    def route(self) -> Dict[str, Any]:
        """Where this goes next: the inbox, the escalation loop, or the end."""
        if self.outcome in HUMAN_OUTCOMES:
            return {"to": "inbox", "card_kind": CARD_KIND[self.outcome]}
        if self.outcome == ESCALATE_MODEL:
            return {"to": "escalation"}
        return {"to": "terminal",
                "status": "retry_later" if self.outcome == RETRY_LATER else "gave_up"}

    def to_dict(self) -> Dict[str, Any]:
        out = {"outcome": self.outcome, "reason": self.reason[:240],
               "decided_by": self.decided_by, "rule": self.rule, "route": self.route()}
        if self.ranker:
            out["ranker"] = self.ranker
        return out


#: The inbox card kind each human outcome posts (``human_inbox.KINDS``).
CARD_KIND = {HUMAN_VALUE: "value", HUMAN_LOGIN: "login_or_2fa",
             HUMAN_CHALLENGE: "challenge", HUMAN_APPROVAL: "approval"}


def _host(url: str) -> str:
    import urllib.parse
    try:
        return urllib.parse.urlsplit(str(url)).netloc.lower()
    except ValueError:
        return ""


def _is_read_only(s: StuckState) -> bool:
    return s.effects == "read_only"


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------

def rule_decision(s: StuckState) -> Tuple[str, str, str]:
    """``(outcome | AMBIGUOUS, rule id, reason)``. Pure and deterministic."""
    stop = (s.stop or "").strip().lower()
    trig = (s.trigger or "").strip().lower()
    text = s.reason_text or ""

    # 1. the provider did not answer: never a verdict about the task
    if stop in PROVIDER_STOPS or trig in PROVIDER_STOPS:
        return RETRY_LATER, "provider", f"the model provider did not answer ({stop or trig})"

    # 2. the engine already typed the hand-off: keep its word
    h = (s.handoff_reason or "").strip().lower()
    if h == "approval":
        return HUMAN_APPROVAL, "handoff.approval", "the step needs the person's approval"
    if h == "reconcile":
        return HUMAN_APPROVAL, "handoff.reconcile", ("a write's outcome is uncertain; the "
                                                     "person says whether it happened")
    if h == "value" or s.missing_inputs or _MISSING_INPUT.search(text):
        names = ", ".join(s.missing_inputs[:5]) or "a value"
        return HUMAN_VALUE, "missing_input", f"a required input is missing ({names})"
    if h == "human_only_host":
        return HUMAN_CHALLENGE, "handoff.human_only_host", ("this host became human-only "
                                                           "after a challenge in this run")

    # 3. block pages, by kind (a detector's code may arrive only in the text)
    b = (s.blocked_reason or "").strip().lower()
    if not b:
        mb = _BLOCK_CODE_TEXT.search(text)
        b = mb.group(1) if mb else ""
    if b:
        if b == _blocked.LOGIN_WALL:
            return HUMAN_LOGIN, "blocked.login_wall", "the site needs a signed-in session"
        if b in _TRANSIENT_BLOCKS:
            wait = f" (asked to wait {s.retry_after_s}s)" if s.retry_after_s else ""
            return RETRY_LATER, f"blocked.{b}", f"the site is {b.replace('_', ' ')}{wait}"
        if b in _CHALLENGE:
            return HUMAN_CHALLENGE, f"blocked.{b}", ("the site asks for a human check; "
                                                     "AnyUse never gets around it")
        return HUMAN_CHALLENGE, "blocked.other", f"an unrecognised block ({b})"
    if h == "blocked":
        return HUMAN_CHALLENGE, "handoff.blocked", "the step hit a block page"

    # 4. a login wall named only in text
    if any(p.search(text) for p in _LOGIN_TEXT):
        return HUMAN_LOGIN, "text.login", "the page asks the person to sign in"

    # 5. policy refusal of the TARGET (robots / destination / a model's policy
    # refusal): nobody should fix it. A gate refusal of the agent's own
    # navigation is the escalation loop's ``policy`` trigger: another level
    # may take an allowed path, and when none remains it is a give-up.
    if _POLICY_TEXT.search(text):
        return GIVE_UP, "policy", "a policy refused the target (robots / destination / model policy)"
    if trig == "policy" or stop == "policy" or _GATE_REFUSAL_TEXT.search(text):
        if s.levels_remaining > 0:
            return ESCALATE_MODEL, "policy.gate", ("the policy gate refused the navigation the run "
                                                   f"ended on; {s.levels_remaining} level(s) remain")
        return GIVE_UP, "policy.gate", "the policy gate refused the navigation and no level remains"

    # 6. transient site or politeness limits
    m = _HTTP.search(text)
    if m:
        code = int(m.group(1))
        if code in (429,) or code >= 500:
            return RETRY_LATER, f"http.{code}", f"the site answered HTTP {code}"
        if code in (401,):
            return HUMAN_LOGIN, "http.401", "the site answered HTTP 401 (sign-in required)"
        if code == 403:
            return HUMAN_CHALLENGE, "http.403", "the site answered HTTP 403"
        return GIVE_UP, f"http.{code}", f"the site answered HTTP {code}"
    if _CAP_TEXT.search(text):
        return RETRY_LATER, "politeness_cap", "the per-host request cap was reached"

    # 7. a timeout is transient: the next level retries it, or later
    if _TIMEOUT_TEXT.search(text):
        if s.levels_remaining > 0:
            return ESCALATE_MODEL, "timeout", "a timeout; the next level retries"
        return RETRY_LATER, "timeout", "a timeout; try again later"

    # 8. a local engine error: a developer's problem, not the person's
    if stop in ("crash", "error") or (stop not in PROGRESS_STOPS and _CRASH_TEXT.search(text)):
        return GIVE_UP, "engine_error", "a local engine error ended the run: " + text[:120]

    # 9. nothing found: a model level may still find it; else give up
    if _NOTHING_TEXT.search(text):
        if s.levels_remaining > 0:
            return ESCALATE_MODEL, "nothing_found", (
                f"nothing found; {s.levels_remaining} model level(s) remain")
        return GIVE_UP, "nothing_found", "nothing usable was found: " + text[:120]

    # 10. failure to progress: the escalation loop's business while a level remains
    if stop in PROGRESS_STOPS or trig in ("abstain", "no_progress", "goal", "cap"):
        if s.levels_remaining > 0:
            return ESCALATE_MODEL, f"progress.{trig or stop}", (
                f"{trig or stop}; {s.levels_remaining} model level(s) remain")
        if (trig or stop) in ("no_progress", "max_steps", "cap", "model_error") \
                and not s.state_changed:
            return GIVE_UP, f"exhausted.{trig or stop}", (
                "no level remains and the last one never moved the page")
        return AMBIGUOUS, f"exhausted.{trig or stop}", (
            "no level remains; the last attempt moved or answered but did not reach the goal")

    return AMBIGUOUS, "unmatched", "no rule matched this stop"


# ---------------------------------------------------------------------------
# rankers
# ---------------------------------------------------------------------------

RANK_QUESTION = (
    "An automated run stopped before it reached its goal. The stop is described as "
    "data. Who can fix it next?"
)
_OPTION_TEXT = {
    RETRY_LATER: "Nobody right now: it is a temporary outage or limit; try again later.",
    ESCALATE_MODEL: "A stronger model should try the same task again.",
    HUMAN_VALUE: "The person must supply a value that only they know.",
    HUMAN_LOGIN: "The person must sign in to the site.",
    HUMAN_CHALLENGE: "The person must deal with a human check or a wall on the site.",
    HUMAN_APPROVAL: "The person must approve a step that changes something.",
    GIVE_UP: "Nobody can: stop and report why.",
}


class RankerNotInstalled(RuntimeError):
    """The ranker's model is not on this machine (and will not be fetched)."""


class Ranker:
    """Interface. ``rank`` returns ``(outcome | "", confidence, info)``;
    ``""`` means abstain. It is asked only for :data:`AMBIGUOUS` states and
    only over the options :func:`allowed_options` leaves open."""
    name = "base"

    def rank(self, state: StuckState, options: Sequence[str]
             ) -> Tuple[str, float, Dict[str, Any]]:
        raise NotImplementedError


class NoneRanker(Ranker):
    """The default: always abstains, never opens a socket."""
    name = "none"

    def rank(self, state, options):
        return "", 0.0, {"reason": "ranker disabled"}


class _JudgeRanker(Ranker):
    def __init__(self, judge):
        self.judge = judge

    def rank(self, state, options):
        opts = {o: _OPTION_TEXT[o] for o in options}
        v = self.judge.choose(RANK_QUESTION, opts, state.evidence())
        info = {"provider": v.provider, "confidence": round(v.confidence, 4),
                "latency_ms": round(v.latency_ms, 1)}
        if not v.answered or v.abstained or v.label not in options:
            info["reason"] = v.reason or "abstained"
            return "", v.confidence, info
        return v.label, v.confidence, info


class JevRanker(_JudgeRanker):
    """The existing :class:`judge.JevJudge` typed choice (abstain allowed)."""
    name = "jev"

    def __init__(self, judge=None, **kw):
        from anyuse.execution.judge import JevJudge
        super().__init__(judge or JevJudge(**kw))


class LlmRanker(_JudgeRanker):
    """The OpenAI-compatible adapter tier through :class:`judge.LlmJudge`."""
    name = "llm"

    def __init__(self, judge=None, **kw):
        from anyuse.execution.judge import LlmJudge
        super().__init__(judge or LlmJudge(**kw))


class LayaRanker(Ranker):
    """A SLOT for a local Laya model, which is not part of this release: it
    refuses and nothing is fetched."""
    name = "laya"

    def __init__(self, **_kw):
        raise RankerNotInstalled(
            "the 'laya' triage ranker is not installed: it is not part of this release. "
            "Use ANYUSE_TRIAGE_RANKER=none, jev or llm.")

    def rank(self, state, options):   # pragma: no cover - never constructed
        raise RankerNotInstalled("laya ranker is not installed")


def ranker_mode(explicit: Optional[str] = None) -> str:
    try:
        from anyuse.settings import get as get_setting
        raw = str(get_setting(RANKER_ENV, explicit) or "")
    except Exception:
        raw = str(explicit or "")
    raw = raw.strip().lower()
    return raw if raw in RANKERS else "none"


def get_ranker(mode: Optional[str] = None, **kw) -> Ranker:
    m = ranker_mode(mode)
    if m == "jev":
        return JevRanker(**kw)
    if m == "llm":
        return LlmRanker(**kw)
    if m == "laya":
        return LayaRanker(**kw)
    return NoneRanker()


# ---------------------------------------------------------------------------
# the validating rule
# ---------------------------------------------------------------------------

def allowed_options(s: StuckState) -> Tuple[str, ...]:
    """What a ranker may choose for this state. Always includes ``give_up``."""
    out = [GIVE_UP, HUMAN_VALUE, HUMAN_LOGIN, HUMAN_CHALLENGE]
    if not _is_read_only(s) and s.effects:
        out.append(HUMAN_APPROVAL)
    if s.levels_remaining > 0:
        out.append(ESCALATE_MODEL)
    if s.retry_after_s or (s.stop or "").lower() in PROVIDER_STOPS:
        out.append(RETRY_LATER)
    return tuple(o for o in OUTCOMES if o in out)


def validate(s: StuckState, outcome: str) -> Tuple[bool, str]:
    """A rule over the ranker's answer. ``(ok, why_not)``."""
    if outcome not in OUTCOMES:
        return False, f"unknown outcome {outcome!r}"
    if outcome == HUMAN_APPROVAL and (_is_read_only(s) or not s.effects):
        return False, "never human_approval for a read-only (or unclassified) step"
    if outcome == ESCALATE_MODEL and s.levels_remaining <= 0:
        return False, "never escalate when no model level remains"
    if outcome == RETRY_LATER and not (s.retry_after_s or
                                       (s.stop or "").lower() in PROVIDER_STOPS):
        return False, "retry_later needs a transient signal the rules did not see"
    if outcome not in allowed_options(s):
        return False, f"{outcome} was not offered for this state"
    return True, ""


def decide(s: StuckState, ranker: Optional[Ranker] = None) -> Decision:
    """Rules first; a ranker only for the ambiguous rest; a rule validates it."""
    outcome, rule, why = rule_decision(s)
    if outcome != AMBIGUOUS:
        return Decision(outcome, why, "rule", rule)
    fallback = Decision(GIVE_UP, f"ambiguous ({why}); nothing automatic is safe",
                        "fallback", rule)
    ranker = ranker if ranker is not None else NoneRanker()
    if isinstance(ranker, NoneRanker):
        return fallback
    options = allowed_options(s)
    try:
        label, conf, info = ranker.rank(s, options)
    except RankerNotInstalled:
        raise
    except Exception as exc:              # a ranker failure is an abstention
        label, conf, info = "", 0.0, {"reason": type(exc).__name__}
    info = {"name": ranker.name, **info}
    if not label:
        fallback.ranker = {**info, "abstained": True}
        return fallback
    ok, why_not = validate(s, label)
    if not ok:
        fallback.ranker = {**info, "label": label, "overruled": why_not}
        return fallback
    return Decision(label, f"{why}; ranker chose {label} (confidence {conf:.2f})",
                    f"ranker:{ranker.name}", rule, {**info, "label": label})
