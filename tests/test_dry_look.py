"""The dry look admits on ONE derivation and never overwrites it.

Pure logic over fake documents: no socket, no browser.
"""
from __future__ import annotations

import sys
import unittest

from anyuse.execution.web_runtime.dry_look import MIN_GROUND_TRUTH, admit, apply, check_third
from anyuse.execution.html_extract import derive_list_items


def listing(rows):
    items = "".join(f'<li class="hit"><a>{r}</a><span>meta {r}</span></li>' for r in rows)
    return (f"<html><head><title>t</title></head><body><nav><a>Home</a><a>About</a>"
            f'<a>Docs</a></nav><ul class="results">{items}</ul>'
            f"<footer><a>Terms</a><a>Privacy</a><a>Status</a></footer></body></html>")


def rec_for(tag, rows, dom=None):
    return {"tag": tag, "dom": dom if dom is not None else listing(rows),
            "ui": {"text": " ".join(rows), "items": []}}


REC = rec_for("rec", ["alpha one", "alpha two", "alpha three", "alpha four"])
FRESH1 = rec_for("fresh1", ["beta one", "beta two", "beta three", "beta four"])
FRESH2 = rec_for("fresh2", ["gamma one", "gamma two", "gamma three", "gamma four"])


class Admission(unittest.TestCase):

    def test_two_loads_with_a_changing_list_are_admitted(self):
        selector, why = admit(REC, FRESH1)
        self.assertTrue(selector, why)
        self.assertEqual(why, "")

    def test_two_loads_with_the_same_list_are_rejected(self):
        same = rec_for("fresh1", ["alpha one", "alpha two", "alpha three", "alpha four"])
        selector, why = admit(REC, same)
        self.assertEqual(selector, "")
        self.assertIn("no repeated group changed", why)

    def test_fewer_than_the_minimum_values_is_rejected(self):
        few = rec_for("rec", ["a", "b"])
        few2 = rec_for("fresh1", ["c", "d"])
        selector, why = admit(few, few2)
        self.assertEqual(selector, "")

    def test_a_missing_load_is_rejected(self):
        selector, why = admit(REC, rec_for("fresh1", [], dom=""))
        self.assertEqual(selector, "")
        self.assertIn("fewer than two pages", why)


class TheThirdLoad(unittest.TestCase):

    def test_a_consistent_third_load_passes_with_the_admitted_selector(self):
        selector, _ = admit(REC, FRESH1)
        ok, why, items = check_third(selector, REC, FRESH2)
        self.assertTrue(ok, why)
        self.assertGreaterEqual(len(items), MIN_GROUND_TRUTH)

    def test_an_unrendered_third_load_is_a_recorder_fault_not_no_ground_truth(self):
        selector, _ = admit(REC, FRESH1)
        blank = rec_for("fresh2", [], dom="<html><head></head><body></body></html>")
        ok, why, items = check_third(selector, REC, blank)
        self.assertFalse(ok)
        self.assertIn("extracts 0 value(s)", why)
        self.assertEqual(items, [])

    def test_a_different_page_shape_on_the_third_load_is_a_recorder_fault(self):
        # The search for the third value redirected straight to an item page.
        selector, _ = admit(REC, FRESH1)
        manpage = rec_for("fresh2", [], dom=(
            "<html><head><title>gamma(1)</title></head><body><h1>gamma</h1>"
            "<p>NAME gamma - do things</p><p>SYNOPSIS gamma [-x]</p></body></html>"))
        ok, why, _items = check_third(selector, REC, manpage)
        self.assertFalse(ok)
        self.assertIn("different page shape", why)

    def test_a_third_load_that_repeats_the_first_list_did_not_answer(self):
        selector, _ = admit(REC, FRESH1)
        repeat = rec_for("fresh2", ["alpha one", "alpha two", "alpha three", "alpha four"])
        ok, why, _items = check_third(selector, REC, repeat)
        self.assertFalse(ok)
        self.assertIn("did not answer its input", why)


class NeverReDerived(unittest.TestCase):

    def test_apply_stores_the_admitted_selector_on_every_recording(self):
        selector, _ = admit(REC, FRESH1)
        recs = [dict(REC, ui={"text": "", "items": []}),
                dict(FRESH1, ui={"text": "", "items": []}),
                dict(FRESH2, ui={"text": "", "items": []})]
        apply(recs, selector)
        for r in recs:
            self.assertEqual(r["ground_truth_selector"], selector)
            self.assertGreaterEqual(len(r["ui"]["items"]), MIN_GROUND_TRUTH)
            self.assertIn("never re-derived", r["dry_look"]["rule"])

    def test_the_old_three_way_re_derivation_would_have_lost_the_entry(self):
        # This is the defect: with a bad third load, deriving over all three
        # returns nothing, and the old code stored THAT over an admitted verdict.
        blank = "<html><head></head><body></body></html>"
        three_way, _ = derive_list_items([REC["dom"], FRESH1["dom"], blank])
        self.assertEqual(three_way, "", "the three-way derivation finds nothing")
        selector, _ = admit(REC, FRESH1)
        self.assertTrue(selector, "the admitted selector stands regardless")
        ok, _why, _items = check_third(selector, REC, rec_for("fresh2", [], dom=blank))
        self.assertFalse(ok, "and the third load is named a recorder fault instead")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
