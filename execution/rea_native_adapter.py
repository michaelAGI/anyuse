"""Fixed native ABI adapters compiled only in a VM from bounded data recipes.

The compiler translates declared fixed ABI families. It does not recover an ABI,
execute model source, load its output, or claim that a returned helper proves a
task. Original and generated code/prototypes retain separate evidence.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from . import rea_mapping as R
from . import rea_runtime_bridge as B

REQUEST = "anyuse.rea-native-adapter-request/1"
BUILD = "anyuse.rea-native-adapter-build/1"
POLICY = "anyuse.rea-adapter-toolchain-policy/1"
ORIGINAL_BUILD = "anyuse.rea-native-original-build/1"
TOOLCHAIN = "anyuse.rea-adapter-toolchain-observation/1"
FAMILIES = {"swift_double_bool_context", "cxx_owned_string_vector_double", "cxx_owned_string_bytes"}
SYMBOL = "anyuse_rea_adapter_v1"
MAX_FILE = 512 * 1024**2
MAX_IR = 64 * 1024**2
SHA = re.compile(r"[0-9a-f]{64}\Z")
CPP_TYPES = [{"kind": "cxx_owned", "name": name, "library": "libc++", "abi_version": 1}
             for name in ("std::string", "std::vector<double>")]
ABI_DEFINES_DENIED = {
    "_LIBCPP_ABI_ENABLE_STRING_TRIVIAL_ABI",
    "_LIBCPP_ABI_ENABLE_VECTOR_TRIVIAL_ABI",
}


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("invalid closed native adapter schema")


def _text(value, limit=4096):
    return isinstance(value, str) and bool(value) and "\0" not in value and len(value.encode()) <= limit


def _claim(value, *, pointer=False):
    _keys(value, {"file", "sha256"} | ({"pointer"} if pointer else set()))
    if (not _text(value["file"]) or not Path(value["file"]).is_absolute() or
            not isinstance(value["sha256"], str) or SHA.fullmatch(value["sha256"]) is None or
            pointer and (not isinstance(value["pointer"], str) or
                         value["pointer"] and not value["pointer"].startswith("/"))):
        raise ValueError("adapter evidence requires an absolute file and exact SHA-256")


def _entry(original):
    return {key: copy.deepcopy(original[key]) for key in ("module", "symbol", "offset") if key in original}


def original_contract(original):
    return B.callable_contract(original, allow_owned_return=original.get("return_type") == CPP_TYPES[0])


def validate_request(request):
    """Pure validation: no compiler, filesystem, VM or model source execution."""
    _keys(request, {"format", "family", "original", "evidence", "limits"})
    if request["format"] != REQUEST or not isinstance(request["family"], str) or request["family"] not in FAMILIES:
        raise ValueError("unsupported fixed compiler-adapter family")
    original = request["original"]
    _keys(original, {"module", "abi", "argument_types", "return_type", "context", "signature_evidence", "pointer_policy"},
          {"symbol", "offset"})
    _keys(original["module"], {"path", "sha256"})
    _claim({"file": original["module"]["path"], "sha256": original["module"]["sha256"]})
    if ("symbol" in original) == ("offset" in original):
        raise ValueError("original private entry needs one symbol or image-relative offset")
    if "symbol" in original and not _text(original["symbol"], 1024):
        raise ValueError("original symbol must be literal bounded evidence")
    if "offset" in original and (not isinstance(original["offset"], str) or
                                 re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]{0,15})", original["offset"]) is None):
        raise ValueError("original offset must be canonical image-relative hexadecimal")
    evidence = original["signature_evidence"]
    _keys(evidence, {"kind", "file", "sha256", "pointer"})
    _claim({key: evidence[key] for key in ("file", "sha256", "pointer")}, pointer=True)
    if evidence["kind"] not in {"compiler", "dwarf", "header", "reviewed_contract"}:
        raise ValueError("original ABI needs attributable declaration evidence")
    swift = request["family"] == "swift_double_bool_context"
    owned_return = request["family"] == "cxx_owned_string_bytes"
    if (original["abi"] != ("swift" if swift else "default") or
            original["argument_types"] != (["double", "bool"] if swift else CPP_TYPES[:1] if owned_return else CPP_TYPES) or
            original["return_type"] != (CPP_TYPES[0] if owned_return else "double") or
            original["context"] != ("swiftself" if swift else "cxx_this") or
            original["pointer_policy"] != "borrowed_no_escape"):
        raise ValueError("original signature differs from its fixed compiler family; virtual/adjusted/generic/throwing Swift ABI is unsupported")
    _keys(request["evidence"], {"source", "ir", "build"})
    _claim(request["evidence"]["source"])
    _keys(request["evidence"]["ir"], {"file", "sha256", "function"})
    _claim({key: request["evidence"]["ir"][key] for key in ("file", "sha256")})
    if not _text(request["evidence"]["ir"]["function"], 1024):
        raise ValueError("original IR must name one exact compiler function")
    ir_name = request["evidence"]["ir"]["function"]
    if "symbol" not in original or original["symbol"] not in {ir_name, "_" + ir_name}:
        raise ValueError("compiler-correlated adapters require this exact symbol/IR name; offset correlation is unsupported")
    _claim(request["evidence"]["build"], pointer=True)
    limits = request["limits"]
    if swift:
        _keys(limits, set())
    elif owned_return:
        _keys(limits, {"max_utf8_bytes", "max_result_bytes"})
        if (type(limits["max_utf8_bytes"]) is not int or not 1 <= limits["max_utf8_bytes"] <= 16384 or
                type(limits["max_result_bytes"]) is not int or not 1 <= limits["max_result_bytes"] <= 16384):
            raise ValueError("owned string input/result copy limits exceed their fixed bounded family")
    else:
        _keys(limits, {"max_utf8_bytes", "max_vector_elements"})
        if (type(limits["max_utf8_bytes"]) is not int or not 1 <= limits["max_utf8_bytes"] <= 16384 or
                type(limits["max_vector_elements"]) is not int or not 1 <= limits["max_vector_elements"] <= 256):
            raise ValueError("owned argument limits exceed their fixed bounded family")
    return copy.deepcopy(request)


def outer_contract(request):
    request = validate_request(request)
    out = {"kind": "buffer", "bytes": 24, "direction": "out"}
    if request["family"] == "swift_double_bool_context":
        arguments = ["pointer", "pointer", "double", "bool", out]
    elif request["family"] == "cxx_owned_string_bytes":
        arguments = ["pointer", "pointer", "utf8",
                     {"kind": "buffer", "bytes": request["limits"]["max_result_bytes"], "direction": "out"}, out]
    else:
        arguments = ["pointer", "pointer", "utf8", "uint32",
                     {"kind": "buffer", "bytes": 8 * request["limits"]["max_vector_elements"], "direction": "in"}, out]
    return {"abi": "default", "argument_types": arguments, "return_type": "uint32",
            "context": False, "pointer_policy": "borrowed_no_escape"}


def completion_contract(request):
    request = validate_request(request)
    outer = outer_contract(request)
    completion = {"format": "anyuse.rea-compiler-completion/1", "argument": len(outer["argument_types"]) - 1,
                  "bytes": 24, "status_return": "uint32", "dispatch_flag": "dispatch_attempted"}
    if request["family"] == "cxx_owned_string_bytes":
        completion.update(format="anyuse.rea-compiler-bytes-completion/1", result_type="bytes",
                          payload_argument=3, payload_bytes=request["limits"]["max_result_bytes"], length_offset=16)
    return completion


def render_source(request):
    """Render trusted templates only; caller strings never become native source."""
    request = validate_request(request)
    prefix = r'''#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cmath>

static std::uint32_t complete(std::uint8_t *out, std::uint32_t status,
    std::uint32_t dispatch_attempted, std::uint32_t uncertain, double value) noexcept {
  if (!out) return 1;
  const std::uint32_t reserved = 0;
  std::memcpy(out, &status, 4);
  std::memcpy(out + 4, &dispatch_attempted, 4);
  std::memcpy(out + 8, &uncertain, 4);
  std::memcpy(out + 12, &reserved, 4);
  std::memcpy(out + 16, &value, 8);
  return status;
}
static_assert(sizeof(double) == 8 && sizeof(std::uint32_t) == 4, "completion widths");
#if !defined(__aarch64__) || !defined(__APPLE__) || defined(__arm64e__)
#error unsupported compiler adapter architecture
#endif
'''
    if request["family"] == "swift_double_bool_context":
        body = r'''
typedef __attribute__((swiftcall)) double (*PrivateEntry)(double, bool,
    void *context __attribute__((swift_context)));
extern "C" __attribute__((visibility("default")))
std::uint32_t anyuse_rea_adapter_v1(void *entry, void *self, double value,
    bool enabled, std::uint8_t *out) noexcept {
  if (!out) return 1;
  if (!entry || !self || !std::isfinite(value)) return complete(out, 1, 0, 0, 0.0);
  const double result = reinterpret_cast<PrivateEntry>(entry)(value, enabled, self);
  if (!std::isfinite(result)) return complete(out, 3, 1, 1, 0.0);
  return complete(out, 0, 1, 0, result);
}
'''
    elif request["family"] == "cxx_owned_string_bytes":
        body = r'''
#include <string>
#include <utility>
#include <type_traits>
static_assert(_LIBCPP_ABI_VERSION == 1, "only the pinned libc++ ABI family");
static_assert(std::is_nothrow_move_constructible<std::string>::value, "string move ABI");
static_assert(std::is_nothrow_destructible<std::string>::value, "string destruction ABI");
using PrivateEntry = std::string (*)(void *, std::string);
static std::uint32_t complete_bytes(std::uint8_t *out, std::uint32_t status,
    std::uint32_t attempted, std::uint32_t uncertain, std::uint32_t length) noexcept {
  if (!out) return 1;
  const std::uint32_t reserved = 0;
  std::memcpy(out, &status, 4);
  std::memcpy(out + 4, &attempted, 4);
  std::memcpy(out + 8, &uncertain, 4);
  std::memcpy(out + 12, &reserved, 4);
  std::memcpy(out + 16, &length, 4);
  std::memcpy(out + 20, &reserved, 4);
  return status;
}
extern "C" __attribute__((visibility("default")))
std::uint32_t anyuse_rea_adapter_v1(void *entry, void *self, const char *text,
    std::uint8_t *payload, std::uint8_t *out) noexcept {
  if (!out) return 1;
  if (!payload) return complete_bytes(out, 1, 0, 0, 0);
  std::memset(payload, 0, ANYUSE_RESULT_LIMIT);
  if (!entry || !self || !text) return complete_bytes(out, 1, 0, 0, 0);
  const std::size_t length = strnlen(text, ANYUSE_UTF8_LIMIT + 1);
  if (length > ANYUSE_UTF8_LIMIT) return complete_bytes(out, 1, 0, 0, 0);
  std::uint32_t attempted = 0, status = 0, uncertain = 0, copied = 0;
  try {
    std::string label(text, length);
    attempted = 1; // Dispatch attempted, never independent original-body proof.
    std::string result = reinterpret_cast<PrivateEntry>(entry)(self, std::move(label));
    if (result.size() > ANYUSE_RESULT_LIMIT) {
      status = 3; uncertain = 1; // No truncation, retry or rollback claim.
    } else {
      copied = static_cast<std::uint32_t>(result.size());
      if (copied) std::memcpy(payload, result.data(), copied);
    }
    // The actual owned return and input leave scope before completion is written.
  } catch (...) {
    status = 2; uncertain = 1; copied = 0;
    std::memset(payload, 0, ANYUSE_RESULT_LIMIT);
  }
  return complete_bytes(out, status, attempted, uncertain, copied);
}
'''
        body = (f"#define ANYUSE_RESULT_LIMIT {request['limits']['max_result_bytes']}\n"
                f"#define ANYUSE_UTF8_LIMIT {request['limits']['max_utf8_bytes']}\n" + body)
    else:
        body = r'''
#include <string>
#include <vector>
#include <utility>
#include <type_traits>
static_assert(_LIBCPP_ABI_VERSION == 1, "only the pinned libc++ ABI family");
static_assert(std::is_nothrow_move_constructible<std::string>::value, "string move ABI");
static_assert(std::is_nothrow_move_constructible<std::vector<double>>::value, "vector move ABI");
using PrivateEntry = double (*)(void *, std::string, std::vector<double>);
extern "C" __attribute__((visibility("default")))
std::uint32_t anyuse_rea_adapter_v1(void *entry, void *self, const char *text,
    std::uint32_t count, const std::uint8_t *packed, std::uint8_t *out) noexcept {
  if (!out) return 1;
  if (!entry || !self || !text || !packed || count > ANYUSE_VECTOR_LIMIT)
    return complete(out, 1, 0, 0, 0.0);
  const std::size_t length = strnlen(text, ANYUSE_UTF8_LIMIT + 1);
  if (length > ANYUSE_UTF8_LIMIT) return complete(out, 1, 0, 0, 0.0);
  std::uint32_t attempted = 0;
  std::uint32_t status = 0;
  std::uint32_t uncertain = 0;
  double result = 0.0;
  try {
    std::string label(text, length);
    std::vector<double> values;
    values.reserve(count);
    for (std::uint32_t i = 0; i < count; ++i) {
      double item = 0.0;
      std::memcpy(&item, packed + i * 8, 8);
      if (!std::isfinite(item)) return complete(out, 1, 0, 0, 0.0);
      values.push_back(item);
    }
    // This marks dispatch attempted, never proves entry into the original body.
    attempted = 1;
    result = reinterpret_cast<PrivateEntry>(entry)(self, std::move(label), std::move(values));
    if (!std::isfinite(result)) { status = 3; uncertain = 1; result = 0.0; }
  } catch (...) {
    // RAII cleans temporaries; a caught exception does not roll back app effects.
    status = 2;
    uncertain = 1;
    result = 0.0;
  }
  return complete(out, status, attempted, uncertain, result);
}
'''
        body = (f"#define ANYUSE_VECTOR_LIMIT {request['limits']['max_vector_elements']}\n"
                f"#define ANYUSE_UTF8_LIMIT {request['limits']['max_utf8_bytes']}\n" + body)
    return prefix + body


def _pin_file(claim, limit=MAX_FILE):
    path = Path(claim["file"])
    if path.resolve(strict=True) != path or path.is_symlink() or not path.is_file():
        raise ValueError("adapter evidence must remain a canonical regular file")
    info = path.stat()
    if not 0 < info.st_size <= limit:
        raise ValueError("adapter artifact exceeds its byte budget")
    sha = R._file_hash(path)
    if sha != claim["sha256"]:
        raise ValueError("adapter artifact changed from its pinned SHA-256")
    return {"file": str(path), "sha256": sha, "bytes": info.st_size}


def _tree(path):
    root = Path(path)
    if root.resolve(strict=True) != root or root.is_symlink() or not root.is_dir():
        raise ValueError("libc++ include root must be a canonical installed directory")
    pins, total = {}, 0
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError("libc++ header tree contains an unresolved symlink")
        if file.is_file():
            if len(pins) >= 4096:
                raise ValueError("libc++ header tree exceeds its file budget")
            pin = _pin_file({"file": str(file), "sha256": R._file_hash(file)}, 8 * 1024**2)
            total += pin["bytes"]
            if total > 128 * 1024**2:
                raise ValueError("libc++ headers exceed their total byte budget")
            pins[str(file)] = pin
    if not pins:
        raise ValueError("libc++ header identity is empty")
    return pins, R.digest({str(Path(file).relative_to(root)): pin for file, pin in pins.items()})


def _policy():
    file = Path(os.environ.get("ANYUSE_REA_NATIVE_ADAPTER_TOOLCHAIN_POLICY", ""))
    if not file.is_absolute() or file.is_symlink() or file.resolve(strict=True) != file:
        raise ValueError("an explicit protected operator toolchain policy is required")
    info = file.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024**2:
        raise ValueError("toolchain policy must be an owned regular 0600 file")
    policy = R.load_json(file)
    _keys(policy, {"format", "compiler", "target_triple", "sdk", "libcxx"})
    if policy["format"] != POLICY or policy["target_triple"] != "arm64-apple-macos13.0":
        raise ValueError("only the reviewed plain arm64/deployment family is supported")
    _claim(policy["compiler"])
    compiler = _pin_file(policy["compiler"])
    if Path(compiler["file"]).name not in {"clang", "clang++"} or not os.access(compiler["file"], os.X_OK):
        raise ValueError("policy must pin the installed native clang executable")
    images = B._macho_images(Path(compiler["file"]))
    if not any(image["cpu_type"] in {0x0100000C, 0x01000007} for image in images):
        raise ValueError("policy must pin an installed native Darwin compiler")
    sdk = policy["sdk"]
    _keys(sdk, {"path", "version", "build", "settings"})
    if any(not _text(sdk[key], 256) for key in ("version", "build")):
        raise ValueError("SDK identity requires version and build evidence")
    root = Path(sdk["path"])
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("SDK root must be a canonical installed directory")
    _claim(sdk["settings"])
    settings = _pin_file(sdk["settings"], 1024**2)
    if Path(settings["file"]).parent != root or Path(settings["file"]).name != "SDKSettings.json":
        raise ValueError("policy must select this SDK's actual settings")
    metadata = R.load_json(settings["file"])
    if metadata.get("Version") != sdk["version"]:
        raise ValueError("operator SDK version differs from its pinned actual settings")
    recorded_build = metadata.get("ProductBuildVersion")
    if recorded_build is not None and recorded_build != sdk["build"]:
        raise ValueError("operator SDK build differs from its pinned actual settings")
    if recorded_build is None and sdk["build"] != "unavailable-in-pinned-SDK-metadata":
        raise ValueError("SDK metadata has no build ID; an asserted build is not observed evidence")
    libcxx = policy["libcxx"]
    _keys(libcxx, {"include", "sha256"})
    headers, tree_sha = _tree(libcxx["include"])
    if tree_sha != libcxx["sha256"]:
        raise ValueError("installed libc++ headers differ from operator policy")
    for name in ("string", "vector", "__config"):
        if str(Path(libcxx["include"]) / name) not in headers:
            raise ValueError("libc++ family lacks a pinned required header")
    return policy, {"policy": _pin_file({"file": str(file), "sha256": R._file_hash(file)}),
                    "compiler": compiler, "sdk_settings": settings, "headers": headers}


def _argv(policy, source):
    return [policy["compiler"]["file"], "-x", "c++", "-std=c++17", "-stdlib=libc++",
            "-target", policy["target_triple"], "-isysroot", policy["sdk"]["path"],
            "-isystem", policy["libcxx"]["include"], "-O0", "-fexceptions", "-fcxx-exceptions",
            "-fno-omit-frame-pointer", str(source)]


def _run(argv, folder, name, timeout):
    started = time.monotonic()
    result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    stdout, stderr = folder / f"{name}.stdout", folder / f"{name}.stderr"
    stdout.write_bytes(result.stdout[:4 * 1024**2] or b"\n")
    stderr.write_bytes(result.stderr[:1024**2] or b"\n")
    row = {"argv": [str(value) for value in argv], "exit_code": result.returncode,
           "elapsed_s": round(time.monotonic() - started, 4),
           "stdout": _pin_file({"file": str(stdout), "sha256": R._file_hash(stdout)}),
           "stderr": _pin_file({"file": str(stderr), "sha256": R._file_hash(stderr)})}
    if result.returncode:
        raise ValueError(f"compiler adapter command failed: {name}; evidence preserved")
    return row


ABI_PROBE = r'''#include <string>
#include <vector>
#include <cstdint>
#define Q2(value) #value
#define Q(value) Q2(value)
extern "C" std::uint64_t anyuse_string_size() { return sizeof(std::string); }
extern "C" std::uint64_t anyuse_string_alignment() { return alignof(std::string); }
extern "C" std::uint64_t anyuse_vector_size() { return sizeof(std::vector<double>); }
extern "C" std::uint64_t anyuse_vector_alignment() { return alignof(std::vector<double>); }
extern "C" std::uint64_t anyuse_libcpp_version() { return _LIBCPP_VERSION; }
extern "C" std::uint64_t anyuse_libcpp_abi_version() { return _LIBCPP_ABI_VERSION; }
extern "C" const char *anyuse_libcpp_namespace() { return Q(_LIBCPP_ABI_NAMESPACE); }
'''


def _triple(ir):
    match = re.search(r'^target triple = "([^"]+)"$', ir, re.MULTILINE)
    if match is None or re.fullmatch(r"(?:arm64|aarch64)-apple-macosx?13\.0(?:\.0)?", match[1]) is None:
        raise ValueError("LLVM evidence has an unsupported target/deployment/PAC family")
    return match[1].replace("aarch64-", "arm64-")


def _arguments(line, start):
    depth, position, quoted, escaped = 1, start, False, False
    while position < len(line):
        char = line[position]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        position += 1
    if depth:
        raise ValueError("LLVM prototype is incomplete")
    text = line[start:position]
    return [] if not text.strip() else re.split(r",\s*(?![^()]*\))", text)


def _physical(line, name=None):
    if name is None:
        line = re.sub(r"^\s*%[-A-Za-z$._0-9]{1,128}\s*=\s*", "", line, count=1)
        match = re.search(r"\b(?:call|invoke)\b.*?\bdouble\s+(%[-a-zA-Z$._0-9]+)\(", line)
    else:
        escaped = re.escape(name)
        match = re.search(rf"^define\b.*?\bdouble\s+@(?:{escaped}|\"{escaped}\")\(", line)
    if match is None:
        return None
    prefix = line[:match.end()]
    args = _arguments(line, match.end())
    types = []
    for argument in args:
        type_match = re.match(r"\s*(double|i1|ptr)\b", argument)
        if type_match is None:
            return None
        attributes = argument.split("%", 1)[0]
        if re.search(r"\b(?:sret|swiftasync|swifterror|inalloca|byval|byref|nest|inreg|preallocated|swiftcall)\b", attributes) or "..." in argument:
            return None
        types.append({"type": type_match[1], "swiftself": re.search(r"\bswiftself\b", attributes) is not None})
    before_return = prefix.rsplit("double", 1)[0]
    allowed = {"define", "call", "invoke", "internal", "private", "external", "weak", "weak_odr", "linkonce", "linkonce_odr",
               "available_externally", "extern_weak", "dso_local", "dso_preemptable", "hidden", "protected", "default",
               "dllimport", "dllexport", "unnamed_addr", "local_unnamed_addr", "noundef", "tail", "musttail", "notail",
               "fast", "nnan", "ninf", "nsz", "arcp", "contract", "afn", "reassoc", "ccc", "swiftcc"}
    if set(before_return.split()) - allowed:
        return None
    conventions = re.findall(r"\b(?:swiftcc|ccc)\b", before_return)
    if len(conventions) > 1:
        return None
    return {"convention": conventions[0] if conventions else "ccc", "return_type": "double", "arguments": types}


def _owned_string_physical(line, name=None):
    """Closed plain-C++ sret shape; optimizer pointer facts are not ABI types."""
    line = re.sub(r"^\s*%[-A-Za-z$._0-9]{1,128}\s*=\s*", "", line, count=1)
    if name is None:
        match = re.search(r"\b(?:call|invoke)\b.*?\bvoid\s+(%[-A-Za-z$._0-9]+)\(", line)
    else:
        escaped = re.escape(name)
        match = re.search(rf"^define\b.*?\bvoid\s+@(?:{escaped}|\"{escaped}\")\(", line)
    if match is None:
        return None
    before_return = re.split(r"\bvoid\b", line[:match.end()], maxsplit=1)[0]
    allowed = {"define", "call", "invoke", "internal", "private", "external", "weak", "weak_odr", "linkonce", "linkonce_odr",
               "available_externally", "extern_weak", "dso_local", "dso_preemptable", "hidden", "protected", "default",
               "dllimport", "dllexport", "unnamed_addr", "local_unnamed_addr", "tail", "musttail", "notail", "ccc"}
    if set(before_return.split()) - allowed or len(re.findall(r"\bccc\b", before_return)) > 1:
        return None
    arguments = _arguments(line, match.end())
    if len(arguments) != 3:
        return None
    physical = []
    for index, argument in enumerate(arguments):
        value = re.search(r'\s+%(?:"[^"\n]+"|[-A-Za-z$._0-9]+)\s*$', argument)
        if value is None:
            return None
        declaration = argument[:value.start()].strip()
        if declaration != "ptr" and not declaration.startswith("ptr "):
            return None
        attributes = declaration[3:].strip()
        if re.search(r"\b(?:swiftself|swiftasync|swifterror|inalloca|byval|byref|nest|inreg|preallocated|addrspace|signext|zeroext)\b", attributes):
            return None
        sret = re.findall(r'sret\((%"[^"\n]+"|%[-A-Za-z$._0-9:]+)\)', attributes)
        alignments = re.findall(r"\balign\s+([0-9]+)\b", attributes)
        cleaned = re.sub(r'sret\((%"[^"\n]+"|%[-A-Za-z$._0-9:]+)\)', "", attributes)
        cleaned = re.sub(r"\b(?:dereferenceable|dereferenceable_or_null)\([0-9]+\)", "", cleaned)
        cleaned = re.sub(r"\bcaptures\(none\)", "", cleaned)
        cleaned = re.sub(r"\balign\s+[0-9]+\b", "", cleaned)
        if set(cleaned.split()) - {"noundef", "noalias", "nonnull", "nocapture", "readonly", "readnone", "writeonly",
                                   "writable", "dead_on_unwind", "nofree"}:
            return None
        if index == 0:
            if len(sret) != 1 or len(alignments) != 1:
                return None
            typename = sret[0][1:].strip('"')
            if re.fullmatch(r"class\.std::__1::basic_string(?:\.[0-9]+)?", typename) is None:
                return None
            physical.append({"type": "ptr", "sret": {"llvm_type": typename, "alignment": int(alignments[0])}})
        else:
            if sret or len(alignments) > 1:
                return None
            physical.append({"type": "ptr"})
    return {"convention": "ccc", "return_type": "void", "arguments": physical}


def lowering_check(original_ir, helper_ir, function, family, *, string_layout=None):
    """Compare actual original definition and helper indirect-call lowerings."""
    original_triple, helper_triple = _triple(original_ir), _triple(helper_ir)
    if original_triple != helper_triple:
        raise ValueError("original and helper LLVM triples differ")
    if family == "cxx_owned_string_bytes":
        _keys(string_layout, {"string_size", "string_alignment"})
        size, alignment = string_layout["string_size"], string_layout["string_alignment"]
        if (type(size) is not int or type(alignment) is not int or not 1 <= size <= 1024 or
                not 1 <= alignment <= 16 or alignment & (alignment - 1) or size % alignment):
            raise ValueError("owned string return requires the independently observed exact compiler layout")
        originals = [_owned_string_physical(line, function) for line in original_ir.splitlines() if line.startswith("define ")]
        originals = [value for value in originals if value is not None]
        calls = [_owned_string_physical(line) for line in helper_ir.splitlines()]
        calls = [value for value in calls if value is not None]
        if (len(originals) != 1 or len(calls) != 1 or originals[0] != calls[0] or
                originals[0]["arguments"][0]["sret"]["alignment"] != alignment):
            raise ValueError("compiler IR does not establish the exact original/helper owned-string sret ABI lowering")
        return {"status": "passed", "target_triple": original_triple, "original_function": function,
                "original": originals[0], "indirect_helper_call": calls[0], "string_layout": copy.deepcopy(string_layout),
                "prototype_automatically_recovered": False, "instruction_boundary_proven": False}
    originals = [_physical(line, function) for line in original_ir.splitlines() if line.startswith("define ")]
    originals = [value for value in originals if value is not None]
    expected = ({"convention": "swiftcc", "return_type": "double", "arguments": [
        {"type": "double", "swiftself": False}, {"type": "i1", "swiftself": False}, {"type": "ptr", "swiftself": True}]}
        if family == "swift_double_bool_context" else
        {"convention": "ccc", "return_type": "double", "arguments": [{"type": "ptr", "swiftself": False}] * 3})
    calls = [_physical(line) for line in helper_ir.splitlines()]
    calls = [value for value in calls if value is not None and value == expected]
    if len(originals) != 1 or originals[0] != expected or len(calls) != 1:
        raise ValueError("compiler IR does not establish the exact original/helper private ABI lowering")
    return {"status": "passed", "target_triple": original_triple, "original_function": function,
            "original": originals[0], "indirect_helper_call": calls[0],
            "prototype_automatically_recovered": False, "instruction_boundary_proven": False}


def _probe_values(ir, macros):
    values = {}
    for name in ("string_size", "string_alignment", "vector_size", "vector_alignment", "libcpp_version", "libcpp_abi_version"):
        match = re.search(rf"define\b[^\n]*@anyuse_{name}\([^)]*\)[^{{]*\{{([^}}]*)\}}", ir)
        returned = re.search(r"\bret i64 ([0-9]+)\b", match[1]) if match else None
        if returned is None:
            raise ValueError("compiler layout probe has no unambiguous constant")
        values[name] = int(returned[1])
    namespace = re.search(r'define\b[^\n]*@anyuse_libcpp_namespace\([^)]*\)[^{]*\{([^}]*)\}', ir)
    global_ref = re.search(r"\bret ptr @([A-Za-z0-9$._-]+)", namespace[1]) if namespace else None
    literal = re.search(rf'@{re.escape(global_ref[1])} = [^\n]*c"([^"]*)"', ir) if global_ref else None
    if literal is None or literal[1] != r"__1\00":
        raise ValueError("libc++ ABI namespace is not the reviewed __1 family")
    selected = {}
    for line in macros.splitlines():
        match = re.fullmatch(r"#define (_LIBCPP_ABI_[A-Za-z0-9_]+|_LIBCPP_VERSION)(?:\s+(.*))?", line)
        if match:
            selected[match[1]] = match[2] or ""
    if (ABI_DEFINES_DENIED & set(selected) or any("TRIVIAL_ABI" in name for name in selected) or
            values["libcpp_abi_version"] != 1 or values["libcpp_version"] <= 0):
        raise ValueError("libc++ trivial ABI family is unsupported")
    if any(not 1 <= values[name] <= 1024 for name in ("string_size", "string_alignment", "vector_size", "vector_alignment")):
        raise ValueError("compiler layout probe exceeds the bounded ownership family")
    return {"namespace": "__1", "layout": values, "abi_defines": selected,
            "string_layout": "alternate" if "_LIBCPP_ABI_ALTERNATE_STRING_LAYOUT" in selected else "standard"}


def _folder(out):
    folder = Path(out)
    if not folder.is_absolute() or folder.exists() or folder.parent.resolve(strict=True) != folder.parent:
        raise ValueError("adapter evidence requires a new canonical outside-app directory")
    if any(parent.suffix.lower() == ".app" for parent in [folder, *folder.parents]):
        raise ValueError("adapter compiler never writes into an app")
    folder.mkdir(mode=0o700)
    return folder


def inspect_toolchain(out, *, timeout=120):
    R.assert_guest()
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("compiler budget must be 1–120 seconds")
    policy, pins = _policy()
    folder = _folder(out)
    probe = folder / "abi-probe.cpp"
    probe.write_text(ABI_PROBE)
    ir = folder / "abi-probe.ll"
    commands = [_run([policy["compiler"]["file"], "--version"], folder, "version", timeout),
                _run(_argv(policy, probe) + ["-S", "-emit-llvm", "-o", str(ir)], folder, "layout", timeout),
                _run(_argv(policy, probe) + ["-dM", "-E"], folder, "macros", timeout)]
    version = (folder / "version.stdout").read_text().strip()
    if not version or ir.stat().st_size > MAX_IR:
        raise ValueError("compiler identity or layout evidence is incomplete")
    macro_text = (folder / "macros.stdout").read_text()
    identity = {"compiler": {**pins["compiler"], "version": version},
                "target_triple": _triple(ir.read_text()), "requested_target_triple": policy["target_triple"],
                "sdk": copy.deepcopy(policy["sdk"]),
                "libcxx": {"include": policy["libcxx"]["include"], "sha256": policy["libcxx"]["sha256"],
                           **_probe_values(ir.read_text(), macro_text)}}
    after_policy, after_pins = _policy()
    if policy != after_policy or pins != after_pins:
        raise ValueError("compiler/SDK/header policy changed during inspection")
    observation = {"format": TOOLCHAIN, "status": "observed", "identity": identity,
                   "policy": pins["policy"], "commands": commands, "artifact_pins": pins,
                   "runtime_verified": False, "evidence_only": True}
    R.write_json(folder / "toolchain.json", observation)
    return observation


def _original_evidence(request, toolchain):
    pins = {}
    for role in ("source", "ir", "build"):
        claim = request["evidence"][role]
        pin = _pin_file(claim, MAX_IR if role == "ir" else MAX_FILE)
        pins[role] = pin
    declaration = request["original"]["signature_evidence"]
    pins["declaration"] = _pin_file(declaration)
    selected = B._json_pointer(R.load_json(declaration["file"]), declaration["pointer"])
    if selected != original_contract(request["original"]):
        raise ValueError("original declaration differs from its exact entry and ABI")
    selected = B._json_pointer(R.load_json(pins["build"]["file"]), request["evidence"]["build"]["pointer"])
    _keys(selected, {"format", "entry", "source", "ir", "compiler", "target_triple", "sdk"}, {"libcxx"})
    if selected["format"] != ORIGINAL_BUILD or selected["entry"] != _entry(request["original"]):
        raise ValueError("original build receipt does not identify this private entry")
    if (selected["source"] != request["evidence"]["source"] or selected["ir"] != request["evidence"]["ir"] or
            selected["target_triple"] != toolchain["identity"]["target_triple"] or
            selected["sdk"] != toolchain["identity"]["sdk"]):
        raise ValueError("original source/IR/target/SDK differs from compiler evidence")
    _keys(selected["compiler"], {"file", "sha256", "version"})
    pins["compiler"] = _pin_file(selected["compiler"])
    if not _text(selected["compiler"]["version"], 16384):
        raise ValueError("original compiler identity is incomplete")
    if request["family"] in {"cxx_owned_string_vector_double", "cxx_owned_string_bytes"}:
        observed = toolchain["identity"]
        if ({**pins["compiler"], "version": selected["compiler"]["version"]} != observed["compiler"] or
                selected.get("libcxx") != observed["libcxx"]):
            raise ValueError("original C++ compiler/header/macro/layout ABI differs from helper")
    images = B._macho_images(Path(request["original"]["module"]["path"]))
    if not any(image["cpu_type"] == 0x0100000C and image["cpu_subtype"] & 0xFFFFFF == 0 for image in images):
        raise ValueError("original entry has no supported plain arm64 image; arm64e/PAC is unproved")
    pins["module"] = _pin_file({"file": request["original"]["module"]["path"],
                              "sha256": request["original"]["module"]["sha256"]})
    return pins


def build(request, out, *, timeout=120):
    R.assert_guest()  # Before any path, installed tool, evidence or output access.
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("compiler budget must be 1–120 seconds")
    request = validate_request(request)
    generator_file = Path(__file__).resolve(strict=True)
    generator_pin = _pin_file({"file": str(generator_file), "sha256": R._file_hash(generator_file)})
    folder = _folder(out)
    observation = inspect_toolchain(folder / "toolchain", timeout=timeout)
    original_pins = _original_evidence(request, observation)
    policy, before_pins = _policy()
    if before_pins != observation["artifact_pins"]:
        raise ValueError("compiler/header/SDK policy changed after its observed ABI probe")
    source, ir, library = folder / "adapter.cpp", folder / "adapter.ll", folder / "adapter.dylib"
    source.write_text(render_source(request))
    commands = []
    try:
        commands.append(_run(_argv(policy, source) + ["-S", "-emit-llvm", "-o", str(ir)], folder, "emit-ir", timeout))
        if ir.stat().st_size > MAX_IR:
            raise ValueError("generated compiler IR exceeds its byte budget")
        layout = observation["identity"]["libcxx"]["layout"]
        lowering = lowering_check(Path(original_pins["ir"]["file"]).read_text(), ir.read_text(),
                                  request["evidence"]["ir"]["function"], request["family"],
                                  string_layout={key: layout[key] for key in ("string_size", "string_alignment")})
        commands.append(_run(_argv(policy, source) + ["-dynamiclib", "-lc++", "-Xlinker", "-install_name", "-Xlinker", str(library),
                                                      "-o", str(library)], folder, "compile", timeout))
        images = B._macho_images(library)
        if len(images) != 1 or images[0]["cpu_type"] != 0x0100000C or images[0]["cpu_subtype"] & 0xFFFFFF != 0:
            raise ValueError("generated helper is not the reviewed plain arm64 image")
        after_policy, after_pins = _policy()
        if policy != after_policy or before_pins != after_pins or _original_evidence(request, observation) != original_pins:
            raise ValueError("native compiler/original evidence changed during generation")
        if _pin_file(generator_pin) != generator_pin:
            raise ValueError("fixed generator/verifier source changed during compilation")
        helper = {"module": {"path": str(library), "sha256": R._file_hash(library)}, "symbol": SYMBOL}
        outer = outer_contract(request)
        R.write_json(folder / "adapter-abi.json", {"outer": outer})
        contract = {"format": "anyuse.rea-compiler-adapter/1", "family": request["family"], "helper": helper,
                    "outer": outer, "original": original_contract(request["original"]),
                    "completion": completion_contract(request)}
        artifacts = {}
        def add(role, pin):
            if pin["file"] in artifacts:
                if any(artifacts[pin["file"]][key] != pin[key] for key in ("sha256", "bytes")):
                    raise ValueError("adapter provenance has conflicting artifact identities")
                artifacts[pin["file"]]["roles"] = sorted(set(artifacts[pin["file"]]["roles"]) | {role})
            else:
                artifacts[pin["file"]] = {**pin, "roles": [role]}
        for role, pin in original_pins.items():
            add("original_" + role, pin)
        add("generator_verifier_source", generator_pin)
        for role in ("compiler", "sdk_settings", "policy"):
            add("toolchain_" + role, before_pins[role])
        for pin in before_pins["headers"].values():
            add("libcxx_header", pin)
        for role, path in (("generated_source", source), ("generated_ir", ir), ("generated_helper", library),
                           ("outer_declaration", folder / "adapter-abi.json"), ("toolchain_observation", folder / "toolchain/toolchain.json")):
            add(role, _pin_file({"file": str(path), "sha256": R._file_hash(path)}))
        for command in [*observation["commands"], *commands]:
            add("compiler_stdout", command["stdout"])
            add("compiler_stderr", command["stderr"])
        for path in (folder / "toolchain/abi-probe.cpp", folder / "toolchain/abi-probe.ll"):
            add("compiler_layout_probe", _pin_file({"file": str(path), "sha256": R._file_hash(path)}))
        receipt = {"format": BUILD, "status": "built", "family": request["family"], "request": request,
                   "request_sha256": R.digest(request), "contract": contract, "toolchain": observation["identity"],
                   "lowering_check": lowering, "commands": commands, "artifacts": list(artifacts.values()),
                   "loaded_into_target": False, "runtime_verified": False, "verified": False, "evidence_only": True}
        R.write_json(folder / "build.json", receipt)
        return receipt
    except BaseException as exc:
        R.write_json(folder / "failure.json", {"format": BUILD, "status": "incomplete",
                     "request_sha256": R.digest(request), "commands": commands,
                     "error": {"type": type(exc).__name__, "message": str(exc)[:4096]},
                     "loaded_into_target": False, "runtime_verified": False, "evidence_only": True})
        raise


def resources(evidence):
    """Revalidate the closed transitive artifact set before load/mapping/replay."""
    R.assert_guest()
    _keys(evidence, {"kind", "file", "sha256", "pointer"})
    _claim({key: evidence[key] for key in ("file", "sha256", "pointer")}, pointer=True)
    manifest = _pin_file(evidence)
    receipt = R.load_json(evidence["file"])
    if (receipt.get("format") != BUILD or receipt.get("status") != "built" or receipt.get("verified") is not False or
            receipt.get("runtime_verified") is not False or receipt.get("loaded_into_target") is not False or
            evidence["pointer"] != "/contract"):
        raise ValueError("helper requires a fixed-family compiler build receipt, not asserted ABI JSON")
    request = validate_request(receipt["request"])
    if (receipt.get("request_sha256") != R.digest(request) or receipt.get("family") != request["family"] or
            receipt.get("lowering_check", {}).get("status") != "passed"):
        raise ValueError("compiler adapter provenance is incomplete")
    rows = receipt.get("artifacts")
    if not isinstance(rows, list) or not 8 <= len(rows) <= 8192:
        raise ValueError("compiler adapter lacks a bounded transitive artifact set")
    pinned = {manifest["file"]: manifest}
    roles = {}
    for row in rows:
        _keys(row, {"file", "sha256", "bytes", "roles"})
        _claim({key: row[key] for key in ("file", "sha256")})
        if (not isinstance(row["roles"], list) or not 1 <= len(row["roles"]) <= 8 or
                any(not _text(role, 128) for role in row["roles"]) or len(set(row["roles"])) != len(row["roles"]) or
                row["file"] in pinned or type(row["bytes"]) is not int):
            raise ValueError("compiler provenance contains ambiguous or invalid artifacts")
        pin = _pin_file(row)
        if pin["bytes"] != row["bytes"]:
            raise ValueError("compiler artifact extent changed")
        pinned[row["file"]] = pin
        for role in row["roles"]:
            roles.setdefault(role, []).append(pin)
    for role in ("generated_source", "generated_ir", "generated_helper", "outer_declaration", "toolchain_observation",
                 "original_source", "original_ir", "original_build", "original_declaration", "original_module", "original_compiler",
                 "toolchain_compiler", "toolchain_sdk_settings", "toolchain_policy", "generator_verifier_source"):
        if len(roles.get(role, [])) != 1:
            raise ValueError("compiler provenance lacks a unique mandatory artifact")
    if roles["generator_verifier_source"][0]["file"] != str(Path(__file__).resolve(strict=True)):
        raise ValueError("compiler provenance belongs to a different fixed generator/verifier")
    if Path(roles["generated_source"][0]["file"]).read_text() != render_source(request):
        raise ValueError("helper source is not the fixed data-driven template")
    observed = R.load_json(roles["toolchain_observation"][0]["file"])
    if observed.get("format") != TOOLCHAIN or observed.get("identity") != receipt.get("toolchain"):
        raise ValueError("helper toolchain observation changed")
    observed_pins = observed.get("artifact_pins")
    if not isinstance(observed_pins, dict) or set(observed_pins) != {"compiler", "sdk_settings", "policy", "headers"}:
        raise ValueError("toolchain observation lacks the exact compiler/header/SDK pin set")
    for name in ("compiler", "sdk_settings", "policy"):
        if roles["toolchain_" + name] != [observed_pins[name]]:
            raise ValueError("compiler manifest does not pin the observed toolchain identity")
    headers = observed_pins["headers"]
    if (not isinstance(headers, dict) or {pin["file"]: pin for pin in roles.get("libcxx_header", [])} != headers or
            any(pin != pinned.get(path) for path, pin in headers.items())):
        raise ValueError("compiler manifest omits or changes observed libc++ headers")
    probe_rows = roles.get("compiler_layout_probe", [])
    probe_ir = [pin for pin in probe_rows if pin["file"].endswith("/abi-probe.ll")]
    if len(probe_ir) != 1:
        raise ValueError("compiler provenance lacks the actual layout probe IR")
    macro_command = next((command for command in observed.get("commands", []) if "-dM" in command.get("argv", [])), None)
    if macro_command is None or macro_command.get("stdout") != pinned.get(macro_command.get("stdout", {}).get("file")):
        raise ValueError("compiler provenance lacks the actual macro probe bytes")
    probe_values = _probe_values(Path(probe_ir[0]["file"]).read_text(), Path(macro_command["stdout"]["file"]).read_text())
    claimed_libcxx = observed["identity"].get("libcxx")
    if not isinstance(claimed_libcxx, dict) or any(claimed_libcxx.get(key) != value for key, value in probe_values.items()):
        raise ValueError("recorded libc++ ABI differs from actual compiler probe evidence")
    if _original_evidence(request, observed) != {key.removeprefix("original_"): rows[0] for key, rows in roles.items()
                                                  if key.startswith("original_")}:
        raise ValueError("original provenance is not bound to current artifacts")
    layout = observed["identity"]["libcxx"]["layout"]
    lowering = lowering_check(Path(roles["original_ir"][0]["file"]).read_text(),
                              Path(roles["generated_ir"][0]["file"]).read_text(),
                              request["evidence"]["ir"]["function"], request["family"],
                              string_layout={key: layout[key] for key in ("string_size", "string_alignment")})
    if lowering != receipt["lowering_check"]:
        raise ValueError("compiler lowering evidence changed")
    helper = roles["generated_helper"][0]
    expected = {"format": "anyuse.rea-compiler-adapter/1", "family": request["family"],
                "helper": {"module": {"path": helper["file"], "sha256": helper["sha256"]}, "symbol": SYMBOL},
                "outer": outer_contract(request), "original": original_contract(request["original"]),
                "completion": completion_contract(request)}
    if receipt.get("contract") != expected:
        raise ValueError("helper manifest ABI/completion/entry differs from compiler provenance")
    return pinned
