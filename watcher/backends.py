"""Text refinement backend for historical workflow composition."""
import asyncio
import logging
from typing import Optional

from anyuse.execution.model_adapter import ModelAdapter, credential_status

logger = logging.getLogger(__name__)


class ObserverModel:
    """Text backend interface. Unsupported generation uses the basic fallback."""

    def generate_text_sync(self, prompt: str, max_tokens: int = 2000,
                           json_mode: bool = False) -> str:
        return ""

    async def generate_text(self, prompt: str, max_tokens: int = 2000,
                            json_mode: bool = False) -> str:
        return await asyncio.to_thread(self.generate_text_sync, prompt, max_tokens, json_mode)


class TextRefinementBackend(ObserverModel):
    """Text generation using the composer's historical visual-role configuration.

    Resolve provider and model together; an omitted model uses that role's
    configured name. The composer gates consent before selecting this backend.
    """

    def __init__(self, model: Optional[str] = None,
                 adapter: Optional[ModelAdapter] = None):
        self._adapter = adapter or ModelAdapter.for_role("visual", model=model)

    def generate_text_sync(self, prompt: str, max_tokens: int = 2000,
                           json_mode: bool = False) -> str:
        try:
            schema = {"type": "object", "additionalProperties": True} if json_mode else None
            result = self._adapter.complete(
                [{"role": "user", "content": prompt}], temperature=0.3,
                max_tokens=max_tokens, json_schema=schema, schema_name="watcher_text")
            return result.text if result.ok else ""
        except Exception as exc:
            logger.warning("Composer text generation failed: %s", exc)
            return ""

    @property
    def name(self) -> str:
        return f"{self._adapter.config.provider} ({self._adapter.config.model})"

    def is_available(self) -> bool:
        return credential_status(self._adapter.config) != "unset"
