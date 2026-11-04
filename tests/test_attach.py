"""Attach recorder: read-only observation, pinning, secret filtering, storage,
and compile-with-proof from attached sessions.

Everything here is offline: the "browser" is a fake WebSocket that answers the
DevTools protocol from a script, discovery is a fake ``http_get``, and the proof
replays against recorded responses. No socket is opened, no browser started.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
import urllib.parse

from anyuse.execution import attach as A
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def snapshot(url, tree, invisible_text=()):
    """Build a DOMSnapshot.captureSnapshot result from a tiny tree spec.

    tree: ("tag", {attrs}, [children]) | "text". Text nodes are laid out unless
    their string is in ``invisible_text``.
    """
    strings, index = [], {}

    def sid(x):
        if x not in index:
            index[x] = len(strings)
            strings.append(x)
        return index[x]

    parent, ntype, name, value, attrs, layout = [], [], [], [], [], []

    def add(node, p):
        i = len(parent)
        parent.append(p)
        if isinstance(node, str):
            ntype.append(3)
            name.append(sid("#text"))
            value.append(sid(node))
            attrs.append([])
            if node not in invisible_text:
                layout.append(i)
            return
        tag, a, kids = node
        ntype.append(1)
        name.append(sid(tag.upper()))
        value.append(-1)
        flat = []
        for k, v in a.items():
            flat += [sid(k), sid(v)]
        attrs.append(flat)
        layout.append(i)
        for k in kids:
            add(k, i)

    parent.append(-1)
    ntype.append(9)
    name.append(sid("#document"))
    value.append(-1)
    attrs.append([])
    add(tree, 0)
    return {"strings": strings, "documents": [{
        "documentURL": sid(url),
        "nodes": {"parentIndex": parent, "nodeType": ntype, "nodeName": name,
                  "nodeValue": value, "attributes": attrs},
        "layout": {"nodeIndex": layout}}]}


def result_page(value, rows, extra=()):
    return ("html", {}, [
        ("head", {}, [("title", {}, [f"Search: {value}"]),
                      ("script", {}, ["var token='abc'"])]),
        ("body", {}, [
            ("nav", {}, [("a", {}, ["Home"]), ("a", {}, ["About us"])]),
            ("form", {}, [("input", {"type": "text", "value": value, "name": "q"}, [])]),
            ("ul", {"class": "results"},
             [("li", {"class": "row"}, [("a", {}, [r])]) for r in rows]),
            *extra,
        ])])


def api_body(rows):
    return json.dumps({"hits": [{"name": r} for r in rows]})


def session_events(value, rows, api_headers=None, extra_info=None):
    """The protocol events of an agent searching ``value`` on ex.test."""
    url = f"https://ex.test/search?q={value}"
    api = f"https://ex.test/api/search?q={value}"
    ev = [
        {"method": "Network.requestWillBeSent", "params": {
            "requestId": "d1", "type": "Document", "frameId": "F",
            "request": {"url": url, "method": "GET", "headers": {}}}},
        {"method": "Network.responseReceived", "params": {
            "requestId": "d1", "type": "Document",
            "response": {"status": 200, "mimeType": "text/html",
                         "headers": {"Content-Type": "text/html",
                                     "Set-Cookie": "sid=SECRETCOOKIE"}}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "d1",
                                                         "encodedDataLength": 900}},
        {"method": "Page.frameNavigated", "params": {"frame": {"id": "F", "url": url}}},
        {"method": "Network.requestWillBeSent", "params": {
            "requestId": "x1", "type": "XHR", "frameId": "F",
            "request": {"url": api, "method": "GET", "headers": api_headers or {}}}},
        {"method": "Network.responseReceived", "params": {
            "requestId": "x1", "type": "XHR",
            "response": {"status": 200, "mimeType": "application/json", "headers": {}}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "x1",
                                                         "encodedDataLength": 200}},
        {"method": "Page.loadEventFired", "params": {}},
    ]
    if extra_info:
        ev.insert(5, {"method": "Network.requestWillBeSentExtraInfo",
                      "params": {"requestId": "x1", **extra_info}})
    return ev


class FakeWS:
    """A page target that answers only what the recorder may ask."""

    def __init__(self, events, bodies, snap):
        self.events = events
        self.bodies = bodies
        self.snap = snap
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg["method"])
        result = {}
        if msg["method"] == "Network.getResponseBody":
            body = self.bodies.get(msg["params"]["requestId"])
            if body is None:
                await self.inbox.put(json.dumps({"id": msg["id"], "error": {"message": "no"}}))
                return
            result = {"body": body, "base64Encoded": False}
        elif msg["method"] == "DOMSnapshot.captureSnapshot":
            result = self.snap
        await self.inbox.put(json.dumps({"id": msg["id"], "result": result}))
        if msg["method"] == "Page.enable":
            for e in self.events:
                await self.inbox.put(json.dumps(e))

    async def recv(self):
        if self.closed:
            raise ConnectionError("closed")
        return await self.inbox.get()

    async def close(self):
        self.closed = True


def pin():
    return A.Pin(endpoint=A.Endpoint("127.0.0.1", 9333), target_id="ABCDEF0123456789",
                 ws_url="ws://127.0.0.1:9333/devtools/page/ABCDEF0123456789",
                 browser="HeadlessChrome/151", browser_id="b-1")


def record(value, rows, *, api_headers=None, extra_info=None, snap=None,
           bodies=None, input_value=True, task="ex_search"):
    url = f"https://ex.test/search?q={value}"
    ws = FakeWS(session_events(value, rows, api_headers, extra_info),
                bodies if bodies is not None else {
                    "d1": "<html><body><ul class='results'>" + "".join(
                        f"<li class='row'><a>{r}</a></li>" for r in rows) + "</ul></body></html>",
                    "x1": api_body(rows)},
                snap or snapshot(url, result_page(value, rows)))
    rec = A.Recorder(pin=pin(), task=task,
                     input_param={"name": "q", "value": value} if input_value else None)
    asyncio.run(A.observe(ws, rec, max_seconds=0.4, delay_scale=0.01))
    return rec, ws


class RecordedTransport:
    def __init__(self, recordings):
        self.by = {}
        for rec in recordings:
            for r in rec["network"]:
                self.by[r["url"]] = r
        self.calls = 0

    def send(self, method, url, headers, body, timeout_s):
        self.calls += 1
        r = self.by.get(url)
        if r is None:
            return ApiResponse(404, {"Content-Type": "text/plain"}, "not recorded")
        return ApiResponse(200, {"Content-Type": r["mime"]}, r["body"] or "")


ROWS = {
    "serde": ["serde v1.0", "serde_json v1.0", "serde_derive v1.0"],
    "regex": ["regex v1.10", "regex-lite v0.1", "regex-automata v0.4"],
    "tokio": ["tokio v1.40", "tokio-util v0.7", "tokio-stream v0.1"],
}
ROWS4 = dict(ROWS, rand=["rand v0.8", "rand_core v0.6", "rand_chacha v0.3"])


# ---------------------------------------------------------------------------
# endpoint validation and pinning
# ---------------------------------------------------------------------------

class EndpointTests(unittest.TestCase):
    def test_accepts_only_literal_loopback_with_port(self):
        self.assertEqual(A.validate_endpoint("http://127.0.0.1:9222").port, 9222)
        self.assertEqual(A.validate_endpoint("http://127.0.0.1:9222/").netloc,
                         "127.0.0.1:9222")
        self.assertEqual(A.validate_endpoint("http://[::1]:9333").netloc, "[::1]:9333")

    def test_refuses_everything_else(self):
        for bad in ["http://localhost:9222", "http://127.0.0.1", "http://127.0.0.1:",
                    "https://127.0.0.1:9222", "ws://127.0.0.1:9222",
                    "http://user:pw@127.0.0.1:9222", "http://127.0.0.1:9222@evil.test:1",
                    "http://127.1:9222", "http://2130706433:9222", "http://127.0.0.2:9222",
                    "http://0.0.0.0:9222", "http://192.168.64.3:9222",
                    "http://127.0.0.1:9222/json", "http://127.0.0.1:9222?x=1",
                    "http://127.0.0.1:9222#f", " http://127.0.0.1:9222",
                    "http://127.0.0.1:99999", "http://127.0.0.1:0", "http://[::ffff:127.0.0.1]:9222",
                    "", None]:
            with self.subTest(bad=bad):
                with self.assertRaises(A.AttachError):
                    A.validate_endpoint(bad)

    def test_ws_url_is_built_not_followed(self):
        ep = A.validate_endpoint("http://127.0.0.1:9333")
        self.assertEqual(A.page_ws_url(ep, "ABCDEF0123456789"),
                         "ws://127.0.0.1:9333/devtools/page/ABCDEF0123456789")
        with self.assertRaises(A.AttachError):
            A.page_ws_url(ep, "ABCDEF0123456789",
                          reported="ws://evil.test:9333/devtools/page/ABCDEF0123456789")
        with self.assertRaises(A.AttachError):
            A.page_ws_url(ep, "../browser/xyz")

    def _http(self, pages, bws="ws://127.0.0.1:9333/devtools/browser/guid-1"):
        def get(url):
            if url.endswith("/json/version"):
                return {"Browser": "HeadlessChrome/151", "webSocketDebuggerUrl": bws,
                        "Protocol-Version": "1.3"}
            if url.endswith("/json/list"):
                return pages
            raise AssertionError(url)
        return get

    def _page(self, tid, url="https://ex.test/"):
        return {"id": tid, "type": "page", "url": url,
                "webSocketDebuggerUrl": f"ws://127.0.0.1:9333/devtools/page/{tid}"}

    def test_single_page_is_pinned(self):
        ep = A.validate_endpoint("http://127.0.0.1:9333")
        p = A.pin_target(ep, http_get=self._http([self._page("AAAAAAAA1111"),
                                                  {"id": "w1", "type": "service_worker"}]))
        self.assertEqual(p.target_id, "AAAAAAAA1111")
        self.assertEqual(p.browser_id, "guid-1")
        self.assertEqual(p.target_origin, "https://ex.test")

    def test_several_pages_need_an_explicit_target(self):
        ep = A.validate_endpoint("http://127.0.0.1:9333")
        http = self._http([self._page("AAAAAAAA1111"), self._page("BBBBBBBB2222")])
        with self.assertRaises(A.AttachError) as cm:
            A.pin_target(ep, http_get=http)
        self.assertIn("--target", str(cm.exception))
        self.assertEqual(A.pin_target(ep, "BBBBBBBB2222", http_get=http).target_id,
                         "BBBBBBBB2222")
        with self.assertRaises(A.AttachError):
            A.pin_target(ep, "CCCCCCCC3333", http_get=http)      # no fallback

    def test_browser_on_another_host_is_refused(self):
        ep = A.validate_endpoint("http://127.0.0.1:9333")
        with self.assertRaises(A.AttachError):
            A.pin_target(ep, http_get=self._http(
                [self._page("AAAAAAAA1111")], bws="ws://10.0.0.5:9222/devtools/browser/x"))
        bad = self._page("AAAAAAAA1111")
        bad["webSocketDebuggerUrl"] = "ws://127.0.0.1:1/devtools/page/AAAAAAAA1111"
        with self.assertRaises(A.AttachError):
            A.pin_target(ep, http_get=self._http([bad]))


# ---------------------------------------------------------------------------
# read-only
# ---------------------------------------------------------------------------

class ReadOnlyTests(unittest.TestCase):
    def test_observer_refuses_actuating_methods_before_sending(self):
        async def go():
            ws = FakeWS([], {}, {})
            obs = A.CdpObserver(ws)
            obs.start()
            for m in ["Runtime.evaluate", "Input.dispatchKeyEvent", "Page.navigate",
                      "Fetch.enable", "Emulation.setDeviceMetricsOverride",
                      "Target.createTarget", "Target.activateTarget",
                      "Network.setUserAgentOverride", "Network.setCookie",
                      "Page.reload", "DOM.setAttributeValue"]:
                with self.assertRaises(A.ReadOnlyViolation):
                    await obs.call(m)
            await obs.close()
            return ws.sent
        self.assertEqual(asyncio.run(go()), [])

    def test_a_whole_session_sends_only_read_only_methods(self):
        _rec, ws = record("serde", ROWS["serde"])
        self.assertTrue(ws.sent)
        self.assertTrue(set(ws.sent) <= A.READ_ONLY_METHODS, ws.sent)
        self.assertIn("DOMSnapshot.captureSnapshot", ws.sent)

    def test_connection_loss_ends_the_session_without_reconnect(self):
        async def go():
            ws = FakeWS([], {}, snapshot("https://ex.test/", ("html", {}, [])))
            rec = A.Recorder(pin=pin())
            task = asyncio.ensure_future(A.observe(ws, rec, max_seconds=5))
            await asyncio.sleep(0.05)
            ws.closed = True
            await ws.inbox.put("{}")          # wake the reader; it sees the close
            await task
            return rec
        rec = asyncio.run(go())
        self.assertIn("closed", rec.stop_reason)

    def test_detach_event_stops(self):
        rec = A.Recorder(pin=pin())
        self.assertEqual(rec.on_event({"method": "Inspector.detached", "params": {}})[0][0],
                         "stop")
        # another target going away is not ours
        self.assertEqual(rec.on_event({"method": "Target.targetDestroyed",
                                       "params": {"targetId": "OTHER0000000"}}), [])


# ---------------------------------------------------------------------------
# secrets never reach the file
# ---------------------------------------------------------------------------

SECRETS = ["SECRETAUTH", "SECRETCOOKIE", "SECRETQUERY", "SECRETPW", "SECRETKEY",
           "SECRETCSRF", "SECRETBEARER0123456789", "SECRETMETA",
           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"]


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _dirty_session(self):
        rec = A.Recorder(pin=pin(), task="t")
        ev = [
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": "a", "type": "XHR",
                "request": {"url": "https://ex.test/api?q=x", "method": "GET",
                            "headers": {"Authorization": "Bearer SECRETAUTH"}}}},
            {"method": "Network.requestWillBeSentExtraInfo", "params": {
                "requestId": "b", "headers": {"Cookie": "sessionid=SECRETCOOKIE; _ga=1"},
                "associatedCookies": [{"blockedReasons": [],
                                       "cookie": {"name": "sessionid",
                                                  "value": "SECRETCOOKIE"}}]}},
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": "b", "type": "XHR",
                "request": {"url": "https://ex.test/me", "method": "GET", "headers": {}}}},
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": "c", "type": "XHR",
                "request": {"url": "https://ex.test/api?q=y&access_token=SECRETQUERY",
                            "method": "GET", "headers": {}}}},
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": "d", "type": "XHR",
                "request": {"url": "https://ex.test/login", "method": "POST",
                            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                            "postData": "user=me&password=SECRETPW"}}},
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": "e", "type": "Fetch",
                "request": {"url": "https://ex.test/api/list?q=z", "method": "GET",
                            "headers": {}}}},
            {"method": "Network.responseReceived", "params": {
                "requestId": "e", "type": "Fetch",
                "response": {"status": 200, "mimeType": "application/json",
                             "headers": {"set-cookie": "sid=SECRETCOOKIE"}}}},
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": "f", "type": "Document",
                "request": {"url": "https://ex.test/page", "method": "GET",
                            "headers": {}}}},
            {"method": "Network.responseReceived", "params": {
                "requestId": "f", "type": "Document",
                "response": {"status": 200, "mimeType": "text/html", "headers": {}}}},
        ]
        for e in ev:
            rec.on_event(e)
        self.assertEqual(rec.on_event({"method": "Network.loadingFinished",
                                       "params": {"requestId": "e"}})[0], ("body", "e"))
        rec.on_body("e", {"body": json.dumps({
            "items": ["one", "two"], "api_key": "SECRETKEY",
            "jwt": SECRETS[-1], "note": "Bearer SECRETBEARER0123456789"})})
        rec.on_body("f", {"body": (
            '<html><head><meta name="csrf-token" content="SECRETMETA"></head><body>'
            '<form><input type="hidden" name="authenticity_token" value="SECRETCSRF">'
            '<input type="password" name="pw" value="SECRETPW"></form></body></html>')})
        rec.requests["f"].mime = "text/html"
        rec.on_body("f", {"body": (
            '<html><head><meta name="csrf-token" content="SECRETMETA"></head><body>'
            '<form><input type="hidden" name="authenticity_token" value="SECRETCSRF">'
            '<input type="password" name="pw" value="SECRETPW"></form></body></html>')})
        rec.on_snapshot(snapshot("https://ex.test/page?token=SECRETQUERY", (
            "html", {}, [("body", {}, [
                ("p", {}, ["hello Bearer SECRETBEARER0123456789"]),
                ("input", {"type": "password", "value": "SECRETPW"}, []),
                ("textarea", {}, ["SECRETPW typed"]),
                ("div", {"contenteditable": "true"}, ["SECRETPW draft"]),
                ("p", {}, ["hidden SECRETKEY"]),
            ])]), invisible_text=("hidden SECRETKEY",)))
        return rec

    def test_no_secret_is_written_and_file_is_private(self):
        rec = self._dirty_session()
        sid = A.new_session_id()
        path = A.write_session(rec.to_session(sid), self.tmp.name)
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        for s in SECRETS:
            self.assertNotIn(s, raw, s)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        data = json.loads(raw)
        by_url = {r["url"]: r for r in data["network"]}
        self.assertIn("authorization", by_url["https://ex.test/api?q=x"]["credential_bound"].lower())
        self.assertIn("sessionid", by_url["https://ex.test/me"]["credential_bound"])
        self.assertTrue(by_url["https://ex.test/login"]["credential_bound"])
        self.assertIsNone(by_url["https://ex.test/login"]["post_data"])
        self.assertTrue(data["password_field_seen"])
        self.assertIn("one", by_url["https://ex.test/api/list?q=z"]["body"])
        for r in data["network"]:
            self.assertEqual(set(r["headers"]) - {"content-type"}, set())

    def test_post_after_password_field_is_dropped(self):
        post, why = A.sanitize_post("q=hello", after_password_field=True)
        self.assertIsNone(post)
        self.assertTrue(why)
        self.assertEqual(A.sanitize_post("q=hello", False), ("q=hello", ""))
        self.assertIsNone(A.sanitize_post('{"user":{"password":"x"}}', False)[0])

    def test_consent_cookies_are_not_a_session(self):
        self.assertEqual(A.cookie_reason(["_ga", "consent", "theme"]), "")
        for name in ["PHPSESSID", "sid", "auth_token", "__Host-x", "remember_me"]:
            self.assertTrue(A.cookie_reason([name]), name)

    def test_signed_in_page_text_is_withheld(self):
        rec = A.Recorder(pin=pin())
        rec.on_event({"method": "Network.requestWillBeSentExtraInfo", "params": {
            "requestId": "d", "headers": {"cookie": "PHPSESSID=abc"}}})
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "d", "type": "Document",
            "request": {"url": "https://ex.test/account", "method": "GET", "headers": {}}}})
        rec.on_snapshot(snapshot("https://ex.test/account",
                                 ("html", {}, [("p", {}, ["Private Name"])])))
        snap = rec.snapshots[-1]
        self.assertEqual(snap["text"], "")
        self.assertEqual(snap["dom"], "")
        self.assertIn("signed-in", snap["withheld"])

    def _anon_visit(self, anonymous, set_first=True, password_first=False):
        """First document: the site answers with Set-Cookie WR_SID; the result
        document then carries it (as on mathworld.wolfram.com)."""
        rec = A.Recorder(pin=pin(), anonymous_browser=anonymous)
        if password_first:
            rec.on_snapshot(snapshot("https://ex.test/login",
                                     ("html", {}, [("input", {"type": "password"}, [])])))
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "d1", "type": "Document",
            "request": {"url": "https://ex.test/", "method": "GET", "headers": {}}}})
        if set_first:
            rec.on_event({"method": "Network.responseReceivedExtraInfo", "params": {
                "requestId": "d1",
                "headers": {"Set-Cookie": "WR_SID=abc; Path=/\ntheme=dark; Path=/"}}})
        rec.on_event({"method": "Network.requestWillBeSentExtraInfo", "params": {
            "requestId": "d2", "headers": {"cookie": "WR_SID=abc; theme=dark"}}})
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "d2", "type": "Document",
            "request": {"url": "https://ex.test/search?q=x", "method": "GET",
                        "headers": {}}}})
        rec.on_snapshot(snapshot("https://ex.test/search?q=x",
                                 ("html", {}, [("p", {}, ["Result x"])])))
        return rec.snapshots[-1]

    def test_a_site_set_cookie_is_a_sign_in_by_default(self):
        self.assertIn("signed-in", self._anon_visit(False).get("withheld", ""))

    def test_anonymous_browser_keeps_pages_with_cookies_the_site_set_itself(self):
        snap = self._anon_visit(True)
        self.assertNotIn("withheld", snap)
        self.assertIn("Result x", snap["text"])

    def test_anonymous_browser_still_withholds_a_cookie_it_did_not_see_set(self):
        self.assertIn("signed-in", self._anon_visit(True, set_first=False)
                      .get("withheld", ""))

    def test_anonymous_browser_still_withholds_after_a_password_field(self):
        self.assertIn("withheld", self._anon_visit(True, password_first=True))

    def _csrf_xhr(self, anonymous, set_cookie=True):
        rec = A.Recorder(pin=pin(), anonymous_browser=anonymous)
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "d1", "type": "Document",
            "request": {"url": "https://ex.test/", "method": "GET", "headers": {}}}})
        if set_cookie:
            rec.on_event({"method": "Network.responseReceivedExtraInfo", "params": {
                "requestId": "d1", "headers": {"set-cookie": "csrftoken=t1; Path=/"}}})
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "x1", "type": "XHR",
            "request": {"url": "https://ex.test/q?search=x", "method": "GET",
                        "headers": {"X-CSRFToken": "t1"}}}})
        return rec.requests["x1"].credential_bound

    def test_a_csrf_echo_header_is_anonymous_only_with_a_site_set_csrf_cookie(self):
        self.assertTrue(self._csrf_xhr(False))
        self.assertTrue(self._csrf_xhr(True, set_cookie=False))
        self.assertEqual(self._csrf_xhr(True), "")
        self.assertTrue(A.header_reason({"Authorization": "Bearer abcdefghijklmnop"},
                                        csrf_from_site=True))

    def test_refused_statics_never_push_the_search_document_out(self):
        rec = A.Recorder(pin=pin())
        for i in range(A.MAX_ENTRIES + 50):
            rid = f"img{i}"
            rec.on_event({"method": "Network.requestWillBeSent", "params": {
                "requestId": rid, "type": "Image",
                "request": {"url": f"https://cdn.test/logo.png?{i}", "method": "GET"}}})
            if i % 2 == 0:
                rec.on_event({"method": "Network.loadingFailed", "params": {
                    "requestId": rid, "errorText": "net::ERR_BLOCKED_BY_CLIENT"}})
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "doc", "type": "Document",
            "request": {"url": "https://ex.test/search/?v=x", "method": "GET"}}})
        self.assertIn("doc", rec.requests)
        self.assertLessEqual(len(rec.order), A.MAX_ENTRIES)
        self.assertGreater(rec.blocked_statics, A.MAX_ENTRIES // 2 - 1)
        full = A.Recorder(pin=pin())
        for i in range(A.MAX_ENTRIES + 5):
            full.on_event({"method": "Network.requestWillBeSent", "params": {
                "requestId": f"i{i}", "type": "Image",
                "request": {"url": f"https://ex.test/i{i}.png", "method": "GET"}}})
        full.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "doc", "type": "Document",
            "request": {"url": "https://ex.test/search/?v=x", "method": "GET"}}})
        self.assertIn("doc", full.requests)
        self.assertEqual(len(full.order), A.MAX_ENTRIES)

    def test_snapshot_keeps_only_visible_text_and_class_id(self):
        page = A.snapshot_to_page(snapshot("https://ex.test/x", (
            "html", {}, [("head", {}, [("title", {}, ["T"]),
                                       ("style", {}, ["body{}"])]),
                         ("body", {}, [
                             ("div", {"class": "r", "id": "i", "data-user": "u@x",
                                      "href": "https://ex.test/?k=v"}, ["Shown"]),
                             ("script", {}, ["var x = 1"]),
                             ("div", {}, ["Not laid out"])])]),
            invisible_text=("Not laid out",)))
        self.assertIn("Shown", page["text"])
        self.assertNotIn("Not laid out", page["text"] + page["dom"])
        self.assertNotIn("var x", page["dom"])
        self.assertNotIn("data-user", page["dom"])
        self.assertNotIn("href", page["dom"])
        self.assertIn('class="r"', page["dom"])
        self.assertEqual(page["title"], "T")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

class PageKeyTests(unittest.TestCase):
    """A credential-named field whose value the site's own page stated earlier is
    kept with its locator; only keyed fingerprints are held during the session."""

    PAGE = ('<html><script>window.cfg={"appId":"APP12345","searchKey":"k3y-public-123"}'
            '</script><body>shop</body></html>')

    def _events(self, rec, rid, url, rtype, mime, body=None, headers=None, extra=None):
        out = rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": rid, "type": rtype, "frameId": "F",
            "request": {"url": url, "method": "GET", "headers": headers or {}}}})
        if extra is not None:
            rec.on_event({"method": "Network.requestWillBeSentExtraInfo",
                          "params": {"requestId": rid, "headers": extra}})
        rec.on_event({"method": "Network.responseReceived", "params": {
            "requestId": rid, "type": rtype, "response": {"status": 200, "mimeType": mime}}})
        out = rec.on_event({"method": "Network.loadingFinished",
                            "params": {"requestId": rid, "encodedDataLength": 10}})
        if ("body", rid) in out and body is not None:
            rec.on_body(rid, {"body": body})
        return out

    def _session(self, provider_url, page=None, headers=None, extra=None, script=None):
        rec = A.Recorder(pin=None)
        rec.on_event({"method": "Page.frameNavigated",
                      "params": {"frame": {"id": "F", "url": "https://shop.test/"}}})
        self._events(rec, "d1", "https://shop.test/", "Document", "text/html", page or self.PAGE)
        asked = []
        if script is not None:
            asked = self._events(rec, "s1", "https://shop.test/cfg.js", "Script",
                                 "application/javascript", script)
        self._events(rec, "x1", provider_url, "XHR", "application/json",
                     '{"hits":[{"name":"red fox"}],"echo":"k3y-public-123"}',
                     headers=headers, extra=extra)
        return rec.to_session("s"), asked

    def test_a_key_the_page_stated_is_kept_by_its_locator_only(self):
        session, _ = self._session("https://search.prov.test/q?query=red"
                                   "&x-algolia-api-key=k3y-public-123")
        provider = session["network"][1]
        self.assertEqual(provider["credential_bound"], "")
        self.assertEqual([k["at"] for k in provider["page_keys"]], ["query:x-algolia-api-key"])
        self.assertIn("red fox", provider["body"])
        self.assertNotIn("k3y-public-123", json.dumps(session))

    def test_the_key_leaves_another_requests_url_too(self):
        rec = A.Recorder(pin=None)
        rec.on_event({"method": "Page.frameNavigated",
                      "params": {"frame": {"id": "F", "url": "https://shop.test/"}}})
        self._events(rec, "d1", "https://shop.test/", "Document", "text/html", self.PAGE)
        self._events(rec, "b1", "https://shop.test/b?k=k3y-public-123&e=view", "Image", "image/gif")
        self._events(rec, "x1", "https://search.prov.test/q?query=red&x-algolia-api-key=k3y-public-123",
                     "XHR", "application/json", '{"hits":[{"name":"red fox"}]}')
        session = rec.to_session("s")
        self.assertTrue(session["network"][-1]["page_keys"])
        self.assertNotIn("k3y-public-123", json.dumps(session))

    def test_a_header_key_through_extra_info(self):
        hdr = {"x-algolia-api-key": "k3y-public-123"}
        session, _ = self._session("https://search.prov.test/q?query=red", headers=hdr,
                                   extra=hdr)
        provider = session["network"][1]
        self.assertEqual(provider["credential_bound"], "")
        self.assertEqual([k["at"] for k in provider["page_keys"]], ["header:x-algolia-api-key"])
        self.assertNotIn("k3y-public-123", json.dumps(session))

    def test_a_key_in_a_site_script_is_found_and_the_script_is_not_stored(self):
        session, asked = self._session(
            "https://search.prov.test/q?query=red&x-algolia-api-key=k3y-public-123",
            page="<html><body>shop</body></html>",
            script='window.cfg={"searchKey":"k3y-public-123"};')
        self.assertIn(("body", "s1"), asked)
        script = session["network"][1]
        self.assertIsNone(script["body"])
        self.assertEqual(session["network"][2]["page_keys"][0]["source_url"],
                         "https://shop.test/cfg.js")

    def test_a_key_no_site_page_stated_stays_refused(self):
        session, _ = self._session("https://search.prov.test/q?query=red"
                                   "&x-algolia-api-key=k3y-public-123",
                                   page="<html><body>no key here</body></html>")
        provider = session["network"][1]
        self.assertTrue(provider["credential_bound"])
        self.assertIsNone(provider["body"])
        self.assertNotIn("page_keys", provider)

    def test_a_session_cookie_is_never_a_page_key(self):
        session, _ = self._session("https://search.prov.test/q?query=red"
                                   "&x-algolia-api-key=k3y-public-123",
                                   extra={"cookie": "sessionid=abc123"})
        self.assertTrue(session["network"][1]["credential_bound"])

    def test_a_redirect_keeps_its_hops(self):
        rec = A.Recorder(pin=None)
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "r1", "type": "Document",
            "request": {"url": "https://shop.test/search?q=nike", "method": "GET", "headers": {}}}})
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": "r1", "type": "Document", "redirectResponse": {"status": 302},
            "request": {"url": "https://shop.test/collections/nike", "method": "GET",
                        "headers": {}}}})
        net = rec.to_session("s")["network"]
        self.assertEqual(len(net), 1)
        self.assertEqual(net[0]["url"], "https://shop.test/collections/nike")
        self.assertEqual(net[0]["redirected_from"], ["https://shop.test/search?q=nike"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_forget_and_expiry(self):
        rec, _ = record("serde", ROWS["serde"])
        p1 = A.write_session(rec.to_session(A.new_session_id()), self.tmp.name)
        p2 = A.write_session(rec.to_session(A.new_session_id()), self.tmp.name)
        self.assertEqual(len(A.list_sessions(self.tmp.name)), 2)
        os.utime(p1, (1, 1))
        self.assertEqual(A.purge_expired(self.tmp.name, max_age_days=7), 1)
        self.assertFalse(os.path.exists(p1))
        sid = os.path.basename(p2)[:-5]
        self.assertEqual(A.forget(sid, self.tmp.name), 1)
        self.assertEqual(A.list_sessions(self.tmp.name), [])
        A.write_session(rec.to_session(A.new_session_id()), self.tmp.name)
        self.assertEqual(A.forget("", self.tmp.name), 1)

    def test_session_id_cannot_escape_the_directory(self):
        with self.assertRaises(A.AttachError):
            A.write_session({"id": "../../x"}, self.tmp.name)


# ---------------------------------------------------------------------------
# compile and prove from attached sessions
# ---------------------------------------------------------------------------

class CompileTests(unittest.TestCase):
    def _sessions(self, input_value=True):
        out = []
        for i, (v, rows) in enumerate(ROWS.items()):
            rec, _ = record(v, rows, input_value=input_value)
            out.append(rec.to_session(f"20260923T12000{i}-0000000{i}"))
        return out

    def test_three_sessions_compile_and_prove(self):
        sessions = self._sessions()
        recs = [A.session_to_recording(s, "q", v, f"t{i}")
                for i, (s, v) in enumerate(zip(sessions, ROWS))]
        executor = ApiStepExecutor(transport=RecordedTransport(recs))
        compiled, proof, report, _recs = A.compile_sessions(
            sessions, executor=executor, preflight=lambda c: "")
        self.assertIsNotNone(compiled.template, compiled.reason)
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(report["values"], ["serde", "regex", "tokio"])
        self.assertTrue(report["ground_truth_selector"].endswith("li.row"))
        self.assertNotIn("Home", report["ground_truth_sample"])
        self.assertEqual(len(report["repeat_ms"]), 2)
        # 3 sessions: the selector saw the validation pages -> never "proved"
        self.assertEqual(report["status"], "checked (selection not held out)")
        self.assertFalse(report["selection_held_out"])

    def test_four_sessions_prove_with_validation_untouched_by_fitting(self):
        from anyuse.execution import learn_loop
        sessions = []
        for i, (v, rows) in enumerate(ROWS4.items()):
            rec, _ = record(v, rows)
            sessions.append(rec.to_session(f"20260923T12000{i}-0000000{i}"))
        recs = [A.session_to_recording(s, "q", v, f"t{i}")
                for i, (s, v) in enumerate(zip(sessions, ROWS4))]
        fitted_on = []
        real = learn_loop.derive_ground_truth

        def spy(recordings):
            fitted_on.append([r["param"]["value"] for r in recordings])
            return real(recordings)
        learn_loop.derive_ground_truth = spy
        try:
            compiled, proof, report, out = A.compile_sessions(
                sessions, executor=ApiStepExecutor(transport=RecordedTransport(recs)),
                preflight=lambda c: "")
        finally:
            learn_loop.derive_ground_truth = real
        self.assertEqual(fitted_on, [["serde", "regex"]])
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual([c.value for c in proof.cases], ["tokio", "rand"])
        self.assertEqual(report["status"], "proved")
        self.assertTrue(report["selection_held_out"])
        # the validation pages were only READ with the fitted selector
        self.assertTrue(out[2]["ui"]["items"])

    def test_status_rule(self):
        self.assertEqual(A.proof_status(True, True), "proved")
        self.assertEqual(A.proof_status(True, False), "checked (selection not held out)")
        self.assertEqual(A.proof_status(False, True), "not proved")

    def test_input_is_inferred_from_the_result_url(self):
        sessions = self._sessions(input_value=False)
        name, values, how = A.infer_param(sessions)
        self.assertEqual((name, values), ("q", ["serde", "regex", "tokio"]))
        self.assertIn("inferred", how)

    def test_too_few_sessions_or_same_input_is_refused(self):
        sessions = self._sessions()
        with self.assertRaises(A.AttachError):
            A.compile_sessions(sessions[:2], preflight=lambda c: "")
        dup = [sessions[0], sessions[0], sessions[1]]
        with self.assertRaises(A.AttachError):
            A.compile_sessions(dup, preflight=lambda c: "")

    def test_origin_not_observed_is_refused_before_proof(self):
        sessions = self._sessions()
        for s in sessions:
            for r in s["network"]:
                r["url"] = r["url"].replace("https://ex.test/api", "https://ex.test/api")
        calls = []

        class T:
            def send(self, *a):
                calls.append(a)
                return ApiResponse(500, {}, "")
        # the preflight sees only origins from the compile session
        compiled, proof, report, _ = A.compile_sessions(
            sessions, executor=ApiStepExecutor(transport=T()),
            preflight=lambda c: "stop here")
        self.assertFalse(proof.passed)
        self.assertIn("stop here", proof.reason)
        self.assertEqual(calls, [])

    def test_select_sessions_groups_by_task(self):
        a = self._sessions()
        other = dict(a[0], task="other", id="20260923T130000-aaaaaaaa")
        picked = A.select_sessions(a + [other], task="ex_search")
        self.assertEqual(len(picked), 3)
        self.assertEqual(A.select_sessions(a + [other])[0]["task"], "other")
        self.assertEqual(len(A.select_sessions(a, n=2)), 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ANYUSE_DATA_DIR")
        os.environ["ANYUSE_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ANYUSE_DATA_DIR", None)
        else:
            os.environ["ANYUSE_DATA_DIR"] = self.old
        self.tmp.cleanup()

    def test_attach_refuses_a_non_loopback_endpoint_before_any_request(self):
        from click.testing import CliRunner
        from anyuse.cli import cli
        for bad in ["http://localhost:9222", "http://192.168.64.2:9222",
                    "http://u:p@127.0.0.1:9222"]:
            res = CliRunner().invoke(cli, ["attach", "--cdp", bad], obj={})
            self.assertEqual(res.exit_code, 2, res.output)
            self.assertIn("attach refused", res.output)

    def test_forget_and_compile_without_sessions(self):
        from click.testing import CliRunner
        from anyuse.cli import cli
        rec, _ = record("serde", ROWS["serde"])
        A.write_session(rec.to_session(A.new_session_id()))
        res = CliRunner().invoke(cli, ["compile", "last"], obj={})
        self.assertEqual(res.exit_code, 2, res.output)
        self.assertIn("at least 3 sessions", res.output)
        res = CliRunner().invoke(cli, ["attach", "--forget"], obj={})
        self.assertIn("deleted 1", res.output)
        res = CliRunner().invoke(cli, ["compile", "last"], obj={})
        self.assertIn("no attach sessions", res.output)

    def test_compile_last_persists_only_after_confirmation(self):
        from click.testing import CliRunner
        from anyuse import cli as cli_mod
        from anyuse.execution import api_permission

        sessions = []
        for i, (v, rows) in enumerate(ROWS.items()):
            rec, _ = record(v, rows)
            s = rec.to_session(f"20260923T12000{i}-0000000{i}")
            A.write_session(s)
            sessions.append(s)
        recs = [A.session_to_recording(s, "q", v, f"t{i}")
                for i, (s, v) in enumerate(zip(sessions, ROWS))]
        real_compile = A.compile_sessions
        real_check = api_permission.check_permission

        def offline_compile(sess, robots_policy=None, **kw):
            return real_compile(sess, executor=ApiStepExecutor(
                transport=RecordedTransport(recs)), preflight=lambda c: "")

        def allowed(url, hosts_path=None, fetcher=None, policy=None):
            return api_permission.PermissionVerdict(
                may_prefer=True, host="ex.test", reason="test")

        A.compile_sessions = offline_compile
        api_permission.check_permission = allowed
        try:
            res = CliRunner().invoke(cli_mod.cli, ["compile", "last"], input="n\n", obj={})
            self.assertIn("PASSED", res.output)
            self.assertIn("not confirmed", res.output)
            res = CliRunner().invoke(cli_mod.cli, ["compile", "last", "--confirm",
                                                   "https://ex.test/other"], obj={})
            self.assertIn("not confirmed", res.output)
            res = CliRunner().invoke(cli_mod.cli, ["compile", "last", "--explain",
                                                   "--confirm",
                                                   "https://ex.test/api/search"], obj={})
            self.assertIn("Persisted", res.output, res.output)
            self.assertIn("Status: checked (selection not held out)", res.output)
            self.assertIn("why NOT proved", res.output)
            # the weaker status travels into the record and the MCP surface
            from anyuse.mcpserver import compiled_tools
            api_permission.check_permission = real_check
            hosts = os.path.join(self.tmp.name, "hosts.json")
            api_permission.acknowledge_host("ex.test", note="test", path=hosts)
            rows = compiled_tools.list_compiled(hosts_path=hosts)
            self.assertEqual(rows[0]["proof"]["status"], "checked (selection not held out)")
            self.assertIn("not proved", rows[0]["ladder_tier_means"])
            tools = compiled_tools.executable_typed_tools(hosts_path=hosts)
            self.assertIn("status checked (selection not held out)",
                          tools[0]["description"])
            self.assertNotIn("status proved", tools[0]["description"])
        finally:
            A.compile_sessions = real_compile
            api_permission.check_permission = real_check



class CratesIoListPage(unittest.TestCase):
    """Four sessions attached to crates.io's own search (trimmed: the result page,
    its document and the API response). Each result row carries a version, a
    description and download counts next to its name, so the whole row explains
    no request; read by each row's own name, the JSON API does."""

    DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "testdata", "attach_crates")

    def test_a_spa_list_compiles_from_the_row_names_and_is_proved_on_held_out_inputs(self):
        names = sorted(os.listdir(self.DIR))
        sessions = [json.load(open(os.path.join(self.DIR, n))) for n in names]
        captured = {}
        for s in sessions:
            for r in s["network"]:
                sp = urllib.parse.urlsplit(r["url"])
                if sp.path == "/api/v1/crates":
                    captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]
        sent = []

        class Captured:
            def send(self, method, url, headers, body, timeout_s):
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
                sent.append(q)
                return ApiResponse(200, {"Content-Type": "application/json"}, captured[q])
        compiled, proof, report, recs = A.compile_sessions(
            sessions, executor=ApiStepExecutor(transport=Captured()), preflight=lambda _c: "")
        self.assertIsNotNone(compiled.template, compiled.reason)
        self.assertEqual(compiled.ground_truth_reading, "labels")
        self.assertEqual(compiled.template.extract, {"items": "crates[*].id"})
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(report["status"], "proved")
        recorded = sorted(v for v in sent if v in captured)
        self.assertEqual(recorded, ["clap", "tokio"])          # only the held-out inputs
        # and the made-up searches that learn the site's empty page
        from anyuse.execution import empty_page as EP
        self.assertEqual(len(sent) - len(recorded), EP.PROBES)
        self.assertEqual(recs[0]["ui"]["labels"][:3], ["serde", "serde-saphyr", "clickhouse"])


class MissingDevToolsClientTests(unittest.TestCase):
    def test_attach_without_websockets_says_what_to_install_before_touching_the_browser(self):
        import importlib.util
        real = importlib.util.find_spec
        touched = []
        importlib.util.find_spec = lambda name, *a: None if name == "websockets" else real(name, *a)
        try:
            with self.assertRaises(A.AttachError) as ctx:
                A.attach("http://127.0.0.1:9222", task="t", input_value="q=x",
                         http_get=lambda url: touched.append(url))
        finally:
            importlib.util.find_spec = real
        self.assertIn("pip install 'anyuse[cdp]'", str(ctx.exception))
        self.assertEqual(touched, [])

if __name__ == "__main__":
    unittest.main()
