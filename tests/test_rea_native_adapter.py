"""Fixed compiler adapter regressions; inert files and mocked compiler only."""
import copy
import os
from pathlib import Path
import types

import pytest

from anyuse.execution import rea_native_adapter as A

TRIPLE = 'target triple = "arm64-apple-macosx13.0.0"\n'
PROBE_IR = TRIPLE + "\n".join(
    f"define i64 @anyuse_{name}() {{ ret i64 {value} }}"
    for name, value in {"string_size": 24, "string_alignment": 8, "vector_size": 24, "vector_alignment": 8,
                        "libcpp_version": 210106, "libcpp_abi_version": 1}.items()) + r'''
@.str = private unnamed_addr constant [4 x i8] c"__1\00"
define ptr @anyuse_libcpp_namespace() { ret ptr @.str }
'''
MACROS = "#define _LIBCPP_VERSION 210106\n#define _LIBCPP_ABI_VERSION 1\n#define _LIBCPP_ABI_NAMESPACE __1\n"
VERSION = "Apple clang version 21.0.0 (inert test compiler)"


def original_ir(family, *, name="private_entry"):
    if family == "cxx_owned_string_bytes":
        return (TRIPLE + 'define void @' + name + '(ptr dead_on_unwind noalias writable '
                'sret(%"class.std::__1::basic_string") align 8 %result, '
                'ptr noundef nonnull align 8 dereferenceable(8) %self, ptr noundef %label) { ret void }\n')
    signature = ("swiftcc double @private_entry(double %0, i1 %1, ptr swiftself %2)"
                 if family == "swift_double_bool_context" else
                 "double @private_entry(ptr noundef %0, ptr noundef %1, ptr noundef %2)")
    return TRIPLE + "define " + signature.replace("@private_entry", "@" + name) + " { ret double 0.0 }\n"


def helper_ir(family):
    if family == "cxx_owned_string_bytes":
        return (TRIPLE + 'define i32 @anyuse_rea_adapter_v1() {\n  invoke void %entry('
                'ptr dead_on_unwind writable sret(%"class.std::__1::basic_string") align 8 %result, '
                'ptr noundef %self, ptr noundef %label) to label %done unwind label %caught\n  ret i32 0\n}\n')
    call = ("call swiftcc double %entry(double %value, i1 %flag, ptr swiftself %self)"
            if family == "swift_double_bool_context" else
            "invoke double %entry(ptr %self, ptr %label, ptr %values) to label %done unwind label %caught")
    return TRIPLE + "define i32 @anyuse_rea_adapter_v1() {\n  %result = " + call + "\n  ret i32 0\n}\n"


def claim(file):
    return {"file": str(file), "sha256": A.R._file_hash(file)}


def request_fixture(tmp_path, monkeypatch, *, family="swift_double_bool_context", alternate=False):
    monkeypatch.setattr(A.R, "assert_guest", lambda: None)
    monkeypatch.setattr(A.B, "_macho_images", lambda _: [{"cpu_type": 0x0100000C, "cpu_subtype": 0, "uuid": "1" * 32}])
    compiler = (tmp_path / "clang").resolve()
    compiler.write_bytes(b"Inert compiler identity, never executable native code.")
    compiler.chmod(0o700)
    sdk = (tmp_path / "sdk").resolve()
    sdk.mkdir()
    settings = sdk / "SDKSettings.json"
    A.R.write_json(settings, {"Version": "26.5"})
    include = (tmp_path / "include").resolve()
    include.mkdir()
    for name in ("string", "vector", "__config"):
        (include / name).write_text("Inert header identity: " + name)
    policy = {"format": A.POLICY, "compiler": claim(compiler), "target_triple": "arm64-apple-macos13.0",
              "sdk": {"path": str(sdk), "version": "26.5", "build": "unavailable-in-pinned-SDK-metadata", "settings": claim(settings)},
              "libcxx": {"include": str(include), "sha256": A._tree(include)[1]}}
    policy_file = (tmp_path / "policy.json").resolve()
    A.R.write_json(policy_file, policy)
    policy_file.chmod(0o600)
    monkeypatch.setenv("ANYUSE_REA_NATIVE_ADAPTER_TOOLCHAIN_POLICY", str(policy_file))
    source = (tmp_path / "original-source.txt").resolve()
    source.write_text("Original private declarations are evidence, never generated model source.")
    module = (tmp_path / "original-image.bin").resolve()
    module.write_bytes(b"Inert original module, never loaded.")
    ir = (tmp_path / "original.ll").resolve()
    ir.write_text(original_ir(family))
    declaration = (tmp_path / "original-declaration.json").resolve()
    original = {"module": {"path": str(module), "sha256": A.R._file_hash(module)}, "symbol": "private_entry",
                "abi": "swift" if family == "swift_double_bool_context" else "default",
                "argument_types": (["double", "bool"] if family == "swift_double_bool_context" else
                                   copy.deepcopy(A.CPP_TYPES[:1] if family == "cxx_owned_string_bytes" else A.CPP_TYPES)),
                "return_type": copy.deepcopy(A.CPP_TYPES[0]) if family == "cxx_owned_string_bytes" else "double",
                "context": "swiftself" if family == "swift_double_bool_context" else "cxx_this",
                "pointer_policy": "borrowed_no_escape",
                "signature_evidence": {"kind": "compiler", "file": str(declaration), "sha256": "0" * 64, "pointer": "/operation"}}
    A.R.write_json(declaration, {"operation": A.original_contract(original)})
    original["signature_evidence"]["sha256"] = A.R._file_hash(declaration)
    macros = MACROS + ("#define _LIBCPP_ABI_ALTERNATE_STRING_LAYOUT\n" if alternate else "")
    cpp_identity = {**policy["libcxx"], **A._probe_values(PROBE_IR, macros)}
    original_build = (tmp_path / "original-build.json").resolve()
    ir_claim = {**claim(ir), "function": "private_entry"}
    selected = {"format": A.ORIGINAL_BUILD, "entry": A._entry(original), "source": claim(source), "ir": ir_claim,
                "compiler": {**claim(compiler), "version": VERSION}, "target_triple": "arm64-apple-macosx13.0.0", "sdk": policy["sdk"]}
    if family in {"cxx_owned_string_vector_double", "cxx_owned_string_bytes"}:
        selected["libcxx"] = cpp_identity
    A.R.write_json(original_build, {"compiled": selected})
    request = {"format": A.REQUEST, "family": family, "original": original,
               "evidence": {"source": claim(source), "ir": ir_claim, "build": {**claim(original_build), "pointer": "/compiled"}},
               "limits": ({} if family == "swift_double_bool_context" else
                          {"max_utf8_bytes": 16384, "max_result_bytes": 128} if family == "cxx_owned_string_bytes" else
                          {"max_utf8_bytes": 16384, "max_vector_elements": 16})}
    calls = []
    def run(argv, **kwargs):
        calls.append(list(argv))
        if "--version" in argv:
            stdout = (VERSION + "\n").encode()
        elif "-dM" in argv:
            stdout = macros.encode()
        elif "-o" in argv:
            destination = Path(argv[argv.index("-o") + 1])
            if "-emit-llvm" in argv:
                destination.write_text(PROBE_IR if destination.name == "abi-probe.ll" else helper_ir(family))
            else:
                destination.write_bytes(b"Inert generated helper identity, never loaded.")
            stdout = b""
        else:
            pytest.fail("Unexpected compiler command")
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")
    monkeypatch.setattr(A.subprocess, "run", run)
    return request, calls, policy_file


def evidence(out):
    return {"kind": "compiler", **claim(out / "build.json"), "pointer": "/contract"}


@pytest.mark.parametrize("family", sorted(A.FAMILIES))
def test_fixed_data_templates_keep_original_identity_out_of_native_source(tmp_path, monkeypatch, family):
    request, _, _ = request_fixture(tmp_path, monkeypatch, family=family)
    source = A.render_source(request)
    assert request["original"]["symbol"] not in source
    assert request["original"]["module"]["path"] not in source
    assert "anyuse_rea_adapter_v1" in source and "dispatch" in source
    if family == "cxx_owned_string_vector_double":
        assert "std::string label" in source and "std::vector<double> values" in source
        assert "std::move(label)" in source and "catch (...)" in source
        assert "!before!" not in source and "!after!" not in source
    elif family == "cxx_owned_string_bytes":
        assert "std::string (*)(void *, std::string)" in source
        assert "std::string result = reinterpret_cast<PrivateEntry>(entry)" in source
        assert "std::memcpy(payload, result.data(), copied)" in source and "catch (...)" in source
        assert "c_str()" not in source and "strlen(result" not in source
        assert A.completion_contract(request) == {"format": "anyuse.rea-compiler-bytes-completion/1", "argument": 4,
            "bytes": 24, "status_return": "uint32", "dispatch_flag": "dispatch_attempted", "result_type": "bytes",
            "payload_argument": 3, "payload_bytes": 128, "length_offset": 16}
    else:
        assert "swiftcall" in source and "swift_context" in source


def test_swift_function_type_attribute_uses_the_guest_compiler_verified_typedef(tmp_path, monkeypatch):
    request, _, _ = request_fixture(tmp_path, monkeypatch)
    source = A.render_source(request)
    assert "typedef __attribute__((swiftcall)) double (*PrivateEntry)(double, bool," in source
    assert "double (__attribute__((swiftcall)) *)" not in source
    actual_indirect_ir = (TRIPLE + "define i32 @anyuse_rea_adapter_v1() {\n"
        "  %15 = call swiftcc double %10(double noundef %11, i1 noundef %13, ptr noundef swiftself %14)\n"
        "  ret i32 0\n}\n")
    checked = A.lowering_check(original_ir("swift_double_bool_context"), actual_indirect_ir,
                               "private_entry", "swift_double_bool_context")
    assert checked["status"] == "passed" and checked["indirect_helper_call"]["arguments"][2]["swiftself"]


@pytest.mark.parametrize("operation", ["call", "invoke"])
def test_actual_assigned_swift_call_rows_preserve_context_and_checked_calling_convention(operation):
    helper = (TRIPLE + "define i32 @anyuse_rea_adapter_v1() {\n"
        f"  %35 = {operation} swiftcc double %30(double noundef %31, i1 noundef %33, ptr noundef swiftself %34)\n"
        "  ret i32 0\n}\n")
    assert A.lowering_check(original_ir("swift_double_bool_context"), helper, "private_entry", "swift_double_bool_context")["status"] == "passed"
    for altered in (helper.replace("swiftcc", "cc 10"), helper.replace("ptr noundef swiftself", "ptr byval(%owned) swiftself")):
        with pytest.raises(ValueError):
            A.lowering_check(original_ir("swift_double_bool_context"), altered, "private_entry", "swift_double_bool_context")


@pytest.mark.parametrize("family", sorted(A.FAMILIES))
def test_build_pins_actual_lowering_and_transitive_artifacts_without_loading_or_task_proof(tmp_path, monkeypatch, family):
    request, calls, _ = request_fixture(tmp_path, monkeypatch, family=family)
    out = (tmp_path / "generated").resolve()
    receipt = A.build(request, out)
    assert len(calls) == 5 and all(argv[0] == str(tmp_path / "clang") for argv in calls)
    assert receipt["status"] == "built" and receipt["lowering_check"]["status"] == "passed"
    assert not receipt["loaded_into_target"] and not receipt["runtime_verified"] and not receipt["verified"]
    assert receipt["contract"]["original"] == A.original_contract(request["original"])
    assert receipt["contract"]["completion"]["dispatch_flag"] == "dispatch_attempted"
    pins = A.resources(evidence(out))
    assert request["original"]["module"]["path"] in pins
    assert str(tmp_path / "include/string") in pins and str(tmp_path / "clang") in pins
    compiler_row = next(row for row in receipt["artifacts"] if row["file"] == str(tmp_path / "clang"))
    assert {"original_compiler", "toolchain_compiler"} <= set(compiler_row["roles"])


@pytest.mark.parametrize("operation", ["call", "invoke"])
def test_owned_return_requires_original_and_helper_exact_sret_and_independent_layout(operation):
    family = "cxx_owned_string_bytes"
    original = original_ir(family, name="avoid_void_symbol")
    helper = helper_ir(family).replace("invoke void", operation + " void")
    checked = A.lowering_check(original, helper, "avoid_void_symbol", family,
                               string_layout={"string_size": 24, "string_alignment": 8})
    expected = {"convention": "ccc", "return_type": "void", "arguments": [
        {"type": "ptr", "sret": {"llvm_type": "class.std::__1::basic_string", "alignment": 8}},
        {"type": "ptr"}, {"type": "ptr"}]}
    assert checked["original"] == checked["indirect_helper_call"] == expected
    assert checked["string_layout"] == {"string_size": 24, "string_alignment": 8}
    assert checked["prototype_automatically_recovered"] is False


@pytest.mark.parametrize("change", ["no_sret", "wrong_type", "alignment", "extra_sret", "sret_on_this", "byval",
    "swiftself", "wrong_cc", "extra_argument", "second_call", "layout_alignment", "layout_boolean", "no_layout", "unknown_attribute"])
def test_owned_return_sret_lowering_cannot_be_inferred_from_pointer_count_or_a_declared_name(change):
    family = "cxx_owned_string_bytes"
    original, helper = original_ir(family), helper_ir(family)
    layout = {"string_size": 24, "string_alignment": 8}
    if change == "no_sret":
        helper = helper.replace('sret(%"class.std::__1::basic_string") ', '')
    elif change == "wrong_type":
        helper = helper.replace('class.std::__1::basic_string', 'class.std::__1::vector')
    elif change == "alignment":
        helper = helper.replace('align 8', 'align 16')
    elif change == "extra_sret":
        helper = helper.replace('align 8 %result', 'sret(%"class.std::__1::basic_string") align 8 %result')
    elif change == "sret_on_this":
        helper = helper.replace('ptr noundef %self', 'ptr sret(%"class.std::__1::basic_string") align 8 %self')
    elif change in {"byval", "swiftself", "unknown_attribute"}:
        attribute = 'byval(%owned)' if change == "byval" else 'swiftself' if change == "swiftself" else 'unreviewed'
        helper = helper.replace('ptr noundef %label', 'ptr ' + attribute + ' %label')
    elif change == "wrong_cc":
        helper = helper.replace('invoke void', 'invoke fastcc void')
    elif change == "extra_argument":
        helper = helper.replace('ptr noundef %label)', 'ptr noundef %label, ptr %extra)')
    elif change == "second_call":
        # Duplicate the actual indirect call, independent of other helper IR rows.
        helper = helper_ir(family) + next(line for line in helper_ir(family).splitlines() if 'invoke void' in line) + '\n'
    elif change == "layout_alignment":
        layout["string_alignment"] = 16
        layout["string_size"] = 32
    elif change == "layout_boolean":
        layout["string_size"] = True
    else:
        layout = None
    with pytest.raises(ValueError):
        A.lowering_check(original, helper, "private_entry", family, string_layout=layout)


@pytest.mark.parametrize("field,value", [("max_result_bytes", 0), ("max_result_bytes", True),
    ("max_result_bytes", 16385), ("max_utf8_bytes", 0), ("max_utf8_bytes", 16385)])
def test_owned_return_bounds_stop_before_compiler_or_output_access(tmp_path, monkeypatch, field, value):
    request, calls, _ = request_fixture(tmp_path, monkeypatch, family="cxx_owned_string_bytes")
    request["limits"][field] = value
    with pytest.raises(ValueError):
        A.build(request, (tmp_path / "refused").resolve())
    assert calls == [] and not (tmp_path / "refused").exists()


def test_canonical_clang_links_cpp_runtime_and_passes_comma_install_name_as_one_literal_argument(tmp_path, monkeypatch):
    request, calls, _ = request_fixture(tmp_path, monkeypatch)
    out = (tmp_path / "literal,not,linker,flags").resolve()
    A.build(request, out)
    linked = next(argv for argv in calls if "-dynamiclib" in argv)
    assert "-lc++" in linked
    position = linked.index("-install_name")
    assert linked[position - 1:position + 3] == ["-Xlinker", "-install_name", "-Xlinker", str(out / "adapter.dylib")]
    assert not any(argument.startswith("-Wl,") for argument in linked)


def test_vm_guard_precedes_any_file_compiler_or_output_access(tmp_path, monkeypatch):
    def denied():
        raise PermissionError("VM only")
    monkeypatch.setattr(A.R, "assert_guest", denied)
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: pytest.fail("host compiler"))
    for action in (lambda: A.build({}, tmp_path / "bad"),
                   lambda: A.inspect_toolchain(tmp_path / "bad"),
                   lambda: A.resources({})):
        with pytest.raises(PermissionError, match="VM only"):
            action()
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("change", ["source", "flags", "family", "owned_return", "virtual", "offset", "different_ir", "large_limit", "bool_limit"])
def test_model_source_unknown_abis_and_uncorrelated_entries_stop_before_compilation(tmp_path, monkeypatch, change):
    request, calls, _ = request_fixture(tmp_path, monkeypatch, family="cxx_owned_string_vector_double")
    if change in {"source", "flags"}:
        request[change] = "#include arbitrary_model_source"
    elif change == "family":
        request["family"] = "arbitrary_native_method"
    elif change == "owned_return":
        request["original"]["return_type"] = copy.deepcopy(A.CPP_TYPES[0])
    elif change == "virtual":
        request["original"]["member_adjustment"] = 8
    elif change == "offset":
        request["original"].pop("symbol")
        request["original"]["offset"] = "0x1000"
    elif change == "different_ir":
        request["evidence"]["ir"]["function"] = "unrelated_private_entry"
    else:
        request["limits"]["max_vector_elements"] = True if change == "bool_limit" else 257
    with pytest.raises(ValueError):
        A.build(request, (tmp_path / "refused").resolve())
    assert not calls and not (tmp_path / "refused").exists()


@pytest.mark.parametrize("changed", ["cc 10", "fastcc", "coldcc", "x86_thiscallcc"])
def test_numeric_and_unknown_llvm_calling_conventions_do_not_become_default_c_abi(changed):
    ir = original_ir("cxx_owned_string_vector_double").replace("define double", "define " + changed + " double")
    with pytest.raises(ValueError):
        A.lowering_check(ir, helper_ir("cxx_owned_string_vector_double"), "private_entry", "cxx_owned_string_vector_double")


@pytest.mark.parametrize("attribute", ["byval(%owned)", "nest", "inreg", "preallocated(%owned)", "sret(%owned)"])
def test_pointer_abi_attributes_cannot_be_erased_from_owned_argument_lowering(attribute):
    ir = original_ir("cxx_owned_string_vector_double").replace("ptr noundef %0", "ptr " + attribute + " %0")
    with pytest.raises(ValueError):
        A.lowering_check(ir, helper_ir("cxx_owned_string_vector_double"), "private_entry", "cxx_owned_string_vector_double")


@pytest.mark.parametrize("change", ["bool_width", "missing_self", "wrong_convention", "wrong_triple", "second_call"])
def test_swift_fp_bool_context_requires_the_actual_single_swiftcc_indirect_lowering(change):
    ir = helper_ir("swift_double_bool_context")
    if change == "bool_width":
        ir = ir.replace("i1 %flag", "i8 %flag")
    elif change == "missing_self":
        ir = ir.replace("ptr swiftself", "ptr")
    elif change == "wrong_convention":
        ir = ir.replace("swiftcc", "ccc")
    elif change == "wrong_triple":
        ir = ir.replace("arm64-", "arm64e-")
    else:
        ir += "  %again = call swiftcc double %entry(double %value, i1 %flag, ptr swiftself %self)\n"
    with pytest.raises(ValueError):
        A.lowering_check(original_ir("swift_double_bool_context"), ir, "private_entry", "swift_double_bool_context")


def test_default_alternate_string_layout_requires_exact_original_helper_macro_and_header_identity(tmp_path, monkeypatch):
    request, _, _ = request_fixture(tmp_path, monkeypatch, family="cxx_owned_string_vector_double", alternate=True)
    out = (tmp_path / "alternate").resolve()
    receipt = A.build(request, out)
    assert receipt["toolchain"]["libcxx"]["string_layout"] == "alternate"
    assert "_LIBCPP_ABI_ALTERNATE_STRING_LAYOUT" in receipt["toolchain"]["libcxx"]["abi_defines"]
    assert A.resources(evidence(out))
    selected = A.R.load_json(request["evidence"]["build"]["file"])
    selected["compiled"]["libcxx"]["abi_defines"].pop("_LIBCPP_ABI_ALTERNATE_STRING_LAYOUT")
    A.R.write_json(request["evidence"]["build"]["file"], selected)
    request["evidence"]["build"]["sha256"] = A.R._file_hash(Path(request["evidence"]["build"]["file"]))
    with pytest.raises(ValueError, match="ABI differs"):
        A.build(request, (tmp_path / "mismatch").resolve())
    assert not (tmp_path / "mismatch/adapter.cpp").exists()


def test_trivial_owned_abi_remains_unsupported_even_when_object_sizes_match():
    with pytest.raises(ValueError, match="trivial ABI"):
        A._probe_values(PROBE_IR, MACROS + "#define _LIBCPP_ABI_ENABLE_STRING_TRIVIAL_ABI\n")


def test_changed_policy_between_probe_and_compile_is_not_a_compatible_abi(tmp_path, monkeypatch):
    request, calls, policy_file = request_fixture(tmp_path, monkeypatch)
    original = A.inspect_toolchain
    def changed(out, **kwargs):
        result = original(out, **kwargs)
        (tmp_path / "include/string").write_text("Changed header after the observed probe")
        policy = A.R.load_json(policy_file)
        policy["libcxx"]["sha256"] = A._tree(tmp_path / "include")[1]
        A.R.write_json(policy_file, policy)
        return result
    monkeypatch.setattr(A, "inspect_toolchain", changed)
    with pytest.raises(ValueError, match="changed after"):
        A.build(request, (tmp_path / "refused").resolve())
    assert len(calls) == 3 and not (tmp_path / "refused/adapter.cpp").exists()


@pytest.mark.parametrize("change", ["header", "original_ir", "helper_ir", "helper_source", "compiler", "missing_header_role", "foreign_header_role"])
def test_runtime_resource_collection_rechecks_all_original_generated_compiler_and_sdk_bytes(tmp_path, monkeypatch, change):
    request, _, _ = request_fixture(tmp_path, monkeypatch)
    out = (tmp_path / "generated").resolve()
    receipt = A.build(request, out)
    assert A.resources(evidence(out))
    if change in {"missing_header_role", "foreign_header_role"}:
        row = next(row for row in receipt["artifacts"] if row["file"] == str(tmp_path / "include/string"))
        if change == "missing_header_role":
            row["roles"] = ["unrelated_file"]
        else:
            other = (tmp_path / "unrelated-header").resolve()
            other.write_text("unrelated")
            row.update(**claim(other), bytes=other.stat().st_size)
        A.R.write_json(out / "build.json", receipt)
    else:
        file = {"header": tmp_path / "include/string", "original_ir": tmp_path / "original.ll",
                "helper_ir": out / "adapter.ll", "helper_source": out / "adapter.cpp", "compiler": tmp_path / "clang"}[change]
        file.write_text("Changed transitive provenance")
    with pytest.raises(ValueError):
        A.resources(evidence(out))


def test_output_path_cannot_write_into_original_or_any_other_app(tmp_path, monkeypatch):
    request, _, _ = request_fixture(tmp_path, monkeypatch)
    app = tmp_path / "Owned.app"
    app.mkdir()
    with pytest.raises(ValueError, match="never writes into an app"):
        A.build(request, app / "adapter")
    assert not (app / "adapter").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
