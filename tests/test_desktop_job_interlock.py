"""The guest job agent behind the foreground interlock (``execution/guest/desktop_e2e_job.py``), offline
with FAKES: a fake AX world (the GF10 world), a fake checked executor, a fake Quartz and AppKit, and fake
session states fed to ``foreground_interlock``'s real rules (``probe_state`` replaced; no sysctl, ioreg or netstat).

Pinned here:

* every input handler asks ``foreground_interlock.require_clear`` first: AX actions and value writes (``_AS``),
  the checked executor (``EX``: presses, text, keys), the posted click, app activation, launch, quit, force-quit;
* host: always refused; guest: only this process's own audit session on the console, unlocked, no screen-sharing
  viewer; the console rule is enforced even where ``foreground_interlock.evaluate`` alone would allow;
* a refused command answers a TYPED refusal (status ``refused``, ``refusal.kind`` ``foreground_interlock``,
  where and why) and sends nothing; a refusal is never swallowed by an op's ``except Exception`` fallback;
* with the interlock clear, the same commands act (fakes record the actions).

No desktop, no AX, no subprocess, no network.
"""
import importlib
import inspect
import json
import os
import re
import sys
import types

import pytest

from anyuse.execution import foreground_interlock as FI

import test_desktop_view_probe as T13

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VM = os.path.join(HERE, "execution", "guest")
PID = T13.PID
OWN = 100001


def _job():
    if VM not in sys.path:
        sys.path.insert(0, VM)
    return importlib.import_module("desktop_e2e_job")


def guest(viewer=False, locked=None, own_on_console=True, other_on_console=False, is_vm=True):
    sessions = [FI.ConsoleSession(uid=501, user="admin", on_console=own_on_console, login_done=True,
                                  locked=locked, audit_id=OWN)]
    if other_on_console:
        sessions.append(FI.ConsoleSession(uid=502, user="other", on_console=True, login_done=True,
                                          audit_id=OWN + 7))
    v = FI.ViewerState(True, "ESTABLISHED 192.168.64.3.5900<-192.168.64.1.50123") if viewer else \
        FI.ViewerState(False, "no ESTABLISHED connection on ports 5900/3283")
    return FI.SessionState(is_vm=is_vm, own_uid=501, sessions=sessions, viewer=v, own_asid=OWN)


CLEAR = guest()
STATES = {
    "a screen-sharing viewer": guest(viewer=True),
    "the screen locked": guest(locked=True),
    "another session on the console": guest(own_on_console=False, other_on_console=True),
    "the own session in the background": guest(own_on_console=False),
    "a second session on the console too": guest(other_on_console=True),
    "the host": guest(is_vm=False),
}


class FakeApp:
    def __init__(self, log):
        self.log = log

    def processIdentifier(self):
        return PID

    def activateWithOptions_(self, opt):
        self.log.append(("activate", opt))
        return True

    def terminate(self):
        self.log.append(("terminate",))

    def forceTerminate(self):
        self.log.append(("forceTerminate",))


@pytest.fixture
def world(monkeypatch):
    J = _job()
    G, G2 = J.G, J.G2
    monkeypatch.delenv(FI.TEST_BYPASS_ENV, raising=False)
    st = T13.gf10_state()
    ax, ex, apps = T13.FakeAS(st["app"]), T13.FakeEX(), []
    state = {"now": CLEAR}
    monkeypatch.setattr(FI, "probe_state", lambda: state["now"])
    monkeypatch.setattr(J, "_AS_BASE", lambda: ax)                  # the guard wraps the fake
    monkeypatch.setattr(J, "EX", J.GuardedExec(ex))
    monkeypatch.setattr(J, "_at", lambda el, name: el.a.get(name) if isinstance(el, T13.El) else None)
    monkeypatch.setattr(J, "_s", lambda v: None if v is None else str(v))
    monkeypatch.setattr(J, "running", lambda bid: FakeApp(apps))
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSApplicationActivateIgnoringOtherApps=2))
    monkeypatch.setattr(J, "front_window", lambda pid: st["w"])
    monkeypatch.setattr(J, "guard_before", lambda pid, where: None)
    monkeypatch.setattr(J, "guard_after", lambda pid, where: None)
    monkeypatch.setattr(J, "_frame", lambda el: (10.0, 100.0, 180.0, 24.0))
    monkeypatch.setattr(G, "record_selector", lambda pid, el: {"role": J.role(el)})
    monkeypatch.setattr(J.time, "sleep", lambda s: None)
    monkeypatch.setattr(G, "_AS", G._AS)                              # restored after install_guard
    monkeypatch.setattr(G2, "_AS", G2._AS)
    return types.SimpleNamespace(J=J, st=st, ax=ax, ex=ex, apps=apps, state=state)


def _sent(w):
    """Every input the fakes saw: AX actions and writes, executor runs, activations."""
    return ([c for c in w.ax.calls if c[0] in ("action", "set")] + [("run", a) for _e, a, _k in w.ex.runs]
            + [a for a in w.apps])


# ── the rules ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_guest_rule_own_session_on_the_console_unlocked_no_viewer():
    J = _job()
    ok = FI.evaluate(CLEAR, "t")
    assert ok.allowed and J.guest_console_refusal(ok) is None
    for name, st in STATES.items():
        v = FI.evaluate(st, "t")
        why = None if not v.allowed else J.guest_console_refusal(v)
        assert (not v.allowed) or why, name
    # a background own session: refused by foreground_interlock itself AND by the job agent's
    # second check, which refuses it even from a verdict that says allowed
    bg = FI.evaluate(STATES["the own session in the background"], "t")
    assert not bg.allowed
    claimed = FI.Verdict(True, "t", "x", "vm-guest", detail=bg.detail)
    assert "console" in J.guest_console_refusal(claimed)
    two = FI.evaluate(STATES["a second session on the console too"], "t")
    assert "console" in (J.guest_console_refusal(FI.Verdict(True, "t", "x", "vm-guest", detail=two.detail)) or "")
    # host: refused here even if a verdict ever said otherwise
    fake_host = FI.Verdict(True, "t", "x", "host", detail=ok.detail)
    assert J.guest_console_refusal(fake_host).startswith("host:")


def test_a_refusal_is_not_an_ordinary_exception_so_no_fallback_swallows_it():
    J = _job()
    assert not issubclass(J.InterlockRefused, Exception) and issubclass(J.InterlockRefused, BaseException)


# ── the job agent refuses when the interlock is not clear ─────────────────────────────────────────────────

def _table_world(w, monkeypatch):
    st = T13.gf10_state(table=True)
    w.st.update(st)
    w.ax.app = st["app"]
    st["sf"].a["AXValue"] = "Ersatzdichtung"
    w.ax.at_point = st["rows"][2].a["AXChildren"][0].a["AXChildren"][0]       # the hit test names the row
    posted = []
    quartz = types.SimpleNamespace(CGPointMake=lambda x, y: (x, y), kCGEventMouseMoved=5, kCGEventLeftMouseDown=1,
                                   kCGEventLeftMouseUp=2, kCGMouseButtonLeft=0, kCGHIDEventTap=0,
                                   CGEventCreateMouseEvent=lambda src, t, pt, b: (t, pt),
                                   CGEventPost=lambda tap, ev: posted.append(ev))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    return posted


HANDLERS = (({"act": "leave_escape"}, "executor press_key"),             # a key through the checked executor
            ({"act": "leave_empty"}, "executor enter_text"),              # text through the checked executor
            ({"act": "focus_list"}, "AX value write AXFocused"),          # an AX value write
            ({"act": "click_folder", "label": "Werkstatt Dichtungen"}, "posted click"),
            ({"act": "relaunch"}, "quit com.apple.Notes"))                 # app quit (then launch)


@pytest.mark.parametrize("why", sorted(STATES))
def test_every_input_handler_refuses_with_a_typed_refusal_and_sends_nothing(world, monkeypatch, why):
    """The activation is let through (the state turns bad right after it), so each handler's OWN input
    primitive meets the refusal."""
    J = world.J
    posted = _table_world(world, monkeypatch)
    for args, where in HANDLERS:
        seq = iter([CLEAR])
        world.J.FI.probe_state = lambda: next(seq, STATES[why])
        world.apps.clear()
        resp, stop = J.handle("probe_act", args)
        assert resp["status"] == "refused" and not stop, (why, args, resp)
        ref = resp["refusal"]
        assert ref["kind"] == "foreground_interlock" and resp["refused_by"] == "foreground_interlock"
        assert ref["where"] == f"desktop_e2e_job: {where}" and ref["reason"], (ref, where)
        assert ref["inputs_sent_before"] == 1 and world.apps == [("activate", 2)]      # only the activation
        assert resp["detail"].startswith("refused by foreground interlock: ")
    assert [c for c in world.ax.calls if c[0] in ("action", "set")] == [] and world.ex.runs == [] and posted == []
    # refused from the first input on: not even the activation
    world.J.FI.probe_state = lambda: STATES[why]
    world.apps.clear()
    resp, _ = J.handle("probe_act", {"act": "leave_escape"})
    assert resp["status"] == "refused" and resp["refusal"]["inputs_sent_before"] == 0 and world.apps == []


def test_ax_actions_and_value_writes_through_the_guard_refuse_before_the_call(world):
    J = world.J
    world.state["now"] = STATES["a screen-sharing viewer"]
    with pytest.raises(J.InterlockRefused) as exc:
        J._AS().AXUIElementPerformAction(world.st["w"], "AXPress")
    assert "screen-sharing viewer" in exc.value.verdict.reason and "AX action AXPress" in exc.value.verdict.where
    with pytest.raises(J.InterlockRefused) as exc:
        J._AS().AXUIElementSetAttributeValue(world.st["sf"], "AXValue", "geheimer Text")
    assert "geheimer Text" not in str(exc.value) and "geheimer" not in exc.value.verdict.where
    assert J._AS().AXUIElementCreateApplication(PID) is world.st["app"]      # a read passes
    assert _sent(world) == []


def test_the_refusal_survives_an_ops_broad_except(world):
    J = world.J
    world.state["now"] = STATES["the screen locked"]

    def helper():
        try:
            J._AS().AXUIElementSetAttributeValue(world.st["w"], "AXPosition", (0, 0))
        except Exception:                                         # noqa: BLE001  (as canonical_frame does)
            return None
    with pytest.raises(J.InterlockRefused):
        helper()


def test_the_helper_modules_route_their_ax_calls_through_the_same_guard(world):
    J = world.J
    J.install_guard()
    assert J.G._AS is J._AS and J.G2._AS is J._AS
    world.state["now"] = STATES["a screen-sharing viewer"]
    with pytest.raises(J.InterlockRefused):
        J.G2._AS().AXUIElementPerformAction(world.st["w"], "AXCancel")
    assert _sent(world) == []


def test_hello_reports_the_verdict_and_sends_nothing(world, monkeypatch):
    J = world.J
    monkeypatch.setattr(J.G2, "_session_state", lambda: {"locked": False, "auth_prompts": [], "apps": []})
    monkeypatch.setattr(J.G2, "_onscreen_windows", lambda: ([], []))
    monkeypatch.setattr(J, "prompt_windows", lambda: [])
    monkeypatch.setattr(J.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="boot"))
    world.state["now"] = STATES["a screen-sharing viewer"]
    r, _ = J.handle("hello", {})
    assert r["status"] == "done" and r["interlock"]["allowed"] is False
    assert "viewer" in r["interlock"]["reason"] and r["interlock"]["environment"] == "vm-guest"
    world.state["now"] = CLEAR
    r, _ = J.handle("hello", {})
    assert r["interlock"]["allowed"] is True and _sent(world) == []


# ── and acts when it is clear ──────────────────────────────────────────────────────────────────────────────

def test_with_the_interlock_clear_the_same_handlers_act(world, monkeypatch):
    J = world.J
    world.state["now"] = CLEAR
    world.st["sf"].a["AXValue"] = "Ersatzdichtung"
    r, _ = J.handle("probe_act", {"act": "leave_escape"})
    assert r["status"] == "done" and ("activate", 2) in world.apps
    assert [(a, kw) for _e, a, kw in world.ex.runs] == [("press_key", {"keycode": 53})]
    st = T13.gf10_state(table=True)
    world.st.update(st)
    world.ax.app = st["app"]
    r, _ = J.handle("probe_act", {"act": "focus_list"})
    assert r["status"] == "done" and ("set", "AXFocused", True) in world.ax.calls
    posted = []
    quartz = types.SimpleNamespace(CGPointMake=lambda x, y: (x, y), kCGEventMouseMoved=5, kCGEventLeftMouseDown=1,
                                   kCGEventLeftMouseUp=2, kCGMouseButtonLeft=0, kCGHIDEventTap=0,
                                   CGEventCreateMouseEvent=lambda src, t, pt, b: (t, pt),
                                   CGEventPost=lambda tap, ev: posted.append(ev))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "1")
    row = st["rows"][2]
    world.ax.at_point = row.a["AXChildren"][0].a["AXChildren"][0]
    r, _ = J.handle("probe_act", {"act": "click_folder", "label": "Werkstatt Dichtungen"})
    assert r["status"] == "done" and [ev[0] for ev in posted] == [5, 1, 2]
    # the viewer attaches: the very next click is refused before anything is posted
    posted.clear()
    world.state["now"] = STATES["a screen-sharing viewer"]
    r, _ = J.handle("probe_act", {"act": "click_folder", "label": "Werkstatt Dichtungen"})
    assert r["status"] == "refused" and posted == []


def test_a_refusal_in_the_middle_of_a_command_says_how_many_inputs_went_before(world):
    J = world.J
    seq = iter([CLEAR, STATES["a screen-sharing viewer"]])
    last = {"s": CLEAR}

    def probe():
        last["s"] = next(seq, last["s"])
        return last["s"]
    world.J.FI.probe_state = probe       # restored by the fixture's monkeypatch of the same attribute
    world.st["sf"].a["AXValue"] = "Ersatzdichtung"
    r, _ = J.handle("probe_act", {"act": "leave_escape"})
    assert r["status"] == "refused" and r["refusal"]["inputs_sent_before"] == 1      # the activation went first
    assert world.apps == [("activate", 2)] and world.ex.runs == []


# ── the source: no input primitive outside the guard ──────────────────────────────────────────────────────

RAW = ("activateWithOptions_", ".terminate()", "forceTerminate()", "CGEventPost", '"/usr/bin/open"')


def test_every_raw_input_primitive_in_the_job_agent_follows_an_interlock_call():
    J = _job()
    src = open(os.path.join(VM, "desktop_e2e_job.py"), encoding="utf-8").read()
    funcs = re.split(r"\n(?=def )", src)
    for f in funcs:
        for raw in RAW:
            at = f.find(raw)
            if at >= 0:
                assert "interlock(" in f[:at], (f[:60], raw)
    assert "EX = GuardedExec(G.Exec())" in src and "install_guard()" in inspect.getsource(J.serve)
    assert "_AS = G._AS" not in src.replace("_AS_BASE = G._AS", "")
    # the job agent still refuses to start outside a VM, before anything else
    assert inspect.getsource(J.serve).index("refuse_unless_in_a_guest()") < \
        inspect.getsource(J.serve).index("install_guard()")


def test_explicit_prompt_resolution_uses_guest_environment_and_a_data_path(world, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    J, calls = world.J, []
    monkeypatch.setenv("ANYUSE_DRIVE_ENABLED", "0")
    monkeypatch.setenv(SP.POLICY_ENV, "/guest/default-policy.json")

    def resolve(where, *, environment, task_context, decision_limits=None, decision_deadline_monotonic=None):
        assert task_context == ""
        calls.append((where, environment))
        assert environment["ANYUSE_DRIVE_ENABLED"] == "0"
        return {"format": SP.RESOLUTION_FORMAT, "status": "manual", "reason": "guest drive authorization required",
                "attempted": False, "reobserve_required": False, "next_action": "stop"}

    monkeypatch.setattr(SP, "resolve_configured", resolve)
    path = "/guest/private/policy $(never-executed); 'data'.json"
    result, stop = J.handle("resolve_system_prompt", {
        "where": "agent_harness.before_observation", "mode": "configured_denials", "policy_path": path})
    assert not stop and result["status"] == "done" and result["resolution"]["next_action"] == "stop"
    assert calls[0][0] == "desktop_e2e_job: agent_harness.before_observation"
    assert calls[0][1][SP.POLICY_ENV] == path and os.environ[SP.POLICY_ENV] == "/guest/default-policy.json"
    assert _sent(world) == []
    calls.clear()
    J.handle("resolve_system_prompt", {"where": "boundary", "mode": "configured_denials"})
    assert calls[0][1][SP.POLICY_ENV] == "/guest/default-policy.json"


def test_prompt_resolution_rejects_executable_or_drive_overrides_before_resolver(world, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    monkeypatch.setattr(SP, "resolve_configured", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("invalid message must not reach the resolver")))
    base = {"where": "boundary", "mode": "configured_denials"}
    for extra in ({"ANYUSE_DRIVE_ENABLED": "1"}, {"environment": {}}, {"callback": "anything"},
                  {"policy_path": "relative.json"}, {"policy_path": "/guest/invalid\0path"},
                  {"mode": "unsupported_mode"}, {"task_context": {"callback": "anything"}},
                  {"provider": "anything"}, {"decision_adapter": "anything"},
                  {"decision_deadline_monotonic": 12345.0},
                  {"decision_limits": {"callback": "anything"}},
                  {"task_context": "é" * (SP.MAX_TASK_CONTEXT_BYTES // 2 + 1)}):
        result, stop = world.J.handle("resolve_system_prompt", dict(base, **extra))
        assert result["status"] == "error" and not stop
    assert _sent(world) == []


def test_guest_llm_receives_task_data_and_preserves_guest_model_configuration(world, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    seen = []
    monkeypatch.setenv("ANYUSE_AGENT_MODEL_NAME", "guest-configured-model")

    def resolve(where, *, environment, task_context, decision_limits=None, decision_deadline_monotonic=None):
        seen.append(task_context)
        assert environment[SP.MODE_ENV] == "llm"
        assert environment["ANYUSE_AGENT_MODEL_NAME"] == "guest-configured-model"
        return {"format": SP.RESOLUTION_FORMAT, "status": "action_attempted", "reason": "supported choice",
                "attempted": True, "reobserve_required": True, "next_action": "observe"}

    monkeypatch.setattr(SP, "resolve_configured", resolve)
    task = "Open Reader using ${secret:AUTH}"
    result, stop = world.J.handle("resolve_system_prompt", {"where": "boundary", "mode": "llm", "task_context": task})
    assert not stop and result["resolution"]["attempted"] and seen == [task] and _sent(world) == []


def test_manual_guest_prompt_operation_does_not_read_policy_or_probe_native_state(world, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    monkeypatch.setattr(SP, "_NativeBackend", lambda: (_ for _ in ()).throw(AssertionError("no native probe")))
    monkeypatch.setattr(SP, "_policy", lambda path: (_ for _ in ()).throw(AssertionError("no policy read")))
    result, stop = world.J.handle("resolve_system_prompt", {"where": "boundary", "mode": "manual"})
    assert result["status"] == "done" and result["resolution"]["status"] == "disabled" and not stop
    assert _sent(world) == []


def test_duplicate_prompt_job_id_never_dispatches_again_or_overwrites_first_attempt_receipt(world, monkeypatch, tmp_path):
    from anyuse.execution import system_prompt_policy as SP
    calls = []

    def resolve(where, *, environment, task_context, decision_limits=None, decision_deadline_monotonic=None):
        calls.append(where)
        return {"format": SP.RESOLUTION_FORMAT, "status": "denial_attempted", "reason": "first window denied",
                "attempted": True, "reobserve_required": True, "next_action": "observe"}

    monkeypatch.setattr(SP, "resolve_configured", resolve)
    done, responses, commands = (tmp_path / name for name in ("done", "resp", "cmd"))
    for directory in (done, responses, commands):
        directory.mkdir()
    cid = "p" + "a" * 32
    command = commands / (cid + ".json")
    message = {"op": "resolve_system_prompt", "args": {"where": "boundary", "mode": "configured_denials"}}
    command.write_text(json.dumps(message))
    assert world.J._serve_prompt_job(str(command), cid, message, str(done), str(responses)) is False
    first = (responses / (cid + ".json")).read_bytes()
    assert json.loads(first)["resolution"]["attempted"] is True
    command.write_text(json.dumps(message))
    assert world.J._serve_prompt_job(str(command), cid, message, str(done), str(responses)) is False
    assert len(calls) == 1 and not command.exists()
    assert (responses / (cid + ".json")).read_bytes() == first
    changed_operation = {"op": "front", "args": {}}
    command.write_text(json.dumps(changed_operation))
    assert world.J._serve_prompt_job(str(command), cid, changed_operation, str(done), str(responses)) is False
    assert (responses / (cid + ".json")).read_bytes() == first and len(calls) == 1


def test_claim_without_prompt_receipt_stays_uncertain_and_cannot_dispatch_again(world, monkeypatch, tmp_path):
    from anyuse.execution import system_prompt_policy as SP
    monkeypatch.setattr(SP, "resolve_configured", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("an interrupted claimed command must never be dispatched again")))
    done, responses, commands = (tmp_path / name for name in ("done", "resp", "cmd"))
    for directory in (done, responses, commands):
        directory.mkdir()
    cid = "p" + "b" * 32
    message = {"op": "resolve_system_prompt", "args": {"where": "boundary", "mode": "configured_denials"}}
    claimed, command = done / (cid + ".json"), commands / (cid + ".json")
    claimed.write_text(json.dumps(message))
    command.write_text(json.dumps(message))
    assert world.J._serve_prompt_job(str(command), cid, message, str(done), str(responses)) is False
    assert claimed.exists() and not command.exists() and not (responses / (cid + ".json")).exists()


@pytest.mark.parametrize("expires,server_id", [(99.0, "a" * 32), (105.0, "b" * 32)])
def test_expired_or_mismatched_prompt_job_claims_id_without_calling_model_or_pressing(world, monkeypatch, tmp_path,
                                                                                  expires, server_id):
    from anyuse.execution import system_prompt_policy as SP
    J = world.J
    monkeypatch.setattr(J, "_PROMPT_CLOCK_ID", "a" * 32)
    monkeypatch.setattr(J.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(SP, "resolve_configured", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("expired or mismatched transport may not reach the model or native resolver")))
    done, responses = tmp_path / "done", tmp_path / "resp"
    done.mkdir()
    responses.mkdir()
    cid = "p" + "c" * 32
    message = {"op": "resolve_system_prompt", "args": {"where": "boundary", "mode": "llm", "task_context": "Inspect Reader",
        "decision_limits": {"remaining_usd": 0.1, "remaining_calls": 1, "timeout_s": 10.0}},
        "decision_deadline": {"server_id": server_id, "monotonic_s": expires}}
    command = tmp_path / (cid + ".json")
    command.write_text(json.dumps(message))
    assert J._serve_prompt_job(str(command), cid, message, str(done), str(responses)) is False
    resolution = json.loads((responses / (cid + ".json")).read_text())["resolution"]
    assert (done / (cid + ".json")).exists() and resolution["next_action"] == "stop"
    assert resolution["attempted"] is False and resolution["decision"]["model_calls"] == 0 and _sent(world) == []


def test_prompt_job_forwards_absolute_guest_deadline_without_resetting_queue_time(world, monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    J, seen = world.J, []
    monkeypatch.setattr(J, "_PROMPT_CLOCK_ID", "a" * 32)
    monkeypatch.setattr(J.time, "monotonic", lambda: 100.0)

    def resolve(where, *, environment, task_context, decision_limits, decision_deadline_monotonic):
        seen.append((decision_limits, decision_deadline_monotonic))
        return {"format": SP.RESOLUTION_FORMAT, "status": "manual", "reason": "test stop",
                "attempted": False, "reobserve_required": False, "next_action": "stop"}

    monkeypatch.setattr(SP, "resolve_configured", resolve)
    response, stop = J._dispatch_prompt_job({"op": "resolve_system_prompt", "args": {
        "where": "boundary", "mode": "llm", "task_context": "Inspect Reader",
        "decision_limits": {"remaining_usd": 0.1, "remaining_calls": 1, "timeout_s": 10.0}},
        "decision_deadline": {"server_id": "a" * 32, "monotonic_s": 105.0}})
    assert not stop and response["resolution"]["next_action"] == "stop"
    assert seen == [({"remaining_usd": 0.1, "remaining_calls": 1, "timeout_s": 5.0}, 105.0)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
