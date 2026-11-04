"""Model choice may name a verified recipe, never mint or bypass its proof."""
import copy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_rea_harness import fixture, runtime, learn, cases, objc_actor, _H_RUNTIME
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_actions as A
from anyuse.execution import rea_harness as H
from anyuse.execution import openjev as O

_REAL_REPLAY = R.replay


@pytest.fixture
def fixed_state_campaign(fixture, monkeypatch):
    """Inert campaign boundary; proof semantics are tested by its own suite."""
    from anyuse import execution
    calls = []
    candidate = {"format": R.CANDIDATE, "backend": H.STATE_BACKEND, "id": "pause_playback",
                 "label": "Pause playback", "target": R.fingerprint(fixture[0]),
                 "effect": "fixed_state_transition", "params": [],
                 "actor": {"kind": "objc_method"},
                 "policy": {"id": "independent_playback_transition"},
                 "scope": {"actor_inputs": {}, "input_generalization_claimed": False}}
    proof = {"format": R.PROOF, "backend": H.STATE_BACKEND, "status": "verified",
             "candidate_sha256": R.digest(candidate)}

    def validate(value):
        calls.append(("validate", value))
        assert value == candidate
        return copy.deepcopy(value)

    def bind(value, inputs):
        calls.append(("bind", inputs))
        if inputs != {}:
            raise ValueError("fixed state tasks require empty actor inputs")
        return copy.deepcopy(value["actor"])

    def proof_status(value, evidence, current):
        calls.append(("proof_status", evidence))
        ready = evidence == proof and current == candidate["target"]
        return ready, "measured campaign" if ready else "no complete campaign proof"

    def learn(*args):
        calls.append(("learn", args))
        return copy.deepcopy(candidate)

    def verify(*args):
        calls.append(("verify", args))
        return copy.deepcopy(proof)

    def replay(*args):
        calls.append(("replay", args))
        return {"verdict": "verified", "model_calls": 0}

    campaign = SimpleNamespace(PROFILE="anyuse.rea-fixed-state-profile/1", validate=validate, bind=bind,
                               proof_status=proof_status, learn=learn, verify=verify, replay=replay)
    monkeypatch.setitem(sys.modules, "anyuse.execution.rea_state_campaign", campaign)
    monkeypatch.setattr(execution, "rea_state_campaign", campaign, raising=False)
    return candidate, proof, calls, campaign


def test_fixed_state_dispatch_keeps_empty_inputs_outside_legacy_input_hash_rules(fixed_state_campaign, fixture, tmp_path):
    candidate, proof, calls, campaign = fixed_state_campaign
    app = str(fixture[0])
    assert R.validate_candidate(candidate) == candidate
    assert R.bind(candidate, {}) == candidate["actor"]
    assert R.proof_status(candidate, proof, candidate["target"])[0]
    assert R.verify(candidate, [{"trial": "retained"}], app, str(tmp_path / "verify")) == proof
    assert R.replay(candidate, proof, {}, app, str(tmp_path / "replay"))["verdict"] == "verified"
    assert H.learn({"format": campaign.PROFILE}, [], app, str(tmp_path / "learn")) == candidate
    assert {name for name, _ in calls} == {"validate", "bind", "proof_status", "verify", "replay", "learn"}
    surface = SimpleNamespace(invoke={"candidate": candidate, "proof": proof, "target": candidate["target"]})
    status, why, bindings = R.map_bindings(surface, None)
    assert status == "uncertain" and "catalog replay" in why and bindings == {}
    with pytest.raises(ValueError, match="empty actor inputs"):
        R.bind(candidate, {"trial_id": "does-not-create-an-actor-input"})
    with pytest.raises(ValueError, match="typed task parameters"):
        H._params([])


def test_fixed_state_card_binds_policy_scope_and_catalog_replay(fixed_state_campaign, fixture, tmp_path):
    candidate, proof, calls, _ = fixed_state_campaign
    app = str(fixture[0])
    card = A.describe(candidate, proof, app, "Pause the current playing video",
                      "Verify the independent playback transition in its measured scope.", str(tmp_path / "state-card"))
    assert card["contract"]["params"] == []
    assert card["contract"]["policy"] == candidate["policy"]
    assert card["contract"]["scope"] == candidate["scope"]
    paths = {name: str(tmp_path / (name + ".json")) for name in ("candidate", "proof")}
    R.write_json(paths["candidate"], candidate)
    R.write_json(paths["proof"], proof)
    entry = {**paths, "card": str(tmp_path / "state-card/action.json"), "bundle": app}
    catalog = A.catalog([entry], str(tmp_path / "state-catalog"))
    seen = []
    selection = A.select(catalog, "Pause playback", client=client({"choice": "a0", "confidence": .95}, seen=seen))
    assert '"policy"' in seen[0]["state"] and '"input_generalization_claimed": false' in seen[0]["state"]
    calls.clear()
    with pytest.raises(ValueError, match="empty actor inputs"):
        A.execute(catalog, selection, {"trial_id": "unmeasured"}, str(tmp_path / "rejected-replay"))
    assert not any(name == "replay" for name, _ in calls)
    assert A.execute(catalog, selection, {}, str(tmp_path / "state-replay"))["verdict"] == "verified"
    card["contract"]["policy"] = {"id": "a-different-transition"}
    R.write_json(entry["card"], card)
    changed = A.catalog([entry], str(tmp_path / "changed-state-catalog"))
    assert not changed["actions"] and "contract changed" in changed["excluded"][0]["why"]


def test_fixed_state_description_cannot_bypass_campaign_proof(fixed_state_campaign, fixture, tmp_path):
    candidate, _, _, _ = fixed_state_campaign
    out = tmp_path / "unproved-state-card"
    with pytest.raises(ValueError, match="complete campaign proof"):
        A.describe(candidate, {"status": "verified"}, str(fixture[0]), "Pause", "An asserted result", str(out))
    assert not out.exists()


@pytest.fixture
def catalog(fixture, runtime, tmp_path):
    app = fixture[0]
    c = learn(fixture, tmp_path)
    proof = R.verify(c, cases(fixture), str(app), str(tmp_path / "verify"))
    card = A.describe(c, proof, str(app), "Read a complete result for an explicit query",
                      "Look up the requested query; verify all result content against its independent reference.", str(tmp_path / "card"))
    R.write_json(tmp_path / "candidate.json", c)
    entry = {"candidate": str(tmp_path / "candidate.json"), "proof": str(tmp_path / "verify/proof.json"),
             "card": str(tmp_path / "card/action.json"), "bundle": str(app)}
    cat = A.catalog([entry], str(tmp_path / "catalog"))
    assert len(cat["actions"]) == 1
    return cat, c, proof, card


def client(answer=None, error=None, seen=None):
    def post(url, body, headers, timeout):
        if seen is not None:
            seen.append(json.loads(body))
        if error:
            raise TimeoutError(error)
        return 200, {}, json.dumps({"answers": {"pick": answer}}).encode()
    return O.Client(url="https://example.invalid", key="", poster=post)


@pytest.fixture
def registered_catalog(execution_catalog, tmp_path, monkeypatch):
    from anyuse.execution.rea_catalog import CatalogStore
    from anyuse.execution import rea_runtime_bridge as B
    from anyuse.execution import foreground_interlock as F
    from test_rea_harness import native_context_identity
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")  # Only the inert replay below is dispatched.
    cat, candidate, _, _ = execution_catalog
    identity = native_context_identity(Path(cat["actions"][0]["entry"]["bundle"]), candidate)
    monkeypatch.setattr(B, "process_identity", lambda *a: copy.deepcopy(identity))
    monkeypatch.setattr(F, "require_clear", lambda where: None)  # Inert guest fixture; no live session is accessed.
    store = CatalogStore(tmp_path / "registered.json")
    specs = store.register("example", tmp_path / "catalog/catalog.json")
    calls = []
    def replay(c, proof, inputs, bundle, out, **kwargs):
        calls.append({"inputs": inputs, "kwargs": kwargs})
        return {"verdict": "verified", "model_calls": 0, "retained_evidence": ["ordinary-replay"]}
    monkeypatch.setattr(R, "replay", replay)
    return store, specs[0], calls, identity


def test_registered_catalog_empty_is_inert(tmp_path, monkeypatch):
    from anyuse.execution.rea_catalog import CatalogStore
    monkeypatch.setattr(R, "assert_guest", lambda: pytest.fail("empty registry must not probe the desktop"))
    store = CatalogStore(tmp_path / "absent/registrations.json")
    assert store.list_tools() == []
    assert not store.path.parent.exists()


def test_registered_catalog_rejects_non_identity_bound_actor_before_advertising(catalog, tmp_path, monkeypatch):
    from anyuse.execution.rea_catalog import CatalogStore
    from anyuse.execution import rea_runtime_bridge as B
    assert catalog[1]["actor"]["kind"] == "apple_event"
    monkeypatch.setattr(B, "process_identity", lambda *args: pytest.fail("registration must not inspect a process"))
    store = CatalogStore(tmp_path / "unsupported/registrations.json")
    with pytest.raises(ValueError, match="process-identity binding"):
        store.register("unsupported", tmp_path / "catalog/catalog.json")
    assert store.list_tools() == [] and not store.path.exists()


def test_registered_failure_retains_replay_receipt(registered_catalog, fixture, monkeypatch):
    store, spec, calls, _ = registered_catalog
    receipt = {"verdict": "refused", "state_uncertain": True, "error": "inert receiver became unavailable"}
    def interrupted(candidate, proof, inputs, bundle, out, **kwargs):
        Path(out).mkdir(parents=True)
        R.write_json(Path(out) / "replay.json", receipt)
        raise ValueError(receipt["error"])
    monkeypatch.setattr(R, "replay", interrupted)
    result = store.execute(spec["name"], fixture[3][0]["inputs"])
    assert result["status"] == "uncertain" and result["result"] == receipt
    assert result["terminal_on_failure"] is True and result["automatic_retry"] is False
    assert not calls


def test_direct_visual_execution_cli_defaults_cover_both_observer_calls():
    from anyuse.execution.rea_cli import rea_commands
    from anyuse.execution import rea_visual_effect as V
    minimum = 2 * 30 + 2 * V.V.CAPTURE_TIMEOUT + V.AFTER_VALIDATION_SECONDS + V.MIN_DISPATCH_SECONDS
    for command in ("execute", "replay"):
        timeout = next(param.default for param in rea_commands.commands[command].params if param.name == "timeout")
        assert timeout >= minimum


def test_registered_catalog_persists_and_executes_through_ordinary_gate(registered_catalog, fixture):
    from anyuse.execution.rea_catalog import CatalogStore
    store, spec, calls, identity = registered_catalog
    reopened = CatalogStore(store.path)
    assert reopened.list_tools() == [spec]
    assert spec["input_schema"]["additionalProperties"] is False
    inputs = fixture[3][0]["inputs"]
    result = reopened.execute(spec["name"], inputs)
    assert result["ok"] and result["result"]["retained_evidence"] == ["ordinary-replay"]
    assert result["verification_tier"] == "deterministic" and result["model_calls"] == 0
    assert calls == [{"inputs": inputs, "kwargs": {"timeout": 300, "expected_identity": identity}}]


@pytest.mark.parametrize("changed", ["candidate", "proof", "card", "catalog", "app"])
def test_registered_catalog_mutation_refuses_before_replay(registered_catalog, execution_catalog, fixture, changed):
    store, spec, calls, _ = registered_catalog
    cat = execution_catalog[0]
    if changed == "catalog":
        path = Path(store._load()["registrations"]["example"]["catalog"])
    elif changed == "app":
        path = Path(cat["actions"][0]["entry"]["bundle"]) / "Contents/Resources/added.txt"
        path.parent.mkdir(exist_ok=True)
    else:
        path = Path(cat["actions"][0]["entry"][changed])
    with path.open("a") as stream:
        stream.write(" changed")
    result = store.execute(spec["name"], fixture[3][0]["inputs"])
    assert result["status"] == "refused" and result["model_calls"] == 0 and not calls
    assert result["output_dir"] is None


def test_registered_catalog_restoration_is_fresh_and_cleanup_result_retained(registered_catalog, fixture, monkeypatch):
    store, spec, calls, identity = registered_catalog
    value = store._load()
    value["registrations"]["example"]["require_restoration"] = True
    R.write_json(store.path, value)
    inputs = fixture[3][0]["inputs"]
    result = store.execute(spec["name"], inputs)
    assert result["status"] == "refused" and "fresh" in result["reason"] and not calls
    provided = []
    declaration = {"identity": identity, "workspace": "/inert", "fields": [], "seconds": 1}
    store.restoration_provider = lambda *args: provided.append(args) or copy.deepcopy(declaration)
    incomplete = {"completion_status": "cleanup_incomplete", "verification_tier": "deterministic",
                  "operation": {"result": {"verdict": "verified", "model_calls": 0}},
                  "restoration": {"status": "failed", "error": "inert cleanup failure", "receipt": {"kept": True}}}
    def execute(*args, **kwargs):
        assert kwargs["restoration"] == declaration and kwargs["expected_identity"] == identity
        return copy.deepcopy(incomplete)
    monkeypatch.setattr(A, "execute", execute)
    for _ in range(2):
        result = store.execute(spec["name"], inputs)
        assert not result["ok"] and result["status"] == "uncertain" and result["result"] == incomplete
        assert result["terminal_on_failure"] and result["automatic_retry"] is False
    assert len(provided) == 2


def test_registered_catalog_rejects_inherited_host_permission_before_app_io(registered_catalog, fixture, monkeypatch):
    store, spec, calls, _ = registered_catalog
    monkeypatch.setattr(R, "host_allowed", lambda: True)
    monkeypatch.setattr(A, "_actions", lambda *a: pytest.fail("must refuse before app/evidence IO"))
    result = store.execute(spec["name"], fixture[3][0]["inputs"])
    assert result["status"] == "refused" and "--allow-host" in result["reason"] and not calls


def test_registered_catalog_wrong_inputs_and_busy_registry_never_dispatch(registered_catalog):
    store, spec, calls, _ = registered_catalog
    assert store.execute(spec["name"], {"undeclared": "value"})["status"] == "refused"
    with store._lock():
        assert store.execute(spec["name"], {})["status"] == "refused"
    assert not calls


def test_registered_result_does_not_promote_uncertainty_or_model_judgments():
    from anyuse.execution.rea_catalog import accepted
    result = {"verdict": "model_judged", "verification_tier": "model_judged", "model_calls": 2}
    assert accepted(result)
    assert not accepted(dict(result, resource_review_required=True))
    assert not accepted(dict(result, state_uncertain=True))
    assert not accepted({"verdict": "model_judged"})


def test_registered_cli_persists_removes_and_rejects_host_override(execution_catalog, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution.rea_cli import rea_commands
    from anyuse.execution.rea_catalog import CatalogStore
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.setattr(R, "_HOST_ALLOWED", False)
    runner = CliRunner()
    added = runner.invoke(rea_commands, ["registered", "add", "cli", str(tmp_path / "catalog/catalog.json")])
    assert added.exit_code == 0, added.output
    assert CatalogStore().list_tools()[0]["name"].startswith("rea.cli.")
    listed = runner.invoke(rea_commands, ["registered", "list"])
    assert listed.exit_code == 0 and json.loads(listed.output) == json.loads(added.output)
    removed = runner.invoke(rea_commands, ["registered", "remove", "cli"])
    assert removed.exit_code == 0 and CatalogStore().list_tools() == []
    # Re-add while genuinely in the fixture VM, then verify a process-global
    # CLI host override never carries into registered-action execution.
    assert runner.invoke(rea_commands, ["registered", "add", "cli", str(tmp_path / "catalog/catalog.json")]).exit_code == 0
    refused = runner.invoke(rea_commands, ["--allow-host", "registered", "list"])
    assert refused.exit_code != 0 and "inherited --allow-host" in refused.output


def test_registered_paths_never_create_registry_or_output_inside_app(registered_catalog, execution_catalog, fixture, tmp_path):
    from anyuse.execution.rea_catalog import CatalogStore
    store, spec, calls, _ = registered_catalog
    app = Path(execution_catalog[0]["actions"][0]["entry"]["bundle"])
    before = R.fingerprint(app)
    unsafe = CatalogStore(app / "registration/registrations.json")
    with pytest.raises(ValueError, match="outside app bundles"):
        unsafe.register("unsafe", tmp_path / "catalog/catalog.json")
    assert not unsafe.path.parent.exists()
    store.output_dir = app / "execution-records"
    refused = store.execute(spec["name"], fixture[3][0]["inputs"])
    assert refused["status"] == "refused" and not store.output_dir.exists() and not calls
    assert R.fingerprint(app) == before


def test_agent_description_cannot_promote_an_unverified_candidate(fixture, tmp_path):
    c = learn(fixture, tmp_path)
    with pytest.raises(ValueError, match="receipt"):
        A.describe(c, {"verified": True}, str(fixture[0]), "Do anything", "Claimed description", str(tmp_path / "card"))
    assert not (tmp_path / "card").exists()


def test_typed_wire_contains_reviewed_description_and_contract_but_no_execution(catalog, runtime):
    cat, c, _, _ = catalog
    seen = []
    before = len(runtime.sent)
    selection = A.select(cat, "Read q3", client=client({"choice": "a0", "confidence": .95}, seen=seen))
    assert selection["status"] == "selected" and selection["action"] == c["id"]
    assert selection["actuated"] is False and len(runtime.sent) == before
    payload = seen[0]
    assert payload["questions"]["pick"]["type"] == "choice"
    assert "none" in payload["questions"]["pick"]["criteria"]
    assert "independent reference" in payload["state"] and '"effect": "scoped_find"' in payload["state"]
    assert "proof_sha256" not in payload["state"]


@pytest.mark.parametrize("answer", [
    {"choice": "invented_function", "confidence": .99},
    {"choice": "a0", "confidence": .2},
    {"choice": "a0", "confidence": float("nan")},
    {"choice": "a0", "confidence": True},
    {"choice": "none", "confidence": .99}, None,
])
def test_unusable_choices_abstain_without_actuation(catalog, runtime, answer):
    before = len(runtime.sent)
    result = A.select(catalog[0], "Unsupported task", client=client(answer))
    assert result["status"] == "abstained" and result["action"] is None
    assert len(runtime.sent) == before


def test_missing_endpoint_and_timeout_abstain(catalog, runtime):
    before = len(runtime.sent)
    missing = A.select(catalog[0], "Read", client=O.Client(url="", key=""))
    timeout = A.select(catalog[0], "Read", client=client(error="timeout"))
    assert missing["model_calls"] == 0 and timeout["model_calls"] == 1
    assert missing["status"] == timeout["status"] == "abstained"
    assert len(runtime.sent) == before


def test_djev_uses_its_actual_request_protocol_without_changing_openjev(catalog):
    from anyuse.execution.rea_picker import DjevClient
    seen = []
    def post(url, body, headers, timeout):
        seen.append((url, json.loads(body), headers))
        return 200, {}, json.dumps({"model": "djev-0.1", "answers": {
            "pick": {"type": "choice", "choice": "a0", "confidence": .95}},
            "usage": {"input_tokens": 240, "output_tokens": 8}}).encode()
    result = A.select(catalog[0], "Read a result", client=DjevClient(url="http://127.0.0.1:8000", poster=post, key=""))
    assert result["status"] == "selected"
    assert seen[0][0] == "http://127.0.0.1:8000/v1/request"
    assert "model" not in seen[0][1] and seen[0][1]["options"] == {"seed": 0, "samples": 1}
    assert seen[0][1]["questions"]["pick"]["type"] == "choice"
    assert O.PATH == "/v1/systemone"


def test_selected_recipe_uses_real_proof_gate_and_full_result(catalog, fixture, runtime, tmp_path):
    cat = catalog[0]
    selection = A.select(cat, "Read q3", client=client({"choice": "a0", "confidence": .99}))
    result = A.execute(cat, selection, fixture[2][3], str(tmp_path / "selected-replay"))
    assert result["verdict"] == "verified"
    assert runtime.sent[-1] == fixture[2][3]
    runtime.wrong = True
    result = A.execute(cat, selection, fixture[2][4], str(tmp_path / "wrong-replay"), timeout=1)
    assert result["verdict"] == "refuted" and result["retry"] is False


@pytest.fixture
def execution_catalog(fixture, runtime, tmp_path):
    for demo in fixture[3]:
        demo["invocation"] = objc_actor(demo["inputs"]["query"], demo["inputs"]["reference"])
    return catalog.__wrapped__(fixture, runtime, tmp_path)


@pytest.fixture
def execution_recovery(execution_catalog, tmp_path, monkeypatch):
    """Real saved-copy recovery with explicitly inert public-state adapters."""
    from anyuse.execution import rea_document_restoration as D
    from anyuse.execution import rea_runtime_bridge as B
    monkeypatch.setattr(D.os, "statvfs", lambda path: SimpleNamespace(f_bavail=16*1024**3, f_frsize=1))
    source = tmp_path / "restoration-source.txt"
    source.write_bytes(b"before replay\n")
    workspace = D.create_workspace(D.pin(source), tmp_path / "execution-workspace")
    document = Path(json.loads(D._verified(workspace))["document"]["file"])
    bundle = Path(execution_catalog[0]["actions"][0]["entry"]["bundle"])
    identity = {"bundle": str(bundle), "bundle_id": execution_catalog[1]["target"]["bundle_id"],
                "executable": str(bundle / "Contents/MacOS/Unseen"), "launch_time": 1000.25,
                "pid": 443, "uid": os.getuid()}
    env = SimpleNamespace(identity=identity, open=True, edited=False, value=.5, calls=[], fail_close=False,
                          document=document, workspace=workspace, source=source, dispatched=0)
    field = {"id": "level", "path": [0], "role": "AXSlider", "identifier": "fixture-level", "restore": True}
    def observe(context):
        env.calls.append("observe")
        return {"identity": copy.deepcopy(env.identity), "document": str(document), "open": env.open,
                "edited": env.edited if env.open else None,
                "window": {"position": [10., 20.], "size": [400., 300.]} if env.open else None,
                "fields": {"level": {"value": env.value, "settable": True}} if env.open else {}}
    def close(context):
        env.calls.append("close")
        if env.fail_close: raise ValueError("inert close refused")
        env.open = False
    def reopen(context):
        env.calls.append("reopen")
        env.open = True
    def set_field(context, field, value):
        env.calls.append("set:" + field["id"])
        if field["id"] == "level": env.value = value
    producer = tmp_path / "inert-execution-document-adapter.py"
    producer.write_text("# inert callbacks; never imported\n")
    env.backend = D.TrustedHooks(observe, close, reopen, set_field,
        {name: D.pin(producer) for name in ("observe", "close", "reopen", "set_field")})
    env.declaration = {"workspace": workspace, "identity": copy.deepcopy(identity), "fields": [field], "seconds": 10}
    monkeypatch.setattr(B, "process_identity", lambda *args: copy.deepcopy(env.identity))
    def replay(*args, **kwargs):
        env.dispatched += 1
        assert kwargs["expected_identity"] == env.declaration["identity"]
        document.write_bytes(b"saved replay edit\n")
        env.value = .9
        return {"verdict": "verified", "model_calls": 0}
    env.replay = replay
    monkeypatch.setattr(R, "replay", replay)
    return env


def _recovery_selection(catalog):
    cat = catalog[0]
    action = cat["actions"][0]
    return {"format": A.SELECTION, "catalog_sha256": R.digest(cat), "status": "selected",
            "action": action["id"], "candidate_sha256": action["candidate_sha256"], "card_sha256": action["card_sha256"]}


def test_execute_fresh_restoration_recovers_saved_copy_and_retains_separate_results(execution_catalog, execution_recovery, fixture, tmp_path):
    from anyuse.execution import rea_document_restoration as D
    env = execution_recovery
    out = tmp_path / "restored-execution"
    result = A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(out),
                       restoration=env.declaration, restoration_backend=env.backend)
    assert env.dispatched == 1 and env.document.read_bytes() == env.source.read_bytes() and env.value == .5
    assert result["operation"]["result"] == {"verdict": "verified", "model_calls": 0}
    assert result["completion_status"] == "completed" and result["restoration"]["status"] == "restored"
    assert D.verify_restored(result["checkpoint"], result["restoration"]["receipt"])["status"] == "restored"
    assert json.loads((out / "execution.json").read_text()) == result
    assert result["checkpoint"]["file"] == str(out / "checkpoint/checkpoint.json")


def test_catalog_restoration_cannot_be_skipped_at_direct_execution(execution_catalog, execution_recovery, fixture, tmp_path):
    env = execution_recovery
    first = A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3],
                      str(tmp_path / "restoration-witness"), restoration=env.declaration, restoration_backend=env.backend)
    recovery = {"checkpoint": first["checkpoint"], "receipt": first["restoration"]["receipt"]}
    A.describe(execution_catalog[1], execution_catalog[2], str(fixture[0]), "Read result",
               "Read and restore the measured saved document.", str(tmp_path / "required-card"), restoration=recovery)
    entry = {**execution_catalog[0]["actions"][0]["entry"], "card": str(tmp_path / "required-card/action.json")}
    required = A.catalog([entry], str(tmp_path / "required-catalog"), restoration=recovery)
    env.calls.clear()
    before, dispatched = env.document.read_bytes(), env.dispatched
    out = tmp_path / "unrestored-execution"
    with pytest.raises(ValueError, match="requires a fresh execution-restoration declaration"):
        A.execute(required, _recovery_selection((required,)), fixture[2][4], str(out), expected_identity=env.identity)
    assert env.dispatched == dispatched and not env.calls and not out.exists()
    assert env.document.read_bytes() == before


def test_model_judged_restored_execution_preserves_tier_and_completes_cleanup(execution_catalog, execution_recovery,
                                                                           fixture, tmp_path, monkeypatch):
    env = execution_recovery
    candidate = copy.deepcopy(R.load_json(execution_catalog[0]["actions"][0]["entry"]["candidate"]))
    candidate["backend"] = H.VISUAL_BACKEND
    def replay(*args, **kwargs):
        env.replay(*args, **kwargs)
        return {"verdict": "model_judged", "verification_tier": "model_judged", "model_calls": 2}
    monkeypatch.setattr(R, "replay", replay)
    result = A._execute_restored(candidate, {}, str(fixture[0]), fixture[2][3],
        str(tmp_path / "model-judged-restored"), 30, env.declaration, env.backend)
    assert result["completion_status"] == "completed" and result["verification_tier"] == "model_judged"
    assert result["operation"]["result"]["verdict"] == "model_judged"
    assert result["operation"]["result"]["model_calls"] == 2
    assert result["restoration"]["status"] == "restored" and env.dispatched == 1
    assert env.document.read_bytes() == env.source.read_bytes() and env.value == .5


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_execute_primary_exception_survives_attempted_cleanup(execution_catalog, execution_recovery, fixture, monkeypatch, tmp_path, cleanup_failure):
    env = execution_recovery
    original = ValueError("primary replay failed")
    def replay(*args, **kwargs):
        env.replay(*args, **kwargs)
        raise original
    monkeypatch.setattr(R, "replay", replay)
    env.fail_close = cleanup_failure
    out = tmp_path / "failed-execution"
    with pytest.raises(ValueError) as raised:
        A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(out),
                  restoration=env.declaration, restoration_backend=env.backend)
    assert raised.value is original and env.dispatched == 1 and "close" in env.calls
    report = json.loads((out / "execution.json").read_text())
    assert report["operation"]["status"] == "raised" and report["completion_status"] == "primary_failed"
    assert report["restoration"]["status"] == ("unknown" if cleanup_failure else "restored")
    assert (out / "restoration/result.json").exists()


@pytest.mark.parametrize("change", ["unknown_key", "bool_seconds", "unsaved", "stale_epoch", "wrong_target", "capture_error"])
def test_execute_restoration_preflight_refuses_without_replay(execution_catalog, execution_recovery, fixture, monkeypatch, tmp_path, change):
    from anyuse.execution import rea_document_restoration as D
    env = execution_recovery
    if change == "unknown_key": env.declaration["checkpoint"] = {"historical": True}
    elif change == "bool_seconds": env.declaration["seconds"] = True
    elif change == "unsaved": env.edited = True
    elif change == "stale_epoch": env.identity["launch_time"] += 1
    elif change == "wrong_target": env.declaration["identity"]["bundle_id"] = "another.target"
    else:
        def fail(*args, **kwargs): raise ValueError("capture failed")
        monkeypatch.setattr(D, "capture", fail)
    out = tmp_path / "refused-execution"
    with pytest.raises(ValueError):
        A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(out),
                  restoration=env.declaration, restoration_backend=env.backend)
    assert env.dispatched == 0 and "close" not in env.calls and "reopen" not in env.calls
    assert env.document.read_bytes() == env.source.read_bytes()
    if out.exists():
        report = json.loads((out / "execution.json").read_text())
        assert report["operation"]["status"] == "not_dispatched" and report["completion_status"] == "preflight_failed"


def test_execute_uncertain_worker_does_not_dispatch_restoration_actions(execution_catalog, execution_recovery, fixture, monkeypatch, tmp_path):
    env = execution_recovery
    original = R.ResourceReviewRequired("worker detach unknown")
    def replay(*args, **kwargs):
        env.replay(*args, **kwargs)
        raise original
    monkeypatch.setattr(R, "replay", replay)
    out = tmp_path / "uncertain-execution"
    with pytest.raises(R.ResourceReviewRequired) as raised:
        A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(out),
                  restoration=env.declaration, restoration_backend=env.backend)
    assert raised.value is original and "close" not in env.calls and "reopen" not in env.calls
    report = json.loads((out / "execution.json").read_text())
    assert report["restoration"]["status"] == "unknown" and not (out / "restoration").exists()


def test_real_harness_retention_failure_preserves_uncertainty_and_primary_exception(execution_catalog, execution_recovery, fixture, monkeypatch, tmp_path):
    env = execution_recovery
    primary = R.ResourceReviewRequired("native worker detachment unknown")
    monkeypatch.setattr(R, "replay", _REAL_REPLAY)
    monkeypatch.setattr(H, "Runtime", lambda *args, **kwargs: SimpleNamespace())
    def uncertain(*args, **kwargs): raise primary
    monkeypatch.setattr(H, "_trial", uncertain)
    write = R.write_json
    def retain(path, value):
        if Path(path).name == "replay.json": raise OSError("replay receipt storage failed")
        return write(path, value)
    monkeypatch.setattr(R, "write_json", retain)
    out = tmp_path / "uncertain-retention"
    with pytest.raises(R.ResourceReviewRequired) as raised:
        A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(out),
                  restoration=env.declaration, restoration_backend=env.backend)
    assert raised.value is primary and isinstance(primary.__cause__, OSError)
    assert "close" not in env.calls and "reopen" not in env.calls
    report = json.loads((out / "execution.json").read_text())
    assert report["operation"]["error"]["type"] == "ResourceReviewRequired"
    assert report["restoration"]["status"] == "unknown"


@pytest.mark.parametrize("retention_failure", [False, True])
def test_interruption_after_real_native_dispatch_suppresses_document_actions(execution_catalog, execution_recovery, fixture, runtime, monkeypatch, tmp_path, retention_failure):
    from anyuse.execution import rea_runtime_bridge as B
    env = execution_recovery
    primary = KeyboardInterrupt("native worker interrupted after dispatch")
    dispatched = []
    monkeypatch.setattr(R, "replay", _REAL_REPLAY)
    monkeypatch.setattr(H, "Runtime", _H_RUNTIME)
    monkeypatch.setattr(H.AE, "Runtime", lambda *args, **kwargs: SimpleNamespace(
        pid=env.identity["pid"], _ready=lambda: None, read=lambda **options: copy.deepcopy(runtime.initial)))
    adapter = B.adapter_identity()
    monkeypatch.setattr(B, "adapter_identity", lambda **kwargs: {**copy.deepcopy(adapter), "transport": {"kind": "local"}})
    def interrupted(argv, *args, **kwargs):
        dispatched.append(R.load_json(argv[-1]))
        raise primary
    monkeypatch.setattr(R, "_bounded_run", interrupted)
    if retention_failure:
        write = R.write_json
        def retain(path, value):
            if Path(path).name == "replay.json": raise OSError("interrupted replay receipt storage failed")
            return write(path, value)
        monkeypatch.setattr(R, "write_json", retain)
    out = tmp_path / "dispatched-interruption"
    with pytest.raises(KeyboardInterrupt) as raised:
        A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(out),
                  restoration=env.declaration, restoration_backend=env.backend)
    assert raised.value is primary and len(dispatched) == 1
    assert dispatched[0]["identity"] == env.identity and "close" not in env.calls and "reopen" not in env.calls
    report = json.loads((out / "execution.json").read_text())
    assert report["native_worker"] == {"started": True, "cleanup_confirmed": False, "diagnostic_only": True}
    assert report["restoration"]["status"] == "unknown"


def test_execute_cleanup_failure_keeps_verified_primary_result(execution_catalog, execution_recovery, fixture, tmp_path):
    env = execution_recovery
    env.fail_close = True
    result = A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(tmp_path / "cleanup-failed"),
                       restoration=env.declaration, restoration_backend=env.backend)
    assert result["operation"]["result"]["verdict"] == "verified"
    assert result["restoration"]["status"] == "unknown" and result["completion_status"] == "cleanup_incomplete"


@pytest.mark.parametrize("verdict", ["refuted", "uncertain"])
def test_execute_nonverified_primary_remains_failed_with_incomplete_cleanup(execution_catalog, execution_recovery, fixture, monkeypatch, tmp_path, verdict):
    env = execution_recovery
    env.fail_close = True
    def replay(*args, **kwargs):
        return {**env.replay(*args, **kwargs), "verdict": verdict}
    monkeypatch.setattr(R, "replay", replay)
    result = A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3],
                       str(tmp_path / "nonverified-cleanup"), restoration=env.declaration, restoration_backend=env.backend)
    assert result["operation"]["result"]["verdict"] == verdict and result["completion_status"] == "primary_failed"
    assert result["restoration"]["status"] == "unknown" and "close" in env.calls


def test_execute_epoch_change_during_capture_refuses_before_replay(execution_catalog, execution_recovery, fixture, monkeypatch, tmp_path):
    from anyuse.execution import rea_document_restoration as D
    env = execution_recovery
    capture = D.capture
    def changed(*args, **kwargs):
        checkpoint = capture(*args, **kwargs)
        env.identity["launch_time"] += 1
        return checkpoint
    monkeypatch.setattr(D, "capture", changed)
    with pytest.raises(ValueError, match="epoch changed"):
        A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], str(tmp_path / "capture-epoch-change"),
                  restoration=env.declaration, restoration_backend=env.backend)
    assert env.dispatched == 0 and "close" not in env.calls


def test_execute_without_restoration_keeps_original_result_and_output(execution_catalog, execution_recovery, fixture, monkeypatch, tmp_path):
    from anyuse.execution import rea_document_restoration as D
    seen = []
    result = {"verdict": "verified", "original": True}
    monkeypatch.setattr(R, "replay", lambda *args, **kwargs: (seen.append((args, kwargs)), result)[1])
    def forbidden(*args, **kwargs): raise AssertionError("default execution must not capture")
    monkeypatch.setattr(D, "capture", forbidden)
    out = str(tmp_path / "ordinary-output")
    assert A.execute(execution_catalog[0], _recovery_selection(execution_catalog), fixture[2][3], out, timeout=7) is result
    assert seen[0][0][-1] == out and seen[0][1] == {"timeout": 7}
    assert not Path(out).exists() and not execution_recovery.calls


def test_execute_restoration_rejects_unsupported_actor_before_capture(execution_catalog, execution_recovery, fixture, tmp_path):
    env = execution_recovery
    candidate = copy.deepcopy(execution_catalog[1])
    candidate["actor"] = {"kind": "apple_event"}
    with pytest.raises(ValueError, match="process-identity binding"):
        A._execute_restored(candidate, {}, str(fixture[0]), {}, str(tmp_path / "unsupported-restoration"),
                            30, env.declaration, env.backend)
    assert not env.calls and env.dispatched == 0 and not (tmp_path / "unsupported-restoration").exists()


def test_app_change_between_model_choice_and_execution_refuses_before_actor(catalog, runtime, tmp_path):
    cat = catalog[0]
    selection = A.select(cat, "Read", client=client({"choice": "a0", "confidence": .99}))
    before = len(runtime.sent)
    app = cat["actions"][0]["entry"]["bundle"]
    from pathlib import Path
    (Path(app) / "Contents/changed.txt").write_text("same version, changed bytes")
    with pytest.raises(ValueError, match="fingerprint"):
        A.execute(cat, selection, {}, str(tmp_path / "blocked"))
    assert len(runtime.sent) == before and not (tmp_path / "blocked").exists()


def test_changed_description_and_wrong_inputs_refuse_before_actor(catalog, runtime, tmp_path):
    cat = catalog[0]
    selection = A.select(cat, "Read", client=client({"choice": "a0", "confidence": .99}))
    before = len(runtime.sent)
    with pytest.raises(ValueError, match="inputs"):
        A.execute(cat, selection, {"shell": "invented"}, str(tmp_path / "bad-input"))
    card = copy.deepcopy(catalog[3])
    card["summary"] = "Changed after choosing"
    R.write_json(cat["actions"][0]["entry"]["card"], card)
    with pytest.raises(ValueError, match="catalog action changed"):
        A.execute(cat, selection, {}, str(tmp_path / "bad-card"))
    assert len(runtime.sent) == before


def test_catalog_excludes_stale_proof_instead_of_publishing_it(catalog, tmp_path):
    entry = catalog[0]["actions"][0]["entry"]
    R.write_json(entry["proof"], {"verified": True})
    cat = A.catalog([entry], str(tmp_path / "excluded-catalog"))
    assert cat["actions"] == [] and len(cat["excluded"]) == 1


def test_vm_guard_precedes_action_catalog_and_model_calls(catalog, monkeypatch, tmp_path):
    def refuse():
        raise PermissionError("VM only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    for fn in (lambda: A.catalog([], str(tmp_path / "guarded")),
               lambda: A.select(catalog[0], "Read"),
               lambda: A.execute(catalog[0], {}, {}, str(tmp_path / "guarded")),
               lambda: A.describe({}, {}, "missing", "Title", "Details", str(tmp_path / "guarded"))):
        with pytest.raises(PermissionError, match="VM only"):
            fn()
    assert not (tmp_path / "guarded").exists()


def test_exported_choice_view_selects_without_host_app_or_file_access(catalog, runtime, monkeypatch):
    cat, candidate, _, _ = catalog
    view = A.prepare_selection(cat, "Read q3")
    before = len(runtime.sent)
    def forbidden(*args, **kwargs):
        raise AssertionError("data-only model choice accessed guest resources")
    with monkeypatch.context() as isolated:
        for name in ("assert_guest", "fingerprint", "load_json", "replay"):
            isolated.setattr(R, name, forbidden)
        selection = A.select_view(view, client=client({"choice": "a0", "confidence": .95}))
    assert selection["status"] == "selected" and selection["action"] == candidate["id"]
    assert selection["catalog_sha256"] == R.digest(cat)
    assert selection["choice_view_sha256"] == R.digest(view)
    assert selection["model_input_sha256"] == R.digest(selection["model_input"])
    assert selection["actuated"] is False and len(runtime.sent) == before


def test_host_selection_cannot_bypass_changed_guest_proof(catalog, fixture, runtime, tmp_path):
    cat, candidate, _, _ = catalog
    view = A.prepare_selection(cat, "Read q3")
    selection = A.select_view(view, client=client({"choice": "a0", "confidence": .95}))
    before = len(runtime.sent)
    entry = cat["actions"][0]["entry"]
    proof = R.load_json(entry["proof"])
    proof["candidate_sha256"] = "0" * 64
    R.write_json(entry["proof"], proof)
    with pytest.raises(ValueError):
        A.execute(cat, selection, fixture[2][3], str(tmp_path / "changed-proof"))
    assert len(runtime.sent) == before


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(format="untrusted"),
    lambda v: v["actions"].append(copy.deepcopy(v["actions"][0])),
    lambda v: v["actions"][0].update(card_sha256="not-a-hash"),
    lambda v: v["actions"][0].update(executable="do-not-run"),
    lambda v: v["actions"][0]["contract"].update(oversized="x" * 12001),
])
def test_bad_choice_views_refuse_before_any_model_or_actor(catalog, runtime, mutate):
    view = A.prepare_selection(catalog[0], "Read q3")
    mutate(view)
    seen, before = [], len(runtime.sent)
    with pytest.raises(ValueError):
        A.select_view(view, client=client({"choice": "a0", "confidence": .95}, seen=seen))
    assert seen == [] and len(runtime.sent) == before


def test_playback_selection_projection_retains_semantics_and_full_view():
    policy = {"format": "anyuse.rea-playback-task/1", "before": "playing", "after": "paused",
              "input_scope": "fixed_no_actor_inputs", "heldout_scope": "fresh_trials_with_declared_restart",
              "target": {"bundle_id": "example.app", "version": "1", "build": "2", "sha256": "a" * 64},
              "observer": {"control": {"path": "operational-path"}, "video": {"region": [.1, .2, .3, .4]}}}
    view = {"format": A.CHOICE_VIEW, "catalog_sha256": "1" * 64, "task": "Pause current playing video",
            "actions": [{"id": "pause", "summary": "Pause video", "details": "Only the measured app build.",
                         "candidate_sha256": "2" * 64, "card_sha256": "3" * 64,
                         "contract": {"effect": "fixed_state_transition", "params": [], "scope": "fixed", "policy": policy}}]}
    original = copy.deepcopy(view)
    result = A.select_view(view, client=client({"choice": "a0", "confidence": .95}))
    state = result["model_input"]["state"]
    assert '"before": "playing"' in state and '"after": "paused"' in state
    assert '"bundle_id": "example.app"' in state and '"params": []' in state
    assert "declared_video_region_on_one_display" in state
    assert "operational-path" not in state
    assert view == original and result["choice_view_sha256"] == R.digest(original)


def _fallback_view():
    return {"format": A.CHOICE_VIEW, "catalog_sha256": "4" * 64, "task": "Pause the current video",
            "actions": [{"id": "measured_pause", "summary": "Pause current playback",
                         "details": "Only the explicitly measured target and scope.",
                         "candidate_sha256": "5" * 64, "card_sha256": "6" * 64,
                         "contract": {"effect": "fixed_state_transition", "params": [], "scope": "declared"}}]}


class _FallbackChooser:
    available = True

    def __init__(self, backend, choice="a0", confidence=.95, *, cleanup=None):
        self.metadata = {"backend": backend, "model": backend + "-fixture"}
        self.answer = {"type": "choice", "choice": choice, "confidence": confidence}
        self.seen = []
        self.closed = 0
        self.last_call = {"model_calls": 0}
        self.last_receipt = {"backend": backend, "raw_output": "not called"}
        if cleanup is not None:
            self.cleanup = copy.deepcopy(cleanup)

    def decide(self, state, questions):
        self.seen.append({"state": state, "questions": copy.deepcopy(questions)})
        self.last_call = {"model_calls": 1, "backend": self.metadata["backend"]}
        self.last_receipt = {"backend": self.metadata["backend"],
                             "raw_output": copy.deepcopy(self.answer), "request_sha256": R.digest(self.seen[-1])}
        return O.Answer(answers={"pick": copy.deepcopy(self.answer)}, ms=2, input_tokens=30)

    def close(self):
        self.closed += 1
        if not hasattr(self, "cleanup"):
            self.cleanup = {"attempted": True, "confirmed": True}
        self.last_receipt["cleanup"] = copy.deepcopy(self.cleanup)


def _forbid_choice_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("selection attempted app, guest, filesystem proof or action execution")
    for name in ("assert_guest", "fingerprint", "load_json", "replay"):
        monkeypatch.setattr(R, name, forbidden)
    monkeypatch.setattr(A, "execute", forbidden)


def test_explicit_uncertain_fallback_keeps_both_receipts_counts_and_exact_final_bindings(monkeypatch):
    _forbid_choice_execution(monkeypatch)
    view = _fallback_view()
    original = copy.deepcopy(view)
    primary = _FallbackChooser("laya", confidence=.2)
    reasoner = _FallbackChooser("reasoning", confidence=.94)
    result = A.select_view(view, client=primary, fallback_client=reasoner)
    assert result["status"] == "selected" and result["action"] == view["actions"][0]["id"]
    assert result["catalog_sha256"] == view["catalog_sha256"]
    assert result["candidate_sha256"] == view["actions"][0]["candidate_sha256"]
    assert result["card_sha256"] == view["actions"][0]["card_sha256"]
    assert result["choice_view_sha256"] == R.digest(view)
    assert result["model_calls"] == 2 and result["actuated"] is False
    assert result["fallback_attempted"] is True and result["chooser"]["backend"] == "reasoning"
    attempts = result["selection_attempts"]
    assert [a["role"] for a in attempts] == ["primary", "fallback"]
    assert [a["selection"]["model_calls"] for a in attempts] == [1, 1]
    assert attempts[0]["selection"]["model_receipt"] == primary.last_receipt
    assert attempts[1]["selection"]["model_receipt"] == reasoner.last_receipt
    assert attempts[0]["selection"]["chooser"]["backend"] == "laya"
    assert attempts[1]["selection"]["chooser"]["backend"] == "reasoning"
    assert primary.seen == reasoner.seen and len(primary.seen) == 1
    primary.last_receipt["raw_output"]["confidence"] = .01
    assert attempts[0]["selection"]["model_receipt"]["raw_output"]["confidence"] == .2
    assert view == original and primary.closed == reasoner.closed == 0


@pytest.mark.parametrize("choice", ["a0", "none"])
def test_confident_primary_action_or_none_never_asks_fallback(monkeypatch, choice):
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya", choice=choice, confidence=.95)
    reasoner = _FallbackChooser("reasoning")
    result = A.select_view(_fallback_view(), client=primary, fallback_client=reasoner)
    assert result["fallback_requested"] is True and result["fallback_attempted"] is False
    assert result["model_calls"] == 1 and len(primary.seen) == 1 and not reasoner.seen
    assert result["status"] == ("selected" if choice == "a0" else "abstained")
    assert result["actuated"] is False


def test_uncertain_primary_none_is_not_mistaken_for_confident_abstention(monkeypatch):
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya", choice="none", confidence=.2)
    reasoner = _FallbackChooser("reasoning", confidence=.96)
    result = A.select_view(_fallback_view(), client=primary, fallback_client=reasoner)
    assert result["fallback_attempted"] is True
    assert result["status"] == "selected" and result["model_calls"] == 2
    assert len(reasoner.seen) == 1 and result["chooser"]["backend"] == "reasoning"


def test_unconfirmed_primary_runtime_cleanup_blocks_fallback_before_reasoner(monkeypatch):
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya", confidence=.2,
                               cleanup={"attempted": True, "confirmed": False, "stop_returncode": 1})
    reasoner = _FallbackChooser("reasoning")
    result = A.select_view(_fallback_view(), client=primary, fallback_client=reasoner)
    assert result["status"] == "abstained" and result["resource_review_required"] is True
    assert result["fallback_attempted"] is False and result["model_calls"] == 1
    assert not reasoner.seen and result["actuated"] is False


def test_owned_default_client_is_closed_and_post_close_receipt_is_retained(monkeypatch):
    from anyuse.execution import rea_picker
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya")
    monkeypatch.setattr(rea_picker, "configured_client", lambda: primary)
    result = A.select_view(_fallback_view())
    assert result["status"] == "selected" and result["actuated"] is False
    assert primary.closed == 1 and result["model_receipt"]["cleanup"]["confirmed"] is True


def test_owned_cleanup_failure_revokes_selection_and_blocks_fallback(monkeypatch):
    from anyuse.execution import rea_picker
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya", cleanup={"attempted": True, "confirmed": False})
    reasoner = _FallbackChooser("reasoning")
    monkeypatch.setattr(rea_picker, "configured_client", lambda: primary)
    result = A.select_view(_fallback_view(), fallback_client=reasoner)
    assert primary.closed == 1 and result["status"] == "abstained" and result["action"] is None
    assert result["resource_review_required"] is True and result["fallback_attempted"] is False
    assert not reasoner.seen and result["model_receipt"]["cleanup"]["confirmed"] is False


def test_owned_default_client_closes_even_when_view_validation_refuses(monkeypatch):
    from anyuse.execution import rea_picker
    primary = _FallbackChooser("laya")
    monkeypatch.setattr(rea_picker, "configured_client", lambda: primary)
    view = _fallback_view()
    view["format"] = "not an admitted view"
    with pytest.raises(ValueError):
        A.select_view(view)
    assert primary.closed == 1 and not primary.seen


def test_supplied_primary_failed_cleanup_revokes_selection_even_without_fallback(monkeypatch):
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya", cleanup={"attempted": True, "confirmed": False})
    result = A.select_view(_fallback_view(), client=primary)
    assert result["status"] == "abstained" and result["action"] is None
    assert result["resource_review_required"] is True and result["model_calls"] == 1
    assert result["actuated"] is False


def test_supplied_fallback_failed_cleanup_cannot_return_a_selected_action(monkeypatch):
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya", confidence=.2)
    reasoner = _FallbackChooser("reasoning", cleanup={"attempted": True, "confirmed": False})
    result = A.select_view(_fallback_view(), client=primary, fallback_client=reasoner)
    assert result["status"] == "abstained" and result["action"] is None
    assert result["resource_review_required"] is True and result["model_calls"] == 2
    assert result["fallback_attempted"] is True and len(reasoner.seen) == 1
    assert result["actuated"] is False


@pytest.mark.parametrize("owned", [False, True])
def test_never_started_unavailable_primary_needs_no_runtime_cleanup_before_fallback(monkeypatch, owned):
    from anyuse.execution import rea_picker
    _forbid_choice_execution(monkeypatch)
    primary = _FallbackChooser("laya", cleanup={"attempted": False, "confirmed": False})
    primary.available = False
    reasoner = _FallbackChooser("reasoning")
    if owned:
        monkeypatch.setattr(rea_picker, "configured_client", lambda: primary)
    result = A.select_view(_fallback_view(), client=None if owned else primary, fallback_client=reasoner)
    assert result["status"] == "selected" and result["fallback_attempted"] is True
    assert result["model_calls"] == 1 and not primary.seen and len(reasoner.seen) == 1
    assert not result.get("resource_review_required") and result["actuated"] is False


def test_resource_review_flag_cannot_be_bypassed_by_selected_status_at_execute(catalog, fixture, runtime, tmp_path):
    selection = A.select(catalog[0], "Read", client=client({"choice": "a0", "confidence": .99}))
    assert selection["status"] == "selected"
    selection["resource_review_required"] = True
    before = len(runtime.sent)
    with pytest.raises(ValueError):
        A.execute(catalog[0], selection, fixture[2][3], str(tmp_path / "resource-blocked"))
    assert len(runtime.sent) == before and not (tmp_path / "resource-blocked").exists()


def test_model_judged_catalog_retains_tier_and_replay_model_cost(fixed_state_campaign, fixture, tmp_path, monkeypatch):
    from anyuse import execution
    candidate, proof, calls, campaign = fixed_state_campaign
    candidate["backend"] = H.VISUAL_BACKEND
    candidate["effect"] = "visible_state_transition"
    candidate["policy"] = {"format": "anyuse.rea-visual-effect-task/1", "before": "Initial visible view",
        "after": "Requested visible view", "minimum_confidence": .8, "target": candidate["target"],
        "model": {"provider": "local", "model": "not-for-the-chooser"}}
    proof.update(backend=H.VISUAL_BACKEND, candidate_sha256=R.digest(candidate),
                 status="model_judged", verification_tier="model_judged")
    def replay(*args):
        calls.append(("model_judged_replay", args))
        return {"verdict": "model_judged", "verification_tier": "model_judged", "model_calls": 2}
    campaign.replay = replay
    monkeypatch.setitem(sys.modules, "anyuse.execution.rea_visual_campaign", campaign)
    monkeypatch.setattr(execution, "rea_visual_campaign", campaign, raising=False)
    app = str(fixture[0])
    card = A.describe(candidate, proof, app, "Set a visible view", "A retained visual model judgment",
                      str(tmp_path / "model-card"))
    assert card["contract"]["verification_tier"] == "model_judged"
    paths = {name: str(tmp_path / ("model-" + name + ".json")) for name in ("candidate", "proof")}
    R.write_json(paths["candidate"], candidate); R.write_json(paths["proof"], proof)
    cat = A.catalog([{**paths, "card": str(tmp_path / "model-card/action.json"), "bundle": app}],
                    str(tmp_path / "model-catalog"))
    view = A.prepare_selection(cat, "Set the requested visible view")
    model_card = A._model_cards(view)[0]
    assert model_card["contract"]["verification_tier"] == "model_judged"
    assert model_card["contract"]["policy"]["replay_verification_uses_model"] is True
    assert "model" not in model_card["contract"]["policy"]
    selection = A.select_view(view, client=client({"choice": "a0", "confidence": .95}))
    result = A.execute(cat, selection, {}, str(tmp_path / "model-replay"))
    assert result == {"verdict": "model_judged", "verification_tier": "model_judged", "model_calls": 2}
    count = sum(name == "model_judged_replay" for name, _ in calls)
    card["contract"]["verification_tier"] = "deterministically_verified"
    R.write_json(tmp_path / "model-card/action.json", card)
    with pytest.raises(ValueError):
        A.execute(cat, selection, {}, str(tmp_path / "changed-tier-replay"))
    assert sum(name == "model_judged_replay" for name, _ in calls) == count


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
