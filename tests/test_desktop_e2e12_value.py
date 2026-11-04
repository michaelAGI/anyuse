"""Arch12, offline with fakes: a value's provenance is checked when it
is bound, and a run is reported done only with checked values.

Arch11's two value errors:
* GF14 — "Use the wording after 'Confirmed:', not the backup location": the planner read the note "River
  equipment" and saved its "boat shed" into the briefing (the right note says "Confirmed: North gate beside the
  stone bridge. Backup: Boat shed.");
* GF02 — "das Wort für dieses Aufschieben": the planner read the only note of the wrong "Sprachzettel" folder,
  looked up its "Trödeln" and reported the run done.

Pinned here: a label the user names for the value must stand in the source with the value right after it; a value
carried from a note in the goal's own language must come from a text that carries the user's words for it; the
value's clause must not carry what the user excludes ("Backup: …", "Erst 14 Kisten geschätzt"). A failed check
unbinds the value BEFORE any lookup or write, drops that source for the run and asks the planner once more with
the observation (its next read gets a fresh variable); a second failure hands off; the completion check reports a
run done only when every delivered value passed and a lookup's field holds exactly that term. No desktop, no AX,
no subprocess, no network.
"""
from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
NOTES, TE, DICT, ROOT = T4.NOTES, T4.TE, T4.DICT, T4.ROOT

GF14 = ("Put the confirmed collection point from my river-cleanup note into the open briefing, on a new line between "
        "the meeting time and the equipment. Use the wording after 'Confirmed:', not the backup location. Save the "
        "briefing.")
RIVER = "River collection\nConfirmed: North gate beside the stone bridge. Backup: Boat shed."
EQUIP = "River equipment\nCollect spare gloves from the boat shed before the event."
GF02 = ("In meinen Sprachzetteln stand doch das Wort für dieses Aufschieben. Such mir das mal im deutschen Duden "
        "raus, nicht die englische Übersetzung. Lass die passende Notiz offen.")
GF06 = ("Kannst du die Kistenzahl aus der Abholnotiz für den Hinterhof in den offenen Packzettel übernehmen? Nur die "
        "endgültige Zahl, nicht die Schätzung. Sonst nix ändern, bitte sichern.")


def test_the_label_the_user_names_must_stand_in_the_source_with_the_value_after_it():
    assert Z.value_labels12(GF14) == ["Confirmed:"]
    assert Z.value_labels12("Don't put it beside the 'Current' folder.") == []
    assert Z.value_labels12("Such mal in den Notizen nach „Ersatzdichtung“.") == []           # a search, no label
    assert Z.value_labels12("Nimm den Text nach „Bestätigt:“.") == ["Bestätigt:"]
    assert Z.value_problems12(GF14, "North gate beside the stone bridge.", RIVER) == []
    p = Z.value_problems12(GF14, "boat shed", EQUIP)
    assert len(p) == 1 and "does not contain 'Confirmed:'" in p[0]
    p2 = Z.value_problems12(GF14, "Boat shed", RIVER)
    assert any("not the text after 'Confirmed:'" in x for x in p2) and any("backup" in x for x in p2)


def test_a_carried_value_comes_from_a_text_with_the_users_words_and_not_from_an_excluded_clause():
    assert Z.value_problems12(GF02, "Prokrastination", "Morgen reicht auch\nDas ständige Aufschieben heißt "
                                                         "Prokrastination.") == []
    p = Z.value_problems12(GF02, "Trödeln", "Später anfangen\nIm Roman heißt das Verhalten einfach Trödeln.")
    assert p and "none of the user's words" in p[0]
    assert "englische" in Z.value_problems12(GF02, "procrastination", "Englischer Vortrag\nAufschieben auf Englisch: "
                                                                      "procrastination.")[0]
    src = "Hinterhof bestätigt\nErst 14 Kisten geschätzt; endgültig 17 Kisten und 4 Säcke."
    assert Z.value_problems12(GF06, "17", src) == [] and "schatzung" in Z.value_problems12(GF06, "14", src)[0]
    # another language than the goal is not judged by words (GA02: "squeaky hinge" / "Scharnier")
    assert Z.value_problems12("Find the note about the squeaky hinge, then look up the German word I wrote for it.",
                              "Scharnier", "Quietschendes Scharnier\nDas Scharnier an der Küchentür quietscht.") == []
    # the document the run edits is not judged by words either (the old value of an edit)
    assert Z.value_problems12("In Notes_Meeting.txt, change the date on the second line to October 2.",
                              "September 25, 2026", "Team Sync\nSeptember 25, 2026\nAttendees", carried=False) == []


def test_a_replans_read_gets_a_fresh_variable_and_its_references_follow():
    steps = [S("open_item", "Notes", "River collection"), S("read_content", "Notes", "note", "note_text"),
             S("extract", "none", "x", "cp", find="{{note_text}}")]
    out = Z.fresh_reads12(steps, ["note_text", "doc_text"])
    assert out[1]["value"] == "note_text_2" and out[2]["find"] == "{{note_text_2}}" and out[0] == steps[0]
    assert Z.fresh_reads12(steps, ["other"]) == steps


def _gf14_guest():
    return T9.FakeGuest9({"R/volunteer-briefing.txt": "River cleanup\nMeeting: 09:00\nEquipment: gloves and bags"},
                         shown="R", te_front="R/volunteer-briefing.txt",
                         notes={"Volunteer Logistics": [("River collection", RIVER), ("River equipment", EQUIP)]})


def arch12(guest, planner, js=None):
    return Z.Arch12(guest, EMAP, planner=planner, config11=T11.cfg(), jchooser=js or T11.ConfOracle("x"), spend=None,
                    inbox=None, sleep=lambda s: None)


def test_gf14_like_the_wrong_source_is_refused_before_the_write_and_the_replan_reads_the_right_note():
    g = _gf14_guest()
    p = FakePlanner4(
        plan4(S("launch_app", "Notes"), S("open_item", "Notes", "River equipment"),
              S("read_content", "Notes", "note", "note_text")),
        plan4(S("extract", "none", "the confirmed collection point", "cp", find="boat shed"),
              S("doc_save", "TextEdit", "", "{{cp}}", mode="insert", find="Meeting: 09:00")),
        plan4(S("open_item", "Notes", "River collection"), S("read_content", "Notes", "note", "note_text")),
        plan4(S("extract", "none", "the confirmed collection point", "cp", find="North gate beside the stone bridge."),
              S("doc_save", "TextEdit", "", "{{cp}}", mode="insert", find="Meeting: 09:00")))
    rec = X.Rec("GF14", "arch", 1)
    arch12(g, p).run(X.Task("GF14", GF14, "cross-app", {"app": NOTES}, [], {}), rec)
    assert g.fs[f"{ROOT}/R/volunteer-briefing.txt"] == \
        "River cleanup\nMeeting: 09:00\nNorth gate beside the stone bridge.\nEquipment: gloves and bags", \
        (rec.stopped, [s.get("why") for s in rec.steps])
    assert not rec.stopped and g.shown_note == "River collection" and rec.value_replans12 == 1
    assert rec.fresh_reads12 == 1 and [p_["problems"] == [] for p_ in rec.provenance12] == [False, True]
    fb = p.histories[2][-1]["content"]
    assert "'boat shed'" in fb and "the note 'River equipment'" in fb and "NEW variable" in fb
    assert len([s for s in rec.steps if s["step"]["effect"] == "doc_save"]) == 1      # nothing written before


def _gf02_guest():
    g = T9.FakeGuest9(notes={"Sprachzettel Seminar": [("Morgen reicht auch", "Morgen reicht auch\nDas ständige "
                                                       "Aufschieben heißt Prokrastination."),
                                                      ("Englischer Vortrag", "Englischer Vortrag\nAufschieben auf "
                                                       "Englisch: procrastination.")],
                             "Sprachzettel Lektüre": [("Später anfangen", "Später anfangen\nIm Roman heißt das "
                                                       "Verhalten einfach Trödeln.")]})
    g.notes_folder = "Sprachzettel Lektüre"
    g.front = NOTES
    return g


P1_GF02 = plan4(S("launch_app", "Notes"), S("navigate", "Notes", "Sprachzettel Lektüre"),
                S("open_item", "Notes", "Später anfangen"), S("read_content", "Notes", "note", "text"))
# Arch11's chooser accepted the wrong folder's only note at 0.99 / 0.99; after the re-plan it names the right one
PICKS = T11.ConfOracle(by_ask=[("Später anfangen", 0.99)] * 2 + [("Morgen reicht auch", 0.99)] * 2)
LOOKUP = lambda w: plan4(S("extract", "none", "das Wort für das Aufschieben", "word", find=w),  # noqa: E731
                         S("launch_app", "Dictionary"), S("scoped_find", "Dictionary", "", "{{word}}"))


def test_gf02_like_the_wrong_word_is_never_looked_up_and_the_replan_finds_the_right_note():
    g = _gf02_guest()
    p = FakePlanner4(P1_GF02, LOOKUP("Trödeln"),
                     plan4(S("navigate", "Notes", "Sprachzettel Seminar"), S("open_item", "Notes", "Aufschieben"),
                           S("read_content", "Notes", "note", "text")),
                     LOOKUP("Prokrastination"))
    rec = X.Rec("GF02", "arch", 1)
    arch12(g, p, PICKS).run(X.Task("GF02", GF02, "cross-app", {"app": NOTES}, [], {}), rec)
    assert g.dict_search == "Prokrastination" and not rec.stopped, (rec.stopped, [s.get("why") for s in rec.steps])
    assert g.shown_note == "Morgen reicht auch" and "completion12" not in rec.__dict__
    fb = p.histories[2][-1]["content"]
    assert "'Sprachzettel Seminar'" in fb                                      # the folders that share the user's words
    assert "The folder shown, 'Sprachzettel Lektüre', holds no other note" in fb
    assert all(a.get("text") != "Trödeln" for op, a in g.log if op == "enter")


def test_a_second_wrong_source_hands_off_and_nothing_is_looked_up():
    g = _gf02_guest()
    p = FakePlanner4(P1_GF02, LOOKUP("Trödeln"), plan4(S("open_item", "Notes", "Später anfangen"),
                                                       S("read_content", "Notes", "note", "text")))
    rec = X.Rec("GF02", "arch", 1)
    arch12(g, p, T11.ConfOracle("Später anfangen", 0.99)).run(X.Task("GF02", GF02, "cross-app", {"app": NOTES}, [], {}),
                                                            rec)
    assert g.dict_search == "" and rec.stopped.startswith("hand-off") and rec.value_replans12 == 1
    assert any(s.get("source_dropped12") == ["Später anfangen"] for s in rec.steps)


def test_the_completion_check_needs_the_lookup_to_hold_exactly_the_checked_term():
    g = _gf02_guest()
    g.notes_folder = "Sprachzettel Seminar"
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("open_item", "Notes", "Morgen reicht auch"),
                           S("read_content", "Notes", "note", "text")), LOOKUP("Prokrastination"))
    real = g.op_search_fields
    n = {"k": 0}

    def later_other(bundle):
        r = real(bundle)
        if bundle == DICT and g.dict_search:
            n["k"] += 1
            if n["k"] > 1:                               # the field changed after the step's own check
                r = {"status": "done", "fields": [{"index": 0, "text": "Suchen", "value": "Trödeln"}]}
        return r
    g.op_search_fields = later_other
    rec = X.Rec("GF02", "arch", 1)
    arch12(g, p, T11.ConfOracle("Morgen reicht auch", 0.99)).run(X.Task("GF02", GF02, "cross-app", {"app": NOTES}, [], {}), rec)
    assert rec.stopped.startswith("completion check:") and rec.status != "completed" and "Trödeln" in rec.stopped


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
