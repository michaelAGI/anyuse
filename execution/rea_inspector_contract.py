"""Retain declared JavaScript argument types against actual session evidence.

This is a bounded data-only admission step. Types are model-declared, never
automatically recovered or independently reviewed. The operation neither calls
the inspector nor creates task proof. Invocation still resolves a fresh receiver
and compares the actual method body; ordinary result/proof gates remain intact.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit

from . import rea_actor_evidence as AE
from . import rea_inspector_bridge as I
from . import rea_mapping as R
from . import rea_runtime_bridge as B

FORMAT = "anyuse.rea-inspector-model-declaration/1"
KIND = "declared_js"
TYPE_BASIS = "model_declared_not_independently_reviewed"
MAX_DECLARATION_BYTES = 256 * 1024
MAX_ACTOR_BYTES = 64 * 1024
MAX_FILES = 64
MAX_TOTAL_BYTES = 64 * 1024**2
ACTOR_KEYS = {"kind", "receiver", "method", "function_sha256", "arguments", "return_type"}
_BINDING_KEYS = ("bundle", "target", "task", "params", "plan", "success_criteria",
                 "workflow", "observer_profile_sha256", "frozen_files")


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _same(left, right):
    return R.digest(left) == R.digest(right)


class _Reader:
    def __init__(self):
        self.deadline = time.monotonic() + 15
        self.files = {}

    def read(self, file, expected=None):
        try:
            raw, pin = AE._read(file, expected, self.deadline)
        except (AE.EvidenceIntegrityError, OSError, TypeError) as error:
            raise ValueError("inspector declaration evidence changed or is unavailable") from error
        old = self.files.get(pin["file"])
        _need(old is None or _same(old, pin), "inspector declaration evidence changed between reads")
        self.files[pin["file"]] = pin
        _need(len(self.files) <= MAX_FILES and sum(p["bytes"] for p in self.files.values()) <= MAX_TOTAL_BYTES,
              "inspector declaration evidence closure exceeds its bound")
        return raw, pin

    def json(self, file, expected=None):
        raw, pin = self.read(file, expected)
        try:
            return AE._json(raw), pin
        except (AE.EvidenceIntegrityError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("inspector declaration needs strict bounded JSON") from error


def _binding(state):
    return R.digest({key: state[key] for key in _BINDING_KEYS if key in state})


def _sources(reader):
    modules = {"declaration": __file__, "inspector": I.__file__, "mapping": R.__file__,
               "evidence_reader": AE.__file__, "runtime_bridge": B.__file__}
    return {role: reader.read(Path(file).resolve())[1] for role, file in modules.items()}


def _unsigned(actor, params):
    _need(type(actor) is dict and set(actor) == ACTOR_KEYS, "declare an inspector actor without signature_evidence")
    _need(len(json.dumps(actor, ensure_ascii=False, allow_nan=False).encode()) <= MAX_ACTOR_BYTES,
          "declared inspector actor exceeds its byte bound")
    temporary = {**copy.deepcopy(actor), "signature_evidence": {"kind": KIND,
        "file": "/anyuse-inspector-declaration.json", "sha256": "0" * 64, "pointer": "/contract"}}
    I.validate_actor(temporary, allow_bindings=True)
    _need(type(params) is list and len(params) <= 16 and all(type(p) is dict and type(p.get("name")) is str for p in params),
          "frozen parameter declaration required")
    names = {p["name"] for p in params}
    for argument in actor["arguments"]:
        value = argument["value"]
        if type(value) is dict and set(value) == {"input"}:
            _need(value["input"] in names, "inspector type declaration names an undeclared input")
    return copy.deepcopy(actor)


def _registered(reader, root, state, name):
    _need(type(name) is str and name in state["artifacts"], "cite registered current-session inspector evidence")
    entry = state["artifacts"][name]
    file = root / name
    _need(file.is_relative_to(root) and type(entry) is dict and entry.get("file") == str(file),
          "registered inspector evidence escaped its session")
    return reader.json(file, entry)


def _identity(value):
    try:
        return dict(B._expected_identity(value))
    except (TypeError, ValueError) as error:
        raise ValueError("complete same-user inspector identity required") from error


def _current(state):
    bundle = Path(state["bundle"])
    _need(R.fingerprint(bundle) == state["target"], "inspector declaration target bytes changed")
    return {"identity": dict(B.process_identity(bundle, state["target"]["bundle_id"])),
            "adapter": I.adapter_identity(), "transport": I.transport_identity(str(bundle))}


def _endpoint(receipt, reader):
    """Validate retained attribution; this does not connect to an inspector."""
    identity, transport = receipt["identity"], receipt["transport"]
    _need(transport.get("kind") == "chromium_renderer_cdp", "inspector transport attribution required")
    I._keys(transport, {"kind", "endpoint", "relative_url", "config"}, {"document_suffix"})
    config_claim = transport["config"]
    _need(type(config_claim) is dict and set(config_claim) == {"file", "sha256", "bytes", "uid", "mode"},
          "owned private inspector configuration pin required")
    private_config, private_pin = I._config_for_target(receipt["target"], config_claim["file"])
    config, config_pin = reader.json(config_claim["file"], {key: config_claim[key] for key in ("file", "bytes", "sha256")})
    _need(_same(private_pin, config_claim) and _same(private_config, config), "private inspector configuration ownership changed")
    I._keys(config, {"format", "endpoint", "relative_url", "target"}, {"document_suffix"})
    _need(config["format"] == I.TRANSPORT and _same(config["target"], receipt["target"])
          and _same(I._transport(config, private_pin), transport), "retained inspector configuration differs")
    relative = Path(transport["relative_url"])
    _need(not relative.is_absolute() and relative.parts and ".." not in relative.parts,
          "app-relative inspector renderer required")
    _, port = I._endpoint(transport["endpoint"])
    endpoint = receipt.get("endpoint_identity", {})
    listener, target = endpoint.get("listener", {}), endpoint.get("target", {})
    _need(listener.get("pid") == identity["pid"] and listener.get("uid") == identity["uid"]
          and type(listener.get("listen_addresses")) is list and 1 <= len(listener["listen_addresses"]) <= 4
          and all(v in {f"127.0.0.1:{port}", f"[::1]:{port}"} for v in listener["listen_addresses"]),
          "inspector listener lacks the retained process binding")
    parsed = urlsplit(target.get("url", "")); suffix = I._document_suffix(transport.get("document_suffix", {"query": "", "fragment": ""}))
    expected = (Path(identity["bundle"]) / relative).resolve()
    _need(expected.is_relative_to(Path(identity["bundle"])) and target.get("type") == "page"
          and I._text(target.get("id"), 128) and parsed.scheme == "file" and parsed.netloc in {"", "localhost"}
          and parsed.query == suffix["query"] and parsed.fragment == suffix["fragment"]
          and Path(unquote(parsed.path)).resolve() == expected, "inspector selected renderer differs")
    context = endpoint.get("execution_context", {})
    _need(type(context) is dict and set(context) == {"unique_id", "numeric_id", "frame_id", "frame_url", "is_default"}
          and I._text(context["unique_id"], 256) and type(context["numeric_id"]) is int and context["numeric_id"] > 0
          and I._text(context["frame_id"], 128) and context["frame_url"] == target["url"] and context["is_default"] is True,
          "inspector default context binding differs")
    frame = endpoint.get("frame", {})
    _need(frame.get("id") == context["frame_id"] and I._effective_frame_url(frame) == context["frame_url"],
          "inspector frame binding differs")
    renderers = endpoint.get("renderer_processes")
    _need(type(renderers) is list and 1 <= len(renderers) <= 16 and all(type(v) is dict for v in renderers),
          "owned renderer process set required")
    _need(len({v.get("pid") for v in renderers}) == len(renderers), "duplicate inspector renderer identity")
    for row in renderers:
        _need(set(row) == {"pid", "ppid", "uid", "started_at", "executable"}
              and type(row["pid"]) is int and row["pid"] > 0 and type(row["ppid"]) is int and row["ppid"] > 0
              and row["uid"] == identity["uid"] and I._text(row["started_at"], 128)
              and type(row["executable"]) is str and Path(row["executable"]).is_relative_to(Path(identity["bundle"])),
              "retained inspector renderer is not owned by the app")


def _observation(reader, root, state, citation, actor, expected):
    _need(type(citation) is dict and set(citation) == {"artifact", "pointer"}, "cite an exact inspector method row")
    match = re.fullmatch(r"s([0-9]{4})/runtime/inspector[.]json", str(citation["artifact"]))
    point = re.fullmatch(r"/operations/(0|[1-9][0-9]*)", str(citation["pointer"]))
    _need(match is not None and point is not None, "cite a raw current-session inspector method operation")
    ordinal, index = int(match[1]), int(point[1])
    _need(ordinal < len(state["steps"]), "cited inspector step does not exist")
    row = state["steps"][ordinal]
    _need(row.get("step") == ordinal and row.get("status") == "completed", "cited inspector step is not completed")
    request = row.get("request", {})
    _need(request.get("op") == "discover" and request.get("provider") == "inspector", "method facts need an inspector discovery step")
    base = f"s{ordinal:04d}"
    original, request_pin = _registered(reader, root, state, base + "/request.json")
    feedback, feedback_pin = _registered(reader, root, state, base + "/feedback.json")
    _need(_same(original, request) and _same(feedback, row), "inspector discovery request/feedback changed")
    receipt, pin = _registered(reader, root, state, citation["artifact"])
    transaction = I.validate_request(request["request"])
    allowed = {"describe", "dom_query", "function_scopes", "graph", "map_keys", "method", "prototype_instances"}
    _need(all(op["op"] in allowed for op in transaction["operations"]), "task calls cannot supply method declaration evidence")
    _need(receipt.get("format") == I.FORMAT and receipt.get("status") in {"observed", "executed"}
          and receipt.get("bundle") == state["bundle"] and _same(receipt.get("target"), state["target"])
          and _same(receipt.get("request"), transaction) and receipt.get("request_sha256") == R.digest(transaction)
          and _same(receipt.get("identity"), expected["identity"]) and _same(receipt.get("after_identity"), expected["identity"])
          and _same(receipt.get("adapter"), expected["adapter"]) and _same(receipt.get("transport"), expected["transport"])
          and receipt.get("protocol_limits") == expected["adapter"].get("protocol_limits"),
          "inspector method evidence differs from its target, process, adapter or request")
    if "expected_identity" in receipt:
        _need(_same(_identity(receipt["expected_identity"]), expected["identity"]),
              "inspector method evidence has a contradictory expected process")
    getters = sum(step.get("kind") == "property" and step.get("access") == "getter"
                  for op in transaction["operations"] for step in op.get("recipe", {}).get("steps", []))
    _need(type(receipt.get("getter_dispatch_attempts")) is int and receipt["getter_dispatch_attempts"] == getters
          and type(receipt.get("method_dispatch_attempts")) is int and receipt["method_dispatch_attempts"] == 0
          and receipt.get("actuated") is bool(getters)
          and receipt["status"] == ("executed" if getters else "observed")
          and receipt.get("state_uncertain") is False and receipt.get("resource_review_required") is False
          and receipt.get("proof_created") is False and receipt.get("task_result_verified") is False
          and receipt.get("retry") is False and type(receipt.get("model_calls")) is int and receipt["model_calls"] == 0
          and receipt.get("error") is None, "inspector method observation is failed, uncertain or actuated a task")
    clean = {"object_group_released": True, "page_socket_closed": True, "browser_socket_closed": True,
             "requested_evaluations_completed": True, "remote_execution_cancelled": False, "local_live_ids_discarded": True}
    _need(receipt.get("cleanup") == clean, "inspector discovery cleanup is not confirmed")
    inner = row.get("feedback", {})
    _need(inner.get("status") == receipt["status"] and inner.get("provider") == "inspector"
          and inner.get("evidence_only") is True and inner.get("proof_created") is False and inner.get("error") is None,
          "inspector tool feedback differs from its completed observation")
    _endpoint(receipt, reader)
    outputs = receipt.get("operations")
    _need(type(outputs) is list and len(outputs) == len(transaction["operations"]) and index < len(outputs),
          "cited inspector method operation is missing")
    operation, observed = transaction["operations"][index], outputs[index]
    _need(operation == {"op": "method", "recipe": actor["receiver"], "method": actor["method"]},
          "observed method request does not use the declared receiver recipe")
    _need(type(observed) is dict and set(observed) == {"op", "method", "function_source", "function_sha256", "types_recovered_automatically"}
          and observed["op"] == "method" and observed["method"] == actor["method"]
          and I._text(observed["function_source"]) and "[native code]" not in observed["function_source"]
          and hashlib.sha256(observed["function_source"].encode()).hexdigest() == actor["function_sha256"] == observed["function_sha256"]
          and observed["types_recovered_automatically"] is False, "actual inspector method body/hash does not support this declaration")
    return {**copy.deepcopy(citation), "pin": pin, "request": request_pin, "feedback": feedback_pin, "step": ordinal}


def declare(session, state, request, folder):
    """Freeze model-declared call types using completed registered method facts."""
    R.assert_guest()
    _need(type(request) is dict and set(request) == {"op", "actor", "citations"}
          and request["op"] == "declare_inspector", "closed inspector declaration request required")
    root, folder = Path(session), Path(folder)
    _need(root.is_absolute() and root.resolve(strict=True) == root and folder.is_absolute()
          and folder.resolve(strict=True) == folder and folder.parent == root, "canonical current declaration step required")
    _need(type(state.get("steps")) is list and 1 <= len(state["steps"]) <= 256
          and all(row.get("step") == i for i, row in enumerate(state["steps"])), "bounded ordered session history required")
    step = len(state["steps"]) - 1
    _need(folder.name == f"s{step:04d}" and state["steps"][-1].get("status") == "running"
          and _same(state["steps"][-1].get("request"), request)
          and all(row.get("status") == "completed" for row in state["steps"][:-1]),
          "declaration requires its current running G step and completed history")
    reader = _Reader(); disk, _ = reader.json(root / "session.json")
    _need(_same(disk, state), "inspector declaration session snapshot changed")
    actor = _unsigned(request["actor"], state["params"])
    citations = request["citations"]
    _need(type(citations) is list and 1 <= len(citations) <= 16
          and len({R.digest(c) for c in citations}) == len(citations), "bounded distinct inspector method citations required")
    expected = _current(state)
    _identity(expected["identity"])
    observations = [_observation(reader, root, state, c, actor, expected) for c in citations]
    document = {"format": FORMAT, "type_basis": TYPE_BASIS, "proof_created": False, "actuated": False,
        "model_calls": 0, "session": str(root), "session_binding": _binding(state), "step": step,
        "request_sha256": R.digest(request), "target": copy.deepcopy(state["target"]),
        "identity": expected["identity"], "adapter": expected["adapter"], "transport": expected["transport"],
        "actor": actor, "contract": I.call_contract(actor), "observations": observations,
        "producer_sources": _sources(reader)}
    raw = (json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
    _need(len(raw) <= MAX_DECLARATION_BYTES and _same(reader.json(root / "session.json")[0], state)
          and _same(_current(state), expected), "inspector declaration inputs changed or exceed their bound")
    file = folder / "inspector-declaration.json"
    with file.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    pin = reader.read(file)[1]
    complete = {**actor, "signature_evidence": {"kind": KIND, "file": str(file), "sha256": pin["sha256"], "pointer": "/contract"}}
    return {"status": "declared", "actor": complete, "declaration": pin,
            "type_basis": TYPE_BASIS, "types_recovered_automatically": False,
            "independently_reviewed": False, "actuated": False, "proof_created": False, "model_calls": 0}


def validate_declaration(actor, *, state=None, current=False, bound=False):
    """Reopen declaration provenance; historical evidence is not a live pointer."""
    I.validate_actor(actor, allow_bindings=not bound)
    evidence = actor["signature_evidence"]
    _need(evidence["kind"] == KIND and evidence["pointer"] == "/contract", "model-declared inspector evidence required")
    reader = _Reader(); raw, pin = reader.read(evidence["file"])
    _need(pin["sha256"] == evidence["sha256"] and len(raw) <= MAX_DECLARATION_BYTES, "inspector declaration pin changed")
    document = reader.json(evidence["file"], pin)[0]
    keys = {"format", "type_basis", "proof_created", "actuated", "model_calls", "session", "session_binding", "step",
            "request_sha256", "target", "identity", "adapter", "transport", "actor", "contract", "observations", "producer_sources"}
    _need(type(document) is dict and set(document) == keys and document["format"] == FORMAT
          and document["type_basis"] == TYPE_BASIS and document["proof_created"] is False and document["actuated"] is False
          and type(document["model_calls"]) is int and document["model_calls"] == 0, "invalid inspector model declaration")
    root = Path(document["session"]); file = Path(evidence["file"])
    _need(root.is_absolute() and root.resolve(strict=True) == root and file.parent.parent == root
          and type(document["step"]) is int and 0 <= document["step"] < 256
          and file.parent.name == f"s{document['step']:04d}", "declaration escaped its producing session step")
    saved, _ = reader.json(root / "session.json")
    _need(_binding(saved) == document["session_binding"] and _same(saved["target"], document["target"])
          and type(saved.get("steps")) is list and document["step"] < len(saved["steps"]), "declaration session binding changed")
    if state is not None:
        _need(_same(state, saved), "inspector proposal session changed")
    declared = _unsigned(document["actor"], saved["params"])
    unsigned = {key: copy.deepcopy(actor[key]) for key in ACTOR_KEYS}
    _need(all(_same(unsigned[key], declared[key]) for key in ACTOR_KEYS - {"arguments"})
          and len(unsigned["arguments"]) == len(declared["arguments"]), "declared inspector receiver or call changed")
    if not bound:
        _need(_same(unsigned["arguments"], declared["arguments"]), "declared inspector input binding or constant changed")
    for value, original in zip(unsigned["arguments"], declared["arguments"]):
        binding = original["value"]
        _need(value["type"] == original["type"] and (type(binding) is dict and set(binding) == {"input"}
              or _same(value["value"], binding)), "declared inspector constant/type changed")
        if not bound and type(value["value"]) is dict and set(value["value"]) == {"input"}:
            _need(_same(value["value"], binding), "declared inspector input binding changed")
    _need(_same(document["contract"], I.call_contract(actor)) and _same(document["contract"], I.call_contract(declared)),
          "declared inspector call types changed")
    row = saved["steps"][document["step"]]
    expected_request = {"op": "declare_inspector", "actor": declared,
                        "citations": [{key: value[key] for key in ("artifact", "pointer")} for value in document["observations"]]}
    _need(row.get("status") == "completed" and row.get("step") == document["step"]
          and _same(row.get("request"), expected_request) and R.digest(expected_request) == document["request_sha256"],
          "declaration is not a completed registered G tool result")
    _need(_same(_registered(reader, root, saved, file.relative_to(root).as_posix())[1], pin),
          "inspector declaration is not registered")
    base = f"s{document['step']:04d}"
    _need(_same(_registered(reader, root, saved, base + "/request.json")[0], expected_request)
          and _same(_registered(reader, root, saved, base + "/feedback.json")[0], row), "declaration dispatch provenance changed")
    expected = {key: document[key] for key in ("identity", "adapter", "transport")}
    _identity(expected["identity"])
    _need(_same(document["producer_sources"], _sources(reader)) and _same(expected["adapter"], I.adapter_identity()),
          "inspector declaration producer/adapter changed")
    _need(type(document["observations"]) is list and 1 <= len(document["observations"]) <= 16,
          "inspector declaration method observations missing")
    for observation in document["observations"]:
        actual = _observation(reader, root, saved, {key: observation[key] for key in ("artifact", "pointer")}, declared, expected)
        _need(_same(actual, observation), "declared inspector observation pins changed")
    if current:
        _need(_same(_current(saved), expected), "inspector declaration current process or transport changed")
    # session.json is a mutable journal, not an immutable resource. Its stable
    # binding, registered completed prefix and each immutable artifact above
    # were reopened; subsequent legitimate G steps remain possible.
    reader.files.pop(str(root / "session.json"), None)
    return {"document": document, "resources": copy.deepcopy(reader.files), "type_basis": TYPE_BASIS,
            "independently_reviewed": False, "proof_created": False}


def resources(actor, *, bound=False):
    return validate_declaration(actor, bound=bound)["resources"]
