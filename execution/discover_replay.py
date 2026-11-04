"""Discovery: browser-replay tools as a first-class result (tier 2, "replay").

When no compiled request reproduces a capability but a deterministic browser
replay does, the learn loop's unchanged tier-2 check (``learn_loop._try_tier2``:
selector fitted on the compile + selection recordings, then each VALIDATION
value replayed fresh and compared, coverage >= 0.80) proves it. This module is
the executor that check calls in discovery, in two modes:

* ``navigate``: open the found URL template with the value bound (a GET form,
  OpenSearch, a pattern or an item template whose answer only exists after the
  page's own scripts ran);
* ``type``: open the page the search input was found on, type the value into
  THAT input (matched by name / id, else by position), press Enter, read the
  result region. Nothing else is clicked or typed.

Every page is opened by ``opener`` — in the bench, the guarded container browser
(``execution/cdp_guard.py``: every request paced before it is sent, same-host
only, robots-gated, 30 per load / 200 per host). A replay's value-bearing
requests are never answered from the run cache. Zero model calls, asserted.

A proved replay tool is persisted as a record with ``tier: "replay"``
(:func:`persist_tools`), next to the tier-1 (``tier: "compiled"``) ones.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from anyuse.execution.cdp_replay import ReplayTemplate, template_from_recording


@dataclass
class TypedReplayTemplate(ReplayTemplate):
    """A replay that types the value instead of binding it into a URL (``visible_only``
    is the base template's field)."""
    mode: str = "type"
    input: Dict[str, Any] = field(default_factory=dict)

    def render(self, params: Optional[Dict[str, Any]] = None) -> str:
        return self.url

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(mode=self.mode, input=dict(self.input))
        return d


class GuardedReplay:
    """``execute(template, params)`` with the replay-executor contract.

    ``opener(url, value)`` opens one page; ``typed_opener(url, value, input)``
    types into it. Both return an ``AgentRun``-like object (dom, text, final_url,
    error) and must be the guarded browser."""

    def __init__(self, opener: Callable, typed_opener: Optional[Callable] = None,
                 host_blocked: Optional[Callable[[str], str]] = None):
        self.opener = opener
        self.typed_open = typed_opener
        self.host_blocked = host_blocked or (lambda h: "")
        #: incremented nowhere; reported so the claim is checkable
        self.model_calls = 0
        self.runs = 0

    # -- the template, fitted on the compile + selection recordings only -------

    def template_from_recording(self, base: dict, param: str, recordings=None,
                                readiness=None) -> ReplayTemplate:
        typed = base.get("typed_replay") or {}
        selector = str(base.get("ground_truth_selector") or "")
        if typed:
            return TypedReplayTemplate(
                url=str(typed.get("start_url") or ""), params=[param],
                input=dict(typed.get("input") or {}), extract={"items": selector},
                visible_only=bool(base.get("ground_truth_visible_only")),
                expect={"selector": selector, "min_items": 1},
                origin="discover-typed",
                notes="type the value into the recorded search input, press Enter, "
                      "read the result region with the fitted selector")
        return template_from_recording(base, param, recordings=recordings, readiness=False)

    # -- one replay --------------------------------------------------------------

    def execute(self, template: ReplayTemplate,
                params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from anyuse.execution.html_extract import extract
        from anyuse.execution.learn_loop import same_url
        base = {"method": "cdp", "modality": "cdp", "model_calls": 0}
        params = dict(params or {})
        sel = (template.extract or {}).get("items") or ""
        if not sel:
            return {**base, "success": False, "error": "no extraction selector"}
        name = (template.params or ["q"])[0]
        value = str(params.get(name, ""))
        typed = isinstance(template, TypedReplayTemplate)
        try:
            url = template.render(params)
        except KeyError as exc:
            return {**base, "success": False, "error": str(exc)}
        host = urllib.parse.urlsplit(url).netloc.lower()
        why = self.host_blocked(host)
        if why:
            return {**base, "success": False, "blocked": True, "error": f"host blocked: {why}"}
        t0 = time.monotonic()
        if typed:
            if self.typed_open is None:
                return {**base, "success": False, "error": "no typing browser"}
            run = self.typed_open(url, value, dict(template.input))
        else:
            run = self.opener(url, value)
        self.runs += 1
        elapsed = (time.monotonic() - t0) * 1000.0
        if getattr(run, "error", ""):
            return {**base, "success": False, "url": url, "elapsed_ms": elapsed,
                    "blocked": str(run.error).startswith("blocked"),
                    "error": str(run.error)[:200]}
        final = getattr(run, "final_url", "") or url
        if urllib.parse.urlsplit(final).netloc.lower() != host:
            return {**base, "success": False, "url": final, "elapsed_ms": elapsed,
                    "error": f"the replay left the host: {final[:100]}"}
        if not typed:
            ok, why = same_url(url, final)
            if not ok:
                return {**base, "success": False, "url": final, "elapsed_ms": elapsed,
                        "error": f"postcondition identity failed: {why}"}
        try:
            items = extract(run.dom or "", sel, limit=100,
                            visible_only=bool(getattr(template, "visible_only", False)))
        except ValueError as exc:
            return {**base, "success": False, "url": final, "elapsed_ms": elapsed,
                    "error": f"HTML parser refused input: {exc}"}
        if not items:
            # the browser replay's cascade (result_contract.empty_verdict): the site's own
            # empty page when the template pins one, then a statement bound to the query.
            # No context is handed over and no request is sent, so no model is asked.
            from anyuse.execution import empty_page as EP
            from anyuse.execution import result_contract as RC
            contract = RC.contract_of(template) or RC.replay_contract(name, positions=None)
            ev = RC.empty_verdict(contract, EP.Page(
                "html", value, markup=run.dom or "", text=getattr(run, "text", "") or "",
                url=final))
            if ev.state == EP.EMPTY:
                return {**base, "success": True, "url": final, "elapsed_ms": elapsed,
                        "extracted": {"items": []}, "empty_state": ev.line,
                        "empty_by": ev.by}
            return {**base, "success": False, "url": final, "elapsed_ms": elapsed,
                    "error": "the replay extracted nothing and the page does not say it "
                             f"found nothing for this query ({ev.why[:300]})"}
        return {**base, "success": True, "url": final, "elapsed_ms": elapsed,
                "extracted": {"items": items}}


def tool_records(host: str, caps: Dict[str, dict]) -> List[dict]:
    """One record per DISTINCT proved template: tier "compiled" (1) or "replay" (2)."""
    out, seen = [], set()
    for name, c in caps.items():
        b = (c or {}).get("best") or {}
        if b.get("status") != "proved" or b.get("template") in seen:
            continue
        seen.add(b.get("template"))
        proof = b.get("proof") or {}
        tier = int(proof.get("tier") or 1)
        rec = {"host": host, "capability": name, "tier": "replay" if tier == 2 else "compiled",
               "proof_kind": b.get("proof_kind") or "learn", "source": b.get("source"),
               "template": b.get("template"), "param": b.get("param"),
               "model_calls_on_execution": 0,
               "later_run_ms": proof.get("later_run_ms"),
               "validation_values": proof.get("validation_values") or [],
               "reason": str(b.get("reason") or "")[:300]}
        if tier == 2:
            rec["replay"] = proof.get("replay_tool") or {}
        out.append(rec)
    return out


def persist_tools(records: List[dict], data_dir: str) -> str:
    """Append the records to ``<data_dir>/discovered_tools.jsonl`` (a scratch
    registry in the bench). Returns the path."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "discovered_tools.jsonl")
    with open(path, "a") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")
    return path
