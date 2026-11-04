"""The web-architecture report: reachable sites, per-arm
proved + audited correct / reachable (Wilson 95 %), task success, wrong data, answer accuracy, gate
correctness, wrong actions, traffic, government contacts, cost, wall time, compiled repeats, the picker
(text vs text+screenshot, against hand-set gold), the confirmer in shadow, failures by layer, the decision
rule. Reads the RE-JUDGED records (research scripts/rejudge.py).

    web_arch_live.py report <R>     -> <R>/results/report.md, <R>/results/summary.json
"""
from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

import web_arch_live as L
from anyuse.execution import web_arch as W

ARMS = ("baseline", "sonnet_agent", "arch")
PROVIDER_STOPS = ("provider_unreachable", "provider_empty", "model_unreadable", "budget_exhausted")


def _load(p, d=None):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return d


def collect(root: str) -> Dict[str, Any]:
    arms: Dict[str, Dict[str, Any]] = {a: {"entries": {}, "records": {}, "states": []} for a in ARMS}
    for st in glob.glob(os.path.join(root, "runs", "*_[AB]", "state.json")):
        name = os.path.basename(os.path.dirname(st))
        arm = name.rsplit("_", 1)[0]
        if arm not in arms:
            continue
        d = _load(st, {})
        arms[arm]["states"].append({k: d.get(k) for k in ("started_at", "finished_at", "code", "code_now", "half",
                                                          "traffic_violation", "stopped_by_guard", "ledger")})
        for site, e in (d.get("entries") or {}).items():
            arms[arm]["entries"][site] = e
            sdir = os.path.join(os.path.dirname(st), re.sub(r"[^a-z0-9]+", "_", site))
            for f in glob.glob(os.path.join(sdir, "*.json")):
                if os.path.basename(f).startswith("proof_"):
                    continue
                r = _load(f, {})
                if r.get("task"):
                    arms[arm]["records"][(site, r["task"])] = r
    return arms


def pct(k, n):
    return f"{k}/{n} ({100 * k / n:.0f} %)" if n else f"{k}/0"


def ci(k, n):
    lo, hi = L.wilson(k, n)
    return f"[{lo:.0f}–{hi:.0f} %]"


def layer_of(task: Dict[str, Any], r: Dict[str, Any], e: Dict[str, Any], arm: str) -> str:
    """First automatic attribution of a failed run (reviewed by hand in the results)."""
    j = r.get("judge") or {}
    steps = r.get("steps") or []
    blob = json.dumps(steps, ensure_ascii=False)
    fin = str((r.get("final") or {}).get("url") or "")
    if r.get("stop") in PROVIDER_STOPS:
        return "provider"
    if fin.startswith("chrome-error") and int(r.get("requests_after") or 0) >= int(e.get("share") or 999) - 2:
        return "traffic share (the page's own requests used the arm's share)"
    if arm != "arch":
        if r.get("refused") and task.get("kind") != "gate":
            return "agent: abstained on a read-only task"
        if j.get("forbidden"):
            return "agent: forbidden attempt"
        return "agent: " + str(j.get("answer_verdict") or r.get("stop") or "wrong end state")
    if r.get("stop") == "refused" or (r.get("gate") and task.get("kind") != "gate"):
        return "planner (refused / gated a read-only task)"
    if "picker hand-off" in blob:
        return "picker (hand-off)"
    if "no candidates" in blob or "no link for the rule" in blob:
        return "map (no candidate)"
    if j.get("answer_verdict") == "no_answer" and "abstained" in str(j.get("why") or ""):
        return "planner/map (reached a page without the answer; abstained)"
    if j.get("answer_verdict") == "wrong":
        return "extract/answer (wrong answer)"
    if "search changed nothing" in blob or "click failed" in blob:
        return "executor"
    return "planner (wrong end state)"


def main(root: str) -> int:
    tasks = _load(os.path.join(L.HERE, "web_arch_tasks.json"), {}).get("sites") or {}
    cohort = _load(os.path.join(root, "screen", "cohort.json"), {})
    screen = _load(os.path.join(root, "screen", "screen.json"), {})
    audit = _load(os.path.join(root, "runs", "audit.json"), {}) or {}
    gold = {g["q"]: g for g in (_load(os.path.join(root, "results", "picker_gold.json"), {}) or {}).get("Q") or []}
    arms = collect(root)
    ledger = _load(os.path.join(root, "ledger.json"), {})
    site_ledger = _load(os.path.join(root, "runs", "site_ledger.json"), {})
    # -- the sites --------------------------------------------------------------------
    sites_run = sorted(set().union(*[set(a["entries"]) for a in arms.values()]))
    blocked_by: Dict[str, Tuple[str, str]] = {}
    for arm in ARMS:
        for s, e in arms[arm]["entries"].items():
            if e.get("outcome") == "blocked" and s not in blocked_by:
                blocked_by[s] = (arm, str(e.get("blocked"))[:120])
    infra = {s for s, e in arms["baseline"]["entries"].items() if e.get("outcome") == "infrastructure"}
    reachable = [s for s in sites_run if s not in blocked_by and s not in infra and s in tasks
                 and not L.refusal(s)]
    summary: Dict[str, Any] = {"reachable": reachable, "blocked": blocked_by, "infrastructure": sorted(infra),
                               "arms": {}}
    lines = ["# Web architecture — report (generated from the re-judged records)", ""]
    sc = Counter((s.get("stratum"), s.get("screen")) for s in (screen.get("sites") or {}).values())
    lines += ["## Screen", "", "| stratum | outcome | n |", "|---|---|---|"]
    lines += [f"| {k[0]} | {k[1]} | {v} |" for k, v in sorted(sc.items(), key=lambda x: (str(x[0][0]), str(x[0][1])))]
    shops_reach = sum(1 for s in reachable if (tasks.get(s) or {}).get("stratum") == "shop")
    lines += ["", f"Screened {len(screen.get('sites') or {})}; cohort main {len(cohort.get('order') or [])}; sites "
                  f"run {len(sites_run)}; **reachable eligible {len(reachable)}** ({shops_reach} shops); blocked "
                  f"{len(blocked_by)} ({', '.join(f'{s} [{a}]' for s, (a, _w) in sorted(blocked_by.items()))}); "
                  f"infrastructure {len(infra)} ({', '.join(sorted(infra))}).", ""]
    # -- per arm ------------------------------------------------------------------------
    rows, layers = [], {}
    for arm in ARMS:
        A = arms[arm]
        m: Dict[str, Any] = defaultdict(int)
        by_kind: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        proved_sites, proved_audited, tier1, item_tools, invalid_proofs = [], [], 0, 0, []
        cost, secs, calls = 0.0, 0.0, 0
        n_sites, incomplete, notrun = 0, [], []
        lay: Counter = Counter()
        for s in reachable:
            e = A["entries"].get(s)
            if not e or e.get("outcome") in ("skipped",):
                notrun.append(s)
                continue
            if e.get("status") != "done" or e.get("outcome") == "infrastructure":
                incomplete.append(s)
                continue
            site_provider = False
            ok_tasks = set()
            for t in tasks[s]["tasks"]:
                r = A["records"].get((s, t["id"]))
                row = next((x for x in e.get("tasks") or [] if x.get("task") == t["id"]), None)
                if r is None or row is None:
                    m["tasks_not_run"] += 1
                    if row is not None and "share" in str(row.get("outcome")):
                        m["tasks_share_exhausted"] += 1
                    continue
                if r.get("stop") in PROVIDER_STOPS:
                    m["tasks_provider"] += 1
                    site_provider = True
                    continue
                j = r.get("judge") or {}
                m["tasks"] += 1
                ok = j.get("verdict") == "ok"
                if ok:
                    ok_tasks.add(t["id"])
                m["ok"] += int(ok)
                by_kind[t["kind"]][0] += int(ok)
                by_kind[t["kind"]][1] += 1
                m["wrong_answers"] += int(j.get("wrong_data") or 0)
                m["forbidden"] += int(j.get("forbidden") or 0)
                if t["kind"] == "answer_question":
                    m["aq"] += 1
                    ans = str(r.get("answer") or "").strip()
                    m["aq_answered"] += int(bool(ans) and not W.abstains(ans))
                    m["aq_correct"] += int(j.get("answer_verdict") == "correct")
                if t["kind"] == "gate":
                    m["gate"] += 1
                    m["gate_ok"] += int(ok)
                if not ok:
                    lay[layer_of(t, r, e, arm)] += 1
                cost += float(r.get("cost") or 0.0)
                secs += float(r.get("seconds_wall") or r.get("seconds") or 0.0)
                calls += int(r.get("model_calls") or 0)
            if site_provider:
                incomplete.append(s)
            else:
                n_sites += 1
            p = e.get("proof") or {}
            if p.get("status") == "proved":
                if p.get("task") not in ok_tasks:
                    invalid_proofs.append(s)
                else:
                    proved_sites.append(s)
                    keys = [k for k in audit if k.startswith(f"{arm}|{s}|")]
                    wrong = sum(int((audit.get(k) or {}).get("wrong") or 0) for k in keys)
                    m["audit_wrong_rows"] += wrong
                    if keys and wrong == 0 and all("error" not in (audit.get(k) or {}) for k in keys):
                        proved_audited.append(s)
                    tier1 += int(p.get("tier1") == "proved")
                    item_tools += int(p.get("kind") == "item")
            for c in e.get("compiled") or []:
                if c.get("task") not in ok_tasks:
                    m["compiled_of_failed_runs"] += int("judge" in c)
                    continue
                if "judge" in c:
                    m["compiled"] += 1
                    cok = (c["judge"].get("verdict") == "ok" and not c.get("model_calls")
                           and not W.abstains(c.get("answer") or ""))
                    m["compiled_ok"] += int(cok)
                    m["compiled_calls"] += int(c.get("model_calls") or 0)
                else:
                    m["compiled_not_run"] += 1
        k = len(proved_audited)
        m["wrong_data"] = m["wrong_answers"] + m["audit_wrong_rows"]
        m["wrong_actions"] = m["forbidden"] + m["wrong_answers"]
        summary["arms"][arm] = dict(m, proved=proved_sites, proved_audited=proved_audited, n_sites=n_sites,
                                    incomplete_sites=incomplete, not_run_sites=notrun, invalid_proofs=invalid_proofs,
                                    tier1=tier1, item_tools=item_tools, cost=round(cost, 4), seconds=round(secs, 1),
                                    model_calls=calls, by_kind={kk: list(v) for kk, v in by_kind.items()},
                                    layers=dict(lay))
        layers[arm] = lay
        rows.append(f"| {arm} | **{k}/{n_sites}** {ci(k, n_sites)} | {len(proved_sites)} (tier 1 {tier1}, item {item_tools}; "
                    f"invalid {len(invalid_proofs)}) | {pct(m['ok'], m['tasks'])} {ci(m['ok'], m['tasks'])} | "
                    f"{m['wrong_data']} ({m['wrong_answers']} answers, {m['audit_wrong_rows']} rows) | "
                    f"{m['aq_correct']}/{m['aq']} (answered {m['aq_answered']}) | {m['gate_ok']}/{m['gate']} | "
                    f"{m['wrong_actions']} ({m['forbidden']} forbidden) | {calls} | {cost:.3f} | {secs / 60:.1f} | "
                    f"{len(incomplete)} / {m['tasks_provider']} / {m['tasks_not_run']} |")
    lines += ["## Arms (reachable eligible sites)", "",
              "| arm | proved + audited / sites | proved (tier 1, item tools; proofs on a run re-judged failed) | "
              "task success | wrong data | answer accuracy (correct / asked) | gate correct | wrong actions | "
              "model calls | cost US$ | minutes (tasks) | incomplete sites / provider-failed tasks / tasks not run |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"] + rows + [""]
    for arm in ARMS:
        a = summary["arms"][arm]
        lines.append(f"* {arm} by kind: " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(a["by_kind"].items())))
    a = summary["arms"]["arch"]
    lines += ["", f"Compiled repeats (arch, of runs that are ok after re-judging): {a.get('compiled_ok', 0)}/"
                  f"{a.get('compiled', 0)} succeeded, {a.get('compiled_calls', 0)} model calls; not run for the share: "
                  f"{a.get('compiled_not_run', 0)}; repeats of runs re-judged failed (not counted): "
                  f"{a.get('compiled_of_failed_runs', 0)}.", ""]
    lines += ["## Failures by layer (automatic first pass)", ""]
    for arm in ARMS:
        lines.append(f"* {arm}: " + "; ".join(f"{k} {v}" for k, v in layers[arm].most_common()))
    lines.append("")
    # -- traffic --------------------------------------------------------------------------
    tot = defaultdict(int)
    for s, e in (site_ledger or {}).items():
        tot[s] += sum(int(v.get("requests") or 0) for v in e.values())
    for s, v in (screen.get("sites") or {}).items():
        tot[s] += int(v.get("requests") or 0)
    by = {s: sum(int(v.get("bytes") or 0) for v in e.values()) for s, e in (site_ledger or {}).items()}
    viol = [st.get("traffic_violation") for a_ in ARMS for st in arms[a_]["states"] if st.get("traffic_violation")]
    viol += [v for a_ in ARMS for e in arms[a_]["entries"].values() for v in (e.get("violations") or [])]
    gaps = [e.get("min_gap_documents_s") for a_ in ARMS for e in arms[a_]["entries"].values()
            if e.get("min_gap_documents_s") is not None]
    peaks = [e.get("static_peak") for a_ in ARMS for e in arms[a_]["entries"].values() if e.get("static_peak")]
    gov = [s for s in tot if L.refusal(s).startswith("skipped: government")]
    cap_hit = {a_: sorted(s for s, e in arms[a_]["entries"].items()
                          if int(e.get("requests") or 0) >= int(e.get("share") or 10 ** 9) - 1) for a_ in ARMS}
    v5_blocks = Counter()
    for a_ in ARMS:
        for r in arms[a_]["records"].values():
            if r.get("block_strict_rule") and (r.get("judge") or {}).get("verdict") != "blocked":
                v5_blocks[a_] += 1
    summary["traffic"] = {"max_site_requests": max(tot.values() or [0]), "max_site_bytes": max(by.values() or [0]),
                          "violations": viol, "over_300": [s for s, v in tot.items() if v > 300],
                          "over_30mb": [s for s, v in by.items() if v > 30 * 1024 * 1024],
                          "min_doc_gap_s": min(gaps) if gaps else None, "static_peak": max(peaks) if peaks else None,
                          "gov_contacts": gov, "share_used_up": cap_hit, "v5_rule_blocks": dict(v5_blocks)}
    lines += ["## Traffic", "", f"Max requests on one site (screen + every arm + DEV): "
              f"{summary['traffic']['max_site_requests']}; max bytes {summary['traffic']['max_site_bytes'] / 1e6:.1f} MB; "
              f"sites over 300: {summary['traffic']['over_300']}; over 30 MB: {summary['traffic']['over_30mb']}; "
              f"traffic violations: {len(viol)}; shortest gap between documents {summary['traffic']['min_doc_gap_s']} s; "
              f"statics in flight at most {summary['traffic']['static_peak']}; government contacts: {len(gov)}. "
              f"The arm's share used up on a site: " + "; ".join(f"{a_} {len(v)} ({', '.join(v)})" for a_, v in cap_hit.items())
              + f". Runs strict block check would have filed as blocked: "
              f"{dict(v5_blocks)}.", ""]
    # -- picker and confirmer -----------------------------------------------------------------
    picks, conf = [], []
    for key in sorted(arms["arch"]["records"]):
        r = arms["arch"]["records"][key]
        for p in r.get("picks") or []:
            picks.append(p)
        for c in r.get("confirmer") or []:
            conf.append(dict(c, verdict=(r.get("judge") or {}).get("verdict")))
    # the gold labels were set in the order of the sorted record files (results/picker_gold.json)
    picks2 = []
    for f in sorted(glob.glob(os.path.join(root, "runs", "arch_*", "*", "*.json"))):
        if os.path.basename(f).startswith("proof_"):
            continue
        for p in (_load(f, {}) or {}).get("picks") or []:
            picks2.append(p)
    pk: Dict[str, Counter] = {"text": Counter(), "image": Counter()}
    for qi, p in enumerate(picks2):
        g = gold.get(qi)
        if g is None:
            continue
        for mode, d in (("text", p), ("image", p.get("image") or {})):
            if mode == "image" and not d:
                pk[mode]["not asked"] += 1
                continue
            idx = d.get("index")
            if idx is None:
                pk[mode]["hand-off, a right option was listed" if g["ok"] else "hand-off, none listed (right)"] += 1
            else:
                pk[mode]["accepted, right" if idx in g["ok"] else "accepted, WRONG"] += 1
    same = sum(1 for p in picks2 if p.get("image") and p.get("index") == p["image"].get("index"))
    summary["picker"] = {"questions": len(picks2), "text": dict(pk["text"]), "image": dict(pk["image"]),
                         "same_decision": same}
    lines += ["## Picker (arch; text acted, text+screenshot logged)", "",
              f"{len(picks2)} questions; the same decision in {same}.", "",
              "| outcome | text | text+screenshot |", "|---|---|---|"]
    for k_ in sorted(set(pk["text"]) | set(pk["image"])):
        lines.append(f"| {k_} | {pk['text'].get(k_, 0)} | {pk['image'].get(k_, 0)} |")
    cf: Counter = Counter()
    for c in conf:
        for mode in ("text", "image"):
            d = c.get(mode) or {}
            if d.get("error") or d.get("veto") is None:
                cf[(c["kind"], mode, "no opinion", c["verdict"])] += 1
            else:
                cf[(c["kind"], mode, "veto" if d["veto"] else "pass", c["verdict"])] += 1
    summary["confirmer"] = {str(k_): v for k_, v in cf.items()}
    lines += ["", "## Confirmer (shadow) against the run's verdict", "",
              "| kind | mode | vetoes on failed runs (recall) | vetoes on ok runs (false veto) | no opinion |",
              "|---|---|---|---|---|"]
    for kind in ("result_page", "wrong_page"):
        for mode in ("text", "image"):
            vf = cf[(kind, mode, "veto", "fail")]
            nf = vf + cf[(kind, mode, "pass", "fail")]
            vo = cf[(kind, mode, "veto", "ok")]
            no = vo + cf[(kind, mode, "pass", "ok")]
            na = sum(v for k_, v in cf.items() if k_[0] == kind and k_[1] == mode and k_[2] == "no opinion")
            lines.append(f"| {kind} | {mode} | {vf}/{nf} | {vo}/{no} | {na} |")
    lines.append("")
    # -- the code's goal detector on the authors' gate tasks -------------------------------------
    pool = [t for f in sorted(glob.glob(os.path.join(root, "authoring", "tasks_*.json")))
            for t in (_load(f, {}) or {}).get("tasks") or []]
    gates = [t for t in pool if t.get("kind") == "gate"]
    hit = sum(1 for t in gates if W.commit_intent(t.get("utterance") or ""))
    ro = [t for t in pool if t.get("kind") in ("answer_question", "search", "open_item", "navigate_in_site",
                                               "filter_sort", "browse")]
    fp = sum(1 for t in ro if W.commit_intent(t.get("utterance") or ""))
    summary["goal_detector"] = {"gate_recall": [hit, len(gates)], "read_only_false_gates": [fp, len(ro)]}
    lines += ["## The code's goal detector on the authors' tasks", "",
              f"Gate tasks it gates: {hit}/{len(gates)}; read-only tasks it would gate: {fp}/{len(ro)}.", ""]
    # -- spend ---------------------------------------------------------------------------------
    summary["ledger"] = ledger
    lines += ["## Spend", "", "```", json.dumps(ledger, indent=1), "```", ""]
    # -- decision -------------------------------------------------------------------------------
    a, b = summary["arms"]["arch"], summary["arms"]["baseline"]
    n, k = a["n_sites"], len(a["proved_audited"])
    ntraffic = len(viol) + len(summary["traffic"]["over_300"]) + len(summary["traffic"]["over_30mb"])
    dec = {"sites": (k, n, bool(n) and k / n >= 0.5 and a["audit_wrong_rows"] == 0),
           "wrong_actions": (a["wrong_actions"], b["wrong_actions"], a["wrong_actions"] <= b["wrong_actions"]),
           "traffic": ntraffic == 0, "gov": len(gov) == 0,
           "compiled": (a.get("compiled_ok", 0), a.get("compiled", 0),
                        a.get("compiled", 0) > 0 and a.get("compiled_ok", 0) / a.get("compiled", 1) >= 0.9)}
    summary["decision"] = dec
    met = dec["sites"][2] and dec["wrong_actions"][2] and dec["traffic"] and dec["gov"] and dec["compiled"][2]
    summary["met"] = bool(met)
    lines += ["## Decision rule", "",
              f"1. proved + audited ≥ 50 % of reachable sites: {k}/{n} → {'held' if dec['sites'][2] else 'NOT held'}",
              f"2. wrong actions arch {a['wrong_actions']} ≤ baseline {b['wrong_actions']} → "
              f"{'held' if dec['wrong_actions'][2] else 'NOT held'}",
              f"3. 0 traffic violations → {'held' if dec['traffic'] else 'NOT held'} ({ntraffic})",
              f"4. 0 government contacts → {'held' if dec['gov'] else 'NOT held'}",
              f"5. compiled repeats ≥ 90 % with 0 calls: {dec['compiled'][0]}/{dec['compiled'][1]} → "
              f"{'held' if dec['compiled'][2] else 'NOT held'}", "",
              f"**Decision: {'MET' if met else 'NOT MET'}**", ""]
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    open(os.path.join(root, "results", "report.md"), "w").write("\n".join(lines))
    json.dump(summary, open(os.path.join(root, "results", "summary.json"), "w"), indent=1, default=str)
    print("\n".join(lines))
    return 0
