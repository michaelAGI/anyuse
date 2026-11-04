"""``anyuse doctor --install``: is this install able to serve MCP at all?

Five checks, each pass or fail, for a fresh checkout an agent just installed
(docs/INSTALL_FOR_AGENTS.md):

* the Python version (``>= 3.11``, as ``pyproject.toml`` requires);
* the imports the MCP server needs: the ``mcp`` SDK, ``httpx``, ``click``,
  ``jsonschema``, ``cryptography.fernet`` and AnyUse's own compiled-workflow modules;
* the data directory is writable: a probe file is created and removed when the
  directory exists, else the nearest existing parent must be writable (the
  directory is created on first write; this check does not create it);
* the MCP entry point ``python -m anyuse.mcpserver`` resolves and its server
  module imports, with a callable ``main``;
* ``settings.toml`` parses strictly, and ``ANYUSE_MCP_PROFILE`` names a profile (an unknown
  value would serve ``default``).

Nothing here opens a socket, starts the server, reads a key or calls a model.
The plain ``anyuse doctor`` checks the compile path and marks missing container,
model and learn items as optional (``opt``), without failing its exit status.
``anyuse doctor --install`` checks MCP server readiness after
``pip install -e '.[mcp]'``.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

MIN_PYTHON = (3, 11)

#: third-party modules the MCP server imports on its own run path
REQUIRED_IMPORTS = ("mcp", "httpx", "click", "jsonschema", "cryptography.fernet")
#: AnyUse modules the meta and default profiles load
ANYUSE_IMPORTS = ("anyuse.settings", "anyuse.paths", "anyuse.mcpserver.compiled_tools",
                  "anyuse.mcpserver.desktop_compiled_tools", "anyuse.mcpserver.meta_tools")
MCP_ENTRY = "anyuse.mcpserver.__main__"
MCP_SERVER = "anyuse.mcpserver.server"


def _row(name: str, ok: bool, found: str, fix: str = "") -> Dict[str, Any]:
    return {"check": name, "ok": bool(ok), "found": found, "fix": fix}


def check_python(version_info: Sequence[int]) -> Dict[str, Any]:
    found = ".".join(str(part) for part in tuple(version_info)[:3])
    ok = tuple(version_info)[:2] >= MIN_PYTHON
    return _row("python", ok, found,
                "" if ok else "create the venv with Python 3.11 or newer")


def check_imports(import_module: Callable[[str], Any]) -> Dict[str, Any]:
    missing = []
    for name in REQUIRED_IMPORTS + ANYUSE_IMPORTS:
        try:
            import_module(name)
        except Exception as exc:                 # ImportError, or a module that fails to load
            missing.append(f"{name} ({type(exc).__name__}: {exc})")
    if missing:
        return _row("imports", False, "; ".join(missing),
                    "pip install -e '.[mcp]' inside the venv")
    return _row("imports", True, f"{len(REQUIRED_IMPORTS + ANYUSE_IMPORTS)} modules")


def check_data_dir(data_dir: Path) -> Dict[str, Any]:
    if data_dir.is_dir():
        try:
            fd, probe = tempfile.mkstemp(prefix=".anyuse-doctor-", dir=str(data_dir))
            os.close(fd)
            os.unlink(probe)
        except OSError as exc:
            return _row("data dir writable", False, f"{data_dir}: {exc}",
                        "fix the directory's permissions or set ANYUSE_DATA_DIR")
        return _row("data dir writable", True, str(data_dir))
    if data_dir.exists():
        return _row("data dir writable", False, f"{data_dir} exists and is not a directory",
                    "set ANYUSE_DATA_DIR to a directory")
    parent = data_dir.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    ok = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    return _row("data dir writable", ok,
                f"{data_dir} (not created yet; {parent} is "
                f"{'writable' if ok else 'not writable'})",
                "" if ok else "set ANYUSE_DATA_DIR to a writable directory")


def check_mcp_entry(import_module: Callable[[str], Any],
                    find_spec: Callable[[str], Any]) -> Dict[str, Any]:
    try:
        spec = find_spec(MCP_ENTRY)
    except Exception as exc:
        spec, error = None, f"{type(exc).__name__}: {exc}"
    else:
        error = ""
    if spec is None:
        return _row("mcp entry", False, error or f"{MCP_ENTRY} not found",
                    "pip install -e '.[mcp]' inside the venv")
    try:
        server = import_module(MCP_SERVER)
    except Exception as exc:
        return _row("mcp entry", False, f"{MCP_SERVER}: {type(exc).__name__}: {exc}",
                    "pip install -e '.[mcp]' inside the venv")
    if not callable(getattr(server, "main", None)):
        return _row("mcp entry", False, f"{MCP_SERVER}.main is not callable")
    return _row("mcp entry", True, "python -m anyuse.mcpserver")


def check_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    from anyuse import settings
    target = settings.settings_path(path)
    try:
        settings.validate_file(path)
        resolved = settings.resolve("ANYUSE_MCP_PROFILE", path=path)
    except settings.SettingError as exc:
        return _row("settings readable", False, str(exc),
                    "fix or remove the named entry (`anyuse settings reset NAME`)")
    problem = settings.env_value_problem("ANYUSE_MCP_PROFILE")
    if problem:
        return _row("settings readable", False, problem,
                    "set ANYUSE_MCP_PROFILE to default, meta or full (or leave it unset) "
                    "where the MCP server is registered")
    state = "present" if target.exists() else "absent, defaults apply"
    return _row("settings readable", True,
                f"{target} ({state}); ANYUSE_MCP_PROFILE={resolved.value} "
                f"(from {resolved.source})")


def install_checks(*, version_info: Sequence[int] = sys.version_info,
                   import_module: Callable[[str], Any] = importlib.import_module,
                   find_spec: Callable[[str], Any] = importlib.util.find_spec,
                   data_dir: Optional[Path] = None,
                   settings_file: Optional[Path] = None) -> List[Dict[str, Any]]:
    if data_dir is None:
        from anyuse.paths import anyuse_data_dir
        data_dir = anyuse_data_dir()
    return [
        check_python(version_info),
        check_imports(import_module),
        check_data_dir(Path(data_dir)),
        check_mcp_entry(import_module, find_spec),
        check_settings(settings_file),
    ]


def render(checks: List[Dict[str, Any]]) -> str:
    lines = []
    for row in checks:
        lines.append(f"{'PASS' if row['ok'] else 'FAIL'}  {row['check']}: {row['found']}")
        if not row["ok"] and row.get("fix"):
            lines.append(f"      -> {row['fix']}")
    bad = sum(1 for row in checks if not row["ok"])
    lines.append(f"{len(checks) - bad} passed, {bad} failed")
    return "\n".join(lines)
