"""The guest additions (``execution/guest/desktop_e2e_job.py``), offline with FAKES.

Pinned here:

* ``replay_draft``: a READY draft workflow replayed in the guest with new values through ``replay_saved`` (the seam
  ``anyuse workflow replay`` runs) on the recorded app's one process: it asks nothing at level ``auto``, makes no
  model call, refuses a draft that is not ready, stops at a level that holds (no person in the guest), and answers
  the interlock's typed refusal when a viewer attaches. Two such replays are the guest proof a draft's MCP tool needs:
  ``guest_proof`` -> ``compiled_record_for_draft`` -> ``write_draft_tool`` -> ``call_operation`` reaches ``run_tool``.
* every command answers its checked menu presses (``menu_checked``), reset per command;
* ``open_docs`` opens only this test's files (under GEN), in TextEdit or Finder only; ``launch_read`` launches only the
  read setups' apps; ``record_te`` records only the front document named.

No desktop, no AX, no subprocess, no network, no VM.
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import types

import pytest

from anyuse.execution import desktop_tools as DTools
from anyuse.execution import draft_compile as DC
from anyuse.execution import draft_proof as PR
from anyuse.execution import foreground_interlock as FI
from anyuse.memory.storage import WorkflowStorage
from anyuse.mcpserver.server import create_server

import test_desktop_choice_identity as TC
import test_desktop_job_interlock as TJ
import test_desktop_tools_mcp as TM

VALUES = [{"name": "A", "betrag": "1,00", "zahlungsart": "Rechnung"},
          {"name": "B", "betrag": "2,00", "zahlungsart": "Lastschrift"}]


@pytest.fixture
def guest(monkeypatch):
    """The REAL job agent in process: a clear guest interlock, a fake app lookup and the recorded world as its AX
    tree; its replay storage in a temporary folder (never the host's home)."""
    monkeypatch.delenv(FI.TEST_BYPASS_ENV, raising=False)
    monkeypatch.delenv(DTools.GUEST_SESSION_ENV, raising=False)
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    state = {"now": TJ.CLEAR}
    monkeypatch.setattr(FI, "probe_state", lambda: state["now"])
    J = TM._job()
    tmp = tempfile.mkdtemp(prefix="anyuse-vmchecks-")
    monkeypatch.setattr(J, "REPLAY_STORE", os.path.join(tmp, "replay"))
    asked = {"apps": [], "worlds": []}

    def app_pid(app):
        asked["apps"].append(dict(app))
        return 4242, None
    monkeypatch.setattr(J, "app_pid", app_pid)
    storage = WorkflowStorage(base_dir=pathlib.Path(tmp, "workflows"))

    def runner(op, timeout=None, **args):
        resp, _stop = J.handle(op, json.loads(json.dumps(args, default=str)))
        return resp
    return types.SimpleNamespace(J=J, runner=runner, asked=asked, state=state, storage=storage, mp=monkeypatch,
                                 tmp=tmp)


def ready_draft(storage):
    ovs = [{"name": "name", "ref": "@c1"}, {"name": "betrag", "ref": "@c2", "type": "number"},
           {"name": "zahlungsart", "ref": "@c3"}]
    rep = asyncio.run(DC.draft_to_workflow(TC.web_draft(), overrides=ovs, save=True, storage=storage,
                                           name="spende_erfassen"))
    assert rep["status"] == "ready", rep["reasons"]
    return DC.find_saved("spende_erfassen", storage)[1]


def bind_world(g, draft):
    def app_tree(pid):
        w = PR.SimAXWorld(draft)
        g.asked["worlds"].append(w)
        return w
    g.mp.setattr(g.J, "app_tree", app_tree)


def typed(world):
    return [e["value"] for e in world.effects if e["op"] == "enter_text"]


def test_a_ready_draft_replays_in_the_guest_with_new_values_asking_nothing_and_no_model_call(guest):
    draft = ready_draft(guest.storage)
    bind_world(guest, draft)
    out = guest.runner("replay_draft", record=draft, values=VALUES[0])
    assert out["status"] == "done" and out["ok"], out
    assert out["served_by"] == "guest:replay_saved" and out["llm_calls"] == 0 and out["holds"] == 0
    assert out["approval"] == {"level": "auto", "mode": "off"}
    assert out["preflight"] == {"may_drive": True, "drive_enabled": True}
    assert typed(guest.asked["worlds"][-1]) == ["A", "1,00"]
    assert guest.asked["apps"] == [{"bundle_id": draft.get("bundle_id") or "", "name": draft["app"]}]
    # the replay ran from a fresh storage of its own under the guest's replay store
    assert os.path.realpath(out["storage"]).startswith(os.path.realpath(os.path.join(guest.tmp, "replay")))


def test_two_guest_replays_prove_the_draft_and_its_mcp_tool_runs_in_the_guest(guest):
    draft = ready_draft(guest.storage)
    bind_world(guest, draft)
    rows = []
    for v in VALUES:
        out = guest.runner("replay_draft", record=draft, values=v)
        rows.append({"values": v, "ok": bool(out.get("ok")) and typed(guest.asked["worlds"][-1])[:1] == [v["name"]],
                     "model_calls": out.get("llm_calls", 0)})
    recorded = {p["name"]: p.get("recorded") for p in draft["parameters"]}
    proof = DTools.guest_proof(rows, recorded=recorded, job="2412", session="vc1", commit="abc1234")
    assert proof["proved"], proof["problems"]
    rec = DTools.compiled_record_for_draft(draft, proof)
    DTools.write_draft_tool(guest.storage, rec)
    new = {"name": "Neu", "betrag": "9,99", "zahlungsart": "Rechnung"}
    out = TM.call(create_server(profile="meta", desktop_storage=guest.storage, desktop_guest_runner=guest.runner),
                  "call_operation", {"site": draft["app"], "operation": "spende_erfassen", "args": new})
    assert out["ok"], out
    assert typed(guest.asked["worlds"][-1]) == ["Neu", "9,99"]


def test_a_draft_that_is_not_ready_is_refused_and_nothing_is_touched(guest):
    draft = dict(ready_draft(guest.storage), status="draft", status_reasons=["needs_fill: a gap"])
    bind_world(guest, draft)
    out = guest.runner("replay_draft", record=draft, values=VALUES[0])
    assert out["status"] == "refused" and "not ready" in out["detail"]
    assert not guest.asked["apps"] and not guest.asked["worlds"]
    out = guest.runner("replay_draft", record={}, values={})
    assert out["status"] == "refused"


def test_a_level_that_holds_stops_with_no_person_in_the_guest(guest):
    draft = ready_draft(guest.storage)
    draft["approval"] = dict(draft.get("approval") or {}, level="all")
    bind_world(guest, draft)
    out = guest.runner("replay_draft", record=draft, values=VALUES[0])
    assert out["status"] == "done" and not out["ok"], out
    assert out["approval"]["level"] == "all"
    assert typed(guest.asked["worlds"][-1]) == []


def test_a_viewer_in_the_guest_gets_the_typed_interlock_refusal_and_nothing_runs(guest):
    draft = ready_draft(guest.storage)
    bind_world(guest, draft)
    guest.state["now"] = TJ.STATES["a screen-sharing viewer"]
    out = guest.runner("replay_draft", record=draft, values=VALUES[0])
    assert out["status"] == "refused" and (out.get("refusal") or {}).get("kind") == "foreground_interlock", out
    assert not guest.asked["worlds"]


def test_a_bad_value_is_refused_before_anything_runs(guest):
    draft = ready_draft(guest.storage)
    bind_world(guest, draft)
    out = guest.runner("replay_draft", record=draft, values=dict(VALUES[0], name="A\tB"))
    assert not out["ok"] and "parameter check" in str(out["stopped"])
    assert all(typed(w) == [] for w in guest.asked["worlds"])


def test_every_command_answers_its_checked_menu_presses_reset_per_command(guest):
    J = guest.J

    def op_menu(a):
        J.STATE.setdefault("menu_checked", []).append({"action": "press", "success": True, "refused": False})
        return {"status": "done"}
    guest.mp.setitem(J.OPS, "fake_menu", op_menu)
    assert guest.runner("fake_menu")["menu_checked"] == [{"action": "press", "success": True, "refused": False}]
    assert "menu_checked" not in guest.runner("front_none")          # an unknown op: no presses, no field
    assert len(guest.runner("fake_menu")["menu_checked"]) == 1       # reset per command


def test_open_docs_and_the_read_setups_stay_on_their_lists(guest):
    out = guest.runner("open_docs", bundle="com.apple.TextEdit", paths=["~/Documents/x.txt"])
    assert out["status"] == "error" and "not this test's files" in out["detail"]
    out = guest.runner("open_docs", bundle="com.apple.Notes", paths=[guest.J.GEN + "/x"])
    assert out["status"] == "error" and "TextEdit documents or Finder folders" in out["detail"]
    out = guest.runner("launch_read", bundle="com.apple.Terminal")
    assert out["status"] == "error" and "read setups' apps" in out["detail"]
    out = guest.runner("launch_read", bundle="com.apple.calculator", lang="fr")
    assert out["status"] == "error"
    out = guest.runner("quit_read", bundle="com.apple.Safari")
    assert out["status"] == "error"
    out = guest.runner("record_te", doc="~/Documents/x.txt", text="Hallo")
    assert out["status"] == "error" and "record_te needs this test's document" in out["detail"]
    out = guest.runner("record_te", doc=guest.J.GEN + "/x.txt", text="")
    assert out["status"] == "error" and "record_te needs this test's document" in out["detail"]
    guest.mp.setattr(guest.J, "pid_of", lambda bid: 4242)
    guest.mp.setattr(guest.J, "front_window", lambda pid: None)
    out = guest.runner("record_te", doc=guest.J.GEN + "/x.txt", text="Hallo")
    assert out["status"] == "done" and not out["ok"] and "not TextEdit's front window" in out["stopped"]


def test_record_te_records_the_front_text_area_and_save_menu_steps(guest):
    from anyuse.context.accessibility import AXElementDesc
    from anyuse.execution import executor

    J, mp = guest.J, guest.mp
    drive = J._drive_module()
    doc, text = J.GEN + "/recorded.txt", "Hallo aus der Aufnahme"
    front, other = object(), object()
    root = {"AXFocusedWindow": front}
    refs = [
        ({"AXWindow": other}, AXElementDesc(role="AXTextArea", identifier="background-text")),
        ({"AXWindow": front}, AXElementDesc(role="AXTextArea", identifier="front-text")),
        ({}, AXElementDesc(role="AXMenuBarItem", title="Bearbeiten", in_menubar=True)),
        ({}, AXElementDesc(role="AXMenuBarItem", title="Ablage", in_menubar=True)),
        ({"AXMenuItemCmdChar": "s", "AXMenuItemCmdModifiers": 1},
         AXElementDesc(role="AXMenuItem", title="Sichern unter", in_menubar=True)),
        ({"AXMenuItemCmdChar": "s", "AXMenuItemCmdModifiers": 0},
         AXElementDesc(role="AXMenuItem", title="Sichern", in_menubar=True)),
    ]
    selected, executed, guards = [], [], []

    class Tree:
        def _root(self):
            return root

        def _walk(self, el, depth, descs, out):
            descs.extend(d for _el, d in refs)
            out.extend(refs)

        def to_selector(self, el):
            d = next(d for candidate, d in refs if candidate is el)
            selected.append(d.identifier or d.title)
            return d.to_selector()

    tree = Tree()

    class CheckedExecutor:
        def __init__(self, *, ax_tree, drift_gate_enabled, hil_gate):
            assert ax_tree is tree and drift_gate_enabled

        async def execute_checked_step(self, step, *, step_idx, workflow, state_graph):
            assert workflow.steps[step_idx].expected_before_state == f"s{step_idx}"
            assert f"s{step_idx}" in state_graph.states
            executed.append(step.to_dict())
            return {"success": True, "method": "ax", "llm_calls": 0}, {}

    mp.setattr(J, "_drive_module", lambda: drive)
    mp.setattr(drive, "_attr", lambda el, name: el.get(name))
    mp.setattr(J, "pid_of", lambda bid: 4242 if bid == J.TEXTEDIT else None)
    mp.setattr(J, "front_window", lambda pid: front)
    mp.setattr(J, "doc_path", lambda w: doc if w is front else None)
    mp.setattr(J, "app_tree", lambda pid: tree)
    mp.setattr(J, "guard_before", lambda pid, where: guards.append(("before", pid, where)))
    mp.setattr(J, "guard_after", lambda pid, where: guards.append(("after", pid, where)))
    mp.setattr(J, "file_info", lambda path: {"path": path})
    mp.setattr(J.time, "sleep", lambda seconds: None)
    mp.setattr(executor, "StateGraphExecutor", CheckedExecutor)

    out = guest.runner("record_te", doc=doc, text=text)
    assert out["status"] == "done" and out["ok"], out
    assert selected == ["front-text", "Ablage", "Sichern"]
    assert [s["command"]["action"] for s in executed] == ["enter_text", "press", "press"]
    assert executed[0]["input_data"] == text and executed[0]["command"]["document"] == doc
    recording = out["recording"]
    assert recording["plan"] == executed and recording["text"] == text and recording["name"] == "recorded.txt"
    assert len(recording["workflow"]["steps"]) == len(recording["graph"]["states"]) == 3
    assert all(r["success"] and r["llm_calls"] == 0 for r in out["results"])
    assert guards == [("before", 4242, "record_te"), ("after", 4242, "record_te")]


def test_relay_model_timeout_records_the_request_and_accounts_for_the_call(guest, tmp_path):
    drive = guest.J._drive_module()
    now = [0.0]
    relay = drive.RelayModel(str(tmp_path), wait_s=1, poll_s=0.25, clock=lambda: now[0],
                             sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    messages, schema = [{"role": "user", "content": "fill"}], {"type": "object"}
    with pytest.raises(drive.RelayError, match="no relay answer to request 1") as exc:
        relay(messages, schema)
    assert exc.value.meta == {"calls": 1} and now[0] == 1
    request = json.loads((tmp_path / "req-1.json").read_text())
    assert request["protocol"] == drive.RELAY_PROTOCOL and request["n"] == 1
    assert request["messages"] == messages and request["schema"] == schema


@pytest.mark.parametrize("response", [
    b"not json",
    b"[]",
    {"protocol": "other", "n": 1, "ok": True, "text": "done", "meta": {}},
    {"protocol": "anyuse.vm-relay/1", "n": 2, "ok": True, "text": "done", "meta": {}},
    {"protocol": "anyuse.vm-relay/1", "n": 1, "ok": "yes", "text": "done", "meta": {}},
    {"protocol": "anyuse.vm-relay/1", "n": 1, "ok": True, "text": "done", "meta": []},
    {"protocol": "anyuse.vm-relay/1", "n": 1, "ok": True, "text": None, "meta": {}},
])
def test_relay_model_refuses_malformed_responses(guest, tmp_path, response):
    drive = guest.J._drive_module()
    raw = response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")
    (tmp_path / "resp-1.json").write_bytes(raw)
    with pytest.raises(drive.RelayError, match="malformed relay answer to request 1") as exc:
        drive.RelayModel(str(tmp_path))([], {})
    assert exc.value.meta == {"calls": 1}
    assert (tmp_path / "req-1.json").is_file()


def test_relay_model_reports_host_model_failures_with_the_hosts_metadata(guest, tmp_path):
    from anyuse.execution.draft_params import ModelCallFailed
    drive = guest.J._drive_module()
    meta = {"calls": 1, "tokens_in": 42}
    (tmp_path / "resp-1.json").write_text(json.dumps({
        "protocol": drive.RELAY_PROTOCOL, "n": 1, "ok": False, "error": "model unavailable", "meta": meta,
    }))
    with pytest.raises(ModelCallFailed, match="the host's model: model unavailable") as exc:
        drive.RelayModel(str(tmp_path))([], {})
    assert exc.value.meta == meta
    assert (tmp_path / "req-1.json").is_file()


def test_fill_draft_takes_only_a_needs_fill_draft_and_a_new_relay_folder_under_its_root(guest):
    from anyuse.execution import draft_fill as DF
    draft = ready_draft(guest.storage)
    guest.mp.setattr(guest.J, "RELAY_ROOT", os.path.join(guest.tmp, "relay"))
    out = guest.runner("fill_draft", record=draft, values=VALUES[0], relay=os.path.join(guest.tmp, "relay", "r1"))
    assert out["status"] == "refused" and "needs_fill" in out["detail"]            # a ready record is no fill
    nf = dict(draft, status="needs_fill")
    out = guest.runner("fill_draft", record=nf, values=VALUES[0], relay=os.path.join(guest.tmp, "elsewhere"))
    assert out["status"] == "refused" and "relay folder" in out["detail"]
    os.makedirs(os.path.join(guest.tmp, "relay", "used"))
    out = guest.runner("fill_draft", record=nf, values=VALUES[0], relay=os.path.join(guest.tmp, "relay", "used"))
    assert out["status"] == "refused"                                              # one relay folder per fill
    assert not guest.asked["apps"]
    drive = guest.J._drive_module()
    rdir = os.path.join(guest.tmp, "relay", "roundtrip")
    os.makedirs(rdir)
    with open(os.path.join(rdir, "resp-1.json"), "w", encoding="utf-8") as fh:
        json.dump({"protocol": drive.RELAY_PROTOCOL, "n": 1, "ok": True, "text": "{\"action\":\"done\"}",
                   "meta": {"calls": 1}}, fh)
    text, meta = drive.RelayModel(rdir, wait_s=0.1, poll_s=0.01)([{"role": "user", "content": "x"}], {})
    assert text == "{\"action\":\"done\"}" and meta == {"calls": 1}
    assert os.path.isfile(os.path.join(rdir, "req-1.json"))

    bind_world(guest, draft)
    live_relay = os.path.join(guest.tmp, "relay", "live-fill")

    async def fake_fill_saved(name, values, *, approver, llm, **kw):
        assert type(llm).__name__ == "RelayModel" and llm.dir == os.path.realpath(live_relay)
        await approver({"args": {"intent": "fill step"}, "step_class": "mutating"})
        with open(os.path.join(live_relay, "resp-1.json"), "w", encoding="utf-8") as fh:
            json.dump({"protocol": drive.RELAY_PROTOCOL, "n": 1, "ok": True, "text": "{\"ok\":true}",
                       "meta": {"calls": 1}}, fh)
        ans, m = llm([{"role": "user", "content": "fill"}], {"type": "object"})
        assert ans == "{\"ok\":true}" and m == {"calls": 1}
        return {"ok": True, "status": "ready", "reasons": [],
                "proof": {"passed": True, "reason": "ok", "harness": "fake"},
                "model": {"calls": 1}, "steps": []}

    guest.mp.setattr(DF, "fill_saved", fake_fill_saved)
    out = guest.runner("fill_draft", record=nf, values=VALUES[0], relay=live_relay)
    assert out["status"] == "done" and out["ok"] and out["served_by"] == "guest:fill_saved", out
    assert out["holds"] == 1 and out["model_calls"] == 1 and out["record"] is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_a_replay_answer_carries_the_steps_own_evidence():
    """desktop-followups re-checks: a state-aware toggle's states and an app switch's polls / answering source reach the
    host in replay_draft's step briefs (draft_compile puts them in the replay entry; the brief used to drop them)."""
    J = TM._job()
    entry = {"index": 2, "intent": "x", "success": False, "error": "e", "method": "ax", "pressed": True,
             "state_before": "on", "state_after": "on", "target_state": "off", "reason_code": "deviation",
             "activation": {"polls": [{"t": 0.0, "front_source": "ax"}]}, "front_source": "ax", "front": "TextEdit",
             "not_a_brief_key": 1}
    b = J._step_brief(entry)
    assert {k: b[k] for k in ("pressed", "state_before", "state_after", "target_state", "front_source")} == {
        "pressed": True, "state_before": "on", "state_after": "on", "target_state": "off", "front_source": "ax"}
    assert b["activation"]["polls"][0]["front_source"] == "ax" and "not_a_brief_key" not in b
    assert J.OPS["menu_mark"] is J.op_menu_mark
