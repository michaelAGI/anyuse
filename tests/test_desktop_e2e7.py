"""Arch7, offline, with a FAKE
guest and fake models (the Arch4-6 fakes, extended).

What is pinned here, one block per fix:
1. routing by object kind: file-like nouns go to Finder/TextEdit, note-like
   nouns to Notes, also after a Notes read (a planner rule and a post-plan check);
2. partial edits keep the rest: the new value is computed from the old value
   and the user's words; a diff check allows only the named part to change,
   else the old text is restored and the step refused;
3. names vs containers: a note's or file's name is never a navigation target;
4. "none of these" for the picker: acting needs both orders to agree and
   neither to choose none; t2 recalibrated with it (frozen before measuring);
5. capability statements for the GC06 refusal;
plus the frontier-agent comparison arm (sonnet_agent) and the Arch7 host.

No desktop, no AX, no subprocess, no network.
"""
import hashlib
import json
import os

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e2 as Y
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e5 as V
from anyuse.execution import desktop_e2e6 as U
from anyuse.execution import desktop_e2e7 as R

import test_desktop_e2e as T3
import test_desktop_e2e4 as T4
import test_desktop_e2e6 as T6

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, NOTES, TE, DICT = T4.FINDER, T4.NOTES, T4.TE, T4.DICT
ROOT = T4.ROOT


def arch7(guest, planner, chooser=None, spend=None, threshold=0.5, **kw):
    return R.Arch7(guest, EMAP, planner=planner, chooser=chooser, spend=spend, inbox=None, sleep=lambda s: None,
                   threshold=threshold, **kw)


def effects(steps):
    return [(s["effect"], s["app"]) for s in steps]


# ── 1. routing by object kind ────────────────────────────────────────────────

def test_object_kind_reads_the_head_noun_and_extensions():
    for t in ("neueren Zettel", "der neuere Zettel", "die Einkaufsliste", "Preisliste", "the price list", "Datei",
              "the newer document", "Notes_Meeting.txt", "Bericht.txt", "Textdatei Vertrag"):
        assert R.object_kind(t) == "file", t
    for t in ("Notiz über den Abholort", "die Wortschatz-Notiz", "the note about the projector", "Notizen"):
        assert R.object_kind(t) == "note", t
    for t in ("Kastanienstraße", "Reden", "the newest draft", "Balkon", "", None):
        assert R.object_kind(t) is None, t


GC07 = "Nimm aus der Notiz über den Abholort die richtige Straße und korrigier sie im neueren Zettel. Nur die Straße, bitte."


def test_a_file_planned_in_notes_after_a_read_goes_to_finder_and_is_read_first_gc07_like():
    # a recorded GC07 continuation (Arch6): the Zettel looked for in Notes, the street written into the note text
    cont = [S("extract", "none", "die richtige Straße", "street", find="Kastanienstraße"),
            dict(S("open_item", "Notes", "neueren Zettel"), target_ui="der neuere Zettel"),
            S("field_rw", "Notes", "note text", "{{street}}", mode="replace")]
    ref = R.refine_plan7(GC07, cont, EMAP)
    core = [s for s in ref.steps if s["effect"] != "extract"]
    assert effects(core) == [("open_item", "Finder"), ("read_content", "TextEdit")]
    assert core[1]["target"] == "document" and ref.pending and ref.pending[0]["effect"] == "field_rw"
    assert any("names a FILE" in c for c in ref.changes)
    # another recorded GC07 continuation (Arch6): the Zettel opened and read in Notes
    cont = [S("extract", "none", "Straße", "correct_street", find="Kastanienstraße"),
            S("open_item", "Notes", "neueren Zettel"), S("read_content", "Notes", "note", "newer_note")]
    ref = R.refine_plan7(GC07, cont, EMAP)
    core = [s for s in ref.steps if s["effect"] != "extract"]
    assert effects(core) == [("open_item", "Finder"), ("read_content", "TextEdit")]
    assert core[1]["target"] == "document" and core[1]["value"] == "newer_note"


def test_a_write_after_the_document_was_read_becomes_doc_save():
    st = [S("open_item", "Notes", "die Preisliste"), S("read_content", "Notes", "note", "t"),
          S("field_rw", "Notes", "note text", "2,40 €", mode="edit", find="2,20 €")]
    ref = Y.Refined([dict(s) for s in st])
    R.route_by_kind(ref)
    assert effects(ref.steps) == [("open_item", "Finder"), ("read_content", "TextEdit"), ("doc_save", "TextEdit")]
    assert ref.steps[2]["mode"] == "edit" and ref.steps[2]["find"] == "2,20 €"


def test_notes_stay_in_notes_and_a_note_planned_in_finder_goes_to_notes():
    st = [S("launch_app", "Notes"), S("open_item", "Notes", "Notiz über den Abholort"), S("read_content", "Notes", "note", "t")]
    ref = R.refine_plan7(GC07, st, EMAP)
    assert effects(ref.steps) == effects(st) and not any("FILE" in c for c in ref.changes)
    ref = Y.Refined([S("open_item", "Finder", "the note about the projector"), S("read_content", "TextEdit", "document", "t")])
    R.route_by_kind(ref)
    assert effects(ref.steps) == [("open_item", "Notes"), ("read_content", "Notes")]
    # a file planned in Finder, and an item of no kind, are left as planned
    for st in ([S("open_item", "Finder", "Zettel 2.txt")], [S("open_item", "Notes", "Abholort")]):
        ref = Y.Refined([dict(s) for s in st])
        R.route_by_kind(ref)
        assert effects(ref.steps) == effects(st) and not ref.changes


def test_lead_ups_move_with_the_rerouted_item_and_the_screen_must_show_files():
    # offline, no screen: the navigate that leads to the file moves with it
    st = [S("launch_app", "Notes"), S("navigate", "Notes", "Privat"), S("open_item", "Notes", "Einkaufsliste")]
    ref = Y.Refined([dict(s) for s in st])
    R.route_by_kind(ref)
    assert effects(ref.steps) == [("launch_app", "Finder"), ("navigate", "Finder"), ("open_item", "Finder")]
    # a Notes read before the lead-ups stays in Notes (GC07-like: the note, then the file)
    st = [S("launch_app", "Notes"), S("open_item", "Notes", "Notiz über den Abholort"), S("read_content", "Notes", "note", "t"),
          S("open_item", "Notes", "den neueren Zettel")]
    ref = Y.Refined([dict(s) for s in st])
    R.route_by_kind(ref, {"finder_window": "Flohmarkt Abholung", "notes_folder": "Flohmarkt Absprachen"})
    assert effects(ref.steps) == [("launch_app", "Notes"), ("open_item", "Notes"), ("read_content", "Notes"),
                                  ("open_item", "Finder")]
    # with the screen known and no file on it (no Finder window, no TextEdit document), the plan stays
    ref = Y.Refined([S("open_item", "Notes", "die Einkaufsliste")])
    R.route_by_kind(ref, {"front": "Notizen (Notes)", "notes_folder": "Privat"})
    assert effects(ref.steps) == [("open_item", "Notes")] and not ref.changes


def _gc07_guest():
    g = T6.FakeGuest6(tree={"Flohmarkt Abholung/Zettel 1.txt": "Abholung: Lindenstraße 8\nUhrzeit: 10:00",
                            "Flohmarkt Abholung/Zettel 2.txt": "Abholung: Lindenstraße 8\nUhrzeit: 11:30"},
                      shown="Flohmarkt Abholung",
                      notes={"Flohmarkt Absprachen": [("Aufbauort", "Aufbauort\nTreffpunkt zum Aufbau ist Lindenstraße 12."),
                                                      ("Abholort", "Abholort\nDer richtige Ort ist jetzt Kastanienstraße 8.")]})
    g.front, g.notes_folder, g.shown_note = NOTES, "Flohmarkt Absprachen", "Aufbauort"
    return g


def test_gc07_like_run_opens_the_file_from_finder_and_edits_it():
    g = _gc07_guest()
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("open_item", "Notes", "Notiz über den Abholort"),
                           S("read_content", "Notes", "note", "text")),
                     plan4(S("extract", "none", "die richtige Straße", "street", find="Kastanienstraße"),
                           S("open_item", "Notes", "neueren Zettel"), S("field_rw", "Notes", "note text", "{{street}}",
                                                                          mode="replace")),
                     plan4(S("doc_save", "TextEdit", "", "{{street}}", mode="edit", find="Lindenstraße")))
    ch = T3.FakeChooser(["Abholort |", "Zettel 2.txt"], conf=0.95)
    t = T4.task("K1", GC07, [{"kind": "folder", "path": f"{ROOT}/Flohmarkt Abholung"}],
                {"file_text": {f"{ROOT}/Flohmarkt Abholung/Zettel 2.txt": "Abholung: Kastanienstraße 8\nUhrzeit: 11:30"}})
    out = W.run_one4(t, "arch", 1, g, arch=arch7(g, p, ch), emap=EMAP)
    assert g.fs[f"{ROOT}/Flohmarkt Abholung/Zettel 2.txt"] == "Abholung: Kastanienstraße 8\nUhrzeit: 11:30", out["steps"]
    assert g.fs[f"{ROOT}/Flohmarkt Abholung/Zettel 1.txt"].startswith("Abholung: Lindenstraße")
    assert dict(g.notes["Flohmarkt Absprachen"])["Aufbauort"].endswith("Lindenstraße 12.")       # no note written
    assert [s["step"]["effect"] for s in out["steps"] if s["step"]["effect"] != "extract"] == \
        ["launch_app", "open_item", "read_content", "open_item", "read_content", "doc_save"]
    assert [s for s in out["steps"] if s["step"]["effect"] == "open_item"][1]["step"]["app"] == "Finder"
    assert out["continuations"] == 2


def test_planner_w7_states_the_object_kind_rule_and_is_derived_from_w6():
    spec = R.load_planner7_spec()
    assert "- The kind of object decides the app, also after you have read a note: a Zettel, Datei, Dokument or " \
           "Liste" in spec["w7_system"]
    assert spec["w7_sha256"] == hashlib.sha256(spec["w7_system"].encode()).hexdigest()
    assert spec["w6_sha256_base"] == hashlib.sha256(
        open(U.PLANNER6_PATH, "rb").read()).hexdigest()
    assert R.planner7_messages("g", spec)[0]["content"] == spec["w7_system"]


# ── 2. partial edits keep the rest ───────────────────────────────────────────

SC12 = "In Notes_Meeting.txt, change the date on the second line to October 2."


def test_complete_partial_keeps_the_parts_the_user_did_not_name():
    assert R.complete_partial("September 25, 2026", "October 2", SC12)[0] == "October 2, 2026"
    assert R.complete_partial("25. September 2026", "2. Oktober", "Verschieb das auf den 2. Oktober")[0] == \
        "2. Oktober 2026"
    assert R.complete_partial("25.09.2026", "02.10.", "Datum auf den 2.10.")[0] == "02.10.2026"
    assert R.complete_partial("Datum: 2026-09-25", "Datum: 2026-10-02", "x")[0] == "Datum: 2026-10-02"
    # the user names a year: nothing added
    assert R.complete_partial("September 25, 2026", "October 2", "change it to October 2 2027")[0] == "October 2"
    assert R.complete_partial("2,20 €", "2,40", "Hafermilch auf 2,40")[0] == "2,40 €"
    assert R.complete_partial("11:30 Uhr", "12:00", "auf 12 Uhr")[0] == "12:00 Uhr"
    assert R.complete_partial("2,20 €", "2,40 €", "x") == ("2,40 €", [])
    assert R.complete_partial("Anna Schmidt", "Müller", "change the surname to Müller")[0] == "Anna Müller"
    assert R.complete_partial("Anna Schmidt", "Petra", "Vorname ist jetzt Petra")[0] == "Petra Schmidt"
    assert R.complete_partial("See you Monday", "See you Tuesday", "x") == ("See you Tuesday", [])


def test_the_diff_check_allows_only_the_named_part_to_change():
    assert R.partial_edit_problems("See you Monday", "See you Tuesday", "change Monday to Tuesday") == []
    assert R.partial_edit_problems("Lindenstraße", "Kastanienstraße", "korrigier die Straße") == []
    assert R.partial_edit_problems("September 25, 2026", "October 2, 2026", SC12) == []
    assert R.partial_edit_problems("2,20 €", "2,40 €", "auf 2,40 €") == []
    p = R.partial_edit_problems("September 25, 2026", "October 2", SC12)
    assert p and any("2026" in x for x in p)
    assert R.partial_edit_problems("Hafermilch August: 2,20 €", "2,40 €", "Hafermilch auf 2,40 €")
    assert R.partial_edit_problems("Treffen am Montag um 10 Uhr", "Treffen am Dienstag", "auf Dienstag verschieben")
    # the user asks to remove, or names the old and the new words exactly: allowed
    assert R.partial_edit_problems("Treffen am Montag um 10 Uhr", "Treffen am Montag", "entferne die Uhrzeit") == []
    assert R.partial_edit_problems("See you Monday!", "Bye", "change 'See you Monday!' to 'Bye'") == []


def _te_guest(text, name="Notes_Meeting.txt"):
    g = T6.FakeGuest6(tree={f"Meetings/{name}": text}, shown="Meetings", te_front=f"Meetings/{name}")
    g.front = TE
    return g


def _te_task(tid, goal, path, want):
    return T4.task(tid, goal, [{"kind": "folder", "path": f"{ROOT}/Meetings"}], {"file_text": {f"{ROOT}/{path}": want}})


def test_sc12_like_a_date_edit_keeps_the_year_end_to_end():
    old = "Team Sync\nSeptember 25, 2026\nAttendees: Anna, Jonas, Petra."
    g = _te_guest(old)
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"), S("read_content", "TextEdit", "document", "text")),
                     plan4(S("doc_save", "TextEdit", "", "October 2", mode="edit", find="September 25, 2026")))
    want = "Team Sync\nOctober 2, 2026\nAttendees: Anna, Jonas, Petra."
    out = W.run_one4(_te_task("P1", SC12, "Meetings/Notes_Meeting.txt", want), "arch", 1, g, arch=arch7(g, p), emap=EMAP)
    assert g.fs[f"{ROOT}/Meetings/Notes_Meeting.txt"] == want, out["steps"]
    assert out["judge"]["success"] and out["steps"][-1]["partial"]["value"] == "October 2, 2026"


def test_an_edit_that_drops_an_unnamed_part_is_refused_before_writing():
    old = "Hafermilch August: 2,20 €\nKaffee August: 8,90 €"
    g = _te_guest(old, "Preisliste.txt")
    goal = "Mach mal den August-Preis für Hafermilch auf 2,40 €."
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"), S("read_content", "TextEdit", "document", "text")),
                     plan4(S("doc_save", "TextEdit", "", "2,40 €", mode="edit", find="Hafermilch August: 2,20 €")))
    out = W.run_one4(_te_task("P2", goal, "Meetings/Preisliste.txt", old), "arch", 1, g, arch=arch7(g, p), emap=EMAP)
    assert g.fs[f"{ROOT}/Meetings/Preisliste.txt"] == old and out["stopped"] == "refused (partial edit check)"
    assert not [a for op, a in g.log if op == "enter"]


class SaveDropsGuest(T6.FakeGuest6):
    """Reads the saved file with the year dropped from the third read on (a save
    that the byte check passed but the diff check does not: defence in depth)."""

    reads = 0

    def op_file(self, path):
        r = super().op_file(path)
        self.reads += 1
        if self.reads == 3 and r.get("text"):
            r = dict(r, text=r["text"].replace(", 2026", ""))
        return r


def test_a_saved_text_that_fails_the_diff_check_is_restored_and_refused():
    old = "Team Sync\nSeptember 25, 2026\nAttendees: Anna."
    g = SaveDropsGuest(tree={"Meetings/Notes_Meeting.txt": old}, shown="Meetings", te_front="Meetings/Notes_Meeting.txt")
    g.front = TE
    a = arch7(g, FakePlanner4(plan4(S("doc_save", "TextEdit", "", "October 2", mode="edit", find="September 25, 2026"))))
    rec = X.Rec("P3", "arch", 1)
    a.run(_te_task("P3", SC12, "Meetings/Notes_Meeting.txt", old), rec)
    assert rec.stopped == "refused (partial edit check: the old text restored)", (rec.stopped, rec.steps)
    assert g.fs[f"{ROOT}/Meetings/Notes_Meeting.txt"] == old and rec.restores == 1


def test_planner_w7_states_the_partial_edit_rule():
    spec = R.load_planner7_spec()
    assert "- Changing PART of a value (a date, a price, a time, a name): find = the whole old value" in spec["w7_system"]
    assert "Keep every part of a value the goal does not name" in spec["continuation_template"]


# ── 3. names vs containers ───────────────────────────────────────────────────

SC03 = "Schau mal im Duden nach, was das Wort in meiner Wortschatz-Notiz bedeutet."


def test_a_notes_name_given_as_a_scope_is_not_applied_as_a_folder_navigation_sc03_r2():
    plan = [S("launch_app", "Notes"), S("navigate", "Notes", "Deutschkurs"), S("open_item", "Notes", "Wortschatz"),
            S("read_content", "Notes", "note", "note_text", sc="Wortschatz")]
    ref6 = U.refine_plan6(SC03, [dict(s) for s in plan], EMAP)
    assert ("navigate", "Notes") in effects(ref6.steps[3:])                    # Arch6: navigate("Wortschatz")
    ref = R.refine_plan7(SC03, plan, EMAP)
    assert [s["target"] for s in ref.steps if s["effect"] == "navigate"] == ["Deutschkurs"]
    assert ref.steps[-1]["effect"] == "read_content" and ref.steps[-1]["scope"] == ""
    assert any("never a navigation target" in c for c in ref.changes)
    # the note the screen shows, and a file name, are no containers either
    ref = R.refine_plan7(SC03, [S("navigate", "Notes", "Wortschatz"), S("read_content", "Notes", "note", "t")], EMAP,
                         screen={"notes_note": "Wortschatz"})
    assert effects(ref.steps) == [("read_content", "Notes")]
    ref = Y.Refined([S("navigate", "Finder", "Bericht.txt"), S("open_item", "Finder", "Bericht.txt")])
    R.drop_item_navigation(ref)
    assert effects(ref.steps) == [("open_item", "Finder")]
    # a folder navigation before an item of another name stays
    ref = Y.Refined([S("navigate", "Notes", "Deutschkurs"), S("open_item", "Notes", "Wortschatz")])
    R.drop_item_navigation(ref)
    assert len(ref.steps) == 2 and not ref.changes


def _deutschkurs_guest():
    g = T6.FakeGuest6(notes={"Deutschkurs": [("Wortschatz", "Wortschatz\nNeues Wort: Serendipität"),
                                             ("Grammatik", "Grammatik\nKonjunktiv II")],
                             "genlychee-2": [("genlychee-2", "genlychee-2\nclutter")],
                             "Notizen": [("Einkauf", "Einkauf\nBrot")]})
    g.front, g.notes_folder, g.shown_note = NOTES, "Deutschkurs", "Wortschatz"
    return g


def test_a_navigation_to_an_items_name_is_skipped_at_run_time_no_pick():
    g = _deutschkurs_guest()
    g.shown_note = "Grammatik"                     # the plan check does not see it: the run-time check does
    ch = T3.FakeChooser("genlychee-2", conf=0.86)
    a = arch7(g, FakePlanner4(plan4(S("navigate", "Notes", "Wortschatz"), S("open_item", "Notes", "Wortschatz"))), ch)
    rec = X.Rec("N1", "arch", 1)
    a.run(T4.task("N1", SC03, [], {}), rec)
    assert rec.steps[0]["resolved"]["op"] == "navigate_skipped" and ch.calls == 0
    assert g.notes_folder == "Deutschkurs" and g.shown_note == "Wortschatz" and rec.steps[1]["outcome"] == "ok"
    assert rec.compiled[0]["resolved"]["op"] == "navigate_skipped"
    # the compiled replay skips it again with no model
    g2 = _deutschkurs_guest()
    g2.shown_note = "Grammatik"
    rec2 = X.Rec("N1", "arch", 1, mode="compiled")
    arch7(g2, None).run(T4.task("N1", SC03, [], {}), rec2, compiled=rec.compiled)
    assert rec2.steps[0]["outcome"] == "ok" and g2.notes_folder == "Deutschkurs" and rec2.calls["picker"] == 0


def test_a_real_folder_is_still_navigated_by_the_label_rule():
    g = _deutschkurs_guest()
    g.notes_folder, g.shown_note = "Notizen", None
    a = arch7(g, FakePlanner4(plan4(S("navigate", "Notes", "Deutschkurs"))))
    rec = X.Rec("N2", "arch", 1)
    a.run(T4.task("N2", "Zeig den Deutschkurs.", [], {}), rec)
    assert g.notes_folder == "Deutschkurs" and rec.steps[0]["resolved"]["op"] == "navigate"


def test_planner_w7_states_containers_only():
    assert "- scope and navigate name CONTAINERS only" in R.load_planner7_spec()["w7_system"]


# ── 4. "none of these" for the picker ────────────────────────────────────────

class NoneChooser:
    """Answers the option containing ``want``; with no such option, "none of these"."""

    def __init__(self, want, conf=0.9, none_in=None):
        self.want, self.conf, self.none_in, self.calls, self.seen = want, conf, none_in, 0, []

    def __call__(self, state, q, options):
        self.calls += 1
        self.seen.append((q, list(options)))
        hits = [i for i, o in enumerate(options) if self.want and self.want in o]
        if self.none_in == self.calls or not hits:
            return options.index(R.NONE_OPTION), self.conf, {"cost": 0.0}
        return hits[0], self.conf, {"cost": 0.0}


def test_every_question_carries_the_none_option_in_both_orders():
    ch = NoneChooser("B")
    p = R.consistent_pick7(ch, "g", "s", ["A", "B", "C"], threshold=0.5)
    assert p.index == 1 and p.calls == 2
    (q0, o0), (q1, o1) = ch.seen
    assert o0 == ["A", "B", "C", R.NONE_OPTION] and o1 == o0[::-1] and "none of these" in q0 and q0 == q1
    p = R.consistent_pick7(NoneChooser("Z"), "g", "s", ["A", "B"], threshold=0.0)
    assert p.index is None and p.reason.startswith("none of these (both orders")
    assert all(a["none"] for a in p.answers)
    p = R.consistent_pick7(NoneChooser("B", none_in=2), "g", "s", ["A", "B"], threshold=0.0)
    assert p.index is None and p.reason.startswith("none of these in one order")
    p = R.consistent_pick7(NoneChooser("B", conf=0.3), "g", "s", ["A", "B"], threshold=0.5)
    assert p.index is None and "below t2" in p.reason
    # OpenJev's 255-option limit: the none option is always sent
    ch = NoneChooser("x")
    R.consistent_pick7(ch, "g", "s", [f"o{i}" for i in range(300)], threshold=0.0)
    assert len(ch.seen[0][1]) == 255 and ch.seen[0][1][-1] == R.NONE_OPTION


def test_a_pick_among_containers_that_hold_no_right_one_is_a_hand_off_sc03_r2_like():
    g = _deutschkurs_guest()
    g.notes_folder, g.shown_note = "Notizen", None
    ch = NoneChooser("Vokabel")                      # the user's folder is not listed: none of these
    a = arch7(g, FakePlanner4(plan4(S("navigate", "Notes", "Vokabelheft"))), ch)
    rec = X.Rec("N3", "arch", 1)
    a.run(T4.task("N3", "Zeig mir mein Vokabelheft.", [], {}), rec)
    assert rec.stopped == "picker hand-off (declined)" and g.notes_folder == "Notizen"
    assert rec.steps[0]["pick"]["reason"].startswith("none of these") and rec.steps[0]["pick"]["none_option"]


def test_the_t2_with_the_none_option_is_frozen_before_the_arm_can_run():
    spec = R.load_picker7()
    assert "neither names" in spec["rule"] and spec["none_option"] == R.NONE_OPTION
    if spec["pickers"]["openjev"]["t2"] is None:
        try:
            arch7(T6.FakeGuest6(), None, picker_name="openjev")
            raise AssertionError("the arm ran without a frozen t2")
        except KeyError:
            pass
    a = arch7(T6.FakeGuest6(), None, picker_name="openjev", t2_override=0.25)
    assert a.threshold == 0.25


def test_the_settings_cross_check_asks_with_the_none_option():
    g = T6.SettingsGuest(tree={"a.txt": "x"}, shown="")
    a = arch7(g, FakePlanner4(plan4(S("launch_app", "TextEdit"))), NoneChooser("nothing"))
    cands = [{"text": "A (checkbox: on)", "option": {"key": 0, "label": "A"}},
             {"text": "B (checkbox: on)", "option": {"key": 1, "label": "B"}}]
    rec, log = X.Rec("C1", "arch", 1), {}
    out = a._crosscheck(rec, T4.task("C1", "turn it off", [], {}), log, TE, cands, {"key": 0, "label": "A"}, "Which?")
    assert out == "handoff" and log["crosscheck"]["verdict"] == "contradicts (none of these)"
    a.chooser = NoneChooser("A (")                  # an agreeing picker leaves the planner's option
    rec, log = X.Rec("C1", "arch", 1), {}
    assert a._crosscheck(rec, T4.task("C1", "turn A off", [], {}), log, TE, cands, {"key": 0, "label": "A"}, "W?") is None
    assert log["crosscheck"]["verdict"] == "agrees"


# ── 5. capability statements (GC06 under Arch6: "newest draft" refused as unsupported) ──

def test_planner_w7_states_the_capabilities_the_gc06_refusal_lacked():
    spec = R.load_planner7_spec()
    w = spec["w7_system"]
    assert "- Choose an item by its date: the newest, latest, most recent or oldest file or note" in w
    assert "another language than the screen (\"the Speeches folder\" for \"Reden\")" in w
    assert "- Change one line of a document (its first, last or closing line" in w
    assert "choose the newest or oldest item by its date" in spec["refusal_feedback_template"]
    assert spec["upto"] == [1, 2, 3, 5]
    # w7 = w6 + the anchored edits only: every line of w6 is still there, in order
    w6 = U.load_planner6_spec()["w6_system"].split("\n")
    it = iter(w.split("\n"))
    assert all(any(line == x for x in it) for line in w6)


def test_the_arm_asks_once_more_with_w7s_refusal_feedback():
    g = T6.FakeGuest6(tree={"Reden/Entwurf B.txt": "Thank you.\nSee you Monday"}, shown="")
    refuse = plan4(S("refuse", "none", "open the newest draft", "unsupported"))
    p = FakePlanner4(refuse, plan4(S("launch_app", "Finder")))
    a = arch7(g, p)
    rec = X.Rec("F5", "arch", 1)
    steps, err = a.plan(rec, "Open the newest draft and change its closing line.")
    assert p.calls == 2 and not err and steps[0]["effect"] == "launch_app"
    assert "choose the newest or oldest item by its date" in p.histories[1][-1]["content"]


# ── the frontier-agent comparison arm (sonnet_agent) ─────────────────────────

class AgentGuest(T6.FakeGuest6):
    """The guest ops the frontier agent uses: screen, ax_text, ax_step, launch,
    front, file, read_text (the checked executor's answers modelled)."""

    def __init__(self, *a, fail=(), gate=(), **kw):
        super().__init__(*a, **kw)
        self.fail, self.gate, self.ax_args = set(fail), set(gate), []

    def op_ax_text(self, value_chars=60):
        self.ax_args.append(value_chars)
        items = [{"app": self.front, "window": "w", "document": self.te_front if self.front == TE else None}]
        if self.front == NOTES:
            items += [{"role": "AXRow", "title": t, "cells": b.split("\n")[1][:120]} for t, b in
                      self.notes.get(self.notes_folder) or []]
            if self.shown_note:
                items.append({"role": "AXTextArea", "value": self.body_of(self.shown_note)[:value_chars]})
            items.append({"role": "AXMenuItem", "title": "Neue Notiz", "menu": "Ablage"})
        if self.front == DICT:
            items += [{"role": "AXSearchField", "ax_description": "Suchen", "value": self.dict_search},
                      {"role": "AXMenuItem", "title": "Duden-Wissensnetz deutsche Sprache", "menu": "Suchen"}]
        return {"status": "done", "bundle": self.front, "text": json.dumps(items, ensure_ascii=False)}

    def op_ax_step(self, step):
        c = step["command"]
        key = c.get("title") or c.get("keycode")
        if key in self.gate or X.irreversible_reason(c.get("title")):
            return {"status": "gated", "detail": "irreversible-action guard"}
        if key in self.fail:
            return {"status": "refused", "detail": f"0 candidates for {key!r}"}
        if c["action"] == "select" and self.front == NOTES:
            self.shown_note = c["title"]
        elif c["action"] == "set_value" and self.front == DICT:
            self.dict_search = c["text"]
        elif c["action"] == "set_value" and self.front == TE:
            self.te_docs[self.te_front] = c["text"]
        return {"status": "done", "detail": ""}

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if bundle == DICT and "search_field" in target:
            self.dict_search = text
            return {"status": "done", "detail": "entered"}
        return super().op_enter(bundle, target, text, commit, keys)


class ScriptModel:
    def __init__(self, *answers, cost=0.01):
        self.answers, self.calls, self.msgs, self.cost = list(answers), 0, [], cost

    def __call__(self, messages):
        self.calls += 1
        self.msgs.append(messages)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return (a if isinstance(a, str) else json.dumps(a)), {"cost": self.cost, "usage": {"prompt": 10, "completion": 5}}


def _agent_guest(**kw):
    g = AgentGuest(notes={"Flohmarkt Absprachen": [("Aufbauort", "Aufbauort\nLindenstraße 12."),
                                                   ("Abholort", "Abholort\nDer richtige Ort ist Kastanienstraße 8.")]},
                   **kw)
    g.front, g.notes_folder, g.shown_note = NOTES, "Flohmarkt Absprachen", "Aufbauort"
    return g


def test_the_agent_observes_acts_and_observes_again_through_the_same_executor():
    g = _agent_guest()
    m = ScriptModel({"actions": [{"action": "select", "role": "AXRow", "title": "Abholort"}], "status": "continue"},
                    {"actions": [{"action": "launch_app", "app": "Lexikon"}], "status": "continue"},
                    {"actions": [{"action": "set_value", "role": "AXSearchField", "title": "",
                                  "text": "Kastanienstraße"}], "status": "done"})
    t = X.Task("A1", "Schlag die Straße aus der Abholort-Notiz im Lexikon nach.", "cross-app", {}, [], {})
    rec = X.Rec("A1", "sonnet_agent", 1)
    R.FrontierAgent(g, m).run(t, rec)
    assert rec.status == "completed" and not rec.stopped and m.calls == 3 and rec.turns == 3
    assert g.shown_note == "Abholort" and g.dict_search == "Kastanienstraße" and rec.actions_executed == 3
    assert rec.calls["agent"] == 3 and abs(rec.cost - 0.03) < 1e-9
    assert g.ax_args == [R.AGENT_VALUE_CHARS] * 3
    user2 = m.msgs[1][1]["content"]
    assert "Kastanienstraße 8." in user2 and "-> done" in user2 and "Screen summary" in user2
    assert "Turn 1: you noted:" in user2              # the agent's own earlier notes carry what it read
    assert m.msgs[0][0]["content"] == R.AGENT_SYSTEM and "ask_user" in R.AGENT_SYSTEM


def test_a_search_field_after_a_launch_in_the_same_batch_belongs_to_the_launched_app():
    # sonnet_agent on GD02 and GD09: launch_app Lexikon + set_value on the search field in one
    # batch sent the word to the search field of the LAST observation's app (Notes)
    g = _agent_guest()
    m = ScriptModel({"actions": [{"action": "launch_app", "app": "Dictionary"},
                                 {"action": "set_value", "role": "AXSearchField", "title": "", "text": "Markise"}],
                     "status": "done"})
    R.FrontierAgent(g, m).run(X.Task("A9", "x", "cross-app", {}, [], {}), X.Rec("A9", "sonnet_agent", 1))
    enters = [a for op, a in g.log if op == "enter"]
    assert enters and enters[0]["bundle"] == DICT and g.dict_search == "Markise"


def test_a_turn_without_actions_is_another_look_within_the_observation_limit():
    g = _agent_guest()
    m = ScriptModel({"actions": [], "status": "continue"})
    rec = X.Rec("A8", "sonnet_agent", 1)
    R.FrontierAgent(g, m).run(X.Task("A8", "x", "single-app", {}, [], {}), rec)
    assert m.calls == R.AGENT_MAX_TURNS == 6 and rec.stopped == "turn limit (6 observations)"


def test_one_re_plan_after_a_failed_step_then_the_run_stops():
    g = _agent_guest(fail={"Nope"})
    bad = {"actions": [{"action": "select", "role": "AXRow", "title": "Nope"}], "status": "continue"}
    m = ScriptModel(bad, bad)
    rec = X.Rec("A2", "sonnet_agent", 1)
    R.FrontierAgent(g, m).run(X.Task("A2", "x", "single-app", {}, [], {}), rec)
    assert m.calls == 2 and rec.stopped.startswith("failed step") and "FAILED" in m.msgs[1][1]["content"]


def test_the_guards_and_the_agents_own_stops():
    g = _agent_guest()
    m = ScriptModel({"actions": [{"action": "press", "role": "AXMenuItem", "title": "Löschen"}], "status": "continue"})
    rec = X.Rec("A3", "sonnet_agent", 1)
    R.FrontierAgent(g, m).run(X.Task("A3", "x", "gate", {}, [], {}), rec)
    assert rec.stopped == "irreversible-action guard (declined)" and rec.gate_stops == 1
    m = ScriptModel({"actions": [{"action": "select", "role": "AXRow", "title": "Abholort"}], "status": "ask_user"})
    rec = X.Rec("A4", "sonnet_agent", 1)
    R.FrontierAgent(_agent_guest(), m).run(X.Task("A4", "delete it, ask me first", "gate", {}, [], {}), rec)
    assert rec.stopped == "human gate (declined)" and rec.gate_stops == 1 and rec.actions_executed == 1
    m = ScriptModel({"actions": [], "status": "refuse", "message": "no e-mail here"})
    rec = X.Rec("A5", "sonnet_agent", 1)
    R.FrontierAgent(_agent_guest(), m).run(X.Task("A5", "mail it", "refuse", {}, [], {}), rec)
    assert rec.stopped == "refused (agent)" and rec.actions_executed == 0
    # keys other than Return/Escape and apps outside the test never reach the guest
    for a in ({"action": "key", "key": "cmd+q"}, {"action": "launch_app", "app": "Safari"}, {"action": "click"}):
        assert R.agent_step(a)[0] is None
    assert R.agent_step({"action": "key", "key": "Return"})[1]["step"]["command"]["keycode"] == 36
    # a search field has no title: the guest's checked enter on the front window's search field
    op, args = R.agent_step({"action": "set_value", "role": "AXSearchField", "title": "", "text": "Windfang"}, DICT)
    assert op == "enter" and args == {"bundle": DICT, "target": {"search_field": 0}, "text": "Windfang"}
    assert R.agent_step({"action": "set_value", "role": "AXSearchField", "text": "x"})[0] is None


def test_a_failed_text_write_is_journalled_and_written_back():
    g = AgentGuest(tree={"d/Brief.txt": "Hallo\nSee you Monday"}, shown="d", te_front="d/Brief.txt", fail={"First Text View"})
    g.front = TE
    m = ScriptModel({"actions": [{"action": "set_value", "role": "AXTextArea", "title": "First Text View",
                                  "text": "x"}], "status": "continue"}, {"actions": [], "status": "done"})
    rec = X.Rec("A6", "sonnet_agent", 1)
    R.FrontierAgent(g, m).run(X.Task("A6", "x", "single-app", {}, [], {}), rec)
    assert rec.journal and rec.journal[0]["old_disk"] == "Hallo\nSee you Monday"
    assert rec.journal[0]["old_shown"] == "Hallo\nSee you Monday" and "restore" in rec.steps[0]


def test_the_agents_budget_is_checked_before_every_call():
    g = _agent_guest()
    m = ScriptModel({"actions": [], "status": "done"})
    try:
        R.FrontierAgent(g, m, spend=lambda k, actual=None: actual is not None).run(X.Task("A7", "x", "s", {}, [], {}),
                                                                                  X.Rec("A7", "sonnet_agent", 1))
        raise AssertionError("no stop")
    except X.RunStop:
        assert m.calls == 0


def test_compact_ax_groups_menus_and_cuts():
    items = [{"app": NOTES, "window": "Notizen", "document": None}, {"role": "AXRow", "title": "A", "cells": "a"},
             {"role": "AXMenuItem", "title": "Neue Notiz", "menu": "Ablage"},
             {"role": "AXMenuItem", "title": "Als Liste", "menu": "Darstellung", "mark": "✓"}]
    out = R.compact_ax(json.dumps(items))
    assert out.splitlines()[1] == 'AXRow "A" | a' and "menu Darstellung: Als Liste ✓" in out
    assert R.compact_ax(json.dumps(items + [{"role": "AXButton", "title": "x" * 50}] * 50), limit=300).endswith("(… cut)")


def test_the_guest_ax_text_value_length_is_a_parameter_with_the_baselines_default():
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index("def op_ax_text(a):"):src.index("# ── the independent reader")]
    assert 'vc = max(1, min(int(a.get("value_chars") or 60), 2000))' in body and '[:vc]' in body
    assert "[:60]" not in body


# ── the Arch7 host driver ────────────────────────────────────────────────────

def _host7():
    import importlib
    import sys
    here = os.path.join(os.path.dirname(os.path.abspath(X.__file__)), "..", "execution", "guest")
    sys.path.insert(0, here)
    return importlib.import_module("desktop_e2e7_host")


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if any(x.default is inspect.Parameter.empty for x in inspect.signature(fn).parameters.values()):
            continue
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            import traceback
            f += 1; print("FAIL", n, "::", repr(e)[:400]); traceback.print_exc(limit=4)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
