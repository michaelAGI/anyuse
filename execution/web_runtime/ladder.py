"""Run the WHOLE ladder over a recorded sample and emit the tables.

usage:
  python execution/web_runtime/ladder.py --recordings DIR --out results.md --json-out rows.json
                         [--levers on|off] [--cdp http://IP:9222] [--no-live]

Tiers, in the order the ladder tries them:

  1  a proved compiled request          milliseconds, 0 model calls
  2  deterministic browser replay       seconds,      0 model calls
  3 a step agent
  4  blocked -> a human

A tier may only claim a workflow if its output is CORRECT against the UI
ground truth the recorder derived for that input. A tier that returns nothing
counts as a failure unless the page itself said there were no results.

Model calls in tiers 1 and 2 are counted, not assumed: a non-zero count makes
the run report itself as unverified.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from anyuse.execution.api_compile import (  # noqa: E402
    Levers, normalise, page_chrome, values_match, visible_items)
from anyuse.execution.api_step import ApiStepExecutor  # noqa: E402
from anyuse.execution.api_verify import compile_and_verify  # noqa: E402
from anyuse.execution.cdp_replay import ReplayExecutor, template_from_recording  # noqa: E402

PROVED_TIER1 = "tier1_request"
TIER2 = "tier2_replay"
TIER3 = "tier3_agent"
TIER4 = "tier4_blocked"


def wilson(successes: int, n: int, z: float = 1.959963985) -> Tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - margin), min(1.0, centre + margin)


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def p90(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(math.ceil(0.9 * len(ordered))) - 1)
    return ordered[idx]


def correctness(got: List[str], want: List[str], min_values: int = 3,
                value: str = "") -> Tuple[bool, str]:
    """Is ``got`` the same answer as the UI showed?

    The same predicate the compiler and the verifier use, so a tier is not
    graded more kindly than a template. At least ``min_values`` ground-truth
    values must exist: one matching field is a coincidence, which is why the
    class-B standard names the number.
    """
    want_n = [normalise(w) for w in want if normalise(w)]
    got_n = [normalise(g) for g in got if normalise(g)]
    if len(want_n) < min_values:
        if value and want_n:
            # the sparse-result contract (api_verify.sparse_identity):
            # exactly the visible results, each naming the query
            from anyuse.execution.api_verify import SPARSE_CONTRACT, sparse_identity
            if SPARSE_CONTRACT:
                ok, why = sparse_identity(want_n, got_n, value)
                if ok:
                    return True, why
                return False, (f"insufficient evidence: {len(want_n)} ground-truth "
                               f"value(s), {min_values} required, and the {why}")
        return False, (f"insufficient evidence: {len(want_n)} ground-truth "
                       f"value(s), {min_values} required")
    if not got_n:
        return False, "the tier returned nothing"
    hit = sum(1 for w in want_n if any(values_match(w, g) for g in got_n))
    cov = hit / len(want_n)
    if cov < 0.8:
        return False, f"coverage {cov:.2f} < 0.80 against the UI ground truth"
    return True, f"coverage {cov:.2f} of {len(want_n)} values"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load(recdir: str, corpus) -> Dict[str, Dict[str, dict]]:
    out: Dict[str, Dict[str, dict]] = {}
    for wf in corpus:
        recs: Dict[str, dict] = {}
        for tag in ("rec", "fresh1", "fresh2", "skip"):
            path = os.path.join(recdir, f"{wf['id']}.{tag}.json")
            if os.path.exists(path):
                with open(path) as fh:
                    recs[tag] = json.load(fh)
        if recs:
            out[wf["id"]] = recs
    return out


class RecordedTransport:
    """Answers a compiled template from the recorded responses (offline mode)."""

    def __init__(self, recordings: List[dict]):
        import urllib.parse
        self.index: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
        for rec in recordings:
            for req in rec.get("network") or []:
                url = req.get("url") or ""
                if not url.startswith("http"):
                    continue
                sp = urllib.parse.urlsplit(url)
                self.index[(sp.netloc, sp.path)].append(req)
        self.calls = 0

    def send(self, method, url, headers, body, timeout_s):
        import urllib.parse

        from anyuse.execution.api_step import ApiResponse
        self.calls += 1
        sp = urllib.parse.urlsplit(url)
        wanted = urllib.parse.parse_qs(sp.query)
        best, best_score = None, -1
        for req in self.index.get((sp.netloc, sp.path), []):
            if (req.get("method") or "GET").upper() != method.upper():
                continue
            got = urllib.parse.parse_qs(urllib.parse.urlsplit(req["url"]).query)
            score = sum(1 for k, v in wanted.items() if got.get(k) == v)
            if method.upper() == "POST":
                score += 2 if (req.get("post_data") or "") == (body or "") else -5
            if score > best_score:
                best, best_score = req, score
        # Every query parameter the template asks for must match. A request with
        # NO query is identified by its (host, path) alone, which the index key
        # already fixed — the old `max(1, ...)` floor made such a request
        # unanswerable offline, which silently suppressed exactly the
        # path-parameter workflows. Live runs never use this transport.
        floor = len(wanted) if method.upper() != "POST" else 0
        if best is None or best_score < floor:
            return ApiResponse(599, {"Content-Type": "text/plain"},
                               "no recorded response for this request")
        return ApiResponse(int(best.get("status") or 200),
                           {"Content-Type": best.get("mime") or "application/json"},
                           best.get("body") or "")


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _maybe_browser_only(base: dict, proof) -> str:
    """Reclassify a client-side 403/429 when the BROWSER was served the page.

    No new request is made and no refusing host is re-hit: the 403 is in the
    proof that just ran, and the browser observation is the recording made
    earlier.
    """
    import re as _re

    from anyuse.execution.blocked import (FORBIDDEN, RATE_LIMITED, BlockedState,
                                          browser_only_from_evidence)
    text = " ".join(str(c.reason or "") for c in proof.cases)
    m = _re.search(r"status (\d{3})", text) or _re.search(r"\b(403|429)\b", text)
    if not m:
        return ""
    status = int(m.group(1))
    if status not in (403, 429):
        return ""
    url = str(base.get("final_url") or "")
    items = len((base.get("ui") or {}).get("items") or [])
    state = BlockedState(blocked=True, status=status,
                         reason=FORBIDDEN if status == 403 else RATE_LIMITED,
                         host=__import__("urllib.parse", fromlist=["parse"]).urlsplit(url).netloc)
    entry = browser_only_from_evidence(state, url, items)
    return entry["host"] if entry else ""


def run_workflow(wf: dict, recs: Dict[str, dict], levers: Levers,
                 live: bool, replay: Optional[ReplayExecutor],
                 permission_fetch: bool, roles: Optional[Dict[str, List[str]]] = None,
                 executor: Optional[ApiStepExecutor] = None,
                 preflight=None) -> dict:
    """One workflow down the ladder.

    ``roles`` (the held-out re-proof) names which
    recording tags FIT (``fitting``), SELECT (``selection``) and VALIDATE
    (``validation``); ``rec`` is always the compile input. Fitting and selection
    recordings shape the template (chrome, bindings, runner-up choice) and
    validation recordings are only read by the final check. Without ``roles``
    the ``sample2``-``sample4`` corpora behave as before: every further load is a fresh case.
    ``executor`` and ``preflight`` let a bench send the proof through its own
    paced transport and permission gate.
    """
    row: Dict[str, Any] = {
        "id": wf["id"], "site": wf["site"], "category": wf["category"],
        "cls": wf["cls"], "in_d_allowed": True,
        "tier": "", "reason": "", "rung": "",
        "tier1_ms": None, "tier2_ms": None,
        "tier1_model_calls": 0, "tier2_model_calls": 0,
        "ground_truth_n": 0, "correct": None, "correctness_note": "",
    }

    if "skip" in recs:
        row["in_d_allowed"] = False
        row["tier"] = "robots_disallow"
        row["reason"] = recs["skip"].get("note") or "robots.txt disallows the path"
        return row

    base = recs.get("rec")
    if base is None:
        row["tier"] = "transport_error"
        return row
    if base.get("blocked_signal"):
        row["tier"] = TIER4
        row["reason"] = base["blocked_signal"]
        return row

    def usable(tags):
        return [recs[t] for t in tags if t in recs
                and not recs[t].get("transport_error")
                and not recs[t].get("blocked_signal")]
    if roles is None:
        fit, sel = [], []
        fresh = usable(("fresh1", "fresh2"))
        chrome = page_chrome([base, *fresh])
    else:
        fit, sel = usable(roles.get("fitting", ())), usable(roles.get("selection", ()))
        fresh = usable(roles.get("validation", ()))
        # chrome for tier 2's ground truth from the non-validation loads only
        chrome = page_chrome([base, *fit, *sel])
    items = visible_items(base, exclude=chrome)
    row["ground_truth_n"] = len(items)
    row["ground_truth_selector"] = base.get("ground_truth_selector", "")
    if not items:
        row["tier"] = "no_ground_truth"
        row["reason"] = (base.get("recorder_warning")
                         or "no repeated group changed between the recorded inputs")
        return row

    # ---- tier 1: a proved compiled request --------------------------------
    if live and permission_fetch:
        try:
            from anyuse.execution.api_permission import check_permission
            check_permission(str(base.get("final_url") or ""))
        except Exception as exc:                 # a courtesy check that cannot
            row["permission_error"] = repr(exc)[:120]     # run is not a failure

    pool = [r for t, r in recs.items() if t != "skip"]
    if executor is None:
        executor = ApiStepExecutor() if live else ApiStepExecutor(
            transport=RecordedTransport(pool))
    t0 = time.monotonic()
    if roles is None:
        compiled, proof = compile_and_verify(base, fresh, executor=executor,
                                             levers=levers, preflight=preflight)
    else:
        compiled, proof = compile_and_verify(base, fresh, executor=executor,
                                             levers=levers, preflight=preflight,
                                             fitting_recordings=fit,
                                             selection_recordings=sel)
    row["compile_reason"] = (compiled.reason or "")[:220]
    # the runner-up binding counter,
    # kept on every row so one run over stored recordings says whether the
    # machinery (api_compile binding_alternatives, api_verify binding_rejected)
    # ever fires: offered = alternatives the compiler proposed, rejected =
    # binding candidates that failed selection (or the chosen split that failed
    # validation), chosen = a runner-up binding site was picked.
    row.update(runner_up_counts(compiled))
    # a 3-load recording whose selector the dry look
    # derived over rec + fresh1 cannot hold fresh1 out -> at most "checked"
    row["proof_status"] = proof.effective_status
    row["proof_split"] = dict(proof.split)
    row["chrome_filtered"] = len(compiled.chrome_dropped)
    if compiled.template is not None:
        row["rung"] = ("readonly_post" if getattr(compiled.template, "readonly_post", False)
                       else getattr(compiled.template, "extract_kind", "json")
                       if not getattr(compiled.template, "steps", None) else "chain")
        row["template_url"] = getattr(compiled.template, "url", "")
    if proof.passed:
        lat = [c.elapsed_ms for c in proof.cases if c.elapsed_ms]
        row["tier"] = PROVED_TIER1
        row["tier1_ms"] = statistics.median(lat) if lat else None
        # The fresh-input proof IS the correctness check for tier 1: it already
        # compares the extraction with the UI ground truth on text, order and
        # count, on inputs that were not compiled from. Re-grading it here with
        # a weaker predicate would only hide the stricter one.
        thin = [c for c in proof.cases if c.ui_count < 3]
        row["correct"] = not thin
        row["correctness_note"] = (
            f"fresh-input proof: {proof.passed_count}/{len(proof.cases)} case(s), "
            f"mean coverage "
            f"{statistics.mean([c.coverage for c in proof.cases]):.2f}"
            if not thin else
            f"proof passed but {len(thin)} case(s) had fewer than 3 ground-truth "
            f"values — insufficient evidence under the specified standard")
        return row
    row["tier1_failed"] = (proof.reason or "")[:200]
    if globals().get("BROWSER_ONLY_ON"):
        row["browser_only"] = _maybe_browser_only(base, proof)

    # ---- tier 2: deterministic browser replay ------------------------------
    if replay is None:
        row["tier"] = TIER3
        row["reason"] = "tier 2 not attempted (no CDP endpoint given)"
        return row

    template = template_from_recording(
        base, wf["param"],
        recordings=(([base, *fresh] if roles is None else [base, *fit, *sel])
                    if levers.path_binding else None),
        readiness=globals().get("READINESS_ON", False))
    row["tier2_settle"] = "ready" if globals().get("READINESS_ON") else "timer"
    if not template.url:
        row["tier"] = TIER3
        row["reason"] = f"tier 2 refused: {template.notes[:140]}"
        return row

    from anyuse.execution.cdp_replay import finalize_minimum, for_validation
    proof_template = for_validation(template)
    per_case: List[Tuple[bool, str, int]] = []
    lats: List[float] = []
    failures: List[str] = []
    for f in fresh:
        value = (f.get("param") or {}).get("value", "")
        out = replay.execute(proof_template, {wf["param"]: value})
        row["tier2_model_calls"] += int(out.get("model_calls") or 0)
        if out.get("elapsed_ms"):
            lats.append(out["elapsed_ms"])
        if not out.get("success"):
            failures.append(f"{value!r}: {str(out.get('error'))[:110]}")
            if out.get("requires_human"):
                row["tier"] = TIER4
                row["reason"] = str(out.get("error"))[:160]
                return row
            continue
        got = [str(x) for x in (out.get("extracted") or {}).get("items") or []]
        # graded PER CASE: concatenating the cases and truncating at 40 let a
        # 30-row first case crowd out the second case's rows (re-proof,
        # slackbuilds_search: coverage 0.65 on two correct pages)
        ok_c, note_c = correctness(got[:40], visible_items(f, exclude=chrome), min_values=3)
        per_case.append((ok_c, f"{value!r}: {note_c}", len(got)))
    row["tier2_ms"] = statistics.median(lats) if lats else None
    if failures:
        row["tier2_failed"] = "; ".join(failures)[:220]
        row["tier"] = TIER3
        row["reason"] = "tier 2 did not reproduce the page: " + failures[0]
        return row
    ok = bool(per_case) and all(c[0] for c in per_case)
    bad = [c[1] for c in per_case if not c[0]]
    note = ("; ".join(bad) if bad else "; ".join(c[1] for c in per_case))[:220]
    row["correct"], row["correctness_note"] = ok, note
    if ok:
        finalize_minimum(template, [c[2] for c in per_case])
        row["tier"] = TIER2
        return row
    row["tier"] = TIER3
    row["reason"] = f"tier 2 output did not match the UI: {note}"
    return row


def runner_up_counts(compiled) -> Dict[str, Any]:
    """The runner-up binding counter of one compile (see ``run_workflow``)."""
    return {
        "binding_alternatives": len(getattr(compiled, "binding_alternatives", None) or []),
        "binding_rejected": int(getattr(compiled, "binding_rejected", 0) or 0),
        "runner_up_chosen": (getattr(compiled, "binding", None) or {}).get("where") == "alternative",
    }


def runner_up_summary(rows: List[dict]) -> Dict[str, int]:
    """Totals over a run: did the runner-up binding machinery ever fire?"""
    return {
        "workflows": len(rows),
        "offered": sum(1 for r in rows if r.get("binding_alternatives")),
        "alternatives": sum(int(r.get("binding_alternatives") or 0) for r in rows),
        "rejected": sum(int(r.get("binding_rejected") or 0) for r in rows),
        "chosen": sum(1 for r in rows if r.get("runner_up_chosen")),
        "chosen_and_proved": sum(1 for r in rows if r.get("runner_up_chosen")
                                 and r.get("tier") == PROVED_TIER1),
    }


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

def tables(rows: List[dict], levers_label: str, off_rows: Optional[List[dict]] = None
           ) -> str:
    out: List[str] = []
    allowed = [r for r in rows if r["in_d_allowed"]]
    d_all, d_allowed = len(rows), len(allowed)

    def share(tier):
        return sum(1 for r in allowed if r["tier"] == tier
                   and (r["correct"] is not False))

    t1 = sum(1 for r in allowed if r["tier"] == PROVED_TIER1 and r["correct"])
    t2 = sum(1 for r in allowed if r["tier"] == TIER2 and r["correct"])
    t4 = sum(1 for r in allowed if r["tier"] == TIER4)
    ngt = sum(1 for r in allowed if r["tier"] == "no_ground_truth")
    t3 = d_allowed - t1 - t2 - t4 - ngt

    out.append("## Headline\n")
    out.append(f"Denominators: **D_all = {d_all}** (every corpus entry drawn), "
               f"**D_allowed = {d_allowed}** (robots-allowed, login-free, "
               f"read-only — the specified headline denominator, minimum "
               f"{globals().get('MIN_DENOMINATOR', 30)}).\n")
    out.append(f"Levers: **{levers_label}**.\n")

    out.append("| tier | finished | share of D_allowed | Wilson 95% |")
    out.append("|---|---|---|---|")
    for label, n in (("1 — proved compiled request (0 model calls)", t1),
                     ("2 — deterministic browser replay (0 model calls)", t2),
                     ("3 — needs the agent", t3),
                     ("4 — blocked, handed to a human", t4),
                     ("— no ground truth (page did not answer the input)", ngt)):
        p, lo, hi = wilson(n, d_allowed)
        out.append(f"| {label} | {n} | {pct(p)} | [{pct(lo)}, {pct(hi)}] |")
    p, lo, hi = wilson(t1 + t2, d_allowed)
    out.append(f"| **1 + 2 (deterministic, zero model calls)** | **{t1 + t2}** "
               f"| **{pct(p)}** | **[{pct(lo)}, {pct(hi)}]** |")

    out.append("\n## Model calls\n")
    mc1 = sum(r["tier1_model_calls"] for r in allowed)
    mc2 = sum(r["tier2_model_calls"] for r in allowed)
    out.append(f"Tier 1: **{mc1}**. Tier 2: **{mc2}**. "
               + ("Both zero, as required." if mc1 == mc2 == 0 else
                  "**NON-ZERO — this run is unverified; see the specification.**"))

    out.append("\n## Latency per tier (ms)\n")
    out.append("| tier | n | median | p90 |")
    out.append("|---|---|---|---|")
    for label, key, tier in (("1 — compiled request", "tier1_ms", PROVED_TIER1),
                             ("2 — browser replay", "tier2_ms", TIER2)):
        vals = [r[key] for r in allowed if r["tier"] == tier and r[key]]
        med = f"{statistics.median(vals):.0f}" if vals else "-"
        hi = f"{p90(vals):.0f}" if vals else "-"
        out.append(f"| {label} | {len(vals)} | {med} | {hi} |")
    ui = [r.get("ui_ms") for r in allowed if r.get("ui_ms")]
    if ui:
        out.append(f"| (recorded UI page load, for reference) | {len(ui)} | "
                   f"{statistics.median(ui):.0f} | {p90(ui):.0f} |")

    out.append("\n## By class\n")
    out.append("| class | n in D_allowed | tier 1 | tier 2 | tier 3 | tier 4 |")
    out.append("|---|---|---|---|---|---|")
    for cls in ("list", "detail"):
        rs = [r for r in allowed if r["cls"] == cls]
        if not rs:
            continue
        a = sum(1 for r in rs if r["tier"] == PROVED_TIER1 and r["correct"])
        b = sum(1 for r in rs if r["tier"] == TIER2 and r["correct"])
        d = sum(1 for r in rs if r["tier"] == TIER4)
        c = len(rs) - a - b - d
        out.append(f"| {cls} | {len(rs)} | {a} | {b} | {c} | {d} |")

    out.append("\n## By category\n")
    out.append("| category | n | tier 1+2 | share |")
    out.append("|---|---|---|---|")
    by_cat = defaultdict(list)
    for r in allowed:
        by_cat[r["category"]].append(r)
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        n = sum(1 for r in rs if r["tier"] in (PROVED_TIER1, TIER2) and r["correct"])
        out.append(f"| {cat} | {len(rs)} | {n} | {pct(n / len(rs))} |")

    if off_rows is not None:
        out.append("\n## Levers on vs off — the untouched sample\n")
        off = {r["id"]: r for r in off_rows}
        out.append("| metric | levers off | levers on |")
        out.append("|---|---|---|")
        for label, tier in (("tier 1 (proved request)", PROVED_TIER1),
                            ("tier 2 (browser replay)", TIER2)):
            a = sum(1 for r in off_rows if r["in_d_allowed"]
                    and r["tier"] == tier and r["correct"])
            b = sum(1 for r in allowed if r["tier"] == tier and r["correct"])
            out.append(f"| {label} | {a} | {b} |")
        moved = [(r["id"], off[r["id"]]["tier"], r["tier"]) for r in allowed
                 if r["id"] in off and off[r["id"]]["tier"] != r["tier"]]
        if moved:
            out.append("\nWorkflows whose tier changed:\n")
            out.append("| workflow | levers off | levers on |")
            out.append("|---|---|---|")
            for wid, a, b in sorted(moved):
                out.append(f"| {wid} | `{a}` | `{b}` |")
        else:
            out.append("\n**No workflow changed tier.** Reported as it is: on this "
                       "sample the levers had no measured effect on coverage.")

    ru = runner_up_summary(rows)
    out.append("\n## Runner-up binding counter\n")
    out.append("| workflows | offered alternatives | alternatives | rejected | runner-up chosen "
               "| chosen and proved |")
    out.append("|---|---|---|---|---|---|")
    out.append(f"| {ru['workflows']} | {ru['offered']} | {ru['alternatives']} | {ru['rejected']} "
               f"| {ru['chosen']} | {ru['chosen_and_proved']} |")

    out.append("\n## Every workflow\n")
    out.append("| id | site | class | tier | rung | correct | note |")
    out.append("|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (x["cls"], x["category"], x["id"])):
        mark = {True: "yes", False: "NO", None: "-"}[r["correct"]]
        note = (r["reason"] or r["correctness_note"] or "")[:64]
        out.append(f"| {r['id']} | {r['site']} | {r['cls']} | `{r['tier']}` | "
                   f"{r['rung'] or '-'} | {mark} | {note} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--recordings", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--levers", default="on")
    ap.add_argument("--cdp", default="", help="DevTools endpoint for tier 2")
    ap.add_argument("--no-live", action="store_true",
                    help="tier 1 replays against the recorded responses")
    ap.add_argument("--compare-off", default="",
                    help="a rows.json from a --levers off run, for the on/off table")
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--corpus", default="sample2",
                    choices=["sample2", "sample3", "sample4"])
    ap.add_argument("--readiness", default="off", choices=["on", "off"],
                    help="tier 2: evidence-based settle instead of the fixed timer")
    ap.add_argument("--browser-only", default="off", choices=["on", "off"],
                    help="reclassify a 403 on a host the browser was served")
    args = ap.parse_args(argv)

    if args.corpus == "sample4":
        from corpus_sample4 import RESERVE, SAMPLE4 as SAMPLE
        # D_admitted >= 24.
        globals()["MIN_DENOMINATOR"], corpus_label = 24, "blind sample 4"
    elif args.corpus == "sample3":
        from corpus_sample3 import RESERVE, SAMPLE3 as SAMPLE
        # D_admitted >= 24 (``corpus_sample2``'s is 30).
        globals()["MIN_DENOMINATOR"], corpus_label = 24, "blind sample 3"
    else:
        from corpus_sample2 import RESERVE, SAMPLE2 as SAMPLE
        globals()["MIN_DENOMINATOR"], corpus_label = 30, "blind sample 2"
    corpus = SAMPLE + RESERVE
    globals()["READINESS_ON"] = (args.readiness == "on")
    globals()["BROWSER_ONLY_ON"] = (args.browser_only == "on")
    loaded = load(args.recordings, corpus)
    levers = Levers.from_env({"ANYUSE_COMPILE_LEVERS": args.levers})
    replay = ReplayExecutor(endpoint=args.cdp) if args.cdp else None
    live = not args.no_live

    rows = []
    for wf in corpus:
        if wf["id"] not in loaded:
            continue
        if args.only and wf["id"] not in args.only:
            continue
        recs = loaded[wf["id"]]
        row = run_workflow(wf, recs, levers, live, replay, permission_fetch=live)
        base = recs.get("rec") or {}
        row["ui_ms"] = base.get("ui_ms")
        rows.append(row)
        print(f"{row['id']:24s} {row['tier']:18s} {row['rung'] or '-':14s} "
              f"correct={row['correct']} {(row['reason'] or row['correctness_note'])[:60]}")

    off_rows = None
    if args.compare_off and os.path.exists(args.compare_off):
        with open(args.compare_off) as fh:
            off_rows = json.load(fh)

    md = tables(rows, args.levers, off_rows)
    print("\n" + md)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(f"# The ladder on {corpus_label}\n\nGenerated "
                     f"{time.strftime('%Y-%m-%d')} by `execution/web_runtime/ladder.py` from "
                     f"{args.recordings}.\n\n" + md + "\n")
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(rows, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
