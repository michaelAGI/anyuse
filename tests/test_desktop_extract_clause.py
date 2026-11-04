"""The extracted value ends at its clause, not at the sentence punctuation that closes the sentence it stands in
(``desktop_fixes``: ``extract_clause`` / ``clause_end`` / ``line_value`` and the layer ``DesktopFixes``),
offline with fakes.

GG09 (arm E, Arch8's own part): the note reads "Agreed collection point: West courtyard."; the plan's
extract named the words "West courtyard." (with the note's period) and the checklist was saved as "Collection
point: West courtyard." -- the right place, an extra '.' on disk. Pinned here:

* GG09's world (the note and the checklist as recorded), through Arch12 WITH the layer and
  its compiled replay (the routine replay carries the layer): the saved line is "Collection point: West courtyard";
  the frozen Arch12 without the layer is byte-for-byte what it was (the period is written, as recorded);
* the period stays when it is part of the requested value: the field asks for a sentence / line / wording, the span
  is a whole sentence or line, an abbreviation or initial ("Co.", "z.B.", "J."), an ellipsis, punctuation inside a
  token, an ordinal followed by its month;
* other clause punctuation (! ? ; : ,) at a clause end is left out too; a clause inside a sentence (GA12)
  as well; the offsets are those of the NFC text (a decomposed note is sliced right);
* where the value is written as a line of its own (append / insert / replace ending with the variable) the source's
  sentence punctuation is part of the requested value and comes back (GA12 and GF14 were
  authored so); an edit writes INTO an existing line, whose own punctuation stays (GG09).

No desktop, no AX, no subprocess, no network.
"""
import sys
import unicodedata

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e5 as V
from anyuse.execution import desktop_e2e12 as Z
from anyuse.execution import desktop_fixes as F2
from anyuse.execution import desktop_routine as R

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
ROOT = T4.ROOT

# GG09, as recorded (the arm-E run)
GG09_GOAL = ("The open checklist needs the agreed collection point for the return trip, not where we set off. Take "
             "it from the trip notes, replace TBD, and save. Leave that note visible.")
GG09_NOTES = {"Museum Trip Notes": [("Outbound confirmed", "Outbound confirmed\nAgreed collection point: North entrance."),
                                    ("Return proposal", "Return proposal\nSuggested collection point: East entrance."),
                                    ("Return confirmed", "Return confirmed\nAgreed collection point: West courtyard.")]}
GG09_DOC = "Museum Trip Checklist/Return-checklist.txt"
GG09_TREE = {GG09_DOC: "Return trip\nCollection point: TBD\nCount bags before leaving."}
GG09_PLAN1 = plan4(S("launch_app", "Notes"), S("navigate", "Notes", "Museum Trip Notes"),
                   S("open_item", "Notes", "Return confirmed"), S("read_content", "Notes", "note", "note_text"))
GG09_PLAN2 = plan4(S("extract", "none", "the agreed collection point", "collection_point", find="West courtyard."),
                   S("launch_app", "TextEdit"),
                   S("doc_save", "TextEdit", "", "{{collection_point}}", mode="edit", find="TBD"))


def _gg09_guest():
    return T9.FakeGuest9(GG09_TREE, shown=None, notes=GG09_NOTES, te_front=GG09_DOC)


Arch12F = F2.with_desktop_fixes(Z.Arch12)


def _arch12(g, planner, cls=Arch12F):
    # the note is described, not named by the user: the picker (a fake that answers the right note) decides
    return cls(g, EMAP, planner=planner, config11=T11.cfg(), jchooser=T11.ConfOracle("Return confirmed", 0.95),
               spend=None, inbox=None, sleep=lambda s: None, precondition13=True)


def test_gg09s_world_the_saved_line_has_no_sentence_period():
    g = _gg09_guest()
    rec = X.Rec("GG09", "arch12", 1)
    _arch12(g, FakePlanner4(GG09_PLAN1, GG09_PLAN2)).run(X.Task("GG09", GG09_GOAL, "cross-app", {}, [], {}), rec)
    assert rec.status == "completed", (rec.stopped, rec.steps[-1])
    assert g.fs[f"{ROOT}/{GG09_DOC}"] == "Return trip\nCollection point: West courtyard\nCount bags before leaving."
    ex = next(s for s in rec.steps if s["step"]["effect"] == "extract")
    assert ex["resolved"]["label"] == "West courtyard" and "'.' is not part of the value" in ex["check"]
    assert ex["extract"]["closing"] == "." and not any(s.get("line_value") for s in rec.steps)
    # the compiled replay extracts again from the text re-read: the same value, 0 model calls
    g2 = _gg09_guest()
    rec2 = X.Rec("GG09", "arch12", 1, mode="compiled")
    _arch12(g2, None).run(X.Task("GG09", GG09_GOAL, "cross-app", {}, [], {}), rec2, compiled=rec.compiled)
    assert rec2.status == "completed" and rec2.calls["planner"] == 0
    assert g2.fs[f"{ROOT}/{GG09_DOC}"] == g.fs[f"{ROOT}/{GG09_DOC}"]
    # the routine and its checked replay (the product path: ReplayArch carries the layer), on a new detail
    rt = R.derive_routine(rec.compiled, GG09_GOAL, task="GG09", arm="arch12")
    g3 = _gg09_guest()
    a = R.ReplayArch(g3, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    steps, goal, probs = a.prepare(rt, {})
    rec3 = X.Rec("GG09", "replay", 1, mode="compiled")
    a.run(X.Task("GG09", goal, "cross-app", {}, [], {}), rec3, compiled=steps)
    assert rec3.status == "completed" and g3.fs[f"{ROOT}/{GG09_DOC}"] == g.fs[f"{ROOT}/{GG09_DOC}"]


def test_prior_frozen_arm_without_the_layer_is_unchanged():
    g = _gg09_guest()
    rec = X.Rec("GG09", "arch12", 1)
    _arch12(g, FakePlanner4(GG09_PLAN1, GG09_PLAN2), cls=Z.Arch12).run(
        X.Task("GG09", GG09_GOAL, "cross-app", {}, [], {}), rec)
    assert rec.status == "completed"
    assert g.fs[f"{ROOT}/{GG09_DOC}"] == "Return trip\nCollection point: West courtyard.\nCount bags before leaving."


def test_the_note_text_of_gg09_alone():
    text = GG09_NOTES["Museum Trip Notes"][2][1]
    assert F2.extract_clause([text], "West courtyard.", "the agreed collection point")[::2] == ("West courtyard", ".")
    assert F2.extract_clause([text], "West courtyard", "the agreed collection point") == ("West courtyard", "exact", "")
    # Arch5's function is unchanged (the earlier arms are frozen)
    assert V.extract_detail([text], "West courtyard.", "x") == ("West courtyard.", "exact")


def test_the_period_stays_when_it_is_part_of_the_requested_value():
    cases = [
        # the field asks for a sentence, line or wording
        ("Approved wording: Please keep the doorway clear.", "Please keep the doorway clear.", "the approved wording"),
        ("Hinweis\nBitte leise sein: Prüfung.", "Prüfung.", "the closing sentence"),
        # the span is a whole sentence / line
        ("Room sign\nPlease keep the doorway clear.", "Please keep the doorway clear.", "what the sign says"),
        ("Stand: offen. Bitte bis Freitag melden.", "Bitte bis Freitag melden.", "the request"),
        # abbreviations and initials
        ("Lieferant: Müller & Co.", "Müller & Co.", "the supplier"),
        ("Beispiel: Kisten, Folie usw.", "Kisten, Folie usw.", "the list"),
        ("Autor: Tolkien, J.", "Tolkien, J.", "the author"),
        ("Format: siehe z.B.", "siehe z.B.", "x"),
        # an ellipsis, a token cut inside, an ordinal followed by its month
        ("Status: Fortsetzung folgt...", "Fortsetzung folgt...", "the status"),
        ("Status: wird geprüft…", "wird geprüft…", "the status"),
        ("Version 3.5 freigegeben.", "Version 3.", "the version"),
        ("Konzert am 3. Dezember, 19 Uhr", "am 3.", "the day"),
    ]
    for text, words, field in cases:
        span, how, left = F2.extract_clause(["Titel\n" + text if "\n" not in text else text], words, field)
        assert span == words and left == "", (text, words, field, span, how)
        assert "not part of the value" not in how


def test_other_clause_punctuation_is_left_out_and_a_clause_inside_a_sentence_too():
    cases = [
        ("Treffpunkt: Haupteingang!", "Haupteingang!", "Haupteingang", "the meeting point"),
        ("Code: LN-S17, Menge: 6 m", "LN-S17,", "LN-S17", "the code"),
        ("Frage: Wer bringt die Kisten?", "Wer bringt die Kisten?", "Wer bringt die Kisten", "x"),
        ("Sammelpunkt: Westhof; danach Bus.", "Westhof;", "Westhof", "the collection point"),
        ("Spare keys: 5.", "5.", "5", "the count"),
        # GA12: a clause inside the note's sentence
        ("Ersatzschlüssel\nDer Schlüssel liegt in der Blechdose hinter dem Blumentopf im Hof.",
         "in der Blechdose hinter dem Blumentopf im Hof.", "in der Blechdose hinter dem Blumentopf im Hof",
         "the hiding place of the spare key"),
    ]
    for text, words, want, field in cases:
        text = text if "\n" in text else "Titel\n" + text
        span, how, left = F2.extract_clause([text], words, field)
        assert span == want and left and "not part of the value" in how, (text, words, span, how)


def test_offsets_are_those_of_the_nfc_text_and_the_title_rule_is_unchanged():
    nfd = unicodedata.normalize("NFD", "Notiz\nTreffpunkt: Straße am Mühlbach.")
    span, how, _ = F2.extract_clause([nfd], "Straße am Mühlbach.", "the meeting point")
    assert span == "Straße am Mühlbach" and unicodedata.is_normalized("NFC", span)
    # the title of a text of several lines is still refused; the whole text too
    assert F2.extract_clause(["Return confirmed\nAgreed: West."], "Return confirmed", "x")[0] is None
    assert F2.extract_clause(["A.\nB."], "A.\nB.", "x")[0] is None


def test_a_value_written_as_a_line_of_its_own_takes_the_sentence_punctuation_back():
    step = S("doc_save", "TextEdit", "", "Ersatzschlüssel: {{spot}}", mode="append")
    conc = dict(step, value="Ersatzschlüssel: in der Blechdose im Hof")
    assert F2.line_value(step, conc, {"spot": "."}) == "Ersatzschlüssel: in der Blechdose im Hof."
    ins = S("doc_save", "TextEdit", "", "{{cp}}", mode="insert", find="Meeting: 09:00")
    assert F2.line_value(ins, dict(ins, value="North gate"), {"cp": "."}) == "North gate."
    # an edit writes into an existing line; a value that does not END with the variable; no punctuation left out
    ed = S("doc_save", "TextEdit", "", "{{cp}}", mode="edit", find="TBD")
    assert F2.line_value(ed, dict(ed, value="West courtyard"), {"cp": "."}) is None
    mid = S("doc_save", "TextEdit", "", "Treffpunkt {{cp}} um 9", mode="append")
    assert F2.line_value(mid, dict(mid, value="Treffpunkt Westhof um 9"), {"cp": "."}) is None
    assert F2.line_value(ins, dict(ins, value="North gate"), {"cp": ""}) is None
    assert F2.line_value(S("scoped_find", "Notes", "", "{{cp}}"), {"value": "x"}, {"cp": "."}) is None


def test_gg09s_note_appended_as_a_new_line_keeps_its_period():
    g = _gg09_guest()
    plan2 = plan4(S("extract", "none", "the agreed collection point", "collection_point", find="West courtyard."),
                  S("launch_app", "TextEdit"),
                  S("doc_save", "TextEdit", "", "{{collection_point}}", mode="append"))
    rec = X.Rec("GG09", "arch12", 1)
    goal = "Append the agreed collection point for the return trip from the trip notes to the open checklist and save."
    _arch12(g, FakePlanner4(GG09_PLAN1, plan2)).run(X.Task("GG09", goal, "cross-app", {}, [], {}), rec)
    assert rec.status == "completed", (rec.stopped, rec.steps[-1])
    assert g.fs[f"{ROOT}/{GG09_DOC}"].endswith("\nWest courtyard.")
    assert any(s.get("line_value") for s in rec.steps)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
