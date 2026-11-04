"""Equivalence proof for a compiled template — the part that makes "with proof"
mean something.

A template that reproduces the recording it was extracted from has proved
nothing: the extractor built it to fit that recording. The only evidence worth
persisting is that the template, bound to a parameter value that was **never in
the recording**, returns what the UI shows for that same new value.

So :func:`verify_template` takes *fresh cases* — ``(value, ui_items)`` pairs
captured from the real UI for values the compiler never saw — replays the
template for each, and compares the extracted values against the visible list on
three axes: normalised text, order, and count. A template is only persisted as
the preferred implementation when it passes on at least
:data:`MIN_FRESH_CASES` fresh values.

What this proves, exactly — and no more: that for the checked values, the
template's extracted list matched the UI list under
``api_compile.normalise``. It does not prove the endpoint is stable, that the
site permits automated access, that the result is identical for every input, or
that a non-read method would be safe. Those limits are stated in
``docs/COMPILE.md`` rather than implied away here.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from anyuse.execution.api_compile import (
    CompileResult,
    compile_recording,
    contains_words,
    first_line,
    is_word,
    long_enough,
    normalise,
    page_chrome,
    values_match,
    visible_items,
    volatile_evidence,
)
from anyuse.execution.api_step import (
    PICK_NONE_NAMED,
    ApiStepExecutor,
    RequestTemplate,
    json_path,
    keyed_rows,
    pick_row,
)
from anyuse.execution.param_binding import apply_transform, binding_evidence
from anyuse.execution.unicode_text import ALL_POSITIONS

logger = logging.getLogger(__name__)

#: A ``{name}`` placeholder in a template URL (as ``api_step`` renders them).
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: A fresh case passes when this share of the visible items is accounted for.
MIN_COVERAGE = 0.8
#: How many leading visible items are compared.
TOP_K = 10
#: How many fresh values a template must pass before it may be persisted.
MIN_FRESH_CASES = 2
#: How many visible items/fields must take part in a case for it to count as
#: evidence at all. "One field matched" is not a proof: on a detail screen a
#: single description is matched just as well by a search response that merely
#: contains it, and a coverage of 1.00 over one item is a coin toss dressed up
#: as a measurement. A case with fewer compared values FAILS, with
#: ``insufficient evidence`` as the reason, rather than passing quietly.
MIN_COMPARED = 3


def permission_preflight(compiled: CompileResult, recording: dict, *,
                         allowed_origins=None, hosts_path=None,
                         checker=None) -> str:
    """Run origin policy and ``check_permission`` before proof traffic.

    A host refused only because it is not acknowledged is put to the CLI's asker first
    (``api_permission.ask_for_host``, with the proof's request count from
    ``compiled.proof_requests``); a yes records the acknowledgement (under ``--dry-run`` /
    ``--no-persist`` it holds for the command only) and the check runs again.
    Without an asker (MCP, benches) the refusal is returned as before."""
    if compiled.template is None:
        return ""
    from anyuse.execution.api_permission import ask_for_host, check_permission, host_acknowledged
    from anyuse.execution.request_policy import (destination_refusal,
                                                 exact_origin_allowlist, origin)
    checker = checker or check_permission
    steps = getattr(compiled.template, "steps", None)
    urls = [step.template.url for step in steps] if steps else [compiled.template.url]
    base = str(recording.get("final_url") or "")
    allowed = exact_origin_allowlist(base, allowed_origins)
    # A separately acknowledged host is an explicit related-origin approval.
    for url in urls:
        host = urllib.parse.urlsplit(url).netloc
        if host_acknowledged(host, hosts_path):
            allowed.add(origin(url))
    for url in urls:
        refusal = destination_refusal(url, allowed)
        if refusal:
            return f"proof pre-send policy refused {url}: {refusal}"
        verdict = checker(url, hosts_path=hosts_path)
        if not getattr(verdict, "may_prefer", False):
            asked, why_not = ask_for_host(url, verdict, hosts_path=hosts_path,
                                          requests=getattr(compiled, "proof_requests", None) or (0, 0))
            if why_not:
                return f"proof not run: {why_not}"
            if asked:
                verdict = checker(url, hosts_path=hosts_path)
        if not getattr(verdict, "may_prefer", False):
            return f"proof permission refused {url}: {getattr(verdict, 'reason', verdict)}"
    return ""


def _labels(items: Sequence[Any], limit: int = TOP_K) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in items or []:
        label = normalise(first_line(str(raw)))
        if not long_enough(label) or label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= limit:
            break
    return out


def _flatten(value: Any) -> List[str]:
    """API-side values → comparable labels (an extraction may yield nested lists)."""
    out: List[str] = []
    stack = [value]
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            stack = list(node) + stack
        elif isinstance(node, dict):
            stack = list(node.values()) + stack
        elif node is not None:
            out.append(normalise(node))
    return [x for x in out if long_enough(x)]


@dataclass
class CaseResult:
    """One fresh value, checked."""
    value: str = ""
    passed: bool = False
    coverage: float = 0.0
    order_ok: bool = False
    ui_count: int = 0
    api_count: int = 0
    missing: List[str] = field(default_factory=list)
    reason: str = ""
    elapsed_ms: float = 0.0
    bytes: int = 0
    contract: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"value": self.value, "passed": self.passed,
                "coverage": round(self.coverage, 3), "order_ok": self.order_ok,
                "ui_count": self.ui_count, "api_count": self.api_count,
                "missing": self.missing[:5], "reason": self.reason,
                "elapsed_ms": round(self.elapsed_ms, 1), "bytes": self.bytes,
                "contract": dict(self.contract)}


@dataclass
class EquivalenceResult:
    """The verdict over all fresh cases."""
    passed: bool = False
    cases: List[CaseResult] = field(default_factory=list)
    reason: str = ""
    #: ``proved`` / ``checked (selection not held out)`` / ``not proved``;
    #: empty until a compile path settles it through ``proof_split``
    status: str = ""
    #: the fit/selection/validation counts and whether validation was held out
    split: Dict[str, Any] = field(default_factory=dict)
    #: the exact claim (``proof_split.scope_text``), counts and enums only
    scope: str = ""
    #: the first passing validation page's answer (value + first items): the
    #: drift anchor a step-level repair re-derives from (execution/
    #: step_repair.py). Stored on the RECORD (``drift_anchor``), never in the
    #: proof dict itself, and never on an MCP surface.
    anchor: Optional[Dict[str, Any]] = None
    #: each passing list case's answer (``{"params", "data", "items"}``): the
    #: result pages the site's empty page is learned against
    #: (``result_contract.learn_empty_page``). Never in :meth:`to_dict`.
    pages: List[Dict[str, Any]] = field(default_factory=list, repr=False)
    #: the fitting input a chain was fitted on when the compile generated it from
    #: the site's own search (``execution/chain_fit.py``): ``{value, from, via,
    #: ui_from}``. Stored on the RECORD (``chain_fit``), never in the proof dict.
    chain_fit: Optional[Dict[str, Any]] = None

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def effective_status(self) -> str:
        from anyuse.execution.proof_split import NOT_PROVED, PROVED
        if not self.passed:
            return NOT_PROVED
        return self.status or PROVED

    def to_dict(self) -> dict:
        out = {"passed": self.passed, "reason": self.reason,
               "passed_count": self.passed_count, "case_count": len(self.cases),
               "cases": [c.to_dict() for c in self.cases]}
        if self.status or self.split:
            out["status"] = self.effective_status
            out["selection_held_out"] = bool(self.split.get("held_out"))
            out["split"] = dict(self.split)
            out["scope"] = self.scope
        if self.anchor:
            out["drift_anchor"] = dict(self.anchor)
        if self.chain_fit:
            out["chain_fit"] = dict(self.chain_fit)
        return out


#: A SPARSE answer (fewer than ``MIN_COMPARED`` visible results) is not rejected
#: outright; it passes only under a stronger identity contract.
SPARSE_CONTRACT = True


def names_value(label: str, value: str, positions: Sequence[str] = ALL_POSITIONS) -> bool:
    """Every word (>= 2 characters, or one ideograph) of ``value`` is a word of
    ``label`` (``api_compile.word_spans``), at ``positions`` of a run of an
    unspaced script (``unicode_text``; every position by default)."""
    words = [w for w in normalise(value).split() if is_word(w)]
    if not words:
        return False
    low = normalise(label)
    return all(contains_words(low, w, positions) for w in words)


def sparse_identity(ui: Sequence[str], api: Sequence[str], value: str,
                    positions: Sequence[str] = ALL_POSITIONS) -> Tuple[bool, str]:
    """The sparse-result contract: 1..MIN_COMPARED-1 visible results pass only
    if (a) the extraction returns EXACTLY as many values, (b) each equals one
    visible label after normalisation (no containment), in the same order, and
    (c) every label names the query value. A constant or unrelated output
    cannot meet (c); an output with extra rows cannot meet (a). ``positions``:
    where in a run of an unspaced script the query may stand (the contract's
    ``query_positions``, which the fitting inputs calibrated); a proof never
    asks a model."""
    if not value:
        return False, "sparse contract needs the query value"
    if len(api) != len(ui):
        return False, (f"sparse contract: {len(ui)} visible result(s) but the extraction "
                       f"returned {len(api)}")
    if [normalise(a) for a in api] != [normalise(u) for u in ui]:
        return False, "sparse contract: the extraction is not exactly the visible result(s)"
    if not all(names_value(u, value, positions) for u in ui):
        if set(positions) != set(ALL_POSITIONS) and all(names_value(u, value) for u in ui):
            return False, (f"sparse contract: a result holds the query {value!r} only inside a "
                           f"longer word, and the site's own result lists on the fitting inputs "
                           f"never did that")
        return False, f"sparse contract: a result does not name the query {value!r}"
    return True, (f"sparse contract: {len(ui)} result(s) reproduced exactly and each "
                  f"names {value!r}")


def record_containment(ui: Sequence[str], api: Sequence[str]) -> Tuple[float, float, int]:
    """``(coverage, precision, assigned)`` of a FIELD-level extraction against a
    RECORD-level ground truth: walking both in order, each extracted value is
    assigned to the first ground-truth record (at or after the previous one)
    whose normalised text contains it. Coverage = share of the records that
    received a value; precision = share of the extracted values, up to the last
    record compared, that landed inside a record."""
    if not ui or not api:
        return 0.0, 0.0, 0
    ptr, got, considered, assigned = 0, set(), 0, 0
    for val in api:
        k = ptr
        while k < len(ui) and val not in ui[k]:
            k += 1
        if k == len(ui) and (len(ui) - 1) in got:
            break                       # past the last compared record: out of the window
        considered += 1
        if k < len(ui):
            got.add(k)
            assigned += 1
            ptr = k
    return len(got) / len(ui), (assigned / considered if considered else 0.0), assigned


def compare(ui_items: Sequence[Any], api_values: Any, top_k: int = TOP_K,
            ordered: bool = True, min_compared: int = MIN_COMPARED,
            value: str = "", records: bool = False,
            positions: Sequence[str] = ALL_POSITIONS) -> CaseResult:
    """Compare a visible result list against an API extraction.

    Three axes, all deterministic:
      * **text** — each visible label must have a matching API value under
        :func:`api_compile.normalise` (equality or containment either way),
      * **order** — the matched API indices must be non-decreasing, i.e. the API
        lists the same things in the same order the UI did. Checked only when
        ``ordered`` (a list-shaped extraction); for a detail screen the order of
        unrelated fields carries no meaning, and the result says so rather than
        quietly passing an order check it never ran,
      * **count** — both counts are reported; a mismatch is not by itself a
        failure (a UI page shows the first N of many) but it is never hidden.

    ``positions`` reaches the sparse contract (:func:`sparse_identity`).
    """
    ui = _labels(ui_items, top_k)
    api = _flatten(api_values)
    res = CaseResult(ui_count=len(ui), api_count=len(api))
    if not ui:
        res.reason = "no visible items to compare against"
        return res
    if len(ui) < min_compared:
        if SPARSE_CONTRACT and value and api:
            ok, why = sparse_identity(ui, api, value, positions)
            res.contract = {"kind": "sparse_identity", "results": len(ui)}
            if ok:
                res.passed, res.coverage, res.order_ok, res.reason = True, 1.0, True, why
                return res
            res.missing = list(ui)
            res.reason = (f"insufficient evidence: only {len(ui)} visible value(s) to compare, "
                          f"{min_compared} required, and the {why}")
            return res
        res.missing = list(ui)
        res.reason = (f"insufficient evidence: only {len(ui)} visible value(s) to compare, "
                      f"{min_compared} required — a single matching field is not a proof")
        return res
    if not api:
        res.reason = "api extraction produced no values"
        res.missing = ui
        return res

    # Matching is BIJECTIVE and exact-first. A greedy "first substring hit" pass
    # collapses a whole result list onto one API entry (every "… regex …" item
    # matches the entry "regex") and then reports a bogus order violation, so:
    #   pass 1 — exact normalised equality, each API entry claimed at most once;
    #   pass 2 — containment, nearest length among the entries still unclaimed.
    hits: Dict[int, int] = {}          # ui index -> api index
    used: set = set()
    for ui_i, label in enumerate(ui):
        for api_i, val in enumerate(api):
            if api_i not in used and val == label:
                hits[ui_i] = api_i
                used.add(api_i)
                break
    for ui_i, label in enumerate(ui):
        if ui_i in hits:
            continue
        best, best_delta = -1, None
        for api_i, val in enumerate(api):
            if api_i in used or not values_match(label, val):
                continue
            delta = abs(len(val) - len(label))
            if best_delta is None or delta < best_delta:
                best, best_delta = api_i, delta
        if best >= 0:
            hits[ui_i] = best
            used.add(best)

    matched_idx = [hits[i] for i in sorted(hits)]
    res.missing = [label for i, label in enumerate(ui) if i not in hits]

    res.coverage = len(matched_idx) / len(ui)
    if ordered:
        res.order_ok = all(matched_idx[i] <= matched_idx[i + 1] for i in range(len(matched_idx) - 1))
    else:
        res.order_ok = True
        res.reason = "order not checked (field-shaped extraction)"
    res.passed = res.coverage >= MIN_COVERAGE and res.order_ok
    if not res.passed and records and ordered:
        # step-level repair (execution/step_repair.py): the ground truth reads
        # whole records, the tool reads one field of each (its granularity is
        # pinned by the drift anchor on the compile page). Every extracted
        # value must sit inside a record, in order, covering >= 80 % of them.
        cov, prec, _n = record_containment(ui, [a for a in api if long_enough(a)])
        if cov >= MIN_COVERAGE and prec >= MIN_COVERAGE:
            res.passed, res.coverage, res.order_ok, res.missing = True, cov, True, []
            res.contract = {**res.contract, "kind": "record_containment",
                            "precision": round(prec, 3)}
            res.reason = (f"record containment: {cov:.2f} of the ground-truth records hold "
                          f"extracted values in order, {prec:.2f} of the values inside a record")
            return res
    if not res.passed:
        bits = []
        if res.coverage < MIN_COVERAGE:
            bits.append(f"coverage {res.coverage:.2f} < {MIN_COVERAGE}")
        if not res.order_ok:
            bits.append("api order does not follow the UI order")
        res.reason = "; ".join(bits)
    return res


def compare_named_fields(expected: Dict[str, Any], actual: Dict[str, Any],
                         min_compared: int = MIN_COMPARED) -> CaseResult:
    """Compare field meanings, not a flattened bag of their values.

    A title and a summary may contain the same words; swapping their extraction
    paths must still fail.  The field name is therefore part of the proof.
    Text is compared as the list proof compares it (``api_compile.normalise`` of
    its first line); a number or a boolean by value.
    """
    expected = dict(expected or {})
    actual = dict(actual or {})
    result = CaseResult(ui_count=len(expected), api_count=len(actual), order_ok=True)
    result.contract = {"kind": "named_fields", "fields": sorted(expected)}
    if len(expected) < min_compared:
        result.reason = (f"insufficient named-field evidence: {len(expected)} field(s), "
                         f"{min_compared} required")
        return result
    missing = []
    for name, wanted in expected.items():
        if name not in actual or not values_match(_comparable(wanted), _comparable(actual[name])):
            missing.append(name)
    result.missing = missing
    result.coverage = (len(expected) - len(missing)) / len(expected)
    extras = sorted(set(actual) - set(expected))
    result.passed = not missing and not extras
    if not result.passed:
        bits = []
        if missing:
            bits.append("field meaning mismatch: " + ", ".join(sorted(missing)))
        if extras:
            bits.append("unexpected fields: " + ", ".join(extras))
        result.reason = "; ".join(bits)
    return result


def _comparable(value: Any) -> Any:
    return normalise(first_line(value)) if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# intermediate bindings traced through a recording's own traffic
# ---------------------------------------------------------------------------

def _recorded_values(tmpl: RequestTemplate, request: dict) -> Optional[Dict[str, str]]:
    """The placeholder values a recorded request carried, when it is a request of
    ``tmpl`` (same method, origin and path shape); else None.

    A placeholder matches one path segment, or as many as the template's
    ``path_params`` span; a query placeholder matches the value of its key,
    whatever it holds (a search for ``ac/dc`` sends ``q=ac/dc``).
    Constant query values are not compared: a recording of another input may
    carry other values for parameters the compiler kept as they were."""
    if (request.get("method") or "GET").upper() != (tmpl.method or "GET").upper():
        return None
    want = urllib.parse.urlsplit(tmpl.url)
    got = urllib.parse.urlsplit(str(request.get("url") or ""))
    if (want.scheme, want.netloc.lower()) != (got.scheme, got.netloc.lower()):
        return None
    values: Dict[str, str] = {}

    def fill(pattern_text: str, text: str, decode,
             spans: Optional[Dict[str, Any]] = None) -> bool:
        """Match ``text`` against ``pattern_text``; ``spans`` (a path) keeps a
        placeholder inside its path segments, None (a query value) does not."""
        names: List[str] = []
        pattern, pos = "", 0
        for match in _PLACEHOLDER.finditer(pattern_text):
            pattern += re.escape(pattern_text[pos:match.start()])
            names.append(match.group(1))
            one_segment = spans is not None and int(spans.get(match.group(1), 1)) <= 1
            pattern += "([^/]+?)" if one_segment else "(.+?)"
            pos = match.end()
        pattern += re.escape(pattern_text[pos:])
        found = re.fullmatch(pattern, text)
        if found is None:
            return False
        for name, raw in zip(names, found.groups()):
            if values.setdefault(name, decode(raw)) != decode(raw):
                return False
        return True

    if not fill(want.path, got.path, urllib.parse.unquote, tmpl.path_params or {}):
        return None
    query = dict(urllib.parse.parse_qsl(want.query, keep_blank_values=True))
    query.update(tmpl.query or {})
    recorded = dict(urllib.parse.parse_qsl(got.query, keep_blank_values=True))
    for key, text in query.items():
        if not _PLACEHOLDER.search(str(text)):
            continue
        if key not in recorded or not fill(str(text), recorded[key], str):
            return None
    return values


def _carries(tmpl: RequestTemplate, got: Dict[str, str], inputs: Dict[str, str],
             bound: Dict[str, Any]) -> bool:
    """Does a recorded request carry this input and the values bound so far?"""
    for name, text in got.items():
        if name in inputs:
            value = inputs[name]
            transform = (tmpl.param_transforms or {}).get(name)
            forms = {normalise(value)}
            if transform:
                try:
                    forms.add(normalise(apply_transform(transform, value)))
                except KeyError:
                    return False
            if normalise(text) not in forms:
                return False
        elif name in bound:
            if normalise(text) != normalise(bound[name]):
                return False
        else:
            return False
    return True


def _shows(tmpl: RequestTemplate, data: Any, labels: Sequence[str]) -> bool:
    """Does a recorded response, read with ``tmpl``'s extraction, explain what the
    page showed as well as the compiler requires of the request it compiles
    from (``api_compile.MIN_COVERAGE``)? With nothing visible to compare there
    is nothing to decide by, and the proof reports that itself."""
    from anyuse.execution.api_compile import MIN_COVERAGE as EXPLAINS
    if not labels:
        return True
    values = _flatten({name: json_path(data, path) for name, path in tmpl.extract.items()})
    matched = sum(1 for label in labels if any(values_match(label, v) for v in values))
    return matched / len(labels) >= EXPLAINS


def _row_choice(step, name: str, path: str, data: Any,
                inputs: Dict[str, str]) -> Tuple[Any, int, str]:
    """The value a plan step binds from ``data``: ``(value, row, rule)``, the row
    rule's answer (``api_step.pick_row``) or the first entry."""
    rule = (step.pick or {}).get(name)
    if rule:
        return pick_row(data, path, rule, inputs.get(str(rule.get("input") or "")))
    value = json_path(data, path)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None, -1, ""
    rows = keyed_rows(data, path)
    return value, (rows[0][0] if rows else 0), "first"


def _what_opens(step, name: str, inputs: Dict[str, str]) -> str:
    """The result a step's binding opens, in words."""
    rule = (step.pick or {}).get(name)
    if rule:
        return f"the result named {inputs.get(str(rule.get('input') or ''), '')!r}"
    return "the first result"


def derive_bindings(template, recording: dict,
                    labels: Sequence[str] = ()) -> Tuple[Optional[Dict[str, Any]],
                                                         Dict[str, Any], str]:
    """The intermediate values a chain binds for ``recording``'s input, traced
    through the recording's own traffic.

    Each step's request is found in the recording (same method, origin and
    path shape, carrying the input). A step that binds a value reads it from
    its recorded response at the binding's path, with the plan's row rule
    (``api_step.pick_row``) or the first entry, as a run does. The value counts
    only when a LATER recorded request of the consuming step carries it, and
    the last step's recorded response shows what the page showed (``labels``,
    normalised): the page opened that result, not one fetched alongside it.

    Returns ``(bindings, how, "")``: the values as the response typed them,
    and per binding the producing and consuming step, the path, the row, the
    rule and the positions of the two recorded requests (no response values).
    ``(None, {}, reason)`` when the value cannot be traced.

    A plan whose every binding is a value read from the site's page
    (``PlanStep.page_bind``) has nothing to trace: its values are read by their
    locators on every run, never recorded. It returns ``({}, how, "")`` with
    the step and the locator per value, and the proof rests on the answers."""
    from anyuse.execution.api_compile import eligible_requests
    steps = list(getattr(template, "steps", None) or [])
    if not steps:
        return None, {}, "not a chain"
    if any(getattr(step, "page_bind", None) for step in steps):
        if any(step.bind for step in steps):
            return None, {}, ("a plan that reads values from the site's page and from a "
                              "response cannot be traced in a recording")
        from anyuse.execution.page_literals import Locator
        how = {name: {"from": "page", "step": step.name,
                      "locator": Locator.from_dict(loc).ident()}
               for step in steps for name, loc in step.page_bind.items()}
        return {}, how, ""
    for index, step in enumerate(steps):
        tmpl = step.template
        if tmpl.slots or tmpl.body or (tmpl.extract_kind or "json") != "json":
            return None, {}, (f"step {index + 1} ({step.name}) is not a JSON read the "
                              "recording's traffic can be matched against")
    requests = eligible_requests(recording)
    if not requests:
        return None, {}, ("the recording has no JSON traffic to trace the chain from. "
                          "Record this input with its traffic (a HAR or an attach "
                          "session) instead of typing what the page showed")
    param = recording.get("param") or {}
    value = str(param.get("value") or "")
    inputs = {name: value for name in (getattr(template, "input_params", None)
                                       or template.params or [str(param.get("name") or "q")])}
    consumers = {name: next((j for j in range(i + 1, len(steps))
                             if name in steps[j].template.params), -1)
                 for i, step in enumerate(steps) for name in step.bind}
    failure = [(-1, "")]

    def fail(depth: int, why: str) -> None:
        if depth > failure[0][0]:
            failure[0] = (depth, why)

    def walk(i: int, after: int, bound: Dict[str, Any], how: Dict[str, Any],
             producers: Dict[str, Tuple[int, int, str]]):
        if i == len(steps):
            return bound, how
        step = steps[i]
        tmpl = step.template
        matched = [(req, got) for req in requests if req["_recording_index"] > after
                   for got in [_recorded_values(tmpl, req)] if got is not None]
        carrying = [(req, got) for req, got in matched if _carries(tmpl, got, inputs, bound)]
        waiting = [name for name, j in consumers.items() if j == i]
        if not carrying:
            if not waiting:
                fail(i, f"the recording has no response from step {i + 1} ({step.name}, "
                        f"{urllib.parse.urlsplit(tmpl.url).path}) for {value!r}. Record "
                        "this input again")
            else:
                name = waiting[0]
                p_step, _p, path = producers[name]
                what = _what_opens(steps[p_step], name, inputs)
                fail(i, f"the recording did not open {what} (the {name!r} step "
                        f"{p_step + 1} reads at {path!r}): no later request of step "
                        f"{i + 1} ({step.name}) carries it. Record this input again "
                        f"and open {what}")
            return None
        if i == len(steps) - 1:
            shown = [(req, got) for req, got in carrying if _shows(tmpl, req["_data"], labels)]
            if not shown:
                name = waiting[0] if waiting else ""
                what = (_what_opens(steps[producers[name][0]], name, inputs)
                        if name else "this result")
                fail(i, f"the recording fetched {what} (step {i + 1}, {step.name}), but "
                        "that answer is not what the page showed, so the page opened "
                        f"something else. Record this input again and open {what}")
                return None
            carrying = shown
        for req, _got in reversed(carrying):
            where = req["_recording_index"]
            values: Dict[str, Any] = {}
            rows: Dict[str, Any] = {}
            for name, path in step.bind.items():
                found, row, rule = _row_choice(step, name, path, req["_data"], inputs)
                if found is None:
                    fail(i, (f"no result in the recorded response of step {i + 1} "
                             f"({step.name}) is named {value!r}, so the chain opens "
                             "nothing for it. Use an input that has a result of that name")
                         if rule == PICK_NONE_NAMED else
                         (f"the recorded response of step {i + 1} ({step.name}) has "
                          f"nothing at {path!r} to bind {name!r}"))
                    break
                values[name] = found
                rows[name] = {"step": step.name, "path": path, "row": row, "rule": rule,
                              "requests": [where]}
            else:
                done = dict(how)
                for name in waiting:
                    done[name] = {**done[name], "consumer": step.name,
                                  "requests": [*done[name]["requests"], where]}
                done.update(rows)
                found_producers = dict(producers)
                found_producers.update({name: (i, where, path)
                                        for name, path in step.bind.items()})
                out = walk(i + 1, where, {**bound, **values}, done, found_producers)
                if out is not None:
                    return out
        return None

    traced = walk(0, -1, {}, {}, {})
    if traced is None:
        return None, {}, failure[0][1] or "the chain cannot be traced in the recording"
    bound, how = traced
    return {name: bound[name] for name in consumers}, how, ""


def _actual_bindings(result: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for step in result.get("plan_trace") or []:
        out.update(step.get("bound") or {})
    return out


def _picked_rows(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Row index and rule of each binding a row rule chose, by binding name."""
    out: Dict[str, Dict[str, Any]] = {}
    for step in result.get("plan_trace") or []:
        for name, how in (step.get("picked") or {}).items():
            out[name] = {"row": int(how.get("row", -1)), "rule": str(how.get("rule", ""))}
    return out


def verify_template(template: RequestTemplate,
                    fresh_cases: Sequence[Dict[str, Any]],
                    executor: Optional[ApiStepExecutor] = None,
                    min_cases: int = MIN_FRESH_CASES,
                    record_containment: bool = False,
                    positions: Optional[Sequence[str]] = None) -> EquivalenceResult:
    """Replay ``template`` for each fresh case and compare with its UI list.

    ``fresh_cases`` is ``[{"value": <never recorded>, "ui_items": [...]}, ...]``
    (or ``{"params": {...}}`` when a template takes several parameters). The
    result passes only when EVERY case passes and at least ``min_cases`` were
    checked — one lucky value is not a proof.

    ``positions``: where in a run of an unspaced script a sparse case's labels
    may hold the query (:func:`fitting_positions`); by default the template's
    contract says (``query_positions``), and a template without one accepts
    every position.
    """
    import copy
    from anyuse.execution import query_match as QM
    from anyuse.execution import result_contract as RC
    executor = executor or ApiStepExecutor()
    validation_template = copy.deepcopy(template)
    if positions is None:
        positions = QM.contract_positions(RC.contract_of(validation_template))

    def relax_count(candidate):
        if hasattr(candidate, "expect") and "min_items" in candidate.expect:
            candidate.expect["min_items"] = 1
        for step in getattr(candidate, "steps", None) or []:
            relax_count(step.template)

    # Completeness is finalized from the minimum count across independently
    # passing recordings. During proof, the UI/API count comparison is the
    # check; enforcing the base recording's count first would reject a valid
    # input with fewer genuine results before comparison could run.
    relax_count(validation_template)
    out = EquivalenceResult()
    seen_inputs = set()
    for case in fresh_cases:
        value = str(case.get("value", ""))
        params = case.get("params") or ({template.params[0]: value} if template.params else {})
        key = tuple(sorted((str(name), normalise(raw)) for name, raw in params.items()))
        if key in seen_inputs:
            continue
        seen_inputs.add(key)
        result = executor.execute(validation_template, params)
        if not result.get("success"):
            out.cases.append(CaseResult(value=value, reason=f"api step failed: {result.get('error')}",
                                        elapsed_ms=float(result.get("elapsed_ms") or 0.0),
                                        bytes=int(result.get("bytes") or 0)))
            continue
        extracted_all = result.get("extracted") or {}
        # Retain only the proved output's names and primitive/container types.
        # This is enough to publish a typed MCP contract and contains none of
        # the response values from the fresh validation input.
        from anyuse.execution.workflow_schema import json_schema_for_value
        observed_schema = json_schema_for_value(extracted_all)
        if template.shape == "list":
            extracted = extracted_all.get("items")
            cr = compare(case.get("ui_items") or [], extracted, ordered=True, value=value,
                         records=record_containment, positions=positions)
        else:
            if not isinstance(case.get("ui_fields"), dict):
                why = str(case.get("fields_refused") or "")
                cr = CaseResult(reason=("field-shaped proof requires named ui_fields; "
                                        "flattened values cannot prove field meaning"
                                        + (f": {why}" if why else "")))
            else:
                cr = compare_named_fields(case["ui_fields"], extracted_all)
                if isinstance(case.get("fields_named_by"), dict):
                    # which evidence named each value (execution/field_names.py)
                    cr.contract["fields_named_by"] = dict(case["fields_named_by"])
        cr.contract["output_schema"] = observed_schema
        if isinstance(result.get("result_contract"), dict):
            # what the runtime result contract measured on this validation
            # page (execution/result_contract.py): calibrates its region check
            cr.contract["runtime"] = dict(result["result_contract"])
        if getattr(template, "steps", None):
            expected_bindings = case.get("bindings")
            if not isinstance(expected_bindings, dict):
                cr.passed = False
                why = str(case.get("bindings_refused") or "")
                cr.reason = ("chain proof requires named intermediate bindings per input"
                             + (f": {why}" if why else ""))
            else:
                actual_bindings = _actual_bindings(result)
                bad = [name for name, wanted in expected_bindings.items()
                       if name not in actual_bindings
                       or not values_match(wanted, actual_bindings[name])]
                cr.contract["intermediate_bindings"] = sorted(expected_bindings)
                if isinstance(case.get("bindings_derived"), dict):
                    # traced from the recording's traffic (derive_bindings)
                    cr.contract["bindings_derived"] = dict(case["bindings_derived"])
                picked = _picked_rows(result)
                if picked:
                    # which row rule chose each binding (exact, normalised, first)
                    cr.contract["row_rules"] = picked
                if bad:
                    cr.passed = False
                    cr.reason = "intermediate binding mismatch: " + ", ".join(sorted(bad))
        cr.value = value
        cr.elapsed_ms = float(result.get("elapsed_ms") or 0.0)
        cr.bytes = int(result.get("bytes") or 0)
        if cr.passed and out.anchor is None and template.shape == "list":
            from anyuse.execution.step_repair import anchor_from
            out.anchor = anchor_from(value, extracted_all.get("items"))
        if cr.passed and template.shape == "list" and not getattr(template, "steps", None):
            got = extracted_all.get("items")
            out.pages.append({"params": dict(params), "data": result.get("data"),
                              "items": list(got) if isinstance(got, list) else []})
        out.cases.append(cr)

    if len(out.cases) < min_cases:
        out.reason = (f"only {len(out.cases)} distinct normalized fresh parameter tuple(s) "
                      f"checked, {min_cases} required")
        return out
    failed = [c for c in out.cases if not c.passed]
    if failed:
        out.reason = (f"{len(failed)}/{len(out.cases)} fresh case(s) failed: "
                      + "; ".join(f"{c.value!r}: {c.reason}" for c in failed[:3]))
        return out
    out.passed = True
    if template.shape == "list":
        contract = f"coverage >= {MIN_COVERAGE}, UI order preserved"
        if any(c.contract.get("kind") == "record_containment" for c in out.cases):
            contract += ("; field values inside ground-truth records (record containment, "
                         "step-level repair)")
    else:
        contract = "named field meanings preserved"
    page_values = sorted(name for step in getattr(template, "steps", None) or []
                         for name in (getattr(step, "page_bind", None) or {}))
    if page_values:
        for c in out.cases:
            c.contract["page_values"] = list(page_values)
        contract += ", page values read from the site's page at run time"
    elif getattr(template, "steps", None):
        contract += ", intermediate bindings checked"
        if any(c.contract.get("bindings_derived") for c in out.cases):
            contract += " (traced from the recorded traffic)"
    if getattr(template, "steps", None):
        if any(getattr(step, "pick", None) for step in template.steps):
            contract += ", result row chosen by name"
    out.reason = (f"passed on {len(out.cases)} fresh value(s) not present in the recording "
                  f"({contract})")
    return out


def finalize_recorded_minimum(template, proof: EquivalenceResult) -> None:
    """Bind list completeness to the minimum independently validated count."""
    counts = [case.api_count for case in proof.cases if case.passed and case.api_count > 0]
    if not counts:
        return
    floor = min(counts)
    target = template
    steps = getattr(template, "steps", None) or []
    if steps:
        target = steps[-1].template
    if hasattr(target, "expect") and "min_items" in target.expect:
        target.expect["min_items"] = min(int(target.expect["min_items"]), floor)


def cases_from_recordings(recordings: Sequence[dict], recorded_value: str = "",
                          chrome=None, excluded_values: Sequence[str] = (),
                          template=None, field_namer=None) -> List[Dict[str, Any]]:
    """Turn UI recordings of FRESH parameter values into verification cases.

    A recording whose parameter value equals ``recorded_value`` is skipped — the
    whole point is that the proof runs on values the compiler never saw.

    ``chrome`` (see ``api_compile.page_chrome``) is subtracted from the UI side
    of every case. It MUST be the same set the compiler used, or the proof would
    hold the template to a list the compiler was never shown.

    For a chain ``template``, a recording that carries no ``bindings`` gets them
    traced from its own traffic (:func:`derive_bindings`), with how they were
    found under ``bindings_derived``; when they cannot be traced the case says
    why under ``bindings_refused``. A recording's own ``bindings`` are kept.

    For a fields-shaped template, a recording that carries no ``ui.fields`` gets
    its values named by ``field_namer`` (``field_names.case_namer``): the names
    under ``ui_fields`` and how each was found under ``fields_named_by``, or why
    a field was not under ``fields_refused``.
    """
    cases: List[Dict[str, Any]] = []
    excluded = {normalise(recorded_value)} if recorded_value else set()
    excluded.update(normalise(value) for value in excluded_values)
    seen = set()
    for rec in recordings:
        value = str(((rec.get("param") or {}).get("value")) or "")
        normalized = normalise(value)
        if normalized in excluded or normalized in seen:
            continue
        seen.add(normalized)
        row = {"value": value,
               "ui_items": visible_items(rec, TOP_K, exclude=chrome)}
        ui_fields = (rec.get("ui") or {}).get("fields")
        if isinstance(ui_fields, dict):
            row["ui_fields"] = dict(ui_fields)
        elif field_namer is not None:
            named, named_by, why = field_namer(rec)
            if named is not None:
                row["ui_fields"], row["fields_named_by"] = named, dict(named_by)
            else:
                row["fields_refused"] = why
        bindings = rec.get("bindings")
        if isinstance(bindings, dict):
            row["bindings"] = dict(bindings)
        elif getattr(template, "steps", None):
            labels = ([normalise(first_line(str(v))) for v in row["ui_fields"].values()]
                      if "ui_fields" in row else row["ui_items"])
            traced, how, why = derive_bindings(template, rec,
                                               [x for x in labels if long_enough(x)])
            if traced is not None:
                row["bindings"] = traced
                row["bindings_derived"] = how
            else:
                row["bindings_refused"] = why
        cases.append(row)
    return cases


def compile_and_verify(recording: dict, fresh_recordings: Sequence[dict],
                       executor: Optional[ApiStepExecutor] = None,
                       min_cases: int = MIN_FRESH_CASES,
                       levers=None, preflight=None,
                       fitting_recordings: Optional[Sequence[dict]] = None,
                       selection_recordings: Optional[Sequence[dict]] = None,
                       split=None, model_scope: str = "", label_keys: bool = True):
    """The whole compile step: extract deterministically, then PROVE on fresh input.

    Returns ``(CompileResult, EquivalenceResult)``. The compile result is only
    fit to persist when ``equivalence.passed`` is True — the caller (the
    ``compile`` CLI command) enforces that, so "compiled" never means "extracted
    and hoped".

    A fields-shaped template (an item's page) is proved on named values. A case
    that does not name them gets them named from its own recording
    (``execution/field_names.py``); ``model_scope`` is the model-check scope its
    model step may ask in (``""``: no model is asked). With ``label_keys``, a
    template compiled from recordings none of which carries ``ui.fields`` names
    its extract keys after the labels the page puts beside its values
    (``field_N`` without one); a recompile of a record with positional keys
    passes False to keep them.
    """
    # Labels that appear in EVERY FITTING recording of a DIFFERENT query are page
    # chrome (navigation, footer, cookie banner), not answers. They are removed
    # from BOTH sides — the compiler's ground truth and the proof's. Validation
    # recordings are deliberately excluded: an input may fit the template or
    # validate it, never both.
    from anyuse.execution import proof_split as PS
    fitting = list(fitting_recordings or [])
    fit_inputs = [recording, *fitting]
    if split is None:
        # roles declared by the caller: the recording, its fitting files, its
        # selection inputs and the fresh (validation) recordings
        split = PS.declared(recording, list(fresh_recordings), fitting=fitting,
                            selection=list(selection_recordings or []),
                            min_validation=min_cases)
    split.note_fit(PS.value_of(r) for r in fit_inputs)
    chrome = page_chrome(fit_inputs) if fitting else set()
    # Lever (b): which query parameters are noise is a question only several
    # recordings of DIFFERENT queries can answer, so it is settled here for the
    # same reason page chrome is.
    evidence = volatile_evidence(fit_inputs) if fitting else None
    # Where an awkward parameter sits (part of a path, several
    # segments, a slug) is likewise a question only several recordings of
    # DIFFERENT values can answer, and it is answered here for the same reason.
    from anyuse.execution.api_compile import DEFAULT_LEVERS
    lever_set = levers if levers is not None else DEFAULT_LEVERS
    # A binding site needs three distinct recordings to corroborate it. With
    # one compile and one fitting input (a 1 + 1 + 1 + 2 layout) that bar is
    # never met, so every value that is only PART of a path ("www/nginx",
    # "function.array-map.php") was refused outright. Choosing among
    # binding-site hypotheses is exactly the selection role, so the selection
    # inputs complete the evidence; validation inputs never do.
    selection_inputs = list(selection_recordings or [])
    binding_inputs = list(fit_inputs)
    if len(binding_inputs) < 3 and selection_inputs:
        binding_inputs += selection_inputs
    binding = (binding_evidence(binding_inputs,
                                placeholder=str((recording.get("param") or {}).get("name")
                                                or "q"))
               if fitting and getattr(lever_set, "path_binding", True)
               else None)
    if binding is not None and len(binding_inputs) > len(fit_inputs):
        split.note_selection(PS.value_of(r) for r in binding_inputs[len(fit_inputs):])
    # Lever (f): where a value nested inside decoded layers sits must agree
    # across the fitting recordings, as a path binding's site must. Fitting
    # inputs only; validation inputs never shape the template.
    slot_ev = None
    if fitting and getattr(lever_set, "nested_slots", False):
        from anyuse.execution.api_slots import slot_evidence
        slot_ev = slot_evidence(fit_inputs,
                                placeholder=str((recording.get("param") or {}).get("name")
                                                or "q"))
    compiled: CompileResult = compile_recording(recording, chrome=chrome,
                                                levers=levers, evidence=evidence,
                                                binding=binding,
                                                chain_recordings=fit_inputs if fitting else None,
                                                slot_evidence=slot_ev)
    compiled.chrome_dropped = sorted(chrome)[:20]
    if compiled.ground_truth_reading == "labels":
        # the template explains each row's own name, so every proof case is held
        # to that same reading of its page (api_compile.labels_reading)
        from anyuse.execution.api_compile import labels_reading
        fresh_recordings = [labels_reading(r) or r for r in fresh_recordings]
        selection_recordings = [labels_reading(r) or r for r in (selection_recordings or [])]
        if fitting:
            chrome = page_chrome([labels_reading(r) or r for r in fit_inputs])
    if compiled.template is None:
        detail = f" — {compiled.reason}" if compiled.reason else ""
        return compiled, _settle(EquivalenceResult(
            reason="nothing to verify: extraction failed" + detail), split, compiled)
    from anyuse.execution import field_names as FN
    # recordings that name their fields (``ui.fields``) are compared under the names they carry
    named = [r for r in [recording, *fitting, *fresh_recordings, *(selection_recordings or [])]
             if isinstance((r.get("ui") or {}).get("fields"), dict)]
    if label_keys and not named:
        for candidate in [compiled.template, *compiled.alternatives]:
            FN.label_keys(candidate, recording, chrome)
    if preflight is not None:
        # what the proof is about to send, for the CLI's question about a new host: one request per
        # step for each validation input, plus the selection inputs when runner-ups compete there
        steps = len(getattr(compiled.template, "steps", None) or []) or 1
        n_sel = len(selection_recordings or []) if compiled.alternatives else 0
        fewest = (len(fresh_recordings) + n_sel) * steps
        from anyuse.execution import empty_page as EP
        from anyuse.execution import result_contract as RC
        if RC.supports(compiled.template) and EP.probe_enabled():
            # after a passing proof: the made-up searches that learn the site's empty page
            fewest += EP.PROBES
        compiled.proof_requests = (fewest, fewest + n_sel * len(compiled.alternatives) * steps)
        refusal = str(preflight(compiled) or "")
        if refusal:
            return compiled, _settle(EquivalenceResult(reason=refusal), split, compiled)
    fit_values = [str((item.get("param") or {}).get("value") or "") for item in fitting]
    # where in a longer word of an unspaced script the query may stand: what the site's
    # own result lists did on the FITTING inputs; validation inputs never set it
    positions, positions_why = fitting_positions(
        [labels_reading_of(r, compiled) for r in fit_inputs], chrome)
    selection = list(selection_recordings or [])
    sel_values = [str((item.get("param") or {}).get("value") or "") for item in selection]

    # SELECTION IS SEPARATE FROM VALIDATION. Trying a failed winner's runners-up
    # on the very fresh cases that then count as the proof would let the proof
    # choose its own answer. So: the winner and its equally-plausible runners-up
    # are compared ONLY on dedicated selection recordings (values that are never
    # validation inputs); the one chosen is then validated ONCE on the untouched
    # fresh cases. With no selection recording, the compiler's own winner is
    # validated and no runner-up is tried.
    binding_alts = {id(t) for t in compiled.alternatives
                    if t.path_params or "alternative binding:" in (t.notes or "")}
    chosen = compiled.template
    if compiled.alternatives and selection:
        sel_cases = cases_from_recordings(selection, compiled.param_value, chrome=chrome,
                                          excluded_values=fit_values)
        picked = None
        for cand in [compiled.template, *compiled.alternatives]:
            if preflight is not None and cand is not compiled.template:
                held = compiled.template
                compiled.template = cand
                refusal = str(preflight(compiled) or "")
                compiled.template = held
                if refusal:
                    continue
            split.note_selection(normalise(c.get("value", "")) for c in sel_cases)
            # a chain traces its bindings, and a fields-shaped candidate names its
            # values, in each selection recording with its own paths
            namer = FN.case_namer(cand, recording, chrome, model_scope)
            cand_cases = (cases_from_recordings(selection, compiled.param_value, chrome=chrome,
                                                excluded_values=fit_values, template=cand,
                                                field_namer=namer)
                          if getattr(cand, "steps", None) or namer is not None else sel_cases)
            sel = verify_template(cand, cand_cases, executor,
                                  min_cases=len(cand_cases) or 1, positions=positions)
            if sel.passed:
                picked = cand
                break
            if id(cand) in binding_alts:
                compiled.binding_rejected += 1
        if picked is not None and picked is not compiled.template:
            chosen = picked
            compiled.template = picked
            compiled.status = "ok"
            compiled.reason = (f"the highest-scoring request failed on the selection "
                               f"input; an equally-plausible candidate passed it and was "
                               f"chosen before validation ({picked.url})")
            if id(picked) in binding_alts:
                compiled.binding = {"where": "alternative",
                                    "describe": "a runner-up binding site chosen on a "
                                                "dedicated selection input"}
    elif compiled.alternatives:
        compiled.reason = ((compiled.reason + "; ") if compiled.reason else "") + (
            f"{len(compiled.alternatives)} runner-up(s) not tried: no dedicated selection "
            f"input, and validation inputs are never used to choose")

    cases = cases_from_recordings(fresh_recordings, compiled.param_value, chrome=chrome,
                                  excluded_values=fit_values + sel_values, template=chosen,
                                  field_namer=FN.case_namer(chosen, recording, chrome,
                                                            model_scope))
    # the validation set is exactly what reaches the final check (a fresh
    # recording that repeats a fitting/selection value was dropped above)
    kept = {normalise(c.get("value", "")) for c in cases}
    split.validation = [r for r in split.validation if PS.value_of(r) in kept]
    # The runtime result contract (execution/result_contract.py) is attached
    # BEFORE the final validation, so the validation replays run the very
    # checks the persisted template will run; it replaces the proof-time item
    # floor at runtime.
    from anyuse.execution import result_contract as RC
    RC.attach(chosen)
    RC.set_positions(chosen, positions, positions_why)
    proof = verify_template(chosen, cases, executor, min_cases, positions=positions)
    if proof.passed:
        finalize_recorded_minimum(chosen, proof)
        RC.finalize(chosen, proof)
        # the site's own empty page, pinned in the contract under the proof's digest
        RC.learn_empty_page(chosen, proof, executor or ApiStepExecutor())
    elif compiled.binding.get("where") in ("path_run", "path_part"):
        compiled.binding_rejected += 1       # the chosen split itself did not hold
    return compiled, _settle(proof, split, compiled)


def labels_reading_of(recording: dict, compiled) -> dict:
    """``recording`` held to the reading of its page that ``compiled`` explains
    (each row's own name, ``api_compile.labels_reading``)."""
    if getattr(compiled, "ground_truth_reading", "") == "labels":
        from anyuse.execution.api_compile import labels_reading
        return labels_reading(recording) or recording
    return recording


def fitting_positions(fit_recordings: Sequence[dict], chrome=None) -> Tuple[List[str], str]:
    """``(positions, why)``: where in a run of an unspaced script the query may stand in
    a result's name (``query_match.calibrate``), from the result lists of the FITTING
    recordings only: a list page of at least :data:`MIN_COMPARED` labels that holds its
    own query at the end of or inside a longer word shows the site matches that way."""
    from anyuse.execution import query_match as QM
    cases = [{"value": str((r.get("param") or {}).get("value") or ""),
              "ui_items": visible_items(r, TOP_K, exclude=chrome)} for r in fit_recordings]
    return QM.calibrate(cases, MIN_COMPARED)


def chain_fitting(compiled, candidates: Sequence[dict]) -> Optional[dict]:
    """The recording a chain is fitted on when ``compiled`` found one that its
    recording alone shows (``CompileResult.chain_needs_fitting``): the first of
    ``candidates``, in the caller's order, with JSON traffic of its own for
    another input. None when ``compiled`` needs none or no candidate has such
    traffic (a typed proof case has none)."""
    if not getattr(compiled, "chain_needs_fitting", False):
        return None
    from anyuse.execution.api_compile import eligible_requests
    own = normalise(str(getattr(compiled, "param_value", "") or ""))
    for rec in candidates:
        value = normalise(str((rec.get("param") or {}).get("value") or ""))
        if value and value != own and eligible_requests(rec):
            return rec
    return None


def compile_and_verify_with_fit(recording: dict, fresh_recordings: Sequence[dict],
                                fit_candidates: Sequence[dict], *,
                                selection_recordings: Optional[Sequence[dict]] = None,
                                split=None, min_cases: int = MIN_FRESH_CASES,
                                generate: Optional[bool] = None, browse=None,
                                prefer_fit: Sequence[str] = (), model_scope: str = "",
                                **kwargs):
    """:func:`compile_and_verify` for a source that declares no fitting recording
    (a list of HARs, attach sessions, the learn loop's pages, a recording folder
    without ``NAME.fit1.json``, a recompile's evidence).

    A chain's derived binding needs a second recorded input to corroborate it
    (``api_compile.compile_chain``). When the compile finds a chain that
    ``recording`` alone shows, it compiles once more with :func:`chain_fitting`
    of ``fit_candidates`` as the fitting recording, as ``workflow compile
    --recording DIR`` does with ``NAME.fit1.json``. That recording leaves the
    validation and selection inputs, so it is never a proof case, and the second
    compile runs only while ``min_cases`` validation inputs remain. Every other
    role of ``split`` is kept.

    When no candidate is free for that, the fitting input is generated from the
    site's own search (``execution/chain_fit.py``; ``generate``: default
    ``ANYUSE_CHAIN_FIT``): another result's name from the first recording's
    search, sent with the chain's own requests through the executor and the
    preflight. It is only ever the fitting recording, so every recorded
    validation input stays a proof case. It is tried only for a chain no second
    input corroborated and only when ``min_cases`` validation inputs carry
    traffic the proof can trace a binding in. ``browse`` captures a value's
    page in a browser first (default: ``chain_fit.browser_route``);
    ``prefer_fit`` values are tried before any other; ``model_scope`` is the
    model-check scope the name-field question and the item-page values' naming
    are asked in (``""``: no model is asked). ``kwargs`` go to
    :func:`compile_and_verify`.

    Returns ``(compiled, proof, fitting)``: ``fitting`` is the recording the
    chain was fitted on, or None when the first compile stands. A generated
    fit is named on ``proof.chain_fit``; why none fitted is
    ``compiled.chain_fit_note``."""
    from anyuse.execution import proof_split as PS
    selection = list(selection_recordings or [])
    roles = None
    if split is not None:
        # the caller's roles as they were before the first compile settled them
        roles = (split.held_out, split.why, list(split.seen_fit),
                 list(split.seen_selection), split.min_validation)
    compiled, proof = compile_and_verify(recording, fresh_recordings, min_cases=min_cases,
                                         selection_recordings=selection, split=split,
                                         model_scope=model_scope, **kwargs)
    fit = chain_fitting(compiled, fit_candidates)
    fresh = [r for r in fresh_recordings if r is not fit]
    if fit is not None and len(fresh) >= min_cases:
        rest = [r for r in selection if r is not fit]
        compiled, proof = compile_and_verify(recording, fresh, min_cases=min_cases,
                                             fitting_recordings=[fit], selection_recordings=rest,
                                             split=_refit_split(recording, fresh, fit, rest,
                                                                roles),
                                             model_scope=model_scope, **kwargs)
        return compiled, proof, fit
    if not _may_generate(compiled, generate):
        return compiled, proof, None
    from anyuse.execution import chain_fit as CF
    from anyuse.execution.api_compile import eligible_requests
    fresh = list(fresh_recordings)
    traced = sum(1 for r in fresh if r.get("bindings") or eligible_requests(r))
    if traced < min_cases:
        compiled.chain_fit_note = (
            f"{len(fresh) - traced} of the {len(fresh)} proof input(s) carry no traffic, so "
            f"the chain cannot be proved on them; nothing was sent to fit it")
        return compiled, proof, None
    exclude = [PS.value_of(r) for r in [recording, *fresh, *selection, *fit_candidates]]
    if browse is None:
        browse = CF.browser_route(recording)
    executor = kwargs.get("executor") or ApiStepExecutor()
    kwargs["executor"] = executor
    # what the host question names: the proof's two requests per validation input, plus
    # the fitting input's search and open (fewest) or every try's three requests (most)
    proof_sends = 2 * len(fresh)
    gen = CF.generate(recording, executor=executor, exclude=exclude, prefer=prefer_fit,
                      preflight=kwargs.get("preflight"), browse=browse,
                      model_scope=model_scope,
                      requests=(proof_sends + 2, proof_sends + 3 * CF.FIT_TRIES))
    if gen.recording is None:
        compiled.chain_fit_note = gen.reason
        return compiled, proof, None
    refit_compiled, refit_proof = compile_and_verify(
        recording, fresh, min_cases=min_cases, fitting_recordings=[gen.recording],
        selection_recordings=selection,
        split=_refit_split(recording, fresh, gen.recording, selection, roles,
                           min_validation=min_cases, generated=1),
        model_scope=model_scope, **kwargs)
    if refit_compiled.template is None:
        compiled.chain_fit_note = (f"{gen.value!r} fitted no chain: "
                                   f"{refit_compiled.reason or refit_proof.reason}")
        return compiled, proof, None
    refit_proof.chain_fit = gen.note()
    return refit_compiled, refit_proof, gen.recording


def _may_generate(compiled, generate: Optional[bool]) -> bool:
    """Is ``compiled`` a chain no second recorded input corroborated, with the
    generated fitting input switched on?"""
    from anyuse.execution.api_compile import CHAIN_UNCORROBORATED
    if not getattr(compiled, "chain_needs_fitting", False):
        return False
    if not str(getattr(compiled, "reason", "") or "").startswith(CHAIN_UNCORROBORATED):
        return False
    if generate is None:
        from anyuse.execution import chain_fit as CF
        generate = CF.enabled()
    return bool(generate)


def _refit_split(recording: dict, fresh: Sequence[dict], fit: dict, selection: Sequence[dict],
                 roles, min_validation: int = MIN_FRESH_CASES, generated: int = 0):
    """The split of the compile fitted on ``fit``: the caller's roles when it
    declared them (``roles``), else the roles of the inputs given. None (no roles,
    a recorded fit) lets :func:`compile_and_verify` declare it from its inputs."""
    from anyuse.execution import proof_split as PS
    if roles is None:
        if not generated:
            return None
        return PS.declared(recording, list(fresh), fitting=[fit], selection=list(selection),
                           min_validation=min_validation, generated=generated)
    held_out, why, seen_fit, seen_selection, min_validation = roles
    refit = PS.declared(recording, list(fresh), fitting=[fit], selection=list(selection),
                        min_validation=min_validation, ground_truth_held_out=held_out,
                        why=why, generated=generated)
    refit.note_fit(seen_fit)
    refit.note_selection(seen_selection)
    return refit


def _settle(proof: EquivalenceResult, split, compiled=None) -> EquivalenceResult:
    """Attach status, split counts and the proof scope to a finished proof."""
    from anyuse.execution import proof_split as PS
    split.finalize()
    proof.status = split.status(proof.passed)
    proof.split = split.to_dict(proof.passed_count)
    template = getattr(compiled, "template", None)
    shape = getattr(template, "shape", "list") if template is not None else "list"
    fields = sorted((getattr(template, "extract", {}) or {}).keys()) if template else []
    proof.scope = PS.scope_text({"status": proof.status, "split": proof.split,
                                 "case_count": len(proof.cases)}, shape, fields)
    if proof.passed and proof.status != PS.PROVED:
        proof.reason += f" — status {proof.status}: {split.why}"
    return proof
