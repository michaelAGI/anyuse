"""Out-of-band human approval/reconcile for routines (offline).

An MCP client may itself be a model, so in the default MCP profile an
approval or reconcile counts only when a person wrote it with
`anyuse routine approve|reconcile --confirm <digest shown by the hand-off>`.
The receipt is bound to the run, step, bound inputs, effects, action pins and
manifest version; MCP resume then continues only if that receipt exists and
the fresh checks still hold. HTTP goes to a fake transport; the CLI runs in
process with a temp skills directory.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner

from anyuse.cli import cli
from anyuse.execution.api_compile import compiled_record, persist_compiled
from anyuse.execution import human_gate
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.skill_gen import RESUME_TOOL_NAME
from anyuse.execution.skill_manifest import validate_manifest
from anyuse.mcpserver import skill_tools

import test_routine_example as rex
from test_human_gate import FakeTty


def write_manifest(version="0.1.0"):
    """crates_lookup with its detail step declared side-effecting + approval."""
    with open(rex.MANIFEST_FILE, encoding="utf-8") as fh:
        m = json.load(fh)
    m["name"], m["version"] = "crates_approve", version
    detail = next(s for s in m["steps"] if s["id"] == "detail")
    detail["effects"] = {"class": "side_effecting", "reversible": False}
    detail["approval"] = {"required": True}
    detail["policy"] = {**detail.get("policy", {}), "retries": 0, "idempotent": False}
    return m


class Transport(rex.FakeTransport):
    def __init__(self, wrong_name=False):
        super().__init__()
        self.wrong_name = wrong_name

    def send(self, method, url, headers, body, timeout_s):
        if self.wrong_name and "/crates?" not in url:
            self.sent.append(url)
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps({"crate": {"name": "other", "description": "d"}}))
        return super().send(method, url, headers, body, timeout_s)


class Elicitor:
    """A client that shows elicitation forms: scripted answers, then decline."""

    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def ask(self, message, schema):
        self.asked.append((message, schema))
        return self.answers.pop(0) if self.answers else {"action": "decline"}


class HumanChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage, self.hosts = rex.setup(self.tmp)
        self.skills = os.path.join(self.tmp, "skills")
        os.makedirs(self.skills)
        self.register(write_manifest())
        self.transport = Transport()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def register(self, manifest):
        self.assertEqual(validate_manifest(manifest), [])
        with open(os.path.join(self.skills, "crates_approve.manifest.json"), "w") as fh:
            json.dump(manifest, fh)

    def mcp(self, name, args):
        return skill_tools.handle(name, args, directory=self.skills, storage=self.storage,
                                  hosts_path=self.hosts, launch_profile=True,
                                  executor=ApiStepExecutor(transport=self.transport))

    def cli(self, *args, tty=True, env_extra=None, answer=None):
        """Run the CLI as a person at a (fake) terminal with no agent markers,
        unless told otherwise."""
        env = {k: v for k, v in os.environ.items() if not human_gate.agent_markers({k: v})}
        env.update(env_extra or {})
        fake = FakeTty(answer)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(human_gate, "_stdio_are_ttys", lambda: tty), \
                mock.patch.object(human_gate, "_open_dev_tty", fake):
            return CliRunner().invoke(
                cli, ["routine", *args, "--skills-dir", self.skills], obj={})

    def at_approval(self):
        res = self.mcp("routine__crates_approve", {"q": "serde"})
        self.assertEqual(res["status"], "handed_off", res)
        # the default profile never takes a human answer from a plain tool call
        forged = self.mcp(RESUME_TOOL_NAME, {
            "run_id": res["run_id"], "expected_head": res["handoff"]["head"],
            "human": {"step": "pick", "outputs": {"crate": "serde_json"}}})
        self.assertEqual(forged["status"], "refused", forged)
        # the person answers the pick through the client's elicitation prompt,
        # then declines to be asked about the approval there
        asker = Elicitor([{"action": "accept",
                           "content": {"done": True, "crate": "serde_json"}}])
        res = skill_tools.handle(RESUME_TOOL_NAME, {"run_id": res["run_id"]},
                                 directory=self.skills, storage=self.storage,
                                 hosts_path=self.hosts, launch_profile=True,
                                 executor=ApiStepExecutor(transport=self.transport),
                                 elicitor=asker)
        self.assertEqual(len(asker.asked), 2)
        self.assertEqual(res["needs_human"]["asked"], "declined")
        self.assertEqual(res["status"], "handed_off", res)
        self.assertEqual(res["handoff"]["reason"], "approval")
        return res

    def test_cli_approval_lets_default_profile_resume_continue(self):
        res = self.at_approval()
        run_id, nxt = res["run_id"], res["handoff"]["next_step"]
        # without the receipt, resume stays handed off and sends nothing more
        again = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id})
        self.assertEqual(again["handoff"]["reason"], "approval")
        self.assertEqual(len(self.transport.sent), 1)
        shown = self.cli("show", run_id)
        self.assertEqual(shown.exit_code, 0, shown.output)
        self.assertIn(nxt["approval_digest"], shown.output)
        out = self.cli("approve", run_id, "detail", "--confirm", nxt["approval_digest"])
        self.assertEqual(out.exit_code, 0, out.output)
        self.assertEqual(json.loads(out.output)["status"], "approved")
        # the head the latest hand-off showed stays valid across the approval
        done = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id,
                                           "expected_head": again["handoff"]["head"]})
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["outputs"]["name"], "serde_json")
        self.assertEqual(len(self.transport.sent), 2)

    def test_digest_alone_is_not_sufficient(self):
        res = self.at_approval()
        run_id, digest = res["run_id"], res["handoff"]["next_step"]["approval_digest"]
        cases = [dict(tty=False), dict(env_extra={"CLAUDECODE": "1"}),
                 dict(env_extra={"CODEX_SANDBOX": "seatbelt"}), dict(answer="WRONG1")]
        for kw in cases:
            out = self.cli("approve", run_id, "detail", "--confirm", digest, **kw)
            self.assertEqual(out.exit_code, 1, (kw, out.output))
            self.assertIn("refused", out.output)
        still = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id})
        self.assertEqual(still["handoff"]["reason"], "approval")
        self.assertEqual(len(self.transport.sent), 1)
        # and piping the right code into stdin does not help
        with mock.patch.object(human_gate, "_stdio_are_ttys", lambda: False):
            out = CliRunner().invoke(cli, ["routine", "approve", run_id, "detail",
                                           "--confirm", digest, "--skills-dir", self.skills],
                                     input="ABCDEF\n", obj={})
        self.assertEqual(out.exit_code, 1, out.output)

    def test_in_band_mcp_approval_is_refused(self):
        res = self.at_approval()
        nxt = res["handoff"]["next_step"]
        out = self.mcp(RESUME_TOOL_NAME, {"run_id": res["run_id"], "approval": {
            "step": "detail", "inputs_digest": nxt["inputs_digest"]}})
        self.assertEqual(out["status"], "refused")
        self.assertEqual(len(self.transport.sent), 1)

    def test_wrong_digest_is_refused(self):
        res = self.at_approval()
        run_id, nxt = res["run_id"], res["handoff"]["next_step"]
        for bad in ("0" * 64, nxt["inputs_digest"]):
            out = self.cli("approve", run_id, "detail", "--confirm", bad)
            self.assertEqual(out.exit_code, 1, out.output)
            self.assertIn("does not match", out.output)
        wrong_step = self.cli("approve", run_id, "search", "--confirm", nxt["approval_digest"])
        self.assertEqual(wrong_step.exit_code, 1)
        still = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id})
        self.assertEqual(still["handoff"]["reason"], "approval")
        self.assertEqual(len(self.transport.sent), 1)

    def test_stale_manifest_version_is_refused(self):
        res = self.at_approval()
        run_id, nxt = res["run_id"], res["handoff"]["next_step"]
        self.register(write_manifest("0.1.1"))
        out = self.cli("approve", run_id, "detail", "--confirm", nxt["approval_digest"])
        self.assertEqual(out.exit_code, 1, out.output)
        self.assertIn("different manifest version", out.output)
        resumed = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id})
        self.assertEqual(resumed["status"], "refused")
        self.assertEqual(len(self.transport.sent), 1)

    def test_approval_does_not_bypass_the_fresh_check(self):
        res = self.at_approval()
        run_id, nxt = res["run_id"], res["handoff"]["next_step"]
        out = self.cli("approve", run_id, "detail", "--confirm", nxt["approval_digest"])
        self.assertEqual(out.exit_code, 0, out.output)
        persist_compiled(self.storage, "crates_detail", compiled_record(
            rex.detail_template("https://crates.io/api/v1/crates/{crate}?v=2"), rex.PROOF,
            workflow="crates_detail"))
        resumed = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id})
        self.assertEqual(resumed["status"], "refused", resumed)
        self.assertIn("changed since the manifest pinned", resumed["error"])
        self.assertEqual(len(self.transport.sent), 1)

    def test_uncertain_write_needs_a_cli_reconcile(self):
        self.transport = Transport(wrong_name=True)
        res = self.at_approval()
        run_id = res["run_id"]
        self.cli("approve", run_id, "detail", "--confirm",
                 res["handoff"]["next_step"]["approval_digest"])
        unsure = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id})
        self.assertEqual(unsure["status"], "needs_reconcile", unsure)
        digest = unsure["handoff"]["next_step"]["reconcile_digest"]
        refused = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id, "reconcile": {
            "step": "detail", "outcome": "not_done"}})
        self.assertEqual(refused["status"], "refused")
        outputs = json.dumps({"name": "serde_json", "description": "checked by hand"})
        bad = self.cli("reconcile", run_id, "detail", "--outcome", "completed",
                       "--outputs", outputs, "--confirm", "f" * 64)
        self.assertEqual(bad.exit_code, 1, bad.output)
        ok = self.cli("reconcile", run_id, "detail", "--outcome", "completed",
                      "--outputs", outputs, "--confirm", digest)
        self.assertEqual(ok.exit_code, 0, ok.output)
        done = self.mcp(RESUME_TOOL_NAME, {"run_id": run_id,
                                           "expected_head": unsure["handoff"]["head"]})
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["outputs"]["description"], "checked by hand")
        self.assertEqual(len(self.transport.sent), 2)


if __name__ == "__main__":
    unittest.main()
