"""Arch5, offline, with a FAKE
guest and fake models (Arch4's fakes, extended).

What is pinned here, one block per fix:
1. picker candidates carry ABSOLUTE dates (the file system's, read in the
   guest; relative labels turned into dates with the guest's clock);
2. the picker acts only on an accepted pick (both orders agree, minimum
   confidence >= t2), t2 calibrated per picker on existing data and frozen;
3. ``extract`` takes a detail from the text read (verified to occur in it,
   never the item's title) and later steps use the detail; the item the user
   DESCRIBES is chosen by content, not the one the screen shows;
4. a write to existing content is journalled and restored when a check fails;
   replacing existing content without an explicit instruction is gated;
5. Arch4's remaining wrong-action causes.

No desktop, no AX, no subprocess, no network.
"""
import datetime as dt
import hashlib
import json
import os

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e2 as Y
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e5 as V

import test_desktop_e2e as T3
import test_desktop_e2e4 as T4

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, NOTES, TE, DICT = T4.FINDER, T4.NOTES, T4.TE, T4.DICT
ROOT = T4.ROOT
NOW = "2026-09-27 11:00"                       # the guest's clock (a Sunday)


class FakeGuest5(T4.FakeGuest4):
    """``FakeGuest4`` plus the read-only ``item_dates`` op (file dates
    from a dict path -> (modified, created))."""

    def __init__(self, *a, dates=None, now=NOW, rel=None, **kw):
        super().__init__(*a, **kw)
        self.dates = dates or {}
        self.now = now
        self.rel = rel or {}                          # label -> the relative date Finder shows

    def op_rows(self, bundle, kind, back=None):
        r = super().op_rows(bundle, kind, back)
        if kind == "finder_files":
            for row in r.get("rows") or []:
                if row["label"] in self.rel:
                    row["text"] = row["text"].replace("01.09.26", self.rel[row["label"]])
        return r

    def op_item_dates(self, labels=(), path=None):
        base = path or self.shown
        items = {}
        for lab in labels:
            p = f"{base}/{lab}"
            items[lab] = ({"modified": self.dates[p][0], "created": self.dates[p][1], "folder": self.isdir(p)}
                          if p in self.dates else None)
        return {"status": "done", "now": self.now, "weekday": "Sunday", "path": base, "items": items}


def arch5(guest, planner, chooser=None, spend=None, threshold=0.5):
    return V.Arch5(guest, EMAP, planner=planner, chooser=chooser, spend=spend, inbox=None, sleep=lambda s: None,
                   threshold=threshold)


def ops(g, name):
    return [a for op, a in g.log if op == name]


# ── 1. absolute dates ────────────────────────────────────────────────────────

def test_relative_labels_become_absolute_dates_with_the_guests_clock():
    now = V.parse_now(NOW)
    assert V.absolute_label("Vorgestern, 08:30", now) == "2026-09-25 08:30"
    assert V.absolute_label("Gestern, 20:00", now) == "2026-09-26 20:00"
    assert V.absolute_label("Heute, 10:15", now) == "2026-09-27 10:15"
    assert V.absolute_label("17:42", now) == "2026-09-27 17:42"
    assert V.absolute_label("Freitag", now) == "2026-09-25"             # the last Friday before today
    assert V.absolute_label("Sonntag", now) == "2026-09-20"             # a weekday name is never today
    assert V.absolute_label("20.09.26, 10:00", now) == "2026-09-20 10:00"
    assert V.absolute_label("05.11.2025, 14:30", None) == "2025-11-05 14:30"
    assert V.absolute_label("Reiner Text", now) is None and V.absolute_label("38 Byte", now) is None
    assert V.absolute_label("Gestern", None) is None                    # no clock: not guessed


def test_a_finder_row_carries_the_file_systems_dates_else_its_own_label_as_a_date():
    now = V.parse_now(NOW)
    row = "Rechnung_Internet.txt | Vorgestern, 08:30 | 38 Byte | Reiner Text | content: Internet: 45 Euro"
    text, how = V.dated_row_text(row, {"modified": "2026-09-25 08:30", "created": "2026-09-25 08:30"}, now)
    assert how == "file system" and "Vorgestern" not in text
    assert text == ("Rechnung_Internet.txt | modified 2026-09-25 08:30, created 2026-09-25 08:30 | 38 Byte | "
                    "Reiner Text | content: Internet: 45 Euro")
    text, how = V.dated_row_text(row, None, now)
    assert how == "label" and text.startswith("Rechnung_Internet.txt | modified 2026-09-25 08:30 | 38 Byte")
    note = "Kürbissuppe | Zum Schluss Croûtons darüberstreuen. | Gestern"
    assert V.dated_row_text(note, None, now)[0] == "Kürbissuppe | Zum Schluss Croûtons darüberstreuen. | modified 2026-09-26"
    assert V.dated_row_text("Alpha.txt", None, now) == ("Alpha.txt", "none")


def test_the_picker_sees_absolute_dates_for_finder_files_sa01():
    # SA01: "die neueste Rechnung"; Finder showed the newest file's date as "Vorgestern"
    tree = {"R/Rechnung_Strom.txt": "Strom: 120 Euro.", "R/Rechnung_Internet.txt": "Internet & Telefon: 45 Euro monatlich."}
    dates = {f"{ROOT}/R/Rechnung_Strom.txt": ("2026-09-20 10:00", "2026-09-20 10:00"),
             f"{ROOT}/R/Rechnung_Internet.txt": ("2026-09-25 08:30", "2026-09-25 08:30")}
    g = FakeGuest5(tree, shown="R", dates=dates, rel={"Rechnung_Internet.txt": "Vorgestern, 08:30"})
    seen = []

    def chooser(state, q, options):
        seen.append((state, list(options)))
        i = next(k for k, o in enumerate(options) if "2026-09-25" in o)
        return i, 0.95, {"cost": 0.0}
    t = T4.task("D1", "Mach mal die neueste Rechnung auf.", [{"kind": "folder", "path": ROOT + "/R"}, {"kind": "front"}],
                {"opened": {"bundle": TE, "path": ROOT + "/R/Rechnung_Internet.txt"}}, kind="cross-app")
    p = FakePlanner4(plan4(S("launch_app", "Finder"), S("open_item", "Finder", "neueste Rechnung")))
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p, chooser), emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out["steps"])
    state, options = seen[0]
    assert all("Vorgestern" not in o for o in options)
    assert any("modified 2026-09-25 08:30, created 2026-09-25 08:30" in o for o in options)
    assert "today is 2026-09-27" in state and "absolute dates" in state
    pick_step = next(s for s in out["steps"] if s.get("pick"))
    assert pick_step["dates"]["how"] == {"file system": 2} and pick_step["dates"]["now"] == NOW
    assert ops(g, "item_dates")[0]["labels"] == ["Rechnung_Internet.txt", "Rechnung_Strom.txt"]


def test_notes_rows_get_absolute_dates_from_their_labels():
    g = FakeGuest5(notes={"Suppen": [("Kürbissuppe", "Kürbissuppe\nZum Schluss Croûtons."),
                                     ("Tomatensuppe", "Tomatensuppe\nBasilikum.")]})
    g.front = NOTES
    orig = g.op_rows

    def rows(bundle, kind, back=None):
        r = orig(bundle, kind, back)
        for row in r.get("rows") or []:
            row["text"] += " | Gestern"
        return r
    g.op_rows = rows
    seen = []

    def chooser(state, q, options):
        seen.append(list(options))
        return 0, 0.9, {"cost": 0.0}
    t = T4.task("D2", "Open the note with the croutons.", [{"kind": "notes", "folders": ["Suppen"]}],
                {"notes_shown": {"folder": "Suppen", "note": "Kürbissuppe"}})
    p = FakePlanner4(plan4(S("open_item", "Notes", "note with the croutons")))
    W.run_one4(t, "arch", 1, g, arch=arch5(g, p, chooser), emap=EMAP)
    assert seen and all(o.endswith("modified 2026-09-26") for o in seen[0])
    assert ops(g, "item_dates")[0]["labels"] == []                   # Notes: no file-system read


def test_guest_item_dates_is_read_only_and_limited_to_this_tests_data():
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index("def op_item_dates("):src.index("OPS = {")]
    assert "ours(base)" in body and "os.stat(" in body and "st_birthtime" in body
    for bad in ("open(", "utime", "remove", "rename", "subprocess", "write", "AXUIElementSetAttributeValue"):
        assert bad not in body, bad
    assert '"item_dates": op_item_dates' in src


# ── 2. picker acceptance by consistency, calibrated t2 ──────────────────────

def test_the_calibration_is_frozen_per_picker_on_existing_data_only():
    spec = V.load_picker5()
    assert spec["cw_max"] == 0.03 and "both option orders" in spec["rule"]
    for name in ("jev", "openjev"):
        p = spec["pickers"][name]
        assert p["t2"] in spec["grid"] and p["at_t2"]["cw"] <= 0.03
        assert p["t2"] == min(r for r in spec["grid"] if r >= p["t2"])          # the smallest value meeting it
    assert spec["pickers"]["jev"]["n_by_source"]["step2"] == 90 and spec["pickers"]["jev"]["n_by_source"]["dev"] > 0
    assert all("e5" not in f and "e2e5" not in f for f in spec["data"]["dev_logs"])   # never Arch5's own pool
    assert V.t2_of("jev", spec) == spec["pickers"]["jev"]["t2"] and V.t2_of("openjev", spec) == spec["pickers"]["openjev"]["t2"]


def test_accept_only_when_both_orders_agree_and_the_minimum_confidence_reaches_t2():
    opts = ["A.txt | x", "B.txt | y", "C.txt | z"]
    ok = V.consistent_pick5(T3.FakeChooser("B.txt", conf=0.8), "g", "s", opts, threshold=0.75)
    assert ok.index == 1 and ok.reason.startswith("accepted")
    low = V.consistent_pick5(T3.FakeChooser("B.txt", conf=0.7), "g", "s", opts, threshold=0.75)
    assert low.index is None and "below t2" in low.reason
    flip = V.consistent_pick5(T3.FakeChooser("B.txt", conf=0.99, flip=True), "g", "s", opts, threshold=0.75)
    assert flip.index is None and "disagree" in flip.reason

    def mixed():                                    # 0.95 in one order, 0.6 in the other: the minimum decides
        c = iter([0.95, 0.6])
        return lambda st, q, o: (next(i for i, x in enumerate(o) if "B.txt" in x), next(c), {"cost": 0})
    assert V.consistent_pick5(mixed(), "g", "s", opts, threshold=0.75).index is None
    assert V.consistent_pick5(mixed(), "g", "s", opts, threshold=0.6).index == 1


def test_the_arm_takes_its_pickers_calibrated_t2():
    g = FakeGuest5()
    assert V.Arch5(g, EMAP, picker_name="jev").threshold == V.t2_of("jev")
    assert V.Arch5(g, EMAP, picker_name="openjev").threshold == V.t2_of("openjev")


# ── 3. extract after a read; the item the user describes ─────────────────────

HINGE = "Quietschendes Scharnier\nDas Scharnier an der Küchentür quietscht; erst ölen."
SOUP = {"Suppenrezepte": [("Kürbissuppe", "Kürbissuppe\nZum Schluss Croûtons aus altem Brot darüberstreuen."),
                          ("Tomatensuppe", "Tomatensuppe\nZum Schluss frisches Basilikum darüberstreuen.")]}


def test_extract_takes_a_detail_in_the_texts_own_spelling_never_the_title_or_the_whole_text():
    assert V.extract_detail([HINGE], "Scharnier", "the German word for the hinge") == ("Scharnier", "exact")
    assert V.extract_detail([SOUP["Suppenrezepte"][0][1]], "Croutons", "the word for the bread cubes") == \
        ("Croûtons", "ignoring case and accents")
    assert V.extract_detail(["Miete: 850 Euro"], "miete", "x") == ("Miete", "ignoring case")
    span, why = V.extract_detail([HINGE], "Quietschendes Scharnier", "the German word for the hinge")
    assert span is None and "title" in why                                     # GA02
    assert V.extract_detail([HINGE], "Quietschendes Scharnier", "the note's title")[0] == "Quietschendes Scharnier"
    span, why = V.extract_detail(["A\nB"], "A\nB", "x")
    assert span is None and "whole text" in why
    assert V.extract_detail([HINGE], "Türgriff", "x")[0] is None
    assert V.extract_detail(["alt", HINGE], "Scharnier", "x")[0] == "Scharnier"      # the latest read first


def test_refine5_ends_the_plan_at_a_read_unless_the_variable_is_a_whole_copy():
    base = [S("launch_app", "Notes"), S("open_item", "Notes", "spare key note"), S("read_content", "Notes", "note", "place"),
            S("launch_app", "TextEdit")]
    # GA12: the whole note inside a larger line
    r = V.refine_plan5("x", base + [S("doc_save", "TextEdit", "", "Ersatzschlüssel: {{place}}", mode="append")], EMAP)
    assert [s["effect"] for s in r.steps] == ["launch_app", "open_item", "read_content"]
    assert any("inside a larger text" in c for c in r.changes) and r.pending
    # a whole copy keeps the plan whole (Arch4's pure data flow)
    r = V.refine_plan5("x", base + [S("doc_save", "TextEdit", "", "{{place}}", mode="append")], EMAP)
    assert len(r.steps) == 5
    # an extract planned before the text was seen ends the plan at the read
    r = V.refine_plan5("x", base + [S("extract", "none", "the place", "p", find="Hof"),
                                    S("doc_save", "TextEdit", "", "Ort: {{p}}", mode="append")], EMAP)
    assert r.steps[-1]["effect"] == "read_content" and "an extract needs the text first" in r.changes[0]
    # in a continuation (the text seen) the extract stays before the step that uses it
    r = V.refine_plan5("x", [S("extract", "none", "the place", "p", find="Hof"),
                             S("doc_save", "TextEdit", "", "Ort: {{p}}", mode="append")], EMAP)
    assert [s["effect"] for s in r.steps] == ["extract", "doc_save"]


def test_check5_after_a_read_names_title_searches_whole_texts_copied_words_and_the_shown_note():
    reads = {"note": HINGE}
    goal = "Find the note about the squeaky hinge, then look up the German word I wrote for it."
    p = V.check_plan5([S("scoped_find", "Dictionary", "", "Quietschendes Scharnier")], goal, reads, [])
    assert p and "extract" in p[0]
    assert V.check_plan5([S("extract", "none", "the German word", "word", find="Scharnier"),
                          S("scoped_find", "Dictionary", "", "{{word}}")], goal, reads, []) == []
    p = V.check_plan5([S("extract", "none", "the German word", "word", find="Quietschendes Scharnier")], goal, reads, [])
    assert p and "title" in p[0]
    p = V.check_plan5([S("scoped_find", "Dictionary", "", "{{note}}")], goal, reads, [])
    assert p                                                                   # the whole text as a search word
    g12 = "Add its hiding place to the end of the open handover file as „Ersatzschlüssel: …“."
    rd = {"place": "Ersatzschlüssel Hof\nErsatzschlüssel: in der Blechdose hinter dem Blumentopf im Hof."}
    p = V.check_plan5([S("doc_save", "TextEdit", "", "Ersatzschlüssel: {{place}}", mode="append")], g12, rd, [])
    assert p and "whole text" in p[0]
    p = V.check_plan5([S("doc_save", "TextEdit", "", "Ersatzschlüssel: in der Blechdose hinter dem Blumentopf",
                         mode="append")], g12, rd, [])
    assert p and "copied from the text read" in p[0]
    assert V.check_plan5([S("extract", "none", "the hiding place", "p", find="in der Blechdose hinter dem Blumentopf im Hof"),
                          S("doc_save", "TextEdit", "", "Ersatzschlüssel: {{p}}", mode="append")], g12, rd, []) == []
    # an edit's new words come from the goal: not a copy
    assert V.check_plan5([S("doc_save", "TextEdit", "", "Lieferung: Donnerstag", mode="edit", find="Lieferung: Dienstag")],
                         "nur „Lieferung: Dienstag“ zu „Lieferung: Donnerstag“ ändern", {"t": "Lieferung: Dienstag"}, []) == []
    # GA07: reading "the note" reads the one the screen shows, which the goal does not name
    g7 = "Schau mal in meinen Rezeptnotizen nach dem Wort für diese kleinen Brotwürfel und such es im Duden nach."
    sc = {"notes_folder": "Suppenrezepte", "notes_note": "Tomatensuppe"}
    p = V.check_plan5([S("launch_app", "Notes"), S("read_content", "Notes", "note", "text")], g7, {}, [], (), sc)
    assert p and "shown on the screen" in p[0]
    assert V.check_plan5([S("open_item", "Notes", "Notiz mit den Brotwürfeln"), S("read_content", "Notes", "note", "t")],
                         g7, {}, [], (), sc) == []
    assert V.check_plan5([S("read_content", "Notes", "note", "t")], "Lies die offene Notiz vor.", {}, [], (), sc) == []


def _dict_task(tid, text, folder, note, word):
    return X.Task(tid, text, "cross-app", {"app": NOTES},
                  [{"kind": "notes", "folders": [folder]}, {"kind": "dictionary"}],
                  {"notes_shown": {"folder": folder, "note": note}, "dictionary_search": word})


def test_ga02_a_title_search_after_the_read_is_fed_back_and_the_extracted_word_is_searched():
    g = FakeGuest5(notes={"Werkstatt": [("Türgriff", "Türgriff\nDer Türgriff wackelt."),
                                        ("Quietschendes Scharnier", HINGE)]})
    g.front = NOTES
    t = _dict_task("E1", "Find the note about the squeaky hinge, then look up the German word I wrote for it.",
                   "Werkstatt", "Quietschendes Scharnier", "Scharnier")
    first = plan4(S("launch_app", "Notes"), S("open_item", "Notes", "note about the squeaky hinge"),
                  S("read_content", "Notes", "note", "note"))
    title = plan4(S("launch_app", "Dictionary"), S("scoped_find", "Dictionary", "", "Quietschendes Scharnier"))
    fixed = plan4(S("extract", "none", "the German word written for the hinge", "word", find="Scharnier"),
                  S("launch_app", "Dictionary"), S("scoped_find", "Dictionary", "", "{{word}}"))
    p = FakePlanner4(first, title, fixed)
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p, T3.FakeChooser("Scharnier", conf=0.95)), emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out.get("stopped"))
    assert p.calls == 3 and out["check_feedbacks"] == 1 and g.dict_search == "Scharnier"
    assert "The engine's check found" in p.histories[2][-1]["content"]
    assert out["extracts"][0]["bound"] == "Scharnier"
    # a second answer with the same problem is not executed: no search at all
    g = FakeGuest5(notes={"Werkstatt": [("Quietschendes Scharnier", HINGE), ("Türgriff", "Türgriff\nx")]})
    g.front = NOTES
    p = FakePlanner4(first, title, title)
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p, T3.FakeChooser("Scharnier", conf=0.95)), emap=EMAP)
    assert "plan check after feedback" in out["stopped"] and g.dict_search == ""
    assert out["judge"]["wrong_actions"] == 0


def test_ga07_the_note_the_screen_shows_is_not_taken_for_the_one_the_user_describes():
    g = FakeGuest5(notes=SOUP)
    g.front, g.notes_folder, g.shown_note = NOTES, "Suppenrezepte", "Tomatensuppe"
    t = _dict_task("E2", "Schau mal in meinen Rezeptnotizen nach dem Wort für diese kleinen Brotwürfel in der Suppe "
                   "und such genau das im Duden nach.", "Suppenrezepte", "Kürbissuppe", "Croûtons")
    # the planner names the note the screen shows: not the user's words -> the picker reads the rows' content
    first = plan4(S("launch_app", "Notes"), S("open_item", "Notes", "Tomatensuppe"), S("read_content", "Notes", "note", "t"))
    rest = plan4(S("extract", "none", "the word for the bread cubes", "w", find="Croutons"),
                 S("launch_app", "Dictionary"), S("scoped_find", "Dictionary", "", "{{w}}"))
    p = FakePlanner4(first, rest)
    ch = T3.FakeChooser("Croûtons", conf=0.9)
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p, ch), emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out.get("stopped"), [s.get("resolution") for s in out["steps"]])
    op = next(s for s in out["steps"] if s["step"]["effect"] == "open_item")
    assert op["resolution"]["by"] == "picker" and "user's words" in op["resolution"]["why"]
    assert g.dict_search == "Croûtons"                                         # the text's spelling, not "Croutons"
    # compiled: 0 model calls, the read and the extract replayed
    g2 = FakeGuest5(notes=SOUP)
    g2.front, g2.notes_folder, g2.shown_note = NOTES, "Suppenrezepte", "Tomatensuppe"
    out2 = W.run_one4(t, "arch", 1, g2, arch=V.Arch5(g2, EMAP, sleep=lambda s: None), compiled=out["compiled"],
                      mode="compiled", emap=EMAP)
    assert out2["judge"]["success"] and out2["model_calls"] == 0 and g2.dict_search == "Croûtons"


def test_ga12_the_place_is_extracted_and_appended_not_the_whole_note():
    notes = {"Schlüssel": [("Ersatzschlüssel Hof", "Ersatzschlüssel Hof\nErsatzschlüssel: in der Blechdose im Hof."),
                           ("Hauptschlüssel", "Hauptschlüssel\nbleibt bei Nora.")]}
    g = FakeGuest5({"Ü/Übergabe.txt": "Fenster schließen."}, shown="Ü", te_front="Ü/Übergabe.txt", notes=notes)
    g.front, g.notes_folder, g.shown_note = NOTES, "Schlüssel", "Hauptschlüssel"
    t = T4.task("E3", "Open the note about the spare key in the courtyard, then add its hiding place to the end of "
                "the open handover file as „Ersatzschlüssel: …“.",
                [{"kind": "folder", "path": ROOT + "/Ü"}],
                {"file_text": {ROOT + "/Ü/Übergabe.txt": "Fenster schließen.\nErsatzschlüssel: in der Blechdose im Hof."}})
    first = plan4(S("launch_app", "Notes"), S("open_item", "Notes", "spare key courtyard"),
                  S("read_content", "Notes", "note", "place"), S("launch_app", "TextEdit"),
                  S("doc_save", "TextEdit", "", "Ersatzschlüssel: {{place}}", mode="append"))
    rest = plan4(S("extract", "none", "the hiding place", "spot", find="in der Blechdose im Hof."),
                 S("doc_save", "TextEdit", "", "Ersatzschlüssel: {{spot}}", mode="append"))
    p = FakePlanner4(first, rest)
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p, T3.FakeChooser("Ersatzschlüssel Hof", conf=0.95)), emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out.get("stopped"))
    assert g.fs[ROOT + "/Ü/Übergabe.txt"] == "Fenster schließen.\nErsatzschlüssel: in der Blechdose im Hof."
    assert out["continuations"] == 1


def test_planner_w5_is_derived_from_w4_and_states_extract_and_the_described_item_rule():
    spec = V.load_planner5_spec()
    assert hashlib.sha256(spec["w5_system"].encode()).hexdigest() == spec["w5_sha256"]
    w5 = spec["w5_system"]
    for must in ("- extract(", "never the item's title", "The item the user describes", "open it first (open_item)",
                 "mode replace on a document that already has text only when the goal explicitly asks",
                 "Never express an irreversible action", "then human_gate", "the whole plan is one refuse step"):
        assert must in w5, must
    enum = spec["plan_schema"]["properties"]["steps"]["items"]["properties"]["effect"]["enum"]
    assert "extract" in enum and [t["name"] for t in spec["types"]].count("extract") == 1
    assert "{problems}" in spec["check_feedback_template"] and "extract" in spec["continuation_template"]
    assert spec["call"]["model"] == "gpt-oss-120b"
    assert W.parse_plan4(plan4(S("extract", "none", "the word", "w", find="Scharnier")), spec)[1] is None


# ── 4. write safety: journal, restore, the replace gate ──────────────────────

class CorruptingGuest(FakeGuest5):
    """The first document write lands garbled (a slipped keystroke), later ones exactly."""

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if target.get("document") and not getattr(self, "corrupted", False):
            self.corrupted = True
            return super().op_enter(bundle, target, text + " x", commit, keys)
        return super().op_enter(bundle, target, text, commit, keys)


def _doc_task(tid, text, want):
    return T4.task(tid, text, [{"kind": "folder", "path": ROOT + "/P"}, {"kind": "front"}],
                   {"file_text": {ROOT + "/P/Preise.txt": want}})


def test_explicit_replace_needs_the_users_words():
    assert V.explicit_replace("Überschreib den Text mit „Geschlossen“.")
    assert V.explicit_replace("Replace the whole text with 'Closed'.")
    assert V.explicit_replace("Ersetze den ganzen Inhalt durch „Neu“.")
    assert not V.explicit_replace("Ändere den Betrag auf 130 Euro.")
    assert not V.explicit_replace("Ersetze 45 Euro durch 130 Euro.")          # a named part: an edit
    assert V.replaces_existing("alt", "replace", "neu") and not V.replaces_existing("", "replace", "neu")
    assert not V.replaces_existing("alt", "append", "neu") and not V.replaces_existing("alt\n", "replace", "alt")


def test_replacing_existing_text_without_an_explicit_instruction_is_gated_and_nothing_is_written():
    g = FakeGuest5({"P/Preise.txt": "Frühstück: 12 €\nKaffee: 3 €"}, shown="P", te_front="P/Preise.txt")
    g.front = TE
    t = _doc_task("J1", "Mach den Frühstückspreis auf 14 € und speicher.", "Frühstück: 14 €\nKaffee: 3 €")
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"), S("doc_save", "TextEdit", "", "Frühstück: 14 €", mode="replace")))
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p), emap=EMAP)
    assert out["stopped"] == "human gate (declined)" and out["gated"] >= 1
    assert g.fs[ROOT + "/P/Preise.txt"] == "Frühstück: 12 €\nKaffee: 3 €" and out["judge"]["wrong_actions"] == 0
    assert "without an explicit instruction" in out["steps"][-1]["why"] and not ops(g, "enter")
    # compiled replay: the gate again, 0 model calls
    g2 = FakeGuest5({"P/Preise.txt": "Frühstück: 12 €\nKaffee: 3 €"}, shown="P", te_front="P/Preise.txt")
    g2.front = TE
    out2 = W.run_one4(t, "arch", 1, g2, arch=V.Arch5(g2, EMAP, sleep=lambda s: None), compiled=out["compiled"],
                      mode="compiled", emap=EMAP)
    assert out2["stopped"] == "human gate (declined)" and out2["model_calls"] == 0 and not ops(g2, "enter")
    # the user's own words ask for it: allowed (and journalled)
    g3 = FakeGuest5({"P/Preise.txt": "alt"}, shown="P", te_front="P/Preise.txt")
    g3.front = TE
    t3 = _doc_task("J2", "Überschreib die offene Preisliste mit „Geschlossen“ und speicher.", "Geschlossen")
    p3 = FakePlanner4(plan4(S("launch_app", "TextEdit"), S("doc_save", "TextEdit", "", "Geschlossen", mode="replace")))
    out3 = W.run_one4(t3, "arch", 1, g3, arch=arch5(g3, p3), emap=EMAP)
    assert out3["judge"]["success"] and out3["journal"][0]["old_disk"] == "alt"


def test_a_failed_write_check_restores_the_journalled_text_and_the_run_is_a_refusal():
    old = "Frühstück: 12 €\nKaffee: 3 €"
    g = CorruptingGuest({"P/Preise.txt": old}, shown="P", te_front="P/Preise.txt")
    g.front = TE
    t = _doc_task("J3", "Mach den Frühstückspreis auf 14 € und speicher.", "Frühstück: 14 €\nKaffee: 3 €")
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"),
                           S("doc_save", "TextEdit", "", "Frühstück: 14 €", mode="edit", find="Frühstück: 12 €")))
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p), emap=EMAP)
    assert out["stopped"] == "refused (write check failed: the old text restored)", out["stopped"]
    assert g.fs[ROOT + "/P/Preise.txt"] == old and out["restores"] == 1
    j = out["journal"][0]
    assert j["old_disk"] == old and j["restored"] is True and j["mode"] == "edit"
    assert j["sha256"] == hashlib.sha256(old.encode()).hexdigest()
    assert out["judge"]["wrong_actions"] == 0 and p.calls == 1                  # no re-plan after a restore


def test_a_different_front_document_after_the_save_restores_too():
    old = "A: 1"
    g = FakeGuest5({"P/Preise.txt": old, "P/Andere.txt": "B: 2"}, shown="P", te_front="P/Preise.txt")
    g.front = TE
    orig_front, orig_press = g.op_front, g.op_press
    saved = {"n": 0}

    def press(bundle, trigger, label):
        r = orig_press(bundle, trigger, label)
        saved["n"] += 1
        return r

    def front():                                     # after the first save another document is in front
        r = orig_front()
        return dict(r, document=ROOT + "/P/Andere.txt") if saved["n"] == 1 else r
    g.op_front, g.op_press = front, press
    t = _doc_task("J4", "Set A to 2 and save.", "A: 2")
    p = FakePlanner4(plan4(S("doc_save", "TextEdit", "", "A: 2", mode="edit", find="A: 1")))
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p), emap=EMAP)
    assert out["stopped"].startswith("refused (write check failed"), (out["stopped"], out["steps"][-1].get("why"))
    assert "front document" in out["steps"][-1]["why"] and g.fs[ROOT + "/P/Preise.txt"] == old


def test_the_journal_holds_every_write_to_existing_content_and_new_documents_need_none():
    g = FakeGuest5({"P/Preise.txt": "Frühstück: 12 €"}, shown="P", te_front="P/Preise.txt")
    g.front = TE
    t = _doc_task("J5", "Häng „Kaffee: 3 €“ an die offene Preisliste an.", "Frühstück: 12 €\nKaffee: 3 €")
    p = FakePlanner4(plan4(S("doc_save", "TextEdit", "", "Kaffee: 3 €", mode="append")))
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, p), emap=EMAP)
    assert out["judge"]["success"] and len(out["journal"]) == 1 and out["journal"][0]["restored"] is None
    g = FakeGuest5({"P/Neu.txt": ""}, shown="P", te_front="P/Neu.txt")
    g.front = TE
    t = T4.task("J6", "Schreib „Hallo“ in das neue Dokument und sichere es.", [{"kind": "folder", "path": ROOT + "/P"}],
                {"file_text": {ROOT + "/P/Neu.txt": "Hallo"}})
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, FakePlanner4(plan4(S("doc_save", "TextEdit", "", "Hallo")))), emap=EMAP)
    assert out["judge"]["success"] and out["journal"] == []


# ── 5. a setting the planner named in its own words is cross-checked ─────────

G11 = "Kannst du nur „Lieferung: Dienstag“ zu „Lieferung: Donnerstag“ ändern? Lass die automatische " \
      "Rechtschreibkorrektur dabei aus, die Prüf-Markierung aber an."


def _step(a, step, goal):
    rec = X.Rec("F", "arch", 1)
    log = {"i": 0, "step": step}
    out = a.do_step(X.Task("F", goal, "single-app", {}, [], {}), rec, step, log)
    return out, log, rec


def test_user_named_option_is_the_goals_own_word():
    assert V.user_named_option("Duden-Wissensnetz deutsche Sprache", "Such das im Duden nach.")
    assert V.user_named_option("Oxford German Dictionary", "look it up in the German–English dictionary")
    assert not V.user_named_option("Apple Dictionary", "look it up in the German–English dictionary")
    assert not V.user_named_option("Rechtschreib- und Grammatikprüfung", G11)
    assert not V.user_named_option("Während der Texteingabe prüfen", G11)


def test_a_confident_contradiction_hands_off_before_any_press_ga11():
    g = FakeGuest5()
    a = arch5(g, FakePlanner4(plan4()), T3.FakeChooser("Während der Texteingabe prüfen", conf=0.95), threshold=0.75)
    out, log, rec = _step(a, S("flag_toggle", "TextEdit", "Rechtschreib- und Grammatikprüfung", "on"), G11)
    assert out == "handoff" and log["crosscheck"]["verdict"] == "contradicts" and not ops(g, "press")
    assert "switched on" in log["crosscheck"]["question"] and rec.handoffs == 1


def test_an_agreeing_or_unsure_picker_leaves_the_planners_option():
    g = FakeGuest5()
    a = arch5(g, FakePlanner4(plan4()), T3.FakeChooser("Während der Texteingabe prüfen", conf=0.95), threshold=0.75)
    out, log, _r = _step(a, S("flag_toggle", "TextEdit", "Während der Texteingabe prüfen", "on"), G11)
    assert log["crosscheck"]["verdict"] == "agrees" and out != "handoff"
    a = arch5(g, FakePlanner4(plan4()), T3.FakeChooser("Während der Texteingabe prüfen", conf=0.4), threshold=0.75)
    out, log, _r = _step(a, S("flag_toggle", "TextEdit", "Rechtschreib- und Grammatikprüfung", "on"), G11)
    assert log["crosscheck"]["verdict"].startswith("unsure") and out != "handoff"


def test_a_dictionary_source_the_user_did_not_name_is_cross_checked_ga02():
    g = FakeGuest5()
    goal = "Find the note about the squeaky hinge, then look up the German word in the German–English dictionary."
    a = arch5(g, FakePlanner4(plan4()), T3.FakeChooser("Oxford", conf=0.9), threshold=0.75)
    out, log, _r = _step(a, S("toggle_select", "Dictionary", "Suchen", "Apple Dictionary"), goal)
    assert out == "handoff" and log["crosscheck"]["planned"] == "Apple Dictionary" and not ops(g, "press")
    # the user's own word: no cross-check, no picker call
    ch = T3.FakeChooser("Oxford", conf=0.9)
    a = arch5(g, FakePlanner4(plan4()), ch, threshold=0.75)
    out, log, _r = _step(a, S("toggle_select", "Dictionary", "Suchen", "Duden-Wissensnetz deutsche Sprache"),
                         "Such es im Duden nach.")
    assert "crosscheck" not in log and ch.calls == 0


# ── the host driver and the unchanged arms ───────────────────────────────────


# ── development findings on SEEN tasks: date veto, stray searches, a picked folder ──

def test_a_pick_against_the_dates_the_target_asks_for_is_handed_off_s01():
    tree = {"U/Checkliste_alt.txt": "Alte Checkliste: Strom ummelden.", "U/Checkliste_neu.txt": "Neue Checkliste.",
            "U/Kartons.txt": "Kartons: 20"}
    dates = {f"{ROOT}/U/Checkliste_alt.txt": ("2026-08-20 09:15", "2026-08-20 09:15"),
             f"{ROOT}/U/Checkliste_neu.txt": ("2025-11-05 14:30", "2025-11-05 14:30"),
             f"{ROOT}/U/Kartons.txt": ("2026-09-01 10:00", "2026-09-01 10:00")}
    t = T4.task("D3", "Mach mal die neueste Checkliste auf und häng 'Kisten beschriften' an.",
                [{"kind": "folder", "path": ROOT + "/U"}, {"kind": "front"}], {})
    plan = plan4(S("open_item", "Finder", "neueste Checkliste"),
                 S("doc_save", "TextEdit", "", "Kisten beschriften", mode="append"))
    g = FakeGuest5(dict(tree), shown="U", dates=dates)
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, FakePlanner4(plan), T3.FakeChooser("Checkliste_neu", conf=0.9),
                                                 threshold=0.75), emap=EMAP)
    pk = next(s for s in out["steps"] if s.get("pick"))["pick"]
    assert out["stopped"] == "picker hand-off (declined)" and "contradicts the newest" in pk["reason"]
    assert pk["date_check"]["extreme"] == 0 and g.fs[ROOT + "/U/Checkliste_neu.txt"] == "Neue Checkliste."
    # the pick the dates agree with is taken (the newest CHECKLIST, not the newer Kartons.txt)
    g = FakeGuest5(dict(tree), shown="U", dates=dates)
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, FakePlanner4(plan), T3.FakeChooser("Checkliste_alt", conf=0.9),
                                                 threshold=0.75), emap=EMAP)
    assert g.fs[ROOT + "/U/Checkliste_alt.txt"].endswith("Kisten beschriften")
    assert V.date_superlative("Checkliste", "Öffne die neueste Checkliste") == "newest"
    assert V.date_superlative("Vertrag", "Öffne die neueste Rechnung und den Vertrag") is None
    assert V.date_superlative("the oldest draft") == "oldest"


def test_a_search_before_a_pick_is_dropped_unless_the_goal_names_it_g04():
    g04 = "Die alte Packliste für die Hüttentour kann weg. Such sie bitte raus, aber frag mich, bevor du sie löschst."
    st = [S("navigate", "Notes", "Hüttentour Planung"), S("scoped_find", "Notes", "", "Packliste"),
          S("select_item", "Notes", "alte Packliste für die Hüttentour"), S("human_gate", "Notes", "Packliste", "delete")]
    r = V.refine_plan5(g04, st, EMAP)
    assert [s["effect"] for s in r.steps] == ["navigate", "select_item", "human_gate"]
    assert any("names no search for this pattern" in c for c in r.changes)
    for goal in ("Find the note where I wrote about the basil and leave the search for 'Basilikum'.",
                 "Such in den Notizen nach Basilikum und öffne die Notiz vom Balkon.",
                 "Search for Basilikum in my notes, then open the balcony note."):
        st = [S("scoped_find", "Notes", "", "Basilikum"), S("open_item", "Notes", "Balkon")]
        assert [s["effect"] for s in V.refine_plan5(goal, st, EMAP).steps] == ["scoped_find", "open_item"], goal
    assert not V.goal_names_search(g04, "Packliste")


def test_a_folder_the_picker_chose_for_a_described_file_is_entered_and_the_file_picked_there_g05():
    tree = {"S/Entwürfe/Elternabend Entwurf.txt": "Beginn: 19 Uhr", "S/Elternabend Fassung.txt": "Beginn: 19 Uhr (fertig)"}
    t = T4.task("D4", "Öffne den Entwurf für den Elternabend.",
                [{"kind": "front"}], {"opened": {"bundle": TE, "path": ROOT + "/S/Entwürfe/Elternabend Entwurf.txt"}})
    g = FakeGuest5(dict(tree), shown="S")
    ch = T3.FakeChooser(["Entwürfe", "Elternabend Entwurf"], conf=0.95)
    out = W.run_one4(t, "arch", 1, g, arch=arch5(g, FakePlanner4(plan4(S("open_item", "Finder", "Entwurf für den Elternabend"))),
                                                 ch, threshold=0.75), emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out.get("stopped"), out["steps"][-1].get("why"))
    st = out["steps"][0]
    assert st["folder_pick"]["entered"] == "Entwürfe" and st["resolved"] == {
        "op": "open", "route": "finder", "label": "Elternabend Entwurf.txt", "enter": ["Entwürfe"]}
    g2 = FakeGuest5(dict(tree), shown="S")
    out2 = W.run_one4(t, "arch", 1, g2, arch=V.Arch5(g2, EMAP, sleep=lambda s: None), compiled=out["compiled"],
                      mode="compiled", emap=EMAP)
    assert out2["judge"]["success"] and out2["model_calls"] == 0
    # a target that asks for a FOLDER stays in it (no second pick)
    g3 = FakeGuest5(dict(tree), shown="S")
    out3 = W.run_one4(T4.task("D5", "Geh in den Ordner mit den Entwürfen.", [{"kind": "front"}], {}), "arch", 1, g3,
                      arch=arch5(g3, FakePlanner4(plan4(S("open_item", "Finder", "Ordner mit den Entwürfen"))),
                                 T3.FakeChooser("Entwürfe", conf=0.95), threshold=0.75), emap=EMAP)
    assert "folder_pick" not in out3["steps"][0] and g3.shown.endswith("/S/Entwürfe")


# ── no Jev: picker mode "none" (a hand-off where the map does not decide) ──

def test_picker_mode_none_hands_off_without_any_model_call():
    tree = {"R/Rechnung_Strom.txt": "Strom", "R/Rechnung_Internet.txt": "Internet"}
    g = FakeGuest5(tree, shown="R")
    t = T4.task("N0", "Mach mal die neueste Rechnung auf.", [{"kind": "front"}], {})
    a = V.Arch5(g, EMAP, planner=FakePlanner4(plan4(S("open_item", "Finder", "neueste Rechnung"))), picker_name="none",
                inbox=None, sleep=lambda s: None)
    assert a.chooser is None
    out = W.run_one4(t, "arch", 1, g, arch=a, emap=EMAP)
    assert out["stopped"] == "picker hand-off (declined)" and out["calls"]["picker"] == 0
    assert out["steps"][-1]["pick"]["reason"] == "no picker (mode none): hand-off" and not ops(g, "enter")
    # a step the map decides (the user's own file name) proceeds
    g = FakeGuest5(tree, shown="R")
    t2 = T4.task("N1", "Öffne Rechnung_Strom.txt.", [{"kind": "front"}],
                 {"opened": {"bundle": TE, "path": ROOT + "/R/Rechnung_Strom.txt"}})
    a = V.Arch5(g, EMAP, planner=FakePlanner4(plan4(S("open_item", "Finder", "Rechnung_Strom.txt"))), picker_name="none",
                inbox=None, sleep=lambda s: None)
    out = W.run_one4(t2, "arch", 1, g, arch=a, emap=EMAP)
    assert out["judge"]["success"] and out["model_calls"] == 1                   # the planner only
    # the settings cross-check has no picker: the planner's option stands (logged)
    a = V.Arch5(FakeGuest5(), EMAP, planner=FakePlanner4(plan4()), picker_name="none", sleep=lambda s: None)
    _o, log, _r = _step(a, S("flag_toggle", "TextEdit", "Rechtschreib- und Grammatikprüfung", "on"), G11)
    assert log["crosscheck"]["verdict"].startswith("unsure (no picker")


def test_look_up_and_find_do_not_name_a_search_pattern():
    g = ("Find the note about the squeaky hinge, then look up the German word I wrote for it in the German–English "
         "dictionary. Not the note about the door handle.")
    assert not V.goal_names_search(g, "squeaky hinge")
    assert V.goal_names_search("Such nach Basilikum.", "Basilikum")
    assert V.goal_names_search("Bitte in den Notizen nach Basilikum suchen.", "Basilikum")


def test_judge_is_judge_plus_textedits_new_document_settings_sb07_like():
    t = X.Task("P5", "Schalt die automatische Rechtschreibkorrektur in TextEdit aus.", "single-app", {},
               [{"kind": "te_prefs"}], {"te_prefs": {"Rechtschreibung automatisch korrigieren": False}})
    s = [{"ok": True, "prefs": {"Rechtschreibung automatisch korrigieren": True, "Lineal einblenden": True}}]
    ok = V.judge(t, s, [{"ok": True, "prefs": {"Rechtschreibung automatisch korrigieren": False,
                                                "Lineal einblenden": True}}])
    assert ok["success"] and ok["wrong_actions"] == 0
    miss = V.judge(t, s, s)
    assert not miss["success"] and any(i["status"] == "missing" for i in miss["items"])
    other = V.judge(t, s, [{"ok": True, "prefs": {"Rechtschreibung automatisch korrigieren": False,
                                                   "Lineal einblenden": False}}])
    assert not other["success"] and other["wrong_actions"] == 1 and "Lineal einblenden" in other["unexpected"][0]
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index('    if k == "te_prefs":'):src.index('    if k == "dictionary":')]
    assert "te_prefs(None, [])" in body                                  # read only: nothing set


def test_a_hand_off_in_a_gallery_folder_opens_the_note_the_user_had_open_again():
    class Gallery(FakeGuest5):
        def op_rows(self, bundle, kind, back=None):
            r = super().op_rows(bundle, kind, back)
            if kind == "notes_notes" and back and self.shown_note:
                r = dict(r, went_back=True, shown_before=self.shown_note)
                self.shown_note = None                     # the grid hides the open note
            return r
    g = Gallery(notes={"W": [("Türgriff", "Türgriff\\nwackelt"), ("Scharnier", "Scharnier\\nquietscht")]})
    g.front, g.notes_folder, g.shown_note = NOTES, "W", "Türgriff"
    t = T4.task("H1", "Find the note about the squeaky hinge.", [{"kind": "notes", "folders": ["W"]}],
                {"notes_shown": {"folder": "W", "note": "Scharnier"}})
    a = V.Arch5(g, EMAP, planner=FakePlanner4(plan4(S("open_item", "Notes", "note about the squeaky hinge"))),
                picker_name="none", sleep=lambda s: None)
    out = W.run_one4(t, "arch", 1, g, arch=a, emap=EMAP)
    assert out["stopped"] == "picker hand-off (declined)" and g.shown_note == "Türgriff"
    assert out["steps"][-1]["view_restored"]["note"] == "Türgriff"


# ── infrastructure failures, the host-sleep check, a bounded Notes check ──

def _host():
    import importlib
    import sys
    here = os.path.join(os.path.dirname(os.path.abspath(X.__file__)), "..", "execution", "guest")
    sys.path.insert(0, here)
    return importlib.import_module("desktop_e2e5_host")


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
