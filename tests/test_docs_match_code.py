"""The first-hour docs say what the code does: QUICKSTART.md, COMPILE.md, MCP_WORKFLOWS.md and SETTINGS.md.

Each test checks one claim of a doc against the code it describes. Offline: a fake transport, a temporary
workflow store and host file.
"""
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

DOCS = os.path.join(HERE, "docs")


def doc(name):
    with open(os.path.join(DOCS, name), encoding="utf-8") as fh:
        return fh.read()


def section(text, heading):
    """The text under ``heading`` up to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1:]
    m = re.search(r"^#{1,%d} " % level, rest, re.M)
    return rest[:m.start()] if m else rest


def test_a_failed_web_tool_goes_to_the_browser_replay_then_the_agent():
    # the code: a failed compiled request falls to the browser replay, then the agent, in that order
    from anyuse.execution.api_compile import compiled_record, persist_compiled
    from anyuse.execution.api_permission import acknowledge_host
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
    from anyuse.mcpserver.compiled_tools import run_compiled

    class Storage:
        def __init__(self, base):
            self.base_dir = base

        def get_workflow_dir(self, workflow_id):
            return os.path.join(self.base_dir, f"workflow_{workflow_id}")

        def get(self, name):
            return None

    class Broken:
        def send(self, method, url, headers, body, timeout_s):
            return ApiResponse(200, {"Content-Type": "application/json"}, '{"packages": []}')

    tmp = tempfile.mkdtemp()
    try:
        storage = Storage(tmp)
        tmpl = RequestTemplate(method="GET", url="https://crates.io/api/v1/crates", query={"q": "{q}"},
                               extract={"items": "crates[*].id"},
                               expect={"status_in": [200], "content_type": "json", "path": "crates",
                                       "min_items": 1}, params=["q"])
        persist_compiled(storage, "s1", compiled_record(tmpl, {"passed": True}, workflow="s1"))
        hosts = os.path.join(tmp, "hosts.json")
        acknowledge_host("crates.io", path=hosts)
        out = run_compiled("s1", {"q": "serde"}, storage=storage, executor=ApiStepExecutor(transport=Broken()),
                           hosts_path=hosts, record_outcome=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert not out["ok"] and [t["rung"] for t in out["rungs_tried"]] == ["api", "browser_replay", "agent"], out
    # the docs: no recorded UI path for a web tool; the browser replay and the agent are named in that order
    for name in ("QUICKSTART.md", "COMPILE.md", "MCP_WORKFLOWS.md"):
        text = doc(name)
        for claim in ("hands over\nto the recorded UI path", "to the recorded UI path instead",
                      "fall back to the recorded path", "falls back to the recorded path",
                      "the next rung is the recorded UI path"):
            assert claim not in text, (name, claim)
    broken = section(doc("QUICKSTART.md"), "### 5. Break it")
    tail = broken[broken.index("--no-fallback`,"):]
    assert tail.index("browser replay") < tail.index("agent") < tail.index("you"), tail
    persist = doc("COMPILE.md").split("4. **Persist**", 1)[1].split("\n\n", 1)[0]
    assert "browser replay" in persist and "agent" in persist, persist


def test_pagination_is_a_candidate_that_is_not_proved():
    text = doc("COMPILE.md")
    bullet = text.split("- **Pagination**", 1)[1].split("\n- **", 1)[0]
    assert "proved like everything else" not in bullet, bullet
    assert "candidate" in bullet and "not proved" in bullet and "not saved" in bullet, bullet
    # the code reports it that way, and the compile record is written without it
    with open(os.path.join(HERE, "cli.py"), encoding="utf-8") as fh:
        cli = fh.read()
    assert '"proved": False}' in cli and "CANDIDATE, not proved" in cli
    record_call = cli.split("record = compiled_record(", 1)[1].split("report[\"persisted\"]", 1)[0]
    assert "pagination" not in record_call


def test_settings_lists_every_model_provider():
    from anyuse import settings as S
    row = next(ln for ln in doc("SETTINGS.md").splitlines() if ln.startswith("| `ANYUSE_MODEL_PROVIDER`"))
    for provider in S.MODEL_PROVIDERS:
        if provider:
            assert f"`{provider}`" in row, (provider, row)


def test_settings_names_every_registry_setting():
    from anyuse import settings as S
    text = doc("SETTINGS.md")
    per_role = re.compile(r"ANYUSE_(PLANNER|VISUAL|AGENT|JUDGE|AGENT_L2|AGENT_L3)_MODEL_"
                          r"(PROVIDER|BASE_URL|NAME|API_KEY_ENV|RETRIES|PRICE_IN|PRICE_OUT)$")
    assert "`ANYUSE_<ROLE>_MODEL_<FIELD>`" in text
    missing = [n for n in S.SETTINGS if f"`{n}`" not in text and not per_role.match(n)]
    assert missing == [], missing
    row = next(ln for ln in text.splitlines() if ln.startswith("| `ANYUSE_AGENT_MAX_USD`"))
    assert f"`{S.DEFAULT_AGENT_MAX_USD}`" in row and "`unlimited`" in row and "* |" in row, row
    caps = section(text, "## Spend caps")
    assert "`ANYUSE_AGENT_MAX_USD`" in caps, caps


def test_settings_say_an_agent_budget_of_zero_allows_no_model_calls():
    # the harness reads 0 as the run budget and the daily cap do (tests/test_agent_harness.py)
    row = next(ln for ln in doc("SETTINGS.md").splitlines() if ln.startswith("| `ANYUSE_AGENT_MAX_USD`"))
    assert "`0` allows no model calls" in row, row
    caps = section(doc("SETTINGS.md"), "## Spend caps")
    assert "`0` allows no model calls" in caps, caps


def test_the_codex_cli_preset_row_says_to_name_the_model():
    from anyuse.execution import model_adapter as M
    # no preset model and --ignore-user-config: Codex does not read the user's default, so doctor asks for a name
    assert M.PROVIDER_PRESETS["codex-cli"].model == ""
    argv, _ = M._codex_request("", "", "x", "last.txt", None)
    assert "--ignore-user-config" in argv and "-m" not in argv
    row = next(ln for ln in doc("MODEL_CONFIGURATION.md").splitlines() if ln.startswith("| codex-cli |"))
    assert "set `ANYUSE_MODEL_NAME`" in row and "own model" not in row, row


def test_the_codex_toml_snippets_pass_minus_p():
    for name in ("INSTALL_FOR_AGENTS.md", "MCP_WORKFLOWS.md"):
        blocks = re.findall(r"```toml\n(.*?)```", doc(name), re.S)
        assert blocks, name
        for block in blocks:
            args = re.search(r"^args = \[(.*)\]$", block, re.M).group(1)
            assert [a.strip().strip('"') for a in args.split(",")] == ["-P", "-m", "anyuse.mcpserver"], (name, args)


def test_public_docs_credit_laya_and_avoid_internal_jargon():
    with open(os.path.join(HERE, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()
    with open(os.path.join(HERE, "THIRD_PARTY_NOTICES.md"), encoding="utf-8") as fh:
        notices = fh.read()
    for label, text in (("README.md", readme), ("THIRD_PARTY_NOTICES.md", notices),
                        ("MODEL_CONFIGURATION.md", doc("MODEL_CONFIGURATION.md")),
                        ("SETTINGS.md", doc("SETTINGS.md")),
                        ("REA_ACCESS_ROUTES.md", doc("REA_ACCESS_ROUTES.md"))):
        assert "https://huggingface.co/convaiinnovations/laya" in text, label
        assert "Convai Innovations" in text and "Apache-2.0" in text, label
        # Laya is public; only hosted Jev is described as not publicly available
        for ln in text.splitlines():
            if "not publicly available" in ln.lower():
                assert "jev" in ln.lower() and "laya model (optional and not publicly available" not in ln.lower(), (label, ln)
                assert "laya (optional; not publicly available)" not in ln.lower(), (label, ln)
                assert "`laya` backend (optional; not publicly available)" not in ln.lower(), (label, ln)
    jargon_re = re.compile(
        r"version[- ]1[0-9]|\bV19\b|\bcanary\b|\bcanaries\b|\bbreadth\b|"
        r"page-keyed|\boracle\b|cold-native-flameshot|follow up with the user",
        re.I,
    )
    for name in sorted(os.listdir(DOCS)):
        if name.endswith(".md"):
            text = doc(name)
            assert not jargon_re.search(text), (name, jargon_re.search(text).group(0))
    assert not jargon_re.search(readme)
    assert len(readme.splitlines()) <= 160, len(readme.splitlines())
    assert "## One pipeline, four targets" in readme
    assert "docs/assets/architecture.svg" in readme
    assert ".[laya]" in readme and "tools/confirmer/fetch_laya.py" in readme
    assert os.path.isfile(os.path.join(DOCS, "assets", "architecture.svg"))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
