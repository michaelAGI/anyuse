"""Generic front door over real workflow/campaign gates and inert effects."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from test_rea_campaign_coordinator import campaign, runtime, visual_campaign_factory, visual_coordinated
from test_rea_agent_driver import model, static_decision
from anyuse.execution import rea_agent as G
from anyuse.execution import rea_agent_driver as D
from anyuse.execution import rea_generic_workflow as W
from anyuse.execution import rea_mapping as R


@pytest.fixture
def workflow(visual_coordinated, tmp_path, monkeypatch, request):
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "default")
    e = visual_coordinated
    policy = e.f.profile["policy"]
    spec = {"format": W.FORMAT, "id": e.f.profile["id"], "task": "Set the requested visible label",
            "before": policy["before"], "after": policy["after"], "observer_model": policy["model"],
            "owned": True, "restart_allowed": True, "params": e.f.profile["params"],
            "window_title": policy["observer"]["window_title"], "plan": G._load(e.root)["plan"],
            "resources": e.cfg["resources"], "replays": e.cfg["replays"],
            "limits": {"trial_seconds": 120, "discovery_decisions": getattr(request, "param", 1)},
            "publication": e.cfg["publication"]}
    root = tmp_path / "generic-workflow"
    W.begin(str(e.f.app), str(G._load(e.root)["rea"]), spec, root)
    # The inert fixture has an older policy identifier; retained measurements
    # must bind the actual front door's independently frozen policy.
    inert_trial = W.V.run_trial
    def trial(bundle, actor, policy, *args, **kwargs):
        result = inert_trial(bundle, actor, policy, *args, **kwargs)
        if result.get("measurement") is not None:
            result["measurement"]["policy_sha256"] = R.digest(policy)
            e.f.measurements[result["measurement"]["case_id"]]["policy_sha256"] = R.digest(policy)
        return result
    monkeypatch.setattr(W.V, "run_trial", trial)
    e.workflow_root, e.spec = root, spec
    e.proposal = static_decision({"op": "propose", "rationale": "Typed candidate still requires independent trials",
        "citations": [{"artifact": "inventory.json", "pointer": "/target"}], "actor": e.f.actor,
        "observer_profile_sha256": G._load(root / "session")["observer_profile_sha256"]})
    return e


@pytest.fixture
def source_workflow(workflow, tmp_path):
    from anyuse.execution import model_adapter as M, rea_native_path_builder as N
    header = tmp_path / "caller-original.h"
    header.write_text("// Caller-retained synthetic original; no real app ABI is proved.\n"
                      "struct OwnedNode { unsigned generation; int payload; };\n")
    config = M.ModelConfig("local", "http://fixture.invalid/v1", "independent-inert-source-reviewer", "",
                           timeout=.01, max_tokens=2048, retries=0)
    policy = {"format": N.POLICY, "mode": "separate_model", "model": asdict(config), "max_calls": 1,
              "source_pins": [{"role": "header", **W._pin(header)}]}
    spec = {**copy.deepcopy(workflow.spec), "native_path_review": policy}
    root = tmp_path / "source-import-workflow"
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    return SimpleNamespace(root=root, session=root / "session", header=header, policy=policy, e=workflow)


def discover(e, *, transport="prompt_json"):
    adapter, seen = model([e.proposal])
    if transport == "json_object":
        adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = W.advance(e.workflow_root, adapter=adapter)
    assert result["status"] == "needs_setup", result.get("error")
    return result, seen


def finish_setup(e, result):
    W.public_start(e.workflow_root)
    checkpoint = R.load_json(e.checkpoint({"pending": result["pending"]}))
    return W.public_finish(e.workflow_root, checkpoint["evidence"], restart=checkpoint.get("restart"))


def reach_restart(e):
    result, _ = discover(e)
    for _ in range(8):
        if result["status"] == "needs_restart":
            return result
        assert result["status"] == "needs_setup", result.get("error")
        result = finish_setup(e, result)
    pytest.fail("real campaign did not reach its frozen held-out restart")


@pytest.mark.parametrize("transport", ["prompt_json", "json_object"])
def test_generic_http_object_transport_preserves_configured_model(workflow, transport):
    result, seen = discover(workflow, transport=transport)
    assert len(seen) == 1 and seen[0]["model"] == "inert-reasoner"
    if transport == "json_object":
        assert seen[0]["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in seen[0]
    wire = R.load_json(workflow.workflow_root / "event-0000/discovery/decision-000/model-input.json")
    assert wire["schema_transport"] == transport
    assert not workflow.calls and result["verification_tier"] == "model_judged"


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_generic_cli_model_does_not_receive_unsupported_open_strict_schema(workflow, provider):
    from anyuse.execution import model_adapter as M
    captured = []
    def runner(argv, stdin, cwd, timeout):
        captured.append((argv, stdin))
        assert "--json-schema" not in argv and "--output-schema" not in argv
        if provider == "claude-cli":
            assert argv[argv.index("--tools") + 1] == "" and "--safe-mode" in argv
        text = json.dumps(workflow.proposal)
        if provider == "codex-cli":
            answer = Path(argv[argv.index("--output-last-message") + 1])
            answer.write_text(text)
            return 0, "", ""
        return 0, json.dumps({"type": "result", "subtype": "success", "result": text}), ""
    cfg = M.ModelConfig(provider, "", "inert-model", "", timeout=.01, retries=0)
    result = W.advance(workflow.workflow_root, adapter=M.ModelAdapter(cfg, cli_runner=runner))
    if provider == "codex-cli":
        assert result["status"] == "failed_no_retry" and not captured
        assert "tool-free" in result["error"]["message"] or "Codex" in result["error"]["message"]
        return
    assert result["status"] == "needs_setup", result.get("error")
    assert len(captured) == 1 and not workflow.calls


def test_closed_protocol_does_not_gain_cli_model_access(workflow, tmp_path):
    from anyuse.execution import model_adapter as M
    called = []
    adapter = M.ModelAdapter(M.ModelConfig("claude-cli", "", "inert-model", "", timeout=.01, retries=0),
                             cli_runner=lambda *args: called.append(args))
    result = D.run(workflow.workflow_root / "session", tmp_path / "closed-driver", adapter=adapter,
        allowed_tools=("evidence",), decision_protocol="request_json_v1", timeout_seconds=300)
    assert result["status"] == "stopped_failure" and not called
    assert not G._load(workflow.workflow_root / "session")["steps"]


def test_generic_cli_effective_timeout_must_fit_original_budget(workflow, tmp_path):
    from anyuse.execution import model_adapter as M
    called = []
    adapter = M.ModelAdapter(M.ModelConfig("claude-cli", "", "inert-model", "", timeout=.01, retries=0),
                             cli_runner=lambda *args: called.append(args))
    result = D.run(workflow.workflow_root / "session", tmp_path / "short-driver", adapter=adapter,
        allowed_tools=("evidence",), decision_protocol=D.TOOL_OBJECT, timeout_seconds=1)
    assert result["status"] == "stopped_failure" and not called
    assert "timeout" in result["error"]["message"]
    assert not G._load(workflow.workflow_root / "session")["steps"]


def test_discovery_budget_requires_explicit_continuation_and_reuses_verified_memory(workflow):
    first, _ = model([static_decision({"op": "evidence", "artifact": "inventory.json", "offset": 0, "limit": 8000})])
    result = W.advance(workflow.workflow_root, adapter=first)
    assert result["status"] == "discovery_incomplete", result.get("error")
    assert R.load_json(workflow.workflow_root / "workflow.json")["discovery_memory"]
    with pytest.raises(ValueError, match="silently resume"):
        W.advance(workflow.workflow_root)
    second, seen = model([workflow.proposal])
    result = W.advance(workflow.workflow_root, adapter=second, continue_discovery=True)
    assert result["status"] == "needs_setup", result.get("error")
    retained = R.load_json(workflow.workflow_root / "event-0001/discovery/driver.json")
    assert retained["investigation_memory"]["restored_turns"] == 1
    assert len(seen) == 1 and not workflow.calls


@pytest.mark.parametrize("workflow", [2], indirect=True)
def test_invalid_static_scalar_is_correctable_without_poisoning_session(workflow):
    invalid = static_decision({"op": "evidence", "artifact": "inventory.json", "offset": None, "limit": None})
    adapter, seen = model([invalid, workflow.proposal])
    result = W.advance(workflow.workflow_root, adapter=adapter)
    assert result["status"] == "needs_setup", result.get("error")
    assert len(seen) == 2 and not workflow.calls
    steps = G._load(workflow.workflow_root / "session")["steps"]
    assert len(steps) == 1 and steps[0]["request"]["op"] == "propose" and steps[0]["status"] == "completed"


def test_model_stop_can_continue_but_tool_failure_cannot(workflow, monkeypatch):
    stopped, _ = model([static_decision()])
    result = W.advance(workflow.workflow_root, adapter=stopped)
    assert result["status"] == "discovery_stopped"
    called = []
    def fail(*args, **kwargs):
        called.append(kwargs.get("resume_from"))
        raise R.ResourceReviewRequired("actual effects are unknown")
    monkeypatch.setattr(W.D, "run", fail)
    result = W.advance(workflow.workflow_root, continue_discovery=True)
    assert result["status"] == "failed_no_retry" and result["automatic_retry"] is False
    for continued in (False, True):
        with pytest.raises(ValueError):
            W.advance(workflow.workflow_root, continue_discovery=continued)
    assert len(called) == 1


def test_public_claim_is_once_only_and_waiting_advance_has_no_effects(workflow):
    result, _ = discover(workflow)
    first = W.public_start(workflow.workflow_root)
    with pytest.raises(ValueError, match="unconsumed"):
        W.public_start(workflow.workflow_root)
    repeated = W.advance(workflow.workflow_root)
    assert repeated["pending"] == result["pending"] and not workflow.calls
    assert R.load_json(first["public_handoff"]["file"])["kind"] == "setup"


def test_reconnect_exposes_existing_public_claim_and_finishes_without_repeating_setup(workflow):
    result, _ = discover(workflow)
    assert W.context(workflow.workflow_root)["public_handoff"] is None
    started = W.public_start(workflow.workflow_root)
    resumed = W.context(workflow.workflow_root)
    assert resumed["status"] == "needs_setup" and resumed["pending"] == result["pending"]
    assert resumed["public_handoff"] == started["public_handoff"]
    instructions = resumed["public_setup"]["instructions"]
    assert "Resume that handoff" in instructions and "public-finish" in instructions
    assert "do not claim or repeat setup automatically" in instructions
    checkpoint = R.load_json(workflow.checkpoint({"pending": resumed["pending"]}))
    result = W.public_finish(workflow.workflow_root, checkpoint["evidence"])
    assert result["status"] == "needs_setup" and len(workflow.calls) == 1, result.get("error")
    assert W.context(workflow.workflow_root)["public_handoff"] is None


@pytest.mark.parametrize("change", ["native_mode", "review_model", "review_source_pin"])
def test_context_rejects_mutation_of_caller_frozen_native_review_contract(source_workflow, change):
    e = source_workflow
    assert W.context(e.root)["status"] == "discovering"
    state = G._load(e.session)
    if change == "native_mode":
        state["native_contract_mode"] = "declared"
    elif change == "review_model":
        state["native_path_review"]["model"]["model"] = "different-reviewer"
    else:
        state["native_path_review"]["source_pins"][0]["sha256"] = "0" * 64
    R.write_json(e.session / "session.json", state)
    with pytest.raises(ValueError, match="caller-frozen native review mode or policy changed"):
        W.context(e.root)
    assert not e.e.calls and not state["steps"]


def test_begin_imports_exact_caller_header_and_generic_driver_can_read_visible_copy(source_workflow, tmp_path):
    e = source_workflow
    state = G._load(e.session)
    assert state["native_path_review"] == e.policy
    assert len(state["native_path_source_imports"]) == 1
    name, imported = next(iter(state["native_path_source_imports"].items()))
    assert name == "source-originals/0000.txt" and imported["role"] == "header"
    assert imported["original"] == W._pin(e.header)
    assert imported["copy"] == W._pin(e.session / name) == state["artifacts"][name]
    assert (e.session / name).read_bytes() == e.header.read_bytes()
    assert (e.session / name).resolve() != e.header.resolve()
    context = G.context(e.session)
    admitted, manifest = D.model_view(context, state, allowed_tools=("build_native_path", "evidence"))
    assert admitted["artifacts"][name] == imported["copy"] and name in manifest["visible_artifacts"]
    narrowed, _ = D.model_view(context, state, allowed_tools=("evidence",))
    assert name not in narrowed["artifacts"]
    adapter, seen = model([static_decision({"op": "evidence", "artifact": name, "offset": 0, "limit": 8000})])
    out = tmp_path / "source-import-driver"
    result = D.run(e.session, out, adapter=adapter, max_decisions=1, until="step",
                   allowed_tools=("build_native_path", "evidence"), decision_protocol=D.TOOL_OBJECT)
    assert result["status"] == "boundary_reached" and result["tool_dispatches"] == len(seen) == 1, result.get("error")
    actual_view = json.loads(seen[0]["messages"][1]["content"])
    assert actual_view["artifacts"][name] == imported["copy"]
    observed = R.load_json(out / "decision-000/tool-result.json")["feedback"]
    assert observed["status"] == "completed" and observed["feedback"]["text"] == e.header.read_text()
    assert not e.e.calls and G._load(e.session)["status"] == "investigating"


@pytest.mark.parametrize("failure", ["before", "uncertain_dispatch"])
def test_failed_or_uncertain_trial_is_retained_without_native_retry(workflow, failure):
    result, _ = discover(workflow)
    if failure == "before":
        workflow.fail_precondition = True
    else:
        workflow.fail_after_dispatch = True
    failed = finish_setup(workflow, result)
    assert failed["status"] == "failed_no_retry" and failed["catalog"] is None
    assert len(workflow.calls) == (0 if failure == "before" else 1)
    with pytest.raises(ValueError):
        W.advance(workflow.workflow_root, continue_discovery=True)
    with pytest.raises(ValueError):
        W.public_start(workflow.workflow_root)
    assert len(workflow.calls) == (0 if failure == "before" else 1)


def test_restart_helper_rejects_setup_without_consuming_claim(workflow):
    discover(workflow)
    with pytest.raises(ValueError, match="restart required"):
        W.public_restart(workflow.workflow_root)
    assert R.load_json(workflow.workflow_root / "workflow.json")["public_handoff"] is None
    assert not workflow.calls


def test_restart_preparation_failure_retains_consumed_claim_and_precise_failure(workflow, monkeypatch):
    from anyuse.execution import rea_public_campaign as U
    reach_restart(workflow)
    calls = []
    def broken_backend():
        calls.append("construct")
        assert R.load_json(workflow.workflow_root / "workflow.json")["status"] == "public_running"
        raise ValueError("public backend initialization failed before dispatch")
    monkeypatch.setattr(U, "PublicPlayback", broken_backend)
    result = W.public_restart(workflow.workflow_root)
    assert result["status"] == "failed_no_retry" and "initialization failed" in result["error"]["message"]
    assert R.load_json(workflow.workflow_root / "workflow.json")["public_handoff"]
    with pytest.raises(ValueError):
        W.public_restart(workflow.workflow_root)
    assert calls == ["construct"] and len(workflow.calls) == 6


@pytest.mark.parametrize("succeeds", [True, False])
def test_public_restart_is_witnessed_once_and_never_retries_uncertain_restart(workflow, monkeypatch, succeeds):
    from anyuse.execution import rea_process_restart as RR, rea_public_campaign as U
    result = reach_restart(workflow)
    old = copy.deepcopy(workflow.identity)
    calls = []
    backend = SimpleNamespace(producers={"stop": {"fixture": "stop"}, "launch": {"fixture": "launch"}},
                              stop=lambda *args: None, launch=lambda *args: None)
    monkeypatch.setattr(U, "PublicPlayback", lambda: backend)
    def restart(bundle, identity, intent, out, **kwargs):
        calls.append(intent)
        assert identity == old and kwargs["stop_hook"] == backend.stop and kwargs["launch_hook"] == backend.launch
        assert R.load_json(workflow.workflow_root / "workflow.json")["status"] == "public_running"
        if not succeeds:
            raise R.ResourceReviewRequired("normal stop had an uncertain outcome")
        checkpoint = R.load_json(workflow.checkpoint({"pending": result["pending"]}))
        entry = checkpoint["restart"]
        receipt = R.load_json(entry["receipt"]["file"])
        receipt["intent"] = entry["intent"]
        R.write_json(entry["receipt"]["file"], receipt)
        entry["receipt"] = W._pin(Path(entry["receipt"]["file"]))
        workflow.f.entries["heldout"][0]["restart"] = entry
        return {"status": "restart_measurement_validated", "receipt": entry["receipt"],
                "measurement": W._pin(Path(receipt["baseline"]["file"]))}
    monkeypatch.setattr(RR, "run_restart", restart)
    result = W.public_restart(workflow.workflow_root, timeout=12)
    assert len(calls) == 1
    if succeeds:
        assert result["status"] == "needs_setup", result.get("error")
        assert result["pending"]["identity"] == workflow.f.new_identity
        result = finish_setup(workflow, result)
        assert len(workflow.calls) == 7 and result["status"] == "needs_setup", result.get("error")
    else:
        assert result["status"] == "failed_no_retry"
        with pytest.raises(ValueError):
            W.public_restart(workflow.workflow_root)
        assert len(calls) == 1 and len(workflow.calls) == 6


def test_frozen_task_and_source_change_refuse_before_model_or_public_effect(workflow, monkeypatch):
    sources = W._sources()
    changed = copy.deepcopy(sources); changed["workflow"]["sha256"] = "0" * 64
    monkeypatch.setattr(W, "_sources", lambda: changed)
    with pytest.raises(ValueError, match="source closure changed"):
        W.advance(workflow.workflow_root)
    assert not workflow.calls
    monkeypatch.setattr(W, "_sources", lambda: sources)
    spec = R.load_json(workflow.workflow_root / "spec.json"); spec["after"] = "A different result"
    R.write_json(workflow.workflow_root / "spec.json", spec)
    with pytest.raises(ValueError, match="specification changed"):
        W.context(workflow.workflow_root)


@pytest.mark.parametrize("invalid", ["publication", "replay_inputs"])
def test_malformed_campaign_contract_rejected_before_discovery(workflow, tmp_path, invalid):
    spec = copy.deepcopy(workflow.spec)
    if invalid == "publication":
        spec["publication"] = {"summary": "", "details": "Must not spend discovery on this malformed contract"}
    else:
        spec["replays"] = [{"id": "invalid-replay", "inputs": {"undeclared": "value"}}]
    out = tmp_path / "invalid-workflow"
    with pytest.raises(ValueError):
        W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, out)
    assert not workflow.calls
    assert not (out / "workflow.json").exists()


def test_workflow_cli_routes_continuation_and_actual_evidence_without_live_effects(tmp_path, monkeypatch):
    from anyuse.execution.rea_cli import rea_commands
    calls = []
    monkeypatch.setattr(W, "advance", lambda root, **kw: calls.append((root, kw)) or {"status": "discovery_incomplete"})
    runner = CliRunner()
    result = runner.invoke(rea_commands, ["workflow", "advance", str(tmp_path), "--continue-discovery"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "discovery_incomplete"
    assert calls == [(str(tmp_path), {"continue_discovery": True})]
    observed = tmp_path / "public.json"; observed.write_text('{"actual":"observation"}')
    monkeypatch.setattr(W, "public_finish", lambda root, evidence, **kw: calls.append((evidence, kw)) or {"status": "needs_setup"})
    result = runner.invoke(rea_commands, ["workflow", "public-finish", str(tmp_path), "--evidence", str(observed)])
    assert result.exit_code == 0, result.output
    assert calls[-1] == ([W._pin(observed)], {"restart": None})


def test_native_declaration_requires_registered_producer_bytes(workflow, tmp_path):
    from anyuse.execution import rea_native_contract as N, rea_runtime_bridge as B
    from test_rea_runtime_bridge import _inert_macho
    old = G._load(workflow.root)
    module = workflow.f.app / "Contents/Frameworks/Declared.dylib"
    module.parent.mkdir(parents=True, exist_ok=True); module.write_bytes(_inert_macho())
    profile = {k: copy.deepcopy(v) for k, v in workflow.f.profile.items() if k != "evidence"}
    profile["policy"]["target"] = R.fingerprint(workflow.f.app)
    root = tmp_path / "native-declaration-session"
    G.begin(workflow.f.app, old["rea"], "Set a visible label", profile["params"], ["Label matches input"],
            old["plan"], root, observer_profile=profile, native_contract_mode="declared")
    observed = G.step(root, {"op": "static_evidence", "request": {"kind": "original_code",
        "relative": "Contents/Frameworks/Declared.dylib", "offset": "0x1000", "bytes": 4, "unit_bytes": 4}})
    assert observed["feedback"]["status"] == "completed"
    unsigned = {"kind": "native_function", "target": {"module": {"path": str(module), "sha256": R._file_hash(module)},
        "offset": "0x1000"}, "abi": "default", "arguments": [{"type": "int32", "value": {"input": "query"}}], "return_type": "void"}
    declared = G.step(root, {"op": "declare_native", "actor": unsigned,
        "citations": [{"artifact": observed["feedback"]["feedback"]["artifact"], "pointer": "/range"}]})
    assert declared["feedback"]["status"] == "completed", declared["feedback"]
    actor = declared["feedback"]["feedback"]["actor"]
    assert N.resources(actor)
    file = Path(actor["signature_evidence"]["file"])
    value = R.load_json(file); value["sources"] = {}; R.write_json(file, value)
    actor["signature_evidence"]["sha256"] = R._file_hash(file)
    with pytest.raises(ValueError, match="registered|producer|source"):
        N.resources(actor)


@pytest.mark.parametrize("mode", ["attributable", "declared"])
@pytest.mark.parametrize("entrypoint", ["propose", "runtime"])
def test_declared_native_cannot_cross_into_another_discovery_session(workflow, tmp_path, monkeypatch, mode, entrypoint):
    from anyuse.execution import rea_native_contract as N, rea_runtime_providers as P
    from test_rea_runtime_bridge import _inert_macho
    old = G._load(workflow.root)
    module = workflow.f.app / "Contents/Frameworks/Declared.dylib"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_bytes(_inert_macho())
    profile = {k: copy.deepcopy(v) for k, v in workflow.f.profile.items() if k != "evidence"}
    profile["policy"]["target"] = R.fingerprint(workflow.f.app)
    owner = tmp_path / "native-owner"
    other = tmp_path / "native-other"
    for root, selected_mode in ((owner, "declared"), (other, mode)):
        G.begin(workflow.f.app, old["rea"], "Set a visible label", profile["params"], ["Label matches input"],
                old["plan"], root, observer_profile=profile, native_contract_mode=selected_mode)
    observed = G.step(owner, {"op": "static_evidence", "request": {"kind": "original_code",
        "relative": "Contents/Frameworks/Declared.dylib", "offset": "0x1000", "bytes": 4, "unit_bytes": 4}})
    unsigned = {"kind": "native_function", "target": {"module": {"path": str(module), "sha256": R._file_hash(module)},
        "offset": "0x1000"}, "abi": "default", "arguments": [], "return_type": "void"}
    declared = G.step(owner, {"op": "declare_native", "actor": unsigned,
        "citations": [{"artifact": observed["feedback"]["feedback"]["artifact"], "pointer": "/range"}]})
    assert declared["feedback"]["status"] == "completed", declared["feedback"]
    actor = declared["feedback"]["feedback"]["actor"]
    N.validate_current(actor, state=G._load(owner))
    def proposal(root):
        return {"op": "propose", "rationale": "Typed hypothesis requires independent visual trials",
                "citations": [{"artifact": "inventory.json", "pointer": "/target"}], "actor": actor,
                "observer_profile_sha256": G._load(root)["observer_profile_sha256"]}
    if entrypoint == "runtime":
        monkeypatch.setattr(P.Provider, "transaction", lambda *a, **kw: pytest.fail("foreign declaration dispatched"))
        request = {"op": "runtime", "request": {"operations": [{"op": "native_invoke",
            **{k: v for k, v in actor.items() if k != "kind"}}]}}
    else:
        request = proposal(other)
    result = G.step(other, request)
    assert result["feedback"]["status"] == "failed", result["feedback"]
    assert "current session" in result["feedback"]["feedback"]["error"]
    assert G._load(other)["status"] == "investigating"
    # Valid, promoted receipts remain reusable: session ownership is a discovery
    # check, not an extra condition imposed by the saved-catalog resource reader.
    assert N.resources(actor)
    accepted = G.step(owner, proposal(owner))
    assert accepted["feedback"]["status"] == "completed", accepted["feedback"]


@pytest.mark.parametrize("field,length", [("task", 3001), ("after", 1201), ("selection_task", 3001)])
def test_workflow_initializer_enforces_downstream_text_bounds_before_writing(workflow, tmp_path, field, length):
    spec = copy.deepcopy(workflow.spec)
    spec[field] = "x" * length
    root = tmp_path / "oversized-workflow"
    with pytest.raises(ValueError, match="bounded task"):
        W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    assert not root.exists()
    if field != "selection_task":
        args = {"task": "Read the result", "before": "Initial state", "after": "Complete result", field: "x" * length}
        with pytest.raises(ValueError, match="bounded task"):
            W.build_spec(**args)


def test_workflow_rejects_a_plan_that_cannot_fit_its_session_before_writing(workflow, tmp_path):
    oversized = {"learn": 32, "fresh": 32, "heldout": 32}
    with pytest.raises(ValueError, match="step budget"):
        W.build_spec("Set visible state", "Initial state", "Changed state", trial_policy=oversized)
    spec = W.build_spec("Set visible state", "Initial state", "Changed state")
    spec["trial_policy"] = oversized
    root = tmp_path / "unreachable-workflow"
    with pytest.raises(ValueError, match="step budget"):
        W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    assert not root.exists()


def test_direct_session_reserves_steps_for_measured_campaign(workflow, tmp_path):
    old = G._load(workflow.root)
    root = tmp_path / "too-small-session"
    profile = {k: copy.deepcopy(v) for k, v in workflow.f.profile.items() if k != "evidence"}
    with pytest.raises(ValueError, match="step budget"):
        G.begin(workflow.f.app, old["rea"], old["task"], old["params"], old["success_criteria"], old["plan"], root,
                max_steps=11, observer_profile=profile)
    assert not root.exists()


@pytest.mark.parametrize("external_proposal", [False, True])
def test_discovery_reserves_the_remaining_campaign_steps(workflow, tmp_path, monkeypatch, external_proposal):
    spec = copy.deepcopy(workflow.spec)
    spec["limits"].update(max_steps=12, discovery_decisions=32)
    root = tmp_path / "exact-step-budget"
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    if external_proposal:
        assert G.step(root / "session", workflow.proposal["request"])["feedback"]["status"] == "completed"
    adapter, seen = model([] if external_proposal else [workflow.proposal])
    run = D.run
    decisions = []
    def bounded_run(*args, **kwargs):
        decisions.append(kwargs["max_decisions"])
        return run(*args, **kwargs)
    monkeypatch.setattr(D, "run", bounded_run)
    result = W.advance(root, adapter=adapter)
    assert result["status"] == "needs_setup", result.get("error")
    assert decisions == [1] and len(seen) == (0 if external_proposal else 1)


def test_external_agent_can_propose_before_first_advance_without_an_embedded_model_call(workflow):
    e = workflow
    result = G.step(e.workflow_root / "session", e.proposal["request"])
    assert result["feedback"]["status"] == "completed"
    adapter, seen = model([])
    result = W.advance(e.workflow_root, adapter=adapter)
    assert result["status"] == "needs_setup", result.get("error")
    assert adapter.call_count == 0 and not seen and not e.calls
    driver = R.load_json(e.workflow_root / "event-0000/discovery/driver.json")
    assert driver["status"] == "boundary_reached" and driver["model_calls"] == 0
    assert G._load(e.workflow_root / "session")["status"] == "hypothesis"


def test_binary_receiver_mode_requires_declared_types_and_stays_frozen(workflow, tmp_path):
    spec = {**copy.deepcopy(workflow.spec), "native_receiver_mode": "binary_runtime_hypothesis"}
    with pytest.raises(ValueError, match="explicit declared-native"):
        W.task_contract(R.fingerprint(workflow.f.app), spec)
    spec["native_contract_mode"] = "declared"
    root = tmp_path / "binary-receiver-workflow"
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    state = G._load(root / "session")
    assert state["native_receiver_mode"] == "binary_runtime_hypothesis"
    adapter, seen = model([workflow.proposal])
    result = W.advance(root, adapter=adapter)
    assert result["status"] == "needs_setup", result.get("error")
    sent = json.loads(seen[0]["messages"][1]["content"])
    assert "declare_native_receiver" in sent["tools"]
    assert "declare_native_receiver_v2" in sent["tools"]
    assert "correlate_native_trace_v2" in sent["tools"]
    assert "declare_native_receiver_v2" in sent["native_receiver_guide"]
    assert sent["runtime_agent_contract"]["native_receiver_mode"] == "binary_runtime_hypothesis"
    state = G._load(root / "session")
    state.pop("native_receiver_mode")
    R.write_json(root / "session/session.json", state)
    with pytest.raises(ValueError, match="caller-frozen native"):
        W.context(root)


def test_default_workflow_does_not_advertise_binary_receiver_experiments(workflow):
    result, seen = discover(workflow)
    assert result["status"] == "needs_setup"
    sent = json.loads(seen[0]["messages"][1]["content"])
    assert "declare_native_receiver" not in sent["tools"]
    assert "declare_native_receiver_v2" not in sent["tools"]
    assert "correlate_native_trace_v2" not in sent["tools"]
    assert "native_receiver_guide" not in sent


def test_workflow_model_can_correct_and_use_trace_ranking_before_proposal(workflow, tmp_path):
    from anyuse.execution import rea_native_receiver_v2 as V2
    spec = {**copy.deepcopy(workflow.spec), "native_contract_mode": "declared",
            "native_receiver_mode": "binary_runtime_hypothesis"}
    spec["limits"]["discovery_decisions"] = 3
    root = tmp_path / "workflow-with-trace"
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    trace = V2.discovery_guide(["correlate_native_trace_v2"])["correlate_native_trace_v2"]["example"]
    invalid = {**trace, "observed_getters": [7]}
    adapter, seen = model([static_decision(invalid), static_decision(trace), workflow.proposal])
    result = W.advance(root, adapter=adapter)
    assert result["status"] == "needs_setup", result.get("error")
    driver = R.load_json(root / "event-0000/discovery/driver.json")
    assert driver["model_calls"] == len(seen) == 3
    assert driver["tool_dispatches"] == 2 and driver["decision_corrections"]["used"] == 1
    state = G._load(root / "session")
    assert [row["request"]["op"] for row in state["steps"]] == ["correlate_native_trace_v2", "propose"]
    receipt = R.load_json(root / "session/s0000/native-receiver-trace-correlation.json")
    assert receipt["candidate_count"] == 1 and receipt["proof_created"] is False
    assert receipt["input_basis"] == "caller_supplied_trace_observations"
    feedback = state["steps"][0]["feedback"]
    for key in ("candidate_count", "matched_candidate_count", "candidates_truncated", "input_basis", "evidence_only", "proof_created"):
        assert feedback[key] == receipt[key]
    last = json.loads(seen[-1]["messages"][1]["content"])
    assert "s0000/native-receiver-trace-correlation.json" in last["artifacts"]
    assert not workflow.calls and not state["proof"] and not state["recordings"]


def test_workflow_effort_rea_fast_enables_v2_and_synthesizes_1_1_1_plan(workflow, tmp_path):
    spec = {k: copy.deepcopy(v) for k, v in workflow.spec.items() if k not in {"params", "plan", "replays"}}
    spec["effort"] = "rea-fast"
    root = tmp_path / "workflow-rea-fast"
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    state = G._load(root / "session")
    assert state["native_contract_mode"] == "declared"
    assert state["native_receiver_mode"] == "binary_runtime_hypothesis"
    assert state["plan"]["trial_policy"] == {"min_learning": 1, "min_verification": 1, "min_heldout": 1, "require_restart": True}
    assert [len(state["plan"][r]) for r in ("learning", "verification", "heldout")] == [1, 1, 1]
    assert state["plan"]["heldout"][0]["restart"] is True

    custom_spec = {**copy.deepcopy(spec), "trial_policy": {"learn": 2, "fresh": 1, "heldout": 1, "restart": False}}
    profile, custom_plan, _ = W.task_contract(R.fingerprint(workflow.f.app), custom_spec)
    assert [len(custom_plan[r]) for r in ("learning", "verification", "heldout")] == [2, 1, 1]
    assert custom_plan["heldout"][0]["restart"] is False
    assert profile["scope"] == W.VC.SCOPE_NO_RESTART


@pytest.mark.parametrize("requested", [
    {"effort": "rea-strict"},
    {"trial_policy": {"preset": "strict"}},
])
def test_explicit_plan_policy_cannot_silently_weaken_workflow(workflow, tmp_path, requested):
    spec = {**copy.deepcopy(workflow.spec), **requested}
    spec["plan"]["trial_policy"] = {"preset": "fast", "restart": False}
    root = tmp_path / "conflicting-workflow"
    with pytest.raises(ValueError, match="explicit plan trial_policy conflicts"):
        W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    assert not root.exists()


def test_explicit_plan_accepts_equivalent_alias_policy_without_mutation(workflow):
    spec = copy.deepcopy(workflow.spec)
    spec["trial_policy"] = "strict"
    spec["plan"]["trial_policy"] = {"learn": 3, "fresh": 3, "heldout": 3, "restart": True}
    original = copy.deepcopy(spec)
    _, plan, _ = W.task_contract(R.fingerprint(workflow.f.app), spec)
    assert W.VC.validate_trial_policy(plan["trial_policy"]) == W.VC.DEFAULT_TRIAL_POLICY
    assert spec == original


@pytest.mark.parametrize("effort,counts", [("rea-strict", (None, None, None, None)),
                                         (None, (3, 3, 3, True))])
def test_cli_overrides_sync_explicit_plan_then_reject_insufficient_rows(workflow, effort, counts):
    from anyuse.execution.rea_cli import _apply_cli_trial_overrides
    spec = copy.deepcopy(workflow.spec)
    spec["plan"]["trial_policy"] = {"preset": "fast", "restart": False}
    for role in ("learning", "verification", "heldout"):
        spec["plan"][role] = spec["plan"][role][:1]
        spec["plan"][role][0]["restart"] = False
    original = copy.deepcopy(spec)
    overridden = _apply_cli_trial_overrides(spec, effort, *counts)
    assert overridden["trial_policy"] == W.VC.DEFAULT_TRIAL_POLICY
    assert overridden["plan"]["trial_policy"] == W.VC.DEFAULT_TRIAL_POLICY
    assert spec == original
    with pytest.raises(ValueError, match="each phase needs 3"):
        W.task_contract(R.fingerprint(workflow.f.app), overridden)


def test_cli_restart_override_rejects_plan_without_restart(workflow):
    from anyuse.execution.rea_cli import _apply_cli_trial_overrides
    spec = copy.deepcopy(workflow.spec)
    spec["plan"]["trial_policy"] = {"preset": "strict", "restart": False}
    for row in spec["plan"]["heldout"]:
        row["restart"] = False
    overridden = _apply_cli_trial_overrides(spec, None, None, None, None, True)
    assert overridden["plan"]["trial_policy"]["require_restart"] is True
    with pytest.raises(ValueError, match="require a declared process restart"):
        W.task_contract(R.fingerprint(workflow.f.app), overridden)


@pytest.mark.parametrize("nested", [False, True])
def test_cli_overrides_normalize_existing_aliases(nested):
    from anyuse.execution.rea_cli import _apply_cli_trial_overrides
    plan = {"format": W.VC.PLAN,
            "trial_policy": {"learn": 1, "fresh": 2, "heldout": 1, "restart": False}}
    target = {"plan": plan} if nested else plan
    original = copy.deepcopy(target)
    out = _apply_cli_trial_overrides(target, None, 3, None, None, True)
    expected = {"min_learning": 3, "min_verification": 2, "min_heldout": 1, "require_restart": True}
    assert out["trial_policy"] == expected
    if nested:
        assert out["plan"]["trial_policy"] == expected
    assert target == original


@pytest.mark.parametrize("base", ["strict", {"preset": "strict"}, {"learn": 3, "restart": False}])
def test_cli_effort_replaces_existing_policy_base(base):
    from anyuse.execution.rea_cli import _apply_cli_trial_overrides
    target = {"trial_policy": base, "plan": {"trial_policy": copy.deepcopy(base)}}
    out = _apply_cli_trial_overrides(target, "rea-fast", None, 2, None, None)
    assert out["effort"] == "rea-fast"
    assert out["trial_policy"] == {"min_learning": 1, "min_verification": 2,
                                    "min_heldout": 1, "require_restart": True}
    assert out["plan"]["trial_policy"] == out["trial_policy"]


@pytest.mark.parametrize("effort", ["rea-fast", "rea-strict"])
def test_workflow_global_effort_is_frozen_before_reload(workflow, tmp_path, monkeypatch, effort):
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", effort)
    spec = {k: copy.deepcopy(v) for k, v in workflow.spec.items() if k not in {"params", "plan", "replays"}}
    root = tmp_path / "global-effort-workflow"
    _, direct_plan, _ = W.task_contract(R.fingerprint(workflow.f.app), spec)
    assert direct_plan["trial_policy"] == W.VC.validate_trial_policy(effort)
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    frozen = R.load_json(root / "spec.json")
    assert frozen["effort"] == effort and "effort" not in spec
    state = G._load(root / "session")
    assert state["native_contract_mode"] == "declared"
    assert state["native_receiver_mode"] == "binary_runtime_hypothesis"
    assert state["plan"]["trial_policy"] == W.VC.validate_trial_policy(effort)
    for changed in ("rea-fast", "rea-strict", "default"):
        monkeypatch.setenv("ANYUSE_MAP_EFFORT", changed)
        assert W.context(root)["status"] == "discovering"
        assert G._load(root / "session")["plan"] == state["plan"]
        assert R.load_json(root / "spec.json") == frozen


def test_default_creation_preserves_legacy_shape_and_ignores_later_global_effort(workflow, tmp_path, monkeypatch):
    spec = {k: copy.deepcopy(v) for k, v in workflow.spec.items() if k not in {"params", "plan", "replays"}}
    root = tmp_path / "default-effort-workflow"
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    assert R.load_json(root / "spec.json") == spec
    state = G._load(root / "session")
    assert "trial_policy" not in state["plan"]
    assert [len(state["plan"][r]) for r in ("learning", "verification", "heldout")] == [3, 3, 3]
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "rea-fast")
    assert W.context(root)["status"] == "discovering"
    assert G._load(root / "session")["plan"] == state["plan"]


def test_explicit_default_effort_overrides_global_rea_fast(workflow, tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "rea-fast")
    spec = {**copy.deepcopy(workflow.spec), "effort": "default"}
    root = tmp_path / "explicit-default-workflow"
    W.begin(workflow.f.app, G._load(workflow.root)["rea"], spec, root)
    state = G._load(root / "session")
    assert state.get("native_contract_mode", "attributable") == "attributable"
    assert state.get("native_receiver_mode") is None
    assert state["plan"]["trial_policy"] == W.VC.DEFAULT_TRIAL_POLICY


def test_partial_cli_counts_keep_global_fast_defaults(workflow, monkeypatch):
    from anyuse.execution.rea_cli import _apply_cli_trial_overrides
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "rea-fast")
    spec = {k: copy.deepcopy(v) for k, v in workflow.spec.items() if k not in {"params", "plan", "replays"}}
    out = _apply_cli_trial_overrides(spec, None, 2, None, None, None)
    assert out["trial_policy"] == {"min_learning": 2, "min_verification": 1,
                                    "min_heldout": 1, "require_restart": True}


def test_workflow_init_cli_writes_validated_spec_accepted_by_begin(workflow, tmp_path):
    from click.testing import CliRunner
    from anyuse.cli import cli
    runner = CliRunner()
    spec_path = tmp_path / "generated-spec.json"
    res = runner.invoke(cli, [
        "map", "rea", "workflow", "init", str(spec_path),
        "--task", "Select September in timeline picker",
        "--before", "The timeline month picker shows the initial month",
        "--after", "The timeline month picker shows September",
        "--window-title", "OwnedStudio",
        "--effort", "rea-fast",
        "--trials-learn", "2",
    ])
    assert res.exit_code == 0, res.output
    written = R.load_json(spec_path)
    assert written["id"] == "select_september_in_timeline_picker"
    assert written["effort"] == "rea-fast"
    assert written["trial_policy"] == {
        "min_learning": 2, "min_verification": 1, "min_heldout": 1, "require_restart": True
    }
    # Refuses to overwrite existing file
    dup = runner.invoke(cli, [
        "map", "rea", "workflow", "init", str(spec_path),
        "--task", "Select September", "--before", "Before", "--after", "After",
    ])
    assert dup.exit_code != 0 and "already exists" in dup.output
    # Accepted directly by W.begin
    out_root = tmp_path / "init-begun-workflow"
    ctx = W.begin(workflow.f.app, G._load(workflow.root)["rea"], written, out_root)
    assert ctx["status"] == "discovering"
    state = G._load(out_root / "session")
    assert state["native_receiver_mode"] == "binary_runtime_hypothesis"
    assert [len(state["plan"][r]) for r in ("learning", "verification", "heldout")] == [2, 1, 1]


def _init_observer_spec(tmp_path, *flags):
    from anyuse.cli import cli
    path = tmp_path / "observer-spec.json"
    result = CliRunner().invoke(cli, [
        "map", "rea", "workflow", "init", str(path),
        "--task", "Show the panel", "--before", "Panel hidden", "--after", "Panel visible",
        "--effort", "rea-fast", *flags,
    ])
    return result, path


@pytest.mark.parametrize("provider", ["local", "openai", "anthropic", "gemini"])
def test_workflow_init_observer_defaults_follow_selected_provider(tmp_path, monkeypatch, provider):
    from anyuse.execution import model_adapter as M
    monkeypatch.setattr(M, "read_model_api_key", lambda *_: pytest.fail("init must not read a credential"))
    # Discovery model settings must not silently redirect the explicitly selected observer.
    monkeypatch.setenv("ANYUSE_MODEL_BASE_URL", "https://unrelated.invalid/v1")
    monkeypatch.setenv("ANYUSE_MODEL_NAME", "unrelated-model")
    monkeypatch.setenv("ANYUSE_MODEL_API_KEY_ENV", "UNRELATED_KEY")
    flags = ["--observer-provider", provider]
    if provider != "local":
        flags += ["--observer-price-in", "1", "--observer-price-out", "2"]
    if provider == "openai":
        flags += ["--observer-model", "caller-vision-model"]
    result, path = _init_observer_spec(tmp_path, *flags)
    assert result.exit_code == 0, result.output
    config = R.load_json(path)["observer_model"]
    preset = M.PROVIDER_PRESETS[provider]
    expected_url = "http://127.0.0.1:11434/v1" if provider == "local" else preset.base_url
    expected_model = "llava" if provider == "local" else (
        "caller-vision-model" if provider == "openai" else preset.model)
    assert config["provider"] == provider
    assert config["base_url"] == expected_url
    assert config["model"] == expected_model
    assert config["api_key_env"] == preset.api_key_env
    assert M.ModelConfig(**config).endpoint == expected_url.rstrip("/") + "/chat/completions"
    assert config["retries"] == 0 and config["timeout"] == 30


@pytest.mark.parametrize("provider", ["local", "openai", "anthropic", "gemini", "openai_compatible"])
@pytest.mark.parametrize("key_env", ["CUSTOM_OBSERVER_KEY", ""])
def test_workflow_init_preserves_explicit_observer_overrides(tmp_path, provider, key_env):
    result, path = _init_observer_spec(tmp_path,
        "--observer-provider", provider, "--observer-base-url", "https://observer.invalid/v1",
        "--observer-model", "custom-vision", "--observer-api-key-env", key_env,
        "--observer-price-in", "1", "--observer-price-out", "2")
    assert result.exit_code == 0, result.output
    config = R.load_json(path)["observer_model"]
    assert {key: config[key] for key in ("provider", "base_url", "model", "api_key_env")} == {
        "provider": provider, "base_url": "https://observer.invalid/v1",
        "model": "custom-vision", "api_key_env": key_env,
    }


@pytest.mark.parametrize("flags,missing", [
    (["--observer-provider", "openai"], "--observer-model"),
    (["--observer-provider", "openai_compatible", "--observer-model", "vision"], "--observer-base-url"),
    (["--observer-provider", "openai_compatible", "--observer-base-url", "http://127.0.0.1:8000/v1"], "--observer-model"),
])
def test_workflow_init_requires_unknown_provider_defaults_before_writing(tmp_path, flags, missing):
    result, path = _init_observer_spec(tmp_path, *flags)
    assert result.exit_code != 0 and missing in result.output
    assert not path.exists()


def test_workflow_init_custom_observer_can_be_unauthenticated(tmp_path):
    result, path = _init_observer_spec(tmp_path,
        "--observer-provider", "openai_compatible", "--observer-base-url", "http://127.0.0.1:8000/v1",
        "--observer-model", "local-vision")
    assert result.exit_code == 0, result.output
    assert R.load_json(path)["observer_model"]["api_key_env"] == ""



def test_hosted_observer_prices_required_before_writing(tmp_path):
    result, path = _init_observer_spec(tmp_path, "--observer-provider", "openai",
                                        "--observer-model", "custom-vision")
    assert result.exit_code != 0 and "price_in" in result.output
    assert not path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
