"""Arch9, offline, with a FAKE guest
and fake models (the Arch4-8 fakes, extended).

What is pinned here, one block per build item:
1. typed candidate evidence: every explicit constraint (kind, container, period,
   identity, exclusions, rank) marks each candidate satisfied / contradicted /
   unknown from observed evidence only; a stated period is not a modification
   time; options are short stable IDs, the evidence is in the state;
2. recovery before the pick: contradicted candidates dropped, NONE last, two
   permutations, one read-only inspection and one retry, unresolved = hand-off,
   no pairwise tie-break, no date veto;
3. the pre-action identity recheck (a map-resolved row, the document written,
   the item before a gate);
4. the exact permitted diff and journalled recovery (the literal kept whole, the
   concurrency and focus checks, a restored mistake recorded as an error);
5. the two planner checks (an anchor verbatim in the observed text; a write task
   that ends after reads);
plus judge and the guest's two new ops.

No desktop, no AX, no subprocess, no network.
"""
import datetime as dt
import hashlib
import json
import os

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e9 as N

import test_desktop_e2e4 as T4
import test_desktop_e2e6 as T6

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, NOTES, TE = T4.FINDER, T4.NOTES, T4.TE
ROOT = T4.ROOT
NOW = dt.datetime(2026, 9, 29, 12, 0)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha(t):
    return hashlib.sha256((t or "").encode()).hexdigest()


class FakeGuest9(T6.FakeGuest6):
    """``FakeGuest6`` plus the file's sha256, Arch9's ``restore_file`` (compare-and-swap) and a hook
    that runs a fault before a chosen op (the host's fault channel)."""

    def __init__(self, *a, before=None, **kw):
        super().__init__(*a, **kw)
        self.before = before or {}               # op -> callable(guest, kwargs) run once before that op

    def __call__(self, op, **a):
        f = self.before.pop(op, None)
        if f is not None:
            f(self, a)
        return super().__call__(op, **a)

    def op_file(self, path):
        t = self.fs.get(path)
        return {"status": "done", "ok": t is not None, "text": t, "mtime": self.mtime, "sha256": sha(t)}

    def op_restore_file(self, path, expect_sha256, text):
        if sha(self.fs.get(path)) != expect_sha256:
            return {"status": "refused", "detail": "the file changed since it was observed"}
        self.fs[path] = text
        self.te_docs[path] = text
        self.mtime += 1
        return {"status": "done", "ok": True, "sha256": sha(text)}

    def op_listing(self, path):
        return {"status": "done", "names": self.kids(path)}


class IdOracle:
    """A fake OpenJev for ID options: it reads the state's candidate lines ("C2: name | …") and answers the ID
    whose line holds ``want`` (or NONE), in every permutation; ``by_perm`` answers per permutation."""

    def __init__(self, want, conf=0.9, by_perm=None):
        self.want, self.conf, self.by_perm = want, conf, by_perm
        self.asked = []

    def __call__(self, state, q, options):
        self.asked.append((state, q, list(options)))
        want = self.want
        if self.by_perm:
            want = self.by_perm[(len(self.asked) - 1) % len(self.by_perm)]
        if want == "none":
            return len(options) - 1, self.conf, {"cost": 0.0}
        lines = {ln.split(":", 1)[0]: ln for ln in state.split("\n") if ln[:1] == "C" and ":" in ln}
        for i, o in enumerate(options[:-1]):
            cid = o.split()[0]
            if want in lines.get(cid, ""):
                return i, self.conf, {"cost": 0.0}
        return len(options) - 1, self.conf, {"cost": 0.0}


def arch9(guest, planner, chooser=None, t2=0.5, fmt="id", positive=True, **kw):
    return N.Arch9(guest, EMAP, planner=planner, chooser=chooser, spend=None, inbox=None, sleep=lambda s: None,
                   picker_name="openjev" if chooser is not None else None,
                   config9={"t2": t2, "format": fmt, "positive": positive}, **kw)


def ops(g, name):
    return [a for op, a in g.log if op == name]


# ── 1. typed candidate evidence ──────────────────────────────────────────────

def fields(rows, lk="finder_files"):
    return [N.candidate_fields(r, NOW, lk) for r in rows]


def test_a_stated_period_is_not_the_modification_date_and_last_month_is_resolved_on_the_clock():
    per = N.goal_period("die Vorstandsprotokolle vom März", NOW)
    assert per["semantics"] == "stated" and per["month_set"] == [3]
    rows = [{"label": "protokoll_vorstand_maerz.txt", "text": "protokoll_vorstand_maerz.txt | modified 2026-09-02 10:00"},
            {"label": "protokoll_juni.txt", "text": "protokoll_juni.txt | modified 2026-03-12 10:00"},
            {"label": "notizen.txt", "text": "notizen.txt | modified 2026-03-12 10:00"}]
    st = N.evaluate([{"id": "period", "period": per, "text": ""}], fields(rows))
    # the March protocol modified in September states March: satisfied; June modified in March: contradicted
    assert [s["period"][0] for s in st] == ["satisfied", "contradicted", "unknown"]
    last = N.goal_period("the minutes from last month", NOW)
    assert last["months"] == [(2026, 8)] and last["resolved"] == "2026-08"
    mod = N.goal_period("the file from last month", NOW, "the file I changed last month")
    assert mod["semantics"] == "modified"
    st2 = N.evaluate([{"id": "period", "period": mod, "text": ""}], fields(rows))
    assert [s["period"][0] for s in st2] == ["contradicted", "contradicted", "contradicted"]


def test_the_rank_compares_only_the_best_matches_of_the_users_words_se01_like():
    goal = "Mach mal das neueste Schreiner-Angebot auf und setz den Preis auf 4.850 Euro."
    step = {"effect": "open_item", "target": "das neueste Schreiner-Angebot", "target_ui": ""}
    rows = [{"label": "angebot_dachdecker.txt", "text": "angebot_dachdecker.txt | modified 2026-09-10 11:05",
             "content": "Angebot Dachdecker Gartenhaus", "content_known": "full"},
            {"label": "angebot_schreiner_v1.txt", "text": "angebot_schreiner_v1.txt | modified 2026-03-02 09:15",
             "content": "Angebot Schreinerarbeiten Gartenhaus", "content_known": "full"},
            {"label": "angebot_schreiner_v2.txt", "text": "angebot_schreiner_v2.txt | modified 2026-08-19 14:40",
             "content": "Angebot Schreinerarbeiten Gartenhaus", "content_known": "full"}]
    ks = N.goal_constraints(goal, step, "finder_files", NOW, "", [r["label"] for r in rows])
    st = N.evaluate(ks, fields(rows))
    # the roofer's offer is newer but not a Schreiner offer: never compared (Arch5's veto compared it)
    assert st[0]["rank"][0] == "unknown" and st[1]["rank"][0] == "contradicted" and st[2]["rank"][0] == "satisfied"
    # same stated date: the modification date decides (SE15: the older of two March protocols)
    rows2 = [{"label": "protokoll_vorstand_maerz.txt", "text": "protokoll_vorstand_maerz.txt | modified 2026-03-30 20:10",
              "content": "Protokoll Vorstandssitzung 12.03.2026 (überarbeitet)", "content_known": "full"},
             {"label": "protokoll_vorstand_maerz_2.txt", "text": "protokoll_vorstand_maerz_2.txt | modified 2026-03-12 22:05",
              "content": "Protokoll Vorstandssitzung 12.03.2026", "content_known": "full"}]
    ks2 = N.goal_constraints("Schmeiß bitte die ältere der beiden Vorstandsprotokolle vom März weg.",
                             {"target": "die ältere der beiden Vorstandsprotokolle vom März"}, "finder_files", NOW)
    st2 = N.evaluate(ks2, fields(rows2))
    assert st2[0]["rank"][0] == "contradicted" and st2[1]["rank"][0] == "satisfied"
    assert "modification date" in st2[1]["rank"][1]


def test_an_exclusion_contradicts_only_by_the_words_that_tell_candidates_apart():
    goal = ("In der offenen Lieferliste fehlt noch die Rückgabe. Pack den Rückgabetermin aus der bestätigten Notiz "
            "dahinter. Nicht den vorläufigen Termin nehmen.")
    step = {"target": "die bestätigte Notiz"}
    rows = [{"label": "Termin vorläufig", "text": "Termin vorläufig | Rückgabe: 06.10.2026, 09:00"},
            {"label": "Termin bestätigt", "text": "Termin bestätigt | Rückgabe: 08.10.2026, 09:30"},
            {"label": "Lieferung bestätigt", "text": "Lieferung bestätigt | Lieferung: 08.10.2026"}]
    ks = N.goal_constraints(goal, step, "notes_notes", NOW)
    st = N.evaluate(ks, fields(rows, "notes_notes"))
    assert [s["exclude"][0] for s in st] == ["contradicted", "satisfied", "satisfied"]
    # a word the excluded phrase shares with the user's own words for the item never excludes
    ks2 = N.goal_constraints("Open the inventory for the projector. Not the spare projector's inventory.",
                             {"target": "inventory for the projector"}, "finder_files", NOW)
    rows2 = [{"label": "Projektor A Inventar.txt", "text": "Projektor A Inventar.txt"},
             {"label": "Projektor B Inventar.txt", "text": "Projektor B Inventar.txt"}]
    assert all(s.get("exclude", ("satisfied",))[0] == "satisfied" for s in N.evaluate(ks2, fields(rows2)))


def test_a_named_container_contradicts_a_description_in_another_language_does_not():
    rows = [{"label": "Zusage Fenster", "text": "Zusage Fenster | Vorläufig: Lea Meier"}]
    ks = N.goal_constraints("Delete the note from my Werkstatt-Log folder.", {"target": "the note"}, "notes_notes", NOW)
    assert N.evaluate(ks, fields(rows, "notes_notes"), where="Salonabend Vormerkungen")[0]["container"][0] == \
        "contradicted"
    assert N.evaluate(ks, fields(rows, "notes_notes"), where="Werkstatt-Log")[0]["container"][0] == "satisfied"
    ks2 = N.goal_constraints("Use the final headcount from the dinner notes.", {"target": "headcount"},
                             "notes_notes", NOW)
    assert N.evaluate(ks2, fields(rows, "notes_notes"), where="Herbstessen Zusagen")[0]["container"][0] == "unknown"


def test_a_screen_name_the_planner_copied_is_no_identity_a_translation_is():
    labels = ["Rückmeldung vorläufig", "Rückmeldung bestätigt", "Rückmeldung Probe"]
    ks = N.goal_constraints("Use the final headcount from the dinner notes.", {"target": "Rückmeldung Probe"},
                            "notes_notes", NOW, "", labels)
    assert not [k for k in ks if k["id"] == "identity"]
    ks2 = N.goal_constraints("Find my note about the blue entrance.", {"target": "die Notiz über den blauen Eingang"},
                             "notes_notes", NOW, "", ["Blauer Eingang", "Hintereingang"])
    assert next(k for k in ks2 if k["id"] == "identity")["words"] == ["blauen", "eingang"]
    ks3 = N.goal_constraints("Delete the note about the broken bike lamp.",
                             {"target": "the broken bike lamp", "target_ui": "die defekte Fahrradlampe"}, "notes_notes", NOW)
    k3 = next(k for k in ks3 if k["id"] == "identity")
    assert "fahrradlampe" in k3["ui_words"] and "lamp" in k3["words"]


def test_the_evidence_goes_into_the_state_within_12000_characters_and_options_are_short_ids():
    rows = [{"label": f"Notiz {i}", "text": f"Notiz {i} | " + "Text " * 200} for i in range(60)]
    fd = fields(rows, "notes_notes")
    st = N.evaluate([], fd)
    text = N.evidence_state("Frontmost application: Notizen (Notes).", [f"C{i + 1}" for i in range(60)], fd, st, [])
    assert len(text) <= N.STATE_CHARS9 and "C60: Notiz 59" in text
    assert N.option_label("C7", fd[6], "id") == "C7" and len(N.option_label("C7", fd[6], "id_name")) <= 60


# ── 2. recovery before the pick ──────────────────────────────────────────────

GOAL_G02 = "Kannst du den Fachbegriff aus meiner Notiz zum undichten Wasserhahn nachschlagen? Nicht die Notiz über den Abfluss."
ROWS_G02 = [{"label": "Wasserhahn Küche", "text": "Wasserhahn Küche | Der Hahn tropft am Griff."},
            {"label": "Abfluss Küche", "text": "Abfluss Küche | Das Wasser läuft langsam ab."},
            {"label": "Wasserhahn Bad", "text": "Wasserhahn Bad | Nichts undicht."}]
STEP_G02 = {"effect": "open_item", "target": "Notiz zum undichten Wasserhahn"}


def test_contradicted_candidates_are_never_offered_none_is_last_and_the_ids_are_permuted():
    o = IdOracle("Wasserhahn Küche")
    r = N.pick9(o, GOAL_G02, STEP_G02, "Frontmost application: Notizen.", ROWS_G02, "notes_notes", NOW, 0.5)
    assert r["index"] == 0 and len(o.asked) == 2
    (s0, _q, opts0), (s1, _q1, opts1) = o.asked
    assert opts0 == ["C1", "C3", N.NONE9] and opts1 == ["C3", "C1", N.NONE9]     # NONE fixed, IDs permuted
    assert "C2" not in [x for x in opts0] and "Dropped (contradicted by the request): C2" in s0
    assert s0.index("C1: ") < s0.index("C3: ") and s1.index("C3: ") < s1.index("C1: ")


def test_every_candidate_contradicted_is_a_hand_off_without_a_model_call():
    o = IdOracle("Abfluss Küche")
    rows = [ROWS_G02[1]]
    r = N.pick9(o, GOAL_G02, STEP_G02, "Notes.", rows, "notes_notes", NOW, 0.0)
    assert r["index"] is None and o.asked == [] and "contradicts" in r["reason"]


def test_a_disagreement_or_none_or_low_confidence_hands_off_no_pairwise_tie_break():
    for o, t2 in ((IdOracle(None, by_perm=["Wasserhahn Küche", "Wasserhahn Bad"]), 0.0),
                  (IdOracle(None, by_perm=["Wasserhahn Küche", "none"]), 0.0),
                  (IdOracle("Wasserhahn Küche", conf=0.3), 0.5)):
        r = N.pick9(o, GOAL_G02, STEP_G02, "Notes.", ROWS_G02, "notes_notes", NOW, t2)
        assert r["index"] is None and len(o.asked) == 2, r["reason"]          # no third (follow-up) ask


def test_unresolved_takes_one_read_only_inspection_and_one_retry():
    goal = "Öffne die Kostenaufstellung mit den Busfahrten."
    step = {"effect": "open_item", "target": "Kostenaufstellung mit den Busfahrten"}
    rows = [{"label": "Kosten_a.txt", "text": "Kosten_a.txt | modified 2026-02-01 09:00", "kind": "file"},
            {"label": "Kosten_b.txt", "text": "Kosten_b.txt | modified 2026-08-15 16:00", "kind": "file"}]
    seen = []

    def inspect(idx, cands):
        seen.append([c["label"] for c in cands])
        return [{"content": "Kostenaufstellung Bahn", "content_known": "full"},
                {"content": "Kostenaufstellung Busfahrten 3x", "content_known": "full"}]
    o = IdOracle(None, by_perm=["Kosten_a", "Kosten_b", "Busfahrten", "Busfahrten"])
    r = N.pick9(o, goal, step, "Finder.", rows, "finder_files", NOW, 0.5, inspect=inspect)
    assert r["index"] == 1 and r["inspected"] and seen == [["Kosten_a.txt", "Kosten_b.txt"]] and len(o.asked) == 4
    # still unresolved after the retry: a hand-off, never a second inspection
    o2 = IdOracle(None, by_perm=["Kosten_a", "Kosten_b"])
    r2 = N.pick9(o2, goal, step, "Finder.", rows, "finder_files", NOW, 0.5, inspect=inspect)
    assert r2["index"] is None and len(seen) == 2 and len(o2.asked) == 4


def test_a_pick_without_positive_evidence_is_unresolved_when_required():
    goal = "Lösch die Notiz mit dem alten Wlan-Passwort."
    step = {"effect": "select_item", "target": "Notiz mit dem alten Wlan-Passwort"}
    rows = [{"label": "Reinigung", "text": "Reinigung | Firma kommt dienstags."},
            {"label": "Büro", "text": "Büro | Schlüssel beim Empfang."}]
    o = IdOracle("Reinigung", conf=0.99)
    assert N.pick9(o, goal, step, "Notes.", rows, "notes_notes", NOW, 0.5)["index"] is None
    assert N.pick9(IdOracle("Reinigung", conf=0.99), goal, step, "Notes.", rows, "notes_notes", NOW, 0.5,
                   require_positive=False)["index"] == 0


# ── the arm: fakes end to end ────────────────────────────────────────────────

SE01 = "Mach mal das neueste Schreiner-Angebot auf und setz den Preis auf 4.850 Euro. Speichern nicht vergessen."


def _se01_guest(**kw):
    tree = {"G/angebot_dachdecker.txt": "Angebot Dachdecker Gartenhaus\nPreis: 1.980 Euro",
            "G/angebot_schreiner_v1.txt": "Angebot Schreinerarbeiten Gartenhaus\nPreis: 5.200 Euro",
            "G/angebot_schreiner_v2.txt": "Angebot Schreinerarbeiten Gartenhaus\nPreis: 5.050 Euro"}
    dates = {f"{ROOT}/G/angebot_dachdecker.txt": ("2026-09-10 11:05", "2026-09-10 11:05"),
             f"{ROOT}/G/angebot_schreiner_v1.txt": ("2026-03-02 09:15", "2026-03-02 09:15"),
             f"{ROOT}/G/angebot_schreiner_v2.txt": ("2026-08-19 14:40", "2026-08-19 14:40")}
    return FakeGuest9(tree, shown="G", dates=dates, now="2026-09-29 12:00", **kw)


SE01_PLAN = (plan4(S("launch_app", "Finder"), S("open_item", "Finder", "das neueste Schreiner-Angebot"),
                   S("read_content", "TextEdit", "document", "text")),
             plan4(S("doc_save", "TextEdit", "", "Preis: 4.850 Euro", mode="edit", find="Preis: 5.050 Euro")))
V2 = f"{ROOT}/G/angebot_schreiner_v2.txt"


def test_se01_like_the_newest_schreiner_offer_is_edited_no_date_veto():
    g = _se01_guest()
    rec = X.Rec("SE01", "arch", 1)
    arch9(g, FakePlanner4(*SE01_PLAN), IdOracle("angebot_schreiner_v2")).run(
        X.Task("SE01", SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2] == "Angebot Schreinerarbeiten Gartenhaus\nPreis: 4.850 Euro", (rec.stopped, rec.steps[-1])
    pick = next(s["pick"] for s in rec.steps if s.get("pick"))
    assert pick["index"] == 2 and "date_check" not in pick and not rec.stopped and rec.errors9 == []
    assert any(k["id"] == "rank" for k in pick["constraints"])


def test_a_concurrent_edit_after_the_read_survives_and_nothing_is_written():
    def concurrent(g, a):
        g.fs[V2] = g.fs[V2] + "\nNotiz: telefonisch bestätigt"
        g.te_docs[V2] = g.fs[V2]
    g = _se01_guest()
    calls = {"n": 0}
    real = g.op_read_text

    def read_then_edit(bundle, what):
        r = real(bundle, what)
        if bundle == TE:
            calls["n"] += 1
            if calls["n"] == 2:                       # right after the read (read twice, the same text)
                concurrent(g, {})
        return r
    g.op_read_text = read_then_edit
    rec = X.Rec("SE01", "arch", 1)
    arch9(g, FakePlanner4(*SE01_PLAN), IdOracle("angebot_schreiner_v2")).run(
        X.Task("SE01", SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2].endswith("Notiz: telefonisch bestätigt") and "5.050" in g.fs[V2]
    assert rec.stopped and "concurrency" in rec.steps[-1]["why"] and ops(g, "enter") == []


def test_a_concurrent_edit_while_the_edit_is_unsaved_is_never_saved_over():
    def concurrent(g, a):
        g.fs[V2] = g.fs[V2] + "\nNotiz: neu"
    g = _se01_guest(before={"enter": concurrent})
    rec = X.Rec("SE01", "arch", 1)
    arch9(g, FakePlanner4(*SE01_PLAN), IdOracle("angebot_schreiner_v2")).run(
        X.Task("SE01", SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2].endswith("Notiz: neu") and "5.050" in g.fs[V2]
    assert rec.stopped and rec.reverted9 and not any(a.get("label", "").endswith("Sichern")
                                                     for a in ops(g, "press"))
    assert rec.errors9 == []                                           # nothing of ours persisted


def test_wrong_focus_before_the_write_is_a_hand_off_and_the_other_document_is_untouched():
    V1 = f"{ROOT}/G/angebot_schreiner_v1.txt"

    def focus(g, a):
        g.te_docs.setdefault(V1, g.fs[V1])
        g.te_front = V1
    g = _se01_guest(before={"field": focus})
    rec = X.Rec("SE01", "arch", 1)
    arch9(g, FakePlanner4(*SE01_PLAN), IdOracle("angebot_schreiner_v2")).run(
        X.Task("SE01", SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V1] == "Angebot Schreinerarbeiten Gartenhaus\nPreis: 5.200 Euro" and "5.050" in g.fs[V2]
    assert rec.stopped and ("focus" in rec.steps[-1]["why"] or "front document" in rec.steps[-1]["why"])


def test_a_save_that_writes_other_bytes_is_restored_from_the_journal_and_recorded_as_an_error():
    g = _se01_guest()
    real = g.op_press

    def torn(bundle, trigger, label):
        r = real(bundle, trigger, label)
        if bundle == TE and g.te_front == V2:
            g.fs[V2] = g.fs[V2][:20]                    # an interrupted save: the file cut short
        return r
    g.op_press = torn
    rec = X.Rec("SE01", "arch", 1)
    arch9(g, FakePlanner4(*SE01_PLAN), IdOracle("angebot_schreiner_v2")).run(
        X.Task("SE01", SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert g.fs[V2] == "Angebot Schreinerarbeiten Gartenhaus\nPreis: 5.050 Euro"      # the preimage again
    assert rec.errors9 and rec.errors9[0]["restored"] and rec.stopped.startswith("refused (write check failed")


def test_a_save_that_did_not_happen_is_a_failure_without_an_error():
    g = _se01_guest()
    real = g.op_press

    def fail(bundle, trigger, label):
        if bundle == TE:
            return {"status": "failed", "detail": "not running"}
        return real(bundle, trigger, label)
    g.op_press = fail
    rec = X.Rec("SE01", "arch", 1)
    arch9(g, FakePlanner4(*SE01_PLAN), IdOracle("angebot_schreiner_v2")).run(
        X.Task("SE01", SE01, "cross-app", {"app": FINDER}, [], {}), rec)
    assert "5.050" in g.fs[V2] and rec.errors9 == [] and rec.stopped


def test_the_users_literal_line_is_kept_whole_se06_like():
    v, how = N.permitted_literal('Er sagte "ja"', 'Switch that off, then add the line Er sagte "ja". at the end of the '
                                                 'open letter and save it.')
    assert v == 'Er sagte "ja".' and how
    assert N.permitted_literal("Buy milk", "Add the line Buy milk. Then save.")[0] == "Buy milk"
    assert N.permitted_literal("{{temp}} Grad", "x")[0] == "{{temp}} Grad"
    g = FakeGuest9({"B/brief.txt": "Sehr geehrte Frau Berger,"}, shown="B", te_front="B/brief.txt")
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"), S("doc_save", "TextEdit", "", 'Er sagte "ja"', mode="append")))
    rec = X.Rec("SE06", "arch", 1)
    goal = 'Add the line Er sagte "ja". at the end of the open letter and save it.'
    arch9(g, p).run(X.Task("SE06", goal, "single-app", {"app": TE}, [], {}), rec)
    assert g.fs[f"{ROOT}/B/brief.txt"] == 'Sehr geehrte Frau Berger,\nEr sagte "ja".', (rec.stopped, rec.steps)


def test_a_map_resolved_row_that_contradicts_the_request_is_rechecked_and_handed_off():
    g = FakeGuest9({"A/Mietvertrag_Garage.txt": "Garage", "A/Mietvertrag.txt": "Wohnung"}, shown="A")
    p = FakePlanner4(plan4(S("launch_app", "Finder"), S("open_item", "Finder", "Mietvertrag Garage")))
    rec = X.Rec("SA10", "arch", 1)
    goal = "Öffne den Mietvertrag Garage – nicht den Mietvertrag Garage, sondern ... Nicht die Garage."
    arch9(g, p, IdOracle("x")).run(X.Task("SA10", goal, "single-app", {"app": FINDER}, [], {}), rec)
    assert rec.stopped and rec.rechecks9 and ops(g, "press") == []


def test_before_a_gate_the_item_shown_must_be_the_one_picked():
    g = FakeGuest9(notes={"Werkstatt": [("Lampe vorne", "Lampe vorne\nLampe vorne defekt"),
                                         ("Lampe hinten", "Lampe hinten\nRücklicht funktioniert")]})
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("select_item", "Notes", "die defekte Lampe"),
                           S("human_gate", "Notes", "the note", "delete")))
    a = arch9(g, p, IdOracle("Lampe vorne"))
    real = g.op_rows

    n = {"after_select": 0}

    def moved(bundle, kind, back=None):
        if kind == "notes_notes" and len(ops(g, "select")) and not back:
            n["after_select"] += 1
            if n["after_select"] == 2:                # after the select's read-back: the selection moved
                g.shown_note = "Lampe hinten"
        return real(bundle, kind, back)
    rec = X.Rec("SE04", "arch", 1)
    g.op_rows = moved
    a.run(X.Task("SE04", "Lösch die Notiz über die defekte Lampe.", "gate", {"app": NOTES}, [], {}), rec)
    assert rec.gate_stops == 0 and rec.stopped and any("before the gate" in (r.get("why") or "") for r in rec.rechecks9)


# ── 5. planner checks ─────────────────────────────────────────────────────────

def test_an_anchor_not_in_the_observed_text_asks_the_planner_once_with_that_text_ge09_like():
    doc = "L/Lieferliste.txt"
    g = FakeGuest9({doc: "Volle Kisten: 8\nLeere Kisten: 12\nKontakt: Mira"}, shown="L", te_front=doc)
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"), S("doc_save", "TextEdit", "", "Rückgabe: 08.10.2026",
                                                          mode="insert", find="leeren Kisten")),
                     plan4(S("doc_save", "TextEdit", "", "Rückgabe: 08.10.2026", mode="insert", find="Leere Kisten")))
    rec = X.Rec("GE09", "arch", 1)
    arch9(g, p).run(X.Task("GE09", "Pack hinter die Zeile für die leeren Kisten den Rückgabetermin.", "single-app",
                           {"app": TE}, [], {}), rec)
    assert g.fs[f"{ROOT}/{doc}"] == "Volle Kisten: 8\nLeere Kisten: 12\nRückgabe: 08.10.2026\nKontakt: Mira", rec.steps
    assert p.calls == 2 and "Leere Kisten: 12" in p.histories[1][-1]["content"]
    assert N.anchor_problems([S("doc_save", "TextEdit", "", "x", mode="edit", find="leeren")],
                             {"text": "Leere Kisten"})


def test_a_write_task_whose_plan_ends_after_reads_is_asked_once_more_ge06_like():
    doc = "O/Bestellung.txt"
    g = FakeGuest9({doc: "Herbstessen\nVegetarisch: 12"}, shown="O", te_front=doc,
                   notes={"Zusagen": [("Rückmeldung bestätigt", "Rückmeldung bestätigt\nvegetarisch 18")]})
    g.shown_note = "Rückmeldung bestätigt"
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("read_content", "Notes", "note", "note")),
                     plan4(S("launch_app", "TextEdit"), S("read_content", "TextEdit", "document", "doc")),
                     plan4(S("extract", "none", "the count", "count", find="18")),
                     plan4(S("extract", "none", "the count", "count", find="18"),
                           S("doc_save", "TextEdit", "", "Vegetarisch: {{count}}", mode="edit", find="Vegetarisch: 12")))
    rec = X.Rec("GE06", "arch", 1)
    arch9(g, p).run(X.Task("GE06", "Use the final headcount from the note Rückmeldung bestätigt to fix the vegetarian "
                                   "count in the open order.", "cross-app", {"app": NOTES}, [], {}), rec)
    assert g.fs[f"{ROOT}/{doc}"] == "Herbstessen\nVegetarisch: 18", (rec.stopped, [s.get("why") for s in rec.steps])
    assert rec.incomplete_replans9 == 1 and p.calls == 4
    assert N.ends_without_write([S("read_content", "TextEdit", "document", "t"), S("extract", "none", "x", "y")], [],
                                False)
    assert not N.ends_without_write([S("read_content", "TextEdit", "document", "t")], [], True)


def test_a_write_task_gets_one_continuation_beyond_the_cap_and_a_plan_ends_at_its_last_read_ge09_like():
    doc = "L/Lieferliste.txt"
    g = FakeGuest9({doc: "Volle Kisten: 8\nLeere Kisten: 12\nKontakt: Mira"}, shown="L", te_front=doc,
                   notes={"Mosterei": [("Termin bestätigt", "Termin bestätigt\nRückgabe: 08.10.2026, 09:30")]})
    g.shown_note = "Termin bestätigt"
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("read_content", "Notes", "note", "note")),
                     # steps after a read, no write: the plan ends at the read, the extract waits for the text
                     plan4(S("extract", "none", "the return", "ret", find="Rückgabe: 08.10.2026, 09:30"),
                           S("read_content", "TextEdit", "document", "doc")),
                     plan4(S("read_content", "TextEdit", "document", "doc2")),
                     # the continuation beyond Arch4's cap of two (a write task, no write yet)
                     plan4(S("extract", "none", "the return", "ret", find="Rückgabe: 08.10.2026, 09:30"),
                           S("doc_save", "TextEdit", "", "{{ret}}", mode="insert", find="Leere Kisten: 12")))
    rec = X.Rec("GE09", "arch", 1)
    arch9(g, p).run(X.Task("GE09", "Pack hinter die Zeile für die leeren Kisten den Rückgabetermin aus der Notiz "
                                   "Termin bestätigt.", "cross-app", {"app": NOTES}, [], {}), rec)
    assert g.fs[f"{ROOT}/{doc}"] == "Volle Kisten: 8\nLeere Kisten: 12\nRückgabe: 08.10.2026, 09:30\nKontakt: Mira", \
        (rec.stopped, [s.get("why") for s in rec.steps])
    assert rec.extra_continuations9 == 1 and p.calls == 4
    # the plan check's reordering (a read moved before an extract) never ends a write task's plan after the read
    cut = rec.plan[1]["refined"]["steps"]
    assert cut[-1]["effect"] == "read_content" or rec.plan[1]["checks9"]["truncated_at_read"]

# ── judge, the frozen configuration, the guest ───────────────────────────────

def test_judge_is_judge_plus_the_restored_error_and_names_the_post_prior_predicates():
    assert N.judge_from({"success": True, "wrong_actions": 0}, {"errors9": []})["success"] is True
    j = N.judge_from({"success": True, "wrong_actions": 0}, {"errors9": [{"kind": "restored_write", "why": "x"}]})
    assert j["success"] is False and j["wrong_actions"] == 1 and j["restored_errors"] == 1
    assert set(N.ADDED_PREDICATES) == {"opened_set", "notes_shown_ok", "restored_error"}


def test_the_configuration_must_be_frozen_before_the_arm_can_run():
    try:
        N.config_of9("openjev", {"pickers": {}})
        raise AssertionError("an unfrozen configuration must not run")
    except KeyError:
        pass
    assert N.config_of9("openjev", {"pickers": {"openjev": {"t2": 0.6, "format": "id", "positive": False}}}) == \
        {"t2": 0.6, "format": "id", "positive": False}


def test_the_guest_ops_restore_only_this_tests_files_by_compare_and_swap_and_faults_are_a_closed_set():
    src = open(os.path.join(HERE, "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index("def op_restore_file"):src.index("FAULT_KINDS = ")]
    assert "if not ours(p)" in body and 'cur.get("sha256") != a.get("expect_sha256")' in body
    fb = src[src.index("def op_fault"):src.index("def op_item_dates(")]
    assert 'FAULT_KINDS = ("edit_file", "truncate_file", "open_doc", "kill_app")' in src
    assert "if k not in FAULT_KINDS" in fb and "need_managed(bid)" in fb and "subprocess" not in fb
    assert '"restore_file": op_restore_file, "fault": op_fault' in src


# ── the Arch9 host: arms, the balanced pair order, the fault channel and its audit ──

def _host9():
    import importlib
    import sys
    here = os.path.join(os.path.dirname(os.path.abspath(X.__file__)), "..", "execution", "guest")
    sys.path.insert(0, here)
    return importlib.import_module("desktop_e2e9_host")


def test_decide9_replays_the_arms_rule_from_recorded_answers():
    def ans(cid0, cid1, c=0.9):
        return [{"id": cid0, "index": 0 if cid0 else None, "none": cid0 is None, "confidence": c},
                {"id": cid1, "index": 0 if cid1 else None, "none": cid1 is None, "confidence": c}]
    log = {"attempts": [{"answers": ans("C1", "C1", 0.6), "positive": {"C1": False}},
                        {"answers": ans("C2", "C2", 0.9), "positive": {"C2": True}}]}
    assert N.decide9(log, 0.5, False) == "C1"          # the first attempt is resolved: no retry
    assert N.decide9(log, 0.5, True) == "C2"           # no positive evidence: the retry after the inspection
    assert N.decide9(log, 0.95, True) is None
    assert N.decide9({"attempts": [{"dropped": [("C1", "x")]}]}, 0.0, False) is None

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
