"""Startup guard for AnyUse's offline unit-test process.

Loaded by Python from ``PYTHONPATH`` before a test module. It refuses desktop,
credential, browser and network entry points. Every refusal contains the
``DESKTOP GUARD`` marker so the runner treats a trip as a failed test.
"""
import importlib.abc
import os
import sys

MARK = "DESKTOP GUARD"

_BLOCKED_MODULES = {
    "AppKit", "Cocoa", "Quartz", "CoreGraphics", "ApplicationServices",
    "HIServices", "Vision", "AVFoundation", "ScreenCaptureKit", "CoreMedia",
    "CoreAudio", "AudioToolbox", "LocalAuthentication", "Security", "objc",
    "Foundation", "CoreServices", "LaunchServices",
    "pyautogui", "pynput", "mouseinfo", "pyscreeze", "pygetwindow", "pymsgbox",
    "pytweening", "sounddevice", "pyaudio", "mss", "rumps",
    "mlx", "mlx_whisper", "mlx_vlm", "playwright",
    # Model paths must remain provider-SDK-free.
    "google", "openai", "anthropic", "cerebras",
}
_DENY_BINARIES = {
    "screencapture", "osascript", "open", "say", "afplay", "cliclick",
    "tccutil", "automator", "shortcuts", "security", "caffeinate",
}
_DENY_SUBSTRINGS = ("anyuse.watcher.overlay", "watcher/overlay.py", "--serve")


class _Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED_MODULES:
            raise ImportError(f"{MARK}: import of {name!r} is refused")
        return None


sys.meta_path.insert(0, _Guard())


def _argv_text(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        return os.fsdecode(args) if not isinstance(args, str) else args
    try:
        return " ".join(os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in args)
    except Exception:
        return str(args)


def _refuse_if_desktop(args):
    text = _argv_text(args)
    tokens = {os.path.basename(t) for t in text.replace("=", " ").split()}
    if tokens & _DENY_BINARIES or any(s in text for s in _DENY_SUBSTRINGS):
        raise PermissionError(f"{MARK}: refusing to spawn {text[:140]!r}")


import subprocess  # noqa: E402

_orig_popen_init = subprocess.Popen.__init__


def _guarded_popen_init(self, args, *a, **kw):
    _refuse_if_desktop(args)
    return _orig_popen_init(self, args, *a, **kw)


subprocess.Popen.__init__ = _guarded_popen_init
_orig_system, _orig_os_popen = os.system, os.popen


def _guarded_system(cmd):
    _refuse_if_desktop(cmd)
    return _orig_system(cmd)


def _guarded_popen(cmd, *a, **kw):
    _refuse_if_desktop(cmd)
    return _orig_os_popen(cmd, *a, **kw)


os.system, os.popen = _guarded_system, _guarded_popen

for _name in (
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "posix_spawn", "posix_spawnp",
):
    _orig = getattr(os, _name, None)
    if _orig is None:
        continue

    def _make(orig):
        def guarded(*a, **kw):
            flattened = [str(x) for x in a if not isinstance(x, (dict, list, tuple))]
            flattened += [str(y) for x in a if isinstance(x, (list, tuple)) for y in x]
            _refuse_if_desktop(flattened)
            return orig(*a, **kw)
        return guarded

    setattr(os, _name, _make(_orig))

try:
    import ctypes
    _orig_cdll_init = ctypes.CDLL.__init__

    def _guarded_cdll_init(self, name, *a, **kw):
        if name and "/System/Library/Frameworks/" in str(name):
            raise OSError(f"{MARK}: refusing to load system framework {name!r}")
        return _orig_cdll_init(self, name, *a, **kw)

    ctypes.CDLL.__init__ = _guarded_cdll_init
except Exception:
    pass

try:
    import webbrowser

    def _no_open(*a, **kw):
        raise PermissionError(f"{MARK}: webbrowser.open is refused")

    webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = _no_open
except Exception:
    pass

# Unit tests are offline. Mock transports remain usable because this blocks only
# real socket connection entry points.
try:
    import socket
    import urllib.parse

    _original_socket_connect = socket.socket.connect
    _original_socket_connect_ex = socket.socket.connect_ex
    _original_create_connection = socket.create_connection

    def _fixture_allowed(address):
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        host, port = str(address[0]).lower(), int(address[1])
        if host not in ("127.0.0.1", "::1", "localhost"):
            return False
        for raw in os.environ.get("ANYUSE_LOCAL_FIXTURE_ORIGINS", "").split(","):
            parts = urllib.parse.urlsplit(raw.strip())
            configured_host = (parts.hostname or "").lower()
            configured_port = parts.port or (443 if parts.scheme == "https" else 80)
            if configured_host in ("127.0.0.1", "::1", "localhost") \
                    and configured_port == port:
                return True
        return False

    def _guarded_connect(sock, address):
        if _fixture_allowed(address):
            return _original_socket_connect(sock, address)
        raise PermissionError(f"{MARK}: network connections are refused")

    def _guarded_connect_ex(sock, address):
        if _fixture_allowed(address):
            return _original_socket_connect_ex(sock, address)
        raise PermissionError(f"{MARK}: network connections are refused")

    def _guarded_create_connection(address, *a, **kw):
        if _fixture_allowed(address):
            return _original_create_connection(address, *a, **kw)
        raise PermissionError(f"{MARK}: network connections are refused")

    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.create_connection = _guarded_create_connection
except Exception:
    pass

os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
try:
    import keyring
    import keyring.backends.null
    keyring.set_keyring(keyring.backends.null.Keyring())
except Exception:
    pass

# Ensure private/non-group-writable modes (0o022 check in REA evidence readers).
try:
    os.umask(0o022)
    import pathlib

    if sys.platform != "darwin":
        _orig_path_resolve = pathlib.Path.resolve

        # execution/rea_process_restart.py has SHA-256 self-pins across 7 REA suites
        # and calls Path('/bin/ps').resolve(strict=True). On usrmerge Linux this
        # resolves to /usr/bin/ps unless Path('/bin/ps') specifically preserves '/bin/ps'.
        def _guarded_path_resolve(self, strict=False):
            resolved = _orig_path_resolve(self, strict=strict)
            if str(self) == "/bin/ps" and not self.is_symlink():
                return self
            return resolved

        pathlib.Path.resolve = _guarded_path_resolve
except Exception:
    pass
