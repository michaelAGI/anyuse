"""Composition v0 end to end, offline: two proved compiled tools, one human hand-off.

The example routine ``examples/routines/crates_lookup.manifest.json`` is BUILT
here from real compiled records (``compiled_record`` + the real
``describe_compiled`` rows, via ``step_from_compiled``) and must equal the
checked-in file, so its proof pins are genuine. Execution goes through the real
``compiled_tools.run_compiled`` read-only path with a fake HTTP transport, and
through ``mcpserver/skill_tools.handle`` exactly as an MCP client would call it.
Nothing touches the network, the desktop, a model or the real ``~/.anyuse``.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile

from anyuse.execution.api_compile import compiled_record, persist_compiled
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.routine import CompiledToolRunner, Engine, ReceiptStore
from anyuse.execution.skill_gen import RESUME_TOOL_NAME, mcp_tool, skill_md
from anyuse.execution.skill_manifest import (
    MANIFEST_SCHEMA, step_from_compiled, validate_manifest,
)
from anyuse.mcpserver import compiled_tools, skill_tools

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_DIR = os.path.join(HERE, "examples", "routines")
MANIFEST_FILE = os.path.join(EXAMPLE_DIR, "crates_lookup.manifest.json")
SKILL_FILE = os.path.join(EXAMPLE_DIR, "crates_lookup.SKILL.md")

SEARCH = {"crates": [{"id": "serde"}, {"id": "serde_json"}, {"id": "serde_yaml"}]}
DETAIL = {"serde_json": {"crate": {"name": "serde_json",
                                   "description": "A JSON serialization file format"}}}
PROOF = {"passed": True, "passed_count": 2, "case_count": 2,
         "reason": "passed on 2 fresh value(s) not present in the recording"}


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        if "/crates?" in url:
            return ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(SEARCH))
        name = url.rsplit("/", 1)[-1]
        if name in DETAIL:
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps(DETAIL[name]))
        return ApiResponse(404, {"Content-Type": "application/json"}, "{}")


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


def search_template():
    return RequestTemplate(
        method="GET", url="https://crates.io/api/v1/crates", query={"q": "{q}"},
        headers={"accept": "application/json"}, extract={"items": "crates[*].id"},
        expect={"status_in": [200], "content_type": "json", "path": "crates",
                "min_items": 1, "required_keys": ["id"]}, params=["q"])


def detail_template(url="https://crates.io/api/v1/crates/{crate}"):
    return RequestTemplate(
        method="GET", url=url, headers={"accept": "application/json"},
        extract={"name": "crate.name", "description": "crate.description"},
        expect={"status_in": [200], "content_type": "json", "path": "crate",
                "non_empty": True}, params=["crate"], shape="fields")


def setup(tmp):
    storage = Storage(os.path.join(tmp, "wf"))
    for name, tmpl in (("crates_search", search_template()),
                       ("crates_detail", detail_template())):
        persist_compiled(storage, name, compiled_record(
            tmpl, PROOF, workflow=name, source={"recording": f"{name}.rec.json"}))
    hosts = os.path.join(tmp, "hosts.json")
    acknowledge_host("crates.io", path=hosts)
    return storage, hosts


def build_manifest(storage, hosts):
    rows = {r["workflow"]: r for r in compiled_tools.list_compiled(storage, hosts)}
    search = step_from_compiled("search", rows["crates_search"], "crates")
    search["postconditions"] = [{"op": "len_min", "ref": "$out.items", "value": 1}]
    pick = {
        "id": "pick", "mode": "human",
        "action": {"kind": "human", "instruction":
                   "Pick exactly one crate id from the search results."},
        "evidence": {"status": "none"},
        "inputs": {"candidates": {"type": "array", "items": {"type": "scalar"}}},
        "outputs": {"crate": {"type": "string", "maxLength": 100}},
        "bindings": {"candidates": {"kind": "derived", "step": "search",
                                    "output": "items"}},
        "postconditions": [{"op": "in", "ref": "$out.crate",
                            "in": {"ref": "$in.candidates"}}],
        "effects": {"class": "read_only", "reversible": True},
        "approval": {"required": False},
        "autonomy": {"level": "rule"},
    }
    detail = step_from_compiled(
        "detail", rows["crates_detail"], "crates", ["name", "description"],
        bindings={"crate": {"kind": "derived", "step": "pick", "output": "crate"}})
    detail["preconditions"] = [{"op": "non_empty", "ref": "$in.crate"}]
    detail["postconditions"] = [{"op": "eq", "ref": "$out.name",
                                 "value": {"ref": "$in.crate"}}]
    return {
        "schema": MANIFEST_SCHEMA, "kind": "routine", "name": "crates_lookup",
        "version": "0.1.0",
        "summary": "Search crates.io, let a person pick one result, then read its details.",
        "inputs": {"q": {"type": "string", "maxLength": 100, "minLength": 1}},
        "apps": {"crates": {"origin": "https://crates.io", "tenant": "public"}},
        "data_transfers": [],
        "steps": [search, pick, detail],
        "outputs": {
            "name": {"type": "scalar", "from": {"step": "detail", "output": "name"}},
            "description": {"type": "scalar",
                            "from": {"step": "detail", "output": "description"}},
        },
    }


class Tmp:
    def __enter__(self):
        self.path = tempfile.mkdtemp()
        return self.path

    def __exit__(self, *_):
        shutil.rmtree(self.path, ignore_errors=True)


def _dump(manifest):
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def test_example_files_are_generated_from_real_compiled_records():
    with Tmp() as tmp:
        manifest = build_manifest(*setup(tmp))
        assert validate_manifest(manifest) == []
        assert [s["mode"] for s in manifest["steps"]] == ["proved", "human", "proved"]
        want = {MANIFEST_FILE: _dump(manifest), SKILL_FILE: skill_md(manifest)}
        for path, text in want.items():
            have = open(path, encoding="utf-8").read() if os.path.exists(path) else None
            if have != text:
                out = os.path.join(os.environ.get("HOME", tmp), "generated",
                                   os.path.basename(path))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(text)
                raise AssertionError(f"{path} is stale; regenerated copy at {out}")


def test_routine_runs_end_to_end_with_one_handoff_and_durable_resume():
    with Tmp() as tmp:
        storage, hosts = setup(tmp)
        manifest = json.load(open(MANIFEST_FILE))
        tr = FakeTransport()
        runner = CompiledToolRunner(storage, hosts, ApiStepExecutor(transport=tr))
        store_dir = os.path.join(tmp, "runs")
        eng = Engine(manifest, {"compiled_tool": runner}, ReceiptStore(store_dir))
        res = eng.run({"q": "serde"})
        assert res["status"] == "handed_off", res
        nxt = res["handoff"]["next_step"]
        assert nxt["id"] == "pick"
        assert nxt["bound_inputs"]["candidates"] == ["serde", "serde_json", "serde_yaml"]
        assert len(tr.sent) == 1 and tr.sent[0].endswith("q=serde")

        # "process restart": new runner, engine and store objects
        tr2 = FakeTransport()
        runner2 = CompiledToolRunner(storage, hosts, ApiStepExecutor(transport=tr2))
        eng2 = Engine(json.load(open(MANIFEST_FILE)), {"compiled_tool": runner2},
                      ReceiptStore(store_dir))
        done = eng2.resume(res["run_id"], expected_head=res["handoff"]["head"],
                           human={"step": "pick", "outputs": {"crate": "serde_json"}})
        assert done["status"] == "completed", done
        assert done["outputs"] == {"name": "serde_json",
                                   "description": "A JSON serialization file format"}
        assert tr2.sent == ["https://crates.io/api/v1/crates/serde_json"]
        chain = ReceiptStore(store_dir).load(res["run_id"])
        detail = [r for r in chain if r.get("step") == "detail" and r["type"] == "step"][0]
        assert detail["action"]["proof_id"] == manifest["steps"][2]["action"]["proof_id"]
        assert detail["provenance"]["llm_calls"] == 0


def test_a_recompiled_tool_stops_the_routine_at_the_fresh_check():
    with Tmp() as tmp:
        storage, hosts = setup(tmp)
        manifest = json.load(open(MANIFEST_FILE))
        tr = FakeTransport()
        runner = CompiledToolRunner(storage, hosts, ApiStepExecutor(transport=tr))
        store = ReceiptStore(os.path.join(tmp, "runs"))
        eng = Engine(manifest, {"compiled_tool": runner}, store)
        res = eng.run({"q": "serde"})
        # someone re-compiles the detail tool while the run waits for the human
        persist_compiled(storage, "crates_detail", compiled_record(
            detail_template("https://crates.io/api/v1/crates/{crate}?v=2"), PROOF,
            workflow="crates_detail"))
        res = eng.resume(res["run_id"], human={"step": "pick",
                                               "outputs": {"crate": "serde_json"}})
        assert res["status"] == "refused" and "changed since the manifest pinned" in res["error"]
        assert len(tr.sent) == 1


def test_mcp_surface_lists_typed_tools_and_runs_and_resumes():
    with Tmp() as tmp:
        storage, hosts = setup(tmp)
        skills = os.path.join(tmp, "skills")
        os.makedirs(skills)
        shutil.copy(MANIFEST_FILE, skills)
        with open(os.path.join(skills, "broken.manifest.json"), "w") as fh:
            fh.write('{"schema": "anyuse.manifest/1", "kind": "skill"}')
        tools = skill_tools.tool_definitions(skills)
        # the read-only human-inbox list rides along with the resume tool
        assert [t["name"] for t in tools] == ["routine__crates_lookup", RESUME_TOOL_NAME,
                                              "anyuse_inbox_list"]
        assert tools[0] == mcp_tool(json.load(open(MANIFEST_FILE)))
        kw = dict(directory=skills, storage=storage, hosts_path=hosts,
                  executor=ApiStepExecutor(transport=FakeTransport()))
        assert skill_tools.handle("run_compiled_workflow", {}, **kw) is None
        res = skill_tools.handle("routine__crates_lookup", {"q": "serde"}, **kw)
        assert res["status"] == "handed_off"
        done = skill_tools.handle(RESUME_TOOL_NAME, {
            "run_id": res["run_id"], "expected_head": res["handoff"]["head"],
            "human": {"step": "pick", "outputs": {"crate": "serde_json"}}}, **kw)
        assert done["status"] == "completed" and done["outputs"]["name"] == "serde_json"
        bad = skill_tools.handle(RESUME_TOOL_NAME, {"run_id": "nope"}, **kw)
        assert bad["status"] == "refused"


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    passed = failed = 0
    for name, fn in [(n, f) for n, f in vars(mod).items()
                     if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); passed += 1; print("OK  ", name)
        except Exception as exc:
            failed += 1; print("FAIL", name, "::", repr(exc)[:400])
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
