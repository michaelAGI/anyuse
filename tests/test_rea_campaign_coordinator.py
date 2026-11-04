"""Deterministic lifecycle over real G/C/H/A gates and inert PB/RR/public effects."""
import copy
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_rea_state_campaign import campaign, pinned_json
from test_rea_runtime_bridge import runtime
from test_rea_playback_campaign import visual_campaign_factory
from anyuse.execution import rea_campaign_coordinator as Q
from anyuse.execution import rea_public_campaign as U
from anyuse.execution import rea_agent as G
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_state_campaign as C


@pytest.fixture
def visual_coordinated(visual_campaign_factory, tmp_path, monkeypatch):
    """Real G/VC/A promotion; only actual app/visual/restart effects are inert."""
    f = visual_campaign_factory(parameterized=True)
    _, effect = Q._visual_modules()
    rea = tmp_path / "visual-inert-rea"; rea.write_text("never executed"); rea.chmod(0o700)
    plan = {"format": f.C.PLAN}
    for role, phase in (("learning", "learning"), ("verification", "fresh"), ("heldout", "heldout")):
        plan[role] = [{"id": row["case"]["id"], "inputs": row["case"]["inputs"],
                       "restart": role == "heldout" and i == 0}
                      for i, row in enumerate([r for r in f.records if r["case"]["phase"] == phase])]
    root = tmp_path / "visual-session"
    G.begin(str(f.app), str(rea), "Set the requested visible label", f.profile["params"],
            ["The owned label must match the input"], plan, str(root),
            observer_profile={k: copy.deepcopy(v) for k, v in f.profile.items() if k != "evidence"})
    observer = G.context(root)["observer_profile_sha256"]
    result = G.step(root, {"op": "propose", "observer_profile_sha256": observer,
        "rationale": "Measured typed actor remains a hypothesis until independent trials", "actor": f.actor,
        "citations": [{"artifact": "inventory.json", "pointer": "/target"}]})
    assert result["feedback"]["status"] == "completed"
    document = tmp_path / "owned-document"; document.write_text("original owned bytes")
    env = SimpleNamespace(f=f, root=root, out=tmp_path / "visual-coordinator", identity=copy.deepcopy(f.old_identity),
                          calls=[], intents=[], fail_precondition=False, fail_after_dispatch=False, checkpoints=0, model_counter=0)
    from anyuse.execution import model_adapter as models
    monkeypatch.setattr(models, "process_call_count", lambda: env.model_counter)
    monkeypatch.setattr(Q.B, "process_identity", lambda *a: copy.deepcopy(env.identity))
    monkeypatch.setattr(Q, "_available_bytes", lambda path: 16 * 1024**3)
    def intent(bundle, policy, identifier, phase, inputs, *, expected_identity=None):
        assert expected_identity == env.identity
        value = {"format": "inert actual visual intent", "id": identifier, "phase": phase,
                 "inputs": copy.deepcopy(inputs), "policy_sha256": R.digest(policy),
                 "deployment": {"identity": copy.deepcopy(env.identity)}, "observed_monotonic": time.monotonic()}
        env.intents.append(copy.deepcopy(value)); return value
    def validate_intent(policy, value):
        assert value["policy_sha256"] == R.digest(policy)
        return copy.deepcopy(value)
    def trial(bundle, actor, policy, actual_intent, out, *, timeout, expected_identity=None, **kwargs):
        assert actual_intent == env.intents[-1] and expected_identity == env.identity
        assert actor == f.actor
        env.model_counter += 1
        if env.fail_precondition:
            return {"status": "refused", "error": {"message": "actual before judgment rejected readiness"}}
        env.calls.append(copy.deepcopy(actual_intent))
        if env.fail_after_dispatch:
            raise R.ResourceReviewRequired("inert actual dispatch cleanup unknown")
        env.model_counter += 1
        if actual_intent["phase"] == "replay":
            original = f.records[0]
            case = copy.deepcopy(original["case"]); case.update(id="later-replay-" + str(len(env.calls)), phase="replay",
                inputs=copy.deepcopy(actual_intent["inputs"]),
                deployment={"id": "deployment-" + str(env.identity["pid"]), "kind": "existing_process", "identity": copy.deepcopy(env.identity)})
            folder = Path(out); folder.mkdir()
            case_pin = pinned_json(folder / "case.json", case)
            measured = copy.deepcopy(original["measurement"])
            measured.update(case_id=case["id"], phase="replay", inputs=case["inputs"],
                actor_sha256=R.digest(effect.bind_actor(actor, policy, case["inputs"])), case_sha256=R.digest(case),
                identity=copy.deepcopy(env.identity), kernel=f.records[-1]["measurement"]["kernel"])
            f.measurements[case["id"]] = measured
        else:
            record = next(row for row in f.records if row["case"]["id"] == actual_intent["id"])
            case, case_pin, measured = record["case"], f.pin(record["path"]), record["measurement"]
        measured.update(started_monotonic=time.monotonic(), finished_monotonic=time.monotonic())
        measured["finished_monotonic"] = max(measured["finished_monotonic"], measured["started_monotonic"] + 1e-9)
        return {"status": "model_judged", "verification_tier": "model_judged", "error": None,
                "case": copy.deepcopy(case), "case_file": case_pin, "measurement": copy.deepcopy(measured), "model_calls": 2}
    monkeypatch.setattr(effect, "make_intent", intent)
    monkeypatch.setattr(effect, "validate_intent", validate_intent)
    monkeypatch.setattr(effect, "run_trial", trial)
    cfg = {"format": Q.VISUAL_CONFIG, "session": str(root), "target": f.target, "plan_sha256": R.digest(plan),
        "observer_profile_sha256": observer, "owned": True, "expected_identity": copy.deepcopy(f.old_identity),
        "resources": [Q._pin(document)], "limits": {"seconds": 1800, "checkpoint_seconds": 300,
        "trial_seconds": 120, "max_actions": 32}, "publication": {"summary": "Set visible label",
        "details": "Retained model-judged visible effect on frozen trials with ordinary restart"},
        "selection_task": "Set the requested visible label", "replays": [{"id": "later-0", "inputs": {"query": "later-value"}}]}
    env.cfg = cfg
    def checkpoint(journal, *, change=None):
        pending = journal["pending"]; env.checkpoints += 1; started = time.monotonic()
        evidence = pinned_json(tmp_path / f"public-observation-{env.checkpoints}.json",
                               {"observed": "inert public computer-use readiness", "case": pending["action"]})
        value = {"format": Q.VISUAL_CHECKPOINT, "request_sha256": R.digest(pending), "kind": pending["kind"],
                 "status": "ready", "identity": copy.deepcopy(env.identity), "evidence": [evidence],
                 "started_monotonic": started, "finished_monotonic": time.monotonic()}
        if pending["kind"] == "restart":
            entry = f.entries["heldout"][0]["restart"]
            baseline = R.load_json(entry["receipt"]["file"])["baseline"]
            baseline_pin = pinned_json(baseline["file"], {"started_monotonic": time.monotonic()})
            receipt = R.load_json(entry["receipt"]["file"])
            receipt.update(baseline=baseline_pin, finished_monotonic=time.monotonic())
            entry["receipt"] = pinned_json(entry["receipt"]["file"], receipt)
            env.identity = copy.deepcopy(f.new_identity)
            value.update(identity=copy.deepcopy(env.identity), restart=copy.deepcopy(entry), finished_monotonic=time.monotonic())
        if change: change(value)
        return pinned_json(tmp_path / f"checkpoint-{env.checkpoints}.json", value)["file"]
    env.checkpoint = checkpoint
    def advance(journal):
        return Q.run_visual(root, cfg, env.out, resume=True, checkpoint=checkpoint(journal))
    env.advance = advance
    return env


def test_visual_coordinator_owns_sequence_and_reuses_through_normal_catalog_api(visual_coordinated):
    e = visual_coordinated
    journal = Q.run_visual(e.root, e.cfg, e.out)
    original = copy.deepcopy(journal["pending"])
    repeated = Q.run_visual(e.root, e.cfg, e.out, resume=True)
    assert repeated["pending"] == original and not e.calls
    for _ in range(16):
        if journal["status"] == "completed": break
        assert journal["status"] in {"needs_setup", "needs_restart"}, journal.get("error")
        journal = e.advance(journal)
    assert journal["status"] == "completed", journal.get("error")
    assert len(e.calls) == len(e.intents) == 10 and journal["model_calls"] == 20 and journal["coordinator_model_calls"] == 0
    assert journal["verification_tier"] == "model_judged" and journal["setup_assertions_are_proof"] is False
    assert journal["replay_index"] == 1 and all(row["status"] == "completed" for row in journal["steps"])
    assert [row["kind"] for row in journal["steps"]] == ["record"] * 3 + ["learn"] + ["verify"] * 3 + ["restart"] + ["heldout"] * 3 + ["publish", "select", "replay"]
    state = G._load(e.root)
    assert state["status"] == "published" and state["heldout_passed"] is True
    assert e.calls[:6] and all(row["deployment"]["identity"] == e.f.old_identity for row in e.calls[:6])
    assert all(row["deployment"]["identity"] == e.f.new_identity for row in e.calls[6:])


@pytest.mark.parametrize("change", ["request", "epoch", "case", "evidence"])
def test_visual_checkpoint_substitution_refuses_before_any_actor(visual_coordinated, change):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    def mutate(value):
        if change == "request": value["request_sha256"] = "0" * 64
        elif change == "epoch": value["identity"]["pid"] += 1
        elif change == "case": value["case"] = 2
        else: value["evidence"][0]["sha256"] = "0" * 64
    result = Q.run_visual(e.root, e.cfg, e.out, resume=True, checkpoint=e.checkpoint(journal, change=mutate))
    assert result["status"] == "failed_no_retry" and not e.calls
    with pytest.raises(ValueError): Q.run_visual(e.root, e.cfg, e.out, resume=True)


def test_visual_checkpoint_reuse_and_direct_g_bypass_refuse(visual_coordinated):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    with pytest.raises(ValueError, match="prepared_case"):
        G.step(e.root, {"op": "record", "case": 0}, expected_runtime_identity=e.identity)
    checkpoint = e.checkpoint(journal)
    result = Q.run_visual(e.root, e.cfg, e.out, resume=True, checkpoint=checkpoint)
    assert len(e.calls) == 1 and result["status"] == "needs_setup"
    result = Q.run_visual(e.root, e.cfg, e.out, resume=True, checkpoint=checkpoint)
    assert result["status"] == "failed_no_retry" and len(e.calls) == 1


@pytest.mark.parametrize("failure", ["before", "cleanup"])
def test_visual_readiness_is_not_proof_and_uncertain_dispatch_never_retries(visual_coordinated, failure):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    e.fail_precondition = failure == "before"; e.fail_after_dispatch = failure == "cleanup"
    result = e.advance(journal)
    assert result["status"] == "failed_no_retry" and len(e.calls) == (failure == "cleanup")
    assert result["model_calls"] == 1  # Retain even the failed attempt's actual before-judge call.
    assert G._load(e.root)["status"] == ("resource_review_required" if failure == "cleanup" else "hypothesis_failed")
    with pytest.raises(ValueError): Q.run_visual(e.root, e.cfg, e.out, resume=True)


@pytest.mark.parametrize("failure", ["predecessor", "old_epoch", "chronology"])
def test_visual_restart_is_anchored_before_another_task_call(visual_coordinated, failure):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    for _ in range(6): journal = e.advance(journal)
    assert journal["status"] == "needs_restart" and len(e.calls) == 6
    def mutate(value):
        if failure == "predecessor": value["restart"]["predecessor"] = "visual-learning-0"
        else:
            entry = value["restart"]; receipt = R.load_json(entry["receipt"]["file"])
            if failure == "old_epoch": receipt["old_identity"]["pid"] += 1
            else:
                baseline = receipt["baseline"]
                receipt["baseline"] = pinned_json(baseline["file"], {"started_monotonic": 0.0})
            entry["receipt"] = pinned_json(entry["receipt"]["file"], receipt)
    result = Q.run_visual(e.root, e.cfg, e.out, resume=True, checkpoint=e.checkpoint(journal, change=mutate))
    assert result["status"] == "failed_no_retry" and len(e.calls) == 6


def test_visual_pause_storage_decline_does_not_reset_request(visual_coordinated, monkeypatch):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    original = copy.deepcopy(journal["pending"])
    monkeypatch.setattr(Q, "_available_bytes", lambda path: 8 * 1024**3)
    result = Q.run_visual(e.root, e.cfg, e.out, resume=True)
    assert result["status"] == "failed_no_retry" and result["pending"] == original and not e.calls


def test_visual_per_action_decline_stops_after_actual_trial_and_retains_samples(visual_coordinated, monkeypatch):
    e = visual_coordinated; _, effect = Q._visual_modules()
    space = [16 * 1024**3]
    monkeypatch.setattr(Q, "_available_bytes", lambda path: space[0])
    journal = Q.run_visual(e.root, e.cfg, e.out)
    original = effect.run_trial
    def consume(*args, **kwargs):
        result = original(*args, **kwargs)
        space[0] -= Q.DEFAULT_STORAGE["action_headroom_bytes"] + 1
        return result
    monkeypatch.setattr(effect, "run_trial", consume)
    result = e.advance(journal)
    assert result["status"] == "failed_no_retry" and len(e.calls) == 1
    row = result["steps"][-1]
    assert "per-action headroom" in row["storage"]["error"]["message"]
    assert row["storage"]["before"] and row["storage"]["after"] and row["model_calls"] == 2
    assert sum(result["storage"]["observed_decline_bytes"].values()) == Q.DEFAULT_STORAGE["action_headroom_bytes"] + 1
    assert result["pending"] is None


def test_visual_failed_callback_still_measures_storage_without_hiding_primary_failure(visual_coordinated, monkeypatch):
    e = visual_coordinated; _, effect = Q._visual_modules()
    space = [16 * 1024**3]
    monkeypatch.setattr(Q, "_available_bytes", lambda path: space[0])
    journal = Q.run_visual(e.root, e.cfg, e.out); e.fail_after_dispatch = True
    original = effect.run_trial
    def consume(*args, **kwargs):
        try: return original(*args, **kwargs)
        finally: space[0] -= 16 * 1024**2
    monkeypatch.setattr(effect, "run_trial", consume)
    result = e.advance(journal)
    row = result["steps"][-1]
    assert result["status"] == "failed_no_retry" and len(e.calls) == 1
    assert row["storage"]["after"] and row["storage"]["error"] is None
    assert sum(result["storage"]["observed_decline_bytes"].values()) == 16 * 1024**2
    assert G._load(e.root)["status"] == "resource_review_required" and result["model_calls"] == 1
    assert (Path(row["out"]) / "g-result.json").is_file()


def test_visual_pause_decline_uses_aggregate_bound_without_action_attribution(visual_coordinated, monkeypatch):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    monkeypatch.setattr(Q, "_available_bytes", lambda path: 16 * 1024**3 - 256 * 1024**2)
    result = Q.run_visual(e.root, e.cfg, e.out, resume=True)
    assert result["status"] == "needs_setup" and result["pending"] == journal["pending"] and not e.calls
    assert sum(result["storage"]["observed_decline_bytes"].values()) == 256 * 1024**2


def test_visual_expired_checkpoint_never_issues_a_new_allowance(visual_coordinated, monkeypatch):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    original = copy.deepcopy(journal["pending"])
    monkeypatch.setattr(Q.time, "time", lambda: original["expires_wall"] + 1)
    result = Q.run_visual(e.root, e.cfg, e.out, resume=True)
    assert result["status"] == "failed_no_retry" and result["pending"] == original and not e.calls


def test_visual_concurrent_resume_cannot_consume_a_checkpoint(visual_coordinated):
    import fcntl
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    checkpoint = e.checkpoint(journal)
    with (e.out / "coordinator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            Q.run_visual(e.root, e.cfg, e.out, resume=True, checkpoint=checkpoint)
    assert R.load_json(e.out / "coordinator.json") == journal and not e.calls


@pytest.mark.parametrize("where", ["before_dispatch", "after_dispatch"])
def test_visual_interruption_preserves_consumed_boundary_and_never_retries(visual_coordinated, monkeypatch, where):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    if where == "before_dispatch":
        monkeypatch.setattr(G, "step", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt("inert interruption")))
        with pytest.raises(KeyboardInterrupt): e.advance(journal)
    else:
        save = Q._save
        def fail_result(path, value, **kw):
            if path.name == "result.json": raise OSError("inert failure retaining actual result")
            return save(path, value, **kw)
        monkeypatch.setattr(Q, "_save", fail_result)
        assert e.advance(journal)["status"] == "failed_no_retry"
    saved = R.load_json(e.out / "coordinator.json")
    assert saved["status"] == "failed_no_retry" and saved["pending"] is None
    assert saved["steps"][-1]["status"] == "started" and len(e.calls) == (where == "after_dispatch")
    with pytest.raises(ValueError): Q.run_visual(e.root, e.cfg, e.out, resume=True)


def test_visual_prepared_intent_cannot_substitute_expected_epoch(visual_coordinated, monkeypatch):
    e = visual_coordinated; journal = Q.run_visual(e.root, e.cfg, e.out)
    original = G.step
    def changed_epoch(*args, **kwargs):
        kwargs["expected_runtime_identity"] = {**e.identity, "pid": e.identity["pid"] + 1}
        return original(*args, **kwargs)
    monkeypatch.setattr(G, "step", changed_epoch)
    result = e.advance(journal)
    assert result["status"] == "failed_no_retry" and not e.calls and len(e.intents) == 1
    assert "prepared visual intent differs" in G._load(e.root)["steps"][-1]["feedback"]["error"]


def test_visual_insufficient_budget_is_rejected_before_binding(visual_coordinated):
    e = visual_coordinated; e.cfg["limits"]["trial_seconds"] = 30
    with pytest.raises(ValueError, match="both judges"):
        Q.run_visual(e.root, e.cfg, e.out)
    assert not e.out.exists() and "coordinator" not in G._load(e.root) and not e.calls


def test_visual_coordinate_cli_exposes_data_checkpoint_without_loading_setup_code(visual_coordinated, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution.rea_cli import rea_commands
    e = visual_coordinated; config = pinned_json(tmp_path / "visual-config.json", e.cfg)["file"]
    checkpoint = pinned_json(tmp_path / "observed.json", {"data": "not a callback"})["file"]
    calls = []
    monkeypatch.setattr(Q, "run_visual", lambda *a, **kw: calls.append((a, kw)) or {"status": "needs_setup"})
    result = CliRunner().invoke(rea_commands, ["agent", "coordinate", str(e.root), config, "--out", str(e.out),
        "--resume", "--checkpoint", checkpoint, "--actions-per-run", "2"])
    assert result.exit_code == 0, result.output
    assert calls == [((str(e.root), e.cfg, str(e.out)), {"resume": True, "actions_per_run": 2, "checkpoint": checkpoint})]
    assert not e.calls


@pytest.fixture
def coordinated(campaign, tmp_path, monkeypatch):
    # Only lower public/native/restart measurements are inert. The nine-case
    # state machine, profile binding, aggregation, catalog and readers stay real.
    monkeypatch.setattr(Q, '_available_bytes', lambda path: 16 * 1024 ** 3)
    monkeypatch.setattr(D.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=16*1024**3, f_frsize=1))
    campaign.policy['observer']={'format':'inert declared public observer'}
    rea=tmp_path/'inert-rea';rea.write_text('never executed');rea.chmod(0o700)
    plan={'format':C.PLAN}
    for role,phase in [('learning','learning'),('verification','fresh'),('heldout','heldout')]:
        plan[role]=[{'id':f'{phase}-{i}','inputs':{},'restart':role=='heldout' and i==0} for i in range(3)]
    root=tmp_path/'session'
    G.begin(campaign.app,rea,'Pause current video',[],['Independent moving to stable video and public control'],plan,root,
            observer_profile={k:copy.deepcopy(v) for k,v in campaign.profile.items() if k!='evidence'})
    initial=G.context(root)
    proposed=G.step(root,{'op':'propose','observer_profile_sha256':initial['observer_profile_sha256'],
        'rationale':'Typed measured action is a hypothesis, not proof','actor':campaign.actor,
        'citations':[{'artifact':'inventory.json','pointer':'/target'}]})
    assert proposed['feedback']['status']=='completed'
    state=R.load_json(root/'session.json')
    media=[]
    for i,row in enumerate(campaign.rows):
        path=tmp_path/f'private-media-{i}.mp4';path.write_text(f'UNSEEN_PRIVATE_MEDIA_BYTES_{i}')
        media.append({'id':row['id'],'media':Q._pin(path)})
    producer=tmp_path/'trusted-inert-producer.py';producer.write_text('# Inert authorized fixture producer; never imported from JSON\n')
    producer_pin=Q._pin(producer)
    env=SimpleNamespace(identity=copy.deepcopy(campaign.old),setups=[],stops=[],launches=[],fail_setup=None,
                        mutate_media=False,bad_restart=False,bad_setup_time=None,producer=producer,campaign=campaign,root=root,plan=plan)
    monkeypatch.setattr(Q.B,'process_identity',lambda *a:copy.deepcopy(env.identity))
    def setup(context):
        started=time.monotonic()
        env.setups.append(copy.deepcopy(context))
        if env.fail_setup == len(env.setups):raise ValueError('public setup refused; no retry')
        assert context['expected_identity']==env.identity
        folder=Path(context['out']);folder.mkdir()
        body={'status':'ready','identity':copy.deepcopy(env.identity),'media':context['request']['media'],
              'native_goal_calls':0,'activation_attempts':1,'play_attempts':1,
              'started_monotonic':started,'finished_monotonic':time.monotonic()}
        if env.bad_setup_time=='early':body['started_monotonic']=0.0
        elif env.bad_setup_time=='late':body['finished_monotonic']=1e30
        elif env.bad_setup_time=='nonfinite':body['started_monotonic']='NaN'
        pin=pinned_json(folder/'setup.json',body)
        if env.mutate_media:Path(context['request']['media']).write_text('mutated after public setup')
        return {**body,'journal':pin['file']}
    def stop(context):
        env.stops.append(copy.deepcopy(context));assert context['old_identity']==env.identity
        return {'accepted':True,'native_goal_calls':0}
    def launch(context):
        env.launches.append(copy.deepcopy(context));env.identity=copy.deepcopy(campaign.new)
        return {'application_pid':env.identity['pid'],'native_goal_calls':0}
    backend=Q.TrustedHooks(setup,stop,launch,{name:copy.deepcopy(producer_pin) for name in ('setup','stop','launch')})
    original_trial=campaign.lower.run_trial
    def trial(bundle,actor,policy,intent,out,*,timeout=30,expected_identity=None):
        assert expected_identity==env.identity==intent['deployment']['identity']
        return original_trial(bundle,actor,policy,intent,out,timeout=timeout)
    monkeypatch.setattr(campaign.lower,'run_trial',trial)
    def intent(bundle,policy,case_id,phase,*,expected_identity=None):
        assert expected_identity==env.identity
        index=next(i for i,row in enumerate(campaign.rows) if row['id']==case_id)
        current_media=next(row['media'] for row in media if row['id']==case_id)
        return {'format':'inert actual playback intent','id':case_id,'phase':phase,'inputs':{},
            'policy_sha256':R.digest(policy),'deployment':{'identity':copy.deepcopy(env.identity)},
            'context':{'media':Q._pin(current_media['file']),'window':{'id':100+index}},'observed_monotonic':time.monotonic()}
    def validate_intent(policy,value):
        assert value['policy_sha256']==R.digest(policy)
        return copy.deepcopy(value)
    monkeypatch.setattr(Q.P,'make_intent',intent)
    monkeypatch.setattr(Q.P,'validate_intent',validate_intent)
    def validate_restart(declaration,receipt_pin):
        receipt=json.loads(Q._read(receipt_pin));actual=json.loads(Q._read(receipt['intent']))
        assert actual==declaration and declaration['old_identity']==campaign.old and declaration['target']==state['target']
        baseline=receipt['baseline'];Q._read(baseline)
        value={'restart_measurement_validated':True,'target':state['target'],
            **{key:receipt[key] for key in ('old_identity','new_identity','old_kernel','new_kernel')},
            'files':{p['file']:p for p in (receipt_pin,baseline)}}
        if env.bad_restart:value['old_identity']={**campaign.old,'pid':999}
        return value
    def restart(bundle,expected,declaration,out,*,stop_hook,launch_hook,timeout):
        assert expected==env.identity==campaign.old
        stop_hook({'old_identity':copy.deepcopy(expected),'deadline_monotonic':99999999})
        launch_hook({'old_identity':copy.deepcopy(expected),'deadline_monotonic':99999999})
        folder=Path(out);folder.mkdir()
        intent_pin=pinned_json(folder/'intent.json',declaration)
        body={**copy.deepcopy(campaign.receipt_data),'intent':intent_pin}
        receipt=pinned_json(folder/'receipt.json',body)
        witness=validate_restart(declaration,receipt)
        return {'status':'restart_measurement_validated','receipt':receipt,
                'measurement':pinned_json(folder/'measurement.json',witness),'proof_created':False}
    monkeypatch.setattr(Q.RR,'run_restart',restart)
    monkeypatch.setattr(Q.RR,'validate_restart',validate_restart)
    cfg={'format':Q.CONFIG,'session':str(root),'target':state['target'],'plan_sha256':state['plan_sha256'],
        'observer_profile_sha256':state['observer_profile_sha256'],'owned':True,'expected_identity':copy.deepcopy(campaign.old),
        'cases':media,'launch_environment':{},'limits':{'seconds':1800,'setup_seconds':35,'restart_seconds':30,
        'trial_seconds':30,'max_actions':32},'publication':{'summary':'Pause current playback','details':'Nine actual independent inert test measurements plus ordinary restart witness.'}}
    env.cfg,env.backend=cfg,backend
    return env


def test_automatic_nine_trials_one_restart_and_publication_use_existing_real_gates(coordinated,tmp_path):
    e=coordinated
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=e.backend)
    assert result['status']=='completed',result.get('error')
    assert len(e.setups)==len(e.campaign.calls)==9 and len(e.stops)==len(e.launches)==1
    assert result['model_calls']==0 and result['automatic_retry'] is False
    assert [row['kind'] for row in result['steps']].count('prepare')==9
    assert len(result['steps'])==21 and all(row['status']=='completed' for row in result['steps'])
    state=R.load_json(e.root/'session.json')
    assert state['status']=='published' and state['heldout_passed'] is True
    assert len(R.load_json(state['published'])['actions'])==1
    actual=e.setups[6]
    assert actual['expected_identity']==e.campaign.new and all(row['expected_identity']==e.campaign.old for row in e.setups[:6])
    entry=state['heldout_recordings']['0']['restart']
    assert entry['predecessor']=='fresh-2' and R.load_json(entry['intent']['file'])['old_identity']==e.campaign.old
    public=G.context(e.root)
    assert 'prepared_case' not in json.dumps(public) and '"restart": {' not in json.dumps(public)


@pytest.mark.parametrize('boundary',[1,2,7,14,15])
def test_completed_boundary_resume_never_repeats_preparation_or_restart(coordinated,tmp_path,boundary):
    e=coordinated;out=tmp_path/'coordinator'
    first=Q.run(e.root,e.cfg,out,backend=e.backend,actions_per_run=boundary)
    assert first['status']=='ready',first.get('error')
    before=len(e.setups)
    result=Q.run(e.root,e.cfg,out,backend=e.backend,resume=True)
    assert result['status']=='completed',result.get('error')
    assert len(e.setups)==len(e.campaign.calls)==9 and len(e.stops)==len(e.launches)==1
    assert len(result['steps'])==21 and before<=9


@pytest.mark.parametrize('failure',['setup','native_cleanup','restart'])
def test_partial_failure_never_retries_or_overacts_even_with_new_output(coordinated,tmp_path,failure):
    e=coordinated;out=tmp_path/'coordinator'
    if failure=='setup':e.fail_setup=1
    elif failure=='native_cleanup':e.campaign.failure_result={'status':'failed','resource_review_required':True,'error':{'message':'cleanup unknown'}}
    else:e.bad_restart=True
    result=Q.run(e.root,e.cfg,out,backend=e.backend)
    assert result['status']=='failed_no_retry'
    calls=(len(e.setups),len(e.campaign.calls),len(e.stops),len(e.launches))
    if failure=='restart':assert calls==(6,6,1,1)
    elif failure=='setup':assert calls==(1,0,0,0)
    else:assert calls==(1,1,0,0)
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,out,backend=e.backend,resume=True)
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,tmp_path/'retry-output',backend=e.backend)
    assert calls==(len(e.setups),len(e.campaign.calls),len(e.stops),len(e.launches))


@pytest.mark.parametrize('drift',['process','media','producer','session'])
def test_resume_drift_stops_before_another_public_or_native_action(coordinated,tmp_path,drift):
    e=coordinated;out=tmp_path/'coordinator'
    first=Q.run(e.root,e.cfg,out,backend=e.backend,actions_per_run=2)
    assert first['status']=='ready'
    if drift=='process':e.identity={**e.identity,'pid':999}
    elif drift=='media':Path(e.cfg['cases'][1]['media']['file']).write_text('changed future media')
    elif drift=='producer':e.producer.write_text('changed trusted producer')
    else:
        state=R.load_json(e.root/'session.json');state['external_change']=True;R.write_json(e.root/'session.json',state)
    try:result=Q.run(e.root,e.cfg,out,backend=e.backend,resume=True)
    except ValueError:pass
    else:assert result['status']=='failed_no_retry'
    assert len(e.setups)==len(e.campaign.calls)==1 and not e.stops


@pytest.mark.parametrize('change',['request','receipt','epoch'])
def test_prepared_case_cannot_be_forged_reordered_or_stale(coordinated,tmp_path,change):
    e=coordinated;out=tmp_path/'coordinator'
    first=Q.run(e.root,e.cfg,out,backend=e.backend,actions_per_run=1)
    assert first['status']=='ready' and len(e.setups)==1 and not e.campaign.calls
    raw=R.load_json(first['steps'][-1]['result']['file'])['prepared']
    active=R.load_json(out/'coordinator.json')
    active['status']='running'
    active['steps'].append({'kind':'record','status':'started','session_before_sha256':active['session_sha256']})
    R.write_json(out/'coordinator.json',active)
    request={'op':'record','case':0,'timeout':30}
    if change=='request':request['case']=1
    elif change=='receipt':Path(raw['file']).write_text('{}')
    else:e.identity={**e.identity,'pid':999}
    with pytest.raises(ValueError):G.step(e.root,request,prepared_case=Q.PreparedCase(raw))
    assert not e.campaign.calls and len(e.setups)==1


def test_changed_media_during_public_setup_is_not_a_native_task_input_or_retry(coordinated,tmp_path):
    e=coordinated;e.mutate_media=True
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=e.backend)
    assert result['status']=='failed_no_retry' and len(e.setups)==1 and not e.campaign.calls


@pytest.mark.parametrize('module',['P','RR','V'])
def test_changed_policy_source_refuses_resume_before_any_additional_setup_or_native(coordinated,tmp_path,monkeypatch,module):
    e=coordinated;out=tmp_path/'coordinator'
    first=Q.run(e.root,e.cfg,out,backend=e.backend,actions_per_run=1)
    assert first['status']=='ready' and len(e.setups)==1 and not e.campaign.calls
    assert first['sources']['public_readiness']==Q._pin(Path(Q.V.__file__).resolve(strict=True))
    changed=tmp_path/f'changed-{module}.py';changed.write_text('# Changed implementation, never executed\n')
    monkeypatch.setattr(getattr(Q,module),'__file__',str(changed))
    with pytest.raises(ValueError,match='drift'):Q.run(e.root,e.cfg,out,backend=e.backend,resume=True)
    assert len(e.setups)==1 and not e.campaign.calls and not e.stops


@pytest.mark.parametrize('timing',['early','late','nonfinite'])
def test_public_setup_chronology_must_belong_to_actual_prepare_action(coordinated,tmp_path,timing):
    e=coordinated;e.bad_setup_time=timing
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=e.backend)
    assert result['status']=='failed_no_retry' and 'chronology' in result['error']['message']
    assert len(e.setups)==1 and not e.campaign.calls and not e.stops


def test_finite_action_budget_stops_without_unbounded_setup(coordinated,tmp_path):
    e=coordinated;e.cfg['limits']['max_actions']=1
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=e.backend)
    assert result['status']=='failed_no_retry' and len(e.setups)==1 and not e.campaign.calls


@pytest.mark.parametrize('field',['owned','plan_sha256','observer_profile_sha256','target','unknown','callbacks'])
def test_bad_or_unapproved_configuration_refuses_before_outputs_or_actions(coordinated,tmp_path,field):
    e=coordinated;cfg=copy.deepcopy(e.cfg)
    if field=='owned':cfg[field]=False
    elif field in {'plan_sha256','observer_profile_sha256'}:cfg[field]='0'*64
    elif field=='target':cfg[field]={**cfg[field],'build':'other'}
    else:cfg[field]='module.function is never interpreted'
    out=tmp_path/'rejected'
    with pytest.raises(ValueError):Q.run(e.root,cfg,out,backend=e.backend)
    assert not out.exists() and not e.setups and not e.campaign.calls


def test_python_hooks_and_prepared_objects_are_not_deserialized_from_model_json(coordinated,tmp_path):
    e=coordinated
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,tmp_path/'bad-backend',backend={'setup':'module.function'})
    with pytest.raises(ValueError):G.step(e.root,{'op':'record','case':0},prepared_case={'receipt':{}})
    assert not e.setups and not e.campaign.calls


def test_fixed_first_party_launch_storage_is_explicit_and_never_overrides_home(tmp_path,monkeypatch):
    directories={name:str(tmp_path/name) for name in ('CFFIXED_USER_HOME','TMPDIR','XDG_CACHE_HOME')}
    for value in directories.values():Path(value).mkdir()
    calls=[]
    monkeypatch.setattr(U,'_open',lambda *args,**kwargs:calls.append((args,kwargs)) or {'application_pid':42})
    backend=U.PublicPlayback(directories)
    context={'old_identity':{'pid':11},'deadline_monotonic':10}
    assert backend.launch(context)=={'application_pid':42}
    assert len(calls)==1 and calls[0][1]=={'new_instance':True,'ignore_saved_state':True,'environment':directories}
    assert 'HOME' not in calls[0][1]['environment']
    with pytest.raises(TypeError):backend.launch_environment['HOME']=str(tmp_path)
    with pytest.raises(ValueError):U.PublicPlayback({'HOME':str(tmp_path)})
    with pytest.raises(ValueError):U.PublicPlayback({'TMPDIR':str(tmp_path/'missing')})
    with pytest.raises(ValueError):U.PublicPlayback({'SHELL':'generated code is not configuration'})


def test_coordinate_cli_only_calls_fixed_backend_api_with_private_configuration(coordinated,tmp_path,monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution.rea_cli import rea_commands
    e=coordinated;file=tmp_path/'caller-private.json';R.write_json(file,e.cfg)
    calls=[]
    def run(*args,**kwargs):
        calls.append((args,kwargs));return {'status':'ready','model_calls':0}
    monkeypatch.setattr(Q,'run',run)
    result=CliRunner().invoke(rea_commands,['agent','coordinate',str(e.root),str(file),'--out',str(tmp_path/'coord'),
        '--resume','--actions-per-run','2'])
    assert result.exit_code==0,result.output
    assert len(calls)==1 and calls[0][0]==(str(e.root),e.cfg,str(tmp_path/'coord'))
    assert calls[0][1]=={'resume':True,'actions_per_run':2}
    assert not e.setups and not e.campaign.calls


def test_resource_size_bound_is_checked_before_open_and_media_is_streamed(tmp_path,monkeypatch):
    file=tmp_path/'bounded-media';file.write_bytes(b'0123456789abcdef')
    opened=[]
    real_open=Q.os.open
    def open_file(*args,**kwargs):
        opened.append(args[0]);return real_open(*args,**kwargs)
    monkeypatch.setattr(Q.os,'open',open_file)
    with pytest.raises(ValueError,match='byte bound'):Q._pin(file,maximum=8)
    assert opened==[]
    pin=Q._pin(file,maximum=32)
    def whole_read(*args,**kwargs):pytest.fail('media verification must not allocate whole-file read_bytes')
    monkeypatch.setattr(Path,'read_bytes',whole_read)
    Q._verify(pin,maximum=32)
    assert len(opened)==2


def test_resource_symlink_or_noncanonical_alias_is_refused_before_consumption(tmp_path):
    file=tmp_path/'actual';file.write_text('resource')
    alias=tmp_path/'alias';alias.symlink_to(file)
    with pytest.raises(ValueError,match='canonical'):Q._pin(alias)
    with pytest.raises(ValueError,match='canonical'):Q._pin(str(tmp_path/'..'/tmp_path.name/'actual'))


@pytest.mark.parametrize('bad', [None, {}, {'reserve_bytes': True, 'action_headroom_bytes': 1048576,
    'max_observed_decline_bytes': 1048576}, {'reserve_bytes': 1048576, 'action_headroom_bytes': 2097152,
    'max_observed_decline_bytes': 1048576}])
def test_storage_configuration_is_closed_finite_and_explicit(coordinated, tmp_path, bad):
    e=coordinated;e.cfg['storage']=bad;out=tmp_path/'coordinator'
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,out,backend=e.backend)
    assert not out.exists() and not e.setups and not e.campaign.calls


def test_storage_refusal_is_retained_before_any_backend_call(coordinated,tmp_path,monkeypatch):
    e=coordinated;available=Q.DEFAULT_STORAGE['reserve_bytes']+Q.DEFAULT_STORAGE['action_headroom_bytes']-1
    monkeypatch.setattr(Q,'_available_bytes',lambda path:available)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=e.backend)
    assert result['status']=='failed_no_retry' and len(result['steps'])==1
    row=result['steps'][0]
    assert row['status']=='not_dispatched' and row['storage']['before']['filesystems'][0]['available_bytes']==available
    assert row['storage']['after'] is None and row['result'] is None and not Path(row['out']).exists()
    assert not e.setups and not e.campaign.calls and not e.stops and not e.launches


def test_unknown_storage_measurement_stops_before_backend(coordinated,tmp_path,monkeypatch):
    e=coordinated
    def failed(path):raise OSError('inert storage probe failed')
    monkeypatch.setattr(Q,'_available_bytes',failed)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=e.backend)
    assert result['status']=='failed_no_retry' and result['steps'][0]['status']=='not_dispatched'
    assert result['steps'][0]['storage']['error']['type']=='OSError'
    assert not e.setups and not e.campaign.calls


def test_action_decline_stops_after_actual_setup_without_trial_or_retry(coordinated,tmp_path,monkeypatch):
    e=coordinated;calls=[];out=tmp_path/'coordinator'
    def free(path):
        calls.append(path)
        return 2*1024**3 if len(calls)==1 else 2*1024**3-Q.DEFAULT_STORAGE['action_headroom_bytes']-1
    monkeypatch.setattr(Q,'_available_bytes',free)
    result=Q.run(e.root,e.cfg,out,backend=e.backend)
    assert result['status']=='failed_no_retry' and len(e.setups)==1 and not e.campaign.calls
    row=result['steps'][0]
    assert row['status']=='failed' and row['result'] and row['storage']['after']
    assert 'per-action headroom' in row['storage']['error']['message']
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,out,backend=e.backend,resume=True)
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,tmp_path/'other',backend=e.backend)
    assert len(e.setups)==1 and not e.campaign.calls


def test_failed_backend_keeps_original_error_and_measures_storage(coordinated,tmp_path,monkeypatch):
    e=coordinated;e.fail_setup=1;calls=[]
    def free(path):
        calls.append(path)
        if len(calls)>1:raise OSError('post-failure storage unknown')
        return 2*1024**3
    monkeypatch.setattr(Q,'_available_bytes',free)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=e.backend)
    row=result['steps'][0]
    assert result['status']=='failed_no_retry' and len(calls)==2
    assert row['error']['message']=='public setup refused; no retry'
    assert row['storage']['error']['message']=='post-failure storage unknown'
    assert len(e.setups)==1 and not e.campaign.calls


def test_resume_counts_gap_decline_and_refuses_before_trial(coordinated,tmp_path,monkeypatch):
    e=coordinated;out=tmp_path/'coordinator'
    e.cfg['storage']={'reserve_bytes':512*Q._MIB,'action_headroom_bytes':128*Q._MIB,
                      'max_observed_decline_bytes':256*Q._MIB}
    monkeypatch.setattr(Q,'_available_bytes',lambda path:2*1024**3)
    first=Q.run(e.root,e.cfg,out,backend=e.backend,actions_per_run=1)
    assert first['status']=='ready' and len(e.setups)==1 and not e.campaign.calls
    monkeypatch.setattr(Q,'_available_bytes',lambda path:2*1024**3-200*Q._MIB)
    result=Q.run(e.root,e.cfg,out,backend=e.backend,resume=True)
    assert result['status']=='failed_no_retry' and result['steps'][-1]['status']=='not_dispatched'
    assert list(result['storage']['observed_decline_bytes'].values())==[200*Q._MIB]
    assert len(e.setups)==1 and not e.campaign.calls


@pytest.mark.parametrize('tamper',['counter','policy','binding','sample'])
def test_resume_rejects_storage_rebinding_or_counter_tamper(coordinated,tmp_path,tamper):
    e=coordinated;out=tmp_path/'coordinator'
    first=Q.run(e.root,e.cfg,out,backend=e.backend,actions_per_run=1)
    assert first['status']=='ready'
    journal=R.load_json(out/'coordinator.json')
    if tamper=='counter':
        device=next(iter(journal['storage']['observed_decline_bytes']))
        journal['storage']['observed_decline_bytes'][device]+=1
    elif tamper=='policy':journal['storage']['policy']['reserve_bytes']+=1
    elif tamper=='binding':journal['storage']['binding']['directories'][0]['inode']+=1
    else:journal['storage']['last_sample']['filesystems'][0]['available_bytes']-=1
    R.write_json(out/'coordinator.json',journal)
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,out,backend=e.backend,resume=True)
    assert len(e.setups)==1 and not e.campaign.calls



def test_interrupt_and_failed_error_retention_keep_interrupt_primary_and_unresumable(coordinated,tmp_path,monkeypatch):
    e=coordinated;out=tmp_path/'coordinator'
    def interrupted(context):
        e.setups.append(context)
        raise KeyboardInterrupt('inert interruption after action began')
    backend=Q.TrustedHooks(interrupted,e.backend.stop,e.backend.launch,e.backend.producers)
    original_save=Q._save
    def save(path,value,**kwargs):
        if path.name=='coordinator.json' and value.get('status')=='failed_no_retry':
            raise OSError('inert final journal disk failure')
        return original_save(path,value,**kwargs)
    monkeypatch.setattr(Q,'_save',save)
    with pytest.raises(KeyboardInterrupt,match='inert interruption') as caught:
        Q.run(e.root,e.cfg,out,backend=backend)
    assert isinstance(caught.value.__cause__,OSError)
    retained=R.load_json(out/'coordinator.json')
    assert retained['status']=='running' and retained['steps'][-1]['status']=='started'
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,out,backend=backend,resume=True)
    assert len(e.setups)==1 and not e.campaign.calls


def test_directory_rebinding_after_callback_stops_before_trial(coordinated,tmp_path,monkeypatch):
    e=coordinated;out=tmp_path/'coordinator';original=Q._storage_bindings
    def binding(*args):
        value=original(*args)
        if e.setups:value['directories'][0]['inode']+=1
        return value
    monkeypatch.setattr(Q,'_storage_bindings',binding)
    result=Q.run(e.root,e.cfg,out,backend=e.backend)
    assert result['status']=='failed_no_retry' and len(e.setups)==1 and not e.campaign.calls
    assert 'binding changed' in result['steps'][0]['storage']['error']['message']



def test_campaign_storage_decline_cap_is_aggregate_across_filesystems():
    policy={'reserve_bytes':100,'action_headroom_bytes':100,'max_observed_decline_bytes':150}
    before={'filesystems':[{'device':1,'available_bytes':1000},{'device':2,'available_bytes':1000}]}
    after={'filesystems':[{'device':1,'available_bytes':920},{'device':2,'available_bytes':920}]}
    declines={'1':0,'2':0}
    with pytest.raises(ValueError,match='aggregate observed storage decline'):
        Q._storage_account(before,after,policy,declines)
    assert declines=={'1':80,'2':80}
    assert [row['observed_decline_bytes'] for row in after['filesystems']]==[80,80]
    with pytest.raises(ValueError,match='aggregate observed-decline budget'):
        Q._storage_admit(before,policy,{'1':30,'2':30})


from anyuse.execution import rea_document_restoration as D


def _document_case(tmp_path, identity, target):
    source=tmp_path/'readonly-original.txt';source.write_bytes(b'baseline document\n')
    workspace=D.create_workspace(D.pin(source),tmp_path/'owned-document-workspace')
    document=Path(json.loads(D._verified(workspace))['document']['file'])
    producer=tmp_path/'inert-document-adapter.py';producer.write_text('# Explicit trusted inert adapter; never imported\n')
    env=SimpleNamespace(identity=copy.deepcopy(identity),open=True,edited=False,position=[10.,20.],size=[400.,300.],
                        value=0.5,calls=[],fail_close=False,ignore_close=False,fail_reopen=False,ignore_field=False,
                        interrupt=False,source=source,document=document,workspace=workspace)
    field={'id':'level','path':[0],'role':'AXSlider','identifier':'fixture-level','restore':True}
    def observe(context):
        assert context['document']==str(document)
        return {'identity':copy.deepcopy(env.identity),'document':str(document),'open':env.open,
                'edited':env.edited if env.open else None,
                'window':{'position':copy.deepcopy(env.position),'size':copy.deepcopy(env.size)} if env.open else None,
                'fields':{'level':{'value':env.value,'settable':True}} if env.open else {}}
    def close(context):
        env.calls.append('close')
        if env.interrupt:raise KeyboardInterrupt('inert document close interrupted')
        if env.fail_close:raise ValueError('inert ordinary close refused')
        if not env.ignore_close:env.open=False
    def reopen(context):
        env.calls.append('reopen')
        if env.fail_reopen:raise ValueError('inert ordinary reopen refused')
        env.open=True
    def set_field(context,field,value):
        env.calls.append('set:'+field['id'])
        if env.ignore_field:return
        if field['id']=='window.position':env.position=copy.deepcopy(value)
        elif field['id']=='window.size':env.size=copy.deepcopy(value)
        else:env.value=copy.deepcopy(value)
    producer_pin=D.pin(producer)
    env.backend=D.TrustedHooks(observe,close,reopen,set_field,{key:copy.deepcopy(producer_pin) for key in ('observe','close','reopen','set_field')})
    env.checkpoint=D.capture(workspace,env.identity,target,[field],tmp_path/'document-checkpoint',backend=env.backend)
    return env


@pytest.fixture
def document_recovery(campaign,tmp_path,monkeypatch):
    monkeypatch.setattr(D.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=16*1024**3, f_frsize=1))
    identity={**campaign.old,'uid':os.getuid()}
    return _document_case(tmp_path,identity,campaign.policy['target'])


def test_document_recovery_restores_real_owned_bytes_and_declared_state_without_original_write(document_recovery,tmp_path):
    e=document_recovery
    initial_source=D.pin(e.source)
    # Normal Save may replace the owned leaf's inode: it remains supported.
    saved=e.document.with_name('normal-save.txt');saved.write_bytes(b'changed saved contents\n');os.replace(saved,e.document)
    e.position=[55.,66.];e.size=[500.,600.];e.value=0.8
    result=D.restore(e.checkpoint,D.current_document(e.checkpoint),tmp_path/'recovery',backend=e.backend)
    assert result['status']=='restored' and result['files_restored'] and result['app_state_restored']
    assert e.document.read_bytes()==e.source.read_bytes()==b'baseline document\n'
    assert D.pin(e.source)==initial_source and e.position==[10.,20.] and e.size==[400.,300.] and e.value==0.5
    assert e.calls==['close','reopen','set:window.position','set:window.size','set:level']
    assert 'unsaved_document_buffers' in result['unsupported'] and result['scope']['other_windows'].startswith('excluded')
    D.verify_restored(e.checkpoint,result['receipt'])
    # Historical catalog evidence must not freeze the working copy forever.
    e.document.write_bytes(b'a legitimate subsequent task\n')
    D.verify_restored(e.checkpoint,result['receipt'])


@pytest.mark.parametrize('failure',['dirty','no_close','reopen','field'])
def test_document_recovery_requires_actual_close_and_readback(document_recovery,tmp_path,failure):
    e=document_recovery;e.document.write_bytes(b'changed saved contents');e.value=0.8
    if failure=='dirty':e.edited=True
    elif failure=='no_close':e.ignore_close=True
    elif failure=='reopen':e.fail_reopen=True
    else:e.ignore_field=True
    result=D.restore(e.checkpoint,D.current_document(e.checkpoint),tmp_path/'recovery',backend=e.backend)
    assert result['status'] in {'failed','partial','unknown'} and not result['app_state_restored']
    if failure in {'dirty','no_close'}:
        assert e.document.read_bytes()==b'changed saved contents' and not result['files_restored']
    else:assert result['files_restored'] and e.document.read_bytes()==b'baseline document\n'
    if failure=='dirty':assert e.calls==[]
    with pytest.raises(ValueError,match='incomplete'):D.verify_restored(e.checkpoint,result['receipt'])


@pytest.mark.parametrize('drift',['external','symlink','hardlink','directory','epoch'])
def test_document_recovery_refuses_unowned_changed_or_rebound_destinations(document_recovery,tmp_path,drift):
    e=document_recovery;e.document.write_bytes(b'first post-task version')
    frozen=D.current_document(e.checkpoint)
    untouched=tmp_path/'unrelated-user-file';untouched.write_bytes(b'user bytes')
    if drift=='external':e.document.write_bytes(b'external later edit')
    elif drift in {'symlink','hardlink'}:
        e.document.unlink()
        if drift=='symlink':e.document.symlink_to(untouched)
        else:os.link(untouched,e.document)
    elif drift=='directory':
        parent=e.document.parent;parent.rename(parent.with_name('old-working'));parent.mkdir(mode=0o700)
        e.document.write_bytes(b'first post-task version')
    else:e.identity={**e.identity,'pid':999}
    try:result=D.restore(e.checkpoint,frozen,tmp_path/'recovery',backend=e.backend,identity=e.identity)
    except ValueError:pass
    else:assert result['status']=='failed' and not result['files_restored']
    assert e.calls==[] and untouched.read_bytes()==b'user bytes' and e.source.read_bytes()==b'baseline document\n'


def test_document_interrupt_retains_receipt_and_propagates_primary_without_retry(document_recovery,tmp_path):
    e=document_recovery;e.document.write_bytes(b'changed');e.interrupt=True;out=tmp_path/'recovery'
    with pytest.raises(KeyboardInterrupt,match='inert document close interrupted'):
        D.restore(e.checkpoint,D.current_document(e.checkpoint),out,backend=e.backend)
    receipt=R.load_json(out/'result.json')
    assert receipt['interrupted'] and receipt['status']=='unknown' and receipt['error']['type']=='KeyboardInterrupt'
    assert not receipt['files_restored'] and e.calls==['close'] and e.document.read_bytes()==b'changed'
    with pytest.raises(ValueError,match='no automatic retry'):
        D.restore(e.checkpoint,D.current_document(e.checkpoint),out,backend=e.backend)
    assert e.calls==['close']


def _arm_document_recovery(e,tmp_path):
    d=_document_case(tmp_path,e.identity,e.cfg['target'])
    e.cfg['restoration']={'checkpoint':d.checkpoint,'seconds':30}
    original_setup=e.backend.setup
    def setup(context):
        result=original_setup(context)
        d.document.write_bytes(b'task saved new contents');d.value=0.8
        return result
    def observe(context):
        d.identity=copy.deepcopy(e.identity)
        return d.backend.observe(context)
    backend=D.TrustedHooks(observe,d.backend.close,d.backend.reopen,d.backend.set_field,d.backend.producers)
    public=Q.TrustedHooks(setup,e.backend.stop,e.backend.launch,e.backend.producers)
    return d,backend,public


def test_coordinator_restores_before_publication_and_catalog_binds_historical_receipts(coordinated,tmp_path):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='completed',result.get('error')
    assert result['primary_outcome']['status']=='completed' and result['restoration']['status']=='restored'
    assert d.calls==['close','reopen','set:level'] and d.document.read_bytes()==b'baseline document\n'
    cat=R.load_json(result['published'])
    assert cat['restoration']=={'checkpoint':d.checkpoint,'receipt':result['restoration']['result']}
    assert len(Q.A._actions(cat))==1 and len(e.campaign.calls)==9 and len(e.launches)==1
    stripped=copy.deepcopy(cat);stripped.pop('restoration')
    with pytest.raises(ValueError,match='removed or changed'):Q.A._actions(stripped)
    d.document.write_bytes(b'next task is allowed')
    assert len(Q.A._actions(cat))==1
    receipt=Path(result['restoration']['result']['file']);receipt.write_text('{}')
    with pytest.raises(ValueError):Q.A._actions(cat)


def test_coordinator_partial_recovery_blocks_publication_without_repeating_task(coordinated,tmp_path):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path);d.ignore_field=True
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='failed_no_retry' and result['restoration']['status']=='partial'
    assert result['published'] is None and R.load_json(e.root/'session.json')['status']=='verified'
    assert result['primary_outcome']=={'status':'proof_completed','error':None}
    assert len(e.campaign.calls)==9 and d.calls==['close','reopen','set:level']
    with pytest.raises(ValueError):Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery,resume=True)
    assert len(e.campaign.calls)==9 and d.calls==['close','reopen','set:level']


def test_coordinator_failed_task_remains_failed_after_successful_document_cleanup(coordinated,tmp_path):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path);e.fail_setup=1
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='failed_no_retry' and result['primary_outcome']['status']=='failed'
    assert result['restoration']['status']=='restored' and d.calls==['close','reopen']
    assert not e.campaign.calls and result['published'] is None


def test_coordinator_unknown_native_cleanup_prevents_document_recovery_actions(coordinated,tmp_path):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path)
    e.campaign.failure_result={'status':'failed','resource_review_required':True,'error':{'message':'cleanup unknown'}}
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='failed_no_retry' and result['restoration']['status']=='unknown' and not d.calls
    assert d.document.read_bytes()==b'task saved new contents' and result['published'] is None


def test_required_document_recovery_refuses_partial_resume_before_any_action(coordinated,tmp_path):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path)
    with pytest.raises(ValueError,match='partial-run/resume'):
        Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery,actions_per_run=1)
    assert not e.setups and not e.campaign.calls and not d.calls


@pytest.mark.parametrize('uncertainty',[{'native_state_uncertain':True},{'native_calls':None},
                                       {'owned_worker':{'reaped':False}}])
def test_retained_uncertain_worker_blocks_cleanup_even_without_review_flag(coordinated,tmp_path,uncertainty):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path);e.fail_setup=1
    state=R.load_json(e.root/'session.json')
    evidence=pinned_json(e.root/'uncertain-worker.json',uncertainty)
    state['artifacts']['uncertain-worker.json']=evidence
    R.write_json(e.root/'session.json',state)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='failed_no_retry' and result['restoration']['status']=='unknown'
    assert not d.calls and result['published'] is None


def test_changed_target_stops_recovery_before_public_close(document_recovery,tmp_path):
    e=document_recovery;e.document.write_bytes(b'task changes')
    Path(e.identity['executable']).write_text('target binary replaced')
    result=D.restore(e.checkpoint,D.current_document(e.checkpoint),tmp_path/'recovery',backend=e.backend)
    assert result['status']=='failed' and not e.calls
    assert not result['files_restored'] and e.document.read_bytes()==b'task changes'


def test_clean_attached_worker_manager_close_does_not_require_os_helper_exit(coordinated,tmp_path):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path);e.fail_setup=1
    state=R.load_json(e.root/'session.json')
    actual={'status':'completed','attached':True,'native_state_uncertain':False,'native_calls':4,'invocations':1,
            'helper_load_attempts':0,'cleanup':{'script_unloaded':True,'detached':True},'cleanup_errors':[],
            'frida_lifecycle':{'format':Q.B.FRIDA_LIFECYCLE,'manager_state':'closed','attachment_attempted':True,
                               'helper_exit_confirmed':False,'error':None},'owned_worker':{'reaped':True}}
    # The production lifecycle validator intentionally requires false here.
    assert Q.B._frida_lifecycle(actual)['manager_state']=='closed'
    state['artifacts']['clean-worker.json']=pinned_json(e.root/'clean-worker.json',actual)
    R.write_json(e.root/'session.json',state)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='failed_no_retry' and result['restoration']['status']=='restored'
    assert d.calls==['close','reopen'] and not e.campaign.calls


def test_document_recovery_final_readback_detects_coupled_public_controls(document_recovery,tmp_path):
    e=document_recovery;e.document.write_bytes(b'task changes');e.position=[50.,60.];e.size=[600.,500.]
    original=e.backend.set_field
    def coupled(context,field,value):
        original(context,field,value)
        if field['id']=='window.size':e.position=[900.,900.]
    backend=D.TrustedHooks(e.backend.observe,e.backend.close,e.backend.reopen,coupled,e.backend.producers)
    result=D.restore(e.checkpoint,D.current_document(e.checkpoint),tmp_path/'recovery',backend=backend)
    assert result['status']=='partial' and result['files_restored'] and not result['app_state_restored']
    assert result['last_observation']['window']['position']==[900.,900.]
    assert 'final complete' in result['error']['message']


def test_document_recovery_reopens_closed_saved_copy_before_replacement(document_recovery,tmp_path):
    e=document_recovery;e.document.write_bytes(b'task changes');e.open=False
    result=D.restore(e.checkpoint,D.current_document(e.checkpoint),tmp_path/'recovery',backend=e.backend)
    assert result['status']=='restored' and e.document.read_bytes()==b'baseline document\n'
    assert e.calls==['reopen','close','reopen']
    assert result['actions'][:2]==['preliminary_reopen_requested','preliminary_reopen_verified']


def test_coordinator_document_closed_by_witnessed_restart_is_recovered(coordinated,tmp_path):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path)
    launch=public.launch
    def closed_after_restart(context):
        result=launch(context);d.open=False;return result
    public=Q.TrustedHooks(public.setup,public.stop,closed_after_restart,public.producers)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='completed',result.get('error')
    assert d.calls==['reopen','close','reopen','set:level']
    assert result['restoration']['status']=='restored' and len(e.campaign.calls)==9


def test_required_recovery_work_cutoff_clamps_setup_and_refuses_short_trial(coordinated,tmp_path,monkeypatch):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path)
    e.cfg['limits']['seconds']=60
    original_clock=time.time;offset=[0.]
    monkeypatch.setattr(Q.time,'time',lambda:original_clock()+offset[0])
    setup=public.setup
    def near_cutoff(context):
        assert 0 < context['deadline_monotonic']-time.monotonic() <= 30
        result=setup(context);offset[0]=29.;return result
    public=Q.TrustedHooks(near_cutoff,public.stop,public.launch,public.producers)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert result['status']=='failed_no_retry' and result['restoration']['status']=='restored'
    assert len(e.setups)==1 and not e.campaign.calls and not e.stops
    assert 'insufficient work budget' in result['error']['message']
    assert d.calls==['close','reopen','set:level']


def test_interrupted_native_g_boundary_without_worker_blocks_document_actions(coordinated,tmp_path,monkeypatch):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path)
    dispatch=Q.G._dispatch
    def interrupt(state,root,folder,request,prepared=None):
        if request['op']=='record':raise KeyboardInterrupt('inert native boundary interrupted')
        return dispatch(state,root,folder,request,prepared)
    monkeypatch.setattr(Q.G,'_dispatch',interrupt)
    out=tmp_path/'coordinator'
    with pytest.raises(KeyboardInterrupt,match='inert native boundary interrupted'):
        Q.run(e.root,e.cfg,out,backend=public,restoration_backend=recovery)
    result=R.load_json(out/'coordinator.json')
    assert result['status']=='failed_no_retry' and result['primary_outcome']['status']=='interrupted'
    assert result['restoration']['status']=='unknown' and not d.calls
    assert d.document.read_bytes()==b'task saved new contents'
    assert R.load_json(e.root/'session.json')['steps'][-1]['status']=='running'


def test_required_recovery_near_cutoff_restart_uses_work_budget(coordinated,tmp_path,monkeypatch):
    e=coordinated;d,recovery,public=_arm_document_recovery(e,tmp_path)
    e.cfg['limits']['seconds']=90
    frozen_wall=time.time();offset=[0.]
    monkeypatch.setattr(Q.time,'time',lambda:frozen_wall+offset[0])
    restart=Q.RR.run_restart;seen=[]
    def bounded(*args,**kwargs):
        # Move to 6s of work +30s cleanup immediately before the actual budget
        # is computed by restart_once; the callback receives only work time.
        seen.append(kwargs['timeout'])
        assert 1 <= kwargs['timeout'] <= 6
        return restart(*args,**kwargs)
    monkeypatch.setattr(Q.RR,'run_restart',bounded)
    original_trial=e.campaign.lower.run_trial
    def trial(*args,**kwargs):
        result=original_trial(*args,**kwargs)
        if len(e.campaign.calls)==6:offset[0]=54.
        return result
    monkeypatch.setattr(e.campaign.lower,'run_trial',trial)
    result=Q.run(e.root,e.cfg,tmp_path/'coordinator',backend=public,restoration_backend=recovery)
    assert seen and len(seen)==1
    # The admitted remaining trial time is still sufficient for this inert
    # fixture; the important boundary is the observed actual RR timeout.
    assert result['restoration']['status']=='restored',result.get('error')


def test_complete_observation_boundary_is_retained_and_oversize_capture_has_zero_actions(document_recovery,tmp_path):
    e=document_recovery
    fields=[{'id':'text','path':[0],'role':'AXTextArea','identifier':'fixture-text','restore':False}]
    payload=['']
    def observe(context):
        value=e.backend.observe(context)
        value['fields']={'text':{'value':payload[0],'settable':False}} if value['open'] else {}
        return value
    backend=D.TrustedHooks(observe,e.backend.close,e.backend.reopen,e.backend.set_field,e.backend.producers)
    context={'document':str(e.document)}
    empty=observe(context)
    payload[0]='x'*(D.MAX_STATE_BYTES-len(D._json(empty)))
    assert len(D._json(observe(context)))==D.MAX_STATE_BYTES
    checkpoint=D.capture(e.workspace,e.identity,D.inspect_checkpoint(e.checkpoint)['context']['target'],fields,
                         tmp_path/'bounded-checkpoint',backend=backend)
    e.document.write_bytes(b'changed task bytes')
    result=D.restore(checkpoint,D.current_document(checkpoint),tmp_path/'bounded-recovery',backend=backend)
    assert result['status']=='restored' and Path(result['receipt']['file']).stat().st_size < 2*1024**2
    assert e.calls==['close','reopen']
    # Restore the initial bytes so capture's fresh-workspace precondition holds.
    assert e.document.read_bytes()==e.source.read_bytes()
    e.calls.clear();payload[0]+='x'
    with pytest.raises(ValueError,match='aggregate document observation'):
        D.capture(e.workspace,e.identity,D.inspect_checkpoint(e.checkpoint)['context']['target'],fields,
                  tmp_path/'oversized-checkpoint',backend=backend)
    assert not e.calls and not (tmp_path/'oversized-checkpoint').exists()


@pytest.mark.parametrize('failure',[None,'timeout','owner'])
def test_public_document_close_button_is_owned_and_bounded_before_press(tmp_path,monkeypatch,failure):
    import sys
    app=SimpleNamespace();window=object();button=object();events=[]
    context={'identity':{'pid':111},'document':str(tmp_path/'owned.txt'),'deadline':time.monotonic()+2}
    def timeout(element,seconds):
        assert element is button and 0 < seconds <= 1
        events.append('timeout');return 1 if failure=='timeout' else 0
    def owner(element,unused):
        assert element is button;events.append('owner');return 0,999 if failure=='owner' else 111
    def press(element,action):
        assert element is button and action=='AXPress' and events==['timeout','owner']
        events.append('press');return 0
    AX=SimpleNamespace(AXUIElementSetMessagingTimeout=timeout,AXUIElementGetPid=owner,AXUIElementPerformAction=press)
    reader=D.PublicAXDocument();reader._last_windows=[];observations=[{'edited':False},{'open':False}]
    monkeypatch.setattr(reader,'observe',lambda c:observations.pop(0))
    def attr(element,name):
        assert element is window
        return Path(context['document']).as_uri() if name=='AXDocument' else button
    monkeypatch.setattr(reader,'_current',lambda c:(AX,attr,window))
    monkeypatch.setitem(sys.modules,'CoreFoundation',SimpleNamespace(CFEqual=lambda a,b:a is b))
    if failure:
        with pytest.raises(ValueError):reader.close(context)
        assert 'press' not in events
    else:
        reader.close(context);assert events==['timeout','owner','press']


@pytest.mark.parametrize('operation',['workspace','capture','restore'])
def test_standalone_document_storage_refuses_before_callbacks_or_output(document_recovery,tmp_path,monkeypatch,operation):
    e=document_recovery;before=e.document.read_bytes();seen=[]
    original=e.backend.observe
    backend=D.TrustedHooks(lambda c:(seen.append('observe'),original(c))[1],e.backend.close,e.backend.reopen,
                           e.backend.set_field,e.backend.producers)
    monkeypatch.setattr(D.os,'statvfs',lambda path:SimpleNamespace(f_bavail=0,f_frsize=1))
    out=tmp_path/('low-'+operation)
    with pytest.raises(ValueError,match='storage reserve'):
        if operation=='workspace':D.create_workspace(D.pin(e.source),out)
        elif operation=='capture':D.capture(e.workspace,e.identity,D.inspect_checkpoint(e.checkpoint)['context']['target'],
                                            D.inspect_checkpoint(e.checkpoint)['context']['fields'],out,backend=backend)
        else:D.restore(e.checkpoint,D.current_document(e.checkpoint),out,backend=backend)
    assert not e.calls and not seen and not out.exists() and e.document.read_bytes()==before


def test_document_storage_counts_same_device_once_and_remaining_allocations(monkeypatch,tmp_path):
    output=tmp_path/'output';working=tmp_path/'working';output.mkdir();working.mkdir()
    current,baseline=23,19
    plan=D._storage_plan((output,current+2*D.MAX_RECEIPT_BYTES),(working,baseline))
    free=[D.STORAGE_RESERVE_BYTES+current+baseline+2*D.MAX_RECEIPT_BYTES]
    monkeypatch.setattr(D.os,'statvfs',lambda path:SimpleNamespace(f_bavail=free[0],f_frsize=1))
    sample=D._storage_admit(plan)
    assert len(sample['filesystems'])==1 and sample['filesystems'][0]['remaining_allocation_bytes']==current+baseline+2*D.MAX_RECEIPT_BYTES
    # Account for already-written attempt/current backup without reserving them again.
    free[0]-=D.MAX_RECEIPT_BYTES+current;plan[0]['remaining_bytes']-=D.MAX_RECEIPT_BYTES+current
    D._storage_admit(plan)
    free[0]-=baseline;plan[1]['remaining_bytes']=0
    D._storage_admit(plan)
    free[0]-=1
    with pytest.raises(ValueError,match='storage reserve'):D._storage_admit(plan)


def test_low_working_filesystem_blocks_restore_before_close(document_recovery,tmp_path,monkeypatch):
    e=document_recovery;e.document.write_bytes(b'saved task edit');before=D.current_document(e.checkpoint)
    out=tmp_path/'recovery';actual=D._storage_directory
    working=e.document.parent
    def directory(path):
        value=actual(path)
        if Path(path)==working:value['device']+=1
        return value
    monkeypatch.setattr(D,'_storage_directory',directory)
    monkeypatch.setattr(D.os,'statvfs',lambda path:SimpleNamespace(f_bavail=0 if Path(path)==working else 4*1024**3,f_frsize=1))
    with pytest.raises(ValueError,match='storage reserve'):D.restore(e.checkpoint,before,out,backend=e.backend)
    assert not e.calls and not out.exists() and D.current_document(e.checkpoint)==before


def test_failed_document_storage_measurement_stops_before_restore(document_recovery,tmp_path,monkeypatch):
    e=document_recovery;before=D.current_document(e.checkpoint);out=tmp_path/'recovery'
    def failed(path):raise OSError('inert statvfs failure')
    monkeypatch.setattr(D.os,'statvfs',failed)
    with pytest.raises(OSError,match='statvfs'):D.restore(e.checkpoint,before,out,backend=e.backend)
    assert not e.calls and not out.exists() and D.current_document(e.checkpoint)==before


def test_external_document_edit_refusal_retains_failed_receipt_without_callback(document_recovery,tmp_path):
    e=document_recovery;e.document.write_bytes(b'task saved content')
    post=D.current_document(e.checkpoint);e.document.write_bytes(b'external later edit')
    out=tmp_path/'external-refusal'
    result=D.restore(e.checkpoint,post,out,backend=e.backend)
    assert result['status']=='failed' and result['actions']==[] and not result['files_restored']
    assert Path(result['receipt']['file']).is_file() and R.load_json(result['receipt']['file'])['status']=='failed'
    assert not e.calls and e.document.read_bytes()==b'external later edit'


@pytest.fixture
def public_document_ax(document_recovery, monkeypatch):
    """Actual public adapter against a small inert AX hierarchy, not a saved-state stub."""
    import sys
    e = document_recovery
    def node(name, **attributes):
        return SimpleNamespace(name=name, attributes=attributes, pid=e.identity['pid'])
    window = node('document', AXRole='AXWindow', AXSubrole='AXStandardWindow',
                  AXDocument=e.document.as_uri(), AXPosition=[10.,20.], AXSize=[400.,300.], AXModal=False)
    button = node('close', AXRole='AXButton', AXSubrole='AXCloseButton', AXEdited=False, AXWindow=window)
    # Non-editable document tree, also used to protect the no-text-area path.
    # The explicit plain-text fixture below supplies a real declared AXTextArea.
    text = node('text', AXRole='AXStaticText', AXSubrole='AXStaticText')
    group = node('group', AXRole='AXScrollArea', AXChildren=[text])
    window.attributes.update(AXCloseButton=button, AXChildren=[button, group])
    app = node('app', AXWindows=[window])
    env = SimpleNamespace(app=app, window=window, button=button, text=text, group=group,
                          reads=[], errors={}, mutations=[], nodes=[app,window,button,text,group],
                          identity=e.identity, document=e.document, workspace=e.workspace,
                          target=D.inspect_checkpoint(e.checkpoint)['context']['target'])
    bounded = set()
    def timeout(element, seconds):
        assert 0 < seconds <= 1
        bounded.add(id(element));return 0
    def owner(element, unused):
        assert id(element) in bounded
        return 0, element.pid
    def read(element, name, unused):
        assert id(element) in bounded
        env.reads.append((element.name,name))
        if (element.name,name) in env.errors:return env.errors[(element.name,name)],None
        if name not in element.attributes:return -25205,None
        return 0,element.attributes[name]
    def mutation(*args):
        env.mutations.append(args);raise AssertionError('observation must never mutate the app')
    AX=SimpleNamespace(AXUIElementCreateApplication=lambda pid:app, AXUIElementSetMessagingTimeout=timeout,
        AXUIElementGetPid=owner, AXUIElementCopyAttributeValue=read,
        AXValueGetValue=lambda value,kind,unused:(True,value), kAXValueTypeCGPoint=1,kAXValueTypeCGSize=2,
        AXUIElementPerformAction=mutation,AXUIElementSetAttributeValue=mutation)
    monkeypatch.setitem(sys.modules,'ApplicationServices',AX)
    monkeypatch.setitem(sys.modules,'CoreFoundation',SimpleNamespace(CFEqual=lambda a,b:a is b))
    monkeypatch.setitem(sys.modules,'Foundation',SimpleNamespace(NSNumber=int))
    monkeypatch.setattr(R,'assert_guest',lambda:None)
    monkeypatch.setattr(U,'gate',lambda identity,where,deadline:(identity,object()))
    env.reader=D.PublicAXDocument()
    env.context={'identity':copy.deepcopy(e.identity),'target':env.target,'document':str(e.document),
                 'fields':[],'deadline':time.monotonic()+5}
    return env


@pytest.mark.parametrize('placement',['window','close_button','both'])
def test_public_saved_state_observation_accepts_actual_bool_and_complete_sheet_free_tree(public_document_ax,tmp_path,placement):
    e=public_document_ax
    if placement in {'window','both'}:e.window.attributes['AXEdited']=False
    if placement=='window':e.button.attributes.pop('AXEdited')
    checkpoint=D.capture(e.workspace,e.identity,e.target,[],tmp_path/'public-checkpoint')
    value=D.inspect_checkpoint(checkpoint)
    assert value['baseline']['edited'] is False and value['baseline']['document']==str(e.document)
    assert ('text','AXRole') in e.reads and ('group','AXChildren') in e.reads
    assert all(name!='AXSheets' for _,name in e.reads) and not e.mutations


@pytest.fixture
def document_metadata_policy(document_recovery, tmp_path, monkeypatch):
    """Fixed public class with inert app calls and real private-leaf I/O."""
    e=document_recovery
    trusted=e.backend
    for name in ('observe','close','reopen','set_field'):
        callback=getattr(trusted,name)
        monkeypatch.setattr(D.PublicAXDocument,name,lambda self,*args,_callback=callback:_callback(*args))
    e.backend=D.PublicAXDocument()
    e.metadata={}
    e.security={'mode':0o100600,'uid':os.getuid(),'gid':os.getgid(),'links':1,'flags':0,'generation':0}
    e.acl={'bytes':16,'sha256':'a'*64}
    def snapshot(fd,context):
        raw,pin,identity=D._read(context['document'])
        info=os.fstat(fd)
        assert (info.st_dev,info.st_ino)==tuple(identity[:2])
        return {'document':{**pin,'identity':list(identity)},'security':copy.deepcopy(e.security),
                'xattrs':copy.deepcopy(e.metadata),'acl':copy.deepcopy(e.acl)}
    e.snapshot=snapshot
    monkeypatch.setattr(D,'_metadata_snapshot',snapshot)
    value=D.inspect_checkpoint(e.checkpoint)
    e.checkpoint=D.capture(e.workspace,e.identity,value['context']['target'],value['context']['fields'],
        tmp_path/'metadata-checkpoint',backend=e.backend,metadata_policy=D.DARWIN_LASTUSED)
    e.document.write_bytes(b'post-task saved bytes\n')
    e.open=False
    e.initial=D.current_document(e.checkpoint)
    e.original_reopen=trusted.reopen
    def reopen(context):
        # Simulate the measured ordinary-open metadata event using an inert
        # marker and a real ctime-changing chmod of the private owned leaf.
        if not e.metadata:
            e.metadata[D.LASTUSED_ATTRIBUTE]={'bytes':16,'sha256':'b'*64}
            os.chmod(e.document,0o600)
        trusted.reopen(context)
    monkeypatch.setattr(D.PublicAXDocument,'reopen',lambda self,context:reopen(context))
    return e


def test_declared_metadata_boundary_restores_bytes_and_retains_original_and_accepted_identities(document_metadata_policy,tmp_path):
    e=document_metadata_policy
    result=D.restore(e.checkpoint,e.initial,tmp_path/'metadata-recovery')
    assert result['status']=='restored' and e.document.read_bytes()==e.source.read_bytes()
    transition=result['metadata_transition']
    assert result['expected_current']==e.initial==transition['before']['document']
    assert transition['admitted_delta']==D.LASTUSED_ATTRIBUTE
    assert transition['before']['xattrs']=={} and transition['after']['xattrs']==e.metadata
    assert transition['before']['document']['identity'][:4]==transition['after']['document']['identity'][:4]
    assert e.calls==['reopen','close','reopen']
    assert D.verify_restored(e.checkpoint,result['receipt'])['metadata_transition']==transition


@pytest.mark.parametrize('update',[False,True])
def test_metadata_policy_accepts_unchanged_boundary_or_exact_existing_value_update(document_metadata_policy,tmp_path,monkeypatch,update):
    e=document_metadata_policy;e.metadata[D.LASTUSED_ATTRIBUTE]={'bytes':16,'sha256':'c'*64}
    def reopen(self,context):
        if update:
            e.metadata[D.LASTUSED_ATTRIBUTE]={'bytes':16,'sha256':'b'*64};os.chmod(e.document,0o600)
        e.original_reopen(context)
    monkeypatch.setattr(D.PublicAXDocument,'reopen',reopen)
    result=D.restore(e.checkpoint,e.initial,tmp_path/'existing-value')
    assert result['status']=='restored'
    assert result['metadata_transition']['admitted_delta']==(D.LASTUSED_ATTRIBUTE if update else 'none')


def test_metadata_policy_receipt_requires_original_observation_and_declared_policy(document_metadata_policy,tmp_path):
    e=document_metadata_policy;result=D.restore(e.checkpoint,e.initial,tmp_path/'verified-policy')
    assert result['status']=='restored'
    for key in ('metadata_policy','expected_current'):
        altered=json.loads(D._verified(result['receipt']))
        if key=='metadata_policy':altered.pop(key)
        else:altered[key]['identity'][4]+=1
        receipt=D._save(tmp_path/(key+'.json'),altered)
        with pytest.raises(ValueError):D.verify_restored(e.checkpoint,receipt)


@pytest.mark.parametrize('change',['content','inode','mtime','security','acl','other_xattr','lastused_short','lastused_delete','ctime_only'])
def test_metadata_boundary_refuses_every_undeclared_delta_without_replacing_bytes(document_metadata_policy,tmp_path,monkeypatch,change):
    e=document_metadata_policy
    if change=='lastused_delete':
        e.metadata[D.LASTUSED_ATTRIBUTE]={'bytes':16,'sha256':'c'*64}
    def reopen(self,context):
        e.original_reopen(context)
        if change=='content':e.document.write_bytes(b'new external bytes\n')
        elif change=='inode':
            replacement=e.document.with_name('other');replacement.write_bytes(e.document.read_bytes());replacement.replace(e.document)
        elif change=='mtime':
            info=e.document.stat();os.utime(e.document,ns=(info.st_atime_ns,info.st_mtime_ns+1000000))
        elif change=='security':e.security['mode']=0o100640
        elif change=='acl':e.acl['sha256']='d'*64
        elif change=='other_xattr':e.metadata['unrelated.attribute']={'bytes':1,'sha256':'e'*64}
        elif change=='lastused_short':e.metadata[D.LASTUSED_ATTRIBUTE]={'bytes':15,'sha256':'b'*64}
        elif change=='lastused_delete':e.metadata.clear()
        else:os.chmod(e.document,0o600)
    monkeypatch.setattr(D.PublicAXDocument,'reopen',reopen)
    # The inert snapshot still binds its FD to the path; replacement is refused
    # at that boundary rather than allowed to adopt the new inode.
    result=D.restore(e.checkpoint,e.initial,tmp_path/'refused-boundary')
    assert result['status']=='unknown' and not result['files_restored']
    assert 'bytes_restored_verified' not in result['actions'] and e.document.read_bytes()!=e.source.read_bytes()
    assert (tmp_path/'refused-boundary'/'result.json').is_file()


def test_metadata_policy_rejects_initial_drift_before_any_app_observation(document_metadata_policy,tmp_path,monkeypatch):
    e=document_metadata_policy;observations=[]
    monkeypatch.setattr(D.PublicAXDocument,'observe',lambda self,context:observations.append(context))
    os.chmod(e.document,0o600)
    result=D.restore(e.checkpoint,e.initial,tmp_path/'initial-drift')
    assert result['status']=='failed' and result['actions']==[] and not observations and not e.calls


def test_metadata_policy_rechecks_accepted_metadata_under_lock_and_before_replace(document_metadata_policy,tmp_path,monkeypatch):
    e=document_metadata_policy;original=e.snapshot;calls=[]
    def snapshot(fd,context):
        value=original(fd,context);calls.append(value)
        # 1 initial,2 before reopen,3 after close,4 under flock,5 pre-replace.
        if len(calls)==5:value['security']['mode']=0o100640
        return value
    monkeypatch.setattr(D,'_metadata_snapshot',snapshot)
    result=D.restore(e.checkpoint,e.initial,tmp_path/'late-drift')
    assert len(calls)==5 and result['status']=='unknown' and not result['files_restored']
    assert 'immediately before replacement' in result['error']['message']
    assert e.document.read_bytes()==b'post-task saved bytes\n' and not list(e.document.parent.glob('.restore-*'))


def test_metadata_policy_does_not_relax_already_open_path(document_metadata_policy,tmp_path):
    e=document_metadata_policy;e.open=True
    # A close-side metadata change has no preliminary reopen witness.
    original=e.backend.close
    def close(context):
        original(context);os.chmod(e.document,0o400)
    e.backend.close=close
    result=D.restore(e.checkpoint,e.initial,tmp_path/'already-open',backend=e.backend)
    assert result['status']=='unknown' and result['metadata_transition'] is None and not result['files_restored']
    assert e.document.stat().st_mode & 0o777 == 0o400


def test_darwin_metadata_reader_measures_real_private_fd_attributes(tmp_path):
    """Exercise SDK bindings on an owned scratch leaf, without any app calls."""
    import ctypes
    import sys
    if sys.platform != 'darwin':
        pytest.skip('Darwin FD metadata ABI')
    document=tmp_path/'owned-metadata-fixture'
    document.write_bytes(b'private metadata reader fixture\n');document.chmod(0o600)
    context={'document':str(document),'deadline':time.monotonic()+10}
    fd=os.open(document,os.O_RDWR|os.O_NOFOLLOW)
    try:
        before=D._metadata_snapshot(fd,context)
        api=D._darwin_metadata_api()
        setter=api.fsetxattr
        setter.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_void_p,
                         ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int]
        setter.restype=ctypes.c_int
        value=b'0123456789abcdef';buffer=ctypes.create_string_buffer(value)
        assert setter(fd,D.LASTUSED_ATTRIBUTE.encode(),buffer,len(value),0,0)==0
        after=D._metadata_snapshot(fd,context)
        assert after['document']['sha256']==hashlib.sha256(document.read_bytes()).hexdigest()
        assert after['xattrs'][D.LASTUSED_ATTRIBUTE]=={
            'bytes':16,'sha256':hashlib.sha256(value).hexdigest()}
        assert after['acl']==before['acl']
        assert after['acl']=={'status':'absent'} or after['acl']['bytes']>0
        transition=D._metadata_transition(before,after)
        assert transition['admitted_delta']==D.LASTUSED_ATTRIBUTE
        assert D._metadata_snapshot(fd,context)==after
    finally:
        os.close(fd)


def test_metadata_policy_refuses_trusted_hooks_and_unknown_policy(document_recovery,tmp_path):
    e=document_recovery;value=D.inspect_checkpoint(e.checkpoint)
    for policy in (D.DARWIN_LASTUSED,'allow_any_metadata'):
        with pytest.raises(ValueError,match='fixed public AX adapter'):
            D.capture(e.workspace,e.identity,value['context']['target'],value['context']['fields'],
                tmp_path/'not-created',backend=e.backend,metadata_policy=policy)
    assert not (tmp_path/'not-created').exists() and not e.calls


def test_default_checkpoint_remains_strict_on_preliminary_open_ctime(document_recovery,tmp_path):
    e=document_recovery;e.document.write_bytes(b'post-task\n');e.open=False
    expected=D.current_document(e.checkpoint);original=e.backend.reopen
    def reopen(context):original(context);os.chmod(e.document,0o600)
    backend=D.TrustedHooks(e.backend.observe,e.backend.close,reopen,e.backend.set_field,e.backend.producers)
    result=D.restore(e.checkpoint,expected,tmp_path/'strict-default',backend=backend)
    assert result['status']=='unknown' and not result['files_restored'] and 'metadata_policy' not in result


def test_metadata_read_failure_stops_before_public_callbacks(document_metadata_policy,tmp_path,monkeypatch):
    e=document_metadata_policy;observations=[]
    monkeypatch.setattr(D.PublicAXDocument,'observe',lambda self,context:observations.append(context))
    monkeypatch.setattr(D,'_metadata_snapshot',lambda *args:(_ for _ in ()).throw(OSError('ACL read unavailable')))
    result=D.restore(e.checkpoint,e.initial,tmp_path/'metadata-unavailable')
    assert result['status']=='failed' and result['actions']==[] and not observations and not e.calls
    assert e.document.read_bytes()==b'post-task saved bytes\n'


@pytest.mark.parametrize('failure',[None,'list_error','short_value','oversize','acl_error','acl_absent',
    'acl_permission','acl_copy_error','acl_cleanup_error','deadline_after_acl'])
def test_fd_metadata_reader_keeps_complete_bounds_and_acl_allocation_ownership(monkeypatch,failure):
    import ctypes
    import errno
    context={'deadline':time.monotonic()+20};calls=[]
    names=(D.LASTUSED_ATTRIBUTE+'\0').encode();value=b'x'*16;acl=b'canonical-acl'
    def put(buffer,data):
        if buffer is not None:ctypes.memmove(buffer,data,len(data))
        return len(data)
    def listing(fd,buffer,size,options):
        assert options==0x20
        if failure=='list_error':return -1
        return put(buffer,names)
    def attribute(fd,name,buffer,size,position,options):
        assert name==D.LASTUSED_ATTRIBUTE.encode() and position==0 and options==0x20
        if failure=='oversize':return 65537
        if buffer is not None and failure=='short_value':return 15
        return put(buffer,value)
    def get_acl(fd,kind):
        assert kind==0x100;calls.append('allocate')
        if failure=='deadline_after_acl':context['deadline']=0
        if failure in {'acl_absent','acl_permission'}:
            ctypes.set_errno(errno.ENOENT if failure=='acl_absent' else errno.EACCES)
            return None
        return None if failure=='acl_error' else 123
    def free_acl(pointer):
        assert pointer==123;calls.append('free')
        return -1 if failure=='acl_cleanup_error' else 0
    api=SimpleNamespace(flistxattr=listing,fgetxattr=attribute,acl_get_fd_np=get_acl,
        acl_size=lambda pointer:len(acl),acl_copy_ext=lambda buffer,pointer,size:-1 if failure=='acl_copy_error' else put(buffer,acl),
        acl_free=free_acl)
    monkeypatch.setattr(D,'_darwin_metadata_api',lambda:api)
    if failure in (None,'acl_absent'):
        attrs,actual_acl=D._metadata_values(42,context)
        assert attrs=={D.LASTUSED_ATTRIBUTE:{'bytes':16,'sha256':hashlib.sha256(value).hexdigest()}}
        assert actual_acl==({'status':'absent'} if failure=='acl_absent' else
            {'bytes':len(acl),'sha256':hashlib.sha256(acl).hexdigest()})
    else:
        with pytest.raises(ValueError):D._metadata_values(42,context)
    assert calls==(['allocate'] if failure in ('acl_error','acl_absent','acl_permission') else ['allocate','free'] if failure in
        (None,'acl_copy_error','acl_cleanup_error','deadline_after_acl') else [])


@pytest.mark.parametrize('failure',['unavailable','non_boolean','conflict','wrong_owner','wrong_role',
    'wrong_subrole','wrong_window','transport','permission','nil_success','window_non_boolean'])
def test_public_saved_state_refuses_unknown_invalid_or_unassociated_indicator(public_document_ax,tmp_path,failure):
    e=public_document_ax;before=e.document.read_bytes()
    if failure=='unavailable':e.button.attributes.pop('AXEdited')
    elif failure=='non_boolean':e.button.attributes['AXEdited']=0
    elif failure=='conflict':e.window.attributes['AXEdited']=True
    elif failure=='wrong_owner':e.button.pid+=1
    elif failure=='wrong_role':e.button.attributes['AXRole']='AXStaticText'
    elif failure=='wrong_subrole':e.button.attributes['AXSubrole']='AXUnknown'
    elif failure=='wrong_window':e.button.attributes['AXWindow']=e.app
    elif failure=='transport':e.errors[('document','AXEdited')]=-25204
    elif failure=='permission':e.errors[('document','AXEdited')]=-25211
    elif failure=='nil_success':e.window.attributes['AXEdited']=None
    elif failure=='window_non_boolean':e.window.attributes['AXEdited']='false'
    out=tmp_path/'refused-public-checkpoint'
    with pytest.raises(ValueError):D.capture(e.workspace,e.identity,e.target,[],out)
    assert not out.exists() and not e.mutations and e.document.read_bytes()==before


@pytest.mark.parametrize('failure',['sheet','nested_dialog','modal','unreadable_role','unreadable_children',
    'unknown_role','oversize','cycle','app_dialog'])
def test_public_document_complete_hierarchy_refuses_sheets_dialogs_and_unknown_branches(public_document_ax,tmp_path,failure):
    e=public_document_ax
    if failure=='sheet':e.text.attributes['AXRole']='AXSheet'
    elif failure=='nested_dialog':
        e.text.attributes.update(AXRole='AXWindow',AXSubrole='AXDialog',AXModal=False)
    elif failure=='modal':e.group.attributes['AXModal']=True
    elif failure=='unreadable_role':e.errors[('text','AXRole')]=-25204
    elif failure=='unreadable_children':e.errors[('group','AXChildren')]=-25204
    elif failure=='unknown_role':e.text.attributes['AXRole']='AXUnknown'
    elif failure=='oversize':e.window.attributes['AXChildren']=[e.text]*129
    elif failure=='cycle':e.text.attributes['AXChildren']=[e.window]
    elif failure=='app_dialog':
        dialog=SimpleNamespace(name='dialog',pid=e.identity['pid'],attributes={'AXRole':'AXWindow','AXSubrole':'AXDialog'})
        e.app.attributes['AXWindows'].append(dialog)
    out=tmp_path/'unsafe-tree-checkpoint'
    with pytest.raises(ValueError):D.capture(e.workspace,e.identity,e.target,[],out)
    assert not out.exists() and not e.mutations


def test_public_dirty_close_button_cannot_authorize_checkpoint_or_close(public_document_ax,tmp_path):
    e=public_document_ax;e.button.attributes['AXEdited']=True
    assert e.reader.observe(e.context)['edited'] is True
    with pytest.raises(ValueError,match='unsaved document'):D.capture(e.workspace,e.identity,e.target,[],tmp_path/'dirty')
    with pytest.raises(ValueError,match='saved task document'):e.reader.close(e.context)
    assert not e.mutations


@pytest.mark.parametrize('subrole',['AXStandardWindow','AXFloatingWindow'])
@pytest.mark.parametrize('modal',['true','integer','string','unsupported','no_value','nil','transport','permission'])
def test_public_other_app_window_modal_or_unknown_refuses_capture_and_close(public_document_ax,tmp_path,subrole,modal):
    e=public_document_ax
    other=SimpleNamespace(name='other',pid=e.identity['pid'],attributes={'AXRole':'AXWindow','AXSubrole':subrole})
    e.app.attributes['AXWindows'].append(other)
    if modal=='true':other.attributes['AXModal']=True
    elif modal=='integer':other.attributes['AXModal']=0
    elif modal=='string':other.attributes['AXModal']='false'
    elif modal=='no_value':e.errors[('other','AXModal')]=-25212
    elif modal=='nil':other.attributes['AXModal']=None
    elif modal=='transport':e.errors[('other','AXModal')]=-25204
    elif modal=='permission':e.errors[('other','AXModal')]=-25211
    # An absent attribute produces explicit unsupported in this inert backend.
    out=tmp_path/'unknown-app-modal'
    before=e.document.read_bytes()
    with pytest.raises(ValueError):D.capture(e.workspace,e.identity,e.target,[],out)
    with pytest.raises(ValueError):e.reader.close(e.context)
    assert ('other','AXModal') in e.reads and not e.mutations and not out.exists()
    assert e.document.read_bytes()==before


def test_public_other_saved_nonmodal_window_is_observed_without_adoption(public_document_ax,tmp_path):
    e=public_document_ax
    other=SimpleNamespace(name='other',pid=e.identity['pid'],attributes={
        'AXRole':'AXWindow','AXSubrole':'AXFloatingWindow','AXModal':False})
    e.app.attributes['AXWindows'].append(other)
    checkpoint=D.capture(e.workspace,e.identity,e.target,[],tmp_path/'nonmodal-other-window')
    assert D.inspect_checkpoint(checkpoint)['context']['document']==str(e.document)
    assert ('other','AXModal') in e.reads and not e.mutations


@pytest.mark.parametrize('code',[-25200,-25204,-25205,-25212,0])
def test_public_nonwindow_subroles_are_not_requested_when_roles_suffice(public_document_ax,tmp_path,code):
    e=public_document_ax
    ordinary=SimpleNamespace(name='ordinary-button',pid=e.identity['pid'],attributes={'AXRole':'AXButton'})
    e.group.attributes['AXChildren'].append(ordinary)
    e.errors[('group','AXSubrole')]=code;e.errors[('ordinary-button','AXSubrole')]=code
    checkpoint=D.capture(e.workspace,e.identity,e.target,[],tmp_path/'role-planned-checkpoint')
    assert D.inspect_checkpoint(checkpoint)['baseline']['edited'] is False
    assert ('group','AXSubrole') not in e.reads and ('ordinary-button','AXSubrole') not in e.reads
    assert ('group','AXRole') in e.reads and ('ordinary-button','AXRole') in e.reads
    assert ('close','AXSubrole') in e.reads and not e.mutations


@pytest.mark.parametrize('attribute',['AXSubrole','AXModal'])
@pytest.mark.parametrize('code',[-25200,-25204,-25205,-25212,0])
def test_public_nested_windows_still_require_known_subrole_and_modal_false(public_document_ax,tmp_path,attribute,code):
    e=public_document_ax
    e.text.attributes.update(AXRole='AXWindow',AXSubrole='AXStandardWindow',AXModal=False)
    e.errors[('text',attribute)]=code
    out=tmp_path/'unknown-nested-window'
    with pytest.raises(ValueError):D.capture(e.workspace,e.identity,e.target,[],out)
    with pytest.raises(ValueError):e.reader.close(e.context)
    assert not out.exists() and not e.mutations and ('text',attribute) in e.reads


@pytest.mark.parametrize('location,attribute',[('close','AXSubrole'),('text','AXRole'),('group','AXChildren'),('group','AXModal')])
def test_public_role_plan_does_not_ignore_requested_safety_attribute_errors(public_document_ax,tmp_path,location,attribute):
    e=public_document_ax;e.errors[(location,attribute)]=-25200
    out=tmp_path/'safety-read-error'
    with pytest.raises(ValueError):D.capture(e.workspace,e.identity,e.target,[],out)
    with pytest.raises(ValueError):e.reader.close(e.context)
    assert (location,attribute) in e.reads and not e.mutations and not out.exists()


@pytest.fixture
def public_plain_text(public_document_ax, tmp_path, monkeypatch):
    import sys
    e=public_document_ax
    e.initial='Owned recovery fixture — saved task edit.\nSecond changed line.\n'
    source=tmp_path/'plain-source.txt';source.write_bytes(e.initial.encode('utf-8'))
    e.workspace=D.create_workspace(D.pin(source),tmp_path/'plain-workspace')
    e.document=Path(json.loads(D._verified(e.workspace))['document']['file'])
    e.window.attributes['AXDocument']=e.document.as_uri()
    e.text.attributes.update(AXRole='AXTextArea',AXIdentifier='First Text View',AXWindow=e.window,AXValue=e.initial)
    e.text.attributes.pop('AXSubrole')  # The actual known unsupported event.
    e.fields=[{'id':'document.text','path':[1,0],'role':'AXTextArea','identifier':'First Text View','restore':False}]
    e.contract={'field':'document.text','encoding':'utf-8','newlines':'exact'}
    e.context.update(document=str(e.document),fields=copy.deepcopy(e.fields),text_buffer=copy.deepcopy(e.contract))
    e.ax=sys.modules['ApplicationServices'];e.actions=[]
    def settable(element,name,unused):
        assert name=='AXValue'
        return 0,True
    monkeypatch.setattr(e.ax,'AXUIElementIsAttributeSettable',settable,raising=False)
    def close(element,action):
        assert element is e.button and action=='AXPress'
        e.actions.append('close');e.app.attributes['AXWindows']=[];return 0
    monkeypatch.setattr(e.ax,'AXUIElementPerformAction',close)
    def reopen(reader,context):
        e.actions.append('reopen');e.text.attributes['AXValue']=e.document.read_bytes().decode('utf-8')
        e.app.attributes['AXWindows']=[e.window]
    monkeypatch.setattr(D.PublicAXDocument,'reopen',reopen)
    return e


def _plain_checkpoint(e, out):
    return D.capture(e.workspace,e.identity,e.target,e.fields,out,text_buffer=e.contract)


def test_false_AXEdited_cannot_hide_actual_65_byte_plain_text_buffer_change(public_plain_text,tmp_path):
    e=public_plain_text;checkpoint=_plain_checkpoint(e,tmp_path/'plain-checkpoint')
    before=D.current_document(checkpoint);assert before['bytes']==65
    e.text.attributes['AXValue']=e.initial+'7'
    assert e.button.attributes['AXEdited'] is False
    result=D.restore(checkpoint,before,tmp_path/'plain-refusal')
    assert result['status']=='failed' and result['actions']==[] and not result['files_restored']
    assert 'unsaved document buffer differs' in result['error']['message']
    guard=result['text_buffer_observations'][-1]
    assert guard['AXEdited'] is False and guard['matches'] is False and guard['disk']==before
    assert e.text.attributes['AXValue']==e.initial+'7' and D.current_document(checkpoint)==before and not e.actions
    retained=json.loads(D._verified(result['receipt']))
    assert retained['text_buffer_observations']==result['text_buffer_observations']
    with pytest.raises(ValueError,match='unsaved document buffer differs'):e.reader.close(e.context)
    assert not e.actions


def test_visible_text_area_requires_declaration_even_with_empty_fields(public_plain_text,tmp_path):
    e=public_plain_text;context={**e.context,'fields':[]};context.pop('text_buffer')
    with pytest.raises(ValueError,match='requires a declared'):e.reader.observe(context)
    with pytest.raises(ValueError,match='requires a declared'):e.reader.close(context)
    out=tmp_path/'undeclared-checkpoint'
    with pytest.raises(ValueError,match='requires a declared'):D.capture(e.workspace,e.identity,e.target,[],out)
    assert not out.exists() and not e.actions


@pytest.mark.parametrize('initially_closed',[False,True])
def test_exact_plain_text_saved_copy_restores_and_reopens_without_metadata_exception(public_plain_text,tmp_path,initially_closed):
    e=public_plain_text;checkpoint=_plain_checkpoint(e,tmp_path/'plain-checkpoint')
    e.document.write_bytes(b'normal saved task edit\n');e.text.attributes['AXValue']='normal saved task edit\n'
    if initially_closed:e.app.attributes['AXWindows']=[]
    result=D.restore(checkpoint,D.current_document(checkpoint),tmp_path/'plain-recovery')
    assert result['status']=='restored',result.get('error')
    assert e.document.read_bytes()==e.initial.encode('utf-8') and e.text.attributes['AXValue']==e.initial
    assert e.actions==(['reopen','close','reopen']if initially_closed else['close','reopen'])
    assert 'declared_exact_utf8_buffer_matches_disk' in result['scope']['required']
    D.verify_restored(checkpoint,result['receipt'])


def test_true_AXEdited_still_refuses_when_plain_buffer_equals_disk(public_plain_text,tmp_path):
    e=public_plain_text;checkpoint=_plain_checkpoint(e,tmp_path/'plain-checkpoint');e.button.attributes['AXEdited']=True
    result=D.restore(checkpoint,D.current_document(checkpoint),tmp_path/'dirty-flag')
    assert result['status']=='failed' and result['actions']==[] and 'unsaved document buffers' in result['error']['message']
    with pytest.raises(ValueError,match='saved task document'):e.reader.close(e.context)
    assert not e.actions


@pytest.mark.parametrize('raw,buffer',[
    (b'line\r\n','line\n'),(b'line\r','line\n'),(b' line\n','line\n'),
    (b'\xef\xbb\xbfline\n','line\n'),(b'\xffline\n','line\n'),(b'line\x00\n','line\x00\n'),
    ('line\n'.encode('utf-16'),'line\n')])
def test_plain_text_never_normalizes_unknown_encoding_newlines_BOM_or_whitespace(public_plain_text,tmp_path,raw,buffer):
    e=public_plain_text;e.document.write_bytes(raw);e.text.attributes['AXValue']=buffer
    before=D.pin(e.document)
    with pytest.raises(ValueError):e.reader.close(e.context)
    with pytest.raises(ValueError):_plain_checkpoint(e,tmp_path/'unsupported-checkpoint')
    assert D.pin(e.document)==before and not e.actions


def test_explicit_plain_text_supports_longer_than_generic_state_scalar_limit(public_plain_text,tmp_path):
    e=public_plain_text;text='a'*2048+'\n';e.document.write_bytes(text.encode());e.text.attributes['AXValue']=text
    assert len(text)>D.S.MAX_TEXT
    state=e.reader.observe(e.context)
    assert state['fields']['document.text']['value']==text
    assert state['edited'] is False and not e.actions


@pytest.mark.parametrize('text',['line\n','line\r\n'])
def test_plain_text_exact_newlines_match_only_when_both_values_match(public_plain_text,text):
    e=public_plain_text;e.document.write_bytes(text.encode());e.text.attributes['AXValue']=text
    assert e.reader.observe(e.context)['fields']['document.text']['value']==text
    assert not e.actions


def test_plain_text_bound_does_not_relax_generic_field_scalar_limit(public_plain_text):
    e=public_plain_text;text='a'*2048;e.text.attributes.update(AXRole='AXTextField',AXSubrole='AXTextField',AXValue=text)
    context={**e.context,'fields':[{**e.fields[0],'role':'AXTextField'}]};context.pop('text_buffer')
    with pytest.raises(ValueError):e.reader.observe(context)
    e.text.attributes.update(AXRole='AXTextArea',AXValue='a'*(D.MAX_TEXT_BYTES+1))
    with pytest.raises(ValueError,match='byte bound'):e.reader.close(e.context)
    assert not e.actions


@pytest.mark.parametrize('failure',['missing','ambiguous','wrong_window','wrong_identifier','wrong_owner','secure','unknown_subrole'])
def test_plain_text_contract_does_not_adopt_ambiguous_or_unidentified_area(public_plain_text,tmp_path,failure):
    e=public_plain_text
    if failure=='missing':e.text.attributes['AXRole']='AXStaticText'
    elif failure=='ambiguous':
        other=SimpleNamespace(name='other-text',pid=e.identity['pid'],attributes={'AXRole':'AXTextArea','AXChildren':[]})
        e.group.attributes['AXChildren'].append(other)
    elif failure=='wrong_window':e.text.attributes['AXWindow']=e.app
    elif failure=='wrong_identifier':e.text.attributes['AXIdentifier']='other-text'
    elif failure=='wrong_owner':e.text.pid+=1
    elif failure=='secure':e.text.attributes['AXSubrole']='AXSecureTextField'
    else:e.errors[('text','AXSubrole')]=-25212
    with pytest.raises(ValueError):_plain_checkpoint(e,tmp_path/'unsupported-area')
    with pytest.raises(ValueError):e.reader.close(e.context)
    assert not e.actions


@pytest.mark.parametrize('drift',['buffer','same_bytes_new_leaf'])
def test_plain_text_is_reobserved_with_full_leaf_identity_immediately_before_close(public_plain_text,monkeypatch,drift):
    e=public_plain_text;original=e.reader.observe;seen=[];before=D.pin(e.document)
    def changed(context):
        value=original(context);seen.append(value)
        if len(seen)==1:
            if drift=='buffer':e.text.attributes['AXValue']=e.initial+'7'
            else:
                replacement=e.document.with_name('replacement.txt');replacement.write_bytes(e.document.read_bytes())
                os.replace(replacement,e.document)
        return value
    monkeypatch.setattr(e.reader,'observe',changed)
    with pytest.raises(ValueError):e.reader.close(e.context)
    assert D.pin(e.document)==before and not e.actions


@pytest.mark.parametrize('change',['encoding','newlines','restoring_field'])
def test_plain_text_declaration_rejects_unsupported_policies(public_plain_text,tmp_path,change):
    e=public_plain_text
    if change=='encoding':e.contract['encoding']='utf-8-sig'
    elif change=='newlines':e.contract['newlines']='universal'
    else:e.fields[0]['restore']=True
    with pytest.raises(ValueError):_plain_checkpoint(e,tmp_path/'bad-contract')
    assert not e.actions


def test_text_buffer_metadata_exception_combination_refuses_before_observation(public_plain_text,tmp_path,monkeypatch):
    e=public_plain_text;seen=[]
    monkeypatch.setattr(D.PublicAXDocument,'observe',lambda *a:seen.append(a))
    with pytest.raises(ValueError,match='combined text-buffer'):
        D.capture(e.workspace,e.identity,e.target,e.fields,tmp_path/'unsupported-combination',
                  text_buffer=e.contract,metadata_policy=D.DARWIN_LASTUSED)
    assert not seen and not e.actions


@pytest.mark.parametrize('change',[
    'removed_log','empty_log','unlabeled','mismatch','dirty_true','unknown_status','wrong_contract',
    'wrong_buffer_hash','wrong_preclose_leaf','missing_before_close','missing_after_reopen','missing_final',
    'duplicate_before_close','unknown_phase','wrong_action_boundary','outside_recovery_time'])
def test_required_plain_text_scope_rejects_missing_or_contradictory_historical_guards(public_plain_text,tmp_path,change):
    e=public_plain_text;checkpoint=_plain_checkpoint(e,tmp_path/'guarded-checkpoint')
    result=D.restore(checkpoint,D.current_document(checkpoint),tmp_path/'guarded-recovery')
    assert result['status']=='restored',result.get('error')
    D.verify_restored(checkpoint,result['receipt'])
    altered=json.loads(D._verified(result['receipt']))
    guards=altered['text_buffer_observations']
    assert [row['phase']for row in guards]==['initial','before_close','after_reopen','final_saved_state']
    if change=='removed_log':altered.pop('text_buffer_observations')
    elif change=='empty_log':altered['text_buffer_observations']=[]
    elif change=='unlabeled':
        for row in guards:row.pop('phase')
    elif change=='mismatch':guards[-1]['matches']=False
    elif change=='dirty_true':guards[-1]['AXEdited']=True
    elif change=='unknown_status':guards[-1]['status']='unknown'
    elif change=='wrong_contract':guards[-1]['contract']['newlines']='universal'
    elif change=='wrong_buffer_hash':guards[-1]['buffer']['sha256']='0'*64
    elif change=='wrong_preclose_leaf':guards[1]['disk']['identity'][4]+=1
    elif change.startswith('missing_'):
        phase={'missing_before_close':'before_close','missing_after_reopen':'after_reopen',
               'missing_final':'final_saved_state'}[change]
        filler=copy.deepcopy(next(row for row in guards if row['phase']==phase))
        guards[:]=[row for row in guards if row['phase']!=phase]
        # Keep length, action/clock and ordering valid with an optional read;
        # only the required named boundary is now absent.
        filler.update(phase='state_adjustment',action_index=len(altered['actions']),
                      observed_monotonic=guards[-1]['observed_monotonic'])
        guards.insert(len(guards)-1 if guards[-1]['phase']=='final_saved_state' else len(guards),filler)
        for index,row in enumerate(guards):row['sequence']=index
    elif change=='duplicate_before_close':
        guards.insert(2,copy.deepcopy(guards[1]))
        for index,row in enumerate(guards):row['sequence']=index
    elif change=='unknown_phase':guards[-1]['phase']='unproved_boundary'
    elif change=='wrong_action_boundary':guards[1]['action_index']=len(altered['actions'])
    elif change=='outside_recovery_time':guards[-1]['observed_monotonic']=altered['finished_monotonic']+1
    receipt=D._save(tmp_path/'tampered-guard-receipt.json',altered)
    with pytest.raises(ValueError):D.verify_restored(checkpoint,receipt)
    assert e.document.read_bytes()==e.initial.encode('utf-8') and e.actions==['close','reopen']


def test_legacy_required_document_scope_keeps_existing_verification_without_text_guards(document_recovery,tmp_path):
    e=document_recovery
    result=D.restore(e.checkpoint,D.current_document(e.checkpoint),tmp_path/'legacy-recovery',backend=e.backend)
    assert result['status']=='restored' and 'text_buffer_observations'not in result
    assert 'declared_exact_utf8_buffer_matches_disk'not in result['scope']['required']
    D.verify_restored(e.checkpoint,result['receipt'])


def test_separate_recovery_workspaces_have_distinct_public_document_filenames(tmp_path):
    source = tmp_path / "same-source.y4m"; source.write_bytes(b"owned original media bytes")
    source.chmod(0o600); original = D.pin(source)
    workspaces = [D.create_workspace(original, tmp_path / name) for name in ("first", "second")]
    copies = [D._workspace(pin) for pin in workspaces]
    names = [Path(row["document"]["file"]).name for row in copies]
    assert names[0] != names[1] and all(name.endswith(".y4m") for name in names)
    for row in copies:
        assert Path(row["document"]["file"]).name == Path(row["baseline"]["file"]).name
        assert Path(row["document"]["file"]).read_bytes() == source.read_bytes()
        assert row["document"]["sha256"] == row["baseline"]["sha256"] == original["sha256"]
    assert D.pin(source) == original


def test_generic_front_door_connects_model_discovery_public_setup_proof_and_catalog(visual_coordinated, tmp_path, monkeypatch):
    from anyuse.execution import rea_generic_workflow as W
    from test_rea_agent_driver import model, static_decision
    e = visual_coordinated
    policy = e.f.profile["policy"]
    spec = {"format": W.FORMAT, "id": e.f.profile["id"], "task": "Set the requested visible label",
            "before": policy["before"], "after": policy["after"], "observer_model": policy["model"],
            "owned": True, "restart_allowed": True, "params": e.f.profile["params"],
            "window_title": policy["observer"]["window_title"], "plan": G._load(e.root)["plan"],
            "resources": e.cfg["resources"], "replays": e.cfg["replays"],
            "limits": {"trial_seconds": 120}, "publication": e.cfg["publication"]}
    root = tmp_path / "generic-workflow"
    result = W.begin(str(e.f.app), str(G._load(e.root)["rea"]), spec, root)
    # The inert effect producer must bind the newly frozen workflow policy,
    # whose identifier differs from the prebuilt fixture campaign's policy.
    inert_trial = W.V.run_trial
    def measure_current_policy(bundle, actor, policy, *args, **kwargs):
        measured = inert_trial(bundle, actor, policy, *args, **kwargs)
        if measured.get("measurement") is not None:
            actual = measured["measurement"]
            actual["policy_sha256"] = R.digest(policy)
            e.f.measurements[actual["case_id"]]["policy_sha256"] = R.digest(policy)
        return measured
    monkeypatch.setattr(W.V, "run_trial", measure_current_policy)
    frozen = G._load(root / "session")["observer_profile_sha256"]
    adapter, seen = model([static_decision({"op": "propose", "rationale": "Measured typed candidate; independent trials still required",
        "citations": [{"artifact": "inventory.json", "pointer": "/target"}],
        "actor": e.f.actor, "observer_profile_sha256": frozen})])
    result = W.advance(root, adapter=adapter)
    assert result["status"] == "needs_setup", result.get("error")
    assert len(seen) == 1
    for _ in range(16):
        if result["status"] == "completed":
            break
        assert result["status"] in {"needs_setup", "needs_restart"}, result.get("error")
        started = W.public_start(root)
        checkpoint = R.load_json(e.checkpoint({"pending": result["pending"]}))
        result = W.public_finish(root, checkpoint["evidence"], restart=checkpoint.get("restart"))
        assert started["public_handoff"]["sha256"]
    assert result["status"] == "completed", result.get("error")
    assert result["catalog"] and len(e.calls) == 10
    final = G._load(root / "session")
    assert final["status"] == "published" and final["heldout_passed"] is True
    assert final["observer_profile_sha256"] == frozen
    assert [row["kind"] for row in R.load_json(root / "campaign/coordinator.json")["steps"]] == (
        ["record"] * 3 + ["learn"] + ["verify"] * 3 + ["restart"] + ["heldout"] * 3 + ["publish", "select", "replay"])
    assert W.advance(root)["status"] == "completed"
    with pytest.raises(ValueError):
        W.public_finish(root, checkpoint["evidence"])


def test_generic_task_contract_requires_explicit_ownership_and_preserves_model_choice(visual_coordinated):
    from anyuse.execution import rea_generic_workflow as W
    e = visual_coordinated
    policy = e.f.profile["policy"]
    spec = {"format": W.FORMAT, "id": "new_unparameterized_operation", "task": "Change a visible control",
            "before": "The control is disabled", "after": "The control is enabled",
            "observer_model": policy["model"], "owned": True, "restart_allowed": True}
    profile, plan, limits = W.task_contract(e.f.target, spec)
    assert profile["policy"]["model"]["model"] == policy["model"]["model"]
    assert len(plan["learning"]) == len(plan["verification"]) == len(plan["heldout"]) == 3
    assert plan["heldout"][0]["restart"] is True
    for field in ("owned", "restart_allowed"):
        with pytest.raises(ValueError, match="ownership"):
            W.task_contract(e.f.target, {**spec, field: False})


def test_native_type_hypothesis_is_opt_in_bound_to_original_code_and_reopens_on_reuse(visual_coordinated, tmp_path):
    from anyuse.execution import rea_native_contract as N
    from anyuse.execution import rea_runtime_bridge as B
    from test_rea_runtime_bridge import _inert_macho
    e = visual_coordinated
    original = G._load(e.root)
    module = e.f.app / "Contents/Frameworks/Declared.dylib"
    module.parent.mkdir(parents=True, exist_ok=True); module.write_bytes(_inert_macho())
    profile = {k: copy.deepcopy(v) for k, v in e.f.profile.items() if k != "evidence"}
    profile["policy"]["target"] = R.fingerprint(e.f.app)
    root = tmp_path / "declared-native-session"
    G.begin(e.f.app, original["rea"], "Set a visible label",
            profile["params"], ["Label matches the requested input"], original["plan"],
            root, observer_profile=profile, native_contract_mode="declared")
    observed = G.step(root, {"op": "static_evidence", "request": {"kind": "original_code",
        "relative": "Contents/Frameworks/Declared.dylib", "offset": "0x1000", "bytes": 4, "unit_bytes": 4}})
    assert observed["feedback"]["status"] == "completed", observed["feedback"]
    artifact = observed["feedback"]["feedback"]["artifact"]
    unsigned = {"kind": "native_function", "target": {"module": {"path": str(module), "sha256": R._file_hash(module)},
        "offset": "0x1000"}, "abi": "default", "arguments": [{"type": "int32", "value": {"input": "query"}}], "return_type": "void"}
    declared = G.step(root, {"op": "declare_native", "actor": unsigned,
                            "citations": [{"artifact": artifact, "pointer": "/range"}]})
    assert declared["feedback"]["status"] == "completed", declared["feedback"]
    actor = declared["feedback"]["feedback"]["actor"]
    assert actor["signature_evidence"]["kind"] == N.KIND
    doc = R.load_json(actor["signature_evidence"]["file"])
    assert doc["abi_recovered"] is doc["independently_reviewed"] is doc["actuated"] is False
    resources = B.native_resources(B.bind_actor(actor, {"query": "9"}))
    assert str(module) in resources and str(root / artifact) in resources
    changed = copy.deepcopy(actor); changed["target"]["offset"] = "0x1004"
    with pytest.raises(ValueError, match="target, ABI or context"):
        B.native_resources(changed)
    changed = copy.deepcopy(actor); changed["signature_evidence"]["kind"] = "reviewed_contract"
    with pytest.raises(ValueError, match="declared-native"):
        B.native_resources(changed)
    state = G._load(root); state["native_contract_mode"] = "attributable"; R.write_json(root / "session.json", state)
    with pytest.raises(ValueError, match="policy changed"):
        B.native_resources(actor)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__,'-q']))
