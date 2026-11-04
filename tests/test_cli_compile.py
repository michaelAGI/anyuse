"""`anyuse workflow compile`, and the top-level group routing it depends on.

Offline: every case here passes a SINGLE recording file, so there are no fresh
cases, so the verifier returns before executing anything. No network, no
desktop, and nothing is written outside a temp directory.
"""
import os

import click
from click.testing import CliRunner

from anyuse.cli import TaskOrSubcommandGroup, cli

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata", "compile")


# ---------------------------------------------------------------------------
# top-level routing
# ---------------------------------------------------------------------------

def _group():
    @click.group(cls=TaskOrSubcommandGroup, invoke_without_command=True)
    @click.argument("task", required=False)
    @click.option("--api-key", default="")
    @click.option("--verbose", "-v", is_flag=True)
    @click.pass_context
    def g(ctx, task, api_key, verbose):
        ctx.obj = {"task": task, "api_key": api_key, "verbose": verbose}

    @g.command("list")
    @click.pass_context
    def lst(ctx):
        click.echo(f"list task={ctx.obj['task']!r} verbose={ctx.obj['verbose']}")

    return g


def test_a_subcommand_is_not_swallowed_by_the_task_argument():
    # The regression: `anyuse workflow list` died with "No such command 'list'"
    # because the group's positional `task` consumed the word "workflow".
    res = CliRunner().invoke(_group(), ["list"])
    assert res.exit_code == 0, res.output
    assert "task=None" in res.output


def test_a_flag_before_a_subcommand_still_routes():
    res = CliRunner().invoke(_group(), ["-v", "list"])
    assert res.exit_code == 0, res.output
    assert "verbose=True" in res.output


def test_an_option_value_is_not_mistaken_for_a_subcommand():
    res = CliRunner().invoke(_group(), ["--api-key", "list", "list"])
    assert res.exit_code == 0, res.output
    assert "task=None" in res.output


def test_a_free_form_task_is_still_a_task():
    res = CliRunner().invoke(_group(), ["open Safari"])
    assert res.exit_code == 0, res.output


def test_every_real_command_group_resolves_again():
    for group_name, sub in [("workflow", "list"), ("workflow", "compile"),
                            ("credential", "list"), ("guide", "list")]:
        group = cli.commands.get(group_name)
        assert group is not None, group_name
        assert sub in group.commands, f"{group_name} {sub}"


# ---------------------------------------------------------------------------
# workflow compile
# ---------------------------------------------------------------------------

def test_compile_reports_the_template_and_refuses_to_persist_without_a_proof():
    res = CliRunner().invoke(cli, ["workflow", "compile", "crates_search",
                                   "--recording", os.path.join(FIXTURES, "crates_search.rec.json")])
    assert res.exit_code == 1, res.output                 # nothing was saved
    assert "Extraction: ok" in res.output
    assert "https://crates.io/api/v1/crates" in res.output
    assert "crates[*].id" in res.output
    # No fresh values were supplied, so there is no proof and nothing is kept.
    assert "NOT PASSED" in res.output
    assert "Not persisted" in res.output
    assert "Persisted" not in res.output.replace("Not persisted", "")
    # No proof request was sent, so the CLI must not claim the template
    # failed to reproduce the UI round).
    assert "proof was not run" in res.output
    assert "did not reproduce the UI" not in res.output


def test_compile_prints_why_a_server_rendered_page_cannot_be_compiled():
    res = CliRunner().invoke(cli, ["workflow", "compile", "npm_search",
                                   "--recording", os.path.join(FIXTURES, "npm_search.rec.json")])
    assert res.exit_code == 1, res.output                 # nothing was saved
    assert "Extraction: failed" in res.output
    assert "server-rendered" in res.output


def test_compile_json_output_carries_extraction_and_equivalence():
    import json as _json
    res = CliRunner().invoke(cli, ["workflow", "compile", "crates_search", "--json",
                                   "--recording", os.path.join(FIXTURES, "crates_search.rec.json")])
    assert res.exit_code == 1, res.output                 # nothing was saved, in --json too
    data = _json.loads(res.output)
    assert data["extraction"]["status"] == "ok"
    assert data["extraction"]["template"]["url"] == "https://crates.io/api/v1/crates"
    assert data["equivalence"]["passed"] is False
    assert data["persisted"] is None


def test_compile_exits_with_an_error_when_the_recording_is_missing():
    res = CliRunner().invoke(cli, ["workflow", "compile", "nope", "--recording", FIXTURES])
    assert res.exit_code != 0
    assert "No recording" in res.output


def test_workflow_list_shows_compiled_web_tools(monkeypatch):
    from anyuse.mcpserver import compiled_tools as CT
    from anyuse.memory import storage as ST
    monkeypatch.setattr(ST.WorkflowStorage, "list_all", lambda self: [])
    monkeypatch.setattr(CT, "list_compiled", lambda storage=None, hosts_path=None: [
        {"workflow": "pkg_search", "host": "registry.example", "host_acknowledged": True,
         "runnable_without_human": True, "proof": {"passed": True, "status": "proved"}}])
    res = CliRunner().invoke(cli, ["workflow", "list"])
    assert res.exit_code == 0 and "pkg_search" in res.output and "proved, acknowledged" in res.output
    res = CliRunner().invoke(cli, ["workflow", "list", "--json"])
    assert '"kind": "web"' in res.output


def test_an_empty_workflow_list_points_at_the_web_path(monkeypatch):
    from anyuse.mcpserver import compiled_tools as CT
    from anyuse.memory import storage as ST
    monkeypatch.setattr(ST.WorkflowStorage, "list_all", lambda self: [])
    monkeypatch.setattr(CT, "list_compiled", lambda storage=None, hosts_path=None: [])
    res = CliRunner().invoke(cli, ["workflow", "list"])
    assert "workflow compile" in res.output and "workflow record" not in res.output


def test_one_har_and_typed_expectations_reach_the_har_importer(tmp_path):
    har = tmp_path / "serde.har"
    har.write_text('{"log": {"entries": []}}')
    res = CliRunner().invoke(cli, ["workflow", "compile", "once", "--from-har", str(har), "--har-param", "q=serde",
                                   "--har-expect", "serde|serde_json|serde_yaml",
                                   "--expect", "regex=regex|regex-syntax|regex-automata",
                                   "--expect", "tokio=tokio|tokio-util|tokio-stream"])
    assert "HAR import refused" in res.output and res.exit_code == 2      # parsed; the empty HAR is refused
    res = CliRunner().invoke(cli, ["workflow", "compile", "once", "--expect", "a=x|y|z"])
    assert res.exit_code == 2 and "--from-har" in res.output


def test_record_once_runs_end_to_end_through_the_text_report(tmp_path, monkeypatch):
    # the documented shape: one HAR, two typed proof cases, the plain text report (no --json)
    import urllib.parse
    import test_har_import as THI
    from anyuse.execution import har_import as HI
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
    sets = {"alpha": THI.ITEMS, "beta": ["Beta One", "Beta Two", "Beta Three"],
            "gamma": ["Gamma One", "Gamma Two", "Gamma Three"]}

    class Offline:
        def send(self, method, url, headers, body, timeout_s):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
            names = sets.get(q)
            return (ApiResponse(200, {"Content-Type": "application/json"}, THI.api_body(names)) if names
                    else ApiResponse(404, {"Content-Type": "application/json"}, "{}"))
    real = HI.compile_from_har
    monkeypatch.setattr(HI, "compile_from_har", lambda cases, **kw: real(
        cases, param_name=kw.get("param_name", "q"), executor=ApiStepExecutor(transport=Offline()),
        preflight=lambda _compiled: ""))
    har = THI.write(str(tmp_path), "alpha.har", THI.har([THI.entry(text=THI.api_body(sets["alpha"]))]))
    res = CliRunner().invoke(cli, ["workflow", "compile", "once", "--no-persist", "--from-har", har,
                                   "--har-param", "q=alpha", "--har-expect", "|".join(sets["alpha"]),
                                   "--expect", "beta=" + "|".join(sets["beta"]),
                                   "--expect", "gamma=" + "|".join(sets["gamma"])])
    assert res.exit_code == 0, (res.output, repr(res.exception))   # --no-persist with a passed proof
    assert "typed: beta" in res.output and "typed: gamma" in res.output, res.output
    assert "Fresh-input proof: PASSED" in res.output, res.output
    # plain words instead of the raw contract, the scope sentence and the split counts
    assert "returns: a list" in res.output and "What was checked: for each of 2 values" in res.output
    for jargon in ("result_contract", "Proof scope:", "split:", "expect:  {"):
        assert jargon not in res.output, jargon


def _isolated_data(tmp_path, monkeypatch):
    """Every path AnyUse writes to under tmp_path/data (and HOME under tmp_path/home);
    robots.txt allows everything and is answered without a socket."""
    from anyuse.execution import api_permission as AP
    data, home = tmp_path / "data", tmp_path / "home"
    for key, value in (("ANYUSE_DATA_DIR", data), ("ANYUSE_SECRETS_DIR", data),
                       ("ANYUSE_API_HOSTS_FILE", data / "api_hosts.json"),
                       ("ANYUSE_ROBOTS_CACHE_FILE", data / "robots.json"),
                       ("ANYUSE_BLOCKED_HOSTS_FILE", data / "blocked.json"),
                       ("HOME", home), ("XDG_CONFIG_HOME", home / ".config"),
                       ("XDG_DATA_HOME", home / ".local" / "share"),
                       ("XDG_CACHE_HOME", home / ".cache")):
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("ANYUSE_ROBOTS", raising=False)
    monkeypatch.delenv("ANYUSE_ROBOTS_POLICY", raising=False)
    monkeypatch.setattr(AP, "_ROBOTS_CACHE", {})
    monkeypatch.setattr(AP, "_ROBOTS_NOTES", {})
    monkeypatch.setattr(AP, "_default_fetch", lambda url, *a, **kw: "User-agent: *\nAllow: /\n")
    return data, home


def _firefox_har(THI, path, items, secrets):
    """A Firefox-style export of one search: cookies on every request, Set-Cookie on the
    response, and a tracking call that carried an Authorization header."""
    cookie = f"consent=yes; prefs_zq={secrets['pref']}; sessionid_zq={secrets['session']}"
    jar = [{"name": p.split("=")[0].strip(), "value": p.split("=")[1]} for p in cookie.split(";")]
    api = THI.entry(url="https://ex.test/api?q=alpha", text=THI.api_body(items),
                    headers={"Cookie": cookie, "Accept": "application/json"}, cookies=jar)
    api["response"]["headers"].append({"name": "Set-Cookie",
                                       "value": f"sessionid_zq={secrets['rotated']}; Path=/"})
    track = THI.entry(url="https://ex.test/track", method="POST", text="{}",
                      headers={"Authorization": f"Bearer {secrets['bearer']}", "Cookie": cookie},
                      post='{"event": "search"}')
    return THI.write(str(path), "alpha.har", THI.har([api, track], creator="Firefox"))


def test_a_har_with_cookies_is_saved_and_no_file_holds_a_cookie_or_a_credential(tmp_path, monkeypatch):
    # the whole CLI path, persisting: the proof runs without the cookies, and nothing
    # AnyUse writes (the tool, the host acknowledgement, robots cache) holds a value
    import urllib.parse
    import test_har_import as THI
    from anyuse.execution import har_import as HI
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
    data, home = _isolated_data(tmp_path, monkeypatch)
    secrets = {"pref": "pv-5521", "session": "sv-8812", "rotated": "rv-0937", "bearer": "bv-6620"}
    sets = {"alpha": THI.ITEMS, "beta": ["Beta One", "Beta Two", "Beta Three"],
            "gamma": ["Gamma One", "Gamma Two", "Gamma Three"]}
    sent = []

    class Offline:
        def send(self, method, url, headers, body, timeout_s):
            sent.append(dict(headers or {}))
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
            return ApiResponse(200, {"Content-Type": "application/json"}, THI.api_body(sets[q]))
    real = HI.compile_from_har
    monkeypatch.setattr(HI, "compile_from_har", lambda cases, **kw: real(
        cases, executor=ApiStepExecutor(transport=Offline()), **kw))
    (tmp_path / "har").mkdir()
    har = _firefox_har(THI, tmp_path / "har", sets["alpha"], secrets)
    res = CliRunner().invoke(cli, ["workflow", "compile", "ff_search", "--yes", "--from-har", har,
                                   "--har-param", "q=alpha", "--har-expect", "|".join(sets["alpha"]),
                                   "--expect", "beta=" + "|".join(sets["beta"]),
                                   "--expect", "gamma=" + "|".join(sets["gamma"])])
    assert res.exit_code == 0, (res.output, repr(res.exception))
    assert "Persisted as the preferred implementation" in res.output, res.output
    assert "1 sent with cookies" in res.output and "1 credential-bound" in res.output, res.output
    # the two proof replays and the two made-up searches that learn the site's empty page
    assert len(sent) == 4
    for headers in sent:
        assert not [k for k in headers if k.lower() in ("cookie", "authorization")], headers
    written = [p for root in (data, home) for p in root.rglob("*") if p.is_file()]
    compiled = [p for p in written if p.name == "compiled_api.json"]
    assert compiled, written
    # the recordings kept for an automatic recompile are among the files scanned
    assert [p for p in written if p.parent.name == "evidence"], written
    for path in written:
        text = path.read_text(errors="replace")
        for value in secrets.values():
            assert value not in text, (path, value)
    record = compiled[0].read_text()
    for name in ("prefs_zq", "sessionid_zq", "cookie_refs", "Bearer"):
        assert name not in record, name
    for value in secrets.values():
        assert value not in res.output, value


def test_four_hars_of_a_chain_are_saved_with_the_second_as_its_fitting_input(tmp_path, monkeypatch):
    import test_chain_sources as TCS
    from anyuse.execution import har_import as HI
    data, _home = _isolated_data(tmp_path, monkeypatch)
    real = HI.compile_from_har
    monkeypatch.setattr(HI, "compile_from_har", lambda cases, **kw: real(
        cases, executor=TCS.executor(), **kw))
    (tmp_path / "har").mkdir()
    args = []
    for q in ("serde", "tokio", "rand", "regex"):
        path = TCS.write_har(str(tmp_path / "har"), q, TCS.traffic(q))
        args += ["--from-har", path, "--har-param", f"q={q}",
                 "--har-expect", "|".join(TCS.releases(TCS.RESULTS[q][0]))]
    res = CliRunner().invoke(cli, ["workflow", "compile", "releases", "--yes", *args])
    assert res.exit_code == 0, (res.output, repr(res.exception))
    assert "  tokio.har: the chain's fitting input" in res.output, res.output
    # the text report names both requests of the chain
    assert "GET https://registry.test/api/search?q={q}" in res.output, res.output
    assert "GET https://registry.test/api/package/{chain_id}/releases" in res.output, res.output
    assert "Persisted as the preferred implementation" in res.output, res.output
    import json
    record = json.loads(next(data.rglob("compiled_api.json")).read_text())
    assert record["source"]["fitting"] == ["tokio.har"]
    assert record["source"]["fresh"] == ["rand.har", "regex.har"]
    assert record["proof"]["split"]["fitting"] == 1 and record["proof"]["status"] == "proved"


def test_three_hars_of_a_chain_are_saved_with_a_generated_fitting_input(tmp_path, monkeypatch):
    import json
    import test_chain_fit as TCF
    import test_chain_sources as TCS
    from anyuse.execution import har_import as HI
    from anyuse.execution.api_step import ApiStepExecutor
    data, _home = _isolated_data(tmp_path, monkeypatch)
    monkeypatch.delenv("ANYUSE_CDP_ENDPOINT", raising=False)
    real = HI.compile_from_har
    monkeypatch.setattr(HI, "compile_from_har", lambda cases, **kw: real(
        cases, executor=ApiStepExecutor(transport=TCF.OpenRegistry()), **kw))
    (tmp_path / "har").mkdir()
    args = []
    for q in ("serde", "tokio", "rand"):
        path = TCS.write_har(str(tmp_path / "har"), q, TCS.traffic(q))
        args += ["--from-har", path, "--har-param", f"q={q}",
                 "--har-expect", "|".join(TCS.releases(TCS.RESULTS[q][0]))]
    res = CliRunner().invoke(cli, ["workflow", "compile", "releases", "--yes", *args])
    assert res.exit_code == 0, (res.output, repr(res.exception))
    assert "Persisted as the preferred implementation" in res.output, res.output
    record = json.loads(next(data.rglob("compiled_api.json")).read_text())
    value = record["chain_fit"]["value"]
    assert value in ("serde_json", "serde_derive")
    assert f"  {value!r}: the chain's fitting input, taken from the first HAR's" in res.output
    assert f"chain fitted on {value!r}" in res.output and "not a proof input" in res.output
    assert record["source"]["fitting"] == []
    assert record["source"]["fresh"] == ["tokio.har", "rand.har"]
    split = record["proof"]["split"]
    assert (split["fitting"], split["fitting_generated"]) == (1, 1)
    assert record["proof"]["status"] == "proved" and "chain_fit" not in record["proof"]


def test_a_recording_folder_without_fit1_compiles_a_chain_on_a_generated_fitting_input(
        tmp_path, monkeypatch):
    import json
    import test_chain_fit as TCF
    import test_chain_sources as TCS
    from anyuse.execution import api_verify as AV
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.har_import import har_to_recording, load_har
    data, _home = _isolated_data(tmp_path, monkeypatch)
    monkeypatch.delenv("ANYUSE_CDP_ENDPOINT", raising=False)
    real = AV.compile_and_verify_with_fit
    monkeypatch.setattr(AV, "compile_and_verify_with_fit", lambda *a, **kw: real(
        *a, executor=ApiStepExecutor(transport=TCF.OpenRegistry()), **kw))
    folder = tmp_path / "rec"
    folder.mkdir()
    for q, name in (("serde", "releases.rec.json"), ("tokio", "releases.fresh1.json"),
                    ("rand", "releases.fresh2.json")):
        har = TCS.write_har(str(tmp_path), q, TCS.traffic(q))
        rec = har_to_recording(load_har(har), "q", q, TCS.releases(TCS.RESULTS[q][0]))
        (folder / name).write_text(json.dumps(rec))
    res = CliRunner().invoke(cli, ["workflow", "compile", "releases", "--yes",
                                   "--recording", str(folder)])
    assert res.exit_code == 0, (res.output, repr(res.exception))
    assert "Persisted as the preferred implementation" in res.output, res.output
    record = json.loads(next(data.rglob("compiled_api.json")).read_text())
    assert f"chain fitted on {record['chain_fit']['value']!r}" in res.output
    assert record["source"]["fitting"] == [] and record["proof"]["status"] == "proved"


def test_a_recording_folder_without_fit1_says_to_add_one_when_no_input_is_generated(
        tmp_path, monkeypatch):
    import json
    import test_chain_sources as TCS
    from anyuse.execution.har_import import har_to_recording, load_har
    _isolated_data(tmp_path, monkeypatch)
    monkeypatch.delenv("ANYUSE_CDP_ENDPOINT", raising=False)
    monkeypatch.setenv("ANYUSE_CHAIN_FIT", "off")
    folder = tmp_path / "rec"
    folder.mkdir()
    for q, name in (("serde", "releases.rec.json"), ("tokio", "releases.fresh1.json"),
                    ("rand", "releases.fresh2.json")):
        har = TCS.write_har(str(tmp_path), q, TCS.traffic(q))
        rec = har_to_recording(load_har(har), "q", q, TCS.releases(TCS.RESULTS[q][0]))
        (folder / name).write_text(json.dumps(rec))
    res = CliRunner().invoke(cli, ["workflow", "compile", "releases", "--yes",
                                   "--recording", str(folder)])
    assert res.exit_code == 1, (res.output, repr(res.exception))
    assert ("Fit it with releases.fit1.json: a recording of another input that opens a "
            "result the same way") in res.output, res.output


def test_a_har_that_needs_its_cookies_is_not_saved_and_says_sign_in(tmp_path, monkeypatch):
    import test_har_import as THI
    from anyuse.execution import har_import as HI
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
    data, _home = _isolated_data(tmp_path, monkeypatch)
    secrets = {"pref": "pv-5521", "session": "sv-8812", "rotated": "rv-0937", "bearer": "bv-6620"}

    class SignedInOnly:
        def send(self, method, url, headers, body, timeout_s):
            if any(k.lower() == "cookie" for k in headers or {}):
                raise AssertionError("a cookie was sent")
            return ApiResponse(401, {"Content-Type": "application/json"}, '{"error": "unauthorized"}')
    real = HI.compile_from_har
    monkeypatch.setattr(HI, "compile_from_har", lambda cases, **kw: real(
        cases, executor=ApiStepExecutor(transport=SignedInOnly()), **kw))
    (tmp_path / "har").mkdir()
    har = _firefox_har(THI, tmp_path / "har", THI.ITEMS, secrets)
    res = CliRunner().invoke(cli, ["workflow", "compile", "ff_search", "--yes", "--from-har", har,
                                   "--har-param", "q=alpha", "--har-expect", "|".join(THI.ITEMS),
                                   "--expect", "beta=Beta One|Beta Two|Beta Three",
                                   "--expect", "gamma=Gamma One|Gamma Two|Gamma Three"])
    assert res.exit_code == 1, (res.output, repr(res.exception))
    assert "Not persisted" in res.output and "probably needs you to be signed in" in res.output
    assert "consent, prefs_zq, sessionid_zq" in res.output, res.output
    assert not list(data.rglob("compiled_api.json"))
    for value in secrets.values():
        assert value not in res.output, value


def test_a_mistyped_group_before_its_subcommand_is_named(monkeypatch):
    import anyuse.cli as C
    monkeypatch.setattr(C, "_run_task", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("agent started")))
    res = CliRunner().invoke(cli, ["wokflow", "list"])
    assert res.exit_code == 2 and "no such command 'wokflow'" in res.output and "workflow" in res.output
    assert "No such command 'list'" not in res.output
    seen = []
    monkeypatch.setattr(C, "_run_task", lambda ctx, task: seen.append(task))
    CliRunner().invoke(cli, ["Open TextEdit and create a note"])
    assert seen == ["Open TextEdit and create a note"]          # a quoted task is still a task


def test_subcommand_usage_lines_leave_out_the_root_task():
    sub = CliRunner().invoke(cli, ["workflow", "play", "--help"])
    assert sub.exit_code == 0 and "workflow play [OPTIONS] NAME" in sub.output, sub.output
    assert "[TASK]" not in sub.output
    root = CliRunner().invoke(cli, ["--help"])
    assert "[OPTIONS] [TASK]" in root.output


def test_compile_help_shows_the_record_once_form():
    res = CliRunner().invoke(cli, ["workflow", "compile", "--help"])
    assert "Record once" in res.output and "--expect regex=" in res.output
    assert "--from-har tokio.har" in res.output


def test_play_names_the_right_next_step_for_a_check_or_an_unknown_tool(monkeypatch):
    import anyuse.cli as C
    from anyuse.mcpserver import compiled_tools as CT
    monkeypatch.setattr(C, "_has_desktop_recording", lambda name: False)
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: {
        "ok": False, "error": "captcha_generic", "action_required": "human",
        "fallback": "complete the check in your own browser session, then run this again",
        "needs_human": {"card": "c7"}})
    res = CliRunner().invoke(cli, ["workflow", "play", "pkg_search", "--param", "q=serde"])
    assert res.exit_code == 1 and "complete the check in your own browser" in res.output
    assert "card c7" in res.output and "recompile" not in res.output
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: {
        "ok": False, "error": "no compiled workflow with that name"})
    res = CliRunner().invoke(cli, ["workflow", "play", "nope", "--param", "q=x"])
    assert "anyuse workflow list" in res.output and "recompile" not in res.output


def test_play_on_a_web_tool_without_a_desktop_recording_gives_a_working_next_step(monkeypatch):
    import anyuse.cli as C
    from anyuse.mcpserver import compiled_tools as CT
    monkeypatch.setattr(C, "_has_desktop_recording", lambda name: False)
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: {
        "ok": False, "error": "pre-send policy refused: host unreachable",
        "fallback": "the recorded UI path is the next rung; this surface does not drive the desktop"})
    res = CliRunner().invoke(cli, ["workflow", "play", "pkg_search", "--param", "q=serde"])
    assert res.exit_code == 1
    assert "record the search again and recompile" in res.output
    assert "recorded UI path" not in res.output and "continuing with the recorded UI path" not in res.output
    monkeypatch.setattr(C, "_web_tool_parameters", lambda name: ["q"])
    res = CliRunner().invoke(cli, ["workflow", "play", "pkg_search"])
    assert res.exit_code == 2 and "--param q=VALUE" in res.output


def test_learn_help_documents_four_values_for_proved():
    res = CliRunner().invoke(cli, ["learn", "--help"])
    assert "serde,regex,tokio,clap" in res.output and '"checked"' in res.output


def test_play_without_fallback_keeps_to_the_compiled_request(monkeypatch):
    import anyuse.cli as C
    from anyuse.mcpserver import compiled_tools as CT
    seen = {}
    monkeypatch.setattr(C, "_has_desktop_recording", lambda name: False)
    monkeypatch.setattr(CT, "run_compiled", lambda name, bound, **kw: seen.update(kw) or {
        "ok": False, "error": "postcondition failed"})
    CliRunner().invoke(cli, ["workflow", "play", "pkg_search", "--param", "q=x", "--no-fallback"])
    assert seen["cascade"] is False
    CliRunner().invoke(cli, ["workflow", "play", "pkg_search", "--param", "q=x"])
    assert seen["cascade"] is True
    res = CliRunner().invoke(cli, ["workflow", "play", "--help"])
    assert "browser replay" in res.output and "agent" in res.output


def test_workflow_list_shows_out_of_service_and_saved_web_tools(monkeypatch):
    from anyuse.execution import harness_compile as HC
    from anyuse.mcpserver import compiled_tools as CT
    from anyuse.memory import storage as ST
    saved = type("W", (), {"name": "crates.io.site_search", "steps": [], "run_count": 0, "success_rate": 1.0,
                           "verified": False, "verification_issues": [], "robustness_score": 0.0,
                           "description": ""})()
    monkeypatch.setattr(ST.WorkflowStorage, "list_all", lambda self: [saved])
    monkeypatch.setattr(HC, "load_web_tool", lambda storage, name: {
        "status": "ready", "site": "crates.io", "param": "q"})
    monkeypatch.setattr(CT, "list_compiled", lambda storage=None, hosts_path=None: [
        {"workflow": "crates_search", "host": "crates.io", "host_acknowledged": True,
         "proof": {"passed": True, "status": "proved"}, "available_via": "browser_replay",
         "health": {"preferred": False}, "degraded": "out of service"}])
    res = CliRunner().invoke(cli, ["workflow", "list"])
    assert "api out of service (served by the browser replay)" in res.output, res.output
    assert "crates.io.site_search  crates.io  ready, input q" in res.output
    assert "Steps: 0" not in res.output


def test_workflow_list_says_when_a_request_out_of_service_is_tried_again(monkeypatch):
    from anyuse.mcpserver import compiled_tools as CT
    from anyuse.memory import storage as ST
    monkeypatch.setattr(ST.WorkflowStorage, "list_all", lambda self: [])
    when = "2026-10-22T12:10:00+02:00"

    def row(name, via, cause):
        return {"workflow": name, "host": "crates.io", "host_acknowledged": True,
                "proof": {"passed": True, "status": "proved"}, "available_via": via,
                "health": {"preferred": False, "out_of_service_cause": cause, "next_check_after": when}}
    monkeypatch.setattr(CT, "list_compiled", lambda storage=None, hosts_path=None: [
        row("due", "api", "outage"), row("down", None, "outage"), row("changed", None, "answers"),
        row("limited", None, "rate_limit")])
    out = CliRunner().invoke(cli, ["workflow", "list"]).output
    assert "served by the api" not in out, out
    assert "api out of service; the next call checks whether it answers again" in out
    down = out.split("  down  ")[1].split("  changed  ")[0]
    assert f"the site did not answer. AnyUse tries it again after {when}" in down, down
    assert "record the search again" not in down
    changed = out.split("  changed  ")[1].split("  limited  ")[0]
    assert f"AnyUse tries it again after {when}" in changed and "record the search again" in changed
    limited = out.split("  limited  ")[1]
    assert (f"the site asked AnyUse to wait (rate limit). AnyUse tries it again after {when}"
            in limited), limited
    assert "record the search again" not in limited and "did not answer" not in limited


def test_compile_last_records_the_selection_session_apart_from_the_held_out_ones(monkeypatch):
    import anyuse.cli as C
    from anyuse.execution import attach as A
    sessions = [{"_path": f"/s/{n}.json"} for n in ("serde", "regex", "tokio", "clap")]
    monkeypatch.setattr(A, "list_sessions", lambda: sessions)
    monkeypatch.setattr(A, "select_sessions", lambda s, task="", n=0: s)
    monkeypatch.setattr(A, "task_name", lambda s: "crates_search")
    monkeypatch.setattr(A, "compile_sessions", lambda s, **kw: (
        object(), object(), {"proof_kind": "list", "n_selection": 1}, [{}]))
    seen = {}
    monkeypatch.setattr(C, "_finish_compile", lambda *a, **kw: seen.update(fresh=a[6], sel=kw["sel_files"]))
    CliRunner().invoke(cli, ["compile", "last", "--json"])
    assert seen["sel"] == ["/s/regex.json"] and seen["fresh"] == ["/s/tokio.json", "/s/clap.json"]


def test_play_on_a_compiled_recording_runs_the_replay(monkeypatch):
    import anyuse.cli as C
    monkeypatch.delenv("ANYUSE_DRIVE_ENABLED", raising=False)
    replay = CliRunner().invoke(cli, ["workflow", "replay", "new_note", "--param", "text=Hi"])
    monkeypatch.setattr(C, "_is_compiled_recording", lambda name: True)
    play = CliRunner().invoke(cli, ["workflow", "play", "new_note", "--param", "text=Hi"])
    assert play.output == replay.output and play.exit_code == replay.exit_code


def test_bare_anyuse_prints_help():
    res = CliRunner().invoke(cli, [])
    assert res.exit_code == 0 and "Usage:" in res.output


def test_a_one_word_typo_is_refused_not_run_as_a_desktop_task(monkeypatch):
    import anyuse.cli as C
    monkeypatch.setattr(C, "_run_task", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("agent started")))
    res = CliRunner().invoke(cli, ["doctr"])
    assert res.exit_code == 2
    assert "no such command 'doctr'" in res.output and "doctor" in res.output


def test_the_documented_har_command_reaches_the_har_importer(tmp_path):
    # the docs' three-HAR shape (docs/QUICKSTART_COMPILE.md): no --recording at all
    args = ["workflow", "compile", "har_case", "--explain"]
    for word in ("river", "stone", "cloud"):
        har = tmp_path / f"{word}.har"
        har.write_text('{"log": {"entries": []}}')
        args += ["--from-har", str(har), "--har-param", f"q={word}", "--har-expect", f"{word}-a|{word}-b"]
    res = CliRunner().invoke(cli, args)
    assert "--recording" not in res.output, res.output
    assert "HAR import refused" in res.output and res.exit_code == 2


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
