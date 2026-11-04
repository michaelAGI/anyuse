"""Every success compiled (execution/harness_compile.py), offline.
Pinned here, WEB on the fake shop (generated DOMs): a scripted-policy run through the harness is a
valid demonstration trace, ``demo_compile`` compiles it, the tool is proved on fresh values at 0 model
calls (an adapter tripwire would fail any call), saved as ``compiled_demo.json`` and replayed again
from the saved file; a run that did not finish, an invalid demonstration and a failed proof stay
drafts with their reasons; a ready tool is never replaced. DESKTOP on the fake TextEdit guest: the
run's trace becomes the guest drive harness's recording, the unchanged draft seam compiles it with
the run's own values as parameters (no model proposal), proves it on the recorded AX world, saves it
``ready``; ``replay_saved`` with fresh values asks nothing and makes exactly 0 model calls; a run
whose typed values are not the task's values stays a draft. No browser, AX, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_harness_compile.py
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

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import draft_proof as PR  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import model_adapter as M  # noqa: E402
from anyuse.memory.storage import WorkflowStorage  # noqa: E402

import test_agent_harness as TA  # noqa: E402
import test_repair_ladder as SHOP  # noqa: E402

BASE, SITE = SHOP.BASE, SHOP.SITE


def storage():
    return WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-hc-")))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Tripwire:
    """Any adapter request during a replay fails the test."""

    def __enter__(self):
        self.real = M.ModelAdapter._complete
        self.mark = M.process_call_count()

        def boom(*_a, **_k):
            raise AssertionError("a model was called during a replay")
        M.ModelAdapter._complete = boom
        return self

    def __exit__(self, *_exc):
        M.ModelAdapter._complete = self.real
        assert M.calls_since(self.mark) == 0, "the process counter moved during a replay"


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------
def web_run(value="Bosch", site=None, script=None, task="search the shop for {q}"):
    site = site or SHOP.Shop()
    tools = HT.BrowserTools(D.DomPage(site), BASE + "/", SITE)
    pol = P.ScriptedPolicy(script or [lambda t, o, h: T.Search(0, t.split("for ")[-1]), T.ReadTable(), T.Done()])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="web1").run(task, {"q": value})
    return tr, site


def test_a_harness_run_is_a_valid_demonstration_and_compiles():
    tr, site = web_run()
    assert tr.ok and tr.task == "search the shop for Bosch" and tr.values == {"q": "Bosch"}
    demo = HC.trace_to_demo(tr)
    assert demo["workflow"] == "site_search" and demo["value"] == "Bosch" and demo["stop"] == "done"
    assert [s["action"]["action"] for s in demo["steps"]] == ["search", "done"]       # reads are not demo steps
    assert demo["steps"][0]["outcome"]["input"]["name"] == "q" and demo["steps"][0]["page"]["url"] == BASE + "/"
    assert demo["steps"][1]["page"]["url"].startswith(BASE + "/search?q=Bosch")
    assert demo["result"]["results"] and all("/p/" in r["url"] for r in demo["result"]["results"])
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    tool = c["tool"]
    assert tool["search"]["mode"] == "url" and "{q}" in tool["search"]["template"] and tool["model_calls"] == 0


def test_the_compiled_tool_replays_on_fresh_values_at_zero_calls_and_is_saved_ready():
    tr, site = web_run()
    st = storage()
    with Tripwire():
        rep = HC.compile_and_save_web(tr, st, site, ["Makita", "Ryobi"])
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["proof"]["passed"] and len(rep["proof"]["cases"]) == 2 and rep["proof"]["model_calls"] == 0
    assert all(c["status"] == "ok" and c["n_results"] >= 1 for c in rep["proof"]["cases"])
    assert rep["model_calls"] == 0 and rep["name"] == "www_ladder-shop_test__site_search"
    saved = HC.load_web_tool(st, rep["name"])
    assert saved["status"] == "ready" and saved["tool_sha256"] and os.path.basename(rep["saved"]["path"]) == "compiled_demo.json"
    # the saved tool runs again from the file, on another fresh value, at 0 calls
    with Tripwire():
        out = HC.replay_web(saved["tool"], site, "Metabo")
    assert out["status"] == "ok" and out["model_calls"] == 0 and out["output"]["results"][0]["url"].endswith("/p/metabo-powermaxx")
    # a nonsense value: the tool says no_results, which the proof accepts as an honest answer
    with Tripwire():
        assert HC.replay_web(saved["tool"], site, "Zxqvbn Plorf")["status"] == "no_results"


def test_the_demonstrated_value_is_never_a_fresh_value_and_the_recorded_status_is_checked():
    tr, site = web_run()
    tool = HC.compile_web(tr)["tool"]
    p = HC.prove_web(tool, site, ["Bosch", "Makita"], demonstrated="Bosch")
    assert not p.get("passed") and "demonstrated value" in p["reason"]
    p = HC.prove_web(tool, site, ["Makita", "Ryobi"], expect={"Ryobi": "no_results"})
    assert not p["passed"] and "recorded run says no_results" in p["reason"]
    p = HC.prove_web(tool, site, ["Makita"])
    assert not p["passed"] and "2 needed" in p["reason"]


def test_unfinished_invalid_and_unproven_runs_stay_drafts_with_reasons():
    st = storage()
    tr, site = web_run(script=[T.Fail("gave up")])
    rep = HC.compile_and_save_web(tr, st, site, ["Makita", "Ryobi"])
    assert rep["status"] == "draft" and "did not finish" in rep["reasons"][0] and rep["saved"] is None
    # finished, but the value never entered the site's search: not a valid demonstration
    tr, site = web_run(script=[T.ReadTable(), T.Done({"results": [{"title": "x", "url": BASE + "/p/bosch-gsr-12v"}]})])
    rep = HC.compile_and_save_web(tr, st, site, ["Makita", "Ryobi"])
    assert rep["status"] == "draft" and "not a valid demonstration" in rep["reasons"][0]
    # a proof that fails on a fresh value: saved as a draft file, never as a tool
    tr, site = web_run()

    class Flaky(SHOP.Shop):
        def route(self, url):
            if "Makita" in url:
                return D.Recorded(url, D.NOT_FOUND, {}, 500), url
            return super().route(url)
    # the caller's values are the proof's first inputs: "Makita" fails (a 500), so the tool stays a draft
    rep = HC.compile_and_save_web(tr, st, Flaky(), ["Makita", "Ryobi"], name="flaky")
    assert rep["status"] == "draft" and "Makita" in rep["reasons"][0]
    assert not rep["proof"]["skipped_values"]
    assert os.path.basename(rep["saved"]["path"]) == "compiled_demo.draft.json"
    assert HC.load_web_tool(st, "flaky")["status"] == "draft"


def test_a_ready_tool_is_never_replaced_by_a_new_compile():
    tr, site = web_run()
    st = storage()
    first = HC.compile_and_save_web(tr, st, site, ["Makita", "Ryobi"], name="shop_search")
    assert first["status"] == "ready"
    second = HC.compile_and_save_web(tr, st, site, ["Makita", "Ryobi"], name="shop_search")
    assert second["status"] == "draft" and "shadow" in second["reasons"][0]
    assert HC.load_web_tool(st, "shop_search")["status"] == "ready"


# ---------------------------------------------------------------------------
# desktop
# ---------------------------------------------------------------------------
def desktop_run(values, task="write {text} into a new TextEdit document and save it as {dateiname}", script=None):
    g = TA.FakeAXGuest()
    tools = HT.DesktopTools(g, "TextEdit", "com.apple.TextEdit")
    text, name = values["text"], values["dateiname"]
    script = script or [lambda t, o, h: T.AxPress(TA.idx(o, text="Ablage")),
                        lambda t, o, h: T.AxPress(TA.idx(o, text="Neu")),
                        lambda t, o, h: T.AxEnterText(TA.idx(o, role="AXTextArea"), text),
                        lambda t, o, h: T.AxPress(TA.idx(o, text="Ablage")),
                        lambda t, o, h: T.AxPress(TA.idx(o, text="Sichern …")),
                        lambda t, o, h: T.AxEnterText(TA.idx(o, role="AXTextField"), name),
                        lambda t, o, h: T.AxPress(TA.idx(o, text="Sichern")), T.Done({"saved": True})]
    tr = H.Harness(HT.Toolbox(desktop=tools), P.ScriptedPolicy(script), H.HarnessConfig(), run_id="dt1").run(task, values)
    return tr, g


def test_the_trace_renders_as_the_guest_drive_recording():
    from anyuse.execution import draft_schema
    tr, g = desktop_run({"text": "Einkauf: Milch", "dateiname": "liste.txt"})
    assert tr.ok and g.saved == [("liste.txt", "Einkauf: Milch")]
    rec = HC.trace_to_drive_recording(tr, "write_note")
    draft_schema.check_drive_recording(rec)                                   # the seam's own schema
    assert len(rec["plan"]) == 7 == len(rec["workflow"]["steps"]) and rec["workflow"]["required_app"] == "TextEdit"
    assert [p["command"]["action"] for p in rec["plan"]] == ["press", "press", "enter_text", "press", "press", "enter_text", "press"]
    assert rec["plan"][2]["input_data"] == "Einkauf: Milch" and rec["plan"][2]["command"]["selector"]["identifier"] == "doc.text"
    states = rec["graph"]["states"]
    assert set(s["expected_before_state"] for s in rec["workflow"]["steps"]) <= set(states)
    sheet = states[rec["workflow"]["steps"][5]["expected_before_state"]]
    assert any(r["role"] == "AXTextField" for r in sheet["fingerprint"]["required_elements"])
    src = DC.load_source(rec)
    assert src.kind == "drive_recording" and not [s for s in src.steps if s.problem]
    from anyuse.execution import draft_params as DP
    cands = {c.id: c for c in DP.collect_candidates(src.steps)}
    ov = HC.value_overrides(rec, tr.values)
    assert [o["name"] for o in ov] == ["text", "dateiname"]
    assert [cands[o["ref"][1:]].value for o in ov] == ["Einkauf: Milch", "liste.txt"]
    assert all(cands[o["ref"][1:]].source == "typed" for o in ov)
    # the menu items pressed are the seam's "chosen" candidates; they are never bound by the harness
    assert {c.source for c in cands.values()} == {"chosen", "typed"}


def test_the_desktop_success_compiles_proves_saves_and_replays_at_zero_calls_asking_nothing():
    tr, g = desktop_run({"text": "Einkauf: Milch", "dateiname": "liste.txt"})
    st = storage()
    rep = run(HC.compile_desktop(tr, st, name="write_note"))
    assert rep["status"] == "ready", rep["reasons"]
    assert rep["model_calls"] == 0 and rep["parameter_choice"]["how"] == "override"
    assert [p["name"] for p in rep["parameters"]] == ["text", "dateiname"]
    assert rep["proof"]["passed"] and rep["proof"]["passed_cases"] >= 2 and rep["unreplayable"] == []
    wf, rec = DC.find_saved("write_note", st)
    assert rec["status"] == "ready" and rec["attribution"]["draft_kind"] == "drive_recording"
    assert "agent harness run" in rec["attribution"]["taught_by"]
    # a replay with fresh values: through the checked executor on the recorded AX world, no question asked
    world = PR.SimAXWorld(rec)
    with Tripwire():
        out = run(HC.replay_desktop(st, "write_note", {"text": "Neue Liste: Äpfel", "dateiname": "neu.txt"}, world=world))
    assert out["ok"], out["stopped"]
    assert out["asked"] == 0 and out["model_calls"] == 0 and out["approval"]["level"] == "auto"
    assert [e["value"] for e in world.effects if e["op"] == "enter_text"] == ["Neue Liste: Äpfel", "neu.txt"]
    assert sum(s.get("llm_calls", 0) for s in out["steps"]) == 0
    # a deviation stops with its reason: a missing value never reaches a step
    out = run(HC.replay_desktop(st, "write_note", {"text": "x"}, world=PR.SimAXWorld(rec)))
    assert not out["ok"] and "missing parameter 'dateiname'" in out["stopped"]


def test_a_desktop_run_whose_typed_text_is_not_a_task_value_stays_a_draft():
    tr, g = desktop_run({"text": "Einkauf: Milch", "dateiname": "liste.txt"},
                        script=[lambda t, o, h: T.AxPress(TA.idx(o, text="Ablage")),
                                lambda t, o, h: T.AxPress(TA.idx(o, text="Neu")),
                                lambda t, o, h: T.AxEnterText(TA.idx(o, role="AXTextArea"), "something else"),
                                T.Done()])
    st = storage()
    rep = run(HC.compile_desktop(tr, st, name="odd"))
    assert rep["status"] == "draft" and any("no parameter" in r for r in rep["reasons"])
    assert any("never typed" in n for n in rep.get("notes", []))
    tr, g = desktop_run({"text": "a", "dateiname": "b.txt"}, script=[T.Fail("no")])
    rep = run(HC.compile_desktop(tr, st, name="odd2"))
    assert rep["status"] == "draft" and "did not finish" in rep["reasons"][0]


def test_compile_success_dispatches_by_kind():
    tr, site = web_run()
    st = storage()
    assert HC.compile_success(tr, st, site=site, fresh_values=["Makita", "Ryobi"])["status"] == "ready"
    tr, g = desktop_run({"text": "a b", "dateiname": "c.txt"})
    assert HC.compile_success(tr, st, name="d")["status"] == "ready"
    other = T.Trace(kind="http", task="t", stop="done")
    assert HC.compile_success(other, st)["status"] == "draft"


def test_run_agent_task_forwards_fresh_sets_to_desktop_compile():
    from anyuse.execution import harness_product as HP
    st = storage()
    g = TA.FakeAXGuest()
    values = {"text": "Einkauf: Milch", "dateiname": "liste.txt"}
    script = [
        lambda t, o, h: T.AxPress(TA.idx(o, text="Ablage")),
        lambda t, o, h: T.AxPress(TA.idx(o, text="Neu")),
        lambda t, o, h: T.AxEnterText(TA.idx(o, role="AXTextArea"), values["text"]),
        lambda t, o, h: T.AxPress(TA.idx(o, text="Ablage")),
        lambda t, o, h: T.AxPress(TA.idx(o, text="Sichern …")),
        lambda t, o, h: T.AxEnterText(TA.idx(o, role="AXTextField"), values["dateiname"]),
        lambda t, o, h: T.AxPress(TA.idx(o, text="Sichern")),
        T.Done({"saved": True}),
    ]
    fresh_sets = [
        {"text": "Frisch 1", "dateiname": "f1.txt"},
        {"text": "Frisch 2", "dateiname": "f2.txt"},
    ]
    res = HP.run_agent_task(
        "write {text} into a new TextEdit document and save it as {dateiname}",
        values=values,
        app="com.apple.TextEdit",
        guest=g,
        policy_obj=P.ScriptedPolicy(script),
        compile=True,
        fresh=fresh_sets,
        name="write_note_fresh",
        storage=st,
        judge=None,
    )
    assert res["ok"] and res["compiled"]["status"] == "ready"
    cases = res["compiled"]["proof"]["cases"]
    assert [c["values"] for c in cases] == fresh_sets
    _, saved_rec = DC.find_saved("write_note_fresh", st)
    assert saved_rec["bundle_id"] == "com.apple.TextEdit"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
