"""REA mapping: VM-only investigation and measured, version-bound adapters.

REA output is evidence, never executable instructions or a verification verdict.
An agent reviews it to write a typed candidate. Execution covers bundle CLIs with
file read-back and Dictionary's typed reopen event with independent definition
read-back, plus learned declarative event/document recipes with shared AX readers.
Other interfaces remain candidates until an appropriate runner exists.
No imports install software, contact a service, or inspect an application.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import signal
import struct
import subprocess
import tempfile
import time
from typing import Any

ANALYSIS = "anyuse.rea-analysis/1"
CANDIDATE = "anyuse.rea-candidate/1"
PROOF = "anyuse.rea-proof/1"
MAX_BUNDLE_BYTES = 2 * 1024**3
MAX_JSON_BYTES = 16 * 1024**2
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
HARNESS_BACKENDS = {"guest_learned_ax_v1", "guest_learned_svg_v1", "guest_fixed_state_v1"}


class ResourceReviewRequired(ValueError):
    """Do not open another target until the failed provider's resources are checked."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def load_json(path: str | Path) -> Any:
    p = Path(path)
    if p.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("manifest is too large")
    return json.loads(p.read_text())


def write_json(path: str | Path, value: Any) -> None:
    """Atomic receipt write; the caller chooses a private experiment directory."""
    p = Path(path)
    fd, name = tempfile.mkstemp(prefix=".rea-", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(name, p)
    finally:
        if os.path.exists(name):
            os.unlink(name)


_HOST_ALLOWED = False
_HOST_WARNED = False


def allow_host() -> None:
    """Allow REA on this Mac for the current process only (``anyuse map rea --allow-host``).

    There is deliberately no environment variable: the opt-in is typed per invocation.
    """
    global _HOST_ALLOWED
    _HOST_ALLOWED = True


def host_allowed() -> bool:
    return _HOST_ALLOWED


def assert_guest() -> None:
    """Tools and actuation require a macOS VM unless ``--allow-host`` was given."""
    global _HOST_WARNED
    if platform.system() != "Darwin":
        raise PermissionError("REA mapping execution requires macOS")
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"],
                            capture_output=True, text=True, timeout=5, check=False)
    if result.returncode or result.stdout.strip() != "1":
        if not _HOST_ALLOWED:
            raise PermissionError("REA mapping execution is VM-only; refusing the host "
                                  "(pass --allow-host to run on this Mac)")
        if not _HOST_WARNED:
            _HOST_WARNED = True
            import sys
            print("anyuse map rea: --allow-host: running on this Mac, not in a VM. REA decompiles "
                  "and instruments apps here; use only apps you are allowed to analyse.",
                  file=sys.stderr)


def _within(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError("expected a path relative to the app bundle")
    p = (root / rel).resolve(strict=True)
    if not p.is_relative_to(root):
        raise ValueError("path escapes the app bundle")
    return p


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(bundle: str | Path) -> dict:
    """Hash bundle bytes/resources, modes and symlinks, including same-version edits.

    Pure file reads; production callers must first assert_guest(). Tests use fake
    bundles. External symlinks and oversized bundles fail instead of being skipped.
    """
    root = Path(bundle).resolve(strict=True)
    if not root.is_dir() or root.suffix != ".app":
        raise ValueError("target must be an app bundle")
    info_path = _within(root, "Contents/Info.plist")
    info = plistlib.loads(info_path.read_bytes())
    rows, total = [], 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(base) / name
            relative = p.relative_to(root).as_posix()
            stat = p.lstat()
            if p.is_symlink():
                _within(root, relative)  # rejects dangling and out-of-bundle links
                rows.append([relative, "symlink", os.readlink(p)])
            elif p.is_file():
                total += stat.st_size
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError("bundle exceeds the 2 GiB analysis budget")
                rows.append([relative, "file", stat.st_mode & 0o777, _file_hash(p)])
            elif not p.is_dir():
                raise ValueError("special file in app bundle")
    if not info.get("CFBundleIdentifier"):
        raise ValueError("bundle has no identifier")
    return {"bundle_id": info["CFBundleIdentifier"],
            "version": str(info.get("CFBundleShortVersionString", "")),
            "build": str(info.get("CFBundleVersion", "")),
            "sha256": digest(sorted(rows)), "files": len(rows), "bytes": total}


def _bounded_run(argv: list[str], output: Path, error: Path, *, timeout: float,
                 cwd: Path | None = None) -> dict:
    """Spool output rather than holding REA's potentially large Evidence in RAM."""
    started = time.monotonic()
    with output.open("xb") as stdout, error.open("xb") as stderr:
        proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                cwd=cwd, start_new_session=True)
        timed_out = False
        def stop_owned():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_owned()
            code = proc.returncode
        except BaseException:
            stop_owned()
            raise
    return {"returncode": code, "timed_out": timed_out,
            "elapsed_s": round(time.monotonic() - started, 3),
            "output": output.name, "sha256": _file_hash(output),
            "stderr": error.name}


OBJC_METHOD = re.compile(r"[-+]\[(\S+) ([^\]\s]+)\]\Z")


def analysis_commands(bundle: str, rea: str, *, provider: str = "ghidra",
                      query: str = "", function: str = "", javascript: str = "",
                      search_kind: str = "strings") -> list[list[str]]:
    """Construct documented REA CLI calls. No shell; no automatic setup or download."""
    if provider not in {"ghidra", "hopper", "ida"}:
        raise ValueError("unknown native provider")
    if search_kind not in {"strings", "procedures"}:
        raise ValueError("search kind is strings or procedures")
    method = OBJC_METHOD.match(function) if provider == "ghidra" else None
    if method:
        # Ghidra names Objective-C methods Class::selector
        function = f"{method.group(1)}::{method.group(2)}"
    if javascript:
        if function or query:
            raise ValueError("native search/decompilation cannot accompany JavaScript analysis")
        target = str(_within(Path(bundle).resolve(strict=True), javascript))
        return [[rea, "analyze-javascript-application", target, "--json"]]
    base = ["--provider", provider, "--json"]
    commands = [[rea, "analyze", bundle, *base]]
    # option form: an Objective-C name ("-[Class sel:]") or a pattern may start with a dash
    if query:
        kind = ["--kind", "procedures"] if search_kind == "procedures" else []
        commands.append([rea, "search", bundle, f"--pattern={query}", *kind, *base])
    if function:
        commands.append([rea, "decompile", bundle, f"--procedure={function}", *base])
    return commands


def native_analysis_target(bundle: Path, folder: Path) -> tuple[str, dict | None]:
    """Copy one unambiguous native slice for providers that cannot select FAT slices.

    The original bundle remains the fingerprinted target. This pure file operation
    is called only after the production VM guard; tests use inert binary fixtures.
    """
    info = plistlib.loads(_within(bundle, "Contents/Info.plist").read_bytes())
    name = info.get("CFBundleExecutable")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError("bundle needs a simple CFBundleExecutable name")
    executable = _within(bundle, "Contents/MacOS/" + name)
    with executable.open("rb") as stream:
        magic = stream.read(4)
        if magic not in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}:
            return str(bundle), None
        count_data = stream.read(4)
        if len(count_data) != 4:
            raise ValueError("truncated universal Mach-O header")
        count = struct.unpack(">I", count_data)[0]
        if not 1 <= count <= 64:
            raise ValueError("invalid universal Mach-O architecture count")
        fmt = ">IIQQII" if magic[-1] == 0xbf else ">IIIII"
        row_size = struct.calcsize(fmt)
        table = stream.read(count * row_size)
        if len(table) != count * row_size:
            raise ValueError("truncated universal Mach-O architecture table")
        size = executable.stat().st_size
        slices = []
        for i in range(count):
            cpu, subtype, offset, length, align, *_ = struct.unpack_from(fmt, table, i * row_size)
            if (align > 31 or offset % (1 << align) or length < 32 or
                    offset < 8 + len(table) or offset + length > size):
                raise ValueError("invalid universal Mach-O slice bounds")
            if any(offset < p[2] + p[3] and p[2] < offset + length for p in slices):
                raise ValueError("overlapping universal Mach-O slices")
            slices.append((cpu, subtype, offset, length))
        cpu = {"arm64": 0x0100000c, "x86_64": 0x01000007}.get(platform.machine())
        matches = [s for s in slices if s[0] == cpu]
        if len(matches) != 1:
            raise ValueError("universal Mach-O needs one unambiguous native architecture")
        cpu, subtype, offset, length = matches[0]
        stream.seek(offset)
        header = stream.read(12)
        if header[:4] != b"\xcf\xfa\xed\xfe" or struct.unpack("<II", header[4:]) != (cpu, subtype):
            raise ValueError("universal Mach-O slice header does not match its table")
        stream.seek(offset)
        target = folder / "native-input.macho"
        with target.open("xb") as output:
            remaining = length
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError("universal Mach-O changed while copying")
                output.write(chunk)
                remaining -= len(chunk)
        target.chmod(0o400)
    return str(target), {"source": str(executable.relative_to(bundle)),
                         "source_sha256": _file_hash(executable), "cpu_type": cpu,
                         "cpu_subtype": subtype, "offset": offset, "bytes": length,
                         "file": target.name, "sha256": _file_hash(target)}


def investigate(bundle: str, rea: str, out: str, *, provider: str = "ghidra",
                query: str = "", function: str = "", javascript: str = "",
                timeout: float = 300, search_kind: str = "strings") -> dict:
    assert_guest()  # before reading targets, creating output, or launching REA
    if not 1 <= timeout <= 900:
        raise ValueError("per-command timeout must be 1–900 seconds")
    root = Path(bundle).resolve(strict=True)
    binary = Path(rea).resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("give the installed guest REA executable, not npx")
    before = fingerprint(root)
    folder = Path(out).resolve()
    if folder.is_relative_to(root):
        raise ValueError("analysis output must be outside the app bundle")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"format": ANALYSIS, "target": before, "bundle": str(root),
              "rea": str(binary), "provider": "javascript" if javascript else provider,
              "status": "incomplete", "runs": [], "model_calls": 0}
    write_json(folder / "analysis.json", report)
    try:
        target, extraction = (str(root), None) if javascript or provider != "ghidra" else native_analysis_target(root, folder)
        if extraction:
            report["native_input"] = extraction
        commands = analysis_commands(target, str(binary), provider=provider, query=query,
                                     function=function, javascript=javascript, search_kind=search_kind)
        version = _bounded_run([str(binary), "--version"], folder / "version.txt",
                               folder / "version.stderr", timeout=15)
        if version["returncode"] != 0:
            raise ValueError("REA version probe failed; see version.stderr")
        report["rea_version"] = (folder / "version.txt").read_text()[:256].strip()
        for i, command in enumerate(commands):
            run = _bounded_run(command, folder / f"evidence-{i}.json", folder / f"evidence-{i}.stderr",
                               timeout=timeout)
            run["operation"] = command[1]
            report["runs"].append(run)
            write_json(folder / "analysis.json", report)
            # REA can return useful partial observations while refusing to confirm
            # cleanup. Preserve them, but stop the whole pilot before another app.
            if run["timed_out"]:
                report["resource_review_required"] = True
                raise ResourceReviewRequired("REA timed out; review guest provider resources before continuing")
            if run["returncode"] != 0:
                try:
                    failure = load_json(folder / run["output"])
                except (OSError, ValueError):
                    failure = {}
                if isinstance(failure, dict) and (failure.get("code") == "cleanup_incomplete" or
                                                   failure.get("category") == "timeout"):
                    report["resource_review_required"] = True
                    raise ResourceReviewRequired("REA provider timeout or cleanup incomplete; review guest provider resources before continuing")
                raise ValueError(f"REA {command[1]} failed; evidence remains incomplete")
        if fingerprint(root) != before:
            raise ValueError("app changed during analysis")
        # A successful REA exit can still contain incomplete or inferred evidence.
        report["status"] = "evidence_collected"
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        write_json(folder / "analysis.json", report)
    return report


def validate_candidate(candidate: dict) -> dict:
    """Validate a reviewed interface spec. An imported `verified` flag has no meaning."""
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be a JSON object")
    if candidate.get("backend") in HARNESS_BACKENDS:
        from . import rea_harness as H
        return H.validate(candidate)
    if candidate.get("backend") == "guest_apple_event_dictionary_ax_v1":
        from . import rea_apple_event as AE
        return AE.validate(candidate)
    if candidate.get("backend") not in (None, "guest_bundle_cli_file_v1"):
        raise ValueError("unsupported REA execution backend")
    c = copy.deepcopy(candidate)
    if c.get("format") != CANDIDATE or not NAME.fullmatch(str(c.get("id", ""))):
        raise ValueError("invalid REA candidate format or id")
    target = c.get("target", {})
    if not isinstance(target, dict) or not target.get("bundle_id") or not SHA.fullmatch(str(target.get("sha256", ""))):
        raise ValueError("candidate requires the analysed target fingerprint")
    if c.get("effect") not in {"doc_save", "object_create"}:
        raise ValueError("this runner supports file writes and file/directory creation only")
    if c.get("item_kind") not in {"file", "document", "folder"}:
        raise ValueError("unsupported item kind for the file observer")
    if c.get("mode", "") not in {"", "replace", "append"}:
        raise ValueError("unknown document mode")
    if not c.get("label") or not isinstance(c.get("argv"), list) or not c["argv"]:
        raise ValueError("candidate needs a label and an argv template")
    if not isinstance(c["argv"][0], str):
        raise ValueError("entry point must be a bundle-relative string")
    exe = Path(c["argv"][0])
    if exe.is_absolute() or ".." in exe.parts or not str(exe).startswith("Contents/"):
        raise ValueError("entry point must be an executable inside the app bundle")
    params = c.get("params")
    if not isinstance(params, list) or not params:
        raise ValueError("typed parameters are required")
    names = set()
    for p in params:
        if not isinstance(p, dict):
            raise ValueError("parameters must be JSON objects")
        if not NAME.fullmatch(str(p.get("name", ""))) or p["name"] in names:
            raise ValueError("invalid or duplicate parameter")
        names.add(p["name"])
        if p.get("type") not in {"string", "path", "integer", "boolean"}:
            raise ValueError("unsupported parameter type")
        if p.get("source") not in {"path", "text", "name"}:
            raise ValueError("parameter needs an explicit map value source")
    holes = set()
    for token in c["argv"]:
        if not isinstance(token, str) or "\0" in token:
            raise ValueError("argv must contain strings without NUL")
        if "{" in token or "}" in token:
            if not re.fullmatch(r"\{[a-z][a-z0-9_]{0,63}\}", token):
                raise ValueError("parameters must occupy an entire argv token")
            holes.add(token[1:-1])
    if holes != names:
        raise ValueError("every parameter must bind exactly named argv placeholders")
    oracle = c.get("observer", {})
    if not isinstance(oracle, dict) or oracle.get("kind") != "file" or oracle.get("path_param") not in names:
        raise ValueError("independent filesystem observer required")
    path_param = next(p for p in params if p["name"] == oracle["path_param"])
    if path_param["type"] != "path" or path_param["source"] != "path":
        raise ValueError("observer path must bind the task's path")
    if sum(p["type"] == "path" for p in params) != 1:
        raise ValueError("this runner supports one output path per operation")
    if c["effect"] == "doc_save":
        text_param = next((p for p in params if p.get("source") == "text"), None)
        if c["item_kind"] == "folder" or not text_param or text_param["type"] != "string":
            raise ValueError("document save needs a text parameter and file item")
    citations = c.get("evidence", [])
    if not isinstance(citations, list) or not citations or any(not isinstance(e, dict) or not e.get("file") or not e.get("pointer") or
                            not SHA.fullmatch(str(e.get("sha256", ""))) for e in citations):
        raise ValueError("candidate needs REA evidence hashes and specific source pointers")
    # Deliberately exclude undeclared fields such as a model-supplied `verified`.
    fields = ("format", "backend", "id", "label", "target", "effect", "item_kind", "mode", "params", "argv", "observer", "evidence")
    return {k: c[k] for k in fields if k in c}


def bind(candidate: dict, inputs: dict) -> list[str] | dict:
    c = validate_candidate(candidate)
    if c.get("backend") in HARNESS_BACKENDS:
        from . import rea_harness as H
        return H.bind(c, inputs)
    if c.get("backend") == "guest_apple_event_dictionary_ax_v1":
        from . import rea_apple_event as AE
        return AE.bind(c, inputs)
    if set(inputs) != {p["name"] for p in c["params"]}:
        raise ValueError("inputs must match the candidate's parameters")
    values = {}
    for p in c["params"]:
        value, kind = inputs[p["name"]], p["type"]
        valid = ((kind in {"string", "path"} and isinstance(value, str) and "\0" not in value) or
                 (kind == "integer" and type(value) is int) or (kind == "boolean" and type(value) is bool))
        if not valid:
            raise ValueError(f"wrong type for {p['name']}")
        if kind == "path" and not Path(value).is_absolute():
            raise ValueError("path input must be absolute")
        values[p["name"]] = str(value).lower() if kind == "boolean" else str(value)
    return [values[t[1:-1]] if t.startswith("{") else t for t in c["argv"]]


def proof_status(candidate: dict, proof: dict | None, current: dict) -> tuple[bool, str]:
    c = validate_candidate(candidate)
    if c.get("backend") in HARNESS_BACKENDS:
        from . import rea_harness as H
        return H.proof_status(c, proof, current)
    if c.get("backend") == "guest_apple_event_dictionary_ax_v1":
        from . import rea_apple_event as AE
        return AE.proof_status(c, proof, current)
    if c["target"] != current:
        return False, "app fingerprint changed; reanalyse and reverify"
    if not proof or proof.get("format") != PROOF or proof.get("candidate_sha256") != digest(c):
        return False, "no matching verification receipt"
    if proof.get("target") != current or proof.get("backend") != "guest_bundle_cli_file_v1":
        return False, "receipt target or verification backend does not match"
    rows = proof.get("cases", [])
    if len(rows) < 3 or any(r.get("verdict") != "verified" or r.get("observer") != "file" for r in rows):
        return False, "all of at least three independent checks must pass"
    if any(not r.get("expected") or r.get("expected") != r.get("seen") or
           r.get("run", {}).get("returncode") != 0 or r.get("run", {}).get("timed_out") or
           not SHA.fullmatch(str(r.get("inputs_sha256", ""))) or
           not SHA.fullmatch(str(r.get("before_sha256", ""))) for r in rows):
        return False, "receipt does not contain complete successful independent observations"
    if len({r.get("inputs_sha256") for r in rows}) < 3:
        return False, "verification needs three distinct input sets"
    if len({r.get("before_sha256") for r in rows}) < 2:
        return False, "verification needs at least two initial states"
    if proof.get("status") != "verified":
        return False, "verification did not finish successfully"
    return True, "measured on this bundle version; receipt is local evidence, not a signature"


def candidate_surface(candidate: dict, proof: dict | None, current: dict, *, app: str = ""):
    from anyuse.execution.surface_discovery import Surface
    c = validate_candidate(candidate)
    ready, why = proof_status(c, proof, current)
    return Surface(kind="rea_adapter", key="rea:" + c["id"], label=c["label"], app=app,
                   bundle_id=c["target"]["bundle_id"], params=c["params"],
                   effect={"effect": c["effect"], "item_kinds": [c.get("item_kind", "")],
                           "mode": c.get("mode", ""), "source": "measured"},
                   interface="declared" if ready else "partial",
                   invoke={"candidate": c, "proof": proof, "target": current},
                   cite={"file": c["evidence"][0]["file"], "pointer": c["evidence"][0]["pointer"],
                         "sha256": c["evidence"][0]["sha256"]}, notes=[why])


def map_bindings(surface: Any, need: Any) -> tuple[str, str, dict]:
    """Bind explicitly named value roles; no word-similarity matching of functions."""
    c = validate_candidate(surface.invoke.get("candidate") or {})
    ready, why = proof_status(c, surface.invoke.get("proof"), surface.invoke.get("target") or {})
    if c.get("backend") == "guest_fixed_state_v1":
        return "uncertain", ("fixed state tasks require catalog replay with a checked current context" if ready else why), {}
    if c["effect"] != need.effect or c["item_kind"] != need.item_kind or c.get("mode", "") != need.mode:
        return "no", "effect, item kind or document mode does not match", {}
    if not ready:
        return "uncertain", why, {}
    if c.get("backend") == "guest_apple_event_dictionary_ax_v1":
        scope = need.values.get("path")
        if not scope:
            return "uncertain", "the dictionary scope must be explicit; use the VM replay command", {}
        if scope != c["observer"]["dictionary_path"]:
            return "no", "the requested dictionary scope does not match", {}
    if c.get("backend") in HARNESS_BACKENDS and c.get("scope"):
        source, value = c["scope"]["source"], c["scope"]["value"]
        if source not in need.values:
            return "uncertain", "the learned interface scope must be explicit", {}
        if need.values[source] != value:
            return "no", "the requested scope does not match the learned interface", {}
    values = {}
    for param in c["params"]:
        source = param["source"]
        if source not in need.values:
            return "no", f"missing explicit task value: {source}", {}
        values[param["name"]] = need.values[source]
    # A creation step's path names the container, whereas this observer takes the
    # full output path. Do not silently turn one meaning into the other.
    if need.effect == "object_create":
        return "uncertain", "creation needs an explicit full output path; use the VM replay command", {}
    try:
        bind(c, values)
    except ValueError as exc:
        return "no", str(exc), {}
    return "served", "explicit parameter roles and matching VM verification receipt", {
        p["name"]: p["source"] for p in c["params"]}


def _observe(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("filesystem observer refuses symbolic-link outputs")
    if not path.exists():
        return {"exists": False}
    if path.is_dir():
        return {"exists": True, "kind": "directory", "entries": sorted(p.name for p in path.iterdir())}
    return {"exists": True, "kind": "file", "sha256": _file_hash(path)}


def _expected(c: dict, inputs: dict, before: dict, initial_text: str = "") -> dict:
    if c["item_kind"] == "folder":
        return {"exists": True, "kind": "directory", "entries": []}
    text = next((inputs[p["name"]] for p in c["params"] if p["source"] == "text"), "")
    if c.get("mode") == "append" and before.get("exists"):
        text = initial_text + text
    return {"exists": True, "kind": "file", "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _execute(c: dict, bundle: Path, inputs: dict, folder: Path, timeout: float) -> dict:
    argv = bind(c, inputs)
    argv[0] = str(_within(bundle, argv[0]))
    if not os.access(argv[0], os.X_OK):
        raise ValueError("candidate entry point is not executable")
    # An actor's output is never the observation; file bytes are read separately.
    return _bounded_run(argv, folder / "actor.stdout", folder / "actor.stderr", timeout=timeout, cwd=folder)


def verify(candidate: dict, cases: list[dict], bundle: str, out: str, *, timeout: float = 30) -> dict:
    """Three or more cases, independent read-back, fresh test paths, no fallback retry.

    Cases bind their output path with the literal '$OUTPUT'. before is null (absent),
    a UTF-8 string (file), or {'directory': true}. Other inputs are explicit.
    """
    assert_guest()
    c = validate_candidate(candidate)
    if c.get("backend") in HARNESS_BACKENDS:
        from . import rea_harness as H
        return H.verify(c, cases, bundle, out, timeout)
    if c.get("backend") == "guest_apple_event_dictionary_ax_v1":
        from . import rea_apple_event as AE
        return AE.verify(c, cases, bundle, out, timeout)
    root = Path(bundle).resolve(strict=True)
    current = fingerprint(root)
    if c["target"] != current:
        raise ValueError("candidate target changed")
    if not 1 <= timeout <= 300 or len(cases) < 3:
        raise ValueError("need at least three cases and a 1–300 second timeout")
    path_param = c["observer"]["path_param"]
    if any(case.get("inputs", {}).get(path_param) != "$OUTPUT" for case in cases):
        raise ValueError("verification cases must use a new '$OUTPUT' path")
    if len({digest(case.get("inputs")) for case in cases}) < 3 or len({digest(case.get("before")) for case in cases}) < 2:
        raise ValueError("need three distinct input sets and two initial states")
    # Validate every case before the first process is started.
    for case in cases:
        before = case.get("before")
        if before is not None and not isinstance(before, str) and before != {"directory": True}:
            raise ValueError("invalid initial state")
        bind(c, {**case["inputs"], path_param: "/placeholder/output"})
    folder = Path(out).resolve()
    if folder.is_relative_to(root):
        raise ValueError("verification must not write into the target bundle")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    proof = {"format": PROOF, "backend": "guest_bundle_cli_file_v1", "candidate_sha256": digest(c),
             "target": current, "status": "incomplete", "cases": [], "model_calls": 0}
    write_json(folder / "proof.json", proof)
    try:
        for i, case in enumerate(cases):
            work = folder / f"case-{i}"
            work.mkdir(mode=0o700)
            path = work / "output"
            initial = case.get("before")
            if isinstance(initial, str):
                path.write_text(initial)
            elif initial == {"directory": True}:
                path.mkdir()
            inputs = {**case["inputs"], path_param: str(path)}
            before = _observe(path)
            expected = _expected(c, inputs, before, initial if isinstance(initial, str) else "")
            if before == expected:
                raise ValueError("a no-op case cannot establish that the adapter works")
            run = _execute(c, root, inputs, work, timeout)
            seen = _observe(path)
            row = {"inputs_sha256": digest(case["inputs"]), "before_sha256": digest(before),
                   "expected": expected, "seen": seen, "observer": "file", "run": run,
                   "verdict": "verified" if run["returncode"] == 0 and seen == expected else "refuted"}
            proof["cases"].append(row)
            write_json(folder / "proof.json", proof)
            if row["verdict"] != "verified":
                raise ValueError("verification failed; stopped without retry or AX fallback")
        if fingerprint(root) != current:
            raise ValueError("app changed during verification")
        proof["status"] = "verified"
        valid, why = proof_status(c, proof, current)
        if not valid:
            proof["status"] = "incomplete"
            raise ValueError(why)
    except Exception as exc:
        proof["error"] = str(exc)
        raise
    finally:
        write_json(folder / "proof.json", proof)
    return proof


def replay(candidate: dict, proof: dict, inputs: dict, bundle: str, out: str, *, timeout: float = 30) -> dict:
    """Replay only inside the VM and after rehashing the current bundle."""
    assert_guest()
    c = validate_candidate(candidate)
    if c.get("backend") in HARNESS_BACKENDS:
        from . import rea_harness as H
        return H.replay(c, proof, inputs, bundle, out, timeout)
    if c.get("backend") == "guest_apple_event_dictionary_ax_v1":
        from . import rea_apple_event as AE
        return AE.replay(c, proof, inputs, bundle, out, timeout)
    root = Path(bundle).resolve(strict=True)
    ready, why = proof_status(c, proof, fingerprint(root))
    if not ready:
        raise ValueError(why)
    if not 1 <= timeout <= 300:
        raise ValueError("timeout must be 1–300 seconds")
    bind(c, inputs)
    path = Path(inputs[c["observer"]["path_param"]])
    if path.is_symlink() or path.resolve().is_relative_to(root):
        raise ValueError("refusing a symlink output or write into the app bundle")
    before = _observe(path)
    initial = path.read_text() if before.get("kind") == "file" and c.get("mode") == "append" else ""
    expected = _expected(c, inputs, before, initial)
    folder = Path(out).resolve()
    if folder.is_relative_to(root) or folder == path.resolve() or folder.is_relative_to(path.resolve()):
        raise ValueError("replay logs must be outside the app and output path")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {"verdict": "uncertain", "candidate_sha256": digest(c), "observer": "file",
              "expected": expected, "retry": False}
    try:
        result["run"] = _execute(c, root, inputs, folder, timeout)
        result["seen"] = _observe(path)
        result["verdict"] = "verified" if (result["run"]["returncode"] == 0 and
            result["seen"] == expected and fingerprint(root) == c["target"]) else "refuted"
    finally:
        write_json(folder / "replay.json", result)
    return result
