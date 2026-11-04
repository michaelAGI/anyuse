"""Arch10, offline, with a FAKE guest
and fake models (the earlier arms' fakes).

What is pinned here, one block per build item:
2. the constraint filter: a candidate is contradicted only on an OBSERVED hard
   mismatch (GC06: a container named in another language; SE08: the
   kind "file" where the right move was the file's folder; localized system
   folder names; a move / put INTO X wants the container);
1. the options and the question: Arch8's rich option text, Arch9's typed
   evidence in the state, "unknown" is not a misfit, NONE only when clearly
   absent and at a fixed last position, the one inspection before the first ask;
3. the second, different picker (a JSON chooser) and the configurations
   openjev / cerebras / agree / cascade, decided offline exactly as in the arm;
4. a concurrent edit injected AFTER the pick and BEFORE the write is caught by
   the write-time check (nothing written, the concurrent edit survives).

No desktop, no AX, no subprocess, no network.
"""
import datetime as dt
import json
import os

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e8 as R8
from anyuse.execution import desktop_e2e9 as N
from anyuse.execution import desktop_e2e10 as T

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, NOTES, TE = T4.FINDER, T4.NOTES, T4.TE
ROOT = T4.ROOT
NOW = dt.datetime(2026, 9, 29, 12, 0)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FakeGuest9 = T9.FakeGuest9
HEAD_DE = ("Frontmost application: Finder. Window: Umzug_2026 (list view). The listed items are the rows of its file "
           "list. The user interface is in German.")


def fields(rows, lk="finder_files"):
    return [N.candidate_fields(r, NOW, lk) for r in rows]


def statuses(goal, step, rows, lk="finder_files", where="", where_path="", ui="de"):
    ks = T.goal_constraints10(goal, step, lk, NOW, where, [r["label"] for r in rows], ui)
    return ks, T.evaluate10(ks, fields(rows, lk), where, where_path)


def contradicted(st):
    return {k: w for k, (s, w) in st.items() if s == "contradicted"}


# ── 2. the constraint filter ─────────────────────────────────────────────────

GC06 = ("Open the newest draft in the Speeches folder and change its closing line from 'See you Monday' to "
        "'See you Tuesday'. Don't touch the draft beside it.")


def test_gc06_a_container_named_in_another_language_is_unknown_never_contradicted():
    # the drafts inside "Reden" (the German folder the English request calls "the Speeches folder")
    rows = [{"label": "Entwurf A.txt", "text": "Entwurf A.txt | modified 2026-09-20 10:00"},
            {"label": "Entwurf B.txt", "text": "Entwurf B.txt | modified 2026-09-27 18:00"}]
    _ks, st = statuses(GC06, {"effect": "open_item", "target": "newest draft"}, rows, where="Reden",
                       where_path="G/Vereinsabend Reden/Reden")
    assert [s["container"][0] for s in st] == ["unknown", "unknown"] and not contradicted(st[1])
    # one level up, the folder "Reden" itself lies in "Vereinsabend Reden"
    rows3 = [{"label": "Reden", "text": "Reden | folder | modified 2026-09-27 18:00"},
             {"label": "Ablauf.txt", "text": "Ablauf.txt | modified 2026-09-10 09:00"}]
    _ks, st3 = statuses(GC06, {"effect": "open_item", "target": "newest draft", "target_ui": "neuester Entwurf"},
                        rows3, where="Vereinsabend Reden", where_path="G/Vereinsabend Reden")
    assert not contradicted(st3[0]) and st3[0]["container"][0] == "unknown"
    # Arch9's filter contradicted both
    k9 = N.goal_constraints(GC06, {"effect": "open_item", "target": "newest draft"}, "finder_files", NOW, "Reden")
    assert N.evaluate(k9, fields(rows), "Reden", "G/Vereinsabend Reden/Reden")[1]["container"][0] == "contradicted"


def test_a_named_container_contradicts_when_observed_elsewhere_or_named_in_the_screens_language():
    goal = "Räum die doppelte Packliste weg. Nur bei den Bergtour-Notizen."
    rows = [{"label": "Packliste", "text": "Packliste | Zelt, Kocher"}, {"label": "Einkauf", "text": "Einkauf | Brot"}]
    ks, st = statuses(goal, {"effect": "select_item", "target": "doppelte Packliste", "scope": "Bergtour-Notizen"},
                      rows, "notes_notes", where="Notizen")
    assert next(k for k in ks if k["id"] == "container")["same_language"] is True
    assert all(s["container"][0] == "contradicted" for s in st)          # German request, German screen
    # the same request in English on a German screen: the name may be translated -> unknown
    _ks, st_en = statuses("Delete the duplicate packing list. Only in my Bergtour-Notizen folder.",
                          {"effect": "select_item", "target": "duplicate packing list"}, rows, "notes_notes",
                          where="Notizen")
    assert all(s["container"][0] == "unknown" for s in st_en)
    # observed elsewhere in the list: the file in the other folder is contradicted, whatever the language
    rows2 = [{"label": "Rede.txt", "text": "Rede.txt", "parent": "Entwürfe"},
             {"label": "Rede.txt", "text": "Rede.txt", "parent": "Archiv"}]
    _ks, st2 = statuses("Open the speech in the Archiv folder.", {"effect": "open_item", "target": "the speech"}, rows2)
    assert st2[0]["container"][0] == "contradicted" and st2[1]["container"][0] == "satisfied"


def test_localized_system_folder_names_match_across_languages():
    assert "dokumente" in T.localized("Documents") and "papierkorb" in T.localized("Trash")
    rows = [{"label": "Brief.txt", "text": "Brief.txt"}]
    _ks, st = statuses("Open the letter in my Documents folder.", {"effect": "open_item", "target": "the letter"},
                       rows, where="Dokumente", where_path="Dokumente")
    assert st[0]["container"][0] == "satisfied"


SE08 = "In the Nord flat's conditions file, change the deposit to 1200 euros. The final one, not the draft, and leave " \
       "the Süd flat alone."


def test_se08_a_wanted_file_never_contradicts_the_folder_it_may_lie_in():
    rows = [{"label": "checkliste_umzug.txt", "text": "checkliste_umzug.txt | file | modified 2026-09-05 19:00"},
            {"label": "Wohnung_Nord", "text": "Wohnung_Nord | folder | modified 2026-08-30 11:20"},
            {"label": "Wohnung_Sued", "text": "Wohnung_Sued | folder | modified 2026-09-01 10:05"}]
    step = {"effect": "open_item", "target": "final Nord flat conditions file",
            "target_ui": "endgültige Bedingungen der Nord-Wohnung"}
    _ks, st = statuses(SE08, step, rows)
    assert not contradicted(st[1]) and st[1]["kind"][0] == "unknown"             # Arch9 dropped it
    assert "exclude" in contradicted(st[2])                                      # the Süd flat stays excluded
    k9 = N.goal_constraints(SE08, step, "finder_files", NOW)
    assert N.evaluate(k9, fields(rows))[1]["kind"][0] == "contradicted"


def test_a_move_into_x_wants_the_container_and_the_kind_is_read_from_the_targets_head():
    want, why = T.target_kind10("Move the invoice file into the Archive folder.",
                                {"target": "into the Archive folder"})
    assert want == "folder" and "container" in why
    rows = [{"label": "Archive", "text": "Archive | folder"}, {"label": "Rechnung.txt", "text": "Rechnung.txt | file"}]
    _ks, st = statuses("Move the invoice file into the Archive folder.",
                       {"effect": "open_item", "target": "into the Archive folder"}, rows)
    assert st[0]["kind"][0] == "satisfied" and "kind" in contradicted(st[1])
    # a container phrase in the target names where the item lies, not what it is (Arch9 read "Ordner" -> folder)
    assert T.target_kind10("Öffne Entwurf B im Ordner Reden.", {"target": "Entwurf B.txt im Ordner Reden"})[0] == "file"
    assert T.target_kind10("Öffne den Ordner Reden.", {"target": "den Ordner Reden"})[0] == "folder"


def test_a_wanted_folder_still_contradicts_a_file_a_hard_mismatch():
    rows = [{"label": "Reden", "text": "Reden | folder"}, {"label": "Reden.txt", "text": "Reden.txt | file"}]
    _ks, st = statuses("Öffne den Ordner Reden.", {"effect": "open_item", "target": "den Ordner Reden"}, rows)
    assert not contradicted(st[0]) and "kind" in contradicted(st[1])


# ── 1. options, state, question ──────────────────────────────────────────────

ROWS_SE01 = [{"label": "angebot_dachdecker.txt", "text": "angebot_dachdecker.txt | modified 2026-09-10 11:05",
              "kind": "file", "content": "Angebot Dachdecker Gartenhaus", "content_known": "full"},
             {"label": "angebot_schreiner_v1.txt", "text": "angebot_schreiner_v1.txt | modified 2026-03-02 09:15",
              "kind": "file", "content": "Angebot Schreinerarbeiten Gartenhaus", "content_known": "full"},
             {"label": "angebot_schreiner_v2.txt", "text": "angebot_schreiner_v2.txt | modified 2026-08-19 14:40",
              "kind": "file", "content": "Angebot Schreinerarbeiten Gartenhaus", "content_known": "full"}]
GOAL_SE01 = T9.SE01
STEP_SE01 = {"effect": "open_item", "target": "das neueste Schreiner-Angebot", "target_ui": ""}


def test_the_options_are_prior_rich_texts_and_the_state_holds_the_typed_evidence():
    prep = T.prepare10(GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW)
    rich = R8.rich_options(T.arm_texts10(ROWS_SE01), NOW, parents=["", "", ""])
    assert prep["rich"] == rich and all(len(r) <= 120 for r in rich)
    assert "content: Angebot Schreinerarbeiten" in rich[2] and "modified 2026-08-19" in rich[2]
    assert T.option10("C3", rich[2], "rich") == rich[2]
    o = T.option10("C3", rich[2], "id_rich")
    assert o.startswith("C3: angebot_schreiner_v2.txt") and len(o) <= 120
    st = T.state10(prep["head"], prep["ids"], prep["fields"], prep["statuses"], prep["constraints"])
    assert T.LEGEND10 in st and "C3: angebot_schreiner_v2.txt" in st and "rank: satisfied" in st
    assert len(st) <= N.STATE_CHARS9


def test_the_question_says_unknown_is_not_a_misfit_and_none_only_when_clearly_absent():
    q = T.question10(GOAL_SE01, {"target": "the newest offer", "target_ui": "das neueste Angebot"}, "de")
    assert "\"unknown\" is NOT a misfit" in q and "only when the right item is clearly NOT among" in q
    assert "in the language of the screen (German): \"das neueste Angebot\"" in q
    assert "If no candidate fits every constraint" not in q                       # Arch9's wording is gone
    assert "Answer with JSON only" in T.question10(GOAL_SE01, STEP_SE01, "de", json_answer=True)


class RichOracle:
    """A fake OpenJev: the first option (NONE aside) that holds ``want``, else NONE; ``by_perm`` per permutation."""

    def __init__(self, want, conf=0.9, by_perm=None):
        self.want, self.conf, self.by_perm, self.asked = want, conf, by_perm, []

    def __call__(self, state, q, options):
        self.asked.append((state, q, list(options)))
        want = self.by_perm[(len(self.asked) - 1) % len(self.by_perm)] if self.by_perm else self.want
        for i, o in enumerate(options[:-1]):
            if want != "none" and want in o:
                return i, self.conf, {"cost": 0.0, "ms": 3}
        return len(options) - 1, self.conf, {"cost": 0.0, "ms": 3}


def test_openjev_is_asked_in_two_permutations_with_none_last_and_the_rich_options():
    prep = T.prepare10(GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW)
    o = RichOracle("schreiner_v2")
    ans, calls, _c = T.ask_openjev10(o, prep, GOAL_SE01, STEP_SE01, "rich", "de")
    assert calls == 2 and [a["id"] for a in ans] == ["C3", "C3"]
    (s0, _q, opts0), (s1, _q1, opts1) = o.asked
    assert opts0[-1] == opts1[-1] == T.NONE10 and opts0[:-1] == opts1[:-1][::-1]
    assert s0.index("C1: ") < s0.index("C3: ") and s1.index("C3: ") < s1.index("C1: ")
    assert N.verdict9(ans, 0.5)[0] == 2


def test_the_one_inspection_comes_before_the_first_ask():
    rows = [{"label": "Kosten_a.txt", "text": "Kosten_a.txt | modified 2026-02-01 09:00", "kind": "file"},
            {"label": "Kosten_b.txt", "text": "Kosten_b.txt | modified 2026-08-15 16:00", "kind": "file"}]
    seen = []

    def inspect(idx, cands):
        seen.append([c["label"] for c in cands])
        return [{"content": "Kostenaufstellung Bahn", "content_known": "full"},
                {"content": "Kostenaufstellung Busfahrten 3x", "content_known": "full"}]
    prep = T.prepare10("Öffne die Kostenaufstellung mit den Busfahrten.",
                       {"effect": "open_item", "target": "Kostenaufstellung mit den Busfahrten"}, "Finder.", rows,
                       "finder_files", NOW, inspect=inspect)
    assert seen == [["Kosten_a.txt", "Kosten_b.txt"]] and prep["inspected"]["got"] == ["C1", "C2"]
    st = T.state10(prep["head"], prep["ids"], prep["fields"], prep["statuses"], prep["constraints"])
    assert "full: Kostenaufstellung Busfahrten 3x" in st
    # the options stay what the arm SHOWED (Arch8's texts, before the inspection)
    assert "Busfahrten" not in " ".join(prep["rich"])


# ── 3. the second picker and the configurations ──────────────────────────────

class JsonOracle:
    """A fake JSON chooser: the ID whose option text holds ``want``, else NONE; ``by_perm`` per permutation."""

    def __init__(self, want, by_perm=None, cost=0.001):
        self.want, self.by_perm, self.cost, self.asked = want, by_perm, cost, []

    def __call__(self, state, q, options):
        self.asked.append((state, q, list(options)))
        want = self.by_perm[(len(self.asked) - 1) % len(self.by_perm)] if self.by_perm else self.want
        for cid, text in options:
            if want != "none" and want in text:
                return cid, False, {"cost": self.cost, "ms": 5}
        return None, True, {"cost": self.cost, "ms": 5}


def cfg(mode, t2=0.5, fmt="rich", positive=True):
    return {"mode": mode, "openjev": {"format": fmt, "t2": t2}, "cerebras": {"model": "fake"}, "positive": positive}


def test_the_json_messages_and_schema_carry_the_same_state_and_a_fixed_none():
    msgs = T.json_messages10("STATE X", "Q?", [("C2", "b.txt"), ("C1", "a.txt")])
    assert "STATE X" in msgs[1]["content"] and msgs[1]["content"].index("C2: b.txt") < msgs[1]["content"].index("C1: ")
    assert msgs[1]["content"].rstrip().split("\n")[-3] == T.NONE10
    assert T.json_schema10(["C1", "C2"])["properties"]["choice"]["enum"] == ["C1", "C2", "NONE"]
    assert T.parse_json10('{"choice": "C2", "none": false}', ["C1", "C2"]) == ("C2", False, None)
    assert T.parse_json10('{"choice": "C2", "none": true}', ["C1", "C2"]) == (None, True, None)   # conflict = NONE
    assert T.parse_json10('{"choice": "C9", "none": false}', ["C1", "C2"])[2]
    assert T.parse_json10("no json", ["C1"])[2]


def test_openjev_alone_cerebras_alone_and_agree():
    for mode, oj, js, want in (("openjev", RichOracle("schreiner_v2"), None, 2),
                               ("cerebras", None, JsonOracle("schreiner_v2"), 2),
                               ("agree", RichOracle("schreiner_v2"), JsonOracle("schreiner_v2"), 2),
                               ("agree", RichOracle("schreiner_v2"), JsonOracle("dachdecker"), None),
                               ("agree", RichOracle("schreiner_v2"), JsonOracle("none"), None)):
        r = T.pick10(cfg(mode), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW, openjev=oj, jchooser=js)
        assert r["index"] == want, (mode, r["reason"])
    r = T.pick10(cfg("agree"), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW,
                 openjev=RichOracle("schreiner_v2"), jchooser=JsonOracle("dachdecker"))
    assert "disagree" in r["reason"] and r["calls"] == 4 and r["calls_by"] == {"openjev": 2, "cerebras": 2}


def test_the_second_model_is_asked_only_when_openjev_accepted():
    js = JsonOracle("schreiner_v2")
    r = T.pick10(cfg("agree"), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW,
                 openjev=RichOracle(None, by_perm=["schreiner_v2", "none"]), jchooser=js)
    assert r["index"] is None and js.asked == [] and r["calls"] == 2 and "OpenJev" in r["reason"]


def test_cascade_asks_the_second_model_only_for_a_pick_that_leads_to_a_mutation():
    assert T.leads_to_mutation(GOAL_SE01, STEP_SE01, "finder_files")                 # "setz den Preis"
    read_goal = "Zeig mir das neueste Schreiner-Angebot."
    assert not T.leads_to_mutation(read_goal, STEP_SE01, "finder_files")
    assert not T.leads_to_mutation(GOAL_SE01, {"effect": "navigate"}, "finder_locations")
    assert T.leads_to_mutation("x", {}, "settings")
    js = JsonOracle("dachdecker")
    r = T.pick10(cfg("cascade"), read_goal, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW,
                 openjev=RichOracle("schreiner_v2"), jchooser=js)
    assert r["index"] == 2 and js.asked == []                                        # read-only: OpenJev alone
    r2 = T.pick10(cfg("cascade"), GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW,
                  openjev=RichOracle("schreiner_v2"), jchooser=js)
    assert r2["index"] is None and len(js.asked) == 2                                # a mutation: both must agree


def test_every_candidate_contradicted_is_a_hand_off_without_a_model_call_and_positive_evidence_is_kept():
    goal = "Lösch die Notiz mit dem alten Wlan-Passwort. Nicht die Notiz über die Reinigung."
    rows = [{"label": "Reinigung", "text": "Reinigung | Firma kommt dienstags."}]
    oj, js = RichOracle("Reinigung"), JsonOracle("Reinigung")
    r = T.pick10(cfg("agree"), goal, {"effect": "select_item", "target": "Notiz mit dem alten Wlan-Passwort"},
                 "Notes.", rows, "notes_notes", NOW, openjev=oj, jchooser=js)
    assert r["index"] is None and oj.asked == [] and js.asked == [] and "contradicts" in r["reason"]
    rows2 = rows + [{"label": "Büro", "text": "Büro | Schlüssel beim Empfang."}]
    r2 = T.pick10(cfg("openjev"), "Lösch die Notiz mit dem alten Wlan-Passwort.",
                  {"effect": "select_item", "target": "Notiz mit dem alten Wlan-Passwort"}, "Notes.", rows2,
                  "notes_notes", NOW, openjev=RichOracle("Büro", conf=0.99))
    assert r2["index"] is None and "positive evidence" in r2["reason"]


def test_decide10_reproduces_the_arms_decision_from_recorded_answers():
    prep = T.prepare10(GOAL_SE01, STEP_SE01, HEAD_DE, ROWS_SE01, "finder_files", NOW)
    ans = {"openjev:rich": T.ask_openjev10(RichOracle("schreiner_v2", conf=0.7), prep, GOAL_SE01, STEP_SE01,
                                           "rich")[0],
           "cerebras:fake": T.ask_json10(JsonOracle("dachdecker"), prep, GOAL_SE01, STEP_SE01)[0]}
    rec = {"ids": prep["ids"], "alive": prep["alive"], "positive": prep["positive"], "mutating": prep["mutating"],
           "answers": ans}
    assert T.decide10(rec, cfg("openjev", t2=0.5)) == ("C3", 2)
    assert T.decide10(rec, cfg("openjev", t2=0.8)) == (None, 2)
    assert T.decide10(rec, cfg("cerebras")) == ("C1", 2)                           # "Angebot": positive evidence
    assert T.decide10(rec, cfg("agree", t2=0.5)) == (None, 4)
    assert T.decide10(rec, cfg("agree", t2=0.8)) == (None, 2)                      # OpenJev handed off first
    assert T.decide10(dict(rec, mutating=False), cfg("cascade", t2=0.5)) == ("C3", 2)


def test_the_json_model_behind_the_text_interface_for_the_settings_cross_check():
    from anyuse.execution import desktop_e2e7 as R7
    ch = T.json_as_chooser(JsonOracle("Rechtschreibung"))
    assert ch("S", "Q", ["Grammatik", "Rechtschreibung", R7.NONE_OPTION])[:2] == (1, 1.0)
    ch2 = T.json_as_chooser(JsonOracle("none"))
    assert ch2("S", "Q", [R7.NONE_OPTION, "Grammatik", "Rechtschreibung"])[:2] == (0, 1.0)


# ── the arm: fakes end to end ────────────────────────────────────────────────

V2 = T9.V2


def arch10(guest, planner, oj=None, js=None, mode="openjev", **kw):
    return T.Arch10(guest, EMAP, planner=planner, chooser=oj, jchooser=js, config10=cfg(mode), spend=None,
                    inbox=None, sleep=lambda s: None, **kw)


def test_se01_like_agree_edits_the_newest_offer_and_a_disagreement_writes_nothing():
    g = T9._se01_guest()
    rec = X.Rec("SE01", "arch", 1)
    arch10(g, FakePlanner4(*T9.SE01_PLAN), RichOracle("schreiner_v2"), JsonOracle("schreiner_v2"), "agree").run(
        X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2] == "Angebot Schreinerarbeiten Gartenhaus\nPreis: 4.850 Euro", (rec.stopped, rec.steps[-1])
    assert rec.picker_calls10 == {"openjev": 2, "cerebras": 2} and not rec.stopped
    g2 = T9._se01_guest()
    rec2 = X.Rec("SE01", "arch", 1)
    arch10(g2, FakePlanner4(*T9.SE01_PLAN), RichOracle("schreiner_v2"), JsonOracle("dachdecker"), "agree").run(
        X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec2)
    assert "5.050" in g2.fs[V2] and rec2.stopped == "picker hand-off (declined)" and T9.ops(g2, "enter") == []


def test_cerebras_alone_needs_no_openjev():
    g = T9._se01_guest()
    rec = X.Rec("SE01", "arch", 1)
    arch10(g, FakePlanner4(*T9.SE01_PLAN), None, JsonOracle("schreiner_v2"), "cerebras").run(
        X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2].endswith("4.850 Euro") and rec.picker_calls10 == {"cerebras": 2}


# ── 4. the concurrent edit after the pick and before the write ───────────────

def test_a_concurrent_edit_after_the_pick_and_before_the_write_is_caught_by_the_write_time_check():
    g = T9._se01_guest()
    real = g.op_field
    n = {"k": 0}

    def field_then_edit(bundle, role=None, identifier=None, **kw):
        r = real(bundle, role=role, identifier=identifier, **kw)
        if bundle == TE:
            n["k"] += 1
            if n["k"] == 1:                     # right after the write's own read of the text shown
                g.fs[V2] = g.fs[V2] + "\nNotiz: telefonisch bestätigt"
        return r
    g.op_field = field_then_edit
    rec = X.Rec("SE01", "arch", 1)
    arch10(g, FakePlanner4(*T9.SE01_PLAN), RichOracle("schreiner_v2")).run(
        X.Task("SE01", GOAL_SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2].endswith("Notiz: telefonisch bestätigt") and "5.050" in g.fs[V2]
    assert rec.steps[-1]["why"].startswith("concurrency: the file changed on disk between the read and the write")
    assert T9.ops(g, "enter") == [] and rec.stopped == "hand-off (declined): concurrency" and rec.errors9 == []


def test_the_frozen_fault_cases_hold_prior_twelve_and_four_concurrent_edits_after_the_pick():
    spec = json.load(open(os.path.join(HERE, "execution", "desktop_e2e10_faults.json"), encoding="utf-8"))
    cases = {c["id"]: c for c in spec["cases"] if not c.get("dev")}
    r9 = {c["id"]: c for c in json.load(open(os.path.join(HERE, "execution", "desktop_e2e9_faults.json"),
                                             encoding="utf-8"))["cases"] if not c.get("dev")}
    assert len(cases) == 16 and all(cases[k] == r9[k] for k in r9)
    new = [cases[k] for k in ("CE5", "CE6", "CE7", "CE8")]
    assert [c["task"] for c in new] == [r9[k]["task"] for k in ("CE1", "CE2", "CE3", "CE4")]
    for c in new:
        assert c["kind"] == "concurrent_edit" and c["at"] == {"op": "field", "when": "after",
                                                              "bundle": "com.apple.TextEdit", "nth": 1}


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
