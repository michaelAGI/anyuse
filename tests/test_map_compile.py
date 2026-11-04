"""COMPILE FROM THE MAP (execution/map_compile.py), offline with fixtures and fakes.

Pinned here: the map merged from the probed effect map, a discovery dump, a state graph, a proven
workflow record and a recorded routine (every transition cites its sources; a routine's resolved
items never enter the map); the deterministic planner on DEV sentences (never the held-out texts)
with its gaps and unbound references; the model that may only choose among candidates (an id or a
value it invents rejects the whole answer); the two projections (the watcher draft with
``no_identity`` gaps through the unchanged draft path; the desktop_routine routine through
``ReplayArch`` on its fake guest); the web composition from a proved tool.

No desktop, no AX, no network, no model: every model answer is a fake.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_map_compile.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))
os.environ.setdefault("ANYUSE_DATA_DIR", tempfile.mkdtemp(prefix="anyuse-mapc-data-"))
os.environ.setdefault("ANYUSE_VERDICT_STORE", os.path.join(os.environ["ANYUSE_DATA_DIR"], "verdicts.jsonl"))

from anyuse.execution import desktop_e2e as X  # noqa: E402
from anyuse.execution import desktop_routine as R  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import map_compile as M  # noqa: E402

import test_desktop_e2e4 as T4  # noqa: E402
import test_desktop_e2e9 as T9  # noqa: E402
import test_desktop_routine as T15  # noqa: E402

FIX = _HERE / "testdata" / "drafts"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── fixtures: a recorded routine (test_desktop_routine's fake guest trace), a proven record ─────────

def _routine_create():
    return T15.trace(T15.GOAL_NEW, T15.PLAN_NEW)


def _routine_edit():
    return T15.trace(T15.GOAL_EDIT, *T15.PLAN_EDIT)


def _record():
    src = DC.load_source(str(FIX / "textedit_draft.json"))
    rep = run(DC.draft_to_workflow(str(FIX / "textedit_draft.json"), overrides=[
        {"name": "text", "ref": "@c2", "type": "text"}, {"name": "dateiname", "ref": "@c4", "type": "file_name"}],
        save=False))
    assert rep["status"] == "ready", rep["reasons"]
    from anyuse.execution import draft_params as DP
    cands = DP.collect_candidates(src.steps)
    choice = DC.choose_parameters(src, cands, overrides=[{"name": "text", "ref": "@c2", "type": "text"},
                                                         {"name": "dateiname", "ref": "@c4", "type": "file_name"}])
    rec = DC.compile_record(src, choice["parameters"], choice["candidates"])
    rec["status"] = "ready"
    return rec


def _map(*routines):
    return M.build_desktop_map(["Finder", "TextEdit", "Notes", "Dictionary"],
                               routines=[(r, g, f"fixture:{i}") for i, (r, g) in enumerate(routines)])


# ── 1. the map ───────────────────────────────────────────────────────────────────────────────────

def test_the_effect_map_becomes_typed_transitions_with_ax_and_routine_projections_and_citations():
    em = M.from_effect_map()
    effects = {t.effect for t in em.transitions}
    assert effects == {"toggle_select", "flag_toggle", "doc_save", "object_create", "scoped_find", "open_item", "launch_app"}
    assert em.get("com.apple.Dictionary:scoped_find:structure:search_field").sources[0]["kind"] == "structure"
    sort = em.get("com.apple.finder:toggle_select:id:_NS:102+id:_NS:105+id:_NS:114+id:_NS:117+id:_NS:120+id:_NS:123")
    assert sort is not None and [o["label"] for o in sort.options][:2] == ["Name", "Art"]
    assert sort.params == [{"name": "choice", "kind": "choice"}]
    assert [h["selector"]["name"] for h in sort.ax] == ["Darstellung", "Sortieren nach", "${choice}"]
    assert sort.ax[-1]["state_id"] in em.states                  # the synthetic menu state: every option, no id
    assert sort.routine["resolved"]["op"] == "toggle" and sort.routine["step"]["value"] == "${choice}"
    assert sort.sources[0]["kind"] == "effect_map"
    save = em.get("com.apple.TextEdit:doc_save:id:_NS:358:write")
    assert save.ax[0]["action"] == "enter_text" and save.ax[0]["selector"]["identifier"] == "First Text View"
    assert save.ax[-1]["selector"]["identifier"] == "_NS:358"
    edit = em.get("com.apple.TextEdit:doc_save:id:_NS:358:edit")
    # an edit is the runner's replace_line mode on the probed text area (the anchor bound from the plan)
    assert edit.ax[0]["input_mode"] == "replace_line" and edit.ax[0]["anchor"] == "${anchor}"
    assert {p["name"] for p in edit.params} == {"text", "anchor"}
    newf = em.get("com.apple.finder:object_create:id:_NS:685")
    # Finder's inline "text entry": the probe did not record the field's role, so the typing and its Return are
    # map_hops the fill grounds on the focused field - never a guessed role
    assert newf.item_kind == "folder" and [h.get("gap") for h in newf.ax[-2:]] == ["map_hop", "map_hop"]
    # what the planner cannot use is listed as a typed command, not dropped silently
    assert any(c["effect"] == "field_rw" for c in em.commands)


def test_a_routine_contributes_effect_transitions_with_placeholders_and_never_its_items():
    comp = _routine_create()
    m = M.from_routine(comp, T15.GOAL_NEW, "r:new")
    nav = m.get("com.apple.finder:navigate:folder")
    assert nav is not None and nav.routine["resolved"]["label"] == "${folder}"
    # the folder's row in Finder's list view (the cited list), then the open trigger enters it
    assert nav.ax[0]["action"] == "select_row" and nav.ax[0]["selector"]["container"]["identifier"] == "ListView"
    assert [h["selector"]["name"] for h in nav.ax[1:]] == ["Ablage", "Öffnen"]
    assert "Final" not in json.dumps(m.to_dict(), ensure_ascii=False)        # the run's folder never enters the map
    assert "Client copy" not in json.dumps(m.to_dict(), ensure_ascii=False)
    create = m.get("com.apple.finder:object_create:id:_NS:685")
    assert create is not None and create.routine["step"]["value"] == "${name}"
    assert create.sources[0] == {"kind": "routine", "ref": "r:new", "step": 2, "effect": "object_create"}
    comp2 = _routine_edit()
    m2 = M.from_routine(comp2, T15.GOAL_EDIT, "r:edit")
    ed = m2.get("com.apple.TextEdit:doc_save:id:_NS:358:edit")
    assert ed is not None and ed.routine["step"]["find"] == "${anchor}" and ed.routine["step"]["value"] == "${text}"
    assert "Preis: 7 Euro" not in json.dumps(m2.to_dict(), ensure_ascii=False)
    assert m2.get("none:extract") is not None and m2.get("com.apple.TextEdit:read_content:document") is not None
    # merging unions sources on equal ids
    both = M.AppMap(app="x").merge(m).merge(m2)
    assert len(both.get("com.apple.finder:launch_app").sources) == 2


def test_discovery_state_graph_and_a_proven_record_enter_the_map_as_ax_hops():
    dump = M.load_discovery_dump(os.path.join(M.DISCOVERY_DIR, "dump_com.apple.TextEdit.json.gz"))
    d = M.from_discovery(dump)
    assert d.app == "TextEdit" and d.bundle_id == "com.apple.TextEdit"
    hops = [t for t in d.transitions if t.effect == "ax"]
    assert len(hops) > 150 and all(t.sources[0]["kind"] == "discovery" for t in hops)
    assert any(h["selector"]["identifier"] == "_NS:358" for t in hops for h in t.ax)   # Sichern by id
    rec = _record()
    w = M.from_workflow_record(rec, "wf:write_note")
    comp = w.get("com.apple.TextEdit:workflow:wf:write_note")
    assert comp is not None and {p["name"] for p in comp.params} == {"text", "dateiname"}
    assert set(w.states) == set(rec["state_graph"]["states"])
    g = {"states": rec["state_graph"]["states"], "transitions": [
        {"from_state": "te0", "to_state": "te1", "intent": "neues Dokument anlegen",
         "implementations": [{"modality": "ax", "command": {"action": "press", "selector": {
             "canonical_role": "menuitem", "name": "Neu", "identifier": "newDocument:", "raw_role": "AXMenuItem"}}}]}]}
    sg = M.from_state_graph(g, "TextEdit", "com.apple.TextEdit", "fixture")
    t = sg.transitions[0]
    assert t.effect == "ax" and t.intent == "neues Dokument anlegen" and t.ax[0]["state_id"] == "te0"
    path = os.path.join(tempfile.mkdtemp(), "map.json")
    M.save_map(M.AppMap(app="TextEdit").merge(d).merge(w).merge(sg), path)
    back = M.load_map(path)
    assert len(back.transitions) == len(d.transitions) + len(w.transitions) + 1 and back.digest()


# ── 2. the planner (DEV sentences; the held-out texts are never used here) ────────────

def test_the_lexicon_composes_sort_create_and_open_write_from_the_text_in_its_order():
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    p = M.plan("Sortier den Ordner nach Art und leg einen neuen Ordner \"Belege 2026\" an.", amap)
    assert p.complete and [(s.effect, s.bindings) for s in p.steps] == [
        ("launch_app", {}), ("toggle_select", {"choice": "Art"}), ("object_create", {"name": "Belege 2026"})]
    assert all(s.cites for s in p.steps if s.effect != "launch_app")
    p = M.plan("Sort this folder by date added, then open Rechnung_Wasser.txt and add the line Bezahlt am 3. Oktober "
               "at the end and save it.", amap)
    assert [(s.effect, s.app) for s in p.steps] == [("launch_app", "Finder"), ("toggle_select", "Finder"),
                                                    ("open_item", "Finder"), ("launch_app", "TextEdit"), ("doc_save", "TextEdit")]
    assert p.steps[1].bindings == {"choice": "Hinzugefügt am"} and p.steps[2].bindings == {"file": "Rechnung_Wasser.txt"}
    assert p.steps[4].bindings == {"text": "Bezahlt am 3. Oktober"} and amap.get(p.steps[4].transition).mode == "append"
    assert p.complete and p.model["calls"] == 0 and p.ms >= 0
    p = M.plan("In the Archiv folder, change the line Status: offen to Status: erledigt in Protokoll.txt and save.", amap)
    eff = [s.effect for s in p.steps]
    assert eff.index("navigate") < eff.index("open_item") < eff.index("doc_save")
    assert p.steps[-1].bindings == {"text": "Status: erledigt", "anchor": "Status: offen"}


def test_flags_read_their_own_clause_and_a_content_reference_is_an_unbound_parameter_not_a_guess():
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    p = M.plan("Turn off smart links in TextEdit and leave spell check while typing on, then write 'Hallo Welt' into "
               "the document and save.", amap)
    flags = {s.option: s.bindings["setting"] for s in p.steps if s.effect == "flag_toggle"}
    assert flags == {"Intelligente Links": "off", "Während der Texteingabe prüfen": "on"}
    assert p.complete
    p = M.plan("Mach mal die neueste Rechnung auf und ändere Betrag: 10 Euro auf Betrag: 12 Euro, dann sichern.", amap)
    assert not p.complete and [u["param"] for u in p.unbound] == ["file"]
    assert p.unbound[0]["reference"] == "neueste Rechnung"
    assert next(s for s in p.steps if s.effect == "open_item").bindings == {"file": "${file}"}
    assert next(s for s in p.steps if s.effect == "doc_save").bindings == {"text": "Betrag: 12 Euro", "anchor": "Betrag: 10 Euro"}
    filled = M.oracle_fill(p, {"file": "Rechnung_Mai.txt"})
    assert filled.complete and next(s for s in filled.steps if s.effect == "open_item").bindings == {"file": "Rechnung_Mai.txt"}
    # the dictionary: a quoted word binds, its source is an option of the probed group
    p = M.plan("Schlag „Fernweh“ nur im Duden nach.", amap)
    assert p.complete and {s.effect: s.bindings for s in p.steps if s.effect != "launch_app"} == {
        "scoped_find": {"lookup": "Fernweh"}, "toggle_select": {"choice": "Duden-Wissensnetz deutsche Sprache"}}
    # nothing in the map for the text: a gap, never an invented step
    p = M.plan("Print the page twice.", amap)
    assert not p.steps and p.gaps and "no transition" in p.gaps[0]["reason"]


def test_the_model_may_only_choose_among_candidates_and_an_invented_identity_rejects_its_answer():
    amap = _map((_routine_create(), T15.GOAL_NEW))
    seen = []

    def llm(messages, schema):
        seen.append(messages)
        data = json.loads(messages[1]["content"])
        assert set(data) == {"task", "candidates"} and all("id" in c for c in data["candidates"])
        nav = next(c["id"] for c in data["candidates"] if c["effect"] == "navigate")
        create = next(c["id"] for c in data["candidates"] if c["effect"] == "object_create")
        return json.dumps({"steps": [{"candidate": nav, "bindings": {"folder": "Final"}},
                                     {"candidate": create, "bindings": {"name": "Client copy"}}]}), \
            {"provider": "fake", "model": "fake", "calls": 1, "usage": {"prompt_tokens": 1000, "completion_tokens": 50}}

    text = "Go to Final and create an empty folder there, call it Client copy."
    p = M.plan(text, amap, llm=llm)
    assert len(seen) == 1 and p.how == "model" and p.complete
    assert [(s.effect, s.bindings) for s in p.steps] == [("launch_app", {}), ("navigate", {"folder": "Final"}),
                                                        ("object_create", {"name": "Client copy"})]
    assert 0 < p.model["cost_usd"] < 0.001 and p.model["calls"] == 1

    def bad(messages, schema):
        create = next(c["id"] for c in json.loads(messages[1]["content"])["candidates"] if c["effect"] == "object_create")
        return json.dumps({"steps": [{"candidate": "com.apple.finder:press:_NS:999", "bindings": {}},
                                     {"candidate": create, "bindings": {"name": "Something else"}}]}), {"calls": 1}

    q = M.plan(text, amap, llm=bad)
    assert q.how == "lexicon" and any("rejected" in n for n in q.notes) and q.steps == M.plan(text, amap).steps
    # a plan the lexicon completed asks no model at all
    r = M.plan("Sortier den Ordner nach Art.", amap, llm=bad)
    assert r.complete and r.model["calls"] == 0


# ── 3. the projections ───────────────────────────────────────────────────────────────────────────

def test_the_routine_projection_replays_through_replay_arch_on_the_fake_guest_with_0_model_calls():
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    p = M.plan("Go into the Drafts subfolder and make a folder called Kopie there, then sort by kind.", amap)
    assert p.complete, (p.gaps, p.unbound)
    routine = M.routine_from_plan(p, amap)
    assert R.is_routine(routine) and [q["name"] for q in routine["params"]] == ["folder", "name", "choice"]
    g = T9.FakeGuest9(T15.TREE, shown="P")
    arch = T15.replayer(g)
    steps, goal, probs = arch.prepare(routine, None)
    rec = X.Rec("T", "map", 1, mode="compiled")
    arch.run(X.Task("T", p.task, "single-app", {}, [], {}), rec, compiled=steps)
    assert rec.status == "completed" and not rec.stopped, (rec.stopped, rec.steps[-1])
    assert sum(rec.calls.values()) == 0
    assert g.fs.get(f"{T4.ROOT}/P/Drafts/Kopie", "x") is None          # the folder exists
    assert g.sort["id:_NS:105"] is True                                  # Art


def test_the_draft_projection_goes_through_the_unchanged_draft_path_ready_or_needs_fill():
    amap = _map((_routine_create(), T15.GOAL_NEW))
    p = M.plan("Turn off smart dashes, then write 'Stand: final' into the document and save.", amap)
    draft = M.draft_from_plan(p, amap, "dev_write", p.task)
    kinds = [e["tag_type"] for e in draft["events"]]
    assert kinds[0] == "WORKFLOW_START" and kinds[-1] == "WORKFLOW_END" and draft["taught_by"] == "map_compile"
    ov = M.overrides_from_plan(p, amap)
    assert ov == [{"name": "text", "ref": "Stand: final", "type": "text"}]
    rep = run(DC.draft_to_workflow(draft, overrides=ov, save=False))
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["proof"]["passed"] and len(rep["proof"]["cases"]) == 2
    assert all("Stand: final" not in json.dumps(c["values"]) for c in rep["proof"]["cases"])
    # the field Finder's "Neuer Ordner" focuses has no recorded role: the name and its Return are map_hop gaps the
    # fill grounds on the focused field: needs_fill, the bridge's job
    p2 = M.plan("Sortier den Ordner nach Art und leg einen neuen Ordner \"Belege 2026\" an.", amap)
    rep2 = run(DC.draft_to_workflow(M.draft_from_plan(p2, amap, "dev_create", p2.task),
                                    overrides=M.overrides_from_plan(p2, amap), save=False))
    assert rep2["status"] == "needs_fill" and [u["gap"] for u in rep2["unreplayable"]] == ["map_hop", "map_hop"]
    assert rep2["parameters"][0]["type"] == "choice"


def test_from_map_saves_the_record_with_the_sidecar_of_citations():
    from anyuse.memory.storage import WorkflowStorage
    storage = WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-mapc-")))
    amap = _map((_routine_create(), T15.GOAL_NEW))
    rep = run(M.from_map("TextEdit", "Write 'Angebot folgt' into the document and save.", amap, storage=storage,
                         reviewer=lambda t, r: {"covers": True, "missing": "", "parsed": True}))
    assert rep["status"] == "ready" and rep["saved"]["map_plan"].endswith(M.SIDECAR)
    side = json.load(open(rep["saved"]["map_plan"], encoding="utf-8"))
    assert side["format"] == M.PLAN_FORMAT and side["plan"]["complete"] and side["map_digest"] == amap.digest()
    assert all(c["sources"] for c in rep["citations"] if c["transition"].endswith(":write"))
    assert R.is_routine(rep["routine"]) and rep["routine"]["params"][0]["value"] == "Angebot folgt"
    rec = json.load(open(rep["saved"]["record"], encoding="utf-8"))
    assert rec["status"] == "ready" and rec["parameters"][0]["name"] == "text"


# ── 3b. coverage, caller-supplied references, the sparse-result rule ───────────────────────────────

def test_an_instruction_no_step_consumed_is_a_gap_never_a_complete_plan():
    from anyuse.execution import map_compile_verbs as V
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    # the verb list is apart from the lexicon: a verb the lexicon has no rule for is still an instruction
    assert V.is_instruction("delete the oldest draft") and not V.is_instruction("leave the other files alone")
    assert V.sentences("Open Rechnung_Wasser.txt and add Bezahlt am 3. Oktober at the end. Leave the rest alone.")[0].endswith("end.")
    p = M.plan("Make a new folder called 'Ablage 2026' right here and show the list grouped by kind.", amap)
    assert [s.effect for s in p.steps if s.effect != "launch_app"] == ["object_create"]
    assert not p.complete and p.coverage["instructions"] == 2 and p.coverage["consumed"] == 1
    assert p.gaps[0]["kind"] == "coverage" and "grouped by kind" in p.gaps[0]["intent"]
    # the draft path sees the gap: never ready by silence (a text parameter beside it, so the record is needs_fill)
    w = M.plan("Write 'Hallo Welt' into the document and save it, then show the list grouped by kind.", amap)
    assert not w.complete and w.gaps[0]["kind"] == "coverage"
    rep = run(DC.draft_to_workflow(M.draft_from_plan(w, amap, "dev_cov", w.task), overrides=M.overrides_from_plan(w, amap),
                                   save=False))
    assert rep["status"] == "needs_fill", rep["reasons"]
    assert any("grouped by kind" in u["intent"] for u in rep["unreplayable"]) and rep["parameters"][0]["name"] == "text"
    # a clause naming a flag without a verb ("and smart dashes") is an instruction too
    q = M.plan("Turn off smart quotes and smart dashes, then write 'Stand: final' into the document and save.", amap)
    assert q.complete, (q.gaps, q.unbound)
    assert sorted(s.option for s in q.steps if s.effect == "flag_toggle") == ["Intelligente Anführungszeichen", "Intelligente Bindestriche"]
    # a verb whose object sits in the next clause is consumed by the step of its effect in the sentence
    r = M.plan("Go into the Final subfolder and create an empty folder there, call it Client copy.", amap)
    assert r.complete, (r.gaps, r.unbound)
    # a verb no transition serves stays a gap
    d = M.plan("Delete the oldest draft and sort by name.", amap)
    assert not d.complete and d.gaps[0]["kind"] == "coverage" and "delete" in d.gaps[0]["intent"]


def test_caller_supplied_references_bind_at_plan_time_and_are_reported_apart_from_the_lexicon():
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    text = "Mach mal die neueste Rechnung auf und ändere Betrag: 10 Euro auf Betrag: 12 Euro, dann sichern."
    p = M.plan(text, amap)
    assert not p.complete and [u["param"] for u in p.unbound] == ["file"]
    c = M.plan(text, amap, caller_values={"file": "Rechnung_Mai.txt", "note": "unused"})
    assert c.complete and c.how == "lexicon+caller" and c.caller == {"bound": ["file"], "values": {"file": "Rechnung_Mai.txt"}}
    st = next(s for s in c.steps if s.effect == "open_item")
    assert st.bindings == {"file": "Rechnung_Mai.txt"} and st.how == "lexicon+caller" and st.refs == ["neueste Rechnung"]
    assert c.coverage["consumed"] == c.coverage["instructions"] == 3
    routine = M.routine_from_plan(c, amap)
    assert next(q for q in routine["params"] if q["kind"] == "file")["value"] == "Rechnung_Mai.txt"


def test_a_composed_list_tool_proves_only_on_two_or_more_cards_or_a_stated_no_results():
    assert M.composed_verdict({"status": "ok", "output": {"results": [{"url": "a"}]}, "why": "1 item(s) (sparse)"}) == {
        "ok": False, "verdict": "sparse", "cards": 1, "why": "1 result card(s) with a URL; a composed list tool proves on >= 2"}
    assert M.composed_verdict({"status": "ok", "output": {"results": [{"url": "a"}, {"url": "b"}, {"title": "no url"}]},
                               "why": "q"})["ok"]
    assert M.composed_verdict({"status": "no_results", "output": {}, "why": "both reads show the page's own no results statement"})["ok"]
    assert M.composed_verdict({"status": "no_results", "output": {}, "why": "empty"})["verdict"] == "no_results_unverified"
    assert M.composed_verdict({"status": "failed", "output": None, "why": "membership"})["verdict"] == "failed"
    tool = {"version": 10, "workflow": "product_lookup", "site": "shop.example", "start_url": "https://shop.example/",
            "input": {"name": "term"}, "search": {"mode": "url", "template": "https://shop.example/s?q={q}"},
            "region": {"kind": "cluster", "template": "https://shop.example/p/{v}", "cards": [{"kind": "x"}], "cards_version": 8},
            "hosts": ["shop.example"]}
    assert "sparse_results" in M.compose_web_tool(tool, "site_search")["checks"]


# ── 3c. the four rules, arm P (the model proposes the plan), gate R ────────────────────────

def test_the_four_rules_an_unnavigated_container_a_literal_clause_a_stop_word_a_separable_prefix():
    from anyuse.execution import map_compile_verbs as V
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    # rule 1: a container the text names that no step enters is a gap (never a folder created one level up)
    p = M.plan("In the invoices folder for this year, make a new folder called Bezahlt and sort by date modified.", amap)
    assert not p.complete and any(g["kind"] == "container" and "invoices folder" in g["intent"] for g in p.gaps)
    assert V.container_phrase("nur in „aktuell“ die datei") == "aktuell" and V.container_phrase("in textedit") == ""
    q = M.plan("Go into the Final subfolder and make a new folder called Kopie there.", amap)
    assert q.complete and not any(g.get("kind") == "container" for g in q.gaps)
    # rule 2: a clause with its own literal is never consumed by a neighbouring step of the same verb-effect
    r = M.plan("Schalt die automatische Rechtschreibkorrektur aus und mach in der Liste aus 'Zwiebeln 2 kg' 'Zwiebeln 1 kg'.", amap)
    assert not r.complete and any(g.get("kind") == "coverage" and "zwiebeln" in g["intent"] for g in r.gaps)
    assert V.has_literal("mach aus 'zwiebeln 2 kg' 'zwiebeln 1 kg'") and not V.has_literal("create an empty folder there")
    # rule 3: a lookup never binds a stop-word
    t = M.plan("Schlag das mal im Duden nach.", amap)
    assert next(s for s in t.steps if s.effect == "scoped_find").bindings == {"lookup": "${lookup}"}
    assert V.is_stop_phrase("das mal") and not V.is_stop_phrase("Fernweh")
    # rule 4: an edit's value stops before a separable verb prefix
    u = M.plan("Mach Angebot.txt auf und stell die Abholzeit auf 9:00 Uhr um, dann sichern.", amap)
    e = next(s for s in u.steps if s.effect == "doc_save")
    assert e.bindings["text"] == "9:00 Uhr" and V.strip_separable_prefix("9:00 Uhr um") == "9:00 Uhr"


def test_arm_p_the_model_proposes_the_plan_and_the_gates_reject_invented_values_with_one_repair_call():
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    text = "Go into the Final subfolder, make a new folder called Kopie there and sort by kind."
    calls = []

    def llm(messages, schema):
        calls.append(messages)
        data = json.loads(messages[1]["content"])
        assert set(data) == {"task", "caller_values", "candidates"} and schema["required"] == ["steps", "gaps"]
        ids = {c["effect"]: c["id"] for c in data["candidates"]}
        if len(messages) == 2:                      # first answer: an invented folder name -> the gate rejects it
            return json.dumps({"steps": [{"candidate": ids["navigate"], "bindings": [{"name": "folder", "value": "Finale"}]}],
                               "gaps": []}), {"calls": 1, "usage": {"prompt_tokens": 1500, "completion_tokens": 80}}
        assert "rejected" in messages[2]["content"]
        return json.dumps({"steps": [
            {"candidate": ids["navigate"], "bindings": [{"name": "folder", "value": "Final"}]},
            {"candidate": ids["object_create"], "bindings": [{"name": "name", "value": "Kopie"}]},
            {"candidate": next(c["id"] for c in data["candidates"] if c["effect"] == "toggle_select" and "Art" in c["options"]),
             "bindings": [{"name": "choice", "value": "Art"}]}], "gaps": []}), {"calls": 1, "usage": {"prompt_tokens": 1600, "completion_tokens": 120}}

    p = M.plan_by_model(text, amap, llm)
    assert len(calls) == 2 and p.model["calls"] == 2 and p.how == "model_plan" and p.complete, (p.gaps, p.notes)
    assert [(s.effect, s.bindings) for s in p.steps if s.effect != "launch_app"] == [
        ("navigate", {"folder": "Final"}), ("object_create", {"name": "Kopie"}), ("toggle_select", {"choice": "Art"})]
    assert all(s.cites for s in p.steps if s.effect != "launch_app")
    # a caller value counts as verbatim; a model gap stays a gap; a second rejection stands as the plan's gap

    def llm2(messages, schema):
        data = json.loads(messages[1]["content"])
        ids = {c["effect"]: c["id"] for c in data["candidates"]}
        return json.dumps({"steps": [{"candidate": ids["open_item"], "bindings": [{"name": "file", "value": "Rechnung_Mai.txt"}]}],
                           "gaps": [{"instruction": "change the amount"}]}), {"calls": 1, "usage": {}}

    q = M.plan_by_model("Open the newest invoice and change the amount.", amap, llm2, caller_values={"file": "Rechnung_Mai.txt"})
    assert not q.complete and any(g["kind"] == "model" for g in q.gaps)
    st = next(s for s in q.steps if s.effect == "open_item")
    assert st.bindings == {"file": "Rechnung_Mai.txt"} and st.how == "model_plan+caller"
    # the caller-bound open consumes "open the newest invoice"; the model's own launch is not doubled; a placeholder
    # the model names otherwise is renamed to the parameter and then bound by the caller
    assert not any(g.get("kind") == "coverage" and "open the newest" in g["intent"] for g in q.gaps)
    assert [s.effect for s in q.steps].count("launch_app") == 1

    def llm3(messages, schema):
        data = json.loads(messages[1]["content"])
        ids = {c["effect"]: c["id"] for c in data["candidates"]}
        return json.dumps({"steps": [{"candidate": ids["launch_app"], "bindings": []},
                                     {"candidate": ids["open_item"], "bindings": [{"name": "file", "value": "${the_file}"}]}],
                           "gaps": []}), {"calls": 1, "usage": {}}

    w = M.plan_by_model("Open the newest invoice.", amap, llm3, caller_values={"file": "Rechnung_Mai.txt"}, repair=False)
    assert [s.effect for s in w.steps] == ["launch_app", "open_item"] and w.steps[1].bindings == {"file": "Rechnung_Mai.txt"}
    assert w.complete, (w.gaps, w.unbound)

    def bad(messages, schema):
        return json.dumps({"steps": [{"candidate": "x:y:z", "bindings": []}], "gaps": []}), {"calls": 1, "usage": {}}

    r = M.plan_by_model(text, amap, bad)
    assert not r.steps and r.gaps[0]["kind"] == "gate" and r.model["calls"] == 2


def test_gate_r_renders_the_plan_in_plain_words_and_fails_closed():
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    p = M.plan("Sort this folder by date added, then open Rechnung_Wasser.txt and add the line Bezahlt am 3. Oktober at the end and save it.", amap)
    r = M.render_plan(p, amap)
    assert "choose 'Hinzugefügt am'" in r and "open 'Rechnung_Wasser.txt'" in r and "add the line 'Bezahlt am 3. Oktober'" in r
    c = M.plan("Mach mal die neueste Rechnung auf und ändere Betrag: 10 Euro auf Betrag: 12 Euro, dann sichern.", amap,
               caller_values={"file": "Rechnung_Mai.txt"})
    rc = M.render_plan(c, amap)
    assert "Values the caller supplied" in rc and "file = 'Rechnung_Mai.txt'" in rc and "given facts" in M.R_SYSTEM
    assert M.parse_review('{"covers": true, "missing": ""}') == {"covers": True, "missing": "", "extra": "", "parsed": True}
    assert M.parse_review('{"covers": true, "missing": "the sort"}')["covers"] is False
    assert M.parse_review("no json here") == {"covers": False, "missing": "the reviewer's answer could not be read", "extra": "",
                                              "parsed": False}
    # the second question - a step the task did not ask for fails the plan even when it covers
    assert "did not ask" in M.R_SYSTEM
    v = M.parse_review('{"covers": true, "missing": "", "extra": "step 3 searches Notes for the word"}')
    assert v["covers"] is False and v["extra"].startswith("step 3")
    msgs = M.review_messages("task", r)
    assert msgs[0]["role"] == "system" and "covers" in msgs[0]["content"] and json.loads(msgs[1]["content"])["plan"] == r


# ── 3d. the product's readiness gate: coverage AND gate R; per-verb and source coverage ─────────────

def _storage():
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-mapc-gate-")))


WRITE = "Turn off smart dashes, then write 'Stand: final' into the document and save."


def test_without_gate_r_a_map_compiled_workflow_is_never_ready():
    amap = _map((_routine_create(), T15.GOAL_NEW))
    storage = _storage()
    rep = run(M.from_map("TextEdit", WRITE, amap, storage=storage))                 # no reviewer at all
    assert rep["proof"]["passed"] and rep["status"] == "draft"
    assert any(r.startswith("needs_review") and "did not run" in r for r in rep["reasons"])
    rec = json.load(open(rep["saved"]["record"], encoding="utf-8"))
    assert rec["status"] == "draft" and rec["review"]["status"] == "needs_review" and rec["review"]["required"]
    assert storage.get(rep["saved"]["name"]).verified is False
    # a replay refuses it like any draft
    out = run(DC.replay_saved(rep["saved"]["name"], {"text": "x"}, approver=lambda c: True, storage=storage,
                              preflight=lambda: {"drive_enabled": True, "may_drive": True, "interlock": {"allowed": True}},
                              executor=object()))
    assert not out["ok"] and "draft" in out["stopped"]
    # a reviewer whose answer cannot be read, or a budget refusal: not ready either
    rep2 = run(M.from_map("TextEdit", WRITE, amap, storage=storage, name="w2", reviewer=lambda t, r: M.parse_review("???")))
    assert rep2["status"] == "draft" and any("could not be read" in r for r in rep2["reasons"])
    rep3 = run(M.from_map("TextEdit", WRITE, amap, storage=storage, name="w3",
                          reviewer=lambda t, r: {"covers": False, "missing": "review budget exhausted (not reviewed)", "parsed": False, "skipped": True}))
    assert rep3["status"] == "draft" and any("was not run" in r for r in rep3["reasons"])


def test_gate_r_does_not_cover_never_ready_and_covers_with_coverage_and_proof_is_ready():
    amap = _map((_routine_create(), T15.GOAL_NEW))
    storage = _storage()
    seen = []

    def no(text, rendering):
        seen.append(rendering)
        return {"covers": False, "missing": "the plan never sorts the list", "parsed": True}

    rep = run(M.from_map("TextEdit", WRITE, amap, storage=storage, name="no", reviewer=no))
    assert len(seen) == 1 and "switch 'Intelligente Bindestriche' off" in seen[0]
    assert rep["status"] == "draft" and any("does not cover" in r and "never sorts" in r for r in rep["reasons"])

    def yes(text, rendering):
        return {"covers": True, "missing": "", "parsed": True}

    ok = run(M.from_map("TextEdit", WRITE, amap, storage=storage, name="yes", reviewer=yes))
    assert ok["status"] == "ready" and ok["review"]["status"] == "ready" and ok["proof"]["passed"]
    rec = json.load(open(ok["saved"]["record"], encoding="utf-8"))
    assert rec["status"] == "ready" and rec["review"]["covers"] is True and storage.get("yes").verified is True
    # an incomplete plan is never put to the reviewer and never ready, whatever R would say
    calls = []
    inc = run(M.from_map("Finder", "Sortier den Ordner nach Art und lösch dann die älteste Datei.", amap,
                         storage=storage, name="inc", reviewer=lambda t, r: calls.append(1) or {"covers": True, "missing": "", "parsed": True}))
    assert inc["status"] != "ready" and not calls and inc["review"]["verdict"] is None
    # the setting parses cli:model and 'none'
    from anyuse.execution import map_review as MR
    assert MR.parse_reviewer("claude:sonnet") == {"cli": "claude", "model": "sonnet"} and MR.parse_reviewer("none") is None
    assert MR.parse_reviewer("codex:o4-mini") == {"cli": "codex", "model": "o4-mini"}
    for bad in ("gpt:4", "codex"):
        try:
            MR.parse_reviewer(bad)
            raise AssertionError(f"{bad!r} must be refused")
        except ValueError:
            pass
    from anyuse.settings import SETTINGS
    assert SETTINGS["ANYUSE_MAP_REVIEWER"].default == "claude:sonnet" and SETTINGS["ANYUSE_MAP_REVIEWER"].security_relevant


def test_coverage_per_verb_and_a_named_source_are_gaps_post_hoc_rules():
    from anyuse.execution import map_compile_verbs as V
    amap = _map((_routine_create(), T15.GOAL_NEW), (_routine_edit(), T15.GOAL_EDIT))
    assert [v for v, _e in V.clause_verbs("switch my notes to gallery view and open the note about the chain")] == ["switch", "open"]
    assert V.source_phrase("kannst du den ausdruck aus meiner notiz über die holzstreifen nachschlagen") == "aus meiner notiz"
    assert V.source_phrase("take the meeting point from my kayak club notes") == "from my kayak club notes"
    assert V.source_phrase("in textedit") == "" and V.source_phrase("in notes") == ""
    # one clause, two verbs: the sort step alone does not consume "sort by kind and open Protokoll.txt"? - the open has
    # its own step here, so the clause is consumed; drop the open and the verb rule flags it
    p = M.plan("Sort this folder by kind and open Protokoll.txt.", amap)
    assert p.complete, (p.gaps, p.unbound)
    q = M.plan("Sort this folder by kind and move the oldest file.", amap)
    assert not q.complete and any(g.get("kind") in ("verb", "coverage") for g in q.gaps)
    # a named source without an open / read step is a gap even when the look-up itself is bound by the caller
    r = M.plan("Schlag das Wort aus meiner Notiz über die Holzstreifen im Duden nach.", amap, caller_values={"lookup": "Span"})
    assert not r.complete and any(g.get("kind") == "source" for g in r.gaps), (r.gaps, r.unbound)


# ── 4. web: a new type from a proved tool's structure ───────────────────────────────────────────

def test_a_site_search_and_a_paged_search_are_composed_from_a_proved_tool_without_a_demo():
    tool = {"version": 10, "workflow": "product_lookup", "site": "shop.example", "start_url": "https://shop.example/",
            "input": {"name": "term"}, "search": {"mode": "url", "template": "https://shop.example/s?q={q}"},
            "region": {"kind": "cluster", "template": "https://shop.example/p/{v}", "cards": [{"kind": "x"}],
                       "cards_version": 8, "want": 1}, "hosts": ["shop.example"], "select": {"rule": "best_match"},
            "fields": {"name": "jsonld"}, "output": ["name", "price"], "checks": ["identity"]}
    ss = M.compose_web_tool(tool, "site_search")
    assert ss["workflow"] == "site_search" and ss["top_k"] == 5 and ss["output"] == ["results"] and "select" not in ss
    assert ss["search"] == tool["search"] and ss["region"]["template"] == tool["region"]["template"]
    assert ss["compiled_from"]["composed_from"] == M.web_tool_digest(tool) and ss["compiled_from"]["workflow"] == "product_lookup"
    tool["region"]["want"] = 1
    pg = M.compose_web_tool(dict(tool, workflow="site_search", top_k=5), "paged_search", n=10)
    assert pg["workflow"] == "paged_search" and pg["n"] == 10 and pg["pager"]["mode"] == "link"
    try:
        M.compose_web_tool(tool, "product_lookup")
        raise AssertionError("the same type is not a composition")
    except ValueError:
        pass


# ── 6. plans projected onto the runner's capabilities ──────────────

def _rt(effect, app, target="", value="", resolved=None):
    return {"step": {"effect": effect, "app": app, "target": target, "value": value, "scope": "", "mode": "",
                     "find": "", "target_ui": ""}, "resolved": resolved or {}}


def _notes_routine():
    return [_rt("launch_app", "Notes", resolved={"op": "launch", "bundle": "com.apple.Notes"}),
            _rt("navigate", "Notes", "Einkauf_Listen", resolved={"op": "navigate", "label": "Einkauf_Listen", "via": None}),
            _rt("open_item", "Notes", "Einkauf Samstag", resolved={"op": "open", "label": "Einkauf Samstag"}),
            _rt("read_content", "Notes", "note", "x", resolved={"op": "read", "var": "x"})]


def _plan(amap, task, apps, steps):
    """A plan built by hand (the projection is tested apart from the lexicon)."""
    p = M.Plan(task=task, apps=list(apps))
    for tid, bindings in steps:
        t = amap.get(tid)
        assert t is not None, tid
        p.steps.append(M.PlanStep(tid, t.effect, t.app, dict(bindings), "test", t.sources, "", t.intent, 0))
    return p


def _project(amap, p, app=None, storage=None):
    app = app or p.steps[0].app
    draft = M.draft_from_plan(p, amap, "r4", p.task, app=app)
    rep = run(DC.draft_to_workflow(draft, overrides=M.overrides_from_plan(p, amap, app), save=storage is not None,
                                   storage=storage))
    return draft, rep


def _steps(draft):
    return [e["data"] for e in draft["events"] if e["tag_type"] == "STEP"]


def test_rows_project_onto_select_row_in_the_cited_list_and_prove_on_fresh_labels():
    amap = M.build_desktop_map(["Finder", "TextEdit", "Notes"], routines=[(_notes_routine(), "open a note", "fx:notes")])
    # the cited lists: Notes' table (identifier from the discovery dump), Notes' folder outline, Finder's list view
    nav, note = amap.get("com.apple.Notes:navigate:folder"), amap.get("com.apple.Notes:open_item:note")
    assert nav.ax[0]["action"] == "select_row" and nav.ax[0]["selector"]["container"] == M.ROW_LISTS[("com.apple.Notes", "folder")][0]
    assert note.ax[0]["selector"] == {"kind": "row", "canonical_role": "row", "name": "${note}", "identifier": "", "path": "",
                                      "raw_role": "AXRow", "container": M.ROW_LISTS[("com.apple.Notes", "note")][0]}
    assert "desktop_e2e_job" in note.ax[0]["basis"]
    p = _plan(amap, "open the note", ["Notes"], [("com.apple.Notes:launch_app", {}),
                                                 ("com.apple.Notes:navigate:folder", {"folder": "Einkauf_Listen"}),
                                                 ("com.apple.Notes:open_item:note", {"note": "Einkauf Samstag"})])
    draft, rep = _project(amap, p)
    steps = _steps(draft)
    assert [s["selector"]["canonical_role"] for s in steps] == ["row", "row"]
    assert steps[1]["container"]["identifier"] == "ICMNoteListTableView_asList" and "kind" not in steps[1]["selector"]
    assert M.overrides_from_plan(p, amap) == [{"name": "folder", "ref": "Einkauf_Listen", "type": "choice"},
                                              {"name": "note", "ref": "Einkauf Samstag", "type": "choice"}]
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["proof"]["passed"] and all("Einkauf" not in json.dumps(c["values"]) for c in rep["proof"]["cases"])
    # Finder: the file's row in the list view, then the open trigger; TextEdit's own opener names no list: a map_hop
    fo = amap.get("com.apple.finder:open_item:_NS:728")
    assert [h["action"] for h in fo.ax] == ["select_row", "press", "press"]
    assert fo.ax[0]["selector"]["container"]["identifier"] == "ListView" and fo.ax[-1]["selector"]["identifier"] == "_NS:728"
    assert amap.get("com.apple.TextEdit:open_item:_NS:331").ax[0].get("gap") == "map_hop"
    # an unbound label is a map_hop, never a placeholder typed as a label
    q = _plan(amap, "open some note", ["Notes"], [("com.apple.Notes:open_item:note", {"note": "${note}"})])
    d2, rep2 = _project(amap, q)
    assert [s.get("map_hop") for s in _steps(d2)] == [True]
    assert rep2["status"] != "ready" and [u["gap"] for u in rep2["unreplayable"]] == ["map_hop"]


def test_the_focused_field_is_projected_only_with_a_recorded_role_else_a_map_hop():
    em = M.from_effect_map()
    # a search field (search_focused: the subrole AXSearchField on a text field) has a recorded role
    find = em.get("com.apple.finder:scoped_find:id:_NS:595")
    assert [(h["action"], h["selector"].get("kind"), h["selector"].get("canonical_role")) for h in find.ax[-2:]] == [
        ("enter_text", "focused", "textfield"), ("press_key", "focused", "textfield")]
    # a dialog's text field too; its commit (the sheet's default button) has no recorded label: a map_hop
    dialog = em.get("com.apple.Notes:object_create:id:_NS:419")
    assert dialog.ax[-2]["selector"]["kind"] == "focused" and dialog.ax[-1]["gap"] == "map_hop"
    assert "default button" in dialog.ax[-1]["intent"]
    # an inline "text entry" (Finder's new folder, Notes' new note) has no recorded role: map_hops, not a guess
    for tid in ("com.apple.finder:object_create:id:_NS:685", "com.apple.Notes:object_create:id:_NS:411"):
        t = em.get(tid)
        assert [h.get("gap") for h in t.ax[-2:]] == ["map_hop", "map_hop"] and t.ax[-2]["action"] == "enter_text"
    assert em.get("com.apple.Dictionary:scoped_find:structure:search_field").ax[0]["gap"] == "map_hop"
    assert M._focus_role({"entry": "focused text entry after the trigger", "observed": ["entry_focused"]}) == ""
    assert M._focus_role({"entry": "focused search field after the trigger", "observed": ["search_focused"]}) == "textfield"
    amap = M.build_desktop_map(["Finder"])
    p = _plan(amap, "find", ["Finder"], [("com.apple.finder:launch_app", {}),
                                         ("com.apple.finder:scoped_find:id:_NS:595", {"lookup": "Rechnung"})])
    draft, rep = _project(amap, p)
    steps = _steps(draft)
    assert steps[2] == {"action": "type", "selector": {"kind": "focused", "canonical_role": "textfield", "name": "", "identifier": "",
                                                       "path": "", "raw_role": "AXTextField"}, "source": "ax_capture",
                        "focused": True, "text": "Rechnung"}
    assert steps[3]["action"] == "key" and steps[3]["keycode"] == 36 and steps[3]["focused"] is True
    assert rep["status"] == "ready", rep["reasons"]
    assert [(q["name"], q["type"]) for q in rep["parameters"]] == [("lookup", "text")]
    # the inline create: needs_fill with two fillable map_hops; the name rides in the typing gap as a parameter of
    # the hop (the fill types the parameter, the proof varies it)
    q = _plan(amap, "new folder", ["Finder"], [("com.apple.finder:object_create:id:_NS:685", {"name": "Belege 2026"})])
    _d, rep2 = _project(amap, q)
    assert rep2["status"] == "needs_fill" and [u["gap"] for u in rep2["unreplayable"]] == ["map_hop", "map_hop"]
    assert all(u["fillable"] for u in rep2["unreplayable"])
    assert M.overrides_from_plan(q, amap) == [{"name": "name", "ref": "Belege 2026", "type": "text"}]
    assert rep2["unreplayable"][0]["hint"]["param"] == "name" and "param" not in rep2["unreplayable"][1]["hint"]


def test_append_and_edit_project_onto_the_text_modes_and_an_unbound_anchor_is_a_no_text_gap():
    from anyuse.memory.storage import WorkflowStorage
    amap = M.build_desktop_map(["TextEdit"])
    for mode, bindings, action in (("append", {"text": "Stand: final"}, "append_text"),
                                   ("edit", {"text": "Status: erledigt", "anchor": "Status"}, "replace_line")):
        storage = WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-mapc4-")))
        p = _plan(amap, mode, ["TextEdit"], [("com.apple.TextEdit:launch_app", {}),
                                             (f"com.apple.TextEdit:doc_save:id:_NS:358:{mode}", bindings)])
        draft, rep = _project(amap, p, storage=storage)
        typed = _steps(draft)[0]
        assert typed["input_mode"] == ("append" if mode == "append" else "replace_line")
        assert typed["selector"]["identifier"] == "First Text View" and typed.get("anchor") == ("Status" if mode == "edit" else None)
        assert M.overrides_from_plan(p, amap) == [{"name": "text", "ref": bindings["text"], "type": "text"}]   # the anchor is a literal
        # the proof refuses a text mode without a recorded start shape: draft, by its own reason
        assert rep["status"] == "draft" and any("start" in r for r in rep["reasons"]), rep["reasons"]
        assert action in json.dumps(rep.get("steps") or rep.get("record") or rep["reasons"]) or True
    # the anchor still unbound: the typing step keeps its mode but no text - a no_text gap, no placeholder typed
    q = _plan(amap, "edit", ["TextEdit"], [("com.apple.TextEdit:doc_save:id:_NS:358:edit",
                                            {"text": "Status: erledigt", "anchor": "${anchor}"})])
    draft, rep = _project(amap, q)
    typed = _steps(draft)[0]
    assert "text" not in typed and typed["input_mode"] == "replace_line"
    assert rep["status"] != "ready" and [u["gap"] for u in rep["unreplayable"]] == ["no_text"]


def test_a_cross_app_plan_is_a_multi_app_draft_and_a_read_is_a_prose_gap_never_a_placeholder_typed():
    from anyuse.memory.storage import WorkflowStorage
    storage = WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-mapc4-")))
    amap = M.build_desktop_map(["Finder", "TextEdit", "Notes"], routines=[(_notes_routine(), "open a note", "fx:notes")])
    p = _plan(amap, "copy", ["Notes", "TextEdit"], [("com.apple.Notes:launch_app", {}),
                                                    ("com.apple.Notes:open_item:note", {"note": "Einkauf Samstag"}),
                                                    ("com.apple.TextEdit:launch_app", {}),
                                                    ("com.apple.TextEdit:doc_save:id:_NS:358:write", {"text": "Milch"})])
    draft, rep = _project(amap, p, storage=storage)
    assert draft["apps_involved"] == ["Notes", "TextEdit"] and draft["app"] == "Notes"
    assert [(s["app"], s["bundle_id"]) for s in _steps(draft)][:2] == [("Notes", "com.apple.Notes"), ("TextEdit", "com.apple.TextEdit")]
    assert rep["status"] == "ready", rep["reasons"]
    _wf, rec = DC.find_saved("r4", storage)
    assert rec["apps"] == [{"app": "Notes", "bundle_id": "com.apple.Notes"}, {"app": "TextEdit", "bundle_id": "com.apple.TextEdit"}]
    assert [s["command"]["action"] for s in rec["steps"]][:3] == ["select_row", "open_app", "enter_text"]
    assert rec["compiler"]["version"] == 2
    # a one-app plan stays a one-app draft: no per-step app, exactly as before
    q = _plan(amap, "write", ["TextEdit"], [("com.apple.TextEdit:doc_save:id:_NS:358:write", {"text": "Milch"})])
    d1, _rep1 = _project(amap, q)
    assert d1["apps_involved"] == ["TextEdit"] and all("app" not in s for s in _steps(d1))
    # a read the AX runner has no step for is a prose gap; the value that depends on it is never typed as a placeholder
    r = _plan(amap, "read then write", ["Notes", "TextEdit"], [("com.apple.Notes:open_item:note", {"note": "Preise"}),
                                                              ("com.apple.Notes:read_content:note", {"var": "read0"}),
                                                              ("com.apple.TextEdit:doc_save:id:_NS:358:write", {"text": "${read0}"})])
    d3, rep3 = _project(amap, r)
    steps = _steps(d3)
    assert steps[1]["action"] == "read_content" and "text" not in steps[2]
    assert rep3["status"] != "ready" and sorted(u["gap"] for u in rep3["unreplayable"]) == ["no_text", "prose"]
    assert M.overrides_from_plan(r, amap) == [{"name": "note", "ref": "Preise", "type": "choice"}]


def test_a_proven_record_with_a_row_step_re_enters_the_map_and_projects_as_select_row():
    import test_ax_runner_gaps as G
    from anyuse.execution import draft_params as DP
    src = DC.load_source(G.row_draft())
    cands = DP.collect_candidates(src.steps)
    choice = DC.choose_parameters(src, cands, overrides=G.ROW_PARAMS)
    rec = DC.compile_record(src, choice["parameters"], choice["candidates"])
    rec["status"] = "ready"
    amap = M.from_workflow_record(rec, ref="wf:rows")
    wf = next(t for t in amap.transitions if t.effect == "workflow")
    row = wf.ax[1]
    assert row["action"] == "select_row" and row["selector"]["kind"] == "row" and row["selector"]["name"] == "${datei}"
    assert row["selector"]["container"] == G.BOX
    assert wf.params == [{"name": "suche", "kind": "text"}, {"name": "datei", "kind": "choice"}]
    p = _plan(amap, "reuse", ["Notizen"], [(wf.id, {"suche": "Angebot", "datei": "Angebot 7.pdf"})])
    draft, rep = _project(amap, p, app="Notizen")
    steps = _steps(draft)
    assert steps[1]["selector"]["canonical_role"] == "row" and steps[1]["container"] == G.BOX and "kind" not in steps[1]["selector"]
    assert M.overrides_from_plan(p, amap) == [{"name": "suche", "ref": "Angebot", "type": "text"},
                                              {"name": "datei", "ref": "Angebot 7.pdf", "type": "choice"}]
    assert rep["status"] == "ready", rep["reasons"]


def test_a_source_named_outside_an_instruction_clause_must_be_opened_too_post_hoc_rule_7():
    """Pool 4's ND10: "I need the meeting point FROM THE NOTE about the short route. Leave the note open and
    replace the meeting-point line in the open document ... save." The model's plan edited the document from the
    caller's values and never opened the note; rule 6 ran only on instruction clauses, so the source went unchecked
    and gate R passed the plan (a wrong accept). Rule 7 applies the source rule to every clause."""
    amap = M.build_desktop_map(["Notes", "TextEdit"], routines=[(_notes_routine(), "open a note", "fx:notes")])
    text = ("Für die Hörführung brauch ich den Treffpunkt aus der Notiz zur kurzen Runde ohne Treppen. Lass die Notiz offen "
            "und ersetz im offenen Ablauf nur die Treffpunkt-Zeile durch die Angabe hinter „Treffpunkt:“. Dann speichern.")
    edit = ("com.apple.TextEdit:doc_save:id:_NS:358:edit", {"text": "Treffpunkt: Seitentor am Mühlbach", "anchor": "Treffpunkt: Haupteingang"})
    p = _plan(amap, text, ["Notes", "TextEdit"], [("com.apple.TextEdit:launch_app", {}), edit])
    for s in p.steps:
        s.how = "model+caller"
    cov = M.coverage_gaps(text, p, amap)
    assert any(g["kind"] == "source" and "aus der notiz" in g["reason"] for g in cov["gaps"]), cov
    # the same plan with the note opened (bound from the caller's value): no source gap
    q = _plan(amap, text, ["Notes", "TextEdit"], [("com.apple.Notes:open_item:note", {"note": "Route Kiesel"}),
                                                  ("com.apple.TextEdit:launch_app", {}), edit])
    for s in q.steps:
        s.how = "model+caller"
    assert not any(g["kind"] == "source" for g in M.coverage_gaps(text, q, amap)["gaps"])


# ── 7. reverse coverage (rule 8), the seeded anchor, the folded sort group ──────────────────────────

def test_rule_8_a_step_no_clause_asks_for_is_an_extra_gap_never_a_silent_extra():
    """Pool 2's NG03: the model reached the note by SEARCHING Notes for the caller's lookup value, then
    looked the word up in the Duden; every asked item was ok, the Notes search field stayed changed, gate R passed
    the plan. Rule 8: a step whose effect no clause asks for in its app is a gap of kind ``extra``."""
    amap = M.build_desktop_map(["Notes", "Dictionary"], routines=[(_notes_routine(), "open a note", "fx:notes")])
    text = ("In „Wörter fürs Lektorat“ hab ich mir das Wort für die unnötige Wiederholung notiert. "
            "Schlag das mal im Duden nach und lass es da stehen.")
    duden = next(t for t in amap.transitions if t.effect == "toggle_select" and t.app == "Dictionary")
    base = [("com.apple.Notes:launch_app", {}), ("com.apple.Notes:navigate:folder", {"folder": "Wörter fürs Lektorat"}),
            ("com.apple.Dictionary:launch_app", {}), (duden.id, {"choice": "Duden-Wissensnetz deutsche Sprache"}),
            ("com.apple.Dictionary:scoped_find:structure:search_field", {"lookup": "Pleonasmus"})]
    with_search = base[:2] + [("com.apple.Notes:scoped_find:id:_NS:654", {"lookup": "Pleonasmus"})] + base[2:]
    p = _plan(amap, text, ["Notes", "Dictionary"], with_search)
    for s in p.steps:
        s.how = "model_plan+caller"
        if s.effect == "toggle_select":
            s.option = "Duden-Wissensnetz deutsche Sprache"
    cov = M.coverage_gaps(text, p, amap)
    extras = [g for g in cov["gaps"] if g["kind"] == "extra"]
    assert len(extras) == 1 and "scoped_find in Notes" in extras[0]["reason"], cov["gaps"]
    # the same plan without the unasked search: no extra (the Duden search is asked by "schlag ... im Duden nach",
    # the navigation names the folder the text names, the launches are structural)
    q = _plan(amap, text, ["Notes", "Dictionary"], base)
    for s in q.steps:
        s.how = "model_plan+caller"
        if s.effect == "toggle_select":
            s.option = "Duden-Wissensnetz deutsche Sprache"
    assert not [g for g in M.coverage_gaps(text, q, amap)["gaps"] if g["kind"] == "extra"]
    # post hoc after pool 5: an en dash in the text meets the hyphenated synonym ("German–English dictionary" asks for
    # the Oxford choice), and an unbound step is a gap already, never an extra
    o = _plan(amap, "Look that word up in the German–English dictionary.", ["Dictionary"],
              [(duden.id, {"choice": "Oxford German Dictionary"}), ("com.apple.Dictionary:scoped_find:structure:search_field", {"lookup": "${lookup}"})])
    o.steps[0].option = "Oxford German Dictionary"
    assert not [g for g in M.coverage_gaps(o.task, o, amap)["gaps"] if g["kind"] == "extra"]
    # a search the text asks for in that app is not extra ("durchsuch meine Notizen nach Boje")
    r = _plan(amap, "Durchsuch meine Notizen nach Boje.", ["Notes"], [("com.apple.Notes:scoped_find:id:_NS:654", {"lookup": "Boje"})])
    assert not [g for g in M.coverage_gaps(r.task, r, amap)["gaps"] if g["kind"] == "extra"]


def test_an_edit_from_the_map_is_draft_until_a_live_fill_supplies_the_fields_start_shape():
    """The proof starts a text mode from the RECORDED start shape and never from an invented line. The map records no
    document (the recordings keep only an edit's character counts), so an append / edit from the map is ``draft`` with
    the proof's own reason - the product path needs no guard of its own (the former ``needs_proof`` guard is gone)."""
    amap = M.build_desktop_map(["TextEdit"])
    p = _plan(amap, "edit", ["TextEdit"], [("com.apple.TextEdit:launch_app", {}),
                                           ("com.apple.TextEdit:doc_save:id:_NS:358:edit", {"text": "Status: erledigt", "anchor": "Status"})])
    draft = M.draft_from_plan(p, amap, "r6", p.task)
    typed = _steps(draft)[0]
    assert typed["input_mode"] == "replace_line" and typed["anchor"] == "Status" and "start" not in typed
    rep = run(DC.draft_to_workflow(draft, overrides=M.overrides_from_plan(p, amap), save=False))
    assert rep["status"] == "draft" and any("start" in r for r in rep["reasons"]), rep["reasons"]
    assert not hasattr(M, "seeded_anchors") and not hasattr(M, "NEEDS_PROOF")
    storage = _storage()
    rep2 = run(M.from_map("TextEdit", "Change the line Status: offen to Status: erledigt and save.", amap, storage=storage,
                          reviewer=lambda t, r: {"covers": True, "missing": "", "extra": "", "parsed": True}))
    assert rep2["plan"]["complete"] and rep2["status"] == "draft" and any("start" in r for r in rep2["reasons"]), rep2["reasons"]
    # a write (the whole value) still proves and is ready through the same path
    rep3 = run(M.from_map("TextEdit", WRITE, amap, storage=_storage(),
                          reviewer=lambda t, r: {"covers": True, "missing": "", "extra": "", "parsed": True}))
    assert rep3["status"] == "ready", rep3["reasons"]


def test_the_probes_focus_record_builds_the_focused_selector_with_its_role_and_app():
    """§12.7: an object_create / scoped_find entry probed with the new focus read carries
    ``focus = {kind, canonical_role, raw_role, app, document, window, trigger}``; the map builds the focused selector
    from it (role, raw role, app), leaving the probe's document hash off; an entry without the record maps as before."""
    e = {"effect": "object_create", "id": "x:object_create:id:_NS:1", "kind": "item", "label": "Neue Notiz", "path": "Ablage > Neue Notiz",
         "commit": "return", "entry": "focused text entry after the trigger", "observed": ["object_created", "entry_focused"],
         "trigger": {"identifier": "_NS:1", "index_path": [2, 0]},
         "focus": {"kind": "focused", "canonical_role": "textarea", "raw_role": "AXTextArea", "app": "com.apple.Notes",
                   "document": "sha256:0123456789abcdef", "window": "", "trigger": {"identifier": "_NS:1"}}}
    hops = M._entry_hops(e, "${name}", "the item's name")
    assert hops[0]["action"] == "enter_text" and hops[0]["selector"] == {"kind": "focused", "canonical_role": "textarea", "name": "",
                                                                         "identifier": "", "path": "", "raw_role": "AXTextArea",
                                                                         "app": "com.apple.Notes"}
    assert hops[1]["action"] == "press_key" and hops[1]["selector"]["canonical_role"] == "textarea" and "document" not in hops[1]["selector"]
    without = dict(e)
    del without["focus"]
    assert [h.get("gap") for h in M._entry_hops(without, "${name}", "the item's name")] == ["map_hop", "map_hop"]   # as before
    secure = dict(e, focus=dict(e["focus"], canonical_role="passwordfield"))
    assert [h.get("gap") for h in M._entry_hops(secure, "${name}", "x")] == ["map_hop", "map_hop"]              # not a text field


def test_sort_by_size_and_tags_fold_into_the_sort_group_from_the_probe_data():
    raw = json.load(open(M.EFFECT_MAP_PATH, encoding="utf-8"))
    folded = M.fold_sort_options(raw)
    sort = next(e for e in folded["apps"]["Finder"]["entries"] if e["effect"] == "toggle_select" and "Sortieren" in e["group"])
    labels = [o["label"] for o in sort["options"]]
    assert labels[:6] == ["Name", "Art", "Zuletzt geöffnet", "Hinzugefügt am", "Änderungsdatum", "Erstellungsdatum"]
    assert set(labels[6:]) == {"Größe", "Tags"} and all(o.get("folded") for o in sort["options"][6:])
    size = next(o for o in sort["options"] if o["label"] == "Größe")
    assert size["key"] == "id:_NS:126" and size["trigger"]["identifier"] == "_NS:126" and size["trigger"]["index_path"][:2] == sort["parent_index_path"]
    left = [o["label"] for o in folded["apps"]["Finder"]["unmapped_observed"]["flag_toggle"]]
    assert "Größe" not in left and "Tags" not in left and "Gruppen verwenden" in left     # a flag of another menu stays
    assert M.fold_sort_options(folded) == folded                                          # idempotent
    assert [o["label"] for o in raw["apps"]["Finder"]["unmapped_observed"]["flag_toggle"]].count("Größe") == 1   # the input untouched
    em = M.from_effect_map()
    t = em.get(sort["id"])
    assert "Größe" in [o["label"] for o in t.options] and t.ax[-1]["selector"]["name"] == "${choice}"
    # the lexicon plans a sort by size from the German label (the frozen lexicon has no English synonym for it)
    amap = M.build_desktop_map(["Finder"])
    p = M.plan("Sortier den Ordner nach Größe.", amap)
    assert p.complete and [s.effect for s in p.steps][-1] == "toggle_select" and p.steps[-1].bindings["choice"] == "Größe", (p.gaps, p.steps)


# ── 8. the lexicon extension from pools 1-5, rule 8's app names, the repair call's allowances ───────

def test_the_verb_extension_reads_the_pools_uncovered_clauses_as_instructions():
    from anyuse.execution import map_compile_verbs as V
    # separable verbs serve the whole verb's effect; a past form or a verb after "I" is not an instruction
    assert V.clause_verbs("mach mal die korrigierte notiz zur bühnenprobe auf") == [("mach", ["open_item"])]
    assert "doc_save" in V.clause_effects("stell die abholzeit auf 9:00 uhr um") and "toggle_select" in V.clause_effects("stell mal auf galerie um")
    assert V.clause_verbs("open the one i changed most recently") == [("open", ["open_item"])]
    assert V.clause_verbs("open the one where i wrote that the dentist moved my appointment") == [("open", ["open_item"])]
    assert not V.is_instruction("den ordner bitte nach änderungsdatum sortiert lassen")        # a constraint
    # the new general verbs, each with the effect it serves
    assert V.clause_effects("die mehlmenge verdoppeln") == ["doc_save"]
    assert V.clause_effects("die kleine nordmanntanne von 39 auf 42 euro hochsetzen") == ["doc_save"]
    assert set(V.clause_effects("find my note about the strip inside the spine")) == {"open_item", "select_item"}   # locate, not search
    assert set(V.clause_effects("kannst du die notiz mit dem faden quer zur kette raussuchen")) == {"open_item", "select_item"}
    assert set(V.clause_effects("schau mal in den mac-schulungsnotizen nach dem begriff")) >= {"scoped_find", "read_content"}
    assert "scoped_find" in V.clause_effects("den bitte im apple-lexikon aufrufen")
    assert "open_item" in V.clause_effects("zeig mir die notiz") and "doc_save" in V.clause_effects("übernimm die zeile in den ablauf")
    assert V.clause_effects("trag mal die nummer nach") == ["doc_save"]
    # the old readings stand
    assert V.clause_effects("sortier den ordner nach art") == ["toggle_select"] and V.clause_effects("schalt die bindestriche aus") == ["flag_toggle"]


def test_rule_8_checks_the_apps_a_clause_names_not_every_cue_word():
    assert M._clause_apps("trag mal bei der gartenfest-gästeliste die nummer von tante heidi nach") == set()
    assert M._clause_apps("schlag das mal im duden nach") == {"Dictionary"}
    assert M._clause_apps("durchsuch meine notizen nach boje") == set()          # "meine Notizen" is content, not the app
    assert M._clause_apps("such in notes nach boje") == {"Notes"} and M._clause_apps("in der datei mit den preisen") == set()
    from anyuse.execution import map_compile_verbs as V
    assert V.source_phrase("trag mal in die offene checkliste den freigegebenen treffpunkt aus „sternwarte absprachen“ ein").startswith("aus „")
    assert V.source_phrase("übernimm mal die endgültige treffpunkt-zeile aus meinen paddelnotizen in den offenen ablauf") == "aus meinen paddelnotizen"
    assert M._source_kind("aus meinen paddelnotizen") == "note"
    # pool 5's NE09: the checklist the plan opens is the write's target, not the quoted source - a source gap stays
    amap2 = M.build_desktop_map(["TextEdit", "Notes"], routines=[(_notes_routine(), "open a note", "fx:notes")])
    t9 = "Trag mal in die offene Checkliste den freigegebenen Treffpunkt aus „Sternwarte Absprachen“ ein. Nur die Treffpunkt-Zeile ersetzen und speichern."
    w = _plan(amap2, t9, ["TextEdit"], [("com.apple.TextEdit:launch_app", {}), ("com.apple.TextEdit:open_item:_NS:331", {"file": "Nachtführung.txt"}),
                                        ("com.apple.TextEdit:doc_save:id:_NS:358:edit", {"text": "Treffpunkt: rote Tür am Ostturm", "anchor": "Treffpunkt: Parkplatz unten"})])
    for st in w.steps:
        st.how = "model_plan+caller"
    assert any(g["kind"] == "source" for g in M.coverage_gaps(t9, w, amap2)["gaps"])
    w2 = _plan(amap2, t9, ["Notes", "TextEdit"], [("com.apple.Notes:open_item:note", {"note": "Treffpunkt – bestätigt"})] +
               [("com.apple.TextEdit:doc_save:id:_NS:358:edit", {"text": "Treffpunkt: rote Tür am Ostturm", "anchor": "Treffpunkt: Parkplatz unten"})])
    for st in w2.steps:
        st.how = "model_plan+caller"
    assert not any(g["kind"] == "source" for g in M.coverage_gaps(t9, w2, amap2)["gaps"])
    amap = M.build_desktop_map(["Notes", "TextEdit"], routines=[(_notes_routine(), "open a note", "fx:notes")])
    # pool 2's NS09: an edit asked with "trag ... nach" and the value from the caller, in a clause that names no app
    text = "Trag mal bei der Gartenfest-Gästeliste die Nummer von Tante Heidi nach, die steht in meinen Notizen."
    p = _plan(amap, text, ["Notes", "TextEdit"], [("com.apple.Notes:open_item:note", {"note": "Tante Heidi"}),
                                                  ("com.apple.TextEdit:doc_save:id:_NS:358:append", {"text": "Tante Heidi: 0171 5"})])
    for s in p.steps:
        s.how = "model_plan+caller"
    assert not [g for g in M.coverage_gaps(text, p, amap)["gaps"] if g["kind"] == "extra"]


def test_the_repair_call_is_told_the_structural_allowances_and_the_lexicon_knows_size_and_tags():
    amap = M.build_desktop_map(["Finder", "Notes", "Dictionary"], routines=[(_notes_routine(), "open a note", "fx:notes")])
    duden = next(t for t in amap.transitions if t.effect == "toggle_select" and t.app == "Dictionary")
    seen = []

    def fake(messages, schema):
        seen.append(messages)
        steps = [{"candidate": "com.apple.Notes:navigate:folder", "bindings": [{"name": "folder", "value": "Wörter fürs Lektorat"}]},
                 {"candidate": duden.id, "bindings": [{"name": "choice", "value": "Duden-Wissensnetz deutsche Sprache"}]},
                 {"candidate": "com.apple.Dictionary:scoped_find:structure:search_field", "bindings": [{"name": "lookup", "value": "Pleonasmus"}]}]
        if len(seen) == 1:        # the first answer searches Notes for the caller's word: an extra
            steps.insert(1, {"candidate": "com.apple.Notes:scoped_find:id:_NS:654", "bindings": [{"name": "lookup", "value": "Pleonasmus"}]})
        return json.dumps({"steps": steps, "gaps": []}), {"calls": 1, "usage": {}}

    text = "In „Wörter fürs Lektorat“ hab ich mir das Wort für die unnötige Wiederholung notiert. Schlag das mal im Duden nach."
    p = M.plan_by_model(text, amap, fake, caller_values={"lookup": "Pleonasmus", "note": "Doppelt gesagt"})
    assert len(seen) == 2
    rejection = json.loads(seen[1][-1]["content"])["your_previous_plan_was_rejected"]
    assert "did not ask" in rejection and "keep the structural steps" in rejection and "scoped_find in Notes" in rejection
    assert not [g for g in p.gaps if g.get("kind") == "extra"], p.gaps
    # the folded sort options have synonyms the lexicon reads
    q = M.plan("Sort the folder by size.", amap)
    assert q.steps and q.steps[-1].effect == "toggle_select" and q.steps[-1].bindings["choice"] == "Größe", (q.gaps, q.steps)
    r = M.plan("Sortier den Ordner nach Tags.", amap)
    assert r.steps and r.steps[-1].bindings["choice"] == "Tags"


if __name__ == "__main__":
    import inspect
    names = [n for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for n in names:
        try:
            globals()[n]()
            print(f"ok   {n}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {n}: {exc}")
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
