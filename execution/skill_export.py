"""Compiled tools as Agent Skills: ``anyuse skills export DIR`` (docs/INSTALL_FOR_AGENTS.md).

One folder per proved compiled tool that MCP serves, ``DIR/anyuse-<tool>/SKILL.md``, in the layout Claude Code,
Codex, OpenClaw and Hermes Agent all read: frontmatter between ``---`` lines, then a markdown body. The body tells
an agent to call the tool (typed MCP tool, ``call_operation``, or ``anyuse --json workflow play``) instead of
browsing the site again.

A skill is written for one MCP profile (``--profile``, else ``ANYUSE_MCP_PROFILE`` as set where the export runs,
:func:`resolve_profile`). Under ``meta`` the server lists only its four fixed tools and refuses a typed tool called
by name, so a meta skill pre-approves ``mcp__anyuse__call_operation`` and leads with that call; a default skill
pre-approves the typed tool ``mcp__anyuse__<tool>`` and leads with it. Both bodies show both calls.

Exported: compiled web tools whose proof passed with status ``proved`` and is current, read-only, on acknowledged
hosts and in service, or out of service only because the site did not answer; tools only the browser replay
proved; compiled desktop tools the MCP server lists and runs (an approval level that holds steps is not run
there). Every other record is skipped with the reason.

Format (the strictest of the four agents): ``name`` is ``anyuse-`` plus the tool name, lower-case letters, digits
and single hyphens, at most 64 characters, equal to the folder name; ``description`` is at most 1024 characters
with the deciding words in its first 250 (Claude Code cuts its skills list there); the body is never empty.

Every frontmatter value is written as a JSON string or a one-line JSON object, which is also a valid YAML
scalar or flow mapping: quotes and backslashes are escaped and nothing breaks a line. :func:`parse_frontmatter`
reads back exactly that subset, and the export checks each file with :func:`problems` before writing it.

The text comes from the compiled record's own fields: the host or app, the workflow name, parameter names and
types, proof values the user gave (a generated proof value may be page text, so it is left out), the result keys
and shape, and when it was proved. Page text, result values and ``template.notes`` never enter a skill: a skill
body is read by an agent as instructions. In the body every value from a record sits inside a code span or a
fenced block, after newlines, control characters and backticks are removed.

A folder is ours when its ``SKILL.md`` carries ``metadata.generated-by: anyuse``; any other folder is left alone.
Our folder is rewritten when its content would change, or with ``force``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import tempfile
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
PREFIX = "anyuse-"
#: the ``generated-by`` marker in ``metadata`` that makes a folder ours
MARKER = "anyuse"
#: the name the README's ``claude mcp add`` line registers the server under (``mcp__anyuse__<tool>``, or
#: ``mcp__anyuse__call_operation`` under the meta profile)
MCP_SERVER = "anyuse"
#: the MCP profiles a skill names its tool for (``ANYUSE_MCP_PROFILE``, mcpserver/server.py)
PROFILE_DEFAULT = "default"
PROFILE_META = "meta"
PROFILES = (PROFILE_DEFAULT, PROFILE_META)
NAME_MAX = 64
DESCRIPTION_MAX = 1024
#: Claude Code's skills list shows this many characters of a description
DESCRIPTION_SHOWN = 250
COMPATIBILITY_MAX = 500
#: the longest host name DNS allows: a host is never cut
SITE_MAX = 253
KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

KIND_WEB = "web"
KIND_BROWSER = "browser_replay"
KIND_DESKTOP = "desktop"

_NOT_NAME = re.compile(r"[^a-z0-9]+")
_LINE = re.compile(r"([a-z][a-z-]*): (.+)")
#: a read of an existing SKILL.md stops here; ours are a few kilobytes
_READ_MAX = 1 << 20

UNKNOWN_TOOL = "no compiled tool with that name. `anyuse workflow list` shows the names."


# ---------------------------------------------------------------------------
# names and text
# ---------------------------------------------------------------------------

def skill_name(tool: Any) -> str:
    """``anyuse-`` plus the tool name, lower-cased, each run of characters outside ``[a-z0-9]`` one hyphen, cut
    to 64 characters."""
    piece = _NOT_NAME.sub("-", str(tool or "").lower()).strip("-") or "tool"
    return (PREFIX + piece)[:NAME_MAX].rstrip("-")


def numbered(name: str, n: int) -> str:
    """``name`` with ``-<n>`` on the end, cut so the whole stays within 64 characters."""
    tag = f"-{n}"
    return name[:NAME_MAX - len(tag)].rstrip("-") + tag


def text(value: Any, limit: int = 200) -> str:
    """One line: control, format and line-separator characters become spaces, a backtick becomes ``'``,
    whitespace runs collapse, cut to ``limit``."""
    out = []
    for ch in str(value if value is not None else ""):
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zl", "Zp"):
            out.append(" ")
        else:
            out.append("'" if ch == "`" else ch)
    return " ".join("".join(out).split())[:limit].rstrip()


def _code(value: Any, limit: int = 120) -> str:
    return f"`{text(value, limit) or '?'}`"


def _scalar(value: Any) -> str:
    """A YAML double-quoted scalar, written as a JSON string."""
    return json.dumps(text(value, DESCRIPTION_MAX), ensure_ascii=False)


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    kind: str
    #: the workflow name (``anyuse workflow list``, ``call_operation``'s ``operation``)
    tool: str
    #: the host (web) or the app (desktop): ``call_operation``'s ``site``
    site: str
    #: the typed MCP tool name under the default profile
    mcp_name: str = ""
    #: ``{name, type, required, example?}``
    params: List[Dict[str, Any]] = field(default_factory=list)
    #: ``list`` / ``fields`` (web); "" (desktop)
    shape: str = ""
    keys: List[str] = field(default_factory=list)
    proved_at: str = ""
    proof_id: str = ""
    passed: Optional[int] = None
    checked: Optional[int] = None
    latency_ms: Optional[float] = None
    app: str = ""
    #: how many HTTP requests one call sends (a chain sends several, in order)
    requests: int = 1

    @property
    def identity(self) -> Tuple[str, str]:
        """What ``metadata`` records the folder is for: the tool and its host, as written."""
        return text(self.tool), text(self.site, SITE_MAX)


def _skip(tool: Any, host: Any, reason: str, kind: str = "") -> Dict[str, Any]:
    return {"tool": str(tool or ""), "host": str(host or ""), **({"kind": kind} if kind else {}),
            "reason": reason}


def _proof_values(record: Dict[str, Any], inputs: Sequence[str]) -> Dict[str, Any]:
    """One value per input from the first passing proof case whose values the user gave (a generated value
    came from the result page or a model)."""
    for case in (record.get("proof") or {}).get("cases") or []:
        if not isinstance(case, dict) or not case.get("passed") or case.get("generated"):
            continue
        values = case.get("params") or case.get("values")
        if isinstance(values, dict):
            return {n: values[n] for n in inputs if isinstance(values.get(n), (str, int, float, bool))}
        value = case.get("value")
        if len(inputs) == 1 and isinstance(value, (str, int, float, bool)) and value != "":
            return {inputs[0]: value}
    return {}


def _web_refusal(row: Dict[str, Any], template, record: Dict[str, Any], hosts_path: Optional[str]) -> str:
    """Why MCP's default profile would not serve this web record as a proved tool, or ""."""
    from anyuse.execution.api_permission import host_acknowledged
    from anyuse.mcpserver import compiled_tools as CT
    from anyuse.settings import get as get_setting
    proof = row["proof"]
    if not proof.get("passed"):
        return "not proved: its fresh-input proof did not pass. Compile it again (docs/COMPILE.md), then export."
    if proof.get("status") != "proved":
        return (f"its proof status is {proof.get('status')!r}, not 'proved'. Prove it on held-out inputs "
                f"(docs/COMPILE.md), then export.")
    if not proof.get("current"):
        return "the tool changed after its proof. Prove it again, then export."
    if row.get("risk_class") != CT.RUNNABLE_CLASS:
        return (f"not read-only (the HIL gate classifies it {row.get('risk_class')!r}). MCP runs only read-only "
                f"tools, so this one gets no skill.")
    if not row.get("hosts_acknowledged"):
        hosts = [h for h in row.get("hosts") or [row.get("host")] if not host_acknowledged(h, hosts_path)]
        cmds = " and ".join(f"`anyuse workflow acknowledge-host {shlex.quote(str(h))}`" for h in hosts)
        return f"its host is not acknowledged. Run {cmds or '`anyuse workflow acknowledge-host`'}, then export."
    if CT.replay_tool(record):
        if not (record.get("browser") or {}).get("template"):
            return "no browser replay is saved with it. Learn it again (`anyuse learn`), then export."
    elif not (row.get("health") or {}).get("preferred", True) and \
            (row.get("health") or {}).get("out_of_service_cause") != "outage":
        # a request taken out while the site did not answer is still the proved one: it goes back
        # in service when the site answers again (api_recompile.demotion_cause), so it is exported
        return ("its compiled request is out of service: it gave answers that failed its checks. "
                "`anyuse workflow list` says what to do. Export again once it is back in service.")
    if getattr(template, "readonly_post", False):
        if not get_setting("ANYUSE_API_READONLY_POST"):
            return ("it is a read-only POST, which runs only with ANYUSE_API_READONLY_POST=on and its endpoint "
                    "approved. Turn that on, then export.")
        # the run checks the same approval (compiled_tools.run_compiled, api_step.readonly_post_enabled)
        from anyuse.execution.api_permission import readonly_post_approved
        from anyuse.execution.api_step import readonly_post_operation
        from anyuse.execution.request_policy import origin
        url, operation = str(template.url), readonly_post_operation(template.body)
        if not readonly_post_approved(url, operation, hosts_path):
            endpoint = origin(url) + (urllib.parse.urlsplit(url).path or "/")
            words = ["anyuse", "workflow", "acknowledge-host", urllib.parse.urlsplit(url).netloc,
                     "--readonly-post-endpoint", endpoint, "--readonly-post-operation", operation]
            return (f"its read-only POST endpoint is not approved. Run "
                    f"`{' '.join(shlex.quote(w) for w in words)}`, then export.")
    if not row.get("host"):
        return "its compile record names no host. Compile it again, then export."
    return ""


def _web_tools(storage, hosts_path: Optional[str]) -> Tuple[List[Tool], List[Dict[str, Any]], List[str]]:
    """``(tools, skipped, names)``: the exportable web tools, the skipped records, and the typed names of every
    web tool the default profile could list (the saved web tools' names are fitted around them)."""
    from anyuse.mcpserver import compiled_tools as CT
    from anyuse.mcpserver import meta_tools as MT
    from anyuse.mcpserver.tool_names import unique

    rows, skipped = [], []
    for name, record in MT.compiled_web_records(storage):
        try:
            row = CT.describe_compiled(name, record, hosts_path)
            template = CT._template_of(record)
            why = _web_refusal(row, template, record, hosts_path)
        except Exception as exc:              # one odd record never hides the others
            logger.warning("skipping malformed compile record %s: %s", name, exc)
            skipped.append(_skip(name, "", f"its compile record could not be read ({type(exc).__name__}). "
                                           f"Compile it again, then export.", KIND_WEB))
            continue
        if why:
            skipped.append(_skip(name, row.get("host"), why,
                                 KIND_BROWSER if CT.replay_tool(record) else KIND_WEB))
        rows.append((row, record, template, not why))
    # the typed names as compiled_tools.host_tool_sets gives them: one naming pass over every listed tool, so a
    # name two tools share gets the same suffix here as there
    named = [(row, record, template, ok) for row, record, template, ok in rows
             if ok or row.get("runnable_without_human")]
    names = unique([(CT.typed_tool_name(row["host"], row["workflow"]),
                     CT.typed_tool_key(row["host"], row["workflow"])) for row, _, _, _ in named])
    tools = []
    for (row, record, template, ok), typed in zip(named, names):
        if not ok:
            continue
        inputs = [str(p.get("name")) for p in row.get("parameter_schema") or []]
        values = _proof_values(record, inputs)
        params = []
        for spec in row.get("parameter_schema") or []:
            item = {"name": str(spec.get("name")), "type": str(spec.get("type") or "string"), "required": True}
            if item["name"] in values:
                item["example"] = values[item["name"]]
            params.append(item)
        proof = row["proof"]
        latency = row.get("latency_ms_median_at_compile_time")
        tools.append(Tool(
            kind=KIND_BROWSER if CT.replay_tool(record) else KIND_WEB, tool=str(row["workflow"]),
            site=str(row["host"]), mcp_name=typed, params=params, shape=str(row.get("result_shape") or "list"),
            keys=[str(k) for k in (getattr(template, "extract", {}) or {})],
            proved_at=str(row.get("compiled_at") or ""), proof_id=str(proof.get("id") or ""),
            passed=_count(proof.get("fresh_cases_passed")), checked=_count(proof.get("fresh_cases_checked")),
            latency_ms=CT.round_ms(latency) if _count(latency) is not None else None,
            requests=len(getattr(template, "steps", None) or []) or 1))
    return tools, skipped, names


def _count(value: Any) -> Optional[float]:
    """A number from a record, or None (a bool or a string is not one)."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _desktop_tools(storage) -> Tuple[List[Tool], List[Dict[str, Any]]]:
    """The desktop tools MCP lists and runs (``desktop_compiled_tools.executable_typed_tools``), and the rest."""
    from anyuse.mcpserver import desktop_compiled_tools as DT

    skipped = []
    for record in DT._records(storage):
        if not DT.verify_record(record):
            skipped.append(_skip(record.get("workflow"), _app_site(record),
                                 "not proved, or changed since its proof. Prove it again, then export.",
                                 KIND_DESKTOP))
    tools = []
    for spec in DT.executable_typed_tools(storage):
        record = spec["record"]
        site = _app_site(record)
        if spec.get("holds"):
            level = spec.get("approval_level")
            skipped.append(_skip(record.get("workflow"), site, (
                f"its approval level ({level}) holds steps for a person, and MCP has no person to approve "
                f"them, so MCP does not run it." if level else
                "its saved workflow changed since the guest proof. Prove it again, then export."), KIND_DESKTOP))
            continue
        schema = spec.get("input_schema") or {}
        required = set(schema.get("required") or [])
        params = [{"name": str(name), "type": str((prop or {}).get("type") or "string"),
                   "required": name in required}
                  for name, prop in (schema.get("properties") or {}).items()]
        proof = spec.get("proof") or {}
        tools.append(Tool(
            kind=KIND_DESKTOP, tool=str(record.get("workflow") or ""), site=site, mcp_name=str(spec["name"]),
            params=params, proved_at=str(record.get("compiled_at") or ""),
            proof_id=str(proof.get("artifact_digest") or "")[:12],
            checked=_count(proof.get("fresh_inputs")),
            app=DT.record_app(record).get("name") or site))
    return tools, skipped


def _app_site(record: Dict[str, Any]) -> str:
    """The desktop record's ``call_operation`` site, as meta_tools lists it."""
    from anyuse.mcpserver.meta_tools import DESKTOP_SITE
    return str((record.get("workflow_template") or {}).get("required_app") or DESKTOP_SITE)


def collect(storage=None, hosts_path: Optional[str] = None) -> Tuple[List[Tool], List[Dict[str, Any]]]:
    """``(tools, skipped)``: every exportable tool with its typed MCP name, and every other record with why."""
    from anyuse.execution import web_tool_call
    from anyuse.mcpserver import meta_tools as MT
    from anyuse.mcpserver.tool_names import Registry

    storage = storage if storage is not None else MT._WorkflowDir()
    web, skipped, web_names = _web_tools(storage, hosts_path)
    try:
        saved = web_tool_call.tool_specs(storage, taken=web_names)
    except Exception as exc:                  # an unreadable store hides the saved tools, never the others
        logger.warning("skipping the saved web tools: %s", type(exc).__name__)
        saved = []
    for spec in saved:
        skipped.append(_skip(spec.get("workflow"), spec.get("host"), (
            f"a saved web tool from an agent run; skills export does not cover these yet. Agents call it "
            f"over MCP as {spec.get('name')}."), web_tool_call.KIND))
    try:
        desktop, desktop_skipped = _desktop_tools(storage)
    except Exception as exc:                  # an unreadable desktop record hides the desktop tools only
        logger.warning("skipping the desktop tools: %s", type(exc).__name__)
        desktop, desktop_skipped = [], []
    skipped += desktop_skipped
    # one listing, in the server's order: a name two kinds would share is suffixed as the server suffixes it
    table = Registry()
    for tool in web:
        tool.mcp_name = table.add("web_api", tool.mcp_name).name
    for spec in saved:
        table.add("web_tool", spec["name"])
    for tool in desktop:
        tool.mcp_name = table.add("desktop", tool.mcp_name).name
    tools = sorted(web + desktop, key=lambda t: (t.tool.lower(), t.site.lower(), t.kind, t.tool))
    return tools, sorted(skipped, key=lambda s: (s["tool"].lower(), s["host"].lower(), s["tool"]))


# ---------------------------------------------------------------------------
# SKILL.md
# ---------------------------------------------------------------------------

def resolve_profile(value: Optional[str] = None) -> str:
    """The MCP profile the skills name their tool for: ``value``, else ``ANYUSE_MCP_PROFILE`` as this process or
    settings.toml sets it. Only ``meta`` is the meta profile; any other value (``default``, ``full``, unknown) is
    the default profile, which lists the typed tools, as the server reads it (``server.mcp_profile``)."""
    from anyuse.settings import get as get_setting
    given = str(value).strip().lower() if value is not None else None
    try:
        chosen = get_setting("ANYUSE_MCP_PROFILE", given)
    except Exception:                         # an unreadable setting is the default, as at server start
        chosen = given
    return PROFILE_META if str(chosen or "").strip().lower() == PROFILE_META else PROFILE_DEFAULT


def allowed_tool(tool: Tool, profile: str = PROFILE_DEFAULT) -> str:
    """The Claude Code tool name the skill pre-approves: ``call_operation`` under the meta profile, else the
    typed tool."""
    from anyuse.mcpserver.meta_tools import TOOL_CALL_OPERATION
    return f"mcp__{MCP_SERVER}__{TOOL_CALL_OPERATION if profile == PROFILE_META else tool.mcp_name}"


def _placeholder(kind: str) -> Any:
    return {"integer": 0, "number": 0, "boolean": False, "array": []}.get(kind, "VALUE")


def _args(tool: Tool) -> Dict[str, Any]:
    out = {}
    for p in tool.params:
        value = p.get("example", _placeholder(p["type"]))
        out[text(p["name"], 80)] = text(value, 120) if isinstance(value, str) else value
    return out


def description(tool: Tool) -> str:
    """What the tool looks up, where, by what; that it is proved and fast; to use it instead of browsing."""
    what = text(re.sub(r"[\W_]+", " ", tool.tool), 60) or "results"
    site = text(tool.site, SITE_MAX) or "the site"
    # whole parameter names, about 80 characters of them; the rest are counted
    names, shown = [text(p["name"], 40) for p in tool.params], []
    for name in names:
        if shown and len(", ".join(shown + [name])) > 80:
            break
        shown.append(name)
    params = ", ".join(shown) + (f" and {len(names) - len(shown)} more" if len(shown) < len(names) else "")
    if tool.kind == KIND_DESKTOP:
        app = text(tool.app, 80) or site
        head = f"Run {what} in {app}" + (f" with {params}" if params else "") + "."
        claim = " Proved desktop tool: runs in the guest VM with no model."
        tails = (f" Use instead of driving {app} yourself.", " Use instead of driving the app yourself.")
    else:
        head = f"Look up {what} on {site}" + (f" by {params}" if params else "") + "."
        claim = (" Proved browser replay: answers in seconds in a container browser, with no model."
                 if tool.kind == KIND_BROWSER else
                 " Proved compiled tool: answers in milliseconds with no browsing.")
        tails = (f" Use instead of opening {site} in a browser.", " Use instead of opening the site in a browser.")
    out = head + claim + tails[0]
    if len(out) > DESCRIPTION_SHOWN:
        out = head + claim + tails[1]
    return out[:DESCRIPTION_MAX]


def compatibility(tool: Tool, profile: str = PROFILE_DEFAULT) -> str:
    server = f"the AnyUse MCP server added under the name {MCP_SERVER}" + (
        " with ANYUSE_MCP_PROFILE=meta" if profile == PROFILE_META else "")
    if tool.kind == KIND_DESKTOP:
        return f"Needs {server}, with a guest runner attached (ANYUSE_DESKTOP_GUEST_SESSION)."
    out = f"Needs {server}, or the anyuse command on PATH."
    if tool.kind == KIND_BROWSER:
        out += " Each call needs a container browser (ANYUSE_CDP_ENDPOINT or ANYUSE_LEARN_ENDPOINT)."
    return out


def _body(tool: Tool, profile: str = PROFILE_DEFAULT) -> List[str]:
    c = _code
    site, app = c(tool.site, SITE_MAX), c(tool.app or tool.site, SITE_MAX)
    when = f" {c(tool.proved_at, 40)}" if tool.proved_at else ""
    lines = [f"# {c(tool.tool)} {'in' if tool.kind == KIND_DESKTOP else 'on'} {site}", "",
             "Written by `anyuse skills export` from this compiled tool's record. Edits here are lost when "
             "the export runs again after the tool changes.", ""]
    if tool.kind == KIND_DESKTOP:
        lines.append(f"A proved desktop tool for {app}. Over MCP it runs in the guest VM through its job agent, "
                     "never on this host.")
        lines.append(f"Proved{when}" + (f" on {tool.checked} new inputs" if tool.checked is not None else "")
                     + (f" (digest {c(tool.proof_id)})." if tool.proof_id else "."))
        lines.append("On success it returns `ok: true` and the checked steps.")
    else:
        if tool.kind == KIND_BROWSER:
            lines.append("No compiled request: each call replays the page in a container browser and takes "
                         "seconds, with no model. The browser is `ANYUSE_CDP_ENDPOINT` or `ANYUSE_LEARN_ENDPOINT` "
                         "where the MCP server or the shell runs. The server lists the typed tool only while it "
                         "has one; without one, `call_operation` and the shell return `ok: false` and say so.")
        else:
            sent = (f"One proved HTTP request to {site}" if tool.requests == 1 else
                    f"{tool.requests} proved HTTP requests in a row, the first to {site}")
            lines.append(f"{sent}: no browser, no model."
                         + (f" It answered in a median {tool.latency_ms} ms when it was proved."
                            if tool.latency_ms is not None else ""))
        counts = (f": {tool.passed} of {tool.checked} held-out inputs matched"
                  if tool.passed is not None and tool.checked is not None else "")
        lines.append(f"Proved{when}{counts}" + (f" (proof {c(tool.proof_id)})." if tool.proof_id else "."))
        keys = ", ".join(c(k, 60) for k in tool.keys) or "`items`"
        lines.append(f"On success `result` holds a list in {keys}." if tool.shape == "list" else
                     f"On success `result` holds the fields {keys}.")
    args = _args(tool)
    meta_call = {"site": text(tool.site, SITE_MAX), "operation": text(tool.tool), "args": args}
    typed = [f"With the server's default profile, call the tool {c(tool.mcp_name)} with:", "",
             "```json", json.dumps(args, ensure_ascii=False), "```"]
    if profile == PROFILE_META:
        lines += ["", "## Call it over MCP", "",
                  "The server runs with the meta profile (`ANYUSE_MCP_PROFILE=meta`): call `call_operation` with:",
                  "", "```json", json.dumps(meta_call, ensure_ascii=False), "```", "",
                  "Under the meta profile a typed tool called by name is refused. " + typed[0], *typed[1:]]
    else:
        lines += ["", "## Call it over MCP", "", *typed, "",
                  "With the meta profile (`ANYUSE_MCP_PROFILE=meta`), call `call_operation` with:", "",
                  "```json", json.dumps(meta_call, ensure_ascii=False), "```"]
    lines += ["", "## Call it from a shell", ""]
    if tool.kind == KIND_DESKTOP:
        lines.append("There is no shell call: this tool runs only through the MCP server, in the guest VM.")
    else:
        # without --no-fallback a failed call goes on to the agent rung (model calls) and, for a tool compiled
        # from a recording, to that recording replayed on this desktop (cli.workflow_play)
        words = ["anyuse", "--json", "workflow", "play", text(tool.tool), "--no-fallback"]
        for name, value in args.items():
            words += ["--param", f"{name}={value if not isinstance(value, bool) else str(value).lower()}"]
        rung = "the browser replay" if tool.kind == KIND_BROWSER else "the proved request"
        lines += ["```sh", " ".join(shlex.quote(str(w)) for w in words), "```", "",
                  "It prints one JSON object with `ok` and `result`; when `ok` is false, `reason` and `next`. "
                  f"`--no-fallback` keeps the call to {rung}: no model calls, and no recording replayed on "
                  "this desktop."]
    if tool.kind == KIND_DESKTOP:
        fails = ("`ok` is false. `call_operation` gives `reason` and `next`; the typed tool gives the reason in "
                 "`error`. They say whether the user has to attach a guest runner, approve the steps, or prove "
                 "the tool again. ")
        avoid = f"Do not drive {app} yourself to work around a refusal."
    else:
        fails = ("`ok` is false. `call_operation` and the shell give `reason` and `next`; the typed tool gives "
                 "the reason in `error` and the next step in `fallback` or `next`. They say whether to try "
                 "again later, to ask the user to sign in or clear a check, or to record the tool again. ")
        avoid = f"Do not open {site} in a browser to work around a refusal."
    lines += ["", "## When it fails", "",
              fails + "A `next` that starts with `hand to the user` is a step for the user: pass it on, do not "
              "run it yourself. " + avoid, "", "## Parameters", ""]
    for p in tool.params:
        line = f"- {c(p['name'], 80)}: {text(p['type'], 20) or 'string'}"
        if not p.get("required", True):
            line += ", optional"
        line += (f". Proof value: {c(p['example'])}." if "example" in p else ". No proof value is recorded.")
        lines.append(line)
    if not tool.params:
        lines.append("- none")
    return lines


def render(tool: Tool, name: str, profile: str = PROFILE_DEFAULT) -> str:
    """The SKILL.md for ``tool`` in folder ``name``, naming the tool ``profile`` serves."""
    # string values only, as the Agent Skills format defines metadata. No OpenClaw
    # requires.bins: the install puts anyuse in a venv, often not on PATH, and
    # OpenClaw would then hide the skill although the MCP server runs it.
    meta = {"generated-by": MARKER, "tool": text(tool.tool), "host": text(tool.site, SITE_MAX)}
    lines = ["---",
             f"name: {_scalar(name)}",
             f"description: {_scalar(description(tool))}",
             f"compatibility: {_scalar(compatibility(tool, profile))}",
             f"allowed-tools: {_scalar(allowed_tool(tool, profile))}",
             f"metadata: {json.dumps(meta, ensure_ascii=False)}",
             "---", ""]
    return "\n".join(lines + _body(tool, profile)) + "\n"


def parse_frontmatter(content: str) -> Optional[Tuple[Dict[str, Any], str]]:
    """``(frontmatter, body)`` when ``content`` is in the subset this module writes (one ``key: <JSON string or
    object>`` per line between ``---`` lines), else None."""
    lines = content.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return None
    end = lines.index("---", 1)
    front: Dict[str, Any] = {}
    for line in lines[1:end]:
        m = _LINE.fullmatch(line)
        if not m or m.group(1) in front:
            return None
        try:
            value = json.loads(m.group(2))
        except ValueError:
            return None
        if not isinstance(value, (str, dict)):
            return None
        front[m.group(1)] = value
    return front, "\n".join(lines[end + 1:])


def problems(content: str, folder: str) -> List[str]:
    """What breaks the format rules of the four agents; empty when ``content`` is a valid skill in ``folder``."""
    parsed = parse_frontmatter(content)
    if parsed is None:
        return ["the frontmatter is not one `key: <JSON value>` per line between `---` lines"]
    front, body = parsed
    out = []
    name, desc = front.get("name"), front.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > NAME_MAX:
        out.append(f"name {name!r} is not lower-case letters, digits and single hyphens, at most {NAME_MAX}")
    elif name != folder:
        out.append(f"name {name!r} is not the folder name {folder!r}")
    if not isinstance(desc, str) or not desc.strip() or len(desc) > DESCRIPTION_MAX:
        out.append(f"description is empty or longer than {DESCRIPTION_MAX} characters")
    extra = sorted(set(front) - set(KEYS))
    if extra:
        out.append(f"unknown frontmatter keys {extra}")
    compat = front.get("compatibility", "")
    if not isinstance(compat, str) or len(compat) > COMPATIBILITY_MAX:
        out.append(f"compatibility is not a string of at most {COMPATIBILITY_MAX} characters")
    if "metadata" in front and not isinstance(front["metadata"], dict):
        out.append("metadata is not a map")
    if not body.strip():
        out.append("the body is empty")
    if any(line.startswith("---") for line in body.split("\n")):
        out.append("a body line starts with ---")
    return out


# ---------------------------------------------------------------------------
# the export
# ---------------------------------------------------------------------------

def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read(_READ_MAX)
    except (OSError, UnicodeError):
        return None


def owner(folder: str) -> Optional[Tuple[str, str]]:
    """``(tool, host)`` of a folder this command wrote; None for any other folder (a link, no SKILL.md, a
    SKILL.md without our marker)."""
    path = os.path.join(folder, SKILL_FILE)
    if os.path.islink(folder) or not os.path.isdir(folder) or os.path.islink(path) or not os.path.isfile(path):
        return None
    parsed = parse_frontmatter(_read(path) or "")
    meta = parsed[0].get("metadata") if parsed else None
    if not isinstance(meta, dict) or meta.get("generated-by") != MARKER:
        return None
    return str(meta.get("tool") or ""), str(meta.get("host") or "")


def _existing(directory: str) -> Dict[str, Optional[Tuple[str, str]]]:
    """Every entry of ``directory`` a skill name could take, with the tool it is ours for (None: not ours)."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return {}
    return {entry: owner(os.path.join(directory, entry)) for entry in entries if entry.startswith(PREFIX)}


def assign(tools: Sequence[Tool], existing: Dict[str, Optional[Tuple[str, str]]]) -> List[str]:
    """A folder name per tool, in order. A tool keeps the folder it was written to before, also when its host
    or app changed since (no current tool has the old one); the others take the first of ``anyuse-<tool>``,
    ``-2``, ``-3`` that no other tool takes and that is not ours for another tool. A name held by a folder that
    is not ours is assigned anyway: the writer leaves that folder alone."""
    def numbered_from(folder: str, base: str) -> bool:
        m = re.fullmatch(r".*-([0-9]+)", folder)
        return folder == base or bool(m and int(m.group(1)) >= 2 and numbered(base, int(m.group(1))) == folder)

    current = {tool.identity for tool in tools}
    taken: set = set()
    out: List[Optional[str]] = [None] * len(tools)
    for same_site in (True, False):
        for i, tool in enumerate(tools):
            if out[i] is not None:
                continue
            base = skill_name(tool.tool)
            mine = sorted(f for f, who in existing.items()
                          if who is not None and f not in taken and numbered_from(f, base)
                          and (who == tool.identity if same_site
                               else who[0] == tool.identity[0] and who not in current))
            if mine:
                out[i] = mine[0]
                taken.add(mine[0])
    for i, tool in enumerate(tools):
        if out[i] is not None:
            continue
        base, n = skill_name(tool.tool), 1
        while True:
            name = base if n == 1 else numbered(base, n)
            if name not in taken and existing.get(name) in (None, tool.identity):
                break
            n += 1
        out[i] = name
        taken.add(name)
    return [name or "" for name in out]


def _write(folder: str, content: str) -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, SKILL_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".SKILL.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def export_skills(directory: str, tools: Iterable[str] = (), force: bool = False, storage=None,
                  hosts_path: Optional[str] = None, profile: Optional[str] = None) -> Dict[str, Any]:
    """Write ``directory/anyuse-<tool>/SKILL.md`` for each exportable tool (only those named in ``tools`` when
    given), for the MCP ``profile`` (:func:`resolve_profile`). ``{ok, dir, profile, written, skipped, reason}``:
    ``reason`` is None unless the folder is unusable, a skill could not be written, or a named tool was neither
    written nor already up to date. A skill written for the other profile differs, so it is rewritten."""
    directory = os.path.abspath(os.path.expanduser(str(directory)))
    wanted = list(dict.fromkeys(str(t) for t in tools))
    profile = resolve_profile(profile)
    report: Dict[str, Any] = {"ok": False, "dir": directory, "profile": profile, "written": [], "skipped": [],
                              "reason": None}
    if os.path.lexists(directory) and not os.path.isdir(directory):
        report["reason"] = f"{directory} exists and is not a folder. Name a folder for the skills."
        return report
    found, skipped = collect(storage, hosts_path)
    # DIR itself is created with the first skill written into it
    existing = _existing(directory)
    names = assign(found, existing)
    up_to_date = set()
    failed: List[str] = []
    for tool, name in zip(found, names):
        if wanted and tool.tool not in wanted:
            continue
        folder = os.path.join(directory, name)
        path = os.path.join(folder, SKILL_FILE)
        where = {"skill": name, "path": path}
        content = render(tool, name, profile)
        bad = problems(content, name)
        if bad:
            failed.append(tool.tool)
            skipped.append({**_skip(tool.tool, tool.site, f"the skill would not be valid: {bad[0]}.", tool.kind),
                            **where})
            continue
        if os.path.lexists(folder):
            # assign() hands out only folders that are free, foreign (None) or ours for this tool
            if existing.get(name) is None:
                skipped.append({**_skip(tool.tool, tool.site, (
                    f"{folder} exists and was not written by anyuse. It is left alone; rename or remove it "
                    f"to export this tool."), tool.kind), **where})
                continue
            if not force and _read(path) == content:
                up_to_date.add(tool.tool)
                skipped.append({**_skip(tool.tool, tool.site, f"{path} is up to date. --force writes it again.",
                                        tool.kind), **where})
                continue
        try:
            _write(folder, content)
        except OSError as exc:
            failed.append(tool.tool)
            skipped.append({**_skip(tool.tool, tool.site, f"could not write {path}: {exc.strerror or exc}.",
                                    tool.kind), **where})
            continue
        report["written"].append({"skill": name, "tool": tool.tool, "host": tool.site, "kind": tool.kind,
                                  "mcp_tool": tool.mcp_name, "allowed_tools": allowed_tool(tool, profile),
                                  "path": path})
    if wanted:
        skipped = [s for s in skipped if s["tool"] in wanted]
        known = {t.tool for t in found} | {s["tool"] for s in skipped}
        skipped += [_skip(name, "", UNKNOWN_TOOL) for name in wanted if name not in known]
    report["skipped"] = sorted(skipped, key=lambda s: (s["tool"].lower(), s["host"].lower(), s["tool"]))
    done = {w["tool"] for w in report["written"]} | up_to_date
    missed = [name for name in wanted if name not in done]
    if missed:
        report["reason"] = (f"not exported: {', '.join(text(n, 80) for n in missed)}. "
                            f"Each skipped entry says why.")
    elif failed:
        report["reason"] = (f"could not write {len(failed)} skill{'s' if len(failed) > 1 else ''} in "
                            f"{directory}. Each skipped entry says why.")
    report["ok"] = not (missed or failed)
    return report


__all__ = ["skill_name", "numbered", "text", "Tool", "collect", "description", "render", "parse_frontmatter",
           "problems", "owner", "assign", "export_skills", "resolve_profile", "allowed_tool", "SKILL_FILE",
           "PREFIX", "MARKER", "MCP_SERVER", "PROFILE_DEFAULT", "PROFILE_META", "PROFILES"]
