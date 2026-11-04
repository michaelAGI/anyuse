"""anyuse context.macos — frontmost app / window / browser context (Phase 3).

What the engine knows about *where it is*: the frontmost application (always, via
NSWorkspace — no permission), and best-effort window title / browser URL / browser
JavaScript (via AppleScript, which needs Automation/Accessibility TCC — degrades
gracefully to None/"" when not granted).

Interface (from the engine's call sites):
  get_full_context() -> dict        (JSON-serializable; agent + cli)
  get_context_summary() -> str      (one-line; cli)
  get_frontmost_pid() -> int|None   (orchestrator / intervention)
  get_front_app_live() -> dict|None (recorder / computer-use loop: the app in front NOW)
  execute_javascript_in_browser(script, browser=None) -> str  (executor / intervention)

AppleScript runs only against an app whose Automation consent is already granted
(``automation_status``: ``AEDeterminePermissionToAutomateTarget`` with
askUserIfNeeded false). An undetermined consent would raise the macOS prompt, which
an unattended run cannot answer: it times out and macOS records a denial (for
example "Terminal möchte TextEdit steuern" from a replay's AppleScript step).
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_CHROMIUM_BROWSERS = {
    "Google Chrome", "Google Chrome Canary", "Brave Browser",
    "Microsoft Edge", "Arc", "Chromium", "Vivaldi",
}

#: bundle ids of the apps the context reads address over Apple Events
_KNOWN_BUNDLES = {
    "System Events": "com.apple.systemevents", "Finder": "com.apple.finder",
    "Safari": "com.apple.Safari", "Google Chrome": "com.google.Chrome",
    "Google Chrome Canary": "com.google.Chrome.canary", "Brave Browser": "com.brave.Browser",
    "Microsoft Edge": "com.microsoft.edgemac", "Arc": "company.thebrowser.Browser",
    "Chromium": "org.chromium.Chromium", "Vivaldi": "com.vivaldi.Vivaldi",
}

# ---------------------------------------------------------------------------
# Automation consent, read without asking
# ---------------------------------------------------------------------------

#: AEDeterminePermissionToAutomateTarget's answers (AppleEvents.h)
AE_GRANTED, AE_DENIED, AE_WOULD_ASK, AE_NOT_RUNNING = 0, -1743, -1744, -600
_AE_STATUS = {AE_GRANTED: "granted", AE_DENIED: "denied", AE_WOULD_ASK: "would_ask",
              AE_NOT_RUNNING: "not_running"}


def _ae_permission(bundle_id: str) -> Optional[int]:
    """``AEDeterminePermissionToAutomateTarget`` for ``bundle_id`` (any event class and id) with
    askUserIfNeeded false: the OSStatus, or None when it cannot be asked. Never shows a prompt."""
    import ctypes
    try:
        cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    except OSError:
        return None

    class _AEDesc(ctypes.Structure):
        _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]

    def four(code: str) -> int:
        return int.from_bytes(code.encode("ascii"), "big")
    try:
        cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc)]
        cs.AECreateDesc.restype = ctypes.c_int16
        cs.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
        cs.AEDisposeDesc.restype = ctypes.c_int16
        ask = cs.AEDeterminePermissionToAutomateTarget
        ask.argtypes = [ctypes.POINTER(_AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_ubyte]
        ask.restype = ctypes.c_int32
        data = bundle_id.encode("utf-8")
        desc = _AEDesc()
        if cs.AECreateDesc(four("bund"), data, len(data), ctypes.byref(desc)) != 0:
            return None
        try:
            return int(ask(ctypes.byref(desc), four("****"), four("****"), 0))
        finally:
            cs.AEDisposeDesc(ctypes.byref(desc))
    except Exception as e:  # noqa: BLE001 - no answer, never a prompt
        logger.debug("AEDeterminePermissionToAutomateTarget(%s) failed: %s", bundle_id, e)
        return None


#: the consent reader (tests replace it; the real one never prompts)
automation_probe: Callable[[str], Optional[int]] = _ae_permission


def automation_status(bundle_id: str) -> str:
    """Has the user already let this process automate ``bundle_id``? "granted", "denied",
    "would_ask" (undetermined: an Apple Event now would raise the prompt), "not_running" (macOS
    cannot tell until the app runs) or "unknown". Read without asking."""
    if not bundle_id:
        return "unknown"
    code = automation_probe(bundle_id)
    return _AE_STATUS.get(code, "unknown") if code is not None else "unknown"


def _bundle_id_of(app: str) -> str:
    """The bundle id of the app an AppleScript names (``tell application "X"``), or ""."""
    if app in _KNOWN_BUNDLES:
        return _KNOWN_BUNDLES[app]
    try:
        from AppKit import NSBundle, NSWorkspace
        ws = NSWorkspace.sharedWorkspace()
        for running in ws.runningApplications():
            if str(running.localizedName() or "") == app and running.bundleIdentifier():
                return str(running.bundleIdentifier())
        path = ws.fullPathForApplication_(app)
        bundle = NSBundle.bundleWithPath_(path) if path else None
        return str(bundle.bundleIdentifier() or "") if bundle is not None else ""
    except Exception as e:  # noqa: BLE001
        logger.debug("bundle id of %r not read: %s", app, e)
        return ""


#: resolves an app name to its bundle id (tests replace it)
bundle_resolver: Callable[[str], str] = _bundle_id_of

_APP_REF = re.compile(r'\b(?:application|app)\s+(id\s+)?"([^"]+)"', re.IGNORECASE)

# Strings and comments are consumed as whole tokens, so app names inside JavaScript or prose are not targets.
_APP_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|--[^\r\n]*|\(\*[\s\S]*?\*\)|\b(?:application|app)\b',
                        re.IGNORECASE)
_APP_END = re.compile(r'[ \t]*(?:$|[\r\n;)]|to\b)', re.IGNORECASE)


def applescript_targets(script: str) -> List[str]:
    """The bundle ids of the apps ``script`` names (``tell application "X"``, ``application id
    "…"``, any ``application "X"`` reference); an app whose bundle id cannot be resolved is kept by
    its name. Raises ValueError for targets expressed as variables or other unsupported expressions."""
    # Preserve strings, but blank comments while retaining line boundaries.
    code = _APP_TOKEN.sub(lambda m: re.sub(r"[^\r\n]", " ", m.group())
                          if m.group().startswith(("--", "(*")) else m.group(), script or "")
    out: List[str] = []
    for token in _APP_TOKEN.finditer(code):
        if token.group().lower() not in ("application", "app"):
            continue
        if re.match(r"\s+process\b", code[token.end():], re.IGNORECASE):
            continue                    # System Events' AX process object, not an Apple Event target
        reference = _APP_REF.match(code, token.start())
        if reference is None or not _APP_END.match(code, reference.end()):
            raise ValueError("the AppleScript application target cannot be preflighted; "
                             'use a literal app name or application id without an expression')
        is_id, target = reference.groups()
        bid = target if is_id else (bundle_resolver(target) or target)
        if bid not in out:
            out.append(bid)
    return out


def applescript_refusal(script: str) -> Optional[str]:
    """None when every app ``script`` addresses has already granted Automation consent, else why
    it must not run: an Apple Event to an app without consent raises the macOS prompt (or fails
    on a recorded denial). A script that addresses no app sends no event that needs consent."""
    try:
        targets = applescript_targets(script)
    except ValueError as exc:
        return str(exc)
    for target in targets:
        status = automation_status(target) if "." in target else "unknown"
        if status != "granted":
            return (f"Automation for {target} is not granted ({status}); AppleScript would "
                    f"raise the macOS consent prompt, so it is not used")
    return None


def _osascript(script: str, timeout: int = 8) -> Optional[str]:
    """Run an AppleScript via osascript; return stdout (stripped) or None on any
    failure (non-zero rc usually = Automation/Accessibility TCC not granted). Not run at
    all when an app it addresses has not granted Automation (``applescript_refusal``)."""
    refused = applescript_refusal(script)
    if refused:
        logger.debug("osascript skipped: %s", refused)
        return None
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout.decode("utf-8", "replace").strip()
        return out or None
    except Exception as e:  # noqa: BLE001 — best-effort context, never raise
        logger.debug("osascript failed: %s", e)
        return None


class MacOSContext:
    """Frontmost-app + window + browser context for the macOS desktop."""

    def get_frontmost_app(self, with_window: bool = True) -> Optional[Dict[str, Any]]:
        """The frontmost application (name/bundle_id/pid) via NSWorkspace (no TCC), and -
        ``with_window`` - its focused window's ``window_title`` through AX
        (``context/accessibility.focused_window_info``; Accessibility TCC): "" when the app
        has no focused window, None when AX could not answer (unknown)."""
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None
            out = {
                "name": str(app.localizedName() or ""),
                "bundle_id": str(app.bundleIdentifier() or ""),
                "pid": int(app.processIdentifier()),
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("get_frontmost_app failed: %s", e)
            return None
        if with_window:
            try:
                from anyuse.context.accessibility import focused_window_info
                info = focused_window_info(out["pid"])
            except Exception:  # noqa: BLE001
                info = None
            out["window_title"] = info["title"] if info is not None else None
        return out

    #: ``() -> front_application``-shaped dict; None: ``watcher.step_capture.front_application``
    front_reader: Optional[Callable[[], Dict[str, Any]]] = None

    def get_front_app_live(self) -> Optional[Dict[str, Any]]:
        """The app in front NOW: ``{name, bundle_id, pid, window_title, front_source}``, or None when
        no source named it. ``watcher.step_capture.front_application``: the system-wide AX focus
        first; on the main thread NSWorkspace right after a run-loop tick, cross-checked with the
        menu-bar owner; off it the window server's frontmost window, confirmed by that app's own
        ``AXFrontmost``. ``get_frontmost_app`` reads ``NSWorkspace.frontmostApplication`` without a
        tick, which in a long-lived process keeps the app that was in front when the process started
        (measured: still Terminal 6 s after TextEdit came forward)."""
        try:
            if self.front_reader is not None:
                f = self.front_reader()
            else:
                from anyuse.watcher.step_capture import front_application
                f = front_application()
        except Exception as e:  # noqa: BLE001 - no reading is no answer
            logger.debug("live front-app reading failed: %s", e)
            return None
        if not f or f.get("pid") is None:
            return None
        return {"name": str(f.get("app") or f.get("name") or ""), "bundle_id": str(f.get("bundle_id") or ""),
                "pid": int(f["pid"]), "window_title": f.get("window_title"),
                "front_source": f.get("front_source") or ""}

    def get_frontmost_pid(self, live: bool = False) -> Optional[int]:
        """The front app's pid; ``live``: from ``get_front_app_live`` (None when no source answers)."""
        app = self.get_front_app_live() if live else self.get_frontmost_app(with_window=False)
        return app.get("pid") if app else None

    @staticmethod
    def app_name_for_pid(pid: Optional[int]) -> str:
        """The localized name of the running app ``pid`` (looked up by pid, not the stale front
        read), or ""."""
        if pid is None:
            return ""
        try:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            return str(app.localizedName() or "") if app is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    #: the text roles ``get_focused_text`` reads
    TEXT_ROLES = ("AXTextArea", "AXTextField", "AXComboBox", "AXSearchField")

    def get_focused_text(self, pid: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """``{"role", "value", "app"}`` of the element with keyboard focus in app ``pid`` (the live
        front app when None) when it is a text element; None when it is not, when it is a password
        field, or when AX cannot answer. A few attribute reads."""
        app_name = ""
        if pid is None:
            front = self.get_front_app_live()
            if not front:
                return None
            pid, app_name = front["pid"], front.get("name") or ""
        try:
            from anyuse.context import accessibility as acc
            A = acc._ax()
            focused = acc._attr(A.AXUIElementCreateApplication(int(pid)), "AXFocusedUIElement")
            if focused is None:
                return None
            role = acc._s(acc._attr(focused, "AXRole"))
            subrole = acc._s(acc._attr(focused, "AXSubrole"))
            if role not in self.TEXT_ROLES or "Secure" in (subrole or "") or "Secure" in (role or ""):
                return None
            value = acc._attr(focused, "AXValue")
            return {"role": role, "value": "" if value is None else str(value), "app": app_name}
        except Exception as e:  # noqa: BLE001 - unreadable: no answer
            logger.debug("focused text not read: %s", e)
            return None

    def get_active_window_title(self) -> Optional[str]:
        """Front window title of the frontmost app (Accessibility/Automation TCC)."""
        return _osascript(
            'tell application "System Events"\n'
            '  set p to first application process whose frontmost is true\n'
            '  try\n'
            '    return name of front window of p\n'
            '  on error\n'
            '    return ""\n'
            '  end try\n'
            'end tell'
        )

    def get_browser_url(self, app_name: Optional[str] = None) -> Optional[str]:
        """URL of the active browser tab if the frontmost app (or ``app_name``, a caller's live
        reading) is a browser (Automation TCC, never asked)."""
        name = app_name if app_name is not None else (self.get_frontmost_app() or {}).get("name", "")
        if name == "Safari":
            return _osascript('tell application "Safari" to return URL of front document')
        if name in _CHROMIUM_BROWSERS:
            return _osascript(
                f'tell application "{name}" to return URL of active tab of front window'
            )
        return None

    def get_full_context(self) -> Dict[str, Any]:
        """Full desktop context as a JSON-serializable dict (best-effort fields)."""
        app = self.get_frontmost_app() or {}
        ctx: Dict[str, Any] = {
            "frontmost_app": app.get("name"),
            "bundle_id": app.get("bundle_id"),
            "pid": app.get("pid"),
        }
        wt = self.get_active_window_title()
        if wt:
            ctx["window_title"] = wt
        url = self.get_browser_url()
        if url:
            ctx["browser_url"] = url
        return ctx

    def get_context_summary(self) -> str:
        """One-line human summary of the current context."""
        c = self.get_full_context()
        parts = []
        if c.get("frontmost_app"):
            parts.append(f"app={c['frontmost_app']}")
        if c.get("window_title"):
            parts.append(f"window={c['window_title']!r}")
        if c.get("browser_url"):
            parts.append(f"url={c['browser_url']}")
        return " | ".join(parts) or "(no context)"

    def execute_javascript_in_browser(
        self, script: str, browser: Optional[str] = None
    ) -> str:
        """Run JavaScript in the active browser tab via AppleScript (Automation TCC).

        Returns the JS result as a string, or "" if it could not run (TCC not
        granted, "Allow JavaScript from Apple Events" off in Safari, or no browser).
        """
        b = browser or (self.get_frontmost_app() or {}).get("name") or "Google Chrome"
        esc = script.replace("\\", "\\\\").replace('"', '\\"')
        if b == "Safari":
            osa = f'tell application "Safari" to do JavaScript "{esc}" in front document'
        elif b in _CHROMIUM_BROWSERS:
            osa = (f'tell application "{b}" to execute active tab of front window '
                   f'javascript "{esc}"')
        else:
            return ""
        return _osascript(osa, timeout=20) or ""
