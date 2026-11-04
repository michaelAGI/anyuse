"""ANYUSE_ROBOTS: respect (default, RFC 9309) | user_initiated | ignore - read ONCE, in the
robots module (``api_permission.robots_override``), when a run's robots gate is built. Pinned here:
* the default is respect;
* user_initiated lets a disallowed search through on a call the user started (an MCP tool call, a CLI run) but not in
  breadth exploration or unattended work;
* ignore lets it through;
* with ANYUSE_REFUSE_GOV_HOSTS on, government hosts are refused in every mode (off by default: judged like any host);
  the benches also refuse their competitor list;
* the run record says what was overridden ("robots would disallow");
* benches pin respect (the contact guard every live bench holds).
No network: robots files are injected.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_robots_setting.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "execution" / "web_runtime"))

import pytest  # noqa: E402

from anyuse.execution import api_permission as AP  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402

SITE = "www.shop-rob.test"
FILE = "User-agent: *\nDisallow: /search\n"
SEARCH = f"https://{SITE}/search?q=Toyota"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots_cache.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANYUSE_ROBOTS", raising=False)
    monkeypatch.delenv("ANYUSE_ROBOTS_POLICY", raising=False)
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    assert AP.robots_allows(f"https://{SITE}/", fetcher=lambda u: FILE)[0] is True   # the site's file, read
    yield monkeypatch
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()


def test_the_default_is_respect(env):
    assert AP.robots_mode() == "respect" and AP.robots_override() == ""
    with AP.user_initiated("mcp"):
        gate = HP.robots_gate()
    assert AP.robots_override() == "" and gate(SEARCH) is False and gate.overrides == {}
    env.setenv("ANYUSE_ROBOTS", "no such mode")                       # a typo never turns the check off
    assert AP.robots_mode() == "respect"


def test_user_initiated_allows_a_disallowed_search_on_a_users_call_only(env):
    env.setenv("ANYUSE_ROBOTS", "user_initiated")
    with AP.user_initiated("mcp"):
        mine = HP.robots_gate()
        with AP.unattended("breadth"):
            breadth = HP.robots_gate()
    background = HP.robots_gate()                                       # no caller started it: unattended
    assert mine(SEARCH) is True and breadth(SEARCH) is False and background(SEARCH) is False
    rec = mine.overrides[SITE]                                          # the run says what it overrode
    assert rec["robots_would"] == "disallow" and "user_initiated (mcp)" in rec["overridden_by"] and rec["requests"] == 1
    assert "disallows" in rec["why"]
    # an MCP tool call is the user's: the context reaches the run (asyncio.to_thread copies it)
    seen = {}

    def run(task, **kw):
        seen["initiation"], seen["override"] = AP.initiation(), AP.robots_override()
        return {"ok": True, "outcome": "done", "reason": "", "model_calls": 0, "cost_usd": 0.0, "compiled": None}
    from anyuse.mcpserver import agent_tools as AT
    from anyuse.mcpserver import meta_tools as MT
    from anyuse.mcpserver.server import create_server
    from mcp.types import CallToolRequest, CallToolRequestParams
    srv = create_server(profile="default", agent_runner=run, compiled_storage=MT._WorkflowDir(),
                        desktop_storage=MT._WorkflowDir())

    async def go():
        req = CallToolRequest(params=CallToolRequestParams(name=AT.TOOL_NAME, arguments={"task": "search", "site": SITE}))
        return (await srv.request_handlers[CallToolRequest](req)).root
    out = json.loads(asyncio.run(go()).content[0].text)
    assert out["ok"] and seen == {"initiation": "user:mcp", "override": "ANYUSE_ROBOTS=user_initiated (mcp)"}
    assert AP.initiation() == ""                                        # nothing leaks past the call


def test_the_routes_breadth_tools_respect_robots_under_user_initiated(env):
    env.setenv("ANYUSE_ROBOTS", "user_initiated")
    import discover_bench as DBN
    from anyuse.execution import watch_live as WL
    assert "paged_search" in WL.BREADTH_WORKFLOWS and "visit_results" in WL.BREADTH_WORKFLOWS
    with AP.user_initiated("mcp"):
        one = DBN.Robots(fetch=None)
        with AP.unattended("breadth"):
            many = DBN.Robots(fetch=None)
    for rb in (one, many):
        rb.parsers[SITE] = AP.RobotsRules()
        rb.parsers[SITE].parse(FILE.splitlines())
    assert one.allow(SEARCH) is True and many.allow(SEARCH) is False
    assert one.overrides[SITE]["robots_would"] == "disallow"


def test_ignore_allows_it_and_records_it(env):
    env.setenv("ANYUSE_ROBOTS", "ignore")
    gate = HP.robots_gate()                                             # unattended too
    assert gate(SEARCH) is True and gate.overrides[SITE]["overridden_by"] == "ANYUSE_ROBOTS=ignore"
    env.delenv("ANYUSE_ROBOTS")
    env.setenv("ANYUSE_ROBOTS_POLICY", "ignore")                        # the older switch means the same
    assert AP.robots_mode() == "ignore"
    assert AP.robots_policy("respect") == "respect"                    # an explicit respect wins


@pytest.mark.parametrize("mode", ["respect", "user_initiated", "ignore"])
@pytest.mark.parametrize("host", ["www.example.gov", "europa.eu"])
def test_government_hosts_are_refused_in_every_mode_when_the_gov_rule_is_on(env, mode, host):
    env.setenv("ANYUSE_ROBOTS", mode)
    env.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    with AP.user_initiated("cli"):
        gate = HP.robots_gate()
        rb = __import__("discover_bench").Robots(fetch=None)
    assert gate(f"https://{host}/search?q=x") is False and gate.overrides == {}
    assert rb.allow(f"https://{host}/search?q=x") is False


def test_by_default_a_government_host_is_judged_like_any_host(env):
    env.delenv("ANYUSE_REFUSE_GOV_HOSTS", raising=False)
    env.setenv("ANYUSE_ROBOTS", "ignore")
    gate = HP.robots_gate()
    assert gate("https://www.example.gov/search?q=x") is True
    assert gate.overrides["www.example.gov"]["overridden_by"] == "ANYUSE_ROBOTS=ignore"


@pytest.mark.parametrize("host", ["docs.anthropic.com", "www.browser-use.com"])
def test_competitor_domains_are_screened_by_the_benches_only(env, host):
    AP.robots_allows(f"https://{host}/", fetcher=lambda u: FILE)        # its file, read offline: /search disallowed
    env.setenv("ANYUSE_ROBOTS", "ignore")
    with AP.user_initiated("cli"):
        gate = HP.robots_gate()
        rb = __import__("discover_bench").Robots(fetch=None)
    assert gate(f"https://{host}/search?q=x") is True and host in gate.overrides
    assert rb.allow(f"https://{host}/search?q=x") is False


def test_the_run_record_notes_the_override(env):
    env.setenv("ANYUSE_ROBOTS", "user_initiated")
    from anyuse.execution import cdp_guard as G
    from anyuse.execution.discover import Pacer
    with AP.user_initiated("cli"):
        gate = HP.robots_gate()
    budget = G.RequestBudget(Pacer(0.0), per_load_cap=10, per_host_cap=10, site_cap=10)
    budget.robots_overrides, budget.robots_override = gate.overrides, gate.override
    assert gate(SEARCH) is True
    tr = budget.traffic(SITE)
    assert tr["robots_override"] == "ANYUSE_ROBOTS=user_initiated (cli)"
    assert tr["robots_overridden"][SITE]["robots_would"] == "disallow"
    # the start of a run: robots.txt fetched and its verdict recorded when overridden
    assert HP.robots_start_refusal(SEARCH, gate) == ""
    assert gate.overrides[SITE]["requests"] == 2
    # the api rung says it too
    assert "robots would disallow" in AP.replay_override(SEARCH, policy="ignore")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
