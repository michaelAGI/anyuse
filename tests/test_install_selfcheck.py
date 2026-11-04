"""``anyuse doctor --install`` and docs/INSTALL_FOR_AGENTS.md, offline.

The five checks pass on this interpreter with a temp data dir, and each one
fails for its own reason when given a bad input (old Python, a missing import,
an unwritable or misplaced data dir, a missing entry point, a malformed
settings file). No socket is opened. The document's verification step (the
quickstart MCP client calling ``list_sites`` on the ``meta`` profile over
stdio) is run as written and its output compared with the document.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from anyuse import selfcheck
from anyuse.cli import cli

HERE = Path(__file__).resolve().parent.parent
DOC = HERE / "docs" / "INSTALL_FOR_AGENTS.md"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: os.environ.get(k) for k in ("ANYUSE_DATA_DIR", "ANYUSE_MCP_PROFILE")}
        os.environ["ANYUSE_DATA_DIR"] = str(self.tmp)
        os.environ.pop("ANYUSE_MCP_PROFILE", None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in self.tmp.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, **kw):
        return {row["check"]: row for row in selfcheck.install_checks(**kw)}


class ChecksTests(Tmp):
    def test_all_five_pass_offline_and_open_no_socket(self):
        with mock.patch.object(socket.socket, "connect",
                               side_effect=AssertionError("socket opened")), \
                mock.patch.object(socket, "create_connection",
                                  side_effect=AssertionError("socket opened")):
            checks = selfcheck.install_checks()
        self.assertEqual([row["check"] for row in checks],
                         ["python", "imports", "data dir writable", "mcp entry",
                          "settings readable"])
        self.assertTrue(all(row["ok"] for row in checks), checks)
        self.assertEqual(list(self.tmp.iterdir()), [], "the probe file is removed")
        text = selfcheck.render(checks)
        self.assertEqual(text.count("PASS  "), 5)
        self.assertTrue(text.endswith("5 passed, 0 failed"))
        settings_row = checks[-1]
        self.assertIn("absent, defaults apply", settings_row["found"])
        self.assertIn("ANYUSE_MCP_PROFILE=default (from default)", settings_row["found"])

    def test_old_python_fails(self):
        row = self.rows(version_info=(3, 10, 14))["python"]
        self.assertEqual((row["ok"], row["found"]), (False, "3.10.14"))
        self.assertIn("3.11", row["fix"])

    def test_missing_mcp_sdk_fails_imports_and_entry(self):
        import importlib

        def no_mcp(name):
            if name == "mcp" or name == selfcheck.MCP_SERVER:
                raise ImportError(f"No module named {name!r}")
            return importlib.import_module(name)
        rows = self.rows(import_module=no_mcp)
        self.assertFalse(rows["imports"]["ok"])
        self.assertIn("mcp (ImportError", rows["imports"]["found"])
        self.assertIn("[mcp]", rows["imports"]["fix"])
        self.assertFalse(rows["mcp entry"]["ok"])
        self.assertTrue(rows["python"]["ok"] and rows["settings readable"]["ok"])

    def test_missing_cryptography_fernet_fails_imports(self):
        import importlib

        def no_fernet(name):
            if name == "cryptography.fernet":
                raise ImportError(f"No module named {name!r}")
            return importlib.import_module(name)

        rows = self.rows(import_module=no_fernet)
        self.assertFalse(rows["imports"]["ok"])
        self.assertIn("cryptography.fernet (ImportError", rows["imports"]["found"])
        self.assertIn("[mcp]", rows["imports"]["fix"])
        self.assertTrue(all(row["ok"] for name, row in rows.items() if name != "imports"), rows)

    def test_missing_entry_point_fails(self):
        row = self.rows(find_spec=lambda name: None)["mcp entry"]
        self.assertFalse(row["ok"])
        self.assertIn(selfcheck.MCP_ENTRY, row["found"])

    def test_data_dir_absent_parent_writable_is_not_created(self):
        target = self.tmp / "new" / "deeper"
        row = self.rows(data_dir=target)["data dir writable"]
        self.assertTrue(row["ok"], row)
        self.assertIn("not created yet", row["found"])
        self.assertFalse((self.tmp / "new").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_unwritable_or_misplaced_data_dir_fails(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.assertFalse(self.rows(data_dir=locked)["data dir writable"]["ok"])
        self.assertFalse(self.rows(data_dir=locked / "child")["data dir writable"]["ok"])
        afile = self.tmp / "afile"
        afile.write_text("x")
        row = self.rows(data_dir=afile)["data dir writable"]
        self.assertFalse(row["ok"])
        self.assertIn("not a directory", row["found"])

    def test_settings_file_is_parsed_strictly(self):
        settings_file = self.tmp / "settings.toml"
        settings_file.write_text('ANYUSE_MCP_PROFILE = "meta"\n')
        row = self.rows()["settings readable"]
        self.assertTrue(row["ok"], row)
        self.assertIn("ANYUSE_MCP_PROFILE=meta (from file)", row["found"])
        for bad in ('ANYUSE_MCP_PROFILE = "sideways"\n', "ANYUSE_NOT_A_SETTING = 1\n",
                    "this is not toml = = \n"):
            settings_file.write_text(bad)
            row = self.rows()["settings readable"]
            self.assertFalse(row["ok"], bad)
            self.assertTrue(row["fix"])


class CliTests(Tmp):
    def test_doctor_install_prints_pass_lines_and_exits_zero(self):
        result = CliRunner().invoke(cli, ["doctor", "--install"])
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.output.strip().splitlines()
        self.assertEqual([line.split(":")[0] for line in lines[:5]],
                         ["PASS  python", "PASS  imports", "PASS  data dir writable",
                          "PASS  mcp entry", "PASS  settings readable"])
        self.assertNotIn("container runtime", result.output)
        result = CliRunner().invoke(cli, ["doctor", "--install", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(json.loads(result.output)["checks"]), 5)

    def test_doctor_install_fails_with_exit_one(self):
        (self.tmp / "settings.toml").write_text("ANYUSE_NOT_A_SETTING = 1\n")
        result = CliRunner().invoke(cli, ["doctor", "--install"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("FAIL  settings readable", result.output)
        self.assertIn("4 passed, 1 failed", result.output)


class DoctorTests(Tmp):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {
            "ANYUSE_API_MODALITY": "on", "ANYUSE_SPEND_DAILY_USD": "5",
            "ANYUSE_MODEL_PROVIDER": "claude-cli", "ANYUSE_LEARN_ENDPOINT": "",
        }))
        self.enterContext(mock.patch("platform.system", return_value="Linux"))
        self.which = self.enterContext(mock.patch("shutil.which", return_value=None))
        self.enterContext(mock.patch("anyuse.cli._desktop_probe", return_value={
            "pyobjc": False, "accessibility": None, "screen_recording": None,
        }))
        self.enterContext(mock.patch.object(socket, "socket", side_effect=AssertionError("socket created")))
        self.enterContext(mock.patch.object(socket, "create_connection",
                                           side_effect=AssertionError("socket opened")))
        self.enterContext(mock.patch.object(socket, "getaddrinfo",
                                           side_effect=AssertionError("DNS lookup")))

    def tearDown(self):
        self.doCleanups()
        super().tearDown()

    def doctor_json(self):
        result = CliRunner().invoke(cli, ["doctor", "--json"])
        self.assertEqual(result.exit_code, 0, (result.output, result.exception))
        return {row["check"]: row for row in json.loads(result.output)["checks"]}

    def test_darwin_docker_does_not_replace_apples_container(self):
        import anyuse.cli as cli_module

        self.which.side_effect = lambda name: "/usr/local/bin/docker" if name == "docker" else None
        real_exists = cli_module._os_path_exists
        with mock.patch("platform.system", return_value="Darwin"), \
                mock.patch.object(cli_module, "_os_path_exists", side_effect=lambda path:
                                  False if path == "/opt/homebrew/bin/container" else real_exists(path)):
            row = self.doctor_json()["container runtime"]
        self.assertFalse(row["ok"])
        self.assertTrue(row["optional"])
        self.assertIn("/usr/local/bin/docker", row["found"])
        self.assertIn("Apple's `container` CLI not found", row["found"])
        self.assertIn("loopback", row["found"])

    def test_learn_endpoints_are_validated_offline(self):
        cases = (
            ("", "unset"),
            ("   ", "unset"),
            ("http://127.0.0.1:9222", "loopback"),
            ("http://127.0.0.2:9222", "loopback"),
            ("http://[::1]:9222", "loopback"),
            ("http://localhost.:9222", "loopback"),
            ("http://CHROME.LOCALHOST.:9222", "loopback"),
            ("http://0.0.0.0:9222", "loopback"),
            ("http://[::]:9222", "loopback"),
            ("http://[::1%25lo0]:9222", "loopback"),
            ("http://[::1", "invalid URL"),
            ("http://10.0.0.2:99999", "invalid URL"),
            ("http://10.0.0.2:0", "invalid URL"),
            ("http://10.0.0.2:-1", "invalid URL"),
            ("http://10.0.0.2:nope", "invalid URL"),
            ("ftp://10.0.0.2:9222", "invalid URL"),
            ("http://:9222", "invalid URL"),
            ("http://192.168.64.2:9222", None),
            (" https://container.example:9222 ", None),
        )
        for endpoint, problem in cases:
            with self.subTest(endpoint=endpoint), \
                    mock.patch.dict(os.environ, {"ANYUSE_LEARN_ENDPOINT": endpoint}):
                row = self.doctor_json()["learn loop"]
                self.assertEqual(row["ok"], problem is None)
                self.assertTrue(row["optional"])
                if problem:
                    self.assertIn(problem, row["found"])
                    self.assertTrue(row["fix"])
                else:
                    self.assertIn("syntax ok; reachability not checked offline", row["found"])
                    self.assertEqual(row["fix"], "")

    def test_json_and_text_fail_when_a_required_check_fails(self):
        with mock.patch.dict(os.environ, {"ANYUSE_API_MODALITY": "off"}):
            for args in (["doctor"], ["doctor", "--json"]):
                with self.subTest(args=args):
                    result = CliRunner().invoke(cli, args)
                    self.assertEqual(result.exit_code, 1, (result.output, result.exception))
                    if "--json" in args:
                        rows = {r["check"]: r for r in json.loads(result.output)["checks"]}
                        self.assertFalse(rows["api modality"]["ok"])
                        self.assertFalse(rows["api modality"]["optional"])
                    else:
                        self.assertIn("[FAIL] api modality", result.output)

    def test_json_and_text_succeed_when_only_optional_checks_fail(self):
        rows = self.doctor_json()
        failed = [row for row in rows.values() if not row["ok"]]
        self.assertTrue(failed)
        self.assertTrue(all(row["optional"] for row in failed), failed)
        for name in ("container runtime", "learn loop", "model role agent"):
            self.assertFalse(rows[name]["ok"], rows[name])
        result = CliRunner().invoke(cli, ["doctor"])
        self.assertEqual(result.exit_code, 0, (result.output, result.exception))
        self.assertIn("[opt ] container runtime", result.output)
        self.assertIn("Compile and replay are ready", result.output)


class DocTests(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_doc_names_no_url_and_the_real_entry_point(self):
        # The only URL the doc names is the project's own repository to clone.
        urls = set(re.findall(r"https?://[^\s)`\"']+", self.text))
        self.assertEqual(urls, {"https://github.com/michaelAGI/anyuse"})
        self.assertTrue((HERE / "mcpserver" / "__main__.py").exists())
        self.assertIn("-m anyuse.mcpserver", self.text)
        doctor = cli.commands["doctor"]
        self.assertIn("--install", [opt for p in doctor.params for opt in p.opts])
        self.assertIn('/.venv/bin/anyuse" doctor --install', self.text)
        self.assertIn('pip install -e "${ANYUSE_CHECKOUT}[mcp]"', self.text)
        claude = re.search(r"claude mcp add [^\n]*\\\n[^\n]*", self.text).group(0)
        for part in ("--scope user", "--env ANYUSE_MCP_PROFILE=meta", " anyuse ",
                     '-- "$ANYUSE_CHECKOUT/.venv/bin/python" -P -m anyuse.mcpserver'):
            self.assertIn(part, claude)
        codex = re.search(r"codex mcp add [^\n]*\\\n[^\n]*", self.text).group(0)
        self.assertIn("--env ANYUSE_MCP_PROFILE=meta", codex)
        self.assertIn('env = { ANYUSE_MCP_PROFILE = "meta" }', self.text)

    def test_doc_says_what_plain_doctor_exits_with(self):
        # the code: the container runtime, the model roles and the learn endpoint are optional rows,
        # so a machine without them still exits 0
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"ANYUSE_DATA_DIR": tmp}):
                result = CliRunner().invoke(cli, ["doctor", "--json"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        rows = {r["check"]: r for r in json.loads(result.output)["checks"]}
        for check in ("container runtime", "learn loop", "model role planner", "model role agent"):
            self.assertTrue(rows[check]["optional"], check)
        para = self.text.split("(Plain `anyuse doctor`", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("fails on a machine without those", para)
        self.assertIn("exit status", para)
        self.assertIn("`opt`", para)

    def test_doc_verification_step_runs_offline_as_written(self):
        expected = re.search(r"Expected on a fresh data directory[^\n]*\n\n```\n(.*?)```",
                             self.text, re.S).group(1)
        tmp = tempfile.mkdtemp()
        try:
            env = dict(os.environ, ANYUSE_MCP_PROFILE="meta", ANYUSE_DATA_DIR=tmp)
            done = subprocess.run(
                [sys.executable, str(HERE / "examples" / "quickstart" / "mcp_call.py"),
                 "list_sites", "{}"],
                env=env, capture_output=True, text=True, timeout=120)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout, expected)


if __name__ == "__main__":
    unittest.main()
