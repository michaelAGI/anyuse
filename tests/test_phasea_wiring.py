"""Phase-A wiring: deterministic-replay-first in the orchestrator (de-islands
replay_path) + selector-on-replay in _execute_ax (uses the durable Selector)."""
import asyncio
import types

import config
from anyuse.execution.executor import StateGraphExecutor
from anyuse.execution.planner import PlannedStep
from anyuse.guides.state_graph import StateGraph, TransitionImpl, UIState, UITransition


# ── selector-on-replay (#8) ───────────────────────────────────────────────────
class _FakeAXTree:
    def __init__(self, selector_hit=True):
        self.calls = []
        self._selector_hit = selector_hit
        self._last_sel = None

    def find_selector(self, sel):
        self.calls.append(("find_selector", sel.canonical_role))
        self._last_sel = sel
        return "EL" if self._selector_hit else None

    def to_selector(self, el):
        # The identity check (executor._same_identity) compares the
        # resolved candidate's OWN selector against the recorded one before
        # accepting it. A "hit" fake stands in for a resolution that really is
        # the recorded element, so it echoes the very selector it was asked to
        # find — trivially identical, exactly like the real AccessibilityTree
        # would be for a correct resolution.
        return self._last_sel

    def find(self, role=None, title=None):
        self.calls.append(("find", role, title))
        return ["EL2"]

    def perform_action(self, el, action):
        self.calls.append(("perform", el, action)); return True

    def set_value(self, el, value):
        self.calls.append(("set_value", el, value)); return True


def _ax_step(command):
    return PlannedStep(intent="i", source="workflow", target_element="", modality="ax", command=command)


def test_execute_ax_uses_selector_when_present():
    ax = _FakeAXTree(selector_hit=True)
    ex = StateGraphExecutor(ax_tree=ax)
    step = _ax_step({"selector": {"canonical_role": "button", "name": "Save", "raw_role": "AXButton"},
                     "role": "AXButton", "title": "Save", "action": "press"})
    res = asyncio.run(ex._execute_step(step))
    assert res["success"] is True
    assert ("find_selector", "button") in ax.calls
    assert ("perform", "EL", "AXPress") in ax.calls
    assert not any(c[0] == "find" for c in ax.calls), "selector hit must not fall back to title-find"


def test_execute_ax_falls_back_to_find_without_selector():
    ax = _FakeAXTree()
    ex = StateGraphExecutor(ax_tree=ax)
    res = asyncio.run(ex._execute_step(_ax_step({"role": "AXButton", "title": "Save", "action": "press"})))
    assert res["success"] is True
    assert any(c[0] == "find" for c in ax.calls)
    assert ("perform", "EL2", "AXPress") in ax.calls


def test_execute_ax_toggle_maps_to_axpress():
    ax = _FakeAXTree(selector_hit=False)
    ex = StateGraphExecutor(ax_tree=ax)
    res = asyncio.run(ex._execute_step(_ax_step({"role": "AXCheckBox", "title": "X", "action": "toggle"})))
    assert res["success"] is True
    assert ("perform", "EL2", "AXPress") in ax.calls  # toggle → AXPress (was unmapped before)


def test_execute_ax_waits_and_retries_for_late_element():
    # wait-for-element: the element appears only on the 2nd lookup; the retry loop
    # must keep polling instead of failing on the first miss (the #1 fragility).
    import os
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"

    class _LateAX:
        def __init__(self):
            self.find_calls = 0

        def find_selector(self, sel):
            return None

        def find(self, role=None, title=None):
            self.find_calls += 1
            return ["EL"] if self.find_calls >= 2 else []  # shows up on the 2nd poll

        def perform_action(self, el, action):
            return True

    ax = _LateAX()
    ex = StateGraphExecutor(ax_tree=ax)
    res = asyncio.run(ex._execute_step(_ax_step({"role": "AXButton", "title": "Save", "action": "press"})))
    assert res["success"] is True and ax.find_calls >= 2


def test_agent_orchestrator_accepts_target_app_and_focus_is_noop_without_target():
    from anyuse.execution.agent_orchestrator import AgentOrchestrator
    o = AgentOrchestrator(api_key="x", target_app="Finder", pixel_scale=1.0)
    assert o._target_app == "Finder"
    AgentOrchestrator(api_key="x", pixel_scale=1.0)._ensure_focus(None)  # no target → no-op, no raise


# ── deterministic-replay-first wiring ────────────────────────────────────────
def _orch():
    old = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = ""  # offline: skip planner/intervention
    try:
        from anyuse.execution.orchestrator import ExecutionOrchestrator
        return ExecutionOrchestrator(api_key="", action_executor=None)
    finally:
        config.GEMINI_API_KEY = old


class _FakeExec:
    def __init__(self):
        self.dispatched = []

    async def _execute_step(self, step):
        self.dispatched.append((step.modality, dict(step.command))); return {"success": True}


def test_replay_deterministic_happy_path():
    sg = StateGraph()
    # find_path requires the states to exist (the recorder adds both in production).
    sg.add_state(UIState(state_id="s1", app="TestApp"))
    sg.add_state(UIState(state_id="s2", app="TestApp"))
    sg.add_transition(UITransition(from_state="s1", to_state="s2", intent="go",
        implementations=[TransitionImpl(modality="coordinate", command={"absolute_x": 5, "absolute_y": 6, "action": "click_at"})]))
    wf = types.SimpleNamespace(state_path=["s1", "s2"], name="t")
    fe = _FakeExec()
    res = asyncio.run(_orch().replay_deterministic(wf, state_graph=sg, executor=fe))
    assert res["success"] is True and res["llm_calls"] == 0, res
    mod, cmd = fe.dispatched[0]
    assert mod == "coordinate" and cmd["x"] == 5 and cmd["y"] == 6 and cmd["action"] == "click", cmd


def test_replay_deterministic_no_recorded_path():
    wf = types.SimpleNamespace(state_path=["s1"], name="t")
    res = asyncio.run(_orch().replay_deterministic(wf, state_graph=StateGraph(), executor=_FakeExec()))
    assert res["success"] is False and res["reason"] == "no_recorded_path"


# ── gated platform routing for AX actuation (closes the gating gap) ──────────
class _FakePlatform:
    def __init__(self):
        self.calls = []

    def perform_verb(self, el, verb, value=None):
        self.calls.append((verb, value)); return True


def test_execute_ax_routes_through_platform_and_is_gated():
    import os
    ax = _FakeAXTree(selector_hit=True)
    plat = _FakePlatform()
    ex = StateGraphExecutor(ax_tree=ax, platform=plat)
    step = _ax_step({"selector": {"canonical_role": "button", "name": "Save"},
                     "role": "AXButton", "title": "Save", "action": "press"})
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""  # gate OFF → no actuation
    r = asyncio.run(ex._execute_step(step))
    assert r.get("gated") is True and plat.calls == []
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"  # gate ON → perform_verb (press→invoke)
    r = asyncio.run(ex._execute_step(step))
    assert r["success"] is True and plat.calls == [("invoke", None)]


def test_execute_ax_platform_set_value_passes_text():
    import os
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    ax = _FakeAXTree(selector_hit=True)
    plat = _FakePlatform()
    ex = StateGraphExecutor(ax_tree=ax, platform=plat)
    step = PlannedStep(intent="i", source="workflow", target_element="", modality="ax",
                       command={"role": "AXTextField", "title": "Name", "action": "set_value"},
                       input_data="hello")
    asyncio.run(ex._execute_step(step))
    assert plat.calls == [("set_value", "hello")]


# ── no-LLM OCR verify tier in _verify_step ───────────────────────────────────
class _FakeDiff:
    added: list = []
    removed: list = []
    changed: list = []

    def to_summary(self):
        return "no changes"

    def clearly_matches(self, expected):
        return False  # AX inconclusive → the visual tier should fire


class _FakeAXForVerify:
    def diff(self, a, b):
        return _FakeDiff()


class _FakeVerifier:
    def __init__(self, passed):
        self._p = passed

    def verify(self, **kw):
        from anyuse.execution.visual_verify import VerifyResult
        return VerifyResult(self._p, 0.9 if self._p else 0.0, "ocr", "x")


class _Shot:
    def capture(self):
        return b"PNG"


def _run_verify(passed):
    from anyuse.context.accessibility import AXSnapshot
    ex = StateGraphExecutor(ax_tree=_FakeAXForVerify(), visual_verifier=_FakeVerifier(passed), screenshot_capture=_Shot())
    # literal expected_text in the command (NOT the NL expected_after) — the only
    # thing the visual tier matches on, to avoid NL substring false positives.
    step = PlannedStep(intent="i", source="workflow", target_element="", modality="ax",
                       command={"expected_text": "Saved"}, expected_after="document is saved")
    return ex._verify_step(step, AXSnapshot(elements=[]), AXSnapshot(elements=[]))


def test_verify_step_visual_tier_promotes_on_ocr_match():
    v = _run_verify(passed=True)
    assert v["clearly_matched"] is True and v.get("visual_matched") is True and v["visual_tier"] == "ocr"


def test_verify_step_visual_tier_no_promote_on_ocr_miss():
    v = _run_verify(passed=False)
    assert v["clearly_matched"] is False and v["visual_tier"] == "ocr"


def test_verify_step_visual_tier_dormant_without_literal_expected_text():
    # No command["expected_text"] → the visual tier must NOT fire on the NL
    # expected_after (a false positive).
    from anyuse.context.accessibility import AXSnapshot
    ex = StateGraphExecutor(ax_tree=_FakeAXForVerify(), visual_verifier=_FakeVerifier(True), screenshot_capture=_Shot())
    step = PlannedStep(intent="i", source="workflow", target_element="", modality="ax",
                       command={}, expected_after="Settings panel opens")
    v = ex._verify_step(step, AXSnapshot(elements=[]), AXSnapshot(elements=[]))
    assert v["clearly_matched"] is False and "visual_tier" not in v


# ── the gating gap is fully closed: js / applescript / AX primitive all gated ─
def test_execute_js_gated_when_drive_off():
    import os
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""

    class _FakeCtx:
        def execute_javascript_in_browser(self, s, browser=None):
            return "ran"  # must NOT be reached when gated

    ex = StateGraphExecutor(macos_context=_FakeCtx())
    step = PlannedStep(intent="i", source="workflow", target_element="", modality="js", command={"script": "x()"})
    r = asyncio.run(ex._execute_step(step))
    assert r.get("gated") is True


def test_execute_applescript_gated_when_drive_off():
    import os
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""
    ex = StateGraphExecutor()
    step = PlannedStep(intent="i", source="workflow", target_element="", modality="applescript",
                       command={"script": 'tell application "Finder" to quit'})
    r = asyncio.run(ex._execute_step(step))
    assert r.get("gated") is True  # the dangerous AppleScript path no longer fires ungated


def test_ax_primitive_gated_when_drive_off():
    import os
    from anyuse.context.accessibility import AccessibilityTree
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""
    t = AccessibilityTree()
    assert t.perform_action("elem", "AXPress") is False  # short-circuits, no real AX call
    assert t.set_value("elem", "v") is False


# ── drag (Batch 2) ────────────────────────────────────────────────────────────
def test_to_action_drag_converts_both_points():
    from anyuse.execution.agent_orchestrator import AgentOrchestrator
    o = AgentOrchestrator(api_key="x", pixel_scale=1.0)
    action, params = o._to_action("drag", {"x": 10, "y": 20, "x2": 100, "y2": 200})
    assert action == "drag_to"
    assert params == {"x1": 10, "y1": 20, "x2": 100, "y2": 200}, params


def test_execute_coordinate_drag_dispatches_drag_to():
    class _FakeAX:
        def __init__(self):
            self.calls = []

        def execute(self, action, params):
            self.calls.append((action, params)); return {"success": True}

    ax = _FakeAX()
    ex = StateGraphExecutor(action_executor=ax)
    step = PlannedStep(intent="i", source="workflow", target_element="", modality="coordinate",
                       command={"action": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4})
    asyncio.run(ex._execute_step(step))
    assert ax.calls == [("drag_to", {"x1": 1, "y1": 2, "x2": 3, "y2": 4})], ax.calls


# ── secure-field value redaction (Batch 2 security) ──────────────────────────
def test_secure_text_field_value_is_redacted():
    from context import accessibility as A
    tree = A.AccessibilityTree()
    # The element stands in for its own role via fake_attr's AXRole lookup.
    def fake_attr(element, attr):
        return {"AXRole": element, "AXValue": "hunter2", "AXTitle": "Password"}.get(attr, "")

    orig = A._attr
    A._attr = fake_attr
    try:
        secure = tree._describe("AXSecureTextField")
        assert secure.role == "AXSecureTextField" and secure.value == "", f"leaked: {secure.value!r}"
        normal = tree._describe("AXTextField")
        assert normal.value == "hunter2", "non-secure value must pass through"
    finally:
        A._attr = orig


if __name__ == "__main__":
    from anyuse.context.accessibility import AXSnapshot  # noqa: F401 (import smoke for _run_verify)
    import inspect, sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:200])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
