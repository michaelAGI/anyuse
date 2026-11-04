"""The deterministic browser replay rung.

Every test here runs with an injected fake driver: this file opens no socket,
starts no browser and imports no websocket client. That is not only for the
test's sake — it is the same boundary the product uses, so a fake driver and a
container browser are interchangeable by construction.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from anyuse.execution import api_permission as perm
from anyuse.execution.cdp_replay import (  # noqa: F401
    READINESS_ENV, ready_read_passes, readiness_default,
    ENDPOINT_ENV, PageResult, ReplayExecutor, ReplayTemplate, cdp_enabled,
    replay_hil_action, template_from_recording)
from anyuse.execution.deterministic import rank_implementations
from anyuse.guides.state_graph import TransitionImpl, UITransition

ROWS = """
<html><body><main>
  <ul class="results">
    <li class="row"><a>Alpha Pkg</a></li>
    <li class="row"><a>Beta Pkg</a></li>
    <li class="row"><a>Gamma Pkg</a></li>
  </ul>
</main></body></html>
"""
EMPTY = ("<html><body><main><p>No results found for alpha.</p>"
         "</main></body></html>")
CHALLENGE = "<html><body>Please complete the captcha to continue</body></html>"

SEL = "ul.results li.row"


class FakeDriver:
    """Returns a canned page. The only place the product touches a browser."""

    def __init__(self, dom=ROWS, text="Alpha Pkg Beta Pkg Gamma Pkg",
                 url="https://ex.test/s?q=alpha", status=200, error="",
                 requests=None, raises=None):
        self.dom, self.text, self.url = dom, text, url
        self.status, self.error = status, error
        self.requests = requests or []
        self.raises = raises
        self.calls = []

    def open(self, url, settle_s, timeout_s):
        self.calls.append(url)
        if self.raises:
            raise self.raises
        return PageResult(url=self.url or url, dom=self.dom, text=self.text,
                          status=self.status, elapsed_ms=1234.0,
                          error=self.error, requests=self.requests)


def template(**kw):
    base = dict(url="https://ex.test/s?q={q}", params=["q"],
                extract={"items": SEL},
                expect={"selector": SEL, "min_items": 1})
    base.update(kw)
    return ReplayTemplate(**base)


class Scratch(unittest.TestCase):
    """Every test points the blocked/host registries at a temp dir."""

    def setUp(self):
        perm._ROBOTS_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self._env = {k: os.environ.get(k) for k in
                     ("ANYUSE_API_HOSTS_FILE", "ANYUSE_BLOCKED_HOSTS_FILE",
                      "ANYUSE_ROBOTS_CACHE_FILE", ENDPOINT_ENV)}
        os.environ["ANYUSE_API_HOSTS_FILE"] = os.path.join(self.tmp.name, "h.json")
        os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = os.path.join(self.tmp.name, "b.json")
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = os.path.join(self.tmp.name, "r.json")
        os.environ.pop(ENDPOINT_ENV, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        perm._ROBOTS_CACHE.clear()
        self.tmp.cleanup()


class OptIn(Scratch):

    def test_the_rung_is_off_unless_an_endpoint_is_named(self):
        self.assertFalse(cdp_enabled())
        out = ReplayExecutor().execute(template(), {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertIn(ENDPOINT_ENV, out["error"])

    def test_the_refusal_says_it_never_runs_on_your_desktop(self):
        out = ReplayExecutor().execute(template(), {"q": "alpha"})
        self.assertIn("never runs on your desktop", out["error"])

    def test_an_endpoint_env_var_turns_it_on(self):
        os.environ[ENDPOINT_ENV] = "http://127.0.0.1:9222"
        self.assertTrue(cdp_enabled())

    def test_importing_the_module_pulls_in_no_websocket_client(self):
        """The driver imports websockets lazily, inside a running replay."""
        import anyuse.execution.cdp_replay as mod
        self.assertFalse(hasattr(mod, "websockets"))

    def test_redirected_browser_navigation_is_refused_per_hop(self):
        class PolicyDriver(FakeDriver):
            def open(self, url, settle_s, timeout_s, readiness=None, url_policy=None):
                self.calls.append(url)
                refusal = url_policy("https://evil.test/landing")
                return PageResult(url=url, error=("redirect refused: " + refusal)
                                  if refusal else "")

        driver = PolicyDriver()
        out = ReplayExecutor(driver=driver).execute(template(), {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertIn("exact-origin allowlist", out["error"])


class Templates(Scratch):

    def test_a_non_https_url_is_refused(self):
        t = template(url="http://ex.test/s?q={q}")
        self.assertTrue(any("https" in p for p in t.problems()))

    def test_a_template_with_no_extraction_is_refused(self):
        t = template(extract={})
        self.assertTrue(any("reads nothing" in p for p in t.problems()))

    def test_an_undeclared_placeholder_is_refused(self):
        t = template(url="https://ex.test/s?q={q}&p={page}")
        self.assertTrue(any("page" in p for p in t.problems()))

    def test_a_parameter_cannot_inject_a_path_or_a_query(self):
        url = template().render({"q": "a/b?c=d&e=f"})
        self.assertEqual(url, "https://ex.test/s?q=a%2Fb%3Fc%3Dd%26e%3Df")

    def test_a_missing_parameter_never_reaches_the_wire(self):
        with self.assertRaises(KeyError):
            template().render({})

    def test_it_round_trips_through_a_dict(self):
        t = template()
        self.assertEqual(ReplayTemplate.from_dict(t.to_dict()).to_dict(),
                         t.to_dict())


class FromRecording(Scratch):

    def _rec(self, url, value="alpha"):
        return {"param": {"name": "q", "value": value}, "final_url": url,
                "ground_truth_selector": SEL}

    def test_a_query_parameter_becomes_a_placeholder(self):
        t = template_from_recording(self._rec("https://ex.test/s?q=alpha&page=1"), "q")
        self.assertIn("q={q}", t.url)
        self.assertIn("page=1", t.url)

    def test_a_path_segment_becomes_a_placeholder(self):
        t = template_from_recording(self._rec("https://ex.test/pkg/alpha"), "q")
        self.assertEqual(t.url, "https://ex.test/pkg/{q}")

    def test_a_navigation_that_cannot_be_re_bound_is_refused(self):
        t = template_from_recording(self._rec("https://ex.test/static/page"), "q")
        self.assertEqual(t.url, "")
        self.assertIn("cannot be re-bound", t.notes)


class CountIsPerInput(Scratch):
    """A result count belongs to one input (as the ladder's re-proof showed)."""

    def _rec(self, value, n):
        return {"param": {"name": "q", "value": value},
                "final_url": f"https://ex.test/s?q={value}", "ground_truth_selector": SEL,
                "ui": {"items": [f"{value} {i}" for i in range(n)]}}

    def test_the_floor_is_the_smallest_count_of_the_fitting_inputs(self):
        from anyuse.execution.cdp_replay import finalize_minimum, for_validation
        base = self._rec("alpha", 30)
        t = template_from_recording(base, "q", recordings=[base, self._rec("beta", 7),
                                                           self._rec("gamma", 12)])
        self.assertEqual(t.expect["min_items"], 7)
        alone = template_from_recording(base, "q")
        self.assertEqual(alone.expect["min_items"], 30)      # unchanged without evidence
        proof = for_validation(t)
        self.assertEqual(proof.expect["min_items"], 1)
        self.assertEqual(t.expect["min_items"], 7)           # the copy, not the template
        finalize_minimum(t, [3, 12])
        self.assertEqual(t.expect["min_items"], 3)

    def test_a_validation_page_with_fewer_rows_is_compared_not_refused(self):
        from anyuse.execution.cdp_replay import for_validation
        t = template(expect={"selector": SEL, "min_items": 8})
        out = ReplayExecutor(driver=FakeDriver()).execute(t, {"q": "alpha"})
        self.assertFalse(out["success"])                     # 3 < 8 under the old floor
        out = ReplayExecutor(driver=FakeDriver()).execute(for_validation(t), {"q": "alpha"})
        self.assertTrue(out["success"], out.get("error"))
        self.assertEqual(len(out["extracted"]["items"]), 3)

    def test_an_empty_page_still_fails_under_the_relaxed_floor(self):
        from anyuse.execution.cdp_replay import for_validation
        t = for_validation(template(expect={"selector": SEL, "min_items": 8}))
        out = ReplayExecutor(driver=FakeDriver(dom="<html><body><p>hi</p></body></html>",
                                               text="hi")).execute(t, {"q": "alpha"})
        self.assertFalse(out["success"])


class HonestFailure(Scratch):

    def _run(self, driver, **kw):
        return ReplayExecutor(driver=driver).execute(template(**kw), {"q": "alpha"})

    def test_a_normal_page_succeeds_and_returns_the_items(self):
        out = self._run(FakeDriver())
        self.assertTrue(out["success"], out.get("error"))
        self.assertEqual(out["extracted"]["items"],
                         ["Alpha Pkg", "Beta Pkg", "Gamma Pkg"])
        self.assertEqual(out["model_calls"], 0)

    def test_zero_items_on_a_silent_page_is_a_FAILURE(self):
        """The single most damaging thing an automation layer can say is
        'it worked, here are no results'."""
        out = self._run(FakeDriver(dom="<html><body><main>x</main></body></html>",
                                   text="something else entirely"))
        self.assertFalse(out["success"])
        self.assertIn("does not say it found nothing", out["error"])

    def test_zero_items_on_a_page_that_says_so_is_a_SUCCESS(self):
        out = self._run(FakeDriver(dom=EMPTY, text="No results found for alpha."))
        self.assertTrue(out["success"])
        self.assertEqual(out["extracted"]["items"], [])
        self.assertIn("No results", out["empty_state"])

    def test_a_no_results_sentence_that_does_not_name_the_query_is_a_FAILURE(self):
        dom = "<html><body><main><p>No results found for your search.</p></main></body></html>"
        out = self._run(FakeDriver(dom=dom, text="No results found for your search."))
        self.assertFalse(out["success"])
        self.assertIn("does not say it found nothing", out["error"])
        self.assertIn("not next to the query", out["error"])

    def test_the_empty_phrase_is_reported_so_it_can_be_disputed(self):
        dom = "<html><body><main><p>No matches for alpha.</p></main></body></html>"
        out = self._run(FakeDriver(dom=dom, text="No matches for alpha."))
        self.assertTrue(out["empty_state"])

    def test_a_postcondition_below_min_items_fails(self):
        out = self._run(FakeDriver(), expect={"selector": SEL, "min_items": 10})
        self.assertFalse(out["success"])
        self.assertEqual(out["outcome_kind"], "incomplete")
        self.assertIn("postcondition failed", out["error"])

    def test_same_origin_wrong_result_page_is_incomplete(self):
        rec = {"param": {"name": "q", "value": "alpha"},
               "final_url": "https://ex.test/s?q=alpha",
               "ground_truth_selector": SEL,
               "ui": {"items": ["Alpha Pkg", "Beta Pkg", "Gamma Pkg"]}}
        tmpl = template_from_recording(rec, "q", readiness=False)
        out = ReplayExecutor(
            driver=FakeDriver(url="https://ex.test/unrelated?q=alpha")).execute(
                tmpl, {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertEqual(out["outcome_kind"], "incomplete")
        self.assertIn("identity failed", out["error"])

    def test_recorded_parameter_identity_must_still_hold(self):
        t = template(expect={"selector": SEL, "min_items": 3,
                             "identity": {"final_url": "rendered_url",
                                          "parameter": "q"}})
        dom = ROWS.replace("Alpha Pkg", "One").replace("Beta Pkg", "Two").replace(
            "Gamma Pkg", "Three")
        out = ReplayExecutor(driver=FakeDriver(dom=dom, text="One Two Three")).execute(
            t, {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertEqual(out["outcome_kind"], "incomplete")
        self.assertIn("identity failed", out["error"])

    def test_a_browser_error_is_an_ordinary_failure(self):
        out = self._run(FakeDriver(raises=RuntimeError("connection refused")))
        self.assertFalse(out["success"])
        self.assertIn("browser error", out["error"])

    def test_a_navigation_error_is_reported_not_swallowed(self):
        out = self._run(FakeDriver(error="navigation failed: TimeoutError"))
        self.assertFalse(out["success"])
        self.assertIn("navigation failed", out["error"])

    def test_every_outcome_reports_zero_model_calls(self):
        for driver in (FakeDriver(), FakeDriver(dom=EMPTY, text="No results"),
                       FakeDriver(dom=CHALLENGE, text="Please complete the captcha"),
                       FakeDriver(raises=RuntimeError("x"))):
            self.assertEqual(self._run(driver)["model_calls"], 0)


class EmptyAnswerModelCalls(Scratch):
    """The check model is asked about an empty answer only inside a product call's scope."""

    PAGE = "<html><body><main><p>Hakuna matokeo ya alpha.</p></main></body></html>"

    def _empty(self, ctx):
        return ReplayExecutor(driver=FakeDriver(dom=self.PAGE, text="Hakuna matokeo ya alpha.")
                              ).execute(template(), {"q": "alpha"}, empty=ctx)

    def test_an_empty_answer_on_a_product_call_reports_the_check_models_call(self):
        from anyuse.execution import empty_page as EP
        from anyuse.execution import model_check as MC
        with MC.scope("product"), MC.using(lambda i, s, o: 0.95):
            out = self._empty(EP.Context(probe=False, learn=False))
        self.assertTrue(out["success"], out.get("error"))
        self.assertEqual(out["model_calls"], 1)
        self.assertEqual(out["result_contract"]["empty_by"], "model_check")
        self.assertEqual(out["empty_state"], "Hakuna matokeo ya alpha.")

    def test_outside_a_scope_no_model_is_asked_and_the_unlisted_statement_is_refused(self):
        from anyuse.execution import empty_page as EP
        from anyuse.execution import model_check as MC
        with MC.using(lambda i, s, o: 0.95):
            out = self._empty(EP.Context(probe=False, learn=False))
        self.assertFalse(out["success"])
        self.assertEqual(out["model_calls"], 0)


class BlockedHandling(Scratch):

    def test_a_captcha_page_hands_over_to_a_human(self):
        out = ReplayExecutor(driver=FakeDriver(
            dom=CHALLENGE, text="Please complete the captcha to continue")).execute(
                template(), {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertTrue(out["requires_human"])
        self.assertIn("asked for a human", out["error"])

    def test_the_hand_over_offers_no_workaround(self):
        out = ReplayExecutor(driver=FakeDriver(
            dom=CHALLENGE, text="Please complete the captcha")).execute(
                template(), {"q": "alpha"})
        blob = str(out).lower()
        for banned in ("bypass", "solve the captcha", "work around", "circumvent"):
            self.assertNotIn(banned, blob)

    def test_a_blocked_host_is_recorded_so_it_is_not_hit_again(self):
        driver = FakeDriver(dom=CHALLENGE, text="Please complete the captcha")
        ex = ReplayExecutor(driver=driver)
        ex.execute(template(), {"q": "alpha"})
        second = ex.execute(template(), {"q": "beta"})
        self.assertFalse(second["success"])
        self.assertEqual(len(driver.calls), 1, "the blocked host was hit twice")

    def test_a_cookie_banner_over_real_results_is_not_a_block(self):
        out = ReplayExecutor(driver=FakeDriver(
            text="We use cookies. Manage your cookie settings. Alpha Pkg")).execute(
                template(), {"q": "alpha"})
        self.assertTrue(out["success"], out.get("error"))


class RobotsApplies(Scratch):

    def test_a_disallowed_path_is_not_opened(self):
        perm.check_permission("https://ex.test/s?q=x",
                              fetcher=lambda _u: "User-agent: *\nDisallow: /s\n")
        driver = FakeDriver()
        out = ReplayExecutor(driver=driver).execute(template(), {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertTrue(out["robots_refused"])
        self.assertEqual(driver.calls, [], "a robots-disallowed page was opened")


class LadderPosition(unittest.TestCase):

    def test_cdp_sits_below_api_and_above_ax(self):
        impls = [TransitionImpl(modality=m, command={}, times_used=1,
                                times_succeeded=1)
                 for m in ("coordinate", "ax", "cdp", "api")]
        order = rank_implementations(
            UITransition(from_state="a", to_state="b", intent="i",
                         implementations=impls))
        self.assertEqual([i.modality for i in order],
                         ["api", "cdp", "ax", "coordinate"])

    def test_the_relative_order_of_the_pre_existing_rungs_is_unchanged(self):
        impls = [TransitionImpl(modality=m, command={}, times_used=1,
                                times_succeeded=1)
                 for m in ("computer_use", "coordinate", "applescript", "js",
                           "ax", "api")]
        order = rank_implementations(
            UITransition(from_state="a", to_state="b", intent="i",
                         implementations=impls))
        self.assertEqual([i.modality for i in order],
                         ["api", "ax", "js", "applescript", "coordinate",
                          "computer_use"])

    def test_a_replay_step_classifies_as_read_only(self):
        from anyuse.execution.hil_gate import classify_step
        action = replay_hil_action(template(), {"q": "alpha"})
        self.assertEqual(action["action"], "read")
        self.assertEqual(str(classify_step(action)), "read_only")

    def test_a_destructive_looking_url_is_still_promoted_by_the_gate(self):
        from anyuse.execution.hil_gate import classify_step
        action = replay_hil_action(
            template(url="https://ex.test/delete?q={q}"), {"q": "alpha"})
        self.assertNotEqual(str(classify_step(action)), "read_only")


class ReadinessDefault(unittest.TestCase):
    """Readiness is on unless switched off; the timer remains its cap and fallback."""

    REC = {"final_url": "https://ex.test/search?q=serde",
           "param": {"name": "q", "value": "serde"},
           "ground_truth_selector": "ul.results li",
           "ui": {"items": ["serde v1", "serde_json v1", "serde_derive v1"]},
           "network": []}

    def test_readiness_is_on_unless_switched_off(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(READINESS_ENV, None)
            self.assertTrue(readiness_default())
            tmpl = template_from_recording(dict(self.REC), "q")
            self.assertEqual(tmpl.readiness.get("selector"), "ul.results li")
            self.assertEqual(tmpl.readiness.get("fallback"), "timer")
            # an explicit argument still wins
            self.assertEqual(template_from_recording(dict(self.REC), "q",
                                                     readiness=False).readiness, {})
        with mock.patch.dict(os.environ, {READINESS_ENV: "off"}):
            self.assertFalse(readiness_default())
            self.assertEqual(template_from_recording(dict(self.REC), "q").readiness, {})
            self.assertTrue(template_from_recording(dict(self.REC), "q",
                                                    readiness=True).readiness)

    def _long(self, value, n):
        return {"final_url": f"https://ex.test/search?q={value}",
                "param": {"name": "q", "value": value},
                "ground_truth_selector": "ul.results li",
                "ui": {"items": [f"{value} {i}" for i in range(n)]}, "network": []}

    def test_the_ready_floor_is_the_count_floor_however_long_the_list(self):
        base = self._long("alpha", 30)
        tmpl = template_from_recording(base, "q", recordings=[base, self._long("beta", 30)],
                                       readiness=True)
        self.assertEqual(tmpl.expect["min_items"], 30)
        self.assertEqual(tmpl.readiness["min_items"], 30)
        tmpl = template_from_recording(base, "q", recordings=[base, self._long("beta", 7)],
                                       readiness=True)
        self.assertEqual(tmpl.readiness["min_items"], 7)

    def test_a_ready_read_of_a_long_list_passes_at_its_floor(self):
        rows = "".join(f"<li>pkg {i}</li>" for i in range(30))
        ready = {"selector": "ul.results li", "min_items": 30, "fallback": "timer"}
        ok, why = ready_read_passes(f"<html><ul class='results'>{rows}</ul></html>", ready)
        self.assertTrue(ok, why)

    def test_finalize_minimum_lowers_the_ready_floor_with_the_count_floor(self):
        from anyuse.execution.cdp_replay import finalize_minimum
        base = self._long("alpha", 30)
        tmpl = template_from_recording(base, "q", recordings=[base, self._long("beta", 7)],
                                       readiness=True)
        finalize_minimum(tmpl, [3, 12])
        self.assertEqual(tmpl.expect["min_items"], 3)
        self.assertEqual(tmpl.readiness["min_items"], 3)
        timer = template_from_recording(base, "q", readiness=False)
        finalize_minimum(timer, [3])
        self.assertEqual(timer.readiness, {})

    def test_a_ready_read_that_extracts_nothing_is_not_accepted(self):
        ready = {"selector": "ul.results li", "min_items": 3, "fallback": "timer"}
        ok, why = ready_read_passes("<html><ul class='results'></ul></html>", ready)
        self.assertFalse(ok)
        self.assertIn("0 item(s)", why)
        ok, why = ready_read_passes(
            "<html><ul class='results'><li>a one</li></ul></html>", ready)
        self.assertFalse(ok)
        self.assertIn("3 needed", why)
        ok, why = ready_read_passes(
            "<html><ul class='results'><li>a one</li><li>b two</li>"
            "<li>c three</li></ul></html>", ready)
        self.assertTrue(ok, why)
        self.assertTrue(ready_read_passes("<html></html>", {"selector": ""})[0])

    def test_recording_sets_the_completeness_floor_for_replay_and_readiness(self):
        tmpl = template_from_recording(dict(self.REC), "q", readiness=True)
        self.assertEqual(tmpl.expect["min_items"], 3)
        self.assertEqual(tmpl.readiness["min_items"], 3)

    def _shown(self, shown, n=6):
        rows = "".join(f"<li>alpha {i}</li>" if i < shown else
                       f"<li style='display:none'>alpha {i}</li>" for i in range(n))
        return f"<html><ul class='results'>{rows}</ul></html>"

    def test_a_visible_only_ready_read_counts_the_rows_on_screen(self):
        rec = {**self._long("alpha", 6), "ground_truth_visible_only": True}
        ready = template_from_recording(rec, "q", readiness=True).readiness
        ok, why = ready_read_passes(self._shown(3), ready)
        self.assertFalse(ok, why)
        self.assertIn("3 item(s)", why)
        self.assertTrue(ready_read_passes(self._shown(6), ready)[0])
        # a template that reads every row counts the hidden ones too
        every = template_from_recording(self._long("alpha", 6), "q", readiness=True).readiness
        self.assertTrue(ready_read_passes(self._shown(3), every)[0])

    def test_a_visible_only_replay_is_read_again_while_rows_are_still_hidden(self):
        import asyncio

        from anyuse.execution.browser_supervisor import DOM_EXPR
        from anyuse.execution.cdp_replay import CdpDriver
        rec = {**self._long("alpha", 6), "ground_truth_visible_only": True}
        tmpl = template_from_recording(rec, "q", readiness=True)
        now = [0.0]

        async def js(expr):
            if expr == DOM_EXPR:
                # every row is in the page from the start; the last three show at 2 s
                return self._shown(6 if now[0] >= 2.0 else 3)
            return {"n": 6, "len": 1000, "ready": "complete"}

        async def pump(seconds):
            now[0] += seconds

        class Driver:
            def open(self, url, settle_s, timeout_s, readiness=None):
                events = [{"method": "Network.requestWillBeSent",
                           "params": {"requestId": "d", "request": {"url": url}}},
                          {"method": "Network.loadingFinished", "params": {"requestId": "d"}}]
                dom, settle = asyncio.run(CdpDriver("http://127.0.0.1:0")._settle_and_read(
                    js, pump, events, readiness, 10.0, clock=lambda: now[0]))
                return PageResult(url=url, dom=dom, text="", status=200, settle=settle)

        out = ReplayExecutor(driver=Driver()).execute(tmpl, {"q": "alpha"})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["settle"]["how"], "ready->timer_fallback", out["settle"])
        self.assertEqual(len(out["extracted"]["items"]), 6)

    # -- the first read: a short or empty answer the replay itself would accept ----

    @staticmethod
    def _rows(names):
        items = "".join(f"<li><a href='/p/{n}'>{n}</a></li>" for n in names)
        return f"<html><body><h1>Search</h1><ul class='results'>{items}</ul></body></html>"

    def _ready(self, contract=True, **extra):
        from anyuse.execution import result_contract as RC
        ready = {"selector": "ul.results li", "min_items": 6, "fallback": "timer",
                 "value": "zzqx", "params": {"q": "zzqx"}, **extra}
        if contract:
            ready["contract"] = RC.replay_contract("q")
        return ready

    def test_a_short_answer_the_contract_accepts_passes_the_ready_read(self):
        dom = self._rows(["zzqx core", "zzqx cli"])
        ok, why = ready_read_passes(dom, self._ready())
        self.assertTrue(ok, why)
        self.assertIn("sparse", why)
        ok, why = ready_read_passes(dom, self._ready(contract=False))
        self.assertFalse(ok)
        self.assertEqual(why, "2 item(s) extracted at the ready read, 6 needed")
        # a result that does not name the input is read again at the ceiling
        ok, why = ready_read_passes(self._rows(["zzqx core", "other"]), self._ready())
        self.assertFalse(ok, why)
        # three or more still need the recorded count
        ok, why = ready_read_passes(self._rows(["zzqx a", "zzqx b", "zzqx c"]), self._ready())
        self.assertFalse(ok, why)

    def test_an_empty_page_that_says_so_passes_the_ready_read(self):
        said = ("<html><body><h1>Search</h1><ul class='results'></ul>"
                "<p>No results found for \"zzqx\"</p></body></html>")
        ok, why = ready_read_passes(said, self._ready(), text='Search\nNo results found for "zzqx"')
        self.assertTrue(ok, why)
        self.assertIn("No results found", why)
        silent = "<html><body><h1>Search</h1><ul class='results'></ul></body></html>"
        self.assertFalse(ready_read_passes(silent, self._ready())[0])
        self.assertFalse(ready_read_passes(said, self._ready(contract=False))[0])
        # the statement must be on screen when the browser's text is given
        self.assertFalse(ready_read_passes(said, self._ready(), text="Search")[0])

    def test_the_empty_statement_check_can_be_replaced(self):
        from anyuse.execution.cdp_replay import runtime_readiness
        said = ("<html><body><ul class='results'></ul>"
                "<p>No results found for \"zzqx\"</p></body></html>")
        seen = []

        def never(markup, text, value):
            seen.append(value)
            return ""
        self.assertFalse(ready_read_passes(said, self._ready(), empty=never)[0])
        self.assertEqual(seen, ["zzqx"])
        self.assertTrue(ready_read_passes("<html><ul class='results'></ul></html>",
                                          self._ready(), empty=lambda m, t, v: "nichts")[0])
        # given to runtime_readiness, the check travels with the replay's readiness
        tmpl = template_from_recording(self._long("zzqx", 6), "q", readiness=True)
        ready = runtime_readiness(tmpl, {"q": "zzqx"}, empty=never)
        self.assertIs(ready["empty"], never)
        self.assertFalse(ready_read_passes(said, ready)[0])

    def test_the_sites_own_empty_page_passes_the_ready_read_in_any_language(self):
        from anyuse.execution import empty_page as EP
        from anyuse.execution.cdp_replay import runtime_readiness, site_empty_check
        from anyuse.execution.html_extract import extract

        def shell(body, promo):
            return (f"<html><body><nav><a href='/'>Trang chủ</a></nav><h1>Tìm kiếm</h1>{body}"
                    f"<aside><p>{promo}</p></aside></body></html>")

        def found(q):
            rows = "".join(f"<li><a href='/p/{n}'>{n} {q}</a></li>" for n in ("Ấm", "Bình"))
            return shell(f"<p>Kết quả cho “{q}”</p><ul class='results'>{rows}</ul>", "Quà tặng")

        def none(q, promo="Quà tặng"):
            return shell(f"<p>Không tìm thấy sản phẩm nào cho “{q}”.</p>", promo)

        def page(markup, q):
            return EP.Page("html", q, markup=markup, items=extract(markup, "ul.results li"))
        made_up = EP.nonsense_values(["alpha", "beta"], seed="https://shop.vn/s?q={q}")
        sig = EP.learn("html", [page(found("alpha"), "alpha"), page(found("beta"), "beta")],
                       [page(none(v, p), v) for v, p in zip(made_up, ("Giảm giá", "Hôm nay"))])
        self.assertEqual(sig["kind"], "html", sig)
        tmpl = template_from_recording(self._long("zzqx", 6), "q", readiness=True)
        self.assertIsNone(site_empty_check(tmpl, None))
        check = site_empty_check(tmpl, EP.Context(learned=sig))
        ready = {**runtime_readiness(tmpl, {"q": "zzqx"}, empty=check), **self._ready()}
        # the listed-language statement rule does not read this page; the learned page does
        self.assertFalse(ready_read_passes(none("zzqx"), self._ready())[0])
        ok, why = ready_read_passes(none("zzqx"), ready)
        self.assertTrue(ok, why)
        self.assertIn("Không tìm thấy", why)
        # a page with a line the learned empty page lacks is not read early
        self.assertFalse(ready_read_passes(
            none("zzqx").replace("</aside>", "</aside><p>Lỗi máy chủ</p>"), ready)[0])

    def test_the_ready_read_takes_the_statement_from_what_the_browser_shows(self):
        import asyncio

        from anyuse.execution.browser_supervisor import DOM_EXPR, TEXT_EXPR
        from anyuse.execution.cdp_replay import CdpDriver
        # a stylesheet hides the statement: the markup has it, the screen does not
        hidden = ("<html><head><style>.gone { display: none }</style></head><body>"
                  "<h1>Search</h1><ul class='results'></ul>"
                  "<p class='gone'>No results found for \"zzqx\"</p></body></html>")
        shown = hidden.replace(" class='gone'", "")
        ready = dict(self._ready(), rule="value_calls", explaining=[["ex.test", "/"]],
                     value_calls=[0], poll_ms=50, stable_ms=0)
        for markup, text, how in ((shown, 'Search\nNo results found for "zzqx"', "ready"),
                                  (hidden, "Search", "ready->timer_fallback")):
            async def js(expr, markup=markup, text=text):
                if expr == DOM_EXPR:
                    return markup
                if expr == TEXT_EXPR:
                    return text
                return {"n": 0, "rlen": 0, "len": len(markup), "ready": "complete"}

            async def pump(_s):
                return None
            _dom, settle = asyncio.run(CdpDriver("http://127.0.0.1:0")._settle_and_read(
                js, pump, [], ready, 0.5, clock=iter([i * 0.05 for i in range(400)]).__next__))
            self.assertEqual(settle["how"], how, settle)

    def test_the_replay_readiness_carries_the_call_and_is_not_stored(self):
        from anyuse.execution.cdp_replay import READINESS_RULE, runtime_readiness
        tmpl = template_from_recording(self._long("alpha", 6), "q", readiness=True)
        stored = dict(tmpl.readiness)
        self.assertEqual(stored["rule"], READINESS_RULE)
        ready = runtime_readiness(tmpl, {"q": "beta"})
        self.assertEqual(ready["value"], "beta")
        self.assertEqual(ready["params"], {"q": "beta"})
        self.assertEqual(ready["contract"]["query_param"], "q")
        self.assertNotIn("empty", ready)
        self.assertEqual(tmpl.readiness, stored)
        self.assertNotIn("value", tmpl.to_dict()["readiness"])
        self.assertIsNone(runtime_readiness(template_from_recording(
            self._long("alpha", 6), "q", readiness=False), {"q": "beta"}))
        # the executor hands it to the driver
        seen = {}
        rows = "<html><ul class='results'>" + "".join(
            f"<li>beta {i}</li>" for i in range(6)) + "</ul></html>"

        class Driver:
            def open(self, url, settle_s, timeout_s, readiness=None):
                seen["readiness"] = readiness
                return PageResult(url=url, dom=rows, text="", status=200)
        ReplayExecutor(driver=Driver()).execute(tmpl, {"q": "beta"})
        self.assertEqual(seen["readiness"]["value"], "beta")

    def test_a_sparse_answer_is_read_without_the_timer(self):
        import asyncio

        from anyuse.execution.browser_supervisor import DOM_EXPR, TEXT_EXPR
        from anyuse.execution.cdp_replay import CdpDriver
        rec = self._long("alpha", 6)
        rec["final_url"] = "https://ex.test/search?q=alpha"
        rec["network"] = [
            {"url": "https://ex.test/search?q=alpha", "status": 200, "mime": "text/html",
             "body": "<html><ul class='results'></ul></html>"},
            {"url": "https://ex.test/api/find?q=alpha", "status": 200,
             "mime": "application/json", "body": '{"r": ["alpha 0", "alpha 1", "alpha 2"]}'}]
        tmpl = template_from_recording(rec, "q", readiness=True)
        now = [0.0]
        answered_at = 0.8

        def page():
            rows = "".join(f"<li><a href='/p/{n}'>{n}</a></li>"
                           for n in (["beta core", "beta cli"] if now[0] >= answered_at else []))
            return f"<html><body><ul class='results'>{rows}</ul></body></html>"

        async def js(expr):
            if expr == DOM_EXPR:
                return page()
            if expr == TEXT_EXPR:
                return "beta core\nbeta cli" if now[0] >= answered_at else ""
            n = 2 if now[0] >= answered_at else 0
            return {"n": n, "rlen": 17 if n else 0, "len": len(page()), "ready": "complete"}

        class Driver:
            def open(self, url, settle_s, timeout_s, readiness=None):
                events = []
                schedule = [(0.0, "Network.requestWillBeSent", "d", url),
                            (0.1, "Network.loadingFinished", "d", None),
                            (0.1, "Network.requestWillBeSent", "x",
                             "https://ex.test/api/find?q=beta"),
                            (answered_at, "Network.loadingFinished", "x", None)]

                async def pump(seconds):
                    now[0] += seconds
                    while schedule and schedule[0][0] <= now[0] + 1e-9:
                        _t, method, rid, u = schedule.pop(0)
                        params = {"requestId": rid}
                        if u:
                            params["request"] = {"url": u}
                        events.append({"method": method, "params": params})
                dom, settle = asyncio.run(CdpDriver("http://127.0.0.1:0")._settle_and_read(
                    js, pump, events, readiness, 10.0, clock=lambda: now[0]))
                return PageResult(url=url, dom=dom, text="", status=200, settle=settle)

        out = ReplayExecutor(driver=Driver()).execute(tmpl, {"q": "beta"})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["settle"]["how"], "ready", out["settle"])
        self.assertLess(out["settle"]["waited_ms"], 2000)
        self.assertEqual(out["extracted"]["items"], ["beta core", "beta cli"])
        self.assertEqual(tmpl.readiness.get("value_calls"), [0, 1])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
