"""The ``web_arch4_live`` report: every number from the raw files of
the run folder (the ledger, the state files, the site ledger, the call counter),
written to ``results/summary.json`` and ``results/report.md``. Offline, no request.

    web_arch4_live.py report <R>
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys
import time
from collections import Counter
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from anyuse.execution import demo_oracle as O  # noqa: E402

WAVES = ("immediate", "delayed")
#: the bench's frozen values, the CI seed and the title (``web_arch4b_live`` sets its own)
VALUES_FILE = os.path.join(HERE, "web_arch4_values.json")
CI_SEED = "anyuse-web-arch4-2026-11-03:ci"
TITLE = "Web-arch 4"


def _j(path: str, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return round((c - r) / d, 4), round((c + r) / d, 4)


def layer_of(rec: Dict[str, Any]) -> str:
    """Where a request that is not correct failed (the first layer that did)."""
    f = O.RequestLedger.final(rec)
    if f == "correct":
        return "correct"
    if f == "wrong":
        return "wrong accepted output"
    if f.startswith("not_run"):
        why = str(rec.get("not_run") or "")
        if "invalid demonstration" in why or "no demonstration" in why:
            return "demonstration"
        if "compile failed" in why:
            return "compile"
        if "share" in why:
            return "traffic share"
        if "block" in why:
            return "blocked"
        if "hard stop" in why:
            return "hard stop"
        if "infrastructure" in why:
            return "infrastructure"
        return "not run: " + why[:40]
    if f == "failure":
        if rec.get("tool_status") == "blocked":
            return "blocked"
        if rec.get("tool_status") == "error":
            return "tool error"
        why = str(rec.get("tool_why") or "")
        return "tool check: " + (why.split(":")[0] if ":" in why else why[:30])
    if f == "incomplete":
        return "audit: incomplete"
    if f == "unaudited":
        return "audit: unaudited (" + ("oracle not run" if str(rec.get("oracle") or "").startswith("not run")
                                        else "reader undeterminable") + ")"
    return f


def build(root: str) -> Dict[str, Any]:
    runs = os.path.join(root, "runs")
    led = O.RequestLedger(os.path.join(runs, "ledger.json"))
    data = led.data()
    vals = _j(VALUES_FILE, {}) or {}
    repl = _j(os.path.join(runs, "replacements.json"), {}) or {}
    screen = _j(os.path.join(root, "screen", "screen.json"), {"sites": {}}) or {"sites": {}}
    demos: Dict[str, Any] = {}
    for f in sorted(glob.glob(os.path.join(runs, "state", "demo_*.json"))):
        demos.update((_j(f, {}) or {}).get("entries") or {})
    enrolled = [r.get("in") or s for s, r in [(s, repl.get(s) or {}) for s in vals.get("order") or []]]
    enrolled = [s for s in enrolled if s]
    sites_meta = {s: dict(vals["sites"].get(s) or {}) for s in enrolled}
    for old, r in repl.items():
        if r.get("in") in sites_meta:
            sites_meta[r["in"]].update(demonstrator=vals["sites"][old]["demonstrator"])
    out: Dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "enrolled": len(enrolled)}
    # the screen
    st = screen.get("sites") or {}
    out["screen"] = {"screened": len(st), "by_stratum": dict(Counter(s.get("stratum") for s in st.values())),
                     "eligible": dict(Counter(s.get("stratum") for s in st.values() if s.get("screen") == "eligible")),
                     "outcomes": dict(Counter(f"{s.get('stratum')} {str(s.get('screen')).split(':')[0]}" for s in st.values())),
                     "replacements": repl}
    # the demonstrations
    dem_rows = []
    for s in enrolled:
        d = demos.get(s) or {}
        m = sites_meta.get(s) or {}
        dem_rows.append({"site": s, "demonstrator": m.get("demonstrator"), "workflow": m.get("workflow"),
                         "ran": d.get("status") == "done", "valid": bool(d.get("valid")), "why": d.get("validity") or d.get("why"),
                         "stop": d.get("stop"), "calls": d.get("calls") or 0, "seconds": d.get("seconds"),
                         "cli_seconds": d.get("cli_seconds"), "cost_usd": d.get("cost_usd"), "tokens": d.get("tokens"),
                         "compiled": bool((d.get("compile") or {}).get("ok")) and bool(d.get("valid")),
                         "compile_why": (d.get("compile") or {}).get("why"), "requests": d.get("requests")})
    out["demonstrations"] = dem_rows
    by_src: Dict[str, Any] = {}
    for src in ("claude", "codex"):
        rows = [r for r in dem_rows if r["demonstrator"] == src]
        secs = [r["seconds"] for r in rows if r.get("seconds")]
        by_src[src] = {"sites": len(rows), "valid": sum(r["valid"] for r in rows), "compiled": sum(r["compiled"] for r in rows),
                       "calls": sum(int(r["calls"] or 0) for r in rows),
                       "seconds_median": round(statistics.median(secs), 1) if secs else None,
                       "seconds_total": round(sum(secs), 1), "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
                       "tokens": sum(int(r.get("tokens") or 0) for r in rows)}
    out["demonstrations_by_source"] = by_src
    n_valid = sum(r["valid"] for r in dem_rows)
    n_comp = sum(r["compiled"] for r in dem_rows)
    out["valid_demonstrations"] = {"valid": n_valid, "of": len(enrolled), "wilson": wilson(n_valid, len(enrolled))}
    out["compilation"] = {"compiled": n_comp, "of_valid": n_valid, "of_enrolled": len(enrolled),
                          "failures": dict(Counter(str(r["compile_why"])[:80] for r in dem_rows if r["valid"] and not r["compiled"]))}
    # the waves
    for wave in WAVES:
        reqs = {rid: r for rid, r in data["requests"].items() if r.get("wave") == wave and O.RequestLedger.final(r) != "replaced"}
        if not any(r.get("tool_status") or r.get("not_run") for r in reqs.values()):
            out[wave] = {"ran": False}
            continue
        s = led.summary(wave, seed=CI_SEED)
        per_src = {}
        for src in ("claude", "codex"):
            ss = {x for x in enrolled if (sites_meta.get(x) or {}).get("demonstrator") == src}
            rr = [r for r in reqs.values() if r["site"] in ss]
            per_src[src] = {"correct": sum(O.RequestLedger.final(r) == "correct" for r in rr), "scheduled": len(rr)}
        empty = [r for r in reqs.values() if r.get("empty")]
        tool_ran = [r for r in reqs.values() if r.get("tool_status")]
        http = [r.get("http") for r in reqs.values() if r.get("http")]
        per_site_ok = {}
        for site in enrolled:
            rr = [r for r in reqs.values() if r["site"] == site]
            per_site_ok[site] = bool(rr) and all(O.RequestLedger.final(r) == "correct" for r in rr)
        out[wave] = {"ran": True, "summary": s, "by_source": per_src,
                     "layers": dict(Counter(layer_of(r) for r in reqs.values())),
                     "empty": {"scheduled": len(empty), "final": dict(Counter(O.RequestLedger.final(r).split(":")[0] for r in empty)),
                               "said_no_results": sum(r.get("tool_status") == "no_results" for r in empty)},
                     "nonempty_no_results": [rid for rid, r in reqs.items() if not r.get("empty") and r.get("tool_status") == "no_results"],
                     "tool_runs": len(tool_ran), "model_calls": sum(int(r.get("model_calls") or 0) for r in tool_ran),
                     "fresh_contexts": sum(1 for r in tool_ran if r.get("fresh")),
                     "search_via": dict(Counter(r.get("search_via") for r in tool_ran)),
                     "http": {"requests_with_tool_run": len([r for r in tool_ran if r.get("tool_status") != "blocked"]),
                              "eligible": sum(1 for h in http if h.get("eligible")),
                              "covered": sum(1 for h in http if h.get("covered")), "checked": len(http)},
                     "sites_all_correct": sum(per_site_ok.values()), "sites_all_correct_list": [k for k, v in per_site_ok.items() if v]}
    imm = out.get("immediate") or {}
    dl = out.get("delayed") or {}
    prov = set(imm.get("sites_all_correct_list") or [])
    out["validated_tools"] = {"provisional_immediate_only": len(prov),
                              "validated_both_waves": (len(prov & set(dl.get("sites_all_correct_list") or []))
                                                       if dl.get("ran") else None),
                              "of_valid_demonstrations": n_valid, "of_enrolled": len(enrolled)}
    # traffic, government, the CLIs
    sl = _j(os.path.join(runs, "site_ledger.json"), {}) or {}
    totals = {s: sum(int(v.get("requests", 0)) for v in e.values()) for s, e in sl.items()}
    tbytes = {s: sum(int(v.get("bytes", 0)) for v in e.values()) for s, e in sl.items()}
    viol, hosts = [], Counter()
    for f in glob.glob(os.path.join(runs, "state", "*.json")):
        d = _j(f, {}) or {}
        if d.get("traffic_violation"):
            viol.append(d["traffic_violation"])
        for e in (d.get("entries") or {}).values():
            viol += list(e.get("violations") or [])
            hosts.update(e.get("hosts_sent") or {})
    for s in st.values():
        viol += list(s.get("violations") or [])
        hosts.update(s.get("hosts_sent") or {})
    # a violation's own file (runs/VIOLATION_*.json, written before the stop) - a site that
    # stopped mid-run keeps no state record of it
    vfiles = []
    for f in sorted(glob.glob(os.path.join(runs, "VIOLATION_*.json"))):
        d = _j(f, {}) or {}
        vfiles.append({"file": os.path.basename(f), "site": d.get("site"), "phase": d.get("phase"), "at": d.get("at"),
                       "violations": len(d.get("violations") or []), "first": (d.get("violations") or [""])[0][:200],
                       "raw_events": len(d.get("violation_events") or [])})
        hosts.update(d.get("hosts_sent") or {})
        if not viol:
            viol += list(d.get("violations") or [])[:5]
    from anyuse.execution.gov_hosts import government_host
    gov = sorted(h for h in hosts if government_host(h))
    out["traffic"] = {"sites": len(totals), "max_requests": max(totals.values() or [0]),
                      "max_site": max(totals, key=totals.get) if totals else None,
                      "max_bytes_mb": round(max(tbytes.values() or [0]) / 1e6, 1), "over_300": [s for s, n in totals.items() if n > 300],
                      "over_30mb": [s for s, n in tbytes.items() if n > 30 * 1024 * 1024], "violations": viol[:20],
                      "government_hosts_contacted": gov, "hosts_sent": len(hosts), "violation_files": vfiles}
    out["cli_calls"] = _j(os.path.join(runs, "cli_calls.json"), {}) or {}
    out["hard_stop"] = _j(os.path.join(runs, "HARD_STOP.json"), None)
    out["cerebras_usd"] = 0.0
    return out


def render(s: Dict[str, Any]) -> str:
    L: List[str] = [f"# {TITLE} - generated report", "", f"generated {s['generated_at']}; enrolled sites {s['enrolled']}", ""]
    imm = s.get("immediate") or {}
    if imm.get("ran"):
        sm = imm["summary"]
        L += [f"**Primary (immediate wave): {sm['correct']}/{sm['scheduled']} audited correct replays** "
              f"({100 * sm['rate']:.1f} %, site-bootstrap 95 % CI {100 * sm['ci95_site_bootstrap'][0]:.1f}-"
              f"{100 * sm['ci95_site_bootstrap'][1]:.1f} %); final statuses {sm['counts']}; wrong {sm['wrong_ids']}", ""]
    vd = s["valid_demonstrations"]
    L += [f"Valid demonstrations {vd['valid']}/{vd['of']}; compiled tools {s['compilation']['compiled']} "
          f"(of {s['compilation']['of_valid']} valid, {s['compilation']['of_enrolled']} enrolled); "
          f"validated tools {s['validated_tools']}", ""]
    for k in ("demonstrations_by_source", "compilation", "screen", "traffic", "cli_calls", "hard_stop"):
        L += [f"## {k}", "```", json.dumps(s.get(k), indent=1, ensure_ascii=False)[:6000], "```", ""]
    for w in WAVES:
        x = s.get(w) or {}
        if not x.get("ran"):
            L += [f"## {w}: not run", ""]
            continue
        L += [f"## {w}", "```", json.dumps({k: v for k, v in x.items() if k != "summary"}, indent=1, ensure_ascii=False),
              "```", "", "per site: " + json.dumps(x["summary"]["per_site"]), ""]
    L += ["## demonstrations", "| site | source | workflow | valid | why | calls | s | compiled |", "|---|---|---|---|---|---|---|---|"]
    for r in s["demonstrations"]:
        L.append(f"| {r['site']} | {r['demonstrator']} | {r['workflow']} | {r['valid']} | {str(r['why'])[:60]} | "
                 f"{r['calls']} | {r['seconds']} | {r['compiled']} |")
    return "\n".join(L) + "\n"


def main(root: str) -> int:
    s = build(root)
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    with open(os.path.join(root, "results", "summary.json"), "w") as fh:
        json.dump(s, fh, indent=1, ensure_ascii=False, default=str)
    with open(os.path.join(root, "results", "report.md"), "w") as fh:
        fh.write(render(s))
    imm = (s.get("immediate") or {}).get("summary") or {}
    print(json.dumps({"primary_immediate": [imm.get("correct"), imm.get("scheduled"), imm.get("ci95_site_bootstrap")],
                      "valid_demos": s["valid_demonstrations"], "compiled": s["compilation"]["compiled"],
                      "validated": s["validated_tools"], "hard_stop": s["hard_stop"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
