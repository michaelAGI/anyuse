"""Attach: item-page sessions go through the detail proof, not the list proof.

Item-page sessions take discovery's detail-specific proof (``discover.page_fields``),
with roles from ``proof_split.assign``. Offline: the sessions are recorded fixtures
(``testdata/attach_detail``), and the "site" is a transport that serves the
documents recorded in those sessions. No socket, no browser.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import tempfile
import unittest

from anyuse.execution import attach as A
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(HERE, "testdata", "attach_detail")


def load_sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(FIX, "session_*.json"))):
        with open(p, encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


class SiteFromRecording:
    """Serves the documents recorded in the sessions; logs every request."""

    def __init__(self, sessions, override=None):
        self.pages = {}
        for s in sessions:
            for r in s["network"]:
                if r["resource_type"] == "Document" and r.get("body"):
                    self.pages[r["url"]] = r["body"]
        self.pages.update(override or {})
        self.calls = []

    def send(self, method, url, headers, body, timeout_s):
        self.calls.append((method, url))
        if url not in self.pages:
            return ApiResponse(404, {"Content-Type": "text/plain"}, "not recorded")
        return ApiResponse(200, {"Content-Type": "text/html"}, self.pages[url])


def compile_detail(sessions, site, **kw):
    return A.compile_sessions(sessions, preflight=lambda c: "", transport=site, **kw)


class DetailRouting(unittest.TestCase):

    def test_item_sessions_are_detected_and_never_use_the_list_ground_truth(self):
        from anyuse.execution import learn_loop
        sessions = load_sessions()
        site = SiteFromRecording(sessions)
        called = []
        real = learn_loop.derive_ground_truth
        learn_loop.derive_ground_truth = lambda recs: called.append(recs) or real(recs)
        try:
            compiled, proof, report, recs = compile_detail(sessions, site)
        finally:
            learn_loop.derive_ground_truth = real
        self.assertEqual(called, [])
        self.assertEqual(report["shape"], "detail")
        self.assertEqual(report["proof_kind"], "detail_fields")
        self.assertEqual(compiled.template.url,
                         "https://pkgs.example.test/package/{package}")
        self.assertEqual(compiled.template.extract_kind, "detail_fields")
        self.assertEqual(compiled.template.extract, {"h1": "h1"})
        self.assertEqual({r["cls"] for r in recs}, {"detail"})
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(report["field"], "h1")

    def test_four_sessions_prove_on_held_out_keys_fetched_fresh(self):
        sessions = load_sessions()
        site = SiteFromRecording(sessions)
        _c, proof, report, _r = compile_detail(sessions, site)
        # 1 compile (vim), 1 selection (git), 2 validation (curl, make)
        self.assertEqual([c.value for c in proof.cases], ["curl", "make"])
        self.assertEqual(site.calls, [("GET", "https://pkgs.example.test/package/curl"),
                                      ("GET", "https://pkgs.example.test/package/make")])
        self.assertEqual(report["status"], "proved")
        self.assertTrue(report["selection_held_out"])
        self.assertEqual(proof.split["validation"], 2)
        self.assertEqual(proof.split["selection"], 1)
        self.assertIn("Field proof: named fields (h1)", proof.scope)
        self.assertIn("held-out", proof.scope)
        self.assertEqual([c.expected for c in proof.cases], ["curl 8.9.1", "make 4.4.1"])

    def test_three_sessions_are_at_most_checked(self):
        sessions = load_sessions()[:3]
        _c, proof, report, _r = compile_detail(sessions, SiteFromRecording(sessions))
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(report["status"], "checked (selection not held out)")
        self.assertFalse(report["selection_held_out"])

    def test_a_fresh_page_that_differs_from_the_captured_one_fails(self):
        sessions = load_sessions()
        site = SiteFromRecording(sessions, override={
            "https://pkgs.example.test/package/make":
                "<html><body><h1>make 9.9 (moved)</h1></body></html>"})
        _c, proof, report, _r = compile_detail(sessions, site)
        self.assertFalse(proof.passed)
        self.assertEqual(report["status"], "not proved")
        self.assertIn("differs from the captured", proof.reason)

    def test_a_validation_session_off_the_template_fails_that_key(self):
        sessions = load_sessions()
        s = sessions[3]
        s["snapshots"][-1]["url"] = "https://pkgs.example.test/package/make/files"
        s["snapshots"][-1]["dom"] = s["snapshots"][-1]["dom"].replace("make 4.4.1",
                                                                       "Files of make")
        site = SiteFromRecording(sessions)
        _c, proof, _rep, _r = compile_detail(sessions, site, shape="detail")
        self.assertFalse(proof.passed)
        self.assertIn("is not https://pkgs.example.test/package/make", proof.reason)
        self.assertNotIn(("GET", "https://pkgs.example.test/package/make"), site.calls)

    def test_the_key_is_inferred_from_the_item_path(self):
        sessions = load_sessions()
        for s in sessions:
            s["input"] = None
        name, values, how = A.infer_param(sessions)
        self.assertEqual((name, values), ("package", ["vim", "git", "curl", "make"]))
        self.assertIn("path", how)
        _c, proof, report, _r = compile_detail(sessions, SiteFromRecording(sessions))
        self.assertEqual(report["status"], "proved", proof.reason)

    def test_search_sessions_stay_on_the_list_proof(self):
        mk = lambda v: {"snapshots": [{"url": f"https://ex.test/search?q={v}",
                                       "dom": "<ul></ul>", "text": v}]}
        self.assertEqual(A.session_shape([mk("a1"), mk("b2")], ["a1", "b2"]), "list")
        mk2 = lambda v: {"snapshots": [{"url": f"https://ex.test/s/{v}?q={v}",
                                        "dom": "<ul></ul>", "text": v}]}
        self.assertEqual(A.session_shape([mk2("a1")], ["a1"]), "list")
        mk3 = lambda v: {"snapshots": [{"url": f"https://ex.test/search/{v}",
                                        "dom": "<ul></ul>", "text": v}]}
        self.assertEqual(A.session_shape([mk3("a1")], ["a1"]), "list")   # path search

    def test_the_recorded_pages_decide_a_path_search_in_any_language(self):
        def search(word, v, echo=True, home=""):
            rows = "".join(f"<li class='hit'><a href='/product/{v}-{i}'>{v} model {i}</a></li>"
                           for i in range(3))
            field = f"<input type='search' name='q' value='{v}'>" if echo else ""
            snaps = [{"url": "https://shop.test/", "dom": f"<html><body>{home}</body></html>"},
                     {"url": f"https://shop.test/{word}/{v}",
                      "dom": f"<html><body><form>{field}</form><ul>{rows}</ul></body></html>",
                      "text": v}]
            return {"snapshots": snaps}
        vals = ["fiets", "lamp", "stoel"]
        # Dutch, Portuguese, Polish: no listed word, the result page itself says it is a search
        self.assertEqual(A.session_shape([search("zoeken", v) for v in vals], vals), "list")
        self.assertEqual(A.session_shape([search("busca", v, echo=False) for v in vals], vals),
                         "list")
        self.assertEqual(A.session_shape([search("szukaj", v) for v in vals], vals), "list")
        # the listed words still decide when the pages say nothing
        bare = [{"snapshots": [{"url": f"https://shop.test/search/{v}", "dom": "<ul></ul>",
                                "text": v}]} for v in vals]
        self.assertEqual(A.session_shape(bare, vals), "list")
        # a home page that lists the inputs as popular searches, and result pages that list
        # results: the pages disagree, the word decides
        popular = "<ul>" + "".join(f"<li class='pop'>{v}</li>" for v in vals) + "</ul>"
        self.assertEqual(A.session_shape([search("search", v, home=popular) for v in vals], vals),
                         "list")

    def test_an_item_page_another_page_lists_is_an_item_under_any_path_word(self):
        vals = ["quartz", "pyrite", "galena"]
        directory = ("<html><body><ul>" + "".join(
            f"<li class='m'><a href='/find/{v}'>{v.title()}</a></li>" for v in vals) +
            "</ul></body></html>")

        def item(v, related=""):
            return {"snapshots": [
                {"url": "https://rocks.test/minerals", "dom": directory},
                {"url": f"https://rocks.test/find/{v}",
                 "dom": f"<html><body><h1>{v.title()}</h1><p>A mineral.</p>{related}</body></html>",
                 "text": v}]}
        self.assertEqual(A.session_shape([item(v) for v in vals], vals), "detail")

        # links to related items of the same kind name the input but are siblings, not results
        def alone(v):
            rel = "<ul>" + "".join(f"<li class='r'><a href='/package/{v}-{x}'>{v}-{x}</a></li>"
                                   for x in ("plug", "airline")) + "</ul>"
            return {"snapshots": [{"url": f"https://pkgs.test/package/{v}",
                                   "dom": f"<html><body><h1>{v}</h1>{rel}</body></html>",
                                   "text": v}]}
        self.assertEqual(A.session_shape([alone(v) for v in ("vim", "git")], ["vim", "git"]),
                         "detail")

    def test_an_item_pages_own_links_that_name_the_input_are_not_results(self):
        # the item's code, its documentation, its versions and its issues each name the input:
        # links to other sites and to the item's own sub-pages, not a list of the site's items
        def item(v):
            links = ("<ul class='links'>"
                     f"<li><a href='https://code.test/{v}/{v}'>{v} source code</a></li>"
                     f"<li><a href='https://docs.test/{v}/'>{v} documentation</a></li>"
                     f"<li><a href='/package/{v}/versions'>All {v} versions</a></li>"
                     f"<li><a href='/package/{v}/issues'>Open {v} issues</a></li></ul>")
            return {"snapshots": [{"url": f"https://pkgs.test/package/{v}",
                                   "dom": f"<html><body><h1>{v}</h1>{links}</body></html>",
                                   "text": v}]}
        self.assertEqual(A.session_shape([item(v) for v in ("vim", "git")], ["vim", "git"]),
                         "detail")
        # rows that link to the site's items of one shape are results, in any path word
        def found(v):
            rows = "".join(f"<li><a href='/produkt/{v}-{i}'>{v} {i}</a></li>" for i in range(3))
            return {"snapshots": [{"url": f"https://sklep.test/szukaj/{v}",
                                   "dom": f"<html><body><ul>{rows}</ul></body></html>",
                                   "text": v}]}
        self.assertEqual(A.session_shape([found(v) for v in ("rower", "lampa")],
                                         ["rower", "lampa"]), "list")

    def test_preflight_refusal_sends_nothing(self):
        sessions = load_sessions()
        site = SiteFromRecording(sessions)
        _c, proof, _rep, _r = A.compile_sessions(sessions, preflight=lambda c: "stop here",
                                                 transport=site)
        self.assertFalse(proof.passed)
        self.assertIn("stop here", proof.reason)
        self.assertEqual(site.calls, [])

    def test_a_template_origin_not_observed_is_refused(self):
        sessions = load_sessions()
        for s in sessions[:2]:
            s["network"] = [r for r in s["network"] if "pkgs.example.test" not in r["url"]]
        site = SiteFromRecording(load_sessions())
        _c, proof, _rep, _r = compile_detail(sessions, site)
        self.assertFalse(proof.passed)
        self.assertIn("origin check refused", proof.reason)
        self.assertEqual(site.calls, [])

    def test_fixtures_are_untouched_by_the_tests(self):
        a = load_sessions()
        b = copy.deepcopy(a)
        compile_detail(a, SiteFromRecording(a))
        self.assertEqual([s["id"] for s in a], [s["id"] for s in b])


class DetailCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ANYUSE_DATA_DIR")
        os.environ["ANYUSE_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ANYUSE_DATA_DIR", None)
        else:
            os.environ["ANYUSE_DATA_DIR"] = self.old
        self.tmp.cleanup()

    def _write_sessions(self):
        sessions = load_sessions()
        for s in sessions:
            A.write_session(s)
        return sessions

    def _invoke_offline(self, sessions, args):
        from click.testing import CliRunner
        from anyuse import cli as cli_mod
        site = SiteFromRecording(sessions)
        real = A.compile_sessions

        def offline(sess, robots_policy=None, shape="auto", **kw):
            return real(sess, preflight=lambda c: "", transport=site, shape=shape)
        A.compile_sessions = offline
        try:
            res = CliRunner().invoke(cli_mod.cli, ["compile", "last", *args], obj={})
        finally:
            A.compile_sessions = real
        return res, site

    def test_compile_last_persists_lists_and_mcp_calls_a_proved_detail_tool(self):
        from anyuse.execution import api_permission
        from anyuse.mcpserver import compiled_tools
        sessions = self._write_sessions()
        template = "https://pkgs.example.test/package/{package}"
        res, site = self._invoke_offline(sessions, ["--confirm", template])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn(f"Detail proof: template {template}", res.output)
        self.assertIn("Status: proved", res.output)
        self.assertIn("Persisted", res.output)

        hosts = os.path.join(self.tmp.name, "hosts.json")
        api_permission.acknowledge_host("pkgs.example.test", note="fixture", path=hosts)
        rows = compiled_tools.list_compiled(hosts_path=hosts)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workflow"], "pkgs_detail")
        self.assertEqual(rows[0]["parameters"], ["package"])
        self.assertEqual(rows[0]["result_shape"], "fields")
        self.assertEqual(rows[0]["proof"]["status"], "proved")
        tools = compiled_tools.executable_typed_tools(hosts_path=hosts)
        self.assertEqual(len(tools), 1)
        self.assertIn("status proved", tools[0]["description"])

        out = compiled_tools.run_typed_tool(
            tools[0]["name"], {"package": "curl"}, hosts_path=hosts,
            executor=ApiStepExecutor(transport=site), require_proved=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"], {"h1": "curl 8.9.1"})

    def test_three_sessions_persist_checked_and_restricted_mcp_refuses_it(self):
        from anyuse.mcpserver import compiled_tools
        sessions = self._write_sessions()
        template = "https://pkgs.example.test/package/{package}"
        res, _site = self._invoke_offline(
            sessions, ["--n", "3", "--name", "checked_detail", "--confirm", template])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Status: checked (selection not held out)", res.output)
        rows = compiled_tools.list_compiled(
            hosts_path=os.path.join(self.tmp.name, "hosts.json"))
        self.assertEqual(rows[0]["proof"]["status"],
                         "checked (selection not held out)")
        out = compiled_tools.run_compiled(
            "checked_detail", {"package": "make"}, require_proved=True,
            hosts_path=os.path.join(self.tmp.name, "hosts.json"))
        self.assertFalse(out["ok"])
        self.assertIn("not 'proved'", out["error"])


if __name__ == "__main__":
    unittest.main()
