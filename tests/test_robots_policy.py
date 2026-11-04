"""The robots policy as a product setting, and the exact conditions under which
a compiled `api` rung outranks `ax`.

Both are product decisions and both are the kind of thing that
drifts silently, so they are pinned here rather than described in a document.

Offline: the robots fetcher is injected everywhere, the host file is a temp
file, and nothing in this module opens a socket.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

from anyuse.execution import api_permission as perm
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.deterministic import rank_implementations
from anyuse.guides.state_graph import TransitionImpl, UITransition

ROBOTS_DENY = "User-agent: *\nDisallow: /search\n"
ROBOTS_ALLOW = "User-agent: *\nDisallow: /admin\n"
URL = "https://ex.test/search?q=alpha"


def fetcher(text):
    return lambda _url: text


class RobotsPolicyBase(unittest.TestCase):

    def setUp(self):
        perm._ROBOTS_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.hosts = os.path.join(self.tmp.name, "api_hosts.json")
        self._env = {k: os.environ.get(k) for k in
                     ("ANYUSE_ROBOTS_POLICY", "ANYUSE_ROBOTS_CACHE_FILE",
                      "ANYUSE_API_HOSTS_FILE")}
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = os.path.join(self.tmp.name, "rc.json")
        os.environ["ANYUSE_API_HOSTS_FILE"] = self.hosts
        os.environ.pop("ANYUSE_ROBOTS_POLICY", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        perm._ROBOTS_CACHE.clear()
        self.tmp.cleanup()

    def ack(self, **kw):
        perm.acknowledge_host("ex.test", note="test", path=self.hosts, **kw)


class PolicyResolution(RobotsPolicyBase):

    def test_the_default_is_respect(self):
        self.assertEqual(perm.robots_policy(), perm.ROBOTS_RESPECT)

    def test_a_typo_is_respect_not_off(self):
        os.environ["ANYUSE_ROBOTS_POLICY"] = "ingore"
        self.assertEqual(perm.robots_policy(), perm.ROBOTS_RESPECT)

    def test_an_explicit_flag_beats_the_environment(self):
        os.environ["ANYUSE_ROBOTS_POLICY"] = "ignore"
        self.assertEqual(perm.robots_policy("respect"), perm.ROBOTS_RESPECT)

    def test_ignore_must_be_spelled_out(self):
        os.environ["ANYUSE_ROBOTS_POLICY"] = "ignore"
        self.assertEqual(perm.robots_policy(), perm.ROBOTS_IGNORE)


class RespectMode(RobotsPolicyBase):

    def test_a_disallowed_path_is_never_preferred(self):
        self.ack()
        v = perm.check_permission(URL, hosts_path=self.hosts,
                                  fetcher=fetcher(ROBOTS_DENY))
        self.assertFalse(v.may_prefer)
        self.assertEqual(v.policy, "respect")
        self.assertFalse(v.robots_overridden)

    def test_an_allowed_path_on_an_acknowledged_host_may_be_preferred(self):
        self.ack()
        v = perm.check_permission(URL, hosts_path=self.hosts,
                                  fetcher=fetcher(ROBOTS_ALLOW))
        self.assertTrue(v.may_prefer)

    def test_an_unacknowledged_host_is_still_not_preferred(self):
        v = perm.check_permission(URL, hosts_path=self.hosts,
                                  fetcher=fetcher(ROBOTS_ALLOW))
        self.assertFalse(v.may_prefer)

    def test_replay_is_refused_too_not_only_preference(self):
        perm.check_permission(URL, hosts_path=self.hosts, fetcher=fetcher(ROBOTS_DENY))
        self.assertTrue(perm.replay_refusal(URL, hosts_path=self.hosts))

    def test_the_executor_refuses_before_sending(self):
        perm.check_permission(URL, hosts_path=self.hosts, fetcher=fetcher(ROBOTS_DENY))

        class Boom:
            def send(self, *a, **k):                       # pragma: no cover
                raise AssertionError("a robots-disallowed path was fetched")

        tmpl = RequestTemplate(method="GET", url="https://ex.test/search",
                               query={"q": "{q}"}, params=["q"])
        out = ApiStepExecutor(transport=Boom()).execute(tmpl, {"q": "alpha"})
        self.assertFalse(out["success"])
        self.assertTrue(out.get("robots_refused"))


class PerHostOverride(RobotsPolicyBase):

    def test_an_override_needs_a_reason(self):
        with self.assertRaises(ValueError):
            self.ack(ignore_robots=True, robots_reason="   ")

    def test_an_override_lets_a_disallowed_path_be_preferred(self):
        self.ack(ignore_robots=True, robots_reason="my own site")
        v = perm.check_permission(URL, hosts_path=self.hosts,
                                  fetcher=fetcher(ROBOTS_DENY))
        self.assertTrue(v.may_prefer)
        self.assertIn("my own site", v.robots_overridden)
        self.assertIn("OVERRIDDEN", v.reason)

    def test_an_override_is_visible_in_the_serialised_verdict(self):
        self.ack(ignore_robots=True, robots_reason="written permission")
        v = perm.check_permission(URL, hosts_path=self.hosts,
                                  fetcher=fetcher(ROBOTS_DENY))
        self.assertIn("written permission", v.to_dict()["robots_overridden"])

    def test_an_override_is_written_next_to_the_acknowledgement(self):
        self.ack(ignore_robots=True, robots_reason="my own site")
        with open(self.hosts) as fh:
            data = json.load(fh)
        self.assertTrue(data["ex.test"]["robots_override"]["ignore_robots"])
        self.assertEqual(data["ex.test"]["robots_override"]["reason"], "my own site")

    def test_an_override_does_not_leak_to_another_host(self):
        self.ack(ignore_robots=True, robots_reason="my own site")
        self.assertIsNone(perm.host_override("other.test", self.hosts))

    def test_a_plain_acknowledgement_is_not_an_override(self):
        self.ack()
        self.assertIsNone(perm.host_override("ex.test", self.hosts))


class IgnoreMode(RobotsPolicyBase):

    def test_ignore_allows_a_disallowed_path_and_says_so(self):
        self.ack()
        v = perm.check_permission(URL, hosts_path=self.hosts,
                                  fetcher=fetcher(ROBOTS_DENY), policy="ignore")
        self.assertTrue(v.may_prefer)
        self.assertEqual(v.policy, "ignore")
        self.assertIn("user's responsibility", v.robots_overridden)

    def test_ignore_still_requires_the_host_acknowledgement(self):
        v = perm.check_permission(URL, hosts_path=self.hosts,
                                  fetcher=fetcher(ROBOTS_DENY), policy="ignore")
        self.assertFalse(v.may_prefer)

    def test_ignore_lifts_the_replay_refusal(self):
        perm.check_permission(URL, hosts_path=self.hosts, fetcher=fetcher(ROBOTS_DENY))
        self.assertTrue(perm.replay_refusal(URL, hosts_path=self.hosts))
        self.assertEqual(perm.replay_refusal(URL, hosts_path=self.hosts,
                                             policy="ignore"), "")


class ReplayPathNeverFetches(RobotsPolicyBase):

    def test_an_unknown_host_is_not_fetched_on_the_replay_path(self):
        """A replay must cost the site nothing extra and must work offline."""
        allowed, note = perm.robots_allows_cached("https://never-seen.test/x")
        self.assertIsNone(allowed)
        self.assertIn("not fetched on the replay path", note)

    def test_the_compile_path_does_fetch_once(self):
        calls = []

        def counting(url):
            calls.append(url)
            return ROBOTS_ALLOW

        perm.check_permission(URL, hosts_path=self.hosts, fetcher=counting)
        perm.check_permission("https://ex.test/other", hosts_path=self.hosts,
                              fetcher=counting)
        self.assertEqual(len(calls), 1)


class LadderPreference(unittest.TestCase):
    """`api` outranks `ax`, but only as built."""

    def _transition(self, *impls):
        return UITransition(from_state="a", to_state="b", intent="search",
                            implementations=list(impls))

    def test_a_compiled_api_rung_outranks_ax_at_equal_success(self):
        api = TransitionImpl(modality="api", command={}, times_used=1, times_succeeded=1)
        ax = TransitionImpl(modality="ax", command={}, times_used=1, times_succeeded=1)
        order = rank_implementations(self._transition(ax, api))
        self.assertEqual(order[0].modality, "api")

    def test_a_failing_api_rung_does_not_outrank_a_working_ax_rung(self):
        """Success rate comes FIRST in the sort. A rung that stopped working
        loses its place without anyone having to demote it by hand."""
        api = TransitionImpl(modality="api", command={}, times_used=4, times_succeeded=1)
        ax = TransitionImpl(modality="ax", command={}, times_used=4, times_succeeded=4)
        order = rank_implementations(self._transition(api, ax))
        self.assertEqual(order[0].modality, "ax")

    def test_an_uncompiled_workflow_has_no_api_rung_at_all(self):
        """The ladder change is additive: without an `api` implementation the
        order is exactly what it was before compilation existed."""
        ax = TransitionImpl(modality="ax", command={}, times_used=1, times_succeeded=1)
        coord = TransitionImpl(modality="coordinate", command={}, times_used=1,
                               times_succeeded=1)
        order = rank_implementations(self._transition(coord, ax))
        self.assertEqual([i.modality for i in order], ["ax", "coordinate"])

    def test_the_api_rung_only_enters_a_graph_through_an_explicit_compile(self):
        """`add_api_implementation` is the single writer, and it is only called
        from `anyuse workflow compile`."""
        import inspect
        from anyuse.execution import api_compile
        src = inspect.getsource(api_compile.add_api_implementation)
        self.assertIn("modality=\"api\"", src.replace("'", '"'))

    def test_an_unproved_template_is_never_persisted(self):
        """The condition: proof AND acknowledgement.

        `compile_and_verify` returns the proof; `cli.workflow_compile` writes
        only when `proof.passed` and `permission.may_prefer`. Pinned by reading
        the CLI, because that conjunction is the whole policy.
        """
        import inspect
        from anyuse import cli
        # The tail of `workflow compile` is shared by the recorder path and the
        # --from-har path, so the rule is pinned where it actually lives.
        src = inspect.getsource(cli._finish_compile)
        self.assertIn("if not all(p.may_prefer for p in permissions):", src)
        self.assertIn("no_persist = True", src)
        self.assertIn("proof.passed and not no_persist", src)

    def test_the_har_path_goes_through_the_same_tail(self):
        """A HAR is a different way to get a recording, not a lower bar."""
        import inspect
        from anyuse import cli
        src = inspect.getsource(cli.workflow_compile.callback)
        self.assertEqual(src.count("_finish_compile("), 2)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
