"""Demotion and recompilation of a compiled ``api`` rung.

A compiled implementation is a claim about somebody else's endpoint, and
endpoints change. The ladder already handles that *softly*: a failing api rung
loses success rate, ``deterministic.rank_implementations`` puts the proven UI
rung first, and replay keeps working. What was missing is the loud half — noticing
that the claim has gone stale, trying to make a new one from the stored recording,
and, when that fails, taking the api rung out of the ladder and saying why.

Three pure pieces plus one manager:

* :class:`ApiHealth` — the per-workflow record of how the compiled rung is
  doing. It lives inside ``compiled_api.json`` next to the template and the
  original proof, so the evidence and the wear stay together.
* :func:`record_outcome` — fold one replay result into that health. Pure.
* :func:`should_recompile` — the policy. Pure.
* :class:`RecompileManager` — loads the stored recording, re-runs
  ``compile_and_verify``, and persists the new template ONLY if the fresh-input
  proof passes. A recompile that cannot prove itself does not replace anything;
  it demotes the rung and records the reason.
* :meth:`RecompileManager.recompile_now` — the automatic path the runtime cascade
  takes (``mcpserver/compiled_tools.run_compiled``) once a lower rung answered for a
  request it took out of service; paced by :func:`recompile_allowed`.

A call the site did not answer (a server error, a timeout, a refused connection) says
nothing about the request: :func:`record_outage` folds it without counting a failure and
sets how long the request waits before it is sent again. A request out of service is
sent once more after :func:`next_probe_at` (a probe, :func:`claim_probe`); when it
answers and passes its checks it goes back in service (:func:`restore`), otherwise the
next probe waits twice as long (:func:`note_probe`).

A request that keeps failing that way while the site itself still answers was taken away
behind a server error: after ``gone_after_outages`` such calls over ``gone_after_s``
(:func:`gone_check_due`), one GET of the site's own page, at most once per
``gone_after_s``, or a lower rung that answered, says the site is up, and the request is
taken out of service as gone (:func:`mark_gone`, cause ``gone``). It then heals or
recompiles like a request that gave wrong answers.

Nothing here actuates, captures or touches the desktop; the only I/O is reading
and writing the workflow's own files, and the verification requests the
verifier makes through an injectable executor.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RecompilePolicy:
    """When a compiled rung is considered stale.

    Two independent triggers, because they catch different failures: a run of
    consecutive failures catches an endpoint that died outright, while a success
    rate sagging over a window catches one that became flaky or started
    returning a changed shape for some inputs only.
    """
    consecutive_failures: int = 3
    min_success_rate: float = 0.5
    min_samples: int = 5
    #: how many recompile attempts may be made before the rung is left demoted
    max_attempts: int = 3
    #: automatic recompiles: the least time between two attempts on one tool
    min_interval_s: float = 3600.0
    #: a request out of service is sent again (a probe) this long after it was taken out,
    #: twice as long after each probe that fails, at most ``probe_max_s``
    probe_after_s: float = 600.0
    probe_max_s: float = 86400.0
    #: after the second call in a row the site did not answer, the request is not sent for
    #: this long, twice as long for each further one, at most ``outage_max_s``; a Retry-After
    #: the site sent replaces it, from the first such call on (at most ``probe_max_s``)
    outage_wait_s: float = 30.0
    outage_max_s: float = 900.0
    #: a request the site has not answered this many calls in a row, over at least this long,
    #: while its own pages answer, is taken out of service as gone; the site's page is read
    #: for that at most once per ``gone_after_s``
    gone_after_outages: int = 4
    gone_after_s: float = 1800.0


#: the previous compile record, kept beside ``compiled_api.json`` when a recompile replaces it
PREVIOUS_FILE = "compiled_api.previous.json"
#: ``flock`` target beside ``compiled_api.json`` (see :func:`workflow_lock`)
LOCK_FILE = "compiled_api.lock"


class NoEvidence(LookupError):
    """A recording loader found nothing to compile from. ``spent``: it loaded a page or sent a
    request on the way. An automatic attempt that spent nothing is not counted."""

    def __init__(self, reason: str, spent: bool = True):
        super().__init__(reason)
        self.spent = spent


@contextlib.contextmanager
def workflow_lock(storage, workflow_id: str) -> Iterator[None]:
    """An exclusive ``flock`` on one workflow's compile record. ``flock`` locks belong to the
    open file, so two threads of one process exclude each other too. Not re-entrant."""
    path = os.path.join(str(storage.get_workflow_dir(workflow_id)), LOCK_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        import fcntl
    except ImportError:                      # no flock on this platform: unlocked
        fcntl = None
    with open(path, "a") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _same_request(a: dict, b: dict) -> bool:
    return (json.dumps(a.get("template"), sort_keys=True)
            == json.dumps(b.get("template"), sort_keys=True))


def _labelled_keys(record: dict) -> bool:
    """May a recompile of ``record`` name an item page's extract keys after the
    page's labels? Not when the stored template's keys are the positional
    ``field_N``: the tool keeps the output names it was published with."""
    template = record.get("template") or {}
    steps = template.get("steps") if isinstance(template, dict) else None
    # a stored chain's step holds its request's fields itself (``PlanStep.to_dict``)
    last = (steps[-1] or {}) if steps else template
    keys = list(((last or {}).get("extract") or {}).keys())
    return not (keys and all(re.fullmatch(r"field_\d+", str(k)) for k in keys))


def save_call_outcome(storage, workflow_id: str, record: dict) -> bool:
    """Persist one call's health (``mcpserver/compiled_tools.run_compiled``) under the workflow
    lock. The stored automatic-recompile and heal state is kept over the call's snapshot, and a
    request swapped in while the call ran is left alone (the outcome was about the old one):
    False."""
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    with workflow_lock(storage, workflow_id):
        stored = load_compiled(storage, workflow_id)
        if stored is not None:
            if not _same_request(stored, record):
                return False
            ours = ApiHealth.from_dict(record.get("health"))
            theirs = ApiHealth.from_dict(stored.get("health"))
            ours.recompile_attempts = theirs.recompile_attempts
            ours.last_recompile_at = theirs.last_recompile_at
            record["health"] = ours.to_dict()
            # execution/step_heal.py writes heal_guard and heal under this lock, restore() restored
            for key in ("recompile", "recompiled", "heal_guard", "heal", "healed", "restored"):
                if key in stored:
                    record[key] = stored[key]
            # an empty page another call learned meanwhile is kept beside this call's
            pages = {**(stored.get("empty_page") or {}), **(record.get("empty_page") or {})}
            if pages:
                record["empty_page"] = pages
        persist_compiled(storage, workflow_id, record)
    return True


@dataclass
class ApiHealth:
    """How the compiled rung has actually been doing."""
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0
    last_error: str = ""
    last_outcome_at: str = ""
    #: False → the ladder should not offer this rung at all (see
    #: :func:`apply_to_graph`); the recorded UI path stays preferred.
    preferred: bool = True
    demoted_reason: str = ""
    recompile_attempts: int = 0
    last_recompile_at: str = ""
    #: calls the site did not answer (:func:`record_outage`); never counted as failures
    consecutive_outages: int = 0
    total_outages: int = 0
    last_outage: str = ""
    #: after such a call the request is not sent before this time ("" = no wait)
    retry_after: str = ""
    #: when the request was taken out of service, and when it is sent again to see whether
    #: it answers (:func:`next_probe_at`)
    demoted_at: str = ""
    probe_after: str = ""
    probe_failures: int = 0
    #: why the last probe failed, whether the site did not answer it, and whether the site
    #: asked AnyUse to wait (a rate limit, also counted as not answering)
    last_probe: str = ""
    last_probe_outage: bool = False
    last_probe_rate_limited: bool = False
    #: the first call of the current run the site did not answer, when the site's own page was
    #: last read to see whether the site answers, and why the request was taken out as gone
    first_outage_at: str = ""
    last_site_check: str = ""
    endpoint_removed: str = ""

    @property
    def success_rate(self) -> float:
        if not self.total_runs:
            return 0.0
        return (self.total_runs - self.total_failures) / self.total_runs

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ApiHealth":
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _at(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


def _now() -> str:
    return _at(time.time())


def record_outcome(health: ApiHealth, success: bool, error: str = "") -> ApiHealth:
    """Fold one replay result into the health record. Pure — returns a new one."""
    out = ApiHealth.from_dict(health.to_dict())
    out.total_runs += 1
    out.last_outcome_at = _now()
    # the site answered: a wait after calls it did not answer is over
    out.consecutive_outages = 0
    out.retry_after = ""
    out.first_outage_at = ""
    out.last_site_check = ""
    out.endpoint_removed = ""
    if success:
        out.consecutive_failures = 0
        out.last_error = ""
    else:
        out.consecutive_failures += 1
        out.total_failures += 1
        out.last_error = (error or "")[:300]
    return out


#: the executor's own words for a failure the site did not answer, read last: a transport
#: message without the ``transport error`` prefix (a browser rung's "Page.navigate timed out",
#: "connection lost"), on a failure that carries no status
_OUTAGE_WORDS = ("timed out", "timeout", "connection", "connect error", "temporarily",
                 "infrastructure")
#: text a message quotes, from a page or a response: never read for the words above. A
#: straight quote opens and closes only outside a word, so the apostrophe of "couldn't" is
#: not one
_QUOTED = re.compile(r"(?<!\w)'[^']*'(?!\w)|\"[^\"]*\"|“[^”]*”|‘[^’]*’|«[^»]*»|„[^“”]*[“”]")
#: what marks a failure the site answered, or one AnyUse refused before sending
_ANSWERED_FLAGS = ("blocked", "blocked_state", "requires_human", "requires_approval",
                   "robots_refused")


def _status_of(status: Any) -> Optional[int]:
    return status if isinstance(status, int) and not isinstance(status, bool) and status > 0 \
        else None


def site_did_not_answer(status: Any, error: str, outcome: Optional[Dict[str, Any]] = None
                        ) -> bool:
    """A failure that says nothing about the request itself: a server error, a timeout, a
    refused connection, a name that did not resolve.

    From the structured outcome first (``status``; ``outcome``, the executor's result when
    the caller has it): an HTTP status from the site decides (5xx: it did not answer; any
    other: it answered, whatever its error text quotes from the page); a block, a robots
    refusal or a request held for a person is an answer, or nothing was sent; a pre-send
    refusal (which the executor also marks as a block) counts only for a name that did not
    resolve. Then the executor's own message: a ``transport error`` (the transport raised)
    always counts. The words of :data:`_OUTAGE_WORDS` are the last rung, read outside
    quotes."""
    o = outcome if isinstance(outcome, dict) else {}
    code = _status_of(status) or _status_of(o.get("status"))
    if code is not None:
        return code >= 500
    err = str(error or o.get("error") or "").lower()
    if "pre-send policy refused" in err:
        return "resolution failed" in err
    if any(o.get(flag) for flag in _ANSWERED_FLAGS):
        return False
    if err.startswith("transport error"):
        return True
    own = _QUOTED.sub(" ", err)
    if re.search(r"\bstatus 5\d\d\b", own):
        return True
    return any(word in own for word in _OUTAGE_WORDS)


def _doubling(base: float, count: int, cap: float) -> float:
    return min(base * 2 ** max(0, min(int(count), 30)), cap)


def record_outage(health: ApiHealth, error: str = "", retry_after_s: Optional[float] = None,
                  policy: Optional[RecompilePolicy] = None,
                  now: Optional[float] = None) -> ApiHealth:
    """Fold one call the site did not answer. It is not a failure of the request, so it never
    counts towards taking it out of service; the request waits before it is sent again: the
    site's Retry-After when it sent one, else nothing after the first such call (a blip) and
    ``outage_wait_s`` doubling from the second in a row. Pure."""
    policy = policy or RecompilePolicy()
    now = time.time() if now is None else now
    out = ApiHealth.from_dict(health.to_dict())
    out.consecutive_outages += 1
    out.total_outages += 1
    out.last_outage = (error or "")[:300]
    if out.consecutive_outages == 1 or not out.first_outage_at:
        out.first_outage_at = _at(now)
    if isinstance(retry_after_s, (int, float)) and not isinstance(retry_after_s, bool) \
            and retry_after_s >= 0:
        wait = min(float(retry_after_s), policy.probe_max_s)
    else:
        wait = (0.0 if out.consecutive_outages < 2 else
                _doubling(policy.outage_wait_s, out.consecutive_outages - 2, policy.outage_max_s))
    out.retry_after = _at(now + wait) if wait > 0 else ""
    return out


def outage_wait(health: ApiHealth, now: Optional[float] = None) -> str:
    """The time before which the request is not sent after the site did not answer, or ""."""
    until = _timestamp(health.retry_after)
    now = time.time() if now is None else now
    return health.retry_after if until is not None and until > now else ""


def demote(health: ApiHealth, why: str, policy: Optional[RecompilePolicy] = None,
           now: Optional[float] = None) -> ApiHealth:
    """Take the request out of service with ``why``; its first probe is ``probe_after_s``
    later. Pure."""
    policy = policy or RecompilePolicy()
    now = time.time() if now is None else now
    out = ApiHealth.from_dict(health.to_dict())
    if out.preferred or not out.demoted_at:
        out.demoted_at = _at(now)
        out.probe_failures = 0
        out.probe_after = _at(now + policy.probe_after_s)
        out.last_probe, out.last_probe_outage = "", False
        out.last_probe_rate_limited = False
    out.preferred = False
    out.demoted_reason = why
    return out


def gone_check_due(health: ApiHealth, policy: Optional[RecompilePolicy] = None,
                   now: Optional[float] = None) -> bool:
    """Should this call find out whether the site still answers? The request is in service,
    the site has not answered ``gone_after_outages`` calls in a row over at least
    ``gone_after_s``, and the site's page was not read for that in the last ``gone_after_s``.
    Pure."""
    policy = policy or RecompilePolicy()
    now = time.time() if now is None else now
    first = _timestamp(health.first_outage_at)
    last = _timestamp(health.last_site_check)
    return (health.preferred and health.consecutive_outages >= policy.gone_after_outages
            and first is not None and now - first >= policy.gone_after_s
            and (last is None or now - last >= policy.gone_after_s))


def note_site_check(health: ApiHealth, now: Optional[float] = None) -> ApiHealth:
    """The site's page was read now to see whether the site answers. Pure."""
    out = ApiHealth.from_dict(health.to_dict())
    out.last_site_check = _at(time.time() if now is None else now)
    return out


def mark_gone(health: ApiHealth, why: str, policy: Optional[RecompilePolicy] = None,
              now: Optional[float] = None) -> ApiHealth:
    """Take a request the site no longer answers, while the site itself does, out of service
    (:func:`demote`); :func:`demotion_cause` then says ``gone``. Pure."""
    out = demote(health, why, policy, now)
    out.endpoint_removed = (why or "the request is gone")[:300]
    return out


def next_probe_at(health: ApiHealth, policy: Optional[RecompilePolicy] = None) -> str:
    """When a request out of service is next sent to see whether it answers again: ``""``
    while it is in service, and for a record that says neither when it was taken out nor
    when it last ran (its first call schedules the probe, :func:`claim_probe`)."""
    if health.preferred:
        return ""
    if health.probe_after:
        return health.probe_after
    policy = policy or RecompilePolicy()
    start = next((t for t in (_timestamp(health.demoted_at), _timestamp(health.last_outcome_at),
                              _timestamp(health.last_recompile_at)) if t is not None), None)
    if start is None:
        return ""
    return _at(start + _doubling(policy.probe_after_s, health.probe_failures, policy.probe_max_s))


def probe_due(health: ApiHealth, policy: Optional[RecompilePolicy] = None,
              now: Optional[float] = None) -> bool:
    """Is a request out of service due to be sent again? Pure."""
    at = _timestamp(next_probe_at(health, policy))
    now = time.time() if now is None else now
    return at is not None and at <= now


def demotion_cause(health: ApiHealth) -> str:
    """Why a request is out of service, for what the call says next: ``rate_limit`` when the
    site asked AnyUse to wait at the last probe, ``gone`` when it was taken out because it kept
    failing while the site answered (:func:`mark_gone`), ``outage`` when the site did not
    answer (the last probe, else the failures that took it out, on a record written before
    calls the site did not answer stopped counting), else ``answers``."""
    if health.endpoint_removed and not health.last_probe_rate_limited:
        return "gone"
    if health.last_probe:
        if health.last_probe_rate_limited:
            return "rate_limit"
        return "outage" if health.last_probe_outage else "answers"
    return "outage" if site_did_not_answer(None, health.last_error) else "answers"


def claim_probe(storage, workflow_id: str, ran: dict, policy: Optional[RecompilePolicy] = None,
                now: Optional[float] = None) -> Tuple[bool, ApiHealth]:
    """``(claimed, health)``: may this call send the out-of-service request ``ran`` once, as a
    probe? Under the workflow lock the next probe moves one window ahead before anything is
    sent, so parallel calls send one probe. A record with no time to count from gets its
    first probe scheduled instead. Not claimed when the stored request is another one or is
    in service again."""
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    policy = policy or RecompilePolicy()
    now = time.time() if now is None else now
    seen = ApiHealth.from_dict(ran.get("health"))
    if seen.preferred or (next_probe_at(seen, policy) and not probe_due(seen, policy, now)):
        # nothing to claim or schedule: no lock for a call that only skips the request
        return False, seen
    with workflow_lock(storage, workflow_id):
        stored = load_compiled(storage, workflow_id)
        if stored is None or not _same_request(stored, ran):
            return False, seen
        health = ApiHealth.from_dict(stored.get("health"))
        if health.preferred:
            return False, health
        due = probe_due(health, policy, now)
        if next_probe_at(health, policy) and not due:
            return False, health
        # a probe now, or the first one scheduled: the next one is a window ahead either way
        health.probe_after = _at(now + _doubling(policy.probe_after_s, health.probe_failures,
                                                 policy.probe_max_s))
        stored["health"] = health.to_dict()
        persist_compiled(storage, workflow_id, stored)
        return due, health


def note_probe(storage, workflow_id: str, ran: dict, error: str, outage: bool,
               policy: Optional[RecompilePolicy] = None,
               now: Optional[float] = None,
               retry_after_s: Optional[float] = None,
               rate_limited: bool = False) -> ApiHealth:
    """A probe that failed: the next one waits twice as long as the last, at most
    ``probe_max_s``, and at least the Retry-After the site sent with it. ``rate_limited``:
    the site asked AnyUse to wait (:func:`demotion_cause` says ``rate_limit``)."""
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    policy = policy or RecompilePolicy()
    now = time.time() if now is None else now
    with workflow_lock(storage, workflow_id):
        stored = load_compiled(storage, workflow_id)
        if stored is None or not _same_request(stored, ran):
            return ApiHealth.from_dict(ran.get("health"))
        health = ApiHealth.from_dict(stored.get("health"))
        if health.preferred:
            return health
        health.probe_failures += 1
        wait = _doubling(policy.probe_after_s, health.probe_failures, policy.probe_max_s)
        if isinstance(retry_after_s, (int, float)) and not isinstance(retry_after_s, bool):
            wait = max(wait, min(float(retry_after_s), policy.probe_max_s))
        health.probe_after = _at(now + wait)
        health.last_probe = (error or "the probe failed")[:300]
        health.last_probe_outage = bool(outage)
        health.last_probe_rate_limited = bool(rate_limited)
        stored["health"] = health.to_dict()
        persist_compiled(storage, workflow_id, stored)
        return health


def mark_restored(record: dict, why: str, by: str) -> dict:
    """Put an out-of-service request back in service in ``record`` and note when and why
    (``record["restored"]``). ``by``: ``probe`` or ``heal``."""
    health = ApiHealth.from_dict(record.get("health"))
    if health.preferred:
        return record
    record["restored"] = {"at": _now(), "by": by, "why": str(why or "")[:300],
                          "was": health.demoted_reason[:300], "demoted_at": health.demoted_at,
                          "probes_failed": health.probe_failures}
    health.preferred = True
    health.demoted_reason = ""
    health.consecutive_failures = 0
    health.last_error = ""
    health.consecutive_outages = 0
    health.retry_after = ""
    health.demoted_at = ""
    health.probe_after = ""
    health.probe_failures = 0
    health.last_probe, health.last_probe_outage = "", False
    health.last_probe_rate_limited = False
    health.first_outage_at = health.last_site_check = health.endpoint_removed = ""
    record["health"] = health.to_dict()
    return record


def restore(storage, workflow_id: str, ran: dict, why: str, by: str = "probe") -> Optional[dict]:
    """:func:`mark_restored` on the stored record under the workflow lock; the stored record,
    or None when the stored request is not the one ``ran``."""
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    with workflow_lock(storage, workflow_id):
        stored = load_compiled(storage, workflow_id)
        if stored is None or not _same_request(stored, ran):
            return None
        if not ApiHealth.from_dict(stored.get("health")).preferred:
            mark_restored(stored, why, by)
            persist_compiled(storage, workflow_id, stored)
        return stored


def should_recompile(health: ApiHealth,
                     policy: Optional[RecompilePolicy] = None) -> Tuple[bool, str]:
    """Is the compiled rung stale enough to rebuild? Pure."""
    policy = policy or RecompilePolicy()
    if not health.preferred:
        return False, "already demoted"
    if health.recompile_attempts >= policy.max_attempts:
        return False, (f"{health.recompile_attempts} recompile attempts already made "
                       f"(max {policy.max_attempts})")
    if health.consecutive_failures >= policy.consecutive_failures:
        return True, (f"{health.consecutive_failures} consecutive failures "
                      f"(>= {policy.consecutive_failures}); last error: "
                      f"{health.last_error or 'unknown'}")
    if (health.total_runs >= policy.min_samples
            and health.success_rate < policy.min_success_rate):
        return True, (f"success rate {health.success_rate:.2f} over {health.total_runs} runs "
                      f"is below {policy.min_success_rate}")
    return False, "healthy"


def _timestamp(text: str) -> Optional[float]:
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp() if text else None
    except ValueError:
        return None


def next_attempt_after(health: ApiHealth, policy: Optional[RecompilePolicy] = None) -> str:
    """When the next automatic recompile may run ("" = now; "never" once the attempts are used up)."""
    policy = policy or RecompilePolicy()
    if health.recompile_attempts >= policy.max_attempts:
        return "never"
    last = _timestamp(health.last_recompile_at)
    if last is None:
        return ""
    return datetime.datetime.fromtimestamp(last + policy.min_interval_s).astimezone().isoformat(
        timespec="seconds")


def recompile_allowed(health: ApiHealth, policy: Optional[RecompilePolicy] = None,
                      now: Optional[float] = None) -> Tuple[bool, str]:
    """May an automatic recompile run now? A bounded number of attempts, at least
    ``min_interval_s`` apart. Pure."""
    policy = policy or RecompilePolicy()
    if health.recompile_attempts >= policy.max_attempts:
        return False, (f"{health.recompile_attempts} automatic recompile attempt(s) made "
                       f"(max {policy.max_attempts}); record the search again and recompile it")
    last = _timestamp(health.last_recompile_at)
    now = time.time() if now is None else now
    if last is not None and now - last < policy.min_interval_s:
        return False, (f"the last automatic recompile attempt was at {health.last_recompile_at}; "
                       f"the next is allowed after {next_attempt_after(health, policy)}")
    return True, ""


def _evidence(loaded) -> Dict[str, Any]:
    """A recording loader's answer as one dict: ``(recording, fresh)``,
    ``(recording, fitting, fresh)`` or a dict with ``recording`` / ``fitting`` /
    ``selection`` / ``fresh`` (or attach ``sessions``), ``allowed_origins`` and ``from``."""
    if isinstance(loaded, dict):
        return dict(loaded)
    if len(loaded) == 3:
        recording, fitting, fresh = loaded
    else:
        recording, fresh = loaded
        fitting = []
    return {"recording": recording, "fitting": list(fitting), "fresh": list(fresh)}


@dataclass
class RecompileOutcome:
    """What a recompile attempt did, and why."""
    attempted: bool = False
    persisted: bool = False
    demoted: bool = False
    reason: str = ""
    proof: Dict[str, Any] = field(default_factory=dict)
    template: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {"attempted": self.attempted, "persisted": self.persisted,
                "demoted": self.demoted, "reason": self.reason,
                "proof": self.proof, "template": self.template}


class RecompileManager:
    """Ties health, policy and recompilation to a workflow's stored files.

    ``recording_loader(workflow_id)`` returns either ``(recording, fresh)`` or
    ``(recording, fitting, fresh)``. The latter preserves independent fitting
    evidence needed to recompile a chain without borrowing its proof cases.
    Tests (and a future recorder) decide where recordings come from; this
    module never goes looking for them itself.
    """

    def __init__(self, storage, recording_loader: Callable[[str], tuple],
                 executor=None, policy: Optional[RecompilePolicy] = None,
                 proof_preflight=None, hosts_path: Optional[str] = None):
        self.storage = storage
        self.recording_loader = recording_loader
        self.executor = executor
        self.policy = policy or RecompilePolicy()
        self.proof_preflight = proof_preflight
        self.hosts_path = hosts_path

    # -- health ----------------------------------------------------------
    def load_record(self, workflow_id: str) -> Optional[dict]:
        from anyuse.execution.api_compile import load_compiled
        return load_compiled(self.storage, workflow_id)

    def save_record(self, workflow_id: str, record: dict) -> str:
        from anyuse.execution.api_compile import persist_compiled
        return persist_compiled(self.storage, workflow_id, record)

    def save_previous(self, workflow_id: str, record: dict) -> str:
        """Keep the record a recompile replaces as ``compiled_api.previous.json``."""
        path = os.path.join(str(self.storage.get_workflow_dir(workflow_id)), PREVIOUS_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(record, fh, indent=2)
        return path

    def note_outcome(self, workflow_id: str, success: bool, error: str = "") -> ApiHealth:
        """Record one replay result against the stored compile record."""
        record = self.load_record(workflow_id)
        if record is None:
            return ApiHealth()
        health = record_outcome(ApiHealth.from_dict(record.get("health")), success, error)
        record["health"] = health.to_dict()
        self.save_record(workflow_id, record)
        return health

    # -- recompile -------------------------------------------------------
    def maybe_recompile(self, workflow_id: str) -> RecompileOutcome:
        """Recompile from the stored recording if the policy says the rung is stale.

        Persists the new template ONLY when the fresh-input proof passes. When it
        does not, the rung is demoted (``health.preferred = False``) with the
        reason recorded, so the recorded UI path stays preferred and nothing
        silently replaces a proved template with an unproved one.
        """
        record = self.load_record(workflow_id)
        if record is None:
            return RecompileOutcome(reason="no compiled record for this workflow")
        health = ApiHealth.from_dict(record.get("health"))
        due, why = should_recompile(health, self.policy)
        if not due:
            return RecompileOutcome(reason=why)

        try:
            loaded = _evidence(self.recording_loader(workflow_id))
        except Exception as exc:
            health = demote(health, f"stale and no recording available to recompile from: {exc}",
                            self.policy)
            record["health"] = health.to_dict()
            self.save_record(workflow_id, record)
            return RecompileOutcome(attempted=False, demoted=True,
                                    reason=health.demoted_reason)

        health.recompile_attempts += 1
        health.last_recompile_at = _now()
        return self._prove_and_swap(workflow_id, record, health, why, loaded)

    def recompile_now(self, workflow_id: str, why: str,
                      now: Optional[float] = None) -> RecompileOutcome:
        """Recompile a compiled request the runtime cascade took out of service.

        Runs only when :func:`recompile_allowed` says so. The check and the count run
        under :func:`workflow_lock` on the stored record, and the count is persisted
        before the loader gathers any evidence (which may load pages); a loader that
        raises :class:`NoEvidence` without spending anything gives the attempt back. A
        new request is swapped in only when it proves at least the status the tool
        already had; then the health starts over and the old record is kept as
        :data:`PREVIOUS_FILE`. Otherwise the rung stays out of service with the reason.
        """
        with workflow_lock(self.storage, workflow_id):
            record = self.load_record(workflow_id)
            if record is None:
                return RecompileOutcome(reason="no compiled record for this workflow")
            health = ApiHealth.from_dict(record.get("health"))
            if health.preferred:
                # another call's recompile already put a request back in service
                return RecompileOutcome(reason="the compiled request is in service again")
            allowed, wait = recompile_allowed(health, self.policy, now)
            if not allowed:
                return RecompileOutcome(reason=wait)
            before = (health.recompile_attempts, health.last_recompile_at)
            health.recompile_attempts += 1
            health.last_recompile_at = _now()
            record["health"] = health.to_dict()
            self.save_record(workflow_id, record)
        try:
            loaded = _evidence(self.recording_loader(workflow_id))
        except Exception as exc:  # noqa: BLE001 - no evidence is a failed attempt, not a failed call
            reason = f"no evidence to recompile from: {str(exc)[:300]}"
            counted = not (isinstance(exc, NoEvidence) and not exc.spent)
            if not counted:
                health.recompile_attempts, health.last_recompile_at = before
            self._note_attempt(workflow_id, record, health, why, reason, "", False,
                               counted=counted)
            return RecompileOutcome(attempted=False, demoted=True, reason=reason)
        return self._prove_and_swap(workflow_id, record, health, why, loaded, auto=True)

    def note_refusal(self, workflow_id: str, why: str, reason: str) -> None:
        """Record why this tool is not recompiled automatically; no attempt is counted."""
        with workflow_lock(self.storage, workflow_id):
            record = self.load_record(workflow_id)
            if record is None:
                return
            health = ApiHealth.from_dict(record.get("health"))
            self._annotate(record, health, why, reason, "", False, counted=False, refused=True)
            self.save_record(workflow_id, record)

    def _annotate(self, record: dict, health: ApiHealth, why: str, reason: str, source: str,
                  swapped: bool, attempt: int = 0, counted: bool = True,
                  refused: bool = False) -> None:
        if not swapped:
            health = demote(health, (
                (f"{why}; " if why else "")
                + (f"automatic recompile skipped: {reason[:400]}" if refused else
                   f"automatic recompile did not restore it: {reason[:400]}; "
                   f"record the search again and recompile it")), self.policy)
        record["health"] = health.to_dict()
        if refused:
            nxt = "never"
        elif swapped or not counted:
            nxt = ""
        else:
            nxt = next_attempt_after(health, self.policy)
        record["recompile"] = {"at": health.last_recompile_at if counted else _now(),
                               "swapped": swapped, "from": source, "reason": reason[:300],
                               "attempt": attempt or health.recompile_attempts,
                               "next_attempt_after": nxt,
                               **({} if counted else {"counted": False})}

    def _note_attempt(self, workflow_id: str, record: dict, health: ApiHealth, why: str,
                      reason: str, source: str, swapped: bool, attempt: int = 0,
                      counted: bool = True) -> None:
        self._annotate(record, health, why, reason, source, swapped, attempt, counted)
        with workflow_lock(self.storage, workflow_id):
            self.save_record(workflow_id, record)

    def _prove_and_swap(self, workflow_id: str, record: dict, health: ApiHealth, why: str,
                        loaded: Dict[str, Any], auto: bool = False) -> RecompileOutcome:
        from anyuse.execution.api_verify import (compile_and_verify,
                                                 compile_and_verify_with_fit,
                                                 permission_preflight)
        from anyuse.execution import proof_split as PS

        previous = json.loads(json.dumps(record))
        recording = loaded.get("recording") or {}
        source = str(loaded.get("from") or "the stored recordings")
        if self.proof_preflight is None:
            origins = loaded.get("allowed_origins")
            gate = lambda compiled: permission_preflight(compiled, recording,
                                                         allowed_origins=origins,
                                                         hosts_path=self.hosts_path)
        else:
            gate = self.proof_preflight
        # a chain's generated fitting input: the value it was fitted on last time first
        stored_fit = str((record.get("chain_fit") or {}).get("value") or "")
        prefer_fit = [stored_fit] if stored_fit else []
        if loaded.get("sessions"):
            from anyuse.execution.attach import compile_sessions
            compiled, proof, _report, _recs = compile_sessions(
                loaded["sessions"], executor=self.executor, preflight=gate, model_scope="",
                prefer_fit=prefer_fit, label_keys=_labelled_keys(record))
        elif loaded.get("fitting"):
            compiled, proof = compile_and_verify(recording, list(loaded.get("fresh") or []),
                                                 self.executor, preflight=gate,
                                                 fitting_recordings=list(loaded.get("fitting") or []),
                                                 selection_recordings=list(loaded.get("selection") or []),
                                                 label_keys=_labelled_keys(record))
        else:
            # a chain is fitted on the selection page, else on a generated input
            # (execution/chain_fit.py); anything else compiles exactly as
            # compile_and_verify does
            selection = list(loaded.get("selection") or [])
            compiled, proof, _fit = compile_and_verify_with_fit(
                recording, list(loaded.get("fresh") or []), selection,
                selection_recordings=selection, executor=self.executor, preflight=gate,
                browse=loaded.get("browse"), prefer_fit=prefer_fit,
                label_keys=_labelled_keys(record))
        outcome = RecompileOutcome(attempted=True, reason=why, proof=proof.to_dict())
        prior = record.get("proof") or {}
        prior_status = (str(prior.get("status") or PS.PROVED) if prior.get("passed")
                        else PS.PROVED)
        new_status = str(getattr(proof, "effective_status", "") or PS.NOT_PROVED)
        if (auto and compiled.template is not None and proof.passed
                and PS.weaker(new_status, prior_status) != prior_status):
            # the cascade never swaps in a weaker claim than the one the tool was listed with
            reason = (f"the new request reached {new_status!r}, weaker than the tool's "
                      f"{prior_status!r}: {getattr(proof, 'reason', '')}")
            self._note_attempt(workflow_id, record, health, why, reason, source, False)
            outcome.demoted, outcome.reason = True, reason
            return outcome

        if compiled.template is not None and proof.passed:
            # A recompile never upgrades the status it inherits: the stored
            # recordings' ground truth was fixed when they were first compiled
            #. The weaker of the two statuses wins.
            new_proof = proof.to_dict()
            status = PS.weaker(proof.effective_status, prior_status)
            if status != proof.effective_status:
                new_proof["status"] = status
                split = dict(new_proof.get("split") or {})
                split["held_out"] = False
                split["why"] = ((split.get("why") + "; ") if split.get("why") else "") + (
                    f"inherited status {prior_status!r} from the record it replaces")
                new_proof["split"] = split
                new_proof["selection_held_out"] = False
                shape = getattr(compiled.template, "shape", "list")
                fields = sorted((getattr(compiled.template, "extract", {}) or {}).keys())
                new_proof["scope"] = PS.scope_text(new_proof, shape, fields)
            from anyuse.execution.workflow_schema import template_digest
            record["template"] = compiled.template.to_dict()
            # pin the new evidence to the new template, as compiled_record does
            new_proof["template_digest"] = template_digest(record["template"])
            anchor = new_proof.pop("drift_anchor", None)
            chain_fit = new_proof.pop("chain_fit", None)
            from anyuse.execution.api_step import signed_in_template
            if isinstance(anchor, dict) and not signed_in_template(compiled.template):
                record["drift_anchor"] = anchor
            # the generated fitting input lives on the record, as compiled_record keeps it;
            # one fitted on a recorded input leaves none behind
            record.pop("chain_fit", None)
            if isinstance(chain_fit, dict) and not signed_in_template(compiled.template):
                record["chain_fit"] = chain_fit
            record["proof"] = new_proof
            record["recompiled_at"] = _now()
            attempt = health.recompile_attempts
            if auto:
                # a fresh start for the new request; pacing still counts from this attempt
                health = ApiHealth(last_recompile_at=health.last_recompile_at)
            health.consecutive_failures = 0
            health.last_error = ""
            health.preferred = True
            health.demoted_reason = ""
            outcome.persisted = True
            outcome.template = record["template"]
            outcome.reason = (f"recompiled from {source} and re-proved: {proof.reason}"[:300]
                              if auto else
                              f"{why} → recompiled from {source} and re-proved on fresh input")
            self.save_previous(workflow_id, previous)
            if auto and loaded.get("keep") and not loaded.get("sessions"):
                # the live pages the new request was proved from become its kept recordings
                from anyuse.execution import evidence_store as ES
                ES.keep_compile_evidence(self.storage, workflow_id, record, recording=recording,
                                         fitting=list(loaded.get("fitting") or []),
                                         selection=list(loaded.get("selection") or []),
                                         fresh=list(loaded.get("fresh") or []))
            if auto:
                record["recompiled"] = {
                    "at": record["recompiled_at"], "from": source, "why": why[:300],
                    "status": status,
                    "previous_template_digest": str(prior.get("template_digest") or ""),
                    "previous_record": PREVIOUS_FILE}
                self._note_attempt(workflow_id, record, health, why, outcome.reason, source, True,
                                   attempt)
                return outcome
        else:
            note = str(getattr(compiled, "chain_fit_note", "") or "")
            reason = (f"recompile did not produce a proved template: "
                      f"{proof.reason or compiled.reason}" + (f" ({note})" if note else ""))
            if auto:
                self._note_attempt(workflow_id, record, health, why, reason, source, False)
                outcome.demoted, outcome.reason = True, reason
                return outcome
            health = demote(health, f"{why} → {reason}", self.policy)
            outcome.demoted = True
            outcome.reason = health.demoted_reason

        record["health"] = health.to_dict()
        self.save_record(workflow_id, record)
        return outcome

    # -- step-level repair first (feat/runtime-repair) --------------------
    def maybe_repair(self, workflow_id: str, *, held_out, selection: str,
                     ui_page=None, navigate=None) -> RecompileOutcome:
        """Localise the failing step and re-learn only it from one capture
        (``execution/step_repair.py``), re-proved on ``held_out`` inputs with
        the shared split. The full recompile (:meth:`maybe_recompile`) runs
        only when localisation or the step's re-derivation fails."""
        from anyuse.execution.step_repair import repair
        record = self.load_record(workflow_id)
        if record is None:
            return RecompileOutcome(reason="no compiled record for this workflow")
        health = ApiHealth.from_dict(record.get("health"))
        due, why = should_recompile(health, self.policy)
        if not due:
            return RecompileOutcome(reason=why)
        new, out = repair(record, executor=self.executor, held_out=list(held_out),
                          selection=selection, ui_page=ui_page, navigate=navigate)
        if new is not None:
            h = ApiHealth.from_dict(new.get("health"))
            h.recompile_attempts = health.recompile_attempts + 1
            h.last_recompile_at = _now()
            new["health"] = h.to_dict()
            new["recompiled_at"] = _now()
            self.save_record(workflow_id, new)
            return RecompileOutcome(attempted=True, persisted=True, proof=out.proof,
                                    template=new["template"],
                                    reason=f"{why} → step-level repair: {out.reason}")
        if out.localised == "none":
            return RecompileOutcome(reason=f"{why} → {out.reason}")
        full = self.maybe_recompile(workflow_id)
        full.reason = (f"step-level repair did not apply ({out.localised}: {out.reason}) "
                       f"→ full recompile: {full.reason}")
        return full


def apply_to_graph(state_graph, from_state: str, to_state: str, intent: str,
                   record: Optional[dict]) -> str:
    """Make the graph reflect a compile record's health.

    * proved and preferred → the ``api`` implementation carries the current
      template (added or replaced through the existing
      ``StateGraph.add_implementation``);
    * demoted → the ``api`` implementation is REMOVED, so the recorded UI rung is
      what ``rank_implementations`` offers first. Removing rather than keeping a
      zero-success rung is deliberate: a dead endpoint should cost the next
      replay nothing at all, not one failed request per run.

    Returns a one-line description of what changed.
    """
    from anyuse.execution.api_compile import add_api_implementation
    from anyuse.execution.api_step import load_command

    transition = state_graph._find_transition(from_state, to_state, intent)
    if transition is None:
        return "no such transition in the graph"
    if not record or not record.get("template"):
        return "no compiled template to apply"
    health = ApiHealth.from_dict(record.get("health"))
    if health.preferred:
        add_api_implementation(state_graph, from_state, to_state, intent,
                               load_command(record["template"]))
        return "api implementation is current and preferred"
    before = len(transition.implementations)
    transition.implementations = [i for i in transition.implementations if i.modality != "api"]
    removed = before - len(transition.implementations)
    return (f"api implementation removed ({removed} rung(s)); UI path preferred — "
            f"{health.demoted_reason or 'demoted'}")


def load_recordings_from_dir(directory: str) -> Callable[[str], Tuple[dict, List[dict]]]:
    """A ``recording_loader`` reading ``<id>.rec.json`` + ``<id>.fresh*.json``."""
    import glob

    def loader(workflow_id: str) -> Tuple[dict, List[dict]]:
        rec_path = os.path.join(directory, f"{workflow_id}.rec.json")
        with open(rec_path) as fh:
            recording = json.load(fh)
        fresh = []
        for path in sorted(glob.glob(os.path.join(directory, f"{workflow_id}.fresh*.json"))):
            with open(path) as fh:
                fresh.append(json.load(fh))
        return recording, fresh

    return loader
