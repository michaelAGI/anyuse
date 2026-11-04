"""KNOWN VERDICTS on compiled web tools' answers: a drift
census found 16 replays that repeated, word for word, an answer an earlier audit had judged WRONG - silently, as "ok".
The store keeps every saved verdict - the audits, the blind reader's, the independent checker's - keyed by
the TOOL's digest, the INPUT and the ANSWER's digest, so that a known wrong answer is never repeated silently.

* ``answer_key`` / ``answer_digest``: the normalisation of an answer:
  ``["no_results"]``; ``["results", [title, url], ...]`` (titles folded, URLs without fragment and trailing slash);
  ``["product", name, price, currency, availability, url]``. A run that answered nothing has no answer (no verdict).
* ``VerdictStore(path)``: one JSON-lines file of verdict rows ``{tool, site, input, answer, verdict, source, at, ref,
  why, workflow}``; ``add`` stores one (dropped only when the LATEST row with the same key, source and reference holds
  the same verdict - so wrong, correct, wrong again is three rows and the last one counts). An input is stored as its
  digest (``input_ref``: inputs can be personal data - a watcher's or a desktop routine's values); a store opened with
  ``plain_inputs=True`` keeps the folded text, and rows of either form are matched;
  ``lookup`` finds the verdict on an answer: the exact key first (``scope: "tool"``), else the same input and answer
  from ANY tool of the same site (``scope: "site"`` - a verdict is about the answer, so a revision, a recompile or
  another tool repeating it is matched too). The LATEST verdict wins (a later "correct" supersedes an earlier
  "wrong"; equal times: checker before reader before audit, then the row written last).
* ``known_wrong`` / ``check``: an answer whose verdict is "wrong" makes the run's status ``known_wrong`` - never ok, its
  answer kept only as evidence (``unverified_output``), ``accepted`` False, ``known_wrong`` the verdict.
* ``StoreChecker``: the acceptance gate's checker - the store first (a stored "correct" on the same answer agrees, a
  stored "wrong" / "incomplete" / "disputed" disagrees), else the independent checker it wraps, whose verdict is then
  recorded (source ``checker``: "correct" when it agrees, "disputed" when it does not - a render that does not
  support an answer is not proof that it is wrong, so a dispute is never flagged as known wrong).

Verdicts: ``correct``, ``wrong``, ``incomplete``, ``undeterminable`` (audit, reader) and ``disputed`` (checker). Only
``wrong`` flags a run. Nothing here contacts anything; the store is a local file.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from anyuse.execution import demo_compile as C
from anyuse.execution import web_arch as W

KNOWN_WRONG = "known_wrong"
VERDICTS = ("correct", "wrong", "incomplete", "undeterminable", "disputed")
SOURCES = ("checker", "reader", "audit")
#: equal times: the more independent source wins
_SOURCE_RANK = {"audit": 0, "reader": 1, "checker": 2}


def _url(u: Any) -> str:
    return W.norm_url(C.strip_fragment(str(u or "")))


def _price(p: Any) -> Any:
    try:
        return round(float(p), 2)
    except (TypeError, ValueError):
        return None if p is None else W.fold(p)


def _text(x: Any) -> str:
    return W.fold(x if isinstance(x, str) or x is None else json.dumps(x, sort_keys=True, ensure_ascii=False))


def answer_key(status: Any, output: Any) -> Optional[List[Any]]:
    """The ANSWER of a run (the drift census's normalisation); None when the run answered nothing."""
    if status == "no_results":
        return ["no_results"]
    if status != "ok" or not isinstance(output, dict):
        return None
    if output.get("no_results"):
        return ["no_results"]
    if "results" in output:
        return ["results"] + [[_text(r.get("title")), _url(r.get("url"))] for r in output.get("results") or []
                              if isinstance(r, dict)]
    if "items" in output:
        return ["items"] + [[_url(r.get("url")), _text(r.get("name")), _price(r.get("price")),
                             _text(r.get("availability"))] for r in output.get("items") or [] if isinstance(r, dict)]
    return ["product", _text(output.get("name")), _price(output.get("price")),
            str(output.get("currency") or "").upper(), _text(output.get("availability")), _url(output.get("url"))]


def answer_digest(status: Any, output: Any) -> Optional[str]:
    k = answer_key(status, output)
    return None if k is None else hashlib.sha256(json.dumps(k, ensure_ascii=False).encode()).hexdigest()


def input_key(value: Any) -> str:
    return " ".join(W.fold(str(value or "")).split())


#: the stored form of an input (``VerdictStore`` without ``plain_inputs``)
_IREF = "sha256:"


def input_ref(value: Any) -> str:
    """An input as stored: ``sha256:<hex>`` of its ``input_key`` (an input already in that form is kept)."""
    s = str(value or "")
    if s.startswith(_IREF) and len(s) == len(_IREF) + 64:
        return s
    return _IREF + hashlib.sha256(input_key(s).encode()).hexdigest()


def site_key(site: Any) -> str:
    s = str(site or "").lower().strip()
    return s[4:] if s.startswith("www.") else s


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VerdictStore:
    """The known verdicts (see the module doc), one JSON-lines file at ``path``."""

    def __init__(self, path: str, plain_inputs: bool = False):
        self.path, self.plain_inputs = path, plain_inputs
        self._rows: Optional[List[Dict[str, Any]]] = None
        self._last: Dict[str, str] = {}

    # -- rows -------------------------------------------------------------------
    def rows(self) -> List[Dict[str, Any]]:
        if self._rows is None:
            self._rows = []
            try:
                with open(self.path, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            r = json.loads(line)
                            self._rows.append(r)
                            self._last[self._ident(r)] = str(r.get("verdict"))
            except OSError:
                pass
        return self._rows

    @staticmethod
    def _ident(r: Dict[str, Any]) -> str:
        """A row's key, source and reference (its input in the stored form, so both forms of one input are one)."""
        return "\u0001".join([str(r.get("tool") or ""), input_ref(r.get("input")), str(r.get("answer") or ""),
                               str(r.get("source") or ""), str(r.get("ref") or "")])

    def add_row(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Store one verdict row (``tool``, ``site``, ``input``, ``answer``, ``verdict``, ``source`` required); None
        when the latest row with the same key, source and reference holds this verdict already."""
        r = dict(row)
        if r.get("verdict") not in VERDICTS or r.get("source") not in SOURCES or not r.get("answer"):
            raise ValueError(f"not a verdict row: {r.get('verdict')!r} / {r.get('source')!r}")
        raw = r.get("input")
        r["input"] = input_key(raw) if self.plain_inputs and not str(raw or "").startswith(_IREF) else input_ref(raw)
        r["site"] = site_key(r.get("site"))
        r.setdefault("at", _now())
        self.rows()
        k = self._ident(r)
        if self._last.get(k) == r["verdict"]:
            return None
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        self._last[k] = r["verdict"]
        self._rows.append(r)
        return r

    def add(self, *, tool: str, site: str, value: Any, status: Any, output: Any, verdict: str, source: str,
            ref: str = "", at: str = "", why: str = "", workflow: str = "") -> Optional[Dict[str, Any]]:
        """Store the verdict on a run's answer (a run that answered nothing has no verdict: None)."""
        a = answer_digest(status, output)
        if a is None:
            return None
        return self.add_row({"tool": str(tool or ""), "site": site, "input": value, "answer": a, "verdict": verdict,
                             "source": source, "ref": str(ref or ""), "at": str(at or "") or _now(),
                             "why": str(why or "")[:300], "workflow": str(workflow or "")})

    # -- lookup -----------------------------------------------------------------
    def lookup(self, tool_digest: str, site: str, value: Any, status: Any, output: Any) -> Optional[Dict[str, Any]]:
        """The verdict on this answer: the exact key (``scope`` "tool"), else the same site, input and answer from any
        tool (``scope`` "site"); the latest wins. None when no verdict is known (or the run answered nothing)."""
        a = answer_digest(status, output)
        if a is None:
            return None
        i, s = input_key(value), site_key(site)
        refs = (i, input_ref(i))
        same = [(n, r) for n, r in enumerate(self.rows()) if r.get("answer") == a and r.get("input") in refs]
        for scope, rs in (("tool", [(n, r) for n, r in same if r.get("tool") == tool_digest]),
                          ("site", [(n, r) for n, r in same if r.get("site") == s])):
            if rs:
                _, best = max(rs, key=lambda nr: (str(nr[1].get("at") or ""),
                                                  _SOURCE_RANK.get(str(nr[1].get("source")), -1), nr[0]))
                return dict(best, scope=scope, n=len(rs))
        return None

    def known_wrong(self, tool: Dict[str, Any], value: Any, out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        hit = self.lookup(C.tool_digest(tool), str(tool.get("site") or ""), value, out.get("status"), out.get("output"))
        return hit if hit and hit.get("verdict") == "wrong" else None

    def summary(self) -> Dict[str, Any]:
        from collections import Counter
        rs = self.rows()
        return {"n": len(rs), "by_source": dict(Counter(r.get("source") for r in rs)),
                "by_verdict": dict(Counter(r.get("verdict") for r in rs)),
                "tools": len({r.get("tool") for r in rs}), "sites": len({r.get("site") for r in rs})}


def flag(out: Dict[str, Any], hit: Dict[str, Any]) -> Dict[str, Any]:
    """The run whose answer is a KNOWN WRONG one: status ``known_wrong`` (never ok), not accepted, the answer kept as
    evidence only."""
    ev = {k: hit.get(k) for k in ("verdict", "source", "ref", "at", "scope", "why")}
    return dict(out, status=KNOWN_WRONG, accepted=False, output=None, unverified_output=out.get("output"),
                known_wrong=ev, why=f"known wrong: this answer was judged wrong before ({hit.get('source')} "
                                    f"{hit.get('ref') or ''}; {str(hit.get('why') or '')[:120]})".rstrip("; "))


def check(store: Optional[VerdictStore], tool: Dict[str, Any], value: Any, out: Dict[str, Any]) -> Dict[str, Any]:
    """``out`` unchanged, or flagged ``known_wrong`` when its answer matches a known wrong verdict."""
    if store is None or out.get("status") not in ("ok", "no_results"):
        return out
    hit = store.known_wrong(tool, value, out)
    return flag(out, hit) if hit else out


Checker = Callable[[Dict[str, Any], str, Dict[str, Any]], Any]


class StoreChecker:
    """The gate's checker: the store's verdict on the same answer first, else ``fallback`` (the independent checker),
    whose agreement / disagreement is recorded (source ``checker``: "correct" / "disputed")."""

    def __init__(self, store: VerdictStore, fallback: Optional[Checker] = None, record: bool = True):
        self.store, self.fallback, self.record = store, fallback, record
        self.asked: List[Dict[str, Any]] = []

    async def __call__(self, tool: Dict[str, Any], value: str, out: Dict[str, Any]) -> Dict[str, Any]:
        import inspect
        hit = self.store.lookup(C.tool_digest(tool), str(tool.get("site") or ""), value, out.get("status"),
                                out.get("output"))
        if hit and hit.get("verdict") in ("correct", "wrong", "incomplete", "disputed"):
            v = {"verdict": "agrees" if hit["verdict"] == "correct" else "disagrees", "source": "verdict store",
                 "stored": {k: hit.get(k) for k in ("verdict", "source", "ref", "scope", "at")},
                 "why": f"a stored {hit.get('source')} verdict on this answer: {hit.get('verdict')}"}
            self.asked.append(v)
            return v
        if self.fallback is None:
            v = {"verdict": "undeterminable", "why": "no stored verdict and no independent checker"}
            self.asked.append(v)
            return v
        r = self.fallback(tool, value, out)
        v = dict(await r if inspect.isawaitable(r) else r or {})
        v.setdefault("source", "independent checker")
        if self.record and v.get("verdict") in ("agrees", "disagrees"):
            self.store.add(tool=C.tool_digest(tool), site=str(tool.get("site") or ""), value=value,
                           status=out.get("status"), output=out.get("output"),
                           verdict="correct" if v["verdict"] == "agrees" else "disputed", source="checker",
                           ref="gate", why=str(v.get("why") or ""), workflow=str(tool.get("workflow") or ""))
        self.asked.append(v)
        return v


def rows_of(store: VerdictStore, verdict: str) -> Iterable[Dict[str, Any]]:
    return (r for r in store.rows() if r.get("verdict") == verdict)


def same_answer(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Do two runs give the same answer (the census's normalisation)?"""
    ka, kb = answer_key(a.get("status"), a.get("output")), answer_key(b.get("status"), b.get("output"))
    return ka is not None and ka == kb


__all__: Sequence[str] = ("KNOWN_WRONG", "VerdictStore", "StoreChecker", "answer_key", "answer_digest", "check", "flag",
                          "input_key", "input_ref", "same_answer", "site_key")
