"""THE SAFE UPGRADE GATE. A drift census recompiled every
demonstration at a newer version and found it worse on 13 inputs where the saved tool was usable - and better
on others (16 known wrong answers of the saved tools: 6 became correct, 6 explicit failures). A newer compile is
therefore never swapped in blindly: it is a CANDIDATE revision, run in SHADOW on the tool's known inputs and checked
per input; it is accepted only when NO input gets worse, else the old revision stays and the decision is logged.

Per input, each side's outcome is one class (``outcome_class``):

* ``usable`` - an answer (``ok`` / ``no_results``) that is no known wrong and is verified: the same answer as the
  last known good one, or as the other side's usable answer, or the checker agrees (``verdict_store.StoreChecker``:
  a stored verdict on the same answer first, else the independent checker);
* ``known_wrong`` - an answer the verdict store knows to be wrong (``verdict_store.KNOWN_WRONG``);
* ``unverified`` - an answer nothing verifies (never reported as an answer);
* ``failure`` - an explicit failure (``failed`` / ``incomplete``: a CONFLICT, "the variant is not listed", ...);
* ``undecided`` - a block, an infrastructure error, a run that did not happen: the shadow run cannot judge it.

``compare(old, new)``: WORSE - usable -> anything else; failure / unverified -> known wrong. BETTER - known wrong ->
usable or failure (an honest failure beats a silent wrong); failure / unverified -> usable. Everything else is the
same. ``decide``: accepted iff every input is decided and none is worse.

``upgrade_boundary``: a candidate is a NEWER version of the same tool - the same workflow, site and bound parameters, no
host beyond the old origin policy. ``evaluate``: the shadow run of a candidate over the store's known inputs (the
inputs with a last known good answer, plus the current one), the decision, the log row (``upgrades.jsonl`` in the
tool's store directory) and, when accepted, the new revision (kind ``upgrade``; never on a pinned tool; not counted
against the cap on automatic repairs - it is a deliberate version upgrade, gated). A candidate the gate refused is
not tried again for the same active revision.
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_compile as C
from anyuse.execution import verdict_store as V

USABLE, KNOWN_WRONG, UNVERIFIED, FAILURE, UNDECIDED = "usable", "known_wrong", "unverified", "failure", "undecided"
#: a run's status after a rate limit and one retry (``repair_pace.RATE_LIMITED``; the paced ladder only)
RATE_LIMITED_STATUS = "rate_limited"
CLASSES = (USABLE, KNOWN_WRONG, UNVERIFIED, FAILURE, UNDECIDED)

WORSE, BETTER, SAME = "worse", "better", "same"

_WORSE = {(USABLE, KNOWN_WRONG), (USABLE, UNVERIFIED), (USABLE, FAILURE), (FAILURE, KNOWN_WRONG),
          (UNVERIFIED, KNOWN_WRONG)}
_BETTER = {(KNOWN_WRONG, USABLE), (KNOWN_WRONG, FAILURE), (FAILURE, USABLE), (UNVERIFIED, USABLE)}


def outcome_class(out: Optional[Dict[str, Any]], known_wrong: bool = False, verified: Optional[bool] = None) -> str:
    """One run's class (see the module doc). ``verified``: is its answer verified (None: not known - unverified)."""
    if not out:
        return UNDECIDED
    st = str(out.get("status") or "")
    if st == V.KNOWN_WRONG or known_wrong:
        return KNOWN_WRONG
    if st in ("ok", "no_results"):
        return USABLE if verified else UNVERIFIED
    if st in ("failed", "incomplete"):
        return FAILURE
    return UNDECIDED


def compare(old: str, new: str) -> str:
    if old == new:
        return SAME
    if (old, new) in _WORSE:
        return WORSE
    if (old, new) in _BETTER:
        return BETTER
    return SAME


def decide(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``rows``: one per input, each with ``old`` and ``new`` classes. Accepted iff every input is decided and none is
    worse."""
    worse = [r for r in rows if compare(r["old"], r["new"]) == WORSE]
    better = [r for r in rows if compare(r["old"], r["new"]) == BETTER]
    undecided = [r for r in rows if UNDECIDED in (r["old"], r["new"])]
    if not rows:
        return {"accepted": False, "why": "no known input to check the candidate on", "worse": 0, "better": 0}
    if undecided:
        return {"accepted": False, "why": f"{len(undecided)} input(s) could not be checked (a block, an error)",
                "worse": len(worse), "better": len(better), "undecided": len(undecided)}
    if worse:
        return {"accepted": False, "why": "the candidate is worse on " + ", ".join(
                    f"{str(r.get('value'))[:40]!r} ({r['old']} -> {r['new']})" for r in worse[:3]),
                "worse": len(worse), "better": len(better)}
    return {"accepted": True, "worse": 0, "better": len(better),
            "why": f"no input worse ({len(rows)} checked, {len(better)} better)"}


def upgrade_boundary(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    """A candidate is a NEWER version of the same tool: "" when it holds, else why."""
    try:
        vo, vn = int(old.get("version") or 0), int(new.get("version") or 0)
    except (TypeError, ValueError):
        return "no version"
    if vn <= vo:
        return f"the candidate is not newer (version {vn} vs {vo})"
    for k in ("workflow", "site"):
        if old.get(k) != new.get(k):
            return f"the candidate changes {k!r}"
    if [p.get("kind") for p in old.get("params") or []] != [p.get("kind") for p in new.get("params") or []]:
        return "the candidate changes its bound parameters"
    po, pn = C.site_hosts_policy(old), C.site_hosts_policy(new)
    site = str(old.get("site") or "")
    if not set(pn.get(site, [])) <= set(po.get(site, [])) or set(pn) - set(po):
        return "the candidate adds a host to the origin policy"
    return ""


async def _call(f: Callable, *a: Any) -> Any:
    r = f(*a)
    return await r if inspect.isawaitable(r) else r


async def classify(tool: Dict[str, Any], value: str, out: Optional[Dict[str, Any]], lkg: Optional[Dict[str, Any]],
                   verdicts: Optional[V.VerdictStore], checker: Optional[Callable],
                   other_usable: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    """The class of ``out`` (``tool`` for ``value``) and why: known wrong by the store; else an answer verified by the
    LKG, by the other side's usable answer or by the checker."""
    from anyuse.execution import repair_ladder as L
    if not out:
        return UNDECIDED, {"why": "not run"}
    kw = verdicts.known_wrong(tool, value, out) if verdicts is not None and out.get("status") in ("ok", "no_results") \
        else None
    if out.get("status") == V.KNOWN_WRONG or kw:
        return KNOWN_WRONG, {"why": "a known wrong answer"}
    if out.get("status") not in ("ok", "no_results"):
        return outcome_class(out), {"why": str(out.get("why") or "")[:160]}
    wf = str(tool.get("workflow") or "")
    if lkg and lkg.get("key") and L.answer_key(wf, out) == lkg["key"]:
        return USABLE, {"why": "the last known good answer"}
    if other_usable is not None and V.same_answer(out, other_usable):
        return USABLE, {"why": "the same answer as the other revision's usable one"}
    v = await _call(checker, tool, value, out) if checker else {"verdict": "undeterminable"}
    if (v or {}).get("rate_limited") is not None and (v or {}).get("verdict") != "agrees":
        # the paced checker only: a render that met a rate limit decides nothing
        return UNDECIDED, {"why": "the checker met a rate limit", "rate_limited": True,
                           "checker": {k: (v or {}).get(k) for k in ("verdict", "source", "why")}}
    ok = (v or {}).get("verdict") == "agrees"
    return (USABLE if ok else UNVERIFIED), {"why": f"the checker: {(v or {}).get('verdict')}",
                                            "checker": {k: (v or {}).get(k) for k in ("verdict", "source", "why")}}


Runner = Callable[[Dict[str, Any], str], Awaitable[Dict[str, Any]]]


async def shadow(run: Runner, old: Dict[str, Any], cand: Dict[str, Any], inputs: Sequence[str],
                 lkgs: Dict[str, Optional[Dict[str, Any]]], verdicts: Optional[V.VerdictStore], checker: Optional[Callable],
                 known: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """The candidate and the old revision run on every known input (``known``: the old revision's outcome already run
    for an input, e.g. the current run); one row per input: the classes, the comparison and both answers."""
    rows = []
    for value in inputs:
        o_old = (known or {}).get(value) or await run(old, value)
        o_new = await run(cand, value)
        lkg = lkgs.get(value)
        c_old, w_old = await classify(old, value, o_old, lkg, verdicts, checker)
        c_new, w_new = await classify(cand, value, o_new, lkg, verdicts, checker,
                                      other_usable=o_old if c_old == USABLE else None)
        row = {"value": value, "old": c_old, "new": c_new, "change": compare(c_old, c_new),
               "old_status": o_old.get("status"), "new_status": o_new.get("status"),
               "old_why": w_old.get("why"), "new_why": w_new.get("why"), "out_new": o_new}
        if w_old.get("rate_limited") or w_new.get("rate_limited") or \
                RATE_LIMITED_STATUS in (o_old.get("status"), o_new.get("status")):
            # pacing on: a side met a rate limit (its run, or the checker's render)
            row["rate_limited"] = True
        rows.append(row)
    return rows


async def evaluate(store: Any, cand: Dict[str, Any], run: Runner, verdicts: Optional[V.VerdictStore] = None,
                   checker: Optional[Callable] = None, current: Optional[Tuple[str, Dict[str, Any]]] = None
                   ) -> Dict[str, Any]:
    """The gate on ``cand`` for the store's ACTIVE revision (``store``: a ``repair_ladder.RepairStore``): the boundary,
    the shadow run over the known inputs (the inputs with a last known good answer and ``current``'s), the decision;
    logged; when accepted, a new revision (kind ``upgrade``) is active. ``{"accepted", "why", "rows", "rev"?}``."""
    old = store.active_tool()
    digest = C.tool_digest(cand)
    base = {"candidate": digest, "from_rev": store.active_rev(), "from_version": old.get("version"),
            "to_version": cand.get("version")}
    if store.pinned() is not None:
        return dict(base, accepted=False, why=f"the tool is pinned at revision {store.pinned()}", rows=[])
    b = upgrade_boundary(old, cand)
    if b:
        store.add_upgrade(dict(base, accepted=False, why=b))
        return dict(base, accepted=False, why=b, rows=[])
    if store.upgrade_decided(digest):
        return dict(base, accepted=False, why="this candidate was decided before for the active revision", rows=[],
                    decided_before=True)
    lkgs = store.lkgs()
    inputs = list(lkgs)
    known: Dict[str, Dict[str, Any]] = {}
    if current is not None:
        if current[0] not in inputs:
            inputs.append(current[0])
        known[current[0]] = current[1]
    rows = await shadow(run, old, cand, inputs, lkgs, verdicts, checker, known)
    d = decide(rows)
    rl = [r for r in rows if r.get("rate_limited")]
    if rl and not d["accepted"] and not d.get("worse"):
        # a rate limit is infrastructure, never a reason to refuse - the decision is DEFERRED (not
        # logged: the candidate is tried again on a later run)
        return dict(base, accepted=False, deferred=True, rows=rows,
                    why=f"deferred: {len(rl)} input(s) met a rate limit after one retry (infrastructure)")
    log = dict(base, accepted=d["accepted"], why=d["why"], rows=[{k: r[k] for k in ("value", "old", "new", "change",
                                                                                      "old_status", "new_status")}
                                                                  for r in rows])
    out = dict(log, rows=rows)
    if d["accepted"]:
        rev = store.add_revision(cand, 0, {"upgrade": {"from_version": old.get("version"),
                                                       "to_version": cand.get("version")}},
                                 {"gate": {k: log[k] for k in ("why", "rows")}}, 0.0, 0, kind="upgrade")
        wf = str(cand.get("workflow") or "")
        for r in rows:
            if r["new"] == USABLE:
                store.set_lkg(r["value"], wf, r["out_new"], rev)
            elif KNOWN_WRONG in (r["old"], r["new"]):
                # a known wrong answer is never a last known good one
                store.drop_lkg(r["value"])
        log["rev"] = out["rev"] = rev
    store.add_upgrade(log)
    return out
