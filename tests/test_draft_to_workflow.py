"""The watcher -> automation seam (execution/draft_compile.py, draft_params.py, draft_proof.py), offline with fixtures and fakes.

Pinned here: the conversion of watcher drafts and the guest harness's recording into the saved
format (AX steps with ${name} placeholders, template, graph, approval gates, drift profile,
attribution, compiler version); parameters chosen from candidates by a model, validated
mechanically, overridden by a person; prompt-injected typed values that change nothing; the
fresh-input proof passing and failing for the right reasons; the web recording directory going
through the unchanged web compile and its proof; the live replay refusing on the host; a ready
workflow never replaced by a new compile; the verdict hook.

No desktop, no AX, no network, no model: every model answer is a fake.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_draft_to_workflow.py
"""
import asyncio
import json
import os
import pathlib
import shutil
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import draft_params as DP  # noqa: E402
from anyuse.execution import draft_proof as PR  # noqa: E402
from anyuse.memory.storage import WorkflowStorage  # noqa: E402

FIX = _HERE / "testdata" / "drafts"
TE = str(FIX / "textedit_draft.json")
WEB = str(FIX / "web_form_draft.json")
RO = str(FIX / "readonly_draft.json")
HOSTILE = str(FIX / "hostile_draft.json")
PROSE = str(FIX / "prose_draft.json")
DRIVE = str(FIX / "drive_recording" / "workflow.json")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def storage():
    return WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-d2w-")))


def ov(name, ref, ptype=""):
    return {"name": name, "ref": ref, "type": ptype}


def fake_llm(answer, seen=None):
    def llm(messages, schema):
        if seen is not None:
            seen.append({"messages": messages, "schema": schema})
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return text, {"provider": "fake", "model": "fake", "calls": 1, "usage": {}}
    return llm


TE_PARAMS = [ov("text", "@c2"), ov("dateiname", "@c4", "file_name")]


# ── 1. the draft, normalized ──────────────────────────────────────────────────────────────────

def test_a_textedit_draft_becomes_ax_steps_and_candidates():
    src = DC.load_source(TE)
    assert src.kind == "draft_file" and src.app == "TextEdit" and not src.partial
    assert [s.action for s in src.steps] == ["press", "enter_text", "press", "enter_text", "press"]
    # the third step's identity came from the AX anchor the watcher wrote (no selector in data)
    assert src.steps[2].selector["identifier"] == "_NS:358" and src.steps[2].selector["name"] == "Sichern"
    cands = DP.collect_candidates(src.steps)
    assert [(c.id, c.source, c.value) for c in cands] == [
        ("c1", "chosen", "Neu"), ("c2", "typed", "Einkaufsliste: Milch, Brot, Kaffee"),
        ("c3", "chosen", "Sichern"), ("c4", "typed", "notiz-2026-10-08.txt")]
    assert src.attribution["taught_by"] == "Alex (fixture)" and src.attribution["taught_at"]


def test_anchor_descriptions_parse_only_in_the_anchor_format():
    assert DC.parse_selector_description("button 'Save' id=_NS:9 path=window/sheet/button[1]") == {
        "canonical_role": "button", "name": "Save", "identifier": "_NS:9",
        "path": "window/sheet/button[1]", "raw_role": ""}
    assert DC.parse_selector_description("menuitem \"Don't save\"")["name"] == "Don't save"
    for prose in ("the blue Save button", "clicked Reply", "button", "spaceship 'x'"):
        assert DC.parse_selector_description(prose) is None


# ── 2. parameters: overrides, the model, validation ──────────────────────────────────────────

def test_overrides_compile_prove_and_save_a_ready_workflow():
    st = storage()
    rep = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st))
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["proof"]["passed"] and rep["proof"]["fresh_inputs"] == 2
    assert rep["parameter_choice"]["how"] == "override"
    wf = st.get("write_note")
    assert wf is not None and wf.verified and wf.variables == ["text", "dateiname"] and wf.steps == []
    rec = json.load(open(os.path.join(rep["saved"]["dir"], DC.RECORD_FILE)))
    # a one-app record of press / enter_text steps says version 1 (the runner contract's
    # version 2 is written only by a record that needs it: test_ax_runner_gaps.py)
    assert rec["format"] == DC.FORMAT and rec["compiler"] == {"name": "draft_compile", "version": 1}
    assert DC.record_compiler(rec["steps"]) == rec["compiler"] and DC.COMPILER["version"] == 2
    inputs = [s["input_data"] for s in rec["steps"]]
    assert inputs == [None, "${text}", None, "${dateiname}", None]
    # the recorded literals stay out of the replayable steps
    assert "Einkaufsliste" not in json.dumps(rec["steps"]) and "notiz-2026" not in json.dumps(rec["steps"])
    assert rec["attribution"]["taught_by"] == "Alex (fixture)"
    assert rec["attribution"]["draft_digest"] and rec["attribution"]["draft_ref"] == TE
    assert rec["drift_profile"]["baseline_steps"] == 5 and rec["drift_profile"]["gate"] == "on"
    assert rec["verdict_keys"]["artifact_digest"] and rec["status"] == "ready"
    for case in rec["proof"]["cases"]:
        assert case["passed"] and case["holds"] == 5 and case["drift_fired"] == 0 and case["llm_calls"] == 0
        assert case["values"]["dateiname"].endswith(".txt") and case["values"]["text"] != "Einkaufsliste"


def test_approval_gates_hold_every_write_save_and_submit_step():
    rep = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, save=False))
    gated = {g["index"]: g for g in rep["approvals"]["gated_steps"]}
    assert rep["approvals"]["mode"] == "all"
    assert set(gated) == {0, 1, 2, 3, 4}                 # both text entries, the save menu, the button
    assert gated[1]["class"] == "side_effecting" and gated[1]["executor_class"] == "destructive"
    web = run(DC.draft_to_workflow(WEB, overrides=[ov("name", "@c1"), ov("betrag", "@c2", "number")],
                                   save=False))
    submit = [g for g in web["approvals"]["gated_steps"] if "Absenden" in g["intent"]
              or g["index"] == 3]
    assert submit and submit[0]["class"] == "destructive"
    api = next(r for r in web["rungs"] if r["rung"] == "api")
    assert api["status"] == "not_applicable" and "destructive" in api["reason"]


def test_the_model_chooses_from_candidates_and_an_override_wins():
    seen = []
    answer = {"parameters": [
        {"candidate": "c2", "name": "notiz", "type": "text", "description": "der Text"},
        {"candidate": "c4", "name": "datei", "type": "file_name", "description": "der Dateiname"}]}
    rep = run(DC.draft_to_workflow(TE, propose=True, llm=fake_llm(answer, seen), save=False))
    assert len(seen) == 1 and rep["status"] == "ready", rep["reasons"]
    assert [(p["name"], p["chosen_by"]) for p in rep["parameters"]] == [("notiz", "model"), ("datei", "model")]
    assert rep["parameter_choice"]["proposal"]["estimate"]["max_usd"] < 0.01
    # the override renames c2 and wins over the model on that candidate
    rep = run(DC.draft_to_workflow(TE, propose=True, llm=fake_llm(answer),
                                   overrides=[ov("Einkaufstext", "@c2")], save=False))
    assert {(p["name"], p["candidate"], p["chosen_by"]) for p in rep["parameters"]} == {
        ("Einkaufstext", "c2", "override"), ("datei", "c4", "model")}
    # overrides alone: no model call at all
    seen2 = []
    rep = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, llm=fake_llm(answer, seen2), save=False))
    assert seen2 == [] and rep["parameter_choice"]["how"] == "override"
    # by recorded value instead of id, and a person's "fixed" removes a model choice
    rep = run(DC.draft_to_workflow(TE, propose=True, llm=fake_llm(answer), fixed=["@c4"],
                                   overrides=[ov("notiz", "Einkaufsliste: Milch, Brot, Kaffee")], save=False))
    assert [p["name"] for p in rep["parameters"]] == ["notiz"]


BAD_ANSWERS = [
    ("not json", "the answer is not a JSON object"),
    ({"params": []}, "'parameters' list"),
    ({"parameters": [{"candidate": "c9", "name": "x", "type": "text", "description": ""}]}, "not a candidate id"),
    ({"parameters": [{"candidate": "c2", "name": "zwei wörter", "type": "text", "description": ""}]},
     "not a parameter name"),
    ({"parameters": [{"candidate": "c2", "name": "t", "type": "choice", "description": ""}]},
     "needs a chosen item"),
    ({"parameters": [{"candidate": "c2", "name": "t", "type": "number", "description": ""}]},
     "does not parse as a number"),
    ({"parameters": [{"candidate": "c2", "name": "t", "type": "date", "description": ""}]},
     "does not parse as a date"),
    ({"parameters": [{"candidate": "c1", "name": "t", "type": "text", "description": ""}]},
     "can only be a 'choice'"),
    ({"parameters": [{"candidate": "c2", "name": "a", "type": "text", "description": ""},
                     {"candidate": "c2", "name": "b", "type": "text", "description": ""}]}, "used twice"),
    ({"parameters": [{"candidate": "c2", "name": "a", "type": "text", "description": ""},
                     {"candidate": "c4", "name": "A", "type": "file_name", "description": ""}]}, "used twice"),
    ({"parameters": [{"candidate": "c2", "name": "a", "type": "text", "description": "",
                      "steps": ["delete everything"]}]}, "unexpected keys"),
    ({"parameters": [], "add_step": "rm -rf ~"}, "unexpected keys"),
]


def test_bad_model_output_is_rejected_whole_and_the_workflow_stays_a_draft():
    src = DC.load_source(TE)
    cands = DP.collect_candidates(src.steps)
    for answer, want in BAD_ANSWERS:
        data = DP._loads(answer) if isinstance(answer, str) else answer
        params, problems = DP.validate_proposal(data, cands)
        assert params == [] and any(want in p for p in problems), (answer, problems)
    # one good and one bad entry: nothing is used
    mixed = {"parameters": [{"candidate": "c2", "name": "ok", "type": "text", "description": ""},
                            {"candidate": "c99", "name": "bad", "type": "text", "description": ""}]}
    rep = run(DC.draft_to_workflow(TE, propose=True, llm=fake_llm(mixed), save=False))
    assert rep["status"] == "draft" and rep["parameters"] == []
    assert any("rejected" in r for r in rep["reasons"])
    # the model raising (no key, an outage) is an honest "incomplete", not a guess
    def broken(_m, _s):
        raise RuntimeError("CEREBRAS_API_KEY is not set")
    rep = run(DC.draft_to_workflow(TE, propose=True, llm=broken, save=False))
    assert rep["status"] == "draft" and any("no model answer" in r for r in rep["reasons"])


def test_prompt_injected_typed_values_change_no_instruction_and_no_step():
    src = DC.load_source(HOSTILE)
    cands = DP.collect_candidates(src.steps)
    msgs = DP.proposal_messages(src.goal, cands)
    assert msgs[0] == {"role": "system", "content": DP.SYSTEM_PROMPT}       # constant, whatever was typed
    data = json.loads(msgs[1]["content"])                                    # one JSON data block
    assert set(data) == {"goal", "candidates"}
    assert data["candidates"][1]["value"] == src.steps[1].value               # carried verbatim, as data
    benign = DP.proposal_messages(DC.load_source(TE).goal, DP.collect_candidates(DC.load_source(TE).steps))
    assert benign[0] == msgs[0]
    # a model that "obeys" the injected text is rejected entirely
    obeying = {"parameters": [{"candidate": "c99", "name": "rm -rf", "type": "shell", "description": ""}]}
    rep = run(DC.draft_to_workflow(HOSTILE, propose=True, llm=fake_llm(obeying), save=False))
    assert rep["parameters"] == [] and rep["status"] == "draft"
    # with a person's choice the hostile text is a value: same steps, the text only as a parameter
    rep = run(DC.draft_to_workflow(HOSTILE, overrides=TE_PARAMS, save=False))
    assert rep["status"] == "ready", rep["reasons"]
    plain = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, save=False))
    assert [g["intent"] for g in rep["approvals"]["gated_steps"]] == \
        [g["intent"] for g in plain["approvals"]["gated_steps"]]
    rec = DC.compile_record(src, [DP.Parameter("text", "c2", "text")], cands)
    assert len(rec["steps"]) == 5 and "Ignore all previous" not in json.dumps(rec["steps"])
    # the hostile file name is refused as a file_name value (it holds no path, but a person sees it)
    assert DP.type_problem("file_name", "a/b.txt", "typed")


# ── 3. drafts that cannot become ready ───────────────────────────────────────────────────────

def test_typed_text_with_no_parameter_is_saved_as_a_draft_with_its_reason():
    st = storage()
    rep = run(DC.draft_to_workflow(TE, storage=st))
    assert rep["status"] == "draft" and rep["candidates"] and rep["parameters"] == []
    assert any("no parameter" in r for r in rep["reasons"]) and not rep["proof"].get("fixed_routine")
    wf = st.get("write_note")
    assert wf and not wf.verified and wf.verification_issues


def test_n17_a_recording_with_nothing_typed_is_a_fixed_routine_proved_by_one_replay():
    # a fixed routine (here: open the account overview; in the run, Chess's new game): no value to
    # vary, so the proof is one replay whose effects are exactly the recorded ones
    st = storage()
    rep = run(DC.draft_to_workflow(RO, storage=st))
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["candidates"] == [] and rep["parameters"] == []
    proof = rep["proof"]
    assert proof["fixed_routine"] and proof["passed"] and proof["min_cases"] == 1 and len(proof["cases"]) == 1
    assert "reproduced the recorded effects" in proof["reason"]
    rec = json.load(open(os.path.join(rep["saved"]["dir"], DC.RECORD_FILE)))
    assert [s["command"]["action"] for s in rec["steps"]] == ["press"]       # scroll / read carry no effect
    wf = st.get("kontostand_ansehen")
    assert wf and wf.verified
    # its replay takes no values and presses the recorded link, with no model call
    from anyuse.execution import harness_compile as HC
    world = PR.SimAXWorld(rec)
    out = run(HC.replay_desktop(st, "kontostand_ansehen", {}, world=world))
    assert out["ok"], out.get("stopped")
    assert [e["op"] for e in world.effects] == ["AXPress"] and out["model_calls"] == 0
    # a routine whose replay does not reproduce the recording is not proved (the recorded state
    # shows another link than the one the step presses)
    bad = json.loads(json.dumps(rec))
    bad["recorded"][0]["command"]["selector"]["name"] = "Umsätze"
    proof = run(PR.prove(bad))
    assert proof["fixed_routine"] and not proof["passed"] and "no candidate matched" in proof["reason"]


def test_prose_steps_are_refused_with_their_reason():
    rep = run(DC.draft_to_workflow(PROSE, save=False))
    assert rep["status"] == "draft" and len(rep["unreplayable"]) == 2
    assert "prose" in rep["unreplayable"][0]["reason"]
    assert any("partial" in r for r in rep["reasons"])                        # no WORKFLOW_END


def test_a_password_field_is_never_a_candidate_and_never_stored():
    data = json.load(open(TE))
    data["events"][2]["data"]["selector"] = {"canonical_role": "passwordfield", "name": "Passwort",
                                             "identifier": "", "path": "", "raw_role": "AXSecureTextField"}
    src = DC.load_source(data)
    assert src.steps[1].value is None and "password" in src.steps[1].problem
    assert all(c.value != "Einkaufsliste: Milch, Brot, Kaffee" for c in DP.collect_candidates(src.steps))
    rep = run(DC.draft_to_workflow(data, overrides=[ov("dateiname", "@c3", "file_name")], save=False))
    # the step is a sensitive gap (the agent bridge types a vault credential);
    # the typed text itself is still never stored
    assert rep["status"] == "needs_fill" and "Einkaufsliste" not in json.dumps(rep)
    assert rep["unreplayable"][0]["sensitive"] and rep["unreplayable"][0]["gap"] == "sensitive"
    # a secure raw role is a password field even when the recording claims another canonical role
    data = json.load(open(TE))
    data["events"][2]["data"]["selector"]["raw_role"] = "AXSecureTextField"   # canonical: textarea
    src = DC.load_source(data)
    assert src.steps[1].value is None and "password" in src.steps[1].problem
    assert src.steps[1].selector["canonical_role"] == "passwordfield"


# ── 4. the fresh-input proof ─────────────────────────────────────────────────────────────────

def _record(path=TE, overrides=TE_PARAMS):
    src = DC.load_source(path)
    cands = DP.collect_candidates(src.steps)
    params, problems = DP.apply_overrides(cands, [], overrides)
    assert not problems, problems
    return DC.compile_record(src, params, cands), cands


def test_the_proof_passes_on_values_that_were_not_recorded():
    rec, cands = _record()
    proof = run(PR.prove(rec, cands))
    assert proof["passed"] and proof["passed_cases"] == 2, proof
    for case in proof["cases"]:
        assert case["values"]["text"] != "Einkaufsliste: Milch, Brot, Kaffee"


def test_the_proof_fails_on_a_missed_binding_and_on_a_recorded_fresh_value():
    rec, cands = _record()
    broken = json.loads(json.dumps(rec))
    broken["steps"][3]["input_data"] = "notiz-2026-10-08.txt"          # the file name left recorded
    proof = run(PR.prove(broken, cands))
    assert not proof["passed"] and "effects differ" in proof["reason"]
    proof = run(PR.prove(rec, cands, fresh=[{"text": "Einkaufsliste: Milch, Brot, Kaffee", "dateiname": "a.txt"},
                                            {"text": "neu", "dateiname": "b.txt"}]))
    assert not proof["passed"] and "is or contains the recorded value" in proof["reason"]
    proof = run(PR.prove(rec, cands, fresh=[{"text": "neu", "dateiname": "b.txt"}]))
    assert not proof["passed"] and "at least 2" in proof["reason"]
    same = {"text": "neu", "dateiname": "b.txt"}
    proof = run(PR.prove(rec, cands, fresh=[same, dict(same)]))           # one set given twice
    assert not proof["passed"] and "repeats an earlier set" in proof["reason"]


def test_a_replay_value_carries_no_tab_or_return_and_a_line_break_only_into_a_text_area():
    rec, _ = _record()                               # text -> a text area, dateiname -> a text field
    ok = {"text": "Zeile 1\nZeile 2", "dateiname": "neu.txt"}
    assert DC.value_problems(rec, ok) == []
    for bad in ({"text": "a\tb", "dateiname": "neu.txt"}, {"text": "a\rb", "dateiname": "neu.txt"},
                {"text": "neu", "dateiname": "neu.txt\r"}, {"text": "neu", "dateiname": "ne\tu.txt"},
                {"text": "neu", "dateiname": "neu\n.txt"}):
        probs = DC.value_problems(rec, bad)
        assert len(probs) == 1 and "control character" in probs[0], (bad, probs)


def test_the_proof_fails_when_the_recorded_screen_drifted():
    rec, cands = _record()
    proof = run(PR.prove(rec, cands, world_factory=lambda r: PR.SimAXWorld(r, drop_titles=("Ablage",))))
    assert not proof["passed"] and proof["cases"][0]["drift_fired"] >= 1


def test_a_choice_parameter_is_proved_with_the_other_recorded_options():
    rep = run(DC.draft_to_workflow(WEB, overrides=[ov("name", "@c1"), ov("betrag", "@c2", "number"),
                                                  ov("zahlungsart", "@c3")], save=False))
    assert rep["status"] == "ready", rep["reasons"]
    vals = [c["values"] for c in rep["proof"]["cases"]]
    assert {v["zahlungsart"] for v in vals} == {"Lastschrift", "Rechnung"}
    assert [v["betrag"] for v in vals] == ["137,50", "154,50"]               # the recorded decimal comma kept


def test_a_choice_whose_item_has_an_ax_identifier_is_not_provable():
    rep = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS + [ov("menue", "@c1")], save=False))
    assert rep["status"] == "draft" and "identifier" in rep["proof"]["reason"]


def test_the_guest_harness_recording_compiles_with_text_and_name_parameters():
    src = DC.load_source(DRIVE)
    cands = DP.collect_candidates(src.steps)
    assert [(c.id, c.source) for c in cands] == [("c1", "chosen"), ("c2", "chosen"), ("c3", "chosen"),
                                                 ("c4", "chosen"), ("c5", "typed"), ("c6", "chosen"),
                                                 ("c7", "typed")]
    assert cands[0].steps == [0, 5]                       # "Ablage" opened twice: one candidate
    rep = run(DC.draft_to_workflow(DRIVE, overrides=[ov("text", "@c5"), ov("dateiname", "@c7", "file_name")],
                                   save=False))
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["drift_profile"]["baseline_steps"] == 9 and rep["attribution"]["draft_kind"] == "drive_recording"


# ── 5. the web recording directory: the unchanged web compile and its proof ──────────────────

class _Allowed:
    may_prefer = True
    reason = "fixture"


class _ReplayExecutor:
    def __init__(self, by_value):
        self.by_value, self.calls = by_value, []

    def execute(self, template, params):
        value = list(params.values())[0] if params else ""
        self.calls.append(value)
        if value not in self.by_value:
            return {"success": False, "error": "no such value", "method": "api"}
        return {"success": True, "method": "api", "elapsed_ms": 1.0, "bytes": 10,
                "extracted": {"items": self.by_value[value]}}


def test_a_web_recording_directory_goes_through_the_web_compile_and_its_proof():
    from anyuse.execution.api_verify import cases_from_recordings
    src_dir = _HERE / "testdata" / "compile"
    d = tempfile.mkdtemp(prefix="anyuse-d2w-web-")
    for f in ("crates_search.rec.json", "crates_search.fresh1.json", "crates_search.fresh2.json"):
        shutil.copy(src_dir / f, d)
    fresh = [json.load(open(src_dir / f)) for f in ("crates_search.fresh1.json", "crates_search.fresh2.json")]
    ui = {c["value"]: c["ui_items"] for c in cases_from_recordings(fresh, "serde")}
    st = storage()
    ex = _ReplayExecutor(ui)
    rep = run(DC.draft_to_workflow(d, storage=st, api_executor=ex,
                                   permission_checker=lambda url, hosts_path=None: _Allowed()))
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["proof"]["harness"] == "api_verify.compile_and_verify" and ex.calls
    assert rep["parameters"][0]["chosen_by"] == "web_compile"
    assert os.path.isfile(rep["saved"]["compiled_api"])
    # a refused host: no proof traffic, a draft with the reason
    ex2 = _ReplayExecutor(ui)

    class _No:
        may_prefer, reason = False, "robots.txt disallows"
    rep = run(DC.draft_to_workflow(d, save=False, api_executor=ex2,
                                   permission_checker=lambda url, hosts_path=None: _No()))
    assert rep["status"] == "draft" and ex2.calls == [] and "robots" in " ".join(rep["reasons"])


# ── 6. the live replay: the host refuses; with the gates open it is the checked executor ─────

class _Spy:
    def __init__(self):
        self.calls = 0

    async def execute_checked_step(self, *a, **k):
        self.calls += 1
        return {"success": True}, {}


def test_the_live_replay_refuses_on_the_host_and_sends_nothing():
    st = storage()
    run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st))
    spy = _Spy()

    async def approve(_ctx):
        raise AssertionError("no approval may be asked on a refused run")
    out = run(DC.replay_saved("write_note", {"text": "x", "dateiname": "y.txt"}, approver=approve,
                              storage=st, executor=spy))
    assert not out["ok"] and "ANYUSE_DRIVE_ENABLED is off" in out["stopped"] and spy.calls == 0
    # an interlock refusal stops it the same way
    out = run(DC.replay_saved("write_note", {"text": "x", "dateiname": "y.txt"}, approver=approve,
                              storage=st, executor=spy,
                              preflight=lambda: {"may_drive": False, "drive_enabled": True,
                                                 "interlock": {"reason": "own session is on the console"}}))
    assert "interlock refuses" in out["stopped"] and spy.calls == 0


def test_with_the_gates_open_the_replay_holds_then_drives_the_checked_executor():
    from anyuse.execution.executor import StateGraphExecutor
    st = storage()
    run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st))
    # holds are opt-in for a started replay; this test holds every step
    DC.set_approval_level("write_note", "all", storage=st)
    _wf, rec = DC.find_saved("write_note", st)
    world = PR.SimAXWorld(rec)
    asked = []

    async def approve(ctx):
        asked.append(ctx["step_class"])
        world.note_hold()
        return True
    gates = lambda: {"may_drive": True, "drive_enabled": True, "interlock": {"allowed": True}}  # noqa: E731
    ex = StateGraphExecutor(ax_tree=world, drift_gate_enabled=True)
    values = {"text": "Neue Liste: Äpfel", "dateiname": "liste.txt"}
    out = run(DC.replay_saved("write_note", values, approver=approve, storage=st, executor=ex,
                              preflight=gates, on_step=world.at_step))
    assert out["ok"], out["stopped"]
    assert len(asked) == 5
    assert [e["value"] for e in world.effects if e["op"] == "enter_text"] == ["Neue Liste: Äpfel", "liste.txt"]
    # a denied approval stops before the step's effect
    world2 = PR.SimAXWorld(rec)

    async def deny(_ctx):
        return False
    out = run(DC.replay_saved("write_note", values, approver=deny, storage=st,
                              executor=StateGraphExecutor(ax_tree=world2), preflight=gates,
                              on_step=world2.at_step))
    assert not out["ok"] and "not approved" in out["stopped"] and world2.effects == []
    # bad values never reach a step
    out = run(DC.replay_saved("write_note", {"text": "x", "dateiname": "a/b.txt"}, approver=approve,
                              storage=st, executor=ex, preflight=gates))
    assert "parameter check" in out["stopped"]
    out = run(DC.replay_saved("write_note", {"text": "x"}, approver=approve, storage=st, executor=ex,
                              preflight=gates))
    assert "missing parameter 'dateiname'" in out["stopped"]


def test_a_draft_workflow_is_never_replayed():
    st = storage()
    run(DC.draft_to_workflow(TE, storage=st))
    out = run(DC.replay_saved("write_note", {}, approver=None, storage=st,
                              preflight=lambda: {"may_drive": True}))
    assert not out["ok"] and "is a draft" in out["stopped"]


# ── 7. storage rules: shadow, verdicts, sessions, the inbox ──────────────────────────────────

def test_a_ready_workflow_is_never_replaced_by_a_new_compile():
    st = storage()
    first = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st))
    again = run(DC.draft_to_workflow(TE, overrides=[ov("notiz", "@c2"), ov("dateiname", "@c4", "file_name")],
                                     storage=st))
    assert first["status"] == "ready" and again["status"] == "draft"
    assert again["saved"]["name"].startswith("write_note.shadow-")
    assert any("shadow" in r for r in again["reasons"])
    _wf, rec = DC.find_saved("write_note", st)
    assert [p["name"] for p in rec["parameters"]] == ["text", "dateiname"]     # untouched
    # the SAME draft and parameters again: a shadow with its own id; the ready files untouched
    ready_file = os.path.join(first["saved"]["dir"], DC.RECORD_FILE)
    before = open(ready_file, "rb").read()
    same = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st))
    assert same["saved"]["workflow_id"] != first["saved"]["workflow_id"] and same["status"] == "draft"
    assert open(ready_file, "rb").read() == before
    # the verdict keys are the same for the same compile (stable over runs, ids and timestamps)
    assert same["proof"]["passed"] and DC.find_saved(same["saved"]["name"], st)[1]["verdict_keys"][
        "artifact_digest"] == rec["verdict_keys"]["artifact_digest"]
    # a draft of the same name IS replaced in place
    st2 = storage()
    d1 = run(DC.draft_to_workflow(TE, storage=st2))
    d2 = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st2))
    assert d1["status"] == "draft" and d2["status"] == "ready" and d2["saved"]["workflow_id"] == d1["saved"]["workflow_id"]


def test_a_verdict_hook_keeps_an_audited_wrong_compile_from_becoming_ready():
    seen = []

    def verdicts(keys):
        seen.append(keys)
        return "audited wrong on 2026-10-08 (fixture)"
    rep = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, verdict_check=verdicts, save=False))
    assert rep["status"] == "draft" and any("verdict" in r for r in rep["reasons"])
    assert set(seen[0]) == {"draft_digest", "parameters_digest", "artifact_digest"}


def test_watcher_sessions_and_the_inbox_are_listed_and_loaded():
    from cryptography.fernet import Fernet
    from anyuse.watcher.models import WatchSession
    from anyuse.watcher.raw_store import RawArtifactCrypto
    from anyuse.watcher.storage import SessionStorage
    data = json.load(open(TE))
    sess = WatchSession.from_dict({"id": "s-fixture-te", "started_at": "2026-10-08T10:00:00",
                                   "events": data["events"], "anchors": data["anchors"],
                                   "status": "stopped"})
    sdir = tempfile.mkdtemp(prefix="anyuse-d2w-sess-")
    ss = SessionStorage(base_dir=pathlib.Path(sdir), crypto=RawArtifactCrypto(key=Fernet.generate_key()))
    ss.save(sess)
    inbox = tempfile.mkdtemp(prefix="anyuse-d2w-inbox-")
    shutil.copy(WEB, inbox)
    rows = DC.list_drafts(session_storage=ss, inbox=inbox)
    assert {r["id"] for r in rows} == {"s-fixture-te/write_note", "web_form_draft"}
    sess_row = next(r for r in rows if r["source"] == "watcher_session")
    assert sess_row["steps"] == 5 and sess_row["complete"]
    src = DC.load_source("s-fixture-te/write_note", session_storage=ss)
    assert src.kind == "watcher_session" and len(src.steps) == 5 and src.attribution["session_id"] == "s-fixture-te"
    # without the fixture's ax_states the session draft has no drift baseline: said, not guessed
    rec = DC.compile_record(src, [], DP.collect_candidates(src.steps))
    assert rec["drift_profile"]["baseline_steps"] == 0 and rec["drift_profile"]["note"]
    assert DC.load_source("web_form_draft", inbox=inbox).name == "spende_erfassen"
    try:
        DC.load_source("s-fixture-te/nope", session_storage=ss)
        raise AssertionError("expected DraftError")
    except DC.DraftError as exc:
        assert "no workflow" in str(exc)


def test_agent_play_refuses_a_draft_compiled_workflow():
    st = storage()
    run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st))
    wf = st.get("write_note")
    why = DC.play_refusal(st, wf)
    assert why and "anyuse workflow replay" in why
    from anyuse.memory.models import Workflow
    plain = Workflow.create_new("plain")
    st.save(plain)
    assert DC.play_refusal(st, plain) is None


# ── 7b. further guards ──────────────────────────────────────────────────────────────────────

def test_a_drifted_screen_never_falls_back_to_a_vision_model_even_with_the_drive_gate_on():
    rec, cands = _record()
    prev = os.environ.get("ANYUSE_DRIVE_ENABLED")
    os.environ["ANYUSE_DRIVE_ENABLED"] = "1"            # the fake world sends nothing either way
    try:
        proof = run(PR.prove(rec, cands, world_factory=lambda r: PR.SimAXWorld(r, drop_titles=("Ablage",))))
    finally:
        if prev is None:
            os.environ.pop("ANYUSE_DRIVE_ENABLED", None)
        else:
            os.environ["ANYUSE_DRIVE_ENABLED"] = prev
    assert not proof["passed"] and "vision model" in proof["reason"]
    assert all(c["llm_calls"] == 0 for c in proof["cases"])
    # a placeholder fingerprint is no baseline (the gate would always fire on it)
    data = json.load(open(TE))
    data["ax_states"]["te0"] = {}
    src = DC.load_source(data)
    assert src.steps[0].state_id == "" and "te0" not in src.states


def test_the_drive_recording_loader_refuses_shortcuts_and_untyped_text():
    rec = json.load(open(DRIVE))
    rec["plan"].append({"intent": "press Return", "source": "workflow", "target_element": "x",
                        "modality": "ax", "command": {"role": "AXTextArea", "title": "", "action": "press_key",
                                                      "keycode": 12, "flags": 1048576,
                                                      "selector": {"canonical_role": "textarea", "name": "",
                                                                   "identifier": "First Text View",
                                                                   "path": "", "raw_role": "AXTextArea"}}})
    rec["plan"][4]["input_data"] = ""
    src = DC.load_source(rec)
    assert "not one an AX replay presses" in src.steps[-1].problem
    assert src.steps[4].problem == "a typing step without the typed text"
    rep = run(DC.draft_to_workflow(rec, overrides=[ov("dateiname", "@c6", "file_name")], save=False))
    assert rep["status"] == "draft"
    rec["plan"][4]["input_data"] = {"text": "not a string"}           # not text at all: malformed
    try:
        DC.load_source(rec)
        raise AssertionError("expected MalformedDraft")
    except DC.MalformedDraft as exc:
        assert "recording.plan[4].input_data must be text" in str(exc)


def test_a_recorded_value_reused_as_a_press_target_fails_the_proof():
    data = json.load(open(TE))
    data["events"].insert(6, {"id": "e9", "tag_type": "STEP", "timestamp": "2026-10-08T10:00:09",
                              # a press target named by the recorded value (static text: a cell
                              # click is select_row with its list's identity, test_ax_runner_gaps.py)
                              "data": {"action": "click", "selector": {
                                  "canonical_role": "text", "name": "notiz-2026-10-08.txt", "identifier": "",
                                  "path": "", "raw_role": "AXStaticText"}}})
    rep = run(DC.draft_to_workflow(data, overrides=TE_PARAMS, save=False))
    assert rep["status"] == "draft" and "reached step 5" in rep["proof"]["reason"]
    rec, cands = _record()
    proof = run(PR.prove(rec, cands, fresh=[{"text": "x Einkaufsliste: Milch, Brot, Kaffee", "dateiname": "a.txt"},
                                            {"text": "neu", "dateiname": "b.txt"}]))
    assert not proof["passed"] and "contains the recorded value" in proof["reason"]


def test_german_parameter_names_bind():
    rep = run(DC.draft_to_workflow(TE, overrides=[ov("Einkaufstext_für_Küche", "@c2"),
                                                  ov("Dateiname", "@c4", "file_name")], save=False))
    assert rep["status"] == "ready", rep["reasons"]
    assert DC.bind_values({"a": "${Einkaufstext_für_Küche}", "b": ["${x}"]},
                          {"Einkaufstext_für_Küche": "Ä"}) == {"a": "Ä", "b": ["${x}"]}
    src = DC.load_source(TE)
    assert DP.validate_proposal({"parameters": [{"candidate": "c2", "name": "text\n", "type": "text",
                                                 "description": ""}]}, DP.collect_candidates(src.steps))[1]


def test_every_step_is_put_to_the_gate_whatever_the_saved_list_says():
    st = storage()
    run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st))
    _wf, rec = DC.find_saved("write_note", st)
    rec["approvals"]["gated_steps"] = []                 # an edited or stale record
    world = PR.SimAXWorld(rec)
    asked = []

    async def approve(ctx):
        asked.append(ctx["args"].get("target"))
        return True
    out = run(DC.run_steps(rec, {"text": "neu", "dateiname": "n.txt"}, executor=DC.checked_executor(world),
                           approver=approve, decide_as_driving=True, on_step=world.at_step))
    assert out["ok"] and len(asked) == 5 and asked[1] == "textarea First Text View"


def test_no_callable_web_rung_is_left_by_a_draft_and_bad_inbox_files_do_not_break_the_list():
    from anyuse.execution.api_verify import cases_from_recordings
    src_dir = _HERE / "testdata" / "compile"
    d = tempfile.mkdtemp(prefix="anyuse-d2w-web-")
    for f in ("crates_search.rec.json", "crates_search.fresh1.json", "crates_search.fresh2.json"):
        shutil.copy(src_dir / f, d)
    fresh = [json.load(open(src_dir / f)) for f in ("crates_search.fresh1.json", "crates_search.fresh2.json")]
    ui = {c["value"]: c["ui_items"] for c in cases_from_recordings(fresh, "serde")}
    st = storage()
    kw = dict(storage=st, api_executor=_ReplayExecutor(ui),
              permission_checker=lambda url, hosts_path=None: _Allowed())
    first = run(DC.draft_to_workflow(d, **kw))
    assert first["status"] == "ready" and isinstance(first["proof"]["cases"], list)
    shadow = run(DC.draft_to_workflow(d, **kw))
    assert shadow["status"] == "draft" and "compiled_api" not in shadow["saved"]
    assert not os.path.exists(os.path.join(shadow["saved"]["dir"], "compiled_api.json"))
    vetoed = run(DC.draft_to_workflow(d, name="other", verdict_check=lambda k: "audited wrong", **kw))
    assert vetoed["status"] == "draft" and not os.path.exists(os.path.join(vetoed["saved"]["dir"],
                                                                          "compiled_api.json"))
    inbox = tempfile.mkdtemp(prefix="anyuse-d2w-inbox-")
    shutil.copy(TE, inbox)
    for bad, body in (("broken.json", "{not json"), ("listy.json", json.dumps({"events": [], "name": "x",
                                                                                "ax_states": [1, 2]})),
                      ("drive.json", json.dumps({"plan": "x", "graph": {}}))):
        open(os.path.join(inbox, bad), "w").write(body)
    rows = {r["id"]: r for r in DC.list_drafts(session_storage=_NoSessions(), inbox=inbox)}
    assert "textedit_draft" in rows and "error" in rows["broken.json"] and "error" in rows["drive.json"]


class _NoSessions:
    def list_sessions(self):
        return []

    def load(self, sid):
        return None


# ── 8. the CLI ───────────────────────────────────────────────────────────────────────────────

def _cli(args, data_dir):
    from click.testing import CliRunner
    from anyuse.cli import cli
    return CliRunner().invoke(cli, args, env={"ANYUSE_DATA_DIR": data_dir, "ANYUSE_DRIVE_ENABLED": ""})


def test_the_cli_from_draft_drafts_and_replay():
    data = tempfile.mkdtemp(prefix="anyuse-d2w-cli-")
    os.makedirs(os.path.join(data, "drafts"))
    shutil.copy(TE, os.path.join(data, "drafts"))
    res = _cli(["workflow", "drafts", "--json"], data)
    assert res.exit_code == 0, res.output
    assert [r["id"] for r in json.loads(res.output)] == ["textedit_draft"]
    # no parameter chosen: the candidates, the model's price, saved as a draft (exit 1)
    res = _cli(["workflow", "from-draft", "textedit_draft"], data)
    assert res.exit_code == 1 and "c4" in res.output and "--propose" in res.output, res.output
    assert "Status: DRAFT" in res.output
    # chosen with --param: proved and saved ready (exit 0)
    res = _cli(["workflow", "from-draft", TE, "--param", "text=@c2",
                "--param", "dateiname:file_name=notiz-2026-10-08.txt", "--json"], data)
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["status"] == "ready" and out["saved"]["name"] == "write_note"
    assert os.path.isfile(os.path.join(data, "workflows", f"workflow_{out['saved']['workflow_id']}",
                                       "draft_workflow.json"))
    # --dry-run writes nothing
    res = _cli(["workflow", "from-draft", WEB, "--param", "name=@c1", "--dry-run"], data)
    assert "(--dry-run: nothing written)" in res.output
    assert not any(n.startswith("spende") for n in WorkflowStorage(base_dir=pathlib.Path(data, "workflows")).list_names())
    # the replay refuses on this machine (the drive gate is off) and sends nothing
    res = _cli(["workflow", "replay", "write_note", "--param", "text=neu", "--param", "dateiname=n.txt"], data)
    assert res.exit_code == 1 and "ANYUSE_DRIVE_ENABLED is off" in res.output
    # an audit verdict on the compile: every later replay is refused, saying why
    res = _cli(["workflow", "verdict", "write_note", "--verdict", "wrong", "--why", "falscher Ordner"], data)
    assert res.exit_code == 0 and "wrong recorded on the compile" in res.output, res.output
    res = _cli(["workflow", "replay", "write_note", "--param", "text=neu", "--param", "dateiname=n.txt"], data)
    assert res.exit_code == 1 and "this compile was judged wrong" in res.output and "falscher Ordner" in res.output
    res = _cli(["workflow", "from-draft", TE, "--param", "text"], data)
    assert res.exit_code != 0 and "NAME=CANDIDATE" in res.output


def test_n18_n19_the_list_shows_status_and_parameters_and_no_rate_before_a_run():
    data = tempfile.mkdtemp(prefix="anyuse-d2w-list-")
    res = _cli(["workflow", "from-draft", TE, "--param", "text=@c2",
                "--param", "dateiname:file_name=notiz-2026-10-08.txt"], data)
    assert res.exit_code == 0, res.output
    res = _cli(["workflow", "from-draft", RO], data)                 # a fixed routine
    assert res.exit_code == 0 and "Status: READY" in res.output, res.output
    from anyuse.memory.models import Workflow
    store = WorkflowStorage(base_dir=pathlib.Path(data, "workflows"))
    store.save(Workflow(id="plain001", name="plain_never_run", description="recorded, never run"))
    store.save(Workflow(id="plain002", name="learned_twice", description="replayed twice",
                        run_count=2, success_count=2))
    res = _cli(["workflow", "list"], data)
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    note = next(ln for ln in lines if "play write_note" in ln)
    assert note.strip().startswith("ready, ") and note.endswith("play write_note --param text=VALUE --param dateiname=VALUE")
    routine = next(ln for ln in lines if "play kontostand_ansehen" in ln)
    assert routine.strip().startswith("ready, 1 step(s)") and routine.endswith("play kontostand_ansehen")
    assert "Success: not run yet" in res.output
    # no verifier ships: the status is what the replays did, never a "Not verified" nothing clears
    assert "Not verified" not in res.output
    assert "❓ plain_never_run" in res.output and "play plain_never_run" in res.output
    assert "✅ learned_twice" in res.output and "Runs: 2 | Success: 100%" in res.output


def test_a_learned_workflow_is_renamed_with_its_template_and_a_compiled_one_is_not():
    data = tempfile.mkdtemp(prefix="anyuse-d2w-rename-")
    from anyuse.memory.models import Workflow, WorkflowTemplate
    store = WorkflowStorage(base_dir=pathlib.Path(data, "workflows"))
    long = "open_textedit_and_create_a_new_document_containing_the_text_"
    wf = Workflow(id="learn001", name=long, description="learned", run_count=1, success_count=1)
    store.save(wf)
    store.save_template(wf.id, WorkflowTemplate(id="t1", name=long, required_app="TextEdit"))
    res = _cli(["workflow", "rename", long, "new_note_learned"], data)
    assert res.exit_code == 0 and "Renamed" in res.output, res.output
    fresh = WorkflowStorage(base_dir=pathlib.Path(data, "workflows"))
    assert fresh.get(long) is None and fresh.get("new_note_learned").id == "learn001"
    assert fresh.get_template("learn001").name == "new_note_learned"
    assert "new_note_learned" in _cli(["workflow", "list"], data).output
    # refused: an unknown name, a bad name, a taken name, a compiled recording
    assert _cli(["workflow", "rename", "nope", "x"], data).exit_code != 0
    assert "letters, digits" in _cli(["workflow", "rename", "new_note_learned", "a b"], data).output
    store = WorkflowStorage(base_dir=pathlib.Path(data, "workflows"))
    store.save(Workflow(id="other01", name="other"))
    assert "exists already" in _cli(["workflow", "rename", "new_note_learned", "other"], data).output
    res = _cli(["workflow", "from-draft", RO], data)
    assert res.exit_code == 0 and "Status: READY" in res.output, res.output
    res = _cli(["workflow", "rename", "kontostand_ansehen", "balance"], data)
    assert res.exit_code != 0 and "from-draft DRAFT --name balance" in res.output, res.output


def test_n23_the_recorders_hint_offers_a_typed_value_only_when_one_was_typed(capsys, monkeypatch):
    import types
    from anyuse import cli as C
    monkeypatch.setenv("ANYUSE_WATCH_STRUCTURED_STEPS", "")         # restored after the test

    class _Watcher:
        def __init__(self, data):
            self.data = data

        async def start(self):
            return None

        async def stop(self):
            ev = types.SimpleNamespace(workflow_name="steps_schach_1", data=self.data)
            return types.SimpleNamespace(id="s1", structured_workflow_names=["steps_schach_1"], step_events=[ev])
    C._record_steps(0.01, watcher=_Watcher({"action": "click"}))
    out = capsys.readouterr().out
    assert "--param" not in out and "fixed routine" in out and "from-draft s1/steps_schach_1" in out
    C._record_steps(0.01, watcher=_Watcher({"action": "type", "value_ref": "vault:abc"}))
    out = capsys.readouterr().out
    assert "--param text=@cN" in out and "play NAME --param text=" in out

# ── 9. the prepared VM end-to-end, its host-safe half ──────

def _load_script(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(_HERE / "execution" / "guest" / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 10. follow-up: verdicts, versions, spend, the approval line, presence, splits ────────────

def _vstore():
    from anyuse.execution.verdict_store import VerdictStore
    return VerdictStore(os.path.join(tempfile.mkdtemp(prefix="anyuse-d2w-v-"), "verdicts.jsonl"))


def test_a_compile_audited_wrong_stays_a_draft_until_a_later_verdict_says_otherwise():
    from anyuse.execution import draft_verdicts as DV
    vs, st = _vstore(), storage()
    rep = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st, verdicts=vs))
    assert rep["status"] == "ready" and rep["verdict"]["source"] == "fresh-input proof"
    _wf, rec = DC.find_saved("write_note", st)
    assert DV.record(rec, "wrong", why="the file name was the old one", ref="audit-1", store=vs)
    assert DV.record(rec, "wrong", why="the file name was the old one", ref="audit-1", store=vs) is None
    again = run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, save=False, verdicts=vs))
    assert again["status"] == "draft" and again["verdict"]["verdict"] == "disagrees"
    assert any("audit audit-1" in r and "wrong" in r and "old one" in r for r in again["reasons"]), again["reasons"]
    from anyuse.execution import verdict_store as VSM
    assert vs.rows()[0]["site"] == "draft:textedit" and vs.rows()[0]["input"] == VSM.input_ref("compile")
    # the same compile with other parameters is another compile: not matched
    other = run(DC.draft_to_workflow(TE, overrides=[ov("notiz", "@c2")], save=False, verdicts=vs))
    assert other["status"] == "ready"
    # a later "correct" supersedes (the store's rule: the latest verdict wins)
    import time
    time.sleep(1.1)
    DV.record(rec, "correct", why="re-audited", ref="audit-2", store=vs)
    assert run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, save=False, verdicts=vs))["status"] == "ready"


def test_a_replay_audited_wrong_is_refused_never_repeated_silently():
    from anyuse.execution import draft_verdicts as DV
    vs, st = _vstore(), storage()
    run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st, verdicts=vs))
    _wf, rec = DC.find_saved("write_note", st)
    gates = lambda: {"may_drive": True, "drive_enabled": True, "interlock": {"allowed": True}}  # noqa: E731
    bad = {"text": "Liste", "dateiname": "liste.txt"}
    DV.record(rec, "wrong", why="wrote into the wrong document", values=bad, store=vs)
    spy = _Spy()

    async def approve(_ctx):
        raise AssertionError("a known-wrong replay asks nobody")
    out = run(DC.replay_saved("write_note", bad, approver=approve, storage=st, executor=spy, preflight=gates,
                              verdicts=vs))
    assert not out["ok"] and "known wrong: this replay was judged wrong" in out["stopped"] and spy.calls == 0
    assert "wrong document" in out["stopped"]
    # other values are another replay: it runs
    world = PR.SimAXWorld(rec)

    async def ok(_ctx):
        return True
    out = run(DC.replay_saved("write_note", {"text": "Andere", "dateiname": "andere.txt"}, approver=ok,
                              storage=st, executor=DC.checked_executor(world), preflight=gates,
                              on_step=world.at_step, verdicts=vs))
    assert out["ok"], out["stopped"]
    # once the compile itself is judged wrong, no replay of it runs
    DV.record(rec, "wrong", why="the save step is the wrong menu", store=vs)
    out = run(DC.replay_saved("write_note", {"text": "Andere", "dateiname": "andere.txt"}, approver=ok,
                              storage=st, executor=spy, preflight=gates, verdicts=vs))
    assert "this compile was judged wrong" in out["stopped"] and spy.calls == 0


def test_a_record_of_an_unknown_or_newer_compiler_is_refused():
    st = storage()
    run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st, verdicts=_vstore()))
    wf, rec = DC.find_saved("write_note", st)
    path = os.path.join(str(st.get_workflow_dir(wf.id)), DC.RECORD_FILE)
    gates = lambda: {"may_drive": True}  # noqa: E731
    newer = DC.COMPILER["version"] + 1
    for change, want in (({"compiler": {"name": "draft_compile", "version": newer}}, "Update AnyUse"),
                         ({"compiler": {"name": "other", "version": 1}}, "unknown compiler"),
                         ({"compiler": None}, "unknown compiler"),
                         ({"format": "anyuse.draft-workflow/9"}, "cannot run it")):
        json.dump(dict(rec, **change), open(path, "w"))
        out = run(DC.replay_saved("write_note", {"text": "a", "dateiname": "b.txt"}, approver=None,
                                  storage=st, executor=_Spy(), preflight=gates, verdicts=_vstore()))
        assert not out["ok"] and want in out["stopped"], (change, out["stopped"])


def test_proposals_are_logged_and_capped_per_day():
    data = tempfile.mkdtemp(prefix="anyuse-d2w-spend-")
    prev = {k: os.environ.get(k) for k in ("ANYUSE_DATA_DIR", DP.DAILY_CAP_ENV)}
    answer = {"parameters": [{"candidate": "c2", "name": "text", "type": "text", "description": ""}]}

    def llm_tokens(messages, schema):
        return json.dumps(answer), {"provider": "fake", "model": "fake", "calls": 1,
                                    "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
    calls = []

    def counting(messages, schema):
        calls.append(1)
        return llm_tokens(messages, schema)
    try:
        os.environ["ANYUSE_DATA_DIR"] = data
        os.environ[DP.DAILY_CAP_ENV] = "0.0015"
        src = DC.load_source(TE)
        cands = DP.collect_candidates(src.steps)
        first = DP.propose(src.goal, cands, llm=counting)
        assert first["parameters"] and abs(first["cost_usd"] - (1000 * 0.25 + 500 * 0.69) / 1e6) < 1e-9
        rows = [json.loads(x) for x in open(DP.spend_log_path())]
        assert len(rows) == 1 and rows[0]["cost_basis"] == "tokens x list price" and rows[0]["calls"] == 1
        second = DP.propose(src.goal, cands, llm=counting)            # 0.0006 spent + ~0.0012 bound > 0.0015
        assert second["parameters"] == [] and "daily cap" in second["problems"][0] and len(calls) == 1
        os.environ[DP.DAILY_CAP_ENV] = "lots"
        third = DP.propose(src.goal, cands, llm=counting)
        assert "is not a number" in third["problems"][0] and len(calls) == 1
        # a sent request with no usage reported is booked at the estimate's upper bound
        os.environ[DP.DAILY_CAP_ENV] = "1"

        def failing(messages, schema):
            raise DP.ModelCallFailed("HTTP 500", {"calls": 1})
        out = DP.propose(src.goal, cands, llm=failing)
        rows = [json.loads(x) for x in open(DP.spend_log_path())]
        assert "no model answer" in out["problems"][0] and rows[-1]["cost_basis"].startswith("upper bound")
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_approval_line_shows_the_whole_value_or_says_it_is_cut():
    from anyuse.cli import _approval_value, APPROVAL_VALUE_INLINE
    shown, full = _approval_value("Einkaufsliste: Äpfel")
    assert shown == "Einkaufsliste: Äpfel" and full is None
    long = "Zeile " * 200
    shown, full = _approval_value(long)
    assert full == long and f"CUT at 300 of {len(long)} characters" in shown and len(long) > APPROVAL_VALUE_INLINE


def test_a_target_the_recorded_state_does_not_show_fails_the_proof():
    data = json.load(open(TE))
    fp = data["ax_states"]["te4"]["fingerprint"]
    fp["required_elements"] = [r for r in fp["required_elements"] if r["title"] != "Sichern" or r["role"] != "AXButton"]
    rep = run(DC.draft_to_workflow(data, overrides=TE_PARAMS, save=False, verdicts=_vstore()))
    assert rep["status"] == "draft"
    assert "does not show its target button:'Sichern'" in rep["proof"]["reason"], rep["proof"]["reason"]


def test_an_override_splits_equal_values_into_parameters_by_step():
    data = json.load(open(TE))
    data["events"][4]["data"]["text"] = "Bericht"
    data["events"][2]["data"]["text"] = "Bericht"
    src = DC.load_source(data)
    c = [x for x in DP.collect_candidates(src.steps) if x.source == "typed"]
    assert len(c) == 1 and c[0].steps == [1, 3]                         # equal values: one candidate
    rep = run(DC.draft_to_workflow(data, overrides=[ov("inhalt", f"@{c[0].id}#1"),
                                                    ov("dateiname", f"@{c[0].id}#3", "file_name")],
                                   save=False, verdicts=_vstore()))
    assert rep["status"] == "ready", rep["reasons"]
    occ = {p["name"]: p["occurrences"] for p in rep["parameters"]}
    assert occ == {"inhalt": [1], "dateiname": [3]}
    vals = rep["proof"]["cases"][0]["values"]
    assert vals["inhalt"] != vals["dateiname"]
    # only one occurrence split off: the other keeps the recorded text, on purpose, not as a leak
    rep = run(DC.draft_to_workflow(data, overrides=[ov("inhalt", f"@{c[0].id}#1")], save=False,
                                   verdicts=_vstore()))
    assert rep["status"] == "ready", rep["reasons"]
    assert {x["id"]: x["steps"] for x in rep["candidates"] if x["source"] == "typed"} == {
        c[0].id: [3], f"{c[0].id}#1": [1]}
    # a step the candidate does not occur at is refused with the steps it does
    rep = run(DC.draft_to_workflow(data, overrides=[ov("inhalt", f"@{c[0].id}#2")], save=False,
                                   verdicts=_vstore()))
    assert rep["status"] == "draft" and any("does not occur at draft step 2" in r for r in rep["reasons"])


def test_a_wrong_after_a_correct_is_stored_again_and_wins_within_the_same_second():
    from anyuse.execution import draft_verdicts as DV
    vs, st = _vstore(), storage()
    run(DC.draft_to_workflow(TE, overrides=TE_PARAMS, storage=st, verdicts=vs))
    _wf, rec = DC.find_saved("write_note", st)
    vals = {"text": "Liste", "dateiname": "liste.txt"}
    for values in (None, vals):
        for verdict in ("wrong", "correct", "wrong"):        # same source, same (empty) reference
            assert DV.record(rec, verdict, values=values, store=vs), (values, verdict)
        assert DV.lookup(vs, rec, values)["verdict"] == "wrong"
        assert DV.record(rec, "wrong", values=values, store=vs) is None   # still the latest: not repeated
    assert DV.replay_refusal(rec, vals, vs)
    assert DV.values_problem(rec, {"text": "x"}) and DV.values_problem(rec, dict(vals, extra="y"))
    assert DV.values_problem(rec, {"text": "x" * (DV.MAX_VALUE + 1), "dateiname": "a.txt"})
    assert DV.values_problem(rec, vals) == ""


def test_a_target_whose_role_no_fingerprint_lists_is_not_checked_for_presence():
    data = json.load(open(TE))
    for ev in data["events"]:
        sel = (ev.get("data") or {}).get("selector") or {}
        if sel.get("name") == "Sichern":
            # an image: never in required_elements (a cell click is select_row, test_ax_runner_gaps.py)
            sel.update(raw_role="AXImage", canonical_role="image")
    rep = run(DC.draft_to_workflow(data, overrides=TE_PARAMS, save=False, verdicts=_vstore()))
    assert rep["status"] == "ready", rep["reasons"]


def test_a_spend_log_that_cannot_be_read_or_booked_stops_the_call_before_it_is_made():
    from datetime import datetime, timezone
    data = tempfile.mkdtemp(prefix="anyuse-d2w-spend2-")
    prev = os.environ.get("ANYUSE_DATA_DIR")
    calls = []

    def llm(messages, schema):
        calls.append(1)
        return json.dumps({"parameters": []}), {"provider": "fake", "model": "fake", "calls": 1}
    try:
        os.environ["ANYUSE_DATA_DIR"] = data
        src = DC.load_source(TE)
        cands = DP.collect_candidates(src.steps)
        log = DP.spend_log_path()
        os.makedirs(os.path.dirname(log), exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        for row in (f'{{"at": "{today}", "cost_usd": NaN}}', f'{{"at": "{today}", "cost_usd": "viel"}}'):
            with open(log, "w") as fh:
                fh.write(row + "\n")
            out = DP.propose(src.goal, cands, llm=llm)
            assert "cannot be read as amounts" in out["problems"][0] and not calls, out["problems"]
            assert out["budget"]["spent_today_usd"] is None
        with open(log, "w") as fh:
            fh.write('[1, 2]\n')                                     # not a row: skipped
        assert DP.spent_today_usd() == 0.0
        os.remove(log)
        os.makedirs(log)                                             # neither readable nor writable
        out = DP.propose(src.goal, cands, llm=llm)
        assert out["problems"][0].startswith("no model call") and not calls, out["problems"]
    finally:
        if prev is None:
            os.environ.pop("ANYUSE_DATA_DIR", None)
        else:
            os.environ["ANYUSE_DATA_DIR"] = prev


def test_a_malformed_web_recording_is_a_malformed_draft():
    for content in ("[]", '"x"', "{"):
        d = tempfile.mkdtemp(prefix="anyuse-d2w-webbad-")
        with open(os.path.join(d, "shop.rec.json"), "w") as fh:
            fh.write(content)
        try:
            DC.load_source(d)
        except DC.MalformedDraft as exc:
            assert "shop.rec.json" in str(exc)
        else:
            raise AssertionError(content)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
