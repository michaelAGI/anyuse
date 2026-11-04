"""The host acknowledgement as part of the compile: after robots.txt is read, the CLI asks once
before the proof replays the compiled request against a new host, `--yes` (or the root `-y`) answers
for scripts, and without a terminal the refusal names both ways on one line. Under `--dry-run` /
`--no-persist` a yes holds for that command only. robots.txt disallowing the path still blocks with
no question, and nothing outside the CLI (MCP, benches) ever asks or acknowledges.

Offline: robots.txt is answered by an injected fetch, the proof's requests go to a fake transport
that serves the quickstart fixture's catalog, and the host file and data directory are temp files.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import urllib.parse

import pytest
from click.testing import CliRunner

from anyuse.cli import _NewHostQuestion, cli
from anyuse.execution import api_permission as AP
from anyuse.execution import api_verify as AV
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST = "pkgs.example"
ORIGIN = f"https://{HOST}"
NAME = "pkg_search"
ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DENY = "User-agent: *\nDisallow: /api/\n"


def _fixture():
    spec = importlib.util.spec_from_file_location(
        "inline_ack_fixture_site", ROOT / "examples" / "quickstart" / "fixture_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FX = _fixture()


def _api_body(q):
    names = FX.search(q)
    return json.dumps({"count": len(names), "results": [{"name": n} for n in names]})


def _recording(value, tag):
    page = (f"<html><body><ul>" + "".join(f'<li class="result">{n}</li>' for n in FX.search(value))
            + "</ul></body></html>")
    q = urllib.parse.quote(value)
    return {"workflow": NAME, "tag": tag, "site": HOST, "param": {"name": "q", "value": value},
            "final_url": f"{ORIGIN}/search?q={q}",
            "ui": {"items": FX.search(value), "text": "\n".join(FX.search(value))},
            "network": [
                {"url": f"{ORIGIN}/search?q={q}", "method": "GET", "status": 200,
                 "mime": "text/html", "bytes": len(page), "body": page},
                {"url": f"{ORIGIN}/api/search?q={q}", "method": "GET", "status": 200,
                 "mime": "application/json", "bytes": len(_api_body(value)),
                 "body": _api_body(value)}]}


class Catalog:
    """The proof's transport: answers /api/search from the fixture's catalog and counts requests."""

    def __init__(self):
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        return ApiResponse(200, {"Content-Type": "application/json"}, _api_body(q))


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Recordings of three searches, a host file and data directory of their own, robots.txt
    answered without a socket, and the proof's transport."""
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(tmp_path / "api_hosts.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.delenv("ANYUSE_ROBOTS", raising=False)
    monkeypatch.delenv("ANYUSE_ROBOTS_POLICY", raising=False)
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    robots = {"text": ROBOTS_ALLOW}
    monkeypatch.setattr(AP, "_default_fetch", lambda url, *a, **kw: robots["text"])
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    for value, tag in (("river", "rec"), ("stone", "fresh1"), ("cloud", "fresh2")):
        (rec_dir / f"{NAME}.{tag}.json").write_text(json.dumps(_recording(value, tag)))
    transport = Catalog()
    real = AV.compile_and_verify
    monkeypatch.setattr(AV, "compile_and_verify", lambda recording, fresh, executor=None, **kw: real(
        recording, fresh, executor=ApiStepExecutor(transport=transport), **kw))
    yield {"dir": str(rec_dir), "robots": robots, "transport": transport,
           "hosts": str(tmp_path / "api_hosts.json")}
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()


def _compile(site, *extra, tty=False, answer=None, monkeypatch=None, root=()):
    import anyuse.cli as C
    monkeypatch.setattr(C, "_terminal_attached", lambda: tty)
    return CliRunner().invoke(cli, [*root, "workflow", "compile", NAME, "--recording", site["dir"],
                                    *extra], input=answer, obj={})


# two proof replays and two made-up searches that learn the site's empty page
QUESTION = (f"Prove against {HOST} now? This sends 4 requests to {HOST} "
            f"(robots.txt allows /api/search).")


# ---------------------------------------------------------------------------
# the CLI: yes, no, --yes, no terminal
# ---------------------------------------------------------------------------

def test_yes_at_the_prompt_records_the_host_and_the_proof_runs_in_the_same_command(site, monkeypatch):
    res = _compile(site, tty=True, answer="y\n", monkeypatch=monkeypatch)
    assert res.exit_code == 0, res.output
    assert QUESTION + " [Y/n]:" in res.output
    assert "Fresh-input proof: PASSED (2/2" in res.output
    assert "Persisted as the preferred implementation" in res.output
    assert len(site["transport"].sent) == 4
    entry = AP.load_hosts(site["hosts"])[HOST]
    assert entry["acknowledged"] is True and entry["note"] == AP.CONFIRMED_AT_COMPILE


def test_enter_takes_the_default_yes(site, monkeypatch):
    res = _compile(site, "--json", tty=True, answer="\n", monkeypatch=monkeypatch)
    assert res.exit_code == 0, res.output
    assert AP.host_acknowledged(HOST, site["hosts"])
    report = json.loads(res.output[res.output.index("{"):])
    assert report["acknowledged_at_compile"] == [HOST]
    assert report["equivalence"]["passed"] is True


def test_no_at_the_prompt_sends_nothing_and_records_nothing(site, monkeypatch):
    res = _compile(site, tty=True, answer="n\n", monkeypatch=monkeypatch)
    assert res.exit_code == 1, res.output
    assert QUESTION in res.output
    assert f"NOT PASSED (0/0 cases) — proof not run: you answered no for {HOST}" in res.output
    assert "Permission: NOT preferred — see the reason above" in res.output
    assert site["transport"].sent == []
    assert not AP.host_acknowledged(HOST, site["hosts"])


def test_yes_flag_answers_without_a_terminal(site, monkeypatch):
    res = _compile(site, "--yes", tty=False, monkeypatch=monkeypatch)
    assert res.exit_code == 0, res.output
    assert QUESTION + " yes (--yes)" in res.output
    assert "Fresh-input proof: PASSED (2/2" in res.output
    assert AP.load_hosts(site["hosts"])[HOST]["note"] == AP.CONFIRMED_AT_COMPILE
    res = _compile(site, "-y", tty=False, monkeypatch=monkeypatch)      # already acknowledged: no question
    assert res.exit_code == 0 and "Prove against" not in res.output, res.output


def test_without_a_terminal_the_refusal_is_one_line_naming_both_ways(site, monkeypatch):
    res = _compile(site, tty=False, monkeypatch=monkeypatch)
    assert res.exit_code == 1, res.output
    line = next(l for l in res.output.splitlines() if l.startswith("Fresh-input proof:"))
    assert line == (f"Fresh-input proof: NOT PASSED (0/0 cases) — proof not run: {HOST} is a new host; "
                    f"re-run with --yes, or run `anyuse workflow acknowledge-host {HOST}` first")
    assert "Prove against" not in res.output                      # nothing was asked
    assert "Permission: NOT preferred — see the reason above" in res.output
    assert "terms" not in res.output and "legal" not in res.output
    assert site["transport"].sent == []
    assert not AP.host_acknowledged(HOST, site["hosts"])


def test_robots_disallow_still_blocks_with_no_question(site, monkeypatch):
    site["robots"]["text"] = ROBOTS_DENY
    for args, tty, answer in (((), True, "y\n"), (("--yes",), False, None)):
        res = _compile(site, *args, tty=tty, answer=answer, monkeypatch=monkeypatch)
        assert res.exit_code == 1, res.output
        assert "Prove against" not in res.output
        assert "robots.txt disallows this path" in res.output
        assert site["transport"].sent == []
        assert not AP.host_acknowledged(HOST, site["hosts"])


def test_the_question_is_asked_once_per_host(monkeypatch):
    import click

    import anyuse.cli as C
    asked = []
    monkeypatch.setattr(C, "_terminal_attached", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda text, **kw: asked.append(text) or False)
    question = _NewHostQuestion(yes=False)
    q = AP.HostQuestion(host=HOST, url=f"{ORIGIN}/api/search", robots_allowed=True, requests=(2, 4))
    assert question(q) == (False, f"you answered no for {HOST}")
    assert question(q) == (False, f"you answered no for {HOST}")
    assert asked == [f"Prove against {HOST} now? This sends 2 to 4 requests to {HOST} "
                     f"(robots.txt allows /api/search)."]
    assert question.declined(HOST) and question.acknowledged() == []


def _options(*path):
    cmd = cli
    for word in path:
        cmd = cmd.commands[word]
    return {o for p in cmd.params for o in getattr(p, "opts", [])}


def test_every_command_that_runs_a_proof_takes_yes():
    commands = {("workflow", "compile"), ("compile",), ("learn",), ("learn-confirm",),
                ("map", "prove"), ("workflow", "from-draft")}
    for path in commands:
        assert {"--yes", "-y"} <= _options(*path), path
    # agent run's compile paths (a DOM-proved web tool, a desktop draft) never replay a compiled
    # request, so nothing there asks the question: a --yes would answer nothing
    assert "--yes" not in _options("agent", "run")


# ---------------------------------------------------------------------------
# --dry-run / --no-persist: the yes holds for the command, nothing is written
# ---------------------------------------------------------------------------

def test_no_persist_proves_after_a_yes_and_writes_no_host(site, monkeypatch):
    res = _compile(site, "--no-persist", "--yes", monkeypatch=monkeypatch)
    assert res.exit_code == 0, res.output
    assert QUESTION + " yes (--yes)" in res.output
    assert "Fresh-input proof: PASSED (2/2" in res.output
    assert "host acknowledged for this command (not recorded)" in res.output
    assert res.output.rstrip().endswith("(--no-persist: nothing written)")
    assert len(site["transport"].sent) == 4
    assert not os.path.exists(site["hosts"])
    res = _compile(site, "--no-persist", "--yes", "--json", monkeypatch=monkeypatch)
    report = json.loads(res.output[res.output.index("{"):])
    assert report["acknowledged_for_this_command"] == [HOST]
    assert "acknowledged_at_compile" not in report
    assert not os.path.exists(site["hosts"])
    assert AP.host_asker() is None and not AP.host_acknowledged(HOST)    # gone with the command


def test_from_draft_dry_run_proves_after_a_yes_and_writes_no_host(site, monkeypatch):
    import anyuse.cli as C
    monkeypatch.setattr(C, "_terminal_attached", lambda: False)
    res = CliRunner().invoke(cli, ["workflow", "from-draft", site["dir"], "--web-name", NAME,
                                   "--dry-run", "--yes"], obj={})
    assert res.exit_code == 0, res.output
    assert QUESTION + " yes (--yes)" in res.output
    assert "Fresh-input proof (api_verify.compile_and_verify): PASSED" in res.output
    assert res.output.rstrip().endswith("(--dry-run: nothing written)")
    assert len(site["transport"].sent) == 4
    assert not os.path.exists(site["hosts"])


def test_a_yes_that_is_not_recorded_lasts_only_for_the_block(tmp_path, monkeypatch):
    # learn --dry-run and learn-confirm --dry-run reach the proof through this preflight
    compiled = _compiled(tmp_path, monkeypatch)
    questions = []
    with AP.asking_hosts(lambda q: questions.append(q.host) or (True, ""), record=False):
        assert AV.permission_preflight(compiled, _recording("river", "rec")) == ""
        assert AP.host_acknowledged(HOST) and AP.acknowledged_for_this_command(HOST)
        verdict = AP.check_permission(f"{ORIGIN}/api/search")
        assert verdict.may_prefer and verdict.reason.endswith(
            "host acknowledged for this command (not recorded)")
    assert questions == [HOST]
    assert not os.path.exists(tmp_path / "api_hosts.json")
    assert not AP.host_acknowledged(HOST)


@pytest.mark.parametrize("command, records", [
    (["learn", "find it", "--url", f"{ORIGIN}/", "--values", "river", "--dry-run"], False),
    (["learn", "find it", "--url", f"{ORIGIN}/", "--values", "river"], True),
    (["compile", "last", "--no-persist"], False),
    (["compile", "last"], True),
])
def test_dry_run_and_no_persist_install_an_asker_that_writes_nothing(command, records, monkeypatch):
    from anyuse.execution import attach as A
    seen = {}

    def stop(*a, **kw):
        state = AP._HOST_ASKER.get()
        seen["record"], seen["asker_records"] = state.record, state.asker.records
        raise A.AttachError("stop here")
    monkeypatch.setattr(A, "require_cdp_client", stop)
    monkeypatch.setattr(A, "list_sessions", lambda: [])
    monkeypatch.setattr(A, "select_sessions", lambda s, task="", n=0: s)
    monkeypatch.setattr(A, "compile_sessions", stop)
    res = CliRunner().invoke(cli, command, obj={})
    assert "stop here" in res.output, res.output
    assert seen == {"record": records, "asker_records": records}


# ---------------------------------------------------------------------------
# where the question can be shown, and the root -y
# ---------------------------------------------------------------------------

class _Stream:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def test_the_question_needs_stdin_and_stderr_on_a_terminal(monkeypatch):
    import anyuse.cli as C
    for stdin, stderr, attached in ((True, True, True), (True, False, False),
                                    (False, True, False), (False, False, False)):
        monkeypatch.setattr(sys, "stdin", _Stream(stdin))
        monkeypatch.setattr(sys, "stderr", _Stream(stderr))
        assert C._terminal_attached() is attached, (stdin, stderr)


def test_stderr_redirected_from_a_terminal_refuses_at_once_instead_of_waiting(monkeypatch):
    # `anyuse workflow compile X --recording d > out.txt 2>&1` typed at a terminal: stdin is one,
    # stderr is the file, so a prompt would wait for an answer to a question nobody sees
    import click
    monkeypatch.setattr(sys, "stdin", _Stream(True))
    monkeypatch.setattr(sys, "stderr", _Stream(False))
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: pytest.fail("asked without a terminal"))
    q = AP.HostQuestion(host=HOST, url=f"{ORIGIN}/api/search", robots_allowed=True, requests=(2, 2))
    assert _NewHostQuestion()(q) == (False, f"{HOST} is a new host; re-run with --yes, or run "
                                            f"`anyuse workflow acknowledge-host {HOST}` first")


def test_the_root_auto_confirm_answers_the_question(site, monkeypatch):
    res = _compile(site, root=("-y",), monkeypatch=monkeypatch)
    assert res.exit_code == 0, res.output
    assert QUESTION + " yes (--auto-confirm)" in res.output
    assert "Fresh-input proof: PASSED (2/2" in res.output
    assert AP.load_hosts(site["hosts"])[HOST]["note"] == AP.CONFIRMED_AT_COMPILE


def test_compile_last_runs_with_the_question_installed(monkeypatch):
    from anyuse.execution import attach as A
    seen = {}

    def compile_sessions(sessions, **kw):
        seen["asker"] = AP.host_asker()
        raise A.AttachError("stop here")
    monkeypatch.setattr(A, "list_sessions", lambda: [])
    monkeypatch.setattr(A, "select_sessions", lambda s, task="", n=0: s)
    monkeypatch.setattr(A, "compile_sessions", compile_sessions)
    res = CliRunner().invoke(cli, ["compile", "last", "--yes"])
    assert res.exit_code == 2 and "stop here" in res.output
    assert isinstance(seen["asker"], _NewHostQuestion) and seen["asker"].yes
    assert AP.host_asker() is None                                 # removed when the command ends


# ---------------------------------------------------------------------------
# the preflight: what is asked, what is recorded, and where nothing is asked
# ---------------------------------------------------------------------------

def _compiled(tmp_path, monkeypatch, robots=ROBOTS_ALLOW):
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(tmp_path / "api_hosts.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    monkeypatch.setattr(AP, "_default_fetch", lambda url, *a, **kw: robots)
    seen = {}

    def stop(compiled):
        seen["compiled"] = compiled
        return "stop"
    AV.compile_and_verify(_recording("river", "rec"), [_recording("stone", "f1"),
                                                      _recording("cloud", "f2")],
                          executor=ApiStepExecutor(transport=Catalog()), preflight=stop)
    return seen["compiled"]


def test_the_preflight_asks_with_the_proof_request_count_and_a_yes_is_recorded(tmp_path, monkeypatch):
    compiled = _compiled(tmp_path, monkeypatch)
    assert compiled.proof_requests == (4, 4)
    questions = []
    with AP.asking_hosts(lambda q: questions.append(q) or (True, "")):
        assert AV.permission_preflight(compiled, _recording("river", "rec")) == ""
    assert [(q.host, q.robots_allowed, q.requests, q.purpose) for q in questions] == [
        (HOST, True, (4, 4), AP.PURPOSE_PROOF)]
    entry = AP.load_hosts()[HOST]
    assert entry == {**entry, "acknowledged": True, "note": AP.CONFIRMED_AT_COMPILE, "by": ""}


def test_a_no_returns_the_askers_reason_and_records_nothing(tmp_path, monkeypatch):
    compiled = _compiled(tmp_path, monkeypatch)
    with AP.asking_hosts(lambda q: (False, "you answered no for " + q.host)):
        why = AV.permission_preflight(compiled, _recording("river", "rec"))
    assert why == f"proof not run: you answered no for {HOST}"
    assert not AP.host_acknowledged(HOST)


def test_robots_disallow_is_never_put_to_the_asker(tmp_path, monkeypatch):
    compiled = _compiled(tmp_path, monkeypatch, robots=ROBOTS_DENY)
    questions = []
    with AP.asking_hosts(lambda q: questions.append(q) or (True, "")):
        why = AV.permission_preflight(compiled, _recording("river", "rec"))
    assert why.startswith("proof permission refused") and "robots.txt disallows" in why
    assert questions == [] and not AP.host_acknowledged(HOST)


def test_without_an_asker_nothing_is_asked_and_the_refusal_is_unchanged(tmp_path, monkeypatch):
    # MCP tools, benches and background work install no asker
    compiled = _compiled(tmp_path, monkeypatch)
    assert AP.host_asker() is None
    why = AV.permission_preflight(compiled, _recording("river", "rec"))
    assert why == (f"proof permission refused {ORIGIN}/api/search: host {HOST!r} is not "
                   f"acknowledged; run `anyuse workflow acknowledge-host {HOST}`")
    assert not AP.host_acknowledged(HOST)


def test_acknowledge_host_prints_what_it_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(tmp_path / "api_hosts.json"))
    res = CliRunner().invoke(cli, ["workflow", "acknowledge-host", HOST], obj={})
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"✓ {HOST} acknowledged at ")
    assert lines[0].endswith(f"({tmp_path / 'api_hosts.json'})")


def test_the_registry_matches_a_host_whatever_its_case(tmp_path, monkeypatch):
    # acknowledge-host stores host_key's lower case; a tool's URL and the inline question give the netloc as written
    hosts = str(tmp_path / "api_hosts.json")
    AP.acknowledge_host(AP.host_key("https://Pkgs.Example/"), path=hosts, ignore_robots=True,
                        robots_reason="my own site", readonly_post_endpoint="https://Pkgs.Example/graphql",
                        readonly_post_operation="Search")
    assert list(AP.load_hosts(hosts)) == ["pkgs.example"]
    assert AP.host_acknowledged("Pkgs.Example", hosts) and AP.host_acknowledged("PKGS.EXAMPLE", hosts)
    assert AP.host_override("Pkgs.Example", hosts)
    assert AP.readonly_post_approved("https://PKGS.example/graphql", "Search", hosts)
    # a key written as the URL gave it is found by the lower-case form, and a second write keeps one entry
    AP.acknowledge_host("Other.Example", path=hosts, ignore_robots=True, robots_reason="written permission")
    assert AP.host_acknowledged("other.example", hosts) and AP.host_override("other.example", hosts)
    AP.acknowledge_host("other.example", path=hosts, note="again")
    assert sorted(AP.load_hosts(hosts)) == ["Other.Example", "pkgs.example"]
    assert AP.host_entry(AP.load_hosts(hosts), "OTHER.example")["note"] == "again"
    assert AP.host_override("other.example", hosts)["reason"] == "written permission"
    # a yes that is not recorded holds for the host in any case too
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(tmp_path / "none.json"))
    verdict = AP.PermissionVerdict(host="Third.Example", robots_allowed=True, acknowledged=False)
    with AP.asking_hosts(lambda q: (True, ""), record=False):
        assert AP.ask_for_host("https://Third.Example/api", verdict) == (True, "")
        assert AP.host_acknowledged("third.example") and AP.host_acknowledged("THIRD.example")
    assert not AP.host_acknowledged("third.example")


def test_no_mcp_module_installs_an_asker():
    for path in (ROOT / "mcpserver").glob("*.py"):
        assert "asking_hosts" not in path.read_text(), path.name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
