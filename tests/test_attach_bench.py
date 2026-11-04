"""Sample 5's bench, offline.

The query rule, the block rule and the never-contact writer only. No socket,
no browser, no model call.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from anyuse.execution.web_runtime import corpus_sample5 as C
from anyuse.execution.web_runtime import attach_bench as S


def listing(rows, nav=True):
    navhtml = ("<nav><ul>" + "".join(f"<li><a href='/x{i}'>Menu{i} item</a></li>"
                                     for i in range(8)) + "</ul></nav>") if nav else ""
    body = "".join(f"<li class='row'><a href='/p/{i}'>{t}</a> <span>some description "
                   f"text for row {i}</span></li>" for i, t in enumerate(rows))
    return f"<html><body>{navhtml}<main><ul class='list'>{body}</ul></main></body></html>"


class QueryRule(unittest.TestCase):

    def test_first_four_distinct_qualifying_tokens_in_order(self):
        dom = listing(["jason — JSON parser", "Next", "plug_cowboy adapter", "jason again",
                       "ab", "ecto 3", "12345", "phoenix_html", "telemetry"])
        self.assertEqual(C.query_values(dom), ["jason", "plug_cowboy", "ecto", "phoenix_html"])

    def test_navigation_groups_are_skipped(self):
        dom = listing(["alpha x", "beta y", "gamma z", "delta w", "epsilon v"])
        self.assertEqual(C.query_values(dom), ["alpha", "beta", "gamma", "delta"])

    def test_too_few_values_is_reported_not_padded(self):
        dom = listing(["alpha", "alpha", "Next", "More", "x"])
        self.assertEqual(C.query_values(dom), ["alpha"])
        self.assertEqual(C.query_values(""), [])

    def test_token_rule(self):
        self.assertEqual(C.token_of("  «Économie» et société"), "Économie")
        self.assertEqual(C.token_of("(vim)"), "vim")
        self.assertEqual(C.token_of(""), "")

    def test_corpus_shape(self):
        # check_fresh() is a pre-run check: the run itself added blocked
        # corpus hosts to NEVER_CONTACT.txt afterwards
        self.assertEqual(len(C.MAIN), C.TARGET)
        self.assertEqual(len({w["site"] for w in C.MAIN + C.RESERVE}),
                         len(C.MAIN) + len(C.RESERVE))


class BlockRule(unittest.TestCase):

    def test_a_403_document_stops_the_host(self):
        why = S.page_block("Forbidden", "https://x.test/s?q=a",
                           [{"url": "https://x.test/s?q=a", "resource_type": "Document",
                             "status": 403}])
        self.assertIn("403", why)

    def test_a_challenge_page_stops_the_host(self):
        self.assertTrue(S.page_block("Just a moment... Checking your browser",
                                     "https://x.test/", []))

    def test_a_served_page_with_a_cookie_banner_does_not(self):
        self.assertEqual(S.page_block("We use cookies. " + "result row " * 600,
                                      "https://x.test/s?q=a", []), "")

    def test_never_contact_is_appended_once_and_kept_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "NC.txt")
            with open(p, "w") as fh:
                fh.write("# header\nb.test\nd.test\n")
            old = S.NEVER_CONTACT
            S.NEVER_CONTACT = p
            try:
                S.add_never_contact("c.test", "challenge")
                S.add_never_contact("c.test", "again")
            finally:
                S.NEVER_CONTACT = old
            with open(p) as fh:
                lines = fh.read().splitlines()
        self.assertEqual([ln for ln in lines if not ln.startswith("#")],
                         ["b.test", "c.test", "d.test"])
        self.assertEqual(sum(1 for ln in lines if "c.test" in ln and ln.startswith("#")), 1)


class CompileLast(unittest.TestCase):

    def test_a_chain_tier_1_refused_keeps_the_selection_session_for_tier_2(self):
        # the selection session searched without opening the result, so the chain fitted on it
        # is refused; tier 2 is then fitted on the compile and selection sessions
        import test_chain_sources as TCS
        from anyuse.execution import attach as A
        sessions = [TCS.session("serde"), TCS.session("tokio", fetch=TCS.single_traffic),
                    TCS.session("rand"), TCS.session("regex")]
        real = (A.list_sessions, A.select_sessions)
        A.list_sessions, A.select_sessions = (lambda *a, **kw: sessions,
                                              lambda found, task=None, **kw: found)
        try:
            row = S.compile_last("package releases", TCS.executor(), checker=None)
        finally:
            A.list_sessions, A.select_sessions = real
        self.assertEqual(row["tier1"], "failed", row)
        self.assertEqual(row["split"]["fitting"], 1)
        self.assertEqual(row["_n_selection"], 1)


if __name__ == "__main__":
    unittest.main()
