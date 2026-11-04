"""Verdicts on draft-compiled workflows, in the shared verdict store (``execution/verdict_store.py``,
imported, unchanged).

A compile or a replay that an audit judged WRONG never repeats silently:

* a COMPILE is keyed by its artifact digest (the steps, parameters, template, graph and gates -
  stable over saves and runs), the input ``"compile"`` and an answer naming the artifact and its
  parameters. ``check_compile`` is ``verdict_store.StoreChecker``: the store first (a stored
  ``wrong`` / ``incomplete`` / ``disputed`` keeps the workflow a draft, whatever the proof says),
  else the fresh-input proof as the independent checker (not recorded: it reruns every time);
* a REPLAY is keyed by the same artifact, the values (canonical JSON) and an answer naming the
  bound steps - what that replay will do, known before it runs. ``replay_refusal`` refuses a
  replay whose compile or whose exact replay was judged wrong, with the verdict's source,
  reference and reason;
* ``record`` stores a person's (``audit``) or a checker's verdict on either.

The site key is ``draft:<app>``, so draft verdicts never mix with the web tools' sites. The store
is ``ANYUSE_VERDICT_STORE`` or ``~/.anyuse/verdicts/verdicts.jsonl``. Nothing here contacts anything.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Any, Dict, Optional

from anyuse.execution import verdict_store as VS
from anyuse.execution.demo_compile import tool_digest

STORE_ENV = "ANYUSE_VERDICT_STORE"
COMPILE_INPUT = "compile"


def store_path() -> str:
    raw = os.environ.get(STORE_ENV, "").strip()
    if raw:
        return os.path.expanduser(raw)
    from anyuse.paths import anyuse_data_path
    return str(anyuse_data_path("verdicts", "verdicts.jsonl"))


def default_store() -> VS.VerdictStore:
    return VS.VerdictStore(store_path())


def site_of(rec: Dict[str, Any]) -> str:
    return "draft:" + str(rec.get("app") or rec.get("kind") or "unknown").strip().lower()


def tool_of(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The 'tool' a verdict is about: the compiled artifact (never its name or id)."""
    return {"kind": "draft_workflow", "artifact": (rec.get("verdict_keys") or {}).get("artifact_digest"),
            "site": site_of(rec), "workflow": str(rec.get("workflow") or "")}


def _tool_key(rec: Dict[str, Any]) -> str:
    t = tool_of(rec)
    return tool_digest({k: t[k] for k in ("kind", "artifact", "site")})


def compile_out(rec: Dict[str, Any]) -> Dict[str, Any]:
    keys = rec.get("verdict_keys") or {}
    return {"status": "ok", "output": {"name": f"compile {keys.get('artifact_digest')} "
                                               f"{keys.get('parameters_digest')}"}}


def replay_input(values: Dict[str, Any]) -> str:
    return json.dumps({k: values[k] for k in sorted(values)}, ensure_ascii=False)


def replay_out(rec: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    from anyuse.execution.draft_compile import bound_steps
    steps = [s.to_dict() for s in bound_steps(rec, values)]
    digest = hashlib.sha256(json.dumps(steps, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"status": "ok", "output": {"name": f"replay {digest}"}}


def lookup(store: VS.VerdictStore, rec: Dict[str, Any], values: Optional[Dict[str, Any]] = None
           ) -> Optional[Dict[str, Any]]:
    """The latest verdict on this compile (``values`` None) or on this replay."""
    out = compile_out(rec) if values is None else replay_out(rec, values)
    value = COMPILE_INPUT if values is None else replay_input(values)
    return store.lookup(_tool_key(rec), site_of(rec), value, out["status"], out["output"])


#: the longest replay value a verdict is recorded on (draft_schema.MAX_TYPED)
MAX_VALUE = 20000


def values_problem(rec: Dict[str, Any], values: Dict[str, Any]) -> str:
    """Why ``values`` cannot name a replay of ``rec``: a replay binds every parameter by name, so
    a verdict on other names, or on some of them, would never match a run."""
    names = {str(p.get("name")) for p in rec.get("parameters") or [] if isinstance(p, dict)}
    if set(values) != names:
        return (f"'values' must name exactly this workflow's parameters ({', '.join(sorted(names)) or 'none'}), "
                f"got {', '.join(sorted(map(str, values)))[:200] or 'none'}")
    long = [k for k, v in values.items() if not isinstance(v, str) or len(v) > MAX_VALUE]
    if long:
        return f"a value is text of at most {MAX_VALUE} characters: {', '.join(sorted(long))[:200]}"
    return ""


def _at() -> str:
    # sub-second: the store orders by this text and keeps the FIRST of equal times, so a
    # "wrong" recorded in the same second as a "correct" would otherwise lose to it
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(rec: Dict[str, Any], verdict: str, *, source: str = "audit", why: str = "", ref: str = "",
           values: Optional[Dict[str, Any]] = None, store: Optional[VS.VerdictStore] = None
           ) -> Optional[Dict[str, Any]]:
    """Store a verdict on the compile (``values`` None) or on a replay with ``values``; None when
    the same verdict is stored already and is still the latest one."""
    if verdict not in VS.VERDICTS:
        raise ValueError(f"a verdict is one of {', '.join(VS.VERDICTS)}, not {verdict!r}")
    if source not in VS.SOURCES:
        raise ValueError(f"a source is one of {', '.join(VS.SOURCES)}, not {source!r}")
    store = store or default_store()
    out = compile_out(rec) if values is None else replay_out(rec, values)
    value = COMPILE_INPUT if values is None else replay_input(values)
    at = _at()
    kw = dict(tool=_tool_key(rec), site=site_of(rec), value=value, status=out["status"], output=out["output"],
              verdict=verdict, source=source, why=why[:300], workflow=str(rec.get("workflow") or ""), at=at)
    row = store.add(ref=ref, **kw)
    if row is None:
        # the store keeps one row per (key, source, reference, verdict): "wrong", then "correct", then
        # "wrong" again would drop the last one and leave "correct" the latest. Store it again.
        cur = lookup(store, rec, values)
        if cur is not None and cur.get("verdict") != verdict:
            row = store.add(ref=f"{ref} (again {at})".strip(), **kw)
    return row


def _say(hit: Dict[str, Any]) -> str:
    return (f"{hit.get('source')} {hit.get('ref') or ''} at {hit.get('at')}: {hit.get('verdict')}"
            + (f" ({str(hit.get('why'))[:160]})" if hit.get("why") else "")).replace("  ", " ")


async def check_compile(rec: Dict[str, Any], proof: Dict[str, Any],
                        store: Optional[VS.VerdictStore] = None) -> Dict[str, Any]:
    """``StoreChecker``'s order: a stored verdict on this compile first, else the proof."""
    store = store or default_store()

    def proof_checker(_tool, _value, _out):
        return {"verdict": "agrees" if proof.get("passed") else "disagrees",
                "source": "fresh-input proof", "why": str(proof.get("reason") or "")[:300]}
    checker = VS.StoreChecker(store, fallback=proof_checker, record=False)
    t = tool_of(rec)
    # StoreChecker digests the tool it is given: the same keys ``record`` stores under
    v = await checker({k: t[k] for k in ("kind", "artifact", "site")}, COMPILE_INPUT, compile_out(rec))
    if v.get("source") == "verdict store":
        hit = lookup(store, rec) or {}
        v["reason"] = f"a stored verdict on this compile: {_say(hit)}"
    return v


def replay_refusal(rec: Dict[str, Any], values: Dict[str, Any],
                   store: Optional[VS.VerdictStore] = None) -> Optional[Dict[str, Any]]:
    """The known-wrong verdict that forbids this replay (on the compile or on this exact replay)."""
    store = store or default_store()
    for scope, hit in (("compile", lookup(store, rec)), ("replay", lookup(store, rec, values))):
        if hit and hit.get("verdict") == "wrong":
            return {"scope": scope, "verdict": hit, "why": f"known wrong: this {scope} was judged wrong "
                                                           f"before ({_say(hit)})"}
    return None
