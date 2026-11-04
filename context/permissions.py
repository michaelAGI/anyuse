"""macOS TCC permission REQUESTS (not just preflight reads).

The preflight checks elsewhere (CGPreflightScreenCaptureAccess / AXIsProcessTrusted)
only READ the current grant state — they never prompt and never register the app in
System Settings. These REQUEST variants do both: they pop the system prompt when the
permission is undetermined, and they add the app to the Privacy & Security list so the
user can toggle it even if they dismissed the prompt.

Run inside the AnyUse Driver bundle's python child (whose TCC "responsible process" is
the signed "AnyUse Driver" app), the prompt/grant attributes to that NAMED app — the
isolated identity — not Terminal and not python globally.

Each request is idempotent + non-nagging: it prompts only while the permission is
undetermined, and returns the current state silently once the user has decided.
"""
import logging

logger = logging.getLogger(__name__)


def request_screen_recording() -> bool:
    """Prompt for + register Screen Recording. Returns the current grant state."""
    try:
        import Quartz
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("request_screen_recording unavailable: %s", exc)
        return False


def request_accessibility(prompt: bool = True) -> bool:
    """Prompt for + register Accessibility (AXIsProcessTrustedWithOptions). Returns
    True iff this process is currently AX-trusted."""
    try:
        import ApplicationServices as A
        key = getattr(A, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
        return bool(A.AXIsProcessTrustedWithOptions({key: bool(prompt)}))
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("request_accessibility unavailable: %s", exc)
        return False


def request_actuation_permissions() -> dict:
    """Request the actuation permissions so macOS prompts + registers this app.

    Screen Recording + Accessibility are requested explicitly here; Automation
    (controlling System Events / other apps) prompts naturally on the first Apple
    Event the engine sends. Returns the post-request state for logging.
    """
    return {
        "screen_recording": request_screen_recording(),
        "accessibility": request_accessibility(prompt=True),
    }
