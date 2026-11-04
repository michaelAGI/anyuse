"""ONE pacing policy for every browser path, and the compile's request budget (execution/cdp_guard.DOC_MIN_INTERVAL_S / REQUEST_MIN_INTERVAL_S;
final-web-fresh defect 3), offline. Pinned here: the agent's guarded page factory, the replay's and the fallback's
guarded site context (the benches' SiteCtx that watch_live and the MCP route use) and the live benches read the same
two constants (>= 3 s between documents, >= 1 s between requests); no browser path keeps its own literal.
A compile run's ceiling is harness_product.COMPILE_SITE_CAP (50, measured on uncached fresh sites; defect 4) and every
run keeps STATIC_RESERVE_FRAC of its ceiling for documents and data (scripts stop below it).
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_pacing_policy.py
"""
import pathlib
import re
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import cdp_guard as G  # noqa: E402


def test_the_policy_values():
    assert G.DOC_MIN_INTERVAL_S == 3.0 and G.REQUEST_MIN_INTERVAL_S == 1.0


def test_the_agents_page_factory_paces_documents():
    import anyuse.execution.agent_browser as AB
    from anyuse.execution import harness_product as HP
    saved = AB.assert_container_endpoint, AB.learn_endpoint
    AB.assert_container_endpoint, AB.learn_endpoint = (lambda ep: None), (lambda ep="": "http://192.168.70.9:9222")
    try:
        f = HP.guarded_page_factory(start_url="https://www.pace.test/")
    finally:
        AB.assert_container_endpoint, AB.learn_endpoint = saved
    assert f.budget.nav_min_interval_s == G.DOC_MIN_INTERVAL_S
    assert f.budget.pacer.min_interval_s == G.REQUEST_MIN_INTERVAL_S


def _factory(**kw):
    import anyuse.execution.agent_browser as AB
    from anyuse.execution import harness_product as HP
    saved = AB.assert_container_endpoint, AB.learn_endpoint
    AB.assert_container_endpoint, AB.learn_endpoint = (lambda ep: None), (lambda ep="": "http://192.168.70.9:9222")
    try:
        return HP.guarded_page_factory(start_url="https://www.pace.test/", **kw)
    finally:
        AB.assert_container_endpoint, AB.learn_endpoint = saved


def test_a_compile_run_has_its_measured_ceiling_and_scripts_never_starve_documents():
    from anyuse.execution import harness_product as HP
    assert HP.COMPILE_SITE_CAP == 50 and HP.STATIC_RESERVE_FRAC == 0.4
    f = _factory(site_cap=HP.COMPILE_SITE_CAP)
    assert f.budget.static_reserve == 20 and f.budget.static_share() == 30
    assert _factory().budget.static_reserve == int(HP.RUN_SITE_CAP * 0.4)
    seen = {}
    real = HP.make_toolbox

    def fake(**kw):
        seen.update(kw)
        return {"toolbox": None, "close": lambda: None, "refusal": {"kind": "test", "reason": "stop here"}}
    HP.make_toolbox = fake
    try:
        HP.run_agent_task("search for {q}", values={"q": "x"}, site="https://www.pace.test/", policy="scripted",
                          compile=True)
        assert seen["site_cap"] == HP.COMPILE_SITE_CAP
        HP.run_agent_task("search for {q}", values={"q": "x"}, site="https://www.pace.test/", policy="scripted")
        assert seen["site_cap"] is None
    finally:
        HP.make_toolbox = real


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
