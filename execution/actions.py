"""ActionExecutor — the low-level desktop actuation primitive.

Everything that actually moves the mouse or types goes through here. The engine
(``StateGraphExecutor._execute_coordinate``), the free-form computer-use loop
(``AgentOrchestrator``), and the legacy ``WorkflowPlayer`` all hold an
``ActionExecutor`` and call ``execute(action, params)``. Historically this class
was referenced as ``actions/executor.py`` but never shipped, so NOTHING actuated
— this module fills that hole.

Contract (matches the test spies in test_execution_visual_step.py / drift_gate):

    executor.execute(action: str, params: dict) -> dict   # always has "success"

Supported actions (params in []):
    click_at         [x, y, button="left", clicks=1]
    type_text_at     [x, y, text, press_enter=False]
    hover_at         [x, y]
    key_combination  [keys]          # "cmd+c" / "command,c" / ["command","c"]
    scroll_at        [x, y, direction="down", magnitude=800]

Safety:
  * Keystone gate — actuation only happens when ``ANYUSE_DRIVE_ENABLED`` is
    truthy. With the gate OFF (the default) ``execute`` is a non-actuating no-op
    that returns ``{"success": False, "gated": True, ...}`` WITHOUT touching the
    mouse/keyboard. This mirrors the gate an orchestrator-facing drive seam enforces,
    so the direct Agent/CLI path is just as safe as an orchestrator-driven one.
  * BOUNDARY seam — ``set_compliance_checker`` lets a caller install a
    pre-action policy check (wired in the BOUNDARY-enforcement step). Until one
    is installed, no policy is consulted and behavior is unchanged.
  * Foreground interlock — the real backend calls
    ``foreground_interlock.require_clear`` before every pyautogui call and every
    chord, so no event reaches a session a person is looking at; ``execute`` returns
    ``{"success": False, "refused": True, "interlock": {...}}`` and logs one
    ERROR line. Injected fake backends send no input and are not checked.

The pyautogui-backed actuation is isolated behind ``_Backend`` so the whole class
is unit-testable with an injected fake (no real screen, no pyautogui import).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def drive_enabled() -> bool:
    """The keystone gate: True only when ANYUSE_DRIVE_ENABLED is truthy."""
    from anyuse.settings import get as get_setting
    return bool(get_setting("ANYUSE_DRIVE_ENABLED"))


def _parse_keys(keys: Any) -> List[str]:
    """Normalize a key-combination spec into pyautogui key names.

    Accepts "cmd+c", "command,c", "cmd c", or ["command", "c"]. Maps common
    aliases (cmd→command, opt→option, ctrl→ctrl, esc→escape, ...).
    """
    if isinstance(keys, (list, tuple)):
        raw = [str(k) for k in keys]
    else:
        raw = [k for k in str(keys).replace("+", ",").replace(" ", ",").split(",") if k]
    alias = {
        "cmd": "command", "win": "win", "opt": "option", "alt": "option",
        "ctrl": "ctrl", "control": "ctrl", "esc": "escape", "del": "delete",
        "return": "enter",
    }
    return [alias.get(k.strip().lower(), k.strip().lower()) for k in raw]


class _Backend:
    """Real pyautogui backend. pyautogui is imported lazily so importing this
    module stays cheap and dependency-light (matches the rest of anyuse)."""

    def __init__(self):
        self._pg = None
        #: types text into the focused element; None = context.keyboard.type_unicode (tests inject one)
        self._typer = None
        #: sends a chord; None = context.keyboard.press_chord (tests inject one)
        self._chord = None
        #: chords through Quartz by the current layout; None = on macOS
        self._native_keys = None
        #: the fail-safe check before a native chord; None = pyautogui.failSafeCheck (tests inject one)
        self._failsafe = None

    @property
    def pg(self):
        """pyautogui, after the foreground interlock. Every backend method reaches
        pyautogui only through this property, once per call it makes."""
        from anyuse.execution.foreground_interlock import require_clear
        require_clear("pyautogui")
        if self._pg is None:
            import pyautogui  # heavy/optional — imported only when we actuate
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.05
            self._pg = pyautogui
        return self._pg

    def click_at(self, x, y, button="left", clicks=1):
        self.pg.click(x=int(x), y=int(y), button=button, clicks=int(clicks))

    def type_text_at(self, x=None, y=None, text="", press_enter=False):
        # Focus the target first, then type into it; without a point, type into the focused element.
        if x is not None and y is not None:
            self.pg.click(x=int(x), y=int(y))
        if text:
            self._type(str(text))
        if press_enter:
            self.pg.press("enter")

    def _type(self, text):
        """Type through Unicode key events, so the keyboard layout never changes a character
        (keycode typing swaps y and z on a German layout). pyautogui only where Quartz is absent."""
        typer = self._typer
        if typer is None:
            from anyuse.context.keyboard import type_unicode as typer
        out = typer(text) or {}
        if out.get("typed"):
            return
        if out.get("refused"):
            raise RuntimeError(out.get("error") or "typing refused by the foreground interlock")
        if int(out.get("chars") or 0) == 0:
            self.pg.typewrite(text, interval=0.01)   # no Quartz (not macOS): best effort by keycode
            return
        raise RuntimeError(f"typing stopped after {out.get('chars')} of {len(text)} characters: "
                           f"{out.get('error') or 'unknown error'}")

    def hover_at(self, x, y):
        self.pg.moveTo(int(x), int(y))

    def leave_failsafe_corner(self):
        """Move the pointer to the screen centre when it rests in a PyAutoGUI fail-safe corner: an
        earlier run's aborted action can leave it there, and every later action is then refused.
        Called once when a run starts, never between a run's actions, so a person moving the
        pointer into a corner still aborts a running task (a native chord checks it too:
        ``_check_failsafe``)."""
        pg = self.pg
        try:
            px, py = (int(v) for v in pg.position())
            w, h = (int(v) for v in pg.size())
        except Exception:  # noqa: BLE001 - no pointer to read, nothing to move
            return
        if (px, py) not in {(0, 0), (0, h - 1), (w - 1, 0), (w - 1, h - 1)}:
            return
        old, pg.FAILSAFE = pg.FAILSAFE, False
        try:
            pg.moveTo(w // 2, h // 2)
        finally:
            pg.FAILSAFE = old
        logger.info("the pointer rested in a fail-safe corner (%d, %d); moved it to the screen centre", px, py)

    def key_combination(self, keys):
        """On macOS the chord goes by the key that types its character on the current layout
        (``context.keyboard.press_chord``): pyautogui's table is US positions, so ``cmd+z`` would be
        ⌘Y on QWERTZ and ``cmd+a`` ⌘Q on AZERTY. An unknown key, or a character no key types
        there, is refused. pyautogui only where there is no Quartz (not macOS)."""
        native = self._native_keys if self._native_keys is not None else sys.platform == "darwin"
        if not native:
            names = _parse_keys(keys)
            if names:
                self.pg.hotkey(*names)
            return
        from anyuse.execution.foreground_interlock import require_clear
        require_clear("keyboard.press_chord")
        self._check_failsafe()
        chord = self._chord
        if chord is None:
            from anyuse.context.keyboard import press_chord as chord
        out = chord(keys) or {}
        if not out.get("sent"):
            raise RuntimeError(out.get("refused") or out.get("error") or f"the chord {keys!r} was not sent")
        logger.debug("chord %r sent as keycode %s flags %#x (%s)", keys, out.get("keycode"),
                     int(out.get("flags") or 0), out.get("how"))

    def _check_failsafe(self):
        """PyAutoGUI's fail-safe for a chord that does not go through pyautogui: the pointer resting in
        a screen corner (a person pushed it there to stop the run) raises ``FailSafeException`` and
        nothing is sent, as ``pyautogui.hotkey`` checks before it presses anything. Without pyautogui
        there is no fail-safe to check (the interlock still decides)."""
        check = self._failsafe
        if check is None:
            try:
                check = getattr(self.pg, "failSafeCheck", None)
            except ImportError:
                return
        if check is not None:
            check()

    def scroll_at(self, x, y, direction="down", magnitude=800):
        amount = int(magnitude)
        if str(direction).lower() == "down":
            amount = -amount
        self.pg.scroll(amount, x=int(x), y=int(y))

    def drag_to(self, x1, y1, x2, y2, button="left", duration=0.3):
        # Press at the start point, drag to the end point (sliders, reorder,
        # selection rectangles, file drag-drop).
        self.pg.moveTo(int(x1), int(y1))
        self.pg.dragTo(int(x2), int(y2), duration=float(duration), button=button)


class ActionExecutor:
    """Gated, policy-aware desktop actuator. See module docstring for contract."""

    #: actions that move mouse/keyboard (subject to the gate + policy)
    _ACTUATING = {"click_at", "type_text_at", "hover_at", "key_combination", "scroll_at", "drag_to",
                  "leave_failsafe_corner"}
    #: the agent's verbs, as older learned workflows stored them, mapped to the executor's actions
    #: (their params were already the executor's, in points)
    LEGACY_VERBS = {"click": "click_at", "double_click": "click_at", "right_click": "click_at",
                    "type": "type_text_at", "key": "key_combination", "scroll": "scroll_at",
                    "hover": "hover_at", "drag": "drag_to"}

    def __init__(self, backend: Optional[_Backend] = None):
        self._backend = backend or _Backend()
        self._compliance_checker = None

    def set_compliance_checker(self, checker) -> None:
        """Install a BOUNDARY/compliance checker consulted before each action.

        The checker must expose ``check_action(descriptor) -> (allowed, reason)``
        (core.restriction.ComplianceChecker's contract; an object/dict with an
        ``allowed`` flag is also tolerated). Until one is set, no policy is consulted.
        """
        self._compliance_checker = checker

    @staticmethod
    def _boundary_snapshot(checker):
        """Private authority snapshot; counters are not another authorization."""
        policy = getattr(checker, "policy", None)
        policy_data = None
        if policy is not None:
            from dataclasses import asdict
            from anyuse.core.restriction import RestrictionPolicy
            if isinstance(policy, RestrictionPolicy):
                # Concrete policies expose plain dataclass configuration. This
                # stays in memory and is never sent to a model or trace artifact.
                policy_data = json.dumps(asdict(policy), sort_keys=True, allow_nan=False)
        method = getattr(checker, "check_action", None)
        return {"checker": checker, "policy": policy, "policy_data": policy_data,
                "control": getattr(checker, "control", None),
                "evaluator": getattr(checker, "guard_evaluator", None),
                "method_owner": getattr(method, "__self__", None),
                "method_function": getattr(method, "__func__", method)}

    def _boundary_unchanged(self, original):
        current = self._boundary_snapshot(self._compliance_checker)
        return (all(current[key] is original[key] for key in
                    ("checker", "policy", "control", "evaluator", "method_owner", "method_function")) and
                current["policy_data"] == original["policy_data"])

    def execute(self, action: str, params: Optional[Dict[str, Any]] = None, *, around=None) -> Dict[str, Any]:
        """Dispatch once after authorization, optionally inside a trusted observer.

        ``around(dispatch)`` belongs to application code, never model input. It
        runs only after drive/BOUNDARY checks and cannot resend an action or
        promote its result. A pre-dispatch refusal may stop stale coordinates.
        """
        params = params or {}
        action = self.LEGACY_VERBS.get(action, action)

        # 1. Keystone gate — never actuate with the drive gate off.
        if action in self._ACTUATING and not drive_enabled():
            return {
                "success": False,
                "gated": True,
                "action": action,
                "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)",
            }

        authorization = None
        if around is not None:
            try:
                params = copy.deepcopy(params)
                authorization = self._boundary_snapshot(self._compliance_checker)
            except Exception:
                return {"success": False, "blocked": True, "action": action,
                        "error": "cannot snapshot BOUNDARY before GUI capture"}

        # 2. BOUNDARY / compliance check (no-op until a checker is installed).
        if self._compliance_checker is not None and action in self._ACTUATING:
            try:
                # core.restriction.ComplianceChecker.check_action takes ONE action
                # descriptor ({"action","args"}) and returns (allowed, reason). The
                # previous 2-arg call + .allowed read was a signature mismatch that
                # fail-closed and blocked 100% of actuation whenever a BOUNDARY was set.
                verdict = self._compliance_checker.check_action({"action": action, "args": params})
                if isinstance(verdict, tuple):
                    allowed, reason = (tuple(verdict) + (None,))[:2]
                else:  # tolerate an object/dict-style checker too
                    allowed = getattr(verdict, "allowed", None)
                    if allowed is None and isinstance(verdict, dict):
                        allowed = verdict.get("allowed")
                    reason = getattr(verdict, "reason", None) or (
                        verdict.get("reason") if isinstance(verdict, dict) else None)
                if allowed is not True:
                    return {
                        "success": False,
                        "blocked": True,
                        "action": action,
                        "error": f"blocked by BOUNDARY: {reason or 'policy denied'}",
                    }
            except Exception as exc:  # a broken checker must not silently actuate
                logger.warning("compliance check errored for %s: %s", action, exc)
                return {
                    "success": False,
                    "blocked": True,
                    "action": action,
                    "error": f"compliance check failed: {exc}",
                }

        # 3. Resolve the authorized backend before starting an observer.
        from anyuse.execution.foreground_interlock import ForegroundInterlockRefused
        try:
            fn = getattr(self._backend, action, None)
        except ForegroundInterlockRefused as exc:
            return {"success": False, "refused": True, "action": action,
                    "interlock": exc.verdict.to_dict(), "error": str(exc)}
        except Exception as exc:
            logger.error("actuation %s failed: %s", action, exc)
            return {"success": False, "action": action, "error": str(exc)}
        if fn is None:
            return {"success": False, "action": action, "error": f"unknown action: {action}"}

        if authorization is not None:
            try:
                unchanged = self._boundary_unchanged(authorization)
            except Exception:
                unchanged = False
            if not unchanged:
                return {"success": False, "blocked": True, "action": action,
                        "error": "BOUNDARY changed during GUI authorization"}

        state = {"started": False, "returned": False, "result": None}
        def finish(result):
            state.update(returned=True, result=result)
            return copy.deepcopy(result) if around is not None else result
        def dispatch():
            if state["started"]:
                if state["returned"]:
                    return copy.deepcopy(state["result"])
                raise RuntimeError("GUI backend dispatch cannot be entered twice")
            state["started"] = True
            try:
                # Capture setup must not outlive revocation or cancellation.
                # check_action has already reserved this action exactly once.
                if around is not None and action in self._ACTUATING and not drive_enabled():
                    return finish({"success": False, "gated": True, "action": action,
                                   "error": "actuation gated (ANYUSE_DRIVE_ENABLED off)"})
                if authorization is not None:
                    try:
                        unchanged = self._boundary_unchanged(authorization)
                    except Exception:
                        unchanged = False
                    if not unchanged:
                        return finish({"success": False, "blocked": True, "action": action,
                                       "error": "BOUNDARY changed during GUI capture; fresh authorization required"})
                control = getattr(self._compliance_checker, "control", None) if around is not None else None
                if control is not None:
                    try:
                        control.require()
                    except Exception as exc:
                        return finish({"success": False, "blocked": True, "action": action,
                                       "error": "actuation control refused: " + str(exc)})
                fn(**params)  # The real backend still checks the foreground interlock.
                return finish({"success": True, "action": action})
            except ForegroundInterlockRefused as exc:
                return finish({"success": False, "refused": True, "action": action,
                               "interlock": exc.verdict.to_dict(), "error": str(exc)})
            except Exception as exc:
                logger.error("actuation %s failed: %s", action, exc)
                return finish({"success": False, "action": action, "error": str(exc)})
        if around is None:
            return dispatch()
        try:
            observed = around(dispatch)
            if state["returned"]:
                return state["result"]
            if isinstance(observed, dict) and observed.get("success") is False:
                return observed
        except Exception as exc:
            logger.warning("GUI around-actuation observer unavailable: %s", type(exc).__name__)
            if state["returned"]:
                return state["result"]
            if state["started"]:
                raise
        return dispatch()
