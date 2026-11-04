"""MCP surface for COMPILED workflows — "be the hands" for an external agent.

Two tools, deliberately the smallest useful surface:

* ``list_compiled_workflows`` — what has been compiled, what parameters it
  takes, whether it is proved, how risky it is, and whether its host has been
  acknowledged. Enough for an agent to decide whether to call it.
* ``run_compiled_workflow`` — run a READ-ONLY compiled workflow through the
  normal ladder and return structured JSON with provenance.

Three properties this surface keeps, and the reasons:

1. **It never drives the desktop.** The api rung is executed through the real
   ``deterministic.replay_transitions`` fall-through, but the executor handed to
   it has no actuator. If the api rung fails, the result says the fall-back is
   the recorded UI path and that this surface will not drive it — rather than
   silently clicking on somebody's screen because a remote agent asked.
2. **The EXISTING HIL gate decides.** Risk is ``hil_gate.classify_step`` applied
   to the api action, exactly as the executor does it. Anything that is not
   ``read_only`` is refused here with the classification in the response; it does
   not become "runnable over MCP" by virtue of being compiled.
3. **Provenance in every result**: which rung served it, wall seconds, the proof
   id the template was persisted under, the URLs touched, and the host.

Everything is injectable (``storage``, ``executor``, ``hosts_path``) so the tests
run with a fake transport and a temp directory and touch nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from anyuse.mcpserver.tool_names import (
    joined as joined_tool_name, lookup as lookup_tool, piece as tool_piece, suffixed as suffixed_tool_name,
    unique as unique_tool_names,
)

logger = logging.getLogger(__name__)

#: Only this risk class may be run through the MCP surface without a human.
RUNNABLE_CLASS = "read_only"

#: The ladder tier a compiled record corresponds to. An agent deciding whether
#: to call a workflow needs to know which one it will get, because the tiers
#: differ by two orders of magnitude in latency and by the whole question of
#: whether a model is involved.
TIER_REQUEST = "1_proved_request"
TIER_REPLAY = "2_deterministic_replay"
TIER_AGENT = "3_agent"
TIER_BLOCKED = "4_blocked_needs_human"

TIER_MEANING = {
    TIER_REQUEST: ("the proved HTTP request (a chained or page-keyed tool sends two or "
                   "more, such as the site's page and then the search); milliseconds; "
                   "zero model calls unless a configured check model is asked where the "
                   "site's own evidence leaves a check open (counted in model_calls); the "
                   "extraction was checked against what the UI showed for inputs that "
                   "were not in the recording"),
    TIER_REPLAY: ("the recorded navigation replayed in a headless browser; seconds; "
                  "zero model calls unless a configured check model is asked about a "
                  "short or empty answer (counted in model_calls); opt-in and "
                  "container-only"),
    TIER_AGENT: ("no deterministic rung proved for this workflow; an agent has "
                 "to drive it, which costs model calls and money and has no "
                 "guaranteed success rate"),
    TIER_BLOCKED: ("the site asked for a human (challenge, CAPTCHA, 403 or a "
                   "login wall). Nothing automatic helps and nothing here will "
                   "try to get around it"),
}

TOOL_SET_VERSION = 1

#: A record ``anyuse learn`` saved from a browser-replay proof: no compiled request, the
#: browser replay is its first rung (``learn_loop.replay_tool_record``)
KIND_BROWSER = "browser_replay"
BROWSER_TOOL_NOTE = ("served by the browser replay (no compiled request); each call "
                     "needs a browser (ANYUSE_CDP_ENDPOINT or ANYUSE_LEARN_ENDPOINT)")


def replay_tool(record: Dict[str, Any]) -> bool:
    """Is this a tool the browser replay serves, with no compiled request behind it?"""
    return (record or {}).get("kind") == KIND_BROWSER


_TOOL_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_tool_piece(value: str) -> str:
    """Sanitize persisted/page-derived labels before they enter MCP metadata."""
    clean = _TOOL_SAFE.sub("_", str(value or "")).strip("._-")
    return clean[:80] or "workflow"


def round_ms(value: Any) -> Any:
    """A latency in milliseconds to one decimal (a None or non-number passes through)."""
    return round(float(value), 1) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def tool_host_piece(host: str) -> str:
    """The host part of a typed tool name: lower case, no port, dots as underscores (``crates_io``)."""
    return tool_piece(str(host or "").split(":", 1)[0].lower(), "site")


def typed_tool_name(host: str, workflow: str) -> str:
    """Stable ``<host>__<workflow>`` name containing no instruction text (``crates_io__search_crates``;
    mcpserver/tool_names.py)."""
    return joined_tool_name(tool_host_piece(host), workflow)


def typed_tool_key(host: str, workflow: str) -> str:
    """What a shared name's suffix is hashed from: the record's own host and workflow."""
    return f"{host}\0{workflow}"


def legacy_typed_tool_name(host: str, workflow: str) -> str:
    """The older dotted ``<host>.<workflow>`` name; still callable as an alias."""
    host_piece = _safe_tool_piece(str(host or "").split(":", 1)[0].lower())
    return f"{host_piece}.{_safe_tool_piece(workflow)}"


def _legacy_field_schema(template, path: str) -> Dict[str, Any]:
    """Typed fallback for records compiled before proof schemas were stored."""
    if getattr(template, "extract_kind", "json") == "html" or "[*]" in str(path):
        return {"type": "array", "items": {
            "type": "string" if getattr(template, "extract_kind", "json") == "html"
            else ["string", "number", "boolean"]}}
    return {"type": ["string", "number", "boolean", "array"]}


def output_schema(template, proof: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """JSON Schema derived from extraction names and proved observed types."""
    extract = dict(getattr(template, "extract", {}) or {})
    observed = []
    for case in (proof or {}).get("cases") or []:
        schema = ((case.get("contract") or {}).get("output_schema")
                  if isinstance(case, dict) else None)
        if isinstance(schema, dict):
            observed.append(schema)
    result = None
    if observed:
        from anyuse.execution.workflow_schema import merge_json_schemas
        result = merge_json_schemas(observed)
    if not isinstance(result, dict) or result.get("type") != "object":
        result = {"type": "object",
                  "properties": {name: _legacy_field_schema(template, path)
                                 for name, path in extract.items()},
                  "required": list(extract), "additionalProperties": False}
    return {"type": "object", "properties": {
        "ok": {"type": "boolean"}, "result": result,
        "provenance": {"type": "object"},
    }, "required": ["ok"], "additionalProperties": True}


def output_signature(template) -> str:
    """Compact named result shape for ``<tool name>(inputs) -> shape``."""
    if getattr(template, "shape", "list") == "list":
        return "{items[]}"
    names = ", ".join((getattr(template, "extract", {}) or {}).keys())
    return "{" + names + "}"


def _field_result_problem(value, template, proof=None):
    from anyuse.execution.api_step import check_extracted_fields
    valid, reason = check_extracted_fields(template, value, output_schema(template, proof)["properties"]["result"])
    return "" if valid else reason


def _terminal_rung_result(result):
    """An authorization stop or uncertain effect cannot authorize another rung.

    An incomplete read-only extraction may still use the documented cascade.
    """
    from anyuse.execution.outcome import classify_outcome
    return (classify_outcome(result) in {"terminal_refusal", "refused", "uncertain_completion", "deviation"}
            or any(result.get(key) for key in ("blocked_state", "blocked", "auth_required", "requires_human")))


def host_tool_sets(storage=None, hosts_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Versioned typed tools generated only from proved, approved workflows.

    Discovery remains broader through :func:`list_compiled`; this executable
    view deliberately omits every unapproved or stale-proof record.
    """
    from anyuse.execution.workflow_schema import json_schema_for_inputs, specs_from_dicts

    groups: Dict[str, List[Dict[str, Any]]] = {}
    rows = []
    for row in list_compiled(storage, hosts_path):
        if not row.get("runnable_without_human"):
            continue
        record = _find_record(row["workflow"], storage)
        if record is not None:
            rows.append((row, record))
    # one naming pass over every host: crates.io and crates.io:8443 share the host part crates_io
    keys = [typed_tool_key(row["host"], row["workflow"]) for row, _ in rows]
    bases = [typed_tool_name(row["host"], row["workflow"]) for row, _ in rows]
    names = unique_tool_names(list(zip(bases, keys)))
    legacy_used: Dict[str, set] = {}
    for (row, record), base, key, name in zip(rows, bases, keys, names):
        template = _template_of(record)
        input_schema = json_schema_for_inputs(specs_from_dicts(
            getattr(template, "parameter_schema", []) or []))
        # the dotted name this tool was listed under before (a second one on the same host: .<proof id>)
        legacy = legacy_typed_tool_name(row["host"], row["workflow"])
        if legacy in legacy_used.setdefault(row["host"], set()):
            legacy = f"{legacy}.{row['proof']['id']}"
        legacy_used[row["host"]].add(legacy)
        own = suffixed_tool_name(base, key)
        tool = {
            "name": name,
            "aliases": [legacy] + ([own] if own != name else []),
            "workflow": row["workflow"],
            "signature": (f"{name}({', '.join(row['parameters'])}) -> "
                          f"{output_signature(template)}"),
            # This sentence is generated from trusted enums/counts only. Page
            # text and template.notes never enter a tool instruction surface.
            "description": (f"Run an approved AnyUse workflow for this host; "
                            f"tier {row['ladder_tier']}; status {row['proof']['status']}; "
                            + (f"{BROWSER_TOOL_NOTE}; " if row.get("needs_browser") else "")
                            + (f"served by the {row['available_via'].replace('_', ' ')} while the "
                               f"compiled request is out of service; "
                               if row.get("degraded") else "")
                            + ("out of service; the next call checks whether the compiled request "
                               "answers again; "
                               if not row["health"]["preferred"] and row.get("available_via") == RUNG_API
                               and not row.get("needs_browser") else "") +
                            f"proof {row['proof']['id']}; "
                            f"schema v{record.get('version', 0)}. "
                            f"{proof_scope(template, record.get('proof') or {}, record)}"),
            "input_schema": input_schema,
            "output_schema": output_schema(template, record.get("proof") or {}),
            "proof": {
                "id": row["proof"]["id"], "summary": row["proof"]["what_was_checked"],
                "template_digest": row["proof"]["template_digest"],
                "status": row["proof"]["status"],
            },
            "tier": row["ladder_tier"],
            "latency_ms": round_ms(row["latency_ms_median_at_compile_time"]),
            "robots": row["robots"],
            "pins": {"compiled_schema_version": record.get("version"),
                     "tool_set_version": TOOL_SET_VERSION},
        }
        if name != base:
            tool["shares"] = base
        groups.setdefault(row["host"], []).append(tool)
    return [{"version": TOOL_SET_VERSION, "host": host,
             "tools": sorted(tools, key=lambda item: item["name"])}
            for host, tools in sorted(groups.items())]


def proof_scope(template, proof: Dict[str, Any], record: Optional[Dict[str, Any]] = None
                ) -> str:
    """Exact, bounded claim safe to place in an MCP tool description.

    Rebuilt from the proof's counts and enums (``proof_split.scope_text``), never
    copied from a stored string, so page text cannot reach this surface.
    """
    from anyuse.execution.proof_split import scope_text
    proof = dict(proof or {})
    if record is not None:
        proof["status"] = proof_status_of(record)
    fields = list((getattr(template, "extract", {}) or {}).keys())
    return scope_text(proof, getattr(template, "shape", "list"), fields)


def executable_typed_tools(storage=None, hosts_path: Optional[str] = None
                           ) -> List[Dict[str, Any]]:
    return [tool for group in host_tool_sets(storage, hosts_path)
            for tool in group["tools"]]


def run_typed_tool(tool_name: str, arguments: Optional[Dict[str, Any]] = None, *,
                   storage=None, hosts_path: Optional[str] = None, executor=None,
                   require_proved: bool = False) -> Optional[Dict[str, Any]]:
    tool = lookup_tool(executable_typed_tools(storage, hosts_path), tool_name)
    if tool is None:
        return None
    return run_compiled(tool["workflow"], arguments or {}, storage=storage,
                        hosts_path=hosts_path, executor=executor,
                        require_proved=require_proved)


def ladder_tier(record: Dict[str, Any], template) -> str:
    """Which rung of the ladder this record represents: the request while it is in service, else the
    saved browser replay when the record has one, else the agent."""
    from anyuse.execution.api_recompile import ApiHealth
    proof = record.get("proof") or {}
    health = ApiHealth.from_dict(record.get("health"))
    if not proof.get("passed"):
        return TIER_AGENT
    if replay_tool(record):
        return TIER_REPLAY if (record.get("browser") or {}).get("template") else TIER_AGENT
    if not health.preferred:
        return TIER_REPLAY if (record.get("browser") or {}).get("template") else TIER_AGENT
    return TIER_REQUEST


def rung_tier(rung: str) -> str:
    """The tier label of the rung that actually served an answer."""
    return {RUNG_API: TIER_REQUEST, RUNG_BROWSER: TIER_REPLAY}.get(rung, TIER_AGENT)


def lower_rung_available(record: Dict[str, Any], name: str = "") -> Optional[str]:
    """Which lower rung could serve this record right now (``browser_replay`` / ``agent``), or None:
    the browser rung needs its saved replay and a configured browser; the agent rung needs a task, a
    configured model and a browser for its page."""
    endpoint = _browser_endpoint()
    if not endpoint:
        return None
    if (record.get("browser") or {}).get("template"):
        return RUNG_BROWSER
    if _task_of(record, name) and _agent_policy():
        return RUNG_AGENT
    return None


def _storage(storage=None):
    if storage is not None:
        return storage
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage()


def proof_id(record: Dict[str, Any]) -> str:
    """A stable short id for "the template and the evidence it was kept on".

    Computed from the template plus the proof, so it changes whenever either
    changes — a caller can quote it and mean something specific.
    """
    blob = json.dumps({"t": record.get("template"), "p": record.get("proof")},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _template_of(record: Dict[str, Any]):
    from anyuse.execution.api_step import load_command
    return load_command(record.get("template") or {})


def risk_class(template, params: Optional[Dict[str, Any]] = None) -> str:
    """Classify the complete bound rung with the existing gate."""
    from anyuse.execution.api_step import api_hil_action
    from anyuse.execution.hil_gate import classify_step
    return classify_step(api_hil_action(template, params))


def _urls(template) -> List[str]:
    steps = getattr(template, "steps", None)
    if steps:
        return [s.template.url for s in steps]
    return [template.url]


def describe_compiled(name: str, record: Dict[str, Any],
                      hosts_path: Optional[str] = None) -> Dict[str, Any]:
    """One row of ``list_compiled_workflows``."""
    from anyuse.execution.api_permission import host_acknowledged
    from anyuse.execution.api_recompile import ApiHealth, probe_due

    from anyuse.execution.api_compile import proof_matches_template
    template = _template_of(record)
    urls = _urls(template)
    host = urllib.parse.urlsplit(urls[0]).netloc if urls else ""
    hosts = list(dict.fromkeys(urllib.parse.urlsplit(url).netloc for url in urls if url))
    hosts_ok = all(host_acknowledged(item, hosts_path) for item in hosts)
    proof = record.get("proof") or {}
    health = ApiHealth.from_dict(record.get("health"))
    cls = risk_class(template)
    status = proof_status_of(record)
    tier = ladder_tier(record, template)
    tier_means = TIER_MEANING.get(tier, "")
    replay_only = replay_tool(record)
    available_via = (lower_rung_available(record, name) if replay_only or not health.preferred
                     else RUNG_API)
    if available_via is None and not replay_only and probe_due(health):
        # out of service with nothing below it: the next call sends the request once more
        available_via = RUNG_API
    if status != "proved" and proof.get("passed"):
        tier_means += (f" — NOTE: status {status!r}: the selection was not held out "
                       f"from the validation inputs, so this is not proved")
    return {
        "workflow": name,
        "kind": (KIND_BROWSER if replay_only
                 else "chain" if getattr(template, "steps", None) else "request"),
        "parameters": list(getattr(template, "input_params", []) or []),
        "parameter_schema": [row for row in getattr(template, "parameter_schema", [])
                             if row.get("kind") == "input"],
        "result_shape": getattr(template, "shape", "list"),
        "urls": urls,
        "host": host,
        "hosts": hosts,
        "host_acknowledged": hosts_ok,
        "hosts_acknowledged": hosts_ok,
        "risk_class": cls,
        # out of service but still answering through a lower rung counts as runnable: an agent that
        # trusts this flag must reach the cascade, not stop at the listing
        "runnable_without_human": (cls == RUNNABLE_CLASS and available_via is not None and hosts_ok
                                   and bool(proof.get("passed"))
                                   and proof_matches_template(record)),
        "available_via": available_via,
        **({"degraded": (f"the compiled request is out of service ({_out_of_service_reason(health)}); "
                         f"calls are served by the {available_via.replace('_', ' ')}")}
           if available_via and available_via != RUNG_API and not replay_only else {}),
        **({"needs_browser": True, "note": BROWSER_TOOL_NOTE} if replay_only else {}),
        "proof": {
            "id": proof_id(record),
            "passed": bool(proof.get("passed")),
            "status": status,
            "selection_held_out": proof.get("selection_held_out"),
            "split": dict(proof.get("split") or {}),
            "scope": proof_scope(template, proof, record),
            "fresh_cases_passed": proof.get("passed_count"),
            "fresh_cases_checked": proof.get("case_count"),
            "what_was_checked": proof.get("reason", ""),
            "template_digest": proof.get("template_digest", ""),
            "current": proof_matches_template(record),
        },
        "health": _health_view(record, health),
        # the last automatic recompile (execution/auto_recompile.py) and the last one that swapped
        "recompile": _recompile_view(record, health),
        "ladder_tier": tier,
        "ladder_tier_means": tier_means,
        "latency_ms_median_at_compile_time": proof.get("elapsed_ms_median"),
        # The page-chrome filter decides what "the result list" was taken to
        # mean. An agent trusting this workflow is trusting that decision, so
        # the labels it removed travel with the listing rather than a count.
        "chrome_filtered": list(record.get("chrome_filtered") or []),
        "robots": {
            "policy": (record.get("permission") or {}).get("robots_policy", ""),
            "allowed": (record.get("permission") or {}).get("robots_allowed"),
            "note": (record.get("permission") or {}).get("robots_note", ""),
            "overridden": (record.get("permission") or {}).get("robots_overridden", ""),
        },
        # the request in use: a recompile replaced the first one
        "compiled_at": record.get("recompiled_at") or record.get("compiled_at", ""),
        "source_recording": (record.get("source") or {}).get("recording", ""),
        # Was this rung recorded by a PERSON or learned by the step
        # agent (`anyuse learn`)? The proof standard is identical either way —
        # that is the whole design — but an agent deciding whether to depend on
        # a rung should be told which, and what the learning cost, rather than
        # having to infer it.
        "origin": (record.get("source") or {}).get("origin", "recording"),
        "learned_from": (record.get("source") or {}).get("learned_from") or None,
        # A host that serves a browser and refuses our named HTTP
        # client. Tier 1 is never attempted there; tier 2 is. AnyUse does not
        # change its User-Agent —
        "browser_only": bool(_browser_only(host)),
    }


def _health_view(record: Dict[str, Any], health) -> Dict[str, Any]:
    """The listing's ``health``: how the compiled request is doing, when a request out of service is
    sent again and why it was taken out (``outage``: the site did not answer; ``rate_limit``: it
    asked AnyUse to wait; ``gone``: it kept failing while the site's own pages answered;
    ``answers``), when the site did not answer the last calls (since ``first_outage_at``; the
    site's page last read at ``last_site_check``), and the last time it went back in service."""
    from anyuse.execution.api_recompile import demotion_cause, next_probe_at, outage_wait
    view = {
        "preferred": health.preferred,
        "consecutive_failures": health.consecutive_failures,
        "total_runs": health.total_runs,
        "demoted_reason": health.demoted_reason,
        "recompile_attempts": health.recompile_attempts,
        "last_recompile_at": health.last_recompile_at,
        "consecutive_outages": health.consecutive_outages,
    }
    if not health.preferred:
        view["out_of_service_cause"] = demotion_cause(health)
        view["next_check_after"] = next_probe_at(health)
    wait = outage_wait(health)
    if wait:
        view["retry_after"] = wait
    for key in ("first_outage_at", "last_site_check"):
        if getattr(health, key, ""):
            view[key] = getattr(health, key)
    if isinstance(record.get("restored"), dict):
        view["restored"] = {k: record["restored"].get(k) for k in ("at", "by", "why")}
    return view


def _recompile_view(record: Dict[str, Any], health) -> Optional[Dict[str, Any]]:
    """The last automatic recompile attempt and the last swap, or None when there was neither."""
    from anyuse.execution.api_recompile import next_attempt_after
    last = record.get("recompile") if isinstance(record.get("recompile"), dict) else None
    swap = record.get("recompiled") if isinstance(record.get("recompiled"), dict) else None
    if last is None and swap is None:
        return None
    out: Dict[str, Any] = {}
    if last is not None:
        out["last_attempt"] = {k: last.get(k) for k in ("at", "swapped", "from", "reason", "attempt")}
    if swap is not None:
        out["recompiled"] = {k: swap.get(k) for k in ("at", "from", "status", "previous_record")}
    if not health.preferred:
        out["next_attempt_after"] = next_attempt_after(health)
    return out


def proof_status_of(record: Dict[str, Any]) -> str:
    """``proved`` / ``checked (selection not held out)`` / ``not proved``.

    Records written before the status existed carry no ``status``. A passed
    proof on those keeps its old meaning, with one exception: a record the
    learn loop wrote (``source.origin == "agent"``) before the shared split
    derived its ground truth across every page, validation pages included, so
    it is ``checked (selection not held out)``.
    """
    from anyuse.execution.proof_split import CHECKED, NOT_PROVED, PROVED
    proof = record.get("proof") or {}
    if not proof.get("passed"):
        return NOT_PROVED
    if proof.get("status"):
        return str(proof["status"])
    if (record.get("source") or {}).get("origin") == "agent" and "split" not in proof:
        return CHECKED
    return PROVED


def _browser_only(host: str) -> bool:
    try:
        from anyuse.execution.blocked import is_browser_only
        return bool(is_browser_only(host))
    except Exception:                        # a registry that cannot be read is
        return False                         # not a reason to fail a listing


def list_compiled(storage=None, hosts_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every workflow with a persisted compile record."""
    from anyuse.execution.api_compile import COMPILED_FILE

    storage = _storage(storage)
    base = str(getattr(storage, "base_dir", "") or os.path.dirname(
        str(storage.get_workflow_dir("x"))))
    out: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(base, "workflow_*", COMPILED_FILE))):
        try:
            with open(path) as fh:
                record = json.load(fh)
            from anyuse.execution.api_compile import migrate_compiled_record
            record = migrate_compiled_record(record)
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable compile record %s: %s", path, exc)
            continue
        name = record.get("workflow") or os.path.basename(os.path.dirname(path))[9:]
        try:
            out.append(describe_compiled(name, record, hosts_path))
        except Exception as exc:
            logger.warning("skipping malformed compile record %s: %s", path, exc)
    return out


def find_compiled(name: str, storage=None) -> Optional[Tuple[str, Dict[str, Any]]]:
    """``(path of its compiled_api.json, record)`` for the compiled workflow the listings call ``name``,
    or None."""
    from anyuse.execution.api_compile import COMPILED_FILE

    storage = _storage(storage)
    base = str(getattr(storage, "base_dir", "") or os.path.dirname(
        str(storage.get_workflow_dir("x"))))
    for path in sorted(glob.glob(os.path.join(base, "workflow_*", COMPILED_FILE))):
        try:
            with open(path) as fh:
                record = json.load(fh)
            from anyuse.execution.api_compile import migrate_compiled_record
            record = migrate_compiled_record(record)
        except (OSError, ValueError):
            continue
        if (record.get("workflow") or os.path.basename(os.path.dirname(path))[9:]) == name:
            return path, record
    return None


def _find_record(name: str, storage=None) -> Optional[Dict[str, Any]]:
    found = find_compiled(name, storage)
    return found[1] if found else None


class _NoActuatorExecutor:
    """The ladder's executor seam with the api rung wired and NOTHING else.

    Any non-api modality returns a refusal instead of actuating. That is the
    whole point: an external agent may make AnyUse replay a proved HTTP request,
    it may not make it move the mouse.
    """

    def __init__(self, api_executor):
        self._api = api_executor
        self.attempted: List[str] = []
        #: replay_transitions keeps only {modality, success, error} per rung, so
        #: the full api result (extracted values, status, bytes) is kept here.
        self.last_api_result: Dict[str, Any] = {}

    async def _execute_step(self, step):
        from anyuse.execution.api_step import load_command

        self.attempted.append(step.modality)
        if step.modality != "api":
            return {"success": False, "method": step.modality,
                    "error": ("fall-back to the recorded UI path is not available over "
                              "MCP: this surface never drives the desktop")}
        cmd = dict(step.command or {})
        params = cmd.pop("bound_params", None) or {}
        self.last_api_result = self._api.execute(load_command(cmd), params)
        return self.last_api_result


def run_compiled(name: str, params: Optional[Dict[str, Any]] = None,
                 storage=None, executor=None, hosts_path: Optional[str] = None,
                 record_outcome: bool = True, require_proved: bool = False,
                 post_card: bool = True, browser_executor=None,
                 agent_runner=None, cascade: bool = True,
                 record: Optional[Dict[str, Any]] = None, *,
                 _healed: Optional[Dict[str, Any]] = None,
                 _lower_tried: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run a compiled workflow's api rung through the normal ladder.

    Refuses, with a reason, when: there is no compiled record; the proof did not
    pass; ``require_proved`` is set (the default MCP profile) and the proof
    status is not ``proved`` (e.g. ``checked (selection not held out)``,
    docs/ATTACH.md); the rung has been demoted and its check is not due; the host is not
    acknowledged; or the existing HIL classification is anything other than ``read_only``.

    A call the site did not answer (a server error, a timeout, a refused connection, a rate
    limit) is not counted against the request; after the second in a row, or with the site's
    Retry-After, the request waits before it is sent again. A demoted request is sent once on
    the first call after its check is due, and goes back in service when that answer passes its
    checks (execution/api_recompile.py). A check the site blocks is answered like any blocked
    call (``blocked`` and an inbox card); a check refused before it was sent is not counted.

    ``record``: run this compile record (a pinned copy, ``anyuse watch``) instead of
    the stored one, with the same checks. Nothing is written back for it.

    When the compiled request fails on an answer from the site and no lower rung
    answers, the call heals the tool from plain reads of its result page
    (execution/step_heal.py) and, when a new request is swapped in, runs once more with
    it; ``_healed`` is that heal's summary, and that run does not heal again.
    ``_lower_tried``: the lower rungs the call before the heal tried; that run does not
    try them again and lists them as tried before the heal.
    """
    from anyuse.execution.api_permission import host_acknowledged
    from anyuse.execution.api_recompile import (ApiHealth, demote, demotion_cause,
                                                gone_check_due, next_probe_at, outage_wait,
                                                record_outage, record_outcome as fold_outcome)
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.deterministic import replay_transitions
    from anyuse.guides.state_graph import TransitionImpl, UITransition

    t0 = time.time()
    if record is not None:
        # a pinned copy: its outcome must never overwrite the stored record
        record_outcome = False
    else:
        record = _find_record(name, storage)
    if record is None:
        return {"ok": False, "workflow": name, "error": "no compiled workflow with that name",
                "hint": "call list_compiled_workflows first"}

    template = _template_of(record)
    info = describe_compiled(name, record, hosts_path)
    base = {"ok": False, "workflow": name, "provenance": {
        "proof_id": info["proof"]["id"], "urls": info["urls"], "host": info["host"],
        "kind": info["kind"], "compiled_at": info["compiled_at"]},
        # a web tool compiled from a HAR, attach or learn has no recorded desktop path to fall back to
        "has_recorded_path": _has_recorded_path(name, storage)}

    if not info["proof"]["passed"]:
        return {**base, "error": "this workflow's template never passed the fresh-input proof",
                "proof": info["proof"]}
    if require_proved and info["proof"].get("status") != "proved":
        return {**base, "error": (f"proof status is {info['proof'].get('status')!r}, not "
                                  "'proved'; this record runs only under the explicit "
                                  "full MCP profile"),
                "proof": info["proof"]}
    if not info["proof"]["current"]:
        return {**base, "error": ("the compiled template changed after proof; "
                                  "re-prove it before execution"),
                "proof": info["proof"]}
    health = ApiHealth.from_dict(record.get("health"))
    api_demoted = not health.preferred
    replay_only = replay_tool(record)
    unacknowledged = [host for host in info.get("hosts", [info["host"]])
                      if not host_acknowledged(host, hosts_path)]
    if unacknowledged:
        return {**base, "error": (f"host(s) {unacknowledged!r} are not acknowledged; run "
                                  f"`anyuse workflow acknowledge-host <host>` for each"),
                "host_acknowledged": False, "unacknowledged_hosts": unacknowledged}

    # a name the tool does not take would fail the binding below and be classed as not read-only
    unknown = [p for p in (params or {}) if p not in info["parameters"]]
    if unknown:
        takes = ", ".join(info["parameters"]) or "no parameters"
        return {**base, "error": (f"unknown parameter{'s' if len(unknown) > 1 else ''} "
                                  f"{', '.join(repr(p) for p in unknown)} (this tool takes: {takes})"),
                "parameters": info["parameters"], "unknown_parameters": unknown}
    missing = [p for p in info["parameters"] if p not in (params or {})]
    if missing:
        return {**base, "error": f"missing parameter(s): {missing}",
                "parameters": info["parameters"]}

    # Parameters can change the classification (most importantly a bound
    # GraphQL document).  The unbound listing is informational; execution is
    # authorized only from the complete request immediately before dispatch.
    bound_risk = risk_class(template, params or {})
    if bound_risk != RUNNABLE_CLASS:
        return {**base, "error": (f"complete bound request classified {bound_risk!r} "
                                  f"by the HIL gate; only {RUNNABLE_CLASS!r} workflows "
                                  "run without a human here"),
                "risk_class": bound_risk}

    if getattr(template, "readonly_post", False):
        from anyuse.execution.api_step import readonly_post_enabled
        try:
            bound_url, _headers, bound_body = template.render(params or {})
        except (KeyError, ValueError) as exc:
            return {**base, "error": f"parameter binding failed: {exc}"}
        if not readonly_post_enabled(bound_url, bound_body, hosts_path):
            return {**base, "error": ("read-only POST requires "
                                      "ANYUSE_API_READONLY_POST=on and an exact "
                                      "endpoint/operation approval")}

    # The product replay path reads per-site modes through the one accessor
    # (identity/robots/login/route). The bench never constructs the executor
    # this way, so a measured bench always runs the safe identity.
    api_exec = executor or ApiStepExecutor(apply_site_modes=True)
    api_exec.readonly_post_hosts_path = hosts_path
    ladder = _NoActuatorExecutor(api_exec)
    tried: List[Dict[str, Any]] = []
    # an empty answer is judged against the site's own empty page; on a product call (not a
    # pinned watch copy, not without the fall-back) the check model may be asked too
    from anyuse.execution import empty_page as EP
    product = bool(cascade and record_outcome)
    req_ctx: Optional[EP.Context] = None
    # a request out of service is sent once more when its probe is due; a request in service
    # waits after calls the site did not answer (execution/api_recompile.py)
    probing, held_until = False, ""
    probe_calls = 0
    if api_demoted and not replay_only and record_outcome:
        probing, health = _claim_probe(name, storage, record, health)
        record["health"] = health.to_dict()
    elif not replay_only and record_outcome:
        held_until = outage_wait(health)
    if probing and browser_executor is None and cascade and _browser_endpoint() \
            and (record.get("browser") or {}).get("template"):
        # one executor for the probe's ground truth and the lower rungs: one pacing
        from anyuse.execution.cdp_replay import ReplayExecutor
        browser_executor = ReplayExecutor(endpoint=_browser_endpoint())
    if replay_only:
        result, step, served, ok, api_result = {}, {}, None, False, {}
        tried.append({"rung": RUNG_API, "ok": False, "skipped": True,
                      "why": "this tool has no compiled request; the browser replay serves it"})
    elif api_demoted and not probing:
        result, step, served, ok, api_result = {}, {}, None, False, {}
        when = next_probe_at(health)
        tried.append({"rung": RUNG_API, "ok": False, "skipped": True,
                      "why": (f"out of service: {_out_of_service_reason(health)}"
                              + (f"; sent again after {when}" if when else ""))[:400]})
    elif held_until:
        result, step, served, ok = {}, {}, None, False
        api_result = {"error": (f"the site did not answer the last calls, so the request is not sent "
                                f"again before {held_until}")}
        tried.append({"rung": RUNG_API, "ok": False, "skipped": True, "why": api_result["error"]})
    else:
        transition = UITransition(
            from_state=f"{name}_start", to_state=f"{name}_done", intent=name,
            implementations=[TransitionImpl(modality="api", command=record["template"])])
        req_ctx = EP.context_for(record, "request", record["template"], learn=record_outcome)
        _set_empty_context(api_exec, req_ctx)
        try:
            with _product_scope(product):
                result = asyncio.run(replay_transitions([transition], ladder, params or {}))
        finally:
            _set_empty_context(api_exec, None)
        _keep_empty_page(name, storage, record, "request", req_ctx, persist=record_outcome)
        step = (result.get("steps") or [{}])[0]
        served = step.get("modality_used")
        ok = bool(result.get("success"))
        api_result = ladder.last_api_result or {}
        tried.append({"rung": RUNG_API, "ok": ok, "ms": round_ms(api_result.get("elapsed_ms")),
                      **({} if ok else {"why": str(api_result.get("error") or step.get("reason")
                                                    or "the compiled request failed")[:200]})})
        if probing:
            tried[-1]["probe"] = True

    if ok:
        field_problem = _field_result_problem(api_result.get("extracted"), template, record.get("proof"))
        if field_problem:
            ok = False
            api_result = dict(api_result, success=False, outcome_kind="incomplete", error=field_problem)
            tried[-1].update(ok=False, why=field_problem)

    # Preserve terminal authorization/effect verdicts through the public report,
    # without dispatching additional requests, repair, or lower rungs on their behalf.
    api_terminal = not ok and _terminal_rung_result(api_result)

    probe: Optional[Dict[str, Any]] = None
    if probing:
        # Account for the probe already attempted, including a blocked answer.
        # A terminal answer is not ok, so it never dispatches _probe_holds.
        # the probe: the request goes back in service when its answer passed its checks and, for a
        # tool with a last good answer, that answer still holds; otherwise the next probe waits longer
        if ok:
            holds, why, checked = _probe_holds(record, template, params or {}, api_result, api_exec,
                                               name=name, storage=storage, hosts_path=hosts_path,
                                               browser_executor=browser_executor)
            probe_calls = int(checked.get("model_calls") or 0)
        else:
            holds, why = False, str(api_result.get("error") or step.get("reason")
                                    or "the compiled request failed")
        restored = _restore(name, storage, record, why) if holds else None
        if restored is not None:
            record, api_demoted = restored, False
            health = ApiHealth.from_dict(record.get("health"))
            info = describe_compiled(name, record, hosts_path)
            probe = {"sent": True, "restored": True, "why": why[:300],
                     **({"model_calls": probe_calls} if probe_calls else {})}
        else:
            if ok:
                if holds:
                    # the answer passed, but the stored request is no longer the one this call ran
                    why = "the tool's request was replaced while this call ran: call it again"
                ok, served = False, None
                api_result = {**api_result, "success": False,
                              "error": f"the compiled request answered, but {why}"[:400]}
                tried[-1].update({"ok": False, "why": api_result["error"][:200]})
            sent = not _sent_nothing(api_result)
            if sent:
                limited = _rate_limited(api_result)
                # the next probe waits at least as long as the site asked
                wait = api_result.get("retry_after_s")
                if wait is None:
                    wait = (api_result.get("blocked_state") or {}).get("retry_after_s")
                # a request taken out as gone that still gets a server error has not come back:
                # that is not the site failing to answer
                outage = (_transient(api_result) and not health.endpoint_removed) or limited
                health = _note_probe(name, storage, record, str(api_result.get("error") or why),
                                     outage, health, wait, limited)
                record["health"] = health.to_dict()
            # a probe refused before it left (a held host, the pre-send policy) asked the site
            # nothing, so it is not counted; claiming it already moved the next one a window ahead
            probe = {"sent": sent, "restored": False, "why": str(api_result.get("error") or why)[:300],
                     "next_check_after": next_probe_at(health)}

    # a chain whose search answered with no result of the asked-for name (api_step.pick_row): an answer, not a
    # changed site, so it is not counted against the request and no lower rung opens some other result instead
    not_found = not ok and api_result.get("reason_code") == "not_found"
    # the runtime cascade: a failure that is not a block falls to the browser replay, then the agent
    lower: Optional[Dict[str, Any]] = None
    lower_block: Optional[Dict[str, Any]] = None
    if replay_only:
        # the browser replay is this tool's own first rung; without the fall-back, no agent
        lower, lower_tried, lower_block = _lower_rungs(
            record, name, params or {}, browser_executor=browser_executor,
            agent_runner=agent_runner, agent=cascade, storage=storage,
            learn_empty=record_outcome, product=product)
        tried.extend(lower_tried)
    elif not cascade and not ok:
        tried.append({"rung": RUNG_BROWSER, "ok": False, "skipped": True, "why": "fall-back turned off"})
        tried.append({"rung": RUNG_AGENT, "ok": False, "skipped": True, "why": "fall-back turned off"})
    elif _lower_tried is not None:
        # the call before the heal already tried the lower rungs; none answered then
        tried.extend(dict(t, before_heal=True) for t in _lower_tried)
    elif not ok and not not_found and not api_terminal:
        if browser_executor is None and _browser_endpoint():
            # one executor for the serving replay and a recompile's replays: one pacing
            from anyuse.execution.cdp_replay import ReplayExecutor
            browser_executor = ReplayExecutor(endpoint=_browser_endpoint())
        lower, lower_tried, lower_block = _lower_rungs(
            record, name, params or {}, browser_executor=browser_executor, agent_runner=agent_runner,
            storage=storage, learn_empty=record_outcome, product=product)
        tried.extend(lower_tried)
    if replay_only and lower is None and lower_block is None:
        no_browser = not (browser_executor or _browser_endpoint())
        return {**base, "error": (f"{BROWSER_TOOL_NOTE}; " + (
                    "no browser is configured" if no_browser else
                    "; ".join(f"{t['rung'].replace('_', ' ')}: {t.get('why', '')}"
                              for t in tried if t["rung"] != RUNG_API)[:300])),
                "needs_browser": True, "no_browser": no_browser, "rungs_tried": tried,
                **_asked(_check_model_calls(req_ctx, tried) + probe_calls)}
    may_heal = (cascade and record_outcome and not replay_only and _healed is None and not api_terminal
                and lower is None and lower_block is None)
    # a probe the site blocked (a CAPTCHA, a sign-in wall, a rate limit, a held host) is answered
    # like any other call the site blocked: the block and its inbox card, below
    if api_demoted and lower is None and lower_block is None and not api_result.get("blocked_state"):
        cause = demotion_cause(health)
        # a heal reads the site again: not while the site does not answer or asked AnyUse to wait
        heal = (_auto_heal(name, storage, params or {}, api_exec, hosts_path,
                           health.demoted_reason, record)
                if may_heal and cause not in _COMES_BACK else None)
        if heal is not None and (heal.get("swapped") or heal.get("restored")):
            return _healed_run(name, params, t0, heal, _lower_of(tried),
                               _check_model_calls(req_ctx, []) + probe_calls, storage=storage,
                               executor=api_exec, hosts_path=hosts_path,
                               require_proved=require_proved, post_card=post_card,
                               browser_executor=browser_executor, agent_runner=agent_runner)
        when = next_probe_at(health)
        return {**base, "error": f"the compiled rung is demoted: {_out_of_service_reason(health)}",
                "health": _health_view(record, health), "rungs_tried": tried,
                "out_of_service": {"cause": cause, "next_check_after": when},
                "fallback": _out_of_service_next(cause, when),
                **({"probe": probe} if probe is not None else {}),
                **({"heal": heal} if heal is not None else {}),
                **_asked(_check_model_calls(req_ctx, tried) + probe_calls + _heal_calls(heal))}

    # a destination the pre-send policy refuses before the first request was never asked (unless its name did not
    # resolve): neither a site that is down nor a request that changed, so it is not counted against the compiled
    # request. A redirect it refuses came from a site that answered and points somewhere else: a changed site.
    api_error = str(api_result.get("error") or "")
    resolved = "resolution failed" not in api_error
    policy_refused = not ok and "pre-send policy refused request" in api_error and resolved
    redirect_refused = not ok and "pre-send policy refused a redirect" in api_error and resolved
    # the site did not answer (a refused connection, a timeout, a server error, an earlier one
    # still waited out): nothing says the request changed
    transient = (not ok and not policy_refused and not redirect_refused and not not_found
                 and (bool(held_until) or _transient(api_result)))
    # a rate limit holds the host until the site's Retry-After (execution/blocked.py); not counted
    rate_limited = not ok and _rate_limited(api_result)
    # the request kept failing that way while the site itself answers: it is gone
    gone_now = False
    if (record_outcome and not api_demoted and not replay_only and not policy_refused and not api_terminal
            and not not_found and not held_until and not rate_limited):
        try:
            if transient:
                health = record_outage(health, api_error, api_result.get("retry_after_s"))
                if gone_check_due(health):
                    health, gone_now = _gone_check(name, storage, record, template, api_exec,
                                                   hosts_path, health, lower, api_result)
            else:
                health = fold_outcome(health, ok, api_error)
            if not ok and not transient and not (api_result.get("blocked_state")
                                                 or api_result.get("blocked")):
                # a run of failures on answers from the site (not a block, a rate limit or a
                # site that did not answer) means the site changed: take the rung out of
                # service and say so, instead of failing on every call
                from anyuse.execution.api_recompile import should_recompile
                due, why = should_recompile(health)
                if lower is not None:
                    # the site still answers through a slower rung, so the request itself changed
                    due, why = True, (f"the compiled request failed and the "
                                      f"{lower['served_by'].replace('_', ' ')} answered")
                if due:
                    health = demote(health, f"{why}; {_DEMOTED_NEXT}")
            record["health"] = health.to_dict()
            from anyuse.execution.api_step import signed_in_template
            if ok and not signed_in_template(template):
                # the last good list answer: the anchor a step-level drift
                # repair re-derives from (execution/step_repair.py). Never for a
                # signed-in (login=replay) template: its items are logged-in data.
                from anyuse.execution.step_repair import note_success
                note_success(record, str((params or {}).get(_query_param(template), "")), api_result)
            storage_obj = _storage(storage)
            # under the workflow lock: keeps a concurrent recompile's count and swap
            from anyuse.execution.api_recompile import save_call_outcome
            save_call_outcome(storage_obj, _workflow_id(name, storage_obj), record)
        except Exception as exc:
            logger.debug("could not record the outcome for %r: %s", name, exc)

    heal = None
    stale_after_heal = ""
    site_answered = (record_outcome and not api_demoted and not replay_only and not policy_refused and not api_terminal
                     and (_changed_answer(api_result, ok) or gone_now))
    if site_answered and may_heal:
        heal = _auto_heal(name, storage, params or {}, api_exec, hosts_path,
                          str(api_result.get("error") or ""), record)
        if heal is not None and heal.get("swapped"):
            return _healed_run(name, params, t0, heal, _lower_of(tried),
                               _check_model_calls(req_ctx, []) + probe_calls, storage=storage,
                               executor=api_exec, hosts_path=hosts_path,
                               require_proved=require_proved, post_card=post_card,
                               browser_executor=browser_executor, agent_runner=agent_runner)
    elif site_answered and lower is None:
        # a failure soon after a heal keeps the tool from being healed again
        stale_after_heal = _note_heal_failure(name, storage, str(api_result.get("error") or ""),
                                              record)

    recompiled = None
    if lower is not None and lower.get("evidence") and record_outcome and not replay_only:
        _keep_call_page(name, storage, record, template, params or {}, lower)
    # a browser-replay tool has no compiled request to recompile
    if lower is not None and record_outcome and not health.preferred and not replay_only:
        recompiled = _auto_recompile(name, storage, params or {}, lower, api_exec,
                                     browser_executor, hosts_path, health.demoted_reason)
    checks = _check_model_calls(req_ctx, tried) + probe_calls + _heal_calls(heal)
    if lower is not None:
        lower = {**lower, "model_calls": int(lower.get("model_calls") or 0) + checks}
        return _lower_rung_result(base, info, lower, tried, t0, api_demoted or not health.preferred,
                                  recompiled, replay_only=replay_only, health=health,
                                  held_until=held_until)
    if lower_block is not None:
        api_result = lower_block

    out = {
        **base, "ok": ok,
        "served_by": served or "none",
        "ladder_tier": info["ladder_tier"],
        "ladder_tier_means": info["ladder_tier_means"],
        "seconds": round(time.time() - t0, 3),
        "latency_ms": round_ms(api_result.get("elapsed_ms")),
        # the ladder's own calls, and the check model's for an empty answer
        "model_calls": result.get("llm_calls", 0) + checks,
        "llm_calls": result.get("llm_calls", 0) + checks,
        "proof": info["proof"],
        "chrome_filtered": info["chrome_filtered"],
        "robots": info["robots"],
        "provenance": {**base["provenance"], "served_by": served or "none",
                       "rungs_attempted": ladder.attempted},
        "rungs_tried": tried,
    }
    if ok:
        out["result"] = api_result.get("extracted")
        out["status"] = api_result.get("status")
        out["bytes"] = api_result.get("bytes")
        if probe is not None and probe.get("restored"):
            out["restored"] = dict(record.get("restored") or {})
            out["next"] = _RESTORED_NEXT
        if api_result.get("result_contract"):
            # which runtime result contract accepted this answer (list, sparse
            # or evidenced empty)
            out["result_contract"] = api_result["result_contract"]
        if api_result.get("empty_state"):
            out["empty_state"] = api_result["empty_state"]
        if api_result.get("plan_trace"):
            out["provenance"]["chain"] = api_result["plan_trace"]
    elif api_result.get("blocked_state"):
        # The site asked for a human. Surface it as its own outcome, not as a
        # generic failure, and do not suggest any way around it.
        out["blocked"] = api_result["blocked_state"]
        out["error"] = api_result.get("error")
        out["action_required"] = "human"
        from anyuse.execution import blocked as _blocked
        out["fallback"] = (("the site asked AnyUse to wait (rate limit); nothing is retried "
                            f"before {api_result['blocked_state'].get('until') or 'the wait is over'}, "
                            "then run this again")
                           if api_result["blocked_state"].get("reason") == _blocked.RATE_LIMITED else
                           ("nothing automatic will help here: complete the check in "
                            "your own browser session, then run this again"))
        out["status"] = "needs_human"
        if probe is not None:
            # the call was the check of a request out of service; it stays out of service
            out["probe"] = probe
        if post_card:
            # A direct call has no run to resume, but the person still gets a
            # card (docs/HUMAN_INBOX.md). A routine step posts its own card.
            try:
                from anyuse.execution import human_inbox
                card = human_inbox.Inbox(hosts_path=hosts_path).post_standalone(
                    out["blocked"], name)
                out["needs_human"] = human_inbox.needs_human(card)
            except Exception as exc:          # a card is never worth failing the call
                logger.warning("could not post an inbox card: %s", type(exc).__name__)
    else:
        out["error"] = (api_result.get("error")
                        or step.get("reason") or "the compiled rung failed")
        if api_result.get("result_contract"):
            out["result_contract"] = api_result["result_contract"]
        # the site did not answer (refused, timed out, a server error): nothing says the request changed
        wait = outage_wait(health) if record_outcome else ""
        if policy_refused:
            out["policy_refused"] = True
        elif not_found:
            out["not_found"] = True
        elif gone_now:
            when = next_probe_at(health)
            out["out_of_service"] = {"cause": "gone", "next_check_after": when}
        elif transient:
            out["transient"] = True
            if wait:
                out["retry_after"] = wait
        # the next step `workflow play` prints for the same failure (cli._play_next_lines): a new recording
        # only where a changed site is the likely cause
        out["fallback"] = (POLICY_REFUSED_NEXT if out.get("policy_refused") else
                           NOT_FOUND_NEXT if not_found else
                           _out_of_service_next("gone", out["out_of_service"]["next_check_after"])
                           if gone_now else
                           site_down_next(wait) if out.get("transient") else
                           ("the recorded UI path is the next rung; this surface does not "
                            "drive the desktop, so run it locally with `anyuse workflow play`")
                           if base["has_recorded_path"] else
                           ("there is no recorded path behind this web tool: if the site changed, "
                            "record the search again and recompile it (docs/QUICKSTART_COMPILE.md)"))
        if heal is not None:
            out["heal"] = heal
    if _healed is not None and _healed.get("restored"):
        out["restored"] = {**dict(record.get("restored") or {}), "heal": _healed}
        out["next"] = (_RESTORED_NEXT if ok else
                       "AnyUse put this tool back in service, but this call failed: "
                       + str(out.get("fallback") or "see the error"))
    elif _healed is not None:
        out["healed"] = _healed
        out["next"] = (_healed_next(_healed) if ok else
                       "AnyUse healed this tool, but the new request failed too; it is not healed "
                       "again for 30 minutes: record the search again and recompile it"
                       if stale_after_heal else
                       "AnyUse healed this tool, but this call failed with the new request: "
                       + str(out.get("fallback") or "see the error"))
    if not ok and _terminal_rung_result(api_result):
        for key in ("outcome_kind", "auth_required", "requires_human", "blocked_state", "result_contract"):
            if key in api_result:
                out[key] = api_result[key]
        if not api_result.get("blocked_state") and not not_found:
            out["fallback"] = "execution stopped; no automatic retry or lower-rung fallback"
            if api_result.get("requires_human") or api_result.get("auth_required"):
                out.update(status="needs_human", action_required="human")
    for attempted in reversed(tried):
        if attempted.get("rea_escalation") is not None:
            out["rea_escalation"] = attempted["rea_escalation"]
            break
    return out


# ---------------------------------------------------------------------------
# The runtime cascade: compiled request -> browser replay -> agent -> the person
# ---------------------------------------------------------------------------

RUNG_API, RUNG_BROWSER, RUNG_AGENT = "api", "browser_replay", "agent"
#: what a demotion tells the person; an automatic recompile is tried first (execution/auto_recompile.py)
_DEMOTED_NEXT = ("AnyUse recompiles it automatically when a lower rung answers; if that cannot "
                 "prove a new request, record the search again and recompile it")
#: the agent rung's own step ceiling: a lookup, not an open-ended task
AGENT_RUNG_MAX_STEPS = 12
#: the next step when the site did not answer: nothing says the request changed, so no new recording
SITE_DOWN_NEXT = ("the site did not answer (a refused connection, a timeout or a server error), so nothing "
                  "says the request changed: call this again later")
#: the next step after a request out of service answered again and passed its checks
_RESTORED_NEXT = ("the compiled request was out of service; it answered again and passed its checks, so "
                  "AnyUse put it back in service and it served this call")

#: the next step when a chain's search listed no result with the asked-for name
NOT_FOUND_NEXT = ("nothing was opened: the search listed no result with that name. Ask again "
                  "with the exact name the site uses")
#: the next step when the pre-send policy refused the destination before anything was sent
POLICY_REFUSED_NEXT = ("nothing was sent: the pre-send policy refused this destination. The error names "
                       "the setting or the address that stopped it; report that to the user")


def site_down_next(wait: str = "") -> str:
    """What to do after a call the site did not answer; ``wait``: when AnyUse sends the request
    again."""
    if not wait:
        return SITE_DOWN_NEXT
    return (f"the site did not answer (a refused connection, a timeout or a server error), so nothing "
            f"says the request changed: AnyUse sends it again after {wait}; call this again later")


def _browser_endpoint() -> str:
    """The container browser the lower rungs may use: ANYUSE_CDP_ENDPOINT, else ANYUSE_LEARN_ENDPOINT."""
    from anyuse.execution.cdp_replay import cdp_endpoint
    endpoint = cdp_endpoint()
    if endpoint:
        return endpoint
    try:
        from anyuse.execution.agent_browser import learn_endpoint
        return learn_endpoint() or ""
    except Exception:  # noqa: BLE001
        return ""


def _agent_policy() -> str:
    """The harness policy for the agent rung, or "" when no model is configured for the agent role."""
    import shutil
    from anyuse.execution import model_adapter as M
    try:
        cfg = M.resolve_model_config("agent")
    except Exception:  # noqa: BLE001 - an unusable configuration is no model
        return ""
    if cfg.provider in M.CLI_PROVIDERS:
        binary = "claude" if cfg.provider == "claude-cli" else "codex"
        if not shutil.which(binary):
            return ""
        # claude-cli has its own isolated policy; codex-cli goes through the adapter
        return "claude" if cfg.provider == "claude-cli" else "cerebras"
    try:
        if M.credential_status(cfg) == "unset":
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return "cerebras"


def _task_of(record: Dict[str, Any], name: str) -> Dict[str, Any]:
    """The stored task, or one derived from the record for tools compiled before tasks were stored."""
    task = dict(record.get("task") or {})
    if task.get("text") and task.get("site"):
        return task
    template = _template_of(record)
    host = urllib.parse.urlsplit(str(getattr(template, "url", "") or "")).netloc
    site = str((record.get("source") or {}).get("site") or host or "")
    params = list(getattr(template, "input_params", None) or getattr(template, "params", None) or ["q"])
    param = str(params[0]) if params else "q"
    if not site:
        return {}
    return {"text": f"search {site} for {{{param}}} and list the names of the results in page order",
            "param": param, "site": site, "start_url": f"https://{site}/", "given": False}


def _transient(api_result: Dict[str, Any]) -> bool:
    """A failure that says nothing about the request itself: a server error, a timeout, a refused
    connection, a name that did not resolve (``api_recompile.site_did_not_answer``). The executor's result is the
    outcome: its status and its flags (a block, a robots refusal, a request held for a person) decide before the
    words of its message."""
    from anyuse.execution.api_recompile import site_did_not_answer
    return site_did_not_answer(api_result.get("status"), str(api_result.get("error") or ""), api_result)


def _rate_limited(api_result: Dict[str, Any]) -> bool:
    """The site asked AnyUse to wait (HTTP 429), or its host is still held for it."""
    from anyuse.execution import blocked as _blocked
    return (api_result.get("blocked_state") or {}).get("reason") == _blocked.RATE_LIMITED


def _sent_nothing(api_result: Dict[str, Any]) -> bool:
    """Was the request refused before it left: a held host, the pre-send policy, robots.txt?
    Then no step, of one request or of a chain, has a status, and none has a time on the wire
    but a first hop the pre-send policy refused (the transport checks it and times the check).
    A name that did not resolve is the site not answering, not a refusal."""
    trace = api_result.get("plan_trace")
    steps = trace if isinstance(trace, list) and trace else [api_result]
    for s in steps:
        if not isinstance(s, dict):
            continue
        err = str(s.get("error") or "")
        refused = "pre-send policy refused request" in err and "resolution failed" not in err
        if s.get("status") is not None or (s.get("elapsed_ms") is not None and not refused):
            return False
    return not _transient(api_result)


#: why a request is out of service when it comes back by itself, with no new recording
_COMES_BACK = ("outage", "rate_limit")


def _out_of_service_reason(health) -> str:
    """Why the request is out of service, without the next step to record again when the site
    did not answer."""
    from anyuse.execution.api_recompile import demotion_cause
    reason = str(health.demoted_reason or "")
    if demotion_cause(health) in _COMES_BACK:
        reason = reason.split(f"; {_DEMOTED_NEXT}")[0]
    return reason


def _out_of_service_next(cause: str, when: str) -> str:
    """What to do after a call a request out of service could not answer."""
    again = f"after {when}" if when else "later"
    if cause == "gone":
        return ("the compiled request kept failing while the site's own pages answered, so it is "
                "out of service; AnyUse heals or recompiles it from those pages when it can. If "
                "it cannot, record the search again and recompile it (docs/QUICKSTART_COMPILE.md)")
    if cause in _COMES_BACK:
        what = ("the site asked AnyUse to wait (rate limit)" if cause == "rate_limit" else
                "the site did not answer")
        return (f"{what}; AnyUse tries again {again}. Call this again "
                f"{'after that time' if when else 'later'}; nothing needs recording again")
    return (f"the compiled request gave answers that failed its checks; AnyUse tries it again {again}. "
            f"If the site changed for good, record the search again and recompile it "
            f"(docs/QUICKSTART_COMPILE.md)")


def _claim_probe(name: str, storage, record: Dict[str, Any], health):
    """``(probing, health)``: may this call send the out-of-service request once
    (``api_recompile.claim_probe``)? Bookkeeping never fails the call."""
    from anyuse.execution.api_recompile import claim_probe
    try:
        storage_obj = _storage(storage)
        return claim_probe(storage_obj, _workflow_id(name, storage_obj), record)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not claim a probe for %r: %s", name, exc)
        return False, health


def _note_probe(name: str, storage, record: Dict[str, Any], error: str, outage: bool, health,
                retry_after_s: Optional[float] = None, rate_limited: bool = False):
    """The health after a probe that failed (``api_recompile.note_probe``)."""
    from anyuse.execution.api_recompile import note_probe
    try:
        storage_obj = _storage(storage)
        return note_probe(storage_obj, _workflow_id(name, storage_obj), record, error, outage,
                          retry_after_s=retry_after_s, rate_limited=rate_limited)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not note the probe for %r: %s", name, exc)
        return health


def _restore(name: str, storage, record: Dict[str, Any], why: str) -> Optional[Dict[str, Any]]:
    """The stored record back in service (``api_recompile.restore``), or None."""
    from anyuse.execution.api_recompile import restore
    try:
        storage_obj = _storage(storage)
        return restore(storage_obj, _workflow_id(name, storage_obj), record,
                       f"a probe: {why}", by="probe")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not restore %r: %s", name, type(exc).__name__)
        return None


def _gone_check(name: str, storage, record: Dict[str, Any], template, executor,
                hosts_path: Optional[str], health, lower: Optional[Dict[str, Any]],
                api_result: Dict[str, Any]):
    """``(health, gone)`` after the call that made a gone check due
    (``api_recompile.gone_check_due``): a lower rung that answered this call read the site's
    own pages, so the site is up and nothing is sent; otherwise one read of the site's page
    (``fresh_truth.site_answers``). The request is taken out of service as gone when the
    site answered."""
    from anyuse.execution import api_recompile as R
    from anyuse.execution import fresh_truth as FT
    if lower is not None:
        up, why = True, f"the {lower['served_by'].replace('_', ' ')} answered from the site's pages"
    else:
        try:
            storage_obj = _storage(storage)
            base_dir = str(storage_obj.get_workflow_dir(_workflow_id(name, storage_obj)))
        except Exception:  # noqa: BLE001 - kept recordings are only one place to find a page
            base_dir = ""
        up, why = FT.site_answers(record, template, executor, hosts_path=hosts_path,
                                  base_dir=base_dir, failing_url=str(api_result.get("url") or ""))
        health = R.note_site_check(health)
    if not up:
        return health, False
    first = R._timestamp(health.first_outage_at)
    minutes = int((R.time.time() - first) // 60) if first is not None else 0
    error = str(api_result.get("error") or "no answer")[:120]
    return R.mark_gone(health, (f"the compiled request failed {health.consecutive_outages} times "
                                f"over {minutes} min ({error}) while {why}; {_DEMOTED_NEXT}")), True


def _query_param(template) -> str:
    """The input a list tool's last good answer is kept for (``step_repair.note_success``)."""
    from anyuse.execution import result_contract as RC
    return str((RC.contract_of(template) or {}).get("query_param")
               or (list(getattr(template, "params", None) or []) or ["q"])[0])


def _probe_holds(record: Dict[str, Any], template, params: Dict[str, Any],
                 api_result: Dict[str, Any], executor, *, name: str = "", storage=None,
                 hosts_path: Optional[str] = None, browser_executor=None
                 ) -> Tuple[bool, str, Dict[str, Any]]:
    """Did a probe's answer show the request works again? It passed the request's checks (its
    result contract among them); for a list tool with a last good answer (the drift anchor), the
    anchor's input must give an answer that holds now (``fresh_truth.answer_holds_now``): the
    last good answer again, or the list the browser replay or the server-rendered result page
    shows for it now. This call's answer counts when it asked for that input, else one more
    request goes through the same executor."""
    from anyuse.execution import fresh_truth as FT
    from anyuse.execution import step_repair as SR
    from anyuse.execution.api_compile import normalise
    from anyuse.execution.api_step import signed_in_template
    anchor = SR.anchor_of(record)
    if anchor is None or getattr(template, "shape", "list") != "list" or signed_in_template(template):
        return True, "the answer passed the tool's checks", {"by": "checks"}
    qp = _query_param(template)
    value = str(anchor["value"])
    other = None
    if normalise(str(params.get(qp, ""))) == normalise(value):
        result = api_result
    else:
        result = executor.execute(template, {**params, qp: value})
        other = (str(params.get(qp, "")), api_result)
    replay = None
    rung = (record.get("browser") or {}).get("template")
    if browser_executor is not None and rung:
        from anyuse.execution.cdp_replay import ReplayTemplate

        def replay(values: Dict[str, Any]) -> Dict[str, Any]:
            return _replay(browser_executor, ReplayTemplate.from_dict(rung), values)
    try:
        storage_obj = _storage(storage)
        base_dir = str(storage_obj.get_workflow_dir(_workflow_id(name, storage_obj))) if name else ""
    except Exception:  # noqa: BLE001 - kept recordings are only one place to find the page
        base_dir = ""
    return FT.answer_holds_now(record, template, anchor, result, executor=executor,
                               params=dict(params), hosts_path=hosts_path, base_dir=base_dir,
                               replay=replay, other=other)


def _as_result(value: Any) -> Optional[Dict[str, Any]]:
    """The agent's answer in the shape of a compiled tool's result."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return {"items": [str(x) for x in value]}
    return {"text": str(value)}


def _item_name(item: Any) -> str:
    """One listed item's name: a string as is. A record is named from its own structure first:
    the field whose text is what the result's own address names (the last path segment of the
    record's http(s) URL, as words; a segment without a word of letters, an id, names
    nothing), unless it only copies the segment as written (a slug field); then a
    name/title/label/text key; then the longest field whose words stand in
    that segment; then its first text. A name that is the segment's words followed only by
    tokens that each hold a digit (a version, a count) is cut to those words ("rand v0.10.3"
    at /packages/rand is "rand"); one followed by words keeps them ("123 Main St" at /123)."""
    if not isinstance(item, dict):
        return str(item)
    from anyuse.execution.api_compile import contains_words, normalise
    texts = [(k, str(v)) for k, v in item.items() if isinstance(v, str) and v.strip()]

    def address(v: str) -> bool:
        try:
            sp = urllib.parse.urlsplit(v.strip())
        except ValueError:
            return False
        return sp.scheme in ("http", "https") and bool(sp.netloc)
    url = str(item.get("url") or "") if address(str(item.get("url") or "")) else \
        next((v for _k, v in texts if address(v)), "")
    seg = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) \
        if url else ""
    want = normalise(seg)
    if not any(len(t) >= 2 and t.isalpha() for t in want.split()):
        want = ""
    named = [(k, v) for k, v in texts if not address(v)]
    keys = ("name", "title", "label", "text")
    label = ""
    if want:
        # a field that is the segment as the address writes it ("pizza-margherita") is the
        # address's own copy, not the text the page shows as the name
        same = [(k, v) for k, v in named if normalise(v) == want]
        label = next((v for k, v in same if k in keys),
                     next((v for _k, v in same if v.strip() != seg), ""))
    if not label:
        label = next((str(item[k]) for k in keys if item.get(k)), "")
    if not label and want:
        inside = [v for _k, v in named if normalise(v) and contains_words(want, normalise(v))]
        label = max(inside, key=lambda v: len(normalise(v)), default="")
    if not label:
        label = next((v for _k, v in texts), "")
    if want:
        tokens = label.split()
        for k in range(1, len(tokens)):
            if normalise(" ".join(tokens[:k])) == want:
                rest = tokens[k:]
                if all(any(ch.isdigit() for ch in t) for t in rest):
                    return " ".join(tokens[:k])
                break
    return label


def _as_declared(value: Any, template) -> Optional[Dict[str, Any]]:
    """The agent's answer in the result shape the compiled tool declares (a list tool: its one key
    holding the item names), so every rung answers alike; None when nothing usable came back."""
    keys = list((getattr(template, "extract", {}) or {}).keys())
    shape = getattr(template, "shape", "list")
    if shape == "list" and len(keys) == 1:
        items = None
        if isinstance(value, (list, tuple)):
            items = list(value)
        elif isinstance(value, dict):
            preferred = [value.get(keys[0]), value.get("items"), value.get("results")]
            items = next((v for v in preferred if isinstance(v, list)), None)
            if items is None:
                items = next((v for v in value.values() if isinstance(v, list)), None)
        names = [n for n in (_item_name(x) for x in (items or [])) if n]
        return {keys[0]: names} if names else None
    if isinstance(value, dict) and keys and all(k in value for k in keys):
        return {k: value[k] for k in keys}
    return None if shape == "fields" else _as_result(value)


def _lower_rungs(record: Dict[str, Any], name: str, params: Dict[str, Any], *,
                 browser_executor=None, agent_runner=None, agent: bool = True, storage=None,
                 learn_empty: bool = True, product: bool = False):
    """``(served, tried, block)``: the first lower rung that answered, every rung tried with
    why it failed, and a block (a CAPTCHA, a sign-in wall, robots) that stops the cascade.
    ``agent=False`` stops after the browser replay. ``learn_empty``: the browser replay may
    learn the site's empty page at an empty answer and keep it on the stored record;
    ``product``: it may ask the check model about one."""
    tried: List[Dict[str, Any]] = []
    rung = record.get("browser") or {}
    endpoint = _browser_endpoint()
    if not rung.get("template"):
        tried.append({"rung": RUNG_BROWSER, "ok": False, "skipped": True,
                      "why": "this tool has no browser replay (no result page was recorded with it)"})
    elif browser_executor is None and not endpoint:
        tried.append({"rung": RUNG_BROWSER, "ok": False, "skipped": True,
                      "why": "no container browser configured (ANYUSE_CDP_ENDPOINT or ANYUSE_LEARN_ENDPOINT)"})
    else:
        from anyuse.execution import empty_page as EP
        from anyuse.execution.cdp_replay import ReplayExecutor, ReplayTemplate
        executor = browser_executor or ReplayExecutor(endpoint=endpoint)
        bctx = EP.context_for(record, "browser", rung["template"], learn=learn_empty)
        with _product_scope(product):
            res = _replay(executor, ReplayTemplate.from_dict(rung["template"]), dict(params),
                          capture=True, empty=bctx)
        _keep_empty_page(name, storage, record, "browser", bctx, persist=learn_empty)
        if res.get("success"):
            extracted = res.get("extracted") or {}
            items = (extracted.get("labels") if rung.get("read") == "labels" and extracted.get("labels")
                     else extracted.get("items"))
            template = _template_of(record)
            answer = extracted if getattr(template, "shape", "list") == "fields" else {"items": list(items or [])}
            field_problem = _field_result_problem(answer, template, record.get("proof"))
            if field_problem:
                res = dict(res, success=False, error=field_problem)
        if res.get("success"):
            tried.append({"rung": RUNG_BROWSER, "ok": True, "ms": round_ms(res.get("elapsed_ms"))})
            served = {"served_by": RUNG_BROWSER, "result": answer,
                      "latency_ms": round_ms(res.get("elapsed_ms")),
                      "model_calls": int(res.get("model_calls") or 0),
                      "cost_usd": 0.0, "url": res.get("url")}
            if res.get("empty_state"):
                served["empty_state"] = res["empty_state"]
            if res.get("result_contract"):
                served["result_contract"] = res["result_contract"]
            served["replay"] = res                # the page and its traffic, for a recompile
            return served, tried, None
        asked = int(res.get("model_calls") or 0)
        tried.append({"rung": RUNG_BROWSER, "ok": False, "ms": round_ms(res.get("elapsed_ms")),
                      "why": str(res.get("error") or "the browser replay failed")[:200],
                      **({"model_calls": asked} if asked else {})})
        if _terminal_rung_result(res):
            return None, tried, res

    if not agent:
        tried.append({"rung": RUNG_AGENT, "ok": False, "skipped": True, "why": "fall-back turned off"})
        return None, tried, None
    task = _task_of(record, name)
    policy = _agent_policy()
    if not task:
        tried.append({"rung": RUNG_AGENT, "ok": False, "skipped": True,
                      "why": "no task is known for this tool"})
    elif agent_runner is None and not policy:
        tried.append({"rung": RUNG_AGENT, "ok": False, "skipped": True,
                      "why": "no model configured (ANYUSE_MODEL_PROVIDER, docs/MODEL_CONFIGURATION.md)"})
    elif agent_runner is None and not endpoint:
        tried.append({"rung": RUNG_AGENT, "ok": False, "skipped": True,
                      "why": "no container browser configured for the agent (ANYUSE_LEARN_ENDPOINT)"})
    else:
        values = {str(k): str(v) for k, v in params.items()}
        kwargs: Dict[str, Any] = {"values": values, "site": task.get("start_url") or task.get("site"),
                                  "policy": policy or "cerebras", "max_steps": AGENT_RUNG_MAX_STEPS,
                                  "commits_need_human": True}
        if agent_runner is None:
            from anyuse.execution import harness_product as HP
            from anyuse.execution.agent_browser import learn_endpoint
            if not learn_endpoint():
                # only ANYUSE_CDP_ENDPOINT names the container browser: hand it to the agent's page
                kwargs["page_factory"] = HP.guarded_page_factory(
                    endpoint, start_url=str(kwargs["site"]))
            agent_runner = HP.run_agent_task
        if _keeps_evidence(agent_runner, record):
            # the page the agent answers on, kept as recompile evidence (execution/evidence_store.py)
            kwargs["keep_evidence"] = True
        t0 = time.time()
        try:
            out = agent_runner(str(task["text"]), **kwargs)
        except Exception as exc:  # noqa: BLE001 - a crashed agent is a failed rung, never a crashed tool
            out = {"ok": False, "outcome": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        template = _template_of(record)
        result = _as_declared(out.get("result"), template) if out.get("ok") else None
        if result is not None and _field_result_problem(result, template, record.get("proof")):
            result = None
        if result is not None:
            tried.append({"rung": RUNG_AGENT, "ok": True, "ms": round((time.time() - t0) * 1000.0, 1)})
            served = {"served_by": RUNG_AGENT, "result": result,
                      "latency_ms": round((time.time() - t0) * 1000.0, 1),
                      "model_calls": int(out.get("model_calls") or 0),
                      "cost_usd": float(out.get("cost_usd") or 0.0),
                      "agent": {"outcome": out.get("outcome"), "steps": out.get("steps"),
                                "policy": out.get("policy") or kwargs["policy"],
                                "task": task["text"], "answer": out.get("result")}}
            if isinstance(out.get("evidence"), dict) and kwargs.get("keep_evidence"):
                served["evidence"] = out["evidence"]
            if out.get("rea_escalation") is not None:
                served["rea_escalation"] = out["rea_escalation"]
                served["agent"]["rea_escalation"] = out["rea_escalation"]
            return served, tried, None
        tried.append({"rung": RUNG_AGENT, "ok": False,
                      "why": str(out.get("reason") or out.get("outcome") or "the agent did not answer")[:200],
                      "model_calls": int(out.get("model_calls") or 0),
                      "cost_usd": float(out.get("cost_usd") or 0.0),
                      **({"rea_escalation": out["rea_escalation"]} if out.get("rea_escalation") is not None else {})})
        if out.get("outcome") in ("human_required", "captcha", "blocked"):
            return None, tried, {"blocked": True, "requires_human": True,
                                 "error": str(out.get("reason") or out.get("outcome")),
                                 "blocked_state": {"blocked": True, "reason": str(out.get("outcome")),
                                                   "message": str(out.get("reason") or ""),
                                                   "action_required": "human"}}
    return None, tried, None


def _keeps_evidence(agent_runner, record: Dict[str, Any]) -> bool:
    """Should the agent rung return the page it answered on? The runner takes
    ``keep_evidence``, ``ANYUSE_KEEP_EVIDENCE`` is on and the tool is not signed in."""
    import inspect
    from anyuse.execution import evidence_store as ES
    from anyuse.execution.auto_recompile import signed_in
    try:
        params = inspect.signature(agent_runner).parameters
    except (TypeError, ValueError):
        return False
    takes = "keep_evidence" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
    try:
        return takes and ES.enabled() and not signed_in(record)
    except Exception:  # noqa: BLE001 - an unreadable template keeps nothing
        return False


def _keep_call_page(name: str, storage, record: Dict[str, Any], template,
                    params: Dict[str, Any], lower: Dict[str, Any]) -> str:
    """Keep the page the agent answered this call on (``evidence_store.keep_call_page``);
    why it was or was not kept. Never fails the call."""
    from anyuse.execution import evidence_store as ES
    result = lower.get("result") or {}
    items = next((v for v in result.values() if isinstance(v, list)), []) \
        if isinstance(result, dict) else []
    qp = _query_param(template)
    try:
        storage_obj = _storage(storage)
        return ES.keep_call_page(storage_obj, _workflow_id(name, storage_obj), record, param=qp,
                                 value=str(params.get(qp, "")), page=lower.get("evidence"),
                                 items=items, rung=str(lower.get("served_by") or RUNG_AGENT))
    except Exception as exc:  # noqa: BLE001 - kept evidence is never worth failing the call
        logger.debug("could not keep the agent's page for %r: %s", name, exc)
        return ""


def _replay(executor, template, params: Dict[str, Any], capture: bool = False,
            empty=None) -> Dict[str, Any]:
    """One browser replay; asks for the page's traffic when the executor can capture it, and
    hands it the empty-answer context (``empty``) when it takes one."""
    import inspect
    try:
        accepted = inspect.signature(executor.execute).parameters
    except (TypeError, ValueError):
        accepted = {}
    kw: Dict[str, Any] = {"empty": empty} if empty is not None and "empty" in accepted else {}
    if capture and "capture_traffic" in accepted:
        return executor.execute(template, params, capture_traffic=True, **kw)
    res = executor.execute(template, params, **kw)
    return {**res, "capture": "unsupported"} if capture and isinstance(res, dict) else res


def _check_model_calls(ctx, tried: List[Dict[str, Any]]) -> int:
    """The check model's calls for an empty answer in this call: the compiled request's (``ctx``)
    and those of a browser replay that did not serve the call (its entry in ``tried``)."""
    return (ctx.model_calls if ctx is not None else 0) + sum(
        int(t.get("model_calls") or 0) for t in tried if t.get("rung") == RUNG_BROWSER)


def _asked(calls: int) -> Dict[str, int]:
    """``model_calls`` and ``llm_calls`` for a failure that asked the check model."""
    return {"model_calls": calls, "llm_calls": calls} if calls else {}


def _set_empty_context(executor, ctx) -> None:
    """Hand the compiled request's executor this call's empty-answer context (or take it back)."""
    try:
        executor.empty_context = ctx
    except AttributeError:                     # an executor that keeps no attributes
        pass


@contextlib.contextmanager
def _product_scope(product: bool):
    """A product call's model-check scope (execution/model_check.py); nothing otherwise, so a
    proof, a recompile or a watch run asks no check model. A heal and the check of a request
    out of service ask only in the scope execution/fresh_truth.py opens for its one question."""
    if not product:
        yield
        return
    from anyuse.execution import model_check as MC
    with MC.scope("product"):
        yield


def _keep_empty_page(name: str, storage, record: Dict[str, Any], rung: str, ctx,
                     persist: bool) -> None:
    """Keep the empty page (or the retry marker) ``ctx`` learned for ``rung`` on the record and,
    with ``persist``, on the stored record, when that still holds the template it was learned
    for. A failure to store it never fails the call."""
    if ctx is None or not ctx.new_learned:
        return
    record.setdefault("empty_page", {})[rung] = ctx.new_learned
    if not persist:
        return
    try:
        from anyuse.execution.api_compile import load_compiled, persist_compiled
        from anyuse.execution.api_recompile import workflow_lock
        from anyuse.execution.workflow_schema import template_digest
        storage_obj = _storage(storage)
        wid = _workflow_id(name, storage_obj)
        with workflow_lock(storage_obj, wid):
            stored = load_compiled(storage_obj, wid)
            if stored is None:
                return
            held = (stored.get("browser") or {}).get("template") if rung == "browser" \
                else stored.get("template")
            if template_digest(held or {}) != ctx.new_learned.get("for"):
                return
            stored.setdefault("empty_page", {})[rung] = ctx.new_learned
            persist_compiled(storage_obj, wid, stored)
    except Exception as exc:  # noqa: BLE001 - a signature that is not stored is learned again
        logger.debug("could not store the empty page learned for %r: %s", name, exc)


def _pinned_by(name: str, record: Dict[str, Any]) -> List[str]:
    """The registered skills (``skill_tools.load_registry``) with a compiled step that pins this
    tool's current proof id."""
    try:
        from anyuse.mcpserver.skill_tools import load_registry
        registry = load_registry()
    except Exception as exc:  # noqa: BLE001 - an unreadable registry pins nothing
        logger.debug("skill registry not read: %s", exc)
        return []
    pid = proof_id(record)
    return [skill for skill, manifest in registry.items()
            if any((step.get("action") or {}).get("kind") == "compiled_tool"
                   and (step.get("action") or {}).get("ref") == name
                   and (step.get("action") or {}).get("proof_id") == pid
                   for step in manifest.get("steps") or [])]


def _auto_recompile(name: str, storage, params: Dict[str, Any], lower: Dict[str, Any], api_exec,
                    browser_executor, hosts_path: Optional[str], why: str) -> Dict[str, Any]:
    """A paced automatic recompile after a lower rung answered for an out-of-service request
    (execution/auto_recompile.py); its summary for the call result. Never fails the call."""
    from anyuse.execution import auto_recompile as AR
    from anyuse.execution.api_permission import unattended
    replay = None
    if lower.get("served_by") == RUNG_BROWSER and browser_executor is not None:
        # the live evidence comes from the replay that just served; a broken replay gives none
        from anyuse.execution.cdp_replay import ReplayTemplate
        executor = browser_executor

        def replay(values: Dict[str, Any], capture: bool) -> Dict[str, Any]:
            record = _find_record(name, storage) or {}
            template = ReplayTemplate.from_dict((record.get("browser") or {}).get("template") or {})
            return _replay(executor, template, values, capture=capture)
    base_why = str(why or "").split("; automatic recompile")[0].split(f"; {_DEMOTED_NEXT}")[0]
    try:
        storage_obj = _storage(storage)
        with unattended("recompile"):
            return AR.recompile(storage_obj, _workflow_id(name, storage_obj), params,
                                call_replay=(lower.get("replay")
                                             if lower.get("served_by") == RUNG_BROWSER else None),
                                replay=replay, executor=api_exec, hosts_path=hosts_path,
                                why=base_why or "the compiled request is out of service",
                                pinned_by=_pinned_by(name, _find_record(name, storage) or {}))
    except Exception as exc:  # noqa: BLE001 - a failed recompile never fails the answer
        logger.warning("automatic recompile of %r failed: %s", name, type(exc).__name__)
        return {"attempted": False, "swapped": False,
                "reason": f"the recompile stopped: {type(exc).__name__}: {str(exc)[:160]}"}


#: the next step after a call that a heal answered
def _healed_next(heal: Dict[str, Any]) -> str:
    """The next step after a call that a heal answered."""
    calls = int((heal or {}).get("model_calls") or 0)
    how = ("no browser, no model" if not calls else
           f"no browser; the check model was asked {calls} time{'s' if calls != 1 else ''} "
           f"which lines of the page are the results")
    return (f"the site changed and AnyUse healed this tool from plain reads of its result page "
            f"({how}); the new request was proved on held-out inputs and served this call, and "
            f"the next calls use it")


def _changed_answer(api_result: Dict[str, Any], ok: bool) -> bool:
    """The compiled request failed on an answer from the site that is not a block, a rate
    limit or a server error: the request or the response may have changed."""
    if ok or api_result.get("blocked") or api_result.get("blocked_state") \
            or api_result.get("requires_human"):
        return False
    return isinstance(api_result.get("status"), int) and not _transient(api_result)


def _auto_heal(name: str, storage, params: Dict[str, Any], api_exec, hosts_path: Optional[str],
               why: str, ran: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A paced heal from plain reads of the result page (execution/step_heal.py); its summary,
    or None when ``ANYUSE_AUTO_HEAL`` is off. ``ran``: the record this call ran. Never fails
    the call."""
    from anyuse.execution import step_heal
    if not step_heal.enabled():
        return None
    try:
        storage_obj = _storage(storage)
        return step_heal.heal(storage_obj, _workflow_id(name, storage_obj), params,
                              executor=api_exec, hosts_path=hosts_path, why=why,
                              pinned_by=_pinned_by(name, _find_record(name, storage) or {}),
                              ran=ran)
    except Exception as exc:  # noqa: BLE001 - a failed heal never fails the answer
        logger.warning("automatic heal of %r failed: %s", name, type(exc).__name__)
        return {"attempted": False, "swapped": False,
                "reason": f"the heal stopped: {type(exc).__name__}: {str(exc)[:160]}"}


def _note_heal_failure(name: str, storage, why: str, ran: Dict[str, Any]) -> str:
    """The stale reason when this failure came soon after a heal (``step_heal.note_failure``),
    else ``""``."""
    try:
        from anyuse.execution import step_heal
        storage_obj = _storage(storage)
        return step_heal.note_failure(storage_obj, _workflow_id(name, storage_obj), why, ran=ran)
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the answer
        logger.debug("could not note the failure for %r: %s", name, exc)
        return ""


def _lower_of(tried: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The lower rungs a call tried before its heal."""
    return [t for t in tried if t.get("rung") != RUNG_API]


def _heal_calls(heal: Optional[Dict[str, Any]]) -> int:
    """The check model's calls a heal of this call made."""
    return int((heal or {}).get("model_calls") or 0)


def _healed_run(name: str, params: Optional[Dict[str, Any]], t0: float, heal: Dict[str, Any],
                lower_tried: List[Dict[str, Any]], asked_before: int = 0,
                **kw: Any) -> Dict[str, Any]:
    """The call again, with the request a heal swapped in or put back; ``seconds`` covers the
    heal too. The lower rungs this call tried before the heal are not tried again (their
    entries carry their own model calls); ``asked_before``: the check model's other calls
    before the heal (the compiled request's empty answer, the probe)."""
    out = run_compiled(name, params, _healed=heal, _lower_tried=lower_tried, **kw)
    out["seconds"] = round(time.time() - t0, 3)
    asked = _heal_calls(heal) + int(asked_before or 0)
    if asked:
        # the check model asked before and in the heal is part of this call's cost
        out["model_calls"] = int(out.get("model_calls") or 0) + asked
        out["llm_calls"] = int(out.get("llm_calls") or 0) + asked
    return out


def _lower_rung_result(base: Dict[str, Any], info: Dict[str, Any], lower: Dict[str, Any],
                       tried: List[Dict[str, Any]], t0: float, api_out_of_service: bool,
                       recompiled: Optional[Dict[str, Any]] = None,
                       replay_only: bool = False, health=None,
                       held_until: str = "") -> Dict[str, Any]:
    """A run answered by the browser replay or the agent: which rung served, what each rung did, and
    whether the compiled request was recompiled or still needs recompiling (a tool with no compiled
    request says so instead). ``health``: the request's health after this call; ``held_until``: the
    request was not sent because the site did not answer the last calls."""
    from anyuse.execution.api_recompile import demotion_cause, next_probe_at
    served = lower["served_by"]
    cause = demotion_cause(health) if api_out_of_service and health is not None else ""
    outage = cause in _COMES_BACK
    when = next_probe_at(health) if health is not None else ""
    # after an outage the request comes back by itself; after wrong answers it needs a new one
    restore_step = ((f"AnyUse tries the compiled request again after {when}" if when else
                     "AnyUse tries the compiled request again later") if outage else
                    f"record the search again and recompile {base['workflow']!r} to restore the fast rung")
    out = {
        **base, "ok": True, "served_by": served,
        "ladder_tier": rung_tier(served), "ladder_tier_means": TIER_MEANING[rung_tier(served)],
        "seconds": round(time.time() - t0, 3),
        "latency_ms": lower.get("latency_ms"),
        "model_calls": lower.get("model_calls", 0), "llm_calls": lower.get("model_calls", 0),
        "cost_usd": lower.get("cost_usd", 0.0),
        "proof": info["proof"], "chrome_filtered": info["chrome_filtered"], "robots": info["robots"],
        "result": lower.get("result"),
        "provenance": {**base["provenance"], "served_by": served,
                       "rungs_attempted": [t["rung"] for t in tried if not t.get("skipped")]},
        "rungs_tried": tried,
        "next": (BROWSER_TOOL_NOTE if replay_only and served == RUNG_BROWSER else
                 ("this tool has no compiled request and the browser replay did not answer; "
                  "the agent did") if replay_only else
                 (f"the compiled request is out of service"
                  + (" because the site asked AnyUse to wait (rate limit)" if cause == "rate_limit" else
                     " because the site did not answer" if outage else
                     " because it kept failing while the site's own pages answered"
                     if cause == "gone" else "")
                  + f" and this answer came from the {served.replace('_', ' ')}; {restore_step}")
                 if api_out_of_service else
                 (f"the site did not answer the last calls, so the compiled request is not sent again "
                  f"before {held_until}; the {served.replace('_', ' ')} answered instead")
                 if held_until else
                 (f"the compiled request failed this time and the {served.replace('_', ' ')} "
                  f"answered instead")),
    }
    for key in ("empty_state", "agent", "url", "result_contract", "rea_escalation"):
        if lower.get(key):
            out[key] = lower[key]
    if recompiled is not None:
        out["recompiled"] = recompiled
        if recompiled.get("swapped"):
            out["next"] = (f"the compiled request failed and the {served.replace('_', ' ')} answered; "
                           f"AnyUse recompiled {base['workflow']!r} from "
                           f"{recompiled.get('from') or 'fresh evidence'} and re-proved it, so the "
                           f"next call is served by the compiled request again")
        elif api_out_of_service:
            out["next"] = (f"the compiled request is out of service and this answer came from the "
                           f"{served.replace('_', ' ')}; automatic recompile: {recompiled.get('reason')}"
                           + (f" (next attempt after {recompiled['next_attempt_after']})"
                              if recompiled.get("next_attempt_after") not in (None, "", "never")
                              else "")
                           + f"; {restore_step}")
    return out


def _has_recorded_path(name: str, storage) -> bool:
    try:
        return _storage(storage).get(name) is not None
    except Exception:  # noqa: BLE001 - an unreadable store has nothing to fall back to
        return False


def _workflow_id(name: str, storage) -> str:
    try:
        wf = storage.get(name)
        if wf is not None:
            return wf.id
    except Exception:
        pass
    return name


# ---------------------------------------------------------------------------
# The two MCP tool payloads (kept here so server.py stays a thin registration)
# ---------------------------------------------------------------------------

TOOL_LIST_NAME = "list_compiled_workflows"
TOOL_RUN_NAME = "run_compiled_workflow"
TOOL_EXPLAIN_NAME = "explain_workflow"


def explain_compiled(name: str, storage=None,
                     hosts_path: Optional[str] = None) -> Dict[str, Any]:
    """Everything an agent needs to decide whether to TRUST this workflow.

    Read-only: it opens no socket, runs nothing, and changes nothing. It exists
    because `list_compiled_workflows` answers "what is there" and
    `run_compiled_workflow` answers "what happened", and neither answers the
    question an agent should actually ask first — *on what evidence, and where
    would it be wrong?*

    Every field is either recorded evidence or a stated limit. There is no
    confidence score: a number between 0 and 1 invites an agent to reason about
    a quantity nobody measured.
    """
    record = _find_record(name, storage)
    if record is None:
        return {"ok": False, "workflow": name,
                "error": "no compiled workflow with that name",
                "hint": "call list_compiled_workflows first"}
    template = _template_of(record)
    info = describe_compiled(name, record, hosts_path)
    proof = record.get("proof") or {}
    cases = proof.get("cases") or []
    kind = "chain" if getattr(template, "steps", None) else getattr(template, "extract_kind", "json")
    if getattr(template, "readonly_post", False):
        kind = "readonly_post"
    if replay_tool(record):
        kind = KIND_BROWSER

    durability = {
        "json": ("reads a JSON API's own fields. The most durable rung: a "
                 "redesign of the page does not touch it. It still breaks if "
                 "the endpoint changes."),
        "embedded_json": ("reads a JSON payload embedded in the page. Survives "
                          "a visual redesign; breaks if the payload moves."),
        "html": ("reads the page's MARKUP with a CSS-like selector. The most "
                 "brittle rung: a class rename or a re-nest breaks it. The "
                 "recorded count and identity postconditions catch known "
                 "incomplete shapes; selector semantics remain a limitation."),
        "detail_fields": ("reads one identifying title, heading or name field "
                          "from an item page. The fresh proof checked that field "
                          "against captured item pages; it is not a completeness "
                          "claim about the rest of the page."),
        "chain": ("two or more dependent requests: a search, then the result it "
                  "lists (the first, or the one with the asked-for name); or the "
                  "site's own page, read for a key or id it states, then the request "
                  "that sends it. Breaks if any of them changes, including a page "
                  "that no longer states its key."),
        KIND_BROWSER: ("opens the page in a container browser and reads the result rows "
                       "with the selector the replay proof used. No compiled request: every "
                       "call needs a browser and takes seconds (no model). Breaks when the "
                       "page's markup changes."),
        "readonly_post": ("a POST the page itself issued once per query, proved "
                          "to be a read (a GraphQL query or a search body) and "
                          "carrying no credential. Still a POST: it is refused "
                          "unless ANYUSE_API_READONLY_POST=on and its exact "
                          "endpoint/operation approval is recorded."),
    }.get(kind, "")

    return {
        "ok": True,
        "workflow": name,
        "ladder_tier": info["ladder_tier"],
        "ladder_tier_means": info["ladder_tier_means"],
        "rung": kind,
        "what_it_does": getattr(template, "notes", "") or "",
        "parameters": info["parameters"],
        "result_shape": info["result_shape"],
        "urls": info["urls"],
        "method": getattr(template, "method", "GET"),
        "evidence": {
            "proof_id": info["proof"]["id"],
            "passed": info["proof"]["passed"],
            "status": info["proof"]["status"],
            "scope": info["proof"]["scope"],
            "split": info["proof"]["split"],
            "fresh_inputs_checked": [c.get("value") for c in cases],
            "what_was_compared": proof.get("reason", ""),
            "per_case": [{"value": c.get("value"), "passed": c.get("passed"),
                          "coverage": c.get("coverage"),
                          "order_ok": c.get("order_ok"),
                          "ui_count": c.get("ui_count"),
                          "api_count": c.get("api_count")} for c in cases],
        },
        "durability": durability,
        "how_it_fails": (
            "Any failure — network, non-2xx, a changed shape, a false "
            "postcondition — fails the compiled request. Incomplete or identity-"
            "mismatched output stops that rung, as a transport failure does: the "
            "call then tries the browser replay and the agent when they are "
            "configured. When none answers and the site changed (or the request "
            "keeps failing while the site's own page answers), AnyUse heals the "
            "request from plain reads of its result page and proves it on held-out "
            "inputs before it serves. A challenge or CAPTCHA is "
            "reported as needing a human and the host is not retried."),
        "chrome_filtered": info["chrome_filtered"],
        "chrome_filtered_means": (
            "labels that appeared on the page for EVERY recorded query and were "
            "therefore treated as navigation/footer rather than as results. If "
            "one of these is actually an answer, the proof checked the wrong "
            "thing."),
        "robots": info["robots"],
        "host_acknowledged": info["host_acknowledged"],
        "health": info["health"],
        "risk_class": info["risk_class"],
        "runnable_without_human": info["runnable_without_human"],
        "limits": [
            "The proof covers the input values listed under evidence, and "
            "nothing else. It is not a claim about every input.",
            "It is not a claim that the endpoint is stable, that the site "
            "permits automated access, or anything about write methods.",
            "Compiled at " + str(record.get("recompiled_at") or record.get("compiled_at", "")) +
            "; the site may have changed since.",
        ],
    }

TOOL_LIST_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {"workflow": {"type": "string",
                                "description": "name from list_compiled_workflows"}},
    "required": ["workflow"],
    "additionalProperties": False,
}
TOOL_EXPLAIN_DESCRIPTION = (
    "Explain a compiled AnyUse workflow so you can decide whether to trust it: which "
    "ladder tier it is, which rung and how durable that rung is, the exact fresh inputs "
    "its proof checked and what was compared, which on-screen labels were filtered out "
    "as page chrome, the robots verdict, and how it fails. Read-only: opens no socket, "
    "runs nothing, changes nothing. There is deliberately no confidence score."
)
TOOL_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "workflow": {"type": "string", "description": "name from list_compiled_workflows"},
        "parameters": {"type": "object",
                       "description": "values for the workflow's declared parameters"},
    },
    "required": ["workflow"],
    "additionalProperties": False,
}

TOOL_LIST_DESCRIPTION = (
    "List AnyUse workflows that have been COMPILED to a proved HTTP request, with their "
    "parameters, result shape, risk class, host acknowledgement and the evidence the "
    "template was kept on. Read-only; touches no desktop."
)
TOOL_RUN_DESCRIPTION = (
    "Run a compiled, read-only AnyUse workflow and return its extracted values with "
    "provenance (which rung served it, seconds, proof id, URLs). Only workflows the "
    "existing HIL gate classifies read_only, whose fresh-input proof passed, whose rung "
    "is not demoted (or is demoted with its check due, or has a lower rung that can "
    "serve it) and whose host has been acknowledged will run. This tool NEVER "
    "drives the desktop: if the compiled request fails, the browser replay and then "
    "the agent answer when they are configured, and when none answers and the site "
    "changed, AnyUse heals the request from plain reads of its result page. A recorded "
    "UI path is only reported, never driven."
)


def handle(name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Dispatch for the two tools; ``None`` when ``name`` is not ours."""
    if name == TOOL_LIST_NAME:
        return {"compiled_workflows": list_compiled()}
    if name == TOOL_RUN_NAME:
        return run_compiled(str(arguments.get("workflow", "")),
                            dict(arguments.get("parameters") or {}))
    if name == TOOL_EXPLAIN_NAME:
        return explain_compiled(str(arguments.get("workflow", "")))
    return None
