"""The product entry points of the agent harness: what ``anyuse agent
run`` and the MCP tool ``agent_run`` share.

* ``make_policy``: ``cerebras`` (``ModelAdapter`` role ``agent``, booked as ``agent_harness`` under the
  daily cap when the process is armed), ``claude`` (``claude -p`` with no tools from an EMPTY directory
  with a clean environment, counted at the process counter with the CLI's cost), ``scripted`` (a JSON
  list of actions: offline checks and tests).
* ``make_toolbox``: ``--site`` -> the browser tool set on a guarded container page (the learn loop's
  endpoint check, the traffic guard, robots); ``--app`` -> the desktop tool set through the guest job
  agent's channel (``desktop_tools.configured_runner``; with no runner attached the typed interlock
  refusal); always the task workspace and the runtime tools over the container sandbox, whose runner is
  wired but refuses until enabled (``harness_sandbox_runner``).
* ``run_agent_task``: one run under the settings' configuration (an unasked irreversible
  action stops by default), optionally compiled; the report every entry point returns.

This module spawns nothing itself: ``claude_runner`` and the container runner are the two places that
may, and both are reached only through the real product paths (never in tests, which inject fakes).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from anyuse.execution import agent_harness as H
from anyuse.execution import harness_policies as P
from anyuse.execution import harness_tools as HT
from anyuse.execution import harness_types as T

#: ``model``: the configured ``agent`` role model (ANYUSE_AGENT_MODEL_* or ANYUSE_MODEL_*: any preset, including
#: claude-cli and codex-cli; Cerebras when nothing is set); ``cerebras`` is its older name; ``claude``: Sonnet through
#: the Claude CLI; ``scripted``: a fixed action list (tests, benches)
POLICIES = ("model", "cerebras", "claude", "scripted")
DEFAULT_POLICY = "model"
MODEL_POLICIES = frozenset({"model", "cerebras"})
FEATURE = "agent_harness"


# ---------------------------------------------------------------------------
# policies
# ---------------------------------------------------------------------------
def clean_env() -> Dict[str, str]:
    """The Claude CLI's environment: no key, no token, nothing of AnyUse's."""
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
           and not k.startswith(("ANYUSE_", "XDG_", "PYTHON", "E2E"))}
    env["HOME"] = os.path.expanduser("~")
    return env


def claude_runner(argv: List[str], stdin_text: str, timeout: float):
    """The real ``claude -p`` runner: an EMPTY working directory, a clean environment, the prompt on stdin.
    Reached only by ``make_policy("claude")`` in a product run."""
    import subprocess  # noqa: S404 - the one product place for the CLI
    cwd = tempfile.mkdtemp(prefix="anyuse-agent-cli-")
    try:
        r = subprocess.run(list(argv), input=stdin_text, capture_output=True, text=True, cwd=cwd, env=clean_env(),
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout:.0f} s"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return r.returncode, r.stdout, r.stderr


def load_script(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise ValueError("a script is a JSON list of action objects")
    return data


def make_policy(name: str, *, runner=None, transport=None, script: Optional[Sequence[Any]] = None,
                max_usd: float = H.DEFAULT_MAX_USD, model: str = "sonnet"):
    name = str(name or DEFAULT_POLICY).strip().lower()
    if name == "scripted":
        return P.ScriptedPolicy(list(script or []))
    if name == "claude":
        # the demonstration's instructions (ANYUSE_DEMO_INSTRUCTIONS), read once per policy build
        return P.ModelPolicy(P.claude_cli_call(runner or claude_runner, model=model, feature=FEATURE),
                             name=f"claude-cli:{model}", system=P.system_prompt())
    if name in MODEL_POLICIES:
        from anyuse.execution.model_adapter import RunBudget
        return P.cerebras_policy(transport=transport, budget=RunBudget(max_usd), feature=FEATURE,
                                 system=P.system_prompt())
    raise ValueError(f"policy must be one of {', '.join(POLICIES)}, not {name!r}")


# ---------------------------------------------------------------------------
# the compile's demonstration model
# ---------------------------------------------------------------------------
#: ``ANYUSE_DEMO_MODEL`` - which model makes a COMPILE's demonstration (``agent run --compile`` with the default Cerebras
#: policy): ``sonnet`` (default) - Sonnet through the Claude CLI (``claude -p``, the existing adapter
#: ``harness_policies.claude_cli_call``); ``cerebras`` - the Cerebras policy itself; ``cascade`` - Cerebras first, then
#: ONE Sonnet retry of a demonstration that failed for an agent-quality reason (once per site). Only the demonstration:
#: the proof replays stay at 0 model calls, the verifier's second stage and the MCP route's fallback are unchanged.
DEMO_SETTING = "ANYUSE_DEMO_MODEL"
DEMO_MODES = ("sonnet", "cerebras", "cascade")
CASCADE_MODEL = "sonnet"
#: a Sonnet demonstration's own cost cap by the CLI's count (inside ANYUSE_SPEND_DAILY_USD: every CLI call is reserved
#: and booked in the spend ledger when it is armed)
DEMO_SONNET_USD = 0.40
CASCADE_RETRY_USD = DEMO_SONNET_USD
CASCADE_FEATURE = "agent_harness_demo"
#: the cascade: the demonstration's own stops a stronger model could plausibly fix (agent quality)
CASCADE_RETRY_STOPS = frozenset({T.Stop.MAX_STEPS, T.Stop.UNREADABLE, T.Stop.POLICY_ERROR, T.Stop.NO_PROGRESS,
                                 T.Stop.CALL_CAP})
#: never retried: the site's walls, refusals and typed stops no model changes
CASCADE_NEVER_STOPS = frozenset({T.Stop.ROBOTS_SEARCH, T.Stop.REFUSED, T.Stop.BLOCKED, T.Stop.OFF_SITE_SEARCH,
                                 T.Stop.CAPTCHA, T.Stop.START_FAILED, T.Stop.POST_ONLY_SEARCH, T.Stop.GOV_HOST,
                                 T.Stop.OFF_SITE, getattr(T.Stop, "STATIC_RUNAWAY", "static_runaway"),
                                 T.Stop.NO_SITE_SEARCH})
#: a finished demonstration the compile could not use for an agent-quality reason (the compile's typed reasons)
CASCADE_RETRY_REASONS = ("not a valid demonstration", "ambiguous_reference", "search_links", "compile: titles_one_label",
                         "compile: no result region holds", "compile: the result structure",
                         "compile: no item was opened", "compile: the search cannot be replayed",
                         "compile: no search step", "compile: no search")


def provider_chosen() -> bool:
    """Has the user chosen the agent's model (ANYUSE_AGENT_MODEL_PROVIDER or ANYUSE_MODEL_PROVIDER, environment or
    settings file)? Then the ``model`` policy runs on it everywhere, including a compile's demonstration."""
    try:
        from anyuse.execution import model_adapter as M
        return bool(M._env("agent", "PROVIDER"))
    except Exception:  # noqa: BLE001 - unreadable: treat as not chosen (the documented default applies)
        return False


def agent_model_problem() -> str:
    """Why the ``model`` policy cannot run now, or "" when it can (``model_adapter.role_problem`` for the
    agent role): with nothing configured, the setup hint that names the no-key route first."""
    from anyuse.execution import model_adapter as M
    try:
        return M.role_problem("agent")
    except Exception as exc:  # noqa: BLE001 - an unreadable configuration is reported, not raised
        return f"the agent model configuration cannot be read: {type(exc).__name__}: {exc}"


def demo_model(explicit: Any = None) -> str:
    """``sonnet`` | ``cerebras`` | ``cascade`` (the setting; an unreadable value is ``cerebras``: no CLI call on a
    doubt)."""
    try:
        from anyuse import settings as S
        v = str(S.get(DEMO_SETTING, explicit if explicit is not None else S.UNSET) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return "cerebras"
    return v if v in DEMO_MODES else "cerebras"


def demo_retry_reason(out: Dict[str, Any]) -> str:
    """The cascade: why a compile run's demonstration is worth ONE retry by the stronger model ("" when it is not):
    its own agent-quality stop, or a finished demonstration the compile could not use for an agent-quality reason. A
    wall, a refusal, a typed site stop, a proof failure or a search that does not depend on its input is never
    retried."""
    outcome = str(out.get("outcome") or "")
    comp = out.get("compiled") or {}
    reasons = [str(r) for r in (comp.get("reasons") or [])]
    text = " ".join(reasons + [str(out.get("reason") or "")])
    # A new harness has a new commit allowance. Only read-only demonstrations
    # can safely start over; failed writes can have uncertain external effects.
    if out.get("commits") or any(effect != T.READ for effect in out.get("effects", [])):
        return ""
    if outcome in CASCADE_NEVER_STOPS or "not_query_dependent" in text or "static_runaway" in text:
        return ""
    if comp.get("status") == "ready":
        return ""
    if outcome in CASCADE_RETRY_STOPS:
        return f"the demonstration stopped: {outcome}"
    if outcome == T.Stop.DONE:
        for r in reasons:
            if r.startswith(CASCADE_RETRY_REASONS):
                return r[:200]
    return ""


def sonnet_demo_policy(runner=None):
    """A Sonnet demonstration's policy: the Claude CLI under ``DEMO_SONNET_USD`` (``cascade_call``)."""
    call = cascade_call(P.claude_cli_call(runner or claude_runner, model=CASCADE_MODEL, feature=CASCADE_FEATURE),
                        DEMO_SONNET_USD)
    return P.ModelPolicy(call, name=f"claude-cli:{CASCADE_MODEL}", system=P.system_prompt()), call


def cascade_call(inner: Callable[[str, str], Any], cap_usd: float = CASCADE_RETRY_USD,
                 model: str = CASCADE_MODEL) -> Callable[[str, str], Any]:
    """The retry's CLI ``call`` under its own cost cap: every call reserves the room left (``spend.reserve``, inside
    ANYUSE_SPEND_DAILY_USD when the ledger is armed) and books the CLI's reported cost (the room left when it reports
    none); a call is refused before it is made once the retry's cap is used."""
    from anyuse.execution import spend
    used = {"usd": 0.0}

    def call(system: str, user: str):
        left = float(cap_usd) - used["usd"]
        if left <= 0:
            return "", {"error": f"the demonstration retry's cost cap (US${cap_usd:.2f}) is used", "model": model}
        ticket, why = spend.reserve(left, CASCADE_FEATURE)
        if why:
            return "", {"error": why, "model": model}
        text, meta = inner(system, user)
        reported = meta.get("cost_usd")
        cost = float(reported) if isinstance(reported, (int, float)) else left
        used["usd"] += cost
        if ticket is not None:
            u = meta.get("usage") or {}
            spend.book(ticket, provider="claude-cli", model=model, prompt_tokens=int(u.get("prompt_tokens") or 0),
                       completion_tokens=int(u.get("completion_tokens") or 0), cost_usd=cost,
                       cost_basis="the CLI's reported total_cost_usd" if isinstance(reported, (int, float))
                       else "upper bound (the CLI reported no cost)")
        return text, meta
    call.used = used  # type: ignore[attr-defined]
    return call


# ---------------------------------------------------------------------------
# tool sets
# ---------------------------------------------------------------------------
#: the request ceiling of ONE run on its site, every session of the run together (the agent's own page and the
#: compile's proof replays): the traffic policy's per-site ceiling (docs/TRAFFIC_POLICY.md)
RUN_SITE_CAP = 200
#: the request ceiling of ONE compile run: the agent's run on an UNCACHED site plus the two
#: proof replays, every request type sent (documents, XHR/fetch, scripts, robots.txt; a static-cache hit is not sent).
#: Measured on 13 fresh-site agent runs (live): the agent's own pages used up to 39 requests, 64 script URLs
#: on one front page; the proof replays add documents and data (their scripts come from the run's cache).
COMPILE_SITE_CAP = 50
#: of a run's ceiling, the share kept for documents and data (``RequestBudget.static_reserve``): a page's scripts are
#: sent only while the site's total is below ceiling - reserve, so a script-heavy front page can never use up the
#: requests the search document and the proof replays need (www.ofen.de's 64 scripts did)
STATIC_RESERVE_FRAC = 0.4
#: (www.drako.it: the agent's run spent the static share; its scripts were never
#: stored, so the proof replay rendered without the scripts that fetch its list) each proof replay may send up to this
#: many same-origin scripts past the static share - counted, within ``COMPILE_SITE_CAP``
#: (``cdp_guard.REPLAY_DOC_RESERVE`` requests always left for its documents and data)
PROOF_SCRIPT_SHARE = 12
#: the robots.txt GET of a live run (None: ``api_permission``'s own GET, under its robots agent name and the
#: pre-send destination policy). A wrapper may gate it (a benchmark's contact guard).
ROBOTS_FETCHER: Optional[Callable[[str], Optional[str]]] = None
#: ``guarded_page_factory(web_scope=...)``'s default: the run's scope from the settings (``web_scope.WebScope.
#: from_settings``: the ANYUSE_THIRD_PARTY mode and the first-party settings)
SCOPE_FROM_SETTINGS = object()


def run_scope():
    """The run's web scope from the settings (None: first-party only): the ONE third-party policy of every guarded
    path (``web_scope.third_party_flags``)."""
    from anyuse.execution.web_scope import WebScope
    return WebScope.from_settings()


def guarded_page_factory(endpoint: str = "", *, start_url: str = "", robots=None, pacer=None,
                         site_cap: int = RUN_SITE_CAP, fresh_context: bool = True,
                         static_reserve: Optional[int] = None, web_scope: Any = SCOPE_FROM_SETTINGS
                         ) -> Callable[[], Any]:
    """A factory for the guarded container page of a live ``--site`` run: the learn loop's endpoint check
    (never a loopback, never a browser with pages open), the traffic guard with the honest identity,
    robots through ``allow_url``. Returns an async context manager factory; nothing is opened here.

    ONE request budget for the whole run: every session the factory makes
    - the agent's page and each proof replay of the compile - shares the pacer, the run's site ceiling and
    the run's static-asset cache; a page load is counted per main-frame navigation (an agent session
    navigates several times: with one 30-request load for the whole session, the second page of a run was
    refused). ``factory.budget`` is that budget."""
    from anyuse.execution import site_canon as SC
    from anyuse.execution.agent_browser import assert_container_endpoint, learn_endpoint
    from anyuse.execution.cdp_guard import (DOC_MIN_INTERVAL_S, PER_LOAD_CAP, REQUEST_MIN_INTERVAL_S, OriginPolicy,
                                            RequestBudget)
    from anyuse.execution.discover import Pacer
    ep = learn_endpoint(endpoint)
    if not ep:
        raise ValueError("no container browser endpoint: set ANYUSE_LEARN_ENDPOINT")
    assert_container_endpoint(ep)
    cap = int(site_cap)
    # the start host's www sibling is the same site (a www <-> apex redirect of the start
    # page is followed, never refused as a third party); nothing else is added
    host = (HT.urllib.parse.urlsplit(start_url).hostname or "") if start_url else ""
    sib = SC.sibling_of(host)
    policy = OriginPolicy({host: [sib]}) if host and sib else None
    reserve = int(cap * STATIC_RESERVE_FRAC) if static_reserve is None else int(static_reserve)
    # the third-party scope (ANYUSE_THIRD_PARTY: off / search / any) - the site's own search
    # provider by default; every host it allows counts in the site's budget, robots.txt of every host checked
    scope = run_scope() if web_scope is SCOPE_FROM_SETTINGS else web_scope
    # the static assets of the site itself, its platform asset CDN
    # and its granted provider load as in a normal browser visit (also CSS, fonts and images) - counted apart, logged,
    # run-cached, no ceiling but the runaway guard against real loops (cdp_guard.STATIC_RUNAWAY_REPEAT /
    # STATIC_RUNAWAY_DISTINCT per page load, a typed stop); the
    # run's ceiling (site_cap: COMPILE_SITE_CAP / RUN_SITE_CAP) and the per-load cap count documents, data calls and
    # robots.txt only. The static share (static_reserve) is left for the statics still counted (a third-party content
    # host under ANYUSE_THIRD_PARTY=any).
    budget = RequestBudget(pacer or Pacer(REQUEST_MIN_INTERVAL_S), per_load_cap=PER_LOAD_CAP, per_host_cap=cap,
                           site_cap=cap, load_per_navigation=True, origin_policy=policy,
                           nav_min_interval_s=DOC_MIN_INTERVAL_S,
                           static_reserve=reserve if 0 <= reserve < cap else None, web_scope=scope,
                           browser_statics=True,
                           # the page's own read-only POST search after typing
                           search_post=True)
    if robots is not None and hasattr(robots, "on_fetch"):
        # every robots.txt GET the run's gate makes (a granted host's, the www sibling's) counts in the site's total
        robots.on_fetch = budget.count_robots
    if robots is not None and isinstance(getattr(robots, "overrides", None), dict):
        # what the gate let through that robots.txt would refuse, on the run record
        budget.robots_overrides = robots.overrides
        budget.robots_override = str(getattr(robots, "override", "") or "")
    # the run's static-asset cache (scripts; documents and XHR/fetch data are never cached): a script the run
    # already fetched is not fetched again by the next page or replay (docs/TRAFFIC_POLICY.md, request economy)
    budget.cache_enabled = True
    # (www.drako.it: the proof replay's result page renders its list from its own
    # scripts; they had been fetched by the agent's run, the run cache refused them for their HTTP freshness, and the
    # static share then skipped them - the list never loaded) the RUN's site cache keeps a static asset the run fetched
    # although it says no-cache or gives no lifetime (never no-store, a cookie, a request body or a validation input),
    # so the proof replays render with the scripts the run already has (not sent again, not counted)
    budget.site_cache = True

    def factory():
        from anyuse.execution.cdp_guard import GuardedSession
        s = GuardedSession(ep, user_agent=HT.HONEST_UA, allow_url=robots, budget=budget)
        # open tasks: every session - the run's page, each proof replay, each replay - opens in a
        # NEW browser context (its own cookie jar, checked empty, disposed at the end): a login never carries over
        # from one task to another (a shared default context kept the previous run's session)
        s.fresh_context = bool(fresh_context)
        return s
    factory.budget = budget  # type: ignore[attr-defined]
    factory.scope = budget.web_scope  # type: ignore[attr-defined]
    return factory


def third_party_record(page_factory: Any, site: str) -> Dict[str, Any]:
    """The run record's third-party part (every third-party host on the run record): the
    mode, and the scope's grants, hosts used and refusals for the run's site."""
    from anyuse.execution.web_scope import third_party_mode
    try:
        mode = third_party_mode()
    except ValueError as exc:
        mode = f"invalid ({exc})"
    budget = getattr(page_factory, "budget", None)
    scope = getattr(budget, "web_scope", None)
    # the run cache's in-run reuse - each static answered from it instead of being sent again
    reuse: Dict[str, Dict[str, int]] = {}
    for h, _u, _t, kind in list(getattr(budget, "cache_reuse", None) or []):
        reuse.setdefault(kind, {})[h] = reuse.setdefault(kind, {}).get(h, 0) + 1
    cache = {"reused": reuse, "stored_on_response": int(getattr(budget, "cached_on_response", 0) or 0)}
    if scope is None:
        return {"mode": mode, "hosts": {}, "grants": {}, "refusals": [], "run_cache": cache}
    return dict(scope.site_report(site), mode=scope.mode or mode, run_cache=cache)


def traffic_record(page_factory: Any, site: str) -> Dict[str, Any]:
    """The run record's request counts: documents + data calls + robots.txt (what the run's
    ceiling counts) and the browser statics counted apart (by kind, the most of one page load, a runaway stop)."""
    budget = getattr(page_factory, "budget", None)
    if budget is None or not hasattr(budget, "traffic") or not site:
        return {}
    host = HT.urllib.parse.urlsplit(site if "://" in site else "https://" + site).hostname or site
    try:
        return dict(budget.traffic(host), ceiling=int(budget.site_cap))
    except Exception:  # noqa: BLE001 - a record never breaks a run
        return {}


def robots_gate(policy: Optional[str] = None, siblings: Sequence[str] = (), scope: Any = None
                ) -> Callable[[str], bool]:
    """``allow_url(url)`` from the cached robots verdicts, unless the global robots policy says ignore: allowed
    or disallowed as the cached file says; a host whose robots.txt was fetched and found ABSENT is allowed (no
    file is no prohibition); a host never fetched or unreadable stays refused (fail closed).
    ``siblings``: the start host's www sibling, whose robots.txt is fetched ONCE when a
    same-site redirect first lands there (through :data:`ROBOTS_FETCHER`), then gated like the start host. ``scope``
    (ANYUSE_THIRD_PARTY): a host the run's web scope allowed for the site (its search
    provider, a content host) gets the same lazy robots.txt check - fetched ONCE, RFC 9309 as everywhere - unless it is
    never contacted (``web_scope.contact_refusal``: refused before any request, its robots.txt included).

    A signed-in task the user started is not crawling (``api_permission.ROBOTS_SIGNED_IN``): once the run says it
    signs in on a site (``gate.sign_in(urls, how)``, the open-task browser with the user's credentials), the gate lets
    that site's own-origin requests through whatever its robots.txt says, and records each one robots.txt would refuse
    (``gate.overrides``). A gate built outside a user's call (``""``) or for breadth / background work (``auto:*``)
    never does."""
    from anyuse.execution.api_permission import (initiation, robots_allows, robots_allows_cached, robots_known_absent,
                                                 robots_override, robots_would, signed_in_override)
    from anyuse.execution.gov_hosts import rule_on
    from anyuse.execution.request_policy import origin
    from anyuse.execution.web_scope import contact_refusal
    # ANYUSE_ROBOTS and ANYUSE_REFUSE_GOV_HOSTS: read ONCE, when the run's gate is built - who started the run decides
    # (respect; user_initiated inside an MCP call / a CLI run; ignore). robots.txt is fetched and counted in every mode
    override = robots_override(policy)
    started = initiation()
    signed_in = signed_in_override(policy, started)
    gov_on = rule_on()
    #: origin -> how the run signs in there (``sign_in``)
    own: Dict[str, str] = {}
    overrides: Dict[str, Dict[str, Any]] = {}
    lazy = {str(h).lower() for h in siblings if h}
    tried: set = set()

    def fetched(url: str) -> str:
        # a robots.txt GET counts in the run's site total (the page factory sets on_fetch);
        # at the site's ceiling it is not made (the host stays unknown: refused, fail closed)
        hook = getattr(allow, "on_fetch", None)
        if hook is None:
            return ""
        sp = HT.urllib.parse.urlsplit(url)
        try:
            return str(hook(f"{sp.scheme or 'https'}://{sp.netloc}/robots.txt") or "")
        except Exception:  # noqa: BLE001 - a failed count never blocks the gate
            return ""

    def respect(url: str) -> bool:
        ok, _why = robots_allows_cached(url)
        host = (HT.urllib.parse.urlsplit(url).hostname or "").lower()
        if scope is not None and host not in lazy and scope.site_for(host):
            if contact_refusal(host, gov_on):
                return False
            if ok is None and host not in tried and not robots_known_absent(url):
                if fetched(url):
                    return False
                tried.add(host)
                ok, _why = robots_allows(url, fetcher=ROBOTS_FETCHER, on_hop=robots_hop_counter(allow, url))
            if ok is None:
                return robots_known_absent(url)
            return bool(ok)
        if ok is None and host in lazy and host not in tried and not robots_known_absent(url):
            if fetched(url):
                return False
            tried.add(host)
            ok, _why = robots_allows(url, fetcher=ROBOTS_FETCHER, on_hop=robots_hop_counter(allow, url))
            if ok is None:
                return robots_known_absent(url)
        if ok is None:
            return robots_known_absent(url)
        return bool(ok)

    def allow(url: str) -> bool:
        if respect(url):
            return True
        host = (HT.urllib.parse.urlsplit(url).hostname or "").lower()
        by = override or (signed_in if origin(url) in own else "")
        if not by or not host or contact_refusal(host, gov_on):
            return False                           # respect, or a never-contacted host (every mode)
        would, note = robots_would(url)
        rec = overrides.setdefault(host, {"requests": 0, "robots_would": would, "why": str(note)[:300],
                                          "overridden_by": by, "example": url[:200]})
        rec["requests"] += 1
        if by == signed_in and own.get(origin(url)):
            rec.setdefault("signed_in_by", own[origin(url)])
        return True

    def sign_in(urls: Sequence[str], how: str = "") -> str:
        """The run signs in on these URLs' origins (``how``: with what): in a task the user started their requests
        are no longer held to robots.txt. Returns the reason the gate records, ``""`` when nothing changes."""
        if not signed_in:
            return ""
        for u in urls or ():
            o = origin(str(u or ""))
            if o:
                own.setdefault(o, str(how or "signs in")[:120])
        return signed_in
    allow.on_fetch = None  # type: ignore[attr-defined]
    allow.override = override  # type: ignore[attr-defined]
    allow.overrides = overrides  # type: ignore[attr-defined]
    allow.sign_in = sign_in  # type: ignore[attr-defined]
    allow.signed_in = own  # type: ignore[attr-defined]
    allow.started = started  # type: ignore[attr-defined]
    allow.gov_on = gov_on  # type: ignore[attr-defined]
    return allow


def robots_hop_counter(gate: Any, url: str) -> Callable[[str], str]:
    """RFC 9309 section 2.3.1.2: each redirect hop of a robots.txt GET is one counted GET in
    the budget of the site that was asked (``gate.on_fetch``: the page factory's ``count_robots``); ``""`` to send the
    hop, else why not (the site's ceiling: the hop is not made - unreachable, complete disallow)."""
    asked = (HT.urllib.parse.urlsplit(url).hostname or "").lower()

    def hop(hop_url: str) -> str:
        hook = getattr(gate, "on_fetch", None)
        if hook is None:
            return ""
        try:
            return str(hook(hop_url, for_host=asked) or "")
        except TypeError:                                  # a hook without for_host (counts in the hop host's site)
            return str(hook(hop_url) or "")
        except Exception:  # noqa: BLE001 - a failed count never blocks the gate
            return ""
    return hop


def robots_start_refusal(start_url: str, gate: Callable[[str], bool], pacer=None, fetcher=None,
                         policy: Optional[str] = None) -> str:
    """The run's ONE robots.txt GET for its start host, before the page opens (the page's gate answers from
    the cache only, so without it a host the data dir had never compiled for would refuse every request of
    the run). Paced on the run's pacer. ``""`` when the start URL may be opened, else why not."""
    from anyuse.execution.api_permission import (robots_allows, robots_allows_cached, robots_known_absent,
                                                 robots_override, robots_would)
    # robots.txt is fetched and counted in every mode; an override is recorded on the gate
    ok, why = robots_allows_cached(start_url)
    if ok is None and not robots_known_absent(start_url):
        host = HT.urllib.parse.urlsplit(start_url).netloc
        if pacer is not None:
            pacer.wait(host)
        ok, why = robots_allows(start_url, fetcher=fetcher if fetcher is not None else ROBOTS_FETCHER,
                                on_hop=robots_hop_counter(gate, start_url))
    if gate(start_url):
        return ""
    ov = getattr(gate, "override", "") or robots_override(policy)
    if ov:
        from anyuse.execution.web_scope import contact_refusal
        host = (HT.urllib.parse.urlsplit(start_url).hostname or "").lower()
        if host and not contact_refusal(host, getattr(gate, "gov_on", None)):
            would, note = robots_would(start_url)
            box = getattr(gate, "overrides", None)
            if isinstance(box, dict):
                rec = box.setdefault(host, {"requests": 0, "robots_would": would, "why": str(note)[:300],
                                            "overridden_by": ov, "example": start_url[:200]})
                rec["requests"] += 1
            return ""
    if ok is False and str(why).startswith(("robots_unreachable", "robots_rate_limited")):
        # RFC 9309: an unreachable robots.txt (5xx, a network failure) or a 429 that stays
        # is a complete disallow for this run
        return f"robots.txt of the start host is unreachable ({why}); nothing is opened"
    if ok is False:
        return f"robots.txt disallows the start URL ({why}); nothing is opened"
    return f"robots.txt of the start host is unknown ({why}); nothing is opened (fail closed)"


class _LivePage:
    """One guarded session kept on ONE event loop for the whole run (the websockets must not change loops)."""

    def __init__(self, factory):
        import asyncio
        self.loop = asyncio.new_event_loop()
        self.session = factory()
        self.loop.run_until_complete(self.session.__aenter__())

    def close(self) -> None:
        try:
            self.loop.run_until_complete(self.session.__aexit__(None, None, None))
        finally:
            self.loop.close()


def make_toolbox(*, site: str = "", app: str = "", page_factory=None, guest=None, workspace: Optional[str] = None,
                 surface: str = "agent harness",
                 run_id: str = "", sandbox=None, registry=None, robots=None, values: Optional[Dict[str, str]] = None,
                 secrets=None, site_cap: Optional[int] = None, rea_store=None) -> Dict[str, Any]:
    """``{"toolbox", "close", "refusal"}``: the tool sets of one run, or a typed refusal (no world). The browser is
    the open-task browser (``web_flow.FlowBrowserTools``: forms, logins with secrets by reference, commit steps under
    the harness's commit policy); ``values`` carry the credentials by reference, ``secrets`` resolves them (the
    Keychain by default)."""
    from anyuse.execution import harness_sandbox_runner as SR
    from anyuse.execution import harness_toolwrite as TW
    from anyuse.execution.rea_catalog import CatalogStore
    from anyuse.execution.harness_rea import ReaTools
    rea_store = rea_store if rea_store is not None else CatalogStore()
    try:
        rea = ReaTools(rea_store) if rea_store.list_tools() else None
    except (ValueError, OSError, RuntimeError) as exc:
        return {"toolbox": None, "close": lambda: None, "refusal": {"kind": "rea_catalog", "reason": str(exc)}}
    ws = workspace or H.workspace_dir(run_id or "run")
    files = HT.FileTools(ws)
    sb = sandbox or TW.ContainerSandbox(runner=SR.container_runner())
    tools = HT.RuntimeTools(registry or TW.ToolRegistry(), sb, files=files)
    closers: List[Callable[[], None]] = []
    browser = desktop = None
    if site:
        start = site if site.startswith("http") else "https://" + site
        host = HT.urllib.parse.urlsplit(start).hostname or ""
        why = HT.gov_refusal(start)
        if why:
            return {"toolbox": None, "close": lambda: None, "refusal": {"kind": "gov_host", "reason": why}}
        browser_gate = robots
        from anyuse.execution import site_canon as SC
        if page_factory is None:
            from anyuse.execution.cdp_guard import REQUEST_MIN_INTERVAL_S
            from anyuse.execution.discover import Pacer
            pacer = Pacer(REQUEST_MIN_INTERVAL_S)
            try:
                scope = run_scope()
            except ValueError as exc:
                return {"toolbox": None, "close": lambda: None, "refusal": {"kind": "settings", "reason": str(exc)}}
            gate = browser_gate = robots or robots_gate(siblings=SC.sibling_hosts(host)[1:], scope=scope)
            # the endpoint check first (nothing is fetched for a run that has no container browser)
            try:
                page_factory = guarded_page_factory(start_url=start, robots=gate, pacer=pacer, web_scope=scope,
                                                    **({"site_cap": int(site_cap)} if site_cap else {}))
            except OSError as exc:
                # an unreachable container browser is a typed refusal, not a crash (not a URLError
                # traceback); nothing was sent to the site
                return {"toolbox": None, "close": lambda: None,
                        "refusal": {"kind": "browser_unreachable",
                                    "reason": f"the container browser did not answer ({type(exc).__name__}: "
                                              f"{str(exc)[:160]}); nothing was opened"}}
            if robots is None:
                # a run that signs in with the user's credentials is a signed-in task (robots_gate)
                from anyuse.execution.web_flow import note_sign_in
                note_sign_in(gate, start, SC.sibling_origins(start)[1:], values)
                why = robots_start_refusal(start, gate, pacer=pacer)
                if why:
                    return {"toolbox": None, "close": lambda: None, "refusal": {"kind": "robots", "reason": why}}
        live = None
        page = page_factory()
        if hasattr(page, "__aenter__"):
            live = _LivePage(lambda: page)
            closers.append(live.close)
            page = live.session
        # the browser's own gate asks the same robots verdicts before a navigation (a refused URL is a typed
        # "blocked" outcome for the policy, not a page the guard failed underneath it)
        from anyuse.execution import secret_refs as SR
        from anyuse.execution.web_flow import FlowBrowserTools
        # the ONE settle rule after every action and every proof replay's navigation
        # (typed_readiness.PageSettle: DOM and XHR / fetch quiet, bounded; each wait recorded in its trail)
        from anyuse.execution import typed_readiness as TR
        browser = FlowBrowserTools(page, start, host, allow_url=browser_gate, loop=live.loop if live else None,
                                   values=values, secrets=secrets or SR.SecretStore(SR.product_runner),
                                   related_origins=SC.sibling_origins(start)[1:], settle=TR.PageSettle())
    if app:
        from anyuse.execution import desktop_tools as DTools
        runner = guest or DTools.configured_runner()
        if runner is None:
            return {"toolbox": None, "close": lambda: None,
                    "refusal": dict(DTools.host_refusal(f"agent run --app {app}", surface=surface),
                                    kind="foreground_interlock")}
        bundle = app if "." in app else ""
        desktop = HT.DesktopTools(lambda op, **a: runner(op, **dict(a, app="" if bundle else app, bundle=bundle))
                                  if op == "ax_elements" else runner(op, **a), app=app, bundle=bundle)
    tb = HT.Toolbox(browser=browser, desktop=desktop, files=files, tools=tools, rea=rea)

    def close():
        for c in closers:
            try:
                c()
            except Exception:  # noqa: BLE001
                pass
    # the page factory the run's browser came from: the compile's proof replays open their pages from it (the
    # CLI passes no factory; without this every live replay answers "no page factory" and no web tool proves)
    return {"toolbox": tb, "close": close, "refusal": None, "workspace": ws, "page_factory": page_factory if site else None,
            "robots": browser_gate if site else None}


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------
def run_agent_task(task: str, *, values: Optional[Dict[str, str]] = None, site: str = "", app: str = "",
                   policy: str = DEFAULT_POLICY, script: Optional[Sequence[Any]] = None,
                   max_steps: Optional[int] = None, compile: bool = False, fresh: Sequence[str] = (),
                   storage=None, approver=None, toolbox=None, policy_obj=None, runner=None, transport=None,
                   page_factory=None, guest=None, workspace: Optional[str] = None, sandbox=None, registry=None,
                   name: str = "", surface: str = "agent harness", asker=None, judge: Any = "auto",
                   fresh_sets: Optional[Sequence[Dict[str, str]]] = None, secrets=None,
                   keep_trace: bool = False, commits_need_human: bool = False,
                   keep_evidence: bool = False, rea_store=None) -> Dict[str, Any]:
    """The report every entry point returns: ``ok``, ``outcome`` (the stop), ``reason``, ``steps``,
    ``model_calls``, ``cost_usd``, ``result``, ``compiled`` (``{"name", "status", "reasons"}`` or None).
    ``keep_evidence``: a finished run on a site also returns ``evidence``, the page it ended on
    (:func:`_page_evidence`), read before the browser closes.

    Open tasks: ``asker(Question) -> Answer`` is the caller's channel for a commit step the task
    does not cover and for a one-time code (the CLI's terminal, the MCP bridge); ``judge`` ("auto": the Cerebras
    coverage judge with the Cerebras policy, else none) is asked only for an uncertain coverage rule result."""
    from anyuse.execution import harness_compile as HC
    t0 = time.monotonic()
    overrides = {"max_steps": int(max_steps)} if max_steps else {}
    if commits_need_human:
        overrides["commits_need_human"] = True
    cfg = H.HarnessConfig.from_settings(**overrides)
    run_id = H.Harness.__name__ and __import__("uuid").uuid4().hex[:12]
    # a COMPILE's demonstration is made by ANYUSE_DEMO_MODEL (default Sonnet via the
    # Claude CLI) when the run asks for the default Cerebras policy
    # the default policy on a provider the user chose runs the demonstration on that provider (plug and play)
    pname = str(policy or "").lower()
    demo_mode = demo_model() if (compile and site and policy_obj is None and pname in MODEL_POLICIES and
                                 not (pname == "model" and provider_chosen())) else ""
    no_model = agent_model_problem() if (policy_obj is None and transport is None and pname in MODEL_POLICIES
                                         and demo_mode in ("", "cerebras")) else ""
    if no_model:
        # refused before a browser, a guest or a model is touched: the person learns the no-key route
        return {"ok": False, "outcome": T.Stop.REFUSED, "reason": no_model,
                "refusal": {"kind": "no_model", "reason": no_model}, "steps": 0, "decisions": 0,
                "model_calls": 0, "cost_usd": 0.0, "result": None, "compiled": None, "run_id": run_id,
                "policy": policy, "seconds": round(time.monotonic() - t0, 3)}
    made = None
    from anyuse.execution import secret_refs as SR
    secrets = secrets or SR.SecretStore(SR.product_runner)
    if toolbox is None:
        made = make_toolbox(site=site, app=app, page_factory=page_factory, guest=guest, workspace=workspace,
                            run_id=run_id, sandbox=sandbox, registry=registry, surface=surface, values=values,
                            secrets=secrets, site_cap=COMPILE_SITE_CAP if compile else None, rea_store=rea_store)
        if made["refusal"]:
            r = made["refusal"]
            return {"ok": False, "outcome": T.Stop.REFUSED, "reason": r.get("reason") or r.get("error") or "refused",
                    "refusal": r, "steps": 0, "decisions": 0, "model_calls": 0, "cost_usd": 0.0, "result": None,
                    "compiled": None, "run_id": run_id, "seconds": round(time.monotonic() - t0, 3)}
        toolbox = made["toolbox"]
    try:
        demo_call = None
        if demo_mode == "sonnet":
            import dataclasses
            pol, demo_call = sonnet_demo_policy(runner)
            cfg = dataclasses.replace(cfg, max_usd=DEMO_SONNET_USD)
        else:
            pol = policy_obj or make_policy(policy, runner=runner, transport=transport, script=script,
                                            max_usd=cfg.max_usd)
        if judge == "auto":
            judge = None
            if policy_obj is None and str(policy or "").lower() in MODEL_POLICIES and demo_mode != "sonnet":
                from anyuse.execution.commit_policy import cerebras_judge
                from anyuse.execution.model_adapter import RunBudget
                judge = cerebras_judge(transport=transport, budget=RunBudget(cfg.max_usd))
        trace = H.Harness(toolbox, pol, cfg, approver=approver, run_id=run_id, asker=asker, judge=judge,
                          workspace=(made or {}).get("workspace") or workspace or "").run(task, values)
        run_traces = [trace]
        out = H.report(trace)
        out.update(ok=trace.ok, outcome=trace.stop, reason=trace.stop_reason, compiled=None, policy=policy,
                   config={"max_steps": cfg.max_steps, "unasked_irreversible": cfg.unasked_irreversible,
                           "max_usd": cfg.max_usd, "commits_need_human": cfg.commits_need_human})
        if site:
            # every third-party host on the run record (the compile's proof replays share the
            # run's budget, so their hosts are here too - this is filled in again after the compile)
            out["third_party"] = third_party_record(page_factory or (made or {}).get("page_factory"),
                                                    getattr(trace, "site", "") or site)
        if keep_trace:
            out["trace"] = trace                     # a bench keeps the redacted trace; never printed by the CLI
        demonstrated_by = str(getattr(pol, "name", "") or policy or "")
        try:
            trace.demonstrated_by = demonstrated_by  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        if compile and any(s.action is not None and s.action.kind == T.RunReaAction.kind for s in trace.steps):
            out["compiled"] = {"name": None, "status": "draft", "reasons": [
                "registered REA executions retain their own catalog proof; this trace cannot become a UI recording"]}
        elif compile and trace.ok:
            out["compiled"] = _compile(trace, storage, fresh, toolbox,
                                       page_factory or (made or {}).get("page_factory"), name,
                                       fresh_sets=fresh_sets, robots=(made or {}).get("robots"), secrets=secrets,
                                       asker=asker)
        elif compile:
            out["compiled"] = {"name": None, "status": "draft",
                               "reasons": [f"the run did not finish ({trace.stop}); nothing compiled"]}
        out["demonstrated_by"] = demonstrated_by
        if demo_call is not None:
            out["demo_cost_usd"] = round(float(demo_call.used["usd"]), 6)
        # ANYUSE_DEMO_MODEL=cascade: ONE retry by Sonnet (claude -p) of a Cerebras demonstration that failed for an
        # agent-quality reason, once per site, on the run's own guard stack and budget
        why_retry = demo_retry_reason(out) if demo_mode == "cascade" and not out.get("rea_executions") else ""
        if why_retry:
            out = _cascade_retry(out, why_retry, task, values, site=site, storage=storage, fresh=fresh, name=name,
                                 fresh_sets=fresh_sets, secrets=secrets, asker=asker, approver=approver, cfg=cfg,
                                 runner=runner, run_id=run_id, made=made, page_factory=page_factory,
                                 workspace=workspace, surface=surface, keep_trace=keep_trace, run_traces=run_traces)
        if site:
            out["third_party"] = third_party_record(page_factory or (made or {}).get("page_factory"),
                                                    getattr(trace, "site", "") or site)
            out["traffic"] = traffic_record(page_factory or (made or {}).get("page_factory"),
                                            getattr(trace, "site", "") or site)
        if keep_evidence and site and out.get("ok") and not out.get("cascade"):
            evidence = _page_evidence(toolbox)
            if evidence is not None:
                out["evidence"] = evidence
        # A completed UI step is evidence of fallback use; refused proposals and
        # file/HTTP-only tasks do not count. A cascade is still one public run.
        from anyuse.execution.learning import ReaFallbackTracker
        used_computer_use = any(s.action is not None and s.action.toolset in {"browser", "desktop"}
                                and s.outcome.ok for t in run_traces for s in t.steps)
        escalation = ReaFallbackTracker(storage).record_run(
            task, used_computer_use=used_computer_use, rea_executions=out.get("rea_executions") or ())
        if escalation is not None:
            out["rea_escalation"] = escalation
        out["seconds"] = round(time.monotonic() - t0, 3)
        return out
    finally:
        if made is not None:
            made["close"]()


def _page_evidence(toolbox) -> Optional[Dict[str, Any]]:
    """The page the run ended on, as recompile evidence: ``final_url``, ``dom``, ``text`` and the
    last GET responses the browser made (``cdp_replay.traffic_from_events`` with ``newest``:
    credential-free, redacted, at most ``cdp_replay.MAX_CAPTURED_BODIES``). None when the
    browser cannot be read; reading it never fails the run."""
    sets = getattr(toolbox, "sets", None)
    browser = sets.get("browser") if isinstance(sets, dict) else None
    page = getattr(browser, "page", None)
    events = getattr(page, "_events", None)
    if page is None or events is None or not all(hasattr(page, n) for n in ("dom", "text", "url", "call")):
        return None
    from anyuse.execution import cdp_replay as CR

    async def read() -> Dict[str, Any]:
        url = await page.url()
        dom = await page.dom()
        text = await page.text()
        network = await CR.read_bodies(CR.traffic_from_events(list(events), newest=True),
                                       page.call, 5.0)
        return {"final_url": str(url or ""), "dom": str(dom or ""), "text": str(text or ""),
                "network": network}
    try:
        return HT.run_sync(read(), getattr(browser, "loop", None))
    except Exception:  # noqa: BLE001 - no evidence, never a failed run
        return None


def _cascade_retry(first: Dict[str, Any], why: str, task: str, values: Optional[Dict[str, str]], *, site: str,
                   storage, fresh, name: str, fresh_sets, secrets, asker, approver, cfg, runner, run_id: str, made,
                   page_factory, workspace, surface: str, keep_trace: bool,
                   run_traces: Optional[List[T.Trace]] = None) -> Dict[str, Any]:
    """The cascade's ONE retry: Sonnet through the Claude CLI demonstrates again on a NEW page of the run's own page
    factory (the same budget, pacer, robots gate, web scope and site ceiling: the same guard stack and traffic rules),
    under ``CASCADE_RETRY_USD``; its compile proves as every compile does (replays at 0 model calls). The report is the
    retry's, with the first demonstration kept beside it (``cascade``) and both runs' calls and cost summed."""
    import dataclasses
    factory = page_factory or (made or {}).get("page_factory")
    gate = (made or {}).get("robots")
    first_view = {k: first.get(k) for k in ("outcome", "reason", "steps", "model_calls", "cost_usd", "compiled",
                                            "demonstrated_by")}
    record = {"retry_reason": why, "first": first_view, "model": f"claude-cli:{CASCADE_MODEL}",
              "cap_usd": CASCADE_RETRY_USD}
    if factory is None:
        return dict(first, cascade=dict(record, retried=False, why_not="no page factory for a second page"))
    made2 = make_toolbox(site=site, page_factory=factory, robots=gate, run_id=run_id + "-cascade", surface=surface,
                         values=values, secrets=secrets, workspace=workspace)
    if made2.get("refusal"):
        return dict(first, cascade=dict(record, retried=False, why_not=str(made2["refusal"].get("reason") or "")[:200]))
    pol, call = sonnet_demo_policy(runner)
    cfg2 = dataclasses.replace(cfg, max_usd=CASCADE_RETRY_USD)
    try:
        trace = H.Harness(made2["toolbox"], pol, cfg2, approver=approver, run_id=run_id + "-cascade", asker=asker,
                          judge=None, workspace=made2.get("workspace") or workspace or "").run(task, values)
        if run_traces is not None:
            run_traces.append(trace)
        out = H.report(trace)
        out.update(ok=trace.ok, outcome=trace.stop, reason=trace.stop_reason, compiled=None, policy="cerebras",
                   config={"max_steps": cfg2.max_steps, "unasked_irreversible": cfg2.unasked_irreversible,
                           "max_usd": cfg2.max_usd, "commits_need_human": cfg2.commits_need_human})
        try:
            trace.demonstrated_by = pol.name  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        if keep_trace:
            out["trace"] = trace
        if trace.ok:
            out["compiled"] = _compile(trace, storage, fresh, made2["toolbox"], factory, name, fresh_sets=fresh_sets,
                                       robots=gate, secrets=secrets, asker=asker)
        else:
            out["compiled"] = {"name": None, "status": "draft",
                               "reasons": [f"the run did not finish ({trace.stop}); nothing compiled"]}
    finally:
        made2["close"]()
    out["demonstrated_by"] = pol.name
    out["model_calls"] = int(first.get("model_calls") or 0) + int(out.get("model_calls") or 0)
    out["cost_usd"] = round(float(first.get("cost_usd") or 0.0) + float(out.get("cost_usd") or 0.0), 6)
    out["cascade"] = dict(record, retried=True, retry_cost_usd=round(float(call.used["usd"]), 6),
                          retry_calls=int(trace.model_calls or 0))
    for k in ("third_party", "traffic", "seconds"):
        if k in first and k not in out:
            out[k] = first[k]
    return out


def flow_toolbox_factory(page_factory, start_url: str, site: str, *, robots=None, secrets=None):
    """``make(values) -> Toolbox`` for a flow tool's proof and replays: a fresh page of the run's factory (one guarded
    session each, the run's budget), the open-task browser with the replay's values; ``toolbox.close()``."""
    from anyuse.execution import secret_refs as SR
    from anyuse.execution.web_flow import FlowBrowserTools
    store = secrets or SR.SecretStore(SR.product_runner)

    def make(values: Dict[str, str]):
        if page_factory is None:
            raise ValueError("no page factory for a flow replay")
        page = page_factory()
        live = None
        if hasattr(page, "__aenter__"):
            live = _LivePage(lambda: page)
            page = live.session
        from anyuse.execution import site_canon as SC
        fb = FlowBrowserTools(page, start_url, site, allow_url=robots, loop=live.loop if live else None,
                              values=values, secrets=store, related_origins=SC.sibling_origins(start_url)[1:])
        tb = HT.Toolbox(browser=fb)
        tb.close = (live.close if live else (lambda: None))  # type: ignore[attr-defined]
        return tb
    return make


def fresh_value_sets(trace: T.Trace, fresh: Sequence[Any], fresh_sets: Optional[Sequence[Dict[str, str]]]
                     ) -> List[Dict[str, str]]:
    """The proof's value sets: the caller's sets, else each ``--fresh`` value for the run's first value that is not a
    credential."""
    from anyuse.execution.web_flow_compile import CREDENTIAL_NAMES
    if fresh_sets:
        return [dict(s) for s in fresh_sets]
    if fresh and all(isinstance(v, dict) for v in fresh):
        return [dict(v) for v in fresh]
    name = next((k for k in trace.values if k.lower() not in CREDENTIAL_NAMES), "")
    return [{name: v} for v in fresh] if name else []


def _compile(trace: T.Trace, storage, fresh: Sequence[str], toolbox, page_factory, name: str, *,
             fresh_sets: Optional[Sequence[Dict[str, str]]] = None, robots=None, secrets=None, asker=None
             ) -> Dict[str, Any]:
    from anyuse.execution import harness_compile as HC
    try:
        if trace.kind == "web":
            from anyuse.execution import web_flow_compile as WFC
            if WFC.is_flow_trace(trace):
                sets = fresh_value_sets(trace, fresh, fresh_sets)
                if len(sets) < WFC.MIN_FRESH_CASES:
                    return {"name": None, "status": "draft",
                            "reasons": [f"a flow proof needs >= {WFC.MIN_FRESH_CASES} fresh value sets "
                                        "(--fresh-set NAME=VALUE,...)"]}
                make = flow_toolbox_factory(page_factory, trace.start_url, trace.site, robots=robots, secrets=secrets)
                rep = WFC.compile_and_save_flow(trace, storage, sets, make, name=name, asker=asker)
                return {"name": rep.get("name"), "status": rep.get("status"), "reasons": list(rep.get("reasons") or []),
                        "model_calls": rep.get("model_calls", 0), "kind": "web_flow",
                        "proof": rep.get("proof")}
        if trace.kind == "desktop":
            sets = fresh_value_sets(trace, fresh, fresh_sets)
            rep = HC.compile_success(trace, storage, **({"name": name} if name else {}),
                                     **({"fresh": sets} if sets else {}))
            return {"name": rep.get("workflow") or name or None, "status": rep.get("status"),
                    "reasons": list(rep.get("reasons") or []), "model_calls": rep.get("model_calls", 0),
                    "proof": rep.get("proof")}
        if trace.kind == "web":
            # the proof's fresh values: the caller's --fresh values first, then the site's own vocabulary
            # (harness_compile.proof_values)
            browser = toolbox.sets.get("browser")

            def replay(tool, _site, value):
                from anyuse.execution import demo_tool as DT
                from anyuse.execution.model_adapter import calls_since, process_call_count
                mark = process_call_count()
                page = page_factory() if page_factory is not None else None
                if page is None:
                    return {"status": "failed", "why": "no page factory for a live replay", "model_calls": 0}
                from anyuse.execution import harness_compile as HC

                async def run_and_keep(pg):
                    # the page the replay ended on is kept (the independent judge reads it; a refused
                    # value's page can be judged later)
                    budget = getattr(page_factory, "budget", None)
                    if budget is not None and hasattr(budget, "open_replay_share"):
                        # the proof replay renders with the page's own scripts (www.drako.it) - its own share of
                        # same-origin scripts past the static share, counted, within the run's ceiling
                        budget.open_replay_share(PROOF_SCRIPT_SHARE)
                    try:
                        got = await DT.run_tool(pg, tool, value, browser.settle,
                                                **({} if hasattr(page, "__aenter__") else {"result_wait_s": 0.0}))
                    finally:
                        if budget is not None and hasattr(budget, "close_replay_share"):
                            budget.close_replay_share()
                    got["final_page"] = await HC.final_page_async(pg)
                    if budget is not None and getattr(budget, "replay_scripts_sent", None):
                        got["replay_scripts_sent"] = budget.replay_scripts_sent[-1]
                    if budget is not None and getattr(budget, "runaway", None):
                        # the browser statics' runaway guard stopped the site - a typed failure
                        got = dict(got, status="failed",
                                   why=f"static_runaway: {next(iter(budget.runaway.values()))}"[:300])
                    return got
                if hasattr(page, "__aenter__"):
                    live = _LivePage(lambda: page)
                    try:
                        out = live.loop.run_until_complete(run_and_keep(live.session))
                    finally:
                        live.close()
                else:
                    out = HT.run_sync(run_and_keep(page))
                out["model_calls"] = calls_since(mark)
                return out
            wf = HC.workflow_of(trace)
            rep = HC.compile_and_save_web(trace, storage, None, list(fresh), replay=replay, workflow=wf,
                                          **({"name": name} if name else {}))
            reasons = list(rep.get("reasons") or [])
            from anyuse.execution import demo_compile as C
            if any(C.NO_RESULTS_UNRENDERED in str(r) for r in reasons):
                # an unrendered result page names what the guard refused that the page needed (the site's
                # own hosts, robots.txt) - the user's settings, not the agent, decide those
                ev = HT.refusal_evidence(getattr(browser, "page", None), trace.start_url)
                reasons = [f"{r} ({ev})" if ev and C.NO_RESULTS_UNRENDERED in str(r) else r for r in reasons]
            return {"name": rep.get("name"), "status": rep.get("status"), "reasons": reasons,
                    "model_calls": rep.get("model_calls", 0), "workflow": wf}
        return {"name": None, "status": "draft", "reasons": [f"no compile seam for a {trace.kind} run"]}
    except Exception as exc:  # noqa: BLE001 - a compile error is reported, never a crash of the run
        return {"name": None, "status": "draft", "reasons": [f"compile failed: {type(exc).__name__}: {exc}"[:300]]}


def replay_flow_tool(name: str, values: Optional[Dict[str, str]] = None, *, storage=None, page_factory=None,
                     asker=None, robots=None, secrets=None, allow_draft: bool = False) -> Dict[str, Any]:
    """Replay a saved flow tool with new values (``anyuse agent replay``): a replay started with values is covered by
    initiation and asks nothing but a one-time code; a deviation stops; 0 model calls."""
    from anyuse.execution import web_flow_compile as WFC
    t0 = time.monotonic()
    rec = WFC.load_flow_tool(storage, name)
    if rec is None:
        return {"ok": False, "outcome": T.Stop.REFUSED, "reason": f"no flow tool named {name!r}", "model_calls": 0}
    if rec.get("status") != "ready" and not allow_draft:
        return {"ok": False, "outcome": T.Stop.REFUSED, "reason": f"the flow tool {name!r} is not ready: "
                + "; ".join(rec.get("status_reasons") or []), "model_calls": 0}
    tool = rec["tool"]
    start = tool.get("start_url") or ""
    why = HT.gov_refusal(start)
    if why:
        return {"ok": False, "outcome": T.Stop.GOV_HOST, "reason": why, "model_calls": 0}
    if page_factory is None:
        from anyuse.execution import site_canon as SC
        from anyuse.execution.discover import Pacer
        scope = run_scope()
        robots = robots or robots_gate(siblings=SC.sibling_hosts(HT.urllib.parse.urlsplit(start).hostname or "")[1:],
                                       scope=scope)
        from anyuse.execution.cdp_guard import REQUEST_MIN_INTERVAL_S
        page_factory = guarded_page_factory(start_url=start, robots=robots, pacer=Pacer(REQUEST_MIN_INTERVAL_S),
                                            web_scope=scope)
    vals = WFC.replay_values(tool, values)
    host = HT.urllib.parse.urlsplit(start).hostname or ""
    tb = flow_toolbox_factory(page_factory, start, host, robots=robots, secrets=secrets)(vals)
    try:
        out = WFC.replay_flow(tool, vals, tb, asker=asker)
    finally:
        tb.close()
    out.pop("trace", None)
    out.update(name=name, seconds=round(time.monotonic() - t0, 3))
    return out


def terminal_asker():
    """The CLI's asker when a person is at a terminal (stdin and stdout both a TTY), else None: no one to ask."""
    import sys
    return cli_asker if (sys.stdin.isatty() and sys.stdout.isatty()) else None


def cli_asker(q) -> Any:
    """The CLI's question (open tasks): a person answers at the terminal; no terminal means no answer (stop)."""
    import sys
    import click
    from anyuse.execution import commit_policy as CP
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return CP.Answer()
    d = q.to_dict()
    if q.kind == CP.OTP:
        code = click.prompt(f"The site asks for a one-time code ({d.get('prompt') or 'code'})", hide_input=True,
                            default="", show_default=False)
        return CP.Answer(code=str(code).strip())
    click.echo(f"The task did not cover this step: {d.get('why')}")
    click.echo(f"  action: {json.dumps(d.get('action'), ensure_ascii=False)[:200]}")
    click.echo(f"  target: {json.dumps(d.get('target'), ensure_ascii=False)[:200]}")
    if d.get("values"):
        click.echo(f"  values: {json.dumps(d.get('values'), ensure_ascii=False)[:400]}")
    if d.get("amount"):
        click.echo(f"  amount: {json.dumps(d.get('amount'), ensure_ascii=False)}")
    return CP.Answer(confirmed=bool(click.confirm("Allow this step?", default=False)))


def cli_approver(ctx: Dict[str, Any]) -> bool:
    """The CLI's hold (``ANYUSE_AGENT_UNASKED_IRREVERSIBLE = hold``): a person answers at the terminal."""
    import click
    a = ctx.get("action") or {}
    return bool(click.confirm(f"The agent wants an {ctx.get('effect')} action the task did not ask for: "
                              f"{json.dumps(a, ensure_ascii=False)[:160]} on {ctx.get('target')!r}. Allow it?",
                              default=False))
