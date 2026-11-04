"""human_solves_live wired into the browser runners (offline).

agent_browser (StepAgent), cdp_replay (CdpDriver) and attach (observe) open a
live solve on a detected challenge instead of stopping, for an allowed run
origin only; bench never opens one. The narrow guard exception lets the
challenge provider's own hosts through (GET/HEAD) only while a solve is active,
only for that page target, only the declared hosts, and it is logged and
revoked. No model is called on a challenge page. Fake CDP, fake view, fake
person; no socket, no browser, no model.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from anyuse.execution import agent_browser as AB
from anyuse.execution import agent_cdp as AC
from anyuse.execution import attach as A
from anyuse.execution import cdp_replay as R
from anyuse.execution import human_inbox as hi
from anyuse.execution import live_solve as ls
from test_attach import FakeWS as AttachWS
from test_attach import pin, snapshot

HSL = hi.HUMAN_SOLVES_LIVE
HOST = "a.example"
CHALLENGE = {"url": f"https://{HOST}/cdn-cgi/challenge-platform/h/b/orchestrate",
             "text": "Just a moment... Performing security verification"}
CLEARED = {"url": f"https://{HOST}/search?q=serde",
           "text": "Results for serde: serde, serde_json, serde_yaml and 40 more crates"}
CF = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0"


class FakeView:
    """Stands in for the loopback LiveView (no server)."""

    def __init__(self, page, ttl_s=0, clock=None, label=""):
        self.page = page
        self.closed = ""

    def start(self):
        return "http://127.0.0.1:1/v/token/"

    def is_open(self):
        return not self.closed

    def close(self, reason="closed"):
        self.closed = reason


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANYUSE_CHALLENGE_POLICY", "ANYUSE_LIVE_SOLVE_ORIGINS")}
        env["ANYUSE_DATA_DIR"] = os.path.join(self.tmp, "data")
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.ex = ls.ChallengeHostException()
        # the runners consult the module-level registry: point it at ours
        p = mock.patch.object(ls, "CHALLENGE_EXCEPTIONS", self.ex)
        p.start()
        self.addCleanup(p.stop)
        self.asked = []

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def solver(self, person, timeout_s=30):
        clock = Clock()

        def ask(req, message, view_url):
            self.asked.append((req, message, view_url))
            return ls.AskHandle("test")

        def sleep(s):
            clock.t += s
            person()
        return ls.LiveSolver(ask=ask, policy=HSL, timeout_s=timeout_s, clock=clock,
                             wall=clock, sleep=sleep, poll_s=1.0,
                             quarantine=ls.ModelQuarantine(), sessions=ls.SolvedSessions(),
                             view_factory=FakeView, exceptions=self.ex)


class ExceptionTests(Base):
    def test_only_during_a_solve_only_that_target_only_listed_hosts(self):
        ex = self.ex
        self.assertFalse(ex.allows("T1", CF, "GET"))              # no solve yet
        ex.grant("T1")
        self.assertTrue(ex.allows("T1", CF, "GET"))
        self.assertTrue(ex.allows("T1", CF, "HEAD"))
        self.assertTrue(ex.allows("T1", "https://www.google.com/recaptcha/api2/anchor?k=x"))
        self.assertTrue(ex.allows("T1", "https://js.hcaptcha.com/1/api.js"))
        self.assertFalse(ex.allows("T2", CF, "GET"))              # another page
        self.assertFalse(ex.allows("T1", CF, "POST"))             # GET/HEAD only
        for url in ("http://challenges.cloudflare.com/x",           # not https
                    "https://challenges.cloudflare.com:8443/x",     # other port
                    "https://user@challenges.cloudflare.com/x",     # userinfo
                    "https://challenges.cloudflare.com.evil.test/x",
                    "https://www.google.com/search?q=x",            # not the path
                    "https://www.google.com/recaptchax",
                    "https://tracker.example/x"):
            self.assertFalse(ex.allows("T1", url, "GET"), url)
        ex.revoke("T1", "cleared")
        self.assertFalse(ex.allows("T1", CF, "GET"))
        log = json.dumps(ex.log)
        self.assertNotIn("turnstile/if", log)                       # host + method only
        self.assertIn("challenges.cloudflare.com", log)
        self.assertEqual([e["event"] for e in ex.log if e["event"] != "request"],
                         ["granted", "revoked"])

    def test_solver_grants_for_its_target_and_revokes_after(self):
        seen = {}

        class Page:
            mode, target_id, viewport = ls.MODE_HEADLESS, "T1", (1280, 900)
            cleared = False

            def observe(self):
                return dict(CLEARED if self.cleared else CHALLENGE)

            def send(self, target, method, params):
                return {}
        page = Page()

        def person():
            seen["during"] = (self.ex.active("T1"), self.ex.active("T2"))
            page.cleared = True
        out = self.solver(person).solve(ls.SolveRequest("task-1", HOST, hi.ORIGIN_USER_TASK),
                                        page)
        self.assertEqual(out["status"], "cleared")
        self.assertEqual(seen["during"], (True, False))
        self.assertFalse(self.ex.active("T1"))

    def test_expired_solve_revokes_too(self):
        class Page:
            mode, target_id, viewport = ls.MODE_HEADLESS, "T1", (1280, 900)

            def observe(self):
                return dict(CHALLENGE)

            def send(self, *a):
                return {}
        out = self.solver(lambda: None, timeout_s=3).solve(
            ls.SolveRequest("task-1", HOST, hi.ORIGIN_USER_TASK), Page())
        self.assertEqual(out["status"], "expired")
        self.assertFalse(self.ex.active("T1"))

    def test_bench_never_opens_a_solve(self):
        s = self.solver(lambda: None)
        self.assertFalse(ls.solver_applies(s, hi.ORIGIN_BENCH))
        self.assertFalse(ls.solver_applies(None, hi.ORIGIN_USER_TASK))
        self.assertTrue(ls.solver_applies(s, hi.ORIGIN_USER_TASK))


class GateWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class AgentGateTests(Base):
    def paused(self, url, method="GET", rid="r1"):
        return {"method": "Fetch.requestPaused",
                "params": {"requestId": rid, "resourceType": "Document",
                           "request": {"url": url, "method": method}}}

    def verdicts(self, target, msgs):
        s = AC.CdpAgentSession("http://127.0.0.1:9345",
                               allow_url=lambda u: u.startswith(f"https://{HOST}/"))
        s._target, s._ws = target, GateWS()

        async def go():
            for m in msgs:
                await s._maybe_gate(m)
        asyncio.run(go())
        return [m["method"] for m in s._ws.sent]

    def test_provider_document_continues_only_during_a_solve_of_that_target(self):
        cont, fail = "Fetch.continueRequest", "Fetch.failRequest"
        self.assertEqual(self.verdicts("T1", [self.paused(CF)]), [fail])
        self.ex.grant("T1")
        self.assertEqual(self.verdicts("T1", [self.paused(CF)]), [cont])
        self.assertEqual(self.verdicts("T2", [self.paused(CF)]), [fail])
        self.assertEqual(self.verdicts("T1", [self.paused(CF, "POST")]), [fail])
        self.assertEqual(self.verdicts("T1", [self.paused("https://evil.test/x")]), [fail])
        self.assertEqual(self.verdicts("T1", [self.paused(f"https://{HOST}/p")]), [cont])
        self.ex.revoke("T1")
        self.assertEqual(self.verdicts("T1", [self.paused(CF)]), [fail])


class FakeAgentPage:
    """A pinned page for the StepAgent: a challenge until the person clears it."""

    def __init__(self, box):
        self.box = box
        self._target = "T1"
        self.refused = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def goto(self, url):
        pass

    async def observe(self):
        obs = dict(CLEARED if self.box.get("cleared") else CHALLENGE)
        obs["elements"] = []
        return obs

    async def challenge_observation(self):
        return await self.observe()

    async def call(self, method, params):
        self.box.setdefault("relayed", []).append(method)
        return {}

    def live_bridge(self, loop):
        return ls.AsyncPageBridge(loop, self._target, self.challenge_observation, self.call)

    async def consent_controls(self):
        return []

    async def dom(self):
        return "<html></html>"

    async def text(self):
        return (await self.observe())["text"]

    async def url(self):
        return (await self.observe())["url"]

    async def network(self, want_bodies=True):
        return []

    async def settle(self, s):
        pass


class AgentWiringTests(Base):
    def agent(self, box, origin, solver):
        seen = []

        def brain(messages, **kw):
            seen.append(messages[-1]["content"])
            return {"action": "done"}, {}, False
        factory = lambda endpoint, **kw: FakeAgentPage(box)          # noqa: E731
        a = AB.StepAgent(brain, session_factory=factory, endpoint="http://127.0.0.1:9345",
                         stay_on_site=False, live_solver=solver, origin=origin,
                         task_id="task-1")
        return a, seen

    def test_user_task_challenge_opens_a_solve_then_continues(self):
        box = {}
        agent, seen = self.agent(box, hi.ORIGIN_USER_TASK,
                                 self.solver(lambda: box.update(cleared=True)))
        run = agent.run("find serde", f"https://{HOST}/", "serde")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(run.live_solves[0]["status"], "cleared")
        self.assertEqual(run.stop, "done")
        self.assertEqual(len(seen), 1)                    # the model only after it cleared
        self.assertNotIn("Just a moment", seen[0])
        self.assertFalse(self.ex.active("T1"))

    def test_bench_origin_never_opens_a_solve(self):
        box = {}
        agent, seen = self.agent(box, hi.ORIGIN_BENCH,
                                 self.solver(lambda: box.update(cleared=True)))
        with mock.patch.dict(os.environ, {"ANYUSE_CHALLENGE_POLICY": HSL}):
            run = agent.run("find serde", f"https://{HOST}/", "serde")
        self.assertEqual(self.asked, [])
        self.assertEqual(run.stop, "blocked: challenge")
        self.assertEqual(seen, [])                        # no model on the challenge
        self.assertEqual(run.live_solves, [])

    def test_no_solver_stops_as_before(self):
        agent, seen = self.agent({}, hi.ORIGIN_USER_TASK, None)
        run = agent.run("find serde", f"https://{HOST}/", "serde")
        self.assertEqual(run.stop, "blocked: challenge")
        self.assertEqual(seen, [])

    def test_escalating_agent_passes_the_solver_through(self):
        s = self.solver(lambda: None)
        e = AB.EscalatingAgent(levels=[], live_solver=s, origin=hi.ORIGIN_USER_TASK,
                               task_id="t")
        self.assertIs(e.live_solver, s)
        self.assertEqual(e.origin, hi.ORIGIN_USER_TASK)


class ReplayWS:
    """A page websocket for CdpDriver._drive: a challenge that loads its check
    from challenges.cloudflare.com (a paused Document), cleared by the person."""

    def __init__(self, box):
        self.box = box
        self.inbox = []
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        m, mid = msg.get("method"), msg.get("id")
        if m in ("Fetch.continueRequest", "Fetch.failRequest"):
            self.box.setdefault("verdicts", []).append(
                (m, msg["params"]["requestId"]))
            return
        if m == "Page.navigate":
            self.inbox.append({"id": mid, "result": {"frameId": "F"}})
            return
        if m == "Runtime.evaluate":
            expr = msg["params"]["expression"]
            page = CLEARED if self.box.get("cleared") else CHALLENGE
            if "location.href" in expr:
                v = page["url"]
            elif "innerText" in expr:
                v = page["text"]
            elif "outerHTML" in expr:
                v = f"<html><body>{page['text']}</body></html>"
            else:
                v = None
            if self.box.get("solving") and not self.box.get("provider_sent"):
                # the challenge widget asks for its check while the person solves
                self.box["provider_sent"] = True
                self.inbox.append({"method": "Fetch.requestPaused", "params": {
                    "requestId": "cf1", "resourceType": "Document",
                    "request": {"url": CF, "method": "GET"}}})
                self.inbox.append({"method": "Fetch.requestPaused", "params": {
                    "requestId": "cf2", "resourceType": "Document",
                    "request": {"url": CF, "method": "POST"}}})
            self.inbox.append({"id": mid, "result": {"result": {"value": v}}})
            return
        self.inbox.append({"id": mid, "result": {}})

    async def recv(self):
        if not self.inbox:
            await asyncio.sleep(0.005)
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))


class ReplayWiringTests(Base):
    def drive(self, box, origin, solver):
        d = R.CdpDriver("http://127.0.0.1:9345", live_solver=solver, origin=origin,
                        task_id="task-1")
        ws = ReplayWS(box)
        with mock.patch("websockets.connect", lambda *a, **k: ws):
            out = asyncio.run(d._drive("ws://127.0.0.1:9345/devtools/page/T1",
                                       f"https://{HOST}/search?q=serde", 0.0, 0.2,
                                       url_policy=lambda u: "" if u.startswith(
                                           f"https://{HOST}/") else "other origin",
                                       target_id="T1"))
        return out, ws

    def test_replay_challenge_is_solved_in_the_same_page_and_read_again(self):
        box = {}

        def person():
            box["solving"] = True
            if box.get("provider_sent"):
                box["cleared"] = True
        out, _ws = self.drive(box, hi.ORIGIN_USER_TASK, self.solver(person))
        self.assertEqual(out.live_solve["status"], "cleared")
        self.assertIn("Results for serde", out.text)
        # the provider's GET went through during the solve, its POST did not
        self.assertIn(("Fetch.continueRequest", "cf1"), box["verdicts"])
        self.assertIn(("Fetch.failRequest", "cf2"), box["verdicts"])
        self.assertFalse(self.ex.active("T1"))
        self.assertGreaterEqual(out.live_solve["receipt"]["provider_requests_allowed"], 1)
        self.assertEqual(out.live_solve["receipt"]["models_on_challenge"], "none")

    def test_replay_bench_never_opens_a_solve(self):
        box = {}
        with mock.patch.dict(os.environ, {"ANYUSE_CHALLENGE_POLICY": HSL}):
            out, _ws = self.drive(box, hi.ORIGIN_BENCH,
                                  self.solver(lambda: box.update(cleared=True)))
        self.assertEqual(out.live_solve, {})
        self.assertEqual(self.asked, [])
        self.assertIn("Just a moment", out.text)

    def test_executor_hands_the_solver_to_its_driver(self):
        s = self.solver(lambda: None)
        with mock.patch.object(R, "cdp_endpoint", lambda e: "http://127.0.0.1:9345"):
            d = R.ReplayExecutor(live_solver=s, origin=hi.ORIGIN_USER_TASK).driver()
        self.assertIs(d.live_solver, s)
        self.assertEqual(d.origin, hi.ORIGIN_USER_TASK)


class ChallengeAttachWS(AttachWS):
    def __init__(self, box):
        super().__init__([], {}, None)
        self.box = box

    @property
    def snap(self):
        page = CLEARED if self.box.get("cleared") else CHALLENGE
        return snapshot(page["url"], ("html", {}, [("body", {}, [page["text"]])]))

    @snap.setter
    def snap(self, _v):
        pass


class AttachWiringTests(Base):
    def observe(self, box, origin, solver, seconds=1.5):
        ws = ChallengeAttachWS(box)
        rec = A.Recorder(pin=pin(), task="ex_search")
        asyncio.run(A.observe(ws, rec, max_seconds=seconds, delay_scale=0.01,
                              live_solver=solver, origin=origin, task_id="task-1"))
        return rec, ws

    def test_attached_challenge_asks_the_person_and_keeps_watching(self):
        box = {}
        rec, ws = self.observe(box, hi.ORIGIN_USER_TASK,
                               self.solver(lambda: box.update(cleared=True)))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("your own browser", self.asked[0][1])
        self.assertEqual(rec.live_solves[0]["status"], "cleared")
        self.assertEqual(rec.live_solves[0]["receipt"]["mode"], ls.MODE_ATTACH)
        # read-only throughout, and no challenge content was stored
        self.assertTrue(set(ws.sent) <= A.READ_ONLY_METHODS, ws.sent)
        self.assertFalse(any("Just a moment" in (s.get("text") or "")
                             for s in rec.snapshots))
        self.assertTrue(any("Results for serde" in (s.get("text") or "")
                            for s in rec.snapshots))
        self.assertFalse(self.ex.active(pin().target_id))           # attach: no exception
        self.assertIn("live_solves", rec.to_session("x"))

    def test_attach_without_origin_or_for_bench_never_asks(self):
        box = {}
        rec, _ = self.observe(box, None, self.solver(lambda: box.update(cleared=True)),
                              seconds=0.4)
        self.assertEqual((self.asked, rec.live_solves), ([], []))
        with mock.patch.dict(os.environ, {"ANYUSE_CHALLENGE_POLICY": HSL}):
            rec, _ = self.observe(box, hi.ORIGIN_BENCH,
                                  self.solver(lambda: box.update(cleared=True)), seconds=0.4)
        self.assertEqual((self.asked, rec.live_solves), ([], []))


if __name__ == "__main__":
    unittest.main()
