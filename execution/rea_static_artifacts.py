"""Bounded, original-file evidence builders for the generic REA agent.

Models select data and offsets, never Python, a shell command or an output path.
These builders neither attach to a process nor confer ABI/lifetime authority.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct

from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_qt_moc as Q

MAX_IMAGE_BYTES = 512 * 1024**2
MAX_RANGE_BYTES = 16384
KINDS = ("original_range", "original_code", "qt_moc_catalog", "qt_sources", "ghidra_code", "qt_code", "qt_selection")


def agent_guide():
    """Data-only syntax; examples contain no observed target facts."""
    return {"format": "anyuse.rea-static-evidence-guide/1", "scope": "Original bytes and analysis only; no receiver, ABI or task authority.",
        "placeholders": "Example paths, offsets, artifact names and indices are fictitious. Replace each with current-session observations.",
        "examples": [
            {"kind": "original_range", "relative": "Contents/MacOS/Example", "offset": "0x2000", "bytes": 16, "unit_bytes": 2},
            {"kind": "original_code", "relative": "Contents/MacOS/Example", "offset": "0x1000", "bytes": 16, "unit_bytes": 4},
            {"kind": "qt_moc_catalog", "relative": "Contents/MacOS/Example", "record_offset": "0x2000"},
            {"kind": "qt_sources"},
            {"kind": "ghidra_code", "relative": "Contents/MacOS/Example", "entry_offsets": ["0x1000"], "record_offsets": []},
            {"kind": "qt_code", "catalog_artifact": "s0000/static-evidence/evidence.json"},
            {"kind": "qt_selection", "catalog_artifact": "s0000/static-evidence/evidence.json",
             "code_artifact": "s0001/static-evidence/evidence.json", "table_artifact": "s0002/static-evidence/evidence.json",
             "sources_artifact": "s0003/static-evidence/manifest.json", "qt_core_relative": "Contents/Frameworks/QtCore.framework/Versions/A/QtCore",
             "local_index": 0},
        ],
        "workflow": [
            "Recover Qt class-local metadata from an observed static record offset. Read the retained method rows; choose only an observed ordinary no-argument void slot for the current Qt dispatch family.",
            "qt_code derives its fixed Ghidra entry, record and metadata-range extraction from that registered catalog; the agent supplies no Java source or list of copied byte ranges.",
            "Retain the original uint16 dispatch table at its evidence-supported offset with original_range. Retain the configured Qt source protocol with qt_sources; a missing cache reports its explicit setup command without downloading during discovery.",
            "qt_selection binds the original metadata, actual imported Ghidra observations, uint16 table, supported sources and original QtCore image. The existing pure static validator checks the interpretation.",
            "A static selection is still uncallable without a separately admitted live receiver, primary QObject base and constructor/destructor lifetime contract. No builder manufactures independent_review or task proof.",
            "Read Ghidra output and partial/failure diagnostics as untrusted investigation data. A seeded analysis entry or decompilation never establishes an ABI or target effect.",
        ]}


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _offset(value):
    _need(type(value) is str and re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]{0,15})", value) is not None,
          "canonical hexadecimal image offset required")
    return int(value, 16)


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def validate_request(request):
    """Pure request admission, so a rejected model decision can be corrected."""
    _need(type(request) is dict and request.get("kind") in KINDS, "select a supported static evidence builder")
    if request["kind"] == "qt_sources":
        _need(set(request) == {"kind"}, "qt_sources uses only the configured reusable source cache")
        return dict(request)
    if request["kind"] == "qt_code":
        _need(set(request) == {"kind", "catalog_artifact"} and type(request["catalog_artifact"]) is str and
              0 < len(request["catalog_artifact"]) <= 4096, "qt_code requires a registered original metadata artifact")
        return dict(request)
    if request["kind"] == "qt_selection":
        required = {"kind", "catalog_artifact", "code_artifact", "table_artifact", "sources_artifact", "qt_core_relative", "local_index"}
        _need(set(request) == required and all(type(request[name]) is str and 0 < len(request[name]) <= 4096
              for name in ("catalog_artifact", "code_artifact", "table_artifact", "sources_artifact", "qt_core_relative")),
              "Qt selection requires four registered artifacts and a bundle-relative QtCore image")
        _need(not Path(request["qt_core_relative"]).is_absolute() and ".." not in Path(request["qt_core_relative"]).parts and
              "\0" not in request["qt_core_relative"] and type(request["local_index"]) is int and 0 <= request["local_index"] < 512,
              "bounded Qt local index and bundle-relative QtCore image required")
        return dict(request)
    if request["kind"] == "ghidra_code":
        required = {"kind", "relative", "entry_offsets", "record_offsets"}
        _need(required <= set(request) <= required | {"data_ranges"}, "closed fixed Ghidra extraction request required")
        relative = request["relative"]
        _need(type(relative) is str and 0 < len(relative) <= 4096 and "\0" not in relative and
              not Path(relative).is_absolute() and ".." not in Path(relative).parts, "bundle-relative original image required")
        for name, minimum, alignment in (("entry_offsets", 1, 4), ("record_offsets", 0, 8)):
            offsets = request[name]
            _need(type(offsets) is list and minimum <= len(offsets) <= 4, "bounded distinct Ghidra offsets required")
            decoded = [_offset(v) for v in offsets]
            _need(len(set(decoded)) == len(decoded) and all(v % alignment == 0 for v in decoded), "distinct aligned Ghidra offsets required")
        ranges = request.get("data_ranges", [])
        _need(type(ranges) is list and len(ranges) <= Q.MAX_RANGES, "bounded imported data ranges required")
        for row in ranges:
            _need(type(row) is dict and set(row) == {"offset", "bytes"} and type(row["bytes"]) is int and
                  1 <= row["bytes"] <= MAX_RANGE_BYTES, "bounded original data range required")
            _offset(row["offset"])
        _need(sum(row["bytes"] for row in ranges) <= Q.MAX_TOTAL_BYTES, "imported data aggregate budget exceeded")
        return dict(request)
    expected = {"kind", "relative", "record_offset"} if request["kind"] == "qt_moc_catalog" else {
        "kind", "relative", "offset", "bytes", "unit_bytes"}
    _need(set(request) == expected, "static evidence request needs exactly its declared fields")
    relative = request["relative"]
    _need(type(relative) is str and 0 < len(relative) <= 4096 and "\0" not in relative and
          not Path(relative).is_absolute() and ".." not in Path(relative).parts,
          "bounded bundle-relative original image required")
    offset = _offset(request["record_offset"] if request["kind"] == "qt_moc_catalog" else request["offset"])
    if request["kind"] == "qt_moc_catalog":
        _need(offset % 8 == 0, "Qt static record must be aligned")
    else:
        size, unit = request["bytes"], request["unit_bytes"]
        _need(type(unit) is int and unit in {1, 2, 4, 8} and type(size) is int and
              1 <= size <= MAX_RANGE_BYTES and size % unit == 0, "bounded integral little-endian units required")
        _need(request["kind"] != "original_code" or offset % 4 == 0 and size % 4 == 0 and unit == 4,
              "arm64 instruction ranges need aligned 4-byte units")
    return dict(request)


class OriginalImage:
    """Read one plain-arm64 Mach-O through its actual file-backed mappings."""

    def __init__(self, bundle, relative, *, allow_arm64e=False):
        _need(type(relative) is str and 0 < len(relative) <= 4096 and "\0" not in relative,
              "bounded bundle-relative original image required")
        self.path = R._within(Path(bundle).resolve(strict=True), relative)
        self.before = self.path.lstat()
        _need(stat.S_ISREG(self.before.st_mode) and not self.path.is_symlink() and
              32 <= self.before.st_size <= MAX_IMAGE_BYTES, "bounded regular original image required")
        self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _need(_identity(os.fstat(self.fd)) == _identity(self.before), "original image changed before open")
            header = os.pread(self.fd, 32, 0)
            cpu, subtype = struct.unpack_from("<II", header, 4)
            _need(
                header[:4] == b"\xcf\xfa\xed\xfe"
                and cpu == 0x0100000c
                and ((subtype & 0xFFFFFF) in {0, 2} if allow_arm64e else subtype == 0),
                "this evidence builder requires a thin plain-arm64 Mach-O",
            )
            # Reuse the runtime's complete positive segment/section validation.
            images, code, segments, data = B._macho_data(self.path, include_native_path_data=True)
            _need(len(images) == 1, "one original image required")
            self.image_identity = images[0]
            self.segments = segments[0]["segments"]
            self.code = code[0]["sections"]
            self.data = data[0]["sections"]
            count, size = struct.unpack_from("<II", header, 16)
            commands = os.pread(self.fd, size, 32)
            at, bases, self.fixups = 0, [], None
            for _ in range(count):
                kind, width = struct.unpack_from("<II", commands, at)
                command = commands[at:at + width]
                if kind == 0x19:
                    vmaddr, _, fileoff, filesize = struct.unpack_from("<QQQQ", command, 24)
                    if fileoff == 0 and filesize >= 32 + size:
                        bases.append(vmaddr)
                if kind == 0x80000034:
                    _need(self.fixups is None and width == 16, "one bounded chained-fixups command required")
                    self.fixups = struct.unpack_from("<II", command, 8)
                at += width
            _need(len(bases) == 1, "one original image base required")
            self.base = bases[0]
            self.pin = {"file": str(self.path), "bytes": self.before.st_size, "sha256": self._hash()}
            self.unchanged()
        except BaseException:
            os.close(self.fd)
            raise

    def _hash(self):
        digest = hashlib.sha256()
        for at in range(0, self.before.st_size, 1024**2):
            raw = os.pread(self.fd, min(1024**2, self.before.st_size - at), at)
            _need(len(raw) == min(1024**2, self.before.st_size - at), "original image shortened")
            digest.update(raw)
        return digest.hexdigest()

    def unchanged(self):
        _need(_identity(self.path.lstat()) == _identity(self.before) == _identity(os.fstat(self.fd)),
              "original image changed while building evidence")

    def close(self):
        os.close(self.fd)

    def read(self, offset, count, *, kind="data"):
        _need(type(offset) is int and offset >= 0 and type(count) is int and 1 <= count <= MAX_RANGE_BYTES,
              "bounded original range required")
        sections = self.code if kind == "code" else self.data
        _need(sum(int(s["offset"], 16) <= offset and offset + count <= int(s["offset"], 16) + s["bytes"]
                  for s in sections) == 1, "range must belong to one positive original section of its declared kind")
        segments = [s for s in self.segments if int(s["offset"], 16) <= offset and
                    offset + count <= int(s["offset"], 16) + s["file_bytes"]]
        _need(len(segments) == 1, "range lacks one exact file-backed segment")
        segment = segments[0]
        at = segment["file_offset"] + offset - int(segment["offset"], 16)
        raw = os.pread(self.fd, count, at)
        _need(len(raw) == count, "original range is incomplete")
        self.unchanged()
        return raw


def original_range(image, request):
    _need(set(request) == {"kind", "relative", "offset", "bytes", "unit_bytes"},
          "original_range requires relative, offset, bytes and unit_bytes")
    size, unit = request["bytes"], request["unit_bytes"]
    _need(type(unit) is int and unit in {1, 2, 4, 8} and type(size) is int and
          1 <= size <= MAX_RANGE_BYTES and size % unit == 0, "bounded integral little-endian units required")
    offset = _offset(request["offset"])
    code = request["kind"] == "original_code"
    _need(not code or offset % 4 == 0 and size % 4 == 0 and unit == 4,
          "arm64 instruction ranges need aligned 4-byte units")
    raw = image.read(offset, size, kind="code" if code else "data")
    return {"format": "anyuse.rea-original-instruction-range/1" if code else "anyuse.rea-original-file-range/1",
            "status": "retained_original_instruction_bytes" if code else "retained_original_file_bytes",
            "original_unchanged": True, "automatic_abi_or_lifetime_admission": False, "task_proof": False,
            "byte_order": "little", "unit_bytes": unit, "image": image.pin,
            "unsigned_units": [int.from_bytes(raw[i:i + unit], "little") for i in range(0, size, unit)],
            "range": {"image_offset": hex(offset), "bytes": size, "original_hex": raw.hex(),
                      "sha256": hashlib.sha256(raw).hexdigest()}}


def _qt_record(image, offset):
    raw = image.read(offset, 56)
    targets = []
    for index in (1, 2, 3):
        encoded = struct.unpack_from("<Q", raw, 8 * index)[0]
        _need(encoded >> 63 == 0 and (encoded >> 44) & 127 == 0 and (encoded >> 36) & 255 == 0,
              "Qt record requires plain original 64-offset rebases")
        targets.append(encoded & ((1 << 36) - 1))
    _need(len(set(targets)) == 3, "Qt record table/code targets must be distinct")
    return raw, targets


def qt_catalog(image, request):
    """Recover class-local metadata from original bytes, without ABI claims.

    This investigation artifact intentionally has a distinct format from the
    older Ghidra imported-image catalog. It never invents imported bytes, a
    relocation application receipt, a decompilation or an independent review.
    """
    _need(set(request) == {"kind", "relative", "record_offset"},
          "qt_moc_catalog requires relative and record_offset")
    at = _offset(request["record_offset"])
    _need(at % 8 == 0, "Qt static record must be aligned")
    record, (strings_at, data_at, dispatcher_at) = _qt_record(image, at)
    image.read(dispatcher_at, 4, kind="code")
    data_bytes = strings_at - data_at
    _need(60 <= data_bytes <= MAX_RANGE_BYTES and data_bytes % 4 == 0,
          "bounded contiguous Qt integer/string storage required")
    data = image.read(data_at, data_bytes)
    words = struct.unpack("<" + "I" * (len(data) // 4), data)
    header = list(words[:14])
    _need(header[0] == 13 and words[-1] == 0 and 1 <= header[4] <= 512,
          "supported revision-13 bounded Qt method metadata required")
    count, method_at = header[4], data_at + 4 * header[5]
    first_pair = image.read(strings_at, 8)
    pair_bytes = int.from_bytes(first_pair[:4], "little")
    _need(8 <= pair_bytes <= min(MAX_RANGE_BYTES, 4096 * 8) and pair_bytes % 8 == 0,
          "bounded Qt string pairs required")
    pairs = image.read(strings_at, pair_bytes)
    strings, ranges = [], []
    total = 0
    def retain(role, address, raw):
        nonlocal total
        _need(len(ranges) < Q.MAX_RANGES and len(raw) <= Q.MAX_RANGE_BYTES and
              total + len(raw) <= Q.MAX_TOTAL_BYTES, "Qt catalog range budget exhausted")
        total += len(raw)
        row = {"id": len(ranges), "role": role, "image_offset": hex(address),
               "address": hex(image.base + address), "bytes": len(raw), "original_hex": raw.hex(),
               "original_sha256": hashlib.sha256(raw).hexdigest()}
        ranges.append(row)
        return row["id"]
    retain("direct_superdata_record", at, record)
    retain("generated_metadata_data_array", data_at, data)
    retain("string_pairs", strings_at, pairs)
    for index in range(pair_bytes // 8):
        offset, size = struct.unpack_from("<II", pairs, 8 * index)
        _need(offset >= pair_bytes and size <= 4096, "Qt string payload must follow the pairs")
        raw = image.read(strings_at + offset, size + 1)
        _need(raw[-1:] == b"\0" and b"\0" not in raw[:-1], "Qt string terminator differs")
        strings.append(raw[:-1].decode("utf-8", errors="strict"))
        retain("string_payload_" + str(index), strings_at + offset, raw)
    _need(header[1] < len(strings), "Qt class string index is outside its actual table")
    rows = image.read(method_at, 24 * count)
    row_id = retain("method_rows", method_at, rows)
    methods = []
    for index in range(count):
        name, argc, payload, tag, flags, meta = struct.unpack_from("<6I", rows, 24 * index)
        _need(name < len(strings) and argc <= 32 and tag < len(strings), "bounded actual Qt method row required")
        payload_at = data_at + 4 * payload
        _need(data_at <= payload_at and payload_at + 4 * (1 + 2 * argc) <= strings_at,
              "Qt method payload is outside its original integer array")
        raw = image.read(payload_at, 4 * (1 + 2 * argc))
        payload_id = retain("method_payload_" + str(index), payload_at, raw)
        result = int.from_bytes(raw[:4], "little")
        ordinary_void = result == 43 and argc == 0
        access = ("private", "protected", "public", "unsupported")[flags & 3]
        kind = {0: "method", 4: "signal", 8: "slot", 12: "constructor"}[flags & 12]
        methods.append({"local_index": index, "name": strings[name], "name_index": name,
                        "signature_as_recorded": strings[name] + "()" if not argc else None,
                        "kind": kind, "access": access, "flags": hex(flags),
                        "parameters": [{"index": i, "encoding": hex(int.from_bytes(raw[4 + i * 4:8 + i * 4], "little"))}
                                       for i in range(argc)],
                        "return_type": {"kind": "source_builtin_id" if result < 0x80000000 else "unresolved",
                                        "id": result, "name": "void" if result == 43 else None,
                                        "encoding": hex(result), "callable": False},
                        "compatibility": bool(flags & 16), "cloned": bool(flags & 32),
                        "scriptable": bool(flags & 64), "is_const": bool(flags & 256),
                        "revision_encoded": int(bool(flags & 128)), "callable_abi_admitted": False,
                        "supported_zero_argument_void_slot": ordinary_void and kind == "slot" and flags & ~15 == 0,
                        "row_range": row_id, "row_byte_offset": 24 * index, "payload_range": payload_id})
    return {"format": "anyuse.rea-original-qt-moc-catalog/1", "status": "original_class_local_metadata_collected",
            "image": image.pin, "macho": {"architecture": "arm64", "cpu_subtype": 0, "image_base": hex(image.base)},
            "record_offset": hex(at), "dispatcher_offset": hex(dispatcher_at), "class_name": strings[header[1]],
            "metadata_revision": 13, "header_u32": header, "metadata_data_bytes": data_bytes,
            "local_method_count": count, "string_count": len(strings), "methods": methods, "ranges": ranges,
            "original_unchanged": True, "imported_image_observed": False,
            "callable_abi_admitted": False, "lifetime_admitted": False, "task_proof": False,
            "next_requirements": ["independent original dispatcher/code interpretation",
                                  "checked original chained-fixup membership",
                                  "supported Qt source protocol", "receiver/lifetime admission"]}


def _registered(artifact, artifacts, session):
    _need(type(artifacts) is dict and type(artifact) is str and artifact in artifacts,
          "a registered current-session static artifact is required")
    pin = artifacts[artifact]
    file, root = Path(pin["file"]), Path(session)
    _need(file == root / artifact and file.is_relative_to(root) and file.resolve(strict=True) == file and
          not file.is_symlink() and file.is_file() and file.stat().st_size <= R.MAX_JSON_BYTES and
          R._file_hash(file) == pin["sha256"], "registered static artifact changed or escaped its session")
    return R.load_json(file), pin


def qt_selection(bundle, request, folder, artifacts, session):
    """Assemble reviewed-format static inputs from actual independent producers.

    Producing the static selection establishes neither an ABI nor a receiver.
    The Qt actor still needs its separately admitted primary-base/lifetime path.
    """
    catalog, catalog_pin = _registered(request["catalog_artifact"], artifacts, session)
    code, code_pin = _registered(request["code_artifact"], artifacts, session)
    table, table_pin = _registered(request["table_artifact"], artifacts, session)
    sources, sources_pin = _registered(request["sources_artifact"], artifacts, session)
    _need(catalog.get("format") == "anyuse.rea-original-qt-moc-catalog/1" and catalog.get("original_unchanged") is True and
          code.get("format") == "anyuse.rea-ghidra-code-at-offsets/1" and code.get("status") == "completed_static_code_observation" and
          code.get("image") == catalog["image"] and table.get("format") == "anyuse.rea-original-file-range/1" and
          table.get("image") == catalog["image"] and table.get("unit_bytes") == 2,
          "Qt selection requires actual original metadata, matching Ghidra code and original uint16 table")
    _need(sources.get("format") == "anyuse.rea-qt-moc-source-retention/1", "retained Qt source protocol required")
    index = request["local_index"]
    _need(index < len(catalog["methods"]), "Qt local method index exceeds the observed catalog")
    method = catalog["methods"][index]
    _need(method.get("supported_zero_argument_void_slot") is True, "selected Qt method needs a new callable family; only ordinary zero-argument void slots are supported")
    matches = [record for record in code.get("records", []) if record["image_offset"] == catalog["record_offset"]]
    _need(len(matches) == 1, "Ghidra must observe the selected original Qt pointer record")
    record = matches[0]
    _need(len(record["pointers"]) == 3, "three actual Ghidra pointer correlations required")
    imported = {(row["image_offset"], row["bytes"]): row["imported_hex"] for row in code.get("data_ranges", [])}
    ranges = []
    for row in catalog["ranges"]:
        key = (row["image_offset"], row["bytes"])
        _need(key in imported, "Ghidra must independently retain every consumed Qt metadata range")
        raw = imported[key]
        _need(row["role"] == "direct_superdata_record" or raw == row["original_hex"], "non-record Qt metadata differs in the imported image")
        ranges.append({**row, "imported_hex": raw, "imported_sha256": hashlib.sha256(bytes.fromhex(raw)).hexdigest()})
    original = next(row for row in ranges if row["role"] == "direct_superdata_record")
    _need(record["imported_hex"] == original["imported_hex"], "Ghidra pointer record and metadata read disagree")
    pointers = []
    for index_word, pointer in zip((1,2,3),record["pointers"]):
        _need(pointer["relocation_status"] == "APPLIED" and pointer["relocation_type"] == 0x8888,
              "Qt selection needs checked applied original chained rebases")
        pointers.append({**pointer, "pointer_format": "DYLD_CHAINED_PTR_64_OFFSET",
                         "original": original["original_hex"][16 * index_word:16 * (index_word+1)]})
    core = OriginalImage(bundle,request["qt_core_relative"])
    try:
        module_pin = {"path": catalog["image"]["file"], "sha256": catalog["image"]["sha256"]}
        core_pin = {"path": core.pin["file"], "sha256": core.pin["sha256"]}
        value = {"format": "anyuse.rea-qt-moc-static-catalog/1", "status": "completed_class_local_static_catalog",
            "completed": True, "inputs_unchanged": True, "callable_abi_admitted": False, "lifetime_admitted": False,
            "task_proof": False, "global_method_indices_available": False,
            "source_protocol": {"qt_version": Q.QT_VERSION, "qtbase_commit": Q.QT_COMMIT, "metadata_revision": 13},
            "record_layout": "direct-superdata-56", "static_storage_layout": "qtmochelpers-contiguous-data-then-strings",
            "image": catalog["image"], "qt_core": core.pin, "source_manifest": sources_pin,
            "locator": {"image_offset": catalog["record_offset"]},
            "dispatcher_code_site": {"image_offset": catalog["dispatcher_offset"]},
            "class_name": catalog["class_name"], "macho": catalog["macho"], "local_method_count": catalog["local_method_count"],
            "methods": catalog["methods"], "ranges": ranges, "metadata_data_bytes": catalog["metadata_data_bytes"],
            "header_u32": catalog["header_u32"], "string_count": catalog["string_count"],
            "table_pointer_authority": pointers[:2], "dispatcher_pointer_authority": pointers[2],
            "producer_artifacts": [catalog_pin,code_pin,table_pin,sources_pin]}
        folder.mkdir(mode=0o700)
        path = folder / "catalog.json"; R.write_json(path,value)
        def file_pin(pin):
            return {key:pin[key] for key in ("file","sha256")}
        selection = {"format": Q.SELECTION, "module": module_pin, "qt_core": core_pin,
            "catalog": {"file": str(path), "sha256": R._file_hash(path)}, "code": file_pin(code_pin),
            "table": file_pin(table_pin), "qt_sources": file_pin(sources_pin), "class_name": catalog["class_name"],
            "record_offset": catalog["record_offset"], "dispatcher_offset": catalog["dispatcher_offset"],
            "local_index": index, "signature": method["signature_as_recorded"]}
        Q.validate_selection(selection)
        # Reuse the existing static interpretation checks while keeping
        # receiver/ABI admission separate; no review is manufactured here.
        Q.static_selection_plan(selection,value,code,table,sources)
        R.write_json(folder / "evidence.json", {"format": "anyuse.rea-qt-static-selection-evidence/1", "status": "static_selection_retained",
            "selection": selection, "callable_abi_admitted": False, "lifetime_admitted": False, "proof_created": False,
            "remaining": ["independently admitted receiver, primary QObject base and lifecycle contract"]})
        return {"status": "static_selection_retained", "receipt": str(folder / "evidence.json"), "selection": selection,
                "evidence_only": True, "proof_created": False, "callable_abi_admitted": False, "lifetime_admitted": False}
    finally:
        core.close()


def build(bundle, request, out, *, artifacts=None, session=None, timeout=180):
    request = validate_request(request)
    if request["kind"] == "qt_sources":
        from . import rea_qt_sources as sources
        return sources.retain(out)
    R.assert_guest()
    if request["kind"] == "qt_selection":
        return qt_selection(bundle, request, Path(out), artifacts, session)
    if request["kind"] in {"ghidra_code", "qt_code"}:
        from . import rea_ghidra_artifacts as ghidra
        if request["kind"] == "qt_code":
            catalog, _ = _registered(request["catalog_artifact"], artifacts, session)
            _need(catalog.get("format") == "anyuse.rea-original-qt-moc-catalog/1" and catalog.get("original_unchanged") is True,
                  "Qt code extraction requires actual original metadata")
            image = Path(catalog["image"]["file"])
            _need(image.is_relative_to(Path(bundle)), "Qt original image is outside the owned bundle")
            request = validate_request({"kind": "ghidra_code", "relative": image.relative_to(bundle).as_posix(),
                "entry_offsets": [catalog["dispatcher_offset"]], "record_offsets": [catalog["record_offset"]],
                "data_ranges": [{"offset": row["image_offset"], "bytes": row["bytes"]} for row in catalog["ranges"]]})
        return ghidra.build(bundle, request, out, timeout=timeout)
    folder = Path(out)
    _need(not folder.exists(), "new static evidence output required")
    image = OriginalImage(
        bundle,
        request.get("relative"),
        allow_arm64e=request["kind"] in {"original_range", "original_code"},
    )
    try:
        value = qt_catalog(image, request) if request["kind"] == "qt_moc_catalog" else original_range(image, request)
        image.unchanged()
        _need(image._hash() == image.pin["sha256"], "original bytes changed while building evidence")
        folder.mkdir(mode=0o700)
        path = folder / "evidence.json"
        with path.open("x") as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
        return {"status": value["status"], "receipt": str(path), "sha256": R._file_hash(path),
                "kind": request["kind"], "evidence_only": True, "proof_created": False,
                "callable_abi_admitted": False, "lifetime_admitted": False}
    finally:
        image.close()
