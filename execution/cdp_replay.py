"""The deterministic browser replay tier — rung 2 of the ladder.

Rung 1 is a proved HTTP request: milliseconds, no browser, no model. It only
exists for a workflow where some single request explained the whole screen, and
milestone 4 measured that at 5 of 33. Rung 3 is a step agent: seconds to
minutes, model calls, money, and a success rate nobody should assume.

Between them sits the thing this module makes into a product: **replay the
recorded navigation in a headless browser with fresh inputs, deterministically,
with zero model calls.** It is slower than a compiled request by two orders of
magnitude and faster than an agent by one, and unlike either it works whenever
the page can be reached with the input in its URL.

Where the milestones-1-4 "UI path" numbers came from
----------------------------------------------------
the measurement harness measured them: open the URL in the container browser
over CDP, wait for the pre-registered result selector, stop the clock. That is
bench code, not a product path — there was no way for a *user* to run it, no
postcondition, no blocked handling and no failure contract. This module is the
same measurement turned into a rung with all four.

Deliberate limits
-----------------
* **Container only.** `ANYUSE_CDP_ENDPOINT` must name a DevTools endpoint;
  nothing here launches a browser, and in particular nothing launches one on
  the user's desktop. Unset → the rung refuses and says so. There is no default
  and no auto-discovery: a replay tier that quietly drives whatever Chrome it
  can find is a desktop actuator wearing a web actuator's clothes.
* **Navigation only.** It opens a URL and reads the resulting DOM. It does not
  click, type, scroll, upload, or run page-supplied script beyond what loading
  the page runs. A workflow that needs interaction is not this rung's job.
* **Zero model calls, counted.** The executor counts them and returns the
  count. It is 0 except for an empty answer the site's own pages do not
  settle on a product call, which may ask the configured check model
  (``result_contract.empty_verdict``, execution/model_check.py); a test pins both.
* **The robots policy applies**, exactly as it does to the api rung.
* **Honest failure.** Zero extracted items is a FAILURE unless the site itself
  says there are no results for this query (``result_contract.empty_verdict``):
  the page is the site's own empty page for this input (learned from searches
  for made-up inputs, execution/empty_page.py), the check model reads one of its
  lines naming the input as saying so, or a listed-language statement names
  the query in its own line, under a heading that names it, or a few lines
  below a search summary that names it (``result_contract.empty_evidence_page``).
  "It worked, here are no results" is the single most damaging thing an
  automation layer can say.
* **The compiled rung's contract.** A template that carries the runtime result
  contract (``expect.result_contract``, set by :func:`template_from_recording`)
  accepts a 1-2 item page only when every item's key names the query, as the
  compiled request does. A template stored without one keeps its count floor.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: DevTools endpoint, e.g. ``http://192.168.64.42:9222``. No default on purpose.
ENDPOINT_ENV = "ANYUSE_CDP_ENDPOINT"
#: How long to let a page settle after `load` before reading the DOM.
DEFAULT_SETTLE_S = 4.0
DEFAULT_NAV_TIMEOUT_S = 45.0
#: Minimum seconds between two loads of the same host.
MIN_INTERVAL_S = 1.0

#: Readiness: instead of waiting out a fixed window, the page is read when the
#: evidence FROM THE RECORDING is there (see :func:`readiness_from_recording`).
#: It never extends the wait: the ceiling is exactly the window the timer
#: uses, min(settle_s + 6, timeout_s), 10 s for the defaults, so readiness can
#: only finish earlier, and a ready read that extracts too little is read
#: again at the ceiling. tests/bench/readiness_bench.py measures both.
DEFAULT_STABLE_MS = 400
#: Readiness is on by default: a template built from a recording without an
#: explicit choice (``readiness=None``) gets it, with the timer as its cap and
#: fallback; ANYUSE_TIER2_READINESS=off builds such templates on the fixed
#: timer alone. The setting is read when the template is built, so a stored
#: template keeps what it was built with until the tool is compiled again.
READINESS_ENV = "ANYUSE_TIER2_READINESS"
#: the stored readiness rule (``readiness["rule"]``): the replay waits for the search's
#: own data calls that carry the input, then for the result rows to stand still
#: (:meth:`CdpDriver._await_value_calls`). A stored readiness without it keeps the
#: earlier wait (:meth:`CdpDriver._await_ready`).
READINESS_RULE = "value_calls"
#: the most requests of one pattern a ready read waits for
MAX_EXPLAINING_COUNT = 3
DEFAULT_POLL_MS = 150
#: The DOM is "stable" when its serialised length has not moved by more than
#: this fraction over the stability window. Exact equality is too strict: many
#: pages tick a clock or a lazy image into the markup forever.
DOM_DRIFT_TOLERANCE = 0.002

USER_AGENT_SUFFIX = "anyuse-compiled-workflow/0.1 (read-only replay; <=1 req/s)"

#: the most response bodies one replay keeps when asked for its traffic (recompile evidence)
MAX_CAPTURED_BODIES = 24

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

def _query_value(template: "ReplayTemplate", contract: Optional[Dict[str, Any]],
                 params: Optional[Dict[str, Any]]) -> str:
    """The value a "no results" statement must name: the contract's query
    parameter, else the template's first parameter, else the only one given."""
    params = params or {}
    name = str((contract or {}).get("query_param")
               or (list(template.params or []) or [""])[0])
    if name and name in params:
        return str(params[name])
    return str(next(iter(params.values()))) if len(params) == 1 else ""


def page_says_empty(markup: str, text: str, value: str) -> str:
    """The phrase with which a rendered page states it found nothing for ``value``, or ``""``:
    the replay's own empty evidence (``result_contract.empty_evidence_page``)."""
    from anyuse.execution import result_contract as RC
    return RC.empty_evidence_page(markup or "", value, text=text or "")[0]


def site_empty_check(template: "ReplayTemplate", ctx: Any = None
                     ) -> Optional[Callable[[str, str, str], str]]:
    """The ready read's empty check from the site's own empty page: the record's page for
    this template (``ctx.learned``, an ``empty_page.Context``), else the contract's pinned
    one. Pure: it sends no request and asks no model. A read that page does not settle
    falls back to :func:`page_says_empty`. ``None`` when no empty page was learned."""
    from anyuse.execution import empty_page as EP
    from anyuse.execution import result_contract as RC
    sig = getattr(ctx, "learned", None)
    if not EP.usable(sig):
        sig = ((RC.contract_of(template.expect) or {}).get("empty") or {}).get("page")
    if not EP.usable(sig) or sig.get("kind") != "html":
        return None

    def check(markup: str, text: str, value: str) -> str:
        ev = EP.match(sig, EP.Page("html", value, markup=markup or "", text=text or ""))
        if ev.state == EP.EMPTY:
            return ev.line or "the site's own empty page"
        if ev.state == EP.NOT_EMPTY:
            return ""
        return page_says_empty(markup, text, value)
    return check


def runtime_readiness(template: "ReplayTemplate", params: Optional[Dict[str, Any]],
                      empty: Optional[Callable[[str, str, str], str]] = None
                      ) -> Optional[Dict[str, Any]]:
    """The readiness one replay waits with: a copy of the stored dict plus what only the call
    knows, never stored. ``value`` is the input the search's own data calls carry
    (:meth:`CdpDriver._await_value_calls`); ``contract`` (when the template has one) and
    ``params`` let :func:`ready_read_passes` accept a short answer the contract allows and a
    page that states it found nothing. ``empty`` replaces :func:`page_says_empty` as that
    statement's check. ``None`` when the template has no readiness (the fixed timer)."""
    if not template.readiness:
        return None
    from anyuse.execution import result_contract as RC
    out = dict(template.readiness)
    contract = RC.contract_of(template.expect)
    out["value"] = _query_value(template, contract, params)
    out["params"] = dict(params or {})
    if contract is not None:
        out["contract"] = contract
    if empty is not None:
        out["empty"] = empty
    return out


def cdp_endpoint(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get(ENDPOINT_ENV) or "").strip()


def cdp_enabled(explicit: Optional[str] = None) -> bool:
    return bool(cdp_endpoint(explicit))


def readiness_default() -> bool:
    """Evidence-based settle unless ``ANYUSE_TIER2_READINESS`` is off."""
    from anyuse.settings import get as get_setting
    return bool(get_setting(READINESS_ENV))


# ---------------------------------------------------------------------------
# the template
# ---------------------------------------------------------------------------

@dataclass
class ReplayTemplate:
    """A recorded navigation, parameterised — the ``cdp`` rung's command.

    Deliberately the same shape of thing as :class:`api_step.RequestTemplate`:
    a URL template, the parameters it consumes, how to extract the answer, and
    a deterministic postcondition. What differs is that the extraction runs
    against the DOM a browser built, not against a response body.
    """
    url: str = ""                                  # may contain {param}
    params: List[str] = field(default_factory=list)
    #: name -> how many whole path segments this parameter occupies. Absent
    #: means one, encoded with no safe characters, exactly as before.
    path_params: Dict[str, int] = field(default_factory=dict)
    #: name -> deterministic transform applied before binding (``lower``,
    #: ``hyphen``, ``slug``); set only when every recording agreed on it.
    param_transforms: Dict[str, str] = field(default_factory=dict)
    #: name -> html_extract selector, run against the rendered DOM
    extract: Dict[str, str] = field(default_factory=dict)
    settle_s: float = DEFAULT_SETTLE_S
    timeout_s: float = DEFAULT_NAV_TIMEOUT_S
    #: deterministic postcondition: {"selector": ..., "min_items": n}
    expect: Dict[str, Any] = field(default_factory=dict)
    #: milestone-6 readiness evidence, see :class:`Readiness`. Empty means the
    #: fixed ``settle_s`` timer, which is what milestones 1–5 used.
    readiness: Dict[str, Any] = field(default_factory=dict)
    origin: str = "deterministic"
    notes: str = ""
    approved_origins: List[str] = field(default_factory=list)
    #: also read each matched row's name (html_extract.row_label) as ``labels``; the
    #: postcondition still counts the rows
    read_labels: bool = False
    #: the ground truth was read visible-only (a filter that hides rows instead of
    #: removing them), so the replay extracts the same way
    visible_only: bool = False

    def to_dict(self) -> dict:
        return {"url": self.url, "params": list(self.params),
                "path_params": dict(self.path_params),
                "param_transforms": dict(self.param_transforms),
                "extract": dict(self.extract), "settle_s": self.settle_s,
                "timeout_s": self.timeout_s, "expect": dict(self.expect),
                "readiness": dict(self.readiness),
                "approved_origins": list(self.approved_origins),
                "origin": self.origin, "notes": self.notes,
                "read_labels": bool(self.read_labels),
                "visible_only": bool(self.visible_only)}

    @classmethod
    def from_dict(cls, data: dict) -> "ReplayTemplate":
        data = data or {}
        return cls(url=str(data.get("url", "")),
                   params=list(data.get("params") or []),
                   path_params={str(k): int(v) for k, v
                                in (data.get("path_params") or {}).items()},
                   param_transforms={str(k): str(v) for k, v
                                     in (data.get("param_transforms") or {}).items()},
                   extract=dict(data.get("extract") or {}),
                   settle_s=float(data.get("settle_s", DEFAULT_SETTLE_S)),
                   timeout_s=float(data.get("timeout_s", DEFAULT_NAV_TIMEOUT_S)),
                   expect=dict(data.get("expect") or {}),
                   readiness=dict(data.get("readiness") or {}),
                   approved_origins=list(data.get("approved_origins") or []),
                   origin=str(data.get("origin", "deterministic")),
                   notes=str(data.get("notes", "")),
                   read_labels=bool(data.get("read_labels", False)),
                   visible_only=bool(data.get("visible_only", False)))

    def problems(self) -> List[str]:
        out: List[str] = []
        scheme = urllib.parse.urlsplit(self.url).scheme
        if scheme != "https":
            out.append(f"url scheme must be https (got {scheme or 'none'!r})")
        if not self.extract:
            out.append("no extraction selector: a replay that reads nothing "
                       "cannot be checked and must not be called a success")
        declared = set(self.params)
        url_placeholders = set(_PLACEHOLDER.findall(self.url or ""))
        for name in url_placeholders:
            if name not in declared:
                out.append(f"placeholder {{{name}}} has no declared parameter")
        for name, span in self.path_params.items():
            if name not in url_placeholders:
                out.append(f"path_params names {name!r}, which is not a placeholder "
                           f"in the url")
            if int(span) < 1:
                out.append(f"path_params[{name!r}] = {span}: a parameter cannot "
                           f"span fewer than one path segment")
        return out

    def render(self, params: Optional[Dict[str, Any]] = None) -> str:
        """Bind ``params`` into the URL.

        A value is percent-encoded with no safe characters, so it can never add
        a path segment, a query or a fragment. The one exception is a parameter
        the recordings proved spans several segments
        (:attr:`path_params`): it may contribute exactly that many, each still
        encoded with no safe characters, and a value of another shape is refused
        rather than reshaping the URL.
        """
        from anyuse.execution.param_binding import apply_transform, bind_path_value
        params = params or {}

        def sub(m):
            name = m.group(1)
            value = str(params[name])
            transform = self.param_transforms.get(name)
            if transform:
                value = apply_transform(transform, value)
            return bind_path_value(value, int(self.path_params.get(name, 1)))

        try:
            return _PLACEHOLDER.sub(sub, self.url)
        except KeyError as exc:
            raise KeyError(f"no value for parameter {exc.args[0]!r}") from exc


def template_from_recording(recording: dict, param_name: str,
                            selector: str = "",
                            recordings: Optional[List[dict]] = None,
                            readiness: Optional[bool] = None) -> "ReplayTemplate":
    """Build a replay template from a recording the sample-2 recorder produced.

    The URL template comes from the recorded URL with the typed value replaced
    by a placeholder; the selector is the one the recorder DERIVED as ground
    truth (`html_extract.derive_list_items`), so the rung reads exactly what the
    measurement compared against.

    ``recordings`` (milestone 6) is the whole set of recordings of DIFFERENT
    values for this workflow. When given, and only when the plain
    whole-segment / whole-query binding finds nothing, the awkward carriers are
    considered — a value spanning several path segments, or a delimited part of
    one — and only when **every** recording corroborates the same site. This is
    what turns `packagist.org/packages/guzzlehttp/guzzle` from "tier 2 refused"
    into a replayable navigation.

    ``readiness`` attaches the evidence-based settle described in
    :func:`readiness_from_recording`, with the fixed timer as its cap and
    fallback; its item floor is the count floor the postcondition enforces.
    ``None`` means :func:`readiness_default`: on unless
    `ANYUSE_TIER2_READINESS=off`.
    """
    from anyuse.execution.api_compile import normalise

    if readiness is None:
        readiness = readiness_default()
    value = (recording.get("param") or {}).get("value", "")
    url = str(recording.get("final_url") or "")
    selector = selector or str(recording.get("ground_truth_selector") or "")
    sp = urllib.parse.urlsplit(url)
    want = normalise(value)

    segs = [urllib.parse.unquote(s) for s in sp.path.split("/")]
    segs = ["{%s}" % param_name if s and normalise(s) == want else s for s in segs]
    query = []
    bound = any(s == "{%s}" % param_name for s in segs)
    for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
        if normalise(val) == want:
            query.append((key, "{%s}" % param_name))
            bound = True
        else:
            query.append((key, val))

    path_params: Dict[str, int] = {}
    transforms: Dict[str, str] = {}
    how = "the typed value is a whole path segment or query value"
    if not bound:
        tmpl_url, path_params, transforms, how = _evidence_url(
            url, value, param_name, recordings)
        if tmpl_url is None:
            return ReplayTemplate(notes=(
                f"the typed value {value!r} is not a whole path segment or query "
                f"value of {url[:120]!r}, and no binding site was corroborated by "
                f"every recording ({how}), so this navigation cannot be re-bound "
                f"to a new input"))
    else:
        qs = "&".join(f"{urllib.parse.quote(k)}="
                      f"{v if v.startswith('{') else urllib.parse.quote(v)}"
                      for k, v in query)
        tmpl_url = (urllib.parse.urlunsplit(
            (sp.scheme, sp.netloc, "/".join(segs), "", "")) + (f"?{qs}" if qs else ""))

    recorded_items = [str(item) for item in ((recording.get("ui") or {}).get("items") or [])
                      if str(item).strip()]
    identity = {"final_url": "rendered_url"}
    declared_identity = dict(recording.get("postcondition_identity") or {})
    if declared_identity.get("parameter") == param_name:
        identity.update({"parameter": param_name, "relation": "normalized_match"})
    recorded_min = max(1, len(recorded_items))
    # A result count is a property of ONE input. When recordings of other inputs
    # are given, the floor is the smallest count any of them showed; a count
    # observed for one query is not a floor for another (the ladder re-proof
    # failed 5 tier-2 replays with 29 < 30, 4 < 8, 3 < 5 on correct pages).
    other_counts = [len([i for i in ((r.get("ui") or {}).get("items") or []) if str(i).strip()])
                    for r in (recordings or [])]
    other_counts = [n for n in other_counts if n > 0]
    if other_counts:
        recorded_min = max(1, min([recorded_min, *other_counts]))
    visible_only = bool(recording.get("ground_truth_visible_only"))
    ready = readiness_from_recording(recording, selector) if readiness else {}
    if ready:
        # the floor the executor enforces below, so a ready read is never taken
        # on fewer rows than the postcondition then requires
        ready["min_items"] = recorded_min
        if visible_only:
            # the executor counts only the rows on screen, so the ready read does too
            ready["visible_only"] = True
    from anyuse.execution import result_contract as RC
    return ReplayTemplate(
        url=tmpl_url,
        params=[param_name],
        path_params=path_params,
        param_transforms=transforms,
        extract={"items": selector},
        # the compiled request's runtime contract (result_contract), so this rung
        # never accepts a page the compiled rung would refuse
        expect={"selector": selector, "min_items": recorded_min,
                "identity": identity, RC.CONTRACT_KEY: RC.replay_contract(param_name)},
        readiness=ready,
        visible_only=visible_only,
        notes=("deterministic browser replay of the recorded navigation; "
               "extraction uses the selector the recorder derived as ground "
               "truth, so the rung reads what the measurement compared. "
               f"Parameter binding: {how}."))


def for_validation(template: "ReplayTemplate") -> "ReplayTemplate":
    """A copy of ``template`` for the proof: the count floor is relaxed to 1.

    The same rule tier 1 follows (``api_verify.verify_template``): during the
    proof the comparison with the validation page's own list IS the count
    check, and enforcing the compile page's count first would reject a valid
    input that genuinely has fewer results before the comparison could run.
    An empty extraction still fails unless the page says it found nothing.
    """
    import copy
    out = copy.deepcopy(template)
    if "min_items" in (out.expect or {}):
        out.expect["min_items"] = 1
    return out


def finalize_minimum(template: "ReplayTemplate", counts: Sequence[int]) -> None:
    """Bind the persisted floor to the smallest independently validated count."""
    counts = [int(n) for n in counts if int(n) > 0]
    if counts and "min_items" in (template.expect or {}):
        template.expect["min_items"] = min(int(template.expect["min_items"]), min(counts))
        if "min_items" in (template.readiness or {}):
            template.readiness["min_items"] = min(int(template.readiness["min_items"]),
                                                  int(template.expect["min_items"]))


def readiness_from_recording(recording: dict, selector: str,
                             stable_ms: int = DEFAULT_STABLE_MS) -> Dict[str, Any]:
    """What this page looked like when it had finished answering, last time.

    The rule (``rule``, :data:`READINESS_RULE`) is read from the recording, and
    none of it is a duration. The responses that **explained the visible list**
    give the request patterns (``explaining``: host plus the constant part of the
    path, because the parameter's own value differs on a replay). Per pattern,
    ``value_calls`` is how many batches of the search's own data calls the page
    had to answer (:func:`_value_calls`): calls that carry the whole input, come
    after the result document and bring rows no earlier call brought. A replay is
    read once every such call it sent has answered, as many of each pattern as
    the recording needed (a list loaded in two batches is not ready after the
    first), and the result rows (their count and their text) have stood still
    for ``stable_ms``. Calls the person's typing made (a suggestion list, a
    preview before Enter) are not waited for, and nor is a request that does not
    carry the input (a long poll, a beacon).

    The recorded count (``min_items``) is the check on that first read
    (:func:`ready_read_passes`), not a condition of the wait. ``explaining_min``
    (requests per pattern, whatever they carry) is kept for a pattern whose calls
    never carry the input, for a replay whose calls carry it in a form the rule
    does not know, and for templates stored before ``rule``, which wait for it
    and for the recorded count.

    The wait is still bounded by exactly the window the fixed timer would have
    used, so this can make a replay finish sooner and can never make it take
    longer. If the evidence never arrives the replay falls through to reading
    the page at the ceiling — the same instant the timer would have read it —
    and says so, rather than failing on a condition the timer never had.
    """
    items = [str(x) for x in ((recording.get("ui") or {}).get("items") or [])]
    recorded_min = max(1, len([item for item in items if item.strip()]))
    patterns = _explaining_patterns(recording, items)
    out = {"selector": selector,
           "rule": READINESS_RULE,
           "explaining": patterns,
           "explaining_min": _explaining_counts(recording, patterns),
           "value_calls": _value_calls(recording, patterns),
           "stable_ms": int(stable_ms),
           "poll_ms": DEFAULT_POLL_MS,
           "min_items": recorded_min,
           # an incomplete ready read is read again at the timer's instant —
           # one navigation, no second request
           "fallback": "timer"}
    if recording.get("ground_truth_visible_only"):
        # the recorded count is of the rows the page showed: rows a filter
        # still hides do not count towards it
        out["visible_only"] = True
    return out


def ready_read_passes(dom: str, readiness: Dict[str, Any], text: str = "",
                      empty: Optional[Callable[[str, str, str], str]] = None) -> Tuple[bool, str]:
    """Did the page read at ``ready`` actually carry the list the rung extracts?

    The readiness conditions are about the page's calls having answered and the
    DOM standing still; this is about the extraction itself. A ready read that
    extracts fewer values than the recording is not accepted on readiness's
    word — the replay waits out the rest of the timer's window and reads again.
    ``visible_only`` counts only the rows the markup does not hide, as the
    executor does for such a template.

    A replay's readiness (:func:`runtime_readiness`) also carries the call's
    ``params`` and, for a template with one, its result ``contract``. With both,
    a short read is accepted as the executor would accept it: one or two rows
    whose names all name the input (``result_contract.check_html``'s sparse
    rule), or no row on a page that states it found nothing for the input.
    ``empty(markup, text, value)`` gives that statement (default
    ``readiness["empty"]``, else :func:`page_says_empty`); ``text`` is what the
    browser shows.
    """
    from anyuse.execution.html_extract import extract
    selector = str(readiness.get("selector") or "")
    if not selector:
        return True, "no selector to check"
    need = max(1, int(readiness.get("min_items") or 1))
    visible = bool(readiness.get("visible_only"))
    try:
        got = [x for x in extract(dom or "", selector, limit=max(20, need),
                                  visible_only=visible)
               if str(x).strip()]
    except Exception as exc:
        return False, f"extraction raised {type(exc).__name__}"
    if len(got) >= need:
        return True, f"{len(got)} item(s) extracted at the ready read"
    contract, params = readiness.get("contract"), readiness.get("params")
    if isinstance(contract, dict) and isinstance(params, dict):
        from anyuse.execution import result_contract as RC
        value = str(readiness.get("value") or "")
        if not got and value:
            check = empty or readiness.get("empty") or page_says_empty
            try:
                phrase = str(check(dom or "", text or "", value) or "")
            except Exception:                     # noqa: BLE001 - no statement, read again
                phrase = ""
            if phrase:
                return True, (f"no item extracted at the ready read; the page says {phrase!r} "
                              f"for {value!r}")
        elif 0 < len(got) < RC.MIN_LIST:
            verdict = RC.check_html(contract, dom or "", selector, params, got,
                                    visible_only=visible)
            if verdict.ok and verdict.kind == "sparse":
                return True, f"{len(got)} item(s) extracted at the ready read: {verdict.reason}"
    return False, f"{len(got)} item(s) extracted at the ready read, {need} needed"


def _explaining_patterns(recording: dict, items: List[str]) -> List[List[str]]:
    """``[[host, path-prefix], ...]`` for the responses that carried the answer.

    The document itself always counts. Beyond it, a response counts when its
    body actually contains one of the values the recorder saw on screen — which
    is the same evidence the compiler scores candidates with, reused here rather
    than a fresh guess about which XHR mattered. The stored path is truncated
    before the first segment carrying the typed value, so the pattern still
    matches when the replay asks for a different item.
    """
    from anyuse.execution.api_compile import normalise
    raw = str(((recording.get("param") or {}).get("value")) or "")
    # A value may occupy several segments (``guzzlehttp/guzzle``), so truncate
    # at the first segment matching ANY part of it, not only the whole of it.
    parts = {normalise(p) for p in raw.split("/") if normalise(p)}
    parts.add(normalise(raw))
    parts.discard("")
    final = str(recording.get("final_url") or "")
    out: List[List[str]] = []
    seen = set()

    def add(url: str) -> None:
        sp = urllib.parse.urlsplit(url)
        if not sp.netloc:
            return
        segs = sp.path.split("/")
        keep: List[str] = []
        for s in segs:
            if s and normalise(urllib.parse.unquote(s)) in parts:
                break
            keep.append(s)
        prefix = "/".join(keep) or "/"
        key = (sp.netloc, prefix)
        if key in seen:
            return
        seen.add(key)
        out.append([sp.netloc, prefix])

    if final:
        add(final)
    wanted = [normalise(i)[:60] for i in items if normalise(i)]
    for req in recording.get("network") or []:
        url = str(req.get("url") or "")
        if not url.startswith("http") or not req.get("body"):
            continue
        status = req.get("status")
        if not status or not (200 <= int(status) < 300):
            continue
        body = normalise(req.get("body") or "")
        if any(w and w in body for w in wanted[:6]):
            add(url)
        if len(out) >= 4:
            break
    return out


def _explaining_counts(recording: dict, patterns: List[List[str]]) -> List[int]:
    """How many requests of each pattern the recorded page made, parallel to
    ``patterns``: the distinct 2xx http(s) URLs in ``network`` that match it,
    whatever their body, at least 1 and at most :data:`MAX_EXPLAINING_COUNT`.
    The document's own pattern counts 1."""
    final = str(recording.get("final_url") or "")
    urls = set()
    for req in recording.get("network") or []:
        url = str(req.get("url") or "")
        try:
            status = int(req.get("status") or 0)
        except (TypeError, ValueError):
            continue
        if url.startswith("http") and 200 <= status < 300:
            urls.add(url)
    out: List[int] = []
    for i, pattern in enumerate(patterns):
        if i == 0 and final and CdpDriver._matches(final, [pattern]):
            out.append(1)
            continue
        seen = sum(1 for url in urls if CdpDriver._matches(url, [pattern]))
        out.append(max(1, min(MAX_EXPLAINING_COUNT, seen)))
    return out


def _value_calls(recording: dict, patterns: List[List[str]]) -> List[Optional[int]]:
    """How many batches of the search's own data calls each pattern answered with, parallel
    to ``patterns``.

    Only 2xx http(s) calls that carry the whole recorded input
    (``typed_readiness.carries_value``) are read, and of those only the ones from the result
    document on: the last ``network`` entry whose URL is ``final_url`` (fragment dropped).
    What comes before it is the page the person typed on (suggestions, previews before
    Enter); with no such entry (a search that rewrites the URL in place) nothing is dropped.
    A call is a batch when its body names a visible item no earlier batch named (a call
    whose body was not kept counts), so a prefix sent while typing never counts, a re-query
    with the same rows is not a second batch, and page 2 of a lazy list is.

    Per pattern: ``0`` for a pattern whose calls carrying the input all came before the
    result document (never waited for, though such a call in flight still holds the read);
    ``None`` for a pattern none of whose calls carries the input (the replay keeps
    ``explaining_min`` for it); else the batch count, at most :data:`MAX_EXPLAINING_COUNT`.
    The document's own pattern (the first) counts the calls on its path after the document
    that bring rows the document did not (a page that fetches its list from its own URL as
    JSON), and is ``0`` without them; the document itself is never one of its batches."""
    from anyuse.execution.api_compile import normalise
    from anyuse.execution.typed_readiness import carries_value, value_forms
    final = str(recording.get("final_url") or "")
    forms = value_forms((recording.get("param") or {}).get("value"))
    items = [str(x) for x in ((recording.get("ui") or {}).get("items") or [])]
    wanted = list(dict.fromkeys(normalise(i)[:60] for i in items if normalise(i)))
    network = [r for r in (recording.get("network") or []) if isinstance(r, dict)]

    def bare(url: Any) -> str:
        return urllib.parse.unquote_plus(str(url or "").split("#", 1)[0])

    target = bare(final)
    docs = [i for i, r in enumerate(network) if final and bare(r.get("url")) == target]
    # the result document, not a data call to the same URL
    pages = [i for i in docs if "html" in str(network[i].get("mime") or "html").lower()]
    cut = (pages or docs or [0])[-1]
    calls: List[Tuple[int, str, str, Any]] = []
    for i, req in enumerate(network):
        url = str(req.get("url") or "")
        try:
            status = int(req.get("status") or 0)
        except (TypeError, ValueError):
            continue
        post = str(req.get("post_data") or req.get("postData") or "")
        if url.startswith("http") and 200 <= status < 300 and carries_value(url, post, forms):
            calls.append((i, url, post, req.get("body")))
    bodies: Dict[int, str] = {}

    def names(i: int, body: Any) -> set:
        if i not in bodies:
            bodies[i] = normalise(body if isinstance(body, str) else json.dumps(body))
        return {w for w in wanted if w and w in bodies[i]}

    # the rows the result document itself brought; a call on its own path that brings
    # no other row is not one the page had to wait for
    in_doc = names(cut, network[cut].get("body")) if docs and network[cut].get("body") else set()
    out: List[Optional[int]] = []
    for n, pattern in enumerate(patterns):
        own = bool(n == 0 and final and CdpDriver._matches(final, [pattern]))
        mine = [c for c in calls if CdpDriver._matches(c[1], [pattern])
                and not (own and docs and c[0] == cut)]
        if not mine:
            out.append(0 if own else None)
            continue
        batches, named, sent = 0, set(in_doc) if own else set(), set()
        for i, url, post, body in mine:
            if i < cut or (url, post) in sent:
                continue
            sent.add((url, post))
            if not body:
                batches += 1
                continue
            new = names(i, body) - named
            if new:
                named |= new
                batches += 1
        out.append(min(MAX_EXPLAINING_COUNT, batches))
    return out


def _evidence_url(url: str, value: str, param_name: str,
                  recordings: Optional[List[dict]]):
    """``(url_template, path_params, transforms, how)`` or ``(None, {}, {}, why)``."""
    from anyuse.execution.param_binding import (binding_evidence,
                                                corroborated_sites, site_params,
                                                templated_url)
    if not recordings or len(recordings) < 2:
        return None, {}, {}, ("no cross-recording evidence: a binding site is "
                              "never taken from a single recording")
    evidence = binding_evidence(recordings, placeholder=param_name)
    sites, why = corroborated_sites(url, value, evidence, param_name)
    if not sites:
        return None, {}, {}, why
    site = sites[0]
    out = templated_url(url, site, param_name)
    if out is None:
        return None, {}, {}, "the corroborated site does not describe this URL"
    path_params, transforms = site_params(site, param_name)
    return out, path_params, transforms, site.describe()


# ---------------------------------------------------------------------------
# the driver boundary
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    """What one navigation produced. The only thing a driver must return."""
    url: str = ""
    dom: str = ""
    text: str = ""
    status: Optional[int] = None
    elapsed_ms: float = 0.0
    error: str = ""
    requests: List[dict] = field(default_factory=list)
    #: how the settle ended: "timer", "ready" or "ceiling", plus what was and
    #: was not satisfied. Reported so a latency number can be read honestly.
    settle: Dict[str, Any] = field(default_factory=dict)
    #: Deterministic browser actions taken before extraction (currently only a
    #: reject-only consent choice).  These are part of the replay log.
    steps: List[dict] = field(default_factory=list)
    consent: Dict[str, Any] = field(default_factory=dict)
    #: a live solve on this page (status + receipt, never page content)
    live_solve: Dict[str, Any] = field(default_factory=dict)
    #: only with ``capture_traffic``: the page's own GET responses with their (redacted)
    #: bodies, in the recording ``network`` format, for an automatic recompile
    network: List[dict] = field(default_factory=list)


def traffic_from_events(events: Sequence[dict], newest: bool = False) -> List[dict]:
    """The responses a replay may keep as compile evidence, from its protocol events, bodies
    not yet read: GET, 2xx, a document / XHR / fetch with a text mime type, finished, and no
    credential on the request: no credential header or query parameter, and no session cookie
    in the headers Chrome reports as sent (``requestWillBeSentExtraInfo``, judged by cookie
    name as ``attach.Observer`` does). Each entry carries ``_rid`` for
    ``Network.getResponseBody``. At most :data:`MAX_CAPTURED_BODIES`: the first ones (one
    page load), or with ``newest`` the last ones (an agent's session, whose last page is
    the one it answered on).

    A request whose credential is only a credential-like NAME (a query key, a header)
    is kept pending with its raw fields in memory (``_key_fields``): :func:`read_bodies`
    keeps it with ``page_keys`` when an earlier body of the site stated those values
    by name (``page_literals``), and drops it otherwise. A redirect's hops are kept
    (``redirected_from``)."""
    from anyuse.execution.attach import (BODY_MIME_HINTS, BODY_TYPES, _cookie_names,
                                         _query_credential, cookie_reason, header_reason)
    from anyuse.execution.har_import import _redact_query_credentials
    from anyuse.execution.page_literals import credential_fields
    reqs: Dict[str, dict] = {}
    #: credential reasons from the sent headers; the event may come before or after the request
    sent_with: Dict[str, str] = {}
    #: requests that sent a session cookie: never a page key
    cookie_sent: set = set()
    #: the credential-like header names each request was sent with (the event may show
    #: headers its request did not): a page-keyed request must have stated every one
    sent_names: Dict[str, set] = {}
    for e in events:
        method = e.get("method")
        p = e.get("params") or {}
        rid = p.get("requestId")
        if not rid:
            continue
        if method == "Network.requestWillBeSentExtraInfo":
            headers = p.get("headers") or {}
            names = [((c or {}).get("cookie") or {}).get("name", "")
                     for c in (p.get("associatedCookies") or [])
                     if not (c or {}).get("blockedReasons")]
            for k, v in headers.items():
                if str(k).lower() == "cookie":
                    names += _cookie_names(v)
            why = header_reason(headers) or cookie_reason(names)
            if cookie_reason(names):
                cookie_sent.add(rid)
            sent_names.setdefault(rid, set()).update(
                str(k).lower() for k in headers
                if str(k).lower() != "cookie" and header_reason({k: ""}))
            if why and not sent_with.get(rid):
                sent_with[rid] = why
        elif method == "Network.requestWillBeSent":
            req = p.get("request") or {}
            url = str(req.get("url") or "")
            headers = req.get("headers") or {}
            hops = (list(reqs[rid].get("redirected_from") or []) + [reqs[rid]["url"]]
                    if p.get("redirectResponse") and rid in reqs else [])
            why = header_reason(headers) or _query_credential(url) or ""
            fields: Dict[str, str] = {}
            if why:
                fields, never = credential_fields(url, headers, None)
                if never or "header:authorization" in fields:
                    fields = {}
            reqs[rid] = {"url": url, "method": str(req.get("method") or "GET").upper(),
                         "resource_type": str(p.get("type") or ""),
                         "credential_bound": why, "_fields": fields}
            if hops:
                reqs[rid]["redirected_from"] = [_redact_query_credentials(h) for h in hops[-10:]]
        elif method == "Network.responseReceived" and rid in reqs:
            resp = p.get("response") or {}
            reqs[rid]["status"] = resp.get("status")
            reqs[rid]["mime"] = str(resp.get("mimeType") or "")
            if p.get("type"):
                reqs[rid]["resource_type"] = str(p["type"])
        elif method == "Network.loadingFinished" and rid in reqs:
            reqs[rid]["finished"] = True
    out: List[dict] = []
    for rid, r in reqs.items():
        status = r.get("status")
        mime = str(r.get("mime") or "").lower()
        keyed = bool(r["credential_bound"] and r["_fields"] and rid not in cookie_sent
                     and all(f"header:{n}" in r["_fields"] for n in sent_names.get(rid, ())))
        if (((r["credential_bound"] or sent_with.get(rid)) and not keyed)
                or r["method"] != "GET"
                or not r.get("finished")
                or r["resource_type"] not in BODY_TYPES
                or not isinstance(status, int) or not 200 <= status < 300
                or not any(h in mime for h in BODY_MIME_HINTS)
                or not r["url"].startswith("https://")):
            continue
        entry = {"url": _redact_query_credentials(r["url"]) if keyed else r["url"],
                 "method": "GET", "status": status, "mime": r["mime"],
                 "body": None, "post_data": None, "bytes": 0,
                 "resource_type": r["resource_type"], "headers": {},
                 "credential_bound": "", "body_dropped": "", "_rid": rid}
        if keyed:
            entry["_key_fields"] = dict(r["_fields"])
        if r.get("redirected_from"):
            entry["redirected_from"] = list(r["redirected_from"])
        out.append(entry)
    return out[-MAX_CAPTURED_BODIES:] if newest else out[:MAX_CAPTURED_BODIES]


async def read_bodies(entries: Sequence[dict], call, timeout_s: float) -> List[dict]:
    """The bodies of :func:`traffic_from_events` entries, read with
    ``call("Network.getResponseBody", {"requestId": ...}, timeout)``: redacted
    (``attach.redact_html`` / ``redact_text``), at most ``attach.MAX_BODY_BYTES`` each; a body
    the browser no longer holds, a binary one or a larger one is left out.

    A pending entry (``_key_fields``) is kept with ``page_keys`` when an earlier body of the
    site states every one of its fields' values by name, and left out otherwise; the stated
    values then leave every body (``page_literals.SHOWN``)."""
    from anyuse.execution import page_literals as PL
    from anyuse.execution.attach import MAX_BODY_BYTES, redact_html, redact_text
    out: List[dict] = []
    index = PL.SourceIndex()
    secrets: set = set()
    site = ""
    for position, entry in enumerate(entries):
        entry = dict(entry)
        rid = entry.pop("_rid", None)
        fields = entry.pop("_key_fields", None)
        if not rid:
            continue
        try:
            res = await call("Network.getResponseBody", {"requestId": rid}, min(5.0, timeout_s))
        except Exception:             # noqa: BLE001 - evicted from the buffer
            continue
        body = (res or {}).get("body")
        if (res or {}).get("base64Encoded") or not isinstance(body, str):
            continue
        size = len(body.encode("utf-8", "ignore"))
        if size > MAX_BODY_BYTES:
            continue
        url = str(entry.get("url") or "")
        if fields:
            keys, _why = PL.provenance(fields, index, position)
            if keys is None:
                continue
            entry["page_keys"] = keys
            secrets.update(v for v in fields.values() if v)
        else:
            from anyuse.execution.web_scope import host_of, registrable
            site = site or registrable(host_of(url))
            if registrable(host_of(url)) == site:
                index.add(position, url, body)
        entry["body"] = (redact_html(body) if "html" in str(entry.get("mime") or "").lower()
                         else redact_text(body))
        entry["bytes"] = size
        out.append(entry)
    return [PL.scrub(e, secrets) for e in out] if secrets else out


class CdpDriver:
    """Opens one page over CDP in the container browser and returns its DOM.

    Imported lazily and only when a replay actually runs, so that importing
    this module — which the executor and every test does — pulls in no
    websocket client and opens no socket.
    """

    def __init__(self, endpoint: str, user_agent: str = "", live_solver=None,
                 origin: Optional[str] = None, task_id: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.user_agent = user_agent or USER_AGENT_SUFFIX
        self._last_hit: Dict[str, float] = {}
        #: human_solves_live: a challenge on the replayed page opens a live
        #: solve for a person (allowed origins only, never bench) in the SAME
        #: still-open target, then the page is read again
        self.live_solver, self.origin, self.task_id = live_solver, origin, task_id

    def _pace(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last_hit.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def open(self, url: str, settle_s: float, timeout_s: float,
             readiness: Optional[Dict[str, Any]] = None, url_policy=None,
             content_selector: str = "", capture_traffic: bool = False) -> PageResult:
        import asyncio
        self._pace(url)
        return asyncio.run(self._open(url, settle_s, timeout_s, readiness, url_policy,
                                      content_selector, capture_traffic))

    async def _open(self, url: str, settle_s: float, timeout_s: float,
                    readiness: Optional[Dict[str, Any]] = None,
                    url_policy=None, content_selector: str = "",
                    capture_traffic: bool = False) -> PageResult:
        import asyncio
        import urllib.request

        import websockets

        from anyuse.execution.browser_supervisor import (STALLED, UNREACHABLE, WS_KEEPALIVE,
                                                         BrowserSupervisor,
                                                         InfrastructureFailure,
                                                         browser_ws_url, classify)
        t0 = time.monotonic()
        # Retry the handshake with OUR OWN endpoint only. This is not a retry
        # against a site — no site is contacted until Page.navigate below — and
        # it exists because the host-to-container network drops connections
        # under load often enough to turn a resource problem into a page of
        # fake "tier 2 could not load the page" rows. Off the event loop and
        # bounded; still down after three tries = infrastructure.
        browser_ws, last = "", None
        for attempt in range(3):
            try:
                browser_ws = await browser_ws_url(self.endpoint, timeout=15)
                break
            except InfrastructureFailure as exc:
                last = exc
                await asyncio.sleep(1.0 * (attempt + 1))
        if not browser_ws:
            raise InfrastructureFailure(UNREACHABLE, f"cdp endpoint {self.endpoint} "
                                                     f"unreachable: {last}")
        try:
            bws_cm = websockets.connect(browser_ws, max_size=64 * 1024 * 1024, **WS_KEEPALIVE)
            bws = await bws_cm.__aenter__()
        except Exception as exc:                  # noqa: BLE001
            raise InfrastructureFailure(UNREACHABLE, f"browser websocket: {exc}") from exc
        target = None
        try:
            await bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                       "params": {"url": "about:blank"}}))
            deadline = time.monotonic() + 15
            while target is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise InfrastructureFailure(STALLED, "Target.createTarget got no answer")
                try:
                    msg = json.loads(await asyncio.wait_for(bws.recv(), timeout=left))
                except asyncio.TimeoutError:
                    continue
                if msg.get("id") == 1:
                    target = msg["result"]["targetId"]
            from anyuse.execution.browser_supervisor import page_ws_url
            page_ws = page_ws_url(self.endpoint, target)
            try:
                result = await self._drive(page_ws, url, settle_s, timeout_s,
                                           readiness, url_policy, content_selector,
                                           target_id=target, capture_traffic=capture_traffic)
            except Exception as exc:              # noqa: BLE001
                if isinstance(exc, InfrastructureFailure) or not classify(exc):
                    raise
                raise InfrastructureFailure(classify(exc), f"{type(exc).__name__}: "
                                                           f"{str(exc)[:120]}") from exc
        except Exception as exc:                  # noqa: BLE001
            if not isinstance(exc, InfrastructureFailure) and classify(exc):
                raise InfrastructureFailure(classify(exc), str(exc)[:160]) from exc
            raise
        finally:
            # one page per navigation, closed explicitly; a dead browser
            # websocket falls back to /json/close (bounded)
            closed = False
            if target is not None:
                try:
                    await bws.send(json.dumps({"id": 2, "method": "Target.closeTarget",
                                               "params": {"targetId": target}}))
                    end = time.monotonic() + 5
                    while time.monotonic() < end:
                        msg = json.loads(await asyncio.wait_for(
                            bws.recv(), timeout=max(0.1, end - time.monotonic())))
                        if msg.get("id") == 2:
                            closed = True
                            break
                except Exception:                 # noqa: BLE001
                    pass
                if not closed:
                    try:
                        await asyncio.to_thread(BrowserSupervisor(self.endpoint).close_target,
                                                target)
                    except Exception:             # noqa: BLE001
                        pass
            try:
                await asyncio.wait_for(bws_cm.__aexit__(None, None, None), timeout=5)
            except Exception:                     # noqa: BLE001
                pass
        result.elapsed_ms = (time.monotonic() - t0) * 1000.0
        return result

    async def _drive(self, page_ws: str, url: str, settle_s: float,
                     timeout_s: float,
                     readiness: Optional[Dict[str, Any]] = None,
                     url_policy=None, content_selector: str = "",
                     target_id: str = "", capture_traffic: bool = False) -> PageResult:
        import asyncio

        import websockets

        from anyuse.execution.browser_supervisor import (DOM_EXPR, STALLED, TEXT_EXPR,
                                                         WS_KEEPALIVE, InfrastructureFailure,
                                                         PageWatch, aprobe)
        out = PageResult()
        watch = PageWatch(target_id or None)
        async with websockets.connect(page_ws, max_size=64 * 1024 * 1024,
                                      **WS_KEEPALIVE) as ws:
            mid = [0]
            events: List[dict] = []
            refused: List[str] = []

            async def gate_event(msg):
                if url_policy is None or msg.get("method") != "Fetch.requestPaused":
                    return
                params = msg.get("params") or {}
                rid = params.get("requestId")
                target = str((params.get("request") or {}).get("url") or "")
                if not rid:
                    return
                reason = str(url_policy(target) or "")
                if reason:
                    # the ONE exception: a live solve on THIS page may load the
                    # check from its provider's own hosts (GET/HEAD)
                    from anyuse.execution.live_solve import CHALLENGE_EXCEPTIONS
                    if CHALLENGE_EXCEPTIONS.allows(
                            target_id, target,
                            str((params.get("request") or {}).get("method") or "GET")):
                        reason = ""
                mid[0] += 1
                if reason:
                    refused.append(f"{target}: {reason}")
                    await ws.send(json.dumps({"id": mid[0], "method": "Fetch.failRequest",
                                              "params": {"requestId": rid,
                                                         "errorReason": "BlockedByClient"}}))
                else:
                    await ws.send(json.dumps({"id": mid[0], "method": "Fetch.continueRequest",
                                              "params": {"requestId": rid}}))

            io_lock = asyncio.Lock()

            async def _call(method, params, timeout):
                watch.check()
                async with io_lock:
                    mid[0] += 1
                    this = mid[0]
                    await ws.send(json.dumps({"id": this, "method": method,
                                              "params": params or {}}))
                    deadline = time.monotonic() + timeout
                    while True:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise TimeoutError(method)
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                        if msg.get("id") == this:
                            if "error" in msg:
                                raise RuntimeError(f"{method}: {msg['error']}")
                            return msg.get("result", {})
                        if "method" in msg:
                            events.append(msg)
                            watch.see(msg)
                            await gate_event(msg)
                            watch.check()

            async def call(method, params=None, timeout=timeout_s):
                try:
                    return await _call(method, params, timeout)
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    if isinstance(exc, InfrastructureFailure):
                        raise
                    ok, why = await aprobe(self.endpoint)
                    raise InfrastructureFailure(
                        STALLED if ok else "unreachable",
                        f"{method} timed out; " + ("the page does not answer" if ok
                                                   else why)) from exc

            async def pump(seconds):
                watch.check()
                async with io_lock:
                    deadline = time.monotonic() + seconds
                    while True:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            return
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                        except asyncio.TimeoutError:
                            return
                        if "method" in msg:
                            events.append(msg)
                            watch.see(msg)
                            await gate_event(msg)
                            watch.check()

            await call("Network.enable")
            await call("Page.enable")
            await call("Runtime.enable")
            try:
                await call("Inspector.enable")
            except RuntimeError:
                pass
            await call("Network.setUserAgentOverride", {"userAgent": self.user_agent})
            if url_policy is not None:
                await call("Fetch.enable", {"patterns": [
                    {"urlPattern": "*", "requestStage": "Request",
                     "resourceType": "Document"}]})
            try:
                await call("Page.navigate", {"url": url})
            except Exception as exc:
                out.error = f"navigation failed: {type(exc).__name__}: {exc}"
                return out
            if refused:
                out.error = "pre-send policy refused browser navigation: " + refused[0]
                return out

            async def js(expr):
                res = await call("Runtime.evaluate",
                                 {"expression": expr, "returnByValue": True,
                                  "awaitPromise": True})
                return (res.get("result") or {}).get("value")

            # The ceiling is EXACTLY the window the fixed timer would have used,
            # so readiness can only ever read the page sooner. That is the whole
            # point, and it is also what makes the on/off comparison fair.
            ceiling = min(settle_s + 6.0, timeout_s)
            out.dom, out.settle = await self._settle_and_read(js, pump, events, readiness,
                                                              ceiling)
            out.text = await js(TEXT_EXPR) or ""
            out.url = await js("location.href") or url

            # Consent is resolved in the still-open target.  The executor sees
            # the resulting page plus the exact deterministic steps; it never
            # asks a driver to reopen a URL merely to click a banner.
            from anyuse.execution.blocked import COOKIE_WALL, detect_blocked_ui
            from anyuse.execution.consent import (ASK, CONTROLS_JS, NEVER, REJECT,
                                                   automatic_action, choose_reject,
                                                   choose_settings, click_js,
                                                   consent_policy,
                                                   remembered_decision)
            rendered_items = []
            if content_selector:
                try:
                    count = int(await js("(() => { try { return document.querySelectorAll("
                                             + json.dumps(content_selector)
                                             + ").length; } catch (_) { return 0; } })()") or 0)
                    rendered_items = ["rendered"] * min(count, 1)
                except Exception:
                    rendered_items = []
            consent_state = detect_blocked_ui(out.text, "", out.url, [],
                                              items=rendered_items)
            if consent_state.blocked and consent_state.reason == COOKIE_WALL:
                policy = consent_policy()
                decision = remembered_decision(consent_state.host)
                action = automatic_action(consent_state.host, policy)
                out.consent = {"policy": policy, "remembered": decision,
                               "status": action}
                if action == REJECT:
                    controls = await js(CONTROLS_JS) or []
                    choice = choose_reject(controls)
                    layer = 1
                    if choice is None:
                        settings_choice = choose_settings(controls)
                        if settings_choice is not None and await js(
                                click_js(settings_choice.index)) is True:
                            out.steps.append(settings_choice.step(layer=1))
                            await pump(min(2.0, timeout_s))
                            controls = await js(CONTROLS_JS) or []
                            choice = choose_reject(controls)
                            layer = 2
                    if choice is not None and await js(click_js(choice.index)) is True:
                        out.steps.append(choice.step(layer=layer))
                        await pump(min(2.0, timeout_s))
                        out.dom = await js(DOM_EXPR) or ""
                        out.text = await js(TEXT_EXPR) or ""
                        out.url = await js("location.href") or out.url
                        after = detect_blocked_ui(out.text, "", out.url, [])
                        out.consent["status"] = ("rejected" if not (
                            after.blocked and after.reason == COOKIE_WALL) else ASK)
                    else:
                        out.consent["status"] = ASK
                elif action == NEVER:
                    out.consent["status"] = NEVER
            if self.live_solver is not None and target_id:
                await self._maybe_live_solve(out, url, target_id, js, call, events)
            for e in events:
                if e.get("method") == "Network.responseReceived":
                    resp = (e.get("params") or {}).get("response") or {}
                    ru = resp.get("url") or ""
                    out.requests.append({"url": ru, "status": resp.get("status")})
                    if ru.split("#")[0] in (url, out.url) and out.status is None:
                        out.status = resp.get("status")
            if capture_traffic:
                out.network.extend(await read_bodies(traffic_from_events(events), _call,
                                                     timeout_s))
        return out

    async def _maybe_live_solve(self, out: PageResult, url: str, target_id: str, js, call,
                                events: List[dict]) -> None:
        """A challenge on the still-open page: a person solves it live, then
        the page is read again. Never for bench; no model sees anything."""
        import asyncio

        from anyuse.execution import live_solve as ls
        from anyuse.execution.browser_supervisor import DOM_EXPR, TEXT_EXPR

        def requests() -> List[dict]:
            return [{"url": str(((e.get("params") or {}).get("request") or {})
                                .get("url") or "")}
                    for e in events[-400:] if e.get("method") == "Network.requestWillBeSent"]
        first = {"url": out.url, "text": out.text, "markup": (out.dom or "")[:200000],
                 "requests": requests()}
        if not ls.challenge_evidence(first) or not ls.solver_applies(self.live_solver,
                                                                    self.origin):
            return

        async def observe():
            return {"url": await js("location.href") or "",
                    "text": await js(TEXT_EXPR) or "",
                    "markup": ((await js(DOM_EXPR)) or "")[:200000],
                    "requests": requests()}

        async def relay(method, params):
            return await call(method, params, timeout=20.0)
        host = (urllib.parse.urlsplit(out.url or url).hostname or "").lower()
        req = ls.SolveRequest(task_id=self.task_id or host, host=host, origin=self.origin,
                              run_id=self.task_id, step="replay")
        bridge = ls.AsyncPageBridge(asyncio.get_running_loop(), target_id, observe, relay)
        res = await ls.solve_async(self.live_solver, req, bridge)
        out.live_solve = {"status": res.get("status"), "receipt": res.get("receipt")}
        if res.get("status") == "cleared":
            out.dom = await js(DOM_EXPR) or ""
            out.text = await js(TEXT_EXPR) or ""
            out.url = await js("location.href") or out.url

    async def _settle_and_read(self, js, pump, events, readiness: Optional[Dict[str, Any]],
                               ceiling_s: float,
                               clock=time.monotonic) -> Tuple[str, Dict[str, Any]]:
        """Let the page settle, then read its DOM: ``(dom, settle)``.

        Without ``readiness`` the page is read at ``ceiling_s``. With it, the
        page is read when the recorded evidence is there: under the stored
        :data:`READINESS_RULE`, with the input known, once the search's own data
        calls answered and the rows stood still (:meth:`_await_value_calls`);
        otherwise by the earlier rule (:meth:`_await_ready`). A ready read whose
        extraction falls short (:func:`ready_read_passes`) is read again at
        ``ceiling_s``, in the same navigation. ``js`` and ``pump`` are the page's
        evaluate and event-wait calls; ``clock`` is the time source.
        """
        from anyuse.execution.browser_supervisor import DOM_EXPR, TEXT_EXPR
        from anyuse.execution.typed_readiness import value_forms
        t_settle = clock()
        if readiness and readiness.get("rule") == READINESS_RULE \
                and value_forms(readiness.get("value")):
            settle = await self._await_value_calls(js, pump, events, readiness, ceiling_s,
                                                   clock=clock)
        elif readiness:
            settle = await self._await_ready(js, pump, events, readiness, ceiling_s,
                                             clock=clock)
        else:
            await pump(ceiling_s)
            settle = {"how": "timer", "waited_ms": round(ceiling_s * 1000)}

        dom = await js(DOM_EXPR) or ""
        if (readiness and settle.get("how") == "ready"
                and readiness.get("fallback") == "timer"):
            text = ""
            if isinstance(readiness.get("contract"), dict):
                # a page's "no results" statement counts only where the browser shows it
                shown = await js(TEXT_EXPR)
                text = shown if isinstance(shown, str) else ""
            ok, why = ready_read_passes(dom, readiness, text=text)
            if not ok:
                # The timer as the fall-back: same navigation, no second
                # request; wait out the rest of the window and read again.
                remaining = max(0.0, ceiling_s - (clock() - t_settle))
                await pump(remaining)
                dom = await js(DOM_EXPR) or ""
                settle = {**settle, "how": "ready->timer_fallback",
                          "first_read": why,
                          "waited_ms": round((clock() - t_settle) * 1000)}
        return dom, settle

    @staticmethod
    def _matches(url: str, patterns: List[List[str]]) -> bool:
        sp = urllib.parse.urlsplit(url)
        for host, prefix in patterns:
            if sp.netloc == host and sp.path.startswith(prefix.rstrip("/")):
                return True
        return False

    @staticmethod
    def _rows_expr(readiness: Dict[str, Any]) -> str:
        """The page expression for the result rows the readiness selector matches."""
        count = ("document.querySelectorAll("
                 + json.dumps(str(readiness.get("selector") or "")) + ")")
        if readiness.get("visible_only"):
            # only the rows html_extract's visible_only keeps (hidden_by_markup): no
            # element from the row up has the hidden attribute, aria-hidden="true" or an
            # inline display: none / visibility: hidden. The ready read extracts from the
            # DOM the same way, and a stylesheet's hiding is in neither.
            from anyuse.execution.html_extract import _HIDDEN_STYLE
            count = ("Array.prototype.filter.call(" + count + ", e => { const style = "
                     "new RegExp(" + json.dumps(_HIDDEN_STYLE.pattern) + ", 'i');"
                     " for (let a = e; a; a = a.parentElement) { if (a.hasAttribute('hidden')"
                     " || (a.getAttribute('aria-hidden') || '').trim().toLowerCase() === 'true'"
                     " || style.test(a.getAttribute('style') || '')) return false; }"
                     " return true; })")
        return count

    async def _await_ready(self, js, pump, events, readiness: Dict[str, Any],
                           ceiling_s: float, clock=time.monotonic) -> Dict[str, Any]:
        """Wait for the recorded evidence, never past ``ceiling_s``.

        Returns what happened, which is reported all the way out to the caller:
        a replay that hit the ceiling without its evidence is not a different
        kind of result from the timer's — it is the timer's — and the row says
        so rather than quietly claiming a readiness win. ``clock`` is the time
        source every wait and deadline here is measured on.
        """
        selector = str(readiness.get("selector") or "")
        patterns = [list(p) for p in (readiness.get("explaining") or [])]
        stable_s = max(0.0, float(readiness.get("stable_ms", DEFAULT_STABLE_MS)) / 1000.0)
        poll_s = max(0.05, float(readiness.get("poll_ms", DEFAULT_POLL_MS)) / 1000.0)
        min_items = int(readiness.get("min_items") or 1)
        count = self._rows_expr(readiness)
        # Per pattern, how many finished requests make it "done", and the DOM
        # must then stand still for the stability window AFTER that: a response
        # has finished loading before the page's script renders it. Without the
        # counts (a template stored without them) one seen and none in flight is
        # enough, and stability counts from whenever the DOM stopped moving.
        counts = readiness.get("explaining_min")
        need = ([max(1, int(c)) for c in counts]
                if isinstance(counts, list) and len(counts) == len(patterns) else None)
        expr = ("(() => { let n = -1; try { n = " + count + ".length } catch (e) { n = -1 }"
                " return {n: n, len: document.documentElement.outerHTML.length,"
                " ready: document.readyState}; })()")

        t0 = clock()
        by_id: Dict[str, str] = {}
        finished: set = set()
        stable_since: Optional[float] = None
        net_since: Optional[float] = None
        last_len = -1
        polls = 0
        state = {"net": False, "items": False, "stable": False}

        while clock() - t0 < ceiling_s:
            await pump(min(poll_s, ceiling_s - (clock() - t0)))
            for e in events:
                m = e.get("method")
                p = e.get("params") or {}
                rid = p.get("requestId")
                if m == "Network.requestWillBeSent" and rid:
                    by_id[rid] = (p.get("request") or {}).get("url") or ""
                elif m in ("Network.loadingFinished", "Network.loadingFailed") and rid:
                    finished.add(rid)
            try:
                st = await js(expr) or {}
            except Exception:                      # a page that navigated away
                break                              # mid-poll: read what is there
            polls += 1
            n, length = int(st.get("n", -1)), int(st.get("len", 0))
            if last_len >= 0 and abs(length - last_len) <= max(
                    8, int(last_len * DOM_DRIFT_TOLERANCE)):
                stable_since = stable_since if stable_since is not None else clock()
            else:
                stable_since = None
            last_len = length

            inflight = [rid for rid, u in by_id.items()
                        if rid not in finished and self._matches(u, patterns)]
            if need is None:
                seen_any = any(self._matches(u, patterns) for u in by_id.values())
                state["net"] = bool(patterns) and seen_any and not inflight
            else:
                done = [len({u for rid, u in by_id.items()
                             if rid in finished and self._matches(u, [p])})
                        for p in patterns]
                state["net"] = (bool(patterns) and not inflight
                                and all(d >= k for d, k in zip(done, need)))
            net_since = (net_since if net_since is not None else clock()) if state["net"] else None
            settled_from = stable_since
            if need is not None and stable_since is not None and net_since is not None:
                settled_from = max(stable_since, net_since)
            state["items"] = (n >= min_items) if selector else True
            state["stable"] = (settled_from is not None
                               and clock() - settled_from >= stable_s)
            if st.get("ready") == "complete" and all(state.values()):
                return {"how": "ready",
                        "waited_ms": round((clock() - t0) * 1000),
                        "polls": polls, "items": n, **state}
        return {"how": "ceiling",
                "waited_ms": round((clock() - t0) * 1000),
                "polls": polls, **state,
                "note": CEILING_NOTE}

    async def _await_value_calls(self, js, pump, events, readiness: Dict[str, Any],
                                 ceiling_s: float, clock=time.monotonic) -> Dict[str, Any]:
        """Wait for the search's own data calls and the result rows, never past ``ceiling_s``
        (the :data:`READINESS_RULE` rule, see :func:`readiness_from_recording`).

        The page is ready when its document is complete and

        * ``calls``: no request of an explaining pattern that carries the input
          (``readiness["value"]``) is open; of each pattern with a batch count
          (``value_calls`` k >= 1) whose calls carrying the input this replay sent, at least
          k distinct ones finished, the page's documents aside (a pattern the replay never
          called, such as a suggestion list fed by typing, is not waited for); of each
          pattern whose calls never carried the input (``None``), the earlier rule holds: seen,
          ``explaining_min`` finished, none open. When the replay called every pattern
          with a batch count but none of those calls carries the input in a form
          ``typed_readiness.value_forms`` knows, each is held to that earlier rule too;
        * ``answered``: one of those calls answered, unless no pattern needs one (the
          list came with the document);
        * ``stable``: the result rows, their count and their text length, have not changed
          for ``stable_ms`` since the calls answered (with no row, the document's length).

        The recorded count is not waited for: it is the check on the read
        (:func:`ready_read_passes`). Returns how the wait ended, as :meth:`_await_ready`.
        """
        from anyuse.execution.typed_readiness import carries_value, value_forms
        selector = str(readiness.get("selector") or "")
        patterns = [list(p) for p in (readiness.get("explaining") or [])]
        stable_s = max(0.0, float(readiness.get("stable_ms", DEFAULT_STABLE_MS)) / 1000.0)
        poll_s = max(0.05, float(readiness.get("poll_ms", DEFAULT_POLL_MS)) / 1000.0)
        forms = value_forms(readiness.get("value"))
        stored = readiness.get("value_calls")
        stored = list(stored) if isinstance(stored, list) and len(stored) == len(patterns) \
            else [None] * len(patterns)
        batches = [int(k) if isinstance(k, int) and not isinstance(k, bool) else None
                   for k in stored]
        counts = readiness.get("explaining_min")
        recorded = ([max(1, int(c)) for c in counts]
                    if isinstance(counts, list) and len(counts) == len(patterns)
                    else [1] * len(patterns))
        rows = self._rows_expr(readiness)
        expr = ("(() => { let n = -1, rlen = 0; try { const rows = " + rows + "; n = rows.length;"
                " rlen = Array.prototype.reduce.call(rows, (s, e) => s"
                " + String(e.textContent || '').length, 0); } catch (e) { n = -1 }"
                " return {n: n, rlen: rlen, len: document.documentElement.outerHTML.length,"
                " ready: document.readyState}; })()")

        def region_same(a, b) -> bool:
            if a is None or b is None or a[0] != b[0]:
                return False
            return abs(a[1] - b[1]) <= max(8, int(b[1] * DOM_DRIFT_TOLERANCE))

        t0 = clock()
        #: request id -> (url, post data, the patterns it matches, whether it carries the input)
        by_id: Dict[str, Tuple[str, str, Tuple[int, ...], bool]] = {}
        finished: set = set()
        ended: set = set()
        #: the page's documents: never one of a pattern's batches
        documents: set = set()
        indexed = 0
        last_region: Optional[Tuple[int, int]] = None
        stable_since: Optional[float] = None
        calls_since: Optional[float] = None
        polls, n, open_value = 0, -1, 0
        state = {"calls": False, "answered": False, "stable": False}

        def earlier(i: int, mine: List[str]) -> bool:
            """The earlier rule for pattern ``i``: seen, ``explaining_min`` answered, none open."""
            done = len({by_id[rid][0] for rid in mine if rid in ended})
            return bool(mine) and done >= recorded[i] and all(rid in ended for rid in mine)

        while clock() - t0 < ceiling_s:
            await pump(min(poll_s, ceiling_s - (clock() - t0)))
            for e in events[indexed:]:
                m = e.get("method")
                p = e.get("params") or {}
                rid = p.get("requestId")
                if m == "Network.requestWillBeSent" and rid:
                    req = p.get("request") or {}
                    url, post = str(req.get("url") or ""), str(req.get("postData") or "")
                    by_id[rid] = (url, post,
                                  tuple(i for i, pat in enumerate(patterns)
                                        if self._matches(url, [pat])),
                                  carries_value(url, post, forms))
                    if p.get("type") == "Document":
                        documents.add(rid)
                elif m == "Network.loadingFinished" and rid:
                    finished.add(rid)
                    ended.add(rid)
                elif m == "Network.loadingFailed" and rid:
                    ended.add(rid)
            indexed = len(events)
            try:
                st = await js(expr) or {}
            except Exception:                      # a page that navigated away
                break                              # mid-poll: read what is there
            polls += 1
            n = int(st.get("n", -1))
            rlen = st.get("rlen")
            rlen = int(rlen) if isinstance(rlen, (int, float)) and not isinstance(rlen, bool) else 0
            region = (n, rlen) if n > 0 else (n, int(st.get("len", 0) or 0))
            if region_same(region, last_region):
                stable_since = stable_since if stable_since is not None else clock()
            else:
                stable_since = None
            last_region = region

            open_value = sum(1 for rid, (_u, _p, pats, value) in by_id.items()
                             if pats and value and rid not in ended)

            calls_ok, answered, needed = open_value == 0, False, False
            #: a request of a pattern with a batch count carries the input
            carried = False
            #: patterns with a batch count of which no call (the documents aside) carries it
            unread: List[Tuple[int, List[str]]] = []
            for i, k in enumerate(batches):
                mine = [rid for rid, (_u, _p, pats, _v) in by_id.items() if i in pats]
                if k is None:
                    needed = True
                    ok = earlier(i, mine)
                    calls_ok = calls_ok and ok
                    answered = answered or ok
                elif k >= 1:
                    needed = True
                    carrying = [rid for rid in mine if by_id[rid][3]]
                    carried = carried or bool(carrying)
                    sent = [rid for rid in carrying if rid not in documents]
                    if sent:
                        done = len({by_id[rid][:2] for rid in sent if rid in finished})
                        calls_ok = calls_ok and done >= k
                        answered = answered or done >= 1
                    else:
                        unread.append((i, mine))
            if not carried and unread and all(mine for _i, mine in unread):
                # the replay called every such pattern, but none of its calls carries the
                # input in a form value_forms knows (the page folded, split or looked up
                # the input): each is held to the earlier rule, as a pattern whose
                # recorded calls never carried the input is
                for i, mine in unread:
                    ok = earlier(i, mine)
                    calls_ok = calls_ok and ok
                    answered = answered or ok
            answered = answered or not needed
            net = calls_ok and answered
            calls_since = (calls_since if calls_since is not None else clock()) if net else None
            settled_from = (max(stable_since, calls_since)
                            if stable_since is not None and calls_since is not None else None)
            state = {"calls": calls_ok, "answered": answered,
                     "stable": settled_from is not None and clock() - settled_from >= stable_s}
            if st.get("ready") == "complete" and (n >= 0 or not selector) and all(state.values()):
                return {"how": "ready", "rule": READINESS_RULE,
                        "waited_ms": round((clock() - t0) * 1000),
                        "polls": polls, "items": n, **state, "open_value_calls": open_value}
        return {"how": "ceiling", "rule": READINESS_RULE,
                "waited_ms": round((clock() - t0) * 1000),
                "polls": polls, "items": n, **state, "open_value_calls": open_value,
                "note": CEILING_NOTE}


#: how a readiness wait that hit its ceiling is reported
CEILING_NOTE = ("the recorded evidence never all arrived; the page was read at the same "
                "instant the fixed timer would have read it")


# ---------------------------------------------------------------------------
# the executor
# ---------------------------------------------------------------------------

class ReplayExecutor:
    """Runs a :class:`ReplayTemplate`. Answers the executor's per-modality contract.

    ``driver`` is injectable and is the ONLY thing that touches a browser, so
    every test in this repository runs this class offline with a fake.
    """

    def __init__(self, driver=None, endpoint: Optional[str] = None,
                 hosts_path: Optional[str] = None, live_solver=None,
                 origin: Optional[str] = None, task_id: str = ""):
        self._driver = driver
        self._endpoint = endpoint
        self.hosts_path = hosts_path
        #: human_solves_live: handed to the CDP driver (execution/live_solve.py)
        self.live_solver, self.origin, self.task_id = live_solver, origin, task_id
        #: the check model's calls for empty answers (``execute(..., empty=)``),
        #: reported so the count is checkable, not assumed
        self.model_calls = 0

    def driver(self):
        if self._driver is not None:
            return self._driver
        endpoint = cdp_endpoint(self._endpoint)
        if not endpoint:
            return None
        # one driver per executor, so its per-host pacing holds across several replays
        made = getattr(self, "_made", None)
        if made is None or made.endpoint != endpoint.rstrip("/"):
            made = self._made = CdpDriver(endpoint, live_solver=self.live_solver,
                                          origin=self.origin, task_id=self.task_id)
        return made

    def execute(self, template: ReplayTemplate,
                params: Optional[Dict[str, Any]] = None,
                capture_traffic: bool = False, *, empty: Any = None,
                _raw: bool = False) -> Dict[str, Any]:
        """``capture_traffic``: a successful read with items also returns ``evidence`` (the page,
        its text and the GET responses it loaded) when the driver can capture them, so the
        runtime cascade can recompile a broken compiled request from what the site serves now;
        a driver that cannot is reported as ``capture: "unsupported"``.

        ``empty``: an ``empty_page.Context`` (``run_compiled``'s product call): an empty
        answer may then load the page for other inputs to compare it with the site's own
        empty page. ``_raw``: return the page read (``dom``, ``text``, ``items``) after the
        identity and block checks, without judging it; such a load never captures traffic."""
        from anyuse.execution.api_permission import replay_refusal
        from anyuse.execution.blocked import (STICKY_REASONS, blocked_hil_action,
                                              blocked_until, detect_blocked_ui,
                                              mark_blocked)
        from anyuse.execution.html_extract import extract, select, parse

        base = {"method": "cdp", "modality": "cdp", "model_calls": 0}

        problems = template.problems()
        if problems:
            return {**base, "success": False,
                    "error": "template refused: " + "; ".join(problems)}

        driver = self.driver()
        if driver is None:
            return {**base, "success": False,
                    "error": (f"the deterministic browser replay rung is opt-in and "
                              f"needs {ENDPOINT_ENV} to name a DevTools endpoint "
                              f"(a headless browser in a container). It is never "
                              f"started automatically and never runs on your desktop.")}

        try:
            url = template.render(params or {})
        except KeyError as exc:
            return {**base, "success": False, "error": str(exc)}

        from anyuse.execution.request_policy import destination_refusal, exact_origin_allowlist
        allowed_origins = exact_origin_allowlist(template.url, template.approved_origins)
        destination_block = destination_refusal(url, allowed_origins)
        if destination_block:
            return {**base, "success": False, "blocked": True,
                    "outcome_kind": "terminal_refusal", "url": url,
                    "error": f"pre-send policy refused navigation: {destination_block}"}

        def url_policy(target):
            return destination_refusal(target, allowed_origins)

        refusal = replay_refusal(url, hosts_path=self.hosts_path)
        if refusal:
            return {**base, "success": False, "blocked": True, "robots_refused": True,
                    "error": refusal}

        host = urllib.parse.urlsplit(url).netloc
        from anyuse.execution.blocked import is_browser_only
        browser_only = is_browser_only(host)
        # A browser-only host refused the HTTP CLIENT, not the browser. Holding
        # the browser rung back on that evidence would file a workflow that
        # works as "the site said no", which is what milestone 5 did to
        # hackage.haskell.org. This is not a re-hit of a host that refused us:
        # the browser was served, and it is the browser asking again.
        held = None if browser_only else blocked_until(host)
        if held:
            return {**base, "success": False, "blocked": True, "requires_human": True,
                    "blocked_state": {"blocked": True, "reason": held.get("reason"),
                                      "message": held.get("message"),
                                      "until": held.get("until"),
                                      "action_required": "human"},
                    "error": (f"host is on do-not-auto-retry until {held.get('until')}: "
                              f"{held.get('reason')}")}

        # A driver from before milestone 6 — including every test fake written
        # against the old signature — takes three arguments and gets the fixed
        # timer, which is exactly what it always had. The signature is INSPECTED
        # rather than a TypeError being caught, because catching one here would
        # also swallow a TypeError raised inside the driver and silently retry
        # it, turning a real bug into a mysterious second attempt.
        takes_readiness = True
        try:
            import inspect
            parameters = inspect.signature(driver.open).parameters
            takes_readiness = "readiness" in parameters
            takes_url_policy = "url_policy" in parameters
            takes_content_selector = "content_selector" in parameters
            takes_capture = "capture_traffic" in parameters
        except (TypeError, ValueError):
            takes_readiness = False
            takes_url_policy = False
            takes_content_selector = False
            takes_capture = False
        if capture_traffic and not takes_capture:
            # said on every result, so a recompile does not replay more pages for traffic
            base["capture"] = "unsupported"

        t0 = time.monotonic()
        try:
            driver_kw = {}
            if takes_readiness:
                driver_kw["readiness"] = runtime_readiness(
                    template, params, empty=site_empty_check(template, empty))
            if takes_url_policy:
                driver_kw["url_policy"] = url_policy
            if takes_content_selector:
                driver_kw["content_selector"] = str(
                    (template.expect or {}).get("selector")
                    or template.extract.get("items") or "")
            if capture_traffic and takes_capture:
                driver_kw["capture_traffic"] = True
            page = driver.open(url, template.settle_s, template.timeout_s, **driver_kw)
        except Exception as exc:
            from anyuse.execution.browser_supervisor import INFRASTRUCTURE, classify
            kind = classify(exc)
            if kind:
                # the browser (or the path to it) stopped answering: never a
                # verdict about the workflow, never a reason to retry the site
                return {**base, "success": False, "url": url,
                        "outcome_kind": INFRASTRUCTURE, "infrastructure": kind,
                        "elapsed_ms": (time.monotonic() - t0) * 1000.0,
                        "error": f"browser infrastructure ({kind}): {exc}"[:300]}
            return {**base, "success": False, "url": url,
                    "elapsed_ms": (time.monotonic() - t0) * 1000.0,
                    "error": f"browser error: {type(exc).__name__}: {exc}"}
        elapsed = page.elapsed_ms or (time.monotonic() - t0) * 1000.0
        base["steps"] = list(getattr(page, "steps", []) or [])
        if page.error:
            return {**base, "success": False, "url": url, "elapsed_ms": elapsed,
                    "error": page.error}
        final_refusal = destination_refusal(page.url or url, allowed_origins)
        if final_refusal:
            return {**base, "success": False, "blocked": True,
                    "outcome_kind": "terminal_refusal", "url": page.url or url,
                    "elapsed_ms": elapsed,
                    "error": f"browser ended at a refused origin: {final_refusal}"}

        expect = template.expect or {}
        identity = expect.get("identity") or {}
        if identity.get("final_url") == "rendered_url":
            def without_fragment(value):
                parts = urllib.parse.urlsplit(value or "")
                return urllib.parse.urlunsplit(
                    (parts.scheme.lower(), parts.netloc.lower(), parts.path,
                     parts.query, ""))
            if without_fragment(page.url or url) != without_fragment(url):
                return {**base, "success": False, "outcome_kind": "incomplete",
                        "url": page.url or url, "elapsed_ms": elapsed,
                        "error": ("postcondition identity failed: final URL "
                                  "does not identify the requested workflow result")}

        visible = bool(getattr(template, "visible_only", False))
        try:
            extracted = {name: extract(page.dom, sel, limit=100, visible_only=visible)
                         for name, sel in template.extract.items()}
        except ValueError as exc:
            return {**base, "success": False, "outcome_kind": "terminal_refusal",
                    "blocked": True, "url": page.url, "elapsed_ms": elapsed,
                    "error": f"HTML parser refused input: {exc}"}
        items = extracted.get("items") or []

        state = detect_blocked_ui(page.text, "", page.url, page.requests,
                                  items=items)
        if state.blocked:
            record = None
            if state.reason in STICKY_REASONS:
                try:
                    record = mark_blocked(state)
                except Exception as exc:                 # a registry that cannot
                    logger.debug("blocked host not recorded: %s", exc)  # be written
            blocked_dict = (state.to_dict() if hasattr(state, "to_dict")
                            else {"blocked": True, "reason": state.reason})
            needs_human = None
            consent_info = dict(getattr(page, "consent", {}) or {})
            if state.reason == "cookie_wall" and consent_info.get("status") != "never":
                from anyuse.execution.consent import handoff_question
                blocked_dict["message"] = handoff_question(state.host)
                blocked_dict["consent"] = consent_info
                try:
                    from anyuse.execution import human_inbox
                    card = human_inbox.Inbox().post_standalone(
                        {**blocked_dict, "card_kind": human_inbox.KIND_CONSENT},
                        template.url)
                    needs_human = human_inbox.needs_human(card)
                except Exception as exc:
                    logger.debug("consent card not recorded: %s", exc)
            if getattr(page, "live_solve", None):
                blocked_dict["live_solve"] = dict(page.live_solve)
            return {**base, "success": False, "blocked": True, "requires_human": True,
                    "url": page.url, "elapsed_ms": elapsed,
                    "blocked_state": blocked_dict,
                    "hil_action": blocked_hil_action(state),
                    "blocked_record": record,
                    **({"needs_human": needs_human} if needs_human else {}),
                    "error": f"the site asked for a human: {state.reason}"}

        if _raw:
            return {**base, "success": True, "raw": True, "url": page.url or url,
                    "elapsed_ms": elapsed, "dom": page.dom, "text": page.text,
                    "items": list(items), "status": page.status}

        sel = expect.get("selector") or template.extract.get("items") or ""
        try:
            # parsed once more for the count; the contract checks below read this tree
            tree = parse(page.dom, visible_only=visible)
            matched = len(select(tree, sel)) if sel else 0
        except ValueError as exc:
            return {**base, "success": False, "outcome_kind": "terminal_refusal",
                    "blocked": True, "url": page.url, "elapsed_ms": elapsed,
                    "error": f"HTML parser refused input: {exc}"}
        min_items = int(expect.get("min_items") or 0)
        from anyuse.execution import result_contract as RC
        contract = RC.contract_of(expect)

        if not items:
            # Every template, with a contract or not: the site must say there are no
            # results for THIS query (result_contract.empty_verdict: the site's own
            # empty page, the check model on a product call, then a statement bound
            # to the query). This is the ONLY route to a success with an empty
            # result, and the line is reported so a reader can disagree with it. The
            # rules read the page visible-only, so they reuse the tree of a
            # visible-only template.
            from anyuse.execution import empty_page as EP
            value = _query_value(template, contract, params)
            qp = str((contract or {}).get("query_param")
                     or (list(template.params or []) or [""])[0])
            fetch = None
            if empty is not None and empty.probe and qp:
                asked = dict(params or {})

                def fetch(v: str):
                    raw = self.execute(template, {**asked, qp: v}, _raw=True)
                    return EP.page_from_replay(raw, v)
            # a template without a contract (made before them) reads the query at
            # every place of a longer word, as it always did
            ev = RC.empty_verdict(
                contract or RC.replay_contract(qp, positions=None),
                EP.Page("html", value, markup=page.dom, text=page.text, url=page.url or url,
                        root=tree if visible else None),
                root=tree if visible else None, ctx=empty, fetch=fetch,
                positive_ok=RC.positive_ok_for(contract, str(template.extract.get("items") or sel),
                                               visible_only=visible, min_items=min_items))
            base["model_calls"] = ev.model_calls
            self.model_calls += ev.model_calls
            if ev.state == EP.EMPTY:
                said = ev.line
                rc = {"ok": True, "kind": "empty", "reason": ev.why, "items": 0,
                      "empty_state": said, "empty_by": ev.by}
                if ev.detail():
                    rc["empty_detail"] = ev.detail()
                note = {EP.BY_SITE: (f"the page is the site's own empty page for this input "
                                     f"({said[:80]!r}); an empty answer is the correct answer here"),
                        EP.BY_MODEL: (f"the check model read {said[:80]!r} as the site saying it "
                                      f"found nothing; an empty answer is the correct answer here")
                        }.get(ev.by, (f"the page reported no results for this query in its "
                                      f"own words ({said!r}); an empty answer is the "
                                      f"correct answer here"))
                return {**base, "success": True, "url": page.url,
                        "elapsed_ms": elapsed, "extracted": {"items": []},
                        "empty_state": said, "result_contract": rc, "note": note}
            return {**base, "success": False, "url": page.url, "elapsed_ms": elapsed,
                    "outcome_kind": "incomplete",
                    "extracted": extracted, "selector_matched": matched,
                    "settle": dict(getattr(page, "settle", {}) or {}),
                    **({"result_contract": {"ok": False, "kind": "refused", "items": 0,
                                            "reason": ev.why[:300],
                                            "empty_detail": ev.detail()}}
                       if ev.detail() else {}),
                    # the reason first: the cascade keeps the first 200 characters
                    "error": (f"the page does not say it found nothing for this query "
                              f"({ev.why}). The replay extracted nothing: the selector no "
                              f"longer matches this page, or the page changed. Reported "
                              f"as a FAILURE, not as an empty result.")}

        verdict = None
        if contract is not None:
            # a 1-2 item page passes only when every item's key names the query,
            # and then without the count floor (the compiled rung's sparse rule)
            verdict = RC.check_html(contract, page.dom,
                                    str(template.extract.get("items") or sel),
                                    dict(params or {}), items, visible_only=visible, root=tree,
                                    empty=empty)
            base["model_calls"] = int(base.get("model_calls") or 0) + verdict.model_calls
            self.model_calls += verdict.model_calls
            if not verdict.ok:
                return {**base, "success": False, "url": page.url, "elapsed_ms": elapsed,
                        "outcome_kind": "incomplete",
                        "extracted": extracted, "selector_matched": matched,
                        "result_contract": verdict.to_dict(),
                        "error": f"result contract refused: {verdict.reason}"}
        sparse = verdict is not None and verdict.kind == "sparse"

        if len(items) < min_items and not sparse:
            return {**base, "success": False, "url": page.url, "elapsed_ms": elapsed,
                    "outcome_kind": "incomplete",
                    "extracted": extracted, "selector_matched": matched,
                    "error": (f"postcondition failed: incomplete extraction has "
                              f"{len(items)} item(s), below recorded minimum {min_items}")}

        from anyuse.execution.api_step import check_output_identity
        identity_ok, identity_reason = check_output_identity(
            extracted, dict(params or {}), expect)
        if not identity_ok:
            return {**base, "success": False, "outcome_kind": "incomplete",
                    "url": page.url, "elapsed_ms": elapsed,
                    "extracted": extracted, "selector_matched": matched,
                    "error": f"postcondition identity failed: {identity_reason}"}

        if template.read_labels and template.extract.get("items"):
            try:
                extracted["labels"] = extract(page.dom, template.extract["items"], limit=100,
                                              visible_only=visible, labels=True)
            except ValueError:
                pass
        out = {**base, "success": True, "url": page.url, "elapsed_ms": elapsed,
               "extracted": extracted, "selector_matched": matched,
               "settle": dict(getattr(page, "settle", {}) or {}),
               "browser_only": bool(browser_only),
               "status": page.status}
        if verdict is not None:
            out["result_contract"] = verdict.to_dict()
        if capture_traffic and getattr(page, "network", None):
            out["evidence"] = {"final_url": page.url or url, "dom": page.dom,
                               "text": page.text, "network": list(page.network)}
        return out


def replay_hil_action(template: ReplayTemplate,
                      params: Optional[Dict[str, Any]] = None) -> dict:
    """Project a replay step into the shape ``hil_gate.classify_step`` consumes.

    Opening a URL and reading the DOM is a read, so the verb is ``read`` and the
    existing gate classifies it read-only — with the same keyword rule that
    promotes a URL mentioning ``delete`` or ``checkout``, which is a
    conservative outcome worth keeping.
    """
    try:
        url = template.render(params or {})
    except KeyError:
        url = template.url
    args: Dict[str, Any] = {"url": url, "method": "GET"}
    if template.notes:
        args["intent"] = template.notes
    return {"action": "read", "args": args}
