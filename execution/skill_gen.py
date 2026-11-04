"""Generate SKILL.md and an MCP tool definition from ONE manifest.

Both projections are pure functions of the manifest and both carry its
``manifest_digest``; a test asserts they agree, so the prose an agent reads can
never describe a different tool than the one that runs.

Instruction-surface hygiene (docs/THREAT_MODEL.md): the MCP ``description`` is
built from trusted enums, counts and the digest only. Owner-authored free text
(``summary``, human-step instructions) appears only in the SKILL.md body, after
:func:`plain` has removed control characters, markdown structure and length.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

from anyuse.execution.skill_manifest import (
    MANIFEST_SCHEMA, manifest_digest, object_schema, to_json_schema, validate_manifest,
)

RESUME_TOOL_NAME = "anyuse_routine_resume"
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_MD = re.compile(r"[`*_#<>\[\]|\\]")


def plain(text: Any, limit: int = 300) -> str:
    """One inert line: no control chars, no markdown/HTML structure, bounded."""
    out = _MD.sub("", _CTRL.sub(" ", str(text or ""))).replace("---", "-").strip()
    return out[:limit]


def _prefix(manifest: Mapping[str, Any]) -> str:
    return "routine" if manifest.get("kind") == "routine" else "skill"


def tool_name(manifest: Mapping[str, Any]) -> str:
    """``routine__<name>`` / ``skill__<name>`` (mcpserver/tool_names.py)."""
    from anyuse.mcpserver.tool_names import joined
    return joined(_prefix(manifest), manifest["name"])


def legacy_tool_name(manifest: Mapping[str, Any]) -> str:
    """The older dotted ``routine.<name>`` / ``skill.<name>``; still callable as an alias."""
    return f"{_prefix(manifest)}.{manifest['name']}"


def _counts(manifest: Mapping[str, Any]) -> Dict[str, int]:
    counts = {"proved": 0, "replay": 0, "agent": 0, "human": 0}
    for step in manifest["steps"]:
        counts[step["mode"]] = counts.get(step["mode"], 0) + 1
    return counts


def output_schema(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    outs = {k: {kk: vv for kk, vv in v.items() if kk != "from"}
            for k, v in dict(manifest.get("outputs") or {}).items()}
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": [
                "completed", "handed_off", "paused", "failed", "refused",
                "needs_reconcile", "cancelled"]},
            "run_id": {"type": "string"},
            "manifest_digest": {"type": "string", "const": manifest_digest(manifest)},
            "outputs": object_schema(outs),
            "handoff": {"type": "object", "properties": {
                "reason": {"type": "string"}, "next_step": {"type": "object"},
                "head": {"type": "string"}}},
            "receipts": {"type": "array", "items": {"type": "object"}},
            "error": {"type": "string"},
            # human inbox (docs/HUMAN_INBOX.md): the card for this stop, and
            # whether the person was asked (elicitation) or it waits for the CLI
            "card": {"type": "object"},
            "needs_human": {"type": "object"},
            "completed_card": {"type": "string"},
            "for_card": {"type": "string"},
        },
        "required": ["status", "run_id", "manifest_digest"],
        "additionalProperties": False,
    }


def mcp_tool(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """The MCP ``Tool`` payload (``name``/``description``/``inputSchema``/...)."""
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("invalid manifest: " + "; ".join(errors[:5]))
    digest = manifest_digest(manifest)
    c = _counts(manifest)
    human = c["human"]
    return {
        "name": tool_name(manifest),
        "aliases": [legacy_tool_name(manifest)],
        # trusted enums/counts only — no free text from the manifest
        "description": (
            f"Run AnyUse {manifest['kind']} {manifest['name']} v{manifest['version']}: "
            f"{len(manifest['steps'])} step(s) ({c['proved']} proved, {c['replay']} "
            f"replay, {c['agent']} agent, {human} human). "
            + ("It may return status handed_off; continue with "
               f"{RESUME_TOOL_NAME}. " if human or c["agent"] else "")
            + f"Manifest {digest[:12]}."),
        "inputSchema": object_schema(dict(manifest.get("inputs") or {})),
        "outputSchema": output_schema(manifest),
        "_meta": {"anyuse": {"manifest_schema": MANIFEST_SCHEMA,
                             "manifest_digest": digest,
                             "name": manifest["name"],
                             "version": manifest["version"]}},
    }


RESUME_TOOL = {
    "name": RESUME_TOOL_NAME,
    "description": ("Resume an AnyUse skill/routine run that returned handed_off or "
                    "paused. Preconditions are re-checked against fresh state; completed "
                    "steps are never re-run; an uncertain write is never retried "
                    "without a human reconcile."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "maxLength": 64},
            "expected_head": {"type": "string", "maxLength": 64},
            "human": {"type": "object", "properties": {
                "step": {"type": "string", "maxLength": 64},
                "outputs": {"type": "object"},
                "accept_proposal": {"type": "boolean"}}},
            "approval": {"type": "object", "properties": {
                "step": {"type": "string", "maxLength": 64},
                "inputs_digest": {"type": "string", "maxLength": 64}}},
            "reconcile": {"type": "object", "properties": {
                "step": {"type": "string", "maxLength": 64},
                "outcome": {"type": "string", "enum": ["completed", "not_done"]},
                "outputs": {"type": "object"}}},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
}


def _type_label(spec: Mapping[str, Any]) -> str:
    t = spec.get("type")
    if t == "array":
        return f"array of {_type_label(spec.get('items') or {})}"
    return str(t)


def skill_md(manifest: Mapping[str, Any]) -> str:
    """SKILL.md (Claude Code / agent skill format) pointing at the MCP tool."""
    tool = mcp_tool(manifest)          # validates
    digest = tool["_meta"]["anyuse"]["manifest_digest"]
    name = manifest["name"]
    lines: List[str] = [
        "---",
        f"name: {name}",
        f"description: {tool['description']}",
        "---",
        "",
        f"# {name} v{manifest['version']}",
        "",
        f"Generated from AnyUse manifest `{digest}` ({MANIFEST_SCHEMA}). Do not edit;",
        "regenerate from the manifest. This file is documentation: the MCP tool below",
        "is the only way this skill executes.",
        "",
        f"Owner summary (untrusted text, for humans): {plain(manifest.get('summary'))}",
        "",
        "## How to run",
        "",
        f"Call the MCP tool `{tool['name']}` with the inputs below. Do not reproduce",
        "the steps yourself. If it returns `handed_off`, show `handoff.next_step` to",
        f"the user and continue with `{RESUME_TOOL_NAME}` using the returned `run_id`",
        "and `handoff.head`.",
        "",
        "## Inputs",
        "",
    ]
    inputs = dict(manifest.get("inputs") or {})
    lines += [f"- `{k}`: {_type_label(v)}" for k, v in inputs.items()] or ["- none"]
    lines += ["", "## Outputs", ""]
    outs = dict(manifest.get("outputs") or {})
    lines += [f"- `{k}`: {_type_label(v)} (from step `{v['from']['step']}`)"
              for k, v in outs.items()] or ["- none"]
    lines += ["", "## Steps", "",
              "| # | step | mode | evidence | effects | approval | autonomy |",
              "|---|---|---|---|---|---|---|"]
    for i, step in enumerate(manifest["steps"], 1):
        eff = step["effects"]
        lines.append(
            f"| {i} | `{step['id']}` | {step['mode']} | {step['evidence']['status']} | "
            f"{eff['class']}{'' if eff['reversible'] else ', irreversible'} | "
            f"{'required' if (step.get('approval') or {}).get('required') else 'no'} | "
            f"{(step.get('autonomy') or {}).get('level', 'rule')} |")
    handoffs = [s for s in manifest["steps"] if s["mode"] in ("human", "agent")]
    if handoffs:
        lines += ["", "## Hand-off points", ""]
        for step in handoffs:
            text = step["action"].get("instruction") or (
                f"choose one of {step['action'].get('choose_from')}")
            lines.append(f"- `{step['id']}`: {plain(text)}")
    bindings = []
    for step in manifest["steps"]:
        for name, b in dict(step.get("bindings") or {}).items():
            if b["kind"] == "derived":
                bindings.append(f"- `{b['step']}.{b['output']}` -> `{step['id']}.{name}`")
            elif b["kind"] == "input":
                bindings.append(f"- input `{b['name']}` -> `{step['id']}.{name}`")
    if bindings:
        lines += ["", "## Bindings (explicit; nothing is matched by name)", ""] + bindings
    transfers = manifest.get("data_transfers") or []
    lines += ["", "## Cross-app data transfers", ""]
    lines += [f"- {t['from_app']} -> {t['to_app']}: {', '.join(t['fields'])} "
              f"({plain(t['purpose'], 120)})" for t in transfers] or ["- none"]
    lines += ["", "## Limits", "",
              "- Proved steps are proved only for the inputs their proof checked; see",
              "  `explain_workflow` for each compiled tool.",
              "- A model may choose among listed candidates on agent steps only; it",
              "  cannot bind values, approve, skip a step or declare success.",
              "- Irreversible steps are never repeated to prove they happened.",
              ""]
    return "\n".join(lines)
