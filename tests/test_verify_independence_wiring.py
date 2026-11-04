"""VERIFIER INDEPENDENCE, the wiring: the shared attribute verifier at the web answer
path (``demo_tool.run_tool``), in the ladder's checker (``repair_check.judge``: probation, the upgrade gate, repair
acceptance) and its availability sentinel, and at the desktop's text effects (``step_effect.evaluate`` with an
independent reading). Off by default: every seam is byte-identical then. Fakes only, no model (a fake resolver)."""
from __future__ import annotations

import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_compile as TC  # noqa: E402
from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import repair_check as RCK  # noqa: E402
from anyuse.execution import step_effect as SE  # noqa: E402
from anyuse.context.accessibility import AccessibilityTree, AXElementDesc, AXSnapshot  # noqa: E402

VALUE = "Borealis Toaster 2 Slice"


class On:
    """The setting on (and the default resolver a fake) for one block."""

    def __init__(self, resolver=None):
        self.resolver = resolver if resolver is not None else AV.Resolver(None)

    def __enter__(self):
        self.old = os.environ.get(AV.SETTING)
        os.environ[AV.SETTING] = "1"
        self.saved = dict(AV._DEFAULT)
        AV._DEFAULT["r"] = self.resolver
        return self

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop(AV.SETTING, None)
        else:
            os.environ[AV.SETTING] = self.old
        AV._DEFAULT.clear()
        AV._DEFAULT.update(self.saved)


def with_text(transform):
    real = TC.spec_for

    def spec(url, *a, **kw):
        s = dict(real(url, *a, **kw))
        if "/p/" in url:
            s["text"] = transform(s["text"])
        return s
    return mock.patch.object(TC, "spec_for", spec)


class AnswerPath(unittest.TestCase):
    def test_off_is_unchanged(self):
        os.environ[AV.SETTING] = "off"                  # on by default
        out = TC.run_tool(TC.FakeShopPage(), TC.tool(), VALUE)
        self.assertEqual(out["status"], "ok")
        self.assertNotIn("attribute_verify", out["checks"])

    def test_on_accepts_an_independently_confirmed_answer(self):
        with On():
            out = TC.run_tool(TC.FakeShopPage(), TC.tool(), VALUE)
        self.assertEqual(out["status"], "ok", out.get("why"))
        self.assertEqual(out["checks"]["attribute_verify"]["verdict"], AV.ACCEPT)
        self.assertEqual(out["model_calls"], 0)

    def test_on_withholds_an_unknown_availability_phrase(self):
        with On(), with_text(lambda t: t.replace("Auf Lager", "Ware befindet sich im Nachschubprozess (Lagerbestand)")):
            out = TC.run_tool(TC.FakeShopPage(), TC.tool(), VALUE)
        self.assertEqual(out["status"], "incomplete")
        self.assertIsNone(out["output"])
        # the extractor's own vocabulary read no class and answered "not stated"; the buy box DOES talk about it
        self.assertEqual(out["unverified_output"]["availability"], "not stated")
        self.assertFalse(out["accepted"])

    def test_the_model_resolves_an_open_attribute(self):
        asked = []

        def model(prompt, schema):
            asked.append(prompt)
            return {"choice": "in_stock", "line": 1, "calls": 1, "usd": 0.0004}
        with On(AV.Resolver(model)), with_text(lambda t: t.replace("Auf Lager", "Auf Lager\nLagerbestand: reichlich")):
            out = TC.run_tool(TC.FakeShopPage(), TC.tool(), VALUE)
        self.assertEqual(out["status"], "ok", out.get("why"))
        self.assertEqual(len(asked), 1)
        self.assertEqual(out["model_calls"], 1)
        self.assertEqual(out["checks"]["attribute_verify"]["stage"], "model")


class Checker(unittest.TestCase):
    """The probation checker's verdict on a product answer from the oracle's render (``judge``)."""

    def case(self, render_text, availability="LimitedAvailability"):
        tool = {"workflow": "product_lookup", "site": "teufel.example"}
        url = "https://teufel.example/massive-mv5c"
        out = {"status": "ok", "output": {"name": "Teufel MASSIVE + Shure MV5C", "price": 219.99, "currency": "EUR",
                                          "availability": availability, "url": url}}
        ev = {"links": [{"url": url, "text": "Teufel MASSIVE + Shure MV5C"}]}
        items = {url: {"url": url, "title": "Teufel MASSIVE + Shure MV5C", "h1": ["Teufel MASSIVE + Shure MV5C"],
                       "text": render_text}}
        return tool, out, ev, items

    TEXT = "RESTPOSTEN\nTeufel MASSIVE + Shure MV5C\n219,99 €\nIn den Warenkorb\nAuf Lager, in 2 – 4 Werktagen bei dir"

    def test_the_old_checker_agrees_with_the_misread(self):
        # teufel.de: the shared vocabulary merges "a few left" into "in stock" and agrees
        tool, out, ev, items = self.case(self.TEXT)
        self.assertEqual(RCK.judge(tool, "x", out, ev, items, availability=True, attribute_verify=False)["verdict"],
                         "agrees")

    def test_the_independent_checker_does_not(self):
        tool, out, ev, items = self.case(self.TEXT)
        v = RCK.judge(tool, "x", out, ev, items, attribute_verify=True, resolver=AV.Resolver(None))
        self.assertEqual(v["verdict"], "disagrees")
        self.assertIn("attribute verify", v["why"])

    def test_the_independent_checker_agrees_with_a_confirmed_answer(self):
        tool, out, ev, items = self.case(self.TEXT, availability="InStock")
        v = RCK.judge(tool, "x", out, ev, items, attribute_verify=True, resolver=AV.Resolver(None))
        self.assertEqual(v["verdict"], "agrees", v["why"])

    def test_not_listed_still_disagrees(self):
        tool, out, ev, items = self.case(self.TEXT, availability="InStock")
        ev = {"links": [{"url": "https://teufel.example/other", "text": "Other"}]}
        self.assertEqual(RCK.judge(tool, "x", out, ev, items, attribute_verify=True,
                                   resolver=AV.Resolver(None))["verdict"], "disagrees")

    def test_the_oracle_checker_passes_the_switch(self):
        c = RCK.OracleChecker(lambda: None, lambda p: None, attribute_verify=True, resolver=AV.Resolver(None))
        self.assertTrue(c.attribute_verify)
        r = RCK.RecordedChecker(lambda t, v: None, attribute_verify=True)
        self.assertTrue(r.attribute_verify)


class DesktopEffect(unittest.TestCase):
    def snap(self, value):
        return AXSnapshot(elements=[AXElementDesc(role="AXTextField", title="Point", identifier="point", value=value)])

    def verdict(self, independent):
        before, after = self.snap(""), self.snap("North gate")
        diff = AccessibilityTree.diff(None, before, after)
        effect = {"changes": [{"op": "value", "role": "AXTextField", "identifier": "point", "value": SE.INPUT}]}
        return SE.evaluate(effect, before, after, diff, input_value="North gate", independent=independent)

    def test_off_or_without_an_independent_reading_unchanged(self):
        os.environ[AV.SETTING] = "off"                  # on by default
        self.assertEqual(self.verdict([("file", "boat shed")]).kind, SE.OBSERVED)
        with On():
            self.assertEqual(self.verdict(None).kind, SE.OBSERVED)

    def test_on_a_differing_file_reading_is_not_observed(self):
        with On():
            v = self.verdict([("file", "Collection point: boat shed\n")])
            self.assertEqual(v.kind, SE.NOT_OBSERVED)
            self.assertIn("independent reading", v.reason)
            self.assertEqual(self.verdict([("file", "Collection point: North gate\n")]).kind, SE.OBSERVED)

    def test_the_executor_reader_hook_defaults_to_none(self):
        from anyuse.execution.executor import StateGraphExecutor
        self.assertIsNone(StateGraphExecutor.independent_reader)


if __name__ == "__main__":
    unittest.main()
