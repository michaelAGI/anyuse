"""Offline receiver declaration fixtures; no native call/type/lifetime proof."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

import pytest

from anyuse.execution import rea_actor_evidence as E
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_native_receiver_contract as N
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_static_artifacts as A
from test_rea_runtime_bridge import _inert_macho_with_data


@pytest.fixture(autouse=True)
def no_live_runtime(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline receiver fixture must not inspect or attach to a live process")
    monkeypatch.setattr(B, "collect", refuse)
    monkeypatch.setattr(B, "process_identity", refuse)
    monkeypatch.setitem(sys.modules, "frida", None)


def _pin(file):
    raw = file.read_bytes()
    return {"file": str(file), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _write(file, value):
    file.write_text(json.dumps(value, sort_keys=True, allow_nan=False))
    return _pin(file)


def _request(module=None):
    module = module or {"path": "/fixture/Owned.app/Contents/MacOS/Owned", "sha256": "a" * 64}
    return {"op": "declare_native_receiver", "getter": {"kind": "native_function",
        "target": {"module": copy.deepcopy(module), "offset": "0x1000"}, "abi": "default",
        "arguments": [], "return_type": "pointer"},
        "consumer": {"kind": "native_function", "target": {"module": copy.deepcopy(module), "offset": "0x1004"},
        "abi": "default", "arguments": [{"type": "pointer", "value": {"receiver": "fresh"}},
                                         {"type": "int32", "value": {"input": "amount"}}], "return_type": "void"},
        "vptr": {"image": copy.deepcopy(module), "data_offset": "0x2000"},
        "citations": [{"role": role, "artifact": f"s{index:04d}/original.json", "pointer": "/range"}
                      for index, role in enumerate(N.ROLES)]}


PARAMS = [{"name": "amount", "type": "integer"}]


def _recipe():
    originals = [{"role": role, "file": f"/fixture/session/s{index:04d}/original.json", "sha256": str(index + 1) * 64}
                 for index, role in enumerate(N.ROLES)]
    return N._decorate(_request(), "/fixture/session/s0003/native-receiver-declaration.json", "c" * 64, originals)


def test_contracts_have_no_declaration_self_hash_cycle_or_source_lifetime_claim():
    recipe, actor = _recipe()
    assert N.validate_request(_request(), PARAMS) == _request()
    assert N.validate_recipe(recipe) == recipe
    assert N.validate_actor(actor, allow_bindings=True) == actor
    assert N.has_hypothesis(actor) and N.evidence_summary(actor) == N.BASIS
    changed, _ = N._decorate(_request(), "/fixture/other/declaration.json", "d" * 64, recipe["root_evidence"]["originals"])
    assert N.contracts(recipe) == N.contracts(changed)
    assert len(N.evidence_plan(recipe)) == 4
    assert N.lifetime_contract(recipe)["evidence_basis"]["lifetime_proven"] is False
    text = json.dumps(N.contracts(recipe))
    assert "independently_established" not in text and "independently_excluded" not in text
    assert "source-admission" not in text and "signature_evidence" not in text


@pytest.mark.parametrize("change", ["getter_args", "getter_symbol", "tail_pointer", "return_pointer", "adjusted_placeholder",
                                  "vptr_unaligned", "citation_kind", "citation_escape", "source_label", "unknown_input"])
def test_closed_request_refuses_wider_or_unsupported_receivers(change):
    request = _request()
    if change == "getter_args":
        request["getter"]["arguments"] = [{"type": "int32", "value": 1}]
    elif change == "getter_symbol":
        request["getter"]["target"] = {"module": request["vptr"]["image"], "symbol": "guessed"}
    elif change == "tail_pointer":
        request["consumer"]["arguments"][1] = {"type": "pointer", "value": {"alias": "old"}}
    elif change == "return_pointer":
        request["consumer"]["return_type"] = "pointer"
    elif change == "adjusted_placeholder":
        request["consumer"]["arguments"][0]["value"] = {"receiver": "fresh", "adjustment": 8}
    elif change == "vptr_unaligned":
        request["vptr"]["data_offset"] = "0x2001"
    elif change == "citation_kind":
        request["citations"][0]["role"] = "header"
    elif change == "citation_escape":
        request["citations"][0]["artifact"] = "../s0000/original.json"
    elif change == "source_label":
        request["source_reviewed"] = True
    else:
        request["consumer"]["arguments"][1]["value"]["input"] = "undeclared"
    with pytest.raises((ValueError, E.EvidenceIntegrityError)):
        N.validate_request(request, PARAMS)


@pytest.mark.parametrize("change", ["edge", "projection", "adjustment", "context", "reviewed_claim", "source_original", "signature_slot"])
def test_recipe_never_upgrades_to_source_evidence_or_a_general_pointer_path(change):
    recipe, _ = _recipe()
    if change == "edge":
        recipe["path"] = ["child"]
    elif change == "projection":
        recipe["types"][0]["fields"].append({"name": "count", "type": "uint32", "offset": 8})
    elif change == "adjustment":
        recipe["consumer"]["this_adjustment"] = 8
    elif change == "context":
        recipe["consumer"]["call"]["context"] = "swift"
    elif change == "reviewed_claim":
        recipe["root_evidence"]["claim"]["kind"] = "reviewed_contract"
    elif change == "source_original":
        recipe["root_evidence"]["originals"][0]["role"] = "header"
    else:
        recipe["consumer"]["call"]["signature_evidence"]["pointer"] = "/signatures/getter"
    with pytest.raises((ValueError, E.EvidenceIntegrityError)):
        N.validate_recipe(recipe)


def _observation(recipe):
    return {"recipe": recipe, "argument": 0, "consumer": N.consumer_contract(recipe["consumer"]),
        "root_kind": "getter", "owner_retained": False, "queue": "main", "lifetime_mode": N.MODE,
        "resolved": True, "sealed": True, "dispatch_permit_consumed": True, "expires": "transaction",
        "evidence_basis": dict(N.BASIS), "borrowed_scope": {"getter_epoch": 1, "checked_epoch": 1,
            "use": "dispatch", "consumed": True, "expires": "use"},
        "hops": [{"type": "DeclaredReceiver", "pointer": {"handle": "n1", "alias": None,
            "ownership": "borrowed_no_escape", "expires": "transaction"}, "guards": [], "edge": None, "identity_checked": True}]}


@pytest.mark.parametrize("change", [None, "lifetime_proven", "old_epoch", "unconsumed", "read", "raw_address"])
def test_runtime_receipt_retains_unproven_tier_and_single_immediate_use(change):
    recipe, _ = _recipe()
    observation = _observation(recipe)
    if change == "lifetime_proven":
        observation["evidence_basis"]["lifetime_proven"] = True
    elif change == "old_epoch":
        observation["borrowed_scope"].update(getter_epoch=2, checked_epoch=2)
    elif change == "unconsumed":
        observation["borrowed_scope"]["consumed"] = False
    elif change == "read":
        observation["borrowed_scope"]["use"] = "read"
    elif change == "raw_address":
        observation["hops"][0]["pointer"]["address"] = "0x1234"
    if change is None:
        assert N.validate_observation(observation, recipe) == observation
        with pytest.raises(ValueError):
            N.validate_observation(observation, recipe, for_call=False)
    else:
        with pytest.raises((ValueError, E.EvidenceIntegrityError)):
            N.validate_observation(observation, recipe)


@pytest.fixture
def declared(tmp_path, monkeypatch):
    # Only this in-memory/fake-bundle fixture supplies an inert guest check.
    # Production's guard and the explicit refusal test remain unchanged.
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    root = tmp_path.resolve() / "session"
    root.mkdir()
    app = tmp_path.resolve() / "Owned.app"
    image = app / "Contents/MacOS/Owned"
    image.parent.mkdir(parents=True)
    image.write_bytes(_inert_macho_with_data())
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "fixture.owned",
        "CFBundleExecutable": "Owned", "CFBundleVersion": "1"}))
    module = {"path": str(image), "sha256": _pin(image)["sha256"]}
    request = _request(module)
    state = {"bundle": str(app), "target": R.fingerprint(app), "task": "Set a fixture scalar",
        "params": PARAMS, "plan": {"fixture": True}, "success_criteria": ["Fixture scalar changes"],
        "workflow": "guest_model_judged_state_v1", "native_contract_mode": "declared",
        "native_receiver_mode": N.POLICY, "observer_profile_sha256": "f" * 64,
        "status": "investigating", "artifacts": {}, "steps": []}
    for index, citation in enumerate(request["citations"]):
        folder = root / f"s{index:04d}"
        folder.mkdir()
        source = {"kind": "original_range" if index == 2 else "original_code", "relative": "Contents/MacOS/Owned",
            "offset": "0x2000" if index == 2 else ("0x1000" if index == 0 else "0x1004"),
            "bytes": 8 if index == 2 else 4, "unit_bytes": 8 if index == 2 else 4}
        producing = {"op": "static_evidence", "request": source}
        original = A.OriginalImage(app, source["relative"])
        try:
            observation = A.original_range(original, source)
        finally:
            original.close()
        row = {"step": index, "status": "completed", "request": producing, "feedback": {"fixture": True}}
        state["steps"].append(row)
        for name, value in [("original.json", observation), ("request.json", producing), ("feedback.json", row)]:
            file = folder / name
            state["artifacts"][file.relative_to(root).as_posix()] = _write(file, value)
    folder = root / "s0003"
    folder.mkdir()
    state["steps"].append({"step": 3, "status": "running", "request": request})
    _write(folder / "request.json", request)
    _write(root / "session.json", state)
    result = N.declare(root, state, request, folder)
    state["steps"][3].update(status="completed", feedback=result)
    _write(folder / "feedback.json", state["steps"][3])
    for name in [N.RECEIPT, "request.json", "feedback.json"]:
        state["artifacts"][f"s0003/{name}"] = _pin(folder / name)
    _write(root / "session.json", state)
    return root, state, result, image


def test_original_binary_declaration_reconstructs_full_actor_getter_and_resource_closure(declared):
    root, state, result, image = declared
    actor, recipe = result["actor"], result["recipe"]
    assert result["actuated"] is result["proof_created"] is result["lifetime_proven"] is False
    resources = N.resources(recipe)
    assert resources == N.resources(actor) == N.resources(recipe["root"]["getter"])
    assert str(root / "session.json") not in resources
    assert str(image) in resources
    assert all(str(root / f"s{index:04d}/request.json") in resources for index in range(4))
    assert N.validate_declaration(actor, state=state)["evidence_basis"] == N.BASIS
    receipt = resources[result["receipt"]]
    assert [row["pointer"] for row in receipt["native_receiver_signatures"]] == ["/signatures/getter", "/signatures/consumer"]
    bound = copy.deepcopy(actor)
    bound["arguments"][1]["value"] = 7
    assert N.resources(bound) == resources
    with pytest.raises(ValueError, match="declared inputs"):
        N.validate_declaration(bound, state=state)


@pytest.mark.parametrize("change", ["mode", "foreign_session", "image", "producing_kind", "registered_pin", "actor_target", "constant"])
def test_resource_and_proposal_reuse_refuses_policy_producer_or_recipe_tampering(declared, change):
    root, state, result, image = declared
    actor = copy.deepcopy(result["actor"])
    if change == "mode":
        state["native_receiver_mode"] = None
        _write(root / "session.json", state)
    elif change == "foreign_session":
        foreign = copy.deepcopy(state)
        foreign["task"] = "Different task"
        with pytest.raises(ValueError, match="current session"):
            N.validate_declaration(actor, state=foreign)
        return
    elif change == "image":
        image.write_bytes(image.read_bytes()[:-1] + b"x")
    elif change == "producing_kind":
        state["steps"][0]["request"]["request"]["kind"] = "ghidra_code"
        _write(root / "session.json", state)
    elif change == "registered_pin":
        state["artifacts"]["s0000/original.json"]["sha256"] = "0" * 64
        _write(root / "session.json", state)
    elif change == "actor_target":
        actor["target"]["offset"] = "0x1008"
    else:
        actor["arguments"].append({"type": "int32", "value": 9})
    with pytest.raises((ValueError, E.EvidenceIntegrityError)):
        N.resources(actor)


def test_guest_guard_runs_before_any_declaration_or_resource_file_read(monkeypatch):
    def refuse():
        raise PermissionError("guest fixture only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    with pytest.raises(PermissionError, match="guest fixture only"):
        N.declare("/missing", {}, {}, "/missing")
    with pytest.raises(PermissionError, match="guest fixture only"):
        N.resources({})


@pytest.mark.parametrize("foreign_package", [False, True])
def test_standalone_worker_reopens_real_receiver_resources_before_attachment(declared, tmp_path, foreign_package):
    """Use the file worker's real resource path, stopping before Frida import.

    Only OS identity and compiled-adapter preconditions are inert. Request,
    declaration, original Mach-O data, producer closure and resource validators
    run unchanged in a separate process with the actual worker's bare imports.
    """
    _, state, result, image = declared
    actor = copy.deepcopy(result["actor"])
    actor["arguments"][1]["value"] = 7
    request = B.invocation_request(actor)
    resources = B.request_resources(request)
    identity = {"bundle": state["bundle"], "bundle_id": state["target"]["bundle_id"],
                "executable": str(image), "pid": 321, "launch_time": 1.0, "uid": os.getuid()}
    transport = {"kind": "local"}
    config = {"format": B.WORKER, "request": request, "bundle": state["bundle"], "target": state["target"],
        "identity": identity, "transport": transport, "adapter": {"transport": transport},
        "deadline_ms": int(time.time() * 1000) + 30000, "trace": str(tmp_path / "trace.jsonl"),
        "native_resources": resources, "compiler_provenance": {"stop_after_real_resource_validation": True}}
    config_file = tmp_path / "standalone-config.json"
    _write(config_file, config)
    code = r'''
from pathlib import Path
import importlib.abc,json,runpy,sys,types
class NoFrida(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0]=='frida':
            raise AssertionError('offline worker fixture must stop before Frida import')
sys.meta_path.insert(0,NoFrida())
bridge=Path(sys.argv[1]);config=Path(sys.argv[2]);foreign=sys.argv[3]=='1'
# A direct file entry puts its own execution directory first on sys.path.
sys.path.insert(0,str(bridge.parent))
worker=runpy.run_path(str(bridge),run_name='standalone_worker_fixture')
# runpy returns a namespace copy; patch the actual production function globals.
worker=worker['_worker'].__globals__
assert worker['__package__']=='' and worker['NX'].__package__==''
# Both the bare file worker and its package resource reader use inert identity
# boundaries here; the real validators still reopen all synthetic evidence.
worker['R'].assert_guest=lambda:None
worker['NX']._resource_module('rea_mapping').assert_guest=lambda:None
data=json.loads(config.read_bytes())
worker['process_identity']=lambda *a,**k:data['identity']
worker['_transport']=lambda **k:(data['transport'],None)
worker['adapter_identity']=lambda **k:data['adapter']
if foreign:
    module=types.ModuleType('execution')
    module.__file__=str(config.parent/'unrelated-package'/'__init__.py')
    sys.modules['execution']=module
before=sys.path[:]
observed=worker['_worker'](str(config))
print(json.dumps({'result':observed,'path_unchanged':before==sys.path,'frida_imported':'frida' in sys.modules},sort_keys=True))
'''
    completed = subprocess.run([sys.executable, "-B", "-c", code, str(Path(B.__file__).resolve()), str(config_file),
                                "1" if foreign_package else "0"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=45, check=False,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    worker = observed["result"]
    assert observed["path_unchanged"] and observed["frida_imported"] is False
    assert worker["attached"] is False and worker["operations"] == []
    assert worker["native_calls"] == worker["invocations"] == 0
    assert worker["frida_lifecycle"]["manager_state"] == "not_acquired"
    assert worker["frida_lifecycle"]["attachment_attempted"] is False
    assert worker["error"]["type"] == "ValueError"
    if foreign_package:
        assert "foreign package/module" in worker["error"]["message"]
        assert worker["native_resources"] == {}
    else:
        assert "verified compiler artifact provenance changed before attachment" == worker["error"]["message"]
        assert worker["native_resources"] == resources
        assert result["receipt"] in worker["native_resources"] and str(image) in worker["native_resources"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
