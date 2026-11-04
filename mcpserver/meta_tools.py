"""The fixed four-tool MCP surface: ``ANYUSE_MCP_PROFILE=meta``.

Under ``default`` every compiled workflow that may run adds one typed tool to
``tools/list`` (``<host>__<workflow>``, ``desktop__<workflow>``), and every
launch-eligible skill manifest adds one more. The listing therefore grows
linearly with what has been compiled, and an MCP client resends it with
every request. ``meta`` advertises four tools whose names and schemas are
constants of this module, so the listing is the same bytes at 0 workflows or
500 (docs/MCP_META.md has the measurement):

* ``list_sites`` -- hosts and desktop apps with compiled operations, with counts;
* ``list_operations(site)`` -- each operation's parameters, result keys, proof
  status, risk class and approval needs, as rows in a tool RESULT;
* ``call_operation(site, operation, args)`` -- runs one operation;
* ``login(site)`` -- does nothing and returns what to ask the user.

Execution is not reimplemented here. ``call_operation`` resolves
``(site, operation)`` to a workflow and hands it to two callables the server
supplies: the same closures its ``run_compiled_workflow`` and
``desktop__<workflow>`` tools dispatch through. On the web side that is
``compiled_tools.run_compiled`` with ``require_proved=True`` (proof passed,
status ``proved``, proof pinned to the current template, rung not demoted or
its check due, every host acknowledged, the complete bound request classified
``read_only`` by the HIL gate, read-only POST approval) followed by the server's block
handling (inbox card, elicitation). On the desktop side it is
``desktop_compiled_tools.run_typed_tool`` (proof digest, the record's approval
level -- a started call asks nothing, a level that holds steps is not run over
MCP --, ``execute_checked_step`` with its drift gate and the executor's
``ANYUSE_DRIVE_ENABLED`` gate). This module adds checks in front of those
paths (site/operation lookup, input schema validation, name ambiguity) and
never removes one.

What ``meta`` exposes is a projection of ``default``: web operations are the
rows ``list_compiled_workflows`` already lists, saved browser-replayed web tools
(kind ``web_tool``, docs/MCP_WEB_TOOLS.md) are exactly the typed web tools
``default`` advertises and run through the same route
(``execution/web_tool_call.py``), desktop operations are exactly
the ``desktop__<workflow>`` tools ``default`` advertises, and runs go through the
same entry points with the same arguments. Skills/routines, the inbox tool,
guides, saved-workflow listings and learning proposals are not part of it.

Results are data. Operation rows are built from record fields, counts and
enums; ``template.notes`` and other page text are not copied into them.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

TOOL_LIST_SITES = "list_sites"
TOOL_LIST_OPERATIONS = "list_operations"
TOOL_CALL_OPERATION = "call_operation"
TOOL_LOGIN = "login"
TOOL_NAMES = (TOOL_LIST_SITES, TOOL_LIST_OPERATIONS, TOOL_CALL_OPERATION, TOOL_LOGIN)

#: What ``login`` and the ``auth`` class ask of the person. This build has no
#: CLI sign-in command (per-site login is being built on another branch), so
#: no command is named.
SIGN_IN_STEP = ("AnyUse has no sign-in step on this build: the user signs in to {site} "
                "in their own browser, then retries")

_SITE = {"type": "string", "description": "a site from list_sites"}
TOOL_DEFINITIONS: Tuple[Dict[str, Any], ...] = (
    {"name": TOOL_LIST_SITES,
     "description": (
         "List the websites (hosts) and desktop apps that have compiled AnyUse "
         "operations, with how many each has and how many run without a person "
         "(ready). Read-only. Then call list_operations for one site."),
     "inputSchema": {"type": "object", "properties": {},
                     "additionalProperties": False}},
    {"name": TOOL_LIST_OPERATIONS,
     "description": (
         "List one site's operations: parameters (type, limits, example, hints), "
         "result keys, proof status, risk class, approval needs, and whether it "
         "runs without a person now (ready) or why not. Read-only."),
     "inputSchema": {"type": "object", "properties": {"site": _SITE},
                     "required": ["site"], "additionalProperties": False}},
    {"name": TOOL_CALL_OPERATION,
     "description": (
         "Run one operation through AnyUse's existing gates (proof, host "
         "acknowledgement, HIL risk class, per-call approval, drive and drift "
         "gates). Success: {ok: true, result}; a list of 2+ records comes back as "
         "{columns, rows} with null for a missing field. Failure: {ok: false, "
         "class, reason, next}. Follow next at most once, then stop and report "
         "to the user."),
     "inputSchema": {"type": "object", "properties": {
         "site": _SITE,
         "operation": {"type": "string", "description": "an operation from list_operations"},
         "args": {"type": "object", "description": "the operation's parameters"}},
         "required": ["site", "operation"], "additionalProperties": False}},
    {"name": TOOL_LOGIN,
     "description": (
         "AnyUse does not sign in, import a browser session or read cookies over "
         "MCP. Returns next: the step to hand to the user for this site."),
     "inputSchema": {"type": "object", "properties": {"site": _SITE},
                     "required": ["site"], "additionalProperties": False}},
)

# -- failure classes --------------------------------------------------------
UNKNOWN_SITE = "unknown_site"
UNKNOWN_OPERATION = "unknown_operation"
INPUT = "input"
NOT_PROVED = "not_proved"
STALE_PROOF = "stale_proof"
DEMOTED = "demoted"
HOST_NOT_ACKNOWLEDGED = "host_not_acknowledged"
NEEDS_APPROVAL = "needs_approval"
DRIVE_DISABLED = "drive_disabled"
BLOCKED = "blocked"
AUTH = "auth"
RATE = "rate"
FAILED = "failed"
#: the site did not answer (a refused connection, a timeout, a server error)
UNAVAILABLE = "unavailable"
NEEDS_USER = "needs_user"
ERROR = "error"
#: saved browser-replayed web tools (execution/web_tool_call.py, docs/MCP_WEB_TOOLS.md)
NEEDS_REVIEW = "needs_review"
WITHHELD = "withheld"
NO_BROWSER = "no_browser"
REFUSED = "refused"

FAILURE_CLASSES = (UNKNOWN_SITE, UNKNOWN_OPERATION, INPUT, NOT_PROVED, STALE_PROOF,
                   DEMOTED, HOST_NOT_ACKNOWLEDGED, NEEDS_APPROVAL, DRIVE_DISABLED,
                   BLOCKED, AUTH, RATE, FAILED, UNAVAILABLE, NEEDS_USER, ERROR, NEEDS_REVIEW, WITHHELD,
                   NO_BROWSER, REFUSED)

#: ``approval_needs`` entries: what has to be approved, and by whom, before or
#: during a run. An empty list means nothing beyond the proof.
APPROVAL_PER_CALL = "per_call_human"            # a person approves every call
APPROVAL_HOST = "host_acknowledgement"          # the user acknowledges the host (CLI)
APPROVAL_READONLY_POST = "readonly_post_approval"  # setting + exact endpoint approval
APPROVAL_NOT_OVER_MCP = "not_over_mcp"          # not read-only: never runs over MCP

#: parameter row keys copied into an operation listing when the record has them
_PARAM_KEYS = ("type", "description", "example", "pattern", "hint", "enum",
               "minimum", "maximum", "min_length", "max_length")

DESKTOP_SITE = "desktop"
KIND_WEB = "web"
KIND_DESKTOP = "desktop"
#: a saved browser-replayed web tool (execution/web_tool_call.py): it runs in the guarded container browser
KIND_WEB_TOOL = "web_tool"
#: the agent harness as one operation of its own site
AGENT_SITE = "agent"
AGENT_OPERATION = "run"
KIND_AGENT = "agent"


def tool_next(tool: str, arguments: Optional[Mapping[str, Any]] = None) -> str:
    """``list_operations {"site": "x"}``: an MCP tool and the arguments to pass."""
    return f"{tool} {json.dumps(dict(arguments or {}), ensure_ascii=False)}"


def user_next(step: str) -> str:
    """A step only the person takes; the agent passes it on and does not run it."""
    return f"hand to the user (do not run it yourself): {step}"


def cli(*words: str) -> str:
    """A CLI command for the user, every word shell-quoted (names and hosts are
    recorded data and may contain shell syntax)."""
    return "`" + " ".join(shlex.quote(str(word)) for word in words) + "`"


class ValidatorUnavailable(RuntimeError):
    """``jsonschema`` is missing: arguments cannot be checked, so nothing runs."""


# ---------------------------------------------------------------------------
# operations: one row per compiled web record or executable desktop tool
# ---------------------------------------------------------------------------

@dataclass
class Operation:
    site: str
    name: str
    kind: str
    row: Dict[str, Any]
    #: the schema the matching typed tool advertises; validated before a run
    input_schema: Dict[str, Any] = field(default_factory=dict)
    #: web: the workflow name run_compiled takes; desktop: the typed tool name
    target: str = ""
    #: desktop: the command a person runs locally for this record ("" when it runs only in the guest)
    local: Optional[str] = None


class _WorkflowDir:
    """The workflow directory by path alone.

    ``WorkflowStorage()`` creates the directory and parses every saved
    workflow (printing load warnings to stdout, the stdio transport). The
    listings only glob compile records under it, so they take the path.
    """

    def __init__(self):
        from anyuse.memory.storage import WorkflowStorage
        self.base_dir = WorkflowStorage.default_base_dir()

    def get_workflow_dir(self, workflow_id: str):
        return self.base_dir / f"workflow_{workflow_id}"


def _storage(storage=None):
    return storage if storage is not None else _WorkflowDir()


def compiled_web_records(storage=None) -> List[Tuple[str, Dict[str, Any]]]:
    """``(workflow name, migrated record)`` for every compile record on disk.

    The same files, migration and name rule as ``compiled_tools.list_compiled``,
    kept with the record so a listing reads each file once.
    """
    from anyuse.execution.api_compile import COMPILED_FILE, migrate_compiled_record

    storage = _storage(storage)
    base = str(getattr(storage, "base_dir", "") or os.path.dirname(
        str(storage.get_workflow_dir("x"))))
    out: List[Tuple[str, Dict[str, Any]]] = []
    for path in sorted(glob.glob(os.path.join(base, "workflow_*", COMPILED_FILE))):
        try:
            with open(path) as fh:
                record = migrate_compiled_record(json.load(fh))
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable compile record %s: %s", path, exc)
            continue
        name = record.get("workflow") or os.path.basename(os.path.dirname(path))[9:]
        out.append((str(name), record))
    return out


def _proof_examples(record: Dict[str, Any], inputs: List[str]) -> Dict[str, Any]:
    """One value per input, from the first passing proof case.

    These are the held-out inputs ``explain_workflow`` already lists as
    ``fresh_inputs_checked``; nothing else from the proof is copied.
    """
    for case in (record.get("proof") or {}).get("cases") or []:
        if not isinstance(case, dict) or not case.get("passed"):
            continue
        values = case.get("params") or case.get("values")
        if isinstance(values, dict):
            return {name: values[name] for name in inputs if name in values}
        if len(inputs) == 1 and case.get("value") not in (None, ""):
            return {inputs[0]: case["value"]}
    return {}


def _web_parameters(row: Dict[str, Any], template, record: Dict[str, Any]) -> List[dict]:
    inputs = [str(item.get("name")) for item in row.get("parameter_schema") or []]
    examples = _proof_examples(record, inputs)
    path_params = dict(getattr(template, "path_params", {}) or {})
    transforms = dict(getattr(template, "param_transforms", {}) or {})
    out = []
    for spec in row.get("parameter_schema") or []:
        name = str(spec.get("name"))
        item: Dict[str, Any] = {"name": name, "required": True}
        item.update({key: spec[key] for key in _PARAM_KEYS if key in spec})
        if name in examples and "example" not in item:
            item["example"] = examples[name]
        hints = []
        if name in path_params:
            hints.append(f"exactly {path_params[name]} path segments separated by '/'")
        if name in transforms:
            hints.append(f"sent as {transforms[name]}")
        if hints and "hint" not in item:
            item["hint"] = "; ".join(hints)
        out.append(item)
    return out


def _web_readiness(row: Dict[str, Any], template) -> Tuple[bool, str, List[str]]:
    """``(ready, why_not, approval_needs)``; ``why_not`` follows ``run_compiled``'s order."""
    from anyuse.mcpserver.compiled_tools import RUNNABLE_CLASS
    from anyuse.settings import get as get_setting
    proof, health = row["proof"], row["health"]
    needs: List[str] = []
    if not row.get("hosts_acknowledged"):
        needs.append(APPROVAL_HOST)
    if row.get("risk_class") != RUNNABLE_CLASS:
        needs.append(APPROVAL_NOT_OVER_MCP)
    post = bool(getattr(template, "readonly_post", False))
    if post:
        needs.append(APPROVAL_READONLY_POST)
    if not proof.get("passed"):
        return False, "the fresh-input proof did not pass", needs
    if proof.get("status") != "proved":
        return False, (f"proof status is {proof.get('status')!r}; only 'proved' runs "
                       "over MCP"), needs
    if not proof.get("current"):
        return False, "the template changed after its proof", needs
    if row.get("needs_browser") and not row.get("available_via"):
        return False, (f"{row.get('note') or 'served by the browser replay'}; no browser is "
                       "configured for this server"), needs
    if not health.get("preferred") and not row.get("available_via"):
        when = health.get("next_check_after") or ""
        return False, (f"the rung is demoted: {health.get('demoted_reason') or ''}"
                       + (f"; AnyUse sends it again after {when}" if when else "")), needs
    if not row.get("hosts_acknowledged"):
        return False, "a host is not acknowledged by the user", needs
    if row.get("risk_class") != RUNNABLE_CLASS:
        return (False, f"the HIL gate classifies it {row.get('risk_class')!r}; only "
                f"{RUNNABLE_CLASS!r} runs over MCP", needs)
    if post and not get_setting("ANYUSE_API_READONLY_POST"):
        return (False, "a read-only POST needs ANYUSE_API_READONLY_POST=on and an exact "
                "endpoint approval", needs)
    return True, "", needs


def web_operations(storage=None, hosts_path: Optional[str] = None) -> List[Operation]:
    """Every compile record, as ``list_compiled_workflows`` lists it."""
    out: List[Operation] = []
    for name, record in compiled_web_records(storage):
        try:
            out.append(_web_operation(name, record, hosts_path))
        except Exception as exc:              # one odd record never hides the others
            logger.warning("skipping malformed compile record %s: %s", name, exc)
    return out


def _web_operation(name: str, record: Dict[str, Any],
                   hosts_path: Optional[str]) -> Operation:
    from anyuse.execution.workflow_schema import json_schema_for_inputs, specs_from_dicts
    from anyuse.mcpserver import compiled_tools

    row = compiled_tools.describe_compiled(name, record, hosts_path)
    template = compiled_tools._template_of(record)
    ready, why_not, needs = _web_readiness(row, template)
    public = {
        "operation": name, "kind": KIND_WEB,
        "parameters": _web_parameters(row, template, record),
        "returns": {"shape": row["result_shape"],
                    "keys": list((getattr(template, "extract", {}) or {}).keys())},
        "proof": {"status": row["proof"]["status"], "id": row["proof"]["id"],
                  "current": row["proof"]["current"], "scope": row["proof"]["scope"]},
        "risk_class": row["risk_class"],
        "approval_needs": needs,
        "ready": ready,
        "tier": row["ladder_tier"],
    }
    if why_not:
        public["why_not"] = why_not
    if ready and row.get("degraded"):
        public["served_by_expected"] = row.get("available_via")
        public["degraded"] = row["degraded"]
    if row.get("needs_browser"):
        # no compiled request: the browser replay serves every call
        public["note"] = row.get("note")
        if ready:
            public["served_by_expected"] = row.get("available_via")
    if len(row.get("hosts") or []) > 1:
        public["hosts"] = list(row["hosts"])
    return Operation(
        site=str(row["host"]), name=name, kind=KIND_WEB, row=public,
        input_schema=json_schema_for_inputs(specs_from_dicts(
            getattr(template, "parameter_schema", []) or [])),
        target=name)


def desktop_operations(storage=None, specs: Optional[Callable[[Any], List[Dict[str, Any]]]] = None
                       ) -> List[Operation]:
    """Exactly the ``desktop__<workflow>`` tools the default profile advertises
    (``specs(storage)``: the server's own listing, which knows its guest runner)."""
    from anyuse.mcpserver import desktop_compiled_tools as DT
    from anyuse.settings import get as get_setting

    # the executor refuses every actuating step while the keystone gate is off
    drive = bool(get_setting("ANYUSE_DRIVE_ENABLED"))
    out: List[Operation] = []
    listing = specs if specs is not None else DT.executable_typed_tools
    for spec in listing(_storage(storage)):
        try:
            out.append(_desktop_operation(spec, drive, DT.FILE_CLASSES))
        except Exception as exc:              # one odd record never hides the others
            logger.warning("skipping malformed desktop record %s: %s", spec.get("name"), exc)
    return out


def _desktop_operation(spec: Dict[str, Any], drive: bool, file_classes) -> Operation:
    record = spec["record"]
    site = str((record.get("workflow_template") or {}).get("required_app") or DESKTOP_SITE)
    params = []
    for item in record.get("parameters") or []:
        name = str(item.get("name") or "")
        if name not in spec["input_schema"]["properties"]:
            continue
        row = {"name": name, "required": bool(item.get("required", True))}
        row.update({key: item[key] for key in _PARAM_KEYS if key in item})
        row.setdefault("type", "string")
        params.append(row)
    # a call started with its values asks nothing; only an approval level
    # that opts into holds keeps an operation from running over MCP
    holds = bool(spec.get("holds"))
    public = {
        "operation": str(record.get("workflow") or ""), "kind": KIND_DESKTOP,
        "parameters": params,
        "proof": {"status": "proved", "fresh_inputs": spec["proof"]["fresh_inputs"],
                  "digest": str(spec["proof"]["artifact_digest"] or "")[:12]},
        "risk_class": spec["compile_class"],
        "approval_needs": [APPROVAL_NOT_OVER_MCP] if holds else [],
        "ready": drive and not holds,
    }
    if spec.get("route") == "guest":
        # run in the guest (no host drive gate): ready when a guest runner is
        # attached and no approval level holds steps
        public["ready"] = bool(spec.get("ready")) and not holds
        if spec.get("why_not"):
            public["why_not"] = spec["why_not"]
    elif holds:
        public["why_not"] = spec.get("why_not") or "its approval level holds steps"
    elif not drive:
        public["why_not"] = ("desktop actuation is off (ANYUSE_DRIVE_ENABLED); the "
                             "executor refuses its steps")
    return Operation(site=site, name=public["operation"], kind=KIND_DESKTOP, row=public,
                     input_schema=spec["input_schema"], target=spec["name"],
                     local=local_command(record, public["operation"]))


def web_tool_operations(specs: Optional[Callable[[], List[Dict[str, Any]]]] = None) -> List[Operation]:
    """Exactly the saved web tools the default profile advertises (``specs()``: the server's own listing, which knows
    its API tool names). A row is built from the tool's enums and counts; the call needs a container browser."""
    from anyuse.execution import web_tool_call as WT
    if specs is None:
        return []
    out: List[Operation] = []
    endpoint = bool(os.environ.get(WT.ENDPOINT_ENV, "").strip())
    try:
        listed = specs()
    except Exception as exc:                  # an unreadable store hides the web tools, never the other operations
        logger.warning("skipping the saved web tools: %s", type(exc).__name__)
        return []
    for spec in listed:
        try:
            out.append(_web_tool_operation(spec, endpoint, WT))
        except Exception as exc:              # one odd record never hides the others
            logger.warning("skipping malformed saved web tool %s: %s", spec.get("name"), exc)
    return out


def _web_tool_operation(spec: Dict[str, Any], endpoint: bool, WT) -> Operation:
    tool = spec["entry"]["tool"]
    param: Dict[str, Any] = {"name": str(spec.get("param") or "value"), "required": True, "type": "string",
                             "min_length": 1, "max_length": WT.VALUE_MAX}
    if spec.get("example"):
        param["example"] = spec["example"]
    wf = spec["tool_workflow"]
    keys = ["results"] if wf != "product_lookup" else [str(k) for k in tool.get("output") or
                                                         ["name", "price", "currency", "availability", "url"]]
    public = {
        "operation": spec["workflow"], "kind": KIND_WEB_TOOL, "parameters": [param],
        "returns": {"shape": "fields" if wf == "product_lookup" else "list", "keys": keys},
        "proof": {"status": spec["proof"]["status"], "fresh_inputs": spec["proof"]["fresh_cases"],
                  "independent": spec["proof"]["independent"], "digest": str(spec["tool_sha256"])[:12]},
        "risk_class": "read_only", "approval_needs": [], "ready": endpoint,
        "tool": {"workflow": wf, "version": spec.get("version")}, "browser_only": bool(spec.get("browser_only")),
        "fallback": "on" if WT.fallback_enabled() else "off",
    }
    if not endpoint:
        public["why_not"] = (f"no container browser endpoint is attached ({WT.ENDPOINT_ENV}); a saved web tool runs "
                             f"only in a guarded container browser")
    return Operation(site=str(spec["host"]), name=str(spec["workflow"]), kind=KIND_WEB_TOOL, row=public,
                     input_schema=spec["input_schema"], target=spec["name"])


def local_command(record: Dict[str, Any], operation: str) -> str:
    """The command a person runs locally for a desktop record, by its compile class: a draft-compiled workflow
    replays with ``anyuse workflow replay`` (``workflow play`` refuses it), a routine runs only in the guest VM
    (""), every other class plays with ``anyuse workflow play``."""
    cls = str(record.get("compile_class") or "")
    if cls == "draft_workflow":
        return cli("anyuse", "workflow", "replay", operation) + " (with --param NAME=VALUE for each parameter)"
    if cls == "routine":
        return ""
    return cli("anyuse", "workflow", "play", operation)


def agent_operation() -> Operation:
    """The one ``agent`` operation: a constant row (the listing cannot grow with it)."""
    from anyuse.mcpserver import agent_tools as AT
    from anyuse.execution.harness_product import agent_model_problem
    # ready only when the default (model) policy has a model to call
    no_model = agent_model_problem()
    row = {"operation": AGENT_OPERATION, "kind": KIND_AGENT,
           "parameters": [{"name": k, "type": v.get("type", "string"), "description": v.get("description", ""),
                           **{x: v[x] for x in ("enum", "default") if x in v}}
                          for k, v in AT.TOOL_SCHEMA["properties"].items()],
           "proof": None, "risk_class": "open_ended_agent", "approval_needs": [], "ready": not no_model,
           **({"why_not": no_model} if no_model else {}),
           "note": ("an open-ended run that browses, reads and fills; a commit step (a login, a cart, a form "
                    "sent, a payment, a deletion) stops with human_required and goes to the user, unless the "
                    "user set ANYUSE_MCP_AGENT_COMMITS=agent: then a step the task asks for runs, a deviation "
                    "stops, and a step it did not ask for returns confirm_required with a resume token (answer "
                    "with resume + confirm, or resume + otp); the daily spend cap applies")}
    return Operation(site=AGENT_SITE, name=AGENT_OPERATION, kind=KIND_AGENT, row=row,
                     input_schema=AT.TOOL_SCHEMA, target=AT.TOOL_NAME)


def all_operations(compiled_storage=None, hosts_path: Optional[str] = None,
                   desktop_storage=None, desktop_specs=None, with_agent: bool = False,
                   web_tool_specs=None) -> List[Operation]:
    """The compiled operations (API recipes, saved browser web tools, desktop records); ``with_agent`` adds the agent
    entry (``call_operation`` only: ``list_sites`` lists compiled sites, and the agent is not one)."""
    return (web_operations(compiled_storage, hosts_path)
            + web_tool_operations(web_tool_specs)
            + desktop_operations(desktop_storage, desktop_specs)
            + ([agent_operation()] if with_agent else []))


# ---------------------------------------------------------------------------
# the four tools
# ---------------------------------------------------------------------------

def list_sites(operations: List[Operation]) -> Dict[str, Any]:
    sites: Dict[str, Dict[str, Any]] = {}
    for op in operations:
        entry = sites.setdefault(op.site, {"site": op.site, "kinds": [],
                                           "operations": 0, "ready": 0})
        if op.kind not in entry["kinds"]:
            entry["kinds"].append(op.kind)
        entry["operations"] += 1
        entry["ready"] += int(bool(op.row.get("ready")))
    rows = [sites[name] for name in sorted(sites)]
    return {"ok": True, "sites": rows, "count": len(rows)}


def failure(cls: str, reason: str, next_step: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "class": cls, "reason": reason, "next": next_step, **extra}


def resolve_site(site: str, operations: List[Operation]) -> str:
    """The site id as listed. A host given without its port (typed tool names drop it,
    ``127.0.0.1`` for ``127.0.0.1:18555``) resolves when exactly one listed site has that host."""
    site = str(site or "").strip()
    if not site or ":" in site or any(op.site == site for op in operations):
        return site
    hosts = {op.site for op in operations if op.site.split(":", 1)[0].lower() == site.lower()}
    return hosts.pop() if len(hosts) == 1 else site


def list_operations(site: str, operations: List[Operation]) -> Dict[str, Any]:
    site = resolve_site(site, operations)
    rows = [op.row for op in operations if op.site == site]
    if not rows:
        return failure(UNKNOWN_SITE, f"no compiled operations for site {site!r}",
                       tool_next(TOOL_LIST_SITES), site=site)
    return {"ok": True, "site": site, "operations": rows, "count": len(rows)}


def login(site: str, operations: List[Operation]) -> Dict[str, Any]:
    site = resolve_site(site, operations)
    return failure(
        NEEDS_USER,
        ("this MCP server never signs in, imports a browser session or reads "
         "cookies; signing in is a step the user takes"),
        user_next(SIGN_IN_STEP.format(site=shlex.quote(site) if site else "the site")),
        site=site, known_site=any(op.site == site for op in operations))


def _records_from(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) < 2:
        return None
    if not all(isinstance(item, dict) for item in value):
        return None
    columns: List[str] = []
    for item in value:
        for key in item:
            if key not in columns:
                columns.append(key)
    return {"columns": columns, "rows": [[item.get(key) for key in columns]
                                         for item in value]}


def shape_result(value: Any) -> Any:
    """A list of 2+ records becomes ``{columns, rows}``; ``None`` fills gaps.

    Applied to the result itself and to each value of a result object (an
    extraction is ``{name: value}``). Cells are left as they are.
    """
    table = _records_from(value)
    if table is not None:
        return table
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            table = _records_from(item)
            out[key] = item if table is None else table
        return out
    return value


def validate_args(args: Mapping[str, Any], schema: Mapping[str, Any]) -> List[str]:
    """The checks the SDK applies to a typed tool's ``inputSchema``."""
    properties = dict(schema.get("properties") or {})
    errors = [f"missing parameter {name!r}" for name in schema.get("required") or []
              if name not in args]
    errors += [f"unknown parameter {name!r}" for name in args if name not in properties]
    if errors:
        return errors
    try:
        import jsonschema
    except ImportError as exc:
        raise ValidatorUnavailable("jsonschema is not installed") from exc
    validator = jsonschema.Draft202012Validator(dict(schema))
    return [f"{'/'.join(map(str, err.path)) or 'args'}: {err.message}"
            for err in validator.iter_errors(dict(args))][:5]


def _web_failure(payload: Dict[str, Any], site: str, operation: str) -> Dict[str, Any]:
    """Map a ``run_compiled`` refusal (or failed run) to a class and a next step.

    The branches follow ``run_compiled``'s own order and the keys each refusal
    carries; only the two refusals that carry no distinctive key are read from
    their message.
    """
    error = str(payload.get("error") or "")
    ran = "served_by" in payload
    listing = tool_next(TOOL_LIST_OPERATIONS, {"site": site})
    again = f"afterwards call {TOOL_CALL_OPERATION} again"
    if "provenance" not in payload:
        return failure(UNKNOWN_OPERATION, error or "no compiled workflow with that name",
                       listing)
    blocked = payload.get("blocked")
    if blocked:
        from anyuse.execution import blocked as B
        reason = str((blocked or {}).get("reason") or "")
        need = payload.get("needs_human") or {}
        extra = {"needs_human": need} if need else {}
        if reason == B.LOGIN_WALL:
            return failure(AUTH, error or "the site asked for a sign-in",
                           user_next(SIGN_IN_STEP.format(site=shlex.quote(site))), **extra)
        if reason == B.RATE_LIMITED:
            wait = (blocked or {}).get("retry_after_s") or (blocked or {}).get("ttl_s")
            return failure(RATE, error or "the site is rate limiting",
                           ("do not retry now; AnyUse holds this host"
                            + (f" for {wait} s" if wait else "")
                            + "; report it to the user"), **extra)
        card = need.get("card")
        step = (f"the user completes the site's check "
                f"({cli('anyuse', 'inbox', 'do', card)} in their own terminal)"
                if card else "the user completes the site's check in their own browser")
        return failure(BLOCKED, error or "the site asked for a human",
                       user_next(f"{step}; {again}"), **extra)
    if payload.get("needs_browser") and not ran:
        if payload.get("no_browser"):
            return failure(NO_BROWSER, error, user_next(
                f"attach a throwaway container browser ({shlex.quote('ANYUSE_CDP_ENDPOINT')} or "
                f"{shlex.quote('ANYUSE_LEARN_ENDPOINT')} in the MCP server's environment); "
                f"{again}"), **({"rungs_tried": payload["rungs_tried"]}
                                if payload.get("rungs_tried") else {}))
        return failure(FAILED, error, user_next(
            f"the page may have changed: the user learns it again with "
            f"{cli('anyuse', 'learn')} (docs/QUICKSTART_COMPILE.md); nothing over MCP can"),
            **({"rungs_tried": payload["rungs_tried"]} if payload.get("rungs_tried") else {}))
    if "unacknowledged_hosts" in payload:
        hosts = list(payload.get("unacknowledged_hosts") or [])
        cmds = " and ".join(cli("anyuse", "workflow", "acknowledge-host", host)
                            for host in hosts)
        return failure(HOST_NOT_ACKNOWLEDGED, error,
                       user_next(f"the user runs {cmds} in their own terminal; {again}"),
                       detail={"unacknowledged_hosts": hosts})
    if "risk_class" in payload and not ran:
        return failure(NEEDS_APPROVAL, error,
                       user_next(f"this operation is not read-only and never runs over "
                                 f"MCP; the user runs "
                                 f"{cli('anyuse', 'workflow', 'play', operation)} "
                                 f"themselves if they want it"),
                       detail={"risk_class": payload["risk_class"]})
    if "health" in payload and not ran:
        oos = payload.get("out_of_service") or {}
        when = str(oos.get("next_check_after") or "")
        extra_rungs = {**({"rungs_tried": payload["rungs_tried"]} if payload.get("rungs_tried") else {}),
                       **({"heal": payload["heal"]} if payload.get("heal") else {}),
                       **({"probe": payload["probe"]} if payload.get("probe") else {})}
        if oos.get("cause") in ("outage", "rate_limit"):
            # the site did not answer or asked AnyUse to wait: the request comes back by itself,
            # nobody records again
            later = f"after {when}" if when else "later"
            what = ("the site asked AnyUse to wait (rate limit)" if oos.get("cause") == "rate_limit"
                    else "the site did not answer")
            return failure(DEMOTED, error, (
                f"{what}; AnyUse tries again {later}. Call {TOOL_CALL_OPERATION} "
                f"again {'after that time' if when else 'later'}; nothing needs recording again"),
                **extra_rungs)
        if when:
            error = f"{error}; AnyUse sends the compiled request again after {when}"
        return failure(DEMOTED, error,
                       user_next(f"the compiled request kept failing; "
                                 f"{cli('anyuse', 'workflow', 'play', operation)} runs the "
                                 f"recorded path locally, and recompiling restores it")
                       if payload.get("has_recorded_path", True) else
                       user_next(f"the compiled request kept failing and was taken out of "
                                 f"service: record the search again and recompile it with "
                                 f"{cli('anyuse', 'workflow', 'compile', operation)} "
                                 f"(docs/QUICKSTART_COMPILE.md)"),
                       **extra_rungs)
    if "parameters" in payload and not ran:
        return failure(INPUT, error, listing)
    if "proof" in payload and not ran:
        proof = payload.get("proof") or {}
        prove = user_next(f"compile and prove {shlex.quote(operation)} again "
                          f"(`anyuse workflow compile`, or more `anyuse attach` sessions "
                          f"for a held-out proof); nothing over MCP can prove it")
        if proof.get("passed") and proof.get("status") == "proved" and not proof.get("current"):
            return failure(STALE_PROOF, error, prove)
        return failure(NOT_PROVED, error, prove,
                       detail={"proof_status": proof.get("status")})
    rungs = {"rungs_tried": payload["rungs_tried"]} if payload.get("rungs_tried") else {}
    if payload.get("heal"):
        # why the call did not heal the tool from its result page (execution/step_heal.py)
        rungs["heal"] = payload["heal"]
    if ran and payload.get("not_found"):
        # the search answered and listed no result with that name: a different value, not a changed site
        from anyuse.mcpserver.compiled_tools import NOT_FOUND_NEXT
        return failure(INPUT, error, NOT_FOUND_NEXT, detail={"not_found": True}, **rungs)
    if ran and payload.get("policy_refused"):
        # the pre-send policy stopped the request before it was sent: neither a site that is down nor one that changed
        return failure(REFUSED, error, "stop and report the reason to the user: nothing was sent, and the reason "
                       "names the setting or the address that stopped the request", **rungs)
    if ran and payload.get("transient"):
        # a refused connection, a timeout or a server error says nothing about the request: no new recording
        wait = str(payload.get("retry_after") or "")
        return failure(UNAVAILABLE, error, (
            f"the site did not answer (a refused connection, a timeout or a server error), so nothing says the "
            f"request changed: "
            + (f"AnyUse sends it again after {wait}; " if wait else "")
            + f"call {TOOL_CALL_OPERATION} again later; if it still does not answer, report it "
            f"to the user"), **rungs)
    if ran:
        return failure(FAILED, error or "the compiled request failed",
                       user_next(f"the next rung is the recorded UI path, which this "
                                 f"server does not drive: "
                                 f"{cli('anyuse', 'workflow', 'play', operation)} in "
                                 f"their own terminal")
                       if payload.get("has_recorded_path", True) else
                       user_next(f"the site may have changed and there is no recorded path behind "
                                 f"this web tool: record the search again and recompile it with "
                                 f"{cli('anyuse', 'workflow', 'compile', operation)} "
                                 f"(docs/QUICKSTART_COMPILE.md); nothing over MCP can recompile it"),
                       **({"result_contract": payload["result_contract"]}
                          if payload.get("result_contract") else {}), **rungs)
    if error.startswith("parameter binding failed"):
        return failure(INPUT, error, listing)
    if "read-only POST" in error:
        return failure(NEEDS_APPROVAL, error,
                       user_next("the user enables `ANYUSE_API_READONLY_POST` (`anyuse "
                                 "settings set ANYUSE_API_READONLY_POST on`) and approves "
                                 f"this exact endpoint; {again}"))
    return failure(ERROR, error or "refused", "stop and report the reason to the user")


def _web_success(payload: Dict[str, Any]) -> Dict[str, Any]:
    proof = payload.get("proof") or {}
    out = {"ok": True, "result": shape_result(payload.get("result")),
           "served_by": payload.get("served_by"), "tier": payload.get("ladder_tier"),
           "latency_ms": payload.get("latency_ms"),
           "model_calls": payload.get("model_calls", 0),
           "proof": {"id": proof.get("id"), "status": proof.get("status")},
           "provenance": payload.get("provenance")}
    for key in ("result_contract", "empty_state", "rungs_tried", "next", "cost_usd", "agent",
                "recompiled", "healed"):
        if payload.get(key):
            out[key] = payload[key]
    return out


def _desktop_failure(payload: Dict[str, Any], site: str, operation: str,
                     local: Optional[str] = None) -> Dict[str, Any]:
    error = str(payload.get("error") or "")
    # the record's own local command (``local_command``): a draft workflow's is `workflow replay`, a routine has none
    play = cli("anyuse", "workflow", "play", operation) if local is None else local
    if payload.get("refused_by") == "approval_level":
        return failure(NEEDS_APPROVAL, error, user_next(
            "its approval level holds steps for a person: the user runs it where a person approves, "
            "or sets the level back to auto"))
    if payload.get("blocked"):
        if "denied" in error:
            return failure(NEEDS_APPROVAL, error, "stop: the person declined this call; "
                           "report it to the user")
        return failure(NEEDS_APPROVAL, error,
                       user_next(f"each call needs a person's approval and this MCP server "
                                 f"has no approver; " + (f"the user runs {play} themselves" if play else
                                                         "a person runs it in the guest VM")))
    if error.startswith("invalid parameters"):
        return failure(INPUT, error, tool_next(TOOL_LIST_OPERATIONS, {"site": site}))
    if "ANYUSE_DRIVE_ENABLED" in error:
        return failure(DRIVE_DISABLED, error,
                       user_next("desktop actuation is off; only the user can turn it on "
                                 "(`anyuse settings set ANYUSE_DRIVE_ENABLED on`, typed "
                                 "confirmation)"),
                       steps=shape_result(payload.get("steps") or []))
    if payload.get("refused_by") == "foreground_interlock":
        return failure(FAILED, error, user_next(
            "desktop operations run only in the guest VM through its job agent; the user "
            "attaches a guest runner or runs it there"), refusal=payload.get("refusal"),
            steps=shape_result(payload.get("steps") or []))
    return failure(FAILED, error or "a desktop step failed",
                   user_next(f"stop and report; the user can run {play} locally" if play else
                             "stop and report; it runs only in the guest VM through its job agent"),
                   steps=shape_result(payload.get("steps") or []))


def _web_tool_failure(payload: Dict[str, Any], site: str, operation: str) -> Dict[str, Any]:
    """A saved web tool's answer that is not ``ok``: its status and typed ``reason_code`` to a class and a next step."""
    status, code = str(payload.get("status") or ""), str(payload.get("reason_code") or "")
    reason = str(payload.get("reason") or code or status or "the saved web tool gave no answer")
    keep = {k: payload[k] for k in ("reason_code", "model_calls", "cost_usd", "requests", "fallback", "replay",
                                    "source", "tool_version", "browser_only") if k in payload}
    if status == NEEDS_REVIEW:
        return failure(NEEDS_REVIEW, reason, user_next(
            "the page's two independent sources disagree: decide from review_view (both sources' evidence) or treat "
            "the question as unanswered; the answer is not verified"), review_view=payload.get("review_view"), **keep)
    if status == WITHHELD:
        return failure(WITHHELD, reason, "stop and report: the answer could not be verified, so it is not returned",
                       **keep)
    if code == "no_browser":
        return failure(NO_BROWSER, reason, user_next(
            f"attach a throwaway container browser ({shlex.quote('ANYUSE_LEARN_ENDPOINT')} in the MCP server's "
            f"environment, docs/MCP_WEB_TOOLS.md); {TOOL_CALL_OPERATION} again afterwards"), **keep)
    if code == "input":
        return failure(INPUT, reason, tool_next(TOOL_LIST_OPERATIONS, {"site": site}), **keep)
    if code == "blocked":
        return failure(BLOCKED, reason, user_next("the site asked for a human; nothing automatic will help and it is "
                                                  "not contacted again by this route"), **keep)
    if code in ("rate_limited", "daily_cap"):
        return failure(RATE, reason, "do not retry now; report it to the user", **keep)
    if status == REFUSED:
        return failure(REFUSED, reason, "stop and report the reason to the user", **keep)
    return failure(FAILED, reason, user_next(
        f"the saved tool could not read the page as demonstrated; the user re-demonstrates it, or turns on the "
        f"verified model fallback (`anyuse settings set ANYUSE_WEB_FALLBACK on`) after seeing its cost per run"),
        **keep)


RunWeb = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]
RunDesktop = Callable[[str, Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]


async def call_operation(site: str, operation: str, args: Any, *,
                         operations: List[Operation], run_web: RunWeb,
                         run_desktop: RunDesktop, run_agent=None, run_web_tool=None,
                         run_rea=None) -> Dict[str, Any]:
    """Resolve ``(site, operation)``, validate ``args``, run through the server's path."""
    site, operation = resolve_site(site, operations), str(operation or "").strip()
    head = {"site": site, "operation": operation}
    on_site = [op for op in operations if op.site == site]
    if not on_site:
        return {**head, **failure(UNKNOWN_SITE, f"no compiled operations for site {site!r}",
                                  tool_next(TOOL_LIST_SITES))}
    listing = tool_next(TOOL_LIST_OPERATIONS, {"site": site})
    matches = [op for op in on_site if op.name == operation]
    if not matches:
        return {**head, **failure(UNKNOWN_OPERATION,
                                  f"site {site!r} has no operation {operation!r}", listing)}
    if len(matches) > 1:
        return {**head, **failure(INPUT, f"{len(matches)} operations on {site!r} share the "
                                  f"name {operation!r}",
                                  user_next("give the compiled workflows distinct names"))}
    op = matches[0]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {**head, **failure(INPUT, "args must be an object", listing)}
    try:
        problems = validate_args(args, op.input_schema)
    except ValidatorUnavailable as exc:
        return {**head, **failure(
            ERROR, f"{exc}; arguments cannot be validated, so nothing ran",
            user_next(f"install the mcp extra: {cli('pip', 'install', '-e', '.[mcp]')} "
                      f"in the AnyUse checkout's venv"))}
    if problems:
        return {**head, **failure(INPUT, "; ".join(problems), listing)}
    head["kind"] = op.kind
    if op.kind == "rea":
        if run_rea is None:
            return {**head, **failure(ERROR, "no REA runner is attached", "stop and report")}
        # The adapter returns the complete REA/restoration result. Never use
        # the desktop success projection, which would discard that evidence.
        return {**head, **await run_rea(op.target, dict(args))}
    if op.kind == KIND_AGENT:
        if run_agent is None:
            return {**head, **failure(ERROR, "no agent runner is attached to this server",
                                      user_next("start the MCP server from the anyuse checkout"))}
        payload = await run_agent(dict(args))
        if payload.get("ok"):
            return {**head, "ok": True, "result": shape_result(payload.get("result")),
                    "outcome": payload.get("outcome"), "reason": payload.get("reason"),
                    "model_calls": payload.get("model_calls"), "cost_usd": payload.get("cost_usd"),
                    "compiled": payload.get("compiled"), "served_by": "agent_harness",
                    **({"rea_executions": payload["rea_executions"]} if payload.get("rea_executions") else {})}
        outcome = payload.get("outcome")
        reason = str(payload.get("reason") or outcome or "the run stopped")
        if outcome == "confirm_required":
            # ANYUSE_MCP_AGENT_COMMITS=agent: the caller answers this one question
            nxt = ('answer this one question: call_operation {"site": "agent", "operation": "run", '
                   '"args": {"resume": "<the token>", "confirm": true|false}} (a one-time code: '
                   '{"resume": "<the token>", "otp": "..."}); no answer stops the run')
        elif outcome == "human_required" and reason.startswith("hand to the user"):
            nxt = reason                    # the step itself, for the user (agent_harness)
        else:
            nxt = user_next("read the reason; an unasked irreversible action or a refused "
                            "destination is the harness stopping on purpose")
        extra = {k: payload[k] for k in ("confirm_required", "resume", "rea_executions") if payload.get(k)}
        return {**head, **failure(FAILED, reason, nxt),
                "outcome": outcome, "model_calls": payload.get("model_calls"),
                "cost_usd": payload.get("cost_usd"), "compiled": payload.get("compiled"),
                "refusal": payload.get("refusal"), **extra}
    if op.kind == KIND_WEB_TOOL:
        if run_web_tool is None:
            return {**head, **failure(ERROR, "no web-tool route is attached to this server",
                                      user_next("start the MCP server from the anyuse checkout"))}
        payload = await run_web_tool(op.target, dict(args))
        if payload is None:
            return {**head, **failure(UNKNOWN_OPERATION, "the saved web tool is no longer served", listing)}
        if payload.get("ok"):
            return {**head, "ok": True, "result": shape_result(payload.get("result")),
                    "status": payload.get("status"), "source": payload.get("source"),
                    "verification": payload.get("verification"), "served_by": "browser_replay"
                    if payload.get("source") != "fallback" else "fallback",
                    **{k: payload[k] for k in ("model_calls", "cost_usd", "requests", "evidence", "fallback",
                                               "replay", "ladder", "tool_version", "browser_only") if k in payload}}
        return {**head, **_web_tool_failure(payload, site, operation)}
    if op.kind == KIND_WEB:
        # run_compiled resolves a workflow by name alone; refuse a name that two
        # records share rather than let it pick the other one.
        if sum(1 for other in operations
               if other.kind == KIND_WEB and other.target == op.target) > 1:
            return {**head, **failure(INPUT, f"two compiled records are named {operation!r}",
                                      user_next("give the compiled workflows distinct names"))}
        payload = await run_web(op.target, dict(args))
        if payload.get("status") == "done_by_human":
            return {**head, "ok": True, "result": shape_result(payload.get("human_result")),
                    "served_by": "human", "status": "done_by_human"}
        if payload.get("ok"):
            return {**head, **_web_success(payload)}
        return {**head, **_web_failure(payload, site, operation)}
    if sum(1 for other in operations
           if other.kind == KIND_DESKTOP and other.target == op.target) > 1:
        return {**head, **failure(INPUT, f"two desktop records map to {op.target!r}",
                                  user_next("give the compiled workflows distinct names"))}
    payload = await run_desktop(op.target, dict(args))
    if payload is None:
        return {**head, **failure(UNKNOWN_OPERATION, "the desktop tool is no longer "
                                  "executable", listing)}
    if payload.get("ok"):
        return {**head, "ok": True, "result": shape_result(payload.get("steps") or []),
                "served_by": payload.get("served_by"), "proof": payload.get("proof")}
    return {**head, **_desktop_failure(payload, site, operation, op.local)}


async def dispatch(name: str, arguments: Mapping[str, Any], *, compiled_storage=None,
                   hosts_path: Optional[str] = None, desktop_storage=None,
                   run_web: RunWeb, run_desktop: RunDesktop,
                   desktop_specs=None, run_agent=None, web_tool_specs=None,
                   run_web_tool=None, rea_specs=None, run_rea=None) -> Dict[str, Any]:
    """One of the four tools; the server checks ``name`` against its profile first."""
    import asyncio
    arguments = dict(arguments or {})
    try:
        operations = await asyncio.to_thread(
            all_operations, compiled_storage, hosts_path, desktop_storage, desktop_specs, False, web_tool_specs)
        if rea_specs is not None:
            from . import rea_tools
            operations += rea_tools.operations(await asyncio.to_thread(rea_specs))
        if name == TOOL_LIST_SITES:
            return list_sites(operations)
        if name == TOOL_LIST_OPERATIONS:
            site = str(arguments.get("site", "") or "").strip()
            if run_agent is not None and site == AGENT_SITE:
                # the agent entry is described on request; it is not a compiled site of list_sites
                return {"ok": True, "site": AGENT_SITE, "operations": [agent_operation().row], "count": 1}
            return list_operations(site, operations)
        if name == TOOL_LOGIN:
            return login(arguments.get("site", ""), operations)
        if name == TOOL_CALL_OPERATION:
            callable_ops = operations + ([agent_operation()] if run_agent is not None else [])
            return await call_operation(
                arguments.get("site", ""), arguments.get("operation", ""),
                arguments.get("args"), operations=callable_ops,
                run_web=run_web, run_desktop=run_desktop, run_agent=run_agent,
                run_web_tool=run_web_tool, run_rea=run_rea)
    except Exception as exc:
        logger.warning("meta tool %s failed: %s", name, type(exc).__name__)
        return failure(ERROR, f"{type(exc).__name__}: {exc}",
                       "stop and report the reason to the user")
    return failure(ERROR, f"unknown meta tool {name!r}", tool_next(TOOL_LIST_SITES))
