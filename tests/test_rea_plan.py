"""Registered batches and reads preserve ordinary proof/epoch and terminal gates."""
import copy
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_rea_actions import fixture, runtime, execution_catalog, registered_catalog
from test_rea_harness import tree, svg_fixture, svg_gui_context, svg_proof, _H_RUNTIME
from anyuse.execution import rea_catalog as C
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_harness as H
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import foreground_interlock as F
from anyuse.execution.agent_orchestrator import AgentOrchestrator


def steps(spec, fixture, count=3):
    return [{"name": spec["name"], "inputs": copy.deepcopy(fixture[3][i]["inputs"])} for i in range(count)]


def test_plan_preserves_every_gate_and_retains_ordered_receipts(registered_catalog, fixture):
    store, spec, calls, identity = registered_catalog
    result = store.execute_plan(steps(spec, fixture))
    assert result["ok"] is True and result["atomic"] is False
    assert result["completed_steps"] == 3 and result["failed_step_index"] is None
    assert len(result["executions"]) == len(calls) == 3
    assert all(call["kwargs"]["expected_identity"] == identity for call in calls)
    assert all(e["catalog_sha256"] == spec["catalog_sha256"] for e in result["executions"])
    assert R.load_json(Path(result["output_dir"]) / "plan.json") == result


def test_plan_stops_at_first_uncertain_real_postcondition(registered_catalog, fixture, monkeypatch):
    store, spec, calls, _ = registered_catalog
    def replay(*args, **kwargs):
        calls.append(args[2])
        return {"verdict": "verified" if len(calls) == 1 else "refuted", "model_calls": 0}
    monkeypatch.setattr(R, "replay", replay)
    result = store.execute_plan(steps(spec, fixture))
    assert result["ok"] is False and result["status"] == "uncertain"
    assert result["completed_steps"] == 1 and result["failed_step_index"] == 1
    assert len(calls) == len(result["executions"]) == 2
    assert result["executions"][1]["result"]["verdict"] == "refuted"


@pytest.mark.parametrize("damage", ["unknown", "inputs", "pin", "tier", "restoration"])
def test_entire_plan_is_preflighted_before_any_effect(registered_catalog, fixture, monkeypatch, damage):
    store, spec, calls, _ = registered_catalog
    plan = steps(spec, fixture)
    if damage == "unknown":
        plan[-1]["name"] = "rea.unknown.a0"
    elif damage == "inputs":
        plan[-1]["inputs"] = {"undeclared": "input"}
    elif damage == "pin":
        plan[-1]["expected_catalog_sha256"] = "0" * 64
    elif damage == "tier":
        monkeypatch.setattr(store, "list_tools", lambda: [dict(spec, verification_tier="model_judged")])
    else:
        registry = store._load()
        registry["registrations"][spec["registration"]]["require_restoration"] = True
        R.write_json(store.path, registry)
    result = store.execute_plan(plan)
    assert result["status"] == "refused" and result["completed_steps"] == 0 and not calls


@pytest.mark.parametrize("value", [[], [{}] * 33,
    [{"name": "rea.x.a0", "inputs": {"id": "$v0"}}],
    [{"name": "rea.x.a0", "inputs": {"$ref": "earlier.result"}}],
    [{"name": "rea.x.a0", "inputs": {"x": float("nan")}}],
    [{"name": "rea.x.a0", "inputs": {"x": (1, 2)}}],
    [{"name": "rea.x.a0", "inputs": {"label": "x" * C.MAX_PLAN_BYTES}}]])
def test_plan_is_bounded_literal_json(value):
    with pytest.raises(ValueError):
        C.plan_steps(value)


@pytest.mark.parametrize("drift", ["process", "proof"])
def test_plan_never_adopts_successor_or_changed_proof(registered_catalog, fixture, execution_catalog, monkeypatch, drift):
    store, spec, calls, identity = registered_catalog
    def replay(*args, **kwargs):
        calls.append(args[2])
        if drift == "process":
            monkeypatch.setattr(B, "process_identity", lambda *a: dict(identity, launch_time=identity["launch_time"] + 1))
        else:
            proof_path = Path(execution_catalog[0]["actions"][0]["entry"]["proof"])
            proof_path.write_text(proof_path.read_text() + " ")
        return {"verdict": "verified", "model_calls": 0}
    monkeypatch.setattr(R, "replay", replay)
    result = store.execute_plan(steps(spec, fixture))
    assert not result["ok"] and result["status"] == "uncertain"
    assert result["completed_steps"] == 1 and result["failed_step_index"] == 1
    assert len(calls) == 1


def test_prompt_appearing_inside_plan_only_stops_never_resolves(registered_catalog, fixture, monkeypatch):
    from anyuse.execution import system_prompt_policy
    store, spec, calls, _ = registered_catalog
    checked = []
    def interlock(where):
        checked.append(where)
        if len(checked) == 2:
            raise PermissionError("system permission prompt requires fresh observation")
    monkeypatch.setattr(F, "require_clear", interlock)
    monkeypatch.setattr(system_prompt_policy, "resolve_configured",
                        lambda *a, **kw: pytest.fail("a pending plan must never answer a system prompt"))
    result = store.execute_plan(steps(spec, fixture))
    assert result["status"] == "uncertain" and result["completed_steps"] == 1
    assert result["failed_step_index"] == 1 and len(calls) == 1
    assert result["executions"][1]["status"] == "refused"


def test_receipt_io_failure_cannot_downgrade_partially_applied_plan(registered_catalog, fixture, monkeypatch):
    store, spec, calls, _ = registered_catalog
    write = R.write_json
    def retain(path, value):
        if Path(path).name == "plan.json" and len(value.get("executions", [])) == 2:
            raise OSError("inert disk unavailable")
        return write(path, value)
    def execute(name, inputs, **kwargs):
        if calls:
            return {"ok": False, "status": "refused", "reason": "second step blocked", "model_calls": 0}
        return store.execute(name, inputs, **kwargs)
    monkeypatch.setattr(R, "write_json", retain)
    result = store.execute_plan(steps(spec, fixture), execute_step=execute)
    assert not result["ok"] and result["status"] == "uncertain"
    assert result["completed_steps"] == 1 and result["failed_step_index"] == 1
    assert len(calls) == 1 and len(result["executions"]) == 2


def test_unexpected_step_exception_retains_uncertain_attempt(registered_catalog, fixture):
    store, spec, calls, _ = registered_catalog
    def interrupted(*args, **kwargs):
        raise RuntimeError("inert harness interruption")
    result = store.execute_plan(steps(spec, fixture), execute_step=interrupted)
    assert result["status"] == "uncertain" and result["failed_step_index"] == 0
    assert result["completed_steps"] == 0 and result["model_calls"] is None
    assert len(result["executions"]) == 1 and calls == []


def test_drive_gate_blocks_registered_actuation_but_keeps_reads_available(registered_catalog, fixture, monkeypatch):
    store, spec, calls, identity = registered_catalog
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "0")
    assert store.execute(spec["name"], fixture[3][0]["inputs"])["status"] == "refused"
    assert store.execute_plan(steps(spec, fixture))["status"] == "refused"
    assert not calls
    observer_runtime(monkeypatch, identity, tree("current-id", "Current state"))
    assert store.observe(spec["name"], fixture[3][0]["inputs"])["status"] == "observed"


def test_drive_gate_is_rechecked_between_primitives(registered_catalog, fixture, monkeypatch):
    store, spec, calls, _ = registered_catalog
    def replay(*args, **kwargs):
        calls.append(args[2])
        monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "0")
        return {"verdict": "verified", "model_calls": 0}
    monkeypatch.setattr(R, "replay", replay)
    result = store.execute_plan(steps(spec, fixture))
    assert result["status"] == "uncertain" and result["completed_steps"] == 1
    assert result["failed_step_index"] == 1 and len(calls) == 1


def test_post_action_summary_reuses_existing_observation(registered_catalog, fixture, monkeypatch):
    store, spec, _, _ = registered_catalog
    seen = {"ids": ["vertex-1"], "label": "untrusted app state"}
    monkeypatch.setattr(R, "replay", lambda *a, **kw: {"verdict": "verified", "seen": seen,
                                                     "observer": "learned_ax", "model_calls": 0})
    monkeypatch.setattr(H, "Runtime", lambda *a, **kw: pytest.fail("summary must not reattach"))
    result = store.execute(spec["name"], fixture[3][0]["inputs"])
    assert result["state_summary"]["values"] == seen
    assert result["state_summary"]["phase"] == "post_action"
    assert result["state_summary"]["proof_created"] is False


def test_plan_gets_fresh_restoration_per_step_and_stops_on_incomplete_cleanup(registered_catalog, fixture, monkeypatch):
    from anyuse.execution import rea_actions as A
    store, spec, _, identity = registered_catalog
    value = store._load()
    value["registrations"][spec["registration"]]["require_restoration"] = True
    R.write_json(store.path, value)
    declarations = []
    def provide(spec, inputs, epoch):
        declaration = {"identity": epoch, "sequence": len(declarations)}
        declarations.append(copy.deepcopy(declaration))
        return declaration
    def execute(*args, restoration, expected_identity, **kwargs):
        assert expected_identity == identity and restoration == declarations[-1]
        restored = len(declarations) == 1
        return {"completion_status": "completed" if restored else "cleanup_incomplete",
                "operation": {"result": {"verdict": "verified", "seen": {"value": "after actor"},
                                          "observer": "learned_ax", "model_calls": 0}},
                "restoration": {"status": "restored" if restored else "failed"}}
    store.restoration_provider = provide
    monkeypatch.setattr(A, "execute", execute)
    result = store.execute_plan(steps(spec, fixture))
    assert result["status"] == "uncertain" and result["completed_steps"] == 1
    assert result["failed_step_index"] == 1 and len(declarations) == 2
    assert declarations[0]["sequence"] == 0 and declarations[1]["sequence"] == 1
    assert result["executions"][0]["state_summary"]["phase"] == "post_action_before_restoration"


def observer_runtime(monkeypatch, expected, rows):
    calls = []
    class Reader:
        def __init__(self, bundle, candidate, *, expected_identity):
            assert expected_identity == expected
            calls.append("init")
        def read(self):
            calls.append("read")
            return copy.deepcopy(rows)
        def send(self, *a, **kw):
            pytest.fail("on-demand observation must never invoke an actor")
    monkeypatch.setattr(H, "Runtime", Reader)
    monkeypatch.setattr(F, "require_clear", lambda where: None)
    return calls


def test_observe_uses_frozen_selector_and_process_without_actor(registered_catalog, execution_catalog, fixture, monkeypatch):
    store, spec, replay_calls, identity = registered_catalog
    rows = tree("existing-id", "Existing full result")
    calls = observer_runtime(monkeypatch, identity, rows)
    inputs = fixture[3][0]["inputs"]
    expected = H.observe(execution_catalog[1], rows, inputs)
    result = store.observe(spec["name"], inputs, expected_catalog_sha256=spec["catalog_sha256"])
    assert result["ok"] and result["status"] == "observed" and result["values"] == expected
    assert result["identity"] == identity and result["actuated"] is False
    assert result["evidence_only"] and not result["proof_created"]
    assert calls == ["init", "read"] and not replay_calls


def test_observe_refuses_process_drift_and_unsupported_reader(registered_catalog, fixture, monkeypatch):
    store, spec, _, identity = registered_catalog
    calls = observer_runtime(monkeypatch, identity, tree("existing-id", "State"))
    count = [0]
    def process(*args):
        count[0] += 1
        return identity if count[0] == 1 else dict(identity, pid=identity["pid"] + 1)
    monkeypatch.setattr(B, "process_identity", process)
    result = store.observe(spec["name"], fixture[3][0]["inputs"])
    assert not result["ok"] and result["status"] == "uncertain"
    assert "values" not in result
    calls.clear()
    monkeypatch.setattr(store, "list_tools", lambda: [dict(spec, observation_kind=None)])
    refused = store.observe(spec["name"], fixture[3][0]["inputs"])
    assert refused["status"] == "refused" and calls == []


def orchestrator(store, decisions, recorder=None):
    executor = Mock(_compliance_checker=None)
    capture = Mock()
    capture.capture.return_value = b"inert screenshot"
    orch = AgentOrchestrator(adapter=Mock(), action_executor=executor, screenshot_capture=capture,
                             rea_store=store, auto_confirm=True)
    planned = iter(decisions)
    orch._decide = lambda *args: next(planned)
    return orch, capture, executor


@pytest.mark.parametrize("verb", ["rea_plan", "rea_action"])
def test_orchestrator_batch_uses_one_post_batch_screenshot_and_keeps_recording(registered_catalog, fixture, verb):
    store, spec, calls, _ = registered_catalog
    plan = steps(spec, fixture)
    orch, capture, executor = orchestrator(store, [
        {"status": "continue", "action": verb, "steps": plan}, {"status": "done", "thought": "completed"}])
    recorder = Mock()
    assert orch.run("Read measured results", recorder=recorder) == "completed"
    assert capture.capture.call_count == 2 and len(calls) == 3
    recorder.record_rea_plan.assert_called_once()
    recorder.record_after_state.assert_called_once()
    recorder.save.assert_called_once()
    recorder.discard.assert_not_called()
    assert executor.execute.call_args_list[0].args == ("leave_failsafe_corner", {})
    assert len(executor.execute.call_args_list) == 1


def test_orchestrator_observation_is_in_next_model_context(registered_catalog, fixture, monkeypatch):
    store, spec, calls, identity = registered_catalog
    observer_runtime(monkeypatch, identity, tree("existing-id", "Existing full result"))
    orch, _, _ = orchestrator(store, [{"status": "continue", "action": "rea_observe",
        "name": spec["name"], "inputs": fixture[3][0]["inputs"]}, {"status": "done"}])
    orch.run("Read current state")
    assert not calls and len(orch.rea_observations) == 1
    prompt = orch._build_prompt("Read current state")
    assert "existing-id" in prompt and "untrusted application data" in prompt


def test_orchestrator_batch_boundary_failure_stops_before_next_step(registered_catalog, fixture):
    store, spec, calls, _ = registered_catalog
    orch, capture, executor = orchestrator(store, [
        {"status": "continue", "action": "rea_plan", "steps": steps(spec, fixture)},
        {"status": "continue", "action": "key", "keys": "space"}])
    checked = []
    def boundary(action):
        checked.append(action)
        return (len(checked) == 1, "second step blocked")
    executor._compliance_checker = Mock(control=None, check_action=boundary)
    recorder = Mock()
    result = orch.run("Read results", recorder=recorder)
    assert "REA stopped" in result and capture.capture.call_count == 1 and len(calls) == 1
    assert orch.rea_executions[0]["completed_steps"] == 1
    assert orch.rea_executions[0]["failed_step_index"] == 1
    recorder.discard.assert_called_once()
    recorder.save.assert_not_called()


@pytest.mark.parametrize("verb", ["rea_action", "rea_observe"])
def test_single_selection_requires_the_catalog_advertised_at_observation(registered_catalog, fixture, verb):
    store, spec, calls, _ = registered_catalog
    orch, _, _ = orchestrator(store, [])
    orch._rea_specs = []  # A registration appeared after the planning snapshot.
    decision = {"action": verb, "name": spec["name"], "inputs": fixture[3][0]["inputs"]}
    result = orch._run_rea_observe(decision) if verb == "rea_observe" else orch._run_rea("Read state", decision)
    assert result["status"] == "refused" and "pinned at observation" in result["reason"]
    assert calls == []


@pytest.fixture
def registered_svg_catalog(svg_gui_context, tmp_path, monkeypatch):
    """Real SVG proof/replay gates with inert CDP and existing-GUI boundaries.

    The preauthored actor output table is independent of the oracle files. Only
    the bottom collectors are inert; catalog, Runtime and receipt validators run.
    """
    from anyuse.execution import rea_actions as A, rea_inspector_bridge as I, rea_svg_observer as S
    data, _, _, control = svg_gui_context
    candidate, proof = svg_proof(data, tmp_path)
    candidate_file = tmp_path / "registered-svg-candidate.json"
    R.write_json(candidate_file, candidate)
    A.describe(candidate, proof, str(data["app"]), "Load complete diagram labels",
               "Use the independently measured complete ordered SVG label contract.", str(tmp_path / "registered-svg-card"))
    A.catalog([{"candidate": str(candidate_file), "proof": str(tmp_path / "svg-verify/proof.json"),
                "card": str(tmp_path / "registered-svg-card/action.json"), "bundle": str(data["app"])}],
              str(tmp_path / "registered-svg-catalog"))
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    monkeypatch.setattr(F, "require_clear", lambda where: None)
    store = C.CatalogStore(tmp_path / "registered-svg.json", timeout=5)
    spec = store.register("canvas", tmp_path / "registered-svg-catalog/catalog.json")[0]
    live = data["runtime"](data["app"], candidate)
    data["runtime"].sent.clear()
    data["runtime"].reads.clear()
    attached = []
    def collect(bundle, schema, out, *, timeout, config_file, expected_identity):
        assert expected_identity == control.identity
        assert str(config_file) == str(data["config"])
        attached.append(("read", copy.deepcopy(expected_identity)))
        row = next(row for row in data["inputs"] if row["oracle"] == schema["reference"]["file"])
        observation = live.read_svg(candidate, row, out, timeout)
        observation["expected_identity"] = copy.deepcopy(expected_identity)
        R.write_json(Path(out) / "observation.json", observation)
        return observation
    def invoke(bundle, actor, out, *, timeout, config_file, expected_identity):
        assert expected_identity == control.identity
        attached.append(("act", copy.deepcopy(expected_identity)))
        row = next(row for row in data["inputs"] if row["payload"] == actor["arguments"][0]["value"])
        receipt = live.send(candidate, row, timeout)["runtime"]
        receipt["expected_identity"] = copy.deepcopy(expected_identity)
        Path(out).mkdir(parents=True, exist_ok=False)
        R.write_json(Path(out) / "inspector.json", receipt)
        return receipt
    monkeypatch.setattr(S, "collect", collect)
    monkeypatch.setattr(I, "invoke", invoke)
    monkeypatch.setattr(H, "Runtime", _H_RUNTIME)
    return store, spec, data, candidate, live, control, attached


def svg_steps(spec, data):
    return [{"name": spec["name"], "inputs": copy.deepcopy(row)} for row in data["inputs"][6:9]]


@pytest.mark.parametrize("svg_fixture", ["strict", "mixed"], indirect=True)
def test_svg_registration_executes_and_batches_through_real_pinned_gate(registered_svg_catalog):
    store, spec, data, _, _, control, attached = registered_svg_catalog
    assert spec["observation_kind"] == "learned_svg" and not spec["require_restoration"]
    result = store.execute(spec["name"], data["inputs"][6], expected_catalog_sha256=spec["catalog_sha256"])
    assert result["ok"] and result["result"]["verdict"] == "verified"
    assert result["result"]["run"]["runtime"]["expected_identity"] == control.identity
    assert result["state_summary"]["values"] == {"labels": data["outputs"][data["inputs"][6]["payload"]]}
    plan = store.execute_plan(svg_steps(spec, data))
    assert plan["ok"] and plan["completed_steps"] == 3 and plan["failed_step_index"] is None
    assert len(data["runtime"].sent) == 4
    assert all(epoch == control.identity for _, epoch in attached)
    assert not control.forbidden


def test_svg_plan_stops_when_real_observer_refutes_second_effect(registered_svg_catalog):
    store, spec, data, _, live, _, _ = registered_svg_catalog
    def dispatch(name, inputs, **pins):
        live.dispatched = False  # A new trial starts; the canvas itself stays live.
        result = store.execute(name, inputs, **pins)
        data["runtime"].wrong = True
        return result
    result = store.execute_plan(svg_steps(spec, data), execute_step=dispatch)
    assert not result["ok"] and result["status"] == "uncertain"
    assert result["completed_steps"] == 1 and result["failed_step_index"] == 1
    assert result["executions"][1]["result"]["verdict"] == "refuted"
    assert len(data["runtime"].sent) == 2


@pytest.mark.parametrize("field", ["pid", "launch_time"])
def test_svg_execution_and_plan_refuse_successor_main_epoch(registered_svg_catalog, field):
    store, spec, data, _, _, control, attached = registered_svg_catalog
    expected = dict(control.identity)
    expected[field] += 1
    refused = store.execute(spec["name"], data["inputs"][6], expected_identity=expected)
    assert refused["status"] == "refused" and not attached
    def dispatch(name, inputs, **pins):
        result = store.execute(name, inputs, **pins)
        control.identity[field] += 1
        return result
    plan = store.execute_plan(svg_steps(spec, data), execute_step=dispatch)
    assert not plan["ok"] and plan["status"] == "uncertain"
    assert plan["completed_steps"] == 1 and plan["failed_step_index"] == 1
    assert len(data["runtime"].sent) == 1


@pytest.mark.parametrize("svg_fixture", ["strict", "mixed"], indirect=True)
def test_svg_observe_returns_current_differing_labels_without_actuation_or_proof(registered_svg_catalog, monkeypatch):
    store, spec, data, _, live, control, attached = registered_svg_catalog
    current = ["Existing vertex", "", "duplicate", "duplicate", "東京"]
    live.state = list(current)
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "0")
    result = store.observe(spec["name"], data["inputs"][6], expected_catalog_sha256=spec["catalog_sha256"])
    assert result["ok"] and result["status"] == "observed" and result["oracle_matches"] is False
    assert result["values"] == {"labels": current} and result["identity"] == control.identity
    assert result["actuated"] is False and result["evidence_only"] and result["proof_created"] is False
    raw = R.load_json(result["observer_receipt"])
    assert raw["complete"] is True and raw["status"] == "refuted" and raw["matches"] is False
    assert raw["expected_identity"] == raw["identity"] == raw["after_identity"] == control.identity
    assert all(raw[field] == 0 for field in ("app_evaluations", "method_calls", "getters_invoked", "model_calls"))
    assert not data["runtime"].sent and [kind for kind, _ in attached] == ["read"]
    assert not control.forbidden


@pytest.mark.parametrize("missing", ["oracle", "observer_contract"])
def test_svg_observe_keeps_both_independent_path_inputs_mandatory(registered_svg_catalog, missing):
    store, spec, data, _, _, _, attached = registered_svg_catalog
    inputs = dict(data["inputs"][6])
    del inputs[missing]
    result = store.observe(spec["name"], inputs)
    assert not result["ok"] and result["status"] == "refused" and not attached


@pytest.mark.parametrize("fault", ["epoch", "after_epoch", "coherent_wrong_pid", "listener_pid", "listener_uid",
    "renderer_uid", "renderer_executable", "endpoint_url", "observer_pin", "complete", "cleanup", "evaluation"])
def test_svg_observe_refuses_mismatching_epoch_endpoint_renderer_or_resources(registered_svg_catalog, fault):
    store, spec, data, _, _, _, _ = registered_svg_catalog
    def tamper(observation, after):
        endpoint = observation["endpoint_identity"]
        if fault == "epoch":
            observation["identity"]["launch_time"] += 1
            observation["after_identity"]["launch_time"] += 1
        elif fault == "after_epoch":
            observation["after_identity"]["launch_time"] += 1
        elif fault == "coherent_wrong_pid":
            # Main and listener agree, but disagree with the supplied epoch.
            observation["identity"]["pid"] += 1
            observation["after_identity"]["pid"] += 1
            endpoint["listener"]["pid"] += 1
        elif fault == "listener_pid":
            endpoint["listener"]["pid"] += 1
        elif fault == "listener_uid":
            endpoint["listener"]["uid"] += 1
        elif fault == "renderer_uid":
            endpoint["renderer_processes"][0]["uid"] += 1
        elif fault == "renderer_executable":
            endpoint["renderer_processes"][0]["executable"] = "/foreign/Renderer"
        elif fault == "endpoint_url":
            endpoint["target_url"] = "file:///foreign/document.html"
        elif fault == "observer_pin":
            observation["resources"]["adapter"]["source"]["sha256"] = "0" * 64
        elif fault == "complete":
            observation["complete"] = False
        elif fault == "cleanup":
            observation["cleanup"]["page_socket_closed"] = False
        else:
            observation["app_evaluations"] = 1
    data["runtime"].tamper = tamper
    result = store.observe(spec["name"], data["inputs"][6])
    assert not result["ok"] and result["status"] == "uncertain"
    assert "values" not in result and result["terminal_on_failure"] and not result["automatic_retry"]
    assert not data["runtime"].sent


def test_svg_observe_rejects_a_contradictory_echoed_epoch(registered_svg_catalog, monkeypatch):
    from anyuse.execution import rea_svg_observer as S
    store, spec, data, _, _, _, _ = registered_svg_catalog
    collect = S.collect
    def inconsistent(*args, **kwargs):
        observation = collect(*args, **kwargs)
        observation["expected_identity"]["launch_time"] += 1
        return observation
    monkeypatch.setattr(S, "collect", inconsistent)
    result = store.observe(spec["name"], data["inputs"][6])
    assert not result["ok"] and result["status"] == "uncertain" and "values" not in result
    assert not data["runtime"].sent


@pytest.mark.parametrize("changed", ["observer_contract", "config"])
def test_svg_observe_refuses_case_or_transport_drift_during_capture(registered_svg_catalog, changed):
    store, spec, data, _, _, _, _ = registered_svg_catalog
    inputs = data["inputs"][6]
    def tamper(observation, after):
        path = Path(data["config"] if changed == "config" else inputs[changed])
        path.write_text(path.read_text() + " ")
    data["runtime"].tamper = tamper
    result = store.observe(spec["name"], inputs)
    assert not result["ok"] and result["status"] == "uncertain" and "values" not in result
    assert not data["runtime"].sent


def test_svg_observe_then_plan_is_available_to_planner_and_keeps_recording(registered_svg_catalog):
    store, spec, data, _, _, _, _ = registered_svg_catalog
    orch, capture, executor = orchestrator(store, [
        {"status": "continue", "action": "rea_observe", "name": spec["name"], "inputs": data["inputs"][6]},
        {"status": "continue", "action": "rea_plan", "steps": svg_steps(spec, data)},
        {"status": "done", "thought": "diagram complete"}])
    recorder = Mock()
    assert orch.run("Update the canvas", recorder=recorder) == "diagram complete"
    prompt = orch._build_prompt("Update the canvas")
    assert '"learned_svg"' in prompt and '"oracle_matches": false' in prompt
    assert "Independent initial canvas" in prompt and "keep all typed inputs" in prompt
    assert capture.capture.call_count == 3 and len(data["runtime"].sent) == 3
    recorder.record_rea_plan.assert_called_once()
    recorder.save.assert_called_once()
    recorder.discard.assert_not_called()
    assert executor.execute.call_count == 1


_OLD_SVG_SOURCE = {"sha256": "f7168a23712e2a7868bcde686dc5f3cfc90198bf0f9773f0b109f4bae2b84c88", "bytes": 60326}
_OLD_MAPPING_SOURCE = {"sha256": "61a6dd1b9362f7cd98c56661dddbe9e3d637cc5a3bbd7b77f7adbd2cce282bb5", "bytes": 35270}


def historical_svg_proof(proof, *, old_mapping=False):
    """Simulate retained old producer pins, without weakening measured rows."""
    proof = copy.deepcopy(proof)
    adapter = proof["resources"]["svg_observer"]["adapter"]
    adapter["source"].update(_OLD_SVG_SOURCE)
    if old_mapping:
        adapter["dependencies"]["mapping"].update(_OLD_MAPPING_SOURCE)
    for row in proof["cases"]:
        for phase in ("before", "after"):
            observation = row[phase + "_observation"]
            observation["resources"]["adapter"] = copy.deepcopy(adapter)
            observation.pop("expected_identity", None)  # The reviewed producer had no caller-epoch input.
            row[phase + "_observation_sha256"] = R.digest(observation)
    return proof


@pytest.mark.parametrize("svg_fixture", ["strict", "mixed"], indirect=True)
@pytest.mark.parametrize("old_mapping", [False, True])
def test_reviewed_historical_svg_proof_registers_and_replays_with_current_epoch_bound_reader(
        registered_svg_catalog, tmp_path, old_mapping):
    from anyuse.execution import rea_actions as A
    store, _, data, candidate, _, control, _ = registered_svg_catalog
    catalog = R.load_json(store._load()["registrations"]["canvas"]["catalog"])
    entry = catalog["actions"][0]["entry"]
    legacy = historical_svg_proof(R.load_json(entry["proof"]), old_mapping=old_mapping)
    original = copy.deepcopy(legacy)
    ready, why = R.proof_status(candidate, legacy, candidate["target"])
    assert ready and "reviewed historical source" in why
    assert legacy == original
    path = tmp_path / "historical-proof.json"
    R.write_json(path, legacy)
    retained_bytes = path.read_bytes()
    A.describe(candidate, legacy, str(data["app"]), "Load complete SVG labels",
               "Retained reviewed history with strict current live resources.", str(tmp_path / "historical-card"))
    A.catalog([{**entry, "proof": str(path), "card": str(tmp_path / "historical-card/action.json")}],
              str(tmp_path / "historical-catalog"))
    registered = C.CatalogStore(tmp_path / "historical-registrations.json", timeout=5)
    spec = registered.register("history", tmp_path / "historical-catalog/catalog.json")[0]
    result = registered.execute(spec["name"], data["inputs"][6])
    assert result["ok"] and result["result"]["verdict"] == "verified"
    assert result["result"]["proof_resource_admission"] == (
        "reviewed_svg_observer_epoch_and_mapping_cache_v1" if old_mapping else "reviewed_svg_observer_epoch_v1")
    live = H.resources(candidate)
    assert result["result"]["live_resources_sha256"] == R.digest(live)
    for phase in ("before", "after"):
        observation = result["result"][phase + "_observation"]
        assert observation["resources"]["adapter"] == live["svg_observer"]["adapter"]
        assert observation["resources"]["adapter"]["source"]["sha256"] != _OLD_SVG_SOURCE["sha256"]
        assert observation["expected_identity"] == observation["identity"] == observation["after_identity"] == control.identity
    observed = registered.observe(spec["name"], data["inputs"][7])
    assert observed["ok"] and observed["oracle_matches"] is False
    assert R.load_json(observed["observer_receipt"])["resources"]["adapter"] == live["svg_observer"]["adapter"]
    fresh = R.verify(candidate, [{"inputs": row} for row in data["inputs"][7:10]], str(data["app"]),
                     str(tmp_path / "fresh-verification"), timeout=5)
    assert fresh["status"] == "verified" and fresh["resources"] == live
    assert all(row["after_observation"]["resources"]["adapter"] == live["svg_observer"]["adapter"]
               for row in fresh["cases"])
    assert path.read_bytes() == retained_bytes and legacy == original
    assert not control.forbidden


@pytest.mark.parametrize("damage", ["unknown_source", "source_size", "source_path", "source_extra",
    "unknown_mapping", "mapping_size", "mapping_path", "inspector", "transport", "row_source", "row_values"])
def test_historical_svg_admission_never_relaxes_unknown_or_sibling_pins_or_row_validation(
        registered_svg_catalog, tmp_path, damage):
    store, _, data, candidate, _, control, attached = registered_svg_catalog
    catalog = R.load_json(store._load()["registrations"]["canvas"]["catalog"])
    legacy = historical_svg_proof(R.load_json(catalog["actions"][0]["entry"]["proof"]), old_mapping=True)
    adapter = legacy["resources"]["svg_observer"]["adapter"]
    if damage == "unknown_source":
        adapter["source"]["sha256"] = "0" * 64
    elif damage == "source_size":
        adapter["source"]["bytes"] += 1
    elif damage == "source_path":
        adapter["source"]["file"] = "/unreviewed/rea_svg_observer.py"
    elif damage == "source_extra":
        adapter["source"]["revision"] = "unreviewed"
    elif damage == "unknown_mapping":
        adapter["dependencies"]["mapping"]["sha256"] = "0" * 64
    elif damage == "mapping_size":
        adapter["dependencies"]["mapping"]["bytes"] += 1
    elif damage == "mapping_path":
        adapter["dependencies"]["mapping"]["file"] = "/unreviewed/rea_mapping.py"
    elif damage == "inspector":
        legacy["resources"]["inspector_bridge"]["adapter"]["sha256"] = "0" * 64
    elif damage == "transport":
        legacy["resources"]["svg_observer"]["transport"]["endpoint"] = "http://127.0.0.1:9247"
    else:
        row = legacy["cases"][0]
        if damage == "row_source":
            row["after_observation"]["resources"]["adapter"]["source"].update(H.resources(candidate)["svg_observer"]["adapter"]["source"])
        else:
            row["after_observation"]["values"][0] = "Forged label"
        row["after_observation_sha256"] = R.digest(row["after_observation"])
    assert not R.proof_status(candidate, legacy, candidate["target"])[0]
    with pytest.raises(ValueError):
        R.replay(candidate, legacy, data["inputs"][6], str(data["app"]), str(tmp_path / "refused-historical-replay"),
                 timeout=5, expected_identity=control.identity)
    assert not attached and not data["runtime"].sent


@pytest.mark.parametrize("changed", ["observer", "mapping"])
def test_historical_svg_migration_requires_the_exact_reviewed_installed_sources(
        registered_svg_catalog, monkeypatch, changed):
    store, _, _, candidate, _, _, _ = registered_svg_catalog
    catalog = R.load_json(store._load()["registrations"]["canvas"]["catalog"])
    legacy = historical_svg_proof(R.load_json(catalog["actions"][0]["entry"]["proof"]), old_mapping=True)
    live = H.resources(candidate)
    adapter = live["svg_observer"]["adapter"]
    pin = adapter["source"] if changed == "observer" else adapter["dependencies"]["mapping"]
    pin["sha256"] = "0" * 64
    monkeypatch.setattr(H, "resources", lambda _: copy.deepcopy(live))
    assert not R.proof_status(candidate, legacy, candidate["target"])[0]


def test_reviewed_historical_pins_cannot_be_returned_by_live_observe_replay_or_verify(
        registered_svg_catalog, tmp_path, monkeypatch):
    from anyuse.execution import rea_svg_observer as S
    store, spec, data, candidate, _, control, _ = registered_svg_catalog
    collect = S.collect
    def stale(*args, **kwargs):
        observation = collect(*args, **kwargs)
        observation["resources"]["adapter"]["source"].update(_OLD_SVG_SOURCE)
        return observation
    monkeypatch.setattr(S, "collect", stale)
    observed = store.observe(spec["name"], data["inputs"][6])
    assert not observed["ok"] and observed["status"] == "uncertain"
    executed = store.execute(spec["name"], data["inputs"][6], expected_identity=control.identity)
    assert not executed["ok"] and executed["status"] == "uncertain"
    with pytest.raises(ValueError, match="complete certain independent"):
        R.verify(candidate, [{"inputs": row} for row in data["inputs"][6:9]], str(data["app"]),
                 str(tmp_path / "stale-live-verification"), timeout=5)
    proof = R.load_json(tmp_path / "stale-live-verification/proof.json")
    assert proof["status"] == "incomplete" and proof["resources"] == H.resources(candidate)
    assert not data["runtime"].sent


def test_historical_svg_validation_rechecks_current_resources_after_retained_rows(
        registered_svg_catalog, monkeypatch):
    store, _, data, candidate, _, _, attached = registered_svg_catalog
    catalog = R.load_json(store._load()["registrations"]["canvas"]["catalog"])
    legacy = historical_svg_proof(R.load_json(catalog["actions"][0]["entry"]["proof"]), old_mapping=True)
    live = H.resources(candidate)
    reads = []
    def resources(_):
        reads.append(True)
        value = copy.deepcopy(live)
        if len(reads) > 1:
            value["svg_observer"]["adapter"]["source"]["sha256"] = "0" * 64
        return value
    monkeypatch.setattr(H, "resources", resources)
    ready, why = R.proof_status(candidate, legacy, candidate["target"])
    assert not ready and "changed during proof validation" in why
    assert len(reads) == 2 and not attached and not data["runtime"].sent


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
