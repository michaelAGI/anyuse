"""anyuse context.macos smoke.

Verifies MacOSContext imports and every method is callable + returns the right
type without crashing. NSWorkspace frontmost-app needs no permission; the
AppleScript methods (window title / browser URL / JS) degrade gracefully to
None/"" when Automation/Accessibility TCC is not granted (the documented gate).

    cd anyuse && /usr/local/bin/python3.13 test_context_macos.py
"""

import json
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


def main():
    print("=" * 60)
    print("anyuse context.macos — desktop context")
    print("=" * 60)

    from anyuse.context.macos import MacOSContext
    print("OK  context.macos imports; MacOSContext present")

    ctx = MacOSContext()

    full = ctx.get_full_context()
    assert isinstance(full, dict), f"get_full_context must be a dict, got {type(full)}"
    json.dumps(full, default=str)  # must be JSON-serializable (cli does this)
    print(f"OK  get_full_context() -> JSON-serializable dict: {full}")

    summ = ctx.get_context_summary()
    assert isinstance(summ, str), "get_context_summary must be str"
    print(f"OK  get_context_summary() -> str: {summ[:80]!r}")

    pid = ctx.get_frontmost_pid()
    assert pid is None or isinstance(pid, int), f"pid must be int|None, got {pid!r}"
    print(f"OK  get_frontmost_pid() -> {pid}")

    # TCC-gated methods must never crash — graceful None/"" without permission.
    wt = ctx.get_active_window_title()
    assert wt is None or isinstance(wt, str)
    url = ctx.get_browser_url()
    assert url is None or isinstance(url, str)
    js = ctx.execute_javascript_in_browser("1+1")
    assert isinstance(js, str)
    print(f"OK  TCC-gated methods degrade gracefully "
          f"(window_title={wt!r}, browser_url={url!r}, js={js!r})")

    print("\nCONTEXT.MACOS TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
