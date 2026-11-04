"""Arch8, offline, with a FAKE
guest and fake models (the Arch4-7 fakes, extended).

What is pinned here, one block per fix:
1. the picker's hand-offs: (a) richer option descriptions (the kind in a mixed
   list, the parent where rows differ, the absolute date AND its period, a
   content snippet, a folder's contents) within 120 characters; (b) the
   disagreement protocol: a pairwise follow-up (A vs B + none; A vs none) in both
   orders, acted on only when consistent; the configuration frozen before the arm;
2. a disabled menu item with a declared precondition: the state left, the press
   retried once, the item shown before shown again;
3. edit scopes ("change only the room on the … line") are not missing
   restrictions; the saved text may change only the scope's lines;
4. duplicate-named AX elements told apart by their window;
plus judge (``opened_set``) and the Arch8 host.

No desktop, no AX, no subprocess, no network.
"""
import datetime as dt
import json
import os

from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e2 as Y
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e7 as R7
from anyuse.execution import desktop_e2e8 as R

import test_desktop_e2e4 as T4
import test_desktop_e2e5 as T5
import test_desktop_e2e6 as T6

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, NOTES, TE = T4.FINDER, T4.NOTES, T4.TE
ROOT = T4.ROOT
NOW = dt.datetime(2026, 9, 29, 5, 10)


def arch8(guest, planner, chooser=None, spend=None, threshold=0.0, protocol=None, **kw):
    a = R.Arch8(guest, EMAP, planner=planner, chooser=chooser, spend=spend, inbox=None, sleep=lambda s: None,
                threshold=threshold, **kw)
    if protocol:
        a.protocol = protocol
    return a


def ops(g, name):
    return [a for op, a in g.log if op == name]


# ── 1a. richer option descriptions ───────────────────────────────────────────

def test_the_period_of_a_date_in_a_users_words():
    p = R.period
    assert p(dt.date(2026, 9, 29), NOW) == "today" and p(dt.date(2026, 9, 28), NOW) == "yesterday"
    assert p(dt.date(2026, 9, 25), NOW) == "4 days ago" and p(dt.date(2026, 9, 10), NOW) == "this month"
    assert p(dt.date(2026, 8, 27), NOW) == "last month" and p(dt.date(2026, 3, 1), NOW) == "this year"
    assert p(dt.date(2025, 8, 27), NOW) == "last year" and p(dt.date(2023, 1, 1), NOW) == "3 years ago"
    assert p(dt.date(2026, 12, 20), dt.datetime(2027, 1, 5)) == "last month"
    assert p(dt.date(2026, 10, 1), NOW) == "" and p(dt.date(2026, 9, 1), None) == ""


GD08_ROWS = ["Minutes August 2025.txt | modified 2025-08-27 15:00, created 2026-09-29 | 60 Byte | Reiner Text | "
             "content: Volunteer training: Room C Book sale: Hall",
             "Minutes August 2026.txt | 27.08.2026, 15:00 | 80 Byte | Reiner Text | content: Volunteer training: "
             "Room A Book sale: Room A Next meeting: 4 September",
             "Minutes September 2026.txt | modified 2026-09-23 15:00 | content: Volunteer training: Room A"]


def test_rich_options_carry_the_period_the_kind_in_a_mixed_list_and_stay_within_120_characters():
    o = R.rich_options(GD08_ROWS, NOW)
    assert o[0].startswith("Minutes August 2025.txt | modified 2025-08-27 15:00 (last year) | content: Volunteer")
    assert "(last month)" in o[1] and "(6 days ago)" in o[2] and all(len(x) <= 120 for x in o)
    assert "Byte" not in " ".join(o) and "Reiner Text" not in " ".join(o) and "created" not in " ".join(o)
    mixed = R.rich_options(GD08_ROWS + ["Archiv | modified 2026-09-01 10:00 | -- | Ordner"], NOW)
    assert " | file | " in mixed[0] and mixed[3] == "Archiv | folder | modified 2026-09-01 10:00 (this month)"
    # a long content is cut last, the name and the date never
    long = R.fit_option8("Leihprotokoll August 2026.txt | modified 2026-08-29 17:00 | content: " + "Stühle " * 40, NOW)
    assert len(long) == 120 and long.startswith("Leihprotokoll August 2026.txt | modified 2026-08-29 17:00 (last month)")


def test_the_parent_is_named_only_where_the_rows_differ_and_folders_show_their_contents():
    rows = ["Umzug | Kartons bestellen | in Privat", "Umzug | Transporter mieten | in Verein"]
    o = R.rich_options(rows, NOW, parents=["Privat", "Verein"])
    assert o[0] == "Umzug | parent: Privat | Kartons bestellen" and o[1].startswith("Umzug | parent: Verein")
    same = R.rich_options(rows, NOW, parents=["Privat", "Privat"])
    assert "parent:" not in " ".join(same)
    nav = R.rich_options(["Chorproben Herbst (the folder shown now)", "September (subfolder of the folder shown)",
                          "Dokumente (sidebar)"], NOW,
                         contains=[["September", "Probe alt.txt"], ["Probe Fassung A.txt", "Probe Fassung B.txt"], None])
    assert nav[1] == "September (subfolder of the folder shown) | contains: Probe Fassung A.txt, Probe Fassung B.txt"
    assert nav[2] == "Dokumente (sidebar)"
    # a note's first line that starts like a place is not a parent
    assert R.parse_option("Tor | In der roten Dose")["parent"] == ""


def test_differences_name_what_tells_two_candidates_apart():
    o = R.rich_options(GD08_ROWS, NOW)
    d = R.differences(o[0], o[1])
    assert any(x.startswith("name: A has '2025.txt', B has '2026.txt'") for x in d)
    assert any("last year" in x and "last month" in x for x in d if x.startswith("modified"))
    assert R.differences("Tor | Schlüssel liegt in der roten Dose", "Tor | Schlüssel liegt in der roten Dose") == []


# ── 1b. the disagreement protocol ────────────────────────────────────────────

class Oracle:
    """A fake OpenJev: the first round splits by order (o0 -> ``first0``, o1 -> ``first1``, by option text);
    the follow-up answers ``follow`` (a substring, or "none") in both orders."""

    def __init__(self, first0, first1, follow, conf=0.9, follow1=None):
        self.first0, self.first1, self.follow, self.follow1, self.conf = first0, first1, follow, follow1, conf
        self.asked = []

    def __call__(self, state, q, options):
        self.asked.append((state, q, list(options)))
        fu = "A follow-up" in state
        rev = options[0] == R7.NONE_OPTION
        want = (self.follow1 if (fu and rev and self.follow1) else self.follow) if fu else \
            (self.first1 if rev else self.first0)
        if want == "none":
            return options.index(R7.NONE_OPTION), self.conf, {"cost": 0.0}
        return next(i for i, o in enumerate(options) if want in o), self.conf, {"cost": 0.0}


OPTS = R.rich_options(GD08_ROWS, NOW)
GOAL8 = "In the minutes from last month, change only the room on the volunteer-training line to Room B."


def test_agreement_is_accepted_without_a_follow_up():
    o = Oracle("August 2026", "August 2026", "August 2025")
    p = R.consistent_pick8(o, GOAL8, "state", OPTS, 0.0, protocol="pair+verify")
    assert p.index == 1 and p.calls == 2 and len(o.asked) == 2


def test_a_disagreement_asks_the_two_chosen_candidates_again_in_both_orders():
    o = Oracle("August 2025", "August 2026", "August 2026")
    p = R.consistent_pick8(o, GOAL8, "Frontmost application: Finder.", OPTS, 0.0, protocol="pair")
    assert p.index == 1 and p.calls == 4 and "pair follow-up" in p.reason
    (s2, q2, l2), (s3, q3, l3) = o.asked[2:]
    assert l2 == [OPTS[0], OPTS[1], R7.NONE_OPTION] and l3 == l2[::-1]
    assert "They differ in: name: A has '2025.txt', B has '2026.txt'" in s2 and q2 == o.asked[0][1]
    assert R7.NONE_HINT.strip() in q2          # the question wording stays the caller's; screen text only in the state
    assert [a["round"] for a in p.answers] == ["first", "first", "pair", "pair"]
    assert p.answers[2]["sub"] == [0, 1]


def test_an_inconsistent_or_none_follow_up_hands_off_as_before():
    o = Oracle("August 2025", "August 2026", "August 2026", follow1="August 2025")
    p = R.consistent_pick8(o, GOAL8, "s", OPTS, 0.0, protocol="pair+verify")
    assert p.index is None and "orders disagree" in p.reason and "pair follow-up" in p.reason
    o = Oracle("August 2025", "August 2026", "none")
    assert R.consistent_pick8(o, GOAL8, "s", OPTS, 0.0, protocol="pair").index is None
    # below t2 in the follow-up: a hand-off
    o = Oracle("August 2025", "August 2026", "August 2026", conf=0.5)
    assert R.consistent_pick8(o, GOAL8, "s", OPTS, 0.6, protocol="pair").index is None


def test_none_in_one_order_is_verified_with_the_other_orders_item_only_when_the_protocol_says_so():
    o = Oracle("none", "Teilnehmer 2025", "Teilnehmer 2025")
    opts = ["Helfer 2025.txt | modified 2025-09-21 18:00 (last year)",
            "Teilnehmer 2025.txt | modified 2025-09-20 18:00 (last year)",
            "Teilnehmer 2026.txt | modified 2026-09-20 18:00 (9 days ago)"]
    p = R.consistent_pick8(o, "Die alte Teilnehmerdatei vom letzten Jahr", "s", opts, 0.0, protocol="pair+verify")
    assert p.index == 1 and "verify follow-up" in p.reason
    assert o.asked[2][2] == [opts[1], R7.NONE_OPTION] and o.asked[3][2] == [R7.NONE_OPTION, opts[1]]
    assert "A follow-up" in o.asked[2][0]
    o = Oracle("none", "Teilnehmer 2025", "Teilnehmer 2025")
    p = R.consistent_pick8(o, "x", "s", opts, 0.0, protocol="pair")
    assert p.index is None and p.calls == 2 and p.reason.startswith("none of these in one order")
    o = Oracle("none", "Teilnehmer 2025", "none")
    assert R.consistent_pick8(o, "x", "s", opts, 0.0, protocol="pair+verify").index is None


def test_protocol_off_is_prior_rule_and_both_none_or_errors_get_no_follow_up():
    o = Oracle("August 2025", "August 2026", "August 2026")
    p = R.consistent_pick8(o, GOAL8, "s", OPTS, 0.0, protocol="off")
    assert p.index is None and p.calls == 2 and p.reason.startswith("orders disagree")
    o = Oracle("none", "none", "August 2026")
    p = R.consistent_pick8(o, GOAL8, "s", OPTS, 0.0, protocol="pair+verify")
    assert p.index is None and p.calls == 2 and p.reason.startswith("none of these (both orders")

    def broken(state, q, options):
        return None, 0.0, {"error": "timeout"}
    p = R.consistent_pick8(broken, GOAL8, "s", OPTS, 0.0, protocol="pair+verify")
    assert p.index is None and p.calls == 2


def test_the_configuration_must_be_frozen_before_the_arm_can_run():
    spec = R.load_picker8()
    assert spec["pickers"]["openjev"]["t2"] is None or spec["pickers"]["openjev"]["protocol"] in R.PROTOCOLS
    try:
        R.config_of8("openjev", {"pickers": {"openjev": {"t2": None, "protocol": None}}})
        assert False, "no configuration must mean no arm"
    except KeyError:
        pass
    assert R.config_of8("openjev", {"pickers": {"openjev": {"t2": 0.2, "protocol": "pair"}}}) == (0.2, "pair")
    a = R.Arch8(T6.FakeGuest6(), EMAP, planner=None, chooser=lambda *x: (0, 1.0, {}), inbox=None,
                picker_name="openjev", config_override=(0.3, "pair+verify"))
    assert a.threshold == 0.3 and a.protocol == "pair+verify"


def _gd08_guest():
    tree = {"Library/Minutes August 2025.txt": "Volunteer training: Room C\nBook sale: Hall",
            "Library/Minutes August 2026.txt": "Volunteer training: Room A\nBook sale: Room A\nNext meeting: 4 September",
            "Library/Minutes September 2026.txt": "Volunteer training: Room A\nBook sale: Hall"}
    dates = {f"{ROOT}/Library/Minutes August 2025.txt": ("2025-08-27 15:00", "2026-09-29 01:00"),
             f"{ROOT}/Library/Minutes August 2026.txt": ("2026-08-27 15:00", "2026-09-29 01:00"),
             f"{ROOT}/Library/Minutes September 2026.txt": ("2026-09-23 15:00", "2026-09-29 01:00")}
    return T6.FakeGuest6(tree, shown="Library", dates=dates, now="2026-09-29 05:10")


GD08 = ("In the minutes from last month, change only the room on the volunteer-training line to Room B. "
        "Open the right minutes first; the newer September meeting is not it.")


def test_a_gd08_like_run_opens_last_months_file_after_the_pair_follow_up_and_edits_one_line():
    g = _gd08_guest()
    o = Oracle("August 2025", "August 2026", "August 2026")
    p = FakePlanner4(plan4(S("launch_app", "Finder"), S("open_item", "Finder", "the minutes from last month"),
                           S("read_content", "TextEdit", "document", "text")),
                     plan4(S("doc_save", "TextEdit", "", "Volunteer training: Room B", mode="edit",
                             find="Volunteer training: Room A")))
    a = arch8(g, p, o, protocol="pair+verify")
    rec = X.Rec("GD08", "arch", 1)
    a.run(X.Task("GD08", GD08, "cross-app", {"app": FINDER}, [], {}), rec)
    want = "Volunteer training: Room B\nBook sale: Room A\nNext meeting: 4 September"
    assert g.fs[f"{ROOT}/Library/Minutes August 2026.txt"] == want, (rec.stopped, rec.steps)
    pick = next(s["pick"] for s in rec.steps if s.get("pick"))
    assert pick["protocol"] == "pair+verify" and "pair follow-up" in pick["reason"]
    assert any("(last month)" in x for x in pick["options"])
    # the edit scope was not fed back as a missing restriction (GD08 under Arch7)
    assert rec.plan[0]["missing_restrictions"] == [] and rec.plan[0]["edit_scopes"]
    assert p.calls == 2 and not rec.stopped


# ── 2. a disabled item with a declared precondition ──────────────────────────

VIEW = next(e for e in EMAP.entries("Notes", "toggle_select") if e.get("group") == "Darstellung")
LIST_KEY = next(o["key"] for o in VIEW["options"] if o["label"] == "Als Liste")
GAL_KEY = next(o["key"] for o in VIEW["options"] if o["label"] == "Als Galerie")


class GalleryGuest(T6.FakeGuest6):
    """Notes in the gallery view: a note opened from the gallery hides the grid, and Notes disables the view
    switch until the engine is back on the grid (GD05 under Arch7)."""

    def __init__(self, *a, declared=True, **kw):
        super().__init__(*a, **kw)
        self.view, self.item_open, self.declared, self.restores = GAL_KEY, False, declared, 0

    def op_select(self, bundle, kind, label, via=None):
        r = super().op_select(bundle, kind, label, via)
        if kind == "notes_notes":
            self.item_open = self.view == GAL_KEY
        return r

    def op_marks(self, bundle, parent, options):
        if bundle == NOTES:
            keys = [o["key"] for o in options]
            return {"status": "done", "ok": True, "target": {"options": keys, "selected": [self.view]}, "sentinel": []}
        return super().op_marks(bundle, parent, options)

    def op_press(self, bundle, trigger, label):
        if bundle == NOTES and trigger.get("identifier") in (LIST_KEY[3:], GAL_KEY[3:]):
            if self.item_open:
                return {"status": "failed", "detail": f"{label.split(' > ')[-1]!r} is disabled"}
            self.view = LIST_KEY if trigger["identifier"] == LIST_KEY[3:] else GAL_KEY
            if self.view == LIST_KEY:
                self.shown_note = None            # worst case: the list view shows no note until one is selected
            return {"status": "done"}
        return super().op_press(bundle, trigger, label)

    def op_precondition(self, bundle):
        if bundle == NOTES and self.item_open and self.declared:
            self.item_open, self.restores = False, self.restores + 1
            return {"status": "done", "restored": "gallery_item_open", "shown_before": self.shown_note,
                    "reshow": "notes_notes"}
        return {"status": "unchanged", "restored": None, "detail": "no declared state found"}


TOR = Oracle("Tor |", "Tor |", "Tor |")
GD05 = ("Mach mal die Notiz mit dem Schlüssel fürs Seitentor auf und stell die Notizen danach als Liste dar. "
        "Nicht die fürs Haupttor.")


def _gd05(declared=True):
    g = GalleryGuest(notes={"Schlüssel": [("Tor", "Tor\nSchlüssel fürs Seitentor liegt in der roten Dose."),
                                          ("Haupttor", "Haupttor\nSchlüssel fürs Haupttor hängt am Brett.")]},
                     declared=declared)
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("navigate", "Notes", "Schlüssel"), S("open_item", "Notes", "Tor"),
                           S("toggle_select", "Notes", "Darstellung", "Als Liste")))
    return g, p


def test_a_view_switch_disabled_while_a_gallery_note_is_open_is_retried_once_on_the_grid_gd05_like():
    g, p = _gd05()
    rec = X.Rec("GD05", "arch", 1)
    arch8(g, p, TOR).run(X.Task("GD05", GD05, "single-app", {"app": NOTES}, [], {}), rec)
    assert not rec.stopped, (rec.stopped, rec.steps[-1])
    assert g.view == LIST_KEY and g.shown_note == "Tor" and g.restores == 1
    last = rec.steps[-1]
    assert last["precondition"]["restored"] == "gallery_item_open" and last["precondition"]["retry"] == "done"
    assert last["precondition"]["reshown"] == {"label": "Tor", "status": "done"}
    assert [a["op"] for a in last["actions"]] == ["press", "precondition", "press", "select"]
    assert rec.precondition_retries == 1


def test_without_a_declared_state_the_disabled_item_fails_as_before_and_is_never_retried_twice():
    g, p = _gd05(declared=False)
    rec = X.Rec("GD05", "arch", 1)
    arch8(g, p, TOR).run(X.Task("GD05", GD05, "single-app", {"app": NOTES}, [], {}), rec)
    assert rec.stopped.startswith("failed step") and g.view == GAL_KEY
    assert len(ops(g, "precondition")) == 2          # once per attempt (the run's one re-plan), never a second retry
    assert all(len([a for a in s.get("actions") or [] if a["op"] == "press"]) == 1 for s in rec.steps
               if (s.get("step") or {}).get("effect") == "toggle_select")


def test_the_compiled_replay_retries_the_disabled_item_too_with_no_model_call():
    g, p = _gd05()
    rec = X.Rec("GD05", "arch", 1)
    arch8(g, p, TOR).run(X.Task("GD05", GD05, "single-app", {"app": NOTES}, [], {}), rec)
    g2, _ = _gd05()
    rec2 = X.Rec("GD05", "arch", 1, mode="compiled")
    R.Arch8(g2, EMAP, planner=None, chooser=None, inbox=None, sleep=lambda s: None).run(
        X.Task("GD05", GD05, "single-app", {"app": NOTES}, [], {}), rec2, compiled=rec.compiled)
    assert not rec2.stopped and g2.view == LIST_KEY and g2.shown_note == "Tor" and sum(rec2.calls.values()) == 0


def test_the_declared_preconditions_are_generic():
    assert set(R.PRECONDITIONS) == {NOTES, FINDER, TE}
    assert R.is_disabled({"status": "failed", "detail": "'Als Liste' is disabled"})
    assert not R.is_disabled({"status": "failed", "detail": "no element"}) and not R.is_disabled(None)


# ── 3. edit scopes ────────────────────────────────────────────────────────────

def test_change_only_x_on_the_y_line_is_an_edit_scope_not_a_missing_restriction():
    sc = R.edit_scopes(GD08)
    assert [s["key"] for s in sc] == [["volunteer", "training"]]
    plan = [S("launch_app", "Finder"), S("open_item", "Finder", "minutes from last month"),
            S("read_content", "TextEdit", "document", "text")]
    assert Y.missing_restrictions(GD08, plan) and R.missing_restrictions8(GD08, plan) == []
    assert R.edit_scopes("Ändere nur in der Zeile mit dem Treffpunkt die Zahl auf 5.")
    # places and sources stay restrictions
    for g in ("Search only in the Oxford dictionary for 'Haus'.", "Öffne die Einkaufsliste, aber nur aus dem Ordner Privat.",
              # G05: the restriction names the DOCUMENT to edit (a place), not the part changed
              "Mach bitte nur im Entwurf für den Elternabend aus „Beginn: 19 Uhr“ ein „Beginn: 18 Uhr“ und sicher ihn.",
              "Sortier nur im Ordner Projekte nach Datum.", "change only files in the Archive folder"):
        assert R.edit_scopes(g) == [], g
        assert Y.restrictions(g) == [] or R.missing_restrictions8(g, []) == Y.missing_restrictions(g, []), g


def test_a_save_that_changes_a_line_outside_the_scope_is_restored():
    old = "Volunteer training: Room A\nBook sale: Room A"
    sc = R.edit_scopes(GD08)
    assert R.edit_scope_problems(sc, old, "Volunteer training: Room B\nBook sale: Room A") == []
    assert R.edit_scope_problems(sc, old, "Volunteer training: Room A\nBook sale: Room B")
    assert R.edit_scope_problems([], old, "x") == []
    g = T6.FakeGuest6(tree={"Library/Minutes August 2026.txt": old}, shown="Library",
                      te_front="Library/Minutes August 2026.txt")
    g.front = TE
    p = FakePlanner4(plan4(S("doc_save", "TextEdit", "", "Book sale: Room B", mode="edit", find="Book sale: Room A")))
    rec = X.Rec("E1", "arch", 1)
    arch8(g, p).run(X.Task("E1", GD08, "cross-app", {"app": TE}, [], {}), rec)
    assert g.fs[f"{ROOT}/Library/Minutes August 2026.txt"] == old and rec.restores == 1, (rec.stopped, rec.steps)
    assert rec.steps[-1]["why"].startswith("edit scope check")


# ── 4. duplicate-named elements by window ────────────────────────────────────

def test_same_named_elements_are_told_apart_by_the_named_document_window_or_the_front_window():
    c = [{"window": "Leihprotokoll September 2026.txt", "document": "/g/Leih/Leihprotokoll September 2026.txt",
          "front": False},
         {"window": "Leihprotokoll August 2026.txt", "document": "/g/Leih/Leihprotokoll August 2026.txt", "front": True}]
    assert R.choose_by_window(c)[0] == 1
    assert R.choose_by_window(c, document="Leihprotokoll September 2026.txt")[0] == 0
    assert R.choose_by_window(c, document="/g/Leih/Leihprotokoll August 2026.txt")[0] == 1
    assert R.choose_by_window(c, window="Leihprotokoll September 2026.txt")[0] == 0
    assert R.choose_by_window([dict(x, front=False) for x in c])[0] is None      # never guessed
    assert R.choose_by_window([dict(x, front=True) for x in c])[0] is None
    assert R.choose_by_window(c, document="other.txt")[0] is None


def test_the_guest_ax_step_scopes_duplicates_by_window_and_never_guesses():
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index("def op_ax_step(a):"):src.index("# ── the baseline's view")]
    assert "k, how = _by_window(pid, els, cmd)" in body and "if len(one) == 1:" in body
    assert "ax_tree=tree if scoped else AccessibilityTree(pid=pid)" in body
    assert "(no recorded identity: refused, never guessed)" in body
    assert "R8.choose_by_window(info, cmd.get(\"document\"), cmd.get(\"window\"))" in body
    pre = src[src.index("def op_precondition(a):"):src.index("OPS = {")]
    assert "notes_searching(pid)" in pre and "_gallery_back(pid)" in pre and "_ql_close(pid)" in pre
    assert '"Abbrechen"' in pre and "guard_before(pid" in pre and "guard_after(pid" in pre
    assert '"precondition": op_precondition' in src


# ── judge ────────────────────────────────────────────────────────────────────

def _opened_task(want):
    return X.Task("J8", "x", "cross-app", {"app": TE}, [{"kind": "front"}], {"opened_set": want})


def test_judge_reads_every_document_the_author_wants_open():
    a, s = ROOT + "/Leih/August.txt", ROOT + "/Leih/September.txt"
    t = _opened_task([a, s])
    base = {"success": True, "items": [], "unexpected": ["document opened: August.txt"], "irreversible": [],
            "wrong_actions": 1}
    j = R.judge_from(base, t, [{"ok": True, "documents": [s]}], [{"ok": True, "documents": [s, a]}])
    assert j["success"] and j["unexpected"] == [] and j["items"][-1]["status"] == "ok" and j["judge"] == "judge_open"
    j = R.judge_from(dict(base, unexpected=[]), t, [{"ok": True, "documents": [s]}], [{"ok": True, "documents": [s]}])
    assert not j["success"] and j["items"][-1]["status"] == "missing"
    j = R.judge_from(dict(base, unexpected=[]), t, [{"ok": True, "documents": [s]}], [{"ok": True, "documents": [a]}])
    assert j["items"][-1]["status"] == "wrong" and j["wrong_actions"] == 1
    plain = X.Task("J9", "x", "cross-app", {"app": TE}, [{"kind": "front"}], {})
    assert R.judge_from(base, plain, [], []) is base


def test_judge_lets_a_note_the_task_reads_stay_shown_and_nothing_else():
    t = X.Task("J10", "x", "cross-app", {"app": NOTES}, [{"kind": "notes"}],
               {"notes_shown_ok": [{"folder": "Rezepte Familie", "note": "Apfelkuchen Oma"}]})
    base = {"success": False, "items": [{"what": "x", "status": "ok"}],
            "unexpected": ["shown note changed: 'Mürbeteig' -> 'Apfelkuchen Oma'"], "irreversible": [], "wrong_actions": 1}
    j = R.judge_from(base, t, [{"ok": True, "folder": "Rezepte Familie", "shown_note": "Mürbeteig"}],
                      [{"ok": True, "folder": "Rezepte Familie", "shown_note": "Apfelkuchen Oma"}])
    assert j["success"] and j["unexpected"] == [] and j["wrong_actions"] == 0 and "allowed_shown" in j
    other = dict(base, unexpected=["shown note changed: 'Mürbeteig' -> 'Käsekuchen Oma'"])
    j = R.judge_from(other, t, [{"ok": True, "folder": "Rezepte Familie", "shown_note": "Mürbeteig"}],
                      [{"ok": True, "folder": "Rezepte Familie", "shown_note": "Käsekuchen Oma"}])
    assert not j["success"] and j["wrong_actions"] == 1


# ── the Arch8 host ────────────────────────────────────────────────────────────

def _host8():
    import importlib
    import sys
    here = os.path.join(os.path.dirname(os.path.abspath(X.__file__)), "..", "execution", "guest")
    sys.path.insert(0, here)
    return importlib.import_module("desktop_e2e8_host")


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
