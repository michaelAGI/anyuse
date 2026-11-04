"""Agent tool-loop regressions: actual learner/proof gate with inert app/runtime."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile

import pytest

_HERE = Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-rea-agent-pkg-")
(Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_agent as G
from anyuse.execution import rea_harness as H
from test_rea_harness import fixture, runtime, objc_actor, objc_sequence, native_template, native_pointer_read, native_swift_read, compiler_cases, svg_fixture
from test_rea_native_adapter import request_fixture as compiler_request_fixture


@pytest.fixture
def session(fixture, tmp_path):
    app, profile, inputs, _ = fixture
    for i in range(6, 9):
        p = tmp_path / f"independent-{i}.txt"
        p.write_text(f"Independent full result {i}.")
        inputs.append({"query": f"q{i}", "reference": str(p)})
    (app / "Contents/facts.plist").write_bytes(plistlib.dumps({"handler": "TEST/READ", "types": {"term": "utf8"}}))
    rea = tmp_path / "rea-inert"
    rea.write_text("inert")
    rea.chmod(0o700)
    root = tmp_path / "session"
    plan = {role: [{"inputs": x} for x in inputs[i*3:(i+1)*3]] for i, role in enumerate(G.ROLES)}
    G.begin(str(app), str(rea), "Read the requested complete result", profile["params"],
            ["Full independent content and requested query must match"], plan, str(root))
    return root, profile, plan


def proposal(session):
    root, profile, _ = session
    result = G.step(root, {"op": "resource", "relative": "Contents/facts.plist"})
    assert result["feedback"]["status"] == "completed"
    artifact = [x for x in result["context"]["artifacts"] if x.endswith("resource.json")][-1]
    return {"op": "propose", "profile": profile, "rationale": "Recovered typed event and full result contract.",
            "citations": [{"artifact": artifact, "pointer": "/content/handler"}],
            "actor": {"kind": "apple_event", "class": "TEST", "id": "READ", "parameters": {
                "term": {"type": "utf8", "value": {"input": "query"}},
                "path": {"type": "utf8", "value": {"input": "reference"}}}}}


def learned(session):
    root = session[0]
    assert G.step(root, proposal(session))["context"]["status"] == "hypothesis"
    for i in range(3):
        result = G.step(root, {"op": "record", "case": i})
        assert result["feedback"]["status"] == "completed"
    result = G.step(root, {"op": "learn"})
    assert result["context"]["status"] == "candidate"
    assert result["context"]["proof_created"] is False


def mutable_session(session, tmp_path):
    original = R.load_json(session[0] / "session.json")
    profile = copy.deepcopy(session[1])
    plan = copy.deepcopy(session[2])
    working = (tmp_path / "mutable-working.txt").resolve()
    working.write_text("Initial working state")
    profile["params"].append({"name": "working", "type": "path", "source": "path", "mutable": True})
    profile["reference"]["fields"]["identity"] = {"kind": "file_identity", "param": "working"}
    for role in G.ROLES:
        for case in plan[role]:
            case["inputs"]["working"] = str(working)
    root = tmp_path / "mutable-session"
    G.begin(original["bundle"], original["rea"], original["task"], profile["params"], original["success_criteria"], plan, str(root))
    return root, profile, plan


def native_session(session, tmp_path):
    original = R.load_json(session[0] / "session.json")
    profile = copy.deepcopy(session[1])
    profile["params"] += [{"name": "counter", "type": "string", "source": "name"},
                          {"name": "buffer_hex", "type": "string", "source": "text"}]
    plan = copy.deepcopy(session[2])
    for i, case in enumerate(case for role in G.ROLES for case in plan[role]):
        case["inputs"].update(counter=str(10 + i), buffer_hex=f"{i:02x}{255-i:02x}")
    types = ["utf8", "utf8", {"kind": "struct", "fields": [{"name": "counter", "type": "int32"}, {"name": "fixed", "type": "int32"}]},
             {"kind": "buffer", "bytes": 2, "direction": "in"}]
    values = [{"input": "query"}, {"input": "reference"}, {"counter": {"input": "counter"}, "fixed": 17}, {"input": "buffer_hex"}]
    actor = native_template(tmp_path, types, values)
    root = tmp_path / "native-session"
    G.begin(original["bundle"], original["rea"], original["task"], profile["params"], original["success_criteria"], plan, str(root))
    return root, profile, plan, actor


def compiler_session(session, tmp_path, monkeypatch, family="swift_double_bool_context"):
    original = R.load_json(session[0] / "session.json")
    profile, plan = copy.deepcopy(session[1]), copy.deepcopy(session[2])
    inputs = [case["inputs"] for role in G.ROLES for case in plan[role]]
    actor = compiler_cases((Path(original["bundle"]), profile, inputs, []), tmp_path, monkeypatch, family=family)
    if family == "swift_double_bool_context":
        actor["arguments"][2]["value"] = {"input": "amount"}
        actor["arguments"][3]["value"] = {"input": "enabled"}
    else:
        for argument, name in zip(actor["arguments"][2:5], ("query", "count", "packed")):
            argument["value"] = {"input": name}
    root = tmp_path / "compiler-session"
    G.begin(original["bundle"], original["rea"], original["task"], profile["params"], original["success_criteria"], plan, str(root))
    return root, profile, plan, actor


def compiler_learned(session):
    root, profile, _, actor = session
    observed = G.step(root, {"op": "discover", "request": {"operations": [{"op": "modules", "limit": 4}]}})
    artifact = next(name for name in observed["context"]["artifacts"] if name.endswith("runtime.json"))
    proposed = G.step(root, {"op": "propose", "profile": profile, "actor": actor,
        "rationale": "Separate original private entry and compiler-correlated helper with complete independent task output.",
        "citations": [{"artifact": artifact, "pointer": "/result/operations/0"}]})
    assert proposed["context"]["status"] == "hypothesis"
    for case in range(3):
        assert G.step(root, {"op": "record", "case": case})["feedback"]["status"] == "completed"
    assert G.step(root, {"op": "learn"})["context"]["status"] == "candidate"


def graph_request():
    return {"operations": [{"op": "graph", "root": {"class": "MappedReader", "kind": "unique", "subclasses": False},
        "types": [{"class": "MappedReader", "fields": [
            {"name": "child", "kind": "ivar", "type": "object", "ivar": "_child", "encoding": "@", "target": "MappedReader"},
            {"name": "content", "kind": "getter", "type": "utf8", "selector": "content", "signature": "@@:"}]}],
        "max_depth": 2, "max_nodes": 4, "methods_limit": 8}]}


def test_agent_extracts_records_learns_checks_and_publishes_through_real_gate(session, runtime):
    root = session[0]
    learned(session)
    early = G.step(root, {"op": "publish", "summary": "Read", "details": "Complete result"})
    assert early["feedback"]["status"] == "failed" and len(runtime.sent) == 3
    checked = G.step(root, {"op": "verify"})
    assert checked["context"]["status"] == "verified"
    assert checked["context"]["proof_created"] is True
    assert G.step(root, {"op": "heldout"})["feedback"]["status"] == "completed"
    result = G.step(root, {"op": "publish", "summary": "Read complete result", "details": "Check the full independent reference and query"})
    assert result["context"]["status"] == "published" and len(runtime.sent) == 9
    assert R.load_json(result["context"]["published"])["actions"][0]["id"] == session[1]["id"]
    history = result["context"]["history"]
    assert any(x["status"] == "failed" for x in history)
    assert result["context"]["task"] == "Read the requested complete result"


def test_mutable_working_path_autosave_and_atomic_replacement_preserve_mapping_proof(session, runtime, tmp_path):
    mutable = mutable_session(session, tmp_path)
    frozen = R.load_json(mutable[0] / "session.json")["frozen_files"]
    working = Path(mutable[2]["learning"][0]["inputs"]["working"])
    runtime.autosave = True
    learned(mutable)
    assert G.step(mutable[0], {"op": "verify"})["context"]["status"] == "verified"
    assert G.step(mutable[0], {"op": "heldout"})["feedback"]["status"] == "completed"
    published = G.step(mutable[0], {"op": "publish", "summary": "Read complete result", "details": "Independent oracle and mutable working-document identity"})
    assert published["context"]["status"] == "published" and len(runtime.sent) == 9
    assert G._files(mutable[1]["params"], mutable[2]) == frozen
    assert working.read_text() == Path(mutable[2]["heldout"][-1]["inputs"]["reference"]).read_text()
    assert frozen[str(working)] == {"kind": "mutable_path", "canonical": str(working), "uid": os.getuid()}
    assert any(isinstance(value, str) for value in frozen.values())


def test_mutable_role_does_not_hide_future_oracle_mutation_during_tool_step(session, tmp_path, monkeypatch):
    from anyuse.execution import rea_apple_event as AE
    mutable = mutable_session(session, tmp_path)
    future = Path(mutable[2]["heldout"][-1]["inputs"]["reference"])
    class Observation:
        def read(self, all_windows=True):
            future.write_text("Changed future oracle")
            return []
    monkeypatch.setattr(AE, "Runtime", lambda *args, **kwargs: Observation())
    result = G.step(mutable[0], {"op": "observe"})
    assert result["feedback"]["status"] == "failed"
    assert "frozen input/reference identity changed" in result["feedback"]["feedback"]["error"]
    assert result["context"]["proof_created"] is False
    with pytest.raises(ValueError, match="frozen input/reference"):
        G.context(mutable[0])


@pytest.mark.parametrize("alias", ["canonical", "hardlink"])
def test_cross_role_mutable_immutable_aliases_are_rejected_before_session_creation(session, tmp_path, alias):
    original = R.load_json(session[0] / "session.json")
    profile = copy.deepcopy(session[1])
    plan = copy.deepcopy(session[2])
    first = (tmp_path / "first-working.txt").resolve()
    second = (tmp_path / "second-working.txt").resolve()
    first.write_text("First")
    second.write_text("Second")
    profile["params"].append({"name": "working", "type": "path", "source": "path", "mutable": True})
    for role in G.ROLES:
        for case in plan[role]:
            case["inputs"]["working"] = str(first)
    oracle = first
    if alias == "hardlink":
        oracle = (tmp_path / "linked-oracle.txt").resolve()
        os.link(first, oracle)
    plan["verification"][0]["inputs"].update(working=str(second), reference=str(oracle))
    target = tmp_path / "rejected-session"
    with pytest.raises(ValueError, match="overlap or alias"):
        G.begin(original["bundle"], original["rea"], original["task"], profile["params"], original["success_criteria"], plan, str(target))
    assert not target.exists()


def test_absent_and_false_mutable_flags_preserve_historical_file_hash_schema(session):
    params, plan = session[1]["params"], session[2]
    expected = {case["inputs"]["reference"]: R._file_hash(Path(case["inputs"]["reference"])) for role in G.ROLES for case in plan[role]}
    assert G._files(params, plan) == expected
    explicit_false = copy.deepcopy(params)
    explicit_false[1]["mutable"] = False
    assert G._files(explicit_false, plan) == expected
    assert all(isinstance(value, str) for value in expected.values())


def test_recovered_internal_method_requires_capability_and_never_actuates(session, runtime):
    request = proposal(session)
    request.pop("profile")
    request["actor"] = {"kind": "cpp_function", "symbol": "doThing", "address": "0x1234"}
    result = G.step(session[0], request)
    assert result["context"]["status"] == "needs_capability" and not runtime.sent
    assert result["context"]["proof_created"] is False
    assert G.step(session[0], {"op": "record", "case": 0})["feedback"]["status"] == "failed"
    assert not runtime.sent


@pytest.fixture
def runtime_tools(monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    calls = []
    def collect(bundle, out, request, timeout=15):
        calls.append({"bundle": bundle, "request": copy.deepcopy(request), "timeout": timeout})
        folder = Path(out)
        folder.mkdir(parents=True)
        receipt = {"format": "anyuse.rea-runtime/1", "status": "observed", "actuated": False,
                   "verified": False, "evidence_only": True,
                   "result": {"operations": [{"class": "MappedReader", "selector": "readResult:reference:enabled:",
                                               "signature": "v40@0:8@16@24B32"}]}}
        if any(operation["op"] == "graph" for operation in request["operations"]):
            graph = next(operation for operation in request["operations"] if operation["op"] == "graph")
            receipt["result"]["operations"] = [{"index": 0, "op": "graph", "nodes": [{"id": "n0", "receiver": copy.deepcopy(graph["root"])}],
                "edges": [], "truncated": True, "errors": [{"node": "n0", "field": "child", "message": "Unresolved child"}],
                "getter_calls": 1, "potentially_mutating_getters": True}]
        R.write_json(folder / "runtime.json", receipt)
        return receipt
    monkeypatch.setattr(B, "collect", collect)
    return calls


def inspector_metadata_receipt(monkeypatch, state, request, rows):
    """Inert provider output with the same observation envelope as collect."""
    from anyuse.execution import rea_inspector_bridge as I
    adapter = {"fixture": "fixed-inspector-adapter"}
    transport = {"kind": "chromium_renderer_cdp", "fixture": "explicit-local-config"}
    monkeypatch.setattr(I, "adapter_identity", lambda: copy.deepcopy(adapter))
    monkeypatch.setattr(I, "transport_identity", lambda bundle: copy.deepcopy(transport))
    identity = {"bundle": state["bundle"], "bundle_id": state["target"]["bundle_id"],
                "executable": state["bundle"] + "/Contents/MacOS/TestApp", "pid": 1234,
                "launch_time": 1234567890., "uid": os.getuid()}
    return {"format": I.FORMAT, "status": "observed", "bundle": state["bundle"], "target": copy.deepcopy(state["target"]),
            "request": copy.deepcopy(request), "request_sha256": R.digest(request), "operations": copy.deepcopy(rows),
            "identity": identity, "after_identity": copy.deepcopy(identity), "adapter": copy.deepcopy(adapter), "transport": copy.deepcopy(transport),
            "actuated": False, "state_uncertain": False, "resource_review_required": False,
            "proof_created": False, "task_result_verified": False, "model_calls": 0,
            "method_dispatch_attempts": 0, "getter_dispatch_attempts": 0,
            "descriptor_traps_may_have_effects": False,
            "cleanup": {"object_group_released": True, "page_socket_closed": True, "browser_socket_closed": True,
                        "requested_evaluations_completed": True, "remote_execution_cancelled": False,
                        "local_live_ids_discarded": True}}


@pytest.mark.parametrize("operation", ["runtime", "discover"])
@pytest.mark.parametrize("metadata_kind", ["describe", "dom_query", "function_scopes", "prototype_instances"])
def test_explicit_inspector_provider_preserves_its_own_receipt_and_evidence(session, monkeypatch, operation, metadata_kind):
    from anyuse.execution import rea_inspector_bridge as I
    from anyuse.execution import rea_runtime_bridge as B
    calls = []

    def collect(bundle, out, request, timeout=30):
        calls.append(copy.deepcopy(request))
        path = Path(out) / "inspector.json"
        path.parent.mkdir(parents=True)
        observed = {"op": request["operations"][0]["op"], "properties": [], "whole_graph_coverage": False}
        if observed["op"] == "dom_query":
            observed = {"op": "dom_query", "total_match_count": 0, "nodes": [],
                        "truncated": False, "live_node_ids_exported": False}
        elif observed["op"] == "function_scopes":
            observed = {"op": "function_scopes", "function_sha256": request["operations"][0]["function_sha256"],
                        "scope_count": 1, "chain_sha256": "b" * 64,
                        "scopes": [{"index": 0, "description": "Global", "kind": "Global", "traversed": False,
                                    "unsupported_reason": "scope_kind_not_traversed"}],
                        "scopes_truncated": False, "whole_scope_coverage": False,
                        "primitive_values_exported": False, "live_ids_exported": False,
                        "debugger_pause": False, "getters_invoked": 0}
        elif observed["op"] == "prototype_instances":
            observed = {"op": "prototype_instances", "constructor_sha256": request["operations"][0]["constructor_sha256"],
                        "complete_candidate_count": 0, "instances": [], "instances_truncated": False,
                        "selection_indices_exported": False, "live_ids_exported": False,
                        "subclasses_selected": False, "heap_scan": True, "forced_gc_possible": True,
                        "objects_retained_until_group_release": True, "server_side_candidate_bound": False,
                        "task_result_verified": False}
        state = R.load_json(session[0] / "session.json")
        receipt = inspector_metadata_receipt(monkeypatch, state, request, [observed])
        R.write_json(path, receipt)
        return receipt

    monkeypatch.setattr(I, "collect", collect)
    monkeypatch.setattr(B, "collect", lambda *a, **k: pytest.fail("selected inspector used native transport"))
    if metadata_kind == "dom_query":
        context = G.context(session[0])
        example = context["tools"]["discover"]["examples"][0]
        assert example == {"op": "discover", "provider": "inspector", "request": {
            "operations": [{"op": "dom_query", "selector": "#app", "max_nodes": 8}]}}
        request = copy.deepcopy(example["request"])
        inspector = next(row for row in context["capabilities"]["runtime_providers"] if row["provider"] == "inspector")
        assert "dom_query" in inspector["discovery_operations"]
        assert inspector["access_observed"] is False and inspector["automatic_fallback"] is False
    elif metadata_kind == "function_scopes":
        context = G.context(session[0])
        example = context["tools"]["discover"]["examples"][1]
        assert example == {"op": "discover", "provider": "inspector", "request": {"operations": [{
            "op": "function_scopes", "recipe": {"root": {"kind": "global", "name": "candidateFunction"}, "steps": []},
            "function_sha256": "0" * 64, "max_scopes": 8, "max_bindings": 8}]}}
        request = copy.deepcopy(example["request"])
        inspector = next(row for row in context["capabilities"]["runtime_providers"] if row["provider"] == "inspector")
        assert "function_scopes" in inspector["discovery_operations"]
        assert inspector["access_observed"] is False and inspector["automatic_fallback"] is False
        assert any("max_scopes and max_bindings from 1 to 32" in row for row in context["agent_instructions"])
        assert any("actual measured hash" in row for row in context["agent_instructions"])
    elif metadata_kind == "prototype_instances":
        context = G.context(session[0])
        example = context["tools"]["discover"]["examples"][2]
        assert example == {"op": "discover", "provider": "inspector", "request": {"operations": [{
            "op": "prototype_instances", "recipe": {"root": {"kind": "global", "name": "candidateConstructor"},
                "steps": [{"kind": "property", "name": "prototype", "access": "data"}]},
            "constructor_sha256": "0" * 64, "max_instances": 8, "max_properties": 8}]}}
        request = copy.deepcopy(example["request"])
        inspector = next(row for row in context["capabilities"]["runtime_providers"] if row["provider"] == "inspector")
        assert "prototype_instances" in inspector["discovery_operations"]
        assert inspector["access_observed"] is False and inspector["automatic_fallback"] is False
        guidance = next(row for row in context["agent_instructions"] if "prototype-instance discovery" in row)
        assert "max_instances and max_properties from 1 to 32" in guidance
        assert "actual measured constructor hash" in guidance and "at most four heap queries" in guidance
        assert "explicit heap scan" in guidance and "forced GC is possible" in guidance
        assert "retained until object-group release" in guidance and "server-side candidate scan bound" in guidance
        assert "not task proof or authorization for reuse" in guidance and "never select an instance by ordinal" in guidance
    else:
        request = {"operations": [{"op": "describe", "recipe": {"root": {"kind": "global", "name": "globalThis"}, "steps": []},
                                   "max_properties": 8}]}
    result = G.step(session[0], {"op": operation, "provider": "inspector", "request": request})
    assert result["feedback"]["status"] == "completed" and calls == [request]
    assert result["feedback"]["feedback"]["provider"] == "inspector"
    assert result["feedback"]["feedback"]["evidence_only"] is True
    assert result["feedback"]["feedback"]["proof_created"] is False
    assert "s0000/runtime/inspector.json" in result["context"]["artifacts"]
    assert result["context"]["proof_created"] is False
    assert result["context"]["status"] == "investigating"
    state = R.load_json(session[0] / "session.json")
    assert state["proof"] is None and "candidate" not in state and "published" not in state
    if metadata_kind == "dom_query":
        observed = R.load_json(session[0] / "s0000/runtime/inspector.json")["operations"][0]
        assert observed["total_match_count"] == 0 and observed["live_node_ids_exported"] is False
    elif metadata_kind == "function_scopes":
        observed = R.load_json(session[0] / "s0000/runtime/inspector.json")["operations"][0]
        assert observed["function_sha256"] == request["operations"][0]["function_sha256"]
        assert observed["whole_scope_coverage"] is False and observed["primitive_values_exported"] is False
        assert observed["live_ids_exported"] is False and observed["debugger_pause"] is False
        assert observed["getters_invoked"] == 0
    elif metadata_kind == "prototype_instances":
        observed = R.load_json(session[0] / "s0000/runtime/inspector.json")["operations"][0]
        assert observed["constructor_sha256"] == request["operations"][0]["constructor_sha256"]
        assert observed["heap_scan"] is True and observed["forced_gc_possible"] is True
        assert observed["objects_retained_until_group_release"] is True and observed["server_side_candidate_bound"] is False
        assert observed["selection_indices_exported"] is False and observed["live_ids_exported"] is False
        assert observed["task_result_verified"] is False


@pytest.mark.parametrize("metadata_kind", ["dom_query", "function_scopes", "prototype_instances"])
def test_inspector_discovery_without_explicit_provider_keeps_the_native_default(session, runtime_tools, monkeypatch, metadata_kind):
    from anyuse.execution import rea_runtime_providers as P
    from anyuse.execution import rea_runtime_bridge as B
    imported = []

    def load(name, **kwargs):
        imported.append(name)
        if name != ".rea_runtime_bridge":
            pytest.fail("native discovery fell back to another provider")
        return B

    monkeypatch.setattr(P.importlib, "import_module", load)
    requests = {
        "dom_query": {"operations": [{"op": "dom_query", "selector": "#app", "max_nodes": 8}]},
        "function_scopes": {"operations": [{"op": "function_scopes",
            "recipe": {"root": {"kind": "global", "name": "candidateFunction"}, "steps": []},
            "function_sha256": "a" * 64, "max_scopes": 4, "max_bindings": 8}]},
        "prototype_instances": {"operations": [{"op": "prototype_instances",
            "recipe": {"root": {"kind": "global", "name": "candidateConstructor"},
                       "steps": [{"kind": "property", "name": "prototype", "access": "data"}]},
            "constructor_sha256": "a" * 64, "max_instances": 4, "max_properties": 8}]},
    }
    request = requests[metadata_kind]
    result = G.step(session[0], {"op": "discover", "request": request})
    assert result["feedback"]["status"] == "failed" and not runtime_tools
    assert imported == [".rea_runtime_bridge"]
    assert result["context"]["status"] == "investigating" and result["context"]["proof_created"] is False


@pytest.mark.parametrize("operation", ["runtime", "discover"])
def test_dom_discovery_closes_before_provider_selection_when_evaluation_starts(session, runtime, monkeypatch, operation):
    from anyuse.execution import rea_runtime_providers as P
    learned(session)
    monkeypatch.setattr(P, "for_name", lambda *args, **kwargs: pytest.fail("DOM investigation reopened evaluation"))
    request = {"operations": [{"op": "dom_query", "selector": "#app", "max_nodes": 8}]}
    result = G.step(session[0], {"op": operation, "provider": "inspector", "request": request})
    assert result["feedback"]["status"] == "failed" and result["context"]["status"] == "candidate"
    assert "evaluation starts" in result["feedback"]["feedback"]["error"]
    assert result["context"]["proof_created"] is False


@pytest.mark.parametrize("operation", ["runtime", "discover"])
@pytest.mark.parametrize("status", ["candidate", "verified", "published", "evaluation_failed"])
@pytest.mark.parametrize("metadata_kind", ["function_scopes", "prototype_instances"])
def test_inspector_object_discovery_closes_at_evaluation_before_provider_selection(session, monkeypatch, operation, status, metadata_kind):
    from anyuse.execution import rea_runtime_providers as P
    state = R.load_json(session[0] / "session.json")
    state["status"] = status
    R.write_json(session[0] / "session.json", state)
    monkeypatch.setattr(P, "for_name", lambda *args, **kwargs: pytest.fail("inspector object discovery reopened evaluation"))
    request = ({"operations": [{"op": "function_scopes",
        "recipe": {"root": {"kind": "global", "name": "candidateFunction"}, "steps": []},
        "function_sha256": "a" * 64, "max_scopes": 4, "max_bindings": 8}]}
        if metadata_kind == "function_scopes" else
        {"operations": [{"op": "prototype_instances",
            "recipe": {"root": {"kind": "global", "name": "candidateConstructor"},
                       "steps": [{"kind": "property", "name": "prototype", "access": "data"}]},
            "constructor_sha256": "a" * 64, "max_instances": 4, "max_properties": 8}]})
    result = G.step(session[0], {"op": operation, "provider": "inspector", "request": request})
    assert result["feedback"]["status"] == "failed" and result["context"]["status"] == status
    assert "evaluation starts" in result["feedback"]["feedback"]["error"]
    assert result["context"]["proof_created"] is False
    assert not (session[0] / "s0000/runtime").exists()


def test_unknown_runtime_provider_is_refused_before_any_collection(session, monkeypatch):
    from anyuse.execution import rea_runtime_providers as P
    monkeypatch.setattr(P.importlib, "import_module", lambda *a, **k: pytest.fail("unknown provider imported a runtime"))
    result = G.step(session[0], {"op": "discover", "provider": "auto", "request": {"operations": []}})
    assert result["feedback"]["status"] == "failed"
    assert result["context"]["proof_created"] is False


def test_complete_backend_state_remains_registered_evidence_without_proof(session, monkeypatch):
    from anyuse.execution import rea_state_observer as S
    target = G.context(session[0])["target"]
    schema = {"target": target}
    calls = []
    monkeypatch.setattr(S, "validate", lambda value: copy.deepcopy(value))

    def collect(bundle, data, inputs, out, timeout):
        calls.append((data, inputs))
        path = Path(out) / "observation.json"
        path.parent.mkdir(parents=True)
        receipt = {"format": "anyuse.rea-state-observation/1", "status": "observed",
                   "complete": True, "matches": True, "proof_created": False, "receipt": str(path)}
        R.write_json(path, receipt)
        return receipt

    monkeypatch.setattr(S, "collect", collect)
    result = G.step(session[0], {"op": "observe_state", "schema": schema, "inputs": {}})
    assert result["feedback"]["status"] == "completed" and len(calls) == 1
    assert "s0000/state/observation.json" in result["context"]["artifacts"]
    assert result["context"]["proof_created"] is False
    assert result["context"]["status"] == "investigating"


def test_backend_state_refuses_changed_target_before_collection(session, monkeypatch):
    from anyuse.execution import rea_state_observer as S
    monkeypatch.setattr(S, "validate", lambda value: copy.deepcopy(value))
    monkeypatch.setattr(S, "collect", lambda *a, **k: pytest.fail("changed state target actuated"))
    result = G.step(session[0], {"op": "observe_state", "schema": {"target": {"sha256": "0" * 64}}, "inputs": {}})
    assert result["feedback"]["status"] == "failed" and result["context"]["proof_created"] is False


def test_early_backend_observer_error_is_preserved_without_fabricated_receipt(session, monkeypatch):
    from anyuse.execution import rea_state_observer as S
    monkeypatch.setattr(S, "validate", lambda value: copy.deepcopy(value))
    monkeypatch.setattr(S, "collect", lambda *a, **k: {"status": "unknown", "complete": False,
        "matches": False, "resource_review_required": False,
        "error": {"type": "ValueError", "message": "invalid observer inputs before output creation"}})
    result = G.step(session[0], {"op": "observe_state", "schema": {"target": G.context(session[0])["target"]}, "inputs": {}})
    feedback = result["feedback"]["feedback"]
    assert result["feedback"]["status"] == "completed" and feedback["status"] == "unknown"
    assert feedback["receipt"] is None and feedback["error"]["message"] == "invalid observer inputs before output creation"
    assert result["context"]["proof_created"] is False


def test_backend_state_investigation_closes_before_started_evaluation(session, runtime, monkeypatch):
    from anyuse.execution import rea_state_observer as S
    learned(session)
    monkeypatch.setattr(S, "validate", lambda *a, **k: pytest.fail("state investigation reopened evaluation"))
    result = G.step(session[0], {"op": "observe_state", "schema": {}, "inputs": {}})
    assert result["feedback"]["status"] == "failed" and result["context"]["status"] == "candidate"


def test_runtime_tool_registers_hash_checked_facts_without_creating_a_proof(session, runtime_tools):
    request = {"operations": [{"op": "classes", "query": "MappedReader", "limit": 4}]}
    result = G.step(session[0], {"op": "runtime", "request": request, "timeout": 15})
    artifact = "s0000/runtime/runtime.json"
    assert runtime_tools[0]["request"] == request
    assert result["feedback"]["feedback"]["evidence_only"] is True
    assert result["context"]["proof_created"] is False
    assert artifact in result["context"]["artifacts"]
    changed = session[0] / artifact
    changed.write_text("{}")
    failure = G.step(session[0], {"op": "evidence", "artifact": artifact})
    assert failure["feedback"]["status"] == "failed"
    assert "evidence changed" in failure["feedback"]["feedback"]["error"]


def test_first_map_discovers_typed_private_metadata_then_freezes_evaluation(session, runtime, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    root, profile, _ = session
    actor = objc_actor({"input": "query"}, {"input": "reference"})
    request = {"operations": [
        {"op": "classes", "owner": "app", "query": "MappedReader", "limit": 4},
        {"op": "methods", "class": "MappedReader", "query": "readResult", "own": True, "limit": 4},
        {"op": "ivars", "class": "MappedReader", "query": "content", "own": True, "limit": 4},
        {"op": "instances", "class": "MappedReader", "alias": "reader", "subclasses": False},
    ]}
    operations = [
        {"index": 0, "op": "classes", "status": "ok", "rows": [{"name": "MappedReader", "origin": "module_backed"}], "matched": 1, "truncated": False},
        {"index": 1, "op": "methods", "status": "ok", "rows": [{"class": "MappedReader", "selector": actor["selector"], "signature": actor["signature"], "dispatch": "-"}], "matched": 1, "truncated": False},
        {"index": 2, "op": "ivars", "status": "ok", "rows": [{"class": "MappedReader", "name": "_content", "encoding": "@", "offset": "8"}], "matched": 1, "truncated": False},
        {"index": 3, "op": "instances", "status": "ok", "object": {"handle": "h0", "class": "MappedReader", "alias": "reader", "expires": "transaction"}},
    ]
    calls = []
    def collect(bundle, out, value, timeout=15):
        calls.append(copy.deepcopy(value))
        Path(out).mkdir(parents=True)
        receipt = {"format": "anyuse.rea-runtime/1", "status": "observed", "actuated": False,
                   "result": {"operations": operations, "invocations": 0, "handles_expired": True}}
        R.write_json(Path(out) / "runtime.json", receipt)
        return receipt
    monkeypatch.setattr(B, "collect", collect)
    observed = G.step(root, {"op": "discover", "request": request, "timeout": 7})
    assert observed["feedback"]["status"] == "completed" and calls == [request]
    assert not runtime.sent and observed["context"]["proof_created"] is False
    artifact = "s0000/runtime/runtime.json"
    pin = observed["context"]["artifacts"][artifact]
    assert pin["sha256"] == R._file_hash(root / artifact)
    evidence = G.step(root, {"op": "evidence", "artifact": artifact})
    assert actor["signature"] in evidence["feedback"]["feedback"]["text"]
    proposed = G.step(root, {"op": "propose", "profile": profile, "actor": actor,
        "rationale": "Exact runtime class and method signature identify a reusable typed boundary; the live handle expires.",
        "citations": [{"artifact": artifact, "pointer": "/result/operations/1/rows/0"}]})
    assert proposed["context"]["status"] == "hypothesis" and not runtime.sent
    for case in range(3):
        assert G.step(root, {"op": "record", "case": case})["feedback"]["status"] == "completed"
    assert G.step(root, {"op": "learn"})["context"]["status"] == "candidate"
    closed = G.step(root, {"op": "discover", "request": request})
    assert closed["feedback"]["status"] == "failed" and calls == [request]
    assert closed["context"]["status"] == "candidate" and len(runtime.sent) == 3
    assert "evaluation starts" in closed["feedback"]["feedback"]["error"]


@pytest.mark.parametrize("operation", [
    {"op": "invoke", "receiver": {"class": "MappedReader", "kind": "unique"}, "selector": "setEnabled:",
     "signature": "v20@0:8B16", "arguments": [{"type": "bool", "value": True}], "return_type": "void"},
    {"op": "read", "receiver": {"class": "MappedReader", "kind": "unique"}, "fields": [
        {"name": "content", "kind": "getter", "selector": "content", "signature": "@@:", "type": "utf8"}]},
])
def test_metadata_discovery_refuses_explicit_calls_and_undeclared_getters(session, runtime_tools, operation):
    result = G.step(session[0], {"op": "discover", "request": {"operations": [operation]}})
    assert result["feedback"]["status"] == "failed" and not runtime_tools
    assert "discovery accepts" in result["feedback"]["feedback"]["error"]
    assert result["context"]["proof_created"] is False


@pytest.mark.parametrize("operation", [
    {"op": "classes", "limit": 129},
    {"op": "methods", "class": "MappedReader", "limit": True},
    {"op": "ivars", "class": "MappedReader", "limit": 0},
    {"op": "instances", "class": "MappedReader", "alias": "reader", "subclasses": "true"},
    {"op": "instances", "class": "MappedReader", "alias": "reader", "address": "0x1234"},
])
def test_metadata_discovery_uses_existing_bounds_and_never_accepts_an_address(session, runtime_tools, operation):
    result = G.step(session[0], {"op": "discover", "request": {"operations": [operation]}})
    assert result["feedback"]["status"] == "failed" and not runtime_tools
    assert result["context"]["proof_created"] is False


def test_bounded_graph_discovery_retains_partial_paths_errors_and_getter_effects_as_evidence(session, runtime_tools):
    request = graph_request()
    result = G.step(session[0], {"op": "discover", "request": request})
    assert runtime_tools[0]["request"] == request
    artifact = "s0000/runtime/runtime.json"
    receipt = R.load_json(session[0] / artifact)
    graph = receipt["result"]["operations"][0]
    assert graph["nodes"][0]["receiver"] == request["operations"][0]["root"]
    assert graph["truncated"] is True and graph["errors"]
    assert graph["getter_calls"] == 1 and graph["potentially_mutating_getters"] is True
    assert result["context"]["proof_created"] is False
    assert result["feedback"]["feedback"]["evidence_only"] is True
    evidence = G.step(session[0], {"op": "evidence", "artifact": artifact})
    assert evidence["feedback"]["status"] == "completed"


@pytest.mark.parametrize("field,value", [("max_depth", 7), ("max_depth", -1), ("max_nodes", 0), ("max_nodes", 65), ("methods_limit", 129)])
def test_graph_discovery_invalid_budgets_are_refused_before_native_collection(session, runtime_tools, field, value):
    request = graph_request()
    request["operations"][0][field] = value
    result = G.step(session[0], {"op": "discover", "request": request})
    assert result["feedback"]["status"] == "failed" and not runtime_tools
    assert result["context"]["proof_created"] is False


def test_declared_native_object_graph_uses_a_borrowed_getter_without_an_old_address(session, runtime_tools, tmp_path):
    getter = native_template(tmp_path, [], [], return_type="pointer")
    fields = [{"name": "count", "offset": 0, "type": "int32"}, {"name": "child", "offset": 8, "type": "pointer", "target": "Node"}]
    evidence = (tmp_path / "native-layout.json").resolve()
    R.write_json(evidence, {"node": {"bytes": 16, "fields": fields, "pointer_policy": "borrowed_no_escape"}})
    layout = {"kind": "reviewed_contract", "file": str(evidence), "sha256": R._file_hash(evidence), "pointer": "/node"}
    request = {"operations": [{"op": "graph", "root": {"native": getter}, "root_type": "Node",
        "types": [{"name": "Node", "bytes": 16, "fields": fields, "layout_evidence": layout}],
        "max_depth": 2, "max_nodes": 4, "methods_limit": 0}]}
    result = G.step(session[0], {"op": "discover", "request": request})
    assert result["feedback"]["status"] == "completed" and runtime_tools[0]["request"] == request
    assert result["context"]["proof_created"] is False
    assert "s0000/runtime/runtime.json" in result["context"]["artifacts"]


def test_swift_field_discovery_keeps_explicit_receiver_type_pins_and_budgets_as_evidence(session, runtime_tools, tmp_path):
    read = native_swift_read(tmp_path)
    operation = {"op": "swift_fields", **{key: copy.deepcopy(read[key]) for key in ("receiver", "receiver_class", "swift_class", "module")},
                 "query": "nativeState", "limit": 8}
    request = {"operations": [operation]}
    result = G.step(session[0], {"op": "discover", "request": request})
    assert result["feedback"]["status"] == "completed" and runtime_tools[0]["request"] == request
    assert result["context"]["proof_created"] is False
    assert "s0000/runtime/runtime.json" in result["context"]["artifacts"]
    assert result["context"]["capabilities"]["runtime_field_sources"] == ["observed_objc_ivar_encoding", "canonical_swift_metadata"]


@pytest.mark.parametrize("limit", [0, True, 129])
def test_swift_field_discovery_rejects_invalid_budget_before_collection(session, runtime_tools, tmp_path, limit):
    read = native_swift_read(tmp_path)
    operation = {"op": "swift_fields", **{key: copy.deepcopy(read[key]) for key in ("receiver", "receiver_class", "swift_class", "module")}, "limit": limit}
    result = G.step(session[0], {"op": "discover", "request": {"operations": [operation]}})
    assert result["feedback"]["status"] == "failed" and not runtime_tools


def test_discovery_refuses_explicit_native_actuation_before_collection(session, runtime_tools, tmp_path):
    from anyuse.execution import rea_runtime_bridge as B
    request = B.invocation_request(native_template(tmp_path))
    result = G.step(session[0], {"op": "discover", "request": request})
    assert result["feedback"]["status"] == "failed" and not runtime_tools
    assert "discovery accepts" in result["feedback"]["feedback"]["error"]


def test_runtime_exploration_cannot_contaminate_started_evaluation(session, runtime, runtime_tools):
    learned(session)
    result = G.step(session[0], {"op": "runtime", "request": {"operations": [{"op": "classes"}]}})
    assert result["feedback"]["status"] == "failed"
    assert result["context"]["status"] == "candidate"
    assert "evaluation starts" in result["feedback"]["feedback"]["error"]
    assert not runtime_tools


def test_graph_discovery_closes_at_evaluation_before_getters_can_run(session, runtime, runtime_tools):
    learned(session)
    result = G.step(session[0], {"op": "discover", "request": graph_request()})
    assert result["feedback"]["status"] == "failed" and not runtime_tools
    assert result["context"]["status"] == "candidate"


def test_access_inventory_uses_static_default_and_registers_evidence_without_publication(session, monkeypatch):
    from anyuse.execution import rea_access as A
    calls = []
    def inspect(bundle, out, *, probe=None, timeout=30, plugin_evidence=None):
        calls.append({"bundle": bundle, "probe": probe, "timeout": timeout, "plugin_evidence": plugin_evidence})
        folder = Path(out)
        folder.mkdir(parents=True)
        receipt = {"format": "anyuse.rea-access/1", "status": "inspected", "evidence_only": True, "actuated": False,
                   "probe": None, "routes": [{"kind": "owned_development_copy", "verified": False}]}
        R.write_json(folder / "access.json", receipt)
        return receipt
    monkeypatch.setattr(A, "inspect", inspect)
    result = G.step(session[0], {"op": "access"})
    assert calls[0]["probe"] is None and calls[0]["plugin_evidence"] is None
    assert "s0000/access/access.json" in result["context"]["artifacts"]
    assert result["feedback"]["feedback"]["actuated"] is False
    assert result["context"]["proof_created"] is False


def test_access_probes_are_closed_once_evaluation_starts(session, runtime, monkeypatch):
    from anyuse.execution import rea_access as A
    learned(session)
    calls = []
    monkeypatch.setattr(A, "inspect", lambda *args, **kwargs: calls.append(args))
    result = G.step(session[0], {"op": "access", "probe": "gadget"})
    assert result["feedback"]["status"] == "failed" and not calls


def test_runtime_cleanup_uncertainty_preserves_receipt_and_closes_followup(session, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    calls = []
    def incomplete(bundle, out, request, timeout=15):
        folder = Path(out)
        folder.mkdir(parents=True)
        receipt = {"status": "incomplete", "resource_review_required": True, "actuated": None,
                   "error": "remote invocation completion unknown"}
        R.write_json(folder / "runtime.json", receipt)
        calls.append(request)
        return receipt
    monkeypatch.setattr(B, "collect", incomplete)
    result = G.step(session[0], {"op": "runtime", "request": {"operations": [{"op": "classes"}]}})
    assert result["context"]["status"] == "resource_review_required"
    assert "s0000/runtime/runtime.json" in result["context"]["artifacts"]
    with pytest.raises(R.ResourceReviewRequired):
        G.step(session[0], {"op": "runtime", "request": {"operations": [{"op": "classes"}]}})
    assert len(calls) == 1


def test_cited_private_method_reuses_shared_record_learn_verify_and_publish(session, runtime, runtime_tools):
    root, profile, _ = session
    observed = G.step(root, {"op": "runtime", "request": {"operations": [{"op": "classes", "query": "MappedReader"}]}})
    artifact = next(x for x in observed["context"]["artifacts"] if x.endswith("runtime.json"))
    request = {"op": "propose", "profile": profile, "rationale": "Runtime types establish a shared callable boundary.",
               "citations": [{"artifact": artifact, "pointer": "/result/operations/0"}],
               "actor": objc_actor({"input": "query"}, {"input": "reference"})}
    assert G.step(root, request)["context"]["status"] == "hypothesis"
    for i in range(3):
        assert G.step(root, {"op": "record", "case": i})["feedback"]["status"] == "completed"
    assert G.step(root, {"op": "learn"})["context"]["status"] == "candidate"
    assert G.step(root, {"op": "verify"})["context"]["status"] == "verified"
    assert G.step(root, {"op": "heldout"})["feedback"]["status"] == "completed"
    result = G.step(root, {"op": "publish", "summary": "Read full result", "details": "Typed private method and independent content check"})
    assert result["context"]["status"] == "published" and len(runtime.sent) == 9
    assert R.load_json(result["context"]["published"])["actions"][0]["id"] == profile["id"]


def test_cited_native_compound_route_reuses_agent_loop_proof_publication_and_replay(session, runtime, runtime_tools, tmp_path):
    root, profile, plan, actor = native_session(session, tmp_path)
    observed = G.step(root, {"op": "discover", "request": {"operations": [{"op": "modules", "limit": 4}]}})
    artifact = next(name for name in observed["context"]["artifacts"] if name.endswith("runtime.json"))
    request = {"op": "propose", "profile": profile, "actor": actor,
        "rationale": "Pinned native module and reviewed typed contract with complete independent result reference.",
        "citations": [{"artifact": artifact, "pointer": "/result/operations/0"}]}
    assert G.step(root, request)["context"]["status"] == "hypothesis"
    for i in range(3):
        assert G.step(root, {"op": "record", "case": i})["feedback"]["status"] == "completed"
    assert G.step(root, {"op": "learn"})["context"]["status"] == "candidate"
    assert G.step(root, {"op": "verify"})["context"]["status"] == "verified"
    assert G.step(root, {"op": "heldout"})["feedback"]["status"] == "completed"
    published = G.step(root, {"op": "publish", "summary": "Apply a complete native result", "details": "Typed compound arguments and independent full AX checks"})
    assert published["context"]["status"] == "published" and len(runtime.sent) == 9
    state = R.load_json(root / "session.json")
    candidate, proof = R.load_json(state["candidate"]), R.load_json(state["proof"])
    assert candidate["actor"] == actor
    replay = H.replay(candidate, proof, plan["heldout"][-1]["inputs"], R.load_json(root / "session.json")["bundle"], str(tmp_path / "native-replay"), 1)
    assert replay["verdict"] == "verified" and len(runtime.sent) == 10


@pytest.mark.parametrize("family", ["swift_double_bool_context", "cxx_owned_string_vector_double"])
def test_compiler_adapter_uses_persistent_mapping_publication_and_replay_with_original_provenance(session, runtime, runtime_tools, tmp_path, monkeypatch, family):
    mapped = compiler_session(session, tmp_path, monkeypatch, family)
    root, profile, plan, actor = mapped
    compiler_learned(mapped)
    assert G.step(root, {"op": "verify"})["context"]["status"] == "verified"
    assert G.step(root, {"op": "heldout"})["feedback"]["status"] == "completed"
    published = G.step(root, {"op": "publish", "summary": "Apply the complete native result",
        "details": "Compiler-correlated private ABI, decoded completion and independent complete AX output"})
    assert published["context"]["status"] == "published" and len(runtime.sent) == 9
    state = R.load_json(root / "session.json")
    candidate, proof = R.load_json(state["candidate"]), R.load_json(state["proof"])
    assert candidate["actor"] == actor and proof["model_calls"] == 0
    provenance = proof["cases"][0]["run"]["runtime"]["compiler_provenance"]
    assert all(pin["file"] in proof["resources"] for body in provenance.values() for pin in body["artifacts"])
    row = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]
    assert row["callable_entries"][0]["original_dispatched"] is False
    assert row["compiler_completion"]["original_body_entry_proved"] is False
    replay = H.replay(candidate, proof, plan["heldout"][-1]["inputs"], state["bundle"], str(tmp_path / "compiler-replay"), 1)
    assert replay["verdict"] == "verified" and len(runtime.sent) == 10
    assert all(operation["op"] != "load_helper" for call in runtime_tools for operation in call["request"]["operations"])


def test_compiler_native_exception_retains_evidence_and_closes_agent_actuation(session, runtime, runtime_tools, tmp_path, monkeypatch):
    mapped = compiler_session(session, tmp_path, monkeypatch, "cxx_owned_string_vector_double")
    compiler_learned(mapped)
    original = runtime.send
    def uncertain(self, candidate, inputs, timeout):
        run = original(self, candidate, inputs, timeout)
        receipt = run["runtime"]
        receipt.update(status="incomplete", resource_review_required=True)
        receipt["result"].update(native_state_uncertain=True, status="incomplete")
        receipt["result"]["operations"][0]["compiler_completion"].update(status=2, status_return=2, uncertain=True, value=None)
        self.receipt_dir.mkdir()
        R.write_json(self.receipt_dir / "runtime.json", receipt)
        raise R.ResourceReviewRequired("compiler exception requires effect review; no retry")
    monkeypatch.setattr(runtime, "send", uncertain)
    failed = G.step(mapped[0], {"op": "verify"})
    assert failed["context"]["status"] == "resource_review_required" and len(runtime.sent) == 4
    assert failed["context"]["proof_created"] is False
    artifact = next(name for name in failed["context"]["artifacts"] if "case-0/runtime/runtime.json" in name)
    assert G.step(mapped[0], {"op": "evidence", "artifact": artifact})["feedback"]["status"] == "completed"
    with pytest.raises(R.ResourceReviewRequired):
        G.step(mapped[0], {"op": "verify"})
    assert len(runtime.sent) == 4


def test_discover_cannot_load_a_compiler_helper_as_passive_metadata(session, runtime_tools, tmp_path, monkeypatch):
    mapped = compiler_session(session, tmp_path, monkeypatch)
    actor = mapped[3]
    result = G.step(mapped[0], {"op": "discover", "request": {"operations": [{"op": "load_helper",
        "module": actor["target"]["module"], "build_evidence": actor["compiler_adapter"]["evidence"]}]}})
    assert result["feedback"]["status"] == "failed" and not runtime_tools
    assert result["context"]["proof_created"] is False


@pytest.mark.parametrize("family", ["swift_double_bool_context", "cxx_owned_string_vector_double"])
def test_agent_prepares_fixed_compiler_evidence_and_can_inspect_its_actual_ir_without_loading(session, runtime, tmp_path, monkeypatch, family):
    from anyuse.execution import rea_native_adapter as A
    request, calls, _ = compiler_request_fixture(tmp_path, monkeypatch, family=family)
    root = session[0]
    observed = G.step(root, {"op": "inspect_compiler_toolchain"})
    assert observed["feedback"]["status"] == "completed" and len(calls) == 3
    assert "s0000/compiler/toolchain.json" in observed["context"]["artifacts"]
    result = G.step(root, {"op": "prepare_compiler_adapter", "request": request})
    feedback, context = result["feedback"]["feedback"], result["context"]
    assert result["feedback"]["status"] == "completed" and len(calls) == 8
    assert context["status"] == "investigating" and context["proof_created"] is False
    assert context["steps_used"] == 2 and not runtime.sent
    assert feedback["actuated"] is feedback["loaded_into_target"] is feedback["runtime_verified"] is False
    assert feedback["build_evidence"]["file"] == str(root / "s0001/compiler/build.json")
    assert feedback["outer_evidence"]["file"] == str(root / "s0001/compiler/adapter-abi.json")
    assert A.resources(feedback["build_evidence"])
    for name in ("adapter.cpp", "adapter.ll", "toolchain/abi-probe.ll", "toolchain/macros.stdout"):
        artifact = "s0001/compiler/" + name
        assert artifact in context["artifacts"]
        evidence = G.step(root, {"op": "evidence", "artifact": artifact, "limit": 1000})
        assert evidence["feedback"]["status"] == "completed"
        assert evidence["feedback"]["feedback"]["text_is_data"] is True
    assert "s0001/compiler/adapter.dylib" not in context["artifacts"]
    assert set(context["capabilities"]["compiler_adapter_families"]) == A.FAMILIES
    assert {"inspect_compiler_toolchain", "prepare_compiler_adapter"} <= set(context["tools"])


def test_compiler_preparation_is_available_through_the_existing_agent_step_cli(session, tmp_path, monkeypatch):
    import json
    from click.testing import CliRunner
    from anyuse.execution.rea_cli import rea_commands
    request, calls, _ = compiler_request_fixture(tmp_path, monkeypatch)
    command = tmp_path / "compiler-tool-request.json"
    R.write_json(command, {"op": "prepare_compiler_adapter", "request": request, "timeout": 7})
    result = CliRunner().invoke(rea_commands, ["agent", "step", str(session[0]), str(command)])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["feedback"]["status"] == "completed" and len(calls) == 5
    assert value["context"]["proof_created"] is False
    assert value["feedback"]["feedback"]["command_timeout_s"] == 7


@pytest.mark.parametrize("op,commands", [("inspect_compiler_toolchain", 3), ("prepare_compiler_adapter", 5)])
def test_compiler_tool_budget_accounts_for_every_fixed_subprocess(session, tmp_path, monkeypatch, op, commands):
    from anyuse.execution import rea_native_adapter as A
    request, _, _ = compiler_request_fixture(tmp_path, monkeypatch)
    calls = []
    def refuse(out, *, timeout):
        calls.append({"out": out, "timeout": timeout})
        raise ValueError("Inert stop before compilation")
    monkeypatch.setattr(A, "inspect_toolchain", refuse)
    monkeypatch.setattr(A, "build", lambda request, out, *, timeout: refuse(out, timeout=timeout))
    monkeypatch.setattr(G.time, "time", lambda: 1000.0)
    state = R.load_json(session[0] / "session.json")
    state["deadline"] = 1025.0
    R.write_json(session[0] / "session.json", state)
    result = G.step(session[0], {"op": op, **({"request": request} if op == "prepare_compiler_adapter" else {})})
    assert result["feedback"]["status"] == "failed"
    assert calls == [{"out": str(session[0] / "s0000/compiler"), "timeout": 25 // commands}]
    state = R.load_json(session[0] / "session.json")
    state["deadline"] = 1000.0 + commands - 1
    R.write_json(session[0] / "session.json", state)
    failed = G.step(session[0], {"op": op, **({"request": request} if op == "prepare_compiler_adapter" else {})})
    assert failed["feedback"]["status"] == "failed" and len(calls) == 1
    assert "budget exhausted" in failed["feedback"]["feedback"]["error"]


@pytest.mark.parametrize("where", ["tool", "request"])
@pytest.mark.parametrize("field,value", [("source", "arbitrary model source"), ("flags", ["-Xlinker", "unexpected"]),
                                         ("compiler", "/tmp/another-compiler"), ("out", "/tmp/another-output")])
def test_compiler_preparation_refuses_model_source_flags_and_output_overrides_before_build(session, tmp_path, monkeypatch, where, field, value):
    request, calls, _ = compiler_request_fixture(tmp_path, monkeypatch)
    command = {"op": "prepare_compiler_adapter", "request": request}
    (command if where == "tool" else command["request"])[field] = value
    if where == "tool":
        with pytest.raises(ValueError, match="tool arguments"):
            G.step(session[0], command)
    else:
        result = G.step(session[0], command)
        assert result["feedback"]["status"] == "failed"
        assert result["context"]["proof_created"] is False
    assert not calls and not (session[0] / "s0000/compiler").exists()


@pytest.mark.parametrize("op", ["inspect_compiler_toolchain", "prepare_compiler_adapter"])
@pytest.mark.parametrize("timeout", [0, 121, True])
def test_compiler_tool_timeouts_are_typed_and_bounded_before_any_command(session, tmp_path, monkeypatch, op, timeout):
    request, calls, _ = compiler_request_fixture(tmp_path, monkeypatch)
    result = G.step(session[0], {"op": op, "timeout": timeout,
        **({"request": request} if op == "prepare_compiler_adapter" else {})})
    assert result["feedback"]["status"] == "failed" and not calls
    assert "bounded tool limit" in result["feedback"]["feedback"]["error"]
    assert not (session[0] / "s0000/compiler").exists()


@pytest.mark.parametrize("failure", ["policy", "original_ir", "compiler"])
def test_compiler_preparation_failure_retains_diagnostics_without_implicit_retry_or_proof(session, runtime, tmp_path, monkeypatch, failure):
    from anyuse.execution import rea_native_adapter as A
    request, calls, _ = compiler_request_fixture(tmp_path, monkeypatch)
    if failure == "policy":
        monkeypatch.delenv("ANYUSE_REA_NATIVE_ADAPTER_TOOLCHAIN_POLICY")
    elif failure == "original_ir":
        Path(request["evidence"]["ir"]["file"]).write_text("Changed independent original compiler IR")
    else:
        import types
        def reject(argv, **kwargs):
            calls.append(list(argv))
            return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"Inert compiler refusal")
        monkeypatch.setattr(A.subprocess, "run", reject)
    result = G.step(session[0], {"op": "prepare_compiler_adapter", "request": request})
    assert result["feedback"]["status"] == "failed" and not runtime.sent
    assert result["context"]["status"] == "investigating" and result["context"]["proof_created"] is False
    assert len(calls) == {"policy": 0, "original_ir": 3, "compiler": 1}[failure]
    assert "s0000/compiler-preparation-failure.json" in result["context"]["artifacts"]
    assert not (session[0] / "s0000/compiler/adapter.dylib").exists()
    if failure == "compiler":
        log = "s0000/compiler/toolchain/version.stderr"
        assert log in result["context"]["artifacts"]
        assert "Inert compiler refusal" in G.step(session[0], {"op": "evidence", "artifact": log})["feedback"]["feedback"]["text"]


@pytest.mark.parametrize("op", ["inspect_compiler_toolchain", "prepare_compiler_adapter"])
@pytest.mark.parametrize("status", ["candidate", "verified", "published", "evaluation_failed"])
def test_compiler_tools_close_at_evaluation_before_compiler_or_policy_access(session, tmp_path, monkeypatch, op, status):
    from anyuse.execution import rea_native_adapter as A
    calls = []
    monkeypatch.setattr(A, "inspect_toolchain", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(A, "build", lambda *args, **kwargs: calls.append(args))
    state = R.load_json(session[0] / "session.json")
    state["status"] = status
    R.write_json(session[0] / "session.json", state)
    result = G.step(session[0], {"op": op, **({"request": {}} if op == "prepare_compiler_adapter" else {})})
    assert result["feedback"]["status"] == "failed" and not calls
    assert "closed once evaluation starts" in result["feedback"]["feedback"]["error"]
    assert not (session[0] / "s0000/compiler").exists()


def test_unknown_nested_native_binding_is_refused_before_recording(session, runtime, runtime_tools, tmp_path):
    root, profile, _, actor = native_session(session, tmp_path)
    observed = G.step(root, {"op": "discover", "request": {"operations": [{"op": "modules"}]}})
    artifact = next(name for name in observed["context"]["artifacts"] if name.endswith("runtime.json"))
    actor["arguments"][2]["value"]["counter"] = {"input": "not_a_frozen_input"}
    result = G.step(root, {"op": "propose", "profile": profile, "actor": actor,
        "rationale": "Unvalidated compound binding", "citations": [{"artifact": artifact, "pointer": "/result/operations/0"}]})
    assert result["feedback"]["status"] == "failed" and not runtime.sent
    assert result["context"]["proof_created"] is False
    assert "frozen task input" in result["feedback"]["feedback"]["error"]


@pytest.mark.parametrize("source", ["read", "swift_read"])
def test_borrowed_field_pointer_uses_the_same_frozen_agent_publication_loop(session, runtime, runtime_tools, tmp_path, source):
    root, profile, _, original = native_session(session, tmp_path)
    read = native_swift_read(tmp_path) if source == "swift_read" else native_pointer_read(tmp_path)
    types = [argument["type"] for argument in original["arguments"]] + ["pointer"]
    values = [argument["value"] for argument in original["arguments"]] + [{source: read}]
    actor = native_template(tmp_path, types, values)
    observed = G.step(root, {"op": "discover", "request": {"operations": [{"op": "modules", "limit": 4}]}})
    artifact = next(name for name in observed["context"]["artifacts"] if name.endswith("runtime.json"))
    proposed = G.step(root, {"op": "propose", "profile": profile, "actor": actor,
        "rationale": "Pinned field metadata resolves the borrowed pointer each time; complete output remains independent.",
        "citations": [{"artifact": artifact, "pointer": "/result/operations/0"}]})
    assert proposed["context"]["status"] == "hypothesis"
    for case in range(3):
        assert G.step(root, {"op": "record", "case": case})["feedback"]["status"] == "completed"
    assert G.step(root, {"op": "learn"})["context"]["status"] == "candidate"
    assert G.step(root, {"op": "verify"})["context"]["status"] == "verified"
    assert G.step(root, {"op": "heldout"})["feedback"]["status"] == "completed"
    published = G.step(root, {"op": "publish", "summary": "Apply result through native state", "details": "Borrowed ivar path with full independent AX output"})
    assert published["context"]["status"] == "published" and len(runtime.sent) == 9
    state = R.load_json(root / "session.json")
    proof = R.load_json(state["proof"])
    assert read["evidence"]["file"] in proof["resources"]
    if source == "swift_read":
        observation = proof["cases"][0]["run"]["runtime"]["result"]["operations"][0]["swift_field_reads"][0]
        assert observation["source"] == "swift_metadata" and observation["recipe"] == read
    assert R.load_json(state["candidate"])["actor"] == actor


def test_cited_two_method_sequence_uses_same_persistent_proof_and_publication_loop(session, runtime, runtime_tools, tmp_path):
    original = R.load_json(session[0] / "session.json")
    profile = copy.deepcopy(session[1])
    profile["params"] += [{"name": "counter", "type": "string", "source": "name"},
                          {"name": "animate", "type": "string", "source": "text"}]
    plan = copy.deepcopy(session[2])
    for i, case in enumerate(case for role in G.ROLES for case in plan[role]):
        case["inputs"].update(counter=str(10 + i), animate="true" if i % 2 == 0 else "false")
    root = tmp_path / "sequence-session"
    G.begin(original["bundle"], original["rea"], original["task"], profile["params"],
            original["success_criteria"], plan, str(root))
    observed = G.step(root, {"op": "runtime", "request": {"operations": [{"op": "classes", "query": "MappedReader"}]}})
    artifact = next(x for x in observed["context"]["artifacts"] if x.endswith("runtime.json"))
    actor = objc_sequence({"input": "query"}, {"input": "reference"}, {"input": "counter"}, {"input": "animate"})
    request = {"op": "propose", "profile": profile, "actor": actor,
               "rationale": "Two ordered typed calls with an independent complete result contract.",
               "citations": [{"artifact": artifact, "pointer": "/result/operations/0"}]}
    assert G.step(root, request)["context"]["status"] == "hypothesis"
    for i in range(3):
        assert G.step(root, {"op": "record", "case": i})["feedback"]["status"] == "completed"
    assert G.step(root, {"op": "learn"})["context"]["status"] == "candidate"
    assert G.step(root, {"op": "verify"})["context"]["status"] == "verified"
    assert G.step(root, {"op": "heldout"})["feedback"]["status"] == "completed"
    published = G.step(root, {"op": "publish", "summary": "Apply result through two typed methods", "details": "Ordered recipe with complete independent AX content checks"})
    assert published["context"]["status"] == "published" and len(runtime.sent) == 9
    candidate = R.load_json(R.load_json(root / "session.json")["candidate"])
    assert candidate["actor"] == actor


def test_old_raw_method_address_cannot_substitute_for_typed_runtime_recipe(session, runtime):
    request = proposal(session)
    request["actor"] = {"kind": "objc_method", "symbol": "doThing:", "address": "0x1234"}
    failure = G.step(session[0], request)
    assert failure["feedback"]["status"] == "failed" and not runtime.sent
    assert failure["context"]["proof_created"] is False


def test_agent_receives_failed_hypothesis_feedback_can_amend_before_evaluation(session, runtime, monkeypatch):
    root = session[0]
    request = proposal(session)
    G.step(root, request)
    runtime.wrong = True
    clock = iter(range(1000))
    monkeypatch.setattr(H.time, "monotonic", lambda: next(clock))
    failure = G.step(root, {"op": "record", "case": 0, "timeout": 1})
    assert failure["context"]["status"] == "hypothesis_failed"
    assert "independent task reference" in failure["feedback"]["feedback"]["error"]
    runtime.wrong = False
    request["rationale"] = "Amend the earlier failed hypothesis using recorded feedback."
    amended = G.step(root, request)
    assert amended["context"]["status"] == "hypothesis"
    assert amended["context"]["recorded_cases"] == []
    assert (root / "s0002/record/recording-check.json").exists()


@pytest.mark.parametrize("phase", ["verify", "heldout"])
def test_evaluation_failure_cannot_be_tuned_and_retried_on_same_inputs(session, runtime, monkeypatch, phase):
    root = session[0]
    learned(session)
    if phase == "heldout":
        G.step(root, {"op": "verify"})
    runtime.wrong = True
    clock = iter(range(1000))
    monkeypatch.setattr(H.time, "monotonic", lambda: next(clock))
    result = G.step(root, {"op": phase, "timeout": 1})
    assert result["context"]["status"] == "evaluation_failed"
    before = len(runtime.sent)
    assert G.step(root, {"op": phase})["feedback"]["status"] == "failed"
    assert len(runtime.sent) == before
    for invalid in ({"op": "record", "case": 0}, {"op": "learn"}):
        result = G.step(root, invalid)
        assert result["context"]["status"] == "evaluation_failed"
    request = proposal(session)
    assert G.step(root, request)["feedback"]["status"] == "failed"
    assert len(runtime.sent) == before


@pytest.mark.parametrize("change", ["input", "app"])
def test_frozen_input_or_same_version_app_change_stops_before_native_action(session, runtime, change):
    root, _, plan = session
    if change == "input":
        Path(plan["verification"][0]["inputs"]["reference"]).write_text("changed")
    else:
        state = R.load_json(root / "session.json")
        (Path(state["bundle"]) / "Contents/change.txt").write_text("same version, different bytes")
    with pytest.raises(ValueError, match="changed"):
        G.step(root, {"op": "observe"})
    assert not runtime.sent


def test_only_pinned_evidence_and_existing_pointer_can_support_proposal(session):
    request = proposal(session)
    request["citations"][0]["pointer"] = "/content/invented"
    result = G.step(session[0], request)
    assert "pointer does not exist" in result["feedback"]["feedback"]["error"]
    request["citations"][0]["artifact"] = "/tmp/model-proof.json"
    assert "unknown session artifact" in G.step(session[0], request)["feedback"]["feedback"]["error"]


@pytest.mark.parametrize("phase,artifact", [("learn", "recording"), ("verify", "candidate"), ("heldout", "proof"), ("publish", "candidate")])
def test_changed_saved_trial_artifacts_refuse_before_further_actuation(session, runtime, phase, artifact):
    root = session[0]
    if phase == "learn":
        G.step(root, proposal(session))
        for i in range(3):
            G.step(root, {"op": "record", "case": i})
    else:
        learned(session)
        if phase in {"heldout", "publish"}:
            G.step(root, {"op": "verify"})
        if phase == "publish":
            G.step(root, {"op": "heldout"})
    state = R.load_json(root / "session.json")
    path = state["recordings"]["0"] if artifact == "recording" else state[artifact]
    value = R.load_json(path)
    value["injected"] = "different artifact after registration"
    R.write_json(path, value)
    before = len(runtime.sent)
    request = {"op": phase}
    if phase == "publish":
        request.update(summary="Read", details="Complete result")
    result = G.step(root, request)
    assert result["feedback"]["status"] == "failed"
    assert "evidence changed" in result["feedback"]["feedback"]["error"]
    assert len(runtime.sent) == before


def test_invalid_learning_calls_cannot_reopen_published_action(session, runtime):
    root = session[0]
    learned(session)
    G.step(root, {"op": "verify"})
    G.step(root, {"op": "heldout"})
    G.step(root, {"op": "publish", "summary": "Read", "details": "Complete independent result"})
    before = len(runtime.sent)
    for invalid in ({"op": "record", "case": 0}, {"op": "learn"}):
        assert G.step(root, invalid)["context"]["status"] == "published"
    request = proposal(session)
    assert G.step(root, request)["feedback"]["status"] == "failed"
    assert len(runtime.sent) == before


def test_symlink_cannot_substitute_another_registered_candidate(session, runtime):
    root = session[0]
    learned(session)
    state = R.load_json(root / "session.json")
    original = Path(state["candidate"])
    other = root / "other-candidate.json"
    value = R.load_json(original)
    value["label"] = "Different registered candidate"
    R.write_json(other, value)
    G._artifact(state, root, other)
    R.write_json(root / "session.json", state)
    original.unlink()
    original.symlink_to(other)
    before = len(runtime.sent)
    result = G.step(root, {"op": "verify"})
    assert result["feedback"]["status"] == "failed"
    assert "evidence changed" in result["feedback"]["feedback"]["error"]
    assert len(runtime.sent) == before


def test_agent_cannot_change_frozen_parameters_or_supply_executable_code(session, runtime):
    request = proposal(session)
    request["profile"] = copy.deepcopy(request["profile"])
    request["profile"]["params"][0]["name"] = "different_task"
    assert G.step(session[0], request)["feedback"]["status"] == "failed"
    request = proposal(session)
    request["profile"]["python"] = "do stuff"
    assert G.step(session[0], request)["feedback"]["status"] == "failed"
    assert not runtime.sent


def test_cleanup_failure_returns_partial_evidence_and_stops_followup_analysis(session, monkeypatch):
    calls = []
    def failure(bundle, rea, out, **kwargs):
        folder = Path(out)
        folder.mkdir()
        R.write_json(folder / "analysis.json", {"status": "incomplete", "resource_review_required": True})
        calls.append(kwargs)
        raise R.ResourceReviewRequired("cleanup incomplete")
    monkeypatch.setattr(R, "investigate", failure)
    result = G.step(session[0], {"op": "analyze", "query": "load", "timeout": 300})
    assert result["context"]["status"] == "resource_review_required"
    assert "s0000/analysis/analysis.json" in result["context"]["artifacts"]
    with pytest.raises(R.ResourceReviewRequired):
        G.step(session[0], {"op": "analyze", "function": "0x1000"})
    assert len(calls) == 1
    read = G.step(session[0], {"op": "evidence", "artifact": "s0000/analysis/analysis.json"})
    assert read["feedback"]["feedback"]["text_is_data"]


def test_step_budget_and_interruption_preserve_history_without_another_action(session, runtime):
    root = session[0]
    state = R.load_json(root / "session.json")
    state["steps"] = [{"status": "running", "request": {"op": "record"}}]
    R.write_json(root / "session.json", state)
    with pytest.raises(R.ResourceReviewRequired, match="interrupted"):
        G.step(root, {"op": "observe"})
    state["steps"] = [{"status": "completed"}] * state["max_steps"]
    R.write_json(root / "session.json", state)
    with pytest.raises(ValueError, match="budget"):
        G.step(root, {"op": "observe"})
    assert not runtime.sent


def test_vm_guard_precedes_session_reads_writes_and_tools(session, monkeypatch, tmp_path):
    def refuse():
        raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    for fn in [lambda: G.begin("missing", "missing", "", [], [], {}, str(tmp_path / "bad")),
               lambda: G.context("missing"), lambda: G.step("missing", {"op": "analyze"}),
               lambda: G.step("missing", {"op": "inspect_compiler_toolchain"}),
               lambda: G.step("missing", {"op": "prepare_compiler_adapter", "request": {}})]:
        with pytest.raises(PermissionError, match="VM only"):
            fn()
    assert not (tmp_path / "bad").exists()


@pytest.fixture
def svg_session(svg_fixture, tmp_path):
    data = svg_fixture
    rea = tmp_path / "svg-rea-inert"
    rea.write_text("inert fixture, never executed")
    rea.chmod(0o700)
    root = tmp_path / "svg-agent-session"
    plan = {role: [{"inputs": copy.deepcopy(row)} for row in data["inputs"][i * 3:(i + 1) * 3]]
            for i, role in enumerate(G.ROLES)}
    G.begin(str(data["app"]), str(rea), "Load complete ordered diagram label content", data["profile"]["params"],
            ["All label units, duplicates, empty values and exact Unicode must match the independent JSON oracle"],
            plan, str(root))
    return root, data, plan


def test_agent_explicit_null_observer_preserves_default_ax_context(session, runtime):
    request = proposal(session)
    request["profile"] = copy.deepcopy(request["profile"])
    request["profile"]["observer"] = None
    result = G.step(session[0], request)
    assert result["context"]["status"] == "hypothesis"
    assert result["context"]["capabilities"]["proof_observer"] == "learned_ax"
    assert G.context(session[0])["capabilities"]["proof_observer"] == "learned_ax"
    assert result["context"]["proof_created"] is False and not runtime.sent


def svg_proposal(session):
    root, data, _ = session
    result = G.step(root, {"op": "resource", "relative": "Contents/Info.plist"})
    assert result["feedback"]["status"] == "completed"
    artifact = next(name for name in result["context"]["artifacts"] if name.endswith("resource.json"))
    return {"op": "propose", "profile": data["profile"], "actor": data["actor"],
            "rationale": "Reviewed shared internal method with separate complete DOM text observer and frozen independent cases.",
            "citations": [{"artifact": artifact, "pointer": "/content/CFBundleIdentifier"}]}


def svg_agent_learned(session):
    root = session[0]
    proposed = G.step(root, svg_proposal(session))
    assert proposed["context"]["status"] == "hypothesis"
    assert proposed["context"]["capabilities"]["proof_observer"] == session[1]["profile"]["observer"]["kind"]
    assert proposed["context"]["proof_created"] is False
    for index in range(3):
        assert G.step(root, {"op": "record", "case": index, "timeout": 5})["feedback"]["status"] == "completed"
    learned = G.step(root, {"op": "learn"})
    assert learned["context"]["status"] == "candidate" and learned["context"]["proof_created"] is False


@pytest.mark.parametrize("svg_fixture", ["strict", "mixed"], indirect=True)
def test_agent_svg_three_record_fresh_heldout_and_publish_use_shared_independent_gate(svg_session):
    root, data, _ = svg_session
    assert G.context(root)["capabilities"]["proof_observer"] == "learned_ax"
    svg_agent_learned(svg_session)
    early = G.step(root, {"op": "publish", "summary": "Diagram", "details": "Complete ordered labels"})
    assert early["feedback"]["status"] == "failed" and len(data["runtime"].sent) == 3
    verified = G.step(root, {"op": "verify", "timeout": 5})
    assert verified["context"]["status"] == "verified" and verified["context"]["proof_created"] is True
    checked = G.step(root, {"op": "heldout", "timeout": 5})
    assert checked["feedback"]["status"] == "completed"
    published = G.step(root, {"op": "publish", "summary": "Load exact complete diagram label text",
                              "details": "Ordered DOM label text only; visibility, geometry, edges and persistence unproved"})
    assert published["context"]["status"] == "published" and len(data["runtime"].sent) == 9
    state = R.load_json(root / "session.json")
    candidate = R.load_json(state["candidate"])
    proof = R.load_json(state["proof"])
    assert candidate["backend"] == H.SVG_BACKEND
    assert candidate["observer"] == data["profile"]["observer"] and "selectors" not in candidate
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    assert all(row["observer"] == data["profile"]["observer"]["kind"] for row in proof["cases"])
    assert all(row["after_observation"]["task_result_verified"] is False for row in proof["cases"])
    assert state["heldout_passed"] is True and checked["feedback"]["feedback"]["heldout_cases"] == 3


def test_agent_svg_proof_filename_changes_cannot_create_semantic_heldout_novelty(svg_session):
    root, data, _ = svg_session
    state = R.load_json(root / "session.json")
    plan = copy.deepcopy(state["plan"])
    plan["heldout"][0]["inputs"]["payload"] = plan["learning"][0]["inputs"]["payload"]
    root = root.with_name("svg-agent-duplicate-session")
    G.begin(state["bundle"], state["rea"], state["task"], state["params"], state["success_criteria"], plan, str(root))
    request = svg_proposal((root, data, plan))
    result = G.step(root, request)
    assert result["feedback"]["status"] == "failed"
    assert "task inputs must be disjoint" in result["feedback"]["feedback"]["error"]
    assert result["context"]["proof_created"] is False and not data["runtime"].sent


@pytest.mark.parametrize("param", ["oracle", "observer_contract"])
def test_agent_svg_future_reference_bytes_frozen_before_any_record(svg_session, param):
    root, data, plan = svg_session
    assert G.step(root, svg_proposal(svg_session))["context"]["status"] == "hypothesis"
    future = Path(plan["heldout"][-1]["inputs"][param])
    future.write_bytes(future.read_bytes() + b" ")
    with pytest.raises(ValueError, match="frozen input/reference"):
        G.step(root, {"op": "record", "case": 0, "timeout": 5})
    assert not data["runtime"].sent


def test_agent_svg_failed_evaluation_is_closed_without_method_retry_or_observer_fallback(svg_session):
    root, data, _ = svg_session
    svg_agent_learned(svg_session)
    data["runtime"].wrong = True
    result = G.step(root, {"op": "verify", "timeout": 5})
    assert result["context"]["status"] == "evaluation_failed" and result["context"]["proof_created"] is False
    assert len(data["runtime"].sent) == 4
    for request in [{"op": "record", "case": 0}, {"op": "verify"},
                    {"op": "discover", "provider": "inspector", "request": {"operations": [{
                        "op": "dom_query", "selector": "svg", "max_nodes": 1}]}}]:
        assert G.step(root, request)["feedback"]["status"] == "failed"
    assert len(data["runtime"].sent) == 4


def test_native_guide_is_bounded_fictitious_data_and_context_copies_it(session):
    guide = G.native_guide()
    raw = json.dumps(guide, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(raw.encode()) <= 10 * 1024
    assert guide["format"] == "anyuse.rea-native-objc-guide/1"
    assert "fictitious" in guide["placeholders"] and "never try" in guide["placeholders"]
    for old_target in ["IINA", "QuickTime", "Chess", "Flameshot", "MarkText", "draw.io", "pipActionPause"]:
        assert old_target not in raw
    assert H.PROFILE in raw
    assert G.context(session[0])["native_guide"] == guide
    guide["runtime"]["invoke_example"]["selector"] = "changed"
    assert G.native_guide()["runtime"]["invoke_example"]["selector"] == "exampleApply:"


def test_native_guide_examples_pass_real_pure_provider_and_actor_validators():
    from anyuse.execution import rea_runtime_providers as P
    from anyuse.execution import rea_runtime_bridge as B
    guide = G.native_guide()
    provider = P.for_name("native")
    assert provider.validate_discovery(guide["discover"]["envelope"]["request"])
    for operation in guide["discover"]["metadata_operations"]:
        assert provider.validate_discovery({"operations": [operation]})
    assert B.validate_request(guide["runtime"]["envelope"]["request"])
    invoke = guide["runtime"]["invoke_example"]
    assert B.validate_request({"operations": [invoke]})
    actor = guide["propose"]["actor_example"]
    assert provider.validate_actor(actor) == actor
    assert B.invocation_request(actor)["operations"][0]["op"] == "invoke"
    # Exercise every documented receiver alternative and every field example.
    for receiver in guide["runtime"]["receiver_examples"]:
        prefix = [{"op": "instances", "class": "ExampleObject", "alias": "example_object"}] if "alias" in receiver else []
        assert B.validate_request({"operations": prefix + [
            {"op": "read", "receiver": receiver, "fields": guide["runtime"]["read_fields"]},
            {**invoke, "receiver": {"alias": "example_child"}},
        ]})


def test_native_guide_invocation_is_not_discovery_and_alias_cannot_be_saved():
    from anyuse.execution import rea_runtime_providers as P
    from anyuse.execution import rea_runtime_bridge as B
    guide = G.native_guide()
    for operation in [guide["runtime"]["envelope"]["request"]["operations"][0],
                      guide["runtime"]["invoke_example"]]:
        with pytest.raises(ValueError, match="discovery"):
            P.for_name("native").validate_discovery({"operations": [operation]})
    actor = copy.deepcopy(guide["propose"]["actor_example"])
    actor["receiver"] = {"alias": "example_child"}
    with pytest.raises(ValueError, match="alias"):
        B.validate_actor(actor)
    actor = copy.deepcopy(guide["propose"]["actor_example"])
    del actor["receiver"]["path"][0]["signature"]
    with pytest.raises(ValueError):
        B.validate_actor(actor)
    operation = copy.deepcopy(guide["runtime"]["invoke_example"])
    operation["generated_code"] = "not admitted"
    with pytest.raises(ValueError):
        B.validate_request({"operations": [operation]})


def test_native_guide_proposal_uses_registered_citation_and_required_caller_profile(session):
    root, profile, _ = session
    guide = G.native_guide()["propose"]
    request = {"op": "propose", "rationale": "Syntax-only fictitious actor; no execution claim.",
               "citations": [guide["citation_example"]], "actor": guide["actor_example"]}
    assert "observer_profile_sha256" in guide["syntax"] and "manual G.step" in guide["syntax"]
    assert "caller-approved" in guide["profile_rules"][0]
    refused = G.step(root, request)
    assert refused["feedback"]["status"] == "failed" and not refused["context"]["proof_created"]
    accepted = G.step(root, {**request, "profile": profile})
    assert accepted["context"]["status"] == "hypothesis" and not accepted["context"]["proof_created"]
    assert accepted["context"]["recorded_cases"] == []


def begin_approved(session, tmp_path, declaration=None, name="approved-session"):
    old, profile, plan = session
    state = R.load_json(old / "session.json")
    declaration = ({k: copy.deepcopy(v) for k, v in profile.items() if k != "evidence"}
                   if declaration is None else declaration)
    root = tmp_path / name
    result = G.begin(state["bundle"], state["rea"], state["task"], state["params"], state["success_criteria"],
                     plan, root, observer_profile=declaration)
    return (root, profile, plan), declaration, result


def test_caller_observer_is_private_frozen_data_and_hash_binding_uses_fresh_citations(session, tmp_path, monkeypatch):
    declaration = {k: copy.deepcopy(v) for k, v in session[1].items() if k != "evidence"}
    declaration["reference"]["fields"]["private"] = {"kind": "constant", "value": "EXPECTED_VALUE_PRIVATE"}
    declaration["reference"]["fields"]["dictionary"] = {"kind": "dictionary_xml", "param": "reference",
        "dictionary_path": "/PRIVATE_DICTIONARY.dictionary", "dictionary_label": "PRIVATE_DICTIONARY_LABEL",
        "dictionary_sha256": "1" * 64, "reference_xml": "Contents/Resources/PRIVATE.xml"}
    declaration["scope"] = {"observer_userdata": "PRIVATE_SCOPE_DATA"}
    with monkeypatch.context() as patch:
        patch.setattr(H, "expected", lambda *a, **k: pytest.fail("begin must not derive an oracle value"))
        approved, original, context = begin_approved(session, tmp_path, declaration)
    frozen = R.load_json(approved[0] / "session.json")
    assert frozen["observer_profile"] == original and frozen["observer_profile_sha256"] == R.digest(original)
    original["label"] = "Caller mutated its own dictionary"
    assert G.context(approved[0])["observer_profile"]["declaration"]["label"] != original["label"]
    # Prove the public projection without needing this intentionally unavailable dictionary oracle.
    for secret in ["EXPECTED_VALUE_PRIVATE", "PRIVATE_DICTIONARY", "PRIVATE_SCOPE_DATA", "PRIVATE.xml"]:
        assert secret not in json.dumps(context)
    assert context["observer_profile"]["proof_created"] is False
    assert context["observer_profile"]["declaration"]["reference"]["fields"]["private"] == {"kind": "constant"}

    normal, _, _ = begin_approved(session, tmp_path, name="hash-only-session")
    request = proposal(normal)
    request.pop("profile")
    request["observer_profile_sha256"] = G.context(normal[0])["observer_profile_sha256"]
    result = G.step(normal[0], request)
    assert result["feedback"]["status"] == "completed" and result["context"]["status"] == "hypothesis"
    state = R.load_json(normal[0] / "session.json")
    assert {k: v for k, v in state["hypothesis"]["profile"].items() if k != "evidence"} == state["observer_profile"]
    assert state["hypothesis"]["profile"]["evidence"] != session[1]["evidence"]
    assert "profile" not in result["context"]["hypothesis"]


@pytest.mark.parametrize("mutation", ["actor", "evidence", "citations", "case_values", "unknown", "family", "params", "oversize", "nonfinite"])
def test_invalid_observer_declaration_refuses_before_session_creation(session, tmp_path, mutation):
    declaration = {k: copy.deepcopy(v) for k, v in session[1].items() if k != "evidence"}
    if mutation in {"actor", "evidence", "citations", "case_values", "unknown"}:
        declaration[mutation] = {}
    elif mutation == "family":
        declaration["format"] = "anyuse.rea-fixed-state-profile/1"
    elif mutation == "params":
        declaration["params"] = declaration["params"][:-1]
    elif mutation == "oversize":
        declaration["scope"] = "x" * G.MAX_OBSERVER_PROFILE_BYTES
    else:
        declaration["scope"] = float("nan")
    with pytest.raises(ValueError):
        begin_approved(session, tmp_path, declaration)
    assert not (tmp_path / "approved-session").exists()


@pytest.mark.parametrize("mutation", ["hash", "constant", "effect", "reference", "missing_binding"])
def test_approved_profile_cannot_be_replaced_by_a_model_proposal(session, tmp_path, mutation):
    approved, _, _ = begin_approved(session, tmp_path)
    request = proposal(approved)
    request["observer_profile_sha256"] = G.context(approved[0])["observer_profile_sha256"]
    if mutation == "hash":
        request["observer_profile_sha256"] = "0" * 64
    elif mutation == "constant":
        request["profile"]["reference"]["fields"]["fabricated"] = {"kind": "constant", "value": "invented"}
    elif mutation == "effect":
        request["profile"]["effect"] = "read_content"
    elif mutation == "reference":
        request["profile"]["reference"]["fields"]["body"]["normalization"] = "exact"
    else:
        request.pop("profile"); request.pop("observer_profile_sha256")
    result = G.step(approved[0], request)
    assert result["feedback"]["status"] == "failed"
    assert result["context"]["status"] == "investigating" and result["context"]["recorded_cases"] == []
    assert not result["context"]["proof_created"]


def test_matching_full_profile_hides_private_values_from_context_and_history(session, tmp_path):
    declaration = {k: copy.deepcopy(v) for k, v in session[1].items() if k != "evidence"}
    declaration["reference"]["fields"]["label"] = {"kind": "constant", "value": "PRIVATE_APPROVED_LABEL"}
    approved, _, _ = begin_approved(session, tmp_path, declaration)
    request = proposal(approved)
    request["profile"] = {**declaration, "evidence": [{"invented": "not used as evidence"}]}
    result = G.step(approved[0], request)
    assert result["feedback"]["status"] == "completed"
    assert "PRIVATE_APPROVED_LABEL" not in json.dumps(result["context"])
    state = R.load_json(approved[0] / "session.json")
    assert state["observer_profile"]["reference"]["fields"]["label"]["value"] == "PRIVATE_APPROVED_LABEL"
    assert "invented" not in json.dumps(state["hypothesis"]["profile"]["evidence"])


@pytest.mark.parametrize("mutation", ["declaration", "hash", "missing", "wrong_family"])
def test_frozen_observer_tamper_is_refused_on_context_reload(session, tmp_path, mutation):
    approved, _, _ = begin_approved(session, tmp_path)
    file = approved[0] / "session.json"
    state = R.load_json(file)
    if mutation == "declaration":
        state["observer_profile"]["label"] = "changed"
    elif mutation == "hash":
        state["observer_profile_sha256"] = "0" * 64
    elif mutation == "missing":
        state.pop("observer_profile")
    else:
        state["observer_profile"]["format"] = "unknown"
        state["observer_profile_sha256"] = R.digest(state["observer_profile"])
    R.write_json(file, state)
    with pytest.raises(ValueError):
        G.context(approved[0])


def test_unapproved_manual_session_keeps_legacy_profile_and_cannot_claim_hash_approval(session):
    context = G.context(session[0])
    assert "observer_profile" not in context and "observer_profile_sha256" not in context
    request = proposal(session)
    refused = G.step(session[0], {**request, "observer_profile_sha256": "1" * 64})
    assert refused["feedback"]["status"] == "failed"
    assert G.step(session[0], request)["feedback"]["status"] == "completed"


@pytest.mark.parametrize("matched,truncated", [(0, False), (7, True)])
def test_symbol_feedback_has_genuine_counts_and_registered_reread_descriptor(session, monkeypatch, matched, truncated):
    from anyuse.execution import rea_symbols as S
    def collect(bundle, out, **kwargs):
        folder = Path(out); folder.mkdir()
        value = {"status": "evidence_collected", "receipt": str(folder / "symbols.json"), "query": kwargs["query"],
            "query_mode": "literal_casefold", "limit": kwargs["limit"],
            "counts": {"scanned_bytes": 100, "scanned_lines": 10, "matched_lines": matched, "returned_rows": min(2, matched), "oversized_lines": 0},
            "truncated": truncated, "truncation": {"scan": False, "rows": truncated, "oversized_lines": False},
            "rows": [{"untrusted": "not in feedback"}] if matched else []}
        R.write_json(folder / "symbols.json", value)
        return value
    monkeypatch.setattr(S, "collect", collect)
    result = G.step(session[0], {"op": "symbols", "query": "operation", "limit": 2})
    feedback = result["feedback"]["feedback"]
    assert result["feedback"]["status"] == "completed"
    assert feedback["counts"]["matched_lines"] == matched and feedback["truncated"] is truncated
    assert feedback["query"] == "operation" and feedback["query_mode"] == "literal_casefold"
    assert "rows" not in feedback and feedback["callable_api"] is False and feedback["proof_created"] is False
    artifact = feedback["artifact"]
    assert artifact.endswith("symbols/symbols.json") and feedback["pointer"] == "/rows"
    assert result["context"]["artifacts"][artifact]["sha256"] == feedback["receipt_sha256"] == R._file_hash(Path(feedback["receipt"]))
    assert feedback["next_read"] == {"op": "evidence", "artifact": artifact, "offset": 0, "limit": 8000}


def clean_negative_metadata(state, transaction):
    """Actual native envelope shape; no target native execution occurs in tests."""
    from anyuse.execution import rea_runtime_bridge as B
    error = {"type": "Error", "message": "exact Objective-C class is unavailable: ExampleMissing"}
    identity = {"pid": 123, "bundle": state["bundle"]}
    return {"format": B.RUNTIME, "status": "incomplete", "request": copy.deepcopy(transaction),
            "request_sha256": R.digest(transaction), "target": state["target"], "bundle": state["bundle"],
            "identity": identity, "after_identity": copy.deepcopy(identity), "resource_review_required": False,
            "actuated": False, "native_calls": False, "error": error,
            "result": {"format": B.WORKER, "status": "incomplete", "request_sha256": R.digest(transaction),
                       "invocations": 0, "native_calls": 0, "helper_load_attempts": 0,
                       "attached": True, "handles_expired": True, "native_state_uncertain": False,
                       "cleanup": {"script_unloaded": True, "detached": True}, "cleanup_errors": [],
                       "error": copy.deepcopy(error), "operations": []}}


@pytest.mark.parametrize("changed,value", [
    ("resource_review_required", True), ("actuated", True), ("native_calls", None),
    ("after_identity", {"pid": 456}), ("target", {}), ("request_sha256", "0" * 64),
    ("result.invocations", 1), ("result.native_calls", 1), ("result.helper_load_attempts", 1),
    ("result.attached", False), ("result.handles_expired", False), ("result.native_state_uncertain", True),
    ("result.cleanup", {"script_unloaded": True, "detached": False}), ("result.cleanup_errors", ["failed"]),
])
def test_uncertain_or_executed_metadata_failure_cannot_be_recovered(session, changed, value):
    state = R.load_json(session[0] / "session.json")
    transaction = {"operations": [{"op": "methods", "class": "ExampleMissing"}]}
    receipt = clean_negative_metadata(state, transaction)
    target = receipt
    parts = changed.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    assert G._negative_native_metadata(state, transaction, receipt) is None


def test_clean_metadata_negative_keeps_raw_error_without_native_or_proof(session):
    state = R.load_json(session[0] / "session.json")
    transaction = {"operations": [{"op": "methods", "class": "ExampleMissing"}]}
    receipt = clean_negative_metadata(state, transaction)
    before = copy.deepcopy(receipt)
    result = G._negative_native_metadata(state, transaction, receipt)
    assert receipt == before
    assert result["status"] == "negative_metadata_observation" and result["native_calls"] == 0
    assert result["negative_observation"]["error"] == receipt["error"]
    assert result["proof_created"] is False and result["automatic_retry"] is False
    for op in ("instances", "read", "invoke", "graph"):
        other = {"operations": [{"op": op}]}
        assert G._negative_native_metadata(state, other, clean_negative_metadata(state, other)) is None


@pytest.mark.parametrize("operation", ["classes", "methods", "ivars"])
def test_native_metadata_feedback_returns_actual_bounded_facts_and_explicit_omissions(tmp_path, operation):
    from anyuse.execution import rea_runtime_bridge as B
    root = tmp_path / "new-session"; folder = root / "s0000"; work = folder / "runtime"; work.mkdir(parents=True)
    request = {"operations": [{"op": operation, "class": "ExampleClass"}]}
    if operation == "classes":
        facts = [{"name": "ExampleClass" + str(i) + "x" * 80, "module": "/example/App"} for i in range(128)]
    elif operation == "methods":
        facts = [{"class": "ExampleClass", "selector": "exampleMethod" + str(i) + ":", "dispatch": "-",
                  "signature": "v24@0:8@16", "origin": "module_backed", "module": "/example/App",
                  "implementation_offset": "0x100"} for i in range(128)]
    else:
        facts = [{"class": "ExampleClass", "name": "field" + str(i), "encoding": "i", "offset": i * 4} for i in range(128)]
    row = {"op": operation, "index": 0, "status": "ok", "rows": facts, "matched": 128, "truncated": False}
    raw = {"format": B.WORKER, "status": "completed", "request_sha256": R.digest(request), "operations": [row]}
    R.write_json(work / "worker-output.json", raw)
    receipt = {"format": B.RUNTIME, "result": copy.deepcopy(raw)}
    before = copy.deepcopy(receipt)
    feedback = G._native_metadata_feedback(root, folder, request, receipt)
    assert receipt == before
    assert len(json.dumps(feedback, ensure_ascii=False, sort_keys=True, allow_nan=False)) <= 3000
    entry = feedback["operations"][0]
    returned = entry["names" if operation == "classes" else "rows"]
    assert returned and entry["rows_in_artifact"] == entry["matched"] == 128
    assert len(returned) + entry["summary_omitted_rows"] == 128 and entry["summary_omitted_rows"] > 0
    assert entry["truncated"] is False  # Distinct from the summary's own omissions.
    if operation == "classes":
        assert returned == [x["name"] for x in facts[:len(returned)]]
    else:
        assert all(all(facts[i][key] == value for key, value in item.items()) for i, item in enumerate(returned))
    assert feedback["source"] == {"artifact": "s0000/runtime/worker-output.json", "sha256": R._file_hash(work / "worker-output.json")}
    receipt["result"]["operations"][0]["matched"] = 0
    with pytest.raises(ValueError, match="actual worker observations"):
        G._native_metadata_feedback(root, folder, request, receipt)


def test_instance_metadata_feedback_omits_the_expired_transaction_handle(tmp_path):
    from anyuse.execution import rea_runtime_bridge as B
    root = tmp_path / "new-session"; folder = root / "s0000"; work = folder / "runtime"; work.mkdir(parents=True)
    request = {"operations": [{"op": "instances", "class": "ExampleClass", "alias": "object"}]}
    raw = {"request_sha256": R.digest(request), "status": "completed", "operations": [
        {"op": "instances", "index": 0, "status": "ok", "object": {"class": "ExampleClass", "expires": "transaction", "handle": "TRANSIENT_HANDLE"}}]}
    R.write_json(work / "worker-output.json", raw)
    result = G._native_metadata_feedback(root, folder, request, {"format": B.RUNTIME, "result": raw})
    assert result["operations"][0]["object"] == {"class": "ExampleClass", "expires": "transaction"}
    assert "TRANSIENT_HANDLE" not in json.dumps(result)


@pytest.mark.parametrize("status", ["unavailable", "incomplete"])
def test_native_preworker_failure_preserves_actual_error_and_receipt(session, monkeypatch, status):
    from anyuse.execution import rea_runtime_bridge as B
    error = {"type": "Unavailable", "message": "compiled runtime adapter is unavailable"}
    calls = []
    def collect(bundle, out, request, **kwargs):
        calls.append(request)
        folder = Path(out); folder.mkdir()
        receipt = {"format": B.RUNTIME, "status": status, "result": None, "worker": None,
                   "error": error, "actuated": False, "native_calls": False,
                   "resource_review_required": False, "receipt": str(folder / "runtime.json")}
        R.write_json(folder / "runtime.json", receipt)
        return receipt
    monkeypatch.setattr(B, "collect", collect)
    result = G.step(session[0], {"op": "discover", "provider": "native",
        "request": {"operations": [{"op": "classes", "query": "Example"}]}})
    assert len(calls) == 1 and result["feedback"]["status"] == "completed"
    feedback = result["feedback"]["feedback"]
    assert feedback["status"] == status and feedback["error"] == error
    assert feedback["receipt"].endswith("s0000/runtime/runtime.json")
    assert "observation" not in feedback and feedback["proof_created"] is False
    assert "s0000/runtime/runtime.json" in result["context"]["artifacts"]


@pytest.mark.parametrize("claim", [{"worker": {}}, {"result": {}}, {"status": "observed"}])
def test_missing_worker_artifact_is_still_refused_when_worker_or_success_is_claimed(tmp_path, claim):
    from anyuse.execution import rea_runtime_bridge as B
    root = tmp_path / "session"; folder = root / "s0000"; folder.mkdir(parents=True)
    receipt = {"format": B.RUNTIME, "status": "unavailable", "worker": None, "result": None,
               "error": {"type": "Unavailable", "message": "missing adapter"}, "actuated": False,
               "native_calls": False, "resource_review_required": False, **claim}
    with pytest.raises(ValueError, match="raw worker artifact"):
        G._native_metadata_feedback(root, folder, {"operations": [{"op": "classes"}]}, receipt)


def _visible_session_inputs(app):
    from anyuse.execution import rea_visual_campaign as C
    from anyuse.execution import rea_visual_effect as V
    params = [{"name": "value", "type": "string", "source": "text"}]
    plan = {"format": C.PLAN}
    for phase, start in zip(G.ROLES, (0, 3, 6)):
        plan[phase] = [{"id": f"visible_{i}", "inputs": {"value": str(i)},
                        "restart": phase == "heldout" and i == start} for i in range(start, start + 3)]
    profile = {"format": C.PROFILE, "id": "visible_example", "label": "Set a visible value",
        "effect": "visible_state_transition", "params": params, "scope": C.SCOPE,
        "policy": {"format": V.POLICY, "id": "visible_example", "target": R.fingerprint(app),
            "kind": "visible_state_transition", "params": params, "observer": {"window_title": None},
            "before": "The owned document is open in its starting view.",
            "after": "The visible value matches inputs.value.", "minimum_confidence": .8,
            "model": {"provider": "local", "base_url": "http://127.0.0.1:11434/v1",
                      "model": "inert-visual-model", "api_key_env": "", "retries": 0,
                      "max_tokens": 128, "timeout": 1}}}
    return params, plan, profile


def test_absent_deterministic_observer_selects_frozen_visual_fallback_without_model_call(fixture, tmp_path):
    from anyuse.execution import model_adapter as M
    app = fixture[0]; params, plan, profile = _visible_session_inputs(app)
    executable = tmp_path / "rea-visual-inert"; executable.write_text("inert"); executable.chmod(0o700)
    out = tmp_path / "visual-fallback-session"; mark = M.process_call_count()
    context = G.begin(str(app), str(executable), "Set the requested visible value", params,
        ["The complete visible result must match the frozen input"], plan, str(out), visual_fallback=profile)
    state = R.load_json(out / "session.json")
    assert context["workflow"] == state["workflow"] == "guest_model_judged_state_v1"
    assert context["verification_tier"] == "model_judged"
    assert context["verification_selection"]["reason"] == "no_deterministic_observer_declared"
    assert state["observer_profile_sha256"] == R.digest(state["observer_profile"])
    assert M.process_call_count() == mark and state["steps"] == []
    early = G.step(out, {"op": "publish", "summary": "Set a value", "details": "Unmeasured description"})
    assert early["feedback"]["status"] == "failed" and not early["context"].get("published")


def test_malformed_declared_observer_cannot_be_hidden_by_visual_fallback(fixture, tmp_path):
    app = fixture[0]; params, plan, profile = _visible_session_inputs(app)
    executable = tmp_path / "rea-inert-malformed"; executable.write_text("inert"); executable.chmod(0o700)
    out = tmp_path / "malformed-deterministic-session"
    with pytest.raises(ValueError):
        G.begin(str(app), str(executable), "Set a value", params, ["Visible value must match"], plan, str(out),
                observer_profile={"format": "unsupported-or-malformed"}, visual_fallback=profile)
    assert not out.exists()


def test_declared_deterministic_observer_keeps_its_route_with_visual_fallback_available(session, tmp_path):
    original = R.load_json(session[0] / "session.json")
    profile = {k: v for k, v in session[1].items() if k != "evidence"}
    out = tmp_path / "deterministic-priority-session"
    context = G.begin(original["bundle"], original["rea"], original["task"], original["params"],
        original["success_criteria"], original["plan"], str(out), observer_profile=profile,
        visual_fallback={"unselected": "never used to replace an actual deterministic failure"})
    assert context.get("workflow") != "guest_model_judged_state_v1"
    assert "verification_selection" not in context
    assert R.load_json(out / "session.json")["observer_profile"] == profile


def test_visual_plan_argument_domain_is_checked_before_session_creation(fixture, tmp_path):
    from anyuse.execution import model_adapter as M
    app = fixture[0]; params, plan, profile = _visible_session_inputs(app)
    profile["policy"]["input_constraints"] = {"value": {"kind": "decimal", "minimum": "0", "maximum": "7"}}
    executable = tmp_path / "rea-domain-inert"; executable.write_text("inert"); executable.chmod(0o700)
    out = tmp_path / "outside-domain"; mark = M.process_call_count()
    with pytest.raises(ValueError):
        G.begin(str(app), str(executable), "Set a bounded visible value", params, ["Value within declared domain"],
                plan, str(out), visual_fallback=profile)
    assert not out.exists() and M.process_call_count() == mark


def test_finite_enum_plan_allows_repeated_values_in_separate_complete_trials(fixture, tmp_path):
    from anyuse.execution import model_adapter as M
    app = fixture[0]; params, plan, profile = _visible_session_inputs(app)
    plan["input_mode"] = "finite_enum_coverage"
    profile["policy"]["input_constraints"] = {"value": {"kind": "enum", "values": ["on", "off"]}}
    for role in G.ROLES:
        for index, case in enumerate(plan[role]):
            case["inputs"] = {"value": "on" if index % 2 == 0 else "off"}
    executable = tmp_path / "rea-enum-inert"; executable.write_text("inert"); executable.chmod(0o700)
    out = tmp_path / "enum-session"; mark = M.process_call_count()
    context = G.begin(str(app), str(executable), "Set the requested visible switch", params,
        ["Respect the finite argument domain"], plan, str(out), visual_fallback=profile)
    assert context["workflow"] == "guest_model_judged_state_v1"
    assert R.load_json(out / "session.json")["plan"] == plan and M.process_call_count() == mark


@pytest.mark.parametrize("fail_at", ["intent", "dispatch"])
def test_visual_trial_keeps_caller_epoch_through_intent_and_dispatch(fixture, tmp_path, monkeypatch, fail_at):
    from anyuse.execution import rea_visual_effect as V
    app = fixture[0]; params, plan, profile = _visible_session_inputs(app)
    executable = tmp_path / "rea-epoch-inert"; executable.write_text("inert"); executable.chmod(0o700)
    out = tmp_path / "visible-epoch"
    G.begin(str(app), str(executable), "Set a visible value", params, ["Exact owned process only"],
            plan, str(out), visual_fallback=profile)
    state = R.load_json(out / "session.json")
    state.update(status="hypothesis", hypothesis={"profile": state["observer_profile"], "actor": {"inert": True}})
    R.write_json(out / "session.json", state)
    identity = {"bundle": str(app), "bundle_id": state["target"]["bundle_id"],
                "executable": str(app / "Contents/MacOS/Example"), "pid": 314,
                "launch_time": 1700000000.0, "uid": os.getuid()}
    seen = []
    def intent(*args, **kwargs):
        assert kwargs == {"expected_identity": identity}; seen.append("intent")
        if fail_at == "intent": raise ValueError("stale owned process before intent")
        return {"inert": True}
    def trial(*args, **kwargs):
        assert kwargs["expected_identity"] == identity; seen.append("dispatch")
        raise ValueError("stale owned process before dispatch")
    monkeypatch.setattr(V, "make_intent", intent); monkeypatch.setattr(V, "run_trial", trial)
    result = G.step(out, {"op": "record", "case": 0}, expected_runtime_identity=identity)
    assert result["feedback"]["status"] == "failed"
    assert seen == (["intent"] if fail_at == "intent" else ["intent", "dispatch"])
    assert result["context"]["status"] == "hypothesis_failed"
    assert R.load_json(out / "session.json")["steps"][-1]["expected_runtime_identity"] == identity


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_inspector_discovery_feedback_reaches_driver_with_actual_dom_descriptors_and_method(session, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I, rea_agent_driver as D
    root = session[0]
    state = R.load_json(root / 'session.json')
    request = {'operations': [
        {'op': 'dom_query', 'selector': 'body', 'max_nodes': 8},
        {'op': 'describe', 'recipe': {'root': {'kind': 'dom', 'selector': 'body'}, 'steps': []}, 'max_properties': 8},
        {'op': 'method', 'recipe': {'root': {'kind': 'global', 'name': 'observedObject'}, 'steps': []}, 'method': 'observedMethod'}]}
    source = "function observedMethod(value) { return value; }"
    rows = [
        {'op': 'dom_query', 'selector': 'body', 'total_match_count': 1, 'nodes': [
            {'tag': 'BODY', 'attributes': [['data-example', 'untrusted app data']], 'attribute_count': 1,
             'attributes_truncated': False, 'child_count': 2}], 'truncated': False, 'live_node_ids_exported': False},
        {'op': 'describe', 'properties': [{'name': 'observedObject', 'kind': 'data', 'type': 'object', 'owner_depth': 0}],
         'property_count': 1, 'truncated': False, 'whole_graph_coverage': False, 'getters_invoked': 0},
        {'op': 'method', 'method': 'observedMethod', 'function_source': source,
         'function_sha256': hashlib.sha256(source.encode()).hexdigest(), 'types_recovered_automatically': False}]
    receipt = inspector_metadata_receipt(monkeypatch, state, request, rows)
    def collect(bundle, out, transaction, timeout=30):
        assert transaction == request
        Path(out).mkdir(); R.write_json(Path(out) / 'inspector.json', receipt)
        return copy.deepcopy(receipt)
    monkeypatch.setattr(I, 'collect', collect)
    result = G.step(root, {'op': 'discover', 'provider': 'inspector', 'request': request})
    assert result['feedback']['status'] == 'completed', result['feedback']
    feedback = result['feedback']['feedback']; projected = feedback['observation']
    assert feedback['receipt'] == str(root / 's0000/runtime/inspector.json')
    assert projected['source'] == {'artifact': 's0000/runtime/inspector.json',
        'bytes': (root / 's0000/runtime/inspector.json').stat().st_size,
        'sha256': R._file_hash(root / 's0000/runtime/inspector.json')}
    assert projected['operations'][0]['nodes'][0]['attributes'][0]['pair'] == ['data-example', 'untrusted app data']
    assert projected['operations'][1]['properties'][0]['name'] == 'observedObject'
    assert projected['operations'][1]['properties'][0]['pointer'] == '/operations/1/properties/0'
    assert projected['operations'][2]['function_source'] == source
    assert projected['operations'][2]['function_sha256'] == rows[2]['function_sha256']
    assert projected['whole_graph_coverage'] is False and projected['text_is_data'] is True
    current = R.load_json(root / 'session.json')
    view, _ = D.model_view(result['context'], current, feedback, allowed_tools=('discover', 'evidence'))
    assert view['last_observation']['observation'] == projected
    assert 'observedObject' in json.dumps(view['last_observation'])
    assert current['proof'] is None and current['status'] == 'investigating'
    for example in G.TOOLS['discover']['examples'][5:]:
        assert I.validate_request(example['request']) == example['request']
    unchanged = R._file_hash(root / 'session.json')
    with pytest.raises(ValueError, match='unknown mapping tool'):
        G.step(root, {'provider': 'inspector', 'request': request})
    assert R._file_hash(root / 'session.json') == unchanged


def test_inspector_feedback_has_independent_summary_omissions_and_source_excerpts(session, monkeypatch):
    root = session[0]; folder = root / 'projection'; (folder / 'runtime').mkdir(parents=True)
    state = R.load_json(root / 'session.json')
    request = {'operations': [{'op': 'describe', 'recipe': {'root': {'kind': 'global', 'name': 'globalThis'}, 'steps': []},
        'max_properties': 32, 'prototype_depth': 0, 'offset': 0, 'property_query': ''}]}
    properties = [{'name': 'property-' + str(i) + '树' * 80, 'kind': 'data', 'type': 'object', 'owner_depth': 0} for i in range(32)]
    row = {'op': 'describe', 'properties': properties, 'property_count': 500, 'filtered_count': 500,
           'offset': 0, 'next_offset': 32, 'truncated': True, 'whole_graph_coverage': False, 'getters_invoked': 0}
    receipt = inspector_metadata_receipt(monkeypatch, state, request, [row])
    file = folder / 'runtime/inspector.json'; R.write_json(file, receipt)
    before = copy.deepcopy(receipt)
    value = G._inspector_metadata_feedback(state, root, folder, request, receipt)
    assert receipt == before and len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()) <= 4500
    projected = value['operations'][0]
    assert projected['next_offset'] == 32 and projected['truncated'] is True
    assert projected['properties_in_artifact'] == 32 and projected['properties_summary_omitted'] > 0
    assert len(projected['properties']) + projected['properties_summary_omitted'] == 32
    assert projected['properties'][0]['name'] == properties[0]['name']
    # A separate short operation demonstrates a bounded excerpt with the full-body hash.
    source = 'function example() {' + ' ' * 1500 + '}'
    request = {'operations': [{'op': 'method', 'recipe': {'root': {'kind': 'global', 'name': 'example'}, 'steps': []}, 'method': 'example'}]}
    row = {'op': 'method', 'method': 'example', 'function_source': source,
           'function_sha256': hashlib.sha256(source.encode()).hexdigest(), 'types_recovered_automatically': False}
    receipt = inspector_metadata_receipt(monkeypatch, state, request, [row]); R.write_json(file, receipt)
    value = G._inspector_metadata_feedback(state, root, folder, request, receipt)['operations'][0]
    assert 'function_source' not in value and value['function_source_excerpt'] == source[:1024]
    assert value['function_source_summary_omitted_characters'] == len(source) - 1024
    assert value['function_sha256'] == hashlib.sha256(source.encode()).hexdigest()


@pytest.mark.parametrize('mutation', ['raw_return_mismatch', 'target', 'identity', 'request', 'cleanup', 'adapter', 'transport', 'source_hash'])
def test_inspector_feedback_refuses_raw_or_binding_tampering(session, monkeypatch, mutation):
    root = session[0]; folder = root / 'projection'; (folder / 'runtime').mkdir(parents=True)
    state = R.load_json(root / 'session.json')
    request = {'operations': [{'op': 'method', 'recipe': {'root': {'kind': 'global', 'name': 'example'}, 'steps': []}, 'method': 'example'}]}
    source = 'function example() { return true; }'
    receipt = inspector_metadata_receipt(monkeypatch, state, request, [{'op': 'method', 'method': 'example',
        'function_source': source, 'function_sha256': hashlib.sha256(source.encode()).hexdigest()}])
    raw_receipt = copy.deepcopy(receipt)
    if mutation == 'raw_return_mismatch': raw_receipt['operations'][0]['method'] = 'changed'
    else:
        if mutation == 'target': raw_receipt['target']['sha256'] = '0' * 64
        elif mutation == 'identity': raw_receipt['after_identity']['pid'] += 1
        elif mutation == 'request': raw_receipt['request']['operations'][0]['method'] = 'changed'
        elif mutation == 'cleanup': raw_receipt['cleanup']['object_group_released'] = False
        elif mutation == 'adapter': raw_receipt['adapter']['fixture'] = 'changed'
        elif mutation == 'transport': raw_receipt['transport']['fixture'] = 'changed'
        elif mutation == 'source_hash': raw_receipt['operations'][0]['function_sha256'] = '0' * 64
        receipt = copy.deepcopy(raw_receipt)
    R.write_json(folder / 'runtime/inspector.json', raw_receipt)
    with pytest.raises(ValueError, match='inspector metadata'):
        G._inspector_metadata_feedback(state, root, folder, request, receipt)


def test_inspector_feedback_never_summarizes_task_read_call_or_explicit_getter(session):
    root = session[0]
    recipe = {'root': {'kind': 'global', 'name': 'example'}, 'steps': []}
    for operation in [{'op': 'read', 'recipe': recipe, 'return_type': 'bool'}, {'op': 'call', 'actor': {}},
        {'op': 'describe', 'recipe': {'root': recipe['root'], 'steps': [
            {'kind': 'property', 'name': 'exampleGetter', 'access': 'getter', 'getter_sha256': '0' * 64}]}, 'max_properties': 8}]:
        assert G._inspector_metadata_feedback({}, root, root / 'absent', {'operations': [operation]}, {'status': 'observed'}) is None


def inspector_preflight_receipt(monkeypatch, state, transaction):
    receipt = inspector_metadata_receipt(monkeypatch, state, transaction, [])
    receipt.update(status='incomplete', runtime_evaluation_attempts=0,
        error={'type': 'DOMRootNotUnique', 'message': 'DOM root requires exactly one match; observed 2'},
        preflight_failure={'format': 'anyuse.rea-inspector-preflight-failure/1', 'kind': 'dom_root_not_unique',
            'operation_index': 0, 'selector': transaction['operations'][0]['recipe']['root']['selector'],
            'match_count': 2, 'context_revalidated': True, 'live_node_ids_exported': False})
    return receipt


def test_known_inspector_root_preflight_is_negative_evidence_and_driver_can_choose_another_query(session, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I, rea_agent_driver as D
    root = session[0]; state = R.load_json(root / 'session.json')
    transaction = {'operations': [{'op': 'describe', 'recipe': {'root': {'kind': 'dom', 'selector': '#duplicate'},
        'steps': []}, 'max_properties': 8}]}
    raw_receipt = inspector_preflight_receipt(monkeypatch, state, transaction)
    calls = []
    def collect(bundle, out, request, timeout=30):
        calls.append(copy.deepcopy(request)); Path(out).mkdir()
        if request == transaction:
            receipt = copy.deepcopy(raw_receipt)
        else:
            receipt = inspector_metadata_receipt(monkeypatch, state, request, [{'op': 'dom_query',
                'selector': request['operations'][0]['selector'], 'total_match_count': 1, 'nodes': [],
                'getters_invoked': 0, 'app_evaluations': 0, 'live_node_ids_exported': False, 'truncated': False}])
        R.write_json(Path(out) / 'inspector.json', receipt); return receipt
    monkeypatch.setattr(I, 'collect', collect)
    result = G.step(root, {'op': 'discover', 'provider': 'inspector', 'request': transaction})
    assert result['feedback']['status'] == 'completed' and D._tool_completed(result)
    observed = result['feedback']['feedback']
    assert observed['status'] == 'negative_metadata_observation' and 'error' not in observed
    assert observed['negative_observation']['error'] == raw_receipt['error']
    assert observed['runtime_evaluation_attempts'] == observed['method_dispatch_attempts'] == observed['getter_dispatch_attempts'] == 0
    metadata = observed['observation']
    assert metadata['operations'][0] == {'op': 'describe', 'index': 0, 'pointer': '/preflight_failure',
        'selector': '#duplicate', 'match_count': 2, 'runtime_evaluation_attempts': 0}
    file = root / 's0000/runtime/inspector.json'
    assert metadata['source'] == {'artifact': 's0000/runtime/inspector.json', 'bytes': file.stat().st_size, 'sha256': R._file_hash(file)}
    assert observed['proof_created'] is False and observed['automatic_retry'] is False
    assert result['context']['status'] == 'investigating'
    changed = {'operations': [{'op': 'dom_query', 'selector': 'body > #duplicate', 'max_nodes': 8}]}
    second = G.step(root, {'op': 'discover', 'provider': 'inspector', 'request': changed})
    assert second['feedback']['status'] == 'completed' and D._tool_completed(second)
    assert calls == [transaction, changed] and second['context']['status'] == 'investigating'


@pytest.mark.parametrize('mutation', ['cleanup', 'runtime_attempt', 'getter_attempt', 'task_attempt', 'uncertain',
    'wrong_error_type', 'context_not_revalidated', 'unique_count', 'target', 'raw_bytes'])
def test_inspector_negative_admission_requires_closed_zero_evaluation_exact_raw_receipt(session, monkeypatch, mutation):
    root = session[0]; state = R.load_json(root / 'session.json')
    folder = root / 'projection'; (folder / 'runtime').mkdir(parents=True)
    transaction = {'operations': [{'op': 'describe', 'recipe': {'root': {'kind': 'dom', 'selector': '#duplicate'},
        'steps': []}, 'max_properties': 8}]}
    receipt = inspector_preflight_receipt(monkeypatch, state, transaction)
    if mutation == 'cleanup': receipt['cleanup']['object_group_released'] = False
    elif mutation == 'runtime_attempt': receipt['runtime_evaluation_attempts'] = 1
    elif mutation == 'getter_attempt': receipt['getter_dispatch_attempts'] = 1
    elif mutation == 'task_attempt': receipt['method_dispatch_attempts'] = 1
    elif mutation == 'uncertain': receipt['state_uncertain'] = True
    elif mutation == 'wrong_error_type': receipt['error']['type'] = 'ProtocolError'
    elif mutation == 'context_not_revalidated': receipt['preflight_failure']['context_revalidated'] = False
    elif mutation == 'unique_count': receipt['preflight_failure']['match_count'] = 1
    elif mutation == 'target': receipt['target']['sha256'] = '0' * 64
    file = folder / 'runtime/inspector.json'; R.write_json(file, receipt)
    if mutation == 'raw_bytes':
        changed = copy.deepcopy(receipt); changed['preflight_failure']['match_count'] = 0; R.write_json(file, changed)
        with pytest.raises(ValueError, match='actual raw receipt'):
            G._negative_inspector_metadata(state, root, folder, transaction, receipt)
    else:
        assert G._negative_inspector_metadata(state, root, folder, transaction, receipt) is None


@pytest.mark.parametrize('outer_operation', ['runtime', 'discover'])
def test_inspector_invocation_or_runtime_failures_never_gain_negative_discovery_continuation(session, monkeypatch, outer_operation):
    from anyuse.execution import rea_inspector_bridge as I, rea_agent_driver as D
    root = session[0]; state = R.load_json(root / 'session.json')
    transaction = {'operations': [{'op': 'describe', 'recipe': {'root': {'kind': 'dom', 'selector': '#duplicate'},
        'steps': []}, 'max_properties': 8}]}
    receipt = inspector_preflight_receipt(monkeypatch, state, transaction)
    if outer_operation == 'discover':
        # A real Runtime template error must not be classified by its message.
        receipt['error']['type'] = 'ProtocolError'; receipt['error']['message'] = 'DOM root requires exactly one match; observed 2'
    def collect(bundle, out, request, timeout=30):
        Path(out).mkdir(); R.write_json(Path(out) / 'inspector.json', receipt); return copy.deepcopy(receipt)
    monkeypatch.setattr(I, 'collect', collect)
    result = G.step(root, {'op': outer_operation, 'provider': 'inspector', 'request': transaction})
    assert result['feedback']['feedback']['status'] == 'incomplete' and not D._tool_completed(result)
    assert result['feedback']['feedback']['error'] == receipt['error']
    assert 'observation' not in result['feedback']['feedback']
    invocation = {'operations': [{'op': 'call', 'actor': {}}]}
    folder = root / 's0000'
    raw = copy.deepcopy(receipt); raw['request'] = invocation; raw['request_sha256'] = R.digest(invocation)
    R.write_json(folder / 'runtime/inspector.json', raw)
    assert G._negative_inspector_metadata(state, root, folder, invocation, raw) is None


def inspector_method_absence(monkeypatch, state, transaction):
    row = {'op': 'method', 'method': transaction['operations'][0]['method'], 'status': 'not_found',
        'lookup_scope': 'own_and_prototype_data_descriptors', 'prototype_depth': 3,
        'complete_prototype_chain': True, 'callable': False, 'getters_invoked': 0,
        'types_recovered_automatically': False}
    receipt = inspector_metadata_receipt(monkeypatch, state, transaction, [row])
    url = Path(state['bundle']).as_uri() + '/Contents/renderer.html'
    receipt.update(runtime_evaluation_attempts=3, descriptor_traps_may_have_effects=True,
        endpoint_identity={'target': {'id': 'owned-page', 'type': 'page', 'url': url},
            'frame': {'id': 'owned-frame', 'url': url}, 'execution_context': {
                'unique_id': 'owned-context', 'numeric_id': 1, 'frame_id': 'owned-frame',
                'frame_url': url, 'is_default': True}})
    return receipt


def test_completed_inspector_method_absence_reaches_driver_and_allows_a_new_observation(session, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I, rea_agent_driver as D
    root = session[0]; state = R.load_json(root / 'session.json')
    transaction = {'operations': [{'op': 'method', 'recipe': {'root': {'kind': 'global', 'name': 'globalThis'},
        'steps': []}, 'method': 'absentLiteralMethod'}]}
    receipt = inspector_method_absence(monkeypatch, state, transaction)
    calls = []
    def collect(bundle, out, request, timeout=30):
        calls.append(copy.deepcopy(request)); Path(out).mkdir()
        value = copy.deepcopy(receipt) if request == transaction else inspector_metadata_receipt(
            monkeypatch, state, request, [{'op': 'dom_query', 'selector': 'body', 'total_match_count': 1,
                'nodes': [], 'truncated': False, 'getters_invoked': 0, 'app_evaluations': 0,
                'live_node_ids_exported': False}])
        R.write_json(Path(out) / 'inspector.json', value); return value
    monkeypatch.setattr(I, 'collect', collect)
    result = G.step(root, {'op': 'discover', 'provider': 'inspector', 'request': transaction})
    assert result['feedback']['status'] == 'completed' and D._tool_completed(result)
    feedback = result['feedback']['feedback']
    row = feedback['observation']['operations'][0]
    assert row['status'] == 'not_found' and row['method'] == 'absentLiteralMethod'
    assert row['callable'] is False and 'function_source' not in row and 'function_sha256' not in row
    assert row['pointer'] == '/operations/0' and feedback['proof_created'] is False
    assert feedback['observation']['descriptor_traps_may_have_effects'] is True
    file = root / 's0000/runtime/inspector.json'
    assert feedback['observation']['source'] == {'artifact': 's0000/runtime/inspector.json',
        'bytes': file.stat().st_size, 'sha256': R._file_hash(file)}
    assert result['context']['status'] == 'investigating'
    changed = {'operations': [{'op': 'dom_query', 'selector': 'body', 'max_nodes': 4}]}
    assert D._tool_completed(G.step(root, {'op': 'discover', 'provider': 'inspector', 'request': changed}))
    assert calls == [transaction, changed]


@pytest.mark.parametrize('mutation', ['function_body', 'function_hash', 'callable', 'depth', 'method',
    'context', 'runtime_attempts', 'getter', 'task_call', 'cleanup', 'uncertain', 'error', 'raw_bytes'])
def test_method_absence_feedback_requires_exact_closed_noncallable_raw_observation(session, monkeypatch, mutation):
    root = session[0]; state = R.load_json(root / 'session.json')
    folder = root / 'absence-projection'; (folder / 'runtime').mkdir(parents=True)
    transaction = {'operations': [{'op': 'method', 'recipe': {'root': {'kind': 'global', 'name': 'globalThis'},
        'steps': []}, 'method': 'absentLiteralMethod'}]}
    receipt = inspector_method_absence(monkeypatch, state, transaction)
    if mutation == 'function_body': receipt['operations'][0]['function_source'] = 'function invented() {}'
    elif mutation == 'function_hash': receipt['operations'][0]['function_sha256'] = 'a' * 64
    elif mutation == 'callable': receipt['operations'][0]['callable'] = True
    elif mutation == 'depth': receipt['operations'][0]['prototype_depth'] = 17
    elif mutation == 'method': receipt['operations'][0]['method'] = 'differentMethod'
    elif mutation == 'context': receipt['endpoint_identity']['execution_context']['is_default'] = False
    elif mutation == 'runtime_attempts': receipt['runtime_evaluation_attempts'] = 0
    elif mutation == 'getter': receipt['getter_dispatch_attempts'] = 1
    elif mutation == 'task_call': receipt['method_dispatch_attempts'] = 1
    elif mutation == 'cleanup': receipt['cleanup']['object_group_released'] = False
    elif mutation == 'uncertain': receipt['state_uncertain'] = True
    elif mutation == 'error': receipt['error'] = {'type': 'ProtocolError', 'message': 'property_not_found'}
    file = folder / 'runtime/inspector.json'; R.write_json(file, receipt)
    if mutation == 'raw_bytes':
        changed = copy.deepcopy(receipt); changed['operations'][0]['method'] = 'changedRaw'; R.write_json(file, changed)
    with pytest.raises(ValueError, match='inspector metadata'):
        G._inspector_metadata_feedback(state, root, folder, transaction, receipt)


def test_negative_method_metadata_cannot_masquerade_as_a_declared_function_in_propose(session, monkeypatch):
    from anyuse.execution import rea_inspector_bridge as I
    root, profile, _ = session; state = R.load_json(root / 'session.json')
    transaction = {'operations': [{'op': 'method', 'recipe': {'root': {'kind': 'global', 'name': 'globalThis'},
        'steps': []}, 'method': 'absentLiteralMethod'}]}
    receipt = inspector_method_absence(monkeypatch, state, transaction)
    def collect(bundle, out, request, timeout=30):
        Path(out).mkdir(); R.write_json(Path(out) / 'inspector.json', receipt); return copy.deepcopy(receipt)
    monkeypatch.setattr(I, 'collect', collect)
    observed = G.step(root, {'op': 'discover', 'provider': 'inspector', 'request': transaction})
    assert observed['feedback']['status'] == 'completed'
    name = 's0000/runtime/inspector.json'; file = root / name
    actor = {'kind': 'inspector_method', 'receiver': transaction['operations'][0]['recipe'],
        'method': 'absentLiteralMethod', 'function_sha256': 'a' * 64,
        'arguments': [{'type': 'utf8', 'value': {'input': 'query'}}], 'return_type': 'utf8',
        'signature_evidence': {'kind': 'declared_js', 'file': str(file), 'sha256': R._file_hash(file),
            'pointer': '/contract'}}
    result = G.step(root, {'op': 'propose', 'actor': actor, 'profile': profile,
        'rationale': 'Attempt to treat negative metadata as a function declaration.',
        'citations': [{'artifact': name, 'pointer': '/operations/0'}]})
    assert result['feedback']['status'] == 'failed'
    assert 'invalid inspector model declaration' in result['feedback']['feedback']['error']
    assert result['context']['status'] == 'investigating' and result['context']['proof_created'] is False
    assert not (root / 's0001/hypothesis.json').exists()
