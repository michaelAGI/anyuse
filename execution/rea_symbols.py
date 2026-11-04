"""VM-only symbol-table evidence for an agent's next REA investigation.

The fixed native tool reads a private binary copy. Symbol names and addresses
are metadata, not callable interfaces or verified actions. Imports do not
inspect apps, activate them, install tools, execute binaries or contact models.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import os
import platform
import plistlib
import re
import stat
import struct
import time

from . import rea_mapping as R

SYMBOLS = "anyuse.rea-symbols/1"
MAX_ROWS = 128
MAX_QUERY_BYTES = 256
MAX_SCAN_BYTES = R.MAX_JSON_BYTES
MAX_LINE_BYTES = 8192
MAX_TOOL_BYTES = 128 * 1024**2
NM_SETTING = "ANYUSE_REA_NM"
DEFAULT_NM_PATHS = (
    "/Library/Developer/CommandLineTools/usr/bin/llvm-nm",
    "/Library/Developer/CommandLineTools/usr/bin/nm",
    "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/llvm-nm",
    "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/nm",
)
_STUBS = {"/usr/bin/nm", "/usr/bin/llvm-nm", "/usr/bin/xcrun"}
_TOOL_MAGIC = {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
_LINE = re.compile(r"^\s*(?:([0-9a-fA-F]{8,16})\s+)?\(([^)\r\n]{1,256})\)\s+(.+)$")
_NAME = re.compile(r"\b(?:non-external|external)\s+(.+)$")
_MAGIC = {b"\xce\xfa\xed\xfe": "<", b"\xcf\xfa\xed\xfe": "<",
          b"\xfe\xed\xfa\xce": ">", b"\xfe\xed\xfa\xcf": ">"}


def _tool_architecture(stream, size):
    """Require a native64 executable slice using only bounded file metadata."""
    native = {"arm64": 0x0100000c, "aarch64": 0x0100000c, "x86_64": 0x01000007}.get(platform.machine())
    if native is None:
        raise ValueError("nm readiness supports native arm64/x86_64 hosts only")
    stream.seek(0)
    magic = stream.read(4)
    offset, length, selected_subtype, container = 0, size, None, "thin"
    if magic in _TOOL_MAGIC - set(_MAGIC):
        container = "universal"
        endian = ">" if magic in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"} else "<"
        count = stream.read(4)
        if len(count) != 4:
            raise ValueError("truncated nm universal header")
        count = struct.unpack(endian + "I", count)[0]
        if not 1 <= count <= 64:
            raise ValueError("invalid nm universal architecture count")
        wide = magic in {b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
        fmt = endian + ("IIQQII" if wide else "IIIII")
        row_size = struct.calcsize(fmt)
        table = stream.read(count * row_size)
        if len(table) != count * row_size:
            raise ValueError("truncated nm universal architecture table")
        rows = []
        for i in range(count):
            cpu, subtype, start, count_bytes, align, *_ = struct.unpack_from(fmt, table, i * row_size)
            if (align > 31 or start % (1 << align) or start < 8 + len(table) or count_bytes < 32 or
                    start + count_bytes > size or any(start < p[2] + p[3] and p[2] < start + count_bytes for p in rows)):
                raise ValueError("invalid nm universal slice bounds")
            rows.append((cpu, subtype, start, count_bytes))
        matches = [row for row in rows if row[0] == native]
        if len(matches) != 1:
            raise ValueError("nm needs one native host slice; translation is not requested")
        _, selected_subtype, offset, length = matches[0]
    stream.seek(offset)
    header = stream.read(32)
    if len(header) != 32 or header[:4] != b"\xcf\xfa\xed\xfe":
        raise ValueError("nm must contain a native64 Mach-O executable, not a wrapper")
    cpu, subtype, filetype, commands, command_bytes, _, _ = struct.unpack("<7I", header[4:])
    if (cpu != native or selected_subtype is not None and subtype != selected_subtype or filetype != 2 or
            not 1 <= commands <= 8192 or not commands * 8 <= command_bytes <= min(16 * 1024**2, length - 32)):
        raise ValueError("nm tool is not a bounded native host executable")
    table = stream.read(command_bytes)
    cursor, entry = 0, False
    for _ in range(commands):
        if cursor + 8 > len(table):
            raise ValueError("truncated nm executable load commands")
        command, command_size = struct.unpack_from("<II", table, cursor)
        if command_size < 8 or command_size % 8 or cursor + command_size > len(table):
            raise ValueError("invalid nm executable load command")
        entry |= command in {0x80000028, 0x5}  # LC_MAIN or LC_UNIXTHREAD
        cursor += command_size
    if cursor != len(table) or not entry:
        raise ValueError("nm executable needs complete load commands and an entry point")
    return {"container": container, "host": platform.machine(), "cpu_type": cpu,
            "cpu_subtype": subtype, "offset": offset, "bytes": length, "filetype": "MH_EXECUTE"}


def _tool_pin(value):
    if (not isinstance(value, (str, os.PathLike)) or not str(value) or len(str(value)) > 4096 or
            not all(c.isprintable() for c in str(value))):
        raise ValueError("nm must be an explicit absolute tool path")
    path = Path(value).expanduser()
    if not path.is_absolute() or str(path) in _STUBS:
        raise ValueError("system nm/xcrun installer shims are not admitted")
    path = path.resolve(strict=True)
    if str(path) in _STUBS or path.name not in {"nm", "llvm-nm"}:
        raise ValueError("need the existing real nm or llvm-nm binary, not a dispatcher")
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or not 4 <= before.st_size <= MAX_TOOL_BYTES or
            not os.access(path, os.X_OK)):
        raise ValueError("nm must be a bounded regular executable Mach-O tool")
    fields = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
    h, size, magic = hashlib.sha256(), 0, None
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        if fields(os.fstat(stream.fileno())) != fields(before):
            raise ValueError("nm tool changed while opening")
        architecture = _tool_architecture(stream, before.st_size)
        stream.seek(0)
        while True:
            chunk = stream.read(min(1024**2, MAX_TOOL_BYTES - size + 1))
            if not chunk:
                break
            if magic is None:
                magic = chunk[:4]
            size += len(chunk)
            if size > MAX_TOOL_BYTES:
                raise ValueError("nm tool exceeds the byte bound")
            h.update(chunk)
        if size != before.st_size or fields(os.fstat(stream.fileno())) != fields(before) or fields(path.lstat()) != fields(before):
            raise ValueError("nm tool changed while hashing")
    if magic not in _TOOL_MAGIC:
        raise ValueError("nm must be a real Mach-O binary; scripts and wrappers are not admitted")
    return {"file": str(path), "sha256": h.hexdigest(), "bytes": size,
            "identity": list(fields(before)), "magic": magic.hex(), "executable": True,
            "architecture": architecture}


def tool_readiness(nm=None):
    """Pure file check for real symbol tooling; never invokes installer shims.

    Explicit cached bytes are pinned, not authenticated against a registry.
    Readiness is neither a target-process operation nor a probe invocation.
    """
    result = {"status": "unavailable", "tool": None, "why": "no existing real nm tool is configured",
              "error": None, "tool_invocations": 0, "selection": None, "candidates": [],
              "authentication": "existing local tool bytes pinned; no registry authentication"}
    try:
        if nm is not None:
            candidates, selection = (nm,), "explicit"
        else:
            from anyuse import settings
            configured = settings.get(NM_SETTING)
            candidates, selection = ((configured,), "setting") if configured else (DEFAULT_NM_PATHS, "fixed_existing_toolchain_paths")
        for candidate in candidates:
            try:
                tool = _tool_pin(candidate)
            except (OSError, ValueError, RuntimeError) as error:
                result["candidates"].append({"path": str(candidate), "why": str(error)[:1024]})
                result["error"] = {"type": type(error).__name__, "message": str(error)[:1024]}
                continue
            result.update(status="ready", tool=tool, selection=selection, why="existing real tool is hash-bound", error=None)
            break
        if result["status"] != "ready" and result["error"]:
            result["why"] = result["error"]["message"]
    except (OSError, ValueError, TypeError) as error:
        result.update(why="nm operator configuration is unavailable",
                      error={"type": type(error).__name__, "message": str(error)[:1024]})
    return result


def _native_copy(root, folder):
    target, provenance = R.native_analysis_target(root, folder)
    if provenance is not None:
        native = Path(target).resolve(strict=True)
        if not native.is_relative_to(folder) or not native.is_file() or native.is_symlink():
            raise ValueError("symbol input must be the private native slice")
        if R._file_hash(native) != provenance["sha256"]:
            raise ValueError("native slice changed while preparing symbol evidence")
        return native, {**provenance, "kind": "universal_slice"}
    # REA accepts a bundle for thin binaries; nm requires its executable. Copy
    # this case too, so native tools never read a mutable installed executable.
    info = plistlib.loads(R._within(root, "Contents/Info.plist").read_bytes())
    source_name = "Contents/MacOS/" + info["CFBundleExecutable"]
    source = R._within(root, source_name)
    size = source.stat().st_size
    if not 28 <= size <= R.MAX_BUNDLE_BYTES:
        raise ValueError("symbol input must be a bounded Mach-O binary")
    with source.open("rb") as stream:
        header = stream.read(12)
        if header[:4] not in _MAGIC:
            raise ValueError("symbol input must be a native Mach-O binary")
        cpu, subtype = struct.unpack(_MAGIC[header[:4]] + "II", header[4:])
        native = folder / "native-input.macho"
        stream.seek(0)
        with native.open("xb") as output:
            remaining = size
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError("thin Mach-O changed while copying")
                output.write(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise ValueError("thin Mach-O changed while copying")
    native.chmod(0o400)
    digest = R._file_hash(native)
    if R._file_hash(source) != digest:
        raise ValueError("thin Mach-O changed while copying")
    return native, {"kind": "thin_binary_copy", "source": source_name,
                    "source_sha256": digest, "cpu_type": cpu, "cpu_subtype": subtype,
                    "offset": 0, "bytes": size, "file": native.name, "sha256": digest}


def _row(raw, number):
    row = {"line": number, "raw": raw, "parsed": False}
    match = _LINE.fullmatch(raw)
    symbol = _NAME.search(match[3]) if match else None
    if symbol:
        row.update(parsed=True, address="0x" + match[1].lower() if match[1] else None,
                   section=match[2], symbol=symbol[1], attributes=match[3][:symbol.start(1)].strip())
    return row


def _scan(path, query, limit):
    rows, counts = [], {"scanned_bytes": 0, "scanned_lines": 0, "matched_lines": 0,
                       "oversized_lines": 0, "returned_rows": 0}
    scan_truncated = False
    needle = query.casefold()
    # Read at most the scan budget plus one byte. In particular, a malformed
    # giant line cannot force an unbounded read or appear as many valid rows.
    with path.open("rb") as stream:
        oversized = False
        while counts["scanned_bytes"] < MAX_SCAN_BYTES:
            allowance = min(MAX_LINE_BYTES + 1, MAX_SCAN_BYTES - counts["scanned_bytes"])
            chunk = stream.readline(allowance)
            if not chunk:
                break
            counts["scanned_bytes"] += len(chunk)
            complete = chunk.endswith(b"\n")
            if oversized:
                if complete:
                    oversized = False
                continue
            counts["scanned_lines"] += 1
            if len(chunk) > MAX_LINE_BYTES:
                counts["oversized_lines"] += 1
                oversized = not complete
                continue
            if not complete and counts["scanned_bytes"] == MAX_SCAN_BYTES:
                # A final unterminated line is usable only if the scan reached EOF.
                scan_truncated = bool(stream.read(1))
                if scan_truncated:
                    break
            raw = chunk.decode("utf-8", "replace").rstrip("\r\n")
            if raw and needle in raw.casefold():
                counts["matched_lines"] += 1
                if len(rows) < limit:
                    rows.append(_row(raw, counts["scanned_lines"]))
        if not scan_truncated:
            scan_truncated = bool(stream.read(1))
    counts["returned_rows"] = len(rows)
    flags = {"scan": scan_truncated, "rows": counts["matched_lines"] > limit,
             "oversized_lines": counts["oversized_lines"] > 0}
    return rows, counts, flags


def _artifact(path):
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": R._file_hash(path)} if path.is_file() else None


def collect(bundle, out, *, query="", limit=MAX_ROWS, timeout=15, nm=None):
    """Collect bounded literal symbol matches and retain raw native diagnostics.

    Errors after output creation produce an incomplete, hash-bound receipt;
    partial rows remain evidence only. No retries, fallback or actuation occur.
    """
    R.assert_guest()  # before validation, filesystem access or native execution
    if (not isinstance(query, str) or "\0" in query or
            any(not char.isprintable() for char in query) or len(query.encode("utf-8")) > MAX_QUERY_BYTES):
        raise ValueError("symbol query must be bounded literal text")
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS or type(timeout) is not int or not 1 <= timeout <= 30:
        raise ValueError("symbols need 1–128 rows and a 1–30 second timeout")
    root, folder = Path(bundle).resolve(strict=True), Path(out).resolve()
    if folder.is_relative_to(root):
        raise ValueError("symbol evidence must be outside the target app")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.monotonic()
    receipt = {"format": SYMBOLS, "status": "incomplete", "bundle": str(root), "target": None,
               "native_input": None, "query": query, "query_mode": "literal_casefold", "limit": limit,
               "scan_limit_bytes": MAX_SCAN_BYTES, "line_limit_bytes": MAX_LINE_BYTES, "timeout_s": timeout,
               "run": None, "rows": [], "counts": None, "truncated": False,
               "truncation": None, "raw_output": None, "stderr": None, "error": None,
               "evidence_only": True, "callable_api": False, "proof_created": False,
               "actuated": False, "model_calls": 0, "receipt": str(folder / "symbols.json")}
    receipt.update(tool=None, tool_readiness=None, tool_after=None, tool_unchanged=False, tool_invocations=0)
    raw, stderr = folder / "nm.txt", folder / "nm.stderr"
    try:
        readiness = tool_readiness(nm)
        receipt["tool_readiness"] = readiness
        if readiness["status"] != "ready":
            receipt["status"] = "unavailable"
            raise ValueError(readiness["why"])
        receipt["tool"] = readiness["tool"]
        receipt["target"] = R.fingerprint(root)
        native, receipt["native_input"] = _native_copy(root, folder)
        if _tool_pin(receipt["tool"]["file"]) != receipt["tool"]:
            raise ValueError("nm tool changed before dispatch")
        argv = [receipt["tool"]["file"], "-m", str(native)]
        receipt["run"] = {"argv": argv, "timeout_s": timeout}
        receipt["tool_invocations"] = 1
        receipt["run"].update(R._bounded_run(argv, raw, stderr, timeout=timeout, cwd=folder))
        receipt["tool_after"] = _tool_pin(receipt["tool"]["file"])
        receipt["tool_unchanged"] = receipt["tool_after"] == receipt["tool"]
        if not receipt["tool_unchanged"]:
            raise ValueError("nm tool changed during symbol collection")
        receipt["rows"], receipt["counts"], receipt["truncation"] = _scan(raw, query, limit)
        receipt["truncated"] = any(receipt["truncation"].values())
        if receipt["run"]["timed_out"]:
            raise ValueError("nm exceeded its bounded timeout; partial output retained")
        if receipt["run"]["returncode"] != 0:
            raise ValueError(f"nm failed with exit code {receipt['run']['returncode']}; diagnostics retained")
        if R._file_hash(native) != receipt["native_input"]["sha256"]:
            raise ValueError("native copy changed during symbol collection")
        current = R.fingerprint(root)
        if current != receipt["target"]:
            receipt["after_target"] = current
            raise ValueError("app fingerprint changed during symbol collection")
        receipt["status"] = "evidence_collected"
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[:4096]}
    receipt["raw_output"], receipt["stderr"] = _artifact(raw), _artifact(stderr)
    receipt["elapsed_s"] = round(time.monotonic() - started, 4)
    R.write_json(folder / "symbols.json", receipt)
    return receipt
