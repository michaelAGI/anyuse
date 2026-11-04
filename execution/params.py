"""Parameter inference — turn a recorded macro into a parameterized callable.

The compile-to-API step needs to know which literal values typed during a recording
are PARAMETERS (so "rename invoice.pdf to final.pdf" compiles to rename(file, to))
rather than fixed text. Two complementary methods:

  * generalize_from_traces(a, b) — DETERMINISTIC, no LLM. Two recordings of the same
    task with different literals: the positions whose typed value CHANGED are the
    params. The most reliable signal — used once a 2nd run exists.

  * propose_params(task, typed_values) — from ONE recording, hand the typed values
    to an LLM and let it propose which are slots. Gives candidates to the model
    rather than pattern-matching; a usable first guess that
    generalize_from_traces later confirms.

The LLM call is injectable (``llm_fn``) so the logic is unit-testable offline.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ParamSlot:
    name: str
    example: str            # the literal value seen in the recording
    kind: str = "text"      # text | file | number | email | credential | url
    confidence: float = 0.0


def infer_kind(value: str) -> str:
    """Coarse type guess for a literal value (drives credential redaction etc.)."""
    v = (value or "").strip()
    low = v.lower()
    if any(k in low for k in ("password", "secret", "token", "api_key", "apikey")):
        return "credential"
    if re.match(r"^https?://", low):
        return "url"
    if "@" in v and "." in v.split("@")[-1]:
        return "email"
    if re.match(r"^[-+]?\d+([.,]\d+)?$", v):  # before 'file' so 3.50 isn't a ".50" file
        return "number"
    if re.search(r"\.[A-Za-z]{1,5}$", v) and " " not in v:  # extension must be alphabetic
        return "file"
    return "text"


def generalize_from_traces(values_a: List[str], values_b: List[str]) -> List[ParamSlot]:
    """Two recordings of the SAME task with different inputs → the values that
    differ are the parameters. Deterministic, no LLM, confidence 1.0."""
    a = values_a or []
    b = values_b or []
    # Equal length is a PRECONDITION for position-wise diffing. Unequal length means
    # the traces are misaligned (an extra dialog / typed value), so a positional diff
    # would drop real params AND compare unrelated positions, manufacturing bogus
    # params at confidence 1.0. Refuse rather than answer confidently-wrong.
    if len(a) != len(b):
        logger.warning("generalize_from_traces: trace length mismatch (%d vs %d); refusing positional diff", len(a), len(b))
        return []
    slots: List[ParamSlot] = []
    for i, (va, vb) in enumerate(zip(a, b)):
        if va != vb:
            slots.append(ParamSlot(name=f"param_{i + 1}", example=va, kind=infer_kind(va), confidence=1.0))
    return slots


def _build_prompt(task: str, typed_values: List[str]) -> str:
    numbered = "\n".join(f"  {i + 1}. {v!r}" for i, v in enumerate(typed_values))
    return f"""A user recorded a task by demonstration. Decide which of the literal
values they typed are PARAMETERS (would change on another run) vs fixed text.

TASK: {task}

VALUES TYPED (in order):
{numbered}

Return JSON only:
{{"params": [{{"name": "short_snake_case", "example": "<one of the values above>",
  "kind": "text|file|number|email|credential|url", "confidence": 0.0-1.0}}]}}
Only include values that are genuinely variable (a filename, a recipient, an amount);
exclude fixed UI text / menu labels."""


def propose_params(task: str, typed_values: List[str],
                   llm_fn: Optional[Callable[[str], str]] = None) -> List[ParamSlot]:
    """LLM proposes parameter slots from one recording's typed values. ``llm_fn`` is
    an injectable ``(prompt) -> json_string`` (defaults to Gemini Flash)."""
    if not typed_values:
        return []
    fn = llm_fn or _default_llm
    try:
        data = _loads_tolerant(fn(_build_prompt(task, typed_values)))
    except Exception as exc:
        logger.warning("propose_params LLM call failed: %s", exc)
        return []
    # Validate shape: the LLM can return a bare array, a non-list 'params', or junk.
    # Without these guards data.get / item.get raise AttributeError outside the
    # try-block above, defeating graceful degradation.
    if not isinstance(data, dict):
        return []
    params = data.get("params")
    if not isinstance(params, list):
        return []
    out: List[ParamSlot] = []
    for item in params:
        if not isinstance(item, dict):
            continue
        val = item.get("example", item.get("value"))
        if val is None:
            continue
        out.append(ParamSlot(
            name=str(item.get("name") or "param"),
            example=str(val),
            kind=str(item.get("kind") or infer_kind(str(val))),
            confidence=float(item.get("confidence", 0.6) or 0.0),
        ))
    return out


def _default_llm(prompt: str) -> str:
    from anyuse.execution.model_adapter import ModelAdapter
    result = ModelAdapter.for_role("planner").complete(
        [{"role": "user", "content": prompt}], temperature=0.1,
        json_schema={
            "type": "object",
            "properties": {"params": {"type": "array", "items": {"type": "object"}}},
            "required": ["params"],
        },
        schema_name="workflow_params",
    )
    if not result.ok:
        raise RuntimeError(result.error or "parameter provider failed")
    return result.text or "{}"


def _loads_tolerant(text: str) -> object:  # may be dict OR list OR {} — caller validates
    if not text or not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        c = text.strip()
        if "```json" in c:
            c = c.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in c:
            c = c.split("```", 1)[1].split("```", 1)[0].strip()
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            return {}
