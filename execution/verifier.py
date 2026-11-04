"""GoalVerifier — LLM goal verification (Gemini Flash) over a screenshot.

This is the AI-judgment verifier that the Agent uses to decide whether a task
"looks done" from the final screen. It is the LLM-based complement to the
deterministic, no-LLM drift gate (AX/OCR/pixel) — used where a fuzzy "did the
goal get achieved" judgment is wanted, not a byte-level state comparison.

Previously imported from a nonexistent ``agent.verifier`` package; this provides
the real implementation. Side-effect-free and injectable (gemini_client) so it is
unit-testable without a network call.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from anyuse.execution.model_adapter import ModelAdapter, image_data_url

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Structured verdict from a goal check."""
    is_success: bool = False
    confidence: float = 0.0
    result: str = "uncertain"  # success | partial | failed | uncertain
    explanation: str = ""
    evidence: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    #: the model calls this verdict cost, read from the process counter
    #: (``model_adapter.process_call_count``), never self-reported
    llm_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result,
            "is_success": self.is_success,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "evidence": self.evidence,
            "suggestions": self.suggestions,
            "llm_calls": self.llm_calls,
        }


# Back-compat alias: legacy callers imported ``VerificationResponse``.
VerificationResponse = VerificationResult


class GoalVerifier:
    """Verify goal achievement from a screenshot using Gemini Flash."""

    def __init__(self, api_key: Optional[str] = None, model: str = "",
                 adapter: Optional[ModelAdapter] = None, gemini_client=None):
        self._adapter = adapter or ModelAdapter.for_role(
            "visual", **({"model": model} if model else {})
        )
        self._legacy_client = gemini_client

    def _ask(self, screenshot_bytes: bytes, prompt: str) -> str:
        if self._legacy_client is not None:
            # a model call outside the adapter: counted at the one process counter
            from anyuse.execution.model_adapter import note_external_model_call
            note_external_model_call("gemini-sdk", self._adapter.config.model, feature="verifier")
            resp = self._legacy_client.models.generate_content(
                model=self._adapter.config.model, contents=prompt,
                config={"temperature": 0.1},
            )
            return resp.text if getattr(resp, "text", None) else ""
        result = self._adapter.complete(
            [{"role": "user", "content": prompt}], temperature=0.1,
            images=[image_data_url(screenshot_bytes)] if screenshot_bytes else (),
        )
        if not result.ok:
            raise RuntimeError(result.error or "verification provider failed")
        return result.text

    def verify_goal(
        self,
        screenshot_bytes: bytes,
        goal: str,
        steps_completed: Optional[List[str]] = None,
        current_state: str = "",
    ) -> VerificationResult:
        prompt = f"""You verify whether a computer task was achieved, from a screenshot.

GOAL: {goal}
STEPS REPORTED: {', '.join(steps_completed or []) or '(none)'}
EXTRA CONTEXT: {current_state[:500]}

Look at the screenshot and judge whether the GOAL is achieved. Respond with JSON only:
{{
  "result": "success" | "partial" | "failed" | "uncertain",
  "is_success": true|false,
  "confidence": 0.0-1.0,
  "explanation": "what you see on screen and why",
  "evidence": ["..."],
  "suggestions": ["what to try if not achieved"]
}}"""
        from anyuse.execution.model_adapter import calls_since, process_call_count
        mark = process_call_count()
        try:
            text = self._ask(screenshot_bytes, prompt)
        except Exception as exc:
            logger.warning("GoalVerifier.verify_goal model call failed: %s", exc)
            return VerificationResult(result="uncertain", explanation=f"verifier error: {exc}",
                                      llm_calls=calls_since(mark))
        out = self._parse(text)
        out.llm_calls = calls_since(mark)
        return out

    def quick_check(self, screenshot_bytes: bytes, question: str) -> Tuple[bool, float, str]:
        prompt = f"""Answer this yes/no question about the screenshot. JSON only:
QUESTION: {question}
{{"answer": true|false, "confidence": 0.0-1.0, "explanation": "..."}}"""
        try:
            text = self._ask(screenshot_bytes, prompt)
            data = _loads_tolerant(text)
            return bool(data.get("answer", False)), float(data.get("confidence", 0.0)), str(data.get("explanation", ""))
        except Exception as exc:
            logger.warning("GoalVerifier.quick_check failed: %s", exc)
            return False, 0.0, f"verifier error: {exc}"

    @staticmethod
    def _parse(text: str) -> VerificationResult:
        data = _loads_tolerant(text)
        if not data:
            return VerificationResult(result="uncertain", explanation=f"unparseable: {text[:200]}")
        result = str(data.get("result", "uncertain")).lower()
        is_success = bool(data.get("is_success", result == "success"))
        return VerificationResult(
            is_success=is_success,
            confidence=float(data.get("confidence", 0.0) or 0.0),
            result=result,
            explanation=str(data.get("explanation", "")),
            evidence=list(data.get("evidence", []) or []),
            suggestions=list(data.get("suggestions", []) or []),
        )


def _loads_tolerant(text: str) -> Dict[str, Any]:
    """Parse JSON, tolerating markdown code fences."""
    if not text or not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}
