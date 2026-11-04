"""Install the REA toolchain into one folder: Node, a JDK, Ghidra and REA, at pinned versions.

``anyuse map rea setup`` runs this inside a macOS VM; ``anyuse map rea --allow-host setup`` runs
it on this Mac. Everything lands under one prefix (deleting it uninstalls), every archive is
checked against a pinned SHA-256 before it is unpacked, and nothing is installed globally: no
sudo, no Homebrew, no shell profile changes. The Command Line Tools are only checked; they come
from Apple's installer. The result is an ``env.sh`` to source and a ``rea doctor`` report.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from . import rea_mapping as R

#: tested together (Ghidra 12.1.x needs a full JDK 21+; REA's Ghidra provider needs Node 22.19+/24.11+)
PINS = {
    "node": {
        "version": "24.22.0",
        "url": "https://nodejs.org/dist/v24.22.0/node-v24.22.0-darwin-arm64.tar.gz",
        "sha256": "6a9724d9b50a5f22b4816cded441b5a8d9ddae546191a5e5b7efa5a5e2ee26d5",
    },
    "jdk": {
        "version": "21.0.12.1+1",
        "url": ("https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/"
                "OpenJDK21U-jdk_aarch64_mac_hotspot_21.0.12.1_1.tar.gz"),
        "sha256": "3623232f33a9c3baadf304480b2535f9a3cba8a58d42ecbb438ba267315d9998",
    },
    "ghidra": {
        "version": "12.1.4",
        "url": ("https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/"
                "ghidra_12.1.4_PUBLIC_20260921.zip"),
        "sha256": "ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db",
    },
    "rea": {
        "version": "6.7.0",
        "package": "rea-agents",
        "integrity": "sha512-yhIx2ioJYYT1C/tONfYeitC6LhnKmCh+ZwRUOgYuhSc733lGhkkt73zhug9WmLgxoMdKuqIYGCoBfD4fXkOhFw==",
    },
}

DEFAULT_GUEST_PREFIX = "~/anyuse-rea"
#: written into a prefix this setup created; a non-empty folder without it is never touched
MARKER = ".anyuse-rea-setup"
DEFAULT_HOST_PREFIX = "~/.anyuse/rea-tools"
CLT_GUEST_HINT = (
    "Install the Command Line Tools first. Headless inside the VM:\n"
    "  sudo touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress\n"
    "  sudo softwareupdate -i \"$(softwareupdate -l | sed -n 's/^\\* Label: \\(Command Line Tools.*\\)$/\\1/p' | tail -1)\"\n"
    "  sudo rm /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress\n"
    "or run `xcode-select --install` and confirm the dialog.")
CLT_HOST_HINT = "Run `xcode-select --install`, confirm Apple's dialog, then run this setup again."


def default_prefix(on_host: bool) -> Path:
    return Path(os.path.expanduser(DEFAULT_HOST_PREFIX if on_host else DEFAULT_GUEST_PREFIX))


def paths(prefix: Path) -> dict:
    ghidra = prefix / "deps" / f"ghidra_{PINS['ghidra']['version']}_PUBLIC"
    return {"node_bin": prefix / "node" / "bin", "java_home": prefix / "jdk" / "Contents" / "Home",
            "ghidra": ghidra, "rea": prefix / "tools" / "node_modules" / ".bin" / "rea",
            "env": prefix / "env.sh", "downloads": prefix / "dl", "receipts": prefix / "install"}


def env_script(prefix: Path) -> str:
    """Shell lines that set the toolchain paths; paths are single-quoted, so nothing in them expands."""
    p = paths(prefix)
    q = lambda path: shlex.quote(str(path))  # noqa: E731
    return ("# AnyUse REA toolchain; load it with the dot command\n"
            f"export PATH={q(p['node_bin'])}:\"$PATH\"\n"
            f"export JAVA_HOME={q(p['java_home'])}\n"
            f"export GHIDRA_INSTALL_DIR={q(p['ghidra'])}\n"
            f"export REA_CLI={q(p['rea'])}\n")


def command_line_tools() -> bool:
    """git and a compiler, as REA's Ghidra provider needs them.

    ``xcode-select -p`` comes first: it fails quietly without developer tools, where ``xcrun`` or
    ``git`` would open Apple's install dialog."""
    try:
        path = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, timeout=10)
        if path.returncode != 0 or not Path(path.stdout.strip() or "/nonexistent").is_dir():
            return False
        return all(subprocess.run(["/usr/bin/xcrun", "--find", tool], capture_output=True,
                                  timeout=30).returncode == 0 for tool in ("clang", "git"))
    except (OSError, subprocess.TimeoutExpired):
        return False


def _sha256(file: Path) -> str:
    h = hashlib.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, dest: Path, sha256: str, *, say=print) -> Path:
    """Download once into ``dest`` and check the pinned SHA-256; a mismatch deletes the file and stops."""
    if dest.is_file() and _sha256(dest) == sha256:
        return dest
    import httpx
    part = dest.with_suffix(dest.suffix + ".part")
    say(f"downloading {url}")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, read=120.0)) as response:
            response.raise_for_status()
            with part.open("wb") as f:
                for chunk in response.iter_bytes(1 << 20):
                    f.write(chunk)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise OSError(f"download failed for {url}: {exc}") from exc
    actual = _sha256(part)
    if actual != sha256:
        part.unlink()
        raise ValueError(f"checksum mismatch for {dest.name}: expected {sha256}, got {actual}")
    part.replace(dest)
    return dest


def _run(cmd, **kwargs):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{Path(cmd[0]).name} timed out after {int(exc.timeout)}s") from exc
    if proc.returncode != 0:
        raise ValueError(f"{Path(cmd[0]).name} failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc


def _locked_package(lock: Path, key: str) -> dict:
    """The recorded entry for one package in package-lock.json, or {} if absent or unreadable.

    Tolerates an interrupted install, an older lockfile layout without ``packages``, and a
    lock that never gained the entry, so a missing record becomes a clean integrity refusal
    rather than a KeyError."""
    try:
        data = json.loads(lock.read_text())
    except (OSError, ValueError):
        return {}
    entry = data.get("packages", {}).get(key) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


def _installed_version(package_json: Path) -> str:
    try:
        data = json.loads(package_json.read_text())
    except (OSError, ValueError):
        return ""
    return str(data.get("version", "")) if isinstance(data, dict) else ""


def _claim_prefix(prefix: Path) -> None:
    """Use a new or empty folder, or one this setup created; never one that holds other files."""
    if any(c in str(prefix) for c in "\n\r\0"):
        raise ValueError("the install folder path must not contain control characters")
    if prefix.exists():
        if not prefix.is_dir():
            raise ValueError(f"{prefix} exists and is not a folder")
        if any(prefix.iterdir()) and not (prefix / MARKER).is_file():
            raise ValueError(f"{prefix} already holds other files; choose a new or empty folder with --prefix")
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / MARKER).write_text("created by anyuse map rea setup\n")


#: where Ghidra and REA expect the decompiler on Apple silicon
DECOMPILER = Path("Ghidra/Features/Decompiler/os/mac_arm_64/decompile")
DECOMPILER_SOURCES = Path("Ghidra/Features/Decompiler/src/decompile/cpp")


def decompiler_sources(source: Path) -> list:
    """The decompiler's translation units, as Ghidra's own Makefile lists them (CORE, DECCORE, GHIDRA)."""
    makefile = (source / "Makefile").read_text().replace("\\\n", " ")
    names = []
    for variable in ("CORE", "DECCORE", "GHIDRA"):
        found = re.search(r"^" + variable + r"\s*=\s*(.*)$", makefile, re.M)
        if not found:
            raise ValueError(f"Ghidra's decompiler Makefile has no {variable} list")
        value = found.group(1)
        for ext, prefix in (("COREEXT_NAMES", "coreext_"), ("GHIDRAEXT_NAMES", "ghidraext_")):
            value = value.replace("$(" + ext + ")", " ".join(sorted(x.stem for x in source.glob(prefix + "*.cc"))))
        if "$" in value:
            raise ValueError(f"unresolved variable in the decompiler Makefile's {variable} list")
        names.extend(value.split())
    if not names or len(set(names)) != len(names) or any(not (source / (n + ".cc")).is_file() for n in names):
        raise ValueError("the decompiler source list does not match the files in the Ghidra archive")
    return names


def build_decompiler(ghidra: Path, build: Path, *, say=print) -> Path:
    """Ghidra's archive ships the decompiler's C++ sources but no Apple silicon binary: compile it with the
    Command Line Tools' clang++ (already required), into the place Ghidra and REA look for it."""
    import concurrent.futures
    out = ghidra / DECOMPILER
    source = ghidra / DECOMPILER_SOURCES
    names = decompiler_sources(source)
    build.mkdir(parents=True, exist_ok=True)
    cxx = ["/usr/bin/xcrun", "clang++", "-std=c++11", "-O2", "-w"]
    say(f"building Ghidra's arm64 decompiler from its {len(names)} source files (a few minutes)")

    def one(name):
        _run([*cxx, "-c", str(source / (name + ".cc")), "-o", str(build / (name + ".o"))], cwd=source, timeout=600)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as pool:
        list(pool.map(one, names))
    tmp = out.with_name(out.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    _run([*cxx, *[str(build / (n + ".o")) for n in names], "-o", str(tmp)], timeout=600)
    if not tmp.is_file():
        raise ValueError("the decompiler link produced no binary")
    tmp.chmod(0o755)
    tmp.replace(out)
    return out


def _unpack_tar(archive: Path, target: Path) -> None:
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    _run(["/usr/bin/tar", "-xzf", str(archive), "-C", str(tmp), "--strip-components", "1"])
    shutil.rmtree(target, ignore_errors=True)
    tmp.replace(target)


def setup(prefix: Path, *, on_host: bool, say=print) -> dict:
    """Install what is missing under ``prefix``; returns the receipt that is also written to install/."""
    R.assert_guest()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("the pinned toolchain is for macOS on Apple silicon (arm64)")
    if not command_line_tools():
        raise ValueError("git and a compiler are missing. " + (CLT_HOST_HINT if on_host else CLT_GUEST_HINT))
    prefix = prefix.expanduser().resolve()
    _claim_prefix(prefix)
    p = paths(prefix)
    for folder in (p["downloads"], p["receipts"], prefix / "deps", prefix / "tools"):
        folder.mkdir(parents=True, exist_ok=True)
    prefix.chmod(0o700)

    node, npm = p["node_bin"] / "node", p["node_bin"] / "npm"
    if (not node.exists() or not npm.exists() or
            _run([str(node), "--version"]).stdout.strip() != "v" + PINS["node"]["version"]):
        pin = PINS["node"]
        _unpack_tar(fetch(pin["url"], p["downloads"] / Path(pin["url"]).name, pin["sha256"], say=say), prefix / "node")
    java = p["java_home"] / "bin" / "java"
    pin = PINS["jdk"]
    # A JDK of a different version satisfies mere existence, so check the reported version too.
    if not java.exists() or pin["version"] not in _run([str(java), "-version"], timeout=30).stderr:
        _unpack_tar(fetch(pin["url"], p["downloads"] / "temurin-jdk.tar.gz", pin["sha256"], say=say), prefix / "jdk")
    if not (p["ghidra"] / "support" / "analyzeHeadless").exists():
        pin = PINS["ghidra"]
        archive = fetch(pin["url"], p["downloads"] / Path(pin["url"]).name, pin["sha256"], say=say)
        # Extract into a staging folder and move it into place, so an interrupted ditto never
        # leaves a half-unpacked Ghidra whose analyzeHeadless exists but is missing dependencies.
        staging = prefix / "deps" / ".ghidra.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        # ditto keeps the executable bits Ghidra's launch scripts and decompiler need
        _run(["/usr/bin/ditto", "-x", "-k", str(archive), str(staging)])
        unpacked = [d for d in staging.iterdir() if d.is_dir() and d.name.startswith("ghidra_")]
        if len(unpacked) != 1:
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError("Ghidra archive did not unpack to a single ghidra_* directory")
        shutil.rmtree(p["ghidra"], ignore_errors=True)
        unpacked[0].replace(p["ghidra"])
        shutil.rmtree(staging, ignore_errors=True)
    if not (p["ghidra"] / DECOMPILER).is_file():
        build_decompiler(p["ghidra"], prefix / "build" / "decompiler", say=say)

    pin = PINS["rea"]
    tools = prefix / "tools"
    # npm settings from the caller's environment (npm_config_global, _prefix, ...) must not apply
    env = {k: v for k, v in os.environ.items() if not k.lower().startswith("npm_config_")}
    env["PATH"] = f"{p['node_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    npm_args = ["--global=false", "--prefix", str(tools), "--cache", str(prefix / "npm-cache"),
                "--ignore-scripts", "--no-audit", "--no-fund"]
    lock = tools / "package-lock.json"
    key = f"node_modules/{pin['package']}"
    installed = _installed_version(tools / "node_modules" / pin["package"] / "package.json")
    if installed != pin["version"] or _locked_package(lock, key).get("integrity") != pin["integrity"]:
        say(f"installing {pin['package']}@{pin['version']}")
        (tools / "package.json").write_text(json.dumps(
            {"name": "anyuse-rea-tools", "private": True, "dependencies": {pin["package"]: pin["version"]}}) + "\n")
        # resolve first, without unpacking anything; install only from a lockfile that names the pinned release
        _run([str(npm), "install", "--package-lock-only", *npm_args], cwd=tools, env=env, timeout=600)
        if _locked_package(lock, key).get("integrity") != pin["integrity"]:
            raise ValueError(f"{pin['package']} integrity differs from the pinned release; refusing to install it")
        # npm ci checks every tarball against the lockfile; no install scripts run
        _run([str(npm), "ci", *npm_args], cwd=tools, env=env, timeout=900)
        if _installed_version(tools / "node_modules" / pin["package"] / "package.json") != pin["version"]:
            raise ValueError(f"{pin['package']} {pin['version']} did not install")

    p["env"].write_text(env_script(prefix))
    env.update(JAVA_HOME=str(p["java_home"]), GHIDRA_INSTALL_DIR=str(p["ghidra"]))
    try:
        doctor = subprocess.run([str(p["rea"]), "doctor", "--provider", "ghidra", "--json"],
                                capture_output=True, text=True, env=env, timeout=300)
        doctor_rc, doctor_out = doctor.returncode, doctor.stdout
    except (OSError, subprocess.TimeoutExpired):
        doctor_rc, doctor_out = None, ""  # a doctor that cannot run or never returns is "not ready"
    (p["receipts"] / "ghidra-doctor.json").write_text(doctor_out)
    try:
        report = json.loads(doctor_out)
    except ValueError:  # includes JSONDecodeError for non-JSON output
        report = None
    healthy = doctor_rc == 0 and isinstance(report, dict) and report.get("healthy") is True
    receipt = {"prefix": str(prefix), "on_host": on_host, "pins": {k: v["version"] for k, v in PINS.items()},
               "env": str(p["env"]), "rea": str(p["rea"]), "ghidra_ready": healthy,
               "doctor": str(p["receipts"] / "ghidra-doctor.json")}
    R.write_json(p["receipts"] / "setup.json", receipt)
    return receipt
