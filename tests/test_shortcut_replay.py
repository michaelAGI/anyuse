"""A recorded shortcut no menu item was found for, replayed as the shortcut itself.

Chess builds its "Spiel" menu only when it opens, so the recorder found no menu item for Cmd+N and
the routine ended ``needs_fill`` on it. The recorder
now opens such a menu when actuation is allowed (test_watcher_structured_steps.py); where it may
not (the host) or the menu still holds no item, the compiler replays the chord itself
(``press_shortcut``), checked by the effect the recording shows.

Offline: a real Chess draft recorded in the VM and fake AX trees; nothing sends input or calls a model.

Pinned:
  - the Chess draft compiles its Cmd+N as ``press_shortcut`` (keycode 45, the command bit only,
    Chess's bundle id, expect one window and one sheet more), reported "replayed as a shortcut";
    without the guest's vault only the two board squares stay gaps (``needs_fill``); with their
    labels released from the recording it is ``ready`` (the fixed-routine replay passes);
  - a destructive chord (quit, close, delete, force quit), a chord with no recorded effect and one
    without the app's bundle id stay ``key`` gaps;
  - the handler sends the chord only to the recorded app in front (it brings that app there
    first); destructive chords, extra flag bits, the gates and a tree bound to another app refuse
    before anything is sent; the recorded effect is read back (``uncertain_completion`` when it
    does not appear);
  - the checked executor runs it without resolving a target, an executor without the row refuses
    it, and under ``destructive`` it is held;
  - the saved routine replays end to end on the recorded AX world, its app found by its bundle id
    (on a German system "Schach" is the process "Chess");
  - ``anyuse workflow from-draft`` prints the step as replayed as a shortcut.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh tests/test_shortcut_replay.py
"""
import asyncio
import copy
import json
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.hil_gate import StepClass, classify_step  # noqa: E402
from anyuse.execution.plan_types import PlannedStep  # noqa: E402
from anyuse.watcher import entity_vault as EV  # noqa: E402
from anyuse.watcher import step_capture as SC  # noqa: E402

CHESS = _HERE / "testdata" / "drafts" / "chess_cmd_n_draft.json"
CMD = AX.SHORTCUT_FLAGS["cmd"]
#: the step targets' labels, from the recording's own log (Starten, then e2 and e4)
LABELS = {"[[ent:ui_label:8abe75f90948]]": "Starten",
          "[[ent:ui_label:35c647bce764]]": "weißer Bauer, e2",
          "[[ent:ui_label:682d0277680a]]": "e4"}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def no_vault():
    """This machine holds no vault (the guest's labels are not released here)."""
    EV._default_vault, EV._default_vault_failed = None, True


def chess():
    return json.loads(CHESS.read_text())


def released():
    """The Chess draft as a compile inside the guest reads it: its step targets' labels released."""
    out, _n = SC.release_labels(chess(), lambda m: LABELS.get(m))
    return out


def key_step(data):
    return next(e for e in data["events"] if e["tag_type"] == "STEP" and e["data"]["action"] == "key")


# ── the Chess draft ──────────────────────────────────────────────────────────────────────────

def test_the_chess_recording_compiles_cmd_n_as_the_shortcut():
    no_vault()
    data = chess()
    assert key_step(data)["data"]["flags"] == 0x20100000          # the tap's raw flags: non-coalesced + command
    rep = run(DC.draft_to_workflow(data, name="chess_e4", save=False))
    assert rep["status"] == "needs_fill", rep["reasons"]
    # the shortcut is no gap any more; only the squares, whose labels are in the guest's vault
    assert [(u["draft_index"], u["gap"]) for u in rep["unreplayable"]] == [(2, "no_identity"), (3, "no_identity")]
    assert [s["draft_index"] for s in rep["shortcuts"]] == [0]
    note = rep["shortcuts"][0]
    assert note["keys"] == "cmd+n" and note["note"].startswith("replayed as a shortcut (cmd+n)")
    assert note["expect"] == {"windows": "more", "sheets": "more"}
    rec = DC.compile_record(DC.load_source(data), [], [])
    cmd = rec["steps"][0]["command"]
    assert {k: cmd[k] for k in ("action", "keys", "keycode", "flags", "bundle_id", "expect")} == {
        "action": "press_shortcut", "keys": "cmd+n", "keycode": 45, "flags": CMD, "bundle_id": "com.apple.Chess",
        "expect": {"windows": "more", "sheets": "more"}}
    assert "selector" not in cmd and rec["steps"][0]["target_element"] == "shortcut cmd+n"
    assert rec["compiler"]["version"] == 2                         # a contract action
    assert rec["shortcuts"] == rep["shortcuts"]


def test_the_chess_recording_with_its_labels_released_is_ready():
    no_vault()
    rep = run(DC.draft_to_workflow(released(), name="chess_e4", save=False))
    assert rep["status"] == "ready", rep["reasons"]
    proof = rep["proof"]
    assert proof["passed"] and proof["fixed_routine"] and len(proof["cases"]) == 1
    assert proof["cases"][0]["drift_fired"] == 0
    rec = DC.compile_record(DC.load_source(released()), [], [])
    assert [(s["command"]["action"], s["command"].get("title")) for s in rec["steps"]] == [
        ("press_shortcut", "cmd+n"), ("press", "Starten"), ("press", "weißer Bauer, e2"), ("press", "e4")]
    # every step is held at the learning level (``all``); under ``destructive`` the chord is held as one
    assert [g["index"] for g in DC.approval_plan(rec["steps"])["gated_steps"]] == [0, 1, 2, 3]
    action = DC._gate_action(PlannedStep.from_dict(rec["steps"][0]), "destructive", "com.apple.Chess")
    assert "keyboard shortcut cmd+n" in action["args"]["hold"]
    assert classify_step(action, DC.gate_keywords("destructive")) == StepClass.DESTRUCTIVE


def _variant(**key):
    data = released()
    key_step(data)["data"].update(key)
    return data


def test_destructive_effectless_and_unidentified_shortcuts_stay_gaps():
    no_vault()
    for keys, code, flags, what in (("cmd+q", 12, CMD, "quits"), ("shift+cmd+q", 12, CMD | 0x20000, "quits"),
                                    ("cmd+w", 13, CMD, "closes a window"), ("cmd+key51", 51, CMD, "deletes"),
                                    ("opt+cmd+key53", 53, CMD | 0x80000, "force quits")):
        src = DC.load_source(_variant(keys=keys, keycode=code, flags=flags))
        st = src.steps[0]
        assert (st.action, st.gap) == ("press_key", "key") and what in st.problem, (keys, st.problem)
        assert "never replayed as a key" in st.problem
    # the next step was recorded on the same screen: nothing would check the replay
    data = released()
    steps = [e for e in data["events"] if e["tag_type"] == "STEP"]
    steps[1]["ax_state_id"] = steps[0]["ax_state_id"]
    st = DC.load_source(data).steps[0]
    assert st.gap == "key" and "shows no effect" in st.problem and st.action == "press_key"
    # the shortcut is the recording's last step
    data = released()
    data["events"] = [e for e in data["events"] if e["tag_type"] != "STEP" or e["data"]["action"] == "key"]
    st = DC.load_source(data).steps[0]
    assert st.gap == "key" and "shows no effect" in st.problem
    # no bundle id anywhere: a key goes to the app in front, which could not be checked
    data = released()
    data.pop("bundle_id")
    for e in data["events"]:
        (e.get("data") or {}).pop("bundle_id", None)
    st = DC.load_source(data).steps[0]
    assert st.gap == "key" and "bundle id" in st.problem
    # the step still compiles with a shortcut the recording shows the effect of (control chord)
    st = DC.load_source(_variant(keys="ctrl+n", flags=0x40000)).steps[0]
    assert (st.action, st.flags, st.problem) == ("press_shortcut", 0x40000, "")


# ── the handler ──────────────────────────────────────────────────────────────────────────────

class ShortcutTree:
    """An app's AX tree as ``press_shortcut`` uses it. ``effect``: what one key press does to its
    window / sheet counts; ``activates``: whether activating it brings it to the front."""

    def __init__(self, front="com.apple.Chess", bound="com.apple.Chess", gate=None, effect=None,
                 activates=True):
        self.front, self.bound, self.gate, self.activates = front, bound, gate, activates
        self.effect = {"windows": 1, "sheets": 1} if effect is None else effect
        self.counts = {"windows": 0, "sheets": 0}
        self.sent, self.activated = [], []

    def bundle_id(self):
        return self.bound

    def frontmost_bundle_id(self):
        return self.front

    def activate_app(self, bid):
        self.activated.append(bid)
        if self.activates:
            self.front = bid
        return self.activates

    def actuation_refusal(self, where):
        return self.gate

    def press_key(self, code, flags=0):
        self.sent.append((code, flags))
        for k, d in self.effect.items():
            self.counts[k] += d
        return True

    def window_counts(self):
        return dict(self.counts)


def cmd_n(**over):
    out = {"action": "press_shortcut", "keys": "cmd+n", "keycode": 45, "flags": CMD, "title": "cmd+n",
           "bundle_id": "com.apple.Chess", "expect": {"windows": "more", "sheets": "more"}}
    out.update(over)
    return out


def test_the_shortcut_goes_to_the_recorded_app_in_front_and_its_effect_is_read_back(monkeypatch):
    monkeypatch.setattr(AX, "SHORTCUT_EFFECT_S", 0.05)
    t = ShortcutTree()
    out = AX.press_shortcut(t, None, None, cmd_n())
    assert out["success"] and out["replayed_as"] == "shortcut" and t.sent == [(45, CMD)]
    assert (out["counts_before"], out["counts_after"]) == ({"windows": 0, "sheets": 0}, {"windows": 1, "sheets": 1})
    # another app in front: the recorded one is brought there first
    t = ShortcutTree(front="com.apple.Terminal")
    assert AX.press_shortcut(t, None, None, cmd_n())["success"] and t.activated == ["com.apple.Chess"]
    # it stays behind: refused, nothing sent
    t = ShortcutTree(front="com.apple.Terminal", activates=False)
    out = AX.press_shortcut(t, None, None, cmd_n())
    assert out["reason_code"] == "wrong_app" and "com.apple.Terminal is in front" in out["error"] and t.sent == []
    # the recorded effect does not appear: acted, uncertain (a stop kind, never sent again)
    t = ShortcutTree(effect={})
    out = AX.press_shortcut(t, None, None, cmd_n())
    assert not out["success"] and out["acted"] and out["outcome_kind"] == "uncertain_completion"
    assert "windows 0 -> 0 (recorded: more)" in out["error"] and t.sent == [(45, CMD)]
    # a window fewer (cmd+w would close one, but it never gets here): the direction is checked
    t = ShortcutTree(effect={"windows": 1})
    assert not AX.press_shortcut(t, None, None, cmd_n(expect={"windows": "fewer"}))["success"]
    # a recorded effect that cannot be read here: refused, nothing sent (nothing would check the replay)
    t = ShortcutTree()
    t.window_counts = None
    out = AX.press_shortcut(t, None, None, cmd_n())
    assert out["refused"] and out["acted"] is False and "cannot be read here" in out["error"] and t.sent == []
    # no recorded count at all: sent, and the next step's drift gate checks the screen
    out = AX.press_shortcut(t, None, None, cmd_n(expect=None))
    assert out["success"] and "counts_after" not in out and t.sent == [(45, CMD)]


def test_the_shortcut_handler_refuses_before_anything_is_sent():
    cases = [
        (cmd_n(keys="cmd+q", keycode=12), "quits", "identity"),
        (cmd_n(keys="cmd+w", keycode=13), "closes a window", "identity"),
        (cmd_n(keys="cmd+key117", keycode=117), "deletes", "identity"),
        (cmd_n(flags=0x20100000), "more than the modifier bits", "identity"),
        (cmd_n(flags=0x20000), "only a command or control chord", "identity"),
        (cmd_n(keycode=None), "keycode", "identity"),
        (cmd_n(bundle_id=""), "bundle id", "identity"),
    ]
    for cmd, why, code in cases:
        t = ShortcutTree()
        out = AX.press_shortcut(t, None, None, cmd)
        assert out["refused"] and why in out["error"] and out["reason_code"] == code, (cmd, out)
        assert t.sent == [] and t.activated == []
    t = ShortcutTree(gate="actuation gated (ANYUSE_DRIVE_ENABLED off)")
    out = AX.press_shortcut(t, None, None, cmd_n())
    assert out["reason_code"] == "gated" and t.sent == [] and t.activated == []
    t = ShortcutTree(bound="com.apple.TextEdit")
    out = AX.press_shortcut(t, None, None, cmd_n())
    assert out["reason_code"] == "wrong_app" and t.sent == []
    out = AX.press_shortcut(object(), None, None, cmd_n())
    assert out["reason_code"] == "unsupported_verb" and AX.NOT_IMPLEMENTED in out["error"]


# ── the executor ─────────────────────────────────────────────────────────────────────────────

class Bare(StateGraphExecutor):
    AX_CONTRACT = frozenset({"open_app"})


def test_the_checked_executor_runs_the_shortcut_without_a_target():
    assert AX.TARGETLESS >= {"press_shortcut"} and set(AX.CONTRACT) >= {"press_shortcut"}
    no_vault()
    rec = DC.compile_record(DC.load_source(released()), [], [])
    step = PlannedStep.from_dict(rec["steps"][0])
    t = ShortcutTree()
    out = DC.checked_executor(t)._execute_ax(step)
    assert out["success"] and out["replayed_as"] == "shortcut" and t.sent == [(45, CMD)]
    assert AX.runner_refusal(rec["steps"][0], DC.checked_executor(t)) is None
    why = AX.runner_refusal(rec["steps"][0], Bare(ax_tree=t))
    assert why["reason_code"] == "unsupported_verb" and why["missing"] == ["press_shortcut"]
    assert "press_shortcut" in AX.record_runner_problem(rec, Bare(ax_tree=t))


def test_the_saved_chess_routine_replays_on_the_app_found_by_its_bundle_id():
    """End to end offline: the Chess routine compiled (labels released), saved ready, replayed through the
    checked executor on the recorded AX world. The app is asked for by its bundle id (the
    record says "Schach", the process is "Chess", and the name finds nothing)."""
    from anyuse.execution import draft_proof as PR
    from anyuse.memory.storage import WorkflowStorage
    no_vault()
    st = WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-shortcut-st-")))
    rep = run(DC.draft_to_workflow(released(), name="chess_new_game", save=True, storage=st))
    assert rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("chess_new_game", st)
    asked, worlds = [], []

    def factory(app):
        asked.append((type(app) is DC.BundleId, str(app)))
        if isinstance(app, DC.BundleId) and app == "com.apple.Chess":
            worlds.append(PR.SimAXWorld(rec))
            return worlds[-1]
        return None                                     # "Schach" names no process

    async def approve(_ctx):
        return True
    out = run(DC.replay_saved("chess_new_game", {}, approver=approve, storage=st, tree_factory=factory,
                              on_step=lambda k: worlds[0].at_step(k),      # the recorded screen per step
                              preflight=lambda: {"drive_enabled": True, "may_drive": True,
                                                 "interlock": {"allowed": True}}))
    assert out["ok"], out
    assert asked == [(True, "com.apple.Chess")]
    effects = worlds[0].effects
    assert [e["op"] for e in effects][:1] == ["key"] and effects[0]["value"] == "45"   # the chord, sent once
    assert [e["op"] for e in effects[1:]] == ["AXPress"] * 3


def test_the_cli_reports_the_step_replayed_as_a_shortcut():
    from click.testing import CliRunner
    from anyuse.cli import cli
    no_vault()
    data = tempfile.mkdtemp(prefix="anyuse-shortcut-cli-")
    res = CliRunner().invoke(cli, ["workflow", "from-draft", str(CHESS), "--name", "chess_e4", "--dry-run"],
                             env={"ANYUSE_DATA_DIR": data, "ANYUSE_DRIVE_ENABLED": ""})
    assert res.exit_code == 1, res.output
    assert "~ step 0: replayed as a shortcut (cmd+n)" in res.output, res.output
    assert "Status: NEEDS_FILL" in res.output
    assert "step 0 cannot be replayed" not in res.output


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
