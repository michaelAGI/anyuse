"""Build the LOCAL seed file from a discovery bench result. Sends nothing.

    python -m anyuse.registry.seed_build DISCOVERY.json OUT.json [--date YYYY-MM-DD]

Every host goes through the same exporter (:func:`client.export_web`) that a
contributor's ``anyuse map export`` uses, so a seed entry is exactly what a
push would send. The proof summaries are the ones recorded in the bench file.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, Optional

from anyuse.registry import client as C
from anyuse.registry import schema as S


def build_seed(bench: Dict[str, Any], date: Optional[str] = None,
               source: str = "") -> Dict[str, Any]:
    date = date or str(bench.get("generated_at") or "")[:10] or None
    entries, report = [], {}
    for host in sorted(bench.get("hosts") or {}):
        entry, rep = C.export_web(bench["hosts"][host], date=date)
        report[host] = {"exported": [e["kind"] for e in rep["exported"]],
                        "dropped": rep["dropped"], "refused": rep.get("refused", "")}
        if entry is not None:
            S.validate_entry(entry)
            entries.append(entry)
    return {"seed": "anyuse.registry.seed/0", "schema": S.contract()["schema"],
            "source": source, "date": date, "entries": entries, "export_report": report,
            "note": "local seed only; never pushed. Proof summaries as recorded by the "
                    "discovery bench; every entry lands as unproved on a client."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bench_json")
    ap.add_argument("out_json")
    ap.add_argument("--date", default=None)
    ap.add_argument("--source", default="discover_off.json")
    a = ap.parse_args(argv)
    with open(a.bench_json, encoding="utf-8") as fh:
        bench = json.load(fh)
    seed = build_seed(bench, a.date, a.source)
    with open(a.out_json, "w", encoding="utf-8") as fh:
        json.dump(seed, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"{len(seed['entries'])} entries -> {a.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
