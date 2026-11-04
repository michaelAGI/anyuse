"""Tests for capture-side exclusion.

Covers step-guard denylists, visible secure fields, credential-typing suppression,
per-window exclusion caching and credential typing flag cleanup.

Self-executing (pytest is not installed in this env). Never touches the real
~/.anyuse capture paths — flag paths and detectors are stubbed per test.

Run from the anyuse package dir:
    cd anyuse && .venv/bin/python test_capture_exclusion.py
"""

import asyncio
import os
import pathlib
import sys
import tempfile
import time

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.watcher.batch_watcher import BatchWatcher, BatchWatcherConfig


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _MacContextStub:
    def __init__(self, app_name="Safari", window_title="Apple"):
        self.app_name = app_name
        self.window_title = window_title

    def get_frontmost_app(self):
        return {"name": self.app_name, "window_title": self.window_title}


def _watcher(app_name="Safari", window_title="Apple"):
    w = BatchWatcher(config=BatchWatcherConfig(output_level="silent"))
    w._running = True
    w._ax_available = False          # secure-field scan stubbed per-test
    w.macos_context = _MacContextStub(app_name, window_title)
    # Point the flag at a tmp path so the real ~/.anyuse is never read.
    w._TYPING_SUPPRESSION_FLAG = pathlib.Path(tempfile.mkdtemp()) / "flag"
    return w


def test_config_defaults_carry_denylist():
    cfg = BatchWatcherConfig()
    assert "1Password" in cfg.denylist_apps
    assert "Keychain Access" in cfg.denylist_apps
    assert "password" in cfg.denylist_title_patterns
    assert "private browsing" in cfg.denylist_title_patterns
    # macOS permission prompts are never recorded as workflow steps
    assert "UserNotificationCenter" in cfg.denylist_apps and "SecurityAgent" in cfg.denylist_apps
    assert "com.apple.UserNotificationCenter" in cfg.denylist_bundle_ids
    print("OK  test_config_defaults_carry_denylist")


def test_denylisted_app_withholds_steps():
    w = _watcher(app_name="1Password 8", window_title="Vault")
    assert w._step_guard({"app": "1Password 8", "window_title": "Vault"}) == "denylist_app"


def test_denylisted_title_withholds_steps():
    w = _watcher(app_name="Safari", window_title="Reset your PASSWORD — site")
    assert w._step_guard({"app": "Safari", "window_title": "Reset your PASSWORD — site"}) == "denylist_title"


def test_secure_field_withholds_steps():
    w = _watcher()
    w._secure_field_on_screen = lambda: True
    assert w._step_guard({"app": "Safari", "window_title": "Apple"}) == "secure_field"
    assert w._exclusion_cached == "secure_field"


def test_typing_suppression_flag_fresh_and_stale():
    w = _watcher()
    flag = w._TYPING_SUPPRESSION_FLAG
    flag.parent.mkdir(parents=True, exist_ok=True)

    # Fresh flag → suppressed.
    flag.write_text("now")
    assert w._typing_suppressed() is True
    assert w._compute_exclusion() == "credential_typing"

    # Stale flag (crash mid-type long ago) → capture resumes.
    import os
    old = time.time() - (w._TYPING_SUPPRESSION_MAX_AGE_S + 5)
    os.utime(flag, (old, old))
    assert w._typing_suppressed() is False
    assert w._compute_exclusion() is None
    print("OK  test_typing_suppression_flag_fresh_and_stale")


def test_exclusion_is_ttl_cached():
    w = _watcher()
    calls = {"n": 0}
    real = w._compute_exclusion

    def counting():
        calls["n"] += 1
        return real()

    w._compute_exclusion = counting
    for _ in range(50):
        w._exclusion_reason()
    assert calls["n"] == 1, "checks within the TTL window must hit the cache"
    w._exclusion_checked_at = 0.0  # expire
    w._exclusion_reason()
    assert calls["n"] == 2
    print("OK  test_exclusion_is_ttl_cached")


def test_input_events_suppressed_on_excluded_surface():
    w = _watcher(app_name="Bitwarden")
    # Simulate the on_input path's guard directly.
    assert w._exclusion_reason() == "denylist_app"
    print("OK  test_input_events_suppressed_on_excluded_surface")


def test_use_credential_sets_and_clears_flag():
    from anyuse.credentials import vault as vault_mod

    with tempfile.TemporaryDirectory() as td:
        flag = pathlib.Path(td) / ".typing_suppression"

        seen = {"during": None}

        class _PyAutoGuiStub:
            @staticmethod
            def write(value, interval=0.0):
                seen["during"] = flag.exists()

            @staticmethod
            def hotkey(*a):
                pass

            @staticmethod
            def press(*a):
                pass

        old_pag = vault_mod.pyautogui
        old_avail = vault_mod.PYAUTOGUI_AVAILABLE
        old_flag = vault_mod.CredentialVault.TYPING_SUPPRESSION_FLAG
        old_drive = os.environ.get("ANYUSE_DRIVE_ENABLED")
        vault_mod.pyautogui = _PyAutoGuiStub
        vault_mod.PYAUTOGUI_AVAILABLE = True
        vault_mod.CredentialVault.TYPING_SUPPRESSION_FLAG = flag
        os.environ["ANYUSE_DRIVE_ENABLED"] = "on"             # the vault types only with desktop input on
        try:
            v = vault_mod.CredentialVault(use_keychain=False)
            v.set_credential("t", "secret123")
            result = v.use_credential("t")
            assert result["success"] is True
            assert seen["during"] is True, \
                "flag must exist WHILE the secret is typed"
            assert not flag.exists(), "flag must be cleared after typing"

            # Error path still clears the flag (finally).
            def _boom(value, interval=0.0):
                raise RuntimeError("typing failed")
            _PyAutoGuiStub.write = staticmethod(_boom)
            result = v.use_credential("t")
            assert result["success"] is False
            assert not flag.exists(), "flag must be cleared on typing errors too"
        finally:
            vault_mod.pyautogui = old_pag
            vault_mod.PYAUTOGUI_AVAILABLE = old_avail
            vault_mod.CredentialVault.TYPING_SUPPRESSION_FLAG = old_flag
            if old_drive is None:
                os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
            else:
                os.environ["ANYUSE_DRIVE_ENABLED"] = old_drive
    print("OK  test_use_credential_sets_and_clears_flag")


def main():
    print("=" * 64)
    print("capture-side exclusion — tests")
    print("=" * 64)
    test_config_defaults_carry_denylist()
    test_denylisted_app_withholds_steps()
    test_denylisted_title_withholds_steps()
    test_secure_field_withholds_steps()
    test_typing_suppression_flag_fresh_and_stale()
    test_exclusion_is_ttl_cached()
    test_input_events_suppressed_on_excluded_surface()
    test_use_credential_sets_and_clears_flag()
    print("\nALL CAPTURE EXCLUSION TESTS PASSED")


if __name__ == "__main__":
    main()
