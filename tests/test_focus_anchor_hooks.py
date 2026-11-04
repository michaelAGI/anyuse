"""Two runner hooks of the draft seam:

(a) a ``focused`` step whose trigger is itself a ``focused`` step (the Return right after the
    focused typing) inherits that step's focus: the compile records ``trigger_kind: focused``,
    and the runner requires the focus to be STILL on the element the typing resolved to (the
    same element, the same window); a focus that moved in between is refused, nothing pressed.
    A click trigger keeps its rule (the focus must have moved, into the trigger's window).
(b) the proof world never invents a text mode's start: a replace_line / append_text field
    starts from the recorded start SHAPE (watcher data ``start``: line count, the anchor's line,
    keyed by the anchor marker; no text), its anchor line holding the case's value of the anchor
    parameter (``anker_<n>``); the recorded anchor in no line of it fails the proof ("the anchor
    line is not in the recorded document"), and without a shape the record stays ``draft``. An
    old field value (``value_before``) is never read.

Fakes only: the real ``AccessibilityTree`` on ``FakeAX`` (``test_ax_runner_executor``), the
proof's ``SimAXWorld``, a throwaway entity vault (to show it is never asked).

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_focus_anchor_hooks.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"            # the fake AX below is the only "desktop"
os.environ["ANYUSE_INTERLOCK_TEST_BYPASS"] = "1"    # honoured only under the guarded runner

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, "wrong checkout (D9)"

import test_ax_runner_executor as TX  # noqa: E402  (FakeAX, FakeAppKit, focus_app, install)
from anyuse.context import accessibility as acc  # noqa: E402
from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import draft_proof as PR  # noqa: E402
from anyuse.memory.storage import WorkflowStorage  # noqa: E402
from anyuse.watcher import step_capture as SC  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def yes(_ctx):
    return True


def vstore():
    from anyuse.execution.verdict_store import VerdictStore
    return VerdictStore(os.path.join(tempfile.mkdtemp(prefix="anyuse-hooks-v-"), "verdicts.jsonl"))


def draft(name, data_steps, app="TextEdit", bundle="com.apple.TextEdit"):
    ev = [{"id": "e0", "tag_type": "WORKFLOW_START", "timestamp": "2026-10-10T10:00:00", "data": {}}]
    for i, (data, reason) in enumerate(data_steps, 1):
        ev.append({"id": f"e{i}", "tag_type": "STEP", "timestamp": f"2026-10-10T10:00:{i:02d}",
                   "reason": reason, "data": data})
    ev.append({"id": f"e{len(data_steps) + 1}", "tag_type": "WORKFLOW_END",
               "timestamp": f"2026-10-10T10:00:{len(data_steps) + 1:02d}", "data": {}})
    return {"name": name, "goal": "g", "session_id": "s", "apps_involved": [app], "urls_visited": [],
            "app": app, "bundle_id": bundle, "events": ev, "anchors": [], "ax_states": {}}


def compile_(d, overrides, save=False, st=None):
    return run(DC.draft_to_workflow(d, overrides=overrides, save=save, verdicts=vstore(),
                                    storage=st or WorkflowStorage(base_dir=pathlib.Path(
                                        tempfile.mkdtemp(prefix="anyuse-hooks-")))))


def record_of(d, overrides):
    src = DC.load_source(d)
    cands = DC.DP.collect_candidates(src.steps)
    params = DC.choose_parameters(src, cands, overrides=overrides)["parameters"]
    return DC.compile_record(src, params, cands)


# ── (a) a focused step after a focused step ──────────────────────────────────────────────────

def folder_draft():
    """Finder's inline create: "Neuer Ordner" focuses the name field, the name is typed into
    it, Return commits it in the same field."""
    return draft("folder", [
        ({"action": "click", "focuses": "AXTextField",
          "selector": {"canonical_role": "button", "name": "Neuer Ordner", "raw_role": "AXButton"}}, "new folder"),
        ({"action": "type", "text": "Archiv 2025"}, "name it"),
        ({"action": "key", "keys": "return", "focused": True, "role": "AXTextField"}, "commit the name"),
    ], app="Finder", bundle="com.apple.finder")


class KeyTree(acc.AccessibilityTree):
    """The real tree; its key press is recorded instead of posted (``context.keyboard`` posts
    through Quartz, which the guarded runner blocks)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.keys = []

    def press_key(self, keycode, flags=0, presser=None):
        self.keys.append(int(keycode))
        return True


def test_a_focused_step_after_a_focused_step_records_trigger_kind_focused():
    rec = record_of(folder_draft(), [{"name": "name", "ref": "@c1"}])
    typing, ret = rec["steps"][1]["command"]["selector"], rec["steps"][2]["command"]["selector"]
    assert (typing["kind"], typing["trigger"], ret["kind"], ret["trigger"]) == ("focused", 0, "focused", 1)
    assert "trigger_kind" not in typing                     # its trigger is a click: the rule of before
    assert ret["trigger_kind"] == "focused"
    assert rec["steps"][2]["command"]["action"] == "press_key" and rec["steps"][2]["command"]["keycode"] == 36


def test_the_return_after_the_focused_typing_runs_while_the_focus_stays_on_that_field():
    rec = record_of(folder_draft(), [{"name": "name", "ref": "@c1"}])
    kit = TX.FakeAppKit({1: ("com.apple.finder", "Finder")}, front="com.apple.finder")
    app, on_press, els = TX.focus_app("name")
    TX.install(app, kit=kit, on_press=on_press)
    t = KeyTree(pid=1)
    out = run(DC.run_steps(rec, {"name": "Neu"}, executor=DC.checked_executor(t), approver=yes,
                           decide_as_driving=True))
    assert out["ok"], out["stopped"]
    assert els["name"].attrs["AXValue"] == "Neu" and t.keys == [36]


def test_a_focus_that_moved_between_the_typing_and_the_return_is_refused_and_nothing_is_pressed():
    rec = record_of(folder_draft(), [{"name": "name", "ref": "@c1"}])
    kit = TX.FakeAppKit({1: ("com.apple.finder", "Finder")}, front="com.apple.finder")
    for move_to in ("search", "other"):          # another field of the window; a field of another window
        app, on_press, els = TX.focus_app("name")
        fake, _kit = TX.install(app, kit=kit, on_press=on_press)
        setter = fake.AXUIElementSetAttributeValue

        def moving(el, name, value, _set=setter, _els=els, _app=app, _to=move_to):
            err = _set(el, name, value)
            if el is _els["name"] and name in ("AXValue", "AXSelectedText"):
                _app.attrs["AXFocusedUIElement"] = _els[_to]      # the app moved the focus on typing
            return err
        fake.AXUIElementSetAttributeValue = moving
        t = KeyTree(pid=1)
        out = run(DC.run_steps(rec, {"name": "Neu"}, executor=DC.checked_executor(t), approver=yes,
                               decide_as_driving=True))
        assert not out["ok"] and "the focus moved after the focused step before this one" in out["stopped"], \
            (move_to, out["stopped"])
        assert t.keys == [] and els["name"].attrs["AXValue"] == "Neu"
        last = out["steps"][-1]
        assert last["index"] == 2 and last.get("reason_code") == "identity", last


def test_the_click_trigger_rule_is_unchanged():
    rec = record_of(folder_draft(), [{"name": "name", "ref": "@c1"}])
    kit = TX.FakeAppKit({1: ("com.apple.finder", "Finder")}, front="com.apple.finder")
    for move_to, words in ((None, "did not move"), ("other", "another window")):
        app, on_press, els = TX.focus_app(move_to)
        TX.install(app, kit=kit, on_press=on_press)
        t = KeyTree(pid=1)
        out = run(DC.run_steps(rec, {"name": "Neu"}, executor=DC.checked_executor(t), approver=yes,
                               decide_as_driving=True))
        assert not out["ok"] and words in out["stopped"], out["stopped"]
        assert els["search"].attrs["AXValue"] == "" and els["name"].attrs["AXValue"] == "" and t.keys == []


def test_the_proof_world_keeps_the_focus_on_the_typed_field_for_the_return():
    d = folder_draft()
    rep = compile_(d, [{"name": "name", "ref": "@c1"}])
    assert rep["status"] == "ready", rep["reasons"]
    rec = record_of(d, [{"name": "name", "ref": "@c1"}])
    world = PR.SimAXWorld(rec)
    out = run(DC.run_steps(rec, {"name": "Neu 7"}, executor=DC.checked_executor(world), approver=yes,
                           decide_as_driving=True, on_step=world.at_step))
    assert out["ok"], out["stopped"]
    # the Return went to the element the name was typed into: it still has the focus
    assert world.focused is not None and world.focused.value == "Neu 7"
    assert [e["op"] for e in world.effects] == ["AXPress", "enter_text", "key"]


# ── (b) the proof never invents a text mode's start ──────────────────────────────────────────

AREA = {"canonical_role": "textarea", "name": "", "identifier": "First Text View", "path": "", "raw_role": "AXTextArea"}
SAVE = {"canonical_role": "menuitem", "name": "Sichern", "identifier": "saveDocument:", "path": "", "raw_role": "AXMenuItem"}
#: the recorded document's text: only its shape may reach the record (it is never in a draft here)
DOC = "Bericht Q3\nStatus: offen\nVerteiler: Team"
#: the watcher's anchor: a typed_text marker (document text, never released into the compile)
MK = "[[ent:typed_text:0123456789ab]]"


def edit_draft(anchor="Status:", start=None, mode="replace_line", **extra):
    """A replace_line (or append) into TextEdit's text, then save. ``start``: ``data.start``, the
    field's shape when the recording focused it (as the watcher records it)."""
    data = dict({"action": "type", "text": "Status: erledigt" if mode == "replace_line" else " und mehr",
                 "selector": dict(AREA), "input_mode": mode}, **extra)
    if mode == "replace_line":
        data["anchor"] = anchor
    if start is not None:
        data["start"] = start
    return draft("edit_status", [(data, "edit the status line"),
                                 ({"action": "click", "selector": dict(SAVE)}, "save")])


def test_a_wrong_anchor_fails_the_proof_and_the_record_stays_draft():
    """NG02 of the map compile tasks: the plan anchored at a phrase no line of the document held."""
    for start in ({"lines": 3, "anchors": {"damit alles": []}},   # the recorded document: in no line
                  {"lines": 3, "anchors": {"Status:": [1]}}):     # a shape of ANOTHER anchor's lines
        rep = compile_(edit_draft(anchor="damit alles", start=start), [{"name": "status", "ref": "@c1"}])
        assert not rep["proof"]["passed"] and rep["status"] == "draft", rep["reasons"]
        assert "the anchor line is not in the recorded document" in rep["proof"]["reason"], rep["proof"]["reason"]


def test_a_right_anchor_in_the_recorded_shape_passes_and_the_record_keeps_no_text():
    st = WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-hooks-")))
    shape = {"lines": 3, "anchors": {"Status:": [1]}}            # AX.start_shape(DOC, ["Status:"])
    rep = compile_(edit_draft(start=shape), [{"name": "status", "ref": "@c1"}], save=True, st=st)
    assert rep["proof"]["passed"] and rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("edit_status", st)
    assert rec["recorded"][0]["start"] == shape and "start" not in rec["steps"][0]
    blob = json.dumps(rec, ensure_ascii=False)
    assert "Bericht Q3" not in blob and "Verteiler" not in blob
    world = PR.SimAXWorld(rec)                       # three lines, the anchor in the second
    area = next(e for e in world.elements if e.sel.get("identifier") == "First Text View")
    assert area.value == "\nStatus:\n"
    # two lines with the anchor: the run refuses it as live would (ambiguous), the proof fails
    rep2 = compile_(edit_draft(start={"lines": 2, "anchors": {"Status:": [0, 1]}}),
                    [{"name": "status", "ref": "@c1"}])
    assert not rep2["proof"]["passed"] and "2 lines of the field match the anchor line" in rep2["proof"]["reason"]


def test_the_watchers_marker_anchor_is_proved_with_each_cases_value_in_the_recorded_line():
    """The watcher's form: the anchor is a typed_text marker, the
    compiler's parameter ``anker_1``; ``data.start`` keys its line by the marker. Each case puts
    ITS anchor value in that line; no text of the document is in the draft, the record or the
    proof."""
    shape = {"lines": 4, "anchors": {MK: [2]}}
    st = WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-hooks-")))
    rep = compile_(edit_draft(anchor=MK, start=shape), [{"name": "status", "ref": "@c1"}], save=True, st=st)
    assert rep["proof"]["passed"] and rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("edit_status", st)
    anker = next(p for p in rec["parameters"] if p.get("anchor"))
    assert rec["steps"][0]["command"]["anchor"] == "${%s}" % anker["name"]
    assert rec["recorded"][0]["command"]["anchor"] == MK and rec["recorded"][0]["start"] == shape
    vals = {"status": "Status: zu", anker["name"]: "Kapitel 9"}
    world = PR.SimAXWorld(rec)
    world.add_row_values(vals)
    area = next(e for e in world.elements if e.sel.get("identifier") == "First Text View")
    assert area.value == "\n\nKapitel 9\n"                # the case's anchor, in the recorded line
    out = run(DC.run_steps(rec, vals, executor=DC.checked_executor(world), approver=yes, decide_as_driving=True,
                           on_step=world.at_step))
    assert out["ok"], out["stopped"]
    assert area.value == "\n\nStatus: zu\n"
    # the marker's line not recorded (another anchor's shape, or none): never invented
    rep = compile_(edit_draft(anchor=MK, start={"lines": 4, "anchors": {}}), [{"name": "status", "ref": "@c1"}])
    assert rep["status"] == "draft" and "the anchor line is not in the recorded document" in rep["proof"]["reason"]


def test_without_a_recorded_start_shape_replace_line_and_append_stay_draft_with_the_reason():
    for mode in ("replace_line", "append"):
        rep = compile_(edit_draft(mode=mode), [{"name": "status", "ref": "@c1"}])
        assert rep["status"] == "draft" and not rep["proof"]["passed"], (mode, rep["reasons"])
        want = "replace_line" if mode == "replace_line" else "append_text"
        assert f"{want}: the recording kept no start value of the field" in rep["proof"]["reason"], \
            rep["proof"]["reason"]
        assert any("start value" in r for r in rep["reasons"]), rep["reasons"]
    rep = compile_(edit_draft(anchor=MK), [{"name": "status", "ref": "@c1"}])
    assert rep["status"] == "draft" and "kept no start value" in rep["proof"]["reason"]
    # an append with a recorded shape proves (the field starts from it, nothing invented)
    rep = compile_(edit_draft(mode="append", start={"lines": 1, "anchors": {}}), [{"name": "status", "ref": "@c1"}])
    assert rep["proof"]["passed"] and rep["status"] == "ready", rep["reasons"]
    # the draft schema refuses a start that is not a shape
    try:
        DC.load_source(edit_draft(start={"lines": "drei", "anchors": {}}))
        raise AssertionError("a start with a text line count was accepted")
    except DC.MalformedDraft as exc:
        assert "start.lines" in str(exc)


def _vault(tmp):
    from cryptography.fernet import Fernet
    from anyuse.watcher.entity_vault import EntityVault
    return EntityVault(db_path=pathlib.Path(tmp) / "vault.db", k_tok=b"\x07" * 32, k_enc=Fernet.generate_key(),
                       presence_provider=lambda reason: True)


def _use_vault(vault):
    from anyuse.watcher import entity_vault as ev
    ev._default_vault, ev._default_vault_failed = vault, vault is None


def test_an_old_field_value_is_never_read_even_under_the_label_grant():
    """Privacy: the field's old value is not released under the standing ``ui_label``
    grant. A ``value_before`` marker (an older capture's form) is ignored: never dereferenced,
    no start from it, the record stays draft."""
    from anyuse.watcher.entity_vault import marker
    tmp = tempfile.mkdtemp(prefix="anyuse-hooks-vault-")
    vault = _vault(tmp)
    SC.ensure_label_grant(vault)
    token = vault.tokenize(DOC, SC.LABEL_CLASS)
    asked = []
    real = vault.deref

    def spy(tok, grantee, purpose=""):
        asked.append(tok)
        return real(tok, grantee, purpose)
    vault.deref = spy
    try:
        _use_vault(vault)
        d = edit_draft(value_before=marker(SC.LABEL_CLASS, token))
        src = DC.load_source(d)
        assert src.steps[0].start is None
        rep = compile_(d, [{"name": "status", "ref": "@c1"}])
        assert rep["status"] == "draft" and "kept no start value" in rep["proof"]["reason"], rep["reasons"]
        assert token not in asked, asked
        assert "Bericht" not in json.dumps(rep, ensure_ascii=False, default=str)
    finally:
        _use_vault(None)


def test_the_shape_holds_no_text_and_rebuilds_a_value_of_the_same_shape():
    assert AX.start_shape(DOC, ["Status:", "damit alles"]) == {"lines": 3, "anchors": {"Status:": [1], "damit alles": []}}
    assert AX.start_shape("Status: offen\nStatus: alt", ["Status:"]) == {"lines": 2, "anchors": {"Status:": [0, 1]}}
    shape = AX.start_shape("eins\r\nStatus: x\nStatus-Info\n", ["Status:", "fehlt"])
    assert shape == {"lines": 4, "anchors": {"Status:": [1], "fehlt": []}}
    own = AX.valid_start(shape, "Status:")                  # the step's own anchor only
    assert own == {"lines": 4, "anchors": {"Status:": [1]}}
    assert AX.valid_start(shape, "") == {"lines": 4, "anchors": {}}
    assert AX.valid_start({"lines": 2, "anchors": {"a": [5]}}, "a") is None          # a line it does not have
    v = AX.start_value(own)
    assert v == "\nStatus:\n\n" and AX.start_shape(v, ["Status:"]) == own
    assert AX.start_value(own, {"Status:": "Kapitel 9"}) == "\nKapitel 9\n\n"
    assert AX.start_value({"lines": 2, "anchors": {"a": [0], "b": [1]}}, {"a": "x", "b": "x"}) is None  # overlap
    assert AX.start_problem(own, "Status:") == ""
    assert AX.start_problem(own, "fehlt") == "replace_line: the anchor line is not in the recorded document"
    other = AX.start_shape("a\x0bb", ["a"])
    assert other["breaks"] == "other" and "line break" in AX.start_problem(other, "a")
    assert AX.start_problem(other, "", "append_text") == ""
    assert "kept no start value" in AX.start_problem(None, "x")
    # a live edit reports the start without the field's text and without its anchor
    t = types_tree("Kopf\nStatus: offen")
    out = AX.replace_line(t, "el", {"input_data": "Status: zu"}, {"action": "replace_line", "anchor": "Status:"})
    assert out["success"] and out["start"] == {"lines": 2, "anchor_lines": [1]}
    assert "Kopf" not in json.dumps(out) and "Status: offen" not in json.dumps(out)
    assert AX.keyed_start(out["start"], "Status:") == {"lines": 2, "anchors": {"Status:": [1]}}


def types_tree(value):
    class _T:
        def __init__(self):
            self.value = value

        def read_value(self, _el):
            return self.value

        def edit_text(self, _el, text, replace_range=None, expect_value=None):
            self.value = expect_value
            return PR._SimEdit(self.value, True)
    return _T()


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if getattr(fn, "__module__", "") != __name__:
            continue
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            import traceback
            f += 1; print("FAIL", n, "::", repr(e)[:600]); traceback.print_exc()
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f or p != 11 else 0)
