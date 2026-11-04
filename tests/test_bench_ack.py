"""The bench's public-host acknowledgement (execution/web_runtime/bench_ack.py), offline.

What must hold: without ``ANYUSE_BENCH_ACK_PUBLIC_HOSTS=1`` nothing is written;
with it, the flag is REFUSED unless ANYUSE_DATA_DIR and the registry both sit
under a scratch root and not in the real ~/.anyuse; an entry names
``acknowledged_by="bench"`` and its reason, never carries a robots override, and
every acknowledged host is printed. No network, no browser, no model.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))

import bench_ack as BA  # noqa: E402
from anyuse.execution.api_permission import check_permission, host_acknowledged  # noqa: E402


class BenchAck(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "home", ".anyuse")
        os.makedirs(self.data)
        self.registry = os.path.join(self.data, "api_hosts.json")
        self.env = {"ANYUSE_DATA_DIR": self.data, "ANYUSE_API_HOSTS_FILE": self.registry,
                    BA.FLAG: "1"}
        self.said = []

    def tearDown(self):
        self.tmp.cleanup()

    def ack(self, hosts, env=None, robots=lambda h: True):
        return BA.acknowledge_public_hosts(hosts, robots, env=env or self.env,
                                           echo=self.said.append)

    def test_off_without_the_flag(self):
        env = dict(self.env)
        env.pop(BA.FLAG)
        self.assertEqual(self.ack(["ex.test"], env=env), {})
        env[BA.FLAG] = "yes"                          # only the literal "1" counts
        self.assertEqual(self.ack(["ex.test"], env=env), {})
        self.assertFalse(os.path.exists(self.registry))

    def test_refused_in_the_real_anyuse_dir(self):
        env = dict(self.env, ANYUSE_DATA_DIR=BA.real_anyuse_dir(), ANYUSE_API_HOSTS_FILE="")
        with self.assertRaises(BA.BenchAckRefused):
            self.ack(["ex.test"], env=env)

    def test_refused_outside_a_scratch_root(self):
        non_scratch = os.path.join(os.path.dirname(BA.real_anyuse_dir()), "anyuse-not-scratch")
        for outside in (non_scratch, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            env = dict(self.env, ANYUSE_DATA_DIR=outside,
                       ANYUSE_API_HOSTS_FILE=os.path.join(outside, "api_hosts.json"))
            with self.assertRaises(BA.BenchAckRefused):
                self.ack(["ex.test"], env=env)
            self.assertFalse(os.path.exists(os.path.join(outside, "api_hosts.json")))

    def test_refused_when_the_registry_escapes_the_data_dir(self):
        env = dict(self.env, ANYUSE_API_HOSTS_FILE=os.path.join(
            BA.real_anyuse_dir(), "api_hosts.json"))
        with self.assertRaises(BA.BenchAckRefused):
            self.ack(["ex.test"], env=env)
        env = dict(self.env, ANYUSE_API_HOSTS_FILE=os.path.join(self.tmp.name, "x.json"))
        with self.assertRaises(BA.BenchAckRefused):
            self.ack(["ex.test"], env=env)

    def test_refused_without_a_data_dir(self):
        with self.assertRaises(BA.BenchAckRefused):
            self.ack(["ex.test"], env={BA.FLAG: "1"})

    def test_acknowledges_marks_and_prints(self):
        out = self.ack(["ex.test", "walled.test", "ex.test"],
                       robots=lambda h: h != "walled.test")
        self.assertEqual(out, {"ex.test": "acknowledged",
                               "walled.test": "skipped: robots.txt does not allow "
                                              "both tokens at the root"})
        with open(self.registry) as fh:
            data = json.load(fh)
        rec = data["ex.test"]
        self.assertTrue(rec["acknowledged"])
        self.assertEqual(rec["acknowledged_by"], "bench")
        self.assertEqual(rec["by"], "bench")
        self.assertIn("benchmark convention", rec["reason"])
        self.assertNotIn("robots_override", rec)
        self.assertNotIn("walled.test", data)
        self.assertTrue(host_acknowledged("ex.test", self.registry))
        self.assertTrue(any("bench-ack ex.test" in s for s in self.said))
        self.assertTrue(any("SKIP walled.test" in s for s in self.said))

    def test_a_host_registered_in_another_case_keeps_one_entry(self):
        # host names ignore case: the bench marks the entry that is there
        with open(self.registry, "w") as fh:
            json.dump({"Ex.Test": {"acknowledged": True, "note": "by hand"}}, fh)
        self.assertEqual(self.ack(["ex.test"]), {"ex.test": "acknowledged"})
        with open(self.registry) as fh:
            data = json.load(fh)
        self.assertEqual(list(data), ["Ex.Test"])
        self.assertEqual(data["Ex.Test"]["acknowledged_by"], "bench")
        self.assertTrue(host_acknowledged("ex.test", self.registry))

    def test_robots_still_respected_after_the_ack(self):
        # own host name and no on-disk robots cache: other test files share the
        # guard's scratch data dir and must not inherit this disallow
        import anyuse.execution.api_permission as AP
        host = "bench-ack-robots.test"
        self.ack([host])
        old = os.environ.get("ANYUSE_ROBOTS_TTL_S")
        os.environ["ANYUSE_ROBOTS_TTL_S"] = "0"
        try:
            v = check_permission(f"https://{host}/search?q=x", hosts_path=self.registry,
                                 policy="respect",
                                 fetcher=lambda u: "User-agent: *\nDisallow: /search\n")
        finally:
            AP._ROBOTS_CACHE.clear()
            if old is None:
                os.environ.pop("ANYUSE_ROBOTS_TTL_S", None)
            else:
                os.environ["ANYUSE_ROBOTS_TTL_S"] = old
        self.assertTrue(v.acknowledged)
        self.assertFalse(v.may_prefer, "acknowledged, but robots still says no")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
