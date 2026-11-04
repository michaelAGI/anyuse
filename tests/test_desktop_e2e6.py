"""Arch6, offline, with a FAKE
guest and fake models (Arch5's fakes, extended).

What is pinned here, one block per fix:
1. find means show: a goal to find / locate / look up / show an item ends
   with the item OPEN (a planner rule and a deterministic post-plan check);
2. judge: the note shown and its content; the folder item reads "the
   declared folder or the search scope while a search is active";
3. settings_set: an app's settings window (tab, checkbox / choice / pop-up by
   its label), verified and closed; not TextEdit-specific;
4. the language bridge: the UI language from the screen summary; targets in
   the user's words AND the UI language; the picker question gives both;
   candidates keep their on-screen text, fitted to OpenJev's 120 characters;
5. t2 recalibrated on a larger development set, frozen.

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

import test_desktop_e2e as T3
import test_desktop_e2e4 as T4
import test_desktop_e2e5 as T5

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
FINDER, NOTES, TE, DICT = T4.FINDER, T4.NOTES, T4.TE, T4.DICT
ROOT = T4.ROOT
NOTES_FIND = next(e for e in EMAP.entries("Notes", "scoped_find"))
SCOPE_ALL = "Alle auf meinem Mac"


class FakeGuest6(T5.FakeGuest5):
    """``FakeGuest5`` plus Notes' search (the list shows the matching notes
    of every folder; the sidebar then reads the search scope, as Notes does)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.notes_search = ""
        self.folder_before_search = None

    def _matches(self):
        q = X.norm(self.notes_search)
        return [(f, t, b) for f, ns in self.notes.items() for t, b in ns if q in X.norm(b)]

    def op_press(self, bundle, trigger, label):
        if bundle == NOTES and trigger.get("identifier") == NOTES_FIND["trigger"]["identifier"]:
            self.front = NOTES
            self.search_focus = True
            return {"status": "done"}
        return super().op_press(bundle, trigger, label)

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if bundle == NOTES and target.get("search"):
            self.notes_search = text
            if self.folder_before_search is None:
                self.folder_before_search = self.notes_folder
            self.notes_folder = SCOPE_ALL
            hits = self._matches()
            self.shown_note = hits[0][1] if hits else None
            return {"status": "done"}
        return super().op_enter(bundle, target, text, commit, keys)

    def op_search_fields(self, bundle):
        if bundle == NOTES:
            return {"status": "done", "fields": [{"index": 0, "text": "Suchen", "value": self.notes_search}]}
        return super().op_search_fields(bundle)

    def op_rows(self, bundle, kind, back=None):
        if kind == "notes_notes" and self.notes_search:
            self.front = NOTES
            return {"status": "done", "where": self.notes_folder, "shown_note": self.shown_note,
                    "rows": [{"label": t, "text": f"{t} | {b.split(chr(10))[1] if chr(10) in b else ''}",
                              "selected": t == self.shown_note} for _f, t, b in self._matches()]}
        return super().op_rows(bundle, kind, back)

    def op_screen(self):
        r = super().op_screen()
        r["screen"]["ui_language"] = "de"
        return r

    def body_of(self, title):
        for ns in self.notes.values():
            for t, b in ns:
                if t == title:
                    return b
        return None

    def op_read_state(self, read):
        out = super().op_read_state(read)
        for p, r in zip(read, out["read"]):
            if p["kind"] == "notes":
                r.update(search=self.notes_search, shown_text=self.body_of(self.shown_note))
        return out


def arch6(guest, planner, chooser=None, spend=None, threshold=0.5, **kw):
    return U.Arch6(guest, EMAP, planner=planner, chooser=chooser, spend=spend, inbox=None, sleep=lambda s: None,
                   threshold=threshold, **kw)


def ops(g, name):
    return [a for op, a in g.log if op == name]


def effects(steps):
    return [(s["effect"], s["app"]) for s in steps]


SB14 = ("Switch notes to gallery view, find the one about Oma's birthday, then look up the word 'Geburtstag' in "
        "the Oxford dictionary.")
SB05 = "Such mal in meinen Notizen nach der, wo's um den Umzug geht."
G04 = "Die alte Packliste für die Hüttentour kann weg. Such sie bitte raus, aber frag mich, bevor du sie löschst."


# ── 1. find means show ───────────────────────────────────────────────────────

def test_find_and_search_words_are_told_apart():
    assert U.asks_find(SB14) and U.asks_find(SB05) and U.asks_find(G04)
    assert U.asks_find("Can you look up the note about the hinge?") and not U.asks_find("Öffne die Notiz Umzug.")
    assert U.asks_search(SB05) and U.asks_search("Search my notes for the move.")
    assert U.asks_search("Durchsuch die Notizen nach Umzug.") and U.asks_search("Bitte nach Umzug suchen.")
    assert not U.asks_search(SB14) and not U.asks_search(G04)
    assert not U.asks_search("Find the one about Oma's birthday.")


def test_a_search_that_ends_a_find_gets_the_open_step_sb14_like():
    plan = [S("launch_app", "Notes"), S("toggle_select", "Notes", "Darstellung", "Als Galerie"),
            S("scoped_find", "Notes", "", "Oma Geburtstag"), S("launch_app", "Dictionary"),
            S("toggle_select", "Dictionary", "Suchen", "Oxford German Dictionary"),
            S("scoped_find", "Dictionary", "", "Geburtstag")]
    st, ch = U.find_means_show(SB14, plan)
    assert effects(st)[2:4] == [("scoped_find", "Notes"), ("open_item", "Notes")]
    assert st[3]["target"] == "Oma Geburtstag" and st[3]["added_by"] == "find means show" and ch
    assert effects(st)[-1] == ("scoped_find", "Dictionary")           # a dictionary look-up is not a content search
    ref = U.refine_plan6(SB14, plan, EMAP)
    # the user asked for no search: Arch5's rule drops the search before the open; the note is opened
    assert ("scoped_find", "Notes") not in effects(ref.steps) and ("open_item", "Notes") in effects(ref.steps)
    assert any("find means show" in c for c in ref.changes)


def test_a_search_the_user_asks_for_stays_in_front_of_the_open_sb05_like():
    plan = [S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Umzug")]
    ref = U.refine_plan6(SB05, plan, EMAP)
    assert effects(ref.steps) == [("launch_app", "Notes"), ("scoped_find", "Notes"), ("open_item", "Notes")]
    assert ref.steps[2]["target"] == "Umzug"
    # the same when the planner itself planned the open (Arch5's r4/r7 would drop the search)
    plan = [S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Umzug"), S("open_item", "Notes", "Umzug")]
    ref = U.refine_plan6(SB05, plan, EMAP)
    assert effects(ref.steps) == [("launch_app", "Notes"), ("scoped_find", "Notes"), ("open_item", "Notes")]
    assert any("the user asks for a search" in c for c in ref.changes)


def test_plans_that_already_show_the_item_or_ask_no_find_are_unchanged():
    plan = [S("launch_app", "Notes"), S("open_item", "Notes", "Umzug")]
    assert U.find_means_show(SB05, plan) == ([dict(s) for s in plan], [])
    plan = [S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Umzug")]
    assert U.find_means_show("Tipp Umzug in die Notizensuche.", plan)[1] == []
    # G04 (Arch5's test): a stray search before the select is still dropped
    st = [S("navigate", "Notes", "Hüttentour Planung"), S("scoped_find", "Notes", "", "Packliste"),
          S("select_item", "Notes", "alte Packliste für die Hüttentour"), S("human_gate", "Notes", "Packliste", "delete")]
    assert [s["effect"] for s in U.refine_plan6(G04, st, EMAP).steps] == ["navigate", "select_item", "human_gate"]
    # the user NAMES the search pattern: the search list is the result, no item is described
    for goal, pat in (("Search for 'invoice' only in the Archive folder.", "invoice"),
                      ("Such „Vertrag“ nur im Ordner Dokumente.", "Vertrag"),
                      ("Search my notes only in the Work folder for 'deadline'.", "deadline")):
        assert U.find_means_show(goal, [S("scoped_find", "Finder", "", pat)])[1] == [], goal
    # a search whose value is a plan variable (after a read) is not a find of an item
    st = [S("scoped_find", "Notes", "", "{{word}}")]
    assert U.find_means_show("Find the note with the word and search it.", st)[1] == []


def _notes_family():
    return {"Familie": [("Geburtstag Oma", "Geburtstag Oma\nTorte bestellen, 80. Geburtstag"),
                        ("Urlaub Opa", "Urlaub Opa\nOstsee im Juni")],
            "Persönliches": [("Einkaufsliste", "Einkaufsliste\nMilch, Brot"),
                             ("Umzug Gedanken", "Umzug Gedanken\nKartons besorgen")]}


def test_sb14_like_run_ends_with_the_note_open_and_no_search_left():
    g = FakeGuest6(notes=_notes_family())
    g.front, g.notes_folder = NOTES, "Familie"
    t = T4.task("F1", "Find the one about Oma's birthday in my notes.", [{"kind": "notes", "folders": ["Familie"]}],
                {"notes_shown": {"folder": "Familie", "note": "Geburtstag Oma"}})
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Oma Geburtstag")))
    ch = T3.FakeChooser("Geburtstag Oma", conf=0.95)
    out = W.run_one4(t, "arch", 1, g, arch=arch6(g, p, ch), emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out["stopped"], out["steps"])
    assert g.shown_note == "Geburtstag Oma" and g.notes_search == ""
    assert out["steps"][-1]["step"]["added_by"] == "find means show"


def test_sb05_like_run_searches_and_shows_the_note():
    g = FakeGuest6(notes=_notes_family())
    g.front, g.notes_folder, g.shown_note = NOTES, "Persönliches", "Einkaufsliste"
    t = T4.task("F2", SB05, [{"kind": "notes", "folders": ["Persönliches"]}],
                {"notes_shown": {"folder": "Persönliches", "note": "Umzug Gedanken"}, "notes_search": "Umzug"})
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Umzug")))
    out = W.run_one4(t, "arch", 1, g, arch=arch6(g, p, T3.FakeChooser("Umzug", conf=0.95)), emap=EMAP)
    assert g.notes_search == "Umzug" and g.shown_note == "Umzug Gedanken"
    assert [s["step"]["effect"] for s in out["steps"]] == ["launch_app", "scoped_find", "open_item"]


def test_planner_w6_states_find_means_show():
    spec = U.load_planner6_spec()
    assert "- Find means show: a goal to find, locate, look up or show a note or file ends with that item OPEN" \
        in spec["w6_system"]
    assert spec["w6_sha256"] == hashlib.sha256(spec["w6_system"].encode()).hexdigest()
    assert spec["w5_sha256_base"] == V.load_planner5_spec()["w5_sha256"]


# ── 2. judge: the note shown and its content; the search scope while a search is active ──

def _notes_read(folder, note, search="", text=None, lst=None):
    return {"ok": True, "folder": folder, "shown_note": note, "counts": {"Persönliches": 3}, "search": search,
            "selected": [], "list": lst, "shown_text": text}


def _sb05_task(with_text=True):
    want = {"folder": "Persönliches", "note": "Umzug Gedanken"}
    if with_text:
        want["text"] = "Kartons besorgen, Spedition anrufen bis Ende Oktober"
    return X.Task("J5", SB05, "single-app", {}, [{"kind": "notes", "folders": ["Persönliches"]}],
                  {"notes_shown": want, "notes_search": "Umzug"})


def test_judge_reads_the_search_scope_as_the_folder_while_a_search_is_active_sb05():
    t = _sb05_task()
    start = [_notes_read("Persönliches", "Einkaufsliste", text="Einkaufsliste\nMilch, Brot, Eier")]
    end = [_notes_read(SCOPE_ALL, "Umzug Gedanken", "Umzug",
                       "Umzug Gedanken\nKartons besorgen, Spedition anrufen bis Ende Oktober")]
    j5 = V.judge(t, start, end)
    assert not j5["success"] and j5["wrong_actions"] == 1               # Arch5's verdict (judge)
    j6 = U.judge(t, start, end)
    assert j6["success"] and j6["wrong_actions"] == 0 and j6["judge"] == "judge_notes"
    item = next(i for i in j6["items"] if i["what"].endswith("shown"))
    assert item["judge_changed"] == "wrong -> ok" and "search scope" in item["judge_note"]
    # the same sidebar WITHOUT an active search is another place: still wrong
    end2 = [_notes_read(SCOPE_ALL, "Umzug Gedanken", "", "Umzug Gedanken\nKartons besorgen, Spedition anrufen")]
    assert not U.judge(_sb05_task(), start, end2)["success"]
    assert U.is_search_scope("Alle auf meinem Mac") and U.is_search_scope("All iCloud")
    assert not U.is_search_scope("Persönliches") and not U.is_search_scope("Allerlei")


def test_judge_compares_the_shown_notes_content_when_it_was_read():
    t = _sb05_task()
    start = [_notes_read("Persönliches", "Einkaufsliste")]
    # the right title in the right folder, but not the author's note (another text): wrong
    end = [_notes_read("Persönliches", "Umzug Gedanken", "Umzug", "Umzug Gedanken\nsomething else")]
    j6 = U.judge(t, start, end)
    assert not j6["success"] and next(i for i in j6["items"] if i["what"].endswith("shown"))["status"] == "wrong"
    # no text read (Arch4 / Arch5 records) or none declared: the title decides, as before
    end = [_notes_read(SCOPE_ALL, "Umzug Gedanken", "Umzug", None)]
    assert U.judge(t, start, end)["success"] and U.judge(_sb05_task(False), start, end)["success"]


def test_judge_changes_nothing_else():
    t = X.Task("J6", "Öffne die Notiz Umzug.", "single-app", {}, [{"kind": "notes", "folders": ["Persönliches"]}],
               {"notes_shown": {"folder": "Persönliches", "note": "Umzug Gedanken"}})
    start = [_notes_read("Persönliches", "Einkaufsliste")]
    end = [_notes_read("Persönliches", "Einkaufsliste")]
    j5, j6 = V.judge(t, start, end), U.judge(t, start, end)
    assert [i["status"] for i in j5["items"]] == [i["status"] for i in j6["items"]] == ["missing"]
    assert j5["success"] == j6["success"] and j5["wrong_actions"] == j6["wrong_actions"]
    t2 = X.Task("J7", "Mach nichts.", "refuse", {}, [{"kind": "front", "bundles": [TE]}], {"no_action": True})
    r = [{"ok": True, "documents": []}]
    assert U.judge(t2, r, r) == dict(V.judge(t2, r, r))


def test_run_one6_judges_every_arm_with_judge_and_keeps_prior_verdict():
    g = FakeGuest6(notes=_notes_family())
    g.front, g.notes_folder, g.shown_note = NOTES, "Persönliches", "Einkaufsliste"
    t = X.Task("F3", SB05, "single-app", {"app": NOTES}, [{"kind": "notes", "folders": ["Persönliches"]}],
               {"notes_shown": {"folder": "Persönliches", "note": "Umzug Gedanken", "text": "Kartons besorgen"},
                "notes_search": "Umzug"})
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Umzug")))
    out = U.run_one6(t, "arch", 1, g, arch=arch6(g, p, T3.FakeChooser("Umzug", conf=0.95)), emap=EMAP)
    assert out["judge"]["success"] and not out["judge_prefs"]["success"]
    assert out["read_end"][0]["folder"] == SCOPE_ALL and out["read_end"][0]["search"] == "Umzug"


def test_the_guest_notes_read_carries_the_shown_notes_text_read_only():
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index('    if k == "notes":'):src.index('    if k == "te_prefs":')]
    assert '"shown_text": nfc(body)[:1000] if body is not None else None' in body
    assert "EX.run" not in body and "AXPress" not in body


# ── 3. settings_set: an app's own settings window (generic) ─────────────────

TE_NEW_DOC = [
    {"label": "Formatierter Text", "kind": "choice", "value": True, "enabled": True, "group": "Format"},
    {"label": "Reiner Text", "kind": "choice", "value": False, "enabled": True, "group": "Format"},
    {"label": "Rechtschreibung während der Texteingabe prüfen ", "kind": "checkbox", "value": True, "enabled": True},
    {"label": "Rechtschreibung automatisch korrigieren", "kind": "checkbox", "value": True, "enabled": True},
    {"label": "Intelligente Links", "kind": "checkbox", "value": False, "enabled": True},
    {"label": "Text ersetzen", "kind": "checkbox", "value": True, "enabled": True}]
TE_OPEN_SAVE = [{"label": "Beim Sichern eine Endung „.txt“ hinzufügen", "kind": "checkbox", "value": True,
                 "enabled": True}]
NOTES_SETTINGS = [{"label": "Notizen sortieren nach", "kind": "popup", "value": "Bearbeitungsdatum", "enabled": True,
                   "options": ["Bearbeitungsdatum", "Erstellungsdatum", "Titel"]}]


class SettingsGuest(FakeGuest6):
    """A settings window per app: tabs, checkboxes, choices, pop-ups; the op mirrors
    the guest's (read every tab / set one control and read it back / close)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.settings = {TE: {"Neues Dokument": [dict(c) for c in TE_NEW_DOC],
                              "Öffnen und Sichern": [dict(c) for c in TE_OPEN_SAVE]},
                         NOTES: {"": [dict(c) for c in NOTES_SETTINGS]}}
        self.windows_open = 0

    def op_settings(self, bundle, action="read", tab="", label="", kind="", value=None):
        if bundle == DICT:
            return {"status": "refused", "detail": "Lexikon's settings choose its sources"}
        if bundle not in self.settings:
            return {"status": "failed", "detail": "no settings item"}
        tabs = self.settings[bundle]
        out = {"status": "done", "tabs": [t for t in tabs if t], "tab_shown": next(iter(tabs))}
        if action == "read":
            return dict(out, controls={t: [dict((k, v) for k, v in c.items() if k != "options") for c in cs]
                                       for t, cs in tabs.items()})
        cs = [c for c in tabs.get(tab, []) if X.norm(c["label"]) == X.norm(label) and c["kind"] == kind]
        if len(cs) != 1:
            return dict(out, status="failed", detail=f"{len(cs)} controls")
        c = cs[0]
        before = c["value"]
        if kind == "checkbox":
            c["value"] = bool(value)
        elif kind == "choice":
            for o in tabs[tab]:
                if o["kind"] == "choice" and o.get("group") == c.get("group"):
                    o["value"] = o is c
        else:
            if value not in c["options"]:
                return dict(out, status="failed", detail="no such option")
            c["value"] = value
        return dict(out, before=before, after=c["value"], verified=True)

    def prefs(self):
        return {c["label"]: c["value"] for c in self.settings[TE]["Neues Dokument"] if c["kind"] == "checkbox"}

    def op_read_state(self, read):
        out = super().op_read_state(read)
        for k, p in enumerate(read):
            if p["kind"] == "te_prefs":
                out["read"][k] = {"ok": True, "prefs": self.prefs()}
        return out


SB07 = "Schalt mal die automatische Rechtschreibkorrektur in TextEdit aus."


def _sb07_task():
    return X.Task("K7", SB07, "single-app", {"app": TE}, [{"kind": "te_prefs"}],
                  {"te_prefs": {"Rechtschreibung automatisch korrigieren": False}})


def test_settings_set_turns_off_a_new_document_setting_sb07_like_and_replays_compiled():
    g = SettingsGuest()
    g.front = TE
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"),
                           S("settings_set", "TextEdit", "Rechtschreibung automatisch korrigieren", "off")))
    ch = T3.FakeChooser("Rechtschreibung automatisch korrigieren", conf=0.95)
    out = U.run_one6(_sb07_task(), "arch", 1, g, arch=arch6(g, p, ch), emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out["stopped"], out["steps"][-1])
    st = out["steps"][-1]
    assert st["resolved"] == {"op": "settings_set", "tab": "Neues Dokument", "kind": "checkbox", "value": False,
                              "label": "Rechtschreibung automatisch korrigieren"}
    assert st["crosscheck"]["verdict"] == "agrees"                    # the planner's label, not the user's words
    assert g.prefs()["Rechtschreibung während der Texteingabe prüfen "] is True
    sets = [a for a in ops(g, "settings") if a.get("action") == "set"]
    assert len(sets) == 1 and "enter" not in [op for op, _a in g.log]
    # the compiled replay sets it again with no model call
    g2 = SettingsGuest()
    g2.front = TE
    out2 = U.run_one6(_sb07_task(), "arch", 1, g2, arch=U.Arch6(g2, EMAP, sleep=lambda s: None),
                      compiled=out["compiled"], mode="compiled", emap=EMAP)
    assert out2["judge"]["success"] and out2["model_calls"] == 0


def test_settings_set_is_not_textedit_specific_a_popup_and_a_choice():
    g = SettingsGuest()
    a = arch6(g, FakePlanner4(plan4()), T3.FakeChooser("x", conf=0.95))
    t = T4.task("K8", "Sortier meine Notizen nach Titel.", [], {})
    log = {"step": {}}
    rec = X.Rec("K8", "arch", 1)
    step = S("settings_set", "Notes", "Notizen sortieren nach", "Titel")
    assert a.do_step(t, rec, step, log) == "ok", log
    assert g.settings[NOTES][""][0]["value"] == "Titel" and log["resolved"]["kind"] == "popup"
    for c in g.settings[TE]["Neues Dokument"]:
        c.pop("group", None)                                     # as the guest reads TextEdit's Format choices
    t2 = T4.task("K9", "Neue TextEdit-Dokumente bitte als reinen Text.", [], {})
    log2 = {"step": {}}
    assert a.do_step(t2, rec, S("settings_set", "TextEdit", "Format", "Reiner Text"), log2) == "ok", log2
    fmt = {c["label"]: c["value"] for c in g.settings[TE]["Neues Dokument"] if c["kind"] == "choice"}
    assert fmt == {"Formatierter Text": False, "Reiner Text": True}
    # a tab named by the target: the control is looked for in that tab
    hit, want, how = U.resolve_setting("Öffnen und Sichern > Endung", "on",
                                       U.settings_candidates(g.op_settings(TE, "read")))
    assert len(hit) == 1 and want is True


def test_settings_set_asks_the_picker_when_the_target_names_no_control_and_hands_off_when_unsure():
    g = SettingsGuest()
    ch = T3.FakeChooser("Rechtschreibung automatisch korrigieren", conf=0.95)
    a = arch6(g, FakePlanner4(plan4()), ch)
    log, rec = {"step": {}}, X.Rec("K10", "arch", 1)
    assert a.do_step(_sb07_task(), rec, S("settings_set", "TextEdit", "autocorrect", "off"), log) == "ok", log
    assert log["resolution"]["by"] == "picker" and g.prefs()["Rechtschreibung automatisch korrigieren"] is False
    g = SettingsGuest()
    a = arch6(g, FakePlanner4(plan4()), T3.FakeChooser("Rechtschreibung automatisch korrigieren", conf=0.3),
              threshold=0.6)
    log = {"step": {}}
    assert a.do_step(_sb07_task(), rec, S("settings_set", "TextEdit", "autocorrect", "off"), log) == "handoff"
    assert g.prefs()["Rechtschreibung automatisch korrigieren"] is True
    assert not [x for x in ops(g, "settings") if x.get("action") == "set"]


def test_a_setting_the_planner_named_against_the_users_words_is_handed_off_before_any_change():
    g = SettingsGuest()
    a = arch6(g, FakePlanner4(plan4()), T3.FakeChooser("Rechtschreibung automatisch korrigieren", conf=0.95),
              threshold=0.6)
    log, rec = {"step": {}}, X.Rec("K11", "arch", 1)
    assert a.do_step(_sb07_task(), rec, S("settings_set", "TextEdit", "Intelligente Links", "off"), log) == "handoff"
    assert log["crosscheck"]["verdict"] == "contradicts"
    assert not [x for x in ops(g, "settings") if x.get("action") == "set"]


def test_lexikons_settings_are_never_operated():
    g = SettingsGuest()
    a = arch6(g, FakePlanner4(plan4()), T3.FakeChooser("x"))
    log = {"step": {}}
    assert a.do_step(_sb07_task(), X.Rec("K12", "arch", 1), S("settings_set", "Dictionary", "Wikipedia", "on"),
                     log) == "failed"
    assert "Lexikon" in log["why"]


def test_the_guest_settings_op_keeps_the_rules():
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index("SETTINGS_ITEMS = "):src.index("OPS = {")]
    assert "SETTINGS_REFUSED = (DICT,)" in body and "need_managed(bid)" in body
    assert "EX.run(" in body and "keys" not in body and '"enter"' not in body and "AXSetValue" not in body
    assert "X.irreversible_reason(" in body and "WEB_SOURCES" in body and "guard_before(pid" in body
    assert "_show_tab(pid, w, first)" in body and "_close_settings(pid, w, opened)" in body
    assert "never guessed" in body
    assert '"settings": op_settings' in src


def test_planner_w6_maps_settings_set_in_the_vocabulary():
    spec = U.load_planner6_spec()
    assert "settings_set" in [t["name"] for t in spec["types"]]
    enum = spec["plan_schema"]["properties"]["steps"]["items"]["properties"]["effect"]["enum"]
    assert "settings_set" in enum and "- settings_set(target=the setting's label" in spec["w6_system"]
    assert "- Settings: a setting of the app itself" in spec["w6_system"]
    assert "settings_set (Einstellungen > Neues Dokument): " in spec["w6_system"]


# ── the Arch6 host driver ────────────────────────────────────────────────────

def _host6():
    import importlib
    import sys
    here = os.path.join(os.path.dirname(os.path.abspath(X.__file__)), "..", "execution", "guest")
    sys.path.insert(0, here)
    return importlib.import_module("desktop_e2e6_host")


# ── 4. the language bridge; options that fit OpenJev's 120 characters ─────────

GB12 = ("In the September inventory file about folding tables, change the count to 6. Don't touch the August "
        "inventory or the September chairs.")


def test_the_ui_language_comes_from_the_screens_menu_bar():
    assert U.ui_language_of(["Apple", "Finder", "Ablage", "Bearbeiten", "Darstellung", "Gehe zu", "Fenster",
                             "Hilfe"]) == "de"
    assert U.ui_language_of(["Apple", "TextEdit", "File", "Edit", "Format", "View", "Window", "Help"]) == "en"
    assert U.ui_language_of(["Apple", "Lexikon"]) is None
    line = U.screen_line6({"front": "Finder", "finder_window": "September", "ui_language": "de"})
    assert line.endswith("the user interface is in German (de).") and "September" in line
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index("def op_screen(a):"):src.index("def op_notes_verify(a):")]
    assert 'out["ui_language"] = U6.ui_language_of(titles)' in body and "AXPress" not in body


def test_fit_option_keeps_the_content_that_tells_items_apart_within_120_characters_gb12():
    rows = ["Bestand 04.txt | modified 2026-09-22 10:00, created 2026-09-22 10:00 | 33 Byte | Reiner Text | "
            "content: Gegenstand: Klapptische Anzahl: 5",
            "Bestand 05.txt | modified 2026-09-22 10:05, created 2026-09-22 10:05 | 34 Byte | Reiner Text | "
            "content: Gegenstand: Klappstühle Anzahl: 5"]
    old = [" ".join(r.split())[:120] for r in rows]                     # what Arch5's client sent
    assert old[0].endswith("Klap") and old[1].endswith("Klap")
    new = [U.fit_option(r) for r in rows]
    assert all(len(x) <= 120 for x in new) and "Klapptische" in new[0] and "Klappstühle" in new[1]
    assert new[0] == "Bestand 04.txt | modified 2026-09-22 10:00 | content: Gegenstand: Klapptische Anzahl: 5"
    assert U.fit_option("September | 22.09.26 | Ordner") == "September | folder | modified 2026-09-22"
    assert U.fit_option("Kürbissuppe | Zum Schluss Croûtons darüberstreuen. | modified 2026-09-26") == \
        "Kürbissuppe | Zum Schluss Croûtons darüberstreuen. | modified 2026-09-26"
    long = "x.txt | modified 2026-09-01 10:00 | content: " + "wort " * 60
    assert len(U.fit_option(long)) <= 120 and U.fit_option(long).endswith("…")


def test_german_ui_and_an_english_target_the_question_gives_both_phrasings_gb12_like():
    tree = {"L/September/Bestand 04.txt": "Gegenstand: Klapptische\nAnzahl: 5",
            "L/September/Bestand 05.txt": "Gegenstand: Klappstühle\nAnzahl: 5"}
    g = FakeGuest6(dict(tree), shown="L/September")
    seen = []

    def chooser(state, q, options):
        seen.append((state, q, list(options)))
        return next(i for i, o in enumerate(options) if "Klapptische" in o), 0.9, {"cost": 0.0}
    step = dict(S("open_item", "Finder", "September inventory file about folding tables"),
                target_ui="Bestandsdatei über die Klapptische")
    t = T4.task("L1", GB12, [{"kind": "front"}],
                {"opened": {"bundle": TE, "path": ROOT + "/L/September/Bestand 04.txt"}}, kind="cross-app")
    a = arch6(g, FakePlanner4(json.dumps({"steps": [step]})), chooser, threshold=0.6)
    a._ui_lang = "de"
    out = W.run_one4(t, "arch", 1, g, arch=a, emap=EMAP)
    assert out["judge"]["success"], (out["judge"], out["stopped"], out["steps"])
    state, q, options = seen[0]
    assert '"September inventory file about folding tables"' in q and '"Bestandsdatei über die Klapptische"' in q
    assert "(German)" in q and q.startswith("Which listed item should be used for this task: " + GB12)
    # candidates keep their on-screen (German) text, within 120 characters, content intact
    assert all(len(o) <= 120 for o in options) and any("Klapptische" in o for o in options)
    assert any("Klappstühle" in o for o in options) and "folding" not in " ".join(options)
    assert "The user interface is in German." in state and "created" not in " ".join(options)
    pk = next(s for s in out["steps"] if s.get("pick"))["pick"]
    assert pk["bridge"]["target_ui"] == "Bestandsdatei über die Klapptische" and pk["bridge"]["ui_language"] == "de"


def test_no_second_phrasing_leaves_prior_question():
    assert U.bridge_question("Open the invoice.", "invoice", "", "de") is None
    assert U.bridge_question("Öffne die Rechnung.", "Rechnung", "Rechnung", "de") is None
    q = U.bridge_question("Open the invoice.", "the invoice", "die Rechnung", "de")
    assert "\"the invoice\"" in q and "\"die Rechnung\"" in q and "(German)" in q


def test_parse_plan6_keeps_target_ui_and_the_planner_is_told_the_screens_language():
    spec = U.load_planner6_spec()
    txt = json.dumps({"steps": [dict(S("open_item", "Finder", "the folding tables"), target_ui="die Klapptische")]})
    steps, err = U.parse_plan6(txt, spec)
    assert err is None and steps[0]["target_ui"] == "die Klapptische"
    steps, err = U.parse_plan6(plan4(S("launch_app", "Notes")), spec)
    assert err is None and steps[0]["target_ui"] == ""
    items = spec["plan_schema"]["properties"]["steps"]["items"]
    assert "target_ui" in items["required"] and items["properties"]["target_ui"] == {"type": "string"}
    assert "- The screen's language: files, notes, their contents, options and settings appear" in spec["w6_system"]
    assert "target_ui = the target phrased in the language of the screen's user interface" in spec["w6_system"]


# ── 5. t2 recalibrated on a larger development set (seen data only), frozen ─────

def test_t2_is_recalibrated_on_the_larger_seen_set_and_frozen():
    spec = U.load_picker6()
    oj = spec["pickers"]["openjev"]
    assert spec["cw_max"] == 0.03 and oj["n"] == oj["n_by_source"]["step2"] + oj["n_by_source"]["dev"]
    assert oj["n_by_source"]["step2"] == 90 and oj["n_by_source"]["dev"] > 60
    # the rule: the smallest grid value whose confident-wrong rate is <= 3 %
    ok = [row["t2"] for row in oj["curve"] if row["cw"] <= spec["cw_max"]]
    assert oj["t2"] == min(ok) and U.t2_of6("openjev", spec) == oj["t2"]
    assert all(f.startswith(("desktop-e2e3", "desktop-e2e4", "desktop-e2e5")) for f in spec["data"]["dev_files"])
    assert "jev" not in spec["pickers"]


def test_the_openjev_arm_takes_prior_t2_and_mode_none_hands_off():
    a = U.Arch6(FakeGuest6(), EMAP, planner=FakePlanner4(plan4()), chooser=T3.FakeChooser("x"), picker_name="openjev",
                sleep=lambda s: None)
    assert a.threshold == U.t2_of6("openjev")
    n = U.Arch6(FakeGuest6(), EMAP, planner=FakePlanner4(plan4()), picker_name="none", sleep=lambda s: None)
    assert n.chooser is None and n.threshold > 1.0


# ── development findings on SEEN tasks ──────────────────────────────────────────

def test_notes_search_rows_carry_the_title_and_the_notes_check_clears_a_left_search():
    here = os.path.dirname(os.path.abspath(X.__file__))
    src = open(os.path.join(here, "..", "execution", "guest", "desktop_e2e_job.py"), encoding="utf-8").read()
    body = src[src.index("def note_rows(pid):"):src.index("def note_body(pid):")]
    assert 'if searching and len(tx) >= 4:' in body and '"label": tx[-1]' in body and '"search_row": True' in body
    ver = src[src.index("def op_notes_verify(a):"):src.index("# TextEdit's Open panel")]
    assert "if notes_searching(pid):" in ver and "quit_app(NOTES, setup)" in ver
    # the host picks among search rows by their titles: the map names the searched note, not a snippet
    g = FakeGuest6(notes=_notes_family())
    g.front, g.notes_folder, g.shown_note = NOTES, "Persönliches", "Einkaufsliste"
    g.notes["picker-B-N2"] = [("Teammeeting", "Teammeeting\nAgenda: Budget, neue Kollegin, Umzug ins 3. OG")]
    t = X.Task("F4", SB05, "single-app", {"app": NOTES}, [{"kind": "notes", "folders": ["Persönliches"]}],
               {"notes_shown": {"folder": "Persönliches", "note": "Umzug Gedanken"}, "notes_search": "Umzug"})
    p = FakePlanner4(plan4(S("launch_app", "Notes"), S("scoped_find", "Notes", "", "Umzug")))
    out = U.run_one6(t, "arch", 1, g, arch=U.Arch6(g, EMAP, planner=p, picker_name="none", sleep=lambda s: None),
                     emap=EMAP)
    assert out["judge"]["success"] and g.shown_note == "Umzug Gedanken"
    assert out["steps"][-1]["resolution"]["by"] == "map"


def test_a_menu_setting_with_no_document_open_goes_to_the_apps_settings_sb07_dev():
    screen = {"front": "TextEdit", "textedit_documents": [], "ui_language": "de"}
    ref = Y.Refined([S("launch_app", "TextEdit"), dict(S("flag_toggle", "TextEdit", "automatische Rechtschreibkorrektur",
                                                         "off"), target_ui="Rechtschreibung automatisch korrigieren")])
    U.route_settings_by_screen(ref, screen)
    assert ref.steps[1]["effect"] == "settings_set" and ref.changes
    # a document is open, or the plan opens one first: the menu setting stays
    for sc, st in (({"textedit_documents": ["Brief.txt"]}, [S("flag_toggle", "TextEdit", "Fett", "on")]),
                   (screen, [S("open_item", "Finder", "Brief.txt"), S("flag_toggle", "TextEdit", "Fett", "on")]),
                   ({"front": "Finder"}, [S("flag_toggle", "TextEdit", "Fett", "on")])):
        r = Y.Refined([dict(x) for x in st])
        U.route_settings_by_screen(r, sc)
        assert [x["effect"] for x in r.steps] == [x["effect"] for x in st]
    # end to end: the planner's flag becomes settings_set; the menu's abbreviation names the settings label
    g = SettingsGuest()
    g.front = TE
    p = FakePlanner4(plan4(S("launch_app", "TextEdit"), S("flag_toggle", "TextEdit", "Rechtschreibung autom. korrigieren",
                                                          "off")))
    orig = g.op_screen
    g.op_screen = lambda: dict(orig(), screen=dict(orig()["screen"], textedit_documents=[]))
    out = U.run_one6(_sb07_task(), "arch", 1, g, arch=U.Arch6(g, EMAP, planner=p, picker_name="none",
                                                            sleep=lambda s: None), emap=EMAP)
    assert out["judge"]["success"], (out["stopped"], out["steps"][-1])
    assert out["steps"][-1]["resolution"]["by"] == "map" and out["steps"][-1]["step"]["effect"] == "settings_set"
    assert U.label_match6("Rechtschreibung autom. korrigieren", ["Rechtschreibung automatisch korrigieren",
                                                                "Rechtschreibung während der Texteingabe prüfen"]) == [0]
    assert U.label_match6("autom korrigieren", ["Rechtschreibung automatisch korrigieren"]) == []
    # a flag target naming a menu GROUP names no setting: the picker chooses by the user's words
    ref = Y.Refined([dict(S("flag_toggle", "TextEdit", "Rechtschreibung und Grammatik", "off"),
                          target_ui="Rechtschreibung und Grammatik")])
    U.route_settings_by_screen(ref, screen, EMAP)
    assert ref.steps[0]["effect"] == "settings_set" and ref.steps[0]["target"] == ref.steps[0]["target_ui"] == ""
    assert ref.steps[0]["flag_target"] == "Rechtschreibung und Grammatik"
    g = SettingsGuest()
    a = arch6(g, FakePlanner4(plan4()), T3.FakeChooser("Rechtschreibung automatisch korrigieren", conf=0.9))
    log = {"step": ref.steps[0]}
    assert a.do_step(_sb07_task(), X.Rec("K13", "arch", 1), ref.steps[0], log) == "ok", log
    assert log["resolution"]["by"] == "picker" and g.prefs()["Rechtschreibung automatisch korrigieren"] is False
    assert g.prefs()["Rechtschreibung während der Texteingabe prüfen "] is True
    assert U.fit_option("August | 23.08.26 | -- | Ordner") == "August | folder | modified 2026-08-23"


def test_judge_reads_a_folder_the_author_names_without_a_note_sc11_like():
    t = X.Task("J8", "Mach die Notizen im Fitnessplan als Galerie an und such nach 'Bein'.", "single-app", {},
               [{"kind": "notes", "folders": ["Fitnessplan"]}],
               {"notes_search": "Bein", "notes_folder": "Fitnessplan", "shown_note_coupled": True})
    start = [dict(_notes_read("Fitnessplan", "Mittwoch"), counts={"Fitnessplan": 3})]
    end = [dict(_notes_read(SCOPE_ALL, "Montag", "Bein"), counts={"Fitnessplan": 3})]
    j = U.judge(t, start, end)
    assert j["success"], j
    assert next(i for i in j["items"] if i["what"].startswith("Notes shows the folder"))["status"] == "ok"
    end2 = [dict(_notes_read("Notizen", "Montag", ""), counts={"Fitnessplan": 3})]
    j2 = U.judge(t, start, end2)
    assert not j2["success"] and next(i for i in j2["items"] if i["what"].startswith("Notes shows"))["status"] == "wrong"


def test_prior_tasks_are_the_draw_verbatim_local_and_without_web_sources():
    data = U.load_tasks6()
    assert len(data["tasks"]) == 15 and all(t["drawn"] for t in data["tasks"])
    assert [t["author_task"]["id"] for t in data["tasks"]] == data["drawn"]
    ts = U.tasks6(data)
    assert len(ts) == 15 and all(t.start for t in ts)
    for t in ts:
        for g in t.expect.get("group") or []:
            assert g["selected"] not in ("Wikipedia", "Alle Lexika")
        assert "{GEN6}" not in json.dumps(t.expect) and "{GEN5}" not in json.dumps(t.expect)
        assert U.GEN6 in json.dumps(t.start) or not t.start.get("finder")
    assert len(U.tasks6_sha256(data)) == 64


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
