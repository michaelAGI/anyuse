"""Inspector protocol/lifecycle and real fixed V8-script checks; guest only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_inspector_bridge as I, rea_mapping as R

METHOD = "applyValue(value, enabled) { return 'state:' + value + ':' + enabled; }"
METHOD_SHA = hashlib.sha256(METHOD.encode()).hexdigest()
RECIPE = {"root": {"kind": "global", "name": "model"}, "steps": []}
ANCHOR_SOURCE = "function retainedAnchor() { return capturedStore; }"
ANCHOR_SHA = hashlib.sha256(ANCHOR_SOURCE.encode()).hexdigest()
ANCHOR_RECIPE = {"root": RECIPE["root"],
                 "steps": [{"kind": "property", "name": "anchor", "access": "data"}]}
CTOR_SOURCE = "class FixtureStore {}"
CTOR_SHA = hashlib.sha256(CTOR_SOURCE.encode()).hexdigest()
PROTOTYPE_RECIPE = {"root": RECIPE["root"],
                    "steps": [{"kind": "property", "name": "storePrototype", "access": "data"}]}


class ProtocolServer:
    """Scripted CDP server exercising the real request/response dispatcher.

    It does not reimplement the fixed JavaScript interpreter: those functions
    run against an independent V8 process in the last test below.
    """
    def __init__(self, bundle):
        self.bundle = bundle
        self.endpoint = "http://127.0.0.1:28123"
        self.url = (bundle / "Contents/Resources/renderer.html").as_uri()
        self.browser_url = "ws://127.0.0.1:28123/devtools/browser/one"
        self.page_url = "ws://127.0.0.1:28123/devtools/page/one"
        self.target = {"id": "one", "type": "page", "url": self.url, "webSocketDebuggerUrl": self.page_url}
        self.connections, self.requests, self.dispatches = [], [], []
        self.fault = None
        self.source = METHOD
        self.renderer_id = 22334
        self.completed_operations = 0
        self.frame_override = None
        self.exception_details = None
        self.dom_matches = [7101, 7102]
        self.dom_nodes = {
            7101: {"nodeId": 0, "backendNodeId": 8101, "nodeType": 1, "nodeName": "DIV", "localName": "div",
                   "attributes": ["id", "app", "data-v-app", ""], "childNodeCount": 1},
            7102: {"nodeId": 0, "backendNodeId": 8102, "nodeType": 1, "nodeName": "DIV", "localName": "div",
                   "attributes": ["id", "app"], "childNodeCount": 3}}
        self.dom_fault = None
        self.command_error = {"code": -32000, "message": "Object couldn't be returned by value"}
        self.anchor_source = ANCHOR_SOURCE
        self.scope_fault = None
        self.scope_descriptions = ["Closure (factory)", "Module", "Global"]
        self.scope_bindings = [
            [{"name": "capturedStore", "value": {"type": "object", "objectId": "@model"}, "isOwn": True},
             {"name": "opaque", "value": {"type": "symbol", "objectId": "@symbol"}, "isOwn": True}],
            [{"name": "capturedStore", "value": {"type": "string", "value": "wrong shadowed binding"}, "isOwn": True}]]
        self.ctor_source = CTOR_SOURCE
        self.prototype_relation = True
        self.instance_direct = [True, True, False]
        self.instance_properties = [
            [{"name": "tag", "isOwn": True, "value": {"type": "string", "value": "first"}},
             {"name": "unsupported", "isOwn": True, "value": {"type": "string", "value": "unused\ud800"}}],
            [{"name": "tag", "isOwn": True, "value": {"type": "string", "value": "second"}}],
            [{"name": "tag", "isOwn": True, "value": {"type": "string", "value": "first"}}]]
        self.instance_fault = None
        self.instance_count_override = None

    def http(self, endpoint, path, deadline):
        assert endpoint == self.endpoint
        if path == "/json/version":
            return {"webSocketDebuggerUrl": self.browser_url}
        assert path == "/json/list"
        if self.fault == "foreign_target":
            return [{**self.target, "url": "https://outside.invalid/"}]
        if self.fault == "ambiguous_target":
            return [self.target, {**self.target, "id": "two"}]
        return [copy.deepcopy(self.target)]

    def connect(self, url, deadline):
        assert url in {self.browser_url, self.page_url}
        connection = ServerConnection(self, url == self.page_url, len(self.connections))
        self.connections.append(connection)
        return connection

    def response(self, connection, message):
        self.requests.append(copy.deepcopy(message))
        method, params = message["method"], message["params"]
        if method == "SystemInfo.getProcessInfo":
            return {"processInfo": [{"type": "browser", "id": 11223}, {"type": "renderer", "id": self.renderer_id}]}
        if method == "Page.getFrameTree":
            frame = self.frame_override if self.frame_override is not None else {"id": "frame-one", "url": self.url}
            return {"frameTree": {"frame": copy.deepcopy(frame)}}
        if method == "Runtime.enable":
            connection.queue.append({"method": "Runtime.executionContextCreated", "params": {"context": {
                "id": 1, "uniqueId": "context-" + str(connection.serial),
                "auxData": {"isDefault": True, "frameId": "frame-one"}}}})
            return {}
        if method == "Runtime.releaseObjectGroup":
            connection.group_released = True
            if self.fault == "release_failure":
                return {"__error__": True}
            return {}
        if method == "DOM.getDocument":
            assert params == {"depth": 0, "pierce": False}
            return {"root": {"nodeId": "wrong" if self.dom_fault == "document" else 6001,
                             "backendNodeId": 9001, "nodeType": 9, "nodeName": "#document"}}
        if method == "DOM.querySelectorAll":
            assert params["nodeId"] == 6001
            if self.dom_fault == "protocol":
                return {"__error__": True}
            if self.dom_fault == "identity":
                return {"nodeIds": [7101, True]}
            if self.dom_fault == "duplicate":
                return {"nodeIds": [7101, 7101]}
            if self.dom_fault == "match_budget":
                return {"nodeIds": list(range(1, I.MAX_DOM_MATCHES + 2))}
            matches = self.dom_matches if params["selector"] == "#app" else [7101] if params["selector"] == "body > #app" else []
            return {"nodeIds": copy.deepcopy(matches)}
        if method == "DOM.describeNode":
            assert params["depth"] == 0 and params["pierce"] is False
            node = copy.deepcopy(self.dom_nodes[params["nodeId"]])
            if self.dom_fault == "attributes":
                node["attributes"] = ["odd-unpaired-name"]
            elif self.dom_fault == "child_count":
                node["childNodeCount"] = False
            elif self.dom_fault == "selected_id":
                node["nodeId"] = 7999
            elif self.dom_fault == "element_type":
                node["nodeType"] = 9
            return {"node": node}
        if method == "DOM.resolveNode":
            assert params["nodeId"] == 7101 and params["executionContextId"] == 1 and params["objectGroup"]
            if self.dom_fault == "resolve":
                return {"__error__": True}
            if self.dom_fault == "resolve_context":
                connection.queue.append({"method": "Runtime.executionContextDestroyed", "params": {"executionContextId": 1}})
            return {"object": {"type": "object", "subtype": "proxy" if self.dom_fault == "resolve_wrapper" else "node",
                               "objectId": connection.prefix + "model"}}
        if method == "Runtime.evaluate":
            assert params["expression"] == I.GLOBAL and "uniqueContextId" in params and "contextId" not in params
            return {"result": {"type": "object", "objectId": connection.prefix + "global"}}
        if method == "Runtime.queryObjects":
            assert params["prototypeObjectId"] == connection.prefix + "prototype" and params["objectGroup"]
            if self.instance_fault == "timeout":
                return None
            if self.instance_fault == "context_lost":
                connection.queue.append({"method": "Runtime.executionContextDestroyed", "params": {"executionContextId": 1}})
            return {"objects": {"type": "object", "subtype": "array", "objectId": connection.prefix + "instance-array"}}
        if method == "Runtime.getProperties":
            assert params == {"objectId": params["objectId"], "ownProperties": True,
                              "accessorPropertiesOnly": False, "generatePreview": False}
            identity = params["objectId"].removeprefix(connection.prefix)
            assert params["objectId"].startswith(connection.prefix), "reused a stale scope identity"
            if identity == "instance-array":
                count = len(self.instance_properties)
                rows = [{"name": "length", "isOwn": True, "value": {"type": "number", "value": count}}]
                for index in range(count):
                    value = {"type": "object", "objectId": connection.prefix + "instance-" + str(index)}
                    if self.instance_fault == "proxy" and index == 0:
                        value["subtype"] = "proxy"
                    rows.append({"name": str(index), "isOwn": True, "value": value})
                if self.instance_fault == "sparse":
                    rows.pop()
                elif self.instance_fault == "array_getter":
                    rows[1]["get"] = {"type": "function", "objectId": connection.prefix + "getter"}
                return {"result": rows}
            if identity.startswith("instance-"):
                return {"result": copy.deepcopy(self.instance_properties[int(identity.removeprefix("instance-"))])}
            if identity == "anchor":
                scopes = {"name": "[[Scopes]]", "value": {"type": "object", "subtype": "internal#scopeList",
                          "objectId": connection.prefix + "scope-list"}}
                if self.scope_fault == "list_subtype":
                    scopes["value"]["subtype"] = "array"
                internal = [] if self.scope_fault == "missing" else [scopes]
                if self.scope_fault == "duplicate_internal":
                    internal.append(copy.deepcopy(scopes))
                return {"result": [], "internalProperties": internal}
            if identity == "scope-list":
                rows = [{"name": str(index), "isOwn": True, "value": {"type": "object", "subtype": "internal#scope",
                        "description": description, "objectId": connection.prefix + "scope-" + str(index)}}
                        for index, description in enumerate(self.scope_descriptions)]
                if self.scope_fault == "entry_subtype":
                    rows[0]["value"]["subtype"] = "array"
                elif self.scope_fault == "duplicate_index":
                    rows.append(copy.deepcopy(rows[0]))
                elif self.scope_fault == "noncanonical":
                    rows[0]["name"] = "00"
                elif self.scope_fault == "gap":
                    rows.pop(0)
                elif self.scope_fault == "entry_accessor":
                    rows[0]["get"] = {"type": "function", "objectId": connection.prefix + "getter"}
                return {"result": rows}
            if identity.startswith("scope-"):
                index = int(identity.removeprefix("scope-"))
                assert self.scope_descriptions[index] not in {"Global", "With Block"}, "traversed a Global/With scope"
                if self.scope_fault == "timeout":
                    return None
                if self.scope_fault == "context_lost":
                    connection.queue.append({"method": "Runtime.executionContextDestroyed", "params": {"executionContextId": 1}})
                rows = copy.deepcopy(self.scope_bindings[index])
                for row in rows:
                    for key in ("value", "get", "set", "symbol"):
                        remote = row.get(key, {})
                        if isinstance(remote.get("objectId"), str) and remote["objectId"].startswith("@"):
                            remote["objectId"] = connection.prefix + remote["objectId"][1:]
                return {"result": rows}
            assert identity == "model"
            return {"result": [], "privateProperties": [{"name": "#hiddenApply", "value": {
                "type": "function", "objectId": connection.prefix + "private-method"}},
                {"name": "#secret", "value": {"type": "string", "value": "private actual value"}}]}
        assert method == "Runtime.callFunctionOn"
        assert params["objectId"].startswith(connection.prefix), "client reused another transaction's live IDs"
        source = params["functionDeclaration"]
        arguments = params["arguments"]
        if self.fault == "context_destroyed" and source == I.PROPERTY:
            connection.queue.append({"method": "Runtime.executionContextDestroyed", "params": {"executionContextId": 1}})
        if self.fault == "replacement_context" and source == I.PROPERTY:
            connection.queue.append({"method": "Runtime.executionContextCreated", "params": {"context": {
                "id": 2, "uniqueId": "replacement-context", "auxData": {"isDefault": True, "frameId": "frame-one"}}}})
        if source == I.PROPERTY:
            name = arguments[0]["value"]
            if name == "model":
                return {"result": {"type": "object", "objectId": connection.prefix + "model"}}
            if name == "applyValue":
                return {"result": {"type": "function", "objectId": connection.prefix + "method"}}
            if name == "anchor":
                return {"result": {"type": "function", "objectId": connection.prefix + "anchor"}}
            if name == "storePrototype":
                return {"result": {"type": "object", "objectId": connection.prefix + "prototype"}}
            raise AssertionError("unexpected fixture property")
        if source == I.FUNCTION_SOURCE:
            value = self.ctor_source if params["objectId"].endswith("constructor") else self.anchor_source if params["objectId"].endswith("anchor") else self.source
            return {"result": {"type": "string", "value": value}}
        if source == I.METHOD_DISCOVERY:
            name = arguments[0]["value"]
            if name == "applyValue":
                value = {"status": "found", "function_source": self.source}
            else:
                value = {"status": "not_found", "prototype_depth": 3}
            return {"result": {"type": "object", "value": value}}
        if source == I.OWN_PROPERTY:
            name = arguments[0]["value"]
            if params["objectId"].endswith("prototype"):
                if self.instance_fault == "constructor_getter":
                    return {"exceptionDetails": {"text": "required_own_data_property_absent_or_accessor"}}
                assert name == "constructor"
                return {"result": {"type": "function", "objectId": connection.prefix + "constructor"}}
            assert params["objectId"].endswith("instance-array") and name.isdigit()
            return {"result": {"type": "object", "objectId": connection.prefix + "instance-" + name}}
        if source == I.PROTOTYPE_RELATION:
            assert arguments[0] == {"objectId": connection.prefix + "prototype"}
            return {"result": {"type": "boolean", "value": self.prototype_relation}}
        if source == I.ARRAY_COUNT:
            count = len(self.instance_properties) if self.instance_count_override is None else self.instance_count_override
            return {"result": {"type": "number", "value": count}}
        if source == I.DIRECT_PROTOTYPE:
            index = int(params["objectId"].removeprefix(connection.prefix + "instance-"))
            return {"result": {"type": "boolean", "value": self.instance_direct[index]}}
        if source == I.OWN_INVARIANTS:
            index = int(params["objectId"].removeprefix(connection.prefix + "instance-"))
            rows = {row["name"]: row for row in self.instance_properties[index]}
            matched = "constructor" not in rows and all(item["name"] in rows and rows[item["name"]].get("isOwn", True) is True and
                "get" not in rows[item["name"]] and "set" not in rows[item["name"]] and
                rows[item["name"]].get("value", {}).get("value") == item["equals"] for item in arguments[0]["value"])
            return {"result": {"type": "boolean", "value": matched}}
        if source == I.CALL:
            assert arguments[0]["objectId"] in {connection.prefix + "model", connection.prefix + "instance-0"}
            if self.instance_fault == "dispatch_guard":
                return {"exceptionDetails": {"text": "selected_instance_preconditions_changed_before_dispatch"}}
            values = arguments[1]["value"]
            self.dispatches.append(copy.deepcopy(values))
            if self.fault == "timeout_after_dispatch":
                return None
            if self.fault == "exception_after_dispatch":
                details = self.exception_details if self.exception_details is not None else {"text": "controlled fault"}
                return {"result": {"type": "undefined"}, "exceptionDetails": copy.deepcopy(details)}
            return {"result": {"type": "object", "value": {"completed": True,
                "value": f"state:{values[0]}:{str(values[1]).lower()}"}}}
        if source == I.DESCRIBE:
            return {"result": {"type": "object", "value": {"properties": [{"name": "applyValue", "kind": "data",
                "type": "function", "function_source": METHOD}], "truncated": False, "getters_invoked": 0}}}
        if source == I.GRAPH:
            if self.fault == "graph_protocol_error":
                return {"__error__": True, "__error_payload__": copy.deepcopy(self.command_error)}
            return {"result": {"type": "object", "value": {"nodes": [], "truncated": False,
                "getters_invoked": 0, "whole_graph_coverage": False}}}
        raise AssertionError("unexpected fixed function")


class ServerConnection:
    def __init__(self, server, page, serial):
        self.server, self.page, self.serial = server, page, serial
        self.prefix = "transaction-" + str(serial) + "."
        self.queue, self.closed, self.group_released = [], False, False

    def send(self, raw):
        assert not self.closed
        message = json.loads(raw)
        response = self.server.response(self, message)
        if response is not None:
            self.queue.append({"id": message["id"], "error": response.get("__error_payload__", {"code": -32000})} if response.get("__error__") else
                              {"id": message["id"], "result": response})

    def recv(self, timeout):
        if not self.queue:
            raise TimeoutError("controlled missing protocol response")
        return json.dumps(self.queue.pop(0))

    def close(self):
        self.closed = True


@pytest.mark.parametrize("lc_all,lang", [("de_DE.UTF-8", "fr_FR.UTF-8"),
                                        ("fr_FR.UTF-8", "ja_JP.UTF-8"), (None, "de_DE.UTF-8")])
def test_kernel_process_epoch_forces_child_locale_without_changing_caller(tmp_path, monkeypatch, lc_all, lang):
    executable = tmp_path / "Owned Application"
    executable.write_text("inert process metadata fixture")
    if lc_all is None:
        monkeypatch.delenv("LC_ALL", raising=False)
    else:
        monkeypatch.setenv("LC_ALL", lc_all)
    monkeypatch.setenv("LANG", lang)
    monkeypatch.setenv("LC_TIME", "de_DE.UTF-8")
    monkeypatch.setenv("REA_KEEP_CONTEXT", "owned")
    caller = dict(os.environ)
    calls = []
    started = "Fri Oct 16 09:30:00 2026"
    def process(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=f" 1 {os.getuid()} {started} {executable}\n", stderr="")
    monkeypatch.setattr(R.subprocess, "run", process)
    identity = I._process(7101, time.monotonic() + 2)
    assert identity == {"pid": 7101, "ppid": 1, "uid": os.getuid(), "started_at": started,
                        "executable": str(executable.resolve(strict=True))}
    assert len(calls) == 1 and calls[0][0] == ["/bin/ps", "-o", "ppid=,uid=,lstart=,comm=", "-p", "7101"]
    options = calls[0][1]
    assert options["env"] == dict(caller, LC_ALL="C", LANG="C") and options["env"] is not os.environ
    assert options["capture_output"] is True and options["text"] is True and options["check"] is False
    assert 0 < options["timeout"] <= 2 and dict(os.environ) == caller


@pytest.fixture
def environment(tmp_path, monkeypatch):
    bundle = tmp_path / "Independent Production.app"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/Resources").mkdir()
    executable = bundle / "Contents/MacOS/Independent Production"
    executable.write_bytes(b"inert original bytes")
    executable.chmod(0o755)
    (bundle / "Contents/Resources/renderer.html").write_text("fixture document")
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.example.production",
        "CFBundleExecutable": executable.name, "CFBundleVersion": "1"}))
    server = ProtocolServer(bundle)
    main = {"bundle_id": "org.example.production", "bundle": str(bundle), "executable": str(executable),
            "pid": 11223, "launch_time": 1700000000.25, "uid": os.getuid()}
    child = {"pid": 22334, "ppid": 11223, "uid": os.getuid(), "started_at": "Tue Oct 13 08:00:00 2026",
             "executable": str(executable)}
    config_file = tmp_path / "operator.json"
    R.write_json(config_file, {"format": I.TRANSPORT, "endpoint": server.endpoint,
        "relative_url": "Contents/Resources/renderer.html", "target": R.fingerprint(bundle)})
    config_file.chmod(0o600)
    declaration = tmp_path / "reviewed-declaration.json"
    actor = {"kind": "inspector_method", "receiver": copy.deepcopy(RECIPE), "method": "applyValue",
             "function_sha256": METHOD_SHA, "arguments": [{"type": "int32", "value": 7}, {"type": "bool", "value": True}],
             "return_type": "utf8", "signature_evidence": {"kind": "reviewed_js", "file": str(declaration),
                "sha256": "0" * 64, "pointer": "/contract"}}
    R.write_json(declaration, {"contract": I.call_contract(actor)})
    actor["signature_evidence"]["sha256"] = R._file_hash(declaration)
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(I.B, "process_identity", lambda app, bundle_id: copy.deepcopy(main))
    monkeypatch.setattr(I, "_listener", lambda endpoint, identity, deadline: {
        "pid": main["pid"], "uid": main["uid"], "listen_addresses": ["127.0.0.1:28123"]})
    monkeypatch.setattr(I, "_process", lambda pid, deadline: copy.deepcopy(child))
    monkeypatch.setattr(I, "_http_json", server.http)
    monkeypatch.setattr(I, "_socket", server.connect)
    return SimpleNamespace(bundle=bundle, server=server, main=main, child=child, actor=actor,
                           config=config_file, out=tmp_path, declaration=declaration)


def run(environment, name="run", operations=None):
    return I.collect(environment.bundle, environment.out / name,
        operations or I.invocation_request(environment.actor), timeout=10, config_file=environment.config)


def scopes_request(*, max_scopes=3, max_bindings=8):
    return {"operations": [{"op": "function_scopes", "recipe": copy.deepcopy(ANCHOR_RECIPE),
        "function_sha256": ANCHOR_SHA, "max_scopes": max_scopes, "max_bindings": max_bindings}]}


def measured_scope_recipe(environment, *, name="mapping", binding="capturedStore"):
    receipt = run(environment, name, scopes_request())
    assert receipt["status"] == "observed", receipt.get("error")
    observed = receipt["operations"][0]
    scope = observed["scopes"][0]
    step = {"kind": "scope_binding", "function_sha256": ANCHOR_SHA,
            "scope": {"index": scope["index"], "description": scope["description"],
                      "chain_sha256": observed["chain_sha256"], "bindings_sha256": scope["bindings_sha256"]},
            "name": binding}
    return {"root": copy.deepcopy(ANCHOR_RECIPE["root"]), "steps": [*copy.deepcopy(ANCHOR_RECIPE["steps"]), step]}


def test_scope_discovery_exports_bounded_names_and_types_without_global_traversal_or_proof(environment):
    receipt = run(environment, operations=scopes_request(max_bindings=1))
    assert receipt["status"] == "observed" and receipt["state_uncertain"] is False
    observed = receipt["operations"][0]
    assert observed["scope_count"] == 3 and observed["scopes_truncated"] is False
    assert observed["scopes"][0]["bindings_truncated"] is True
    assert observed["scopes"][0]["bindings"] == [{"name": "capturedStore", "kind": "data", "type": "object", "value_supported": True}]
    assert observed["scopes"][1]["bindings"][0]["type"] == "string"
    assert observed["scopes"][2] == {"index": 2, "description": "Global", "kind": "Global",
        "traversed": False, "unsupported_reason": "unsupported_scope_kind"}
    encoded = json.dumps(receipt)
    assert "wrong shadowed binding" not in encoded and "transaction-" not in encoded and '"objectId"' not in encoded
    assert observed["primitive_values_exported"] is False and observed["live_ids_exported"] is False
    assert observed["debugger_pause"] is False and observed["whole_scope_coverage"] is False
    assert receipt["method_dispatch_attempts"] == receipt["getter_dispatch_attempts"] == 0
    assert receipt["proof_created"] is False and receipt["task_result_verified"] is False
    assert receipt["protocol_limits"] == receipt["adapter"]["protocol_limits"] == I._protocol_limits()
    assert receipt["cleanup"]["object_group_released"] is True
    assert all(connection.closed for connection in environment.server.connections)


def test_scope_recipe_reuses_fresh_handles_and_selects_exact_captured_object_over_shadowed_name(environment):
    receiver = measured_scope_recipe(environment)
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = receiver
    # Receiver selection is pinned by the recipe; the independent typed
    # declaration remains about the same original method body and signature.
    first = run(environment, "first-scoped", I.invocation_request(actor))
    second = run(environment, "second-scoped", I.invocation_request(actor))
    assert first["status"] == second["status"] == "executed"
    assert first["operations"][0]["value"] == second["operations"][0]["value"] == "state:7:true"
    assert environment.server.dispatches == [[7, True], [7, True]]
    assert I.receipt_valid(first, actor, first["target"], first["adapter"], first["transport"])
    assert first["endpoint_identity"]["execution_context"] != second["endpoint_identity"]["execution_context"]
    assert "transaction-" not in json.dumps([first, second])
    changed = copy.deepcopy(first)
    changed["protocol_limits"]["wire_message_bytes"] *= 2
    assert not I.receipt_valid(changed, actor, first["target"], first["adapter"], first["transport"], check_files=False)


@pytest.mark.parametrize("failure", ["missing", "duplicate_internal", "list_subtype", "entry_subtype", "duplicate_index",
    "noncanonical", "gap", "entry_accessor", "timeout", "context_lost"])
def test_malformed_or_lost_scope_metadata_stops_later_calls_and_preserves_uncertainty_cleanup(environment, failure):
    environment.server.scope_fault = failure
    request = scopes_request()
    request["operations"].append({"op": "call", "actor": environment.actor})
    receipt = run(environment, operations=request)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is True
    assert receipt["resource_review_required"] is True and receipt["retry"] is False
    assert not environment.server.dispatches and receipt["method_dispatch_attempts"] == 0
    assert all(connection.closed for connection in environment.server.connections)
    assert any(item["method"] == "Runtime.releaseObjectGroup" for item in environment.server.requests)


@pytest.mark.parametrize("change", ["source", "native", "chain", "description", "bindings", "missing_binding"])
def test_scope_reuse_refuses_changed_pins_without_searching_a_shadowed_binding(environment, change):
    receiver = measured_scope_recipe(environment)
    if change == "source":
        environment.server.anchor_source += " changed"
    elif change == "native":
        environment.server.anchor_source = "function retainedAnchor() { [native code] }"
    elif change == "chain":
        environment.server.scope_descriptions[1] = "Closure (differentOuter)"
    elif change == "description":
        receiver["steps"][-1]["scope"]["description"] = "Closure (changed)"
    elif change == "bindings":
        environment.server.scope_bindings[0].append({"name": "newBinding", "value": {"type": "number", "value": 1}})
    else:
        receiver["steps"][-1]["name"] = "absentHere"
        environment.server.scope_bindings[1].append({"name": "absentHere", "value": {"type": "object", "objectId": "@model"}})
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = receiver
    receipt = run(environment, "changed-scoped", I.invocation_request(actor))
    assert receipt["status"] == "incomplete" and not environment.server.dispatches
    assert receipt["state_uncertain"] is True and receipt["resource_review_required"] is True
    assert receipt["cleanup"]["object_group_released"] is True


@pytest.mark.parametrize("descriptor", [
    {"name": "capturedStore", "get": {"type": "function", "objectId": "@getter"}},
    {"name": "capturedStore", "wasThrown": True, "value": {"type": "object", "objectId": "@error"}},
    {"name": "capturedStore", "symbol": {"type": "symbol", "objectId": "@symbol"}, "value": {"type": "object", "objectId": "@model"}},
    {"name": "capturedStore", "value": {"type": "number", "unserializableValue": "NaN"}},
    {"name": "capturedStore", "value": {"type": "undefined"}},
    {"name": "capturedStore", "value": {"type": "object", "objectId": ""}},
    {"name": "capturedStore", "value": {"type": "not-real"}},
    {"name": "capturedStore", "value": {"type": "object", "objectId": "@model"}, "isOwn": False}])
def test_scope_binding_refuses_accessor_thrown_unserializable_and_malformed_values(environment, descriptor):
    receiver = measured_scope_recipe(environment)
    environment.server.scope_bindings[0][0] = descriptor
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = receiver
    receipt = run(environment, "unsupported-scoped", I.invocation_request(actor))
    assert receipt["status"] == "incomplete" and not environment.server.dispatches
    assert receipt["getter_dispatch_attempts"] == receipt["method_dispatch_attempts"] == 0
    assert receipt["state_uncertain"] is True and receipt["cleanup"]["object_group_released"] is True


def test_large_scope_metadata_is_filtered_inside_wire_cap_and_complete_binding_cardinality(environment):
    environment.server.scope_bindings[0].append({"name": "largeFunction", "value": {"type": "function", "objectId": "@large",
        "description": "unretained-source" + "x" * (I.MAX_MESSAGE + 1)}})
    receipt = run(environment, operations=scopes_request(max_scopes=1, max_bindings=1))
    assert receipt["status"] == "observed", receipt.get("error")
    assert "unretained-source" not in json.dumps(receipt) and len(json.dumps(receipt).encode()) < 20000
    environment.server.scope_bindings[0] = [{"name": "binding" + str(index), "value": {"type": "number", "value": index}}
        for index in range(I.MAX_SCOPE_BINDINGS)]
    boundary = run(environment, "boundary", scopes_request(max_scopes=1, max_bindings=1))
    assert boundary["status"] == "observed" and boundary["operations"][0]["scopes"][0]["binding_count"] == I.MAX_SCOPE_BINDINGS
    environment.server.scope_bindings[0].append({"name": "overflow", "value": {"type": "null"}})
    refused = run(environment, "overflow", scopes_request(max_scopes=1))
    assert refused["status"] == "incomplete" and refused["state_uncertain"] is True


def test_scope_chain_and_preview_bounds_are_distinct_and_duplicate_bindings_refuse(environment):
    preview = run(environment, operations=scopes_request(max_scopes=1, max_bindings=1))
    assert preview["operations"][0]["scope_count"] == 3 and preview["operations"][0]["scopes_truncated"] is True
    environment.server.scope_bindings[0].append(copy.deepcopy(environment.server.scope_bindings[0][0]))
    duplicate = run(environment, "duplicate-binding", scopes_request())
    assert duplicate["status"] == "incomplete" and not duplicate["operations"]
    environment.server.scope_descriptions = ["Global"] * (I.MAX_SCOPE_CHAIN + 1)
    chain = run(environment, "long-chain", scopes_request())
    assert chain["status"] == "incomplete" and not chain["operations"]


@pytest.mark.parametrize("value,reason", [("lone-high\ud800", "non_utf8_string"), ("lone-low\udfff", "non_utf8_string"),
    ("x" * (I.MAX_TEXT + 1), "text_budget"), ("embedded\0nul", "nul_in_string")])
def test_unrelated_unsupported_scope_strings_do_not_block_valid_object_but_cannot_be_selected(environment, value, reason):
    environment.server.scope_bindings[0].append({"name": "unrelatedConstant", "value": {"type": "string", "value": value}})
    discovery = run(environment, "unsupported-string-discovery", scopes_request())
    assert discovery["status"] == "observed", discovery.get("error")
    metadata = next(row for row in discovery["operations"][0]["scopes"][0]["bindings"] if row["name"] == "unrelatedConstant")
    assert metadata == {"name": "unrelatedConstant", "kind": "data", "type": "string",
                        "value_supported": False, "unsupported_reason": reason}
    json.dumps(discovery, ensure_ascii=False).encode("utf-8")
    receiver = measured_scope_recipe(environment, name="valid-object-mapping")
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = receiver
    good = run(environment, "valid-object", I.invocation_request(actor))
    assert good["status"] == "executed" and environment.server.dispatches == [[7, True]]
    actor["receiver"]["steps"][-1]["name"] = "unrelatedConstant"
    refused = run(environment, "unsupported-string-selection", I.invocation_request(actor))
    assert refused["status"] == "incomplete" and refused["state_uncertain"] is True
    assert refused["method_dispatch_attempts"] == 0 and environment.server.dispatches == [[7, True]]
    assert refused["cleanup"]["object_group_released"] is True


def test_invalid_utf8_request_text_is_refused_as_schema_data_before_app_access(environment):
    assert I._text("valid") is True and I._text("bad\ud800") is False
    request = scopes_request()
    request["operations"][0]["recipe"]["root"]["name"] = "bad\ud800"
    with pytest.raises(ValueError):
        run(environment, operations=request)
    assert not environment.server.connections


@pytest.mark.parametrize("mutation", ["hash", "preview", "extra", "getter", "op_budget", "scope_global", "scope_index", "step_budget"])
def test_scope_schemas_reject_source_handles_unsupported_selection_and_overbudget_requests_before_app_access(environment, mutation):
    request = scopes_request()
    operation = request["operations"][0]
    if mutation == "hash":
        operation["function_sha256"] = "not-a-hash"
    elif mutation == "preview":
        operation["max_bindings"] = 33
    elif mutation == "extra":
        operation["objectId"] = "untrusted-live-handle"
    elif mutation == "getter":
        operation["recipe"]["steps"][0].update(access="getter", getter_sha256="0" * 64)
    elif mutation == "op_budget":
        request["operations"] = [copy.deepcopy(operation)] * 3
    else:
        step = {"kind": "scope_binding", "function_sha256": ANCHOR_SHA, "name": "store",
            "scope": {"index": 0, "description": "Closure (factory)", "chain_sha256": "0" * 64, "bindings_sha256": "0" * 64}}
        if mutation == "scope_global":
            step["scope"]["description"] = "Global"
        elif mutation == "scope_index":
            step["scope"]["index"] = True
        operation["recipe"]["steps"] += [copy.deepcopy(step)] * (3 if mutation == "step_budget" else 1)
    with pytest.raises(ValueError):
        run(environment, operations=request)
    assert not environment.server.connections


def test_cleanup_drains_large_late_scope_response_without_accepting_it_as_a_result():
    class LateScopeSocket:
        def __init__(self):
            self.original = None
            self.queue = []

        def send(self, raw):
            message = json.loads(raw)
            if message["method"] == "Runtime.getProperties":
                self.original = message["id"]
            else:
                self.queue = [json.dumps({"id": self.original, "result": {"result": [],
                    "discarded": "x" * (I.MAX_MESSAGE + 1)}}), json.dumps({"id": message["id"], "result": {}})]

        def recv(self, timeout):
            if not self.queue:
                raise TimeoutError("controlled delayed scope metadata")
            return self.queue.pop(0)

    channel = I._CDP(LateScopeSocket(), time.monotonic() + 10)
    with pytest.raises(TimeoutError):
        channel.call("Runtime.getProperties", {"objectId": "fresh"})
    assert channel.call("Runtime.releaseObjectGroup", {"objectGroup": "owned"}, cleanup=True) == {}
    assert channel.pending == set() and channel.next_id == 2


@pytest.mark.parametrize("kind", ["wire_overflow", "ordinary_large", "large_event"])
def test_wire_growth_does_not_raise_retained_reply_or_event_limits(kind):
    class BoundedSocket:
        def send(self, raw):
            request = json.loads(raw)
            if kind == "wire_overflow":
                self.reply = "x" * (I.MAX_WIRE_MESSAGE + 1)
            elif kind == "ordinary_large":
                self.reply = json.dumps({"id": request["id"], "result": {"unused": "x" * (I.MAX_MESSAGE + 1)}})
            else:
                self.reply = json.dumps({"method": "Runtime.consoleAPICalled", "params": {"unused": "x" * (I.MAX_MESSAGE + 1)}})

        def recv(self, timeout):
            return self.reply

    channel = I._CDP(BoundedSocket(), time.monotonic() + 10)
    with pytest.raises(I.ProtocolError):
        channel.call("Runtime.evaluate" if kind == "ordinary_large" else "Runtime.getProperties")
    assert channel.events == [] and channel.retained_event_bytes == 0


def instances_request():
    return {"operations": [{"op": "prototype_instances", "recipe": copy.deepcopy(PROTOTYPE_RECIPE),
        "constructor_sha256": CTOR_SHA, "max_instances": 8, "max_properties": 8}]}


def instance_recipe(*, invariants=None, max_candidates=64):
    return {"root": copy.deepcopy(PROTOTYPE_RECIPE["root"]), "steps": [*copy.deepcopy(PROTOTYPE_RECIPE["steps"]),
        {"kind": "unique_instance", "constructor_sha256": CTOR_SHA, "max_candidates": max_candidates,
         "own_invariants": [{"name": "tag", "type": "utf8", "equals": "first"}] if invariants is None else invariants}]}


def test_instance_discovery_exports_only_bounded_own_literals_and_explicit_heap_effects(environment):
    receipt = run(environment, operations=instances_request())
    assert receipt["status"] == "observed", receipt.get("error")
    observed = receipt["operations"][0]
    assert observed["complete_candidate_count"] == 3 and observed["instances_truncated"] is False
    first = observed["instances"][0]
    assert first["direct_prototype"] is True
    assert first["properties"][0]["literal_invariant"] == {"name": "tag", "type": "utf8", "equals": "first"}
    assert first["properties"][1]["unsupported_reason"] == "non_utf8_string"
    assert "literal_invariant" not in first["properties"][1]
    assert observed["instances"][2]["direct_prototype"] is False
    assert "properties" not in observed["instances"][2]
    assert observed["selection_indices_exported"] is False and observed["live_ids_exported"] is False
    assert receipt["heap_query_effects"] == I._heap_effects(1) and observed["server_side_candidate_bound"] is False
    assert receipt["instance_selection_modes"] == [] and not environment.server.dispatches
    assert receipt["proof_created"] is False and receipt["task_result_verified"] is False
    assert "transaction-" not in json.dumps(receipt) and '"objectId"' not in json.dumps(receipt)
    json.dumps(receipt, ensure_ascii=False).encode()
    assert receipt["cleanup"]["object_group_released"] is True


def test_exact_literal_instance_selection_excludes_subclasses_and_reuses_fresh_ids(environment):
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = instance_recipe()
    first = run(environment, "first-instance", I.invocation_request(actor))
    second = run(environment, "second-instance", I.invocation_request(actor))
    assert first["status"] == second["status"] == "executed", (first.get("error"), second.get("error"))
    assert environment.server.dispatches == [[7, True], [7, True]]
    assert first["instance_selection_modes"] == ["literal_own_invariants"]
    assert first["heap_query_effects"] == I._heap_effects(1)
    assert I.receipt_valid(first, actor, first["target"], first["adapter"], first["transport"])
    assert "transaction-" not in json.dumps([first, second])
    altered = copy.deepcopy(first)
    altered["heap_query_effects"]["server_side_candidate_bound"] = True
    assert not I.receipt_valid(altered, actor, first["target"], first["adapter"], first["transport"], check_files=False)


def test_empty_instance_invariants_require_raw_complete_singleton_before_direct_filtering(environment):
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = instance_recipe(invariants=[])
    environment.server.instance_direct = [True, False, False]
    multiple = run(environment, "multiple", I.invocation_request(actor))
    assert multiple["status"] == "incomplete" and not environment.server.dispatches
    assert not any(row["method"] == "Runtime.callFunctionOn" and row["params"]["functionDeclaration"] == I.DIRECT_PROTOTYPE
                   for row in environment.server.requests)
    environment.server.instance_properties = environment.server.instance_properties[:1]
    environment.server.instance_direct = [True]
    single = run(environment, "singleton", I.invocation_request(actor))
    assert single["status"] == "executed" and single["instance_selection_modes"] == ["snapshot_singleton"]
    assert environment.server.dispatches == [[7, True]]
    assert I.receipt_valid(single, actor, single["target"], single["adapter"], single["transport"])


@pytest.mark.parametrize("failure", ["ambiguous", "no_match", "inherited", "getter", "constructor_prototype", "only_subclass", "zero", "overbudget",
    "proxy", "sparse", "array_getter", "timeout", "context_lost"])
def test_instance_selection_never_chooses_first_or_inherited_accessor_proxy_or_incomplete_matches(environment, failure):
    if failure == "ambiguous":
        environment.server.instance_properties[1][0]["value"]["value"] = "first"
    elif failure == "no_match":
        environment.server.instance_properties[0][0]["value"]["value"] = "absent"
    elif failure == "inherited":
        environment.server.instance_properties[0][0]["isOwn"] = False
    elif failure == "getter":
        environment.server.instance_properties[0][0] = {"name": "tag", "get": {"type": "function", "objectId": "ignored-getter"}}
    elif failure == "constructor_prototype":
        environment.server.instance_properties[0].append({"name": "constructor", "value": {"type": "function", "objectId": "ignored-constructor"}})
    elif failure == "only_subclass":
        environment.server.instance_direct = [False, False, False]
    elif failure == "zero":
        environment.server.instance_properties = []
        environment.server.instance_direct = []
    elif failure == "overbudget":
        environment.server.instance_count_override = 65
    else:
        environment.server.instance_fault = failure
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = instance_recipe()
    receipt = run(environment, operations=I.invocation_request(actor))
    assert receipt["status"] == "incomplete" and not environment.server.dispatches
    assert receipt["state_uncertain"] is True and receipt["resource_review_required"] is True
    assert receipt["method_dispatch_attempts"] == receipt["getter_dispatch_attempts"] == 0
    assert receipt["heap_query_effects"] == I._heap_effects(1)
    assert all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("failure", ["source", "native", "relation", "constructor_getter"])
def test_constructor_source_and_own_relation_refuse_before_heap_query(environment, failure):
    if failure == "source":
        environment.server.ctor_source += " changed"
    elif failure == "native":
        environment.server.ctor_source = "function Object() { [native code] }"
    elif failure == "relation":
        environment.server.prototype_relation = False
    else:
        environment.server.instance_fault = failure
    receipt = run(environment, operations=instances_request())
    assert receipt["status"] == "incomplete" and receipt["heap_query_effects"] == I._heap_effects(0)
    assert not any(row["method"] == "Runtime.queryObjects" for row in environment.server.requests)


def test_instance_dispatch_guard_uses_fresh_anchor_handles_inside_call_and_does_not_leak_to_later_operation(environment):
    actor = copy.deepcopy(environment.actor)
    actor["receiver"] = instance_recipe()
    request = {"operations": [{"op": "call", "actor": actor}, {"op": "call", "actor": environment.actor}]}
    receipt = run(environment, operations=request)
    assert receipt["status"] == "executed" and len(environment.server.dispatches) == 2
    calls = [row for row in environment.server.requests if row["method"] == "Runtime.callFunctionOn" and row["params"]["functionDeclaration"] == I.CALL]
    guarded, unguarded = [row["params"]["arguments"] for row in calls]
    assert len(guarded) == 9 and len(unguarded) == 6
    assert guarded[5]["value"] == [{"constructor_source": CTOR_SOURCE,
        "own_invariants": [{"name": "tag", "type": "utf8", "equals": "first"}]}]
    assert all("objectId" in row for row in guarded[6:]) and unguarded[5] == {"value": []}


@pytest.mark.parametrize("invalid", ["source", "ordinal", "bad_literal", "duplicate", "two_steps", "heap_budget"])
def test_instance_schemas_refuse_source_indices_bindings_and_overbudget_work_before_app_access(environment, invalid):
    request = instances_request()
    if invalid == "source":
        request["operations"][0]["constructor_sha256"] = "source instead of hash"
    elif invalid == "heap_budget":
        request["operations"] *= 5
    else:
        actor = copy.deepcopy(environment.actor)
        actor["receiver"] = instance_recipe()
        step = actor["receiver"]["steps"][-1]
        if invalid == "ordinal":
            step["index"] = 0
        elif invalid == "bad_literal":
            step["own_invariants"][0]["equals"] = {"input": "variable"}
        elif invalid == "duplicate":
            step["own_invariants"] *= 2
        else:
            actor["receiver"]["steps"].append(copy.deepcopy(step))
        request = {"operations": [{"op": "call", "actor": actor}]}
    with pytest.raises(ValueError):
        run(environment, operations=request)
    assert not environment.server.connections


def test_transport_can_pin_verified_target_without_bundle_or_runtime_access(environment, monkeypatch):
    target = R.fingerprint(environment.bundle)

    def forbidden(*args, **kwargs):
        raise AssertionError("transport metadata must not resolve a bundle or access an app")

    monkeypatch.setattr(R, "fingerprint", forbidden)
    monkeypatch.setattr(I.B, "process_identity", forbidden)
    monkeypatch.setattr(I, "_listener", forbidden)
    monkeypatch.setattr(I, "_http_json", forbidden)
    monkeypatch.setattr(I, "_socket", forbidden)
    transport = I.transport_identity_for_target(target, config_file=environment.config)
    assert transport == {"kind": "chromium_renderer_cdp", "endpoint": environment.server.endpoint,
                         "relative_url": "Contents/Resources/renderer.html",
                         "config": {"file": str(environment.config), "sha256": R._file_hash(environment.config),
                                    "bytes": environment.config.stat().st_size, "uid": os.getuid(), "mode": "0600"}}


@pytest.mark.parametrize("change", ["target", "public_mode", "symlink", "relative_escape"])
def test_transport_metadata_rejects_unowned_or_mismatched_config(environment, change):
    target, config_file = R.fingerprint(environment.bundle), environment.config
    if change == "target":
        target["version"] += "changed"
    elif change == "public_mode":
        config_file.chmod(0o644)
    elif change == "symlink":
        config_file = environment.out / "configuration-link.json"
        config_file.symlink_to(environment.config)
    else:
        config = R.load_json(config_file)
        config["relative_url"] = "../foreign.html"
        R.write_json(config_file, config)
    with pytest.raises(ValueError):
        I.transport_identity_for_target(target, config_file=config_file)
    assert not environment.server.connections


def test_transport_metadata_checks_vm_before_reading_private_configuration(environment, monkeypatch):
    def denied():
        raise PermissionError("not in VM")

    def forbidden(*args, **kwargs):
        raise AssertionError("configuration read happened before VM guard")

    monkeypatch.setattr(R, "assert_guest", denied)
    monkeypatch.setattr(I.B, "_private_file", forbidden)
    with pytest.raises(PermissionError, match="not in VM"):
        I.transport_identity_for_target({}, config_file=environment.config)


def test_typed_call_uses_fresh_receiver_function_ids_and_own_provider_envelope(environment):
    first, second = run(environment, "first"), run(environment, "second")
    assert first["status"] == second["status"] == "executed"
    assert first["operations"][0]["value"] == "state:7:true"
    assert first["proof_created"] is False and first["task_result_verified"] is False
    assert environment.server.dispatches == [[7, True], [7, True]]
    assert all(connection.closed for connection in environment.server.connections)
    assert all(connection.group_released for connection in environment.server.connections if connection.page)
    assert I.receipt_valid(first, environment.actor, first["target"], first["adapter"], first["transport"])
    assert I.receipt_valid(first, environment.actor, first["target"], first["adapter"], first["transport"], check_files=False)
    encoded = json.dumps([first, second])
    assert '"objectId"' not in encoded and "transaction-1." not in encoded and "transaction-3." not in encoded
    assert first["cleanup"]["requested_evaluations_completed"] is True
    assert first["cleanup"]["remote_execution_cancelled"] is False
    assert first["operations"][0]["body_entry_independently_proved"] is False
    assert first["endpoint_identity"]["execution_context"]["unique_id"] != second["endpoint_identity"]["execution_context"]["unique_id"]


@pytest.mark.parametrize("failure", ["foreign_target", "ambiguous_target", "foreign_renderer"])
def test_foreign_or_ambiguous_target_stops_before_any_remote_app_evaluation(environment, failure):
    if failure == "foreign_renderer":
        environment.child["uid"] += 1
    else:
        environment.server.fault = failure
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is False
    assert not environment.server.dispatches
    assert not [row for row in environment.server.requests if row["method"] in {"Runtime.evaluate", "Runtime.callFunctionOn"}]
    assert all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("failure", ["timeout_after_dispatch", "exception_after_dispatch", "context_destroyed", "replacement_context", "release_failure"])
def test_fault_never_retries_or_runs_later_operation_and_always_attempts_cleanup(environment, failure):
    environment.server.fault = failure
    request = {"operations": [{"op": "call", "actor": environment.actor}, {"op": "call", "actor": environment.actor}]}
    receipt = run(environment, operations=request)
    assert receipt["status"] == "incomplete" and receipt["resource_review_required"] is True
    assert receipt["retry"] is False
    if failure != "release_failure":
        assert len(environment.server.dispatches) <= 1 and receipt["state_uncertain"] is True
        assert receipt["actuated"] is None
    assert all(connection.closed for connection in environment.server.connections)
    assert any(row["method"] == "Runtime.releaseObjectGroup" for row in environment.server.requests)
    if failure == "timeout_after_dispatch":
        assert environment.server.dispatches == [[7, True]]
        assert receipt["cleanup"]["requested_evaluations_completed"] is False
        assert receipt["cleanup"]["remote_execution_cancelled"] is False


def test_remote_exception_diagnostic_is_actionable_without_exporting_handles(environment):
    environment.server.fault = "exception_after_dispatch"
    environment.server.exception_details = {"exceptionId": 19, "text": "Uncaught", "lineNumber": 7, "columnNumber": 23,
        "url": "file:///shipped-renderer.js", "executionContextId": 99,
        "exception": {"type": "object", "subtype": "error", "className": "TypeError",
            "description": "TypeError: controlled original-method fault", "objectId": "never-export-this-live-handle",
            "preview": {"description": "never-export-this-preview"}, "value": "never-export-this-value"},
        "stackTrace": {"callFrames": [{"url": "never-export-this-stack-object"}]}}
    receipt = run(environment)
    diagnostic = receipt["error"]["remote_exception"]
    assert diagnostic == {"malformed": False, "truncated": False, "text": "Uncaught", "line": 7, "column": 23,
        "source_url": "file:///shipped-renderer.js", "type": "object", "subtype": "error",
        "description": "TypeError: controlled original-method fault", "fixed_template": "CALL"}
    assert "never-export-this" not in json.dumps(receipt)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is True
    assert receipt["resource_review_required"] is True and receipt["retry"] is False
    assert environment.server.dispatches == [[7, True]]
    assert receipt["cleanup"]["object_group_released"] is True
    assert all(connection.closed for connection in environment.server.connections)


def test_remote_exception_text_budgets_are_utf8_bytes_and_preserve_uncertain_cleanup(environment):
    environment.server.fault = "exception_after_dispatch"
    environment.server.exception_details = {"text": "x" * 6000, "lineNumber": 0, "columnNumber": 0,
        "url": "file:///" + "y" * 6000, "exception": {"type": "object", "subtype": "error",
            "description": "🧭" * 3000, "objectId": "unexported"}}
    receipt = run(environment)
    diagnostic = receipt["error"]["remote_exception"]
    assert diagnostic["truncated"] is True and diagnostic["malformed"] is False
    assert len(diagnostic["text"].encode()) <= 1024 and len(diagnostic["source_url"].encode()) <= 1024
    assert len(diagnostic["description"].encode()) <= 2048 and diagnostic["description"]
    assert len(json.dumps(diagnostic, ensure_ascii=False).encode()) < 8192
    assert "objectId" not in diagnostic and "unexported" not in json.dumps(receipt)
    assert receipt["state_uncertain"] is True and receipt["resource_review_required"] is True
    assert receipt["cleanup"]["object_group_released"] is True


@pytest.mark.parametrize("details", [None, [], "malformed", {"text": {}, "lineNumber": True, "columnNumber": -1,
    "url": [], "exception": {"type": [], "subtype": None, "description": {"objectId": "hidden"}}},
    {"text": "safe\0text", "exception": {"type": "object", "description": "invalid-surrogate\ud800"}}])
def test_malformed_exception_metadata_is_bounded_diagnostic_not_a_second_serialization_failure(environment, details):
    environment.server.fault = "exception_after_dispatch"
    # None is represented as an explicit protocol field for this malformed case.
    original = environment.server.response

    def malformed(connection, message):
        response = original(connection, message)
        if message["method"] == "Runtime.callFunctionOn" and message["params"]["functionDeclaration"] == I.CALL:
            response["exceptionDetails"] = copy.deepcopy(details)
        return response

    environment.server.response = malformed
    receipt = run(environment)
    diagnostic = receipt["error"]["remote_exception"]
    assert diagnostic["malformed"] is True
    assert set(diagnostic) <= {"malformed", "truncated", "text", "type", "subtype", "description", "source_url",
                               "line", "column", "fixed_template"}
    json.dumps(diagnostic, ensure_ascii=False).encode("utf-8")
    assert '"objectId"' not in json.dumps(diagnostic) and "hidden" not in json.dumps(diagnostic)
    assert receipt["state_uncertain"] is True and receipt["resource_review_required"] is True
    assert receipt["cleanup"]["object_group_released"] is True


def test_cdp_command_error_records_trusted_graph_template_and_preserves_uncertainty(environment):
    environment.server.fault = "graph_protocol_error"
    environment.server.command_error = {"code": -32000, "message": "Object couldn't be returned by value",
        "data": "Controlled serialization failure", "objectId": "never-export-command-handle"}
    request = {"operations": [{"op": "graph", "recipe": RECIPE, "max_properties": 8, "max_nodes": 2, "max_depth": 1},
                               {"op": "call", "actor": environment.actor}]}
    receipt = run(environment, operations=request)
    assert receipt["error"]["command_error"] == {"command": "Runtime.callFunctionOn", "fixed_template": "GRAPH",
        "code": -32000, "message": "Object couldn't be returned by value", "data": "Controlled serialization failure",
        "malformed": False, "truncated": False}
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is True
    assert receipt["resource_review_required"] is True and receipt["retry"] is False
    assert receipt["method_dispatch_attempts"] == 0 and not environment.server.dispatches
    assert receipt["cleanup"]["object_group_released"] is True
    assert all(connection.closed for connection in environment.server.connections)
    assert "never-export-command-handle" not in json.dumps(receipt)


@pytest.mark.parametrize("error", [None, [], {"code": True, "message": [], "data": {"objectId": "unexported"}},
    {"code": -32000, "message": "🧭" * 1000, "data": "x" * 6000},
    {"code": -32000, "message": "nul\0surrogate\ud800", "data": "safe"}])
def test_command_error_metadata_is_bounded_without_returning_structured_handles(environment, error):
    environment.server.fault = "graph_protocol_error"
    environment.server.command_error = error
    receipt = run(environment, operations={"operations": [{"op": "graph", "recipe": RECIPE, "max_properties": 8}]})
    diagnostic = receipt["error"]["command_error"]
    assert diagnostic["command"] == "Runtime.callFunctionOn" and diagnostic["fixed_template"] == "GRAPH"
    assert set(diagnostic) <= {"command", "fixed_template", "code", "message", "data", "malformed", "truncated"}
    for name, budget in (("message", 1024), ("data", 2048)):
        if name in diagnostic:
            assert len(diagnostic[name].encode()) <= budget
    assert len(json.dumps(diagnostic, ensure_ascii=False).encode()) < 4096
    assert "unexported" not in json.dumps(diagnostic) and '"objectId"' not in json.dumps(diagnostic)
    assert diagnostic["malformed"] or diagnostic["truncated"]
    assert receipt["state_uncertain"] is True and receipt["cleanup"]["object_group_released"] is True


@pytest.mark.parametrize("change", ["function_changed", "native_eval", "declaration_changed"])
def test_unattributable_or_changed_callable_is_not_dispatched(environment, change):
    if change == "declaration_changed":
        environment.declaration.write_text('{}')
        with pytest.raises(ValueError, match="declaration changed"):
            run(environment)
        assert not environment.server.connections
    else:
        environment.server.source = "function unrelated() { return 5; }" if change == "function_changed" else "function eval() { [native code] }"
        if change == "native_eval":
            environment.actor["function_sha256"] = hashlib.sha256(environment.server.source.encode()).hexdigest()
            R.write_json(environment.declaration, {"contract": I.call_contract(environment.actor)})
            environment.actor["signature_evidence"]["sha256"] = R._file_hash(environment.declaration)
        receipt = run(environment)
        assert receipt["status"] == "incomplete" and receipt["method_dispatch_attempts"] == 0
    assert not environment.server.dispatches


def test_actual_private_descriptor_data_can_be_read_without_exporting_remote_handles(environment):
    request = {"operations": [{"op": "read", "recipe": {"root": RECIPE["root"],
        "steps": [{"kind": "private_property", "name": "#secret"}]}, "return_type": "utf8"}]}
    receipt = run(environment, operations=request)
    assert receipt["status"] == "observed" and receipt["operations"] == [{"op": "read", "value": "private actual value", "type": "utf8"}]
    assert not environment.server.dispatches and '"objectId"' not in json.dumps(receipt)


@pytest.mark.parametrize("selector,count", [("#app", 2), ("body > #app", 1), ("#absent", 0)])
def test_dom_query_reports_actual_zero_one_or_duplicate_matches_without_any_app_evaluation(environment, selector, count):
    request = {"operations": [{"op": "dom_query", "selector": selector, "max_nodes": 8}]}
    receipt = run(environment, operations=request)
    assert receipt["status"] == "observed" and receipt["state_uncertain"] is False
    assert receipt["descriptor_traps_may_have_effects"] is False
    assert receipt["method_dispatch_attempts"] == receipt["getter_dispatch_attempts"] == 0
    row = receipt["operations"][0]
    assert row["total_match_count"] == count and len(row["nodes"]) == count
    assert row["truncated"] is False and row["live_node_ids_exported"] is False
    assert row["app_evaluations"] == row["getters_invoked"] == 0
    if count:
        assert row["nodes"][0] == {"tag": "DIV", "attributes": [["id", "app"], ["data-v-app", ""]],
                                   "attribute_count": 2, "attributes_truncated": False, "child_count": 1}
    encoded = json.dumps(receipt)
    assert all(marker not in encoded for marker in ('"nodeId"', '"backendNodeId"', '"nodeIds"'))
    assert "7101" not in json.dumps(row) and "8101" not in json.dumps(row)
    assert not any(item["method"] in {"Runtime.evaluate", "Runtime.callFunctionOn"} for item in environment.server.requests)
    assert not environment.server.dispatches
    assert receipt["cleanup"]["object_group_released"] is True
    assert all(connection.closed for connection in environment.server.connections)


def test_dom_query_node_and_attribute_previews_are_bounded_without_inventing_uniqueness(environment):
    environment.server.dom_nodes[7101]["attributes"].extend(["title", "🧭" * 1000])
    request = {"operations": [{"op": "dom_query", "selector": "#app", "max_nodes": 1}]}
    receipt = run(environment, operations=request)
    row = receipt["operations"][0]
    assert receipt["status"] == "observed" and row["total_match_count"] == 2
    assert len(row["nodes"]) == 1 and row["truncated"] is True
    assert row["nodes"][0]["attributes_truncated"] is True
    assert len(row["nodes"][0]["attributes"][-1][1].encode()) <= 1024
    assert len([item for item in environment.server.requests if item["method"] == "DOM.describeNode"]) == 1
    assert receipt["state_uncertain"] is False and receipt["actuated"] is False


@pytest.mark.parametrize("failure", ["document", "identity", "duplicate", "match_budget", "attributes", "child_count",
                                      "selected_id", "element_type", "protocol"])
def test_bad_dom_metadata_prevents_later_task_call_without_claiming_app_state_uncertainty(environment, failure):
    environment.server.dom_fault = failure
    request = {"operations": [{"op": "dom_query", "selector": "#app", "max_nodes": 8},
                               {"op": "call", "actor": environment.actor}]}
    receipt = run(environment, operations=request)
    assert receipt["status"] == "incomplete" and receipt["operations"] == []
    assert receipt["state_uncertain"] is False and receipt["resource_review_required"] is False
    assert receipt["actuated"] is False and receipt["retry"] is False
    assert not environment.server.dispatches
    assert not any(item["method"] in {"Runtime.evaluate", "Runtime.callFunctionOn"} for item in environment.server.requests)
    assert receipt["cleanup"]["object_group_released"] is True
    assert all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("operation", [{"op": "dom_query", "selector": "", "max_nodes": 8},
    {"op": "dom_query", "selector": "x" * 1025, "max_nodes": 8},
    {"op": "dom_query", "selector": "#app", "max_nodes": 0},
    {"op": "dom_query", "selector": "#app", "max_nodes": 33},
    {"op": "dom_query", "selector": "#app", "max_nodes": True},
    {"op": "dom_query", "selector": "#app", "max_nodes": 8, "nodeId": 17}])
def test_dom_query_schema_rejects_oversized_or_live_id_inputs_before_transport(environment, operation):
    with pytest.raises(ValueError):
        run(environment, operations={"operations": [operation]})
    assert not environment.server.connections and not environment.server.dispatches


@pytest.mark.parametrize("change", ["remote_endpoint", "object_id", "javascript", "bad_later_argument"])
def test_bad_input_fails_before_transport_or_earlier_actuation(environment, change):
    actor = copy.deepcopy(environment.actor)
    if change == "object_id":
        actor["receiver"] = {"objectId": "old-owned-context-id"}
    elif change == "javascript":
        actor["javascript"] = "model.changeState()"
    elif change == "bad_later_argument":
        actor["arguments"][1]["value"] = "true"
    else:
        config = R.load_json(environment.config)
        config["endpoint"] = "http://192.168.66.3:28123"
        R.write_json(environment.config, config)
    with pytest.raises(ValueError):
        I.collect(environment.bundle, environment.out / "denied", {"operations": [{"op": "call", "actor": environment.actor},
            {"op": "call", "actor": actor}]}, config_file=environment.config)
    assert not environment.server.connections and not environment.server.dispatches


@pytest.mark.parametrize("value", [10**1000, -0.0, float("inf"), float("nan")])
@pytest.mark.parametrize("kind", ["number", "json"])
def test_unrepresentable_number_is_denied_before_transport(environment, value, kind):
    actor = copy.deepcopy(environment.actor)
    actor["arguments"][0] = {"type": kind, "value": value}
    with pytest.raises(ValueError):
        run(environment, operations={"operations": [{"op": "call", "actor": actor}]})
    assert not environment.server.connections and not environment.server.dispatches


@pytest.mark.parametrize("change", ["operator_config", "declaration"])
def test_pinned_inputs_changed_after_dispatch_make_result_uncertain_without_retry(environment, monkeypatch, change):
    original = environment.server.response

    def changed(connection, message):
        response = original(connection, message)
        if message["method"] == "Runtime.callFunctionOn" and message["params"]["functionDeclaration"] == I.CALL:
            if change == "operator_config":
                config = R.load_json(environment.config)
                config["endpoint"] = "http://127.0.0.1:28124"
                R.write_json(environment.config, config)
            else:
                environment.declaration.write_text('{}')
        return response

    monkeypatch.setattr(environment.server, "response", changed)
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is True
    assert receipt["resource_review_required"] is True and receipt["retry"] is False
    assert environment.server.dispatches == [[7, True]]
    assert all(connection.closed for connection in environment.server.connections)
    assert receipt["cleanup"]["object_group_released"] is True


@pytest.mark.parametrize("suffix", ["?another=state", "#another-state"])
def test_query_or_fragment_is_not_the_exact_configured_renderer(environment, suffix):
    environment.server.target["url"] += suffix
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is False
    assert not environment.server.dispatches and not environment.server.connections


def exact_suffix(environment):
    suffix = {"query": "profile=%2Fvm%2Fresearch&wid=1&debug=0", "fragment": "/editor"}
    config = R.load_json(environment.config)
    config["document_suffix"] = copy.deepcopy(suffix)
    R.write_json(environment.config, config)
    environment.server.url += "?" + suffix["query"] + "#" + suffix["fragment"]
    environment.server.target["url"] = environment.server.url
    return suffix


def test_explicit_exact_document_suffix_is_pinned_and_invocable(environment):
    suffix = exact_suffix(environment)
    transport = I.transport_identity(environment.bundle, config_file=environment.config)
    assert transport["document_suffix"] == suffix
    assert I.transport_identity_for_target(R.fingerprint(environment.bundle), config_file=environment.config) == transport
    receipt = run(environment)
    assert receipt["status"] == "executed" and receipt["transport"] == transport
    assert receipt["endpoint_identity"]["target"]["url"] == environment.server.url
    assert receipt["endpoint_identity"]["execution_context"]["frame_url"] == environment.server.url
    assert I.receipt_valid(receipt, environment.actor, receipt["target"], receipt["adapter"], transport)
    assert I.receipt_valid(receipt, environment.actor, receipt["target"], receipt["adapter"], transport, check_files=False)
    receipt["transport"]["document_suffix"]["fragment"] = "/another-editor"
    assert not I.receipt_valid(receipt, environment.actor, receipt["target"], receipt["adapter"], receipt["transport"], check_files=False)


def test_standard_cdp_split_fragment_binds_effective_and_raw_frame(environment):
    exact_suffix(environment)
    base, fragment = environment.server.url.split("#", 1)
    frame = {"id": "frame-one", "url": base, "urlFragment": "#" + fragment, "loaderId": "actual-loader"}
    environment.server.frame_override = frame
    receipt = run(environment)
    assert receipt["status"] == "executed"
    assert receipt["endpoint_identity"]["frame"] == frame
    assert receipt["endpoint_identity"]["execution_context"]["frame_url"] == environment.server.url
    assert I.receipt_valid(receipt, environment.actor, receipt["target"], receipt["adapter"], receipt["transport"])
    receipt["endpoint_identity"]["frame"]["urlFragment"] = "#/unbound"
    assert not I.receipt_valid(receipt, environment.actor, receipt["target"], receipt["adapter"], receipt["transport"], check_files=False)


@pytest.mark.parametrize("frame,expected", [
    ({"url": "file:///exact.html?raw=%2Fone"}, "file:///exact.html?raw=%2Fone"),
    ({"url": "file:///exact.html?raw=%2Fone", "urlFragment": "#/editor"}, "file:///exact.html?raw=%2Fone#/editor"),
    ({"url": "file:///exact.html#/legacy"}, "file:///exact.html#/legacy"),
    ({"url": "file:///exact.html", "urlFragment": ""}, "file:///exact.html"),
    ({"url": "file:///exact.html", "urlFragment": "#"}, "file:///exact.html#")])
def test_effective_frame_url_preserves_actual_raw_components(frame, expected):
    assert I._effective_frame_url(frame) == expected


@pytest.mark.parametrize("fragment", [None, 1, "/editor", "#/bad route", "#/line\nbreak", "#/nul\0", "#" + "x" * 4097])
def test_invalid_or_contradictory_frame_fragment_stops_before_any_app_evaluation(environment, fragment):
    exact_suffix(environment)
    base = environment.server.url.split("#", 1)[0]
    environment.server.frame_override = {"id": "frame-one", "url": base, "urlFragment": fragment}
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is False
    assert not environment.server.dispatches
    assert not any(row["method"] in {"Runtime.evaluate", "Runtime.callFunctionOn"} for row in environment.server.requests)


@pytest.mark.parametrize("fragment", ["", "#/editor", "#/contradiction"])
def test_full_url_plus_separate_fragment_is_not_silently_joined(environment, fragment):
    exact_suffix(environment)
    environment.server.frame_override = {"id": "frame-one", "url": environment.server.url, "urlFragment": fragment}
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is False
    assert not environment.server.dispatches


def test_raw_frame_changed_after_dispatch_is_uncertain_even_if_target_metadata_is_unchanged(environment, monkeypatch):
    exact_suffix(environment)
    base, fragment = environment.server.url.split("#", 1)
    environment.server.frame_override = {"id": "frame-one", "url": base, "urlFragment": "#" + fragment, "loaderId": "one"}
    original = environment.server.response

    def changed(connection, message):
        response = original(connection, message)
        if message["method"] == "Runtime.callFunctionOn" and message["params"]["functionDeclaration"] == I.CALL:
            environment.server.frame_override["loaderId"] = "two"
        return response

    monkeypatch.setattr(environment.server, "response", changed)
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is True
    assert receipt["resource_review_required"] is True and receipt["retry"] is False
    assert environment.server.dispatches == [[7, True]]


@pytest.mark.parametrize("change", ["query", "fragment", "wrong_path", "ambiguous"])
def test_exact_suffix_does_not_allow_route_drift_other_document_or_ambiguous_target(environment, change, monkeypatch):
    suffix = exact_suffix(environment)
    if change == "query":
        environment.server.target["url"] = environment.server.url.replace("wid=1", "wid=2")
    elif change == "fragment":
        environment.server.target["url"] = environment.server.url.replace("#/editor", "#/other")
    elif change == "wrong_path":
        environment.server.target["url"] = environment.server.url.replace("renderer.html", "other-document.html")
    else:
        environment.server.fault = "ambiguous_target"
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is False
    assert not environment.server.dispatches and not environment.server.connections


def test_suffix_changed_after_method_dispatch_remains_uncertain_without_retry(environment, monkeypatch):
    exact_suffix(environment)
    original = environment.server.response

    def changed(connection, message):
        response = original(connection, message)
        if message["method"] == "Runtime.callFunctionOn" and message["params"]["functionDeclaration"] == I.CALL:
            environment.server.target["url"] = environment.server.url.replace("#/editor", "#/after")
        return response

    monkeypatch.setattr(environment.server, "response", changed)
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is True
    assert receipt["resource_review_required"] is True and receipt["retry"] is False
    assert environment.server.dispatches == [[7, True]]
    assert receipt["cleanup"]["object_group_released"] is True


@pytest.mark.parametrize("suffix", [None, {}, {"query": "x=1"}, {"query": "", "fragment": "", "extra": "x"},
    {"query": 1, "fragment": ""}, {"query": "has space", "fragment": ""},
    {"query": "x=1#unbound", "fragment": ""}, {"query": "", "fragment": "line\nbreak"},
    {"query": "", "fragment": "nul\0value"}, {"query": "", "fragment": "x" * 4097}])
def test_malformed_explicit_suffix_is_denied_before_transport(environment, suffix):
    config = R.load_json(environment.config)
    config["document_suffix"] = suffix
    R.write_json(environment.config, config)
    with pytest.raises(ValueError):
        run(environment)
    assert not environment.server.connections and not environment.server.dispatches


def test_duplicate_renderer_pid_is_denied_before_app_evaluation(environment, monkeypatch):
    original = environment.server.response

    def duplicated(connection, message):
        response = original(connection, message)
        if message["method"] == "SystemInfo.getProcessInfo":
            response["processInfo"].append(copy.deepcopy(response["processInfo"][-1]))
        return response

    monkeypatch.setattr(environment.server, "response", duplicated)
    receipt = run(environment)
    assert receipt["status"] == "incomplete" and receipt["state_uncertain"] is False
    assert not environment.server.dispatches
    assert not any(row["method"] in {"Runtime.evaluate", "Runtime.callFunctionOn"} for row in environment.server.requests)


@pytest.mark.parametrize("change", ["frida_format", "unsafe_cleanup", "unmatched_return", "wrong_context_process", "missing_context_pin", "claimed_task_proof"])
def test_invocation_gate_rejects_other_provider_or_incomplete_attribution(environment, change):
    receipt = run(environment)
    if change == "frida_format":
        receipt["format"] = "anyuse.rea-runtime/1"
    elif change == "unsafe_cleanup":
        receipt["cleanup"]["object_group_released"] = False
    elif change == "unmatched_return":
        receipt["operations"][0]["value"] = 42
    elif change == "wrong_context_process":
        receipt["endpoint_identity"]["renderer_processes"][0]["uid"] += 1
    elif change == "missing_context_pin":
        receipt["endpoint_identity"].pop("execution_context")
    else:
        receipt["task_result_verified"] = True
    assert not I.receipt_valid(receipt, environment.actor, receipt["target"], receipt["adapter"], receipt["transport"], check_files=False)


def test_fixed_functions_run_in_real_v8_and_preserve_cycle_budget_getter_gate_typed_call():
    R.assert_guest()
    node = shutil.which("node")
    assert node, "guest's existing Node is required for the fixed-script behavioral test"
    templates = {name: getattr(I, name) for name in ("PROPERTY", "MAP_KEY", "MAP_KEYS", "FUNCTION_SOURCE", "OWN_PROPERTY",
        "ARRAY_COUNT", "PROTOTYPE_RELATION", "DIRECT_PROTOTYPE", "OWN_INVARIANTS", "CALL", "DESCRIBE", "GRAPH", "READ")}
    fixture = r'''
const fs = require('fs');
const assert = require('assert');
const source = JSON.parse(fs.readFileSync(0, 'utf8'));
const F = Object.fromEntries(Object.entries(source).map(([name, value]) => [name, eval('(' + value + ')')]));
let getterEffects = 0;
class InternalStore {
  constructor() {
    this.state = 'initial'; this.self = this;
    this.children = new Map([['chosen', {count: 3}]]); this.extra = {count: 4};
  }
  applyValue(value, enabled) { this.state = 'state:' + value + ':' + enabled; return this.state; }
  get dangerous() { ++getterEffects; return 'mutated'; }
}
(async () => {
  const state = new InternalStore();
  assert.strictEqual(F.PROPERTY.call(state, 'state'), 'initial');
  assert.throws(() => F.PROPERTY.call(state, 'dangerous'), /explicit_getter/);
  assert.strictEqual(getterEffects, 0);
  const graph = F.GRAPH.call(state, 2, 2, 8);
  assert(graph.nodes.length <= 2 && graph.truncated && graph.whole_graph_coverage === false);
  assert(graph.nodes[0].edges.find(row => row.name === 'self').cycle_or_reuse);
  const descriptors = F.DESCRIBE.call(state, 8, 1);
  assert(descriptors.properties.find(row => row.name === 'applyValue').function_source);
  assert.strictEqual(descriptors.getters_invoked, 0);
  assert.strictEqual(getterEffects, 0);
  assert.strictEqual(F.MAP_KEY.call(state.children, 'chosen').count, 3);
  assert.deepStrictEqual(F.MAP_KEYS.call(state.children, 2), {
    keys: [{type: 'string', value: 'chosen'}], unsupported_key_count: 0, truncated: false});
  assert.throws(() => F.MAP_KEY.call(state.children, 'missing'), /not_found/);
  const method = F.PROPERTY.call(state, 'applyValue');
  const methodSource = F.FUNCTION_SOURCE.call(method);
  const result = await F.CALL.call(method, state, [19, true], ['int32', 'bool'], methodSource, 'utf8');
  assert.deepStrictEqual(result, {completed: true, value: 'state:19:true'});
  assert.strictEqual(state.state, 'state:19:true');
  await assert.rejects(F.CALL.call(method, state, [19, 'true'], ['int32', 'bool'], methodSource, 'utf8'), /type_mismatch/);
  await assert.rejects(F.CALL.call(method, state, [19, false], ['int32', 'bool'], methodSource + ' changed', 'utf8'), /method_changed/);
  assert.strictEqual(state.state, 'state:19:true');
  const evalSource = Function.prototype.toString.call(eval);
  await assert.rejects(F.CALL.call(eval, globalThis, ['globalThis.unreviewed = true'], ['utf8'], evalSource, 'bool'), /unattributable_native/);
  assert.strictEqual(globalThis.unreviewed, undefined);
  const jsonRead = value => F.READ.call(value, 'json');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(jsonRead({items: [1, true, null]}))),
    {value: {items: [1, true, null]}});
  assert.throws(() => jsonRead(new Map([['actual', 'state']])), /nonplain_object/);
  assert.throws(() => jsonRead([, 1]), /sparse_or_extended_array/);
  const extended = [1]; extended.extra = 'lost by JSON';
  assert.throws(() => jsonRead(extended), /sparse_or_extended_array/);
  assert.throws(() => jsonRead({[Symbol('hidden')]: 3}), /symbol_unsupported/);
  assert.throws(() => jsonRead({value: -0}), /unsupported_number/);
  assert.throws(() => jsonRead({value: Number.MAX_SAFE_INTEGER + 1}), /unsupported_number/);
  const symbolKey = Symbol('omitted-key');
  const primitiveState = {symbolValue: Symbol('opaque-value'), bigintValue: 1n, undefinedValue: undefined,
    nonfiniteValue: NaN, infiniteValue: Infinity, unsafeInteger: Number.MAX_SAFE_INTEGER + 1,
    negativeZero: -0, trueValue: true, nullValue: null, zeroValue: 0, textValue: 'actual'};
  primitiveState[symbolKey] = {hidden: 'must not claim whole coverage'};
  const primitiveGraph = F.GRAPH.call(primitiveState, 1, 0, 32);
  const primitiveDescription = F.DESCRIBE.call(primitiveState, 32, 0);
  assert.strictEqual(primitiveGraph.nodes[0].symbol_key_count, 1);
  assert.strictEqual(primitiveGraph.nodes[0].symbol_keys_omitted, true);
  assert.strictEqual(primitiveGraph.symbol_keys_exported, false);
  assert.strictEqual(primitiveGraph.traversal_scope, 'own_string_data_descriptors');
  assert.strictEqual(primitiveDescription.symbol_key_count, 1);
  assert.strictEqual(primitiveDescription.symbol_keys_omitted, true);
  for (const rows of [primitiveGraph.nodes[0].edges, primitiveDescription.properties]) {
    for (const [name, reason] of [['symbolValue', 'symbol'], ['bigintValue', 'bigint'], ['undefinedValue', 'undefined'],
      ['nonfiniteValue', 'nonfinite_number'], ['infiniteValue', 'nonfinite_number'],
      ['unsafeInteger', 'unsafe_integer'], ['negativeZero', 'negative_zero']]) {
      const row = rows.find(item => item.name === name);
      assert.strictEqual(row.value_unsupported, true);
      assert.strictEqual(row.unsupported_reason, reason);
      assert.strictEqual(Object.prototype.hasOwnProperty.call(row, 'value'), false);
    }
    assert.strictEqual(rows.find(row => row.name === 'nullValue').value, null);
    assert.strictEqual(rows.find(row => row.name === 'zeroValue').value, 0);
    assert.strictEqual(rows.find(row => row.name === 'trueValue').value, true);
    assert.strictEqual(rows.find(row => row.name === 'textValue').value, 'actual');
  }
  JSON.stringify(primitiveGraph);
  JSON.stringify(primitiveDescription);
  class GuardedStore {
    constructor() { this.role = 'first'; this.state = 'untouched'; }
    apply(value) { this.state = value; return this.state; }
  }
  const guarded = new GuardedStore(), guardedMethod = GuardedStore.prototype.apply;
  assert.strictEqual(F.PROTOTYPE_RELATION.call(GuardedStore, GuardedStore.prototype), true);
  assert.strictEqual(F.PROTOTYPE_RELATION.call(Object, Object.prototype), false);
  assert.strictEqual(F.PROTOTYPE_RELATION.call(Function, Function.prototype), false);
  assert.throws(() => F.OWN_PROPERTY.call(Object.create({inherited: 1}), 'inherited'), /required_own_data/);
  assert.strictEqual(F.ARRAY_COUNT.call([guarded, {}]), 2);
  assert.strictEqual(F.DIRECT_PROTOTYPE.call(guarded, GuardedStore.prototype), true);
  const guardedSource = Function.prototype.toString.call(guardedMethod);
  const guard = [{constructor_source: Function.prototype.toString.call(GuardedStore),
    own_invariants: [{name: 'role', type: 'utf8', equals: 'first'}]}];
  const guardedCall = candidate => F.CALL.call(guardedMethod, candidate, ['must-not-apply'], ['utf8'],
    guardedSource, 'utf8', guard, candidate, GuardedStore, GuardedStore.prototype);
  guarded.role = 'changed';
  await assert.rejects(guardedCall(guarded), /preconditions_changed_before_dispatch/);
  assert.strictEqual(guarded.state, 'untouched');
  guarded.role = 'first';
  Object.setPrototypeOf(guarded, {});
  await assert.rejects(guardedCall(guarded), /preconditions_changed_before_dispatch/);
  Object.setPrototypeOf(guarded, GuardedStore.prototype);
  GuardedStore.prototype.constructor = function ChangedConstructor() {};
  await assert.rejects(guardedCall(guarded), /preconditions_changed_before_dispatch/);
  GuardedStore.prototype.constructor = GuardedStore;
  Object.defineProperty(guarded, 'role', {get() { ++getterEffects; return 'first'; }, configurable: true});
  await assert.rejects(guardedCall(guarded), /preconditions_changed_before_dispatch/);
  assert.strictEqual(getterEffects, 0);
  Object.defineProperty(guarded, 'role', {value: 'first', configurable: true});
  let proxyDescriptorEffects = 0;
  Object.defineProperty(guarded, 'constructor', {value: new Proxy(GuardedStore, {
    getOwnPropertyDescriptor() { ++proxyDescriptorEffects; throw Error('must not follow proxy constructor'); }
  }), configurable: true});
  await assert.rejects(guardedCall(guarded), /preconditions_changed_before_dispatch/);
  assert.strictEqual(proxyDescriptorEffects, 0);
  delete guarded.constructor;
  class DerivedStore extends GuardedStore {}
  DerivedStore.prototype.role = 'first';
  assert.strictEqual(F.DIRECT_PROTOTYPE.call(new DerivedStore(), GuardedStore.prototype), false);
  assert.strictEqual(F.DIRECT_PROTOTYPE.call(DerivedStore.prototype, GuardedStore.prototype), true);
  assert.strictEqual(F.OWN_INVARIANTS.call(DerivedStore.prototype, guard[0].own_invariants), false);
  await assert.rejects(guardedCall(DerivedStore.prototype), /preconditions_changed_before_dispatch/);
  assert.strictEqual(Object.prototype.hasOwnProperty.call(DerivedStore.prototype, 'state'), false);
  assert.strictEqual(guarded.state, 'untouched');
  process.stdout.write(JSON.stringify({passed: true, getterEffects, nodes: graph.nodes.length}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", fixture], input=json.dumps(templates), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True


def test_real_v8_scope_recipe_finds_private_closure_store_and_reuses_after_restart(tmp_path):
    R.assert_guest()
    node = shutil.which("node")
    assert node, "guest's existing Node is required for the real closure-scope test"
    fixture = r'''
const fs = require('fs');
const inspector = require('inspector');
const readline = require('readline');
const stores = [];
class PrivateStore {
  constructor(initial) {
    this.tag = initial.split('-')[0]; this.state = initial;
    this.unrelated = '\ud800';
    this.child = {state: 'child-untouched', applyValue: PrivateStore.prototype.applyValue};
  }
  applyValue(value, enabled) { if (enabled) this.state = value; return this.state; }
  get shiftingChild() { this.tag = 'shifted'; return this.child; }
}
class DerivedStore extends PrivateStore {}
DerivedStore.prototype.tag = 'first';
globalThis.__prototype_fixture = {prototype: PrivateStore.prototype, derived: new DerivedStore('derived-initial')};
function factory(initial) {
  const capturedStore = new PrivateStore(initial);
  stores.push(capturedStore);
  return function retainedAnchor() { return capturedStore; };
}
globalThis.__scope_fixture = {first: factory('first-initial'), second: factory('second-initial')};
const observer = process.argv[1];
const observe = () => {
  fs.writeFileSync(observer, JSON.stringify(stores.map(store => store.state)));
  fs.writeFileSync(observer + '.children', JSON.stringify(stores.map(store => store.child.state)));
};
observe();
const timer = setInterval(observe, 10);
const session = new inspector.Session(); session.connect();
const send = message => process.stdout.write(JSON.stringify(message) + '\n');
session.on('inspectorNotification', send);
const input = readline.createInterface({input: process.stdin});
input.on('line', line => {
  const request = JSON.parse(line);
  session.post(request.method, request.params, (error, result) => {
    send(error ? {id: request.id, error: {code: error.code, message: error.message}} : {id: request.id, result});
  });
});
input.on('close', () => { clearInterval(timer); session.disconnect(); });
'''
    anchor = {"root": {"kind": "global", "name": "__scope_fixture"},
              "steps": [{"kind": "property", "name": "first", "access": "data"}]}
    retained_recipe = None

    class NodeSocket:
        def __init__(self, observer):
            self.process = subprocess.Popen([node, "-e", fixture, str(observer)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.buffer = b""

        def send(self, raw):
            self.process.stdin.write(raw.encode() + b"\n")
            self.process.stdin.flush()

        def recv(self, timeout):
            deadline = time.monotonic() + timeout
            while b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise TimeoutError("real V8 scope fixture did not answer")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("real V8 scope fixture closed unexpectedly")
                self.buffer += chunk
                if len(self.buffer) > I.MAX_WIRE_MESSAGE * 2:
                    raise ValueError("real V8 fixture output exceeded its bounded buffer")
            line, self.buffer = self.buffer.split(b"\n", 1)
            return line.decode()

        def close(self):
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            assert self.process.returncode == 0, self.process.stderr.read().decode()
            self.process.stdout.close()
            self.process.stderr.close()

    for generation in range(2):
        observer = tmp_path / ("actual-store-state-" + str(generation) + ".json")
        page = I._CDP(NodeSocket(observer), time.monotonic() + 20)
        group = "scope-fixture-" + str(generation)
        transaction = None
        try:
            page.call("Runtime.enable")
            contexts = [event["params"]["context"] for event in page.events if event["method"] == "Runtime.executionContextCreated"]
            assert len(contexts) == 1 and contexts[0].get("uniqueId")
            transaction = I._Transaction(page, contexts[0]["uniqueId"], group)
            if retained_recipe is None:
                observed = transaction.operation({"op": "function_scopes", "recipe": anchor,
                    "function_sha256": ANCHOR_SHA, "max_scopes": 32, "max_bindings": 32})
                matches = [scope for scope in observed["scopes"] if any(binding["name"] == "capturedStore"
                    for binding in scope.get("bindings", []))]
                assert len(matches) == 1
                scope = matches[0]
                retained_recipe = {"root": copy.deepcopy(anchor["root"]), "steps": [*copy.deepcopy(anchor["steps"]),
                    {"kind": "scope_binding", "function_sha256": ANCHOR_SHA, "name": "capturedStore",
                     "scope": {"index": scope["index"], "description": scope["description"],
                        "chain_sha256": observed["chain_sha256"], "bindings_sha256": scope["bindings_sha256"]}}]}
                I.validate_recipe(retained_recipe)
                assert observed["live_ids_exported"] is False and observed["primitive_values_exported"] is False
            read_recipe = copy.deepcopy(retained_recipe)
            read_recipe["steps"].append({"kind": "property", "name": "state", "access": "data"})
            read = transaction.operation({"op": "read", "recipe": read_recipe, "return_type": "utf8"})
            assert read["value"] == "first-initial"
            method = transaction.operation({"op": "method", "recipe": retained_recipe, "method": "applyValue"})
            declaration = tmp_path / ("scope-method-" + str(generation) + ".json")
            actor = {"kind": "inspector_method", "receiver": retained_recipe, "method": "applyValue",
                "function_sha256": method["function_sha256"], "arguments": [{"type": "utf8", "value": "actual-change"},
                    {"type": "bool", "value": True}], "return_type": "utf8",
                "signature_evidence": {"kind": "reviewed_js", "file": str(declaration), "sha256": "0" * 64, "pointer": "/contract"}}
            R.write_json(declaration, {"contract": I.call_contract(actor)})
            actor["signature_evidence"]["sha256"] = R._file_hash(declaration)
            I.validate_request(I.invocation_request(actor))
            assert transaction.operation({"op": "call", "actor": actor})["value"] == "actual-change"
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    actual = json.loads(observer.read_text())
                except (ValueError, OSError):
                    actual = None
                if actual == ["actual-change", "second-initial"]:
                    break
                time.sleep(0.01)
            assert actual == ["actual-change", "second-initial"]
            assert transaction.method_attempts == 1 and transaction.getter_attempts == 0
            assert "objectId" not in json.dumps(retained_recipe)
            prototype_recipe = {"root": {"kind": "global", "name": "__prototype_fixture"},
                                "steps": [{"kind": "property", "name": "prototype", "access": "data"}]}
            constructor = transaction.operation({"op": "method", "recipe": prototype_recipe, "method": "constructor"})
            discovery = transaction.operation({"op": "prototype_instances", "recipe": prototype_recipe,
                "constructor_sha256": constructor["function_sha256"], "max_instances": 16, "max_properties": 8})
            assert discovery["complete_candidate_count"] >= 3
            assert any(item.get("direct_prototype") is False for item in discovery["instances"])
            assert any(item.get("unsupported_reason") == "own_constructor_descriptor" for item in discovery["instances"])
            assert any(property.get("unsupported_reason") == "non_utf8_string"
                       for item in discovery["instances"] for property in item.get("properties", []))
            instance_receiver = copy.deepcopy(prototype_recipe)
            instance_receiver["steps"].append({"kind": "unique_instance", "constructor_sha256": constructor["function_sha256"],
                "max_candidates": 64, "own_invariants": [{"name": "tag", "type": "utf8", "equals": "first"}]})
            instance_actor = copy.deepcopy(actor)
            instance_actor["receiver"] = copy.deepcopy(instance_receiver)
            instance_actor["arguments"][0]["value"] = "instance-change"
            singleton = copy.deepcopy(instance_actor)
            singleton["receiver"]["steps"][-1]["own_invariants"] = []
            with pytest.raises(I.ProtocolError, match="complete instance candidate count"):
                transaction.operation({"op": "call", "actor": singleton})
            assert transaction.operation({"op": "call", "actor": instance_actor})["value"] == "instance-change"
            selected = transaction.instance_guards[0]["candidate"]
            getter_source = transaction.function(selected, I.GETTER_SOURCE, ["shiftingChild"], by_value=True)["value"]
            guarded_actor = copy.deepcopy(instance_actor)
            guarded_actor["receiver"]["steps"].append({"kind": "property", "name": "shiftingChild", "access": "getter",
                "getter_sha256": hashlib.sha256(getter_source.encode()).hexdigest()})
            I.validate_request(I.invocation_request(guarded_actor))
            with pytest.raises(I.ProtocolError, match="evaluation failed") as failed:
                transaction.operation({"op": "call", "actor": guarded_actor})
            assert failed.value.remote_exception["fixed_template"] == "CALL"
            assert "selected_instance_preconditions_changed_before_dispatch" in failed.value.remote_exception["description"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    actual = json.loads(observer.read_text())
                    children = json.loads(Path(str(observer) + ".children").read_text())
                except (ValueError, OSError):
                    actual = children = None
                if actual == ["instance-change", "second-initial"] and children == ["child-untouched", "child-untouched"]:
                    break
                time.sleep(0.01)
            assert actual == ["instance-change", "second-initial"] and children == ["child-untouched", "child-untouched"]
            assert transaction.heap_queries == 4 and transaction.getter_attempts == 1
        finally:
            try:
                page.call("Runtime.releaseObjectGroup", {"objectGroup": group}, cleanup=True)
            finally:
                if transaction:
                    transaction.live_ids.clear()
                page.close()


if __name__ == "__main__":
    R.assert_guest()
    raise SystemExit(pytest.main([__file__, "-q"]))


def dom_root_request(selector):
    return {'operations': [{'op': 'describe', 'recipe': {'root': {'kind': 'dom', 'selector': selector}, 'steps': []},
                            'max_properties': 8}]}


@pytest.mark.parametrize('selector,count', [('#absent', 0), ('#app', 2)])
def test_dom_root_zero_or_multiple_matches_are_typed_closed_preflight_without_runtime(environment, selector, count):
    receipt = run(environment, operations=dom_root_request(selector))
    assert receipt['status'] == 'incomplete' and receipt['state_uncertain'] is False
    assert receipt['resource_review_required'] is False and receipt['actuated'] is False
    assert receipt['runtime_evaluation_attempts'] == receipt['getter_dispatch_attempts'] == receipt['method_dispatch_attempts'] == 0
    assert receipt['descriptor_traps_may_have_effects'] is False
    assert receipt['error']['type'] == 'DOMRootNotUnique'
    assert receipt['preflight_failure'] == {'format': 'anyuse.rea-inspector-preflight-failure/1',
        'kind': 'dom_root_not_unique', 'operation_index': 0, 'selector': selector, 'match_count': count,
        'context_revalidated': True, 'live_node_ids_exported': False}
    assert receipt['after_identity'] == receipt['identity'] == environment.main
    assert receipt['operations'] == [] and receipt['proof_created'] is False and receipt['task_result_verified'] is False
    assert receipt['cleanup'] == {'object_group_released': True, 'page_socket_closed': True,
        'browser_socket_closed': True, 'requested_evaluations_completed': True,
        'remote_execution_cancelled': False, 'local_live_ids_discarded': True}
    assert all(connection.closed for connection in environment.server.connections)
    assert not any(row['method'] in {'Runtime.evaluate', 'Runtime.callFunctionOn', 'DOM.resolveNode'} for row in environment.server.requests)
    assert '"nodeId"' not in json.dumps(receipt) and '"objectId"' not in json.dumps(receipt)
    assert not environment.server.dispatches


def test_dom_root_single_match_uses_exact_context_group_node_resolution_without_global_evaluation(environment):
    receipt = run(environment, operations=dom_root_request('body > #app'))
    assert receipt['status'] == 'observed' and receipt['state_uncertain'] is False
    assert receipt['runtime_evaluation_attempts'] == 1  # The existing descriptor template only.
    assert receipt['getter_dispatch_attempts'] == receipt['method_dispatch_attempts'] == 0
    commands = environment.server.requests
    assert not any(row['method'] == 'Runtime.evaluate' for row in commands)
    assert not any(row['method'] == 'Runtime.callFunctionOn' and row['params']['functionDeclaration'] == I.DOM_ROOT for row in commands)
    resolved = [row['params'] for row in commands if row['method'] == 'DOM.resolveNode']
    released = [row['params']['objectGroup'] for row in commands if row['method'] == 'Runtime.releaseObjectGroup']
    assert len(resolved) == 1 and resolved[0]['nodeId'] == 7101 and resolved[0]['executionContextId'] == 1
    assert [resolved[0]['objectGroup']] == released
    assert all(connection.closed for connection in environment.server.connections)
    assert '"objectId"' not in json.dumps(receipt) and '"nodeId"' not in json.dumps(receipt)
    assert not environment.server.dispatches


@pytest.mark.parametrize('failure', ['protocol', 'resolve', 'resolve_wrapper', 'resolve_context', 'release'])
def test_dom_root_protocol_resolution_context_or_cleanup_failure_still_requires_review(environment, failure):
    if failure == 'release':
        environment.server.fault = 'release_failure'; selector = '#absent'
    else:
        environment.server.dom_fault = failure; selector = 'body > #app'
    receipt = run(environment, operations=dom_root_request(selector))
    assert receipt['status'] == 'incomplete' and receipt['resource_review_required'] is True
    assert receipt['state_uncertain'] is True and receipt['actuated'] is None
    assert receipt['runtime_evaluation_attempts'] == receipt['getter_dispatch_attempts'] == receipt['method_dispatch_attempts'] == 0
    assert not environment.server.dispatches
    assert not any(row['method'] in {'Runtime.evaluate', 'Runtime.callFunctionOn'} for row in environment.server.requests)
    assert all(connection.closed for connection in environment.server.connections)


def test_dom_root_ambiguity_after_prior_evaluation_preserves_sticky_uncertainty(environment):
    first = dom_root_request('body > #app')['operations'][0]
    second = dom_root_request('#absent')['operations'][0]
    receipt = run(environment, operations={'operations': [first, second]})
    assert receipt['status'] == 'incomplete' and receipt['state_uncertain'] is True and receipt['resource_review_required'] is True
    assert receipt['runtime_evaluation_attempts'] == 1 and len(receipt['operations']) == 1
    assert receipt['getter_dispatch_attempts'] == receipt['method_dispatch_attempts'] == 0
    assert receipt['preflight_failure']['operation_index'] == 1 and receipt['preflight_failure']['context_revalidated'] is False
    assert receipt['actuated'] is None and not environment.server.dispatches


def test_dom_root_known_count_is_not_admitted_when_owned_process_changes_during_preflight(environment, monkeypatch):
    original = environment.server.response
    def change(connection, message):
        result = original(connection, message)
        if message['method'] == 'DOM.querySelectorAll':
            environment.main['launch_time'] += 1
        return result
    monkeypatch.setattr(environment.server, 'response', change)
    receipt = run(environment, operations=dom_root_request('#absent'))
    assert receipt['state_uncertain'] is True and receipt['resource_review_required'] is True
    assert receipt['runtime_evaluation_attempts'] == 0 and receipt['preflight_failure']['context_revalidated'] is False
    assert 'preflight_revalidation_error' in receipt
    assert not environment.server.dispatches
