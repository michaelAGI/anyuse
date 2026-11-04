"""VM-only runtime access inventory and explicit, non-actuating probes.

This planner reports candidate routes with their provenance. It never launches
an app, changes its signature or permissions, installs a plug-in, or retries a
denied attachment through another backend. A predicted route is not tested
access and runtime access is not proof of an automation task.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import plistlib
import re
import struct
import subprocess

from . import rea_mapping as R
from . import rea_runtime_bridge as B

ACCESS = "anyuse.rea-access/1"
ENTITLEMENTS = (
    "com.apple.security.get-task-allow",
    "com.apple.security.cs.debugger",
    "com.apple.security.cs.allow-dyld-environment-variables",
    "com.apple.security.cs.disable-library-validation",
    "com.apple.security.cs.allow-unsigned-executable-memory",
    "com.apple.security.app-sandbox",
)


def classify_probe(receipt: dict) -> dict:
    """Classify observed evidence without claiming an unobserved root cause."""
    result = receipt.get("result") or {}
    error = receipt.get("error") or result.get("error") or {}
    message = str(error.get("message", ""))[:4096]
    name = str(error.get("type", ""))
    text = (name + " " + message).lower()
    inspector = receipt.get("format") == "anyuse.rea-inspector/1"
    cleanup = receipt.get("cleanup", {}) if inspector else result.get("cleanup") or {}
    uncertain = bool(receipt.get("state_uncertain") or result.get("native_state_uncertain"))
    review = bool(receipt.get("resource_review_required") or result.get("resource_review_required") or uncertain)
    if inspector and receipt.get("status") == "observed" and cleanup != {
            "object_group_released": True, "page_socket_closed": True, "browser_socket_closed": True,
            "requested_evaluations_completed": True, "remote_execution_cancelled": False,
            "local_live_ids_discarded": True}:
        # A claimed observation cannot hide an unacknowledged inspector release.
        review = True
    if review:
        category = "resource_review_required"
    elif receipt.get("status") == "observed":
        category = "access_observed"
    elif "unambiguous running target" in text or "processnotfounderror" in text:
        category = "target_missing_or_ambiguous"
    elif "identity does not match" in text or "owned by this guest user" in text:
        category = "target_identity_denied"
    elif "permissiondenied" in text or "not permitted" in text or "access denied" in text:
        category = "authorization_denied"
    elif "thread_create" in text or "mach thread" in text:
        category = "injection_failed"
    elif "connection refused" in text or "unable to connect" in text or "connectionclosed" in text:
        category = "endpoint_unavailable"
    elif "authentication" in text or "unauthorized" in text or "token" in text:
        category = "authentication_or_configuration"
    elif "library validation" in text or "different team" in text or "code signature" in text:
        category = "code_loading_denied"
    elif "architecture" in text or "unsupported cpu" in text or "unsupported abi" in text:
        category = "unsupported_architecture_or_abi"
    elif "not running" in text or "no running" in text or "target process" in text:
        category = "target_unavailable"
    else:
        category = "unclassified_failure"
    return {"category": category, "observed_status": receipt.get("status"),
            "error_type": name or None, "message": message or None,
            "attached": result.get("attached"), "cleanup": cleanup,
            "state_uncertain": uncertain, "resource_review_required": review,
            "inferred_root_cause": False}


def _command(argv: list[str]) -> dict:
    result = subprocess.run(argv, capture_output=True, timeout=10, check=False)
    return {"exit_code": result.returncode,
            "stdout": result.stdout[:65536].decode("utf-8", errors="replace"),
            "stderr": result.stderr[:16384].decode("utf-8", errors="replace")}


def _architectures(path: Path) -> list[dict]:
    with path.open("rb") as stream:
        header = stream.read(4096)
    magic = header[:4]
    names = {0x0100000C: "arm64", 0x01000007: "x86_64", 12: "arm", 7: "x86"}
    rows = []
    fat = {b"\xca\xfe\xba\xbe": (">", 20), b"\xbe\xba\xfe\xca": ("<", 20),
           b"\xca\xfe\xba\xbf": (">", 32), b"\xbf\xba\xfe\xca": ("<", 32)}
    thin = {b"\xcf\xfa\xed\xfe": "<", b"\xce\xfa\xed\xfe": "<",
            b"\xfe\xed\xfa\xcf": ">", b"\xfe\xed\xfa\xce": ">"}
    if magic in fat:
        endian, size = fat[magic]
        count = struct.unpack_from(endian + "I", header, 4)[0]
        if not 1 <= count <= 16 or 8 + count * size > len(header):
            raise ValueError("Mach-O architecture table exceeds the inventory budget")
        pairs = [struct.unpack_from(endian + "II", header, 8 + i * size) for i in range(count)]
    elif magic in thin:
        pairs = [struct.unpack_from(thin[magic] + "II", header, 4)]
    else:
        return []
    for cpu, subtype in pairs:
        arm64e = cpu == 0x0100000C and subtype & 0xFFFFFF == 2
        row = {"architecture": "arm64e" if arm64e else names.get(cpu, "unknown"),
               "cpu_type": cpu, "cpu_subtype": subtype}
        if arm64e and subtype & 0x80000000:
            row["pointer_auth_abi_version"] = (subtype >> 24) & 0x7F
        rows.append(row)
    return rows


def _plugin_evidence(value, target: dict):
    if value is None:
        return None
    required = {"kind", "documentation", "observed_host_bundle_id", "minimum_version", "entrypoint"}
    if (not isinstance(value, dict) or set(value) != required or value["kind"] != "native_plugin" or
            value["observed_host_bundle_id"] != target["bundle_id"] or
            not isinstance(value["documentation"], str) or not value["documentation"].startswith("https://") or
            any(not isinstance(value[k], str) or not value[k] or "\0" in value[k] or len(value[k]) > 4096
                for k in required)):
        raise ValueError("plugin evidence needs an exact host identity and bounded documented entrypoint")
    return {**value, "provenance": "caller_supplied_documentation",
            "native_code_loaded_in_target": False, "verified": False}


def inspect(bundle, out, *, probe=None, timeout=30, plugin_evidence=None):
    R.assert_guest()  # Before path discovery, any read/write or native command.
    if probe not in {None, "local", "gadget", "inspector"}:
        raise ValueError("select one explicit access probe, never an automatic fallback")
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("access probe deadline must be 1–60 seconds")
    app = Path(bundle).resolve(strict=True)
    folder = Path(out).resolve()
    if folder.is_relative_to(app):
        raise ValueError("access evidence must be outside the target app bundle")
    if folder.exists():
        raise ValueError("access evidence directory already exists")
    target = R.fingerprint(app)
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable_name = plist.get("CFBundleExecutable")
    if (not isinstance(executable_name, str) or Path(executable_name).name != executable_name or
            not executable_name or "\0" in executable_name):
        raise ValueError("bundle has no exact executable name")
    executable = (app / "Contents/MacOS" / executable_name).resolve(strict=True)
    if not executable.is_relative_to(app):
        raise ValueError("executable escapes the selected app")
    plugin = _plugin_evidence(plugin_evidence, target)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    signature = _command(["/usr/bin/codesign", "--display", "--verbose=4", str(app)])
    raw_entitlements = _command(["/usr/bin/codesign", "--display", "--entitlements", ":-", str(app)])
    selected = {}
    parsed = False
    text = raw_entitlements["stdout"]
    if "<?xml" in text:
        try:
            data = plistlib.loads(text[text.index("<?xml"):].encode())
            selected = {key: data[key] for key in ENTITLEMENTS if key in data and type(data[key]) is bool}
            parsed = True
        except (ValueError, plistlib.InvalidFileException):
            pass
    flags = re.search(r"^.*flags=([^\n]+)", signature["stderr"], re.MULTILINE)
    team = re.search(r"^TeamIdentifier=(.+)$", signature["stderr"], re.MULTILINE)
    inventory = {"executable": str(executable), "architectures": _architectures(executable),
                 "signature_exit_code": signature["exit_code"], "signing_flags": flags.group(1) if flags else None,
                 "team_identifier": team.group(1) if team else None,
                 "selected_entitlements": selected, "entitlements_parsed": parsed,
                 "declared_document_types": len(plist.get("CFBundleDocumentTypes", [])),
                 "declared_url_types": len(plist.get("CFBundleURLTypes", [])),
                 "nested_plugin_bundles": sorted(str(p.relative_to(app)) for p in (app / "Contents/PlugIns").glob("*.plugin")),
                 "facts_are_access_predictions": False}
    routes = [
        {"id": "local", "mechanism": "explicit_local_attachment", "status": "unprobed",
         "metadata_implies_attach_permission": False},
        {"id": "gadget", "mechanism": "explicit_authenticated_in_process_endpoint", "status": "unprobed",
         "requires": ["library already loaded in actual target", "pinned protected endpoint configuration"],
         "changes_applied": False},
        {"id": "inspector", "mechanism": "explicit_owned_loopback_renderer_inspector", "status": "unprobed",
         "requires": ["inspector already enabled in the actual target", "owned canonical 0600 operator configuration",
                      "exact app fingerprint, owned listener and default renderer context"],
         "metadata_implies_access_permission": False, "changes_applied": False},
        {"id": "development_copy", "mechanism": "separate_owned_instrumented_copy", "status": "candidate_only",
         "requires": ["compatible executable architecture", "app functions with development identity", "verified unchanged original"],
         "stock_app_coverage_credit": 0, "changes_applied": False},
    ]
    if plugin:
        routes.insert(2, {"id": "native_plugin", "mechanism": "documented_native_extension_entrypoint",
                         "status": "declared_not_verified", "evidence": plugin,
                         "requires": ["supported host/version/SDK", "normal code-loading approval", "actual host PID verification"],
                         "changes_applied": False})
    result = {"format": ACCESS, "status": "inspected", "evidence_only": True, "actuated": False,
              "proof_created": False, "task_result_verified": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "target": target,
              "inventory": inventory, "routes": routes, "probe": None,
              "automatic_fallback": False, "security_or_signature_changes": False,
              "resource_review_required": False, "receipt": str(folder / "access.json")}
    if probe is not None:
        probe_folder = folder / ("probe-" + probe)
        if probe == "inspector":
            from . import rea_inspector_bridge as I
            # The provider reads the explicit operator configuration. This uses
            # DOM protocol metadata only: no global lookup or app evaluation.
            runtime = I.collect(str(app), probe_folder, {"operations": [
                {"op": "dom_query", "selector": "body", "max_nodes": 1}
            ]}, timeout=timeout)
            probe_receipt = str(probe_folder / "inspector.json")
        else:
            runtime = B.collect(str(app), probe_folder, {"operations": [
                {"op": "classes", "owner": "app", "query": "", "limit": 1}
            ]}, timeout=timeout, backend=probe)
            probe_receipt = runtime.get("receipt")
        classification = classify_probe(runtime)
        result["probe"] = {"backend": probe, "provider": "inspector" if probe == "inspector" else "native",
                           "receipt": probe_receipt,
                           "runtime": runtime, "classification": classification}
        next(route for route in routes if route["id"] == probe)["status"] = classification["category"]
        result["resource_review_required"] = classification["resource_review_required"]
    result["target_unchanged"] = R.fingerprint(app) == target
    if not result["target_unchanged"]:
        result["status"] = "incomplete"
    R.write_json(folder / "access.json", result)
    return result
