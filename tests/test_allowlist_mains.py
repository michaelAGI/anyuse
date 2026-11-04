"""Every allowlisted test file must run its tests when executed as a script.

tools/guarded_tests/run_allowed.sh runs each file with `python <file>` and counts
exit code 0 as a pass. A file without an `if __name__ == "__main__":` block exits 0
without running anything, so its tests silently never run (test_desktop_e2e5.py and
test_desktop_e2e6.py were two such files).
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _allowlisted():
    with open(os.path.join(HERE, "docs", "TEST_ALLOWLIST.txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def test_every_allowlisted_file_has_a_main_block():
    missing = []
    for name in _allowlisted():
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            missing.append(f"{name} (file missing)")
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if 'if __name__ == "__main__":' not in src and "if __name__ == '__main__':" not in src:
            missing.append(name)
    assert not missing, f"allowlisted test files that run no tests: {missing}"


def test_this_check_is_itself_allowlisted():
    assert "tests/test_allowlist_mains.py" in set(_allowlisted())


if __name__ == "__main__":
    p = f = 0
    for n, fn in [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]:
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            f += 1; print("FAIL", n, "::", repr(e)[:400])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
