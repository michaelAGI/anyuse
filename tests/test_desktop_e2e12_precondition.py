"""Arch12, offline with fakes: Notes' active search is a declared
precondition.

Arch11 on GF10: "Such mal in den Notizen nach „Ersatzdichtung“ … Zeig mir den als Liste und lass die Suche
stehen." After the search, "Als Liste" was disabled (Notes disables its view items while a search is active) and no
declared precondition covered it: a failed step.

Pinned here: the state is declared (``PRECONDITIONS12``); the guest's ``precondition`` op leaves the search and
reports its pattern; Arch8's mechanism retries the press ONCE; the host then enters the same search again (the
user asked to leave it standing) and checks that the field holds it; a failed re-entry is a failed step, never a
silent success. The guest op's source is pinned (leave the search through the checked executor: Escape, the
field's clear button behind the irreversible-action guard, the emptied field). No desktop, no AX, no subprocess,
no network.
"""
import os

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z

import test_desktop_e2e4 as T4
import test_desktop_e2e6 as T6
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
NOTES = T4.NOTES
VIEW = next(e for e in EMAP.entries("Notes", "toggle_select") if e.get("group") == "Darstellung")
LIST_KEY = next(o["key"] for o in VIEW["options"] if o["label"] == "Als Liste")
GAL_KEY = next(o["key"] for o in VIEW["options"] if o["label"] == "Als Galerie")

GF10 = ("Such mal in den Notizen nach „Ersatzdichtung“. Ich brauch den Zettel für die kleine Presse, nicht für die "
        "Kaffeemaschine. Zeig mir den als Liste und lass die Suche stehen.")
NOTES_GF10 = {"Werkstatt Dichtungen": [("Handpresse", "Handpresse\nErsatzdichtung für die kleine Presse: 18 mm."),
                                       ("Standpresse", "Standpresse\nErsatzdichtung für die große Presse: 28 mm."),
                                       ("Kaffee in der Werkstatt", "Kaffee in der Werkstatt\nErsatzdichtung für die "
                                                                   "Kaffeemaschine: 18 mm.")]}


class SearchViewGuest(T6.FakeGuest6):
    """Notes in the gallery view; while a search is active Notes disables its view items; the declared state is
    left by emptying the search field (``reenter`` False: the search cannot be entered again)."""

    def __init__(self, *a, declared=True, reenter=True, **kw):
        super().__init__(*a, **kw)
        self.view, self.declared, self.reenter, self.left = GAL_KEY, declared, reenter, 0

    def op_marks(self, bundle, parent, options):
        if bundle == NOTES:
            keys = [o["key"] for o in options]
            return {"status": "done", "ok": True, "target": {"options": keys, "selected": [self.view]}, "sentinel": []}
        return super().op_marks(bundle, parent, options)

    def op_press(self, bundle, trigger, label):
        if bundle == NOTES and trigger.get("identifier") in (LIST_KEY[3:], GAL_KEY[3:]):
            if self.notes_search or self.notes_folder == T6.SCOPE_ALL:      # the scope stays selected
                return {"status": "failed", "detail": f"{label.split(' > ')[-1]!r} is disabled"}
            self.view = LIST_KEY if trigger["identifier"] == LIST_KEY[3:] else GAL_KEY
            return {"status": "done"}
        return super().op_press(bundle, trigger, label)

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if bundle == NOTES and "search_field" in target:
            if not self.reenter:
                return {"status": "failed", "detail": "the field did not take the text"}
            return super().op_enter(bundle, {"focused": True, "search": True}, text, commit, keys)
        return super().op_enter(bundle, target, text, commit, keys)

    def op_precondition(self, bundle):
        if bundle == NOTES and self.notes_search and self.declared:
            pattern, self.notes_search, self.left = self.notes_search, "", self.left + 1
            return {"status": "done", "restored": "notes_search_active", "shown_before": pattern,
                    "reshow": "notes_search"}
        return {"status": "unchanged", "restored": None, "detail": "no declared state found"}


PLAN_GF10 = plan4(S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Ersatzdichtung"),
                  S("toggle_select", "Notes", "Darstellung", "Als Liste"),
                  S("open_item", "Notes", "Zettel für die kleine Presse"))


def _run(**kw):
    g = SearchViewGuest(notes={k: list(v) for k, v in NOTES_GF10.items()}, **kw)
    g.front, g.shown_note = NOTES, "Kaffee in der Werkstatt"
    rec = X.Rec("GF10", "arch", 1)
    Z.Arch12(g, EMAP, planner=FakePlanner4(PLAN_GF10), config11=T11.cfg(),
             jchooser=T11.ConfOracle("Handpresse", 0.99), spend=None,
             inbox=None, sleep=lambda s: None).run(X.Task("GF10", GF10, "single-app", {"app": NOTES}, [], {}), rec)
    return g, rec


def test_the_search_state_is_declared():
    ids = [d["id"] for d in Z.PRECONDITIONS12["com.apple.Notes"]]
    assert ids == ["gallery_item_open", "notes_search_active"]
    assert Z.PRECONDITIONS12["com.apple.finder"] == Z.R8.PRECONDITIONS["com.apple.finder"]


def test_gf10_like_the_search_is_left_the_press_retried_once_and_the_search_entered_again():
    g, rec = _run()
    tog = next(s for s in rec.steps if s["step"]["effect"] == "toggle_select")
    assert tog["outcome"] == "ok", (rec.stopped, [s.get("why") for s in rec.steps])
    pc = tog["precondition"]
    assert pc["restored"] == "notes_search_active" and pc["retry"] == "done"
    assert pc["search_again12"] == {"pattern": "Ersatzdichtung", "status": "done", "holds": True}
    assert [a["op"] for a in tog["actions"]] == ["press", "precondition", "select", "press", "enter"]
    assert pc["folder12"] == {"label": "Werkstatt Dichtungen", "status": "done"}
    assert g.view == LIST_KEY and g.notes_search == "Ersatzdichtung" and g.left == 1
    assert not rec.stopped and g.shown_note == "Handpresse" and rec.precondition_retries == 1


def test_a_search_that_cannot_be_entered_again_is_a_failed_step_not_a_success():
    g, rec = _run(reenter=False)
    tog = next(s for s in rec.steps if s["step"]["effect"] == "toggle_select")
    assert tog["outcome"] == "failed" and "could not be entered again" in tog["why"]
    assert tog["precondition"]["search_again12"]["holds"] is False


def test_without_the_declared_state_the_disabled_item_fails_as_in_prior_version():
    g, rec = _run(declared=False)
    assert rec.stopped.startswith("failed step") and g.view == GAL_KEY and "disabled" in rec.stopped


def test_the_gallery_state_is_still_left_and_its_note_shown_again_through_arch12():
    import test_desktop_e2e8 as T8
    g, p = T8._gd05()
    rec = X.Rec("GD05", "arch", 1)
    Z.Arch12(g, EMAP, planner=p, config11=T11.cfg(), jchooser=T11.ConfOracle("Tor |", 0.99), spend=None, inbox=None,
             sleep=lambda s: None).run(X.Task("GD05", T8.GD05, "single-app", {"app": NOTES}, [], {}), rec)
    last = rec.steps[-1]
    assert not rec.stopped and g.view == T8.LIST_KEY and g.shown_note == "Tor", (rec.stopped, last)
    assert last["precondition"]["restored"] == "gallery_item_open" and last["precondition"]["retry"] == "done"
    assert [a["op"] for a in last["actions"]] == ["press", "precondition", "press", "select"]


def test_the_guest_leaves_the_search_through_the_checked_executor():
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    pre = src[src.index("def op_precondition(a):"):src.index("OPS = {")]
    assert '"restored": "notes_search_active"' in pre and '"reshow": "notes_search"' in pre
    assert "_leave_search(pid)" in pre and "guard_before(pid" in pre and "guard_after(pid" in pre
    leave = src[src.index("def _leave_search(pid):"):src.index("def op_precondition(a):")]
    assert 'EX.run(el, pid, G.record_selector(pid, el), "press_key", keycode=53)' in leave
    assert "X.irreversible_reason(lab)" in leave and "AXClearButton" in leave
    assert "AXUIElementSetAttributeValue" not in leave            # never around the executor


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
