"""Smoke tests for the computer-use stack: ActionExecutor + AgentOrchestrator.

These are the foundations that were missing (the legacy ``agent`` package), so
Agent.run raised "No module named agent" and nothing actuated. Pure unit tests:
no real screen, no pyautogui, no network — every external seam is injected.
"""
import os

from anyuse.execution.actions import ActionExecutor
from anyuse.execution.agent_orchestrator import AgentOrchestrator


# ---- fakes -----------------------------------------------------------------
class _FakeBackend:
    def __init__(self):
        self.calls = []

    def click_at(self, **k):
        self.calls.append(("click_at", k))

    def type_text_at(self, **k):
        self.calls.append(("type_text_at", k))

    def scroll_at(self, **k):
        self.calls.append(("scroll_at", k))

    def key_combination(self, **k):
        self.calls.append(("key_combination", k))


class _FakeResp:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.i = 0

    def generate_content(self, **_kw):
        t = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        return _FakeResp(t)


class _FakeClient:
    def __init__(self, scripts):
        self.models = _FakeModels(scripts)


class _FakeShot:
    def capture(self):
        return b"PNGBYTES"


# ---- ActionExecutor: keystone gate -----------------------------------------
def test_action_executor_gated_off_does_not_actuate():
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""
    be = _FakeBackend()
    r = ActionExecutor(backend=be).execute("click_at", {"x": 1, "y": 2})
    assert r["success"] is False and r.get("gated") is True, r
    assert be.calls == [], "gate OFF must never touch the backend"


def test_action_executor_gated_on_actuates():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    r = ActionExecutor(backend=be).execute("click_at", {"x": 3, "y": 4, "button": "left"})
    assert r["success"] is True, r
    assert be.calls == [("click_at", {"x": 3, "y": 4, "button": "left"})], be.calls


def test_action_executor_compliance_block():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    ax = ActionExecutor(backend=be)

    class _Deny:
        # Real ComplianceChecker contract: check_action(descriptor) -> (allowed, reason)
        def check_action(self, descriptor):
            assert descriptor.get("action") == "click_at"  # correct descriptor shape
            return (False, "blocked app")

    ax.set_compliance_checker(_Deny())
    r = ax.execute("click_at", {"x": 1, "y": 1})
    assert r["success"] is False and r.get("blocked") is True, r
    assert be.calls == [], "blocked action must not actuate"


def test_action_executor_real_compliance_checker_allows():
    # The signature fix: a real (inactive) ComplianceChecker must ALLOW, not
    # TypeError-block 100% of actuation as it did before.
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    from anyuse.core.restriction import ComplianceChecker
    be = _FakeBackend()
    ax = ActionExecutor(backend=be)
    ax.set_compliance_checker(ComplianceChecker())  # no active policy → pass-through
    r = ax.execute("click_at", {"x": 1, "y": 1})
    assert r["success"] is True, r
    assert be.calls, "allowed action must actuate"


def test_action_executor_unknown_action():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    r = ActionExecutor(backend=_FakeBackend()).execute("teleport", {})
    assert r["success"] is False and "unknown action" in r["error"], r


# ---- AgentOrchestrator: the loop -------------------------------------------
def test_loop_done_first_turn():
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        gemini_client=_FakeClient(['{"status":"done","thought":"Notes is open"}']),
    )
    assert o.run("open Notes") == "Notes is open"
    assert len(o.history) == 1
    assert o.model_calls == 1


def test_loop_click_then_done_actuates():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=be),
        gemini_client=_FakeClient([
            '{"status":"continue","action":"click","x":100,"y":200,"thought":"click"}',
            '{"status":"done","thought":"done now"}',
        ]),
    )
    assert o.run("click the button") == "done now"
    assert be.calls and be.calls[0][1]["x"] == 100 and be.calls[0][1]["y"] == 200
    assert len(o.history) == 2
    assert o.model_calls == 2


def test_loop_dry_run_when_gated_off():
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        gemini_client=_FakeClient(['{"status":"continue","action":"click","x":1,"y":2,"thought":"tap"}']),
    )
    out = o.run("tap")
    assert out.startswith("Dry-run"), out


def test_loop_handles_unparseable_model_response():
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        gemini_client=_FakeClient(["not json at all"]),
    )
    out = o.run("do thing")
    assert out.startswith("Error"), out


def test_loop_max_turns_guard():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    o = AgentOrchestrator(
        api_key="x", max_turns=3, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=_FakeBackend()),
        gemini_client=_FakeClient(['{"status":"continue","action":"scroll","x":0,"y":0,"direction":"down","thought":"keep scrolling"}']),
    )
    assert o.run("scroll forever") == "maximum turns reached"
    assert len(o.history) == 3


class _SpyRecorder:
    """Records the record-driver lifecycle without touching AX/storage."""

    def __init__(self):
        self.events = []

    def start(self):
        self.events.append(("start", None))

    def record_action(self, **kw):
        self.events.append(("record_action", kw))

    def stop(self):
        self.events.append(("stop", None))

    def save(self):
        self.events.append(("save", None))
        return ("workflow", "template")

    def discard(self):
        self.events.append(("discard", None))


def test_recorder_driver_saves_on_success():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    rec = _SpyRecorder()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=_FakeBackend()),
        gemini_client=_FakeClient([
            '{"status":"continue","action":"click","x":1,"y":2,"thought":"click it"}',
            '{"status":"done","thought":"done"}',
        ]),
    )
    assert o.run("do thing", recorder=rec) == "done"
    names = [e[0] for e in rec.events]
    assert names == ["start", "record_action", "stop", "save"], names
    assert "discard" not in names


def test_recorder_driver_discards_on_failure():
    rec = _SpyRecorder()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        gemini_client=_FakeClient(['{"status":"fail","thought":"cannot"}']),
    )
    out = o.run("do thing", recorder=rec)
    assert out.startswith("Error")
    names = [e[0] for e in rec.events]
    assert names == ["start", "discard"], names
    assert "save" not in names


def test_recorder_driver_discards_on_dry_run():
    os.environ["ANYUSE_DRIVE_ENABLED"] = ""  # gate off -> dry run
    rec = _SpyRecorder()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        gemini_client=_FakeClient(['{"status":"continue","action":"click","x":1,"y":2,"thought":"tap"}']),
    )
    out = o.run("do thing", recorder=rec)
    assert out.startswith("Dry-run")
    names = [e[0] for e in rec.events]
    # action is recorded before actuation is attempted, then discarded (no save)
    assert names == ["start", "record_action", "discard"], names
    assert "save" not in names



# ---- focus lock, screenshot mapping, recording note ---------------------------
def test_focus_target_is_the_app_the_task_names_never_a_terminal_or_prompt():
    from anyuse.execution.agent_orchestrator import focus_target
    running = ["Terminal", "TextEdit", "Finder", "UserNotificationCenter"]
    assert focus_target("Open TextEdit and create a note", running) == "TextEdit"
    assert focus_target("tidy up", running) is None                    # nothing named: nothing raised
    assert focus_target("open the Terminal", running) is None           # never the launching terminal
    assert focus_target("x", running, explicit="Notes") == "Notes"
    assert focus_target("x", running, explicit="SecurityAgent") is None


class _ShotPNG:
    def __init__(self, png):
        self.png = png

    def capture(self):
        return self.png


def _retina_png():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2880, 1800), (30, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_a_click_maps_back_from_the_downscaled_screenshot_to_screen_points():
    # 2880x1800 capture at 2x: the model sees 1280x800 and answers in it; the click lands in points
    from anyuse.execution.agent_orchestrator import MODEL_IMAGE_MAX_EDGE, model_view
    png = _retina_png()
    sent, size, (kx, ky) = model_view(png)
    assert size == (1280, 800) and max(size) == MODEL_IMAGE_MAX_EDGE and kx == ky == 2.25
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    client = _FakeClient(['{"status":"continue","action":"click","x":1088,"y":680,"thought":"dock icon"}',
                          '{"status":"done","thought":"ok"}'])
    o = AgentOrchestrator(api_key="x", pixel_scale=2.0, screenshot_capture=_ShotPNG(png),
                          action_executor=ActionExecutor(backend=be), gemini_client=client)
    seen = []
    real = o._build_prompt
    o._build_prompt = lambda task: seen.append(real(task)) or seen[-1]
    assert o.run("click the TextEdit icon") == "ok"
    assert (be.calls[0][1]["x"], be.calls[0][1]["y"]) == (1224, 765), be.calls
    assert "1280x800 pixels" in seen[0]
    # an image that cannot be decoded is sent as captured and maps 1:1 (as before)
    assert model_view(b"PNGBYTES") == (b"PNGBYTES", None, (1.0, 1.0))


def test_the_focus_lock_follows_the_task_not_the_app_in_front_at_start():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=_FakeBackend()),
        gemini_client=_FakeClient(['{"status":"continue","action":"click","x":1,"y":2,"thought":"c"}',
                                   '{"status":"done","thought":"d"}']))
    raised = []
    o._running_app_names = lambda: ["Terminal", "TextEdit"]
    o._ensure_focus = lambda target: raised.append(target)
    o._frontmost_app_name = lambda: "Terminal"
    o.run("Open TextEdit and write hello")
    assert raised == ["TextEdit", "TextEdit"], raised


def test_a_run_that_cannot_be_recorded_says_so():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"

    class _Broken(_SpyRecorder):
        def start(self):
            raise RuntimeError("front app unreadable")
    o = AgentOrchestrator(api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
                          gemini_client=_FakeClient(['{"status":"done","thought":"done"}']))
    out = o.run("do thing", recorder=_Broken())
    assert out == "done (not recorded: front app unreadable)", out


def test_the_recorder_reads_the_front_app_in_process_never_through_system_events():
    from anyuse.context import accessibility as AXM
    from anyuse.memory import window_manager as WM

    class _App:
        def __init__(self, name, bid, pid):
            self._n, self._b, self._p = name, bid, pid

        def localizedName(self):
            return self._n

        def bundleIdentifier(self):
            return self._b

        def processIdentifier(self):
            return self._p
    apps = [_App("Terminal", "com.apple.Terminal", 10), _App("TextEdit", "com.apple.TextEdit", 20)]

    def no_osascript(*a, **k):
        raise AssertionError("osascript / System Events must not be asked")
    saved = (AXM.frontmost_reading, AXM.focused_window_info, WM._running_app, WM.subprocess.run)
    try:
        AXM.frontmost_reading = lambda: {"bundle_id": "com.apple.TextEdit", "front_source": "ax"}
        AXM.focused_window_info = lambda pid: {"pid": pid, "title": "Untitled",
                                               "frame": ((10.0, 25.0), (800.0, 600.0)), "focus": None}
        WM._running_app = lambda name=None, bundle_id=None: next(
            (a for a in apps if a.bundleIdentifier() == bundle_id or a.localizedName() == name), None)
        WM.subprocess.run = no_osascript
        wm = WM.WindowManager()
        assert wm.get_frontmost_app() == "TextEdit"
        st = wm.get_window_state("TextEdit")
        assert st.app == "TextEdit" and st.bounds == {"x": 10, "y": 25, "width": 800, "height": 600}
        AXM.frontmost_reading = lambda: {"bundle_id": None, "front_source": None}
        assert wm.get_frontmost_app() == ""            # unreadable: an empty answer, not a stall
    finally:
        AXM.frontmost_reading, AXM.focused_window_info, WM._running_app, WM.subprocess.run = saved


def test_the_credential_helpers_print_nothing_for_a_task_without_2fa():
    import contextlib
    import io
    from anyuse.credentials.two_factor import TOTPGenerator
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        TOTPGenerator()
    assert buf.getvalue() == "", buf.getvalue()


class _FakePG:
    """pyautogui stand-in: records calls; the pointer starts where it is told."""
    def __init__(self, pos=(500, 400), size=(1440, 900)):
        self.FAILSAFE = True
        self.pos, self._size, self.calls = pos, size, []

    def position(self):
        return self.pos

    def size(self):
        return self._size

    def moveTo(self, x, y, **k):
        self.calls.append(("moveTo", x, y, self.FAILSAFE))
        self.pos = (x, y)

    def click(self, **k):
        self.calls.append(("click", k))

    def typewrite(self, text, **k):
        self.calls.append(("typewrite", text))

    def press(self, key):
        self.calls.append(("press", key))


def _no_quartz(text):
    """``context.keyboard.type_unicode`` where Quartz cannot be imported (not macOS)."""
    return {"typed": False, "chars": 0, "stuck_modifiers": [], "error": "ImportError: no Quartz"}


def _pg_backend(pg, typer=_no_quartz):
    from anyuse.execution.actions import _Backend

    class _B(_Backend):
        pg = None          # a plain attribute instead of the interlocked property
    b = _B()
    b.pg = pg
    b._typer = typer       # never the real Quartz typing in a test
    return b


def test_a_type_without_a_point_types_into_the_focus_and_never_clicks():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=be),
        gemini_client=_FakeClient([
            '{"status":"continue","action":"type","text":"Hello from AnyUse","thought":"the field has focus"}',
            '{"status":"done","thought":"typed"}',
        ]),
    )
    assert o.run("type it") == "typed"
    typed = [k for name, k in be.calls if name == "type_text_at"]
    assert typed == [{"x": None, "y": None, "text": "Hello from AnyUse", "press_enter": False}], be.calls
    pg = _FakePG()
    _pg_backend(pg).type_text_at(None, None, "hi")
    assert pg.calls == [("typewrite", "hi")]                     # no click at (0, 0)


def test_a_key_combination_needs_no_point():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=be),
        gemini_client=_FakeClient([
            '{"status":"continue","action":"key","keys":"command+n","thought":"new document"}',
            '{"status":"done","thought":"opened"}',
        ]),
    )
    assert o.run("new document") == "opened"
    keys = [k for name, k in be.calls if name == "key_combination"]
    assert keys == [{"keys": "command+n"}], be.calls


def test_a_click_without_a_point_goes_back_to_the_model_and_is_never_sent():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=be),
        gemini_client=_FakeClient([
            '{"status":"continue","action":"click","thought":"click the button"}',
            '{"status":"continue","action":"click","x":100,"y":200,"thought":"click it at its place"}',
            '{"status":"done","thought":"done"}',
        ]),
    )
    assert o.run("click the button") == "done"
    clicks = [k for name, k in be.calls if name == "click_at"]
    assert clicks == [{"x": 100, "y": 200, "button": "left"}], be.calls
    assert "needs x and y" in o.history[0]["actuation"]["error"]
    assert "[not done: click needs x and y" in o._build_prompt("click the button")


def test_the_pointer_leaves_a_failsafe_corner_once_when_a_run_starts():
    pg = _FakePG(pos=(0, 0))
    _pg_backend(pg).leave_failsafe_corner()
    assert pg.calls == [("moveTo", 720, 450, False)] and pg.FAILSAFE is True
    pg = _FakePG(pos=(300, 300))
    _pg_backend(pg).leave_failsafe_corner()
    assert pg.calls == []                                          # not in a corner: untouched

    class _Spy(_FakeBackend):
        def leave_failsafe_corner(self, **k):
            self.calls.append(("leave_failsafe_corner", k))
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _Spy()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=be),
        gemini_client=_FakeClient(['{"status":"continue","action":"click","x":5,"y":6,"thought":"a"}',
                                   '{"status":"done","thought":"ok"}']),
    )
    assert o.run("click") == "ok"
    names = [name for name, _k in be.calls]
    assert names[0] == "leave_failsafe_corner" and names.count("leave_failsafe_corner") == 1, names


# ---- typing is layout-independent --------------------------------------------------------------
def test_n11_text_is_typed_as_unicode_events_with_the_exact_characters():
    # keycode typing (pyautogui.typewrite) swaps y and z on a German layout and drops @, ä, ß
    pg = _FakePG()
    got = []
    b = _pg_backend(pg, typer=lambda text: got.append(text) or {"typed": True, "chars": len(text)})
    b.type_text_at(text="yz äöüß @ Zürich")
    assert got == ["yz äöüß @ Zürich"] and pg.calls == []          # no keycode typing, no click
    b.type_text_at(x=10, y=20, text="AnyUse", press_enter=True)
    assert got[-1] == "AnyUse" and pg.calls == [("click", {"x": 10, "y": 20}), ("press", "enter")]
    # the interlock refuses: an error, and nothing typed by keycode instead
    pg = _FakePG()
    b = _pg_backend(pg, typer=lambda t: {"typed": False, "chars": 0, "refused": True,
                                         "error": "a person is at the console"})
    try:
        b.type_text_at(text="x")
        raise AssertionError("a refused typing must raise")
    except RuntimeError as e:
        assert "person" in str(e)
    # stopped part-way: an error naming how far it got, never a retype by keycode
    b = _pg_backend(pg, typer=lambda t: {"typed": False, "chars": 2, "error": "CGEventPost failed"})
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    r = ActionExecutor(backend=b).execute("type_text_at", {"text": "abcde"})
    assert r["success"] is False and "after 2 of 5" in r["error"] and pg.calls == [], r


def test_n11_type_unicode_posts_each_character_with_empty_modifiers():
    from anyuse.context import keyboard as K
    posted = []

    class Q:
        kCGEventSourceStateHIDSystemState = kCGHIDEventTap = 0

        @staticmethod
        def CGEventSourceCreate(_s):
            return "src"

        @staticmethod
        def CGEventCreateKeyboardEvent(_src, code, down):
            return {"code": code, "down": down}

        @staticmethod
        def CGEventKeyboardSetUnicodeString(ev, n, ch):
            ev["text"], ev["n"] = ch, n

        @staticmethod
        def CGEventSetFlags(ev, flags):
            ev["flags"] = flags

        @staticmethod
        def CGEventPost(_tap, ev):
            if ev.get("text") == "!":
                raise RuntimeError("post failed")
            posted.append(dict(ev))
    saved = (K._quartz, K._interlock, K.stuck_modifiers)
    K._quartz, K._interlock, K.stuck_modifiers = (lambda: Q), (lambda where: None), (lambda: [])
    try:
        out = K.type_unicode("zä@ß", per_char=0)
        assert out["typed"] and out["chars"] == 4
        assert [e["text"] for e in posted if e["down"]] == ["z", "ä", "@", "ß"]
        assert all(e["flags"] == 0 and e["code"] == 0 for e in posted) and len(posted) == 8
        out = K.type_unicode("ab!c", per_char=0)                  # fails at the third character
        assert not out["typed"] and out["chars"] == 2 and "post failed" in out["error"]
    finally:
        K._quartz, K._interlock, K.stuck_modifiers = saved


# ---- learned workflows use the executor's action names -------------------------------------------
class _Rec:
    """The orchestrator's view of a StateGraphRecorder."""

    def __init__(self):
        self.actions, self.saved, self.discarded = [], False, False

    def start(self):
        pass

    def record_action(self, **k):
        self.actions.append(k)

    def stop(self):
        pass

    def save(self):
        self.saved = True

    def discard(self):
        self.discarded = True


def test_n12_the_agents_verbs_of_older_workflows_run_as_executor_actions():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    ex = ActionExecutor(backend=be)
    for verb, params in (("key", {"keys": "command+n"}), ("click", {"x": 1, "y": 2}),
                         ("type", {"x": None, "y": None, "text": "a"}), ("scroll", {"x": 1, "y": 2})):
        assert ex.execute(verb, params)["success"], verb
    assert [c[0] for c in be.calls] == ["key_combination", "click_at", "type_text_at", "scroll_at"]
    assert ex.execute("frobnicate", {})["error"] == "unknown action: frobnicate"


def test_n12_a_run_records_the_executor_action_not_the_agents_verb():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    rec = _Rec()
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=_FakeBackend()),
        gemini_client=_FakeClient([
            '{"status":"continue","action":"key","keys":"command+n","thought":"new game"}',
            '{"status":"continue","action":"click","x":5,"y":6,"thought":"Starten"}',
            '{"status":"done","thought":"started"}',
        ]),
    )
    assert o.run("new game", recorder=rec) == "started"
    assert [a["action"] for a in rec.actions] == ["key_combination", "click_at"]
    assert rec.actions[0]["args"] == {"keys": "command+n"} and rec.saved


def test_n12_the_state_graph_recorder_keeps_a_key_chord_and_a_pointless_type():
    # the learned workflow of the Chess run: cmd+n had no implementation (no transition stored,
    # the replay skipped it) and a type into the focus has no point to look up
    from anyuse.memory import recorder as R
    rec = R.StateGraphRecorder.__new__(R.StateGraphRecorder)
    legacy = []

    class _Legacy:
        def record_action(self, **k):
            legacy.append(k)

    class _NoPointTree:
        def find_at(self, x, y):
            raise AssertionError("an action without a point is never looked up at a point")
    captured = []
    rec._refresh_app_context = lambda: None
    rec._take_snapshot = lambda: None
    rec._legacy_recorder = _Legacy()
    rec._previous_state_id, rec._states_discovered, rec._state_path = None, {}, []
    rec._ax_tree, rec._current_app = _NoPointTree(), "Schach"
    rec._current_pid, rec._steps_meta = None, []
    rec._get_window_bounds = lambda: {"x": 0, "y": 0, "width": 800, "height": 600}
    rec._template_params, rec._template_steps = [], []
    rec._capture_after_state = lambda intent, impls, before, **kwargs: captured.append((intent, impls))
    rec.record_action("key_combination", {"keys": "command+n"})
    rec.record_action("type_text_at", {"x": None, "y": None, "text": "Hallo"})
    (key_intent, key_impls), (type_intent, type_impls) = captured
    assert [(i.modality, i.command) for i in key_impls] == [
        ("coordinate", {"action": "key_combination", "keys": "command+n"})]
    assert [(i.modality, i.command) for i in type_impls] == [
        ("coordinate", {"action": "type_text_at", "x": None, "y": None, "text": "Hallo", "press_enter": False})]
    assert key_intent.input_data == "command+n" and type_intent.input_data == "Hallo"
    assert [k["action"] for k in legacy] == ["key_combination", "type_text_at"]
    # replayed through the coordinate rung: the chord, and the text into the focus with no click
    from anyuse.execution.executor import StateGraphExecutor
    from anyuse.execution.plan_types import PlannedStep
    from anyuse.execution.deterministic import _normalize_coordinate_command
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    be = _FakeBackend()
    ex = StateGraphExecutor.__new__(StateGraphExecutor)
    ex._action_executor = ActionExecutor(backend=be)
    for impl in (key_impls[0], type_impls[0]):
        cmd = _normalize_coordinate_command(impl.command)
        step = PlannedStep(intent="x", source="workflow", target_element="", modality="coordinate",
                           command=cmd, input_data=cmd.get("text"))
        assert ex._execute_coordinate(step)["success"], cmd
    assert be.calls == [("key_combination", {"keys": "command+n"}),
                        ("type_text_at", {"x": None, "y": None, "text": "Hallo", "press_enter": False})]

# ---- a finished run is saved only after the goal check ------------------------------------------
def _done_orch(goal_check):
    o = AgentOrchestrator(
        api_key="x", pixel_scale=1.0, screenshot_capture=_FakeShot(),
        action_executor=ActionExecutor(backend=_FakeBackend()),
        gemini_client=_FakeClient(['{"status":"continue","action":"click","x":5,"y":6,"thought":"a"}',
                                   '{"status":"done","thought":"moved e2-e4"}']),
    )
    o.goal_check = goal_check
    return o


def test_n15_the_goal_check_gates_saving_a_finished_run():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    asked = []
    rec = _Rec()
    out = _done_orch(lambda task, msg: (asked.append((task, msg)) or (False, "the board shows no move"))).run(
        "move e2-e4", recorder=rec)
    assert asked == [("move e2-e4", "moved e2-e4")]
    assert rec.discarded and not rec.saved and "not saved as a workflow: the board shows no move" in out
    rec = _Rec()
    _done_orch(lambda task, msg: (True, "seen")).run("move e2-e4", recorder=rec)
    assert rec.saved and not rec.discarded

    def boom(task, msg):
        raise RuntimeError("verifier down")
    rec = _Rec()
    out = _done_orch(boom).run("move e2-e4", recorder=rec)
    assert rec.discarded and not rec.saved and "verifier down" in out
    # without a recorder nothing is asked (nothing would be saved)
    asked.clear()
    _done_orch(lambda task, msg: asked.append(1) or (False, "x")).run("move e2-e4")
    assert asked == []

def test_prompt_denial_precedes_fresh_capture_and_discards_ui_recording():
    from unittest.mock import patch
    from anyuse.execution import system_prompt_policy as SP
    events, rec = [], _SpyRecorder()
    o = AgentOrchestrator(adapter=object(), screenshot_capture=_FakeShot())

    def resolve(where, *, task_context, decision_limits=None, decision_deadline_monotonic=None):
        assert task_context == "inspect the app"
        events.append("resolve")
        return {"attempted": True, "reobserve_required": True, "next_action": "observe"}

    def capture():
        events.append("capture")
        return b"fresh screen"

    def decide(task, png):
        assert png == b"fresh screen"
        events.append("decide")
        return {"status": "done", "thought": "fresh decision"}

    with patch.object(SP, "resolve_configured", resolve), patch.object(o, "_capture", capture), \
            patch.object(o, "_decide", decide), patch.object(o.executor, "execute", return_value={"success": True}):
        result = o.run("inspect the app", recorder=rec)
    assert result.startswith("fresh decision") and "invalidated the UI recording" in result
    assert events == ["resolve", "capture", "decide"]
    assert [name for name, _ in rec.events] == ["start", "discard"]


def test_uncertain_prompt_attempt_captures_then_stops_without_new_action():
    from unittest.mock import patch
    from anyuse.execution import system_prompt_policy as SP
    o = AgentOrchestrator(adapter=object(), screenshot_capture=_FakeShot())
    resolution = {"attempted": True, "reobserve_required": True, "next_action": "stop", "reason": "uncertain press"}
    with patch.object(SP, "resolve_configured", return_value=resolution), \
            patch.object(o, "_capture", return_value=b"fresh screen") as capture, \
            patch.object(o, "_decide", side_effect=AssertionError("must not plan another action")), \
            patch.object(o.executor, "execute", return_value={"success": True}) as execute:
        result = o.run("inspect the app")
    assert "manual handling: uncertain press" in result and capture.call_count == 1
    assert execute.call_args_list[0].args == ("leave_failsafe_corner", {}) and execute.call_count == 1


def test_manual_prompt_handoff_does_not_capture_or_plan():
    from unittest.mock import patch
    from anyuse.execution import system_prompt_policy as SP
    o = AgentOrchestrator(adapter=object(), screenshot_capture=_FakeShot())
    resolution = {"attempted": False, "reobserve_required": False, "next_action": "stop", "reason": "grant stays manual"}
    with patch.object(SP, "resolve_configured", return_value=resolution), \
            patch.object(o, "_capture", side_effect=AssertionError("manual handoff must stop")), \
            patch.object(o, "_decide", side_effect=AssertionError("manual handoff must stop")), \
            patch.object(o.executor, "execute", return_value={"success": True}):
        result = o.run("inspect the app")
    assert "manual handling: grant stays manual" in result and o.history == []


def test_orchestrator_prompt_context_uses_existing_redaction_without_reading_credentials():
    from types import SimpleNamespace
    from unittest.mock import patch
    from anyuse.execution import secret_refs as SR
    from anyuse.execution import system_prompt_policy as SP
    redactor = SR.Redactor()
    redactor.add("KNOWN_SECRET", "${secret:AUTH}")
    o = AgentOrchestrator(adapter=object(), screenshot_capture=_FakeShot(), credentials=SimpleNamespace(redactor=redactor))

    def resolve(where, *, task_context, decision_limits=None, decision_deadline_monotonic=None):
        assert task_context == "Open Reader using ${secret:AUTH}"
        return {"attempted": False, "reobserve_required": False, "next_action": "unchanged"}

    with patch.object(SP, "resolve_configured", resolve), \
            patch.object(o, "_decide", return_value={"status": "done", "thought": "done"}), \
            patch.object(o.executor, "execute", return_value={"success": True}):
        assert o.run("Open Reader using KNOWN_SECRET") == "done"


def test_nested_rea_uses_prepared_boundary_and_failures_remain_terminal():
    from unittest.mock import patch
    from anyuse.execution import system_prompt_policy as SP
    for native_ok in (True, False):
        events, calls = [], []

        class Store:
            def list_tools(self):
                return [{"name": "rea.test.pause", "description": "Pause owned video", "catalog_sha256": "a" * 64}]
            def execute(self, name, inputs, *, expected_catalog_sha256):
                assert expected_catalog_sha256 == "a" * 64
                events.append("native")
                calls.append((name, inputs))
                return {"ok": native_ok, "status": "completed" if native_ok else "uncertain",
                        "reason": "completed" if native_ok else "restoration failed"}

        o = AgentOrchestrator(adapter=object(), screenshot_capture=_FakeShot(), rea_store=Store(), auto_confirm=True)
        decisions = iter([
            {"status": "continue", "action": "rea_action", "name": "rea.test.pause", "inputs": {}},
            {"status": "done", "thought": "paused"},
        ])

        def resolve(where, *, task_context, decision_limits=None, decision_deadline_monotonic=None):
            assert task_context == "Pause owned video"
            events.append("resolve")
            return {"attempted": False, "reobserve_required": False, "next_action": "unchanged"}

        def capture():
            events.append("capture")
            return b"fresh screen"

        def decide(task, png):
            events.append("decide")
            return next(decisions)

        with patch.dict(os.environ, {"ANYUSE_DRIVE_ENABLED": "1"}), \
                patch.object(SP, "resolve_configured", resolve), patch.object(o, "_capture", capture), \
                patch.object(o, "_decide", decide), patch.object(o.executor, "execute", return_value={"success": True}):
            result = o.run("Pause owned video")
        assert calls == [("rea.test.pause", {})]
        if native_ok:
            assert result.startswith("paused")
            assert events == ["resolve", "capture", "decide", "native", "resolve", "capture", "decide"]
        else:
            assert "REA stopped: restoration failed" in result
            assert events == ["resolve", "capture", "decide", "native"]


def test_orchestrator_accounts_for_prompt_model_and_checks_limits_before_ordinary_decision():
    from unittest.mock import patch
    from anyuse.execution import system_prompt_policy as SP
    resolution = {"status": "action_attempted", "attempted": True, "reobserve_required": True, "next_action": "observe",
                  "decision": {"model_calls": 1, "cost_usd": 0.02, "budget_charged_usd": 0.03}}
    for max_turns in (1, 3):
        o = AgentOrchestrator(adapter=object(), screenshot_capture=_FakeShot(), max_turns=max_turns)

        def resolve(where, *, task_context, decision_limits, decision_deadline_monotonic):
            assert task_context == "Inspect Reader" and decision_limits["remaining_calls"] == max_turns
            assert decision_limits["timeout_s"] > 0 and decision_deadline_monotonic > 0
            return resolution

        with patch.dict(os.environ, {SP.MODE_ENV: "llm"}), patch.object(SP, "resolve_configured", resolve), \
                patch.object(o, "_decide", return_value={"status": "done", "thought": "done"}) as decide, \
                patch.object(o.executor, "execute", return_value={"success": True}):
            result = o.run("Inspect Reader")
        assert o.model_calls == 1 and o.system_prompt_usage["cost_usd"] == 0.02
        assert o.system_prompt_usage["budget_charged_usd"] == 0.03
        if max_turns == 1:
            assert "exhausted" in result and decide.call_count == 0
        else:
            assert result == "done" and decide.call_count == 1


def test_boundary_count_and_deadline_block_backend_dispatch():
    from anyuse.core.restriction import ComplianceChecker, RestrictionPolicy, ExecutionControl
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    for boundary in ({"action_rules": {"click": "exclude"}}, {"action_rules": {"click_at": "guard"}}, {"max_actions": 0}):
        backend = _FakeBackend()
        executor = ActionExecutor(backend=backend)
        executor.set_compliance_checker(ComplianceChecker(RestrictionPolicy.from_boundary(boundary)))
        assert executor.execute("click_at", {"x": 1, "y": 2})["blocked"]
        assert backend.calls == []
    backend = _FakeBackend()
    executor = ActionExecutor(backend=backend)
    control = ExecutionControl(2, clock=lambda: now[0])
    now = [0]
    checker = ComplianceChecker(RestrictionPolicy.from_boundary({"max_actions": 2}), control=control)
    executor.set_compliance_checker(checker)
    assert executor.execute("click_at", {"x": 1, "y": 2})["success"]
    now[0] = 2
    assert executor.execute("click_at", {"x": 1, "y": 2})["blocked"]
    assert len(backend.calls) == checker.actions_executed == 1


def test_focus_is_resolved_before_capture_and_stale_decision_is_discarded():
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"
    events = []
    front = [{"pid": 1, "name": "Terminal"}]
    backend = _FakeBackend()
    o = AgentOrchestrator(api_key="x", max_turns=2, pixel_scale=1,
        action_executor=ActionExecutor(backend=backend), screenshot_capture=_FakeShot(),
        gemini_client=_FakeClient(["{}"]), front_reader=lambda: dict(front[0]))
    o._running_app_names = lambda: ["TextEdit"]
    def focus(target):
        events.append("focus")
        front[0] = {"pid": 2, "name": target}
    def capture():
        events.append("capture")
        assert front[0]["name"] == "TextEdit"
        return b"PNGBYTES"
    def decide(*_):
        events.append("decide")
        if len(o.history) == 0:
            front[0] = {"pid": 3, "name": "Other"}
            return {"action": "click", "x": 1, "y": 2}
        return {"status": "done", "thought": "done"}
    o._ensure_focus, o._capture, o._decide = focus, capture, decide
    assert o.run("Open TextEdit") == "done"
    assert events == ["focus", "capture", "decide"] * 2
    assert backend.calls == []
    assert "decision discarded" in o.history[0]["actuation"]["error"]


def test_drag_requires_both_finite_destination_coordinates():
    o = AgentOrchestrator(adapter=object(), screenshot_capture=_FakeShot(), pixel_scale=1)
    for dest in ({}, {"x2": 3}, {"y2": 4}, {"x2": None, "y2": 4},
                 {"x2": float("nan"), "y2": 4}, {"x2": 3, "y2": float("inf")},
                 {"x2": True, "y2": 4}, {"x2": "3", "y2": 4}):
        action, _ = o._to_action("drag", dict(x=1, y=2, **dest))
        assert action is None, dest
    assert o._to_action("drag", {"x": 1, "y": 2, "x2": 0, "y2": 0}) == (
        "drag_to", {"x1": 1, "y1": 2, "x2": 0, "y2": 0})
    assert o._to_action("drag", {"x": 1, "y": 2, "to_x": 3, "to_y": 4})[1]["x2"] == 3


if __name__ == "__main__":
    import inspect, sys
    mod = sys.modules[__name__]
    fns = [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]
    p = f = 0
    for n, fn in fns:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:160])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
