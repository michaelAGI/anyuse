"""The agent harness's outcome (core/agent.py ``_decide_outcome``) and the execution's
STOP kinds (execution/outcome.py: ``refused``, ``deviation``, ``uncertain_completion``,
``terminal_refusal``; ``ExecutionResult.stopped``), aligned.

A replay the execution stopped is never a success: the orchestrator carries the stop kind
(``OrchestratorResult.outcome_kind``), the agent's replay status keeps it in its reason
(``outcome.stop_text``), the learning orchestrator's replay-first path ends the run with it
instead of handing the screen to computer-use, and ``_decide_outcome`` reads it before any
verifier: an unobserved effect is UNCERTAIN, every other stop FAILED. Fakes only.

Run from the anyuse package dir:
    cd anyuse && python3 test_replay_stop_outcome.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.core.agent import Agent, replay_outcome  # noqa: E402
from anyuse.core.result import AgentResult, ResultStatus  # noqa: E402
from anyuse.execution import outcome as O  # noqa: E402
from anyuse.execution.learning import LearningOrchestrator  # noqa: E402
from anyuse.execution.orchestrator import ExecutionOrchestrator, OrchestratorResult  # noqa: E402


def test_stop_text_round_trips_every_stop_kind():
    for kind in O.STOP_KINDS:
        assert O.stopped_kind(O.stop_text(kind, "why")) == kind
    assert O.stopped_kind("Task complete") == "" and O.stopped_kind("stopped (nonsense): x") == ""


def test_replay_outcome_keeps_the_stop_kind():
    ok = replay_outcome(OrchestratorResult(success=True, message="Replayed"))
    assert ok[0] is ResultStatus.SUCCESS
    for kind, status in (("refused", ResultStatus.FAILED), ("deviation", ResultStatus.FAILED),
                         ("terminal_refusal", ResultStatus.FAILED),
                         ("uncertain_completion", ResultStatus.UNCERTAIN)):
        st, why = replay_outcome(OrchestratorResult(success=False, message="m", outcome_kind=kind))
        assert st is status and O.stopped_kind(why) == kind, (kind, st, why)
    st, why = replay_outcome(OrchestratorResult(success=False, message="no path"))
    assert st is ResultStatus.FAILED and O.stopped_kind(why) == ""


class _Orch:
    def __init__(self, response):
        self.response, self.history = response, [1, 2]

    def run(self, _task, **_kw):
        return self.response


class _NoVerifier:
    def verify_goal(self, **_kw):
        raise AssertionError("a run the execution stopped is not re-judged by a verifier")


def _agent(response):
    a = Agent(enable_planning=False, enable_memory=False, enable_credentials=False,
              enable_verification=True, auto_learn=False, auto_replay=False)
    a._orchestrator = _Orch(response)
    a._verifier = _NoVerifier()
    a._screenshot_fn = lambda: b"png"
    return a


def test_decide_outcome_reads_the_stop_before_any_verifier():
    r = _agent(O.stop_text("deviation", "changed outside the declared effect")).run("save")
    assert r.status is ResultStatus.FAILED and not r.success and "deviation" in r.outcome_reason
    r = _agent(O.stop_text("uncertain_completion", "not observed")).run("save")
    assert r.status is ResultStatus.UNCERTAIN and not r.success


class _Storage:
    def __init__(self, wf):
        self.wf = wf

    def find_with_details(self, task):
        return self.wf, 0.95, "match"


class _Teacher:
    calls = 0

    def run(self, task, recorder=None):
        _Teacher.calls += 1
        return "Task complete (computer-use)"


class _WF:
    name = "rechnung senden"


def test_a_stopped_replay_ends_the_run_and_is_not_handed_to_computer_use():
    stop = O.stop_text("refused", "wrong document: the target's window shows /b/notes.txt")
    played = AgentResult(status=ResultStatus.FAILED, success=False, message="m", outcome_reason=stop)
    lo = LearningOrchestrator(_Teacher(), storage=_Storage(_WF()), auto_learn=False, auto_replay=True,
                              replay_fn=lambda name: played)
    assert lo.run("rechnung senden") == stop and _Teacher.calls == 0
    missed = AgentResult(status=ResultStatus.FAILED, success=False, message="no path", outcome_reason="no path")
    lo = LearningOrchestrator(_Teacher(), storage=_Storage(_WF()), auto_learn=False, auto_replay=True,
                              replay_fn=lambda name: missed)
    assert lo.run("rechnung senden") == "Task complete (computer-use)" and _Teacher.calls == 1


def test_the_orchestrator_carries_the_stop_kind_of_a_stopped_replay():
    from anyuse.memory.models import WorkflowTemplate
    orch = ExecutionOrchestrator.__new__(ExecutionOrchestrator)
    orch._verbose, orch._planner = False, None

    async def _replay(workflow, state_graph=None, ax_tree=None, executor=None):
        return {"success": False, "outcome_kind": "deviation", "stopped_at": "save",
                "reason": "changed outside the declared effect", "llm_calls": 0}

    orch.replay_deterministic = _replay
    wf = WorkflowTemplate.create_new("t", "t")
    wf.state_path = ["a", "b"]
    os.environ["ANYUSE_DETERMINISTIC_REPLAY"] = "1"
    res = asyncio.run(orch._execute_workflow("t", wf, {}, None, None))
    assert res.success is False and res.outcome_kind == "deviation", res.to_dict()
    assert res.to_dict()["outcome_kind"] == "deviation"
    assert replay_outcome(res)[0] is ResultStatus.FAILED


def _cleanup_run(primary, report, *, clean_room=True, restore_error=None, prepare_error=None):
    from anyuse.memory.environment import RestoreReport
    from anyuse.memory.models import WorkflowTemplate
    calls = []
    class Environment:
        def prepare(self, app):
            calls.append(("prepare", app))
            if prepare_error is not None:
                raise prepare_error
            return object()
        def restore(self, saved):
            calls.append(("restore", saved))
            if restore_error is not None:
                raise restore_error
            return report
    orch = ExecutionOrchestrator.__new__(ExecutionOrchestrator)
    orch._verbose, orch._environment = False, Environment()
    async def execute(*args):
        if isinstance(primary, Exception):
            raise primary
        return primary
    orch._execute_workflow = execute
    wf = WorkflowTemplate.create_new("cleanup", "cleanup")
    wf.required_app = "Example"
    return orch, wf, calls, lambda: asyncio.run(orch.run("cleanup", wf, use_clean_room=clean_room))


def test_successful_primary_action_keeps_its_result_and_exposes_partial_cleanup():
    from anyuse.memory.environment import RestoreReport
    report = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"},
        {"field": "frontmost_app", "status": "unknown"}])
    primary = OrchestratorResult(success=True, message="Work complete")
    orch, wf, calls, run = _cleanup_run(primary, report)
    result = run()
    assert result is primary and result.success is True and result.message == "Work complete"
    assert result.outcome_kind == "" and result.restoration_report is report
    wire = result.to_dict()
    assert wire["success"] is True and wire["cleanup_status"] == "partial"
    assert wire["completion_status"] == "incomplete" and wire["restoration"] == report.to_dict()
    assert orch.last_restoration_report is report and len(calls) == 2


def test_failed_primary_action_is_not_replaced_by_cleanup_outcome():
    from anyuse.memory.environment import RestoreReport
    report = RestoreReport.unknown("original window closed")
    primary = OrchestratorResult(success=False, message="Primary refusal", outcome_kind="refused")
    orch, wf, calls, run = _cleanup_run(primary, report)
    result = run()
    assert result is primary and result.success is False
    assert result.message == "Primary refusal" and result.outcome_kind == "refused"
    assert result.restoration_report is report and result.to_dict()["cleanup_status"] == "unknown"
    assert replay_outcome(result)[0] is ResultStatus.FAILED


def test_unrequested_cleanup_is_always_serialized_and_performs_no_environment_calls():
    primary = OrchestratorResult(success=True, message="Work complete")
    orch, wf, calls, run = _cleanup_run(primary, None, clean_room=False)
    result = run()
    assert calls == [] and result.success is True
    assert result.to_dict()["restoration"]["status"] == "not_requested"
    assert result.to_dict()["cleanup_status"] == "not_requested"
    assert result.to_dict()["completion_status"] == "complete"


def test_cleanup_exception_is_visible_without_overwriting_a_successful_action():
    primary = OrchestratorResult(success=True, message="Saved")
    orch, wf, calls, run = _cleanup_run(primary, None, restore_error=RuntimeError("window state unavailable"))
    result = run()
    assert result is primary and result.success and result.message == "Saved"
    assert result.restoration_report.status == "unknown"
    assert "RuntimeError: window state unavailable" in result.restoration_report.fields[0]["reason"]
    assert result.completion_status == "incomplete"


def test_primary_exception_propagates_unchanged_while_cleanup_failure_is_retained():
    primary = ValueError("primary execution failure")
    orch, wf, calls, run = _cleanup_run(primary, None, restore_error=RuntimeError("cleanup unavailable"))
    try:
        run()
    except ValueError as caught:
        assert caught is primary and str(caught) == "primary execution failure"
    else:
        raise AssertionError("primary exception was swallowed")
    assert len(calls) == 2 and orch.last_restoration_report.status == "unknown"
    assert "cleanup unavailable" in orch.last_restoration_report.fields[0]["reason"]


def test_preparation_failure_does_not_claim_clean_restoration():
    primary = OrchestratorResult(success=True, message="Work complete")
    orch, wf, calls, run = _cleanup_run(primary, None, prepare_error=RuntimeError("capture unavailable"))
    result = run()
    assert result is primary and result.success and len(calls) == 1
    assert result.restoration_report.status == "unknown" and result.completion_status == "incomplete"
    assert "capture unavailable" in result.restoration_report.fields[0]["reason"]


def test_legacy_cleanup_without_a_report_remains_unknown():
    primary = OrchestratorResult(success=True, message="Work complete")
    orch, wf, calls, run = _cleanup_run(primary, None)
    result = run()
    assert result.restoration_report.status == "unknown"
    assert result.to_dict()["completion_status"] == "incomplete"


def test_restore_aggregate_does_not_claim_partial_success_without_a_restored_control():
    from anyuse.core.result import RestoreReport
    report = RestoreReport.from_fields([{"field": "do_not_disturb", "status": "failed"},
                                       {"field": "frontmost_app", "status": "unknown"}])
    assert report.status == "failed"
    assert RestoreReport.from_fields([]).status == "unknown"


def test_agent_result_defaults_and_cleanup_serialization_preserve_primary_success():
    import json
    from anyuse.core.result import RestoreReport
    plain = AgentResult.success_result("done")
    assert plain.to_dict()["cleanup_status"] == "not_requested"
    assert plain.to_dict()["completion_status"] == "complete" and bool(plain)
    report = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"},
                                       {"field": "frontmost_app", "status": "unknown"}])
    result = AgentResult.success_result("done", restoration_report=report)
    wire = json.loads(result.to_json())
    assert wire["success"] is True and wire["status"] == "success" and bool(result)
    assert wire["cleanup_status"] == "partial" and wire["completion_status"] == "incomplete"
    assert wire["restoration"] == report.to_dict()
    wire["restoration"]["fields"].clear()
    assert len(report.fields) == 2 and plain.restoration_report.fields == []


def _fake_saved_replay(primary):
    from types import SimpleNamespace
    from anyuse.memory.models import WorkflowTemplate
    workflow = SimpleNamespace(id="saved", name="saved", description="replay saved task")
    template = WorkflowTemplate.create_new("saved", "replay saved task")
    template.required_app = "Example"
    class Storage:
        def find(self, task):
            return workflow
        def find_with_details(self, task):
            return workflow, 1.0, "fixture"
        def get(self, name):
            return workflow
        def has_template(self, identifier):
            return True
        def get_template(self, identifier):
            return template
        def list_all(self):
            return []
    class Orchestrator:
        def __init__(self, **kwargs):
            pass
        async def run(self, *args, **kwargs):
            return primary
    guides = SimpleNamespace(get_by_app=lambda app: None)
    return Storage(), workflow, Orchestrator, guides


def test_smart_player_carries_cleanup_report_without_changing_primary_failure():
    from types import SimpleNamespace
    from unittest import mock
    from anyuse.core.result import RestoreReport
    from anyuse.memory.player import SmartPlayer
    report = RestoreReport.unknown("original window closed")
    primary = OrchestratorResult(success=False, message="Primary refusal", outcome_kind="refused",
                                 duration_seconds=2.0, restoration_report=report)
    storage, workflow, orchestrator, guides = _fake_saved_replay(primary)
    player = SmartPlayer(SimpleNamespace(storage=storage, executor=object()))
    with mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", orchestrator), \
            mock.patch("anyuse.guides.storage.GuideStorage", return_value=guides):
        result = player.run_or_play("replay saved task")
    assert not result.success and result.status is ResultStatus.FAILED
    assert result.message == "Primary refusal" and result.error == "Primary refusal"
    assert result.to_dict()["restoration"] == report.to_dict()
    assert result.to_dict()["cleanup_status"] == "unknown"
    assert result.completion_status == "incomplete" and result.duration_seconds == 2.0
    result.restoration_report.fields.clear()
    assert len(primary.restoration_report.fields) == 1


def test_agent_replay_and_run_preserve_cleanup_through_the_learning_text_wrapper():
    from types import SimpleNamespace
    from unittest import mock
    from anyuse.core.result import RestoreReport
    report = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"},
                                       {"field": "frontmost_app", "status": "unknown"}])
    primary = OrchestratorResult(success=True, message="Saved", mode="replay-deterministic",
                                 restoration_report=report)
    storage, workflow, orchestrator, guides = _fake_saved_replay(primary)
    agent = Agent(enable_planning=False, enable_memory=True, enable_credentials=False,
                  enable_verification=False, auto_learn=False, auto_replay=True, verbose=False)
    agent._workflows, agent._guide_storage = storage, guides
    agent._orchestrator = SimpleNamespace(executor=object(), history=[])
    agent._record_play = lambda *args: None
    agent._focused_text_fn = lambda: None
    with mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", orchestrator), \
            mock.patch("anyuse.execution.draft_compile.play_refusal", return_value=None):
        direct = agent.play(workflow.name)
        result = agent.run("replay saved task")
    assert direct.success and direct.status is ResultStatus.SUCCESS
    assert result.success and result.status is ResultStatus.SUCCESS
    assert direct.to_dict()["restoration"] == result.to_dict()["restoration"] == report.to_dict()
    assert result.completion_status == "incomplete" and result.to_dict()["cleanup_status"] == "partial"
    assert result.llm_calls == 0


def _interrupted_saved_replay(report):
    storage, workflow, _, guides = _fake_saved_replay(None)
    class Orchestrator:
        calls = 0
        def __init__(self, **kwargs):
            self.last_restoration_report = report
        async def run(self, *args, **kwargs):
            Orchestrator.calls += 1
            raise ValueError("healing failed after action")
    return storage, workflow, Orchestrator, guides


def test_smart_player_returns_started_replay_exception_with_cleanup_without_repeating_task():
    from types import SimpleNamespace
    from unittest import mock
    from anyuse.core.result import RestoreReport
    from anyuse.memory.player import SmartPlayer
    report = RestoreReport.unknown("original window closed")
    storage, workflow, orchestrator, guides = _interrupted_saved_replay(report)
    fresh_agent = SimpleNamespace(run=mock.Mock(side_effect=AssertionError("started replay must not repeat")))
    player = SmartPlayer(SimpleNamespace(storage=storage, executor=object()), agent=fresh_agent)
    with mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", orchestrator), \
            mock.patch("anyuse.guides.storage.GuideStorage", return_value=guides):
        result = player.run_or_play("replay saved task")
    assert not result.success and result.status is ResultStatus.FAILED
    assert result.error == "ValueError: healing failed after action"
    assert O.stopped_kind(result.outcome_reason) == O.INCOMPLETE
    assert result.to_dict()["restoration"] == report.to_dict()
    assert result.to_dict()["cleanup_status"] == "unknown" and result.completion_status == "incomplete"
    assert orchestrator.calls == 1
    fresh_agent.run.assert_not_called()


def test_agent_run_keeps_interrupted_replay_cleanup_and_does_not_call_teacher_or_legacy_player():
    from types import SimpleNamespace
    from unittest import mock
    from anyuse.core.result import RestoreReport
    report = RestoreReport.from_fields([{"field": "window_geometry", "status": "restored"},
                                       {"field": "frontmost_app", "status": "unknown"}])
    storage, workflow, orchestrator, guides = _interrupted_saved_replay(report)
    agent = Agent(enable_memory=True, enable_credentials=False, enable_verification=False,
                  auto_learn=False, auto_replay=True, verbose=False)
    teacher = mock.Mock(side_effect=AssertionError("started replay must not repeat"))
    agent._workflows, agent._guide_storage = storage, guides
    agent._orchestrator = SimpleNamespace(executor=object(), history=[], run=teacher)
    with mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", orchestrator), \
            mock.patch("anyuse.execution.draft_compile.play_refusal", return_value=None), \
            mock.patch("anyuse.memory.player.WorkflowPlayer", side_effect=AssertionError("no legacy retry")) as legacy:
        result = agent.run("replay saved task")
    assert not result.success and result.status is ResultStatus.FAILED
    assert "ValueError: healing failed after action" in result.message
    assert O.stopped_kind(result.outcome_reason) == O.INCOMPLETE
    assert result.to_dict()["restoration"] == report.to_dict()
    assert result.to_dict()["cleanup_status"] == "partial" and result.completion_status == "incomplete"
    assert orchestrator.calls == 1 and result.llm_calls == 0
    teacher.assert_not_called()
    legacy.assert_not_called()


def test_unavailable_orchestrator_before_execution_can_still_use_existing_fallbacks():
    from types import SimpleNamespace
    from unittest import mock
    from anyuse.memory.player import SmartPlayer
    storage, workflow, _, guides = _fake_saved_replay(None)
    fresh = AgentResult.success_result("fresh task")
    fresh_agent = SimpleNamespace(run=mock.Mock(return_value=fresh))
    smart = SmartPlayer(SimpleNamespace(storage=storage, executor=object()), agent=fresh_agent)
    agent = Agent(enable_memory=True, enable_credentials=False, verbose=False)
    agent._workflows, agent._guide_storage = storage, guides
    agent._orchestrator = SimpleNamespace(executor=object())
    legacy = SimpleNamespace(play=mock.Mock(return_value=fresh))
    with mock.patch("anyuse.execution.orchestrator.ExecutionOrchestrator", side_effect=ImportError("optional engine unavailable")), \
            mock.patch("anyuse.guides.storage.GuideStorage", return_value=guides), \
            mock.patch("anyuse.execution.draft_compile.play_refusal", return_value=None), \
            mock.patch("anyuse.memory.player.WorkflowPlayer", return_value=legacy):
        smart_result = smart.run_or_play("fixture task")
        agent_result = agent.play(workflow.name)
    assert smart_result is fresh and agent_result is fresh
    assert smart_result.to_dict()["cleanup_status"] == "not_requested"
    fresh_agent.run.assert_called_once_with("fixture task")
    legacy.play.assert_called_once_with(workflow)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
