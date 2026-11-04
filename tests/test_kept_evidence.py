"""Recompile evidence kept in the tool's folder (execution/evidence_store.py): the recordings
a tool was compiled from, and the pages the agent rung answered on, through the CLI compile,
``mcpserver/compiled_tools.run_compiled`` and ``auto_recompile``.

Offline: HAR files and the crates.io attach sessions behind fake transports; the agent is a
fake runner. Nothing here opens a socket, starts a browser or calls a model.
"""
import asyncio
import json
import os
import sys
import urllib.parse
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import test_auto_recompile as AR
import test_cli_compile as TCC
import test_har_import as THI
from anyuse.cli import cli
from anyuse.execution import api_permission as perm
from anyuse.execution import attach as A
from anyuse.execution import har_import as HI
from anyuse.execution.api_compile import load_compiled, persist_compiled
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.auto_recompile import stored_evidence
from anyuse.execution.cdp_replay import MAX_CAPTURED_BODIES, traffic_from_events
from anyuse.mcpserver import compiled_tools as CT

class _Lazy:
    """``evidence_store``, imported when a test first uses it."""

    def __getattr__(self, name):
        from anyuse.execution import evidence_store
        return getattr(evidence_store, name)


ES = _Lazy()
SECRETS = {"pref": "pv-5521", "session": "sv-8812", "rotated": "rv-0937", "bearer": "bv-6620"}
SETS = {"alpha": THI.ITEMS, "beta": ["Beta One", "Beta Two", "Beta Three"],
        "gamma": ["Gamma One", "Gamma Two", "Gamma Three"]}


class Offline:
    """The HAR's API; ``flaky`` answers that many requests with a changed shape."""

    def __init__(self):
        self.flaky, self.sent = 0, []

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.sent.append(q)
        if self.flaky > 0:
            self.flaky -= 1
            return ApiResponse(200, {"Content-Type": "application/json"}, '{"changed": true}')
        return ApiResponse(200, {"Content-Type": "application/json"}, THI.api_body(SETS[q]))


def _har_tool(tmp_path, monkeypatch, name="ff_search"):
    data, home = TCC._isolated_data(tmp_path, monkeypatch)
    for var in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_KEEP_EVIDENCE"):
        monkeypatch.delenv(var, raising=False)
    site = Offline()
    real = HI.compile_from_har
    monkeypatch.setattr(HI, "compile_from_har", lambda cases, **kw: real(
        cases, executor=ApiStepExecutor(transport=site), **kw))
    (tmp_path / "har").mkdir()
    har = TCC._firefox_har(THI, tmp_path / "har", SETS["alpha"], SECRETS)
    res = CliRunner().invoke(cli, ["workflow", "compile", name, "--yes", "--from-har", har,
                                   "--har-param", "q=alpha", "--har-expect", "|".join(SETS["alpha"]),
                                   "--expect", "beta=" + "|".join(SETS["beta"]),
                                   "--expect", "gamma=" + "|".join(SETS["gamma"])])
    assert res.exit_code == 0 and "Persisted" in res.output, (res.output, repr(res.exception))
    path = next(data.rglob("compiled_api.json"))
    return SimpleNamespace(data=data, home=home, site=site, path=path,
                           record=json.loads(path.read_text()))


def test_a_har_compile_keeps_its_recordings_in_the_tools_folder(tmp_path, monkeypatch):
    tool = _har_tool(tmp_path, monkeypatch)
    paths = tool.record["source"]["paths"]
    assert paths["recording"] == "evidence/recording.json" and tool.record["source"]["kept"]
    assert paths["fresh"] == ["evidence/fresh-0.json", "evidence/fresh-1.json"], paths
    folder = tool.path.parent
    for rel in [paths["recording"], *paths["fresh"]]:
        assert (folder / rel).is_file(), rel
    for written in (folder / "evidence").rglob("*"):
        text = written.read_text(errors="replace")
        for value in SECRETS.values():
            assert value not in text, (written, value)
    loaded = stored_evidence(tool.record, base_dir=str(folder))
    assert loaded["from"] == "the stored recordings" and len(loaded["fresh"]) == 2
    # one changed answer takes the request out; the agent answers; the kept recordings re-prove it
    tool.site.flaky, tool.site.sent = 1, []
    out = CT.run_compiled("ff_search", {"q": "alpha"}, hosts_path=os.environ["ANYUSE_API_HOSTS_FILE"],
                          executor=ApiStepExecutor(transport=tool.site),
                          agent_runner=lambda task, **kw: {"ok": True, "result": SETS["alpha"]})
    assert out["ok"] and out["served_by"] == "agent", out
    assert out["recompiled"]["swapped"] is True, out["recompiled"]
    assert out["recompiled"]["from"] == "the stored recordings"


def test_a_renamed_tool_still_finds_its_kept_evidence(tmp_path, monkeypatch):
    from anyuse import cli as C
    from anyuse.memory.storage import WorkflowStorage
    tool = _har_tool(tmp_path, monkeypatch)
    storage = WorkflowStorage()
    C._rename_compiled(storage, "ff_search", "ff_renamed", str(tool.path), dict(tool.record))
    moved = load_compiled(storage, "ff_renamed")
    loaded = stored_evidence(moved, base_dir=str(storage.get_workflow_dir("ff_renamed")))
    assert loaded["recording"]["param"]["value"] == "alpha" and len(loaded["fresh"]) == 2


def test_a_kept_recording_holds_no_credential_header_value(tmp_path, monkeypatch):
    monkeypatch.delenv("ANYUSE_KEEP_EVIDENCE", raising=False)
    storage = AR.Storage(str(tmp_path))
    record = {"template": {"url": "https://ex.test/api", "params": ["q"]}, "source": {}}

    def rec(q):
        return {"param": {"name": "q", "value": q}, "_path": "/somewhere/rec.json",
                "network": [{"url": f"https://ex.test/api?q={q}", "method": "GET", "status": 200,
                             "headers": {"Cookie": f"sid={SECRETS['session']}",
                                         "Authorization": f"Bearer {SECRETS['bearer']}",
                                         "X-Session-Token": SECRETS["rotated"],
                                         "Accept": "application/json"},
                             "response_headers": {"Set-Cookie": f"pref={SECRETS['pref']}",
                                                  "Content-Type": "application/json"},
                             "body": "{}"}]}
    paths = ES.keep_compile_evidence(storage, "w", record, recording=rec("alpha"),
                                     fresh=[rec("beta")])
    assert paths["recording"] == "evidence/recording.json", paths
    for name in os.listdir(ES.folder(storage, "w")):
        text = open(os.path.join(ES.folder(storage, "w"), name)).read()
        for value in SECRETS.values():
            assert value not in text, (name, value)
        kept = json.loads(text)
        assert "_path" not in kept
        headers = kept["network"][0]["headers"]
        # the names stay: the compile judges a request by them
        assert headers["Cookie"] == headers["Authorization"] == "" and "X-Session-Token" in headers
        assert headers["Accept"] == "application/json"


def test_keep_evidence_off_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_KEEP_EVIDENCE", "off")
    storage = AR.Storage(str(tmp_path))
    record = {"template": {"url": "https://ex.test/api", "params": ["q"]}, "source": {}}
    assert ES.keep_compile_evidence(storage, "w", record, recording={"param": {}}) == {}
    assert "off" in ES.keep_call_page(storage, "w", record, param="q", value="x",
                                      page={"dom": "<p>a b c</p>", "text": "a b c"},
                                      items=["a", "b", "c"])
    assert not os.path.exists(ES.folder(storage, "w")) and record["source"] == {}
    assert CT._keeps_evidence(lambda task, **kw: {}, record) is False


# ---------------------------------------------------------------------------
# attach sessions, kept after the sessions folder is emptied
# ---------------------------------------------------------------------------

@pytest.fixture
def crates(tmp_path, monkeypatch):
    for var in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_ROBOTS",
                "ANYUSE_KEEP_EVIDENCE"):
        monkeypatch.delenv(var, raising=False)
    data, _home = TCC._isolated_data(tmp_path, monkeypatch)
    sessions = []
    for n in sorted(os.listdir(AR.DATA)):
        session = json.load(open(os.path.join(AR.DATA, n)))
        path = tmp_path / "sessions" / n
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(session))
        sessions.append({**session, "_path": str(path)})
    captured = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]
    perm.acknowledge_host("crates.io")
    yield SimpleNamespace(tmp=tmp_path, data=data, sessions=sessions, captured=captured,
                          hosts=os.environ["ANYUSE_API_HOSTS_FILE"])


def test_an_attach_compile_keeps_its_sessions_after_the_sessions_folder_is_emptied(crates, monkeypatch):
    from anyuse.memory.storage import WorkflowStorage
    real = A.compile_sessions
    monkeypatch.setattr(A, "list_sessions", lambda: list(crates.sessions))
    monkeypatch.setattr(A, "select_sessions", lambda s, task="", n=0: s)
    monkeypatch.setattr(A, "compile_sessions", lambda sessions, **kw: real(
        sessions, executor=ApiStepExecutor(transport=AR.Site(crates.captured, moved=False)),
        preflight=lambda c: ""))
    res = CliRunner().invoke(cli, ["compile", "last", "--name", "crates_search", "--confirm",
                                   "https://crates.io/api/v1/crates"], obj={})
    assert "Persisted" in res.output, res.output
    for s in crates.sessions:
        os.remove(s["_path"])                     # the sessions' retention removed them
    storage = WorkflowStorage()
    record = load_compiled(storage, "crates_search")
    assert record["source"]["paths"]["recording"] == "evidence/recording.json", record["source"]
    loaded = stored_evidence(record, base_dir=str(storage.get_workflow_dir("crates_search")))
    assert loaded["from"] == "the stored attach sessions" and len(loaded["sessions"]) == 4
    assert not any("_path" in s for s in loaded["sessions"])
    site = AR.Site(crates.captured, moved=False, flaky=1)
    out = CT.run_compiled("crates_search", {"q": "tokio"}, hosts_path=crates.hosts,
                          executor=ApiStepExecutor(transport=site),
                          agent_runner=lambda task, **kw: {"ok": True, "result": ["tokio", "tokio-util"]})
    assert out["served_by"] == "agent" and out["recompiled"]["swapped"] is True, out.get("recompiled")
    assert out["recompiled"]["from"] == "the stored attach sessions"


# ---------------------------------------------------------------------------
# the agent rung's pages
# ---------------------------------------------------------------------------

def _crates_record(crates):
    from anyuse.execution.api_compile import compiled_record, with_runtime_rungs
    from anyuse.memory.storage import WorkflowStorage
    compiled, proof, _report, recs = A.compile_sessions(
        crates.sessions, executor=ApiStepExecutor(transport=AR.Site(crates.captured, moved=False)),
        preflight=lambda c: "")
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="crates_search",
                             source={"site": "crates.io", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    storage = WorkflowStorage()
    persist_compiled(storage, "crates_search", record)
    return storage, record


def _names(crates, q):
    return [c["name"] for c in json.loads(crates.captured[q])["crates"]][:10]


def _agent(crates, site, seen, on_page=True):
    def run(task, **kw):
        seen.append(kw)
        assert kw.get("keep_evidence") is True, kw
        q = kw["values"]["q"]
        names = _names(crates, q)
        shown = names if on_page else ["something else"]
        dom = "<html><body><ul>" + "".join(f"<li>{n}</li>" for n in shown) + "</ul></body></html>"
        return {"ok": True, "outcome": "done", "result": names, "model_calls": 2, "cost_usd": 0.01,
                "evidence": {"final_url": f"https://crates.io/search?q={q}", "dom": dom,
                             "text": "\n".join(shown),
                             "network": [{"url": f"https://crates.io/search?q={q}", "method": "GET",
                                          "status": 200, "mime": "text/html", "body": dom,
                                          "resource_type": "Document"},
                                         {"url": site.new_url(q), "method": "GET", "status": 200,
                                          "mime": "application/json",
                                          "body": AR._moved(crates.captured[q]),
                                          "resource_type": "Fetch"}]}}
    return run


def test_the_agent_rungs_pages_are_kept_and_recompile_the_tool(crates):
    storage, _record = _crates_record(crates)
    site, seen = AR.Site(crates.captured), []
    values = list(crates.captured)
    assert len(values) == ES.MIN_CALLS
    outs = []
    for q in values:
        outs.append(CT.run_compiled("crates_search", {"q": q}, storage=storage, hosts_path=crates.hosts,
                                    executor=ApiStepExecutor(transport=site),
                                    agent_runner=_agent(crates, site, seen)))
        assert outs[-1]["ok"] and outs[-1]["served_by"] == "agent", outs[-1]
    for out in outs[:-1]:
        assert not (out.get("recompiled") or {}).get("swapped"), out.get("recompiled")
        assert "calls: " in "; ".join((out.get("recompiled") or {}).get("skipped_evidence") or [
            out["recompiled"]["reason"]])
    last = outs[-1]["recompiled"]
    assert last["swapped"] is True and last["from"] == ES.FROM_CALLS, last
    assert "/api/v2/crates" in load_compiled(storage, "crates_search")["template"]["url"]
    site.sent.clear()
    out = CT.run_compiled("crates_search", {"q": values[0]}, storage=storage, hosts_path=crates.hosts,
                          executor=ApiStepExecutor(transport=site),
                          agent_runner=_agent(crates, site, seen))
    assert out["served_by"] == "api" and len(seen) == ES.MIN_CALLS, out


def test_an_agent_answer_not_on_its_page_is_not_kept(crates):
    storage, _record = _crates_record(crates)
    site = AR.Site(crates.captured)
    out = CT.run_compiled("crates_search", {"q": "tokio"}, storage=storage, hosts_path=crates.hosts,
                          executor=ApiStepExecutor(transport=site),
                          agent_runner=_agent(crates, site, [], on_page=False))
    assert out["served_by"] == "agent"
    assert ES.kept_calls(storage, "crates_search") == []


def test_a_signed_in_tool_keeps_no_page(tmp_path):
    storage = AR.Storage(str(tmp_path))
    signed = {"template": {"url": "https://ex.test/api", "params": ["q"], "cookie_refs": ["sid"]}}
    assert CT._keeps_evidence(lambda task, **kw: {}, signed) is False
    why = ES.keep_call_page(storage, "w", signed, param="q", value="x",
                            page={"dom": "<p>a b c</p>", "text": "a b c"}, items=["a", "b", "c"])
    assert "signed-in" in why and ES.kept_calls(storage, "w") == []
    assert ES.keep_compile_evidence(storage, "w", signed, recording={"param": {}}) == {}


def test_at_most_six_call_pages_are_kept_newest_first(tmp_path):
    storage = AR.Storage(str(tmp_path))
    record = {"template": {"url": "https://ex.test/api", "params": ["q"]}}
    t0 = 1_800_000_000.0
    for i in range(8):
        q = f"word{i}"
        assert ES.keep_call_page(storage, "w", record, param="q", value=q,
                                 page={"dom": f"<p>{q} a</p><p>{q} b</p><p>{q} c</p>",
                                       "final_url": f"https://ex.test/s?q={q}"},
                                 items=[f"{q} a", f"{q} b", f"{q} c"], now=t0 + i) == "kept"
    # the same input again replaces its older page
    ES.keep_call_page(storage, "w", record, param="q", value="word7",
                      page={"dom": "<p>word7 x</p><p>word7 y</p><p>word7 z</p>"},
                      items=["word7 x", "word7 y", "word7 z"], now=t0 + 9)
    kept = ES.kept_calls(storage, "w", now=t0 + 10)
    assert [r["param"]["value"] for r in kept] == ["word7", "word6", "word5", "word4", "word3",
                                                  "word2"], kept
    assert kept[0]["ui"]["items"] == ["word7 x", "word7 y", "word7 z"]
    assert len(os.listdir(os.path.join(ES.folder(storage, "w"), ES.CALLS_DIR))) == ES.MAX_KEPT_CALLS
    assert ES.kept_calls(storage, "w", now=t0 + ES.MAX_CALL_AGE_S + 20) == [], "older than a day"
    evidence = ES.call_evidence(storage, "w", record, now=t0 + 10)
    assert evidence["recording"]["param"]["value"] == "word7" and len(evidence["fresh"]) == 4
    assert evidence["selection"][0]["param"]["value"] == "word6"
    assert evidence["from"] == ES.FROM_CALLS


def test_the_agent_run_reads_its_last_page_from_the_toolbox_browser():
    from anyuse.execution import harness_product as HP
    from anyuse.execution import harness_tools as HT

    class Page:
        """The browser set's page as the agent left it: its DOM, text and protocol events."""
        def __init__(self):
            self._events = [
                {"method": "Network.requestWillBeSent", "params": {
                    "requestId": "1", "type": "Fetch",
                    "request": {"url": "https://ex.test/api?q=alpha", "method": "GET", "headers": {}}}},
                {"method": "Network.responseReceived", "params": {
                    "requestId": "1", "type": "Fetch",
                    "response": {"status": 200, "mimeType": "application/json"}}},
                {"method": "Network.loadingFinished", "params": {"requestId": "1"}}]

        async def url(self):
            return "https://ex.test/search?q=alpha"

        async def dom(self):
            return "<ul><li>Alpha Pkg</li><li>Beta Pkg</li></ul>"

        async def text(self):
            return "Alpha Pkg\nBeta Pkg"

        async def call(self, method, params=None, timeout=45.0):
            assert method == "Network.getResponseBody" and params == {"requestId": "1"}
            return {"body": '{"results": [{"name": "Alpha Pkg"}]}', "base64Encoded": False}

    browser = HT.BrowserTools(Page(), "https://ex.test/", "ex.test")
    evidence = HP._page_evidence(HT.Toolbox(browser=browser))
    assert evidence is not None, "the browser set's page was not found"
    assert evidence["final_url"] == "https://ex.test/search?q=alpha" and "Alpha Pkg" in evidence["dom"]
    assert [n["url"] for n in evidence["network"]] == ["https://ex.test/api?q=alpha"]
    assert "Alpha Pkg" in evidence["network"][0]["body"]


def test_traffic_from_events_newest_keeps_the_last_bodies():
    events = []
    for i in range(MAX_CAPTURED_BODIES + 6):
        rid = str(i)
        events += [
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": rid, "type": "Fetch",
                "request": {"url": f"https://s.test/api?page={i}", "method": "GET", "headers": {}}}},
            {"method": "Network.responseReceived", "params": {
                "requestId": rid, "type": "Fetch",
                "response": {"status": 200, "mimeType": "application/json"}}},
            {"method": "Network.loadingFinished", "params": {"requestId": rid}}]
    first = traffic_from_events(events)
    last = traffic_from_events(events, newest=True)
    assert len(first) == len(last) == MAX_CAPTURED_BODIES
    assert first[0]["url"].endswith("page=0") and last[-1]["url"].endswith(f"page={MAX_CAPTURED_BODIES + 5}")
    assert last[0]["url"].endswith("page=6")

    async def call(method, params, timeout):
        rid = params["requestId"]
        if rid == "7":
            raise RuntimeError("evicted")
        return {"body": '{"token": "abc.def.ghi", "n": %s}' % rid, "base64Encoded": rid == "8"}
    from anyuse.execution.cdp_replay import read_bodies
    bodies = asyncio.run(read_bodies(last[:4], call, 5.0))
    assert [b["url"].rsplit("=", 1)[1] for b in bodies] == ["6", "9"]
    assert all("_rid" not in b and b["bytes"] > 0 for b in bodies)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
