"""anyuse execution.drift_gate unit test (the visual self-healing loop).

The token-saver loop under test: during deterministic replay, compare the LIVE
AX elements to the RECORDED baseline fingerprint and fall back to vision ONLY on
a real drift. The pure drift_gate.evaluate_drift function is tested against
hand-built in-memory StateGraph/UIState/UIFingerprint fixtures (cases a-d), and
the executor-level wiring is tested with a STUBBED ax_tree (canned AXSnapshots)
and a SPY action_executor (case e).

SAFETY-CRITICAL invariants under test:
  - The drift gate is OFF by default (drift_gate_enabled=False) so existing
    callers are byte-for-byte unchanged.
  - The gate is PURE: NO real screencapture, NO pyautogui, NO osascript, NO
    actuation. The escalated 'computer_use' step, with ANYUSE_DRIVE_ENABLED
    unset (default OFF), returns the non-actuating needs_orchestrator stub — the
    spy action_executor is asserted NEVER called.
  - A self-healing placeholder fingerprint (empty UIFingerprint, marked
    'fingerprint_pending') yields reason='no_recorded_fingerprint' (safe path),
    NOT a bogus 0.0-similarity 'drift'.

Run from the anyuse package dir:
    cd anyuse && /usr/local/bin/python3.13 test_execution_drift_gate.py
"""

import asyncio
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))            # flat: import config
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution import drift_gate                       # noqa: E402
from anyuse.execution.drift_gate import (                     # noqa: E402
    evaluate_drift,
    DriftReport,
    FINGERPRINT_PENDING_NOTE,
    _classify,
)
from anyuse.execution.executor import StateGraphExecutor      # noqa: E402
from anyuse.execution.planner import ExecutionPlan, PlannedStep  # noqa: E402
from anyuse.guides.state_graph import (                       # noqa: E402
    StateGraph,
    UIState,
    UIFingerprint,
    compute_structural_hash,
)
from anyuse.memory.models import WorkflowTemplate, WorkflowIntent  # noqa: E402
from anyuse.context.accessibility import AXSnapshot, AXElementDesc   # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fixtures — hand-built recorded baseline state (no live AX, no OS layer)
# ---------------------------------------------------------------------------

# Recorded baseline: a login page with two required anchors.
_BASELINE_ELEMENTS = [
    {"role": "AXTextField", "title": "Email"},
    {"role": "AXTextField", "title": "Password"},
    {"role": "AXButton", "title": "Sign In"},
    {"role": "AXStaticText", "title": "Welcome"},
]


def _baseline_fingerprint() -> UIFingerprint:
    count = len(_BASELINE_ELEMENTS)
    return UIFingerprint(
        required_elements=[
            {"role": "AXTextField", "title": "Email"},
            {"role": "AXButton", "title": "Sign In"},
        ],
        element_count_range=(max(0, count - 1), count + 1),
        structural_hash=compute_structural_hash(_BASELINE_ELEMENTS),
    )


def _graph_with_baseline() -> StateGraph:
    sg = StateGraph()
    sg.add_state(
        UIState(
            state_id="login_page",
            app="Google Chrome",
            fingerprint=_baseline_fingerprint(),
            times_seen=5,
        )
    )
    return sg


def _graph_with_placeholder() -> StateGraph:
    """A state discovered by self-healing: empty UIFingerprint, marked pending."""
    sg = StateGraph()
    sg.add_state(
        UIState(
            state_id="discovered_state",
            app="",
            fingerprint=UIFingerprint(),  # placeholder
            times_seen=1,
            custom_render_notes=[FINGERPRINT_PENDING_NOTE],
        )
    )
    return sg


# ---------------------------------------------------------------------------
# (a) live == recorded  ->  NO drift (deterministic replay, no vision)
# ---------------------------------------------------------------------------

def test_no_drift_when_live_matches_recorded():
    sg = _graph_with_baseline()
    # Live AX exactly mirrors the recorded baseline (same required + same hash).
    live = list(_BASELINE_ELEMENTS)

    report = evaluate_drift(sg, "login_page", live)

    assert isinstance(report, DriftReport), report
    assert report.drifted is False, report.to_dict()
    assert report.reason == "", report.to_dict()
    assert report.matched_state_id == "login_page", report.to_dict()
    # Raw signals are preserved for a possible LLM override.
    assert report.signals.get("required_missing") == [], report.signals
    assert report.signals.get("count_in_range") is True, report.signals
    assert report.signals.get("structural_similarity") == 1.0, report.signals
    print("OK  (a) live == recorded -> drifted=False (deterministic replay, NO vision)")


# ---------------------------------------------------------------------------
# (b) a required anchor is missing  ->  drift, reason=required_missing
# ---------------------------------------------------------------------------

def test_drift_when_required_element_missing():
    sg = _graph_with_baseline()
    # 'Sign In' button has been removed/renamed in the live UI.
    live = [
        {"role": "AXTextField", "title": "Email"},
        {"role": "AXTextField", "title": "Password"},
        {"role": "AXStaticText", "title": "Welcome"},
    ]

    report = evaluate_drift(sg, "login_page", live)

    assert report.drifted is True, report.to_dict()
    assert report.reason == "required_missing", report.to_dict()
    missing = report.signals.get("required_missing") or []
    assert any(m.get("title") == "Sign In" for m in missing), report.signals
    print("OK  (b) required anchor missing -> drifted=True reason='required_missing'")


# ---------------------------------------------------------------------------
# (c) element count far outside recorded range  ->  count_out_of_range
# ---------------------------------------------------------------------------

def test_drift_when_count_out_of_range():
    sg = _graph_with_baseline()
    # All required anchors are present, but the page exploded to ~30 elements —
    # far outside the recorded (3, 5) count range. required_missing is empty so
    # the count signal is what fires.
    live = [
        {"role": "AXTextField", "title": "Email"},
        {"role": "AXButton", "title": "Sign In"},
    ]
    live += [{"role": "AXStaticText", "title": f"row {i}"} for i in range(30)]

    report = evaluate_drift(sg, "login_page", live)

    assert report.drifted is True, report.to_dict()
    assert report.reason == "count_out_of_range", report.to_dict()
    assert report.signals.get("count_in_range") is False, report.signals
    # Sanity: the required anchors WERE found — this is not a required_missing case.
    assert (report.signals.get("required_missing") or []) == [], report.signals
    print("OK  (c) count far outside range -> drifted=True reason='count_out_of_range'")


def test_missing_anchor_tolerance_does_not_bypass_count_or_structure_checks():
    base = {
        "required_missing": [{"role": "AXStaticText", "title": "minor"}],
        "required_found": [{"role": "AXStaticText", "title": str(i)}
                           for i in range(99)],
        "count_in_range": False,
        "structural_similarity": 1.0,
    }
    assert _classify(base, missing_tolerance=0.02) == (
        True, "count_out_of_range")
    structural = {**base, "count_in_range": True, "structural_similarity": 0.1}
    assert _classify(structural, missing_tolerance=0.02) == (
        True, "low_structural_similarity")


# ---------------------------------------------------------------------------
# (d) placeholder fingerprint  ->  no_recorded_fingerprint (safe path, not 0.0)
# ---------------------------------------------------------------------------

def test_placeholder_fingerprint_is_no_recorded_fingerprint():
    sg = _graph_with_placeholder()
    live = list(_BASELINE_ELEMENTS)

    report = evaluate_drift(sg, "discovered_state", live)

    # The empty self-healing placeholder must NOT masquerade as genuine drift
    # (would be a bogus 0.0 structural similarity). It is explicitly handled.
    assert report.drifted is True, report.to_dict()
    assert report.reason == "no_recorded_fingerprint", report.to_dict()
    assert report.matched_state_id == "discovered_state", report.to_dict()
    assert report.signals.get("placeholder") is True, report.signals
    # Crucially, it did NOT report a low_structural_similarity / 0.0 drift.
    assert "structural_similarity" not in report.signals, report.signals
    print("OK  (d) placeholder fingerprint -> drifted=True reason='no_recorded_fingerprint'")


def test_unknown_expected_state_falls_back_to_identify_state():
    """Bonus: unknown expected_state_id -> identify_state over all states picks
    the baseline and reports no drift when the live UI matches it."""
    sg = _graph_with_baseline()
    live = list(_BASELINE_ELEMENTS)

    report = evaluate_drift(sg, None, live)

    assert report.drifted is False, report.to_dict()
    assert report.matched_state_id == "login_page", report.to_dict()
    assert "all_matches" in report.signals, report.signals
    print("OK  (+) no expected_state -> identify_state picks login_page, no drift")


# ---------------------------------------------------------------------------
# (e) executor-level: stubbed OS layer, spy actions — escalate ONLY on drift,
#     never actuate, gate OFF by default.
# ---------------------------------------------------------------------------

class _StubAXTree:
    """Returns a canned AXSnapshot for every snapshot(); records snapshot calls.

    Provides only the surface StateGraphExecutor touches. diff() returns an
    empty-ish AXDiff stand-in. NEVER touches a real AX API or screen.
    """

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return self._snapshot

    def diff(self, before, after):
        from anyuse.context.accessibility import AXDiff
        return AXDiff()  # no changes — keeps verification on the "no changes" path


class _SpyActionExecutor:
    """Records every .execute() call; returns success WITHOUT touching pyautogui."""

    def __init__(self):
        self.calls = []

    def execute(self, action, params):
        self.calls.append((action, params))
        return {"success": True, "method": "coordinate"}


def _ax_snapshot(element_dicts):
    return AXSnapshot(
        elements=[
            AXElementDesc(role=e.get("role"), title=e.get("title"))
            for e in element_dicts
        ]
    )


def _workflow_with_baseline_step():
    return WorkflowTemplate(
        id="wf-test",
        name="login_replay",
        required_app="Google Chrome",
        steps=[
            WorkflowIntent(
                intent="click Sign In",
                target_element="Sign In",
                expected_before_state="login_page",
                expected_after_state="dashboard",
                input_type="click",
            )
        ],
    )


def _deterministic_step():
    return PlannedStep(
        intent="click Sign In",
        source="workflow",
        target_element="Sign In",
        modality="coordinate",
        command={"x": 100, "y": 200, "action": "click"},
        expected_after="signed in",
        expected_after_state="dashboard",
    )


def test_executor_gate_off_by_default_preserves_behavior():
    """drift_gate_enabled defaults to False — existing behavior is unchanged."""
    ex = StateGraphExecutor()
    assert ex._drift_gate_enabled is False, "drift gate must default OFF"
    print("OK  (e0) drift_gate_enabled defaults to False (existing behavior preserved)")


def test_executor_no_drift_keeps_deterministic_modality():
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)  # gate OFF (default)

    sg = _graph_with_baseline()
    # Live AX matches the recorded baseline -> no drift -> stay deterministic.
    stub_ax = _StubAXTree(_ax_snapshot(_BASELINE_ELEMENTS))
    spy_actions = _SpyActionExecutor()
    ex = StateGraphExecutor(
        action_executor=spy_actions,
        ax_tree=stub_ax,
        drift_gate_enabled=True,
    )

    plan = ExecutionPlan(current_state="login_page", steps=[_deterministic_step()])
    result = _run(ex.execute(plan, _workflow_with_baseline_step(), sg))

    step = result.steps[0]
    # Stayed on the cheap coordinate modality — vision was NEVER invoked.
    assert step.method == "coordinate", step.to_dict()
    assert "drift detected" not in step.reasoning, step.to_dict()
    # The deterministic coordinate path actuated through the spy (one click_at).
    assert len(spy_actions.calls) == 1, spy_actions.calls
    assert spy_actions.calls[0][0] == "click_at", spy_actions.calls
    print("OK  (e1) no drift -> step keeps planned 'coordinate' modality (no vision)")


def test_executor_drift_escalates_to_vision_without_actuating():
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)  # gate OFF (default)

    sg = _graph_with_baseline()
    # Live AX is missing the 'Sign In' anchor -> real drift -> escalate to vision.
    drifted_live = [
        {"role": "AXTextField", "title": "Email"},
        {"role": "AXTextField", "title": "Password"},
        {"role": "AXStaticText", "title": "Welcome"},
    ]
    stub_ax = _StubAXTree(_ax_snapshot(drifted_live))
    spy_actions = _SpyActionExecutor()
    ex = StateGraphExecutor(
        action_executor=spy_actions,
        ax_tree=stub_ax,
        drift_gate_enabled=True,
    )

    plan = ExecutionPlan(current_state="login_page", steps=[_deterministic_step()])
    result = _run(ex.execute(plan, _workflow_with_baseline_step(), sg))

    step = result.steps[0]
    # Escalated to vision, annotated with the drift reason.
    assert step.method == "computer_use", step.to_dict()
    assert "drift detected: required_missing -> vision fallback" in step.reasoning, step.to_dict()

    # KEYSTONE SAFETY: ANYUSE_DRIVE_ENABLED off -> _execute_visual returns the
    # non-actuating needs_orchestrator stub. The spy action_executor is NEVER
    # called (no click_at, no type, nothing). No real screencapture/actuation.
    assert spy_actions.calls == [], spy_actions.calls
    assert step.success is False, step.to_dict()
    print("OK  (e2) drift -> escalated to 'computer_use', gate OFF, ZERO actuation")


# ---------------------------------------------------------------------------
# execute_checked_step's own drift-gate wiring: the gate also covers the
# CHECKED replay path deterministic.py's replay_transitions goes
# through, without changing behavior for a caller that has no workflow/
# state_graph/step_idx to give it (deterministic.py itself, every existing
# test, unchanged).
# ---------------------------------------------------------------------------

def test_checked_step_without_context_never_evaluates_the_gate():
    """No step_idx/workflow/state_graph -> byte-for-byte prior behavior, even
    with drift_gate_enabled=True: there is nothing to compare the live AX
    against, so the gate must not guess a baseline."""
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
    stub_ax = _StubAXTree(_ax_snapshot(_BASELINE_ELEMENTS))
    spy_actions = _SpyActionExecutor()
    ex = StateGraphExecutor(action_executor=spy_actions, ax_tree=stub_ax, drift_gate_enabled=True)

    exec_result, verification = _run(ex.execute_checked_step(_deterministic_step()))

    assert exec_result["method"] == "coordinate", exec_result
    assert "drift_gate" not in verification, verification
    print("OK  (e3) execute_checked_step with no workflow context never evaluates the gate")


def test_checked_step_context_given_but_gate_disabled_does_not_evaluate():
    """step_idx/workflow/state_graph given, but drift_gate_enabled defaults
    False -> still opt-in, still no evaluation (the token-saver default)."""
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
    sg = _graph_with_baseline()
    stub_ax = _StubAXTree(_ax_snapshot(_BASELINE_ELEMENTS))
    ex = StateGraphExecutor(ax_tree=stub_ax)  # drift_gate_enabled defaults False
    assert ex._drift_gate_enabled is False

    exec_result, verification = _run(ex.execute_checked_step(
        _deterministic_step(), step_idx=0, workflow=_workflow_with_baseline_step(),
        state_graph=sg,
    ))

    assert "drift_gate" not in verification, verification
    print("OK  (e4) context given but drift_gate_enabled=False -> still no evaluation")


def test_checked_step_no_drift_reports_the_gate_ran_and_did_not_fire():
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
    sg = _graph_with_baseline()
    stub_ax = _StubAXTree(_ax_snapshot(_BASELINE_ELEMENTS))  # matches the baseline
    spy_actions = _SpyActionExecutor()
    ex = StateGraphExecutor(action_executor=spy_actions, ax_tree=stub_ax, drift_gate_enabled=True)

    exec_result, verification = _run(ex.execute_checked_step(
        _deterministic_step(), step_idx=0, workflow=_workflow_with_baseline_step(),
        state_graph=sg,
    ))

    assert exec_result["method"] == "coordinate", exec_result  # stayed deterministic
    assert verification["drift_gate"] == {"fired": False, "note": None}, verification
    assert len(spy_actions.calls) == 1, spy_actions.calls
    print("OK  (e5) checked path, no drift -> gate ran, did not fire, stayed deterministic")


def test_checked_step_drift_escalates_and_reports_why():
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
    sg = _graph_with_baseline()
    drifted_live = [
        {"role": "AXTextField", "title": "Email"},
        {"role": "AXTextField", "title": "Password"},
        {"role": "AXStaticText", "title": "Welcome"},
    ]
    stub_ax = _StubAXTree(_ax_snapshot(drifted_live))
    spy_actions = _SpyActionExecutor()
    ex = StateGraphExecutor(action_executor=spy_actions, ax_tree=stub_ax, drift_gate_enabled=True)

    exec_result, verification = _run(ex.execute_checked_step(
        _deterministic_step(), step_idx=0, workflow=_workflow_with_baseline_step(),
        state_graph=sg,
    ))

    assert exec_result["method"] == "computer_use", exec_result
    assert verification["drift_gate"]["fired"] is True, verification
    assert "required_missing" in verification["drift_gate"]["note"], verification
    # KEYSTONE SAFETY: gate OFF (ANYUSE_DRIVE_ENABLED unset) -> the escalated
    # computer_use step is the non-actuating stub; the spy is NEVER called.
    assert spy_actions.calls == [], spy_actions.calls
    print("OK  (e6) checked path, real drift -> escalated to computer_use, reason reported, zero actuation")


class _SettlingAXTree(_StubAXTree):
    """The first ``drifted_reads`` snapshots show the screen before a new window exists."""

    def __init__(self, drifted, settled, drifted_reads):
        super().__init__(settled)
        self._drifted, self._left = drifted, drifted_reads

    def snapshot(self):
        self.snapshot_calls += 1
        if self._left > 0:
            self._left -= 1
            return self._drifted
        return self._snapshot


def test_checked_step_waits_for_a_window_to_open_before_calling_it_drift():
    import anyuse.execution.executor as EX
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
    old = (EX.DRIFT_SETTLE_S, EX.DRIFT_SETTLE_POLL_S)
    EX.DRIFT_SETTLE_S, EX.DRIFT_SETTLE_POLL_S = 0.5, 0.01
    try:
        sg = _graph_with_baseline()
        menus_only = _ax_snapshot([{"role": "AXMenuBarItem", "title": "Ablage"}])
        stub_ax = _SettlingAXTree(menus_only, _ax_snapshot(_BASELINE_ELEMENTS), drifted_reads=2)
        spy_actions = _SpyActionExecutor()
        ex = StateGraphExecutor(action_executor=spy_actions, ax_tree=stub_ax, drift_gate_enabled=True)
        exec_result, verification = _run(ex.execute_checked_step(
            _deterministic_step(), step_idx=0, workflow=_workflow_with_baseline_step(),
            state_graph=sg, on_drift="stop"))
        assert verification["drift_gate"] == {"fired": False, "note": None}, verification
        assert exec_result["method"] == "coordinate" and len(spy_actions.calls) == 1, exec_result
        # a screen that never settles still stops, with the reason
        stub_ax = _StubAXTree(menus_only)
        spy_actions = _SpyActionExecutor()
        ex = StateGraphExecutor(action_executor=spy_actions, ax_tree=stub_ax, drift_gate_enabled=True)
        exec_result, verification = _run(ex.execute_checked_step(
            _deterministic_step(), step_idx=0, workflow=_workflow_with_baseline_step(),
            state_graph=sg, on_drift="stop"))
        assert verification["drift_gate"]["fired"] is True and spy_actions.calls == [], verification
        assert "required_missing" in verification["drift_gate"]["note"]
        assert stub_ax.snapshot_calls > 2                      # it re-read before giving up
    finally:
        EX.DRIFT_SETTLE_S, EX.DRIFT_SETTLE_POLL_S = old
    print("OK  (e7) a window still opening is waited for; a screen that stays drifted still stops")


def main():
    print("=" * 60)
    print("anyuse Wave-6 — drift gate (deterministic replay vs vision-on-drift)")
    print("=" * 60)
    # Pure evaluate_drift cases
    test_no_drift_when_live_matches_recorded()
    test_drift_when_required_element_missing()
    test_drift_when_count_out_of_range()
    test_placeholder_fingerprint_is_no_recorded_fingerprint()
    test_unknown_expected_state_falls_back_to_identify_state()
    # Executor-level wiring (stubbed OS layer, spy actions)
    test_executor_gate_off_by_default_preserves_behavior()
    test_executor_no_drift_keeps_deterministic_modality()
    test_executor_drift_escalates_to_vision_without_actuating()
    # execute_checked_step's own drift-gate wiring
    test_checked_step_without_context_never_evaluates_the_gate()
    test_checked_step_context_given_but_gate_disabled_does_not_evaluate()
    test_checked_step_no_drift_reports_the_gate_ran_and_did_not_fire()
    test_checked_step_drift_escalates_and_reports_why()
    test_checked_step_waits_for_a_window_to_open_before_calling_it_drift()
    print("\nANYUSE DRIFT GATE TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
