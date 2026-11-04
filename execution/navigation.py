"""Compiled container navigation — never a free-form URL tool."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from anyuse.execution.api_step import RequestTemplate
from anyuse.execution.request_policy import destination_refusal, exact_origin_allowlist
from anyuse.execution.workflow_schema import template_digest


@dataclass
class CompiledNavigationTemplate:
    """A proved URL template executable only by an owned container context."""

    name: str
    request: RequestTemplate
    proof_id: str
    proof_template_digest: str = ""
    approved_origins: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        return {"name": self.name, "request": self.request.to_dict(),
                "approved_origins": list(self.approved_origins)}

    def current_digest(self) -> str:
        return template_digest(self.payload())

    def pin(self) -> "CompiledNavigationTemplate":
        self.proof_template_digest = self.current_digest()
        return self


def navigate_to(template: CompiledNavigationTemplate, inputs: Dict[str, Any], context) -> dict:
    """Navigate a container with a compiled template.

    There is intentionally no ``url`` argument.  Callers supply only the
    template's typed inputs; changing the destination requires compilation and
    a new proof pin.
    """
    if not getattr(context, "owned_container", False):
        return {"success": False, "outcome_kind": "terminal_refusal",
                "error": "compiled navigation requires an owned container context"}
    if not template.proof_id or template.proof_template_digest != template.current_digest():
        return {"success": False, "outcome_kind": "terminal_refusal",
                "error": "compiled navigation template is not pinned to its proof"}
    if template.request.method.upper() != "GET" or template.request.body:
        return {"success": False, "outcome_kind": "terminal_refusal",
                "error": "compiled navigation must be a body-free GET"}
    problems = template.request.problems()
    if problems:
        return {"success": False, "outcome_kind": "terminal_refusal",
                "error": "compiled navigation refused: " + "; ".join(problems)}
    try:
        url, _headers, _body = template.request.render(inputs)
    except (KeyError, ValueError) as exc:
        return {"success": False, "outcome_kind": "terminal_refusal", "error": str(exc)}
    allowed = exact_origin_allowlist(template.request.url, template.approved_origins)
    refusal = destination_refusal(url, allowed)
    if refusal:
        return {"success": False, "outcome_kind": "terminal_refusal", "error": refusal}
    result = context.navigate(url)
    return {"success": bool(result), "outcome_kind": "success" if result else "incomplete",
            "template": template.name, "proof_id": template.proof_id,
            "url": url}
