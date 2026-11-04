"""Real lower playback trials through campaign learning/fresh publication gate."""
import copy

import pytest

from anyuse.execution import rea_actions as A
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_state_campaign as C
from test_rea_playback_trial import ACTOR, playback, run
from test_rea_runtime_bridge import runtime


def test_six_actual_retained_pixel_native_trials_cross_campaign_gate(playback, tmp_path):
    entries = {"learning": [], "fresh": []}
    total_goals = 0
    for phase in entries:
        for index in range(3):
            # Separate public setup, not actor input novelty: the inert fixture
            # returns to playing and its already-existing process stays the same.
            playback["native"]["calls"].clear()
            playback["clock"].sleep(.5)
            playback["out"] = tmp_path / f"actual-{phase}-{index}"
            playback["intent"] = {**copy.deepcopy(playback["intent"]), "id": f"{phase}-{index}", "phase": phase,
                                  "observed_monotonic": playback["clock"]()}
            result = run(playback)
            assert result["status"] == "trial_measurement_validated", result
            assert result["measurement"]["native_calls"] == 4 and result["measurement"]["invocations"] == 1
            total_goals += len(playback["native"]["calls"])
            entries[phase].append({"case": result["case_file"], "restart": None})
    citation = tmp_path / "independent-interface.json"
    R.write_json(citation, {"method": {"selector": ACTOR["selector"], "signature": ACTOR["signature"]}})
    profile = {"format": C.PROFILE, "id": "inert_pause_playback", "label": "Pause current inert media",
               "effect": "fixed_state_transition", "params": [], "policy": playback["policy"], "scope": C.SCOPE,
               "evidence": [{"file": str(citation), "sha256": R._file_hash(citation), "pointer": "/method"}]}
    candidate = C.learn(profile, [{"actor": ACTOR, "case": entry["case"]} for entry in entries["learning"]],
                        str(playback["app"]), str(tmp_path / "learn"))
    proof = C.verify(candidate, entries["fresh"], str(playback["app"]), str(tmp_path / "verify"))
    assert total_goals == 6
    assert proof["status"] == "fresh_verified"
    assert proof["measurement"]["phase_counts"] == {"learning": 3, "fresh": 3}
    assert proof["measurement"]["native_calls"] == 24 and proof["measurement"]["task_invocations"] == 6
    assert proof["measurement"]["actor_inputs"] == {} and not proof["measurement"]["restart_deployment_proven"]
    for unsupported in (proof, {**proof, "status": "verified"}):
        assert not C.proof_status(candidate, unsupported, candidate["target"])[0]
        with pytest.raises(ValueError):
            A.describe(candidate, unsupported, str(playback["app"]), "Pause media", "No witnessed heldout restart yet", str(tmp_path / "forbidden-card"))
    assert not (tmp_path / "forbidden-card").exists()


@pytest.fixture
def visual_campaign_factory(runtime, tmp_path, monkeypatch):
    """Campaign contract tests over inert retained lower-layer measurements.

    The visual-effect producer has separate screenshot/model/runtime tests. This
    fixture exercises promotion, input partitions, epoch continuity and retained
    artifact reopening without calling a model or an application.
    """
    from pathlib import Path
    from types import SimpleNamespace
    from anyuse.execution import rea_visual_campaign as VC, rea_visual_effect as VE
    from anyuse.execution import rea_process_restart as RR
    from anyuse.execution import rea_inspector_bridge as I
    app, initial_identity, _, _, _ = runtime
    target = R.fingerprint(app)

    def create(*, parameterized=False, provider="native", enum_values=None):
        root = tmp_path / "visual-campaign-fixture"
        root.mkdir()
        params = [{"name": "query", "type": "string", "source": "text"}] if parameterized else []
        actor = copy.deepcopy(ACTOR)
        if parameterized:
            actor["arguments"] = [{"type": "utf8", "value": {"input": "query"}}]
        if provider == "inspector":
            actor = {"kind": "inspector_method", "receiver": {"root": {"kind": "global", "name": "measuredEditor"}, "steps": []},
                     "method": "setLabel", "function_sha256": "a" * 64,
                     "arguments": [{"type": "utf8", "value": {"input": "query"} if parameterized else "constant"}],
                     "return_type": "void"}
            declaration = root / "inspector-declaration.json"
            R.write_json(declaration, {"contract": I.call_contract(actor)})
            actor["signature_evidence"] = {"kind": "reviewed_js", "file": str(declaration),
                                           "sha256": R._file_hash(declaration), "pointer": "/contract"}
        policy = {"format": VE.POLICY, "id": "inert-visible-change", "target": target,
                  "kind": "visible_state_transition", "params": params,
                  "observer": {"window_title": None}, "before": "The owned label shows its prior value.",
                  "after": "The owned label shows the declared new value.", "minimum_confidence": .9,
                  "model": {"provider": "local", "base_url": "http://127.0.0.1:9138/v1", "model": "inert-model",
                            "api_key_env": "", "retries": 0, "timeout": 10, "max_tokens": 128,
                            "price_in": 0, "price_out": 0}}
        if enum_values is not None:
            assert parameterized
            policy["input_constraints"] = {"query": {"kind": "enum", "values": enum_values}}
        policy = VE.validate_policy(policy)
        citation = root / "independent-interface.json"
        R.write_json(citation, {"handler": "Inert independently supplied callable/interface metadata."})
        profile = {"format": VC.PROFILE, "id": "inert_visual_change", "label": "Change an owned visible label",
                   "effect": "visible_state_transition", "params": params, "policy": policy, "scope": VC.SCOPE,
                   "evidence": [{"file": str(citation), "sha256": R._file_hash(citation), "pointer": "/handler"}]}
        old_identity = copy.deepcopy(initial_identity)
        new_identity = {**old_identity, "pid": old_identity["pid"] + 100, "launch_time": old_identity["launch_time"] + 100.0}
        def kernel(identity):
            return {"pid": identity["pid"], "uid": identity["uid"], "executable": identity["executable"],
                    "ppid": 1, "started_at": str(identity["launch_time"])}
        def pin(path):
            return {"file": str(path), "bytes": path.stat().st_size, "sha256": R._file_hash(path)}
        entries = {"learning": [], "fresh": [], "heldout": []}
        measurements, records = {}, []
        for index in range(9):
            phase, ordinal = ("learning", index) if index < 3 else (("fresh", index - 3) if index < 6 else ("heldout", index - 6))
            identity = old_identity if index < 6 else new_identity
            identifier = f"visual-{phase}-{ordinal}"
            folder = root / identifier; folder.mkdir()
            evidence = {}
            for role in sorted(VE.EVIDENCE):
                path = folder / (role + ".json")
                R.write_json(path, {"fixture": "inert retained lower-layer evidence", "case": identifier, "role": role})
                evidence[role] = pin(path)
            inputs = ({"query": enum_values[index % len(enum_values)] if enum_values is not None else f"separate-value-{index}"}
                      if parameterized else {})
            case = VE.validate_case(policy, {"format": VE.CASE, "id": identifier, "phase": phase,
                "inputs": inputs, "deployment": {"id": f"deployment-{identity['pid']}", "kind": "existing_process", "identity": identity},
                "evidence": evidence})
            path = folder / "case.json"; R.write_json(path, case)
            entry = {"case": pin(path), "restart": None}; entries[phase].append(entry)
            bound = VE.bind_actor(actor, policy, inputs)
            measurement = {"status": VC.TIER, "verification_tier": VC.TIER, "trial_measurement_validated": True,
                "proof_created": False, "deterministic_proof_created": False, "task_proof_created": False,
                "actor_sha256": R.digest(bound), "policy_sha256": R.digest(policy), "case_sha256": R.digest(case),
                "target": target, "identity": copy.deepcopy(identity), "kernel": kernel(identity),
                "case_id": identifier, "phase": phase, "inputs": inputs, "model_calls": 2, "invocations": 1,
                "started_monotonic": 10. + index * 10, "finished_monotonic": 12. + index * 10,
                "files": {value["file"]: value for value in evidence.values()}}
            measurements[identifier] = measurement
            records.append({"case": case, "path": path, "entry": entry, "measurement": measurement})
        baseline = root / "restart-baseline.json"; R.write_json(baseline, {"started_monotonic": 63.})
        restart_intent = root / "restart-intent.json"; R.write_json(restart_intent, {"target": target})
        restart_receipt = root / "restart-receipt.json"
        R.write_json(restart_receipt, {"baseline": pin(baseline), "finished_monotonic": 65., "target": target,
            "old_identity": old_identity, "new_identity": new_identity,
            "old_kernel": kernel(old_identity), "new_kernel": kernel(new_identity)})
        restart = {"intent": pin(restart_intent), "receipt": pin(restart_receipt), "predecessor": records[5]["case"]["id"]}
        entries["heldout"][0]["restart"] = restart
        def validate_restart(declaration, receipt_pin):
            receipt = R.load_json(receipt_pin["file"])
            assert declaration["target"] == receipt["target"] == target
            return {"restart_measurement_validated": True, **{key: receipt[key] for key in
                ("target", "old_identity", "new_identity", "old_kernel", "new_kernel")},
                "files": {value["file"]: value for value in (pin(baseline), pin(restart_receipt), pin(restart_intent))}}
        monkeypatch.setattr(RR, "validate_restart", validate_restart)
        monkeypatch.setattr(VE, "validate_trial", lambda policy, case, actor: copy.deepcopy(measurements[case["id"]]))
        def replace_case(index):
            record = records[index]; R.write_json(record["path"], record["case"])
            record["entry"]["case"] = pin(record["path"])
            record["measurement"]["case_sha256"] = R.digest(record["case"])
        def learn():
            return VC.learn(profile, [{"actor": actor, "case": entry["case"]} for entry in entries["learning"]],
                            str(app), str(root / "learn"))
        def promote():
            candidate = learn()
            fresh = VC.verify(candidate, entries["fresh"], str(app), str(root / "fresh"))
            return candidate, VC.heldout(candidate, fresh, entries["heldout"], str(app), str(root / "heldout"))
        return SimpleNamespace(C=VC, V=VE, app=app, root=root, target=target, actor=actor, policy=policy,
            profile=profile, entries=entries, records=records, measurements=measurements,
            old_identity=old_identity, new_identity=new_identity, learn=learn, promote=promote,
            replace_case=replace_case, pin=pin)
    return create


@pytest.mark.parametrize("parameterized,provider", [(False, "native"), (True, "native"), (False, "inspector"), (True, "inspector")])
def test_visual_campaign_promotes_only_retained_model_judgments_with_restart(visual_campaign_factory, parameterized, provider):
    f = visual_campaign_factory(parameterized=parameterized, provider=provider)
    candidate = f.learn()
    proof = f.C.verify(candidate, f.entries["fresh"], str(f.app), str(f.root / "fresh"))
    assert proof["status"] == "fresh_model_judged" and proof["verification_tier"] == "model_judged"
    assert not f.C.proof_status(candidate, proof, f.target)[0]
    complete = f.C.heldout(candidate, proof, f.entries["heldout"], str(f.app), str(f.root / "heldout"))
    assert f.C.proof_status(candidate, complete, f.target)[0]
    assert complete["status"] == "model_judged" and complete["verification_tier"] == "model_judged"
    assert complete["measurement"]["phase_counts"] == {"learning": 3, "fresh": 3, "heldout": 3}
    assert complete["measurement"]["model_calls"] == 18 and complete["measurement"]["task_invocations"] == 9
    assert complete["measurement"]["restart_deployment_proven"] is True
    assert not f.C.proof_status(candidate, {**complete, "status": "verified"}, f.target)[0]
    assert not f.C.proof_status(candidate, {**complete, "verification_tier": "deterministic"}, f.target)[0]
    if parameterized:
        assert f.C.bind(candidate, {"query": "a further declared input"})["arguments"][0]["value"] == "a further declared input"
        assert f.records[0]["measurement"]["actor_sha256"] != R.digest(candidate["actor"])


@pytest.mark.parametrize("change", ["missing_restart", "early_restart", "duplicate_id", "overlapping_inputs"])
def test_visual_plan_requires_distinct_real_cases_inputs_and_heldout_restart(visual_campaign_factory, change):
    f = visual_campaign_factory(parameterized=True)
    plan = {"format": f.C.PLAN, "learning": [], "verification": [], "heldout": []}
    for phase, key in (("learning", "learning"), ("verification", "fresh"), ("heldout", "heldout")):
        plan[phase] = [{"id": row["case"]["id"], "inputs": row["case"]["inputs"], "restart": index == 0 and phase == "heldout"}
                       for index, row in enumerate([r for r in f.records if r["case"]["phase"] == key])]
    assert f.C.validate_plan(plan, f.policy["params"]) == plan
    if change == "missing_restart": plan["heldout"][0]["restart"] = False
    elif change == "early_restart": plan["learning"][0]["restart"] = True
    elif change == "duplicate_id": plan["verification"][0]["id"] = plan["learning"][0]["id"]
    else: plan["heldout"][0]["inputs"] = plan["learning"][0]["inputs"]
    with pytest.raises(ValueError): f.C.validate_plan(plan, f.policy["params"])


def _visual_plan(f, *, input_mode=None):
    plan = {"format": f.C.PLAN}
    if input_mode is not None:
        plan["input_mode"] = input_mode
    for phase, measured in (("learning", "learning"), ("verification", "fresh"), ("heldout", "heldout")):
        plan[phase] = [{"id": row["case"]["id"], "inputs": copy.deepcopy(row["case"]["inputs"]),
                        "restart": phase == "heldout" and index == 0}
                       for index, row in enumerate(r for r in f.records if r["case"]["phase"] == measured)]
    return plan


@pytest.mark.parametrize("provider", ["native", "inspector"])
def test_visual_finite_enum_repeats_are_actual_distinct_trials_covering_each_phase(visual_campaign_factory, provider):
    f = visual_campaign_factory(parameterized=True, provider=provider, enum_values=["alpha", "beta"])
    plan = _visual_plan(f, input_mode=f.C.FINITE_INPUTS)
    assert f.C.validate_plan(plan, f.policy["params"]) == plan
    assert f.C.validate_coverage(plan, f.policy) == plan
    candidate, proof = f.promote()
    assert f.C.proof_status(candidate, proof, f.target)[0]
    assert proof["measurement"]["input_mode"] == "finite_enum_coverage"
    assert proof["measurement"]["phase_counts"] == {"learning": 3, "fresh": 3, "heldout": 3}
    assert proof["measurement"]["task_invocations"] == 9 and proof["measurement"]["model_calls"] == 18
    assert len({row["case"]["id"] for row in f.records}) == 9
    with pytest.raises(ValueError, match="disjoint"):
        f.C.validate_plan(_visual_plan(f), f.policy["params"])
    plan["heldout"][2]["id"] = plan["learning"][0]["id"]
    with pytest.raises(ValueError, match="distinct trial IDs"):
        f.C.validate_plan(plan, f.policy["params"])


def test_visual_finite_enum_coverage_is_cartesian_for_multiple_parameters(visual_campaign_factory):
    f = visual_campaign_factory(parameterized=True, enum_values=["alpha", "beta"])
    policy = copy.deepcopy(f.policy)
    policy["params"].append({"name": "mode", "type": "string", "source": "text"})
    policy["input_constraints"]["mode"] = {"kind": "enum", "values": ["first", "second"]}
    combinations = [{"query": query, "mode": mode} for query in ("alpha", "beta") for mode in ("first", "second")]
    plan = {"format": f.C.PLAN, "input_mode": f.C.FINITE_INPUTS}
    for phase in ("learning", "verification", "heldout"):
        plan[phase] = [{"id": f"{phase}-{index}", "inputs": values,
                        "restart": phase == "heldout" and index == 0}
                       for index, values in enumerate(combinations)]
    assert f.C.validate_coverage(plan, policy) == plan
    plan["verification"][3]["inputs"] = plan["verification"][0]["inputs"]
    with pytest.raises(ValueError, match="complete frozen enum domain"):
        f.C.validate_coverage(plan, policy)


@pytest.mark.parametrize("change", ["missing_enum", "decimal", "unconstrained_parameter", "too_many_combinations",
                                   "missing_phase_value", "outside_domain"])
def test_visual_finite_plan_refuses_unbounded_or_incomplete_domains_before_dispatch(visual_campaign_factory, change):
    f = visual_campaign_factory(parameterized=True, enum_values=["alpha", "beta"])
    plan, policy = _visual_plan(f, input_mode=f.C.FINITE_INPUTS), copy.deepcopy(f.policy)
    if change == "missing_enum":
        policy["input_constraints"] = {}
    elif change == "decimal":
        policy["input_constraints"]["query"] = {"kind": "decimal", "minimum": "0", "maximum": "10"}
    elif change == "unconstrained_parameter":
        policy["params"].append({"name": "mode", "type": "string", "source": "text"})
        for phase in ("learning", "verification", "heldout"):
            for row in plan[phase]: row["inputs"]["mode"] = "unbounded"
    elif change == "too_many_combinations":
        policy["params"].append({"name": "mode", "type": "string", "source": "text"})
        policy["input_constraints"]["query"]["values"] = [str(i) for i in range(6)]
        policy["input_constraints"]["mode"] = {"kind": "enum", "values": [str(i) for i in range(6)]}
        for phase in ("learning", "verification", "heldout"):
            for row in plan[phase]: row["inputs"] = {"query": "0", "mode": "0"}
    elif change == "missing_phase_value":
        for row in plan["verification"]: row["inputs"] = {"query": "alpha"}
    else:
        plan["heldout"][0]["inputs"] = {"query": "outside"}
    with pytest.raises(ValueError): f.C.validate_coverage(plan, policy)
    assert not (f.root / "learn").exists()


@pytest.mark.parametrize("phase", ["learning", "fresh", "heldout"])
def test_visual_measured_enum_campaign_requires_each_phase_full_coverage(visual_campaign_factory, phase):
    f = visual_campaign_factory(parameterized=True, enum_values=["alpha", "beta"])
    for index, record in enumerate(f.records):
        if record["case"]["phase"] == phase:
            record["case"]["inputs"] = {"query": "alpha"}
            record["measurement"]["inputs"] = {"query": "alpha"}
            record["measurement"]["actor_sha256"] = R.digest(f.V.bind_actor(f.actor, f.policy, {"query": "alpha"}))
            f.replace_case(index)
    with pytest.raises(ValueError, match="complete frozen enum domain"):
        candidate = f.learn()
        proof = f.C.verify(candidate, f.entries["fresh"], str(f.app), str(f.root / "fresh"))
        f.C.heldout(candidate, proof, f.entries["heldout"], str(f.app), str(f.root / "heldout"))


def test_visual_enum_repetition_still_refuses_reused_execution_artifacts(visual_campaign_factory):
    f = visual_campaign_factory(parameterized=True, enum_values=["alpha", "beta"])
    candidate = f.learn()
    record = f.records[3]
    role = next(iter(record["case"]["evidence"]))
    record["case"]["evidence"][role] = copy.deepcopy(f.records[0]["case"]["evidence"][role])
    f.replace_case(3)
    with pytest.raises(ValueError, match="artifacts cannot represent another execution"):
        f.C.verify(candidate, f.entries["fresh"], str(f.app), str(f.root / "rejected"))


def test_visual_default_plan_validates_continuous_constraint_without_allowing_repeats(visual_campaign_factory):
    f = visual_campaign_factory(parameterized=True)
    policy = copy.deepcopy(f.policy)
    policy["input_constraints"]["query"] = {"kind": "decimal", "minimum": "0", "maximum": "8"}
    plan = _visual_plan(f)
    for index, row in enumerate(row for phase in ("learning", "verification", "heldout") for row in plan[phase]):
        row["inputs"] = {"query": str(index)}
    assert f.C.validate_coverage(plan, policy) == plan
    plan["heldout"][0]["inputs"] = {"query": "9"}
    with pytest.raises(ValueError, match="outside its frozen decimal range"):
        f.C.validate_coverage(plan, policy)
    plan["heldout"][0]["inputs"] = copy.deepcopy(plan["learning"][0]["inputs"])
    with pytest.raises(ValueError, match="disjoint"):
        f.C.validate_coverage(plan, policy)


@pytest.mark.parametrize("change", ["identity", "kernel", "overlapping_inputs", "artifact_reuse", "unordered"])
def test_visual_fresh_phase_refuses_unwitnessed_process_or_reused_evidence(visual_campaign_factory, change):
    f = visual_campaign_factory(parameterized=change == "overlapping_inputs")
    candidate = f.learn(); record = f.records[3]
    if change == "identity":
        record["case"]["deployment"]["identity"] = copy.deepcopy(f.new_identity)
        record["measurement"]["identity"] = copy.deepcopy(f.new_identity)
    elif change == "kernel": record["measurement"]["kernel"]["started_at"] = "changed without restart"
    elif change == "overlapping_inputs":
        record["case"]["inputs"] = copy.deepcopy(f.records[0]["case"]["inputs"])
        record["measurement"]["inputs"] = record["case"]["inputs"]
    elif change == "artifact_reuse":
        role = next(iter(record["case"]["evidence"]))
        record["case"]["evidence"][role] = copy.deepcopy(f.records[0]["case"]["evidence"][role])
    else: record["measurement"]["started_monotonic"] = f.records[2]["measurement"]["finished_monotonic"]
    f.replace_case(3)
    with pytest.raises(ValueError): f.C.verify(candidate, f.entries["fresh"], str(f.app), str(f.root / "rejected"))
    assert not (f.root / "rejected").exists()


@pytest.mark.parametrize("change", ["missing", "predecessor", "same_epoch"])
def test_visual_heldout_requires_restart_at_actual_epoch_boundary(visual_campaign_factory, change):
    f = visual_campaign_factory(); candidate = f.learn()
    proof = f.C.verify(candidate, f.entries["fresh"], str(f.app), str(f.root / "fresh"))
    if change == "missing": f.entries["heldout"][0]["restart"] = None
    elif change == "predecessor": f.entries["heldout"][0]["restart"]["predecessor"] = f.records[0]["case"]["id"]
    else:
        f.records[6]["case"]["deployment"]["identity"] = copy.deepcopy(f.old_identity)
        f.records[6]["measurement"]["identity"] = copy.deepcopy(f.old_identity)
        f.records[6]["measurement"]["kernel"] = copy.deepcopy(f.records[5]["measurement"]["kernel"])
        f.replace_case(6)
    with pytest.raises(ValueError): f.C.heldout(candidate, proof, f.entries["heldout"], str(f.app), str(f.root / "rejected"))
    assert not (f.root / "rejected").exists()


@pytest.mark.parametrize("field,value", [("verification_tier", "deterministic"), ("model_calls", 0),
    ("model_calls", True), ("proof_created", True), ("deterministic_proof_created", True),
    ("trial_measurement_validated", False), ("invocations", True)])
def test_visual_learning_cannot_promote_bad_or_relabelled_lower_measurement(visual_campaign_factory, field, value):
    f = visual_campaign_factory(); f.records[0]["measurement"][field] = value
    with pytest.raises(ValueError): f.learn()
    assert not (f.root / "learn").exists()


def test_visual_publication_reopens_actual_model_evidence(visual_campaign_factory):
    f = visual_campaign_factory(); candidate, proof = f.promote()
    path = f.records[4]["case"]["evidence"]["judgment"]["file"]
    from pathlib import Path
    Path(path).write_bytes(b'{"changed":"retained judgment"}')
    assert not f.C.proof_status(candidate, proof, f.target)[0]


def test_visual_campaign_reopens_more_than_legacy_256_evidence_files(visual_campaign_factory):
    f = visual_campaign_factory()
    for record in f.records:
        for index in range(30):
            path = record["path"].parent / f"retained-closure-{index}.json"
            R.write_json(path, {"case": record["case"]["id"], "source_member": index})
            record["measurement"]["files"][str(path)] = f.pin(path)
    candidate, proof = f.promote()
    assert f.C.proof_status(candidate, proof, f.target)[0]
    measurement = proof["measurement"]
    assert measurement["campaign_evidence_files"] > 256
    assert measurement["campaign_evidence_limits"] == {"max_files": 4096, "max_total_bytes": 2 * 1024**3}
    assert 0 < measurement["campaign_evidence_bytes"] < measurement["campaign_evidence_limits"]["max_total_bytes"]
    # The promoted closure is still rehashed, even above the former count cap.
    path.write_bytes(b'{"changed":"a retained source member"}')
    assert not f.C.proof_status(candidate, proof, f.target)[0]


def test_visual_campaign_reader_accepts_300_files_without_changing_legacy_limit(tmp_path):
    from anyuse.execution import rea_visual_campaign as VC, rea_state_evidence as E
    campaign, legacy = VC._CampaignReader(), E._Reader()
    pins = []
    for index in range(300):
        path = tmp_path / f"pinned-{index}.json"
        R.write_json(path, {"index": index})
        pin = {"file": str(path), "bytes": path.stat().st_size, "sha256": R._file_hash(path)}
        pins.append(pin)
        assert campaign.json(pin, "resource") == {"index": index}
        if index < 256:
            assert legacy.json(pin, "resource") == {"index": index}
    assert len(campaign.files) == 300 and len(legacy.files) == E.MAX_FILES == 256
    assert campaign.total == sum(pin["bytes"] for pin in pins)
    campaign.recheck()
    with pytest.raises(ValueError, match="file/aggregate byte budget"):
        legacy.read(pins[256], "resource")


@pytest.mark.parametrize("budget", ["files", "bytes"])
def test_visual_campaign_reader_enforces_its_own_count_and_byte_budget(tmp_path, monkeypatch, budget):
    from anyuse.execution import rea_visual_campaign as VC, rea_state_evidence as E
    if budget == "files": monkeypatch.setattr(VC, "_CAMPAIGN_MAX_FILES", 2)
    else: monkeypatch.setattr(VC, "_CAMPAIGN_MAX_TOTAL_BYTES", 5)
    reader = VC._CampaignReader(); accepted = []
    for index in range(3):
        path = tmp_path / f"budget-{index}"
        path.write_bytes(b"xx")
        pin = {"file": str(path), "bytes": 2, "sha256": R._file_hash(path)}
        if index < 2:
            assert reader.read(pin, "resource") == b"xx"
            accepted.append(pin)
        else:
            with pytest.raises(ValueError, match="visual campaign file/aggregate byte budget exceeded"):
                reader.read(pin, "resource")
    assert len(reader.files) == 2 and reader.total == 4
    # Rechecking an existing pin consumes no additional count or byte budget.
    reader.recheck()
    assert reader.read(accepted[0], "resource") == b"xx"
    assert E.MAX_FILES == 256 and E.MAX_TOTAL_BYTES == 2 * 1024**3


@pytest.mark.parametrize("change", ["role", "hash", "symlink", "hardlink", "writable"])
def test_visual_campaign_reader_preserves_original_pin_alias_and_role_admission(tmp_path, change):
    import os
    from anyuse.execution import rea_visual_campaign as VC, rea_state_evidence as E
    path = tmp_path / "original.json"; R.write_json(path, {"owned": "one pinned file"})
    pin = {"file": str(path), "bytes": path.stat().st_size, "sha256": R._file_hash(path)}
    readers = [VC._CampaignReader(), E._Reader()]
    for reader in readers: reader.read(pin, "resource")
    domain = "resource"
    if change == "role": domain = "trial"
    elif change == "hash": pin = {**pin, "sha256": "0" * 64}
    elif change == "symlink":
        alias = tmp_path / "alias.json"; alias.symlink_to(path)
        pin = {**pin, "file": str(alias)}
    elif change == "hardlink":
        alias = tmp_path / "alias.json"; os.link(path, alias)
        pin = {**pin, "file": str(alias)}
    else: path.chmod(0o666)
    for reader in readers:
        with pytest.raises(ValueError): reader.read(pin, domain)


def test_visual_replay_binds_expected_epoch_before_intent_and_retains_model_tier(visual_campaign_factory, monkeypatch):
    f = visual_campaign_factory(parameterized=True); candidate, proof = f.promote()
    inputs = {"query": "one further actual caller value"}; observed = []
    case = copy.deepcopy(f.records[8]["case"]); case.update(id="visual-replay", phase="replay", inputs=inputs)
    path = f.root / "replay-case.json"; R.write_json(path, case)
    measured = {**copy.deepcopy(f.records[8]["measurement"]), "case_id": case["id"], "phase": "replay", "inputs": inputs,
                "case_sha256": R.digest(case), "actor_sha256": R.digest(f.V.bind_actor(f.actor, f.policy, inputs))}
    f.measurements[case["id"]] = measured
    def intent(*args, **kwargs):
        observed.append(("intent", args, kwargs)); return {"fixture": "actual inert intent"}
    def trial(*args, **kwargs):
        observed.append(("trial", args, kwargs))
        return {"status": "model_judged", "error": None, "model_calls": 2, "measurement": measured,
                "case": case, "case_file": f.pin(path), "resource_review_required": False}
    monkeypatch.setattr(f.V, "make_intent", intent); monkeypatch.setattr(f.V, "run_trial", trial)
    result = f.C.replay(candidate, proof, inputs, str(f.app), str(f.root / "replay"), expected_identity=f.new_identity)
    assert result["verdict"] == result["verification_tier"] == "model_judged" and result["model_calls"] == 2
    assert result["inputs"] == inputs and not result["proof_created"]
    assert [row[0] for row in observed] == ["intent", "trial"]
    assert all(row[2]["expected_identity"] == f.new_identity for row in observed)
    assert observed[0][1][4] == inputs


@pytest.mark.parametrize("uncertain", [False, True])
def test_visual_replay_keeps_refusal_or_uncertain_native_effects_without_retry(visual_campaign_factory, monkeypatch, uncertain):
    f = visual_campaign_factory(); candidate, proof = f.promote(); calls = []
    monkeypatch.setattr(f.V, "make_intent", lambda *args, **kwargs: {})
    def trial(*args, **kwargs):
        calls.append(1)
        return {"status": "refused", "model_calls": 1, "error": {"message": "known failure"}, "resource_review_required": uncertain}
    monkeypatch.setattr(f.V, "run_trial", trial)
    with pytest.raises(R.ResourceReviewRequired if uncertain else ValueError):
        f.C.replay(candidate, proof, {}, str(f.app), str(f.root / "refused"))
    assert calls == [1]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_disjoint_enum_admission_survives_learning_and_full_campaign(visual_campaign_factory):
    f = visual_campaign_factory(parameterized=True, enum_values=["value-" + str(i) for i in range(12)])
    plan = _visual_plan(f)
    assert f.C.validate_coverage(plan, f.policy) == plan
    candidate = f.C.learn(f.profile,
        [{"actor": f.actor, "case": e["case"]} for e in f.entries["learning"]],
        str(f.app), str(f.root / "sampled-learn"), input_mode=f.C.DISJOINT_INPUTS)
    assert candidate["input_mode"] == f.C.DISJOINT_INPUTS
    fresh = f.C.verify(candidate, f.entries["fresh"], str(f.app), str(f.root / "sampled-fresh"))
    proof = f.C.heldout(candidate, fresh, f.entries["heldout"], str(f.app), str(f.root / "sampled-heldout"))
    assert f.C.proof_status(candidate, proof, f.target)[0]
    assert proof["measurement"]["input_mode"] == f.C.DISJOINT_INPUTS
    assert proof["measurement"]["phase_counts"] == {"learning": 3, "fresh": 3, "heldout": 3}
    invalid = copy.deepcopy(candidate); invalid["input_mode"] = "unknown"
    with pytest.raises(ValueError, match="input mode"):
        f.C.validate(invalid)


def test_legacy_enum_candidate_keeps_its_shape_and_finite_semantics(visual_campaign_factory):
    f = visual_campaign_factory(parameterized=True, enum_values=["alpha", "beta"])
    candidate = f.learn()
    assert "input_mode" not in candidate and "input_mode" not in f.C.validate(candidate)
    assert R.digest(candidate) == R.digest(f.C.validate(candidate))
    proof = f.C.verify(candidate, f.entries["fresh"], str(f.app), str(f.root / "legacy-fresh"))
    assert proof["measurement"]["input_mode"] == f.C.FINITE_INPUTS


def test_visual_campaign_supports_fast_1_1_1_and_custom_trial_policy(visual_campaign_factory):
    f = visual_campaign_factory(parameterized=True)
    learn_entry = f.entries["learning"][0]
    fresh_entry = f.entries["fresh"][2]
    heldout_entry = f.entries["heldout"][0]
    plan = {
        "format": f.C.PLAN,
        "trial_policy": {"preset": "fast"},
        "learning": [{"id": learn_entry["case"]["id"] if "id" in learn_entry["case"] else f.records[0]["case"]["id"],
                      "inputs": f.records[0]["case"]["inputs"], "restart": False}],
        "verification": [{"id": f.records[5]["case"]["id"], "inputs": f.records[5]["case"]["inputs"], "restart": False}],
        "heldout": [{"id": f.records[6]["case"]["id"], "inputs": f.records[6]["case"]["inputs"], "restart": True}],
    }
    validated = f.C.validate_coverage(plan, f.policy)
    assert validated["trial_policy"] == f.C.FAST_TRIAL_POLICY
    candidate = f.C.learn(f.profile, [{"actor": f.actor, "case": learn_entry["case"]}],
                          str(f.app), str(f.root / "fast-learn"), trial_policy="fast")
    assert candidate["trial_policy"] == f.C.FAST_TRIAL_POLICY
    fresh = f.C.verify(candidate, [fresh_entry], str(f.app), str(f.root / "fast-fresh"))
    complete = f.C.heldout(candidate, fresh, [heldout_entry], str(f.app), str(f.root / "fast-heldout"))
    assert f.C.proof_status(candidate, complete, f.target)[0] is True
    assert complete["measurement"]["phase_counts"] == {"learning": 1, "fresh": 1, "heldout": 1}
    assert complete["measurement"]["trial_policy"] == f.C.FAST_TRIAL_POLICY
    assert complete["measurement"]["restart_deployment_proven"] is True


@pytest.mark.parametrize("witness_restart", [False, True])
def test_visual_optional_restart_scope_status_and_card_are_evidence_bound(visual_campaign_factory, witness_restart):
    f = visual_campaign_factory(parameterized=True)
    tp = f.C.validate_trial_policy({"preset": "fast", "require_restart": False})
    profile = {**f.profile, "scope": f.C.SCOPE_NO_RESTART}
    assert f.C.validate_profile(profile)["scope"] == f.C.SCOPE_NO_RESTART
    candidate = f.C.learn(profile, [{"actor": f.actor, "case": f.entries["learning"][0]["case"]}],
                          str(f.app), str(f.root / "optional-learn"), trial_policy=tp)
    assert candidate["scope"] == f.C.SCOPE_NO_RESTART
    with pytest.raises(ValueError, match="scope differs"):
        f.C.validate({**candidate, "scope": f.C.SCOPE})
    fresh = f.C.verify(candidate, [f.entries["fresh"][2]], str(f.app), str(f.root / "optional-fresh"))
    assert fresh["measurement"]["scope"] == f.C.SCOPE_NO_RESTART
    if not witness_restart:
        record = f.records[6]
        record["entry"]["restart"] = None
        record["case"]["deployment"] = copy.deepcopy(f.records[5]["case"]["deployment"])
        record["measurement"]["identity"] = copy.deepcopy(f.old_identity)
        record["measurement"]["kernel"] = copy.deepcopy(f.records[5]["measurement"]["kernel"])
        f.replace_case(6)
    complete = f.C.heldout(candidate, fresh, [f.entries["heldout"][0]], str(f.app), str(f.root / "optional-heldout"))
    assert complete["measurement"]["restart_deployment_proven"] is witness_restart
    assert complete["measurement"]["scope"] == (f.C.SCOPE if witness_restart else f.C.SCOPE_NO_RESTART)
    ready, reason = f.C.proof_status(candidate, complete, f.target)
    assert ready and ("and witnessed restart" if witness_restart else "without witnessed restart") in reason
    card = A.describe(candidate, complete, str(f.app), "Change owned label", "The measured visible transition.", str(f.root / "optional-card"))
    assert card["contract"]["scope"] == f.C.SCOPE_NO_RESTART
    assert card["contract"]["trial_policy"] == tp
    assert card["contract"]["verification_tier"] == f.C.TIER
    candidate_path, proof_path = f.root / "optional-candidate.json", f.root / "optional-proof.json"
    R.write_json(candidate_path, candidate); R.write_json(proof_path, complete)
    entry = {"candidate": str(candidate_path), "proof": str(proof_path),
             "card": str(f.root / "optional-card/action.json"), "bundle": str(f.app)}
    assert A._entry(entry)[2] == card
    card["contract"]["scope"] = f.C.SCOPE
    R.write_json(entry["card"], card)
    with pytest.raises(ValueError, match="measured contract changed"):
        A._entry(entry)


def test_visual_legacy_contract_and_campaign_keep_omitted_policy_shape(visual_campaign_factory):
    f = visual_campaign_factory()
    candidate, complete = f.promote()
    assert "trial_policy" not in candidate and "trial_policy" not in complete["measurement"]
    assert complete["measurement"]["scope"] == candidate["scope"] == f.C.SCOPE
    contract = A._contract(candidate)
    assert set(contract) == {"effect", "item_kind", "mode", "params", "scope", "reference", "policy", "verification_tier"}
