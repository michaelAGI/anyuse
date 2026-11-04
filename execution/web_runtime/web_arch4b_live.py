"""The web architecture: the rerun of ``web_arch4_live``
("an agent demonstrates a workflow once, and AnyUse compiles it into a checked tool, or
returns an explicit failure") after its hard stop in the screen.

The bench of ``execution/web_runtime/web_arch4_live.py`` runs every phase unchanged in structure; this
module sets its own seed, files and caps on it and adds its own frame:

    web_arch4b_live.py frame  <R>        offline: about 400 fresh hosts (below)
    web_arch4b_live.py screen|cohort|demo|wave|report <R> ...   as web_arch4_live.py

**The frame** (offline, pure given its inputs): ``web_arch4_live``'s frame hosts that it never
contacted (its screen, its site ledger, its DEV sites; the two hosts possibly mid-screen when
its lanes were stopped by hand are excluded too) plus host names written for this bench
(``screen/hosts_raw_4b.tsv``) filtered by REGISTRABLE domain against a new prior-host
snapshot (every host written in the worktree and in every earlier
archive), every host ``web_arch3_live`` and ``web_arch4_live`` contacted, NEVER_CONTACT, the
government rule incl. public bodies and competitor domains; the new hosts are drawn per
stratum (seeded) up to ``FRAME_TARGET``; each stratum is shuffled with
``<SEED>:frame:<stratum>`` and interleaved 3 shops : 2 others; DEV = the last ``N_DEV``
shops and others of that order (removed; never measured).
"""
from __future__ import annotations

import json
import os
import random
import sys
from typing import Any, Dict, List, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import web_arch4_live as A4  # noqa: E402
import web_arch4_report as REP  # noqa: E402
import web_arch_live as L  # noqa: E402

SEED = "anyuse-web-arch4b-2026-11-03"
SPEC_ID = "SPEC_WEB_ARCH4B.md"
LABEL = "web-arch4b"
CANDIDATES = os.path.join(HERE, "web_arch4b_candidates.tsv")
VALUES_FILE = os.path.join(HERE, "web_arch4b_values.json")
ARCH4_ROOT = os.path.join(A4.REAL_HOME, "anyuse-raw-results/web-arch4-2026-11-03")
ARCH4_CANDIDATES = os.path.join(HERE, "web_arch4_candidates.tsv")
PRIOR_CONTACTED_FILE = "contacted_prior.txt"
#: two of ``web_arch4_live``'s screen lanes were stopped by hand (after a third lane's violation); the
#: next host of each lane may have been mid-screen, unrecorded: never contacted again
ARCH4_MAYBE_IN_FLIGHT = ("www.digitalcameraworld.com", "www.spartoo.com")
#: the frame's size per stratum (about 400; shops >= 50 %: ``web_arch4_live`` passed ~6 % of its shops and
#: ~15 % of its other sites)
FRAME_TARGET = {"shop": 270, "other": 150}
#: DEV sites (never measured): the value rules are developed and frozen on them before the protocol is fixed
N_DEV = {"shop": 10, "other": 8}
#: the DEV extension (before the protocol was fixed): none of the first 10 DEV shops was eligible after two
#: screen passes (robots 4, bot walls 2, a third-party search, JS-only results, a search that ignores the
#: query), so the NEXT shops of the tail (the last ``DEV_EXTRA_SHOPS`` of the measured order) become DEV
#: sites too, and as many further new shops of the seeded draw are appended to the measured frame's end
#: (its shop count stays 260; the frame's order is otherwise unchanged)
DEV_EXTRA_SHOPS = 12
#: the SHOP SUPPLEMENT (before the protocol was fixed): 1 of the 22 DEV shops passed the screen
#: (``web_arch4_live``: 9 of 148), so the frame's 260 shops may give fewer than 16 + 4 eligible shops. The new
#: shops the seeded draw left out follow the frame, in the draw's order (source ``shop_supplement``); the screen's
#: extension reaches them only while the shop stratum has fewer than main + 4 eligible sites (others are never
#: taken there)
SHOP_SUPPLEMENT = True
#: the bench's CLI caps: <= 300 demonstration calls (DEV included; ``web_arch4_live``'s 9 DEV calls do not
#: count), <= 30 reader calls per wave. DEV_CALL_CAP bounds DEV's share of the 300 (DEV reader
#: calls count against it too); the measured cap is 300 minus what DEV used (set when the protocol is fixed)
DEV_CALL_CAP = 60
#: DEV used 20 calls (demonstrations 17, reader 3; runs/prev and dev/runs/cli_calls.json): the measured cap
DEMO_CALL_CAP = 280
#: the screen's extension: shops to main + 4; others to main + 4 + 6 (the fill of shop slots if the shops
#: run short, FILL_SHOP_SLOTS_WITH_OTHERS)
EXTENSION_TARGET = {"shop": 16 + 4, "other": 14 + 4 + 6}


def configure() -> None:
    """This bench's seed, files, labels and caps on ``web_arch4_live`` (module globals read at call
    time) and its report."""
    A4.SEED = SEED
    A4.SPEC_ID = SPEC_ID
    A4.BENCH_LABEL = LABEL
    A4.CANDIDATES = CANDIDATES
    A4.VALUES_FILE = VALUES_FILE
    A4.PRIOR_CONTACTED_FILE = PRIOR_CONTACTED_FILE
    A4.DEV_CALL_CAP = DEV_CALL_CAP
    A4.DEMO_CALL_CAP = DEMO_CALL_CAP
    A4.EXTENSION_TARGET = dict(EXTENSION_TARGET)
    A4.FILL_SHOP_SLOTS_WITH_OTHERS = True
    # DEV (never measured): the screen share allows the value rules' re-runs; the site's whole
    # measurement stays <= 300 requests and <= 30 MB
    A4.DEV_SHARES = {"screen": 150, "demo": 60, "immediate": 70, "delayed": 0}
    A4.DEV_BYTES = {"screen": 12 * A4.TR.MB, "demo": 7 * A4.TR.MB, "immediate": 7 * A4.TR.MB, "delayed": 0}
    REP.VALUES_FILE = VALUES_FILE
    REP.CI_SEED = SEED + ":ci"
    REP.TITLE = "Web-arch 4b"


# ---------------------------------------------------------------------------
# the frame
# ---------------------------------------------------------------------------

def _hosts_of_screen(path: str) -> Set[str]:
    out: Set[str] = set()
    for k, v in ((A4.load_json(path, {}) or {}).get("sites") or {}).items():
        out |= {k, str(v.get("site") or ""), str(v.get("frame_host") or ""), str(v.get("canonical_from") or "")}
        out |= set((v.get("hosts_sent") or {}).keys())
    return {h for h in out if h}


def arch4_contacted(r4: str = ARCH4_ROOT) -> Set[str]:
    """Every host ``web_arch4_live`` contacted or may have: its screen records (incl. www/apex variants
    and the hosts its browser sent to), its site ledger, its DEV screen, ledger and sites, the
    hosts of its DEV runs, and the two hosts possibly mid-screen at its stop."""
    out = _hosts_of_screen(os.path.join(r4, "screen", "screen.json"))
    out |= set((A4.load_json(os.path.join(r4, "runs", "site_ledger.json"), {}) or {}).keys())
    out |= _hosts_of_screen(os.path.join(r4, "dev", "screen", "screen.json"))
    out |= set((A4.load_json(os.path.join(r4, "dev", "runs", "site_ledger.json"), {}) or {}).keys())
    out |= {e.get("site") for e in A4.load_json(os.path.join(r4, "screen", "dev_sites.json"), []) or []}
    out |= set(A4.load_json(os.path.join(r4, "analysis", "hosts_sent_screen.json"), []) or [])
    out |= set(ARCH4_MAYBE_IN_FLIGHT)
    return {h for h in out if h}


def frame_from(arch4_frame: List[Tuple[str, str, str]], raw_new: List[Tuple[str, str, str]], contacted: Set[str],
               prior: Set[str], nc: Set[str], gov=None) -> Tuple[List[Dict[str, str]], List[Dict[str, str]],
                                                                 Dict[str, List[str]]]:
    """PURE: (measured frame, DEV sites, dropped). ``contacted``: every host the earlier benches
    contacted; ``prior``: the prior-host snapshot (every host written anywhere)."""
    if gov is None:
        from anyuse.execution.gov_hosts import government_host as gov
    creg = {L.registrable(h) for h in contacted}
    dropped: Dict[str, List[str]] = {}
    rem: Dict[str, List[Dict[str, str]]] = {"shop": [], "other": []}
    seen: Set[str] = set()
    for host, st, cat in arch4_frame:
        h = host.strip().lower()
        why = ""
        if h in contacted or L.registrable(h) in creg:
            why = "contacted_prior"
        elif h in nc:
            why = "never_contact"
        elif gov(h):
            why = "government"
        elif L.registrable(h) in L.COMPETITORS:
            why = "competitor"
        elif L.registrable(h) in seen or st not in rem:
            why = "duplicate"
        if why:
            dropped.setdefault("arch4_" + why, []).append(h)
            continue
        seen.add(L.registrable(h))
        rem[st].append({"site": h, "stratum": st, "category": cat.strip(), "source": "arch4_remainder"})
    old = L.SEED
    try:
        L.SEED = SEED + ":new"
        new, d2 = L.build_frame(raw_new, set(prior) | set(contacted) | {h for h, _s, _c in arch4_frame}, nc, gov=gov)
    finally:
        L.SEED = old
    for k, v in d2.items():
        dropped.setdefault("new_" + k, []).extend(v)
    by: Dict[str, List[Dict[str, str]]] = {"shop": [], "other": []}
    for e in new:
        if L.registrable(e["site"]) in seen:
            dropped.setdefault("new_duplicate_of_remainder", []).append(e["site"])
            continue
        by[e["stratum"]].append(dict(e, source="new_4b"))
    keep: Dict[str, List[Dict[str, str]]] = {}
    extra: List[Dict[str, str]] = []
    supplement: List[Dict[str, str]] = []
    for st in ("shop", "other"):
        want = max(0, FRAME_TARGET[st] - len(rem[st]))
        # build_frame shuffled each stratum with the seed: its first `want` are the draw
        keep[st] = rem[st] + by[st][:want]
        more = DEV_EXTRA_SHOPS if st == "shop" else 0
        extra += [dict(e, source="new_4b_after_dev_extension") for e in by[st][want:want + more]]
        if st == "shop" and SHOP_SUPPLEMENT:
            supplement = [dict(e, source="shop_supplement") for e in by[st][want + more:]]
        else:
            dropped.setdefault("new_not_drawn", []).extend(e["site"] for e in by[st][want + more:])
        random.Random(f"{SEED}:frame:{st}").shuffle(keep[st])
    out: List[Dict[str, str]] = []
    s, o = list(keep["shop"]), list(keep["other"])
    while s or o:
        for _ in range(3):
            if s:
                out.append(s.pop(0))
        for _ in range(2):
            if o:
                out.append(o.pop(0))
    dev = [e for e in out if e["stratum"] == "shop"][-N_DEV["shop"]:] + \
        [e for e in out if e["stratum"] == "other"][-N_DEV["other"]:]
    dev_sites = {e["site"] for e in dev}
    out = [e for e in out if e["site"] not in dev_sites]
    if DEV_EXTRA_SHOPS:
        dev2 = [e for e in out if e["stratum"] == "shop"][-DEV_EXTRA_SHOPS:]
        dev += [dict(e, dev_extension=True) for e in dev2]
        d2 = {e["site"] for e in dev2}
        out = [e for e in out if e["site"] not in d2] + extra
    return out + supplement, dev, dropped


def cmd_frame(root: str) -> int:
    scr = os.path.join(root, "screen")
    raw = [ln.rstrip("\n").split("\t") for ln in open(os.path.join(scr, "hosts_raw_4b.tsv")) if ln.strip()]
    raw = [(r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "") for r in raw]
    r4 = [ln.rstrip("\n").split("\t") for ln in open(ARCH4_CANDIDATES) if ln.strip()]
    r4 = [(r[0], r[1], r[2] if len(r) > 2 else "") for r in r4]
    prior = {ln.strip() for ln in open(os.path.join(scr, "prior_hosts_snapshot.txt")) if ln.strip()}
    c3 = A4.arch3_contacted()
    c4 = arch4_contacted()
    if len(c3) < 77 or len(c4) < 250:
        raise SystemExit(f"refused: prior arch contacted hosts not found ({len(c3)}, {len(c4)})")
    r3_snapshot = {ln.strip() for ln in open(os.path.join(A4.ARCH3_ROOT, "screen", "prior_hosts_snapshot.txt"))
                   if ln.strip()}
    contacted = c3 | c4
    snap = os.path.join(scr, "never_contact_at_frame.txt")
    if not os.path.exists(snap):
        with open(snap, "w") as fh:
            fh.write("\n".join(sorted(L.never())) + "\n")
    nc = {ln.strip() for ln in open(snap) if ln.strip() and not ln.startswith("#")}
    frame, dev, dropped = frame_from(r4, raw, contacted, prior, nc)
    # the screen's own guard list: every host the earlier benches contacted (the first two: ``web_arch3_live``'s
    # prior snapshot, a superset of every name written before it) - refused by registrable domain
    with open(os.path.join(scr, PRIOR_CONTACTED_FILE), "w") as fh:
        fh.write("\n".join(sorted(contacted | r3_snapshot)) + "\n")
    with open(CANDIDATES, "w") as fh:
        for e in frame:
            fh.write(f"{e['site']}\t{e['stratum']}\t{e['category']}\n")
    A4.save_json(os.path.join(scr, "dev_sites.json"), dev)
    A4.save_json(os.path.join(scr, "frame_dropped.json"), dropped)
    A4.save_json(os.path.join(scr, "frame_sources.json"), {e["site"]: e["source"] for e in frame + dev})
    from collections import Counter
    print(json.dumps({"frame": len(frame), "by_stratum": dict(Counter(e["stratum"] for e in frame)),
                      "by_source": dict(Counter(e["source"] for e in frame)), "dev": [e["site"] for e in dev],
                      "contacted_rounds3_4": len(contacted), "dropped": {k: len(v) for k, v in dropped.items()}},
                     indent=1))
    return 0


def main(argv=None) -> int:
    configure()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "frame":
        return cmd_frame(os.path.abspath(argv[1]))
    return A4.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
