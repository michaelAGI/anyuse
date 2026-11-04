"""The heal checks the result page as it is now (execution/fresh_truth.py, through
execution/step_heal.py and ``mcpserver/compiled_tools.run_compiled``).

The site is an in-test fake, ``Feed``: a server-rendered result page and a JSON API whose
lists change between reads (live prices in the names, newest entries first). Its knobs
switch what the API serves and what the page shows. The check model is a fake given to
``model_check.using``. Nothing here opens a socket, starts a browser or calls a model.
"""
import html as _html
import json
import os
import sys
import urllib.parse
from types import SimpleNamespace

import pytest

from anyuse.execution import api_permission as perm
from anyuse.execution import model_check as MC
from anyuse.execution import proof_split as PS
from anyuse.execution import step_heal as H
from anyuse.execution.api_compile import (compiled_record, load_compiled, persist_compiled,
                                          with_runtime_rungs)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.api_verify import compile_and_verify
from anyuse.mcpserver.compiled_tools import run_compiled

HOST = "feed.test"
ORIGIN = f"https://{HOST}"
NAME = "feed_search"
WORDS = ["amber", "birch", "cedar", "cloud", "delta", "ember", "falcon", "harbor",
         "maple", "river", "stone", "tulip"]
SUFFIXES = ["core", "cli", "kit", "lite", "sdk", "tools"]


class Feed:
    """``epoch``: the prices in the names; with ``churn`` two newer entries per epoch come
    first. ``api``: ``results``, ``packages`` (the list moved), ``gone`` (410), ``error503``.
    ``page``: ``list``; ``featured`` (another word's entries for every input, a made-up one
    too); ``popular_on_empty`` (a made-up input gets "No results for X" and a popular list in
    the same region); ``filler`` (the same popular list, with no statement); ``picks`` (a
    made-up input gets "Popular picks instead of X", a line in no listed language, and a
    fixed list in the region whose rows have the class ``pick``). ``cls``: the class of the
    result rows."""

    def __init__(self):
        self.epoch, self.churn, self.api, self.page = 0, False, "results", "list"
        self.cls, self.pick = "result", "pick"
        self.log = []

    def names(self, q):
        q = q.strip().lower()
        if q not in WORDS:
            return []
        out = [f"{q}-{s} {10 + i + self.epoch} EUR" for i, s in enumerate(SUFFIXES)]
        if self.churn:
            out = [f"{q}-new{e}{k} {4 + e} EUR" for e in range(self.epoch, 0, -1) for k in "ab"] + out
        return out[:10]

    def _other(self, q):
        other = WORDS[(sum(map(ord, q.lower())) + 5) % len(WORDS)]
        return other if other != q.lower() else WORDS[(WORDS.index(other) + 1) % len(WORDS)]

    def shown(self, q):
        """``(statement or "", the region's entries)`` the page shows for ``q``."""
        if self.page == "featured":
            return "", [f"{n.split(' ')[0]}-pro" for n in self.names(self._other(q))]
        if self.page == "picks" and not self.names(q):
            return f"Popular picks instead of {q}", ["maple-top", "birch-top", "cedar-top", "delta-top"]
        if self.page in ("popular_on_empty", "filler") and not self.names(q):
            popular = [n.split(" ")[0] + "-top" for n in self.names("maple")[:4]]
            return (f"No results for {q}." if self.page == "popular_on_empty" else ""), popular
        return "", self.names(q)

    def page_html(self, q):
        said, entries = self.shown(q)
        cls = self.pick if self.page == "picks" and not self.names(q) else self.cls
        rows = "".join(f'<li class="{cls}"><a href="/pkg/{_html.escape(n)}">{_html.escape(n)}</a></li>'
                       for n in entries)
        notice = f"<p class='notice'>{_html.escape(said)}</p>" if said else ""
        return (f"<!doctype html><html><head><title>Search results for {_html.escape(q)} - Feed"
                f"</title></head><body><nav><a href='/'>Feed Packages</a></nav>"
                f"<form action='/search'><input name='q' value='{_html.escape(q)}'></form>"
                f"<h2>Latest prices</h2>{notice}<ul id='results'>{rows}</ul>"
                f"<footer>Prices change every day.</footer></body></html>")

    def api_body(self, q):
        names = self.names(q)
        if self.api == "gone":
            return 410, {"error": "this API was retired"}
        if self.api == "error503":
            return 503, {"error": "busy"}
        if self.api == "packages":
            return 200, {"total": len(names), "packages": [{"title": n} for n in names]}
        return 200, {"count": len(names), "results": [{"name": n} for n in names]}

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(sp.query).get("q", [""])[0]
        self.log.append((sp.path, q))
        if sp.path == "/api/search":
            status, payload = self.api_body(q)
            return ApiResponse(status, {"Content-Type": "application/json"}, json.dumps(payload))
        if sp.path in ("/", "/search"):
            return ApiResponse(200, {"Content-Type": "text/html; charset=utf-8"}, self.page_html(q))
        return ApiResponse(404, {"Content-Type": "text/plain"}, "not found")

    def pages(self):
        return [q for path, q in self.log if path == "/search"]


def recording(feed, value, tag):
    page_url = f"{ORIGIN}/search?q={value}"
    page = feed.page_html(value)
    _status, api = feed.api_body(value)
    items = feed.shown(value)[1]
    return {"workflow": NAME, "tag": tag, "site": HOST, "param": {"name": "q", "value": value},
            "final_url": page_url, "ui": {"items": items, "text": "\n".join(items)},
            "network": [{"url": page_url, "method": "GET", "status": 200, "mime": "text/html",
                         "body": page},
                        {"url": f"{ORIGIN}/api/search?q={value}", "method": "GET", "status": 200,
                         "mime": "application/json", "body": json.dumps(api)}]}


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
                 "ANYUSE_AUTO_HEAL", "ANYUSE_OPENJEV_URL", "ANYUSE_JUDGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_MODEL_CHECKS", "none")
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(perm, "_default_fetch", lambda url, *a, **k: "User-agent: *\nAllow: /\n")
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()
    hosts = str(tmp_path / "hosts.json")
    perm.acknowledge_host(HOST, path=hosts)
    yield SimpleNamespace(tmp=tmp_path, hosts=hosts, storage=Storage(str(tmp_path / "wf")))
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()


def tool(env, feed=None, page_only=False):
    """Record river (compile), stone and cloud (held out) at epoch 0; compile and persist."""
    feed = feed or Feed()
    recs = [recording(feed, v, t) for v, t in (("river", "rec"), ("stone", "fresh1"),
                                               ("cloud", "fresh2"))]
    if page_only:
        for r in recs:
            r["network"] = r["network"][:1]
    compiled, proof = compile_and_verify(recs[0], recs[1:], ApiStepExecutor(transport=feed))
    assert proof.passed and proof.effective_status == PS.PROVED, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow=NAME, source={"site": HOST})
    with_runtime_rungs(record, recs[0])
    persist_compiled(env.storage, NAME, record)
    feed.log.clear()
    return feed


def call(env, feed, q, **kw):
    return run_compiled(NAME, {"q": q}, storage=env.storage, hosts_path=env.hosts,
                        executor=ApiStepExecutor(transport=feed), **kw)


def stored(env):
    return load_compiled(env.storage, NAME)


def gold(feed, q):
    return {"items": feed.names(q)}


def _ft():
    from anyuse.execution import fresh_truth
    return fresh_truth


def nonsense(selection="river"):
    """The made-up input the page check searches for this tool (anchor stone, selection river)."""
    return _ft().made_up(["stone", selection], NAME)


class Asker:
    """A fake check model: ``answer(line) -> P(yes)``; records the lines it was asked about."""

    def __init__(self, answer):
        self.answer, self.lines = answer, []

    def __call__(self, instructions, state, guard_obs):
        line = state.rsplit(">> ", 1)[-1]
        self.lines.append(line)
        return self.answer(line)


@pytest.fixture
def scopes(monkeypatch):
    """The model-check scope each check was made in."""
    seen = []
    real = MC.check

    def check(*args, **kwargs):
        seen.append(MC.current_scope())
        return real(*args, **kwargs)
    monkeypatch.setattr(MC, "check", check)
    return seen


def a_result(line):
    return 0.95 if "-new" in line or " EUR" in line else 0.05


# ---------------------------------------------------------------------------
# the page's list changed since the last good answer
# ---------------------------------------------------------------------------

def test_a_moved_json_list_heals_when_the_list_changed_since_the_last_good_answer(env):
    feed = tool(env)
    feed.epoch, feed.api = 1, "packages"
    out = call(env, feed, "amber")
    assert out["ok"] and out["served_by"] == "api" and out["result"] == gold(feed, "amber"), out
    healed = out["healed"]
    assert healed["swapped"] and healed["rung"] == "step_repair", healed
    assert healed["truth"]["rung"] == "site" and healed["truth"]["refreshed"], healed["truth"]
    assert healed["model_calls"] == 0 and out["model_calls"] == 0
    assert nonsense() not in feed.pages(), "the tool's own response settled it: no made-up search"
    record = stored(env)
    assert record["template"]["extract"]["items"] == "packages[*].title"
    previous = json.load(open(os.path.join(env.storage.get_workflow_dir(NAME),
                                           "compiled_api.previous.json")))
    assert previous["drift_anchor"]["items"][0] == "stone-core 10 EUR"


def test_the_repaired_record_keeps_the_list_the_page_shows_now_as_its_anchor(env):
    feed = tool(env)
    feed.epoch, feed.api = 1, "packages"
    out = H.heal(env.storage, NAME, {"q": "amber"}, executor=ApiStepExecutor(transport=feed),
                 hosts_path=env.hosts)
    assert out["swapped"], out
    anchor = stored(env)["drift_anchor"]
    assert anchor["value"] == "stone" and anchor["items"] == feed.names("stone")[:10], anchor
    assert anchor["source"] == "response"


def test_an_api_that_is_gone_recompiles_from_a_page_whose_list_moved(env):
    feed = tool(env)
    feed.epoch, feed.churn, feed.api = 1, True, "gone"
    out = call(env, feed, "amber")
    assert out["ok"] and out["result"] == gold(feed, "amber"), out.get("heal") or out
    healed = out["healed"]
    assert healed["rung"] == "recompile" and healed["truth"]["rung"] == "site", healed
    assert feed.pages().count(nonsense()) == 1, feed.pages()
    record = stored(env)
    assert record["template"]["url"] == f"{ORIGIN}/search" and record["template"]["extract_kind"] == "html"
    assert record["proof"]["status"] == PS.PROVED


def test_a_steady_list_heals_with_no_extra_read(env):
    feed = tool(env)
    feed.api = "packages"
    out = call(env, feed, "amber")
    assert out["ok"] and out["healed"]["truth"]["rung"] == "anchor", out
    # the anchor's and the selection's pages, then the two held-out pages: no made-up search
    assert feed.pages() == ["stone", "river", "cloud", "amber"], feed.pages()


def test_a_featured_box_that_fills_for_any_query_is_refused_without_a_model(env):
    feed = tool(env)
    feed.page, feed.api = "featured", "gone"
    out = call(env, feed, "amber")
    assert not out["ok"] and out["heal"]["attempted"] and not out["heal"]["swapped"], out
    assert "lists other things" in out["heal"]["reason"], out["heal"]
    assert out["heal"]["truth"]["verdict"] is False and out["heal"]["model_calls"] == 0
    record = stored(env)
    assert record["template"]["extract"]["items"] == "results[*].name"
    assert record["heal_guard"]["ops"][H.OP]["stale_until"]


def _with_currency(feed):
    """The moved API gives every entry the same currency field, before its title."""
    def body(q):
        names = feed.names(q)
        return 200, {"total": len(names),
                     "packages": [{"currency": "EUR", "title": n} for n in names]}
    feed.api_body = body


def test_a_value_every_entry_repeats_does_not_vouch_for_a_featured_box(env):
    feed = tool(env)
    feed.epoch, feed.page = 1, "featured"
    _with_currency(feed)
    shown = feed.shown
    feed.shown = lambda q: (shown(q)[0], [f"{e} 9 EUR" for e in shown(q)[1]])
    out = call(env, feed, "amber")
    assert not out["ok"] and not out["heal"]["swapped"], out.get("healed") or out
    assert "lists other things" in out["heal"]["reason"], out["heal"]
    assert stored(env)["template"]["extract"]["items"] == "results[*].name"


def test_a_value_every_entry_repeats_is_not_taken_for_the_entries(env):
    feed = tool(env)
    feed.epoch = 1
    _with_currency(feed)
    out = call(env, feed, "amber")
    assert out["ok"] and out["result"] == gold(feed, "amber"), out.get("heal") or out
    assert out["healed"]["rung"] == "step_repair", out["healed"]
    assert stored(env)["template"]["extract"]["items"] == "packages[*].title"


def test_a_no_results_statement_for_the_made_up_input_makes_a_filled_region_query_bound(env):
    feed = tool(env)
    feed.page, feed.epoch, feed.churn, feed.api = "popular_on_empty", 1, True, "gone"
    out = call(env, feed, "amber")
    assert out["ok"] and out["result"] == gold(feed, "amber"), out.get("heal") or out
    assert out["healed"]["truth"]["rung"] == "fallback", out["healed"]


def _indonesian_on_empty(env):
    """The made-up input gets "Tidak ada hasil untuk X." (a language no phrase list reads)
    above the popular list; the API is gone and the list moved on."""
    feed = tool(env)
    feed.page, feed.epoch, feed.churn, feed.api = "popular_on_empty", 1, True, "gone"
    shown = feed.shown

    def indonesian(q):
        said, entries = shown(q)
        return (f"Tidak ada hasil untuk {q}." if said else ""), entries
    feed.shown = indonesian
    return feed


def test_the_check_model_reads_the_made_up_page_in_the_heal_scope(env, scopes):
    feed = _indonesian_on_empty(env)
    asker = Asker(lambda line: 0.95 if line.startswith("Tidak ada hasil") else 0.05)
    with MC.using(asker):
        out = call(env, feed, "amber")
    assert out["ok"] and out["result"] == gold(feed, "amber"), out.get("heal") or out
    truth = out["healed"]["truth"]
    assert truth["rung"] == "model" and truth["model_calls"] >= 1, truth
    assert scopes and set(scopes) == {"heal"}, scopes
    # the model reads the line as no statement: the page check has nothing that decides
    feed = _indonesian_on_empty(env)
    with MC.using(Asker(lambda line: 0.05)):
        out = call(env, feed, "amber")
    assert not out["ok"] and not out["heal"]["swapped"], out.get("healed") or out


def test_without_a_model_the_made_up_page_in_another_language_decides_nothing(env):
    feed = _indonesian_on_empty(env)
    out = call(env, feed, "amber")
    assert not out["ok"] and out["heal"]["model_calls"] == 0 and not out["heal"]["swapped"], out


def test_an_html_tool_whose_made_up_search_is_its_learned_empty_page_heals(env):
    feed = Feed()
    feed.page = "picks"
    tool(env, feed, page_only=True)
    record = stored(env)
    assert record["template"]["extract_kind"] == "html"
    assert record["template"]["expect"]["result_contract"]["empty"]["page"]["learned"], \
        "the proof learned the site's empty page"
    # the rows get one class, the made-up search's picks too, and the list moved on
    feed.epoch, feed.churn, feed.cls, feed.pick = 1, True, "row", "row"
    out = call(env, feed, "amber")
    assert out["ok"] and out["result"] == gold(feed, "amber"), out.get("heal") or out
    truth = out["healed"]["truth"]
    assert truth["rung"] == "site" and "site's own empty page" in " ".join(truth["evidence"]), truth
    assert out["model_calls"] == 0


def test_the_page_list_becomes_the_anchor_when_the_request_still_reproduces_it(env):
    feed = tool(env)
    feed.epoch = 1                           # the request is fine; only the prices moved
    out = H.heal(env.storage, NAME, {"q": "amber"}, executor=ApiStepExecutor(transport=feed),
                 hosts_path=env.hosts)
    assert out["attempted"] and not out["swapped"] and out["localised"] == "none", out
    assert "against the page read now" in out["reason"]
    record = stored(env)
    assert record["drift_anchor"]["items"] == feed.names("stone")[:10], record["drift_anchor"]
    assert record["template"]["extract"]["items"] == "results[*].name"


# ---------------------------------------------------------------------------
# the check model, when the page alone cannot say
# ---------------------------------------------------------------------------

def _filled(env):
    """The API is gone, the list moved, and a made-up search fills the list region with a
    popular list without saying it found nothing: the page alone cannot say."""
    feed = tool(env)
    feed.page, feed.epoch, feed.churn, feed.api = "filler", 1, True, "gone"
    return feed


def test_the_check_model_picks_a_result_line_and_the_heal_swaps(env, scopes):
    feed = _filled(env)
    asker = Asker(a_result)
    with MC.using(asker):
        out = call(env, feed, "amber")
    assert out["ok"] and out["result"] == gold(feed, "amber"), out.get("heal") or out
    healed = out["healed"]
    assert healed["truth"]["rung"] == "model" and healed["model_calls"] == len(asker.lines) > 0
    assert out["model_calls"] == healed["model_calls"]
    assert scopes == ["heal"], scopes
    assert "the check model was asked" in out["next"]
    # the held-out re-proof asked nothing: every question was about the anchor's page
    assert all(line in feed.page_html("stone") for line in asker.lines)


def _page_check(env, feed, asker):
    executor = ApiStepExecutor(transport=feed)
    record = stored(env)
    page = H.page_block(record)
    read = H.page_reader(H.page_template(page, "q"), executor)
    with MC.using(asker):
        return _ft().page_truth(read, record["drift_anchor"], "river", "q", seed=NAME)


@pytest.mark.parametrize("answer, why", [
    (lambda line: 0.95, "lines outside the list"),               # every line is a result
    (lambda line: 0.5, "no answer that decides"),                 # unclear
    (lambda line: 1 / 0, "no answer that decides"),               # the model fails
    (lambda line: (None, 0, 0.0, "URLError"), "no answer that decides"),   # unreachable
])
def test_only_a_clear_pick_of_a_list_line_counts(env, answer, why):
    feed = _filled(env)
    truth = _page_check(env, feed, Asker(answer))
    assert truth.verdict is False and "lists other things" in truth.why(), truth.why()
    assert why in "; ".join(truth.evidence), truth.evidence


def test_a_heal_that_asked_the_model_and_did_not_swap_counts_its_calls(env, scopes):
    feed = _filled(env)
    asker = Asker(lambda line: 0.95)             # every line is a result: nothing decides
    with MC.using(asker):
        out = call(env, feed, "amber")
    assert not out["ok"] and not out["heal"]["swapped"], out
    assert out["heal"]["model_calls"] == len(asker.lines) > 0, out["heal"]
    assert out["model_calls"] == out["llm_calls"] == len(asker.lines), out
    assert scopes == ["heal"], scopes


def test_a_heal_of_a_request_out_of_service_counts_its_model_calls_too(env):
    import time
    from anyuse.execution.api_recompile import _at
    feed = _filled(env)
    record = stored(env)
    now = time.time()
    record["health"] = {**(record.get("health") or {}), "preferred": False,
                        "demoted_reason": "3 consecutive failures (>= 3); last error: postcondition failed",
                        "demoted_at": _at(now), "probe_after": _at(now + 600)}
    persist_compiled(env.storage, NAME, record)
    asker = Asker(lambda line: 0.95)
    with MC.using(asker):
        out = call(env, feed, "amber")
    assert not out["ok"] and out["heal"]["attempted"] and not out["heal"]["swapped"], out
    assert out["model_calls"] == out["heal"]["model_calls"] == len(asker.lines) > 0, out


def test_the_page_check_asks_in_the_heal_scope_whatever_scope_is_open(env, scopes):
    feed = _filled(env)
    with MC.scope("product"):
        truth = _page_check(env, feed, Asker(a_result))
    assert truth.rung == "model" and scopes == ["heal"], (truth.summary(), scopes)


def test_a_yes_to_a_line_that_is_not_a_result_does_not_count(env):
    feed = _filled(env)
    truth = _page_check(env, feed, Asker(lambda line: 0.95 if "Feed" in line else 0.05))
    assert truth.verdict is False and truth.model_calls > 0, truth.summary()


def test_without_a_configured_model_nothing_is_asked(env):
    feed = _filled(env)
    out = call(env, feed, "amber")
    assert not out["ok"] and out["heal"]["model_calls"] == 0 and not out["heal"]["swapped"]
    assert "lists other things" in out["heal"]["reason"], out["heal"]


def test_a_summary_holds_no_page_text(env):
    feed = _filled(env)
    truth = _page_check(env, feed, Asker(a_result))
    text = json.dumps(truth.summary())
    assert truth.verdict and truth.rung == "model", truth.summary()
    assert not any(n.split(" ")[0] in text for n in feed.names("stone")), text


def test_continuity_and_provenance():
    FT = _ft()
    assert FT.continuity(["a-core 10 EUR", "a-cli 11 EUR"], ["a-new 3 EUR", "a-core 11 EUR",
                                                          "a-cli 12 EUR"]) == 1.0
    assert FT.continuity(["a-core 10 EUR"], ["b-kit 10 EUR"]) == 0.0
    share, found = FT.provenance(["a-core 11 EUR", "a-cli 12 EUR"],
                                 {"items": [{"name": "a-core", "price": "11"}, {"name": "a-cli"}]})
    assert share == 1.0 and found == ["a-core", "a-cli"]
    assert FT.provenance(["x-pro"], {"items": [{"name": "a-core"}]})[0] == 0.0
    # a value every entry repeats names no entry
    tagged = {"items": [{"currency": "EUR", "name": n} for n in ("a-core", "a-cli")]}
    assert FT.provenance(["x-pro 9 EUR", "y-pro 9 EUR"], tagged) == (0.0, [])
    assert FT.provenance(["a-core 11 EUR", "a-cli 12 EUR"], tagged) == (1.0, ["a-core", "a-cli"])
    assert FT.provenance(["x"], None) == (None, [])
    assert FT.made_up(["stone", "river"], NAME) == FT.made_up(["stone", "river"], NAME)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
