"""The wheel ships no test files.

The repository root IS the ``anyuse`` package, so every root
``test_*.py`` was a package module and went into the wheel (308 of 691 files).
``_wheel_build.BuildPy`` (``[tool.setuptools.cmdclass]``)
leaves them out. This test builds a real wheel in-process from a scratch copy
of the package (setuptools only, no network, no subprocess) and reads its file
list.

Run from the anyuse package dir:
    cd anyuse && python3 test_wheel_contents.py
"""
import glob
import os
import pathlib
import shutil
import sys
import tempfile
import zipfile
from email.parser import Parser

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse import _wheel_build  # noqa: E402

#: what the build reads: pyproject's listed packages and root files
_DIRS = ("context", "core", "credentials", "docs", "execution", "guides", "mcpserver", "memory",
         "osplatform", "registry", "tools", "watcher", "testdata", "licenses")


def _scratch_copy(dst: pathlib.Path) -> None:
    for item in _HERE.iterdir():
        if item.is_file() and (item.suffix in (".py", ".toml", ".txt", ".md") or item.name == "LICENSE"):
            shutil.copy2(item, dst / item.name)
    for d in _DIRS:
        if (_HERE / d).is_dir():
            shutil.copytree(_HERE / d, dst / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))


#: the optional pinned Public Suffix List and its digest (execution/web_scope.py); the checkout holds neither, so the
#: scratch copy gets stand-ins
_PSL_FILES = ("execution/web_scope_data/public_suffix_list.dat", "execution/web_scope_data/public_suffix_list.dat.sha256")


def _wheel_contents(extra: dict = None) -> tuple[list, str]:
    with tempfile.TemporaryDirectory(prefix="anyuse-wheel-") as tmp:
        src, out = pathlib.Path(tmp) / "src", pathlib.Path(tmp) / "out"
        src.mkdir(), out.mkdir()
        _scratch_copy(src)
        for rel, text in (extra or {}).items():
            (src / rel).write_text(text, encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(src)
        try:
            import setuptools.build_meta as build_meta
            from contextlib import redirect_stderr, redirect_stdout
            with open(os.devnull, "w") as null, redirect_stdout(null), redirect_stderr(null):
                name = build_meta.build_wheel(str(out))
        finally:
            os.chdir(cwd)
        with zipfile.ZipFile(out / name) as z:
            names = z.namelist()
            metadata = next(n for n in names if n.endswith(".dist-info/METADATA"))
            return names, z.read(metadata).decode("utf-8")


def test_the_built_wheel_has_no_test_files_and_still_has_the_package():
    names, metadata = _wheel_contents({p: "stand-in\n" for p in _PSL_FILES})
    tests = [n for n in names if os.path.basename(n).startswith("test_") and n.endswith(".py")]
    assert tests == [], tests[:5]
    for must in ("anyuse/__init__.py", "anyuse/cli.py", "anyuse/execution/executor.py",
                 "anyuse/execution/step_effect.py", "anyuse/context/accessibility.py"):
        assert must in names, must

    parsed_metadata = Parser().parsestr(metadata)
    extras = parsed_metadata.get_all("Provides-Extra", [])
    assert sorted(extras) == ["cdp", "desktop", "laya", "mcp", "rea-native", "registry", "test"], extras
    requirements = [Requirement(raw) for raw in parsed_metadata.get_all("Requires-Dist", [])]
    core = {r.name: r.specifier for r in requirements if r.marker is None}
    assert core == {"httpx": SpecifierSet(">=0.27,<1"), "click": SpecifierSet(">=8.1,<9")}, core
    mcp_crypto = [r for r in requirements if r.name == "cryptography" and r.marker
                  and r.marker.evaluate({"extra": "mcp", "sys_platform": "linux"})]
    assert len(mcp_crypto) == 1 and mcp_crypto[0].specifier == SpecifierSet(">=46,<51"), mcp_crypto
    for name in ("pyobjc-framework-Vision", "keyring"):
        desktop = [r for r in requirements if r.name == name and r.marker
                   and r.marker.evaluate({"extra": "desktop", "sys_platform": "darwin"})]
        assert len(desktop) == 1, (name, desktop)
        assert not desktop[0].marker.evaluate({"extra": "desktop", "sys_platform": "linux"})
        assert not desktop[0].marker.evaluate({"extra": "", "sys_platform": "darwin"})
    forbidden = {"google-genai", "mlx-vlm", "aiohttp", "playwright"}
    assert not {canonicalize_name(r.name) for r in requirements} & forbidden
    assert "anyuse/_wheel_build.py" not in names
    assert not any(n.endswith("conftest.py") for n in names)
    # the shipped web-scope data, and a pinned Public Suffix List with its digest once the file is added
    for must in ("anyuse/execution/web_scope_data/easylist_domains.txt.gz",
                 "anyuse/execution/web_scope_data/tracker_supplement.json", *(f"anyuse/{p}" for p in _PSL_FILES)):
        assert must in names, must
    for file in (_HERE / "testdata" / "discovery").iterdir():
        if file.name.endswith((".json", ".json.gz")):
            assert f"anyuse/testdata/discovery/{file.name}" in names, file.name
    for file in (_HERE / "execution" / "web_scope_data").iterdir():
        if file.is_file():
            assert f"anyuse/execution/web_scope_data/{file.name}" in names, file.name
    for must in ("anyuse/tools/confirmer/laya_worker.py", "anyuse/execution/web_runtime/NEVER_CONTACT.txt",
                 "anyuse/execution/guest/interlock_drive_e2e.py"):
        assert must in names, must
    for license in ("EasyList-GPL-3.0.txt", "EasyList-CC-BY-SA-3.0.txt"):
        assert any(n.endswith(f".dist-info/licenses/licenses/{license}") for n in names), license


def test_the_hook_filters_only_test_modules():
    assert not _wheel_build.shipped("/x/test_executor.py")
    assert not _wheel_build.shipped("conftest.py")
    assert _wheel_build.shipped("/x/executor.py") and _wheel_build.shipped("/x/latest_test.py")


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
