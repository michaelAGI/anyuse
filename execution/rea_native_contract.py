"""Explicit native ABI hypotheses backed by original instruction observations.

The caller must enable this experimental mode before discovery. A declaration
records the agent's types; it neither recovers nor independently proves an ABI.
The fixed runtime keeps its supported calling families, receiver/lifetime rules
and independent effect checks. No source program or raw pointer is accepted.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import time

from . import rea_actor_evidence as E
from . import rea_runtime_bridge as B
from . import rea_mapping as R

FORMAT = "anyuse.rea-native-model-declaration/1"
KIND = "declared_native"
TYPE_BASIS = "model_declared_not_independently_reviewed"
MAX_BYTES = 256 * 1024
BINDING_KEYS = ("bundle", "target", "task", "params", "plan", "success_criteria", "workflow",
                "observer_profile_sha256", "native_contract_mode")


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _binding(state):
    return R.digest({key: state[key] for key in BINDING_KEYS if key in state})


class Reader:
    def __init__(self):
        self.files, self.deadline = {}, time.monotonic() + 15

    def read(self, path, expected=None):
        raw, pin = E._read(Path(path), expected, self.deadline)
        _need(pin["bytes"] <= R.MAX_JSON_BYTES, "native declaration resource exceeds its bound")
        old = self.files.get(pin["file"])
        _need(old is None or old == pin, "native declaration resource changed between reads")
        self.files[pin["file"]] = pin
        _need(len(self.files) <= 32 and sum(p["bytes"] for p in self.files.values()) <= 32 * 1024**2,
              "native declaration evidence closure exceeded its bound")
        return raw, pin

    def data(self, path, expected=None):
        raw, pin = self.read(path, expected)
        return E._json(raw), pin

    def recheck(self):
        for pin in list(self.files.values()):
            self.read(pin["file"], pin)


def _unsigned(actor, params):
    required = {"kind", "target", "abi", "arguments", "return_type"}
    _need(type(actor) is dict and required <= set(actor) <= required | {"context"} and
          actor["kind"] == "native_function", "declare one native function without signature_evidence or compiler source")
    _need(type(actor["target"]) is dict and set(actor["target"]) == {"module", "offset"},
          "a declaration binds an exact measured original module offset")
    _need(len(json.dumps(actor, allow_nan=False).encode()) <= 64 * 1024, "native actor declaration exceeds its bound")
    temporary = {**copy.deepcopy(actor), "signature_evidence": {"kind": KIND,
        "file": "/anyuse-native-declaration.json", "sha256": "0" * 64, "pointer": "/contract"}}
    B.validate_actor(temporary, allow_bindings=True)
    names = {p["name"] for p in params}
    for argument in actor["arguments"]:
        for leaf in B.argument_leaves(argument):
            value = leaf["value"]
            if type(value) is dict and set(value) == {"input"}:
                _need(value["input"] in names, "native declaration names an undeclared input")
    return copy.deepcopy(actor)


def _observations(reader, root, state, citations, actor):
    _need(type(citations) is list and 1 <= len(citations) <= 16, "cite original current-session code evidence")
    found, observations = False, []
    module = actor["target"]["module"]
    _need(actor["abi"] != "unix64", "plain-arm64 original-code declarations cannot use the x86-64 calling convention")
    offset = actor["target"]["offset"]
    for citation in citations:
        _need(type(citation) is dict and set(citation) == {"artifact", "pointer"} and
              citation["artifact"] in state["artifacts"], "registered current-session citation required")
        name = citation["artifact"]; pin = state["artifacts"][name]
        _need(Path(pin["file"]) == root / name and (root / name).is_relative_to(root), "native citation escaped its session")
        value, actual = reader.data(pin["file"], pin)
        B._json_pointer(value, citation["pointer"])
        observations.append({"citation": copy.deepcopy(citation), "pin": actual})
        if value.get("format") != "anyuse.rea-original-instruction-range/1":
            continue
        index = int(Path(name).parts[0][1:])
        _need(index < len(state["steps"]) and state["steps"][index]["status"] == "completed" and
              state["steps"][index]["request"].get("op") == "static_evidence" and
              state["steps"][index]["request"]["request"].get("kind") == "original_code",
              "original code observation lacks its actual producing tool")
        _need(value.get("original_unchanged") is True and value.get("task_proof") is False,
              "original code observation is incomplete")
        image, row = value["image"], value["range"]
        if image["file"] == module["path"] and image["sha256"] == module["sha256"] and row["image_offset"] == offset:
            from . import rea_static_artifacts as A
            path = Path(module["path"])
            _need(path.is_relative_to(Path(state["bundle"])), "declared entry is outside the owned target bundle")
            original = A.OriginalImage(state["bundle"], path.relative_to(state["bundle"]).as_posix())
            try:
                raw = original.read(int(offset, 16), row["bytes"], kind="code")
                _need(raw.hex() == row["original_hex"] and original.pin == image, "original entry changed after observation")
            finally:
                original.close()
            found = True
    _need(found, "declaration needs original_code evidence beginning at this exact original entry")
    return observations


def declare(session, state, request, folder):
    R.assert_guest()
    _need(state.get("native_contract_mode") == "declared" and state.get("workflow") == "guest_model_judged_state_v1",
          "caller must explicitly enable declared native types before a model-judged session begins")
    _need(state["status"] == "investigating", "native declaration is closed once a hypothesis or evaluation starts")
    root = Path(session).resolve(strict=True)
    actor = _unsigned(request["actor"], state["params"])
    reader = Reader()
    observations = _observations(reader, root, state, request["citations"], actor)
    document = {"format": FORMAT, "type_basis": TYPE_BASIS, "abi_recovered": False, "independently_reviewed": False,
                "actuated": False, "proof_created": False, "model_calls": 0, "session": str(root),
                "session_binding": _binding(state), "step": len(state["steps"]) - 1,
                "request_sha256": R.digest(request), "target": copy.deepcopy(state["target"]), "actor": actor,
                "contract": B._contract(actor), "observations": observations,
                "sources": {name: {"file": str(Path(module.__file__).resolve()),
                    "sha256": R._file_hash(Path(module.__file__).resolve()),
                    "bytes": Path(module.__file__).stat().st_size} for name, module in
                    (("declaration", __import__(__name__, fromlist=["_"])), ("runtime", B), ("mapping", R))}}
    reader.recheck()
    path = Path(folder) / "native-declaration.json"
    _need(len(json.dumps(document).encode()) <= MAX_BYTES and not path.exists(), "new bounded native declaration required")
    with path.open("x") as stream:
        json.dump(document, stream, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    signed = {**actor, "signature_evidence": {"kind": KIND, "file": str(path), "sha256": R._file_hash(path), "pointer": "/contract"}}
    return {"status": "native_types_declared", "actor": signed, "type_basis": TYPE_BASIS,
            "abi_recovered": False, "independently_reviewed": False, "proof_created": False, "actuated": False,
            "model_calls": 0, "receipt": str(path)}


def validate_current(value, *, state):
    """Admit declared types only in their opted-in producing discovery session.

    Saved catalog replay deliberately uses resources() instead: its already
    promoted candidate may outlive the discovery session.
    """
    def calls(node):
        if type(node) is dict:
            if type(node.get("signature_evidence")) is dict and node["signature_evidence"].get("kind") == KIND:
                if node.get("op") == "native_invoke":
                    yield {"kind": "native_function", **{k: v for k, v in node.items() if k not in {"op", "alias"}}}
                else:
                    yield node
            for child in node.values():
                yield from calls(child)
        elif type(node) is list:
            for child in node:
                yield from calls(child)

    for actor in calls(value):
        _need(state.get("native_contract_mode") == "declared" and
              state.get("workflow") == "guest_model_judged_state_v1",
              "current session must explicitly enable declared native types")
        resources(actor)
        claim = actor["signature_evidence"]
        document = R.load_json(claim["file"])
        name = f"s{document['step']:04d}/native-declaration.json"
        file = Path(claim["file"])
        pin = {"file": str(file), "sha256": R._file_hash(file), "bytes": file.stat().st_size}
        _need(state.get("artifacts", {}).get(name) == pin and _binding(state) == document["session_binding"],
              "native declaration belongs to a different or changed current session")


def resources(actor):
    """Reopen the producer closure and exact recipe before each native use."""
    R.assert_guest()
    evidence = actor["signature_evidence"]
    _need(evidence["kind"] == KIND and evidence["pointer"] == "/contract", "declared-native evidence required")
    reader = Reader()
    value, pin = reader.data(evidence["file"])
    _need(pin["sha256"] == evidence["sha256"] and pin["bytes"] <= MAX_BYTES and
          value["format"] == FORMAT and value["type_basis"] == TYPE_BASIS and
          value["abi_recovered"] is value["independently_reviewed"] is value["actuated"] is value["proof_created"] is False,
          "invalid or changed native model declaration")
    root = Path(value["session"])
    _need(root.resolve(strict=True) == root and Path(evidence["file"]).parent.parent == root and
          type(value["step"]) is int and 0 <= value["step"] < 64 and
          Path(evidence["file"]).parent.name == f"s{value['step']:04d}", "native declaration escaped its producing session step")
    state = reader.data(root / "session.json")[0]
    name = Path(evidence["file"]).relative_to(root).as_posix()
    _need(state.get("artifacts", {}).get(name) == pin,
          "native declaration differs from its registered producing artifact")
    _need(_binding(state) == value["session_binding"] and state.get("native_contract_mode") == "declared" and
          state.get("workflow") == "guest_model_judged_state_v1" and state["target"] == value["target"],
          "native declaration's frozen task or policy changed")
    step = state["steps"][value["step"]]
    _need(step["status"] == "completed" and step["request"]["op"] == "declare_native" and
          R.digest(step["request"]) == value["request_sha256"], "native declaration's producing tool changed")
    unsigned = {k: copy.deepcopy(v) for k, v in actor.items() if k != "signature_evidence"}
    original = _unsigned(value["actor"], state["params"])
    _need(original == _unsigned(step["request"]["actor"], state["params"]) and
          [r["citation"] for r in value["observations"]] == step["request"]["citations"],
          "native declaration differs from its actual producing request")
    _need(set(unsigned) == set(original) and all(unsigned[k] == original[k] for k in set(original) - {"arguments"}),
          "native declaration's target, ABI or context changed")
    _need(len(unsigned["arguments"]) == len(original["arguments"]), "native argument count changed")
    for current, declared in zip(unsigned["arguments"], original["arguments"]):
        normalized = copy.deepcopy(current)
        _need(current["type"] == declared["type"], "native argument type changed")
        for leaf in B.argument_leaves(declared):
            if type(leaf["value"]) is dict and set(leaf["value"]) == {"input"}:
                normalized = B.replace_argument_leaf(normalized, leaf["path"], leaf["value"])
        _need(normalized == declared, "native constant or receiver recipe changed")
    _need(value["contract"] == B._contract(actor), "native declared ABI changed")
    _observations(reader, root, state, [r["citation"] for r in value["observations"]], original)
    expected_sources = {name: Path(module.__file__).resolve() for name, module in
                        (("declaration", __import__(__name__, fromlist=["_"])), ("runtime", B), ("mapping", R))}
    _need(type(value.get("sources")) is dict and set(value["sources"]) == set(expected_sources) and
          all(value["sources"][name].get("file") == str(path) for name, path in expected_sources.items()),
          "native declaration must retain its exact canonical producer source roles")
    for source in value["sources"].values():
        reader.read(source["file"], source)
    reader.recheck()
    # The mutable session journal is validated above, not installed as an
    # invocation resource; subsequent successful workflow steps may update it.
    return {name: pin for name, pin in reader.files.items() if name != str(root / "session.json")}
