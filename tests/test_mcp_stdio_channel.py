"""The MCP server's stdout carries JSON-RPC and nothing else.

``WorkflowStorage()`` prints a warning for every saved workflow it cannot load,
and ``default``'s ``tools/list`` and every ``call_operation`` construct one. If
that reached stdout, the client would read a non-JSON line on the protocol
channel. ``serve()`` keeps the real stdout for the protocol and points file
descriptor 1 at stderr for everything else.

Here the real entry point (``python -m anyuse.mcpserver``) runs as a child
process over pipes with a corrupt ``workflow.json`` in a temp data dir, under
``default`` (``tools/list``) and ``meta`` (``tools/list`` + ``call_operation``
on a compiled record whose host is not acknowledged, so nothing is sent).
Every stdout line must parse as JSON-RPC, and the warning must show up on
stderr instead.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from anyuse.execution.api_compile import compiled_record, persist_compiled

import test_routine_example as rex

TIMEOUT_S = 60


def exchange(env, requests, last_id):
    """Send JSON-RPC lines, read stdout until the response ``last_id`` arrives,
    then close stdin and read stdout to EOF (a buffered print is flushed at exit)."""
    proc = subprocess.Popen([sys.executable, "-m", "anyuse.mcpserver"], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    lines: "queue.Queue[str | None]" = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=pump, daemon=True).start()
    for message in requests:
        proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    out = []
    try:
        while True:
            line = lines.get(timeout=TIMEOUT_S)
            if line is None:
                break
            out.append(line)
            try:
                if json.loads(line).get("id") == last_id:
                    break
            except ValueError:
                continue
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    while True:
        try:
            line = lines.get(timeout=TIMEOUT_S)
        except queue.Empty:
            break
        if line is None:
            break
        out.append(line)
    proc.stdout.close()
    err = proc.stderr.read()
    proc.stderr.close()
    return out, err


def init_messages():
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "stdio-channel-test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


class StdioChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        data = os.path.join(self.tmp, "data")
        workflows = os.path.join(data, "workflows")
        os.makedirs(os.path.join(workflows, "workflow_broken"))
        with open(os.path.join(workflows, "workflow_broken", "workflow.json"), "w") as fh:
            fh.write("{this is not json")
        persist_compiled(rex.Storage(workflows), "crates_detail", compiled_record(
            rex.detail_template(), rex.PROOF, workflow="crates_detail"))
        self.env = dict(os.environ, ANYUSE_DATA_DIR=data,
                        ANYUSE_API_HOSTS_FILE=os.path.join(self.tmp, "hosts.json"),
                        ANYUSE_BLOCKED_HOSTS_FILE=os.path.join(self.tmp, "blocked.json"),
                        ANYUSE_NOTIFY_CHANNEL="none", PYTHONUNBUFFERED="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertProtocolOnly(self, lines, ids):
        self.assertTrue(lines)
        seen = set()
        for line in lines:
            message = json.loads(line)          # any non-JSON line fails here
            self.assertEqual(message.get("jsonrpc"), "2.0", line)
            seen.add(message.get("id"))
        self.assertTrue(set(ids) <= seen, (ids, lines))

    def test_default_tools_list_keeps_stdout_json(self):
        env = dict(self.env, ANYUSE_MCP_PROFILE="default")
        lines, err = exchange(env, init_messages() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}], 2)
        self.assertProtocolOnly(lines, [1, 2])
        self.assertIn("Could not load workflow", err)

    def test_meta_call_operation_keeps_stdout_json(self):
        env = dict(self.env, ANYUSE_MCP_PROFILE="meta")
        lines, err = exchange(env, init_messages() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "call_operation", "arguments": {
                    "site": "crates.io", "operation": "crates_detail",
                    "args": {"crate": "serde"}}}}], 3)
        self.assertProtocolOnly(lines, [1, 2, 3])
        result = next(json.loads(line) for line in lines if json.loads(line).get("id") == 3)
        payload = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(payload["class"], "host_not_acknowledged")
        self.assertIn("Could not load workflow", err)


if __name__ == "__main__":
    unittest.main()
