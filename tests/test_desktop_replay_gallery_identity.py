"""The checked replay's identity check reads the note rows in EVERY Notes view (``desktop_routine.ReplayArch``),
offline with fakes.

GG06: Notes starts in the gallery view with a note OPENED from it (the grid hidden). The replay's identity
check read the note list with a plain read, got no rows, and refused every variant before acting ("'X' is not in
'Abholnotizen Werkstatt'", the recorded note included): 6 false refusals, nothing done. The step itself asks the
guest for its candidates with ``back`` (the guest goes back to the grid). Pinned here:

* GG06's world (the pool's notes, the gallery with "Blaue Kiste komplett" opened, the open list in TextEdit): a
  routine compiled in the list view replays its variants in the gallery start: identity confirmed after going back
  to the grid, the right number written;
* never act without identity: when the grid does not come back, the replay refuses with the reason (no rows read)
  and does nothing; a note that is not in the folder is still refused; a name taken twice is still refused;
* the check before a human gate reads the note SHOWN with a plain read (it never goes back, which would hide it).

No desktop, no AX, no subprocess, no network.
"""
import sys

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z
from anyuse.execution import desktop_routine as R

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
ROOT = T4.ROOT

# the pool's GG06
GG06_GOAL = ("Nimm bitte aus meinen Abholnotizen die Nummer für die blaue Kiste, die ohne Deckel. Trag die in der "
             "offenen Liste statt OFFEN ein und speicher.")
FOLDER = "Abholnotizen Werkstatt"
GG06_NOTES = {FOLDER: [("Blaue Kiste komplett", "Blaue Kiste komplett\nBlaue Kiste mit Deckel: Nummer BK-704."),
                       ("Blaue Kiste offen", "Blaue Kiste offen\nBlaue Kiste ohne Deckel: Nummer BK-740."),
                       ("Grüne Kiste offen", "Grüne Kiste offen\nGrüne Kiste ohne Deckel: Nummer GK-740.")]}
DOC = "Abholung Werkstatt/Abholliste.txt"
TREE = {DOC: "Abholung Werkstatt\nKistennummer: OFFEN\nTermin: vereinbart"}
PLAN1 = plan4(S("launch_app", "Notes"), S("navigate", "Notes", FOLDER),
              S("open_item", "Notes", "die blaue Kiste ohne Deckel"), S("read_content", "Notes", "note", "note_text"))
PLAN2 = plan4(S("extract", "none", "die Nummer der blauen Kiste ohne Deckel", "number", find="BK-740"),
              S("launch_app", "TextEdit"), S("doc_save", "TextEdit", "", "{{number}}", mode="edit", find="OFFEN"))


class GalleryGuest(T9.FakeGuest9):
    """Notes in the gallery view with a note OPENED from it (the grid hidden): a plain read of the note list gives
    no rows (the guest's ``note_rows``); asked for the candidates (``back``) the guest goes back to the grid, the
    note is no longer shown; selecting a gallery cell opens the note again. ``grid_returns=False``: going back
    fails (the grid stays hidden)."""

    def __init__(self, *a, opened=None, grid_returns=True, **kw):
        super().__init__(*a, **kw)
        self.shown_note, self.item_open, self.grid_returns, self.backs = opened, opened is not None, grid_returns, 0

    def op_rows(self, bundle, kind, back=None):
        if kind != "notes_notes":
            return super().op_rows(bundle, kind, back)
        went = False
        if self.item_open and back and self.grid_returns:
            self.item_open, self.shown_note, went = False, None, True
            self.backs += 1
        r = super().op_rows(bundle, kind, back)
        if self.item_open:
            r = dict(r, rows=[])
        return dict(r, went_back=went, shown_note=r.get("shown_note") or "")

    def op_select(self, bundle, kind, label, via=None):
        r = super().op_select(bundle, kind, label, via)
        if kind == "notes_notes":
            self.item_open = True                       # a gallery cell opens its note
        return r


def _arch12(g, planner, want="Blaue Kiste offen"):
    return Z.Arch12(g, EMAP, planner=planner, config11=T11.cfg(), jchooser=T11.ConfOracle(want, 0.95), spend=None,
                    inbox=None, sleep=lambda s: None, precondition13=True)


def _routine():
    g = T9.FakeGuest9(TREE, shown=None, notes=GG06_NOTES, te_front=DOC)
    g.front, g.notes_folder = T4.NOTES, FOLDER
    rec = X.Rec("GG06", "arch12", 1)
    _arch12(g, FakePlanner4(PLAN1, PLAN2)).run(X.Task("GG06", GG06_GOAL, "cross-app", {}, [], {}), rec)
    assert rec.status == "completed", (rec.stopped, rec.steps[-1])
    assert g.fs[f"{ROOT}/{DOC}"] == "Abholung Werkstatt\nKistennummer: BK-740\nTermin: vereinbart"
    return R.derive_routine(rec.compiled, GG06_GOAL, task="GG06", arm="arch12")


def _replay(routine, values, **kw):
    g = GalleryGuest(TREE, shown=None, notes=GG06_NOTES, te_front=DOC, opened="Blaue Kiste komplett", **kw)
    g.front, g.notes_folder = T4.NOTES, FOLDER
    a = R.ReplayArch(g, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    steps, goal, probs = a.prepare(routine, values)
    assert probs == []
    rec = X.Rec("GG06", "replay", 1, mode="compiled")
    a.run(X.Task("GG06", goal, "cross-app", {}, [], {}), rec, compiled=steps)
    return g, rec


def test_gg06s_variants_replay_in_the_gallery_start_identity_read_after_going_back():
    r = _routine()
    ps = {p["name"]: p for p in r["params"]}
    assert ps["note"]["value"] == "Blaue Kiste offen" and ps["detail"]["value"] == "BK-740"
    for note, detail in (("Grüne Kiste offen", "GK-740"), ("Blaue Kiste komplett", "BK-704"),
                         ("Blaue Kiste offen", "BK-740")):
        g, rec = _replay(r, {"note": note, "detail": detail})
        assert rec.status == "completed" and not rec.identity15, (note, rec.stopped, rec.identity15)
        assert g.fs[f"{ROOT}/{DOC}"] == f"Abholung Werkstatt\nKistennummer: {detail}\nTermin: vereinbart"
        assert g.shown_note == note and g.backs == 1 and sum(rec.calls.values()) == 0
        op = next(s for s in rec.steps if s["step"]["effect"] == "open_item")
        assert op["identity15"] == {"ok": True, "went_back": True}


def test_never_act_without_identity_the_grid_that_does_not_come_back_is_a_refusal():
    r = _routine()
    g, rec = _replay(r, {"note": "Grüne Kiste offen", "detail": "GK-740"}, grid_returns=False)
    assert rec.stopped.startswith("failed step: identity check: no note rows could be read in")
    assert "'Blaue Kiste komplett' is shown" in rec.stopped and "not confirmed" in rec.stopped
    assert g.fs[f"{ROOT}/{DOC}"] == TREE[DOC] and not any(op == "select" for op, _a in g.log)


def test_a_note_not_in_the_folder_or_taken_twice_is_still_refused():
    r = _routine()
    g, rec = _replay(r, {"note": "Rote Kiste offen", "detail": "RK-1"})
    assert "'Rote Kiste offen' is not in 'Abholnotizen Werkstatt'" in rec.stopped
    assert g.fs[f"{ROOT}/{DOC}"] == TREE[DOC]
    twice = {FOLDER: GG06_NOTES[FOLDER] + [("Grüne Kiste offen", "Grüne Kiste offen\nDoppelt: GK-999.")]}
    g = GalleryGuest(TREE, shown=None, notes=twice, te_front=DOC, opened="Blaue Kiste komplett")
    g.front, g.notes_folder = T4.NOTES, FOLDER
    a = R.ReplayArch(g, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    steps, goal, _ = a.prepare(r, {"note": "Grüne Kiste offen", "detail": "GK-740"})
    rec = X.Rec("GG06", "replay", 1, mode="compiled")
    a.run(X.Task("GG06", goal, "cross-app", {}, [], {}), rec, compiled=steps)
    assert "2 items are named 'Grüne Kiste offen'" in rec.stopped and g.fs[f"{ROOT}/{DOC}"] == TREE[DOC]


def test_the_check_before_a_gate_reads_the_note_shown_and_never_goes_back():
    g = GalleryGuest(TREE, shown=None, notes=GG06_NOTES, te_front=DOC, opened="Blaue Kiste offen")
    g.front, g.notes_folder = T4.NOTES, FOLDER
    a = R.ReplayArch(g, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    a._bound15 = {f"item:{T4.NOTES}": "Blaue Kiste offen"}
    assert a._gate15(S("human_gate", "Notes", "Blaue Kiste offen", "delete")) is None
    assert g.backs == 0 and g.shown_note == "Blaue Kiste offen"
    assert all(not a_.get("back") for op, a_ in g.log if op == "rows")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
