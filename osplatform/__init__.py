"""osplatform — the OS abstraction seam (perception + actuation + native actions).

Named ``osplatform`` (not ``platform``) on purpose: a top-level ``platform``
package would shadow the Python stdlib ``platform`` module under the flat-import
path, the same name-collision that broke ``mcp`` → ``mcpserver``.

Everything ABOVE this seam (the state-graph map, record/replay, learn/persist,
verify-logic, compile-to-API) is OS-agnostic. Only this layer is per-OS:
  * macOS  → osplatform.macos.MacOSPlatform (ApplicationServices AX + Vision + pyautogui)
  * Windows→ (future) UIA + Windows.Media.Ocr + COM

The Protocol is intentionally PROVISIONAL — extracted bottom-up from the working
macOS primitives, not authored top-down against zero implementations.
"""
from anyuse.osplatform.base import ABSTRACT_VERBS, Platform, Verb

__all__ = ["Platform", "Verb", "ABSTRACT_VERBS"]
