"""Compiled desktop tools from GUEST-proved routines and draft workflows, and the guest route every MCP desktop tool
takes (docs/DESKTOP_TOOLS.md).

1. **The record.** A routine (``desktop_routine``) or a draft workflow (``draft_compile``) that replayed correctly IN
   THE GUEST on at least ``MIN_GUEST_PROOFS`` value sets that were not recorded writes ``compiled_desktop.json`` next
   to its storage entry (``mcpserver/desktop_compiled_tools.py``'s format, version 1): its name, typed parameters, the
   proof record (the guest runs: values, verdict, model calls), the app, the approval level, and the payload (the
   routine, or the draft record). The proof is pinned by the record's artifact digest.
2. **The MCP tool** (``desktop__<name>`` in the default profile, ``call_operation`` in the meta profile; both through
   ``desktop_compiled_tools.run_typed_tool``). A call with values runs the replay WITHOUT an approval prompt: starting
   it is the approval. A deviation stops it with its reason (a value check, the
   target's identity, the drift gate, the guest's guards). A workflow whose approval level opts into holds
   (``all`` / ``destructive``) is not run over MCP (no person to approve there).
3. **Never on the host.** Every compiled desktop tool -- these and the older compile classes -- is routed to the guest
   runner: the guest job agent (``execution/guest/desktop_e2e_job.py``, behind the foreground interlock) builds the AX
   tree bound to the recorded app (its ONE running process) and runs the checked steps there; a routine is replayed
   op by op from the host (``ReplayArch``) with every action in the guest. With no guest runner attached the call
   is refused with a typed reason: the foreground interlock's verdict (on a host: refused in every state).

The guest runner is a callable ``runner(op, timeout=..., **args) -> dict`` speaking the job agent's command protocol
(``GuestJobChannel``: one JSON command file per op over the VM's ssh wrapper, as ``desktop_e2e_host.Channel``). It is
attached explicitly (``create_server(desktop_guest_runner=...)``) or by ``ANYUSE_DESKTOP_GUEST_SESSION`` (the serving
job agent's session name; ``ANYUSE_DESKTOP_GUEST_SSH`` the ssh wrapper, default ``~/.anyuse/vm-tools/bin/vm-ssh``).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

COMPILE_ROUTINE = "routine"
COMPILE_DRAFT = "draft_workflow"
GUEST_CLASSES = (COMPILE_ROUTINE, COMPILE_DRAFT)
#: at least this many guest replays on value sets that were not recorded, all correct (the compilers' floor)
MIN_GUEST_PROOFS = 2
HOLD_LEVELS = ("all", "destructive")
GUEST_SESSION_ENV = "ANYUSE_DESKTOP_GUEST_SESSION"
GUEST_SSH_ENV = "ANYUSE_DESKTOP_GUEST_SSH"
DEFAULT_SSH = os.path.expanduser("~/.anyuse/vm-tools/bin/vm-ssh")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ROUTINE_FILE = "routine.json"

Runner = Callable[..., Dict[str, Any]]


# ---------------------------------------------------------------------------
# 1. the proof and the record
# ---------------------------------------------------------------------------

def guest_proof(rows: Sequence[Dict[str, Any]], *, recorded: Optional[Dict[str, Any]] = None,
                same: Optional[Callable[[str, Any], bool]] = None,
                job: str = "", session: str = "", commit: str = "") -> Dict[str, Any]:
    """The proof record of guest replays. ``rows``: one per replay -- ``values`` (the new values), ``ok`` (the
    harness's verdict on the replay's own expected end state), ``model_calls``, and optional ``designed_to_fail``
    (a variant meant to be refused: it must NOT succeed and is not counted as a proof). ``same(name, value)``
    says whether a value names the recorded one (default: equal to ``recorded[name]``). Proved when at least
    ``MIN_GUEST_PROOFS`` positive rows succeeded with 0 model calls, each changing at least one value, every
    recorded parameter took a new value in at least one of them, and no positive row failed."""
    recorded = {k: v for k, v in (recorded or {}).items()}
    same = same or (lambda name, value: name in recorded and recorded[name] == value)
    pos = [r for r in rows if not r.get("designed_to_fail")]
    neg = [r for r in rows if r.get("designed_to_fail")]
    problems = []
    changed_any = set()
    for r in pos:
        new = [k for k, v in (r.get("values") or {}).items() if not same(k, v)]
        if not new:
            problems.append("a proof replay without a new value")
        if not r.get("ok"):
            problems.append(f"a proof replay failed: {str(r.get('why') or r.get('stopped') or '')[:120]}")
        if int(r.get("model_calls") or 0):
            problems.append("a proof replay made model calls")
        if r.get("ok") and not int(r.get("model_calls") or 0):
            changed_any.update(new)
    for r in neg:
        if r.get("ok"):
            problems.append("a replay designed to be refused succeeded")
    never = sorted(set(recorded) - changed_any)
    if never:
        problems.append(f"parameter(s) never given a new value in a correct replay: {never}")
    good = [r for r in pos if r.get("ok") and not int(r.get("model_calls") or 0)
            and any(not same(k, v) for k, v in (r.get("values") or {}).items())]
    if len(good) < MIN_GUEST_PROOFS:
        problems.append(f"{len(good)} correct guest replay(s) on new values; {MIN_GUEST_PROOFS} are needed")
    return {"proved": not problems, "harness": "guest", "kind": "guest_replay",
            "fresh_inputs": len(good), "rows": [dict(r) for r in rows], "problems": problems,
            "job": str(job), "session": str(session), "commit": str(commit)}


def routine_same(routine: Dict[str, Any]) -> Callable[[str, Any], bool]:
    """``same`` for a routine: a value names the recorded one (a choice by key, recorded key or label)."""
    from . import desktop_routine as R
    by = {p["name"]: p for p in routine.get("params") or []}
    return lambda name, value: name in by and R._same_value(routine, by[name], value)


def _string_row(name: str, description: str, *, kind: str = "", enum: Sequence[str] = (),
                example: Any = None, required: bool = False) -> Dict[str, Any]:
    row: Dict[str, Any] = {"name": name, "type": "string", "required": required,
                           "description": description[:300]}
    if kind:
        row["kind"] = kind
    if enum:
        row["enum"] = list(dict.fromkeys(str(e) for e in enum if e))
    if example not in (None, ""):
        row["example"] = example
    return row


def routine_parameters(routine: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Typed parameter rows of a routine: every parameter is optional (left out = the recorded value). A choice
    lists its options' identity keys and recorded labels (``enum``)."""
    rows = []
    for p in routine.get("params") or []:
        if p["kind"] == "choice":
            opts = [o for o in p.get("options") or [] if o.get("key")]
            desc = ("one option of the recorded group, by its identity key or recorded label: "
                    + "; ".join(f"{o['key']} = {o['label']}" for o in opts))
            rows.append(_string_row(p["name"], desc, kind="choice",
                                    enum=[o["key"] for o in opts] + [o["label"] for o in opts],
                                    example=p.get("value")))
        elif p["kind"] == "setting":
            rows.append(_string_row(p["name"], "on or off", kind="setting", enum=("on", "off"),
                                    example=p.get("value")))
        else:
            rows.append(_string_row(p["name"], f"the {p['kind']} (recorded: {str(p.get('value'))[:60]!r})",
                                    kind=p["kind"], example=p.get("value")))
    return rows


def draft_parameters(draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Typed parameter rows of a draft workflow (every one required, as ``value_problems`` asks). A choice bound by
    identity lists its options' recorded titles and identifiers. Recorded examples are not copied (they may be
    vault markers or personal text)."""
    by_choice = {(s.get("choice") or {}).get("param"): s["choice"] for s in draft.get("steps") or []
                 if (s.get("choice") or {}).get("param")}
    rows = []
    for p in draft.get("parameters") or []:
        ch = by_choice.get(p["name"])
        enum = ([o["title"] for o in ch["options"]] + ["id:" + o["identifier"] for o in ch["options"]]
                if ch else ())
        rows.append(_string_row(p["name"], str(p.get("description") or f"a {p['type']} value"),
                                kind=str(p.get("type") or "text"), enum=enum, required=True))
    return rows


def compiled_record_for_routine(routine: Dict[str, Any], proof: Dict[str, Any], *, name: str, app: Dict[str, Any],
                                approval_level: str = "auto", compiled_at: str = "") -> Dict[str, Any]:
    """``compiled_desktop.json`` for a guest-proved routine. Refuses a routine that stops at a refusal, a gate or a
    hand-off (no tool), one without the routine format, and an unproved proof."""
    from . import desktop_routine as R
    from anyuse.mcpserver import desktop_compiled_tools as DT
    if not R.is_routine(routine):
        raise ValueError("not a routine (anyuse-desktop-routine/1)")
    stops = [c for c in routine.get("steps") or [] if (c.get("resolved") or {}).get("op") in R._STOP_OPS]
    if stops:
        raise ValueError("a routine that stops at a refusal, a gate or a hand-off is not a tool")
    if not proof.get("proved") or proof.get("harness") != "guest":
        raise ValueError("the routine is not proved in the guest: " + "; ".join(proof.get("problems") or [])[:300])
    for row in proof.get("rows") or []:
        if row.get("designed_to_fail"):
            continue
        steps, _goal, probs = R.instantiate(routine, row.get("values") or {})
        if probs or steps is routine["steps"]:
            raise ValueError(f"a proof row is not a replay on new values of this routine: {probs or 'no change'}")
    again = guest_proof(proof.get("rows") or [], recorded={p["name"]: p["value"] for p in routine["params"]},
                        same=routine_same(routine))
    if not again["proved"]:
        raise ValueError("the proof rows do not prove this routine: " + "; ".join(again["problems"])[:300])
    rec = DT.compiled_desktop_record(
        workflow=name, compile_class=COMPILE_ROUTINE, steps=[], proof=proof,
        parameters=routine_parameters(routine),
        workflow_template={"id": name, "name": name, "required_app": str(app.get("name") or "")},
        compiled_at=compiled_at or _now(),
        extra={"app": dict(app), "approval_level": approval_level,
               "payload": {"kind": COMPILE_ROUTINE, "routine": routine}})
    return rec


def compiled_record_for_draft(draft: Dict[str, Any], proof: Dict[str, Any], *, compiled_at: str = "") -> Dict[str, Any]:
    """``compiled_desktop.json`` for a draft workflow that is ``ready`` and was proved in the guest."""
    from anyuse.mcpserver import desktop_compiled_tools as DT
    if draft.get("status") != "ready":
        raise ValueError("the draft workflow is not ready")
    if not proof.get("proved") or proof.get("harness") != "guest":
        raise ValueError("the workflow is not proved in the guest: " + "; ".join(proof.get("problems") or [])[:300])
    from . import draft_compile as DC
    name = str(draft.get("workflow") or "")
    return DT.compiled_desktop_record(
        workflow=name, compile_class=COMPILE_DRAFT, steps=[], proof=proof,
        parameters=draft_parameters(draft),
        workflow_template={"id": name, "name": name, "required_app": str(draft.get("app") or "")},
        compiled_at=compiled_at or _now(),
        extra={"app": {"name": str(draft.get("app") or ""), "bundle_id": str(draft.get("bundle_id") or "")},
               "approval_level": DC.approval_level(draft),
               "payload": {"kind": COMPILE_DRAFT, "record": draft}})


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def routine_storage_id(routine: Dict[str, Any]) -> str:
    """The storage entry of a routine: ``workflow_routine-<sha12>/`` beside the workflows (no ``workflow.json``,
    so ``Agent.play`` and the workflow listing never see it; the MCP lister reads ``compiled_desktop.json``)."""
    from . import desktop_routine as R
    return "routine-" + R.routine_sha256(routine)[:12]


def write_routine_tool(storage, routine: Dict[str, Any], record: Dict[str, Any]) -> str:
    """Write the routine (``routine.json``) and its ``compiled_desktop.json`` into its storage entry."""
    from anyuse.mcpserver import desktop_compiled_tools as DT
    wid = routine_storage_id(routine)
    d = str(storage.get_workflow_dir(wid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ROUTINE_FILE), "w", encoding="utf-8") as fh:
        json.dump(routine, fh, ensure_ascii=False, indent=1)
    return DT.persist_compiled_desktop(storage, wid, record)


def write_draft_tool(storage, record: Dict[str, Any]) -> str:
    """Write ``compiled_desktop.json`` next to the saved draft workflow of the same name."""
    from . import draft_compile as DC
    from anyuse.mcpserver import desktop_compiled_tools as DT
    wf, rec = DC.find_saved(record["workflow"], storage)
    if wf is None or rec is None:
        raise ValueError(f"no saved draft workflow named {record['workflow']!r}")
    if (rec.get("verdict_keys") or {}).get("artifact_digest") != \
            (((record.get("payload") or {}).get("record") or {}).get("verdict_keys") or {}).get("artifact_digest"):
        raise ValueError("the saved workflow is not the one that was proved (another artifact)")
    return DT.persist_compiled_desktop(storage, wf.id, record)


# ---------------------------------------------------------------------------
# 2. the guest runner
# ---------------------------------------------------------------------------

class GuestJobChannel:
    """The job agent's command channel over the VM's ssh wrapper: ``cmd/<id>.json`` written atomically, the answer
    read from ``resp/<id>.json`` (``execution/guest/desktop_e2e_job.py serve``). Prompt resolution is submitted
    once; transport recovery only retrieves that job's receipt."""

    def __init__(self, session: str, ssh: str = DEFAULT_SSH, run=subprocess.run):
        if not _SESSION_RE.match(session or ""):
            raise ValueError("a guest session name is letters, digits, '.', '_' or '-'")
        self.session, self.ssh, self.run = session, ssh, run
        self.dir = f"anyuse-guest/e2e/{session}"
        self.n = None

    def _ssh(self, cmd: str, stdin: Optional[bytes] = None, timeout: float = 60):
        return self.run([self.ssh, cmd], input=stdin, capture_output=True, timeout=timeout)

    def _next_id(self) -> int:
        r = self._ssh(f"ls ~/{self.dir}/done ~/{self.dir}/cmd 2>/dev/null | grep -c json || true")
        try:
            return int((r.stdout or b"0").strip() or 0)
        except ValueError:
            return 0

    def _prompt_job(self, timeout: float, args: Dict[str, Any], *, clock_probe=False, decision_deadline=None) -> Dict[str, Any]:
        """Submit once; after any transport outcome, retrieve only that ID.

        A failed existence probe cannot establish absence after a mutation.
        This operation therefore has no existence probe or resubmission path.
        """
        op, cid = "resolve_system_prompt", "p" + uuid.uuid4().hex
        message = {"op": op, "args": args}
        if clock_probe:
            message["clock_probe"] = True
        if decision_deadline is not None:
            message["decision_deadline"] = decision_deadline
        payload = json.dumps(message, ensure_ascii=False, default=str).encode()
        wait = (f"i=0; while [ $i -lt {int(timeout * 5)} ]; do [ -f resp/{cid}.json ] && cat resp/{cid}.json "
                f"&& exit 0; sleep 0.2; i=$((i+1)); done; exit 3")
        retrieve = f"cd ~/{self.dir} && {wait}"
        submit = f"cd ~/{self.dir} && cat > cmd/.{cid}.tmp && mv cmd/.{cid}.tmp cmd/{cid}.json && {wait}"
        for attempt in range(4):
            try:
                response = self._ssh(submit, stdin=payload, timeout=timeout + 30) if attempt == 0 else \
                    self._ssh(retrieve, timeout=timeout + 30)
                if response.returncode == 0 and (response.stdout or b"").strip():
                    receipt = json.loads(response.stdout)
                    if type(receipt) is dict and receipt.get("id") == cid and receipt.get("op") == op:
                        return receipt
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
        return {"status": "error", "id": cid, "op": op, "reobserve_required": True, "automatic_retry": False,
                "detail": "guest prompt-resolution outcome is unknown; submitted once, never resubmitted"}

    def _resolve_prompt_once(self, timeout: float, args: Dict[str, Any]) -> Dict[str, Any]:
        limits = args.get("decision_limits")
        if args.get("mode") != "llm" or type(limits) is not dict:
            return self._prompt_job(timeout, args)
        started = time.monotonic()
        try:
            duration = float(limits["timeout_s"])
            sample = self._prompt_job(min(timeout, duration), {"where": "transport.clock", "mode": "manual"}, clock_probe=True)
            clock = sample.get("clock")
            remaining = duration - (time.monotonic() - started)
            if sample.get("status") != "done" or type(clock) is not dict or \
                    set(clock) != {"server_id", "monotonic_s"} or \
                    type(clock["server_id"]) is not str or re.fullmatch(r"[0-9a-f]{32}", clock["server_id"]) is None or \
                    type(clock["monotonic_s"]) not in (int, float) or not math.isfinite(clock["monotonic_s"]) or \
                    clock["monotonic_s"] <= 0 or not math.isfinite(remaining) or remaining <= 0:
                raise ValueError("guest clock sample or remaining deadline is unavailable")
            # Subtract the complete probe round trip. This maps the host's
            # remaining duration conservatively without assuming UTC clock sync.
            deadline = {"server_id": clock["server_id"], "monotonic_s": clock["monotonic_s"] + remaining}
            bounded_args = dict(args, decision_limits=dict(limits, timeout_s=remaining))
            return self._prompt_job(min(timeout, remaining), bounded_args, decision_deadline=deadline)
        except (KeyError, TypeError, ValueError, OSError):
            return {"status": "done", "resolution": {"format": "anyuse.system-prompt-resolution/1", "status": "manual",
                    "reason": "guest deadline could not be established before dispatch", "attempted": False,
                    "reobserve_required": False, "next_action": "stop", "decision": {
                        "model_calls": 0, "cost_usd": 0.0, "budget_charged_usd": 0.0, "budget_exhausted": True}}}

    def __call__(self, op: str, timeout: float = 420, **args) -> Dict[str, Any]:
        if op == "resolve_system_prompt":
            return self._resolve_prompt_once(timeout, args)
        if self.n is None:
            self.n = self._next_id()
        self.n += 1
        cid = f"q{os.getpid() % 100000:05d}-{self.n:05d}"
        payload = json.dumps({"op": op, "args": args}, ensure_ascii=False, default=str).encode()
        wait = (f"i=0; while [ $i -lt {int(timeout * 5)} ]; do [ -f resp/{cid}.json ] && cat resp/{cid}.json "
                f"&& exit 0; sleep 0.2; i=$((i+1)); done; exit 3")
        cmd = f"cd ~/{self.dir} && cat > cmd/.{cid}.tmp && mv cmd/.{cid}.tmp cmd/{cid}.json && {wait}"
        for attempt in range(4):
            try:
                if attempt == 0:
                    r = self._ssh(cmd, stdin=payload, timeout=timeout + 30)
                else:
                    chk = self._ssh(f"cd ~/{self.dir} && ls cmd/{cid}.json done/{cid}.json resp/{cid}.json 2>/dev/null")
                    if chk.returncode != 0 and b"json" not in (chk.stdout or b""):
                        r = self._ssh(cmd, stdin=payload, timeout=timeout + 30)
                    else:
                        r = self._ssh(f"cd ~/{self.dir} && {wait}", timeout=timeout + 30)
                if r.returncode == 0 and (r.stdout or b"").strip():
                    return json.loads(r.stdout)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
            time.sleep(2 + 3 * attempt)
        return {"status": "error", "detail": f"no answer from the guest for {cid} {op}"}


def configured_runner(env: Optional[Dict[str, str]] = None) -> Optional[Runner]:
    """The guest runner attached by ``ANYUSE_DESKTOP_GUEST_SESSION``, or None."""
    env = os.environ if env is None else env
    session = str(env.get(GUEST_SESSION_ENV) or "").strip()
    if not session:
        return None
    return GuestJobChannel(session, ssh=str(env.get(GUEST_SSH_ENV) or DEFAULT_SSH))


def host_refusal(where: str, surface: str = "mcp desktop tool") -> Dict[str, Any]:
    """The typed answer when no guest runner is attached: the foreground interlock's verdict for acting HERE (a
    host: refused in every state). Nothing is built, nothing sent. ``surface`` names the entry point in the
    refusal's ``where`` (an MCP desktop tool by default; the agent harness passes its own caller's, the CLI "cli")."""
    from . import foreground_interlock as FI
    v = FI.check(f"{surface}: {where}")
    reason = v.reason if not v.allowed else ("this process may send input, but a compiled desktop tool runs only "
                                             "through the guest job agent")
    return {"ok": False, "refused": True, "refused_by": "foreground_interlock", "route": "none",
            "refusal": {"kind": "foreground_interlock", "where": v.where, "reason": reason,
                        "environment": v.environment},
            "error": (f"refused by the foreground interlock: {reason}. A compiled desktop tool runs only in the "
                      f"guest: attach a guest runner ({GUEST_SESSION_ENV} names the serving job agent's session); "
                      "nothing was sent")}


class _Watch:
    """A runner wrapper that keeps the guest's typed interlock refusal (``refusal.kind`` foreground_interlock)."""

    def __init__(self, runner: Runner):
        self.runner, self.refusal, self.ops = runner, None, 0

    def __call__(self, op, **kw):
        self.ops += 1
        r = self.runner(op, **kw)
        if isinstance(r, dict) and (r.get("refusal") or {}).get("kind") == "foreground_interlock" \
                and self.refusal is None:
            self.refusal = r["refusal"]
        return r


# ---------------------------------------------------------------------------
# 3. running a tool in the guest
# ---------------------------------------------------------------------------

def run_routine(record: Dict[str, Any], arguments: Dict[str, Any], runner: Runner, *,
                sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """A routine tool: ``ReplayArch`` on the host (no planner, no picker: 0 model calls; the runtime checks of
    ``desktop_routine``) with every action sent to the guest job agent through ``runner``."""
    from . import desktop_e2e as X
    from . import desktop_e2e4 as W
    from . import desktop_routine as R
    routine = (record.get("payload") or {}).get("routine") or {}
    guest = _Watch(runner)
    arch = R.ReplayArch(guest, W.EffectMap4(), spend=None, inbox=None, sleep=sleep)
    steps, goal, probs = arch.prepare(routine, dict(arguments or {}))
    if probs:
        return {"ok": False, "error": "invalid parameters: " + "; ".join(probs)[:400], "steps": [],
                "parameter_problems": probs, "route": "guest", "served_by": "guest:ReplayArch"}
    rec = X.Rec(str(routine.get("task") or record.get("workflow") or "routine"), "mcp", 1, mode="compiled")
    try:
        arch.run(X.Task(rec.task, goal, "single-app", {}, [], {}), rec, compiled=steps)
    except X.RunStop as exc:
        rec.stopped = f"the guest stopped: {exc}"
    ok = rec.status == "completed" and not rec.stopped
    out = {"ok": ok, "route": "guest", "served_by": "guest:ReplayArch",
           "steps": [{"effect": (s.get("step") or {}).get("effect"), "app": (s.get("step") or {}).get("app"),
                      "outcome": s.get("outcome"), "why": s.get("why"), "check": s.get("check")}
                     for s in rec.steps],
           "model_calls": sum(rec.calls.values()), "actions": rec.actions_executed,
           "identity_checks": rec.__dict__.get("identity15") or []}
    if not ok:
        out["error"] = rec.stopped or "the routine did not complete"
    if guest.refusal is not None:
        out.update(refused=True, refused_by="foreground_interlock", refusal=guest.refusal)
    return out


def run_in_guest(record: Dict[str, Any], arguments: Dict[str, Any], runner: Runner) -> Dict[str, Any]:
    """Any other compiled desktop tool: the guest job agent's ``run_tool`` (an AX tree bound to the recorded app,
    the checked steps there). The guest's typed refusal comes back typed."""
    sent = {k: v for k, v in record.items() if not k.startswith("_")}
    r = runner("run_tool", record=sent, arguments=dict(arguments or {}), timeout=600)
    if not isinstance(r, dict):
        return {"ok": False, "error": "no answer from the guest", "route": "guest"}
    if (r.get("refusal") or {}).get("kind") == "foreground_interlock":
        return {"ok": False, "refused": True, "refused_by": "foreground_interlock", "refusal": r["refusal"],
                "error": r.get("detail") or "refused by the foreground interlock", "route": "guest", "steps": []}
    if r.get("status") != "done":
        return {"ok": False, "error": str(r.get("detail") or r.get("status") or "the guest failed"),
                "route": "guest", "steps": r.get("steps") or []}
    out = {k: r[k] for k in ("ok", "steps", "stopped", "app", "served_by", "holds") if k in r}
    out["ok"], out["route"] = bool(r.get("ok")), "guest"
    if not out["ok"]:
        out["error"] = str(r.get("stopped") or r.get("detail") or "a step failed")
    return out
