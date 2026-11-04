"""Replay-match seam: LearningOrchestrator replays a matched saved workflow
token-free BEFORE falling to the expensive computer-use teacher."""
import types
import pytest

from anyuse.execution.learning import LearningOrchestrator


@pytest.fixture(autouse=True)
def isolated_fallback_tracking(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "anyuse"))
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", "default")
    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "3")


class _FakeStorage:
    def __init__(self, match):
        self._match = match

    def find_with_details(self, task):
        return self._match  # (workflow, confidence, reason) | None


class _FakeOrch:
    def __init__(self):
        self.ran = []

    def run(self, task, recorder=None):
        self.ran.append(task)
        return "computer-use ran"


class _Res:
    def __init__(self, success, message):
        self.success = success
        self.message = message


def _wf(name="rename_file"):
    return types.SimpleNamespace(name=name)


def test_replays_on_confident_match_without_computer_use():
    orch = _FakeOrch()
    replayed = []
    lo = LearningOrchestrator(orch, storage=_FakeStorage((_wf(), 0.9, "good match")), auto_replay=True,
                              replay_fn=lambda name: (replayed.append(name) or _Res(True, "replayed ok")))
    out = lo.run("rename the file")
    assert out == "replayed ok"
    assert replayed == ["rename_file"]
    assert orch.ran == [], "must NOT fall to computer-use when replay succeeds"


def test_falls_to_computer_use_on_no_match():
    orch = _FakeOrch()
    lo = LearningOrchestrator(orch, storage=_FakeStorage(None), auto_learn=False, auto_replay=True,
                              replay_fn=lambda n: _Res(True, "x"))
    lo.run("novel task")
    assert orch.ran == ["novel task"]


def test_falls_to_computer_use_on_low_confidence():
    orch = _FakeOrch()
    lo = LearningOrchestrator(orch, storage=_FakeStorage((_wf(), 0.3, "weak")), auto_learn=False, auto_replay=True,
                              replay_fn=lambda n: _Res(True, "x"))
    lo.run("t")
    assert orch.ran == ["t"]


def test_falls_to_computer_use_on_replay_failure():
    orch = _FakeOrch()
    lo = LearningOrchestrator(orch, storage=_FakeStorage((_wf(), 0.9, "good")), auto_learn=False, auto_replay=True,
                              replay_fn=lambda n: _Res(False, "failed"))
    lo.run("t")
    assert orch.ran == ["t"], "replay failure must fall through to computer-use"


def test_no_replay_when_auto_replay_disabled():
    orch = _FakeOrch()
    lo = LearningOrchestrator(orch, storage=_FakeStorage((_wf(), 0.9, "g")), auto_learn=False, auto_replay=False,
                              replay_fn=lambda n: _Res(True, "x"))
    lo.run("t")
    assert orch.ran == ["t"]


def test_replay_reentry_does_not_recurse():
    # Simulate WorkflowPlayer's LLM fallback re-entering learner.run mid-replay.
    # The _replaying guard must short-circuit the inner call to computer-use so
    # replay_fn is invoked exactly once (no unbounded recursion / token burn).
    orch = _FakeOrch()
    calls = {"n": 0}
    holder = {}

    def reentrant_replay(name):
        calls["n"] += 1
        holder["lo"].run("same task again")  # re-enters; guard must block re-replay
        return _Res(False, "outer replay failed")

    lo = LearningOrchestrator(orch, storage=_FakeStorage((_wf(), 0.9, "match")), auto_learn=False,
                              auto_replay=True, replay_fn=reentrant_replay)
    holder["lo"] = lo
    lo.run("task")
    assert calls["n"] == 1, f"replay_fn must run once, not recurse: {calls}"
    assert orch.ran, "re-entry + outer failure must reach computer-use"



# ---- a failed replay's takeover -------------------------------------

class _RecordingOrch:
    def __init__(self):
        self.ran = []

    def run(self, task, recorder=None):
        self.ran.append((task, recorder))
        return "computer-use ran"


class _NeverAsked:
    def find_with_details(self, task):
        raise AssertionError("replay=False must not look for a saved workflow")


def test_n13_replay_false_never_tries_a_saved_workflow_and_learn_false_records_nothing():
    orch = _RecordingOrch()
    lo = LearningOrchestrator(orch, storage=_NeverAsked(), auto_learn=True, auto_replay=True,
                              replay_fn=lambda n: _Res(True, "x"))
    made = []
    lo._make_recorder = lambda task: made.append(task) or "a recorder"
    assert lo.run("move e2-e4", replay=False, learn=False) == "computer-use ran"
    assert orch.ran == [("move e2-e4", None)] and made == []
    lo.run("move e2-e4", replay=False)
    assert orch.ran[-1] == ("move e2-e4", "a recorder") and made == ["move e2-e4"]


def _player_module():
    """memory.player without pyautogui (its template matcher imports it; nothing here moves anything)."""
    import sys
    sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
    from anyuse.memory import player as P
    return P


def test_n14_a_failed_step_hands_the_model_the_task_in_words_not_the_recorded_steps():
    P = _player_module()
    from anyuse.core.result import AgentResult, ResultStatus
    from anyuse.memory.models import Workflow, WorkflowStep
    wf = Workflow(id="w1", name="open_chess_e2e4", description="Open Chess and move the white pawn from e2 to e4",
                  steps=[WorkflowStep(action="key", args={"keys": "command+n"}, description="key(keys='command+n')"),
                         WorkflowStep(action="click_at", args={"x": 596, "y": 566},
                                      description="click_at(x=596, y=566)")])
    asked, stats = [], []

    class _Agent:
        def run(self, goal, **kw):
            asked.append((goal, kw))
            return AgentResult(status=ResultStatus.SUCCESS, success=True, message="moved", task=goal)

    class _Exec:
        def execute(self, action, args):
            return {"success": False, "error": f"unknown action: {action}"}

    class _Store:
        def get_workflow_dir(self, wid):
            return "/nonexistent/workflow_" + wid

        def update_stats(self, name, success, error=""):
            stats.append((name, success, error))
    player = P.WorkflowPlayer(_Exec(), agent=_Agent(), storage=_Store())
    out = player.play(wf, prepare_window=False)
    (goal, kw), = asked
    assert kw == {"replay": False, "learn": False}
    assert goal.startswith("Open Chess and move the white pawn from e2 to e4")
    assert "step 1" in goal and "596" not in goal and "click_at" not in goal     # no screen points to misread
    assert stats == [("open_chess_e2e4", False, "step 1 failed: unknown action: key")]
    assert out.success and "LLM took over" in out.message
    # an agent without the switches still gets the task
    asked.clear()

    class _OldAgent:
        def run(self, goal):
            asked.append(goal)
            return AgentResult(status=ResultStatus.FAILED, success=False, message="no", task=goal)
    P.WorkflowPlayer(_Exec(), agent=_OldAgent(), storage=_Store()).play(wf, prepare_window=False)
    assert len(asked) == 1 and asked[0].startswith("Open Chess")


# ---- the matcher's model --------------------------------------------------

def test_n20_the_matcher_sends_its_gemini_model_id_only_to_gemini():
    # the bug: ANYUSE_MODEL_NAME=sonnet, yet every match request asked for gemini-3.5-flash
    import os
    from anyuse.memory import storage as S
    keys = [k for k in os.environ if k.startswith(("ANYUSE_MODEL_", "ANYUSE_PLANNER_MODEL_"))]
    saved = {k: os.environ.pop(k) for k in keys}
    try:
        cfg = S._matcher_adapter().config
        assert (cfg.provider, cfg.model) == ("gemini", S.MATCHER_MODEL)
        os.environ["ANYUSE_MODEL_NAME"] = "sonnet"
        assert S._matcher_adapter().config.model == "sonnet"           # the configured name wins
        del os.environ["ANYUSE_MODEL_NAME"]
        os.environ["ANYUSE_PLANNER_MODEL_PROVIDER"] = "local"
        cfg = S._matcher_adapter().config
        assert cfg.provider == "local" and "gemini" not in cfg.model     # the local preset's own model
    finally:
        for k in ("ANYUSE_MODEL_NAME", "ANYUSE_PLANNER_MODEL_PROVIDER"):
            os.environ.pop(k, None)
        os.environ.update(saved)


def test_n21_frequency_threshold_flags_repeated_vision_fallbacks_for_rea_v2_and_resolves_effort():
    import os
    from anyuse.execution.learning import LearningOrchestrator, resolve_mapping_effort

    saved = {k: os.environ.pop(k) for k in ("ANYUSE_MAP_EFFORT", "ANYUSE_REA_AUTO_THRESHOLD") if k in os.environ}
    try:
        cfg_default = resolve_mapping_effort("default")
        assert cfg_default["record_http_dom"] is True and cfg_default["record_ax"] is True
        assert cfg_default["rea_enabled"] is False and cfg_default["native_receiver_mode"] is None

        cfg_fast = resolve_mapping_effort("rea-fast")
        assert cfg_fast["rea_enabled"] is True
        assert cfg_fast["native_contract_mode"] == "declared"
        assert cfg_fast["native_receiver_mode"] == "binary_runtime_hypothesis"
        assert cfg_fast["trial_policy"] == {
            "min_learning": 1, "min_verification": 1, "min_heldout": 1, "require_restart": True
        }

        cfg_custom = resolve_mapping_effort("rea-fast", trials_learn=2, trials_fresh=1, trials_heldout=1, require_restart=False)
        assert cfg_custom["trial_policy"] == {
            "min_learning": 2, "min_verification": 1, "min_heldout": 1, "require_restart": False
        }

        os.environ["ANYUSE_REA_AUTO_THRESHOLD"] = "3"
        orch = _RecordingOrch()
        lo = LearningOrchestrator(orch, storage=None, auto_learn=False, auto_replay=False)
        for i in range(2):
            lo.run("select Sep in vacation picker")
            status = lo.rea_escalation_status("select Sep in vacation picker")
            assert status["vision_fallbacks"] == i + 1
            assert status["escalate_to_rea"] is False
            assert lo.list_rea_candidates() == []

        lo.run("select Sep in vacation picker")
        status = lo.rea_escalation_status("select Sep in vacation picker")
        assert status["vision_fallbacks"] == 3
        assert status["escalate_to_rea"] is True
        assert status["recommended_config"]["effort"] == "rea-fast"
        assert status["recommended_config"]["native_receiver_mode"] == "binary_runtime_hypothesis"
        assert lo.get_learning_stats()["rea_candidates"] == ["select_sep_in_vacation_picker"]
    finally:
        for k in ("ANYUSE_MAP_EFFORT", "ANYUSE_REA_AUTO_THRESHOLD"):
            os.environ.pop(k, None)
        os.environ.update(saved)


@pytest.mark.parametrize("mode,effort,escalated,expected", [
    ("auto", "default", False, "off"),
    ("auto", "default", True, "windows"),
    ("auto", "rea-fast", False, "live"),
    ("auto", "rea-strict", True, "live"),
    ("off", "rea-fast", True, "off"),
    ("off", "default", True, "off"),
    ("windows", "rea-strict", True, "windows"),
    ("windows", "default", False, "windows"),
    ("live", "default", False, "live"),
    ("live", "default", True, "live"),
])
def test_gui_capture_setting_separates_collection_from_proof_effort(monkeypatch, mode, effort, escalated, expected):
    from anyuse.execution.learning import resolve_gui_capture_mode
    monkeypatch.setenv("ANYUSE_REA_GUI_CAPTURE", mode)
    monkeypatch.setenv("ANYUSE_MAP_EFFORT", effort)
    assert resolve_gui_capture_mode(escalate_to_rea=escalated) == expected


def test_explicit_gui_capture_mode_does_not_resolve_native_mapping_effort(monkeypatch):
    from anyuse.execution import learning
    monkeypatch.setenv("ANYUSE_REA_GUI_CAPTURE", "off")
    monkeypatch.setattr(learning, "resolve_mapping_effort", lambda: pytest.fail("off must stay independent of mapping"))
    assert learning.resolve_gui_capture_mode(escalate_to_rea=True) == "off"


def test_fallback_counts_persist_across_instances_and_use_storage_directory(tmp_path):
    import json
    storage = types.SimpleNamespace(base_dir=tmp_path / "workflows")
    for count in range(1, 4):
        lo = LearningOrchestrator(_FakeOrch(), storage=storage, auto_learn=False, auto_replay=False)
        lo.run("Open the panel")
        assert lo.last_rea_escalation["vision_fallbacks"] == count
        assert lo.last_rea_escalation["escalate_to_rea"] is (count == 3)
    saved = storage.base_dir / "vision_fallbacks.json"
    assert json.loads(saved.read_text())["counts"] == {"open_the_panel": 3}
    fresh = LearningOrchestrator(_FakeOrch(), storage=storage, auto_learn=False)
    assert fresh.list_rea_candidates()[0]["vision_fallbacks"] == 3
    other = LearningOrchestrator(_FakeOrch(), storage=types.SimpleNamespace(storage_dir=tmp_path / "other"))
    assert other.rea_escalation_status("Open the panel")["vision_fallbacks"] == 0


def test_default_fallback_store_is_shared_across_new_instances():
    first = LearningOrchestrator(_FakeOrch(), auto_learn=False)
    first.run("shared task")
    second = LearningOrchestrator(_FakeOrch(), auto_learn=False)
    second.run("shared task")
    assert second.last_rea_escalation["vision_fallbacks"] == 2


def test_disabled_tracking_never_increments_or_writes(tmp_path, monkeypatch):
    from anyuse.execution.learning import ReaFallbackTracker
    directory = tmp_path / "disabled"
    tracker = ReaFallbackTracker(storage_dir=directory)
    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "0")
    for _ in range(4):
        assert tracker.note_vision_fallback("panel")["vision_fallbacks"] == 0
    assert not directory.exists()
    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "3")
    assert tracker.note_vision_fallback("panel")["vision_fallbacks"] == 1
    snapshot = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "0")
    for _ in range(4):
        assert tracker.note_vision_fallback("panel")["vision_fallbacks"] == 1
    assert snapshot == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    monkeypatch.setenv("ANYUSE_REA_AUTO_THRESHOLD", "3")
    fresh = ReaFallbackTracker(storage_dir=directory)
    assert fresh.note_vision_fallback("panel")["escalate_to_rea"] is False
    assert fresh.note_vision_fallback("panel")["escalate_to_rea"] is True


def test_truncated_task_slug_is_idempotent_and_candidates_keep_counts():
    from anyuse.execution.learning import _slug
    task = "a" * 59 + " b"
    for text in (task, "!!!", "x " * 90, "a" * 100, "Some SHORT task"):
        assert _slug(_slug(text)) == _slug(text)
    lo = LearningOrchestrator(_FakeOrch(), auto_learn=False)
    for _ in range(3):
        lo.note_vision_fallback(task)
    assert lo.list_rea_candidates() == [lo.rea_escalation_status(task)]
    assert lo.list_rea_candidates()[0]["vision_fallbacks"] == 3


def test_atomic_fallback_updates_do_not_lose_concurrent_counts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from anyuse.execution.learning import ReaFallbackTracker
    def run_one(_):
        return ReaFallbackTracker(storage_dir=tmp_path).note_vision_fallback("shared panel")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(run_one, range(12)))
    assert sorted(row["vision_fallbacks"] for row in rows) == list(range(1, 13))
    assert ReaFallbackTracker(storage_dir=tmp_path).rea_escalation_status("shared panel")["vision_fallbacks"] == 12


def test_fallback_state_is_bounded_and_invalid_state_is_not_overwritten(tmp_path, monkeypatch):
    from anyuse.execution.learning import ReaFallbackTracker
    monkeypatch.setattr(ReaFallbackTracker, "MAX_TASKS", 2)
    tracker = ReaFallbackTracker(storage_dir=tmp_path)
    for task in ("old", "kept", "new"):
        tracker.note_vision_fallback(task)
    assert set(tracker.counts) == {"kept", "new"}
    tracker.path.write_text('{"counts": "broken"}')
    original = tracker.path.read_bytes()
    assert "tracking_error" in tracker.note_vision_fallback("new")
    assert tracker.path.read_bytes() == original


@pytest.mark.parametrize("rea_rows,history,expected", [
    ([], [], False),
    ([], [{"decision": {"action": []}}], False),
    ([], [{"decision": {"action": "click"}, "actuation": {"success": False, "error": "missing coordinates"}}], False),
    ([], [{"decision": {"action": "click"}, "actuation": {"success": True, "gated": True}}], False),
    ([], [{"decision": {"action": "click"}, "actuation": {"success": True, "blocked": True}}], False),
    ([{"ok": False}], [{"decision": {"action": "rea_action"}, "actuation": {"ok": False}}], False),
    ([{"ok": True}], [{"decision": {"action": "click"}, "actuation": {"success": True}}], False),
    ([{"ok": False}], [{"decision": {"action": "click"}, "actuation": {"success": True}}], True),
    ([], [{"decision": {"action": "click"}, "actuation": {"success": True}}], True),
])
def test_only_actual_computer_use_without_successful_rea_counts(rea_rows, history, expected):
    orch = _FakeOrch()
    orch.rea_executions = rea_rows
    orch.history = history
    lo = LearningOrchestrator(orch, auto_learn=False)
    lo.run("panel")
    assert (lo.last_rea_escalation is not None) is expected
    assert lo.rea_escalation_status("panel")["vision_fallbacks"] == int(expected)


@pytest.mark.parametrize("memory", [False, True])
def test_public_agent_result_reports_persistent_escalation(memory):
    from anyuse.core.agent import Agent
    from anyuse.core.result import ResultStatus

    def make_agent():
        agent = Agent.__new__(Agent)
        agent.enable_memory = memory
        agent.auto_learn = agent.auto_replay = memory
        agent.max_turns = 20
        agent._workflows = None
        agent._orchestrator = _FakeOrch()
        agent._orchestrator.history = [{"decision": {"action": "click"}, "actuation": {"success": True}}]
        agent._learner = LearningOrchestrator(agent._orchestrator, auto_learn=False, auto_replay=False)
        agent._decide_outcome = lambda *args: (ResultStatus.SUCCESS, "inert test outcome")
        return agent

    for count in range(1, 4):
        result = make_agent().run("Open the panel")
        assert result.success, result.error
        assert result.rea_escalation["vision_fallbacks"] == count
        assert result.to_dict()["rea_escalation"]["escalate_to_rea"] is (count == 3)
    agent = make_agent()
    assert agent.run("Open the panel").rea_escalation["escalate_to_rea"] is True
    if memory:
        agent._learner._try_replay = lambda task: "saved workflow succeeded"
    else:
        agent._orchestrator.history = []
    assert agent.run("a different task").rea_escalation is None


@pytest.mark.parametrize("serialized", [False, True])
def test_audit_terminal_replay_result_never_calls_teacher(serialized):
    from anyuse.core.result import AgentResult, ResultStatus
    result = AgentResult(status=ResultStatus.UNCERTAIN, success=False, message="not observed",
                         outcome_reason="stopped (uncertain_completion): not observed")
    orch = _FakeOrch()
    learner = LearningOrchestrator(orch, storage=_FakeStorage((_wf(), .95, "match")),
        auto_learn=False, replay_fn=lambda name: result.to_dict() if serialized else result)
    assert learner.run("task").startswith("stopped (uncertain_completion)")
    assert not orch.ran


def test_audit_replay_exception_after_effect_is_terminal():
    calls = []
    def replay(name):
        calls.append("submitted")
        raise OSError("receipt write failed")
    orch = _FakeOrch()
    learner = LearningOrchestrator(orch, storage=_FakeStorage((_wf(), .95, "match")),
                                   auto_learn=False, replay_fn=replay)
    assert learner.run("submit once").startswith("stopped (incomplete)")
    assert calls == ["submitted"] and not orch.ran


def _audit_player(monkeypatch, tmp_path, outcome, *, optional=False, agent=None):
    import sys
    from anyuse.memory.models import Workflow, WorkflowStep
    from anyuse.memory.player import WorkflowPlayer
    matcher = types.ModuleType("anyuse.memory.matcher")
    matcher.TemplateMatcher = lambda *a: object()
    monkeypatch.setitem(sys.modules, "anyuse.memory.matcher", matcher)
    calls = []
    class Executor:
        def execute(self, action, args):
            calls.append(action)
            return outcome
    storage = types.SimpleNamespace(get_workflow_dir=lambda _: tmp_path, update_stats=lambda *a: None)
    wf = Workflow(id="fixture", name="fixture", steps=[
        WorkflowStep(action="fixture_submit", args={}, optional=optional),
        WorkflowStep(action="fixture_second", args={})])
    return WorkflowPlayer(Executor(), storage=storage, agent=agent), wf, calls


@pytest.mark.parametrize("kind", ["terminal_refusal", "refused", "uncertain_completion", "deviation", "incomplete"])
@pytest.mark.parametrize("optional", [False, True])
def test_audit_legacy_player_terminal_steps_stop_even_when_optional(monkeypatch, tmp_path, kind, optional):
    child = types.SimpleNamespace(run=lambda *a, **k: pytest.fail("terminal step called fallback"))
    player, wf, calls = _audit_player(monkeypatch, tmp_path,
        {"success": False, "outcome_kind": kind, "error": "stop"}, optional=optional, agent=child)
    out = player.play(wf, prepare_window=False)
    assert not out.success and out.outcome_reason.startswith("stopped (" + kind + ")")
    assert calls == ["fixture_submit"]


def test_audit_legacy_fallback_retains_child_result_metadata(monkeypatch, tmp_path):
    from anyuse.core.result import AgentResult, ResultStatus, RestoreReport
    child = AgentResult(status=ResultStatus.UNCERTAIN, success=False, message="unknown", llm_calls=2,
        outcome_reason="stopped (uncertain_completion): unknown", restoration_report=RestoreReport.unknown("inert"),
        rea_escalation={"escalate_to_rea": True}, rea_executions=[{"ok": False}])
    player, wf, _ = _audit_player(monkeypatch, tmp_path, {"success": False},
                                 agent=types.SimpleNamespace(run=lambda *a, **k: child))
    out = player.play(wf, prepare_window=False)
    assert out.outcome_reason == child.outcome_reason and out.llm_calls == 2
    assert out.restoration_report == child.restoration_report
    assert out.rea_executions == child.rea_executions and out.rea_escalation == child.rea_escalation


def test_audit_fallback_typeerror_does_not_reinvoke_agent(monkeypatch, tmp_path):
    calls = []
    def child(goal, **kwargs):
        calls.append(kwargs)
        raise TypeError("failed after effect")
    player, wf, _ = _audit_player(monkeypatch, tmp_path, {"success": False},
                                 agent=types.SimpleNamespace(run=child))
    with pytest.raises(TypeError, match="failed after effect"):
        player.play(wf, prepare_window=False)
    assert calls == [{"replay": False, "learn": False}]


def test_audit_workflow_name_is_unique_and_old_duplicates_are_forgotten(tmp_path):
    import json
    from anyuse.memory.models import Workflow
    from anyuse.memory.storage import WorkflowStorage
    store = WorkflowStorage(tmp_path)
    store.save(Workflow(id="old", name="same"))
    with pytest.raises(ValueError, match="already exists"):
        store.save(Workflow(id="new", name="same"))
    # An entry left by an old release must also disappear on explicit deletion.
    old = tmp_path / "workflow_historical"
    old.mkdir()
    (old / "workflow.json").write_text(json.dumps(Workflow(id="historical", name="same").to_dict()))
    assert store.delete("same")
    assert WorkflowStorage(tmp_path).get("same") is None


def test_audit_workflow_export_preserves_template_and_refuses_compiled_import(tmp_path):
    import json
    from anyuse.memory.models import Workflow, WorkflowStep, WorkflowTemplate, WorkflowIntent
    from anyuse.memory.storage import WorkflowStorage
    source, target = WorkflowStorage(tmp_path / "source"), WorkflowStorage(tmp_path / "target")
    wf = Workflow(id="owner", name="recorded", steps=[WorkflowStep(action="key", args={"keys": "a"})])
    tpl = WorkflowTemplate(id="independent", name=wf.name, steps=[WorkflowIntent(intent="type", target_element="field")],
                           approval={"level": "all"})
    source.save(wf); source.save_template(wf.id, tpl)
    export = tmp_path / "export.json"
    source.export(wf.name, str(export))
    imported = target.import_from(str(export))
    assert imported is not None and not imported.verified
    assert target.get_template(imported.id).approval == {"level": "all"}
    assert target.get_template(imported.id).steps[0].intent == "type"
    compiled = Workflow(id="compiled", name="compiled", tags=["from-draft"], verified=True)
    source.save(compiled)
    with pytest.raises(ValueError, match="proof package"):
        source.export(compiled.name, str(tmp_path / "compiled-export.json"))
    export.write_text(json.dumps(compiled.to_dict()))
    assert target.import_from(str(export)) is None and target.get("compiled") is None
    # Historical legacy workflows with real steps still import, without old proof claims.
    export.write_text(json.dumps(dict(wf.to_dict(), name="legacy", verified=True)))
    assert target.import_from(str(export)).verified is False


def test_audit_healing_persists_to_owner_id_and_reports_write_failure(tmp_path):
    from anyuse.memory.models import Workflow, WorkflowTemplate
    from anyuse.memory.storage import WorkflowStorage
    from anyuse.execution.self_healing import SelfHealingManager
    from anyuse.guides.state_graph import StateGraph
    store = WorkflowStorage(tmp_path)
    wf, tpl = Workflow(id="owner", name="task"), WorkflowTemplate(id="template", name="task")
    store.save(wf); store.save_template(wf.id, tpl)
    tpl.version += 1
    SelfHealingManager(workflow_storage=store)._persist_changes(tpl, StateGraph(), None, [])
    assert store.get_template(wf.id).version == tpl.version
    assert not (tmp_path / "workflow_template").exists()
    class BrokenStore:
        def save_template(self, *a): raise OSError("disk full")
        def save_trace(self, *a): pass
    result = types.SimpleNamespace(success=True, steps=[], states_discovered=[], transitions_discovered=0,
                                   duration_seconds=0, llm_calls=0)
    report = SelfHealingManager(workflow_storage=BrokenStore()).process_execution_result(result, tpl, StateGraph())
    assert not report.changes_persisted


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
