"""The learn loop: the agent finds the page ONCE, a person confirms it ONCE, AnyUse proves it.

An earlier loop needed the agent to reach the right page three times in a row
on three values and then took whatever page it reached as ground truth.
Measured: it converted 2 of 20 tasks, one of them to the wrong page.

The shape now
-------------
1. **The agent navigates once**, for the first value, in a container browser,
   and is recorded (:mod:`agent_browser`).
2. **AnyUse shows the candidate**: the final URL, the page title, a preview of
   the largest repeated group on that one page, and where in the URL it
   believes the value sits. **It asks for one confirmation.** In the CLI that
   is a y/n prompt or an explicit restatement of the URL; over MCP it is a
   separate ``confirm`` call that must restate the URL; in the bench it is a
   deterministic oracle that compares the candidate with a human recording and
   stands in for a person.
3. **A proposal persists only in an explicit raw-candidate store.** It is not a
   learned rung; stored raw data is mode 0600 and expires by policy.
4. After confirmation, the further values come from **parameter substitution**
   on the confirmed URL — a plain navigation, no model call — and from the
   agent only where the value cannot be found in the URL at all.
5. Every further run passes a **landing check by URL pattern and title**, never
   by "the body mentions the value": the page it
   ended on must be the confirmed page's shape with the new value bound. The
   confirmed page is the ground truth, not whatever the agent reached.
6. Ground truth is derived across the values by the same rule a human
   recording gets; the chrome filter, the compiler, the fresh-input proof and
   persistence run **unchanged**, and persistence still needs the proof AND a
   host acknowledgement.

What this cannot do, stated up front: it cannot tell whether the person
confirmed the right page. That is the point — it is a person's decision, made
once, on a preview that shows exactly what will be learned. What it can do is
refuse to learn anything a person did not confirm, and refuse every later
page that is not that page.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
import statistics
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: A learned workflow needs one confirmed page to compile from and at least
#: this many values in total, so the proof has >= 2 values the compiler never
#: saw. Same number the human path uses (``api_verify.MIN_FRESH_CASES``).
MIN_VALUES = 3
#: With this many values ``proof_split.assign`` holds the validation out
#: (1 compile + 1 selection + 2 validation), so the status can reach ``proved``.
PROVED_VALUES = 4
#: generated values tried beyond the ones missing, per source
GENERATE_SPARE = 2
#: where a generated value came from
FROM_PAGE, FROM_MODEL = "result_page", "model"
PREVIEW_ITEMS = 8
LANDING_CHECK = "url_pattern+title"
#: what a tool saved from a browser-replay proof says about itself
BROWSER_TOOL_NOTE = ("served by the browser replay (no compiled request); each call "
                     "needs a browser (ANYUSE_CDP_ENDPOINT or ANYUSE_LEARN_ENDPOINT)")

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# the candidate — what a person is shown, and what a confirmation echoes
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """The page the agent reached once, before anything is learned from it."""
    workflow: str = ""
    task: str = ""
    start_url: str = ""
    param: str = "q"
    cls: str = "list"
    value: str = ""
    final_url: str = ""
    title: str = ""
    #: the confirmed URL with ``{param}`` where the value sits, or "" when the
    #: value is not carried by the URL (then the agent is needed for every value)
    url_template: str = ""
    path_params: Dict[str, int] = field(default_factory=dict)
    param_transforms: Dict[str, str] = field(default_factory=dict)
    binding: str = ""
    preview_selector: str = ""
    preview: List[str] = field(default_factory=list)
    preview_note: str = ""
    blocked: str = ""
    error: str = ""
    #: what the one agent run cost
    agent_seconds: float = 0.0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    #: the agent run in the human recorder's format; kept so a confirmation can
    #: continue without running the agent again
    recording: Optional[dict] = None
    candidate_id: str = ""
    created_at: str = ""
    #: the agent tier's escalation log (levels tried, triggers), when on
    escalation: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return bool(self.recording) and not self.error and not self.blocked

    def summary(self) -> dict:
        """Exactly what is SHOWN before a confirmation, and echoed with one."""
        return {"candidate_id": self.candidate_id, "workflow": self.workflow,
                "task": self.task, "start_url": self.start_url,
                "param": self.param, "value": self.value,
                "final_url": self.final_url, "title": self.title,
                "value_bound_as": self.binding, "url_template": self.url_template,
                "preview": list(self.preview[:PREVIEW_ITEMS]),
                "preview_selector": self.preview_selector,
                "preview_note": self.preview_note,
                "blocked": self.blocked, "error": self.error,
                "agent_seconds": round(self.agent_seconds, 2),
                "model_calls": self.model_calls, "cost_usd": self.cost_usd,
                "escalation": self.escalation,
                "persisted": ("proposal objects do not persist themselves; an explicit "
                              "candidate store may retain this raw candidate temporarily")}

    def to_dict(self, with_recording: bool = True) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k != "recording"}
        if with_recording:
            out["recording"] = self.recording
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def page_title(dom: str) -> str:
    m = _TITLE.search(dom or "")
    if not m:
        return ""
    return " ".join(_html.unescape(_TAG.sub(" ", m.group(1))).split())[:200]


def preview_items(dom: str, limit: int = PREVIEW_ITEMS) -> Tuple[str, List[str]]:
    """The largest repeated group on ONE page: the candidate list, unproved.

    A single page cannot show that a group *changes with the input* — that is
    what the cross-value derivation does after confirmation. This is what a
    person looks at to decide whether the page is the one they meant.
    """
    from anyuse.execution.api_compile import first_line
    from anyuse.execution.html_extract import _row_selector, parse, repeated_groups
    if not dom:
        return "", []
    try:
        root = parse(dom)
    except Exception:
        return "", []
    for rows in repeated_groups(root)[:5]:
        sel = _row_selector(rows)
        items = [first_line(r.text()) for r in rows if r.text()]
        items = [i for i in items if i][:limit]
        if sel and items:
            return sel, items
    return "", []


def url_template_for(url: str, value: str, param: str) -> Tuple[str, Dict[str, int],
                                                              Dict[str, str], str]:
    """``(template, path_params, transforms, how)`` — the product's own binding rules.

    Best kind first (a whole query value, then a run of whole path segments,
    then a delimited fragment). A site taken from ONE URL is not corroborated;
    what corroborates it here is the confirmation plus the landing check on
    every substituted navigation: a wrong site lands on a page whose URL and
    title do not match, and nothing is learned.
    """
    from anyuse.execution.param_binding import site_params, sites_for, templated_url
    for site in sites_for(url, value):
        tmpl = templated_url(url, site, param)
        if tmpl:
            pp, tr = site_params(site, param)
            return tmpl, pp, tr, site.describe()
    return "", {}, {}, (f"{value!r} is not a whole query value, a run of path segments "
                        f"or a delimited fragment of the URL, so the other values "
                        f"cannot be substituted; the agent is needed for each")


def render_url(template: str, param: str, value: str, path_params: Dict[str, int],
               transforms: Dict[str, str]) -> str:
    from anyuse.execution.cdp_replay import ReplayTemplate
    return ReplayTemplate(url=template, params=[param], path_params=dict(path_params),
                          param_transforms=dict(transforms)).render({param: value})


# ---------------------------------------------------------------------------
# the landing check: URL pattern + title, never "the body mentions the value"
# ---------------------------------------------------------------------------

def same_url(want: str, actual: str) -> Tuple[bool, str]:
    """Same host, same path, and every query pair of ``want`` present in ``actual``."""
    a, b = urllib.parse.urlsplit(want), urllib.parse.urlsplit(actual)
    if a.scheme.lower() != b.scheme.lower() or a.netloc.lower() != b.netloc.lower():
        return False, f"different host: {b.netloc or actual[:60]!r}"
    pa = urllib.parse.unquote(a.path).rstrip("/") or "/"
    pb = urllib.parse.unquote(b.path).rstrip("/") or "/"
    if pa != pb:
        return False, f"path {pb!r} is not the confirmed pattern's {pa!r}"
    qa = urllib.parse.parse_qsl(a.query, keep_blank_values=True)
    qb = dict(urllib.parse.parse_qsl(b.query, keep_blank_values=True))
    for k, v in qa:
        if qb.get(k) != v:
            return False, f"query {k}={qb.get(k)!r} is not the confirmed pattern's {v!r}"
    return True, "URL matches the confirmed pattern"


def _value_parts(value: str) -> List[str]:
    from anyuse.execution.api_compile import normalise
    return [normalise(p) for p in str(value).replace("/", " ").split() if normalise(p)]


def _word_in(part: str, blob: str) -> bool:
    from anyuse.execution.api_compile import contains_words
    return contains_words(blob, part)


def _strip(blob: str, parts: Sequence[str]) -> str:
    from anyuse.execution.api_compile import word_spans
    for p in parts:
        kept, last = [], 0
        for start, end in word_spans(blob, p):
            kept += [blob[last:start], " "]
            last = end
        blob = "".join(kept) + blob[last:]
    # versions, counts and dates vary between two right pages ("yay 12.3-1")
    blob = " ".join(t for t in blob.split() if not any(c.isdigit() for c in t))
    return blob.strip()


def _same_frame(rest0: str, rest1: str) -> bool:
    """Do two title remainders share a frame — the site's constant words?

    Half the tokens of the shorter remainder in common (and at least two), or
    a common suffix of two tokens ("| Man Page | Example"). A challenge page's
    title shares nothing with a content page's; a search page's title shares
    a site name at most, which is one token.
    """
    a, b = rest0.split(), rest1.split()
    if not a or not b:
        return False
    common = set(a) & set(b)
    if len(common) >= 2 and len(common) >= 0.5 * min(len(a), len(b)):
        return True
    return len(a) >= 2 and len(b) >= 2 and a[-2:] == b[-2:]


def title_matches(confirmed_title: str, confirmed_value: str,
                  title: str, value: str) -> Tuple[bool, str]:
    """Is ``title`` the confirmed title's pattern with ``value`` in it?

    If the confirmed title names its value, the new title must name the new
    value and the rest must agree. If the confirmed title is constant (a search
    page whose title never changes), the new title must be the same. An index
    page fails either way: it does not name the value and it is not the
    confirmed title.
    """
    from anyuse.execution.api_compile import normalise, values_match
    n0, n1 = normalise(confirmed_title), normalise(title)
    if not n0 and not n1:
        return True, "neither page has a title; the title check is vacuous"
    if not n1:
        return False, "the page has no title where the confirmed page had one"
    p0, p1 = _value_parts(confirmed_value), _value_parts(value)
    present0 = [p for p in p0 if _word_in(p, n0)]
    if present0:
        present1 = [p for p in p1 if _word_in(p, n1)]
        if not present1:
            return False, (f"the confirmed title names its value ({confirmed_title!r}); "
                           f"this title does not name {value!r} ({title!r})")
        rest0, rest1 = _strip(n0, present0), _strip(n1, present1)
        if rest0 == rest1 or values_match(rest0, rest1) or (not rest0 and not rest1):
            return True, "title matches the confirmed pattern"
        if _same_frame(rest0, rest1):
            # "rsync: a fast file-copying tool | Man Page | Example" and
            # "tar: an archiving utility | Man Page | Example": the value and a
            # per-item description differ, the frame around them is the same.
            return True, "title frame matches the confirmed pattern (per-item text differs)"
        return False, f"title pattern differs: {confirmed_title!r} vs {title!r}"
    if n0 == n1 or values_match(n0, n1):
        return True, "same title as the confirmed page"
    return False, f"title differs from the confirmed page: {confirmed_title!r} vs {title!r}"


def landing_check(candidate: Candidate, run: Any,
                  confirmer_runner: Optional[Callable] = None) -> Tuple[bool, str]:
    """Did this run end on the CONFIRMED page's shape, with its own value bound?

    In ``ANYUSE_CONFIRMER=shadow`` the finished rule decision is also shown to
    the ``wrong_page`` confirmer and logged.  That observation is best-effort
    and cannot change this function's return value.
    """
    def decided(ok: bool, why: str) -> Tuple[bool, str]:
        try:
            from anyuse.execution import confirmer
            confirmer.observe_landing(candidate, run, ok, runner=confirmer_runner)
        except Exception:
            # Shadow instrumentation must not turn a runner/logging problem
            # into a different landing verdict.
            pass
        return ok, why

    notes = []
    if candidate.url_template:
        try:
            want = render_url(candidate.url_template, candidate.param, run.value,
                              candidate.path_params, candidate.param_transforms)
        except Exception as exc:
            return decided(False,
                           f"cannot bind {run.value!r} into the confirmed URL pattern: {exc}")
        ok, why = same_url(want, run.final_url or "")
        if not ok:
            return decided(False, why)
        notes.append(why)
    else:
        notes.append("no URL pattern (the value is not in the confirmed URL)")
    ok, why = title_matches(candidate.title, candidate.value,
                            page_title(run.dom), run.value)
    if not ok:
        return decided(False, why)
    notes.append(why)
    return decided(True, "; ".join(notes))


def usable_runs(runs: Sequence[Any], candidate: Candidate,
                check: Optional[Callable] = None) -> tuple:
    """``(usable, discarded)`` — the landing check against the confirmed page.
    ``check`` (default :func:`landing_check`) may only ADD a way to pass on top
    of the default check; ``discover.py`` passes one for detail pages."""
    check = check or landing_check
    usable, discarded = [], []
    for run in runs:
        if not getattr(run, "ok", False):
            discarded.append({"value": run.value, "why": run.error or "no DOM captured"})
            continue
        ok, why = check(candidate, run)
        if not ok:
            discarded.append({"value": run.value,
                              "why": (f"ended on {run.final_url[:100]!r}, which is not the "
                                      f"confirmed page with {run.value!r} bound: {why}")})
            continue
        usable.append(run)
    return usable, discarded


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------

@dataclass
class LearnResult:
    """Everything the loop decided, and why. Serialisable end to end."""
    workflow: str = ""
    task: str = ""
    start_url: str = ""
    param: str = "q"
    values: List[str] = field(default_factory=list)
    #: "tier1" | "tier2" | "tier2_nonpersistent" | "nothing" | "unconfirmed"
    outcome: str = "nothing"
    reason: str = ""
    candidate: Optional[dict] = None
    confirmed_by: str = ""
    landing_check: str = LANDING_CHECK
    runs: List[dict] = field(default_factory=list)
    discarded: List[dict] = field(default_factory=list)
    ground_truth_selector: str = ""
    ground_truth_sample: List[str] = field(default_factory=list)
    chrome_filtered: List[str] = field(default_factory=list)
    compiled: Optional[dict] = None
    proof: Optional[dict] = None
    tier2: Optional[dict] = None
    learned_url: str = ""
    persisted: Optional[str] = None
    #: what the learning itself cost
    agent_runs: int = 0
    navigations: int = 0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    learn_seconds: float = 0.0
    #: first run (agent) vs later run (ladder)
    first_run_ms: Optional[float] = None
    later_run_ms: Optional[float] = None
    later_run_model_calls: int = 0
    #: ``proved`` / ``checked (selection not held out)`` / ``not proved``
    #: (execution/proof_split.py), for tier 1 and tier 2 alike
    proof_status: str = "not proved"
    proof_scope: str = ""
    split: Optional[dict] = None
    #: values learn proposed itself: ``[{"value", "from": "result_page" | "model"}]``;
    #: only ever selection or validation inputs, proved like the given ones
    generated: List[dict] = field(default_factory=list)
    #: why fewer values were generated than were missing
    generate_note: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


# ---------------------------------------------------------------------------
# step 1: propose — the agent runs ONCE
# ---------------------------------------------------------------------------

def propose(task: str, start_url: str, value: str, *, workflow: str = "",
            param: str = "q", cls: str = "list", agent: Any = None) -> Candidate:
    """One agent run, turned into something a person can say yes or no to."""
    from anyuse.execution.agent_browser import spend_usd
    from anyuse.execution.blocked import detect_blocked_ui

    cand = Candidate(workflow=workflow or _workflow_name(start_url, task), task=task,
                     start_url=start_url, param=param, cls=cls, value=str(value),
                     created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    run = _agent_run(agent, task, start_url, str(value))
    cand.agent_seconds = run.seconds
    cand.model_calls, cand.tokens_in, cand.tokens_out = (run.model_calls, run.tokens_in,
                                                          run.tokens_out)
    cand.cost_usd = round(float(getattr(run, "cost_usd", 0.0) or 0.0)
                          or spend_usd(run.tokens_in, run.tokens_out), 5)
    cand.escalation = getattr(run, "escalation", None)
    cand.final_url = run.final_url or ""
    if not run.ok:
        cand.error = run.error or "the agent captured no page"
        cand.candidate_id = _candidate_id(cand)
        return cand
    cand.title = page_title(run.dom)
    from anyuse.execution.agent_browser import same_site
    if cand.final_url and not same_site(start_url, cand.final_url):
        cand.error = (f"the agent ended off the site, on {cand.final_url[:80]}; a page on "
                      f"another host cannot be this site's answer and is not offered "
                      f"as a candidate")
        cand.candidate_id = _candidate_id(cand)
        return cand
    try:
        state = detect_blocked_ui(run.text or "", "", cand.final_url,
                                  run.network or [], items=[])
        if state.blocked:
            cand.blocked = f"{state.reason}: {'; '.join(state.evidence)[:120]}"
    except Exception:                       # the detector is advisory here
        pass
    cand.url_template, cand.path_params, cand.param_transforms, cand.binding = \
        url_template_for(cand.final_url, cand.value, param)
    cand.preview_selector, cand.preview = preview_items(run.dom)
    cand.preview_note = (("the largest repeated group on this ONE page — a candidate "
                          "list, not yet shown to change with the input")
                         if cand.preview else "no repeated group was found on the page")
    cand.recording = run.as_recording(cand.workflow, param, "agent0", cls)
    cand.candidate_id = _candidate_id(cand)
    return cand


def _agent_run(agent: Any, task: str, start_url: str, value: str) -> Any:
    """One run of the learn agent, inside the ``agent`` model-check scope: its own steps may ask the configured
    small model (several submit controls next to a search box, several search addresses). Only the agent's run
    is inside it; the navigations, the proof and the replay learn runs afterwards ask nothing."""
    from anyuse.execution import model_check as MC
    with MC.scope("agent"):
        return agent.run(task, start_url, value)


def _candidate_id(cand: Candidate) -> str:
    blob = json.dumps([cand.task, cand.start_url, cand.value, cand.final_url,
                       cand.title, cand.created_at], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# step 2 + 3: confirm, then substitute, prove, persist
# ---------------------------------------------------------------------------

def learn(task: str, start_url: str, values: Sequence[str], *,
          workflow: str = "", param: str = "q", cls: str = "list",
          agent: Any = None, navigator: Optional[Callable] = None,
          executor: Any = None, replay: Any = None,
          persist: Optional[Callable] = None,
          confirm: Optional[Callable[[Candidate], bool]] = None,
          confirmed_by: str = "",
          min_values: int = MIN_VALUES,
          budget_usd: Optional[float] = None,
          proof_preflight=None, generate: bool = False,
          model: Any = None) -> LearnResult:
    """propose → confirm → continue. ``confirm`` is ``(Candidate) -> bool``.

    With no ``confirm`` the loop stops after proposing: outcome ``unconfirmed``,
    the candidate attached, nothing persisted. ``persist`` is the ONLY thing
    that writes; ``None`` means "decide, report, persist nothing".

    ``generate``: with fewer than :data:`PROVED_VALUES` values, propose the
    missing ones (:func:`continue_learning`); one value is then enough to start.
    """
    values = [str(v) for v in values if str(v).strip()]
    res = LearnResult(workflow=workflow or _workflow_name(start_url, task),
                      task=task, start_url=start_url, param=param, values=list(values))
    if len(values) < (1 if generate else min_values):
        res.reason = (f"{len(values)} value(s) given; the loop needs {min_values} "
                      f"— one to confirm and compile from and at least two to prove "
                      f"on, which is the same standard a human recording is held to")
        return res

    t0 = time.monotonic()
    cand = propose(task, start_url, values[0], workflow=res.workflow, param=param,
                   cls=cls, agent=agent)
    res.candidate = cand.summary()
    res.agent_runs, res.model_calls = 1, cand.model_calls
    res.tokens_in, res.tokens_out = cand.tokens_in, cand.tokens_out
    res.cost_usd = cand.cost_usd
    res.runs.append(_run_summary(cand))
    res.first_run_ms = cand.agent_seconds * 1000.0 if cand.recording else None
    if not cand.ok:
        res.outcome = "nothing"
        res.reason = (f"the agent's one run produced no candidate: "
                      f"{cand.blocked or cand.error}")
        res.learn_seconds = time.monotonic() - t0
        return res
    if confirm is None:
        res.outcome = "unconfirmed"
        res.reason = ("a candidate was proposed but no runnable artifact was persisted: "
                      "no confirmation was given")
        res.learn_seconds = time.monotonic() - t0
        return res
    if not confirm(cand):
        res.outcome = "nothing"
        res.reason = (f"the candidate {cand.final_url[:90]!r} was NOT confirmed"
                      f"{' by ' + confirmed_by if confirmed_by else ''}; nothing learned, "
                      f"nothing persisted")
        res.learn_seconds = time.monotonic() - t0
        return res

    out = continue_learning(cand, values[1:], agent=agent, navigator=navigator,
                            executor=executor, replay=replay, persist=persist,
                            confirmed_by=confirmed_by or "caller",
                            min_values=min_values, budget_usd=budget_usd,
                            proof_preflight=proof_preflight, generate=generate,
                            model=model)
    out.learn_seconds = time.monotonic() - t0
    return out


def continue_learning(cand: Candidate, others: Sequence[str], *,
                      agent: Any = None, navigator: Optional[Callable] = None,
                      executor: Any = None, replay: Any = None,
                      persist: Optional[Callable] = None,
                      confirmed_by: str = "caller",
                      min_values: int = MIN_VALUES,
                      budget_usd: Optional[float] = None,
                      proof_preflight=None, n_selection: Optional[int] = None,
                      landing: Optional[Callable] = None, generate: bool = False,
                      model: Any = None) -> LearnResult:
    """Everything after a confirmation. The confirmed page is the ground truth.

    ``n_selection``: the first ``n_selection`` usable further runs are SELECTION
    inputs (they may choose among equally-plausible compiled candidates) and are
    never validation inputs; at least ``min_values - 1`` runs remain for
    validation. ``None`` (the default) lets ``proof_split.assign`` decide: one
    selection input when enough runs are usable, otherwise none, and then the
    ground truth is derived across every page and the best status is
    ``checked (selection not held out)``, never ``proved``.

    ``generate``: when fewer than ``PROVED_VALUES - 1`` further runs are usable,
    propose the missing values (:func:`_generated_runs`): names on the result
    pages already fetched, else one call to ``model`` (default: the configured
    ``agent`` role). They come after the given values, so they are only ever
    selection or validation inputs, and they are fetched, landing-checked and
    proved exactly like the given ones."""
    from anyuse.execution.agent_browser import AgentRun, spend_usd
    from anyuse.execution.api_compile import page_chrome, visible_items
    from anyuse.execution.api_verify import compile_and_verify_with_fit, permission_preflight
    from anyuse.execution import proof_split as PS

    t0 = time.monotonic()
    others = [str(v) for v in others if str(v).strip()]
    res = LearnResult(workflow=cand.workflow, task=cand.task, start_url=cand.start_url,
                      param=cand.param, values=[cand.value, *others],
                      candidate=cand.summary(), confirmed_by=confirmed_by)
    res.agent_runs, res.model_calls = 1, cand.model_calls
    res.tokens_in, res.tokens_out = cand.tokens_in, cand.tokens_out
    res.cost_usd = cand.cost_usd
    res.first_run_ms = cand.agent_seconds * 1000.0
    res.runs.append(_run_summary(cand))
    if not cand.ok:
        res.reason = f"the candidate is not usable: {cand.blocked or cand.error}"
        return res
    auto_selection = n_selection is None
    n_selection = 0 if auto_selection else max(0, int(n_selection or 0))
    if not generate and len(others) < min_values - 1 + n_selection:
        res.reason = (f"{len(others)} further value(s); the loop needs {min_values - 1} "
                      f"the compiler never saw"
                      + (f" plus {n_selection} selection value(s)" if n_selection else ""))
        return res
    cand.recording["confirmed_by"] = confirmed_by
    cand.recording["confirmed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def fetch(value: str) -> Any:
        """The value's page: the confirmed URL with the value bound, else the agent."""
        run = None
        if cand.url_template and navigator is not None:
            try:
                url = render_url(cand.url_template, cand.param, value,
                                 cand.path_params, cand.param_transforms)
            except Exception as exc:
                url = ""
                logger.info("cannot bind %r into %s: %s", value, cand.url_template, exc)
            if url:
                run = navigator(url, value)
                res.navigations += 1
        if run is None:
            if agent is None:
                run = AgentRun(value=value, task=cand.task, start_url=cand.start_url,
                               error="no agent and no URL pattern to substitute into")
            else:
                run = _agent_run(agent, cand.task, cand.start_url, value)
                res.agent_runs += 1
                res.model_calls += run.model_calls
                res.tokens_in += run.tokens_in
                res.tokens_out += run.tokens_out
                # the provider's reported cost (the proposal's is already in res.cost_usd);
                # list prices only when a run reports none
                res.cost_usd = round(res.cost_usd + (float(getattr(run, "cost_usd", 0.0) or 0.0)
                                                     or spend_usd(run.tokens_in, run.tokens_out)), 5)
        return run

    def over_budget() -> bool:
        return budget_usd is not None and res.cost_usd >= budget_usd

    def navigate(value: str) -> Any:
        """The value's page by the confirmed URL only (never the agent): the made-up searches of the empty page."""
        if not cand.url_template or navigator is None:
            return None
        try:
            url = render_url(cand.url_template, cand.param, value, cand.path_params, cand.param_transforms)
        except Exception:  # noqa: BLE001 - a value the template cannot carry is not searched
            return None
        res.navigations += 1
        return navigator(url, value)

    runs: List[Any] = []
    for value in others:
        run = fetch(value)
        runs.append(run)
        res.runs.append(run.to_dict())
        if over_budget():
            res.reason = f"learning budget of ${budget_usd:.2f} reached after {len(runs)} further run(s)"
            break

    good, discarded = usable_runs(runs, cand, landing)
    res.discarded = discarded
    target = max(PROVED_VALUES - 1, min_values - 1 + n_selection)
    if generate and len(good) < target and not over_budget():
        extra = _generated_runs(cand, good, [cand.value, *others], target - len(good),
                                fetch=fetch, landing=landing, res=res, model=model,
                                over_budget=over_budget, empty=SiteEmptyPage(cand, good, navigate))
        runs.extend(extra)
        good = good + extra
    # the agent's own first run belongs to the cost of learning too
    res.learn_seconds = float(cand.agent_seconds or 0.0) + time.monotonic() - t0
    if len(good) < min_values - 1 + n_selection:
        res.reason = res.reason or (
            f"only {len(good)} of {len(runs)} further run(s) ended on the confirmed "
            f"page's URL pattern and title with their own value bound; the loop "
            f"needs {min_values - 1 + n_selection}")
        return res

    recordings = [cand.recording]
    generated_from = {g["value"]: g["from"] for g in res.generated}
    for i, r in enumerate(good, start=1):
        tag = f"nav{i}" if r.model_calls == 0 and _is_navigation(r) else f"agent{i}"
        rec = r.as_recording(cand.workflow, cand.param, tag, cand.cls)
        if r.value in generated_from:
            rec["generated"] = generated_from[r.value]
        recordings.append(rec)
    # the shared rule (execution/proof_split.py) assigns the roles
    # A derived ground truth needs two fitting pages, so holding validation
    # out needs at least one selection page; with too few runs assign()
    # returns the degraded split (every page fits, status at most "checked").
    split = PS.assign(recordings, min_validation=min_values - 1,
                      n_selection=max(PS.N_SELECTION, n_selection))
    n_selection = len(split.selection)
    if split.held_out:
        # strict separation: the ground-truth selector and the page chrome are
        # fitted on the confirmed page + the selection pages only; validation
        # pages are then only READ with that selector (their expected answer)
        from anyuse.execution.html_extract import extract as _extract
        fit_recs = [split.compile, *split.fitting, *split.selection]
        selector = derive_ground_truth(fit_recs)
        vis = any(r.get("ground_truth_visible_only") for r in fit_recs)
        for r in split.validation:
            r["ground_truth_selector"] = selector
            if vis:
                r["ground_truth_visible_only"] = True
            readable = bool(selector and r.get("dom"))
            # the same two readings as derive_ground_truth: whole rows, and each row's name
            r.setdefault("ui", {})["items"] = (_extract(r["dom"], selector, limit=30,
                                                        visible_only=vis)
                                               if readable else [])
            r["ui"]["labels"] = (_extract(r["dom"], selector, limit=30, visible_only=vis,
                                          labels=True) if readable else [])
    else:
        fit_recs = recordings
        selector = derive_ground_truth(recordings)
    fitted_on = [str((r.get("param") or {}).get("value") or "") for r in fit_recs]
    for r in recordings:
        r["ground_truth_fitted_on"] = list(fitted_on)
    split.note_fit(PS.value_of(r) for r in fit_recs)
    res.ground_truth_selector = selector
    if not selector:
        res.outcome, res.reason = "nothing", (
            recordings[0].get("recorder_warning")
            or "no cross-value ground truth could be derived from the confirmed page family")
        return res

    base, fresh = recordings[0], recordings[1:]
    chrome = page_chrome(fit_recs)
    res.chrome_filtered = sorted(chrome)[:20]
    res.ground_truth_sample = visible_items(base, exclude=chrome)[:8]
    if not res.ground_truth_sample:
        res.outcome, res.reason = "nothing", (
            "every label on the confirmed page also appears for the other values, so "
            "nothing on screen answers the parameter")
        return res
    res.learned_url = base.get("final_url", "")

    # ---- tier 1: compile and PROVE, exactly as for a human recording -------
    gate = proof_preflight or (lambda compiled: permission_preflight(compiled, base))
    selection, validation = list(split.selection), list(split.validation)
    # a chain is fitted on the selection page, which the result list was fitted on too;
    # with no page free for it, on another result's name from the first page's search
    compiled, proof, chain_fit = compile_and_verify_with_fit(
        base, validation, selection, executor=executor, preflight=gate,
        selection_recordings=selection, split=split, model_scope="compile")
    chain_hint = ""
    if chain_fit is not None and compiled.chain_needs_fitting:
        chain_hint = (f". The page for {(chain_fit.get('param') or {}).get('value')!r} does not "
                      f"open a result the way the first page does, so it cannot fit the "
                      f"chain: learn again")
    elif compiled.chain_needs_fitting:
        tried = (f". A value taken from the first page's own search results did not fit it "
                 f"({compiled.chain_fit_note})" if compiled.chain_fit_note else "")
        chain_hint = (f"{tried}. The page opens a search result with a second request, so "
                      f"learning it needs {min_values + PS.N_SELECTION} values: the second "
                      f"fits the chain and the later ones prove it. "
                      + ("The second value's page has no JSON traffic to fit it on: learn "
                         "again" if selection else "Give another value"))
        compiled.reason += chain_hint
        proof.reason += chain_hint
    res.compiled = compiled.to_dict()
    res.proof = mark_generated(proof.to_dict(), res.generated)
    res.split = dict(proof.split)
    res.proof_status, res.proof_scope = proof.effective_status, proof.scope
    if proof.passed:
        lat = [c.elapsed_ms for c in proof.cases if c.elapsed_ms]
        res.outcome = "tier1"
        res.later_run_ms = statistics.median(lat) if lat else None
        res.later_run_model_calls = 0
        res.reason = proof.reason
        if persist is not None:
            res.persisted = persist(compiled, proof, recordings, res)
        return res

    # ---- tier 2: replay the confirmed navigation ----------------------------
    res.tier2 = _try_tier2(base, validation, cand.param, replay, chrome,
                           fitting=selection if split.held_out else None)
    if res.tier2.get("ok"):
        # tier 2 fits its URL template on [base, *fitting] (or on every page
        # when nothing is held out) and replays the validation values
        res.proof_status = PS.status_for(True, split.held_out)
        res.split = split.to_dict(len(validation))
        res.proof_scope = (f"Tier-2 browser replay reproduced {len(validation)} "
                           f"validation input(s) "
                           + ("held out from fitting and selection"
                              if split.held_out else
                              "that also took part in fitting (not held out)")
                           + f"; status {res.proof_status}.")
        res.outcome = "tier2"
        res.later_run_ms = res.tier2.get("median_ms")
        res.later_run_model_calls = 0
        res.reason = res.tier2.get("note", "")
        before = res.reason
        if persist is not None:
            # the persister saves it as a tool served by the browser replay
            res.persisted = persist(None, None, recordings, res)
        if res.persisted:
            res.reason = f"{BROWSER_TOOL_NOTE}. Replay proof: {before}"
        else:
            refused = res.reason[len(before):] if res.reason.startswith(before) else ""
            res.outcome = "tier2_nonpersistent"
            res.reason = ("tier 2 reproduced every validation input, but no runnable "
                          "artifact was persisted" + refused
                          + "; the candidate remains available")
        return res

    res.outcome = "nothing"
    res.reason = (f"the compiled request did not reproduce the confirmed page "
                  f"({(proof.reason or compiled.reason or '')[:160]}); "
                  f"tier 2: {res.tier2.get('reason', 'not attempted')}{chain_hint}")
    return res


def _is_navigation(run: Any) -> bool:
    steps = getattr(run, "steps", None) or []
    return bool(steps) and all(str(s.get("action")) == "navigate" for s in steps
                               if isinstance(s, dict) and "action" in s)


# ---------------------------------------------------------------------------
# generated values: what learn proposes when fewer than PROVED_VALUES are given
# ---------------------------------------------------------------------------

#: a trailing version is not part of a name ("serde v1.0.210" -> "serde")
_VERSION = re.compile(r"^v?\d+(?:\.\d+)+\S*$|^v\d+\S*$", re.I)
_EDGE = " \t-–—|·•,;:"

VALUES_SCHEMA = {"type": "object",
                 "properties": {"values": {"type": "array", "items": {"type": "string"}}},
                 "required": ["values"], "additionalProperties": False}


def _as_input(text: Any) -> str:
    """A result name as an input value, or "" when it is not one."""
    words = " ".join(str(text or "").split()).strip(_EDGE).split()
    while len(words) > 1 and _VERSION.match(words[-1]):
        words.pop()
    value = " ".join(words).strip(_EDGE)
    if not 2 <= len(value) <= 60 or len(words) > 6:
        return ""
    if not re.search(r"[^\W\d_]", value) or _EMPTY_RESULT.search(value):
        return ""
    return value


def _new_inputs(names: Sequence[Any], exclude: Sequence[str]) -> List[str]:
    from anyuse.execution.api_compile import normalise
    seen = {normalise(v) for v in exclude}
    out = []
    for name in names:
        value = _as_input(name)
        key = normalise(value)
        if value and key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def values_from_pages(cand: Candidate, runs: Sequence[Any],
                      exclude: Sequence[str] = ()) -> List[str]:
    """Names on the first value's result page, in page order: other results are
    inputs of the same kind. With a second usable page the list is the group
    that changed between them (``derive_list_items``); else the group outside
    page chrome whose rows name the value (:func:`_naming_group`)."""
    from anyuse.execution.html_extract import derive_list_items, extract
    dom = str((cand.recording or {}).get("dom") or "")
    if not dom:
        return []
    selector = ""
    pages = [r for r in runs if getattr(r, "dom", "")]
    if pages:
        try:
            selector, _items = derive_list_items([dom, *[r.dom for r in pages]],
                                                 values=[cand.value, *[r.value for r in pages]])
        except Exception:  # noqa: BLE001 - an unreadable page offers no names
            selector = ""
    selector = selector or _naming_group(dom, cand.value)
    if not selector:
        return []
    try:
        names = extract(dom, selector, limit=40, labels=True)
    except ValueError:
        return []
    return _new_inputs(names, exclude)


def _naming_group(dom: str, value: str) -> str:
    """One page's result rows: the row selector of the largest repeated group outside
    nav, header, footer and sidebars whose rows name ``value``. "" when no group
    does; the model is asked then. The largest group by text alone is often a footer."""
    from anyuse.execution.html_extract import (_names, _page_text_len, _row_selector,
                                               in_chrome, parse, repeated_groups)
    try:
        root = parse(dom)
    except Exception:  # noqa: BLE001 - an unreadable page offers no names
        return ""
    total = _page_text_len(root)
    for rows in repeated_groups(root)[:20]:
        if in_chrome(rows[0], total) or not _names(value, [r.text() for r in rows]):
            continue
        selector = _row_selector(rows)
        if selector:
            return selector
    return ""


def values_from_model(cand: Candidate, given: Sequence[str], n: int, *, model: Any = None,
                      exclude: Sequence[str] = ()) -> Tuple[List[str], str, Any]:
    """``(values, note, model result)``: ONE call to the configured model for ``n``
    more inputs of the kind ``given`` are. Only the task, the site and the given
    values are sent. ``model``: a ``ModelAdapter`` or anything with its ``complete``."""
    from anyuse.execution import model_adapter as M
    if model is None:
        try:
            model = M.ModelAdapter.for_role("agent", feature="learn_values")
            if M.credential_status(model.config) == "unset":
                return [], "no model is configured (docs/MODEL_CONFIGURATION.md)", None
        except Exception as exc:  # noqa: BLE001 - an unusable configuration is no model
            return [], f"no model is configured ({type(exc).__name__})", None
    site = urllib.parse.urlsplit(cand.start_url).netloc or cand.start_url
    prompt = (f"A tool runs this task on {site}: {cand.task!r}, with {{{cand.param}}} "
              f"replaced by an input. Inputs that work: {json.dumps(list(given))}. "
              f"Give {n} more inputs of the same kind: things a person would enter "
              f"there that exist on {site} and have results, none of them in the list "
              f"above. Answer with JSON: {{\"values\": [...]}}.")
    try:
        out = model.complete([{"role": "system",
                               "content": "You suggest test inputs. Answer with JSON only."},
                              {"role": "user", "content": prompt}],
                             temperature=0.2, max_tokens=400, json_schema=VALUES_SCHEMA,
                             schema_name="values")
    except Exception as exc:  # noqa: BLE001 - a failed call generates nothing, it does not stop learn
        return [], f"the model call failed ({type(exc).__name__})", None
    if not getattr(out, "ok", False):
        return [], f"the model did not answer ({str(getattr(out, 'error', '') or 'no answer')[:120]})", out
    data = getattr(out, "parsed", None)
    if not isinstance(data, dict):
        try:
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", str(out.text or "").strip()).strip())
        except ValueError:
            data = {}
    raw = data.get("values") if isinstance(data, dict) else None
    values = _new_inputs([v for v in (raw or []) if isinstance(v, str)], [*given, *exclude])[:n]
    return values, ("" if values else "the model proposed no usable value"), out


class SiteEmptyPage:
    """The empty-answer cascade the compiled tools use (``result_contract.empty_verdict``), for the pages learn
    opens for the values it generated: the site's own empty page, learned once from two searches for made-up
    inputs through the confirmed URL (``ANYUSE_EMPTY_PROBE``; never through the agent) against the result pages
    in hand, then the statement rule bound to the query. No model is asked here (no model-check scope is open)."""

    def __init__(self, cand: Candidate, good: Sequence[Any], navigate: Optional[Callable[[str], Any]] = None):
        from anyuse.execution import empty_page as EP
        from anyuse.execution.html_extract import derive_list_items
        self.cand, self.navigate = cand, navigate
        dom = str((cand.recording or {}).get("dom") or "")
        pages = [r for r in good if getattr(r, "dom", "")]
        self.selector = ""
        if dom and pages:
            try:
                self.selector, _items = derive_list_items([dom, *[r.dom for r in pages]],
                                                          values=[cand.value, *[r.value for r in pages]])
            except Exception:  # noqa: BLE001 - no result list read: the preview's group
                self.selector = ""
        self.selector = self.selector or str(cand.preview_selector or "")
        probe = EP.probe_enabled() and navigate is not None and bool(cand.url_template)
        digest = hashlib.sha256(str(cand.url_template or cand.final_url).encode()).hexdigest()[:16]
        self.ctx = EP.Context(positives=[cand.value, *[r.value for r in pages]], probe=probe, learn=False,
                              digest=digest)
        self.results = [self._page(cand.value, dom, str(((cand.recording or {}).get("ui") or {}).get("text") or ""),
                                   cand.final_url), *[self.page(r) for r in pages]]
        self.learned = not probe
        self.requests = 0
        #: the pages already in hand, by normalised input: a check that asks for one again sends nothing
        self.seen: Dict[str, Any] = {}
        for pg in self.results:
            self.seen.setdefault(self._key(pg.value), pg)

    @staticmethod
    def _key(value: str) -> str:
        from anyuse.execution.api_compile import normalise
        return normalise(value) or str(value)

    def _page(self, value: str, dom: str, text: str, url: str = "", ok: bool = True) -> Any:
        from anyuse.execution import empty_page as EP
        from anyuse.execution.html_extract import extract
        items: List[str] = []
        if self.selector and dom:
            try:
                items = extract(dom, self.selector, limit=30)
            except ValueError:
                items = []
        return EP.Page("html", value, markup=dom, text=text, items=items, url=url, ok=ok and bool(dom))

    def page(self, run: Any) -> Any:
        return self._page(str(getattr(run, "value", "") or ""), str(getattr(run, "dom", "") or ""),
                          str(getattr(run, "text", "") or ""), str(getattr(run, "final_url", "") or ""),
                          ok=bool(getattr(run, "ok", False)))

    def fetch(self, value: str) -> Any:
        from anyuse.execution import empty_page as EP
        key = self._key(value)
        if key in self.seen:
            return self.seen[key]
        self.requests += 1
        try:
            run = self.navigate(value) if self.navigate is not None else None
        except Exception as exc:  # noqa: BLE001 - a navigation that fails is an input without an answer
            run, why = None, f"the navigation failed ({type(exc).__name__})"
        else:
            why = "no page for this input"
        got = EP.Page("html", value, ok=False, error=why) if run is None else self.page(run)
        self.seen[key] = got
        return got

    def verdict(self, run: Any) -> Any:
        """``empty_page.Evidence`` for the page of ``run``: EMPTY, NOT_EMPTY or UNKNOWN."""
        from anyuse.execution import empty_page as EP
        from anyuse.execution import result_contract as RC
        if not self.learned:
            self.learned = True
            values = EP.nonsense_values([self.cand.value, *self.ctx.positives], seed=self.ctx.digest)
            try:
                sig = EP.learn("html", self.results, [self.fetch(v) for v in values])
            except Exception:  # noqa: BLE001 - a made-up search that fails leaves no signature
                sig = None
            if EP.usable(sig) and sig.get("kind") == "html":
                self.ctx.learned = sig
        page = self.page(run)
        self.seen.setdefault(self._key(page.value), page)
        fetch = self.fetch if self.ctx.probe else None
        return RC.empty_verdict(None, page, ctx=self.ctx, fetch=fetch, positive_ok=lambda p: bool(p.items))


def _generated_page_ok(cand: Candidate, run: Any, landing: Optional[Callable],
                       empty: Optional[SiteEmptyPage] = None) -> Tuple[bool, str]:
    """A generated value is kept when its page is the confirmed page with it bound, is not the site's empty
    page (:class:`SiteEmptyPage`) and shows a list that changed from the confirmed page's. The phrase list
    (:data:`_EMPTY_RESULT`) is the last rung: a phrase the confirmed page shows too (a cart line "0 items") is
    the site's chrome, not a statement about this search."""
    from anyuse.execution import empty_page as EP
    if not getattr(run, "ok", False):
        return False, run.error or "no DOM captured"
    ok, why = (landing or landing_check)(cand, run)
    if not ok:
        return False, (f"ended on {run.final_url[:100]!r}, which is not the confirmed page "
                       f"with {run.value!r} bound: {why}")
    ev = None
    if empty is not None:
        try:
            ev = empty.verdict(run)
        except Exception:  # noqa: BLE001 - no verdict: the next rungs decide
            ev = None
    if ev is not None and ev.state == EP.EMPTY:
        return False, f"the page is an empty result ({ev.by}: {ev.why[:120]})"
    phrase = _new_empty_phrase(cand, run)
    if not _changed_list(cand, run):
        return False, ("the page states an empty result" if phrase else
                       "the page shows no list that changed from the confirmed page's")
    if phrase and (ev is None or ev.state != EP.NOT_EMPTY):
        return False, f"the page states an empty result ({phrase!r})"
    return True, ""


def _empty_phrases(text: Any) -> List[str]:
    return [" ".join(m.group(0).lower().split()) for m in _EMPTY_RESULT.finditer(" ".join(str(text or "").split()))]


def _new_empty_phrase(cand: Candidate, run: Any) -> str:
    """An empty-result phrase on ``run``'s page that the confirmed page does not show, or ``""``."""
    rec = cand.recording or {}
    seen = set(_empty_phrases((rec.get("ui") or {}).get("text") or "")) | set(_empty_phrases(_TAG.sub(
        " ", str(rec.get("dom") or ""))))
    return next((p for p in _empty_phrases(getattr(run, "text", "")) if p not in seen), "")


def _changed_list(cand: Candidate, run: Any) -> bool:
    """Does the page hold a repeated group that is on the confirmed page too and
    changed with the value? Page chrome repeats unchanged on both; an empty result
    page has no result rows to change."""
    from anyuse.execution.html_extract import derive_list_items
    dom = str((cand.recording or {}).get("dom") or "")
    if not dom or not getattr(run, "dom", ""):
        return False
    try:
        selector, items = derive_list_items([run.dom, dom], values=[run.value, cand.value])
    except Exception:  # noqa: BLE001 - an unreadable page shows no list
        return False
    return bool(selector and items)


def _generated_runs(cand: Candidate, good: Sequence[Any], given: Sequence[str], missing: int, *,
                    fetch: Callable, landing: Optional[Callable], res: LearnResult,
                    model: Any, over_budget: Callable[[], bool],
                    empty: Optional[SiteEmptyPage] = None) -> List[Any]:
    """Fetch up to ``missing`` generated values: names from the result pages first,
    then one model call. A value is kept only when its page passes the landing
    check and shows a list; a rejected one is listed under ``discarded``."""
    from anyuse.execution.agent_browser import spend_usd
    from anyuse.execution.api_compile import normalise
    kept: List[Any] = []
    tried: List[str] = list(given)

    def attempt(values: Sequence[str], source: str) -> None:
        for value in values:
            if len(kept) >= missing or over_budget():
                return
            if normalise(value) in {normalise(v) for v in tried}:
                continue
            tried.append(value)
            run = fetch(value)
            row = run.to_dict()
            row["generated"] = source
            res.runs.append(row)
            ok, why = _generated_page_ok(cand, run, landing, empty)
            if not ok:
                res.discarded.append({"value": value, "why": why, "generated": source})
                continue
            kept.append(run)
            res.generated.append({"value": value, "from": source})

    attempt(values_from_pages(cand, good, exclude=tried)[:missing + GENERATE_SPARE], FROM_PAGE)
    if len(kept) < missing and not over_budget():
        asked, note, out = values_from_model(cand, given, missing - len(kept) + GENERATE_SPARE,
                                             model=model, exclude=tried)
        if out is not None:
            usage = getattr(out, "usage", None)
            tin = int(getattr(usage, "prompt_tokens", 0) or 0)
            tout = int(getattr(usage, "completion_tokens", 0) or 0)
            # the requests actually sent: a call refused before sending counts none
            sent = getattr(out, "llm_calls", None)
            if sent is None:   # a model object that does not report it: one per answer
                sent = 1 if getattr(out, "ok", False) else 0
            res.model_calls += int(sent or 0)
            res.tokens_in += tin
            res.tokens_out += tout
            res.cost_usd = round(res.cost_usd + (float(getattr(usage, "cost_usd", 0.0) or 0.0)
                                                 or spend_usd(tin, tout)), 5)
        res.generate_note = note
        attempt(asked, FROM_MODEL)
    if len(kept) < missing:
        res.generate_note = "; ".join(x for x in (
            f"{len(kept)} of {missing} missing value(s) could be generated", res.generate_note) if x)
    else:
        res.generate_note = ""
    return kept


def mark_generated(proof: Dict[str, Any], generated: Sequence[dict]) -> Dict[str, Any]:
    """``proof`` with each case of a generated value marked, and the generated values listed."""
    by_value = {str(g.get("value")): str(g.get("from")) for g in generated or []}
    if not by_value:
        return proof
    for case in proof.get("cases") or []:
        if isinstance(case, dict) and str(case.get("value")) in by_value:
            case["generated"] = by_value[str(case.get("value"))]
    proof["generated_values"] = [dict(g) for g in generated]
    return proof


def _run_summary(cand: Candidate) -> dict:
    rec = cand.recording or {}
    agent = rec.get("agent") or {}
    return {"value": cand.value, "task": cand.task, "start_url": cand.start_url,
            "final_url": cand.final_url, "steps": [{"action": "agent",
                                                    "steps": agent.get("steps", 0)}],
            "typed": list(agent.get("typed") or []), "model_calls": cand.model_calls,
            "cached_calls": 0, "tokens_in": cand.tokens_in,
            "tokens_out": cand.tokens_out, "seconds": round(cand.agent_seconds, 3),
            "dom_bytes": len(rec.get("dom") or ""), "requests": len(rec.get("network") or []),
            "error": cand.error, "candidate": True}


#: an empty-result statement on a search page (a few languages; whole phrases)
_EMPTY_RESULT = re.compile(
    r"(?i)\b(no (?:results?|matches|items|cards|records|entries|documents)(?: were)? found"
    r"|(?:0|zero) (?:results?|matches|items|cards|records|entries|hits)\b"
    r"|nothing (?:was )?found|did(?:n't| not) match any|no results for"
    r"|keine (?:treffer|ergebnisse)|aucun r[ée]sultat|sin resultados|nessun risultato)")


#: try the result-region model when no repeated group changed
RESULT_REGION = True


def no_list_diagnosis(recordings: List[dict]) -> str:
    """Why no result list was found, from the captured pages alone.

    ``"<class>: <explanation>"`` or ``""``. Classes: ``unchanged`` (the pages
    of different values are the same page), ``not_rendered`` (no page shows
    its value: the results were never rendered in the captured page),
    ``empty_results`` (the pages state an empty result). Diagnostic only: it
    names the failure, it never changes an outcome."""
    pages = [(str((r.get("param") or {}).get("value") or ""),
              " ".join(str((r.get("ui") or {}).get("text") or "").split()),
              str(r.get("dom") or "")) for r in recordings if r.get("dom")]
    if len(pages) < 2:
        return ""
    if len({d for _v, _t, d in pages}) == 1 or len({t for _v, t, _d in pages if t}) == 1:
        return ("unchanged: the result pages of different values are the same page "
                "(the search did not run in the captured page, or its results come "
                "from a script or host the capture did not load)")
    shown = [v.lower() in t.lower() for v, t, _d in pages if v]
    if shown and not any(shown):
        return ("not_rendered: no result page shows its value (the results were not "
                "rendered in the captured page: a refused third-party or scripted "
                "search backend)")
    empty = [bool(_EMPTY_RESULT.search(t)) for _v, t, _d in pages]
    if sum(empty) * 2 >= len(empty):
        return "empty_results: the result pages state an empty result for the values"
    return ""


def derive_ground_truth(recordings: List[dict], implied_end_tags: bool = False) -> str:
    """Fill ``ui.items`` across the recordings; return the selector or ``""``.

    The same function and the same rule the recorder uses. An agent's or a
    substituted recording is given no easier standard. ``implied_end_tags``:
    the pages are raw server responses, not DOM snapshots (see
    ``html_extract.parse``).
    """
    from anyuse.execution.api_compile import error_document
    from anyuse.execution.html_extract import derive_list_items, extract
    usable = []
    for r in recordings:
        err = error_document(r) if r.get("dom") else ""
        if err:
            # an error page's changing ids are not a result list
            r["recorder_warning"] = f"the result page is an error page ({err})"
            r.setdefault("ui", {})["items"] = []
        elif r.get("dom"):
            usable.append(r)
    if len(usable) < 2:
        errs = [r for r in recordings if r.get("dom") and r not in usable]
        if errs:
            # name it (a site may serve a search with no match as HTTP 404,
            # so only one page was left to compare)
            why = (f"error_pages: {len(errs)} of {len(recordings)} result page(s) were "
                   f"served as an error ({error_document(errs[0])}); a search without a "
                   f"match may be answered that way")
            for r in recordings:
                if r not in errs:
                    r["recorder_warning"] = ("no result list could be derived: fewer than "
                                             f"two usable result pages ({why})")
                r["no_list_class"] = "error_pages"
        return ""
    # the values let a group that NAMES each page's query outrank page
    # furniture that merely changes with it (html_extract.derive_list_items)
    selector, _items = derive_list_items(
        [r["dom"] for r in usable],
        values=[str((r.get("param") or {}).get("value") or "") for r in usable],
        implied_end_tags=implied_end_tags)
    visible_only = False
    if not selector and any(r.get("before_dom") for r in usable):
        # a search reached by TYPING (the recordings carry the pre-typing page):
        # read the pages visible-only and look for the region the typing changed
        from anyuse.execution.html_extract import derive_typed_items
        before = next((r["before_dom"] for r in usable if r.get("before_dom")), "")
        selector, _items, _how = derive_typed_items(
            [(str((r.get("param") or {}).get("value") or ""), r["dom"]) for r in usable],
            before=before, implied_end_tags=implied_end_tags)
        visible_only = bool(selector)
    if not selector and RESULT_REGION:
        # the result-region model (html_extract.derive_result_region):
        # heterogeneous or short answers that no repeated group explains
        from anyuse.execution.html_extract import derive_result_region
        before = next((r["before_dom"] for r in usable if r.get("before_dom")), "")
        selector, _items, _how = derive_result_region(
            [(str((r.get("param") or {}).get("value") or ""), r["dom"]) for r in usable],
            before=before, implied_end_tags=implied_end_tags)
        visible_only = bool(selector)
        if selector:
            for r in recordings:
                r["ground_truth_how"] = _how
    if not selector:
        why = no_list_diagnosis(usable)
        for r in recordings:
            r["ground_truth_selector"] = ""
            r["ui"]["items"] = []
            r["recorder_warning"] = (
                "no repeated group on the confirmed page family changed between the "
                "values: the page does not answer the parameter in a way that can be "
                "checked" + (f" ({why})" if why else ""))
            r["no_list_class"] = why.split(":", 1)[0] if why else ""
        return ""
    for r in recordings:
        r["ground_truth_selector"] = selector
        if visible_only:
            r["ground_truth_visible_only"] = True
        usable_dom = r.get("dom") and not error_document(r)
        # items: the whole row text (what a browser replay of the selector reads back);
        # labels: each row's own name (its heading or main link), which is what a
        # request's field carries; the compiler falls back to the labels
        r["ui"]["items"] = (extract(r["dom"], selector, limit=30, visible_only=visible_only,
                                    implied_end_tags=implied_end_tags)
                            if usable_dom else [])
        r["ui"]["labels"] = (extract(r["dom"], selector, limit=30, visible_only=visible_only,
                                     labels=True, implied_end_tags=implied_end_tags)
                             if usable_dom else [])
    return selector


def _try_tier2(base: dict, fresh: List[dict], param: str, replay,
               chrome, fitting: Optional[List[dict]] = None) -> Dict[str, Any]:
    """Re-bind the confirmed navigation to the other values and replay it.

    Tier 2 replays a NAVIGATION, never the agent's clicks: the cdp rung is
    navigation-only by design. A task whose answer is only reachable by
    clicking is reported as refused.
    """
    from anyuse.execution.api_compile import visible_items
    from anyuse.execution.cdp_replay import (finalize_minimum, for_validation,
                                             template_from_recording)
    if replay is None:
        return {"ok": False, "reason": "no browser replay endpoint was given"}
    # the template is fitted on the confirmed page (+ selection pages when the
    # caller separated them); ``fresh`` are only replayed and compared
    recordings = [base, *fitting] if fitting is not None else [base, *fresh]
    # a replay executor may build its own template from the SAME fitted
    # recordings (``discover_replay``: a typed search is replayed by typing); the
    # comparison below is the same
    make = getattr(replay, "template_from_recording", None) or template_from_recording
    template = make(base, param, recordings=recordings, readiness=True)
    if not template.url:
        return {"ok": False, "reason": f"refused: {template.notes[:180]}"}
    # where in a longer word of a script without spaces a result's name may hold the
    # input: what the result lists of the confirmed page and its fitting pages show
    # (``query_match.calibrate``); the validation pages never set it
    from anyuse.execution import result_contract as RC
    from anyuse.execution.api_verify import fitting_positions
    RC.set_positions(template, *fitting_positions([base, *(fitting or [])], chrome))

    lats, failures, checked = [], [], []
    proof_template = for_validation(template)      # the comparison is the count check
    counts: List[int] = []
    for rec in fresh:
        value = (rec.get("param") or {}).get("value", "")
        out = replay.execute(proof_template, {param: value})
        if out.get("elapsed_ms"):
            lats.append(out["elapsed_ms"])
        if not out.get("success"):
            failures.append(f"{value!r}: {str(out.get('error'))[:120]}")
            continue
        got = [str(x) for x in (out.get("extracted") or {}).get("items") or []]
        counts.append(len(got))
        want = visible_items(rec, exclude=chrome)
        ok, note = _same_answer(got[:40], want[:40])
        checked.append({"value": value, "ok": ok, "note": note,
                        "coverage": round(_coverage(got[:40], want[:40]), 3),
                        "ui_count": len(want), "replay_count": len(got)})
        if not ok:
            failures.append(f"{value!r}: {note}")
    if failures:
        return {"ok": False, "reason": "; ".join(failures)[:220],
                "url": template.url}
    finalize_minimum(template, counts)
    note = "; ".join(f"{row['value']!r}: {row['note']}" for row in checked)
    return {"ok": True, "reason": "", "note": note,
            "url": template.url, "path_params": dict(template.path_params),
            # the template the validation values were replayed with: what a saved tool runs
            "template": template.to_dict(),
            "validation": checked,
            "median_ms": statistics.median(lats) if lats else None,
            "model_calls": 0}


def _coverage(got: List[str], want: List[str]) -> float:
    """Share of the ground-truth labels the replay returned."""
    from anyuse.execution.api_compile import normalise, values_match
    want_n = [normalise(w) for w in want if normalise(w)]
    got_n = [normalise(g) for g in got if normalise(g)]
    if not want_n or not got_n:
        return 0.0
    return sum(1 for w in want_n if any(values_match(w, g) for g in got_n)) / len(want_n)


def _same_answer(got: List[str], want: List[str], min_values: int = 3):
    """The SAME predicate tiers 1 and 2 are held to in `execution/web_runtime/ladder.py`."""
    from anyuse.execution.api_compile import normalise
    want_n = [normalise(w) for w in want if normalise(w)]
    got_n = [normalise(g) for g in got if normalise(g)]
    if len(want_n) < min_values:
        return False, (f"insufficient evidence: {len(want_n)} ground-truth value(s), "
                       f"{min_values} required")
    if not got_n:
        return False, "the replay returned nothing"
    cov = _coverage(got, want)
    if cov < 0.8:
        return False, f"coverage {cov:.2f} < 0.80 against what the confirmed page family showed"
    return True, f"coverage {cov:.2f} of {len(want_n)} values the confirmed page family showed"


#: words a learned tool's name leaves out ("search example.org for {q}" -> example_org_search)
_NAME_STOP = frozenset("a an the for of on in at to and or with by from this that site page website "
                       "its their all list find me".split())


def _workflow_name(start_url: str, task: str) -> str:
    """A short readable tool name: the site and the task's first words (example_org_search)."""
    host = urllib.parse.urlsplit(start_url).netloc.split(":", 1)[0].lower()
    host = host[4:] if host.startswith("www.") else host
    host_slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    text = re.sub(r"\{[^}]*\}", " ", task.lower())
    for piece in sorted({host, "www." + host}, key=len, reverse=True):
        if piece:
            text = text.replace(piece, " ")
    host_words = set(host_slug.split("_"))
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in _NAME_STOP and w not in host_words]
    verb = "_".join(words[:2])
    if host_slug and verb:
        return f"{host_slug}_{verb}"
    return host_slug or verb or "learned"


def _free_name(base: str, task: str, start_url: str) -> str:
    """``base``, or base_2, base_3 ... when a compiled tool of that name was learned for another task."""
    try:
        from anyuse.mcpserver.compiled_tools import _find_record
    except Exception:  # noqa: BLE001
        return base
    name, n = base, 1
    while True:
        rec = _find_record(name)
        learned = ((rec or {}).get("source") or {}).get("learned_from") or {}
        if rec is None or (learned.get("task") == task and learned.get("start_url") == start_url):
            return name
        n += 1
        name = f"{base}_{n}"


def _proved_request_status(name: str) -> str:
    """The proof status of the saved tool ``name`` when it is a compiled request whose
    proof passed; "" when there is none, or it is browser-only or unproved."""
    try:
        from anyuse.mcpserver.compiled_tools import KIND_BROWSER, _find_record
    except Exception:  # noqa: BLE001
        return ""
    rec = _find_record(name)
    if not rec or rec.get("kind") == KIND_BROWSER:
        return ""
    proof = rec.get("proof") or {}
    return str(proof.get("status") or "proof passed") if proof.get("passed") else ""


# ---------------------------------------------------------------------------
# candidates on disk — so a confirmation can happen later without a second run
# ---------------------------------------------------------------------------

CANDIDATES_DIR = "learn_candidates"
CANDIDATE_TTL_HOURS = 24.0


def candidates_dir(data_dir: Optional[str] = None) -> str:
    import os
    if data_dir is None:
        from anyuse.paths import anyuse_data_dir
        data_dir = str(anyuse_data_dir())
    return os.path.join(data_dir, CANDIDATES_DIR)


def save_candidate(cand: Candidate, data_dir: Optional[str] = None) -> str:
    """Write the candidate (with its recording) so `confirm` can pick it up.

    This is NOT persistence of anything learned: it is the proposal, kept
    only so the agent need not run a second time when the person answers.
    """
    import os
    d = candidates_dir(data_dir)
    os.makedirs(d, exist_ok=True)
    cleanup_candidates(data_dir)
    path = os.path.join(d, f"{cand.candidate_id}.json")
    with open(path, "w") as fh:
        json.dump(cand.to_dict(with_recording=True), fh)
    os.chmod(path, 0o600)
    return path


def cleanup_candidates(data_dir: Optional[str] = None,
                       max_age_hours: Optional[float] = None) -> int:
    """Delete expired proposal records from the explicit candidate store."""
    import os
    import time as _time
    if max_age_hours is None:
        from anyuse.settings import get as get_setting
        max_age_hours = float(get_setting("ANYUSE_LEARN_CANDIDATE_TTL_HOURS"))
    directory = candidates_dir(data_dir)
    if not os.path.isdir(directory):
        return 0
    cutoff = _time.time() - max(0.0, float(max_age_hours)) * 3600.0
    removed = 0
    for name in os.listdir(directory):
        if not re.fullmatch(r"[0-9a-f]{12}\.json", name):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def load_candidate(candidate_id: str, data_dir: Optional[str] = None) -> Optional[Candidate]:
    import os
    if not re.fullmatch(r"[0-9a-f]{12}", str(candidate_id or "")):
        return None
    path = os.path.join(candidates_dir(data_dir), f"{candidate_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return Candidate.from_dict(json.load(fh))


def discard_candidate(candidate_id: str, data_dir: Optional[str] = None) -> None:
    import os
    path = os.path.join(candidates_dir(data_dir), f"{candidate_id}.json")
    try:
        os.unlink(path)
    except OSError:
        pass


def explicit_confirmation(cand: Candidate, restated_url: str) -> Tuple[bool, str]:
    """A non-interactive confirmation must restate the candidate's URL exactly.

    A confirmation that names nothing is not a confirmation; this is what
    stops a script — or an MCP client — from saying yes to a page it never
    looked at.
    """
    if not restated_url:
        return False, "no URL was restated; a confirmation must name the page it confirms"
    ok, why = same_url(cand.final_url, restated_url)
    if not ok:
        return False, f"the restated URL is not the candidate's: {why}"
    ok2, why2 = same_url(restated_url, cand.final_url)
    if not ok2:
        return False, f"the restated URL is not the candidate's: {why2}"
    return True, "the restated URL is the candidate's"


# ---------------------------------------------------------------------------
# persistence — the ONLY thing that writes, and it changes no rule
# ---------------------------------------------------------------------------

def make_persister(recordings_dir: str = "", robots_policy: Optional[str] = None):
    """A ``persist`` callable that writes exactly what the human path writes.

    Nothing is relaxed because an agent found the page: the host must still be
    acknowledged, the robots policy still applies, and only a template whose
    fresh-input proof PASSED is persisted. A page only the browser replay proved
    (tier 2) is saved as a tool served by that replay (:func:`replay_tool_record`),
    except over a compiled request of the same name whose proof passed: that one is kept.
    """
    import os

    def persist(compiled, proof, recordings, result: LearnResult) -> Optional[str]:
        from anyuse.execution.api_compile import (compiled_record, persist_compiled,
                                                  with_runtime_rungs)
        from anyuse.execution.api_permission import check_permission
        from anyuse.memory.storage import WorkflowStorage

        # a tool of the same name learned for another task is never replaced
        result.workflow = _free_name(result.workflow, result.task, result.start_url)
        if recordings_dir:
            os.makedirs(recordings_dir, exist_ok=True)
            for rec in recordings:
                path = os.path.join(recordings_dir,
                                    f"{result.workflow}.{rec['tag']}.json")
                with open(path, "w") as fh:
                    json.dump(rec, fh)
        replay_proved = bool(recordings and compiled is None and proof is None
                             and (result.tier2 or {}).get("ok")
                             and (result.tier2 or {}).get("template"))
        if not replay_proved and (compiled is None or proof is None or not proof.passed):
            return None
        if replay_proved:
            # a re-learn whose compiled request failed this time (a 503, fewer values)
            # never replaces a proved compiled request with a browser-only tool
            kept = _proved_request_status(result.workflow)
            if kept:
                result.reason += (f" — NOT persisted: {result.workflow} already has a compiled "
                                  f"request ({kept}), which needs no browser; it was kept. "
                                  f"Run `anyuse workflow delete {result.workflow}` "
                                  f"first to replace it with a tool served by the browser replay.")
                return None
            from anyuse.execution.cdp_replay import ReplayTemplate
            replay = ReplayTemplate.from_dict(result.tier2["template"])
            if not replay.url or replay.problems():
                return None
            url = replay.url
        else:
            url = getattr(compiled.template, "url", "")
        permission = check_permission(url, policy=robots_policy)
        if not permission.may_prefer:
            # the CLI asks here when the proof never asked (a page only the browser replay reproduced)
            from anyuse.execution.api_permission import PURPOSE_SAVE, ask_for_host
            asked, why_not = ask_for_host(url, permission, purpose=PURPOSE_SAVE)
            if why_not:
                result.reason += f" — NOT persisted: {why_not}"
                return None
            if asked:
                permission = check_permission(url, policy=robots_policy)
        if not permission.may_prefer:
            result.reason += f" — NOT persisted: {permission.reason}"
            return None
        storage = WorkflowStorage()
        wf = storage.get(result.workflow)
        source = _learned_source(recordings, result)
        if replay_proved:
            record = replay_tool_record(replay, recordings, result, source=source,
                                        permission=permission.to_dict())
        else:
            record = compiled_record(
                compiled.template, mark_generated(proof.to_dict(), result.generated),
                workflow=result.workflow, chrome_filtered=list(compiled.chrome_dropped),
                permission=permission.to_dict(), source=source)
            # the runtime cascade's lower rungs: the browser replay of this page family and the task
            with_runtime_rungs(record, recordings[0], task=result.task,
                               start_url=result.start_url, origin="learn",
                               recordings=recordings)
            # the pages the tool was learned from, kept for an automatic recompile
            from anyuse.execution.evidence_store import keep_compile_evidence
            n_fit, n_sel = _learned_roles(recordings, result)
            keep_compile_evidence(storage, wf.id if wf else result.workflow, record,
                                  recording=recordings[0], fitting=recordings[1:1 + n_fit],
                                  selection=recordings[1 + n_fit:1 + n_fit + n_sel],
                                  fresh=recordings[1 + n_fit + n_sel:])
        return persist_compiled(storage, wf.id if wf else result.workflow, record)

    return persist


def _learned_roles(recordings: List[dict], result: LearnResult) -> Tuple[int, int]:
    """``(fitting, selection)``: how many of the recordings after the first fitted and
    selected; the rest validated."""
    split = result.split or {}
    # a fitting input generated from the site's search (execution/chain_fit.py) is no page
    n_fit = int(split.get("fitting") or 0) - int(split.get("fitting_generated") or 0)
    n_sel = int(split.get("selection") or 0)
    if n_fit + n_sel + int(split.get("validation") or 0) != len(recordings) - 1:
        # a split that held nothing out counts the later pages as fitting and validation
        # both; with disjoint roles a chain's fitting page comes first
        # (api_verify.compile_and_verify_with_fit)
        n_fit = 0
    return n_fit, n_sel


def _learned_source(recordings: List[dict], result: LearnResult) -> dict:
    n_fit, n_sel = _learned_roles(recordings, result)
    learned = {"task": result.task,
               "start_url": result.start_url,
               "confirmed_by": result.confirmed_by,
               "confirmed_url": (result.candidate or {}).get("final_url", ""),
               "landing_check": result.landing_check,
               "agent_runs": result.agent_runs,
               "navigations": result.navigations,
               "model_calls": result.model_calls,
               "cost_usd": result.cost_usd}
    if result.generated:
        # values learn proposed itself; each was fetched and proved like a given one
        learned["generated_values"] = [dict(g) for g in result.generated]
    names = [f"{result.workflow}.{r.get('tag')}.json" for r in recordings[1:]]
    return {"recording": f"{result.workflow}.{recordings[0].get('tag', 'agent0')}.json",
            "fitting": names[:n_fit],
            "selection": names[n_fit:n_fit + n_sel],
            "fresh": names[n_fit + n_sel:],
            "site": recordings[0].get("site", ""),
            "origin": "agent",
            "learned_from": learned}


def replay_tool_record(replay: Any, recordings: List[dict], result: LearnResult, *,
                       source: dict, permission: Optional[dict] = None) -> dict:
    """The compile record of a tool the browser replay proved and no compiled request did.

    The runtime-cascade record shape, with ``kind = "browser_replay"``: the
    ``browser`` rung is the template the validation values were replayed with,
    the ``task`` rung is the agent's, and ``template`` only DESCRIBES the page
    (URL, parameters, result shape) for the listings and the HIL gate; the
    request rung never runs for this kind (``compiled_tools.run_compiled``).
    """
    from anyuse.execution.api_compile import compiled_record, with_runtime_rungs
    from anyuse.execution.api_step import RequestTemplate
    descriptor = RequestTemplate(
        method="GET", url=replay.url, params=list(replay.params),
        path_params=dict(replay.path_params), param_transforms=dict(replay.param_transforms),
        extract={"items": str(replay.extract.get("items") or "")}, extract_kind="html",
        shape="list", origin="browser_replay",
        notes="no compiled request: the browser replay serves this tool")
    tier2 = result.tier2 or {}
    cases = [{"value": row.get("value"), "passed": bool(row.get("ok")),
              "coverage": row.get("coverage"), "ui_count": row.get("ui_count"),
              "replay_count": row.get("replay_count")}
             for row in tier2.get("validation") or []]
    split = dict(result.split or {})
    proof = mark_generated({
        "passed": True, "rung": "browser_replay", "status": result.proof_status,
        "selection_held_out": bool(split.get("held_out")), "split": split,
        "case_count": len(cases), "passed_count": sum(1 for c in cases if c["passed"]),
        "cases": cases, "reason": result.proof_scope,
        "elapsed_ms_median": tier2.get("median_ms")}, result.generated)
    record = compiled_record(descriptor, proof, workflow=result.workflow,
                             chrome_filtered=list(result.chrome_filtered),
                             permission=permission, source=source)
    record["kind"] = "browser_replay"
    base = recordings[0]
    with_runtime_rungs(record, base, task=result.task, start_url=result.start_url,
                       origin="learn", recordings=recordings)
    # each row's name, as the compiled tools' browser rung reads it, when the pages carried names
    labels = bool((base.get("ui") or {}).get("labels"))
    replay.read_labels = labels
    record["browser"] = {"template": replay.to_dict(), "read": "labels" if labels else "rows",
                         "origin": "learn", "proved": True}
    return record
