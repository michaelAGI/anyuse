"""Shared learned-recipe regressions; inert bundles/AX recordings, no GUI."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import sys
import tempfile

import pytest

_HERE = Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-rea-harness-pkg-")
(Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_harness as H
from anyuse.execution import surface_discovery as S
from test_rea_runtime_bridge import runtime as qt_moc_bridge_runtime
_H_RUNTIME = H.Runtime


def objc_actor(query, reference):
    return {"kind": "objc_method", "receiver": {"class": "MappedReader", "kind": "unique", "subclasses": False},
            "selector": "readResult:reference:enabled:", "signature": "v40@0:8@16@24B32",
            "arguments": [{"type": "utf8", "value": query}, {"type": "utf8", "value": reference},
                          {"type": "bool", "value": True}], "return_type": "void"}


def native_context_identity(app, candidate):
    """Complete inert NS process identity for the ordinary runtime's PID."""
    return {"bundle": str(app.resolve()), "bundle_id": candidate["target"]["bundle_id"],
            "executable": str(app.resolve() / "Contents/MacOS/Unseen"),
            "launch_time": 1000.25, "pid": 443, "uid": os.getuid()}


def objc_sequence(query, reference, number, animate):
    return {"kind": "objc_sequence", "steps": [objc_actor(query, reference),
            {"kind": "objc_method", "receiver": {"class": "MappedReader", "kind": "unique", "subclasses": False},
             "selector": "commitCounter:animated:", "signature": "v24@0:8i16B20", "return_type": "void",
             "arguments": [{"type": "int32", "value": number}, {"type": "bool", "value": animate}]}]}


def inspector_cases(fixture, tmp_path, monkeypatch):
    """Typed declarations and operator metadata only; no running inspector."""
    from anyuse.execution import rea_inspector_bridge as I
    app, _, _, demos = fixture
    actor = {"kind": "inspector_method", "receiver": {"root": {"kind": "global", "name": "mappedState"},
             "steps": [{"kind": "property", "name": "current", "access": "data"}]},
             "method": "readResult", "function_sha256": hashlib.sha256(b"independently reviewed method body").hexdigest(),
             "arguments": [{"type": "utf8", "value": "query"}, {"type": "utf8", "value": "reference"},
                           {"type": "bool", "value": True}], "return_type": "void"}
    declaration = (tmp_path / "inspector-contract.json").resolve()
    R.write_json(declaration, {"declarations": {"operation": I.call_contract(actor)}})
    actor["signature_evidence"] = {"kind": "reviewed_js", "file": str(declaration),
        "sha256": R._file_hash(declaration), "pointer": "/declarations/operation"}
    config = (tmp_path / "inspector-config.json").resolve()
    R.write_json(config, {"format": I.TRANSPORT, "endpoint": "http://127.0.0.1:9246",
        "relative_url": "Contents/Resources/result.html", "target": R.fingerprint(app)})
    config.chmod(0o600)
    monkeypatch.setenv("ANYUSE_REA_INSPECTOR_CONFIG", str(config))
    for demo in demos:
        recorded = copy.deepcopy(actor)
        recorded["arguments"][0]["value"] = demo["inputs"]["query"]
        recorded["arguments"][1]["value"] = demo["inputs"]["reference"]
        demo["invocation"] = recorded
    return actor


def inspector_receipt(c, invocation, *, pid=443, uid=None, bundle="/tmp/Unseen.app"):
    """Complete inert inspector envelope, separate from the independent AX tree."""
    from anyuse.execution import rea_inspector_bridge as I
    uid = os.getuid() if uid is None else uid
    pinned = H.resources(c)["inspector_bridge"]
    request = I.invocation_request(invocation)
    identity = {"bundle_id": c["target"]["bundle_id"], "bundle": bundle, "pid": pid,
                "launch_time": 1000.25, "uid": uid, "executable": str(Path(bundle) / "Contents/MacOS/Unseen")}
    url = (Path(bundle) / pinned["transport"]["relative_url"]).as_uri()
    suffix = pinned["transport"].get("document_suffix", {"query": "", "fragment": ""})
    if suffix["query"]:
        url += "?" + suffix["query"]
    if suffix["fragment"]:
        url += "#" + suffix["fragment"]
    return {"format": I.FORMAT, "status": "executed", "target": copy.deepcopy(c["target"]), "bundle": bundle,
        "request": request, "request_sha256": R.digest(request), "adapter": copy.deepcopy(pinned["adapter"]),
        "protocol_limits": copy.deepcopy(pinned["adapter"]["protocol_limits"]),
        "heap_query_effects": {"queries_attempted": 0, "heap_scan_possible": False,
            "forced_gc_possible": False, "objects_retained_until_group_release": False,
            "server_side_candidate_bound": False},
        "instance_selection_modes": [],
        "transport": copy.deepcopy(pinned["transport"]), "identity": identity, "after_identity": copy.deepcopy(identity),
        "state_uncertain": False, "resource_review_required": False, "proof_created": False,
        "task_result_verified": False, "retry": False, "actuated": True, "model_calls": 0,
        "method_dispatch_attempts": 1, "getter_dispatch_attempts": sum(
            step["kind"] == "property" and step["access"] == "getter" for step in invocation["receiver"]["steps"]),
        "cleanup": {"object_group_released": True, "page_socket_closed": True, "browser_socket_closed": True,
            "requested_evaluations_completed": True, "remote_execution_cancelled": False, "local_live_ids_discarded": True},
        "declaration_resources": I.resources(invocation),
        "endpoint_identity": {"listener": {"pid": pid, "uid": uid, "listen_addresses": ["127.0.0.1:9246"]},
            "target": {"id": "inert-renderer", "type": "page", "url": url},
            "renderer_binding_scope": "selected exact app URL plus owned app descendant renderer set",
            "execution_context": {"unique_id": "inert-context", "numeric_id": 11, "frame_id": "inert-frame",
                "frame_url": url, "is_default": True},
            "renderer_processes": [{"pid": pid + 1, "ppid": pid, "uid": uid, "started_at": "inert-start-time",
                "executable": str(Path(bundle) / "Contents/Frameworks/Renderer")}]},
        "operations": [{"op": "call", "method": invocation["method"], "function_sha256": invocation["function_sha256"],
            "value": None, "return_type": invocation["return_type"], "completed": True, "body_entry_independently_proved": False}]}


def native_metadata_image(module, *, split_segments=False):
    """Inert synthetic file metadata; never loaded as an image or executed."""
    uuid = struct.pack("<II", 0x1B, 24) + bytes.fromhex("0123456789abcdef0123456789abcdef")
    segment = struct.pack("<II16sQQQQiiII", 0x19, 152, b"__TEXT", 0, 0x2000, 0, 0x1020, 7, 5, 1, 0)
    section = struct.pack("<16s16sQQIIIIIIII", b"__text", b"__TEXT", 0x1000, 0x20,
                          0x1000, 2, 0, 0, 0x80000400, 0, 0, 0)
    commands, count, file_bytes = uuid + segment + section, 2, 0x1020
    if split_segments:
        commands += struct.pack("<II16sQQQQiiII", 0x19, 72, b"__DATA_CONST", 0x4000, 0x1000, 0x1020, 0x1000, 3, 1, 0, 0)
        commands += struct.pack("<II16sQQQQiiII", 0x19, 72, b"__DATA", 0x8000, 0x1000, 0x2020, 0x1000, 3, 3, 0, 0)
        count, file_bytes = 4, 0x3020
    metadata = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 6, count, len(commands), 0, 0) + commands
    module.write_bytes(metadata + b"\0" * (file_bytes - len(metadata)))


def native_template(tmp_path, argument_types=None, values=None, *, abi="default", return_type="void", context=None):
    """Inert module and attributable contract; no native code or runtime tools."""
    types = argument_types if argument_types is not None else ["utf8", "utf8"]
    values = values if values is not None else ["query", "reference"]
    module = (tmp_path / "native-image.bin").resolve()
    if not module.exists():
        native_metadata_image(module)
    contract = {"abi": abi, "argument_types": copy.deepcopy(types), "return_type": copy.deepcopy(return_type),
                "context": context is not None, "pointer_policy": "borrowed_no_escape"}
    evidence = (tmp_path / f"native-contract-{R.digest(contract)[:12]}.json").resolve()
    if not evidence.exists():
        R.write_json(evidence, {"declarations": {"operation": contract}})
    actor = {"kind": "native_function", "target": {"module": {"path": str(module), "sha256": R._file_hash(module)},
              "symbol": "inert_read_result"}, "abi": abi,
             "arguments": [{"type": copy.deepcopy(kind), "value": copy.deepcopy(value)} for kind, value in zip(types, values)],
             "return_type": copy.deepcopy(return_type), "signature_evidence": {"kind": "reviewed_contract", "file": str(evidence),
                 "sha256": R._file_hash(evidence), "pointer": "/declarations/operation"}}
    if context is not None:
        actor["context"] = copy.deepcopy(context)
    return actor


def native_cases(fixture, tmp_path, *, compound=False, mixed=False):
    profile, inputs, demos = fixture[1:]
    types = ["utf8", "utf8"]
    values = ["query", "reference"]
    if compound:
        types += [{"kind": "struct", "fields": [{"name": "counter", "type": "int32"}, {"name": "fixed", "type": "int32"}]},
                  {"kind": "buffer", "bytes": 2, "direction": "in"}]
        values += [{"counter": 10, "fixed": 17}, [0, 255]]
        profile["params"] += [{"name": "counter", "type": "string", "source": "name"},
                              {"name": "buffer_hex", "type": "string", "source": "text"}]
        for i, row in enumerate(inputs):
            row.update(counter=str(10 + i), buffer_hex=f"{i:02x}{255-i:02x}")
    template = native_template(tmp_path, types, values)
    for demo in demos:
        actor = copy.deepcopy(template)
        row = demo["inputs"]
        actor["arguments"][0]["value"] = row["query"]
        actor["arguments"][1]["value"] = row["reference"]
        if compound:
            actor["arguments"][2]["value"]["counter"] = int(row["counter"])
            actor["arguments"][3]["value"] = list(bytes.fromhex(row["buffer_hex"]))
        demo["invocation"] = {"kind": "runtime_sequence", "steps": [objc_actor(row["query"], row["reference"]), actor]} if mixed else actor
    return template


def native_pointer_read(tmp_path, *, receiver_class="MappedReader", owner_class=None):
    module = native_template(tmp_path)["target"]["module"]
    field = {"class": owner_class or receiver_class, "name": "_nativeState", "encoding": "^v", "offset": 16}
    contract = {"receiver_class": receiver_class, "field": field, "pointer_policy": "borrowed_no_escape"}
    evidence = (tmp_path / f"borrowed-read-{R.digest(contract)[:12]}.json").resolve()
    R.write_json(evidence, {"declarations": {"borrowed": contract}})
    return {"receiver": {"class": receiver_class, "kind": "unique", "subclasses": False},
            "receiver_class": receiver_class, "field": field, "module": copy.deepcopy(module),
            "evidence": {"kind": "reviewed_contract", "file": str(evidence), "sha256": R._file_hash(evidence), "pointer": "/declarations/borrowed"}}


def pointer_read_cases(fixture, tmp_path, *, nested=False):
    recipe = native_pointer_read(tmp_path, receiver_class="DerivedMappedReader", owner_class="BaseMappedReader")
    value = {"read": copy.deepcopy(recipe)}
    if nested:
        getter = native_template(tmp_path, ["pointer"], [value], return_type="pointer")
        getter["target"]["symbol"] = "inert_borrowed_substate"
        value = {"getter": getter}
    actor = native_template(tmp_path, ["pointer", "utf8", "utf8"], [value, "query", "reference"])
    for demo in fixture[3]:
        trace = copy.deepcopy(actor)
        trace["arguments"][1]["value"] = demo["inputs"]["query"]
        trace["arguments"][2]["value"] = demo["inputs"]["reference"]
        demo["invocation"] = trace
    return recipe


def native_swift_read(tmp_path, *, receiver_class="MappedModule.MappedReader"):
    module = native_template(tmp_path)["target"]["module"]
    read = {"receiver": {"class": receiver_class, "kind": "unique", "subclasses": False},
            "receiver_class": receiver_class, "swift_class": {"module": "MappedModule", "name": "MappedReader"},
            "field": {"name": "nativeState", "type_name": "Swift.Optional<Swift.OpaquePointer>", "offset": 16},
            "module": copy.deepcopy(module), "preconditions": [{"name": "closed", "type_name": "Swift.Bool", "equals": False}]}
    contract = {key: copy.deepcopy(read[key]) for key in ("receiver_class", "swift_class", "field", "preconditions")}
    contract.update(pointer_policy="borrowed_no_escape", queue="main")
    evidence = (tmp_path / f"swift-read-{R.digest(contract)[:12]}.json").resolve()
    R.write_json(evidence, {"declarations": {"borrowed": contract}})
    read["evidence"] = {"kind": "reviewed_contract", "file": str(evidence), "sha256": R._file_hash(evidence), "pointer": "/declarations/borrowed"}
    return read


def compiler_cases(fixture, tmp_path, monkeypatch, *, family="swift_double_bool_context"):
    """Inert compiler-collector fixture; A's actual verifier has its own regressions."""
    from anyuse.execution import rea_runtime_bridge as B
    profile, inputs, demos = fixture[1:]
    original_module = (tmp_path / "original-native-image.bin").resolve()
    native_metadata_image(original_module)
    declaration = (tmp_path / "original-callable.json").resolve()
    original = {"module": {"path": str(original_module), "sha256": R._file_hash(original_module)}, "symbol": "inert_private_entry",
                "abi": "swift", "argument_types": ["double", "bool"], "return_type": "double", "context": "swiftself",
                "pointer_policy": "borrowed_no_escape",
                "signature_evidence": {"kind": "reviewed_contract", "file": str(declaration), "sha256": "0" * 64, "pointer": "/operation"}}
    R.write_json(declaration, {"operation": B.callable_contract(original)})
    original["signature_evidence"]["sha256"] = R._file_hash(declaration)
    self_recipe = {"objc": {"class": "MappedReader", "kind": "unique", "subclasses": False}}
    if family == "cxx_owned_string_vector_double":
        original.update(abi="default", context="cxx_this",
                        argument_types=[{"kind": "cxx_owned", "name": name, "library": "libc++", "abi_version": 1}
                                        for name in ("std::string", "std::vector<double>")])
        R.write_json(declaration, {"operation": B.callable_contract(original)})
        original["signature_evidence"]["sha256"] = R._file_hash(declaration)
        profile["params"] += [{"name": "count", "type": "string", "source": "name"}, {"name": "packed", "type": "string", "source": "text"}]
        for i, row in enumerate(inputs):
            row.update(count=str(i % 4), packed=struct.pack("<4d", *[float(i + j) for j in range(4)]).hex())
        actor = native_template(tmp_path, ["pointer", "pointer", "utf8", "uint32",
            {"kind": "buffer", "bytes": 32, "direction": "in"}, {"kind": "buffer", "bytes": 24, "direction": "out"}],
            [{"callable": original}, self_recipe, "query", 0, list(bytes.fromhex(inputs[0]["packed"])), None], return_type="uint32")
    elif family == "cxx_owned_string_bytes":
        string = {"kind": "cxx_owned", "name": "std::string", "library": "libc++", "abi_version": 1}
        original.update(abi="default", context="cxx_this", argument_types=[copy.deepcopy(string)], return_type=copy.deepcopy(string))
        R.write_json(declaration, {"operation": B.callable_contract(original, allow_owned_return=True)})
        original["signature_evidence"]["sha256"] = R._file_hash(declaration)
        actor = native_template(tmp_path, ["pointer", "pointer", "utf8",
            {"kind": "buffer", "bytes": 16, "direction": "out"}, {"kind": "buffer", "bytes": 24, "direction": "out"}],
            [{"callable": original}, self_recipe, "query", None, None], return_type="uint32")
    else:
        profile["params"] += [{"name": "amount", "type": "string", "source": "name"}, {"name": "enabled", "type": "string", "source": "text"}]
        for i, row in enumerate(inputs):
            row.update(amount=str(10 + i), enabled="true" if i % 2 == 0 else "false")
        actor = native_template(tmp_path, ["pointer", "pointer", "double", "bool", {"kind": "buffer", "bytes": 24, "direction": "out"}],
                                [{"callable": original}, self_recipe, 10.0, True, None], return_type="uint32")
    build = (tmp_path / "compiler-build.json").resolve()
    actor["compiler_adapter"] = {"family": family, "evidence": {"kind": "compiler", "file": str(build), "sha256": "0" * 64, "pointer": "/contract"},
                                 "completion_argument": len(actor["arguments"]) - 1}
    artifacts = []
    def add(role, file):
        artifacts.append({"file": str(file), "sha256": R._file_hash(file), "bytes": file.stat().st_size, "roles": [role]})
    add("generated_helper", Path(actor["target"]["module"]["path"]))
    add("outer_declaration", Path(actor["signature_evidence"]["file"]))
    add("original_module", original_module)
    add("original_declaration", declaration)
    for role in ("generated_source", "generated_ir", "toolchain_observation", "original_source", "original_ir", "original_build",
                 "original_compiler", "toolchain_compiler", "toolchain_sdk_settings", "toolchain_policy", "generator_verifier_source"):
        file = (tmp_path / (role + ".txt")).resolve()
        file.write_text("Inert compiler evidence role: " + role)
        add(role, file)
    for role, name, content in (("compiler_stdout", "compiler.stdout", ""), ("compiler_stderr", "compiler.stderr", ""),
                               ("compiler_layout_probe", "abi-probe.ll", "Inert compiler layout IR")):
        file = (tmp_path / name).resolve()
        file.write_text(content)
        add(role, file)
    include = tmp_path / "headers"
    include.mkdir()
    for name in ("string", "vector", "__config"):
        file = (include / name).resolve()
        file.write_text("Inert SDK header: " + name)
        add("libcxx_header", file)
    R.write_json(build, {"contract": B.compiler_adapter_contract(actor), "artifacts": artifacts})
    actor["compiler_adapter"]["evidence"]["sha256"] = R._file_hash(build)
    def compiler_resources(evidence):
        if evidence != actor["compiler_adapter"]["evidence"] or R._file_hash(build) != evidence["sha256"]:
            raise ValueError("compiler manifest changed")
        pins = {str(build): {"file": str(build), "sha256": evidence["sha256"], "bytes": build.stat().st_size}}
        for row in artifacts:
            if R._file_hash(Path(row["file"])) != row["sha256"]:
                raise ValueError("compiler transitive artifact changed")
            pins[row["file"]] = {key: row[key] for key in ("file", "sha256", "bytes")}
        return pins
    monkeypatch.setattr(B, "_compiler_resources", compiler_resources)
    for demo in demos:
        demo["invocation"] = copy.deepcopy(actor)
        if family == "cxx_owned_string_vector_double":
            demo["invocation"]["arguments"][2]["value"] = demo["inputs"]["query"]
            demo["invocation"]["arguments"][3]["value"] = int(demo["inputs"]["count"])
            demo["invocation"]["arguments"][4]["value"] = list(bytes.fromhex(demo["inputs"]["packed"]))
        elif family == "cxx_owned_string_bytes":
            demo["invocation"]["arguments"][2]["value"] = demo["inputs"]["query"]
        else:
            demo["invocation"]["arguments"][2]["value"] = float(demo["inputs"]["amount"])
            demo["invocation"]["arguments"][3]["value"] = demo["inputs"]["enabled"] == "true"
    return actor


def swift_read_cases(fixture, tmp_path, *, nested=False):
    recipe = native_swift_read(tmp_path)
    value = {"swift_read": copy.deepcopy(recipe)}
    if nested:
        getter = native_template(tmp_path, ["pointer"], [value], return_type="pointer")
        getter["target"]["symbol"] = "inert_swift_borrowed_substate"
        value = {"getter": getter}
    actor = native_template(tmp_path, ["pointer", "utf8", "utf8"], [value, "query", "reference"])
    for demo in fixture[3]:
        demo["invocation"] = copy.deepcopy(actor)
        demo["invocation"]["arguments"][1]["value"] = demo["inputs"]["query"]
        demo["invocation"]["arguments"][2]["value"] = demo["inputs"]["reference"]
    return recipe


def native_path_cases(fixture, tmp_path, *, root_kind="getter", receiver_path=None):
    """Shared inert source/admission files with real resource collection, no native code."""
    from test_rea_runtime_bridge import _native_path_files
    base = native_template(tmp_path)
    getter = native_template(tmp_path, [], [], return_type="pointer")
    getter["target"]["symbol"] = "inert_path_root"
    native_files = (base, getter, Path(base["target"]["module"]["path"]), (tmp_path / "native-path-outer-abi.json").resolve())
    actor, recipe, files = _native_path_files(native_files, root_kind=root_kind, receiver_path=receiver_path, return_type="void",
        arguments=[{"type": "utf8", "value": "query"}, {"type": "utf8", "value": "reference"}])
    for demo in fixture[3]:
        demo["invocation"] = copy.deepcopy(actor)
        demo["invocation"]["arguments"][1]["value"] = demo["inputs"]["query"]
        demo["invocation"]["arguments"][2]["value"] = demo["inputs"]["reference"]
    return actor, recipe, files


def swift_field_layout(*, boolean=False):
    return {"metadata_kind": 0x200 if boolean else 0x202, "bytes": 1 if boolean else 8,
            "stride": 1 if boolean else 8, "alignment_mask": 0 if boolean else 7, "flags": 0 if boolean else 7,
            "pod": True, "bitwise_takable": True, "copyable": True, "bitwise_borrowable": True,
            "addressable_for_dependencies": False, "complete": True, "canonical_type_verified": True,
            "encoding": "bool_byte" if boolean else "zero_is_none"}


def sequence_cases(fixture):
    from anyuse.execution import rea_runtime_bridge as B
    profile, inputs, demos = fixture[1:]
    profile["params"] += [{"name": "counter", "type": "string", "source": "name"},
                          {"name": "animate", "type": "string", "source": "text"}]
    for i, row in enumerate(inputs):
        row.update(counter=str(10 + i), animate="true" if i % 2 == 0 else "false")
    for demo in demos:
        row = demo["inputs"]
        demo["invocation"] = objc_sequence(row["query"], row["reference"],
                                          B.convert_binding("int32", row["counter"]), B.convert_binding("bool", row["animate"]))


def mutable_cases(fixture, tmp_path):
    profile, inputs, demos = fixture[1:]
    working = (tmp_path / "working.txt").resolve()
    working.write_text("Original working document")
    profile["params"].append({"name": "working", "type": "path", "source": "path", "mutable": True})
    profile["reference"]["fields"]["identity"] = {"kind": "file_identity", "param": "working"}
    for row in inputs:
        row["working"] = str(working)
    for demo in demos:
        demo["after"][0]["AXDocument"] = working.as_uri()
    return working


def objc_adapter():
    from anyuse.execution import rea_runtime_bridge as B
    source = R._file_hash(Path(B.__file__).with_name("rea_runtime_agent.js"))
    return {"file": "/tmp/fixed-runtime.js", "sha256": "1" * 64, "bytes": 100,
            "source_sha256": source,
            "manifest": {"file": "/tmp/fixed-runtime.js.manifest.json", "sha256": "2" * 64, "bytes": 100},
            "build": {"format": "anyuse.rea-runtime-adapter/1", "source_sha256": source, "bundle_sha256": "1" * 64}}


def objc_receipt(c, invocation, pid=443):
    from anyuse.execution import rea_runtime_bridge as B
    request = B.invocation_request(invocation)
    identity = {"bundle_id": c["target"]["bundle_id"], "bundle": "/tmp/Unseen.app", "pid": pid,
                "launch_time": 1000.25, "uid": 501, "executable": "/tmp/Unseen.app/Contents/MacOS/Unseen"}
    methods = B.actor_calls(invocation)
    native_pins = B.native_resources(invocation) if H._native_file_claims(invocation) else {}
    provenance = B.request_compiler_provenance(request)
    rows = []
    for index, actor in enumerate(methods):
        if actor["kind"] == "native_function":
            function = {"target": copy.deepcopy(actor["target"]), "abi": actor["abi"],
                        "argument_types": [copy.deepcopy(a["type"]) for a in actor["arguments"]],
                        "return_type": copy.deepcopy(actor["return_type"]), "signature_evidence": copy.deepcopy(actor["signature_evidence"]),
                        "module": actor["target"]["module"]["path"], "module_size": 0x2000,
                        "implementation_offset": actor["target"].get("offset", "0x1000"), "executable": True,
                        "symbol": actor["target"].get("symbol"), "bridge": "frida-swift" if actor["abi"] == "swift" else "frida-native"}
            function["image_identity"] = copy.deepcopy(native_pins[actor["target"]["module"]["path"]]["macho_images"][0])
            function["instruction_section"] = copy.deepcopy(native_pins[actor["target"]["module"]["path"]]["macho_sections"][0]["sections"][0])
            segments = native_pins[actor["target"]["module"]["path"]]["macho_segments"][0]["segments"]
            function["module_size"] = max(int(segment["offset"], 16) + segment["bytes"] for segment in segments)
            function["reported_module_size"] = 0x2000
            outputs = [{"argument": i, "bytes": [0] * a["type"]["bytes"]} for i, a in enumerate(actor["arguments"])
                       if isinstance(a["type"], dict) and a["type"].get("kind") == "buffer" and a["type"].get("direction") in {"out", "inout"}]
            pointer_reads = []
            for handle, recipe in enumerate(B.pointer_read_recipes(actor)):
                offset = recipe["field"].get("offset", 16)
                pointer_reads.append({"recipe": copy.deepcopy(recipe), "receiver_class": recipe["receiver_class"],
                    "field": {**{key: recipe["field"][key] for key in ("class", "name", "encoding")},
                        "module": recipe["module"]["path"], "offset": str(offset)},
                    "image_identity": copy.deepcopy(native_pins[recipe["module"]["path"]]["macho_images"][0]),
                    "instance_bytes": offset + 16,
                    "pointer": {"handle": f"n{handle}", "alias": None, "ownership": "borrowed_no_escape", "expires": "transaction"}})
            swift_reads = []
            for handle, recipe in enumerate(B.swift_read_recipes(actor)):
                offset = recipe["field"].get("offset", 16)
                preconditions = recipe.get("preconditions", [])
                segments = native_pins[recipe["module"]["path"]]["macho_segments"][0]["segments"]
                extent = max(int(segment["offset"], 16) + segment["bytes"] for segment in segments)
                swift_reads.append({"recipe": copy.deepcopy(recipe), "source": "swift_metadata",
                    "receiver_class": recipe["receiver_class"], "swift_class": copy.deepcopy(recipe["swift_class"]),
                    "field": {"name": recipe["field"]["name"], "type_name": recipe["field"]["type_name"],
                        "offset": offset, "index": 0, "num_fields": len(preconditions) + 1, "vector_offset_words": 7,
                        "descriptor_offset": "0x100", "mangled_type_offset": "0x120", "mangled_type_bytes": 24},
                    "image_identity": copy.deepcopy(native_pins[recipe["module"]["path"]]["macho_images"][0]),
                    "module_size": extent, "reported_module_size": 0x2000, "instance_bytes": offset + 16 + len(preconditions), "layout": swift_field_layout(),
                    "class_size": 128, "class_address_point": 16,
                    "preconditions": [{"name": field["name"], "type_name": field["type_name"], "offset": offset + 8 + i, "index": i + 1,
                        "layout": swift_field_layout(boolean=True), "raw_byte": int(field["equals"]), "value": field["equals"], "expected": field["equals"]}
                        for i, field in enumerate(preconditions)],
                    "pointer": {"handle": f"n{handle}", "alias": None, "ownership": "borrowed_no_escape", "expires": "transaction"},
                    "lifetime": {"receiver_retained": True, "queue": "main", "native_handle_retained": False}})
            callables = []
            for handle, recipe in enumerate(B.callable_recipes(actor)):
                target = B.callable_contract(recipe,
                    allow_owned_return=actor.get("compiler_adapter", {}).get("family") == "cxx_owned_string_bytes")["entry"]
                pin = native_pins[target["module"]["path"]]
                segments = pin["macho_segments"][0]["segments"]
                entry = {"target": target, "module": target["module"]["path"],
                         "image_identity": copy.deepcopy(pin["macho_images"][0]),
                         "instruction_section": copy.deepcopy(pin["macho_sections"][0]["sections"][0]),
                         "module_size": max(int(segment["offset"], 16) + segment["bytes"] for segment in segments),
                         "reported_module_size": 0x2000, "executable": True, "symbol": target.get("symbol"),
                         "implementation_offset": target.get("offset", "0x1000")}
                callables.append({"recipe": copy.deepcopy(recipe), "entry": entry,
                    "declaration": {key: copy.deepcopy(recipe[key]) for key in ("abi", "argument_types", "return_type", "context", "pointer_policy")},
                    "pointer": {"handle": f"n{handle}", "alias": None, "ownership": "borrowed_no_escape", "expires": "transaction"},
                    "wrapper_constructed": False, "original_dispatched": False})
            completion, returned = None, None
            if "compiler_adapter" in actor:
                adapter = actor["compiler_adapter"]
                raw = list(struct.pack("<IIIId", 0, 1, 0, 0, 5.25))
                outputs[-1]["bytes"] = raw
                returned = 0
                completion = {"format": "anyuse.rea-compiler-completion/1", "family": adapter["family"],
                    "argument": adapter["completion_argument"], "bytes": 24, "evidence": copy.deepcopy(adapter["evidence"]),
                    "valid": True, "status": 0, "status_return": 0, "dispatch_attempted": True, "uncertain": False,
                    "reserved_zero": 0, "value": 5.25, "original_body_entry_proved": False}
                if adapter["family"] == "cxx_owned_string_bytes":
                    payload = [65, 0, 255, 66]
                    capacity = actor["arguments"][3]["type"]["bytes"]
                    outputs[0]["bytes"] = payload + [0] * (capacity - len(payload))
                    outputs[-1]["bytes"] = list(struct.pack("<IIIIII", 0, 1, 0, 0, len(payload), 0))
                    completion.update(format="anyuse.rea-compiler-bytes-completion/1", result_type="bytes",
                        payload_argument=3, payload_bytes=capacity, length=len(payload), length_offset=16,
                        reserved_tail_zero=0, padding_zero=True, value=payload)
            rows.append({"index": index, "op": "native_invoke", "status": "ok", "function": function,
                         "return_type": copy.deepcopy(actor["return_type"]), "value": returned, "out_buffers": outputs,
                         "pointer_reads": pointer_reads, "swift_field_reads": swift_reads, "callable_entries": callables,
                         "compiler_completion": completion})
            paths = B.native_path_recipes(actor)
            if paths:
                from test_rea_native_path import _observations
                rows[-1]["native_paths"] = [observation for recipe in paths for observation in _observations(recipe)]
            continue
        method = {"selector": actor["selector"], "signature": actor["signature"], "class": "MappedReader",
                  "dispatch": "-", "return_type": actor["return_type"],
                  "argument_types": [a["type"] for a in actor["arguments"]],
                  "origin": "module_backed", "module": identity["executable"], "implementation_offset": "0x123"}
        rows.append({"index": index, "op": "invoke", "status": "ok", "method": method,
                     "return_type": actor["return_type"], "value": None})
    return {"format": "anyuse.rea-runtime/1", "status": "executed", "target": c["target"], "bundle": identity["bundle"],
            "request": request, "request_sha256": R.digest(request),
            "native_resources": copy.deepcopy(native_pins),
            "compiler_provenance": copy.deepcopy(provenance),
            "identity": identity, "after_identity": copy.deepcopy(identity),
            "adapter": objc_adapter(), "worker": {"returncode": 0, "timed_out": False},
            "result": {"format": "anyuse.rea-runtime-worker/1", "status": "completed", "request_sha256": R.digest(request),
                       "attached": True, "handles_expired": True,
                       "native_resources": copy.deepcopy(native_pins), "native_state_uncertain": False,
                       "compiler_provenance": copy.deepcopy(provenance),
                       "process": {"pid": pid, "executable": identity["executable"], "architecture": "arm64"},
                       "operations": rows,
                       "native_calls": B.minimum_native_calls(invocation),
                       "invocations": len(rows),
                       "helper_load_attempts": 0,
                       "callable_resolutions": sum(len(row.get("callable_entries", [])) for row in rows),
                       "cleanup": {"script_unloaded": True, "detached": True}},
            "actuated": True, "verified": False, "evidence_only": True}


def gadget_transport():
    def file(path, sha, restricted=False):
        value = {"file": path, "sha256": sha * 64, "bytes": 100}
        if restricted:
            value.update(uid=501, mode="0600")
        return value
    return {"kind": "gadget", "endpoint": "127.0.0.1:27042",
            "token_file": file("/tmp/owned-gadget/token", "3", True),
            "library": file("/tmp/owned-gadget/Gadget.dylib", "4"),
            "config": file("/tmp/owned-gadget/Gadget.config", "5", True)}


def use_gadget(runtime, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    adapter = objc_adapter()
    adapter["transport"] = gadget_transport()
    monkeypatch.setattr(B, "adapter_identity", lambda: copy.deepcopy(adapter))
    original = runtime.send
    def send(self, c, inputs, timeout):
        run = original(self, c, inputs, timeout)
        receipt = run["runtime"]
        receipt["adapter"] = copy.deepcopy(adapter)
        receipt["transport"] = copy.deepcopy(adapter["transport"])
        receipt["result"]["transport"] = copy.deepcopy(adapter["transport"])
        receipt["result"]["preloaded_gadget_remains"] = True
        return run
    monkeypatch.setattr(runtime, "send", send)
    return adapter


def tree(query, body, variant=False, scope="Selected"):
    if variant:
        return [{"parent": None, "role": "AXWindow"},
                {"parent": 0, "role": "AXTextArea", "AXValue": body},
                {"parent": 0, "role": "AXTextField", "AXSubrole": "AXSearchField", "AXValue": query}]
    return [{"parent": None, "role": "AXWindow"},
            {"parent": 0, "role": "AXTextField", "AXSubrole": "AXSearchField", "AXValue": query},
            {"parent": 0, "role": "AXRadioButton", "AXTitle": scope, "AXValue": "1"},
            {"parent": 0, "role": "AXRadioButton", "AXTitle": "Other", "AXValue": "0"},
            {"parent": 0, "role": "AXWebArea"},
            {"parent": 4, "role": "AXStaticText", "AXValue": body}]


def nested_text_tree(query, body, *, wrapped=False):
    """A complete editor value with paragraph text areas underneath groups."""
    rows = [{"parent": None, "role": "AXWindow"},
            {"parent": 0, "role": "AXTextField", "AXSubrole": "AXSearchField", "AXValue": query}]
    parent = 0
    if wrapped:
        parent = len(rows)
        rows.append({"parent": 0, "role": "AXGroup"})
    editor = len(rows)
    rows.append({"parent": parent, "role": "AXTextArea", "AXValue": body})
    for paragraph in body.split("\n\n"):
        group = len(rows)
        rows.append({"parent": editor, "role": "AXGroup"})
        rows.append({"parent": group, "role": "AXTextArea", "AXValue": paragraph})
    return rows


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(H.time, "sleep", lambda delay: None)
    from anyuse.execution import rea_runtime_bridge as B
    monkeypatch.setattr(B, "adapter_identity", objc_adapter)
    app = tmp_path / "Unseen.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.test.UnseenReader"}))
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"handler":"reviewed event descriptor facts"}')
    paths = []
    for i in range(6):
        p = tmp_path / f"independent-{i}.txt"
        p.write_text(f"Independent full result {i}.")
        paths.append(p)
    profile = {"format": H.PROFILE, "id": "learned_reader", "label": "Read a result",
               "effect": "scoped_find", "item_kind": "",
               "params": [{"name": "query", "type": "string", "source": "lookup"},
                          {"name": "reference", "type": "path", "source": "path"}],
               "reference": {"fields": {"query": {"kind": "input", "param": "query"},
                                        "body": {"kind": "file_text", "param": "reference", "normalization": "compact"}}},
               "evidence": [{"file": str(evidence), "pointer": "/handler", "sha256": R._file_hash(evidence)}]}
    inputs = [{"query": f"q{i}", "reference": str(p)} for i, p in enumerate(paths)]
    demos = [{"inputs": row, "invocation": {"kind": "apple_event", "class": "TEST", "id": "READ",
               "parameters": {"term": {"type": "utf8", "value": row["query"]},
                              "path": {"type": "utf8", "value": row["reference"]},
                              "mode": {"type": "utf8", "value": "fixed"}}},
              "after": tree(row["query"], p.read_text())} for row, p in zip(inputs[:3], paths)]
    return app, profile, inputs, demos


@pytest.fixture
def runtime(monkeypatch):
    class Fake:
        sent = []
        wrong = False
        reference_changes = False
        autosave = False
        initial = tree("seed", "Seed full result.")
        def __init__(self, bundle, c):
            self.state = copy.deepcopy(self.initial)
        def read(self):
            return self.state
        def send(self, c, inputs, timeout):
            self.sent.append(copy.deepcopy(inputs))
            content = Path(inputs["reference"]).read_text()
            self.state = tree(inputs["query"], "Wrong result." if self.wrong else content)
            if "working" in inputs:
                working = Path(inputs["working"])
                self.state[0]["AXDocument"] = working.as_uri()
                if self.autosave:
                    replacement = working.with_name("working-replacement.txt")
                    replacement.write_text(content)
                    os.replace(replacement, working)
            if self.reference_changes:
                Path(inputs["reference"]).write_text("Changed while the actor ran.")
            if c["actor"]["kind"] in H.INSPECTOR_KINDS:
                receipt = inspector_receipt(c, H.bind(c, inputs))
                return {"returncode": 0, "timed_out": False, "permission": "inspector_connected", "pid": 443,
                        "uid": receipt["identity"]["uid"], "bundle": "/tmp/Unseen.app",
                        "primitive": c["actor"]["kind"], "runtime": receipt}
            if c["actor"]["kind"] in H.NATIVE_RUNTIME_KINDS:
                return {"returncode": 0, "timed_out": False, "permission": "runtime_attached", "pid": 443, "uid": 501,
                        "bundle": "/tmp/Unseen.app",
                        "primitive": c["actor"]["kind"], "runtime": objc_receipt(c, H.bind(c, inputs))}
            return {"returncode": 0, "timed_out": False, "permission": 0}
    monkeypatch.setattr(H, "Runtime", Fake)
    return Fake


def learn(fixture, tmp_path):
    app, profile, _, demos = fixture
    return H.learn(profile, demos, str(app), str(tmp_path / "learn"))


def cases(fixture):
    return [{"inputs": row} for row in fixture[2][:3]]


def test_learning_infers_typed_bindings_and_complete_selector_without_granting_proof(fixture, tmp_path):
    c = learn(fixture, tmp_path)
    assert c["target"]["bundle_id"] == "org.test.UnseenReader"
    assert c["actor"]["parameters"]["term"]["value"] == {"input": "query"}
    assert c["actor"]["parameters"]["path"]["value"] == {"input": "reference"}
    assert c["actor"]["parameters"]["mode"]["value"] == {"constant": "fixed"}
    assert c["selectors"]["body"] == {"where": {"role": "AXWebArea"}, "read": "subtree_text"}
    assert not R.proof_status(c, {"verified": True}, c["target"])[0]
    assert not (tmp_path / "learn/proof.json").exists()


def test_same_learner_handles_second_bundle_and_different_content_role(fixture, tmp_path):
    app, profile, inputs, demos = fixture
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.vendor.Different"}))
    for demo in demos:
        demo["after"] = tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text(), variant=True)
    c = learn(fixture, tmp_path)
    assert c["target"]["bundle_id"] == "com.vendor.Different"
    assert c["selectors"]["body"] == {"where": {"role": "AXTextArea"}, "read": "AXValue"}


@pytest.mark.parametrize("primitive", ["apple_event", "inspector_method"])
def test_nested_editor_whole_text_learns_verifies_and_replays_fresh_paragraphs(fixture, runtime, tmp_path, monkeypatch, primitive):
    app, profile, inputs, demos = fixture
    profile["reference"]["fields"]["body"]["normalization"] = "exact"
    for index in range(3, 9):
        path = tmp_path / f"fresh-nested-{index}.txt"
        path.write_text(f"First paragraph {index}.\n\nSecond paragraph {index}: Grüße 東京.")
        values = {"query": f"fresh{index}", "reference": str(path)}
        if index < len(inputs):
            inputs[index] = values
        else:
            inputs.append(values)
    if primitive == "inspector_method":
        inspector_cases(fixture, tmp_path, monkeypatch)
    for demo in demos:
        demo["after"] = nested_text_tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text())
    original = runtime.send
    def send(self, candidate, values, timeout):
        run = original(self, candidate, values, timeout)
        body = self.state[-1]["AXValue"]
        self.state = nested_text_tree(values["query"], body, wrapped=len(self.sent) % 2 == 0)
        return run
    monkeypatch.setattr(runtime, "send", send)
    report = H.run(profile, demos, [{"inputs": values} for values in inputs[3:6]],
                   [{"inputs": values} for values in inputs[6:]], str(app), str(tmp_path / "nested-run"), timeout=1)
    candidate = R.load_json(tmp_path / "nested-run/learn/candidate.json")
    proof = R.load_json(tmp_path / "nested-run/verify/proof.json")
    assert candidate["selectors"]["body"] == {"where": {"role": "AXTextArea"}, "read": "AXValue", "outermost": True}
    assert report["status"] == "verified" and report["verified_cases"] == 3
    assert len(report["heldout"]) == 3 and len(runtime.sent) == 6
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    assert len({case["before_ax_sha256"] for case in proof["cases"]}) >= 2
    assert all(case["expected"]["body"] == Path(case["inputs"]["reference"]).read_text() for case in proof["cases"])
    assert all(case["seen"] == case["expected"] and case["observer"] == "learned_ax" for case in report["heldout"])
    legacy = copy.deepcopy(candidate)
    del legacy["selectors"]["body"]["outermost"]
    H.validate(legacy)
    with pytest.raises(ValueError, match="ambiguous"):
        H.observe(legacy, demos[0]["after"], demos[0]["inputs"])


def test_outermost_complete_text_selector_has_a_closed_literal_schema(fixture, tmp_path):
    for demo in fixture[3]:
        demo["after"] = nested_text_tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text())
    candidate = learn(fixture, tmp_path)
    for flag in (False, 0, 1, None, "true", []):
        changed = copy.deepcopy(candidate)
        changed["selectors"]["body"]["outermost"] = flag
        with pytest.raises(ValueError, match="outermost"):
            H.validate(changed)
    for selector in ({"where": {"role": "AXWebArea"}, "read": "subtree_text", "outermost": True},
                     {"where": {"role": "AXTextArea"}, "read": "AXTitle", "outermost": True}):
        changed = copy.deepcopy(candidate)
        changed["selectors"]["body"] = selector
        with pytest.raises(ValueError, match="outermost"):
            H.validate(changed)
    changed = copy.deepcopy(candidate)
    changed["selectors"]["query"]["outermost"] = True
    with pytest.raises(ValueError, match="outermost"):
        H.validate(changed)
    for key in ("index", "ordinal", "expected", "unknown"):
        changed = copy.deepcopy(candidate)
        changed["selectors"]["body"][key] = 0
        with pytest.raises(ValueError, match="declarative AX selector"):
            H.validate(changed)
    for anchor in ("AXValue", "AXTitle"):
        changed = copy.deepcopy(candidate)
        changed["selectors"]["body"]["where"][anchor] = "Recorded task output"
        with pytest.raises(ValueError, match="constant"):
            H.validate(changed)


@pytest.mark.parametrize("paragraph_subrole", [None, "AXParagraph"])
@pytest.mark.parametrize("outer_value", [None, "Wrong complete outer document."])
def test_learning_cannot_use_an_exact_paragraph_to_rescue_missing_or_wrong_whole_text(fixture, tmp_path, paragraph_subrole, outer_value):
    for demo in fixture[3]:
        rows = nested_text_tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text())
        if outer_value is None:
            del rows[2]["AXValue"]
        else:
            rows[2]["AXValue"] = outer_value
        if paragraph_subrole is not None:
            rows[-1]["AXSubrole"] = paragraph_subrole
        demo["after"] = rows
    with pytest.raises(ValueError, match="unambiguous full-output selector: body"):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn/candidate.json").exists()


@pytest.mark.parametrize("other_value", ["same", "Unrelated editor with different complete text."])
def test_outermost_does_not_choose_between_independent_editor_roots(fixture, tmp_path, other_value):
    for demo in fixture[3]:
        demo["after"] = nested_text_tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text())
    candidate = learn(fixture, tmp_path)
    for demo in fixture[3]:
        rows = demo["after"]
        body = Path(demo["inputs"]["reference"]).read_text() if other_value == "same" else other_value
        editor = len(rows)
        rows.append({"parent": 0, "role": "AXTextArea", "AXValue": body})
        rows.append({"parent": editor, "role": "AXGroup"})
        rows.append({"parent": editor + 1, "role": "AXTextArea", "AXValue": body})
        with pytest.raises(ValueError, match="ambiguous learned AX selector"):
            H.observe(candidate, rows, demo["inputs"])
    with pytest.raises(ValueError, match="unambiguous full-output selector: body"):
        H.learn(fixture[1], fixture[3], str(fixture[0]), str(tmp_path / "two-editors"))
    assert not (tmp_path / "two-editors/candidate.json").exists()


@pytest.mark.parametrize("outer_value", [None, "Wrong complete outer document."])
def test_wrong_or_missing_outer_value_refutes_verification_and_replay_even_with_exact_child(fixture, runtime, tmp_path, monkeypatch, outer_value):
    for demo in fixture[3]:
        demo["after"] = nested_text_tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text())
    candidate = learn(fixture, tmp_path)
    original = runtime.send
    def send(self, c, values, timeout):
        run = original(self, c, values, timeout)
        self.state = nested_text_tree(values["query"], self.state[-1]["AXValue"])
        return run
    monkeypatch.setattr(runtime, "send", send)
    checks = [{"inputs": values} for values in fixture[2][3:]]
    proof = H.verify(candidate, checks, str(fixture[0]), str(tmp_path / "verified-outer"), 1)
    def corrupt(self, c, values, timeout):
        run = send(self, c, values, timeout)
        if outer_value is None:
            del self.state[2]["AXValue"]
        else:
            self.state[2]["AXValue"] = outer_value
        return run
    monkeypatch.setattr(runtime, "send", corrupt)
    result = H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "refuted-outer"), 1)
    assert result["verdict"] == "refuted" and result["run"]["returncode"] == 0
    expected_seen = None if outer_value is None else "Wrongcompleteouterdocument."
    assert result["seen"]["body"] == expected_seen and result["seen"] != result["expected"]
    sent = len(runtime.sent)
    with pytest.raises(ValueError, match="failed; stopped without retry"):
        H.verify(candidate, checks, str(fixture[0]), str(tmp_path / "failed-outer"), 1)
    failed = R.load_json(tmp_path / "failed-outer/proof.json")
    assert failed["status"] == "incomplete" and len(failed["cases"]) == 1
    assert failed["cases"][0]["verdict"] == "refuted" and len(runtime.sent) == sent + 1


def test_fresh_nested_paragraph_cannot_replace_a_previously_unique_complete_editor(fixture, tmp_path):
    for demo in fixture[3]:
        demo["after"] = tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text(), variant=True)
        demo["after"][1]["AXSubrole"] = "AXDocumentContent"
    candidate = learn(fixture, tmp_path)
    assert candidate["selectors"]["body"] == {
        "where": {"role": "AXTextArea", "AXSubrole": "AXDocumentContent"}, "read": "AXValue"}
    values = fixture[2][3]
    rows = nested_text_tree(values["query"], Path(values["reference"]).read_text())
    rows[2]["AXValue"] = "Wrong full outer document."
    rows[2]["AXSubrole"] = "AXEditor"
    rows[-1]["AXSubrole"] = "AXDocumentContent"
    assert H.observe(candidate, rows, values)["body"] is None


def test_outermost_matching_is_scoped_to_the_independent_document_window(fixture, tmp_path):
    profile = fixture[1]
    profile["reference"]["fields"]["identity"] = {"kind": "file_identity", "param": "reference"}
    profile["reference"]["window_path_param"] = "reference"
    for demo in fixture[3]:
        demo["after"] = nested_text_tree(demo["inputs"]["query"], Path(demo["inputs"]["reference"]).read_text())
        demo["after"][0]["AXDocument"] = Path(demo["inputs"]["reference"]).as_uri()
    candidate = learn(fixture, tmp_path)
    values = fixture[2][3]
    body = Path(values["reference"]).read_text()
    rows = [
        {"parent": None, "role": "AXWindow", "AXDocument": (tmp_path / "other.txt").as_uri()},
        {"parent": 0, "role": "AXTextArea", "AXValue": "Other editor"},
        {"parent": 1, "role": "AXGroup"},
        {"parent": 2, "role": "AXTextArea", "AXValue": body},
        {"parent": None, "role": "AXWindow", "AXDocument": Path(values["reference"]).as_uri()},
        {"parent": 4, "role": "AXGroup"},
        {"parent": 5, "role": "AXTextArea", "AXValue": body},
        {"parent": 6, "role": "AXGroup"},
        {"parent": 7, "role": "AXTextArea", "AXValue": body},
        {"parent": 4, "role": "AXTextField", "AXSubrole": "AXSearchField", "AXValue": values["query"]}]
    assert H.observe(candidate, rows, values) == H.expected(candidate, values)[0]


@pytest.mark.parametrize("attribute", ["AXValue", "AXTitle"])
def test_independent_constant_control_disambiguates_same_role_without_anchoring_task_output(fixture, tmp_path, attribute):
    app, profile, _, demos = fixture
    profile["reference"]["fields"]["control"] = {"kind": "constant", "value": "UNCHANGED CONTROL"}
    for demo in demos:
        demo["after"] += [{"parent": 0, "role": "AXStaticText", attribute: "UNCHANGED CONTROL"},
                          {"parent": 0, "role": "AXStaticText", attribute: "Unrelated window title"}]
    c = learn(fixture, tmp_path)
    assert c["selectors"]["control"] == {
        "where": {"role": "AXStaticText", attribute: "UNCHANGED CONTROL"}, "read": attribute}
    assert c["selectors"]["body"] == {"where": {"role": "AXWebArea"}, "read": "subtree_text"}
    assert c["selectors"]["query"] == {
        "where": {"role": "AXTextField", "AXSubrole": "AXSearchField"}, "read": "AXValue"}
    expected = H.expected(c, demos[0]["inputs"])[0]
    assert H.observe(c, demos[0]["after"], demos[0]["inputs"]) == expected
    for missing in (False, True):
        altered = copy.deepcopy(demos[0]["after"])
        if missing:
            altered.pop(-2)
        else:
            altered[-2][attribute] = "CONTROL CHANGED"
        assert H.observe(c, altered, demos[0]["inputs"])["control"] is None
    duplicate = copy.deepcopy(demos[0]["after"])
    duplicate.append(copy.deepcopy(duplicate[-2]))
    with pytest.raises(ValueError, match="ambiguous"):
        H.observe(c, duplicate, demos[0]["inputs"])
    wrong_anchor = copy.deepcopy(c)
    wrong_anchor["selectors"]["control"]["where"][attribute] = "Not independently declared"
    with pytest.raises(ValueError, match="constant"):
        H.validate(wrong_anchor)
    for field, anchor in (("query", "AXValue"), ("body", "AXValue"), ("body", "AXTitle")):
        dynamic = copy.deepcopy(c)
        dynamic["selectors"][field]["where"][anchor] = "Recorded task output"
        with pytest.raises(ValueError, match="constant"):
            H.validate(dynamic)


def test_constant_control_remains_required_during_fresh_verification_and_replay(fixture, runtime, tmp_path, monkeypatch):
    app, profile, inputs, demos = fixture
    profile["reference"]["fields"]["control"] = {"kind": "constant", "value": "UNCHANGED CONTROL"}
    controls = [{"parent": 0, "role": "AXStaticText", "AXValue": "UNCHANGED CONTROL"},
                {"parent": 0, "role": "AXStaticText", "AXValue": "Unrelated window title"}]
    for demo in demos:
        demo["after"].extend(copy.deepcopy(controls))
    original = runtime.send
    def send(self, candidate, values, timeout):
        result = original(self, candidate, values, timeout)
        self.state.extend(copy.deepcopy(controls))
        return result
    monkeypatch.setattr(runtime, "send", send)
    c = learn(fixture, tmp_path)
    proof = H.verify(c, cases(fixture), str(app), str(tmp_path / "constant-proof"), 1)
    assert H.proof_status(c, proof, c["target"])[0]
    controls[0]["AXValue"] = "CONTROL CHANGED"
    result = H.replay(c, proof, inputs[3], str(app), str(tmp_path / "constant-replay"), 1)
    assert result["seen"]["body"] == result["expected"]["body"]
    assert result["seen"]["control"] is None and result["verdict"] == "refuted"


def test_shared_learner_binds_private_method_and_keeps_independent_result_gate(fixture, runtime, tmp_path):
    app, profile, inputs, demos = fixture
    for demo in demos:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    report = H.run(profile, demos, cases(fixture), [{"inputs": r} for r in inputs[3:]],
                   str(app), str(tmp_path / "runtime-run"), timeout=1)
    candidate = R.load_json(tmp_path / "runtime-run/learn/candidate.json")
    assert candidate["actor"] == objc_actor({"input": "query"}, {"input": "reference"})
    assert candidate["selectors"]["body"]["read"] == "subtree_text"
    assert report["status"] == "verified" and len(runtime.sent) == 6
    assert R.load_json(tmp_path / "runtime-run/verify/proof.json")["model_calls"] == 0


@pytest.mark.parametrize("suffix", [None, {"query": "mode=editor&wid=1", "fragment": "/editor"}])
def test_inspector_method_reuses_learning_proof_and_replay_with_separate_transport_pins(fixture, runtime, tmp_path, monkeypatch, suffix):
    from anyuse.execution import rea_inspector_bridge as I
    inspector_cases(fixture, tmp_path, monkeypatch)
    if suffix is not None:
        config_file = Path(os.environ["ANYUSE_REA_INSPECTOR_CONFIG"])
        config = R.load_json(config_file)
        config["document_suffix"] = suffix
        R.write_json(config_file, config)
    app, profile, inputs, demos = fixture
    report = H.run(profile, demos, cases(fixture), [{"inputs": row} for row in inputs[3:]],
                   str(app), str(tmp_path / "inspector-run"), timeout=1)
    c = R.load_json(tmp_path / "inspector-run/learn/candidate.json")
    proof = R.load_json(tmp_path / "inspector-run/verify/proof.json")
    assert [argument["value"] for argument in c["actor"]["arguments"]] == [
        {"input": "query"}, {"input": "reference"}, True]
    assert report["status"] == "verified" and len(runtime.sent) == 6
    assert c["selectors"]["body"] == {"where": {"role": "AXWebArea"}, "read": "subtree_text"}
    assert "inspector_bridge" in proof["resources"] and "runtime_bridge" not in proof["resources"]
    identity = proof["resources"]["inspector_bridge"]
    assert identity["adapter"]["protocol_limits"] == I._protocol_limits()
    for mutation in ("missing", "changed", "malformed"):
        changed = copy.deepcopy(proof["resources"])
        limits = changed["inspector_bridge"]["adapter"]["protocol_limits"]
        if mutation == "missing":
            del changed["inspector_bridge"]["adapter"]["protocol_limits"]
        elif mutation == "changed":
            limits["wire_message_bytes"] += 1
        else:
            limits["scope_steps_per_recipe"] = True
        assert not H._resources_equal(proof["resources"], changed)
    assert identity["transport"]["kind"] == "chromium_renderer_cdp"
    assert identity["transport"]["config"]["mode"] == "0600"
    assert identity["transport"].get("document_suffix") == suffix
    if suffix is not None:
        changed = copy.deepcopy(proof["resources"])
        changed["inspector_bridge"]["transport"]["document_suffix"]["query"] += "&other=1"
        assert not H._resources_equal(proof["resources"], changed)
        changed["inspector_bridge"]["transport"]["document_suffix"] = {"query": "", "fragment": "", "ignored": True}
        assert not H._resources_equal(proof["resources"], changed)
    assert H.proof_status(c, proof, c["target"])[0]
    runtime.wrong = True
    result = H.replay(c, proof, inputs[3], str(app), str(tmp_path / "wrong-inspector-output"), 1)
    assert result["verdict"] == "refuted"


def test_inspector_inference_converts_declared_types_and_requires_boolean_diversity(fixture, tmp_path, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I
    inspector_cases(fixture, tmp_path, monkeypatch)
    profile, inputs, demos = fixture[1:]
    profile["params"] += [{"name": "amount", "type": "string", "source": "name"},
                          {"name": "enabled", "type": "string", "source": "name"},
                          {"name": "payload", "type": "string", "source": "text"}]
    for index, row in enumerate(inputs):
        row.update(amount=str(index + 10), enabled="true" if index % 2 == 0 else "false",
                   payload=json.dumps({"counter": index}))
    for demo in demos:
        row = demo["inputs"]
        # An actual JSON number can arrive as int although text conversion is
        # float. Its declared JavaScript number type stays unchanged.
        demo["invocation"]["arguments"] += [
            {"type": "number", "value": int(row["amount"])},
            {"type": "bool", "value": I.convert_binding("bool", row["enabled"])},
            {"type": "json", "value": I.convert_binding("json", row["payload"])},
            {"type": "null", "value": None}, {"type": "json", "value": {"fixed": [1, True]}}]
    declaration = Path(demos[0]["invocation"]["signature_evidence"]["file"])
    R.write_json(declaration, {"declarations": {"operation": I.call_contract(demos[0]["invocation"])}})
    for demo in demos:
        demo["invocation"]["signature_evidence"]["sha256"] = R._file_hash(declaration)
    c = learn(fixture, tmp_path)
    assert [argument["value"] for argument in c["actor"]["arguments"][3:]] == [
        {"input": "amount"}, {"input": "enabled"}, {"input": "payload"}, None, {"fixed": [1, True]}]
    bound = H.bind(c, inputs[3])
    assert [argument["value"] for argument in bound["arguments"][3:6]] == [13.0, False, {"counter": 3}]
    bad = copy.deepcopy(c["actor"])
    bad["arguments"][0]["value"] = {"input": "not_frozen"}
    with pytest.raises(ValueError, match="frozen task input"):
        H._runtime_actor(bad, c["params"])


@pytest.mark.parametrize("changed", ["recipe", "method", "function", "declaration", "argument_type", "return_type"])
def test_inspector_inference_refuses_changed_callable_shape(fixture, tmp_path, monkeypatch, changed):
    inspector_cases(fixture, tmp_path, monkeypatch)
    actor = fixture[3][1]["invocation"]
    if changed == "recipe": actor["receiver"]["steps"][0]["name"] = "different"
    elif changed == "method": actor["method"] = "differentResult"
    elif changed == "function": actor["function_sha256"] = "f" * 64
    elif changed == "declaration": actor["signature_evidence"]["pointer"] = "/different"
    elif changed == "argument_type": actor["arguments"][2]["type"], actor["arguments"][2]["value"] = "int32", 1
    else: actor["return_type"] = "bool"
    with pytest.raises(ValueError, match="shape varies"):
        learn(fixture, tmp_path)


def test_inspector_inference_refuses_ambiguous_inputs_and_varying_constants(fixture, tmp_path, monkeypatch):
    inspector_cases(fixture, tmp_path, monkeypatch)
    profile, inputs, _ = fixture[1:]
    profile["params"].append({"name": "alias", "type": "string", "source": "lookup"})
    for row in inputs:
        row["alias"] = row["query"]
    with pytest.raises(ValueError, match="unique typed inspector"):
        learn(fixture, tmp_path)
    with pytest.raises(ValueError, match="unique typed inspector"):
        H._infer_inspector_binding("json", [1, True, 1], [{"q": "x"}, {"q": "y"}, {"q": "z"}], {"q"})


def test_inspector_proof_refuses_changed_envelope_and_incomplete_independent_results(fixture, runtime, tmp_path, monkeypatch):
    inspector_cases(fixture, tmp_path, monkeypatch)
    c = learn(fixture, tmp_path)
    proof = H.verify(c, cases(fixture), str(fixture[0]), str(tmp_path / "inspector-proof"), 1)
    assert H.proof_status(c, proof, c["target"])[0]
    for changed in ("format", "permission", "transport", "adapter", "declaration", "cleanup", "uncertain",
                    "context", "return", "seen", "pinned_transport", "pinned_adapter"):
        altered = copy.deepcopy(proof)
        row = altered["cases"][0]
        receipt = row["run"]["runtime"]
        if changed == "format": receipt["format"] = "anyuse.rea-runtime/1"
        elif changed == "permission": row["run"]["permission"] = "runtime_attached"
        elif changed == "transport": receipt["transport"]["endpoint"] = "http://127.0.0.1:9247"
        elif changed == "adapter": receipt["adapter"]["sha256"] = "0" * 64
        elif changed == "declaration": next(iter(receipt["declaration_resources"].values()))["bytes"] += 1
        elif changed == "cleanup": receipt["cleanup"]["object_group_released"] = False
        elif changed == "uncertain": receipt["state_uncertain"] = True
        elif changed == "context": receipt["endpoint_identity"]["execution_context"]["is_default"] = False
        elif changed == "return": receipt["operations"][0]["value"] = 1
        elif changed == "seen": row["seen"]["body"] = "Wrong complete output."
        elif changed == "pinned_transport": altered["resources"]["inspector_bridge"]["transport"]["config"]["sha256"] = "0" * 64
        else: altered["resources"]["inspector_bridge"]["adapter"]["sha256"] = "0" * 64
        assert not H.proof_status(c, altered, c["target"])[0], changed


def test_inspector_replay_refuses_transport_switch_and_protects_configuration_receipts(fixture, runtime, tmp_path, monkeypatch):
    inspector_cases(fixture, tmp_path, monkeypatch)
    c = learn(fixture, tmp_path)
    proof = H.verify(c, cases(fixture), str(fixture[0]), str(tmp_path / "inspector-proof"), 1)
    pinned = H.resources(c)["inspector_bridge"]
    for path in (pinned["adapter"]["file"], pinned["transport"]["config"]["file"], c["actor"]["signature_evidence"]["file"]):
        with pytest.raises(ValueError, match="outside app and pinned"):
            H._outside(c, fixture[0].resolve(), Path(path) / "receipts")
    replacement = (tmp_path / "other-inspector-config.json").resolve()
    replacement.write_bytes(Path(pinned["transport"]["config"]["file"]).read_bytes())
    replacement.chmod(0o600)
    monkeypatch.setenv("ANYUSE_REA_INSPECTOR_CONFIG", str(replacement))
    sent = len(runtime.sent)
    with pytest.raises(ValueError, match="pinned reference resource changed"):
        H.replay(c, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "redirected-inspector"), 1)
    assert len(runtime.sent) == sent and not (tmp_path / "redirected-inspector").exists()


def test_runtime_invokes_inspector_with_frozen_configuration_and_no_native_fallback(fixture, tmp_path, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I
    from anyuse.execution import rea_runtime_bridge as B
    inspector_cases(fixture, tmp_path, monkeypatch)
    c = learn(fixture, tmp_path)
    class NativeObservation:
        pid = 443
        def __init__(self, *args, **kwargs): pass
        def _ready(self): pass
    monkeypatch.setattr(H.AE, "Runtime", NativeObservation)
    invoked = []
    def invoke(bundle, actor, out, **kwargs):
        invoked.append((bundle, copy.deepcopy(actor), out, kwargs))
        return inspector_receipt(c, actor, bundle=str(fixture[0].resolve()))
    monkeypatch.setattr(I, "invoke", invoke)
    def unexpected_native(*args, **kwargs):
        raise AssertionError("inspector execution entered native provider")
    monkeypatch.setattr(B, "invoke", unexpected_native)
    executor = H.Runtime(fixture[0].resolve(), c)
    executor.receipt_dir = tmp_path / "inspector-execution"
    run = executor.send(c, fixture[2][3], 1)
    assert run["returncode"] == 0 and run["permission"] == "inspector_connected"
    assert len(invoked) == 1
    assert invoked[0][3]["config_file"] == executor.inspector_identity["transport"]["config"]["file"]
    assert invoked[0][1] == H.bind(c, fixture[2][3])
    def failed(bundle, actor, out, **kwargs):
        return {"resource_review_required": True, "state_uncertain": True}
    monkeypatch.setattr(I, "invoke", failed)
    with pytest.raises(R.ResourceReviewRequired, match="no retry"):
        executor.send(c, fixture[2][4], 1)


def test_native_struct_and_buffer_bindings_reuse_the_independent_complete_result_gate(fixture, runtime, tmp_path):
    native_cases(fixture, tmp_path, compound=True)
    report = H.run(fixture[1], fixture[3], cases(fixture), [{"inputs": x} for x in fixture[2][3:]],
                   str(fixture[0]), str(tmp_path / "native"), 1)
    candidate = R.load_json(tmp_path / "native/learn/candidate.json")
    assert candidate["actor"]["arguments"][2]["value"] == {"counter": {"input": "counter"}, "fixed": 17}
    assert candidate["actor"]["arguments"][3]["value"] == {"input": "buffer_hex"}
    bound = H.bind(candidate, fixture[2][3])
    assert bound["arguments"][2]["value"] == {"counter": 13, "fixed": 17}
    assert bound["arguments"][3]["value"] == [3, 252]
    proof = R.load_json(tmp_path / "native/verify/proof.json")
    assert report["status"] == "verified" and len(runtime.sent) == 6
    assert proof["resources"][candidate["actor"]["target"]["module"]["path"]]["sha256"] == candidate["actor"]["target"]["module"]["sha256"]
    assert H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("family", ["swift_double_bool_context", "cxx_owned_string_vector_double", "cxx_owned_string_bytes"])
def test_compiler_adapters_reuse_learning_full_output_checks_and_transitive_proof_pins(fixture, runtime, tmp_path, monkeypatch, family):
    actor = compiler_cases(fixture, tmp_path, monkeypatch, family=family)
    report = H.run(fixture[1], fixture[3], cases(fixture), [{"inputs": x} for x in fixture[2][3:]],
                   str(fixture[0]), str(tmp_path / "compiler-run"), 1)
    candidate = R.load_json(tmp_path / "compiler-run/learn/candidate.json")
    proof = R.load_json(tmp_path / "compiler-run/verify/proof.json")
    assert report["status"] == "verified" and len(runtime.sent) == 6
    assert candidate["actor"]["arguments"][0] == actor["arguments"][0]
    assert candidate["actor"]["arguments"][1] == actor["arguments"][1]
    assert candidate["actor"]["compiler_adapter"] == actor["compiler_adapter"]
    assert candidate["selectors"]["body"] == {"where": {"role": "AXWebArea"}, "read": "subtree_text"}
    if family == "swift_double_bool_context":
        assert [a["value"] for a in candidate["actor"]["arguments"][2:4]] == [{"input": "amount"}, {"input": "enabled"}]
    elif family == "cxx_owned_string_bytes":
        assert candidate["actor"]["arguments"][2]["value"] == {"input": "query"}
        assert [a["value"] for a in candidate["actor"]["arguments"][3:]] == [None, None]
    else:
        assert [a["value"] for a in candidate["actor"]["arguments"][2:5]] == [{"input": "query"}, {"input": "count"}, {"input": "packed"}]
    receipt = proof["cases"][0]["run"]["runtime"]
    artifacts = receipt["compiler_provenance"][actor["compiler_adapter"]["evidence"]["file"]]["artifacts"]
    assert all(proof["resources"][pin["file"]]["sha256"] == pin["sha256"] for pin in artifacts)
    assert any(pin["bytes"] == 0 and "compiler_stderr" in pin["roles"] for pin in artifacts)
    original = receipt["result"]["operations"][0]["callable_entries"][0]
    completion = receipt["result"]["operations"][0]["compiler_completion"]
    assert original["wrapper_constructed"] is False and original["original_dispatched"] is False
    assert completion["dispatch_attempted"] is True and completion["original_body_entry_proved"] is False
    assert receipt["result"]["helper_load_attempts"] == 0 and receipt["result"]["callable_resolutions"] == 1
    assert H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("change", ["payload", "padding", "truncated", "length", "reserved_tail", "status", "attempted",
    "uncertain", "status_echo", "boolean_length", "boolean_byte", "reported_value", "reported_capacity", "reported_argument",
    "reported_length_offset", "reported_padding", "old_format", "extra_field", "body_entry", "resource", "state_uncertain", "missing_payload"])
def test_owned_return_completion_is_independently_decoded_even_when_full_ax_matches(fixture, runtime, tmp_path, monkeypatch, change):
    compiler_cases(fixture, tmp_path, monkeypatch, family="cxx_owned_string_bytes")
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    receipt = proof["cases"][0]["run"]["runtime"]
    row = receipt["result"]["operations"][0]
    completion = row["compiler_completion"]
    if change == "payload":
        row["out_buffers"][0]["bytes"][2] = 254
    elif change == "padding":
        row["out_buffers"][0]["bytes"][-1] = 1
    elif change in {"truncated", "length", "reserved_tail", "status", "attempted", "uncertain"}:
        raw = [0, 1, 0, 0, 4, 0]
        index = {"truncated": 4, "length": 4, "reserved_tail": 5, "status": 0, "attempted": 1, "uncertain": 2}[change]
        raw[index] = {"truncated": 17, "length": 3, "reserved_tail": 1, "status": 3, "attempted": 0, "uncertain": 1}[change]
        row["out_buffers"][-1]["bytes"] = list(struct.pack("<IIIIII", *raw))
    elif change == "status_echo":
        row["value"] = 1
    elif change == "boolean_length":
        completion["length"] = True
    elif change == "boolean_byte":
        completion["value"][0] = True
    elif change == "reported_value":
        completion["value"] = [65, 0, 255]
    elif change == "reported_capacity":
        completion["payload_bytes"] = 4
    elif change == "reported_argument":
        completion["payload_argument"] = 4
    elif change == "reported_length_offset":
        completion["length_offset"] = 20
    elif change == "reported_padding":
        completion["padding_zero"] = False
    elif change == "old_format":
        completion["format"] = "anyuse.rea-compiler-completion/1"
    elif change == "extra_field":
        completion["truncated"] = False
    elif change == "body_entry":
        completion["original_body_entry_proved"] = True
    elif change == "resource":
        completion["evidence"]["sha256"] = "e" * 64
    elif change == "state_uncertain":
        receipt["result"]["native_state_uncertain"] = True
    else:
        row["out_buffers"].pop(0)
    assert proof["cases"][0]["seen"] == proof["cases"][0]["expected"]
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_owned_return_helper_success_still_requires_the_independent_complete_application_result(fixture, runtime, tmp_path, monkeypatch):
    compiler_cases(fixture, tmp_path, monkeypatch, family="cxx_owned_string_bytes")
    candidate = learn(fixture, tmp_path)
    runtime.wrong = True
    with pytest.raises(ValueError, match="failed; stopped without retry"):
        H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    proof = R.load_json(tmp_path / "verify/proof.json")
    assert proof["status"] == "incomplete" and len(proof["cases"]) == 1 and len(runtime.sent) == 1
    assert proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["compiler_completion"]["valid"] is True
    assert proof["cases"][0]["seen"] != proof["cases"][0]["expected"]
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("change", ["recipe", "module", "symbol", "image", "instruction", "context", "alias", "expired",
                                    "wrapper", "body_entry", "missing", "load", "load_missing", "load_boolean", "count", "count_missing"])
def test_compiler_original_callable_evidence_cannot_be_replaced_or_credit_an_implicit_load(fixture, runtime, tmp_path, monkeypatch, change):
    compiler_cases(fixture, tmp_path, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    result = proof["cases"][0]["run"]["runtime"]["result"]
    original = result["operations"][0]["callable_entries"][0]
    if change == "recipe":
        original["recipe"]["symbol"] = "unrelated_entry"
    elif change == "module":
        original["entry"]["module"] = candidate["actor"]["target"]["module"]["path"]
    elif change == "symbol":
        original["entry"]["symbol"] = "unrelated_entry"
    elif change == "image":
        original["entry"]["image_identity"]["uuid"] = "f" * 32
    elif change == "instruction":
        original["entry"]["implementation_offset"] = "0x1020"
    elif change == "context":
        original["declaration"]["context"] = "none"
    elif change == "alias":
        original["pointer"]["alias"] = "persisted_address"
    elif change == "expired":
        original["pointer"]["expires"] = "process"
    elif change == "wrapper":
        original["wrapper_constructed"] = True
    elif change == "body_entry":
        original["original_dispatched"] = True
    elif change == "missing":
        result["operations"][0].pop("callable_entries")
    elif change in {"load", "load_boolean"}:
        result["helper_load_attempts"] = 1 if change == "load" else False
    elif change == "load_missing":
        result.pop("helper_load_attempts")
    elif change == "count":
        result["callable_resolutions"] = 0
    else:
        result.pop("callable_resolutions")
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("change", ["status", "echo", "attempted", "uncertain", "reserved", "nan", "reported_value", "bytes",
                                    "boolean_status", "family", "evidence", "body_entry", "missing", "fault"])
def test_compiler_completion_requires_independent_exact_bytes_even_with_correct_full_ax(fixture, runtime, tmp_path, monkeypatch, change):
    compiler_cases(fixture, tmp_path, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    receipt = proof["cases"][0]["run"]["runtime"]
    row = receipt["result"]["operations"][0]
    completion = row["compiler_completion"]
    raw = [0, 1, 0, 0, 5.25]
    if change in {"status", "attempted", "uncertain", "reserved", "nan"}:
        index = {"status": 0, "attempted": 1, "uncertain": 2, "reserved": 3, "nan": 4}[change]
        raw[index] = float("nan") if change == "nan" else (0 if change == "attempted" else 1)
        row["out_buffers"][-1]["bytes"] = list(struct.pack("<IIIId", *raw))
    elif change == "echo":
        row["value"] = 2
    elif change == "reported_value":
        completion["value"] = 99.0
    elif change == "bytes":
        row["out_buffers"][-1]["bytes"].pop()
    elif change == "boolean_status":
        completion["status_return"] = False
    elif change == "family":
        completion["family"] = "cxx_owned_string_vector_double"
    elif change == "evidence":
        completion["evidence"]["sha256"] = "f" * 64
    elif change == "body_entry":
        completion["original_body_entry_proved"] = True
    elif change == "missing":
        row.pop("compiler_completion")
    else:
        receipt["result"]["native_state_uncertain"] = True
    assert proof["cases"][0]["seen"] == proof["cases"][0]["expected"]
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("change", ["echo", "manifest", "missing", "role", "helper_join", "ir_hash", "artifact_size", "pin"])
def test_compiler_proof_pins_the_whole_artifact_closure_not_just_declared_abi(fixture, runtime, tmp_path, monkeypatch, change):
    actor = compiler_cases(fixture, tmp_path, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    receipt = proof["cases"][0]["run"]["runtime"]
    provenance = receipt["compiler_provenance"][actor["compiler_adapter"]["evidence"]["file"]]
    generated = next(pin for pin in provenance["artifacts"] if "generated_ir" in pin["roles"])
    if change == "echo":
        receipt["result"]["compiler_provenance"] = {}
    elif change == "manifest":
        provenance["manifest_sha256"] = "f" * 64
    elif change == "missing":
        receipt.pop("compiler_provenance")
        receipt["result"].pop("compiler_provenance")
    elif change == "role":
        generated["roles"] = ["unrelated_ir"]
    elif change == "helper_join":
        next(pin for pin in provenance["artifacts"] if "generated_helper" in pin["roles"])["sha256"] = "f" * 64
    elif change == "ir_hash":
        generated["sha256"] = "f" * 64
    elif change == "artifact_size":
        generated["bytes"] += 1
    else:
        proof["resources"].pop(generated["file"])
    if change not in {"echo", "missing"}:
        receipt["result"]["compiler_provenance"] = copy.deepcopy(receipt["compiler_provenance"])
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("role", ["generated_ir", "original_ir", "libcxx_header", "generator_verifier_source"])
def test_compiler_transitive_artifact_change_stops_replay_before_actuation(fixture, runtime, tmp_path, monkeypatch, role):
    actor = compiler_cases(fixture, tmp_path, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    manifest = R.load_json(actor["compiler_adapter"]["evidence"]["file"])
    pin = next(pin for pin in manifest["artifacts"] if role in pin["roles"])
    Path(pin["file"]).write_text("Changed transitive compiler bytes")
    sent = len(runtime.sent)
    with pytest.raises(ValueError, match="transitive artifact"):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert len(runtime.sent) == sent


def test_compiler_exception_after_visible_mutation_keeps_uncertainty_and_stops_without_retry(fixture, runtime, tmp_path, monkeypatch):
    compiler_cases(fixture, tmp_path, monkeypatch, family="cxx_owned_string_vector_double")
    candidate = learn(fixture, tmp_path)
    original = runtime.send
    def exception(self, c, inputs, timeout):
        run = original(self, c, inputs, timeout)
        receipt = run["runtime"]
        row = receipt["result"]["operations"][0]
        row["value"] = 2
        row["out_buffers"][-1]["bytes"] = list(struct.pack("<IIIId", 2, 1, 1, 0, 0.0))
        row["compiler_completion"].update(status=2, status_return=2, uncertain=True, value=None)
        receipt["result"].update(native_state_uncertain=True, status="incomplete")
        receipt.update(status="incomplete", resource_review_required=True)
        run["returncode"] = 1
        return run
    monkeypatch.setattr(runtime, "send", exception)
    with pytest.raises(ValueError, match="stopped without retry"):
        H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "exception"), 1)
    proof = R.load_json(tmp_path / "exception/proof.json")
    assert len(runtime.sent) == 1 and proof["status"] == "incomplete"
    assert proof["cases"][0]["seen"] == proof["cases"][0]["expected"]
    assert proof["cases"][0]["run"]["runtime"]["resource_review_required"] is True
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_legacy_noncompiler_native_receipt_may_omit_new_empty_protocol_fields(fixture, runtime, tmp_path):
    native_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    for case in proof["cases"]:
        receipt = case["run"]["runtime"]
        receipt.pop("compiler_provenance")
        for key in ("compiler_provenance", "helper_load_attempts", "callable_resolutions"):
            receipt["result"].pop(key)
        row = receipt["result"]["operations"][0]
        row.pop("callable_entries")
        row.pop("compiler_completion")
    assert H.proof_status(candidate, proof, candidate["target"])[0]


def test_mixed_objc_native_sequence_keeps_every_ordered_invocation_and_full_output_check(fixture, runtime, tmp_path):
    native_cases(fixture, tmp_path, compound=True, mixed=True)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert candidate["actor"]["kind"] == "runtime_sequence"
    assert [row["op"] for row in proof["cases"][0]["run"]["runtime"]["result"]["operations"]] == ["invoke", "native_invoke"]
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    proof["cases"][0]["run"]["runtime"]["result"]["operations"].reverse()
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("change", ["module", "offset", "contract", "layout", "order"])
def test_native_route_and_compound_layout_must_not_vary_across_recordings(fixture, tmp_path, change):
    native_cases(fixture, tmp_path, compound=True)
    actor = fixture[3][1]["invocation"]
    if change == "module":
        actor["target"]["module"]["sha256"] = "f" * 64
    elif change == "offset":
        actor["target"].pop("symbol")
        actor["target"]["offset"] = "0x1000"
    elif change == "contract":
        actor["signature_evidence"]["pointer"] = "/declarations/another"
    elif change == "layout":
        actor["arguments"][2]["type"]["fields"].reverse()
    else:
        actor["arguments"].reverse()
    with pytest.raises(ValueError):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


@pytest.mark.parametrize("change", ["module", "module_hash", "symbol", "abi", "types", "contract", "bridge", "executable", "offset", "module_size", "pins", "worker_pins", "receipt_pins", "fault", "image", "missing_image"])
def test_native_proof_requires_resolved_module_contract_and_correct_callable_bridge(fixture, runtime, tmp_path, change):
    native_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    receipt = proof["cases"][0]["run"]["runtime"]
    function = receipt["result"]["operations"][0]["function"]
    if change == "module":
        function["module"] = "/tmp/different-image.bin"
    elif change == "module_hash":
        function["target"]["module"]["sha256"] = "f" * 64
    elif change == "symbol":
        function["symbol"] = "other_function"
    elif change == "abi":
        function["abi"] = "unix64"
    elif change == "types":
        function["argument_types"] = ["int32", "int32"]
    elif change == "contract":
        function["signature_evidence"]["sha256"] = "f" * 64
    elif change == "bridge":
        function["bridge"] = "frida-swift"
    elif change == "executable":
        function["executable"] = False
    elif change == "offset":
        function["implementation_offset"] = "0x2000"
    elif change == "module_size":
        function["module_size"] = True
    elif change == "pins":
        del proof["resources"][candidate["actor"]["signature_evidence"]["file"]]
    elif change == "fault":
        receipt["result"]["native_state_uncertain"] = True
    elif change == "image":
        function["image_identity"]["uuid"] = "f" * 32
    elif change == "missing_image":
        function.pop("image_identity")
    else:
        path = candidate["actor"]["target"]["module"]["path"]
        receipt["result"]["native_resources"][path]["bytes"] += 1
        if change == "receipt_pins":
            receipt["native_resources"][path]["bytes"] += 1
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_output_buffer_bounds_do_not_replace_the_resolved_module_size(fixture, tmp_path):
    actor = native_template(tmp_path, [{"kind": "buffer", "bytes": 8, "direction": "out"}], [None])
    c = {"target": {"bundle_id": "org.test.Native", "sha256": "a" * 64}}
    row = objc_receipt(c, actor)["result"]["operations"][0]
    assert row["function"]["implementation_offset"] == "0x1000"
    assert row["function"]["module_size"] == 0x2000
    assert H._native_function_run(row, actor, 0)
    row["function"]["module_size"] = "8192"
    assert not H._native_function_run(row, actor, 0)
    row["function"]["module_size"] = 0x2000
    row["out_buffers"][0]["bytes"][0] = True
    assert not H._native_function_run(row, actor, 0)


@pytest.mark.parametrize("change", ["missing", "metadata", "outside", "boolean_size", "boolean_flags", "unbounded", "wrong_name",
                                    "missing_pin", "pin_image", "pin_section"])
def test_rx_pages_need_an_exact_pinned_instruction_section_before_proof(fixture, runtime, tmp_path, change):
    native_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    receipt = proof["cases"][0]["run"]["runtime"]
    function = receipt["result"]["operations"][0]["function"]
    path = candidate["actor"]["target"]["module"]["path"]
    assert function["executable"] is True and H.proof_status(candidate, proof, candidate["target"])[0]
    if change == "missing":
        function.pop("instruction_section")
    elif change == "metadata":
        function["instruction_section"]["flags"] = 0
    elif change == "outside":
        function["implementation_offset"] = "0x1100"
    elif change == "boolean_size":
        function["instruction_section"]["bytes"] = True
    elif change == "boolean_flags":
        function["instruction_section"]["flags"] = True
    elif change == "unbounded":
        function["instruction_section"]["bytes"] = 0x2000
    elif change == "wrong_name":
        function["instruction_section"]["name"] = "__const"
    else:
        pin = receipt["native_resources"][path]
        if change == "missing_pin":
            pin.pop("macho_sections")
        elif change == "pin_image":
            pin["macho_sections"][0]["image_identity"]["uuid"] = "f" * 32
        else:
            pin["macho_sections"][0]["sections"][0]["offset"] = "0x1010"
        # Keep all three resource echoes consistent to isolate section matching.
        receipt["result"]["native_resources"][path] = copy.deepcopy(pin)
        proof["resources"][path] = copy.deepcopy(pin)
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("input_name,value", [("counter", str(2**31)), ("buffer_hex", "03FC"), ("buffer_hex", "03")])
def test_invalid_nested_native_value_stops_before_first_sequence_step(fixture, tmp_path, monkeypatch, input_name, value):
    from anyuse.execution import rea_runtime_bridge as B
    native_cases(fixture, tmp_path, compound=True, mixed=True)
    candidate = learn(fixture, tmp_path)
    class Native:
        pid = 443
        ready_calls = 0
        def __init__(self, bundle, bundle_id):
            pass
        def _ready(self):
            self.ready_calls += 1
    calls = []
    monkeypatch.setattr(H.AE, "Runtime", Native)
    monkeypatch.setattr(B, "process_identity", lambda *args: native_context_identity(fixture[0], candidate))
    monkeypatch.setattr(B, "invoke", lambda *args, **kwargs: calls.append(args))
    runtime = H.Runtime(fixture[0], candidate)
    runtime.receipt_dir = tmp_path / "refused"
    with pytest.raises(ValueError):
        runtime.send(candidate, {**fixture[2][3], input_name: value}, 1)
    assert not calls and runtime.native.ready_calls == 0 and not runtime.receipt_dir.exists()


@pytest.mark.parametrize("resource", ["module", "contract"])
def test_native_resource_changes_refuse_replay_before_actuation(fixture, runtime, tmp_path, resource):
    native_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    actor = candidate["actor"]
    file = Path(actor["target"]["module"]["path"] if resource == "module" else actor["signature_evidence"]["file"])
    file.write_bytes(b"Changed immutable native provenance")
    before = len(runtime.sent)
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "refused").exists()


@pytest.mark.parametrize("change", ["changed", "deleted", "moved"])
@pytest.mark.parametrize("resource", ["getter_module", "getter_contract", "ivar_layout", "swift_layout",
    "original_source", "original_ir", "original_build", "original_declaration", "original_module",
    "path_getter_abi", "path_consumer_abi", "path_contracts", "path_header", "path_build", "path_review"])
def test_native_external_resource_drift_blocks_publication_and_selection(fixture, runtime, tmp_path, monkeypatch, resource, change):
    from anyuse.execution import rea_actions as A
    from anyuse.execution import openjev as O
    if resource.startswith("path_"):
        _, _, files = native_path_cases(fixture, tmp_path)
        file = files[resource.removeprefix("path_")]
    elif resource.startswith("original_"):
        actor = compiler_cases(fixture, tmp_path, monkeypatch, family="cxx_owned_string_bytes")
        artifacts = R.load_json(actor["compiler_adapter"]["evidence"]["file"])["artifacts"]
        file = Path(next(pin["file"] for pin in artifacts if resource in pin["roles"]))
    elif resource == "swift_layout":
        read = swift_read_cases(fixture, tmp_path, nested=True)
        file = Path(read["evidence"]["file"])
    else:
        read = pointer_read_cases(fixture, tmp_path, nested=True)
        getter = fixture[3][0]["invocation"]["arguments"][0]["value"]["getter"]
        if resource == "getter_module":
            file = (tmp_path / "nested-getter-image.bin").resolve()
            native_metadata_image(file)
            for demo in fixture[3]:
                demo["invocation"]["arguments"][0]["value"]["getter"]["target"]["module"] = {
                    "path": str(file), "sha256": R._file_hash(file)}
        else:
            file = Path(getter["signature_evidence"]["file"] if resource == "getter_contract" else read["evidence"]["file"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert not file.is_relative_to(fixture[0]) and str(file) in proof["resources"]
    frozen = (R.digest(candidate), R.digest(proof), R.fingerprint(fixture[0]))
    A.describe(candidate, proof, str(fixture[0]), "Read the complete result", "Check all result text against its independent reference.",
               str(tmp_path / "card"))
    R.write_json(tmp_path / "candidate.json", candidate)
    entry = {"candidate": str(tmp_path / "candidate.json"), "proof": str(tmp_path / "verify/proof.json"),
             "card": str(tmp_path / "card/action.json"), "bundle": str(fixture[0])}
    catalog = A.catalog([entry], str(tmp_path / "catalog"))
    posts = []
    def post(url, body, headers, timeout):
        posts.append(body)
        return 200, {}, b'{"answers":{"pick":{"choice":"a0","confidence":0.99}}}'
    chooser = O.Client(url="https://example.invalid", key="", poster=post)
    before = len(runtime.sent)
    selection = A.select(catalog, "Read a complete result", client=chooser)
    assert selection["status"] == "selected" and selection["model_calls"] == 1 and len(posts) == 1
    assert len(runtime.sent) == before

    if change == "changed":
        file.write_bytes(file.read_bytes() + b"\nChanged external provenance")
    elif change == "deleted":
        file.unlink()
    else:
        file.rename(file.with_name(file.name + ".moved"))
    assert (R.digest(candidate), R.digest(proof), R.fingerprint(fixture[0])) == frozen
    ready, why = R.proof_status(candidate, proof, candidate["target"])
    assert not ready and "resource" in why
    with pytest.raises(ValueError, match="resource"):
        A.describe(candidate, proof, str(fixture[0]), "Stale result", "An unchanged saved proof is insufficient.",
                   str(tmp_path / "stale-card"))
    assert not (tmp_path / "stale-card").exists()
    excluded = A.catalog([entry], str(tmp_path / "excluded-catalog"))
    assert excluded["actions"] == [] and len(excluded["excluded"]) == 1 and "resource" in excluded["excluded"][0]["why"]
    with pytest.raises(ValueError, match="resource"):
        A.select(catalog, "Read a complete result", client=chooser)
    with pytest.raises(ValueError, match="resource"):
        A.execute(catalog, selection, fixture[2][3], str(tmp_path / "stale-selected-replay"))
    with pytest.raises(ValueError, match="resource"):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "stale-replay"), 1)
    assert len(posts) == 1 and len(runtime.sent) == before
    assert not (tmp_path / "stale-selected-replay").exists() and not (tmp_path / "stale-replay").exists()


def test_native_publication_status_checks_guest_guard_before_resource_access(fixture, runtime, tmp_path, monkeypatch):
    native_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    def refuse():
        raise PermissionError("VM-only")
    def unexpected(*args, **kwargs):
        pytest.fail("native proof status accessed resources before its guest guard")
    monkeypatch.setattr(R, "assert_guest", refuse)
    monkeypatch.setattr(R, "_file_hash", unexpected)
    monkeypatch.setattr(H, "resources", unexpected)
    with pytest.raises(PermissionError, match="VM-only"):
        R.proof_status(candidate, proof, candidate["target"])


@pytest.mark.parametrize("root_kind", ["getter", "objc", "read", "swift_read"])
def test_native_path_records_learns_verifies_publishes_and_replays_with_complete_pins(fixture, runtime, tmp_path, root_kind):
    from anyuse.execution import rea_actions as A
    from anyuse.execution import rea_runtime_bridge as B
    _, recipe, files = native_path_cases(fixture, tmp_path, root_kind=root_kind)
    demo = fixture[3][0]
    fixture[3][0] = H.record(fixture[1], demo["invocation"], demo["inputs"], str(fixture[0]), str(tmp_path / "record"), 1)
    recorded = R.load_json(tmp_path / "record/recording-check.json")
    assert recorded["status"] == "observed" and recorded["proof_created"] is False
    candidate = learn(fixture, tmp_path)
    assert candidate["actor"]["arguments"][0]["value"] == {"native_path": recipe}
    assert [argument["value"] for argument in candidate["actor"]["arguments"][1:]] == [
        {"input": "query"}, {"input": "reference"}]
    assert B.argument_leaves(candidate["actor"]["arguments"][0]) == []
    bound = H.bind(candidate, fixture[2][3])
    assert bound["arguments"][0]["value"]["native_path"] == recipe
    assert [argument["value"] for argument in bound["arguments"][1:]] == [fixture[2][3]["query"], fixture[2][3]["reference"]]
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    expected_files = {str(files[key]) for key in ("module", "abi", "consumer_abi", "contracts", "header", "build", "review")}
    if root_kind != "objc":
        expected_files.add(str(files["getter_abi"]))
    assert expected_files <= set(proof["resources"])
    for case in proof["cases"]:
        result = case["run"]["runtime"]["result"]
        assert result["native_calls"] == (2 if root_kind == "getter" else 1)
        assert result["invocations"] == 1 and result["handles_expired"] is True
        observed = result["operations"][0]["native_paths"][0]
        assert observed["recipe"] == recipe and observed["sealed"] is True and observed["dispatch_permit_consumed"] is True
    card = A.describe(candidate, proof, str(fixture[0]), "Read a complete result", "Use the measured typed path and check all result text.",
                      str(tmp_path / "card"))
    assert card["candidate_sha256"] == R.digest(candidate) and card["proof_sha256"] == R.digest(proof)
    replay = H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert replay["verdict"] == "verified" and replay["model_calls"] == 0 and len(runtime.sent) == 5
    assert B.native_path_recipes(candidate["actor"]) == [recipe]


@pytest.mark.parametrize("root_kind", ["objc", "read", "swift_read"])
@pytest.mark.parametrize("getters", [1, 2])
def test_native_path_owner_receiver_getters_count_before_publication(fixture, runtime, tmp_path, root_kind, getters):
    from anyuse.execution import rea_actions as A
    path = [{"selector": "ownedState" + str(index), "signature": "@16@0:8"} for index in range(getters)]
    native_path_cases(fixture, tmp_path, root_kind=root_kind, receiver_path=path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    results = [case["run"]["runtime"]["result"] for case in proof["cases"]]
    assert all(result["native_calls"] == 1 + getters and result["invocations"] == 1 for result in results)
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    results[0]["native_calls"] = getters
    assert proof["cases"][0]["seen"] == proof["cases"][0]["expected"]
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    before = len(runtime.sent)
    with pytest.raises(ValueError):
        A.describe(candidate, proof, str(fixture[0]), "Undercounted path", "All root getters must be recorded.", str(tmp_path / "card"))
    assert len(runtime.sent) == before and not (tmp_path / "card").exists()


@pytest.mark.parametrize("fault", ["extra_call", "shifted_epoch"])
def test_retained_borrowed_dispatch_binds_the_same_exact_epoch_as_the_live_worker(fixture, tmp_path, fault):
    """Retained evidence must not accept a call the live worker would reject."""
    from anyuse.execution import rea_runtime_bridge as B
    from test_rea_runtime_bridge import _native_path_files
    base = native_template(tmp_path)
    getter = native_template(tmp_path, [], [], return_type="pointer")
    getter["target"]["symbol"] = "inert_path_root"
    native_files = (base, getter, Path(base["target"]["module"]["path"]),
                    (tmp_path / "native-borrowed-outer-abi.json").resolve())
    actor, recipe, _ = _native_path_files(native_files, borrowed_root=True)
    candidate = {"target": {"bundle_id": "org.test.Native", "sha256": "a" * 64}, "actor": actor,
                 "params": [{"name": "payload", "type": "string", "source": "text"}]}
    inputs = {"payload": "23"}
    bound = B.bind_actor(actor, inputs)
    receipt = objc_receipt(candidate, bound)
    run = {"runtime": receipt, "pid": 443, "uid": 501, "bundle": "/tmp/Unseen.app",
           "permission": "runtime_attached"}
    assert H._objc_run(candidate, inputs, run)
    result = receipt["result"]
    if fault == "extra_call":
        result["native_calls"] = 3  # Above the old lower bound, outside this exact use window.
    else:
        result["operations"][0]["native_paths"][0]["borrowed_scope"].update(getter_epoch=2, checked_epoch=2)
    # All receipt shape/recipe and lower-bound checks still pass individually.
    request = B.invocation_request(bound)
    B.validate_native_path_observations(request["operations"][0], result["operations"][0])
    assert result["native_calls"] >= B.minimum_native_calls(actor)
    assert not H._objc_run(candidate, inputs, run)


@pytest.mark.parametrize("fault", ["missing", "extra", "recipe", "slot", "consumer", "owner", "queue", "lifetime",
    "unresolved", "unsealed", "permit", "expires", "hop", "null", "alias", "raw", "edge", "guard", "cycle",
    "read_mode", "calls", "cleanup", "selected_contract", "original_pin"])
def test_native_path_proof_refuses_bad_path_evidence_even_when_complete_output_matches(fixture, runtime, tmp_path, fault):
    from anyuse.execution import rea_actions as A
    _, recipe, files = native_path_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    receipt = proof["cases"][0]["run"]["runtime"]
    result = receipt["result"]
    row = result["operations"][0]
    observed = row["native_paths"][0]
    if fault == "missing": del row["native_paths"]
    elif fault == "extra": row["native_paths"].append(copy.deepcopy(observed))
    elif fault == "recipe": observed["recipe"]["preconditions"][1]["equals"] = 8
    elif fault == "slot": observed["argument"] = 1
    elif fault == "consumer": observed["consumer"]["callable"]["entry"]["symbol"] = "another_call"
    elif fault == "owner": observed["owner_retained"] = True
    elif fault == "queue": observed["queue"] = "worker"
    elif fault == "lifetime": observed["lifetime_mode"] = "retained_owner_main_queue_stable"
    elif fault == "unresolved": observed["resolved"] = False
    elif fault == "unsealed": observed["sealed"] = False
    elif fault == "permit": observed["dispatch_permit_consumed"] = False
    elif fault == "expires": observed["expires"] = "process"
    elif fault == "hop": observed["hops"].pop()
    elif fault == "null": observed["hops"][0]["pointer"]["handle"] = "n0"
    elif fault == "alias": observed["hops"][0]["pointer"]["alias"] = "yesterday"
    elif fault == "raw": observed["hops"][0]["pointer"]["address"] = "0x1000"
    elif fault == "edge": observed["hops"][0]["edge"]["unchanged"] = False
    elif fault == "guard": observed["hops"][0]["guards"][0]["matched"] = False
    elif fault == "cycle": observed["hops"][-1]["pointer"]["handle"] = observed["hops"][0]["pointer"]["handle"]
    elif fault == "read_mode":
        observed.update(argument=None, consumer=None, sealed=False, dispatch_permit_consumed=False)
    elif fault == "calls": result["native_calls"] = 1
    elif fault == "cleanup": result["handles_expired"] = False
    elif fault == "selected_contract":
        receipt["native_resources"][str(files["contracts"])]["native_path_contracts"].pop()
        result["native_resources"] = copy.deepcopy(receipt["native_resources"])
    else:
        del receipt["native_resources"][str(files["header"])]
        result["native_resources"] = copy.deepcopy(receipt["native_resources"])
    assert all(case["seen"] == case["expected"] and case["run"]["returncode"] == 0 for case in proof["cases"])
    before = len(runtime.sent)
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    with pytest.raises(ValueError):
        A.describe(candidate, proof, str(fixture[0]), "Invalid path", "Matching text cannot replace native path evidence.", str(tmp_path / "card"))
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "card").exists() and not (tmp_path / "replay").exists()


@pytest.mark.parametrize("scope", ["worker", "receipt", "proof"])
@pytest.mark.parametrize("field", ["child_retained", "bool_guard"])
def test_native_path_resource_bool_cannot_be_replaced_by_equal_integer(fixture, runtime, tmp_path, scope, field):
    from anyuse.execution import rea_actions as A
    from anyuse.execution import rea_native_path as P
    _, _, files = native_path_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    paths = lambda: [case["run"]["runtime"]["result"]["operations"][0]["native_paths"] for case in proof["cases"]]
    frozen = (R.digest(candidate), R.digest(paths()), R.digest([(case["seen"], case["expected"]) for case in proof["cases"]]))
    file = str(files["contracts"])
    original = copy.deepcopy(proof["cases"][0]["run"]["runtime"]["native_resources"][file])

    def corrupt(pins):
        contract = next(row["contract"] for row in pins[file]["native_path_contracts"]
                        if row["contract"]["format"] == P.LIFETIME_CONTRACT)
        if field == "child_retained":
            assert contract["native_child_retained"] is False
            contract["native_child_retained"] = 0
        else:
            assert contract["preconditions"][0]["equals"] is True
            contract["preconditions"][0]["equals"] = 1
        # The old Python equality admitted these different JSON contracts.
        assert pins[file] == original and R.digest(pins[file]) != R.digest(original)

    receipt = proof["cases"][0]["run"]["runtime"]
    if scope == "worker":
        corrupt(receipt["result"]["native_resources"])
    elif scope == "receipt":
        corrupt(receipt["native_resources"])
        receipt["result"]["native_resources"] = copy.deepcopy(receipt["native_resources"])
    else:
        corrupt(proof["resources"])
        for case in proof["cases"]:
            receipt = case["run"]["runtime"]
            corrupt(receipt["native_resources"])
            receipt["result"]["native_resources"] = copy.deepcopy(receipt["native_resources"])
    assert (R.digest(candidate), R.digest(paths()), R.digest([(case["seen"], case["expected"]) for case in proof["cases"]])) == frozen
    assert all(case["seen"] == case["expected"] and case["run"]["returncode"] == 0 for case in proof["cases"])
    before = len(runtime.sent)
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    with pytest.raises(ValueError):
        A.describe(candidate, proof, str(fixture[0]), "Coerced contract", "Complete output cannot replace exact typed resources.",
                   str(tmp_path / "card"))
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "card").exists() and not (tmp_path / "replay").exists()


@pytest.mark.parametrize("changed", ["offset", "guard", "path", "evidence"])
def test_learning_never_binds_native_path_metadata_or_merges_different_paths(fixture, runtime, tmp_path, changed):
    native_path_cases(fixture, tmp_path)
    recipe = fixture[3][1]["invocation"]["arguments"][0]["value"]["native_path"]
    if changed == "offset": recipe["types"][1]["fields"][1]["offset"] = 8
    elif changed == "guard": recipe["preconditions"][1]["equals"] = 8
    elif changed == "path":
        recipe["types"][0]["fields"][1]["name"] = "other_child"
        recipe["path"] = ["other_child"]
    else: recipe["root_evidence"]["claim"]["pointer"] = "/different_root"
    with pytest.raises(ValueError, match="varies across recordings"):
        learn(fixture, tmp_path)
    assert not runtime.sent and not (tmp_path / "learn").exists()


@pytest.mark.parametrize("fault", ["missing", "unsealed", "calls"])
def test_recording_native_path_requires_bound_receipt_and_stops_without_retry(fixture, runtime, tmp_path, monkeypatch, fault):
    native_path_cases(fixture, tmp_path)
    send = runtime.send
    def incomplete(self, candidate, inputs, timeout):
        run = send(self, candidate, inputs, timeout)
        result = run["runtime"]["result"]
        if fault == "missing": del result["operations"][0]["native_paths"]
        elif fault == "unsealed": result["operations"][0]["native_paths"][0]["sealed"] = False
        else: result["native_calls"] = 1
        return run
    monkeypatch.setattr(runtime, "send", incomplete)
    demo = fixture[3][0]
    with pytest.raises(ValueError, match="bound runtime evidence; no retry"):
        H.record(fixture[1], demo["invocation"], demo["inputs"], str(fixture[0]), str(tmp_path / "record"), 1)
    receipt = R.load_json(tmp_path / "record/recording-check.json")
    assert receipt["status"] == "incomplete" and receipt["proof_created"] is False and len(runtime.sent) == 1
    assert not (tmp_path / "record/recording.json").exists()


@pytest.mark.parametrize("native", [False, True])
def test_receipts_without_paths_allow_only_missing_or_empty_path_rows(fixture, runtime, tmp_path, native):
    if native:
        native_cases(fixture, tmp_path)
    else:
        for demo in fixture[3]:
            demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    rows = [case["run"]["runtime"]["result"]["operations"][0] for case in proof["cases"]]
    assert all("native_paths" not in row for row in rows) and R.proof_status(candidate, proof, candidate["target"])[0]
    for row in rows:
        row["native_paths"] = []
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    rows[0]["native_paths"] = [{"unexpected": "not part of this call"}]
    assert not R.proof_status(candidate, proof, candidate["target"])[0]


def test_native_borrowed_getter_arguments_are_learned_without_persisting_an_address(fixture, runtime, tmp_path):
    profile, inputs, demos = fixture[1:]
    profile["params"].append({"name": "counter", "type": "string", "source": "name"})
    for i, row in enumerate(inputs):
        row["counter"] = str(10 + i)
    getter = native_template(tmp_path, ["int32"], [10], return_type="pointer")
    getter["target"]["symbol"] = "inert_borrowed_state"
    actor = native_template(tmp_path, ["pointer", "utf8", "utf8"], [{"getter": getter}, "query", "reference"])
    for demo in demos:
        trace = copy.deepcopy(actor)
        trace["arguments"][0]["value"]["getter"]["arguments"][0]["value"] = int(demo["inputs"]["counter"])
        trace["arguments"][1]["value"] = demo["inputs"]["query"]
        trace["arguments"][2]["value"] = demo["inputs"]["reference"]
        demo["invocation"] = trace
    candidate = learn(fixture, tmp_path)
    learned_getter = candidate["actor"]["arguments"][0]["value"]["getter"]
    assert learned_getter["target"] == getter["target"]
    assert learned_getter["arguments"][0]["value"] == {"input": "counter"}
    assert H.bind(candidate, inputs[3])["arguments"][0]["value"]["getter"]["arguments"][0]["value"] == 13
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    assert getter["signature_evidence"]["file"] in proof["resources"]
    assert not any(key in learned_getter for key in ("address", "handle", "alias"))


def test_named_struct_input_field_is_distinct_from_a_scalar_binding(fixture, tmp_path):
    profile, inputs, demos = fixture[1:]
    profile["params"].append({"name": "counter", "type": "string", "source": "name"})
    for i, row in enumerate(inputs):
        row["counter"] = str(10 + i)
    actor = native_template(tmp_path, [{"kind": "struct", "fields": [{"name": "input", "type": "int32"}]}], [{"input": 10}])
    for demo in demos:
        demo["invocation"] = copy.deepcopy(actor)
        demo["invocation"]["arguments"][0]["value"]["input"] = int(demo["inputs"]["counter"])
    candidate = learn(fixture, tmp_path)
    assert candidate["actor"]["arguments"][0]["value"] == {"input": {"input": "counter"}}
    assert H.bind(candidate, inputs[3])["arguments"][0]["value"] == {"input": 13}


@pytest.mark.parametrize("nested", [False, True])
def test_borrowed_ivar_pointer_recipe_reuses_its_owner_and_recursive_layout_pins(fixture, runtime, tmp_path, nested):
    recipe = pointer_read_cases(fixture, tmp_path, nested=nested)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    row = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]
    assert row["pointer_reads"][0]["recipe"] == recipe
    assert row["pointer_reads"][0]["receiver_class"] != row["pointer_reads"][0]["field"]["class"]
    assert recipe["evidence"]["file"] in proof["resources"]
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    replay = H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert replay["verdict"] == "verified" and len(runtime.sent) == 4
    assert not any(key in candidate["actor"]["arguments"][0]["value"] for key in ("address", "handle", "alias"))


@pytest.mark.parametrize("change", ["recipe", "receiver", "owner", "encoding", "module", "offset", "bounds", "image", "handle", "alias", "expiry", "nil", "missing"])
def test_private_pointer_proof_requires_exact_runtime_ivar_provenance(fixture, runtime, tmp_path, change):
    pointer_read_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    row = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]
    observed = row["pointer_reads"][0]
    if change == "recipe":
        observed["recipe"]["field"]["name"] = "anotherState"
    elif change == "receiver":
        observed["receiver_class"] = "AnotherReachedClass"
    elif change == "owner":
        observed["field"]["class"] = "AnotherIvarOwner"
    elif change == "encoding":
        observed["field"]["encoding"] = "Q"
    elif change == "module":
        observed["field"]["module"] = "/tmp/other-image.bin"
    elif change == "offset":
        observed["field"]["offset"] = "24"
    elif change == "bounds":
        observed["instance_bytes"] = 20
    elif change == "image":
        observed["image_identity"]["uuid"] = "f" * 32
    elif change == "handle":
        observed["pointer"]["handle"] = "0xdeadbeef"
    elif change == "alias":
        observed["pointer"]["alias"] = "persisted_object"
    elif change == "expiry":
        observed["pointer"]["expires"] = "next_launch"
    elif change == "nil":
        observed["pointer"] = None
    else:
        row.pop("pointer_reads")
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_private_read_metadata_cannot_change_across_learning_examples(fixture, tmp_path):
    pointer_read_cases(fixture, tmp_path)
    fixture[3][1]["invocation"]["arguments"][0]["value"]["read"]["field"]["offset"] = 24
    with pytest.raises(ValueError, match="argument layout varies"):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


def test_borrowed_ivar_layout_contract_change_refuses_replay_before_actuation(fixture, runtime, tmp_path):
    recipe = pointer_read_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    Path(recipe["evidence"]["file"]).write_text("Changed declared private layout")
    before = len(runtime.sent)
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "refused").exists()


def test_previous_native_receipt_without_pointer_reads_remains_valid_for_no_read_recipe(fixture, runtime, tmp_path):
    native_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    for case in proof["cases"]:
        case["run"]["runtime"]["result"]["operations"][0].pop("pointer_reads")
    assert H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("nested", [False, True])
def test_swift_optional_pointer_mapping_freezes_reflected_field_and_borrowed_lifetime(fixture, runtime, tmp_path, nested):
    recipe = swift_read_cases(fixture, tmp_path, nested=nested)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    observed = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["swift_field_reads"][0]
    assert observed["source"] == "swift_metadata" and observed["recipe"] == recipe
    assert recipe["evidence"]["file"] in proof["resources"]
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    assert H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)["verdict"] == "verified"
    assert len(runtime.sent) == 4
    assert observed["lifetime"]["native_handle_retained"] is False
    assert not any(key in observed["recipe"] for key in ("address", "handle", "alias"))


def split_swift_proof(fixture, runtime, tmp_path):
    native_metadata_image((tmp_path / "native-image.bin").resolve(), split_segments=True)
    swift_read_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    for case in proof["cases"]:
        observed = case["run"]["runtime"]["result"]["operations"][0]["swift_field_reads"][0]
        observed["field"].update(descriptor_offset="0x4100", mangled_type_offset="0x8140")
    return candidate, proof


def test_swift_metadata_in_pinned_data_segments_can_exceed_frida_text_size(fixture, runtime, tmp_path):
    candidate, proof = split_swift_proof(fixture, runtime, tmp_path)
    observed = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["swift_field_reads"][0]
    assert observed["reported_module_size"] == 0x2000 and observed["module_size"] == 0x9000
    assert int(observed["field"]["descriptor_offset"], 16) > observed["reported_module_size"]
    assert H.proof_status(candidate, proof, candidate["target"])[0]


def test_instruction_section_cannot_span_a_gap_in_the_declared_image(fixture, runtime, tmp_path):
    candidate, proof = split_swift_proof(fixture, runtime, tmp_path)
    receipt = proof["cases"][0]["run"]["runtime"]
    row = receipt["result"]["operations"][0]
    path = candidate["actor"]["target"]["module"]["path"]
    row["function"]["instruction_section"]["bytes"] = 0x3100
    pin = receipt["native_resources"][path]
    pin["macho_sections"][0]["sections"][0] = copy.deepcopy(row["function"]["instruction_section"])
    receipt["result"]["native_resources"][path] = copy.deepcopy(pin)
    proof["resources"][path] = copy.deepcopy(pin)
    assert H._native_function_run(row, candidate["actor"], 0)
    assert not H._native_section_pin_run(row["function"], receipt["native_resources"])
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("change", ["descriptor_hole", "descriptor_crosses", "mangled_hole", "mangled_crosses", "mangled_zero", "mangled_bool",
    "mangled_large", "missing_segments", "segment_image", "segment_overlap", "extent", "reported_size", "text_envelope"])
def test_declared_image_extent_cannot_replace_whole_range_segment_membership(fixture, runtime, tmp_path, change):
    candidate, proof = split_swift_proof(fixture, runtime, tmp_path)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    receipt = proof["cases"][0]["run"]["runtime"]
    row = receipt["result"]["operations"][0]
    observed = row["swift_field_reads"][0]
    if change == "descriptor_hole":
        observed["field"]["descriptor_offset"] = "0x2000"
    elif change == "descriptor_crosses":
        observed["field"]["descriptor_offset"] = "0x4fe0"
    elif change == "mangled_hole":
        observed["field"]["mangled_type_offset"] = "0x5000"
    elif change == "mangled_crosses":
        observed["field"]["mangled_type_offset"] = "0x8ff8"
    elif change in {"mangled_zero", "mangled_bool", "mangled_large"}:
        observed["field"]["mangled_type_bytes"] = {"mangled_zero": 0, "mangled_bool": True, "mangled_large": 1026}[change]
    elif change == "extent":
        observed["module_size"] += 8
    elif change == "reported_size":
        observed["reported_module_size"] = True
    elif change == "text_envelope":
        observed["module_size"] = observed["reported_module_size"]
    else:
        path = candidate["actor"]["arguments"][0]["value"]["swift_read"]["module"]["path"]
        pin = receipt["native_resources"][path]
        if change == "missing_segments":
            pin.pop("macho_segments")
        elif change == "segment_image":
            pin["macho_segments"][0]["image_identity"]["uuid"] = "f" * 32
        else:
            pin["macho_segments"][0]["segments"][1]["offset"] = "0x1fff"
        receipt["result"]["native_resources"][path] = copy.deepcopy(pin)
        proof["resources"][path] = copy.deepcopy(pin)
    # Test the range gate directly, beyond the separate resource-echo check.
    assert not H._native_swift_reads_run(row, candidate["actor"], receipt["native_resources"])
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("change", ["source", "recipe", "receiver", "class", "type", "offset", "index", "count", "vector", "absent_vector", "vector_extent", "class_extent", "descriptor", "mangled",
    "bounds", "module_size", "image", "kind", "bytes", "stride", "alignment", "canonical", "encoding", "copyable", "handle", "nil", "alias", "lifetime",
    "precondition", "precondition_type", "precondition_kind", "precondition_index", "precondition_bounds", "precondition_overlap", "precondition_byte", "precondition_byte_bool", "precondition_byte_value", "missing"])
def test_swift_pointer_proof_refuses_wrong_reflection_layout_and_failed_preconditions(fixture, runtime, tmp_path, change):
    swift_read_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    row = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]
    observed = row["swift_field_reads"][0]
    if change == "source":
        observed["source"] = "objc_ivar"
    elif change == "recipe":
        observed["recipe"]["preconditions"][0]["equals"] = True
    elif change == "receiver":
        observed["receiver_class"] = "AnotherModule.MappedReader"
    elif change == "class":
        observed["swift_class"]["module"] = "AnotherModule"
    elif change == "type":
        observed["field"]["type_name"] = "Swift.Optional<Swift.Int>"
    elif change == "offset":
        observed["field"]["offset"] = 24
    elif change == "index":
        observed["field"]["index"] = True
    elif change == "count":
        observed["field"]["num_fields"] = 0
    elif change == "vector":
        observed["field"]["vector_offset_words"] = -1
    elif change == "absent_vector":
        observed["field"]["vector_offset_words"] = 0
    elif change == "vector_extent":
        observed["class_size"] = 72
    elif change == "class_extent":
        observed["class_address_point"] = observed["class_size"]
    elif change == "descriptor":
        observed["field"]["descriptor_offset"] = "0x2000"
    elif change == "mangled":
        observed["field"].pop("mangled_type_offset")
    elif change == "bounds":
        observed["instance_bytes"] = 20
    elif change == "module_size":
        observed["module_size"] = True
    elif change == "image":
        observed["image_identity"]["uuid"] = "f" * 32
    elif change == "kind":
        observed["layout"]["metadata_kind"] = 0x200
    elif change in {"bytes", "stride", "alignment"}:
        observed["layout"]["alignment_mask" if change == "alignment" else change] = 1
    elif change == "canonical":
        observed["layout"]["canonical_type_verified"] = False
    elif change == "encoding":
        observed["layout"]["encoding"] = "non_optional_pointer"
    elif change == "copyable":
        observed["layout"]["copyable"] = False
    elif change == "handle":
        observed["pointer"]["handle"] = "0x123456789"
    elif change == "nil":
        observed["pointer"] = None
    elif change == "alias":
        observed["pointer"]["alias"] = "from_an_old_process"
    elif change == "lifetime":
        observed["lifetime"]["native_handle_retained"] = True
    elif change == "precondition":
        observed["preconditions"][0]["value"] = True
    elif change == "precondition_type":
        observed["preconditions"][0]["value"] = 0
    elif change == "precondition_kind":
        observed["preconditions"][0]["layout"]["metadata_kind"] = 0x202
    elif change == "precondition_index":
        observed["preconditions"][0]["index"] = observed["field"]["index"]
    elif change == "precondition_bounds":
        observed["preconditions"][0]["offset"] = observed["instance_bytes"]
    elif change == "precondition_overlap":
        observed["preconditions"][0]["offset"] = observed["field"]["offset"] + 1
    elif change == "precondition_byte":
        observed["preconditions"][0]["raw_byte"] = 2
    elif change == "precondition_byte_bool":
        observed["preconditions"][0]["raw_byte"] = False
    elif change == "precondition_byte_value":
        observed["preconditions"][0]["raw_byte"] = 1
    else:
        row.pop("swift_field_reads")
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("flag", [0x00010000, 0x00100000, 0x00400000, 0x00800000, 0x01000000, 0x02000000])
def test_swift_field_raw_flags_cannot_be_hidden_behind_positive_layout_labels(fixture, runtime, tmp_path, flag):
    swift_read_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    observed = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["swift_field_reads"][0]
    observed["layout"]["flags"] |= flag
    assert observed["layout"]["pod"] and observed["layout"]["complete"]
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_swift_read_contract_change_refuses_before_replay_and_cannot_use_empty_objc_encoding(fixture, runtime, tmp_path):
    from anyuse.execution import rea_runtime_bridge as B
    recipe = swift_read_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    wrong_route = native_pointer_read(tmp_path)
    wrong_route["field"]["encoding"] = ""
    actor = copy.deepcopy(candidate["actor"])
    actor["arguments"][0]["value"] = {"read": wrong_route}
    with pytest.raises(ValueError):
        B.validate_actor(actor, allow_bindings=True)
    Path(recipe["evidence"]["file"]).write_text("Changed independent Swift layout contract")
    before = len(runtime.sent)
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "refused").exists()


def test_previous_native_receipt_without_swift_read_rows_remains_valid_for_no_read_recipe(fixture, runtime, tmp_path):
    native_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    for case in proof["cases"]:
        case["run"]["runtime"]["result"]["operations"][0].pop("swift_field_reads")
    assert H.proof_status(candidate, proof, candidate["target"])[0]


def test_native_swift_recipe_requires_the_actual_swift_bridge_in_its_proof(fixture, runtime, tmp_path):
    profile, inputs, demos = fixture[1:]
    profile["params"].append({"name": "counter", "type": "string", "source": "name"})
    for i, row in enumerate(inputs):
        row["counter"] = str(10 + i)
    actor = native_template(tmp_path, ["int32"], [10], abi="swift")
    for demo in demos:
        demo["invocation"] = copy.deepcopy(actor)
        demo["invocation"]["arguments"][0]["value"] = int(demo["inputs"]["counter"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["function"]["bridge"] = "frida-native"
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_swift_metadata_pair_keeps_declared_integer_fields_and_nested_getter_bindings(fixture, runtime, tmp_path):
    profile, inputs, demos = fixture[1:]
    profile["params"] += [{"name": "counter", "type": "string", "source": "name"}, {"name": "other", "type": "string", "source": "text"}]
    pair = {"kind": "swift_type", "module": "MappedModule", "name": "MappedPair", "category": "struct", "bytes": 16,
            "fields": [{"name": "x", "type": "int64"}, {"name": "y", "type": "int64"}]}
    getter = native_template(tmp_path, ["int64", "int64"], ["10", "20"], abi="swift", return_type=pair)
    getter["target"]["symbol"] = "inert_swift_pair_constructor"
    actor = native_template(tmp_path, [pair], [{"getter": getter}], abi="swift")
    for i, row in enumerate(inputs):
        row.update(counter=str(10 + i), other=str(20 + i))
    for demo in demos:
        trace = copy.deepcopy(actor)
        for argument, name in zip(trace["arguments"][0]["value"]["getter"]["arguments"], ("counter", "other")):
            argument["value"] = demo["inputs"][name]
        demo["invocation"] = trace
    candidate = learn(fixture, tmp_path)
    learned = candidate["actor"]["arguments"][0]
    assert learned["type"] == pair
    assert [argument["value"] for argument in learned["value"]["getter"]["arguments"]] == [{"input": "counter"}, {"input": "other"}]
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    bound = H.bind(candidate, inputs[3])
    assert [argument["value"] for argument in bound["arguments"][0]["value"]["getter"]["arguments"]] == ["13", "23"]
    assert getter["signature_evidence"]["file"] in proof["resources"]


def test_partial_native_sequence_cannot_verify_matching_ax_output_or_retry(fixture, runtime, tmp_path, monkeypatch):
    native_cases(fixture, tmp_path, mixed=True)
    candidate = learn(fixture, tmp_path)
    original = runtime.send
    def partial(self, c, inputs, timeout):
        run = original(self, c, inputs, timeout)
        run["returncode"] = 1
        run["runtime"]["status"] = run["runtime"]["result"]["status"] = "incomplete"
        run["runtime"]["result"]["operations"] = run["runtime"]["result"]["operations"][:1]
        run["runtime"]["result"]["invocations"] = 1
        return run
    monkeypatch.setattr(runtime, "send", partial)
    with pytest.raises(ValueError, match="stopped without retry"):
        H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "partial"), 1)
    proof = R.load_json(tmp_path / "partial/proof.json")
    assert len(runtime.sent) == 1 and proof["status"] == "incomplete"
    assert proof["cases"][0]["seen"] == proof["cases"][0]["expected"]
    assert proof["cases"][0]["verdict"] == "refuted"


def test_two_method_recipe_learns_numeric_boolean_bindings_and_reuses_full_result_gate(fixture, runtime, tmp_path):
    sequence_cases(fixture)
    report = H.run(fixture[1], fixture[3], cases(fixture), [{"inputs": x} for x in fixture[2][3:]],
                   str(fixture[0]), str(tmp_path / "sequence"), 1)
    candidate = R.load_json(tmp_path / "sequence/learn/candidate.json")
    expected = objc_sequence({"input": "query"}, {"input": "reference"}, {"input": "counter"}, {"input": "animate"})
    assert candidate["actor"] == expected
    bound = H.bind(candidate, fixture[2][3])
    assert bound["steps"][1]["arguments"] == [{"type": "int32", "value": 13}, {"type": "bool", "value": False}]
    proof = R.load_json(tmp_path / "sequence/verify/proof.json")
    assert report["status"] == "verified" and len(runtime.sent) == 6
    assert all(row["run"]["runtime"]["result"]["invocations"] == 2 for row in proof["cases"])


@pytest.mark.parametrize("change", ["order", "count", "nested"])
def test_sequence_structure_cannot_change_across_learning_cases(fixture, tmp_path, change):
    sequence_cases(fixture)
    actor = fixture[3][1]["invocation"]
    if change == "order":
        actor["steps"].reverse()
    elif change == "count":
        actor["steps"].append(copy.deepcopy(actor["steps"][0]))
    else:
        actor["steps"][1] = copy.deepcopy(fixture[3][0]["invocation"])
    with pytest.raises(ValueError):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


def test_ambiguous_converted_numeric_bindings_cannot_be_learned(fixture, tmp_path):
    sequence_cases(fixture)
    fixture[1]["params"].append({"name": "counter_copy", "type": "string", "source": "name"})
    for row in fixture[2]:
        row["counter_copy"] = row["counter"]
    with pytest.raises(ValueError, match="unique typed runtime parameter"):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


@pytest.mark.parametrize("change", ["missing", "order", "index", "abi", "argument_types", "count", "request"])
def test_sequence_proof_requires_every_ordered_named_method(fixture, runtime, tmp_path, change):
    sequence_cases(fixture)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    receipt = proof["cases"][0]["run"]["runtime"]
    rows = receipt["result"]["operations"]
    if change == "missing":
        rows.pop()
    elif change == "order":
        rows.reverse()
    elif change == "index":
        rows[1]["index"] = 0
    elif change == "abi":
        rows[1]["method"]["signature"] = "v@:@"
    elif change == "argument_types":
        rows[1]["method"]["argument_types"] = ["uint32", "bool"]
    elif change == "count":
        receipt["result"]["invocations"] = 1
    else:
        receipt["request"]["operations"].reverse()
        receipt["request_sha256"] = R.digest(receipt["request"])
        receipt["result"]["request_sha256"] = receipt["request_sha256"]
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_partial_sequence_cannot_verify_even_when_first_step_matches_full_output(fixture, runtime, tmp_path, monkeypatch):
    sequence_cases(fixture)
    candidate = learn(fixture, tmp_path)
    original = runtime.send
    def send(self, c, inputs, timeout):
        run = original(self, c, inputs, timeout)
        run["returncode"] = 1
        receipt = run["runtime"]
        receipt["status"] = receipt["result"]["status"] = "incomplete"
        receipt["result"]["operations"] = receipt["result"]["operations"][:1]
        receipt["result"]["invocations"] = 1
        return run
    monkeypatch.setattr(runtime, "send", send)
    with pytest.raises(ValueError, match="stopped without retry"):
        H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "partial"), 1)
    proof = R.load_json(tmp_path / "partial/proof.json")
    assert proof["status"] == "incomplete" and len(runtime.sent) == 1
    row = proof["cases"][0]
    assert row["seen"] == row["expected"] and row["verdict"] == "refuted"


def test_invalid_later_scalar_is_refused_before_ready_or_any_bridge_call(fixture, tmp_path, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    sequence_cases(fixture)
    candidate = learn(fixture, tmp_path)
    class Native:
        pid = 443
        ready_calls = 0
        def __init__(self, bundle, bundle_id):
            pass
        def _ready(self):
            self.ready_calls += 1
    calls = []
    monkeypatch.setattr(H.AE, "Runtime", Native)
    monkeypatch.setattr(B, "process_identity", lambda *args: native_context_identity(fixture[0], candidate))
    monkeypatch.setattr(B, "invoke", lambda *args, **kwargs: calls.append(args))
    native = H.Runtime(fixture[0], candidate)
    native.receipt_dir = tmp_path / "should-not-exist"
    inputs = {**fixture[2][3], "counter": str(2**31)}
    with pytest.raises(ValueError):
        native.send(candidate, inputs, 1)
    assert native.native.ready_calls == 0 and not calls and not native.receipt_dir.exists()


def test_mutable_working_document_autosaves_without_changing_immutable_oracles(fixture, runtime, tmp_path):
    working = mutable_cases(fixture, tmp_path)
    original = {row["reference"]: R._file_hash(Path(row["reference"])) for row in fixture[2]}
    runtime.autosave = True
    report = H.run(fixture[1], fixture[3], cases(fixture), [{"inputs": x} for x in fixture[2][3:]],
                   str(fixture[0]), str(tmp_path / "mutable"), 1)
    assert report["status"] == "verified" and len(runtime.sent) == 6
    assert working.read_text() == Path(fixture[2][-1]["reference"]).read_text()
    assert {path: R._file_hash(Path(path)) for path in original} == original
    candidate = R.load_json(tmp_path / "mutable/learn/candidate.json")
    proof = R.load_json(tmp_path / "mutable/verify/proof.json")
    changed = copy.deepcopy(candidate)
    changed["params"][-1]["mutable"] = False
    assert not H.proof_status(changed, proof, changed["target"])[0]


def test_mutable_working_role_does_not_permit_immutable_oracle_changes(fixture, runtime, tmp_path):
    mutable_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    runtime.autosave = runtime.reference_changes = True
    with pytest.raises(ValueError, match="independent reference changed"):
        H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == 1


@pytest.mark.parametrize("kind", ["file_text", "fen_plist", "dictionary_xml"])
def test_mutable_paths_cannot_supply_substantive_reference_fields(fixture, tmp_path, kind):
    mutable_cases(fixture, tmp_path)
    params = fixture[1]["params"]
    reference = {"fields": {"body": {"kind": kind, "param": "working"}}}
    with pytest.raises(ValueError, match="cannot be substantive"):
        H._reference(reference, H._params(params), params)


@pytest.mark.parametrize("value,kind", [(1, "path"), ("true", "path"), (None, "path"), (True, "string"), (False, "string")])
def test_mutable_modifier_is_only_a_boolean_path_declaration(value, kind):
    with pytest.raises(ValueError):
        H._params([{"name": "working", "type": kind, "source": "path", "mutable": value}])


@pytest.mark.parametrize("change", ["symlink", "parent_symlink", "directory", "owner"])
def test_mutable_path_identity_changes_refuse_before_standalone_replay(fixture, runtime, tmp_path, monkeypatch, change):
    working = mutable_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    inputs = copy.deepcopy(fixture[2][3])
    if change == "symlink":
        working.unlink()
        working.symlink_to(inputs["reference"])
    elif change == "directory":
        working.unlink()
        working.mkdir()
    elif change == "parent_symlink":
        link = tmp_path / "parent-link"
        link.symlink_to(working.parent, target_is_directory=True)
        inputs["working"] = str(link / working.name)
    else:
        uid = H.os.getuid()
        monkeypatch.setattr(H.os, "getuid", lambda: uid + 1)
    before = len(runtime.sent)
    with pytest.raises(ValueError):
        H.replay(candidate, proof, inputs, str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "refused").exists()


@pytest.mark.parametrize("alias", ["same_path", "hardlink"])
def test_standalone_replay_refuses_mutable_oracle_alias_before_actuation(fixture, runtime, tmp_path, alias):
    working = mutable_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    inputs = copy.deepcopy(fixture[2][3])
    if alias == "same_path":
        inputs["reference"] = str(working)
    else:
        link = (tmp_path / "oracle-alias.txt").resolve()
        os.link(working, link)
        inputs["reference"] = str(link)
    before = len(runtime.sent)
    with pytest.raises(ValueError, match="overlap or alias"):
        H.replay(candidate, proof, inputs, str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "refused").exists()


def test_cross_case_mutable_oracle_overlap_stops_verification_up_front(fixture, runtime, tmp_path):
    working = mutable_cases(fixture, tmp_path)
    candidate = learn(fixture, tmp_path)
    verifying = copy.deepcopy(cases(fixture))
    other = (tmp_path / "other-working.txt").resolve()
    other.write_text("Another working document")
    verifying[1]["inputs"].update(working=str(other), reference=str(working))
    with pytest.raises(ValueError, match="overlap or alias"):
        H.verify(candidate, verifying, str(fixture[0]), str(tmp_path / "refused"), 1)
    assert not runtime.sent and not (tmp_path / "refused").exists()


def test_cross_learning_case_working_oracle_overlap_is_rejected_before_oracle_read(fixture, tmp_path):
    working = mutable_cases(fixture, tmp_path)
    other = (tmp_path / "other-working.txt").resolve()
    other.write_text("Another working document")
    fixture[3][1]["inputs"].update(working=str(other), reference=str(working))
    with pytest.raises(ValueError, match="overlap or alias"):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


@pytest.mark.parametrize("change", ["permission", "status", "cleanup", "identity", "target", "request", "digest", "invocations",
                                    "method", "process", "handles", "attached", "asset"])
def test_private_method_proof_requires_bound_runtime_invocation_evidence(fixture, runtime, tmp_path, change):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    row = proof["cases"][0]
    receipt = row["run"]["runtime"]
    if change == "permission":
        row["run"]["permission"] = "not_required"
    elif change == "status":
        receipt["status"] = "observed"
    elif change == "cleanup":
        receipt["result"]["cleanup"]["detached"] = False
    elif change == "identity":
        receipt["after_identity"]["launch_time"] += 1
    elif change == "target":
        receipt["target"] = {**receipt["target"], "bundle_id": "wrong.target"}
    elif change == "request":
        receipt["request"] = {"operations": []}
    elif change == "digest":
        receipt["request_sha256"] = "0" * 64
    elif change == "invocations":
        receipt["result"]["invocations"] = 0
    elif change == "method":
        receipt["result"]["operations"][0]["method"]["signature"] = "wrong ABI"
    elif change == "process":
        receipt["result"]["process"]["pid"] += 1
    elif change == "handles":
        receipt["result"]["handles_expired"] = False
    elif change == "attached":
        receipt["result"]["attached"] = False
    else:
        receipt["adapter"]["sha256"] = "3" * 64
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def modern_runtime_lifecycle(runtime, monkeypatch):
    """Keep the existing inert operation/resources, adding the current envelope."""
    from anyuse.execution import rea_runtime_bridge as B
    send = runtime.send
    def modern(self, candidate, inputs, timeout):
        run = send(self, candidate, inputs, timeout)
        receipt = run["runtime"]
        receipt["format"] = B.RUNTIME
        receipt["result"].update(format=B.WORKER, cleanup_errors=[],
            frida_lifecycle={"format": B.FRIDA_LIFECYCLE, "manager_state": "closed",
                             "attachment_attempted": True, "helper_exit_confirmed": False, "error": None})
        return run
    monkeypatch.setattr(runtime, "send", modern)


def lifecycle_cases(fixture, tmp_path):
    """Synthetic scalar actor with the real reviewed-file resource collector."""
    from anyuse.execution import rea_runtime_bridge as B
    from test_rea_runtime_bridge import _lifecycle_path_files
    base = native_template(tmp_path, ["int32"], [0], return_type="void")
    getter = native_template(tmp_path, [], [], return_type="pointer")
    getter["target"]["symbol"] = "unused_old_getter"
    files = (base, getter, Path(base["target"]["module"]["path"]),
             (tmp_path / "lifecycle-outer-abi.json").resolve())
    actor, recipe, pins = _lifecycle_path_files(files, return_type="void", external_hooks=True,
        arguments=[{"type": "int32", "value": {"input": "payload"}}])
    fixture[1]["params"].append({"name": "payload", "type": "string", "source": "text"})
    for index, inputs in enumerate(fixture[2]):
        inputs["payload"] = str(23 + index)
    for demo in fixture[3]:
        demo["invocation"] = B.bind_actor(actor, {"payload": demo["inputs"]["payload"]})
    return actor, recipe, pins


def observed_lifecycle_runtime(runtime, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    from test_rea_native_path import _lifecycle_result
    modern_runtime_lifecycle(runtime, monkeypatch)
    send = runtime.send
    def observed(self, candidate, inputs, timeout):
        run = send(self, candidate, inputs, timeout)
        result = run["runtime"]["result"]
        recipe = B.request_native_path_recipes(run["runtime"]["request"])[0]
        result["lifecycle_observer"] = _lifecycle_result(recipe, for_call=True)
        return run
    monkeypatch.setattr(runtime, "send", observed)


def test_lifecycle_observer_supports_retained_proof_and_fresh_inert_replay_with_frozen_metadata(
        fixture, runtime, tmp_path, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    _, recipe, files = lifecycle_cases(fixture, tmp_path)
    observed_lifecycle_runtime(runtime, monkeypatch)
    candidate = learn(fixture, tmp_path)
    assert B.native_path_recipes(candidate["actor"])[0] == recipe
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    for case in proof["cases"]:
        receipt = case["run"]["runtime"]
        result = receipt["result"]
        assert receipt["format"] == B.RUNTIME and result["format"] == B.WORKER
        assert result["native_calls"] == result["invocations"] == 1
        assert B.validate_lifecycle_observer_result(receipt["request"], result) == result["lifecycle_observer"]
        assert str(files["hooks"]) in receipt["native_resources"]
        assert result["frida_lifecycle"]["helper_exit_confirmed"] is False
    replay = H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert replay["verdict"] == "verified" and len(runtime.sent) == 4
    assert B.native_path_recipes(candidate["actor"])[0] == recipe


@pytest.mark.parametrize("change", ["missing", "digest", "scope_generation", "scope_owner", "birth", "death",
                                    "hooks", "callbacks", "expiry", "extra_call", "legacy_envelope"])
def test_retained_lifecycle_global_ledger_cannot_be_stripped_or_mutated_to_publish_or_replay(
        fixture, runtime, tmp_path, monkeypatch, change):
    from anyuse.execution import rea_actions as A
    from anyuse.execution import rea_runtime_bridge as B
    lifecycle_cases(fixture, tmp_path)
    observed_lifecycle_runtime(runtime, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    case = proof["cases"][0]
    receipt, result = case["run"]["runtime"], case["run"]["runtime"]["result"]
    observer = result["lifecycle_observer"]
    scope = result["operations"][0]["native_paths"][0]["lifecycle_scope"]
    if change == "missing": del result["lifecycle_observer"]
    elif change == "digest": observer["observer_sha256"] = "0" * 64
    elif change == "scope_generation": scope["generation"] = 2
    elif change == "scope_owner": scope["owner_thread_id"] = 102
    elif change == "birth": observer["events"][2]["kind"] = "destructor_leave"
    elif change == "death":
        observer["events"].insert(6, {"seq": 7, "kind": "destructor_enter", "generation": 1,
                                       "entry": 0, "return_site": None, "thread": 101})
        for seq, event in enumerate(observer["events"], 1): event["seq"] = seq
        scope["consume_event"] = 8
    elif change == "hooks": observer["hooks_detached"] -= 1
    elif change == "callbacks": observer["callbacks_inflight"] = 1
    elif change == "expiry": observer["tokens_expired"] = False
    elif change == "extra_call": result["native_calls"] = 2
    else:
        receipt["format"], result["format"] = B.LEGACY_RUNTIME, B.LEGACY_WORKER
        del result["frida_lifecycle"], result["cleanup_errors"]
    # Each path row and task output still passes its independent old checks.
    B.validate_native_path_observations(receipt["request"]["operations"][0], result["operations"][0])
    assert result["native_calls"] >= B.minimum_request_native_calls(receipt["request"])
    assert all(row["seen"] == row["expected"] for row in proof["cases"])
    assert result["cleanup"] == {"script_unloaded": True, "detached": True}
    before = len(runtime.sent)
    assert not H._objc_run(candidate, case["inputs"], case["run"], check_files=False)
    assert not H.proof_status(candidate, proof, candidate["target"])[0]
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    with pytest.raises(ValueError):
        A.describe(candidate, proof, str(fixture[0]), "Invalid observer", "A scalar output cannot replace a lifecycle ledger.",
                   str(tmp_path / "card"))
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "card").exists() and not (tmp_path / "replay").exists()


def test_lifecycle_metadata_is_not_a_task_binding_and_scalar_payload_does_not_rewrite_it(fixture, tmp_path):
    lifecycle_cases(fixture, tmp_path)
    # Varying a discovery policy cannot be inferred as an input binding.
    fixture[3][1]["invocation"]["arguments"][0]["value"]["native_path"]["root"]["lifecycle"]["observe_ms"] = 1001
    with pytest.raises(ValueError):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


@pytest.mark.parametrize("native", [False, True])
def test_modern_lifecycle_supports_retained_proof_and_fresh_replay_without_helper_exit_claim(
        fixture, runtime, tmp_path, monkeypatch, native):
    from anyuse.execution import rea_runtime_bridge as B
    if native:
        native_cases(fixture, tmp_path)
    else:
        for demo in fixture[3]:
            demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    modern_runtime_lifecycle(runtime, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    for case in proof["cases"]:
        receipt = case["run"]["runtime"]
        assert receipt["format"] == B.RUNTIME and receipt["result"]["format"] == B.WORKER
        assert B._frida_lifecycle(receipt["result"])["manager_state"] == "closed"
        assert receipt["result"]["frida_lifecycle"]["helper_exit_confirmed"] is False
        assert receipt["result"]["cleanup_errors"] == []
    replay = H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert replay["verdict"] == "verified" and len(runtime.sent) == 4


@pytest.mark.parametrize("change", [
    "pending", "failed", "helper_exit_claim", "cleanup_error", "missing_lifecycle", "missing_errors",
    "stripped_disposition", "missing_runtime_format", "missing_worker_format", "runtime_legacy",
    "worker_legacy", "legacy_pair_with_modern_fields",
])
def test_modern_retained_lifecycle_cannot_be_mutated_or_stripped_to_publish_or_replay(
        fixture, runtime, tmp_path, monkeypatch, change):
    from anyuse.execution import rea_actions as A
    from anyuse.execution import rea_runtime_bridge as B
    native_cases(fixture, tmp_path)
    modern_runtime_lifecycle(runtime, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    case = proof["cases"][0]
    assert H._objc_run(candidate, case["inputs"], case["run"], check_files=False)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    receipt = case["run"]["runtime"]
    result = receipt["result"]
    detail = {"type": "RuntimeError", "message": "cooperative manager close failed"}
    if change == "pending":
        result["frida_lifecycle"]["manager_state"] = "pending"
    elif change == "failed":
        result["frida_lifecycle"].update(manager_state="failed", error=detail)
        result["cleanup_errors"] = [{"phase": "manager_close", "error": detail}]
    elif change == "helper_exit_claim":
        result["frida_lifecycle"]["helper_exit_confirmed"] = True
    elif change == "cleanup_error":
        result["cleanup_errors"] = [{"phase": "journal", "error": detail}]
    elif change == "missing_lifecycle":
        del result["frida_lifecycle"]
    elif change == "missing_errors":
        del result["cleanup_errors"]
    elif change == "stripped_disposition":
        del result["frida_lifecycle"]
        del result["cleanup_errors"]
    elif change == "missing_runtime_format":
        del receipt["format"]
    elif change == "missing_worker_format":
        del result["format"]
    elif change == "runtime_legacy":
        receipt["format"] = B.LEGACY_RUNTIME
    elif change == "worker_legacy":
        result["format"] = B.LEGACY_WORKER
    else:
        receipt["format"], result["format"] = B.LEGACY_RUNTIME, B.LEGACY_WORKER
    # Correct task output and the old two cleanup flags do not replace the
    # independent disposition. Only retained lifecycle/version data changed.
    assert all(row["seen"] == row["expected"] for row in proof["cases"])
    assert result["cleanup"] == {"script_unloaded": True, "detached": True}
    assert not receipt.get("resource_review_required")
    before = len(runtime.sent)
    assert not H._objc_run(candidate, case["inputs"], case["run"], check_files=False)
    assert not H.proof_status(candidate, proof, candidate["target"])[0]
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    with pytest.raises(ValueError):
        A.describe(candidate, proof, str(fixture[0]), "Invalid lifecycle", "A task result cannot replace cleanup evidence.",
                   str(tmp_path / "card"))
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "card").exists() and not (tmp_path / "replay").exists()


def test_legacy_runtime_proof_remains_legacy_without_synthesized_manager_or_helper_cleanup(fixture, runtime, tmp_path):
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    before = copy.deepcopy(proof)
    for case in proof["cases"]:
        receipt = case["run"]["runtime"]
        assert receipt["format"] == B.LEGACY_RUNTIME and receipt["result"]["format"] == B.LEGACY_WORKER
        assert "frida_lifecycle" not in receipt["result"] and "cleanup_errors" not in receipt["result"]
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    assert proof == before


def test_private_method_execution_does_not_verify_wrong_independent_output(fixture, runtime, tmp_path):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    runtime.wrong = True
    clock = iter(range(1000))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(H.time, "monotonic", lambda: next(clock))
        with pytest.raises(ValueError, match="stopped without retry"):
            H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "refused"), 1)
    proof = R.load_json(tmp_path / "refused/proof.json")
    assert proof["status"] == "incomplete" and len(runtime.sent) == 1
    assert proof["cases"][0]["run"]["runtime"]["status"] == "executed"
    assert proof["cases"][0]["verdict"] == "refuted"


def test_private_method_structure_and_input_types_cannot_vary_between_recordings(fixture, tmp_path):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    fixture[3][1]["invocation"]["receiver"]["class"] = "AnotherReader"
    with pytest.raises(ValueError, match="varies across recordings"):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


def test_private_method_rejects_abi_incompatible_and_unknown_frozen_bindings(fixture, tmp_path):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    for value, argument_type in [({"input": "unknown"}, "utf8"), ({"input": "query"}, "bool")]:
        changed = copy.deepcopy(candidate)
        changed["actor"]["arguments"][0] = {"type": argument_type, "value": value}
        with pytest.raises(ValueError):
            H.validate(changed)


@pytest.mark.parametrize("pinned", [False, True])
def test_real_shared_runtime_uses_one_bridge_transaction_with_preserved_receipt(fixture, tmp_path, monkeypatch, pinned):
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    app, inputs = fixture[0], fixture[2][3]
    identity = native_context_identity(app, candidate)
    calls, context_options = [], []
    class Native:
        pid = 443
        def __init__(self, bundle, bundle_id, **options):
            context_options.append(options)
            self.ready = 0
        def _ready(self):
            self.ready += 1
        def send_invocation(self, *args):
            raise AssertionError("runtime actor must not fall back to Apple Events")
    def invoke(bundle, actor, out, timeout=15, *, expected_identity):
        calls.append((bundle, copy.deepcopy(actor), timeout, copy.deepcopy(expected_identity)))
        receipt = objc_receipt(candidate, actor)
        receipt["expected_identity"] = copy.deepcopy(expected_identity)
        receipt["identity"] = copy.deepcopy(identity)
        receipt["bundle"] = identity["bundle"]
        receipt["after_identity"] = copy.deepcopy(identity)
        receipt["result"]["process"]["executable"] = identity["executable"]
        folder = Path(out)
        folder.mkdir()
        R.write_json(folder / "runtime.json", receipt)
        return receipt
    monkeypatch.setattr(H.AE, "Runtime", Native)
    monkeypatch.setattr(B, "process_identity", lambda *args: copy.deepcopy(identity))
    monkeypatch.setattr(B, "invoke", invoke)
    disposition = R._ReplayDisposition() if pinned else None
    native = H.Runtime(app, candidate, **({"expected_identity": identity, "_disposition": disposition} if pinned else {}))
    native.receipt_dir = tmp_path / "actual-runtime-path"
    run = native.send(candidate, inputs, 120)
    assert run["returncode"] == 0 and run["permission"] == "runtime_attached"
    assert calls == [(str(app), H.bind(candidate, inputs), 60, identity)]
    assert context_options == [{"expected_identity": identity} if pinned else {}]
    if pinned:
        assert disposition.worker_started is True and disposition.cleanup_confirmed is True
    assert native.native.ready == 1
    assert (native.receipt_dir / "runtime.json").is_file()


def test_native_replay_supplied_epoch_refuses_successor_before_context(fixture, tmp_path, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    identity = native_context_identity(fixture[0], candidate)
    monkeypatch.setattr(B, "process_identity", lambda *args: {**identity, "launch_time": identity["launch_time"] + 1})
    monkeypatch.setattr(H.AE, "Runtime", lambda *a, **k: pytest.fail("successor reached GUI context"))
    monkeypatch.setattr(B, "invoke", lambda *a, **k: pytest.fail("successor reached native actor"))
    with pytest.raises(ValueError, match="supplied epoch"):
        H.Runtime(fixture[0], candidate, expected_identity=identity)


def test_learned_replay_expected_epoch_rejects_unsupported_actor_before_context(fixture, tmp_path, monkeypatch):
    candidate = learn(fixture, tmp_path)
    monkeypatch.setattr(H, "Runtime", lambda *a, **k: pytest.fail("unsupported replay constructed a context"))
    with pytest.raises(ValueError, match="cannot bind"):
        H.replay(candidate, {}, fixture[2][3], str(fixture[0]), str(tmp_path / "unsupported-pin"), 1,
                 expected_identity=native_context_identity(fixture[0], candidate))
    assert not (tmp_path / "unsupported-pin").exists()


@pytest.mark.parametrize("fault", ["absent", "pid", "epoch"])
def test_pinned_existing_gui_context_never_launches_or_adopts_successor(fixture, tmp_path, monkeypatch, fault):
    from types import SimpleNamespace
    from anyuse.execution import rea_runtime_bridge as B
    candidate = learn(fixture, tmp_path)
    app = fixture[0]
    identity = native_context_identity(app, candidate)
    process_calls = []
    def process(*args):
        process_calls.append(True)
        return {**identity, "launch_time": identity["launch_time"] + (1 if fault == "epoch" and len(process_calls) > 1 else 0)}
    def forbidden(*args, **kwargs): raise AssertionError("pinned context attempted launch, activation or AE initialization")
    live = SimpleNamespace(bundleURL=lambda: SimpleNamespace(path=lambda: str(app)),
                           processIdentifier=lambda: identity["pid"] + (fault == "pid"), activateWithOptions_=forbidden)
    monkeypatch.setattr(B, "process_identity", process)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSRunningApplication=SimpleNamespace(
        runningApplicationsWithBundleIdentifier_=lambda bundle_id: [] if fault == "absent" else [live])))
    monkeypatch.setitem(sys.modules, "ApplicationServices", SimpleNamespace(AXIsProcessTrusted=lambda: True))
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(CGSessionCopyCurrentDictionary=lambda: {}))
    monkeypatch.setattr(H.AE.subprocess, "run", forbidden)
    monkeypatch.setattr(H.AE.C, "CDLL", forbidden)
    with pytest.raises(ValueError, match="existing app|process epoch"):
        H.AE.Runtime(app, bundle_id=identity["bundle_id"], expected_identity=identity)


@pytest.mark.parametrize("changed", ["launch_time", "uid", "executable", "pid", "bundle", "bundle_id"])
def test_native_runtime_refuses_changed_complete_identity_before_dispatch(fixture, tmp_path, monkeypatch, changed):
    from types import SimpleNamespace
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    identity = native_context_identity(fixture[0], candidate)
    process_calls, ready = [], []
    def process(bundle, bundle_id):
        process_calls.append((bundle, bundle_id))
        return identity  # Mutation must not change the captured defensive pin.
    monkeypatch.setattr(H.AE, "Runtime", lambda *a, **k: SimpleNamespace(pid=443, _ready=lambda: ready.append(True)))
    monkeypatch.setattr(B, "process_identity", process)
    monkeypatch.setattr(B, "invoke", lambda *a, **k: pytest.fail("changed process reached native dispatch"))
    native = H.Runtime(fixture[0], candidate)
    native.receipt_dir = tmp_path / "never-runtime"
    if changed in {"launch_time", "uid", "pid"}:
        identity[changed] += 1
    elif changed == "executable":
        identity[changed] = str(fixture[0].resolve() / "Contents/MacOS/Successor")
    elif changed == "bundle":
        identity[changed] = str(tmp_path / "Other.app")
    else:
        identity[changed] = "org.test.Other"
    with pytest.raises(ValueError):
        native.send(candidate, fixture[2][3], 1)
    assert len(process_calls) == 2 and ready == [True]
    assert not native.receipt_dir.exists()


def test_native_runtime_binds_initial_identity_to_ordinary_context_pid(fixture, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    identity = {**native_context_identity(fixture[0], candidate), "pid": 444}
    monkeypatch.setattr(H.AE, "Runtime", lambda *a, **k: SimpleNamespace(pid=443))
    monkeypatch.setattr(B, "process_identity", lambda *a: identity)
    monkeypatch.setattr(B, "invoke", lambda *a, **k: pytest.fail("initial successor reached native dispatch"))
    with pytest.raises(ValueError, match="original context PID"):
        H.Runtime(fixture[0], candidate)


def test_native_runtime_keeps_pin_when_bridge_mutates_forwarded_copy(fixture, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    identity = native_context_identity(fixture[0], candidate)
    forwarded = []
    monkeypatch.setattr(H.AE, "Runtime", lambda *a, **k: SimpleNamespace(pid=443, _ready=lambda: None))
    monkeypatch.setattr(B, "process_identity", lambda *a: identity)
    def invoke(bundle, actor, out, *, timeout, expected_identity):
        forwarded.append(copy.deepcopy(expected_identity))
        expected_identity["launch_time"] += 1
        receipt = objc_receipt(candidate, actor)
        receipt.update(bundle=identity["bundle"], expected_identity=copy.deepcopy(identity),
                       identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity))
        receipt["result"]["process"]["executable"] = identity["executable"]
        return receipt
    monkeypatch.setattr(B, "invoke", invoke)
    native = H.Runtime(fixture[0], candidate)
    for index in range(2):
        native.receipt_dir = tmp_path / f"runtime-{index}"
        assert native.send(candidate, fixture[2][3], 1)["returncode"] == 0
    assert forwarded == [identity, identity] and identity["launch_time"] == 1000.25


@pytest.mark.parametrize("change", ["matched_successor", "missing_expected", "changed_expected", "identity_only", "after_only"])
def test_native_runtime_retains_incoherent_returned_identity_for_resource_review(fixture, tmp_path, monkeypatch, change):
    from types import SimpleNamespace
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    inputs = fixture[2][3]
    identity = native_context_identity(fixture[0], candidate)
    lookups, returned = [], []
    def process(*args):
        lookups.append(copy.deepcopy(identity))
        return copy.deepcopy(identity)
    def invoke(bundle, actor, out, *, timeout, expected_identity):
        assert expected_identity == identity
        receipt = objc_receipt(candidate, actor)
        receipt.update(bundle=identity["bundle"], expected_identity=copy.deepcopy(expected_identity),
                       identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity))
        receipt["result"]["process"]["executable"] = identity["executable"]
        if change == "matched_successor":
            receipt["identity"]["launch_time"] += 1
            receipt["after_identity"]["launch_time"] += 1
        elif change == "missing_expected":
            receipt.pop("expected_identity")
        elif change == "changed_expected":
            receipt["expected_identity"]["launch_time"] += 1
        elif change == "identity_only":
            receipt["identity"]["launch_time"] += 1
        else:
            receipt["after_identity"]["launch_time"] += 1
        # Historical proof checks retain their old semantics. In particular,
        # internally matching epochs or missing expected pins were not a live
        # process handoff and must now fail at the current caller boundary.
        if change in {"matched_successor", "missing_expected", "changed_expected"}:
            assert H._objc_run(candidate, inputs, {"runtime": receipt, "pid": identity["pid"],
                "uid": identity["uid"], "bundle": identity["bundle"], "permission": "runtime_attached"})
        folder = Path(out)
        folder.mkdir()
        R.write_json(folder / "runtime.json", receipt)
        returned.append(copy.deepcopy(receipt))
        return receipt
    monkeypatch.setattr(H.AE, "Runtime", lambda *a, **k: SimpleNamespace(pid=443, _ready=lambda: None))
    monkeypatch.setattr(B, "process_identity", process)
    monkeypatch.setattr(B, "invoke", invoke)
    native = H.Runtime(fixture[0], candidate)
    native.receipt_dir = tmp_path / "retained-runtime"
    with pytest.raises(R.ResourceReviewRequired, match="review retained receipt, no retry"):
        native.send(candidate, inputs, 1)
    assert lookups == [identity, identity] and len(returned) == 1
    assert R.load_json(native.receipt_dir / "runtime.json") == returned[0]


def test_verification_refuses_midtrial_successor_before_next_actor(fixture, tmp_path, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    identity = native_context_identity(fixture[0], candidate)
    original = copy.deepcopy(identity)
    instances, calls = [], []
    class Native:
        pid = 443
        def __init__(self, bundle, bundle_id):
            self.state = tree("seed", "Seed full result.")
            instances.append(self)
        def _ready(self):
            pass
        def read(self, **kwargs):
            return copy.deepcopy(self.state)
    def invoke(bundle, actor, out, *, timeout, expected_identity):
        calls.append(copy.deepcopy(expected_identity))
        receipt = objc_receipt(candidate, actor)
        receipt.update(bundle=original["bundle"], expected_identity=copy.deepcopy(expected_identity),
                       identity=copy.deepcopy(original), after_identity=copy.deepcopy(original))
        receipt["result"]["process"]["executable"] = original["executable"]
        query, reference = [argument["value"] for argument in actor["arguments"][:2]]
        instances[0].state = tree(query, Path(reference).read_text())
        # The same bundle and PID can now name a different launch epoch.
        identity["launch_time"] += 1
        return receipt
    monkeypatch.setattr(H.AE, "Runtime", Native)
    monkeypatch.setattr(B, "process_identity", lambda *a: identity)
    monkeypatch.setattr(B, "invoke", invoke)
    folder = tmp_path / "midtrial-successor"
    with pytest.raises(ValueError, match="no successor adoption"):
        H.verify(candidate, cases(fixture), str(fixture[0]), str(folder), 1)
    proof = R.load_json(folder / "proof.json")
    assert calls == [original] and proof["status"] == "incomplete"
    assert len(proof["cases"]) == 1 and proof["cases"][0]["verdict"] == "verified"
    assert not (folder / "case-1/runtime").exists()


def test_changed_runtime_adapter_stops_replay_before_actuation(fixture, runtime, tmp_path, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    changed = objc_adapter()
    changed["sha256"] = "3" * 64
    monkeypatch.setattr(B, "adapter_identity", lambda: changed)
    before = len(runtime.sent)
    with pytest.raises(ValueError, match="pinned reference resource changed"):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "refused").exists()


def test_named_runtime_generated_method_can_pass_with_independent_complete_output(fixture, runtime, tmp_path, monkeypatch):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    original = runtime.send
    def send(self, c, inputs, timeout):
        run = original(self, c, inputs, timeout)
        method = run["runtime"]["result"]["operations"][0]["method"]
        method.update(origin="runtime_generated", module=None, implementation_offset=None)
        return run
    monkeypatch.setattr(runtime, "send", send)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "generated"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    assert all(row["seen"] == row["expected"] for row in proof["cases"])
    assert proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["method"]["module"] is None


@pytest.mark.parametrize("origin", ["module_backed", "runtime_generated"])
def test_measured_leading_dot_method_class_keeps_full_abi_and_provenance_checks(fixture, runtime, tmp_path, monkeypatch, origin):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    original = runtime.send
    def send(self, c, inputs, timeout):
        run = original(self, c, inputs, timeout)
        method = run["runtime"]["result"]["operations"][0]["method"]
        method["class"] = "..NSKVONotifying_IINA.MainWindow"
        if origin == "runtime_generated":
            method.update(origin=origin, module=None, implementation_offset=None)
        return run
    monkeypatch.setattr(runtime, "send", send)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "leading-dot"), 1)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    method = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["method"]
    assert method["class"] == "..NSKVONotifying_IINA.MainWindow"
    method["signature"] = "v16@0:8"
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("origin,module,offset", [
    ("runtime_generated", "/tmp/method.dylib", None),
    ("runtime_generated", None, "0x123"),
    ("module_backed", None, None),
    ("module_backed", "relative-module", "0x123"),
    ("module_backed", "/tmp/method.dylib", "unknown"),
    ("unknown", None, None),
    (None, None, None),
])
def test_runtime_method_provenance_cannot_be_missing_or_inconsistent(fixture, runtime, tmp_path, origin, module, offset):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    method = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["method"]
    method.update(origin=origin, module=module, implementation_offset=offset)
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_runtime_generated_origin_requires_explicit_null_provenance_fields(fixture, runtime, tmp_path):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    method = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["method"]
    method["origin"] = "runtime_generated"
    del method["module"], method["implementation_offset"]
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


def test_gadget_route_is_pinned_while_independent_readback_and_cleanup_stay_required(fixture, runtime, tmp_path, monkeypatch):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    adapter = use_gadget(runtime, monkeypatch)
    report = H.run(fixture[1], fixture[3], cases(fixture), [{"inputs": x} for x in fixture[2][3:]],
                   str(fixture[0]), str(tmp_path / "gadget"), 1)
    proof = R.load_json(tmp_path / "gadget/verify/proof.json")
    assert report["status"] == "verified" and proof["resources"]["runtime_bridge"] == adapter
    assert all(row["seen"] == row["expected"] and row["observer"] == "learned_ax" for row in proof["cases"])
    assert all(row["run"]["runtime"]["result"]["preloaded_gadget_remains"] is True for row in proof["cases"])
    assert proof["cases"][0]["run"]["runtime"]["result"]["cleanup"] == {"script_unloaded": True, "detached": True}


@pytest.mark.parametrize("change", ["kind", "endpoint", "token", "library", "config", "token_owner", "mode"])
def test_changed_gadget_route_or_assets_refuse_before_replay_actuation(fixture, runtime, tmp_path, monkeypatch, change):
    from anyuse.execution import rea_runtime_bridge as B
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    adapter = use_gadget(runtime, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    changed = copy.deepcopy(adapter)
    transport = changed["transport"]
    if change == "kind":
        changed["transport"] = {"kind": "local"}
    elif change == "endpoint":
        transport["endpoint"] = "127.0.0.1:27043"
    elif change == "token_owner":
        transport["token_file"]["uid"] = 502
    elif change == "mode":
        transport["config"]["mode"] = "0644"
    else:
        transport["token_file" if change == "token" else change]["sha256"] = "6" * 64
    monkeypatch.setattr(B, "adapter_identity", lambda: changed)
    before = len(runtime.sent)
    with pytest.raises(ValueError):
        H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "refused"), 1)
    assert len(runtime.sent) == before and not (tmp_path / "refused").exists()


@pytest.mark.parametrize("change", ["top_missing", "worker_missing", "endpoint", "preload_false", "preload_missing", "owner"])
def test_gadget_proof_requires_transport_echo_and_honest_preload_lifecycle(fixture, runtime, tmp_path, monkeypatch, change):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    use_gadget(runtime, monkeypatch)
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    receipt = proof["cases"][0]["run"]["runtime"]
    if change == "top_missing":
        del receipt["transport"]
    elif change == "worker_missing":
        del receipt["result"]["transport"]
    elif change == "endpoint":
        receipt["result"]["transport"]["endpoint"] = "127.0.0.1:27043"
    elif change == "preload_false":
        receipt["result"]["preloaded_gadget_remains"] = False
    elif change == "preload_missing":
        del receipt["result"]["preloaded_gadget_remains"]
    else:
        for transport in (receipt["transport"], receipt["result"]["transport"], receipt["adapter"]["transport"]):
            transport["token_file"]["uid"] = transport["config"]["uid"] = 502
    assert not H.proof_status(candidate, proof, candidate["target"])[0]


@pytest.mark.parametrize("native", [False, True])
def test_old_local_receipt_schema_replays_with_explicit_current_local_default(fixture, runtime, tmp_path, monkeypatch, native):
    from anyuse.execution import rea_runtime_bridge as B
    if native:
        pointer_read_cases(fixture, tmp_path, nested=True)
    else:
        for demo in fixture[3]:
            demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    candidate = learn(fixture, tmp_path)
    proof = H.verify(candidate, cases(fixture), str(fixture[0]), str(tmp_path / "verify"), 1)
    del proof["resources"]["runtime_bridge"]["transport"]
    assert "transport" not in proof["cases"][0]["run"]["runtime"]["adapter"]
    current = objc_adapter()
    current["transport"] = {"kind": "local"}
    monkeypatch.setattr(B, "adapter_identity", lambda: current)
    assert H.proof_status(candidate, proof, candidate["target"])[0]
    result = H.replay(candidate, proof, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"), 1)
    assert result["verdict"] == "verified" and len(runtime.sent) == 4


@pytest.mark.parametrize("transport", [None, {"kind": "unknown"}, {"kind": "local", "endpoint": "127.0.0.1:27042"}])
def test_runtime_transport_defaults_do_not_hide_unknown_or_malformed_routes(transport):
    with pytest.raises(ValueError):
        H._runtime_transport(transport)


@pytest.mark.parametrize("key", ["token_file", "library", "config"])
def test_runtime_receipts_cannot_replace_external_gadget_assets(fixture, runtime, tmp_path, monkeypatch, key):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    adapter = use_gadget(runtime, monkeypatch)
    candidate = learn(fixture, tmp_path)
    forbidden = Path(adapter["transport"][key]["file"]).resolve()
    with pytest.raises(ValueError, match="outside app and pinned resources"):
        H._outside(candidate, fixture[0].resolve(), forbidden)


def test_content_root_generalizes_to_multiple_runs_and_checks_the_whole_body(fixture, tmp_path):
    c = learn(fixture, tmp_path)
    inputs = fixture[2][3]
    rows = tree(inputs["query"], "Independent full")
    rows.append({"parent": 4, "role": "AXStaticText", "AXValue": "result 3."})
    correct = H.expected(c, inputs)[0]
    assert H.observe(c, rows, inputs) == correct
    rows.append({"parent": 4, "role": "AXStaticText", "AXValue": "Additional wrong content."})
    assert H.observe(c, rows, inputs) != correct


def test_result_heading_cannot_replace_the_query_control(fixture, tmp_path):
    for demo in fixture[3]:
        demo["after"].append({"parent": 0, "role": "AXStaticText", "AXValue": demo["inputs"]["query"]})
    c = learn(fixture, tmp_path)
    assert c["selectors"]["query"]["where"]["role"] == "AXTextField"
    assert c["selectors"]["query"]["read"] == "AXValue"


def test_boolean_control_state_cannot_count_as_full_text(fixture, tmp_path):
    for demo in fixture[3]:
        Path(demo["inputs"]["reference"]).write_text("1")
        demo["after"] = [
            {"parent": None, "role": "AXWindow"},
            {"parent": 0, "role": "AXTextField", "AXSubrole": "AXSearchField", "AXValue": demo["inputs"]["query"]},
            {"parent": 0, "role": "AXCheckBox", "AXTitle": "Flag", "AXValue": "1"}]
    with pytest.raises(ValueError, match="full-output selector: body"):
        learn(fixture, tmp_path)


def scalar_spec(*, kind="decimal", unit="percent", minimum="0", maximum="100"):
    field = {"kind": "file_scalar", "param": "reference", "scalar_type": kind, "unit": unit}
    if kind == "decimal":
        field.update(min=minimum, max=maximum)
    return field


@pytest.mark.parametrize("value,canonical", [("45.000", "45"), ("4.5e1", "45"), ("-0.0", "0"),
    ("0.10000000000000000000000000001", "0.10000000000000000000000000001")])
def test_staged_scalar_decimal_preserves_exact_property_value_without_float_rounding(fixture, value, canonical):
    field = H._scalar_reference_spec(scalar_spec(), fixture[1]["params"])
    assert H._scalar_value(value, field) == canonical


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "0x2d", "045", "45,0", "true", "101", "-1"])
def test_staged_scalar_reference_refuses_nonfinite_ambiguous_and_out_of_range_values(fixture, value):
    field = H._scalar_reference_spec(scalar_spec(), fixture[1]["params"])
    with pytest.raises(ValueError):
        H._scalar_value(value, field)


def test_boolean_scalar_oracle_is_distinct_from_numeric_or_mixed_control_state(fixture):
    field = H._scalar_reference_spec(scalar_spec(kind="boolean", unit="boolean"), fixture[1]["params"])
    assert H._scalar_value("false", field) == "false"
    assert H._scalar_value("0", field, observation=True) == "false"
    assert H._scalar_value("1", field, observation=True) == "true"
    for value in ("0", "1", "False", "-1", "mixed"):
        with pytest.raises(ValueError):
            H._scalar_value(value, field)
    with pytest.raises(ValueError):
        H._scalar_value("-1", field, observation=True)


@pytest.mark.parametrize("change", ["unit", "bounds", "reversed", "noncanonical", "mutable", "string", "normalization"])
def test_staged_scalar_contract_requires_explicit_immutable_typed_property_schema(fixture, change):
    params = copy.deepcopy(fixture[1]["params"])
    field = scalar_spec()
    if change == "unit":
        field["unit"] = "unreviewed_unit"
    elif change == "bounds":
        field.pop("max")
    elif change == "reversed":
        field.update(min="10", max="0")
    elif change == "noncanonical":
        field["min"] = "0.0"
    elif change == "mutable":
        params[1]["mutable"] = True
    elif change == "string":
        params[1]["type"] = "string"
    else:
        field["normalization"] = "compact"
    with pytest.raises(ValueError):
        H._scalar_reference_spec(field, params)


def test_staged_scalar_oracle_pins_independent_file_bytes_and_is_vm_guarded(fixture, tmp_path, monkeypatch):
    file = (tmp_path / "expected-percent.txt").resolve()
    file.write_text("45.000\n")
    inputs = {**fixture[2][0], "reference": str(file)}
    value, pin = H._scalar_reference_file(scalar_spec(), fixture[1]["params"], inputs)
    assert value == "45" and pin == {"path": str(file), "sha256": R._file_hash(file)}
    file.write_text("45\n")
    after, changed = H._scalar_reference_file(scalar_spec(), fixture[1]["params"], inputs)
    assert after == value and changed["sha256"] != pin["sha256"]
    def refuse():
        raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    with pytest.raises(PermissionError, match="VM only"):
        H._scalar_reference_file(scalar_spec(), fixture[1]["params"], inputs)


def test_staged_scalar_oracle_cannot_alias_a_mutable_working_document(fixture, tmp_path):
    working = mutable_cases(fixture, tmp_path)
    alias = (tmp_path / "aliased-percent.txt").resolve()
    os.link(working, alias)
    inputs = {**fixture[2][0], "reference": str(alias)}
    with pytest.raises(ValueError, match="overlap or alias"):
        H._scalar_reference_file(scalar_spec(), fixture[1]["params"], inputs)


def test_scalar_preparation_does_not_enable_a_live_or_after_state_fitted_observer(fixture):
    field = scalar_spec()
    H._scalar_reference_spec(field, fixture[1]["params"])
    with pytest.raises(ValueError, match="unknown independent reference source"):
        H._reference({"fields": {"property": field}}, H._params(fixture[1]["params"]), fixture[1]["params"])


def test_query_echo_and_ambiguous_or_wrong_full_results_cannot_be_learned(fixture, tmp_path):
    app, profile, inputs, demos = fixture
    p = copy.deepcopy(profile)
    del p["reference"]["fields"]["body"]
    with pytest.raises(ValueError, match="echoes"):
        H.learn(p, demos, str(app), str(tmp_path / "echo"))
    for demo in demos:
        demo["after"].append({"parent": 0, "role": "AXTextField", "AXSubrole": "AXSearchField",
                              "AXValue": demo["inputs"]["query"]})
    with pytest.raises(ValueError, match="unambiguous"):
        learn(fixture, tmp_path)
    assert not (tmp_path / "learn").exists()


def test_same_input_in_two_roles_refuses_ambiguous_binding(fixture, tmp_path):
    app, profile, inputs, demos = fixture
    for demo in demos:
        demo["inputs"]["query"] = demo["inputs"]["reference"]
        demo["invocation"]["parameters"]["term"]["value"] = demo["inputs"]["query"]
    with pytest.raises(ValueError, match="unique parameter binding"):
        learn(fixture, tmp_path)


def test_one_driver_learns_verifies_and_replays_only_novel_heldouts(fixture, runtime, tmp_path):
    app, profile, inputs, demos = fixture
    report = H.run(profile, demos, cases(fixture), [{"inputs": r} for r in inputs[3:]],
                   str(app), str(tmp_path / "run"), timeout=1)
    assert report["status"] == "verified" and report["verified_cases"] == 3
    assert len(report["heldout"]) == 3 and len(runtime.sent) == 6
    with pytest.raises(ValueError, match="held-out inputs"):
        H.run(profile, demos, cases(fixture), cases(fixture), str(app), str(tmp_path / "overlap"))
    assert not (tmp_path / "overlap").exists()


def test_recorder_captures_independently_checked_data_and_does_not_create_a_proof(fixture, runtime, tmp_path):
    app, profile, inputs, demos = fixture
    demo = H.record(profile, demos[0]["invocation"], inputs[0], str(app), str(tmp_path / "record"), timeout=1)
    assert demo["inputs"] == inputs[0] and len(runtime.sent) == 1
    assert R.load_json(tmp_path / "record/recording-check.json")["status"] == "observed"
    assert not (tmp_path / "record/proof.json").exists()
    runtime.wrong = True
    with pytest.raises(ValueError, match="independent task reference"):
        H.record(profile, demos[1]["invocation"], inputs[1], str(app), str(tmp_path / "wrong"), timeout=1)
    assert len(runtime.sent) == 2 and not (tmp_path / "wrong/recording.json").exists()


def test_wrong_result_stops_after_one_send_and_never_retries(fixture, runtime, tmp_path):
    app = fixture[0]
    c = learn(fixture, tmp_path)
    runtime.wrong = True
    with pytest.raises(ValueError, match="stopped without retry"):
        R.verify(c, cases(fixture), str(app), str(tmp_path / "proof"), timeout=1)
    assert len(runtime.sent) == 1
    assert R.load_json(tmp_path / "proof/proof.json")["status"] == "incomplete"


def test_noop_and_changed_reference_are_rejected(fixture, runtime, tmp_path):
    app, _, inputs, _ = fixture
    c = learn(fixture, tmp_path)
    runtime.initial = tree(inputs[0]["query"], Path(inputs[0]["reference"]).read_text())
    with pytest.raises(ValueError, match="no-op"):
        R.verify(c, cases(fixture), str(app), str(tmp_path / "noop"))
    assert not runtime.sent
    runtime.initial = tree("seed", "Seed full result.")
    runtime.reference_changes = True
    with pytest.raises(ValueError, match="reference changed"):
        R.verify(c, cases(fixture), str(app), str(tmp_path / "changed"))
    assert len(runtime.sent) == 1


def test_proof_invalidation_and_surface_requires_complete_task_receipt(fixture, runtime, tmp_path):
    app, _, inputs, _ = fixture
    c = learn(fixture, tmp_path)
    proof = R.verify(c, cases(fixture), str(app), str(tmp_path / "proof"))
    surface = R.candidate_surface(c, proof, R.fingerprint(app), app="Any Reader")
    need = S.Need(app="Any Reader", effect="scoped_find", values={"lookup": inputs[3]["query"], "path": inputs[3]["reference"]})
    assert R.map_bindings(surface, need)[0] == "served"
    spec = S.readback_for(surface, need)
    assert S.judge_readback(spec, "rea_adapter", {"source": "ax", "values": {"lookup": inputs[3]["query"]}})["verdict"] == "uncertain"
    result = R.replay(c, proof, inputs[3], str(app), str(tmp_path / "replay"))
    assert S.judge_readback(spec, "rea_adapter", {"source": "ax", "rea_result": result})["verdict"] == "verified"
    result["inputs"] = inputs[4]
    assert S.judge_readback(spec, "rea_adapter", {"source": "ax", "rea_result": result})["verdict"] == "refuted"
    altered = copy.deepcopy(c)
    altered["label"] = "Different recipe"
    assert not R.proof_status(altered, proof, c["target"])[0]
    (app / "Contents/resource").write_text("same-version change")
    with pytest.raises(ValueError, match="fingerprint changed"):
        R.replay(c, proof, inputs[4], str(app), str(tmp_path / "must-not-exist"))
    assert not (tmp_path / "must-not-exist").exists()


def test_document_primitive_and_window_scope_are_data_not_app_code(fixture, tmp_path):
    app, profile, inputs, demos = fixture
    profile["effect"], profile["item_kind"] = "read_content", "document"
    profile["params"] = [profile["params"][1]]
    profile["reference"] = {"window_path_param": "reference", "fields": {
        "path": {"kind": "file_identity", "param": "reference"},
        "body": {"kind": "file_text", "param": "reference"}}}
    for demo in demos:
        path = demo["inputs"]["reference"]
        demo["inputs"] = {"reference": path}
        demo["invocation"] = {"kind": "open_document", "path": path}
        demo["after"] = [{"parent": None, "role": "AXWindow", "AXTitle": Path(path).name,
                          "AXDocument": Path(path).resolve().as_uri()},
                         {"parent": 0, "role": "AXTextArea", "AXValue": Path(path).read_text()},
                         {"parent": None, "role": "AXWindow", "AXTitle": Path(path).name,
                          "AXDocument": (Path(path).resolve().parent / "other" / Path(path).name).as_uri()},
                         {"parent": 2, "role": "AXTextArea", "AXValue": "Different text"}]
    c = learn(fixture, tmp_path)
    assert c["actor"] == {"kind": "open_document", "path": {"input": "reference"}}
    observed = H.observe(c, demos[0]["after"], demos[0]["inputs"])
    assert observed["body"] == Path(inputs[0]["reference"]).read_text()
    duplicate = copy.deepcopy(demos[0]["after"])
    duplicate[2]["AXDocument"] = duplicate[0]["AXDocument"]
    with pytest.raises(ValueError, match="ambiguous document"):
        H.observe(c, duplicate, demos[0]["inputs"])
    wrong = copy.deepcopy(demos[0]["after"])
    wrong[0]["AXDocument"] = wrong[2]["AXDocument"]
    assert H.observe(c, wrong, demos[0]["inputs"])["path"] is None


def test_vm_guard_precedes_learning_native_actions_and_reference_reads(monkeypatch, fixture, tmp_path):
    c = learn(fixture, tmp_path)
    def refuse():
        raise PermissionError("VM-only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    for call in (lambda: H.learn(fixture[1], fixture[3], str(fixture[0]), str(tmp_path / "another")),
                 lambda: H.expected(c, fixture[2][0]),
                 lambda: R.verify(c, cases(fixture), str(fixture[0]), str(tmp_path / "proof")),
                 lambda: R.replay(c, {}, fixture[2][3], str(fixture[0]), str(tmp_path / "replay"))):
        with pytest.raises(PermissionError, match="VM-only"):
            call()
    assert not (tmp_path / "proof").exists()


def test_canvas_position_learner_requires_every_square_and_full_document_identity(fixture, tmp_path):
    import json
    from anyuse.execution.rea_formats import fen_labels
    app, source, _, _ = fixture
    labels = {p: p for p in "PNBRQKpnbrqk"}
    labels.update(empty="{square}", occupied="{piece}, {square}")
    profile = {"format": H.PROFILE, "id": "grid_position", "label": "Load a saved position",
        "effect": "load_state", "item_kind": "document", "params": [{"name": "path", "type": "path", "source": "path"}],
        "evidence": source["evidence"], "reference": {"window_path_param": "path", "fields": {
            "document": {"kind": "file_identity", "param": "path"},
            "board": {"kind": "fen_plist", "param": "path", "key": "Position", "labels": labels}}}}
    demos = []
    for i, placement in enumerate(("4KR2", "4K1R1", "4K2R")):
        p = tmp_path / f"position-{i}.game"
        p.write_bytes(plistlib.dumps({"Position": f"4k3/8/8/8/8/8/8/{placement} w - - 0 1"}))
        wanted = json.loads(fen_labels(p.read_bytes(), "Position", labels))
        rows = [{"parent": None, "role": "AXWindow", "AXDocument": p.as_uri()},
                {"parent": 0, "role": "AXGroup"}]
        rows += [{"parent": 1, "role": "AXButton", "AXTitle": label} for label in wanted]
        rows.append({"parent": 0, "role": "AXGroup"})  # unrelated window controls
        demos.append({"inputs": {"path": str(p)}, "invocation": {"kind": "open_document", "path": str(p)}, "after": rows})
    c = H.learn(profile, demos, str(app), str(tmp_path / "grid-learn"))
    assert c["selectors"]["board"]["grid"] == {"child_role": "AXButton", "count": 64}
    correct = H.expected(c, demos[0]["inputs"])[0]
    assert H.observe(c, demos[0]["after"], demos[0]["inputs"]) == correct
    ambiguous = copy.deepcopy(demos[0]["after"])
    other_root = len(ambiguous)
    ambiguous.append({"parent": 0, "role": "AXGroup"})
    ambiguous.extend(dict(row, parent=other_root) for row in demos[0]["after"] if row.get("parent") == 1)
    with pytest.raises(ValueError, match="ambiguous"):
        H.observe(c, ambiguous, demos[0]["inputs"])
    for mutation in ("missing", "extra", "duplicate", "wrong_piece", "wrong_path"):
        rows = copy.deepcopy(demos[0]["after"])
        if mutation == "missing": rows.pop(2)
        elif mutation == "extra": rows.append({"parent": 1, "role": "AXButton", "AXTitle": "extra cell"})
        elif mutation == "duplicate": rows[2]["AXTitle"] = rows[3]["AXTitle"]
        elif mutation == "wrong_piece": rows[2]["AXTitle"] = "wrong piece, a1"
        else: rows[0]["AXDocument"] = (tmp_path / "same-name-wrong-folder.game").as_uri()
        assert H.observe(c, rows, demos[0]["inputs"]) != correct
    wrong = copy.deepcopy(c)
    wrong["selectors"]["board"]["grid"]["count"] = 63
    with pytest.raises(ValueError, match="64 visible"):
        H.validate(wrong)
    # The interpreter learns other cell/root roles from data, without a Chess branch.
    for demo in demos:
        demo["after"][1]["role"] = "AXLayoutArea"
        for row in demo["after"][2:]:
            if row.get("parent") == 1: row["role"] = "AXImage"
    other = H.learn(profile, demos, str(app), str(tmp_path / "other-grid-learn"))
    assert other["selectors"]["board"]["where"]["role"] == "AXLayoutArea"
    assert other["selectors"]["board"]["grid"]["child_role"] == "AXImage"


@pytest.mark.parametrize("fen", [
    "8/8/8/8/8/8/8/8 w - - 0 1", "4k3/8/8/8/8/8/8/4K3 w - - 0",
    "4k3/8/8/8/8/8/8/4K4 w - - 0 1", "4k3/8/8/8/8/8/8/4K3 x - - 0 1",
    "4k3/8/8/8/8/8/8/4X3 w - - 0 1", "4k3/8/8/8/8/8/8/4K3 w - z9 0 1",
])
def test_position_reference_refuses_invalid_or_incomplete_documents(fen):
    from anyuse.execution.rea_formats import fen_labels
    labels = {p: p for p in "PNBRQKpnbrqk"}
    labels.update(empty="{square}", occupied="{piece}, {square}")
    with pytest.raises(ValueError):
        fen_labels(plistlib.dumps({"Position": fen}), "Position", labels)


@pytest.fixture
def svg_fixture(fixture, tmp_path, monkeypatch, request):
    """Independent synthetic oracle/actor/DOM evidence; never an app execution."""
    from types import SimpleNamespace
    from xml.sax.saxutils import escape
    from anyuse.execution import rea_inspector_bridge as I
    from anyuse.execution import rea_svg_observer as V

    app, base, _, _ = fixture
    mixed = getattr(request, "param", "strict") == "mixed"
    observer_kind = V.MIXED_KIND if mixed else V.KIND
    observer_mode = V.MIXED_MODE if mixed else "dom_svg_elements"
    actor = {"kind": "inspector_method", "receiver": {"root": {"kind": "global", "name": "canvasOwner"}, "steps": []},
             "method": "loadDiagram", "function_sha256": hashlib.sha256(b"reviewed synthetic load operation").hexdigest(),
             "arguments": [{"type": "utf8", "value": {"input": "payload"}}], "return_type": "void"}
    contract = (tmp_path / "svg-actor-declaration.json").resolve()
    R.write_json(contract, {"operation": I.call_contract(actor)})
    actor["signature_evidence"] = {"kind": "reviewed_js", "file": str(contract),
                                   "sha256": R._file_hash(contract), "pointer": "/operation"}
    config = (tmp_path / "svg-operator.json").resolve()
    R.write_json(config, {"format": I.TRANSPORT, "endpoint": "http://127.0.0.1:9246",
                         "relative_url": "Contents/Resources/diagram.html", "target": R.fingerprint(app)})
    config.chmod(0o600)
    monkeypatch.setenv("ANYUSE_REA_INSPECTOR_CONFIG", str(config))
    profile = {"format": H.PROFILE, "id": "mapped_canvas", "label": "Load complete diagram labels",
               "effect": "load_state", "item_kind": "document",
               "params": [{"name": "payload", "type": "string", "source": "text"},
                          {"name": "oracle", "type": "path", "source": "path"},
                          {"name": "observer_contract", "type": "path", "source": "path"}],
               "reference": {"fields": {"labels": {"kind": H.SVG_REFERENCE, "param": "oracle"}}},
               "observer": {"kind": observer_kind, "selector": "div.canvas > svg", "limits": copy.deepcopy(V.LIMITS),
                            "namespace_mode": observer_mode, "contract_param": "observer_contract",
                            "contract_pointer": "/observer"}, "evidence": copy.deepcopy(base["evidence"])}
    authored = [[f"Case {i}", "", "duplicate", "duplicate", " Ａe\u0301\n東京 &<> "] for i in range(11)]
    authored[9] = []
    authored[10] = ["", "", "e\u0301", "é", "a b", "ab"]
    inputs, outputs = [], {}
    for i, values in enumerate(authored):
        oracle = (tmp_path / f"svg-oracle-{i}.json").resolve()
        declaration = (tmp_path / f"svg-observer-{i}.json").resolve()
        R.write_json(oracle, values)
        schema = {"format": V.SCHEMA, "kind": observer_kind, "target": R.fingerprint(app),
                  "selector": profile["observer"]["selector"], "limits": copy.deepcopy(V.LIMITS),
                  "namespace_mode": observer_mode,
                  "reference": {"format": "json_string_list", "file": str(oracle), "sha256": R._file_hash(oracle)},
                  "contract": {"file": str(declaration), "sha256": "0" * 64, "pointer": "/observer"}}
        R.write_json(declaration, {"observer": V.declaration(schema)})
        row = {"payload": f"independent-source-{i}", "oracle": str(oracle), "observer_contract": str(declaration)}
        inputs.append(row)
        # The simulated actor uses this preauthored table, never the oracle file.
        outputs[row["payload"]] = copy.deepcopy(values)

    def runtime_receipt(c, row):
        receipt = inspector_receipt(c, H.bind(c, row), bundle=str(app.resolve()))
        endpoint = receipt["endpoint_identity"]
        endpoint["frame"] = {"id": endpoint["execution_context"]["frame_id"], "url": endpoint["target"]["url"]}
        return receipt

    class SVGReader:
        sent = []
        reads = []
        initial = ["Independent initial canvas"]
        wrong = False
        delayed_reads = 0
        tamper = None
        mutate_on_send = None
        clock = 0.0

        def __init__(self, bundle, c):
            self.bundle = Path(bundle)
            self.native = SimpleNamespace(pid=443)
            self.state = copy.deepcopy(type(self).initial)
            self.dispatched = False
            self.remaining_delayed = type(self).delayed_reads

        def read(self):
            pytest.fail("SVG proof must not fall back to AX")

        def send(self, c, row, timeout):
            cls = type(self)
            cls.sent.append(copy.deepcopy(row))
            self.dispatched = True
            self.state = ["Wrong complete canvas"] if cls.wrong else copy.deepcopy(outputs[row["payload"]])
            if cls.mutate_on_send is not None:
                cls.mutate_on_send(row)
            receipt = runtime_receipt(c, row)
            return {"returncode": 0, "timed_out": False, "permission": "inspector_connected", "pid": 443,
                    "uid": os.getuid(), "bundle": str(self.bundle.resolve()), "primitive": "inspector_method", "runtime": receipt}

        def read_svg(self, c, row, out, timeout):
            cls = type(self)
            cls.clock += timeout + 1 if cls.wrong and self.dispatched else .1
            values = copy.deepcopy(self.state)
            if self.dispatched and self.remaining_delayed:
                self.remaining_delayed -= 1
                values = ["Complete but not yet updated"]
            cls.reads.append({"inputs": copy.deepcopy(row), "after": self.dispatched, "values": copy.deepcopy(values)})
            correct, source = H._svg_case(c, row)
            schema = source["schema"]
            pins = H.resources(c)["svg_observer"]
            original = runtime_receipt(c, row)
            endpoint = original["endpoint_identity"]
            markup = '<svg xmlns="http://www.w3.org/2000/svg"><g>' + ''.join(
                "<text>" + escape(value) + "</text>" for value in values) + "</g></svg>"
            root = {"tag": "svg", "is_svg": True, "attribute_count": 1, "attributes_sha256": R.digest(["xmlns", V.SVG_NS]),
                    "child_count": 1, "identity_sha256": R.digest({"node_id": 20, "backend_node_id": 120})}
            rows = [{"tag": "svg", "depth": 1, "element_children": 1, "attributes_sha256": R.digest([])},
                    {"tag": "g", "depth": 2, "element_children": len(values), "attributes_sha256": R.digest([])}]
            rows += [{"tag": "text", "depth": 3, "element_children": 0, "attributes_sha256": R.digest([])} for _ in values]
            context = {"format": V.DOM_CONTEXT, "namespace": V.SVG_NS, "source": "DOM.describeNode",
                       "root_identity_sha256": root["identity_sha256"], "elements": rows, "elements_sha256": R.digest(rows),
                       "element_count": len(rows), "dom_node_count": len(rows) + sum(bool(value) for value in values),
                       "metadata_tree_sha256": R.digest({"tree": rows, "text": values}), "complete": True,
                       "element_coverage_only": True, "metadata_whitespace_mode": "all_requested", "whitespace_source": "DOM.getOuterHTML"}
            if schema["kind"] == V.MIXED_KIND:
                serial = [120]
                def native_element(tag, children, *, svg=True):
                    serial[0] += 1
                    return {"nodeType": 1, "nodeId": serial[0] - 100, "backendNodeId": serial[0],
                            "nodeName": tag if svg else tag.upper(), "localName": tag, "isSVG": svg,
                            "nodeValue": "", "attributes": [], "children": children, "childNodeCount": len(children)}
                def native_text(value):
                    serial[0] += 1
                    return {"nodeType": 3, "nodeId": serial[0] - 100, "backendNodeId": serial[0],
                            "nodeName": "#text", "localName": "", "nodeValue": value, "childNodeCount": 0}
                labels = []
                for i, value in enumerate(values):
                    content = [native_text(value)] if value else []
                    labels.append(native_element("text", [native_element("tspan", content)]) if i % 2 == 0 else
                                  native_element("foreignObject", [native_element("div", [native_element("span", content, svg=False)], svg=False)]))
                native = {"nodeType": 1, "nodeId": 20, "backendNodeId": 120, "nodeName": "svg", "localName": "svg",
                          "nodeValue": "", "isSVG": True, "attributes": ["xmlns", V.SVG_NS],
                          "childNodeCount": 1, "children": [native_element("g", labels)]}
                markup = '<svg xmlns="http://www.w3.org/2000/svg"><g>' + ''.join(
                    "<text><tspan>" + escape(value) + "</tspan></text>" if i % 2 == 0 else
                    "<foreignObject><div><span>" + escape(value) + "</span></div></foreignObject>"
                    for i, value in enumerate(values)) + "</g></svg>"
                witness = V._mixed_witness(native, 20, root, schema["limits"])
                capture = V._mixed_capture(markup, witness, copy.deepcopy(witness), schema["limits"])
            else:
                capture = V.parse_svg(markup)
                capture.update(namespace_mode="dom_svg_elements", namespace_context=context)
            expected = next(iter(correct.values()))
            observation = {"format": V.OBSERVATION, "kind": schema["kind"], "target": copy.deepcopy(c["target"]),
                           "schema_sha256": R.digest(schema), "request_sha256": R.digest(V._request(schema)),
                           "resources": {"reference": source["files"]["oracle"], "declaration": source["files"]["observer_contract"],
                                         "adapter": pins["adapter"], "transport": pins["transport"]},
                           "expected": expected, "values": values, "complete": True, "matches": values == expected,
                           "status": "observed" if values == expected else "refuted", "evidence_only": True,
                           "identity": original["identity"], "after_identity": original["after_identity"],
                           "endpoint_identity": {"listener": endpoint["listener"], "renderer_processes": endpoint["renderer_processes"],
                               "target_type": endpoint["target"]["type"], "target_url": endpoint["target"]["url"],
                               "target_identity_sha256": R.digest(endpoint["target"]), "frame_sha256": R.digest(endpoint["frame"])},
                           "root_before": root, "root_after": copy.deepcopy(root), "capture": capture,
                           "cleanup": {"page_socket_closed": True, "browser_socket_closed": True,
                                       "requested_reads_completed": True, "local_live_ids_discarded": True}}
            if schema["kind"] == V.MIXED_KIND:
                observation.update(mixed_witness_stable=True, mixed_witness_after_sha256=R.digest(capture["witness"]))
            else:
                observation.update(namespace_context_stable=True, namespace_context_after_sha256=R.digest(context))
            observation.update({name: False for name in ("state_uncertain", "resource_review_required", "retry", "proof_created",
                "task_result_verified", "visibility_proved", "geometry_proved", "connectivity_proved", "persistence_proved", "actuated")})
            observation.update({name: 0 for name in ("model_calls", "app_evaluations", "method_calls", "getters_invoked")})
            if cls.tamper is not None:
                cls.tamper(observation, self.dispatched)
            Path(out).mkdir(parents=True)
            R.write_json(Path(out) / "observation.json", observation)
            return observation

    monkeypatch.setattr(H, "Runtime", SVGReader)
    monkeypatch.setattr(H.time, "monotonic", lambda: SVGReader.clock)
    return {"app": app, "profile": profile, "actor": actor, "inputs": inputs,
            "outputs": outputs, "runtime": SVGReader, "receipt": runtime_receipt, "config": config}


def svg_recordings(data, tmp_path):
    profile = data["profile"]
    partial = {**profile, "actor": data["actor"]}
    return [H.record(profile, H.bind(partial, row), row, str(data["app"]), str(tmp_path / f"svg-record-{i}"), timeout=5)
            for i, row in enumerate(data["inputs"][:3])]


def svg_candidate(data, tmp_path):
    return H.learn(data["profile"], svg_recordings(data, tmp_path), str(data["app"]), str(tmp_path / "svg-learn"))


def svg_proof(data, tmp_path):
    candidate = svg_candidate(data, tmp_path)
    proof = R.verify(candidate, [{"inputs": row} for row in data["inputs"][3:6]], str(data["app"]),
                     str(tmp_path / "svg-verify"), timeout=5)
    return candidate, proof


@pytest.mark.parametrize("svg_fixture", ["strict", "mixed"], indirect=True)
def test_svg_full_mapping_preserves_order_empty_duplicates_and_exact_unicode(svg_fixture, tmp_path):
    data = svg_fixture
    candidate, proof = svg_proof(data, tmp_path)
    assert candidate["backend"] == H.SVG_BACKEND and "selectors" not in candidate
    assert candidate["actor"]["arguments"][0]["value"] == {"input": "payload"}
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    assert len(data["runtime"].sent) == 6
    for i, row in enumerate(data["inputs"][6:]):
        result = R.replay(candidate, proof, row, str(data["app"]), str(tmp_path / f"svg-replay-{i}"), timeout=5)
        assert result["verdict"] == "verified" and result["model_calls"] == 0
        assert result["seen"] == {"labels": data["outputs"][row["payload"]]}
        assert result["run"]["runtime"]["task_result_verified"] is False
        assert result["after_observation"]["task_result_verified"] is False
    assert len(data["runtime"].sent) == 11
    assert proof["cases"][0]["seen"]["labels"][-1] == " Ａe\u0301\n東京 &<> "
    assert proof["cases"][0]["seen"]["labels"][1:4] == ["", "duplicate", "duplicate"]
    if candidate["observer"]["kind"] == "svg_mixed_label_text":
        assert proof["cases"][0]["after_observation"]["capture"]["unit_kinds"] == [
            "svg_text", "svg_foreign_object", "svg_text", "svg_foreign_object", "svg_text"]


@pytest.mark.parametrize("mutation", ["normalization", "mutable_oracle", "mutable_contract", "mixed_ax", "ax_backend",
                                     "proof_argument", "mode_without_kind", "kind_without_mode", "output_selector", "observer_removed"])
def test_svg_closed_profile_rejects_weakening_before_actor(svg_fixture, tmp_path, mutation):
    data = svg_fixture
    profile, actor = copy.deepcopy(data["profile"]), copy.deepcopy(data["actor"])
    if mutation == "normalization":
        profile["reference"]["fields"]["labels"]["normalization"] = "compact"
    elif mutation == "mutable_oracle":
        profile["params"][1]["mutable"] = True
    elif mutation == "mutable_contract":
        profile["params"][2]["mutable"] = True
    elif mutation == "mixed_ax":
        profile["reference"]["fields"]["echo"] = {"kind": "input", "param": "payload"}
    elif mutation == "ax_backend":
        profile["backend"] = H.BACKEND
    elif mutation == "proof_argument":
        actor["arguments"][0]["value"] = {"input": "oracle"}
    elif mutation == "mode_without_kind":
        profile["observer"]["namespace_mode"] = "dom_svg_html_elements"
    elif mutation == "kind_without_mode":
        profile["observer"]["kind"] = "svg_mixed_label_text"
    elif mutation == "output_selector":
        profile["observer"]["selector"] = "svg:has-text('Case 0')"
    else:
        profile.pop("observer")
    with pytest.raises(ValueError):
        H._reference(profile["reference"], H._params(profile["params"]), profile["params"])
        H._svg_template({**profile, "actor": actor})
    assert not data["runtime"].sent


@pytest.mark.parametrize("mutation", ["declaration", "not_list", "non_string", "oracle_contract_alias"])
def test_svg_case_files_are_checked_before_record_dispatch(svg_fixture, tmp_path, mutation):
    data = svg_fixture
    row = copy.deepcopy(data["inputs"][0])
    if mutation == "declaration":
        contract = R.load_json(row["observer_contract"])
        contract["observer"]["request"]["selector"] = "svg.other"
        R.write_json(Path(row["observer_contract"]), contract)
    elif mutation in {"not_list", "non_string"}:
        R.write_json(Path(row["oracle"]), {"labels": []} if mutation == "not_list" else [False])
    else:
        row["observer_contract"] = row["oracle"]
    with pytest.raises(ValueError):
        H.record(data["profile"], H.bind({**data["profile"], "actor": data["actor"]}, row), row,
                 str(data["app"]), str(tmp_path / "svg-invalid-record"), timeout=5)
    assert not data["runtime"].sent


def test_svg_no_op_recording_stops_before_method_and_replay_remains_idempotent(svg_fixture, tmp_path):
    data = svg_fixture
    row = data["inputs"][0]
    data["runtime"].initial = copy.deepcopy(data["outputs"][row["payload"]])
    with pytest.raises(ValueError, match="no-op"):
        H.record(data["profile"], H.bind({**data["profile"], "actor": data["actor"]}, row), row,
                 str(data["app"]), str(tmp_path / "svg-noop"), timeout=5)
    assert not data["runtime"].sent
    data["runtime"].initial = ["Independent initial canvas"]
    candidate, proof = svg_proof(data, tmp_path)
    reuse = data["inputs"][9]
    data["runtime"].initial = copy.deepcopy(data["outputs"][reuse["payload"]])
    result = R.replay(candidate, proof, reuse, str(data["app"]), str(tmp_path / "svg-idempotent"), timeout=5)
    assert result["verdict"] == "verified" and result["before"] == result["expected"] == {"labels": []}
    assert len(data["runtime"].sent) == 7


@pytest.mark.parametrize("fault", ["wrong", "incomplete", "cleanup", "frame", "pid", "oracle_changed", "contract_changed"])
def test_svg_failure_stops_after_one_actor_without_fallback(svg_fixture, tmp_path, fault):
    data = svg_fixture
    candidate = svg_candidate(data, tmp_path)
    sent = len(data["runtime"].sent)
    if fault == "wrong":
        data["runtime"].wrong = True
    elif fault.endswith("changed"):
        key = "oracle" if fault == "oracle_changed" else "observer_contract"
        data["runtime"].mutate_on_send = lambda row: Path(row[key]).write_text("[]")
    else:
        def tamper(observation, after):
            if not after:
                return
            if fault == "incomplete":
                observation["complete"] = False
            elif fault == "cleanup":
                observation["resource_review_required"] = True
                observation["cleanup"]["page_socket_closed"] = False
            elif fault == "frame":
                observation["endpoint_identity"]["frame_sha256"] = "f" * 64
            else:
                observation["identity"]["pid"] += 8
                observation["after_identity"]["pid"] += 8
        data["runtime"].tamper = tamper
    with pytest.raises(ValueError):
        R.verify(candidate, [{"inputs": row} for row in data["inputs"][3:6]], str(data["app"]),
                 str(tmp_path / "svg-failed-check"), timeout=5)
    assert len(data["runtime"].sent) == sent + 1
    receipt = R.load_json(tmp_path / "svg-failed-check/proof.json")
    assert receipt["status"] == "incomplete" and not R.proof_status(candidate, receipt, candidate["target"])[0]
    if fault == "wrong":
        assert receipt["cases"][0]["verdict"] == "refuted"
    if fault in {"incomplete", "cleanup", "frame", "pid"}:
        assert len([read for read in data["runtime"].reads[6:] if read["after"]]) == 1


def test_svg_clean_read_polling_never_repeats_the_actor(svg_fixture, tmp_path):
    data = svg_fixture
    candidate = svg_candidate(data, tmp_path)
    data["runtime"].delayed_reads = 1
    proof = R.verify(candidate, [{"inputs": row} for row in data["inputs"][3:6]], str(data["app"]),
                     str(tmp_path / "svg-polled-check"), timeout=5)
    assert proof["cases"][0]["read_only_polls"] == 1
    assert len(data["runtime"].sent) == 6


@pytest.mark.parametrize("fault", ["schema", "reference_hash", "frame", "pid", "transport", "topology", "capture_values", "observation_digest", "cleanup", "status", "before_hash"])
def test_svg_proof_rechecks_full_independent_envelopes_not_matches_flag(svg_fixture, tmp_path, fault):
    data = svg_fixture
    candidate, original = svg_proof(data, tmp_path)
    proof = copy.deepcopy(original)
    row = proof["cases"][0]
    after = row["after_observation"]
    if fault == "schema":
        row["reference"]["sources"]["schema"]["selector"] = "svg.changed"
    elif fault == "reference_hash":
        row["reference"]["sources"]["files"]["oracle"]["sha256"] = "e" * 64
    elif fault == "frame":
        after["endpoint_identity"]["frame_sha256"] = "e" * 64
    elif fault == "pid":
        after["identity"]["pid"] += 1
        after["after_identity"]["pid"] += 1
    elif fault == "transport":
        after["resources"]["transport"]["endpoint"] = "http://127.0.0.1:9247"
    elif fault == "topology":
        context = after["capture"]["namespace_context"]
        context["elements"][1]["element_children"] += 1
        context["elements_sha256"] = R.digest(context["elements"])
        after["namespace_context_after_sha256"] = R.digest(context)
    elif fault == "capture_values":
        after["capture"]["values"] = ["forged"]
        after["capture"]["values_sha256"] = R.digest(["forged"])
    elif fault == "observation_digest":
        row["after_observation_sha256"] = "e" * 64
    elif fault == "cleanup":
        after["cleanup"]["requested_reads_completed"] = False
    elif fault == "status":
        after["task_result_verified"] = True
    else:
        row["before_state_sha256"] = "e" * 64
    if fault != "observation_digest":
        row["after_observation_sha256"] = R.digest(after)
    row["reference_sha256"] = R.digest(row["reference"])
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    assert R.proof_status(candidate, original, candidate["target"])[0]


def test_svg_retained_case_oracle_or_declaration_drift_invalidates_proof(svg_fixture, tmp_path):
    data = svg_fixture
    candidate, proof = svg_proof(data, tmp_path)
    path = Path(data["inputs"][3]["observer_contract"])
    old = path.read_bytes()
    path.write_bytes(old + b" ")
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    path.write_bytes(old)
    assert R.proof_status(candidate, proof, candidate["target"])[0]


def test_svg_semantic_novelty_excludes_new_oracle_and_declaration_names(svg_fixture, tmp_path):
    data = svg_fixture
    candidate = svg_candidate(data, tmp_path)
    checking = copy.deepcopy(data["inputs"][3:6])
    checking[0]["payload"] = data["inputs"][0]["payload"]
    with pytest.raises(ValueError, match="substantive"):
        R.verify(candidate, [{"inputs": row} for row in checking], str(data["app"]),
                 str(tmp_path / "svg-renamed-proof-files"), timeout=5)
    assert len(data["runtime"].sent) == 3
    demos = [R.load_json(tmp_path / f"svg-record-{i}/recording.json") for i in range(3)]
    heldout = [{"inputs": row} for row in data["inputs"][6:9]]
    heldout[0]["inputs"] = {**heldout[0]["inputs"], "payload": data["inputs"][3]["payload"]}
    with pytest.raises(ValueError, match="held-out"):
        H.run(data["profile"], demos, [{"inputs": row} for row in data["inputs"][3:6]], heldout,
              str(data["app"]), str(tmp_path / "svg-overlap"), timeout=5)
    assert not (tmp_path / "svg-overlap").exists()


def test_svg_observation_receipt_alone_cannot_replace_before_after_actor_proof(svg_fixture, tmp_path):
    data = svg_fixture
    demos = svg_recordings(data, tmp_path)
    for demo in demos:
        demo["after"] = demo["after"]["after_observation"]
    with pytest.raises(ValueError, match="independent"):
        H.learn(data["profile"], demos, str(data["app"]), str(tmp_path / "svg-observation-only"))
    assert not (tmp_path / "svg-observation-only").exists()


@pytest.mark.parametrize("svg_fixture", ["mixed"], indirect=True)
@pytest.mark.parametrize("fault", ["copied_values", "witness_text", "witness_root", "witness_stability", "old_kind"])
def test_mixed_svg_proof_recomputes_complete_typed_witness_and_preserves_distinct_kind(svg_fixture, tmp_path, fault):
    data = svg_fixture
    candidate, original = svg_proof(data, tmp_path)
    proof = copy.deepcopy(original)
    row = proof["cases"][0]
    after, capture = row["after_observation"], row["after_observation"]["capture"]
    if fault == "copied_values":
        capture["values"] = ["forged actor result"]
        capture["values_sha256"] = R.digest(capture["values"])
    elif fault == "witness_text":
        witness = capture["witness"]
        next(node for node in witness["nodes"] if node["node_type"] == 3)["value"] = "different independently observed text"
        witness["nodes_sha256"] = R.digest(witness["nodes"])
        capture["witness_after_sha256"] = R.digest(witness)
        after["mixed_witness_after_sha256"] = R.digest(witness)
    elif fault == "witness_root":
        witness = capture["witness"]
        witness["root_identity_sha256"] = "e" * 64
        capture["witness_after_sha256"] = R.digest(witness)
        after["mixed_witness_after_sha256"] = R.digest(witness)
    elif fault == "witness_stability":
        after["mixed_witness_stable"] = False
    else:
        after["kind"] = "svg_text_elements"
    row["after_observation_sha256"] = R.digest(after)
    assert not R.proof_status(candidate, proof, candidate["target"])[0]
    assert R.proof_status(candidate, original, candidate["target"])[0]


@pytest.fixture
def svg_gui_context(svg_fixture, monkeypatch):
    """Native-module fakes for no-launch control flow, not a live GUI proof."""
    from types import SimpleNamespace
    from anyuse.execution import rea_runtime_bridge as B
    data = svg_fixture
    c = {**data["profile"], "actor": data["actor"], "target": R.fingerprint(data["app"])}
    row = data["inputs"][0]
    control = SimpleNamespace(
        identity=data["receipt"](c, row)["identity"], trusted=True, missing=False, calls=[], forbidden=[],
        session={"on-console": True, "kCGSessionLoginDoneKey": True, "console-uid": os.getuid(), "CGSSessionScreenIsLocked": False})
    monkeypatch.setitem(sys.modules, "ApplicationServices", SimpleNamespace(AXIsProcessTrusted=lambda: control.trusted))
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(
        kCGSessionOnConsoleKey="on-console", kCGSessionUserIDKey="console-uid",
        CGSessionCopyCurrentDictionary=lambda: copy.deepcopy(control.session)))
    def process(bundle, bundle_id):
        control.calls.append((str(bundle), bundle_id))
        if control.missing:
            raise B.Unavailable("no existing target")
        return copy.deepcopy(control.identity)
    def forbidden(*args, **kwargs):
        control.forbidden.append((args, kwargs))
        raise AssertionError("SVG context attempted launch/activation/AppleEvent initialization")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=forbidden),
        NSApplication=SimpleNamespace(sharedApplication=forbidden),
        NSRunningApplication=SimpleNamespace(runningApplicationsWithBundleIdentifier_=forbidden)))
    monkeypatch.setattr(B, "process_identity", process)
    monkeypatch.setattr(H.AE, "Runtime", forbidden)
    monkeypatch.setattr(H.subprocess, "run", forbidden)
    monkeypatch.setattr(H.AE.C, "CDLL", forbidden)
    return data, c, row, control


@pytest.mark.parametrize("backend", [None, H.SVG_BACKEND])
def test_svg_runtime_uses_existing_process_without_ax_ae_launch_or_activation(svg_gui_context, tmp_path, monkeypatch, backend):
    from anyuse.execution import rea_inspector_bridge as I
    from anyuse.execution import rea_svg_observer as V
    data, c, row, control = svg_gui_context
    if backend is not None:
        c["backend"] = backend
    calls = []
    monkeypatch.setattr(V, "collect", lambda *args, **kwargs: calls.append(("read", args, kwargs)) or {"evidence_only": True})
    def invoke(bundle, actor, out, **kwargs):
        calls.append(("invoke", bundle, copy.deepcopy(actor)))
        assert actor == H.bind(c, row)
        assert kwargs["expected_identity"] == control.identity
        return {**data["receipt"](c, row), "expected_identity": copy.deepcopy(kwargs["expected_identity"])}
    monkeypatch.setattr(I, "invoke", invoke)
    runtime = _H_RUNTIME(data["app"].resolve(), c)
    assert isinstance(runtime.native, H._SVGGUIContext) and runtime.native.pid == control.identity["pid"]
    assert not any(hasattr(runtime.native, name) for name in ("app", "lib", "read", "send_invocation"))
    initial = len(control.calls)
    assert runtime.read_svg(c, row, tmp_path / "direct-svg-evidence", 5) == {"evidence_only": True}
    runtime.receipt_dir = tmp_path / "direct-inspector"
    assert runtime.send(c, row, 5)["returncode"] == 0
    assert len(control.calls) == initial + 2 and [call[0] for call in calls] == ["read", "invoke"]
    with pytest.raises(ValueError, match="no AX"):
        runtime.read()
    changed = {**c, "actor": {"kind": "open_document", "path": {"input": "oracle"}}}
    with pytest.raises(ValueError, match="no opening"):
        runtime.send(changed, row, 5)
    assert len(calls) == 2 and not control.forbidden


@pytest.mark.parametrize("operation", ["read", "send"])
@pytest.mark.parametrize("fault", ["missing", "pid", "launch_time", "bundle", "bundle_id", "uid", "executable", "locked", "other_console", "off_console", "not_logged_in", "untrusted"])
def test_svg_runtime_refuses_changed_or_unready_context_before_any_collector_or_actor(svg_gui_context, tmp_path, monkeypatch, operation, fault):
    from anyuse.execution import rea_inspector_bridge as I
    from anyuse.execution import rea_svg_observer as V
    data, c, row, control = svg_gui_context
    runtime = _H_RUNTIME(data["app"].resolve(), c)
    runtime.receipt_dir = tmp_path / "never-inspector"
    monkeypatch.setattr(V, "collect", lambda *a, **k: pytest.fail("unready SVG context reached DOM collector"))
    monkeypatch.setattr(I, "invoke", lambda *a, **k: pytest.fail("unready SVG context reached private actor"))
    if fault == "missing":
        control.missing = True
    elif fault == "pid":
        control.identity["pid"] += 1
    elif fault == "launch_time":
        control.identity["launch_time"] += 1
    elif fault == "bundle":
        control.identity["bundle"] = str(tmp_path / "Another.app")
    elif fault == "bundle_id":
        control.identity["bundle_id"] = "org.test.Other"
    elif fault == "uid":
        control.identity["uid"] += 1
    elif fault == "executable":
        control.identity["executable"] = str(tmp_path / "outside-app")
    elif fault == "locked":
        control.session["CGSSessionScreenIsLocked"] = True
    elif fault == "other_console":
        control.session["console-uid"] += 1
    elif fault == "off_console":
        control.session["on-console"] = False
    elif fault == "not_logged_in":
        control.session["kCGSessionLoginDoneKey"] = False
    else:
        control.trusted = False
    with pytest.raises((ValueError, PermissionError)):
        if operation == "read":
            runtime.read_svg(c, row, tmp_path / "never-svg-evidence", 5)
        else:
            runtime.send(c, row, 5)
    assert not control.forbidden and not (tmp_path / "never-svg-evidence").exists()
    assert not runtime.receipt_dir.exists()


def test_svg_gui_context_rejects_absent_process_at_construction_without_auto_open(svg_gui_context):
    data, c, _, control = svg_gui_context
    control.missing = True
    with pytest.raises(ValueError, match="no existing target"):
        _H_RUNTIME(data["app"].resolve(), c)
    assert not control.forbidden and len(control.calls) == 1


def test_svg_runtime_guest_guard_precedes_native_imports_and_identity_io(svg_gui_context, monkeypatch):
    import builtins
    data, c, _, control = svg_gui_context
    native_imports = []
    original_import = builtins.__import__
    def imported(name, *args, **kwargs):
        if name in {"ApplicationServices", "Quartz", "AppKit"}:
            native_imports.append(name)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", imported)
    def deny():
        raise PermissionError("VM-only before native imports")
    monkeypatch.setattr(R, "assert_guest", deny)
    with pytest.raises(PermissionError, match="VM-only"):
        _H_RUNTIME(data["app"], c)
    assert not native_imports and not control.calls and not control.forbidden


def test_default_ax_runtime_preserves_legacy_constructor_and_read_path(fixture, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from anyuse.execution import rea_runtime_bridge as B
    candidate = learn(fixture, tmp_path)
    calls = []
    native = SimpleNamespace(pid=443, read=lambda **kwargs: calls.append(("read", kwargs)) or ["AX evidence"])
    monkeypatch.setattr(H.AE, "Runtime", lambda bundle, **kwargs: calls.append(("constructor", bundle, kwargs)) or native)
    monkeypatch.setattr(B, "process_identity", lambda *a: pytest.fail("nonnative context requested a native process pin"))
    monkeypatch.setattr(H, "_SVGGUIContext", lambda *a, **k: pytest.fail("default AX entered SVG context"))
    runtime = _H_RUNTIME(fixture[0], candidate)
    assert runtime.native is native and runtime.read() == ["AX evidence"]
    assert calls == [("constructor", fixture[0], {"bundle_id": candidate["target"]["bundle_id"]}), ("read", {"all_windows": True})]


def _qt_moc_retained_row(actor, *, pinned=None):
    """Inert complete receipt data; no actual Qt call or body-entry evidence."""
    from anyuse.execution import rea_qt_moc as Q
    from test_rea_native_path import _observations
    recipe, selection = actor["receiver"]["native_path"], actor["selection"]
    consumer = Q.consumer_contract(recipe["consumer"])
    image = {"cpu_type": 0x0100000c, "cpu_subtype": 0, "uuid": "a" * 32}
    section = {"segment": "__TEXT", "name": "__text", "offset": selection["dispatcher_offset"], "bytes": 256, "flags": 0x80000400}
    extent = int(selection["dispatcher_offset"], 16) + 256
    if pinned is not None:
        module = pinned[selection["module"]["path"]]
        image = copy.deepcopy(module["macho_images"][0])
        section = copy.deepcopy(next(section for group in module["macho_sections"] for section in group["sections"]
            if int(section["offset"], 16) <= int(selection["dispatcher_offset"], 16) < int(section["offset"], 16) + section["bytes"]))
        extent = max(int(segment["offset"], 16) + segment["bytes"]
            for group in module["macho_segments"] for segment in group["segments"])
    return {"index": 0, "op": "qt_moc_invoke", "status": "ok", "function": {
        "target": consumer["callable"]["entry"], "module": selection["module"]["path"], "image_identity": image,
        "instruction_section": section, "module_size": extent, "reported_module_size": extent,
        "executable": True, "symbol": None, "implementation_offset": selection["dispatcher_offset"],
        "abi": "default", "argument_types": ["pointer", "int32", "int32", "pointer"], "return_type": "void",
        "signature_evidence": copy.deepcopy(recipe["consumer"]["evidence"]["claim"]), "bridge": "frida-native"},
        "return_type": "void", "value": None, "out_buffers": [], "pointer_reads": [], "swift_field_reads": [],
        "callable_entries": [], "compiler_completion": None, "native_paths": _observations(recipe),
        "qt_moc": {"format": Q.OBSERVATION, "selection": copy.deepcopy(selection), "consumer": consumer,
            "call": 0, "local_index": selection["local_index"], "argv": {**consumer["argv"], "expired": True},
            "permit_consumed": True, "returned": True, "expires": "transaction"}}


@pytest.mark.parametrize("fault", [None, "native_op", "call_bool", "method_index", "vector_live", "non_null_slot",
                                    "unsealed", "wrong_dispatcher", "short_code", "wrong_image", "nonvoid_value"])
def test_retained_qt_method_row_is_bound_to_exact_callback_vector_method_and_seal(fault):
    from test_rea_native_path import _qt_moc_path_actor
    actor, recipe = _qt_moc_path_actor()
    row = _qt_moc_retained_row(actor)
    assert H._qt_moc_method_run(row, actor, 0)
    if fault is None: return
    if fault == "native_op": row["op"] = "native_invoke"
    elif fault == "call_bool": row["qt_moc"]["call"] = False
    elif fault == "method_index": row["qt_moc"]["local_index"] += 1
    elif fault == "vector_live": row["qt_moc"]["argv"]["expired"] = False
    elif fault == "non_null_slot": row["qt_moc"]["argv"]["slot0"] = "n1"
    elif fault == "unsealed": row["native_paths"][0]["sealed"] = False
    elif fault == "wrong_dispatcher": row["function"]["implementation_offset"] = "0x3004"
    elif fault == "short_code": row["function"]["instruction_section"]["bytes"] = 3
    elif fault == "wrong_image": row["function"]["image_identity"]["cpu_subtype"] = 2
    else: row["value"] = False
    assert not H._qt_moc_method_run(row, actor, 0)


def test_qt_learning_and_empty_parameter_route_are_explicitly_unimplemented():
    from test_rea_native_path import _qt_moc_path_actor
    actor, recipe = _qt_moc_path_actor()
    params = [{"name": "reference", "type": "string", "source": "text"}]
    assert H._runtime_actor(actor, params) == actor
    with pytest.raises(ValueError): H._runtime_actor(actor, [])
    demos = [{"invocation": copy.deepcopy(actor), "inputs": {"reference": "independent-" + str(index)}} for index in range(3)]
    with pytest.raises(ValueError, match="separate fixed-state trial gate"):
        H._infer_actor(demos, ["reference"])


@pytest.fixture
def qt_moc_bridge_native_files(qt_moc_bridge_runtime):
    # B's native-files factory requires B's five-part runtime, not this module's
    # unrelated H.Runtime Fake fixture. Keep both dependencies uniquely named.
    from test_rea_runtime_bridge import native_files as bridge_native_files
    return bridge_native_files.__wrapped__(qt_moc_bridge_runtime)


@pytest.fixture
def qt_moc_retained(qt_moc_bridge_native_files, monkeypatch):
    """Real original-file collectors plus inert modern retained evidence.

    This validates the retained runtime boundary only; no learned candidate,
    successful task output, replay, public card or actual Qt dispatch is made.
    """
    from anyuse.execution import rea_runtime_bridge as B
    from test_rea_qt_moc import _qt_moc_files, _qt_moc_result
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    actor, recipe, files = _qt_moc_files(qt_moc_bridge_native_files, monkeypatch)
    resources = B.native_resources(actor)
    result = _qt_moc_result(actor, resources=resources)
    identity = {"bundle_id": "org.test.retained-qt", "bundle": "/tmp/RetainedQt.app", "pid": 443,
        "launch_time": 1000.25, "uid": 501, "executable": "/tmp/RetainedQt.app/Contents/MacOS/RetainedQt"}
    target = {"bundle_id": identity["bundle_id"], "version": "1", "build": "1", "sha256": "9" * 64, "files": 1, "bytes": 1}
    request = B.invocation_request(actor)
    result.update(identity=copy.deepcopy(identity), after_identity=copy.deepcopy(identity),
        process={"pid": identity["pid"], "executable": identity["executable"], "architecture": "arm64"})
    adapter = objc_adapter()
    adapter["transport"] = {"kind": "local"}
    receipt = {"format": B.RUNTIME, "status": "executed", "target": target, "bundle": identity["bundle"],
        "request": request, "request_sha256": R.digest(request), "expected_identity": copy.deepcopy(identity),
        "identity": identity, "after_identity": copy.deepcopy(identity), "adapter": adapter, "transport": {"kind": "local"},
        "worker": {"returncode": 0, "timed_out": False}, "native_resources": copy.deepcopy(resources),
        "compiler_provenance": {}, "resource_review_required": False, "result": result,
        "actuated": True, "native_calls": True, "verified": False, "evidence_only": True, "proof_created": False}
    run = {"runtime": receipt, "pid": identity["pid"], "uid": identity["uid"], "bundle": identity["bundle"],
           "permission": "runtime_attached", "primitive": "qt_moc_method", "returncode": 0, "timed_out": False}
    candidate = {"actor": actor, "target": target, "params": [{"name": "reference", "type": "string", "source": "text"}]}
    return candidate, {"reference": "independent evidence is not an actor argument"}, run, recipe, files


def test_retained_qt_boundary_reuses_the_full_shared_result_gate_without_synthetic_learning(qt_moc_retained):
    from anyuse.execution import rea_runtime_bridge as B
    candidate, inputs, run, recipe, files = qt_moc_retained
    assert H._objc_run(candidate, inputs, run, check_files=False)
    receipt = run["runtime"]
    assert B.validate_qt_moc_result(receipt["request"], receipt["result"]) == receipt["result"]["operations"][0]["qt_moc"]
    assert set(receipt["native_resources"]) == set(B.native_file_claims(candidate["actor"]))
    assert receipt["result"]["native_calls"] == receipt["result"]["invocations"] == 1


@pytest.mark.parametrize("fault", ["legacy", "missing_observer", "owner_thread", "extra_call", "hook_cleanup", "error",
                                    "source_pin", "runtime_plan", "resource_bool", "loaded_image"])
def test_retained_qt_global_evidence_cannot_be_replaced_by_a_valid_void_row(qt_moc_retained, fault):
    from anyuse.execution import rea_runtime_bridge as B
    from anyuse.execution import rea_qt_moc as Q
    candidate, inputs, run, recipe, files = qt_moc_retained
    assert H._objc_run(candidate, inputs, run, check_files=False)
    receipt, result = run["runtime"], run["runtime"]["result"]
    row = result["operations"][0]
    if fault == "legacy":
        receipt["format"], result["format"] = B.LEGACY_RUNTIME, B.LEGACY_WORKER
        del result["frida_lifecycle"], result["cleanup_errors"]
    elif fault == "missing_observer": del result["lifecycle_observer"]
    elif fault == "owner_thread": result["lifecycle_observer"]["owner_thread_id"] += 1
    elif fault == "extra_call": result["native_calls"] = 2
    elif fault == "hook_cleanup": result["lifecycle_observer"]["hooks_detached"] -= 1
    elif fault == "error": result["error"] = {"type": "Error", "message": "inert completed receipt still carried a failure"}
    elif fault == "loaded_image": row["function"]["image_identity"]["uuid"] = "0" * 32
    else:
        pins = receipt["native_resources"]
        selection = candidate["actor"]["selection"]
        if fault == "source_pin":
            from anyuse.execution import rea_qt_moc as Q
            source = str(Path(selection["qt_sources"]["file"]).parent / next(iter(Q._SOURCES)))
            del pins[source]
        elif fault == "runtime_plan": del pins[selection["catalog"]["file"]]["qt_moc_dispatches"]
        else: pins[selection["catalog"]["file"]]["qt_moc_dispatches"][0]["consumer"]["this_adjustment"] = False
        result["native_resources"] = copy.deepcopy(pins)
    # The per-row public callback ABI and void result remain valid. Only the
    # GLOBAL ledger/resource/current-envelope authority was changed.
    Q.validate_row(row, receipt["request"]["operations"][0])
    B.validate_native_path_observations(receipt["request"]["operations"][0], row)
    assert row["value"] is None and result["native_calls"] >= B.minimum_native_calls(candidate["actor"])
    assert not H._objc_run(candidate, inputs, run, check_files=False)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
