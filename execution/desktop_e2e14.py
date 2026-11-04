"""Desktop engine, generation 14: arm C = ``desktop_e2e12``'s arm (with ``precondition13``) with
OpenJev as the picker. Builds on
``desktop_e2e`` ... ``desktop_e2e12`` (all left byte-identical) and is PURE like them (no pyobjc, no
subprocess, no network): the guest, the planner and OpenJev are injected callables.

``desktop_e2e11`` compared, on ``desktop_e2e9``'s pool, ``desktop_e2e8``'s frozen incumbent on OpenJev (arm A, 14/30)
with ``desktop_e2e8``'s protocol on Cerebras plus the recovery layer of ``desktop_e2e9`` and ``desktop_e2e10`` (arm B,
9/30): A's extra completions were mostly B's chooser handing off the RIGHT item below its t2 = 0.99.
``desktop_e2e12`` (with ``precondition13``) fixed ``desktop_e2e11``'s other causes (the filter, the planner checks,
the executor) on seen tasks. This module asks whether those layers help when the picker is OpenJev again.

``Arch14`` is a THIN composition of existing parts, nothing new in the decision itself:

* **``desktop_e2e12``'s arm** (``desktop_e2e12.Arch12``) with the flag ``precondition13=True``: the recovery layer of
  ``desktop_e2e9`` and ``desktop_e2e10`` (typed evidence in the STATE, the one read-only inspection, the pre-action
  identity recheck, the exact permitted diff, the journal with its compare-and-swap restore, the concurrency checks,
  the two planner checks), ``desktop_e2e12``'s fixed filter (negations by slot, kept parts, a search's rows in their
  own folder), its planner checks (location exclusions, the fill-in at a label, the value's provenance, the
  completion check) and the declared precondition of Notes' view items;
* **OpenJev as the picker** in place of the Cerebras chooser, with **``desktop_e2e8``'s frozen pick protocol**
  (``desktop_e2e8_picker.json``: protocol pair, t2 = 0.45): its RICH option texts (``rich_texts10`` =
  ``desktop_e2e8.rich_options``), both orders of the alive candidates with NONE at a fixed LAST position (the
  placement of ``desktop_e2e9`` ... ``desktop_e2e11``), act only when both orders name the same candidate, neither
  NONE and the minimum OpenJev confidence >= t2; when the orders named two DIFFERENT candidates, the pair follow-up
  (the two and NONE, both orders, the fields that tell them apart in the state); else a hand-off. The question and
  the typed-evidence state are ``desktop_e2e10``'s (``question10``, ``state10``), exactly as ``desktop_e2e11``'s
  ``ask11`` sends them;
* **``desktop_e2e8``'s date veto** (``desktop_e2e5.date_superlative`` / ``date_extreme``, frozen as ``desktop_e2e8``
  used it): on a dated list, an accepted pick that is not the one candidate with the newest (oldest) modification
  date among the candidates OpenJev was asked about that share a word with the target is handed off;
* ``desktop_e2e7``'s settings cross-check asks OpenJev's text chooser with the same t2 (as ``Arch8`` does).

judge stays frozen (``desktop_e2e9.judge_from``); every run goes through ``run_one14`` (= ``desktop_e2e9``'s run).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import desktop_e2e as X
from . import desktop_e2e3 as Z3
from . import desktop_e2e5 as V
from . import desktop_e2e6 as U
from . import desktop_e2e8 as R8
from . import desktop_e2e9 as N
from . import desktop_e2e10 as T
from . import desktop_e2e12 as Z

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS14_PATH = os.path.join(HERE, "desktop_e2e14_tasks.json")
GEN14 = X.GEN + "/e14"
NONE14 = T.NONE10                     # "NONE: the right item is clearly not listed", always the LAST option
PICKER14 = "openjev"                  # desktop_e2e8's frozen configuration of this picker (pair, t2 0.45)


# ---------------------------------------------------------------------------
# OpenJev behind the interface desktop_e2e11's pick asks
# ---------------------------------------------------------------------------

def openjev_as_json14(chooser) -> Callable:
    """OpenJev's text chooser ``(state, instructions, options) -> (index, confidence, meta)`` behind the interface
    that ``desktop_e2e11``'s ``ask11`` (and so ``desktop_e2e12``'s ``pick12``) calls:
    ``(state, question, [(id, text), ...]) -> (id or None, none, meta)``. The option texts go out in the order given
    with NONE LAST; OpenJev's confidence (its option probability) is the answer's ``confidence``; an error or an index
    out of range is an answer error."""
    def jchooser(state, question, options):
        opts = [text for _cid, text in options] + [NONE14]
        i, conf, meta = chooser(state, question, opts)
        meta = dict(meta or {})
        meta["confidence"] = float(conf or 0.0)
        if meta.get("error"):
            return None, False, meta
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(opts):
            return None, False, dict(meta, error=f"choice {i!r} out of range")
        if i == len(opts) - 1:
            return None, True, meta
        return options[i][0], False, meta
    return jchooser


def config_of14(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Arm C's picker configuration = ``desktop_e2e8``'s FROZEN one for OpenJev (``desktop_e2e8_picker.json``), with
    its date veto; none before that file is frozen (the arm cannot run)."""
    t2, proto = R8.config_of8(PICKER14, spec)
    if proto != "pair":
        raise KeyError(f"the frozen protocol is 'pair', not {proto!r}")
    return {"model": "openjev", "protocol": proto, "t2": float(t2), "date_veto": True}


# ---------------------------------------------------------------------------
# The pick: desktop_e2e12's pick (desktop_e2e8's protocol on desktop_e2e12's filter) asked to OpenJev, then
# desktop_e2e8's date veto
# ---------------------------------------------------------------------------

def _num(cid: Optional[str]) -> Optional[int]:
    return int(cid[1:]) - 1 if cid and cid[:1] == "C" and cid[1:].isdigit() else None


def date_veto14(log: Dict[str, Any], goal: str, step: Dict[str, Any], rich: Sequence[str]) -> Dict[str, Any]:
    """``desktop_e2e8``'s date veto on an accepted pick (``Arch8.pick``): when the step's target (or the goal's words
    for it) asks for the newest / oldest item, the pick must be the ONE candidate with that modification date among the
    candidates OpenJev was asked about (the alive ones, in listing order) that share a word with the target (all
    dated ones when none does); else a hand-off. ``rich`` = the whole list's option texts (index = candidate)."""
    if log.get("index") is None:
        return log
    target = step.get("target") or ""
    which = V.date_superlative(target, goal)
    if not which:
        return log
    alive = [i for i in (_num(c) for c in log.get("alive") or []) if i is not None and i < len(rich)]
    ext = V.date_extreme([rich[i] for i in alive], target, which)
    ext_i = alive[ext] if ext is not None else None
    log["date_check"] = {"asks": which, "extreme": f"C{ext_i + 1}" if ext_i is not None else None,
                         "pick": log.get("id"), "among": [f"C{i + 1}" for i in alive]}
    if ext_i is not None and ext_i != log["index"]:
        log.update(index=None, id=None, reason=f"hand-off: the pick contradicts the {which} modification date the "
                                               f"target asks for ({rich[ext_i][:60]!r})")
    return log


def pick14(cfg: Dict[str, Any], goal: str, step: Dict[str, Any], head: str, cands: Sequence[Dict[str, Any]],
           list_kind: str, now: Optional[_dt.datetime], openjev, ui_lang: Optional[str] = None, inspect=None,
           spend=None, where: str = "", where_path: str = "", bad: Optional[Dict[str, str]] = None,
           dated: bool = False) -> Dict[str, Any]:
    """This module's pick. ``cfg`` = ``config_of14()``. ``desktop_e2e12``'s ``pick12`` (``prepare12``: its filter, the
    typed evidence, the one inspection; every candidate contradicted = a hand-off without a call; then
    ``desktop_e2e8``'s protocol: both orders, NONE last, the pair follow-up, min confidence >= t2) with OpenJev as the
    chooser, then ``desktop_e2e8``'s date veto on a ``dated`` list (Finder rows, the Open panel)."""
    rich = T.rich_texts10([dict(c) for c in cands], now, list_kind)    # = prepare12's options (before any inspection)
    log = Z.pick12(cfg, goal, step, head, cands, list_kind, now, openjev_as_json14(openjev), ui_lang=ui_lang,
                   inspect=inspect, spend=spend, where=where, where_path=where_path, rich=rich, bad=bad)
    log["mode"] = "openjev (pair protocol on value filter)"
    log["calls_by"] = {"openjev": log["calls"]} if log["calls"] else {}
    if dated and cfg.get("date_veto", True):
        date_veto14(log, goal, step, rich)
    return log


# ---------------------------------------------------------------------------
# The arm
# ---------------------------------------------------------------------------

class Arch14(Z.Arch12):
    """``desktop_e2e12``'s arm with ``precondition13`` and OpenJev as the picker (``desktop_e2e8``'s frozen protocol
    and date veto); see the module docstring."""

    protocol14 = "arch14 (Arch12 + declared precondition; OpenJev, pair protocol and date veto)"

    def __init__(self, guest, emap, planner=None, openjev=None, config14: Optional[Dict[str, Any]] = None, **kw):
        real = config14 is not None
        if real and openjev is None:
            raise X.GuestError("Arch14 needs its picker (OpenJev)")
        cfg = dict(config14) if real else None
        kw.setdefault("picker_kind", "openjev")
        kw.setdefault("precondition13", True)
        super().__init__(guest, emap, planner=planner, config11=cfg, jchooser=openjev_as_json14(openjev) if real
                         else None, **kw)
        self.config14, self.openjev = cfg, openjev
        if real:
            # desktop_e2e7's settings cross-check asks OpenJev's text chooser with the same t2 (as Arch8 does)
            self.chooser = openjev
            self.threshold = float(cfg["t2"])
            self.picker_name = "openjev"
            self.protocol = f"arch14 (OpenJev, {cfg['protocol']} protocol, t2 {cfg['t2']}, date veto)"

    def pick(self, rec: X.Rec, goal: str, state: str, cands: List[Dict[str, Any]], step_log: Dict[str, Any],
             question: Optional[str] = None):
        if self.config14 is None:
            return super().pick(rec, goal, state, cands, step_log, question)
        kind = V.cand_kind(state)
        lk = N.list_kind_of(state)
        shown = [dict(c) for c in cands]
        head = state
        if kind:
            shown = self._dates(kind, shown, step_log)
            head = U.fitted_state(kind, V.dated_state(kind, state, self._now))
        head = U.ui_state(head, getattr(self, "_ui_lang", None))
        if lk == "finder_locations" and "places the window can show" in state:
            cont = self._contents(shown, "finder_locations")
            if cont is not None:
                for c, n in zip(shown, cont):
                    if n is not None:
                        c["contains"] = n
        step = dict(step_log.get("step") or {})
        where, wpath = self._where9(state, lk)
        if lk == "notes_notes":
            shown, where_f = Z.search_rows12(shown, where)
            if where_f != where:
                step_log["search_scope12"] = {"scope": where, "parents": [c.get("parent") for c in shown]}
                where = where_f
        sp = None
        if self.spend is not None:
            def sp(_k, actual=None, _kind=self.picker_kind):
                return self.spend(_kind, actual=actual) if actual is not None else self.spend(_kind)
        res = pick14(self.config14, goal, step, head, shown, lk, self._now, self.openjev,
                     ui_lang=getattr(self, "_ui_lang", None), inspect=self._inspector(lk), spend=sp, where=where,
                     where_path=wpath, bad=getattr(self, "_bad12", None), dated=bool(kind))
        rec.calls["picker"] += res["calls"]
        rec.cost += res["cost"]
        by = rec.__dict__.setdefault("picker_calls10", {})
        for k, v in (res.get("calls_by") or {}).items():
            by[k] = by.get(k, 0) + v
        step_log["pick"] = dict(res, list_kind=lk, head=head[:600], index=res.get("index"),
                                rule="pair protocol on OpenJev: both orders (NONE last) the same ID, neither "
                                     "NONE, min confidence >= t2; else the pair follow-up by the same rule; "
                                     "date veto; value filter")
        if res.get("inspected"):
            rec.__dict__["inspections9"] = int(rec.__dict__.get("inspections9") or 0) + 1
        if res.get("index") is None:
            rec.handoffs += 1
            rec.gated += 1
            self._inbox(rec, "picker hand-off: " + res["reason"], step_log)
            return None
        ch = cands[res["index"]]
        self._select9(lk, ch, goal, step, res["constraints"], where, wpath)
        return ch


def run_one14(task: X.Task, arm: str, run: int, guest, **kw):
    """``desktop_e2e9``'s run (judge for EVERY arm, frozen; ``desktop_e2e8``'s and ``desktop_e2e7``'s verdicts
    kept)."""
    return N.run_one9(task, arm, run, guest, **kw)


# ---------------------------------------------------------------------------
# The run order and the paired analysis (pure)
# ---------------------------------------------------------------------------

ARMS14 = ("arch8", "arch14")          # A = desktop_e2e8's frozen incumbent; C = this module's arm


def pair_order14(task_ids: Sequence[str], seed: int, arms: Sequence[str] = ARMS14) -> List[tuple]:
    """One run per task per arm in a randomized BALANCED order within task pairs: the tasks shuffled with ``seed``;
    within each task the two arms in a seeded order, exactly half the tasks (rounded down) with C first."""
    import random
    rnd = random.Random(seed)
    ts = list(task_ids)
    rnd.shuffle(ts)
    k = len(ts) // 2
    c_first = [True] * k + [False] * (len(ts) - k)
    rnd.shuffle(c_first)
    out = []
    for t, cf in zip(ts, c_first):
        order = ["arch14", "arch8"] if cf else ["arch8", "arch14"]
        out += [(t, a) for a in order if a in arms]
    return out


def order_seed14(tasks_sha256_hex: str, salt: str = "e14") -> int:
    """The order seed: int(sha256(the tasks file's sha256 hex + ':' + salt)[:8], 16)
    (``desktop_e2e11``'s construction)."""
    import hashlib
    return int(hashlib.sha256(f"{tasks_sha256_hex}:{salt}".encode()).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# The tasks (a NEW independent pool); judge for every arm
# ---------------------------------------------------------------------------

def load_tasks14(path: str = TASKS14_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_task14(t: Dict[str, Any], titles, baseline, plain_text_title="", gen: str = GEN14) -> X.Task:
    """The translation conventions of ``desktop_e2e5`` ... ``desktop_e2e9`` under this module's data root ({GEN14})."""
    t = json.loads(json.dumps(t).replace("{GEN14}", "{GEN5}"))
    return V.build_task5(t, titles, baseline, plain_text_title, gen)


def tasks14(data: Optional[Dict[str, Any]] = None, drawn: Optional[bool] = True) -> List[X.Task]:
    data = data or load_tasks14()
    out = [build_task14(t, data["te_prefs_titles"], data["te_prefs_baseline"], data.get("te_prefs_plain_text") or "")
           for t in data["tasks"]]
    return [t for t in out if drawn is None or (not t.dev) == drawn]


def task14_by_id(tid: str, data: Optional[Dict[str, Any]] = None) -> X.Task:
    for t in tasks14(data, drawn=None):
        if t.id == tid:
            return t
    raise KeyError(tid)


def statements14(tid: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    data = data or load_tasks14()
    t = next(x for x in data["tasks"] if x["author_task"]["id"] == tid)
    return {k.replace("{GEN14}", GEN14): v for k, v in (t["translation"].get("statements") or {}).items()}


def tasks14_sha256(data: Optional[Dict[str, Any]] = None) -> str:
    return X.tasks_sha256(tasks14(data, drawn=True))


def seeds14_spec(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The one-time Notes seeding of this module's pool (``desktop_e2e3``'s rule: the Finder data is rebuilt at every
    clean start)."""
    return Z3.seeds3_spec(data or load_tasks14())
