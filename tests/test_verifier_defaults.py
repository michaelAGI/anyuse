"""``ANYUSE_ATTRIBUTE_VERIFY`` and ``ANYUSE_WEB_FALLBACK`` are ON by default (about US$0.07
and US$0.50 per 1,000 runs; the only false accept measured came from the replay without the verifier).

* one place each: ``attribute_verify.enabled`` and ``web_tool_call.fallback_enabled`` read the settings;
  every product route goes through them; ``off`` / ``0`` turn them off; an explicit argument (a bench's arm) wins;
* without a model key, fail closed: the verifier's rules still run and an attribute only the model could
  settle is withheld, typed ``verifier_model_unavailable`` (never ok); the fallback is never tried and the miss carries
  ``no_model_configured`` (no crash, no fallback contact).

Offline (fake shops, no browser, no network, no model).

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_verifier_defaults.py
"""
import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

from anyuse import settings as S  # noqa: E402
from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import web_tool_call as WT  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
# (imported here, BEFORE the fixture below clears the setting: these fixture modules pin arm S on import)
import test_mcp_web_tools as TM  # noqa: E402
import test_repair_ladder as TL  # noqa: E402


@pytest.fixture(autouse=True)
def no_key(monkeypatch, tmp_path):
    for k in (AV.SETTING, WT.SETTING, "CEREBRAS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANYUSE_MODEL_ENV_FILE", os.devnull)
    AV._DEFAULT.pop("r", None)


def test_both_defaults_are_on_and_off_or_0_turn_them_off(monkeypatch):
    assert S.SETTINGS[AV.SETTING].default is True and S.SETTINGS[WT.SETTING].default is True
    assert AV.enabled() and WT.fallback_enabled()
    for off in ("off", "0"):
        monkeypatch.setenv(AV.SETTING, off)
        monkeypatch.setenv(WT.SETTING, off)
        assert not AV.enabled() and not WT.fallback_enabled()
    monkeypatch.delenv(AV.SETTING)
    monkeypatch.delenv(WT.SETTING)
    # a bench's explicit arm wins over the default: arm S stays S
    assert AV.enabled(False) is False and WT.fallback_enabled(False) is False


def test_without_a_key_the_resolver_is_typed_unavailable_and_an_open_attribute_is_withheld():
    assert not AV.model_configured()
    r = AV.default_resolver()
    assert r.model is None and r.unavailable
    # a product whose price the page does not show: the rules leave it open, only the model could settle it
    page = AV.Page("<html><body><h1>Akkuschrauber X</h1><p>Auf Lager</p></body></html>",
                   "Akkuschrauber X\nAuf Lager", "https://www.shop-x.test/p/x")
    answer = {"name": "Akkuschrauber X", "price": 89.9, "currency": "EUR", "availability": "Auf Lager",
              "url": "https://www.shop-x.test/p/x"}
    v = AV.check("product_lookup", answer, page, r)
    assert v.verdict != AV.ACCEPT
    rec = AV.withhold({"status": "ok", "output": answer}, v, "answer")
    assert rec["status"] == "incomplete" and rec["accepted"] is False and rec["output"] is None
    assert AV.MODEL_UNAVAILABLE in rec["why"]
    assert WT.reason_code(rec) == (WT.WITHHELD, AV.MODEL_UNAVAILABLE)


def test_the_default_resolver_runs_on_the_configured_provider(monkeypatch):
    # the second stage is not Cerebras-only - any configured provider sets it up on a tool call (no request is sent
    # here); outside a tool call's scope (a proof, a watch run) it asks no model whatever is configured
    from anyuse.execution import model_check as MC
    monkeypatch.setenv("ANYUSE_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("ANYUSE_MODEL_NAME", "some-model")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    assert AV.model_configured()
    with MC.scope("product"):
        r = AV.default_resolver()
    assert r.model is not None and not r.unavailable
    assert AV.default_resolver().model is None
    assert WT.default_model() is not None
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("ANYUSE_MODEL_ENV_FILE", "/nonexistent")
    with MC.scope("product"):
        r = AV.default_resolver()
    assert r.model is None and "ANYUSE_MODEL_PROVIDER" in r.unavailable


def test_a_key_in_the_model_env_file_counts_as_configured(monkeypatch, tmp_path):
    env = tmp_path / "model.env"
    env.write_text("CEREBRAS_API_KEY=not-a-real-key\n")
    monkeypatch.setenv("ANYUSE_MODEL_ENV_FILE", str(env))
    assert AV.model_configured()


def _fixture(tool, replay):
    fx = TM.Fixture(tool=dict(tool), fallback=None)          # None: the route reads the setting (the default)
    fx.route.model_ready = WT.WebToolRoute.model_ready.__get__(fx.route)     # the product's own key check
    fx.route.replay = replay
    return fx, TM


def test_the_mcp_route_with_the_defaults_verifies_the_replay_and_types_the_no_key_miss():
    v = "Bosch GSR 12V Akkuschrauber"
    tool = TL.compiled(v)
    calls = []

    def replay(entry, value, caps, ladder=None):
        calls.append(value)
        # the product's replay of a saved tool: demo_tool.run_tool, which runs the verifier when it is on (the default)
        return asyncio.run(T.run_tool(D.DomPage(TL.Shop()), entry["tool"], value, D.no_settle, result_wait_s=0.0))
    fx, TM = _fixture(tool, replay)
    try:
        name = WT.tool_specs(fx.storage)[0]["name"]
        out = TM.call(fx.server(), name, {"value": v})
        rec = fx.route.last.get("replay") or {}
        # the verifier ran inside the replay by default (its decisions are booked on the run record); on this fake
        # shop an attribute only the model could settle stays open, so without a key the answer is withheld, typed -
        # never ok - and the fallback (on by default) is not tried: typed, no crash, no contact
        assert rec["checks"]["attribute_verify"]["verdict"] != AV.ACCEPT, rec.get("checks")
        assert out["ok"] is False and out["reason_code"] == AV.MODEL_UNAVAILABLE, out
        assert out["fallback"]["tried"] is False and out["fallback"]["reason_code"] == WT.NO_MODEL
        assert fx.fallbacks == []
    finally:
        fx.close()


def test_a_replay_miss_with_the_defaults_and_no_key_is_typed_and_never_ok():
    v = "Bosch GSR 12V Akkuschrauber"
    tool = TL.compiled(v)

    def replay(entry, value, caps, ladder=None):
        # the drifted shop: the replay misses
        return asyncio.run(T.run_tool(D.DomPage(TL.Shop(cls=TL.RENAMED)), entry["tool"], value, D.no_settle,
                                      result_wait_s=0.0))
    fx, TM = _fixture(tool, replay)
    try:
        name = WT.tool_specs(fx.storage)[0]["name"]
        out = TM.call(fx.server(), name, {"value": v})
        assert out["ok"] is False and out["fallback"]["tried"] is False
        assert out["fallback"]["reason_code"] == WT.NO_MODEL and fx.fallbacks == []
    finally:
        fx.close()


def test_off_restores_todays_route(monkeypatch):
    monkeypatch.setenv(WT.SETTING, "off")
    v = "Bosch GSR 12V Akkuschrauber"

    def replay(entry, value, caps, ladder=None):
        return asyncio.run(T.run_tool(D.DomPage(TL.Shop(cls=TL.RENAMED)), entry["tool"], value, D.no_settle,
                                      result_wait_s=0.0))
    fx, TM = _fixture(TL.compiled(v), replay)
    try:
        name = WT.tool_specs(fx.storage)[0]["name"]
        out = TM.call(fx.server(), name, {"value": v})
        assert out["fallback"] == {"tried": False, "why": f"{WT.SETTING} is off"}
    finally:
        fx.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
