"""The learn loop, confirm-once: the agent finds the page once, a person confirms
it once, AnyUse substitutes, proves and persists.

Milestone 7. Every test here injects a fake agent, a fake navigator and a fake
transport: this file opens no socket, starts no browser and makes no model call.

The tests that matter most are the ones where the agent was WRONG, where the
person said NO, and where a later page is NOT the confirmed page. A loop that
learns from any of those is worse than no loop.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from anyuse.execution.agent_browser import (AgentRun, NotAContainerError,
                                            assert_container_endpoint,
                                            parse_action, task_text)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.learn_loop import (MIN_VALUES, Candidate, continue_learning,
                                         candidates_dir, cleanup_candidates,
                                         explicit_confirmation, landing_check,
                                         learn, load_candidate, page_title,
                                         preview_items, propose, save_candidate,
                                         title_matches, url_template_for,
                                         usable_runs)


def page(rows, value, title=None, extra=""):
    """A list page that ANSWERS the value: its rows mention it."""
    items = "".join(f'<li class="row"><a>{r}</a></li>' for r in rows)
    title = title if title is not None else f"Search: {value} - ex.test"
    return (f"<html><head><title>{title}</title></head><body>"
            f'<nav><a>Home</a><a>About</a></nav>'
            f'<ul class="results">{items}</ul>{extra}</body></html>')


def api_body(rows):
    import json as _json
    return _json.dumps({"hits": [{"name": r} for r in rows]})


def run_for(value, rows, url=None, dom=None, text=None, error="", title=None,
            model_calls=3, steps=None):
    url = url if url is not None else f"https://ex.test/search?q={value}"
    d = dom if dom is not None else page(rows, value, title=title)
    return AgentRun(
        value=value, task=f"search for {value}", start_url="https://ex.test/",
        final_url=url, dom=d,
        text=text if text is not None else (value + " " + " ".join(rows)),
        network=[{"url": url, "method": "GET", "status": 200,
                  "mime": "text/html", "body": d, "bytes": len(d)},
                 {"url": f"https://ex.test/api?q={value}", "method": "GET",
                  "status": 200, "mime": "application/json",
                  "body": api_body(rows), "bytes": 100}],
        steps=steps if steps is not None else [
            {"action": "type", "index": 0, "text": value},
            {"action": "submit", "index": 0}, {"action": "done"}],
        typed=[value], model_calls=model_calls,
        tokens_in=900 if model_calls else 0, tokens_out=40 if model_calls else 0,
        seconds=9.1 if model_calls else 4.2, error=error)


def nav_for(value, rows, url=None, **kw):
    """What the NAVIGATOR returns: the same page, opened by URL, 0 model calls."""
    return run_for(value, rows, url=url, model_calls=0,
                   steps=[{"action": "navigate",
                           "url": url or f"https://ex.test/search?q={value}"}], **kw)


class FakeAgent:
    def __init__(self, runs):
        self.by_value = {r.value: r for r in runs}
        self.calls = []

    def run(self, task, start_url, value):
        self.calls.append(value)
        return self.by_value[value]


class FakeNavigator:
    def __init__(self, runs):
        self.by_value = {r.value: r for r in runs}
        self.urls = []

    def __call__(self, url, value):
        self.urls.append(url)
        return self.by_value[value]


class RecordedTransport:
    """Answers a compiled template from the recorded responses."""

    def __init__(self, recordings):
        import urllib.parse
        self.index = {}
        for rec in recordings:
            for req in rec.get("network") or []:
                sp = urllib.parse.urlsplit(req["url"])
                self.index.setdefault((sp.netloc, sp.path), []).append(req)
        self.calls = 0

    def send(self, method, url, headers, body, timeout_s):
        import urllib.parse
        self.calls += 1
        sp = urllib.parse.urlsplit(url)
        wanted = urllib.parse.parse_qs(sp.query)
        best, best_score = None, -1
        for req in self.index.get((sp.netloc, sp.path), []):
            got = urllib.parse.parse_qs(urllib.parse.urlsplit(req["url"]).query)
            score = sum(1 for k, v in wanted.items() if got.get(k) == v)
            if score > best_score:
                best, best_score = req, score
        if best is None or best_score < len(wanted):
            return ApiResponse(599, {"Content-Type": "text/plain"}, "no recorded response")
        return ApiResponse(int(best["status"]),
                           {"Content-Type": best.get("mime") or "application/json"},
                           best.get("body") or "")


class DeadTransport:
    """Every compiled request fails on the wire — tier 1 cannot prove anything."""

    def send(self, method, url, headers, body, timeout_s):
        return ApiResponse(503, {"Content-Type": "text/plain"}, "upstream is down")


SERDE = run_for("serde", ["serde v1", "serde_json v1", "serde_derive v1"])
REGEX = nav_for("regex", ["regex v1", "regex-lite v0", "regex-automata v0"])
TOKIO = nav_for("tokio", ["tokio v1", "tokio-util v0", "tokio-stream v0"])
YES = lambda cand: True    # noqa: E731
NO = lambda cand: False    # noqa: E731


def executor_for(runs):
    recordings = [r.as_recording("wf", "q", f"x{i}") for i, r in enumerate(runs)]
    return ApiStepExecutor(transport=RecordedTransport(recordings))


def do_learn(first, others, confirm=YES, executor=None, navigator=None, agent_runs=(),
             **kw):
    agent = FakeAgent([first, *agent_runs])
    nav = navigator if navigator is not None else FakeNavigator(others)
    if executor is None:
        executor = executor_for([first, *others])
    kw.setdefault("proof_preflight", lambda _compiled: "")
    res = learn("search this site for {q}", "https://ex.test/",
                [first.value, *[o.value for o in others]], workflow="wf", param="q",
                agent=agent, navigator=nav, executor=executor, confirm=confirm,
                confirmed_by="test", **kw)
    return res, agent, nav


# ---------------------------------------------------------------------------
# the container guard (unchanged from milestone 6)
# ---------------------------------------------------------------------------

class ContainerGuard(unittest.TestCase):

    def test_no_endpoint_is_refused(self):
        for ep in ("", "   "):
            with self.subTest(endpoint=ep):
                opener = mock.Mock()
                with self.assertRaises(NotAContainerError) as cm:
                    assert_container_endpoint(ep, opener=opener)
                self.assertIn("never be pointed at a browser somebody is using",
                              str(cm.exception))
                opener.assert_not_called()

    def test_a_loopback_endpoint_is_refused(self):
        for ep in ("http://127.0.0.1:9222", "http://localhost:9222",
                   "http://0.0.0.0:9222", "http://127.0.0.2:9222",
                   "http://[::1]:9222", "http://[::]:9222",
                   "http://localhost.:9222", "http://CHROME.LOCALHOST.:9222",
                   "http://[::1%25lo0]:9222", "http://[::ffff:127.0.0.1]:9222"):
            with self.subTest(endpoint=ep):
                opener = mock.Mock()
                with self.assertRaises(NotAContainerError) as cm:
                    assert_container_endpoint(ep, opener=opener)
                self.assertIn("loopback", str(cm.exception))
                opener.assert_not_called()

    def test_hostnames_resolving_to_loopback_are_refused_before_opening(self):
        import socket

        for address in ("127.0.0.1", "::1", "0.0.0.0", "::", "::ffff:127.0.0.1"):
            with self.subTest(address=address):
                resolver = mock.Mock(return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.64.5", 9222)),
                    (socket.AF_INET6 if ":" in address else socket.AF_INET,
                     socket.SOCK_STREAM, 6, "", (address, 9222)),
                ])
                opener = mock.Mock()
                with self.assertRaisesRegex(NotAContainerError, "loopback"):
                    assert_container_endpoint("http://chrome.example:9222", opener=opener,
                                              resolver=resolver)
                resolver.assert_called_once_with("chrome.example", 9222, type=socket.SOCK_STREAM)
                opener.assert_not_called()

    def test_unresolvable_endpoint_is_refused_before_opening(self):
        import socket

        for result in ([], socket.gaierror("no address")):
            resolver = mock.Mock()
            if isinstance(result, Exception):
                resolver.side_effect = result
            else:
                resolver.return_value = result
            opener = mock.Mock()
            with self.assertRaisesRegex(NotAContainerError, "could not resolve"):
                assert_container_endpoint("http://chrome.example:9222", opener=opener,
                                          resolver=resolver)
            opener.assert_not_called()

    def test_hostname_resolving_to_container_is_accepted(self):
        import socket

        resolver = mock.Mock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.64.5", 9222))])
        opener = self._opener({"Browser": "Chrome/151.0.7922.47"},
                              [{"type": "page", "url": "about:blank"}])
        info = assert_container_endpoint("http://container.example:9222", opener=opener,
                                         resolver=resolver)
        self.assertIn("no pages open", info["note"])

    def test_invalid_endpoint_is_refused_before_opening(self):
        for ep in ("http://[::1", "http://10.0.0.2:99999", "http://10.0.0.2:0",
                   "http://10.0.0.2:-1", "http://10.0.0.2:nope", "ftp://10.0.0.2:9222",
                   "http://:9222", "10.0.0.2:9222"):
            with self.subTest(endpoint=ep):
                opener = mock.Mock()
                with self.assertRaises(NotAContainerError) as cm:
                    assert_container_endpoint(ep, opener=opener)
                self.assertIn("invalid URL", str(cm.exception))
                opener.assert_not_called()

    def _opener(self, version, targets=None):
        import io
        import json as _json

        def opener(url):
            if url.endswith("/json/list"):
                return io.StringIO(_json.dumps(targets or []))
            return io.StringIO(_json.dumps(version))

        return opener

    def test_a_browser_with_pages_open_is_refused(self):
        opener = self._opener(
            {"Browser": "Chrome/151.0.0.0"},
            [{"type": "page", "url": "https://mail.example.com/inbox"}])
        with self.assertRaises(NotAContainerError) as cm:
            assert_container_endpoint("http://192.168.64.5:9222", opener=opener)
        self.assertIn("page(s) open", str(cm.exception))

    def test_a_browser_with_nothing_open_is_accepted_with_a_note(self):
        opener = self._opener({"Browser": "Chrome/151.0.7922.47"},
                              [{"type": "page", "url": "about:blank"}])
        info = assert_container_endpoint("http://192.168.64.5:9222", opener=opener)
        self.assertFalse(info["headless_declared"])
        self.assertIn("no pages open", info["note"])


# ---------------------------------------------------------------------------
# the candidate: what a person is shown
# ---------------------------------------------------------------------------

class TheCandidate(unittest.TestCase):

    def test_propose_runs_agent_once_without_persisting_a_runnable_artifact(self):
        agent = FakeAgent([SERDE])
        cand = propose("search this site for {q}", "https://ex.test/", "serde",
                       workflow="wf", agent=agent)
        self.assertEqual(agent.calls, ["serde"])
        self.assertTrue(cand.ok)
        self.assertEqual(cand.final_url, "https://ex.test/search?q=serde")
        self.assertEqual(cand.title, "Search: serde - ex.test")
        self.assertEqual(cand.url_template, "https://ex.test/search?q={q}")
        self.assertIn("query parameter 'q'", cand.binding)
        self.assertEqual(cand.preview[:2], ["serde v1", "serde_json v1"])
        self.assertIn("not yet shown to change", cand.preview_note)
        self.assertEqual(cand.model_calls, 3)
        self.assertGreater(cand.cost_usd, 0)
        self.assertIn("do not persist themselves", cand.summary()["persisted"])

    def test_a_value_in_the_path_is_bound_as_a_path_segment(self):
        tmpl, pp, tr, how = url_template_for("https://ex.test/pkg/ring/ring-core",
                                             "ring/ring-core", "artifact")
        self.assertEqual(tmpl, "https://ex.test/pkg/{artifact}")
        self.assertEqual(pp, {"artifact": 2})
        self.assertIn("path segments", how)

    def test_a_value_absent_from_the_url_yields_no_pattern(self):
        tmpl, _pp, _tr, how = url_template_for("https://ex.test/results", "serde", "q")
        self.assertEqual(tmpl, "")
        self.assertIn("agent is needed", how)

    def test_the_preview_is_the_largest_repeated_group(self):
        sel, items = preview_items(page(["a one", "b two", "c three"], "x"))
        self.assertTrue(sel)
        self.assertEqual(items, ["a one", "b two", "c three"])
        self.assertEqual(preview_items("<html><body><p>nothing</p></body></html>"),
                         ("", []))

    def test_page_title_is_read_and_unescaped(self):
        self.assertEqual(page_title("<html><head><title> A &amp; B </title></head></html>"),
                         "A & B")
        self.assertEqual(page_title("<html></html>"), "")

    def test_a_candidate_round_trips_through_disk(self):
        cand = propose("t {q}", "https://ex.test/", "serde", workflow="wf",
                       agent=FakeAgent([SERDE]))
        with tempfile.TemporaryDirectory() as tmp:
            path = save_candidate(cand, tmp)
            self.assertTrue(os.path.exists(path))
            back = load_candidate(cand.candidate_id, tmp)
            self.assertEqual(back.final_url, cand.final_url)
            self.assertEqual(back.recording["dom"], cand.recording["dom"])
            self.assertIsNone(load_candidate("nope", tmp))
            self.assertIsNone(load_candidate("../../etc", tmp))


# ---------------------------------------------------------------------------
# the confirmation gate
# ---------------------------------------------------------------------------

class TheConfirmation(unittest.TestCase):

    def test_without_a_confirmation_nothing_is_learned_or_persisted(self):
        seen = {}
        res, agent, nav = do_learn(SERDE, [REGEX, TOKIO], confirm=None,
                                   persist=lambda *a: seen.setdefault("called", True))
        self.assertEqual(res.outcome, "unconfirmed")
        self.assertIn("no runnable artifact was persisted", res.reason)
        self.assertEqual(agent.calls, ["serde"], "the agent ran exactly once")
        self.assertEqual(nav.urls, [], "no further value was fetched")
        self.assertNotIn("called", seen)
        self.assertIsNone(res.persisted)
        self.assertEqual(res.candidate["final_url"], "https://ex.test/search?q=serde")

    def test_a_refused_candidate_learns_nothing(self):
        res, agent, nav = do_learn(SERDE, [REGEX, TOKIO], confirm=NO)
        self.assertEqual(res.outcome, "nothing")
        self.assertIn("NOT confirmed", res.reason)
        self.assertEqual(nav.urls, [])
        self.assertEqual(res.agent_runs, 1)

    def test_an_explicit_confirmation_must_restate_the_url(self):
        cand = propose("t {q}", "https://ex.test/", "serde", workflow="wf",
                       agent=FakeAgent([SERDE]))
        self.assertFalse(explicit_confirmation(cand, "")[0])
        self.assertFalse(explicit_confirmation(cand, "https://ex.test/search?q=regex")[0])
        self.assertFalse(explicit_confirmation(cand, "https://ex.test/")[0])
        self.assertTrue(explicit_confirmation(cand, "https://ex.test/search?q=serde")[0])

    def test_a_candidate_the_agent_could_not_produce_stops_before_asking(self):
        broken = run_for("serde", [], dom="", error="TimeoutError")
        asked = []
        res, _agent, _nav = do_learn(broken, [REGEX, TOKIO],
                                     confirm=lambda c: asked.append(c) or True)
        self.assertEqual(res.outcome, "nothing")
        self.assertEqual(asked, [], "nobody is asked to confirm nothing")
        self.assertIn("no candidate", res.reason)


# ---------------------------------------------------------------------------
# after confirmation: substitution, then the agent only where needed
# ---------------------------------------------------------------------------

class Learns(unittest.TestCase):

    def test_confirmed_then_substituted_reaches_tier_1_with_one_agent_run(self):
        res, agent, nav = do_learn(SERDE, [REGEX, TOKIO])
        self.assertEqual(res.outcome, "tier1", res.reason)
        self.assertEqual(agent.calls, ["serde"], "the agent ran ONCE")
        self.assertEqual(nav.urls, ["https://ex.test/search?q=regex",
                                    "https://ex.test/search?q=tokio"])
        self.assertEqual(res.agent_runs, 1)
        self.assertEqual(res.navigations, 2)
        self.assertEqual(res.model_calls, 3)
        self.assertEqual(res.confirmed_by, "test")
        self.assertEqual(res.landing_check, "url_pattern+title")
        self.assertEqual(res.later_run_model_calls, 0)
        self.assertTrue(res.proof["passed"])
        self.assertAlmostEqual(res.first_run_ms, 9100.0, places=0)

    def test_the_agent_is_used_only_where_the_value_is_not_in_the_url(self):
        # The confirmed page's URL does not carry the value (a POST search
        # rendered at /results): nothing to substitute into, so the agent runs
        # for the other values — and must still land on /results with a title
        # naming its value.
        first = run_for("serde", ["serde v1", "serde_json v1", "serde_derive v1"],
                        url="https://ex.test/results")
        others = [run_for("regex", ["regex v1", "regex-lite v0", "regex-automata v0"],
                          url="https://ex.test/results"),
                  run_for("tokio", ["tokio v1", "tokio-util v0", "tokio-stream v0"],
                          url="https://ex.test/results")]
        res, agent, nav = do_learn(first, others, agent_runs=others,
                                   executor=ApiStepExecutor(transport=DeadTransport()))
        self.assertEqual(agent.calls, ["serde", "regex", "tokio"])
        self.assertEqual(nav.urls, [], "nothing to substitute into")
        self.assertEqual(res.agent_runs, 3)
        self.assertEqual(res.navigations, 0)
        self.assertEqual(res.discarded, [], "same path, title names each value")

    def test_the_cost_of_learning_counts_the_agents_own_first_run(self):
        from anyuse.execution.learn_loop import propose
        agent = FakeAgent([SERDE])
        cand = propose("search this site for {q}", "https://ex.test/", SERDE.value, workflow="wf",
                       param="q", agent=agent)
        res = continue_learning(cand, [REGEX.value, TOKIO.value], agent=agent,
                                navigator=FakeNavigator([REGEX, TOKIO]),
                                executor=executor_for([SERDE, REGEX, TOKIO]), confirmed_by="test",
                                proof_preflight=lambda _c: "")
        self.assertGreater(cand.agent_seconds, 9.0)
        self.assertGreaterEqual(res.learn_seconds, cand.agent_seconds)

    def test_a_learned_tool_gets_a_short_readable_name_and_never_replaces_another(self):
        from unittest import mock
        from anyuse.execution import learn_loop as LL
        self.assertEqual(LL._workflow_name("https://crates.io", "search crates.io for {q}"),
                         "crates_io_search")
        self.assertEqual(LL._workflow_name("https://www.pypi.org/", "search this site for {q}"),
                         "pypi_org_search")
        same = {"source": {"learned_from": {"task": "t", "start_url": "u"}}}
        other = {"source": {"learned_from": {"task": "other", "start_url": "u"}}}
        with mock.patch("anyuse.mcpserver.compiled_tools._find_record",
                        lambda name: {"crates_io_search": other}.get(name)):
            self.assertEqual(LL._free_name("crates_io_search", "t", "u"), "crates_io_search_2")
        with mock.patch("anyuse.mcpserver.compiled_tools._find_record",
                        lambda name: {"crates_io_search": same}.get(name)):
            self.assertEqual(LL._free_name("crates_io_search", "t", "u"), "crates_io_search")

    def test_the_learning_cost_is_the_providers_reported_cost(self):
        # three agent runs that report what the provider billed; the cost line and
        # the --budget-usd check use it, not a list price for another model
        first = run_for("serde", ["serde v1", "serde_json v1", "serde_derive v1"],
                        url="https://ex.test/results")
        others = [run_for("regex", ["regex v1", "regex-lite v0", "regex-automata v0"],
                          url="https://ex.test/results"),
                  run_for("tokio", ["tokio v1", "tokio-util v0", "tokio-stream v0"],
                          url="https://ex.test/results")]
        for run, usd in zip([first, *others], (0.0173, 0.0151, 0.0149)):
            run.cost_usd = usd
        res, _agent, _nav = do_learn(first, others, agent_runs=others,
                                     executor=ApiStepExecutor(transport=DeadTransport()))
        self.assertAlmostEqual(res.cost_usd, 0.0473, places=4)
        res, agent, _nav = do_learn(first, others, agent_runs=others, budget_usd=0.03,
                                    executor=ApiStepExecutor(transport=DeadTransport()))
        self.assertEqual(agent.calls, ["serde", "regex"])            # stopped once $0.03 was reached
        self.assertIn("learning budget", res.reason)

    def test_the_persister_is_the_only_thing_that_writes(self):
        seen = {}

        def persist(compiled, proof, recordings, result):
            seen["origin"] = recordings[0]["origin"]
            seen["tags"] = [r["tag"] for r in recordings]
            seen["confirmed_by"] = recordings[0].get("confirmed_by")
            return "compiled_api.json"

        res, _a, _n = do_learn(SERDE, [REGEX, TOKIO], persist=persist)
        self.assertEqual(res.persisted, "compiled_api.json")
        self.assertEqual(seen["origin"], "agent")
        self.assertEqual(seen["tags"], ["agent0", "nav1", "nav2"])
        self.assertEqual(seen["confirmed_by"], "test")

    def test_nothing_is_persisted_without_a_persister(self):
        res, _a, _n = do_learn(SERDE, [REGEX, TOKIO])
        self.assertIsNone(res.persisted)

    def test_the_recording_is_indistinguishable_from_a_human_one(self):
        rec = SERDE.as_recording("wf", "q", "agent0")
        for key in ("workflow", "site", "cls", "tag", "param", "final_url", "ui",
                    "dom", "network", "recorded_at"):
            self.assertIn(key, rec)
        self.assertEqual(rec["param"], {"name": "q", "value": "serde"})
        self.assertEqual(rec["origin"], "agent")


# ---------------------------------------------------------------------------
# the landing check: URL pattern + title, never "the body mentions the value"
# ---------------------------------------------------------------------------

class LandingCheck(unittest.TestCase):

    def cand(self):
        return propose("t {q}", "https://ex.test/", "serde", workflow="wf",
                       agent=FakeAgent([SERDE]))

    def test_the_confirmed_pattern_with_the_new_value_passes(self):
        ok, why = landing_check(self.cand(), REGEX)
        self.assertTrue(ok, why)

    def test_an_index_page_that_mentions_the_value_is_refused(self):
        # A front page that lists everything mentions every value; it is not
        # the page.
        index = nav_for("regex", ["serde", "regex", "tokio", "everything"],
                        url="https://ex.test/",
                        title="ex.test: every crate there is")
        ok, why = landing_check(self.cand(), index)
        self.assertFalse(ok)
        self.assertIn("path", why)

    def test_a_different_path_with_the_right_title_is_refused(self):
        wrong = nav_for("regex", ["regex v1", "b", "c"], url="https://ex.test/crates/regex")
        ok, why = landing_check(self.cand(), wrong)
        self.assertFalse(ok)
        self.assertIn("path", why)

    def test_the_right_url_with_a_wrong_title_is_refused(self):
        # The navigation kept the URL but served a page of a different kind.
        odd = nav_for("regex", ["regex v1", "b", "c"], title="Just a moment...")
        ok, why = landing_check(self.cand(), odd)
        self.assertFalse(ok)
        self.assertIn("title", why)

    def test_a_body_mention_alone_never_counts(self):
        # Right title pattern, wrong path, value all over the body: refused.
        body = nav_for("regex", ["regex", "regex", "regex"], url="https://ex.test/blog",
                       title="Search: regex - ex.test")
        ok, _why = landing_check(self.cand(), body)
        self.assertFalse(ok)

    def test_wrong_page_shadow_logs_beside_rule_and_never_changes_it(self):
        import json as _json
        from anyuse.execution import confirmer

        class FakeRunner:
            def __init__(self):
                self.calls = []

            def __call__(self, requests, checkpoint):
                self.calls.append((requests, checkpoint))
                # Confident "no": Laya thinks this is the wrong page.  Shadow
                # must still return the deterministic landing decision.
                return [{"id": requests[0]["id"], "noul": 0.01, "ms": 2.0}]

        old = {k: os.environ.get(k) for k in
               ("ANYUSE_CONFIRMER", "ANYUSE_CONFIRMER_CHECKPOINT", "ANYUSE_DATA_DIR")}
        with tempfile.TemporaryDirectory() as data:
            try:
                os.environ["ANYUSE_CONFIRMER"] = "off"
                baseline = landing_check(self.cand(), REGEX)
                os.environ.update(ANYUSE_CONFIRMER="shadow",
                                  ANYUSE_CONFIRMER_CHECKPOINT="en",
                                  ANYUSE_DATA_DIR=data)
                fake = FakeRunner()
                observed = landing_check(self.cand(), REGEX, confirmer_runner=fake)
                self.assertEqual(observed, baseline)
                self.assertTrue(observed[0])
                req = fake.calls[0][0][0]
                self.assertIn("list of search results", req["question"]["instructions"])
                self.assertIn("regex", req["question"]["instructions"])
                with open(confirmer.log_path(), encoding="utf-8") as fh:
                    logs = [_json.loads(line) for line in fh]
                self.assertEqual(logs[-1]["kind"], "wrong_page")
                self.assertEqual(logs[-1]["deterministic"], "landing:pass")
                self.assertEqual(logs[-1]["acted"], "no")
                self.assertNotIn("regex", _json.dumps(logs[-1]))
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_wrong_page_is_off_by_default_even_with_a_fake_runner(self):
        old = os.environ.pop("ANYUSE_CONFIRMER", None)
        calls = []
        try:
            baseline = landing_check(self.cand(), REGEX)
            observed = landing_check(
                self.cand(), REGEX,
                confirmer_runner=lambda reqs, ck: calls.append((reqs, ck)) or [])
            self.assertEqual(observed, baseline)
            self.assertEqual(calls, [])
        finally:
            if old is not None:
                os.environ["ANYUSE_CONFIRMER"] = old

    def test_title_patterns(self):
        self.assertTrue(title_matches("bash - Arch packages", "core/x86_64/bash",
                                      "vim - Arch packages", "extra/x86_64/vim")[0])
        self.assertTrue(title_matches("yay 12.3.5-1 (AUR)", "yay",
                                      "paru 2.0.3-1 (AUR)", "paru")[0])
        self.assertTrue(title_matches("Packages", "serde", "Packages", "regex")[0])
        self.assertFalse(title_matches("Packages", "serde", "Home", "regex")[0])
        self.assertFalse(title_matches("serde - crates", "serde",
                                       "The Rust registry", "regex")[0])
        self.assertTrue(title_matches("", "a", "", "b")[0])
        self.assertFalse(title_matches("serde", "serde", "", "regex")[0])
        # a per-item description after the name, inside the site's constant frame
        self.assertTrue(title_matches(
            "rsync: a fast, versatile, local/remote file-copying tool | Man Page | Commands | rsync | ManKier",
            "rsync", "tar: an archiving utility | Man Page | Commands | tar | ManKier", "tar")[0])
        # a search page on the same site is not the man page's frame
        self.assertFalse(title_matches(
            "rsync: a fast, versatile, local/remote file-copying tool | Man Page | Commands | rsync | ManKier",
            "rsync", "Search results for tar | ManKier", "tar")[0])
        # a page whose JavaScript has not set the title yet is still refused;
        # the navigator's job is to read the page later, not the check's to relax
        self.assertFalse(title_matches("purescript-prelude - Pursuit", "prelude",
                                       "Pursuit", "arrays")[0])

    def test_usable_runs_names_the_reason(self):
        index = nav_for("tokio", ["a", "b"], url="https://ex.test/", title="ex.test")
        good, discarded = usable_runs([REGEX, index], self.cand())
        self.assertEqual([r.value for r in good], ["regex"])
        self.assertEqual(discarded[0]["value"], "tokio")
        self.assertIn("not the confirmed page", discarded[0]["why"])

    def test_a_run_that_errored_is_discarded(self):
        broken = run_for("x", [], dom="", error="TimeoutError")
        good, discarded = usable_runs([broken], self.cand())
        self.assertEqual(good, [])
        self.assertEqual(discarded[0]["why"], "TimeoutError")

    def test_fewer_than_two_further_landings_learns_nothing(self):
        index = nav_for("tokio", ["a", "b"], url="https://ex.test/", title="ex.test")
        res, _a, _n = do_learn(SERDE, [REGEX, index])
        self.assertEqual(res.outcome, "nothing")
        self.assertIn("confirmed page's URL pattern and title", res.reason)


# ---------------------------------------------------------------------------
# the ways the confirmed page can still fail to compile
# ---------------------------------------------------------------------------

class CannotCompile(unittest.TestCase):

    def test_too_few_values_is_refused_before_the_agent_runs(self):
        agent = FakeAgent([SERDE])
        res = learn("t {q}", "https://ex.test/", ["serde"], agent=agent,
                    executor=ApiStepExecutor(transport=RecordedTransport([])), confirm=YES)
        self.assertEqual(res.outcome, "nothing")
        self.assertEqual(agent.calls, [], "no value may be spent on a doomed run")
        self.assertIn(f"needs {MIN_VALUES}", res.reason)

    def test_a_confirmed_generic_page_learns_nothing(self):
        # The person confirmed a page whose list is the same for every value.
        first = run_for("serde", ["Home", "About", "Contact"], title="Search - ex.test")
        others = [nav_for(v, ["Home", "About", "Contact"], title="Search - ex.test")
                  for v in ("regex", "tokio")]
        res, _a, _n = do_learn(first, others)
        self.assertEqual(res.outcome, "nothing")
        self.assertTrue("nothing on screen answers the parameter" in res.reason
                        or "no repeated group" in res.reason, res.reason)

    def test_a_request_that_agrees_on_the_recording_and_not_on_fresh_input_fails(self):
        import copy
        first, others = copy.deepcopy(SERDE), copy.deepcopy([REGEX, TOKIO])
        recordings = [r.as_recording("wf", "q", f"x{i}") for i, r in enumerate([first, *others])]
        for rec in recordings[1:]:
            for req in rec["network"]:
                if "api" in req["url"]:
                    req["body"] = api_body(["WRONG", "ALSO WRONG", "NOPE"])
                else:
                    req["body"] = "<html><body><p>nothing</p></body></html>"
        res, _a, _n = do_learn(first, others,
                               executor=ApiStepExecutor(transport=RecordedTransport(recordings)))
        self.assertNotEqual(res.outcome, "tier1", res.reason)

    def test_a_budget_stops_the_loop_between_runs(self):
        first = run_for("serde", ["serde v1", "serde_json v1", "serde_derive v1"],
                        url="https://ex.test/results")
        others = [run_for("regex", ["regex v1", "b", "c"], url="https://ex.test/results"),
                  run_for("tokio", ["tokio v1", "b", "c"], url="https://ex.test/results")]
        res, agent, _n = do_learn(first, others, agent_runs=others,
                                  executor=ApiStepExecutor(transport=DeadTransport()),
                                  budget_usd=0.0000001)
        self.assertEqual(len(agent.calls), 2, "the candidate run, then one more, then stop")
        self.assertIn("budget", res.reason)


# ---------------------------------------------------------------------------
# tier 2 fall-back
# ---------------------------------------------------------------------------

class Tier2(unittest.TestCase):

    def test_when_the_proof_fails_tier_2_replays_the_confirmed_navigation(self):
        import copy
        runs = copy.deepcopy([SERDE, REGEX, TOKIO])
        rows_by_value = {r.value: r for r in runs}

        class Replay:
            def __init__(self):
                self.seen = []

            def execute(self, template, params):
                from anyuse.execution.html_extract import extract
                value = params["q"]
                self.seen.append(template.render(params))
                dom = rows_by_value[value].dom
                return {"success": True, "elapsed_ms": 5200.0, "model_calls": 0,
                        "extracted": {"items": extract(dom, "ul.results li.row", 40)}}

        replay = Replay()
        res, _a, _n = do_learn(runs[0], runs[1:], replay=replay,
                               executor=ApiStepExecutor(transport=DeadTransport()))
        self.assertEqual(res.outcome, "tier2_nonpersistent", res.reason)
        self.assertIn("no runnable artifact", res.reason)
        self.assertEqual(res.later_run_model_calls, 0)
        self.assertEqual(res.later_run_ms, 5200.0)
        self.assertTrue(all("q=" in u for u in replay.seen))

    def test_tier_2_is_graded_against_the_page_not_against_itself(self):
        class WrongReplay:
            def execute(self, template, params):
                return {"success": True, "elapsed_ms": 5000.0, "model_calls": 0,
                        "extracted": {"items": ["something", "else", "entirely"]}}

        res, _a, _n = do_learn(SERDE, [REGEX, TOKIO], replay=WrongReplay(),
                               executor=ApiStepExecutor(transport=DeadTransport()))
        self.assertEqual(res.outcome, "nothing")
        self.assertIn("coverage", res.tier2["reason"])

    def test_tier_2_does_not_pool_swapped_validation_answers(self):
        rows = {
            "regex": ["tokio v1", "tokio-util v0", "tokio-stream v0"],
            "tokio": ["regex v1", "regex-lite v0", "regex-automata v0"],
        }

        class SwappedReplay:
            def execute(self, template, params):
                return {"success": True, "elapsed_ms": 5000.0, "model_calls": 0,
                        "extracted": {"items": rows[params["q"]]}}

        res, _a, _n = do_learn(SERDE, [REGEX, TOKIO], replay=SwappedReplay(),
                               executor=ApiStepExecutor(transport=DeadTransport()))
        self.assertEqual(res.outcome, "nothing")
        self.assertIn("coverage", res.tier2["reason"])

    def test_without_a_replay_endpoint_tier_2_says_so(self):
        res, _a, _n = do_learn(SERDE, [REGEX, TOKIO],
                               executor=ApiStepExecutor(transport=DeadTransport()))
        self.assertEqual(res.outcome, "nothing")
        self.assertIn("no browser replay endpoint", res.tier2["reason"])


# ---------------------------------------------------------------------------
# continuing a saved candidate later (the CLI's learn-confirm, MCP's confirm)
# ---------------------------------------------------------------------------

class ContinueLater(unittest.TestCase):

    def test_a_saved_candidate_continues_without_running_the_agent_again(self):
        cand = propose("t {q}", "https://ex.test/", "serde", workflow="wf",
                       agent=FakeAgent([SERDE]))
        with tempfile.TemporaryDirectory() as tmp:
            save_candidate(cand, tmp)
            back = load_candidate(cand.candidate_id, tmp)
        agent = FakeAgent([])
        nav = FakeNavigator([REGEX, TOKIO])
        res = continue_learning(back, ["regex", "tokio"], agent=agent, navigator=nav,
                                executor=executor_for([SERDE, REGEX, TOKIO]),
                                confirmed_by="later",
                                proof_preflight=lambda _compiled: "")
        self.assertEqual(res.outcome, "tier1", res.reason)
        self.assertEqual(agent.calls, [])
        self.assertEqual(res.agent_runs, 1)
        self.assertEqual(res.confirmed_by, "later")

    def test_real_candidate_resolver_uses_anyuse_data_dir(self):
        old = os.environ.get("ANYUSE_DATA_DIR")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ["ANYUSE_DATA_DIR"] = tmp
                self.assertEqual(candidates_dir(), os.path.join(tmp, "learn_candidates"))
            finally:
                if old is None:
                    os.environ.pop("ANYUSE_DATA_DIR", None)
                else:
                    os.environ["ANYUSE_DATA_DIR"] = old

    def test_candidate_store_cleans_up_expired_raw_proposals(self):
        import time
        cand = propose("t {q}", "https://ex.test/", "serde", workflow="wf",
                       agent=FakeAgent([SERDE]))
        with tempfile.TemporaryDirectory() as tmp:
            path = save_candidate(cand, tmp)
            old = time.time() - 48 * 3600
            os.utime(path, (old, old))
            self.assertEqual(cleanup_candidates(tmp, max_age_hours=24), 1)
            self.assertIsNone(load_candidate(cand.candidate_id, tmp))


# ---------------------------------------------------------------------------
# small pieces
# ---------------------------------------------------------------------------

class AgentTrace(unittest.TestCase):
    """The per-step trace is bench-only, off by default, and records what the
    agent saw, chose and got — what a later re-ranking study needs."""

    class FakeSession:
        def __init__(self, endpoint, allow_url=None):
            self.url_now = "https://ex.test/"
            self.refused = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def goto(self, url):
            self.url_now = url

        async def observe(self):
            return {"url": self.url_now, "text": "Search here",
                    "elements": [{"i": 0, "tag": "input", "type": "search",
                                  "name": "Search", "href": ""},
                                 {"i": 1, "tag": "a", "type": "", "name": "About",
                                  "href": "/about"}]}

        async def focus(self, idx):
            return True

        async def type(self, text):
            pass

        async def press_enter(self):
            self.url_now = "https://ex.test/search?q=serde"

        async def click(self, idx):
            pass

        async def settle(self, s):
            pass

        async def dom(self):
            return "<html><title>serde</title><ul class='results'><li>a</li></ul></html>"

        async def text(self):
            return "serde"

        async def url(self):
            return self.url_now

        async def network(self):
            return []

    def brain(self):
        script = iter([{"action": "type", "index": 0, "text": "serde"},
                       {"action": "submit", "index": 0}, {"action": "done"}])
        return lambda messages: (next(script), {"prompt_tokens": 10, "completion_tokens": 2}, False)

    def test_off_by_default(self):
        from anyuse.execution.agent_browser import StepAgent
        agent = StepAgent(brain=self.brain(), session_factory=self.FakeSession,
                          endpoint="http://192.168.64.5:9222")
        run = agent.run("search for {q}", "https://ex.test/", "serde")
        self.assertEqual(run.trace, [])
        self.assertNotIn("trace", run.to_dict())

    def test_records_candidates_choice_and_outcome_per_step(self):
        from anyuse.execution.agent_browser import StepAgent
        agent = StepAgent(brain=self.brain(), session_factory=self.FakeSession,
                          endpoint="http://192.168.64.5:9222", trace=True)
        run = agent.run("search for {q}", "https://ex.test/", "serde")
        self.assertEqual([t["choice"]["action"] for t in run.trace],
                         ["type", "submit", "done"])
        self.assertEqual(len(run.trace[0]["candidates"]), 2)
        self.assertEqual(run.trace[0]["chosen"]["name"], "Search")
        self.assertEqual(run.trace[1]["outcome"]["url_after"],
                         "https://ex.test/search?q=serde")
        self.assertTrue(run.trace[1]["outcome"]["navigated"])
        self.assertNotIn("outcome", run.trace[2], "done acts on nothing")
        self.assertIn("trace", run.to_dict(with_trace=True))


class StayOnSite(unittest.TestCase):

    def test_same_site(self):
        from anyuse.execution.agent_browser import same_site
        self.assertFalse(same_site("https://pkgs.racket-lang.org/", "https://pkgd.racket-lang.org/x"))
        self.assertFalse(same_site("https://www.freebsd.org/", "https://man.freebsd.org/cgi/man.cgi"))
        self.assertTrue(same_site(
            "https://www.freebsd.org/", "https://man.freebsd.org/cgi/man.cgi",
            related_origins=["https://man.freebsd.org"]))
        self.assertFalse(same_site("https://victim.co.uk/", "https://attacker.co.uk/"))
        self.assertFalse(same_site("https://good.github.io/", "https://evil.github.io/"))
        self.assertFalse(same_site("https://www.freebsd.org/", "https://duckduckgo.com/?q=x"))
        self.assertFalse(same_site("https://ex.test/", ""))

    def test_index_zero_is_a_real_element(self):
        from anyuse.execution.agent_browser import action_index
        self.assertEqual(action_index({"action": "type", "index": 0}), 0)
        self.assertEqual(action_index({"action": "type", "index": "3"}), 3)
        self.assertEqual(action_index({"action": "read"}), -1)
        self.assertEqual(action_index({"action": "click", "index": "x"}), -1)

    def test_an_off_site_landing_is_not_a_candidate(self):
        away = run_for("serde", ["a", "b", "c"], url="https://duckduckgo.com/?q=serde")
        cand = propose("t {q}", "https://ex.test/", "serde", workflow="wf",
                       agent=FakeAgent([away]))
        self.assertFalse(cand.ok)
        self.assertIn("off the site", cand.error)

    def test_the_gate_refuses_off_site_navigations_before_they_are_sent(self):
        from anyuse.execution.agent_browser import StepAgent
        seen = {}

        class Session(AgentTrace.FakeSession):
            def __init__(self, endpoint, allow_url=None):
                super().__init__(endpoint)
                seen["gate"] = allow_url

        script = iter([{"action": "done"}])
        agent = StepAgent(brain=lambda m: (next(script), {}, False),
                          session_factory=Session, endpoint="http://192.168.64.5:9222",
                          allow_url=lambda u: "forbidden" not in u)
        agent.run("t {q}", "https://ex.test/", "serde")
        gate = seen["gate"]
        self.assertTrue(gate("https://ex.test/search?q=serde"))
        self.assertFalse(gate("https://ex.test/forbidden"), "robots still applies")
        self.assertFalse(gate("https://duckduckgo.com/?q=serde"), "off-site is refused")


class NavigatorSettle(unittest.TestCase):
    """The navigator reads a page only once it is complete, still and titled."""

    class Session(AgentTrace.FakeSession):
        def __init__(self, endpoint, allow_url=None):
            super().__init__(endpoint)
            self.polls = 0

        async def js(self, expr):
            self.polls += 1
            # the title arrives on the third poll, the DOM stops growing after
            return {"ready": "complete", "len": 1000 + (0 if self.polls > 2 else self.polls * 100),
                    "title": 0 if self.polls < 3 else 12}

    def test_waits_for_a_titled_still_document(self):
        from anyuse.execution.agent_browser import Navigator
        holder = {}

        def factory(endpoint, allow_url=None):
            holder["s"] = NavigatorSettle.Session(endpoint, allow_url)
            return holder["s"]

        nav = Navigator("http://192.168.64.5:9222", session_factory=factory, stable_ms=10,
                        ceiling_s=5.0)
        run = nav("https://ex.test/search?q=regex", "regex")
        self.assertEqual(run.model_calls, 0)
        self.assertEqual(run.steps[0]["action"], "navigate")
        self.assertGreaterEqual(holder["s"].polls, 3)

    def test_a_session_without_js_just_waits(self):
        from anyuse.execution.agent_browser import Navigator
        nav = Navigator("http://192.168.64.5:9222", session_factory=AgentTrace.FakeSession)
        run = nav("https://ex.test/search?q=regex", "regex")
        self.assertTrue(run.ok)


class Pieces(unittest.TestCase):

    def test_task_text_binds_the_placeholder(self):
        self.assertEqual(task_text("search for {q} here", "serde"),
                         "search for serde here")
        self.assertEqual(task_text("just search", "serde"), "just search (for: serde)")

    def test_an_unreadable_model_reply_is_not_an_action(self):
        # it used to become ``done``, which ended the run as if it had finished
        self.assertIsNone(parse_action("I think we should click the button"))
        self.assertIsNone(parse_action(""))
        self.assertIsNone(parse_action("[1, 2, 3]"))
        self.assertIsNone(parse_action("{not json}"))
        self.assertIsNone(parse_action("```json\nnothing here\n```"))

    def test_a_readable_reply_survives_fences_prose_lists_and_a_trailing_comma(self):
        want = {"action": "click", "index": 2}
        self.assertEqual(parse_action('```json\n{"action":"click","index":2}\n```'), want)
        self.assertEqual(parse_action('Sure. {"action":"click","index":2} Done.'), want)
        self.assertEqual(parse_action('{"action":"click","index":2,}'), want)
        self.assertEqual(parse_action('[{"action":"click","index":2}]'), want)
        self.assertEqual(parse_action('```\nnot this\n```\n```json\n{"action":"click","index":2}\n```'),
                         want)
        self.assertEqual(parse_action('{"a": {"b": 1}} and then {"action":"click","index":2}'),
                         {"a": {"b": 1}}, "the first object wins; the model gets one object")

    def test_candidate_from_dict_ignores_unknown_keys(self):
        c = Candidate.from_dict({"final_url": "https://x", "bogus": 1})
        self.assertEqual(c.final_url, "https://x")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
