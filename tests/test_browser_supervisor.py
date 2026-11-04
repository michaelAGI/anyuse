"""execution/browser_supervisor.py and the hardened CDP sessions, offline.

A fake CDP (no socket, no browser, no container) that crashes, stalls or
drops the connection. What must hold (docs/TRAFFIC_POLICY.md "Browser
infrastructure"):

* a dead browser, a stalled page, a crashed or vanished target or a dropped
  websocket ends as ``InfrastructureFailure`` (category ``infrastructure``),
  never as a product failure;
* the page target is always closed explicitly, also when setup fails;
* a dropped connection re-pins the SAME target (never a new one) and only
  when no request interceptor was active; otherwise the page is closed;
* DOM/text reads are bounded in the page;
* the supervisor checks health BEFORE a site, restarts only an
  ``anyuse-browser-*`` container, and closes page targets a site left open.
"""
from __future__ import annotations

import asyncio
import errno
import json
import unittest
import urllib.error
from unittest import mock

from websockets.exceptions import ConnectionClosedError

from anyuse.execution import agent_cdp as AC
from anyuse.execution import browser_supervisor as BS
from anyuse.execution import cdp_guard as G
from anyuse.execution import cdp_replay as R

EP = "http://127.0.0.1:9345"
T1 = "AAAABBBBCCCCDDDD0001"


class FakeBrowser:
    """The browser side: targets, and how the page misbehaves."""

    def __init__(self, mode="ok"):
        self.mode = mode              # ok | crash | stall | drop | dead | fail_enable
        self.alive = True
        self.targets = []
        self.created = 0
        self.closed = []
        self.connects = []
        self.http = []

    # -- DevTools HTTP ---------------------------------------------------------
    def http_get(self, url, timeout=3.0):
        self.http.append(url)
        if not self.alive:
            raise urllib.error.URLError(OSError(errno.ETIMEDOUT, "Connect call failed"))
        if url.endswith("/json/version"):
            return {"Browser": "HeadlessChrome/151", "webSocketDebuggerUrl":
                    "ws://127.0.0.1:9345/devtools/browser/b1"}
        if url.endswith("/json/list"):
            return [{"id": t, "type": "page"} for t in self.targets]
        if "/json/close/" in url:
            tid = url.rsplit("/", 1)[1]
            if tid in self.targets:
                self.targets.remove(tid)
                self.closed.append(tid)
            return "Target is closing"
        raise AssertionError(url)

    async def connect(self, url, **kw):
        self.connects.append(url)
        if not self.alive:
            raise OSError(errno.ETIMEDOUT, "Connect call failed")
        return FakeWS(self, url)


class FakeWS:
    def __init__(self, browser, url):
        self.b = browser
        self.url = url
        self.page = "/devtools/page/" in url
        self.inbox = []
        self.sent = []
        self.dropped = False

    async def send(self, raw):
        if self.dropped:
            raise ConnectionClosedError(None, None)
        msg = json.loads(raw)
        self.sent.append(msg)
        m, mid = msg.get("method"), msg.get("id")
        b = self.b
        if m == "Target.createTarget":
            b.created += 1
            tid = f"AAAABBBBCCCCDDDD{b.created:04d}"
            b.targets.append(tid)
            self.inbox.append({"id": mid, "result": {"targetId": tid}})
            return
        if m == "Target.closeTarget":
            tid = msg["params"]["targetId"]
            if tid in b.targets:
                b.targets.remove(tid)
                b.closed.append(tid)
            self.inbox.append({"id": mid, "result": {"success": True}})
            return
        if m in ("Fetch.continueRequest", "Fetch.failRequest"):
            return
        if b.mode == "fail_enable" and m == "Page.enable":
            self.inbox.append({"id": mid, "error": {"message": "boom"}})
            return
        if m == "Runtime.evaluate":
            if b.mode == "crash":
                self.inbox.append({"method": "Inspector.targetCrashed", "params": {}})
                return
            if b.mode == "stall":
                return                                    # never answered
            if b.mode == "drop" and not getattr(self, "reconnected", False):
                self.dropped = True
                return
            self.inbox.append({"id": mid, "result": {"result": {"value": "<html>ok</html>"}}})
            return
        self.inbox.append({"id": mid, "result": {}})

    async def recv(self):
        if self.dropped:
            raise ConnectionClosedError(None, None)
        if not self.inbox:
            await asyncio.sleep(0.01)
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))

    async def close(self):
        return None


def session_cls(browser, base=AC.CdpAgentSession):
    class S(base):
        async def _ws_connect(self, url):
            ws = await browser.connect(url)
            if browser.mode == "drop" and len([c for c in browser.connects
                                               if "/page/" in c]) > 1:
                ws.reconnected = True
            return ws
    return S


def run(coro):
    return asyncio.run(coro)


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(BS.classify(ConnectionClosedError(None, None)), BS.CONNECTION_LOST)
        self.assertEqual(BS.classify(OSError(errno.ETIMEDOUT, "x")), BS.UNREACHABLE)
        self.assertEqual(BS.classify(urllib.error.URLError(ConnectionRefusedError())),
                         BS.UNREACHABLE)
        self.assertEqual(BS.classify(BS.InfrastructureFailure(BS.STALLED)), BS.STALLED)
        # a bare timeout (a slow site, a slow model) is not infrastructure by itself
        self.assertIsNone(BS.classify(TimeoutError("timed out")))
        self.assertIsNone(BS.classify(ValueError("bad selector")))
        self.assertIsNone(BS.classify(RuntimeError("Runtime.evaluate: {'message': 'x'}")))

    def test_confirm_a_timeout_with_a_health_ping(self):
        dead, alive = FakeBrowser(), FakeBrowser()
        dead.alive = False
        err = BS.confirm(TimeoutError("timed out"), EP, http_get=dead.http_get)
        self.assertIsInstance(err, BS.InfrastructureFailure)
        self.assertEqual(err.kind, BS.UNREACHABLE)
        self.assertIsNone(BS.confirm(TimeoutError("timed out"), EP, http_get=alive.http_get))

    def test_probe(self):
        b = FakeBrowser()
        self.assertTrue(BS.probe(EP, http_get=b.http_get)[0])
        b.alive = False
        ok, why = BS.probe(EP, http_get=b.http_get)
        self.assertFalse(ok)
        self.assertIn("URLError", why)


class WatchAndBoundTests(unittest.TestCase):
    def test_crash_and_destroy_events(self):
        w = BS.PageWatch(T1)
        w.see({"method": "Target.targetDestroyed", "params": {"targetId": "OTHER0000000"}})
        w.check()                                          # another target: ignored
        w.see({"method": "Inspector.targetCrashed", "params": {}})
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            w.check()
        self.assertEqual(cm.exception.kind, BS.TARGET_CRASHED)
        w2 = BS.PageWatch(T1)
        w2.see({"method": "Target.targetDestroyed", "params": {"targetId": T1}})
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            w2.check()
        self.assertEqual(cm.exception.kind, BS.TARGET_GONE)
        w3 = BS.PageWatch(T1)
        w3.see({"method": "Inspector.detached", "params": {"reason": "Render process gone."}})
        self.assertEqual(w3.failed.kind, BS.TARGET_CRASHED)

    def test_bounded_expression(self):
        e = BS.bounded("document.title", 10)
        self.assertIn("slice(0,10)", e)
        self.assertIn(str(BS.MAX_DOM_CHARS), BS.DOM_EXPR)
        self.assertIn(str(BS.MAX_TEXT_CHARS), BS.TEXT_EXPR)


class SessionTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(BS, "http_get_json", side_effect=self._http)
        p.start()
        self.addCleanup(p.stop)
        self.b = FakeBrowser()

    def _http(self, url, timeout=3.0):
        return self.b.http_get(url, timeout)

    def test_healthy_session_reads_bounded_and_closes_its_page(self):
        S = session_cls(self.b)

        async def go():
            async with S(EP) as page:
                dom = await page.dom()
                return page, dom
        page, dom = run(go())
        self.assertEqual(dom, "<html>ok</html>")
        page_ws_sent = [m for m in page._ws.sent if m.get("method") == "Runtime.evaluate"]
        self.assertIn("slice(0,", page_ws_sent[0]["params"]["expression"])
        self.assertIn("Inspector.enable", [m.get("method") for m in page._ws.sent])
        self.assertEqual(self.b.targets, [])               # closed explicitly
        self.assertEqual(self.b.closed, ["AAAABBBBCCCCDDDD0001"])

    def test_crashed_page_is_infrastructure_and_closed(self):
        self.b.mode = "crash"
        S = session_cls(self.b)

        async def go():
            async with S(EP) as page:
                await page.dom()
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            run(go())
        self.assertEqual(cm.exception.kind, BS.TARGET_CRASHED)
        self.assertEqual(self.b.targets, [])

    def test_stalled_page_with_live_browser_is_stalled(self):
        self.b.mode = "stall"
        S = session_cls(self.b)

        async def go():
            async with S(EP) as page:
                await page.call("Runtime.evaluate", {"expression": "1"}, timeout=0.1)
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            run(go())
        self.assertEqual(cm.exception.kind, BS.STALLED)
        self.assertEqual(self.b.targets, [])

    def test_stalled_page_with_dead_browser_is_unreachable(self):
        self.b.mode = "stall"
        S = session_cls(self.b)

        async def go():
            async with S(EP) as page:
                self.b.alive = False
                await page.call("Runtime.evaluate", {"expression": "1"}, timeout=0.1)
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            run(go())
        self.assertEqual(cm.exception.kind, BS.UNREACHABLE)

    def test_dead_browser_fails_before_any_target(self):
        self.b.alive = False
        S = session_cls(self.b)

        async def go():
            async with S(EP):
                pass
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            run(go())
        self.assertEqual(cm.exception.kind, BS.UNREACHABLE)
        self.assertEqual(self.b.created, 0)

    def test_setup_failure_closes_the_target(self):
        self.b.mode = "fail_enable"
        S = session_cls(self.b)

        async def go():
            async with S(EP):
                pass
        with self.assertRaises(RuntimeError):
            run(go())
        self.assertEqual(self.b.created, 1)
        self.assertEqual(self.b.targets, [])

    def test_drop_without_interceptor_repins_the_same_target(self):
        self.b.mode = "drop"
        S = session_cls(self.b)

        async def go():
            async with S(EP) as page:
                return await page.dom(), page
        dom, page = run(go())
        self.assertEqual(dom, "<html>ok</html>")
        self.assertEqual(self.b.created, 1)                 # never a new target
        pages = [c for c in self.b.connects if "/devtools/page/" in c]
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0], pages[1])                # the SAME target
        self.assertEqual(page.reconnects, 1)

    def test_drop_is_never_retried_for_an_action(self):
        self.b.mode = "drop"
        S = session_cls(self.b)

        async def go():
            async with S(EP) as page:
                await page.js("document.querySelector('a').click()")
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            run(go())
        self.assertEqual(cm.exception.kind, BS.CONNECTION_LOST)
        self.assertEqual(self.b.targets, [])

    def test_drop_with_interceptor_closes_the_page_and_fails(self):
        self.b.mode = "drop"
        S = session_cls(self.b)

        async def go():
            async with S(EP, allow_url=lambda u: True) as page:
                await page.dom()
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            run(go())
        self.assertEqual(cm.exception.kind, BS.CONNECTION_LOST)
        self.assertEqual(len([c for c in self.b.connects if "/page/" in c]), 1)
        self.assertEqual(self.b.targets, [])

    def test_vanished_target_is_not_replaced(self):
        self.b.mode = "drop"
        S = session_cls(self.b)

        async def go():
            async with S(EP) as page:
                self.b.targets.clear()                      # the page is gone
                await page.dom()
        with self.assertRaises(BS.InfrastructureFailure) as cm:
            run(go())
        self.assertEqual(cm.exception.kind, BS.TARGET_GONE)
        self.assertEqual(self.b.created, 1)

    def test_guarded_session_never_reconnects(self):
        budget = mock.Mock()
        s = G.GuardedSession(EP, budget=budget)
        self.assertTrue(s._intercepting)


class ReplayTests(unittest.TestCase):
    def test_infrastructure_is_not_a_workflow_failure(self):
        class DeadDriver:
            def open(self, url, settle_s, timeout_s):
                raise BS.InfrastructureFailure(BS.UNREACHABLE, "container gone")
        t = R.ReplayTemplate(url="https://books.example.test/search?q={q}",
                             params=["q"], extract={"items": "li.r"},
                             expect={"selector": "li.r", "min_items": 1})
        with mock.patch("anyuse.execution.api_permission.replay_refusal", return_value=""), \
                mock.patch("anyuse.execution.blocked.blocked_until", return_value=None):
            res = R.ReplayExecutor(driver=DeadDriver()).execute(t, {"q": "x"})
        self.assertFalse(res["success"])
        self.assertEqual(res["outcome_kind"], BS.INFRASTRUCTURE)
        self.assertEqual(res["infrastructure"], BS.UNREACHABLE)


class ContainerTests(unittest.TestCase):
    def test_only_anyuse_browser_containers(self):
        for name in ("laya-offline", "sandboxed-chrome", "bench-chrome-1", "anyuse-browser-",
                     "anyuse-browser-X;rm", "anyuse-webarch-", "anyuse-webarch-1;rm",
                     "film-tools", "postgres-db", "other-project-1", "anyuse-webarchx-1",
                     "anyuse-webarch2-", "anyuse-webarch2-1;rm", "anyuse-webarch3-1"):
            with self.assertRaises(ValueError):
                BS.ContainerController(BS.ContainerSpec(name=name), run=lambda a: None)
        # the web-architecture bench's own browsers
        BS.ContainerController(BS.ContainerSpec(name="anyuse-webarch-1"), run=lambda a: None)
        # its ``anyuse-webarch2-`` prefix
        BS.ContainerController(BS.ContainerSpec(name="anyuse-webarch2-1"), run=lambda a: None)
        with self.assertRaises(ValueError):
            BS.ContainerSpec(name="anyuse-browser-1", publish="0.0.0.0:9345:9222").check()
        with self.assertRaises(ValueError):
            BS.ContainerSpec(name="anyuse-browser-1",
                             chrome_args=("--remote-debugging-address=0.0.0.0",)).check()

    def test_restart_argv_limits_and_health(self):
        calls, b = [], FakeBrowser()
        c = BS.ContainerController(BS.ContainerSpec(name="anyuse-browser-1", memory="2g",
                                                    cpus=2),
                                   run=lambda argv: calls.append(argv) or mock.Mock(returncode=0),
                                   sleep=lambda s: None, http_get=b.http_get)
        c.restart()
        self.assertEqual(calls[0], ["container", "stop", "anyuse-browser-1"])
        run_argv = calls[1]
        for flag in ("-m", "2g", "-c", "2", "-p", "127.0.0.1:9345:9222", "--rm",
                     "--disable-dev-shm-usage"):
            self.assertIn(flag, run_argv)
        self.assertEqual(c.restarts, 1)

    def test_restart_that_never_gets_healthy_is_infrastructure(self):
        b = FakeBrowser()
        b.alive = False
        c = BS.ContainerController(BS.ContainerSpec(name="anyuse-browser-1"),
                                   run=lambda argv: mock.Mock(returncode=0),
                                   sleep=lambda s: None, http_get=b.http_get)
        with self.assertRaises(BS.InfrastructureFailure):
            c.start(wait_s=0)


class SupervisorTests(unittest.TestCase):
    def test_dead_browser_fails_the_site_before_any_request(self):
        b = FakeBrowser()
        b.alive = False
        sup = BS.BrowserSupervisor(EP, http_get=b.http_get)
        with self.assertRaises(BS.InfrastructureFailure):
            sup.require("books.example.test")
        self.assertTrue(all(u.startswith(EP) for u in b.http))   # only our endpoint

    def test_unhealthy_browser_is_restarted_once(self):
        b = FakeBrowser()
        b.alive = False

        def run_cli(argv):
            if argv[1] == "run":
                b.alive = True
            return mock.Mock(returncode=0)
        c = BS.ContainerController(BS.ContainerSpec(name="anyuse-browser-1"), run=run_cli,
                                   sleep=lambda s: None, http_get=b.http_get)
        sup = BS.BrowserSupervisor(EP, container=c, http_get=b.http_get)
        h = sup.require("books.example.test")
        self.assertTrue(h.ok and h.restarted)
        self.assertEqual(c.restarts, 1)

    def test_restart_between_sites_and_close_strays(self):
        b = FakeBrowser()
        b.targets = ["KEEPKEEPKEEP0001"]
        c = BS.ContainerController(BS.ContainerSpec(name="anyuse-browser-1"),
                                   run=lambda argv: mock.Mock(returncode=0),
                                   sleep=lambda s: None, http_get=b.http_get)
        sup = BS.BrowserSupervisor(EP, container=c, restart_between_sites=True,
                                   http_get=b.http_get)
        sup.before_site("a.example.test")
        self.assertEqual(c.restarts, 1)
        b.targets += ["LEFTOPEN00000001", "LEFTOPEN00000002"]
        closed = sup.after_site("a.example.test")
        self.assertEqual(closed, ["LEFTOPEN00000001", "LEFTOPEN00000002"])
        self.assertEqual(b.targets, ["KEEPKEEPKEEP0001"])

    def test_too_many_targets(self):
        b = FakeBrowser()
        b.targets = [f"PAGEPAGEPAGE{i:04d}" for i in range(6)]
        sup = BS.BrowserSupervisor(EP, http_get=b.http_get, max_page_targets=4)
        closed = sup.check_targets(keep=["PAGEPAGEPAGE0000"])
        self.assertEqual(len(closed), 5)
        self.assertEqual(b.targets, ["PAGEPAGEPAGE0000"])


class AgentTests(unittest.TestCase):
    def test_dead_browser_is_an_infrastructure_stop_not_an_agent_failure(self):
        from anyuse.execution import agent_browser as AB

        class Dead:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise BS.InfrastructureFailure(BS.UNREACHABLE, "gone")

            async def __aexit__(self, *a):
                return False
        agent = AB.StepAgent(lambda m: ({"action": "done"}, {}, False),
                             session_factory=Dead, endpoint=EP, stay_on_site=False)
        r = agent.run("find books", "https://books.example.test/", "x")
        self.assertEqual(r.stop, AB.INFRASTRUCTURE_STOP)


class BenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))
        import live_bench
        cls.V = live_bench

    def test_infrastructure_is_in_no_rate(self):
        import json as _j
        import os
        import tempfile
        V = self.V
        info = {"site": "a.example.test", "status": "started"}
        V.infrastructure_entry(info, ConnectionClosedError(None, None), "mid-site")
        self.assertEqual((info["status"], info["outcome"]), ("done", "infrastructure"))
        self.assertEqual(info["infrastructure"]["kind"], BS.CONNECTION_LOST)
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "c1_S"))
            entries = {"a.example.test": info,
                       "b.example.test": {"site": "b.example.test", "status": "done",
                                          "outcome": "failed", "failure": "compile"}}
            with open(os.path.join(root, "c1_S", "state.json"), "w") as fh:
                _j.dump({"cohort": 1, "arm": "S", "entries": entries}, fh)
            rep = V.report(root)
        row = next(l for l in rep.splitlines() if l.startswith("| 1 | S |"))
        self.assertEqual(row.split("|")[3].strip(), "1")      # sampled: b only

    def test_supervisor_from_args_touches_only_anyuse_containers(self):
        import argparse
        V = self.V
        ok = V.make_supervisor(argparse.Namespace(browser="http://127.0.0.1:9346",
                                                  container="anyuse-browser-2",
                                                  container_memory="3g", container_cpus=2,
                                                  restart_between_sites=True), print)
        self.assertEqual(ok.container.spec.publish, "127.0.0.1:9346:9222")
        self.assertTrue(ok.restart_between_sites)
        with self.assertRaises(ValueError):
            V.make_supervisor(argparse.Namespace(browser="http://127.0.0.1:9345",
                                                 container="bench-chrome-1"), print)
        plain = V.make_supervisor(argparse.Namespace(browser="http://127.0.0.1:9345"), print)
        self.assertIsNone(plain.container)
        self.assertFalse(plain.restart_between_sites)


if __name__ == "__main__":
    unittest.main()
