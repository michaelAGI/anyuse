"""VM-only, exact-window visual evidence for REA investigations.

Pixels can help an agent propose an observation contract. A successful capture
is evidence, never task verification: this module has no model, actor or proof
promotion. It needs the guest's existing Screen Recording grant, not AX access.
Imports do not load native frameworks, inspect apps or photograph the desktop.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from numbers import Real
import os
from pathlib import Path
import struct
import subprocess
import time

from . import rea_mapping as R

VISUAL = "anyuse.rea-visual/1"
WINDOWS = "anyuse.rea-visual-windows/1"
MAX_WINDOWS = 128
MAX_TITLE_BYTES = 4096
MAX_PNG_BYTES = 64 * 1024**2
CAPTURE_TIMEOUT = 15
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class Unavailable(ValueError):
    """Capture was refused; do not request permissions or try another window."""


def _native_number(value, *, integer: bool = False):
    """Normalize bridged numeric subclasses without accepting coercible text.

    Quartz dictionaries can contain subclasses rather than builtin int/float.
    Strict identity checks consume only builtins after this native boundary.
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        raise Unavailable("guest window metadata contains a non-numeric value")
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite number")
        if integer:
            result = int(value)
            if value != result:
                raise ValueError("fractional integer metadata")
            return result
        return number
    except (OverflowError, TypeError, ValueError) as error:
        raise Unavailable("guest window metadata contains an invalid numeric value") from error


class Backend:
    """Lazy native boundary; tests replace this class with inert observations."""
    def __init__(self):
        R.assert_guest()
        import AppKit
        import Quartz
        self.AppKit, self.Quartz = AppKit, Quartz

    def inspect(self, bundle_id: str) -> dict:
        q = self.Quartz
        session = q.CGSessionCopyCurrentDictionary()
        if (not session or session.get("CGSSessionScreenIsLocked") or
                not session.get(q.kCGSessionOnConsoleKey) or
                session.get(q.kCGSessionUserIDKey) != os.getuid()):
            raise Unavailable("guest desktop is locked or its console session is unavailable")
        # Preflight only. CGRequestScreenCaptureAccess is deliberately never used.
        if not q.CGPreflightScreenCaptureAccess():
            raise Unavailable("guest process has no existing Screen Recording grant")
        apps = []
        for app in self.AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id):
            url, launched = app.bundleURL(), app.launchDate()
            apps.append({"bundle_id": str(app.bundleIdentifier() or ""),
                         "bundle": str(url.path()) if url else None,
                         "pid": int(app.processIdentifier()),
                         "launch_time": float(launched.timeIntervalSince1970()) if launched else None,
                         "terminated": bool(app.isTerminated())})
        windows = []
        info = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly |
                                          q.kCGWindowListExcludeDesktopElements, q.kCGNullWindowID)
        if info is None:
            raise Unavailable("guest window information is unavailable")
        for row in info:
            bounds = row.get(q.kCGWindowBounds) or {}
            windows.append({"id": _native_number(row.get(q.kCGWindowNumber), integer=True),
                            "pid": _native_number(row.get(q.kCGWindowOwnerPID), integer=True),
                            "title": str(row.get(q.kCGWindowName) or ""),
                            "layer": _native_number(row.get(q.kCGWindowLayer), integer=True),
                            "on_screen": bool(row.get(q.kCGWindowIsOnscreen)),
                            "alpha": _native_number(row.get(q.kCGWindowAlpha)),
                            "bounds": {k: _native_number(bounds.get(k))
                                       for k in ("X", "Y", "Width", "Height")}})
        return {"session": {"uid": int(session[q.kCGSessionUserIDKey]), "on_console": True},
                "apps": apps, "windows": windows}

    def capture(self, argv: list[str], *, timeout=CAPTURE_TIMEOUT):
        if not _number(timeout) or not 0 < timeout <= CAPTURE_TIMEOUT:
            raise ValueError("bounded capture timeout required")
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, check=False)


def _number(value) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _process_identity(state: dict, root: Path, bundle_id: str) -> dict:
    """Common exact-process and console validation for discovery and capture."""
    session = state.get("session")
    if (not isinstance(session, dict) or type(session.get("uid")) is not int or
            session["uid"] < 0 or session.get("on_console") is not True):
        raise Unavailable("guest console session identity is incomplete")
    apps = state.get("apps", [])
    if not isinstance(apps, list) or len(apps) != 1:
        raise Unavailable("need one unambiguous running target app process")
    app = apps[0]
    if (not isinstance(app, dict) or app.get("terminated") is not False or
            app.get("bundle_id") != bundle_id or not isinstance(app.get("bundle"), str) or
            Path(app["bundle"]).resolve(strict=True) != root or
            type(app.get("pid")) is not int or app["pid"] <= 0 or
            not _number(app.get("launch_time")) or app["launch_time"] <= 0):
        raise Unavailable("running app does not match the exact target bundle/process")
    return {"session": session, "process": app}


def _visible_windows(state: dict, pid: int, title: str | None = None, *, window_layer=0):
    """Yield valid visible ordinary windows; never infer identity from pixels."""
    for row in state.get("windows", []):
        if (not isinstance(row, dict) or row.get("pid") != pid or
                type(row.get("layer")) is not int or
                (window_layer is not None and row.get("layer") != window_layer) or row.get("on_screen") is not True or
                not _number(row.get("alpha")) or row["alpha"] <= 0):
            continue
        if title is not None and row.get("title") != title:
            continue
        bounds = row.get("bounds")
        if (type(row.get("id")) is not int or row["id"] <= 0 or
                not isinstance(row.get("title"), str) or
                not isinstance(bounds, dict) or set(bounds) != {"X", "Y", "Width", "Height"} or
                not all(_number(n) for n in bounds.values()) or
                not 0 < bounds["Width"] <= 32768 or not 0 < bounds["Height"] <= 32768):
            raise Unavailable("target window metadata is incomplete or invalid")
        yield row


def _identity(state: dict, root: Path, bundle_id: str, title: str | None, *, window_layer=0) -> dict:
    """Select by exact app identity and visible window metadata, without AX."""
    identity = _process_identity(state, root, bundle_id)
    windows = list(_visible_windows(state, identity["process"]["pid"], title, window_layer=window_layer))
    if len(windows) != 1:
        raise Unavailable("need one unambiguous visible target window; an exact title may scope it")
    return {**identity, "window": windows[0]}


def _image(path: Path) -> dict:
    size = path.stat().st_size
    if not 45 <= size <= MAX_PNG_BYTES:
        raise ValueError("screenshot is missing, empty or exceeds the PNG budget")
    with path.open("rb") as stream:
        header = stream.read(33)
        stream.seek(-12, 2)
        tail = stream.read()
    if (header[:8] != _PNG_MAGIC or header[8:16] != b"\0\0\0\rIHDR" or
            tail != b"\0\0\0\0IEND\xaeB`\x82"):
        raise ValueError("screenshot is not a complete PNG")
    width, height = struct.unpack(">II", header[16:24])
    if not 0 < width <= 32768 or not 0 < height <= 32768:
        raise ValueError("screenshot has invalid pixel dimensions")
    return {"file": path.name, "sha256": R._file_hash(path), "bytes": size,
            "width": width, "height": height}


def _log(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value or "")[:65536]


def _artifacts(folder: Path) -> list[dict]:
    rows = []
    for path in sorted(folder.iterdir()):
        if path.is_file():
            size = path.stat().st_size
            rows.append({"file": path.name, "bytes": size,
                         "sha256": R._file_hash(path) if size <= MAX_PNG_BYTES else None,
                         "oversized": size > MAX_PNG_BYTES})
    return rows


def _output(bundle: str, out: str) -> tuple[Path, Path]:
    """The public entry points must VM-guard before calling this IO helper."""
    root = Path(bundle).resolve(strict=True)
    folder = Path(out).resolve()
    if folder.is_relative_to(root):
        raise ValueError("visual evidence must be outside the target app bundle")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    return root, folder


def inspect(bundle: str, out: str, *, window_layer=0) -> dict:
    """Persist exact native window captions for an agent, without taking pixels.

    This read-only metadata discovery requires the existing Screen Recording
    grant so native titles are available. It needs neither AX nor app activation.
    A listing is a bounded snapshot; later capture must recheck the exact title.
    """
    R.assert_guest()  # before target reads, writes or native framework imports
    _check_layer(window_layer, allow_all=True)
    root, folder = _output(bundle, out)
    started = time.monotonic()
    receipt = {"format": WINDOWS, "status": "error", "evidence_only": True,
               "proof_created": False, "verified": False, "actuated": False,
               "bundle": str(root), "target": None, "identity": None, "windows": [],
               "window_limit": MAX_WINDOWS, "window_count": 0, "truncated": False,
               "error": None, "observed_at": datetime.now(timezone.utc).isoformat(),
               "receipt": str(folder / "windows.json")}
    try:
        target = receipt["target"] = R.fingerprint(root)
        state = Backend().inspect(target["bundle_id"])
        identity = receipt["identity"] = _process_identity(state, root, target["bundle_id"])
        for window in _visible_windows(state, identity["process"]["pid"], window_layer=window_layer):
            receipt["window_count"] += 1
            if len(receipt["windows"]) >= MAX_WINDOWS:
                receipt["truncated"] = True
                continue
            if len(window["title"].encode("utf-8")) > MAX_TITLE_BYTES:
                raise Unavailable("native window caption exceeds the metadata budget")
            receipt["windows"].append({k: window[k] for k in
                                       ("id", "pid", "title", "layer", "on_screen", "alpha", "bounds")})
        current = R.fingerprint(root)
        if current != target:
            receipt["after_target"] = current
            raise Unavailable("target app fingerprint changed during window discovery")
        receipt["status"] = "observed"
    except Unavailable as error:
        receipt["status"] = "unavailable"
        receipt["error"] = {"type": type(error).__name__, "message": _log(error)}
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": _log(error)}
    receipt["elapsed_s"] = round(time.monotonic() - started, 4)
    R.write_json(folder / "windows.json", receipt)
    return receipt


def _check_layer(value, *, allow_all=False):
    if not (allow_all and value is None) and (type(value) is not int or not 0 <= value <= 100):
        raise ValueError("window layer must be an explicit integer from 0 to 100")


def capture(bundle: str, out: str, *, window_title: str | None = None, window_layer=0) -> dict:
    """Persist a local exact-window PNG/receipt, or an unavailable/error receipt.

    No launch, activation, consent request, retry, display-wide capture or fallback
    is performed. Successful pixels still require an independent task observer.
    """
    R.assert_guest()  # before any target read, output write or platform inspection
    _check_layer(window_layer)
    if window_title is not None and (not isinstance(window_title, str) or not window_title or
                                     "\0" in window_title or len(window_title.encode("utf-8")) > 4096):
        raise ValueError("window title must be nonempty bounded exact text")
    root, folder = _output(bundle, out)
    started = time.monotonic()
    receipt = {"format": VISUAL, "status": "error", "evidence_only": True,
               "verified": False, "actuated": False, "bundle": str(root),
               "target": None, "identity": None, "after_identity": None, "image": None,
               "window_title": window_title, "run": None, "error": None,
               "started_at": datetime.now(timezone.utc).isoformat(),
               "capture_started_at": None, "capture_finished_at": None,
               "receipt": str(folder / "visual.json")}
    try:
        target = receipt["target"] = R.fingerprint(root)
        backend = Backend()
        identity = receipt["identity"] = _identity(backend.inspect(target["bundle_id"]), root,
                                                  target["bundle_id"], window_title, window_layer=window_layer)
        image = folder / "window.png"
        # -o excludes the window shadow so bounds-to-image geometry is meaningful.
        argv = ["/usr/sbin/screencapture", "-x", "-o", "-l", str(identity["window"]["id"]),
                "-t", "png", str(image)]
        receipt["run"] = {"argv": argv, "timeout_s": CAPTURE_TIMEOUT,
                          "returncode": None, "timed_out": False}
        try:
            receipt["capture_started_at"] = datetime.now(timezone.utc).isoformat()
            run = backend.capture(argv)
            receipt["run"]["returncode"] = run.returncode
            (folder / "capture.stdout").write_text(_log(run.stdout))
            (folder / "capture.stderr").write_text(_log(run.stderr))
        except subprocess.TimeoutExpired as error:
            receipt["run"]["timed_out"] = True
            (folder / "capture.stdout").write_text(_log(error.stdout))
            (folder / "capture.stderr").write_text(_log(error.stderr))
            raise ValueError("window capture exceeded its 15 second timeout") from error
        finally:
            receipt["capture_finished_at"] = datetime.now(timezone.utc).isoformat()
        if run.returncode != 0:
            raise ValueError(f"window capture failed with exit code {run.returncode}")
        after = receipt["after_identity"] = _identity(backend.inspect(target["bundle_id"]), root,
                                                      target["bundle_id"], window_title, window_layer=window_layer)
        if after != identity:
            raise Unavailable("guest session, process or window changed during capture")
        current = R.fingerprint(root)
        if current != target:
            receipt["after_target"] = current
            raise Unavailable("target app fingerprint changed during capture")
        receipt["image"] = _image(image)
        bounds = identity["window"]["bounds"]
        receipt["image"]["pixels_per_point"] = {
            "x": receipt["image"]["width"] / bounds["Width"],
            "y": receipt["image"]["height"] / bounds["Height"]}
        receipt["status"] = "captured"
    except Unavailable as error:
        receipt["status"] = "unavailable"
        receipt["error"] = {"type": type(error).__name__, "message": _log(error)}
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": _log(error)}
    receipt["elapsed_s"] = round(time.monotonic() - started, 4)
    # Retain partial capture files on failure, but never label them a valid image.
    receipt["artifacts"] = _artifacts(folder)
    R.write_json(folder / "visual.json", receipt)
    return receipt
