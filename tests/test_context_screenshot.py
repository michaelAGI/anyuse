"""anyuse context.screenshot — the capture path, WITHOUT photographing the screen.

This file used to call ``ScreenshotCapture().capture()`` for real, which runs
``/usr/sbin/screencapture`` and takes a picture of whoever's screen the suite is
running on.

It now drives the same code through the ``runner=`` seam with a fake that writes
canned bytes, so every branch — argv construction, a non-zero return code, the
temp-file read, the PNG-magic check, the empty-output permission message, the
unlink — is exercised with no capture and no TCC prompt.

The live capture stays available behind an explicit opt-in, because it is the
only thing that proves the real binary still behaves:

    ANYUSE_LIVE_SCREENSHOT_TEST=1 python test_context_screenshot.py

Without that variable set, nothing here touches the display. Allowlisted for the
guarded runner; the live branch cannot run under the guard anyway, because the
guard refuses to spawn ``screencapture`` at all.
"""

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.context.screenshot import ScreenshotCapture, _PNG_MAGIC, _SCREENCAPTURE

PNG = _PNG_MAGIC + b"\x00\x00\x00\rIHDR" + b"x" * 2000


class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def _fake_runner(payload=PNG, returncode=0, stderr=b"", record=None):
    """A stand-in for subprocess.run that writes ``payload`` instead of capturing."""

    def run(cmd, path):
        if record is not None:
            record.append((list(cmd), path))
        if payload is not None:
            with open(path, "wb") as f:
                f.write(payload)
        return _FakeProc(returncode, stderr)

    return run


def test_command_is_silent_and_png():
    """-x keeps the shutter sound off; the cursor is excluded unless asked for."""
    cmd = ScreenshotCapture().build_command("/tmp/x.png")
    assert cmd[0] == _SCREENCAPTURE
    assert "-x" in cmd and "-t" in cmd and "png" in cmd
    assert "-C" not in cmd, "the cursor must not be captured by default"
    assert cmd[-1] == "/tmp/x.png"

    cmd = ScreenshotCapture(display=2, include_cursor=True).build_command("/tmp/y.png")
    assert "-C" in cmd and cmd[cmd.index("-D") + 1] == "2"
    print("OK  screenshot: argv is a silent PNG capture; -C/-D only when asked")


def test_capture_returns_the_bytes_the_binary_wrote():
    record = []
    cap = ScreenshotCapture(runner=_fake_runner(record=record))
    data = cap.capture()
    assert data == PNG and data.startswith(_PNG_MAGIC)
    assert len(record) == 1 and record[0][0][0] == _SCREENCAPTURE
    print(f"OK  screenshot: capture() returned {len(data):,} PNG bytes (faked binary)")


def test_capture_base64_is_the_same_image():
    import base64
    cap = ScreenshotCapture(runner=_fake_runner())
    assert base64.b64decode(cap.capture_base64()) == PNG
    print("OK  screenshot: capture_base64() is the same image, base64-encoded")


def test_temp_file_is_always_removed():
    """The privacy-relevant property: the PNG does not survive the call."""
    record = []
    ScreenshotCapture(runner=_fake_runner(record=record)).capture()
    path = record[0][1]
    assert not os.path.exists(path), f"capture left {path} on disk"

    # …and also when the capture fails.
    record = []
    cap = ScreenshotCapture(runner=_fake_runner(payload=None, returncode=1,
                                                stderr=b"nope", record=record))
    try:
        cap.capture()
    except RuntimeError:
        pass
    assert not os.path.exists(record[0][1]), "a failed capture left its temp file behind"
    print("OK  screenshot: the temp PNG is unlinked on success AND on failure")


def test_failure_modes_raise_the_documented_errors():
    cap = ScreenshotCapture(runner=_fake_runner(payload=None, returncode=1, stderr=b"denied"))
    try:
        cap.capture()
        raise AssertionError("a non-zero return code must raise")
    except RuntimeError as e:
        assert "screencapture failed" in str(e) and "denied" in str(e)

    cap = ScreenshotCapture(runner=_fake_runner(payload=b""))
    try:
        cap.capture()
        raise AssertionError("empty output must raise")
    except RuntimeError as e:
        assert "Screen Recording" in str(e), e   # the actionable permission hint

    cap = ScreenshotCapture(runner=_fake_runner(payload=b"GIF89a-not-a-png"))
    try:
        cap.capture()
        raise AssertionError("non-PNG output must raise")
    except RuntimeError as e:
        assert "not a valid PNG" in str(e)
    print("OK  screenshot: rc!=0, empty output and non-PNG each raise their own message")


def test_module_performs_no_network_io():
    """Asserted, not assumed: the capture path has no way to transmit a picture."""
    src = (_HERE / "context" / "screenshot.py").read_text()
    for forbidden in ("requests", "urllib", "httpx", "socket", "aiohttp", "http.client"):
        assert forbidden not in src, f"screenshot.py must not import {forbidden}"
    print("OK  screenshot: no HTTP/socket client in the module — bytes go to the caller only")


def test_live_capture_is_opt_in():
    """The real binary, only when someone explicitly asks for it."""
    if os.environ.get("ANYUSE_LIVE_SCREENSHOT_TEST", "") != "1":
        print("SKIP screenshot: live capture is opt-in "
              "(ANYUSE_LIVE_SCREENSHOT_TEST=1) — the display was NOT photographed")
        return
    cap = ScreenshotCapture()
    try:
        data = cap.capture()
        assert data.startswith(_PNG_MAGIC) and len(data) > 1000
        print(f"OK  screenshot: LIVE capture returned {len(data):,} bytes")
    except RuntimeError as e:
        msg = str(e).lower()
        assert "screencapture" in msg or "permission" in msg or "screen recording" in msg
        print(f"OK  screenshot: LIVE capture hit the documented permission gate: {str(e)[:90]}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("=" * 60)
    print(f"anyuse context.screenshot — {len(tests)} tests, no screen touched")
    print("=" * 60)
    for t in tests:
        t()
    print("\nCONTEXT.SCREENSHOT TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
