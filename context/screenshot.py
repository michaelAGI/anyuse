"""anyuse context.screenshot — screen capture as PNG bytes (Phase 3 / 6.x).

The engine's eyes: ``ScreenshotCapture().capture()`` returns PNG bytes of the
current screen (fed to the cloud VLM); ``capture_base64()`` returns it base64-
encoded for an inline image part.

Uses the macOS ``screencapture`` CLI rather than ``CGDisplayCreateImage`` —
the CG call is deprecated on recent macOS (ScreenCaptureKit is the successor) and
the CLI is the stable, cross-version path. Either way, returning REAL pixels needs
Screen-Recording TCC permission for the host process; without it the call still
runs but the image is empty/desktop-only (the documented privacy/permission gate).
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile

_SCREENCAPTURE = "/usr/sbin/screencapture"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class ScreenshotCapture:
    """Capture the current screen as PNG bytes via macOS ``screencapture``.

    ``runner=`` is a test seam. It stands in for the
    ``subprocess.run`` call, so a test can exercise the whole capture path —
    argv construction, return-code handling, the temp-file read, PNG validation
    and the unlink — WITHOUT photographing the screen of whoever is using the
    machine. The fake is called as ``runner(cmd, path)`` and should write the
    bytes it wants returned to ``path``; it returns anything carrying
    ``returncode`` and ``stderr``. ``None`` means "really run screencapture",
    which is the production default and the only behaviour that ships.
    """

    def __init__(self, display: int | None = None, include_cursor: bool = False,
                 runner=None):
        # display: 1-based display index for -D; None = main display.
        self._display = display
        self._include_cursor = include_cursor
        self._runner = runner

    def build_command(self, path: str) -> list:
        """The exact argv that would be executed. Pure — safe to assert on."""
        cmd = [_SCREENCAPTURE, "-x", "-t", "png"]  # -x: silent (no shutter sound)
        if self._include_cursor:
            cmd.append("-C")  # -C includes the cursor; default = no cursor
        if self._display is not None:
            cmd += ["-D", str(self._display)]
        cmd.append(path)
        return cmd

    def capture(self) -> bytes:
        """Return PNG bytes of the current screen.

        Raises RuntimeError if screencapture fails or yields no data (most often
        the Screen-Recording permission has not been granted to this process).
        """
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            cmd = self.build_command(path)
            if self._runner is not None:
                proc = self._runner(cmd, path)
            else:
                proc = subprocess.run(cmd, capture_output=True, timeout=15)
            if proc.returncode != 0:
                raise RuntimeError(
                    "screencapture failed "
                    f"(rc={proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:200]}"
                )
            with open(path, "rb") as f:
                data = f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not data:
            raise RuntimeError(
                "screencapture produced no data — grant Screen Recording permission "
                "to this process (System Settings → Privacy & Security → Screen Recording)."
            )
        if not data.startswith(_PNG_MAGIC):
            raise RuntimeError("screencapture output is not a valid PNG")
        return data

    def capture_base64(self) -> str:
        """Return the screen as a base64-encoded PNG string (for an inline image part)."""
        return base64.b64encode(self.capture()).decode("ascii")
