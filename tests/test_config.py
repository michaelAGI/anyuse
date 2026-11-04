"""Smoke test for the top-level anyuse config module (6.1).

Verifies config exposes the three symbols the engine's flat `import config` needs,
resolves the Gemini key from the env, and that a previously-dead engine module
(guides.detector) now imports with config in place.

Run from the anyuse package dir:
    cd anyuse && /usr/local/bin/python3.13 test_config.py
"""

import os
import sys
import tempfile
import pathlib

# anyuse uses mixed flat + package imports → both the package dir and its parent
# must be importable.
_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))            # flat: import config / from context / execution
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)


def main():
    print("=" * 60)
    print("anyuse 6.1 — top-level config module")
    print("=" * 60)

    import config
    import anyuse
    import tomllib

    package_version = tomllib.loads((_HERE / "pyproject.toml").read_text())["project"]["version"]
    assert package_version == anyuse.__version__ == "0.4.0"
    print("OK  package and project versions agree at 0.4.0")

    for sym in ("GEMINI_API_KEY", "GEMINI_MODEL_FLASH", "GEMINI_MODEL_COMPUTER_USE"):
        assert hasattr(config, sym), f"config missing {sym}"
    assert config.GEMINI_MODEL_FLASH, "GEMINI_MODEL_FLASH must have a default"
    assert config.GEMINI_MODEL_COMPUTER_USE, "GEMINI_MODEL_COMPUTER_USE must have a default"
    print("OK  config exposes GEMINI_API_KEY + the two model ids")

    # Env resolution: a key in the env is picked up; is_configured reflects it.
    prev = os.environ.get("GEMINI_API_KEY")
    try:
        os.environ["GEMINI_API_KEY"] = "test-key-123"
        import importlib
        importlib.reload(config)
        assert config.GEMINI_API_KEY == "test-key-123", config.GEMINI_API_KEY
        assert config.is_configured() is True
        print("OK  GEMINI_API_KEY resolves from the environment; is_configured() True")
    finally:
        if prev is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = prev
        import importlib
        importlib.reload(config)

    # A previously-dead engine module imports now that `config` exists.
    import guides.detector  # noqa: F401
    print("OK  guides.detector imports (was ModuleNotFoundError: config)")

    print("\nANYUSE CONFIG SMOKE TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
