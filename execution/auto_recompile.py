"""Automatic recompilation of a compiled web request the runtime cascade took out of service.

``mcpserver/compiled_tools.run_compiled`` calls :func:`recompile` when the compiled
request failed (or was already out of service) and a lower rung answered. The evidence,
freshest first:

1. **live** — the browser replay's own traffic: the result page it loaded for one input
   with the GET responses that page made, as the compile recording; the replayed result
   pages of the tool's proof inputs, read with the browser rung's selector, as the
   validation set. For a search-then-open chain, a second page is captured too: an input
   the browser rung was fitted on, else another result's name from the compile page's
   own search (``execution/chain_fit.py``), and it fits the chain. Needs the browser rung
   and a driver that captures traffic (``cdp_replay.CdpDriver`` does when asked).
2. **kept calls** — the pages of the last calls the agent rung answered, with the GET
   responses each page made, kept in the tool's folder (``evidence_store.call_evidence``),
   once four inputs are kept.
3. **stored** — the recordings or attach sessions the tool was compiled from: kept in the
   tool's folder (``source.paths`` relative to it, ``evidence_store``), else the files
   the compile read when they are still there (attach sessions by name in the sessions
   directory).

The proof is the existing one: ``api_verify.compile_and_verify_with_fit`` (or
``attach.compile_sessions``) behind ``permission_preflight`` — origin policy, robots.txt
and host acknowledgement — run through ``RecompileManager.recompile_now``, which paces
the attempts and swaps only on a proof at the tool's own status. An attempt that found no
evidence without loading a page is not counted.

Never recompiled automatically: a signed-in (login=replay) tool, and a tool whose current
proof a registered skill pins (the swap would stop that skill at its fresh check).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: ``replay(params, capture_traffic) -> ReplayExecutor.execute result``
Replay = Callable[[Dict[str, Any], bool], Dict[str, Any]]


def _norm(value: Any) -> str:
    from anyuse.execution.api_compile import normalise
    return normalise(str(value or ""))


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def proof_values(record: Dict[str, Any]) -> List[str]:
    """The inputs the tool's proof held out (its passed validation cases), in order."""
    out: List[str] = []
    for case in (record.get("proof") or {}).get("cases") or []:
        if isinstance(case, dict) and case.get("passed") and case.get("value") not in (None, ""):
            value = str(case["value"])
            if _norm(value) not in {_norm(v) for v in out}:
                out.append(value)
    return out


def _page_recording(record: Dict[str, Any], param: str, value: str, res: Dict[str, Any],
                    fitted_on: List[str]) -> Dict[str, Any]:
    """One replayed result page in the recording format the compiler and the proof read.

    A browser rung that reads visible-only marks the page so: its items are the rows
    the page showed, and whatever reads the page's DOM again reads it the same way."""
    rung = record.get("browser") or {}
    template = rung.get("template") or {}
    selector = str((template.get("extract") or {}).get("items") or "")
    ev = res.get("evidence") or {}
    extracted = res.get("extracted") or {}
    final_url = str(ev.get("final_url") or res.get("url") or "")
    ui: Dict[str, Any] = {"text": str(ev.get("text") or ""),
                          "items": list(extracted.get("items") or [])}
    if extracted.get("labels"):
        ui["labels"] = list(extracted["labels"])
    out = {"workflow": str(record.get("workflow") or ""),
           "site": urllib.parse.urlsplit(final_url).netloc,
           "category": "replayed", "tag": "replay", "origin": "replay",
           "param": {"name": param, "value": value},
           "final_url": final_url, "ui": ui, "dom": str(ev.get("dom") or ""),
           "network": list(ev.get("network") or []),
           "ground_truth_selector": selector,
           "ground_truth_fitted_on": list(fitted_on)}
    if template.get("visible_only"):
        out["ground_truth_visible_only"] = True
    return out


def _floor(record: Dict[str, Any]) -> int:
    """The browser rung's count floor (``expect.min_items``). A page with fewer rows
    was served only under the result contract's rule for 1-2 item answers."""
    expect = ((record.get("browser") or {}).get("template") or {}).get("expect") or {}
    try:
        return max(1, int(expect.get("min_items") or 1))
    except (TypeError, ValueError):
        return 1


def _rows(res: Optional[Dict[str, Any]]) -> int:
    return len(((res or {}).get("extracted") or {}).get("items") or [])


def _items(res: Optional[Dict[str, Any]]) -> bool:
    return bool(res and res.get("success") and (res.get("extracted") or {}).get("items"))


def _traffic(res: Optional[Dict[str, Any]]) -> bool:
    return bool(((res or {}).get("evidence") or {}).get("network"))


def signed_in(record: Dict[str, Any]) -> bool:
    from anyuse.execution.api_step import load_command, signed_in_template
    return signed_in_template(load_command(record.get("template") or {}))


def live_evidence(record: Dict[str, Any], params: Dict[str, Any],
                  call_replay: Optional[Dict[str, Any]], replay: Optional[Replay]
                  ) -> Dict[str, Any]:
    """Compile + validation recordings from the browser replay, as ``recompile_now`` loads them.

    Compile input: a list page with traffic, one with at least the browser rung's floor of
    rows. That is this call's page when its input is not one of the proof's held-out inputs
    (a "no results" page compiles nothing), else the page of an input the browser rung was
    fitted on, replayed with capture. A smaller page (a correct 1-2 item answer) is the
    compile input only when no list page has traffic. Validation: every held-out proof input,
    so the claim stays the one the tool was listed with; a call whose input is held out is
    that input's proof case. Raises ``NoEvidence`` with the reason when the live path cannot
    be taken; ``spent`` is left to the caller, which sees the replays.
    """
    from anyuse.execution.api_recompile import NoEvidence
    rung = record.get("browser") or {}
    if not rung.get("template"):
        raise NoEvidence("this tool has no browser replay to capture fresh traffic with")
    if replay is None:
        raise NoEvidence("the browser replay did not answer this call")
    if signed_in(record):
        raise NoEvidence("a signed-in tool is recompiled from a new capture only")
    if (call_replay or {}).get("capture") == "unsupported":
        raise NoEvidence("the browser driver does not capture traffic")
    names = list((rung.get("template") or {}).get("params") or [])
    if len(names) != 1 or names[0] not in params:
        raise NoEvidence("the browser replay takes more than one input")
    param = names[0]
    value = str(params[param])
    held = proof_values(record)
    held_norm = {_norm(v) for v in held}
    fitted = [str(v) for v in (rung.get("fitted_on") or []) if _norm(v) not in held_norm]

    pages: Dict[str, Dict[str, Any]] = {}
    if _items(call_replay):
        pages[_norm(value)] = call_replay

    def read(v: str, capture: bool) -> Optional[Dict[str, Any]]:
        known = pages.get(_norm(v))
        if known is not None and (not capture or _traffic(known)):
            return known
        res = replay({param: v}, capture)
        if _items(res):
            pages[_norm(v)] = res
            return res
        if res.get("capture") == "unsupported":
            raise NoEvidence("the browser driver does not capture traffic")
        return None

    # The compile recording is a list page (at least the browser rung's floor of rows)
    # with traffic: this call's page, else a fitted input's. A smaller page with traffic
    # is the fallback, a fitted input's before this call's: a correct 1-2 item answer
    # compiles from fewer visible values than any list page.
    floor = _floor(record)
    usable = _norm(value) not in held_norm and _items(call_replay) and _traffic(call_replay)
    candidates = [value] if usable and _rows(call_replay) >= floor else []
    candidates += [v for v in fitted if _norm(v) not in {_norm(c) for c in candidates}]
    if not candidates and not usable:
        raise NoEvidence("this call's input is one of the proof's held-out inputs or found "
                         "nothing, and no other input of the tool is known to compile from")
    compile_value, base = "", None
    small: List[Any] = []
    for v in candidates:
        res = read(v, True)
        if _traffic(res) and _rows(res) >= floor:
            compile_value, base = v, res
            break
        if _traffic(res):
            small.append((v, res))
        elif res is not None:
            # the page answered without traffic to keep: the next input would not differ
            break
    if base is None and usable:
        small.append((value, call_replay))
    if base is None and small:
        compile_value, base = small[0]
    if base is None:
        raise NoEvidence(f"the browser replay of {candidates[0]!r} gave no page with traffic "
                         f"to compile from")
    # a chain needs a second input's traffic to fit on, and the proof traces each held-out
    # input's binding in that input's own traffic, so a chain's pages are all captured
    chain = bool((record.get("template") or {}).get("steps"))
    fit_values = [compile_value]
    selection: List[Dict[str, Any]] = []
    sel_value = next((v for v in fitted if _norm(v) != _norm(compile_value)), "")
    if sel_value:
        res = read(sel_value, chain)
        if res is not None:
            fit_values.append(sel_value)
            selection.append(_page_recording(record, param, sel_value, res, []))
    gt_fit = list(dict.fromkeys([*(str(v) for v in rung.get("fitted_on") or []), *fit_values]))
    fresh: List[Dict[str, Any]] = []
    for v in held:
        if _norm(v) in {_norm(x) for x in fit_values}:
            continue
        res = read(v, chain)
        if res is None:
            raise NoEvidence(f"the browser replay could not read the result page for {v!r}")
        if chain and not _traffic(res):
            # the proof traces the chain's result id in this input's own traffic; the
            # stored recordings may still have it
            raise NoEvidence(f"the browser replay of {v!r} captured no traffic to trace the "
                             f"chain's result id in")
        fresh.append(_page_recording(record, param, v, res, gt_fit))
    if len(fresh) < 2:
        raise NoEvidence(f"{len(fresh)} held-out proof input(s) to re-prove on, 2 required")
    recording = _page_recording(record, param, compile_value, base, gt_fit)
    for rec in selection:
        rec["ground_truth_fitted_on"] = gt_fit
    origins = list(dict.fromkeys(o for o in (_origin(recording["final_url"]),
                                             *(_origin(r.get("url", "")) for r in recording["network"]))
                                 if o))
    out = {"recording": recording, "selection": selection, "fresh": fresh,
           "allowed_origins": origins, "from": "the browser replay's traffic",
           # a swap from these pages keeps them as the tool's compile recordings
           "keep": True}
    if chain:
        def browse(v: str) -> Optional[Dict[str, Any]]:
            """A generated fitting input's page, replayed with capture (``chain_fit``)."""
            res = read(v, True)
            if not _traffic(res):
                return None
            return _page_recording(record, param, v, res, gt_fit)
        out["browse"] = browse
    return out


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def stored_evidence(record: Dict[str, Any], base_dir: str = "") -> Dict[str, Any]:
    """The recordings or attach sessions the tool was compiled from, as ``recompile_now`` loads
    them; a relative path is read from the tool's folder ``base_dir`` (``evidence_store``).
    Raises ``NoEvidence`` (nothing spent: it only reads files) when they were not kept,
    or when they are a signed-in capture (``anyuse attach --login-replay``)."""
    from anyuse.execution.api_recompile import NoEvidence
    if signed_in(record):
        raise NoEvidence("a signed-in tool is recompiled from a new capture only", spent=False)
    source = record.get("source") or {}
    paths = dict(source.get("paths") or {})
    if not paths and source.get("origin") == "attach" and source.get("recording"):
        from anyuse.execution.attach import sessions_dir
        base = sessions_dir()
        paths = {key: ([os.path.join(base, os.path.basename(str(p))) for p in source.get(key) or []]
                       if key != "recording" else os.path.join(base, os.path.basename(str(source[key]))))
                 for key in ("recording", "selection", "fresh") if source.get(key)}
    def where(path: Any) -> str:
        path = str(path or "")
        return os.path.join(str(base_dir), path) if path and base_dir and not os.path.isabs(path) \
            else path
    first = where(paths.get("recording"))
    if not first or not os.path.exists(first):
        raise NoEvidence("the recordings this tool was compiled from were not kept", spent=False)
    try:
        recording = _load_json(first)
        others = {key: [_load_json(where(p)) for p in paths.get(key) or []]
                  for key in ("fitting", "selection", "fresh")}
    except (OSError, ValueError) as exc:
        raise NoEvidence(f"a stored recording could not be read: {type(exc).__name__}",
                         spent=False) from exc
    if any(r.get("login_replay_site") for r in (recording, *others["selection"], *others["fresh"],
                                                *others["fitting"])):
        raise NoEvidence("the stored sessions are a signed-in capture, recompiled by hand only",
                         spent=False)
    from anyuse.execution.attach import SESSION_KIND
    if recording.get("kind") == SESSION_KIND:
        return {"sessions": [recording, *others["selection"], *others["fresh"]],
                "from": "the stored attach sessions"}
    return {"recording": recording, **others, "from": "the stored recordings"}


def recompile(storage, workflow_id: str, params: Dict[str, Any], *,
              call_replay: Optional[Dict[str, Any]] = None, replay: Optional[Replay] = None,
              executor=None, hosts_path: Optional[str] = None, why: str = "",
              policy=None, pinned_by: Sequence[str] = ()) -> Dict[str, Any]:
    """Try one paced automatic recompile; the summary a call result carries as ``recompiled``.

    ``pinned_by``: the registered skills whose steps pin this tool's current proof. A swap
    would stop them (``routine.CompiledToolRunner.fresh_check``), so such a tool, like a
    signed-in one, is not recompiled automatically and no attempt is counted.
    """
    from anyuse.execution.api_recompile import NoEvidence, RecompileManager

    manager = RecompileManager(storage, None, executor=executor, policy=policy,
                               hosts_path=hosts_path)
    record = manager.load_record(workflow_id) or {}
    refused = ""
    if signed_in(record):
        refused = ("a signed-in tool is recompiled from a new capture only: run `anyuse attach "
                   "--login-replay` again, then `anyuse compile last`")
    elif pinned_by:
        refused = (f"skill(s) {', '.join(repr(s) for s in pinned_by)} pin this tool's proof and "
                   f"would stop at their next run: recompile it yourself and pin the new proof "
                   f"in them")
    if refused:
        manager.note_refusal(workflow_id, why, refused)
        return {"attempted": False, "swapped": False, "reason": refused,
                "next_attempt_after": "never"}

    skipped: List[str] = []
    replays = [0]

    def counted(values: Dict[str, Any], capture: bool) -> Dict[str, Any]:
        replays[0] += 1
        return replay(values, capture)

    def loader(wid: str) -> Dict[str, Any]:
        current = manager.load_record(wid) or {}
        try:
            return live_evidence(current, params, call_replay,
                                 counted if replay is not None else None)
        except LookupError as exc:
            skipped.append(f"live: {exc}")
        from anyuse.execution import evidence_store as ES
        try:
            return ES.call_evidence(storage, wid, current)
        except LookupError as exc:
            skipped.append(f"calls: {exc}")
        try:
            return stored_evidence(current, base_dir=str(storage.get_workflow_dir(wid)))
        except LookupError as exc:
            skipped.append(f"stored: {exc}")
        raise NoEvidence("; ".join(skipped), spent=replays[0] > 0)

    manager.recording_loader = loader
    outcome = manager.recompile_now(workflow_id, why)
    stored = manager.load_record(workflow_id) or {}
    note = stored.get("recompile") or {}
    out = {"attempted": bool(outcome.attempted), "swapped": bool(outcome.persisted),
           "reason": outcome.reason}
    if outcome.attempted or (note.get("at") and note.get("counted", True)):
        out["from"] = str(note.get("from") or "")
        out["attempt"] = note.get("attempt")
    if not outcome.persisted:
        from anyuse.execution.api_recompile import ApiHealth, next_attempt_after
        out["next_attempt_after"] = next_attempt_after(ApiHealth.from_dict(stored.get("health")),
                                                       manager.policy)
    if skipped and outcome.attempted:
        out["skipped_evidence"] = skipped
    return out
