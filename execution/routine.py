"""Runtime for skill/routine manifests: run, run_until, hand_off, resume.

A run is a sequence of steps from ONE validated manifest (``skill_manifest``),
executed in declared order (a topological order of the binding DAG). Every
state change is an append-only, hash-chained **receipt** on disk; the in-memory
state of a run is only ever rebuilt from those receipts. That gives:

* **durable resume** — a new process can continue a run from its receipts;
  completed steps are never re-run;
* **fresh checks** — before any step executes (including right after a resume
  or an approval) its preconditions and its runner's ``fresh_check`` (e.g. the
  compiled proof is still the pinned one) are evaluated against current state;
* **no repeated irreversible action** — a step whose outcome is uncertain
  (timeout or crash after dispatch) is retried automatically only if it is
  read-only and idempotent; otherwise the run stops with ``needs_reconcile``
  and only a human can say whether it happened;
* **an audit log** — model proposals/decisions, approvals, hand-offs and
  reconciles are receipts in the same chain.

What a model may do (docs/ROUTINES.md): on an ``agent`` step, choose ONE value
from an explicit candidate list (the step's ``choose_from``), or abstain. It
cannot create bindings, approve, skip a step, or declare success — success is
always the step's own typed outputs plus its postconditions. Its autonomy is the
step's declared level:

* ``rule`` — no model; deterministic (``action.rule == "only"`` picks the single
  candidate, anything else hands off to a human);
* ``model_proposes_human_confirms`` — the proposal is audited and handed off;
  a human accepts it (or chooses another candidate) on resume;
* ``model_decides_within_limits`` — read-only/reversible steps only (validated),
  a per-step decision count and US$ cap reserved BEFORE each call, every call
  audited; limit reached, abstention or an out-of-list answer hands off.

Human inbox (docs/HUMAN_INBOX.md): every ``handed_off`` stop can become a
card in a durable inbox (``execution/human_inbox.py``). A runner result that
carries a detected block page (CAPTCHA, challenge, 403, login wall) is a
``blocked`` receipt and a hand-off card, not a dead end. What happens after the
person finishes is governed by ``ANYUSE_CHALLENGE_POLICY``: by default the
person does the blocked step themselves and hands back the result, and the host
becomes human-only for the rest of the run; AnyUse never re-dispatches into a
session a person cleared unless the host is on the person's own-sites allowlist
(or it is a login to the person's own account) under
``continue_in_own_session``. Nothing here solves or evades a challenge.

No network, model or desktop is touched here; runners and the advisor are
injected. ``CompiledToolRunner`` is the one production runner and calls the
existing ``mcpserver.compiled_tools.run_compiled`` read-only path.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from anyuse.execution.skill_manifest import (
    EFFECT_CLASSES, canonical, check_value, cross_app_problems, evaluate,
    manifest_digest, resolve_ref, validate_manifest,
)

_RUN_ID = re.compile(r"^[0-9a-f]{8,64}$")
_RANK = {c: i for i, c in enumerate(EFFECT_CLASSES)}


class ManifestError(ValueError):
    pass


class ReceiptError(RuntimeError):
    pass


def _sha(data: Any) -> str:
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def inputs_digest(bound: Mapping[str, Any]) -> str:
    return _sha(dict(bound))


HUMAN_CHANNEL_CLI = "cli"
HUMAN_CHANNEL_INBOX = "cli-inbox"
#: The connected MCP client showed the person a typed form (elicitation) and
#: returned their answer, the way Claude Code shows its own permission prompts.
HUMAN_CHANNEL_ELICIT = "mcp-elicitation"
#: Channels whose human receipts count as out of band (docs/HUMAN_INBOX.md).
HUMAN_CHANNELS = frozenset({HUMAN_CHANNEL_CLI, HUMAN_CHANNEL_INBOX, HUMAN_CHANNEL_ELICIT})
#: A clearance written after a person solved a challenge live in this run's
#: own page (human_solves_live). Not an approval channel.
LIVE_SOLVE_CHANNEL = "live-solve"
#: human-step card kinds that only ``anyuse inbox do`` may complete
INBOX_ONLY_CARDS = frozenset({"value", "login_or_2fa"})
_ACTION_PINS = ("kind", "ref", "proof_id", "template_digest", "digest")


def confirm_digest(purpose: str, manifest_digest: str, run_id: str,
                   step: Mapping[str, Any], idig: str) -> str:
    """What a person confirms out of band (``anyuse routine approve/reconcile``).

    Binds the purpose, the manifest version (digest), the run, the step id,
    the exact bound-inputs digest, the step's declared effects and the action's
    pins (compiled proof id / template digest / sub-skill digest). Any change to
    one of them yields a different digest, so a confirmation cannot be replayed
    onto a different request, effect or version.
    """
    return _sha({"purpose": purpose, "manifest_digest": manifest_digest,
                 "run_id": run_id, "step": step["id"], "inputs_digest": idig,
                 "effects": step.get("effects"),
                 "action": {k: step["action"].get(k) for k in _ACTION_PINS}})


# ---------------------------------------------------------------------------
# durable receipts
# ---------------------------------------------------------------------------

class ReceiptStore:
    """Append-only, hash-chained receipts, one directory per run.

    Each receipt is written to a temp file, fsynced, then hard-linked to its
    final sequence name — the link fails if that name exists, so two writers
    cannot both append receipt N. ``load`` re-verifies the whole chain.
    """

    def __init__(self, base_dir: str):
        self.base_dir = str(base_dir)

    def _dir(self, run_id: str) -> str:
        if not _RUN_ID.match(str(run_id)):
            raise ReceiptError("malformed run id")
        return os.path.join(self.base_dir, run_id)

    def create(self, run_id: str, meta: Mapping[str, Any]) -> None:
        path = self._dir(run_id)
        os.makedirs(path, exist_ok=False)
        self._write_new(os.path.join(path, "run.json"), dict(meta))

    def meta(self, run_id: str) -> Dict[str, Any]:
        try:
            with open(os.path.join(self._dir(run_id), "run.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except FileNotFoundError:
            raise ReceiptError("unknown run id") from None
        if meta.get("meta_hash") != _sha({k: v for k, v in meta.items() if k != "meta_hash"}):
            raise ReceiptError("run metadata was modified")
        return meta

    @staticmethod
    def _write_new(final: str, data: Mapping[str, Any]) -> None:
        d = os.path.dirname(final)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.link(tmp, final)          # atomic, and refuses to overwrite
        except FileExistsError:
            raise ReceiptError(f"concurrent append: {os.path.basename(final)} exists") from None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def load(self, run_id: str) -> List[Dict[str, Any]]:
        path = self._dir(run_id)
        names = sorted(n for n in os.listdir(path) if re.fullmatch(r"\d{6}\.json", n))
        out: List[Dict[str, Any]] = []
        prev = self.meta(run_id)["meta_hash"]
        for i, name in enumerate(names):
            with open(os.path.join(path, name), encoding="utf-8") as fh:
                rec = json.load(fh)
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("seq") != i or rec.get("prev") != prev or rec.get("hash") != _sha(body):
                raise ReceiptError(f"receipt chain broken at {name}")
            prev = rec["hash"]
            out.append(rec)
        return out

    def append(self, run_id: str, record: Mapping[str, Any]) -> Dict[str, Any]:
        chain = self.load(run_id)
        prev = chain[-1]["hash"] if chain else self.meta(run_id)["meta_hash"]
        body = {**dict(record), "seq": len(chain), "prev": prev}
        rec = {**body, "hash": _sha(body)}
        self._write_new(os.path.join(self._dir(run_id), f"{len(chain):06d}.json"), rec)
        return rec

    def head(self, run_id: str) -> str:
        chain = self.load(run_id)
        return chain[-1]["hash"] if chain else self.meta(run_id)["meta_hash"]


# ---------------------------------------------------------------------------
# run state, rebuilt only from receipts
# ---------------------------------------------------------------------------

class RunState:
    def __init__(self, meta: Mapping[str, Any], chain: List[Mapping[str, Any]]):
        self.run_id = meta["run_id"]
        self.inputs = dict(meta["inputs"])
        self.outputs: Dict[str, Dict[str, Any]] = {}
        self.last: Dict[str, Mapping[str, Any]] = {}
        self.approvals: set = set()
        self.approval_records: List[Mapping[str, Any]] = []
        self.decisions: Dict[str, List[Mapping[str, Any]]] = {}
        self.clearances: Dict[str, Mapping[str, Any]] = {}
        self.human_only_hosts: set = set()
        self.cancelled: Optional[Mapping[str, Any]] = None
        self.head = chain[-1]["hash"] if chain else meta["meta_hash"]
        for rec in chain:
            t, step = rec.get("type"), rec.get("step")
            if t == "step" or t == "reconcile":
                self.last[step] = rec
                if rec.get("status") == "completed":
                    self.outputs[step] = dict(rec.get("outputs") or {})
            elif t == "clearance":
                self.clearances[step] = rec
            elif t == "host_policy" and rec.get("mode") == "human_only":
                self.human_only_hosts.add(str(rec.get("host") or ""))
            elif t == "cancelled":
                self.cancelled = rec
            elif t == "approval":
                self.approvals.add((step, rec.get("inputs_digest")))
                self.approval_records.append(rec)
            elif t == "decision":
                self.decisions.setdefault(step, []).append(rec)

    def completed(self, step_id: str) -> bool:
        return step_id in self.outputs

    def blocked(self, step_id: str) -> Optional[Mapping[str, Any]]:
        """The step's live ``blocked`` receipt, unless a person cleared it since."""
        last = self.last.get(step_id)
        if not last or last.get("status") != "blocked":
            return None
        clear = self.clearances.get(step_id)
        if clear and int(clear.get("seq", -1)) > int(last.get("seq", -1)):
            return None
        return last

    def cleared(self, step_id: str) -> Optional[Mapping[str, Any]]:
        """A person's clearance that post-dates the step's blocked receipt."""
        last = self.last.get(step_id)
        clear = self.clearances.get(step_id)
        if (clear and last and last.get("status") == "blocked"
                and int(clear.get("seq", -1)) > int(last.get("seq", -1))):
            return clear
        return None

    def scope(self, bound=None, outputs=None) -> Dict[str, Any]:
        return {"run": self.inputs, "in": dict(bound or {}),
                "out": dict(outputs or {}), "steps": self.outputs}


# ---------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------

def outcome(ok: bool, outputs=None, error: str = "", transient: bool = False,
            uncertain: bool = False, effect_class: str = "read_only",
            provenance=None, blocked=None) -> Dict[str, Any]:
    """``blocked`` is a ``BlockedState.to_dict()`` when the site served a
    block page (challenge, CAPTCHA, 403, login wall) instead of the content."""
    out = {"ok": ok, "outputs": dict(outputs or {}), "error": error,
           "transient": transient, "uncertain": uncertain,
           "effect_class": effect_class, "provenance": dict(provenance or {})}
    if blocked and blocked.get("blocked"):
        out["blocked"] = dict(blocked)
    return out


#: A step output that is a value only the human knows (inbox ``value`` card).
#: Receipts carry this placeholder, never the value; the engine reveals it
#: only in memory, immediately before a runner is called.
SECRET_KEY = "$inbox_value"


def is_secret_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {SECRET_KEY}


class CompiledToolRunner:
    """Runs a ``compiled_tool`` step through ``compiled_tools.run_compiled``.

    ``fresh_check`` re-reads the compiled record and refuses unless the proof
    id and template digest are EXACTLY the ones pinned in the manifest, the
    proof is current, and the record is runnable without a human. That makes
    the manifest's proof reference immutable: re-compiling a tool silently
    changes nothing in a routine; it stops it.
    """

    #: The compiled rung is a named HTTP client; it never shares a browser
    #: session a person cleared, so ``continue_in_own_session`` falls back to
    #: ``human_does_step`` for its steps (docs/HUMAN_INBOX.md).
    supports_attached_session = False

    def __init__(self, storage=None, hosts_path: Optional[str] = None, executor=None,
                 require_proved: bool = False):
        self.storage, self.hosts_path, self.executor = storage, hosts_path, executor
        # Default MCP profile: a record whose proof status is not "proved"
        # (e.g. "checked (selection not held out)") is refused.
        self.require_proved = require_proved

    def _row(self, ref: str) -> Optional[Mapping[str, Any]]:
        from anyuse.mcpserver import compiled_tools
        for row in compiled_tools.list_compiled(self.storage, self.hosts_path):
            if row.get("workflow") == ref:
                return row
        return None

    def fresh_check(self, step: Mapping[str, Any], bound: Mapping[str, Any]) -> Optional[str]:
        action = step["action"]
        row = self._row(action["ref"])
        if row is None:
            return f"compiled tool {action['ref']!r} is not present"
        proof = row.get("proof") or {}
        if proof.get("id") != action.get("proof_id"):
            return "the compiled proof changed since the manifest pinned it"
        if proof.get("template_digest") != action.get("template_digest"):
            return "the compiled template changed since the manifest pinned it"
        if not proof.get("current") or not row.get("runnable_without_human"):
            return "the compiled tool is not currently runnable without a human"
        if self.require_proved and str(proof.get("status") or "proved") != "proved":
            return f"the compiled tool's proof status is {proof.get('status')!r}, not 'proved'"
        if _RANK.get(row.get("risk_class"), 99) > _RANK[step["effects"]["class"]]:
            return "the compiled tool's risk class exceeds the step's permitted effects"
        return None

    def execute(self, step, bound, ctx) -> Dict[str, Any]:
        from anyuse.mcpserver import compiled_tools
        res = compiled_tools.run_compiled(step["action"]["ref"], dict(bound),
                                          storage=self.storage, executor=self.executor,
                                          hosts_path=self.hosts_path,
                                          require_proved=self.require_proved,
                                          post_card=False)
        prov = dict(res.get("provenance") or {})
        if prov.get("proof_id") and prov["proof_id"] != step["action"].get("proof_id"):
            return outcome(False, error="served by a different proof than pinned")
        if not res.get("ok"):
            # a search that listed no result with the asked-for name answers the same on a retry
            return outcome(False, error=str(res.get("error") or "compiled tool failed"),
                           transient=not res.get("blocked") and not res.get("not_found"), provenance=prov,
                           blocked=res.get("blocked") if isinstance(res.get("blocked"), dict)
                           else None)
        result = res.get("result")
        return outcome(True, result if isinstance(result, dict) else {"items": result},
                       provenance={**prov, "llm_calls": res.get("llm_calls", 0)})


class SkillRunner:
    """Composition: runs a pinned sub-manifest (``action.kind == "skill"``).

    Limits: the sub-manifest must be all-``proved`` (no nested hand-off), and
    its digest must equal the one pinned in the routine.
    """

    def __init__(self, registry: Mapping[str, Mapping[str, Any]], runners, store):
        self.registry, self.runners, self.store = registry, runners, store

    def _sub(self, step) -> Tuple[Optional[Mapping[str, Any]], Optional[str]]:
        sub = self.registry.get(step["action"]["ref"])
        if sub is None:
            return None, f"skill {step['action']['ref']!r} is not registered"
        if manifest_digest(sub) != step["action"].get("digest"):
            return None, "the skill changed since the routine pinned it"
        if any(s["mode"] != "proved" for s in sub.get("steps") or []):
            return None, "v0 composes only all-proved skills"
        return sub, None

    def fresh_check(self, step, bound) -> Optional[str]:
        sub, why = self._sub(step)
        if why:
            return why
        for sstep in sub["steps"]:
            runner = self.runners.get(sstep["action"]["kind"])
            if runner is None:
                return f"no runner for {sstep['action']['kind']!r}"
        return None

    def execute(self, step, bound, ctx) -> Dict[str, Any]:
        sub, why = self._sub(step)
        if why:
            return outcome(False, error=why)
        res = Engine(sub, self.runners, self.store).run(dict(bound))
        if res["status"] != "completed":
            return outcome(False, error=f"sub-skill {res['status']}: {res.get('error', '')}")
        return outcome(True, res["outputs"], provenance={"sub_run_id": res["run_id"]})


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, manifest: Mapping[str, Any], runners: Mapping[str, Any],
                 store: ReceiptStore, advisor=None,
                 clock: Callable[[], float] = time.time,
                 id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
                 human_channel: Optional[str] = None, inbox=None,
                 origin: Optional[str] = None, live_solver=None, shadow_ranker=None):
        errors = validate_manifest(manifest)
        if errors:
            raise ManifestError("; ".join(errors))
        self.manifest = json.loads(canonical(manifest))   # frozen private copy
        self.digest = manifest_digest(self.manifest)
        self.steps = list(self.manifest["steps"])
        self.runners, self.store, self.advisor = dict(runners), store, advisor
        self.clock, self.id_factory = clock, id_factory
        # When set (the default MCP profile uses "cli"), an approval or a
        # reconcile counts only if it was written through that out-of-band
        # channel with a matching confirm_digest; approvals passed to
        # resume() are refused.
        self.human_channel = human_channel
        # Optional ``human_inbox.Inbox``: every handed_off stop posts a card,
        # and it keeps the values of ``value`` cards (never in receipts).
        self.inbox = inbox
        # Who starts runs through this engine (user_task | scheduled | bench |
        # bulk), written into each new run's hash-covered metadata. A resumed
        # run keeps the origin it was started with. ``human_solves_live`` is
        # refused outright for bench and applies only to the origins in
        # ANYUSE_LIVE_SOLVE_ORIGINS (execution/live_solve.py).
        from anyuse.execution import human_inbox as _hi
        if origin is not None and origin not in _hi.ORIGINS:
            raise ValueError(f"unknown run origin {origin!r}")
        _hi.refuse_bench_live_solve(origin)
        self.origin = origin
        # Optional ``live_solve.LiveSolver``: with human_solves_live, a person
        # solves a challenge live and THIS run continues.
        self.live_solver = live_solver
        # Optional SHADOW ranker for agent-step hand-offs (resume points):
        # ``rank(step_id, candidates, context)``; its pick goes to its own log
        # (execution/openjev.py), never into a receipt, a card or a decision.
        # None = ANYUSE_ROUTINE_SHADOW_RANKER (default none).
        self.shadow_ranker = shadow_ranker

    # -- public API ---------------------------------------------------------

    def run(self, inputs: Mapping[str, Any]) -> Dict[str, Any]:
        return self.run_until(inputs, None)

    def run_until(self, inputs: Mapping[str, Any], stop_after: Optional[str]) -> Dict[str, Any]:
        if stop_after is not None and stop_after not in {s["id"] for s in self.steps}:
            raise ValueError(f"unknown step {stop_after!r}")
        run_id = self.id_factory()
        problems = self._check_run_inputs(inputs)
        if problems:
            return self._result("refused", run_id, error="; ".join(problems))
        meta = {"run_id": run_id, "manifest": self.manifest["name"],
                "version": self.manifest["version"], "manifest_digest": self.digest,
                "inputs": dict(inputs), "created": self.clock()}
        if self.origin is not None:
            meta["origin"] = self.origin
        meta["meta_hash"] = _sha(meta)
        self.store.create(run_id, meta)
        return self._drive(run_id, stop_after=stop_after)

    def hand_off(self, run_id: str) -> Dict[str, Any]:
        """Current state and what the next step expects, without running it."""
        state = self._state(run_id)
        nxt = self._next(state)
        if nxt is None:
            return self._completed(state)
        bound, errs = self._bind(nxt, state)
        return self._result("handed_off", run_id, handoff=self._handoff(
            state, nxt, "paused", bound if not errs else {}, extra={"binding_errors": errs}))

    def resume(self, run_id: str, *, expected_head: Optional[str] = None,
               human: Optional[Mapping[str, Any]] = None,
               approval: Optional[Mapping[str, Any]] = None,
               reconcile: Optional[Mapping[str, Any]] = None,
               stop_after: Optional[str] = None) -> Dict[str, Any]:
        try:
            state = self._state(run_id)
        except ReceiptError as exc:
            return self._result("refused", run_id, error=str(exc))
        if expected_head is not None and not self._head_ok(run_id, expected_head, state):
            return self._result("refused", run_id,
                                error="stale resume: the run advanced since this hand-off")
        if self.human_channel and (approval or reconcile):
            return self._result("refused", run_id, error=(
                "approval and reconcile must come through the out-of-band human channel "
                f"({self.human_channel!r}: anyuse routine approve/reconcile)"))
        by_id = {s["id"]: s for s in self.steps}
        if reconcile:
            sid = str(reconcile.get("step"))
            last = state.last.get(sid)
            if not last or last.get("status") != "uncertain":
                return self._result("refused", run_id, error="nothing to reconcile for that step")
            if reconcile.get("outcome") == "completed":
                outs, errs = self._typed_outputs(by_id[sid], reconcile.get("outputs") or {})
                if errs:
                    return self._result("refused", run_id, error="; ".join(errs))
                self._append(run_id, {"type": "reconcile", "step": sid, "status": "completed",
                                      "actor": "human", "outputs": outs})
            elif reconcile.get("outcome") == "not_done":
                self._append(run_id, {"type": "reconcile", "step": sid, "status": "not_done",
                                      "actor": "human"})
            else:
                return self._result("refused", run_id, error="reconcile outcome must be "
                                    "completed or not_done")
        if approval:
            sid = str(approval.get("step"))
            step = by_id.get(sid)
            if step is None or not (step.get("approval") or {}).get("required"):
                return self._result("refused", run_id, error="that step takes no approval")
            self._append(run_id, {"type": "approval", "step": sid, "actor": "human",
                                  "inputs_digest": str(approval.get("inputs_digest") or ""),
                                  "effects": step["effects"]})
        if human is not None and str(human.get("step")) not in by_id:
            return self._result("refused", run_id, error="unknown step in human input")
        return self._drive(run_id, stop_after=stop_after, human=human)

    def human_approve(self, run_id: str, step_id: str, confirm: str, *,
                      channel: str = HUMAN_CHANNEL_CLI,
                      gate: Optional[Callable[[Mapping[str, Any], str], Optional[str]]] = None
                      ) -> Dict[str, Any]:
        """Record a person's approval for the PENDING step, out of band.

        Refused unless the run belongs to this exact manifest version, the step
        is the next one and requires approval, its inputs bind, its
        preconditions hold now, and ``confirm`` equals the approval digest the
        hand-off showed (same run, step, inputs, effects and pins). ``gate``
        (the CLI's terminal gate, ``execution/human_gate.py``) is then shown
        the effect summary and must return ``None`` before anything is written.
        """
        try:
            state = self._state(run_id)
        except ReceiptError as exc:
            return self._result("refused", run_id, error=str(exc))
        step = self._next(state)
        if step is None or step["id"] != step_id:
            return self._result("refused", run_id, error=(
                f"step {step_id!r} is not the run's pending step"))
        if not (step.get("approval") or {}).get("required"):
            return self._result("refused", run_id, error="that step takes no approval")
        bound, errs = self._bind(step, state)
        if errs:
            return self._result("refused", run_id, error="; ".join(errs))
        idig = inputs_digest(bound)
        want = confirm_digest("approve", self.digest, run_id, step, idig)
        if str(confirm or "") != want:
            return self._result("refused", run_id, error=(
                "confirmation digest does not match this run's pending request/effect"))
        failed_pre = self._pre(step, state, bound)
        if failed_pre:
            return self._result("refused", run_id,
                                error="precondition(s) false now: " + ", ".join(failed_pre))
        pins = {k: step["action"].get(k) for k in _ACTION_PINS}
        if gate is not None:
            why = gate({"purpose": "approve", "manifest": self.manifest["name"],
                        "version": self.manifest["version"], "run_id": run_id,
                        "step": step_id, "effects": step["effects"], "action": pins,
                        "bound_inputs": bound, "inputs_digest": idig}, want)
            if why:
                return self._result("refused", run_id, error=why)
        rec = self._append(run_id, {
            "type": "approval", "step": step_id, "actor": "human", "channel": channel,
            "inputs_digest": idig, "approval_digest": want, "effects": step["effects"],
            "action": {k: step["action"].get(k) for k in _ACTION_PINS},
            "manifest_version": self.manifest["version"]})
        return {**self._result("approved", run_id), "step": step_id, "receipt": rec["hash"]}

    def human_reconcile(self, run_id: str, step_id: str, outcome_: str, confirm: str,
                        outputs: Optional[Mapping[str, Any]] = None, *,
                        channel: str = HUMAN_CHANNEL_CLI,
                        gate: Optional[Callable[[Mapping[str, Any], str], Optional[str]]] = None
                        ) -> Dict[str, Any]:
        """Record a person's reconcile of an UNCERTAIN step, out of band."""
        try:
            state = self._state(run_id)
        except ReceiptError as exc:
            return self._result("refused", run_id, error=str(exc))
        step = {s["id"]: s for s in self.steps}.get(step_id)
        last = state.last.get(step_id)
        if step is None or not last or last.get("status") != "uncertain":
            return self._result("refused", run_id, error="nothing to reconcile for that step")
        want = confirm_digest("reconcile", self.digest, run_id, step,
                              str(last.get("inputs_digest") or ""))
        if str(confirm or "") != want:
            return self._result("refused", run_id, error=(
                "confirmation digest does not match this run's uncertain request/effect"))
        rec: Dict[str, Any] = {"type": "reconcile", "step": step_id, "actor": "human",
                               "channel": channel, "reconcile_digest": want,
                               "manifest_version": self.manifest["version"]}
        if outcome_ == "completed":
            outs, errs = self._typed_outputs(step, outputs or {})
            if errs:
                return self._result("refused", run_id, error="; ".join(errs))
            rec.update(status="completed", outputs=outs)
        elif outcome_ == "not_done":
            rec.update(status="not_done")
        else:
            return self._result("refused", run_id,
                                error="reconcile outcome must be completed or not_done")
        if gate is not None:
            why = gate({"purpose": "reconcile", "manifest": self.manifest["name"],
                        "version": self.manifest["version"], "run_id": run_id,
                        "step": step_id, "effects": step["effects"],
                        "action": {k: step["action"].get(k) for k in _ACTION_PINS},
                        "outcome": outcome_, "outputs": rec.get("outputs"),
                        "inputs_digest": last.get("inputs_digest")}, want)
            if why:
                return self._result("refused", run_id, error=why)
        saved = self._append(run_id, rec)
        return {**self._result("reconciled", run_id), "step": step_id,
                "receipt": saved["hash"]}

    # -- human inbox (execution/human_inbox.py) -------------------------------

    def step_host(self, step: Mapping[str, Any]) -> str:
        """The host a step acts on (its app's origin), or ``""``."""
        import urllib.parse
        app = (self.manifest.get("apps") or {}).get(step.get("app") or "") or {}
        return (urllib.parse.urlsplit(str(app.get("origin") or "")).netloc or "").lower()

    def _reveal(self, run_id: str, bound: Mapping[str, Any]) -> Dict[str, Any]:
        """Bound inputs with inbox values revealed, in memory only."""
        out = dict(bound)
        for name, value in bound.items():
            if is_secret_ref(value):
                if self.inbox is None:
                    raise KeyError(name)
                out[name] = self.inbox.reveal(run_id, value[SECRET_KEY])
        return out

    def pending(self, run_id: str) -> Tuple[Optional[RunState], Optional[Mapping[str, Any]],
                                            Dict[str, Any], str]:
        """(state, pending step, bound inputs, refusal)."""
        try:
            state = self._state(run_id)
        except ReceiptError as exc:
            return None, None, {}, str(exc)
        if state.cancelled:
            return state, None, {}, "the run was cancelled"
        step = self._next(state)
        if step is None:
            return state, None, {}, "the run has no pending step"
        bound, errs = self._bind(step, state)
        if errs:
            return state, step, {}, "; ".join(errs)
        return state, step, bound, ""

    def complete_card(self, run_id: str, step_id: str, *, how: str, card_id: str,
                      inputs_digest_: str, outputs: Optional[Mapping[str, Any]] = None,
                      secret_outputs: Optional[Mapping[str, Any]] = None,
                      host: str = "", channel: str = HUMAN_CHANNEL_INBOX) -> Dict[str, Any]:
        """Record what a person did for a card, out of band. Does NOT resume.

        ``how``:

        * ``"human_step"`` — a ``human`` step's typed outputs (``secret_outputs``
          for a ``value`` card: checked and postcondition-evaluated in memory,
          stored by the inbox, and recorded in the receipt only as a
          placeholder);
        * ``"human_does_step"`` — a person did a machine step themselves (a
          challenge, a login wall, a human-only host) and hands back its
          result; the host becomes human-only for the rest of this run;
        * ``"continue_session"`` — a person cleared a block in their own
          attached browser session; the step is re-dispatched on resume, after
          the precondition and fresh checks, in that session.

        Refused unless ``step_id`` is the run's pending step, its bound inputs
        still hash to ``inputs_digest_`` and its preconditions hold now.
        """
        state, step, bound, why = self.pending(run_id)
        if why:
            return self._result("refused", run_id, error=why)
        if step["id"] != step_id:
            return self._result("refused", run_id, error=(
                f"step {step_id!r} is not the run's pending step"))
        idig = inputs_digest(bound)
        if idig != inputs_digest_:
            return self._result("refused", run_id, error=(
                "the step's inputs changed since this card was posted"))
        failed_pre = self._pre(step, state, bound)
        if failed_pre:
            return self._result("refused", run_id,
                                error="precondition(s) false now: " + ", ".join(failed_pre))
        base = {"step": step_id, "actor": "human", "channel": channel, "card": card_id,
                "inputs_digest": idig, "manifest_version": self.manifest["version"]}
        if how == "continue_session":
            if step["mode"] == "human" or state.blocked(step_id) is None:
                return self._result("refused", run_id,
                                    error="there is no block on this step to clear")
            rec = self._append(run_id, {**base, "type": "clearance", "host": host})
            return {**self._result("recorded", run_id), "receipt": rec["hash"]}
        raw = dict(outputs or {})
        secret = dict(secret_outputs or {})
        outs, errs = self._typed_outputs(step, {**raw, **secret})
        if not errs:
            errs += [f"postcondition false: {p}" for p in self._post(step, state, bound, outs)]
        if errs:
            return self._result("refused", run_id, error="; ".join(errs))
        if how == "human_step":
            if step["mode"] != "human":      # agent steps go through resume(human=)
                return self._result("refused", run_id, error="not a human step")
        elif how == "human_does_step":
            if step["mode"] in ("human", "agent"):
                return self._result("refused", run_id, error="not a machine step")
            if secret:
                return self._result("refused", run_id, error="no secret outputs here")
        else:
            return self._result("refused", run_id, error=f"unknown completion {how!r}")
        recorded = dict(outs)
        for name in secret:
            if name in recorded:
                ref = self.inbox.keep(run_id, f"{step_id}.{name}", recorded[name])
                recorded[name] = {SECRET_KEY: ref}
        if how == "human_does_step":
            hosts = sorted({h for h in (host, self.step_host(step)) if h})
            for h in hosts:
                if h not in state.human_only_hosts:
                    self._append(run_id, {**base, "type": "host_policy", "host": h,
                                          "mode": "human_only"})
        rec = self._append(run_id, {**base, "type": "step", "status": "completed",
                                    "mode": step["mode"], "via": how, "outputs": recorded,
                                    "secret_outputs": sorted(secret)})
        return {**self._result("recorded", run_id), "receipt": rec["hash"]}

    def cancel(self, run_id: str, *, card_id: str = "",
               channel: str = HUMAN_CHANNEL_INBOX) -> Dict[str, Any]:
        """A person stops the run for good; every later resume returns cancelled."""
        try:
            state = self._state(run_id)
        except ReceiptError as exc:
            return self._result("refused", run_id, error=str(exc))
        if state.cancelled:
            return self._result("cancelled", run_id)
        if self._next(state) is None:
            return self._result("refused", run_id, error="the run already completed")
        self._append(run_id, {"type": "cancelled", "actor": "human", "channel": channel,
                              "card": card_id})
        self._end_live_sessions(run_id)
        if self.inbox is not None:
            self.inbox.run_finished(run_id)
        return self._result("cancelled", run_id)

    # -- internals ----------------------------------------------------------

    def _head_ok(self, run_id: str, expected_head: str, state: RunState) -> bool:
        """The head matches, or every receipt after it is an out-of-band
        human approval/reconcile (which a person adds between hand-off and
        resume)."""
        if expected_head == state.head:
            return True
        chain = self.store.load(run_id)
        heads = [self.store.meta(run_id)["meta_hash"]] + [r["hash"] for r in chain]
        if expected_head not in heads:
            return False
        after = chain[heads.index(expected_head):]
        return bool(after) and all(
            r.get("type") in ("approval", "reconcile", "step", "clearance", "host_policy")
            and r.get("actor") == "human" and r.get("channel") in HUMAN_CHANNELS for r in after)

    def _approved(self, state: RunState, run_id: str, step, idig: str) -> bool:
        if not self.human_channel:
            return (step["id"], idig) in state.approvals
        want = confirm_digest("approve", self.digest, run_id, step, idig)
        return any(r.get("step") == step["id"] and r.get("channel") in HUMAN_CHANNELS
                   and r.get("inputs_digest") == idig and r.get("approval_digest") == want
                   for r in state.approval_records)

    def _state(self, run_id: str) -> RunState:
        meta = self.store.meta(run_id)
        if meta.get("manifest_digest") != self.digest:
            raise ReceiptError("this run belongs to a different manifest version")
        return RunState(meta, self.store.load(run_id))

    def _append(self, run_id: str, rec: Mapping[str, Any]) -> Dict[str, Any]:
        return self.store.append(run_id, {**dict(rec), "manifest_digest": self.digest,
                                          "at": self.clock()})

    def _check_run_inputs(self, inputs: Mapping[str, Any]) -> List[str]:
        declared = dict(self.manifest.get("inputs") or {})
        out = [f"undeclared input {k!r}" for k in inputs if k not in declared]
        for name, spec in declared.items():
            if name not in inputs:
                out.append(f"missing input {name!r}")
            else:
                out.extend(check_value(spec, inputs[name], name))
        return out

    def _next(self, state: RunState) -> Optional[Mapping[str, Any]]:
        for step in self.steps:
            if not state.completed(step["id"]):
                return step
        return None

    def _bind(self, step, state: RunState) -> Tuple[Dict[str, Any], List[str]]:
        bound: Dict[str, Any] = {}
        errs = list(cross_app_problems(self.manifest, step))
        for name, spec in (step.get("inputs") or {}).items():
            b = step["bindings"][name]
            if b["kind"] == "const":
                value = b["value"]
            elif b["kind"] == "input":
                value = state.inputs.get(b["name"])
            else:
                produced = state.outputs.get(b["step"])
                if produced is None or b["output"] not in produced:
                    errs.append(f"{name}: producer {b['step']}.{b['output']} has no receipt")
                    continue
                value = produced[b["output"]]
            if not is_secret_ref(value):     # checked when the person typed it
                errs.extend(check_value(spec, value, f"{step['id']}.{name}"))
            bound[name] = value
        return bound, errs

    def _typed_outputs(self, step, raw: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        declared = dict(step.get("outputs") or {})
        if not isinstance(raw, Mapping):
            return {}, ["outputs must be an object"]
        errs = [f"{step['id']}: missing output {k!r}" for k in declared if k not in raw]
        outs = {k: raw[k] for k in declared if k in raw}   # undeclared names never flow
        for k, v in outs.items():
            errs.extend(check_value(declared[k], v, f"{step['id']}.{k}"))
        return outs, errs

    def _post(self, step, state, bound, outs) -> List[str]:
        scope = state.scope(bound, outs)
        return [label for ok, label in (evaluate(p, scope) for p in
                                        step.get("postconditions") or []) if not ok]

    def _pre(self, step, state, bound) -> List[str]:
        scope = state.scope(bound)
        return [label for ok, label in (evaluate(p, scope) for p in
                                        step.get("preconditions") or []) if not ok]

    def _handoff(self, state: RunState, step, reason: str, bound, extra=None) -> Dict[str, Any]:
        from anyuse.execution.skill_manifest import object_schema
        nxt = {
            "id": step["id"], "mode": step["mode"],
            "instruction": step["action"].get("instruction", ""),
            "bound_inputs": bound,
            "inputs_digest": inputs_digest(bound),
            "expected_outputs": object_schema(dict(step.get("outputs") or {})),
            "preconditions": list(step.get("preconditions") or []),
            "preconditions_hold_now": not self._pre(step, state, bound),
            "postconditions": list(step.get("postconditions") or []),
            "effects": step["effects"],
            "approval_required": bool((step.get("approval") or {}).get("required")),
        }
        if nxt["approval_required"]:
            nxt["approval_digest"] = confirm_digest(
                "approve", self.digest, state.run_id, step, nxt["inputs_digest"])
        last = state.last.get(step["id"])
        if reason == "reconcile" and last and last.get("status") == "uncertain":
            nxt["reconcile_digest"] = confirm_digest(
                "reconcile", self.digest, state.run_id, step,
                str(last.get("inputs_digest") or ""))
        nxt.update(extra or {})
        return {"reason": reason, "next_step": nxt, "head": state.head,
                "completed_steps": sorted(state.outputs)}

    def _result(self, status: str, run_id: str, *, outputs=None, handoff=None,
                error: str = "", receipts=None) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": status, "run_id": run_id,
                               "manifest_digest": self.digest}
        if outputs is not None:
            out["outputs"] = outputs
        if handoff is not None:
            out["handoff"] = handoff
        if error:
            out["error"] = error
        if receipts is not None:
            out["receipts"] = receipts
        return out

    def _live_solve(self, run_id, step, runner, ctx, info, bound
                    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """human_solves_live: a person solves the challenge live, then THIS run
        re-dispatches the step. ``(False, None)`` means the usual hand-off card;
        ``(True, result)`` is the re-dispatch's own result (``None`` = completed).

        One live solve per step per run. The model advisor is never consulted
        here, and the runner's page is only observed (attach) or relayed to
        (headless live view); see execution/live_solve.py."""
        from anyuse.execution import human_inbox as _hi
        from anyuse.execution import live_solve as _ls
        if self.live_solver is None or info.get("reason") not in _hi.CHALLENGE_REASONS:
            return False, None
        page_for = getattr(runner, "challenge_page", None)
        if page_for is None or not getattr(runner, "supports_live_solve", False):
            return False, None
        origin = self.store.meta(run_id).get("origin")
        if not self.live_solver.applies(origin):
            return False, None
        sid = step["id"]
        if any(r.get("type") == _ls.RECEIPT_TYPE and r.get("step") == sid
               for r in self.store.load(run_id)):
            return False, None
        page = page_for(ctx)
        if page is None:
            return False, None
        host = str(info.get("host") or self.step_host(step)).lower()
        req = _ls.SolveRequest(task_id=run_id, host=host, origin=origin, run_id=run_id,
                               manifest=self.manifest["name"], step=sid)
        out = self.live_solver.solve(req, page)
        if out.get("status") in ("not_applicable", "not_challenge"):
            return False, None
        self._append(run_id, {**out["receipt"], "step": sid, "actor": "human"})
        if out["status"] != "cleared":
            return False, None
        self._append(run_id, {"type": "clearance", "step": sid, "actor": "human",
                              "channel": LIVE_SOLVE_CHANNEL, "card": "", "host": host,
                              "inputs_digest": inputs_digest(bound),
                              "manifest_version": self.manifest["version"]})
        return True, self._machine_step(run_id, step, self._state(run_id), bound)

    def _end_live_sessions(self, run_id: str) -> None:
        if self.live_solver is not None:
            self.live_solver.sessions.end_task(run_id)

    def _completed(self, state: RunState) -> Dict[str, Any]:
        self._end_live_sessions(state.run_id)
        if self.inbox is not None:
            self.inbox.run_finished(state.run_id)
        outs = {}
        for name, spec in dict(self.manifest.get("outputs") or {}).items():
            src = spec["from"]
            outs[name] = state.outputs[src["step"]][src["output"]]
        chain = self.store.load(state.run_id)
        receipts = [{"seq": r["seq"], "type": r["type"], "step": r.get("step"),
                     "status": r.get("status"), "actor": r.get("actor"),
                     "hash": r["hash"]} for r in chain]
        return self._result("completed", state.run_id, outputs=outs, receipts=receipts)

    def _shadow_rank(self, step, candidates) -> None:
        """Observation only: any failure, any answer, changes nothing."""
        try:
            ranker = self.shadow_ranker
            if ranker is None:
                from anyuse.settings import get as get_setting
                if str(get_setting("ANYUSE_ROUTINE_SHADOW_RANKER") or "none") != "openjev":
                    return
                from anyuse.execution.openjev import ResumeRanker
                ranker = self.shadow_ranker = ResumeRanker()
            action = step.get("action") or {}
            ranker.rank(step["id"], list(candidates),
                        {"routine": self.manifest.get("name", ""),
                         "into": action.get("into", ""),
                         "description": str(step.get("description") or "")[:300]})
        except Exception:
            pass

    def _stop(self, run_id, status, step, reason, state, bound=None, error="", extra=None):
        if step.get("mode") == "agent" and isinstance((extra or {}).get("candidates"), list) \
                and len(extra["candidates"]) > 1 and status == "handed_off":
            self._shadow_rank(step, extra["candidates"])
        self._append(run_id, {"type": "handoff", "step": step["id"], "reason": reason})
        state = self._state(run_id)
        res = self._result(status, run_id, error=error,
                           handoff=self._handoff(state, step, reason, bound or {}, extra))
        if self.inbox is not None and status in ("handed_off", "needs_reconcile"):
            card = self.inbox.post(self, res, step)
            if card:
                res["card"] = {"id": card["id"], "kind": card["kind"],
                               "needed": card["needed"]}
        return res

    def _drive(self, run_id: str, *, stop_after: Optional[str] = None,
               human: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        while True:
            state = self._state(run_id)
            if state.cancelled:
                return self._result("cancelled", run_id,
                                    error="a person cancelled this run")
            step = self._next(state)
            if step is None:
                return self._completed(state)
            sid = step["id"]
            last = state.last.get(sid)
            if last and last.get("status") == "uncertain":
                safe = (step["effects"]["class"] == "read_only"
                        and (step.get("policy") or {}).get("idempotent") is True)
                if not safe:
                    return self._stop(run_id, "needs_reconcile", step, "reconcile", state,
                                      error="the outcome of this step is uncertain; a human "
                                            "must reconcile it before anything is retried")
            bound, errs = self._bind(step, state)
            if errs:
                self._append(run_id, {"type": "step", "step": sid, "status": "refused",
                                      "error": "; ".join(errs)})
                return self._result("refused", run_id, error="; ".join(errs))
            failed_pre = self._pre(step, state, bound)
            if failed_pre:
                return self._stop(run_id, "refused", step, "precondition", state, bound,
                                  error="precondition(s) false: " + ", ".join(failed_pre))
            h = human if (human and str(human.get("step")) == sid) else None
            if step["mode"] == "human":
                res = self._human_step(run_id, step, state, bound, h)
            elif step["mode"] == "agent":
                res = self._agent_step(run_id, step, state, bound, h)
            else:
                res = self._machine_step(run_id, step, state, bound)
            if res is not None:
                return res
            human = None if h is not None else human
            if stop_after == sid:
                nstate = self._state(run_id)
                nxt = self._next(nstate)
                if nxt is None:
                    return self._completed(nstate)
                nbound, nerrs = self._bind(nxt, nstate)
                return self._result("paused", run_id, handoff=self._handoff(
                    nstate, nxt, "paused", nbound if not nerrs else {}))

    # human -----------------------------------------------------------------

    def _human_step(self, run_id, step, state, bound, h) -> Optional[Dict[str, Any]]:
        if h is None:
            return self._stop(run_id, "handed_off", step, "human_step", state, bound)
        if step["action"].get("card") in INBOX_ONLY_CARDS:
            # A value only the person knows, or their own login, never comes
            # in-band (an MCP client may be a model): `anyuse inbox do` only.
            return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                              error=("this step is completed only by a person with "
                                     "`anyuse inbox do <card>` in their own terminal"))
        outs, errs = self._typed_outputs(step, h.get("outputs") or {})
        errs += [] if errs else [f"postcondition false: {p}" for p in
                                 self._post(step, state, bound, outs)]
        if errs:
            return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                              error="; ".join(errs))
        self._append(run_id, {"type": "step", "step": step["id"], "status": "completed",
                              "actor": "human", "mode": "human",
                              "inputs_digest": inputs_digest(bound), "outputs": outs})
        return None

    # agent -----------------------------------------------------------------

    def _agent_step(self, run_id, step, state, bound, h) -> Optional[Dict[str, Any]]:
        action = step["action"]
        into = action["into"]
        candidates = resolve_ref(action["choose_from"], state.scope(bound))
        if not isinstance(candidates, list) or not candidates:
            return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                              error="no candidates to choose from")
        level = (step.get("autonomy") or {}).get("level", "rule")
        decisions = state.decisions.get(step["id"], [])

        def finish(value, actor, decision_hash=""):
            outs, errs = self._typed_outputs(step, {into: value})
            if value not in candidates:
                errs.append("the choice is not one of the listed candidates")
            errs += [] if errs else [f"postcondition false: {p}" for p in
                                     self._post(step, state, bound, outs)]
            if errs:
                return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                                  error="; ".join(errs), extra={"candidates": candidates})
            self._append(run_id, {"type": "step", "step": step["id"], "status": "completed",
                                  "actor": actor, "mode": "agent", "decision": decision_hash,
                                  "inputs_digest": inputs_digest(bound), "outputs": outs})
            return None

        if h is not None:                      # a human decides, at any level
            if h.get("accept_proposal"):
                if not decisions or decisions[-1].get("choice") is None:
                    return self._stop(run_id, "handed_off", step, "human_step", state,
                                      bound, error="there is no proposal to accept",
                                      extra={"candidates": candidates})
                return finish(decisions[-1]["choice"], "human", decisions[-1]["hash"])
            return finish((h.get("outputs") or {}).get(into), "human")

        if level == "rule":
            if action.get("rule") == "only" and len(candidates) == 1:
                return finish(candidates[0], "rule")
            return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                              extra={"candidates": candidates})

        if self.advisor is None:
            return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                              error="no model advisor configured",
                              extra={"candidates": candidates})

        limits = dict((step.get("autonomy") or {}).get("limits") or {})
        if level == "model_decides_within_limits":
            spent = sum(float(d.get("cost_usd") or 0) for d in decisions)
            reserve = float(getattr(self.advisor, "max_cost_usd", 0.0))
            if (len(decisions) >= limits["max_decisions"]
                    or spent + reserve > float(limits["cap_usd"])):
                return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                                  error="model autonomy limit reached",
                                  extra={"candidates": candidates})

        proposal = self.advisor.propose(step["id"], list(candidates))
        choice = (proposal or {}).get("choice")
        cost = float((proposal or {}).get("cost_usd") or 0.0)
        in_list = choice is not None and choice in candidates
        rec = self._append(run_id, {"type": "decision", "step": step["id"], "level": level,
                                    "actor": "model", "choice": choice if in_list else None,
                                    "abstained": choice is None, "rejected_out_of_list":
                                    choice is not None and not in_list, "cost_usd": cost})
        state = self._state(run_id)
        if not in_list:
            return self._stop(run_id, "handed_off", step, "human_step", state, bound,
                              error="the model abstained or chose outside the candidates",
                              extra={"candidates": candidates})
        if level == "model_proposes_human_confirms":
            return self._stop(run_id, "handed_off", step, "confirm_model_proposal", state,
                              bound, extra={"candidates": candidates, "proposal": choice,
                                            "decision": rec["hash"]})
        return finish(choice, "model", rec["hash"])

    # machine ---------------------------------------------------------------

    def _call(self, runner, step, bound, ctx, timeout_s) -> Dict[str, Any]:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(runner.execute, step, dict(bound), ctx)
        read_only = step["effects"]["class"] == "read_only"
        try:
            return fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            return outcome(False, error=f"timed out after {timeout_s}s",
                           transient=read_only, uncertain=not read_only)
        except Exception as exc:                     # after dispatch: effect unknown
            return outcome(False, error=f"runner raised {type(exc).__name__}",
                           uncertain=not read_only)
        finally:
            pool.shutdown(wait=False)

    def _machine_step(self, run_id, step, state, bound) -> Optional[Dict[str, Any]]:
        sid = step["id"]
        runner = self.runners.get(step["action"]["kind"])
        if runner is None:
            return self._stop(run_id, "refused", step, "no_runner", state, bound,
                              error=f"no runner for {step['action']['kind']!r}")
        idig = inputs_digest(bound)
        host = self.step_host(step)
        if host and host in state.human_only_hosts:
            # A person already did a blocked step on this host in this run; the
            # session they cleared is theirs, not ours to keep automating.
            return self._stop(run_id, "handed_off", step, "human_only_host", state, bound,
                              extra={"host": host})
        held = state.blocked(sid)
        if held is not None:
            # Never re-dispatch into a block page until a person has dealt with
            # it through the inbox; an in-band resume does not count.
            return self._stop(run_id, "handed_off", step, "blocked", state, bound,
                              extra={"blocked": dict(held.get("blocked") or {})})
        if (step.get("approval") or {}).get("required") and not self._approved(
                state, run_id, step, idig):
            return self._stop(run_id, "handed_off", step, "approval", state, bound)
        # fresh check immediately before dispatch (also after resume/approval)
        why = runner.fresh_check(step, bound)
        if why:
            return self._stop(run_id, "refused", step, "fresh_check", state, bound, error=why)
        policy = dict(step.get("policy") or {})
        attempts = 1 + int(policy.get("retries", 0))
        ctx = {"run_id": run_id, "idempotency_key": _sha([run_id, sid, idig])}
        if self.live_solver is not None and host and self.live_solver.sessions.valid(
                run_id, host, self.store.meta(run_id).get("origin")):
            ctx["live_session"] = True        # a person cleared this host in THIS run
        clearance = state.cleared(sid)
        if clearance is not None and clearance.get("channel") == LIVE_SOLVE_CHANNEL:
            # human_solves_live: the person cleared it in this run's own page;
            # only a runner that holds that page may continue, in this run only.
            if not getattr(runner, "supports_live_solve", False):
                return self._stop(run_id, "refused", step, "blocked", state, bound,
                                  error="this runner cannot continue in the page the "
                                        "person solved")
            ctx["session"] = "live_solved"
            ctx["live_solve_task"] = run_id
        elif clearance is not None:
            # continue_in_own_session: allowed only when the card said so and
            # the runner can share the person's attached browser session.
            if not getattr(runner, "supports_attached_session", False):
                return self._stop(run_id, "refused", step, "blocked", state, bound,
                                  error="this runner cannot continue in the person's "
                                        "browser session")
            ctx["session"] = "attached"
            ctx["cleared_by_card"] = clearance.get("card")
        try:
            exec_bound = self._reveal(run_id, bound)
        except KeyError:
            return self._stop(run_id, "refused", step, "value_missing", state, bound,
                              error="a value a person entered is no longer available; "
                                    "cancel this run and start it again")
        res: Dict[str, Any] = outcome(False, error="not run")
        for attempt in range(attempts):
            res = self._call(runner, step, exec_bound, ctx,
                             float(policy.get("timeout_s", 30)))
            if res["ok"] or res["uncertain"] or not res["transient"] or res.get("blocked"):
                break
        base = {"type": "step", "step": sid, "mode": step["mode"], "actor": "runtime",
                "inputs_digest": idig, "attempts": attempt + 1,
                "provenance": res.get("provenance") or {},
                "action": {k: step["action"].get(k) for k in
                           ("kind", "ref", "proof_id", "template_digest", "digest")}}
        if res.get("blocked"):
            # The site served a block page instead of the content: nothing was
            # processed, and nothing automatic will be tried against it.
            blk = res["blocked"]
            info = {"reason": str(blk.get("reason") or ""),
                    "host": str(blk.get("host") or host),
                    "status": blk.get("status"),
                    "evidence": [str(e)[:160] for e in (blk.get("evidence") or [])[:3]]}
            self._append(run_id, {**base, "status": "blocked", "blocked": info,
                                  "error": f"blocked ({info['reason']})"})
            handled, live = self._live_solve(run_id, step, runner, ctx, info, bound)
            if handled:
                return live         # None: the step completed after the live solve
            return self._stop(run_id, "handed_off", step, "blocked", self._state(run_id),
                              bound, extra={"blocked": info})
        if res["uncertain"]:
            self._append(run_id, {**base, "status": "uncertain", "error": res["error"]})
            return self._stop(run_id, "needs_reconcile", step, "reconcile",
                              self._state(run_id), bound, error=res["error"])
        if not res["ok"]:
            self._append(run_id, {**base, "status": "failed", "error": res["error"]})
            return self._result("failed", run_id, error=res["error"])
        errs: List[str] = []
        if _RANK.get(res.get("effect_class"), 99) > _RANK[step["effects"]["class"]]:
            errs.append("the runner reported an effect beyond the permitted class")
        outs, oerrs = self._typed_outputs(step, res["outputs"])
        errs += oerrs
        if not errs:
            errs += [f"postcondition false: {p}" for p in self._post(step, state, bound, outs)]
        if errs:
            status = "failed" if step["effects"]["class"] == "read_only" else "uncertain"
            self._append(run_id, {**base, "status": status, "error": "; ".join(errs)})
            if status == "uncertain":
                return self._stop(run_id, "needs_reconcile", step, "reconcile",
                                  self._state(run_id), bound, error="; ".join(errs))
            return self._result("failed", run_id, error="; ".join(errs))
        self._append(run_id, {**base, "status": "completed", "outputs": outs})
        return None
