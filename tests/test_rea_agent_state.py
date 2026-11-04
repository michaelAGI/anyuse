"""Persistent G fixed-state workflow with real campaign gates and inert trials."""
import copy
from pathlib import Path

import pytest

from test_rea_state_campaign import campaign, pinned_json
from anyuse.execution import rea_agent as G
from anyuse.execution import rea_state_campaign as C
from anyuse.execution import rea_mapping as R


@pytest.fixture
def state_session(campaign, tmp_path):
    rea = tmp_path / "rea-inert"
    rea.write_text("never executed")
    rea.chmod(0o700)
    plan = {"format": C.PLAN}
    for role, phase in [("learning", "learning"), ("verification", "fresh"), ("heldout", "heldout")]:
        plan[role] = [{"id": f"{phase}-{i}", "inputs": {}, "restart": phase == "heldout" and i == 0} for i in range(3)]
    root = tmp_path / "session"
    context = G.begin(str(campaign.app), str(rea), "Pause the current playing video", [],
                      ["Independent moving-to-stable video and public-control transition"], plan, str(root))
    assert context["workflow"] == C.BACKEND and context["capabilities"]["actor_inputs"] == {}
    return root, plan


def propose(campaign, state_session):
    root, _ = state_session
    result = G.step(root, {"op": "resource", "relative": "Contents/Info.plist"})
    artifact = next(name for name in result["context"]["artifacts"] if name.endswith("resource.json"))
    request = {"op": "propose", "profile": campaign.profile, "actor": campaign.actor,
               "rationale": "Measured typed operation with independent playback state contract",
               "citations": [{"artifact": artifact, "pointer": "/content/CFBundleIdentifier"}]}
    assert G.step(root, request)["context"]["status"] == "hypothesis"


def learned(campaign, state_session):
    root, _ = state_session
    propose(campaign, state_session)
    for i in range(3):
        before = len(campaign.calls)
        result = G.step(root, {"op": "record", "case": i})
        assert result["feedback"]["status"] == "completed" and len(campaign.calls) == before + 1, result["feedback"]
    result = G.step(root, {"op": "learn"})
    assert result["context"]["status"] == "candidate" and not result["context"]["proof_created"]


def test_g_executes_one_trial_per_step_then_publishes_complete_campaign(campaign, state_session):
    root, _ = state_session
    learned(campaign, state_session)
    for i in range(3):
        before = len(campaign.calls)
        result = G.step(root, {"op": "verify", "case": i})
        assert result["feedback"]["status"] == "completed" and len(campaign.calls) == before + 1
        assert result["context"]["proof_created"] is False
    assert result["context"]["status"] == "verified"
    premature = G.step(root, {"op": "publish", "summary": "Pause", "details": "Held-out trials are still pending."})
    assert premature["feedback"]["status"] == "failed" and not premature["context"]["proof_created"]
    assert len(campaign.calls) == 6
    for i in range(3):
        request = {"op": "heldout", "case": i}
        if i == 0: request["restart"] = campaign.restart
        result = G.step(root, request)
        assert result["feedback"]["status"] == "completed"
    assert len(campaign.calls) == 9 and result["context"]["proof_created"] is True
    result = G.step(root, {"op": "publish", "summary": "Pause current playback",
                           "details": "Fixed transition measured across three phases and a witnessed restart."})
    assert result["context"]["status"] == "published"
    assert len(R.load_json(result["context"]["published"])["actions"]) == 1


@pytest.mark.parametrize("operation", ["verify", "heldout"])
def test_g_evaluation_failure_closes_session_and_keeps_actual_attempt(campaign, state_session, operation):
    root, _ = state_session
    learned(campaign, state_session)
    if operation == "heldout":
        for i in range(3):
            assert G.step(root, {"op": "verify", "case": i})["feedback"]["status"] == "completed"
    request = {"op": operation, "case": 0}
    if operation == "heldout": request["restart"] = campaign.restart
    campaign.failure = "independent before was black; no native dispatch"
    failed = G.step(root, request)
    assert failed["feedback"]["status"] == "failed" and failed["context"]["status"] == "evaluation_failed"
    attempts = len(campaign.calls)
    campaign.failure = None
    retry = G.step(root, request)
    assert retry["feedback"]["status"] == "failed" and len(campaign.calls) == attempts
    publish = G.step(root, {"op": "publish", "summary": "Pause", "details": "Cannot erase the failure"})
    assert publish["feedback"]["status"] == "failed" and not publish["context"]["proof_created"]
    assert any(row["status"] == "failed" and "black" in row["feedback"]["error"] for row in publish["context"]["history"])


def test_g_rejects_json_callbacks_and_out_of_order_cases_before_trial(campaign, state_session):
    root, _ = state_session
    propose(campaign, state_session)
    with pytest.raises(ValueError, match="invalid mapping tool arguments"):
        G.step(root, {"op": "record", "case": 0, "setup_hook": "module.function"})
    result = G.step(root, {"op": "record", "case": 1})
    assert result["feedback"]["status"] == "failed" and campaign.calls == []
    assert "frozen phase order" in result["feedback"]["feedback"]["error"]


def test_g_plan_rejects_actor_input_novelty_and_undeclared_callbacks(campaign, state_session):
    _, plan = state_session
    bad = copy.deepcopy(plan)
    bad["learning"][0]["inputs"] = {"trial_id": "not-an-argument"}
    with pytest.raises(ValueError, match="inputs"):
        C.validate_plan(bad)
    bad = copy.deepcopy(plan)
    bad["learning"][0]["callback"] = "app_specific_runner.py"
    with pytest.raises(ValueError, match="closed"):
        C.validate_plan(bad)


def test_g_uncertain_cleanup_blocks_further_runtime_or_task_attempts(campaign, state_session):
    root, _ = state_session
    learned(campaign, state_session)
    campaign.failure_result = {"status": "failed", "resource_review_required": True,
                               "error": {"message": "native cleanup is uncertain"}}
    failed = G.step(root, {"op": "verify", "case": 0})
    assert failed["context"]["status"] == "resource_review_required"
    attempts = len(campaign.calls)
    for request in ({"op": "verify", "case": 0}, {"op": "discover", "request": {"operations": []}}):
        with pytest.raises(R.ResourceReviewRequired):
            G.step(root, request)
    assert len(campaign.calls) == attempts


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
