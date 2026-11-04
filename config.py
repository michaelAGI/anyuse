"""Top-level anyuse runtime config (Phase 3 / 6.1).

anyuse's engine modules use flat imports (``import config``) — they run with the
anyuse package dir on ``sys.path``, not as ``anyuse.execution.*``. This module is
the small shared settings surface those modules read: the Gemini API key + the
two model ids. (Per-subsystem config — watcher BatchWatcherConfig, 2FA
TwoFactorConfig, SSOConfig — lives with
each subsystem; this is ONLY the cross-cutting model/key settings.)

Everything is environment-overridable so deployment never hardcodes a key.
Provider and role selection is handled by execution/model_adapter.py; the
step recorder runs without a model.
"""

import os

from anyuse.settings import get as get_setting

# ── Gemini credentials ──────────────────────────────────────────────────────
# Resolve the key from the standard env names (the common convention, so a
# single GEMINI_API_KEY / GOOGLE_API_KEY in the environment serves both).
GEMINI_API_KEY: str = (
    os.environ.get("GEMINI_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or ""
)

# ── Model ids (env-overridable) ─────────────────────────────────────────────
# Flash: the legacy Gemini model setting used by control-plane callers.
GEMINI_MODEL_FLASH: str = get_setting("ANYUSE_GEMINI_MODEL_FLASH")
# Computer-use: the vision/computer-use model the executor drives for last-resort
# pixel actuation (_execute_visual, 6.2). Pin the exact id via env in deployment.
GEMINI_MODEL_COMPUTER_USE: str = get_setting("ANYUSE_GEMINI_MODEL_COMPUTER_USE")


def is_configured() -> bool:
    """True iff a Gemini API key is available (cloud perception/actuation works)."""
    return bool(GEMINI_API_KEY)
