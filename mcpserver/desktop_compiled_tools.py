"""MCP projection for locally compiled and proved desktop workflows.

Records live beside ``template.json`` as ``compiled_desktop.json``.  They do
not contain an actuator: dispatch reconstructs the pinned planned steps and
passes every one through ``StateGraphExecutor.execute_checked_step``.  This
keeps selector identity refusal, before/after verification and the drift gate
on the only execution path.

See ``execution/desktop_tools.py`` and docs/DESKTOP_TOOLS.md:

* every class follows one rule: a call started with its values asks
  nothing; a record's ``approval_level`` (default ``auto``) may opt into holds
  (``all`` / ``destructive``), and such a tool is not run over MCP;
* two more classes, ``routine`` and ``draft_workflow``: a routine or a draft
  workflow proved IN THE GUEST (``proof.harness == "guest"``, at least
  ``MIN_GUEST_PROOFS`` correct replays on new values), with its app, approval
  level and payload pinned by the artifact digest. A call runs without an
  approval prompt (starting it is the approval); an approval level that opts
  into holds is not run over MCP;
* every call of the shipped server (no executor injected) is routed to the
  guest runner: the guest job agent builds the AX tree bound to the recorded
  app and runs the checked steps there (a routine is replayed op by op from
  here, every action in the guest). Without a guest runner the call is refused
  with the foreground interlock's typed reason; nothing runs on the host.
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from anyuse.mcpserver.tool_names import joined, lookup, suffixed, unique

logger = logging.getLogger(__name__)
#: the tool-name namespace of a compiled desktop tool (``desktop__<workflow>``)
DESKTOP_NAMESPACE = "desktop"
COMPILED_DESKTOP_FILE = "compiled_desktop.json"
COMPILED_DESKTOP_VERSION = 1
ALLOWED_CLASSES = frozenset({
    "arithmetic", "file_write", "directory_op", "file_search", "read_value",
    "app_script", "routine", "draft_workflow",
})
#: proved in the guest, routed to the guest runner (execution/desktop_tools.py)
GUEST_CLASSES = frozenset({"routine", "draft_workflow"})
#: optional keys a record may carry; digested only when present, so an
#: older record's digest is unchanged
_EXTRA_KEYS = ("app", "approval_level", "payload")
#: classes that write files or run an app script: their risk class, listed as such. They follow
#: the same rule as every class: a call started with its values asks nothing (approval levels below)
FILE_CLASSES = frozenset({"file_write", "directory_op", "app_script"})
#: a record's approval level (``approval_level``, default ``auto``): ``auto`` / ``off`` hold nothing, a started
#: call asks nothing; ``all`` / ``destructive`` are opt-in holds for a person, so such a tool is not run over MCP
#: (no person to approve there). An unknown level holds (fails closed).
APPROVAL_LEVELS = ("auto", "off", "destructive", "all")
HOLD_LEVELS = ("all", "destructive")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _digest_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: record.get(key) for key in (
        "version", "workflow", "compile_class", "subtype", "parameters", "steps",
        "workflow_template", "state_graph")}
    out.update({key: record[key] for key in _EXTRA_KEYS if key in record})
    return out


def artifact_digest(record: Dict[str, Any]) -> str:
    blob = json.dumps(_digest_payload(record), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def compiled_desktop_record(*, workflow: str, compile_class: str,
                            steps: List[dict], proof: dict,
                            parameters: Optional[List[dict]] = None,
                            workflow_template: Optional[dict] = None,
                            state_graph: Optional[dict] = None,
                            subtype: str = "", compiled_at: str = "",
                            extra: Optional[dict] = None) -> dict:
    """Build a proof-pinned record; compiler/guest glue may persist this result.
    ``extra``: the app, approval level and payload of a guest-proved routine or
    draft workflow (``execution/desktop_tools.py``), digested with the rest."""
    record = {
        "version": COMPILED_DESKTOP_VERSION,
        "workflow": workflow,
        "compile_class": compile_class,
        "subtype": subtype,
        "parameters": list(parameters or []),
        "steps": [dict(step) for step in steps],
        "workflow_template": dict(workflow_template or {}),
        "state_graph": dict(state_graph or {}),
        "proof": dict(proof or {}),
        "compiled_at": compiled_at,
    }
    for key in _EXTRA_KEYS:
        if extra and key in extra:
            record[key] = extra[key]
    record["proof"]["artifact_digest"] = artifact_digest(record)
    return record


def persist_compiled_desktop(storage, workflow_id: str, record: dict) -> str:
    directory = storage.get_workflow_dir(workflow_id)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(str(directory), COMPILED_DESKTOP_FILE)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return path


def _storage(storage=None):
    if storage is not None:
        return storage
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage()


def _records(storage=None) -> List[dict]:
    storage = _storage(storage)
    base = str(getattr(storage, "base_dir", "") or os.path.dirname(
        str(storage.get_workflow_dir("x"))))
    records = []
    for path in sorted(glob.glob(os.path.join(base, "workflow_*", COMPILED_DESKTOP_FILE))):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable desktop compile record %s: %s", path, exc)
            continue
        if isinstance(record, dict):
            record["_source_dir"] = os.path.dirname(path)      # not digested, never sent
        records.append(record)
    return records


def approval_level_of(record: dict) -> Optional[str]:
    """The level a call of this record runs under: a draft workflow's SAVED level now (None when the saved
    workflow is not the proved artifact any more), else the record's ``approval_level`` (default ``auto``)."""
    if record.get("compile_class") == "draft_workflow":
        level = _current_draft_level(record)
        if level is None:
            return None
    else:
        level = str(record.get("approval_level") or "auto")
    return level if level in APPROVAL_LEVELS else "all"


def _current_draft_level(record: dict) -> Optional[str]:
    """A draft workflow tool: the approval level of the SAVED workflow beside it now (a level raised after the
    guest proof holds over MCP too), or None when the saved workflow is not the proved artifact any more."""
    path = os.path.join(str(record.get("_source_dir") or ""), "draft_workflow.json")
    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return None
    pinned = ((record.get("payload") or {}).get("record") or {}).get("verdict_keys") or {}
    if (saved.get("verdict_keys") or {}).get("artifact_digest") != pinned.get("artifact_digest"):
        return None
    from anyuse.execution.draft_compile import approval_level
    return approval_level(saved)


def _current_and_proved(record: dict) -> bool:
    proof = record.get("proof") or {}
    cls = str(record.get("compile_class") or "")
    if record.get("version") != COMPILED_DESKTOP_VERSION or cls not in ALLOWED_CLASSES:
        return False
    if cls == "app_script" and record.get("subtype") != "mesh_add_move":
        return False
    if cls in GUEST_CLASSES:
        from anyuse.execution.desktop_tools import MIN_GUEST_PROOFS
        if (proof.get("harness") != "guest" or int(proof.get("fresh_inputs") or 0) < MIN_GUEST_PROOFS
                or (record.get("payload") or {}).get("kind") != cls):
            return False
    return bool(proof.get("proved") and proof.get("artifact_digest")
                and proof["artifact_digest"] == artifact_digest(record))


def verify_record(record: dict) -> bool:
    """The proof pin of one record (the guest re-checks a record it is sent)."""
    try:
        return _current_and_proved(record)
    except Exception:  # noqa: BLE001 - a malformed record is not proved
        return False


def record_app(record: dict) -> Dict[str, str]:
    """The app a record is bound to: ``app`` (bundle id, name), else the
    template's ``required_app`` (a name)."""
    app = record.get("app") if isinstance(record.get("app"), dict) else {}
    return {"bundle_id": str(app.get("bundle_id") or ""),
            "name": str(app.get("name") or (record.get("workflow_template") or {}).get("required_app") or "")}


def _name(record: dict) -> str:
    """``desktop__<workflow>`` (mcpserver/tool_names.py)."""
    return joined(DESKTOP_NAMESPACE, str(record.get("workflow") or "workflow"))


def _key(record: dict) -> str:
    """What a shared name's suffix is hashed from: the record's workflow and its directory."""
    return f"{record.get('workflow') or ''}\0{os.path.basename(str(record.get('_source_dir') or ''))}"


def _legacy_name(record: dict) -> str:
    """The older dotted ``desktop.<workflow>``; still callable as an alias."""
    piece = _SAFE.sub("_", str(record.get("workflow") or "workflow")).strip("._-")
    return "desktop." + (piece[:80] or "workflow")


def _input_schema(record: dict) -> dict:
    properties, required = {}, []
    for row in record.get("parameters") or []:
        name = str(row.get("name") or "")
        kind = str(row.get("type") or "string")
        if not name or kind not in ("string", "number", "boolean", "array"):
            continue
        properties[name] = {"type": kind}
        if record.get("compile_class") in GUEST_CLASSES:
            for key in ("description", "enum"):
                if row.get(key):
                    properties[name][key] = row[key]
        if row.get("required", True):
            required.append(name)
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def executable_typed_tools(storage=None, *, guest_runner=None, executor=None) -> List[dict]:
    """The proved records as typed tools. ``guest_runner`` / ``executor`` are the
    server's own (``create_server``): with no executor a tool runs in the guest
    (``route: guest``) and is ready only when a guest runner is attached -- the
    injected one or ``ANYUSE_DESKTOP_GUEST_SESSION`` -- and its level holds
    nothing."""
    tools = []
    for record in _records(storage):
        if not _current_and_proved(record):
            continue
        proof = record.get("proof") or {}
        fresh = int(proof.get("fresh_inputs") or len(proof.get("rows") or []))
        cls = str(record["compile_class"])
        level = approval_level_of(record)
        holds = level is None or level in HOLD_LEVELS
        note = (f" Its approval level ({level}) holds steps: not run over MCP." if level in HOLD_LEVELS
                else (" Its saved workflow changed since the proof: not run." if level is None else ""))
        guest = {"approval_level": level, "holds": holds}
        if holds:
            guest["why_not"] = (f"its approval level ({level}) holds steps; a person runs it, not MCP"
                                if level else "the saved workflow is not the proved artifact; prove it again")
        if cls in GUEST_CLASSES or executor is None:
            guest.update(_guest_spec(record, holds, guest_runner))
        if cls in GUEST_CLASSES:
            description = (f"Run a desktop {cls.replace('_', ' ')} proved in the guest VM on {fresh} new "
                           "input(s), in the guest through its job agent (never on this host). Starting "
                           "the call is the approval: nothing is asked; a deviation stops it with its "
                           f"reason.{note}")
        else:
            description = (f"Run a compiled/proved desktop {cls} workflow through the "
                           f"checked executor; proof covered {fresh} unseen input(s)."
                           f"{note}")
        tools.append({
            "name": _name(record),
            "aliases": [_legacy_name(record)],
            "workflow": record.get("workflow"),
            "description": description,
            "input_schema": _input_schema(record),
            "output_schema": {"type": "object", "properties": {
                "ok": {"type": "boolean"}, "steps": {"type": "array"},
                "proof": {"type": "object"}}, "required": ["ok"],
                "additionalProperties": True},
            "proof": {"artifact_digest": proof.get("artifact_digest"),
                      "fresh_inputs": fresh},
            "compile_class": cls,
            "record": record,
            **guest,
        })
    # two workflows whose names differ only in characters a tool name cannot hold: each gets a suffix from its own
    # workflow and directory (mcpserver/tool_names.unique), whichever was compiled first
    keys = [_key(item["record"]) for item in tools]
    for item, key, name in zip(tools, keys, unique([(item["name"], key) for item, key in zip(tools, keys)])):
        base, own = item["name"], suffixed(item["name"], key)
        item["name"] = name
        if own != name:
            item["aliases"].append(own)
        if name != base:
            item["shares"] = base
    return sorted(tools, key=lambda item: item["name"])


def _guest_spec(record: dict, holds: bool, guest_runner=None) -> Dict[str, Any]:
    """Listing facts of a guest-routed tool: whether it runs without a person now."""
    from anyuse.execution import desktop_tools as DTools
    attached = guest_runner is not None or DTools.configured_runner() is not None
    out = {"route": "guest", "app": record_app(record), "ready": attached and not holds}
    if not holds and not attached:
        out["why_not"] = ("runs only in the guest VM and no guest runner is attached "
                          f"({DTools.GUEST_SESSION_ENV}); host-side execution is always refused")
    return out


def _bind(value: Any, arguments: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}", value)
        return arguments[match.group(1)] if match else value
    if isinstance(value, list):
        return [_bind(item, arguments) for item in value]
    if isinstance(value, dict):
        return {key: _bind(item, arguments) for key, item in value.items()}
    return value


def bind_step(raw: Any, arguments: Dict[str, Any], record: dict) -> "tuple":
    """(the step with the call's values, None) or (None, why). An AX step carries the record's app (its bundle id)
    in its selector and its command when they name none (a step compiled before selectors carried their app is
    then checked against the record's app right before acting);
    a selector or command that names ANOTHER app is refused before anything runs. Any other step: the values only."""
    step = _bind(raw, arguments)
    bid = record_app(record).get("bundle_id") or ""
    if not bid or not isinstance(step, dict) or step.get("modality") != "ax":
        return step, None
    cmd = dict(step.get("command") or {})
    sel = cmd.get("selector")
    if isinstance(sel, dict) and sel:
        if sel.get("app") and sel["app"] != bid:
            return None, f"the step's selector names the app {sel['app']!r}, the tool is bound to {bid!r}"
        cmd["selector"] = dict(sel, app=bid)
    app = cmd.get("app")
    if app and "." in str(app) and app != bid:
        return None, f"the step names the app {app!r}, the tool is bound to {bid!r}"
    if not app:
        cmd["app"] = bid
    return dict(step, command=cmd), None


def bind_steps(record: dict, arguments: Dict[str, Any]) -> "tuple":
    """Every step of a record bound with the call's values BEFORE any runs: (the steps, None), or (None, why) at the
    first step that names another app -- the whole call is refused and nothing is sent ("refused before
    anything runs"; binding step by step once typed a three-step record's first step before its second step,
    naming Notes, was refused)."""
    out = []
    for raw in record.get("steps") or []:
        bound, why = bind_step(raw, arguments, record)
        if why:
            return None, why
        out.append(bound)
    return out, None


async def run_typed_tool(tool_name: str, arguments: Optional[Dict[str, Any]] = None, *,
                         storage=None, executor=None,
                         approver: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
                         guest_runner=None) -> Optional[Dict[str, Any]]:
    """Run one compiled desktop tool. A call started with its values asks
    nothing (``approver`` is never called; an approval level that holds steps
    is refused). With no ``executor`` injected (the shipped server) the call
    goes to the guest runner (``guest_runner``, else
    ``ANYUSE_DESKTOP_GUEST_SESSION``) or is refused with the foreground
    interlock's typed reason; routines and draft workflows always go to the
    guest. An injected ``executor`` runs the older classes' checked steps in
    this process (tests)."""
    tool = lookup(executable_typed_tools(storage), tool_name)
    if tool is None:
        return None
    record = tool["record"]
    arguments = dict(arguments or {})
    schema = tool["input_schema"]
    missing = [name for name in schema["required"] if name not in arguments]
    extras = sorted(set(arguments) - set(schema["properties"]))
    if missing or extras:
        return {"ok": False, "error": f"invalid parameters; missing={missing}, extra={extras}"}

    cls = tool["compile_class"]
    # a call started with its values asks nothing, whatever its class; an
    # approval level that opts into holds is refused here (MCP has no person), before anything is sent
    level = approval_level_of(record)
    if level is None:
        return {"ok": False, "error": ("the saved workflow beside this tool is not the artifact that was "
                                       "proved in the guest (recompiled or removed); prove it again"),
                "proof": tool["proof"]}
    if level in HOLD_LEVELS:
        return {"ok": False, "blocked": True, "action_required": "human", "refused_by": "approval_level",
                "approval_level": level,
                "error": (f"its approval level ({level}) holds steps for a person; MCP has no person to "
                          "approve them (not run)"), "proof": tool["proof"]}
    # never on the host: an AX tree bound to the recorded app exists only in the guest;
    # refused before any person is asked
    route_guest = executor is None or cls in GUEST_CLASSES
    runner = None
    if route_guest:
        from anyuse.execution import desktop_tools as DTools
        runner = guest_runner or DTools.configured_runner()
        if runner is None:
            return dict(DTools.host_refusal(tool_name), proof=tool["proof"])

    if route_guest:
        import asyncio
        if cls == "routine":
            out = await asyncio.to_thread(DTools.run_routine, record, arguments, runner)
        else:
            out = await asyncio.to_thread(DTools.run_in_guest, record, arguments, runner)
        return dict(out, proof=tool["proof"])
    from anyuse.execution.plan_types import PlannedStep
    from anyuse.guides.state_graph import StateGraph
    from anyuse.memory.models import WorkflowTemplate
    workflow = WorkflowTemplate.from_dict(record.get("workflow_template") or {
        "id": record.get("workflow", ""), "name": record.get("workflow", "")})
    graph = StateGraph.from_dict(record.get("state_graph") or {})
    results = []
    bound_steps, why = bind_steps(record, arguments)
    if why:
        return {"ok": False, "steps": results, "error": why, "refused": True, "proof": tool["proof"]}
    for index, bound in enumerate(bound_steps):
        step = PlannedStep.from_dict(bound)
        executed, verification = await executor.execute_checked_step(
            step, step_idx=index, workflow=workflow, state_graph=graph)
        results.append({"execution": executed, "verification": verification})
        if not executed.get("success"):
            return {"ok": False, "steps": results, "error": executed.get("error"),
                    "proof": tool["proof"]}
    return {"ok": True, "steps": results, "proof": tool["proof"],
            "served_by": "execute_checked_step"}
