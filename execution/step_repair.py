"""Step-level drift repair: re-learn only the step that broke, then re-prove.

The repeatability study found drift
detection exact (the failing step, every time, no silent output) but repair
not step-local: a broken selector sent the tool back through the whole
first-run pipeline (agent, replays, compile, proof) at 77.7-141.6 % of the
first run's tokens, and only 1 of 3 repairs re-proved exactly.

This module localises the failure to ONE step of the compiled request and
re-learns only that step from ONE fresh capture:

* **request_shape** — the proved request no longer answers (non-2xx, a
  transport error, another content type). Re-learned from a navigation
  capture (``navigate``, e.g. the recorded UI path replayed in the container
  browser): the request that now carries the answer, compiled from that one
  capture.
* **parameter_binding** — the request answers but the page no longer answers
  THE QUERY (the value is not named, none of the anchor's items occur): the
  value moved to another parameter or path segment. Re-learned from the
  navigation capture's URL (``learn_loop.url_template_for``).
* **extraction_region** — the page answers the query but the extraction finds
  nothing, or other things: the result region moved or was renamed (a class,
  a wrapper chain, a JSON key or list path). Re-derived on the fresh page
  only, reusing the request.
* **record_segmentation** — the extraction still matches, but its items are
  merged or split relative to the anchor. Re-derived like the region.

The anchor is what makes one capture enough: the tool's last good answer
(``record["drift_anchor"]``: a value and the first items the tool returned
for it, written on every successful list run, and at compile time from a
passing validation page). The fresh page for that value must reproduce the
anchor's items EXACTLY (normalised equality, in order, >= 80 %), which keeps
the item granularity the tool had: rows stay rows, cells stay cells.

A re-learned tool is not trusted until it is re-proved on held-out inputs
with the SHARED split (``proof_split.assign``: the anchor page compiles, one
page selects, >= 2 validation pages are never seen by fitting or
selection), against ground truth derived from the fit pages only
(``learn_loop.derive_ground_truth``), with the runtime result contract
attached (execution/result_contract.py). The new status is never stronger than
the one it replaces. When localisation or the step's re-derivation fails, and
only then, the caller's ``full_pipeline`` runs.

No model is called here. Every request goes through the product's
``ApiStepExecutor`` (policy, robots, HIL, blocked-host checks unchanged).
"""
from __future__ import annotations

import copy
import datetime
import types
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

STEPS = ("request_shape", "parameter_binding", "extraction_region", "record_segmentation")
#: items kept in an anchor
ANCHOR_ITEMS = 10
#: an anchor needs at least this many items (a 1-2 item answer is too little
#: evidence to re-derive an extraction from)
MIN_ANCHOR = 3
#: share of the anchor's items the fresh page must reproduce exactly
MIN_ANCHOR_COVER = 0.8


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# anchors
# ---------------------------------------------------------------------------

def anchor_from(value: str, items: Any) -> Optional[Dict[str, Any]]:
    """An anchor from a successful LIST answer, or ``None``."""
    seq = items if isinstance(items, list) else []
    texts = [str(x) for x in seq if x is not None and str(x).strip()]
    if not value or len(texts) < MIN_ANCHOR:
        return None
    return {"value": str(value), "items": texts[:ANCHOR_ITEMS], "count": len(texts),
            "at": _now()}


def note_success(record: Dict[str, Any], value: str, result: Dict[str, Any]) -> bool:
    """Keep the tool's last good list answer on its record (the drift anchor).
    Only a list verdict of the runtime contract (>= 3 items) qualifies."""
    rc = result.get("result_contract") or {}
    if rc and rc.get("kind") != "list":
        return False
    items = (result.get("extracted") or {}).get("items")
    anchor = anchor_from(value, items)
    if anchor is None:
        return False
    record["drift_anchor"] = anchor
    return True


def anchor_of(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    a = record.get("drift_anchor")
    if isinstance(a, dict) and a.get("value") and len(a.get("items") or []) >= MIN_ANCHOR:
        return a
    return None


def answer_holds(anchor: Dict[str, Any], result: Dict[str, Any]) -> Tuple[bool, str]:
    """Does a run of the tool's request for the anchor's value give the tool's last good
    answer again? The run must pass the request's checks (its result contract among them)
    and repeat at least :data:`MIN_ANCHOR_COVER` of the anchor's items exactly, in order."""
    value = str(anchor.get("value") or "")
    if not (result or {}).get("success"):
        return False, (f"the request for {value!r} failed: "
                       f"{str((result or {}).get('error') or 'no answer')[:200]}")
    items = ((result or {}).get("extracted") or {}).get("items")
    share, _hit = exact_cover(anchor.get("items") or [], items if isinstance(items, list) else [])
    if share < MIN_ANCHOR_COVER:
        return False, (f"the answer for {value!r} repeats {share:.0%} of the tool's last good "
                       f"answer, {MIN_ANCHOR_COVER:.0%} needed")
    return True, f"the answer for {value!r} repeats {share:.0%} of the tool's last good answer"


# ---------------------------------------------------------------------------
# matching the anchor
# ---------------------------------------------------------------------------

def _norm(x: Any) -> str:
    from anyuse.execution.api_compile import normalise
    return normalise(x)


def exact_cover(anchor_items: Sequence[Any], got: Sequence[Any]) -> Tuple[float, int]:
    """Share of the anchor items found in ``got`` by normalised EQUALITY, in
    order (a greedy in-order match), and the number matched."""
    want = [_norm(a) for a in anchor_items if _norm(a)]
    have = [_norm(g) for g in got]
    if not want:
        return 0.0, 0
    j = 0
    hit = 0
    for w in want:
        k = j
        while k < len(have) and have[k] != w:
            k += 1
        if k < len(have):
            hit += 1
            j = k + 1
    return hit / len(want), hit


# ---------------------------------------------------------------------------
# one fresh capture, through the product's executor
# ---------------------------------------------------------------------------

@dataclass
class Capture:
    value: str
    ok: bool = False
    url: str = ""
    status: Optional[int] = None
    body: str = ""
    data: Any = None
    error: str = ""
    blocked: bool = False


def _probe(template: Any) -> Any:
    """The template's request with no extraction and no result postcondition:
    it answers "does the request still answer?" through every policy check."""
    from anyuse.execution.api_step import RequestTemplate
    d = template.to_dict()
    expect = {k: v for k, v in (d.get("expect") or {}).items()
              if k in ("status_in", "content_type")}
    kind = d.get("extract_kind") or "json"
    if kind == "embedded_json":
        kind = "html"
        expect["content_type"] = "html"
    # A response capture has no public field-result contract. Retaining a
    # field tool's shape would require the extraction deliberately removed here.
    d.update({"extract": {}, "expect": expect, "extract_kind": kind, "embedded": {},
              "shape": "list"})
    return RequestTemplate.from_dict(d)


def capture(template: Any, value: str, executor, param: str = "") -> Capture:
    params = {param or (template.params[0] if template.params else "q"): value}
    res = executor.execute(_probe(template), params)
    body = res.get("data")
    text = body if isinstance(body, str) else ""
    return Capture(value=value, ok=bool(res.get("success")), url=str(res.get("url") or ""),
                   status=res.get("status"), body=text,
                   data=None if isinstance(body, str) else body,
                   error=str(res.get("error") or ""),
                   blocked=bool(res.get("blocked") or res.get("requires_human")))


# ---------------------------------------------------------------------------
# localisation
# ---------------------------------------------------------------------------

@dataclass
class Localisation:
    step: str                        # one of STEPS, "none" or "unknown"
    evidence: str
    reuse_request: bool = False


def _visible(markup: str, implied_end_tags: bool = False) -> str:
    from anyuse.execution.html_extract import parse
    try:
        return parse(markup, implied_end_tags=implied_end_tags).text()
    except ValueError:
        return ""


def _old_items(template: Any, cap: Capture) -> List[str]:
    """What the tool's own extraction reads from ``cap``, on the tree the tool
    was proved on (``RequestTemplate.html_parser``)."""
    from anyuse.execution.api_step import json_path
    sel = str((template.extract or {}).get("items") or "")
    if template.extract_kind == "html":
        from anyuse.execution.html_extract import extract
        rules = bool(getattr(template, "implied_end_tags", False))
        try:
            return extract(cap.body, sel, implied_end_tags=rules) if sel else []
        except ValueError:
            return []
    data = cap.data
    if template.extract_kind == "embedded_json":
        from anyuse.execution.embedded_json import select_blob
        data = select_blob(cap.body, template.embedded)
    got = json_path(data, sel) if data is not None and sel else None
    return [str(x) for x in got] if isinstance(got, list) else []


def _answers_query(template: Any, cap: Capture, value: str, anchor_items: Sequence[str]) -> bool:
    from anyuse.execution.api_verify import names_value
    if cap.data is not None:
        from anyuse.execution.api_step import json_leaves
        blob = " ".join(str(v) for _p, v in json_leaves(cap.data))
    else:
        blob = _visible(cap.body, implied_end_tags=True)       # a raw response body
    if names_value(blob, value):
        return True
    low = _norm(blob)
    return any(_norm(a) and _norm(a) in low for a in anchor_items)


def localise(template: Any, cap: Capture, anchor: Dict[str, Any]) -> Localisation:
    """Which step broke, from one fresh capture of the anchor's value."""
    if cap.blocked:
        return Localisation("unknown", f"the site asked for a human or policy refused: "
                                       f"{cap.error}")
    if not cap.ok:
        return Localisation("request_shape", f"the proved request no longer answers: "
                                             f"{cap.error or cap.status}")
    items = anchor.get("items") or []
    if not _answers_query(template, cap, anchor["value"], items):
        return Localisation("parameter_binding", (
            f"the request answers (HTTP {cap.status}) but the page neither names "
            f"{anchor['value']!r} nor shows any of the anchor's items: the value no longer "
            f"reaches the site where the template puts it"), reuse_request=False)
    got = _old_items(template, cap)
    share, _hit = exact_cover(items, got)
    if share >= MIN_ANCHOR_COVER:
        return Localisation("none", (f"the proved extraction still reproduces {share:.0%} of "
                                     f"the anchor's items on a fresh page; not a drift of "
                                     f"this tool's steps"), reuse_request=True)
    if not got:
        return Localisation("extraction_region", (
            f"the request answers and the page names the query, but the extraction "
            f"matches nothing: the result region moved or was renamed"), reuse_request=True)
    have = [_norm(g) for g in got]
    want = [_norm(a) for a in items]
    merged = any(sum(1 for w in want if w and w in h) >= 2 for h in have)
    split = any(sum(1 for h in have if h and h in w and h != w) >= 2 for w in want)
    if merged or split:
        return Localisation("record_segmentation", (
            f"the extraction still matches {len(got)} item(s) but they "
            f"{'merge' if merged else 'split'} the anchor's records"), reuse_request=True)
    return Localisation("extraction_region", (
        f"the extraction matches {len(got)} item(s) that are not the anchor's items "
        f"({share:.0%} exact): it now reads another region"), reuse_request=True)


# ---------------------------------------------------------------------------
# re-derivation of the extraction from the one capture
# ---------------------------------------------------------------------------

def _structural_variants(selector: str) -> List[str]:
    """The old selector with classes dropped per step and shorter suffixes:
    what survives a renamed class or a moved container."""
    from anyuse.execution.html_extract import parse_selector
    toks = selector.split()
    if ">" in toks:
        return []
    steps = parse_selector(selector)

    def render(chain):
        out = []
        for tag, classes in chain:
            ident = "".join("#" + c[1:] for c in classes if c.startswith("#"))
            out.append(tag + ident + "".join("." + c for c in classes if not c.startswith("#")))
        return " ".join(out)
    out: List[str] = []
    for k in range(1, len(steps) + 1):
        suffix = steps[-k:]
        out.append(render(suffix))
        out.append(render([(t, ()) for t, _c in suffix]))
        for i in range(len(suffix)):
            out.append(render([(t, () if j == i else c) for j, (t, c) in enumerate(suffix)]))
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _confined(body: str, selector: str, max_rows: int = 2000) -> bool:
    """Every match sits inside ONE element below ``<body>``: a long result
    list with several fields per record is still one region; a bare tag
    spread over the page (header, footer, results) is not. ``body`` is a raw
    response, read with the implied end tags."""
    from anyuse.execution.html_extract import parse, select
    from anyuse.execution.result_contract import _lca
    try:
        nodes = select(parse(body, implied_end_tags=True), selector)
    except ValueError:
        return False
    if not (0 < len(nodes) <= max_rows):
        return False
    lca = _lca(nodes) if len(nodes) > 1 else nodes[0].parent
    return lca is not None and getattr(lca, "tag", "") not in ("", "html", "body")


def _similarity(a: str, b: str) -> float:
    ta, tb = set(a.replace(".", " ").split()), set(b.replace(".", " ").split())
    return len(ta & tb) / max(1, len(ta | tb))


def html_candidates(body: str, anchor: Dict[str, Any], old_selector: str
                    ) -> List[Tuple[Tuple, str, List[str]]]:
    """Every selector that reproduces the anchor on ``body``, best first:
    ``[(score, selector, items)]``. ``body`` is the raw response of one
    capture, read with the implied end tags as a browser reads it; a template
    built from a candidate records that tree (:func:`with_extraction`)."""
    from anyuse.execution.html_extract import (derive_selector, derive_selector_repeated,
                                               extract, selector_is_one_list,
                                               selector_is_specific)
    items = anchor.get("items") or []
    count = int(anchor.get("count") or len(items))
    pool = list(_structural_variants(old_selector))
    for fn in (derive_selector, derive_selector_repeated):
        try:
            sel = fn(body, items, implied_end_tags=True)[0]
        except ValueError:
            sel = ""
        if sel and sel not in pool:
            pool.append(sel)
    out = []
    for sel in pool:
        try:
            got = extract(body, sel, limit=200, implied_end_tags=True)
        except ValueError:
            continue
        if not got:
            continue
        if not (selector_is_specific(body, sel, 60, implied_end_tags=True)
                or selector_is_one_list(body, sel, implied_end_tags=True)
                or _confined(body, sel)):
            continue
        share, _ = exact_cover(items, got)
        if share < MIN_ANCHOR_COVER:
            continue
        prefix = sum(1 for a, g in zip(items, got) if _norm(a) == _norm(g))
        score = (round(share, 3), prefix, -abs(len(got) - count),
                 round(_similarity(sel, old_selector), 3), -len(sel))
        out.append((score, sel, got[:50]))
    out.sort(key=lambda r: r[0], reverse=True)
    return out


def _generalise(path: str) -> str:
    import re
    return re.sub(r"\[\d+\]", "[*]", path)


def json_candidates(data: Any, anchor: Dict[str, Any]) -> List[Tuple[Tuple, str, List[str]]]:
    """Every JSON leaf path (``a.b[*].c``) whose values reproduce the anchor."""
    from anyuse.execution.api_step import json_leaves, json_path
    items = anchor.get("items") or []
    count = int(anchor.get("count") or len(items))
    paths: List[str] = []
    for p, _v in json_leaves(data):
        g = _generalise(p)
        if "[*]" in g and g not in paths:
            paths.append(g)
    out = []
    for g in paths:
        got = json_path(data, g)
        if not isinstance(got, list) or not got:
            continue
        got = [str(x) for x in got if x is not None]
        share, _ = exact_cover(items, got)
        if share < MIN_ANCHOR_COVER:
            continue
        prefix = sum(1 for a, v in zip(items, got) if _norm(a) == _norm(v))
        out.append(((round(share, 3), prefix, -abs(len(got) - count), -g.count(".")),
                    g, got[:50]))
    out.sort(key=lambda r: r[0], reverse=True)
    return out


def with_extraction(template: Any, selector_or_path: str, how: str) -> Any:
    """A copy of ``template`` reading ``selector_or_path``; request unchanged.
    An HTML copy reads the tree :func:`html_candidates` derived its selectors
    on (``html_parser``)."""
    from anyuse.execution import result_contract as RC
    from anyuse.execution.api_compile import _split_list_path
    from anyuse.execution.api_step import RequestTemplate
    from anyuse.execution.html_extract import HTML5_PARSER
    d = copy.deepcopy(template.to_dict())
    d["extract"] = {"items": selector_or_path}
    expect = dict(d.get("expect") or {})
    expect.pop(RC.CONTRACT_KEY, None)                 # re-proved, re-calibrated
    if d.get("extract_kind") == "html":
        expect["selector"] = selector_or_path
        d["html_parser"] = HTML5_PARSER
    else:
        list_path, key = _split_list_path(selector_or_path)
        expect["path"] = list_path
        if key:
            expect["required_keys"] = [key]
        else:
            expect.pop("required_keys", None)
    d["expect"] = expect
    d["notes"] = ((d.get("notes") or "") + f" Step-level repair ({_now()}): {how}.").strip()
    return RequestTemplate.from_dict(d)


# ---------------------------------------------------------------------------
# the repair
# ---------------------------------------------------------------------------

@dataclass
class RepairOutcome:
    localised: str = ""
    evidence: str = ""
    reused: List[str] = field(default_factory=list)
    relearned: List[str] = field(default_factory=list)
    persisted: bool = False
    status: str = ""
    reason: str = ""
    proof: Dict[str, Any] = field(default_factory=dict)
    template: Optional[Dict[str, Any]] = None
    candidates_tried: int = 0
    fallback: str = ""
    fallback_result: Any = None
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "localised", "evidence", "reused", "relearned", "persisted", "status", "reason",
            "proof", "template", "candidates_tried", "fallback", "fallback_result",
            "model_calls", "tokens_in", "tokens_out", "usd")}


def _recording(value: str, param: str, url: str, page: str, status: int = 200,
               implied_end_tags: bool = False) -> Dict[str, Any]:
    return {"param": {"name": param, "value": value}, "final_url": url, "dom": page,
            "network": [{"url": url, "method": "GET", "status": status, "mime": "text/html",
                         "body": page, "resource_type": "Document"}],
            "ui": {"items": [], "text": _visible(page, implied_end_tags)},
            "origin": "step_repair"}


def reprove(template: Any, values: Sequence[str], executor, *,
            ui_page: Optional[Callable[[str], Optional[Tuple[str, str]]]] = None,
            prior_status: str = "", choose: Optional[Sequence[Any]] = None,
            ui_page_raw: bool = False, previous: Any = None) -> Tuple[Any, Any]:
    """Prove ``template`` (or the first of ``choose`` that passes selection) on
    ``values`` with the shared split. ``values[0]`` is the anchor (compile);
    ``ui_page(v) -> (url, html)`` is the page a person sees for ``v`` (default:
    the tool's own request, for an HTML document tool, whose raw responses are
    read with the implied end tags; ``ui_page_raw``: ``ui_page`` returns raw
    responses too, read the same way). Returns ``(chosen_template, proof)``;
    ``proof.status`` is final.

    ``previous``: the template being repaired. The chosen template keeps its
    learned empty page when it sends the same request; when the request changed,
    the old page is dropped and the new request's is learned
    (``result_contract.learn_empty_page``)."""
    from anyuse.execution import proof_split as PS
    from anyuse.execution import result_contract as RC
    from anyuse.execution.api_compile import page_chrome
    from anyuse.execution.api_verify import (EquivalenceResult, _settle,
                                             cases_from_recordings, finalize_recorded_minimum,
                                             verify_template)
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth

    param = template.params[0] if template.params else "q"
    # the pages fetched here are raw responses; a ui_page page may be a DOM snapshot
    raw = ui_page is None or ui_page_raw

    def page_for(v: str) -> Tuple[str, str]:
        if ui_page is not None:
            got = ui_page(v)
            return got if got else ("", "")
        if template.extract_kind != "html":
            return "", ""
        cap = capture(template, v, executor, param)
        return (cap.url, cap.body) if cap.ok else ("", "")

    seen, vals = set(), []
    for v in values:
        if _norm(v) and _norm(v) not in seen:
            seen.add(_norm(v))
            vals.append(v)
    recs = []
    for v in vals:
        url, page = page_for(v)
        if page:
            recs.append(_recording(v, param, url, page, implied_end_tags=raw))
    split = PS.assign(recs, n_selection=1)
    dummy = types.SimpleNamespace(template=template)
    if not split.held_out or not split.selection:
        return template, _settle(EquivalenceResult(
            reason=f"re-proof needs >= 4 distinct pages (anchor, selection, 2 validation); "
                   f"{len(recs)} available"), split, dummy)
    fit = [split.compile, *split.selection]
    selector = derive_ground_truth(fit, implied_end_tags=raw)
    if not selector:
        return template, _settle(EquivalenceResult(
            reason="no ground truth on the fit pages: " + str(
                fit[0].get("recorder_warning") or "no result list changed")), split, dummy)
    vis = any(r.get("ground_truth_visible_only") for r in fit)
    fitted_on = [PS.value_of(r) for r in fit]
    for r in split.validation:
        r["ground_truth_selector"] = selector
        r["ground_truth_fitted_on"] = list(fitted_on)
        if vis:
            r["ground_truth_visible_only"] = True
        r["ui"]["items"] = extract(r["dom"], selector, limit=30, visible_only=vis,
                                   implied_end_tags=raw)
    split.note_fit(fitted_on)
    chrome = page_chrome(fit)
    anchor_value = PS.value_of(split.compile)
    sel_value = PS.value_of(split.selection[0])
    # selection: the first candidate reproducing the selection page's ground truth
    chosen = template
    options = list(choose or [template])
    if len(options) > 1:
        sel_cases = cases_from_recordings(split.selection, anchor_value, chrome=chrome)
        split.note_selection([sel_value])
        chosen = None
        for cand in options:
            if verify_template(cand, sel_cases, executor, min_cases=1,
                               record_containment=True).passed:
                chosen = cand
                break
        if chosen is None:
            return options[0], _settle(EquivalenceResult(
                reason=f"no re-derived candidate reproduced the selection page "
                       f"({len(options)} tried)"), split, dummy)
    cases = cases_from_recordings(split.validation, anchor_value, chrome=chrome,
                                  excluded_values=[sel_value])
    kept = {_norm(c.get("value", "")) for c in cases}
    split.validation = [r for r in split.validation if PS.value_of(r) in kept]
    RC.attach(chosen)
    if previous is not None:
        RC.carry_positions(previous, chosen)
    proof = verify_template(chosen, cases, executor, 2, record_containment=True)
    if proof.passed:
        finalize_recorded_minimum(chosen, proof)
        RC.finalize(chosen, proof)
        if previous is not None and RC.request_of(previous) != RC.request_of(chosen):
            # the empty page belongs to the request: a new request learns its own
            RC.drop_empty_page(chosen)
            RC.learn_empty_page(chosen, proof, executor)
    proof = _settle(proof, split, types.SimpleNamespace(template=chosen))
    if prior_status and proof.passed:
        status = PS.weaker(proof.effective_status, prior_status)
        if status != proof.effective_status:
            proof.status = status
            proof.split["held_out"] = False
            proof.split["why"] = ((proof.split.get("why") + "; ") if proof.split.get("why")
                                  else "") + f"inherited status {prior_status!r}"
    return chosen, proof


def repair(record: Dict[str, Any], *, executor, held_out: Sequence[str], selection: str,
           anchor: Optional[Dict[str, Any]] = None,
           ui_page: Optional[Callable[[str], Optional[Tuple[str, str]]]] = None,
           navigate: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
           full_pipeline: Optional[Callable[[str], Any]] = None,
           ui_page_raw: bool = False, fresh: Optional[Capture] = None
           ) -> Tuple[Optional[Dict[str, Any]], RepairOutcome]:
    """Localise, re-learn one step from one capture, re-prove; else fall back.

    Returns ``(new_record_or_None, outcome)``. ``new_record`` is only set when
    the re-proof passed; it carries the new template, the new proof pinned to
    it, and a ``repair`` block that says what was reused and what re-learned.
    ``ui_page_raw``: ``ui_page`` returns raw HTTP responses (:func:`reprove`).
    ``fresh``: the capture of the anchor's value, when the caller already made it.
    """
    from anyuse.execution import proof_split as PS
    from anyuse.execution.api_step import RequestTemplate, load_command
    from anyuse.execution.workflow_schema import template_digest

    out = RepairOutcome()
    prior = record.get("proof") or {}
    prior_status = str(prior.get("status") or PS.PROVED) if prior.get("passed") \
        else PS.NOT_PROVED

    def fall_back(why: str):
        out.reason = why
        if full_pipeline is not None:
            out.fallback = "full_pipeline"
            out.fallback_result = full_pipeline(why)
        return None, out

    template = load_command(record.get("template") or {})
    if not isinstance(template, RequestTemplate) or template.shape != "list":
        out.localised = "unknown"
        return fall_back("step-level repair covers single-request list tools only")
    stored_anchor = anchor_of(record)
    anchor = anchor or stored_anchor
    if anchor is None:
        out.localised = "unknown"
        return fall_back("no drift anchor (the tool has no recorded good answer with >= "
                         f"{MIN_ANCHOR} items), so one capture cannot say what the answer was")
    cap = fresh if fresh is not None else capture(template, anchor["value"], executor)
    loc = localise(template, cap, anchor)
    out.localised, out.evidence = loc.step, loc.evidence
    if loc.step == "none":
        out.reason = "nothing to repair: " + loc.evidence
        return None, out
    if loc.step == "unknown":
        return fall_back("localisation failed: " + loc.evidence)

    new_templates: List[Any] = []
    if loc.step in ("extraction_region", "record_segmentation"):
        out.reused = ["request", "parameter_binding"]
        if template.extract_kind == "html":
            cands = html_candidates(cap.body, anchor, str(template.extract.get("items") or ""))
            how = "extraction re-derived from one fresh page against the drift anchor"
        else:
            data = cap.data
            if template.extract_kind == "embedded_json":
                from anyuse.execution.embedded_json import select_blob
                data = select_blob(cap.body, template.embedded)
            cands = json_candidates(data, anchor) if data is not None else []
            how = "JSON path re-derived from one fresh response against the drift anchor"
        out.candidates_tried = len(cands)
        new_templates = [with_extraction(template, sel, f"{how} ({loc.step}): {sel!r}")
                         for _score, sel, _got in cands[:5]]
        out.relearned = ["extraction"]
    elif loc.step in ("parameter_binding", "request_shape"):
        nav = navigate(anchor["value"]) if navigate is not None else None
        if not nav:
            return fall_back(f"{loc.step}: re-learning it needs one navigation capture of "
                             f"{anchor['value']!r}, and none is available")
        if loc.step == "parameter_binding":
            from anyuse.execution.learn_loop import url_template_for
            param = template.params[0] if template.params else "q"
            url_t, path_params, transforms, how = url_template_for(
                str(nav.get("final_url") or ""), anchor["value"], param)
            if not url_t:
                return fall_back(f"parameter_binding: {how}")
            parts = urllib.parse.urlsplit(url_t)
            query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            d = copy.deepcopy(template.to_dict())
            d["url"] = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            d["query"] = query
            d["path_params"], d["param_transforms"] = dict(path_params), dict(transforms)
            d["notes"] = ((d.get("notes") or "") + f" Step-level repair ({_now()}): "
                          f"parameter re-bound from one navigation ({how}).").strip()
            fixed = RequestTemplate.from_dict(d)
            out.reused, out.relearned = ["request_method", "extraction"], ["parameter_binding"]
            recap = capture(fixed, anchor["value"], executor)
            if not recap.ok or exact_cover(anchor["items"], _old_items(fixed, recap))[0] \
                    < MIN_ANCHOR_COVER:
                cands = html_candidates(recap.body, anchor, str(fixed.extract.get("items")
                                                                 or "")) \
                    if recap.ok and fixed.extract_kind == "html" else []
                if not cands:
                    return fall_back("parameter_binding re-bound, but the page for the anchor "
                                     "does not reproduce the anchor's items")
                out.relearned.append("extraction")
                out.reused.remove("extraction")
                new_templates = [with_extraction(fixed, sel, "extraction re-derived after "
                                                 "re-binding") for _s, sel, _g in cands[:5]]
            else:
                new_templates = [fixed]
        else:
            from anyuse.execution.api_compile import compile_recording
            rec = dict(nav)
            rec.setdefault("param", {"name": template.params[0] if template.params else "q",
                                     "value": anchor["value"]})
            rec["ui"] = {"items": list(anchor["items"]), "text": ""}
            compiled = compile_recording(rec, allow_chain=False)
            if compiled.template is None:
                return fall_back(f"request_shape: no request in the one capture explains the "
                                 f"anchor ({compiled.reason})")
            out.reused, out.relearned = [], ["request", "extraction"]
            new_templates = [compiled.template]
    if not new_templates:
        return fall_back(f"{loc.step}: no re-derivation from the one capture reproduces the "
                         f"anchor's items")

    values = [anchor["value"], selection, *held_out]
    chosen, proof = reprove(new_templates[0], values, executor, ui_page=ui_page,
                            prior_status=prior_status, choose=new_templates,
                            ui_page_raw=ui_page_raw, previous=template)
    out.proof = proof.to_dict()
    out.status = proof.effective_status
    if not proof.passed:
        return fall_back(f"re-proof failed: {proof.reason}")
    new = copy.deepcopy(record)
    new["template"] = chosen.to_dict()
    new_proof = proof.to_dict()
    new_proof.pop("drift_anchor", None)        # the anchor stays the last GOOD answer
    if anchor != stored_anchor:
        # repaired against the list the site shows now: that list is the last good answer
        new["drift_anchor"] = {**anchor, "at": _now(), "source": anchor.get("source") or "page"}
    new_proof["status"] = proof.effective_status
    new_proof["template_digest"] = template_digest(new["template"])
    new["proof"] = new_proof
    new["repair"] = {"at": _now(), "localised": loc.step, "evidence": loc.evidence,
                     "reused": list(out.reused), "relearned": list(out.relearned),
                     "anchor_value": anchor["value"], "captures": 1,
                     "previous_template_digest": prior.get("template_digest"),
                     "previous_status": prior_status, "model_calls": 0}
    # the new request starts with its own runs; the automatic-recompile pacing carries over
    from anyuse.execution.api_recompile import ApiHealth
    old = ApiHealth.from_dict(record.get("health"))
    new["health"] = ApiHealth(recompile_attempts=old.recompile_attempts,
                              last_recompile_at=old.last_recompile_at).to_dict()
    out.persisted = True
    out.template = new["template"]
    out.reason = (f"{loc.step} re-learned from one capture and re-proved on held-out input: "
                  f"{proof.reason}")
    return new, out
