"""The sparse-result contract.

A held-out value whose search shows fewer than three results is no longer
rejected outright; it passes only if the extraction returns EXACTLY the
visible results, in order, and every result names the query. Fixture: the
www.thecocktaildb.com sessions ('Boyarsky' had 1 result, 'Boston' 2; both
failed "insufficient evidence" before).
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import unittest

from anyuse.execution import api_verify as V
from anyuse.execution import attach as A
from anyuse.execution.html_extract import extract

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))
import ladder as L  # noqa: E402

SEL = "div.center div.row div.col-sm-3"


def pages():
    with gzip.open(os.path.join(HERE, "testdata", "web_sessions",
                                "thecocktaildb_sparse.sessions.json.gz"), "rt") as fh:
        f = json.load(fh)
    return {v: A.result_snapshot(s, v)["dom"] for s, v in zip(f["sessions"], f["values"])}


class Sparse(unittest.TestCase):

    def setUp(self):
        self.p = pages()

    def items(self, v):
        return extract(self.p[v], SEL)

    def test_saved_sparse_pages_have_fewer_than_three_results(self):
        self.assertEqual(len(self.items("Boyarsky")), 1)
        self.assertEqual(len(self.items("Boston")), 2)
        self.assertGreaterEqual(len(self.items("Mojito")), 3)

    def test_exact_sparse_answer_naming_the_query_passes(self):
        for v in ("Boyarsky", "Boston"):
            want = self.items(v)
            ok, why = L.correctness(want, want, min_values=3, value=v)
            self.assertTrue(ok, why)
            cr = V.compare(want, want, value=v)
            self.assertTrue(cr.passed, cr.reason)
            self.assertEqual(cr.contract["kind"], "sparse_identity")

    def test_without_the_value_the_old_floor_stands(self):
        want = self.items("Boyarsky")
        ok, why = L.correctness(want, want, min_values=3)
        self.assertFalse(ok)
        self.assertIn("insufficient evidence", why)
        self.assertFalse(V.compare(want, want).passed)

    def test_extra_rows_or_another_answer_fail(self):
        want = self.items("Boyarsky")
        ok, _ = L.correctness(want + self.items("Mojito")[:1], want, value="Boyarsky")
        self.assertFalse(ok)
        # the Boston page's answer for the Boyarsky query: does not name it
        ok, why = L.correctness(self.items("Boston"), self.items("Boston"), value="Boyarsky")
        self.assertFalse(ok)
        self.assertIn("does not name", why)
        # containment is not equality under the sparse contract
        self.assertFalse(V.compare(["Boston Sour"], ["Boston Sour Special"], value="Boston").passed)

    def test_names_value_is_word_bound(self):
        self.assertTrue(V.names_value("Boston Sour", "boston"))
        self.assertFalse(V.names_value("Bostonian Cocktail", "Boston"))
        self.assertTrue(V.names_value("Rose-Quartz ring", "rose quartz"))


if __name__ == "__main__":
    unittest.main()
