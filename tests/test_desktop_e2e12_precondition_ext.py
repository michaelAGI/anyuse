"""The fix found by the GF10 probe, offline with fakes: the DECLARED precondition of Notes'
view items, version-gated (``Arch12(precondition13=True)``; the default is the plain Arch12).

A guest job read "Als Liste" AXEnabled after every step of GF10: disabled at its clean start (a note opened from
the gallery, Arch8's ``gallery_item_open``), during the search (Arch12's ``notes_search_active``) and again after
the search was left, because Notes brings the opened gallery note back; leaving the gallery state as well enabled
it. The fake guest below behaves that way: its ``precondition`` op leaves ONE declared state per call (the search
first), as the real op does.

Pinned here: with the flag every declared state present is left (never one twice, at most one call per state), the
press is retried ONCE, and what was left is re-established in reverse order (the gallery's note shown again, then
the search entered again: it must hold); without the flag the plain Arch12 fails exactly as recorded; the gallery
state alone (GD05-like) still works; a state that will not go away ends in a failed step, never a loop; the host's
re-run builds both arms with the flag. No desktop, no AX, no subprocess, no network.
"""
import importlib
import os
import sys

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z

import test_desktop_e2e4 as T4
import test_desktop_e2e6 as T6
import test_desktop_e2e11 as T11
import test_desktop_e2e12_precondition as T12

EMAP = W.EffectMap4()
S, plan4, FakePlanner4, NOTES = T4.S, T4.plan4, T4.FakePlanner4, T4.NOTES
LIST_KEY, GAL_KEY, GF10, NOTES_GF10 = T12.LIST_KEY, T12.GAL_KEY, T12.GF10, T12.NOTES_GF10
VIEW_IDS = (LIST_KEY[3:], GAL_KEY[3:])


class SearchGalleryGuest(T6.FakeGuest6):
    """GF10's start as the guest saw it: the gallery view with a note OPENED from it (the grid hidden). Notes disables
    its view items while a search is active or a gallery note is open; leaving the search brings the opened gallery
    note back; the ``precondition`` op leaves one declared state per call (``stuck``: the search never ends)."""

    def __init__(self, *a, stuck=False, **kw):
        super().__init__(*a, **kw)
        self.view, self.item_open, self.stuck, self.calls13 = GAL_KEY, True, stuck, []
        self.item_before_search = False

    def op_marks(self, bundle, parent, options):
        if bundle == NOTES:
            keys = [o["key"] for o in options]
            return {"status": "done", "ok": True, "target": {"options": keys, "selected": [self.view]}, "sentinel": []}
        return super().op_marks(bundle, parent, options)

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if bundle == NOTES and "search_field" in target:
            target = {"focused": True, "search": True}
        if bundle == NOTES and target.get("search"):
            self.item_before_search, self.item_open = self.item_open, False     # the hits as a grid
        return super().op_enter(bundle, target, text, commit, keys)

    def op_select(self, bundle, kind, label, via=None):
        shown, same = self.shown_note, label == self.notes_folder
        r = super().op_select(bundle, kind, label, via)
        if bundle == NOTES and kind == "notes_folders" and same and self.item_open:
            self.shown_note = shown              # the folder shown again keeps its opened gallery note
        if bundle == NOTES and kind == "notes_notes" and not self.notes_search:
            self.item_open = self.view == GAL_KEY                                 # a gallery cell opens the note
        return r

    def op_press(self, bundle, trigger, label):
        if bundle == NOTES and trigger.get("identifier") in VIEW_IDS:
            if self.notes_search or self.item_open:
                return {"status": "failed", "detail": f"{label.split(' > ')[-1]!r} is disabled"}
            self.view = LIST_KEY if trigger["identifier"] == LIST_KEY[3:] else GAL_KEY
            return {"status": "done"}
        return super().op_press(bundle, trigger, label)

    def op_precondition(self, bundle):
        if bundle == NOTES and self.notes_search:
            pattern = self.notes_search
            self.calls13.append("notes_search_active")
            if not self.stuck:
                self.notes_search, self.notes_folder = "", self.folder_before_search or self.notes_folder
                self.folder_before_search = None
                self.item_open, self.shown_note = self.item_before_search, "Kaffee in der Werkstatt"
            return {"status": "done", "restored": "notes_search_active", "shown_before": pattern,
                    "reshow": "notes_search"}
        if bundle == NOTES and self.item_open:
            self.calls13.append("gallery_item_open")
            self.item_open = False
            return {"status": "done", "restored": "gallery_item_open", "shown_before": self.shown_note,
                    "reshow": "notes_notes"}
        self.calls13.append("unchanged")
        return {"status": "unchanged", "restored": None, "detail": "no declared state found"}


PLAN_GF10 = plan4(S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Ersatzdichtung"),
                  S("toggle_select", "Notes", "Darstellung", "Als Liste"),
                  S("open_item", "Notes", "Zettel für die kleine Presse"))


def _run(flag=True, stuck=False, plan=PLAN_GF10):
    g = SearchGalleryGuest(notes={k: list(v) for k, v in NOTES_GF10.items()}, stuck=stuck)
    g.front, g.notes_folder, g.shown_note = NOTES, "Werkstatt Dichtungen", "Kaffee in der Werkstatt"
    rec = X.Rec("GF10", "arch", 1)
    kw = {"precondition13": True} if flag else {}
    Z.Arch12(g, EMAP, planner=FakePlanner4(plan), config11=T11.cfg(), jchooser=T11.ConfOracle("Handpresse", 0.99),
             spend=None, inbox=None, sleep=lambda s: None, **kw).run(X.Task("GF10", GF10, "single-app",
                                                                           {"app": NOTES}, [], {}), rec)
    return g, rec


def _toggle(rec):
    return next(s for s in rec.steps if s["step"]["effect"] == "toggle_select")


def test_the_view_items_declare_both_states_and_the_flag_defaults_off():
    assert Z.ITEM_PRECONDITIONS13 == {(NOTES, "Darstellung > Als Liste"): ("notes_search_active", "gallery_item_open"),
                                      (NOTES, "Darstellung > Als Galerie"): ("notes_search_active", "gallery_item_open")}
    ids = {d["id"] for d in Z.PRECONDITIONS12[NOTES]}
    assert all(set(v) <= ids for v in Z.ITEM_PRECONDITIONS13.values())         # only states the guest can leave
    assert Z.Arch12(T6.FakeGuest6(), EMAP).precondition13 is False


def test_gf10_like_both_states_left_one_retry_then_reestablished_in_reverse():
    g, rec = _run()
    tog = _toggle(rec)
    assert tog["outcome"] == "ok", (rec.stopped, [s.get("why") for s in rec.steps])
    pc = tog["precondition"]
    assert [x["state"] for x in pc["left13"]] == ["notes_search_active", "gallery_item_open"]
    assert pc["retry"] == "done" and pc["folder12"] == {"label": "Werkstatt Dichtungen", "status": "done"}
    assert pc["search_again12"] == {"pattern": "Ersatzdichtung", "status": "done", "holds": True}
    assert pc["reshown"]["label"] == "Kaffee in der Werkstatt"
    # the press, the search left, its folder shown again, the gallery note left, the ONE retry; then in reverse:
    # the gallery's note (still shown in the list), the search entered again
    assert [a["op"] for a in tog["actions"]] == ["press", "precondition", "select", "precondition", "press", "enter"]
    assert g.calls13 == ["notes_search_active", "gallery_item_open"] and rec.precondition_retries == 1
    assert g.view == LIST_KEY and g.notes_search == "Ersatzdichtung"
    assert not rec.stopped and g.shown_note == "Handpresse"                     # the plan's open_item afterwards
    assert any(f.get("fix") == "precondition13" for f in rec.fixes12)


def test_without_the_flag_prior_arm_fails_without_precondition():
    g, rec = _run(flag=False)
    tog = _toggle(rec)
    assert rec.stopped.startswith("failed step") and "disabled" in rec.stopped
    pc = tog["precondition"]
    assert "left13" not in pc and pc["restored"] == "notes_search_active" and pc["retry"] == "failed"
    assert [a["op"] for a in tog["actions"]] == ["press", "precondition", "select", "press", "enter"]
    assert g.view == GAL_KEY and "gallery_item_open" not in g.calls13


def test_the_gallery_state_alone_is_left_and_its_note_shown_again_gd05_like():
    plan = plan4(S("launch_app", "Notes"), S("toggle_select", "Notes", "Darstellung", "Als Liste"))
    g, rec = _run(plan=plan)
    tog = _toggle(rec)
    assert tog["outcome"] == "ok" and g.view == LIST_KEY and g.notes_search == ""
    pc = tog["precondition"]
    assert [x["state"] for x in pc["left13"]] == ["gallery_item_open"] and pc["unchanged13"]
    assert "search_again12" not in pc and pc["reshown"]["label"] == "Kaffee in der Werkstatt"
    assert g.calls13 == ["gallery_item_open", "unchanged"] and rec.precondition_retries == 1


def test_a_state_that_will_not_go_away_is_a_failed_step_never_a_loop():
    g, rec = _run(stuck=True)
    tog = _toggle(rec)
    assert rec.stopped.startswith("failed step")
    pc = tog["precondition"]
    assert [x["state"] for x in pc["left13"]] == ["notes_search_active"] and "reported twice" in pc["stopped13"]
    assert pc["retry"] == "failed"
    presses = [a for a in tog["actions"] if a["op"] == "press"]
    assert len(presses) == 2 and len([a for a in tog["actions"] if a["op"] == "precondition"]) == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
