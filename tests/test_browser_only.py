"""Hosts that serve a browser and refuse a named HTTP client.

Milestone 6, work item 4. The rule this file pins is the one that matters:
AnyUse never changes its User-Agent or its headers to look like a different
client. What it does instead is record the fact, stop sending compiled requests
to that host, and keep using the rung that the host does serve.

Every test runs against a scratch registry file and an injected transport or
driver: nothing here makes a request.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

from anyuse.execution import blocked as B
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor, ReplayTemplate

ROWS = """<html><body><ul class="results">
<li class="row"><a>Alpha</a></li><li class="row"><a>Beta</a></li>
<li class="row"><a>Gamma</a></li></ul></body></html>"""


class Scratch(unittest.TestCase):
    """Every registry write goes to a temp dir, never to the real data dir."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.browser_only = os.path.join(self.dir, "browser_only.json")
        self.blocked = os.path.join(self.dir, "blocked.json")
        self._env = {k: os.environ.get(k) for k in
                     (B.BROWSER_ONLY_FILE_ENV, "ANYUSE_BLOCKED_HOSTS_FILE",
                      "ANYUSE_ROBOTS_POLICY")}
        os.environ[B.BROWSER_ONLY_FILE_ENV] = self.browser_only
        os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = self.blocked
        os.environ["ANYUSE_ROBOTS_POLICY"] = "off"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Registry(Scratch):

    def test_a_403_plus_a_served_browser_page_is_browser_only(self):
        state = B.BlockedState(blocked=True, reason=B.FORBIDDEN,
                               host="hackage.haskell.org", status=403)
        entry = B.browser_only_from_evidence(
            state, "https://hackage.haskell.org/packages/search?terms=lens", 12)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], 403)
        self.assertIn("12 item(s)", entry["browser_evidence"])
        self.assertTrue(B.is_browser_only("hackage.haskell.org"))

    def test_a_captcha_is_never_reclassified(self):
        for reason in (B.CAPTCHA_RECAPTCHA, B.CHALLENGE_INTERSTITIAL,
                       B.LOGIN_WALL, B.COOKIE_WALL):
            state = B.BlockedState(blocked=True, reason=reason, host="x.test",
                                   status=200)
            self.assertIsNone(B.browser_only_from_evidence(
                state, "https://x.test/p", 12),
                f"{reason} must never become browser-only")
        self.assertFalse(B.is_browser_only("x.test"))

    def test_a_browser_that_saw_nothing_is_not_evidence(self):
        state = B.BlockedState(blocked=True, reason=B.FORBIDDEN, host="x.test",
                               status=403)
        self.assertIsNone(B.browser_only_from_evidence(state, "https://x.test/p", 0))

    def test_evidence_from_a_different_host_is_not_evidence(self):
        state = B.BlockedState(blocked=True, reason=B.FORBIDDEN, host="a.test",
                               status=403)
        self.assertIsNone(B.browser_only_from_evidence(state, "https://b.test/p", 9))

    def test_marking_browser_only_clears_the_do_not_auto_retry_entry(self):
        state = B.BlockedState(blocked=True, reason=B.FORBIDDEN, host="h.test",
                               status=403)
        B.mark_blocked(state)
        self.assertIsNotNone(B.blocked_until("h.test"))
        B.browser_only_from_evidence(state, "https://h.test/p", 5)
        self.assertIsNone(B.blocked_until("h.test"))

    def test_it_can_be_cleared(self):
        state = B.BlockedState(blocked=True, reason=B.FORBIDDEN, host="h.test",
                               status=403)
        B.browser_only_from_evidence(state, "https://h.test/p", 5)
        self.assertTrue(B.clear_browser_only("h.test"))
        self.assertIsNone(B.is_browser_only("h.test"))

    def test_the_note_says_we_will_not_spoof(self):
        state = B.BlockedState(blocked=True, reason=B.FORBIDDEN, host="h.test",
                               status=403)
        entry = B.browser_only_from_evidence(state, "https://h.test/p", 5)
        self.assertIn("does not change its User-Agent", entry["note"])


class Rungs(Scratch):

    def mark(self, host="h.test"):
        B.browser_only_from_evidence(
            B.BlockedState(blocked=True, reason=B.FORBIDDEN, host=host, status=403),
            f"https://{host}/p", 7)

    def test_the_compiled_request_rung_is_never_attempted(self):
        self.mark()
        sent = []

        class Transport:
            def send(self, method, url, headers, body, timeout_s):
                sent.append(url)
                return ApiResponse(200, {"Content-Type": "application/json"}, "{}")

        out = ApiStepExecutor(transport=Transport()).execute(
            RequestTemplate(url="https://h.test/api", params=[],
                            extract={"items": "a[*]"}),
            {})
        self.assertFalse(out["success"])
        self.assertTrue(out["browser_only"])
        self.assertEqual(sent, [], "nothing may be put on the wire")
        self.assertIn("will not disguise its client", out["error"])

    def test_the_browser_replay_rung_still_runs_there(self):
        self.mark()

        class Driver:
            def open(self, url, settle_s, timeout_s, readiness=None):
                return PageResult(url=url, dom=ROWS, text="Alpha Beta Gamma",
                                  status=200, elapsed_ms=900.0)

        out = ReplayExecutor(driver=Driver()).execute(
            ReplayTemplate(url="https://h.test/s?q={q}", params=["q"],
                           extract={"items": "li.row"},
                           expect={"selector": "li.row", "min_items": 1}),
            {"q": "alpha"})
        self.assertTrue(out["success"], out.get("error"))
        self.assertTrue(out["browser_only"])
        self.assertEqual(len(out["extracted"]["items"]), 3)

    def test_a_do_not_auto_retry_entry_still_stops_the_replay_on_other_hosts(self):
        B.mark_blocked(B.BlockedState(blocked=True, reason=B.CAPTCHA_RECAPTCHA,
                                      host="wall.test", status=200))

        class Driver:
            def open(self, url, settle_s, timeout_s, readiness=None):
                raise AssertionError("a blocked host must not be opened")

        out = ReplayExecutor(driver=Driver()).execute(
            ReplayTemplate(url="https://wall.test/s?q={q}", params=["q"],
                           extract={"items": "li.row"}),
            {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertTrue(out["requires_human"])

    def test_a_pre_milestone6_driver_signature_still_works(self):
        class OldDriver:
            def open(self, url, settle_s, timeout_s):
                return PageResult(url=url, dom=ROWS, text="Alpha", status=200,
                                  elapsed_ms=10.0)

        out = ReplayExecutor(driver=OldDriver()).execute(
            ReplayTemplate(url="https://ok.test/s?q={q}", params=["q"],
                           extract={"items": "li.row"},
                           expect={"selector": "li.row", "min_items": 1},
                           readiness={"selector": "li.row"}),
            {"q": "alpha"})
        self.assertTrue(out["success"], out.get("error"))

    def test_a_type_error_inside_the_driver_is_a_failure_not_a_retry(self):
        # The signature is INSPECTED rather than a TypeError being caught:
        # catching one would also swallow a TypeError raised inside the driver
        # and silently re-run the navigation with the fixed timer, turning a
        # real bug into a mysterious second attempt against the site.
        calls = []

        class BuggyDriver:
            def open(self, url, settle_s, timeout_s, readiness=None):
                calls.append(url)
                raise TypeError("a bug inside the driver")

        out = ReplayExecutor(driver=BuggyDriver()).execute(
            ReplayTemplate(url="https://ok.test/s?q={q}", params=["q"],
                           extract={"items": "li.row"},
                           readiness={"selector": "li.row"}),
            {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertIn("a bug inside the driver", out.get("error", ""))
        self.assertEqual(len(calls), 1, "the navigation must not be retried")


class Reporting(Scratch):

    def test_the_registry_round_trips_as_json(self):
        self.assertEqual(B.browser_only_hosts(), {})
        B.browser_only_from_evidence(
            B.BlockedState(blocked=True, reason=B.FORBIDDEN, host="h.test",
                           status=403), "https://h.test/p", 4)
        with open(self.browser_only) as fh:
            raw = json.load(fh)
        self.assertEqual(list(raw), ["h.test"])
        self.assertEqual(B.browser_only_hosts()["h.test"]["from_reason"], B.FORBIDDEN)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
