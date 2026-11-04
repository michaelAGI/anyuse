"""The web demonstration's instructions behind one setting: ``ANYUSE_DEMO_INSTRUCTIONS`` = ``v1`` (default: the agent
harness's prompt byte for byte as it stood) | ``v2`` (the same plus ``WEB_LOOKUP_V2``). Read once per demonstration
policy build in ``harness_policies.system_prompt``; the default prompt of every other policy (the route's fallback
agent) is unchanged. No model, no network.
"""
import hashlib
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402

#: sha256 of ``harness_policies.SYSTEM`` as it stood: v1 must stay byte-identical to it
V1_SHA256 = "a3554e8f7e6b1139feaf25d898a2ff339d48444d816e6d7c75caa24df90ae23e"


def _system_of(policy):
    obs = T.Observation(kind="web", url="https://shop.example/")
    return policy.prompt_fn("search this site for x", obs, [], ["search", "done"])[0]


def test_v1_is_todays_prompt_byte_for_byte(monkeypatch):
    monkeypatch.delenv("ANYUSE_DEMO_INSTRUCTIONS", raising=False)
    assert hashlib.sha256(P.SYSTEM.encode("utf-8")).hexdigest() == V1_SHA256
    assert P.instructions_version() == "v1" and P.system_prompt() == P.SYSTEM and P.system_prompt("v1") == P.SYSTEM
    pol = HP.make_policy("claude", runner=lambda argv, text, t: (0, "{}", ""))
    assert _system_of(pol) == P.SYSTEM
    # an unknown value never widens anything: today's prompt
    assert P.instructions_version("v9") == "v1"


def test_v2_adds_exactly_the_five_rules(monkeypatch):
    v2 = P.system_prompt("v2")
    assert v2.startswith(P.SYSTEM) and v2[len(P.SYSTEM):] == "\n" + P.WEB_LOOKUP_V2
    rules = [ln for ln in P.WEB_LOOKUP_V2.strip().splitlines() if ln.startswith("- ")]
    assert len(rules) == 5 and P.WEB_LOOKUP_V2.strip().splitlines()[0] == "Results of a site search:"
    for i, phrase in enumerate(("not only its title", "the link that carries its name", "the page's result list",
                                "only when the page says that nothing was found", "names the site's search URL")):
        assert phrase in rules[i], (i, rules[i])
    monkeypatch.setenv("ANYUSE_DEMO_INSTRUCTIONS", "v2")
    assert P.instructions_version() == "v2"
    pol = HP.make_policy("claude", runner=lambda argv, text, t: (0, "{}", ""))
    assert _system_of(pol) == v2
    # read once, when the policy is built: a later change does not reach this run's policy
    monkeypatch.setenv("ANYUSE_DEMO_INSTRUCTIONS", "v1")
    assert _system_of(pol) == v2
    # the Sonnet demonstration of the cascade reads it too
    monkeypatch.setenv("ANYUSE_DEMO_INSTRUCTIONS", "v2")
    sp, _call = HP.sonnet_demo_policy(runner=lambda argv, text, t: (0, "{}", ""))
    assert _system_of(sp) == v2


def test_other_policies_keep_todays_prompt(monkeypatch):
    monkeypatch.setenv("ANYUSE_DEMO_INSTRUCTIONS", "v2")
    pol = P.ModelPolicy(lambda system, user: ("", {}))                # the route's fallback agent builds this way
    assert _system_of(pol) == P.SYSTEM


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
