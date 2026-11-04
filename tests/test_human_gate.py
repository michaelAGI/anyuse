"""The terminal gate for `anyuse routine approve|reconcile` (offline, fake TTY).

Nothing opens the real /dev/tty: every gate gets a fake terminal object.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest

from anyuse.execution import human_gate as hg

DIGEST = "ab" * 32
SUMMARY = {"purpose": "approve", "manifest": "m", "version": "0.1.0", "run_id": "r",
           "step": "detail", "effects": {"class": "side_effecting", "reversible": False},
           "action": {"kind": "compiled_tool"}, "bound_inputs": {"crate": "serde_json"}}


class FakeTty:
    """A person at the terminal: reads the code the gate printed and types it
    (or ``answer`` when given)."""

    def __init__(self, answer=None):
        self.answer, self.written, self.opened = answer, [], 0

    def __call__(self):
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass

    def readline(self):
        if self.answer is not None:
            return self.answer + "\n"
        return re.search(r"type this code: (\w+)", "".join(self.written)).group(1) + "\n"


def gate(tty, *, env=None, ttys=True, optin=None, host="host-a"):
    return hg.TtyGate(env=env or {}, stdio_are_ttys=lambda: ttys, open_tty=tty,
                      nonce_factory=lambda: "n0", hostname=host,
                      optin_file=optin or os.path.join(tempfile.mkdtemp(), "optin.json"))


class GateTests(unittest.TestCase):
    def test_person_typing_the_shown_code_passes_and_sees_the_effect(self):
        tty = FakeTty()
        self.assertIsNone(gate(tty)(SUMMARY, DIGEST))
        shown = "".join(tty.written)
        self.assertIn("side_effecting", shown)
        self.assertIn("serde_json", shown)
        self.assertIn(DIGEST, shown)
        self.assertIn(hg.short_code(DIGEST, "n0"), shown)

    def test_wrong_or_empty_code_is_refused(self):
        for answer in ("", "000000", DIGEST, DIGEST[:6]):
            self.assertIn("did not match", gate(FakeTty(answer))(SUMMARY, DIGEST))

    def test_code_depends_on_digest_and_nonce(self):
        self.assertNotEqual(hg.short_code(DIGEST, "n0"), hg.short_code(DIGEST, "n1"))
        self.assertNotEqual(hg.short_code(DIGEST, "n0"), hg.short_code("cd" * 32, "n0"))

    def test_no_tty_on_stdin_or_stdout_is_refused_before_any_prompt(self):
        tty = FakeTty()
        self.assertIn("must both be a terminal", gate(tty, ttys=False)(SUMMARY, DIGEST))
        self.assertEqual(tty.opened, 0)

    def test_agent_markers_are_refused_without_opt_in(self):
        for env in ({"CLAUDECODE": "1"}, {"CLAUDE_CODE_ENTRYPOINT": "cli"},
                    {"CODEX_SANDBOX": "seatbelt"}, {"CODEX_ANYTHING": "x"}):
            tty = FakeTty()
            why = gate(tty, env=env)(SUMMARY, DIGEST)
            self.assertIn("agent environment marker", why)
            self.assertEqual(tty.opened, 0)
        self.assertEqual(hg.agent_markers({"PATH": "/bin", "CLAUDECODE": "1"}), ["CLAUDECODE"])

    def test_opt_in_written_from_a_clean_tty_allows_marked_terminals(self):
        optin = os.path.join(tempfile.mkdtemp(), "sub", "optin.json")
        # cannot be written from a marked terminal, nor without a TTY
        self.assertEqual(gate(FakeTty(), env={"CLAUDECODE": "1"}, optin=optin)
                         .write_optin()["status"], "refused")
        self.assertEqual(gate(FakeTty(), ttys=False, optin=optin).write_optin()["status"],
                         "refused")
        self.assertEqual(gate(FakeTty("nope"), optin=optin).write_optin()["status"], "refused")
        self.assertFalse(os.path.exists(optin))
        self.assertEqual(gate(FakeTty(), optin=optin).write_optin()["status"], "allowed")
        self.assertEqual(os.stat(optin).st_mode & 0o777, 0o600)
        marked = gate(FakeTty(), env={"CLAUDECODE": "1"}, optin=optin)
        self.assertIsNone(marked(SUMMARY, DIGEST))
        # the code is still required, and the opt-in is per machine
        self.assertIn("did not match", gate(FakeTty("x"), env={"CLAUDECODE": "1"},
                                            optin=optin)(SUMMARY, DIGEST))
        other_host = gate(FakeTty(), env={"CLAUDECODE": "1"}, optin=optin, host="host-b")
        self.assertIn("agent environment marker", other_host(SUMMARY, DIGEST))

    def test_opt_in_file_readable_by_others_or_forged_is_ignored(self):
        optin = os.path.join(tempfile.mkdtemp(), "optin.json")
        self.assertEqual(gate(FakeTty(), optin=optin).write_optin()["status"], "allowed")
        os.chmod(optin, 0o644)
        self.assertIsNotNone(gate(FakeTty(), env={"CLAUDECODE": "1"}, optin=optin)(
            SUMMARY, DIGEST))
        forged = os.path.join(tempfile.mkdtemp(), "optin.json")
        fd = os.open(forged, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"schema": hg.OPTIN_SCHEMA, "host": "host-a"}, fh)
        self.assertIsNotNone(gate(FakeTty(), env={"CLAUDECODE": "1"}, optin=forged)(
            SUMMARY, DIGEST))


if __name__ == "__main__":
    unittest.main()
