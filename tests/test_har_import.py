"""HAR import: what it accepts, and — mostly — what it refuses.

A HAR is attacker-controllable input, so this file is written as the threat
model's test suite: for every claim in ``har_import``'s docstring there is a
test that the claim holds.

Everything here is offline. The fixtures are synthetic HARs built in-process;
one test replays a HAR recorded from the container browser if it is present,
and skips loudly otherwise, because a test that silently does nothing is worse
than no test.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

from anyuse.execution.har_import import (
    HarCase, HarError, MAX_BODY_BYTES, MAX_ENTRIES, MAX_FILE_BYTES,
    compile_from_har, har_to_recording, load_har, parse_har)

ITEMS = ["Alpha Pkg", "Beta Pkg", "Gamma Pkg", "Delta Pkg"]


def api_body(names=ITEMS):
    return json.dumps({"results": [{"name": n} for n in names]})


def entry(url="https://ex.test/api?q=alpha", method="GET", status=200,
          mime="application/json", text=None, headers=None, cookies=None,
          post=None, encoding=None):
    content = {"size": len(text or ""), "mimeType": mime}
    if encoding:
        content["encoding"] = encoding
    if text is not None:
        content["text"] = text
    req = {"method": method, "url": url, "cookies": cookies or [],
           "headers": [{"name": k, "value": v} for k, v in (headers or {}).items()],
           "queryString": []}
    if post is not None:
        req["postData"] = {"mimeType": "application/json", "text": post}
    return {"startedDateTime": "2026-09-21T00:00:00+00:00", "time": 1,
            "request": req,
            "response": {"status": status, "statusText": "OK",
                         "headers": [{"name": "content-type", "value": mime}],
                         "content": content, "redirectURL": ""},
            "cache": {}, "timings": {}}


def har(entries, creator="DevTools"):
    return json.dumps({"log": {"version": "1.2",
                               "creator": {"name": creator, "version": "1"},
                               "entries": entries}})


def write(tmpdir, name, text):
    path = os.path.join(tmpdir, name)
    with open(path, "w") as fh:
        fh.write(text)
    return path


# ---------------------------------------------------------------------------
# parsing and refusals
# ---------------------------------------------------------------------------

class Parsing(unittest.TestCase):

    def test_a_plain_har_parses(self):
        imp = parse_har(har([entry(text=api_body())]))
        self.assertEqual(len(imp.entries), 1)
        self.assertEqual(imp.entries[0].status, 200)
        self.assertEqual(imp.creator, "DevTools")

    def test_not_json_is_refused_with_a_readable_message(self):
        with self.assertRaises(HarError) as ctx:
            parse_har("<html>not a har</html>")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_json_that_is_not_a_har_is_refused(self):
        with self.assertRaises(HarError) as ctx:
            parse_har(json.dumps({"hello": "world"}))
        self.assertIn("does not look like a HAR", str(ctx.exception))

    def test_entries_must_be_a_list(self):
        with self.assertRaises(HarError):
            parse_har(json.dumps({"log": {"entries": "nope"}}))

    def test_an_empty_har_is_refused(self):
        with self.assertRaises(HarError):
            parse_har(har([]))

    def test_a_har_of_only_non_http_entries_is_refused(self):
        with self.assertRaises(HarError):
            parse_har(har([entry(url="data:text/html,<p>hi"),
                           entry(url="file:///etc/passwd"),
                           entry(url="ws://ex.test/socket")]))

    def test_non_http_schemes_are_skipped_not_followed(self):
        imp = parse_har(har([entry(text=api_body()),
                             entry(url="file:///etc/passwd", text="root:x:0:0"),
                             entry(url="data:text/html,<script>alert(1)</script>")]))
        self.assertEqual(len(imp.entries), 1)
        self.assertEqual(len(imp.skipped), 2)
        self.assertTrue(all("http(s)" in why for _u, why in imp.skipped))

    def test_a_malformed_entry_does_not_derail_the_file(self):
        raw = json.loads(har([entry(text=api_body())]))
        raw["log"]["entries"].insert(0, "this is not an object")
        raw["log"]["entries"].append({"request": None, "response": 42})
        imp = parse_har(json.dumps(raw))
        self.assertEqual(len(imp.entries), 1)


class Caps(unittest.TestCase):

    def test_a_file_over_the_cap_is_refused(self):
        with self.assertRaises(HarError) as ctx:
            parse_har("x" * (MAX_FILE_BYTES + 1))
        self.assertIn("cap", str(ctx.exception))

    def test_entries_beyond_the_cap_are_not_read(self):
        many = [entry(url=f"https://ex.test/a{i}", text="{}")
                for i in range(MAX_ENTRIES + 5)]
        imp = parse_har(har(many))
        self.assertEqual(len(imp.entries), MAX_ENTRIES)
        self.assertTrue(any("beyond the" in why for _u, why in imp.skipped))

    def test_an_oversized_body_is_dropped_not_truncated(self):
        big = "x" * (MAX_BODY_BYTES + 10)
        imp = parse_har(har([entry(text=big)]))
        self.assertIsNone(imp.entries[0].body)
        self.assertIn("dropped, not truncated", imp.entries[0].body_dropped)

    def test_a_base64_body_is_never_decoded(self):
        imp = parse_har(har([entry(text="AAAA", encoding="base64",
                                   mime="application/octet-stream")]))
        self.assertIsNone(imp.entries[0].body)
        self.assertIn("base64", imp.entries[0].body_dropped)

    def test_a_missing_body_says_so(self):
        raw = json.loads(har([entry()]))
        raw["log"]["entries"][0]["response"]["content"].pop("text", None)
        imp = parse_har(json.dumps(raw))
        self.assertIn("not capturing", imp.entries[0].body_dropped)


class CredentialsAreRemovedAndRefused(unittest.TestCase):

    def test_a_cookie_header_never_reaches_the_recording(self):
        imp = parse_har(har([entry(headers={"Cookie": "session=abc123"},
                                   text=api_body())]))
        blob = json.dumps(imp.entries[0].to_network())
        self.assertNotIn("abc123", blob)
        self.assertNotIn("cookie", blob.lower())

    def test_an_authorization_header_never_reaches_the_recording(self):
        imp = parse_har(har([entry(headers={"Authorization": "Bearer sk-secret"},
                                   text=api_body())]))
        self.assertNotIn("sk-secret", json.dumps(imp.entries[0].to_network()))

    def test_carrying_a_credential_is_recorded_as_a_refusal_not_a_strip(self):
        imp = parse_har(har([entry(headers={"Authorization": "Bearer x"},
                                   text=api_body())]))
        self.assertTrue(imp.entries[0].credential_bound)
        self.assertEqual(len(imp.credential_bound), 1)

    def test_a_cookies_array_keeps_the_request_and_only_the_names(self):
        imp = parse_har(har([entry(cookies=[{"name": "sid", "value": "cookie-value-41"}],
                                   text=api_body())]))
        self.assertEqual(imp.entries[0].credential_bound, "")
        self.assertEqual(imp.entries[0].sent_cookies, ["sid"])
        self.assertNotIn("cookie-value-41", json.dumps(imp.entries[0].__dict__))

    def test_a_cookie_header_keeps_the_request_as_a_candidate(self):
        imp = parse_har(har([entry(headers={"Cookie": "consent=yes-77; _ga=GA1.2.88"},
                                   cookies=[{"name": "consent", "value": "yes-77"},
                                            {"name": "_ga", "value": "GA1.2.88"}],
                                   text=api_body())], creator="Firefox"))
        e = imp.entries[0]
        self.assertEqual(e.credential_bound, "")
        self.assertEqual(e.sent_cookies, ["consent", "_ga"])
        self.assertEqual(imp.with_cookies, [e])
        self.assertEqual(imp.summary()["with_cookies"], 1)
        rec = har_to_recording(imp, "q", "alpha", ITEMS)
        self.assertEqual(len(rec["network"]), 1)
        blob = json.dumps(rec)
        for leaked in ("yes-77", "GA1.2.88", "consent", "_ga", "cookie_names"):
            self.assertNotIn(leaked, blob)

    def test_cookie_names_are_capped_and_values_never_read(self):
        many = "; ".join(f"c{i}=v{i}" for i in range(50))
        imp = parse_har(har([entry(headers={"Cookie": many + "; " + "n" * 500 + "=x"},
                                   cookies=[{"name": {"not": "a name"}, "value": "v"}, "junk"],
                                   text=api_body())]))
        self.assertEqual(imp.entries[0].sent_cookies, [f"c{i}" for i in range(20)])

    def test_a_cookie_name_is_cut_to_64_chars_and_one_with_a_control_char_is_skipped(self):
        imp = parse_har(har([entry(headers={"Cookie": "n" * 500 + "=x; ok=1"},
                                   cookies=[{"name": "evil\x1b]0;title\x07", "value": "v"},
                                            {"name": "line\nbreak", "value": "v"}],
                                   text=api_body())]))
        self.assertEqual(imp.entries[0].sent_cookies, ["n" * 64, "ok"])

    def test_a_cookie_does_not_excuse_another_credential(self):
        for extra in ({"Authorization": "Bearer x"}, {"X-CSRF-Token": "t"},
                      {"X-CSRFToken": "t"}, {"X-Api-Key": "k"}):
            imp = parse_har(har([entry(headers={"Cookie": "sid=1", **extra},
                                       text=api_body())]))
            self.assertTrue(imp.entries[0].credential_bound, extra)
            self.assertEqual(imp.with_cookies, [], extra)
        imp = parse_har(har([entry(url="https://ex.test/s?q=a&access_token=t",
                                   headers={"Cookie": "sid=1"}, text=api_body())]))
        self.assertTrue(imp.entries[0].credential_bound)

    def test_a_key_in_the_query_string_is_a_refusal(self):
        imp = parse_har(har([entry(url="https://ex.test/s?q=a&x-typesense-api-key=k",
                                   text=api_body())]))
        self.assertIn("api-key", imp.entries[0].credential_bound)
        self.assertNotIn("=k", imp.entries[0].url)
        self.assertIn("redacted", imp.entries[0].url)

    def test_query_credentials_are_redacted_from_entries_and_reports(self):
        secret = "super-secret-value"
        imp = parse_har(har([entry(
            url=f"https://ex.test/s?q=a&access_token={secret}", text=api_body())]))
        self.assertNotIn(secret, imp.entries[0].url)
        self.assertNotIn(secret, json.dumps(imp.entries[0].to_network()))
        self.assertNotIn(secret, json.dumps(imp.summary()))

    def test_an_x_api_key_header_never_reaches_the_recording(self):
        imp = parse_har(har([entry(headers={"X-Api-Key": "topsecret"},
                                   text=api_body())]))
        self.assertNotIn("topsecret", json.dumps(imp.entries[0].to_network()))

    def test_credential_bound_requests_are_left_out_of_the_recording(self):
        imp = parse_har(har([entry(headers={"Authorization": "Bearer b"}, text=api_body()),
                             entry(url="https://ex.test/open?q=alpha",
                                   text=api_body())]))
        rec = har_to_recording(imp, "q", "alpha", ITEMS)
        self.assertEqual(len(rec["network"]), 1)
        self.assertIn("/open", rec["network"][0]["url"])

    def test_an_ordinary_header_is_not_mistaken_for_a_credential(self):
        imp = parse_har(har([entry(headers={"Accept": "application/json",
                                            "Accept-Language": "en"},
                                   text=api_body())]))
        self.assertEqual(imp.entries[0].credential_bound, "")


class OriginsAreBounded(unittest.TestCase):

    def test_origins_lists_exactly_what_is_in_the_file(self):
        imp = parse_har(har([entry(url="https://a.test/x", text="{}"),
                             entry(url="https://b.test/y", text="{}"),
                             entry(url="https://a.test/z", text="{}")]))
        self.assertEqual(imp.origins(), ["https://a.test", "https://b.test"])

    def test_importing_opens_no_socket(self):
        """The strongest form of "no network": patch the socket module out."""
        import socket
        real = socket.socket

        def boom(*a, **k):                                 # pragma: no cover
            raise AssertionError("the HAR importer opened a socket")

        socket.socket = boom
        try:
            imp = parse_har(har([entry(text=api_body())]))
            har_to_recording(imp, "q", "alpha", ITEMS)
        finally:
            socket.socket = real
        self.assertEqual(len(imp.entries), 1)


class GroundTruthIsRequired(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = [write(self.tmp.name, f"c{i}.har",
                            har([entry(url=f"https://ex.test/api?q=v{i}",
                                       text=api_body())]))
                      for i in range(3)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_too_few_hars_is_refused_with_the_arithmetic_spelled_out(self):
        with self.assertRaises(HarError) as ctx:
            compile_from_har([HarCase(self.paths[0], "v0", ITEMS)])
        self.assertIn("at least 3", str(ctx.exception))

    def test_a_har_without_expected_results_is_refused(self):
        cases = [HarCase(p, f"v{i}", ITEMS if i else [])
                 for i, p in enumerate(self.paths)]
        with self.assertRaises(HarError) as ctx:
            compile_from_har(cases)
        self.assertIn("ground truth", str(ctx.exception))

    def test_an_all_credential_bound_har_is_refused_with_the_reason(self):
        path = write(self.tmp.name, "auth.har",
                     har([entry(headers={"Authorization": "Bearer x"},
                                text=api_body())]))
        cases = [HarCase(path, f"v{i}", ITEMS) for i in range(3)]
        with self.assertRaises(HarError) as ctx:
            compile_from_har(cases)
        msg = str(ctx.exception)
        self.assertIn("credential-bound", msg)
        self.assertIn("out of scope", msg)


class CompilesOffline(unittest.TestCase):
    """The whole path, with an injected transport, so no socket is opened."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sets = {"alpha": ITEMS,
                     "beta": ["Beta One", "Beta Two", "Beta Three", "Beta Four"],
                     "gamma": ["Gamma One", "Gamma Two", "Gamma Three"]}
        self.paths = {}
        for value, names in self.sets.items():
            self.paths[value] = write(
                self.tmp.name, f"{value}.har",
                har([entry(url=f"https://ex.test/api?q={value}",
                           text=api_body(names))]))

    def tearDown(self):
        self.tmp.cleanup()

    def _executor(self):
        from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
        import urllib.parse

        outer = self

        class T:
            calls = 0

            def send(self, method, url, headers, body, timeout_s):
                T.calls += 1
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
                names = outer.sets.get(q)
                if names is None:
                    return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   api_body(names))
        return ApiStepExecutor(transport=T()), T

    def test_it_compiles_and_the_proof_passes_on_unseen_values(self):
        executor, _T = self._executor()
        cases = [HarCase(self.paths[v], v, self.sets[v])
                 for v in ("alpha", "beta", "gamma")]
        compiled, proof, report = compile_from_har(cases, param_name="q",
                                                   executor=executor,
                                                   preflight=lambda _compiled: "")
        self.assertIsNotNone(compiled.template, compiled.reason)
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(proof.passed_count, 2)
        self.assertEqual(compiled.template.expect["min_items"], 3)
        self.assertEqual(report["origins"], ["https://ex.test"])
        self.assertEqual(report["template_origin"], "https://ex.test")
        self.assertTrue(report["origin_is_recorded"])

    def test_one_har_and_two_typed_cases_compile_and_prove(self):
        executor, T = self._executor()
        cases = [HarCase(self.paths["alpha"], "alpha", self.sets["alpha"]),
                 HarCase("", "beta", self.sets["beta"]), HarCase("", "gamma", self.sets["gamma"])]
        compiled, proof, report = compile_from_har(cases, param_name="q", executor=executor,
                                                   preflight=lambda _compiled: "")
        self.assertIsNotNone(compiled.template, compiled.reason)
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(proof.passed_count, 2)
        self.assertEqual([x.get("kind") for x in report["source"]][-2:], ["typed", "typed"])
        # a typed case whose labels are wrong fails the proof: the ground truth is the user's, not the tool's
        bad = [cases[0], HarCase("", "beta", ["Not", "What", "It", "Shows"]), cases[2]]
        _c, proof, _r = compile_from_har(bad, param_name="q", executor=executor, preflight=lambda _compiled: "")
        self.assertFalse(proof.passed)

    def test_the_first_case_must_be_a_har(self):
        with self.assertRaises(HarError) as ctx:
            compile_from_har([HarCase("", "alpha", self.sets["alpha"]),
                              HarCase(self.paths["beta"], "beta", self.sets["beta"]),
                              HarCase("", "gamma", self.sets["gamma"])])
        self.assertIn("needs a HAR", str(ctx.exception))

    def test_a_template_is_never_emitted_for_an_unrecorded_origin(self):
        executor, _T = self._executor()
        cases = [HarCase(self.paths[v], v, self.sets[v])
                 for v in ("alpha", "beta", "gamma")]
        compiled, _proof, _report = compile_from_har(cases, param_name="q",
                                                     executor=executor,
                                                     preflight=lambda _compiled: "")
        self.assertTrue(compiled.template.url.startswith("https://ex.test/"))

    def test_a_wrong_expected_list_fails_the_proof_rather_than_passing_quietly(self):
        executor, _T = self._executor()
        cases = [HarCase(self.paths["alpha"], "alpha", self.sets["alpha"]),
                 HarCase(self.paths["beta"], "beta",
                         ["Something Else", "Entirely Different", "Not This"]),
                 HarCase(self.paths["gamma"], "gamma",
                         ["Also Wrong", "Still Wrong", "Wrong Again"])]
        _compiled, proof, report = compile_from_har(cases, param_name="q",
                                                    executor=executor,
                                                    preflight=lambda _compiled: "")
        self.assertFalse(proof.passed)
        self.assertNotIn("needs_sign_in", report)     # no cookies were recorded


class CookiesAreJudgedByTheProof(unittest.TestCase):
    """A HAR exported with cookies (Firefox, Safari, Chrome "with sensitive data")
    compiles; the proof replays without the cookies and decides."""

    COOKIE = "consent=yes-77; _ga=GA1.2.88; sessionid=sess-secret-9"
    SECRETS = ("yes-77", "GA1.2.88", "sess-secret-9")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sets = {"alpha": ITEMS,
                     "beta": ["Beta One", "Beta Two", "Beta Three", "Beta Four"],
                     "gamma": ["Gamma One", "Gamma Two", "Gamma Three"]}
        jar = [{"name": p.split("=")[0].strip(), "value": p.split("=")[1]}
               for p in self.COOKIE.split(";")]
        self.path = write(self.tmp.name, "firefox.har", har([
            entry(url="https://ex.test/search?q=alpha", mime="text/html",
                  text="<html><body><p>results</p></body></html>",
                  headers={"Cookie": self.COOKIE}, cookies=jar),
            entry(url="https://ex.test/api?q=alpha", text=api_body(ITEMS),
                  headers={"Cookie": self.COOKIE, "Accept": "application/json"},
                  cookies=jar)], creator="Firefox"))
        self.fresh_host_files()

    def fresh_host_files(self):
        """A new, empty do-not-auto-retry and browser-only registry."""
        from unittest import mock
        d = tempfile.mkdtemp(dir=self.tmp.name)
        patch = mock.patch.dict(os.environ, {
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(d, "blocked.json"),
            "ANYUSE_BROWSER_ONLY_HOSTS_FILE": os.path.join(d, "browser_only.json")})
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, needs_cookie=False, respond=None):
        """``respond(q, sent_count)`` answers a proof replay instead, when given."""
        from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
        import urllib.parse
        sets, sent = self.sets, []

        class T:
            def send(self, method, url, headers, body, timeout_s):
                sent.append(dict(headers or {}))
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
                if respond is not None:
                    return respond(q, len(sent))
                if needs_cookie and not any(k.lower() == "cookie" for k in (headers or {})):
                    return ApiResponse(401, {"Content-Type": "application/json"},
                                       '{"error": "sign in required"}')
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   api_body(sets[q]))
        cases = [HarCase(self.path, "alpha", ITEMS), HarCase("", "beta", self.sets["beta"]),
                 HarCase("", "gamma", self.sets["gamma"])]
        compiled, proof, report = compile_from_har(
            cases, param_name="q", executor=ApiStepExecutor(transport=T()),
            preflight=lambda _compiled: "")
        return compiled, proof, report, sent

    def test_a_har_exported_with_cookies_compiles_and_the_proof_sends_none(self):
        compiled, proof, report, sent = self._run(needs_cookie=False)
        self.assertIsNotNone(compiled.template, compiled.reason)
        self.assertTrue(proof.passed, proof.reason)
        # the two proof replays and the made-up searches that learn the site's empty page
        from anyuse.execution import empty_page as EP
        self.assertEqual(len(sent), 2 + EP.PROBES)
        for headers in sent:
            self.assertFalse([k for k in headers if k.lower() == "cookie"], headers)
        template = json.dumps(compiled.template.to_dict())
        self.assertNotIn("cookie_refs", template)
        for secret in self.SECRETS + ("sessionid",):
            self.assertNotIn(secret, template)
        self.assertEqual(report["source"][0]["with_cookies"], 2)
        self.assertEqual(report["cookie_requests"][1]["cookies"], ["consent", "_ga", "sessionid"])
        self.assertNotIn("needs_sign_in", report)
        for secret in self.SECRETS:
            self.assertNotIn(secret, json.dumps(report, default=str))

    def test_a_request_that_needs_its_cookies_fails_the_proof_and_says_sign_in(self):
        compiled, proof, report, sent = self._run(needs_cookie=True)
        self.assertIsNotNone(compiled.template, compiled.reason)
        self.assertFalse(proof.passed)
        self.assertTrue(sent)
        for headers in sent:
            self.assertFalse([k for k in headers if k.lower() == "cookie"], headers)
        hint = report["needs_sign_in"]
        self.assertIn("consent, _ga, sessionid", hint)
        self.assertIn("signed in", hint)
        self.assertNotIn("403", hint)
        for secret in self.SECRETS:
            self.assertNotIn(secret, json.dumps(report, default=str))

    def test_the_sign_in_hint_needs_an_answer_that_can_mean_cookies(self):
        # a refused connection, a 5xx, a CAPTCHA or a rate limit says nothing about
        # cookies; a 401, a redirect, a bare 403 or a wrong 200 can
        from anyuse.execution.api_step import ApiResponse
        js = {"Content-Type": "application/json"}
        captcha = ("<html><script src='https://www.google.com/recaptcha/api.js'></script>"
                   "<div class='g-recaptcha'></div></html>")

        def refused(q, n):
            raise ConnectionError("connection refused")

        def first_ok_then_refused(q, n):
            if n == 1:
                return ApiResponse(200, js, api_body(self.sets[q]))
            raise ConnectionError("connection refused")
        modes = {
            "401": (True, lambda q, n: ApiResponse(401, js, '{"error": "sign in"}')),
            "302 to login": (True, lambda q, n: ApiResponse(302, {"Location": "/login"}, "")),
            "403": (True, lambda q, n: ApiResponse(403, js, '{"error": "forbidden"}')),
            "empty 200": (True, lambda q, n: ApiResponse(200, js, api_body([]))),
            "other results": (True, lambda q, n: ApiResponse(
                200, js, api_body(["Zed One", "Zed Two", "Zed Three"]))),
            "503": (False, lambda q, n: ApiResponse(503, {"Content-Type": "text/plain"}, "down")),
            "429": (False, lambda q, n: ApiResponse(429, {"Content-Type": "text/plain"}, "slow")),
            "captcha": (False, lambda q, n: ApiResponse(200, {"Content-Type": "text/html"},
                                                        captcha)),
            "refused connection": (False, refused),
            "one ok, one refused": (False, first_ok_then_refused),
        }
        for mode, (hint, respond) in modes.items():
            self.fresh_host_files()
            _compiled, proof, report, sent = self._run(respond=respond)
            self.assertFalse(proof.passed, mode)
            self.assertTrue(sent, mode)
            self.assertEqual("needs_sign_in" in report, hint, (mode, proof.reason))

    def test_a_403_hint_says_until_when_nothing_is_sent(self):
        from anyuse.execution.api_step import ApiResponse
        from anyuse.execution.blocked import blocked_until
        _compiled, proof, report, sent = self._run(respond=lambda q, n: ApiResponse(
            403, {"Content-Type": "application/json"}, '{"error": "forbidden"}'))
        self.assertFalse(proof.passed)
        self.assertEqual(len(sent), 1)
        held = blocked_until("ex.test")
        self.assertIsNotNone(held)
        hint = report["needs_sign_in"]
        self.assertIn(f"sends nothing more to ex.test until {held['until']}; attach after that",
                      hint)

    def test_the_hint_matches_a_template_with_a_path_parameter(self):
        from types import SimpleNamespace
        from anyuse.execution.har_import import cookies_sent_with
        imp = parse_har(har([
            entry(url="https://ex.test/api/crates/serde", headers={"Cookie": "sid=1"},
                  text=api_body()),
            entry(url="https://ex.test/other", headers={"Cookie": "zz=1"}, text="{}")]))
        def t(url, method="GET"):
            return SimpleNamespace(url=url, method=method)
        self.assertEqual(cookies_sent_with(t("https://ex.test/api/crates/{q}"), imp), ["sid"])
        self.assertEqual(cookies_sent_with(t("https://ex.test/api/crates/{q}", "POST"), imp), [])
        self.assertEqual(cookies_sent_with(t("https://other.test/api/crates/{q}"), imp), [])
        plan = SimpleNamespace(steps=[SimpleNamespace(template=t("https://ex.test/other"))])
        self.assertEqual(cookies_sent_with(plan, imp), ["zz"])


class DetailPageFromHar(unittest.TestCase):
    """A HAR of an item's page, with only the values the page showed: each value is named from the HAR's own
    response (or, for a typed case, by its order on the page) and the page is proved."""

    ITEMS = {"alpha": {"title": "Alpha Package", "summary": "parses alpha files", "version": "2.4.1"},
             "beta": {"title": "Beta Package", "summary": "renders beta charts", "version": "0.9.7"},
             "gamma": {"title": "Gamma Package", "summary": "streams gamma rays", "version": "5.0.2"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = {v: write(self.tmp.name, f"{v}.har",
                               har([entry(url=f"https://ex.test/api/package?q={v}",
                                          text=json.dumps({"item": item}))]))
                      for v, item in self.ITEMS.items()}

    def tearDown(self):
        self.tmp.cleanup()

    def shown(self, v):
        item = self.ITEMS[v]
        return [item["title"], item["summary"], item["version"]]

    def executor(self):
        from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
        import urllib.parse
        items = self.ITEMS

        class T:
            def send(self, method, url, headers, body, timeout_s):
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
                if q not in items:
                    return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   json.dumps({"item": items[q]}))
        return ApiStepExecutor(transport=T())

    def compile(self, cases):
        return compile_from_har(cases, param_name="q", executor=self.executor(),
                                preflight=lambda _compiled: "")

    def test_three_hars_of_an_item_page_compile_and_prove(self):
        compiled, proof, _report = self.compile([HarCase(self.paths[v], v, self.shown(v))
                                                 for v in ("alpha", "beta", "gamma")])
        self.assertEqual(compiled.template.shape, "fields", compiled.reason)
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual({tuple(sorted(set(c.contract["fields_named_by"].values()))) for c in proof.cases},
                         {("json_key",)})
        self.assertNotIn("requires named ui_fields", proof.reason)

    def test_typed_cases_of_an_item_page_are_named_by_their_order(self):
        cases = [HarCase(self.paths["alpha"], "alpha", self.shown("alpha")),
                 HarCase("", "beta", self.shown("beta")), HarCase("", "gamma", self.shown("gamma"))]
        _compiled, proof, _report = self.compile(cases)
        self.assertTrue(proof.passed, proof.reason)
        self.assertTrue(all(set(c.contract["fields_named_by"].values()) == {"page_order"}
                            for c in proof.cases))
        # typed in another order than the page showed: a field meaning mismatch, never a pass
        beta = self.shown("beta")
        cases[1] = HarCase("", "beta", [beta[1], beta[0], beta[2]])
        _compiled, proof, _report = self.compile(cases)
        self.assertFalse(proof.passed)
        self.assertIn("field meaning mismatch", proof.reason)


class PageKeysAndRedirects(unittest.TestCase):
    """A provider key the site's own page stated is kept as a locator, never as a value."""

    PAGE = ('<html><script>window.cfg={"appId":"APP12345","searchKey":"pk-live-1234567"}'
            '</script></html>')

    def test_a_key_the_sites_page_stated_is_kept_with_its_locator(self):
        imp = parse_har(har([
            entry("https://shop.test/", mime="text/html", text=self.PAGE),
            entry("https://search.prov.test/1/q?query=alpha&x-typesense-api-key=pk-live-1234567"
                  "&app=APP12345", text=api_body()),
        ]))
        provider = imp.entries[1]
        self.assertEqual(provider.credential_bound, "")
        net = provider.to_network()
        self.assertEqual([k["at"] for k in net["page_keys"]], ["query:x-typesense-api-key"])
        self.assertIn("x-typesense-api-key=%3Credacted%3E", net["url"])
        rec = har_to_recording(imp, "q", "alpha", ITEMS)
        self.assertEqual(len(rec["network"]), 2)
        self.assertNotIn("pk-live-1234567", json.dumps(rec))
        self.assertIn("<page value>", rec["network"][0]["body"])

    def test_the_key_leaves_another_requests_url_too(self):
        imp = parse_har(har([
            entry("https://shop.test/", mime="text/html", text=self.PAGE),
            entry("https://shop.test/b?k=pk-live-1234567&e=view", mime="image/gif", text=""),
            entry("https://search.prov.test/1/q?query=alpha&x-typesense-api-key=pk-live-1234567",
                  text=api_body()),
        ]))
        self.assertTrue(imp.entries[2].to_network()["page_keys"])
        self.assertNotIn("pk-live-1234567", json.dumps([e.to_network() for e in imp.entries]))

    def test_a_key_no_page_stated_stays_a_refusal(self):
        imp = parse_har(har([
            entry("https://shop.test/", mime="text/html", text="<html>no key</html>"),
            entry("https://search.prov.test/1/q?query=alpha&x-typesense-api-key=pk-live-1234567",
                  text=api_body()),
        ]))
        self.assertIn("api-key", imp.entries[1].credential_bound)
        self.assertNotIn("page_keys", imp.entries[1].to_network())

    def test_a_redirect_keeps_its_hops(self):
        hop = entry("https://shop.test/search?q=nike", status=302, mime="text/html", text="")
        hop["response"]["redirectURL"] = "/collections/nike"
        imp = parse_har(har([hop, entry("https://shop.test/collections/nike", mime="text/html",
                                        text="<html>landing</html>")]))
        self.assertEqual(imp.entries[0].to_network()["redirect_url"],
                         "https://shop.test/collections/nike")
        self.assertEqual(imp.entries[1].to_network()["redirected_from"],
                         ["https://shop.test/search?q=nike"])


class RealBrowserHar(unittest.TestCase):
    """The same path on a HAR a real browser produced, when one is present."""

    #: A directory of real HARs, if you have one. Never a path in the tree.
    DIR = os.environ.get("ANYUSE_TEST_HAR_DIR", "")

    def test_a_real_har_parses_and_yields_the_api_call(self):
        path = os.path.join(self.DIR, "crates_serde.har") if self.DIR else ""
        if not path or not os.path.exists(path):
            self.skipTest("no recorded HAR: set ANYUSE_TEST_HAR_DIR to a directory "
                          "holding crates_serde.har")
        imp = load_har(path)
        self.assertGreater(len(imp.entries), 10)
        self.assertEqual(imp.credential_bound, [])
        api = [e for e in imp.entries if "/api/v1/crates" in e.url and e.body]
        self.assertTrue(api, "the crates.io search API call is not in this HAR")
        self.assertIn("crates", json.loads(api[0].body))


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
