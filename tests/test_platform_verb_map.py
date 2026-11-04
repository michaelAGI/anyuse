"""The platform verb map.

With a Platform injected (the orchestrator injects one whenever it has a tree),
``_execute_ax`` used to map every action it did not know - ``enter_text``,
``press_key``, ``insert_text``, ``replace_range``, ``raise`` - to the verb
``invoke`` (AXPress) and report success. One table (``AX_ACTIONS``) now drives
both branches: an action with a platform verb goes to ``perform_verb``; an
action without one runs its own inline handler on either branch; an action
that is not in the table is a typed ``unsupported_verb`` refusal on both. A
parity test checks that, for every action, the two branches produce the same
native call. Fakes only.

Run from the anyuse package dir:
    cd anyuse && python3 test_platform_verb_map.py
"""
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"   # fakes only: nothing below reaches an OS layer

from anyuse.execution.executor import AX_ACTIONS, StateGraphExecutor  # noqa: E402
from anyuse.execution.plan_types import PlannedStep  # noqa: E402
from anyuse.osplatform.macos import _VERB_TO_AX  # noqa: E402

EL = {"id": "field"}


class _Ev:
    def __init__(self, ok=True):
        self.ok, self.rung, self.readback = ok, "ax_value", "x"

    def __bool__(self):
        return self.ok

    def to_dict(self):
        return {"ok": self.ok}


class _Tree:
    """Records the native call each action ends in. The runner contract's rows
    (execution/ax_actions.py) read the field's value before and after an edit, the selected
    rows after a selection and the front app after a switch: those reads keep a value, a
    selection and a front app, and the edit, the selection and the switch change them."""

    def __init__(self):
        self.calls = []
        self.value, self.selected, self.front = "1.0", [], "com.example.other"
        self.toggle = "off"                 # a state-aware toggle's state (set_toggle)

    def find(self, role=None, title=None):
        return [EL]

    def perform_action(self, el, action):
        self.calls.append(("native", action))
        if action == "AXPress":
            self.toggle = "on" if self.toggle == "off" else "off"
        return True

    def toggle_state(self, el):
        self.calls.append(("read",)); return self.toggle

    def toggle_readback(self, el, want):
        self.calls.append(("read",)); return self.toggle

    def close_menu(self, el):
        self.calls.append(("close_menu",)); return True

    def set_value(self, el, value):
        self.calls.append(("set_value", value)); return True

    def enter_text(self, el, value, **kw):
        self.calls.append(("enter_text", value)); return _Ev()

    def edit_text(self, el, value, replace_range=None, expect_value=None):
        self.calls.append(("edit_text", value, replace_range))
        if replace_range is not None:
            start, length = replace_range
            self.value = self.value[:start] + value + self.value[start + length:]
        return _Ev()

    def select_row(self, el):
        self.calls.append(("select_row",)); self.selected = ["Name"]; return True

    def selected_row_labels(self, el):
        return list(self.selected)

    def activate_app(self, bundle_id):
        self.calls.append(("activate_app", bundle_id)); self.front = bundle_id; return True

    def frontmost_bundle_id(self):
        return self.front

    def press_key(self, keycode, flags=0):
        self.calls.append(("press_key", keycode, flags)); return True

    def read_value(self, el):
        self.calls.append(("read",)); return self.value


class _Platform:
    """MacOSPlatform's own verb translation, recorded instead of performed."""

    def __init__(self, tree):
        self.tree, self.verbs = tree, []

    def perform_verb(self, el, verb, value=None):
        self.verbs.append(verb)
        if verb == "set_value":
            return self.tree.set_value(el, value)
        native = _VERB_TO_AX.get(verb)
        return False if native is None else self.tree.perform_action(el, native)


def _step(action):
    cmd = {"role": "AXTextField", "title": "Name", "action": action}
    if action in ("press_key", "key"):
        cmd.update(keycode=36, flags=0)
    if action == "replace_range":
        cmd["range"] = [0, 2]
    if action == "replace_line":
        cmd["anchor"] = "1.0"               # the fake field's one line
    if action == "open_app":
        cmd.update(bundle_id="com.example.app", app_name="Example")
    if action == "set_toggle":
        cmd["target_state"] = "on"          # the fake starts off: one checked press
    if action == "press_shortcut":
        # a recorded chord no menu item owned, sent to the recorded app (the fake's front app)
        cmd.update(keys="cmd+n", keycode=45, flags=0x100000, bundle_id="com.example.other")

    return PlannedStep(intent=action, source="workflow", target_element="", modality="ax",
                       command=cmd, input_data="hello")


def _run(action, with_platform):
    tree = _Tree()
    plat = _Platform(tree) if with_platform else None
    res = StateGraphExecutor(ax_tree=tree, platform=plat)._execute_ax(_step(action))
    return res, tree.calls, (plat.verbs if plat else [])


#: the runner contract's rows read the value back around their one actuation
_CONTRACT_READS = {"append_text", "replace_line", "set_toggle"}


def test_every_action_has_a_handler_and_none_becomes_a_press_by_default():
    for action, spec in AX_ACTIONS.items():
        res, calls, verbs = _run(action, with_platform=True)
        assert res["success"] is True, (action, res)
        acts = [c for c in calls if c != ("read",)] if action in _CONTRACT_READS else calls
        assert len(acts) == 1, (action, calls)
        if spec["verb"] is None:
            assert verbs == [], f"{action} has no platform verb and must not reach perform_verb: {verbs}"
            assert calls[0] != ("native", "AXPress"), f"{action} became AXPress: {calls}"


def test_text_and_key_verbs_run_their_own_handlers_on_the_platform_branch():
    expected = {"enter_text": ("enter_text", "hello"), "insert_text": ("edit_text", "hello", None),
                "replace_range": ("edit_text", "hello", (0, 2)), "press_key": ("press_key", 36, 0),
                "key": ("press_key", 36, 0), "raise": ("native", "AXRaise"), "read": ("read",)}
    for action, call in expected.items():
        res, calls, verbs = _run(action, with_platform=True)
        assert calls == [call] and verbs == [], (action, calls, verbs)
    # the runner contract's rows (execution/ax_actions.py): their own handler, their read-back
    contract = {"select_row": [("select_row",)],
                "append_text": [("read",), ("edit_text", "hello", (3, 0)), ("read",)],
                "replace_line": [("read",), ("edit_text", "hello", (0, 3)), ("read",)],
                "open_app": [("activate_app", "com.example.app")],
                # the state-aware toggle: its press only after the state was read, then the read-back
                "set_toggle": [("read",), ("native", "AXPress"), ("read",)],
                # a shortcut: the chord itself, with its modifier flags (no window counts to read here)
                "press_shortcut": [("press_key", 45, 0x100000)]}
    for action, want in contract.items():
        res, calls, verbs = _run(action, with_platform=True)
        assert res["success"] and calls == want and verbs == [], (action, res, calls, verbs)


def test_parity_platform_branch_and_inline_branch_make_the_same_native_call():
    for action in AX_ACTIONS:
        on_platform = _run(action, with_platform=True)[1]
        inline = _run(action, with_platform=False)[1]
        assert on_platform == inline, (action, on_platform, inline)


def test_unsupported_verbs_are_typed_refusals_on_both_branches():
    for action in ("AXShowDefaultUI", "double_click", "hover", "drag", "", "Press"):
        for with_platform in (True, False):
            res, calls, verbs = _run(action, with_platform)
            assert res["success"] is False and res["outcome_kind"] == "refused", (action, res)
            assert res["reason_code"] == "unsupported_verb", res
            assert calls == [] and verbs == [], (action, calls, verbs)


def test_press_key_without_a_keycode_fails_with_a_reason():
    tree = _Tree()
    step = PlannedStep(intent="k", source="workflow", target_element="", modality="ax",
                       command={"role": "AXTextField", "title": "Name", "action": "press_key"})
    res = StateGraphExecutor(ax_tree=tree, platform=_Platform(tree))._execute_ax(step)
    assert res["success"] is False and "keycode" in res["error"] and tree.calls == []


def test_the_recorder_never_records_a_gesture_as_a_press():
    from anyuse.memory.recorder import StateGraphRecorder
    from anyuse.context.accessibility import AXElementDesc
    btn, box = AXElementDesc(role="AXButton", title="OK"), AXElementDesc(role="AXCheckBox", title="On")
    m = StateGraphRecorder._ax_action_for
    assert m("click_at", btn) == "press" and m("click_at", box) == "toggle"
    assert m("type_text_at", btn) == "set_value" and m("right_click_at", btn) == "show_menu"
    assert m("hover_at", btn) is None and m("double_click_at", btn) is None
    for a in ("press", "toggle", "set_value", "show_menu"):
        assert a in AX_ACTIONS, a


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
