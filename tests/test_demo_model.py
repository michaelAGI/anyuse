"""The compile's demonstration model: ``ANYUSE_DEMO_MODEL`` = ``sonnet`` (default: Sonnet
through the Claude CLI, ``claude -p``, the existing adapter) | ``cerebras`` (as before) | ``cascade`` (Cerebras first,
then ONE Sonnet retry of a demonstration that failed for an agent-quality reason, once per site). A Sonnet
demonstration is capped at US$0.40 by the CLI's count and booked in the spend ledger (inside ANYUSE_SPEND_DAILY_USD);
the run record and the saved tool name the model that demonstrated; the proof replays stay at 0 model calls.

Offline: the fake shop of ``test_repair_ladder`` on a ``dom_page`` page factory and a FAKE CLI runner (no browser,
no network, no model, no real ``claude``).

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_demo_model.py
"""
import json
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

import pytest  # noqa: E402

from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import model_adapter as M  # noqa: E402

import test_repair_ladder as SHOP  # noqa: E402

BASE = SHOP.BASE
SCRIPT = [{"action": "search", "element": 0, "text": "Bosch"}, {"action": "read_table"}, {"action": "done"}]


@pytest.fixture
def env(monkeypatch):
    d = tempfile.mkdtemp(prefix="anyuse-demo-")
    monkeypatch.setenv("ANYUSE_DATA_DIR", d)
    for k in ("ANYUSE_DEMO_MODEL", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_AGENT_UNASKED_IRREVERSIBLE"):
        monkeypatch.delenv(k, raising=False)
    return d


class FakeCLI:
    """A fake ``claude -p`` runner: answers the scripted actions in turn, reporting ``cost`` per call."""

    def __init__(self, script=SCRIPT, cost=0.05):
        self.script, self.cost, self.calls = list(script), float(cost), []

    def __call__(self, argv, stdin_text, timeout):
        self.calls.append(list(argv))
        act = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return 0, json.dumps({"result": json.dumps(act), "total_cost_usd": self.cost,
                              "usage": {"input_tokens": 900, "output_tokens": 40}}), ""


def run(env, **kw):
    from anyuse.memory.storage import WorkflowStorage
    st = WorkflowStorage(base_dir=pathlib.Path(env, "workflows"))
    site = SHOP.Shop()
    out = HP.run_agent_task("search the shop for {q}", values={"q": "Bosch"}, site=BASE + "/",
                            page_factory=lambda: D.DomPage(site), compile=True, storage=st, name="shop_search", **kw)
    return out, st


def test_the_setting_defaults_to_sonnet():
    from anyuse import settings as S
    spec = S.SETTINGS[HP.DEMO_SETTING]
    assert spec.default == "sonnet" and set(spec.allowed_values) == {"sonnet", "cerebras", "cascade"}
    assert HP.demo_model() in HP.DEMO_MODES and HP.demo_model("cascade") == "cascade"
    assert HP.DEMO_SONNET_USD == 0.40


def test_a_compile_demonstration_is_made_by_sonnet_through_the_cli_by_default(env):
    cli = FakeCLI()
    mark = M.process_call_count()
    out, st = run(env, runner=cli)
    assert out["ok"] and out["compiled"]["status"] == "ready", out["compiled"]
    assert out["demonstrated_by"] == "claude-cli:sonnet" and out["demo_cost_usd"] == pytest.approx(0.15)
    assert len(cli.calls) == 3 and M.calls_since(mark) == 3              # the demonstration only: replays at 0 calls
    assert cli.calls[0][:4] == ["claude", "-p", "--model", "sonnet"] and "--safe-mode" in cli.calls[0]
    assert out["config"]["max_usd"] == HP.DEMO_SONNET_USD
    # the saved tool names the model that demonstrated
    from anyuse.execution import harness_compile as HC
    rec = HC.load_web_tool(st, "shop_search")
    assert rec["status"] == "ready" and rec["demonstrated_by"] == "claude-cli:sonnet", rec.get("demonstrated_by")


def test_a_sonnet_demonstration_stops_at_its_cost_cap(env):
    cli = FakeCLI(script=[{"action": "read_table"}] * 10, cost=0.25)
    out, _st = run(env, runner=cli)
    assert not out["ok"] and out["compiled"]["status"] == "draft"
    assert len(cli.calls) <= 2 and out["demo_cost_usd"] <= 0.50, (len(cli.calls), out["demo_cost_usd"])


def test_cerebras_mode_keeps_the_cerebras_demonstration(env, monkeypatch):
    import httpx
    monkeypatch.setenv("ANYUSE_DEMO_MODEL", "cerebras")
    cli = FakeCLI()
    out, _st = run(env, runner=cli, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert out["outcome"] == "policy_error" and not cli.calls and "claude" not in out["demonstrated_by"]


def test_cascade_mode_retries_an_agent_quality_failure_once_with_sonnet(env, monkeypatch):
    import httpx
    monkeypatch.setenv("ANYUSE_DEMO_MODEL", "cascade")
    cli = FakeCLI()
    out, _st = run(env, runner=cli, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert out["cascade"]["retried"] and out["cascade"]["first"]["outcome"] == "policy_error"
    assert out["demonstrated_by"] == "claude-cli:sonnet" and out["compiled"]["status"] == "ready", out["compiled"]
    assert out["cascade"]["retry_cost_usd"] == pytest.approx(0.15) and len(cli.calls) == 3


def test_what_the_cascade_retries_and_what_it_never_does():
    r = HP.demo_retry_reason
    assert r({"outcome": T.Stop.MAX_STEPS, "compiled": {"status": "draft"}}).startswith("the demonstration stopped")
    assert r({"outcome": T.Stop.DONE, "compiled": {"status": "draft", "reasons": [
        "not a valid demonstration: the demonstration reported no results for a value its own site lists"]}})
    assert r({"outcome": T.Stop.DONE, "compiled": {"status": "draft", "reasons": ["ambiguous_reference: x"]}})
    assert r({"outcome": T.Stop.DONE, "compiled": {"status": "draft", "reasons": [
        "compile: no result region holds the reported results"]}})
    for stop in (T.Stop.ROBOTS_SEARCH, T.Stop.REFUSED, T.Stop.BLOCKED, T.Stop.OFF_SITE_SEARCH, T.Stop.CAPTCHA):
        assert r({"outcome": stop, "compiled": {"status": "draft"}}) == ""
    assert r({"outcome": T.Stop.DONE, "compiled": {"status": "draft", "reasons": [
        "value 'Headset': not_query_dependent: the results for 'Headset' are the results for 'helmet'"]}}) == ""
    assert r({"outcome": T.Stop.DONE, "compiled": {"status": "draft", "reasons": ["value 'X': status failed: y"]}}) == ""
    assert r({"outcome": T.Stop.DONE, "compiled": {"status": "ready"}}) == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
