"""THE INPUT-vs-DRIFT CANARY of the repair ladder (OFF by default:
``LadderConfig(canary=True)``).

Without it, a tier-0 failure on a NEW input (no last known good answer for it) goes straight to the repair tiers: a
run that reached tier 1 used ~76 requests against ~9 for a plain run. The ladder's
sentinel compares an answer only with the SAME input's last known good (LKG) answer. The canary asks the cheaper
question first: does the tool still answer ANOTHER input it answered before? It re-runs ONE last known good input of
the same tool - the freshest one, never the failing input:

* the LKG input answers as before -> the verdict is ``INPUT``: the site has not drifted, the failure belongs to this
  input ("no results", "not found", "the variant is not listed", ...). No repair, no revision; the run keeps its
  failure, reported with the canary as evidence (``input_reason``);
* the LKG input fails too, or its answer collapsed -> ``DRIFT``: the ladder proceeds as without the canary.

The rule "answers as before" (``canary_verdict``, fixed code): the run is
``ok``; it is no known wrong answer and no flagged one (the promo / block sentinels); the ladder's SENTINEL against its
own LKG answer does not fire (no results / no echo / a list shorter than half / an early last page); the structure
reads at least half of the LKG run's results (the gate's coverage rule); and the answer still overlaps the LKG answer
(a product lookup: the same product URL or name; a list: at least one result URL in common).

**Only for READ-ONLY tools**: a re-run is allowed only for a tool whose every step is
read-only and idempotent. This is decided from the tool's own STEPS (``step_plan``: its start page, its search, its
result reads, its refinement / pager / inserted navigation, a declared ``effects`` class), each projected into the
shape the EXISTING human-in-the-loop gate classifies (``hil_gate.classify_step``: a GET is the verb ``read``, exactly
as ``api_step.api_hil_action`` projects an API step; typing, clicking, choosing are ``side_effecting``; a destructive
word - submit, pay, send, buy, delete, ... - in any of the step's strings is ``destructive``; an action the gate does
not know is ``destructive``) plus the web engine's gated controls (``web_arch.forbidden_control``: cart, checkout,
login, account, ...). Never from the tool's name or workflow label. The most severe step wins; a field the
classification does not know is an UNKNOWN effect. Anything but ``read_only`` gets NO re-run canary (fail closed;
``rerun_refusal`` gives the typed reason).

**The PROBE canary** (``probe_path``; ``LadderConfig(probe_canary=True)``, off by default) is the hook for the tools
that may not be re-run: it walks the recorded path only UP TO the first step that is not read-only and stops there -
it never triggers that step. It answers: is each page on the way reachable (no error status, no block), and is the
first irreversible step's control present with its recorded identity (a search input by name / id / index, a control
by tag and label)? It types no value, clicks nothing, submits nothing. A broken path is evidence of DRIFT; an intact
path is NOT an INPUT verdict (the probe never sees an answer). For the write / login / purchase tools to come, the
caller hands ``probe_path`` its own plan (the same step dicts, from the tool's manifest: each step's effect class and
its control identity).

Pure apart from the async page walk; no model, no file. The loop guards live in the ladder (``repair_ladder``).
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_compile as C
from anyuse.execution import web_arch as W
from anyuse.execution.hil_gate import StepClass, classify_step

#: the canary's verdicts
INPUT, DRIFT = "input", "drift"
#: no canary ran: the tool may not be re-run (its effects), no other input has a last known good answer, the failing
#: input has a positive last known good answer of its own (``LadderConfig.canary_own_lkg``)
REFUSED, NO_LKG, OWN_LKG = "refused", "no_lkg", "own_lkg"
#: the probe's verdicts (never INPUT: a probe sees no answer)
PATH_INTACT, PATH_BROKEN, UNDECIDED = "path_intact", "path_broken", "undecided"

#: the typed reasons a re-run canary is refused
NOT_READ_ONLY, UNKNOWN_EFFECT = "not_read_only", "unknown_effect"

READ_ONLY, SIDE_EFFECTING, DESTRUCTIVE = StepClass.READ_ONLY, StepClass.SIDE_EFFECTING, StepClass.DESTRUCTIVE
_RANK = {READ_ONLY: 0, SIDE_EFFECTING: 1, DESTRUCTIVE: 2}
#: the request methods that read (``api_step.READ_METHODS``)
READ_METHODS = frozenset({"GET", "HEAD"})

#: a compiled web tool's fields that hold no step: what it is, what it reads, how it checks - never something it does
#: on the site (``demo_compile`` / ``demo_breadth``, format versions 4-14; ``result_scope``: the hosts and paths
#: its results may link to - data the card reader checks against, no step)
METADATA_KEYS = frozenset({
    "version", "workflow", "site", "input", "model_calls", "output", "checks", "compiled_from", "hosts", "select",
    "fields", "top_k", "n", "k", "max_pages", "params", "applied", "result_scope"})
#: the fields that ARE steps (``step_plan`` projects each); any other field is an UNKNOWN effect
STEP_KEYS = ("start_url", "search", "region", "refine", "pager", "repair_nav", "effects")


# ---------------------------------------------------------------------------
# the tool's steps and their effect class
# ---------------------------------------------------------------------------

def _get(url: Any, **more: Any) -> Dict[str, Any]:
    """A GET of ``url`` as the HIL gate sees an API GET (``api_step.api_hil_action``): the verb ``read``."""
    return {"action": "read", "args": dict({"url": str(url or ""), "method": "GET"}, **more)}


def _req(url: Any, method: Any, **more: Any) -> Dict[str, Any]:
    m = str(method or "GET").upper()
    if m in READ_METHODS:
        return _get(url, **more)
    return {"action": f"api_{m.lower()}", "args": dict({"url": str(url or ""), "method": m}, **more)}


def step_plan(tool: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tool's steps in the order a run takes them, each ``{"step", "kind", "action", "identity", "url",
    "needs_value", "gated"}``: ``kind`` is ``goto`` (a GET the run opens), ``read`` (a read of the page it is on),
    ``follow`` (a same-site link / result it opens), ``type`` / ``key`` / ``click`` (an action on a control) or
    ``unknown``; ``action`` is the HIL gate's shape; ``gated`` is ``web_arch.forbidden_control``'s reason for a gated
    control / link (cart, checkout, login, ...) or "". A typed search's steps also carry ``named14``: the tool's
    replay finds its input by the rule of web tools before version 15 too (``cdp_guard.replay_inputs``). From the
    tool's fields only - never its name."""
    plan: List[Dict[str, Any]] = []

    def add(step: str, kind: str, action: Dict[str, Any], identity: Optional[Dict[str, Any]] = None, url: str = "",
            needs_value: bool = False, text: Any = "", **more: Any) -> None:
        gated = W.forbidden_control(text, url) if (text or url) and kind in ("goto", "follow", "click") else None
        plan.append(dict({"step": step, "kind": kind, "action": action, "identity": dict(identity or {}), "url": url,
                          "needs_value": needs_value, "gated": gated or ""}, **more))

    if not isinstance(tool, dict):
        add("tool", "unknown", {"action": "unknown:tool", "args": {}})
        return plan
    start = str(tool.get("start_url") or "")
    if "start_url" in tool:
        add("start_url", "goto", _get(start), url=start)
    s = tool.get("search")
    if "search" in tool:
        if not isinstance(s, dict):
            add("search", "unknown", {"action": "unknown:search", "args": {}})
        else:
            mode = s.get("mode")
            if s.get("start_url") and str(s.get("start_url")) != start:
                add("search.start_url", "goto", _get(s["start_url"]), url=str(s["start_url"]))
            if mode == "url":
                add("search", "goto", _req(s.get("template"), s.get("method")), url=str(s.get("template") or ""),
                    needs_value=True)
            elif mode == "typed":
                rec = s.get("input") or {}
                inp = {k: v for k, v in rec.items() if k in ("index", "name", "id")}
                # the identity the replay finds the input by: its name / id / index, the accessibility tree's mark
                ident = dict(inp, **({"ax": True} if rec.get("ax") else {}))
                old_rule = not C.v15(tool)
                add("search", "type", {"action": "type", "args": {"input": inp}}, identity=ident, named14=old_rule)
                add("search.submit", "key", {"action": "key", "args": {"key": "Enter", "input": inp}}, identity=ident,
                    named14=old_rule)
                if C.v14(tool):
                    # version 14: when Enter moves nothing, the replay clicks the search's own control (the one the
                    # demonstration clicked first), and when that moves nothing either, loads the URL the input's own
                    # GET form gives (``cdp_guard.type_search``)
                    label = str(rec.get("submit_label") or "")
                    add("search.control", "click", {"action": "click", "args": {"text": label} if label else {}},
                        identity={"text": label} if label else {}, text=label)
                    add("search.form", "goto", _get("", form="the input's own GET form"), needs_value=True)
            else:
                add("search", "unknown", {"action": f"unknown:search.mode={mode}", "args": {}})
            for t in s.get("fallback_templates") or []:
                add("search.fallback", "goto", _req(t, s.get("method")), url=str(t), needs_value=True)
            extra = set(s) - {"mode", "template", "fallback_templates", "start_url", "input", "overlay", "method"}
            for k in sorted(extra):
                add(f"search.{k}", "unknown", {"action": f"unknown:search.{k}", "args": {}})
    r = tool.get("region")
    if "region" in tool:
        if not isinstance(r, dict):
            add("region", "unknown", {"action": "unknown:region", "args": {}})
        else:
            add("region", "read", {"action": "read", "args": {"region": str(r.get("template") or "")}})
            if "select" in tool or "fields" in tool:
                # a product lookup / a visit opens result pages of the region (a GET of a result link)
                add("region.open", "follow", _get(r.get("template")), url=str(r.get("template") or ""))
    ref = tool.get("refine")
    if "refine" in tool:
        if not isinstance(ref, dict):
            add("refine", "unknown", {"action": "unknown:refine", "args": {}})
        elif ref.get("mode") == "url":
            add("refine", "goto", _req("", ref.get("method"), params=dict(ref.get("params") or {})), needs_value=True)
        elif ref.get("mode") == "controls":
            for i, ctl in enumerate(ref.get("controls") or []):
                ident = {k: ctl.get(k) for k in ("tag", "text", "option_text") if ctl.get(k) not in (None, "")}
                if ctl.get("tag") == "a":
                    add(f"refine.controls[{i}]", "follow", _get("", link=str(ctl.get("text") or "")), identity=ident,
                        text=ctl.get("text"))
                else:
                    # a drop-down choice, a checkbox, a button: an action on a control (side effecting unless the gate
                    # finds a destructive word)
                    add(f"refine.controls[{i}]", "click", {"action": "click", "args": ident}, identity=ident,
                        text=ctl.get("text") or ctl.get("option_text"))
        else:
            add("refine", "unknown", {"action": f"unknown:refine.mode={ref.get('mode')}", "args": {}})
    pg = tool.get("pager")
    if "pager" in tool:
        if not isinstance(pg, dict):
            add("pager", "unknown", {"action": "unknown:pager", "args": {}})
        elif pg.get("mode") in ("param", "path_append", "path_segment"):
            add("pager", "goto", _get("", key=str(pg.get("key") or "")), needs_value=True)
        elif pg.get("mode") == "link":
            # version 10 compiles a link pager only from an <a> (a next-page / page-2 link): a GET of its href
            lk = pg.get("link") or {}
            add("pager", "follow", _get("", link=str(lk.get("text") or ""), rel=str(lk.get("rel") or "")),
                identity={"tag": "a", "text": lk.get("text"), "rel": lk.get("rel")}, text=lk.get("text"))
        else:
            add("pager", "unknown", {"action": f"unknown:pager.mode={pg.get('mode')}", "args": {}})
    navs = tool.get("repair_nav") if "repair_nav" in tool else []
    if not isinstance(navs, list):
        add("repair_nav", "unknown", {"action": "unknown:repair_nav", "args": {}})
        navs = []
    for i, nav in enumerate(navs):
        lk = ((nav.get("link") or {}) if isinstance(nav, dict) else {})
        add(f"repair_nav[{i}]", "follow", _get(lk.get("path") or "", link=str(lk.get("text") or "")),
            identity={"tag": "a", "text": lk.get("text"), "path": lk.get("path")}, url=str(lk.get("path") or ""),
            text=lk.get("text"))
    if "effects" in tool:
        # a DECLARED effect (a skill manifest's ``effects``: class, reversible): it can only make the tool stricter
        e = tool.get("effects") if isinstance(tool.get("effects"), dict) else {}
        cls = str(e.get("class") or "")
        if cls == READ_ONLY and e.get("reversible") is not False:
            add("effects", "read", {"action": "read", "args": {"declared": cls}})
        elif cls in (SIDE_EFFECTING, DESTRUCTIVE):
            add("effects", "declared", {"action": f"declared:{cls}", "args": {}, "declared_class": cls})
        else:
            add("effects", "unknown", {"action": f"unknown:effects.class={cls or None}", "args": {}})
    for k in sorted(set(tool) - METADATA_KEYS - set(STEP_KEYS)):
        add(k, "unknown", {"action": f"unknown:{k}", "args": {}})
    return plan


def classify(plan_step: Dict[str, Any]) -> Tuple[str, str]:
    """One step's effect class and why: the HIL gate on its action (a declared class kept as declared), promoted to
    destructive for a gated control / link (``web_arch.forbidden_control``)."""
    a = plan_step.get("action") or {}
    if a.get("declared_class"):
        return str(a["declared_class"]), f"declared {a['declared_class']}"
    cls = classify_step(a)
    why = f"the HIL gate: {cls} ({a.get('action')})"
    if plan_step.get("gated"):
        return DESTRUCTIVE, f"a gated control / link: {plan_step['gated']}"
    return cls, why


def effect_class(tool: Dict[str, Any]) -> Dict[str, Any]:
    """The tool's effect class (the MOST SEVERE of its steps'), every step's class, the first step that is not
    read-only and the steps the classification does not know (an unknown effect)."""
    plan = step_plan(tool)
    rows, worst, first, unknown = [], READ_ONLY, None, []
    for i, s in enumerate(plan):
        cls, why = classify(s)
        rows.append({"step": s["step"], "kind": s["kind"], "class": cls, "why": why})
        if s["kind"] == "unknown":
            unknown.append(s["step"])
        if _RANK.get(cls, 2) > _RANK[worst]:
            worst = cls if cls in _RANK else DESTRUCTIVE
        if first is None and cls != READ_ONLY:
            first = i
    if not plan:
        worst = DESTRUCTIVE
        unknown.append("(no step)")
    return {"class": worst, "steps": rows, "first_irreversible": first, "unknown": unknown}


def rerun_refusal(tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """None when the tool may be re-run by the canary (every step read-only); else the TYPED reason:
    ``{"code": "unknown_effect" | "not_read_only", "class", "step", "why"}``. Fail closed."""
    ec = effect_class(tool)
    if ec["unknown"]:
        st = ec["unknown"][0]
        return {"code": UNKNOWN_EFFECT, "class": DESTRUCTIVE, "step": st,
                "why": f"the effect of {st!r} is unknown (fail closed: no re-run)"}
    if ec["class"] != READ_ONLY:
        row = ec["steps"][ec["first_irreversible"]]
        return {"code": NOT_READ_ONLY, "class": ec["class"], "step": row["step"],
                "why": f"step {row['step']!r} is {row['class']} ({row['why']}): no re-run"}
    return None


# ---------------------------------------------------------------------------
# the re-run canary: which input, and its verdict
# ---------------------------------------------------------------------------

def positive(lkg: Optional[Dict[str, Any]]) -> bool:
    """A last known good answer that ANSWERED something (a product, a non-empty list): "no results" answering as before
    proves nothing about the search or the result structure."""
    a = (lkg or {}).get("answer")
    if not isinstance(a, dict) or a.get("no_results"):
        return False
    return bool(a.get("url") or a.get("results") or a.get("items"))


def pick_canary(lkgs: Dict[str, Dict[str, Any]], value: str) -> Optional[str]:
    """The canary's input: the FRESHEST other input with a positive last known good answer (never ``value``); ties by
    the input's text (deterministic). None: no canary."""
    rows = [(v, x) for v, x in (lkgs or {}).items() if str(v) != str(value) and positive(x)]
    if not rows:
        return None
    rows.sort(key=lambda vx: (str(vx[1].get("at") or ""), str(vx[0])), reverse=True)
    return rows[0][0]


def _overlap(workflow: str, ans: Optional[Dict[str, Any]], lkg_ans: Dict[str, Any]) -> Tuple[bool, str]:
    if workflow == "product_lookup" or ("url" in lkg_ans and "results" not in lkg_ans and "items" not in lkg_ans):
        a = ans or {}
        if a.get("url") and lkg_ans.get("url") and C._norm(str(a["url"])) == C._norm(str(lkg_ans["url"])):
            return True, "the same product URL"
        if a.get("name") and lkg_ans.get("name") and W.fold(a["name"]) == W.fold(lkg_ans["name"]):
            return True, "the same product name"
        return False, "another product than its last known good answer"
    key = "items" if "items" in lkg_ans else "results"
    if key == "items":
        mine = {C._norm(str(r.get("url") or "")) for r in (ans or {}).get("items") or [] if r.get("url")}
        old = {C._norm(str(r.get("url") or "")) for r in lkg_ans.get("items") or [] if r.get("url")}
    else:
        mine = {C._norm(str(u)) for u in (ans or {}).get("results") or [] if u}
        old = {C._norm(str(u)) for u in lkg_ans.get("results") or [] if u}
    common = len(mine & old)
    if common:
        return True, f"{common} result(s) in common with its last known good answer"
    return False, "no result in common with its last known good answer"


def canary_verdict(tool: Dict[str, Any], value: str, out: Dict[str, Any], lkg: Optional[Dict[str, Any]],
                   known_wrong: bool = False, flagged: str = "") -> Tuple[str, str]:
    """``(INPUT | DRIFT, why)`` for the canary's run ``out`` of ``tool`` on its input ``value`` against that input's
    last known good answer ``lkg`` (fixed code; see the module doc). ``known_wrong``: the verdict store knows the answer
    to be wrong; ``flagged``: the promo / block sentinel's reason on the canary's page."""
    from anyuse.execution import repair_ladder as L
    wf = str(tool.get("workflow") or "")
    st = str(out.get("status") or "")
    if st != "ok":
        return DRIFT, f"the known-good input fails too ({st}: {str(out.get('why') or '')[:120]})"
    if known_wrong:
        return DRIFT, "the known-good input's answer is a known wrong one"
    if flagged:
        return DRIFT, f"the known-good input's answer is flagged ({flagged[:120]})"
    if not positive(lkg):
        return DRIFT, "no positive last known good answer to compare with"
    collapsed, _steps = L.sentinel(tool, value, out, lkg)
    if collapsed:
        return DRIFT, f"the known-good input's answer collapsed ({collapsed})"
    n0, n = (lkg or {}).get("n_items"), L.n_items(out)
    if isinstance(n0, int) and n0 >= 2 and n is not None and n * 2 < n0:
        return DRIFT, f"the structure reads {n} result(s) where the known-good run read {n0}"
    ok, why = _overlap(wf, L.answer_of(wf, out), (lkg or {}).get("answer") or {})
    if not ok:
        return DRIFT, f"the known-good input answers otherwise ({why})"
    return INPUT, f"the known-good input answers as before ({why})"


def input_reason(out: Dict[str, Any]) -> str:
    """The failing input's own failure in plain words (the run's check, its ``why`` kept beside it)."""
    why = str(out.get("why") or "").strip()
    w = why.lower()
    if out.get("status") == "no_results" or w.startswith("ladder sentinel: no results"):
        return "no results for this input (it had results before)" if w.startswith("ladder sentinel") else \
            "no results for this input"
    if w.startswith("ladder sentinel:"):
        return "this input's answer changed against its last known good one: " + why.split(":", 1)[1].strip()[:120]
    core = w[len("incomplete:"):].strip() if w.startswith("incomplete:") else w
    # (readout wording only) a result the tool cannot read is checked FIRST - a run
    # whose why says "no result names the input; a result the compiled structure cannot read names it" is not "not
    # found"; a membership failure (no item of the compiled region) claims nothing about the site's results
    if "could not be read" in core or "cannot read" in core or "result card" in core:
        return "some results for this input could not be read (a layout the tool does not read)"
    if core.startswith("identity") and "variant" in core:
        return "the asked-for variant is not listed"
    if core.startswith("identity"):
        return "not found: the opened product is not the input's"
    if core.startswith("membership"):
        return "the tool reads no result for this input, and the page states no 'no results'"
    if core.startswith("query") or "no result names the input" in core:
        return "not found: no result the tool reads names the input"
    if core.startswith("conflict"):
        return "the product page contradicts itself (a CONFLICT); not answered"
    if core.startswith("ambiguous"):
        return "ambiguous: several results match the input equally"
    head = core.split(":", 1)[0].strip()
    return f"the run failed for this input ({head or out.get('status')})"


def cached(rows: Sequence[Dict[str, Any]], rev: int, now: _dt.datetime, ttl_min: float) -> Optional[Dict[str, Any]]:
    """The latest canary verdict of revision ``rev`` younger than ``ttl_min`` minutes (one canary per tool per N
    minutes), else None. Only verdicts (INPUT / DRIFT) are cached."""
    for r in reversed(list(rows or [])):
        if r.get("verdict") not in (INPUT, DRIFT) or r.get("cached") or int(r.get("rev", -1)) != int(rev):
            continue
        try:
            at = _dt.datetime.strptime(str(r.get("at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
        if _dt.timedelta(0) <= now - at <= _dt.timedelta(minutes=float(ttl_min)):
            return r
        return None
    return None


def same_bytes(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Are two tools byte-identical (their canonical JSON; ``demo_compile.tool_digest``)?"""
    return C.tool_digest(a) == C.tool_digest(b)


def canonical(tool: Dict[str, Any]) -> str:
    return json.dumps(tool, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# the probe canary: the recorded path up to the first step that is not read-only, never that step
# ---------------------------------------------------------------------------

async def _reachable(page) -> Tuple[bool, str]:
    from anyuse.execution import demo_tool as T
    from anyuse.execution import web_arch_run as RR
    try:
        net = await page.network(want_bodies=False)
    except Exception:                                  # noqa: BLE001 - a read never fails the probe
        net = []
    docs = [n for n in net or [] if str(n.get("resource_type") or "") == "Document"]
    bad = next((n for n in docs if isinstance(n.get("status"), int) and int(n["status"]) >= 400), None)
    if bad is not None:
        return False, f"the page answers {bad.get('status')}"
    read = await RR.read_page(page)
    blk = await T._blocked(page, read)
    if blk:
        return False, f"blocked: {blk[:120]}"
    return True, ""


async def control_present(page, step: Dict[str, Any]) -> Tuple[bool, str]:
    """Is the step's control on the current page with its recorded identity? A search input: the same name or id,
    else, when the tool recorded neither, the same index, among the inputs the tool's own replay chooses from
    (``cdp_guard.replay_inputs``: the search-like ones, the one with the recorded name or id whatever the rules call
    it now, and for a tool before version 15 the ones its own rule named; ``page.recorded_inputs``); a control: the
    same tag and label (``demo_breadth.apply_control``'s match). A local read only - nothing is typed or clicked."""
    from anyuse.execution import web_arch_run as RR
    ident = step.get("identity") or {}
    if step.get("kind") in ("type", "key"):
        try:
            if hasattr(page, "recorded_inputs"):
                inputs = list(await page.recorded_inputs(ident, bool(step.get("named14"))) or [])
            else:
                inputs = [i for i in (await page.search_inputs() or []) if i.get("visible")]
        except Exception:                              # noqa: BLE001
            inputs = []
        same = [i for i in inputs if (ident.get("name") and i.get("name") == ident["name"]) or
                (ident.get("id") and i.get("id") == ident["id"])]
        if not same and "index" in ident and not (ident.get("name") or ident.get("id")):
            same = [i for i in inputs if i.get("index") == ident.get("index")]
        return (True, f"the input {ident} is present") if same else (False, f"the input {ident} is not on the page")
    read = await RR.read_page(page)
    want = W.fold(ident.get("option_text") or ident.get("text") or "")
    for e in read.get("elements") or []:
        if ident.get("tag") and str(e.get("tag") or "") != ident["tag"]:
            continue
        labels = [W.fold(e.get("text"))] + [W.fold(o.get("text")) for o in e.get("options") or []]
        if want and want in labels:
            return True, f"the control {ident} is present"
    return False, f"the control {ident} is not on the page"


async def probe_path(page, tool_or_plan: Any, settle: Callable, value: Optional[str] = None) -> Dict[str, Any]:
    """The PROBE canary on ``page``: the plan's steps in order while they are read-only (a GET is opened and checked
    reachable; a read step is a read), then the FIRST step that is not read-only is only LOOKED AT - its control
    present with its identity - and the walk stops. ``value``: an input for a read-only GET that needs one (a last known
    good input; never needed for a step with an effect). ``{"verdict": path_intact | path_broken | undecided,
    "stopped_before", "checks", "gotos"}``. Never types, clicks, chooses or submits."""
    plan = list(tool_or_plan) if isinstance(tool_or_plan, (list, tuple)) else step_plan(tool_or_plan)
    checks: List[Dict[str, Any]] = []
    res: Dict[str, Any] = {"verdict": UNDECIDED, "stopped_before": None, "checks": checks}
    for s in plan:
        cls, why = classify(s)
        if cls != READ_ONLY:
            ok, w = await control_present(page, s)
            checks.append({"step": s["step"], "class": cls, "present": ok, "why": w, "triggered": False})
            res.update(verdict=PATH_INTACT if ok else PATH_BROKEN, stopped_before=s["step"],
                       why=(f"the path up to {s['step']!r} ({cls}) is intact; it was not triggered" if ok else
                            f"{w} (the step was not triggered)"))
            break
        if s["kind"] == "goto":
            url = str(s.get("url") or "")
            if s.get("needs_value"):
                if value is None or "{q}" not in url:
                    res.update(stopped_before=s["step"], why="a read-only step needs an input value the probe does "
                                                            "not have")
                    break
                url = url.replace("{q}", urllib.parse.quote_plus(str(value)))
            if not url.startswith("http"):
                res.update(stopped_before=s["step"], why="a read-only step without a URL of its own")
                break
            await page.goto(url)
            await settle(page)
            ok, w = await _reachable(page)
            checks.append({"step": s["step"], "class": cls, "reachable": ok, "why": w, "url": url[:200]})
            if not ok:
                res.update(verdict=PATH_BROKEN, stopped_before=s["step"], why=w)
                break
        elif s["kind"] == "read":
            checks.append({"step": s["step"], "class": cls, "read": True})
        else:
            # a result / link to follow: the probe follows nothing (its target depends on an answer)
            res.update(stopped_before=s["step"], why="the next read-only step follows a result or link; the probe "
                                                    "stops before it")
            break
    else:
        res["why"] = "every step is read-only: nothing irreversible to stop before (the re-run canary applies)"
    res["gotos"] = len(getattr(page, "gotos", []) or [])
    return res
