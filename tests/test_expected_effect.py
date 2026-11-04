"""A declared expected effect decides a step's success.

The product path used to report success from the actuator alone (the AX diff
was "NEVER used to override"). Now a step's ``command["expect"]``
(``execution/step_effect.py``) is checked after the action: missing -> the step
is ``uncertain_completion`` (looked for again for a short while, never acted
on twice), anything outside it -> ``deviation``; both stop the run. A recorded
transition's to_state is part of the effect on recorded-graph replay, and the
recorder declares the demonstrated effect. Fakes only (a snapshot model of the
screen; the real ``AccessibilityTree.diff``).

Run from the anyuse package dir:
    cd anyuse && python3 test_expected_effect.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"   # fakes only

from anyuse.execution import step_effect as SE  # noqa: E402
from anyuse.execution.deterministic import replay_path  # noqa: E402
from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.plan_types import ExecutionPlan, PlannedStep  # noqa: E402
from anyuse.guides.state_graph import (  # noqa: E402
    StateGraph, TransitionImpl, UIFingerprint, UIState, compute_structural_hash)
from anyuse.memory.models import WorkflowIntent, WorkflowTemplate  # noqa: E402
from anyuse.context.accessibility import AccessibilityTree, AXElementDesc, AXSnapshot  # noqa: E402

StateGraphExecutor._effect_sleep = staticmethod(lambda s: None)   # no real waiting in tests


class World:
    """The screen as a list of AXElementDesc; actions run scripted effects."""

    def __init__(self, els, on_press=None, on_write=None, late=0):
        self.els = els
        self.on_press, self.on_write = on_press, on_write
        self.late = late                   # snapshots before a pending effect lands
        self.pending = None
        self.writes, self.presses, self.snaps = [], [], 0

    def snapshot(self):
        self.snaps += 1
        if self.pending is not None:
            if self.late <= 0:
                self.pending(), setattr(self, "pending", None)
            else:
                self.late -= 1
        return AXSnapshot(elements=[AXElementDesc(**vars(e)) for e in self.els])

    def diff(self, before, after):
        return AccessibilityTree.diff(self, before, after)

    def _el(self, ident=None, title=None):
        return next(e for e in self.els if (ident and e.identifier == ident) or (title and e.title == title))

    def find_selector(self, sel):
        return next((e for e in self.els if (sel.identifier and e.identifier == sel.identifier)
                     or (not sel.identifier and sel.name and sel.name == (e.title or e.ax_description))), None)

    def to_selector(self, el):
        return el.to_selector()

    def find(self, role=None, title=None):
        return [e for e in self.els if (role is None or e.role == role) and (title is None or e.title == title)]

    def set_value(self, el, value):
        self.writes.append(value)
        el.value = value
        if self.on_write:
            self.on_write(self, value)
        return True

    def perform_action(self, el, action):
        self.presses.append(el.title or el.identifier)
        if self.on_press:
            effect = lambda: self.on_press(self, el)   # noqa: E731
            if self.late:
                self.pending = effect
            else:
                effect()
        return True


def _field(name="Betreff", value=""):
    return AXElementDesc(role="AXTextField", title=name, value=value)


def _write_step(world, field, value, expect):
    cmd = {"role": "AXTextField", "action": "set_value", "selector": field.to_selector().to_dict(),
           "expect": expect}
    return PlannedStep(intent="type", source="workflow", target_element="", modality="ax",
                       command=cmd, input_data=value)


WRITE = {"changes": [{"op": "value", "target": True, "value": SE.INPUT}], "permitted": [], "only": True}


def test_a_write_whose_effect_is_seen_succeeds():
    f = _field()
    w = World([f, _field("An", "a@b.de"), AXElementDesc(role="AXScrollBar", value="0")])
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_write_step(w, f, "Hallo", WRITE)))
    assert res["success"] is True and ver["effect"]["kind"] == "observed", (res, ver.get("effect"))


def test_a_write_that_also_changed_another_field_is_a_deviation():
    f, other = _field(), _field("An", "a@b.de")
    w = World([f, other], on_write=lambda w, v: setattr(other, "value", "x@y.de"))
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_write_step(w, f, "Hallo", WRITE)))
    assert res["success"] is False and res["outcome_kind"] == "deviation", res
    assert "An" in res["error"] and res["acted"] is True and w.writes == ["Hallo"]


def test_noise_never_counts_as_a_deviation():
    f, bar = _field(), AXElementDesc(role="AXScrollBar", value="0")
    menu = AXElementDesc(role="AXMenuItem", title="Rückgängig", in_menubar=True)
    w = World([f, bar, menu], on_write=lambda w, v: (setattr(bar, "value", "0.4"),
                                                     setattr(menu, "title", "Rückgängig: Tippen")))
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_write_step(w, f, "Hallo", WRITE)))
    assert res["success"] is True, (res, ver["effect"])


def test_set_value_is_read_back_plus_the_expected_effect():
    """The actuator read the value back, but the screen afterwards does not show it."""
    f = _field()
    w = World([f], on_write=lambda w, v: setattr(f, "value", ""))   # the app threw it away
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_write_step(w, f, "Hallo", WRITE)))
    assert res["outcome_kind"] == "uncertain_completion" and res["reason_code"] == "effect_not_observed", res
    assert w.writes == ["Hallo"], "a missing effect is never answered with a second write"


SHEET = {"changes": [{"op": "added", "role": "AXSheet"}], "permitted": [
    {"op": "any", "role": "AXButton", "name": "Sichern"}, {"op": "any", "role": "AXTextField"}], "only": True}


def _press(name, expect):
    sel = AXElementDesc(role="AXButton", title=name).to_selector().to_dict()
    return PlannedStep(intent=f"press {name}", source="workflow", target_element="", modality="ax",
                       command={"role": "AXButton", "action": "press", "selector": sel, "expect": expect})


def _opens_sheet(w, el):
    w.els += [AXElementDesc(role="AXSheet", title="Sichern"), AXElementDesc(role="AXButton", title="Sichern"),
              AXElementDesc(role="AXTextField", title="Sichern unter:", value="Ohne Titel")]


def test_a_press_whose_sheet_opens_succeeds_even_when_it_opens_late():
    w = World([AXElementDesc(role="AXButton", title="Ablage")], on_press=_opens_sheet, late=2)
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_press("Ablage", SHEET)))
    assert res["success"] is True and w.presses == ["Ablage"], (res, ver["effect"])
    assert w.snaps >= 3, "the screen was read again, the press was not repeated"


def test_a_press_whose_effect_never_comes_is_uncertain_and_not_repeated():
    w = World([AXElementDesc(role="AXButton", title="Ablage")])
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_press("Ablage", SHEET)))
    assert res["outcome_kind"] == "uncertain_completion" and w.presses == ["Ablage"], res
    assert ver["effect"]["missing"] == [{"op": "added", "role": "AXSheet"}], ver["effect"]


def test_a_press_that_opens_something_else_is_a_deviation():
    w = World([AXElementDesc(role="AXButton", title="Ablage")],
              on_press=lambda w, el: w.els.append(AXElementDesc(role="AXButton", title="In den Papierkorb")))
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_press("Ablage", SHEET)))
    assert res["outcome_kind"] == "deviation" and "Papierkorb" in res["error"], res


def test_an_undeclared_step_keeps_the_actuator_evidence():
    w = World([AXElementDesc(role="AXButton", title="OK")])
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_press("OK", None)))
    assert res["success"] is True and ver["effect"] == {"kind": "undeclared"}


def test_a_declared_effect_with_no_snapshots_is_not_assumed():
    class Blind(World):
        def snapshot(self):
            raise RuntimeError("no AX snapshot")
    w = Blind([AXElementDesc(role="AXButton", title="Ablage")])
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(_press("Ablage", SHEET)))
    assert res["outcome_kind"] == "uncertain_completion", res


def _state(sid, rows):
    els = [{"role": r, "title": t} for r, t in rows]
    return UIState(state_id=sid, app="App", times_seen=2, fingerprint=UIFingerprint(
        required_elements=els, element_count_range=(max(0, len(els) - 1), len(els) + 1),
        structural_hash=compute_structural_hash(els)))


def test_recorded_replay_checks_the_transition_to_state():
    g = StateGraph()
    g.add_state(_state("list", [("AXButton", "Neu")]))
    g.add_state(_state("editor", [("AXButton", "Neu"), ("AXButton", "Fertig")]))
    sel = AXElementDesc(role="AXButton", title="Neu").to_selector().to_dict()
    g.add_implementation("list", "editor", "new", TransitionImpl(
        modality="ax", command={"role": "AXButton", "action": "press", "selector": sel}))
    good = World([AXElementDesc(role="AXButton", title="Neu")],
                 on_press=lambda w, el: w.els.append(AXElementDesc(role="AXButton", title="Fertig")))
    ok = asyncio.run(replay_path(g, "list", "editor", StateGraphExecutor(ax_tree=good)))
    assert ok["success"] is True, ok
    stuck = World([AXElementDesc(role="AXButton", title="Neu")])
    out = asyncio.run(replay_path(g, "list", "editor", StateGraphExecutor(ax_tree=stuck)))
    assert out["outcome_kind"] == "uncertain_completion" and "editor" in out["reason"], out
    assert [t["modality"] for t in out["steps"][0]["tried"]] == ["ax"] and stuck.presses == ["Neu"]


class _NoIntervention:
    async def intervene(self, **kw):
        raise AssertionError("a deviation stops; it does not ask")


def test_the_planned_path_stops_on_a_deviation():
    f, other = _field(), _field("An", "a@b.de")
    w = World([f, other], on_write=lambda w, v: setattr(other, "value", "x@y.de"))
    ex = StateGraphExecutor(ax_tree=w, intervention_handler=_NoIntervention())
    steps = [_write_step(w, f, "Hallo", WRITE), _press("Senden", None)]
    wf = WorkflowTemplate.create_new("t", "t")
    wf.steps = [WorkflowIntent(intent=s.intent, target_element="", optional=True) for s in steps]
    res = asyncio.run(ex.execute(ExecutionPlan(current_state="s0", steps=steps), wf, StateGraph()))
    assert res.success is False and res.stopped["outcome_kind"] == "deviation" and len(res.steps) == 1


def test_effect_from_diff_declares_what_a_replay_with_new_values_then_observes():
    f = _field()
    rec = World([f, AXElementDesc(role="AXStaticText", value="0 Zeichen")],
                on_write=lambda w, v: setattr(w.els[1], "value", f"{len(v)} Zeichen"))
    before = rec.snapshot()
    rec.set_value(f, "recorded")
    expect = SE.effect_from_diff(rec.diff(before, rec.snapshot()), target=f.to_selector(), action="set_value")
    assert expect["changes"] == [{"op": "value", "target": True, "value": SE.INPUT}], expect
    assert {"op": "any", "role": "AXStaticText"} in expect["permitted"], expect
    f2 = _field()
    w = World([f2, AXElementDesc(role="AXStaticText", value="0 Zeichen")],
              on_write=lambda w, v: setattr(w.els[1], "value", f"{len(v)} Zeichen"))
    res, ver = asyncio.run(StateGraphExecutor(ax_tree=w).execute_checked_step(
        _write_step(w, f2, "ein neuer Wert", expect)))
    assert res["success"] is True and ver["effect"]["kind"] == "observed", (res, ver["effect"])


def test_the_recorder_declares_the_demonstrated_effect():
    from anyuse.memory.recorder import StateGraphRecorder
    rec = StateGraphRecorder.__new__(StateGraphRecorder)
    w = World([AXElementDesc(role="AXButton", title="Ablage")], on_press=_opens_sheet)
    rec._ax_tree = w
    before = w.snapshot()
    w.perform_action(w.els[0], "AXPress")
    sel = AXElementDesc(role="AXButton", title="Ablage").to_selector().to_dict()
    impls = [TransitionImpl(modality="ax", command={"action": "press", "selector": sel}),
             TransitionImpl(modality="coordinate", command={"action": "click_at", "selector": sel}),
             TransitionImpl(modality="js", command={"script": "x"})]
    rec._declare_effects(impls, before, w.snapshot())
    assert impls[0].command["expect"]["changes"] == [{"op": "added", "role": "AXSheet"}]
    assert impls[1].command["expect"] == impls[0].command["expect"]
    assert "expect" not in impls[2].command


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
