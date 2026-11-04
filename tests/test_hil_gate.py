"""Unit tests for anyuse.execution.hil_gate — the HIL pause-before-destructive gate.

SAFETY-CRITICAL invariant under test: the gate ONLY decides — it touches no OS
layer — and the master ``drive_enabled`` contract is absolute (gate OFF →
``actuate=False`` and the approver is NEVER consulted). Pure in-memory: no live
agent, no actuation, no real screencapture/pyautogui/osascript, no network.

Run from the anyuse package dir:
    /usr/local/bin/python3.13 test_hil_gate.py
    # or
    /usr/local/bin/python3.13 -m pytest test_hil_gate.py -q
"""
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)                       # flat imports
import pathlib as _pathlib  # noqa: E402
import tempfile as _tempfile  # noqa: E402
# `import anyuse` must resolve to THIS checkout, never a sibling directory named anyuse.
_LINK = _tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(_pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution.hil_gate import (  # noqa: E402
    ApprovalDecision,
    ApprovalRequired,
    HILGate,
    StepClass,
    classify_step,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# A destructive step reused across the gate tests.
_DESTRUCTIVE_STEP = {"action": "click", "args": {"label": "Delete account"}}
_READ_ONLY_STEP = {"action": "screenshot"}


class _Recorder:
    """An approver that records every call but NEVER actuates anything — it only
    returns a fixed verdict. A recorder, so the gate-OFF
    test can assert the approver was never consulted."""

    def __init__(self, verdict=True):
        self.calls = []
        self.verdict = verdict

    async def __call__(self, context):
        self.calls.append(context)
        return self.verdict


# ---------------------------------------------------------------------------
# 1. Classifier
# ---------------------------------------------------------------------------

def test_classifier_labels_destructive():
    # A destructive keyword on a click label → destructive.
    assert classify_step(
        {"action": "click", "args": {"label": "Delete account"}}
    ) == StepClass.DESTRUCTIVE
    # Navigation → side-effecting.
    assert classify_step(
        {"action": "navigate", "args": {"url": "https://x.com"}}
    ) == StepClass.SIDE_EFFECTING
    # Pure perception / inert motion → read-only.
    assert classify_step({"action": "screenshot"}) == StepClass.READ_ONLY
    assert classify_step({"action": "scroll"}) == StepClass.READ_ONLY
    # Unknown action under an active gate → FAIL CLOSED → destructive.
    assert classify_step({"action": "frobnicate_widget"}) == StepClass.DESTRUCTIVE
    print("OK  classifier: destructive / side_effecting / read_only / fail-closed")


def test_classifier_extra_shapes_and_keywords():
    # Destructive keyword in the action NAME (not just args).
    assert classify_step({"action": "submit_form"}) == StepClass.DESTRUCTIVE
    # Destructive keyword nested in a payload (reuses _iter_strings walk).
    assert classify_step(
        {"action": "type", "args": {"fields": ["ok", "please erase everything"]}}
    ) == StepClass.DESTRUCTIVE
    # A benign mutating action (type) with no destructive keyword → side-effecting.
    assert classify_step(
        {"action": "type_text", "args": {"text": "hello world"}}
    ) == StepClass.SIDE_EFFECTING
    # Scripting action → side-effecting (no destructive keyword present).
    assert classify_step(
        {"action": "applescript", "args": {"script": "tell app \"Finder\""}}
    ) == StepClass.SIDE_EFFECTING
    # Flat shape (no "args" wrapper) still classifies.
    assert classify_step({"action": "navigate", "url": "https://x.com"}) == StepClass.SIDE_EFFECTING
    print("OK  classifier: name keywords / nested payload / flat shape / scripting")


# ---------------------------------------------------------------------------
# 2. THE safety assertion — gate OFF never calls the approver and does not actuate
# ---------------------------------------------------------------------------

def test_gate_off_never_calls_approver_and_does_not_actuate():
    rec = _Recorder(verdict=True)  # would approve — but must never be asked
    gate = HILGate(mode="destructive", approver=rec)
    decision = _run(gate.require_approval(_DESTRUCTIVE_STEP, drive_enabled=False))
    assert decision.actuate is False, decision
    assert decision.reason == "gate OFF: no actuation", decision
    # THE safety assertion: gate OFF short-circuits BEFORE approval.
    assert rec.calls == [], f"APPROVER CONSULTED while gate OFF: {rec.calls}"
    print("OK  gate OFF → actuate=False, approver NEVER called")


# ---------------------------------------------------------------------------
# 3. Destructive step BLOCKS pending approval when gate ON and no approver
# ---------------------------------------------------------------------------

def test_destructive_blocks_pending_approval_when_gate_on_no_approver():
    gate = HILGate(mode="destructive")  # no approver injected
    raised = False
    try:
        _run(gate.require_approval(_DESTRUCTIVE_STEP, drive_enabled=True))
    except ApprovalRequired as e:
        raised = True
        assert e.step_class == StepClass.DESTRUCTIVE, e.step_class
    assert raised, "destructive step with no approver must RAISE ApprovalRequired"
    print("OK  destructive + gate ON + no approver → raises ApprovalRequired (no actuation)")


# ---------------------------------------------------------------------------
# 4. Destructive step awaits the approver and actuates only on explicit True
# ---------------------------------------------------------------------------

def test_destructive_awaits_and_actuates_only_on_explicit_approval():
    # Approver returns True → actuate True, awaited exactly once.
    rec_yes = _Recorder(verdict=True)
    gate_yes = HILGate(mode="destructive", approver=rec_yes)
    decision = _run(gate_yes.require_approval(_DESTRUCTIVE_STEP, drive_enabled=True))
    assert decision.actuate is True, decision
    assert decision.step_class == StepClass.DESTRUCTIVE, decision
    assert len(rec_yes.calls) == 1, rec_yes.calls
    # The approver received a context dict carrying the classification.
    ctx = rec_yes.calls[0]
    assert ctx["step_class"] == StepClass.DESTRUCTIVE, ctx
    assert ctx["action"] == "click", ctx

    # Approver returns False → denied, still no actuation.
    rec_no = _Recorder(verdict=False)
    gate_no = HILGate(mode="destructive", approver=rec_no)
    decision_no = _run(gate_no.require_approval(_DESTRUCTIVE_STEP, drive_enabled=True))
    assert decision_no.actuate is False, decision_no
    assert "denied" in decision_no.reason, decision_no
    assert len(rec_no.calls) == 1, rec_no.calls

    # Anything non-True (e.g. None) is treated as NOT approved (fail-closed).
    rec_none = _Recorder(verdict=None)
    gate_none = HILGate(mode="destructive", approver=rec_none)
    decision_none = _run(gate_none.require_approval(_DESTRUCTIVE_STEP, drive_enabled=True))
    assert decision_none.actuate is False, decision_none
    print("OK  destructive → awaits approver; actuates only on explicit True")


# ---------------------------------------------------------------------------
# 5. mode 'off' passthrough (unchanged behavior even with gate ON)
# ---------------------------------------------------------------------------

def test_mode_off_passthrough():
    rec = _Recorder(verdict=False)  # would deny — but must never be asked
    gate = HILGate(mode="off", approver=rec)
    decision = _run(gate.require_approval(_DESTRUCTIVE_STEP, drive_enabled=True))
    assert decision.actuate is True, decision
    assert rec.calls == [], f"approver called under mode 'off': {rec.calls}"
    print("OK  mode 'off' + gate ON → actuate=True, approver never called")


# ---------------------------------------------------------------------------
# 6. read-only step passes under mode 'destructive' (below threshold)
# ---------------------------------------------------------------------------

def test_readonly_passes_under_destructive_mode():
    rec = _Recorder(verdict=False)
    gate = HILGate(mode="destructive", approver=rec)
    decision = _run(gate.require_approval(_READ_ONLY_STEP, drive_enabled=True))
    assert decision.actuate is True, decision
    assert decision.step_class == StepClass.READ_ONLY, decision
    assert rec.calls == [], f"approver called for a read-only step: {rec.calls}"
    print("OK  read-only step under mode 'destructive' → actuate=True, no approval")


# ---------------------------------------------------------------------------
# Mode 'all' holds side-effecting too; default-dormant env handling.
# ---------------------------------------------------------------------------

def test_mode_all_holds_side_effecting_but_not_read_only():
    rec = _Recorder(verdict=True)
    gate = HILGate(mode="all", approver=rec)
    # A side-effecting nav is held under mode 'all'.
    dec_nav = _run(gate.require_approval(
        {"action": "navigate", "args": {"url": "https://x.com"}}, drive_enabled=True))
    assert dec_nav.actuate is True, dec_nav
    assert dec_nav.step_class == StepClass.SIDE_EFFECTING, dec_nav
    assert len(rec.calls) == 1, rec.calls
    # A read-only step is NOT held even under 'all'.
    dec_ro = _run(gate.require_approval(_READ_ONLY_STEP, drive_enabled=True))
    assert dec_ro.actuate is True, dec_ro
    assert len(rec.calls) == 1, "read-only must not consult the approver"
    print("OK  mode 'all' → holds side-effecting, not read-only")


def test_unknown_env_mode_is_dormant_off():
    prev = os.environ.get("ANYUSE_HIL_MODE")
    os.environ["ANYUSE_HIL_MODE"] = "bogus-mode"
    try:
        gate = HILGate()  # mode read from env
        assert gate.mode == "off", gate.mode
        # Dormant: a destructive step still actuates (only master-gated upstream).
        decision = _run(gate.require_approval(_DESTRUCTIVE_STEP, drive_enabled=True))
        assert decision.actuate is True, decision
    finally:
        if prev is None:
            os.environ.pop("ANYUSE_HIL_MODE", None)
        else:
            os.environ["ANYUSE_HIL_MODE"] = prev
    print("OK  unknown ANYUSE_HIL_MODE → dormant 'off' (behavior unchanged)")


def test_default_env_unset_is_off():
    prev = os.environ.get("ANYUSE_HIL_MODE")
    os.environ.pop("ANYUSE_HIL_MODE", None)
    try:
        gate = HILGate()
        assert gate.mode == "off", gate.mode
        # And with the master gate OFF the decision is actuate=False regardless.
        dec = _run(gate.require_approval(_DESTRUCTIVE_STEP, drive_enabled=False))
        assert dec.actuate is False, dec
    finally:
        if prev is not None:
            os.environ["ANYUSE_HIL_MODE"] = prev
    print("OK  ANYUSE_HIL_MODE unset → mode 'off' (default-dormant)")


# ---------------------------------------------------------------------------
# The gate only DECIDES — it imports/touches no OS actuation layer.
# ---------------------------------------------------------------------------

def test_module_imports_no_os_actuation_layer():
    # The gate must only DECIDE — its OWN source must declare no OS-actuation
    # dependency. NOTE: importing any anyuse submodule runs the parent package
    # __init__ chain (anyuse.core / anyuse.execution), which eagerly imports the
    # actuation-laden siblings (executor/intervention -> matcher/vault ->
    # pyautogui) — that is PRE-EXISTING package behavior, NOT something hil_gate
    # introduces. So we assert the meaningful invariant at the SOURCE level:
    # hil_gate.py imports/references no pyautogui/Quartz/screencapture/osascript.
    import ast

    import anyuse.execution.hil_gate as hg

    with open(hg.__file__, "r", encoding="utf-8") as fh:
        src = fh.read()
    # Parse the module and collect every imported top-level module name. A
    # docstring that merely MENTIONS "osascript"/"screencapture" (this module's
    # docs say it touches none of them) must not trip the check — only real
    # imports count.
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden_modules = {
        "pyautogui", "Quartz", "AppKit", "subprocess", "ctypes",
        "Cocoa", "ApplicationServices", "Carbon",
    }
    leaked = sorted(imported & forbidden_modules)
    assert leaked == [], f"hil_gate imports an OS-actuation module: {leaked}"

    # And the module surface exposes no actuation symbol — only decision logic.
    public = {n for n in dir(hg) if not n.startswith("_")}
    for sym in ("pyautogui", "screencapture", "click_at", "type_text", "osascript"):
        assert sym not in public, f"hil_gate exposes an actuation symbol: {sym}"
    print("OK  hil_gate source/surface references NO OS actuation layer — it only decides")


# ---------------------------------------------------------------------------
# ApprovalDecision is a frozen, data-only carrier.
# ---------------------------------------------------------------------------

def test_approval_decision_is_frozen():
    dec = ApprovalDecision(actuate=True, reason="x", step_class="read_only")
    frozen = False
    try:
        dec.actuate = False  # type: ignore[misc]
    except Exception:
        frozen = True
    assert frozen, "ApprovalDecision must be frozen (immutable result carrier)"
    print("OK  ApprovalDecision frozen / immutable")


# ---------------------------------------------------------------------------
# Wiring: the gate consulted by StateGraphExecutor._execute_step BEFORE dispatch
# ---------------------------------------------------------------------------
#
# These exercise the FIX-1 wire-point: a destructive step routed through
# ``_execute_step`` with HIL ON is HELD before any actuator runs (the actuator
# spy is asserted NEVER called); with HIL OFF (no gate / mode off) the step
# dispatches exactly as before; a read-only step never holds. PURE + mocked:
# the master ``ANYUSE_DRIVE_ENABLED`` env is never set — we monkeypatch the
# executor module's ``_visual_drive_enabled`` so the drive-ON branch is reachable
# without enabling the real gate, and the modality executor is a spy that records
# calls but actuates NOTHING (no pyautogui / AX / osascript / screencapture).

from anyuse.execution import executor as _executor_mod  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.planner import PlannedStep  # noqa: E402


class _AXSpy:
    """A stand-in AccessibilityTree that RECORDS every call but actuates nothing.
    If the HIL gate holds a step correctly, NONE of these are ever called."""

    def __init__(self):
        self.calls = []

    def find(self, *a, **k):
        self.calls.append(("find", a, k))
        return []  # element-not-found → _execute_ax returns a benign failure dict

    def perform_action(self, *a, **k):
        self.calls.append(("perform_action", a, k))
        return True

    def set_value(self, *a, **k):
        self.calls.append(("set_value", a, k))
        return True

    def snapshot(self):
        return None


_DESTRUCTIVE_AX_STEP = PlannedStep(
    intent="delete the account",
    source="workflow",
    target_element="btn",
    modality="ax",
    command={"role": "AXButton", "title": "Delete account", "action": "press"},
)
_READ_ONLY_AX_STEP = PlannedStep(
    intent="read the screen",
    source="workflow",
    target_element="lbl",
    modality="ax",
    command={"role": "AXStaticText", "title": "hello", "action": "read"},
)


class _force_drive_on:
    """Context manager: force the executor's master-gate reader to report ON,
    WITHOUT setting ANYUSE_DRIVE_ENABLED (honors the safety contract: the real
    env gate stays unset; we monkeypatch the module function the executor reads).
    """

    def __enter__(self):
        self._orig = _executor_mod._visual_drive_enabled
        _executor_mod._visual_drive_enabled = lambda: True
        return self

    def __exit__(self, *exc):
        _executor_mod._visual_drive_enabled = self._orig
        return False


def test_execute_step_holds_destructive_when_hil_on_actuator_never_called():
    # HIL ON (mode 'destructive', no approver → fail-closed) + master gate ON:
    # the destructive ax step is HELD before dispatch; the AX spy is NEVER called.
    spy = _AXSpy()
    gate = HILGate(mode="destructive")  # no approver → ApprovalRequired on hold
    ex = StateGraphExecutor(ax_tree=spy, hil_gate=gate)
    with _force_drive_on():
        res = _run(ex._execute_step(_DESTRUCTIVE_AX_STEP))
    assert res["success"] is False, res
    assert res.get("blocked") is True, res
    assert res.get("hil_step_class") == StepClass.DESTRUCTIVE, res
    assert res["method"] == "ax", res
    # THE safety assertion: the actuator (AX tree) was NEVER touched.
    assert spy.calls == [], f"ACTUATOR LEAKED on a held destructive step: {spy.calls}"
    print("OK  _execute_step: HIL ON destructive → HELD, actuator NEVER called")


def test_execute_step_held_when_approver_denies_actuator_never_called():
    # HIL ON with an approver that DENIES → still held, actuator never called.
    rec = _Recorder(verdict=False)
    spy = _AXSpy()
    ex = StateGraphExecutor(ax_tree=spy, hil_gate=HILGate(mode="destructive", approver=rec))
    with _force_drive_on():
        res = _run(ex._execute_step(_DESTRUCTIVE_AX_STEP))
    assert res["success"] is False and res.get("blocked") is True, res
    assert res.get("hil_step_class") == StepClass.DESTRUCTIVE, res
    assert len(rec.calls) == 1, rec.calls  # the approver WAS consulted (once)
    assert spy.calls == [], f"ACTUATOR LEAKED on a denied step: {spy.calls}"
    print("OK  _execute_step: HIL ON + approver denies → HELD, actuator NEVER called")


def test_execute_step_dispatches_destructive_when_hil_off_no_gate():
    # HIL OFF (NO gate injected — the default): the SAME destructive step
    # dispatches exactly as before — the actuator IS reached (spy.find called).
    spy = _AXSpy()
    ex = StateGraphExecutor(ax_tree=spy)  # no hil_gate → inert
    with _force_drive_on():
        res = _run(ex._execute_step(_DESTRUCTIVE_AX_STEP))
    assert res.get("blocked") is not True, res
    assert res["method"] == "ax", res
    # Dispatch proceeded into _execute_ax (which called find on the spy).
    assert any(c[0] == "find" for c in spy.calls), (
        f"no-gate path must dispatch to the actuator: {spy.calls}")
    print("OK  _execute_step: no gate → destructive dispatches as before (unchanged)")


def test_execute_step_dispatches_when_gate_mode_off():
    # Gate present but mode 'off' (default-dormant): inert — the destructive step
    # dispatches as before; the gate is never even consulted (approver untouched).
    rec = _Recorder(verdict=False)  # would deny — must never be asked
    spy = _AXSpy()
    ex = StateGraphExecutor(ax_tree=spy, hil_gate=HILGate(mode="off", approver=rec))
    with _force_drive_on():
        res = _run(ex._execute_step(_DESTRUCTIVE_AX_STEP))
    assert res.get("blocked") is not True, res
    assert rec.calls == [], f"mode-off gate consulted the approver: {rec.calls}"
    assert any(c[0] == "find" for c in spy.calls), (
        f"mode-off must dispatch to the actuator: {spy.calls}")
    print("OK  _execute_step: gate mode 'off' → inert, dispatches as before")


def test_execute_step_readonly_never_holds_under_hil_on():
    # HIL ON (mode 'destructive') but a READ-ONLY step → never held; dispatch
    # proceeds (the approver is never consulted, the actuator IS reached).
    rec = _Recorder(verdict=False)
    spy = _AXSpy()
    ex = StateGraphExecutor(ax_tree=spy, hil_gate=HILGate(mode="destructive", approver=rec))
    with _force_drive_on():
        res = _run(ex._execute_step(_READ_ONLY_AX_STEP))
    assert res.get("blocked") is not True, res
    assert rec.calls == [], f"read-only step consulted the approver: {rec.calls}"
    assert any(c[0] == "find" for c in spy.calls), (
        f"read-only must dispatch to the actuator: {spy.calls}")
    print("OK  _execute_step: HIL ON read-only → never held, dispatches")


def test_execute_step_master_gate_off_is_not_a_hil_hold():
    # HIL mode active BUT master gate OFF: the gate returns actuate=False with an
    # empty step_class ("nothing to approve") — that is NOT a HIL hold, so the
    # executor must PROCEED (let its own downstream gating govern), not block.
    spy = _AXSpy()
    ex = StateGraphExecutor(ax_tree=spy, hil_gate=HILGate(mode="destructive"))
    # NOTE: no _force_drive_on() here → _visual_drive_enabled() reads the env,
    # which is unset → master gate OFF (the safe default).
    prev = os.environ.get("ANYUSE_DRIVE_ENABLED")
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
    try:
        res = _run(ex._execute_step(_DESTRUCTIVE_AX_STEP))
    finally:
        if prev is not None:
            os.environ["ANYUSE_DRIVE_ENABLED"] = prev
    assert res.get("blocked") is not True, res
    # Dispatch proceeded (master gate governs downstream); the HIL layer did not
    # itself manufacture a block on a master-gated-OFF path.
    assert any(c[0] == "find" for c in spy.calls), (
        f"master-gate-OFF must not be a HIL hold: {spy.calls}")
    print("OK  _execute_step: HIL on + master gate OFF → not a HIL hold, proceeds")


# ---------------------------------------------------------------------------
# Lightweight runner (works without pytest installed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    fns = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    failed = 0
    for name, fn in fns:
        try:
            fn()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed of {len(fns)}")
    sys.exit(1 if failed else 0)
