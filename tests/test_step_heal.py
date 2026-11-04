"""Healing a compiled request from plain reads of its result page (execution/step_heal.py,
through ``mcpserver/compiled_tools.run_compiled``).

The site is the offline quickstart's fixture (examples/quickstart/fixture_site.py), answered
in-process by a fake transport under a public-looking host; its shape is switched in place.
Nothing here opens a socket, starts a browser or calls a model.
"""
import importlib.util
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from anyuse.execution import api_permission as perm
from anyuse.execution import proof_split as PS
from anyuse.execution import step_heal as H
from anyuse.execution import step_repair as SR
from anyuse.execution.api_compile import (compiled_record, load_compiled, persist_compiled,
                                          runtime_rungs, with_runtime_rungs)
from anyuse.execution.api_recompile import PREVIOUS_FILE
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.api_verify import compile_and_verify
from anyuse.mcpserver.compiled_tools import run_compiled

_ROOT = pathlib.Path(__file__).resolve().parent.parent
ORIGIN = "https://fixture.test"
HOST = "fixture.test"


def _fixture():
    spec = importlib.util.spec_from_file_location(
        "heal_fixture_site", _ROOT / "examples" / "quickstart" / "fixture_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FX = _fixture()


class Site:
    """The fixture site behind a fake transport; ``shape`` switches what it serves."""

    def __init__(self, shape="original"):
        self.shape, self.log, self.hook = shape, [], None

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        self.log.append((sp.path, urllib.parse.parse_qs(sp.query).get("q", [""])[0]))
        if self.hook is not None:
            self.hook(sp.path)
        status, ctype, text = FX.respond(sp.path + ("?" + sp.query if sp.query else ""), self.shape)
        return ApiResponse(status, {"Content-Type": ctype}, text)

    def pages(self):
        return [q for path, q in self.log if path == "/search"]

    def api(self):
        return [q for path, q in self.log if path == "/api/search"]


def _recording(site, value, tag, page_only=False):
    """What examples/quickstart/record_fixture.py writes, from the fake transport."""
    page_url = f"{ORIGIN}/search?q={value}"
    _st, _ct, page = FX.respond(f"/search?q={value}", site.shape)
    _st2, _ct2, api = FX.respond(f"/api/search?q={value}", site.shape)
    items = [re.sub(r"<[^>]+>", "", m)
             for m in re.findall(r'<li class="(?:result|hit)">(.*?)</li>', page)]
    if not items:                    # the page's script fills the list from the API
        data = json.loads(api)
        items = [r.get("name") or r.get("title") for r in data.get("results") or data.get("packages") or []]
    network = [{"url": page_url, "method": "GET", "status": 200, "mime": "text/html", "body": page}]
    if not page_only:
        network.append({"url": f"{ORIGIN}/api/search?q={value}", "method": "GET", "status": 200,
                        "mime": "application/json", "body": api})
    return {"workflow": "fixture_search", "tag": tag, "site": HOST,
            "param": {"name": "q", "value": value}, "final_url": page_url,
            "ui": {"items": items, "text": "\n".join(items)}, "network": network}


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANYUSE_ROBOTS",
                 "ANYUSE_AUTO_HEAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    robots = {HOST: "User-agent: *\nAllow: /\n"}
    fetched = []

    def fetch(robots_url, *args, **kwargs):
        host = urllib.parse.urlsplit(robots_url).netloc
        fetched.append(host)
        return robots.get(host)
    monkeypatch.setattr(perm, "_default_fetch", fetch)
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()
    hosts = str(tmp_path / "hosts.json")
    perm.acknowledge_host(HOST, path=hosts)
    yield SimpleNamespace(tmp=tmp_path, hosts=hosts, robots=robots, fetched=fetched,
                          storage=Storage(str(tmp_path / "wf")))
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()


def _tool(env, shape="original", page_only=False, name="fixture_search"):
    """Record river (compile), stone and cloud (held out); compile, prove and persist."""
    site = Site(shape)
    recs = [_recording(site, v, t, page_only) for v, t in
            (("river", "rec"), ("stone", "fresh1"), ("cloud", "fresh2"))]
    compiled, proof = compile_and_verify(recs[0], recs[1:], ApiStepExecutor(transport=site))
    assert proof.passed and proof.effective_status == PS.PROVED, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow=name,
                             source={"site": HOST})
    with_runtime_rungs(record, recs[0])
    persist_compiled(env.storage, name, record)
    site.log.clear()
    return site


def _call(env, site, q, name="fixture_search", **kw):
    return run_compiled(name, {"q": q}, storage=env.storage, hosts_path=env.hosts,
                        executor=ApiStepExecutor(transport=site), **kw)


def _stored(env, name="fixture_search"):
    return load_compiled(env.storage, name)


def _gold(q):
    return {"items": FX.search(q)}


# ---------------------------------------------------------------------------

def test_compile_keeps_the_result_page_as_a_url_template_without_its_content(env):
    _tool(env)
    page = _stored(env)["page"]
    assert page["url"] == f"{ORIGIN}/search" and page["query"] == {"q": "{q}"}
    assert page["html_holds_items"] is True and page["values"] == ["river"]
    assert set(page) == {"url", "query", "param", "path_params", "transforms",
                         "html_holds_items", "values"}
    # a page the server sends empty (a script fills it) is marked so at compile
    _tool(env, "js_only", name="js_tool")
    assert _stored(env, "js_tool")["page"]["html_holds_items"] is False
    assert runtime_rungs({"param": {"name": "q", "value": "x"},
                          "final_url": "http://fixture.test/search?q=x"}).get("page") is None


def test_a_moved_json_list_heals_from_the_server_rendered_page(env):
    site = _tool(env)
    assert _call(env, site, "harbor")["ok"]                    # the anchor becomes harbor
    site.shape, site.log = "changed", []
    out = _call(env, site, "amber")
    assert out["ok"] and out["served_by"] == "api" and out["result"] == _gold("amber"), out
    healed = out["healed"]
    assert healed["swapped"] and healed["rung"] == "step_repair"
    assert healed["localised"] == "extraction_region" and healed["relearned"] == ["extraction"]
    assert healed["model_calls"] == 0 and healed["browser"] is False and out["model_calls"] == 0
    assert healed["status"] == PS.PROVED and healed["requests"] <= 15, healed
    assert "healed" in out["next"]
    stored = _stored(env)
    assert stored["template"]["extract"]["items"] == "packages[*].title"
    assert stored["proof"]["status"] == PS.PROVED and stored["health"]["preferred"]
    assert stored["healed"]["rung"] == "step_repair" and stored["drift_anchor"]["value"] == "amber"
    previous = json.load(open(os.path.join(env.storage.get_workflow_dir("fixture_search"),
                                           PREVIOUS_FILE)))
    assert previous["template"]["extract"]["items"] == "results[*].name"
    site.log = []
    for q in ("birch", "cedar", "tools"):                      # unseen inputs, exact answers
        again = _call(env, site, q)
        assert again["ok"] and again["result"] == _gold(q) and "healed" not in again, again
    assert site.log == [("/api/search", q) for q in ("birch", "cedar", "tools")]


def test_a_cold_tool_heals_from_its_compile_time_anchor(env):
    site = _tool(env)
    assert _stored(env)["drift_anchor"]["value"] == "stone"
    site.shape = "changed"
    out = _call(env, site, "amber")
    assert out["ok"] and out["result"] == _gold("amber") and out["healed"]["swapped"], out
    # the held-out inputs were cloud and this call's own input; the anchor's page and the
    # selection page fitted the ground truth
    assert sorted(set(site.pages())) == ["amber", "cloud", "river", "stone"]


def test_a_renamed_html_list_heals_from_the_tools_own_request(env):
    site = _tool(env, page_only=True)
    assert _stored(env)["template"]["extract_kind"] == "html"
    site.shape = "html_renamed"
    out = _call(env, site, "amber")
    assert out["ok"] and out["result"] == _gold("amber"), out
    assert out["healed"]["rung"] == "step_repair" and out["healed"]["localised"] == "extraction_region"
    assert not site.api(), "an HTML tool needs no API request"
    for q in ("birch", "core"):
        assert _call(env, site, q)["result"] == _gold(q)


def test_an_api_that_is_gone_is_recompiled_from_the_result_pages(env):
    site = _tool(env)
    site.shape = "api_gone"
    out = _call(env, site, "amber")
    assert out["ok"] and out["result"] == _gold("amber"), out
    assert out["healed"]["rung"] == "recompile" and out["healed"]["localised"] == "request_shape"
    stored = _stored(env)
    assert stored["template"]["extract_kind"] == "html" and stored["template"]["url"] == f"{ORIGIN}/search"
    assert stored["proof"]["status"] == PS.PROVED and stored["proof"]["split"]["held_out"]
    site.log = []
    assert _call(env, site, "birch")["result"] == _gold("birch")
    assert site.log == [("/search", "birch")]


def test_a_page_a_script_fills_is_never_read_without_a_browser(env):
    site = _tool(env, "js_only")
    site.shape = "js_only_changed"
    out = _call(env, site, "amber")
    assert not out["ok"] and not out.get("result") and "healed" not in out
    assert out["heal"]["attempted"] is False and "filled in by a script" in out["heal"]["reason"]
    assert site.log == [("/api/search", "amber")], "no page request"
    assert "heal_guard" not in _stored(env)


def test_a_page_that_lists_other_things_is_refused_by_the_anchor_gate(env):
    site = _tool(env)
    site.shape = "changed_lying_page"
    out = _call(env, site, "amber")
    assert not out["ok"] and not out.get("result"), out
    assert out["heal"]["attempted"] and not out["heal"]["swapped"]
    assert "lists other things" in out["heal"]["reason"], out["heal"]
    assert site.pages() == ["stone", "river"] and site.api() == ["amber", "stone"], site.log
    assert out["heal"]["next_attempt_after"]
    assert _stored(env)["template"]["extract"]["items"] == "results[*].name"


def test_removed_data_fails_loudly_and_the_next_calls_send_no_heal_request(env):
    site = _tool(env)
    site.shape = "gone"
    out = _call(env, site, "amber")
    assert not out["ok"] and not out.get("result") and out["heal"]["attempted"], out
    assert "no list that changes" in out["heal"]["reason"], out["heal"]
    site.log = []
    for q in ("birch", "cedar"):
        again = _call(env, site, q)
        assert not again["ok"] and again["heal"]["attempted"] is False
        assert "not healed again before" in again["heal"]["reason"]
    assert site.pages() == [], "a stale tool sends no heal request"


def test_a_failure_soon_after_a_heal_marks_the_tool_stale(env):
    site = _tool(env)
    site.shape = "changed"
    assert _call(env, site, "amber")["healed"]["swapped"]
    site.shape, site.log = "gone", []
    out = _call(env, site, "birch")
    assert not out["ok"] and out["heal"]["attempted"] is False
    assert "after a heal" in out["heal"]["reason"], out["heal"]
    assert site.log == [("/api/search", "birch")]


def test_a_failure_with_the_fall_back_off_also_marks_a_recent_heal_stale(env):
    site = _tool(env)
    site.shape = "changed"
    assert _call(env, site, "amber")["healed"]["swapped"]
    site.shape = "gone"
    out = _call(env, site, "birch", cascade=False)
    assert not out["ok"] and "heal" not in out
    ops = _stored(env)["heal_guard"]["ops"][H.OP]
    assert "after a heal" in ops["stale_reason"]


def test_no_heal_without_the_fall_back_or_with_the_setting_off(env, monkeypatch):
    site = _tool(env)
    site.shape = "changed"
    out = _call(env, site, "amber", cascade=False)
    assert not out["ok"] and "heal" not in out and "healed" not in out
    monkeypatch.setenv("ANYUSE_AUTO_HEAL", "off")
    out = _call(env, site, "amber")
    assert not out["ok"] and "heal" not in out
    assert site.pages() == [] and "heal_guard" not in _stored(env)


def test_a_server_error_or_a_timeout_is_not_healed(env):
    site = _tool(env)

    class Down(Site):
        def send(self, method, url, headers, body, timeout_s):
            self.log.append((urllib.parse.urlsplit(url).path, ""))
            return ApiResponse(503, {"Content-Type": "text/plain"}, "busy")
    down = Down()
    out = _call(env, down, "amber")
    assert not out["ok"] and out.get("transient") and "heal" not in out
    assert down.log == [("/api/search", "")]


def test_a_one_off_bad_answer_reads_no_page_when_the_request_works_again(env):
    site = _tool(env)
    site.shape = "changed"
    seen = []

    def recover(path):
        seen.append(path)
        if len(seen) == 2:               # the site answers normally from the second request on
            site.shape = "original"
    site.hook = recover
    out = _call(env, site, "amber")
    assert not out["ok"] and out["heal"]["attempted"] and not out["heal"]["swapped"]
    assert out["heal"]["localised"] == "none" and "nothing to repair" in out["heal"]["reason"]
    assert site.pages() == [] and site.api() == ["amber", "stone"], site.log
    assert _stored(env)["template"]["extract"]["items"] == "results[*].name"


def test_signed_in_and_pinned_tools_are_refused_without_an_attempt(env):
    _tool(env)
    record = _stored(env)
    signed = dict(record, template={**record["template"], "cookie_refs": ["sid"]})
    persist_compiled(env.storage, "signed", signed)
    site = Site("changed")
    executor = ApiStepExecutor(transport=site)
    out = H.heal(env.storage, "signed", {"q": "amber"}, executor=executor, hosts_path=env.hosts)
    assert out["attempted"] is False and "signed-in" in out["reason"]
    out = H.heal(env.storage, "fixture_search", {"q": "amber"}, executor=executor,
                 hosts_path=env.hosts, pinned_by=["weekly-report"])
    assert out["attempted"] is False and "weekly-report" in out["reason"]
    assert site.log == []
    assert "heal_guard" not in _stored(env) and "heal_guard" not in _stored(env, "signed")


def test_an_unacknowledged_page_host_gets_no_request(env):
    site = _tool(env)
    record = _stored(env)
    record["page"] = {**record["page"], "url": "https://www.fixture-pages.test/search"}
    persist_compiled(env.storage, "fixture_search", record)
    site.shape = "changed"
    out = _call(env, site, "amber")
    assert not out["ok"] and "not acknowledged" in out["heal"]["reason"]
    assert out["heal"]["attempted"] is False and site.pages() == []
    assert "www.fixture-pages.test" not in env.fetched


def test_a_page_robots_txt_disallows_is_not_read(env):
    site = _tool(env)
    env.robots[HOST] = "User-agent: *\nDisallow: /search\n"
    site.shape = "changed"
    out = _call(env, site, "amber")
    assert not out["ok"] and "may not be read" in out["heal"]["reason"], out["heal"]
    assert site.pages() == []


def test_a_parallel_call_does_not_start_a_second_heal(env):
    site = _tool(env)
    site.shape = "changed"
    seen = []

    def during(path):
        if path == "/search" and not seen:
            seen.append(H.heal(env.storage, "fixture_search", {"q": "birch"},
                               executor=ApiStepExecutor(transport=Site("changed")),
                               hosts_path=env.hosts))
    site.hook = during
    out = _call(env, site, "amber")
    assert out["ok"] and out["healed"]["swapped"]
    assert seen[0]["attempted"] is False and "did not finish" in seen[0]["reason"], seen


def test_a_template_changed_during_the_heal_is_not_overwritten(env):
    site = _tool(env)
    site.shape = "changed"
    other = dict(_stored(env))
    other["template"] = {**other["template"], "timeout_s": 7.0}

    def during(path):
        if path == "/search" and site.hook is not None:
            site.hook = None
            persist_compiled(env.storage, "fixture_search", other)
    site.hook = during
    out = _call(env, site, "amber")
    assert not out["ok"] and "changed while the heal ran" in out["heal"]["reason"], out["heal"]
    assert _stored(env)["template"]["timeout_s"] == 7.0


def test_a_weaker_status_is_not_swapped_in(env, monkeypatch):
    site = _tool(env)
    site.shape = "changed"
    real = SR.repair

    def weaker(record, **kw):
        new, out = real(record, **kw)
        if new is not None:
            new["proof"]["status"] = PS.CHECKED
        return new, out
    monkeypatch.setattr(SR, "repair", weaker)
    out = _call(env, site, "amber")
    assert not out["ok"] and "weaker" in out["heal"]["reason"], out["heal"]
    assert _stored(env)["template"]["extract"]["items"] == "results[*].name"


def test_slot_kept_refuses_a_request_that_drops_or_adds_an_input():
    old = RequestTemplate(url="https://x.test/api", query={"q": "{q}"}, params=["q"])
    assert H.slot_kept(old, RequestTemplate(url="https://x.test/s", query={"q": "{q}"},
                                            params=["q"]))[0]
    assert H.slot_kept(old, RequestTemplate(url="https://x.test/p/{q}", params=["q"]))[0]
    dropped = RequestTemplate(url="https://x.test/api", query={"q": "fixed"}, params=["q"])
    ok, why = H.slot_kept(old, dropped)
    assert not ok and "does not send" in why
    ok, why = H.slot_kept(old, RequestTemplate(url="https://x.test/api",
                                               query={"q": "{q}", "p": "{page}"},
                                               params=["q", "page"]))
    assert not ok and "takes" in why


def test_the_run_after_a_heal_does_not_heal_again(env, monkeypatch):
    site = _tool(env)
    site.shape = "changed"
    calls = []
    real = H.heal

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(H, "heal", counted)

    def after_the_swap(path):
        # the site changes again between the heal and the run that uses its request
        if path == "/api/search" and (_stored(env) or {}).get("healed"):
            site.shape = "gone"
    site.hook = after_the_swap
    out = _call(env, site, "amber")
    assert len(calls) == 1 and not out["ok"] and out["healed"]["swapped"], out
    assert "failed too" in out["next"] and not out.get("result")
    assert "after a heal" in _stored(env)["heal_guard"]["ops"][H.OP]["stale_reason"]


def test_a_demoted_tool_without_a_lower_rung_heals_on_its_next_call(env):
    site = _tool(env)
    record = _stored(env)
    record["health"] = {"preferred": False, "demoted_reason": "3 consecutive failures",
                        "consecutive_failures": 3, "total_runs": 3, "total_failures": 3}
    persist_compiled(env.storage, "fixture_search", record)
    site.shape = "changed"
    out = _call(env, site, "amber")
    assert out["ok"] and out["result"] == _gold("amber") and out["healed"]["swapped"], out
    assert _stored(env)["health"]["preferred"] is True


def test_a_demoted_tool_whose_request_answers_again_is_put_back_in_service_by_the_heal(env):
    from anyuse.execution.api_recompile import _at
    site = _tool(env)
    assert _call(env, site, "harbor")["ok"]                    # the anchor becomes harbor
    record = _stored(env)
    now = time.time()
    # taken out on wrong answers a moment ago: its probe is not due, the heal runs
    record["health"] = {**record["health"], "preferred": False,
                        "demoted_reason": "3 consecutive failures (>= 3); last error: postcondition failed",
                        "last_error": "postcondition failed: path 'results' not found",
                        "demoted_at": _at(now), "probe_after": _at(now + 600)}
    persist_compiled(env.storage, "fixture_search", record)
    site.log.clear()
    out = _call(env, site, "amber")
    assert out["ok"] and out["result"] == _gold("amber"), out
    assert out["restored"]["by"] == "heal" and out["restored"]["heal"]["restored"] is True, out
    assert "back in service" in out["next"] and "healed" not in out
    # the anchor's read, the check of the tool's own request, then this call: no page is read
    assert site.api() == ["harbor", "harbor", "amber"] and site.pages() == [], site.log
    stored = _stored(env)
    assert stored["health"]["preferred"] is True and stored["restored"]["by"] == "heal"
    assert "stale_until" not in stored["heal_guard"]["ops"][H.OP], stored["heal_guard"]
    assert stored["heal_guard"]["ops"][H.OP]["healed_at"], stored["heal_guard"]
    assert stored["heal"]["restored"] is True and stored["template"] == record["template"]


def _restored_by_a_heal(env):
    """A tool taken out on wrong answers whose request answers again: the heal puts it back."""
    from anyuse.execution.api_recompile import _at
    site = _tool(env)
    assert _call(env, site, "harbor")["ok"]
    record = _stored(env)
    now = time.time()
    record["health"] = {**record["health"], "preferred": False,
                        "demoted_reason": "3 consecutive failures (>= 3); last error: postcondition failed",
                        "last_error": "postcondition failed: path 'results' not found",
                        "demoted_at": _at(now), "probe_after": _at(now + 600)}
    persist_compiled(env.storage, "fixture_search", record)
    out = _call(env, site, "amber")
    assert out["ok"] and out["restored"]["by"] == "heal", out
    return site


def test_a_failure_soon_after_a_restore_by_the_heal_marks_the_tool_stale(env):
    site = _restored_by_a_heal(env)
    site.shape, site.log = "gone", []
    out = _call(env, site, "birch")
    assert not out["ok"] and out["heal"]["attempted"] is False, out
    assert "after a heal" in out["heal"]["reason"], out["heal"]
    assert site.log == [("/api/search", "birch")], site.log


def test_the_healed_run_does_not_run_the_lower_rungs_again(env):
    site = _tool(env)
    site.shape = "changed"
    asked = []

    def agent(task, **kw):
        asked.append(task)
        return {"ok": False, "outcome": "error", "reason": "the page did not load"}

    def after_the_swap(path):
        if path == "/api/search" and (_stored(env) or {}).get("healed"):
            site.shape = "gone"
    site.hook = after_the_swap
    out = _call(env, site, "amber", agent_runner=agent)
    assert out["healed"]["swapped"] and not out["ok"], out
    assert len(asked) == 1, asked
    earlier = [t for t in out["rungs_tried"] if t["rung"] == "agent"]
    assert len(earlier) == 1 and earlier[0]["before_heal"] is True, out["rungs_tried"]
    assert _stored(env)["health"]["preferred"] is True


def test_a_heal_starts_the_new_requests_health_over(env):
    site = _tool(env)
    record = _stored(env)
    record["health"] = {"preferred": True, "total_runs": 4, "total_failures": 3,
                        "recompile_attempts": 1, "last_recompile_at": "2026-01-01T00:00:00+00:00"}
    persist_compiled(env.storage, "fixture_search", record)
    site.shape = "changed"
    out = _call(env, site, "amber")
    assert out["ok"] and out["healed"]["swapped"], out
    health = _stored(env)["health"]
    assert health["total_runs"] == 1 and health["total_failures"] == 0, health
    assert health["recompile_attempts"] == 1 and health["last_recompile_at"].startswith("2026-01-01")
    site.shape = "gone"
    assert not _call(env, site, "birch")["ok"]
    health = _stored(env)["health"]
    assert health["preferred"] is True and health["total_failures"] == 1, health


def test_a_heal_that_finds_nothing_to_repair_on_a_lying_site_keeps_the_tool_out(env):
    site = _tool(env)
    assert _call(env, site, "harbor")["ok"]
    record = _stored(env)
    record["health"] = {**record["health"], "preferred": False, "demoted_reason": "wrong answers"}
    persist_compiled(env.storage, "fixture_search", record)
    real = site.send

    def lying(method, url, headers, body, timeout_s):
        # the anchor's raw page still lists its items, the tool's own request does not pass
        res = real(method, url, headers, body, timeout_s)
        if len(site.log) == 2:
            return ApiResponse(200, {"Content-Type": "application/json"}, '{"results": []}')
        return res
    site.send = lying
    site.log.clear()
    out = H.heal(env.storage, "fixture_search", {"q": "amber"},
                 executor=ApiStepExecutor(transport=site), hosts_path=env.hosts)
    assert out["localised"] == "none" and not out.get("restored") and not out["swapped"], out
    stored = _stored(env)
    assert stored["health"]["preferred"] is False and stored["heal_guard"]["ops"][H.OP]["stale_until"]


def test_a_record_compiled_before_the_page_was_stored_reads_its_kept_recording(env):
    site = _tool(env)
    record = _stored(env)
    rec_path = env.tmp / "fixture_search.rec.json"
    rec_path.write_text(json.dumps(_recording(Site(), "river", "rec")))
    record.pop("page")
    record["source"] = {**record["source"], "paths": {"recording": str(rec_path)}}
    persist_compiled(env.storage, "fixture_search", record)
    site.shape = "changed"
    out = _call(env, site, "amber")
    assert out["ok"] and out["healed"]["swapped"], out.get("heal")


def test_too_few_inputs_to_re_prove_with_is_said_and_sends_nothing(env):
    site = _tool(env)
    record = _stored(env)
    record["proof"]["cases"] = record["proof"]["cases"][:1]
    persist_compiled(env.storage, "fixture_search", record)
    site.shape = "changed"
    out = _call(env, site, "stone")
    assert not out["ok"] and "distinct input(s)" in out["heal"]["reason"], out["heal"]
    assert site.pages() == []


def test_a_call_that_loaded_the_record_before_a_heal_attempt_keeps_its_pacing(env):
    from anyuse.execution.api_recompile import save_call_outcome
    site = _tool(env)
    snapshot = _stored(env)                 # a parallel call loaded the record here
    site.shape = "gone"
    out = H.heal(env.storage, "fixture_search", {"q": "amber"},
                 executor=ApiStepExecutor(transport=site), hosts_path=env.hosts)
    assert out["attempted"] and not out["swapped"]
    assert save_call_outcome(env.storage, "fixture_search", snapshot)
    stored = _stored(env)
    assert stored["heal_guard"]["ops"][H.OP]["stale_until"] and stored["heal"]["swapped"] is False


def test_play_says_when_a_call_healed_the_tool_and_why_one_did_not(env, monkeypatch):
    from click.testing import CliRunner
    from anyuse import cli as C
    from anyuse.mcpserver import compiled_tools as CT
    site = _tool(env)
    site.shape = "changed"
    healed = _call(env, site, "amber")
    site.shape = "gone"
    refused = _call(env, site, "birch")
    monkeypatch.setattr(C, "_has_desktop_recording", lambda name: False)
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: healed)
    res = CliRunner().invoke(C.cli, ["workflow", "play", "fixture_search", "--param", "q=amber"])
    assert res.exit_code == 0, res.output
    assert "Healed: the site changed; AnyUse re-learned the extraction from plain HTTP reads" in res.output
    assert "0 model calls, no browser" in res.output
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: refused)
    res = CliRunner().invoke(C.cli, ["workflow", "play", "fixture_search", "--param", "q=birch"])
    assert res.exit_code == 1 and "Not healed: it failed again soon after a heal" in res.output, res.output
    assert res.output.count(refused["heal"]["next_attempt_after"]) == 1, res.output


def test_the_meta_profile_passes_the_heal_through(env):
    from anyuse.mcpserver import meta_tools as MT
    site = _tool(env)
    site.shape = "changed"
    healed = _call(env, site, "amber")
    assert MT._web_success(healed)["healed"]["swapped"]
    site.shape = "gone"
    refused = _call(env, site, "birch")
    shaped = MT._web_failure(refused, HOST, "fixture_search")
    assert shaped["ok"] is False and shaped["heal"]["attempted"] is False


def test_the_quickstart_heal_reads_four_pages_and_replays_two_held_out_inputs(env):
    site = _tool(env)
    assert _call(env, site, "harbor")["ok"]
    site.shape, site.log = "changed", []
    out = _call(env, site, "amber")
    assert out["ok"] and out["healed"]["requests"] == 7, out["healed"]
    # the failing call, one capture of the anchor, the anchor's, the selection's and the two
    # held-out result pages, the two held-out inputs through the repaired request, the answer
    assert site.log == [("/api/search", "amber"), ("/api/search", "harbor"),
                        ("/search", "harbor"), ("/search", "river"), ("/search", "stone"),
                        ("/search", "cloud"), ("/api/search", "stone"), ("/api/search", "cloud"),
                        ("/api/search", "amber")], site.log


def test_a_page_without_closing_tags_heals_by_step_repair(env):
    class Untidy(Site):
        """The result pages leave out the end tags a browser implies."""

        def send(self, method, url, headers, body, timeout_s):
            res = super().send(method, url, headers, body, timeout_s)
            if urllib.parse.urlsplit(url).path == "/search":
                res = ApiResponse(res.status, res.headers,
                                  res.text.replace("</li>", "").replace("</a>", ""))
            return res
    site = Untidy()
    recs = [_recording(Site(), v, t) for v, t in (("river", "rec"), ("stone", "fresh1"),
                                                  ("cloud", "fresh2"))]
    compiled, proof = compile_and_verify(recs[0], recs[1:], ApiStepExecutor(transport=site))
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="fixture_search",
                             source={"site": HOST})
    with_runtime_rungs(record, recs[0])
    persist_compiled(env.storage, "fixture_search", record)
    site.shape = "changed"
    out = _call(env, site, "amber")
    assert out["ok"] and out["result"] == _gold("amber"), out.get("heal")
    assert out["healed"]["rung"] == "step_repair", out["healed"]


def test_a_repair_that_stops_sending_the_input_is_not_swapped_in(env, monkeypatch):
    site = _tool(env)
    site.shape = "changed"
    real = SR.repair

    def fixed_query(record, **kw):
        new, out = real(record, **kw)
        if new is not None:
            new["template"]["query"] = {"q": "stone"}
        return new, out
    monkeypatch.setattr(SR, "repair", fixed_query)
    out = _call(env, site, "amber")
    assert not out["ok"] and "does not send ['q']" in out["heal"]["reason"], out["heal"]
    assert out["heal"]["next_attempt_after"]
    assert _stored(env)["template"]["extract"]["items"] == "results[*].name"


def test_a_repair_that_changes_nothing_is_not_swapped_in(env, monkeypatch):
    site = _tool(env)
    site.shape = "changed"
    real = SR.repair

    def same(record, **kw):
        new, out = real(record, **kw)
        if new is not None:
            new["template"] = {**record["template"], "notes": "re-derived"}
        return new, out
    monkeypatch.setattr(SR, "repair", same)
    out = _call(env, site, "amber")
    assert not out["ok"] and "the same as the old one" in out["heal"]["reason"], out["heal"]
    assert out["heal"]["next_attempt_after"]
    assert not os.path.exists(os.path.join(env.storage.get_workflow_dir("fixture_search"),
                                           PREVIOUS_FILE))


@pytest.mark.parametrize("status, text", [
    (429, "slow down"), (403, "<html><title>Just a moment...</title>cf-chl</html>")])
def test_a_rate_limit_or_a_challenge_is_not_healed(env, status, text):
    _tool(env)

    class Blocking(Site):
        def send(self, method, url, headers, body, timeout_s):
            self.log.append((urllib.parse.urlsplit(url).path, ""))
            return ApiResponse(status, {"Content-Type": "text/html"}, text)
    blocking = Blocking()
    out = _call(env, blocking, "amber", post_card=False)
    assert not out["ok"] and out.get("blocked") and "heal" not in out, out
    assert blocking.log == [("/api/search", "")]
    assert "heal_guard" not in _stored(env)


def test_a_tool_with_two_inputs_is_refused_without_a_request(env):
    _tool(env)
    record = _stored(env)
    record["template"] = {**record["template"], "params": ["q", "page"], "parameter_schema": [],
                          "query": {"q": "{q}", "page": "{page}"}}
    persist_compiled(env.storage, "fixture_search", record)
    site = Site("changed")
    out = H.heal(env.storage, "fixture_search", {"q": "amber", "page": "1"},
                 executor=ApiStepExecutor(transport=site), hosts_path=env.hosts)
    assert out["attempted"] is False and "one input" in out["reason"], out
    assert site.log == [] and "heal_guard" not in _stored(env)


def test_a_recompile_that_inherits_a_weaker_status_says_the_selection_was_not_held_out(env):
    site = _tool(env)
    record = _stored(env)
    record["proof"]["status"] = PS.CHECKED
    persist_compiled(env.storage, "fixture_search", record)
    site.shape = "api_gone"
    out = _call(env, site, "amber")
    assert out["ok"] and out["healed"]["rung"] == "recompile", out.get("heal")
    proof = _stored(env)["proof"]
    assert proof["status"] == PS.CHECKED and proof["split"]["held_out"] is False
    assert proof["selection_held_out"] is False


def test_a_call_that_ran_the_replaced_request_leaves_the_healed_tool_alone(env):
    site = _tool(env)
    site.shape = "changed"

    def heal_meanwhile(path):
        # another call heals the tool while this one waits for its answer
        if path == "/api/search" and site.hook is not None:
            site.hook = None
            assert H.heal(env.storage, "fixture_search", {"q": "birch"},
                          executor=ApiStepExecutor(transport=Site("changed")),
                          hosts_path=env.hosts)["swapped"]
    site.hook = heal_meanwhile
    out = _call(env, site, "amber")
    assert not out["ok"] and out["heal"]["attempted"] is False, out
    assert "replaced while this call ran" in out["heal"]["reason"], out["heal"]
    ops = _stored(env)["heal_guard"]["ops"][H.OP]
    assert ops["healed_at"] and "stale_until" not in ops, ops
    assert _call(env, site, "cedar")["result"] == _gold("cedar")


def test_a_server_error_right_after_a_heal_does_not_claim_the_tool_is_paused(env, monkeypatch):
    site = _tool(env)
    site.shape = "changed"

    def down_after_the_swap(path):
        if path == "/api/search" and (_stored(env) or {}).get("healed"):
            site.shape = "server_error"
    site.hook = down_after_the_swap
    real = FX.respond

    def respond(path, shape="original"):
        if shape == "server_error":
            return 503, "text/plain", "busy"
        return real(path, shape)
    monkeypatch.setattr(FX, "respond", respond)
    out = _call(env, site, "amber")
    assert not out["ok"] and out["healed"]["swapped"] and out.get("transient"), out
    assert "30 minutes" not in out["next"] and "did not answer" in out["next"], out["next"]
    assert "stale_until" not in _stored(env)["heal_guard"]["ops"][H.OP]

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
