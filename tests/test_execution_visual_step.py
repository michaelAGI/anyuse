"""anyuse execution.visual_step + executor._execute_visual unit test.

SAFETY-CRITICAL invariant under test: with ``ANYUSE_DRIVE_ENABLED`` unset (the
default), StateGraphExecutor._execute_visual on a step with NO x/y performs NO
capture, NO model call, and NO actuation — it returns the non-actuating
structured stub. A spy resolver and a spy action_executor are injected and
asserted to be NEVER called on the gated-OFF path.

The whole capture->model->parse loop (VisualStepResolver) is exercised with a
fake ScreenshotCapture and a recorded adapter, so NO real screencapture, NO
real network/model call, and NO live drive ever happen. The gated-ON path is
driven through a SPY action_executor (never real pyautogui) and only asserts the
EXISTING coordinate executor is reached — no new actuation site.

Run from the anyuse package dir:
    cd anyuse && /usr/local/bin/python3.13 test_execution_visual_step.py
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

from anyuse.execution.planner import PlannedStep          # noqa: E402
from anyuse.execution.visual_step import VisualStepResolver  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor   # noqa: E402
from anyuse.execution.model_adapter import ModelConfig, ModelResult  # noqa: E402

_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-pixels"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fakes — no real screen, no real model, no real actuation
# ---------------------------------------------------------------------------

class _FakeScreenshotCapture:
    """capture() -> fake PNG bytes; records that it was called."""

    def __init__(self, png=_FAKE_PNG):
        self.png = png
        self.calls = 0

    def capture(self):
        self.calls += 1
        return self.png


class _FakeAdapter:
    """Recorded ModelAdapter boundary; returns one provider-shaped result."""
    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []
        self.config = ModelConfig("fake", "http://127.0.0.1:1/v1", "vision", "")

    async def acomplete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return ModelResult(ok=True, text=self.response_text, provider="fake",
                           model="vision", llm_calls=1)


class _SpyActionExecutor:
    """Records every .execute() call; returns success WITHOUT touching pyautogui."""

    def __init__(self):
        self.calls = []  # list of (action, params)

    def execute(self, action, params):
        self.calls.append((action, params))
        return {"success": True}


class _SpyVisualResolver:
    """Records resolve() calls; returns a canned result. Never captures/actuates."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": False, "command": {}, "reasoning": "", "error": "spy"}

    async def resolve(self, step, ax_json=""):
        self.calls.append({"step": step, "ax_json": ax_json})
        return self.result


def _step_no_coords():
    return PlannedStep(
        intent="click the Save button",
        source="workflow",
        target_element="Save button",
        modality="computer_use",
        command={"description": "click the Save button"},
        expected_after="document saved",
    )


def _step_with_coords():
    return PlannedStep(
        intent="click the Save button",
        source="workflow",
        target_element="Save button",
        modality="computer_use",
        command={"x": 200, "y": 400, "action": "click"},
        expected_after="document saved",
    )


# ---------------------------------------------------------------------------
# 1) Resolver: capture -> model -> parse (plain JSON)
# ---------------------------------------------------------------------------

def test_resolver_capture_and_parse():
    cap = _FakeScreenshotCapture()
    adapter = _FakeAdapter(
        '{"action":"coordinate_command",'
        '"command":{"x":120,"y":340,"action":"click"},'
        '"reasoning":"button at top-left"}'
    )
    resolver = VisualStepResolver(
        adapter=adapter,
        screenshot_capture=cap,
        pixel_scale=1.0,  # deterministic: no Retina conversion in the assertion
    )

    out = _run(resolver.resolve(_step_no_coords(), ax_json='{"elements":[]}'))

    assert out["ok"] is True, out
    assert out["command"]["x"] == 120, out
    assert out["command"]["y"] == 340, out
    assert out["command"]["action"] == "click", out
    assert out["llm_calls"] == 1, out

    # capture() was used (no real screen), model was called once.
    assert cap.calls == 1, cap.calls
    assert len(adapter.calls) == 1, adapter.calls
    assert adapter.calls[0]["images"][0].startswith("data:image/png;base64,"), adapter.calls
    print("OK  resolver capture->adapter->parse yields x=120,y=340 (image data URL)")


# ---------------------------------------------------------------------------
# 2) Resolver: markdown-fenced JSON still parses
# ---------------------------------------------------------------------------

def test_resolver_markdown_fenced_json():
    cap = _FakeScreenshotCapture()
    fenced = (
        "Here is the action:\n"
        "```json\n"
        '{"action":"coordinate_command","command":{"x":50,"y":60,"action":"click"},'
        '"reasoning":"fenced"}\n'
        "```\n"
    )
    resolver = VisualStepResolver(
        adapter=_FakeAdapter(fenced),
        screenshot_capture=cap,
        pixel_scale=1.0,  # deterministic: assert raw parsed coords, no Retina conversion
    )

    out = _run(resolver.resolve(_step_no_coords()))
    assert out["ok"] is True, out
    assert out["command"]["x"] == 50 and out["command"]["y"] == 60, out
    print("OK  resolver tolerates ```json fenced output (x=50,y=60)")


# ---------------------------------------------------------------------------
# 3) Resolver: unparseable model output -> ok=False, no exception
# ---------------------------------------------------------------------------

def test_resolver_unparseable():
    cap = _FakeScreenshotCapture()
    resolver = VisualStepResolver(
        adapter=_FakeAdapter("sorry no"),
        screenshot_capture=cap,
    )

    out = _run(resolver.resolve(_step_no_coords()))
    assert out["ok"] is False, out
    assert "command" in out and out["command"] == {}, out
    assert out.get("error"), out
    print("OK  resolver returns ok=False on unparseable output (no exception)")


# ---------------------------------------------------------------------------
# 4) KEYSTONE: gate OFF -> zero capture, zero actuation
# ---------------------------------------------------------------------------

def test_execute_visual_gate_off_never_actuates():
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)  # default OFF

    spy_actions = _SpyActionExecutor()
    spy_resolver = _SpyVisualResolver(
        result={"ok": True, "command": {"x": 9, "y": 9, "action": "click"}, "reasoning": "x"}
    )
    ex = StateGraphExecutor(
        action_executor=spy_actions,
        visual_resolver=spy_resolver,
    )

    out = _run(ex._execute_visual(_step_no_coords()))

    assert out["success"] is False, out
    assert out["needs_orchestrator"] is True, out
    assert out["method"] == "computer_use", out
    assert "gated" in out["error"].lower(), out

    # The keystone safety assertion: gate OFF => zero resolve, zero actuation.
    assert spy_resolver.calls == [], spy_resolver.calls
    assert spy_actions.calls == [], spy_actions.calls
    print("OK  gate OFF: _execute_visual actuates NOTHING (resolver + actions both untouched)")


# ---------------------------------------------------------------------------
# 5) Gate ON -> resolver result flows through the EXISTING coordinate executor
# ---------------------------------------------------------------------------

def test_execute_visual_gate_on_uses_resolver_then_coordinate():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    try:
        spy_actions = _SpyActionExecutor()
        spy_resolver = _SpyVisualResolver(
            result={
                "ok": True,
                "command": {"x": 50, "y": 60, "action": "click"},
                "reasoning": "Save at 50,60",
                "llm_calls": 1,
            }
        )
        ex = StateGraphExecutor(
            action_executor=spy_actions,
            visual_resolver=spy_resolver,
        )

        out = _run(ex._execute_visual(_step_no_coords()))

        assert out["success"] is True, out
        assert out["method"] == "coordinate", out
        assert out["llm_calls"] == 1, out

        # Resolver consulted exactly once.
        assert len(spy_resolver.calls) == 1, spy_resolver.calls

        # Actuation reached ONLY via the EXISTING coordinate executor — exactly
        # one click_at with the resolved coordinates. No new actuation site.
        assert len(spy_actions.calls) == 1, spy_actions.calls
        action, params = spy_actions.calls[0]
        assert action == "click_at", action
        assert params["x"] == 50 and params["y"] == 60, params
        print("OK  gate ON: resolver -> EXISTING _execute_coordinate (click_at x=50,y=60, once)")
    finally:
        os.environ.pop("ANYUSE_DRIVE_ENABLED", None)


# ---------------------------------------------------------------------------
# 6) x/y shortcut is unchanged regardless of the gate
# ---------------------------------------------------------------------------

def test_get_current_ax_json_with_a_real_snapshot_present():
    """_get_current_ax_json() called .to_json() on an AXSnapshot,
    which context/accessibility.py has never defined. Every existing test in
    this file constructs StateGraphExecutor with no ax_tree, so
    _take_snapshot() short-circuits to None before .to_json() is ever
    reached — this is the gap where the bug hid. A drift-gate escalation
    against a REAL ax_tree (execute_checked_step) reaches it."""
    import json as _json
    from anyuse.context.accessibility import AXSnapshot, AXElementDesc

    class _StubAxTree:
        def snapshot(self):
            return AXSnapshot(app_name="Calculator",
                              elements=[AXElementDesc(role="AXButton", title="AC")])

    ex = StateGraphExecutor(ax_tree=_StubAxTree())
    raw = ex._get_current_ax_json()
    data = _json.loads(raw)  # must not raise, and must not be the error stub
    assert data["app_name"] == "Calculator", data
    assert data["elements"][0]["role"] == "AXButton", data
    print("OK  _get_current_ax_json() returns real JSON when a snapshot is present")


def test_execute_visual_xy_shortcut_unchanged():
    os.environ.pop("ANYUSE_DRIVE_ENABLED", None)  # gate OFF

    spy_actions = _SpyActionExecutor()
    spy_resolver = _SpyVisualResolver()
    ex = StateGraphExecutor(
        action_executor=spy_actions,
        visual_resolver=spy_resolver,
    )

    out = _run(ex._execute_visual(_step_with_coords()))

    # Deterministic shortcut: straight to _execute_coordinate, gate-independent,
    # resolver NEVER consulted.
    assert out["success"] is True, out
    assert out["method"] == "coordinate", out
    assert spy_resolver.calls == [], spy_resolver.calls
    assert len(spy_actions.calls) == 1, spy_actions.calls
    assert spy_actions.calls[0][0] == "click_at", spy_actions.calls
    assert spy_actions.calls[0][1]["x"] == 200, spy_actions.calls
    print("OK  x/y shortcut preserved: -> _execute_coordinate directly (gate-independent)")


def main():
    print("=" * 60)
    print("anyuse Wave-6 — visual tier (planner-path VisualStepResolver)")
    print("=" * 60)
    test_resolver_capture_and_parse()
    test_resolver_markdown_fenced_json()
    test_resolver_unparseable()
    test_execute_visual_gate_off_never_actuates()
    test_execute_visual_gate_on_uses_resolver_then_coordinate()
    test_get_current_ax_json_with_a_real_snapshot_present()
    test_execute_visual_xy_shortcut_unchanged()
    print("\nANYUSE VISUAL STEP TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
