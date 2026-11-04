"""`anyuse doctor` reports the active desktop profile — matcher, weights
source, drift tolerance, menu-bar exclusion, and whether it is the tuned
combination named "r2" — read from the SAME resolvers the
desktop code itself calls (``context.element_scorer.matcher_mode``/
``ax_weights``, ``execution.drift_gate._env_tolerance``,
``context.accessibility.drift_excludes_menubar``), never re-derived from env
vars in the CLI. Offline, no desktop: every case here only sets/reads
environment variables and JSON files under ``testdata/ax/``.
"""
import json
import os
import pathlib
import sys
import tempfile

from click.testing import CliRunner

# `import anyuse` resolves through whatever
# "anyuse"-named directory is first on sys.path/PYTHONPATH. The guarded
# runner's PYTHONPATH includes this checkout's PARENT directory
# (`tools/guarded_tests/run_allowed.sh`, so files under other worktrees can be
# reached at all) — which, in this repository layout, also contains the
# actual `anyuse` web-branch checkout as a sibling. A bare `from anyuse.cli
# import ...` here would therefore import THAT checkout's `cli.py`, not this
# one's, and silently test the wrong branch (this raised
# `ImportError: cannot import name '_desktop_profile'`, since that function
# only exists on this branch). A private directory holding a symlink
# `anyuse` -> this checkout, put at the FRONT of ``sys.path``, makes the
# package always resolve to the tree this test file lives in.
_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINKDIR = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
_PKGROOT = pathlib.Path(_LINKDIR.name)
(_PKGROOT / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, str(_PKGROOT))

from anyuse.cli import _desktop_profile, cli

_PROFILE_ENV = (
    "ANYUSE_ELEMENT_MATCHER", "ANYUSE_ELEMENT_WEIGHTS",
    "ANYUSE_DRIFT_MISSING_TOLERANCE", "ANYUSE_DRIFT_EXCLUDE_MENUBAR",
)


class _clean_profile_env:
    """Save/clear/restore the four profile env vars around a test."""

    def __enter__(self):
        import platform
        self._saved = {k: os.environ.get(k) for k in _PROFILE_ENV}
        self._real_system = platform.system
        platform.system = lambda: "Darwin"
        for k in _PROFILE_ENV:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        import platform
        platform.system = self._real_system
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _corpus2_path():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "testdata", "ax", "ax_weights_corpus2.json")


def test_default_profile_is_todays_default_not_r2():
    with _clean_profile_env():
        p = _desktop_profile()
    assert p["matcher"] == "cascade", p
    assert p["weights_source"] == "built-in AX_FITTED_WEIGHTS", p
    assert p["drift_missing_tolerance"] == 0.05, p     # measured with the resolver fixes in place (resolver safety)
    assert p["drift_excludes_menubar"] is False, p
    assert p["is_documented_r2_profile"] is False, p


def test_profile_detects_the_documented_r2_combination():
    with _clean_profile_env():
        os.environ["ANYUSE_ELEMENT_MATCHER"] = "weighted"
        os.environ["ANYUSE_ELEMENT_WEIGHTS"] = _corpus2_path()
        os.environ["ANYUSE_DRIFT_MISSING_TOLERANCE"] = "0.02"
        os.environ["ANYUSE_DRIFT_EXCLUDE_MENUBAR"] = "1"
        p = _desktop_profile()
    assert p["matcher"] == "weighted", p
    assert p["weights_source"] == f"file: {_corpus2_path()}", p
    assert p["drift_missing_tolerance"] == 0.02, p
    assert p["drift_excludes_menubar"] is True, p
    assert p["is_documented_r2_profile"] is True, p


def test_profile_is_not_r2_when_only_three_of_four_match():
    # r1-shaped: weighted + corpus2 weights, but tolerance 0.05 and menu bar IN.
    with _clean_profile_env():
        os.environ["ANYUSE_ELEMENT_MATCHER"] = "weighted"
        os.environ["ANYUSE_ELEMENT_WEIGHTS"] = _corpus2_path()
        os.environ["ANYUSE_DRIFT_MISSING_TOLERANCE"] = "0.05"
        p = _desktop_profile()
    assert p["is_documented_r2_profile"] is False, p


def test_unreadable_weights_file_falls_back_and_says_so():
    with _clean_profile_env():
        os.environ["ANYUSE_ELEMENT_WEIGHTS"] = "/no/such/file/anywhere.json"
        p = _desktop_profile()
    assert "unreadable" in p["weights_source"], p
    assert "AX_FITTED_WEIGHTS" in p["weights_source"], p
    assert p["is_documented_r2_profile"] is False, p


def test_doctor_cli_prints_the_desktop_profile_line():
    with _clean_profile_env():
        res = CliRunner().invoke(cli, ["doctor"])
    assert "desktop profile" in res.output, res.output
    assert "matcher=cascade" in res.output, res.output
    assert "tuned combination=no" in res.output, res.output


def test_doctor_json_includes_the_desktop_profile_check():
    with _clean_profile_env():
        os.environ["ANYUSE_ELEMENT_MATCHER"] = "weighted"
        os.environ["ANYUSE_ELEMENT_WEIGHTS"] = _corpus2_path()
        os.environ["ANYUSE_DRIFT_MISSING_TOLERANCE"] = "0.02"
        os.environ["ANYUSE_DRIFT_EXCLUDE_MENUBAR"] = "1"
        res = CliRunner().invoke(cli, ["doctor", "--json"])
    data = json.loads(res.output)
    rows = [c for c in data["checks"] if c["check"] == "desktop profile"]
    assert len(rows) == 1, data["checks"]
    row = rows[0]
    assert row["ok"] is True, row  # never fails doctor by itself
    assert "matcher=weighted" in row["found"], row
    assert "tuned combination=yes" in row["found"], row


def test_doctor_reports_desktop_readiness_as_optional_rows():
    import platform
    import anyuse.cli as C
    real_probe, real_system = C._desktop_probe, platform.system
    C._desktop_probe = lambda: {"pyobjc": True, "accessibility": False, "screen_recording": None}
    platform.system = lambda: "Darwin"
    prev = os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
    try:
        with _clean_profile_env():
            res = CliRunner().invoke(cli, ["doctor", "--json"])
        rows = {c["check"]: c for c in json.loads(res.output)["checks"]}
        for name in ("pyobjc", "accessibility permission", "screen recording permission", "desktop input"):
            assert rows[name]["optional"] is True and "desktop tasks" in rows[name]["needed_for"], rows[name]
        assert rows["pyobjc"]["ok"] is True and rows["accessibility permission"]["ok"] is False
        assert "Accessibility" in rows["accessibility permission"]["fix"]
        assert rows["screen recording permission"]["found"].startswith("unknown")
        assert rows["desktop input"]["found"].startswith("off") and "ANYUSE_DRIVE_ENABLED=on" in rows["desktop input"]["fix"]
        os.environ["ANYUSE_DRIVE_ENABLED"] = "on"
        C._desktop_probe = lambda: {"pyobjc": True, "accessibility": True, "screen_recording": True}
        with _clean_profile_env():
            res = CliRunner().invoke(cli, ["doctor", "--json"])
        rows = {c["check"]: c for c in json.loads(res.output)["checks"]}
        assert all(rows[n]["ok"] for n in ("pyobjc", "accessibility permission",
                                           "screen recording permission", "desktop input")), rows
    finally:
        C._desktop_probe, platform.system = real_probe, real_system
        os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
        if prev is not None:
            os.environ["ANYUSE_DRIVE_ENABLED"] = prev


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in sorted(vars(mod).items()):
        if not (n.startswith("test_") and callable(fn)):
            continue
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
