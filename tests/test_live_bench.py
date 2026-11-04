"""Offline tests of the live bench: corpus, arms, the
seeded query rule, the trace replay, the pacer and the report's taxonomy.
No network, no browser, no model."""
import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

import corpus_live as C  # noqa: E402


def listing(n=12):
    rows = "".join(f'<li class="row"><a href="/p/{i}">Name{i} extra words</a></li>'
                   for i in range(n))
    return f"<html><body><main><ul>{rows}</ul></main></body></html>"


class Corpus(unittest.TestCase):
    def test_well_formed_and_paired(self):
        # after the run, hosts the run itself stopped are on NEVER_CONTACT;
        # before it the list was empty (plan). Here: well-formed, clashes only
        # with hosts this corpus contains
        clash = C.check_fresh()
        self.assertTrue(set(clash) <= set(C.BY_SITE), clash)
        a = C.assign()
        self.assertEqual(len(a), sum(C.PER_COHORT.values()) * len(C.COHORTS))
        for c in C.COHORTS:
            for st, n in C.PER_COHORT.items():
                arms = [v["arm"] for s, v in a.items() if v["cohort"] == c and v["stratum"] == st]
                self.assertEqual(arms.count("S"), n // 2)
                self.assertEqual(arms.count("L"), n // 2)
            self.assertEqual(len(C.queue(c, "S")), 12)
            self.assertEqual(len(C.queue(c, "L")), 12)

    def test_assignment_is_fixed(self):
        self.assertEqual(C.assign(), C.assign())

    def test_registrable(self):
        self.assertEqual(C.registrable("www.tate.org.uk"), "tate.org.uk")
        self.assertEqual(C.registrable("www.jstage.jst.go.jp"), "jstage.jst.go.jp")
        self.assertEqual(C.registrable("docs.sympy.org"), "sympy.org")


class QueryRule(unittest.TestCase):
    def test_seeded_draw_is_deterministic_and_from_the_listing(self):
        v1 = C.query_values("example.org", listing())
        v2 = C.query_values("example.org", listing())
        self.assertEqual(v1, v2)
        self.assertEqual(len(v1), 5)
        self.assertEqual(len(set(v1)), 5)
        for v in v1:
            self.assertRegex(v, r"^Name\d+$")

    def test_different_sites_draw_differently(self):
        self.assertNotEqual(C.query_values("a.org", listing(30)),
                            C.query_values("b.org", listing(30)))

    def test_too_few_tokens(self):
        self.assertLess(len(C.query_values("x.org", listing(4))), 5)


class Bench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import live_bench
        cls.V = live_bench

    def test_substitute(self):
        s = self.V.substitute
        self.assertEqual(s("Alpha", "alpha", "beta"), "beta")
        self.assertEqual(s("find alpha now", "alpha", "beta"), "find beta now")
        self.assertEqual(s("other", "alpha", "beta"), "other")

    def test_pacer_honours_a_crawl_delay(self):
        p = self.V.HostPacer(1.0)
        clock = [0.0]
        p.clock = lambda: clock[0]
        p.intervals["slow.org"] = 5.0
        self.assertEqual(p.reserve("slow.org"), 0.0)
        self.assertEqual(p.reserve("slow.org"), 5.0)
        self.assertEqual(p.reserve("fast.org"), 0.0)
        self.assertEqual(p.reserve("fast.org"), 1.0)
        self.assertEqual(p.count["slow.org"], 2)

    def test_agent_success(self):
        ok = self.V.agent_success
        self.assertTrue(ok("https://a.org/", "home", "https://a.org/s?q=x", "results for Foo", "foo"))
        self.assertFalse(ok("https://a.org/", "home", "https://b.org/s?q=x", "Foo", "foo"))
        self.assertFalse(ok("https://a.org/", "home", "chrome-error://x", "Foo", "foo"))
        self.assertFalse(ok("https://a.org/", "home Foo", "https://a.org/", "home Foo", "foo"))
        self.assertFalse(ok("https://a.org/", "home", "https://a.org/s", "nothing", "foo"))

    def test_trace_replay_types_the_new_value_and_skips_consent_steps(self):
        calls = []

        class Page:
            guard = type("G", (), {"failed": []})()

            def __init__(self, *a, **k):
                self._url = "about:blank"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def goto(self, url):
                calls.append(("goto", url))
                self._url = url

            async def dom(self):
                return "<html><body><input></body></html>"

            async def text(self):
                return "page"

            async def url(self):
                return self._url

            async def network(self, want_bodies=True):
                return []

            async def focus(self, i):
                calls.append(("focus", i))
                return True

            async def js(self, expr):
                return True

            async def type(self, t):
                calls.append(("type", t))

            async def press_enter(self):
                calls.append(("enter",))
                self._url = "https://a.org/search?q=x"

            async def click(self, i):
                calls.append(("click", i))

            async def settle(self, s):
                pass

        class BF:
            endpoint = "http://127.0.0.1:1"

            @staticmethod
            def _allowed(u):
                return True

            class _settler:
                @staticmethod
                async def _settle(page, t0):
                    return None

        steps = [{"action": "consent_reject", "deterministic": True},
                 {"action": "click", "index": 5,
                  "outcome": {"acted": False, "page_changed": "gone"}},
                 {"action": "type", "index": 3, "text": "Alpha"},
                 {"action": "submit", "index": 3}, {"action": "read"},
                 {"action": "done"}, {"action": "click", "index": 9}]
        out = asyncio.run(self.V._trace_replay(BF, Page, {"start_url": "https://a.org/",
                                                          "site": "a.org"},
                                               steps, "Alpha", "Beta"))
        self.assertIn(("type", "Beta"), calls)
        self.assertIn(("enter",), calls)
        self.assertNotIn(("click", 5), calls)            # refused in the run: nothing to replay
        self.assertNotIn(("click", 9), calls)            # after "done": never replayed
        self.assertEqual(out["final_url"], "https://a.org/search?q=x")
        self.assertIn("before_dom", out)

    def test_trace_replay_stops_at_the_first_result_page(self):
        # on www.nhs.uk the replay ran the agent's redundant second search and
        # a suggestion click, ending on another query's results; the replay now
        # stops at the first submit/click after which the task check holds
        calls = []

        class Page:
            guard = type("G", (), {"failed": []})()

            def __init__(self, *a, **k):
                self._url, self._text, self._typed = "about:blank", "home", ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def goto(self, url):
                self._url = url

            async def dom(self):
                return "<html><body><input></body></html>"

            async def text(self):
                return self._text

            async def url(self):
                return self._url

            async def network(self, want_bodies=True):
                return []

            async def focus(self, i):
                return True

            async def js(self, expr):
                calls.append(("select",))
                self._selected = True
                return True

            async def type(self, t):
                calls.append(("type", t))
                self._typed = t if getattr(self, "_selected", False) else self._typed + t
                self._selected = False

            async def press_enter(self):
                calls.append(("enter",))
                self._url = f"https://a.org/search?q={self._typed}"
                self._text = f"results for {self._typed}"

            async def click(self, i):
                calls.append(("click", i))
                self._url, self._text = "https://a.org/search?q=betaing", "results for betaing"

            async def settle(self, s):
                pass

        class BF:
            endpoint = "http://127.0.0.1:1"

            @staticmethod
            def _allowed(u):
                return True

            class _settler:
                @staticmethod
                async def _settle(page, t0):
                    return None

        steps = [{"action": "type", "index": 2, "text": "Alpha"},
                 {"action": "submit", "index": 2},
                 {"action": "type", "index": 8, "text": "Alpha"},
                 {"action": "click", "index": 9}, {"action": "done"}]
        entry = {"start_url": "https://a.org/", "site": "a.org"}
        out = asyncio.run(self.V._trace_replay(BF, Page, entry, steps, "Alpha", "Beta"))
        self.assertEqual(calls, [("select",), ("type", "Beta"), ("enter",)])
        self.assertEqual(out["final_url"], "https://a.org/search?q=Beta")
        self.assertIn("stopped_early", out)
        calls.clear()
        out = asyncio.run(self.V._trace_replay(BF, Page, entry, steps, "Alpha", "Beta",
                                               compact=False))
        self.assertIn(("click", 9), calls)                 # the full replay, kept as an option
        self.assertNotIn("stopped_early", out)

    def test_typed_searches_wait_for_the_value_request(self):
        import ladder_verify as LR
        self.assertGreater(LR.GetOnlySession.inflight_grace_s, 0)

    def test_our_own_ceiling_is_not_a_block(self):
        self.assertTrue(self.V.own_cap("tier 2: host blocked: host cap of 300 reached"))
        self.assertTrue(self.V.own_cap("site ceiling of 300 requests for a.org reached"))
        self.assertFalse(self.V.own_cap("HTTP 429 on the document https://a.org/"))
        self.assertFalse(self.V.own_cap("captcha_turnstile"))

    def test_only_a_refused_search_request_is_robots_search_path(self):
        f = self.V.search_refused
        aux = ["https://a.org/api/i18n/en", "robots_refused: https://a.org/api/i18n/en"]
        srch = ["https://a.org/search?q=Lake+Erie", "robots_refused: https://a.org/search"]
        self.assertEqual(f([aux], "Lake Erie"), [])
        self.assertEqual(f([aux, srch], "Lake Erie"), [srch])
        self.assertEqual(f([["https://a.org/x?q=Lake", "third-party host"]], "Lake"), [])

    def test_a_detail_shape_tool_persists_without_chrome_dropped(self):
        # attach's detail path returns a namespace with no
        # chrome_dropped; persisting its proved tool crashed the arm process
        import types
        from unittest import mock
        from anyuse.execution import api_compile
        seen = {}

        def rec(template, proof, workflow="", chrome_filtered=None, source=None):
            seen["chrome"] = chrome_filtered
            return {"ok": 1}
        compiled = types.SimpleNamespace(template=object(), reason="")
        proof = types.SimpleNamespace(passed=True, to_dict=lambda: {})
        with mock.patch.object(api_compile, "compiled_record", rec), \
                mock.patch.object(api_compile, "persist_compiled", lambda st, wf, r: "/x"), \
                mock.patch("anyuse.memory.storage.WorkflowStorage", lambda: None):
            out = self.V.persist_tier1({"_compiled": compiled, "_proof": proof}, "a.org", "wf")
        self.assertEqual(out, "/x")
        self.assertEqual(seen["chrome"], [])

    def test_a_withheld_page_is_not_a_usable_capture(self):
        # mathworld.wolfram.com: attach withheld every page loaded with
        # the site's own 'WR_SID' cookie; result_snapshot fell back to about:blank
        blank = {"url": "about:blank", "dom": "<html><body></body></html>", "text": ""}
        held = {"url": "https://a.org/search?q=Beta", "dom": "", "text": "",
                "withheld": "the page was loaded with a credential"}
        self.assertFalse(self.V.usable_snapshot({"snapshots": [blank, held]}, "Beta"))
        ok = {"url": "https://a.org/search?q=Beta", "dom": "<p>Beta</p>", "text": "Beta"}
        self.assertTrue(self.V.usable_snapshot({"snapshots": [blank, ok]}, "Beta"))
        self.assertFalse(self.V.usable_snapshot(None, "Beta"))
        self.assertEqual(self.V.failure_of({"arm": "S", "outcome": "capture_failed",
                                            "capture_withheld": ["x"], "runs": []}),
                         "capture_cookie")

    def test_taxonomy(self):
        f = self.V.primary_failure
        self.assertEqual(f({"outcome": "blocked", "blocked": "x"}), "blocked")
        self.assertEqual(f({"outcome": "agent_failed",
                            "escalation": {"stopped": "provider_unreachable"}}), "agent_provider")
        self.assertEqual(f({"outcome": "agent_failed", "escalation": {
            "attempts": [{"trigger": "policy"}]}}), "agent_policy")
        self.assertEqual(f({"proved": True, "audit": "correct", "mcp": {"ok": True}}), "")
        self.assertEqual(f({"proved": True, "audit": "wrong", "mcp": {"ok": True}}), "audit_wrong")
        self.assertEqual(f({"proved": True, "audit": "correct", "mcp": {"ok": False}}), "mcp")
        self.assertEqual(f({"arm": "S", "compile": False, "agent_success": True,
                            "usable_capture": True, "runs": [{"probe": {}}]}), "compile")

    def test_newcombe(self):
        d, lo, hi = self.V.newcombe(6, 12, 6, 12)
        self.assertAlmostEqual(d, 0.0)
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)


class DevGate(unittest.TestCase):
    """DEV gate: the DEV set is well formed,
    never blocked, and the runner refuses DEV-only switches outside DEV."""


class SiteScreen(unittest.TestCase):
    """The eligibility screen's offline part and the
    seeded allocation."""

    def test_classify(self):
        import corpus_live as CL
        import site_screen as SC
        home = ('<html><body><form action="/search" method="get"><input name="q" '
                'type="text"></form><a href="/list">all</a>' + listing(12)[12:])
        r = SC.classify("a.org", "https://a.org/", "https://a.org/list", home,
                        "https://a.org/", CL.qualifying_tokens)
        self.assertEqual((r["screen"], r["stratum"], r["listing_check"]),
                         ("eligible", "get_form", "checked"))
        js = '<html><body><div><input type="search" placeholder="Find"></div><a href="/list">x</a></body></html>'
        r = SC.classify("a.org", "https://a.org/", "https://a.org/list", js,
                        "https://a.org/", CL.qualifying_tokens)
        self.assertEqual((r["screen"], r["stratum"], r["listing_check"]),
                         ("eligible", "other", "linked"))
        r = SC.classify("a.org", "https://a.org/", "https://a.org/other", js,
                        "https://a.org/", CL.qualifying_tokens)
        self.assertEqual(r["screen"], "listing")
        hidden = '<html><body><div style="display:none"><input name="q"></div></body></html>'
        self.assertEqual(SC.classify("a.org", "https://a.org/", "https://a.org/", hidden,
                                     "https://a.org/", CL.qualifying_tokens)["screen"],
                         "no_search")
        post = '<form method="post" action="/s"><input name="query"></form>' + listing(12)
        self.assertEqual(SC.classify("a.org", "https://a.org/", "https://a.org/", post,
                                     "https://a.org/", CL.qualifying_tokens)["stratum"],
                         "other")


if __name__ == "__main__":
    unittest.main()
