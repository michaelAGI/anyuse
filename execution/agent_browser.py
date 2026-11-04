"""A step agent that drives a container browser — and is RECORDED while it does.

Without it, a workflow exists only if a human (or the bench) recorded it, so
**the ladder's success rate on an unrecorded task is exactly zero.**
The agent, meanwhile, succeeds about 42 % of the time on a site it has never
seen, in about nine seconds, for a fifth of a cent — and leaves nothing behind.

This module closes that loop. The agent is a minimal per-step loop (it is a
floor, not a product), with one thing
added that changes what it is for: **the same recorder that watches a human
watches the agent.** Network, DOM and the agent's own typed inputs come back as
a recording in exactly the format the measurement harness produces, so the
normal compile → fresh-input proof → persist path runs over it unchanged.

What is deliberately NOT here
-----------------------------
* **No desktop.** This drives a browser over CDP in a container and nothing
  else. :func:`assert_container_endpoint` refuses a loopback endpoint and a
  non-headless browser before a single action is taken, because an endpoint on
  ``127.0.0.1`` is very likely the user's own Chrome with their own sessions in
  it, and clicking and typing there is desktop actuation however it is dressed.
  The check is a *heuristic*, and the docstring says so — it is a guard rail,
  not a proof, which is why this is a CLI command and not an MCP tool.
* **No writes.** The system prompt forbids logging in and submitting anything
  that changes data, and the action vocabulary has no navigate-to-arbitrary-URL
  verb. That is a prompt-level restriction on a model, i.e. the weakest kind,
  and it is stated as such rather than claimed as a guarantee. The container is
  the actual boundary: no credentials are in it and nothing it does reaches the
  user's machine.
* **No trust in the agent's answer.** Nothing in this module decides whether the
  agent was right. It returns what the page showed; ``learn_loop`` decides, and
  it decides with the same cross-value ground truth and the same chrome filter a
  human recording is held to.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution.model_adapter import (
    ModelAdapter,
    RunBudget,
    credential_status,
    model_env_file_path,
    read_model_api_key,
    resolve_model_config,
)

logger = logging.getLogger(__name__)

#: The endpoint the agent drives. Separate from ``ANYUSE_CDP_ENDPOINT`` on
#: purpose: the replay rung only navigates, this one clicks and types, and the
#: two should not be switched on by the same variable.
ENDPOINT_ENV = "ANYUSE_LEARN_ENDPOINT"
MAX_STEPS = 10
MIN_INTERVAL_S = 1.0

#: ASCII only, and checked to be: an HTTP header cannot carry anything else,
#: and a non-ASCII character here fails every request the agent makes with a
#: UnicodeEncodeError that looks like a model outage. (It did exactly that once.)
USER_AGENT = ("anyuse-learn/0.1 (research prototype; read-only exploration; "
              "<=1 req/s)")
assert USER_AGENT.isascii(), "the User-Agent must be ASCII"

SYSTEM = """You drive a web browser one step at a time to complete a read-only task.
You are given the interactive elements on the current page and the visible text.
Reply with JSON only, exactly one action:

{"action":"type","index":<element index>,"text":"..."}     type into an input
{"action":"click","index":<element index>}                 click an element
{"action":"submit","index":<element index>}                press Enter in an input
{"action":"read"}                                          re-read the page
{"action":"done"}                                          the task is complete

Rules: one action per reply, no prose. Prefer the site's own search box. After
submitting a search, wait by issuing "read" once, then "done" when the page the
task asks for is visible. Never log in, never submit anything that changes data.
Give up with "done" rather than repeating a failed action."""

#: The same prompt plus a TYPED abstention (trigger (b) of the escalation
#: loop). Used only by the escalation loop, so the historical
#: agent's prompt is byte-for-byte unchanged when escalation is off.
SYSTEM_ESCALATION = SYSTEM + """
{"action":"search","index":<input index>,"text":"..."}     replace the input's text,
                                                          submit it and wait for the results
Prefer "search" (one step) over separate type and submit steps."""  + """
If you cannot make progress towards the task from here (no usable search, the
site does not offer it, a wall or an error page), reply
{"action":"abstain","reason":"..."} instead of guessing. If the input lists
previous_attempts, each of them did NOT reach the requested page, for the
reason given; do something different."""

#: ``words => text => bool``: does a word of ``text`` (runs of letters and digits in any script, split where a
#: lower-case letter meets an upper-case one, and before the last capital of a run of capitals followed by a
#: lower-case letter) equal or start with one of ``words``? ``searchBox``, ``HTMLSearchInput`` and ``zoekterm`` do;
#: ``research`` and ``Besuchen`` do not. Shared by the page scripts here and in ``cdp_guard``
SEARCH_TOKENS_JS = r"""
  const namedBy = words => text => String(text || '').replace(/(\p{Ll})(\p{Lu})/gu, '$1 $2')
    .replace(/(\p{Lu})(\p{Lu}\p{Ll})/gu, '$1 $2').toLowerCase()
    .split(/[^\p{L}\p{N}]+/u).some(t => t && words.some(w => t === w || t.startsWith(w)));
"""
#: the words that rank a control as a search control (whole words or word starts), after the page's structure
SEARCH_HINT_WORDS = ("search", "suche", "recherche", "buscar", "zoek", "cerca")

#: ONE element collector for OBSERVE_JS, SNAPSHOT_JS, PICK_JS and
#: BOUND_PICK_FN (the indices the model sees are the indices a click uses):
#:  * an icon-only control is listed as "search (icon)" when the page's structure makes it one (inside
#:    ``role=search`` or ``<search>``, or its ``aria-controls`` names a hidden text input), else when its
#:    id/class/data-testid names a search (without that it is dropped for having no text, and a header search
#:    that expands on click cannot be opened);
#:  * search controls come FIRST (search inputs by type, ``role=searchbox`` or landmark, then by name; then search
#:    toggles and submit buttons), then the rest in document order, 40 in total: a search box late in a long
#:    header does not fall off the list. The words only rank; the model chooses the action.
_COLLECT_JS = r"""
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=option]';
""" + SEARCH_TOKENS_JS + r"""
  const searchNamed = namedBy(""" + json.dumps(list(SEARCH_HINT_WORDS)) + r""");
  const texty = x => x.tagName === 'INPUT' && ['text', 'search', '']
    .includes((x.getAttribute('type') || 'text').toLowerCase());
  const shown = x => { const b = x.getBoundingClientRect(); return b.width > 0 && b.height > 0; };
  const hidesInput = el => !!el && [...(texty(el) ? [el] : el.querySelectorAll('input'))]
    .some(x => texty(x) && !shown(x));
  const controlsInput = n => String(n.getAttribute('aria-controls') || '').split(/\s+/).filter(Boolean)
    .some(i => hidesInput(document.getElementById(i)));
  const cands = [];
  document.querySelectorAll(sel).forEach(n => {
    const r = n.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const hint = [n.id, typeof n.className === 'string' ? n.className : '',
                  n.getAttribute('data-testid') || ''].join(' ');
    const landmark = !!n.closest('[role=search], search');
    const structural = n.tagName !== 'INPUT' && (landmark || controlsInput(n));
    const name = (n.getAttribute('aria-label') || n.placeholder || n.value ||
                  (n.innerText || '').trim() || n.getAttribute('title') ||
                  ((structural || searchNamed(hint)) ? 'search (icon)' : '')).slice(0, 70);
    if (!name && n.tagName !== 'INPUT') return;
    const t = (n.getAttribute('type') || '').toLowerCase();
    const blob = [name, hint, n.getAttribute('name') || ''].join(' ');
    let rank = 2;
    if (n.tagName === 'INPUT' && (t === 'search' || (n.getAttribute('role') || '').toLowerCase() === 'searchbox' ||
        landmark || searchNamed(blob) ||
        ['q', 's', 'query'].includes((n.getAttribute('name') || '').toLowerCase()))) rank = 0;
    else if (n.tagName !== 'A' && (structural || searchNamed(blob))) rank = 1;
    cands.push({n: n, name: name, rank: rank});
  });
  const ordered = cands.filter(c => c.rank === 0).concat(cands.filter(c => c.rank === 1),
                                                           cands.filter(c => c.rank === 2)).slice(0, 40);
"""

_OBSERVE_BODY = """
  const els = ordered.map((c, i) => ({i: i, tag: c.n.tagName.toLowerCase(),
              type: c.n.getAttribute('type') || '', name: c.name,
              href: (c.n.getAttribute('href') || '').slice(0, 60)}));
  const text = (document.body.innerText || '').split('\\n')
      .map(s => s.trim()).filter(Boolean).slice(0, 60).join('\\n').slice(0, 2500);
  const obs = {url: location.href, title: document.title || '', elements: els, text: text};
"""

OBSERVE_JS = """(() => {""" + _COLLECT_JS + _OBSERVE_BODY + """  return obs;
})()"""

#: OBSERVE_JS plus the listed nodes themselves. It is evaluated by reference
#: (``returnByValue: false``), so the nodes stay in the browser behind a CDP
#: object handle and nothing is written into the page.
SNAPSHOT_JS = """(() => {""" + _COLLECT_JS + _OBSERVE_BODY + """  return {obs: obs, nodes: ordered.map(c => c.n)};
})()"""

#: Focus (and with ``o.act`` click) the element the model was shown as
#: ``o.index``. ``snap`` is the SNAPSHOT_JS object of that observation, or null
#: when its handle is gone (the document was replaced). ``o.expect`` is the
#: element's row in the table the model saw (tag, type, name, href);
#: ``o.prev`` / ``o.next`` are the rows listed just before and after it;
#: ``o.rank`` / ``o.count`` say it was the rank-th of count rows equal to it.
#: In order:
#:  * ``held``: the observed node is still listed and still shows that row. A
#:    text field also counts when only its name changed (a placeholder that
#:    rotates, a value the page rewrote) and no other control shows the row;
#:  * ``moved``: exactly one listed control shows that row (the node was
#:    replaced, or its label moved to another node);
#:  * ``near``: several controls show it, and one of them sits between the
#:    neighbours the model saw more closely than any other;
#:  * ``order``: still tied, the list shows as many such rows as the model saw,
#:    and the one at the same rank among them is one of the tied (equal
#:    buttons rendered again below a new link);
#:  * ``same``: still tied, and the control now at that index is one of them
#:    (a page that re-renders a list replaces its nodes with equal ones).
#: Anything else is refused without acting: ``gone`` (no control shows the
#: row) or ``ambiguous`` (a tie neither the order nor the index settles).
#: ``unseen`` (no row: the model was not shown that index) acts on nothing.
#: The order and the index only break a tie between controls that show the row.
BOUND_PICK_FN = """function (snap, o) {""" + _COLLECT_JS + """
  const shows = (c, r) => !!c && !!r && c.n.tagName.toLowerCase() === r.tag &&
      (c.n.getAttribute('type') || '') === r.type && c.name === r.name &&
      (c.n.getAttribute('href') || '').slice(0, 60) === r.href;
  const field = n => n.tagName === 'TEXTAREA' || (n.tagName === 'INPUT' &&
      !/^(button|submit|reset|image|checkbox|radio|file|range|color)$/i.test(n.getAttribute('type') || ''));
  const want = o.expect;
  if (!want) return {ok: false, how: 'unseen'};
  const held = snap && snap.nodes ? snap.nodes[o.index] : null;
  const live = held && held.isConnected ? cands.find(x => x.n === held) : null;
  const m = cands.filter(x => shows(x, want));
  let c = live && (shows(live, want) || (!m.length && field(live.n) &&
      live.n.tagName.toLowerCase() === want.tag && (live.n.getAttribute('type') || '') === want.type))
      ? live : null;
  let how = 'held';
  if (!c) {
    if (!m.length) return {ok: false, how: 'gone'};
    if (m.length === 1) { c = m[0]; how = 'moved'; }
    else {
      const near = x => { const i = ordered.indexOf(x);
        return i < 0 ? 0 : (shows(ordered[i - 1], o.prev) ? 1 : 0) + (shows(ordered[i + 1], o.next) ? 1 : 0); };
      const best = Math.max(...m.map(near));
      const tied = m.filter(x => near(x) === best);
      const listed = ordered.filter(x => shows(x, want));
      if (best > 0 && tied.length === 1) { c = tied[0]; how = 'near'; }
      else if (listed.length === o.count && tied.includes(listed[o.rank])) { c = listed[o.rank]; how = 'order'; }
      else if (tied.includes(ordered[o.index])) { c = ordered[o.index]; how = 'same'; }
      else return {ok: false, how: 'ambiguous'};
    }
  }
  c.n.scrollIntoView(); c.n.focus();
  if (o.act) c.n.click();
  return {ok: true, how: how, at: ordered.indexOf(c)};
}"""

#: Focus or click ``ordered[idx]`` by index alone: for a session that has not
#: observed the page (a replayed trace), where there is nothing to bind to.
PICK_JS = """(idx => {""" + _COLLECT_JS + """
  const c = ordered[idx];
  if (!c) return false;
  const n = c.n;
  n.scrollIntoView(); n.focus();
  if (%s) n.click();
  return true;
})(%d)"""


class NotAContainerError(RuntimeError):
    """The endpoint given is not recognisably an isolated headless browser."""


def learn_endpoint(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get(ENDPOINT_ENV) or "").strip()


def _local_endpoint_address(host: str) -> bool:
    ip = ipaddress.ip_address(host.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def validate_learn_endpoint_url(endpoint: str) -> Optional[str]:
    """Validate endpoint syntax and refuse literal loopback hosts, without I/O."""
    if not endpoint.strip():
        return "unset"
    try:
        parsed = urllib.parse.urlsplit(endpoint.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "invalid URL"
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return "invalid URL"
    except ValueError:
        return "invalid URL"
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return "loopback"
    try:
        if _local_endpoint_address(host):
            return "loopback"
    except ValueError:
        pass
    return None


def assert_container_endpoint(endpoint: str, opener=None, resolver=None) -> Dict[str, Any]:
    """Refuse anything that is not recognisably a throwaway browser.

    Three checks, all cheap and all made **before any action is taken**:

    1. **the host is not loopback**, including any resolved hostname address.
       A DevTools endpoint on ``127.0.0.1`` is
       most likely the user's own Chrome started with remote debugging,
       carrying their sessions and their cookies. Clicking and typing in it is
       desktop actuation however it is described, and this module will not do
       it. This is the check that actually protects the user.
    2. **the browser says it is headless**, if it says anything. Note that this
       is weaker than it sounds and was measured to be: `chromedp/headless-shell`
       reports ``Chrome/151.0.7922.47`` and no ``Headless`` token at all, so
       this check passes nobody and fails nobody on the image AnyUse's own
       benchmarks use.
    3. **the browser has nothing open.** This is the one that does work. A fresh
       container browser has one ``about:blank`` target; a browser a person is
       using has their tabs in it. Any target whose URL is a real page means
       somebody is using this browser, and the loop refuses.

    These are **heuristics, not proofs**: a determined setup defeats all three.
    That is precisely why the learn loop is a CLI command the user runs against
    an endpoint they chose, and not an MCP tool an external agent can point
    wherever it likes. Returns the ``/json/version`` payload on success.
    """
    problem = validate_learn_endpoint_url(endpoint)
    endpoint = endpoint.strip()
    if problem == "unset":
        raise NotAContainerError(
            f"the learn loop needs {ENDPOINT_ENV} to name the DevTools endpoint of "
            f"a headless browser in a container. There is no default and nothing "
            f"is started for you: an agent that clicks and types must never be "
            f"pointed at a browser somebody is using.")
    if problem and problem != "loopback":
        raise NotAContainerError(
            f"{endpoint} is an invalid URL. Set {ENDPOINT_ENV} to a valid "
            f"http://<container-ip>:9222 DevTools URL.")
    if problem is None:
        parsed = urllib.parse.urlsplit(endpoint)
        host = parsed.hostname.rstrip(".")
        try:
            ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            # Keep doctor syntax checks offline; resolve only when the endpoint
            # is about to be used, before even asking for browser metadata.
            try:
                addresses = (resolver or socket.getaddrinfo)(
                    host, parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM)
                if not addresses:
                    raise ValueError("no addresses")
                if any(_local_endpoint_address(address[4][0]) for address in addresses):
                    problem = "loopback"
            except (OSError, ValueError) as exc:
                raise NotAContainerError(
                    f"could not resolve the learn endpoint host {host!r}; "
                    f"use the container's own address.") from exc
    if problem == "loopback":
        raise NotAContainerError(
            f"{endpoint} is a loopback endpoint. That is almost always the "
            f"browser on this machine, with this user's sessions in it, and the "
            f"learn loop clicks and types. Point it at the container's own "
            f"address instead (Apple's `container list` prints it).")
    opener = opener or (lambda u: urllib.request.urlopen(u, timeout=10))
    base = endpoint.rstrip("/")
    with opener(f"{base}/json/version") as r:
        info = json.load(r)
    blob = f"{info.get('Browser') or ''} {info.get('User-Agent') or ''}".lower()
    info["headless_declared"] = "headless" in blob
    if info["headless_declared"]:
        return info

    # The browser does not say it is headless. Ask what it has open instead:
    # a browser somebody is using has their pages in it.
    try:
        with opener(f"{base}/json/list") as r:
            targets = json.load(r)
    except Exception as exc:
        raise NotAContainerError(
            f"{endpoint} does not declare itself headless and its target list "
            f"could not be read ({type(exc).__name__}), so there is no way to "
            f"tell it apart from a browser somebody is using. Refusing.") from None
    open_pages = [t for t in (targets or [])
                  if str(t.get("type")) == "page"
                  and not _is_blank(str(t.get("url") or ""))]
    if open_pages:
        raise NotAContainerError(
            f"{endpoint} has {len(open_pages)} page(s) open "
            f"({', '.join(str(t.get('url'))[:40] for t in open_pages[:3])}). "
            f"A throwaway container browser has nothing open; a browser with "
            f"pages in it is somebody's. The learn loop clicks and types, so it "
            f"refuses.")
    info["headless_declared"] = False
    info["note"] = ("the browser does not declare itself headless; accepted "
                    "because it is not on loopback and has no pages open")
    return info


def _is_blank(url: str) -> bool:
    low = url.strip().lower()
    return (not low or low == "about:blank"
            or low.startswith(("chrome://", "devtools://", "chrome-extension://")))


# ---------------------------------------------------------------------------
# what a run produces
# ---------------------------------------------------------------------------

@dataclass
class AgentRun:
    """One attempt at one task with one value, plus everything it touched."""
    value: str = ""
    task: str = ""
    start_url: str = ""
    final_url: str = ""
    dom: str = ""
    text: str = ""
    network: List[dict] = field(default_factory=list)
    steps: List[dict] = field(default_factory=list)
    #: every string the agent typed, in order — the parameter candidates
    typed: List[str] = field(default_factory=list)
    model_calls: int = 0
    cached_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    error: str = ""
    #: provider-reported US$ (OpenRouter ``usage.cost``), summed; 0.0 = not reported
    cost_usd: float = 0.0
    #: why the step loop ended: done | abstain | no_progress | max_steps |
    #: provider_unreachable | provider_empty | model_unreadable | model_error | error
    stop: str = ""
    #: the observed page state (URL + title + element table) changed at least
    #: once during the run, or it ended on another URL than it started on
    state_changed: bool = False
    #: navigations the gate refused in THIS run (robots / off-site)
    refused: List[str] = field(default_factory=list)
    #: empty answers that were retried inside a step (and then answered)
    empty_retries: int = 0
    #: unreadable answers that were repaired inside a step (and then answered)
    unreadable_retries: int = 0
    #: the escalation log, when an :class:`EscalatingAgent` produced this run
    escalation: Optional[dict] = None
    #: per-step trace, only when the agent was built with ``trace=True``
    #: (bench-only): what the agent SAW (the candidate elements and the URL),
    #: what it CHOSE, and what happened (the URL after the action). This is
    #: what a later study of "re-rank the candidates by description" needs,
    #: and it is off by default because it is large and nobody reads it live.
    trace: List[dict] = field(default_factory=list)
    #: live solves opened on this run (status + receipt; never page content)
    live_solves: List[dict] = field(default_factory=list)
    #: typed search only: the page's DOM just before the value was typed, so
    #: the ground truth can tell what the typing changed (never sent anywhere)
    before_dom: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.dom) and not self.error

    def to_dict(self, with_dom: bool = False, with_trace: bool = False) -> dict:
        out = {"value": self.value, "task": self.task, "start_url": self.start_url,
               "final_url": self.final_url, "steps": self.steps,
               "typed": list(self.typed), "model_calls": self.model_calls,
               "cached_calls": self.cached_calls, "tokens_in": self.tokens_in,
               "tokens_out": self.tokens_out, "seconds": round(self.seconds, 3),
               "dom_bytes": len(self.dom), "requests": len(self.network),
               "error": self.error, "cost_usd": round(self.cost_usd, 6),
               "stop": self.stop, "empty_retries": self.empty_retries,
               "unreadable_retries": self.unreadable_retries}
        if self.escalation is not None:
            out["escalation"] = self.escalation
        if with_dom:
            out["dom"] = self.dom
        if with_trace:
            out["trace"] = list(self.trace)
        return out

    def as_recording(self, workflow: str, param_name: str, tag: str,
                     cls: str = "list") -> dict:
        """The agent's run, in the SAME shape the human recorder produces.

        This is the whole point of the module. Nothing downstream — not the
        compiler, not the proof, not the persistence rule — can tell that a
        human did not make this, and none of them is given a reason to be more
        generous because an agent did.
        """
        # a credential-named request field the site's own page stated is kept as
        # a locator (page_literals); its value leaves the recording entirely. Any
        # other request stays as the agent's browser sent it
        from anyuse.execution.page_literals import mark_network, scrub
        site = urllib.parse.urlsplit(self.final_url or self.start_url).hostname or ""
        secrets: set = set()
        network = mark_network(self.network, site, secrets_out=secrets, refuse_unstated=False)
        rec = {
            "workflow": workflow,
            "site": urllib.parse.urlsplit(self.final_url or self.start_url).netloc,
            "category": "learned", "cls": cls, "tag": tag,
            "param": {"name": param_name, "value": self.value},
            "final_url": self.final_url,
            "ui": {"text": self.text, "items": []},     # derived across values
            "dom": self.dom,
            "network": network,
            "ui_ms": self.seconds * 1000.0,
            "wall_ms": self.seconds * 1000.0,
            "ui_bytes": sum(int(r.get("bytes") or 0) for r in self.network),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            # provenance, carried all the way into compiled_api.json
            "origin": "agent",
            "agent": {"task": self.task, "steps": len(self.steps),
                      "model_calls": self.model_calls, "typed": list(self.typed),
                      "start_url": self.start_url},
            **({"before_dom": self.before_dom} if self.before_dom else {}),
        }
        if secrets:
            for k in ("ui", "dom", "before_dom"):
                if k in rec:
                    rec[k] = scrub(rec[k], secrets)
        return rec


# ---------------------------------------------------------------------------
# the model boundary
# ---------------------------------------------------------------------------

CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"  # compatibility constant
DEFAULT_MODEL = "gpt-oss-120b"
#: Cerebras list prices for gpt-oss-120b, USD per 1M tokens, as of September 2026.
PRICE_IN, PRICE_OUT = 0.25, 0.69


#: Where the model key lives. The environment first (``CEREBRAS_API_KEY``),
#: then the file named by ``ANYUSE_MODEL_ENV_FILE``, then ``model.env`` in the
#: secrets directory (``anyuse doctor`` prints where that is). The file is
#: ``KEY=value`` lines, ``export`` allowed. The env var exists because a
#: harness that redirects HOME to a scratch directory would otherwise look for
#: the key in the scratch directory, fail, and report it as a model outage.
KEY_FILE_ENV = "ANYUSE_MODEL_ENV_FILE"
KEY_FILE_NAME = "model.env"


def key_file_path() -> str:
    return model_env_file_path()


def read_api_key(name: str = "CEREBRAS_API_KEY") -> str:
    """In-process only. Never printed, never logged, never on a command line."""
    return read_model_api_key(name)


class ProviderUnreachable(RuntimeError):
    """402 / 408 / 429 / 5xx / timeout: an outage, never a verdict on the agent."""

    def __init__(self, msg: str = "", usage: Optional[dict] = None, calls: int = 0):
        super().__init__(msg)
        #: tokens / cost the failed call(s) still consumed (they are billed)
        self.usage = dict(usage or {})
        self.calls = int(calls)


class EmptyAnswer(ProviderUnreachable):
    """HTTP 200 with no answer text, twice (the second time with twice the
    token budget). Same class as an outage: never a verdict, never a repair
    round. A reasoning model that spends ``max_tokens`` on hidden reasoning
    produces exactly this."""


class UnreadableAnswer(ProviderUnreachable):
    """HTTP 200 with answer text that is not a JSON action, twice (the second
    time after one in-step repair message). Same class as an outage: never a
    verdict, never a repair round, never escalated. Without it an unreadable
    reply would silently become ``done``; the attempt would then fail its goal
    check and be escalated as ``no_progress`` at the next level's price."""


class BudgetExhausted(ProviderUnreachable):
    """The shared run budget refused a request before it reached a provider."""


#: the one in-step repair message after an unreadable reply. No page content
#: and no history go with it: the model already has both in the messages it
#: is answering, and only its own (truncated) reply is echoed back.
UNREADABLE_REPAIR = ("Your previous reply was not a JSON action. Reply with exactly "
                     "one JSON object such as {\"action\": \"click\", \"index\": 0} "
                     "and nothing else.")


#: an empty answer is retried ONCE inside the same step with this many times
#: the level's token budget, capped here
EMPTY_RETRY_FACTOR = 2
EMPTY_RETRY_MAX_TOKENS = 4000


class CerebrasBrain:
    """One adapter-backed action per call, with a repeat-payload cache.

    The historical class name is kept for callers, but its provider is the
    resolved model role (``agent`` by default; ``agent_l2`` / ``agent_l3`` for
    the escalation levels) and can be Cerebras, OpenRouter, Gemini or local.
    ``images`` (data URLs) go through the adapter's image input; the cache key
    includes them.
    """

    def __init__(self, model: str = "", cache: Optional[dict] = None,
                 adapter: Optional[ModelAdapter] = None, role: str = "agent",
                 max_tokens: int = 200, run_budget: Optional[RunBudget] = None,
                 price_in: Optional[float] = None,
                 price_out: Optional[float] = None):
        self._adapter = adapter or ModelAdapter.for_role(
            role, user_agent=USER_AGENT, budget=run_budget,
            **({"model": model} if model else {})
        )
        if run_budget is not None:
            self._adapter.set_budget(run_budget, price_in=price_in, price_out=price_out)
        self.model = self._adapter.config.model
        self.provider = self._adapter.config.provider
        self.cache = cache if cache is not None else {}
        self.max_tokens = max_tokens

    def __call__(self, messages: List[dict], images: Sequence[str] = ()):
        fingerprint = json.dumps([self.model, messages, list(images)], sort_keys=True) \
            if images or self.model != DEFAULT_MODEL else json.dumps(messages, sort_keys=True)
        if fingerprint in self.cache:
            action, usage = self.cache[fingerprint]
            return dict(action), dict(usage), True
        spent: List[dict] = []
        calls = 0                      # requests sent, adapter retries included
        empty_retries = unreadable_retries = 0

        def ask(msgs: List[dict], budget: int):
            nonlocal calls
            res = self._adapter.complete(msgs, temperature=0.1, max_tokens=budget,
                                         images=tuple(images))
            calls += int(res.llm_calls or 0)
            if res.llm_calls:
                spent.append(res.usage.to_dict())
            return res

        result = ask(messages, self.max_tokens)
        if not result.ok and getattr(result, "empty_answer", False):
            # one retry with a larger budget; it is part of THIS step, not a
            # repair round, and both calls are billed and counted
            empty_retries += 1
            result = ask(messages, min(EMPTY_RETRY_MAX_TOKENS,
                                       self.max_tokens * EMPTY_RETRY_FACTOR))
        action = parse_action(result.text) if result.ok else None
        if result.ok and action is None:
            # one in-step repair: the model is told that its reply was not an
            # action and asked again with the same budget. Not a repair round;
            # both calls are billed and counted.
            unreadable_retries += 1
            repair = list(messages) + [
                {"role": "assistant", "content": result.text.strip()[:600]},
                {"role": "user", "content": UNREADABLE_REPAIR}]
            result = ask(repair, self.max_tokens)
            action = parse_action(result.text) if result.ok else None
        if not result.ok:
            if getattr(result, "budget_exhausted", False):
                raise BudgetExhausted(result.error or "run budget exhausted",
                                      usage=_sum_usage(spent), calls=calls)
            if getattr(result, "empty_answer", False):
                raise EmptyAnswer(result.error or "provider returned an empty answer",
                                  usage=_sum_usage(spent), calls=calls)
            if result.provider_unreachable:
                raise ProviderUnreachable(result.error or "provider unreachable",
                                          usage=_sum_usage(spent), calls=calls)
            raise RuntimeError(result.error or "agent model failed")
        if action is None:
            raise UnreadableAnswer("the model's reply was not a JSON action, twice",
                                   usage=_sum_usage(spent), calls=calls)
        usage = _sum_usage(spent) if len(spent) > 1 else result.usage.to_dict()
        usage["llm_calls"] = calls
        if empty_retries:
            usage["empty_retries"] = empty_retries
        if unreadable_retries:
            usage["unreadable_retries"] = unreadable_retries
        self.cache[fingerprint] = (action, usage)
        return dict(action), dict(usage), False


def _sum_usage(parts: Sequence[dict]) -> dict:
    out: Dict[str, Any] = {}
    for part in parts:
        for k, v in (part or {}).items():
            if isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + v
    return out


def parse_action(text: str) -> Optional[dict]:
    """The model's reply as an action, or ``None`` when it is not one.

    Tolerant on the way in — a fenced block, prose around the object, a list
    wrapping the object, a trailing comma — and strict on the way out: no JSON
    object means no action. ``None`` is never turned into ``done``; the brain
    repairs once and then raises :class:`UnreadableAnswer`.
    """
    text = (text or "").strip()
    if not text:
        return None
    candidates: List[str] = []
    if "```" in text:
        candidates += re.findall(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    candidates.append(text)
    for cand in candidates:
        obj = _first_object(cand)
        if obj is not None:
            return obj
    return None


def _first_object(text: str) -> Optional[dict]:
    """The first JSON object in ``text``: as a whole, inside a list, or found
    by a brace scan (a trailing comma before ``}``/``]`` is repaired)."""
    text = text.strip()
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return whole
    if isinstance(whole, list):
        return next((x for x in whole if isinstance(x, dict)), None)
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start:i + 1]
                for attempt in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
                    try:
                        obj = json.loads(attempt)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        return obj
                    break
                start = None
    return None


def spend_usd(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT


# ---------------------------------------------------------------------------
# the agent
# ---------------------------------------------------------------------------

def action_index(action: dict) -> int:
    """The element index an action names, or -1. Index 0 is a real element.

    Not ``int(action.get("index", -1) or -1)``, which turns 0 into -1 and
    makes the FIRST interactive element on every page unreachable.
    """
    raw = action.get("index")
    if raw is None or raw == "":
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


#: what the model is told when a focus or click it asked for was refused
#: because the element moved or changed after the observation
PAGE_CHANGED_NOTE = ("not done: the page changed before this action and the element "
                     "was not where it was shown; the element list below is the page now")


def pick_refusal(page) -> str:
    """Why the session's last focus or click did not act, or ``""``.

    Only a refusal of a bound pick counts (``gone``, ``ambiguous``: see
    :data:`BOUND_PICK_FN`). A session that does not report its picks, and a
    pick by index alone, never refuse. Nor does an index the model was not
    shown (``unseen``): the page did not change, nothing was there to act on,
    and the step goes on as it did before (a submit still presses Enter in
    the field that has the focus)."""
    last = getattr(page, "last_pick", None)
    if not isinstance(last, dict) or last.get("ok") or \
            last.get("how") in (None, "", "index", "unseen"):
        return ""
    return str(last.get("how"))


def same_site(start_url: str, url: str, related_origins=()) -> bool:
    """Exact-origin stay-on-site rule, plus explicitly approved origins."""
    from anyuse.execution.request_policy import exact_origin_allowlist, origin
    return origin(url) in exact_origin_allowlist(start_url, related_origins)


def task_text(task: str, value: str) -> str:
    """Bind the user's ``{placeholder}`` to this value, or append it."""
    if "{" in task and "}" in task:
        return re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", value, task)
    return f"{task} (for: {value})"


async def _resolve_consent_wall(page, run: AgentRun, workflow: str) -> bool:
    """Reject a consent wall deterministically, or post a card and stop.

    Returns true only when the page remains a wall.  A banner over rendered
    results is ignored by the same detector used after an agent run.
    """
    from anyuse.execution.blocked import COOKIE_WALL, detect_blocked_ui
    from anyuse.execution.consent import (ASK, NEVER, REJECT, automatic_action,
                                          choose_reject, choose_settings,
                                          consent_policy, handoff_question,
                                          remembered_decision)
    from anyuse.execution.learn_loop import preview_items

    dom = await page.dom()
    text = await page.text()
    url = await page.url()
    items = preview_items(dom)[1] if dom else []
    state = detect_blocked_ui(text, "", url, [], items=items)
    if not state.blocked or state.reason != COOKIE_WALL:
        return False

    policy = consent_policy()
    remembered = remembered_decision(state.host)
    action = automatic_action(state.host, policy)
    if action == REJECT and hasattr(page, "consent_controls") and hasattr(
            page, "click_consent"):
        controls = await page.consent_controls()
        choice = choose_reject(controls)
        layer = 1
        if choice is None:
            settings_choice = choose_settings(controls)
            if settings_choice is not None and await page.click_consent(
                    settings_choice.index):
                run.steps.append(settings_choice.step(layer=1))
                controls = await page.consent_controls()
                choice = choose_reject(controls)
                layer = 2
        if choice is not None and await page.click_consent(choice.index):
            run.steps.append(choice.step(layer=layer))
            dom, text, url = await page.dom(), await page.text(), await page.url()
            items = preview_items(dom)[1] if dom else []
            after = detect_blocked_ui(text, "", url, [], items=items)
            if not after.blocked or after.reason != COOKIE_WALL:
                return False

    if action != NEVER:
        state_dict = state.to_dict()
        state_dict["message"] = handoff_question(state.host)
        state_dict["consent"] = {"policy": policy, "remembered": remembered,
                                 "status": ASK}
        try:
            from anyuse.execution import human_inbox
            card = human_inbox.Inbox().post_standalone(
                {**state_dict, "card_kind": human_inbox.KIND_CONSENT}, workflow)
            run.steps.append({"action": "consent_handoff", "deterministic": True,
                              "card": card["id"], "kind": card["kind"],
                              "host": state.host})
        except Exception as exc:
            logger.debug("consent card not recorded: %s", exc)
    return True


class StepAgent:
    """Drives one task to completion, one action at a time.

    ``brain`` is any callable ``(messages) -> (action, usage, cached)`` and
    ``session_factory`` is any callable ``(endpoint) -> AgentSession``. Both are
    injected in every test in this repository, so the whole class runs offline
    with no browser and no model.
    """

    def __init__(self, brain: Callable, session_factory: Optional[Callable] = None,
                 endpoint: str = "", max_steps: int = MAX_STEPS,
                 allow_url: Optional[Callable] = None, trace: bool = False,
                 stay_on_site: bool = True, related_origins=(),
                 system: str = "", vision: bool = False,
                 no_progress_steps: int = 0, max_calls: int = 0,
                 deadline_s: float = 0.0, live_solver=None, origin: Optional[str] = None,
                 task_id: str = ""):
        self.brain = brain
        #: ``human_solves_live`` (execution/live_solve.py): a detected
        #: challenge opens a live solve for a person instead of stopping, but
        #: only for an allowed run origin (never ``bench``). The model is not
        #: called while the page is quarantined.
        self.live_solver = live_solver
        self.origin = origin
        self.task_id = task_id
        #: the system prompt; the escalation loop uses :data:`SYSTEM_ESCALATION`
        #: (the same prompt plus a typed ``abstain`` verb)
        self.system = system or SYSTEM
        #: send a screenshot of the CONTAINER browser with every step (level L3)
        self.vision = vision
        #: trigger (c): stop when the page state (URL without fragment + title
        #: + element-table hash) is unchanged for this many consecutive steps;
        #: 0 = off (the historical behaviour)
        self.no_progress_steps = int(no_progress_steps)
        #: per-run caps from the escalation level; 0 = none
        self.max_calls = int(max_calls)
        self.deadline_s = float(deadline_s)
        self.endpoint = endpoint
        self.max_steps = max_steps
        self._factory = session_factory
        #: keep a per-step trace on every run (bench-only; see ``AgentRun.trace``)
        self.trace = trace
        #: refuse top-level navigations off the site the run started on
        #: (the agent once followed a site's "search the
        #: web" link to a search engine and proposed THAT as the candidate).
        #: Off-site navigations are refused inside the browser before they are
        #: sent, exactly like a robots refusal, and reported the same way.
        self.stay_on_site = stay_on_site
        self.related_origins = tuple(related_origins or ())
        #: ``(url) -> bool``, consulted BEFORE every top-level navigation the
        #: agent causes. The recorder checks the one URL it chose; an agent
        #: clicks its own way around a site, so the only honest place for a
        #: robots check is in front of the request, not after it.
        self.allow_url = allow_url
        #: navigations the gate refused, across all runs of this agent
        self.refused: List[str] = []

    async def _live_solve(self, page, start_url: str, run: "AgentRun", obs: dict) -> bool:
        """One live solve per run on a challenge page; True when the person
        cleared it. Only with a solver whose policy applies to this run's
        origin (never bench) and a page that can bridge to the solver."""
        import asyncio
        from anyuse.execution import live_solve as ls
        if run.live_solves or not ls.solver_applies(self.live_solver, self.origin) or \
                not hasattr(page, "live_bridge"):
            return False
        host = urllib.parse.urlsplit(str(obs.get("url") or start_url)).hostname or \
            urllib.parse.urlsplit(start_url).hostname or ""
        req = ls.SolveRequest(task_id=self.task_id or run.task[:64], host=host.lower(),
                              origin=self.origin, run_id=self.task_id, step="agent")
        out = await ls.solve_async(self.live_solver, req,
                                   page.live_bridge(asyncio.get_running_loop()))
        run.live_solves.append({"status": out.get("status"),
                                "receipt": out.get("receipt")})
        run.steps.append({"live_solve": out.get("status"), "model": "not called"})
        return out.get("status") == "cleared"

    def session_factory(self):
        if self._factory is not None:
            return self._factory
        from anyuse.execution.agent_cdp import CdpAgentSession
        return CdpAgentSession

    def run(self, task: str, start_url: str, value: str,
            feedback: Optional[List[dict]] = None) -> AgentRun:
        import asyncio
        return asyncio.run(self.run_async(task, start_url, value, feedback))

    async def run_async(self, task: str, start_url: str, value: str,
                        feedback: Optional[List[dict]] = None) -> AgentRun:
        run = AgentRun(value=value, task=task_text(task, value), start_url=start_url)
        t0 = time.monotonic()
        factory = self.session_factory()
        try:
            gate = self.allow_url
            if self.stay_on_site:
                robots = self.allow_url

                def gate(url: str, _start=start_url, _robots=robots,
                         _related=self.related_origins) -> bool:
                    if not same_site(_start, url, _related):
                        return False
                    from anyuse.execution.request_policy import (destination_refusal,
                                                                 exact_origin_allowlist)
                    if destination_refusal(url, exact_origin_allowlist(_start, _related)):
                        return False
                    return bool(_robots(url)) if _robots is not None else True
            kwargs = {"allow_url": gate} if gate else {}
            async with factory(self.endpoint, **kwargs) as page:
                # the agent's own session: several submit controls next to a search
                # input may go to the configured small model (inside an open scope)
                try:
                    page.submit_chooser = True
                except AttributeError:
                    pass
                await page.goto(start_url)
                consent_blocked = await _resolve_consent_wall(
                    page, run, f"agent:{urllib.parse.urlsplit(start_url).netloc}")
                history: List[dict] = []
                last_state, unchanged, first_state = "", 0, ""
                run.stop = "blocked: cookie_wall" if consent_blocked else "max_steps"
                for step_no in range(0 if consent_blocked else self.max_steps):
                    obs = await page.observe() or {}
                    # Owner decision: no model ever sees a challenge page or
                    # decides where to click on one; a person solves it
                    # (docs/HUMAN_INBOX.md, execution/live_solve.py).
                    from anyuse.execution.live_solve import challenge_evidence, model_may_see
                    if not model_may_see(obs, getattr(page, "_target", None)):
                        solved = await self._live_solve(page, start_url, run, obs)
                        if solved:
                            continue                  # the person cleared it: go on
                        run.stop = "blocked: challenge"
                        run.steps.append({"challenge": (challenge_evidence(obs)
                                                        or "quarantined")[:120],
                                          "model": "not called"})
                        break
                    state = page_state(obs)
                    first_state = first_state or state
                    if state != first_state:
                        run.state_changed = True
                    if last_state and state == last_state:
                        unchanged += 1
                    else:
                        unchanged = 0
                    last_state = state
                    if self.no_progress_steps and unchanged >= self.no_progress_steps:
                        run.stop = "no_progress"
                        run.steps.append({"no_progress": unchanged})
                        break
                    if self.max_calls and run.model_calls >= self.max_calls:
                        run.stop = "cap_calls"
                        break
                    if self.deadline_s and time.monotonic() - t0 >= self.deadline_s:
                        run.stop = "cap_seconds"
                        break
                    body = {"task": run.task}
                    if feedback:
                        body["previous_attempts"] = feedback
                    body.update({"history": history[-4:], "url": obs.get("url", ""),
                                 "elements": obs.get("elements", []),
                                 "visible_text": obs.get("text", "")})
                    messages = [
                        {"role": "system", "content": self.system},
                        {"role": "user", "content": json.dumps(body)[:14000]}]
                    try:
                        if self.vision and hasattr(page, "screenshot"):
                            shot = await page.screenshot()
                            action, usage, cached = self.brain(
                                messages, images=[shot] if shot else [])
                        else:
                            action, usage, cached = self.brain(messages)
                    except ProviderUnreachable as exc:
                        run.stop = ("budget_exhausted" if isinstance(exc, BudgetExhausted)
                                    else "provider_empty" if isinstance(exc, EmptyAnswer)
                                    else "model_unreadable" if isinstance(exc, UnreadableAnswer)
                                    else "provider_unreachable")
                        run.steps.append({"error": repr(exc)[:160]})
                        # the failed calls were made and are billed: count them
                        run.model_calls += exc.calls
                        run.tokens_in += int(exc.usage.get("prompt_tokens", 0) or 0)
                        run.tokens_out += int(exc.usage.get("completion_tokens", 0) or 0)
                        run.cost_usd += float(exc.usage.get("cost_usd", 0.0) or 0.0)
                        break
                    except Exception as exc:
                        run.stop = "model_error"
                        run.steps.append({"error": repr(exc)[:160]})
                        break
                    run.model_calls += 1
                    run.cached_calls += int(bool(cached))
                    if not cached:
                        # every request the step made is counted: the empty and
                        # unreadable retries and the adapter's own 408/429/5xx
                        # retries. A brain that does not report ``llm_calls``
                        # made one request per answer plus its empty retries.
                        empties = int(usage.get("empty_retries", 0) or 0)
                        made = int(usage.get("llm_calls") or (1 + empties))
                        run.model_calls += max(0, made - 1)
                        run.empty_retries += empties
                        run.unreadable_retries += int(usage.get("unreadable_retries", 0) or 0)
                        run.tokens_in += int(usage.get("prompt_tokens", 0) or 0)
                        run.tokens_out += int(usage.get("completion_tokens", 0) or 0)
                        run.cost_usd += float(usage.get("cost_usd", 0.0) or 0.0)
                    run.steps.append(action)
                    history.append(action)
                    kind = str(action.get("action", "done"))
                    if is_abstention(action):
                        run.stop = "abstain"
                        break
                    if self.trace:
                        idx_seen = action_index(action)
                        run.trace.append({
                            "step": step_no, "url": obs.get("url", ""),
                            "candidates": [{"i": e.get("i"), "tag": e.get("tag"),
                                            "type": e.get("type"), "name": e.get("name"),
                                            "href": e.get("href")}
                                           for e in (obs.get("elements") or [])],
                            "visible_text_chars": len(obs.get("text") or ""),
                            "choice": dict(action),
                            "chosen": next((e for e in (obs.get("elements") or [])
                                            if e.get("i") == idx_seen), None),
                            "cached": bool(cached)})
                    if kind == "done":
                        run.stop = "done"
                        break
                    idx = action_index(action)
                    picked = kind in ("type", "submit", "click") or (
                        kind == "search" and hasattr(page, "search"))
                    if kind == "search" and hasattr(page, "search"):
                        # one transaction (replace, read back, submit,
                        # wait for the results); one step for the progress check
                        typed = str(action.get("text", value))
                        res = await page.search(idx, typed)
                        if not pick_refusal(page):
                            run.typed.append(typed)
                        if isinstance(res, dict):
                            run.steps[-1] = dict(action, outcome={
                                k: res.get(k) for k in ("focused", "held", "retyped",
                                                        "submit", "url")})
                    elif kind == "type":
                        typed = str(action.get("text", value))
                        if await page.focus(idx):
                            await page.type(typed)
                            run.typed.append(typed)
                    elif kind == "submit":
                        if await page.focus(idx) or not pick_refusal(page):
                            await page.press_enter()
                    elif kind == "click":
                        await page.click(idx)
                    else:
                        await page.settle(2.0)
                    refusal = pick_refusal(page) if picked else ""
                    if refusal:
                        # nothing was focused, typed or clicked; the model sees
                        # why in its history and decides again on the next
                        # observation
                        note = dict(run.steps[-1])
                        note["outcome"] = dict(note.get("outcome") or {}, acted=False,
                                               page_changed=refusal, note=PAGE_CHANGED_NOTE)
                        run.steps[-1] = note
                        history[-1] = note
                    if self.trace and run.trace:
                        after = await page.url()
                        run.trace[-1]["outcome"] = {"url_after": after,
                                                    "navigated": after != run.trace[-1]["url"]}
                        if picked and isinstance(getattr(page, "last_pick", None), dict):
                            run.trace[-1]["outcome"]["pick"] = dict(page.last_pick)
                        if refusal:
                            run.trace[-1]["outcome"]["acted"] = False
                run.dom = await page.dom()
                run.text = await page.text()
                run.final_url = await page.url()
                run.network = await page.network()
                refused = list(getattr(page, "refused", []) or [])
                if refused:
                    self.refused += refused
                    run.refused = refused
                    run.steps.append({"robots_refused": refused[:5]})
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"[:200]
            from anyuse.execution.browser_supervisor import classify
            if classify(exc):
                # the browser stopped answering: not the agent's failure, and
                # no further attempt may run against a dead browser
                run.stop = INFRASTRUCTURE_STOP
            else:
                run.stop = run.stop if run.stop not in ("", "max_steps") else "error"
        norm = lambda u: str(u or "").split("#", 1)[0].rstrip("/")
        if run.final_url and norm(run.final_url) != norm(start_url):
            run.state_changed = True
        run.seconds = time.monotonic() - t0
        return run


# ---------------------------------------------------------------------------
# the navigator — one URL, opened and recorded, with no model and no clicking
# ---------------------------------------------------------------------------

class Navigator:
    """Open ONE URL in the container browser and record it. No model, no actions.

    After a candidate page has been confirmed, the further values
    are bound into the confirmed URL and simply opened — the same kind of
    traffic as a tier-2 replay — and recorded through the same session class
    the agent uses, so the recording has the shape the compiler needs (DOM,
    text, final URL, the network log with bodies). ``model_calls`` is 0 and
    ``steps`` is the single ``navigate`` action, which is how the learn loop
    tells a substituted run from an agent run.

    ``allow_url`` is consulted BEFORE the navigation and again inside the
    session for every top-level navigation the page causes (a redirect is a
    navigation too), exactly as for the agent.
    """

    def __init__(self, endpoint: str, session_factory: Optional[Callable] = None,
                 allow_url: Optional[Callable] = None, extra_settle_s: float = 2.0,
                 stable_ms: int = 800, ceiling_s: float = 10.0,
                 title_stable_ms: int = 1000, quiet_ms: int = 1000,
                 clock: Callable[[], float] = time.monotonic):
        self.endpoint = endpoint
        self._factory = session_factory
        self.allow_url = allow_url
        self.extra_settle_s = extra_settle_s
        #: after the navigation, keep reading until the document is complete
        #: and its length has stood still for ``stable_ms`` — never past
        #: ``ceiling_s``, which is tier 2's timer window. Without it,
        #: a page that sets its title from JavaScript is read before it has,
        #: and the landing check refuses the right page for an empty title.
        self.stable_ms = stable_ms
        self.ceiling_s = ceiling_s
        #: A page that ships a PLACEHOLDER title and replaces it after a later
        #: fetch is complete, still and titled before its real title arrives.
        #: So the read also waits until
        #: the title TEXT has not changed for ``title_stable_ms`` AND the page's
        #: own fetches (XHR / fetch on the page's site: none in flight, none
        #: finished) have been quiet for ``quiet_ms`` - still under the hard cap
        #: ``ceiling_s``. Timing only: what is read is unchanged.
        self.title_stable_ms = title_stable_ms
        self.quiet_ms = quiet_ms
        self.clock = clock
        self.refused: List[str] = []

    def session_factory(self):
        if self._factory is not None:
            return self._factory
        from anyuse.execution.agent_cdp import CdpAgentSession
        return CdpAgentSession

    def __call__(self, url: str, value: str) -> AgentRun:
        import asyncio
        return asyncio.run(self.go(url, value))

    async def go(self, url: str, value: str) -> AgentRun:
        run = AgentRun(value=value, task=f"open {url}", start_url=url)
        t0 = self.clock()
        if self.allow_url is not None:
            try:
                allowed = bool(self.allow_url(url))
            except Exception:
                allowed = False
            if not allowed:
                self.refused.append(url)
                run.error = f"robots_refused: {url[:120]} was not opened"
                run.seconds = self.clock() - t0
                return run
        factory = self.session_factory()
        try:
            kwargs = {"allow_url": self.allow_url} if self.allow_url else {}
            async with factory(self.endpoint, **kwargs) as page:
                await page.goto(url)
                await self._settle(page, t0)
                run.steps = [{"action": "navigate", "url": url}]
                run.dom = await page.dom()
                run.text = await page.text()
                run.final_url = await page.url()
                run.network = await page.network()
                refused = list(getattr(page, "refused", []) or [])
                if refused:
                    self.refused += refused
                    run.steps.append({"robots_refused": refused[:5]})
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"[:200]
        run.seconds = self.clock() - t0
        return run

    #: the page's state, read in the page (no request): readiness, the document's
    #: length, the title TEXT, and how many fetch / XHR responses it has had
    SETTLE_JS = ("(() => ({ready: document.readyState, len: document.documentElement."
                 "outerHTML.length, title: document.title || '', fetches: performance."
                 "getEntriesByType('resource').filter(e => e.initiatorType === 'fetch' || "
                 "e.initiatorType === 'xmlhttprequest').length}))()")

    async def _own_fetches(self, page) -> Optional[Tuple[int, int]]:
        """(in flight, finished) XHR / fetch requests on the page's own site,
        from the session's network log (no bodies fetched); None when the
        session keeps no log."""
        net = getattr(page, "network", None)
        if net is None:
            return None
        try:
            try:
                rows = await net(want_bodies=False)
            except TypeError:
                rows = await net()
            here = await page.url()
        except Exception:
            return None
        live = done = 0
        for r in rows or []:
            if str(r.get("resource_type") or "").lower() not in ("xhr", "fetch"):
                continue
            if not _own_site(here, str(r.get("url") or "")):
                continue
            if r.get("t_end") is None and r.get("status") in (None, 0):
                live += 1
            else:
                done += 1
        return live, done

    async def _settle(self, page, t0: float) -> None:
        """Wait for a complete, still, titled document whose title text and own
        fetches have settled (``title_stable_ms``, ``quiet_ms``), never past
        ``ceiling_s``; a session without ``js`` just waits."""
        js = getattr(page, "js", None)
        if js is None:
            if self.extra_settle_s > 0:
                await page.settle(self.extra_settle_s)
            return
        now = self.clock
        last, since = -1, None
        title_last, title_since = object(), None
        fetch_last, quiet_since = object(), None
        while now() - t0 < self.ceiling_s:
            try:
                st = await js(self.SETTLE_JS) or {}
            except Exception:
                break
            length = int(st.get("len") or 0)
            if last >= 0 and abs(length - last) <= max(8, last // 500):
                since = since if since is not None else now()
            else:
                since = None
            last = length
            title = st.get("title")
            if title != title_last:
                title_last, title_since = title, now()
            titled = (len(title) if isinstance(title, str) else int(title or 0)) > 0
            own = await self._own_fetches(page) if self.quiet_ms > 0 else None
            sig = (st.get("fetches"), own[1] if own else None)
            if own is not None and own[0] > 0:
                fetch_last, quiet_since = sig, None          # an own fetch is in flight
            elif sig != fetch_last or quiet_since is None:
                fetch_last, quiet_since = sig, now()
            if (st.get("ready") == "complete" and since is not None
                    and (now() - since) * 1000 >= self.stable_ms and titled
                    and (now() - title_since) * 1000 >= self.title_stable_ms
                    and quiet_since is not None
                    and (now() - quiet_since) * 1000 >= self.quiet_ms):
                return
            await page.settle(0.25)


def _own_site(page_url: str, url: str) -> bool:
    """Is ``url`` on the page's own site (the same host, or the same last two
    host labels: ``api.example.com`` for ``www.example.com``)? Crude on
    two-label public suffixes; there it counts MORE fetches as the page's own,
    which only makes the wait longer (never past the hard cap)."""
    a = (urllib.parse.urlsplit(page_url).hostname or "").lower()
    b = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not a or not b:
        return False
    return a == b or a.split(".")[-2:] == b.split(".")[-2:]


# ---------------------------------------------------------------------------
# the escalation loop
# ---------------------------------------------------------------------------
#
# ESCALATE ONLY ON A DETECTED FAILURE TO PROGRESS, NEVER ON A SCHEDULE. The
# triggers are deterministic and no model decides the escalation. The policy:
#
# HARD — escalate at once, no repair round:
#   (a) ``policy``: the run ended on the gate's refusal (a robots / off-site
#       navigation was refused and the page left is not a site page);
#   (b) ``abstain``: typed ``abstain``, or ``done`` with a cannot/blocked reason;
#   (c) ``no_progress``: URL (no fragment) + title + element-table hash
#       unchanged for two consecutive steps, or the run never left the state
#       it started in;
#   (d) ``provider_unreachable`` / ``provider_empty`` / ``model_unreadable``:
#       STOP — an outage, or a model that did not answer in protocol; never a
#       verdict, never escalated.
# SOFT — the page-kind check says "not there yet" but the state changed: ONE
# repair round with the check's reason fed back, then escalate (``goal``).
# A level's cap ends it (``cap``). A blocked page (challenge / login / consent
# / 403) ends the loop at once — never escalated, never re-hit.

ESCALATION_ENV = "ANYUSE_AGENT_ESCALATION"
NO_PROGRESS_STEPS = 2
#: repair rounds a SOFT failure gets before the level escalates
SOFT_REPAIR_ROUNDS = 1
#: the stops that mean "the provider did not answer" (or did not answer in
#: protocol, twice): never a verdict
#: the browser (or its container) stopped answering (execution/browser_supervisor.py)
INFRASTRUCTURE_STOP = "infrastructure"
PROVIDER_STOPS = ("provider_unreachable", "provider_empty", "model_unreadable",
                  "budget_exhausted")
_ABSTAIN_VERBS = ("abstain", "blocked", "unknown", "cannot", "give_up", "fail", "stuck")
_ABSTAIN_WORDS = ("cannot", "can't", "can not", "unable", "blocked", "unknown",
                  "not possible", "impossible", "no way to", "not available")


def escalation_enabled() -> bool:
    """``ANYUSE_AGENT_ESCALATION=on`` — default off until measured."""
    from anyuse.settings import get as get_setting
    return bool(get_setting(ESCALATION_ENV))


def page_state(obs: dict) -> str:
    """URL without fragment + title + element table + visible text, hashed
    (trigger (c)). The visible text is part of the state, so in-page
    results that appear without a URL change count as progress."""
    import hashlib
    url = str(obs.get("url") or "").split("#", 1)[0]
    table = [(e.get("tag"), e.get("type"), e.get("name"), e.get("href"))
             for e in (obs.get("elements") or [])]
    blob = json.dumps([url, str(obs.get("title") or ""), table,
                       str(obs.get("text") or "")[:2500]], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def is_abstention(action: dict) -> bool:
    """Trigger (b): a typed abstention, or ``done`` that says it could not."""
    kind = str(action.get("action", "")).strip().lower()
    if kind in _ABSTAIN_VERBS:
        return True
    if kind == "done":
        why = " ".join(str(action.get(k) or "") for k in ("reason", "answer", "note")).lower()
        return any(w in why for w in _ABSTAIN_WORDS)
    return False


def page_kind_check(run: AgentRun) -> Tuple[bool, str]:
    """Trigger (a), reference-free: did the run end on a page FOR this value?

    Deterministic and available without a recording or a person: the run
    captured a page, on the start site, that is not the start page, and whose
    URL or title names every word of the value. It is deliberately a floor —
    a wrong page that names the value passes it, which is exactly what the
    person's single confirmation (and, in the bench, the oracle) is for. The
    reason text describes only the agent's OWN page, so feeding it back to the
    model leaks nothing about the answer.
    """
    if not run.dom or run.error:
        return False, f"no page was captured ({run.error or 'empty DOM'})"
    final = run.final_url or ""
    if not same_site(run.start_url, final):
        return False, f"the run ended off the start site, on {final[:80]!r}"
    norm = lambda u: urllib.parse.unquote_plus(u).split("#", 1)[0].rstrip("/")
    title_m = re.search(r"<title[^>]*>(.*?)</title>", run.dom, re.I | re.S)
    title = re.sub(r"\s+", " ", (title_m.group(1) if title_m else "")).strip()
    if norm(final) == norm(run.start_url):
        return False, (f"the run ended on the start page ({final[:80]!r}, title "
                       f"{title[:60]!r}); the requested page was not opened")
    from anyuse.execution.api_compile import contains_words, normalise
    want = normalise(run.value).split()
    have = " ".join(t for t in (normalise(urllib.parse.unquote_plus(final)), normalise(title)) if t)
    missing = [w for w in want if not contains_words(have, w)]
    if not want or missing:
        return False, (f"the page {final[:80]!r} (title {title[:60]!r}) does not name "
                       f"{run.value!r} in its URL or title, so it is not a page for it")
    return True, "the page is on the site, is not the start page, and names the value"


@dataclass
class EscalationLevel:
    """One rung inside the agent tier. Caps are per task-run, per level."""
    name: str
    role: str
    #: attempts per level; the policy allows at most 1 + SOFT_REPAIR_ROUNDS
    rounds: int = 1 + SOFT_REPAIR_ROUNDS
    max_calls: int = 30
    max_seconds: float = 300.0
    max_usd: float = 0.10
    vision: bool = False
    #: completion budget per call. It must cover a reasoning model's hidden
    #: reasoning: gpt-oss spent L1's old 200 on reasoning and answered empty
    #: (44 L1 attempts); see docs/MODEL_CONFIGURATION.md
    max_tokens: int = 1000
    #: list prices US$/1M tokens, used only when the provider reports no cost
    price_in: float = PRICE_IN
    price_out: float = PRICE_OUT
    model: str = ""

    def usd(self, tokens_in: int, tokens_out: int, reported: float = 0.0) -> float:
        if reported:
            return reported
        return tokens_in / 1e6 * self.price_in + tokens_out / 1e6 * self.price_out


def default_levels() -> List[EscalationLevel]:
    """L1 cheap text agent -> L2 stronger text model -> L3 vision. Roles resolve
    through the adapter (``ANYUSE_AGENT_L2_MODEL_NAME`` etc. override)."""
    return [
        EscalationLevel("L1", "agent", max_usd=0.02, max_seconds=240.0, max_tokens=1000),
        EscalationLevel("L2", "agent_l2", max_usd=0.06, max_tokens=600,
                        price_in=2.0, price_out=10.0),
        EscalationLevel("L3", "agent_l3", max_usd=0.06, vision=True, max_tokens=2000,
                        price_in=0.75, price_out=3.75, max_seconds=360.0),
    ]


def ended_on_refusal(run: AgentRun) -> bool:
    """Trigger (a) ``policy``: the gate's refusal is where the run ended.

    A refused navigation leaves the tab on the browser's own error page
    (``chrome-error://``) or on nothing at all; a run that was refused once and
    then carried on to a site page is NOT this (it is judged by its page).
    """
    if not run.refused:
        return False
    final = str(run.final_url or "").lower()
    return not final.startswith(("http://", "https://"))


def classify_failure(run: AgentRun) -> Tuple[str, bool]:
    """``(trigger, soft)`` for an attempt whose goal check failed.

    Hard triggers escalate at once; the one soft case (state changed, page not
    there yet) earns ONE repair round. Provider stops are handled before this.
    """
    if ended_on_refusal(run):
        return "policy", False
    if run.stop == "abstain":
        return "abstain", False
    if run.stop == "no_progress" or not run.state_changed:
        return "no_progress", False
    return "goal", True


class EscalatingAgent:
    """The agent tier with its escalation loop. Same ``run`` contract as
    :class:`StepAgent`, so ``learn_loop.propose`` takes it unchanged.

    Per level: a HARD failure — the gate's refusal (a), an
    abstention (b), no progress (c) — escalates at once; the one SOFT failure
    (the state changed but ``goal_check`` says "not there yet") gets exactly
    one repair round with the check's reason fed back, then escalates
    (``goal``). A provider outage or an empty answer (d) stops the loop and is
    never a verdict. Every escalation is logged with its trigger. ``brains`` maps a level name to a brain callable
    (tests inject fakes; by default an adapter-backed brain per role).
    """

    def __init__(self, levels: Optional[Sequence[EscalationLevel]] = None, *,
                 endpoint: str = "", session_factory: Optional[Callable] = None,
                 allow_url: Optional[Callable] = None,
                 goal_check: Optional[Callable] = None,
                 brains: Optional[Dict[str, Callable]] = None,
                 budget_left: Optional[Callable[[], float]] = None,
                 run_budget: Optional[RunBudget] = None,
                 related_origins=(), max_steps: int = MAX_STEPS,
                 pause_s: float = MIN_INTERVAL_S, blocked_check: Optional[Callable] = None,
                 on_attempt: Optional[Callable[[dict], None]] = None,
                 live_solver=None, origin: Optional[str] = None, task_id: str = ""):
        self.levels = list(levels or default_levels())
        self.live_solver, self.origin, self.task_id = live_solver, origin, task_id
        self.endpoint = endpoint
        self._factory = session_factory
        self.allow_url = allow_url
        self.goal_check = goal_check or page_kind_check
        self._brains = dict(brains or {})
        self.budget_left = budget_left
        self.run_budget = run_budget or RunBudget.from_env()
        self.related_origins = tuple(related_origins or ())
        self.max_steps = max_steps
        self.pause_s = pause_s
        self.blocked_check = blocked_check or _blocked_reason
        self.on_attempt = on_attempt
        self.refused: List[str] = []
        self._credentials_logged = False

    def _available_levels(self) -> List[EscalationLevel]:
        available, pruned = [], []
        for level in self.levels:
            if level.name in self._brains:
                available.append(level)
                continue
            config = resolve_model_config(
                level.role, max_tokens=level.max_tokens,
                **({"model": level.model} if level.model else {}))
            if credential_status(config) == "unset":
                pruned.append(level.name)
            else:
                available.append(level)
        if pruned and not self._credentials_logged:
            logger.info("agent escalation pruned unavailable credential levels once: %s",
                        ", ".join(pruned))
            self._credentials_logged = True
        return available

    def brain_for(self, level: EscalationLevel) -> Callable:
        if level.name not in self._brains:
            self._brains[level.name] = CerebrasBrain(
                role=level.role, max_tokens=level.max_tokens,
                run_budget=self.run_budget, price_in=level.price_in,
                price_out=level.price_out,
                **({"model": level.model} if level.model else {}))
        brain = self._brains[level.name]
        adapter = getattr(brain, "_adapter", None)
        if adapter is not None and hasattr(adapter, "set_budget"):
            adapter.set_budget(self.run_budget, price_in=level.price_in,
                               price_out=level.price_out)
        return brain

    def run(self, task: str, start_url: str, value: str) -> AgentRun:
        t0 = time.monotonic()
        levels = self._available_levels()
        log = {"levels": [lv.name for lv in levels],
               "pruned_levels": [lv.name for lv in self.levels if lv not in levels],
               "attempts": [],
               "escalations": [], "solved_at": None, "stopped": ""}
        if not levels:
            log["stopped"] = "no configured model credentials"
        final: Optional[AgentRun] = None
        totals = {"calls": 0, "cached": 0, "tin": 0, "tout": 0, "usd": 0.0}
        #: the repair loop's memory: what earlier attempts ended on and why the
        #: check refused it (the agent's OWN pages only; nothing from a reference)
        feedback: List[dict] = []
        for li, level in enumerate(levels):
            used = {"calls": 0, "usd": 0.0, "seconds": 0.0}
            trigger, why = "goal", ""
            attempts_allowed = max(1, min(level.rounds, 1 + SOFT_REPAIR_ROUNDS))
            for rnd in range(attempts_allowed):
                if self.run_budget.remaining_usd <= 0 or (
                        self.budget_left is not None and self.budget_left() <= 0):
                    log["stopped"] = "global budget exhausted"
                    break
                calls_left = level.max_calls - used["calls"]
                secs_left = level.max_seconds - used["seconds"]
                if calls_left <= 0 or secs_left <= 0 or used["usd"] >= level.max_usd:
                    trigger, why = "cap", (f"level cap reached ({used['calls']} calls, "
                                         f"{used['seconds']:.0f} s, ${used['usd']:.4f})")
                    break
                if final is not None and self.pause_s:
                    time.sleep(self.pause_s)          # <= 1 navigation/s per host
                agent = StepAgent(self.brain_for(level), session_factory=self._factory,
                                  endpoint=self.endpoint, max_steps=self.max_steps,
                                  allow_url=self.allow_url,
                                  related_origins=self.related_origins,
                                  system=SYSTEM_ESCALATION, vision=level.vision,
                                  no_progress_steps=NO_PROGRESS_STEPS,
                                  max_calls=calls_left, deadline_s=secs_left,
                                  live_solver=self.live_solver, origin=self.origin,
                                  task_id=self.task_id)
                run = agent.run(task, start_url, value, feedback=feedback[-3:] or None)
                self.refused += agent.refused
                usd = level.usd(run.tokens_in, run.tokens_out, run.cost_usd)
                used["calls"] += run.model_calls
                used["usd"] += usd
                used["seconds"] += run.seconds
                totals["calls"] += run.model_calls
                totals["cached"] += run.cached_calls
                totals["tin"] += run.tokens_in
                totals["tout"] += run.tokens_out
                totals["usd"] += usd
                final = run
                blocked = self.blocked_check(run)
                ok, reason = (False, blocked) if blocked else self.goal_check(run)
                attempt = {"level": level.name, "round": rnd + 1, "role": level.role,
                           "model": getattr(self.brain_for(level), "model", ""),
                           "stop": run.stop, "goal_ok": ok, "reason": reason[:200],
                           "state_changed": run.state_changed,
                           "empty_retries": run.empty_retries,
                           "unreadable_retries": run.unreadable_retries,
                           "refused": len(run.refused),
                           "final_url": (run.final_url or "")[:160],
                           "calls": run.model_calls, "tokens_in": run.tokens_in,
                           "tokens_out": run.tokens_out, "usd": round(usd, 6),
                           "seconds": round(run.seconds, 2), "error": run.error[:120],
                           "step_error": next((str(x.get("error"))[:120] for x in run.steps
                                               if isinstance(x, dict) and x.get("error")), "")}
                log["attempts"].append(attempt)
                if self.on_attempt:
                    self.on_attempt(attempt)
                if run.stop in PROVIDER_STOPS:
                    # (d): an outage or an empty answer is never a verdict
                    log["stopped"] = run.stop
                    break
                if run.stop == INFRASTRUCTURE_STOP:
                    # the browser stopped answering: never a verdict, never
                    # escalated (a dead browser is not a harder task)
                    log["stopped"] = INFRASTRUCTURE_STOP
                    break
                if blocked:
                    log["stopped"] = f"blocked: {blocked}"[:160]
                    break
                if ok:
                    log["solved_at"] = level.name
                    break
                trigger, soft = classify_failure(run)
                attempt["trigger"] = trigger
                if trigger == "policy":
                    why = ("the gate refused the navigation the run ended on: "
                           + ", ".join(u[:80] for u in run.refused[:2]))
                elif trigger == "abstain":
                    why = "the model abstained: " + json.dumps(
                        run.steps[-1] if run.steps else {})[:120]
                elif trigger == "no_progress":
                    why = (f"page state unchanged for {NO_PROGRESS_STEPS} consecutive steps"
                           if run.stop == "no_progress" else
                           f"the run never left the page it started on ({reason[:120]})")
                else:
                    why = reason
                if not soft:
                    break
                # the one repair round: the check's reason goes back to the model
                feedback.append({"ended_on": (run.final_url or "")[:160],
                                 "check": reason[:240]})
            if log["solved_at"] or log["stopped"]:
                break
            nxt = levels[li + 1].name if li + 1 < len(levels) else "human"
            esc = {"from": level.name, "to": nxt, "trigger": trigger, "reason": why[:200]}
            log["escalations"].append(esc)
            logger.info("agent escalation %s -> %s, trigger (%s): %s",
                        level.name, nxt, trigger, why[:120])
            if final is not None and (not feedback or feedback[-1]["check"] != why[:240]):
                feedback.append({"ended_on": (final.final_url or "")[:160],
                                 "check": why[:240]})
        out = final or AgentRun(value=value, task=task_text(task, value),
                                start_url=start_url, error="no attempt was made")
        out.model_calls, out.cached_calls = totals["calls"], totals["cached"]
        out.tokens_in, out.tokens_out = totals["tin"], totals["tout"]
        out.cost_usd = round(totals["usd"], 6)
        out.seconds = time.monotonic() - t0
        log["level"] = log["solved_at"] or (log["attempts"][-1]["level"]
                                             if log["attempts"] else "")
        out.escalation = log
        log["run_budget"] = self.run_budget.to_dict()
        if not log["solved_at"]:
            log["triage"] = _triage_unsolved(log, out, [lv.name for lv in levels])
        return out


def _triage_unsolved(log: dict, run: AgentRun, level_names: List[str]) -> dict:
    """Who can fix an unsolved run. Rules only, no ranker:
    the loop above already spent its model budget, and this never opens a
    socket. The decision is recorded; it does not change the loop."""
    try:
        from anyuse.execution.triage import StuckState, decide
        last = log["attempts"][-1] if log["attempts"] else {}
        stopped = str(log.get("stopped") or "")
        blocked_reason = ""
        if stopped.startswith("blocked: "):
            blocked_reason = stopped[len("blocked: "):].split(":", 1)[0].strip()
        stop = str(last.get("stop") or run.stop or "")
        if stopped == "global budget exhausted":
            stop = "budget_exhausted"
        elif stopped == "no configured model credentials":
            stop = "provider_unreachable"
        lv = str(last.get("level") or "")
        remaining = (len(level_names) - level_names.index(lv) - 1
                     if lv in level_names else 0)
        state = StuckState.from_agent_run(run, remaining if not stopped else 0,
                                          blocked_reason=blocked_reason,
                                          trigger=str(last.get("trigger") or ""))
        state.stop = stop
        return decide(state).to_dict()
    except Exception as exc:              # triage never breaks a run
        return {"outcome": "", "error": type(exc).__name__}


def _blocked_reason(run: AgentRun) -> str:
    """A challenge / login / consent wall / 403 — the loop stops, never escalates."""
    try:
        from anyuse.execution.blocked import detect_blocked_ui
        from anyuse.execution.learn_loop import preview_items
        # the rendered list downgrades a SOFT wall (a consent banner over content
        # that arrived), exactly as on the ladder; a hard block stays a block
        items = preview_items(run.dom)[1] if run.dom else []
        state = detect_blocked_ui(run.text or "", "", run.final_url or "",
                                  run.network or [], items=items)
        if state.blocked:
            return f"{state.reason}: {'; '.join(state.evidence)[:100]}"
    except Exception:
        pass
    return ""


def make_agent(endpoint: str, allow_url: Optional[Callable] = None, **kw):
    """The agent tier as configured: escalation loop iff ANYUSE_AGENT_ESCALATION=on."""
    run_budget = kw.pop("run_budget", None) or RunBudget.from_env()
    if escalation_enabled():
        return EscalatingAgent(endpoint=endpoint, allow_url=allow_url,
                               run_budget=run_budget, **kw)
    return StepAgent(brain=CerebrasBrain(run_budget=run_budget), endpoint=endpoint,
                     allow_url=allow_url, **kw)
