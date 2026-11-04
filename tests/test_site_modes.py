"""Per-site web-tier modes (execution/site_modes.py), their CLI, and the
enforcement points on the PRODUCT path. Offline: no network, injected cookie
readers, fake transports, synthetic attach sessions."""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import tempfile
import unittest
import urllib.parse
import urllib.robotparser
from unittest import mock

from click.testing import CliRunner

from anyuse.cli import cli
from anyuse.execution import site_modes
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, HttpxTransport, RequestTemplate

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL = "SIGNED-IN-SECRET-7c1e"


def _toml(tmp):
    return os.path.join(tmp, "site_modes.toml")


class DataDir(unittest.TestCase):
    """A private ANYUSE_DATA_DIR for tests that use the default file path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("ANYUSE_DATA_DIR")
        os.environ["ANYUSE_DATA_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("ANYUSE_DATA_DIR", None) if saved is None
                        else os.environ.__setitem__("ANYUSE_DATA_DIR", saved))
        self.path = _toml(self.tmp.name)


class CapTransport:
    """Records every send; returns a canned response (or one per call)."""

    def __init__(self, status=200, text='{"crates":[{"id":"a"}]}',
                 content_type="application/json", responses=None):
        self.status, self.text, self.ct = status, text, content_type
        self.responses = list(responses) if responses else None
        self.calls = []

    def send_with_policy(self, method, url, headers, body, timeout_s, *,
                         allowed_origins, user_agent="", user_agent_domain="", cookies=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "user_agent": user_agent, "user_agent_domain": user_agent_domain,
                           "cookies": [dict(c) for c in cookies] if cookies else None})
        if self.responses:
            status, text, ct = self.responses.pop(0)
            return ApiResponse(status=status, headers={"Content-Type": ct}, text=text)
        return ApiResponse(status=self.status, headers={"Content-Type": self.ct}, text=self.text)


def _tmpl(**kw):
    base = dict(method="GET", url="https://api.example.com/api/v1/crates",
                query={"q": "{q}"}, headers={"accept": "application/json"},
                extract={"items": "crates[*].id"},
                expect={"status_in": [200], "content_type": "json", "path": "crates",
                        "min_items": 1, "required_keys": ["id"]},
                params=["q"])
    base.update(kw)
    return RequestTemplate(**base)


def _cookie(value, name="sid"):
    return {"name": name, "value": value, "domain": "example.com", "host_only": False,
            "path": "/", "secure": False}


def _login_site(path, host="example.com"):
    site_modes.set_modes(host, login="replay", route="replay",
                         session={"browser": "chrome", "profile": "P", "host": host,
                                  "db_path": "/nonexistent", "cookie_names": ["sid"]},
                         path=path)


# ---------------------------------------------------------------------------
# storage + the one accessor
# ---------------------------------------------------------------------------

class Storage(DataDir):
    def test_keyed_by_site_and_owner_only(self):
        site_modes.set_modes("www.example.com", identity="browser", robots="ignore",
                             path=self.path)
        m = site_modes.effective_modes("api.example.com", self.path)
        self.assertEqual((m.host, m.identity, m.robots), ("example.com", "browser", "ignore"))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertIsNone(site_modes.effective_modes("unrelated.test", self.path))
        self.assertFalse(hasattr(m, "identity_user_agent"))        # dead field removed

    def test_route_replay_requires_login_replay(self):
        with self.assertRaises(site_modes.SiteModeError):
            site_modes.set_modes("example.com", route="replay", path=self.path)
        _login_site(self.path)
        self.assertEqual(site_modes.stored_modes("example.com", self.path).route, "replay")

    def test_reset(self):
        site_modes.set_modes("example.com", robots="ignore", path=self.path)
        self.assertTrue(site_modes.reset_modes("www.example.com", self.path))
        self.assertIsNone(site_modes.stored_modes("example.com", self.path))


# ---------------------------------------------------------------------------
# (1) the certain-registrable-domain rule
# ---------------------------------------------------------------------------

class SiteKeyRule(unittest.TestCase):
    REFUSED = ("*", "*.example.com", "example .com", "com", "localhost", "co.uk",
               "com.au", "com.pk", "shop.com.pk", "co.ug", "x.co.ug", "tokyo.jp",
               "foo.tokyo.jp", "shop.adv.br", "x.mi.it", "mi.it", "qc.ca", "co.me",
               "uk.com", "duckdns.org", "github.io", "herokuapp.com", "vercel.app",
               "netlify.app", "a_b.com", "ex<ample>.com")
    ACCEPTED = {"www.example.co.uk": "example.co.uk", "sub.example.com": "example.com",
                "x.qc.ca": "x.qc.ca", "google.ca": "google.ca", "x.uk.com": "x.uk.com",
                "me.duckdns.org": "me.duckdns.org", "foo.github.io": "foo.github.io",
                "app.herokuapp.com": "app.herokuapp.com", "web.de": "web.de",
                "id.me": "id.me", "co.co": "co.co", "x.co.me": "x.co.me",
                "google.de": "google.de", "shop.com.br": "shop.com.br"}

    def test_uncertain_suffixes_and_private_registry_parents_are_refused(self):
        for bad in self.REFUSED:
            with self.assertRaises(site_modes.SiteModeError, msg=bad):
                site_modes.validate_host(bad)

    def test_certain_registrable_domains_are_accepted(self):
        for host, key in self.ACCEPTED.items():
            self.assertEqual(site_modes.validate_host(host), key, host)

    def test_go_to_passes_the_suffix_rule_but_the_gov_rule_refuses_it(self):
        # go.<cc> is a government pattern in execution/gov_hosts.py: refused with ANYUSE_REFUSE_GOV_HOSTS on
        self.assertEqual(site_modes.site_key("go.to"), "go.to")
        with mock.patch.dict(os.environ, {"ANYUSE_REFUSE_GOV_HOSTS": "on"}):
            with self.assertRaises(site_modes.SiteModeError) as ctx:
                site_modes.validate_host("go.to")
        self.assertIn("government", str(ctx.exception))
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS", None)
            self.assertEqual(site_modes.validate_host("go.to"), "go.to")

    def test_uts46_idna(self):
        self.assertEqual(site_modes.validate_host("straße.de"), "xn--strae-oqa.de")
        self.assertEqual(site_modes.validate_host("bücher.de"), "xn--bcher-kva.de")

    def test_hand_edited_suffix_or_gov_key_never_applies(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = _toml(tmp.name)
        site_modes._write({
            "co.ug": {"identity": "browser", "robots": "ignore", "login": "none", "route": "ui"},
            "duckdns.org": {"identity": "browser", "robots": "ignore", "login": "none",
                            "route": "ui"},
            "bund.de": {"identity": "browser", "robots": "ignore", "login": "none",
                        "route": "ui"}}, path)
        for host in ("shop.co.ug", "me.duckdns.org"):
            self.assertIsNone(site_modes.effective_modes(host, path), host)
        # the government key applies only while ANYUSE_REFUSE_GOV_HOSTS is off
        with mock.patch.dict(os.environ, {"ANYUSE_REFUSE_GOV_HOSTS": "on"}):
            self.assertIsNone(site_modes.effective_modes("www.bund.de", path))
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS", None)
            self.assertIsNotNone(site_modes.effective_modes("www.bund.de", path))

    def test_never_contact_refused(self):
        with mock.patch.object(site_modes, "_never_contact", return_value={"blockme.test"}):
            self.assertIn("never-contact", site_modes.host_block_reason("blockme.test"))


# ---------------------------------------------------------------------------
# (8/9/10) file trust and the TOML writer
# ---------------------------------------------------------------------------

class FileTrust(DataDir):
    def test_world_writable_file_gives_no_modes(self):
        site_modes.set_modes("example.com", robots="ignore", path=self.path)
        os.chmod(self.path, 0o666)
        self.assertIn("writable", site_modes.file_trust_reason(self.path))
        self.assertIsNone(site_modes.effective_modes("example.com", self.path))

    def test_symlinked_file_is_refused(self):
        real = os.path.join(self.tmp.name, "real.toml")
        site_modes.set_modes("example.com", robots="ignore", path=real)
        link = _toml(self.tmp.name)
        os.symlink(real, link)
        self.assertIn("symlink", site_modes.file_trust_reason(link))
        self.assertIsNone(site_modes.effective_modes("example.com", link))

    def test_group_writable_parent_is_refused(self):
        sub = os.path.join(self.tmp.name, "shared")
        os.mkdir(sub)
        path = _toml(sub)
        site_modes.set_modes("example.com", robots="ignore", path=path)
        os.chmod(sub, 0o770)
        self.assertIn("writable", site_modes.file_trust_reason(path))
        self.assertIsNone(site_modes.effective_modes("example.com", path))

    def test_unreadable_file_is_not_clobbered(self):
        with open(self.path, "w") as fh:
            fh.write("this is { not valid TOML")
        os.chmod(self.path, 0o600)
        with self.assertRaises(site_modes.SiteModeError):
            site_modes.set_modes("example.com", robots="ignore", path=self.path)
        with open(self.path) as fh:
            self.assertIn("not valid TOML", fh.read())

    def test_control_characters_are_escaped_and_round_trip(self):
        site_modes.set_modes("example.com", login="replay", path=self.path,
                             session={"browser": "chrome", "profile": "Pro\x07file\x1b",
                                      "host": "example.com", "db_path": "/a\nb",
                                      "cookie_names": ["sid"]})
        with open(self.path) as fh:
            raw = fh.read()
        self.assertNotIn("\x07", raw)
        self.assertNotIn("\x1b", raw)
        got = site_modes.stored_modes("example.com", self.path).session
        self.assertEqual((got["profile"], got["db_path"]), ("Pro\x07file\x1b", "/a\nb"))


# ---------------------------------------------------------------------------
# the CLI, behind the typed-code TTY gate
# ---------------------------------------------------------------------------

class Cli(DataDir):
    def setUp(self):
        super().setUp()
        self.runner = CliRunner()

    def _allow(self):
        from anyuse.execution import human_gate
        return mock.patch.object(human_gate, "TtyGate", return_value=mock.Mock(return_value=None))

    def test_set_non_default_needs_the_tty_gate(self):
        from anyuse.execution import human_gate
        refused = mock.Mock(return_value="refused: needs a person")
        with mock.patch.object(human_gate, "TtyGate", return_value=refused):
            r = self.runner.invoke(cli, ["sites", "set", "example.com", "--robots", "ignore"])
        self.assertNotEqual(r.exit_code, 0)
        self.assertIsNone(site_modes.stored_modes("example.com"))
        with self._allow():
            r = self.runner.invoke(cli, ["sites", "set", "example.com", "--robots", "ignore"])
        self.assertEqual(r.exit_code, 0, r.output)

    def test_list_show_reset(self):
        with self._allow():
            self.runner.invoke(cli, ["sites", "set", "example.com", "--identity", "browser"])
        self.assertIn("identity=browser",
                      self.runner.invoke(cli, ["sites", "show", "example.com"]).output)
        self.assertIn("reset", self.runner.invoke(cli, ["sites", "reset", "example.com"]).output)

    def _cookie_db(self, value):
        from anyuse.execution import session_import as SI
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        import hashlib
        key = SI.derive_key("pw")
        payload = hashlib.sha256(b".example.com").digest() + value.encode()
        pad = 16 - (len(payload) % 16)
        payload += bytes([pad]) * pad
        enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
        blob = b"v10" + enc.update(payload) + enc.finalize()
        db = os.path.join(self.tmp.name, "Cookies")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES('version','24')")
        conn.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, encrypted_value BLOB, "
                     "path TEXT, is_secure INTEGER, expires_utc INTEGER)")
        conn.execute("INSERT INTO cookies VALUES(?,?,?,?,?,?)", (".example.com", "sid", blob,
                                                               "/", 1, 0))
        conn.commit()
        conn.close()
        return db

    def test_login_stores_names_only_never_the_value(self):
        db = self._cookie_db(SENTINEL)
        with self._allow():
            r = self.runner.invoke(cli, ["sites", "login", "shop.example.com", "--browser",
                                         "chrome", "--profile", "Default", "--db", db])
        self.assertEqual(r.exit_code, 0, r.output)
        modes = site_modes.stored_modes("shop.example.com")
        self.assertEqual(modes.session["cookie_names"], ["sid"])
        with open(site_modes.site_modes_path()) as fh:
            self.assertNotIn(SENTINEL, fh.read())
        self.assertNotIn(SENTINEL, r.output)
        self.assertNotIn(SENTINEL, repr(modes))

    def test_login_refuses_off_site_cookie_host_and_control_characters(self):
        db = self._cookie_db("v")
        r = self.runner.invoke(cli, ["sites", "login", "shop.example.com", "--browser",
                                     "chrome", "--db", db, "--cookie-host", "evil.com"])
        self.assertIn("not under the site", r.output)
        r = self.runner.invoke(cli, ["sites", "login", "shop.example.com", "--browser",
                                     "chrome", "--db", db, "--profile", "a\x07b"])
        self.assertIn("control character", r.output)


# ---------------------------------------------------------------------------
# enforcement on the product replay path
# ---------------------------------------------------------------------------

class Identity(DataDir):
    def test_browser_identity_sends_only_the_captured_ua(self):
        site_modes.set_modes("example.com", identity="browser", path=self.path)
        cap = CapTransport()
        r = ApiStepExecutor(transport=cap, apply_site_modes=True, site_modes_path=self.path
                            ).execute(_tmpl(browser_ua="Captured/7.0"), {"q": "a"})
        self.assertTrue(r["success"], r)
        self.assertEqual((cap.calls[0]["user_agent"], cap.calls[0]["user_agent_domain"]),
                         ("Captured/7.0", "example.com"))
        self.assertEqual(cap.calls[0]["headers"], {"accept": "application/json"})

    def test_browser_identity_refused_without_a_captured_ua(self):
        site_modes.set_modes("example.com", identity="browser", path=self.path)
        r = ApiStepExecutor(transport=CapTransport(), apply_site_modes=True,
                            site_modes_path=self.path).execute(_tmpl(), {"q": "a"})
        self.assertIn("captured no browser User-Agent", r["error"])

    def test_robots_ignore(self):
        from anyuse.execution import api_permission
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /"])
        api_permission._ROBOTS_CACHE["api.example.com"] = parser
        self.addCleanup(api_permission._ROBOTS_CACHE.pop, "api.example.com", None)
        cap = CapTransport()
        r = ApiStepExecutor(transport=cap, apply_site_modes=True,
                            site_modes_path=self.path).execute(_tmpl(), {"q": "a"})
        self.assertTrue(r.get("robots_refused"))
        site_modes.set_modes("example.com", robots="ignore", path=self.path)
        cap2 = CapTransport()
        r2 = ApiStepExecutor(transport=cap2, apply_site_modes=True,
                             site_modes_path=self.path).execute(_tmpl(), {"q": "a"})
        self.assertTrue(r2["success"], r2)


class LoginReplay(DataDir):
    """(5) login=replay auth handling: re-import once, never a sticky block."""

    def setUp(self):
        super().setUp()
        _login_site(self.path)

    def _ex(self, cap):
        return ApiStepExecutor(transport=cap, apply_site_modes=True, site_modes_path=self.path)

    def test_cookie_refs_refused_without_login_and_route_replay(self):
        site_modes.set_modes("example.com", login="none", route="ui", path=self.path)
        r = self._ex(CapTransport()).execute(_tmpl(cookie_refs=["sid"]), {"q": "a"})
        self.assertIn("logged-in workflow", r["error"])

    def test_reimport_sends_the_NEW_cookies_on_the_retry(self):
        ok = '{"crates":[{"id":"a"}]}'
        cap = CapTransport(responses=[(401, "", "text/plain"), (200, ok, "application/json")])
        ex = self._ex(cap)
        batches = iter([[_cookie("OLD")], [_cookie("NEW")]])
        with mock.patch.object(ex, "_import_cookies", side_effect=lambda m: next(batches)):
            r = ex.execute(_tmpl(cookie_refs=["sid"]), {"q": "a"})
        self.assertTrue(r["success"], r)
        self.assertEqual([c["cookies"][0]["value"] for c in cap.calls], ["OLD", "NEW"])

    def test_non_challenge_403_reimports_and_is_never_a_sticky_block(self):
        cap = CapTransport(responses=[(403, "Forbidden", "text/plain"),
                                      (403, "Forbidden", "text/plain")])
        ex = self._ex(cap)
        with mock.patch.object(ex, "_import_cookies", return_value=[_cookie("v")]), \
                mock.patch("anyuse.execution.blocked.mark_blocked") as marked:
            r = ex.execute(_tmpl(cookie_refs=["sid"]), {"q": "a"})
        self.assertTrue(r.get("auth_required"))
        self.assertIn("anyuse sites login", r["error"])
        self.assertNotIn("blocked_state", r)
        self.assertEqual(len(cap.calls), 2)
        marked.assert_not_called()

    def test_a_401_saying_authentication_required_is_auth_not_a_login_wall_block(self):
        body = "<html><body><h1>Authentication required</h1>Please sign in</body></html>"
        cap = CapTransport(responses=[(401, body, "text/html"), (401, body, "text/html")])
        ex = self._ex(cap)
        with mock.patch.object(ex, "_import_cookies", return_value=[_cookie("v")]), \
                mock.patch("anyuse.execution.blocked.mark_blocked") as marked:
            r = ex.execute(_tmpl(cookie_refs=["sid"]), {"q": "a"})
        self.assertTrue(r.get("auth_required"))
        self.assertNotIn("blocked_state", r)
        marked.assert_not_called()

    def test_a_failed_reimport_never_retries_with_the_old_cookies(self):
        cap = CapTransport(responses=[(401, "", "text/plain")])
        ex = self._ex(cap)
        calls = []

        def imp(m):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("profile locked")
            return [_cookie("OLD")]
        with mock.patch.object(ex, "_import_cookies", side_effect=imp):
            r = ex.execute(_tmpl(cookie_refs=["sid"]), {"q": "a"})
        self.assertEqual(len(cap.calls), 1)                 # no second send
        self.assertIn("re-import failed", r["error"])
        self.assertIn("anyuse sites login", r["error"])

    def test_a_challenge_stays_a_sticky_block_without_reimport(self):
        challenge = ('<html><head><title>Just a moment...</title></head><body>'
                     '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>'
                     'Checking your browser</body></html>')
        cap = CapTransport(responses=[(403, challenge, "text/html")])
        ex = self._ex(cap)
        imports = []
        with mock.patch.object(ex, "_import_cookies",
                               side_effect=lambda m: imports.append(1) or [_cookie("v")]), \
                mock.patch("anyuse.execution.blocked.mark_blocked") as marked:
            r = ex.execute(_tmpl(cookie_refs=["sid"]), {"q": "a"})
        self.assertIn("blocked_state", r)
        self.assertEqual(len(cap.calls), 1)
        self.assertEqual(len(imports), 1)                   # the initial import only
        marked.assert_called_once()


# ---------------------------------------------------------------------------
# (2/6) the real transport: per-hop cookie and UA selection
# ---------------------------------------------------------------------------

class TransportPerHop(unittest.TestCase):
    def test_cookie_and_ua_only_on_the_entry_domain_hop(self):
        class SeqClient:
            def __init__(self, responses):
                self.responses = list(responses)
                self.sent = []
                self.cookies = type("C", (), {"clear": lambda self: None})()

            def request(self, method, url, headers, **kw):
                self.sent.append((url, dict(headers)))
                status, loc = self.responses.pop(0)
                return type("R", (), {"status_code": status,
                                      "headers": {"location": loc} if loc else {},
                                      "text": "{}"})()

        t = HttpxTransport()
        t._client = SeqClient([(302, "https://other.test/x"), (200, None)])
        with mock.patch("anyuse.execution.request_policy.destination_refusal", return_value=""):
            t.send_with_policy("GET", "https://example.com/", {}, None, 1,
                               allowed_origins={"https://example.com", "https://other.test"},
                               user_agent="UA/9", user_agent_domain="example.com",
                               cookies=[_cookie("S")])
        (_u0, h0), (_u1, h1) = t._client.sent
        self.assertEqual((h0.get("Cookie"), h0.get("User-Agent")), ("sid=S", "UA/9"))
        self.assertNotIn("Cookie", h1)
        self.assertNotIn("User-Agent", h1)


# ---------------------------------------------------------------------------
# attach capture: signed-in pages withheld (2b), UA only when opted in (6)
# ---------------------------------------------------------------------------

def _feed_xhr(rec, rid, url, cookies=("sid",), ua="Cap/3", body='{"results":[]}'):
    rec.on_event({"method": "Network.requestWillBeSentExtraInfo",
                  "params": {"requestId": rid, "headers": {},
                             "associatedCookies": [{"cookie": {"name": n}} for n in cookies]}})
    rec.on_event({"method": "Network.requestWillBeSent",
                  "params": {"requestId": rid, "type": "XHR",
                             "request": {"url": url, "method": "GET",
                                         "headers": {"User-Agent": ua}}}})
    rec.on_event({"method": "Network.responseReceived",
                  "params": {"requestId": rid, "type": "XHR",
                             "response": {"status": 200, "mimeType": "application/json",
                                          "headers": {"content-type": "application/json"}}}})
    rec.on_event({"method": "Network.loadingFinished",
                  "params": {"requestId": rid, "encodedDataLength": len(body)}})
    rec.on_body(rid, {"body": body})


def _feed_signed_in_document(rec, url):
    rec.on_event({"method": "Network.requestWillBeSentExtraInfo",
                  "params": {"requestId": "doc", "headers": {},
                             "associatedCookies": [{"cookie": {"name": "sid"}}]}})
    rec.on_event({"method": "Network.requestWillBeSent",
                  "params": {"requestId": "doc", "type": "Document",
                             "request": {"url": url, "method": "GET", "headers": {}}}})
    rec.on_event({"method": "Network.responseReceived",
                  "params": {"requestId": "doc", "type": "Document",
                             "response": {"status": 200, "mimeType": "text/html",
                                          "headers": {"content-type": "text/html"}}}})
    out = rec.on_event({"method": "Network.loadingFinished",
                        "params": {"requestId": "doc", "encodedDataLength": 100}})
    if ("body", "doc") in out:
        rec.on_body("doc", {"body": f"<html><body>{SENTINEL}</body></html>"})


class AttachCapture(unittest.TestCase):
    def test_signed_in_page_text_never_lands_in_the_session(self):
        import sys
        sys.path.insert(0, HERE)
        from test_attach import snapshot
        from anyuse.execution.attach import Recorder
        rec = Recorder(login_replay_site="example.com")
        url = "https://shop.example.com/account?q=x"
        _feed_signed_in_document(rec, url)
        rec.on_snapshot(snapshot(url, ("html", {}, [("body", {}, [
            ("ul", {}, [("li", {}, [f"{SENTINEL} order 1"])])])])))
        _feed_xhr(rec, "x1", "https://shop.example.com/api/orders?q=x",
                  body='{"orders":[{"title":"keep me"}]}')
        session = rec.to_session("s")
        blob = json.dumps(session)
        self.assertNotIn(SENTINEL, blob)                   # no page text, DOM or body
        self.assertIn("keep me", blob)                     # the data the compile needs
        self.assertTrue(session["snapshots"][0]["withheld"])
        self.assertEqual(session["login_replay_site"], "example.com")

    def test_only_the_opted_in_site_is_kept(self):
        from anyuse.execution.attach import Recorder
        rec = Recorder(login_replay_site="example.com")
        _feed_xhr(rec, "a", "https://api.example.com/s?q=a")
        _feed_xhr(rec, "b", "https://other.test/s?q=a")
        net = {r["url"].split("/")[2]: r for r in rec.to_session("s")["network"]}
        self.assertEqual(net["api.example.com"].get("cookie_names"), ["sid"])
        self.assertTrue(net["other.test"]["credential_bound"])

    def test_ua_is_kept_only_when_the_predicate_accepts_the_host(self):
        from anyuse.execution.attach import Recorder
        rec = Recorder() # the measurement harness: no predicate
        _feed_xhr(rec, "a", "https://api.example.com/s?q=a", cookies=())
        self.assertNotIn("request_user_agent", rec.to_session("s")["network"][0])
        rec2 = Recorder(capture_ua_for=lambda h: h == "api.example.com")
        _feed_xhr(rec2, "a", "https://api.example.com/s?q=a", cookies=())
        self.assertEqual(rec2.to_session("s")["network"][0]["request_user_agent"], "Cap/3")

    def test_har_import_keeps_no_ua_without_the_predicate(self):
        from anyuse.execution.har_import import parse_har
        har = json.dumps({"log": {"entries": [{
            "request": {"url": "https://api.example.com/s?q=a", "method": "GET",
                        "headers": [{"name": "User-Agent", "value": "Cap/3"}]},
            "response": {"status": 200, "content": {"mimeType": "application/json",
                                                    "text": "{}"}}}]}})
        self.assertNotIn("request_user_agent", parse_har(har).entries[0].to_network())
        got = parse_har(har, capture_ua_for=lambda h: True).entries[0].to_network()
        self.assertEqual(got["request_user_agent"], "Cap/3")


class CompileSerialization(unittest.TestCase):
    REC = {"param": {"name": "q", "value": "serde"}, "ui": {"items": ["serde thing"]},
           "final_url": "https://api.example.com/search?q=serde",
           "network": [{"url": "https://api.example.com/search?q=serde", "method": "GET",
                        "status": 200, "mime": "application/json", "bytes": 60,
                        "body": '{"results":[{"name":"serde thing"}]}'}]}

    def _compile(self, **extra):
        import copy
        from anyuse.execution.api_compile import compile_recording
        rec = copy.deepcopy(self.REC)
        rec["network"][0].update(extra)
        return compile_recording(rec).template

    def test_a_recording_with_a_captured_ua_compiles_it_onto_the_template(self):
        t = self._compile(request_user_agent="RealBrowser/12", cookie_names=["sid"])
        self.assertEqual((t.browser_ua, t.cookie_refs), ("RealBrowser/12", ["sid"]))
        self.assertEqual(t.problems(), [])

    def test_a_default_compile_is_byte_identical_including_the_digest(self):
        from anyuse.execution.workflow_schema import template_digest
        plain = self._compile()
        self.assertNotIn("browser_ua", plain.to_dict())
        self.assertNotIn("cookie_refs", plain.to_dict())
        # a default attach session records no UA, so its compile cannot differ
        from anyuse.execution.attach import Recorder
        rec = Recorder()
        _feed_xhr(rec, "a", "https://api.example.com/search?q=serde", cookies=(),
                  body='{"results":[{"name":"serde thing"}]}')
        entry = rec.to_session("s")["network"][0]
        self.assertNotIn("request_user_agent", entry)
        again = self._compile(**{k: v for k, v in entry.items()
                                 if k in ("request_user_agent", "cookie_names")})
        self.assertEqual(template_digest(plain.to_dict()), template_digest(again.to_dict()))


# ---------------------------------------------------------------------------
# (2a) signed-in items are never persisted by a run
# ---------------------------------------------------------------------------

class RunNeverPersistsSignedInItems(DataDir):
    BODY = '{"orders":[{"title":"one"},{"title":"two"},{"title":"three"}]}'

    def _run(self, template, name):
        from anyuse.execution.api_compile import compiled_record, load_compiled, persist_compiled
        from anyuse.execution.api_permission import acknowledge_host
        from anyuse.mcpserver.compiled_tools import run_compiled

        class Store:
            def __init__(self, base):
                self.base_dir = base

            def get_workflow_dir(self, wid):
                return os.path.join(self.base_dir, f"workflow_{wid}")

            def get(self, name):
                return None
        store = Store(self.tmp.name)
        persist_compiled(store, name, compiled_record(
            template, {"passed": True, "passed_count": 2, "case_count": 2}, workflow=name))
        hosts = os.path.join(self.tmp.name, "hosts.json")
        acknowledge_host("shop.example.com", path=hosts)
        ex = ApiStepExecutor(transport=CapTransport(text=self.BODY), apply_site_modes=True,
                             site_modes_path=self.path)
        with mock.patch.object(ex, "_import_cookies", return_value=[_cookie("v")]):
            res = run_compiled(name, {"q": "x"}, storage=store, executor=ex, hosts_path=hosts)
        self.assertTrue(res["ok"], res)
        return load_compiled(store, name)

    def _template(self, **kw):
        return RequestTemplate(
            method="GET", url="https://shop.example.com/api/orders", query={"q": "{q}"},
            headers={"accept": "application/json"}, extract={"items": "orders[*].title"},
            expect={"status_in": [200], "content_type": "json", "path": "orders",
                    "min_items": 1, "required_keys": ["title"]}, params=["q"], **kw)

    def test_a_signed_in_run_stores_no_drift_anchor(self):
        _login_site(self.path)
        record = self._run(self._template(cookie_refs=["sid"]), "orders")
        self.assertNotIn("drift_anchor", record)
        self.assertNotIn("one", json.dumps(record))

    def test_control_an_ordinary_run_still_stores_its_anchor(self):
        record = self._run(self._template(), "public")
        self.assertIn("drift_anchor", record)


# ---------------------------------------------------------------------------
# (3) attach --login-replay -> compile -> proof -> saved
# ---------------------------------------------------------------------------

class LoginReplayEndToEnd(DataDir):
    VALUES = ("alpha", "bravo", "charlie", "delta")

    @staticmethod
    def _orders(value):
        return json.dumps({"orders": [{"title": f"{value} order {i}"} for i in (1, 2, 3)]})

    def _sessions(self):
        from anyuse.execution.attach import Recorder
        out = []
        for i, v in enumerate(self.VALUES):
            rec = Recorder(login_replay_site="example.com", task="orders",
                           input_param={"name": "q", "value": v})
            _feed_xhr(rec, f"x{i}", f"https://shop.example.com/api/orders?q={v}",
                      body=self._orders(v))
            session = rec.to_session(f"s{i}")
            out.append(session)
        return out

    def test_capture_compile_prove_and_save(self):
        from anyuse.execution import attach as A
        from anyuse.execution.api_compile import compiled_record, load_compiled, persist_compiled
        _login_site(self.path)

        class Fake:
            def __init__(self, owner):
                self.owner = owner
                self.cookies_seen = []

            def send_with_policy(self, method, url, headers, body, timeout_s, *,
                                 allowed_origins, user_agent="", user_agent_domain="",
                                 cookies=None):
                self.cookies_seen.append(cookies)
                value = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   self.owner._orders(value))
        fake = Fake(self)
        with mock.patch.object(ApiStepExecutor, "_import_cookies",
                               return_value=[_cookie("SESSION")]):
            compiled, proof, report, _recs = A.compile_sessions(
                self._sessions(), transport=fake, preflight=lambda c: "")
        self.assertTrue(proof.passed, proof.to_dict())
        self.assertEqual(compiled.template.cookie_refs, ["sid"])
        self.assertTrue(fake.cookies_seen and all(fake.cookies_seen))  # proved WITH cookies
        self.assertEqual(report["ground_truth_sample"], [])           # never echoed

        class Store:
            def __init__(self, base):
                self.base_dir = base

            def get_workflow_dir(self, wid):
                return os.path.join(self.base_dir, f"workflow_{wid}")
        store = Store(self.tmp.name)
        persist_compiled(store, "orders", compiled_record(
            compiled.template, proof.to_dict(), workflow="orders",
            source={"origin": "attach", "ground_truth": report["ground_truth_source"]}))
        saved = load_compiled(store, "orders")
        self.assertEqual(saved["template"]["cookie_refs"], ["sid"])
        self.assertNotIn("drift_anchor", saved)
        self.assertNotIn("order 1", json.dumps(saved))                # no signed-in items

    def test_a_bench_style_compile_without_login_sessions_never_enables_modes(self):
        from anyuse.execution import attach as A
        built = []
        real = ApiStepExecutor.__init__

        def spy(self, *a, **kw):
            built.append(kw.get("apply_site_modes", False))
            real(self, *a, **kw)
        with mock.patch.object(ApiStepExecutor, "__init__", spy):
            try:
                A.compile_sessions([{"input": {"name": "q", "value": v}, "network": [],
                                     "snapshots": []} for v in ("a", "b", "c")],
                                   preflight=lambda c: "")
            except A.AttachError:
                pass
        self.assertNotIn(True, built)


# ---------------------------------------------------------------------------
# (B) the benchmark guard and executor never see the modes
# ---------------------------------------------------------------------------

class BenchmarksNeverSeeModes(DataDir):
    def setUp(self):
        super().setUp()
        site_modes.set_modes("example.com", identity="browser", robots="ignore", path=self.path)
        _login_site(self.path)
        self.assertIsNotNone(site_modes.effective_modes("www.example.com", self.path))

    def test_guarded_browser_keeps_the_honest_identity(self):
        from anyuse.execution.cdp_guard import GuardedSession, RequestBudget
        from anyuse.execution.discover import Pacer
        sess = GuardedSession("http://127.0.0.1:9222", budget=RequestBudget(Pacer()),
                              allow_url=lambda u: True)
        self.assertEqual(sess.user_agent, "research-probe/0.1")
        self.assertIsNotNone(sess.guard.allow_url)

    def test_bench_executor_default_off(self):
        self.assertFalse(ApiStepExecutor().apply_site_modes)

    def test_guarded_and_bench_sources_do_not_touch_site_modes(self):
        import anyuse.execution.cdp_guard as G
        with open(G.__file__) as fh:
            self.assertNotIn("site_modes", fh.read())
        for f in glob.glob(os.path.join(HERE, "execution", "web_runtime", "*.py")):
            with open(f) as fh:
                src = fh.read()
            self.assertNotIn("site_modes", src, f)
            self.assertNotIn("apply_site_modes=True", src, f)
            self.assertNotIn("login_replay_site", src, f)
            self.assertNotIn("capture_ua_for", src, f)
            for m in re.finditer(r"run_compiled\(", src):
                self.assertIn("executor=", src[m.start():m.start() + 500],
                              f"{os.path.basename(f)}: run_compiled without executor=")


# ---------------------------------------------------------------------------
# (E) a default site sends the SAME request before and after this change
# ---------------------------------------------------------------------------

class DefaultPathUnchanged(DataDir):
    def test_same_request_with_modes_on_or_off(self):
        cap_off, cap_on = CapTransport(), CapTransport()
        ApiStepExecutor(transport=cap_off).execute(_tmpl(), {"q": "serde"})
        ApiStepExecutor(transport=cap_on, apply_site_modes=True,
                        site_modes_path=self.path).execute(_tmpl(), {"q": "serde"})
        self.assertEqual(cap_off.calls, cap_on.calls)
        self.assertEqual((cap_off.calls[0]["user_agent"], cap_off.calls[0]["cookies"]),
                         ("", None))

    def test_default_through_the_real_transport(self):
        class FakeClient:
            def __init__(self):
                self.sent = []
                self.cookies = type("C", (), {"clear": lambda self: None})()

            def request(self, method, url, headers, **kw):
                self.sent.append(dict(headers))
                return type("R", (), {"status_code": 200,
                                      "headers": {"Content-Type": "application/json"},
                                      "text": '{"crates":[{"id":"a"}]}'})()
        t = HttpxTransport()
        t._client = FakeClient()
        with mock.patch("anyuse.execution.request_policy.destination_refusal", return_value=""):
            r = ApiStepExecutor(transport=t).execute(_tmpl(), {"q": "serde"})
        self.assertTrue(r["success"], r)
        self.assertEqual(t._client.sent, [{"accept": "application/json"}])


if __name__ == "__main__":
    unittest.main()
