"""Human solves live (offline): a person solves a challenge; AnyUse only relays.

A fake CDP browser with two page targets (T1 shows a fake challenge that
clears after two relayed clicks on its checkbox; T2 is another page), a fake
attached page, fake clocks and a fake terminal. The real stdlib HTTP handler
parses requests and produces responses over an in-memory transport. The real
server binding method receives a fake socket; no sockets, desktop or model.
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

from cryptography.fernet import Fernet

from anyuse import settings
from anyuse.execution import human_gate, human_inbox as hi, live_solve as ls
from anyuse.execution.routine import Engine, ReceiptStore, outcome
from test_skill_manifest import base, proved_step

KEY = Fernet.generate_key()
HOST = "a.example"
TURNSTILE = {"blocked": True, "reason": "captcha_turnstile", "host": HOST,
             "status": 403, "evidence": ["matched 'cf-turnstile'"]}
CHECKBOX = (100, 200, 140, 240)             # x0, y0, x1, y1 of the fake checkbox
HSL = hi.HUMAN_SOLVES_LIVE


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeBrowser:
    """Two targets. T1 is challenged until ``clicks_needed`` presses land on
    the checkbox; ``Storage.clearDataForOrigin`` brings the challenge back."""

    def __init__(self, clicks_needed=2):
        self.sent = []
        self.clicks, self.clicks_needed = 0, clicks_needed

    @property
    def cleared(self):
        return self.clicks >= self.clicks_needed

    def send(self, target, method, params):
        self.sent.append((target, method, json.loads(json.dumps(params))))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\xff\xd8JPEG:" + target.encode()).decode()}
        if (method == "Input.dispatchMouseEvent" and target == "T1"
                and params["type"] == "mousePressed"):
            x0, y0, x1, y1 = CHECKBOX
            if x0 <= params["x"] <= x1 and y0 <= params["y"] <= y1:
                self.clicks += 1
        if method == "Storage.clearDataForOrigin" and target == "T1":
            self.clicks = 0
        return {}

    def observe(self, target="T1"):
        if target == "T1" and not self.cleared:
            return {"url": f"https://{HOST}/cdn-cgi/challenge-platform/h/b/orchestrate",
                    "text": "Just a moment... Performing security verification"}
        return {"url": f"https://{HOST}/search?q=serde",
                "text": "Results for serde: serde, serde_json, serde_yaml and 40 more crates",
                "items": ["serde", "serde_json"]}


class FakePage:
    """What a runner hands the solver for its pinned page."""

    def __init__(self, browser, mode=ls.MODE_HEADLESS, target_id="T1"):
        self.browser, self.mode, self.target_id = browser, mode, target_id
        self.viewport = (1280, 900)
        if mode == ls.MODE_HEADLESS:
            self.send = browser.send

    def observe(self):
        return self.browser.observe(self.target_id)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def http_call(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=dict(headers or {}))
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


class Viewer:
    """The person's browser tab on the live view."""

    def __init__(self, url):
        parts = urllib.parse.urlsplit(url)
        self.port, self.path = parts.port, parts.path
        self.origin = f"http://127.0.0.1:{self.port}"
        self.cookie = ""

    def open(self):
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        status, headers, body = http_call(self.port, "GET", self.path, headers)
        if status == 200 and "set-cookie" in headers:
            self.cookie = headers["set-cookie"].split(";")[0]
        return status, headers, body

    def _h(self, **extra):
        return {"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie, **extra}

    def frame(self):
        return http_call(self.port, "GET", self.path + "frame", self._h())

    def send(self, events, origin=None):
        return http_call(self.port, "POST", self.path + "input",
                         self._h(**{"Origin": origin or self.origin,
                                    "Content-Type": "application/json"}),
                         json.dumps(events).encode())

    def click(self, x=120, y=220):
        return self.send([{"t": "down", "x": x, "y": y, "button": "left"},
                          {"t": "up", "x": x, "y": y, "button": "left"}])


class FixtureOrigins:
    """Run the production server binding and handler without any OS sockets."""

    def __init__(self):
        self.servers = {}
        self.next_port = 48000
        self.client_address = ("127.0.0.1", 12345)
        fixture = self

        class MemoryServer(ls._Server):
            def __init__(self, address, handler):
                fixture.next_port += 1
                sock = mock.Mock()
                sock.getsockname.return_value = (address[0], fixture.next_port)
                # Exercise TCPServer.__init__ and the production server_bind
                # override, including its avoidance of reverse DNS lookup.
                with mock.patch("socket.socket", return_value=sock):
                    super().__init__(address, handler)
                sock.bind.assert_called_once_with(address)
                sock.listen.assert_called_once()
                self.stopped = threading.Event()
                self.finished = threading.Event()
                fixture.servers[self.server_port] = self

            def serve_forever(self, poll_interval=.1):
                self.stopped.wait()
                self.finished.set()

            def shutdown(self):
                self.stopped.set()
                self.finished.wait(1)

            def server_close(self):
                fixture.servers.pop(self.server_port, None)
                super().server_close()

        class BytesSocket:
            def __init__(self, incoming):
                self.incoming = io.BytesIO(incoming)
                self.outgoing = io.BytesIO()

            def makefile(self, *_):
                return self.incoming

            def sendall(self, data):
                self.outgoing.write(data)

        class MemoryConnection:
            def __init__(self, host, port, timeout=None):
                if host != "127.0.0.1":
                    raise AssertionError("in-memory client only supports this fixture's loopback host")
                self.port, self.response = port, None

            def request(self, method, path, body=None, headers=None):
                if self.port not in fixture.servers:
                    raise ConnectionRefusedError("fixture server is closed")
                body = body.encode() if isinstance(body, str) else (body or b"")
                headers = dict(headers or {})
                if not any(k.lower() == "host" for k in headers):
                    headers["Host"] = f"127.0.0.1:{self.port}"
                if not any(k.lower() == "content-length" for k in headers):
                    headers["Content-Length"] = str(len(body))
                head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                connection = BytesSocket(head.encode("latin-1") + b"\r\n" + body)
                server = fixture.servers[self.port]
                # BaseHTTPRequestHandler's real request parser, _route(),
                # cookie/origin checks, relay and response writer all run.
                server.RequestHandlerClass(connection, fixture.client_address, server)
                self.response = http.client.HTTPResponse(BytesSocket(connection.outgoing.getvalue()))
                self.response.begin()

            def getresponse(self):
                return self.response

            def close(self):
                if self.response is not None:
                    self.response.close()

        self.server_patch = mock.patch.object(ls, "_Server", MemoryServer)
        self.client_patch = mock.patch.object(http.client, "HTTPConnection", MemoryConnection)
        self.server_patch.start()
        self.client_patch.start()

    def allow(self, url):
        parts = urllib.parse.urlsplit(url)
        assert parts.hostname == "127.0.0.1" and parts.port in self.servers

    def restore(self):
        for server in list(self.servers.values()):
            server.shutdown()
            server.server_close()
        self.client_patch.stop()
        self.server_patch.stop()


class Tty:
    def __init__(self):
        self.written = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.written)

    def readline(self):
        return re.findall(r"type this code: (\w+)", self.text)[-1] + "\n"


def gate(tmp, tty, env=None):
    return human_gate.TtyGate(env=env or {}, stdio_are_ttys=lambda: True, open_tty=tty,
                              nonce_factory=lambda: "n0", hostname="h",
                              optin_file=os.path.join(tmp, "optin"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fixture = FixtureOrigins()
        self.inbox = hi.Inbox(os.path.join(self.tmp, "inbox"), key_provider=lambda: KEY)
        self.quarantine = ls.ModelQuarantine()
        self.sessions = ls.SolvedSessions()
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANYUSE_CHALLENGE_POLICY", "ANYUSE_LIVE_SOLVE_ORIGINS")}
        env["ANYUSE_DATA_DIR"] = os.path.join(self.tmp, "data")
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.fixture.restore()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def solver(self, ask, *, origins=None, clock=None, sleep=None, timeout_s=60, **kw):
        clock = clock or Clock()

        def default_sleep(s):
            clock.t += s
        return ls.LiveSolver(ask=ask, policy=HSL, origins=origins, timeout_s=timeout_s,
                             clock=clock, wall=clock, sleep=sleep or default_sleep,
                             poll_s=1.0, quarantine=self.quarantine,
                             sessions=self.sessions, inbox=self.inbox, **kw)


def human_over_view(test, box, browser, spy_hook=None):
    """A sleep() that is the person: opens the view once and clicks twice."""
    state = {"n": 0}

    def sleep(_s):
        state["n"] += 1
        if spy_hook is not None:                  # a model hook while it is a challenge
            with test.assertRaises(ls.ChallengeQuarantined):
                spy_hook(target_id="T1", observation=browser.observe("T1"))
        if state["n"] == 1:
            test.fixture.allow(box["url"])
            viewer = Viewer(box["url"])
            box["viewer"] = viewer
            status, headers, body = viewer.open()
            test.assertEqual(status, 200)
            test.assertIn(b"Solve the human check", body)
            test.assertIn("no-store", headers["cache-control"])
            status, headers, frame = viewer.frame()
            test.assertEqual((status, headers["content-type"]), (200, "image/jpeg"))
            test.assertTrue(frame.startswith(b"\xff\xd8JPEG:T1"))
        else:
            test.assertEqual(box["viewer"].click()[0], 204)
    return sleep


def capture_ask(box, channel="test"):
    def ask(req, message, view_url):
        box["message"], box["url"], box["req"] = message, view_url, req
        box["asked"] = box.get("asked", 0) + 1
        return ls.AskHandle(channel)
    return ask


# ---------------------------------------------------------------------------
# policy, origins and settings
# ---------------------------------------------------------------------------

class PolicyTests(Base):
    def test_default_origins_only_user_task(self):
        self.assertEqual(hi.challenge_policy(HSL, origin=hi.ORIGIN_USER_TASK), HSL)
        for origin in (hi.ORIGIN_SCHEDULED, hi.ORIGIN_BULK, None, "unknown"):
            self.assertEqual(hi.challenge_policy(HSL, origin=origin), hi.HUMAN_DOES_STEP)

    def test_origins_setting_widens_to_scheduled_and_bulk(self):
        os.environ["ANYUSE_LIVE_SOLVE_ORIGINS"] = "user_task,scheduled,bulk"
        for origin in (hi.ORIGIN_USER_TASK, hi.ORIGIN_SCHEDULED, hi.ORIGIN_BULK):
            self.assertEqual(hi.challenge_policy(HSL, origin=origin), HSL)
        os.environ["ANYUSE_LIVE_SOLVE_ORIGINS"] = "scheduled"
        self.assertEqual(hi.challenge_policy(HSL, origin=hi.ORIGIN_USER_TASK),
                         hi.HUMAN_DOES_STEP)
        self.assertEqual(hi.challenge_policy(HSL, origin=hi.ORIGIN_SCHEDULED), HSL)

    def test_bench_is_never_allowed_whatever_the_setting(self):
        os.environ["ANYUSE_LIVE_SOLVE_ORIGINS"] = "user_task,scheduled,bulk,bench"
        self.assertNotIn("bench", hi.live_solve_origins())
        self.assertEqual(hi.challenge_policy(HSL, origin=hi.ORIGIN_BENCH), hi.HUMAN_DOES_STEP)
        self.assertEqual(hi.challenge_policy(HSL, origin=hi.ORIGIN_BENCH,
                                             origins="bench"), hi.HUMAN_DOES_STEP)
        with self.assertRaises(settings.SettingError):
            settings.parse_for_storage("ANYUSE_LIVE_SOLVE_ORIGINS", "user_task,bench")
        with self.assertRaises(hi.LiveSolveRefused):
            hi.refuse_bench_live_solve(hi.ORIGIN_BENCH, HSL)
        with self.assertRaises(hi.LiveSolveRefused):
            self.solver(capture_ask({})).applies(hi.ORIGIN_BENCH)
        box = {}
        with self.assertRaises(hi.LiveSolveRefused):
            self.solver(capture_ask(box)).solve(
                ls.SolveRequest("t1", HOST, hi.ORIGIN_BENCH), FakePage(FakeBrowser()))
        self.assertNotIn("asked", box)

    def test_bench_engine_refuses_the_policy_outright(self):
        os.environ["ANYUSE_CHALLENGE_POLICY"] = HSL
        with self.assertRaises(hi.LiveSolveRefused):
            Engine(chal_manifest(), {}, ReceiptStore(self.tmp), origin=hi.ORIGIN_BENCH)
        os.environ["ANYUSE_CHALLENGE_POLICY"] = hi.HUMAN_DOES_STEP
        Engine(chal_manifest(), {}, ReceiptStore(self.tmp), origin=hi.ORIGIN_BENCH)
        with self.assertRaises(ValueError):
            Engine(chal_manifest(), {}, ReceiptStore(self.tmp), origin="cron")

    def test_settings_are_security_relevant_and_warn(self):
        pol = settings.SETTINGS["ANYUSE_CHALLENGE_POLICY"]
        self.assertTrue(pol.security_relevant)
        self.assertTrue(pol.needs_confirmation(HSL))
        self.assertFalse(pol.needs_confirmation(hi.HUMAN_DOES_STEP))
        org = settings.SETTINGS["ANYUSE_LIVE_SOLVE_ORIGINS"]
        self.assertTrue(org.security_relevant)
        self.assertEqual(org.default, "user_task")
        self.assertFalse(org.needs_confirmation("user_task"))
        self.assertTrue(org.needs_confirmation(
            settings.parse_for_storage(org.name, "bulk, user_task")[1]))
        self.assertEqual(settings.parse_for_storage(org.name, "bulk,user_task")[1],
                         "user_task,bulk")
        self.assertIn("WARNING", settings.security_warning(org.name, "user_task,scheduled"))
        self.assertEqual(settings.security_warning(org.name, "user_task"), "")
        self.assertIn("WARNING", settings.security_warning(pol.name, HSL))
        with self.assertRaises(settings.SettingError):
            settings.parse_for_storage("ANYUSE_LIVE_SOLVE_TIMEOUT_S", "5")
        self.assertEqual(settings.get("ANYUSE_LIVE_SOLVE_TIMEOUT_S"), 900)


# ---------------------------------------------------------------------------
# the relay and the view
# ---------------------------------------------------------------------------

class RelayTests(Base):
    def test_only_pinned_target_and_input_methods(self):
        br = FakeBrowser()
        page = ls.PinnedPage("T1", br.send)
        page.relay({"t": "down", "x": 120, "y": 220})
        page.relay({"t": "keydown", "key": "a", "text": "a", "code": "KeyA", "vk": 65})
        page.screenshot()
        for method in ("Page.navigate", "Runtime.evaluate", "Target.createTarget",
                       "Input.insertText", "Fetch.enable"):
            with self.assertRaises(ls.RelayRefused):
                page._call(method, {}, ls.RELAY_METHODS | ls.VIEW_METHODS)
        self.assertEqual({t for t, _, _ in br.sent}, {"T1"})
        self.assertEqual([m for _, m, _ in br.sent], ["Input.dispatchMouseEvent",
                                                      "Input.dispatchKeyEvent",
                                                      "Page.captureScreenshot"])
        with self.assertRaises(ValueError):
            ls.PinnedPage("", br.send)

    def test_events_are_rebuilt_and_bounded(self):
        vp = (1280, 900)
        method, params = ls.sanitize_event(
            {"t": "down", "x": 5, "y": 6, "button": "left", "url": "https://evil.example",
             "method": "Page.navigate"}, vp)
        self.assertEqual(method, "Input.dispatchMouseEvent")
        self.assertNotIn("url", params)
        refused = [
            {"t": "down", "x": 5000, "y": 6}, {"t": "down", "x": float("nan"), "y": 1},
            {"t": "keydown", "key": "l", "mods": 2}, {"t": "keydown", "key": "r", "mods": 4},
            {"t": "keydown", "key": "ArrowLeft", "mods": 1},
            {"t": "keydown", "key": "BrowserBack"}, {"t": "keydown", "key": "F5"},
            {"t": "navigate", "url": "https://x"}, {"t": "keydown", "key": "a",
                                                    "text": "abcdefgh"},
            "not an object",
        ]
        for ev in refused:
            with self.assertRaises(ls.RelayRefused, msg=repr(ev)):
                ls.sanitize_event(ev, vp)
        _, params = ls.sanitize_event({"t": "keydown", "key": "A", "mods": 8, "text": "A"}, vp)
        self.assertEqual(params["modifiers"], 8)
        _, params = ls.sanitize_event({"t": "wheel", "x": 1, "y": 1, "dy": 120}, vp)
        self.assertEqual((params["type"], params["deltaY"]), ("mouseWheel", 120.0))


class ViewTests(Base):
    def start(self, clock=None, ttl_s=60):
        self.br = FakeBrowser()
        self.view = ls.LiveView(ls.PinnedPage("T1", self.br.send), ttl_s=ttl_s,
                                clock=clock or Clock(), label=HOST)
        url = self.view.start()
        self.addCleanup(self.view.close)
        self.fixture.allow(url)
        return url

    def test_refuses_non_loopback(self):
        for bind in ("0.0.0.0", "192.168.1.5", "localhost", "::", ""):
            with self.assertRaises(ValueError):
                ls.LiveView(ls.PinnedPage("T1", FakeBrowser().send), bind=bind)
        self.assertFalse(ls._loopback_client("10.0.0.5"))
        self.assertFalse(ls._loopback_client("192.168.64.1"))
        self.assertTrue(ls._loopback_client("127.0.0.1"))
        self.assertTrue(ls._loopback_client("::ffff:127.0.0.1"))

    def test_binding_makes_no_reverse_dns_lookup(self):
        # http.server's own server_bind runs socket.getfqdn, which raises macOS's Local Network prompt
        with mock.patch("socket.getfqdn", side_effect=AssertionError("reverse DNS lookup")):
            url = self.start()
        self.assertEqual(urllib.parse.urlsplit(url).hostname, "127.0.0.1")

    def test_url_is_loopback_random_port_and_token(self):
        url = self.start()
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.hostname, "127.0.0.1")
        self.assertNotIn(parts.port, (80, 443, 8080, 9222))
        self.assertRegex(parts.path, r"^/v/[A-Za-z0-9_-]{40,}/$")

    def test_token_required_and_one_time(self):
        url = self.start()
        port, path = urllib.parse.urlsplit(url).port, urllib.parse.urlsplit(url).path
        host = {"Host": f"127.0.0.1:{port}"}
        self.assertEqual(http_call(port, "GET", "/", host)[0], 404)
        self.assertEqual(http_call(port, "GET", "/v/wrongtoken/", host)[0], 404)
        self.assertEqual(http_call(port, "GET", "/v/wrongtoken/frame", host)[0], 404)
        self.assertEqual(http_call(port, "GET", path + "frame", host)[0], 403)  # not claimed
        viewer = Viewer(url)
        status, headers, _ = viewer.open()
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("SameSite=Strict", headers["set-cookie"])
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        # a second opener with the same token is refused: the token is one-time
        self.assertEqual(Viewer(url).open()[0], 403)
        self.assertEqual(viewer.open()[0], 200)                    # the claimant may reload
        self.assertEqual(viewer.frame()[0], 200)

    def test_dns_rebinding_and_cross_origin_input_refused(self):
        url = self.start()
        viewer = Viewer(url)
        viewer.open()
        port, path = viewer.port, viewer.path
        self.assertEqual(http_call(port, "GET", path, {"Host": "evil.example"})[0], 403)
        self.assertEqual(http_call(port, "GET", path + "frame",
                                   {"Host": f"localhost:{port}",
                                    "Cookie": viewer.cookie})[0], 403)
        self.assertEqual(viewer.send([{"t": "down", "x": 1, "y": 1}],
                                     origin="http://evil.example")[0], 403)
        status, _, _ = http_call(port, "POST", path + "input",
                                 {"Host": f"127.0.0.1:{port}", "Cookie": viewer.cookie,
                                  "Content-Type": "application/json"}, b"[]")
        self.assertEqual(status, 403)                          # no Origin header
        self.assertEqual(http_call(port, "PUT", path, {"Host": f"127.0.0.1:{port}"})[0], 405)
        self.assertEqual(self.br.sent, [])

    def test_relay_reaches_only_the_pinned_target(self):
        url = self.start()
        viewer = Viewer(url)
        viewer.open()
        self.assertEqual(viewer.click()[0], 204)
        self.assertEqual(viewer.send([{"t": "keydown", "key": "l", "mods": 2}])[0], 400)
        self.assertEqual(viewer.send([{"t": "navigate", "url": "https://x.example"}])[0], 400)
        sent_methods = {m for _, m, _ in self.br.sent}
        self.assertEqual({t for t, _, _ in self.br.sent}, {"T1"})
        self.assertLessEqual(sent_methods, ls.RELAY_METHODS)
        self.assertEqual(self.br.clicks, 1)

    def test_expires_and_closes(self):
        clock = Clock()
        url = self.start(clock=clock, ttl_s=120)
        viewer = Viewer(url)
        viewer.open()
        clock.t += 121
        self.assertEqual(viewer.frame()[0], 410)
        self.assertEqual(self.view.closed_reason, "expired")
        self.view.close("cleared")
        with self.assertRaises(OSError):
            viewer.frame()


# ---------------------------------------------------------------------------
# the solve: headless view and attached browser
# ---------------------------------------------------------------------------

class SolveTests(Base):
    def test_headless_person_clears_it_through_the_view(self):
        br, box = FakeBrowser(), {}
        spy = mock.Mock(return_value="model answer")
        hook = self.quarantine.guard(spy)
        solver = self.solver(capture_ask(box), sleep=human_over_view(self, box, br, hook))
        res = solver.solve(ls.SolveRequest("task-A", HOST, hi.ORIGIN_USER_TASK, step="fetch"),
                           FakePage(br))
        self.assertEqual(res["status"], "cleared")
        spy.assert_not_called()                     # no model on the challenge page
        self.assertIn(box["url"], box["message"])
        self.assertEqual({t for t, _, _ in br.sent}, {"T1"})
        self.assertLessEqual({m for _, m, _ in br.sent}, ls.RELAY_METHODS | ls.VIEW_METHODS)
        rec = res["receipt"]
        self.assertEqual((rec["type"], rec["task"], rec["origin"], rec["mode"], rec["host"]),
                         ("live_solve", "task-A", "user_task", "headless", HOST))
        self.assertEqual(rec["inputs_relayed"], 4)            # two clicks: down + up each
        self.assertEqual(rec["models_on_challenge"], "none")
        self.assertIn("os_user", rec["who"])
        self.assertLessEqual(rec["asked_at"], rec["ended_at"])
        blob = json.dumps(rec)
        token = urllib.parse.urlsplit(box["url"]).path.split("/")[2]
        self.assertNotIn(token, blob)
        self.assertNotIn("JPEG", blob)
        self.assertNotIn("127.0.0.1:", blob)
        # the view closed with the solve
        with self.assertRaises(OSError):
            box["viewer"].frame()
        # after it cleared, the page is no longer quarantined
        self.assertEqual(hook(target_id="T1", observation=br.observe("T1")), "model answer")
        self.assertTrue(self.sessions.valid("task-A", HOST, hi.ORIGIN_USER_TASK))
        self.assertFalse(self.sessions.valid("task-B", HOST, hi.ORIGIN_USER_TASK))

    def test_attached_browser_is_only_observed(self):
        br, box = FakeBrowser(), {}

        def person_solves_in_own_browser(_s):
            br.clicks += 1                                 # outside AnyUse
        solver = self.solver(capture_ask(box), sleep=person_solves_in_own_browser)
        res = solver.solve(ls.SolveRequest("task-A", HOST, hi.ORIGIN_USER_TASK),
                           FakePage(br, mode=ls.MODE_ATTACH))
        self.assertEqual(res["status"], "cleared")
        self.assertIsNone(box["url"])
        self.assertIn("your own browser", box["message"])
        self.assertEqual(br.sent, [])                       # nothing sent to the page
        self.assertEqual(res["receipt"]["mode"], "attach")
        self.assertEqual(res["receipt"]["inputs_relayed"], 0)

    def test_times_out_and_leaves_the_page_quarantined(self):
        br, box = FakeBrowser(), {}
        solver = self.solver(capture_ask(box), timeout_s=30)
        res = solver.solve(ls.SolveRequest("task-A", HOST, hi.ORIGIN_USER_TASK), FakePage(br))
        self.assertEqual(res["status"], "expired")
        self.assertTrue(self.quarantine.active("T1"))
        self.assertFalse(self.sessions.valid("task-A", HOST, hi.ORIGIN_USER_TASK))
        self.assertEqual([m for _, m, _ in br.sent], [])

    def test_navigating_away_stops_the_view(self):
        br, box = FakeBrowser(), {}

        def leaves(_s):
            br.observe = lambda target="T1": {"url": "https://elsewhere.example/",
                                              "text": "x" * 100}
        res = self.solver(capture_ask(box), sleep=leaves).solve(
            ls.SolveRequest("task-A", HOST, hi.ORIGIN_USER_TASK), FakePage(br))
        self.assertEqual(res["status"], "navigated_away")
        self.assertFalse(self.sessions.valid("task-A", HOST, hi.ORIGIN_USER_TASK))

    def test_no_channel_and_not_a_challenge(self):
        br = FakeBrowser()
        res = self.solver(lambda *a: None).solve(
            ls.SolveRequest("t", HOST, hi.ORIGIN_USER_TASK), FakePage(br))
        self.assertEqual(res["status"], "no_channel")
        br.clicks = 5
        res = self.solver(capture_ask({})).solve(
            ls.SolveRequest("t", HOST, hi.ORIGIN_USER_TASK), FakePage(br))
        self.assertEqual(res["status"], "not_challenge")

    def test_learn_agent_never_calls_the_model_on_a_challenge(self):
        from anyuse.execution.agent_browser import StepAgent

        class Session:
            def __init__(self, endpoint, allow_url=None):
                self.refused = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def goto(self, url):
                pass

            async def observe(self):
                return FakeBrowser().observe("T1")

            async def settle(self, s):
                pass

            async def dom(self):
                return ""

            async def text(self):
                return ""

            async def url(self):
                return f"https://{HOST}/"

            async def network(self):
                return []

            async def consent_controls(self):
                return []

        brain = mock.Mock(side_effect=AssertionError("a model saw a challenge"))
        agent = StepAgent(brain=brain, session_factory=Session,
                          endpoint="http://192.168.64.5:9222")
        run = agent.run("search for {q}", f"https://{HOST}/", "serde")
        brain.assert_not_called()
        self.assertEqual(run.stop, "blocked: challenge")


# ---------------------------------------------------------------------------
# the engine: THIS task continues; sessions are per task
# ---------------------------------------------------------------------------

def chal_manifest():
    m = base(name="chal")
    m["steps"] = [
        proved_step("fetch", inputs={"q": {"type": "string"}},
                    outputs={"title": {"type": "string"}},
                    bindings={"q": {"kind": "input", "name": "q"}}),
        proved_step("detail", inputs={"title": {"type": "string"}},
                    outputs={"body": {"type": "string"}},
                    bindings={"title": {"kind": "derived", "step": "fetch",
                                        "output": "title"}}),
    ]
    m["outputs"] = {"body": {"type": "string", "from": {"step": "detail", "output": "body"}}}
    return m


class LiveRunner:
    """A browser runner that holds one page per task (T1) and can continue in it."""

    supports_live_solve = True
    supports_attached_session = False

    def __init__(self, browser, mode=ls.MODE_HEADLESS):
        self.browser, self.mode = browser, mode
        self.calls = []

    def fresh_check(self, step, bound):
        return None

    def challenge_page(self, ctx):
        return FakePage(self.browser, mode=self.mode)

    def execute(self, step, bound, ctx):
        self.calls.append((step["id"], dict(ctx)))
        if not self.browser.cleared:
            return outcome(False, error="blocked", blocked=TURNSTILE)
        if step["id"] == "fetch":
            return outcome(True, {"title": f"T:{bound['q']}"})
        return outcome(True, {"body": f"B:{bound['title']}"})


class EngineTests(Base):
    def engine(self, runner, solver, origin):
        return Engine(chal_manifest(), {"compiled_tool": runner},
                      ReceiptStore(os.path.join(self.tmp, "runs")), inbox=self.inbox,
                      human_channel="cli", origin=origin, live_solver=solver)

    def chain(self, run_id):
        return ReceiptStore(os.path.join(self.tmp, "runs")).load(run_id)

    def test_user_task_continues_after_the_person_solves(self):
        br, box = FakeBrowser(), {}
        runner = LiveRunner(br)
        advisor = mock.Mock(side_effect=AssertionError("no model"))
        solver = self.solver(capture_ask(box), sleep=human_over_view(self, box, br))
        eng = self.engine(runner, solver, hi.ORIGIN_USER_TASK)
        eng.advisor = advisor
        res = eng.run({"q": "serde"})
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["outputs"], {"body": "B:T:serde"})
        advisor.assert_not_called()
        chain = self.chain(res["run_id"])
        types = [r["type"] for r in chain]
        self.assertEqual(types.count("live_solve"), 1)
        live = next(r for r in chain if r["type"] == "live_solve")
        self.assertEqual((live["task"], live["step"], live["outcome"], live["origin"]),
                         (res["run_id"], "fetch", "cleared", "user_task"))
        clear = next(r for r in chain if r["type"] == "clearance")
        self.assertEqual(clear["channel"], "live-solve")
        self.assertNotIn("host_policy", types)            # the host is not human-only
        self.assertEqual(ReceiptStore(os.path.join(self.tmp, "runs")).meta(
            res["run_id"])["origin"], "user_task")
        (s1, c1), (s2, c2), (s3, c3) = runner.calls
        self.assertEqual((s1, s2, s3), ("fetch", "fetch", "detail"))
        self.assertEqual(c2["session"], "live_solved")
        self.assertTrue(c3.get("live_session"))           # same host, same task
        # the task is over: the session is gone and the headless browser forgot it
        self.assertFalse(self.sessions.valid(res["run_id"], HOST, hi.ORIGIN_USER_TASK))
        self.assertEqual(br.sent[-2:][0][1], "Storage.clearDataForOrigin")
        self.assertEqual({t for t, _, _ in br.sent}, {"T1"})
        self.assertFalse(br.cleared)
        # the audit card is closed and holds no link
        cards = self.inbox.cards(None)
        self.assertEqual([(c["how"], c["status"]) for c in cards], [("solve_live", "done")])
        self.assertNotIn("127.0.0.1", json.dumps(cards))

    def test_scheduled_falls_back_unless_the_setting_allows_it(self):
        br, box = FakeBrowser(), {}
        eng = self.engine(LiveRunner(br), self.solver(capture_ask(box)),
                          hi.ORIGIN_SCHEDULED)
        res = eng.run({"q": "serde"})
        self.assertEqual(res["status"], "handed_off")
        self.assertNotIn("asked", box)
        card = self.inbox.get(res["card"]["id"])
        self.assertEqual((card["kind"], card["how"]), ("challenge", "human_does_step"))
        self.assertNotIn("live_solve", [r["type"] for r in self.chain(res["run_id"])])

    def test_scheduled_run_waits_on_an_inbox_card_when_allowed(self):
        br = FakeBrowser()
        notices = []
        state = {"n": 0}

        def person(_s):
            state["n"] += 1
            if state["n"] == 1:                          # the person runs `inbox live`
                tty = Tty()
                card_id = notices[0]["id"]
                out = hi.reveal_live(self.inbox, card_id, gate=gate(self.tmp, tty))
                self.assertEqual(out["status"], "shown")
                self.assertNotIn("127.0.0.1", json.dumps(out))
                url = re.findall(r"open: (\S+)", tty.text)[0]
                self.fixture.allow(url)
                self.viewer = Viewer(url)
                self.assertEqual(self.viewer.open()[0], 200)
            else:
                self.viewer.click()
        solver = self.solver(ls.inbox_ask(self.inbox, notify=notices.append),
                             origins="user_task,scheduled", sleep=person)
        res = self.engine(LiveRunner(br), solver, hi.ORIGIN_SCHEDULED).run({"q": "serde"})
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["how"], "solve_live")
        self.assertNotIn("127.0.0.1", json.dumps(notices))
        from anyuse.execution import notify
        self.assertIn("anyuse inbox live", notify.message_for(notices[0]))
        card = self.inbox.get(notices[0]["id"])
        self.assertEqual(card["status"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.inbox.base_dir, "values",
                                                     f"{card['id']}.json")))
        live = next(r for r in self.chain(res["run_id"]) if r["type"] == "live_solve")
        self.assertEqual((live["origin"], live["asked_via"]), ("scheduled", "inbox"))

    def test_live_card_url_is_encrypted_and_gated(self):
        req = ls.SolveRequest("task-A", HOST, hi.ORIGIN_SCHEDULED, step="fetch")
        url = "http://127.0.0.1:5555/v/SECRETTOKEN123/"
        card = self.inbox.post_live(req, url)
        for d, _, names in os.walk(self.inbox.base_dir):
            for n in names:
                with open(os.path.join(d, n), encoding="utf-8") as fh:
                    self.assertNotIn("SECRETTOKEN123", fh.read())
        tty = Tty()
        out = hi.reveal_live(self.inbox, card["id"],
                             gate=gate(self.tmp, tty, env={"CLAUDECODE": "1"}))
        self.assertEqual(out["status"], "refused")        # agent terminal
        self.assertNotIn("SECRETTOKEN123", tty.text)
        out = hi.do_card(self.inbox, card["id"], engine_for=None, gate=gate(self.tmp, Tty()))
        self.assertEqual(out["status"], "refused")
        self.assertIn("inbox live", out["error"])
        self.assertEqual([c["id"] for c in hi.list_pending(self.inbox)["cards"]],
                         [card["id"]])
        self.assertNotIn("SECRETTOKEN123", json.dumps(hi.list_pending(self.inbox)))

    def test_declined_through_the_inbox_falls_back(self):
        br = FakeBrowser()
        notices = []

        def person_skips(_s):
            self.inbox.set_status(notices[0]["id"], "skipped")
        solver = self.solver(ls.inbox_ask(self.inbox, notify=notices.append),
                             origins="scheduled", sleep=person_skips)
        res = self.engine(LiveRunner(br), solver, hi.ORIGIN_SCHEDULED).run({"q": "x"})
        self.assertEqual(res["status"], "handed_off")
        live = next(r for r in self.chain(res["run_id"]) if r["type"] == "live_solve")
        self.assertEqual(live["outcome"], "declined")
        self.assertEqual(self.inbox.get(res["card"]["id"])["how"], "human_does_step")

    def test_bulk_run_asks_again_for_every_task(self):
        br, box = FakeBrowser(), {}
        seen = []

        viewers = {}

        def person(_s):                                     # solves in each task's view
            task = box["req"].task_id
            if task not in viewers:
                seen.append((task, self.sessions.valid(task, HOST, hi.ORIGIN_BULK)))
                self.fixture.allow(box["url"])
                viewers[task] = Viewer(box["url"])
                self.assertEqual(viewers[task].open()[0], 200)
            else:
                self.assertEqual(viewers[task].click()[0], 204)
        solver = self.solver(capture_ask(box), origins="bulk", sleep=person)
        runner = LiveRunner(br)
        eng = self.engine(runner, solver, hi.ORIGIN_BULK)
        results = [eng.run({"q": q}) for q in ("a", "b", "c")]
        self.assertEqual([r["status"] for r in results], ["completed"] * 3)
        self.assertEqual(box["asked"], 3)                       # each task asked again
        self.assertEqual([t for t, _ in seen], [r["run_id"] for r in results])
        self.assertEqual({v for _, v in seen}, {False})         # never pre-cleared
        self.assertEqual(len({v.path for v in viewers.values()}), 3)   # a new view each
        forgets = [p for t, m, p in br.sent if m == "Storage.clearDataForOrigin"]
        self.assertEqual(len(forgets), 6)                       # https + http per task
        for r in results:
            self.assertFalse(self.sessions.valid(r["run_id"], HOST, hi.ORIGIN_BULK))

    def test_session_not_reused_across_tasks_or_origins(self):
        sessions = ls.SolvedSessions()
        forgot = []
        sessions.grant("A", HOST, hi.ORIGIN_USER_TASK, forget=forgot.append)
        self.assertTrue(sessions.valid("A", HOST, hi.ORIGIN_USER_TASK))
        self.assertTrue(sessions.valid("A", HOST.upper(), hi.ORIGIN_USER_TASK))
        self.assertFalse(sessions.valid("B", HOST, hi.ORIGIN_USER_TASK))
        self.assertFalse(sessions.valid("A", "b.example", hi.ORIGIN_USER_TASK))
        for origin in (hi.ORIGIN_SCHEDULED, hi.ORIGIN_BENCH, hi.ORIGIN_BULK, None):
            self.assertFalse(sessions.valid("A", HOST, origin))
        self.assertEqual(sessions.end_task("A"), [HOST])
        self.assertEqual(forgot, [HOST])
        self.assertFalse(sessions.valid("A", HOST, hi.ORIGIN_USER_TASK))

    def test_cancel_ends_the_task_session(self):
        br, box = FakeBrowser(), {}
        br_solves = lambda _s: setattr(br, "clicks", br.clicks_needed)  # noqa: E731
        solver = self.solver(capture_ask(box), sleep=br_solves)

        class Stops(LiveRunner):
            def execute(self, step, bound, ctx):
                if step["id"] == "detail":
                    return outcome(False, error="blocked", blocked={
                        **TURNSTILE, "reason": "login_wall"})
                return super().execute(step, bound, ctx)
        eng = self.engine(Stops(br, mode=ls.MODE_ATTACH), solver, hi.ORIGIN_USER_TASK)
        res = eng.run({"q": "x"})
        self.assertEqual(res["status"], "handed_off")
        self.assertTrue(self.sessions.valid(res["run_id"], HOST, hi.ORIGIN_USER_TASK))
        eng.cancel(res["run_id"])
        self.assertFalse(self.sessions.valid(res["run_id"], HOST, hi.ORIGIN_USER_TASK))


if __name__ == "__main__":
    unittest.main()
