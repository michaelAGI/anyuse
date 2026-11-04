"""The REPAIR LADDER for compiled web tools of version >= 10: a runtime layer around
``demo_tool.run_tool``, OFF by default (``LadderConfig.enabled = False``: ``run_with_ladder`` then IS ``run_tool``,
byte for byte). Step-local - only the step that broke is repaired, never the whole demonstration pipeline
(the old repair re-ran everything at 78-142 % of the first run's tokens).

The tiers, in order, each tried only when the one below found no accepted repair:

* **tier 0** - the run with the tool's runtime checks, as today ($0);
* **tier 1** - a deterministic re-locate of the failing step from the tool's recorded anchors (``repair_relocate``; no
  model);
* **tier 2** - a small-model patch (``repair_model``: Cerebras gpt-oss-120b gets the failing check, the tool's spec and a
  compact element table; its answer must be a patch of the closed language of ``repair_patch``);
* **tier 3** - a re-demonstration through the demonstrator loop (the goal plus the old tool as a hint), recompiled at
  the tool's own version (10 or 11; an injected ``demonstrator``; tested with fakes);
* **tier 4** - ``needs_human``, with the evidence. Never a guess.

**The acceptance gate** (``gate``; fixed code - no patch reaches it): every runtime check passes on the repaired run
(its status is ``ok``); an answer that differs from the LAST KNOWN GOOD answer for the same input (or with none known)
needs the INDEPENDENT CHECKER's agreement, else the outcome is ``changed_unverified`` and the old revision stays
active; a second input with a known good answer is replayed when one is available and must pass the same way.

**Revisions** (``RepairStore``): each accepted repair is a new revision (parent digest, the patch as its diff, the
evidence, the tier, the cost); the old ones are kept, ``rollback`` re-activates the parent. A PINNED revision is never
rewritten automatically: a repair it would need ends at ``needs_human``. **Budgets**: per tool per day <= 3 tier-2
calls and <= 1 tier-3 run; at most 2 ACCEPTED automatic rewrites per tool in a rolling 7 days (then ``needs_human``);
a cost ledger of every call. **Reliability state** per tool, from the ledger: ``healthy``, ``watch`` (a repair, a
``changed_unverified`` or a ``needs_human`` among the last N runs) or ``needs_human`` (the last run ended there).
**Per-run readout** (``out["ladder"]``): the tier reached, the model calls, the US$, the revision used and one status
line; an unverified repair is never reported as a success (the run keeps its tier-0 failure).
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import inspect
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_compile as C
from anyuse.execution import repair_patch as P
from anyuse.execution import repair_relocate as R
from anyuse.execution import web_arch as W
from anyuse.execution.repair_pace import PACING_BY_DEFAULT

#: tier 1 follows a candidate that fails at another step once more (two steps broke at once)
MAX_TIER1_DEPTH = 2

#: the ladder is OFF unless a caller turns it on (``LadderConfig(enabled=True)``)
ENABLED_BY_DEFAULT = False
#: tier 2 is OUT of the default ladder (behind ``LadderConfig(tier2=True)``) - on 120 seeded drift
#: cases it repaired 3 (design 1) and 2 (design 2: the anchors' context, ranked candidates, few-shot examples) of 120,
#: below the 15 % target
TIER2_BY_DEFAULT = False
#: tier 3 is OUT of the default ladder too (behind ``LadderConfig(tier3=True)``) - live it accepted 0 of 10
#: re-demonstrations (US$0.995); offline no general change (tool version 13, the content-check skip, pacing, the
#: results-page rule) makes any of the 10 acceptable: a case goes to ``needs_human`` instead, at US$0
TIER3_BY_DEFAULT = False
#: the default rule: a fix is default ON only if, in a live run with every fix on, it examined answers, let no false
#: accept through and withheld <= 2 % of the usable answers - tier 1's fallback search, pacing and the results-page
#: rule met it; probation, the checker's availability check, the block sentinel and the content-check skip did not
#: (they stay behind their switches)
FALLBACK_SEARCH_BY_DEFAULT = True
RESULTS_PAGE_BY_DEFAULT = True

OK, REPAIRED, UNVERIFIED, NEEDS_HUMAN = "ok", "repaired", "changed_unverified", "needs_human"
HEALTHY, WATCH = "healthy", "watch"
#: the run's answer was a KNOWN WRONG one (the verdict store) and no repair replaced it
NEEDS_REPAIR = "needs_repair"
#: a newer compile accepted by the safe upgrade gate (``repair_upgrade``)
UPGRADED = "upgraded"
#: with pacing on, the run met a rate limit after one retry - infrastructure, not an outcome of the tool
RATE_LIMITED = "rate_limited"
#: the canary's known-good input answered as before - the failure is this input's own (no repair)
INPUT_FAILURE = "input"
#: loop guard 1: the accepted repair was byte-identical to the active revision - no drift, nothing written
NO_DRIFT = "no_drift"


@dataclass
class LadderConfig:
    enabled: bool = ENABLED_BY_DEFAULT
    tier1: bool = True
    tier2: bool = TIER2_BY_DEFAULT
    tier3: bool = TIER3_BY_DEFAULT
    #: per tool per (UTC) day
    tier2_calls_per_day: int = 3
    tier3_runs_per_day: int = 1
    #: the cap on ACCEPTED automatic rewrites per tool over a rolling window
    max_rewrites: int = 2
    rewrite_window_days: float = 7.0
    #: the reliability state's window (the last N runs)
    watch_runs: int = 5
    #: the tier-1 candidates tried per run
    tier1_candidates: int = 2
    #: off by default: PROBATION - on a revision the ladder accepted (an upgrade, a repair), a
    #: tier-0 answer that is not the input's last known good answer is reported only when the checker agrees
    probation: bool = False
    #: off by default: the BLOCK sentinel - a list answer with a result whose every link on the
    #: result page sits in a sidebar / widget or a promotion / recommendation container is never accepted (tier 0, the
    #: upgrade gate's shadow runs, the repaired runs)
    block_sentinel: bool = False
    #: off by default: no re-demonstration for a CONTENT check failure (``content_failure``) -
    #: recompiled with the same checks it meets the same page (measured: 4 of 10 re-demonstrations, all rejected so)
    skip_tier3_on_content: bool = False
    #: ON by default: tier 1's search step also tries the tool's recorded fallback search URLs
    #: when the site shows no GET search form (``repair_relocate._fallback_search``)
    fallback_search: bool = FALLBACK_SEARCH_BY_DEFAULT
    #: ON by default: PACING - the ladder's own page openings (tier 0, the upgrade gate's shadow
    #: runs, tier 1's probes and candidate runs, the second value, tier 3's gated run) and its checker's renders keep a
    #: per-host minimum interval (``repair_pace``: 10 s or the robots.txt crawl-delay); a RATE-LIMIT page (an HTTP 429,
    #: "one search every N seconds", "too many requests") is infrastructure - waited for and retried once, never an
    #: answer, a failure, a block or a reason to refuse an upgrade (the gate defers instead)
    pacing: bool = PACING_BY_DEFAULT
    #: ON by default: a repaired structure reads result cards only on the SEARCH's own result page
    #: (``off_search_page``) - a search that redirected to a campaign page let tier 1 re-derive "result cards" from
    #: its content block (a false accept)
    results_page: bool = RESULTS_PAGE_BY_DEFAULT
    #: off by default: the AVAILABILITY sentinel - a tier-0 product answer whose own page (the one
    #: the run read; a local read, no request) states another availability attached to the product
    #: (``repair_check.availability_contradiction(v2=True)``) is never reported; the checker's availability check uses
    #: it too when ``OracleChecker(availability=2)``
    availability_sentinel: bool = False
    #: off by default: the BLOCK-AWARE list rule - the checker's "every answered result names a word
    #: of the input" is waived for a list answer none of whose results sits in a sidebar / widget / promotion block on
    #: the answer's own result page (``block_results``; the order, membership and coverage rules still hold). Measured
    #: live: probation withheld 8 correct list answers for that rule alone (a site search that matches the full text)
    list_rule2: bool = False
    #: off by default: before any repair tier, a tier-0 failure or a
    #: collapse flagged by the sentinel re-runs ONE other input's last known good answer (``repair_canary``) - it
    #: answers as before: the failure is the INPUT's (no repair, no revision; reported with the canary as evidence);
    #: it fails or collapsed: DRIFT (the ladder proceeds). Only for a tool whose every step is read-only (the HIL
    #: gate's classes, ``repair_canary.rerun_refusal``; fail closed); one canary per tool per ``canary_ttl_min``
    canary: bool = False
    canary_ttl_min: float = 30.0
    #: a failing input that has a POSITIVE
    #: last known good answer of its own answered before - its failure is evidence of drift on its own pages, which a
    #: canary on another input cannot exclude (every false INPUT of the measurement had one): no canary, the ladder
    #: proceeds. ``True`` runs the canary for such inputs too
    canary_own_lkg: bool = False
    #: off by default: a tool that may not be re-run gets the PROBE canary - its path walked up to the
    #: first step that is not read-only, never that step (``repair_canary.probe_path``); evidence only, never INPUT
    probe_canary: bool = False
    #: loop guard 1 (off by default): an accepted repair whose tool is byte-identical to the active revision
    #: is no drift - nothing is written (no revision, no last known good answer, no rewrite counted)
    identical_no_drift: bool = False
    #: loop guard 2 (off by default; ``api_rescan.HealGuard``'s rule): a failure within
    #: ``stale_after_repair_min`` of an accepted repair marks the tool STALE for ``stale_for_min`` - no new repair, the
    #: run ends at ``needs_human``
    stale_guard: bool = False
    stale_after_repair_min: float = 10.0
    stale_for_min: float = 30.0


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------

def answer_of(workflow: str, out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ANSWER of an accepted run, as compared across runs: a product's name / price / currency / availability /
    URL; a list's result URLs in order (a filtered list with the refinement applied; a paged list with its pages); a
    visit table's rows (URL, status, name, price, availability); ``no_results``. None for a run that answered nothing."""
    st = out.get("status")
    o = out.get("output") or {}
    if st == "no_results":
        return {"no_results": True}
    if st != "ok" or not isinstance(o, dict):
        return None
    if workflow == "product_lookup":
        return {k: o.get(k) for k in ("name", "price", "currency", "availability", "url")}
    if workflow == "visit_results":
        return {"items": [{k: r.get(k) for k in ("url", "status", "name", "price", "availability")}
                          for r in o.get("items") or []]}
    res = [C._norm(str(r.get("url") or "")) for r in o.get("results") or []]
    a: Dict[str, Any] = {"results": res}
    if workflow == "filtered_search":
        a["applied"] = o.get("applied")
    return a


def answer_key(workflow: str, out: Dict[str, Any]) -> Optional[str]:
    a = answer_of(workflow, out)
    return None if a is None else hashlib.sha256(json.dumps(a, sort_keys=True, ensure_ascii=False,
                                                            default=str).encode()).hexdigest()


def anchors_of(workflow: str, out: Dict[str, Any]) -> List[List[str]]:
    """The results an accepted run read, as re-locate anchors ``[url, title]`` (tier 1 finds them again)."""
    o = out.get("output") or {}
    if workflow == "product_lookup":
        pk = out.get("picked") or {}
        return [[str(pk.get("url")), str(pk.get("title") or o.get("name") or "")]] if pk.get("url") else []
    if workflow == "visit_results":
        return [[str(r.get("url")), str(r.get("title") or r.get("name") or "")] for r in o.get("items") or []
                if r.get("url")][:12]
    return [[str(r.get("url")), str(r.get("title") or "")] for r in o.get("results") or [] if r.get("url")][:12]


def n_items(out: Dict[str, Any]) -> Optional[int]:
    """The results the run's structure read on its (first) result page."""
    n = ((out.get("checks") or {}).get("result_page") or {}).get("n_items")
    return int(n) if isinstance(n, int) else None


def list_count(out: Dict[str, Any]) -> int:
    o = out.get("output") or {}
    return len(o.get("results") or o.get("items") or []) if isinstance(o, dict) else 0


def page_echo(out: Dict[str, Any], value: str) -> bool:
    """Does the run's result page name the input in its title or main heading (a search page echoes its query)?"""
    pages = out.get("pages") or []
    p = pages[0] if pages else {}
    return C.name_share(f"{p.get('title') or ''} {' '.join(str(h) for h in p.get('h1') or [])}", value) >= 0.5


def unread_anchors(out: Dict[str, Any]) -> List[List[str]]:
    """A run that could not read some result cards (a layout the demonstration did not show): the results it DID read
    and the ones it could not, as re-locate anchors after the LKG's (the re-derived structure must hold both)."""
    why = str(out.get("why") or "")
    if "could not be read" not in why and "result card" not in why:
        return []
    rp = (out.get("checks") or {}).get("result_page") or {}
    got = [[str(i.get("url")), str(i.get("title") or "")] for i in rp.get("items") or [] if i.get("url")]
    for u in rp.get("unread") or out.get("missing") or []:
        url = (u.get("url") or next(iter(u.get("urls") or []), None)) if isinstance(u, dict) else None
        if url:
            got.append([str(url), str(u.get("title") or "")])
    return got[:12]


def sentinel(tool: Dict[str, Any], value: str, out: Dict[str, Any], lkg: Optional[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """The ladder's SENTINEL on an ACCEPTED tier-0 answer (ladder on only; fixed code): the answer COLLAPSED against the
    last known good answer for the same input - no results where it had results; a result page that no longer names the
    input where the known good one did; a list shorter than half of it; a paged list that ended early at a "last page".
    ``(why, the steps to re-locate)``; ``("", [])`` when nothing is suspicious. A flagged answer is never accepted
    unverified: the ladder tries to repair it, else it is reported "changed, unverified"."""
    if not lkg or lkg.get("answer") is None or lkg["answer"].get("no_results"):
        return "", []
    wf = str(tool.get("workflow") or "")
    if out.get("status") == "no_results":
        return "no results where the last known good answer had results", ["region", "search"]
    if out.get("status") != "ok":
        return "", []
    if lkg.get("echo") and not page_echo(out, value):
        return "the result page no longer names the input (the last known good one did)", ["search", "region"]
    if wf in ("site_search", "filtered_search", "paged_search", "visit_results"):
        n0, n = int(lkg.get("count") or 0), list_count(out)
        if n0 >= 2 and n * 2 < n0:
            return f"the list collapsed ({n} of the last known good {n0})", ["region", "search"]
        end = str(((out.get("checks") or {}).get("paged") or {}).get("end") or "")
        if wf == "paged_search" and n < n0 and end.startswith("last page"):
            return f"the pages ended early ({n} of the last known good {n0}; {end})", ["pager", "region"]
    return "", []


# ---------------------------------------------------------------------------
# the store: revisions, last known good answers, the run and cost ledgers, the state
# ---------------------------------------------------------------------------

class RepairStore:
    """One directory per tool (``<root>/<tool id>``): ``revisions.json`` (every revision kept; the active one; the
    pin), ``lkg.json`` (the last known good answer per input), ``ledger.jsonl`` (one row per run), ``costs.jsonl`` (one
    row per model call / re-demonstration), ``state.json`` (the reliability state). The tool id is the digest of the
    tool the store was opened with first (revision 0)."""

    def __init__(self, root: str, tool: Dict[str, Any], clock: Callable[[], _dt.datetime] = utcnow):
        self.clock = clock
        self.tool_id = C.tool_digest(tool)[:16]
        self.dir = os.path.join(root, self.tool_id)
        os.makedirs(self.dir, exist_ok=True)
        if not os.path.exists(self._p("revisions.json")):
            self._save("revisions.json", {"tool_id": self.tool_id, "active": 0, "pinned": None, "revisions": [
                {"rev": 0, "digest": C.tool_digest(tool), "parent": None, "tier": 0, "diff": None, "evidence": None,
                 "cost_usd": 0.0, "calls": 0, "at": _iso(self.clock()), "tool": copy.deepcopy(tool)}]})

    # -- files ---------------------------------------------------------------
    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _load(self, name: str, default: Any) -> Any:
        try:
            with open(self._p(name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def _save(self, name: str, data: Any) -> None:
        tmp = self._p(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False, default=str)
        os.replace(tmp, self._p(name))

    def _append(self, name: str, row: Dict[str, Any]) -> None:
        with open(self._p(name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _rows(self, name: str) -> List[Dict[str, Any]]:
        try:
            with open(self._p(name), encoding="utf-8") as fh:
                return [json.loads(x) for x in fh if x.strip()]
        except (OSError, ValueError):
            return []

    # -- revisions ------------------------------------------------------------
    def revisions(self) -> List[Dict[str, Any]]:
        return self._load("revisions.json", {}).get("revisions") or []

    def active_rev(self) -> int:
        return int(self._load("revisions.json", {}).get("active") or 0)

    def active_tool(self) -> Dict[str, Any]:
        r = self.active_rev()
        return copy.deepcopy(next(x for x in self.revisions() if x["rev"] == r)["tool"])

    def pinned(self) -> Optional[int]:
        return self._load("revisions.json", {}).get("pinned")

    def pin(self, rev: Optional[int] = None) -> None:
        """Pin a revision (the active one by default) and make it active: never rewritten automatically."""
        d = self._load("revisions.json", {})
        rev = d["active"] if rev is None else int(rev)
        if not any(x["rev"] == rev for x in d["revisions"]):
            raise ValueError(f"no revision {rev}")
        d["pinned"], d["active"] = rev, rev
        self._save("revisions.json", d)

    def unpin(self) -> None:
        d = self._load("revisions.json", {})
        d["pinned"] = None
        self._save("revisions.json", d)

    def add_revision(self, tool: Dict[str, Any], tier: int, diff: Any, evidence: Dict[str, Any], cost_usd: float,
                     calls: int, kind: Optional[str] = None) -> int:
        d = self._load("revisions.json", {})
        parent = next(x for x in d["revisions"] if x["rev"] == d["active"])
        rev = max(x["rev"] for x in d["revisions"]) + 1
        row = {"rev": rev, "digest": C.tool_digest(tool), "parent": parent["digest"],
               "parent_rev": parent["rev"], "tier": tier, "diff": diff, "evidence": evidence,
               "cost_usd": round(float(cost_usd), 6), "calls": int(calls), "at": _iso(self.clock()),
               "tool": copy.deepcopy(tool)}
        if kind:
            # "upgrade" - a newer compile the safe upgrade gate accepted (not an automatic repair)
            row["kind"] = kind
        d["revisions"].append(row)
        d["active"] = rev
        self._save("revisions.json", d)
        return rev

    def rollback(self, to: Optional[int] = None) -> int:
        """Re-activate the active revision's parent (or revision ``to``); every revision is kept. A pinned tool is
        rolled back only to its pin."""
        d = self._load("revisions.json", {})
        cur = next(x for x in d["revisions"] if x["rev"] == d["active"])
        target = int(to) if to is not None else cur.get("parent_rev")
        if target is None or not any(x["rev"] == target for x in d["revisions"]):
            raise ValueError("no revision to roll back to")
        if d.get("pinned") is not None and target != d["pinned"]:
            raise ValueError("the tool is pinned")
        d["active"] = target
        self._save("revisions.json", d)
        self._append("ledger.jsonl", {"at": _iso(self.clock()), "event": "rollback", "from": cur["rev"], "to": target})
        return target

    # -- last known good ------------------------------------------------------
    def lkg(self, value: str) -> Optional[Dict[str, Any]]:
        return self._load("lkg.json", {}).get(str(value))

    def set_lkg(self, value: str, workflow: str, out: Dict[str, Any], rev: int) -> None:
        d = self._load("lkg.json", {})
        d[str(value)] = {"key": answer_key(workflow, out), "answer": answer_of(workflow, out),
                         "anchors": anchors_of(workflow, out), "n_items": n_items(out), "count": list_count(out),
                         "echo": page_echo(out, value), "rev": rev, "at": _iso(self.clock())}
        self._save("lkg.json", d)

    def lkgs(self) -> Dict[str, Dict[str, Any]]:
        """Every input with a last known good answer (the upgrade gate's known inputs)."""
        return {v: x for v, x in self._load("lkg.json", {}).items() if x.get("answer") is not None}

    def drop_lkg(self, value: str) -> None:
        """Forget the last known good answer of ``value`` (a known wrong answer is never one)."""
        d = self._load("lkg.json", {})
        if str(value) in d:
            d.pop(str(value))
            self._save("lkg.json", d)

    def add_upgrade(self, row: Dict[str, Any]) -> None:
        """Log one decision of the safe upgrade gate (``upgrades.jsonl``)."""
        self._append("upgrades.jsonl", dict(row, at=_iso(self.clock()), active_rev=self.active_rev()))

    def upgrades(self) -> List[Dict[str, Any]]:
        return self._rows("upgrades.jsonl")

    def upgrade_decided(self, digest: str) -> bool:
        """Was the candidate ``digest`` decided (accepted or refused) for the ACTIVE revision?"""
        r = self.active_rev()
        return any(u.get("candidate") == digest and u.get("from_rev") == r for u in self.upgrades())

    def other_value(self, value: str) -> Optional[str]:
        """The most recent OTHER input with a known good answer (the gate's second value)."""
        d = self._load("lkg.json", {})
        rows = sorted(((v, x) for v, x in d.items() if v != str(value) and x.get("answer") is not None),
                      key=lambda vx: str(vx[1].get("at")), reverse=True)
        return rows[0][0] if rows else None

    # -- the canary and the loop guard ------------------------------------------------
    def canaries(self) -> List[Dict[str, Any]]:
        """Every canary verdict (``canary.jsonl``): its input, the failing input, the revision, the verdict, why."""
        return self._rows("canary.jsonl")

    def add_canary(self, row: Dict[str, Any]) -> None:
        self._append("canary.jsonl", dict(row, at=_iso(self.clock())))

    def loop_guard(self, recent_min: float, stale_min: float):
        """The tool's loop guard (``api_rescan.HealGuard``, its state in ``loop_guard.json``; the store's clock)."""
        from anyuse.execution.api_rescan import HealGuard
        d = self._load("loop_guard.json", {})
        g = HealGuard.from_dict(d, clock=lambda: self.clock().timestamp())
        g.recent_s, g.stale_s = float(recent_min) * 60.0, float(stale_min) * 60.0
        return g

    def save_loop_guard(self, g: Any) -> None:
        self._save("loop_guard.json", g.to_dict())

    # -- ledgers ----------------------------------------------------------------
    def ledger(self) -> List[Dict[str, Any]]:
        return [r for r in self._rows("ledger.jsonl") if "event" not in r]

    def costs(self) -> List[Dict[str, Any]]:
        return self._rows("costs.jsonl")

    def add_cost(self, tier: int, calls: int, usd: float, detail: Optional[Dict[str, Any]] = None) -> None:
        self._append("costs.jsonl", dict({"at": _iso(self.clock()), "tier": tier, "calls": int(calls),
                                          "usd": round(float(usd), 6)}, **(detail or {})))

    def used_today(self, tier: int) -> int:
        day = self.clock().astimezone(_dt.timezone.utc).date()
        return sum(int(r.get("calls") or 0) for r in self.costs() if r.get("tier") == tier and
                   (_parse(r.get("at")) or self.clock()).date() == day)

    def rewrites_within(self, days: float) -> int:
        cut = self.clock() - _dt.timedelta(days=days)
        return sum(1 for x in self.revisions() if x["rev"] > 0 and x.get("tier", 0) > 0 and
                   (_parse(x.get("at")) or self.clock()) > cut)

    def record_run(self, row: Dict[str, Any], watch_runs: int) -> Dict[str, Any]:
        self._append("ledger.jsonl", row)
        st = self.state(watch_runs)
        self._save("state.json", st)
        return st

    # -- the reliability state --------------------------------------------------
    def state(self, watch_runs: int = 5) -> Dict[str, Any]:
        """``needs_repair`` when the last run answered a KNOWN WRONG answer no repair replaced;
        ``needs_human`` when the last run ended there; ``watch`` when one of the last ``watch_runs`` runs was repaired,
        upgraded, changed but unverified, known wrong or needed a human; else ``healthy``."""
        rows = self.ledger()[-max(1, int(watch_runs)):]
        if not rows:
            return {"state": HEALTHY, "why": "no run yet", "at": _iso(self.clock())}
        last = rows[-1]
        if last.get("known_wrong") and last.get("outcome") not in (REPAIRED, UPGRADED):
            # the last run answered a KNOWN WRONG answer and no repair replaced it
            return {"state": NEEDS_REPAIR, "why": str(last.get("why") or "")[:200], "at": _iso(self.clock())}
        if last.get("outcome") == NEEDS_HUMAN:
            return {"state": NEEDS_HUMAN, "why": str(last.get("why") or "")[:200], "at": _iso(self.clock())}
        hit = [r for r in rows if r.get("outcome") in (REPAIRED, UNVERIFIED, NEEDS_HUMAN, UPGRADED) or r.get("known_wrong")]
        if hit:
            return {"state": WATCH, "why": f"{len(hit)} of the last {len(rows)} run(s): " +
                    ", ".join(sorted({str(r['outcome']) for r in hit})), "at": _iso(self.clock())}
        return {"state": HEALTHY, "why": f"the last {len(rows)} run(s) needed no repair", "at": _iso(self.clock())}


# ---------------------------------------------------------------------------
# the gate (fixed code)
# ---------------------------------------------------------------------------

Checker = Callable[[Dict[str, Any], str, Dict[str, Any]], Any]


async def _call(f: Callable, *a: Any) -> Any:
    r = f(*a)
    return await r if inspect.isawaitable(r) else r


async def gate(tool: Dict[str, Any], value: str, out: Dict[str, Any], lkg: Optional[Dict[str, Any]],
               checker: Optional[Checker], second: Optional[Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]] = None,
               verdicts: Any = None, require_checker: bool = False) -> Dict[str, Any]:
    """The acceptance gate of a repaired run (``out`` of ``tool`` for ``value``): ``{"verdict": "accepted" |
    "changed_unverified" | "rejected", "why", ...}``. ``lkg``: the last known good answer for ``value``; ``checker``: the
    independent checker (``(tool, value, out) -> {"verdict": "agrees" | "disagrees" | "undeterminable"}``);
    ``second``: ``(value2, out2, lkg2)``, the repaired tool's run on another input with a known good answer.
    ``verdicts`` (a ``verdict_store.VerdictStore``): a repaired answer that is a KNOWN WRONG one is rejected;
    ``require_checker`` (the run being repaired answered a known wrong answer): the checker must agree, an answer equal
    to the last known good one is not enough."""
    wf = str(tool.get("workflow") or "")
    g: Dict[str, Any] = {"status": out.get("status"), "why_run": str(out.get("why") or "")[:200]}
    if out.get("status") != "ok":
        return dict(g, verdict="rejected", why=f"the runtime checks did not pass ({out.get('status')}: "
                                               f"{str(out.get('why') or '')[:120]})")
    if verdicts is not None and verdicts.known_wrong(tool, value, out):
        return dict(g, verdict="rejected", why="the repaired answer is a known wrong one (the verdict store)")
    collapsed, csteps = sentinel(tool, value, out, lkg)
    if collapsed:
        return dict(g, verdict="rejected", collapse_steps=csteps,
                    why=f"the repaired answer collapsed against the last known good one ({collapsed})")
    n0, n = (lkg or {}).get("n_items"), n_items(out)
    if isinstance(n0, int) and n0 >= 2 and n is not None and n * 2 < n0:
        return dict(g, verdict="rejected", why=f"the repaired structure reads {n} result(s) where the last known good run "
                                               f"read {n0}")
    key = answer_key(wf, out)
    if lkg and lkg.get("key") and key == lkg["key"] and not require_checker:
        g["same_as_lkg"] = True
    else:
        g["same_as_lkg"] = False
        v = await _call(checker, tool, value, out) if checker else {"verdict": "undeterminable",
                                                                     "why": "no independent checker"}
        g["checker"] = v
        if (v or {}).get("verdict") != "agrees":
            return dict(g, verdict=UNVERIFIED, why=("the answer differs from the last known good answer" if lkg else
                                                     "no known good answer for this input") +
                        f" and the independent checker does not agree ({(v or {}).get('verdict')})")
    if second is not None:
        v2, out2, lkg2 = second
        g["second"] = {"value": v2, "status": out2.get("status")}
        if out2.get("status") not in ("ok", "no_results"):
            return dict(g, verdict="rejected", why=f"the second value {v2!r} fails ({out2.get('status')}: "
                                                   f"{str(out2.get('why') or '')[:100]})")
        if verdicts is not None and verdicts.known_wrong(tool, v2, out2):
            return dict(g, verdict="rejected", why=f"the second value {v2!r} answers a known wrong answer")
        if not (lkg2 and lkg2.get("key") == answer_key(wf, out2)):
            w = await _call(checker, tool, v2, out2) if checker else {"verdict": "undeterminable"}
            g["second"]["checker"] = w
            if (w or {}).get("verdict") != "agrees":
                return dict(g, verdict=UNVERIFIED, why=f"the second value {v2!r}'s answer changed and the checker "
                                                       f"does not agree")
        else:
            g["second"]["same_as_lkg"] = True
    else:
        g["second"] = None
    return dict(g, verdict="accepted", why="the runtime checks pass" +
                ("; the same answer as the last known good" if g["same_as_lkg"] else "; the checker agrees") +
                ("; the second value passes" if second is not None else "; no second value available"))


# ---------------------------------------------------------------------------
# tier 3: the boundary of a recompiled tool
# ---------------------------------------------------------------------------

def tier3_boundary(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    """A re-demonstrated tool may change its anchors, never what it is: the same workflow, site, version (the old
    tool's own, >= 10), checks, counts and caps, no host beyond the old origin policy's. "" when it holds, else why."""
    if not C.v10(new):
        return "the recompiled tool is not version 10 or newer"
    for k in ("workflow", "site", "version", "checks", "n", "k", "top_k", "max_pages"):
        if old.get(k) != new.get(k):
            return f"the recompiled tool changes {k!r}"
    if [{x: p.get(x) for x in ("kind", "default", "key")} for p in old.get("params") or []] != \
            [{x: p.get(x) for x in ("kind", "default", "key")} for p in new.get("params") or []]:
        return "the recompiled tool changes its bound parameters"
    po, pn = C.site_hosts_policy(old), C.site_hosts_policy(new)
    site = str(old.get("site") or "")
    if not set(pn.get(site, [])) <= set(po.get(site, [])) or set(pn) - set(po):
        return "the recompiled tool adds a host to the origin policy"
    return ""


def structure_guard(tool: Dict[str, Any], cand: Dict[str, Any], probe: Optional[Dict[str, Any]],
                    anchors: Sequence[Sequence[str]] = ()) -> str:
    """A patch that REPLACES the result cards, checked on the probe page's DOM before it runs (fixed code, every tier):
    no card the new structure reads as a result (a link of the region's URL pattern outside the page chrome) sits in a
    sidebar / widget (``demo_cards.widget_why``, version 10's names), and no block of its cards precedes the anchored
    results (``repair_relocate.foreign_before``). "" when it holds (or no DOM to check: the gate decides)."""
    import re
    from anyuse.execution import demo_cards as K
    new = (cand.get("region") or {}).get("cards") or []
    if not new or new == (tool.get("region") or {}).get("cards") or not (probe or {}).get("dom"):
        return ""
    rx = (cand.get("region") or {}).get("regex") or ""
    try:
        pat = re.compile(rx, re.I) if rx else None
    except re.error:
        pat = None
    bem = C.cards_v11(cand.get("region") or {})
    with K.bem_free(bem):
        # a version-11 region's cards are described without BEM modifiers (``demo_cards.bem_free``)
        items = K.structure_items(str(probe.get("dom") or ""), str(probe.get("url") or ""), new, v10=True)
    read = [it for it in items if it.get("area") not in ("header", "nav", "footer") and it.get("url") and
            (pat is None or pat.match(C.strip_fragment(str(it["url"]))))]
    w = next((it for it in read if it.get("widget")), None)
    if w is not None:
        return f"the new structure reads a sidebar / widget card ({str(w['widget'])[:100]})"
    present = [str(a[0]) for a in anchors if a and any(C._norm(str(a[0])) == C._norm(str(it.get("url"))) for it in read)]
    if present and R.foreign_before(probe, new, present, rx, bem=bem):
        return "the new structure reads a block of cards before the anchored results"
    return ""


def _search_path_rx(template: str) -> Optional["re.Pattern[str]"]:
    import urllib.parse
    path = urllib.parse.unquote(urllib.parse.urlsplit(template).path or "/").rstrip("/") or "/"
    rx = "^" + re.escape(path).replace(re.escape("{q}"), ".+") + "/?$"
    try:
        return re.compile(rx)
    except re.error:
        return None


def off_search_page(tool: Dict[str, Any], url: Any, out: Optional[Dict[str, Any]] = None) -> str:
    """``""`` when the page at ``url`` is the tool's SEARCH result page - its host and path are one of the
    tool's search URLs' (the URL template, the recorded fallback templates; ``{q}`` matches the input, also across
    path segments) - or the answered product's own page (a search that landed on the product), or when the tool has no
    search URL to compare (a typed search without a recorded one: never refused); else why not. A search that
    redirected to a campaign / landing / content page has no result list: cards read there are content blocks.
    Offline: 0 of 826 saved usable answers and 0 of 204 version-12 answers are on such a page; a measured tier-1 false
    accept is."""
    import urllib.parse
    s = tool.get("search") or {}
    ts = ([str(s["template"])] if s.get("mode") == "url" and s.get("template") else []) + \
        [str(t) for t in s.get("fallback_templates") or [] if "{q}" in str(t)]
    u = str(url or "")
    if not ts or not u:
        return ""
    o = (out or {}).get("output") or {}
    if isinstance(o, dict) and o.get("url") and C._norm(str(o["url"])) == C._norm(u):
        return ""
    sp = urllib.parse.urlsplit(u)
    host = (sp.hostname or "").lower()
    path = urllib.parse.unquote(sp.path or "/")
    for t in ts:
        th = (urllib.parse.urlsplit(t).hostname or "").lower()
        rx = _search_path_rx(t)
        if th.removeprefix("www.") == host.removeprefix("www.") and rx is not None and \
                (rx.match(path.rstrip("/") or "/") or rx.match(path)):
            return ""
    return f"the page {sp.path[:80]!r} is not the search's own result page (a page the search redirected to)"


def results_page_refusal(tool: Dict[str, Any], cand: Dict[str, Any], probe: Optional[Dict[str, Any]]) -> str:
    """The results-page check BEFORE a repaired structure runs: "" or why a patch that replaces the result cards is
    refused because the probe page is no result list (``off_search_page``). Only when the patch leaves the SEARCH as the
    probe ran it: a patch that also re-binds the search reads another page than the probe's - its run's own result
    page decides (the check after the run). Measured live: 2 values on two sites where the old search's page
    refused a patch that moved the search."""
    if (cand.get("region") or {}).get("cards") == (tool.get("region") or {}).get("cards"):
        return ""
    if (cand.get("search") or {}) != (tool.get("search") or {}):
        return ""
    off = off_search_page(cand, (probe or {}).get("url"))
    return f"the new structure's page is no result list: {off}" if off else ""


#: a tier-0 failure that is a CONTENT check of the page the run reached - its structured and
#: visible data disagree (a CONFLICT), the opened product is not the input's or the asked-for variant is not listed
#: (identity), results equally close (ambiguous)
_CONTENT_CHECK = re.compile(r"^(?:incomplete:\s*)?(conflict|identity|ambiguous)\b")


def content_failure(out: Dict[str, Any]) -> str:
    """The content check a failed / incomplete run failed (``_CONTENT_CHECK``), "" for any other failure."""
    if out.get("status") not in ("failed", "incomplete"):
        return ""
    m = _CONTENT_CHECK.match(str(out.get("why") or "").strip().lower())
    return m.group(1) if m else ""


def block_why(n: Any) -> str:
    """Why a link node sits in a BLOCK beside the results - a sidebar / widget (``demo_cards.
    widget_why``'s version-8 reasons: the page's aside / nav / header / footer, a widget container or label), a sidebar
    container (``sidebar_ancestor``) that is not a card's own article / list item (a post class such as "format-aside"),
    or a promotion / advertising / recommendation container (version 12's words, ``PROMO_WORDS12``); "" when not."""
    from anyuse.execution import demo_cards as K
    w = K.widget_why(n, v10=False)
    if w:
        return w
    sa = K.sidebar_ancestor(n)
    if sa is not None and sa.tag not in ("article", "li"):
        return f"inside a sidebar container ({K.desc(sa)})"
    x = n.parent
    while x is not None and not x.tag.startswith("#") and x.tag not in ("body", "html"):
        if any(t in K.PROMO_WORDS12 for t in K.promo_tokens12(x)):
            return f"inside a promotion / recommendation container ({K.desc(x)})"
        x = x.parent
    return ""


def block_results(dom: str, url: str, results: Sequence[str]) -> List[List[str]]:
    """The answered result URLs whose EVERY link on the page sits in a block (``block_why``), as ``[url, why]``; a URL
    the page does not link is not one (silent wrongs: a "NEUE REPORTS" aside, a ``#sidebar-a`` - new articles in a
    sidebar a saved tool reads as its results)."""
    from anyuse.execution import demo_cards as K
    if not dom:
        return []
    root, base = K.parse(dom)
    occ: Dict[str, List[Any]] = {}
    for a in K.walk(root):
        if a.tag == "a" and "href" in a.attrs:
            occ.setdefault(C._norm(C.strip_fragment(K.href_of(a, url, base))), []).append(a)
    out: List[List[str]] = []
    for r in results:
        nodes = occ.get(C._norm(C.strip_fragment(str(r)))) or []
        whys = [block_why(n) for n in nodes]
        if nodes and all(whys):
            out.append([str(r), whys[0]])
    return out


def promo_results(tool: Dict[str, Any], dom: str, url: str, results: Sequence[str]) -> List[str]:
    """The ``results`` (URLs) whose every card on the page (``dom``; the tool's own card structure, described the
    tool's way) sits in a promotion / advertising / recommendation container that does not hold every card
    (``demo_cards.promo_flags12``, version 12's rule)."""
    from anyuse.execution import demo_cards as K
    region = tool.get("region") or {}
    specs = region.get("cards") or []
    if not specs or not dom:
        return []
    with K.bem_free(C.cards_v11(region)):
        root, base = K.parse(dom)
        nodes = K._card_nodes12(root, specs)
        bases = [K._tile_of(n) if n.tag == "a" else n for n in nodes]
        flags = K.promo_flags12(bases, [K.area_of(n) in K.CHROME_AREAS for n in nodes])
    promo: Dict[str, bool] = {}
    for n, f in zip(nodes, flags):
        links = [n] if n.tag == "a" else [a for a in K.walk(n) if a.tag == "a"]
        for a in links:
            if "href" in a.attrs:
                u = C._norm(C.strip_fragment(K.href_of(a, url, base)))
                promo[u] = promo.get(u, True) and bool(f)
    return [r for r in results if promo.get(C._norm(C.strip_fragment(str(r))))]


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------

PageFactory = Callable[[], Any]


def readout(row: Dict[str, Any]) -> str:
    """The run's one status line: the tier reached, the outcome, the model calls, the US$, the revision used."""
    o = row.get("outcome")
    word = {OK: "ok", REPAIRED: "REPAIRED", UNVERIFIED: "CHANGED, UNVERIFIED - not an answer; the old revision stays",
            NEEDS_HUMAN: "NEEDS HUMAN", UPGRADED: "UPGRADED - a newer compile the upgrade gate accepted",
            RATE_LIMITED: "RATE LIMITED - infrastructure; nothing reported, nothing refused",
            INPUT_FAILURE: "INPUT - a known input answers as before (canary); this input's failure is reported, no "
                           "repair",
            NO_DRIFT: "NO DRIFT - the repair is byte-identical to the active revision; nothing written"}.get(o, str(o))
    if row.get("stale"):
        word += " (STALE: no new repair)"
    if row.get("known_wrong") and o not in (REPAIRED, UPGRADED):
        word += " (KNOWN WRONG answer withheld)"
    rev = f"rev {row.get('revision')}" + (f" (was {row['revision_before']})" if row.get("revision_before") not in
                                         (None, row.get("revision")) else "")
    return (f"tier {row.get('tier')}: {word}; status {row.get('status')}; {row.get('model_calls', 0)} model call(s), "
            f"US${float(row.get('usd') or 0.0):.4f}; {rev}")


async def _page(factory: PageFactory) -> Any:
    p = factory()
    return await p if inspect.isawaitable(p) else p


async def run_with_ladder(page_factory: PageFactory, tool: Dict[str, Any], value: str, settle: Callable, *,
                          config: Optional[LadderConfig] = None, store: Optional[RepairStore] = None,
                          checker: Optional[Checker] = None, model: Any = None, demonstrator: Any = None,
                          params: Optional[Dict[str, Any]] = None, result_wait_s: Optional[float] = None,
                          verdicts: Any = None, upgrade: Any = None) -> Dict[str, Any]:
    """One invocation of ``tool`` for ``value`` with the ladder. OFF (no ``config`` or ``config.enabled`` False, or a
    tool below version 10): exactly ``demo_tool.run_tool`` on one page of ``page_factory`` (the same call, the same
    result). ON: the store's ACTIVE revision runs (tier 0); a failure climbs the tiers (see the module doc); the result
    carries ``out["ladder"]`` (the readout and the evidence).

    ``verdicts`` (a ``verdict_store.VerdictStore``; opt-in, ladder on or off): a run whose answer matches a
    known WRONG verdict is ``known_wrong`` - never ok; with the ladder on, the tool's state becomes ``needs_repair``,
    the ladder tries to repair it and the gate requires the checker (the store first, then the independent checker)
    to agree on the new answer. ``upgrade`` (ladder on): ``tool -> candidate`` (a newer compile of the tool's
    demonstration, or None); a known wrong or a failure first tries the candidate through the safe upgrade gate
    (``repair_upgrade.evaluate``). A tool below version 10 with a store and ``verdicts`` / ``upgrade``: the flag, the
    store's state and the upgrade gate only (the repair tiers need version 10); without them: exactly ``run_tool``."""
    from anyuse.execution import demo_tool as T
    kw = {} if result_wait_s is None else {"result_wait_s": result_wait_s}
    legacy = store is not None and (verdicts is not None or upgrade is not None)
    if config is None or not config.enabled or (not C.v10(tool) and not legacy):
        out = await T.run_tool(await _page(page_factory), tool, value, settle, params=params, **kw)
        if verdicts is not None:
            from anyuse.execution import verdict_store as VS
            out = VS.check(verdicts, tool, value, out)
        return out
    if store is None:
        raise ValueError("the ladder needs a RepairStore (revisions, last known good answers, ledgers)")
    lad = _Ladder(page_factory, settle, config, store, checker, model, demonstrator, params, kw)
    lad.verdicts, lad.upgrade = verdicts, upgrade
    if config.list_rule2 and checker is not None:
        # the block-aware list rule reads the answer's own page (innermost: its verdict is the one the
        # store records)
        checker = _BlockAwareChecker(checker, lad)
        lad.checker = checker
    if config.pacing and checker is not None:
        # the checker's renders are paced and a rate-limited render is retried once (a stored
        # verdict answers without a render: the store is asked first)
        checker = _PacedChecker(checker)
        lad.checker = checker
    if verdicts is not None:
        from anyuse.execution import verdict_store as VS
        lad.checker = VS.StoreChecker(verdicts, checker)
    # the ladder's own checks read words the way the tool does: every script from version 14
    with W.scripts(C.v14(tool)):
        return await lad.run(value)


class _BlockAwareChecker:
    """The independent checker with the BLOCK-AWARE list rule (``LadderConfig(list_rule2=True)``) - a
    disagreement only because answered results name no word of the input (the rest of the list rule holding:
    ``coverage_ok``) is an agreement when none of the answered results sits in a block on the answer's own result page
    (the ladder keeps each list run's page, a local read)."""

    def __init__(self, inner: Any, ladder: Any):
        self.inner, self.ladder = inner, ladder

    async def __call__(self, tool: Dict[str, Any], value: str, out: Dict[str, Any]) -> Dict[str, Any]:
        v = dict(await _call(self.inner, tool, value, out) or {})
        if v.get("verdict") != "disagrees" or not v.get("unnamed") or not v.get("coverage_ok"):
            return v
        own = (getattr(self.ladder, "doms", {}) or {}).get(id(out))
        res = [str(r.get("url") or "") for r in ((out.get("output") or {}).get("results") or []) if r.get("url")]
        if not own or not own[0] or not res:
            return v
        if block_results(own[0], own[1], res):
            return v
        return dict(v, verdict="agrees", waived=v.get("why"),
                    why="the oracle's render lists the answered results in order, covering its query results; none "
                        "sits in a block on the answer's own page (the naming rule waived)")


class _PacedChecker:
    """The independent checker behind the ladder's pacer - every call waits for the host's interval;
    a render that met a rate limit (``repair_check.OracleChecker(rate_limit=True)``: ``rate_limited`` in its answer) is
    asked once more after the stated wait; a second rate limit stays ``undeterminable`` (never a disagreement)."""

    def __init__(self, inner: Any):
        self.inner = inner

    async def __call__(self, tool: Dict[str, Any], value: str, out: Dict[str, Any]) -> Dict[str, Any]:
        from anyuse.execution import repair_pace as RP
        host = RP.host_of(tool)
        await RP.PACER.wait(host, "checker")
        v = dict(await _call(self.inner, tool, value, out) or {})
        if v.get("rate_limited") is None:
            return v
        w = RP.PACER.backoff(host, float(v.get("rate_limited") or 0.0))
        if w < 0:
            return dict(v, verdict="undeterminable", why=f"rate limit (the stated wait is over the cap): {v.get('why')}")
        await RP.PACER.wait(host, "checker retry after a rate limit")
        v2 = dict(await _call(self.inner, tool, value, out) or {})
        if v2.get("rate_limited") is not None:
            return dict(v2, verdict="undeterminable", why=f"rate limit after one retry: {v2.get('why')}")
        return dict(v2, rate_limit_retried=True)


class _Ladder:
    def __init__(self, page_factory, settle, config, store, checker, model, demonstrator, params, kw):
        self.factory, self.settle, self.cfg, self.store = page_factory, settle, config, store
        self.checker, self.model, self.demonstrator, self.params, self.kw = checker, model, demonstrator, params, kw
        self.calls, self.usd = 0, 0.0
        self.verdicts: Any = None
        self.upgrade: Any = None
        self.known_wrong = False
        #: the result URLs of a known wrong answer - never re-location anchors
        self.avoid: List[str] = []
        #: each list run's own page by its output's id (the outputs kept alive with it)
        self.doms: Dict[int, Tuple[str, str]] = {}
        self._keep: List[Dict[str, Any]] = []
        #: set once the tier-0 run is done: every later run (a repair candidate, the gate's second value, an
        #: upgrade's shadow run) re-proves the tool, so its in-run verifier asks no model
        self.reproving = False

    async def _run(self, tool: Dict[str, Any], value: str) -> Dict[str, Any]:
        if self.reproving:
            from anyuse.execution import attribute_verify as AV
            with AV.no_model("a repair's re-proof asks no model"):
                return await self._run_kept(tool, value)
        return await self._run_kept(tool, value)

    async def _run_kept(self, tool: Dict[str, Any], value: str) -> Dict[str, Any]:
        out = await self._run0(tool, value)
        if self.cfg.list_rule2 and out.get("status") == "ok" and \
                tool.get("workflow") in ("site_search", "filtered_search", "paged_search"):
            # the answer's own page, for the block-aware list rule (a local read)
            page = getattr(self, "last_page", None)
            try:
                dom = str(await page.dom() or "") if page is not None and hasattr(page, "dom") else ""
                url = str(await page.url() or "") if page is not None and hasattr(page, "url") else ""
            except Exception:                          # noqa: BLE001 - a read never fails a run
                dom, url = "", ""
            self.doms[id(out)] = (dom, url)
            self._keep.append(out)
        return out

    async def _run0(self, tool: Dict[str, Any], value: str) -> Dict[str, Any]:
        from anyuse.execution import demo_tool as T
        if self.cfg.pacing:
            return await self._paced_run(tool, value)
        page = await _page(self.factory)
        self.last_page = page
        out = await T.run_tool(page, tool, value, self.settle, params=self.params, **self.kw)
        if hasattr(page, "gotos"):
            out.setdefault("ladder_gotos", list(getattr(page, "gotos") or []))
        return out

    async def _page_text(self, page: Any) -> str:
        try:
            return str(await page.text() or "") if page is not None and hasattr(page, "text") else ""
        except Exception:                              # noqa: BLE001 - a read never fails a run
            return ""

    async def _paced_run(self, tool: Dict[str, Any], value: str) -> Dict[str, Any]:
        """The run after the host's interval; a run that met a RATE LIMIT (an HTTP 429, a page stating
        one) is retried once after the stated wait (or Retry-After); a second rate limit is status ``rate_limited``
        (infrastructure: no answer, no failure, no block)."""
        from anyuse.execution import demo_tool as T
        from anyuse.execution import repair_pace as RP
        host = RP.host_of(tool)
        first: Optional[Dict[str, Any]] = None
        for attempt in (0, 1):
            await RP.PACER.wait(host, "run" if attempt == 0 else "run retry after a rate limit")
            page = await _page(self.factory)
            self.last_page = page
            out = await T.run_tool(page, tool, value, self.settle, params=self.params, **self.kw)
            if hasattr(page, "gotos"):
                out.setdefault("ladder_gotos", list(getattr(page, "gotos") or []))
            why, stated = RP.rate_limit_why(await self._page_text(page),
                                            out.get("why") if out.get("status") == "blocked" else "")
            if not why:
                return dict(out, rate_limit_retried=first) if first else out
            if not stated and out.get("status") == "blocked":
                try:
                    stated = RP.retry_after(await page.network(want_bodies=False))
                except Exception:                      # noqa: BLE001
                    stated = 0.0
            first = {"why": why, "stated_s": stated, "status": out.get("status")}
            if attempt == 1 or RP.PACER.backoff(host, stated) < 0:
                break
        return dict(out, status=RP.RATE_LIMITED, accepted=False, output=None, unverified_output=out.get("output"),
                    why=f"rate limit: {first['why'] if first else ''}"[:240], rate_limited=first)

    async def _probe(self, tool: Dict[str, Any], value: str, step: str) -> Dict[str, Any]:
        """Tier 1 / 2's probe page (``repair_relocate.probe``); paced, and a rate-limited probe retried once."""
        if not self.cfg.pacing:
            return await R.probe(await _page(self.factory), tool, value, self.settle, step)
        from anyuse.execution import repair_pace as RP
        host = RP.host_of(tool)
        pr: Dict[str, Any] = {}
        for attempt in (0, 1):
            await RP.PACER.wait(host, "probe" if attempt == 0 else "probe retry after a rate limit")
            pr = await R.probe(await _page(self.factory), tool, value, self.settle, step)
            why, stated = RP.rate_limit_why(str((pr.get("result") or {}).get("text") or ""),
                                            pr.get("why") if pr.get("status") == "blocked" else "")
            if not why:
                return pr
            if attempt == 1 or RP.PACER.backoff(host, stated) < 0:
                break
        return dict(pr, status=RP.RATE_LIMITED, why=f"rate limit: {why}"[:200])

    async def run(self, value: str) -> Dict[str, Any]:
        st = self.store
        rev0 = st.active_rev()
        tool = st.active_tool()
        wf = str(tool.get("workflow") or "")
        out0 = await self._run(tool, value)
        self.reproving = True
        row: Dict[str, Any] = {"at": _iso(st.clock()), "value": value, "revision": rev0, "revision_before": rev0,
                               "status": out0.get("status"), "tier": 0, "model_calls": 0, "usd": 0.0}
        if out0.get("status") == RATE_LIMITED:
            # a rate limit after one retry is infrastructure - nothing to repair, nothing reported
            return self._finish(out0, dict(row, outcome=RATE_LIMITED, why=str(out0.get("why") or "")[:160]),
                                {"rate_limited": out0.get("rate_limited")})
        flag, fsteps = ("", [])
        if self.verdicts is not None and out0.get("status") in ("ok", "no_results"):
            from anyuse.execution import verdict_store as VS
            hit = self.verdicts.known_wrong(tool, value, out0)
            if hit:
                # a KNOWN WRONG answer is never ok - never the last known good one either
                lk = st.lkg(value)
                if lk and lk.get("key") == answer_key(wf, out0):
                    st.drop_lkg(value)
                out0 = VS.flag(out0, hit)
                row.update(status=VS.KNOWN_WRONG, known_wrong=True)
                self.known_wrong = True
                uo = out0.get("unverified_output") or {}
                self.avoid = [str(r.get("url")) for r in uo.get("results") or uo.get("items") or [] if r.get("url")] + \
                    ([str(uo.get("url"))] if uo.get("url") else [])
        #: the tier-0 outcome the canary may diagnose - a check failure, or a collapse the sentinel flags
        t0_status, collapse = str(out0.get("status") or ""), False
        if out0.get("status") in ("ok", "no_results"):
            flag, fsteps = sentinel(tool, value, out0, st.lkg(value))
            collapse = bool(flag)
            if not flag and out0.get("status") == "ok":
                # answered results that are cards of a promotion / advertising / recommendation
                # block beside the results - read on the result page the run just showed (a local read, no request)
                flag, fsteps = await self._promo_sentinel(tool, out0)
            if not flag and out0.get("status") == "ok" and self.cfg.block_sentinel:
                # answered results that sit only in a sidebar / widget / promotion block
                flag, fsteps = await self._block_sentinel(tool, out0)
            if not flag and out0.get("status") == "ok" and self.cfg.availability_sentinel:
                # the product's own page contradicts the answered availability
                flag, fsteps = await self._availability_sentinel(tool, out0)
            if not flag and self.cfg.probation and rev0 > 0 and out0.get("status") == "ok":
                # the accepted revision's untested answer (another answer than the last known
                # good one, or none known) is checked before it is reported
                lk = st.lkg(value)
                if not (lk and lk.get("key") == answer_key(wf, out0)):
                    v = await _call(self.checker, tool, value, out0) if self.checker else \
                        {"verdict": "undeterminable", "why": "no independent checker"}
                    if (v or {}).get("verdict") != "agrees":
                        why = (f"probation: revision {rev0}'s answer for this input is not verified (the checker: "
                               f"{(v or {}).get('verdict')}; {str((v or {}).get('why') or '')[:100]})")
                        held = dict(out0, status="incomplete", accepted=False, output=None,
                                    unverified_output=out0.get("output"), why=why)
                        return self._finish(held, dict(row, status="incomplete", outcome=UNVERIFIED, why=why),
                                            {"probation": v})
            if not flag:
                st.set_lkg(value, wf, out0, rev0)
                return self._finish(out0, dict(row, outcome=OK, why=str(out0.get("why") or "")[:160]), {})
            # the sentinel: the accepted answer collapsed against the last known good one - never accepted unverified
            out0 = dict(out0, status="incomplete", accepted=False, why=f"ladder sentinel: {flag}",
                        unverified_output=out0.get("output"), output=None)
            row["status"] = "incomplete"
        if self.known_wrong:
            # the step a known wrong answer points at is not known: the result structure (a sidebar, a sibling), and
            # for a list also the search
            steps = ["region"] if wf == "product_lookup" else ["region", "search"]
        else:
            steps = fsteps or R.localize(tool, out0)
        ev: Dict[str, Any] = {"tier0": {"status": out0.get("status"), "why": str(out0.get("why") or "")[:300]},
                              "steps": steps, "attempts": []}
        if flag:
            ev["sentinel"] = flag
        if self.known_wrong:
            ev["known_wrong"] = out0.get("known_wrong")
        if not steps:
            # a block, an infrastructure error, a product page's identity / fields: nothing to re-locate; tier 2 / 3
            # only for a check failure (never for a block or an error)
            if out0.get("status") in ("blocked", "error"):
                return self._finish(out0, dict(row, outcome=str(out0.get("status")), why="not repairable (never "
                                                                                          "retried)"), ev)
        if st.pinned() is not None:
            ev["why"] = f"the tool is pinned at revision {st.pinned()}: never rewritten automatically"
            return self._finish(out0, dict(row, outcome=NEEDS_HUMAN, tier=4, why=ev["why"]), ev)
        if st.rewrites_within(self.cfg.rewrite_window_days) >= self.cfg.max_rewrites:
            ev["why"] = (f"the cap on automatic rewrites: {self.cfg.max_rewrites} accepted in "
                         f"{self.cfg.rewrite_window_days:g} days")
            return self._finish(out0, dict(row, outcome=NEEDS_HUMAN, tier=4, why=ev["why"]), ev)
        upgraded_here = False
        if self.upgrade is not None:
            # a newer compile of the tool, through the safe upgrade gate (before any repair)
            got = await self._upgrade(tool, value, out0, ev)
            if got is None and (ev.get("upgrade") or {}).get("deferred"):
                # the upgrade gate met a rate limit - its decision is deferred (infrastructure); the
                # run keeps its tier-0 outcome (a known wrong answer stays withheld)
                return self._finish(out0, dict(row, outcome=RATE_LIMITED, why=str(ev["upgrade"].get("why"))[:160]), ev)
            if got is not None:
                upgraded_here = True
                out_u, cls_u = got
                from anyuse.execution import repair_upgrade as U
                if cls_u == U.USABLE or (self.known_wrong and cls_u == U.FAILURE):
                    return self._finish(out_u, dict(row, outcome=UPGRADED, revision=st.active_rev(),
                                                    status=out_u.get("status"), why=ev["upgrade"]["why"]), ev)
                # the upgraded revision fails as the old one did: the repair tiers work on it
                tool, out0 = st.active_tool(), out_u
                rev0 = st.active_rev()
                row["revision"] = rev0
                if self.verdicts is not None and out0.get("status") in ("ok", "no_results"):
                    from anyuse.execution import verdict_store as VS
                    hit = self.verdicts.known_wrong(tool, value, out0)
                    if hit:
                        out0 = VS.flag(out0, hit)
                if out0.get("status") in ("ok", "no_results"):
                    # an answer the upgrade gate could not verify is never reported as one
                    out0 = dict(out0, status="incomplete", accepted=False, output=None,
                                unverified_output=out0.get("output"),
                                why="the upgraded revision's answer is not verified (the upgrade gate)")
                steps = steps if self.known_wrong else (R.localize(tool, out0) or steps)
                ev["steps"] = steps
        if self.cfg.canary and not self.known_wrong and not upgraded_here and \
                (t0_status in ("failed", "incomplete") or collapse):
            # before any repair tier - does a known input still answer as before?
            c = await self._canary(tool, value, ev)
            if c.get("verdict") == "input":
                return self._input(out0, row, ev)
        if self.cfg.stale_guard:
            # loop guard 2: a failure soon after an accepted repair marks the tool STALE (no new repair)
            g = st.loop_guard(self.cfg.stale_after_repair_min, self.cfg.stale_for_min)
            g.failed(st.tool_id, str(out0.get("why") or "")[:120])
            may, why_stale = g.check(st.tool_id)
            st.save_loop_guard(g)
            if not may:
                ev["why"] = f"STALE: {why_stale}"
                return self._finish(out0, dict(row, outcome=NEEDS_HUMAN, tier=4, stale=True, why=ev["why"][:240]), ev)
        if not C.v10(tool):
            # a tool below version 10: the repair tiers patch / recompile version-10 tools only - the flag,
            # the state and the upgrade gate above are its ladder
            ev["why"] = "a tool below version 10: no repair tier applies (only the upgrade gate)"
            return self._finish(out0, dict(row, outcome=NEEDS_HUMAN, tier=4, why=ev["why"]), ev)
        lkg = st.lkg(value)
        unverified: Optional[Dict[str, Any]] = None
        top = 0
        # tier 1
        if self.cfg.tier1 and steps:
            top = 1
            self.extra_anchors = unread_anchors(out0)
            res = await self._tier1(tool, tool, value, steps, lkg, ev, [], 0)
            if res is not None and res["verdict"] == "accepted":
                return self._accept(res, row, ev, value, tool)
            if res is not None and res["verdict"] == UNVERIFIED:
                unverified = res
        # tier 2 (a check failure the steps localize; never a block or an error)
        if self.cfg.tier2 and self.model is not None and steps:
            if st.used_today(2) < self.cfg.tier2_calls_per_day:
                top = 2
                res = await self._tier2(tool, value, steps, out0, lkg, ev)
                if res and res["verdict"] == "accepted":
                    return self._accept(res, row, ev, value, tool)
                if res and res["verdict"] == UNVERIFIED and unverified is None:
                    unverified = res
            else:
                ev["attempts"].append({"tier": 2, "skipped": f"the daily budget ({self.cfg.tier2_calls_per_day} calls) "
                                                             f"is used"})
        # tier 3
        if self.cfg.tier3 and self.demonstrator is not None and out0.get("status") in ("failed", "incomplete"):
            if self.cfg.skip_tier3_on_content and content_failure(out0):
                # a re-demonstration recompiled with the same checks meets the same page
                ev["attempts"].append({"tier": 3, "skipped": f"a content check failed ({content_failure(out0)}): "
                                                             f"no re-demonstration"})
            elif st.used_today(3) < self.cfg.tier3_runs_per_day:
                top = 3
                res = await self._tier3(tool, value, out0, lkg, ev)
                if res and res["verdict"] == "accepted":
                    return self._accept(res, row, ev, value, tool)
                if res and res["verdict"] == UNVERIFIED and unverified is None:
                    unverified = res
            else:
                ev["attempts"].append({"tier": 3, "skipped": f"the daily budget ({self.cfg.tier3_runs_per_day} run) "
                                                             f"is used"})
        if unverified is not None:
            ev["unverified"] = {k: unverified.get(k) for k in ("tier", "rule", "patch", "gate", "answer")}
            return self._finish(out0, dict(row, outcome=UNVERIFIED, tier=unverified["tier"],
                                           why=unverified["gate"]["why"]), ev)
        ev["why"] = "no tier found a repair the gate accepts"
        return self._finish(out0, dict(row, outcome=NEEDS_HUMAN, tier=4, why=ev["why"], tier_tried=top), ev)

    async def _tier1(self, tool: Dict[str, Any], cur: Dict[str, Any], value: str, steps: Sequence[str],
                     lkg: Optional[Dict[str, Any]], ev: Dict[str, Any], prefix: List[Dict[str, Any]],
                     depth: int) -> Optional[Dict[str, Any]]:
        """Tier 1 on ``cur`` (the tool, or the tool with ``prefix``'s operations): each localized step's re-located
        candidates, as ONE patch of the original ``tool`` (``prefix`` + the candidate's operations). A candidate whose run
        now fails at ANOTHER step (two steps broke at once) is followed once more (``MAX_TIER1_DEPTH``): that step is
        re-located on the patched tool and both patches are tried together - each still step-local. The first accepted
        result, else the first "changed, unverified", else None."""
        unverified = None
        for step in steps:
            pr = await self._probe(cur, value, step)
            if pr.get("status") in ("blocked", RATE_LIMITED):
                ev["attempts"].append({"tier": 1, "step": step, "probe": pr.get("status")})
                return unverified
            cands = R.candidates(step, cur, value, pr, list((lkg or {}).get("anchors") or []) +
                                 list(getattr(self, "extra_anchors", []) or []), avoid=self.avoid,
                                 **({"fallback": True} if self.cfg.fallback_search else {}))
            ev["attempts"].append({"tier": 1, "step": step, "candidates": len(cands), "depth": depth})
            for c in cands[:self.cfg.tier1_candidates]:
                ops = list(prefix) + list(c["patch"]["ops"])
                if len(ops) > P.MAX_OPS:
                    continue
                patch = {"ops": ops, "why": c["patch"].get("why", "")}
                res = await self._try(tool, value, patch, 1, lkg, ev, rule=c["rule"] + (" (+ the step before)" if prefix else ""),
                                      probe=pr.get("result"))
                if res["verdict"] == "accepted":
                    return res
                if res["verdict"] == UNVERIFIED and unverified is None:
                    unverified = res
                if res["verdict"] == "rejected" and depth + 1 < MAX_TIER1_DEPTH and res.get("tool") is not None:
                    nxt = [x for x in (R.localize(res["tool"], res["out"]) or res["gate"].get("collapse_steps") or [])
                           if x != step]
                    if nxt:
                        sub = await self._tier1(tool, res["tool"], value, nxt, lkg, ev, ops, depth + 1)
                        if sub is not None and sub["verdict"] == "accepted":
                            return sub
                        if sub is not None and sub["verdict"] == UNVERIFIED and unverified is None:
                            unverified = sub
        return unverified

    async def _canary(self, tool: Dict[str, Any], value: str, ev: Dict[str, Any]) -> Dict[str, Any]:
        """The INPUT-vs-DRIFT canary (``repair_canary``) before any repair tier - ``ev["canary"]``, its
        verdict: ``input`` (the freshest OTHER input with a positive last known good answer, re-run on the active
        revision, answers as before), ``drift`` (it fails or collapsed; a broken probe path), ``refused`` (the tool may
        not be re-run: its typed reason; with ``probe_canary`` the probe's evidence), ``no_lkg`` (no other input to
        re-run: the ladder as without the canary). One canary per tool and revision per ``canary_ttl_min`` (a cached
        INPUT verdict is not reused for an input that has a last known good answer of its own). Its runs open pages of
        the same ``page_factory`` (the site's budget counts them) inside this method (a stack-based meter can tag
        them)."""
        from anyuse.execution import repair_canary as RC
        from anyuse.execution import verdict_store as VS
        st = self.store
        rev = st.active_rev()
        c: Dict[str, Any] = {}
        ev["canary"] = c
        refusal = RC.rerun_refusal(tool)
        if refusal:
            c.update(verdict=RC.REFUSED, refusal=refusal, why=refusal["why"], gotos=0)
            if self.cfg.probe_canary:
                v = RC.pick_canary(st.lkgs(), value)
                page = await _page(self.factory)
                pr = await RC.probe_path(page, tool, self.settle, value=v)
                c.update(probe=pr, gotos=int(pr.get("gotos") or 0))
                if pr.get("verdict") == RC.PATH_BROKEN:
                    c.update(verdict=RC.DRIFT, why=f"probe: {pr.get('why')}")
            st.add_canary({"rev": rev, "verdict": c["verdict"], "why": str(c["why"])[:200],
                           "refusal": refusal["code"], "probe": (c.get("probe") or {}).get("verdict"),
                           "gotos": (c.get("probe") or {}).get("gotos", 0)})
            return c
        if not self.cfg.canary_own_lkg and RC.positive(st.lkg(value)):
            # the input answered before - its failure is drift evidence a canary on another input cannot exclude
            c.update(verdict=RC.OWN_LKG, gotos=0, why="the failing input has a positive last known good answer of its "
                                                      "own: no canary (its failure is evidence of drift)")
            st.add_canary({"rev": rev, "verdict": RC.OWN_LKG, "failing": value, "why": c["why"], "gotos": 0})
            return c
        hit = RC.cached(st.canaries(), rev, st.clock(), self.cfg.canary_ttl_min)
        if hit is not None and (hit.get("verdict") == RC.DRIFT or st.lkg(value) is None):
            c.update(verdict=hit["verdict"], why=str(hit.get("why") or ""), value_ref=hit.get("value_ref"), cached=True,
                     cached_at=hit.get("at"), gotos=0)
            st.add_canary({"rev": rev, "verdict": hit["verdict"], "cached": True, "value": hit.get("value"),
                           "value_ref": hit.get("value_ref"), "failing": value, "why": c["why"][:200], "gotos": 0})
            return c
        cv = RC.pick_canary(st.lkgs(), value)
        if cv is None:
            c.update(verdict=RC.NO_LKG, why="no other input has a positive last known good answer: no canary", gotos=0)
            st.add_canary({"rev": rev, "verdict": RC.NO_LKG, "failing": value, "why": c["why"], "gotos": 0})
            return c
        out_c = await self._blocked_out(tool, await self._run(tool, cv))
        kw = bool(self.verdicts is not None and out_c.get("status") in ("ok", "no_results") and
                  self.verdicts.known_wrong(tool, cv, out_c))
        flagged = ""
        if out_c.get("status") == "ok" and not kw:
            flagged, _s = await self._promo_sentinel(tool, out_c)
        verdict, why = RC.canary_verdict(tool, cv, out_c, st.lkg(cv), known_wrong=kw, flagged=flagged)
        c.update(verdict=verdict, why=why, value_ref=VS.input_ref(cv), cached=False, status=out_c.get("status"),
                 gotos=len(out_c.get("ladder_gotos") or []))
        st.add_canary({"rev": rev, "verdict": verdict, "value": cv, "value_ref": c["value_ref"], "failing": value,
                       "status": out_c.get("status"), "why": why[:200], "gotos": c["gotos"]})
        return c

    def _input(self, out0: Dict[str, Any], row: Dict[str, Any], ev: Dict[str, Any]) -> Dict[str, Any]:
        """The INPUT verdict: the run keeps its tier-0 failure (a collapsed answer stays withheld), reported
        as this input's own (``out["input_failure"]``: the reason in plain words, the canary as evidence). No repair,
        no revision, no last known good answer written."""
        from anyuse.execution import repair_canary as RC
        c = ev.get("canary") or {}
        reason = RC.input_reason(out0)
        out = dict(out0)
        out["input_failure"] = {"reason": reason, "canary": {k: c.get(k) for k in ("verdict", "why", "value_ref",
                                                                                   "cached")}}
        return self._finish(out, dict(row, outcome=INPUT_FAILURE, tier=0, why=f"{reason} ({c.get('why')})"[:240]), ev)

    async def _promo_sentinel(self, tool: Dict[str, Any], out0: Dict[str, Any]) -> Tuple[str, List[str]]:
        """The promo sentinel (ladder on; a list answered by a tool whose card read is older than version 12, which
        leaves such cards out itself): ``(why, ["region"])`` when answered results are cards of a promotion /
        advertising / recommendation block beside the results (``promo_results``), else ``("", [])``."""
        if tool.get("workflow") not in ("site_search", "filtered_search", "paged_search") or \
                C.cards_v12(tool.get("region") or {}) or not (tool.get("region") or {}).get("cards"):
            return "", []
        page = getattr(self, "last_page", None)
        try:
            dom = str(await page.dom() or "") if page is not None and hasattr(page, "dom") else ""
            url = str(await page.url() or "") if page is not None and hasattr(page, "url") else ""
        except Exception:                              # noqa: BLE001 - a read never fails a run
            return "", []
        res = [str(r.get("url") or "") for r in ((out0.get("output") or {}).get("results") or []) if r.get("url")]
        if not dom or not res:
            return "", []
        bad = promo_results(tool, dom, url, res)
        if not bad:
            return "", []
        return (f"{len(bad)} answered result(s) are cards of a promotion / advertising / recommendation block beside "
                f"the results"), ["region"]

    async def _block_sentinel(self, tool: Dict[str, Any], out: Dict[str, Any]) -> Tuple[str, List[str]]:
        """The block sentinel (a list answer, read on the result page the run just showed - a local read, no
        request): ``(why, ["region"])`` when answered results sit only in a sidebar / widget / promotion block."""
        if tool.get("workflow") not in ("site_search", "filtered_search", "paged_search"):
            return "", []
        page = getattr(self, "last_page", None)
        try:
            dom = str(await page.dom() or "") if page is not None and hasattr(page, "dom") else ""
            url = str(await page.url() or "") if page is not None and hasattr(page, "url") else ""
        except Exception:                              # noqa: BLE001 - a read never fails a run
            return "", []
        res = [str(r.get("url") or "") for r in ((out.get("output") or {}).get("results") or []) if r.get("url")]
        bad = block_results(dom, url, res) if dom and res else []
        if not bad:
            return "", []
        return (f"{len(bad)} answered result(s) sit only in a block beside the results ({bad[0][1][:80]})"), ["region"]

    async def _availability_sentinel(self, tool: Dict[str, Any], out: Dict[str, Any]) -> Tuple[str, List[str]]:
        """For a product answer, read on the page the run just opened (a local read): ``(why, [])`` when
        the product's own page states another availability attached to the product than the answer
        (``repair_check.availability_contradiction(v2=True)``: a not-now line, goods on order, a plain in-stock line
        against "a few left", a delivery time against out of stock)."""
        if tool.get("workflow") != "product_lookup":
            return "", []
        from anyuse.execution import repair_check as RCK
        o = out.get("output") or {}
        page = getattr(self, "last_page", None)
        text = await self._page_text(page)
        if not text or not isinstance(o, dict):
            return "", []
        from anyuse.execution import attribute_verify as AV
        if AV.enabled():
            # verifier independence: the availability read by the shared independent verifier (its own phrases,
            # unknown is uncertain, the small model on an open attribute) - never the extractor's vocabulary
            try:
                dom = str(await page.dom() or "") if hasattr(page, "dom") else ""
            except Exception:                          # noqa: BLE001 - a read never fails a run
                dom = ""
            v = AV.check("product_lookup", {"availability": o.get("availability"), "name": o.get("name"),
                                            "price": o.get("price"), "url": o.get("url")},
                         AV.Page(dom, text, str(o.get("url") or "")), AV.default_resolver())
            d = next((x for x in v.decisions if x.attr == "availability"), None)
            return (f"availability sentinel (attribute verify): {d.why}"[:240], []) if d is not None and \
                d.critical and d.status != AV.VERIFIED else ("", [])
        contra = RCK.availability_contradiction(text, o.get("availability"), o.get("price"), v2=True)
        return (f"availability sentinel: {contra}", []) if contra else ("", [])

    async def _blocked_out(self, tool: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
        """A run's answer the block sentinel flags is not an answer (the gate and the shadow runs)."""
        if not self.cfg.block_sentinel or out.get("status") != "ok":
            return out
        why, _s = await self._block_sentinel(tool, out)
        if not why:
            return out
        return dict(out, status="incomplete", accepted=False, output=None, unverified_output=out.get("output"),
                    why=f"block sentinel: {why}")

    async def _upgrade(self, tool: Dict[str, Any], value: str, out0: Dict[str, Any],
                       ev: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], str]]:
        """The candidate revision (``self.upgrade(tool)``) through the safe upgrade gate; ``(the candidate's outcome for
        value, its class)`` when accepted, else None (the decision is in ``ev["upgrade"]`` and the store's log)."""
        from anyuse.execution import repair_upgrade as U
        cand = await _call(self.upgrade, tool)
        if not cand:
            return None

        async def run(t: Dict[str, Any], v: str) -> Dict[str, Any]:
            return await self._blocked_out(t, await self._run(t, v))
        res = await U.evaluate(self.store, cand, run, self.verdicts, self.checker, current=(value, out0))
        ev["upgrade"] = {k: res.get(k) for k in ("accepted", "why", "from_version", "to_version", "rev")}
        if res.get("deferred"):
            ev["upgrade"]["deferred"] = True
        ev["upgrade"]["rows"] = [{k: r.get(k) for k in ("value", "old", "new", "change")} for r in res.get("rows") or []]
        if not res.get("accepted"):
            return None
        mine = next((r for r in res.get("rows") or [] if r.get("value") == value), None)
        return (mine["out_new"], mine["new"]) if mine else None

    async def _second(self, cand: Dict[str, Any], value: str) -> Optional[Tuple[str, Dict[str, Any], Any]]:
        v2 = self.store.other_value(value)
        if v2 is None:
            return None
        out2 = await self._run(cand, v2)
        return (v2, out2, self.store.lkg(v2))

    async def _try(self, tool: Dict[str, Any], value: str, patch: Any, tier: int, lkg: Optional[Dict[str, Any]],
                   ev: Dict[str, Any], rule: str = "", probe: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Validate and apply ``patch`` to a copy, check a new card structure on the probe page (``structure_guard``),
        run it, gate it."""
        att: Dict[str, Any] = {"tier": tier, "rule": rule}
        ev["attempts"].append(att)
        try:
            cand = P.apply(tool, patch)
        except P.PatchError as exc:
            att.update(verdict="invalid", why=str(exc)[:200])
            return {"verdict": "invalid", "tier": tier}
        att["patch"] = P.validate(patch, tool)
        anchors = list((lkg or {}).get("anchors") or []) + list(getattr(self, "extra_anchors", []) or [])
        refused = structure_guard(tool, cand, probe, anchors)
        if not refused and self.cfg.results_page:
            # new result cards are read only on the search's own result page
            refused = results_page_refusal(tool, cand, probe)
        if refused:
            att.update(verdict="refused", why=refused)
            return {"verdict": "refused", "tier": tier}
        return await self._run_and_gate(cand, value, tier, lkg, att, rule, att["patch"])

    async def _run_and_gate(self, cand: Dict[str, Any], value: str, tier: int, lkg: Optional[Dict[str, Any]],
                            att: Dict[str, Any], rule: str, diff: Any) -> Dict[str, Any]:
        out = await self._blocked_out(cand, await self._run(cand, value))
        if self.cfg.results_page and out.get("status") == "ok":
            # a repaired run's answer read off the search's own result page is no answer
            off = off_search_page(cand, ((out.get("checks") or {}).get("result_page") or {}).get("url"), out)
            if off:
                out = dict(out, status="incomplete", accepted=False, output=None, unverified_output=out.get("output"),
                           why=f"results page: {off}")
        second = await self._second(cand, value) if out.get("status") == "ok" else None
        g = await gate(cand, value, out, lkg, self.checker, second, verdicts=self.verdicts,
                       require_checker=self.known_wrong)
        att.update(verdict=g["verdict"], why=g["why"], status=out.get("status"))
        return {"verdict": g["verdict"], "tier": tier, "rule": rule, "patch": diff, "gate": g, "tool": cand,
                "out": out, "second": second, "answer": answer_of(str(cand.get("workflow") or ""), out)}

    async def _tier2(self, tool, value, steps, out0, lkg, ev) -> Optional[Dict[str, Any]]:
        from anyuse.execution import repair_model as M
        step = (steps or ["region"])[0]
        pr = await self._probe(tool, value, step)
        if pr.get("status") in ("blocked", RATE_LIMITED):
            ev["attempts"].append({"tier": 2, "probe": pr.get("status")})
            return None
        anchors = list((lkg or {}).get("anchors") or []) + list(getattr(self, "extra_anchors", []) or [])
        if int(getattr(self.model, "design", 1) or 1) >= 2:
            # design 2 also sees tier 1's near misses and every localized step
            ans = await _call(lambda *a: self.model.propose(*a, near=list(ev.get("attempts") or []), steps=list(steps)),
                              tool, step, out0, pr, anchors)
        else:
            ans = await _call(self.model.propose, tool, step, out0, pr, (lkg or {}).get("anchors") or [])
        calls, usd = int(ans.get("calls") or 0), float(ans.get("usd") or 0.0)
        self.calls += calls
        self.usd += usd
        self.store.add_cost(2, calls, usd, {"prompt_tokens": ans.get("prompt_tokens"),
                                             "completion_tokens": ans.get("completion_tokens"), "model": ans.get("model"),
                                             "error": ans.get("error")})
        patch = M.patch_from_text(ans.get("text") or "")
        if patch is None:
            ev["attempts"].append({"tier": 2, "verdict": "invalid", "why": "no JSON patch in the answer",
                                   "error": ans.get("error")})
            return None
        return await self._try(tool, value, patch, 2, lkg, ev, rule=f"model: {step}", probe=pr.get("result"))

    async def _tier3(self, tool, value, out0, lkg, ev) -> Optional[Dict[str, Any]]:
        from anyuse.execution import demo_breadth as DB
        wf = str(tool.get("workflow") or "")
        goal = DB.goal_text(wf, value, int(tool.get("k") or 2)) if wf in DB.BREADTH else C.goal_text(wf, value)
        hint = {"tool": {k: tool.get(k) for k in ("workflow", "site", "start_url", "search", "refine", "pager")},
                "failure": {"status": out0.get("status"), "why": str(out0.get("why") or "")[:300]},
                "last_known_good": (lkg or {}).get("anchors")}
        att: Dict[str, Any] = {"tier": 3, "rule": "re-demonstration"}
        ev["attempts"].append(att)
        got = await _call(self.demonstrator, goal, hint) or {}
        calls, usd = int(got.get("calls") or 0), float(got.get("usd") or 0.0)
        self.calls += calls
        self.usd += usd
        self.store.add_cost(3, 1, usd, {"demonstrator_calls": calls})
        tr = got.get("trace")
        if not tr:
            att.update(verdict="invalid", why="the re-demonstration returned no trace")
            return None
        # recompiled at the tool's OWN version (a version-11 tool stays version 11; the boundary checks it)
        comp = C.compile_trace(tr, version=int(tool.get("version") or 10))
        if not comp.get("ok"):
            att.update(verdict="invalid", why=f"the re-demonstration does not compile ({str(comp.get('why'))[:120]})")
            return None
        cand = comp["tool"]
        b = tier3_boundary(tool, cand)
        if b:
            att.update(verdict="invalid", why=b)
            return None
        diff = {"recompiled_from": (cand.get("compiled_from") or {}).get("trace_sha256"),
                "changed": sorted(k for k in set(tool) | set(cand) if tool.get(k) != cand.get(k))}
        return await self._run_and_gate(cand, value, 3, lkg, att, "re-demonstration", diff)

    def _accept(self, res: Dict[str, Any], row: Dict[str, Any], ev: Dict[str, Any], value: str,
                tool: Dict[str, Any]) -> Dict[str, Any]:
        st = self.store
        cand, out = res["tool"], res["out"]
        if self.cfg.identical_no_drift:
            from anyuse.execution import repair_canary as RC
            if RC.same_bytes(cand, tool):
                # loop guard 1: the "repair" is the active revision itself - the failure was no drift;
                # nothing is written (no revision, no last known good answer, no rewrite counted)
                ev["no_drift"] = (f"the tier-{res['tier']} repair is byte-identical to revision {row.get('revision')}: "
                                  f"no drift, nothing written")
                return self._finish(out, dict(row, outcome=NO_DRIFT, tier=res["tier"], status=out.get("status"),
                                              why=ev["no_drift"]), ev)
        rev = st.add_revision(cand, res["tier"], res["patch"], {"tier0": ev["tier0"], "steps": ev["steps"],
                                                                "rule": res["rule"], "gate": res["gate"]},
                              self.usd, self.calls)
        wf = str(cand.get("workflow") or "")
        st.set_lkg(value, wf, out, rev)
        if res.get("second") and res["second"][1].get("status") in ("ok", "no_results"):
            st.set_lkg(res["second"][0], wf, res["second"][1], rev)
        if self.cfg.stale_guard:
            # loop guard 2: the accepted repair is a heal (``HealGuard``: a new revision was written)
            g = st.loop_guard(self.cfg.stale_after_repair_min, self.cfg.stale_for_min)
            g.heal_succeeded(st.tool_id, ("rev", row.get("revision")), ("rev", rev))
            st.save_loop_guard(g)
        return self._finish(out, dict(row, outcome=REPAIRED, tier=res["tier"], revision=rev, status=out.get("status"),
                                      why=res["gate"]["why"]), ev)

    def _finish(self, out: Dict[str, Any], row: Dict[str, Any], ev: Dict[str, Any]) -> Dict[str, Any]:
        row.update(model_calls=self.calls, usd=round(self.usd, 6))
        if ev.get("canary"):
            # the canary's verdict and its traffic (page loads) in the run's ledger row
            row["canary"] = {k: ev["canary"].get(k) for k in ("verdict", "cached", "gotos") if k in ev["canary"]}
        row["line"] = readout(row)
        state = self.store.record_run({k: v for k, v in row.items()}, self.cfg.watch_runs)
        out = dict(out)
        out["ladder"] = dict(row, state=state["state"], evidence=ev)
        return out
