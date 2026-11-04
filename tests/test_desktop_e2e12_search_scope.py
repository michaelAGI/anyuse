"""Arch12, offline with fakes: a search's results keep their own folder.

Arch11 on SF07: after the asked-for search "Zahnarzt", Notes lists the hits of every folder under the search
scope "Alle auf meinem Mac"; the filter took that scope as every row's container and contradicted ALL of them
against the folder the user named (Gesundheit_Log): a hand-off without a model call.

Pinned here: a result row lies in the folder its own "in <folder>" cell names (the guest's search-row format); the
search scope is no container (a row without its own folder is "unknown", never contradicted by the scope); a
container NAME the user gives that is observed in full as a row's folder contradicts a row whose folder matches it
only in part ("Termine Gesundheit" for "Gesundheit_Log"); outside a search nothing changes. No desktop, no AX, no
subprocess, no network.
"""
import datetime as dt

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e9 as N
from anyuse.execution import desktop_e2e12 as Z

import test_desktop_e2e4 as T4
import test_desktop_e2e6 as T6
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
NOTES = T4.NOTES
NOW = dt.datetime(2026, 10, 1, 12, 0)
SCOPE = T6.SCOPE_ALL
ConfOracle, cfg = T11.ConfOracle, T11.cfg

SF07 = "Such mal in den Notizen nach Zahnarzt und mach meine Notiz aus dem Ordner Gesundheit_Log auf, nicht die von " \
       "den Kindern."
STEP_SF07 = {"effect": "open_item", "target": "meine Notiz aus dem Ordner Gesundheit_Log", "scope": "Gesundheit_Log"}
NOTES_SF07 = {"Gesundheit_Log": [("Zahnarzt Termin", "Zahnarzt Termin\nKontrolle am 8. Oktober, Praxis Dr. Lenz"),
                                 ("Hausarzt", "Hausarzt\nBlutwerte abholen")],
              "Alltag_Sonstiges": [("Zahnarzt Kinder", "Zahnarzt Kinder\nMia und Ben im November")],
              "Termine Gesundheit": [("Zahnarzt Kosten", "Zahnarzt Kosten\n…Zahnarzt Krone: 640 Euro")],
              "AnyUse-Pick": [("Zahnarzt", "Zahnarzt\nKontrolltermin am 14. Oktober")]}


def rows_of(notes, q="zahnarzt"):
    """The guest's search rows: title | match snippet | in <folder>."""
    return [{"label": t, "text": f"{t} | {b.split(chr(10))[1]} | in {f}"} for f, ns in notes.items() for t, b in ns
            if q in b.casefold()]


def statuses(rows, where, goal=SF07, step=STEP_SF07):
    rows2, where2 = Z.search_rows12(rows, where)
    ks = Z.goal_constraints12(goal, step, "notes_notes", NOW, where2, [r["label"] for r in rows], "de")
    return Z.evaluate12(ks, [N.candidate_fields(r, NOW, "notes_notes") for r in rows2], where2)


def test_a_search_row_names_its_own_folder_and_the_scope_is_no_container():
    assert Z.search_parent12("Zahnarzt Termin | Kontrolle am 8. Oktober | in Gesundheit_Log") == "Gesundheit_Log"
    assert Z.search_parent12("Zahnarzt Termin") == "" and Z.search_parent12("a | Innen streichen") == ""
    rows, where = Z.search_rows12(rows_of(NOTES_SF07), SCOPE)
    assert where == "" and [r["parent"] for r in rows] == ["Gesundheit_Log", "Alltag_Sonstiges", "Termine Gesundheit",
                                                          "AnyUse-Pick"]
    rows2, where2 = Z.search_rows12(rows_of(NOTES_SF07), "Gesundheit_Log")      # no search: unchanged
    assert where2 == "Gesundheit_Log" and not any(r.get("parent") for r in rows2)


def test_sf07_the_right_note_is_alive_the_others_are_contradicted_by_their_own_folder():
    st = statuses(rows_of(NOTES_SF07), SCOPE)
    labels = [r["label"] for r in rows_of(NOTES_SF07)]
    alive = [lab for lab, s in zip(labels, st) if not any(v[0] == "contradicted" for v in s.values())]
    assert alive == ["Zahnarzt Termin"]
    assert st[0]["container"][0] == "satisfied"
    assert "Gesundheit_Log" in st[2]["container"][1]          # a partial match of the named folder: contradicted
    # the Arch10 filter's view (the scope as every row's container): everything contradicted
    k10 = Z.T.goal_constraints10(SF07, STEP_SF07, "notes_notes", NOW, SCOPE, labels, "de")
    st10 = Z.T.evaluate10(k10, [N.candidate_fields(r, NOW, "notes_notes") for r in rows_of(NOTES_SF07)], SCOPE)
    assert all(s["container"][0] == "contradicted" for s in st10)


def test_a_row_without_its_own_folder_is_unknown_under_a_search_scope():
    st = statuses([{"label": "Zahnarzt Termin", "text": "Zahnarzt Termin | Kontrolle"}], SCOPE)
    assert st[0]["container"][0] == "unknown"


class FakeSearch12(T6.FakeGuest6):
    """``FakeGuest6`` with the guest's search-row format (title | snippet | in <folder>)."""

    def op_rows(self, bundle, kind, back=None):
        if kind == "notes_notes" and self.notes_search:
            self.front = NOTES
            q = X.norm(self.notes_search)
            return {"status": "done", "where": self.notes_folder, "shown_note": self.shown_note,
                    "rows": [{"label": t, "text": f"{t} | {b.split(chr(10))[1]} | in {f}", "selected": t == self.shown_note}
                             for f, ns in self.notes.items() for t, b in ns if q in X.norm(b)]}
        return super().op_rows(bundle, kind, back)


def test_sf07_like_the_arm_opens_the_note_of_the_named_folder_after_the_search():
    g = FakeSearch12(notes=dict(NOTES_SF07))
    g.notes_folder = "Alltag_Sonstiges"
    g.shown_note = "Zahnarzt Kinder"
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("navigate", "Notes", "Gesundheit_Log"),
                           S("scoped_find", "Notes", "", "Zahnarzt"),
                           S("open_item", "Notes", STEP_SF07["target"], sc="Gesundheit_Log")))
    js = ConfOracle("Zahnarzt Termin", 0.99)
    rec = X.Rec("SF07", "arch", 1)
    Z.Arch12(g, EMAP, planner=p, config11=cfg(t2=0.9), jchooser=js, spend=None, inbox=None,
             sleep=lambda s: None).run(X.Task("SF07", SF07, "single-app", {"app": NOTES}, [], {}), rec)
    assert not rec.stopped and g.shown_note == "Zahnarzt Termin" and g.notes_search == "Zahnarzt", \
        (rec.stopped, [s.get("why") for s in rec.steps])
    step = next(s for s in rec.steps if s.get("pick"))
    assert step["search_scope12"]["scope"] == SCOPE and step["pick"]["alive"] == ["C1"]
    assert [c for c, _t in js.asked[0][2]] == ["C1"]          # only the right note is asked about


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
