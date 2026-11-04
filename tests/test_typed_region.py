"""Tier-2 typed search: visible-only parsing and the typed result region.

Offline fixtures only (no browser, no network). Each fixture is one generic
failure shape of a typed search: an in-page filter that hides rows, a result
list that appears next to a static "featured" list, a short list for one value.
"""
from __future__ import annotations

import unittest

from anyuse.execution import html_extract as X

ROWS = ["alpha parser", "beta server", "gamma client", "delta parser", "epsilon tool"]


def listing(show=None, style="display: none"):
    lis = []
    for r in ROWS:
        hide = show is not None and not any(w in r for w in show.split())
        attr = f' style="{style}"' if hide and style else (" hidden" if hide else "")
        lis.append(f'<li class="pkg"{attr}><a>{r}</a></li>')
    feat = "".join(f'<div class="feat"><span>featured item {i}</span></div>' for i in range(4))
    return (f'<html><body><input id="q"><div class="side">{feat}</div>'
            f'<ul class="list">{"".join(lis)}</ul></body></html>')


class VisibleOnly(unittest.TestCase):

    def test_default_parse_keeps_hidden_nodes(self):
        dom = listing(show="parser")
        self.assertEqual(len(X.extract(dom, "ul.list li.pkg")), 5)

    def test_visible_only_drops_inline_hidden_and_hidden_attribute(self):
        for style in ("display: none", "visibility:hidden", ""):
            dom = listing(show="parser", style=style)
            got = X.extract(dom, "ul.list li.pkg", visible_only=True)
            self.assertEqual(got, ["alpha parser", "delta parser"], style)

    def test_aria_hidden_true_is_hidden_false_is_not(self):
        dom = ('<ul><li aria-hidden="true">a b</li><li aria-hidden="false">c d</li>'
               '<li>e f</li></ul>')
        self.assertEqual(X.extract(dom, "ul li", visible_only=True), ["c d", "e f"])


class TypedRegion(unittest.TestCase):

    def test_in_page_filter_that_hides_rows_yields_ground_truth(self):
        pages = [("parser", listing("parser")), ("server", listing("server")),
                 ("client", listing("client"))]
        # the old derivation sees the unchanged full list
        self.assertEqual(X.derive_list_items([d for _v, d in pages])[0], "")
        sel, items, how = X.derive_typed_items(pages, before=listing())
        self.assertEqual(sel, "ul.list li.pkg")
        self.assertEqual(items, ["alpha parser", "delta parser"])
        self.assertIn("name their value", how)

    def test_results_that_appear_beside_static_furniture(self):
        def page(value, n):
            res = "".join(f'<article class="hit"><h3>{value} result {i}</h3></article>'
                          for i in range(n))
            feat = "".join(f'<div class="feat"><span>news {i} {value[:1]}</span></div>'
                           for i in range(5))
            return f'<body><section class="f">{feat}</section><main>{res}</main></body>'
        before = '<body><section class="f"><div class="feat"><span>x</span></div></section></body>'
        sel, items, _how = X.derive_typed_items([("kernel", page("kernel", 4)),
                                                 ("shell", page("shell", 2))], before=before)
        self.assertEqual(sel, "main article.hit")
        self.assertEqual(items[0], "kernel result 0")

    def test_short_list_on_the_first_value_still_found_from_another(self):
        def page(value, n):
            return "<ol class='r'>" + "".join(f"<li>{value} hit {i}</li>" for i in range(n)) + "</ol>"
        sel, items, _ = X.derive_typed_items([("rare", page("rare", 1)),
                                              ("common", page("common", 6))])
        self.assertEqual(sel, "ol.r li")
        self.assertEqual(items, ["rare hit 0"])

    def test_unchanged_page_claims_nothing(self):
        dom = listing()
        sel, items, why = X.derive_typed_items([("a", dom), ("b", dom)], before=dom)
        self.assertEqual((sel, items), ("", []))
        self.assertIn("no visible repeated group", why)

    def test_needs_two_pages(self):
        self.assertEqual(X.derive_typed_items([("a", listing())])[0], "")


def big_listing(show=None):
    """12 rows, 3 per word, so every value leaves >= 3 visible rows (the
    evidence floor of the proof)."""
    rows = [f"{w} package {n}" for w in TypedProof.WORDS for n in ("one", "two", "three")]
    lis = "".join(f'<li class="pkg"{"" if show is None or show in r else " hidden"}>'
                  f"<a>{r}</a></li>" for r in rows)
    return f'<html><body><input id="q"><ul class="list">{lis}</ul></body></html>'


class TypedProof(unittest.TestCase):
    """End to end through the unchanged learn loop, offline (fake typed opener)."""

    WORDS = ["parser", "server", "client", "tool"]

    def _prove(self, with_before: bool):
        import os
        import test_discover_v4 as V4
        from anyuse.execution import discover as D
        from anyuse.execution import discover_replay as R
        from anyuse.execution.agent_browser import AgentRun
        from anyuse.execution.api_step import ApiStepExecutor
        os.environ["ANYUSE_API_MODALITY"] = "on"

        def typed_open(url, value, spec):
            return AgentRun(value=value, start_url=url, final_url=url,
                            dom=big_listing(value), text=f"{value} results",
                            before_dom=big_listing() if with_before else "",
                            steps=[{"action": "type"}], typed=[value])
        replay = R.GuardedReplay(lambda u, v: AgentRun(error="not used"), typed_open)
        f = V4.fetcher(lambda u: (404, {}, ""))
        cap = D.Capability(cap="search", template=V4.O + "/", where="type", param="q",
                           values=list(self.WORDS), source="typed-replay",
                           input_spec={"index": 0, "name": "", "id": "q"})
        ex = ApiStepExecutor(transport=D.PacedTransport(
            f, inner=V4.Transport(lambda u: (404, {}, ""))))
        D.prove_capability(cap, V4.O, f, executor=ex, proof_preflight=lambda c, r: "",
                           replay=replay)
        return cap

    def test_hiding_filter_is_proved_at_tier_2_visible_only(self):
        cap = self._prove(with_before=True)
        self.assertEqual(cap.status, "proved", cap.reason)
        self.assertEqual(cap.proof["tier"], 2)
        self.assertTrue(cap.proof["replay_tool"]["visible_only"])

    def test_without_the_pre_typing_page_nothing_changes(self):
        cap = self._prove(with_before=False)
        self.assertNotEqual(cap.status, "proved")
        self.assertIn("no repeated group", cap.reason)


if __name__ == "__main__":
    unittest.main()
