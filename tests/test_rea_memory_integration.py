"""Raw inspection stays investigation evidence across provider and agent gates.

Requests use the actual B/MI validator. Only the collector is inert: its retained
journal tests opaque artifact/citation handling, not primitive result validity,
native process access or successful reads. No GUI/native runtime is contacted.
"""
import copy
import json
from pathlib import Path
import plistlib
import sys
import tempfile

import pytest

_HERE = Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-rea-memory-integration-pkg-")
(Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
from anyuse.execution import rea_agent as G
from anyuse.execution import rea_harness as H
from anyuse.execution import rea_inspector_bridge as I
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_runtime_providers as P
from anyuse.execution import rea_state_observer as S


MEMORY_OPS = {"memory_ranges", "memory_range", "memory_bytes", "memory_pointer_range"}
RAW_POINTER = "/result/memory_inspection/attempts/0/fixture_observation/returned_hex"


def memory_request():
    return {"operations": [
        {"op": "memory_ranges", "scope": {"kind": "all"}, "limit": 4, "as": "maps"},
        {"op": "memory_bytes", "from": {"ranges": "maps", "index": 0}, "offset": 0, "bytes": 16},
    ]}


def module_request(module):
    return {"operations": [
        {"op": "memory_range", "module": copy.deepcopy(module), "rva": "0x40", "bytes": 64, "as": "root"},
        {"op": "memory_pointer_range", "from": {"span": "root"}, "offset": 0, "pointee_bytes": 32, "as": "child"},
        {"op": "memory_bytes", "from": {"span": "child"}, "offset": 0, "bytes": 16},
    ]}


def ordinary_objc_actor():
    return {"kind": "objc_method", "receiver": {"class": "MappedReader", "kind": "unique", "subclasses": False},
            "selector": "refresh", "signature": "v16@0:8", "arguments": [], "return_type": "void"}


@pytest.fixture
def memory_session(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = (tmp_path / "InspectionTarget.app").resolve()
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "org.test.RawInspection", "CFBundleExecutable": "Target", "CFBundleVersion": "1"}))
    executable = app / "Contents" / "MacOS" / "Target"
    executable.write_bytes(b"inert fixture, never launched or loaded")
    rea = (tmp_path / "rea-inert").resolve()
    rea.write_text("inert fixture, never executed")
    rea.chmod(0o700)
    params = [{"name": "query", "type": "string", "source": "lookup"},
              {"name": "reference", "type": "path", "source": "path"}]
    profile = {"format": H.PROFILE, "id": "raw_evidence_control", "label": "Read complete result",
               "effect": "scoped_find", "item_kind": "", "params": params,
               "reference": {"fields": {
                   "query": {"kind": "input", "param": "query"},
                   "body": {"kind": "file_text", "param": "reference", "normalization": "compact"}}}}
    inputs = []
    for index in range(9):
        reference = (tmp_path / f"reference-{index}.txt").resolve()
        reference.write_text(f"Independent full result {index}.")
        inputs.append({"query": f"query-{index}", "reference": str(reference)})
    plan = {role: [{"inputs": value} for value in inputs[index * 3:(index + 1) * 3]]
            for index, role in enumerate(G.ROLES)}
    root = (tmp_path / "mapping-session").resolve()
    G.begin(str(app), str(rea), "Read a complete independent result", params,
            ["Full independent content and query must match"], plan, str(root))
    return {"root": root, "bundle": app, "profile": profile, "plan": plan,
            "module": {"path": str(executable), "sha256": R._file_hash(executable)}}


@pytest.fixture
def memory_collector(monkeypatch):
    """Opaque retained evidence, deliberately not an MI.validate_result fixture."""
    state = {"calls": [], "partial": False, "review": False, "deny": False, "forged_proof": False}

    def collect(bundle, out, request, timeout=30):
        state["calls"].append({"bundle": bundle, "request": copy.deepcopy(request), "timeout": timeout})
        if state["deny"]:
            raise PermissionError("native inspection denied; no alternate transport")
        folder = Path(out)
        folder.mkdir(parents=True)
        # This synthetic observation is intentionally opaque to the agent. The
        # real worker/MI suites own exact journal validation and read accounting.
        raw = {"format": B.WORKER, "request_sha256": R.digest(request),
               "result": {"memory_inspection": {"raw_observation_only": True,
                   "fixture_only": True, "attempts": [{"fixture_observation": {
                       "returned_hex": "00112233", "partial": state["partial"],
                       "error": "unmapped after prefix" if state["partial"] else None}}]}}}
        R.write_json(folder / "worker-output.json", raw)
        receipt = {"format": B.RUNTIME, "status": "incomplete" if state["partial"] else "observed",
                   "request": copy.deepcopy(request), "request_sha256": R.digest(request),
                   "actuated": False, "verified": False, "proof_created": False,
                   "evidence_only": True, "resource_review_required": state["review"],
                   "result": copy.deepcopy(raw["result"])}
        if state["forged_proof"]:
            receipt.update(status="verified", verified=True, proof_created=True, task_result_verified=True)
        R.write_json(folder / "runtime.json", receipt)
        return receipt

    monkeypatch.setattr(B, "collect", collect)
    return state


def inspect(session, operation="discover", request=None, provider="native"):
    return G.step(session["root"], {"op": operation, "provider": provider,
        "request": memory_request() if request is None else request, "timeout": 15})


def raw_artifact(result):
    return next(name for name in result["context"]["artifacts"] if name.endswith("runtime/worker-output.json"))


def assert_unproved(context):
    assert context["proof_created"] is False and context["published"] is None
    assert not context["recorded_cases"]


def test_raw_capability_declaration_is_lazy_native_only_and_not_an_actor(monkeypatch):
    def unexpected_import(*args, **kwargs):
        pytest.fail("declared capability must not import a runtime")
    monkeypatch.setattr(P.importlib, "import_module", unexpected_import)
    capabilities = {value["provider"]: value for value in P.capabilities()}
    assert MEMORY_OPS <= set(capabilities["native"]["discovery_operations"])
    assert not MEMORY_OPS.intersection(capabilities["inspector"]["discovery_operations"])
    assert set(capabilities["native"]["actor_kinds"]) == {
        "objc_method", "objc_sequence", "native_function", "runtime_sequence", "qt_moc_method"}
    assert not MEMORY_OPS.intersection(P.actor_kinds())
    assert all(value["access_observed"] is False and value["automatic_fallback"] is False
               for value in capabilities.values())


@pytest.mark.parametrize("entry", ["collect", "transaction"])
@pytest.mark.parametrize("family", ["ranges", "module"])
def test_native_provider_accepts_real_validated_raw_requests_once(memory_session, memory_collector, tmp_path, entry, family):
    request = memory_request() if family == "ranges" else module_request(memory_session["module"])
    frozen = copy.deepcopy(request)
    assert B.validate_request(request) == frozen
    provider = P.for_name("native")
    result = getattr(provider, entry)(str(memory_session["bundle"]), str(tmp_path / "out"), request, timeout=12)
    assert result["actuated"] is False and result["proof_created"] is False
    assert request == frozen and len(memory_collector["calls"]) == 1
    assert memory_collector["calls"][0] == {
        "bundle": str(memory_session["bundle"]), "request": frozen, "timeout": 12}
    assert provider.validate_discovery(request) is not request


@pytest.mark.parametrize("operation", ["runtime", "discover"])
def test_agent_retains_citable_raw_evidence_without_a_task_or_publication(memory_session, memory_collector, operation):
    result = inspect(memory_session, operation)
    assert result["feedback"]["status"] == "completed"
    assert result["feedback"]["feedback"]["evidence_only"] is True
    assert result["context"]["status"] == "investigating"
    assert_unproved(result["context"])
    artifact = raw_artifact(result)
    read = G.step(memory_session["root"], {"op": "evidence", "artifact": artifact})
    assert json.loads(read["feedback"]["feedback"]["text"])["result"]["memory_inspection"]["attempts"][0][
        "fixture_observation"]["returned_hex"] == "00112233"
    state = R.load_json(memory_session["root"] / "session.json")
    citations = G._citations(state, [{"artifact": artifact, "pointer": RAW_POINTER}])
    assert citations == [{"file": state["artifacts"][artifact]["file"],
                          "sha256": state["artifacts"][artifact]["sha256"], "pointer": RAW_POINTER}]
    with pytest.raises(ValueError, match="does not exist"):
        G._citations(state, [{"artifact": artifact, "pointer": "/result/missing"}])
    publication = G.step(memory_session["root"], {"op": "publish", "summary": "Raw bytes", "details": "Observed data"})
    assert publication["feedback"]["status"] == "failed"
    assert "fresh verification and heldout" in publication["feedback"]["feedback"]["error"]
    assert_unproved(publication["context"])
    assert len(memory_collector["calls"]) == 1
    assert not list(memory_session["root"].glob("s*/catalog"))


@pytest.mark.parametrize("kind", sorted(MEMORY_OPS))
def test_raw_actor_hypothesis_stays_unsupported_and_never_records(memory_session, memory_collector, kind):
    observed = inspect(memory_session)
    proposed = G.step(memory_session["root"], {"op": "propose", "rationale": "Raw observations motivate investigation.",
        "citations": [{"artifact": raw_artifact(observed), "pointer": RAW_POINTER}],
        "actor": {"kind": kind, "from": {"span": "child"}}})
    assert proposed["context"]["status"] == "needs_capability"
    assert proposed["feedback"]["feedback"]["actuated"] is False
    assert_unproved(proposed["context"])
    recorded = G.step(memory_session["root"], {"op": "record", "case": 0})
    assert recorded["feedback"]["status"] == "failed"
    assert "supported hypothesis" in recorded["feedback"]["feedback"]["error"]
    assert len(memory_collector["calls"]) == 1
    assert not list(memory_session["root"].glob("s*/record"))


def test_raw_citation_can_inform_an_existing_typed_hypothesis_but_is_not_proof(memory_session, memory_collector):
    observed = inspect(memory_session)
    actor = {"kind": "apple_event", "class": "TEST", "id": "READ", "parameters": {
        "term": {"type": "utf8", "value": {"input": "query"}},
        "path": {"type": "utf8", "value": {"input": "reference"}}}}
    proposed = G.step(memory_session["root"], {"op": "propose", "profile": memory_session["profile"],
        "rationale": "Observed bytes are evidence; this is a separate typed event hypothesis requiring trials.",
        "citations": [{"artifact": raw_artifact(observed), "pointer": RAW_POINTER}], "actor": actor})
    assert proposed["feedback"]["status"] == "completed" and proposed["context"]["status"] == "hypothesis"
    assert proposed["context"]["hypothesis"]["actor"] == actor
    assert_unproved(proposed["context"])
    assert len(memory_collector["calls"]) == 1


@pytest.mark.parametrize("kind", sorted(MEMORY_OPS))
def test_memory_operation_cannot_be_used_as_a_shared_action(kind):
    actor = {"kind": kind, "from": {"span": "root"}, "offset": 0, "bytes": 8}
    with pytest.raises(ValueError):
        P.for_actor(actor)
    with pytest.raises(ValueError):
        B.invocation_request(actor)
    with pytest.raises(ValueError):
        H._runtime_actor(actor, [{"name": "query", "type": "string", "source": "lookup"}])
    with pytest.raises(ValueError):
        B.validate_actor({"kind": "runtime_sequence", "steps": [ordinary_objc_actor(), actor]})
    assert B.validate_actor(ordinary_objc_actor()) == ordinary_objc_actor()


@pytest.mark.parametrize("raw", [{"kind": "span", "alias": "root"}, {"alias": "root"}])
def test_raw_alias_cannot_supply_a_typed_objc_receiver(raw):
    actor = ordinary_objc_actor()
    actor["receiver"] = copy.deepcopy(raw)
    with pytest.raises(ValueError):
        B.validate_actor(actor)


@pytest.mark.parametrize("raw", [{"span": "root"}, {"alias": "root"}])
def test_raw_alias_cannot_supply_a_typed_objc_argument(raw):
    actor = ordinary_objc_actor()
    actor.update(selector="use:", signature="v24@0:8@16", arguments=[{"type": "object", "value": copy.deepcopy(raw)}])
    with pytest.raises(ValueError):
        B.validate_actor(actor)


@pytest.mark.parametrize("operation", ["runtime", "discover"])
@pytest.mark.parametrize("legacy", ["classes", "modules", "symbols", "invoke"])
def test_agent_rejects_mixed_raw_batches_before_collection(memory_session, memory_collector, operation, legacy):
    ordinary = (B.invocation_request(ordinary_objc_actor())["operations"][0] if legacy == "invoke"
                else {"op": legacy, "limit": 2})
    if legacy == "symbols":
        ordinary["module"] = copy.deepcopy(memory_session["module"])
    assert B.validate_request({"operations": [ordinary]}) == {"operations": [ordinary]}
    request = memory_request()
    request["operations"].append(ordinary)
    result = inspect(memory_session, operation, request)
    assert result["feedback"]["status"] == "failed" and not memory_collector["calls"]
    assert_unproved(result["context"])


@pytest.mark.parametrize("operation", ["runtime", "discover"])
@pytest.mark.parametrize("source", ["stale", "forward", "duplicate", "wrong_type", "address"])
def test_invalid_raw_aliases_and_addresses_stop_before_collection(memory_session, memory_collector, operation, source):
    request = memory_request()
    prior_calls = 0
    if source == "stale":
        first = inspect(memory_session, operation)
        assert first["feedback"]["status"] == "completed"
        prior_calls = 1
        request["operations"] = [request["operations"][1]]
    elif source == "forward":
        request["operations"].reverse()
    elif source == "duplicate":
        request["operations"].insert(1, copy.deepcopy(request["operations"][0]))
    elif source == "wrong_type":
        request["operations"][1]["from"] = {"span": "maps"}
    else:
        request["operations"][1]["from"] = {"address": "0x1000"}
    result = inspect(memory_session, operation, request)
    assert result["feedback"]["status"] == "failed" and len(memory_collector["calls"]) == prior_calls
    assert_unproved(result["context"])


@pytest.mark.parametrize("operation", ["runtime", "discover"])
def test_inspector_selection_never_falls_back_to_native_memory(memory_session, memory_collector, monkeypatch, operation):
    monkeypatch.setattr(I, "collect", lambda *a, **k: pytest.fail("unsupported raw request reached inspector collector"))
    result = inspect(memory_session, operation, provider="inspector")
    assert result["feedback"]["status"] == "failed" and not memory_collector["calls"]
    assert_unproved(result["context"])


@pytest.mark.parametrize("operation", ["runtime", "discover"])
def test_native_inspection_denial_is_not_retried_or_switched(memory_session, memory_collector, monkeypatch, operation):
    memory_collector["deny"] = True
    monkeypatch.setattr(I, "collect", lambda *a, **k: pytest.fail("native denial opened inspector transport"))
    result = inspect(memory_session, operation)
    assert result["feedback"]["status"] == "failed" and len(memory_collector["calls"]) == 1
    assert result["feedback"]["feedback"]["retry"] is False
    assert "native inspection denied" in result["feedback"]["feedback"]["error"]
    assert_unproved(result["context"])


def test_partial_raw_journal_is_retained_without_success_promotion(memory_session, memory_collector):
    memory_collector["partial"] = True
    result = inspect(memory_session)
    assert result["feedback"]["feedback"]["status"] == "incomplete"
    assert result["context"]["status"] == "investigating"
    assert_unproved(result["context"])
    artifact = raw_artifact(result)
    evidence = G.step(memory_session["root"], {"op": "evidence", "artifact": artifact})
    raw = json.loads(evidence["feedback"]["feedback"]["text"])
    observation = raw["result"]["memory_inspection"]["attempts"][0]["fixture_observation"]
    assert observation == {"returned_hex": "00112233", "partial": True, "error": "unmapped after prefix"}
    assert len(memory_collector["calls"]) == 1


def test_raw_provider_proof_labels_cannot_advance_the_mapping_gate(memory_session, memory_collector):
    memory_collector["forged_proof"] = True
    result = inspect(memory_session)
    # Agent control test: intentionally malformed opaque collector output is
    # not authority even if it contains proof-like labels. MI/B result tests own
    # primitive/envelope rejection; this test covers only the agent's state gate.
    assert result["feedback"]["feedback"]["proof_created"] is False
    assert result["feedback"]["feedback"]["evidence_only"] is True
    assert result["context"]["status"] == "investigating"
    assert_unproved(result["context"])
    publication = G.step(memory_session["root"], {"op": "publish", "summary": "Forged", "details": "Invalid proof labels"})
    assert publication["feedback"]["status"] == "failed"
    assert_unproved(publication["context"])
    assert len(memory_collector["calls"]) == 1


def test_uncertain_cleanup_retains_raw_evidence_and_closes_followup(memory_session, memory_collector):
    memory_collector["review"] = True
    result = inspect(memory_session)
    assert result["feedback"]["status"] == "failed"
    assert result["context"]["status"] == "resource_review_required"
    assert_unproved(result["context"])
    artifact = raw_artifact(result)
    assert G.step(memory_session["root"], {"op": "evidence", "artifact": artifact})["feedback"]["status"] == "completed"
    with pytest.raises(R.ResourceReviewRequired):
        inspect(memory_session)
    assert len(memory_collector["calls"]) == 1


def test_changed_raw_evidence_is_refused(memory_session, memory_collector):
    result = inspect(memory_session)
    artifact = raw_artifact(result)
    Path(result["context"]["artifacts"][artifact]["file"]).write_text("{}")
    changed = G.step(memory_session["root"], {"op": "evidence", "artifact": artifact})
    assert changed["feedback"]["status"] == "failed"
    assert "evidence changed" in changed["feedback"]["feedback"]["error"]
    assert_unproved(changed["context"])


@pytest.mark.parametrize("operation", ["runtime", "discover"])
def test_evaluation_gate_stops_raw_inspection_before_provider_selection(memory_session, memory_collector, monkeypatch, operation):
    path = memory_session["root"] / "session.json"
    state = R.load_json(path)
    state["status"] = "candidate"
    R.write_json(path, state)
    monkeypatch.setattr(P, "for_name", lambda *a: pytest.fail("evaluation reopened native investigation"))
    result = inspect(memory_session, operation)
    assert result["feedback"]["status"] == "failed" and not memory_collector["calls"]
    assert "closed once evaluation starts" in result["feedback"]["feedback"]["error"]
    assert_unproved(result["context"])


def test_raw_inspection_is_not_an_independent_task_state_observer(memory_session):
    schema = {"format": S.SCHEMA, "kind": "runtime_state", "target": R.fingerprint(memory_session["bundle"]),
        "params": [{"name": "expected", "type": "path", "role": "independent_reference"},
                   {"name": "expected_sha", "type": "sha256", "role": "reference_hash"}],
        "fields": {"payload": {"type": "utf8", "source": {"operation": 0, "field": "payload"}}},
        "reference": {"format": "utf8", "file": {"input": "expected"}, "sha256": {"input": "expected_sha"}},
        "contract": {"file": str(memory_session["root"] / "independent-contract.json"), "sha256": "a" * 64, "pointer": "/observer"},
        "request": {"operations": [{"op": "read", "receiver": ordinary_objc_actor()["receiver"], "fields": [
            {"name": "payload", "kind": "getter", "selector": "payload", "type": "utf8", "signature": "@16@0:8"}]}]}}
    assert S.validate(schema) == schema  # Existing declared scalar route remains valid.
    schema["request"] = memory_request()
    with pytest.raises(S.Unsupported, match="explicit reads"):
        S.validate(schema)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
