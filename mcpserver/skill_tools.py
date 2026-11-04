"""MCP surface for skill/routine manifests.

Wired into ``mcpserver/server.py`` on ``feat/integration`` (docs/SKILLS.md,
"Integration"): ``list_tools`` advertises ``tool_definitions(...)`` and
``call_tool`` dispatches through ``handle(...)`` after the profile check.

Profile rule (launch 1): the ``default`` MCP profile lists
a skill/routine only when every step is ``proved``, ``replay`` or ``human``
(:func:`launch_eligible`); a manifest with an ``agent`` step is listed only
under the explicit ``full`` profile. In both profiles no model advisor is
configured, so an ``agent`` step never auto-runs: it hands off to a human.
Under the default profile the resume tool also refuses ``approval`` and
``reconcile`` (an MCP client may itself be a model; docs/ROUTINES.md).

Manifests are read from ``<ANYUSE_DATA_DIR>/skills/*.manifest.json``; receipts
live in ``<ANYUSE_DATA_DIR>/skills/runs``. Only manifests that validate are
listed or run. The only production runner is the compiled-tool runner (read-only
``compiled_tools.run_compiled``) plus composition of registered skills; there is
no desktop, replay or agent actuator on this surface, so a ``replay`` step is
refused (no runner) rather than executed.

Asking the human (docs/HUMAN_INBOX.md): every engine built here posts a card
for each stop that needs a person. When the caller passes an ``elicitor`` (the
server does, if the connected client advertises elicitation), the person is
asked right away through the client's own prompt and the run resumes on
``accept``. Otherwise, or on decline/cancel, the card waits for
``anyuse inbox do`` (the CLI fallback behind the terminal gate). MCP gets ONE
inbox tool, ``anyuse_inbox_list``, which is read-only. Under the default
profile a plain tool call can never answer a human step (``resume`` with
``human`` is refused): only elicitation or the CLI can.
"""
from __future__ import annotations

import glob
import logging
import os
from typing import Any, Dict, List, Mapping, Optional

from anyuse.execution.routine import (
    HUMAN_CHANNEL_CLI, CompiledToolRunner, Engine, ReceiptError, ReceiptStore, SkillRunner,
)
from anyuse.execution.skill_gen import RESUME_TOOL, RESUME_TOOL_NAME, legacy_tool_name, mcp_tool, tool_name
from anyuse.execution.skill_manifest import load_manifest, validate_manifest
from anyuse.execution import human_inbox

logger = logging.getLogger(__name__)


def skills_dir() -> str:
    from anyuse.paths import anyuse_data_path
    return str(anyuse_data_path("skills"))


def load_registry(directory: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """name -> manifest, for every manifest file that validates."""
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(directory or skills_dir(),
                                              "*.manifest.json"))):
        try:
            manifest = load_manifest(path)
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable manifest %s: %s", path, exc)
            continue
        errors = validate_manifest(manifest)
        if errors:
            logger.warning("skipping invalid manifest %s: %s", path, errors[:3])
            continue
        if manifest["name"] in out:
            logger.warning("skipping duplicate manifest name %s", manifest["name"])
            continue
        out[manifest["name"]] = manifest
    return out


LAUNCH_STEP_MODES = frozenset({"proved", "replay", "human"})


def launch_eligible(manifest: Mapping[str, Any]) -> bool:
    """True when every step is proved/replay or hands off to a human."""
    steps = list(manifest.get("steps") or [])
    return bool(steps) and all(step.get("mode") in LAUNCH_STEP_MODES for step in steps)


def tool_definitions(directory: Optional[str] = None, *,
                     launch_profile: bool = False) -> List[Dict[str, Any]]:
    tools = [mcp_tool(m) for m in load_registry(directory).values()
             if not launch_profile or launch_eligible(m)]
    return tools + ([RESUME_TOOL, human_inbox.MCP_LIST_TOOL] if tools else [])


class _Context:
    def __init__(self, directory, storage, hosts_path, executor, launch_profile=False):
        self.directory = directory or skills_dir()
        self.registry = load_registry(self.directory)
        self.store = ReceiptStore(os.path.join(self.directory, "runs"))
        os.makedirs(self.store.base_dir, exist_ok=True)
        self.runners: Dict[str, Any] = {
            "compiled_tool": CompiledToolRunner(storage, hosts_path, executor,
                                                require_proved=launch_profile)}
        self.runners["skill"] = SkillRunner(self.registry, self.runners, self.store)
        # Default profile: approvals/reconciles count only when a person wrote
        # them through the CLI (anyuse routine approve/reconcile).
        self.human_channel = HUMAN_CHANNEL_CLI if launch_profile else None
        self.inbox = human_inbox.Inbox(hosts_path=hosts_path)

    def engine(self, manifest: Mapping[str, Any], elicitor=None) -> Engine:
        # A tool call is the user's own task (origin user_task); scheduled and
        # bulk runners construct their engines with their own origin.
        from anyuse.execution import live_solve
        return Engine(manifest, self.runners, self.store, advisor=None,
                      human_channel=self.human_channel, inbox=self.inbox,
                      origin=human_inbox.ORIGIN_USER_TASK,
                      live_solver=live_solve.default_solver(self.inbox, elicitor=elicitor))

    def run_engine(self, run_id: str):
        """(engine, None) for a run's registered manifest, or (None, refusal)."""
        try:
            meta = self.store.meta(run_id)
        except ReceiptError as exc:
            return None, {"status": "refused", "run_id": run_id, "error": str(exc)}
        manifest = self.registry.get(str(meta.get("manifest")))
        if manifest is None:
            return None, {"status": "refused", "run_id": run_id,
                          "error": "the run's manifest is no longer registered"}
        return self.engine(manifest), None


def is_skill_tool_name(name: str) -> bool:
    return (name in (RESUME_TOOL_NAME, human_inbox.MCP_LIST_TOOL_NAME)
            or name.startswith(("skill__", "routine__", "skill.", "routine.")))


def handle(name: str, arguments: Mapping[str, Any], *, directory: Optional[str] = None,
           storage=None, hosts_path: Optional[str] = None, executor=None,
           launch_profile: bool = False, elicitor=None) -> Optional[Dict[str, Any]]:
    """Dispatch a skill/routine tool or the resume tool; ``None`` if not ours.

    ``launch_profile=True`` applies the default-profile rules from the module
    docstring (defence in depth; the server also checks its advertised set).
    """
    if not is_skill_tool_name(name):
        return None
    if name == human_inbox.MCP_LIST_TOOL_NAME:
        # Read-only: an agent may tell its user "N things need you"; it can
        # never complete, skip or cancel a card.
        return human_inbox.list_pending(inbox_for(hosts_path=hosts_path))
    ctx = _Context(directory, storage, hosts_path, executor, launch_profile)
    args = dict(arguments or {})
    if name == RESUME_TOOL_NAME:
        run_id = str(args.get("run_id") or "")
        if launch_profile and (args.get("approval") or args.get("reconcile")):
            return {"status": "refused", "run_id": run_id,
                    "error": ("approval and reconcile are human-only mutations and "
                              "are not accepted over the default MCP profile; a person "
                              "runs `anyuse routine approve|reconcile` instead")}
        if launch_profile and args.get("human") is not None:
            return {"status": "refused", "run_id": run_id,
                    "error": ("a human step is answered only by the person: through the "
                              "MCP client's elicitation prompt, or `anyuse inbox do <card>` "
                              "in their own terminal; a tool call cannot answer it")}
        try:
            meta = ctx.store.meta(run_id)
        except ReceiptError as exc:
            return {"status": "refused", "run_id": run_id, "error": str(exc)}
        manifest = ctx.registry.get(str(meta.get("manifest")))
        if manifest is None:
            return {"status": "refused", "run_id": run_id,
                    "error": "the run's manifest is no longer registered"}
        if launch_profile and not launch_eligible(manifest):
            return {"status": "refused", "run_id": run_id,
                    "error": "this run's manifest is not in the default MCP profile"}
        return ask_human(ctx, ctx.engine(manifest, elicitor).resume(
            run_id, expected_head=args.get("expected_head"), human=args.get("human"),
            approval=args.get("approval"), reconcile=args.get("reconcile")), elicitor)
    for manifest in ctx.registry.values():
        if name in (tool_name(manifest), legacy_tool_name(manifest)):
            if launch_profile and not launch_eligible(manifest):
                return {"status": "refused", "run_id": "",
                        "error": "this manifest is not in the default MCP profile"}
            return ask_human(ctx, ctx.engine(manifest, elicitor).run(args), elicitor)
    return None


#: A run can need a person several times in a row (approve, then a value ...).
MAX_ASKS_PER_CALL = 8


def _notify_owner(card: Dict[str, Any], need: Dict[str, Any]) -> None:
    """No client can ask the person: tell them through the configured channel
    (docs/NOTIFY.md). Default ``none`` sends nothing and adds nothing."""
    from anyuse.execution import notify
    out = notify.notify_card(card, elicitation_attached=False)
    if out.get("sent"):
        need["notified"] = out.get("channel")


def ask_human(ctx: "_Context", res: Dict[str, Any], elicitor) -> Dict[str, Any]:
    """Elicitation first; the inbox card stays pending otherwise."""
    for _ in range(MAX_ASKS_PER_CALL):
        card = res.get("card") if isinstance(res, dict) else None
        if not card or res.get("status") not in ("handed_off", "needs_reconcile"):
            return res
        if elicitor is None:
            need = {**card, "asked": "no_elicitation",
                    "how_to_complete": human_inbox.HOW_TO_COMPLETE}
            _notify_owner(card, need)
            return {**res, "needs_human": need}
        out = human_inbox.elicit_card(ctx.inbox, card["id"], engine_for=ctx.run_engine,
                                      elicitor=elicitor)
        if "asked" in out:
            return {**res, "needs_human": {**card, **out,
                                           "how_to_complete": human_inbox.HOW_TO_COMPLETE}}
        res = out
    return res


# ---------------------------------------------------------------------------
# out-of-band human channel (CLI: anyuse routine show/approve/reconcile)
# ---------------------------------------------------------------------------

def show_run(run_id: str, *, directory: Optional[str] = None) -> Dict[str, Any]:
    """The run's current hand-off (including approval/reconcile digests)."""
    ctx = _Context(directory, None, None, None)
    engine, refusal = ctx.run_engine(run_id)
    if refusal:
        return refusal
    try:
        state = engine._state(run_id)
    except ReceiptError as exc:
        return {"status": "refused", "run_id": run_id, "error": str(exc)}
    step = engine._next(state)
    last = state.last.get(step["id"]) if step else None
    if step is not None and last and last.get("status") == "uncertain":
        return engine._result("needs_reconcile", run_id, handoff=engine._handoff(
            state, step, "reconcile", {}))
    return engine.hand_off(run_id)


def human_approve(run_id: str, step_id: str, confirm: str, *, gate,
                  directory: Optional[str] = None) -> Dict[str, Any]:
    """``gate`` is required: the CLI passes ``execution.human_gate.TtyGate()``."""
    if gate is None:
        raise ValueError("a human gate is required")
    ctx = _Context(directory, None, None, None)
    engine, refusal = ctx.run_engine(run_id)
    if refusal:
        return refusal
    return engine.human_approve(run_id, step_id, confirm, channel=HUMAN_CHANNEL_CLI,
                                gate=gate)


def human_reconcile(run_id: str, step_id: str, outcome: str, confirm: str,
                    outputs: Optional[Mapping[str, Any]] = None, *, gate,
                    directory: Optional[str] = None) -> Dict[str, Any]:
    if gate is None:
        raise ValueError("a human gate is required")
    ctx = _Context(directory, None, None, None)
    engine, refusal = ctx.run_engine(run_id)
    if refusal:
        return refusal
    return engine.human_reconcile(run_id, step_id, outcome, confirm, outputs,
                                  channel=HUMAN_CHANNEL_CLI, gate=gate)


# ---------------------------------------------------------------------------
# human inbox (CLI: anyuse inbox / inbox do|skip|cancel)
# ---------------------------------------------------------------------------

def inbox_for(directory: Optional[str] = None,
              hosts_path: Optional[str] = None) -> "human_inbox.Inbox":
    """The inbox lives in the AnyUse data dir, whatever the skills dir."""
    return human_inbox.Inbox(hosts_path=hosts_path)


def _engine_for(directory, storage=None, hosts_path=None, executor=None):
    ctx = _Context(directory, storage, hosts_path, executor, launch_profile=True)
    return ctx, ctx.run_engine


def inbox_list(*, directory: Optional[str] = None, include_all: bool = False
               ) -> Dict[str, Any]:
    return human_inbox.list_pending(inbox_for(directory), include_all=include_all)


def inbox_do(card_id: str, *, gate, directory: Optional[str] = None, storage=None,
             hosts_path: Optional[str] = None, executor=None,
             read_secret=None) -> Dict[str, Any]:
    """``gate`` is required: the CLI passes ``execution.human_gate.TtyGate()``."""
    if gate is None:
        raise ValueError("a human gate is required")
    ctx, engine_for = _engine_for(directory, storage, hosts_path, executor)
    return human_inbox.do_card(ctx.inbox, card_id, engine_for=engine_for, gate=gate,
                               read_secret=read_secret)


def inbox_skip(card_id: str, *, gate, directory: Optional[str] = None) -> Dict[str, Any]:
    if gate is None:
        raise ValueError("a human gate is required")
    return human_inbox.skip_card(inbox_for(directory), card_id, gate=gate)


def inbox_cancel(card_id: str, *, gate, directory: Optional[str] = None) -> Dict[str, Any]:
    if gate is None:
        raise ValueError("a human gate is required")
    ctx, engine_for = _engine_for(directory)
    return human_inbox.cancel_card(ctx.inbox, card_id, engine_for=engine_for, gate=gate)


def inbox_live(card_id: str, *, gate, directory: Optional[str] = None) -> Dict[str, Any]:
    """Show a waiting live solve's private view link on the person's terminal."""
    if gate is None:
        raise ValueError("a human gate is required")
    return human_inbox.reveal_live(inbox_for(directory), card_id, gate=gate)
