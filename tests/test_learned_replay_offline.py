"""What computer use learns replays without a model, offline.

A fake desktop (three apps by pid: the launching Terminal, Spotlight, TextEdit) answers the AX
reads, the live front-app reading and the actuator. A learning run of "open TextEdit with
Spotlight and type a text" is recorded through the real AgentOrchestrator and StateGraphRecorder,
saved through the real storages, and replayed through the real ExecutionOrchestrator replay and
StateGraphExecutor: three steps, 0 model calls. Also the single fixes:

* the recorder reads the front app live; ``get_frontmost_app`` (untickled NSWorkspace) is
  never asked.
* name and bundle id apart; ``required_app`` is a name, ``required_bundle_id`` its bundle
  id, which the clean room opens; a dict app does not raise.
* a missing app guide is created; a Selector in ``ax_path`` serialises; the guide write is
  atomic, and so are the workflow's and the template's; the template keeps its own graph.
* the after-state is read after the action ran; no transition is a self-loop.
* a drag retains its coordinate gesture and both endpoint identities for replay.
* AppleScript runs only against an app that already granted Automation (never asked), and
  the planner offers it only then.
* a template replay counts as a run, reports its model calls and who served it, and the
  planner gets the description.
* the goal check fails a text with an extra blank line; the computer-use loop waits for a
  launch to land.

No desktop, no model, no network.
"""
import asyncio
import json
import logging
import os
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory

_PKGROOT = TemporaryDirectory(prefix="anyuse-learned-replay-")
Path(_PKGROOT.name, "anyuse").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
sys.path.insert(0, _PKGROOT.name)

import pytest  # noqa: E402

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"   # fakes only

# the legacy recorder's anchor capture imports pyautogui at its module top: a stand-in
_matcher = types.ModuleType("anyuse.memory.matcher")


class _NoAnchors:
    def __init__(self, *a, **k):
        pass

    def capture_target(self, *a, **k):
        return None


_matcher.AnchorCapture = _NoAnchors
sys.modules.setdefault("anyuse.memory.matcher", _matcher)

from anyuse.context import macos as M  # noqa: E402
from anyuse.context.accessibility import AccessibilityTree, AXElementDesc, AXSnapshot, Selector  # noqa: E402
from anyuse.execution.actions import ActionExecutor  # noqa: E402
from anyuse.execution.agent_orchestrator import AgentOrchestrator  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.orchestrator import ExecutionOrchestrator, OrchestratorResult  # noqa: E402
from anyuse.execution.plan_types import PlannedStep  # noqa: E402
from anyuse.guides import storage as GS  # noqa: E402
from anyuse.guides.state_graph import StateGraph, UIElement, UIState  # noqa: E402
from anyuse.memory import recorder as R  # noqa: E402
from anyuse.memory.models import WorkflowTemplate  # noqa: E402
from anyuse.memory.storage import WorkflowStorage  # noqa: E402

StateGraphExecutor._effect_sleep = staticmethod(lambda s: None)
StateGraphExecutor._app_sleep = staticmethod(lambda s: None)
R.StateGraphRecorder.FRONT_RETRY_S = ()

TERMINAL, SPOTLIGHT, TEXTEDIT = 101, 102, 103
BUNDLES = {TERMINAL: "com.apple.Terminal", SPOTLIGHT: "com.apple.Spotlight", TEXTEDIT: "com.apple.TextEdit"}
NAMES = {TERMINAL: "Terminal", SPOTLIGHT: "Spotlight", TEXTEDIT: "TextEdit"}


# ---------------------------------------------------------------------------
# the fake desktop
# ---------------------------------------------------------------------------

class Desktop:
    """Apps by pid, each a list of AX elements; the actuator's keys change them."""

    def __init__(self):
        self.front = TERMINAL
        self.running = {TERMINAL, SPOTLIGHT}
        self.els = {
            TERMINAL: [AXElementDesc(role="AXWindow", title="anyuse — zsh"),
                       AXElementDesc(role="AXTextArea", value="$ anyuse run …")],
            SPOTLIGHT: [AXElementDesc(role="AXTextField", ax_description="Spotlight-Suche", value="")],
            TEXTEDIT: [AXElementDesc(role="AXWindow", title="Ohne Titel"),
                       AXElementDesc(role="AXButton", ax_description="schließen"),
                       AXElementDesc(role="AXTextArea", identifier="First Text View", value="")],
        }
        self.typed = []
        self.read_front_app = 0     # get_frontmost_app (the stale read) asked

    def snapshot(self, pid):
        return AXSnapshot(elements=[AXElementDesc(**vars(e)) for e in self.els.get(pid, [])],
                          app_name=NAMES.get(pid, ""))

    def front_app(self):
        return {"name": NAMES[self.front], "app": NAMES[self.front], "bundle_id": BUNDLES[self.front],
                "pid": self.front, "has_window": self.front != SPOTLIGHT, "front_source": "ax"}

    def text(self, pid):
        return next(e.value for e in self.els[pid] if e.role == "AXTextArea")

    def _set_text(self, pid, value):
        next(e for e in self.els[pid] if e.role == "AXTextArea").value = value

    # the actuator's backend
    def key_combination(self, keys):
        self.typed.append(("key", keys))
        if keys == "cmd+space":
            self.front = SPOTLIGHT
        elif keys in ("enter", "return") and self.front == TEXTEDIT:
            self._set_text(TEXTEDIT, "\n" + self.text(TEXTEDIT))

    def type_text_at(self, x=None, y=None, text="", press_enter=False):
        self.typed.append(("type", text, press_enter))
        if self.front == SPOTLIGHT:
            self.els[SPOTLIGHT][0].value = text
            if press_enter and text == "TextEdit":
                self.els[SPOTLIGHT][0].value = ""
                self.running.add(TEXTEDIT)
                self.front = TEXTEDIT
        elif self.front == TEXTEDIT:
            self._set_text(TEXTEDIT, self.text(TEXTEDIT) + text)


class FakeTree(AccessibilityTree):
    """An AccessibilityTree bound by pid whose reads come from the fake desktop (its ``diff`` is
    the real one)."""
    desktop = None

    def snapshot(self, *a, **k):
        return self.desktop.snapshot(self._pid)

    def find_at(self, x, y):
        return None


class FakeContext:
    def __init__(self, desktop):
        self.d = desktop

    def get_front_app_live(self):
        return self.d.front_app()

    def get_frontmost_app(self, *a, **k):
        self.d.read_front_app += 1
        raise AssertionError("the stale NSWorkspace read is never asked (N24)")

    def get_browser_url(self, *a, **k):
        return None


class FakeModel:
    """The learning run's model: one JSON decision per screenshot."""

    def __init__(self, decisions):
        self.models = self
        self.decisions = [json.dumps(d) for d in decisions]
        self.calls = 0

    def generate_content(self, **_kw):
        self.calls += 1
        return types.SimpleNamespace(text=self.decisions[min(self.calls - 1, len(self.decisions) - 1)])


class Shot:
    def capture(self):
        return b"PNG"


LEARNING_RUN = [
    {"status": "continue", "action": "key", "keys": "cmd+space", "thought": "open Spotlight"},
    {"status": "continue", "action": "type", "text": "TextEdit", "press_enter": True, "thought": "launch TextEdit"},
    {"status": "continue", "action": "type", "text": "Hello from AnyUse", "thought": "type the text"},
    {"status": "done", "thought": "TextEdit shows Hello from AnyUse"},
]


def _learn(tmp, desktop, decisions=LEARNING_RUN):
    """One recorded computer-use run over the fake desktop; returns (recorder, storage, guides, orch)."""
    FakeTree.desktop = desktop
    saved = (R.AccessibilityTree, R.AX_AVAILABLE)
    R.AccessibilityTree, R.AX_AVAILABLE = FakeTree, True
    try:
        storage = WorkflowStorage(base_dir=tmp / "workflows")
        guides = GS.GuideStorage(base_dir=tmp / "guides")
        rec = R.StateGraphRecorder("open_textedit", "Open TextEdit and type the text Hello from AnyUse",
                                   storage=storage, guide_storage=guides)
        rec._macos_ctx = FakeContext(desktop)
        orch = AgentOrchestrator(api_key="x", pixel_scale=1.0, screenshot_capture=Shot(),
                                 action_executor=ActionExecutor(backend=desktop),
                                 gemini_client=FakeModel(decisions), front_reader=desktop.front_app)
        orch._sleep = lambda s: None
        orch.goal_check = lambda task, msg: (True, "checked")
        out = orch.run("Open TextEdit and type the text Hello from AnyUse", recorder=rec)
        assert out == "TextEdit shows Hello from AnyUse", out
        return rec, storage, guides, orch
    finally:
        R.AccessibilityTree, R.AX_AVAILABLE = saved


def _template(storage):
    (wf,) = storage.list_all()
    return wf, storage.get_template(wf.id)


# ---------------------------------------------------------------------------
# the whole loop: learn, save, replay without a model
# ---------------------------------------------------------------------------

def test_a_learned_textedit_run_replays_without_a_model(tmp_path):
    desktop = Desktop()
    rec, storage, guides, orch = _learn(tmp_path, desktop)
    assert desktop.text(TEXTEDIT) == "Hello from AnyUse"
    assert desktop.read_front_app == 0
    wf, tpl = _template(storage)
    # a name, not the front-app dict, and the bundle id beside it
    assert tpl.required_app == "TextEdit" and tpl.required_bundle_id == "com.apple.TextEdit"
    # three apps, three transitions, none a self-loop; the typing step leaves an alias
    trans = rec.transitions_discovered
    assert [(t.from_state.split("_")[0], t.to_state.split("_")[0]) for t in trans] == [
        ("terminal", "spotlight"), ("spotlight", "textedit"), ("textedit", "textedit")]
    assert all(t.from_state != t.to_state for t in trans)
    assert trans[2].to_state.startswith(trans[1].to_state + "~")
    assert tpl.state_path == [trans[0].from_state, trans[0].to_state, trans[1].to_state, trans[2].to_state]
    # the launching terminal is no precondition: no elements, no baseline
    start = rec.states_discovered[trans[0].from_state]
    assert start.elements == [] and not start.fingerprint.required_elements
    # a step that brought another app forward declares no diff; the typed one does
    assert "expect" not in trans[0].implementations[0].command
    assert "expect" not in trans[1].implementations[0].command
    assert trans[2].implementations[0].command["expect"]["permitted"]
    # the launch's wait became the step player's delay too
    assert wf.steps[1].delay_after >= 0.2
    # the guide did not exist; now it does, named and identified as the OS reports the app
    guide = GS.GuideStorage(base_dir=tmp_path / "guides").get_by_app("TextEdit")
    assert guide is not None and guide.bundle_id == "com.apple.TextEdit"
    assert guide.identity_provenance == "os_app_name"
    assert len(guide.get_state_graph().transitions) == 3
    assert len(tpl.recorded_graph["transitions"]) == 3

    # the replay: a fresh desktop, the Terminal in front, TextEdit not running
    replay_desktop = Desktop()
    FakeTree.desktop = replay_desktop
    ex = StateGraphExecutor(action_executor=ActionExecutor(backend=replay_desktop),
                            ax_tree=FakeTree(TERMINAL), drift_gate_enabled=True)
    ex.front_reader = replay_desktop.front_app
    ex.app_namer = lambda pid: NAMES.get(pid, "")
    orch2 = object.__new__(ExecutionOrchestrator)
    res = asyncio.run(orch2.replay_deterministic(tpl, state_graph=guide.get_state_graph(), executor=ex))
    assert res["success"] is True and res["llm_calls"] == 0, res
    assert replay_desktop.typed == [("key", "cmd+space"), ("type", "TextEdit", True),
                                    ("type", "Hello from AnyUse", False)]
    assert replay_desktop.text(TEXTEDIT) == "Hello from AnyUse"
    assert ex._ax_tree._pid == TEXTEDIT      # the tree followed the recorded apps


def test_a_replay_whose_launch_never_lands_stops_instead_of_typing_into_spotlight(tmp_path):
    desktop = Desktop()
    _rec, storage, guides, _o = _learn(tmp_path, desktop)
    _wf, tpl = _template(storage)
    stuck = Desktop()
    stuck.type_text_at = lambda x=None, y=None, text="", press_enter=False: stuck.typed.append(("type", text))
    FakeTree.desktop = stuck
    ex = StateGraphExecutor(action_executor=ActionExecutor(backend=stuck), ax_tree=FakeTree(TERMINAL),
                            drift_gate_enabled=True)
    ex.front_reader, ex.app_namer = stuck.front_app, (lambda pid: NAMES.get(pid, ""))
    res = asyncio.run(object.__new__(ExecutionOrchestrator).replay_deterministic(tpl, executor=ex))
    assert res["success"] is False and res.get("outcome_kind") == "uncertain_completion", res
    assert stuck.typed == [("key", "cmd+space"), ("type", "TextEdit")]   # "Hello" never typed


# ---------------------------------------------------------------------------
# the front app, its name and its bundle id
# ---------------------------------------------------------------------------

def test_n24_the_live_front_reading_shapes_name_bundle_and_pid():
    ctx = M.MacOSContext()
    ctx.front_reader = lambda: {"app": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": 7,
                                "window_title": "Ohne Titel", "front_source": "window_server"}
    assert ctx.get_front_app_live() == {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": 7,
                                        "window_title": "Ohne Titel", "front_source": "window_server"}
    assert ctx.get_frontmost_pid(live=True) == 7
    ctx.front_reader = lambda: {"app": "", "bundle_id": "", "pid": None, "front_source": ""}
    assert ctx.get_front_app_live() is None and ctx.get_frontmost_pid(live=True) is None


def test_n24_the_recorder_keeps_the_last_app_it_acts_in_not_the_terminal():
    d = Desktop()
    rec = R.StateGraphRecorder.__new__(R.StateGraphRecorder)
    rec._macos_ctx, rec._current_url, rec._target_app = FakeContext(d), "", ("", "")
    rec._refresh_app_context()
    assert (rec._current_app, rec._current_bundle_id, rec._current_pid) == ("Terminal", "com.apple.Terminal", TERMINAL)
    assert rec._target_app == ("", "")
    d.front = TEXTEDIT
    rec._refresh_app_context()
    assert rec._target_app == ("TextEdit", "com.apple.TextEdit")
    d.front_app = lambda: None
    rec._refresh_app_context()
    assert rec._current_pid is None          # unknown front: no AX state, never the previous app's


def test_n25_a_browser_check_takes_a_name_or_a_front_app_dict():
    assert R._is_browser_app("Safari") and R._is_browser_app({"name": "Google Chrome", "pid": 3})
    assert not R._is_browser_app({"name": "TextEdit"}) and not R._is_browser_app(None)


def test_n25_a_lost_recorder_step_is_said_and_not_saved(caplog):
    class Broken:
        discarded = saved = False

        def start(self):
            pass

        def record_action(self, **k):
            raise AttributeError("'dict' object has no attribute 'lower'")

        def discard(self):
            Broken.discarded = True

        def save(self):
            Broken.saved = True
    d = Desktop()
    orch = AgentOrchestrator(api_key="x", pixel_scale=1.0, screenshot_capture=Shot(),
                             action_executor=ActionExecutor(backend=d), front_reader=d.front_app,
                             gemini_client=FakeModel([LEARNING_RUN[0], LEARNING_RUN[-1]]))
    orch._sleep = lambda s: None
    with caplog.at_level(logging.WARNING):
        out = orch.run("open spotlight", recorder=Broken())
    assert "not recorded" in out and Broken.discarded and not Broken.saved
    assert any("record_action(key_combination) failed" in r.getMessage() for r in caplog.records)


def test_n25_an_old_template_with_a_dict_app_still_finds_its_guide(tmp_path):
    from anyuse.core.agent import Agent
    asked = []
    agent = Agent.__new__(Agent)
    agent.verbose = False
    agent._workflows = types.SimpleNamespace(
        has_template=lambda wid: True,
        get_template=lambda wid: WorkflowTemplate(id="w", name="slug_name", description="Type the text Hi",
                                                  required_app={"name": "TextEdit", "pid": 3}),
        update_stats=lambda *a, **k: None, save_template=lambda *a: None)
    agent._guide_storage = types.SimpleNamespace(get_by_app=lambda name: asked.append(name) or None)
    agent._orchestrator = types.SimpleNamespace(executor=None)
    agent.enable_memory = True

    async def run(self, task, workflow, params=None, app_guide=None, use_clean_room=True):
        asked.append(task)
        return OrchestratorResult(success=True, message="Replayed: 2 transitions, 0 LLM calls.",
                                  mode="replay-deterministic")
    saved = ExecutionOrchestrator.run, ExecutionOrchestrator.__init__
    ExecutionOrchestrator.run, ExecutionOrchestrator.__init__ = run, (lambda self, **k: None)
    try:
        import anyuse.execution.draft_compile as DC
        refusal, DC.play_refusal = DC.play_refusal, (lambda *a: None)
        try:
            res = agent._play_workflow(types.SimpleNamespace(id="w", name="slug_name", description=""))
        finally:
            DC.play_refusal = refusal
    finally:
        ExecutionOrchestrator.run, ExecutionOrchestrator.__init__ = saved
    # the guide looked up by the NAME; the planner got the description, not the slug
    assert asked == ["TextEdit", "Type the text Hi"]
    assert res.success and res.llm_calls == 0
    assert "served by the recorded replay; 0 model calls" in res.message


# ---------------------------------------------------------------------------
# guides, selectors, atomic writes
# ---------------------------------------------------------------------------

def test_n26_a_selector_in_ax_path_is_written_as_its_dict(tmp_path):
    guides = GS.GuideStorage(base_dir=tmp_path)
    from anyuse.guides.models import AppGuide
    g = AppGuide.create_app_guide("TextEdit")
    sel = Selector(canonical_role="textarea", name="First Text View", raw_role="AXTextArea")
    graph = StateGraph(states={"s": UIState(state_id="s", app="TextEdit", elements=[
        UIElement(role="AXTextArea", title="", ax_path=sel)])})
    g.set_state_graph(graph)
    guides.save(g)
    data = json.loads((tmp_path / g.filename).read_text())
    assert data["ui_states"]["s"]["elements"][0]["ax_path"]["canonical_role"] == "textarea"


def test_n26_a_failing_guide_write_leaves_the_previous_file_whole(tmp_path):
    path = tmp_path / "textedit.json"
    GS.write_json_atomic(path, {"ok": 1})
    with pytest.raises(TypeError):
        GS.write_json_atomic(path, {"broken": object()})
    assert json.loads(path.read_text()) == {"ok": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["textedit.json"]   # no temp file left


def test_n25_the_clean_room_opens_the_required_app_by_its_bundle_id():
    from anyuse.execution.orchestrator import _entry_app
    graph = StateGraph()
    graph.add_state(UIState(state_id="schach_board", app="Schach"))
    graph.add_state(UIState(state_id="terminal_start", app="Terminal"))
    tpl = WorkflowTemplate(id="w", name="new game", required_app="Schach",
                           required_bundle_id="com.apple.Chess", state_path=["schach_board"])
    assert _entry_app(tpl, graph) == "com.apple.Chess"
    # a launch recorded from another app starts there, by the name it was recorded under
    tpl.state_path = ["terminal_start", "schach_board"]
    assert _entry_app(tpl, graph) == "Terminal"
    # a template saved before the bundle id existed: the name, as before; nothing new is written
    old = WorkflowTemplate.from_dict({"id": "w", "name": "n", "required_app": "TextEdit"})
    assert old.required_bundle_id == "" and _entry_app(old, graph) == "TextEdit"
    assert "required_bundle_id" not in old.to_dict()
    assert WorkflowTemplate.from_dict(tpl.to_dict()).required_bundle_id == "com.apple.Chess"


def test_n26_a_failing_template_write_leaves_the_previous_template_whole(tmp_path):
    storage = WorkflowStorage(base_dir=tmp_path / "workflows")
    tpl = WorkflowTemplate(id="w", name="new game", required_app="TextEdit")
    storage.save_template("w", tpl)
    path = storage.get_workflow_dir("w") / "template.json"
    before = path.read_text()
    tpl.recorded_graph = {"broken": object()}
    with pytest.raises(TypeError):
        storage.save_template("w", tpl)
    assert path.read_text() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["template.json"]


def test_n26_a_partial_recording_keeps_no_recorded_graph():
    rec = R.StateGraphRecorder.__new__(R.StateGraphRecorder)
    from anyuse.memory.models import WorkflowIntent
    rec._template_steps = [WorkflowIntent(intent="a", target_element="", expected_before_state="x",
                                          expected_after_state="y")]
    rec._transitions_discovered, rec._states_discovered = [], {}
    assert rec._recorded_graph() is None


# ---------------------------------------------------------------------------
# the after-state is read after the action
# ---------------------------------------------------------------------------

def test_n27_the_loop_reads_the_after_state_after_it_acted():
    events = []

    class Spy:
        def start(self):
            pass

        def record_action(self, **k):
            events.append(("record", k["action"], k.get("defer_after")))

        def record_after_state(self, waited_s=None):
            events.append(("after", waited_s))

        def stop(self):
            pass

        def save(self):
            events.append(("save",))

        def discard(self):
            pass
    d = Desktop()
    real = d.key_combination
    d.key_combination = lambda keys: events.append(("act", keys)) or real(keys)
    orch = AgentOrchestrator(api_key="x", pixel_scale=1.0, screenshot_capture=Shot(),
                             action_executor=ActionExecutor(backend=d), front_reader=d.front_app,
                             gemini_client=FakeModel([LEARNING_RUN[0], LEARNING_RUN[-1]]))
    orch._sleep = lambda s: None
    orch.goal_check = lambda t, m: (True, "")
    orch.run("open spotlight", recorder=Spy())
    # Spotlight came forward: one poll until it is seen twice in front, then the after-state
    assert events[:3] == [("record", "key_combination", True), ("act", "cmd+space"), ("after", 0.1)]
    assert events[-1] == ("save",)


def test_n27_an_after_state_never_asked_for_is_read_at_the_next_step():
    d = Desktop()
    FakeTree.desktop = d
    saved = (R.AccessibilityTree, R.AX_AVAILABLE)
    R.AccessibilityTree, R.AX_AVAILABLE = FakeTree, True
    try:
        rec = R.StateGraphRecorder.__new__(R.StateGraphRecorder)
        rec.__dict__.update(_legacy_recorder=types.SimpleNamespace(record_action=lambda **k: None, steps=[]),
                            _macos_ctx=FakeContext(d), _current_url="", _target_app=("", ""),
                            _previous_state_id=None, _previous_snapshot=None, _states_discovered={},
                            _transitions_discovered=[], _template_steps=[], _template_params=[],
                            _state_path=[], _pending=None, _last_after=None, _steps_meta=[], _ax_tree=None,
                            _current_app="", _current_bundle_id="", _current_pid=None)
        rec.record_action("key_combination", {"keys": "cmd+space"}, defer_after=True)
        assert rec.transitions_discovered == []          # nothing read before the action
        d.key_combination("cmd+space")
        rec.record_action("type_text_at", {"x": None, "y": None, "text": "TextEdit", "press_enter": True},
                          defer_after=True)
        (t,) = rec.transitions_discovered
        assert t.from_state.startswith("terminal_") and t.to_state.startswith("spotlight_")
    finally:
        R.AccessibilityTree, R.AX_AVAILABLE = saved


# ---------------------------------------------------------------------------
# a drag
# ---------------------------------------------------------------------------

class _Board(AccessibilityTree):
    """Two squares of a board: AX buttons at (10, 10) and (10, 50)."""
    squares = {(10, 10): AXElementDesc(role="AXButton", ax_description="e2, weißer Bauer"),
               (10, 50): AXElementDesc(role="AXButton", ax_description="e4")}

    def find_at(self, x, y):
        return self.squares.get((x, y))

    def describe_element(self, ref):
        return ref

    def to_selector(self, ref):
        return ref.to_selector(path=f"board/{ref.ax_description}")


def test_n28_a_drag_on_a_button_grid_keeps_the_coordinate_gesture():
    rec = R.StateGraphRecorder.__new__(R.StateGraphRecorder)
    rec._ax_tree = _Board(5)
    (drag,) = rec._drag_implementations({"x1": 10, "y1": 10, "x2": 10, "y2": 50})
    assert drag.modality == "coordinate" and {k: drag.command[k] for k in ("action", "x1", "y1", "x2", "y2")} == {
        "action": "drag", "x1": 10, "y1": 10, "x2": 10, "y2": 50}
    assert drag.command["from_selector"]["name"] == "e2, weißer Bauer"
    # no grid under the ends: the coordinate drag alone
    rec._ax_tree = None
    (only,) = rec._drag_implementations({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
    assert only.modality == "coordinate" and "from_selector" not in only.command


def test_n28_the_drag_replays_with_both_endpoint_checks():
    presses = []

    class Tree(_Board):
        def find_selector(self, sel):
            return next((e for e in self.squares.values() if e.ax_description == sel.name), None)

        def perform_action(self, el, action):
            presses.append((el.ax_description, action))
            return True
    rec = R.StateGraphRecorder.__new__(R.StateGraphRecorder)
    rec._ax_tree = Tree(5)
    (drag,) = rec._drag_implementations({"x1": 10, "y1": 10, "x2": 10, "y2": 50})

    class Backend:
        calls = []

        def drag_to(self, **k):
            Backend.calls.append(k)
    ex = StateGraphExecutor(ax_tree=Tree(5), action_executor=ActionExecutor(backend=Backend()))
    res = ex._execute_coordinate(PlannedStep(intent="move", source="workflow", target_element="",
                                             modality="coordinate", command=drag.command))
    assert res["success"] and Backend.calls == [{"x1": 10, "y1": 10, "x2": 10, "y2": 50}], res
    assert presses == []
    # an end that no longer shows the recorded square is refused before anything is dragged
    Tree.squares = {(10, 10): AXElementDesc(role="AXButton", ax_description="e2, weißer Bauer"),
                    (10, 50): AXElementDesc(role="AXButton", ax_description="e5")}
    try:
        res = ex._execute_coordinate(PlannedStep(intent="move", source="workflow", target_element="",
                                                 modality="coordinate", command=drag.command))
    finally:
        Tree.squares = _Board.squares
    assert not res["success"] and len(Backend.calls) == 1, res


# ---------------------------------------------------------------------------
# AppleScript only with Automation consent, never asked
# ---------------------------------------------------------------------------

def _consent(codes):
    asked = []
    saved = M.automation_probe, M.bundle_resolver
    M.automation_probe = lambda bid: asked.append(bid) or codes.get(bid)
    M.bundle_resolver = lambda name: {"TextEdit": "com.apple.TextEdit", "Finder": "com.apple.finder"}.get(name, "")
    return asked, saved


def test_n29_applescript_runs_only_where_automation_was_granted():
    asked, saved = _consent({"com.apple.TextEdit": M.AE_WOULD_ASK, "com.apple.finder": M.AE_GRANTED})
    try:
        assert M.applescript_targets('tell application "TextEdit" to make new document\n'
                                     'tell application id "com.apple.finder" to get name') == [
            "com.apple.TextEdit", "com.apple.finder"]
        why = M.applescript_refusal('tell application "TextEdit" to make new document')
        assert "com.apple.TextEdit" in why and "would_ask" in why
        assert M.applescript_refusal('tell application "Finder" to get name of front window') is None
        assert M.applescript_refusal('tell application "Nowhere" to activate')       # unresolved: refused
        assert M.applescript_refusal("beep") is None                                 # addresses no app
        assert M.automation_status("com.apple.TextEdit") == "would_ask"
        M.automation_probe = lambda bid: None
        assert M.automation_status("com.apple.TextEdit") == "unknown"
    finally:
        M.automation_probe, M.bundle_resolver = saved


def test_n29_the_executor_and_the_intervention_never_send_an_unconsented_script(monkeypatch):
    import subprocess
    spawned = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a))
    monkeypatch.setenv("ANYUSE_INTERLOCK_TEST_BYPASS", "1")
    asked, saved = _consent({"com.apple.TextEdit": M.AE_DENIED})
    try:
        from anyuse.execution import foreground_interlock as FI
        monkeypatch.setattr(FI, "refusal", lambda where: None)
        ex = StateGraphExecutor.__new__(StateGraphExecutor)
        r = ex._execute_applescript(PlannedStep(intent="new document", source="ai", target_element="",
                                                modality="applescript",
                                                command={"script": 'tell application "TextEdit" to make new document'}))
        assert r["success"] is False and r["reason_code"] == "automation_not_granted" and not r.get("refused")
        from anyuse.execution.intervention import InterventionHandler
        h = InterventionHandler.__new__(InterventionHandler)
        assert asyncio.run(h._run_applescript_fix({"script": 'tell application "TextEdit" to activate'})) is False
        assert M._osascript('tell application "TextEdit" to get name') is None
    finally:
        M.automation_probe, M.bundle_resolver = saved
    assert spawned == [] and asked == ["com.apple.TextEdit"] * 3


def test_n29_the_planner_offers_applescript_only_where_automation_was_granted():
    from anyuse.execution.planner import ExecutionPlanner
    planner = ExecutionPlanner(adapter=types.SimpleNamespace())
    tpl = WorkflowTemplate(id="w", name="save", required_app="TextEdit")

    def prompt():
        return planner._build_prompt("save the document", {}, tpl, "{}", StateGraph(), {}, None)

    asked, saved = _consent({"com.apple.TextEdit": M.AE_WOULD_ASK, "com.apple.systemevents": M.AE_WOULD_ASK})
    try:
        text = prompt()
        assert '"applescript": NOT AVAILABLE' in text and "Never choose it" in text
        assert asked == ["com.apple.TextEdit", "com.apple.systemevents"]      # read, never asked
        M.automation_probe = lambda bid: {"com.apple.systemevents": M.AE_GRANTED}.get(bid)
        assert "Address only these apps, which granted Automation: System Events." in prompt()
        M.automation_probe = lambda bid: M.AE_GRANTED
        assert "granted Automation: TextEdit, System Events." in prompt()
        # the recorded bundle id is asked, not the name's lookup
        tpl.required_bundle_id = "com.apple.TextEdit"
        M.bundle_resolver = lambda name: pytest.fail("the bundle id was recorded")
        assert "granted Automation: TextEdit, System Events." in prompt()
    finally:
        M.automation_probe, M.bundle_resolver = saved


def test_n29_the_consent_probe_degrades_to_no_answer_without_coreservices():
    # under the test guard the framework cannot load: no answer, never a prompt
    assert M._ae_permission("com.apple.TextEdit") in (None, 0, -1743, -1744, -600)


# ---------------------------------------------------------------------------
# run stats; exact text, the launch settle
# ---------------------------------------------------------------------------

def test_n32_a_template_replay_counts_as_a_run(tmp_path):
    from anyuse.core.agent import Agent
    storage = WorkflowStorage(base_dir=tmp_path)
    from anyuse.memory.models import Workflow
    wf = Workflow.create_new("w", "Type the text Hi")
    storage.save(wf)
    tpl = WorkflowTemplate.create_new("w", "Type the text Hi")
    storage.save_template(wf.id, tpl)
    agent = Agent.__new__(Agent)
    agent._workflows = storage
    agent._record_play(wf, tpl, True, "ok")
    agent._record_play(wf, tpl, False, "stopped")
    again = WorkflowStorage(base_dir=tmp_path)
    assert (again.get("w").run_count, again.get("w").success_count) == (2, 1)
    assert (again.get_template(wf.id).run_count, again.get_template(wf.id).success_count) == (2, 1)


def test_n32_a_recorded_replay_is_decided_without_the_verifier():
    from anyuse.core.agent import Agent
    from anyuse.core.result import ResultStatus
    agent = Agent.__new__(Agent)
    agent._focused_text_fn = lambda: {"role": "AXTextArea", "value": "Hello from AnyUse"}
    agent._last_replay = {"mode": "replay-deterministic", "status": ResultStatus.SUCCESS, "llm_calls": 0,
                          "message": "Replayed (served by the recorded replay; 0 model calls)"}
    agent._decided = None
    agent._decide_replayed("type the text Hello from AnyUse", "Replayed (served by the recorded replay; 0 model calls)")
    assert agent._decided[1] is ResultStatus.SUCCESS
    agent._focused_text_fn = lambda: {"role": "AXTextArea", "value": "\nHello from AnyUse"}
    agent._decide_replayed("type the text Hello from AnyUse", "Replayed (served by the recorded replay; 0 model calls)")
    assert agent._decided[1] is ResultStatus.FAILED
    # served by the model planner: the verifier still decides
    agent._decided = None
    agent._last_replay = dict(agent._last_replay, mode="replay", llm_calls=1)
    agent._decide_replayed("t", agent._last_replay["message"])
    assert agent._decided is None


def test_n33_requested_text_and_the_exact_text_check():
    from anyuse.core.agent import Agent, requested_text
    from anyuse.core.result import ResultStatus
    assert requested_text("Open TextEdit and create a new document containing the text Hello from AnyUse") \
        == "Hello from AnyUse"
    assert requested_text('type "Grüße, Welt" into the note') == "Grüße, Welt"
    assert requested_text("Öffne TextEdit und schreibe „Hallo Welt“") == "Hallo Welt"
    assert requested_text("open Alex's files") is None
    agent = Agent.__new__(Agent)
    agent._verifier, agent.enable_verification = None, False
    task = "Open TextEdit and create a new document containing the text Hello from AnyUse"
    agent._focused_text_fn = lambda: {"role": "AXTextArea", "value": "\nHello from AnyUse"}
    status, why = agent._decide_outcome("done", task)
    assert status is ResultStatus.FAILED and "extra blank lines" in why
    agent._focused_text_fn = lambda: {"role": "AXTextArea", "value": "Hello from AnyUse"}
    assert agent._decide_outcome("done", task)[0] is ResultStatus.UNCERTAIN    # no verifier: undecided
    agent._focused_text_fn = lambda: {"role": "AXTextArea", "value": "Dear team,\nHello from AnyUse"}
    assert agent._text_check(task) is None                                      # more text: the verifier's
    agent._focused_text_fn = lambda: None
    assert agent._text_check(task) is None


def test_n33_the_loop_waits_until_a_spotlight_launch_has_landed():
    d = Desktop()
    reads = []
    frames = [d.front_app() | {"pid": SPOTLIGHT, "bundle_id": "com.apple.Spotlight", "name": "Spotlight",
                               "has_window": False}] * 3 + [
        {"name": "Terminal", "bundle_id": "com.apple.Terminal", "pid": TERMINAL, "has_window": True}] * 2 + [
        {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": TEXTEDIT, "has_window": True}] * 3
    orch = AgentOrchestrator(api_key="x", pixel_scale=1.0,
                             front_reader=lambda: reads.append(1) or frames[min(len(reads) - 1, len(frames) - 1)])
    orch._sleep = lambda s: None
    before = {"name": "Spotlight", "bundle_id": "com.apple.Spotlight", "pid": SPOTLIGHT}
    waited = orch._settle_after(before, "type_text_at", {"text": "TextEdit", "press_enter": True})
    assert round(waited, 1) == 0.6 and reads and frames[min(len(reads) - 1, len(frames) - 1)]["pid"] == TEXTEDIT
    # a step that neither launched nor switched waits for nothing
    reads.clear()
    orch._front_reader = lambda: {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": TEXTEDIT,
                                  "has_window": True}
    assert orch._settle_after({"pid": TEXTEDIT, "bundle_id": "com.apple.TextEdit"}, "type_text_at",
                              {"text": "x"}) == 0.0


def test_n33_the_focus_lock_leaves_spotlight_alone():
    orch = AgentOrchestrator(api_key="x", pixel_scale=1.0,
                             front_reader=lambda: {"name": "Spotlight", "bundle_id": "com.apple.Spotlight",
                                                   "pid": SPOTLIGHT})
    import anyuse.execution.foreground_interlock as FI
    saved = FI.refusal
    FI.refusal = lambda where: None
    activated = []
    sys.modules["AppKit"] = types.SimpleNamespace(NSWorkspace=types.SimpleNamespace(
        sharedWorkspace=lambda: activated.append("asked") or None))
    try:
        orch._ensure_focus("TextEdit")
    finally:
        FI.refusal = saved
        sys.modules.pop("AppKit", None)
    assert activated == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
