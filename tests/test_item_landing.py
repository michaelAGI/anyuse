"""A search that lands on an ITEM page goes to the detail proof, also when
the URL carries the key lower-cased, slugged or with a page extension.

Fixture: four ARM S sessions of www.minerals.net (the search
went straight to /mineral/<key>). Offline: the fresh fetch is a fake transport that serves
each validation session's own captured page.
"""
from __future__ import annotations

import gzip
import json
import os
import types
import unittest

from anyuse.execution import attach as A

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    with gzip.open(os.path.join(HERE, "testdata", "web_sessions",
                                "minerals_item_landing.sessions.json.gz"), "rt") as fh:
        return json.load(fh)


class FakeTransport:
    def __init__(self, pages):
        self.pages, self.sent = pages, []

    def send(self, method, url, headers, body, timeout):
        self.sent.append(url)
        page = self.pages.get(url)
        return types.SimpleNamespace(status=200 if page else 404, text=page or "")


class ItemLanding(unittest.TestCase):

    def test_saved_minerals_sessions_take_the_detail_proof_and_prove(self):
        f = load()
        sessions = f["sessions"]
        pages = {A.result_snapshot(s, v)["url"]: A.result_snapshot(s, v)["dom"]
                 for s, v in zip(sessions, f["values"])}
        self.assertEqual(A.session_shape(sessions, f["values"]), "detail")
        tr = FakeTransport(pages)
        compiled, proof, report, _recs = A.compile_sessions(
            sessions, transport=tr, preflight=lambda c: "")
        self.assertEqual(report["shape"], "detail")
        self.assertEqual(report["template_url"], "https://www.minerals.net/mineral/{q}")
        self.assertEqual(compiled.template.param_transforms, {"q": "lower"})
        self.assertTrue(proof.passed, proof.reason)
        # the fresh requests went to the lower-cased item URLs
        self.assertEqual(sorted(tr.sent), ["https://www.minerals.net/mineral/tridymite",
                                           "https://www.minerals.net/mineral/zincite"])
        url, _h, _b = compiled.template.render({"q": "Zincite"})
        self.assertEqual(url, "https://www.minerals.net/mineral/zincite")

    def test_segment_key_forms(self):
        self.assertEqual(A._segment_key("Quartz", "quartz.htm"), ("lower", ".htm"))
        self.assertEqual(A._segment_key("Rose Quartz", "rose-quartz"), ("slug", ""))
        self.assertEqual(A._segment_key("Rose Quartz", "Rose-Quartz.shtml"), ("hyphen", ".shtml"))
        self.assertEqual(A._segment_key("vim", "vim"), ("identity", ""))
        self.assertIsNone(A._segment_key("vim", "vimrc"))
        self.assertIsNone(A._segment_key("vim", "vim.tar.gz"))

    def test_extension_template_renders_with_the_extension(self):
        t, how = A._detail_template_t("https://ex.org/minerals/quartz.htm", "Quartz", "q")
        self.assertEqual((t, how), ("https://ex.org/minerals/{q}.htm", "lower"))
        self.assertEqual(A._render(t, "q", "Rose Quartz", "slug"),
                         "https://ex.org/minerals/rose-quartz.htm")

    def test_path_search_is_still_a_list(self):
        self.assertFalse(A._in_path("vim", "https://ex.org/search/vim"))
        self.assertTrue(A._in_path("Vim", "https://ex.org/package/vim.html"))


if __name__ == "__main__":
    unittest.main()
