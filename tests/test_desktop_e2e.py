"""End-to-end test: the host orchestration,
offline, with a FAKE guest and fake models.

What is pinned here: the planner is the frozen w1 wording (sha256); the map
resolves by the observed-effect entries and the option LABEL, and sends misses
to the picker; the picker acts only on a pick that is the same in both option
orders and confident; the irreversible-action guard reads German and English
verbs as whole-token prefixes; a human gate stops execution and is declined;
refuse executes nothing; a failed effect check is re-planned once, never
retried blindly; the compiled replay makes ZERO model calls; the whole run
stops at the budget cap, on a guest stop, on an irreversible change; the judge
separates ok / missing / wrong and counts unexpected and irreversible changes.

No desktop, no AX, no subprocess, no network.
"""
import copy
import hashlib
import json
import os

from anyuse.execution import desktop_e2e as X

FINDER, NOTES, TE, DCM = "com.apple.finder", "com.apple.Notes", "com.apple.TextEdit", "com.apple.DigitalColorMeter"
EMAP = X.EffectMap()


# ── fakes ───────────────────────────────────────────────────────────────────

def _group(app, effect, pred=lambda e: True):
    return next(e for e in EMAP.entries(app, effect) if pred(e))


SORT = _group("Finder", "toggle_select")
MAG = _group("Digital Color Meter", "toggle_select", lambda e: "Vergrößerung" in e["group"])
LOCK = EMAP.app("Digital Color Meter")["unmapped_observed"]["flag_toggle"][0]
OPEN = EMAP.open_trigger("Finder")
NEWF = _group("Finder", "object_create")
SAVE = _group("TextEdit", "doc_save")


class FakeGuest:
    """Just enough of desktop_e2e_job's ops, over a dict state."""

    def __init__(self):
        self.log = []
        self.front = FINDER
        self.folder = "/Users/guest/" + X.LIST_DIR[2:]          # as the guest reports it
        self.files = {n: f"seed {n}" for n, _d, _s in X.LIST_SEEDS}
        self.dirs = []
        self.selected = []
        self.focus_new = False
        self.marks = {o["key"]: o["label"] == "Name" for o in SORT["options"]}
        self.mag = {o["key"]: o["label"] == "8x" for o in MAG["options"]}
        self.lock = False
        self.other_marks = ["_NS:999||0"]
        self.doc = os.path.expanduser(X.DOC)
        self.doc_text, self.doc_buf, self.doc_mtime = "Entwurf\n", None, 1.0
        self.te_doc = None
        self.notes = {X.NOTES_PICK: [t for t, _l in X.NOTES_SEEDS], X.NOTES_START: ["Startnotiz"]}
        self.notes_folder, self.shown_note = X.NOTES_PICK, X.NOTES_REST
        self.overrides = {}          # op -> list of canned answers (popped first)
        self.baseline_actions = {}   # title -> effect for ax_step

    def __call__(self, op, **a):
        self.log.append((op, a))
        if self.overrides.get(op):
            return self.overrides[op].pop(0)
        return getattr(self, "op_" + op)(**a)

    def op_clean_start(self, task, start, read):
        return {"status": "done", "clean": True, "problems": [], "setup": [], "read": self.op_read_state(read)["read"]}

    def op_launch(self, bundle):
        self.front = bundle
        return {"status": "done"}

    def op_front(self):
        doc = self.te_doc if self.front == TE else None
        return {"status": "done", "front_bundle": self.front, "document": doc}

    def op_rows(self, bundle, kind):
        if kind == "finder_files":
            return {"status": "done", "where": "Testordner",
                    "rows": [{"label": n, "text": f"{n} | 17.03.26 | 2 KB | Text", "selected": n in self.selected}
                             for n in sorted(self.files)]}
        if kind == "finder_locations":
            return {"status": "done", "where": "Testordner",
                    "rows": [{"label": "Testordner", "text": "Testordner (the folder shown now)", "shown": True},
                             {"label": "Dokumente", "text": "Dokumente (sidebar)", "shown": False}]}
        if kind == "notes_notes":
            return {"status": "done", "where": self.notes_folder, "shown_note": self.shown_note,
                    "rows": [{"label": t, "text": t, "selected": t == self.shown_note}
                             for t in self.notes[self.notes_folder]]}
        if kind == "notes_folders":
            return {"status": "done", "where": self.notes_folder,
                    "rows": [{"label": f, "text": f, "shown": f == self.notes_folder} for f in self.notes]}

    def op_select(self, bundle, kind, label):
        if kind == "finder_files":
            self.selected = [label]
        elif kind == "notes_notes":
            self.shown_note = label
        elif kind == "notes_folders":
            self.notes_folder, self.shown_note = label, self.notes[label][0]
        return {"status": "done"}

    def _top(self, bundle):
        return {FINDER: self.marks, DCM: self.mag}.get(bundle, {})

    def op_marks(self, bundle, parent, options):
        keys = [o["key"] for o in options]
        if bundle == DCM and keys and keys[0].startswith("single:"):
            return {"status": "done", "ok": True, "target": {"options": keys, "selected": keys if self.lock else []},
                    "sentinel": sorted(self.other_marks + [k for k, v in self.mag.items() if v])}
        m = self._top(bundle)
        return {"status": "done", "ok": True, "target": {"options": keys, "selected": [k for k in keys if m.get(k)]},
                "sentinel": sorted(self.other_marks + (["single:lock"] if (bundle == DCM and self.lock) else []))}

    def op_press(self, bundle, trigger, label):
        if X.irreversible_reason(label):
            return {"status": "gated", "detail": "guard"}
        ident = trigger.get("identifier")
        if bundle == FINDER and ident == NEWF["trigger"]["identifier"]:
            self.focus_new = True
            return {"status": "done"}
        if bundle == FINDER and OPEN and ident == OPEN["trigger"]["identifier"]:
            self.front, self.te_doc = TE, os.path.join(self.folder, self.selected[0])
            return {"status": "done"}
        if bundle == TE and ident == SAVE["trigger"]["identifier"]:
            if self.doc_buf is not None:
                self.doc_text, self.doc_mtime = self.doc_buf, self.doc_mtime + 1
            return {"status": "done"}
        m = self._top(bundle)
        if ident == LOCK["trigger"]["identifier"]:
            self.lock = not self.lock
            return {"status": "done"}
        for o in (SORT["options"] if bundle == FINDER else MAG["options"]):
            if o["trigger"]["identifier"] == ident and o["trigger"].get("cmd_char") == trigger.get("cmd_char"):
                for k in m:
                    m[k] = k == o["key"]
                return {"status": "done"}
        return {"status": "refused", "detail": "unresolved"}

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if target.get("focused") and self.focus_new:
            self.dirs.append(text)
            self.focus_new = False
            return {"status": "done"}
        if target.get("document"):
            self.doc_buf = text
            return {"status": "done"}
        return {"status": "refused", "detail": "no entry"}

    def op_shown_folder(self, bundle):
        return {"status": "done", "path": self.folder}

    def op_listing(self, path):
        return {"status": "done", "names": sorted(list(self.files) + self.dirs)}

    def op_file(self, path):
        return {"status": "done", "ok": True, "text": self.doc_text, "mtime": self.doc_mtime}

    def op_read_state(self, read):
        out = []
        for p in read:
            if p["kind"] == "folder":
                ents = [{"name": n, "type": "file", "sha256": hashlib.sha256(t.encode()).hexdigest(), "text": t}
                        for n, t in self.files.items()] + [{"name": d, "type": "dir"} for d in self.dirs]
                if os.path.basename(p["path"]) == "docs":
                    ents = [{"name": "Testdokument.txt", "type": "file", "text": self.doc_text,
                             "sha256": hashlib.sha256(self.doc_text.encode()).hexdigest()},
                            {"name": "Sentinel.txt", "type": "file", "text": X.SENTINEL_TEXT, "sha256": "s"}]
                out.append({"ok": True, "entries": ents})
            elif p["kind"] == "marks":
                if p["bundle"] == FINDER:
                    items = [{"path": o["path"], "mark": self.marks[o["key"]]} for o in SORT["options"]]
                    items.append({"path": "Darstellung > Als Liste", "mark": True})
                else:
                    items = [{"path": o["path"], "mark": self.mag[o["key"]]} for o in MAG["options"]]
                    items.append({"path": "Darstellung > Position fixieren", "mark": self.lock})
                    items.append({"path": "Darstellung > Werte anzeigen > als Gleitzahl", "mark": True})
                out.append({"ok": True, "items": items})
            elif p["kind"] == "notes":
                out.append({"ok": True, "folder": self.notes_folder, "shown_note": self.shown_note,
                            "counts": {f: len(v) for f, v in self.notes.items()}, "search": "",
                            "list": [[t, "x"] for t in self.notes[self.notes_folder]]})
            elif p["kind"] == "front":
                out.append({"ok": True, "documents": [self.te_doc] if self.te_doc else []})
            else:
                out.append({"ok": False})
        return {"status": "done", "read": out}

    def op_ax_text(self):
        return {"status": "done", "bundle": self.front, "text": "[]"}

    def op_ax_step(self, step):
        title = (step.get("command") or {}).get("title", "")
        if X.irreversible_reason(title):
            return {"status": "gated", "detail": "guard"}
        eff = self.baseline_actions.get(title)
        if eff is None:
            return {"status": "refused", "detail": "0 candidates"}
        eff(self)
        return {"status": "done"}


def plan_text(*steps):
    return json.dumps({"steps": [dict(zip(("effect", "app", "target", "value"), s)) for s in steps]})


class FakePlanner:
    def __init__(self, *texts):
        self.texts, self.calls = list(texts), 0

    def __call__(self, goal):
        self.calls += 1
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0], {"cost": 0.001}


class FakeChooser:
    """Answers by the option text: ``want`` (substring) with ``conf``; ``flip``
    answers a different item when the options come reversed (order bias)."""

    def __init__(self, want, conf=0.9, flip=False):
        self.want = [want] if isinstance(want, str) else list(want)
        self.conf, self.flip, self.calls = conf, flip, 0

    def __call__(self, state, q, options):
        self.calls += 1
        hits = [i for i, o in enumerate(options) if any(w in o for w in self.want)]
        i = hits[0] if hits else 0
        if self.flip and self.calls % 2 == 0:
            i = (i + 1) % len(options)
        return i, self.conf, {"cost": 0.00003}


def arch(guest, planner, chooser=None, spend=None, inbox=None):
    return X.Arch(guest, EMAP, planner=planner, chooser=chooser, spend=spend, inbox=inbox, sleep=lambda s: None)


# ── the planner (w1, frozen) ────────────────────────────────────────────────

def test_planner_is_step1_frozen_w1():
    spec = X.load_planner_spec()
    assert hashlib.sha256(spec["w1_system"].encode()).hexdigest() == \
        "5c2fed38ec490ead72f7226cc593c6b251cf2dd53f759d03dd398464545ad940"
    m = X.planner_messages("Open the file from March.", spec)
    assert m[0]["role"] == "system" and m[1]["content"] == "Plan this goal: Open the file from March."
    assert spec["call"]["model"] == "gpt-oss-120b" and spec["call"]["temperature"] == 0.1


def test_parse_plan_rejects_outside_the_vocabulary():
    ok, err = X.parse_plan(plan_text(("launch_app", "Finder", "", "")))
    assert err is None and ok[0]["effect"] == "launch_app"
    assert X.parse_plan(plan_text(("click", "Finder", "", "")))[1]
    assert X.parse_plan(plan_text(("launch_app", "Safari", "", "")))[1]
    assert X.parse_plan("no json")[0] == []


# ── labels, guard, keys ─────────────────────────────────────────────────────

def test_label_match_is_literal_no_translation():
    labels = ["Alle Lexika", "Duden-Wissensnetz deutsche Sprache", "Apple Dictionary"]
    assert X.label_match("Duden", labels) == [1]
    assert X.label_match("Hinzugefügt am", ["Name", "Hinzugefügt am"]) == [1]
    assert X.label_match("date added", ["Name", "Hinzugefügt am"]) == []
    assert X.label_match("4x", ["1x", "2x", "4x", "8x"]) == [2]
    assert X.label_match("", labels) == []


def test_irreversible_guard_reads_german_and_english_tokens():
    for bad in ("In den Papierkorb legen", "Notiz löschen", "Senden an", "E-Mail", "Delete", "Teilen …",
                "Änderungen verwerfen", "Beenden", "Drucken …", "Fenster schließen"):
        assert X.irreversible_reason(bad), bad
    for fine in ("Format", "Hinzugefügt am", "Sichern", "Ersetzungen", "Intelligentes Kopieren/Einsetzen",
                 "Position fixieren", "Neuer Ordner", "Öffnen", "Als Galerie", "Duden-Wissensnetz deutsche Sprache"):
        assert X.irreversible_reason(fine) is None, fine


def test_only_return_and_escape_keys():
    assert X.key_allowed(36) and X.key_allowed(53)
    assert not X.key_allowed(51) and not X.key_allowed(117) and not X.key_allowed(36, 1 << 20)
    assert not X.key_allowed("x")


# ── the map ─────────────────────────────────────────────────────────────────

def test_map_uses_observed_entries_and_drops_refuted_ones():
    te = EMAP.app("TextEdit")
    assert [e["path"] for e in EMAP.entries("TextEdit", "doc_save")] == ["Ablage > Sichern"]
    assert {r["id"] for r in te["refuted"]} == {"com.apple.TextEdit:doc_save:id:_NS:97",
                                                  "com.apple.TextEdit:doc_save:id:_NS:273"}
    for app in EMAP.data["apps"].values():
        for e in app["entries"]:
            obs = e.get("observed") or [x for o in e.get("options", []) for x in o.get("observed", [])]
            assert obs or e["effect"] in ("field_rw", "property_update"), e["id"]
    assert "b9b3f76" in EMAP.data["caveat"]


def test_map_decides_by_label_and_sends_misses_to_the_picker():
    r = EMAP.resolve_toggle("Finder", "sort order", "Hinzugefügt am")
    assert r.by == "map" and r.option["label"] == "Hinzugefügt am"
    r = EMAP.resolve_toggle("Finder", "sort order", "date added")
    assert r.by == "picker" and len(r.candidates) == 6
    r = EMAP.resolve_toggle("Digital Color Meter", "magnification", "4x")
    assert r.by == "map" and r.option["label"] == "4x"
    r = EMAP.resolve_flag("Digital Color Meter", "lock position")
    assert r.by == "picker" and any("Position fixieren" in c["text"] for c in r.candidates)
    assert EMAP.resolve_flag("TextEdit", "intelligentes Kopieren/Einsetzen").by == "map"
    assert EMAP.resolve_single("Finder", "object_create", "folder").by == "map"
    assert EMAP.resolve_single("Notes", "object_create", "folder").entry["kind"] == "folder"
    assert EMAP.resolve_single("Notes", "object_create", "note").entry["kind"] == "item"
    assert EMAP.resolve_single("Finder", "scoped_find").by == "picker"
    assert EMAP.resolve_single("Dictionary", "scoped_find").by == "unsupported"


# ── the picker rule ─────────────────────────────────────────────────────────

def test_picker_acts_only_on_a_consistent_confident_pick():
    opts = ["Adressen.txt | 03.07.26", "Besprechung.txt | 17.03.26", "Ideen.txt | 19.08.26"]
    p = X.consistent_pick(FakeChooser("Besprechung"), "g", "s", opts)
    assert p.index == 1 and p.calls == 2
    assert [a["index"] for a in p.answers] == [1, 1]          # o1's reversed index mapped back
    assert X.consistent_pick(FakeChooser("Besprechung", flip=True), "g", "s", opts).index is None
    low = X.consistent_pick(FakeChooser("Besprechung", conf=0.1), "g", "s", opts)
    assert low.index is None and "below" in low.reason
    bad = X.consistent_pick(lambda s, q, o: (None, 0.0, {"error": "HTTP 500"}), "g", "s", opts)
    assert bad.index is None
    assert X.consistent_pick(FakeChooser("x"), "g", "s", []).index is None


def test_picker_question_is_step2_wording_v1():
    seen = []

    def ch(state, q, options):
        seen.append(q)
        return 0, 0.9, {}
    X.consistent_pick(ch, "Open the file from March in the test folder.", "s", ["a", "b"])
    assert seen[0] == "Which listed item should be used for this task: Open the file from March in the test folder."


def test_openjev_text_picker_through_anyuses_client_both_orders_threshold_08():
    """The chosen picker: OpenJev text via execution.openjev.Client.choose,
    t = 0.8 (the tuned OpenJev text configuration), both orders."""
    from anyuse.execution.openjev import Client
    sent = []

    def poster(url, body, headers, timeout):
        d = json.loads(body)
        sent.append((url, d))
        crit = d["questions"]["q"]["criteria"]
        pick = next(k for k, v in crit.items() if "Besprechung" in v)
        return 200, {}, json.dumps({"answers": {"q": {"type": "choice", "choice": pick,
                                                     "confidence": 0.93}}}).encode()
    cl = Client(url="http://127.0.0.1:18080", model="openjev-latest", poster=poster, key="")

    def chooser(state, q, options):
        i, c, a = cl.choose(state, q, options)
        return i, c, {"ms": a.ms, "error": a.error or None}
    opts = ["Adressen.txt | 03.07.26", "Besprechung.txt | 17.03.26", "Ideen.txt | 19.08.26"]
    p = X.consistent_pick(chooser, "Open the file from March.", "s", opts, threshold=X.OPENJEV_THRESHOLD)
    assert p.index == 1 and len(sent) == 2
    assert sent[0][0] == "http://127.0.0.1:18080/v1/systemone" and "steps" not in sent[0][1]
    assert sent[0][1]["model"] == "openjev-latest" and "images" not in sent[0][1]
    assert list(sent[1][1]["questions"]["q"]["criteria"].values()) == opts[::-1]
    low = X.consistent_pick(FakeChooser("Besprechung", conf=0.75), "g", "s", opts, threshold=X.OPENJEV_THRESHOLD)
    assert low.index is None                                   # 0.75 passes Jev's 0.2, not OpenJev's 0.8
    assert Client(url="http://10.0.0.5:8080").available is False  # http only to loopback


# ── budget ──────────────────────────────────────────────────────────────────

def test_ledger_refuses_a_call_that_could_cross_the_cap(tmp=None):
    import tempfile
    d = tempfile.mkdtemp()
    lg = X.Ledger(cap=0.003, path=os.path.join(d, "l.json"))
    assert lg("planner")
    lg("planner", actual=0.0015)
    assert not lg("planner")               # 0.0015 + worst 0.0018 > 0.003
    assert X.Ledger(cap=0.003, path=os.path.join(d, "l.json")).spent == 0.0015
    g = FakeGuest()
    a = arch(g, FakePlanner(plan_text(("launch_app", "Finder", "", ""))), spend=lg)
    try:
        a.run(X.task_by_id("T1"), X.Rec("T1", "arch", 1))
        assert False, "must stop"
    except X.RunStop as exc:
        assert "budget" in str(exc)
    assert not any(op == "launch" for op, _a in g.log)


# ── the architecture arm ────────────────────────────────────────────────────

T1_PLAN = plan_text(("launch_app", "Finder", "", ""), ("navigate", "Finder", "test folder", ""),
                    ("object_create", "Finder", "folder", "Pick-Q3"),
                    ("toggle_select", "Finder", "sort order", "date added"))


def test_arch_t1_map_and_picker_end_to_end_and_judged():
    g = FakeGuest()
    chooser = FakeChooser(["Testordner", "Hinzugefügt"])
    out = X.run_one(X.task_by_id("T1"), "arch", 1, g, arch=arch(g, FakePlanner(T1_PLAN), chooser))
    assert out["judge"]["success"], out["judge"]
    assert out["calls"] == {"planner": 1, "picker": 4, "agent": 0}     # navigate + sort, both orders each
    assert g.dirs == ["Pick-Q3"] and out["judge"]["wrong_actions"] == 0
    steps = {s["step"]["effect"]: s for s in out["steps"]}
    assert steps["object_create"]["resolution"]["by"] == "map"
    assert steps["toggle_select"]["resolution"]["by"] == "picker"
    assert steps["toggle_select"]["check"] == "toggle_select contract met"


def test_compiled_replay_makes_zero_model_calls():
    g = FakeGuest()
    first = X.run_one(X.task_by_id("T1"), "arch", 1, g, arch=arch(g, FakePlanner(T1_PLAN), FakeChooser(["Testordner", "Hinzugefügt"])))
    g2 = FakeGuest()
    blind = X.Arch(g2, EMAP, planner=None, chooser=None)
    rep = X.run_one(X.task_by_id("T1"), "arch", 1, g2, arch=blind, compiled=first["compiled"], mode="compiled")
    assert rep["model_calls"] == 0 and rep["judge"]["success"], rep["judge"]
    assert g2.dirs == ["Pick-Q3"]


def test_compiled_replay_never_calls_a_model_when_it_would_need_one():
    g = FakeGuest()
    blind = X.Arch(g, EMAP, planner=None, chooser=None)
    prog = [{"step": {"effect": "toggle_select", "app": "Finder", "target": "sort", "value": "x"},
             "resolved": None}]
    rec = blind.run(X.task_by_id("T1"), X.Rec("T1", "arch", 1, mode="compiled"), compiled=prog)
    assert sum(rec.calls.values()) == 0


def test_human_gate_stops_declines_and_nothing_irreversible_runs():
    g = FakeGuest()
    posted = []
    plan = plan_text(("launch_app", "Notes", "", ""), ("select_item", "Notes", "the dentist note", ""),
                     ("human_gate", "Notes", "the dentist note", "delete"))
    out = X.run_one(X.task_by_id("T12"), "arch", 1, g,
                    arch=arch(g, FakePlanner(plan), FakeChooser("Zahnarzt"),
                              inbox=lambda rec, why, log: posted.append(why) or "c0ffee000000"))
    assert out["stopped"] == "human gate (declined)" and out["gate_stops"] == 1
    assert out["judge"]["success"] and not out["judge"]["irreversible"], out["judge"]
    assert posted and posted[0].startswith("human_gate")
    assert g.notes[X.NOTES_PICK].count("Zahnarzt") == 1


def test_refuse_executes_nothing():
    g = FakeGuest()
    out = X.run_one(X.task_by_id("T13"), "arch", 1, g,
                    arch=arch(g, FakePlanner(plan_text(("refuse", "none", "email", "outside_apps")))))
    assert out["actions_executed"] == 0 and out["judge"]["success"]
    assert [op for op, _a in g.log] == ["clean_start", "read_state"]


def test_picker_hand_off_is_a_gated_stop():
    g = FakeGuest()
    out = X.run_one(X.task_by_id("T2"), "arch", 1, g,
                    arch=arch(g, FakePlanner(plan_text(("launch_app", "Finder", "", ""),
                                                       ("open_item", "Finder", "the file from March", ""))),
                              FakeChooser("Besprechung", flip=True)))
    assert out["stopped"] == "picker hand-off (declined)" and out["handoffs"] == 1 and out["gated"] >= 1
    assert not out["judge"]["success"]


def test_open_item_uses_the_open_trigger_and_is_judged_on_the_document():
    g = FakeGuest()
    out = X.run_one(X.task_by_id("T2"), "arch", 1, g,
                    arch=arch(g, FakePlanner(plan_text(("launch_app", "Finder", "", ""),
                                                       ("open_item", "Finder", "the file from March", ""))),
                              FakeChooser("Besprechung")))
    assert out["judge"]["success"], out["judge"]
    assert g.te_doc.endswith("Besprechung.txt")


def test_a_failed_check_is_replanned_once_never_retried_blindly():
    g = FakeGuest()
    g.overrides["listing"] = [{"status": "done", "names": []}, {"status": "done", "names": []},
                              {"status": "done", "names": []}, {"status": "done", "names": []}]
    planner = FakePlanner(T1_PLAN)
    out = X.run_one(X.task_by_id("T1"), "arch", 1, g, arch=arch(g, planner, FakeChooser(["Testordner", "Hinzugefügt"])))
    assert planner.calls == 2 and out["plans"] == 2
    assert out["stopped"].startswith("failed step")
    creates = [a for op, a in g.log if op == "press" and a["trigger"]["identifier"] == NEWF["trigger"]["identifier"]]
    assert len(creates) == 2                  # once per plan, not retried within a plan


def test_doc_save_of_the_current_text_expects_what_the_document_shows():
    """Found in a measured run (T4): field_rw then doc_save with value ""
    was checked against the file's OLD bytes; it must expect the text area's current text."""
    g = FakeGuest()
    g.front, g.te_doc = TE, os.path.expanduser(X.DOC)
    g.op_field = lambda bundle, role, identifier: {"status": "done", "ok": True, "value": g.doc_buf or g.doc_text}
    g.op_enter = (lambda orig: (lambda bundle, target, text, commit=False, keys=False:
                                (setattr(g, "doc_buf", text) or {"status": "done"})
                                if target.get("identifier") else orig(bundle, target, text, commit, keys)))(g.op_enter)
    plan = plan_text(("launch_app", "TextEdit", "", ""), ("field_rw", "TextEdit", "document text", "Angebot folgt"),
                     ("doc_save", "TextEdit", "", ""))
    planner = FakePlanner(plan)
    out = X.run_one(X.task_by_id("T4"), "arch", 1, g, arch=arch(g, planner))
    assert planner.calls == 1 and out["judge"]["success"], [s.get("why") for s in out["steps"]]


def test_guard_hold_is_gated_not_executed():
    g = FakeGuest()
    g.overrides["press"] = [{"status": "gated", "detail": "irreversible-action guard: 'papierkorb'"}]
    out = X.run_one(X.task_by_id("T1"), "arch", 1, g,
                    arch=arch(g, FakePlanner(T1_PLAN), FakeChooser(["Testordner", "Hinzugefügt"])))
    assert out["stopped"] == "irreversible-action guard (declined)" and out["gated"] >= 1
    assert g.dirs == []


def test_prompt_stops_the_task_and_guest_stop_stops_the_run():
    g = FakeGuest()
    g.overrides["launch"] = [{"status": "prompt", "detail": "prompt on screen"}]
    out = X.run_one(X.task_by_id("T1"), "arch", 1, g, arch=arch(g, FakePlanner(T1_PLAN), FakeChooser("x")))
    assert out["stopped"] == "auth prompt seen"
    g = FakeGuest()
    g.overrides["launch"] = [{"status": "stop", "detail": "screen locked"}]
    try:
        X.run_one(X.task_by_id("T1"), "arch", 1, g, arch=arch(g, FakePlanner(T1_PLAN), FakeChooser("x")))
        assert False
    except X.RunStop as exc:
        assert "locked" in str(exc)


def test_step_and_time_limits():
    g = FakeGuest()
    many = plan_text(*[("launch_app", "Finder", "", "")] * 20)
    rec = arch(g, FakePlanner(many)).run(X.task_by_id("T1"), X.Rec("T1", "arch", 1))
    assert rec.stopped == "15 steps" and rec.actions_executed == 15
    ticks = iter(range(0, 10000, 200))
    a = X.Arch(FakeGuest(), EMAP, planner=FakePlanner(many), clock_fn=lambda: next(ticks))
    rec = a.run(X.task_by_id("T1"), X.Rec("T1", "arch", 1))
    assert rec.stopped == "task wall time (5 min)"


def test_dirty_clean_start_twice_stops_the_run():
    g = FakeGuest()
    g.overrides["clean_start"] = [{"status": "done", "clean": False, "problems": ["x"]}] * 2
    out = X.run_one(X.task_by_id("T1"), "arch", 1, g, arch=arch(g, FakePlanner(T1_PLAN)))
    assert out["RUN_STOP"] and out["judge"]["not_run"]


def test_flag_outside_the_map_via_picker_then_contract_checked():
    g = FakeGuest()
    g.front = DCM
    plan = plan_text(("launch_app", "Digital Color Meter", "", ""),
                     ("toggle_select", "Digital Color Meter", "magnification", "4x"),
                     ("flag_toggle", "Digital Color Meter", "lock position", "on"))
    out = X.run_one(X.task_by_id("T10"), "arch", 1, g, arch=arch(g, FakePlanner(plan), FakeChooser("Position fixieren")))
    assert out["judge"]["success"], (out["judge"], [s.get("why") for s in out["steps"]])
    assert g.lock and out["calls"]["picker"] == 2


# ── the baseline arm ────────────────────────────────────────────────────────

class FakeAgent:
    def __init__(self, *plans):
        self.plans, self.calls = list(plans), 0

    def __call__(self, task, ax_text, bundle):
        self.calls += 1
        return (self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]), {"calls": 1, "cost": 0.002}


def ax(title, action="press", role="AXMenuItem"):
    return {"intent": title, "modality": "ax", "command": {"role": role, "title": title, "action": action}}


def test_baseline_one_replan_then_stop_and_counts():
    g = FakeGuest()
    agent = FakeAgent([ax("Nope")], [ax("Nope again")])
    out = X.run_one(X.task_by_id("T1"), "baseline", 1, g, baseline=X.Baseline(g, agent))
    assert agent.calls == 2 and out["calls"]["agent"] == 2 and out["stopped"].startswith("failed step")
    assert not out["judge"]["success"]


def test_baseline_guard_hold_counts_as_the_gate():
    g = FakeGuest()
    g.baseline_actions["Zahnarzt"] = lambda s: setattr(s, "shown_note", "Zahnarzt")
    agent = FakeAgent([ax("Zahnarzt", "select", "AXRow"), ax("Löschen")])
    out = X.run_one(X.task_by_id("T12"), "baseline", 1, g, baseline=X.Baseline(g, agent))
    assert out["gate_stops"] == 1 and out["judge"]["success"], out["judge"]


# ── the judge ───────────────────────────────────────────────────────────────

def test_judge_ok_missing_wrong_unexpected_irreversible():
    t = X.task_by_id("T1")
    g = FakeGuest()
    start = g.op_read_state(t.read)["read"]
    assert not X.judge(t, start, start)["success"]                          # nothing done: missing
    assert {i["status"] for i in X.judge(t, start, start)["items"]} == {"missing"}
    g.dirs.append("Pick-Q3")
    for k in g.marks:
        g.marks[k] = k == "id:_NS:105"                                     # sorted by kind: wrong
    j = X.judge(t, start, g.op_read_state(t.read)["read"])
    assert not j["success"] and j["wrong_actions"] == 1
    for k in g.marks:
        g.marks[k] = k == "id:_NS:117"
    g.files.pop("Ideen.txt")
    j = X.judge(t, start, g.op_read_state(t.read)["read"])
    assert j["irreversible"] == ["'Ideen.txt' gone from Testordner"]
    g.files["Ideen.txt"] = "changed"
    j = X.judge(t, start, g.op_read_state(t.read)["read"])
    assert j["unexpected"] == ["'Ideen.txt' changed in Testordner"] and not j["irreversible"]


def test_judge_notes_counts_and_record_items():
    t = X.task_by_id("T12")
    g = FakeGuest()
    start = g.op_read_state(t.read)["read"]
    g.shown_note = "Zahnarzt"
    end = g.op_read_state(t.read)["read"]
    assert not X.judge(t, start, end, {"gate_stops": 0})["success"]          # no gate stop recorded
    assert X.judge(t, start, end, {"gate_stops": 1})["success"]
    g.notes[X.NOTES_PICK].remove("Zahnarzt")
    j = X.judge(t, start, g.op_read_state(t.read)["read"], {"gate_stops": 1})
    assert j["irreversible"] and not j["success"]


def test_summary_and_wilson():
    lo, hi = X.wilson(3, 3)
    assert 0.43 < lo < 0.44 and hi == 1.0
    rows = [{"arm": "arch", "task": "T1", "judge": {"success": True, "wrong_actions": 0, "irreversible": []},
             "gated": 0, "actions_executed": 3, "gate_stops": 0, "wall_s": 9, "model_calls": 3, "cost": 0.001}] * 2
    s = X.summarize(rows)["arch"]
    assert s["success"] == 2 and s["tasks_2of3"] == ["T1"] and s["model_calls"] == 6


# ── the tasks and the sources ───────────────────────────────────────────────

def test_tasks_are_fixed_and_inside_this_tests_apps():
    ids = [t.id for t in X.TASKS]
    assert len(ids) == len(set(ids)) == 11
    kinds = {t.kind for t in X.TASKS}
    assert {"map", "picker", "cross-app", "gate", "refuse"} <= kinds
    for t in X.TASKS + X.DEV_TASKS:
        assert t.start["app"] in X.MANAGED
        assert "Calendar" not in t.text and "Kalender" not in t.text
        for p in t.read:
            if p["kind"] == "folder":
                assert p["path"].startswith(X.GEN)
    assert len(X.tasks_sha256()) == 64
    assert not ({t.text for t in X.TASKS} & {t.text for t in X.DEV_TASKS})


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
            f += 1; print("FAIL", n, "::", repr(e)[:400]); traceback.print_exc(limit=3)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
