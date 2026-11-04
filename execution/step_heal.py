"""Heal a compiled request without a browser, from plain HTTP reads of its result page.

``mcpserver/compiled_tools.run_compiled`` calls :func:`heal` when the compiled request
failed on an answer from the site (not a block, a server error, a timeout or a pre-send
refusal), or was taken out as gone (``api_recompile.mark_gone``), and no lower rung
answered: no browser replay and no agent ran. Two rungs, in order:

1. **step repair** (``step_repair.repair``): one capture of the drift anchor's value says
   which step broke, only that step is re-derived, and the result is re-proved on
   held-out inputs against their result pages.
2. **full recompile** (``api_verify.compile_and_verify``), when step repair cannot
   re-derive the step (the request no longer answers, the value moved, no candidate
   proves): the result pages are the recordings. The anchor's page compiles, the
   selection page fits, the held-out pages validate.

A result page is read with a plain GET through the product's executor (origin policy,
robots.txt, blocked hosts), after ``check_permission`` on the page URL: its host must be
acknowledged and robots.txt must allow the path. A plain GET shows what a browser shows
only when the server renders the list, so two gates decide whether the page can stand in:

* **at compile**: the recorded result page's HTML held the visible labels
  (``api_compile._labels_in_html``), kept as ``record["page"]["html_holds_items"]``;
* **at heal time**: the page as it is now (``fresh_truth.page_truth``): the list derived
  from the anchor's and the selection's pages (``learn_loop.derive_ground_truth``)
  reproduces at least ``step_repair.MIN_ANCHOR_COVER`` of the anchor's items exactly, or,
  for a list that changed since, the tool's own fresh response holds it, a made-up
  search shows it depends on the query and it overlaps the anchor, or a configured
  check model picks one of its lines (the only model call a heal makes). The heal then
  repairs against that list (``info["anchor"]``). A page that lists other things is not
  used.

An HTML tool reads its result page with its own request: step repair re-proves from that
request, and the page check runs before the full recompile only. The re-proof asks no
model.

A heal replaces the template only when the new one takes the same inputs and puts each
of them into the request (:func:`slot_kept`), proves at least the status the tool had,
differs from the old one, and the stored template is still the one it was repaired from.
The previous record is kept as ``compiled_api.previous.json``.

A request out of service that answers the anchor's input again (nothing to repair) goes
back in service when the tool's own request passes its result contract and its answer
holds now (``fresh_truth.answer_holds_now``, the check a probe makes); the summary says
``restored`` and the tool is not marked stale.

Pacing (``api_rescan.HealGuard``, kept on the record as ``heal_guard``): an attempt marks
the tool stale before its first request, so parallel calls make one attempt; a failed
attempt keeps it stale for 30 minutes; a failure within 10 minutes of a heal (a swap or a
restore) marks it stale for 30 minutes. A swapped-in request starts with health of its own
(:func:`fresh_health`). Refused before any request and without an attempt: a signed-in
tool, a tool a registered skill pins, and a tool with nothing to heal from (no anchor, a
page the server does not render, too few inputs, a page host that is not acknowledged).
``ANYUSE_AUTO_HEAL=off`` turns it off.
"""
from __future__ import annotations

import copy
import datetime
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SETTING = "ANYUSE_AUTO_HEAL"
#: the HealGuard op this module paces
OP = "heal"
#: anchor, selection and two held-out inputs
MIN_VALUES = 4
#: what the call result and the record say the evidence was
SOURCE = "plain HTTP reads of the result page"

#: ``read(value) -> (url, html)`` or None
Reader = Callable[[str], Optional[Tuple[str, str]]]


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _norm(value: Any) -> str:
    from anyuse.execution.api_compile import normalise
    return normalise(str(value or ""))


def enabled() -> bool:
    """``ANYUSE_AUTO_HEAL``: on unless switched off."""
    from anyuse.settings import get as get_setting
    try:
        return bool(get_setting(SETTING))
    except Exception:  # noqa: BLE001 - an unreadable settings file keeps the default
        return True


# ---------------------------------------------------------------------------
# the result page
# ---------------------------------------------------------------------------

def _stored_recording(record: Dict[str, Any], base_dir: str = "") -> Optional[Dict[str, Any]]:
    """The compile recording kept at ``source.paths.recording`` (a path relative to the tool's
    folder ``base_dir`` when it was kept there), or None."""
    import json
    import os
    path = str(((record.get("source") or {}).get("paths") or {}).get("recording") or "")
    if path and not os.path.isabs(path) and base_dir:
        path = os.path.join(str(base_dir), path)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def page_block(record: Dict[str, Any], base_dir: str = "") -> Optional[Dict[str, Any]]:
    """``record["page"]``; for a record compiled before it was stored, built from the
    kept compile recording."""
    page = record.get("page")
    if isinstance(page, dict) and page.get("url"):
        return page
    recording = _stored_recording(record, base_dir)
    if recording is None:
        return None
    from anyuse.execution.api_compile import result_page
    return result_page(recording)


def page_template(page: Dict[str, Any], param: str):
    """A GET of the result page that reads nothing: ``step_repair.capture`` returns its HTML."""
    from anyuse.execution.api_step import RequestTemplate
    return RequestTemplate(
        method="GET", url=str(page.get("url") or ""), query=dict(page.get("query") or {}),
        params=[param], path_params=dict(page.get("path_params") or {}),
        param_transforms=dict(page.get("transforms") or {}), extract={},
        extract_kind="html", shape="list",
        expect={"status_in": [200], "content_type": "html"},
        notes="the tool's result page, read with a plain GET to heal the tool")


def page_reader(template, executor) -> Reader:
    """Reads each value's result page once; a page that did not answer 2xx HTML is None."""
    from anyuse.execution import step_repair as SR
    cache: Dict[str, Optional[Tuple[str, str]]] = {}

    def read(value: str) -> Optional[Tuple[str, str]]:
        key = _norm(value)
        if key not in cache:
            cap = SR.capture(template, value, executor)
            cache[key] = (cap.url, cap.body) if cap.ok and cap.body else None
        return cache[key]
    return read


def _page_recording(value: str, param: str, got: Tuple[str, str]) -> Dict[str, Any]:
    from anyuse.execution import step_repair as SR
    url, body = got
    return SR._recording(value, param, url, body, implied_end_tags=True)


# ---------------------------------------------------------------------------
# which inputs re-prove
# ---------------------------------------------------------------------------

def heal_values(record: Dict[str, Any], call_value: str, page: Optional[Dict[str, Any]] = None,
                base_dir: str = "") -> Tuple[str, List[str], str]:
    """``(selection, held_out, reason)``. Held out: the inputs the tool's proof held out,
    minus the anchor's, plus this call's input while fewer than two remain. Selection: an
    input the tool was compiled or fitted on, else this call's input. ``reason`` is set
    when fewer than :data:`MIN_VALUES` distinct inputs result."""
    from anyuse.execution import step_repair as SR
    from anyuse.execution.auto_recompile import proof_values
    anchor = SR.anchor_of(record) or {}
    taken = {_norm(anchor.get("value"))}
    held: List[str] = []
    for value in proof_values(record):
        if _norm(value) not in taken:
            held.append(value)
            taken.add(_norm(value))
    call = str(call_value or "")
    if len(held) < 2 and _norm(call) and _norm(call) not in taken:
        held.append(call)
        taken.add(_norm(call))
    stored = (_stored_recording(record, base_dir) or {}).get("param") or {}
    pool = [*((page or {}).get("values") or []),
            *((record.get("browser") or {}).get("fitted_on") or []),
            stored.get("value"), call]
    selection = next((str(v) for v in pool if _norm(v) and _norm(v) not in taken), "")
    if not selection or len(held) < 2:
        return selection, held, (f"{1 + len(held) + bool(selection)} distinct input(s) to "
                                 f"re-prove with, {MIN_VALUES} needed (the last good answer, "
                                 f"one to select with, two held out)")
    return selection, held, ""


# ---------------------------------------------------------------------------
# swap rules
# ---------------------------------------------------------------------------

def slot_kept(old, new) -> Tuple[bool, str]:
    """Does ``new`` take exactly ``old``'s inputs and put each into the request?"""
    names = list(getattr(old, "input_params", None) or getattr(old, "params", None) or [])
    have = list(getattr(new, "input_params", None) or getattr(new, "params", None) or [])
    if sorted(names) != sorted(have):
        return False, f"the new request takes {have}, the tool takes {names}"
    probe = {n: f"zq{i}slot" for i, n in enumerate(names)}
    try:
        url, _headers, body = new.render(dict(probe))
    except (KeyError, ValueError) as exc:
        return False, f"the new request cannot be filled in: {exc}"
    sent = urllib.parse.unquote(str(url)) + " " + str(body or "")
    lost = [n for n, v in probe.items() if v not in sent]
    if lost:
        return False, f"the new request does not send {lost}"
    return True, ""


def _reads(template) -> Dict[str, Any]:
    """The request and what it reads, without notes or the calibrated contract."""
    d = template.to_dict() if hasattr(template, "to_dict") else dict(template or {})
    keys = ("method", "url", "query", "headers", "body", "extract", "extract_kind", "embedded",
            "path_params", "param_transforms", "slots", "html_parser")
    return {k: d.get(k) for k in keys}


# ---------------------------------------------------------------------------
# the heal
# ---------------------------------------------------------------------------

class _Counted:
    """The executor, counting the requests that reached the site."""

    def __init__(self, executor):
        self._executor = executor
        self.sent = 0

    def execute(self, template, params=None, *, _probe: bool = False):
        if _probe:
            from anyuse.execution.result_contract import probe_unsupported, takes_probe
            if not takes_probe(self._executor):
                return probe_unsupported()
            res = self._executor.execute(template, params, _probe=True)
        else:
            res = self._executor.execute(template, params)
        if isinstance(res, dict) and (isinstance(res.get("status"), int)
                                      or str(res.get("error") or "").startswith("transport error")):
            self.sent += 1
        return res

    def __getattr__(self, name):
        return getattr(self._executor, name)


@dataclass
class _Plan:
    template: Any
    anchor: Dict[str, Any]
    param: str
    selection: str
    held_out: List[str]
    page: Optional[Any] = None              # the result page's GET, a RequestTemplate
    page_url: str = ""


def _plan(record: Dict[str, Any], params: Dict[str, Any], hosts_path: Optional[str],
          base_dir: str = "") -> Tuple[Optional[_Plan], str]:
    """What a heal would read, or why there is nothing to heal from. Sends nothing."""
    from anyuse.execution import step_repair as SR
    from anyuse.execution.api_permission import host_acknowledged
    from anyuse.execution.api_step import RequestTemplate, load_command
    template = load_command(record.get("template") or {})
    if not isinstance(template, RequestTemplate) or template.shape != "list":
        return None, "only a single-request list tool is healed automatically"
    names = list(template.input_params or template.params)
    if len(names) != 1 or names[0] not in params:
        return None, "only a tool with one input is healed automatically"
    param = names[0]
    anchor = SR.anchor_of(record)
    if anchor is None:
        return None, ("the tool has no last good answer with at least "
                      f"{SR.MIN_ANCHOR} items to repair against")
    page = page_block(record, base_dir)
    if template.extract_kind == "html":
        page_t = page_template(page, param) if page else SR._probe(template)
    elif not page:
        return None, ("no result page with the input in its address was recorded for this tool, "
                      "so it cannot be read without a browser")
    elif not page.get("html_holds_items"):
        return None, ("the tool's result page is filled in by a script, so a plain read of it "
                      "does not show the results; it heals only through a browser replay")
    else:
        page_t = page_template(page, param)
    try:
        page_url = page_t.render({param: str(anchor["value"])})[0]
    except (KeyError, ValueError) as exc:
        return None, f"the result page's address cannot be filled in: {exc}"
    host = urllib.parse.urlsplit(page_url).netloc
    if not host_acknowledged(host, hosts_path):
        return None, (f"the result page's host {host} is not acknowledged; run "
                      f"`anyuse workflow acknowledge-host {host}`")
    selection, held, why = heal_values(record, str(params.get(param, "")), page, base_dir)
    if why:
        return None, why
    return _Plan(template=template, anchor=anchor, param=param, selection=selection,
                 held_out=held, page=page_t, page_url=page_url), ""


def _guard(record: Dict[str, Any], clock: Callable[[], float]):
    from anyuse.execution.api_rescan import HealGuard
    return HealGuard.from_dict(record.get("heal_guard") or {}, clock=clock)


def _until(guard) -> str:
    until = float((guard.ops.get(OP) or {}).get("stale_until") or 0.0)
    if until <= guard.clock():
        return ""
    return datetime.datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds")


def _out_of_service(record: Dict[str, Any]) -> bool:
    from anyuse.execution.api_recompile import ApiHealth
    return not ApiHealth.from_dict(record.get("health")).preferred


def fresh_health(record: Dict[str, Any]) -> Dict[str, Any]:
    """The health of a request a heal swapped in: its own runs only. The old request's runs
    and failures say nothing about the new one; the automatic-recompile pacing carries over."""
    from anyuse.execution.api_recompile import ApiHealth
    old = ApiHealth.from_dict(record.get("health"))
    return ApiHealth(recompile_attempts=old.recompile_attempts,
                     last_recompile_at=old.last_recompile_at).to_dict()


def _status_of(record: Dict[str, Any]) -> str:
    from anyuse.execution import proof_split as PS
    proof = record.get("proof") or {}
    return str(proof.get("status") or PS.PROVED) if proof.get("passed") else PS.NOT_PROVED


def _recompile(record: Dict[str, Any], plan: _Plan, read: Reader, fit: List[Dict[str, Any]],
               executor, hosts_path: Optional[str], kept: Optional[Dict[str, Any]] = None
               ) -> Tuple[Optional[Dict[str, Any]], str]:
    """The full recompile from the result pages; ``(new record or None, reason)``. ``kept``
    receives the pages the new request was proved from (``evidence_store``)."""
    from anyuse.execution import proof_split as PS
    from anyuse.execution.api_step import signed_in_template
    from anyuse.execution.api_verify import compile_and_verify, permission_preflight
    from anyuse.execution.html_extract import extract
    from anyuse.execution.workflow_schema import template_digest
    selector = str(fit[0].get("ground_truth_selector") or "")
    visible = any(r.get("ground_truth_visible_only") for r in fit)
    fitted_on = [plan.anchor["value"], plan.selection]
    fresh = []
    for value in plan.held_out:
        got = read(value)
        if not got:
            return None, f"the result page for {value!r} did not answer with HTML"
        rec = _page_recording(value, plan.param, got)
        rec["ground_truth_selector"] = selector
        rec["ground_truth_fitted_on"] = list(fitted_on)
        if visible:
            rec["ground_truth_visible_only"] = True
        rec["ui"]["items"] = extract(rec["dom"], selector, limit=30, visible_only=visible,
                                     implied_end_tags=True)
        rec["ui"]["labels"] = extract(rec["dom"], selector, limit=30, visible_only=visible,
                                      labels=True, implied_end_tags=True)
        fresh.append(rec)
    parts = urllib.parse.urlsplit(plan.page_url)
    origins = [f"{parts.scheme}://{parts.netloc}"]
    compiled, proof = compile_and_verify(
        fit[0], fresh, executor, fitting_recordings=[fit[1]],
        preflight=lambda c: permission_preflight(c, fit[0], allowed_origins=origins,
                                                 hosts_path=hosts_path))
    if compiled.template is None or not proof.passed:
        return None, f"the recompile did not prove a request: {proof.reason or compiled.reason}"
    if kept is not None:
        kept.update({"recording": fit[0], "fitting": [fit[1]], "fresh": fresh})
    prior = _status_of(record)
    new = copy.deepcopy(record)
    new["template"] = compiled.template.to_dict()
    new_proof = proof.to_dict()
    anchor = new_proof.pop("drift_anchor", None)
    status = PS.weaker(proof.effective_status, prior)
    new_proof["status"] = status
    if status != proof.effective_status:
        split = dict(new_proof.get("split") or {})
        split["held_out"] = False
        split["why"] = ((split.get("why") + "; ") if split.get("why") else "") + (
            f"inherited status {prior!r} from the record it replaces")
        new_proof["split"] = split
        new_proof["selection_held_out"] = False
    new_proof["template_digest"] = template_digest(new["template"])
    new["proof"] = new_proof
    if isinstance(anchor, dict) and not signed_in_template(compiled.template):
        new["drift_anchor"] = anchor
    new["chrome_filtered"] = sorted(compiled.chrome_dropped)
    new["health"] = fresh_health(record)
    return new, f"recompiled from the result pages and re-proved: {proof.reason}"


def _response_data(template, cap) -> Any:
    """The tool's own fresh response as data, when it answered: its JSON, or the payload an
    embedded-JSON tool reads from the page."""
    if not cap.ok:
        return None
    if template.extract_kind == "embedded_json":
        from anyuse.execution.embedded_json import select_blob
        try:
            return select_blob(cap.body, template.embedded)
        except Exception:  # noqa: BLE001 - no payload is no evidence
            return None
    return cap.data


def _attempt(record: Dict[str, Any], plan: _Plan, executor, hosts_path: Optional[str],
             seed: str = "") -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Step repair, then the full recompile; ``(new record or None, what happened)``.

    ``info["truth"]`` summarises the page check (``fresh_truth.page_truth``); when it
    found that the page lists this query's results under another anchor than the stored
    one, ``info["anchor"]`` is that anchor and ``info["anchor_refreshed"]`` is set."""
    from anyuse.execution import fresh_truth as FT
    from anyuse.execution import result_contract as RC
    from anyuse.execution import step_repair as SR
    from anyuse.execution.api_permission import check_permission
    info: Dict[str, Any] = {"rung": "", "localised": "", "relearned": [], "reason": "",
                            "model_calls": 0}
    is_html = plan.template.extract_kind == "html"
    # which step broke, before any page is read: a request that answers the anchor's
    # input again needs no repair
    fresh = SR.capture(plan.template, str(plan.anchor["value"]), executor)
    loc = SR.localise(plan.template, fresh, plan.anchor)
    if loc.step in ("none", "unknown"):
        info.update({"rung": "step_repair", "localised": loc.step,
                     "reason": ("nothing to repair: " if loc.step == "none" else "") + loc.evidence})
        return None, info
    if not is_html:
        verdict = check_permission(plan.page_url, hosts_path=hosts_path)
        if not verdict.may_prefer:
            info["reason"] = f"the result page may not be read: {verdict.reason}"
            return None, info
    read = page_reader(plan.page, executor)
    contract = RC.contract_of(plan.template) if is_html else None

    def check(api_data: Any = None):
        truth = FT.page_truth(read, plan.anchor, plan.selection, plan.param, api_data=api_data,
                              contract=contract, seed=seed)
        info["truth"] = truth.summary()
        info["model_calls"] = int(info.get("model_calls") or 0) + truth.model_calls
        return truth

    def refreshed(anchor: Dict[str, Any], why: str) -> Tuple[None, Dict[str, Any]]:
        info.update({"rung": "step_repair", "localised": "none", "anchor": anchor,
                     "anchor_refreshed": True,
                     "reason": f"nothing to repair against the page read now: {why}"})
        return None, info

    def repair(anchor: Dict[str, Any]):
        new, out = SR.repair(record, executor=executor, held_out=list(plan.held_out),
                             selection=plan.selection, anchor=anchor,
                             ui_page=None if is_html else read, ui_page_raw=True, fresh=fresh)
        info.update({"rung": "step_repair", "localised": out.localised,
                     "relearned": list(out.relearned), "reason": out.reason,
                     "status": out.status})
        return new, out

    truth = None
    anchor = plan.anchor
    if not is_html:
        # the page as it is now, before step repair re-derives anything against the anchor
        truth = check(_response_data(plan.template, fresh)
                      if loc.step in ("extraction_region", "record_segmentation") else None)
        if not truth.verdict:
            info["reason"] = truth.why()
            return None, info
        if truth.refreshed and truth.anchor:
            anchor = truth.anchor
            again = SR.localise(plan.template, fresh, anchor)
            if again.step == "none":
                return refreshed(anchor, again.evidence)
    new, out = repair(anchor)
    if new is not None:
        return new, info
    if out.localised in ("none", "unknown"):
        return None, info
    step_why = out.reason
    if is_html:
        # an HTML tool's request is its page: the page check runs once step repair failed
        truth = check(None)
        if truth.verdict and truth.refreshed and truth.anchor:
            new, again = repair(truth.anchor)
            if new is not None:
                return new, info
            if again.localised == "none":
                return refreshed(truth.anchor, again.evidence)
            step_why = f"{step_why}; against the page read now: {again.reason}"
        if not truth.verdict:
            info["reason"] = f"step repair: {step_why}; recompile: {truth.why()}"
            return None, info
    kept: Dict[str, Any] = {}
    new, why = _recompile(record, plan, read, truth.pages, executor, hosts_path, kept)
    if kept:
        info["evidence"] = kept
    info.update({"rung": "recompile", "reason": f"step repair: {step_why}; {why}",
                 "relearned": ["request", "extraction"] if new is not None else []})
    if new is not None:
        info["status"] = str((new.get("proof") or {}).get("status") or "")
    return new, info


def note_failure(storage, workflow_id: str, reason: str = "",
                 clock: Callable[[], float] = time.time,
                 ran: Optional[Dict[str, Any]] = None) -> str:
    """A compiled request failed on an answer from the site: a failure within 10 minutes
    of a heal marks the tool stale. Writes only then; returns the stale reason or ``""``.
    ``ran``: the record the call ran; a failure of a request that has since been replaced
    says nothing about the new one."""
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    from anyuse.execution.api_recompile import _same_request, workflow_lock
    with workflow_lock(storage, workflow_id):
        stored = load_compiled(storage, workflow_id)
        if not stored or not (stored.get("heal_guard") or {}).get("ops"):
            return ""
        if ran is not None and not _same_request(stored, ran):
            return ""
        guard = _guard(stored, clock)
        stale = guard.failed(OP, reason[:200])
        if stale:
            stored["heal_guard"] = guard.to_dict()
            persist_compiled(storage, workflow_id, stored)
        return stale


def heal(storage, workflow_id: str, params: Dict[str, Any], *, executor,
         hosts_path: Optional[str] = None, why: str = "", pinned_by: Sequence[str] = (),
         clock: Callable[[], float] = time.time,
         ran: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One paced heal attempt; the summary a call result carries as ``healed`` (swapped) or
    ``heal`` (not swapped). ``ran``: the record the failing call ran; when another call has
    replaced its request since, nothing is attempted and the pacing is left alone."""
    from anyuse.execution import proof_split as PS
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    from anyuse.execution.api_permission import unattended
    from anyuse.execution.api_recompile import (RecompileManager, _same_request, mark_restored,
                                                workflow_lock)
    from anyuse.execution.auto_recompile import signed_in
    from anyuse.execution.api_step import load_command

    record = load_compiled(storage, workflow_id) or {}
    if not record:
        return {"attempted": False, "swapped": False, "reason": "no compiled record"}
    if ran is not None and not _same_request(record, ran):
        return {"attempted": False, "swapped": False,
                "reason": ("the tool's request was replaced while this call ran (another call "
                           "healed or recompiled it): call it again")}
    if signed_in(record):
        return {"attempted": False, "swapped": False,
                "reason": ("a signed-in tool is not healed automatically: run `anyuse attach "
                           "--login-replay` again, then `anyuse compile last`")}
    if pinned_by:
        return {"attempted": False, "swapped": False,
                "reason": (f"skill(s) {', '.join(repr(s) for s in pinned_by)} pin this tool's "
                           f"proof and would stop at their next run: recompile it yourself "
                           f"and pin the new proof in them")}
    base_dir = str(storage.get_workflow_dir(workflow_id))
    plan, refused = _plan(record, params, hosts_path, base_dir)

    with workflow_lock(storage, workflow_id):
        stored = load_compiled(storage, workflow_id)
        if not stored or not _same_request(stored, record):
            return {"attempted": False, "swapped": False,
                    "reason": "the compiled request changed while this call ran"}
        guard = _guard(stored, clock)
        stale = guard.failed(OP, why[:200])
        may, _wait = guard.check(OP)
        if stale or not may or plan is None:
            if stale:
                stored["heal_guard"] = guard.to_dict()
                persist_compiled(storage, workflow_id, stored)
            until = _until(guard)
            if stale:
                reason = (f"it failed again soon after a heal, so it is not healed again before "
                          f"{until}; if the site changed for good, record the search again and "
                          f"recompile it")
            elif not may:
                reason = (f"not healed again before {until}: "
                          f"{(guard.ops.get(OP) or {}).get('stale_reason') or 'a heal failed'}")
            else:
                reason = refused
            out = {"attempted": False, "swapped": False, "reason": reason}
            if not may:
                out["next_attempt_after"] = until
            return out
        row = guard.ops.setdefault(OP, {})
        row["stale_until"] = clock() + guard.stale_s
        row["stale_reason"] = f"a heal started at {_now()} and did not finish"
        stored["heal_guard"] = guard.to_dict()
        persist_compiled(storage, workflow_id, stored)

    t0 = time.time()
    counted = _Counted(executor)
    try:
        with unattended("recompile"):
            new, info = _attempt(record, plan, counted, hosts_path, seed=workflow_id)
            if new is None and info.get("localised") == "none" and _out_of_service(record):
                # the request answers the anchor's input again: the check a probe makes
                # (mcpserver/compiled_tools.run_compiled) decides whether it goes back in service
                from anyuse.execution import fresh_truth as FT
                anchor = info.get("anchor") or plan.anchor
                values = {plan.param: str(anchor["value"])}
                holds, why_holds, detail = FT.answer_holds_now(
                    record, plan.template, anchor, counted.execute(plan.template, values),
                    executor=counted, params=values, hosts_path=hosts_path, base_dir=base_dir)
                info["restore"] = holds
                info["model_calls"] = int(info.get("model_calls") or 0) + int(detail.get("model_calls") or 0)
                info["reason"] = f"{info.get('reason') or ''}; {why_holds}"
    except Exception as exc:  # noqa: BLE001 - a failed heal never fails the call
        logger.warning("heal of %r stopped: %s", workflow_id, type(exc).__name__)
        new, info = None, {"rung": "", "reason": f"the heal stopped: {type(exc).__name__}: "
                                                  f"{str(exc)[:160]}"}
    summary = {"attempted": True, "swapped": False, "from": SOURCE,
               "rung": info.get("rung") or "", "localised": info.get("localised") or "",
               "relearned": list(info.get("relearned") or []),
               "requests": counted.sent, "model_calls": int(info.get("model_calls") or 0),
               "browser": False, "seconds": round(time.time() - t0, 3),
               "reason": str(info.get("reason") or "")[:400]}
    if info.get("truth"):
        summary["truth"] = dict(info["truth"])
    prior = _status_of(record)
    if new is not None:
        status = str((new.get("proof") or {}).get("status") or "")
        kept, lost = slot_kept(plan.template, load_command(new.get("template") or {}))
        if not kept:
            new, summary["reason"] = None, f"not swapped: {lost}"
        elif PS.weaker(status, prior) != prior:
            new, summary["reason"] = None, (f"not swapped: the new request reached {status!r}, "
                                            f"weaker than the tool's {prior!r}")
        else:
            summary["status"] = status

    with workflow_lock(storage, workflow_id):
        stored = load_compiled(storage, workflow_id) or {}
        guard = _guard(stored, clock)
        if new is not None and not (stored and _same_request(stored, record)):
            new, summary["reason"] = None, "not swapped: the compiled request changed while the heal ran"
        if new is not None and guard.heal_succeeded(OP, _reads(plan.template),
                                                    _reads(load_command(new["template"]))):
            RecompileManager(storage, None).save_previous(workflow_id, stored)
            at = _now()
            summary["swapped"] = True
            new["heal_guard"] = guard.to_dict()
            new["healed"] = {"at": at, "from": SOURCE, "rung": summary["rung"],
                             "localised": summary["localised"],
                             "relearned": summary["relearned"], "status": summary.get("status"),
                             "requests": summary["requests"],
                             "model_calls": summary["model_calls"],
                             **({"truth": summary["truth"]} if summary.get("truth") else {}),
                             "previous_template_digest": str((record.get("proof") or {})
                                                             .get("template_digest") or ""),
                             "previous_record": "compiled_api.previous.json"}
            new["heal"] = {"at": at, "swapped": True, "rung": summary["rung"],
                           "reason": summary["reason"][:300], "requests": summary["requests"]}
            new["recompiled_at"] = at
            if info.get("evidence"):
                # the result pages the new request was proved from become its kept recordings
                from anyuse.execution import evidence_store as ES
                ES.keep_compile_evidence(storage, workflow_id, new, **info["evidence"])
            persist_compiled(storage, workflow_id, new)
            return summary
        if new is None and info.get("restore") and stored and _same_request(stored, record):
            # nothing to repair and the request gives the last good answer again: back in
            # service, and the mark this attempt set is lifted instead of kept for 30 minutes
            row = guard.ops.setdefault(OP, {})
            row.pop("stale_until", None)
            row.pop("stale_reason", None)
            # a failure soon after this marks the tool stale, as after a swap
            row["healed_at"] = clock()
            summary["restored"] = True
            summary["reason"] = (f"{summary['reason']}; the compiled request answers again, so "
                                 f"it is back in service")[:400]
            mark_restored(stored, summary["reason"], "heal")
            if info.get("anchor_refreshed") and info.get("anchor"):
                stored["drift_anchor"] = dict(info["anchor"])
            stored["heal_guard"] = guard.to_dict()
            stored["heal"] = {"at": _now(), "swapped": False, "restored": True,
                              "rung": summary["rung"], "reason": summary["reason"][:300],
                              "requests": summary["requests"]}
            persist_compiled(storage, workflow_id, stored)
            return summary
        if new is not None:
            summary["reason"] = "not swapped: the repaired request is the same as the old one"
        if stored:
            guard.heal_failed(OP, summary["reason"][:200])
            if info.get("anchor_refreshed") and info.get("anchor") \
                    and info.get("localised") == "none" and _same_request(stored, record):
                # the tool's own request reproduces the list the page shows now: that list is
                # its last good answer
                stored["drift_anchor"] = dict(info["anchor"])
            stored["heal_guard"] = guard.to_dict()
            stored["heal"] = {"at": _now(), "swapped": False, "rung": summary["rung"],
                              "reason": summary["reason"][:300], "requests": summary["requests"]}
            persist_compiled(storage, workflow_id, stored)
        summary["next_attempt_after"] = _until(guard)
    return summary
