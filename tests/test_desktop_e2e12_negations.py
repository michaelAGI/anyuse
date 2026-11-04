"""Arch12, offline with fakes: a negated phrase is read by its SLOT.

Arch11's constraint filter dropped the right item twice on a word overlap:
* GF06 — "Nur die endgültige Zahl, nicht die Schätzung" contradicted the right note because its TEXT says
  "Erst 14 Kisten geschätzt; endgültig 17": a word in the content is not the item's identity;
* GF13 — "Der Beginn bleibt wie er ist" (a part of the text that stays) was read as an exclusion: the right file
  contains "Beginn".

Pinned here: an exclusion contradicts a candidate by its NAME (and kind); by its content only when the phrase
itself describes the item by its content ("die Notiz über den Abfluss", "das mit dem Gehalt"); "X bleibt / leave X
as it is" contradicts only an item it names and keeps every line carrying X byte-identical on the save (checked
before anything is written). The Arch9 / Arch10 cases (the Süd flat, the provisional appointment, the drain note)
still hold. No desktop, no AX, no subprocess, no network.
"""
import datetime as dt

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e9 as N
from anyuse.execution import desktop_e2e10 as T
from anyuse.execution import desktop_e2e12 as Z

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, TE, ROOT = T4.FINDER, T4.TE, T4.ROOT
NOW = dt.datetime(2026, 10, 1, 12, 0)
ConfOracle, cfg = T11.ConfOracle, T11.cfg


def statuses(goal, step, rows, lk, where="", ui="de"):
    fs = [N.candidate_fields(r, NOW, lk) for r in rows]
    ks = Z.goal_constraints12(goal, step, lk, NOW, where, [r["label"] for r in rows], ui)
    k10 = T.goal_constraints10(goal, step, lk, NOW, where, [r["label"] for r in rows], ui)
    return Z.evaluate12(ks, fs, where), T.evaluate10(k10, fs, where)


def contradicted(st):
    return {k for k, (s, _w) in st.items() if s == "contradicted"}


GF06 = ("Kannst du die Kistenzahl aus der Abholnotiz für den Hinterhof in den offenen Packzettel übernehmen? Nur die "
        "endgültige Zahl, nicht die Schätzung. Sonst nix ändern, bitte sichern.")
ROWS_GF06 = [{"label": "Hinterhof bestätigt",
              "text": "Hinterhof bestätigt | Erst 14 Kisten geschätzt; endgültig 17 Kisten und 4 Säcke."},
             {"label": "Vorderhaus bestätigt", "text": "Vorderhaus bestätigt | Endgültig 19 Kisten und 2 Säcke."},
             {"label": "Hinterhof Material", "text": "Hinterhof Material | 20 leere Kisten bestellt."}]
GF13 = ("Nur im Ordner für den kleinen Saal: Öffne den Ablauf mit „Einlass: 18:30“ und schieb den Einlass auf 18:45. "
        "Der Beginn bleibt wie er ist. Speichern, bitte.")
ABLAUF = "Kleiner Saal\nEinlass: 18:30\nBeginn: 19:00\nEnde: 20:15"
AUFBAU = "Kleiner Saal\nAufbau: 17:30\nTechniktest: 18:30"
SCOPE13 = "Ordner für den kleinen Saal"                 # Arch11's plan carried the restriction this way


def test_gf06_a_word_in_the_content_never_excludes_the_item_by_a_plain_negation():
    st, st10 = statuses(GF06, {"target": "Abholnotiz für den Hinterhof"}, ROWS_GF06, "notes_notes", "Umzug Abholung")
    assert "exclude" in contradicted(st10[0])                     # the Arch10 filter: the right note dropped
    assert not any(contradicted(s) for s in st)
    neg = Z.neg_phrases12(GF06)
    assert neg == [{"phrase": "die Schätzung", "words": ["schatzung"], "slot": "plain", "content": False,
                    "all": ["schatzung"]}]
    # a NAME that carries the excluded word is still excluded
    rows = ROWS_GF06 + [{"label": "Schätzung Kisten", "text": "Schätzung Kisten | etwa 15"}]
    st2, _ = statuses(GF06, {"target": "Abholnotiz für den Hinterhof"}, rows, "notes_notes", "Umzug Abholung")
    assert [("exclude" in contradicted(s)) for s in st2] == [False, False, False, True]


def test_gf13_a_part_that_stays_is_no_exclusion_of_the_file_that_holds_it():
    rows = [{"label": "Ablauf.txt", "text": "Ablauf.txt", "content": ABLAUF, "content_known": "full"},
            {"label": "Aufbau.txt", "text": "Aufbau.txt", "content": AUFBAU, "content_known": "full"}]
    st, st10 = statuses(GF13, {"target": "Ablauf mit „Einlass: 18:30“"}, rows, "finder_files", "Kleiner Saal")
    assert "exclude" in contradicted(st10[0])                     # the Arch10 filter: the right file dropped
    assert not contradicted(st[0]) and not contradicted(st[1])
    assert [k["phrase"] for k in Z.keep_parts12(GF13)] == ["Der Beginn"]


def test_a_phrase_that_describes_the_item_by_its_content_still_uses_the_content():
    goal = "Kannst du den Fachbegriff aus meiner Notiz zum undichten Wasserhahn nachschlagen? Nicht die Notiz über " \
           "den Abfluss."
    rows = [{"label": "Hahn Küche", "text": "Hahn Küche | Der Hahn tropft am Griff."},
            {"label": "Spüle", "text": "Spüle | Der Abfluss läuft langsam ab."}]
    st, _ = statuses(goal, {"target": "Notiz zum undichten Wasserhahn"}, rows, "notes_notes")
    assert not contradicted(st[0]) and "exclude" in contradicted(st[1]) and "content" in st[1]["exclude"][1]
    st2, _ = statuses("Schlag das Wort aus meiner Notiz zur Tür nach, nicht das mit dem Gehalt.",
                      {"target": "Notiz zur Tür"}, [{"label": "Tür", "text": "Tür | Wort: Zarge"},
                                                     {"label": "Büro", "text": "Büro | Zusätzlich zum Gehalt"}],
                      "notes_notes")
    assert [("exclude" in contradicted(s)) for s in st2] == [False, True]
    assert Z.neg_slot12("die von den Kindern", "not") == ("object", True)
    assert Z.neg_slot12("die Schätzung", "not") == ("plain", False)
    assert Z.neg_slot12("Der Beginn", "keep") == ("keep", False)
    assert Z.neg_slot12("die alte Liste", "keep") == ("object", False)


def test_a_phrase_naming_the_lists_own_kind_is_read_in_the_content_gd13_gb03():
    gd13 = ("Die Notiz mit der abgesagten Führung vom August brauch ich nicht mehr. Zeig mir genau die und frag erst "
            "nach, bevor du sie löschst. Die verschobene Führung bleibt.")
    rows = [{"label": "Führung August – Gruppe A", "text": "Führung August – Gruppe A | Die Führung am 14. August wurde "
                                                           "abgesagt; keine Ersatzzeit."},
            {"label": "Führung August – Gruppe B", "text": "Führung August – Gruppe B | Die Führung am 21. August wurde "
                                                           "auf 16 Uhr verschoben."}]
    st, _ = statuses(gd13, {"target": "Führung August – Gruppe B"}, rows, "notes_notes")
    assert [("exclude" in contradicted(s)) for s in st] == [False, True]      # the postponed tour stays excluded
    gb03 = "Kannst du den August-Beleg mit den Stühlen aufmachen und den Betrag von 84 auf 48 Euro korrigieren? Nur " \
           "den, nicht den Tisch-Beleg."
    rows3 = [{"label": "Beleg 17.txt", "text": "Beleg 17.txt", "content": "Artikel: vier Stühle", "content_known": "full"},
             {"label": "Beleg 18.txt", "text": "Beleg 18.txt", "content": "Artikel: ein Tisch", "content_known": "full"}]
    st3, _ = statuses(gb03, {"target": "August-Beleg mit den Stühlen"}, rows3, "finder_files")
    assert [("exclude" in contradicted(s)) for s in st3] == [False, True]


def test_prior_exclusions_still_hold_by_the_name():
    goal = ("In der offenen Lieferliste fehlt noch die Rückgabe. Pack den Rückgabetermin aus der bestätigten Notiz "
            "dahinter. Nicht den vorläufigen Termin nehmen.")
    rows = [{"label": "Termin vorläufig", "text": "Termin vorläufig | Rückgabe: 06.10.2026, 09:00"},
            {"label": "Termin bestätigt", "text": "Termin bestätigt | Rückgabe: 08.10.2026, 09:30"},
            {"label": "Lieferung bestätigt", "text": "Lieferung bestätigt | Lieferung: 08.10.2026"}]
    st, _ = statuses(goal, {"target": "die bestätigte Notiz"}, rows, "notes_notes")
    assert [s["exclude"][0] for s in st] == ["contradicted", "satisfied", "satisfied"]
    se08 = ("In the Nord flat's conditions file, change the deposit to 1200 euros. The final one, not the draft, and "
            "leave the Süd flat alone.")
    rows8 = [{"label": "Wohnung_Nord", "text": "Wohnung_Nord | folder"}, {"label": "Wohnung_Sued", "text": "Wohnung_Sued | folder"}]
    st8, _ = statuses(se08, {"target": "final Nord flat conditions file"}, rows8, "finder_files", ui="de")
    assert not contradicted(st8[0]) and "exclude" in contradicted(st8[1])


def test_the_keep_check_names_a_kept_line_the_edit_would_change():
    keeps = Z.keep_parts12(GF13)
    assert Z.keep_problems12(keeps, ABLAUF, ABLAUF.replace("Einlass: 18:30", "Einlass: 18:45")) == []
    probs = Z.keep_problems12(keeps, ABLAUF, ABLAUF.replace("Beginn: 19:00", "Beginn: 19:15"))
    assert probs and "Beginn: 19:00" in probs[0]
    assert Z.keep_parts12("Leave the date as it is and change the room to B12.")[0]["words"] == ["date"]


# ── the arm end to end on fakes ───────────────────────────────────────────────

def _gf13_guest():
    return T9.FakeGuest9({"L/Kleiner Saal/Ablauf.txt": ABLAUF, "L/Kleiner Saal/Aufbau.txt": AUFBAU},
                         shown="L/Kleiner Saal")


def arch12(guest, planner, js, t2=0.5, **kw):
    return Z.Arch12(guest, EMAP, planner=planner, config11=cfg(t2=t2), jchooser=js, spend=None, inbox=None,
                    sleep=lambda s: None, **kw)


def test_gf13_like_the_arm_edits_the_right_file_and_keeps_the_beginning():
    g = _gf13_guest()
    p = FakePlanner4(plan4(S("launch_app", "Finder"), S("open_item", "Finder", "Ablauf mit „Einlass: 18:30“", sc=SCOPE13),
                           S("doc_save", "TextEdit", "", "Einlass: 18:45", mode="edit", find="Einlass: 18:30")))
    rec = X.Rec("GF13", "arch", 1)
    arch12(g, p, ConfOracle("Ablauf.txt", 0.99)).run(X.Task("GF13", GF13, "cross-app", {"app": FINDER}, [], {}), rec)
    path = f"{ROOT}/L/Kleiner Saal/Ablauf.txt"
    assert g.fs[path] == "Kleiner Saal\nEinlass: 18:45\nBeginn: 19:00\nEnde: 20:15", (rec.stopped, rec.steps)
    pick = next(s["pick"] for s in rec.steps if s.get("pick"))
    assert pick["dropped"] == [] and pick["alive"] == ["C1", "C2"]
    assert next(s for s in rec.steps if s["step"]["effect"] == "doc_save")["keep12"]["problems"] == []


def test_an_edit_that_would_change_a_kept_line_writes_nothing():
    g = _gf13_guest()
    p = FakePlanner4(plan4(S("launch_app", "Finder"), S("open_item", "Finder", "Ablauf mit „Einlass: 18:30“", sc=SCOPE13),
                           S("doc_save", "TextEdit", "", "Beginn: 19:15", mode="edit", find="Beginn: 19:00")))
    rec = X.Rec("GF13", "arch", 1)
    arch12(g, p, ConfOracle("Ablauf.txt", 0.99)).run(X.Task("GF13", GF13, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[f"{ROOT}/L/Kleiner Saal/Ablauf.txt"] == ABLAUF and rec.stopped.startswith("hand-off")
    assert T9.ops(g, "enter") == [] and rec.fixes12[0]["fix"] == "keep" and rec.errors9 == []


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
