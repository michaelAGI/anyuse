"""`anyuse learn` saves a page only the browser replay proved, and proposes the values it is short of.

Offline: the fakes of ``test_learn_loop`` (agent, navigator, recorded transport), a fake browser
driver for the replay and a fake model. Nothing here opens a socket, starts a browser or calls a
model.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import urllib.parse
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_learn_loop import (REGEX, SERDE, TOKIO, DeadTransport, FakeAgent,  # noqa: E402
                             FakeNavigator, executor_for, nav_for, page, run_for)

from anyuse.execution import learn_loop as L  # noqa: E402
from anyuse.execution.api_step import ApiStepExecutor  # noqa: E402
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor  # noqa: E402
from anyuse.execution.model_adapter import ModelResult, TokenUsage  # noqa: E402
from anyuse.mcpserver import compiled_tools as CT  # noqa: E402
from anyuse.mcpserver import meta_tools as MT  # noqa: E402

CLAP = nav_for("clap", ["clap v4", "clap_builder v4", "clap_derive v4"])
SERDE_JSON = nav_for("serde_json", ["serde_json v1", "serde_json5 v0", "json-patch v1"])
SERDE_DERIVE = nav_for("serde_derive", ["serde_derive v1", "serde_derive_internals v0",
                                        "serde-derive-more v0"])
TOKIO_GEN = nav_for("tokio", ["tokio v1", "tokio-util v0", "tokio-stream v0"])
RAND = nav_for("rand", ["rand v0", "rand_core v0", "rand_chacha v0"])
LONG = ["a framework for serializing and deserializing rust data structures",
        "a generic serialization and deserialization framework for many formats",
        "derive macros for the serialization framework used by many crates"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data directory of its own, ex.test acknowledged, robots.txt answered without a socket."""
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "ANYUSE_API_HOSTS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    from anyuse.execution import api_permission as AP
    monkeypatch.setattr(AP, "robots_allows", lambda url, fetcher=None, on_hop=None:
                        (None, "no robots.txt (test)"))
    AP.acknowledge_host("ex.test")
    return tmp_path


def _dom_by_value(runs):
    return {r.value: r.dom for r in runs}


class Replay:
    """The learn loop's tier-2 replay: reads each value's recorded page with the template's selector."""

    def __init__(self, doms):
        self.doms, self.seen = doms, []

    def execute(self, template, params):
        from anyuse.execution.html_extract import extract
        self.seen.append(template.render(params))
        dom = self.doms[params["q"]]
        return {"success": True, "elapsed_ms": 4100.0, "model_calls": 0,
                "extracted": {"items": extract(dom, template.extract["items"], 40)}}


class Browser:
    """A browser driver for the saved tool's replay: serves each value's recorded page."""

    def __init__(self, doms):
        self.doms, self.opened = doms, []

    def open(self, url, settle_s, timeout_s):
        self.opened.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        dom = self.doms.get(q, "")
        return PageResult(url=url, dom=dom, text="results" if dom else "", status=200,
                          elapsed_ms=900.0)


class FakeModel:
    """The configured model: answers once with fixed values, and keeps what it was asked."""

    def __init__(self, values):
        self.values, self.calls = list(values), []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return ModelResult(ok=True, parsed={"values": list(self.values)},
                           text=json.dumps({"values": self.values}),
                           usage=TokenUsage(120, 30, 150), llm_calls=1)


def _learn(values, others, *, generate, model=None, replay=None, executor=None, persist=None,
           first=SERDE):
    agent = FakeAgent([first])
    nav = FakeNavigator(others)
    res = L.learn("search this site for {q}", "https://ex.test/", values, workflow="ex_search",
                  param="q", agent=agent, navigator=nav,
                  executor=executor or executor_for([first, *others]), replay=replay,
                  persist=persist, confirm=lambda cand: True, confirmed_by="test",
                  proof_preflight=lambda _c: "", generate=generate, model=model)
    return res, agent, nav


# ---------------------------------------------------------------------------
# (1) a page only the browser replay proved is saved, listed and played
# ---------------------------------------------------------------------------

def _browser_only_tool(home):
    runs = copy.deepcopy([SERDE, REGEX, TOKIO, CLAP])
    res, _agent, _nav = _learn([r.value for r in runs], runs[1:], generate=False,
                               replay=Replay(_dom_by_value(runs)),
                               executor=ApiStepExecutor(transport=DeadTransport()),
                               persist=L.make_persister(), first=runs[0])
    return res, runs


def test_a_browser_only_proof_is_saved_as_a_tool_served_by_the_replay(home):
    res, _runs = _browser_only_tool(home)
    assert res.outcome == "tier2", res.reason
    assert res.persisted and os.path.exists(res.persisted)
    assert res.proof_status == "proved"
    assert res.reason.startswith(L.BROWSER_TOOL_NOTE)
    record = json.load(open(res.persisted))
    assert record["kind"] == CT.KIND_BROWSER
    assert record["browser"]["template"]["url"] == "https://ex.test/search?q={q}"
    assert record["browser"]["proved"] is True
    assert record["proof"]["rung"] == "browser_replay" and record["proof"]["passed"]
    assert [c["value"] for c in record["proof"]["cases"]] == ["tokio", "clap"]
    assert record["task"]["param"] == "q"
    assert record["permission"]["acknowledged"] is True
    assert record["source"]["origin"] == "agent"


def test_the_saved_tool_is_listed_and_says_it_needs_a_browser(home, monkeypatch):
    res, _runs = _browser_only_tool(home)
    row = next(r for r in CT.list_compiled() if r["workflow"] == res.workflow)
    assert row["kind"] == CT.KIND_BROWSER and row["needs_browser"]
    assert row["note"] == CT.BROWSER_TOOL_NOTE
    assert row["ladder_tier"] == CT.TIER_REPLAY
    assert row["available_via"] is None and not row["runnable_without_human"]
    assert "degraded" not in row
    assert "Browser replay proof" in row["proof"]["scope"]
    monkeypatch.setenv("ANYUSE_CDP_ENDPOINT", "http://10.0.0.5:9222")
    row = next(r for r in CT.list_compiled() if r["workflow"] == res.workflow)
    assert row["available_via"] == CT.RUNG_BROWSER and row["runnable_without_human"]

    from click.testing import CliRunner
    from anyuse.cli import cli
    out = CliRunner().invoke(cli, ["workflow", "list"])
    assert out.exit_code == 0, out.output
    assert res.workflow in out.output and "no compiled request" in out.output
    listed = json.loads(CliRunner().invoke(cli, ["workflow", "list", "--json"]).output)
    assert any(t["name"] == res.workflow and t["needs_browser"] for t in listed)


def test_the_saved_tool_plays_through_the_browser_replay(home, monkeypatch):
    res, runs = _browser_only_tool(home)
    doms = {**_dom_by_value(runs), "rand": RAND.dom}
    browser = Browser(doms)
    out = CT.run_compiled(res.workflow, {"q": "rand"},
                          browser_executor=ReplayExecutor(driver=browser),
                          agent_runner=lambda *a, **k: pytest.fail("the agent must not run"))
    assert out["ok"], out
    assert out["served_by"] == CT.RUNG_BROWSER and out["model_calls"] == 0
    assert out["result"]["items"][:2] == ["rand v0", "rand_core v0"]
    assert out["rungs_tried"][0] == {"rung": "api", "ok": False, "skipped": True,
                                     "why": "this tool has no compiled request; the browser "
                                            "replay serves it"}
    assert out["next"] == CT.BROWSER_TOOL_NOTE
    assert browser.opened == ["https://ex.test/search?q=rand"]

    # `anyuse workflow play NAME --param q=...`, the browser named by ANYUSE_CDP_ENDPOINT
    monkeypatch.setenv("ANYUSE_CDP_ENDPOINT", "http://10.0.0.5:9222")
    monkeypatch.setattr(ReplayExecutor, "driver", lambda self: browser)
    from click.testing import CliRunner
    from anyuse.cli import cli
    played = CliRunner().invoke(cli, ["workflow", "play", res.workflow, "--param", "q=rand"])
    assert played.exit_code == 0, played.output
    assert "served by browser replay" in played.output
    assert "no compiled request" in played.output
    assert "rand_core v0" in played.output


def test_without_a_browser_it_says_so_and_runs_nothing(home):
    res, _runs = _browser_only_tool(home)
    out = CT.run_compiled(res.workflow, {"q": "rand"})
    assert not out["ok"] and out["needs_browser"] and out["no_browser"]
    assert "no browser is configured" in out["error"]
    failure = MT._web_failure(out, "ex.test", res.workflow)
    assert failure["class"] == MT.NO_BROWSER


def test_the_mcp_profiles_serve_the_saved_tool(home, monkeypatch):
    res, runs = _browser_only_tool(home)
    # no browser: listed by the meta profile, not ready, and not a typed tool
    op = next(o for o in MT.web_operations() if o.name == res.workflow)
    assert op.row["ready"] is False and "no browser is configured" in op.row["why_not"]
    assert op.row["note"] == CT.BROWSER_TOOL_NOTE
    assert not [t for t in CT.executable_typed_tools() if t["workflow"] == res.workflow]

    monkeypatch.setenv("ANYUSE_CDP_ENDPOINT", "http://10.0.0.5:9222")
    typed = [t for t in CT.executable_typed_tools() if t["workflow"] == res.workflow]
    assert len(typed) == 1 and CT.BROWSER_TOOL_NOTE in typed[0]["description"]
    assert typed[0]["tier"] == CT.TIER_REPLAY
    ops = MT.web_operations()
    op = next(o for o in ops if o.name == res.workflow)
    assert op.row["ready"] is True and op.row["served_by_expected"] == CT.RUNG_BROWSER

    browser = Browser({**_dom_by_value(runs), "rand": RAND.dom})

    async def run_web(workflow, params):
        return CT.run_compiled(workflow, params, require_proved=True,
                               browser_executor=ReplayExecutor(driver=browser))

    async def run_desktop(workflow, params):
        return None

    got = asyncio.run(MT.call_operation("ex.test", res.workflow, {"q": "rand"}, operations=ops,
                                        run_web=run_web, run_desktop=run_desktop))
    assert got["ok"], got
    assert got["served_by"] == CT.RUNG_BROWSER
    explained = CT.explain_compiled(res.workflow)
    assert explained["rung"] == CT.KIND_BROWSER and "every call needs a browser" in explained["durability"]


def test_an_unacknowledged_host_is_still_not_saved(home):
    from anyuse.execution import api_permission as AP
    os.unlink(AP.hosts_file())
    res, _runs = _browser_only_tool(home)
    assert res.outcome == "tier2_nonpersistent"
    assert res.persisted is None
    assert "NOT persisted" in res.reason and "acknowledge-host ex.test" in res.reason


def test_the_cli_asks_before_saving_a_tool_whose_proof_never_asked(home):
    # the browser replay proved the page, so the compiled request's proof never reached its question
    from anyuse.execution import api_permission as AP
    os.unlink(AP.hosts_file())
    questions = []
    with AP.asking_hosts(lambda q: questions.append(q) or (False, "you answered no for ex.test")):
        res, _runs = _browser_only_tool(home)
    assert res.persisted is None and res.reason.endswith("NOT persisted: you answered no for ex.test"
                                                         "; the candidate remains available")
    assert [(q.host, q.purpose) for q in questions] == [("ex.test", AP.PURPOSE_SAVE)]
    assert not AP.host_acknowledged("ex.test")
    with AP.asking_hosts(lambda q: (True, "")):
        res, _runs = _browser_only_tool(home)
    assert res.outcome == "tier2" and res.persisted, res.reason
    assert AP.load_hosts()["ex.test"]["note"] == AP.CONFIRMED_AT_COMPILE


# ---------------------------------------------------------------------------
# (2) learn proposes the values it is short of
# ---------------------------------------------------------------------------

def test_two_values_generate_two_from_the_first_result_page_and_reach_proved(home):
    others = [REGEX, SERDE_JSON, SERDE_DERIVE]
    model = FakeModel(["never"])
    res, agent, nav = _learn(["serde", "regex"], others, generate=True, model=model,
                             persist=L.make_persister())
    assert res.outcome == "tier1", res.reason
    assert res.proof_status == "proved"
    assert res.generated == [{"value": "serde_json", "from": L.FROM_PAGE},
                             {"value": "serde_derive", "from": L.FROM_PAGE}]
    assert model.calls == [], "the page offered enough values"
    assert agent.calls == ["serde"]
    opened = ["https://ex.test/search?q=regex", "https://ex.test/search?q=serde_json",
              "https://ex.test/search?q=serde_derive"]
    assert [u for u in nav.urls if u in opened] == opened
    # the site's own empty page, learned once from two made-up searches by the confirmed URL
    assert len([u for u in nav.urls if u not in opened]) == 2
    assert res.split["selection"] == 1 and res.split["validation"] == 2
    cases = {c["value"]: c for c in res.proof["cases"]}
    assert cases["serde_json"]["generated"] == L.FROM_PAGE
    assert res.proof["generated_values"] == res.generated
    record = json.load(open(res.persisted))
    assert record["source"]["learned_from"]["generated_values"] == res.generated
    assert {c.get("generated") for c in record["proof"]["cases"]} == {L.FROM_PAGE}


def test_with_no_usable_page_items_the_model_is_asked_once(home):
    first = run_for("serde", LONG)
    model = FakeModel(["serde", "tokio", "clap", "rand"])
    res, agent, nav = _learn(["serde", "regex"], [REGEX, TOKIO_GEN, CLAP], generate=True,
                             model=model, first=first,
                             executor=executor_for([first, REGEX, TOKIO_GEN, CLAP]))
    assert len(model.calls) == 1
    prompt = model.calls[0]["messages"][-1]["content"]
    assert '"serde"' in prompt and '"regex"' in prompt
    assert LONG[0] not in prompt, "no page text goes to the model"
    assert res.generated == [{"value": "tokio", "from": L.FROM_MODEL},
                             {"value": "clap", "from": L.FROM_MODEL}]
    assert res.model_calls == first.model_calls + 1
    assert res.outcome == "tier1", res.reason
    assert res.proof_status == "proved"
    assert agent.calls == ["serde"]


def test_generated_values_never_become_the_demonstration_value(home):
    # one value given: the page offers two names, the model the third; the demonstration
    # stays the given value, the generated ones are the selection and the held-out values
    model = FakeModel(["serde", "tokio", "clap"])
    res, agent, _nav = _learn(["serde"], [SERDE_JSON, SERDE_DERIVE, TOKIO_GEN], generate=True,
                              model=model,
                              executor=executor_for([SERDE, SERDE_JSON, SERDE_DERIVE, TOKIO_GEN]))
    assert agent.calls == ["serde"]
    assert res.candidate["value"] == "serde"
    assert [g["value"] for g in res.generated] == ["serde_json", "serde_derive", "tokio"]
    assert "serde" not in [g["value"] for g in res.generated]
    assert res.outcome == "tier1", res.reason
    assert res.split["compile"] == 1 and res.split["selection"] == 1
    assert res.proof_status == "proved"
    assert [c["value"] for c in res.proof["cases"]] == ["serde_derive", "tokio"]


def test_a_generated_value_whose_page_is_not_the_confirmed_page_is_dropped(home):
    off = nav_for("serde_json", ["a", "b"], url="https://ex.test/", title="ex.test")
    res, _agent, _nav = _learn(["serde", "regex"], [REGEX, off, SERDE_DERIVE], generate=True,
                               model=FakeModel([]),
                               executor=executor_for([SERDE, REGEX, SERDE_DERIVE]))
    assert [g["value"] for g in res.generated] == ["serde_derive"]
    dropped = [d for d in res.discarded if d.get("generated")]
    assert dropped and dropped[0]["value"] == "serde_json"
    assert "1 of 2 missing value(s)" in res.generate_note
    assert res.proof_status == "checked (selection not held out)"


def test_no_generate_keeps_todays_behaviour(home):
    model = FakeModel(["tokio", "clap"])
    res, _agent, nav = _learn(["serde", "regex", "tokio"], [REGEX, TOKIO], generate=False,
                              model=model)
    assert res.outcome == "tier1", res.reason
    assert res.proof_status == "checked (selection not held out)"
    assert res.generated == [] and model.calls == []
    assert nav.urls == ["https://ex.test/search?q=regex", "https://ex.test/search?q=tokio"]
    res, agent, _nav = _learn(["serde", "regex"], [REGEX], generate=False, model=model)
    assert res.outcome == "nothing" and agent.calls == []
    assert f"needs {L.MIN_VALUES}" in res.reason


def test_the_cli_flag_turns_generation_off(home, monkeypatch):
    from click.testing import CliRunner
    from anyuse.cli import cli
    from anyuse.execution import agent_browser as AB
    from anyuse.execution import attach as A
    monkeypatch.setattr(A, "require_cdp_client", lambda: None)
    monkeypatch.setattr(AB, "assert_container_endpoint", lambda ep, **kw: {"Browser": "test"})
    monkeypatch.setattr(AB, "make_agent", lambda *a, **k: pytest.fail("no agent may start"))
    out = CliRunner().invoke(cli, ["learn", "search this site for {q}", "--url", "https://ex.test/",
                                   "--values", "serde,regex", "--endpoint", "http://10.0.0.5:9222",
                                   "--no-generate"])
    assert out.exit_code == 2 and "at least three values" in out.output
    out = CliRunner().invoke(cli, ["learn", "search this site for {q}", "--url", "https://ex.test/",
                                   "--values", ",", "--endpoint", "http://10.0.0.5:9222"])
    assert out.exit_code == 2 and "at least one value" in out.output


FOOTER = "<footer><a>Docs</a><a>Blog</a><a>Privacy</a></footer>"
BIG_FOOTER = ("<footer><ul class=links><li><a>Documentation and guides</a></li>"
              "<li><a>Privacy policy and terms</a></li><li><a>Contact the team</a></li>"
              "</ul></footer>")


def _with(extra, value, rows, *, agent=False, **kw):
    """A page of the site whose every page carries ``extra`` (a footer)."""
    make = run_for if agent else nav_for
    return make(value, rows, dom=page(rows, value, extra=extra), **kw)


def test_page_chrome_is_not_the_list_a_generated_page_must_show(home):
    # an empty result page that says so in words _EMPTY_RESULT does not know, with a
    # three-link footer: the footer is on the confirmed page too and does not change
    first = _with(FOOTER, "serde", LONG, agent=True)
    others = [_with(FOOTER, "regex", ["regex v1", "regex-lite v0", "regex-automata v0"]),
              _with(FOOTER, "nosuchcrate", [], text="Your search returned no matches."),
              _with(FOOTER, "tokio", ["tokio v1", "tokio-util v0", "tokio-stream v0"]),
              _with(FOOTER, "clap", ["clap v4", "clap_builder v4", "clap_derive v4"])]
    model = FakeModel(["nosuchcrate", "tokio", "clap"])
    res, _agent, _nav = _learn(["serde", "regex"], others, generate=True, model=model,
                               first=first, executor=executor_for([first, *others]))
    assert [g["value"] for g in res.generated] == ["tokio", "clap"]
    dropped = {d["value"]: d["why"] for d in res.discarded if d.get("generated")}
    assert "no list" in dropped["nosuchcrate"]
    assert res.outcome == "tier1", res.reason
    assert res.proof_status == "proved"


def test_one_value_takes_names_from_the_result_rows_not_the_footer(home):
    rows = ["serde v1", "serde_json v1", "serde_derive v1"]
    first = _with(BIG_FOOTER, "serde", rows, agent=True)
    cand = L.propose("search this site for {q}", "https://ex.test/", "serde",
                     agent=FakeAgent([first]))
    assert L.values_from_pages(cand, [], exclude=["serde"]) == ["serde_json", "serde_derive"]
    # no group outside the footer names the value: no page names, the model is asked
    other = _with(BIG_FOOTER, "serde", ["alpha one", "beta two", "gamma three"], agent=True)
    cand = L.propose("search this site for {q}", "https://ex.test/", "serde",
                     agent=FakeAgent([other]))
    assert L.values_from_pages(cand, [], exclude=["serde"]) == []

    pages = [_with(BIG_FOOTER, "serde_json", ["serde_json v1", "serde_json5 v0", "json-patch v1"]),
             _with(BIG_FOOTER, "serde_derive", ["serde_derive v1", "serde_derive_internals v0",
                                                "serde-derive-more v0"]),
             _with(BIG_FOOTER, "tokio", ["tokio v1", "tokio-util v0", "tokio-stream v0"])]
    model = FakeModel(["tokio", "clap"])
    res, agent, nav = _learn(["serde"], pages, generate=True, model=model, first=first,
                             executor=executor_for([first, *pages]))
    assert res.generated == [{"value": "serde_json", "from": L.FROM_PAGE},
                             {"value": "serde_derive", "from": L.FROM_PAGE},
                             {"value": "tokio", "from": L.FROM_MODEL}]
    assert len(model.calls) == 1 and agent.calls == ["serde"]
    assert res.outcome == "tier1", res.reason
    assert res.proof_status == "proved"


class RefusingModel:
    """A model refused before anything was sent (a CLI provider whose binary is missing)."""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        return ModelResult(ok=False, error="claude CLI not found on PATH", llm_calls=0,
                           provider_unreachable=True)


def test_a_model_call_refused_before_sending_is_not_counted(home):
    first = run_for("serde", LONG)
    model = RefusingModel()
    res, _agent, _nav = _learn(["serde", "regex"], [REGEX], generate=True, model=model,
                               first=first, executor=executor_for([first, REGEX]))
    assert model.calls == 1
    assert res.model_calls == first.model_calls
    assert "did not answer" in res.generate_note


def test_play_without_a_browser_says_how_to_configure_one(home):
    from click.testing import CliRunner
    from anyuse.cli import cli
    res, _runs = _browser_only_tool(home)
    out = CliRunner().invoke(cli, ["workflow", "play", res.workflow, "--param", "q=rand"])
    assert out.exit_code == 1, out.output
    assert "no browser is configured" in out.output
    assert "Next: point ANYUSE_CDP_ENDPOINT" in out.output
    assert "recompile" not in out.output


# ---------------------------------------------------------------------------
# (3) learning the same task again
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n_values", [4, 3])
def test_a_relearn_whose_request_fails_keeps_the_proved_compiled_request(home, n_values):
    runs = copy.deepcopy([SERDE, REGEX, TOKIO, CLAP])
    first, _a, _n = _learn([r.value for r in runs], runs[1:], generate=False,
                           persist=L.make_persister(), first=runs[0])
    assert first.outcome == "tier1" and first.persisted, first.reason
    with open(first.persisted) as fh:
        before = json.load(fh)
    assert before.get("kind") != CT.KIND_BROWSER and before["proof"]["status"] == "proved"

    # the same task again; the API answers 503 during this proof only
    runs2 = copy.deepcopy([SERDE, REGEX, TOKIO, CLAP][:n_values])
    second, _a, _n = _learn([r.value for r in runs2], runs2[1:], generate=False,
                            replay=Replay(_dom_by_value(runs2)),
                            executor=ApiStepExecutor(transport=DeadTransport()),
                            persist=L.make_persister(), first=runs2[0])
    assert second.outcome == "tier2_nonpersistent", second.reason
    assert second.persisted is None
    assert "already has a compiled request (proved), which needs no browser; it was kept" \
        in second.reason
    assert f"anyuse workflow delete {first.workflow}" in second.reason
    with open(first.persisted) as fh:
        assert json.load(fh) == before


def test_a_relearn_replaces_a_browser_only_tool_with_a_compiled_request(home):
    res, runs = _browser_only_tool(home)
    assert res.outcome == "tier2" and res.persisted
    again, _a, _n = _learn([r.value for r in runs], copy.deepcopy(runs[1:]), generate=False,
                           persist=L.make_persister(), first=copy.deepcopy(runs[0]))
    assert again.outcome == "tier1", again.reason
    assert again.persisted == res.persisted
    with open(again.persisted) as fh:
        record = json.load(fh)
    assert record.get("kind") != CT.KIND_BROWSER and record["proof"]["status"] == "proved"


def test_result_names_become_inputs():
    assert L._as_input("serde_json v1.0.128") == "serde_json"
    assert L._as_input("  regex-lite  v0 ") == "regex-lite"
    assert L._as_input("12345") == ""
    assert L._as_input("No results found") == ""
    assert L._as_input(LONG[0]) == ""
    assert L._new_inputs(["Serde", "tokio", "Tokio"], ["serde"]) == ["tokio"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
