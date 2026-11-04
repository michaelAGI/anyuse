"""Runtime tool writing (execution/harness_toolwrite.py), offline.
Pinned here: the spec's shape; the static checks accept a plain transform and refuse subprocess, os,
socket, eval/exec, open, a dunder attribute, `from x import *`, global, async code, a missing `run`;
the proof derives fresh value sets (never an example), checks the declared types, the examples, the
writer's invariants, determinism and the harness's own CSV/JSON round-trips, and fails each for its
own reason; the registry saves a proven tool with its hash and refuses a changed one; a proven tool
runs at 0 model calls; the fake sandbox blocks imports off the allowlist and files outside the
workspace; the container sandbox builds its argv (no host mount but the workspace, network off) and
never starts without a runner. No subprocess, no network, no model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_harness_toolwrite.py
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

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_toolwrite as TW  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import model_adapter as M  # noqa: E402

TABLE_TO_CSV = '''
import csv, io

def run(inputs):
    rows = inputs["rows"]
    cols = inputs["columns"]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return {"csv": out.getvalue(), "n": len(rows)}
'''


def table_spec(**over):
    spec = {"name": "table_to_csv", "description": "rows of dicts -> CSV with a header",
            "inputs": {"rows": "list", "columns": "list"}, "outputs": {"csv": "csv", "n": "int"},
            "effect": "transform", "code": TABLE_TO_CSV,
            "examples": [{"inputs": {"rows": [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}], "columns": ["a", "b"]},
                          "outputs": {"csv": "a,b\n1,x\n2,y\n", "n": 2}}],
            "invariants": ["outputs['n'] == len(inputs['rows'])", "outputs['csv'].startswith(','.join(inputs['columns']))"]}
    spec.update(over)
    return spec


def ws():
    return tempfile.mkdtemp(prefix="anyuse-tw-")


def registry():
    return TW.ToolRegistry(tempfile.mkdtemp(prefix="anyuse-reg-"))


# ---------------------------------------------------------------------------
# the spec and the static checks
# ---------------------------------------------------------------------------
def test_spec_shape_is_checked():
    spec, probs = TW.ToolSpec.from_dict(table_spec())
    assert spec is not None and probs == [] and spec.sha256()
    for bad, word in ((table_spec(name="Bad Name"), "name"), (table_spec(inputs={}), "inputs"),
                      (table_spec(outputs={"x": "blob"}), "outputs.x"), (table_spec(effect="shell"), "effect"),
                      (table_spec(code=""), "code"), (table_spec(examples=[]), "examples"),
                      (table_spec(effect="http_read"), "host"), ("nope", "object")):
        s, p = TW.ToolSpec.from_dict(bad)
        assert s is None and any(word in x for x in p), (word, p)


def test_static_checks_accept_the_transform_and_refuse_the_unsafe():
    spec, _ = TW.ToolSpec.from_dict(table_spec())
    rep = TW.static_check(spec)
    assert rep.ok and rep.imports == ["csv", "io"], rep.problems
    unsafe = {
        "subprocess": "import subprocess\ndef run(inputs):\n    return {'csv': subprocess.check_output(['ls']).decode(), 'n': 0}\n",
        "os.system": "import os\ndef run(inputs):\n    os.system('rm -rf /')\n    return {'csv': '', 'n': 0}\n",
        "socket": "import socket\ndef run(inputs):\n    socket.create_connection(('h', 80))\n    return {'csv': '', 'n': 0}\n",
        "eval": "def run(inputs):\n    return {'csv': eval(inputs['rows'][0]), 'n': 0}\n",
        "exec": "def run(inputs):\n    exec('x = 1')\n    return {'csv': '', 'n': 0}\n",
        "open": "def run(inputs):\n    return {'csv': open('/etc/passwd').read(), 'n': 0}\n",
        "dunder": "def run(inputs):\n    return {'csv': str(().__class__.__bases__[0].__subclasses__()), 'n': 0}\n",
        "dunder string": "def run(inputs):\n    k = '__builtins__'\n    return {'csv': k, 'n': 0}\n",
        "import *": "from json import *\ndef run(inputs):\n    return {'csv': '', 'n': 0}\n",
        "global": "G = []\ndef run(inputs):\n    global G\n    return {'csv': '', 'n': 0}\n",
        "async": "async def helper():\n    return 1\ndef run(inputs):\n    return {'csv': '', 'n': 0}\n",
        "no run": "def go(inputs):\n    return {}\n",
        "two args": "def run(inputs, extra):\n    return {}\n",
        "getattr": "import json\ndef run(inputs):\n    return {'csv': str(getattr(json, 'loads')), 'n': 0}\n",
        "pathlib": "import pathlib\ndef run(inputs):\n    return {'csv': pathlib.Path('/').read_text(), 'n': 0}\n",
        "fetch in a transform": "def run(inputs):\n    return {'csv': fetch('https://x.test/'), 'n': 0}\n",
        "syntax": "def run(inputs)\n    return {}\n",
        "bad invariant": TABLE_TO_CSV,
    }
    for name, code in unsafe.items():
        over = {"code": code}
        if name == "bad invariant":
            over["invariants"] = ["__import__('os')"]
        s, p = TW.ToolSpec.from_dict(table_spec(**over))
        assert s is not None, p
        rep = TW.static_check(s)
        assert not rep.ok and rep.problems, name
    assert "line 1" in TW.static_check(TW.ToolSpec.from_dict(table_spec(code=unsafe["subprocess"]))[0]).problems[0]


# ---------------------------------------------------------------------------
# the proof
# ---------------------------------------------------------------------------
def test_fresh_inputs_are_never_the_examples_and_keep_the_types():
    spec, _ = TW.ToolSpec.from_dict(table_spec())
    sets = TW.fresh_inputs(spec, 3)
    assert len(sets) == 3 and all(s != spec.examples[0]["inputs"] for s in sets) and len({json.dumps(s) for s in sets}) == 3
    for s in sets:
        assert isinstance(s["rows"], list) and all(isinstance(r, dict) for r in s["rows"]) and isinstance(s["columns"], list)
    assert TW._fresh_value(True, 0) is False and TW._fresh_value(3, 0) == 10 and TW._fresh_value("Bosch 12V", 0) != "Bosch 12V"


def test_the_proof_passes_the_csv_tool_at_zero_model_calls():
    spec, _ = TW.ToolSpec.from_dict(table_spec())
    mark = M.process_call_count()
    proof = TW.prove_tool(spec, TW.FakeSandbox(), workspace=ws())
    assert proof.passed, proof.reason
    kinds = [c["kind"] for c in proof.cases]
    assert kinds.count("example") == 1 and kinds.count("fresh") == 2 and proof.sandbox == "fake"
    assert proof.model_calls == 0 == M.calls_since(mark)
    assert all(c["outputs"]["n"] == len(c["inputs"]["rows"]) for c in proof.cases if c["kind"] == "fresh")


def test_the_proof_fails_for_each_of_its_own_reasons():
    sb = TW.FakeSandbox()
    # the examples do not reproduce
    s, _ = TW.ToolSpec.from_dict(table_spec(examples=[{"inputs": {"rows": [{"a": "1"}], "columns": ["a"]},
                                                        "outputs": {"csv": "wrong", "n": 1}}]))
    p = TW.prove_tool(s, sb)
    assert not p.passed and "do not reproduce" in p.reason
    # a declared type that does not hold
    s, _ = TW.ToolSpec.from_dict(table_spec(outputs={"csv": "csv", "n": "str"}, examples=[
        {"inputs": {"rows": [{"a": "1"}], "columns": ["a"]}, "outputs": {"csv": "a\n1\n", "n": 1}}]))
    p = TW.prove_tool(s, sb)
    assert not p.passed and "not a str" in p.reason
    # a writer's invariant that does not hold on fresh inputs
    s, _ = TW.ToolSpec.from_dict(table_spec(invariants=["outputs['n'] == 2"]))
    p = TW.prove_tool(s, sb)
    assert not p.passed and "invariant" in p.reason and "does not hold" in p.reason
    # not deterministic (the clock)
    code = ("import datetime\ndef run(inputs):\n    t = datetime.datetime.now().microsecond\n"
            "    return {'csv': 'a\\n%d\\n' % t, 'n': t}\n")
    s, _ = TW.ToolSpec.from_dict(table_spec(code=code, invariants=[], examples=[
        {"inputs": {"rows": [{"a": "1"}], "columns": ["a"]}, "outputs": {"csv": "a\n1\n", "n": 1}}]))
    p = TW.prove_tool(s, sb)
    assert not p.passed and ("disagree" in p.reason or "do not reproduce" in p.reason)
    # the harness's own CSV round-trip: ragged rows
    code = "def run(inputs):\n    return {'csv': 'a,b\\n1\\n2,3,4\\n', 'n': len(inputs['rows'])}\n"
    s, _ = TW.ToolSpec.from_dict(table_spec(code=code, invariants=[], examples=[
        {"inputs": {"rows": [{"a": "1"}, {"a": "2"}], "columns": ["a", "b"]}, "outputs": {"csv": "a,b\n1\n2,3,4\n", "n": 2}}]))
    p = TW.prove_tool(s, sb)
    assert not p.passed and "different widths" in p.reason
    # a tool that raises on fresh inputs
    code = "def run(inputs):\n    if inputs['columns'] != ['a', 'b']:\n        raise ValueError('only a,b')\n    return {'csv': 'a,b\\n', 'n': 0}\n"
    s, _ = TW.ToolSpec.from_dict(table_spec(code=code, invariants=[], examples=[
        {"inputs": {"rows": [], "columns": ["a", "b"]}, "outputs": {"csv": "a,b\n", "n": 0}}]))
    p = TW.prove_tool(s, sb)
    assert not p.passed and "ValueError: only a,b" in p.reason
    # too few fresh sets
    p = TW.prove_tool(TW.ToolSpec.from_dict(table_spec())[0], sb, fresh=[{"rows": [], "columns": ["z"]}])
    assert not p.passed and "1 fresh value set" in p.reason


def test_the_fake_sandbox_blocks_imports_files_and_times_out():
    sb = TW.FakeSandbox(timeout_s=0.5)
    s, _ = TW.ToolSpec.from_dict(table_spec(code="import json\ndef run(inputs):\n    import os\n    return {'csv': os.getcwd(), 'n': 0}\n"))
    r = sb.run(s, {"rows": [], "columns": []})
    assert not r.ok and "not on the allowlist" in r.why
    s, _ = TW.ToolSpec.from_dict(table_spec(code="def run(inputs):\n    return {'csv': read_input('../../etc/hosts'), 'n': 0}\n"))
    r = sb.run(s, {"rows": [], "columns": []}, workspace=ws())
    assert not r.ok and "workspace" in r.why
    s, _ = TW.ToolSpec.from_dict(table_spec(code="def run(inputs):\n    write_output('r.csv', 'a\\n')\n    return {'csv': 'a\\n', 'n': 0}\n"))
    w = ws()
    r = sb.run(s, {"rows": [], "columns": []}, workspace=w)
    assert r.ok and r.written == ["out/r.csv"] and os.path.isfile(os.path.join(w, "out", "r.csv"))
    s, _ = TW.ToolSpec.from_dict(table_spec(code="def run(inputs):\n    while True:\n        pass\n"))
    r = sb.run(s, {"rows": [], "columns": []})
    assert not r.ok and "timed out" in r.why
    s, _ = TW.ToolSpec.from_dict(table_spec(code="def run(inputs):\n    return [1]\n"))
    assert "not a dict" in sb.run(s, {}).why
    # no fetch for a transform; a fetch only to the declared host for http_read
    s, _ = TW.ToolSpec.from_dict(table_spec(effect="http_read", host="api.shop.test",
                                            code="def run(inputs):\n    return {'csv': fetch('https://api.shop.test/x'), 'n': 1}\n"))
    r = sb.run(s, {"rows": [], "columns": []}, fetch=lambda u: "a\n1\n")
    assert r.ok and r.fetched == ["https://api.shop.test/x"]
    s, _ = TW.ToolSpec.from_dict(table_spec(effect="http_read", host="api.shop.test",
                                            code="def run(inputs):\n    return {'csv': fetch('https://other.test/x'), 'n': 1}\n"))
    r = sb.run(s, {"rows": [], "columns": []}, fetch=lambda u: "a\n1\n")
    assert not r.ok and "not the declared host" in r.why


# ---------------------------------------------------------------------------
# the registry and the whole path
# ---------------------------------------------------------------------------
def test_write_tool_saves_a_proven_tool_and_runs_it_at_zero_model_calls():
    reg, sb = registry(), TW.FakeSandbox()
    rep = TW.write_tool(table_spec(), reg, sb, workspace=ws())
    assert rep["saved"] and rep["name"] == "table_to_csv" and rep["sha256"], rep
    assert reg.names() == ["table_to_csv"] and reg.load("table_to_csv")["status"] == "proven"
    mark = M.process_call_count()
    out = reg.run("table_to_csv", {"rows": [{"a": "9", "b": "z"}], "columns": ["a", "b"]}, sb)
    assert out["ok"] and out["outputs"] == {"csv": "a,b\n9,z\n", "n": 1} and out["model_calls"] == 0 == M.calls_since(mark)
    assert "missing input" in reg.run("table_to_csv", {"rows": []}, sb)["why"]
    assert "no proven tool" in reg.run("nope_tool", {}, sb)["why"]
    # a changed code never runs under the saved name
    rec = reg.load("table_to_csv")
    rec["spec"]["code"] = rec["spec"]["code"].replace("len(rows)", "len(rows) + 1")
    with open(os.path.join(reg.dir, "table_to_csv.json"), "w") as fh:
        json.dump(rec, fh)
    assert reg.spec_of("table_to_csv") is None and "no longer matches" in reg.run("table_to_csv", {"rows": [], "columns": []}, sb)["why"]


def test_refused_tools_become_drafts_with_their_reasons():
    reg, sb = registry(), TW.FakeSandbox()
    rep = TW.write_tool(table_spec(code="import os\ndef run(inputs):\n    return {'csv': '', 'n': 0}\n"), reg, sb)
    assert not rep["saved"] and rep["why"].startswith("static checks") and rep["proof"] is None
    assert reg.names() == [] and os.path.isfile(os.path.join(reg.dir, "drafts", "table_to_csv.json"))
    rep = TW.write_tool(table_spec(invariants=["outputs['n'] == 2"]), reg, sb)
    assert not rep["saved"] and rep["why"].startswith("proof") and rep["proof"]["passed"] is False
    assert reg.names() == []
    rep = TW.write_tool({"name": "x"}, reg, sb)
    assert not rep["saved"] and rep["why"].startswith("spec")
    # a later proven version replaces the draft
    rep = TW.write_tool(table_spec(), reg, sb)
    assert rep["saved"] and not os.path.exists(os.path.join(reg.dir, "drafts", "table_to_csv.json"))


def test_the_container_sandbox_is_wired_and_never_started_here():
    spec, _ = TW.ToolSpec.from_dict(table_spec())
    w = ws()
    cs = TW.ContainerSandbox()
    argv = cs.plan(spec, w)
    assert argv[:3] == ["container", "run", "--rm"] and "--network" in argv and argv[argv.index("--network") + 1] == "none"
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert len(mounts) == 3 and all(f"source={os.path.realpath(w)}/" in m for m in mounts)
    assert sum("readonly" in m for m in mounts) == 2 and any("target=/task/out" in m for m in mounts)
    assert argv[-4:] == ["python3", "-I", "-S", "/task/tool/main.py"] and not any("ANYUSE" in a or "KEY" in a for a in argv)
    r = cs.run(spec, {"rows": [], "columns": []}, workspace=w)
    assert not r.ok and "not started" in r.why
    import ast
    tree = ast.parse(open(TW.__file__, encoding="utf-8").read())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
        {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"subprocess", "socket", "multiprocessing"}, imported
    # with an injected runner: the tool file and its inputs are staged, the runner's output is read
    seen = []

    def runner(argv, stdin_text, timeout):
        seen.append(argv)
        with open(os.path.join(w, "tool", "inputs.json")) as fh:
            inputs = json.load(fh)
        return 0, json.dumps({"outputs": {"csv": "a,b\n", "n": len(inputs["rows"])}}), ""
    r = TW.ContainerSandbox(runner=runner).run(spec, {"rows": [{"a": 1}], "columns": ["a", "b"]}, workspace=w)
    assert r.ok and r.outputs == {"csv": "a,b\n", "n": 1} and seen[0] == argv
    assert "def run(inputs)" in open(os.path.join(w, "tool", "main.py")).read()


def test_the_harness_writes_and_reuses_a_tool_through_the_tools_set():
    reg, sb = registry(), TW.FakeSandbox()
    w = ws()
    tools = HT.RuntimeTools(reg, sb, files=HT.FileTools(w))
    tb = HT.Toolbox(files=tools.files, tools=tools)
    bad = table_spec(name="evil_tool", code="import socket\ndef run(inputs):\n    return {'csv': '', 'n': 0}\n")
    tr = H.Harness(tb, P.ScriptedPolicy([T.WriteTool(bad), T.WriteTool(table_spec()),
                                         T.RunTool("table_to_csv", {"rows": [{"a": "5", "b": "q"}], "columns": ["a", "b"]}),
                                         T.RunTool("evil_tool", {}), T.Done()]),
                   H.HarnessConfig(no_progress_steps=0), run_id="tw").run("turn the table into CSV")
    assert tr.ok, tr.stop_reason
    s = tr.steps
    assert not s[0].outcome.ok and "static checks" in s[0].outcome.why and "TOOL REFUSED" in s[0].outcome.observation.note
    assert s[1].outcome.ok and s[1].outcome.data["saved"] and "TOOL WRITTEN" in s[1].outcome.observation.note
    assert s[2].outcome.ok and s[2].outcome.data["outputs"]["csv"] == "a,b\n5,q\n" and s[2].effect == "read"
    assert not s[3].outcome.ok and "no proven tool" in s[3].outcome.why
    assert tr.model_calls == 0 and reg.names() == ["table_to_csv"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
