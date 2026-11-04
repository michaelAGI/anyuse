"""Government sites (execution/gov_hosts.py, execution/web_runtime/never_contact.py). Offline:
the declared list; every bench entry point that chooses hosts skips them whatever the setting;
the request-time refusal on every path follows ANYUSE_REFUSE_GOV_HOSTS (off by default) while
the never-contact list is refused in every mode."""
from __future__ import annotations

import glob
import inspect
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

from anyuse.execution import gov_hosts as GH  # noqa: E402
from anyuse.execution import web_scope as W  # noqa: E402
import never_contact as NC  # noqa: E402

#: named by the coordinator from the committed corpora
NAMED = ("eric.ed.gov", "www.congress.gov", "catalogue.data.govt.nz", "data.gov.ie",
         "www.ris.bka.gv.at", "www.esa.int", "www.wipo.int", "dane.gov.pl", "data.europa.eu",
         "www.ema.europa.eu", "pubmed.ncbi.nlm.nih.gov", "www.parks.ca.gov",
         "www.sl.nsw.gov.au", "www.bea.gov")


class DeclaredList(unittest.TestCase):
    def test_named_hosts_are_government(self):
        for h in NAMED:
            self.assertTrue(GH.government_host(h), h)

    def test_list_patterns(self):
        for h in ("www.army.mil", "www.gov.uk", "www.service.gov.uk", "www.stats.govt.nz",
                  "www.canada.ca", "open.canada.ca", "www150.statcan.gc.ca",
                  "www.bfs.admin.ch", "www.legifrance.gouv.fr",
                  "www.boe.gob.es", "www.data.go.jp", "www.bmi.bund.de",
                  "www.stmwi.bayern.de", "www.gov.scot", "www.state.nj.us", "www.who.int"):
            self.assertTrue(GH.government_host(h), h)

    def test_ordinary_hosts_are_not(self):
        for h in ("www.governmentjobs-review.com", "www.govtrack.us", "www.bayern-tourismus.com",
                  "www.go.com", "www.mccormick.com", "www.bbc.co.uk",
                  "www.rightmove.co.uk", "localhost", ""):
            self.assertEqual(GH.government_host(h), "", h)

    def test_non_suffix_public_bodies_named_by_the_coordinator(self):
        for h in ("www.bundesregierung.de", "www.muenchen.de", "www.elysee.fr",
                  "www.regjeringen.no", "www.nhs.uk", "www.arbeitsagentur.de",
                  "www.bundesbank.de", "www.bundestag.de", "www.destatis.de", "www.kb.nl",
                  "www.iwm.org.uk", "www.library.wales", "www.rbge.org.uk"):
            self.assertTrue(GH.listed_government_host(h), h)
            self.assertTrue(GH.government_host(h), h)

    def test_more_declared_bodies_and_zones(self):
        for h in ("www.gov.bc.ca", "www.gouv.qc.ca", "www.bl.uk", "www.bnf.fr",
                  "www.zh.ch", "www.stadt-zuerich.ch", "www.comune.milano.it", "www.un.org",
                  "www.ecb.europa.eu", "www.banque-france.fr", "www.si.edu"):
            self.assertTrue(GH.listed_government_host(h), h)

    def test_heuristic_hits_block_fail_closed(self):
        for h in ("gov.example.com", "www.ministerium-xyz.de", "stats.council-x.org"):
            self.assertEqual(GH.listed_government_host(h), "", h)
            self.assertIn("fail-closed", GH.government_host(h), h)
            self.assertEqual(GH.government_host(h, fail_closed=False), "", h)

    def test_looks_governmental_is_report_only(self):
        self.assertTrue(GH.looks_governmental("www.congress-center.example"))
        self.assertEqual(GH.looks_governmental("www.senat.fr"), "", "listed now, not 'looks'")
        self.assertEqual(GH.looks_governmental("www.congress.gov"), "", "listed, not 'looks'")
        self.assertEqual(GH.looks_governmental("www.mccormick.com"), "")

    def test_never_contact_entry(self):
        self.assertEqual(NC.refusal("www.bea.gov", never=set()), "")            # the rule is off by default
        with mock.patch.dict(os.environ, {"ANYUSE_REFUSE_GOV_HOSTS": "on"}):
            self.assertEqual(NC.refusal("www.bea.gov", never=set()), NC.GOV_SKIP)
        self.assertEqual(NC.refusal("www.example.org", never={"www.example.org"}),
                         "on the never-contact list")
        self.assertEqual(NC.refusal("www.example.org", never=set()), "")
        self.assertTrue(NC.is_government("data.gov.ie"))


class EntryPoints(unittest.TestCase):
    def test_web_scope_never_allows_a_government_host_when_the_rule_is_on(self):
        with mock.patch.dict(os.environ, {"ANYUSE_REFUSE_GOV_HOSTS": "on"}):
            for tp in ("allow_all", "allow_with_blocklist"):
                s = W.WebScope(third_party=tp, own_hosts=True, blocklist_domains=set())
                v = s.classify("www.example.org", "https://www.bundestag.de/x.js", "GET", "Script")
                self.assertFalse(v.ok, tp)
                self.assertIn("government", v.why)
            s = W.WebScope(own_hosts=True, blocklist_domains=set())
            self.assertFalse(s.classify("www.example.gov", "https://cdn.example.gov/a.js", "GET",
                                        "Script").ok)
        # by default a government host is judged like any other host
        s = W.WebScope(third_party="allow_all", own_hosts=True, blocklist_domains=set())
        self.assertTrue(s.classify("www.example.org", "https://www.bundestag.de/x.js", "GET", "Script").ok)
        self.assertTrue(W.WebScope(own_hosts=True, blocklist_domains=set()).classify(
            "www.example.gov", "https://cdn.example.gov/a.js", "GET", "Script").ok)

    def test_bench_site_choice_stays_on_the_list_whatever_the_setting(self):
        import discover_bench as DB
        import web_arch_live as WAL
        self.assertNotIn("ANYUSE_REFUSE_GOV_HOSTS", os.environ)
        self.assertTrue(DB._government("www.congress.gov"))
        rows = [("www.bundestag.de", "other", ""), ("www.example-shop.com", "shop", "")]
        frame, dropped = WAL.build_frame(rows, set(), set())
        self.assertEqual(dropped.get("government"), ["www.bundestag.de"])
        self.assertEqual([r.get("host") or r.get("site") for r in frame], ["www.example-shop.com"])

    def test_discover_bench_skips_before_robots(self):
        import discover_bench as DB

        class Robots:
            notes = {}

            def root_ok(self, h):
                raise AssertionError(f"robots fetched for {h}")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"n": 2, "name": "X", "hosts": ["www.congress.gov", "data.gov.ie"]}, fh)
        state = {}
        with mock.patch.object(DB, "never_contact", lambda: set()):
            self.assertEqual(DB.choose_hosts("", Robots(), state, fh.name), [])
        self.assertEqual(set(state["skipped"]), {"www.congress.gov", "data.gov.ie"})

    def test_ladder_verify_scope_skips(self):
        import corpus_sample4 as C4
        import ladder_verify as LR
        wid = next(iter(LR.OLD_TIER))
        with mock.patch.dict(C4.BY_ID, {wid: dict(C4.BY_ID[wid], site="www.congress.gov")}), \
                mock.patch.object(LR, "never_contact", lambda: set()):
            _run, out = LR.scope({})
        self.assertEqual(out[wid], "skipped: government host (owner rule)")


class GovSetting(unittest.TestCase):
    """ANYUSE_REFUSE_GOV_HOSTS: off by default; on, every request-time path refuses a government host."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {})
        self.env.start()
        os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS", None)
        from anyuse.execution import api_permission as AP
        self.AP = AP
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = os.path.join(self.tmp.name, "robots_cache.json")
        AP._ROBOTS_CACHE.clear()
        AP._ROBOTS_NOTES.clear()

    def tearDown(self):
        self.AP._ROBOTS_CACHE.clear()
        self.AP._ROBOTS_NOTES.clear()
        self.env.stop()
        self.tmp.cleanup()

    def paths(self):
        from anyuse.execution import demo_compile as DC
        from anyuse.execution import harness_tools as HT
        from anyuse.execution import site_modes
        from anyuse.execution import surface_discovery as SD
        from anyuse.execution.api_step import HttpxTransport
        return {"gov_hosts": GH.government_refusal("www.congress.gov"),
                "result_scope": "" if DC._host_ok("cdn.example.gov", "www.example.gov") else "refused",
                "browser_ua_hop": "" if HttpxTransport._ua_hop_ok("www.bund.de", "bund.de") else "refused",
                "contact_refusal": W.contact_refusal("api.data.europa.eu"),
                "harness_tools": HT.gov_refusal("https://www.bundestag.de/"),
                "site_modes": site_modes.host_block_reason("www.bund.de"),
                "never_contact": NC.refusal("www.bea.gov", never=set()),
                "surface_discovery": SD.discover_site("service.gov.uk").get("refused", "")}

    def test_the_rule_is_off_by_default_and_never_contact_stays(self):
        self.assertEqual(self.paths(), {k: "" for k in self.paths()})
        self.assertTrue(GH.government_host("www.congress.gov"))                 # the list itself is unchanged
        from anyuse.execution import site_modes
        with mock.patch.object(site_modes, "_never_contact", lambda: {"never.example.test"}):
            self.assertEqual(W.contact_refusal("never.example.test"), "on NEVER_CONTACT")
            self.assertTrue(site_modes.host_block_reason("never.example.test"))
        self.assertFalse(W.WebScope.from_settings().refuse_gov)

    def test_on_it_refuses_on_every_path(self):
        os.environ["ANYUSE_REFUSE_GOV_HOSTS"] = "on"
        got = self.paths()
        self.assertTrue(all(got.values()), got)
        self.assertEqual(got["never_contact"], NC.GOV_SKIP)
        self.assertTrue(W.WebScope.from_settings().refuse_gov)
        from anyuse.execution import harness_product as HP
        os.environ["ANYUSE_ROBOTS"] = "ignore"
        gate = HP.robots_gate()
        self.assertFalse(gate("https://www.example.gov/x"))
        self.assertEqual(gate.overrides, {})
        # off again: the same gate kind lets it through and records the override
        os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS")
        gate = HP.robots_gate()
        self.assertTrue(gate("https://www.example.gov/x"))
        self.assertIn("www.example.gov", gate.overrides)

    def test_the_setting_is_registered_and_documented(self):
        from anyuse import settings as S
        spec = S.SETTINGS["ANYUSE_REFUSE_GOV_HOSTS"]
        self.assertIs(spec.default, False)
        self.assertTrue(spec.security_relevant)
        self.assertFalse(spec.needs_confirmation(True))
        with open(os.path.join(HERE, "docs", "SETTINGS.md"), encoding="utf-8") as fh:
            self.assertIn("| `ANYUSE_REFUSE_GOV_HOSTS` * |", fh.read())


if __name__ == "__main__":
    unittest.main()
