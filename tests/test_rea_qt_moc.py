"""Inert Qt contracts/resources; run only through the guest guarded suite.

These files and observations are explicit test fixtures, never vendor ABI,
Ghidra/native execution or successful application/task evidence.
"""
import copy
import hashlib
import json
from pathlib import Path
import struct

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_native_path as P
from anyuse.execution import rea_qt_moc as Q
from anyuse.execution import rea_runtime_bridge as B
from test_rea_runtime_bridge import native_files, runtime


def _qt_moc_files(native_files, monkeypatch):
    """Real file/section/admission collector, with separately hashed inert SDK.

    The test-only SDK substitution avoids copying actual Qt source into fixtures.
    No production gate is changed and no native code is executed by this factory.
    """
    from test_rea_runtime_bridge import _lifecycle_path_files, _refreeze_native_path_contracts
    _, recipe, files = _lifecycle_path_files(native_files)
    module = files["module"]
    raw = bytearray(module.read_bytes())
    # Existing inert command layout: UUID, original __TEXT, then __DATA. Extend
    # only the positive FILE-BACKED data section and its actual segment extent.
    data_command = 32 + 24 + 152
    struct.pack_into("<Q", raw, data_command + 48, 0x800)
    struct.pack_into("<Q", raw, data_command + 72 + 40, 0x800)
    raw.extend(b"\0" * (0x2800 - len(raw)))
    raw[0x1010:0x1018] = bytes.fromhex("1f2003d5c0035fd6")
    record, data, table_at = 0x2080, 0x2100, 0x2300
    header = [13, 0, 0, 0, 1, 14, 0, 0, 0, 0, 0, 0, 0, 0]
    method_row = [1, 0, 20, 2, 8, 0]
    words = header + method_row + [43, 0]
    strings = data + 4 * len(words)
    raw[data:data + 4 * len(words)] = struct.pack("<" + "I" * len(words), *words)
    encoded_strings = [b"Node", b"togglePanel", b""]
    pair_values, payload = [], b""
    for value in encoded_strings:
        pair_values += [24 + len(payload), len(value)]
        payload += value + b"\0"
    pairs = struct.pack("<6I", *pair_values)
    raw[strings:strings + len(pairs + payload)] = pairs + payload
    raw[record:record + 56] = struct.pack("<7Q", 0, strings, data, 0x1010, 0, 0, 0)
    raw[table_at:table_at + 2] = b"\0\0"
    module.write_bytes(raw)
    image = {"path": str(module), "sha256": R._file_hash(module)}
    for entry in B.NL.entries(recipe["root"]["lifecycle"]):
        # NL.entries returns defensive copies; update the actual recipe below.
        assert entry["module"]["path"] == str(module)
    observer = recipe["root"]["lifecycle"]
    observer["constructor"]["entry"]["module"] = copy.deepcopy(image)
    observer["constructor"]["return_sites"][0]["module"] = copy.deepcopy(image)
    observer["destructors"][0]["entry"]["module"] = copy.deepcopy(image)
    recipe["types"][0]["owner_image"] = copy.deepcopy(image)
    recipe["types"][0]["runtime_identity"]["image"] = copy.deepcopy(image)
    recipe["types"][0]["fields"] = [copy.deepcopy(recipe["types"][0]["fields"][0])]
    recipe["preconditions"] = []
    parent = module.parent
    qt_core = parent / "QtCore.dylib"
    qt_core.write_bytes(bytes(raw))
    qt_image = {"path": str(qt_core), "sha256": R._file_hash(qt_core)}
    source_dir = parent / "qt-public-source-fixture"
    source_dir.mkdir()
    source_hashes = {}
    for name in Q._SOURCES:
        path = source_dir / name
        path.write_text("// independently hashed inert Qt source fixture: " + name + "\n")
        source_hashes[name] = R._file_hash(path)
    monkeypatch.setattr(Q, "_SOURCES", source_hashes)
    files.update(qt_core=qt_core, catalog=parent / "qt-catalog.json", code=parent / "qt-code.json",
                 table=parent / "qt-table.json", qt_sources=source_dir / "manifest.json")
    manifest = {"format": "anyuse.rea-qt-moc-source-retention/1", "status": "complete",
                "qtbase_commit": Q.QT_COMMIT, "version_under_review": Q.QT_VERSION,
                "source_only": True, "downloaded_source_executed": False, "native_abi_admission": False,
                "files": {name: {"sha256": digest, "bytes": (source_dir / name).stat().st_size,
                    "status": "retained_source_bytes_only"} for name, digest in source_hashes.items()}}
    R.write_json(files["qt_sources"], manifest)
    ranges = []
    def ranged(role, at, count):
        chunk = bytes(raw[at:at + count])
        value = {"id": len(ranges), "role": role, "address": hex(at), "image_offset": hex(at),
                 "bytes": count, "original_hex": chunk.hex(), "imported_hex": chunk.hex(),
                 "original_sha256": hashlib.sha256(chunk).hexdigest(), "imported_sha256": hashlib.sha256(chunk).hexdigest()}
        ranges.append(value)
        return value["id"]
    ranged("direct_superdata_record", record, 56)
    ranged("generated_metadata_data_array", data, len(words) * 4)
    ranged("string_pairs", strings, len(pairs))
    offset = len(pairs)
    for index, value in enumerate(encoded_strings):
        ranged("string_payload_" + str(index), strings + offset, len(value) + 1)
        offset += len(value) + 1
    rows_id = ranged("method_rows", data + 56, 24)
    payload_id = ranged("method_payload_0", data + 80, 4)
    method = {"local_index": 0, "name": "togglePanel", "name_index": 1,
              "signature_as_recorded": "togglePanel()", "kind": "slot", "access": "private", "flags": "0x8",
              "parameters": [], "compatibility": False, "cloned": False, "scriptable": False,
              "is_const": False, "revision_encoded": 0, "callable_abi_admitted": False,
              "return_type": {"kind": "source_builtin_id", "id": 43, "name": "void", "encoding": "0x2b", "callable": False},
              "row_range": rows_id, "row_byte_offset": 0, "payload_range": payload_id}
    pointers = [{"slot": hex(record + 8 * word), "original": struct.pack("<Q", target).hex(),
                 "pointer_format": "DYLD_CHAINED_PTR_64_OFFSET", "relocation_status": "APPLIED", "relocation_type": 0x8888,
                 "target": hex(target), "target_image_offset": hex(target)} for word, target in ((1, strings), (2, data), (3, 0x1010))]
    catalog = {"format": "anyuse.rea-qt-moc-static-catalog/1", "status": "completed_class_local_static_catalog", "completed": True,
               "inputs_unchanged": True, "callable_abi_admitted": False, "lifetime_admitted": False, "task_proof": False,
               "global_method_indices_available": False, "source_protocol": {"qt_version": Q.QT_VERSION,
                    "qtbase_commit": Q.QT_COMMIT, "metadata_revision": 13}, "record_layout": "direct-superdata-56",
               "static_storage_layout": "qtmochelpers-contiguous-data-then-strings",
               "image": {"file": str(module), "sha256": image["sha256"], "bytes": len(raw)},
               "qt_core": {"file": str(qt_core), "sha256": qt_image["sha256"], "bytes": len(raw)},
               "source_manifest": {"file": str(files["qt_sources"]), "sha256": R._file_hash(files["qt_sources"])},
               "locator": {"image_offset": hex(record)}, "dispatcher_code_site": {"image_offset": "0x1010"},
               "class_name": "Node", "macho": {"architecture": "arm64", "cpu_subtype": 0, "image_base": "0x0"},
               "local_method_count": 1, "methods": [method], "ranges": ranges, "metadata_data_bytes": len(words) * 4,
               "header_u32": header, "string_count": 3,
               "table_pointer_authority": pointers[:2], "dispatcher_pointer_authority": pointers[2]}
    R.write_json(files["catalog"], catalog)
    chunk = bytes(raw[0x1010:0x1018])
    code = {"format": "anyuse.rea-ghidra-code-at-offsets/1", "status": "completed_static_code_observation",
            "original_file_unchanged": True, "automatic_abi_or_lifetime_admission": False, "cfg_complete_claimed": False,
            "task_proof": False, "ghidra": "12.1.4", "language": "AARCH64:LE:64:AppleSilicon",
            "image": catalog["image"], "sites": [{"image_offset": "0x1010", "status": "decompiled_static_code_observation",
                "decompile_completed": True, "cfg_complete_claimed": False,
                "body_original_ranges": [{"image_offset": "0x1010", "bytes": 8, "original_hex": chunk.hex(),
                    "sha256": hashlib.sha256(chunk).hexdigest(), "original_equals_imported": True}],
                "instructions": [{"address": hex(at), "image_offset": hex(at), "bytes": bytes(raw[at:at + 4]).hex()}
                                 for at in (0x1010, 0x1014)]}]}
    R.write_json(files["code"], code)
    table = {"format": "anyuse.rea-original-file-range/1", "status": "retained_original_file_bytes",
             "original_unchanged": True, "automatic_abi_or_lifetime_admission": False, "task_proof": False,
             "byte_order": "little", "unit_bytes": 2, "image": catalog["image"], "unsigned_units": [0],
             "range": {"image_offset": hex(table_at), "bytes": 2, "original_hex": "0000", "sha256": hashlib.sha256(b"\0\0").hexdigest()}}
    R.write_json(files["table"], table)
    selection = {"format": Q.SELECTION, "module": image, "qt_core": qt_image,
                 **{key: {"file": str(files[key]), "sha256": R._file_hash(files[key])}
                    for key in ("catalog", "code", "table", "qt_sources")},
                 "class_name": "Node", "record_offset": hex(record), "dispatcher_offset": "0x1010",
                 "local_index": 0, "signature": "togglePanel()"}
    recipe["consumer"] = {"kind": "qt_moc", "selection": copy.deepcopy(selection), "argument": 0,
                           "pointee_type": "Node", "role": "qobject_primary_base", "dispatch": "static_metacall",
                           "this_adjustment": 0, "evidence": copy.deepcopy(recipe["root_evidence"])}
    recipe["consumer"]["evidence"]["claim"]["pointer"] = "/qt_consumer"
    files["header"].write_text("// Inert independent primary-QObject declaration; not vendor/source evidence.\n"
        "struct QObject { virtual ~QObject(); }; struct Node : QObject { unsigned long opaque; };\n")
    R.write_json(files["build"], {"fixture": True, "original_image": image, "qt_core": qt_image,
                                 "abi": dict(P.ABI), "lifecycle_entries": B.NL.entries(observer)})
    _refreeze_native_path_contracts(recipe, files)
    actor = {"kind": Q.ACTOR, "receiver": {"native_path": recipe}, "selection": selection}
    B.validate_actor(actor)
    return actor, recipe, files


def _qt_moc_result(actor, resources=None):
    from test_rea_native_path import _observations, _lifecycle_result
    resources = B.native_resources(actor) if resources is None else resources
    request = B.invocation_request(actor)
    s, recipe = actor["selection"], actor["receiver"]["native_path"]
    pin = resources[s["module"]["path"]]
    identity = pin["macho_images"][0]
    section = pin["macho_sections"][0]["sections"][0]
    extent = max(int(segment["offset"], 16) + segment["bytes"] for segment in pin["macho_segments"][0]["segments"])
    consumer = Q.consumer_contract(recipe["consumer"])
    row = {"index": 0, "op": Q.OPERATION, "status": "ok", "return_type": "void", "value": None,
           "out_buffers": [], "pointer_reads": [], "swift_field_reads": [], "callable_entries": [], "compiler_completion": None,
           "native_paths": _observations(recipe),
           "function": {"target": {"module": s["module"], "offset": s["dispatcher_offset"]}, "module": s["module"]["path"],
                "image_identity": identity, "instruction_section": section, "module_size": extent, "reported_module_size": extent,
                "executable": True, "symbol": None, "implementation_offset": s["dispatcher_offset"], "abi": "default",
                "argument_types": ["pointer", "int32", "int32", "pointer"], "return_type": "void",
                "signature_evidence": recipe["consumer"]["evidence"]["claim"], "bridge": "frida-native"},
           "qt_moc": {"format": Q.OBSERVATION, "selection": s, "consumer": consumer, "call": 0, "local_index": s["local_index"],
                "argv": {**consumer["argv"], "expired": True}, "permit_consumed": True, "returned": True, "expires": "transaction"}}
    return {"format": B.WORKER, "status": "completed", "attached": True, "native_state_uncertain": False,
            "transport": {"kind": "local"}, "preloaded_gadget_remains": False, "request_sha256": R.digest(request),
            "operations": [row], "native_calls": B.minimum_request_native_calls(request), "invocations": 1, "helper_load_attempts": 0,
            "callable_resolutions": 0, "helper_residency": [], "handles_expired": True, "queue": "main", "error": None,
            "native_resources": copy.deepcopy(resources), "compiler_provenance": {},
            "cleanup": {"script_unloaded": True, "detached": True}, "cleanup_errors": [],
            "frida_lifecycle": {"format": B.FRIDA_LIFECYCLE, "manager_state": "closed", "attachment_attempted": True,
                "helper_exit_confirmed": False, "error": None},
            **({"lifecycle_observer": _lifecycle_result(recipe, for_call=True)} if P.is_lifecycle_root(recipe) else {})}


def test_qt_actual_file_collector_pins_complete_static_sources_before_one_dispatch(native_files, monkeypatch):
    actor, recipe, files = _qt_moc_files(native_files, monkeypatch)
    resources = B.native_resources(actor)
    assert set(resources) == set(B.native_file_claims(actor))
    assert resources[str(files["catalog"])]["qt_moc_dispatches"][0]["consumer"] == Q.consumer_contract(recipe["consumer"])
    assert all(row["offset"] != actor["selection"]["record_offset"] for row in resources[str(files["catalog"])]["qt_moc_dispatches"][0]["ranges"])
    request, result = B.invocation_request(actor), _qt_moc_result(actor, resources)
    assert B.minimum_request_native_calls(request) == B.minimum_native_calls(actor) == 1
    assert B._worker_result(result, R.digest(request), request, {"kind": "local"}) is result
    assert B.validate_qt_moc_result(request, result) == result["operations"][0]["qt_moc"]
    assert result["frida_lifecycle"]["helper_exit_confirmed"] is False


@pytest.mark.parametrize("literal", ['{"duplicate":1,"duplicate":2}', '{"overflow":1e309}', '{"nan":NaN}'])
def test_qt_static_json_refuses_duplicate_and_nonfinite_values_before_semantic_admission(runtime, literal):
    path = runtime[-1].parent / "bad-qt-evidence.json"
    path.write_text(literal)
    with pytest.raises(ValueError):
        B._qt_moc_json({"file": str(path), "sha256": R._file_hash(path)})


@pytest.mark.parametrize("change", ["source", "class_name", "method_flags", "access", "nonvoid", "row_bytes", "payload", "rebase", "table_pointer", "code_bytes", "table_words", "record_extent"])
def test_qt_static_plan_binds_selected_original_metadata_code_table_and_source(native_files, monkeypatch, change):
    actor, recipe, files = _qt_moc_files(native_files, monkeypatch)
    catalog, code, table, sources = (R.load_json(files[key]) for key in ("catalog", "code", "table", "qt_sources"))
    Q.static_plan(recipe["consumer"], catalog, code, table, sources)
    if change == "source": sources["files"]["qobjectdefs.h"]["sha256"] = "f" * 64
    elif change == "class_name": catalog["class_name"] = "Other"
    elif change == "method_flags": catalog["methods"][0]["flags"] = "0x28"
    elif change == "access": catalog["methods"][0]["access"] = "public"
    elif change == "nonvoid": catalog["methods"][0]["return_type"]["id"] = 2
    elif change == "row_bytes": catalog["methods"][0]["row_byte_offset"] = 4
    elif change == "payload": catalog["methods"][0]["payload_range"] = catalog["methods"][0]["row_range"]
    elif change == "rebase": catalog["dispatcher_pointer_authority"]["target_image_offset"] = "0x1014"
    elif change == "table_pointer": catalog["table_pointer_authority"].reverse()
    elif change == "code_bytes": code["sites"][0]["instructions"][0]["bytes"] = "00000000"
    elif change == "table_words": table["unsigned_units"] = [1]
    else: catalog["ranges"][0]["bytes"] = 48
    with pytest.raises(ValueError): Q.static_plan(recipe["consumer"], catalog, code, table, sources)


@pytest.mark.parametrize("change", ["incomplete", "error", "legacy", "request_digest", "extra_call", "shifted_scope", "no_qt", "vector", "missing_cleanup", "manager_pending", "false_integer", "stripped_resource", "compiler_provenance", "missing_compiler_provenance", "gadget_disposition", "missing_gadget_disposition", "missing_transport"])
def test_qt_completed_shared_gate_refuses_incoherent_retained_global_evidence(native_files, monkeypatch, change):
    actor, recipe, files = _qt_moc_files(native_files, monkeypatch)
    request, result = B.invocation_request(actor), _qt_moc_result(actor)
    B.validate_qt_moc_result(request, result)
    if change == "incomplete": result["status"] = "incomplete"
    elif change == "error": result["error"] = {"type": "Error", "message": "inert completed result still carried an error"}
    elif change == "legacy": result["format"] = B.LEGACY_WORKER
    elif change == "request_digest": result["request_sha256"] = "f" * 64
    elif change == "extra_call": result["native_calls"] = 2
    elif change == "shifted_scope":
        scope = result["operations"][0]["native_paths"][0]["lifecycle_scope"]
        scope["acquire_epoch"] = scope["checked_epoch"] = 1
    elif change == "no_qt": result["operations"][0].pop("qt_moc")
    elif change == "vector": result["operations"][0]["qt_moc"]["argv"]["slot0"] = "0x1234"
    elif change == "missing_cleanup": result.pop("cleanup")
    elif change == "manager_pending": result["frida_lifecycle"]["manager_state"] = "pending"
    elif change == "false_integer": result["operations"][0]["qt_moc"]["call"] = False
    elif change == "compiler_provenance": result["compiler_provenance"] = {"unexpected": {"artifact": "not a requested compiler adapter"}}
    elif change == "missing_compiler_provenance": result.pop("compiler_provenance")
    elif change == "gadget_disposition": result["preloaded_gadget_remains"] = True
    elif change == "missing_gadget_disposition": result.pop("preloaded_gadget_remains")
    elif change == "missing_transport": result.pop("transport")
    else: result["native_resources"].pop(str(files["code"]))
    with pytest.raises(ValueError): B.validate_qt_moc_result(request, result)


def test_qt_runtime_spec_never_adopts_old_native_invoke_or_user_argv(native_files, monkeypatch):
    actor, recipe, files = _qt_moc_files(native_files, monkeypatch)
    saved = copy.deepcopy(actor)
    assert B.bind_actor(actor, {}) == saved
    for key, value in (("argv", None), ("arguments", []), ("context", {"type": "pointer"}), ("alias", "saved")):
        changed = copy.deepcopy(actor)
        changed[key] = value
        with pytest.raises(ValueError): B.validate_actor(changed)
    old = {"kind": "native_function", "target": {"module": actor["selection"]["module"], "offset": "0x1010"},
           "abi": "default", "arguments": [{"type": "pointer", "value": {"native_path": recipe}}],
           "return_type": "void", "signature_evidence": recipe["consumer"]["evidence"]["claim"]}
    with pytest.raises(ValueError): B.validate_actor(old)
    assert actor == saved


@pytest.mark.parametrize("change", ["deleted_catalog", "changed_sdk", "changed_code", "missing_qobject_admission"])
def test_qt_resource_drift_stops_collect_before_worker_or_actor(native_files, runtime, monkeypatch, change):
    actor, recipe, files = _qt_moc_files(native_files, monkeypatch)
    app, identity, _, state, out = runtime
    if change == "deleted_catalog":
        files["catalog"].unlink()
    elif change == "changed_sdk":
        (files["qt_sources"].parent / "qobjectdefs.h").write_text("modified independent SDK file")
    elif change == "changed_code":
        files["code"].write_text("{}\n")
    else:
        review = R.load_json(files["review"])
        review["declarations"] = [row for row in review["declarations"] if row["contract"].get("format") != Q.CONSUMER]
        R.write_json(files["review"], review)
        # Keep current hashes so failure proves the missing independent Qt/base
        # declaration, rather than only a stale review file hash.
        digest = R._file_hash(files["review"])
        for wrapper in [recipe["root_evidence"], recipe["lifetime"]["evidence"], recipe["types"][0]["evidence"],
                        recipe["root"]["lifecycle"]["evidence"], recipe["consumer"]["evidence"]]:
            wrapper["originals"][-1]["sha256"] = digest
    receipt = B.collect(app, out, B.invocation_request(actor), expected_identity=identity)
    assert receipt["status"] == ("unavailable" if change == "deleted_catalog" else "incomplete")
    assert receipt["worker"] is None and state["calls"] == []
    assert receipt["actuated"] is False and receipt["native_calls"] is False and receipt["error"]


def test_qt_collect_forwards_exact_identity_and_retains_complete_receipt(native_files, runtime, monkeypatch):
    actor, _, _ = _qt_moc_files(native_files, monkeypatch)
    app, identity, _, state, out = runtime
    resources = B.native_resources(actor)
    state["result_changes"].update(_qt_moc_result(actor, resources))
    receipt = B.collect(app, out, B.invocation_request(actor), expected_identity=identity)
    assert receipt["status"] == "executed" and len(state["calls"]) == 1
    assert receipt["expected_identity"] == identity == state["calls"][0]["config"]["identity"]
    assert receipt["native_resources"] == resources and receipt["result"]["operations"][0]["qt_moc"]["returned"] is True
    assert receipt["resource_review_required"] is False and receipt["proof_created"] is False


def test_cold_static_builders_recover_metadata_and_table_from_actual_original_bytes(native_files, monkeypatch, tmp_path):
    from anyuse.execution import rea_static_artifacts as A
    actor, _, files = _qt_moc_files(native_files, monkeypatch)
    module = files["module"]
    before = module.read_bytes()
    catalog_result = A.build(module.parent, {"kind": "qt_moc_catalog", "relative": module.name,
                                             "record_offset": actor["selection"]["record_offset"]}, tmp_path / "built-catalog")
    catalog = R.load_json(catalog_result["receipt"])
    assert catalog["class_name"] == "Node" and catalog["dispatcher_offset"] == "0x1010"
    assert catalog["methods"][0]["signature_as_recorded"] == "togglePanel()"
    assert catalog["methods"][0]["supported_zero_argument_void_slot"] is True
    assert catalog["callable_abi_admitted"] is catalog["lifetime_admitted"] is catalog["task_proof"] is False
    assert catalog["imported_image_observed"] is False
    table_result = A.build(module.parent, {"kind": "original_range", "relative": module.name,
                            "offset": "0x2300", "bytes": 2, "unit_bytes": 2}, tmp_path / "built-table")
    table = R.load_json(table_result["receipt"])
    assert table["unsigned_units"] == [0] and table["range"]["original_hex"] == "0000"
    assert table["image"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert module.read_bytes() == before
    # An original metadata catalog is deliberately not an invented Ghidra receipt.
    with pytest.raises(ValueError, match="completed bounded original Qt catalog"):
        Q.static_plan(actor["receiver"]["native_path"]["consumer"], catalog,
                      R.load_json(files["code"]), table, R.load_json(files["qt_sources"]))


@pytest.mark.parametrize("offset,size,unit", [("0x1010", 4, 2), ("0x27ff", 2, 2), ("0x2300", 3, 2),
                                                ("0x2300", True, 1), ("0x2300", 2, True)])
def test_static_ranges_refuse_code_holes_invalid_units_and_boolean_sizes(native_files, monkeypatch, tmp_path, offset, size, unit):
    from anyuse.execution import rea_static_artifacts as A
    _, _, files = _qt_moc_files(native_files, monkeypatch)
    module = files["module"]
    with pytest.raises(ValueError):
        A.build(module.parent, {"kind": "original_range", "relative": module.name,
                 "offset": offset, "bytes": size, "unit_bytes": unit}, tmp_path / "refused-range")
    assert not (tmp_path / "refused-range").exists()


def test_static_builder_rejects_bundle_escape_and_original_mutation(native_files, monkeypatch, tmp_path):
    from anyuse.execution import rea_static_artifacts as A
    _, _, files = _qt_moc_files(native_files, monkeypatch)
    module = files["module"]
    with pytest.raises(ValueError):
        A.build(module.parent, {"kind": "original_range", "relative": "../outside",
                 "offset": "0x2300", "bytes": 2, "unit_bytes": 2}, tmp_path / "escape")
    image = A.OriginalImage(module.parent, module.name)
    try:
        raw = bytearray(module.read_bytes()); raw[-1] ^= 1; module.write_bytes(raw)
        with pytest.raises(ValueError, match="changed"):
            image.read(0x2300, 2)
    finally:
        image.close()


def test_qt_source_protocol_is_reused_without_per_app_source_preparation(native_files, monkeypatch, tmp_path):
    from anyuse.execution import rea_qt_sources as S, rea_static_artifacts as A
    actor, _, files = _qt_moc_files(native_files, monkeypatch)
    monkeypatch.setenv(S.CACHE_ENV, str(files["qt_sources"].parent))
    result = A.build(files["module"].parent, {"kind": "qt_sources"}, tmp_path / "retained-sdk")
    assert result["status"] == "retained_qt_source_protocol" and len(result["resources"]) == 7
    selection = copy.deepcopy(actor["selection"])
    selection["qt_sources"] = {"file": result["receipt"], "sha256": R._file_hash(Path(result["receipt"]))}
    claims = Q.source_claims(selection, R.load_json(result["receipt"]))
    assert len(claims) == 7 and all(R._file_hash(Path(file)) == digest for file, digest in claims.items())
    assert result["source_execution"] is result["callable_abi_admitted"] is result["proof_created"] is False


def test_qt_source_cache_missing_is_a_concrete_capability_result_without_network(tmp_path, monkeypatch):
    from anyuse.execution import rea_qt_sources as S, rea_static_artifacts as A
    monkeypatch.setenv(S.CACHE_ENV, str(tmp_path / "missing-sdk"))
    result = A.build(tmp_path, {"kind": "qt_sources"}, tmp_path / "missing-evidence")
    assert result["status"] == "needs_capability" and result["reason"] == "qt_source_cache_unavailable"
    receipt = R.load_json(result["receipt"])
    assert receipt["expected_commit"] == Q.QT_COMMIT and receipt["source_execution"] is False


def test_qt_source_setup_downloads_only_fixed_public_paths_and_checks_every_hash(native_files, monkeypatch, tmp_path):
    import httpx
    from anyuse.execution import rea_qt_sources as S
    _, _, files = _qt_moc_files(native_files, monkeypatch)
    originals = {name: (files["qt_sources"].parent / name).read_bytes() for name in Q._SOURCES}
    requested = []
    def fetch(request):
        requested.append(str(request.url))
        assert request.method == "GET" and request.content == b""
        assert request.url.host == "raw.githubusercontent.com" and Q.QT_COMMIT in request.url.path
        return httpx.Response(200, content=originals[Path(request.url.path).name])
    result = S.setup(tmp_path / "new-sdk-cache", transport=httpx.MockTransport(fetch))
    assert result["status"] == "complete" and len(requested) == 7 and result["source_execution"] is False
    with pytest.raises(ValueError, match="never overwritten"):
        S.setup(tmp_path / "new-sdk-cache", transport=httpx.MockTransport(fetch))
    assert len(requested) == 7
    with pytest.raises(ValueError, match="SHA-256"):
        S.setup(tmp_path / "corrupt-sdk-cache", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"corrupt")))
    assert not (tmp_path / "corrupt-sdk-cache/manifest.json").exists()
    assert R.load_json(tmp_path / "corrupt-sdk-cache/failure.json")["status"] == "incomplete"


def test_qt_static_assembly_produces_admissible_static_files_without_manufacturing_receiver_evidence(native_files, monkeypatch, tmp_path):
    from anyuse.execution import rea_static_artifacts as A
    actor, _, files = _qt_moc_files(native_files, monkeypatch)
    root = files["module"].parent
    original = A.build(root, {"kind": "qt_moc_catalog", "relative": files["module"].name,
                             "record_offset": "0x2080"}, tmp_path / "original-catalog")
    metadata = R.load_json(original["receipt"])
    old = R.load_json(files["catalog"])
    code = R.load_json(files["code"])
    record = next(row for row in old["ranges"] if row["role"] == "direct_superdata_record")
    # Explicit inert stand-ins for the separate Ghidra producer. The builder
    # must match these imported observations against actual original bytes.
    code["records"] = [{"image_offset": "0x2080", "imported_hex": record["imported_hex"],
                        "pointers": old["table_pointer_authority"] + [old["dispatcher_pointer_authority"]]}]
    code["data_ranges"] = [{"image_offset": row["image_offset"], "bytes": row["bytes"],
                            "imported_hex": row["original_hex"]} for row in metadata["ranges"]]
    code_file = root / "inert-independent-code.json"; R.write_json(code_file, code)
    paths = [Path(original["receipt"]), code_file, files["table"], files["qt_sources"]]
    artifacts = {p.relative_to(root).as_posix(): {"file": str(p), "sha256": R._file_hash(p), "bytes": p.stat().st_size} for p in paths}
    names = list(artifacts)
    result = A.build(root, {"kind": "qt_selection", "catalog_artifact": names[0], "code_artifact": names[1],
        "table_artifact": names[2], "sources_artifact": names[3], "qt_core_relative": files["qt_core"].name,
        "local_index": 0}, tmp_path / "assembled-selection", artifacts=artifacts, session=root)
    selection = result["selection"]
    plan = Q.static_selection_plan(selection, R.load_json(selection["catalog"]["file"]), code,
                                  R.load_json(files["table"]), R.load_json(files["qt_sources"]))
    assert plan["method"]["name"] == "togglePanel" and plan["original_ranges"]
    assert result["status"] == "static_selection_retained" and result["callable_abi_admitted"] is False
    with pytest.raises(ValueError):
        Q.static_plan({"selection": selection, "pointee_type": "Node"}, {}, {}, {}, {})


def test_fixed_ghidra_builder_reports_missing_tool_before_import_or_app_execution(native_files, monkeypatch, tmp_path):
    from anyuse.execution import rea_static_artifacts as A
    monkeypatch.setenv("GHIDRA_INSTALL_DIR", str(tmp_path / "missing-ghidra"))
    result = A.build(tmp_path, {"kind": "ghidra_code", "relative": "not-opened.dylib",
                              "entry_offsets": ["0x1000"], "record_offsets": []}, tmp_path / "missing-ghidra-evidence")
    assert result["status"] == "needs_capability" and result["reason"] == "configured_ghidra_unavailable"
    assert result["proof_created"] is False


def test_every_static_builder_example_is_strict_json_and_matches_its_pure_request_validator():
    from anyuse.execution import rea_static_artifacts as A
    guide = A.agent_guide()
    for example in guide["examples"]:
        assert A.validate_request(json.loads(json.dumps(example))) == example
    assert {row["kind"] for row in guide["examples"]} == set(A.KINDS)


def test_qt_static_and_resource_admission_accept_existing_fresh_borrowed_getter_family(native_files, monkeypatch):
    from test_rea_runtime_bridge import _refreeze_native_path_contracts
    actor, recipe, files = _qt_moc_files(native_files, monkeypatch)
    getter = copy.deepcopy(native_files[1])
    getter["target"]["module"] = copy.deepcopy(actor["selection"]["module"])
    getter["signature_evidence"] = {"kind": "reviewed_contract", "file": str(files["getter_abi"]),
        "sha256": R._file_hash(files["getter_abi"]), "pointer": "/getter"}
    recipe["root"] = {"getter": getter}
    recipe["lifetime"]["mode"] = P.BORROWED_ROOT_MODE
    _refreeze_native_path_contracts(recipe, files)
    actor["receiver"]["native_path"] = recipe
    assert B.validate_actor(actor) == actor
    resources = B.native_resources(actor)
    assert actor["selection"]["catalog"]["file"] in resources
    assert getter["signature_evidence"]["file"] in resources
    request, result = B.invocation_request(actor), _qt_moc_result(actor, resources)
    assert B.minimum_request_native_calls(request) == result["native_calls"] == 2
    assert B._worker_result(result, R.digest(request), request, {"kind": "local"}) is result
    assert B.validate_qt_moc_result(request, result) == result["operations"][0]["qt_moc"]
    for wrong_count in (0, 1, 3):
        result["native_calls"] = wrong_count
        with pytest.raises(ValueError):
            B.validate_qt_moc_result(request, result)


def test_qt_cache_follows_shared_data_resolver_and_keeps_its_explicit_override(tmp_path, monkeypatch):
    from anyuse.execution import rea_qt_sources as sources
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(sources.CACHE_ENV, raising=False)
    assert sources.default_cache() == tmp_path / "data" / "rea-qt-sources" / Q.QT_COMMIT
    monkeypatch.setenv(sources.CACHE_ENV, str(tmp_path / "explicit-qt-cache"))
    assert sources.default_cache() == tmp_path / "explicit-qt-cache"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
