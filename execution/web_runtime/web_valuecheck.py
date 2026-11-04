"""The web value check: OpenJev as the VALUE CHECK in the screen, its offline
calibration on values the earlier web-architecture benches already drew, a DEV live check, and ``watch --explain``
on OpenJev.

    web_valuecheck.py build   <R>                     offline: the candidate set from the benches' records
    web_valuecheck.py split   <R>                     offline: labels + the seeded SELECTION/CHECK split -> SET_FILE
    web_valuecheck.py ask     <R> [--url U] [--workers N]   both orders for every value (OpenJev, loopback only)
    web_valuecheck.py tune    <R>                     t on SELECTION only (the rule in TUNE_RULE)
    web_valuecheck.py check   <R>                     opens CHECK ONCE with the tuned t (refuses a second time)
    web_valuecheck.py freeze  <R>                     writes t to execution/web_valuecheck.json
    web_valuecheck.py dev     <R> --browser URL [--url U]   the DEV live screen with value_check="openjev"
    web_valuecheck.py devreport <R>                   offline: the DEV values drawn vs what the rules would have drawn
    web_valuecheck.py explain <R> [--url U]           watch --explain on synthetic diffs (no site contact)

The screen hook: ``execution/web_runtime/web_arch4_live.py`` has ``VALUE_CHECK = "rules"`` (default: the frozen value
rules, byte-identical in behaviour) and ``VALUE_CHECKER``; with ``"openjev"`` every candidate the rules'
draw yields - in the rules' own seeded order - is asked ``execution/value_check.py``'s typed question and
kept only on its accept rule; a rejected candidate is replaced by the next of that order
(:func:`checked_values`). The rules' search check (a result names >= 75 % of the value's words, no "no
results" statement) then runs on the kept candidates exactly as before.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SEED = "anyuse-web-valuecheck-2026-11-03"
SET_FILE = os.path.join(HERE, "web_valuecheck_set.json")
LOOPBACK = "http://127.0.0.1:18080"


def _a4():
    import web_arch4_live as A4
    return A4


def _vc():
    from anyuse.execution import value_check as VC
    return VC


# ---------------------------------------------------------------------------
# 1. the screen hook: the rules' draw order, and the draw with the check
# ---------------------------------------------------------------------------

def product_pool(titles: Sequence[str], site: str, first_n: int = 12) -> List[Tuple[str, int]]:
    """The shops' candidate pool in the rules' own order - ``web_arch4_live.product_values`` before its
    word-subset filter: (candidate, index of its title), the first ``first_n`` usable titles, shuffled by
    the bench's seed. ``product_values(k)`` = the first k of this pool that are no word-subset of an earlier
    kept one (asserted by the tests)."""
    A4 = _a4()
    W = A4.W
    pool: List[Tuple[str, int]] = []
    seen = set()
    for i, t in enumerate(titles):
        c = A4.short_name(A4.clean_title(t))
        toks = W.content_tokens(c)
        if not (2 <= len(c.split()) <= 10) or len(toks) < 2 or W.forbidden_control(c, "") or A4.chrome_value(c):
            continue
        key = frozenset(toks)
        if key in seen:
            continue
        seen.add(key)
        pool.append((c, i))
        if len(pool) >= first_n:
            break
    rng = random.Random(f"{A4.SEED}:values:{site}")
    rng.shuffle(pool)
    return pool


def _subset_conflict(c: str, kept: Sequence[str]) -> bool:
    W = _a4().W
    s = set(W.content_tokens(c))
    return any(s <= set(W.content_tokens(o)) or set(W.content_tokens(o)) <= s for o in kept)


def word_pool(titles: Sequence[str], site: str, seed: str, top: int = 10) -> List[str]:
    """Other sites: the rules' own order - ``word_values`` with k = ``top`` (the seeded shuffle of the
    ``top`` most shared words); ``word_values(k)`` is its first k."""
    return _a4().word_values(titles, site, seed, k=top, top=top)


def _titles_naming(word: str, rows: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    W = _a4().W
    w = W.fold(word)
    return [(t, u) for t, u in rows if w in {W.fold(x) for x in re.findall(r"[^\W\d_][^\W_]*", str(t))}]


def candidate_of(stratum: str, value: str, rows: Sequence[Tuple[str, str]], site: str, read: Dict[str, Any],
                 index: Optional[int] = None):
    """The value check's :class:`Candidate` for a value the screen drew from ``rows`` (the result page's
    (title, url) rows in page order): shops - the tile's title, its link path, its neighbour tiles; other
    sites - a word shared by result links: the first naming link's path, the naming titles as neighbours."""
    VC = _vc()
    lang = VC.page_language(str(read.get("dom") or ""), site, str(read.get("main_text") or read.get("text") or ""))
    if stratum == "shop":
        texts = [t for t, _u in rows]
        i = index if index is not None else next((j for j, t in enumerate(texts) if t == value), -1)
        url = rows[i][1] if 0 <= i < len(rows) else ""
        return VC.Candidate(value=value, source="tile", path=VC.link_path(url), lang=lang,
                            neighbours=VC.neighbours_of(texts, i) if i >= 0 else texts[:VC.MAX_NEIGHBOURS], site=site)
    naming = _titles_naming(value, rows)
    return VC.Candidate(value=value, source="link", path=VC.link_path(naming[0][1]) if naming else "", lang=lang,
                        neighbours=[t for t, _u in naming][:VC.MAX_NEIGHBOURS], site=site)


def checked_values(stratum: str, rows: Sequence[Tuple[str, str]], site: str, seed: str, read: Dict[str, Any],
                   rules_values: Sequence[str], checker: Any, k: int) -> Tuple[List[str], Dict[str, Any]]:
    """``value_check="openjev"``: the rules' candidates in the rules' own order, each asked the value check;
    up to ``k`` kept; a rejected candidate is replaced by the next of that order (for shops the word-subset
    filter runs against the KEPT values only). Returns (kept values, what the try record adds)."""
    if checker is None:
        raise RuntimeError('value_check="openjev" needs a VALUE_CHECKER (execution.value_check.ValueChecker)')
    kept: List[str] = []
    asked: List[Dict[str, Any]] = []
    if stratum == "shop":
        order = product_pool([t for t, _u in rows], site)
        for c, i in order:
            if len(kept) >= k:
                break
            if _subset_conflict(c, kept):
                continue
            v = checker.check(candidate_of(stratum, c, rows, site, read, index=i))
            asked.append({"value": c, "keep": v.keep, "by": v.by, "reason": v.reason, "fallback": v.fallback,
                          "answers": v.answers})
            if v.keep:
                kept.append(c)
    else:
        for c in word_pool([t for t, _u in rows], site, seed):
            if len(kept) >= k:
                break
            v = checker.check(candidate_of(stratum, c, rows, site, read))
            asked.append({"value": c, "keep": v.keep, "by": v.by, "reason": v.reason, "fallback": v.fallback,
                          "answers": v.answers})
            if v.keep:
                kept.append(c)
    return kept, {"value_check": "openjev", "rules_values": list(rules_values), "checked": asked,
                  "fallbacks": sum(1 for a in asked if a["fallback"])}


# ---------------------------------------------------------------------------
# 2. the labelled calibration set: values the earlier benches ALREADY drew (offline)
# ---------------------------------------------------------------------------

REAL_HOME = os.environ.get("ANYUSE_REAL_HOME") or os.path.expanduser("~")
RESEARCH = os.path.join(REAL_HOME, "Desktop", "analysis", "research")
#: the benches' records read (never written): (bench, phase, screen file, dumps folder or "")
SOURCES = (
    ("r4b", "screen", "web-arch4b-2026-11-03/screen/screen.json", ""),
    ("r4b", "dev", "web-arch4b-2026-11-03/dev/screen/screen.json", "web-arch4b-2026-11-03/dev/dumps"),
    ("r4b", "dev_pass1", "web-arch4b-2026-11-03/dev/screen/screen_pass1.json", "web-arch4b-2026-11-03/dev/dumps_pass1"),
    ("r4", "screen", "web-arch4-2026-11-03/screen/screen.json", ""),
    ("r4", "dev", "web-arch4-2026-11-03/dev/screen/screen.json", ""),
)
R3_DRAW = "web-arch3-2026-11-03/authoring/review_and_draw.json"
R3_SCREEN = "web-arch3-2026-11-03/screen/screen.json"
R4B_COHORT = "web-arch4b-2026-11-03/screen/cohort.json"
LABELS = ("product", "brand_category", "ui", "number", "none")


def _load(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _fold(s: Any) -> str:
    return _a4().W.fold(s)


def _words(s: Any) -> List[str]:
    return [_fold(w) for w in re.findall(r"[^\W\d_][^\W_]*", str(s or ""))]


def _dump_reads(folder: str) -> Dict[str, Dict[str, Any]]:
    """A DEV dump folder's seed renders: {seed term: the page read} (the reads keep elements and the DOM)."""
    import gzip
    out: Dict[str, Dict[str, Any]] = {}
    if not folder or not os.path.isdir(folder):
        return out
    for f in sorted(os.listdir(folder)):
        if "_seed_" not in f:
            continue
        try:
            with gzip.open(os.path.join(folder, f), "rt", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        out.setdefault(str(d.get("term") or ""), d)
    return out


def _href_naming(read: Dict[str, Any], value: str) -> str:
    """The first body link whose text contains the value (folded), else whose words hold all its words."""
    fv, wv = _fold(value), set(_words(value))
    best = ""
    for e in read.get("elements") or []:
        if e.get("tag") != "a" or not e.get("href") or e.get("area") in ("header", "nav", "footer"):
            continue
        ft = _fold(e.get("text"))
        if fv and fv in ft:
            return str(e["href"])
        if not best and wv and wv <= set(_words(e.get("text"))):
            best = str(e["href"])
    return best


def _title_index(value: str, titles: Sequence[str]) -> int:
    """The index of the title a shop value was cut from (the rules' own cleaning), else of the first title
    containing it; -1 when none."""
    A4 = _a4()
    for i, t in enumerate(titles):
        if A4.short_name(A4.clean_title(t)) == value or t == value:
            return i
    fv = _fold(value)
    for i, t in enumerate(titles):
        if fv and fv in _fold(t):
            return i
    return -1


def _snippet_text(rec: Dict[str, Any]) -> str:
    sn = rec.get("snippets") or {}
    return " ".join([str(sn.get("title") or "")] + [str(x) for x in sn.get("links") or []] + [str(sn.get("text") or "")])


def _rules_check(rec: Dict[str, Any], value: str) -> Optional[str]:
    """The rules' own search check of this value in that screen ("kept" / "rejected"), None if not checked."""
    for c in rec.get("value_checks") or []:
        if c.get("value") == value:
            return "kept" if (c.get("naming") or 0) > 0 and not c.get("no_results") else "rejected"
    return None


def items_of_screen(round_: str, phase: str, sites: Dict[str, Any], reads_by_site: Dict[str, Dict[str, Dict[str, Any]]]
                    ) -> List[Dict[str, Any]]:
    """PURE given its inputs: one item per (site, drawn value) of a screen file, with the value check's
    context: where it was drawn, its link path (from the DEV dumps; else not recorded), the page language,
    2-3 neighbouring texts; the rules' search check where it ran; whether it became a measured value."""
    VC = _vc()
    out: List[Dict[str, Any]] = []
    for site, rec in sites.items():
        final = rec.get("values") or {}
        used = [final.get("demo")] + list(final.get("held_out") or [])[:2] if final else []
        text = _snippet_text(rec)
        reads = reads_by_site.get(site) or {}
        for tr in rec.get("value_tries") or []:
            titles = [str(t) for t in tr.get("titles") or []]
            read = reads.get(str(tr.get("seed") or ""))
            for v in tr.get("values") or []:
                st = rec.get("stratum")
                if st == "shop":
                    i = _title_index(v, titles)
                    nb = VC.neighbours_of(titles, i) if i >= 0 else titles[:VC.MAX_NEIGHBOURS]
                    src = "tile"
                else:
                    wv = _fold(v)
                    nb = [t for t in titles if wv in _words(t)][:VC.MAX_NEIGHBOURS]
                    src = "link"
                path = VC.link_path(_href_naming(read, v)) if read else None
                lang = VC.page_language(str((read or {}).get("dom") or ""), site, text)
                out.append({"site": site, "round": round_, "phase": phase, "stratum": st, "seed": tr.get("seed"),
                            "value": v, "source": src, "path": path, "lang": lang, "neighbours": nb,
                            "rules_check": _rules_check(rec, v), "round_value": v in used})
    return out


def items_of_arch3(draw: Dict[str, Any], screen: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``web_arch3_live``'s drawn proof values (single words of the start page's item titles): the start page's
    link texts naming the word are the neighbours; no path or search check was recorded."""
    VC = _vc()
    out: List[Dict[str, Any]] = []
    for site, rec in (draw.get("sites") or {}).items():
        srec = (screen.get("sites") or {}).get(site) or {}
        links = [str(x) for x in ((srec.get("snippets") or {}).get("links") or [])]
        text = _snippet_text(srec)
        for v in rec.get("values") or []:
            wv = _fold(v)
            nb = [t for t in links if wv in _words(t)][:VC.MAX_NEIGHBOURS]
            out.append({"site": site, "round": "r3", "phase": "cohort", "stratum": rec.get("stratum"), "seed": "",
                        "value": v, "source": "start_link", "path": None, "lang": VC.page_language("", site, text),
                        "neighbours": nb, "rules_check": None, "round_value": True})
    return out


def build_items(research: str = RESEARCH) -> List[Dict[str, Any]]:
    """Every value the screens of the earlier benches drew (reruns reused ``web_arch4b_live``'s cohort values), one item
    per (site, value); ids ``v0001...`` in a stable order (site, value)."""
    items: List[Dict[str, Any]] = []
    for round_, phase, f, dumps in SOURCES:
        sites = (_load(os.path.join(research, f), {}) or {}).get("sites") or {}
        reads = {}
        if dumps:
            for site in sites:
                reads[site] = _dump_reads(os.path.join(research, dumps, re.sub(r"[^a-z0-9]+", "_", site.lower())))
        items += items_of_screen(round_, phase, sites, reads)
    items += items_of_arch3(_load(os.path.join(research, R3_DRAW), {}) or {},
                             _load(os.path.join(research, R3_SCREEN), {}) or {})
    cohort = _load(os.path.join(research, R4B_COHORT), {}) or {}
    main4b = set((cohort.get("main") or {}).get("shop") or []) | set((cohort.get("main") or {}).get("other") or [])
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for it in items:
        k = (it["site"], it["value"])
        if it["round"] == "r4b" and it["site"] in main4b and it["round_value"]:
            it["phase"] = "cohort_4b_4c_4d"
        if k not in merged:
            merged[k] = dict(it, seen_in=[f"{it['round']}:{it['phase']}"])
            continue
        m = merged[k]
        m["seen_in"].append(f"{it['round']}:{it['phase']}")
        if m.get("path") is None and it.get("path") is not None:
            m["path"] = it["path"]
        if m.get("rules_check") is None and it.get("rules_check") is not None:
            m["rules_check"] = it["rules_check"]
        m["round_value"] = m["round_value"] or it["round_value"]
    out = sorted(merged.values(), key=lambda x: (x["site"], x["value"]))
    for n, it in enumerate(out, 1):
        it["id"] = f"v{n:04d}"
    return out


def site_key(site: str) -> str:
    """The split's unit: the registrable domain (a www/apex variant is the same site)."""
    import web_arch_live as L
    return L.registrable(site)


def split_sites(sites: Sequence[str], seed: str = SEED) -> Dict[str, str]:
    """PURE: site (registrable domain) -> "selection" | "check", half each, by the seeded shuffle of the
    sorted sites (``<SEED>:split``); an odd one out goes to CHECK."""
    keys = sorted({site_key(s) for s in sites})
    random.Random(f"{seed}:split").shuffle(keys)
    n_sel = len(keys) // 2
    return {k: ("selection" if i < n_sel else "check") for i, k in enumerate(keys)}


def cmd_build(root: str) -> int:
    items = build_items()
    os.makedirs(os.path.join(root, "calib"), exist_ok=True)
    with open(os.path.join(root, "calib", "candidates.json"), "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    c = Counter((it["round"], it["stratum"]) for it in items)
    print(json.dumps({"items": len(items), "sites": len({it['site'] for it in items}),
                      "by_round_stratum": {f"{a}/{b}": n for (a, b), n in sorted(c.items())},
                      "with_path": sum(1 for it in items if it["path"] is not None),
                      "rules_checked": sum(1 for it in items if it["rules_check"])}, indent=1))
    return 0


def set_from(items: Sequence[Dict[str, Any]], labels: Dict[str, str], rule: str, seed: str = SEED
             ) -> Dict[str, Any]:
    """PURE: the labelled set - every item with its label (GOOD = "product") and its split (by site,
    seeded); refuses an unlabelled item or an unknown label."""
    bad = [i["id"] for i in items if labels.get(i["id"]) not in LABELS]
    if bad:
        raise ValueError(f"unlabelled or unknown labels: {bad[:10]} ({len(bad)})")
    split = split_sites([i["site"] for i in items], seed)
    out = []
    for it in items:
        k = site_key(it["site"])
        out.append(dict(it, site_key=k, label=labels[it["id"]], good=labels[it["id"]] == "product", split=split[k]))
    c = Counter((x["split"], x["good"]) for x in out)
    return {"version": 1, "seed": seed, "split_rule": f"registrable domains, sorted, shuffled by '{seed}:split'; "
            "the first half SELECTION, the rest CHECK", "label_rule": rule, "labels": list(LABELS),
            "good_label": "product",
            "counts": {sp: {"items": c[(sp, True)] + c[(sp, False)], "good": c[(sp, True)], "bad": c[(sp, False)],
                            "sites": sum(1 for v in split.values() if v == sp)} for sp in ("selection", "check")},
            "items": out}


def cmd_split(root: str) -> int:
    items = _load(os.path.join(root, "calib", "candidates.json"))
    labels = {}
    with open(os.path.join(root, "calib", "labels.tsv"), encoding="utf-8") as fh:
        for ln in fh:
            if ln.strip():
                i, lab = ln.rstrip("\n").split("\t")[:2]
                labels[i] = lab
    with open(os.path.join(root, "calib", "LABEL_RULE.md"), encoding="utf-8") as fh:
        rule = fh.read()
    s = set_from(items, labels, rule)
    with open(SET_FILE, "w", encoding="utf-8") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=1)
    print(json.dumps(s["counts"], indent=1))
    return 0


# ---------------------------------------------------------------------------
# 3. the pod session: both orders on every value, t on SELECTION, CHECK once, freeze
# ---------------------------------------------------------------------------

#: the t grid and the rule that picks t on SELECTION (written before any inference)
T_GRID = (0.0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.97, 0.99)
TARGET_PRECISION = 0.95
MIN_KEPT = 20
TUNE_RULE = (f"on SELECTION only: the SMALLEST t of the grid {list(T_GRID)} at which the kept values' precision "
             f"(good kept / all kept) is >= {TARGET_PRECISION} with >= {MIN_KEPT} values kept; if no t reaches it, "
             "the t with the highest precision (ties: the smaller t), reported as a missed target")
ASK_WORKERS = 8
ASK_TIMEOUT_S = 20.0


def loopback_client(url: str = LOOPBACK, timeout_s: float = ASK_TIMEOUT_S):
    """The OpenJev client for the pod session: the loopback tunnel only (no key, never another host)."""
    from anyuse.execution import openjev
    import urllib.parse as up
    h = up.urlsplit(url).hostname
    if h != "127.0.0.1":
        raise SystemExit(f"refused: {url!r} is not the loopback tunnel 127.0.0.1")
    cl = openjev.Client(url=url, model="openjev-latest", timeout_s=timeout_s, key="")
    if not cl.available:
        raise SystemExit(f"refused: {url!r} is not a usable OpenJev URL")
    return cl


def load_set(path: Optional[str] = None) -> Dict[str, Any]:
    return _load(path or SET_FILE, {}) or {}


def load_answers(path: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    d = json.loads(ln)
                    out[d["id"]] = d["answers"]
    except OSError:
        pass
    return out


def ask_items(items: Sequence[Dict[str, Any]], chooser, out_path: str, workers: int = ASK_WORKERS,
              log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Both orders for every item not yet answered (resumable: ``out_path`` is appended); an unreachable
    answer is not written (the item is asked again on the next run)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    VC = _vc()
    done = load_answers(out_path)
    todo = [it for it in items if it["id"] not in done]
    lock = threading.Lock()
    stats = {"asked": 0, "written": 0, "unreachable": 0, "invalid": 0}

    def one(it):
        c = VC.Candidate(value=it["value"], source=it["source"], path=it.get("path"), lang=it.get("lang") or "unknown",
                         neighbours=list(it.get("neighbours") or []), site=it["site"])
        t0 = time.time()
        ans = VC.ask_both(chooser, VC.state_of(c))
        rec = {"id": it["id"], "answers": ans, "wall_ms": round((time.time() - t0) * 1000.0, 1),
               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with lock:
            stats["asked"] += 1
            if any(VC.unreachable(a.get("error")) for a in ans):
                stats["unreachable"] += 1
                return
            if any(a.get("label") is None for a in ans):
                stats["invalid"] += 1
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["written"] += 1
            if stats["asked"] % 50 == 0:
                log(f"{time.strftime('%H:%M:%S')} asked {stats['asked']}/{len(todo)} ({stats})")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, todo))
    stats["todo"] = len(todo)
    stats["answered_total"] = len(load_answers(out_path))
    return stats


def keep_of(answers: Optional[Sequence[Dict[str, Any]]], t: float) -> bool:
    if not answers:
        return False
    return _vc().decide(answers, t)[0]


def metrics(items: Sequence[Dict[str, Any]], keep: Callable[[Dict[str, Any]], bool]) -> Dict[str, Any]:
    """Bad values let through (the key error), good values rejected, the kept values' precision."""
    n = len(items)
    good = sum(1 for i in items if i["good"])
    kept = [i for i in items if keep(i)]
    kb = sum(1 for i in kept if not i["good"])
    kg = len(kept) - kb
    return {"n": n, "good": good, "bad": n - good, "kept": len(kept), "bad_let_through": kb,
            "good_rejected": good - kg, "precision": round(kg / len(kept), 4) if kept else None,
            "bad_let_through_rate": round(kb / (n - good), 4) if n - good else None,
            "good_kept_rate": round(kg / good, 4) if good else None}


def by_stratum(items, keep) -> Dict[str, Any]:
    out = {"all": metrics(items, keep)}
    for st in ("shop", "other"):
        out[st] = metrics([i for i in items if i["stratum"] == st], keep)
    return out


def tune_t(items: Sequence[Dict[str, Any]], answers: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """TUNE_RULE on the items given (SELECTION only; the caller filters)."""
    grid = []
    for t in T_GRID:
        m = metrics(items, lambda i, t=t: keep_of(answers.get(i["id"]), t))
        grid.append(dict(m, t=t))
    ok = [g for g in grid if g["precision"] is not None and g["precision"] >= TARGET_PRECISION and g["kept"] >= MIN_KEPT]
    if ok:
        pick, met = min(ok, key=lambda g: g["t"]), True
    else:
        cand = [g for g in grid if g["precision"] is not None]
        pick = max(cand, key=lambda g: (g["precision"], -g["t"])) if cand else grid[-1]
        met = False
    return {"rule": TUNE_RULE, "t": pick["t"], "target_met": met, "grid": grid}


def describe(items, answers) -> Dict[str, Any]:
    """OpenJev's agreed label vs the labels; order agreement; latency."""
    conf = Counter()
    agree = total = 0
    ms, sms = [], []
    for i in items:
        a = answers.get(i["id"]) or []
        if len(a) == 2:
            total += 1
            if a[0].get("label") == a[1].get("label") and a[0].get("label"):
                agree += 1
                conf[(i["label"], a[0]["label"])] += 1
            else:
                conf[(i["label"], "orders disagree")] += 1
            for x in a:
                if x.get("ms") is not None:
                    ms.append(float(x["ms"]))
                if x.get("server_ms") is not None:
                    sms.append(float(x["server_ms"]))

    def pct(v, q):
        v = sorted(v)
        return round(v[min(len(v) - 1, int(q * len(v)))], 1) if v else None
    return {"answered": total, "orders_agree": agree,
            "confusion": {f"{a} -> {b}": n for (a, b), n in sorted(conf.items())},
            "ms_p50": pct(ms, 0.5), "ms_p95": pct(ms, 0.95), "server_ms_p50": pct(sms, 0.5),
            "server_ms_p95": pct(sms, 0.95)}


def check_table(items: Sequence[Dict[str, Any]], answers: Dict[str, List[Dict[str, Any]]], t: float) -> Dict[str, Any]:
    """The CHECK table: OpenJev at the frozen t vs the incumbent rules on the same values."""
    ojk = lambda i: keep_of(answers.get(i["id"]), t)  # noqa: E731
    checked = [i for i in items if i.get("rules_check")]
    return {"t": t,
            "openjev": by_stratum(items, ojk),
            "rules_draw": by_stratum(items, lambda i: True),
            "subset_with_rules_search_check": {
                "rules_draw_and_search_check": by_stratum(checked, lambda i: i["rules_check"] == "kept"),
                "openjev_and_rules_search_check": by_stratum(checked, lambda i: ojk(i) and i["rules_check"] == "kept"),
                "openjev": by_stratum(checked, ojk)},
            "missing_answers": sum(1 for i in items if not answers.get(i["id"])),
            "describe": describe(items, answers),
            "bad_let_through": [{"id": i["id"], "site": i["site"], "value": i["value"], "label": i["label"],
                                 "answers": [(a.get("label"), a.get("confidence")) for a in answers.get(i["id"]) or []]}
                                for i in items if ojk(i) and not i["good"]],
            "good_rejected": [{"id": i["id"], "site": i["site"], "value": i["value"],
                               "answers": [(a.get("label"), a.get("confidence")) for a in answers.get(i["id"]) or []]}
                              for i in items if i["good"] and not ojk(i)]}


def _paths(root: str) -> Dict[str, str]:
    return {"answers": os.path.join(root, "runs", "label_answers.jsonl"),
            "tune": os.path.join(root, "results", "tune.json"),
            "check": os.path.join(root, "results", "check.json"),
            "opened": os.path.join(root, "results", "CHECK_OPENED.json")}


def _save(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def cmd_ask(root: str, url: str, workers: int) -> int:
    VC = _vc()
    s = load_set()
    P = _paths(root)
    os.makedirs(os.path.dirname(P["answers"]), exist_ok=True)
    chooser = VC.client_chooser(loopback_client(url))
    t0 = time.time()
    st = ask_items(s["items"], chooser, P["answers"], workers=workers)
    st["seconds"] = round(time.time() - t0, 1)
    print(json.dumps(st))
    return 0 if st["answered_total"] == len(s["items"]) else 5


def cmd_tune(root: str) -> int:
    s = load_set()
    P = _paths(root)
    ans = load_answers(P["answers"])
    sel = [i for i in s["items"] if i["split"] == "selection"]
    r = tune_t(sel, ans)
    r.update(selection=by_stratum(sel, lambda i: keep_of(ans.get(i["id"]), r["t"])),
             selection_rules_draw=by_stratum(sel, lambda i: True), describe=describe(sel, ans),
             missing_answers=sum(1 for i in sel if not ans.get(i["id"])), at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _save(P["tune"], r)
    print(json.dumps({k: r[k] for k in ("t", "target_met", "missing_answers")}), json.dumps(r["selection"]["all"]))
    return 0


def cmd_check(root: str) -> int:
    s = load_set()
    P = _paths(root)
    if os.path.exists(P["opened"]):
        raise SystemExit(f"refused: CHECK was opened already ({P['opened']}); it is opened ONCE")
    tune = _load(P["tune"])
    if not tune or tune.get("t") is None:
        raise SystemExit("refused: no tuned t (run tune on SELECTION first)")
    t = float(tune["t"])
    ans = load_answers(P["answers"])
    chk = [i for i in s["items"] if i["split"] == "check"]
    missing = [i["id"] for i in chk if not ans.get(i["id"])]
    if missing:
        raise SystemExit(f"refused: {len(missing)} CHECK values have no answers yet (ask first); CHECK not opened")
    _save(P["opened"], {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "t": t, "tune_file_t": tune["t"]})
    r = check_table(chk, ans, t)
    r["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save(P["check"], r)
    print(json.dumps(r["openjev"]["all"]), json.dumps(r["rules_draw"]["all"]))
    return 0


def cmd_freeze(root: str) -> int:
    VC = _vc()
    P = _paths(root)
    tune, chk = _load(P["tune"]), _load(P["check"])
    if not tune or not chk or float(chk["t"]) != float(tune["t"]):
        raise SystemExit("refused: freeze needs the tuned t and the CHECK opened with it")
    spec = VC.load_config()
    spec.update(threshold=float(tune["t"]), status="frozen", frozen_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                tune_rule=TUNE_RULE, target_met_on_selection=bool(tune["target_met"]),
                selection=tune["selection"]["all"], check=chk["openjev"]["all"],
                check_rules_draw=chk["rules_draw"]["all"], set_file="web_valuecheck_set.json")
    with open(VC.CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(json.dumps({"threshold": spec["threshold"], "check": spec["check"]}))
    return 0


# ---------------------------------------------------------------------------
# 4. the DEV live check: <= 3 fresh DEV hosts screened with value_check="openjev"
# ---------------------------------------------------------------------------

DEV_LABEL = "web-valuecheck"
DEV_PRIOR_FILE = "contacted_before.txt"
MAX_DEV_SITES = 3


def configure_dev(checker: Any) -> None:
    """``web_arch4b_live``'s frozen value rules (its bench configuration) with this bench's own seed, label, guard
    list and the usual screen share (70 requests / 7 MB per DEV site), the OpenJev value check on."""
    import web_arch4b_live as B4B
    A4 = _a4()
    B4B.configure()
    A4.SEED = SEED
    A4.BENCH_LABEL = DEV_LABEL
    A4.PRIOR_CONTACTED_FILE = DEV_PRIOR_FILE
    A4.DEV_SHARES = {"screen": 70, "demo": 0, "immediate": 0, "delayed": 0}
    A4.DEV_BYTES = {"screen": 7 * A4.TR.MB, "demo": 0, "immediate": 0, "delayed": 0}
    A4.VALUE_CHECK = "openjev"
    A4.VALUE_CHECKER = checker


def check_dev_sites(sites: Sequence[Dict[str, Any]], prior: Sequence[str]) -> List[str]:
    """Why these DEV sites may not be screened ([] = fine): more than 3; a host (by registrable domain, or
    the same name on another TLD) in the prior list (every host any earlier bench wrote or contacted, the latest
    frame and cohort included); NEVER_CONTACT, government or a competitor (the benches' own refusal)."""
    import web_arch_live as L
    why = []
    if len(sites) > MAX_DEV_SITES:
        why.append(f"{len(sites)} DEV sites (at most {MAX_DEV_SITES})")
    preg = {L.registrable(h) for h in prior if h}
    #: the same name on another TLD (www.idee-shop.com contacted before -> www.idee-shop.de refused)
    plabel = {r.split(".")[0] for r in preg if "." in r}
    for e in sites:
        h = e["site"]
        if h in prior or L.registrable(h) in preg:
            why.append(f"{h}: written or contacted by an earlier round")
        elif L.registrable(h).split(".")[0] in plabel:
            why.append(f"{h}: the same name on another TLD was written or contacted by an earlier round")
        r = L.refusal(h)
        if r:
            why.append(f"{h}: {r}")
    return why


def cmd_dev(root: str, browser: str, url: str) -> int:
    VC = _vc()
    sites = _load(os.path.join(root, "screen", "dev_sites.json"), []) or []
    with open(os.path.join(root, "screen", DEV_PRIOR_FILE), encoding="utf-8") as fh:
        prior = [ln.strip() for ln in fh if ln.strip()]
    why = check_dev_sites(sites, prior)
    if why:
        raise SystemExit("refused: " + "; ".join(why))
    os.makedirs(os.path.join(root, "dev", "runs"), exist_ok=True)
    logp = os.path.join(root, "dev", "runs", "value_checks.jsonl")

    def log(rec):
        with open(logp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(rec, at=time.strftime("%H:%M:%S")), ensure_ascii=False) + "\n")
    checker = VC.ValueChecker(client=loopback_client(url, timeout_s=15.0), log=log)
    configure_dev(checker)
    rc = _a4().main(["screen", root, "--dev", "--browser", browser, "--part", "0", "--parts", "1"])
    print(json.dumps({"openjev_calls": checker.calls, "down": checker.down, "verdicts": len(checker.verdicts)}))
    return rc


def dev_comparison(screen: Dict[str, Any]) -> List[Dict[str, Any]]:
    """PURE: per DEV site, the values drawn with the OpenJev check vs what the rules would have drawn from
    the same result pages (the rules' first 5 candidates per try; which of them the screen search-checked)."""
    out = []
    for site, rec in (screen.get("sites") or {}).items():
        checks = {c["value"]: c for c in rec.get("value_checks") or []}
        row = {"site": site, "stratum": rec.get("stratum"), "screen": rec.get("screen"), "why": rec.get("why"),
               "requests": rec.get("requests"), "bytes": rec.get("bytes"), "tries": [],
               "values": rec.get("values")}
        for tr in rec.get("value_tries") or []:
            rules = list(tr.get("rules_values") or tr.get("values") or [])
            row["tries"].append({
                "seed": tr.get("seed"), "rules_values": rules, "openjev_values": list(tr.get("values") or []),
                "rejected": [{"value": c["value"], "reason": c["reason"]} for c in tr.get("checked") or [] if not c["keep"]],
                "fallbacks": tr.get("fallbacks", 0),
                "rules_only": [v for v in rules if v not in (tr.get("values") or [])],
                "openjev_only": [v for v in tr.get("values") or [] if v not in rules],
                "search_checked": {v: ("kept" if (checks[v].get("naming") or 0) > 0 and not checks[v].get("no_results")
                                       else "rejected") for v in (tr.get("values") or []) if v in checks}})
        out.append(row)
    return out


def cmd_devreport(root: str) -> int:
    scr = _load(os.path.join(root, "dev", "screen", "screen.json"), {}) or {}
    rows = dev_comparison(scr)
    _save(os.path.join(root, "results", "dev_comparison.json"), rows)
    print(json.dumps(rows, ensure_ascii=False, indent=1)[:6000])
    return 0


# ---------------------------------------------------------------------------
# 5. watch --explain live on OpenJev: synthetic diffs from the watch fixtures (no site contact)
# ---------------------------------------------------------------------------

#: the watch fixtures' shapes (test_watch.py: PRODUCT_TOOL, LIST_TOOL, product(), listing(), NEWS)
EXPLAIN_T0 = "2026-11-03T08:00:00Z"
_PRODUCT_URL = "https://www.shop.test/p/line-6-pod-express-guitar/"


def _product(price=159.0, availability="InStock", name="Line 6 POD Express Guitar", url=_PRODUCT_URL):
    return {"status": "ok", "output": {"name": name, "price": price, "currency": "EUR", "availability": availability,
                                       "url": url},
            "why": "identity and fields checked", "model_calls": 0, "requests": 9, "bytes": 400000,
            "pages": [{"url": "https://www.shop.test/search?q=Line+6", "title": "Suche"},
                      {"url": url, "title": name, "h1": [name]}], "picked": {"title": name, "url": url}}


def _listing(rows):
    return {"status": "ok", "output": {"results": [{"title": t, "url": u} for t, u in rows]}, "why": "query: ok",
            "model_calls": 0, "requests": 6, "bytes": 300000,
            "pages": [{"url": "https://www.news.test/?s=Garten", "title": "Suche"}]}


def _art(n: int, title: str = "") -> Tuple[str, str]:
    return (title or f"Artikel {n}", f"https://www.news.test/artikel-{n}/")


def explain_cases() -> List[Dict[str, Any]]:
    """Four synthetic, realistic diffs (before -> after) and the OBVIOUS answer, written before any call:
    the kinds of change line a person watching the item would call the most important."""
    return [
        {"case": "price drop", "workflow": "product_lookup", "input": "Line 6 POD Express Guitar",
         "before": _product(), "after": _product(price=129.0, url=_PRODUCT_URL + "?variant=standard"),
         "obvious": ["price"], "note": "a price drop of 30 EUR (-18.9 %) and a cosmetic URL query change"},
        {"case": "stock -> out of stock", "workflow": "product_lookup", "input": "Line 6 POD Express Guitar",
         "before": _product(), "after": _product(availability="OutOfStock", url=_PRODUCT_URL + "?ref=search"),
         "obvious": ["availability"], "note": "in stock -> out of stock and a cosmetic URL query change"},
        {"case": "list: 2 new + 1 removed", "workflow": "site_search", "input": "Garten",
         "before": _listing([_art(n) for n in range(1, 6)]),
         "after": _listing([_art(7, "Artikel 7: Hochbeet im Herbst anlegen"), _art(1), _art(2),
                            _art(8, "Artikel 8: Rosen richtig schneiden"), _art(4), _art(5)]),
         "obvious": ["added", "removed"], "note": "2 new results (#1, #4), 1 removed (#3); the rest moved down"},
        {"case": "cosmetic reorder only", "workflow": "site_search", "input": "Garten",
         "before": _listing([_art(n) for n in range(1, 6)]),
         "after": _listing([_art(1), _art(3), _art(2), _art(4), _art(5)]),
         "obvious": [], "note": "two results swapped (#2 <-> #3): nothing substantive changed; the obvious answer "
                               "is the fixed last option 'nothing important changed' (web-valuecheck2)"},
    ]


def run_explain(store_dir: str, explainer: Any) -> List[Dict[str, Any]]:
    """Each case as a watched item: a first run (the before snapshot, a baseline), a second one hour later (the after
    snapshot) with ``--explain`` - the real ``watch.run_watch`` path; only the replay is synthetic."""
    import datetime as dt
    from anyuse.execution import watch as WA
    t0 = WA.parse_iso(EXPLAIN_T0)
    st = WA.WatchStore(store_dir)
    out = []
    for n, c in enumerate(explain_cases()):
        tool = {"version": 6, "workflow": c["workflow"],
                "site": "www.shop.test" if c["workflow"] == "product_lookup" else "www.news.test",
                "start_url": "https://www.shop.test/" if c["workflow"] == "product_lookup" else "https://www.news.test/",
                "search": {"mode": "url", "template": ("https://www.shop.test/search?q={q}" if c["workflow"] ==
                                                       "product_lookup" else "https://www.news.test/?s={q}")},
                "fields": ({"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical"}
                           if c["workflow"] == "product_lookup" else None),
                "region": {"regex": "^x$"}, "hosts": ["www.shop.test" if c["workflow"] == "product_lookup" else
                                                     "www.news.test"], "model_calls": 0, "top_k": 6, "case": n}
        tp = os.path.join(store_dir, f"case{n}.tool.json")
        os.makedirs(store_dir, exist_ok=True)
        with open(tp, "w", encoding="utf-8") as fh:
            json.dump(tool, fh)
        item = st.add(tp, c["input"], "1h", now=t0)
        seq = [c["before"], c["after"]]
        replay = lambda it, tl, caps, seq=seq: seq.pop(0)  # noqa: E731
        WA.run_watch(st, replay, now=t0 + dt.timedelta(hours=n * 2), only=[item["id"]])
        rep = WA.run_watch(st, replay, now=t0 + dt.timedelta(hours=n * 2 + 1), explainer=explainer, only=[item["id"]])
        e = rep["entries"][0]
        lines = [WA.change_line(x) for x in e["changes"]]
        x = e.get("explain") or {}
        text = str(x.get("text") or "")
        nothing = x.get("important") is False          # the fixed last option
        picked = None if nothing else next((ln for ln in lines if text.endswith(ln)), None)
        kind = next((ch["kind"] for ch, ln in zip(e["changes"], lines) if ln == picked), None) if picked else None
        if nothing:
            match = not c["obvious"]
        elif kind:
            match = kind in c["obvious"]
        else:
            match = None
        out.append({"case": c["case"], "note": c["note"], "kind": e["kind"], "lines": lines, "explain": x,
                    "picked_line": picked, "picked_kind": "nothing important" if nothing else kind,
                    "obvious": c["obvious"], "matches_obvious": match, "model_calls": rep["model_calls"]})
    return out


def cmd_explain(root: str, url: str) -> int:
    from anyuse.execution import watch as WA
    store = os.path.join(root, "explain", "watch_store")
    if os.path.exists(os.path.join(store, "items.json")):
        raise SystemExit(f"refused: {store} exists (one live run; move it away to run again)")
    res = run_explain(store, WA.OpenJevExplainer(client=loopback_client(url, timeout_s=15.0)))
    _save(os.path.join(root, "results", "explain.json"), res)
    for r in res:
        print(f"{r['case']:28s} lines={len(r['lines'])} pick={r['picked_line']!r} "
              f"({(r['explain'] or {}).get('text', '')[:40]!r}) obvious={r['obvious']} match={r['matches_obvious']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd")
    ap.add_argument("root")
    ap.add_argument("--url", default=LOOPBACK)
    ap.add_argument("--workers", type=int, default=ASK_WORKERS)
    ap.add_argument("--browser", default="")
    args, _rest = ap.parse_known_args(argv)
    root = os.path.abspath(args.root)
    if args.cmd == "build":
        return cmd_build(root)
    if args.cmd == "split":
        return cmd_split(root)
    if args.cmd == "ask":
        return cmd_ask(root, args.url, args.workers)
    if args.cmd == "tune":
        return cmd_tune(root)
    if args.cmd == "check":
        return cmd_check(root)
    if args.cmd == "freeze":
        return cmd_freeze(root)
    if args.cmd == "dev":
        if not args.browser:
            raise SystemExit("dev needs --browser")
        return cmd_dev(root, args.browser, args.url)
    if args.cmd == "devreport":
        return cmd_devreport(root)
    if args.cmd == "explain":
        return cmd_explain(root, args.url)
    raise SystemExit(f"unknown command {args.cmd!r}")


if __name__ == "__main__":
    raise SystemExit(main())
