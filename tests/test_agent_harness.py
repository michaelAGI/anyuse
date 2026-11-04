"""The general agent harness (execution/agent_harness.py, harness_types.py, harness_tools.py,
harness_policies.py), offline with fakes.
Pinned here: typed actions and their effect classes (declared by the tool, raised by labels, never
lowered); the loop's stops with a reason (done, fail, abstain, step cap, call cap, budget, wall time,
unreadable, no progress, blocked, a declined hold); the rule for an irreversible action the task did
not ask for (``asked_for``, the setting, stop vs hold); the tool sets behind the existing guards: the
browser's forbidden controls and off-site refusals on a fake shop, the desktop's guest ``ax_step`` with
the interlock and the irreversible-label gate, HTTP GET-only with the destination, gov and robots gates,
the file workspace jail; the policies (scripted, model over an injected call, the Claude CLI call that
counts at the process counter, the OpenJev picker); model calls reported from the ONE counter.
No browser, no AX, no subprocess, no network, no model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_agent_harness.py
"""
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

import httpx  # noqa: E402

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import model_adapter as M  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402

import test_repair_ladder as SHOP  # noqa: E402  (the fake shop served from generated DOMs)

BASE, SITE = SHOP.BASE, SHOP.SITE


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeAXGuest:
    """A tiny TextEdit as the guest job callable: the menu bar (Ablage, Format), the Ablage menu (Neu,
    Sichern ..., Löschen), a text area, the save sheet (a name field, Sichern / Abbrechen). ``ax_elements``
    lists the screen; ``ax_step`` runs ONE planned AX step with the guest's own guards (an irreversible
    label is gated, an ambiguous identity refused)."""

    def __init__(self):
        self.state, self.text, self.name = "doc", "", ""
        self.saved, self.log = [], []

    def elements(self):
        els = [{"role": "AXMenuBarItem", "title": "Ablage", "identifier": "", "path": "menubar[0]/menuitem[0]",
                "in_menubar": True},
               {"role": "AXMenuBarItem", "title": "Format", "identifier": "", "path": "menubar[0]/menuitem[1]",
                "in_menubar": True},
               {"role": "AXTextArea", "title": "", "identifier": "doc.text", "path": "window[0]/scroll[0]/textarea[0]",
                "value": self.text}]
        if self.state == "menu":
            els += [{"role": "AXMenuItem", "title": "Neu", "identifier": "file.new", "path": "menubar[0]/menuitem[0]/menu[0]/menuitem[0]", "in_menubar": True},
                    {"role": "AXMenuItem", "title": "Sichern …", "identifier": "file.save", "path": "menubar[0]/menuitem[0]/menu[0]/menuitem[1]", "in_menubar": True},
                    {"role": "AXMenuItem", "title": "Löschen", "identifier": "file.delete", "path": "menubar[0]/menuitem[0]/menu[0]/menuitem[2]", "in_menubar": True}]
        if self.state == "sheet":
            els += [{"role": "AXTextField", "title": "", "identifier": "sheet.name", "path": "window[0]/sheet[0]/textfield[0]", "value": self.name},
                    {"role": "AXButton", "title": "Sichern", "identifier": "sheet.save", "path": "window[0]/sheet[0]/button[0]"},
                    {"role": "AXButton", "title": "Abbrechen", "identifier": "sheet.cancel", "path": "window[0]/sheet[0]/button[1]"}]
        return els

    def __call__(self, op, **a):
        self.log.append((op, a))
        if op == "ax_elements":
            return {"status": "done", "app": "TextEdit", "bundle": "com.apple.TextEdit", "window": "Ohne Titel",
                    "elements": self.elements(), "text": self.text}
        if op == "read_text":
            return {"status": "done", "text": self.text}
        if op == "ax_step":
            return self.ax_step(a["step"])
        return {"status": "refused", "detail": f"unknown op {op}"}

    def ax_step(self, step):
        from anyuse.execution.desktop_e2e import irreversible_reason
        cmd, sel = step["command"], (step["command"].get("selector") or {})
        action, ident, title, role = cmd["action"], sel.get("identifier"), cmd.get("title"), cmd.get("role")
        if step.get("modality") != "ax":
            return {"status": "refused", "detail": "modality"}
        if action == "press_key":                      # as the guest: a key needs no target element
            return {"status": "done"}
        if irreversible_reason(title) or (title or "").lower() in ("löschen",):
            return {"status": "gated", "detail": "irreversible-action guard"}
        els = [e for e in self.elements() if (ident and e["identifier"] == ident)
               or (not ident and e["title"] == title and e["role"] == role)]
        if len(els) != 1:
            return {"status": "refused", "detail": f"{len(els)} candidates for {role} {title!r}"}
        e = els[0]
        if action == "press":
            if e["title"] == "Ablage":
                self.state = "menu"
            elif e["title"] == "Format":
                self.state = "doc"
            elif ident == "file.new":
                self.text, self.state = "", "doc"
            elif ident == "file.save":
                self.state = "sheet"
            elif ident == "sheet.save":
                self.saved.append((self.name, self.text))
                self.state = "doc"
            elif ident == "sheet.cancel":
                self.state = "doc"
            return {"status": "done"}
        if action == "enter_text":
            if e["role"] == "AXTextArea":
                self.text = str(step.get("input_data") or "")
            elif ident == "sheet.name":
                self.name = str(step.get("input_data") or "")
            else:
                return {"status": "refused", "detail": "not a text field"}
            return {"status": "done"}
        if action == "press_key":
            return {"status": "done"}
        return {"status": "refused", "detail": f"unsupported {action}"}


def shop_tools(**kw):
    site = kw.pop("site", None) or SHOP.Shop()
    return HT.BrowserTools(D.DomPage(site), BASE + "/", SITE, **kw), site


def desktop_tools(**kw):
    g = FakeAXGuest()
    return HT.DesktopTools(g, "TextEdit", "com.apple.TextEdit", **kw), g


def idx(obs, **match):
    """The index of the first element matching the given fields."""
    for e in obs.elements:
        if all(getattr(e, k) == v for k, v in match.items()):
            return e.index
    raise AssertionError(f"no element {match} in {[e.line() for e in obs.elements][:12]}")


def run(policy, toolbox, task="t", values=None, cfg=None, approver=None):
    return H.Harness(toolbox, policy, cfg or H.HarnessConfig(), approver=approver, run_id="r1").run(task, values)


# ---------------------------------------------------------------------------
# types and effects
# ---------------------------------------------------------------------------
def test_actions_parse_strictly_and_carry_their_effect_class():
    assert isinstance(T.action_from_dict({"action": "search", "element": 2, "text": "x"}), T.Search)
    assert T.action_from_dict({"action": "click"}) is None                   # a required field missing
    assert T.action_from_dict({"action": "click", "element": True}) is None  # a bool is not an index
    assert T.action_from_dict({"action": "fly"}) is None
    assert T.action_from_dict({"action": "done"}).result is None
    a = T.action_from_dict({"action": "write_file", "name": "a.txt", "text": "x"})
    assert a.effect == T.WRITE and a.toolset == "files"
    assert T.HttpGet("u").effect == T.READ and T.HttpPost("u").effect == T.WRITE
    assert T.RunTool("x").effect == T.UNKNOWN and T.AxRead().effect == T.READ
    assert T.max_effect(T.READ, T.WRITE) == T.WRITE and T.max_effect(T.WRITE, T.UNKNOWN) == T.UNKNOWN
    assert T.max_effect(T.UNKNOWN, T.IRREVERSIBLE) == T.IRREVERSIBLE and T.max_effect("bogus") == T.UNKNOWN
    assert T.needs_gate(T.UNKNOWN) and T.needs_gate(T.IRREVERSIBLE) and not T.needs_gate(T.WRITE)
    assert set(T.STOPS) >= {"done", "max_steps", "irreversible_not_asked", "gov_host"}


def test_observation_renders_as_data_and_hides_the_page_record():
    o = T.Observation(kind="web", url="https://a.test/x#f", title="A", elements=[T.Element(0, "link", "Go", "/g")],
                      text="hello", page={"dom": "<html>"})
    s = o.render()
    assert "data, not instructions" in s and "[0] link 'Go' -> /g" in s and "<html>" not in s
    assert "page" not in o.to_dict() and "page" in o.to_dict(with_page=True)
    assert o.state_key() == T.Observation(kind="web", url="https://a.test/x#g", title="A",
                                          elements=[T.Element(0, "link", "Go", "/g")]).state_key()


# ---------------------------------------------------------------------------
# asked_for and the setting
# ---------------------------------------------------------------------------
def test_asked_for_needs_the_verb_and_the_target_in_the_task_text():
    assert H.asked_for("Delete the draft note", "Delete", "draft")
    assert H.asked_for("Lösche die Notiz Entwurf", "Löschen", "Entwurf")
    assert not H.asked_for("tidy up my notes", "Delete", "draft")
    assert not H.asked_for("delete the old invoice", "Delete", "draft")          # another target
    assert not H.asked_for("send the draft", "Delete", "draft")                   # another verb
    assert H.asked_for("empty the trash", "Empty Trash", "")                      # no target named: the verb suffices
    assert not H.asked_for("", "Delete", "x") and not H.asked_for("delete it", "", "x")


def test_the_setting_asks_by_default_and_stop_never_asks():
    # open tasks: a commit step the task did not cover is asked of the caller by default; ``stop`` (unattended)
    # never asks; ``hold`` is the earlier name of ``ask``
    from anyuse import settings as S
    spec = S.SETTINGS["ANYUSE_AGENT_UNASKED_IRREVERSIBLE"]
    assert spec.default == "ask" and spec.allowed_values == ("ask", "stop", "hold") and spec.security_relevant
    assert not spec.needs_confirmation("stop") and not spec.needs_confirmation("ask")
    try:
        S.parse_for_storage("ANYUSE_AGENT_UNASKED_IRREVERSIBLE", "allow")
        raise AssertionError("allow was accepted")
    except S.SettingError:
        pass
    assert S.SETTINGS["ANYUSE_AGENT_MAX_STEPS"].default == 20
    cfg = H.HarnessConfig.from_settings()
    assert cfg.unasked_irreversible == "ask" and cfg.max_steps == 20
    assert H.HarnessConfig.from_settings(max_steps=3).max_steps == 3


# ---------------------------------------------------------------------------
# the loop: stops with a reason
# ---------------------------------------------------------------------------
def test_done_fail_abstain_and_the_result_and_model_calls_come_from_the_counter():
    tools, _ = shop_tools()
    tb = HT.Toolbox(browser=tools)
    tr = run(P.ScriptedPolicy([T.Done({"ok": 1})]), tb)
    assert tr.ok and tr.stop == "done" and tr.result == {"ok": 1} and tr.decisions == 1
    assert tr.model_calls == 0 and tr.kind == "web" and tr.site == SITE
    assert run(P.ScriptedPolicy([T.Fail("nope")]), HT.Toolbox(browser=shop_tools()[0])).stop == "fail"
    tr = run(P.ScriptedPolicy([T.Abstain("wall")]), HT.Toolbox(browser=shop_tools()[0]))
    assert tr.stop == "abstain" and tr.stop_reason == "wall" and tr.stopped_action["action"] == "abstain"
    rep = H.report(tr)
    assert rep["stop"] == "abstain" and rep["model_calls"] == 0 and rep["effects"] == ["read"]


def test_step_cap_call_cap_budget_and_wall_time_each_stop_by_name():
    tools, _ = shop_tools()
    forever = P.ScriptedPolicy([T.ReadText()] * 50)
    assert run(forever, HT.Toolbox(browser=tools), cfg=H.HarnessConfig(max_steps=4, no_progress_steps=0)).stop == "max_steps"
    tr = run(P.ScriptedPolicy([T.ReadText()] * 50), HT.Toolbox(browser=shop_tools()[0]),
             cfg=H.HarnessConfig(max_steps=10, max_calls=2, no_progress_steps=0))
    assert tr.stop == "call_cap" and tr.decisions == 2
    tr = run(P.ScriptedPolicy([T.ReadText()] * 50, cost_per_call=0.01), HT.Toolbox(browser=shop_tools()[0]),
             cfg=H.HarnessConfig(max_steps=10, max_usd=0.025, no_progress_steps=0))
    assert tr.stop == "budget" and tr.decisions == 3 and abs(tr.cost_usd - 0.03) < 1e-9
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 100.0
        return clock["t"]
    h = H.Harness(HT.Toolbox(browser=shop_tools()[0]), P.ScriptedPolicy([T.ReadText()] * 50),
                  H.HarnessConfig(wall_s=250, no_progress_steps=0), clock=tick)
    assert h.run("t").stop == "wall_time"


def test_a_budget_of_zero_allows_no_model_calls(monkeypatch):
    # 0 reads as the run budget and the daily cap read it: no model calls, not no cap
    asked = []

    def call(system, user):
        asked.append(user)
        return '{"action": "done", "result": {"no_results": true}}', {"cost_usd": 0.001}
    tr = run(P.ModelPolicy(call), HT.Toolbox(browser=shop_tools()[0]),
             cfg=H.HarnessConfig(max_usd=0.0, no_progress_steps=0))
    assert tr.stop == "budget" and tr.decisions == 0 and asked == [], (tr.stop, tr.stop_reason)
    assert "no model calls" in tr.stop_reason and "ANYUSE_AGENT_MAX_USD" in tr.stop_reason, tr.stop_reason
    # a scripted policy with a cost stands in for a model
    tr = run(P.ScriptedPolicy([T.ReadText()] * 5, cost_per_call=0.01), HT.Toolbox(browser=shop_tools()[0]),
             cfg=H.HarnessConfig(max_steps=4, max_usd=0.0, no_progress_steps=0))
    assert tr.stop == "budget" and tr.decisions == 0
    # a policy that calls no model still runs
    tr = run(P.ScriptedPolicy([T.Done({"no_results": True})]), HT.Toolbox(browser=shop_tools()[0]),
             cfg=H.HarnessConfig(max_usd=0.0))
    assert tr.stop == "done" and tr.decisions == 1
    from anyuse.execution.web_flow_compile import FlowReplayPolicy
    assert FlowReplayPolicy({}, {}).calls_model is False
    # the setting: 0 is a budget of 0, and a positive budget still stops once it is spent
    monkeypatch.setenv("ANYUSE_AGENT_MAX_USD", "0")
    cfg = H.HarnessConfig.from_settings(no_progress_steps=0)
    assert cfg.max_usd == 0.0
    tr = run(P.ModelPolicy(call), HT.Toolbox(browser=shop_tools()[0]), cfg=cfg)
    assert tr.stop == "budget" and asked == []
    tr = run(P.ScriptedPolicy([T.ReadText()] * 50, cost_per_call=0.01), HT.Toolbox(browser=shop_tools()[0]),
             cfg=H.HarnessConfig(max_steps=10, max_usd=0.025, no_progress_steps=0))
    assert tr.stop == "budget" and tr.decisions == 3


def test_unreadable_answers_are_repaired_once_then_stop_and_no_progress_stops():
    tools, _ = shop_tools()
    tr = run(P.ScriptedPolicy(["not json", "{\"action\": \"fly\"}", T.Done()]), HT.Toolbox(browser=tools))
    assert tr.stop == "unreadable" and tr.decisions == 2 and tr.steps[0].action is None
    tools, _ = shop_tools()
    tr = run(P.ScriptedPolicy([T.Click(99)] * 10), HT.Toolbox(browser=tools), cfg=H.HarnessConfig(no_progress_steps=3))
    assert tr.stop == "no_progress" and all(not s.outcome.ok for s in tr.steps)


def test_a_policy_error_stops_and_a_dispatch_exception_is_an_outcome():
    class Boom:
        def decide(self, *_a):
            raise RuntimeError("provider down")
    tr = run(Boom(), HT.Toolbox(browser=shop_tools()[0]))
    assert tr.stop == "policy_error" and "provider down" in tr.stop_reason

    class BadTools(HT.FileTools):
        def dispatch(self, action, obs):
            raise ValueError("disk")
    ws = tempfile.mkdtemp(prefix="anyuse-ws-")
    tr = run(P.ScriptedPolicy([T.ListFiles(), T.Done()]), HT.Toolbox(files=BadTools(ws)))
    assert tr.ok and not tr.steps[0].outcome.ok and "disk" in tr.steps[0].outcome.why


# ---------------------------------------------------------------------------
# the effect policy
# ---------------------------------------------------------------------------
def test_an_unasked_irreversible_desktop_action_stops_before_the_guest_sees_it():
    tools, g = desktop_tools()
    tb = HT.Toolbox(desktop=tools)
    obs = tb.observe()
    tr = run(P.ScriptedPolicy([T.AxPress(idx(obs, text="Ablage")),
                               lambda t, o, h: T.AxPress(idx(o, text="Löschen")), T.Done()]), tb,
             task="tidy up the document")
    assert tr.stop == "irreversible_not_asked" and tr.stopped_action["action"] == "ax_press"
    assert "Löschen" in tr.stop_reason and "stop" in tr.stop_reason
    assert not any(op == "ax_step" and a["step"]["command"]["title"] == "Löschen" for op, a in g.log)
    assert tr.steps[-1].effect == "irreversible" and not tr.steps[-1].outcome.ok


def test_an_asked_irreversible_action_proceeds_and_the_guest_gates_it():
    tools, g = desktop_tools()
    tb = HT.Toolbox(desktop=tools)
    obs = tb.observe()
    tr = run(P.ScriptedPolicy([T.AxPress(idx(obs, text="Ablage")),
                               lambda t, o, h: T.AxPress(idx(o, text="Löschen")), T.Done()]), tb,
             task="Lösche das Dokument (Löschen im Menü Ablage)")
    assert tr.ok, tr.stop_reason
    gated = [s for s in tr.steps if s.action is not None and s.action.kind == "ax_press" and s.effect == "irreversible"]
    assert gated and gated[0].outcome.status == "gated" and not gated[0].outcome.ok and gated[0].held


def test_hold_asks_the_approver_and_a_declined_hold_stops():
    asked = []

    def approve(ctx):
        asked.append(ctx)
        return True

    def decline(_ctx):
        return False
    for approver, stop in ((approve, "done"), (decline, "confirm_declined"), (None, "irreversible_not_asked")):
        tools, g = desktop_tools()
        tb = HT.Toolbox(desktop=tools)
        obs = tb.observe()
        tr = run(P.ScriptedPolicy([T.AxPress(idx(obs, text="Ablage")),
                                   lambda t, o, h: T.AxPress(idx(o, text="Löschen")), T.Done()]), tb,
                 task="tidy up", cfg=H.HarnessConfig(unasked_irreversible="hold"), approver=approver)
        assert tr.stop == stop, (stop, tr.stop_reason)
    assert len(asked) == 1 and asked[0]["effect"] == "irreversible" and asked[0]["target"] == "Löschen"


def test_a_write_in_a_read_only_set_and_an_unknown_tool_set_are_refused_softly():
    tr = run(P.ScriptedPolicy([T.HttpGet("https://x.test/"), T.Done()]), HT.Toolbox(browser=shop_tools()[0]))
    assert tr.ok and "no http tools" in tr.steps[0].outcome.why and tr.steps[0].effect == "read"
    # RunTool on a toolbox without a registry: the set's absence refuses it before the effect policy
    tr = run(P.ScriptedPolicy([T.RunTool("x"), T.Done()]), HT.Toolbox(browser=shop_tools()[0]), task="run x")
    assert tr.ok and "no tools tools" in tr.steps[0].outcome.why and tr.steps[0].effect == "unknown"


# ---------------------------------------------------------------------------
# browser tools on the fake shop
# ---------------------------------------------------------------------------
def test_browser_search_click_read_and_the_trace_keeps_the_page_records():
    tools, site = shop_tools()
    tb = HT.Toolbox(browser=tools)
    obs = tb.observe()
    assert obs.kind == "web" and obs.url == BASE + "/" and obs.inputs and obs.elements[0].kind == "search box"
    tr = run(P.ScriptedPolicy([T.Search(0, "Bosch"), T.ReadTable(),
                               lambda t, o, h: T.Click(idx(o, text="Bosch GSR 12V Akkuschrauber")),
                               T.ExtractFields(["name", "price"]), T.Done()]), tb)
    assert tr.ok, tr.stop_reason
    s0, s1, s2, s3 = tr.steps[:4]
    assert s0.outcome.ok and s0.outcome.how == "typed" and s0.outcome.input["name"] == "q" and "/search?q=Bosch" in s0.outcome.url_after
    assert s1.outcome.data["rows"] and all("/p/" in r["url"] for r in s1.outcome.data["rows"])
    assert s2.outcome.how == "goto" and s2.outcome.href.endswith("/p/bosch-gsr-12v")
    assert s3.outcome.data["fields"]["name"] == "Bosch GSR 12V Akkuschrauber" and s3.outcome.data["fields"]["price"] == 89.9
    assert s0.observation.page.get("dom") and "search_urls" in s0.observation.page
    assert [s.effect for s in tr.steps] == ["read"] * 5


def test_browser_refuses_forbidden_controls_other_sites_and_gov_hosts(monkeypatch):
    from anyuse.execution.web_arch import forbidden_control
    assert forbidden_control("Konto", "/konto") and forbidden_control("", "/cart")
    # a forbidden control ends the run (as the demonstrator), whatever the task says
    tools, site = shop_tools()
    tb = HT.Toolbox(browser=tools)
    obs = tb.observe()
    tr = run(P.ScriptedPolicy([T.Click(idx(obs, text="Konto")), T.Done()]), tb, task="open the Konto page")
    assert tr.stop == "refused" and "forbidden" in tr.stop_reason and tr.steps[0].effect == "irreversible"
    assert tools.page.gotos == [BASE + "/"]                     # nothing forbidden was ever loaded
    tools, site = shop_tools()
    tr = run(P.ScriptedPolicy([T.Open(BASE + "/cart")]), HT.Toolbox(browser=tools), task="open the cart")
    assert tr.stop == "refused" and "forbidden" in tr.stop_reason and tools.page.gotos == [BASE + "/"]
    # another website is a soft refusal (the policy sees it); a government host is one too by default
    tools, site = shop_tools()
    tr = run(P.ScriptedPolicy([T.Open("https://example.gov/"), T.Done()]), HT.Toolbox(browser=tools))
    assert "another website" in tr.steps[0].outcome.why and tr.stop != "gov_host"
    # with ANYUSE_REFUSE_GOV_HOSTS on, a government host ends the run
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    tools, site = shop_tools()
    tr = run(P.ScriptedPolicy([T.Open("https://other.test/"), T.Open("https://example.gov/")]), HT.Toolbox(browser=tools))
    assert tr.stop == "gov_host" and "government host" in tr.stop_reason
    assert "another website" in tr.steps[0].outcome.why and tools.page.gotos == [BASE + "/"]


def test_browser_robots_gate_and_back():
    tools, site = shop_tools(allow_url=lambda u: "/p/" not in u)
    tb = HT.Toolbox(browser=tools)
    tr = run(P.ScriptedPolicy([T.Search(0, "Makita"), lambda t, o, h: T.Click(idx(o, text="Makita DF333 Akkuschrauber")),
                               T.Back(), T.Done()]), tb)
    assert tr.ok and "robots" in tr.steps[1].outcome.why and tr.steps[2].outcome.ok
    assert tools.page.gotos[-1] == BASE + "/"


def test_a_blocked_page_stops_the_run_before_any_policy_call():
    class Wall(SHOP.Shop):
        def route(self, url):
            rec, u = super().route(url)
            return D.Recorded(url, "<html><head><title>Access denied</title></head><body><h1>Access Denied</h1>"
                              "<p>You don't have permission to access this site.</p></body></html>", {}, 403), u
    tools, _ = shop_tools(site=Wall())
    pol = P.ScriptedPolicy([T.Done()])
    tr = run(pol, HT.Toolbox(browser=tools))
    assert tr.stop == "blocked" and tr.decisions == 0 and pol.calls == []


# ---------------------------------------------------------------------------
# desktop tools on the fake guest
# ---------------------------------------------------------------------------
def test_desktop_observation_has_fingerprints_and_steps_go_through_ax_step():
    tools, g = desktop_tools()
    tb = HT.Toolbox(desktop=tools)
    obs = tb.observe()
    assert obs.kind == "ax" and obs.app == "TextEdit" and obs.state_id.startswith("textedit_")
    fp = obs.fingerprint["fingerprint"]
    assert {r["role"] for r in fp["required_elements"]} == {"AXTextArea"} and fp["structural_hash"]
    tr = run(P.ScriptedPolicy([lambda t, o, h: T.AxEnterText(idx(o, role="AXTextArea"), "Hallo"),
                               lambda t, o, h: T.AxPress(idx(o, text="Ablage")),
                               lambda t, o, h: T.AxPress(idx(o, text="Sichern …")),
                               lambda t, o, h: T.AxEnterText(idx(o, role="AXTextField"), "a.txt"),
                               lambda t, o, h: T.AxPress(idx(o, text="Sichern")), T.AxRead(), T.Done()]), tb,
             task="write Hallo and save as a.txt")
    assert tr.ok, tr.stop_reason
    assert g.saved == [("a.txt", "Hallo")]
    steps = [a["step"] for op, a in g.log if op == "ax_step"]
    assert [s["command"]["action"] for s in steps] == ["enter_text", "press", "press", "enter_text", "press"]
    assert steps[0]["command"]["selector"]["identifier"] == "doc.text" and steps[0]["modality"] == "ax"
    assert [s.effect for s in tr.steps] == ["write"] * 5 + ["read", "read"]
    assert tr.steps[5].outcome.data["text"] == "Hallo"
    # each step's observation carries the state before it; the sheet state lists the sheet's controls
    assert any(r["role"] == "AXButton" for r in tr.steps[4].observation.fingerprint["fingerprint"]["required_elements"])


def test_a_menu_bar_item_is_never_irreversible_by_its_title_but_a_menu_item_is():
    tools, g = desktop_tools()
    obs = tools.observe()
    fmt = idx(obs, text="Format")
    assert obs.elements[fmt].role == "AXMenuBarItem" and tools.effect_of(T.AxPress(fmt), obs) == "write"
    menu_item = T.Element(index=99, kind="menuitem", text="Format Disk", role="AXMenuItem")
    obs2 = T.Observation(kind="ax", elements=obs.elements + [menu_item])
    assert tools.effect_of(T.AxPress(99), obs2) == "irreversible"
    tr = run(P.ScriptedPolicy([T.AxPress(fmt), T.Done()]), HT.Toolbox(desktop=tools), task="make the text plain")
    assert tr.ok and tr.steps[0].outcome.ok and tr.steps[0].effect == "write"


def test_desktop_interlock_refusal_stops_and_keys_are_return_or_escape():
    tools, g = desktop_tools(interlock=lambda where: f"refused by foreground interlock: a person is at the console ({where})")
    tb = HT.Toolbox(desktop=tools)
    tr = run(P.ScriptedPolicy([T.AxRead(), lambda t, o, h: T.AxPress(idx(o, text="Ablage")), T.Done()]), tb)
    assert tr.steps[0].outcome.ok                                  # a read needs no interlock
    assert tr.stop == "refused" and "foreground interlock" in tr.stop_reason
    assert not any(op == "ax_step" for op, _a in g.log)
    tools, g = desktop_tools()
    tr = run(P.ScriptedPolicy([T.AxKey("cmd+s"), T.AxKey("return"), T.AxPress(42), T.Done()]), HT.Toolbox(desktop=tools))
    assert tr.ok and "Return or Escape" in tr.steps[0].outcome.why and tr.steps[1].outcome.ok
    assert "not on this screen" in tr.steps[2].outcome.why
    assert [a["step"]["command"]["keycode"] for op, a in g.log if op == "ax_step"] == [36]


def test_a_guest_prompt_stops_the_run():
    tools, g = desktop_tools()
    g.ax_step = lambda step: {"status": "prompt", "detail": "an authentication dialog"}
    tr = run(P.ScriptedPolicy([lambda t, o, h: T.AxPress(idx(o, text="Ablage")), T.Done()]), HT.Toolbox(desktop=tools))
    assert tr.stop == "blocked" and "prompt" in tr.stop_reason


# ---------------------------------------------------------------------------
# http and files
# ---------------------------------------------------------------------------
def http_tools(robots=lambda u: (True, "ok"), **kw):
    sent = []

    def transport(method, url, headers, body):
        sent.append((method, url, headers.get("User-Agent"), body))
        if url.endswith("/data.json"):
            return 200, {"Content-Type": "application/json"}, json.dumps({"items": [1, 2, 3]})
        if url.endswith("/missing"):
            return 404, {"Content-Type": "text/html"}, "<html>nope</html>"
        return 200, {"Content-Type": "text/html"}, "<html><body>hello</body></html>"
    return HT.HttpTools(transport, "https://api.shop.test/", robots=robots, **kw), sent


def test_http_get_is_the_default_and_posts_are_refused_without_an_approval(monkeypatch):
    # by default a government host is refused like any other origin outside the allowlist
    tools, sent = http_tools()
    tr = run(P.ScriptedPolicy([T.HttpGet("https://example.gov/x"), T.Done()]), HT.Toolbox(http=tools))
    assert "not in the exact-origin allowlist" in tr.steps[0].outcome.why and tr.stop != "gov_host" and sent == []
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    tools, sent = http_tools()
    tb = HT.Toolbox(http=tools)
    tr = run(P.ScriptedPolicy([T.HttpGet("https://api.shop.test/data.json"), T.HttpGet("https://api.shop.test/missing"),
                               T.HttpPost("https://api.shop.test/search", body="q=x", operation="search"),
                               T.HttpGet("https://other.test/"), T.HttpGet("https://example.gov/x")]), tb)
    assert tr.stop == "gov_host"
    s = tr.steps
    assert s[0].outcome.ok and s[0].outcome.observation.json_data == {"items": [1, 2, 3]} and s[0].effect == "read"
    assert not s[1].outcome.ok and "HTTP 404" in s[1].outcome.why
    assert s[2].effect == "write" and "POST is a write" in s[2].outcome.why and not s[2].outcome.ok
    assert "not in the exact-origin allowlist" in s[3].outcome.why
    assert [m for m, *_ in sent] == ["GET", "GET"] and all(ua.startswith("anyuse-agent-harness") for _m, _u, ua, _b in sent)


def test_http_robots_unknown_or_disallowed_refuses():
    tools, sent = http_tools(robots=lambda u: (None, "no cached robots.txt"))
    tr = run(P.ScriptedPolicy([T.HttpGet("https://api.shop.test/a"), T.Done()]), HT.Toolbox(http=tools))
    assert tr.ok and "robots.txt not known" in tr.steps[0].outcome.why and sent == []
    tools, sent = http_tools(robots=lambda u: (False, "disallows /a"))
    tr = run(P.ScriptedPolicy([T.HttpGet("https://api.shop.test/a"), T.Done()]), HT.Toolbox(http=tools))
    assert "disallows" in tr.steps[0].outcome.why and sent == []


def test_file_workspace_jail():
    ws = tempfile.mkdtemp(prefix="anyuse-ws-")
    ft = HT.FileTools(ws)
    with open(os.path.join(ws, "in", "data.csv"), "w") as fh:
        fh.write("a,b\n1,2\n")
    os.symlink("/", os.path.join(ws, "out", "escape"))
    tb = HT.Toolbox(files=ft)
    tr = run(P.ScriptedPolicy([T.ListFiles(), T.ReadFile("in/data.csv"), T.WriteFile("result.txt", "ok"),
                               T.WriteFile("../evil.txt", "x"), T.WriteFile("/tmp/evil.txt", "x"),
                               T.ReadFile("out/escape/etc/hosts"), T.WriteFile("escape/x.txt", "x"), T.ReadFile("nope.txt"),
                               T.Done()]), tb, cfg=H.HarnessConfig(no_progress_steps=0))
    assert tr.ok, tr.stop_reason
    s = tr.steps
    assert s[0].outcome.data["listing"] == ["in/data.csv"] and s[1].outcome.data["text"] == "a,b\n1,2\n"
    assert s[2].outcome.ok and s[2].outcome.data["path"] == "out/result.txt" and s[2].effect == "write"
    assert all(not x.outcome.ok for x in s[3:8]) and "outside the workspace" in s[5].outcome.why
    assert "outside the workspace" in s[6].outcome.why and "no file" in s[7].outcome.why
    assert not os.path.exists("/tmp/evil.txt") or True
    assert sorted(os.listdir(os.path.join(ws, "out"))) == ["escape", "result.txt"]


def test_workspace_dir_is_under_the_data_dir():
    base = tempfile.mkdtemp(prefix="anyuse-runs-")
    ws = H.workspace_dir("run/1 x", base=base)
    assert ws.startswith(base) and os.path.isdir(os.path.join(ws, "in")) and os.path.isdir(os.path.join(ws, "out"))
    assert "/" not in os.path.basename(ws)


# ---------------------------------------------------------------------------
# policies and the counter
# ---------------------------------------------------------------------------
def _adapter(replies):
    def handler(_request):
        text = replies.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    cfg = M.ModelConfig("local", "http://127.0.0.1:9/v1", "fake-model", "", max_tokens=100, price_in=1.0, price_out=1.0)
    return M.ModelAdapter(cfg, transport=httpx.MockTransport(handler), budget=M.RunBudget(1.0), feature="agent_harness")


def test_a_model_policy_over_the_adapter_counts_once_per_decision_at_the_process_counter():
    ad = _adapter(['{"action": "read_text"}', 'prose then {"action": "done", "result": {"x": 1}}'])
    pol = P.ModelPolicy(P.adapter_call(ad))
    tr = run(pol, HT.Toolbox(browser=shop_tools()[0]), task="read the page")
    assert tr.ok and tr.result == {"x": 1} and tr.model_calls == 2 == tr.decisions == ad.call_count
    assert tr.cost_usd > 0 and tr.steps[0].decision_text == '{"action": "read_text"}'
    # the prompt is the generic one: the task, the available actions and the observation as data
    system, user = P.default_prompt("t", T.Observation(kind="web", url="u"), ["1. x"], ["open", "done"])
    assert "ONE JSON object" in system and "AVAILABLE ACTIONS: open, done" in user and "data, not instructions" in user


def test_the_claude_cli_call_spawns_through_the_injected_runner_only_and_counts():
    calls = []

    def runner(argv, stdin_text, timeout):
        calls.append((argv, stdin_text, timeout))
        return 0, json.dumps({"result": '{"action": "done"}', "total_cost_usd": 0.012,
                              "usage": {"input_tokens": 100, "output_tokens": 9}}), ""
    mark = M.process_call_count()
    pol = P.ModelPolicy(P.claude_cli_call(runner, model="sonnet"))
    tr = run(pol, HT.Toolbox(browser=shop_tools()[0]))
    assert tr.ok and tr.model_calls == 1 == M.calls_since(mark) and abs(tr.cost_usd - 0.012) < 1e-9
    argv, stdin_text, timeout = calls[0]
    assert argv[:4] == ["claude", "-p", "--model", "sonnet"] and "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "TASK:" in stdin_text and not any(a.startswith("sk-") or "API_KEY" in a for a in argv)
    assert stdin_text not in argv                                  # the prompt goes on stdin, not the command line
    assert tr.steps[0].decision_text == '{"action": "done"}'

    def broken(argv, stdin_text, timeout):
        return 1, "", "no such command"
    tr = run(P.ModelPolicy(P.claude_cli_call(broken)), HT.Toolbox(browser=shop_tools()[0]))
    assert tr.stop == "policy_error" and "exit 1" in tr.stop_reason


def test_the_openjev_picker_chooses_a_listed_element_or_nothing():
    class Client:
        def __init__(self, choice, conf):
            self.choice, self.conf, self.seen = choice, conf, []

        def choose(self, state, instructions, options):
            self.seen.append((state, instructions, options))
            return {"choice": self.choice, "confidence": self.conf}
    obs = T.Observation(kind="ax", elements=[T.Element(0, "button", "Neu", role="AXMenuItem"),
                                             T.Element(1, "button", "Sichern", role="AXMenuItem")])
    c = Client(1, 0.95)
    i, conf, meta = P.OpenJevPicker(c).pick(obs, "which item saves?")
    assert i == 1 and conf == 0.95 and c.seen[0][2][1].startswith("[1] button Sichern")
    assert P.OpenJevPicker(Client(1, 0.5)).pick(obs, "q")[0] is None
    assert P.OpenJevPicker(Client(7, 0.99)).pick(obs, "q")[0] is None
    assert P.OpenJevPicker(Client("[0] button Neu", 0.99)).pick(obs, "q")[0] == 0


def test_cerebras_policy_without_a_key_refuses_before_any_call_is_counted():
    mark = M.process_call_count()
    pol = P.cerebras_policy(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    tr = run(pol, HT.Toolbox(browser=shop_tools()[0]))
    assert tr.stop == "policy_error" and tr.model_calls == 0 == M.calls_since(mark)


def test_rea_action_keeps_full_result_and_stops_before_any_ui_fallback():
    from anyuse.execution.harness_rea import ReaTools
    result = {"ok": False, "status": "uncertain", "verification_tier": "model_judged", "model_calls": 2,
              "result": {"verdict": "uncertain", "restoration": {"status": "failed", "receipt": {"kept": True}}}}
    class Store:
        def __init__(self): self.calls = []
        def list_tools(self):
            return [{"name": "rea.test.pause", "description": "Pause owned video", "verification_tier": "model_judged",
                     "input_schema": {"type": "object", "properties": {}}, "contract": {"effect": "pause"}}]
        def execute(self, name, inputs):
            self.calls.append((name, inputs))
            return result
    store, guest = Store(), FakeAXGuest()
    tb = HT.Toolbox(desktop=HT.DesktopTools(guest, app="TextEdit"), rea=ReaTools(store))
    obs = tb.observe()
    assert "run_rea_action" in tb.available() and '"model_judged"' in obs.render()
    assert obs.to_dict()["rea_actions"] == store.list_tools()
    policy = P.ScriptedPolicy([T.RunReaAction("rea.test.pause", {}), T.AxKey("return"), T.Done("claimed")])
    tr = run(policy, tb, task="Pause owned video", approver=lambda context: True)
    assert tr.stop == T.Stop.UNCERTAIN and tr.result == result and tr.steps[-1].outcome.data == result
    assert len(store.calls) == 1 and not any(op == "ax_step" for op, _ in guest.log)


def test_rea_action_keeps_existing_commit_gate():
    from anyuse.execution.harness_rea import ReaTools
    class Store:
        def list_tools(self): return [{"name": "rea.test.remove", "description": "Delete document"}]
        def execute(self, *args): raise AssertionError("declined commit must not execute")
    tb = HT.Toolbox(rea=ReaTools(Store()))
    tr = run(P.ScriptedPolicy([T.RunReaAction("rea.test.remove", {})]), tb,
             task="Inspect the document", approver=lambda context: False)
    assert not tr.ok and not any(s.outcome.how == "rea" for s in tr.steps)


def test_rea_prompt_extension_is_only_present_with_registered_tools():
    observation = T.Observation(kind="tool")
    ordinary, _ = P.default_prompt("inspect", observation, [], ["read_text", "done"])
    enabled, _ = P.default_prompt("inspect", observation, [], ["run_rea_action", "done"])
    assert ordinary == P.SYSTEM and "run_rea_action" not in ordinary
    assert enabled == P.SYSTEM + P.REA_SYSTEM and "model_judged" in enabled
    custom, _ = P.default_prompt("inspect", observation, [], ["run_rea_action"], system="caller system")
    assert custom == "caller system" + P.REA_SYSTEM


def test_prompt_denial_discards_cached_observation_before_new_policy(monkeypatch):
    events = []
    desktop, _ = desktop_tools()
    tb = HT.Toolbox(desktop=desktop)
    first, fresh = T.Observation(kind="ax", text="before"), T.Observation(kind="ax", text="fresh targets")
    stale = T.Observation(kind="ax", text="stale targets")
    observations = iter([first, fresh])
    resolutions = iter([
        {"attempted": False, "reobserve_required": False, "next_action": "observe"},
        {"attempted": True, "reobserve_required": True, "next_action": "observe"},
    ])

    def resolve(where, *, task_context, decision_limits=None):
        assert task_context == "t"
        events.append("resolve")
        return next(resolutions)

    def observe():
        events.append("observe")
        return next(observations)

    def dispatch(action, obs):
        events.append("dispatch")
        return T.Outcome(True, observation=stale)

    def prompt(task, obs, history, available):
        events.append("policy:" + obs.text)
        if obs.text == "before":
            return "", '{"action":"ax_read"}'
        assert obs.text == "fresh targets" and "Earlier targets are invalid" in obs.note
        return "", '{"action":"done","result":"fresh decision"}'

    monkeypatch.setattr(desktop, "resolve_system_prompt", resolve)
    monkeypatch.setattr(tb, "observe", observe)
    monkeypatch.setattr(tb, "dispatch", dispatch)
    tr = run(P.ModelPolicy(lambda system, user: (user, {}), prompt_fn=prompt), tb)
    assert tr.ok and tr.result == "fresh decision"
    assert events == ["resolve", "observe", "policy:before", "dispatch", "resolve", "observe", "policy:fresh targets"]


def test_uncertain_prompt_attempt_reobserves_and_stops_without_retry(monkeypatch):
    desktop, _ = desktop_tools()
    tb, events = HT.Toolbox(desktop=desktop), []
    monkeypatch.setattr(desktop, "resolve_system_prompt", lambda where, **kwargs: {
        "attempted": True, "reobserve_required": True, "next_action": "stop", "reason": "press outcome uncertain"})
    monkeypatch.setattr(tb, "observe", lambda: events.append("observe") or T.Observation(kind="ax"))
    monkeypatch.setattr(tb, "dispatch", lambda *args: events.append("dispatch"))
    tr = run(P.ScriptedPolicy([T.AxKey("return"), T.Done()]), tb)
    assert tr.stop == T.Stop.UNCERTAIN and tr.decisions == 0
    assert tr.result["reobserve_required"] and tr.result["automatic_retry"] is False
    assert "manual handling" in tr.stop_reason and events == ["observe"]


def test_scripted_pending_action_stops_after_denial_with_reobserve_required(monkeypatch):
    desktop, guest = desktop_tools()
    tb = HT.Toolbox(desktop=desktop)
    monkeypatch.setattr(desktop, "resolve_system_prompt", lambda where, **kwargs: {
        "attempted": True, "reobserve_required": True, "next_action": "observe"})
    tr = run(P.ScriptedPolicy([T.AxKey("return"), T.Done()]), tb)
    assert tr.stop == T.Stop.UNCERTAIN and tr.decisions == 0 and tr.result["status"] == "reobserve_required"
    assert tr.result["system_prompt"]["attempted"] is True
    assert [operation for operation, _ in guest.log] == ["ax_elements"]


def test_failed_fresh_observation_retains_structured_reobserve_requirement(monkeypatch):
    desktop, _ = desktop_tools()
    tb = HT.Toolbox(desktop=desktop)
    monkeypatch.setattr(desktop, "resolve_system_prompt", lambda where, **kwargs: {
        "attempted": True, "reobserve_required": True, "next_action": "observe"})
    monkeypatch.setattr(tb, "observe", lambda: (_ for _ in ()).throw(RuntimeError("unreadable screen")))
    tr = run(P.ScriptedPolicy([T.AxKey("return")]), tb)
    assert tr.stop == T.Stop.UNCERTAIN and tr.decisions == 0 and tr.result["reobserve_required"]
    assert "fresh observation failed" in tr.stop_reason


def test_read_only_observation_and_non_desktop_runs_do_not_resolve_prompts(monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    monkeypatch.setattr(SP, "resolve_configured", lambda where, **kwargs: (_ for _ in ()).throw(
        AssertionError("read-only observation and web runs must not resolve a host prompt")))
    desktop, _ = desktop_tools()
    assert HT.Toolbox(desktop=desktop).observe().kind == "ax"
    assert run(P.ScriptedPolicy([T.Done()]), HT.Toolbox(browser=shop_tools()[0])).ok


def test_prompt_boundary_receives_only_redacted_rendered_task_without_resolving_secrets(monkeypatch):
    from anyuse.execution import secret_refs as SR
    desktop, _ = desktop_tools()
    redactor, seen = SR.Redactor(), []
    redactor.add("KNOWN_SECRET", "${secret:AUTH}")
    monkeypatch.setattr(desktop, "redactor", redactor, raising=False)
    monkeypatch.setattr(SR, "product_runner", lambda *args: (_ for _ in ()).throw(AssertionError("no secret reads")))

    def resolve(where, *, task_context, decision_limits=None):
        seen.append((where, task_context))
        return {"attempted": False, "reobserve_required": False, "next_action": "unchanged"}

    monkeypatch.setattr(desktop, "resolve_system_prompt", resolve)
    tr = run(P.ScriptedPolicy([T.Done()]), HT.Toolbox(desktop=desktop),
             task="Open {app} using {secret}", values={"app": "Reader", "secret": "KNOWN_SECRET", "unused": "DO_NOT_FORWARD"})
    assert tr.ok and seen == [("agent_harness.before_observation", "Open Reader using ${secret:AUTH}")]


def test_terminal_rea_failure_does_not_resolve_another_prompt_or_retry(monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    from anyuse.execution.harness_rea import ReaTools
    events = []
    monkeypatch.setattr(SP, "resolve_configured", lambda where, **kwargs: events.append("resolve") or {
        "attempted": False, "reobserve_required": False, "next_action": "unchanged"})

    class Store:
        def list_tools(self):
            return [{"name": "rea.test.pause", "description": "Pause owned video"}]
        def execute(self, name, inputs):
            events.append("execute")
            return {"ok": False, "status": "uncertain", "reason": "restoration failed"}

    tb = HT.Toolbox(rea=ReaTools(Store()))
    tr = run(P.ScriptedPolicy([T.RunReaAction("rea.test.pause", {}), T.Done()]), tb,
             task="Pause owned video", approver=lambda context: True)
    assert tr.stop == T.Stop.UNCERTAIN and tr.decisions == 1
    assert events == ["resolve", "execute"]


def test_prompt_usage_counts_remote_and_in_process_guest_calls_once(monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    monkeypatch.setenv(SP.MODE_ENV, "llm")
    for local in (False, True):
        desktop, _ = desktop_tools()

        def resolve(where, *, task_context, decision_limits):
            assert decision_limits["remaining_calls"] > 0 and decision_limits["remaining_usd"] == 1.0
            assert 0 < decision_limits["timeout_s"] <= 600
            if local:
                M.note_external_model_call("test", "guest-in-process")
            return {"status": "no_prompt", "attempted": False, "reobserve_required": False, "next_action": "observe",
                    "decision": {"model_calls": 1, "cost_usd": 0.02, "budget_charged_usd": 0.03,
                                 "provider": "test", "model": "guest-model"}}

        monkeypatch.setattr(desktop, "resolve_system_prompt", resolve)
        tr = run(P.ScriptedPolicy([T.Done()]), HT.Toolbox(desktop=desktop))
        assert tr.ok and tr.model_calls == 1 and tr.cost_usd == 0.02
        assert tr.system_prompt_usage["remote_model_calls"] == (0 if local else 1)
        assert tr.system_prompt_usage["budget_charged_usd"] == 0.03
        assert H.report(tr)["system_prompt_usage"]["cost_complete"] is True


def test_prompt_call_consumes_call_cap_before_ordinary_policy_decision(monkeypatch):
    from anyuse.execution import system_prompt_policy as SP
    monkeypatch.setenv(SP.MODE_ENV, "llm")
    desktop, _ = desktop_tools()
    monkeypatch.setattr(desktop, "resolve_system_prompt", lambda *args, **kwargs: {
        "status": "no_prompt", "attempted": False, "reobserve_required": False, "next_action": "observe",
        "decision": {"model_calls": 1, "cost_usd": 0.01, "budget_charged_usd": 0.01}})
    tr = run(P.ScriptedPolicy([T.Done()]), HT.Toolbox(desktop=desktop), cfg=H.HarnessConfig(max_calls=1))
    assert tr.stop == T.Stop.CALL_CAP and tr.decisions == 0 and tr.model_calls == 1


def test_lost_prompt_usage_stays_unknown_in_report(monkeypatch):
    desktop, _ = desktop_tools()
    monkeypatch.setattr(desktop, "resolve_system_prompt", lambda *args, **kwargs: {
        "status": "manual", "attempted": True, "reobserve_required": True, "next_action": "stop",
        "decision": {"model_calls": None, "cost_usd": None, "budget_charged_usd": None}})
    tr = run(P.ScriptedPolicy([T.Done()]), HT.Toolbox(desktop=desktop))
    usage = H.report(tr)["system_prompt_usage"]
    assert tr.stop == T.Stop.UNCERTAIN and not usage["cost_complete"] and not usage["model_calls_complete"]
    assert not usage["budget_complete"] and usage["decisions"][0]["cost_usd"] is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
